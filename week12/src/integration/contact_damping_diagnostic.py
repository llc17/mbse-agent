"""Source-only proposal and separately approved, finite damping diagnostic.

Only one independent physical parameter changes.  The old modules and closed
ledgers remain immutable; isolated function globals inject the new generator.
Passing this diagnostic never grants vehicle/C acceptance.
"""
from copy import deepcopy
import ast
import hashlib
import os
from pathlib import Path
import re
import sys
from types import FunctionType
import unittest
import uuid

from . import failure_diagnostic as previous
from . import vehicle_amendment as vehicle
from . import vehicle_amendment_execution as execution
from .execution import Batch, read, save, sha, require_sources, dependency_audit
from .review import check_review_receipt
from ..composition.schema import digest

ROOT = vehicle.ROOT
REVIEW = ROOT/'results/s3-c-contact-damping-review-20261005-01'
OUT = ROOT/'results/s3-c-contact-damping-execution-20261005-01'
PHYSICAL_SPEC = previous.PHYSICAL_SPEC
FLAT, STAIRS = 'fixed_nose_flat_brake', 'fixed_nose_three_up'
CASE_ORDER = (FLAT, STAIRS)
SCENES = tuple((case, refined) for case in CASE_ORDER for refined in (False, True))
NEW_PATHS = ('src/integration/contact_damping_diagnostic.py',
             'scripts/s3_c_contact_damping_diagnostic.py',
             'tests/test_c_contact_damping_diagnostic.py')
KNOWN_TASK_FAILURES = frozenset(('complete_geometry_beyond_exit', 'continued_exit_cruise',
    'continuous_high_slip', 'front_all_riser_crossings_with_physics',
    'no_stagnation_during_traversal', 'nose_all_riser_crossings_with_physics',
    'rear_all_riser_crossings_with_physics'))
BUDGET = {'scene_attempts_per_complete_round':4, 'maximum_rounds':3,
          'maximum_scene_attempts':12, 'new_local_execution_checks_limit':12,
          'new_local_timeout_s':10, 'sysml_parse_limit':0, 'identity_probe_limit':0,
          'product_online_calls':0, 'execution_total_wall_s':5400,
          'build_timeout_s':180, 'simulation_timeout_s':180,
          'postprocess_timeout_s':60}
PROVENANCE = {'origin':'AI single-parameter diagnostic hypothesis pending user confirmation',
    'per_physical_wheel_Ns_m':1500, 'physical_pair_Ns_m':3000,
    'reference':'numerical value c_ground=3000 from user original model only',
    'limits':'not measured hardware, not restoration of original physics; original terrain/drive/kinematics are different',
    'unchanged':'k=50000 per wheel/100000 per pair; 85kg, topology, radii, CoM/J, drives, controller, tasks and acceptance thresholds'}


class TerminalFailure(RuntimeError):
    """Physical, numerical, dependency or recovery failure: no implementation retry."""


def _prior():
    identity = read(previous.REVIEW/'diagnostic-review-packet.json')
    raw = dict(identity); checksum = raw.pop('specification_digest')
    if digest(raw) != checksum or identity['physical_specification_digest'] != PHYSICAL_SPEC:
        raise ValueError('OLD_DIAGNOSTIC_IDENTITY_DRIFT')
    rows = identity['implementation_manifest']
    if len(rows) != 24 or len(execution._manifest_paths(rows)) != 24:
        raise ValueError('EXACT_24_IMMUTABLE_PRIOR_SOURCES_REQUIRED')
    require_sources(rows)
    original_packet, original, contract = vehicle.inputs()
    execution.guard_sources(original_packet, rows)
    previous._check_reuse()
    final = read(previous.OUT/'final-result.json')
    if (final['status'] != 'DIAGNOSTIC_COMPLETED_VEHICLE_NOT_ACCEPTED' or
            final['classification'] != 'FAILURE_REPRODUCED_NUMERICALLY' or
            final['diagnostic_digest'] != checksum or
            final['usage']['unresolved_processes'] != 0 or
            final['unused_budget_disposition'] != 'RETIRED' or
            set(identity['expected_base_failed_checks']) != KNOWN_TASK_FAILURES):
        raise ValueError('CLOSED_PRIOR_FAILURE_DIAGNOSTIC_REQUIRED')
    return identity, original_packet, original, contract


def build_draft(original, old_contract):
    """Deep copy; pair damping is the derived value of the one wheel parameter."""
    bundle, contract = deepcopy(original), deepcopy(old_contract)
    physical = bundle['physical']
    if (physical['contact_per_physical_wheel']['damping_Ns_m'] != 100 or
            physical['wheel_pair_lowering']['damping_Ns_m'] != 200 or
            physical['contact_per_physical_wheel']['stiffness_N_m'] != 50000 or
            physical['wheel_pair_lowering']['stiffness_N_m'] != 100000 or
            len(bundle['design']['wheels']) != 3 or
            any(w['count'] != 2 for w in bundle['design']['wheels'])):
        raise ValueError('EXACT_ORIGINAL_THREE_PAIR_K_C_REQUIRED')
    physical['contact_per_physical_wheel']['damping_Ns_m'] = 1500
    physical['wheel_pair_lowering']['damping_Ns_m'] = 3000
    physical['status'] = 'PROPOSED_SINGLE_DAMPING_DIAGNOSTIC_NOT_AUTHORIZED'
    physical['provenance_notes']['contact_damping_diagnostic'] = deepcopy(PROVENANCE)
    vehicle._seal_physical(physical)
    contract['case_order'] = list(CASE_ORDER)
    contract['scope'] = 'single damping hypothesis, flat gate then three-up diagnostic; no vehicle acceptance or tuning'
    contract['budget_proposal'] = deepcopy(BUDGET)
    return bundle, contract


def _generate(bundle, case_id, packet, original):
    if case_id not in CASE_ORDER: raise ValueError('EXACT_TWO_DIAGNOSTIC_CASES_REQUIRED')
    result = vehicle.generate(original, case_id)
    old_source = result['modelica']
    pattern = r'(?m)^(    WheelPairWithTerrainPort wheel_[A-Za-z0-9_]+\([^\n]*k=100000\.0, )c=200\.0(?=, mu=)'
    if len(re.findall(pattern, old_source)) != 3:
        raise ValueError('EXACT_THREE_OLD_WHEEL_DAMPING_DECLARATIONS_REQUIRED')
    source, count = re.subn(pattern, r'\g<1>c=3000.0', old_source)
    if count != 3: raise ValueError('SINGLE_PARAMETER_CODEGEN_REQUIRED')
    source = source.replace(PHYSICAL_SPEC, packet['specification_digest'])
    # No SysML physics field exists for this parameter: preserve original bytes,
    # including its historical identity comment, and bind that fact in the trace.
    result['modelica'] = source
    if case_id != FLAT:
        canonical = vehicle.generate(original,FLAT)['sysml']
        marker = '// case='+STAIRS+' specification_digest='+PHYSICAL_SPEC
        if result['sysml'].count(marker) != 1 or result['sysml'].replace(marker,
                '// case='+FLAT+' specification_digest='+PHYSICAL_SPEC,1) != canonical:
            raise ValueError('ONLY_SYSML_CASE_COMMENT_MAY_DIFFER')
        result['sysml'] = canonical
    trace = result['trace']
    trace['previous_generator_version'] = trace['generator_version']
    trace['generator_version'] = 'v11-single-contact-damping-diagnostic-1'
    trace['frozen'].update(specification_digest=packet['specification_digest'],
        physical_digest=bundle['physical']['physical_digest'], verification_digest=packet['verification_digest'])
    trace['damping_diagnostic'] = deepcopy(PROVENANCE)
    trace['sysml_identity_comment_retained_from_original'] = PHYSICAL_SPEC
    trace['canonical_sysml'] = {'source':'previously parsed original flat vehicle structure',
        'case_comment':'historical flat comment is not the dynamic scenario; use Modelica/input identity for current case/physics',
        'guard':'stairs original differs from canonical only by exactly one case comment'}
    trace['original_modelica_sha256'] = hashlib.sha256(old_source.encode('utf-8')).hexdigest()
    trace['modelica_source_sha256'] = hashlib.sha256(source.encode('utf-8')).hexdigest()
    trace['sysml_source_sha256'] = hashlib.sha256(result['sysml'].encode('utf-8')).hexdigest()
    return result


def _reuse_files(prior):
    paths = [ROOT/r['path'] for r in prior['reused_evidence']]
    paths += [previous.REVIEW/'diagnostic-review-packet.json', previous.OUT/'authorization.json',
              previous.OUT/'approved-diagnostic.json', previous.OUT/'final-result.json',
              vehicle.REVIEW/'execution-review-packet.json',
              vehicle.OUT/'round-02/vehicle.sysml']
    for case in CASE_ORDER:
        for variant in ('base', 'refined'):
            folder = vehicle.OUT/'round-02'/(case+'-'+variant)
            if case == STAIRS and variant == 'refined':
                folder = previous.OUT/'round-01'/(case+'-'+variant)
            for name in ('result.csv','physical.json','result.json','trace.json','V11CGenerated.mo'):
                paths.append(folder/name)
            if case == FLAT and variant == 'refined': paths.append(folder/'numerical.json')
    return [{'path':str(p.resolve().relative_to(ROOT.resolve())), 'sha256':sha(p)}
            for p in sorted(set(paths))]


def prepare():
    """Write review/preview only. Never execute tests, tools, approval or ledger."""
    prior, original_packet, original, old_contract = _prior()
    bundle, contract = build_draft(original, old_contract)
    for name in NEW_PATHS: ast.parse((ROOT/name).read_text(encoding='utf-8'))
    if contract['numerical']['refined'] != previous.NUMERICS:
        raise ValueError('FROZEN_NUMERICS_REQUIRED')
    if next(c for c in bundle['physical']['cases'] if c['id'] == STAIRS)['stop_time_s'] != 35:
        raise ValueError('FROZEN_35S_STAIRS_REQUIRED')
    fixed = deepcopy(prior['implementation_manifest'])
    packet = {'scope':'v11_s3_c_single_contact_damping_diagnostic',
        'original_physical_specification_digest':PHYSICAL_SPEC,
        'original_bundle_digest':digest(original), 'bundle_digest':digest(bundle),
        'physical_digest':bundle['physical']['physical_digest'], 'verification_digest':digest(contract),
        'case_order':list(CASE_ORDER), 'variants':['base','refined'], 'budget':deepcopy(BUDGET),
        'numerical':deepcopy(contract['numerical']),
        'numerical_comparison':deepcopy(contract['numerical_comparison']),
        'known_stairs_task_failures':sorted(KNOWN_TASK_FAILURES),
        'fixed_inputs':deepcopy(original_packet['fixed_inputs']),
        'fixed_implementation_manifest':fixed,
        'implementation_manifest':[*fixed, *[{'path':str((ROOT/p).resolve()),'sha256':sha(ROOT/p)} for p in NEW_PATHS]],
        'reused_evidence':_reuse_files(prior), 'parameter_hypothesis':deepcopy(PROVENANCE),
        'no_prior_budget_carry_over':True, 'old_closed_batches_never_reopened':True,
        'vehicle_acceptance':'NOT_GRANTED', 'unused_budget_retired_on_completion':True,
        'refined_stairs_verification_and_comparison_share_60s':True,
        'repair_boundary':'only new three files; same physical/contract identity, full four-scene affected round; at most three rounds'}
    packet['specification_digest'] = digest(packet)
    generated = {case:_generate(bundle, case, packet, original) for case in CASE_ORDER}
    original_sysml = (vehicle.OUT/'round-02/vehicle.sysml').read_bytes()
    if any(generated[case]['sysml'].encode('utf-8') != original_sysml for case in CASE_ORDER):
        raise ValueError('EXACT_PREVIOUSLY_PARSED_SYSML_BYTES_REQUIRED')
    REVIEW.mkdir(exist_ok=False)
    save(REVIEW/'diagnostic-review-packet.json', packet)
    save(REVIEW/'proposal.json', bundle)
    save(REVIEW/'verification-contract.json', contract)
    for case, refined in SCENES:
        folder = REVIEW/(case+('-refined' if refined else '-base')); folder.mkdir()
        with (folder/'V11CGenerated.mo').open('x',encoding='utf-8',newline='\n') as stream:
            stream.write(generated[case]['modelica'])
        save(folder/'trace.json', generated[case]['trace'])
        with (folder/'build-preview.mos').open('x',encoding='utf-8',newline='\n') as stream:
            case_data = next(c for c in bundle['physical']['cases'] if c['id'] == case)
            stream.write(execution.build_script(generated[case]['model_class'],case_data['stop_time_s'],
                                               generated[case]['variable_filter'],refined))
    with (REVIEW/'vehicle.sysml').open('xb') as stream: stream.write(original_sysml)
    save(REVIEW/'preparation.json', {'status':'SOURCE_ONLY_PREPARED_NOT_AUTHORIZED',
        'new_tests_run':0,'new_simulations':0,'new_sysml':0,'online':0,
        'reused_sysml_sha256':sha(vehicle.OUT/'round-02/vehicle.sysml'),
        'limits':'new damping hypothesis; previous vehicle authorization does not approve this variant'})
    return packet


def validate_budget(packet):
    if packet.get('budget') != BUDGET or any(type(v) is not int for v in packet['budget'].values()):
        raise ValueError('EXACT_FINITE_DAMPING_BUDGET_REQUIRED')
    if packet.get('case_order') != list(CASE_ORDER) or packet.get('variants') != ['base','refined']:
        raise ValueError('EXACT_FOUR_SCENE_DIAGNOSTIC_REQUIRED')
    return deepcopy(BUDGET)


def guard_sources(packet, manifest, *, allow_new_drift=False):
    if execution._manifest_paths(manifest) != execution._manifest_paths(packet['implementation_manifest']):
        raise ValueError('COMPLETE_27_SOURCE_MANIFEST_REQUIRED')
    actual = {str(Path(r['path']).resolve()):r['sha256'] for r in manifest}
    fixed = packet['fixed_implementation_manifest']
    if any(actual[str(Path(r['path']).resolve())] != r['sha256'] for r in fixed):
        raise ValueError('IMMUTABLE_24_PRIOR_SOURCES_CANNOT_BE_REPAIRED')
    execution.guard_sources(packet, fixed if allow_new_drift else manifest)
    for row in packet['reused_evidence']:
        if sha(ROOT/row['path']) != row['sha256']: raise ValueError('REUSED_EVIDENCE_DRIFT')


def inputs():
    packet = read(REVIEW/'diagnostic-review-packet.json')
    raw = dict(packet); checksum = raw.pop('specification_digest')
    if digest(raw) != checksum: raise ValueError('DAMPING_IDENTITY_DRIFT')
    prior, original_packet, original, old_contract = _prior()
    bundle, contract = read(REVIEW/'proposal.json'), read(REVIEW/'verification-contract.json')
    expected = build_draft(original, old_contract)
    if (bundle != expected[0] or contract != expected[1] or
            packet['original_physical_specification_digest'] != PHYSICAL_SPEC or
            packet['original_bundle_digest'] != digest(original) or
            packet['bundle_digest'] != digest(bundle) or packet['verification_digest'] != digest(contract) or
            packet['physical_digest'] != bundle['physical']['physical_digest'] or
            packet['fixed_inputs'] != original_packet['fixed_inputs'] or
            packet['fixed_implementation_manifest'] != prior['implementation_manifest'] or
            packet['reused_evidence'] != _reuse_files(prior) or
            packet['parameter_hypothesis'] != PROVENANCE or
            packet['known_stairs_task_failures'] != sorted(KNOWN_TASK_FAILURES) or
            packet['numerical'] != old_contract['numerical'] or
            packet['numerical_comparison'] != old_contract['numerical_comparison']):
        raise ValueError('EXACT_SINGLE_PARAMETER_AND_FROZEN_CONTRACT_REQUIRED')
    validate_budget(packet)
    guard_sources(packet, packet['implementation_manifest'], allow_new_drift=True)
    return packet, bundle, contract


def generate(bundle, case_id):
    packet, expected, _ = inputs()
    if bundle != expected: raise ValueError('UNAPPROVED_DAMPING_BUNDLE')
    _, original, _ = vehicle.inputs()
    return _generate(bundle, case_id, packet, original)


class _VehicleProxy:
    __slots__ = ()
    def __getattr__(self, name):
        return generate if name == 'generate' else getattr(vehicle, name)
    def __setattr__(self, name, value): raise AttributeError('READ_ONLY_ISOLATED_VEHICLE_PROXY')


def execute_scene(*args, **kwargs):
    # Shallow-copy the globals mapping; no module/global monkeypatch occurs.
    old = execution.execute_scene
    isolated = FunctionType(old.__code__, dict(old.__globals__, amendment=_VehicleProxy()),
                            old.__name__, old.__defaults__, old.__closure__)
    return isolated(*args, **kwargs)


def initialize(receipt_path):
    path = Path(receipt_path)
    if not path.is_absolute() or path.name == '.env':
        raise ValueError('EXPLICIT_EXTERNAL_RECEIPT_PATH_REQUIRED')
    path = path.resolve(strict=True)
    packet, bundle, contract = inputs()
    receipt = read(path); check_review_receipt(receipt, packet)
    guard_sources(packet, packet['implementation_manifest'])
    OUT.mkdir(exist_ok=False)
    save(OUT/'authorization.json', receipt)
    save(OUT/'authorization-source.json', {'path':str(path), 'sha256':sha(path), 'receipt_not_created_by_runner':True})
    save(OUT/'approved-packet.json', packet)
    save(OUT/'proposal.json', bundle); save(OUT/'verification-contract.json', contract)
    save(OUT/'policy.json', {'budget':deepcopy(BUDGET), 'specification_digest':packet['specification_digest'],
        'physical_digest':packet['physical_digest'], 'proposal_digest':packet['bundle_digest'],
        'no_prior_budget_carry_over':True, 'old_batch_closed':True, 'vehicle_acceptance':'NOT_GRANTED'})
    return receipt


def approved_inputs():
    if (OUT/'final-result.json').exists(): raise ValueError('CLOSED_DAMPING_DIAGNOSTIC_CANNOT_RUN')
    packet, bundle, contract = inputs()
    check_review_receipt(read(OUT/'authorization.json'), packet)
    policy = read(OUT/'policy.json')
    if (read(OUT/'approved-packet.json') != packet or read(OUT/'proposal.json') != bundle or
            read(OUT/'verification-contract.json') != contract or policy['budget'] != BUDGET or
            policy['specification_digest'] != packet['specification_digest'] or
            policy['physical_digest'] != packet['physical_digest'] or
            policy['proposal_digest'] != packet['bundle_digest'] or
            policy.get('no_prior_budget_carry_over') is not True or
            policy.get('old_batch_closed') is not True or policy.get('vehicle_acceptance') != 'NOT_GRANTED'):
        raise ValueError('APPROVAL_EXECUTION_OR_POLICY_DRIFT')
    source = read(OUT/'authorization-source.json')
    if sha(source['path']) != source['sha256']: raise ValueError('EXTERNAL_RECEIPT_DRIFT')
    return packet, bundle, contract


def round_manifest(number, packet):
    if type(number) is not int or not 1 <= number <= 3: raise ValueError('ROUND_LIMIT')
    manifest = packet['implementation_manifest']
    if number > 1:
        directory = OUT/f'round-{number-1:02d}'
        result, repair = read(directory/'batch-result.json'), read(directory/'diagnosis.json')
        if (result['status'] != 'STOPPED' or result.get('retry_eligible') is not True or
                repair.get('classification') != 'implementation' or
                not isinstance(repair.get('reason'), str) or not repair['reason'].strip() or
                result['specification_digest'] != packet['specification_digest'] or
                repair.get('specification_digest') != packet['specification_digest'] or
                repair.get('physical_digest') != packet['physical_digest'] or
                repair.get('proposal_digest') != packet['bundle_digest']):
            raise ValueError('BOUND_NEW_FILE_IMPLEMENTATION_DIAGNOSIS_REQUIRED')
        manifest = repair['rebound_implementation_manifest']
        if digest(manifest) == digest(read(directory/'source-manifest.json')):
            raise ValueError('UNCHANGED_BLIND_RETRY_FORBIDDEN')
    guard_sources(packet, manifest)
    return manifest


def require_complete_round(batch):
    used = batch.usage()
    if (used['unresolved_processes'] or used['scene']+4 > BUDGET['maximum_scene_attempts'] or
            used['local']+4 > BUDGET['new_local_execution_checks_limit'] or used['sysml'] or used['probe'] or
            used['process_seconds']+1800 > BUDGET['execution_total_wall_s']):
        raise ValueError('NO_COMPLETE_FOUR_SCENE_ROUND_OR_UNRESOLVED_PROCESS')


def local_targets():
    def flatten(suite):
        for item in suite:
            if isinstance(item, unittest.TestSuite): yield from flatten(item)
            else: yield item.id()
    names = list(flatten(unittest.defaultTestLoader.loadTestsFromName('week12.tests.test_c_contact_damping_diagnostic')))
    if len(names) != 4 or len(set(names)) != 4 or any('_FailedTest' in n for n in names):
        raise ValueError('EXACT_FOUR_NEW_DAMPING_TESTS_REQUIRED')
    return names


def classify_physical(case, report, csv_path):
    if (report.get('status') not in ('PASS','FAIL') or report.get('diagnostics') or report.get('issues') or
            report.get('metrics',{}).get('case_id') != case or
            report.get('metrics',{}).get('csv_sha256') != sha(csv_path) or
            not report.get('checks') or any(type(c.get('pass')) is not bool for c in report['checks']) or
            len({c['name'] for c in report['checks']}) != len(report['checks'])):
        raise ValueError('COMPLETE_GENUINE_PHYSICAL_EVIDENCE_REQUIRED')
    failures = {c['name'] for c in report['checks'] if not c['pass']}
    if (report['status'] == 'PASS') != (not failures):
        raise ValueError('PHYSICAL_STATUS_CHECKS_MISMATCH')
    if not failures: return 'PHYSICAL_PASS'
    if case == STAIRS and failures <= KNOWN_TASK_FAILURES: return 'KNOWN_STAIRS_TASK_FAIL'
    return 'HARD_PHYSICAL_FAILURE'


def _verification_time(batch, row, folder):
    last = read(sorted(batch.root.glob('process-*.result.json'))[-1])
    physical = read(folder/'physical.json')
    expected_error = 'RuntimeError: '+('PHYSICAL_NUMERIC_OR_POSTPROCESS_FAILURE' if physical['status'] == 'FAIL'
                                      else 'INCOMPLETE_PHYSICAL_OR_NUMERIC_EVIDENCE')
    if (last.get('label') != 'verification' or last.get('returncode') != (1 if physical['status'] == 'FAIL' else 0) or
            last.get('timed_out') or last.get('uncertain') or last.get('interrupted') or
            row.get('compile') != 'PASS' or row.get('simulation') != 'COMPLETED' or
            row.get('physical') != physical['status'] or
            row.get('error') not in (None, expected_error) or
            (folder/'verification.stderr.log').read_text(encoding='utf-8').strip()):
        raise ValueError('UNEXPECTED_VERIFICATION_TOOL_FAILURE')
    return last['duration_s']


def classify_numerical(result, a, b):
    """Separate task outcome from consistency of two new-physics resolutions."""
    if result['classification'] == 'INCOMPLETE': return 'INCOMPLETE'
    consistent = (a['status'] == b['status'] and
        result.get('base_failed_checks') == result.get('refined_failed_checks') and
        result.get('checks') and all(c['pass'] for c in result['checks']))
    if not consistent: return 'NUMERICAL_UNRESOLVED'
    return 'TASK_PASS_NUMERICALLY_CONSISTENT' if a['status'] == 'PASS' else 'FAILURE_REPRODUCED_NUMERICALLY'


def scene_tool_failure(row, folder):
    """Known physical asserts are terminal; unresolved simulation failure is not a repair diagnosis."""
    text = '\n'.join((folder/name).read_text(encoding='utf-8') for name in
        ('simulation.stdout.log','simulation.stderr.log') if (folder/name).is_file())
    physical_asserts = ('Wheel centre entered the terrain solid; fixture is physically invalid',
        'Contact dissipation must be nonnegative','Drive torque exceeds paired continuous rating',
        'Drive shaft power exceeds paired continuous rating','DC bus power exceeds supply rating',
        'DC usable energy exhausted','Non-regenerative drive loss must be nonnegative')
    if any(marker in text for marker in physical_asserts):
        raise TerminalFailure('PHYSICAL_SIMULATION_ASSERTION')
    if row.get('compile') == 'PASS' and row.get('simulation') != 'COMPLETED':
        raise TerminalFailure('SIMULATION_FAILURE_CLASSIFICATION_UNRESOLVED_NOT_IMPLEMENTATION_APPROVED')
    if 'DRIFT' in row.get('error',''):
        raise TerminalFailure('SCENE_DEPENDENCY_OR_IDENTITY_DRIFT')
    raise RuntimeError('SCENE_TOOL_FAILURE_REQUIRES_IMPLEMENTATION_DIAGNOSIS')


def run_round(number):
    packet, bundle, contract = approved_inputs()
    manifest = round_manifest(number, packet)
    batch = Batch(OUT, packet['specification_digest']); require_complete_round(batch)
    directory = OUT/f'round-{number:02d}'
    if directory.exists(): raise ValueError('ROUND_DIRECTORY_ALREADY_EXISTS')
    lock, token = OUT/'.execution.lock', f'{os.getpid()}:{uuid.uuid4().hex}'
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try: os.write(fd, token.encode('ascii'))
    finally: os.close(fd)
    owns = False
    report = {'round':number,'status':'PENDING','local':[],'scenes':[],
        'specification_digest':packet['specification_digest'], 'physical_digest':packet['physical_digest'],
        'proposal_digest':packet['bundle_digest'], 'vehicle_acceptance':'NOT_GRANTED',
        'C_complete':False, 'sysml':0,'online':0,'identity_probes':0,'retry_eligible':False}
    try:
        directory.mkdir(exist_ok=False); owns = True
        guard_sources(packet, manifest)
        execution.snapshot_inputs(directory, packet, bundle, contract, manifest)
        try: dependency_audit(directory)
        except Exception as exc: raise TerminalFailure('DEPENDENCY_DRIFT_OR_UNVERIFIED: '+str(exc)) from exc
        for index, target in enumerate(local_targets(), 1):
            guard_sources(packet, manifest); batch.reserve('local', target, number)
            proc = batch.run([sys.executable,'-B','-m','unittest',target], ROOT.parent, 10, directory, f'local-{index:02d}')
            status = 'PASS' if proc['returncode'] == 0 and not proc['timed_out'] else 'FAIL'
            report['local'].append({'test':target,'status':status})
            if status != 'PASS': raise RuntimeError('FIRST_LOCAL_FAILURE_REQUIRES_IMPLEMENTATION_DIAGNOSIS')
        base = None
        for case, refined in SCENES:
            guard_sources(packet, manifest)
            # Only the flat acceptance pair uses the original PASS comparator.
            # Stairs compare is the separate same-new-physics diagnostic child.
            row, folder = execute_scene(batch,directory,bundle,contract,case,refined,number,manifest,packet,
                                        base=base if case == FLAT and refined else None)
            report['scenes'].append(row); guard_sources(packet, manifest)
            if not (folder/'physical.json').is_file(): scene_tool_failure(row,folder)
            physical = read(folder/'physical.json')
            classification = classify_physical(case, physical, folder/'result.csv')
            row['diagnostic_physical_classification'] = classification
            if classification == 'HARD_PHYSICAL_FAILURE': raise TerminalFailure('HARD_PHYSICAL_FAILURE')
            if case == FLAT:
                if refined and row.get('numeric') == 'FAIL': raise TerminalFailure('FLAT_NUMERICAL_UNRESOLVED')
                if row['status'] != 'PASS' or (refined and row.get('numeric') != 'PASS'):
                    raise RuntimeError('FLAT_COMPLETE_PASS_AND_NUMERICAL_EVIDENCE_REQUIRED')
            else:
                used_post = _verification_time(batch,row,folder)
                if refined:
                    remaining = 60-used_post
                    if remaining <= 0: raise TerminalFailure('SHARED_POSTPROCESS_BUDGET_EXHAUSTED')
                    proc = batch.run([sys.executable,'-B','-m','week12.scripts.s3_c_contact_damping_diagnostic',
                        '--compare',str(folder)],ROOT.parent,remaining,folder,'damping-comparison')
                    if proc['returncode'] != 0 or proc['timed_out']:
                        raise RuntimeError('DAMPING_COMPARATOR_TOOL_FAILURE')
                    comparison = read(folder/'damping-diagnostic.json')
                    report['classification'] = comparison['classification']
                    if comparison['classification'] == 'NUMERICAL_UNRESOLVED':
                        raise TerminalFailure('STAIRS_NUMERICAL_UNRESOLVED')
                    if comparison['classification'] not in ('FAILURE_REPRODUCED_NUMERICALLY','TASK_PASS_NUMERICALLY_CONSISTENT'):
                        raise RuntimeError('INCOMPLETE_DAMPING_DIAGNOSTIC')
            if not refined: base = folder
        report['status'] = 'DIAGNOSTIC_COMPLETED_VEHICLE_NOT_ACCEPTED'
    except BaseException as exc:
        report.update(status='STOPPED',error=type(exc).__name__+': '+str(exc),
                      retry_eligible=not isinstance(exc,(TerminalFailure,KeyboardInterrupt,SystemExit)))
    finally:
        try:
            report['usage'] = batch.usage()
            if report['usage']['unresolved_processes']:
                report.update(status='STOPPED',retry_eligible=False,stop_reason='UNRESOLVED_PROCESS')
            if owns:
                try: guard_sources(packet, manifest); report['source_guard_end'] = 'PASS'
                except Exception as exc:
                    report.update(status='STOPPED',retry_eligible=False,source_guard_end='FAIL',
                                  source_guard_error=type(exc).__name__+': '+str(exc))
                report['not_run'] = [case+('-refined' if refined else '-base') for case,refined in SCENES
                    if not any(r['case'] == case and r['variant'] == ('refined' if refined else 'base') for r in report['scenes'])]
                save(directory/'batch-result.json',report)
                if report['status'] == 'DIAGNOSTIC_COMPLETED_VEHICLE_NOT_ACCEPTED' or not report['retry_eligible'] or number == 3:
                    final = deepcopy(report)
                    final.update(unused_budget_disposition='RETIRED',automatic_additional_scene_permitted=False)
                    save(OUT/'final-result.json', final)
        finally:
            if lock.is_file() and lock.read_text(encoding='ascii') == token: lock.unlink()
    return report


def compare_folder(folder):
    folder = Path(folder).resolve(strict=True)
    packet, _, contract = approved_inputs()
    if folder.name != STAIRS+'-refined' or folder.parent.parent != OUT.resolve():
        raise ValueError('OWNED_EXACT_NEW_STAIRS_REFINED_FOLDER_REQUIRED')
    if not (OUT/'.execution.lock').is_file(): raise ValueError('ACTIVE_PARENT_LOCK_REQUIRED')
    starts = sorted(OUT.glob('process-*.start.json'))
    if not starts: raise ValueError('METERED_COMPARISON_REQUIRED')
    start = read(starts[-1])
    if (start.get('label') != 'damping-comparison' or start.get('command',[])[-2:] != ['--compare',str(folder)] or
            starts[-1].with_name(starts[-1].name.replace('.start.json','.result.json')).exists()):
        raise ValueError('ACTIVE_METERED_COMPARISON_REQUIRED')
    manifest = read(folder.parent/'source-manifest.json'); guard_sources(packet,manifest)
    base = folder.parent/(STAIRS+'-base')
    a, b = read(base/'physical.json'), read(folder/'physical.json')
    for owned, report in ((base,a),(folder,b)):
        if classify_physical(STAIRS,report,owned/'result.csv') == 'HARD_PHYSICAL_FAILURE':
            raise TerminalFailure('HARD_PHYSICAL_FAILURE')
        bound = read(owned/'input-identity.json')
        if (bound['physical_digest'] != packet['physical_digest'] or bound['proposal_digest'] != packet['bundle_digest'] or
                bound['specification_digest'] != packet['specification_digest'] or
                bound['implementation_digest'] != digest(manifest) or bound['modelica_sha256'] != sha(owned/'V11CGenerated.mo')):
            raise ValueError('SAME_NEW_PHYSICS_PAIR_BINDING_REQUIRED')
    if (base/'V11CGenerated.mo').read_bytes() != (folder/'V11CGenerated.mo').read_bytes():
        raise ValueError('IDENTICAL_NEW_PHYSICS_SOURCE_REQUIRED')
    result = previous.compare_failure(contract,base/'result.csv',folder/'result.csv',a,b)
    original_classification = result['classification']
    result['original_comparator_classification'] = original_classification
    result['classification'] = classify_numerical(result,a,b)
    result['comparison_scope'] = 'same new physical variant only; old/new parameter effect is not trajectory consistency'
    old = read(previous.BASE/'physical.json')
    result['parameter_effect'] = {'classification':'PARAMETER_EFFECT_ONLY_NOT_NUMERICAL_ACCEPTANCE',
        'old_physical_specification_digest':PHYSICAL_SPEC,'new_physical_digest':packet['physical_digest'],
        'old_base_csv_sha256':old['metrics']['csv_sha256'],'new_base_csv_sha256':a['metrics']['csv_sha256'],
        'old_status':old['status'],'new_status':a['status'],
        'old_failed_checks':sorted(c['name'] for c in old['checks'] if not c['pass']),
        'new_failed_checks':sorted(c['name'] for c in a['checks'] if not c['pass']),
        'old_signals':deepcopy(old['metrics']['signals']),'new_signals':deepcopy(a['metrics']['signals']),
        'old_cumulative':deepcopy(old['metrics']['cumulative']),'new_cumulative':deepcopy(a['metrics']['cumulative']),
        'old_events':deepcopy(old['metrics']['event_times']),'new_events':deepcopy(a['metrics']['event_times']),
        'old_task_geometry':{key:deepcopy(old['metrics'].get(key)) for key in
            ('high_slip_longest_s','terminal_geometry_x_min_m','minimum_nonwheel_clearance_m',
             'wheel_penetration_geometry_error_max_m','pitch_peak_rad')},
        'new_task_geometry':{key:deepcopy(a['metrics'].get(key)) for key in
            ('high_slip_longest_s','terminal_geometry_x_min_m','minimum_nonwheel_clearance_m',
             'wheel_penetration_geometry_error_max_m','pitch_peak_rad')},
        'claim':'observed parameter effect only; no claim of hardware measurement, restored original model or unique cause'}
    guard_sources(packet,manifest)
    save(folder/'damping-diagnostic.json',result)
    return result
