"""Receipt-bound same-physics refined diagnostic; preparation starts no tools.

Reuse the frozen comparator and scene runner through private globals. The
vehicle's original physical identity remains in generated Modelica comments.
"""
from copy import deepcopy
import ast
import math
import os
from pathlib import Path
import sys
from types import FunctionType
import uuid

from . import dimension_execution as execution
from . import failure_diagnostic as comparator
from . import vehicle_amendment_execution as previous
from .execution import Batch, read, save, sha, require_sources, dependency_audit
from .review import check_review_receipt
from ..composition.schema import digest

ROOT = execution.ROOT
REVIEW = ROOT/'results/s3-c-dimension-failure-diagnostic-review-20261006-01'
OUT = ROOT/'results/s3-c-dimension-failure-diagnostic-execution-20261006-01'
OLD = execution.OUT
OLD_ROUND = OLD/'round-01'
CASE = 'fixed_nose_three_up'
BASE = OLD_ROUND/(CASE+'-base')
PHYSICAL_SPEC = 'de9bbc13ddb6328242bc4b8233ad42382ba6ef99cf493dd8c820c6162e9d03ee'
BUNDLE_DIGEST = '18da81824f05d0e7e97ca7f50d4264545e6037e7941e7c179b16254edca68bcf'
PHYSICAL_DIGEST = '16ee7a69769eeebaaa40438ff25a24983c32082f9b7d2ac45f770a58d0769fa2'
OLD_MANIFEST_SHA = '4e1dd02c2ebc2c107cc63814538601628d839561b2a8528505193a22270a62a2'
COMPARATOR_PATH = ROOT/'src/integration/failure_diagnostic.py'
COMPARATOR_SHA = '60cdb379a8a23b182b8481865fe5b0ffb955be1b3d349c54a2cb8fb1e1bb942f'
SCOPE = 'v11_s3_c_same_dimension_physical_three_up_failure_diagnostic'
NEW_PATHS = ('src/integration/dimension_failure_diagnostic.py',
             'scripts/s3_c_dimension_failure_diagnostic.py',
             'tests/test_c_dimension_failure_diagnostic.py')
BUDGET = deepcopy(comparator.BUDGET)
NUMERICS = deepcopy(comparator.NUMERICS)
FINAL_CLASSIFICATIONS = comparator.FINAL_CLASSIFICATIONS
EXPECTED_FAILED = frozenset(('no_stagnation_during_traversal',
    'rear_all_riser_crossings_with_physics', 'front_all_riser_crossings_with_physics',
    'complete_geometry_beyond_exit', 'continued_exit_cruise'))
REPAIR_CODES = frozenset(('NEW_LOCAL_CHECK_FAILURE', 'NEW_GENERATED_SYNTAX'))
compare_failure = comparator.compare_failure
_completed_verification = comparator._completed_verification


class ImplementationFailure(RuntimeError):
    """Narrow candidate; retry still requires concrete evidence and changed code."""


def _bound_files(paths):
    return [{'path':str(Path(p).resolve().relative_to(ROOT.resolve())),
             'sha256':sha(p)} for p in paths]


def _fixed_sources(packet):
    if sha(OLD_ROUND/'source-manifest.json') != OLD_MANIFEST_SHA:
        raise ValueError('FROZEN_DIMENSION_MANIFEST_DRIFT')
    rows = read(OLD_ROUND/'source-manifest.json')
    if len(rows) != 26 or len(previous._manifest_paths(rows)) != 26:
        raise ValueError('EXACT_FROZEN_26_DIMENSION_SOURCES_REQUIRED')
    execution.guard_sources(packet, rows)
    if sha(COMPARATOR_PATH) != COMPARATOR_SHA:
        raise ValueError('FROZEN_FAILURE_COMPARATOR_DRIFT')
    return rows


def _failed_checks(report):
    checks = report.get('checks')
    if (not isinstance(checks, list) or len(checks) != 68 or
            any(type(c.get('pass')) is not bool for c in checks) or
            len({c['name'] for c in checks}) != 68 or
            report.get('status') not in ('PASS', 'FAIL') or
            (report['status'] == 'PASS') != all(c['pass'] for c in checks) or
            report.get('diagnostics') or report.get('issues') or
            report['metrics']['case_id'] != CASE):
        raise ValueError('COMPLETE_68_CHECK_PHYSICAL_REPORT_REQUIRED')
    return {c['name'] for c in checks if not c['pass']}


def _check_reuse(packet, bundle, contract):
    if (packet['specification_digest'] != PHYSICAL_SPEC or
            packet['bundle_digest'] != BUNDLE_DIGEST or digest(bundle) != BUNDLE_DIGEST or
            packet['physical_digest'] != PHYSICAL_DIGEST or
            bundle['physical']['physical_digest'] != PHYSICAL_DIGEST or
            packet['verification_digest'] != digest(contract)):
        raise ValueError('EXACT_UNCHANGED_DIMENSION_PHYSICAL_BINDING_REQUIRED')
    final, prior = read(OLD/'final-result.json'), read(OLD_ROUND/'batch-result.json')
    if ({k:v for k,v in final.items() if k not in ('unused_budget_disposition',
            'automatic_additional_scene_permitted')} != prior or final['status'] != 'STOPPED' or
            final['failure_code'] != 'TERMINAL' or final['retry_eligible'] is not False or
            final['unused_budget_disposition'] != 'RETIRED' or
            final['automatic_additional_scene_permitted'] is not False or
            final['specification_digest'] != PHYSICAL_SPEC or
            final['physical_digest'] != PHYSICAL_DIGEST or
            final['proposal_digest'] != BUNDLE_DIGEST or
            len(final['local']) != 8 or any(r['status'] != 'PASS' for r in final['local']) or
            final['sysml'] != 'PASS' or final['usage']['scene'] != 3 or
            final['usage']['local'] != 8 or final['usage']['sysml'] != 1 or
            final['usage']['probe'] != 0 or final['usage']['unresolved_processes'] != 0 or
            final['online'] != 0 or final['identity_probes'] != 0):
        raise ValueError('CLOSED_DIMENSION_PREFLIGHT_EVIDENCE_REQUIRED')
    for row in final['scenes'][:2]:
        if row['case'] != 'fixed_nose_flat_brake' or row['status'] != 'PASS':
            raise ValueError('VALID_FLAT_EVIDENCE_REQUIRED')
    parsed = read(OLD_ROUND/'sysml-result.json')
    if parsed['status'] != 'PASS' or parsed.get('issues'):
        raise ValueError('VALID_REUSED_SYSML_EVIDENCE_REQUIRED')
    report, result = read(BASE/'physical.json'), read(BASE/'result.json')
    if (_failed_checks(report) != EXPECTED_FAILED or
            report['metrics']['csv_sha256'] != sha(BASE/'result.csv') or
            result['compile'] != 'PASS' or result['simulation'] != 'COMPLETED' or
            result['physical'] != 'FAIL' or result['event_codegen'] != 'PASS' or
            result['owner_audit'] != 'PASS'):
        raise ValueError('EXACT_FIVE_KNOWN_BASE_TASK_FAILURES_REQUIRED')
    case = next(c for c in bundle['physical']['cases'] if c['id'] == CASE)
    if case['stop_time_s'] != 35 or contract['numerical']['refined'] != NUMERICS:
        raise ValueError('EXACT_FROZEN_35S_REFINED_NUMERICS_REQUIRED')
    return report


def validate_budget(identity):
    if (identity.get('scope') != SCOPE or identity.get('budget') != BUDGET or
            any(type(v) is not int for v in identity['budget'].values()) or
            identity.get('case_order') != [CASE] or identity.get('variant') != 'refined' or
            identity.get('stop_time_s') != 35 or identity.get('numerics') != NUMERICS):
        raise ValueError('EXACT_SINGLE_REFINED_FINITE_DIAGNOSTIC_REQUIRED')
    return deepcopy(BUDGET)


def guard_sources(identity, packet, manifest, *, allow_new_drift=False):
    fixed = _fixed_sources(packet)
    immutable = [*fixed, {'path':str(COMPARATOR_PATH.resolve()), 'sha256':COMPARATOR_SHA}]
    if identity['fixed_implementation_manifest'] != immutable:
        raise ValueError('EXACT_26_PLUS_COMPARATOR_FIXED_BINDING_REQUIRED')
    expected_paths = previous._manifest_paths(immutable) | {str((ROOT/p).resolve()) for p in NEW_PATHS}
    if (len(expected_paths) != 30 or previous._manifest_paths(manifest) != expected_paths or
            previous._manifest_paths(identity['implementation_manifest']) != expected_paths):
        raise ValueError('COMPLETE_30_SOURCE_MANIFEST_REQUIRED')
    actual = execution._maps(manifest)
    if any(actual[str(Path(r['path']).resolve())] != r['sha256'] for r in immutable):
        raise ValueError('FROZEN_IMPLEMENTATION_CANNOT_BE_REPAIRED')
    # The original dimension guard receives exactly its original26, never30.
    execution.guard_sources(packet, fixed)
    require_sources(immutable if allow_new_drift else manifest)
    if identity['fixed_inputs'] != packet['fixed_inputs'] or identity['readonly_manifest'] != packet['readonly_manifest']:
        raise ValueError('FIXED_INPUT_OR_READONLY_MANIFEST_DRIFT')
    for row in [*identity['reused_evidence'], *identity['review_evidence']]:
        if sha(ROOT/row['path']) != row['sha256']:
            raise ValueError('REUSED_OR_REVIEW_EVIDENCE_DRIFT')


def _generated(packet, bundle, contract):
    result = execution.revision.generate(bundle, contract, CASE, PHYSICAL_SPEC)
    if result['modelica'].encode('utf-8') != (BASE/'V11CGenerated.mo').read_bytes():
        raise ValueError('SAME_BASE_MODEL_SOURCE_BYTES_REQUIRED')
    base_script = previous.build_script(result['model_class'], 35., result['variable_filter'], False)
    refined_script = previous.build_script(result['model_class'], 35., result['variable_filter'], True)
    if base_script.encode('utf-8') != (BASE/'build.mos').read_bytes():
        raise ValueError('SAME_BASE_BUILD_SOURCE_REQUIRED')
    if refined_script.encode('utf-8') != (execution.REVIEW/(CASE+'-refined')/'build-preview.mos').read_bytes():
        raise ValueError('ORIGINAL_REFINED_BUILD_PREVIEW_REQUIRED')
    for filename, payload in (('V11PhysicalB1.mo', execution.vehicle.isolated_b().encode('utf-8')),
                              ('V11ContactC2.mo', execution.vehicle.CANDIDATE.read_bytes())):
        if payload != (BASE/filename).read_bytes():
            raise ValueError('UNCHANGED_BASE_B_AND_C2_REQUIRED')
    return result, refined_script


def prepare():
    """Exclusive source-only archive; caller supplies no approval and starts no tools."""
    packet, bundle, contract = execution.inputs()
    fixed = _fixed_sources(packet)
    _check_reuse(packet, bundle, contract)
    for name in NEW_PATHS: ast.parse((ROOT/name).read_text(encoding='utf-8'))
    tree = ast.parse((ROOT/NEW_PATHS[2]).read_text(encoding='utf-8'))
    tests = [n.name for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name.startswith('test_')]
    if len(tests) != 4 or len(set(tests)) != 4:
        raise ValueError('EXACT_FOUR_TEST_DEFINITIONS_REQUIRED')
    generated, build = _generated(packet, bundle, contract)
    paths = [OLD/name for name in ('final-result.json', 'authorization.json', 'authorization-source.json',
        'approved-packet.json', 'policy.json', 'proposal.json', 'verification-contract.json')]
    paths += [OLD_ROUND/name for name in ('batch-result.json', 'source-manifest.json', 'sysml-result.json',
        'dependency-audit.json', 'vehicle.sysml', 'readonly-manifest.json', 'fixed-input-manifest.json')]
    paths += [BASE/name for name in ('result.csv', 'physical.json', 'result.json', 'input-identity.json',
        'V11CGenerated.mo', 'V11PhysicalB1.mo', 'V11ContactC2.mo', 'build.mos', 'simulation-launch.json',
        'event-codegen.json', 'owner-audit.json', 'trace.json')]
    paths += [OLD_ROUND/f'local-{i:02d}.{stream}.log' for i in range(1,9) for stream in ('stdout','stderr')]
    for folder_name in ('fixed_nose_flat_brake-base', 'fixed_nose_flat_brake-refined'):
        paths += [OLD_ROUND/folder_name/name for name in ('result.csv','physical.json','result.json')]
    paths += [OLD_ROUND/'fixed_nose_flat_brake-refined/numerical.json']
    immutable = [*fixed, {'path':str(COMPARATOR_PATH.resolve()), 'sha256':COMPARATOR_SHA}]
    identity = {'scope':SCOPE, 'physical_specification_digest':PHYSICAL_SPEC,
        'vehicle_bundle_digest':BUNDLE_DIGEST, 'physical_digest':PHYSICAL_DIGEST,
        'vehicle_verification_digest':digest(contract), 'case_order':[CASE], 'variant':'refined',
        'stop_time_s':35, 'numerics':deepcopy(NUMERICS), 'base_numerics':deepcopy(contract['numerical']['base']),
        'physical':deepcopy(contract['physical']), 'numerical_comparison':deepcopy(contract['numerical_comparison']),
        'budget':deepcopy(BUDGET), 'reused_evidence':_bound_files(paths),
        'review_evidence':_bound_files([REVIEW/'decision-basis.json']),
        'fixed_inputs':deepcopy(packet['fixed_inputs']), 'readonly_manifest':deepcopy(packet['readonly_manifest']),
        'fixed_implementation_manifest':immutable,
        'implementation_manifest':[*immutable, *[{'path':str((ROOT/n).resolve()),'sha256':sha(ROOT/n)} for n in NEW_PATHS]],
        'expected_base_failed_checks':sorted(EXPECTED_FAILED), 'no_prior_budget_carry_over':True,
        'old_closed_batch_never_reopened':True, 'complete_round_reserve_s':500,
        'postprocess_timeout_is_shared_between_verification_and_comparison':True,
        'physical_FAIL_remains_FAIL':True, 'vehicle_acceptance':'NOT_GRANTED',
        'completed_diagnostic_retires_unused_budget':True,
        'repair_boundary':'new three files only; explicit evidence-backed implementation diagnosis; changed sources; same physics and diagnostic; complete round budget',
        'generated_modelica_sha256':sha(BASE/'V11CGenerated.mo')}
    identity['specification_digest'] = digest(identity)
    validate_budget(identity)
    guard_sources(identity, packet, identity['implementation_manifest'])
    targets = [REVIEW/n for n in ('diagnostic-review-packet.json','vehicle-confirmation-identity.json',
        'proposal.json','verification-contract.json','confirmation-identity.json','source-manifest.json',
        'fixed-input-manifest.json','readonly-manifest.json','preparation.json','source-snapshot','input-snapshot',
        'readonly-snapshot',CASE+'-refined')]
    if any(p.exists() for p in targets):
        raise ValueError('SOURCE_PREPARATION_ALREADY_EXISTS_NO_OVERWRITE')
    REVIEW.mkdir(exist_ok=True)
    previous.snapshot_inputs(REVIEW, identity, bundle, contract, identity['implementation_manifest'])
    execution.snapshot_readonly(REVIEW, packet)
    save(REVIEW/'vehicle-confirmation-identity.json', packet)
    save(REVIEW/'diagnostic-review-packet.json', identity)
    folder = REVIEW/(CASE+'-refined'); folder.mkdir(exist_ok=False)
    for name, payload in (('V11CGenerated.mo',generated['modelica'].encode('utf-8')),
                           ('V11PhysicalB1.mo',execution.vehicle.isolated_b().encode('utf-8')),
                           ('V11ContactC2.mo',execution.vehicle.CANDIDATE.read_bytes()),
                           ('build-preview.mos',build.encode('utf-8'))):
        with (folder/name).open('xb') as stream: stream.write(payload)
    save(folder/'trace.json', generated['trace'])
    save(REVIEW/'preparation.json', {'status':'SOURCE_ONLY_PREPARED_NOT_AUTHORIZED', 'new_tests_run':0,
        'new_simulations':0, 'new_sysml':0, 'online':0, 'identity_probes':0,
        'reused_local_pass':8, 'reused_sysml_pass':1, 'C_complete':False})
    return identity


def inputs():
    identity = read(REVIEW/'diagnostic-review-packet.json')
    raw = dict(identity); checksum = raw.pop('specification_digest')
    if digest(raw) != checksum: raise ValueError('DIAGNOSTIC_IDENTITY_DRIFT')
    packet, bundle, contract = execution.inputs()
    if (read(REVIEW/'vehicle-confirmation-identity.json') != packet or
            read(REVIEW/'proposal.json') != bundle or read(REVIEW/'verification-contract.json') != contract or
            identity['physical_specification_digest'] != PHYSICAL_SPEC or
            identity['physical_digest'] != PHYSICAL_DIGEST or
            identity['vehicle_bundle_digest'] != digest(bundle) or
            identity['vehicle_verification_digest'] != digest(contract) or
            identity['physical'] != contract['physical'] or
            identity['numerical_comparison'] != contract['numerical_comparison'] or
            identity['numerics'] != contract['numerical']['refined'] or
            identity['base_numerics'] != contract['numerical']['base'] or
            identity['expected_base_failed_checks'] != sorted(EXPECTED_FAILED)):
        raise ValueError('UNCHANGED_VEHICLE_AND_VERIFICATION_REQUIRED')
    validate_budget(identity)
    guard_sources(identity, packet, identity['implementation_manifest'], allow_new_drift=True)
    _check_reuse(packet, bundle, contract)
    return identity, packet, bundle, contract


def initialize(receipt_path):
    path = Path(receipt_path)
    if not path.is_absolute() or path.name == '.env':
        raise ValueError('EXPLICIT_ABSOLUTE_EXTERNAL_RECEIPT_REQUIRED')
    path = path.resolve(strict=True)
    path.relative_to(path.anchor)
    if path.is_relative_to(REVIEW.resolve()) or path.is_relative_to(OUT.resolve()):
        raise ValueError('RECEIPT_MUST_BE_EXTERNAL_TO_DIAGNOSTIC_ARCHIVES')
    identity, packet, bundle, contract = inputs()
    receipt = read(path)
    check_review_receipt(receipt, identity)
    guard_sources(identity, packet, identity['implementation_manifest'])
    OUT.mkdir(exist_ok=False)
    save(OUT/'authorization.json', receipt)
    save(OUT/'authorization-source.json', {'path':str(path), 'sha256':sha(path),
         'receipt_not_created_by_runner':True})
    save(OUT/'approved-diagnostic.json', identity)
    save(OUT/'policy.json', {'budget':deepcopy(BUDGET), 'scope':SCOPE,
        'specification_digest':identity['specification_digest'], 'physical_specification_digest':PHYSICAL_SPEC,
        'physical_digest':PHYSICAL_DIGEST, 'old_batch_closed':True,
        'physical_FAIL_remains_FAIL':True, 'no_prior_budget_carry_over':True})
    return receipt


def approved_inputs():
    if (OUT/'final-result.json').exists(): raise ValueError('CLOSED_DIAGNOSTIC_CANNOT_RUN')
    identity, packet, bundle, contract = inputs()
    check_review_receipt(read(OUT/'authorization.json'), identity)
    policy = read(OUT/'policy.json')
    if (read(OUT/'approved-diagnostic.json') != identity or policy['budget'] != BUDGET or
            policy['scope'] != SCOPE or policy['specification_digest'] != identity['specification_digest'] or
            policy['physical_specification_digest'] != PHYSICAL_SPEC or policy['physical_digest'] != PHYSICAL_DIGEST or
            policy.get('old_batch_closed') is not True or policy.get('physical_FAIL_remains_FAIL') is not True or
            policy.get('no_prior_budget_carry_over') is not True):
        raise ValueError('APPROVAL_OR_POLICY_DRIFT')
    source = read(OUT/'authorization-source.json')
    path = Path(source['path'])
    if not path.is_absolute() or sha(path) != source['sha256']:
        raise ValueError('EXTERNAL_RECEIPT_DRIFT')
    check_review_receipt(read(path), identity)
    return identity, packet, bundle, contract


def validate_repair(identity, packet, old, result, diagnosis):
    if (result.get('status') != 'STOPPED' or result.get('retry_eligible') is not True or
            result.get('failure_code') not in REPAIR_CODES or
            diagnosis.get('classification') != 'implementation' or
            diagnosis.get('failure_code') != result['failure_code']):
        raise ValueError('EXPLICIT_NEW_ADAPTER_IMPLEMENTATION_FAILURE_REQUIRED')
    for key, value in (('diagnostic_digest',identity['specification_digest']),
                       ('physical_specification_digest',PHYSICAL_SPEC),('physical_digest',PHYSICAL_DIGEST)):
        if result.get(key) != value or diagnosis.get(key) != value:
            raise ValueError('SAME_PHYSICAL_DIAGNOSTIC_IDENTITY_REQUIRED')
    if (diagnosis.get('same_physical_task_verification') is not True or
            not isinstance(diagnosis.get('reason'),str) or not diagnosis['reason'].strip() or
            not isinstance(diagnosis.get('evidence_files'),list) or not diagnosis['evidence_files']):
        raise ValueError('CONCRETE_DIAGNOSIS_AND_HASHED_EVIDENCE_REQUIRED')
    for row in diagnosis['evidence_files']:
        path = (OUT/row['path']).resolve(strict=True)
        path.relative_to(OUT.resolve())
        if path.name == '.env' or sha(path) != row['sha256']:
            raise ValueError('DIAGNOSIS_EVIDENCE_DRIFT')
    manifest = diagnosis['rebound_implementation_manifest']
    if previous._manifest_paths(manifest) != previous._manifest_paths(old):
        raise ValueError('COMPLETE_REBOUND_MANIFEST_REQUIRED')
    before, after = execution._maps(old), execution._maps(manifest)
    changed = sorted(p for p in before if before[p] != after[p])
    if (not changed or not set(changed) <= {str((ROOT/p).resolve()) for p in NEW_PATHS} or
            diagnosis.get('changed_paths') != changed):
        raise ValueError('EXACT_NEW_THREE_CHANGED_SOURCES_REQUIRED_NO_BLIND_RETRY')
    guard_sources(identity, packet, manifest)
    return manifest


def round_manifest(number, identity, packet):
    if type(number) is not int or not 1 <= number <= BUDGET['maximum_rounds']:
        raise ValueError('ROUND_LIMIT')
    if number == 1:
        if any(OUT.glob('round-*')): raise ValueError('FIRST_ROUND_MUST_START_EMPTY')
        manifest = identity['implementation_manifest']
    else:
        directory = OUT/f'round-{number-1:02d}'
        manifest = validate_repair(identity, packet, read(directory/'source-manifest.json'),
            read(directory/'batch-result.json'), read(directory/'diagnosis.json'))
    guard_sources(identity, packet, manifest)
    return manifest


def require_complete_round(batch):
    used = batch.usage()
    if (batch.budget != BUDGET or used['unresolved_processes'] or used['sysml'] or used['probe'] or
            used['scene']+1 > BUDGET['maximum_scene_attempts'] or
            used['local']+4 > BUDGET['new_local_execution_checks_limit'] or
            used['process_seconds']+500 > BUDGET['execution_total_wall_s']):
        raise ValueError('COMPLETE_1_SCENE_4_LOCAL_500S_ROUND_REQUIRED')


def local_targets():
    tree = ast.parse((ROOT/NEW_PATHS[2]).read_text(encoding='utf-8'))
    names = [f'week12.tests.test_c_dimension_failure_diagnostic.{cls.name}.{fn.name}'
        for cls in tree.body if isinstance(cls,ast.ClassDef)
        for fn in cls.body if isinstance(fn,ast.FunctionDef) and fn.name.startswith('test_')]
    if len(names) != 4 or len(set(names)) != 4:
        raise ValueError('EXACT_FOUR_DIAGNOSTIC_CHECKS_REQUIRED')
    return names


def execute_scene(batch, directory, bundle, contract, number, manifest, identity, packet):
    original = previous.execute_scene
    namespace = dict(original.__globals__, amendment=execution._VehicleProxy(contract,packet),
        guard_sources=lambda vehicle_packet,rows:guard_sources(identity,vehicle_packet,rows))
    isolated = FunctionType(original.__code__,namespace,original.__name__,original.__defaults__,original.__closure__)
    return isolated(batch,directory,bundle,contract,CASE,True,number,manifest,packet,base=None)


def _check_refined(folder):
    report = read(folder/'physical.json')
    failed = _failed_checks(report)
    if (report['metrics']['csv_sha256'] != sha(folder/'result.csv') or not failed <= EXPECTED_FAILED or
            {c['name'] for c in report['checks']} != {c['name'] for c in read(BASE/'physical.json')['checks']}):
        raise ValueError('NEW_PHYSICAL_TASK_HARD_FAILURE_OR_INCOMPLETE_REFINED_EVIDENCE')
    return report


def _classify_scene_error(row, folder):
    if row.get('compile') == 'PASS' and row.get('simulation') == 'COMPLETED': return
    if row.get('error') == 'RuntimeError: BUILD_TOOL_FAILURE' and folder is not None:
        text = '\n'.join((folder/name).read_text(encoding='utf-8') for name in
            ('build.stdout.log','build.stderr.log') if (folder/name).is_file())
        original = execution._completed_build_evidence
        helper = FunctionType(original.__code__,dict(original.__globals__,OUT=OUT),original.__name__)
        if 'V11CGenerated.mo' in text and 'syntax error' in text.lower() and helper(folder):
            raise ImplementationFailure('NEW_GENERATED_SYNTAX')
    raise RuntimeError('UNKNOWN_TOOL_SIMULATION_OR_ENVIRONMENT_FAILURE')


def run_round(number):
    identity, packet, bundle, contract = approved_inputs()
    manifest = round_manifest(number,identity,packet)
    batch = Batch(OUT,identity['specification_digest'])
    require_complete_round(batch)
    directory = OUT/f'round-{number:02d}'
    if directory.exists(): raise ValueError('ROUND_DIRECTORY_ALREADY_EXISTS')
    lock, token = OUT/'.execution.lock',f'{os.getpid()}:{uuid.uuid4().hex}'
    fd = os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    try: os.write(fd,token.encode('ascii'))
    finally: os.close(fd)
    owns = False
    report = {'round':number,'status':'PENDING','local':[],'scenes':[],
        'diagnostic_digest':identity['specification_digest'],'physical_specification_digest':PHYSICAL_SPEC,
        'physical_digest':PHYSICAL_DIGEST,'vehicle_acceptance':'NOT_GRANTED','C_complete':False,
        'sysml':0,'online':0,'identity_probes':0,'retry_eligible':False}
    try:
        directory.mkdir(exist_ok=False); owns = True
        guard_sources(identity,packet,manifest)
        previous.snapshot_inputs(directory,identity,bundle,contract,manifest)
        execution.snapshot_readonly(directory,packet)
        save(directory/'vehicle-confirmation-identity.json',packet)
        save(directory/'diagnostic-identity.json',identity)
        dependency_audit(directory)
        for index,target in enumerate(local_targets(),1):
            guard_sources(identity,packet,manifest)
            batch.reserve('local',target,number)
            proc = batch.run([sys.executable,'-B','-m','unittest',target],ROOT.parent,10,directory,f'local-{index:02d}')
            status = 'PASS' if proc['returncode'] == 0 and not proc['timed_out'] else 'FAIL'
            report['local'].append({'test':target,'status':status})
            if proc['timed_out']: raise RuntimeError('LOCAL_TOOL_TIMEOUT')
            if status != 'PASS':
                stderr = (directory/f'local-{index:02d}.stderr.log').read_text(encoding='utf-8')
                if proc['returncode'] == 1 and 'AssertionError' in stderr and 'test_c_dimension_failure_diagnostic' in stderr:
                    raise ImplementationFailure('NEW_LOCAL_CHECK_FAILURE')
                raise RuntimeError('UNKNOWN_LOCAL_TOOL_OR_GUARD_FAILURE')
        guard_sources(identity,packet,manifest)
        _generated(packet,bundle,contract)
        row,folder = execute_scene(batch,directory,bundle,contract,number,manifest,identity,packet)
        report['scenes'].append(row)
        guard_sources(identity,packet,manifest)
        _classify_scene_error(row,folder)
        used_post = _completed_verification(batch,row,folder)
        _check_refined(folder)
        remaining = BUDGET['postprocess_timeout_s']-used_post
        if not math.isfinite(remaining) or remaining <= 0:
            raise RuntimeError('SHARED_POSTPROCESS_BUDGET_EXHAUSTED')
        proc = batch.run([sys.executable,'-B','-m','week12.scripts.s3_c_dimension_failure_diagnostic',
            '--compare',str(folder)],ROOT.parent,remaining,folder,'failure-comparison')
        if proc['returncode'] != 0 or proc['timed_out']:
            raise RuntimeError('DIAGNOSTIC_COMPARATOR_TOOL_FAILURE')
        comparison = read(folder/'failure-diagnostic.json')
        report['classification'] = comparison['classification']
        if comparison.get('vehicle_acceptance') != 'NOT_GRANTED' or report['classification'] not in FINAL_CLASSIFICATIONS:
            raise RuntimeError('INCOMPLETE_DIAGNOSTIC_EVIDENCE')
        report['status'] = 'DIAGNOSTIC_COMPLETED_VEHICLE_NOT_ACCEPTED'
    except BaseException as exc:
        report.update(status='STOPPED',error=type(exc).__name__+': '+str(exc),
            retry_eligible=isinstance(exc,ImplementationFailure),
            failure_code=str(exc) if isinstance(exc,ImplementationFailure) else 'TERMINAL')
    finally:
        try:
            report['usage'] = batch.usage()
            if report['usage']['unresolved_processes'] or report['usage']['process_seconds'] >= BUDGET['execution_total_wall_s']:
                report.update(status='STOPPED',retry_eligible=False,stop_reason='UNRESOLVED_PROCESS_OR_BUDGET_EXHAUSTED')
            if report['retry_eligible']:
                try: require_complete_round(batch)
                except ValueError: report.update(retry_eligible=False,stop_reason='NO_COMPLETE_REPAIR_ROUND_BUDGET')
            if owns:
                try: guard_sources(identity,packet,manifest); report['source_guard_end'] = 'PASS'
                except Exception as exc:
                    report.update(status='STOPPED',retry_eligible=False,source_guard_end='FAIL',
                        source_guard_error=type(exc).__name__+': '+str(exc))
                if number == 3: report['retry_eligible'] = False
                save(directory/'batch-result.json',report)
                if report['status'] == 'DIAGNOSTIC_COMPLETED_VEHICLE_NOT_ACCEPTED' or not report['retry_eligible']:
                    final = deepcopy(report)
                    final.update(unused_budget_disposition='RETIRED',automatic_additional_scene_permitted=False)
                    save(OUT/'final-result.json',final)
        finally:
            if lock.is_file() and lock.read_text(encoding='ascii') == token: lock.unlink()
    return report


def compare_folder(folder):
    folder = Path(folder).resolve(strict=True)
    identity,packet,_,contract = approved_inputs()
    if (folder.name != CASE+'-refined' or folder.parent.parent != OUT.resolve() or
            folder.parent.name not in ('round-01','round-02','round-03')):
        raise ValueError('OWNED_EXACT_DIAGNOSTIC_FOLDER_REQUIRED')
    if not (OUT/'.execution.lock').is_file():
        raise ValueError('ACTIVE_PARENT_DIAGNOSTIC_LOCK_REQUIRED')
    starts = sorted(OUT.glob('process-*.start.json'))
    if not starts: raise ValueError('METERED_COMPARISON_PROCESS_REQUIRED')
    started = read(starts[-1])
    expected_command = [sys.executable,'-B','-m','week12.scripts.s3_c_dimension_failure_diagnostic','--compare',str(folder)]
    if (started.get('label') != 'failure-comparison' or started.get('command') != expected_command or
            Path(started.get('cwd','')).resolve() != ROOT.parent.resolve() or
            not 0 < started.get('timeout_s',0) <= BUDGET['postprocess_timeout_s'] or
            starts[-1].with_name(starts[-1].name.replace('.start.json','.result.json')).exists()):
        raise ValueError('EXACT_ACTIVE_METERED_COMPARISON_PROCESS_REQUIRED')
    manifest = read(folder.parent/'source-manifest.json')
    guard_sources(identity,packet,manifest)
    refined = _check_refined(folder)
    result = compare_failure(contract,BASE/'result.csv',folder/'result.csv',read(BASE/'physical.json'),refined)
    guard_sources(identity,packet,manifest)
    save(folder/'failure-diagnostic.json',result)
    return result
