"""Fixed-input evaluator for the B contact's acyclic scalar equation subset.

Reads expressions from the actual model. Does not emulate Modelica events,
DAEs, MSL frames, or integration. Unknown equation shapes are rejected.
"""
import re
from week12.tests.modelica_scalar_harness import expression, section, OneBasedArray


def equation(source, name, index=None):
    target = re.escape(name)+(r'\['+re.escape(index)+r'\]' if index is not None else '')
    match = re.search(r'\b'+target+r'\s*=\s*([^;]+);', source)
    if match is None:
        raise ValueError('Missing equation '+name+' '+str(index))
    return ' '.join(match.group(1).split())


def evaluate(value, env):
    if value.startswith('if '):
        match = re.fullmatch(r'if (.*?) then (.*?) else (.*)', value)
        if match is None:
            raise ValueError('Unrecognized conditional equation')
        condition, yes, no = match.groups()
        return expression(yes if expression(condition, env) else no, env)
    return expression(value, env)


def contact(points, kinds, *, x, z, vx=0, vz=0, omega=0, mu=0):
    source = section('PolylineWheelContact')
    if '\n  algorithm' in source:
        raise ValueError('Expected equation contact')
    n, m = len(points), len(points)-1
    env = {'nPoints': n, 'nSegments': m, 'R': .1, 'k': 100000., 'c': 200., 'mu': mu,
           'vreg': .001, 'xCenter': x, 'zCenter': z, 'vxCenter': vx, 'vzCenter': vz,
           'omegaAbsolute': omega, 'xPoints': OneBasedArray(n, [p[0] for p in points]),
           'zPoints': OneBasedArray(n, [p[1] for p in points]), 'vertexKind': OneBasedArray(n, kinds)}
    preamble = source.split('\n  equation\n')[0]
    vectors = re.findall(r'final parameter (?:SI\.\w+|Real) (\w+)\[nSegments\]\s*=\s*\{(.*?) for i in 1:nSegments\};', preamble, re.S)
    if [name for name, _ in vectors] != ['segmentLength', 'tx', 'tz', 'nx', 'nz']:
        raise ValueError('Unexpected geometry vector definitions')
    for name, formula in vectors:
        env[name] = OneBasedArray(m)
        for i in range(1, m+1):
            env['i'] = i
            env[name][i] = expression(' '.join(formula.split()), env)
    def fill(name, size, *, indices=None, index='i'):
        env.setdefault(name, OneBasedArray(size))
        rhs = equation(source, name, index)
        for i in (range(1, size+1) if indices is None else indices):
            env['i'] = i
            target = expression(index, env)
            env[name][target] = evaluate(rhs, env)
    for name in ('projection', 'distanceFace', 'faceActive', 'deltaFace', 'vnFace', 'vtFace',
                 'feFace', 'fdFace', 'fnFace', 'ftFace', 'dnFace', 'dtFace', 'contactFace'):
        fill(name, m)
    for name in ('distanceVertex', 'vertexNx', 'vertexNz'):
        fill(name, n)
    env['vertexActive'] = OneBasedArray(n)
    env['vertexActive'][1] = expression(equation(source, 'vertexActive', '1'), env)
    env['vertexActive'][n] = expression(equation(source, 'vertexActive', 'nPoints'), env)
    fill('vertexActive', n, indices=range(2, n))
    for name in ('deltaVertex', 'vnVertex', 'vtVertex', 'feVertex', 'fdVertex', 'fnVertex',
                 'ftVertex', 'dnVertex', 'dtVertex', 'contactVertex'):
        fill(name, n)
    for suffix, size in (('Face', m), ('Vertex', n)):
        for prefix in ('selectedPenetration', 'selectedSlip', 'selectedFeature'):
            name = prefix+suffix
            env[name] = OneBasedArray(size+1)
            env[name][1] = expression(equation(source, name, '1'), env)
        selected = suffix.lower()+'SelectionImproves'
        env[selected] = OneBasedArray(size)
        for i in range(1, size+1):
            env['i'] = i
            env[selected][i] = evaluate(equation(source, selected, 'i'), env)
            for prefix in ('selectedPenetration', 'selectedSlip', 'selectedFeature'):
                name = prefix+suffix
                env[name][i+1] = evaluate(equation(source, name, 'i + 1'), env)
    for name in ('penetration', 'slip', 'featureId'):
        env[name] = expression(equation(source, name), env)
    # The output RHS is read from source; only its specific sum syntax is supported.
    for name in ('Fx', 'Fz', 'normal', 'elasticEnergy', 'normalLossPower', 'frictionLossPower', 'contactCount', 'contactTorqueY'):
        rhs = equation(source, name)
        def vector_sum(match):
            formula, bound = match.groups()
            values = []
            for i in range(1, env[bound]+1):
                env['i'] = i
                values.append(expression(formula, env))
            return repr(sum(values))
        rhs = re.sub(r'sum\(\{(.*?) for i in 1:(nSegments|nPoints)\}\)', vector_sum, rhs)
        rhs = re.sub(r'sum\((\w+)\)', lambda match: repr(sum(env[match.group(1)].values())), rhs)
        env[name] = expression(rhs, env)
    return env
