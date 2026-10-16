"""Approval-bound, finite vehicle C2 execution; importing does not run tools.

Reuse the original execution ledger, frozen dependency audit and independent
vehicle verifier.  Never manufacture a receipt or alter the accepted B file.
"""
from copy import deepcopy
import os
from pathlib import Path
import sys
import unittest
import uuid

from . import vehicle_amendment as amendment
from .execution import (Batch, read, save, sha, require_sources, dependency_audit,
                        build_script as original_build_script)
from .review import check_review_receipt
from ..composition.schema import digest

ROOT, REVIEW, OUT = amendment.ROOT, amendment.REVIEW, amendment.OUT
CASE_ORDER = tuple(amendment.CASE_ORDER)
EXPECTED_BUDGET = {
    'scene_attempts_per_complete_round':14, 'maximum_rounds':3,
    'maximum_scene_attempts':42, 'new_local_execution_checks_limit':24,
    'sysml_parse_limit':3, 'identity_probe_limit':0, 'product_online_calls':0,
    'execution_total_wall_s':3600, 'build_timeout_s':180,
    'simulation_timeout_s':180, 'postprocess_timeout_s':60,
    'new_local_timeout_s':10, 'sysml_parse_timeout_s':30,
}


def validate_budget(contract):
    budget = contract['budget_proposal']
    if any(type(budget.get(k)) is not int or budget[k] != v
           for k, v in EXPECTED_BUDGET.items()):
        raise ValueError('UNAPPROVED_EXECUTION_BUDGET')
    if tuple(contract['case_order']) != CASE_ORDER or len(CASE_ORDER) != 7:
        raise ValueError('EXACT_SEVEN_VEHICLE_CASES_REQUIRED')
    return deepcopy(budget)


def _manifest_paths(manifest):
    if not isinstance(manifest, list) or not manifest:
        raise ValueError('IMPLEMENTATION_MANIFEST_REQUIRED')
    paths = set()
    for row in manifest:
        path = Path(row['path'])
        if not path.is_absolute(): raise ValueError('ABSOLUTE_SOURCE_PATH_REQUIRED')
        path = path.resolve(strict=True)
        path.relative_to(ROOT.resolve())
        if path.name == '.env' or path.suffix not in ('.py', '.mo', '.json'):
            raise ValueError('INVALID_IMPLEMENTATION_SOURCE')
        if str(path) in paths: raise ValueError('DUPLICATE_IMPLEMENTATION_PATH')
        if not isinstance(row['sha256'], str) or len(row['sha256']) != 64:
            raise ValueError('SOURCE_SHA256_REQUIRED')
        paths.add(str(path))
    return paths


def guard_sources(packet, manifest):
    _manifest_paths(manifest)
    require_sources(manifest)
    for row in packet['fixed_inputs']:
        path = (ROOT / row['path']).resolve(strict=True)
        path.relative_to(ROOT.resolve())
        if path.name == '.env': raise ValueError('INVALID_FIXED_INPUT')
        if sha(path) != row['sha256']: raise ValueError('FIXED_INPUT_DRIFT')
    if sha(amendment.CANDIDATE) != amendment.C2_SHA:
        raise ValueError('ACCEPTED_CONTACT_DRIFT')


def initialize(receipt_path):
    """Read an explicit existing external user receipt; no implicit approval."""
    path = Path(receipt_path)
    if not path.is_absolute() or path.name == '.env':
        raise ValueError('EXPLICIT_ABSOLUTE_RECEIPT_PATH_REQUIRED')
    path = path.resolve(strict=True)
    packet, bundle, contract = amendment.inputs()
    receipt = read(path)
    check_review_receipt(receipt, packet)
    budget = validate_budget(contract)
    guard_sources(packet, packet['implementation_manifest'])
    OUT.mkdir(exist_ok=False)
    save(OUT/'authorization.json', receipt)
    save(OUT/'authorization-source.json', {'path':str(path), 'sha256':sha(path),
         'receipt_not_created_by_runner':True})
    save(OUT/'approved-packet.json', packet)
    save(OUT/'proposal.json', bundle)
    save(OUT/'verification-contract.json', contract)
    save(OUT/'policy.json', {'budget':budget, 'scope':packet['scope'],
         'proposal_digest':packet['bundle_digest'],
         'specification_digest':packet['specification_digest'],
         'no_prior_budget_carry_over':True, 'first_failure_stops':True,
         'execution_time_accounting':'actual child-process wall time including failures',
         'repair_boundary':'diagnosed implementation only; same specification and complete round budget',
         'stop':['physical/task/numerical hard failure', 'fixed input or dependency drift',
                 'budget exhausted', 'unresolved process'], 'success_closes_unused_budget':True})
    save(OUT/'initial-state.json', {'status':'APPROVED_NOT_RUN', 'scene_attempts':0,
         'local_checks':0, 'sysml':0, 'identity_probes':0, 'online':0})
    return receipt


def approved_inputs():
    if (OUT/'final-result.json').exists(): raise ValueError('CLOSED_BATCH_CANNOT_RUN')
    packet, bundle, contract = amendment.inputs()
    check_review_receipt(read(OUT/'authorization.json'), packet)
    if (read(OUT/'approved-packet.json') != packet or
            read(OUT/'proposal.json') != bundle or
            read(OUT/'verification-contract.json') != contract):
        raise ValueError('EXECUTION_COPY_OR_APPROVAL_DRIFT')
    budget = validate_budget(contract)
    policy = read(OUT/'policy.json')
    if (policy['budget'] != budget or policy['scope'] != packet['scope'] or
            policy['proposal_digest'] != packet['bundle_digest'] or
            policy['specification_digest'] != packet['specification_digest']):
        raise ValueError('EXECUTION_POLICY_DRIFT')
    return packet, bundle, contract


def local_targets():
    def flatten(suite):
        for item in suite:
            if isinstance(item, unittest.TestSuite): yield from flatten(item)
            else: yield item.id()
    names = []
    for module in ('week12.tests.test_c_vehicle_amendment',
                   'week12.tests.test_c_vehicle_amendment_execution'):
        targets = list(flatten(unittest.defaultTestLoader.loadTestsFromName(module)))
        if len(targets) != 4 or any('_FailedTest' in name for name in targets):
            raise ValueError('FOUR_EXPLICIT_TESTS_REQUIRED: '+module)
        names.extend(targets)
    if len(names) != 8 or len(set(names)) != 8:
        raise ValueError('EIGHT_UNIQUE_PREFLIGHT_TESTS_REQUIRED')
    return names


def round_manifest(number, packet):
    if type(number) is not int or not 1 <= number <= 3:
        raise ValueError('ROUND_LIMIT')
    manifest = packet['implementation_manifest']
    if number > 1:
        previous = OUT / f'round-{number-1:02d}'
        result = read(previous/'batch-result.json')
        repair = read(previous/'diagnosis.json')
        if result['status'] != 'STOPPED': raise ValueError('NO_RETRY_AFTER_SUCCESS')
        # A failed report can mean a verifier/interface implementation error;
        # the explicit, evidence-backed diagnosis is the failure classification.
        # Actual physical/task/numerical failures must never be labelled implementation.
        if (repair.get('classification') != 'implementation' or
                not isinstance(repair.get('reason'), str) or not repair['reason'].strip() or
                repair.get('proposal_digest') != packet['bundle_digest'] or
                repair.get('specification_digest') != packet['specification_digest'] or
                result.get('proposal_digest') != packet['bundle_digest'] or
                result.get('specification_digest') != packet['specification_digest']):
            raise ValueError('BOUND_IMPLEMENTATION_DIAGNOSIS_REQUIRED')
        manifest = repair['rebound_implementation_manifest']
        if _manifest_paths(manifest) != _manifest_paths(packet['implementation_manifest']):
            raise ValueError('INCOMPLETE_REPAIR_MANIFEST')
        old = read(previous/'source-manifest.json')
        old_map = {str(Path(r['path']).resolve()):r['sha256'] for r in old}
        new_map = {str(Path(r['path']).resolve()):r['sha256'] for r in manifest}
        if old_map == new_map: raise ValueError('UNCHANGED_SOURCE_BLIND_RETRY')
    guard_sources(packet, manifest)
    return manifest


def require_complete_round(batch):
    used, budget = batch.usage(), batch.budget
    if used['unresolved_processes']: raise ValueError('UNRESOLVED_PROCESS_BLOCKS_RESUME')
    if (used['scene']+14 > budget['maximum_scene_attempts'] or
            used['local']+8 > budget['new_local_execution_checks_limit'] or
            used['sysml']+1 > budget['sysml_parse_limit'] or
            used['process_seconds'] >= budget['execution_total_wall_s']):
        raise ValueError('NO_COMPLETE_AFFECTED_ROUND_BUDGET')


def snapshot_inputs(directory, packet, bundle, contract, manifest):
    save(directory/'proposal.json', bundle)
    save(directory/'verification-contract.json', contract)
    save(directory/'confirmation-identity.json', packet)
    save(directory/'source-manifest.json', manifest)
    for rows, name, relative in ((manifest, 'source-snapshot', False),
                                 (packet['fixed_inputs'], 'input-snapshot', True)):
        copied = {}
        for row in rows:
            source = ROOT/row['path'] if relative else Path(row['path'])
            canonical = source.resolve().relative_to(ROOT.resolve())
            key = str(canonical).casefold()
            if key in copied:
                if copied[key] != row['sha256']:
                    raise ValueError('CONFLICTING_SNAPSHOT_ALIASES')
                continue
            target = directory/name/canonical
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open('xb') as stream: stream.write(source.read_bytes())
            copied[key] = row['sha256']
    save(directory/'fixed-input-manifest.json', packet['fixed_inputs'])


def build_script(model_class, stop, variable_filter, refined):
    script = original_build_script(model_class, stop, variable_filter, refined)
    marker = 'ok := loadFile("V11PhysicalB1.mo");'
    if script.count(marker) != 1: raise ValueError('B_LOAD_MARKER_REQUIRED')
    insertion = ('c2 := loadFile("V11ContactC2.mo");\n'
                 'print("__C_C2_LOAD__="+String(c2)+"\\n");\n'
                 'print(getErrorString()+"\\n");\nif not c2 then exit(1); end if;\n')
    return script.replace(marker, insertion+marker, 1)


def execute_scene(batch, directory, bundle, contract, case_id, refined,
                  number, manifest, packet, base=None):
    from ..component_physics.runner import OMC, MSL, build_observations
    from ..component_physics.event_codegen import audit_source
    from ..toolchain import _resolve_generated
    from .contact_repair_execution import ownership_audit
    guard_sources(packet, manifest)
    case = next(c for c in bundle['physical']['cases'] if c['id'] == case_id)
    name = case_id + ('-refined' if refined else '-base')
    reservation = batch.reserve('scene', name, number)
    folder = directory/name
    folder.mkdir(exist_ok=False)
    result = {'case':case_id, 'variant':'refined' if refined else 'base', 'status':'FAIL',
              'reservation':reservation, 'compile':'NOT_RUN', 'simulation':'NOT_RUN',
              'physical':'UNVERIFIED', 'numeric':'UNVERIFIED'}
    try:
        generated = amendment.generate(bundle, case_id)
        with (folder/'V11ContactC2.mo').open('xb') as stream:
            stream.write(amendment.CANDIDATE.read_bytes())
        for filename, source in (('V11PhysicalB1.mo', amendment.isolated_b()),
                                 ('V11CGenerated.mo', generated['modelica'])):
            with (folder/filename).open('x', encoding='utf-8', newline='\n') as stream:
                stream.write(source)
        binding = bundle['physical']['contact_implementation']
        if (sha(folder/'V11ContactC2.mo') != amendment.C2_SHA or
                sha(amendment.B_SOURCE) != binding['original_B_sha256'] or
                sha(folder/'V11PhysicalB1.mo') != binding['runtime_B_copy_sha256'] or
                'model PolylineWheelContact = V11ContactC2.PolylineWheelContact;' not in
                (folder/'V11PhysicalB1.mo').read_text(encoding='utf-8')):
            raise ValueError('ACTUAL_CONTACT_BINDING_DRIFT')
        save(folder/'trace.json', generated['trace'])
        save(folder/'input-identity.json', {
            'specification_digest':packet['specification_digest'],
            'proposal_digest':packet['bundle_digest'], 'implementation_digest':digest(manifest),
            'design_digest':bundle['design']['design_digest'],
            'physical_digest':bundle['physical']['physical_digest'],
            'original_B_sha256':sha(amendment.B_SOURCE),
            'runtime_B_copy_sha256':sha(folder/'V11PhysicalB1.mo'),
            'C2_sha256':sha(folder/'V11ContactC2.mo'),
            'modelica_sha256':sha(folder/'V11CGenerated.mo'),
            'original_B_acceptance_not_rerun':True})
        with (folder/'build.mos').open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(build_script(generated['model_class'], case['stop_time_s'],
                                      generated['variable_filter'], refined))
        build = batch.run([str(OMC), 'build.mos'], folder,
                          batch.budget['build_timeout_s'], folder, 'build')
        result.update(build_observations(build['stdout']))
        if build['returncode'] != 0 or build['timed_out']: raise RuntimeError('BUILD_TOOL_FAILURE')
        if result['model_check'] != 'PASS': raise RuntimeError('MODEL_CHECK_FAILED')
        if result['observed_msl_version'] != '4.0.0' or Path(result['observed_msl_source']).resolve() != MSL.resolve():
            raise RuntimeError('MSL_IDENTITY_DRIFT')
        executable = _resolve_generated(folder, result['reported_executable'])
        if executable is None: raise RuntimeError('NO_GENERATED_EXECUTABLE')
        result['compile'] = 'PASS'
        event = audit_source((folder/'model_05evt.c').read_text(encoding='utf-8'))
        points = case['terrain_points']
        if all(point[1] == points[0][1] for point in points):
            owner = {'status':'NOT_APPLICABLE_NO_CONVEX_CORNER', 'scope':'flat vehicle terrain'}
        else:
            owner = ownership_audit(folder, case_id)
        save(folder/'event-codegen.json', event)
        save(folder/'owner-audit.json', owner)
        result['event_codegen'], result['owner_audit'] = event['status'], owner['status']
        if event['status'] != 'PASS' or owner['status'] not in ('PASS', 'NOT_APPLICABLE_NO_CONVEX_CORNER'):
            raise RuntimeError('GENERATED_EVENT_OR_SHARED_OWNER_UNVERIFIED')
        result['simulation'] = 'LAUNCH_REQUESTED'
        step = contract['numerical']['refined' if refined else 'base']['max_step_s']
        save(folder/'simulation-launch.json', {'executable':str(executable), 'max_step_s':step,
             'specification_digest':packet['specification_digest']})
        proc = batch.run([str(executable), '-s=dassl', f'-maxStepSize={step}', '-r=result.csv'],
                         folder, batch.budget['simulation_timeout_s'], folder, 'simulation')
        result['simulation'] = 'COMPLETED' if proc['returncode'] == 0 and not proc['timed_out'] else 'FAILED_OR_TIMED_OUT'
        if result['simulation'] != 'COMPLETED': raise RuntimeError('SIMULATION_FAILURE_REQUIRES_DIAGNOSIS')
        command = [sys.executable, '-B', '-m', 'week12.scripts.s3_c_verify',
                   '--proposal', str(directory/'proposal.json'),
                   '--contract', str(directory/'verification-contract.json'), '--case', case_id,
                   '--csv', str(folder/'result.csv')]
        if base is not None: command += ['--base', str(base/'result.csv')]
        proc = batch.run(command, ROOT.parent, batch.budget['postprocess_timeout_s'], folder, 'verification')
        for key, filename in (('physical','physical.json'), ('numeric','numerical.json')):
            if (folder/filename).exists(): result[key] = read(folder/filename)['status']
        if proc['returncode'] != 0 or proc['timed_out']: raise RuntimeError('PHYSICAL_NUMERIC_OR_POSTPROCESS_FAILURE')
        if result['physical'] != 'PASS' or (refined and result['numeric'] != 'PASS'):
            raise RuntimeError('INCOMPLETE_PHYSICAL_OR_NUMERIC_EVIDENCE')
        guard_sources(packet, manifest)
        result['status'] = 'PASS'
    except Exception as exc:
        result['error'] = type(exc).__name__ + ': ' + str(exc)
    finally:
        save(folder/'result.json', result)
    print(name, result['status'], result.get('error',''), flush=True)
    return result, folder


def run_round(number):
    packet, bundle, contract = approved_inputs()
    manifest = round_manifest(number, packet)
    batch = Batch(OUT, packet['specification_digest'])
    require_complete_round(batch)
    directory = OUT / f'round-{number:02d}'
    if directory.exists(): raise ValueError('ROUND_DIRECTORY_ALREADY_EXISTS')
    lock = OUT/'.execution.lock'
    token = f'{os.getpid()}:{uuid.uuid4().hex}'
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try: os.write(fd, token.encode('ascii'))
    finally: os.close(fd)
    owns = False
    report = {'round':number, 'status':'PENDING', 'local':[], 'scenes':[],
              'sysml':'NOT_RUN', 'online':0, 'identity_probes':0,
              'scope':packet['scope'], 'specification_digest':packet['specification_digest'],
              'proposal_digest':packet['bundle_digest']}
    try:
        directory.mkdir(exist_ok=False)
        owns = True
        guard_sources(packet, manifest)
        snapshot_inputs(directory, packet, bundle, contract, manifest)
        dependency_audit(directory)
        for index, target in enumerate(local_targets(), 1):
            guard_sources(packet, manifest)
            batch.reserve('local', target, number)
            proc = batch.run([sys.executable, '-B', '-m', 'unittest', target], ROOT.parent,
                             batch.budget['new_local_timeout_s'], directory, f'local-{index:02d}')
            status = 'PASS' if proc['returncode'] == 0 and not proc['timed_out'] else 'FAIL'
            report['local'].append({'test':target, 'status':status})
            if status != 'PASS': raise RuntimeError('FIRST_LOCAL_FAILURE_REQUIRES_DIAGNOSIS')
        guard_sources(packet, manifest)
        generated = amendment.generate(bundle, CASE_ORDER[0])
        with (directory/'vehicle.sysml').open('x', encoding='utf-8', newline='\n') as stream:
            stream.write(generated['sysml'])
        save(directory/'sysml-trace.json', generated['trace'])
        batch.reserve('sysml', 'single_vehicle_source', number)
        proc = batch.run([sys.executable, '-B', '-m', 'week12.scripts.s3_c_execute',
                          '--parse-sysml', str(directory/'vehicle.sysml'),
                          '--parse-output', str(directory/'sysml-result.json')], ROOT.parent,
                         batch.budget['sysml_parse_timeout_s'], directory, 'sysml')
        if proc['returncode'] != 0 or proc['timed_out']: raise RuntimeError('SYSML_PARSER_FAILURE')
        parsed = read(directory/'sysml-result.json')
        report['sysml'] = parsed.get('status', 'UNVERIFIED')
        if report['sysml'].upper() != 'PASS': raise RuntimeError('SYSML_SYNTAX_FAILED')
        if parsed.get('tool_version') != '0.34.1': raise RuntimeError('SYSML_PARSER_IDENTITY_DRIFT')
        for case_id in CASE_ORDER:
            base = None
            for refined in (False, True):
                row, folder = execute_scene(batch, directory, bundle, contract, case_id,
                                            refined, number, manifest, packet, base)
                report['scenes'].append(row)
                if row['status'] != 'PASS': raise RuntimeError('FIRST_SCENE_FAILURE_REQUIRES_DIAGNOSIS')
                if not refined: base = folder
        guard_sources(packet, manifest)
        report['status'] = 'PASS_APPROVED_C2_SINGLE_VEHICLE_NOT_ALL_C'
    except BaseException as exc:
        report.update(status='STOPPED', error=type(exc).__name__ + ': ' + str(exc))
    finally:
        try:
            report['usage'] = batch.usage()
            report['not_run'] = [case+('-refined' if refined else '-base')
                for case in CASE_ORDER for refined in (False, True)
                if not any(row['case'] == case and row['variant'] == ('refined' if refined else 'base')
                           for row in report['scenes'])]
            if owns:
                try:
                    guard_sources(packet, manifest)
                    report['source_guard_end'] = 'PASS'
                except Exception as exc:
                    report.update(status='STOPPED', source_guard_end='FAIL',
                                  source_guard_error=type(exc).__name__ + ': ' + str(exc))
                report['current_source_hashes'] = [{'path':row['path'],
                    'sha256':sha(row['path']) if Path(row['path']).is_file() else None} for row in manifest]
                report['accepted_C2_sha256_end'] = sha(amendment.CANDIDATE)
                save(directory/'batch-result.json', report)
        finally:
            if lock.is_file() and lock.read_text(encoding='ascii') == token: lock.unlink()
    print(report['status'], report.get('error',''), report['usage'], flush=True)
    return report
