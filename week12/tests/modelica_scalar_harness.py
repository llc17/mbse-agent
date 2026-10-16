"""Evaluate the current package's scalar algorithm at fixed inputs, without OMC.

This is a deliberately narrow test interpreter, not a Modelica simulator. It
executes the source algorithm (not a second contact implementation); MSL frames,
connections, DAEs, events and numerical integration remain untested here.
Unknown syntax fails closed. Arrays use Modelica's one-based indices.
"""
import ast
import math
import re
from pathlib import Path

PACKAGE = Path(__file__).resolve().parents[1]/'models/components/V11PhysicalB1/package.mo'
FUNCTIONS = {'sqrt': math.sqrt, 'tanh': math.tanh, 'min': min, 'max': max, 'abs': abs}
ALLOWED = (ast.Expression, ast.Constant, ast.Name, ast.Load, ast.BinOp, ast.UnaryOp,
           ast.BoolOp, ast.Compare, ast.Subscript, ast.Call, ast.Add, ast.Sub,
           ast.Mult, ast.Div, ast.Pow, ast.USub, ast.UAdd, ast.Not, ast.And,
           ast.Or, ast.Eq, ast.NotEq, ast.Lt, ast.LtE, ast.Gt, ast.GtE)
FOR_RANGES = {
    '1:nSegments': ('1', 'nSegments'),
    '1:nPoints': ('1', 'nPoints'),
    '2:nPoints-1': ('2', 'nPoints - 1'),
}


class OneBasedArray(dict):
    """Fixed-size Modelica array that rejects zero and out-of-range indices."""

    def __init__(self, size, values=()):
        self.size = size
        super().__init__()
        for index, value in enumerate(values, 1):
            self[index] = value

    def _check(self, index):
        if isinstance(index, bool) or not isinstance(index, int) or not 1 <= index <= self.size:
            raise IndexError(f'one-based array index {index} outside 1..{self.size}')

    def __getitem__(self, index):
        self._check(index)
        return super().__getitem__(index)

    def __setitem__(self, index, value):
        self._check(index)
        super().__setitem__(index, value)


def expression(text, values):
    text = text.strip().replace('^', '**').replace('Modelica.Constants.small', '1e-60')
    text = re.sub(r'\btrue\b', 'True', text)
    text = re.sub(r'\bfalse\b', 'False', text)
    tree = ast.parse(text, mode='eval')
    for node in ast.walk(tree):
        if not isinstance(node, ALLOWED):
            raise ValueError('Unsupported scalar syntax: '+type(node).__name__)
        if isinstance(node, ast.Call) and (not isinstance(node.func, ast.Name) or node.func.id not in FUNCTIONS):
            raise ValueError('Unsupported function')
    return eval(compile(tree, '<actual Modelica scalar expression>', 'eval'),
                {'__builtins__': {}, **FUNCTIONS}, values)


def section(name):
    source = PACKAGE.read_text(encoding='utf-8')
    match = re.search(r'  model '+re.escape(name)+r'\b(.*?)  end '+re.escape(name)+r';', source, re.S)
    if match is None:
        raise ValueError('Model section missing: '+name)
    return match.group(1)


def algorithm_nodes(text):
    """Parse assignment / if / for subset; never execute unrecognised statements."""
    tokens, pending = [], ''
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        pending = (pending+' '+line).strip()
        if pending.endswith((' then', ' loop', ';')) or pending == 'else':
            tokens.append(pending)
            pending = ''
    if pending:
        raise ValueError('Incomplete algorithm statement')

    def block(index, ends):
        nodes = []
        while index < len(tokens) and tokens[index] not in ends:
            token = tokens[index]
            if token.startswith('if ') and token.endswith(' then'):
                yes, index = block(index+1, {'else', 'end if;'})
                no = []
                if index < len(tokens) and tokens[index] == 'else':
                    no, index = block(index+1, {'end if;'})
                if index >= len(tokens) or tokens[index] != 'end if;':
                    raise ValueError('Unterminated if')
                nodes.append(('if', token[3:-5], yes, no))
            elif token.startswith('for ') and token.endswith(' loop'):
                match = re.fullmatch(r'for (\w+) in (.+) loop', token)
                if match is None:
                    raise ValueError('Unsupported for range')
                variable, range_text = match.groups()
                bounds = FOR_RANGES.get(re.sub(r'\s+', '', range_text))
                if bounds is None:
                    raise ValueError('Unsupported for range')
                body, index = block(index+1, {'end for;'})
                if index >= len(tokens) or tokens[index] != 'end for;':
                    raise ValueError('Unterminated for')
                nodes.append(('for', variable, *bounds, body))
            else:
                match = re.fullmatch(r'(\w+)(?:\[(.*?)\])?\s*:=\s*(.*);', token)
                if match is None:
                    raise ValueError('Unsupported statement: '+token)
                nodes.append(('assign', *match.groups()))
            index += 1
        return nodes, index
    nodes, index = block(0, set())
    if index != len(tokens):
        raise ValueError('Unconsumed algorithm')
    return nodes


def execute(nodes, values):
    for node in nodes:
        if node[0] == 'assign':
            _, name, index, rhs = node
            value = expression(rhs, values)
            if index is None:
                values[name] = value
            else:
                values[name][expression(index, values)] = value
        elif node[0] == 'if':
            execute(node[2] if expression(node[1], values) else node[3], values)
        elif node[0] == 'for':
            first = int(expression(node[2], values))
            last = int(expression(node[3], values))
            for index in range(first, last+1):
                values[node[1]] = index
                execute(node[4], values)
        else:
            raise ValueError('Unknown node')


def contact(points, kinds, *, x, z, vx=0, vz=0, omega=0, mu=0):
    source = section('PolylineWheelContact')
    preamble, remainder = source.split('\n  algorithm\n', 1)
    algorithm, equations = remainder.split('\n  equation\n', 1)
    env = {'nPoints': len(points), 'nSegments': len(points)-1,
           'xPoints': OneBasedArray(len(points), (p[0] for p in points)),
           'zPoints': OneBasedArray(len(points), (p[1] for p in points)),
           'vertexKind': OneBasedArray(len(kinds), kinds), 'R': .1, 'k': 100000.,
           'c': 200., 'mu': mu, 'vreg': .001, 'xCenter': x, 'zCenter': z,
           'vxCenter': vx, 'vzCenter': vz, 'omegaAbsolute': omega}
    # Evaluate the actual vector definitions in declaration order.
    vector_pattern = r'final parameter (?:SI\.\w+|Real) (\w+)\[nSegments\]\s*=\s*\{(.*?) for i in 1:nSegments\};'
    vectors = re.findall(vector_pattern, preamble, re.S)
    if [name for name, _ in vectors] != ['segmentLength', 'tx', 'tz', 'nx', 'nz']:
        raise ValueError('Unsupported vector declarations')
    for name, formula in vectors:
        env[name] = OneBasedArray(env['nSegments'])
        for i in range(1, env['nSegments']+1):
            env['i'] = i
            env[name][i] = expression(' '.join(formula.split()), env)
    arrays = re.findall(r'(?:SI\.\w+|Real|Boolean) (\w+)\[(nSegments|nPoints)\]', preamble)
    for name, dimension in arrays:
        env.setdefault(name, OneBasedArray(env[dimension]))
    execute(algorithm_nodes(algorithm), env)
    # Runtime assertion is taken from the actual source, not recreated geometry.
    guard = re.search(r'assert\((not insideTerrain),', equations)
    if guard is None or not expression(guard.group(1), env):
        raise ValueError('INVALID: wheel centre inside terrain')
    return env


def drive(torque, omega, *, available=100):
    source = section('IdealDriveSupply')
    env = {'torqueCommand': torque, 'omegaRelative': omega, 'efficiency': .8,
           'ratedTorque': .2, 'ratedMechanicalPower': 4, 'supplyPower': 10,
           'energyAvailable': available, 'voltage': 24}
    for name in ('shaftPower', 'dcPower', 'driveLoss', 'driveLossPower', 'busCurrent'):
        match = re.search(r'output SI\.\w+ '+name+r'\s*=\s*([^;]+);', source)
        if match is None:
            raise ValueError('Output binding missing: '+name)
        env[name] = expression(match.group(1), env)
    for name in ('energyAvailable', 'driveDissipatedEnergy'):
        match = re.search(r'der\('+name+r'\)\s*=\s*([^;]+);', source)
        if match is None:
            raise ValueError('Energy derivative missing')
        env['der_'+name] = expression(match.group(1), env)
    assertions = re.findall(r'assert\((.*?),\s*"[^"]*"\);', source, re.S)
    if len(assertions) != 5:
        raise ValueError('Unexpected drive assertion set')
    for condition in assertions:
        if not expression(' '.join(condition.split()), env):
            raise ValueError('INVALID: drive rating or energy limit')
    return env
