"""One dimension candidate: source preparation and externally approved execution.

Old C2/B sources and closed ledgers are immutable. Importing starts no tools.
Unknown failures close execution; only diagnosed new-adapter implementation
errors can consume another complete round under the original exact proposal.
"""
from copy import deepcopy
import ast
import os
from pathlib import Path
import sys
from types import FunctionType
import unittest
import uuid

from . import dimension_revision as revision
from . import vehicle_amendment as vehicle
from . import vehicle_amendment_execution as previous
from .execution import Batch, read, save, sha, dependency_audit
from .review import check_review_receipt
from ..composition.schema import digest

ROOT = revision.ROOT
REVIEW = ROOT/'results/s3-c-dimension-review-20261006-01'
OUT = ROOT/'results/s3-c-dimension-execution-20261006-01'
CASE_ORDER = tuple(revision.CASE_ORDER)
SCENES = tuple((case, refined) for case in CASE_ORDER for refined in (False, True))
BUDGET = deepcopy(revision.BUDGET)
SCOPE = 'v11_s3_c_one_dimension_revision_flat_and_three_up'
NEW_PATHS = ('src/integration/dimension_revision.py',
             'src/integration/dimension_execution.py',
             'scripts/s3_c_dimension_revision.py',
             'tests/test_c_dimension_revision.py',
             'tests/test_c_dimension_execution.py')
PRIOR_MANIFEST = vehicle.OUT/'round-02/source-manifest.json'
PRIOR_MANIFEST_SHA = '853fe0c1ae93f9d9592e62834507341dc31a1ef71cf1d655debd2458f5c7ebf0'
REPAIR_CODES = frozenset(('NEW_GENERATOR_INTERFACE', 'NEW_LOCAL_CHECK_FAILURE',
                         'NEW_SYSML_INTERFACE', 'NEW_GENERATED_SYNTAX'))
READONLY_PATHS = ('src/integration/preview.py',)


class TerminalFailure(RuntimeError):
    """Never eligible for automatic implementation retry."""


class ImplementationFailure(RuntimeError):
    """Candidate local adapter error; a concrete diagnosis is still mandatory."""


def _prior():
    if sha(PRIOR_MANIFEST) != PRIOR_MANIFEST_SHA:
        raise ValueError('FROZEN_C2_SOURCE_MANIFEST_DRIFT')
    rows = read(PRIOR_MANIFEST)
    if len(rows) != 21 or len(previous._manifest_paths(rows)) != 21:
        raise ValueError('EXACT_21_IMMUTABLE_C2_SOURCES_REQUIRED')
    packet, _, _ = vehicle.inputs()
    previous.guard_sources(packet, rows)
    return rows, packet['fixed_inputs']


def validate_budget(packet, contract=None):
    if packet.get('budget') != BUDGET:
        raise ValueError('EXACT_PENDING_FINITE_BUDGET_REQUIRED')
    if any(type(packet['budget'][key]) is not int for key,value in BUDGET.items()
           if type(value) is int):
        raise ValueError('INTEGER_EXECUTION_LIMITS_REQUIRED')
    if (packet.get('case_order') != list(CASE_ORDER) or
            packet.get('variants') != ['base', 'refined']):
        raise ValueError('EXACT_FOUR_DIMENSION_SCENES_REQUIRED')
    if contract is not None and contract.get('budget_proposal') != BUDGET:
        raise ValueError('CONTRACT_BUDGET_DRIFT')
    return deepcopy(BUDGET)


def _maps(rows):
    return {str(Path(r['path']).resolve()): r['sha256'] for r in rows}


def guard_sources(packet, manifest, *, allow_new_drift=False):
    fixed, fixed_inputs = _prior()
    if (packet['fixed_implementation_manifest'] != fixed or
            packet['fixed_inputs'] != fixed_inputs):
        raise ValueError('IMMUTABLE_C2_INPUT_OR_SOURCE_BINDING_DRIFT')
    expected = previous._manifest_paths(packet['implementation_manifest'])
    new = {str((ROOT/p).resolve()) for p in NEW_PATHS}
    if expected != set(_maps(fixed)) | new or len(expected) != 26:
        raise ValueError('EXACT_21_PLUS_5_MANIFEST_REQUIRED')
    if previous._manifest_paths(manifest) != expected:
        raise ValueError('COMPLETE_26_SOURCE_MANIFEST_REQUIRED')
    current = _maps(manifest)
    if any(current[p] != h for p, h in _maps(fixed).items()):
        raise ValueError('OLD_21_SOURCES_CANNOT_BE_REPAIRED')
    previous.guard_sources(packet, fixed if allow_new_drift else manifest)
    readonly = packet['readonly_manifest']
    if {str(Path(r['path']).resolve()) for r in readonly} != {str((ROOT/p).resolve()) for p in READONLY_PATHS}:
        raise ValueError('EXACT_READONLY_HELPER_BINDING_REQUIRED')
    for row in readonly:
        if sha(row['path']) != row['sha256']:
            raise ValueError('READONLY_HELPER_SOURCE_DRIFT')
    for row in packet.get('review_evidence', []):
        if sha(ROOT/row['path']) != row['sha256']:
            raise ValueError('FROZEN_REVIEW_EVIDENCE_DRIFT')


def prepare():
    """Exclusive source-only writes; main reviewer calls after all five files exist."""
    bundle, contract = revision.build_draft()
    review = revision.validate(bundle, contract)
    fixed, fixed_inputs = _prior()
    for name in NEW_PATHS:
        ast.parse((ROOT/name).read_text(encoding='utf-8'))
    evidence = []
    for name in ('scope-authorization.json', 'candidate-independent.json'):
        path = REVIEW/name
        evidence.append({'path':str(path.relative_to(ROOT)), 'sha256':sha(path)})
    packet = {'scope':SCOPE, 'bundle_digest':digest(bundle),
        'design_digest':bundle['design']['design_digest'],
        'physical_digest':bundle['physical']['physical_digest'],
        'verification_digest':digest(contract), 'case_order':list(CASE_ORDER),
        'variants':['base', 'refined'], 'budget':deepcopy(BUDGET),
        'fixed_inputs':deepcopy(fixed_inputs), 'fixed_implementation_manifest':fixed,
        'readonly_manifest':[{'path':str((ROOT/p).resolve()),'sha256':sha(ROOT/p)} for p in READONLY_PATHS],
        'implementation_manifest':[*fixed, *[{'path':str((ROOT/p).resolve()),
                                           'sha256':sha(ROOT/p)} for p in NEW_PATHS]],
        'review_evidence':evidence, 'old_closed_batches_never_reopened':True,
        'no_prior_budget_carry_over':True, 'C_complete':False,
        'repair_boundary':'exact new five files only; same bundle/contract; explicit diagnosis; changed source; full four-scene round',
        'hard_failure_stops_immediately':True, 'known_old_task_failures_not_exempt':True,
        'unused_budget_retired_on_completion':True}
    packet['specification_digest'] = digest(packet)
    generated = {case:revision.generate(bundle, contract, case,
                 packet['specification_digest']) for case in CASE_ORDER}
    # Generate and validate all strings before beginning exclusive writes.
    validate_budget(packet, contract)
    guard_sources(packet, packet['implementation_manifest'])
    targets = [REVIEW/name for name in ('dimension-review-packet.json', 'proposal.json',
        'verification-contract.json', 'static-review.json', 'preparation.json', 'vehicle.sysml',
        'digests.json', 'source-manifest.json', 'fixed-input-manifest.json')]
    targets += [REVIEW/(case+('-refined' if refined else '-base')) for case,refined in SCENES]
    targets += [REVIEW/'source-snapshot', REVIEW/'input-snapshot',
                REVIEW/'confirmation-identity.json', REVIEW/'readonly-snapshot', REVIEW/'readonly-manifest.json']
    if any(path.exists() for path in targets):
        raise ValueError('SOURCE_PREPARATION_ALREADY_EXISTS_NO_OVERWRITE')
    previous.snapshot_inputs(REVIEW, packet, bundle, contract, packet['implementation_manifest'])
    snapshot_readonly(REVIEW, packet)
    save(REVIEW/'dimension-review-packet.json', packet)
    save(REVIEW/'static-review.json', review)
    save(REVIEW/'digests.json', {key:packet[key] for key in
        ('specification_digest', 'bundle_digest', 'design_digest', 'physical_digest', 'verification_digest')})
    with (REVIEW/'vehicle.sysml').open('x',encoding='utf-8',newline='\n') as stream:
        stream.write(generated[CASE_ORDER[0]]['sysml'])
    for case, refined in SCENES:
        folder = REVIEW/(case+('-refined' if refined else '-base'))
        folder.mkdir(exist_ok=False)
        result = generated[case]
        for filename, source in (('V11CGenerated.mo', result['modelica']),
                                 ('V11PhysicalB1.mo', vehicle.isolated_b())):
            with (folder/filename).open('x',encoding='utf-8',newline='\n') as stream:
                stream.write(source)
        with (folder/'V11ContactC2.mo').open('xb') as stream:
            stream.write(vehicle.CANDIDATE.read_bytes())
        case_data = next(c for c in bundle['physical']['cases'] if c['id'] == case)
        with (folder/'build-preview.mos').open('x',encoding='utf-8',newline='\n') as stream:
            stream.write(previous.build_script(result['model_class'], case_data['stop_time_s'],
                                               result['variable_filter'], refined))
        save(folder/'trace.json', result['trace'])
    save(REVIEW/'preparation.json', {'status':'SOURCE_ONLY_PREPARED_NOT_AUTHORIZED',
        'new_tests_run':0, 'new_simulations':0, 'new_sysml_parses':0,
        'online':0, 'identity_probes':0, 'C_complete':False,
        'pending':'concrete physical proposal and total finite execution budget confirmation'})
    return packet


def snapshot_readonly(directory, packet):
    save(directory/'readonly-manifest.json', packet['readonly_manifest'])
    for row in packet['readonly_manifest']:
        path = Path(row['path'])
        target = directory/'readonly-snapshot'/path.relative_to(ROOT)
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open('xb') as stream: stream.write(path.read_bytes())


def inputs():
    packet = read(REVIEW/'dimension-review-packet.json')
    raw = dict(packet); checksum = raw.pop('specification_digest')
    if digest(raw) != checksum or packet.get('scope') != SCOPE:
        raise ValueError('DIMENSION_REVIEW_IDENTITY_DRIFT')
    bundle = read(REVIEW/'proposal.json')
    contract = read(REVIEW/'verification-contract.json')
    revision.validate(bundle, contract)
    if (packet['bundle_digest'] != digest(bundle) or
            packet['design_digest'] != bundle['design']['design_digest'] or
            packet['physical_digest'] != bundle['physical']['physical_digest'] or
            packet['verification_digest'] != digest(contract)):
        raise ValueError('ACTUAL_CANDIDATE_OR_CONTRACT_DRIFT')
    validate_budget(packet, contract)
    guard_sources(packet, packet['implementation_manifest'], allow_new_drift=True)
    return packet, bundle, contract


def initialize(receipt_path):
    """Require existing external receipt. Never derive approval from preparation."""
    path = Path(receipt_path)
    if not path.is_absolute() or path.name == '.env':
        raise ValueError('EXPLICIT_ABSOLUTE_EXTERNAL_RECEIPT_REQUIRED')
    path = path.resolve(strict=True)
    packet, bundle, contract = inputs()
    receipt = read(path)
    check_review_receipt(receipt, packet)
    guard_sources(packet, packet['implementation_manifest'])
    OUT.mkdir(exist_ok=False)
    save(OUT/'authorization.json', receipt)
    save(OUT/'authorization-source.json', {'path':str(path), 'sha256':sha(path),
                                         'receipt_not_created_by_runner':True})
    save(OUT/'approved-packet.json', packet)
    save(OUT/'proposal.json', bundle)
    save(OUT/'verification-contract.json', contract)
    save(OUT/'policy.json', {'budget':deepcopy(BUDGET), 'scope':SCOPE,
        'specification_digest':packet['specification_digest'],
        'physical_digest':packet['physical_digest'], 'proposal_digest':packet['bundle_digest'],
        'no_prior_budget_carry_over':True, 'hard_failure_stops_immediately':True,
        'unknown_failure_is_terminal':True, 'C_complete':False,
        'accounting':'actual child wall seconds including failure; no previous ledger balances'})
    save(OUT/'initial-state.json', {'status':'APPROVED_NOT_RUN', 'scenes':0, 'local':0,
                                   'sysml':0, 'online':0, 'identity_probes':0})
    return receipt


def approved_inputs():
    if (OUT/'final-result.json').exists():
        raise ValueError('CLOSED_DIMENSION_BATCH_CANNOT_RUN')
    packet, bundle, contract = inputs()
    check_review_receipt(read(OUT/'authorization.json'), packet)
    policy = read(OUT/'policy.json')
    if (read(OUT/'approved-packet.json') != packet or read(OUT/'proposal.json') != bundle or
            read(OUT/'verification-contract.json') != contract or policy['budget'] != BUDGET or
            policy['scope'] != SCOPE or policy['specification_digest'] != packet['specification_digest'] or
            policy['physical_digest'] != packet['physical_digest'] or
            policy['proposal_digest'] != packet['bundle_digest'] or
            policy.get('no_prior_budget_carry_over') is not True or
            policy.get('hard_failure_stops_immediately') is not True or
            policy.get('unknown_failure_is_terminal') is not True or policy.get('C_complete') is not False):
        raise ValueError('APPROVAL_EXECUTION_OR_POLICY_DRIFT')
    source = read(OUT/'authorization-source.json')
    if sha(source['path']) != source['sha256']:
        raise ValueError('EXTERNAL_RECEIPT_DRIFT')
    check_review_receipt(read(source['path']), packet)
    return packet, bundle, contract


def validate_repair(packet, prior_manifest, result, diagnosis):
    """Keep original approval identity while binding exact diagnosed new source."""
    if (result.get('status') != 'STOPPED' or result.get('retry_eligible') is not True or
            result.get('failure_code') not in REPAIR_CODES or
            diagnosis.get('classification') != 'implementation' or
            diagnosis.get('failure_code') != result['failure_code']):
        raise ValueError('EXPLICIT_LOCAL_IMPLEMENTATION_FAILURE_REQUIRED')
    for key in ('specification_digest', 'physical_digest', 'proposal_digest'):
        expected = packet['bundle_digest'] if key == 'proposal_digest' else packet[key]
        if result.get(key) != expected or diagnosis.get(key) != expected:
            raise ValueError('SAME_PHYSICAL_PROPOSAL_DIAGNOSIS_REQUIRED')
    if (diagnosis.get('same_physical_task_verification') is not True or
            not isinstance(diagnosis.get('reason'), str) or not diagnosis['reason'].strip() or
            not isinstance(diagnosis.get('evidence_files'), list) or not diagnosis['evidence_files']):
        raise ValueError('CONCRETE_DIAGNOSIS_AND_EVIDENCE_REQUIRED')
    for row in diagnosis['evidence_files']:
        path = (OUT/row['path']).resolve(strict=True)
        path.relative_to(OUT.resolve())
        if path.name == '.env' or sha(path) != row['sha256']:
            raise ValueError('DIAGNOSIS_EVIDENCE_DRIFT')
    manifest = diagnosis['rebound_implementation_manifest']
    if previous._manifest_paths(manifest) != previous._manifest_paths(prior_manifest):
        raise ValueError('COMPLETE_REBOUND_MANIFEST_REQUIRED')
    before, after = _maps(prior_manifest), _maps(manifest)
    changed = sorted(p for p in before if before[p] != after[p])
    new = {str((ROOT/p).resolve()) for p in NEW_PATHS}
    if not changed or not set(changed) <= new or diagnosis.get('changed_paths') != changed:
        raise ValueError('EXACT_NEW_FIVE_CHANGED_SOURCES_REQUIRED_NO_BLIND_RETRY')
    guard_sources(packet, manifest)
    return manifest


def round_manifest(number, packet):
    if type(number) is not int or not 1 <= number <= 3:
        raise ValueError('ROUND_LIMIT')
    if number == 1:
        if any(OUT.glob('round-*')):
            raise ValueError('FIRST_ROUND_MUST_START_EMPTY')
        manifest = packet['implementation_manifest']
    else:
        directory = OUT/f'round-{number-1:02d}'
        manifest = validate_repair(packet, read(directory/'source-manifest.json'),
                                  read(directory/'batch-result.json'), read(directory/'diagnosis.json'))
    guard_sources(packet, manifest)
    return manifest


def require_complete_round(batch):
    used = batch.usage()
    if (batch.budget != BUDGET or used['unresolved_processes'] or used['probe'] or
            used['scene']+4 > BUDGET['maximum_scene_attempts'] or
            used['local']+8 > BUDGET['new_local_execution_checks_limit'] or
            used['sysml']+1 > BUDGET['sysml_parse_limit'] or
            used['process_seconds']+2000 > BUDGET['execution_total_wall_s']):
        raise ValueError('COMPLETE_8_LOCAL_1_SYSML_4_SCENE_2000S_ROUND_REQUIRED')


def local_targets():
    def flatten(suite):
        for item in suite:
            if isinstance(item, unittest.TestSuite): yield from flatten(item)
            else: yield item.id()
    targets = []
    for module in ('week12.tests.test_c_dimension_revision', 'week12.tests.test_c_dimension_execution'):
        rows = list(flatten(unittest.defaultTestLoader.loadTestsFromName(module)))
        if len(rows) != 4 or any('_FailedTest' in row for row in rows):
            raise ValueError('EXACT_FOUR_TESTS_PER_NEW_MODULE_REQUIRED')
        targets.extend(rows)
    if len(set(targets)) != 8:
        raise ValueError('EXACT_EIGHT_NEW_LOCAL_CHECKS_REQUIRED')
    return targets


class _VehicleProxy:
    def __init__(self, contract, packet):
        object.__setattr__(self, '_contract', contract)
        object.__setattr__(self, '_packet', packet)
    def __getattr__(self, name):
        if name == 'generate':
            return lambda bundle, case: revision.generate(bundle, self._contract, case,
                                                         self._packet['specification_digest'])
        return getattr(vehicle, name)
    def __setattr__(self, name, value):
        raise AttributeError('READ_ONLY_ISOLATED_VEHICLE_PROXY')


def execute_scene(batch, directory, bundle, contract, case_id, refined, number, manifest, packet, base=None):
    old = previous.execute_scene
    namespace = dict(old.__globals__, amendment=_VehicleProxy(contract, packet), guard_sources=guard_sources)
    isolated = FunctionType(old.__code__, namespace, old.__name__, old.__defaults__, old.__closure__)
    return isolated(batch, directory, bundle, contract, case_id, refined, number, manifest, packet, base)


def _completed_build_evidence(folder):
    records = []
    for path in OUT.glob('process-*.start.json'):
        started = read(path)
        if started.get('label') == 'build' and Path(started.get('cwd', '')).resolve() == folder.resolve():
            finished = path.with_name(path.name.replace('.start.json', '.result.json'))
            if not finished.is_file(): return False
            records.append(read(finished))
    return (len(records) == 1 and records[0].get('label') == 'build' and
            type(records[0].get('returncode')) is int and
            records[0]['returncode'] != 0 and
            records[0].get('timed_out') is False and records[0].get('interrupted') is False and
            records[0].get('uncertain') is False)


def classify_scene(row, folder):
    """No diagnostic exception for the old seven failed stair checks."""
    if row.get('physical') == 'FAIL' or row.get('numeric') == 'FAIL':
        raise TerminalFailure('PHYSICAL_TASK_OR_NUMERICAL_FAIL')
    if row.get('status') != 'PASS':
        if row.get('compile') != 'PASS' and folder is not None:
            text = '\n'.join((folder/name).read_text(encoding='utf-8') for name in
                ('build.stdout.log', 'build.stderr.log') if (folder/name).is_file())
            # Restrict evidence to a parser syntax error in the newly generated
            # Modelica file. Unknown compiler/tool errors remain terminal.
            if ('V11CGenerated.mo' in text and 'syntax error' in text.lower() and
                    row.get('error') == 'RuntimeError: BUILD_TOOL_FAILURE' and
                    _completed_build_evidence(folder)):
                raise ImplementationFailure('NEW_GENERATED_SYNTAX')
        # Missing report, build failure or unknown simulation state is not proof
        # of a local implementation bug and cannot authorize a repeat.
        raise TerminalFailure('UNKNOWN_SCENE_TOOL_OR_SIMULATION_FAILURE')
    if row.get('physical') != 'PASS' or (row.get('variant') == 'refined' and row.get('numeric') != 'PASS'):
        raise TerminalFailure('INCOMPLETE_SCENE_EVIDENCE')
    if folder is not None:
        physical = read(folder/'physical.json')
        if (physical.get('status') != 'PASS' or physical.get('diagnostics') or physical.get('issues') or
                not physical.get('checks') or any(c.get('pass') is not True for c in physical['checks']) or
                physical.get('metrics', {}).get('csv_sha256') != sha(folder/'result.csv')):
            raise TerminalFailure('UNVERIFIED_PHYSICAL_REPORT')


def run_round(number):
    packet, bundle, contract = approved_inputs()
    manifest = round_manifest(number, packet)
    batch = Batch(OUT, packet['specification_digest'])
    require_complete_round(batch)
    directory = OUT/f'round-{number:02d}'
    if directory.exists(): raise ValueError('ROUND_DIRECTORY_ALREADY_EXISTS')
    lock, token = OUT/'.execution.lock', f'{os.getpid()}:{uuid.uuid4().hex}'
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try: os.write(fd, token.encode('ascii'))
    finally: os.close(fd)
    owns = False
    report = {'round':number, 'status':'PENDING', 'local':[], 'scenes':[], 'sysml':'NOT_RUN',
        'specification_digest':packet['specification_digest'], 'physical_digest':packet['physical_digest'],
        'proposal_digest':packet['bundle_digest'], 'C_complete':False, 'online':0,
        'identity_probes':0, 'retry_eligible':False}
    try:
        directory.mkdir(exist_ok=False); owns = True
        guard_sources(packet, manifest)
        previous.snapshot_inputs(directory, packet, bundle, contract, manifest)
        snapshot_readonly(directory, packet)
        dependency_audit(directory)
        for index, target in enumerate(local_targets(), 1):
            guard_sources(packet, manifest)
            batch.reserve('local', target, number)
            proc = batch.run([sys.executable, '-B', '-m', 'unittest', target], ROOT.parent,
                             BUDGET['new_local_timeout_s'], directory, f'local-{index:02d}')
            status = 'PASS' if proc['returncode'] == 0 and not proc['timed_out'] else 'FAIL'
            report['local'].append({'test':target, 'status':status})
            if proc['timed_out']: raise TerminalFailure('LOCAL_TOOL_TIMEOUT')
            if status != 'PASS': raise ImplementationFailure('NEW_LOCAL_CHECK_FAILURE')
        guard_sources(packet, manifest)
        try: generated = revision.generate(bundle, contract, CASE_ORDER[0], packet['specification_digest'])
        except Exception as exc: raise ImplementationFailure('NEW_GENERATOR_INTERFACE') from exc
        with (directory/'vehicle.sysml').open('x',encoding='utf-8',newline='\n') as stream:
            stream.write(generated['sysml'])
        save(directory/'sysml-trace.json', generated['trace'])
        batch.reserve('sysml', 'revised_vehicle_source', number)
        proc = batch.run([sys.executable, '-B', '-m', 'week12.scripts.s3_c_execute',
            '--parse-sysml', str(directory/'vehicle.sysml'), '--parse-output', str(directory/'sysml-result.json')],
            ROOT.parent, BUDGET['sysml_parse_timeout_s'], directory, 'sysml')
        if proc['timed_out']: raise TerminalFailure('SYSML_TOOL_TIMEOUT')
        if not (directory/'sysml-result.json').is_file():
            raise TerminalFailure('UNKNOWN_SYSML_TOOL_FAILURE')
        parsed = read(directory/'sysml-result.json')
        if parsed.get('tool_version') != '0.34.1': raise TerminalFailure('SYSML_DEPENDENCY_DRIFT')
        report['sysml'] = parsed.get('status', 'UNVERIFIED')
        if report['sysml'].upper() != 'PASS':
            kind = str(parsed.get('failure_kind', '')).lower()
            reason = parsed.get('evidence', {}).get('reason')
            if kind == 'content' and reason in ('sysml_syntax_error', 'sysml_minimum_structure_missing'):
                raise ImplementationFailure('NEW_SYSML_INTERFACE')
            raise TerminalFailure('UNKNOWN_SYSML_CLASSIFICATION')
        if proc['returncode'] != 0: raise TerminalFailure('UNKNOWN_SYSML_EXIT_FAILURE')
        for case in CASE_ORDER:
            base = None
            for refined in (False, True):
                guard_sources(packet, manifest)
                # Preflight source generation isolates provable adapter errors
                # before a scene reservation or an external process is started.
                try: revision.generate(bundle, contract, case, packet['specification_digest'])
                except Exception as exc: raise ImplementationFailure('NEW_GENERATOR_INTERFACE') from exc
                row, folder = execute_scene(batch, directory, bundle, contract, case, refined,
                                            number, manifest, packet, base)
                report['scenes'].append(row)
                classify_scene(row, folder)
                guard_sources(packet, manifest)
                if not refined: base = folder
        report['status'] = 'PASS_DIMENSION_SMALL_SCENES_C_INCOMPLETE'
    except BaseException as exc:
        report.update(status='STOPPED', error=type(exc).__name__+': '+str(exc),
                      retry_eligible=isinstance(exc, ImplementationFailure),
                      failure_code=str(exc) if isinstance(exc, ImplementationFailure) else 'TERMINAL')
    finally:
        try:
            report['usage'] = batch.usage()
            if report['usage']['unresolved_processes']:
                report.update(status='STOPPED', retry_eligible=False, stop_reason='UNRESOLVED_PROCESS')
            if report['usage']['process_seconds'] >= BUDGET['execution_total_wall_s']:
                report.update(status='STOPPED', retry_eligible=False, stop_reason='BUDGET_EXHAUSTED')
            if report['retry_eligible']:
                try: require_complete_round(batch)
                except ValueError:
                    report.update(retry_eligible=False, stop_reason='NO_COMPLETE_REPAIR_ROUND_BUDGET')
            report['not_run'] = [case+('-refined' if refined else '-base') for case,refined in SCENES
                if not any(r['case'] == case and r['variant'] == ('refined' if refined else 'base')
                           for r in report['scenes'])]
            if owns:
                try: guard_sources(packet, manifest); report['source_guard_end'] = 'PASS'
                except Exception as exc:
                    report.update(status='STOPPED', retry_eligible=False, source_guard_end='FAIL',
                                  source_guard_error=type(exc).__name__+': '+str(exc))
                save(directory/'batch-result.json', report)
                if report['status'] == 'PASS_DIMENSION_SMALL_SCENES_C_INCOMPLETE' or not report['retry_eligible'] or number == 3:
                    final = deepcopy(report)
                    final.update(unused_budget_disposition='RETIRED', automatic_additional_scene_permitted=False)
                    save(OUT/'final-result.json', final)
        finally:
            if lock.is_file() and lock.read_text(encoding='ascii') == token: lock.unlink()
    return report
