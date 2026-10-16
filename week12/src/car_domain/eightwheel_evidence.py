"""Read-only final gates for the frozen fourteen-scene eightwheel route.

Scene summaries are never publication proof. Actual artifacts, owned process
records and the execution ledger must agree before a PASS can be accepted.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path, PurePosixPath
import math
import re
import stat

from .outcomes import REQUIRED_SEQUENCE, make_outcome

BODY_SHA = '8b552d569926f0a2bdf05f473544dd58b095f50eb9f2b0225b0bc9219cf6311b'
SUCCESS = dict(agent_execution_status='COMPLETED', chain_status='TRUSTED_PHYSICAL_PASS',
               vehicle_task_status='PASS', tool_status='PASS', numerical_status='PASS',
               evidence_status='COMPLETE')
PROCESS_FIELDS = ('command', 'cwd', 'returncode', 'duration_s', 'pid',
                  'actual_started_utc', 'actual_ended_utc', 'absolute_cap_s',
                  'timed_out', 'uncertain', 'interrupted')


def _require(ok, reason):
    if not ok:
        raise ValueError('EIGHTWHEEL_EVIDENCE_' + reason)


def _number(value, *, positive=False):
    _require(type(value) in (int, float) and math.isfinite(value)
             and (value > 0 if positive else value >= 0), 'NONFINITE_OR_INVALID_TIME')
    return value


def _unlinked(path):
    """Reject links and Windows junctions at every existing ancestor."""
    path = Path(path).absolute()
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            raise ValueError('EIGHTWHEEL_EVIDENCE_MISSING_PATH') from None
        _require(not stat.S_ISLNK(info.st_mode)
                 and not (getattr(info, 'st_file_attributes', 0) & 0x400), 'LINK_OR_REPARSE_PATH')
    return path.resolve(strict=True)


def _relative_path(base, name):
    _require(isinstance(name, str) and bool(name) and '\\' not in name
             and ':' not in name and '\x00' not in name, 'INVALID_RELATIVE_PATH')
    parts = name.split('/')
    _require(not PurePosixPath(name).is_absolute()
             and all(p not in ('', '.', '..') for p in parts), 'PATH_ESCAPE')
    path = _unlinked(Path(base).joinpath(*parts))
    _require(path.is_relative_to(Path(base).resolve()), 'PATH_ESCAPE')
    _require(path.is_file(), 'ARTIFACT_NOT_FILE')
    return path


def _absolute_path(value, roots):
    _require(isinstance(value, str) and Path(value).is_absolute(), 'EXPECTED_ABSOLUTE_PATH')
    candidate = Path(value).absolute()
    _require('..' not in candidate.parts and candidate.name.casefold() != '.env', 'SOURCE_PATH_ESCAPE')
    # Constrain paths BEFORE reading or hashing them.
    _require(any(candidate.is_relative_to(Path(root).absolute()) for root in roots), 'SOURCE_PATH_ESCAPE')
    path = _unlinked(candidate)
    _require(any(path.is_relative_to(Path(root).resolve()) for root in roots), 'SOURCE_PATH_ESCAPE')
    _require(path.is_file(), 'SOURCE_NOT_FILE')
    return path


def _files(manifest, base, sha):
    table = manifest.get('files')
    _require(isinstance(table, dict) and bool(table), 'MISSING_FILE_TABLE')
    checked = {}
    aliases = set()
    for name, record in table.items():
        path = _relative_path(base, name)
        key = str(path).casefold()
        _require(key not in aliases, 'DUPLICATE_ARTIFACT_PATH')
        aliases.add(key)
        _require(isinstance(record, dict) and type(record.get('bytes')) is int
                 and record['bytes'] >= 0 and isinstance(record.get('sha256'), str)
                 and re.fullmatch('[0-9a-f]{64}', record['sha256']) is not None, 'INVALID_FILE_RECORD')
        _require(path.stat().st_size == record['bytes'] and sha(path) == record['sha256'], 'ARTIFACT_BYTES_DRIFT')
        checked[name] = path
    return checked


def _required(files, name):
    _require(name in files, 'MISSING_ARTIFACT:' + name)
    return files[name]


def _process(record, operations, cap, command, cwd, used_tokens):
    _require(isinstance(record, dict) and all(k in record for k in PROCESS_FIELDS), 'MISSING_ACTUAL_PROCESS')
    _require(type(record['returncode']) is int and record['returncode'] == 0
             and all(record[k] is False for k in ('timed_out', 'uncertain', 'interrupted')),
             'ACTUAL_PROCESS_NOT_PASS')
    duration = _number(record['duration_s'])
    actual_cap = _number(record['absolute_cap_s'], positive=True)
    _require(duration <= actual_cap <= cap, 'ACTUAL_PROCESS_OVER_CAP')
    _require(type(record['pid']) is int and record['pid'] > 0, 'INVALID_ACTUAL_PID')
    try:
        start = datetime.fromisoformat(record['actual_started_utc'].replace('Z', '+00:00'))
        end = datetime.fromisoformat(record['actual_ended_utc'].replace('Z', '+00:00'))
        _require(start.utcoffset() is not None and end.utcoffset() is not None
                 and end >= start and abs((end-start).total_seconds()-duration) < 2,
                 'ACTUAL_PROCESS_TIMESTAMPS')
    except (TypeError, AttributeError, ValueError):
        raise ValueError('EIGHTWHEEL_EVIDENCE_ACTUAL_PROCESS_TIMESTAMPS') from None
    _require(record['command'] == list(map(str, command))
             and Path(record['cwd']).resolve() == Path(cwd).resolve(), 'ACTUAL_COMMAND_MISMATCH')
    token = record.get('ledger_operation_token')
    _require(isinstance(token, str) and bool(token) and token not in used_tokens, 'DUPLICATE_OR_MISSING_OPERATION')
    matches = [row for row in operations if row.get('token') == token]
    _require(len(matches) == 1 and matches[0].get('status') == 'RETURNED', 'OPERATION_NOT_RETURNED')
    row = matches[0]
    _require(all(row.get(k) == v for k, v in record.items()), 'ACTUAL_PROCESS_LEDGER_MISMATCH')
    used_tokens.add(token)
    return row


def _provenance(record, paths, roots, sha):
    table = record.get('evidence_files')
    _require(isinstance(table, dict) and bool(table), 'MISSING_VERIFIER_PROVENANCE')
    checked = {}
    for value, evidence in table.items():
        path = _absolute_path(value, roots)
        _require(isinstance(evidence, dict) and type(evidence.get('bytes')) is int
                 and evidence['bytes'] == path.stat().st_size
                 and evidence.get('sha256') == sha(path), 'VERIFIER_INPUT_OR_REPORT_DRIFT')
        _require(path not in checked, 'DUPLICATE_VERIFIER_PROVENANCE')
        checked[path] = evidence
    _require(all(Path(path).resolve() in checked for path in paths), 'INCOMPLETE_VERIFIER_PROVENANCE')


def _passed_report(report, section):
    block = report.get(section)
    _require(isinstance(block, dict) and block.get('status') == 'PASS', section.upper() + '_NOT_PASS')
    checks = block.get('checks')
    _require(isinstance(checks, list) and bool(checks)
             and all(isinstance(c, dict) and isinstance(c.get('code'), str)
                     and bool(c['code']) and c.get('status') == 'PASS' for c in checks),
             section.upper() + '_CHECKS_NOT_PASS')
    _require(len({c['code'] for c in checks}) == len(checks), section.upper() + '_DUPLICATE_CHECK')


def verify_manifest_binding(manifest, result, task, proposal, owner, require_worker_exit=True):
    """Verify actual artifacts and owned ledger proof; never manufacture a PASS."""
    from . import eightwheel_route as route
    from .eightwheel_profile import PROFILE, PHYSICAL_SHA, CONTRACT_SHA, AUTH_SHA, WEEK12_ROOT

    route.verify_confirmation(proposal, task['session_id'])
    _require(owner == task.get('worker_owner') and bool(owner), 'WRONG_WORKER_OWNER')
    _require(task.get('proposal_id') == proposal.get('proposal_id')
             and task.get('project_id') == proposal.get('project_id')
             and task.get('session_id') == proposal.get('session_id'), 'TASK_PROPOSAL_MISMATCH')
    expected = dict(task_id=task['task_id'], project_id=task['project_id'],
                    proposal_id=proposal['proposal_id'], session_id=task['session_id'],
                    spec_digest=proposal['spec_digest'], displayed_digest=proposal['displayed_digest'],
                    physical_spec_sha256=PHYSICAL_SHA, scope_proposal_sha256=CONTRACT_SHA,
                    authorization_sha256=AUTH_SHA, profile=PROFILE, owner=owner)
    _require(isinstance(manifest, dict) and manifest.get('schema') == 'v11-eightwheel-manifest-1'
             and isinstance(manifest.get('binding'), dict)
             and all(manifest['binding'].get(k) == v for k, v in expected.items()), 'MANIFEST_BINDING_MISMATCH')
    for key in ('project_id', 'version_id'):
        value = task.get(key)
        _require(isinstance(value, str) and bool(value) and re.fullmatch('[A-Za-z0-9_-]+', value) is not None,
                 'INVALID_PROJECT_OR_VERSION_ID')
    base = _unlinked(Path(WEEK12_ROOT) / 'projects' / task['project_id'] / 'versions' / task['version_id'])
    path_text = result.get('outcome_manifest_path')
    _require(isinstance(path_text, str) and Path(path_text).is_absolute(), 'MISSING_OUTCOME_MANIFEST_PATH')
    actual_manifest = _absolute_path(path_text, [base])
    _require(route.sha(actual_manifest) == result.get('outcome_manifest_sha256')
             and route.read(actual_manifest) == manifest, 'MANIFEST_BYTES_DRIFT')
    payload = manifest.get('outcome')
    compared = {k:v for k,v in result.items()
                if k not in ('outcome_manifest_path', 'outcome_manifest_sha256')}
    if compared.get('published_version_id') is not None:
        _require(compared['published_version_id'] == task['version_id']
                 and task.get('state') == 'succeeded' and isinstance(payload, dict)
                 and payload.get('agent_execution_status') == 'COMPLETED'
                 and payload.get('published_version_id') is None, 'INVALID_PUBLISHED_VERSION')
        compared['published_version_id'] = None
    _require(isinstance(payload, dict) and payload == compared, 'IMMUTABLE_OUTCOME_MISMATCH')
    rows = payload.get('results')
    _require(isinstance(rows, list) and len(rows) <= len(REQUIRED_SEQUENCE)
             and all(isinstance(row, dict) for row in rows)
             and tuple((r.get('scene'), r.get('variant')) for r in rows) == REQUIRED_SEQUENCE[:len(rows)],
             'SCENE_SEQUENCE_MISMATCH')
    _require(payload.get('failure_audit') is None and payload.get('failure_audit_promotes_result') is False,
             'FAILURE_AUDIT_FORBIDDEN')
    expected_outcome = make_outcome(rows, error=payload.get('error'))
    expected_outcome['profile'] = PROFILE
    _require(payload == expected_outcome, 'OUTCOME_NOT_DERIVED_FROM_ACTUAL_SCENES')
    files = _files(manifest, base, route.sha)
    if payload.get('agent_execution_status') != 'COMPLETED':
        _require(payload.get('vehicle_task_status') == 'UNKNOWN'
                 and payload.get('chain_status') == 'BLOCKED_TOOL_OR_NUMERIC', 'FAILED_OUTCOME_PROMOTED')
        return True
    _require(all(payload.get(k) == v for k,v in SUCCESS.items()), 'COMPLETION_TUPLE_INVALID')
    with route.transaction() as (_, scope):
        route.assert_completion_scope(scope, task, require_worker_exit=require_worker_exit)
        _verify_completed(route, manifest, rows, task, base, files, scope)
        # Recheck source ownership and budget after hashing the actual evidence.
        route.assert_completion_scope(scope, task, require_worker_exit=require_worker_exit)
    return True


def _verify_completed(route, manifest, rows, task, base, files, scope):
    from .eightwheel_preparation import verification_command
    from week12.src.component_physics.runner import OMC

    original = Path(route.RUN) / 'generated' / task['task_id']
    roots = [base, original,route.RUN,route.PROPOSAL_PATH.parent]
    copied_binding = route.read(_required(files, 'generated-evidence/source-binding.json'))
    route.assert_binding(copied_binding)
    _require(copied_binding.get('proposal_sha256') == manifest['binding']['physical_spec_sha256']
             and copied_binding.get('scope_proposal_sha256') == manifest['binding']['scope_proposal_sha256'],
             'GENERATED_SPEC_BINDING_MISMATCH')
    generated = copied_binding.get('generated', {})
    _require(Path(generated.get('directory', '')).resolve() == original.resolve(), 'UNOWNED_GENERATED_SOURCE')
    generation_manifest = _absolute_path(generated.get('manifest'), [original])
    graph = _absolute_path(generated.get('graph'), [original])
    model_manifest = route.read(generation_manifest)
    package = _absolute_path(model_manifest.get('package_path'), [original])
    sysml = _absolute_path(model_manifest.get('sysml_path'), [original])
    _require(route.sha(package) == BODY_SHA, 'FROZEN_MODEL_BODY_DRIFT')
    copy_map = route.read(_required(files, 'generated-evidence/copy-source-map.json'))
    _require(isinstance(copy_map, list) and bool(copy_map), 'MISSING_GENERATED_COPY_PROVENANCE')
    mapped = {}
    for entry in copy_map:
        _require(isinstance(entry, dict), 'INVALID_GENERATED_COPY')
        source = _absolute_path(entry.get('original_source_path'), [original])
        copy_name = entry.get('copy')
        _require(isinstance(copy_name, str) and '/' not in copy_name and copy_name not in ('', '.', '..'),
                 'INVALID_GENERATED_COPY_NAME')
        copied = _required(files, 'generated-evidence/' + copy_name)
        _require(source not in mapped and route.sha(source) == entry.get('sha256') == route.sha(copied),
                 'GENERATED_COPY_DRIFT')
        mapped[source] = copied
    required_sources = [generation_manifest, graph, package, sysml, original/'source-binding.json']
    _require(all(Path(p).resolve() in mapped for p in required_sources), 'GENERATED_COPY_INCOMPLETE')
    generated_files = copied_binding.get('generated_files')
    _require(isinstance(generated_files, list) and bool(generated_files), 'MISSING_GENERATED_FILE_BINDING')
    bound_sources = set()
    for row in generated_files:
        path = _absolute_path(row.get('path'), [original])
        _require(path not in bound_sources and route.sha(path) == row.get('sha256') and path in mapped,
                 'GENERATED_FILE_BINDING_DRIFT')
        bound_sources.add(path)
    _require(all(p in bound_sources for p in (generation_manifest, graph, package, sysml)),
             'GENERATED_BINDING_INCOMPLETE')
    operations = scope.get('operations', [])
    _require(isinstance(operations, list), 'MISSING_OPERATIONS')
    used_tokens = set()
    front = route.read(_required(files, 'frontend/frontend-process.json'))
    _process(front, operations, 60, [OMC, base/'frontend/frontend.mos'], base/'frontend', used_tokens)
    proof = route.read(_required(files, 'frontend/result.json'))
    _require(proof.get('status') == 'PASS_FRONTEND_ONLY'
             and proof.get('script_sha256') == route.sha(_required(files, 'frontend/frontend.mos')),
             'FRONTEND_NOT_BOUND_PASS')
    reuse = copied_binding.get('frontend_reuse', {})
    _require(Path(reuse.get('proof_path', '')).resolve() == (base/'frontend/result.json').resolve()
             and reuse.get('proof_sha256') == route.sha(files['frontend/result.json'])
             and reuse.get('model_sha256') == route.sha(package), 'FRONTEND_REUSE_DRIFT')
    input_rows = proof.get('input_readset')
    _require(isinstance(input_rows, list) and bool(input_rows) and input_rows == reuse.get('inputs')
             and route.read(_required(files, 'frontend/inputs-before.json')) == input_rows,
             'FRONTEND_READSET_MISSING')
    dependencies = route.physical_dependencies(package)
    _require([Path(p).resolve() for p in proof.get('actual_source_paths', [])]
             == [Path(p).resolve() for p in dependencies], 'FRONTEND_ACTUAL_SOURCES_MISMATCH')
    _require(len(input_rows) == len(dependencies)
             and all(Path(row['path']).resolve() == Path(dep).resolve()
                     and row.get('sha256') == route.sha(dep) for row,dep in zip(input_rows,dependencies)),
             'FRONTEND_SOURCE_BYTES_DRIFT')
    parsed = route.read(_required(files, 'parse-round-01/parse.json'))
    _require(parsed.get('status') == 'PASS', 'SYSML_NOT_PASS')
    parse_process = route.read(_required(files, 'parse-round-01/parse-process.json'))
    import sys
    parse_command = [sys.executable, '-B', '-m', 'week12.scripts.s3_c_active_cluster',
                     'parse-child', '--source', str(sysml), '--report', str(base/'parse-round-01/parse.json')]
    _process(parse_process, operations, 30, parse_command, route.PROJECT, used_tokens)
    continued=bool(scope.get('continuation_claim'))
    if continued:
        route.probe_reuse(scope)
        old_base=Path(route.PROBE_BASE).parent.parent
        old_original=route.RUN/'generated/task_eea4728ef1c44680a5310262b562dee3'
        roots += [old_base,old_original]
        old_binding=route.read(old_original/'source-binding.json')
        old_manifest=old_original/'manifest.json';old_graph=old_original/'design.json'
        old_contract=route.RUN/'next-execution-proposal.json'
    all_attempts=scope.get('scene_attempts')
    _require(isinstance(all_attempts,list) and len(all_attempts)==(15 if continued else 14)
             and (not continued or all_attempts[2].get('status')=='FAIL'), 'FAILED_MODULE_NOT_PRESERVED')
    attempts=all_attempts[:2]+all_attempts[3:] if continued else all_attempts
    _require(isinstance(attempts, list) and len(attempts) == 14
             and [(r.get('label'), r.get('status'), r.get('round')) for r in attempts]
             == [(name+'-'+variant, 'PASS', 1) for name,variant in REQUIRED_SEQUENCE], 'LEDGER_SCENE_SEQUENCE_MISMATCH')
    claim_token = scope.get('execution_claim', {}).get('token')
    old_claim=scope.get('continuation_claim',{}).get('previous_claim',{})
    _require(bool(claim_token) and all(r.get('owner_token') == claim_token for r in attempts[2:] if continued)
             and all(r.get('owner_token')==(old_claim.get('token') if continued else claim_token) for r in attempts[:2])
             and (continued or all(r.get('owner_token')==claim_token for r in attempts)), 'SCENE_OWNER_MISMATCH')
    limits = {name:sum(route.scene_limits(name)) for name,variant in REQUIRED_SEQUENCE}
    for row, attempt, (name, variant) in zip(rows, attempts, REQUIRED_SEQUENCE):
        reused=continued and name==route.ORDER[0]
        local_files=files
        local_base=base
        local_binding=copied_binding
        local_manifest=generation_manifest;local_graph=graph;local_contract=route.PROPOSAL_PATH
        contract_sha=route.CONTRACT_SHA;auth_sha=route.AUTH_SHA
        if reused:
            local_base=old_base;local_binding=old_binding
            local_manifest=old_manifest;local_graph=old_graph;local_contract=old_contract
            contract_sha=route.BASE_CONTRACT_SHA;auth_sha=route.BASE_AUTH_SHA
            local_files={('round-01/'+name+'-'+variant+'/'+p.name):p
                         for p in (old_base/'round-01'/(name+'-'+variant)).iterdir() if p.is_file()}
            local_files['round-01/'+name+'-base/result.csv']=route.PROBE_BASE/'result.csv'
        prefix = 'round-01/' + name + '-' + variant
        directory = local_base/prefix
        actual = route.read(_required(local_files, prefix+'/scene-result.json'))
        _require(actual == row and actual.get('source_binding') == local_binding,
                 'ACTUAL_SCENE_SUMMARY_OR_SOURCE_MISMATCH')
        _require(Path(attempt.get('scene_result', '')).resolve() == (directory/'scene-result.json').resolve()
                 and attempt.get('scene_active_wall_s') == actual.get('scene_active_wall_s'), 'LEDGER_SCENE_ARTIFACT_MISMATCH')
        _require(_number(actual.get('scene_active_wall_s')) <= limits[name], 'SCENE_WALL_OVER_CAP')
        csv = _required(local_files, prefix+'/result.csv')
        _require(csv.stat().st_size > 0, 'EMPTY_ACTUAL_CSV')
        report_path = _required(local_files, prefix+'/independent-result.json')
        report = route.read(report_path)
        from week12.src.active_cluster.verify import SIGNED_WORK_ADDENDUM
        _require(report.get('numerical_addendum')==dict(id=SIGNED_WORK_ADDENDUM,proposal_sha256=contract_sha,authorization_sha256=auth_sha),'NUMERICAL_ADDENDUM_PROVENANCE')
        _passed_report(report, 'physical')
        if variant == 'refined':
            _passed_report(report, 'numeric')
            _require({'whole_curve_1percent_energy_shaft_energy','final_energy_1percent_energy_shaft_energy'}.issubset({c['code'] for c in report['numeric']['checks']}),'NUMERICAL_WHOLE_CURVE_GATE_MISSING')
        else:
            _require(report.get('numeric', {}).get('status') == 'NOT_RUN_BASE_VARIANT', 'BASE_NUMERIC_STATUS_MISMATCH')
        base_dir = local_base/'round-01'/(name+'-base') if variant == 'refined' else None
        commands = {'build': ([str(OMC), 'build.mos'], directory),
                    'simulation': ([str(directory/'model.exe'), '-s=dassl', '-r=result.csv'], directory),
                    'verification': (verification_command(directory, local_manifest, local_graph, name, base_dir,numerical_contract=local_contract), route.PROJECT)}
        phase_records={}
        compiler_cap,remaining_cap=route.scene_limits(name)
        for label, (command, cwd) in commands.items():
            for stream in ('stdout', 'stderr'):
                _required(local_files, prefix+'/'+label+'.'+stream+'.log')
            record = route.read(_required(local_files, prefix+'/'+label+'-process.json'))
            op = _process(record, operations, compiler_cap if label=='build' else remaining_cap, command, cwd, used_tokens)
            phase_records[label]=record
            _require(op.get('kind') == 'scene' and op.get('scene_label') == name+'-'+variant
                     and op.get('label') == label and any(p.get('token') == op['token'] for p in attempt.get('processes', [])),
                     'PROCESS_SCENE_LEDGER_MISMATCH')
            if label == 'verification':
                paths = [csv, report_path, local_manifest, local_graph,local_contract]
                if base_dir is not None:
                    paths.append(_required(local_files, 'round-01/'+name+'-base/result.csv'))
                _provenance(record, paths, roots, route.sha)
        _phase_time(phase_records,compiler_cap,remaining_cap)
        _required(local_files, prefix+'/build.mos')
        _required(local_files, prefix+'/model_init.xml')
        _required(local_files, prefix+'/model.exe')
    scene_operations = [r for r in operations if r.get('kind') == 'scene']
    failed_tokens={p.get('token') for p in all_attempts[2].get('processes',[])} if continued else set()
    _require(len(scene_operations) == (43 if continued else 42) and len(failed_tokens)==(1 if continued else 0)
             and all(r.get('token') in used_tokens or r.get('token') in failed_tokens for r in scene_operations),
             'EXTRA_OR_MISSING_SCENE_PROCESS')
    parents = scope.get('parents', [])
    workers = [r for r in parents if r.get('label') == 'car-worker:'+task['task_id']
               and r.get('pid') == task['worker_pid']]
    _require(len(workers) == 1, 'MISSING_OWNED_WORKER_PARENT')
    worker_token = workers[0]['token']
    for op in operations:
        if op.get('token') not in used_tokens:
            continue
        token = op.get('parent_token'); seen = set()
        accepted_worker=worker_token
        if continued and op.get('scene_label','').startswith(route.ORDER[0]+'-'):
            old_workers=[p for p in parents if p.get('label')=='car-worker:'+old_claim.get('task_id','')
                         and p.get('pid')==old_claim.get('pid')]
            _require(len(old_workers)==1,'OLD_PROBE_WORKER_MISSING');accepted_worker=old_workers[0]['token']
        while token != accepted_worker:
            _require(bool(token) and token not in seen, 'PROCESS_NOT_OWNED_BY_WORKER')
            seen.add(token)
            matches = [p for p in parents if p.get('token') == token]
            _require(len(matches) == 1, 'PROCESS_NOT_OWNED_BY_WORKER')
            token = matches[0].get('parent_token')


def _phase_time(records,compiler_cap,remaining_cap):
    _require(_number(records['build']['duration_s'])<=compiler_cap,'COMPILER_AGGREGATE_OVER_CAP')
    _require(sum(_number(records[k]['duration_s']) for k in ('simulation','verification'))<=remaining_cap,
             'SIMULATION_VERIFICATION_AGGREGATE_OVER_CAP')
    start=datetime.fromisoformat(records['simulation']['actual_started_utc'].replace('Z','+00:00'))
    end=datetime.fromisoformat(records['verification']['actual_ended_utc'].replace('Z','+00:00'))
    _require(0<=(end-start).total_seconds()<=remaining_cap,'SIMULATION_VERIFICATION_WALL_OVER_CAP')
