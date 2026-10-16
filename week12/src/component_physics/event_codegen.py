"""Structural guard for the observed OMC cached-relation aliasing defect.

This is a generated-code consistency check, not a compiler proof or simulation.
Only the two physical operands and comparison operator identify a relation;
temporary hysteresis scale arguments are deliberately excluded.
"""
import re


def _arguments(source, start):
    depth, beginning, arguments = 1, start, []
    for index in range(start, len(source)):
        char = source[index]
        if char == '(':
            depth += 1
        elif char == ')':
            depth -= 1
            if depth == 0:
                arguments.append(source[beginning:index])
                return arguments
        elif char == ',' and depth == 1:
            arguments.append(source[beginning:index])
            beginning = index+1
    raise ValueError('Truncated comparison call')


def audit_source(source):
    source = re.sub(r'/\*.*?\*/', '', source, flags=re.S)
    calls = list(re.finditer(r'\b(GreaterEqZC|GreaterZC|LessEqZC|LessZC)\s*\(', source))
    relations = {}
    issues = []
    observed = 0
    for call in calls:
        args = _arguments(source, call.end())
        if len(args) != 5:
            issues.append('Unexpected ZC comparison signature')
            continue
        cached = re.fullmatch(r'\s*data->simulationInfo->storedRelations\[(\d+)\]\s*', args[4])
        if cached is None:
            issues.append('Nonconstant/unrecognized relation cache reference')
            continue
        index = int(cached.group(1))
        operands = [re.sub(r'\s+', '', argument) for argument in args[:2]]
        signature = (call.group(1), *operands)
        if index in relations and relations[index] != signature:
            issues.append(f'Relation cache {index} aliases distinct comparison operands')
        relations[index] = signature
        observed += 1
    if not observed:
        issues.append('No cached zero-crossing relations observed')
    return {'status': 'FAIL' if issues else 'PASS', 'observed_calls': observed,
            'distinct_cache_slots': len(relations), 'issues': sorted(set(issues)),
            'scope': 'No duplicate cache slot for distinct emitted scalar operands; not a full semantic proof'}
