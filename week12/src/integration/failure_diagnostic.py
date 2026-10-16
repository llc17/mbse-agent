"""A new, receipt-bound numerical diagnostic of one unchanged failed vehicle.

Import and prepare are source/evidence-only.  Vehicle FAIL remains FAIL; this
module never calls the acceptance comparator with invented PASS reports.
"""
from copy import deepcopy
import ast
import math
import os
from pathlib import Path
import sys
import unittest
import uuid

from . import vehicle_amendment as vehicle
from . import vehicle_amendment_execution as execution
from .execution import Batch, read, save, sha, require_sources, dependency_audit
from .review import check_review_receipt
from .vehicle_verify import _read_csv, _interp, _relative
from ..composition.schema import digest

ROOT = vehicle.ROOT
REVIEW = ROOT/'results/s3-c-failure-diagnostic-review-20261005-01'
OUT = ROOT/'results/s3-c-failure-diagnostic-execution-20261005-01'
OLD = vehicle.OUT
OLD_ROUND = OLD/'round-02'
BASE = OLD_ROUND/'fixed_nose_three_up-base'
CASE = 'fixed_nose_three_up'
PHYSICAL_SPEC = 'c38f520f238587dab8fa558281f178905154c9b51668bcbc45246f7cbe8f8df7'
NEW_PATHS = ('src/integration/failure_diagnostic.py',
             'scripts/s3_c_failure_diagnostic.py', 'tests/test_c_failure_diagnostic.py')
BUDGET = {'scene_attempts_per_complete_round':1, 'maximum_rounds':3,
          'maximum_scene_attempts':3, 'new_local_execution_checks_limit':12,
          'new_local_timeout_s':10, 'sysml_parse_limit':0, 'identity_probe_limit':0,
          'product_online_calls':0, 'execution_total_wall_s':1500,
          'build_timeout_s':180, 'simulation_timeout_s':180,
          'postprocess_timeout_s':60}
NUMERICS = {'solver':'dassl', 'tolerance':1e-7, 'max_step_s':.0005,
            'output_interval_s':.001}
FINAL_CLASSIFICATIONS = ('FAILURE_REPRODUCED_NUMERICALLY', 'NUMERICAL_SENSITIVITY',
                         'TASK_OUTCOME_CHANGED')


def _bound_files(paths):
    return [{'path':str(Path(p).resolve().relative_to(ROOT.resolve())),
             'sha256':sha(p)} for p in paths]


def _old_sources():
    rows = read(OLD_ROUND/'source-manifest.json')
    if len(rows) != 21: raise ValueError('EXACT_ACCEPTED_21_SOURCE_MANIFEST_REQUIRED')
    require_sources(rows)
    return rows


def _check_reuse():
    final, prior = read(OLD/'final-result.json'), read(OLD_ROUND/'batch-result.json')
    if (final['status'] != 'STOPPED_PHYSICAL_TASK_FAILURE_C_INCOMPLETE' or
            final['specification_digest'] != PHYSICAL_SPEC or
            final['usage']['unresolved_processes'] != 0 or
            len(prior['local']) != 8 or any(r['status'] != 'PASS' for r in prior['local']) or
            prior['sysml'] != 'PASS'):
        raise ValueError('CLOSED_UNCHANGED_ACCEPTED_PREFLIGHT_EVIDENCE_REQUIRED')
    report, result = read(BASE/'physical.json'), read(BASE/'result.json')
    if (report['status'] != 'FAIL' or report['metrics']['case_id'] != CASE or
            report['metrics']['csv_sha256'] != sha(BASE/'result.csv') or
            report.get('diagnostics') or report.get('issues') or
            result['compile'] != 'PASS' or result['simulation'] != 'COMPLETED' or
            result['physical'] != 'FAIL'):
        raise ValueError('COMPLETE_KNOWN_BASE_FAILURE_REQUIRED')
    return report


def prepare():
    """Create only the new review directory, never an approval or execution dir."""
    packet, bundle, contract = vehicle.inputs()
    if packet['specification_digest'] != PHYSICAL_SPEC:
        raise ValueError('UNCHANGED_C2_PHYSICAL_SPEC_REQUIRED')
    execution.guard_sources(packet, _old_sources())
    _check_reuse()
    case = next(c for c in bundle['physical']['cases'] if c['id'] == CASE)
    if case['stop_time_s'] != 35 or contract['numerical']['refined'] != NUMERICS:
        raise ValueError('FROZEN_35S_REFINED_NUMERICS_REQUIRED')
    # AST syntax checking deliberately does not import/run any new test.
    for name in NEW_PATHS: ast.parse((ROOT/name).read_text(encoding='utf-8'))
    reuse = _bound_files([OLD/'final-result.json', OLD/'authorization.json',
                         OLD_ROUND/'batch-result.json', OLD_ROUND/'source-manifest.json',
                         OLD_ROUND/'sysml-result.json', OLD_ROUND/'dependency-audit.json',
                         *[BASE/name for name in ('result.csv', 'physical.json',
                            'result.json', 'input-identity.json', 'build.mos',
                            'simulation-launch.json', 'event-codegen.json', 'owner-audit.json')]])
    identity = {'scope':'v11_s3_c_same_physical_three_up_failure_diagnostic',
                'physical_specification_digest':PHYSICAL_SPEC,
                'vehicle_bundle_digest':packet['bundle_digest'],
                'vehicle_verification_digest':packet['verification_digest'],
                'case_order':[CASE], 'variant':'refined', 'stop_time_s':35,
                'numerics':deepcopy(NUMERICS),
                'base_numerics':deepcopy(contract['numerical']['base']),
                'numerical_comparison':deepcopy(contract['numerical_comparison']),
                'budget':deepcopy(BUDGET), 'reused_evidence':reuse,
                'fixed_inputs':deepcopy(packet['fixed_inputs']),
                'fixed_implementation_manifest':_old_sources(),
                'implementation_manifest':[*_old_sources(),
                    *[{'path':str((ROOT/n).resolve()), 'sha256':sha(ROOT/n)} for n in NEW_PATHS]],
                'expected_base_failed_checks':sorted(c['name'] for c in
                    read(BASE/'physical.json')['checks'] if c['pass'] is False),
                'no_prior_budget_carry_over':True,
                'old_closed_batch_never_reopened':True,
                'postprocess_timeout_is_shared_between_verification_and_comparison':True,
                'physical_FAIL_remains_FAIL':True,
                'repair_boundary':'new three adapter files only; same physics, diagnostic identity, frozen inputs and complete round budget',
                'completed_diagnostic_retires_unused_budget':True}
    identity['specification_digest'] = digest(identity)
    REVIEW.mkdir(exist_ok=False)
    save(REVIEW/'diagnostic-review-packet.json', identity)
    save(REVIEW/'vehicle-confirmation-identity.json', packet)
    save(REVIEW/'proposal.json', bundle)
    save(REVIEW/'verification-contract.json', contract)
    save(REVIEW/'preparation.json', {'status':'SOURCE_ONLY_PREPARED_NOT_AUTHORIZED',
         'new_tests_run':0, 'new_simulations':0, 'new_sysml':0, 'online':0,
         'reused_local_pass':8, 'reused_sysml_pass':1})
    return identity


def inputs():
    identity = read(REVIEW/'diagnostic-review-packet.json')
    raw = dict(identity); value = raw.pop('specification_digest')
    if digest(raw) != value: raise ValueError('DIAGNOSTIC_IDENTITY_DRIFT')
    packet, bundle, contract = vehicle.inputs()
    if (packet['specification_digest'] != PHYSICAL_SPEC or
            read(REVIEW/'vehicle-confirmation-identity.json') != packet or
            read(REVIEW/'proposal.json') != bundle or
            read(REVIEW/'verification-contract.json') != contract or
            identity['physical_specification_digest'] != PHYSICAL_SPEC or
            identity['vehicle_bundle_digest'] != digest(bundle) or
            identity['vehicle_verification_digest'] != digest(contract) or
            identity['numerical_comparison'] != contract['numerical_comparison'] or
            identity['numerics'] != contract['numerical']['refined'] or
            identity['base_numerics'] != contract['numerical']['base']):
        raise ValueError('UNCHANGED_VEHICLE_BINDING_REQUIRED')
    validate_budget(identity)
    guard_sources(identity, packet, identity['implementation_manifest'], allow_new_drift=True)
    return identity, packet, bundle, contract


def validate_budget(identity):
    if identity.get('budget') != BUDGET or any(type(v) is not int for v in identity['budget'].values()):
        raise ValueError('EXACT_FINITE_DIAGNOSTIC_BUDGET_REQUIRED')
    if (identity.get('case_order') != [CASE] or identity.get('variant') != 'refined' or
            identity.get('stop_time_s') != 35 or identity.get('numerics') != NUMERICS):
        raise ValueError('EXACT_SINGLE_REFINED_DIAGNOSTIC_REQUIRED')
    return deepcopy(BUDGET)


def guard_sources(identity, vehicle_packet, manifest, *, allow_new_drift=False):
    expected = identity['implementation_manifest']
    if execution._manifest_paths(manifest) != execution._manifest_paths(expected):
        raise ValueError('COMPLETE_SAME_PATH_MANIFEST_REQUIRED')
    fixed = identity['fixed_implementation_manifest']
    actual = {str(Path(r['path']).resolve()):r['sha256'] for r in manifest}
    if any(actual[str(Path(r['path']).resolve())] != r['sha256'] for r in fixed):
        raise ValueError('FROZEN_OLD_IMPLEMENTATION_CANNOT_BE_REPAIRED')
    execution.guard_sources(vehicle_packet, fixed if allow_new_drift else manifest)
    if identity['fixed_inputs'] != vehicle_packet['fixed_inputs']:
        raise ValueError('FIXED_INPUT_MANIFEST_DRIFT')
    for row in identity['reused_evidence']:
        if sha(ROOT/row['path']) != row['sha256']: raise ValueError('REUSED_BASE_OR_PREFLIGHT_DRIFT')
    _check_reuse()


def initialize(receipt_path):
    path = Path(receipt_path)
    if not path.is_absolute() or path.name == '.env':
        raise ValueError('EXPLICIT_EXTERNAL_RECEIPT_PATH_REQUIRED')
    path = path.resolve(strict=True)
    identity, packet, bundle, contract = inputs()
    receipt = read(path)
    check_review_receipt(receipt, identity)
    guard_sources(identity, packet, identity['implementation_manifest'])
    OUT.mkdir(exist_ok=False)
    save(OUT/'authorization.json', receipt)
    save(OUT/'authorization-source.json', {'path':str(path), 'sha256':sha(path),
                                          'receipt_not_created_by_runner':True})
    save(OUT/'approved-diagnostic.json', identity)
    save(OUT/'policy.json', {'budget':deepcopy(BUDGET),
         'specification_digest':identity['specification_digest'],
         'physical_specification_digest':PHYSICAL_SPEC,
         'old_batch_closed':True, 'physical_FAIL_remains_FAIL':True})
    return receipt


def approved_inputs():
    if (OUT/'final-result.json').exists(): raise ValueError('CLOSED_DIAGNOSTIC_CANNOT_RUN')
    identity, packet, bundle, contract = inputs()
    check_review_receipt(read(OUT/'authorization.json'), identity)
    policy = read(OUT/'policy.json')
    if (read(OUT/'approved-diagnostic.json') != identity or policy['budget'] != BUDGET or
            policy['specification_digest'] != identity['specification_digest'] or
            policy['physical_specification_digest'] != PHYSICAL_SPEC or
            policy.get('old_batch_closed') is not True or
            policy.get('physical_FAIL_remains_FAIL') is not True):
        raise ValueError('APPROVAL_OR_POLICY_DRIFT')
    source = read(OUT/'authorization-source.json')
    if sha(source['path']) != source['sha256']: raise ValueError('EXTERNAL_RECEIPT_DRIFT')
    return identity, packet, bundle, contract


def round_manifest(number, identity, packet):
    if type(number) is not int or not 1 <= number <= 3: raise ValueError('ROUND_LIMIT')
    manifest = identity['implementation_manifest']
    if number > 1:
        previous = OUT/f'round-{number-1:02d}'
        result, repair = read(previous/'batch-result.json'), read(previous/'diagnosis.json')
        if (result['status'] != 'STOPPED' or repair.get('classification') != 'implementation' or
                not isinstance(repair.get('reason'), str) or not repair['reason'].strip() or
                result['diagnostic_digest'] != identity['specification_digest'] or
                repair.get('diagnostic_digest') != identity['specification_digest']):
            raise ValueError('SAME_DIAGNOSTIC_IMPLEMENTATION_DIAGNOSIS_REQUIRED')
        manifest = repair['rebound_implementation_manifest']
        old = read(previous/'source-manifest.json')
        if digest(manifest) == digest(old): raise ValueError('UNCHANGED_BLIND_RETRY_FORBIDDEN')
    guard_sources(identity, packet, manifest)
    return manifest


def require_complete_round(batch):
    used = batch.usage()
    if (used['unresolved_processes'] or used['scene']+1 > BUDGET['maximum_scene_attempts'] or
            used['local']+4 > BUDGET['new_local_execution_checks_limit'] or
            used['sysml'] or used['probe'] or
            used['process_seconds']+500 > BUDGET['execution_total_wall_s']):
        raise ValueError('NO_COMPLETE_DIAGNOSTIC_ROUND_BUDGET_OR_UNRESOLVED_PROCESS')


def local_targets():
    def flatten(suite):
        for item in suite:
            if isinstance(item, unittest.TestSuite): yield from flatten(item)
            else: yield item.id()
    names = list(flatten(unittest.defaultTestLoader.loadTestsFromName('week12.tests.test_c_failure_diagnostic')))
    if len(names) != 4 or len(set(names)) != 4 or any('_FailedTest' in n for n in names):
        raise ValueError('EXACT_FOUR_DIAGNOSTIC_GUARD_TESTS_REQUIRED')
    return names


def compare_failure(contract, base_csv, refined_csv, base_report, refined_report):
    """Compare real failed reports without changing their acceptance statuses."""
    checks = []
    result = {'classification':'INCOMPLETE', 'vehicle_acceptance':'NOT_GRANTED',
              'base_physical_status':base_report.get('status'),
              'refined_physical_status':refined_report.get('status'), 'checks':checks}
    try:
        for path, report in ((base_csv, base_report), (refined_csv, refined_report)):
            if (report.get('status') not in ('PASS', 'FAIL') or report.get('diagnostics') or
                    report.get('issues') or report['metrics']['case_id'] != CASE or
                    report['metrics']['csv_sha256'] != sha(path)):
                raise ValueError('REPORT_INCOMPLETE_OR_CSV_HASH_MISMATCH')
            if not report.get('checks') or any(type(c.get('pass')) is not bool for c in report['checks']):
                raise ValueError('PHYSICAL_CHECK_EVIDENCE_REQUIRED')
            if (report['status'] == 'PASS') != all(c['pass'] for c in report['checks']):
                raise ValueError('PHYSICAL_STATUS_CHECKS_MISMATCH')
        rules = contract['numerical_comparison']
        a, b = base_report['metrics'], refined_report['metrics']
        failed_a = sorted(c['name'] for c in base_report['checks'] if not c['pass'])
        failed_b = sorted(c['name'] for c in refined_report['checks'] if not c['pass'])
        if (set(c['name'] for c in base_report['checks']) !=
                set(c['name'] for c in refined_report['checks'])):
            raise ValueError('PHYSICAL_CHECK_SET_CHANGED')
        result.update(base_failed_checks=failed_a, refined_failed_checks=failed_b)
        for key in ('signals', 'event_times', 'cumulative'):
            if set(a[key]) != set(b[key]):
                checks.append({'name':'same_'+key+'_set', 'pass':False,
                               'base':sorted(a[key]), 'refined':sorted(b[key])})
        if not a['signals'] or not a['cumulative']:
            raise ValueError('MEASURED_SIGNALS_AND_ENERGIES_REQUIRED')
        for metrics in (a, b):
            values = [v for signal in metrics['signals'].values() for v in signal.values()]
            values += list(metrics['event_times'].values())+list(metrics['cumulative'].values())
            if any(type(v) not in (int, float) or not math.isfinite(v) for v in values):
                raise ValueError('FINITE_MEASURED_METRICS_REQUIRED')
        def floor(name):
            return rules['position_abs_m'] if name.endswith(('_x', '_z')) else (
                rules['pitch_abs_rad'] if name.endswith('_pitch') else (
                rules['wheel_speed_abs_rad_s'] if name.endswith('_omega') else rules['velocity_abs_m_s']))
        for name in sorted(set(a['signals']) & set(b['signals'])):
            for statistic in (('peak_abs',) if name.endswith('_normal') else ('peak_abs', 'terminal')):
                _relative(checks, name+'_'+statistic, a['signals'][name][statistic],
                    b['signals'][name][statistic],
                    rules['peak_contact_force_relative'] if name.endswith('_normal') else rules['state_relative'],
                    rules['peak_contact_force_abs_N'] if name.endswith('_normal') else floor(name))
        for name in sorted(set(a['event_times']) & set(b['event_times'])):
            _relative(checks, name+'_event_time', a['event_times'][name], b['event_times'][name],
                      0., rules['event_time_abs_s'])
        for name in sorted(set(a['cumulative']) & set(b['cumulative'])):
            _relative(checks, name+'_energy', a['cumulative'][name], b['cumulative'][name],
                      rules['energy_relative'], rules['energy_abs_J'])
        names = set(a['signals']) | set(b['signals'])
        data = [_read_csv(Path(p), {'time', *names}, 35., .001) for p in (base_csv, refined_csv)]
        for name in sorted(names):
            if name.endswith('_normal'): continue
            worst, error = 0., 0.
            for t in sorted(set(data[0].time) | set(data[1].time)):
                av, bv = (_interp(d, name, t) for d in data)
                limit = max(floor(name), rules['state_relative']*max(abs(av), abs(bv)))
                delta = abs(av-bv)
                error, worst = max(error, delta), max(worst, delta/limit)
            checks.append({'name':name+'_aligned_state', 'max_error':error,
                           'max_tolerance_ratio':worst, 'pass':worst <= 1})
        if any(not math.isfinite(c.get('max_tolerance_ratio', 0)) for c in checks):
            raise ValueError('NONFINITE_COMPARISON')
        result['classification'] = ('TASK_OUTCOME_CHANGED' if failed_a != failed_b or
            base_report['status'] != refined_report['status'] else
            'NUMERICAL_SENSITIVITY' if not all(c['pass'] for c in checks) else
            'FAILURE_REPRODUCED_NUMERICALLY' if base_report['status'] == 'FAIL' else
            'TASK_OUTCOME_CHANGED')
    except (OSError, KeyError, TypeError, ValueError, ArithmeticError) as exc:
        result['error'] = type(exc).__name__+': '+str(exc)
    return result


def _completed_verification(batch, row, folder):
    processes = sorted(batch.root.glob('process-*.result.json'))
    if not processes: raise ValueError('VERIFIER_PROCESS_RECORD_REQUIRED')
    last = read(processes[-1])
    physical = read(folder/'physical.json')
    expected = (1, 'RuntimeError: PHYSICAL_NUMERIC_OR_POSTPROCESS_FAILURE') if physical['status'] == 'FAIL' else (
                0, 'RuntimeError: INCOMPLETE_PHYSICAL_OR_NUMERIC_EVIDENCE')
    if (last.get('label') != 'verification' or last.get('returncode') != expected[0] or
            last.get('timed_out') or last.get('uncertain') or last.get('interrupted') or
            row.get('error') != expected[1] or row.get('compile') != 'PASS' or
            row.get('simulation') != 'COMPLETED' or row.get('physical') != physical['status'] or
            physical.get('diagnostics') or physical.get('issues') or
            (folder/'verification.stderr.log').read_text(encoding='utf-8').strip()):
        raise ValueError('UNEXPECTED_TOOL_OR_PHYSICAL_EVIDENCE_FAILURE')
    return last['duration_s']


def run_round(number):
    identity, packet, bundle, contract = approved_inputs()
    manifest = round_manifest(number, identity, packet)
    batch = Batch(OUT, identity['specification_digest'])
    require_complete_round(batch)
    directory = OUT/f'round-{number:02d}'
    if directory.exists(): raise ValueError('ROUND_DIRECTORY_ALREADY_EXISTS')
    lock, token = OUT/'.execution.lock', f'{os.getpid()}:{uuid.uuid4().hex}'
    fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    try: os.write(fd, token.encode('ascii'))
    finally: os.close(fd)
    owns = False
    report = {'round':number, 'status':'PENDING', 'local':[], 'scenes':[],
              'diagnostic_digest':identity['specification_digest'],
              'physical_specification_digest':PHYSICAL_SPEC,
              'vehicle_acceptance':'NOT_GRANTED', 'sysml':0, 'online':0, 'identity_probes':0}
    try:
        directory.mkdir(exist_ok=False); owns = True
        guard_sources(identity, packet, manifest)
        execution.snapshot_inputs(directory, packet, bundle, contract, manifest)
        save(directory/'diagnostic-identity.json', identity)
        dependency_audit(directory)
        for index, target in enumerate(local_targets(), 1):
            guard_sources(identity, packet, manifest)
            batch.reserve('local', target, number)
            proc = batch.run([sys.executable, '-B', '-m', 'unittest', target], ROOT.parent,
                             10, directory, f'local-{index:02d}')
            status = 'PASS' if proc['returncode'] == 0 and not proc['timed_out'] else 'FAIL'
            report['local'].append({'test':target, 'status':status})
            if status != 'PASS': raise RuntimeError('FIRST_LOCAL_FAILURE_REQUIRES_DIAGNOSIS')
        guard_sources(identity, packet, manifest)
        row, folder = execution.execute_scene(batch, directory, bundle, contract, CASE,
                                             True, number, manifest, packet, base=None)
        report['scenes'].append(row)
        # execute_scene exits early for a task FAIL: the parent ALWAYS guards it.
        guard_sources(identity, packet, manifest)
        used_post = _completed_verification(batch, row, folder)
        remaining_post = 60-used_post
        if remaining_post <= 0: raise RuntimeError('SHARED_POSTPROCESS_BUDGET_EXHAUSTED')
        proc = batch.run([sys.executable, '-B', '-m', 'week12.scripts.s3_c_failure_diagnostic',
                          '--compare', str(folder)], ROOT.parent, remaining_post, folder, 'failure-comparison')
        if proc['returncode'] != 0 or proc['timed_out']:
            raise RuntimeError('DIAGNOSTIC_COMPARATOR_TOOL_FAILURE')
        comparison = read(folder/'failure-diagnostic.json')
        report['classification'] = comparison['classification']
        if report['classification'] not in FINAL_CLASSIFICATIONS:
            raise RuntimeError('DIAGNOSTIC_INCOMPLETE_REQUIRES_DIAGNOSIS')
        report['status'] = 'DIAGNOSTIC_COMPLETED_VEHICLE_NOT_ACCEPTED'
    except BaseException as exc:
        report.update(status='STOPPED', error=type(exc).__name__+': '+str(exc))
    finally:
        try:
            report['usage'] = batch.usage()
            if owns:
                try:
                    guard_sources(identity, packet, manifest)
                    report['source_guard_end'] = 'PASS'
                except Exception as exc:
                    report.update(status='STOPPED', source_guard_end='FAIL',
                                  source_guard_error=type(exc).__name__+': '+str(exc))
                save(directory/'batch-result.json', report)
                if report['status'] == 'DIAGNOSTIC_COMPLETED_VEHICLE_NOT_ACCEPTED' or number == 3:
                    final = deepcopy(report)
                    final.update(unused_budget_disposition='RETIRED',
                                 automatic_additional_scene_permitted=False, C_complete=False)
                    save(OUT/'final-result.json', final)
        finally:
            if lock.is_file() and lock.read_text(encoding='ascii') == token: lock.unlink()
    return report


def compare_folder(folder):
    folder = Path(folder).resolve(strict=True)
    identity, packet, _, contract = approved_inputs()
    if folder.name != CASE+'-refined' or folder.parent.parent != OUT.resolve():
        raise ValueError('OWNED_EXACT_DIAGNOSTIC_FOLDER_REQUIRED')
    # This CLI mode is a bounded runner child, not a second unmetered entry.
    if not (OUT/'.execution.lock').is_file():
        raise ValueError('ACTIVE_PARENT_DIAGNOSTIC_LOCK_REQUIRED')
    starts = sorted(OUT.glob('process-*.start.json'))
    if not starts or read(starts[-1]).get('label') != 'failure-comparison':
        raise ValueError('METERED_COMPARISON_PROCESS_REQUIRED')
    if starts[-1].with_name(starts[-1].name.replace('.start.json', '.result.json')).exists():
        raise ValueError('COMPARISON_PROCESS_ALREADY_FINISHED')
    manifest = read(folder.parent/'source-manifest.json')
    guard_sources(identity, packet, manifest)
    result = compare_failure(contract, BASE/'result.csv', folder/'result.csv',
                             read(BASE/'physical.json'), read(folder/'physical.json'))
    guard_sources(identity, packet, manifest)
    save(folder/'failure-diagnostic.json', result)
    return result
