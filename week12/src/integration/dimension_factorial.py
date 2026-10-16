"""Two missing numerical corners; no tool starts at import or preparation.

Frozen vehicle generation, verification and comparison are reused unchanged.
Summary LOG_STATS telemetry is not an adaptive integration-step history.
"""
from copy import deepcopy
import ast
import math
import os
from pathlib import Path
import sys
from types import FunctionType
import uuid
import xml.etree.ElementTree as ET

from . import dimension_reference as reference
from .execution import Batch, read, save, sha, require_sources, dependency_audit
from .review import check_review_receipt
from ..composition.schema import digest

prior = reference.prior
execution, previous, comparator = prior.execution, prior.previous, prior.comparator
ROOT = reference.ROOT
REVIEW = ROOT/'results/s3-c-dimension-factorial-review-20261006-01'
OUT = ROOT/'results/s3-c-dimension-factorial-execution-20261006-01'
CASE = reference.CASE
BASE = reference.BASE
STRICT = reference.OUT/'round-01'/(CASE+'-refined')
PHYSICAL_SPEC, PHYSICAL_DIGEST = reference.PHYSICAL_SPEC, reference.PHYSICAL_DIGEST
SCOPE = 'v11_s3_c_same_dimension_numerical_factor_separation'
NEW_PATHS = ('src/integration/dimension_factorial.py',
             'scripts/s3_c_dimension_factorial.py', 'tests/test_c_dimension_factorial.py')
CLI = 'week12.scripts.s3_c_dimension_factorial'
STATS_FLAG = '-lv=LOG_STATS'
CORNER_ORDER = ('tolerance_only', 'step_only')
NUMERICS = {
    'tolerance_only': {'solver':'dassl', 'tolerance':1e-8, 'max_step_s':.0005, 'output_interval_s':.001},
    'step_only': {'solver':'dassl', 'tolerance':1e-7, 'max_step_s':.00025, 'output_interval_s':.001}}
ENDPOINT_NUMERICS = {'A_existing':deepcopy(prior.NUMERICS), 'D_existing':deepcopy(reference.NUMERICS)}
EDGE_FACTORS = {'tolerance_only': {'A_existing':'tolerance', 'D_existing':'max_step_s'},
                'step_only': {'A_existing':'max_step_s', 'D_existing':'tolerance'}}
BUDGET = {'scene_attempts_per_complete_round':2, 'maximum_rounds':3,
          'maximum_scene_attempts':6, 'new_local_execution_checks_limit':12,
          'new_local_timeout_s':10, 'sysml_parse_limit':0, 'identity_probe_limit':0,
          'product_online_calls':0, 'execution_total_wall_s':3000,
          'build_timeout_s':180, 'simulation_timeout_s':180, 'postprocess_timeout_s':90}
FINAL_CLASSIFICATIONS, EXPECTED_FAILED = prior.FINAL_CLASSIFICATIONS, prior.EXPECTED_FAILED
REPAIR_CODES, ImplementationFailure = prior.REPAIR_CODES, prior.ImplementationFailure
compare_failure, _completed_verification = prior.compare_failure, prior._completed_verification
_failed_checks = prior._failed_checks


def _reuse(name):
    fn = getattr(prior,name)
    return FunctionType(fn.__code__,globals(),fn.__name__,fn.__defaults__,fn.__closure__)


def _original_inputs():
    old_identity, packet, bundle, strict = reference.inputs()
    original = read(reference.REVIEW/'original-contract.json')
    if strict != reference.effective_contract(original):
        raise ValueError('EXACT_ORIGINAL_AND_STRICT_CONTRACT_REQUIRED')
    return old_identity, packet, bundle, original


def effective_contract(original, corner):
    if corner not in CORNER_ORDER or original['numerical']['refined'] != prior.NUMERICS:
        raise ValueError('EXACT_FROZEN_CORNER_AND_ORIGINAL_NUMERICS_REQUIRED')
    effective = deepcopy(original)
    effective['numerical']['refined'] = deepcopy(NUMERICS[corner])
    return effective


def _fixed_sources(old_identity, packet):
    rows = read(reference.OUT/'round-01/source-manifest.json')
    if len(rows) != 33 or rows != old_identity['implementation_manifest']:
        raise ValueError('EXACT_PREVIOUS_33_SOURCES_REQUIRED')
    reference.guard_sources(old_identity,packet,rows)
    return rows


def _archive_paths():
    """Use fixed completed manifests, never enumerate unrelated results."""
    paths = []
    for root in (prior.OUT, reference.OUT):
        manifest = root/'closeout-manifest.json'
        paths.append(manifest)
        rows = read(manifest)['files']
        names = set()
        for row in rows:
            path = (root/row['path']).resolve(strict=True)
            path.relative_to(root.resolve())
            if path.name == '.env' or path in names or sha(path) != row['sha256']:
                raise ValueError('FROZEN_COMPLETED_ARCHIVE_DRIFT')
            names.add(path); paths.append(path)
    return paths


def _check_reuse():
    reference._check_reuse()
    final = read(reference.OUT/'final-result.json')
    previous_result = read(reference.OUT/'round-01/batch-result.json')
    diagonal = read(STRICT/'failure-diagnostic.json')
    if ({k:v for k,v in final.items() if k not in ('unused_budget_disposition',
            'automatic_additional_scene_permitted')} != previous_result or
            final['status'] != 'DIAGNOSTIC_COMPLETED_VEHICLE_NOT_ACCEPTED' or
            final['retry_eligible'] is not False or final['source_guard_end'] != 'PASS' or
            final['unused_budget_disposition'] != 'RETIRED' or
            final['automatic_additional_scene_permitted'] is not False or
            final['usage']['unresolved_processes'] != 0 or final['usage']['scene'] != 1 or
            final['usage']['local'] != 4 or final['usage']['sysml'] or final['usage']['probe'] or
            final['online'] or final['identity_probes'] or len(final['local']) != 4 or
            any(r['status'] != 'PASS' for r in final['local']) or
            diagonal['classification'] != 'NUMERICAL_SENSITIVITY' or len(diagonal['checks']) != 182 or
            sum(c['pass'] is True for c in diagonal['checks']) != 172 or
            diagonal['vehicle_acceptance'] != 'NOT_GRANTED'):
        raise ValueError('EXACT_CLOSED_STRICT_REFERENCE_REQUIRED')
    for folder, numerics in ((BASE,prior.NUMERICS), (STRICT,reference.NUMERICS)):
        physical, scene = read(folder/'physical.json'), read(folder/'result.json')
        if (_failed_checks(physical) != EXPECTED_FAILED or
                physical['metrics']['csv_sha256'] != sha(folder/'result.csv') or
                any(scene[k] != v for k,v in (('compile','PASS'),('simulation','COMPLETED'),
                    ('physical','FAIL'),('event_codegen','PASS'),('owner_audit','PASS')))):
            raise ValueError('EXACT_FIVE_KNOWN_FAILURE_ENDPOINTS_REQUIRED')
        xml = list(ET.parse(folder/'model_init.xml').getroot().iter('DefaultExperiment'))
        expected = {'startTime':0.,'stopTime':35.,'stepSize':.001,'tolerance':numerics['tolerance']}
        records = [read(p) for p in folder.parent.parent.glob('process-*.start.json')]
        launches = [r for r in records if r.get('label') == 'simulation' and
                    Path(r.get('cwd','')).resolve() == folder.resolve()]
        if (len(xml) != 1 or any(float(xml[0].get(k,'nan')) != v for k,v in expected.items()) or
                xml[0].get('solver') != 'dassl' or xml[0].get('outputFormat') != 'csv' or
                read(folder/'simulation-launch.json')['max_step_s'] != numerics['max_step_s'] or
                len(launches) != 1 or launches[0]['command'][1:] !=
                ['-s=dassl',f'-maxStepSize={numerics["max_step_s"]}','-r=result.csv']):
            raise ValueError('FROZEN_ENDPOINT_ACTUAL_NUMERICS_DRIFT')
    actual = read(STRICT/'actual-numerics.json')
    if (actual['numerics'] != reference.NUMERICS or actual['init_xml_sha256'] != sha(STRICT/'model_init.xml') or
            actual['launch_command'] != launches[0]['command'] or
            previous_result['scenes'][0]['numerics'] != reference.NUMERICS):
        raise ValueError('STRICT_ENDPOINT_RECORDED_ACTUAL_BINDING_DRIFT')


def validate_budget(identity):
    if (identity.get('scope') != SCOPE or identity.get('budget') != BUDGET or
            any(type(v) is not int for v in identity['budget'].values()) or
            identity.get('case_order') != [CASE] or identity.get('variant') != 'refined' or
            identity.get('stop_time_s') != 35 or identity.get('corner_order') != list(CORNER_ORDER) or
            identity.get('numerics') != NUMERICS or identity.get('endpoint_numerics') != ENDPOINT_NUMERICS or
            identity.get('edge_factors') != EDGE_FACTORS or identity.get('stats_flag') != STATS_FLAG):
        raise ValueError('EXACT_FIXED_TWO_CORNER_FINITE_DIAGNOSTIC_REQUIRED')
    return deepcopy(BUDGET)


def guard_sources(identity, packet, manifest, *, allow_new_drift=False):
    old_identity, old_packet, _, original = _original_inputs()
    fixed = _fixed_sources(old_identity,packet)
    expected = previous._manifest_paths(fixed) | {str((ROOT/p).resolve()) for p in NEW_PATHS}
    if (packet != old_packet or identity['fixed_implementation_manifest'] != fixed or len(expected) != 36 or
            previous._manifest_paths(manifest) != expected or
            previous._manifest_paths(identity['implementation_manifest']) != expected):
        raise ValueError('EXACT_FIXED33_PLUS_NEW3_SOURCE_BINDING_REQUIRED')
    actual = execution._maps(manifest)
    if any(actual[str(Path(r['path']).resolve())] != r['sha256'] for r in fixed):
        raise ValueError('OLD_33_SOURCES_CANNOT_BE_REPAIRED')
    reference.guard_sources(old_identity,packet,fixed)
    require_sources(fixed if allow_new_drift else manifest)
    if (identity['fixed_inputs'] != packet['fixed_inputs'] or identity['readonly_manifest'] != packet['readonly_manifest'] or
            identity['vehicle_verification_digest'] != digest(original) or
            identity['effective_verification_digests'] != {c:digest(effective_contract(original,c)) for c in CORNER_ORDER}):
        raise ValueError('FIXED_INPUT_OR_ORIGINAL_EFFECTIVE_CONTRACT_DRIFT')
    for row in [*identity['reused_evidence'],*identity['review_evidence']]:
        if sha(ROOT/row['path']) != row['sha256']: raise ValueError('REUSED_OR_REVIEW_EVIDENCE_DRIFT')
    _check_reuse()


def build_script(model_class, stop, variable_filter, refined, corner):
    if refined is not True or stop != 35 or corner not in CORNER_ORDER:
        raise ValueError('EXACT_FACTORIAL_BUILD_REQUIRED')
    script = previous.build_script(model_class,stop,variable_filter,True)
    old = f'tolerance={prior.NUMERICS["tolerance"]:.17g}'
    new = f'tolerance={NUMERICS[corner]["tolerance"]:.17g}'
    if script.count(old) != 1: raise ValueError('EXACT_ONE_OLD_BUILD_TOLERANCE_LITERAL_REQUIRED')
    return script.replace(old,new,1)


def _generated(packet, bundle, effective, corner):
    _, _, _, original = _original_inputs()
    if effective != effective_contract(original,corner): raise ValueError('ONLY_CORNER_REFINED_NUMERICS_CHANGE_ALLOWED')
    generated = execution.revision.generate(bundle,original,CASE,PHYSICAL_SPEC)
    if generated['modelica'].encode('utf-8') != (BASE/'V11CGenerated.mo').read_bytes():
        raise ValueError('UNCHANGED_ORIGINAL_MODELICA_BYTES_REQUIRED')
    for name,payload in (('V11PhysicalB1.mo',execution.vehicle.isolated_b().encode('utf-8')),
                         ('V11ContactC2.mo',execution.vehicle.CANDIDATE.read_bytes())):
        if payload != (BASE/name).read_bytes() or payload != (STRICT/name).read_bytes():
            raise ValueError('UNCHANGED_B_C2_ENDPOINT_BYTES_REQUIRED')
    if generated['modelica'].encode('utf-8') != (STRICT/'V11CGenerated.mo').read_bytes():
        raise ValueError('UNCHANGED_STRICT_MODELICA_BYTES_REQUIRED')
    old = previous.build_script(generated['model_class'],35.,generated['variable_filter'],True)
    if old.encode('utf-8') != (BASE/'build.mos').read_bytes(): raise ValueError('EXACT_EXISTING_BUILD_REQUIRED')
    return generated,build_script(generated['model_class'],35.,generated['variable_filter'],True,corner)


def local_targets():
    tree = ast.parse((ROOT/NEW_PATHS[2]).read_text(encoding='utf-8'))
    names = [f'week12.tests.test_c_dimension_factorial.{cls.name}.{fn.name}' for cls in tree.body
        if isinstance(cls,ast.ClassDef) for fn in cls.body if isinstance(fn,ast.FunctionDef) and fn.name.startswith('test_')]
    if len(names) != 4 or len(set(names)) != 4: raise ValueError('EXACT_FOUR_FACTORIAL_CHECKS_REQUIRED')
    return names


def prepare():
    """Exclusive source archive only; decision-basis must already be parent-owned."""
    if OUT.exists(): raise ValueError('EXECUTION_DIRECTORY_MUST_NOT_EXIST_DURING_PREPARATION')
    decision = REVIEW/'decision-basis.json'
    if not decision.is_file(): raise ValueError('PREEXISTING_PARENT_DECISION_BASIS_REQUIRED')
    old_identity,packet,bundle,original = _original_inputs()
    fixed = _fixed_sources(old_identity,packet); _check_reuse()
    for name in NEW_PATHS: ast.parse((ROOT/name).read_text(encoding='utf-8'))
    local_targets()
    prepared = {c:_generated(packet,bundle,effective_contract(original,c),c) for c in CORNER_ORDER}
    paths = _archive_paths()
    paths += [reference.REVIEW/n for n in ('diagnostic-review-packet.json','original-contract.json','verification-contract.json')]
    # All hashes come from real files; the two completed summaries are not recreated.
    bound = prior._bound_files(list(dict.fromkeys(paths)))
    identity = {'scope':SCOPE,'physical_specification_digest':PHYSICAL_SPEC,'physical_digest':PHYSICAL_DIGEST,
        'vehicle_bundle_digest':digest(bundle),'vehicle_verification_digest':digest(original),
        'effective_verification_digests':{c:digest(effective_contract(original,c)) for c in CORNER_ORDER},
        'case_order':[CASE],'variant':'refined','stop_time_s':35,'corner_order':list(CORNER_ORDER),
        'numerics':deepcopy(NUMERICS),'endpoint_numerics':deepcopy(ENDPOINT_NUMERICS),
        'edge_factors':deepcopy(EDGE_FACTORS),'stats_flag':STATS_FLAG,
        'physical':deepcopy(original['physical']),'numerical_comparison':deepcopy(original['numerical_comparison']),
        'budget':deepcopy(BUDGET),'fixed_inputs':deepcopy(packet['fixed_inputs']),
        'readonly_manifest':deepcopy(packet['readonly_manifest']),'fixed_implementation_manifest':fixed,
        'implementation_manifest':[*fixed,*[{'path':str((ROOT/n).resolve()),'sha256':sha(ROOT/n)} for n in NEW_PATHS]],
        'reused_evidence':bound,'review_evidence':prior._bound_files([decision]),
        'prior_reference_digest':old_identity['specification_digest'],'expected_base_failed_checks':sorted(EXPECTED_FAILED),
        'generated_modelica_sha256':sha(BASE/'V11CGenerated.mo'),'no_prior_budget_carry_over':True,
        'old_closed_batch_never_reopened':True,'complete_round_reserve_s':1000,'vehicle_acceptance':'NOT_GRANTED',
        'physical_FAIL_remains_FAIL':True,'completed_diagnostic_retires_unused_budget':True,
        'postprocess_timeout_shared_per_scene_between_verification_and_two_comparisons':True,
        'existing_diagonal':str((STRICT/'failure-diagnostic.json').relative_to(ROOT)),
        'convergence_order_proven':False,'unique_physical_cause_proven':False,'full_adaptive_history_collected':False,
        'repair_boundary':'new three files only; evidence-backed changed sources; same physics and factors; complete round budget'}
    identity['specification_digest'] = digest(identity)
    validate_budget(identity); guard_sources(identity,packet,identity['implementation_manifest'])
    names = ('diagnostic-review-packet.json','vehicle-confirmation-identity.json','prior-reference-identity.json',
        'original-contract.json','proposal.json','verification-contract.json','confirmation-identity.json',
        'source-manifest.json','fixed-input-manifest.json','readonly-manifest.json','preparation.json',
        'source-snapshot','input-snapshot','readonly-snapshot',*CORNER_ORDER)
    if any((REVIEW/n).exists() for n in names): raise ValueError('SOURCE_PREPARATION_ALREADY_EXISTS_NO_OVERWRITE')
    previous.snapshot_inputs(REVIEW,identity,bundle,original,identity['implementation_manifest'])
    execution.snapshot_readonly(REVIEW,packet)
    for name,value in (('original-contract.json',original),('vehicle-confirmation-identity.json',packet),
                       ('prior-reference-identity.json',old_identity),('diagnostic-review-packet.json',identity)):
        save(REVIEW/name,value)
    for corner,(generated,build) in prepared.items():
        directory = REVIEW/corner; directory.mkdir(exist_ok=False)
        save(directory/'verification-contract.json',effective_contract(original,corner))
        folder = directory/(CASE+'-refined'); folder.mkdir(exist_ok=False)
        for name,payload in (('V11CGenerated.mo',generated['modelica'].encode('utf-8')),
            ('V11PhysicalB1.mo',execution.vehicle.isolated_b().encode('utf-8')),
            ('V11ContactC2.mo',execution.vehicle.CANDIDATE.read_bytes()),('build-preview.mos',build.encode('utf-8'))):
            with (folder/name).open('xb') as stream: stream.write(payload)
        save(folder/'trace.json',generated['trace'])
    save(REVIEW/'preparation.json',{'status':'SOURCE_ONLY_PREPARED_NOT_AUTHORIZED',
        'ordinary_development_unit_checks':'recorded separately by parent review, not execution preflight',
        'receipt_bound_preflight_checks_run':0,'new_simulations':0,'new_sysml':0,
        'online':0,'identity_probes':0,'C_complete':False})
    return identity


def inputs():
    identity = read(REVIEW/'diagnostic-review-packet.json')
    raw = dict(identity); checksum = raw.pop('specification_digest')
    if digest(raw) != checksum: raise ValueError('FACTORIAL_IDENTITY_DRIFT')
    old_identity,packet,bundle,original = _original_inputs()
    if (read(REVIEW/'original-contract.json') != original or read(REVIEW/'verification-contract.json') != original or
            read(REVIEW/'vehicle-confirmation-identity.json') != packet or read(REVIEW/'proposal.json') != bundle or
            read(REVIEW/'prior-reference-identity.json') != old_identity or
            identity['prior_reference_digest'] != old_identity['specification_digest'] or
            identity['physical_specification_digest'] != PHYSICAL_SPEC or identity['physical_digest'] != PHYSICAL_DIGEST or
            identity['vehicle_bundle_digest'] != digest(bundle) or identity['physical'] != original['physical'] or
            identity['numerical_comparison'] != original['numerical_comparison'] or
            identity['expected_base_failed_checks'] != sorted(EXPECTED_FAILED) or
            any(read(REVIEW/c/'verification-contract.json') != effective_contract(original,c) for c in CORNER_ORDER)):
        raise ValueError('UNCHANGED_VEHICLE_AND_ONLY_FIXED_CORNER_NUMERICS_REQUIRED')
    validate_budget(identity); guard_sources(identity,packet,identity['implementation_manifest'],allow_new_drift=True)
    return identity,packet,bundle,original


# The original functions use this module's isolated globals, never patch old modules.
for _name in ('initialize','approved_inputs','validate_repair','round_manifest','_check_refined','_classify_scene_error'):
    globals()[_name] = _reuse(_name)


def require_complete_round(batch):
    used = batch.usage()
    if (batch.budget != BUDGET or used['unresolved_processes'] or used['sysml'] or used['probe'] or
            used['scene']+2 > BUDGET['maximum_scene_attempts'] or
            used['local']+4 > BUDGET['new_local_execution_checks_limit'] or
            not math.isfinite(used['process_seconds']) or used['process_seconds'] < 0 or
            used['process_seconds']+1000 > BUDGET['execution_total_wall_s']):
        raise ValueError('COMPLETE_2_SCENE_4_LOCAL_1000S_ROUND_REQUIRED')


class _NumericsBatch:
    """Verify actual XML and raw runner argv; append only the approved stats flag."""
    def __init__(self,batch,corner): self._batch,self.corner = batch,corner
    def __getattr__(self,name): return getattr(self._batch,name)
    def run(self,command,cwd,timeout,log_dir,label):
        if label != 'simulation': return self._batch.run(command,cwd,timeout,log_dir,label)
        n = NUMERICS[self.corner]
        path = Path(cwd)/'model_init.xml'
        elements = list(ET.parse(path).getroot().iter('DefaultExperiment'))
        expected = {'startTime':0.,'stopTime':35.,'stepSize':.001,'tolerance':n['tolerance']}
        if (len(elements) != 1 or any(float(elements[0].get(k,'nan')) != v for k,v in expected.items()) or
                elements[0].get('solver') != 'dassl' or elements[0].get('outputFormat') != 'csv' or
                command[1:] != ['-s=dassl',f'-maxStepSize={n["max_step_s"]}','-r=result.csv']):
            raise ValueError('ACTUAL_XML_OR_LAUNCH_NUMERICS_DRIFT_BEFORE_SIMULATION')
        actual = [*command,STATS_FLAG]
        save(Path(log_dir)/'actual-numerics.json',{'status':'PASS_BEFORE_SIMULATION','corner':self.corner,
            'numerics':deepcopy(n),'init_xml_sha256':sha(path),'default_experiment':expected,
            'runner_command':command,'launch_command':actual,'stats_flag':STATS_FLAG})
        result = self._batch.run(actual,cwd,timeout,log_dir,label)
        stdout = Path(log_dir)/'simulation.stdout.log'
        marker = 'LOG_STATS' in stdout.read_text(encoding='utf-8')
        successful = result['returncode'] == 0 and not result['timed_out']
        save(Path(log_dir)/'solver-statistics.json',{'status':'COLLECTED_RAW_SUMMARY' if marker else 'TELEMETRY_INCOMPLETE',
            'corner':self.corner,'flag':STATS_FLAG,'stdout_sha256':sha(stdout),
            'stats_marker_present':marker,'simulation_successful':successful,'full_adaptive_history_collected':False})
        if successful and not marker: raise RuntimeError('SUCCESSFUL_SIMULATION_STATS_TELEMETRY_INCOMPLETE')
        return result


def execute_scene(batch,directory,bundle,contract,number,manifest,identity,packet,corner):
    _, _, _, original = _original_inputs()
    fn = previous.execute_scene
    namespace = dict(fn.__globals__,amendment=execution._VehicleProxy(original,packet),
        build_script=lambda m,s,v,r:build_script(m,s,v,r,corner),
        guard_sources=lambda p,rows:guard_sources(identity,p,rows))
    isolated = FunctionType(fn.__code__,namespace,fn.__name__,fn.__defaults__,fn.__closure__)
    row,folder = isolated(_NumericsBatch(batch,corner),directory,bundle,contract,CASE,True,number,manifest,packet,base=None)
    row.update(corner=corner,numerics=deepcopy(NUMERICS[corner]))
    save(folder/'numerical-factor-binding.json',{'diagnostic_digest':identity['specification_digest'],
        'physical_specification_digest':PHYSICAL_SPEC,'effective_verification_digest':digest(contract),
        'original_verification_digest':identity['vehicle_verification_digest'],'corner':corner,
        'numerics':deepcopy(NUMERICS[corner]),'generated_trace_is_original_physical_trace':True})
    return row,folder


def _check_telemetry(folder,corner):
    actual,stats = read(folder/'actual-numerics.json'),read(folder/'solver-statistics.json')
    if (actual['corner'] != corner or actual['numerics'] != NUMERICS[corner] or actual['stats_flag'] != STATS_FLAG or
            actual['init_xml_sha256'] != sha(folder/'model_init.xml') or
            actual['launch_command'] != [*actual['runner_command'],STATS_FLAG] or
            stats['status'] != 'COLLECTED_RAW_SUMMARY' or stats['simulation_successful'] is not True or
            stats['stats_marker_present'] is not True or stats['stdout_sha256'] != sha(folder/'simulation.stdout.log')):
        raise ValueError('ACTUAL_NUMERICS_OR_RAW_STATISTICS_EVIDENCE_DRIFT')


def compare_folder(folder):
    folder = Path(folder).resolve(strict=True)
    identity,packet,_,original = approved_inputs()
    corner = folder.parent.name
    directory = folder.parent.parent
    if (corner not in CORNER_ORDER or folder.name != CASE+'-refined' or directory.parent != OUT.resolve() or
            directory.name not in ('round-01','round-02','round-03')):
        raise ValueError('OWNED_EXACT_FACTORIAL_FOLDER_REQUIRED')
    if not (OUT/'.execution.lock').is_file(): raise ValueError('ACTIVE_PARENT_DIAGNOSTIC_LOCK_REQUIRED')
    starts = sorted(OUT.glob('process-*.start.json'))
    if not starts: raise ValueError('METERED_COMPARISON_PROCESS_REQUIRED')
    started = read(starts[-1])
    expected = [sys.executable,'-B','-m',CLI,'--compare',str(folder)]
    timeout = started.get('timeout_s',0)
    if (started.get('label') != 'factor-comparison-'+corner or started.get('command') != expected or
            Path(started.get('cwd','')).resolve() != ROOT.parent.resolve() or not math.isfinite(timeout) or
            not 0 < timeout <= BUDGET['postprocess_timeout_s'] or
            starts[-1].with_name(starts[-1].name.replace('.start.json','.result.json')).exists()):
        raise ValueError('EXACT_ACTIVE_METERED_COMPARISON_PROCESS_REQUIRED')
    manifest = read(directory/'source-manifest.json'); guard_sources(identity,packet,manifest)
    contract = effective_contract(original,corner)
    if read(folder.parent/'verification-contract.json') != contract: raise ValueError('CORNER_CONTRACT_DRIFT')
    refined = _check_refined(folder); _check_telemetry(folder,corner)
    edges = []
    for role,endpoint in (('A_existing',BASE),('D_existing',STRICT)):
        result = compare_failure(contract,endpoint/'result.csv',folder/'result.csv',read(endpoint/'physical.json'),refined)
        result.update(comparison_roles={'base':role,'refined':corner},
            isolated_factor=EDGE_FACTORS[corner][role],prior_numerics=deepcopy(ENDPOINT_NUMERICS[role]),
            corner_numerics=deepcopy(NUMERICS[corner]),convergence_order_proven=False,
            unique_physical_cause_proven=False)
        guard_sources(identity,packet,manifest)
        save(folder/('edge-'+role+'.json'),result)
        edges.append({'base':role,'corner':corner,'isolated_factor':EDGE_FACTORS[corner][role],
                      'classification':result['classification'],'vehicle_acceptance':result['vehicle_acceptance']})
    result = {'corner':corner,'edges':edges,'vehicle_acceptance':'NOT_GRANTED',
        'existing_diagonal_reused':identity['existing_diagonal'],'convergence_order_proven':False}
    guard_sources(identity,packet,manifest); save(folder/'factor-comparisons.json',result)
    return result


def run_round(number):
    identity,packet,bundle,original = approved_inputs()
    manifest = round_manifest(number,identity,packet)
    batch = Batch(OUT,identity['specification_digest']); require_complete_round(batch)
    directory = OUT/f'round-{number:02d}'
    if directory.exists(): raise ValueError('ROUND_DIRECTORY_ALREADY_EXISTS')
    lock,token = OUT/'.execution.lock',f'{os.getpid()}:{uuid.uuid4().hex}'
    fd = os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    try: os.write(fd,token.encode('ascii'))
    finally: os.close(fd)
    owns = False
    report = {'round':number,'status':'PENDING','local':[],'scenes':[],'edges':[],
        'diagnostic_digest':identity['specification_digest'],'physical_specification_digest':PHYSICAL_SPEC,
        'physical_digest':PHYSICAL_DIGEST,'vehicle_acceptance':'NOT_GRANTED','C_complete':False,
        'sysml':0,'online':0,'identity_probes':0,'retry_eligible':False}
    try:
        directory.mkdir(exist_ok=False); owns = True
        guard_sources(identity,packet,manifest)
        previous.snapshot_inputs(directory,identity,bundle,original,manifest)
        execution.snapshot_readonly(directory,packet)
        save(directory/'original-contract.json',original)
        save(directory/'vehicle-confirmation-identity.json',packet); save(directory/'diagnostic-identity.json',identity)
        dependency_audit(directory)
        for index,target in enumerate(local_targets(),1):
            guard_sources(identity,packet,manifest); batch.reserve('local',target,number)
            proc = batch.run([sys.executable,'-B','-m','unittest',target],ROOT.parent,10,directory,f'local-{index:02d}')
            status = 'PASS' if proc['returncode'] == 0 and not proc['timed_out'] else 'FAIL'
            report['local'].append({'test':target,'status':status})
            if proc['timed_out']: raise RuntimeError('LOCAL_TOOL_TIMEOUT')
            if status != 'PASS':
                stderr = (directory/f'local-{index:02d}.stderr.log').read_text(encoding='utf-8')
                if proc['returncode'] == 1 and 'AssertionError' in stderr and 'test_c_dimension_factorial' in stderr:
                    raise ImplementationFailure('NEW_LOCAL_CHECK_FAILURE')
                raise RuntimeError('UNKNOWN_LOCAL_TOOL_OR_GUARD_FAILURE')
        for corner in CORNER_ORDER:
            guard_sources(identity,packet,manifest)
            contract = effective_contract(original,corner); _generated(packet,bundle,contract,corner)
            target = directory/corner; target.mkdir(exist_ok=False)
            save(target/'proposal.json',bundle); save(target/'verification-contract.json',contract)
            row,folder = execute_scene(batch,target,bundle,contract,number,manifest,identity,packet,corner)
            report['scenes'].append(row); guard_sources(identity,packet,manifest)
            _classify_scene_error(row,folder)
            used_post = _completed_verification(batch,row,folder)
            _check_refined(folder); _check_telemetry(folder,corner)
            remaining = BUDGET['postprocess_timeout_s']-used_post
            if not math.isfinite(remaining) or remaining <= 0: raise RuntimeError('SHARED_POSTPROCESS_BUDGET_EXHAUSTED')
            proc = batch.run([sys.executable,'-B','-m',CLI,'--compare',str(folder)],
                ROOT.parent,remaining,folder,'factor-comparison-'+corner)
            if proc['returncode'] != 0 or proc['timed_out']: raise RuntimeError('DIAGNOSTIC_COMPARATOR_TOOL_FAILURE')
            result = read(folder/'factor-comparisons.json')
            if (result.get('corner') != corner or result.get('vehicle_acceptance') != 'NOT_GRANTED' or
                    len(result.get('edges',[])) != 2 or
                    [(e.get('base'),e.get('corner'),e.get('isolated_factor')) for e in result['edges']] !=
                    [(r,corner,EDGE_FACTORS[corner][r]) for r in ('A_existing','D_existing')] or
                    any(e.get('classification') not in FINAL_CLASSIFICATIONS or
                        e.get('vehicle_acceptance') != 'NOT_GRANTED' for e in result['edges'])):
                raise RuntimeError('INCOMPLETE_TWO_EDGE_DIAGNOSTIC_EVIDENCE')
            report['edges'].extend(result['edges'])
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
