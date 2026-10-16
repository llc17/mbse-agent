"""Approved active-cluster route: one persistent ledger, no implicit retries.

Reuses only the pinned toolchain's bounded child runner and parser. Historical
vehicle executors, receipts, paused preparation tests and closed budgets are
never imported. A successful tool is not a successful vehicle.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sys
import time

PROJECT = Path(__file__).resolve().parents[3]
V11 = PROJECT / 'week12'
DEFAULT_OUT = V11 / 'results/s3-c-active-eightwheel-execution-20261009-01'
SPEC = V11 / 'results/s3-c-open-configuration-proposal-20261009-01/proposal-final.json'
EXPECTED_SPEC = 'ec45de70bd6ffc114c5a9670c0db5000f5ee131a4144f6949fd5df7215c627ac'
ORDER = ('active_joint_energy', 'one_twin_module_load_cycle',
         'flat_energy_and_brake', 'three_up', 'three_forward_down',
         'twenty_up', 'twenty_forward_down')
KINDS = {'scene': ('scene_attempts', 42), 'parse': ('parse_attempts', 3),
         'other': ('other_checks', 12)}


def read(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def save(path, value):
    path = Path(path)
    temporary = path.with_name(path.name + '.writing')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2,
                                   allow_nan=False) + '\n', encoding='utf-8')
    temporary.replace(path)


def stamp():
    return datetime.now(timezone.utc).isoformat()


class Stop(RuntimeError):
    pass


def authorized(directory=DEFAULT_OUT):
    directory = Path(directory).resolve()
    if directory != DEFAULT_OUT.resolve():
        raise Stop('ONE_NAMED_ROUTE_ONLY_NO_DIRECTORY_BUDGET_RESET')
    receipt = read(directory / 'authorization.json')
    if (receipt.get('status') != 'APPROVED_BY_USER' or
            receipt.get('user_message_verbatim') != '方案已确认继续' or
            receipt.get('proposal_sha256') != EXPECTED_SPEC or sha(SPEC) != EXPECTED_SPEC):
        raise Stop('EXTERNAL_APPROVAL_OR_SPEC_DRIFT')
    spec = read(SPEC)
    if receipt['budget'] != spec['execution_contract_proposal']:
        raise Stop('APPROVED_FINITE_CONTRACT_DRIFT')
    if receipt['git_HEAD'] != 'ab8f0e3f2e35b661f89d62415ed61773be91b145':
        raise Stop('PROJECT_BASELINE_DRIFT')
    return receipt, spec


class Route:
    def __init__(self, directory=DEFAULT_OUT):
        self.directory = Path(directory).resolve()
        self.receipt, self.spec = authorized(self.directory)
        self.path = self.directory / 'ledger.json'
        self.refresh()

    def refresh(self):
        self.data = read(self.path)
        if (self.data['proposal_sha256'] != EXPECTED_SPEC or
                self.data['approval_sha256'] != sha(self.directory / 'authorization.json')):
            raise Stop('LEDGER_APPROVAL_BINDING_DRIFT')
        return self.data

    def persist(self):
        self.data['last_checkpoint_utc'] = stamp()
        save(self.path, self.data)

    def elapsed(self):
        intervals=self.data.get('active_intervals')
        if intervals:
            if intervals[0]['started_utc'] != self.data['started_utc']:
                raise Stop('INITIAL_ACTIVE_TIME_ORIGIN_DRIFT')
            total=0.0;previous=None;now=datetime.now(timezone.utc)
            for row in intervals:
                begin=datetime.fromisoformat(row['started_utc'].replace('Z','+00:00'))
                end=(datetime.fromisoformat(row['ended_utc'].replace('Z','+00:00'))
                     if row.get('ended_utc') else now)
                if end<begin or end>now or (previous and begin<previous):
                    raise Stop('ACTIVE_TIME_INTERVAL_INVALID')
                total+=(end-begin).total_seconds();previous=end
            return total
        start = datetime.fromisoformat(self.data['started_utc'].replace('Z', '+00:00'))
        return (datetime.now(timezone.utc) - start).total_seconds()

    def process_used(self):
        leaf = sum(float(p.get('duration_s', 0)) for k, _ in KINDS.values()
                   for r in self.data[k] for p in r.get('processes', [])
                   if not p.get('dispatcher_overhead'))
        overhead = sum(float(p.get('duration_s', 0)) for r in self.data['other_checks']
                       for p in r.get('processes', []) if p.get('dispatcher_overhead'))
        for r in self.data['other_checks']:
            if r.get('status') == 'DISPATCHING':
                start = datetime.fromisoformat(r['started_utc'].replace('Z','+00:00'))
                parent_elapsed = (datetime.now(timezone.utc)-start).total_seconds()
                overhead += max(0,parent_elapsed-(leaf-r['leaf_wall_before']))
        return float(self.data.get('process_time_uncertainty_s', 0)) + leaf + overhead

    def kind_used(self, kind, round_no=None):
        key, _ = KINDS[kind]
        return sum(float(p.get('duration_s', 0)) for r in self.data[key]
                   if round_no is None or r.get('round', 1) == round_no
                   for p in r.get('processes', []))

    def assert_ready(self):
        self.refresh()
        if self.data['status'].startswith(('CLOSED', 'STOPPED')):
            raise Stop('ROUTE_ALREADY_STOPPED_NO_AUTOMATIC_REOPEN')
        if self.elapsed() >= self.data['future_cap_s']:
            raise Stop('TOTAL_ACTIVE_ROUTE_TIME_EXHAUSTED')
        if self.process_used() >= self.data['process_cap_s']:
            raise Stop('ALL_CHILD_PROCESS_TIME_EXHAUSTED')
        if max(0, self.elapsed() - self.process_used()) >= self.data['implementation_analysis_cap_s']:
            raise Stop('IMPLEMENTATION_ANALYSIS_TIME_EXHAUSTED')
        for key, _ in KINDS.values():
            if any(r.get('status') == 'RUNNING' for r in self.data[key]):
                raise Stop('UNCERTAIN_PRIOR_LAUNCH_BLOCKS_RESUME')

    def reserve(self, kind, label, wall, round_no=1):
        self.assert_ready()
        key, limit = KINDS[kind]
        if kind == 'other':
            limit = self.auxiliary_limit()
        if len(self.data[key]) >= limit or not 1 <= round_no <= self.source_round_limit():
            raise Stop('FINITE_ATTEMPT_LIMIT')
        if wall > self.data['process_cap_s'] - self.process_used():
            raise Stop('INSUFFICIENT_FULL_OPERATION_TIME_RESERVE')
        if wall > self.data['future_cap_s'] - self.elapsed():
            raise Stop('INSUFFICIENT_ROUTE_WALL_RESERVE')
        contract = self.spec['execution_contract_proposal']
        if kind == 'parse' and wall > contract['parse_each_cap_s']:
            raise Stop('PARSE_SINGLE_ATTEMPT_CAP')
        if kind == 'scene':
            scene = label.removesuffix('-refined').removesuffix('-base')
            cap = next((r['wall_each_s'] for r in contract['scene_caps']
                        if r['name'] == scene), None)
            if cap is None or wall > cap:
                raise Stop('SCENE_SINGLE_ATTEMPT_CAP')
        kind_cap = (contract['all_scene_set_cap_s'] if kind == 'scene' else
                    contract['other_process_checks_reserved_s'] if kind == 'other' else
                    contract['parse_each_cap_s'] * contract['SysML_parse_attempts'])
        if wall > kind_cap - self.kind_used(kind, round_no if kind == 'scene' else None):
            raise Stop('KIND_PROCESS_TIME_RESERVE_EXHAUSTED')
        row = {'ordinal': len(self.data[key]) + 1, 'kind': kind, 'label': label,
               'round': round_no, 'status': 'RUNNING', 'reserved_wall_s': wall,
               'started_utc': stamp(), 'processes': []}
        self.data[key].append(row)
        self.persist()
        return key, row['ordinal'] - 1

    def auxiliary_limit(self):
        """Only the user's separately bound addendum extends the original12."""
        if self.final_correction_limits():
            return 17
        receipt_path=self.directory/'auxiliary-cap-addendum-authorization.json'
        if not receipt_path.exists():
            if self.data.get('other_check_cap',12) != 12:
                raise Stop('AUXILIARY_LIMIT_WITHOUT_EXTERNAL_APPROVAL')
            return 12
        receipt=read(receipt_path)
        proposal_path=self.directory/'auxiliary-cap-addendum-proposal.json'
        proposal=read(proposal_path)
        change={'other_external_diagnostic_check_attempt_cap':{'from':12,'to':15}}
        if (receipt.get('status') != 'APPROVED_BY_USER' or
                receipt.get('user_message_verbatim') != '确认继续' or
                receipt.get('only_change') != change or
                proposal.get('only_requested_change') != change or
                receipt.get('approved_proposal_sha256') != sha(proposal_path) or
                receipt.get('original_authorization_sha256') != self.data['approval_sha256'] or
                receipt.get('physical_proposal_sha256') != EXPECTED_SPEC or
                receipt.get('unchanged') != proposal.get('unchanged') or
                receipt.get('base_ledger_checkpoint_sha256') != sha(self.directory/'ledger-before-addendum.json') or
                self.data.get('auxiliary_addendum_sha256') != sha(receipt_path) or
                self.data.get('other_check_cap') != 15):
            raise Stop('AUXILIARY_ADDENDUM_BINDING_DRIFT')
        old=read(self.directory/'ledger-before-addendum.json')
        if (old['started_utc'] != self.data['started_utc'] or
                self.data['active_intervals'][0] != old['active_intervals'][0] or
                any(self.data[field] != old[field] for field in
                    ('future_cap_s','implementation_analysis_cap_s','process_cap_s',
                     'scene_cap','parse_cap','repair_rounds_cap'))):
            raise Stop('ADDENDUM_CANNOT_RESET_OR_EXPAND_OTHER_BUDGETS')
        return 15

    def final_correction_limits(self):
        """Validate the final user receipt, without resetting any old history."""
        receipt_path = self.directory/'final-correction-authorization.json'
        if not receipt_path.exists():
            if self.data.get('repair_rounds_cap', 2) != 2:
                raise Stop('FINAL_REPAIR_LIMIT_WITHOUT_EXTERNAL_APPROVAL')
            return False
        receipt = read(receipt_path)
        proposal_path = self.directory/'final-correction-budget-proposal.json'
        proposal = read(proposal_path)
        old_path = self.directory/'ledger-before-final-correction.json'
        old = read(old_path)
        expected_changes = proposal.get('only_requested_changes', {})
        if (receipt.get('status') != 'APPROVED_BY_USER' or
                receipt.get('user_message_verbatim') != '继续' or
                receipt.get('approved_proposal_sha256') != sha(proposal_path) or
                receipt.get('physical_spec_sha256') != EXPECTED_SPEC or
                receipt.get('original_authorization_sha256') != self.data['approval_sha256'] or
                receipt.get('previous_auxiliary_authorization_sha256') !=
                    sha(self.directory/'auxiliary-cap-addendum-authorization.json') or
                receipt.get('base_ledger_sha256') != sha(old_path) or
                receipt.get('only_changes') != expected_changes or
                expected_changes.get('accepted_implementation_repairs') != {'from':2,'to':3} or
                expected_changes.get('auxiliary_external_attempts') != {'from':15,'to':17} or
                expected_changes.get('source_revision_round', {}).get('from') != 3 or
                expected_changes.get('source_revision_round', {}).get('to') != 4 or
                receipt.get('unchanged') != proposal.get('unchanged') or
                self.data.get('final_correction_authorization_sha256') != sha(receipt_path) or
                self.data.get('other_check_cap') != 17 or
                self.data.get('repair_rounds_cap') != 3 or
                self.data.get('source_revision_round_cap') != 4):
            raise Stop('FINAL_CORRECTION_APPROVAL_BINDING_DRIFT')
        for field in ('started_utc','future_cap_s','implementation_analysis_cap_s',
                      'process_cap_s','scene_cap','parse_cap','prepare_actual_s','prepare_reserved_s'):
            if self.data.get(field) != old.get(field):
                raise Stop('FINAL_CORRECTION_CANNOT_RESET_OR_EXPAND_TIME')
        for key in ('scene_attempts','parse_attempts','other_checks','repairs','source_bindings'):
            if self.data.get(key, [])[:len(old.get(key, []))] != old.get(key, []):
                raise Stop('FINAL_CORRECTION_HISTORICAL_ROWS_DRIFT')
        if self.data['active_intervals'][:len(old['active_intervals'])] != old['active_intervals']:
            raise Stop('FINAL_CORRECTION_ACTIVE_TIME_HISTORY_DRIFT')
        return True

    def repair_limit(self):
        return 3 if self.final_correction_limits() else 2

    def source_round_limit(self):
        return 4 if self.final_correction_limits() else 3

    def child(self, slot, command, cwd, wall, logs, label):
        """Persist intention, bound and account actual returned cleanup time."""
        from week12.src.toolchain import _run
        self.refresh()
        key, index = slot
        row = self.data[key][index]
        if row['status'] != 'RUNNING':
            raise Stop('RESERVATION_NOT_RUNNING')
        remaining = self.data['process_cap_s'] - self.process_used()
        if not math.isfinite(wall) or wall <= 5 or remaining <= 5:
            raise Stop('NO_BOUNDED_CHILD_AND_CLEANUP_RESERVE')
        entry = {'label': label, 'command': list(map(str, command)), 'cwd': str(cwd),
                 'status': 'LAUNCH_REQUESTED', 'started_utc': stamp(),
                 'timeout_s': min(wall, remaining) - 5}
        row['processes'].append(entry)
        self.persist()
        result = _run(entry['command'], Path(cwd), entry['timeout_s'])
        Path(logs).mkdir(exist_ok=True, parents=True)
        for stream in ('stdout', 'stderr'):
            (Path(logs) / (label + '.' + stream + '.log')).write_text(
                result[stream], encoding='utf-8')
        self.refresh()
        entry = self.data[key][index]['processes'][-1]
        entry.update({k: v for k, v in result.items() if k not in ('stdout', 'stderr')})
        entry['status'] = 'UNCERTAIN' if result['uncertain'] else 'RETURNED'
        entry['ended_utc'] = stamp()
        self.persist()
        if result['uncertain'] or result['interrupted']:
            raise Stop('CHILD_CLEANUP_UNCERTAIN_OR_INTERRUPTED')
        return result

    def finish(self, slot, status, **evidence):
        self.refresh()
        key, index = slot
        self.data[key][index].update(status=status, ended_utc=stamp(), **evidence)
        self.persist()

    def source_bind(self, paths, generated, round_no=1):
        self.assert_ready()
        if round_no > 1:
            if len(self.data['repairs']) != round_no - 1:
                raise Stop('REPAIR_REQUIRES_RECORDED_IMPLEMENTATION_EVIDENCE')
            scene_total = self.spec['execution_contract_proposal']['all_scene_set_cap_s']
            if (42 - len(self.data['scene_attempts']) < 14 or
                    self.data['process_cap_s'] - self.process_used() < scene_total or
                    self.data['future_cap_s'] - self.elapsed() < scene_total):
                raise Stop('NO_COMPLETE_AFFECTED_SET_RESERVED')
        rows = []
        for path in sorted(set(Path(p).resolve() for p in paths), key=str):
            path.relative_to(PROJECT.resolve())
            relative = path.relative_to(PROJECT.resolve()).as_posix().lower()
            if path.name.lower() == '.env' or relative == 'week9/experiments/cases.py':
                raise Stop('CREDENTIAL_SOURCE_FORBIDDEN')
            if not (path in {p.resolve() for p in source_paths()} or
                    path.is_relative_to(self.directory.resolve())):
                raise Stop('SOURCE_OUTSIDE_ACTIVE_ADAPTER_ALLOWLIST')
            rows.append({'path': path.relative_to(PROJECT).as_posix(), 'sha256': sha(path)})
        binding = {'round': round_no, 'proposal_sha256': EXPECTED_SPEC,
                   'files': rows, 'generated': generated, 'created_utc': stamp()}
        self.data['source_bindings'].append(binding)
        self.persist()
        return binding

    def assert_sources(self, binding):
        if any(sha(PROJECT / r['path']) != r['sha256'] for r in binding['files']):
            raise Stop('SOURCE_DRIFT_DURING_BOUND_EXECUTION')

    def terminal(self, reason, status='STOPPED'):
        self.refresh()
        self.data.update(status=status, stop_reason=reason)
        self.persist()

    def begin_dispatch(self, label, round_no):
        """Count launcher once; account only parent time outside nested children."""
        slot = self.reserve('other', label, 60, round_no)
        key,index=slot
        self.data[key][index].update(status='DISPATCHING',
            leaf_wall_before=sum(float(p.get('duration_s',0)) for k,_ in KINDS.values()
                for r in self.data[k] for p in r.get('processes',[]) if not p.get('dispatcher_overhead')))
        self.persist()
        return slot,time.monotonic()

    def end_dispatch(self, dispatch, status):
        slot,started=dispatch;self.refresh();key,index=slot;r=self.data[key][index]
        leaf=sum(float(p.get('duration_s',0)) for k,_ in KINDS.values()
                 for row in self.data[k] for p in row.get('processes',[]) if not p.get('dispatcher_overhead'))
        overhead=max(0,time.monotonic()-started-(leaf-r['leaf_wall_before']))
        r['processes'].append({'label':'dispatcher overhead excluding all child process wall',
                              'duration_s':overhead,'dispatcher_overhead':True})
        r.update(status=status,ended_utc=stamp(),parent_overhead_s=overhead)
        if overhead>r['reserved_wall_s']:
            self.data.update(status='STOPPED_DISPATCH_OVERHEAD',stop_reason='finite dispatcher overhead exceeded')
        self.persist()

    def accept_implementation_repair(self, evidence_path):
        """Explicit reviewed proof, never automatic reinterpretation of failure."""
        self.refresh()
        evidence_path = Path(evidence_path).resolve()
        evidence_path.relative_to(self.directory)
        evidence = read(evidence_path)
        permitted = {'COMPILE_PROCESS_FAILURE', 'MODEL_CHECK_FAILED',
                     'SYSML_SYNTAX_NOT_VERIFIED', 'PROVEN_CHECKER_IMPLEMENTATION_DEFECT'}
        if (self.data['status'] != 'STOPPED_AFFECTED_EXECUTION' or
                evidence.get('classification') != 'PROVEN_IMPLEMENTATION_DEFECT' or
                evidence.get('root_review_status') != 'ACCEPTED' or
                evidence.get('proposal_sha256') != EXPECTED_SPEC or
                evidence.get('failed_operation') not in permitted or
                not evidence.get('specific_defect') or not evidence.get('proof_files') or
                evidence.get('unchanged_physical_control_task_attestation') is not True):
            raise Stop('NO_REVIEWED_IMPLEMENTATION_REPAIR_AUTHORITY')
        if len(self.data['repairs']) >= self.repair_limit():
            raise Stop('IMPLEMENTATION_REPAIR_ROUNDS_EXHAUSTED')
        for key, _ in KINDS.values():
            if any(r.get('status') == 'RUNNING' or any(p.get('uncertain') for p in r.get('processes', []))
                   for r in self.data[key]):
                raise Stop('UNRESOLVED_PRIOR_CHILD_PREVENTS_REPAIR')
        for proof in evidence['proof_files']:
            path = (self.directory / proof['path']).resolve()
            path.relative_to(self.directory)
            if sha(path) != proof['sha256']:
                raise Stop('IMPLEMENTATION_PROOF_DRIFT')
        reserve = self.spec['execution_contract_proposal']['all_scene_set_cap_s']
        if (len(self.data['scene_attempts']) + 14 > 42 or
                self.process_used() + reserve > self.data['process_cap_s'] or
                self.elapsed() + reserve > self.data['future_cap_s'] or
                max(0,self.elapsed()-self.process_used()) >= self.data['implementation_analysis_cap_s']):
            raise Stop('NO_FULL_REPAIR_SET_TIME_RESERVE')
        record = {'round':len(self.data['repairs'])+2, 'evidence_path':str(evidence_path),
                  'evidence_sha256':sha(evidence_path), 'accepted_utc':stamp(),
                  'full_scene_time_reserved_s':reserve, 'old_stop_reason':self.data.get('stop_reason')}
        self.data['repairs'].append(record)
        self.data['status'] = 'REPAIR_IMPLEMENTATION_BOUND'
        self.data['gates'] = {n:'NOT_RUN' for n in ORDER}
        self.persist()
        return record


def source_paths():
    # New owned directory only; no historical results or private candidate scan.
    paths = list((V11 / 'src/active_cluster').glob('*.py'))
    paths += [V11 / 'scripts/s3_c_active_cluster.py', V11 / 'src/toolchain.py',
              V11 / 'scripts/s3_c_vehicle_recovery.py',
              V11 / 'scripts/s3_c_execution_remediation.py',
              V11 / 'tests/test_active_cluster_run_policy.py',
              V11 / 'tests/test_active_cluster_run_scope.py',
              V11 / 'tests/test_active_cluster_vehicle_recovery.py',
              V11 / 'tests/test_active_cluster_frontend.py',
              V11 / 'tests/test_active_cluster_execution.py',
              V11 / 'tests/test_active_cluster_generation.py',
              V11 / 'tests/test_active_cluster_verification.py',
              V11 / 'models/components/V11PhysicalB1/package.mo',
              V11 / 'models/components/V11ContactC2/package.mo',
              V11 / 'models/components/V11ActiveClusterC3/package.mo']
    return paths


def prepare(route, round_no=1):
    from .generator import generate
    from .verify import verify_generated
    out = route.directory / f'generated-round-{round_no:02d}'
    if out.exists():
        raise Stop('GENERATED_DIRECTORY_EXISTS_NO_OVERWRITE')
    manifest = generate(SPEC, out)
    design_path = Path(manifest['design_path'])
    if not design_path.is_absolute():
        design_path = out / design_path
    graph = read(design_path)
    review = verify_generated(graph, manifest, route.spec)
    save(out / 'independent-generation-review.json', review)
    if review.get('status') != 'PASS':
        raise Stop('GENERATION_CONSISTENCY_NOT_VERIFIED')
    for required in ('sysml_path', 'package_path', 'model_class_by_scene', 'output_contract'):
        if required not in manifest:
            raise Stop('GENERATOR_INTERFACE_MISSING:' + required)
    for name in ORDER:
        if name not in manifest['model_class_by_scene']:
            raise Stop('SCENE_CLASS_NOT_REGISTERED:' + name)
    save(out / 'manifest.json', manifest)
    paths = source_paths() + [p for p in out.iterdir() if p.is_file()]
    binding = route.source_bind(paths, {'directory': str(out), 'manifest': str(out/'manifest.json'),
                                       'graph': str(design_path)}, round_no)
    save(out / 'source-binding.json', binding)
    return manifest, graph, binding


def build_mos(model_class, package, stop, output_interval, refined, variable_filter='.*',
              preflight_classes=()):
    from week12.src.component_physics.runner import COMPLEX, SERVICES, MSL, OMC
    if not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*', model_class):
        raise Stop('INVALID_MODEL_CLASS')
    paths = [COMPLEX, SERVICES, MSL, V11/'models/components/V11PhysicalB1/package.mo',
             V11/'models/components/V11ContactC2/package.mo',
             V11/'models/components/V11ActiveClusterC3/package.mo', Path(package)]
    if any('"' in p.as_posix() for p in paths) or '"' in variable_filter:
        raise Stop('INVALID_MOS_STRING')
    lines = []
    for i, path in enumerate(paths):
        lines += [f'ok := loadFile("{path.as_posix()}");',
                  f'print("__AC_LOAD_{i}__="+String(ok)+"\\n");',
                  'print(getErrorString()+"\\n");', 'if not ok then exit(1); end if;']
    lines += ['print("__AC_OMC_VERSION__="+getVersion()+"\\n");',
              'print("__AC_MSL_VERSION__="+getVersion(Modelica)+"\\n");',
              'print("__AC_MSL_SOURCE__="+getSourceFile(Modelica)+"\\n");']
    if preflight_classes:
        if len(preflight_classes) != 7 or len(set(preflight_classes)) != 7:
            raise Stop('ALL_SEVEN_DISTINCT_FRONTEND_CLASSES_REQUIRED')
        lines += ['frontendOK := true;']
        for i, cls in enumerate(preflight_classes):
            if not re.fullmatch(r'[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*', cls):
                raise Stop('INVALID_FRONTEND_CLASS')
            lines += [f'frontcheck := checkModel({cls});', 'frontError := getErrorString();',
                      f'print("__AC_FRONTEND_{i}__="+frontcheck+"\\n");',
                      'print(frontError+"\\n");',
                      'frontendOK := frontendOK and regexBool(frontcheck,"completed successfully") and not regexBool(frontError,"Error:");']
        lines += ['print("__AC_ALL_SEVEN_FRONTEND__="+String(frontendOK)+"\\n");',
                  'if not frontendOK then exit(1); end if;']
    lines += [
              f'check := checkModel({model_class});',
              'print("__AC_CHECK__="+check+"\\n");',
              'print(getErrorString()+"\\n");',
              'if not regexBool(check,"completed successfully") then exit(1); end if;',
              f'build := buildModel({model_class},startTime=0,stopTime={stop},'
              f'numberOfIntervals={math.ceil(stop/output_interval)},tolerance={1e-7 if refined else 1e-6},'
              f'method="dassl",outputFormat="csv",fileNamePrefix="model",variableFilter="{variable_filter}");',
              'buildError := getErrorString();', 'print(buildError+"\\n");',
              'print("__AC_BUILD_EXE__="+build[1]+"\\n");']
    return '\n'.join(lines) + '\n'


def parse_current_sysml(route, path, round_no=1):
    slot = route.reserve('parse', 'current-generated-sysml', 30, round_no)
    logs = route.directory / f'parse-round-{round_no:02d}'
    command = [sys.executable, '-B', '-m', 'week12.scripts.s3_c_active_cluster',
               'parse-child', '--source', str(path), '--report', str(logs/'parse.json')]
    try:
        result = route.child(slot, command, PROJECT, 30, logs, 'parse')
        report = read(logs/'parse.json') if (logs/'parse.json').exists() else {'status': 'MISSING'}
        status = 'PASS' if result['returncode'] == 0 and report['status'] == 'PASS' else 'FAIL'
        route.finish(slot, status, report=report)
        if status != 'PASS':
            raise Stop('SYSML_SYNTAX_NOT_VERIFIED')
        return report
    except Exception as exc:
        route.finish(slot, 'FAIL', error=str(exc))
        raise


def scene_stop(spec, name):
    times = spec['execution_contract_proposal']['stop_times_s']
    if name == ORDER[0]: return times['joint_energy']
    if name == ORDER[1]: return times['one_twin_module_load_cycle']
    if name == ORDER[2]: return times['flat']
    return times['three_steps'] if name.startswith('three') else times['twenty_steps']


def execute_scene(route, manifest, graph, binding, name, refined=False, round_no=1, base=None):
    from week12.src.toolchain import _marker, _resolve_generated
    from week12.src.component_physics.runner import OMC, MSL
    route.assert_sources(binding)
    reuse_frontend = False
    if binding.get('frontend_reuse'):
        validator = getattr(route, 'frontend_reusable', None)
        if not callable(validator) or validator(binding, manifest) is not True:
            raise Stop('UNVERIFIED_FRONTEND_REUSE')
        reuse_frontend = True
    wall = next(x['wall_each_s'] for x in route.spec['execution_contract_proposal']['scene_caps'] if x['name']==name)
    wall = getattr(route, 'scene_wall', lambda n, w: w)(name, wall)
    label = name + ('-refined' if refined else '-base')
    slot = route.reserve('scene', label, wall, round_no)
    directory = (route.scene_directory(name, refined, round_no) if hasattr(route, 'scene_directory')
                 else route.directory / f'round-{round_no:02d}' / label)
    directory.mkdir(parents=True, exist_ok=False)
    started = time.monotonic()
    deadline = started + wall
    result = {'scene': name, 'variant': 'refined' if refined else 'base', 'status': 'FAIL',
              'compile': 'NOT_RUN', 'simulation': 'NOT_RUN', 'physical': 'NOT_RUN',
              'numeric': 'NOT_RUN', 'proposal_sha256': EXPECTED_SPEC, 'source_binding': binding}
    try:
        interval = .01 if refined else .02
        package = Path(manifest['package_path'])
        if not package.is_absolute(): package = Path(binding['generated']['directory']) / package
        output_contract = manifest['output_contract']
        variable_filter = manifest.get('variable_filter', output_contract.get('variable_filter', '.*'))
        if variable_filter == '.*':
            raise Stop('EXPLICIT_BOUNDED_OUTPUT_CONTRACT_REQUIRED')
        stop = scene_stop(route.spec, name)
        script=build_mos(manifest['model_class_by_scene'][name],
            package, stop, interval, refined, variable_filter,
            tuple(manifest['model_class_by_scene'][n] for n in ORDER)
                if round_no == 4 and name == ORDER[0] and not refined
                and not reuse_frontend else ())
        script=getattr(route,'instrument_build',lambda text:text)(script)
        (directory/'build.mos').write_text(script,encoding='utf-8')
        compile_budget=getattr(route,'scene_budget',lambda n:(wall,wall))(name)[0]
        result['compile']='ATTEMPTED'
        save(directory/'scene-result.json',result)
        build = route.child(slot, [str(OMC), 'build.mos'], directory,
                            min(compile_budget,deadline-time.monotonic()), directory, 'build')
        result['compile'] = 'ATTEMPTED'
        if round_no == 4 and name == ORDER[0] and not refined and not reuse_frontend:
            result['all_seven_frontend'] = {
                'status': 'PASS' if _marker(build['stdout'], '__AC_ALL_SEVEN_FRONTEND__') == 'true' else 'FAIL',
                'classes': manifest['model_class_by_scene'],
                'check_texts': [_marker(build['stdout'], f'__AC_FRONTEND_{i}__') for i in range(7)],
                'method': 'One no-build checkModel prefix in first component compiler process; component build only after all seven checks PASS.'}
            if result['all_seven_frontend']['status'] != 'PASS':
                raise Stop('ALL_SEVEN_FRONTEND_NOT_VERIFIED_NO_FURTHER_REPAIR')
        if build['timed_out']: raise Stop('COMPILE_TIMEOUT')
        if build['returncode'] != 0: raise Stop('COMPILE_PROCESS_FAILURE')
        stdout = build['stdout']
        result['check_model_text'] = _marker(stdout, '__AC_CHECK__')
        if 'completed successfully' not in result['check_model_text'].lower():
            raise Stop('MODEL_CHECK_FAILED')
        result['actual_omc_version'] = _marker(stdout, '__AC_OMC_VERSION__')
        if not re.search(r'(?<!\d)1\.25\.7(?!\d)', result['actual_omc_version']):
            raise Stop('PINNED_OMC_IDENTITY_DRIFT')
        if (_marker(stdout, '__AC_MSL_VERSION__') != '4.0.0' or
                Path(_marker(stdout, '__AC_MSL_SOURCE__').strip('"')).resolve() != MSL.resolve()):
            raise Stop('PINNED_MSL_IDENTITY_DRIFT')
        exe = _resolve_generated(directory, _marker(stdout, '__AC_BUILD_EXE__'))
        if exe is None: raise Stop('NO_REAL_GENERATED_EXECUTABLE')
        result['compile'] = 'PASS'
        if hasattr(route,'scene_budget'):
            deadline=min(deadline,time.monotonic()+route.scene_budget(name)[1])
        route.assert_sources(binding)
        result['simulation'] = 'LAUNCH_REQUESTED'
        save(directory/'scene-result.json', result)
        sim = route.child(slot, [str(exe), '-s=dassl', '-r=result.csv'], directory,
                          deadline-time.monotonic(), directory, 'simulation')
        if sim['returncode'] != 0 or sim['timed_out']: raise Stop('SIMULATION_FAILED_OR_TIMEOUT')
        result['simulation'] = 'COMPLETED'
        command = [sys.executable, '-B', '-m', 'week12.scripts.s3_c_active_cluster', 'verify-child',
                   '--source', str(directory/'result.csv'), '--manifest', binding['generated']['manifest'],
                   '--graph', binding['generated']['graph'], '--scene', name,
                   '--report', str(directory/'independent-result.json')]
        if base: command += ['--base', str(base/'result.csv')]
        if hasattr(route, 'verification_command'):
            command = route.verification_command(directory, binding['generated']['manifest'],
                binding['generated']['graph'], name, base)
        verified = route.child(slot, command, PROJECT, deadline-time.monotonic(), directory, 'verification')
        result['verification_returncode'] = verified['returncode']
        result['verification_timed_out'] = verified['timed_out']
        result['verification_uncertain'] = verified['uncertain']
        route.assert_sources(binding)
        report = read(directory/'independent-result.json') if (directory/'independent-result.json').exists() else {}
        result['physical'] = report.get('physical', {}).get('status', 'MISSING_EVIDENCE')
        result['numeric'] = report.get('numeric', {}).get('status', 'NOT_RUN')
        if hasattr(route, 'verification_command'):
            from week12.src.car_domain.outcomes import scene_classification
            result['classification'] = scene_classification(result, report)
            if result['classification'] != 'PASS':
                raise Stop(result['classification'])
        elif verified['returncode'] != 0 or verified['timed_out'] or result['physical'] != 'PASS':
            raise Stop('PHYSICAL_OR_MISSING_EVIDENCE_NO_IMPLEMENTATION_RETRY')
        if refined and result['numeric'] != 'PASS': raise Stop('NUMERICAL_UNTRUSTED')
        if time.monotonic() > deadline: raise Stop('COMPLETE_SCENE_WALL_LIMIT_EXCEEDED')
        result['status'] = 'PASS'
    except Exception as exc:
        result['error'] = str(exc)
        process_file=directory/'build-process.json'
        if result['compile']=='ATTEMPTED' and process_file.exists():
            process=read(process_file)
            result['compile']='TIMEOUT' if process.get('timed_out') else 'FAILED'
            result['classification']='TOOL_FAIL'
        progress=directory/'build-progress.log'
        if progress.exists():result['compile_phase_progress']=progress.read_text(encoding='utf-8').splitlines()
    finally:
        result['scene_active_wall_s'] = time.monotonic()-started
        save(directory/'scene-result.json', result)
        route.finish(slot, result['status'], scene_result=str(directory/'scene-result.json'),
                     error=result.get('error', ''), scene_active_wall_s=result['scene_active_wall_s'])
    return result, directory


def run_bound_round(route, round_no=1, maximum_gate=None):
    binding = next((x for x in reversed(route.data['source_bindings']) if x['round']==round_no), None)
    if not binding: raise Stop('NO_INDEPENDENTLY_CHECKED_GENERATION_BINDING')
    manifest = read(binding['generated']['manifest'])
    graph = read(binding['generated']['graph'])
    route.assert_sources(binding)
    sysml = Path(manifest['sysml_path'])
    if not sysml.is_absolute(): sysml = Path(binding['generated']['directory']) / sysml
    route.refresh()
    parsed = [r for r in route.data['parse_attempts']
              if r.get('round') == round_no and r.get('status') == 'PASS']
    if not parsed and not getattr(route, 'reuse_sysml', lambda *_: False)(sysml, binding):
        try:
            parse_current_sysml(route, sysml, round_no)
        except Exception as exc:
            route.terminal(str(exc), 'STOPPED_AFFECTED_EXECUTION')
            raise
    results = []
    for name in ORDER:
        route.refresh()
        if route.data['gates'][name] == 'PASS':
            # Only source-bound results from this round can be continued.
            done = [r for r in route.data['scene_attempts'] if r.get('round') == round_no
                    and r.get('label') in (name+'-base', name+'-refined')
                    and r.get('status') == 'PASS']
            if len(done) != 2:
                raise Stop('GATE_NOT_BOUND_TO_CURRENT_ROUND')
            if name == maximum_gate: break
            continue
        # The actual two-direction three-step gates are prerequisites of either20.
        if name.startswith('twenty'):
            route.refresh()
            if any(route.data['gates'][n] != 'PASS' for n in ORDER[:5]):
                raise Stop('SMALL_BOTH_DIRECTIONS_NOT_CREDIBLE_NO_TWENTY')
        base = None
        for refined in (False, True):
            result, folder = execute_scene(route, manifest, graph, binding, name, refined, round_no, base)
            results.append(result)
            if result['status'] != 'PASS':
                route.terminal(result.get('error', 'FAILED_SCENE'), 'STOPPED_AFFECTED_EXECUTION')
                save(route.directory/'latest-result.json', {'status':'STOPPED', 'results':results})
                return results
            if not refined: base = folder
        route.refresh()
        route.data['gates'][name] = 'PASS'
        route.persist()
        if name == maximum_gate: break
    save(route.directory/'latest-result.json', {'status':'GATES_VERIFIED', 'results':results,
                                               'whole_V11_complete':False})
    return results
