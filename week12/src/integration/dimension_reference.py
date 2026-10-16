"""One stricter same-physics reference; external receipt required for execution.

Reuse frozen guards, diagnosis rules and comparator in isolated function globals.
The effective contract changes only numerical.refined; generation uses the original.
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

from . import dimension_failure_diagnostic as prior
from .execution import Batch, read, save, sha, require_sources, dependency_audit
from .review import check_review_receipt
from ..composition.schema import digest

execution, previous, comparator = prior.execution, prior.previous, prior.comparator
ROOT = prior.ROOT
REVIEW = ROOT/'results/s3-c-dimension-reference-review-20261006-01'
OUT = ROOT/'results/s3-c-dimension-reference-execution-20261006-01'
OLD, OLD_ROUND = prior.OUT, prior.OUT/'round-01'
CASE = prior.CASE
BASE = OLD_ROUND/(CASE+'-refined')
PHYSICAL_SPEC, PHYSICAL_DIGEST = prior.PHYSICAL_SPEC, prior.PHYSICAL_DIGEST
SCOPE = 'v11_s3_c_same_dimension_stricter_numerical_reference'
NEW_PATHS = ('src/integration/dimension_reference.py',
             'scripts/s3_c_dimension_reference.py', 'tests/test_c_dimension_reference.py')
BUDGET = deepcopy(prior.BUDGET)
NUMERICS = {'solver':'dassl', 'tolerance':1e-8, 'max_step_s':.00025, 'output_interval_s':.001}
FINAL_CLASSIFICATIONS, EXPECTED_FAILED = prior.FINAL_CLASSIFICATIONS, prior.EXPECTED_FAILED
REPAIR_CODES = prior.REPAIR_CODES
compare_failure, _completed_verification = prior.compare_failure, prior._completed_verification
_failed_checks = prior._failed_checks
ImplementationFailure = prior.ImplementationFailure
FROZEN = {'source-manifest.json':'5edf5f77975c70d507be11fefcace58398b2b47baab43250b00b1acddd510532',
          'final-result.json':'9ae9463a702c0a53192ddd21a849a3739dcc8c18abaaba68933b7eba529af1aa',
          'failure-diagnostic.json':'6a541d38a7d9f46c41d933ec3e42c9b7abe73aea93485e50ee6b4328eae5caa2'}


def _reuse(name):
    fn = getattr(prior,name)
    # This module's globals are separate from every frozen module's globals.
    return FunctionType(fn.__code__,globals(),fn.__name__,fn.__defaults__,fn.__closure__)


def effective_contract(original):
    if original['numerical']['refined'] != prior.NUMERICS:
        raise ValueError('FROZEN_PREVIOUS_REFINED_NUMERICS_REQUIRED')
    effective = deepcopy(original)
    effective['numerical']['refined'] = deepcopy(NUMERICS)
    return effective


def _fixed_sources(old_identity, packet):
    if sha(OLD_ROUND/'source-manifest.json') != FROZEN['source-manifest.json']:
        raise ValueError('FROZEN_30_SOURCE_MANIFEST_DRIFT')
    rows = read(OLD_ROUND/'source-manifest.json')
    if len(rows) != 30 or rows != old_identity['implementation_manifest']:
        raise ValueError('EXACT_ORIGINAL_30_SOURCES_REQUIRED')
    prior.guard_sources(old_identity,packet,rows)
    return rows


def _check_reuse():
    if (sha(OLD/'final-result.json') != FROZEN['final-result.json'] or
            sha(BASE/'failure-diagnostic.json') != FROZEN['failure-diagnostic.json']):
        raise ValueError('CLOSED_PRIOR_REFERENCE_EVIDENCE_DRIFT')
    final, result = read(OLD/'final-result.json'), read(OLD_ROUND/'batch-result.json')
    expected = {k:v for k,v in final.items() if k not in
                ('unused_budget_disposition','automatic_additional_scene_permitted')}
    comparison, physical, scene = read(BASE/'failure-diagnostic.json'), read(BASE/'physical.json'), read(BASE/'result.json')
    if (expected != result or final['status'] != 'DIAGNOSTIC_COMPLETED_VEHICLE_NOT_ACCEPTED' or
            final['classification'] != 'NUMERICAL_SENSITIVITY' or final['retry_eligible'] is not False or
            final['unused_budget_disposition'] != 'RETIRED' or final['automatic_additional_scene_permitted'] is not False or
            final['usage']['unresolved_processes'] != 0 or final['source_guard_end'] != 'PASS' or
            len(final['local']) != 4 or any(r['status'] != 'PASS' for r in final['local']) or
            comparison['classification'] != 'NUMERICAL_SENSITIVITY' or len(comparison['checks']) != 182 or
            sum(c['pass'] is True for c in comparison['checks']) != 165 or
            _failed_checks(physical) != EXPECTED_FAILED or physical['metrics']['csv_sha256'] != sha(BASE/'result.csv') or
            any(scene[k] != v for k,v in (('compile','PASS'),('simulation','COMPLETED'),('physical','FAIL'),
                                         ('event_codegen','PASS'),('owner_audit','PASS')))):
        raise ValueError('CLOSED_COMPLETE_PRIOR_REFINED_REFERENCE_REQUIRED')


def guard_sources(identity, packet, manifest, *, allow_new_drift=False):
    old_identity, old_packet, _, original = prior.inputs()
    fixed = _fixed_sources(old_identity,packet)
    expected = previous._manifest_paths(fixed) | {str((ROOT/p).resolve()) for p in NEW_PATHS}
    if (packet != old_packet or identity['fixed_implementation_manifest'] != fixed or len(expected) != 33 or
            previous._manifest_paths(manifest) != expected or
            previous._manifest_paths(identity['implementation_manifest']) != expected):
        raise ValueError('EXACT_FIXED30_PLUS_NEW3_SOURCE_BINDING_REQUIRED')
    actual = execution._maps(manifest)
    if any(actual[str(Path(r['path']).resolve())] != r['sha256'] for r in fixed):
        raise ValueError('OLD_30_SOURCES_CANNOT_BE_REPAIRED')
    # Never send the new33 to the frozen30 guard.
    prior.guard_sources(old_identity,packet,fixed)
    require_sources(fixed if allow_new_drift else manifest)
    if (identity['fixed_inputs'] != packet['fixed_inputs'] or identity['readonly_manifest'] != packet['readonly_manifest'] or
            identity['vehicle_verification_digest'] != digest(original) or
            identity['effective_verification_digest'] != digest(effective_contract(original))):
        raise ValueError('FIXED_INPUT_OR_ORIGINAL_EFFECTIVE_CONTRACT_DRIFT')
    for row in [*identity['reused_evidence'],*identity['review_evidence']]:
        if sha(ROOT/row['path']) != row['sha256']: raise ValueError('REUSED_OR_REVIEW_EVIDENCE_DRIFT')
    _check_reuse()


def build_script(model_class, stop, variable_filter, refined):
    if refined is not True or stop != 35: raise ValueError('EXACT_STRICTER_REFERENCE_BUILD_REQUIRED')
    script = previous.build_script(model_class,stop,variable_filter,True)
    old, new = (f'tolerance={n["tolerance"]:.17g}' for n in (prior.NUMERICS,NUMERICS))
    if script.count(old) != 1: raise ValueError('EXACT_ONE_OLD_BUILD_TOLERANCE_LITERAL_REQUIRED')
    return script.replace(old,new,1)


def _generated(packet, bundle, effective):
    _, _, _, original = prior.inputs()
    if effective != effective_contract(original): raise ValueError('ONLY_REFINED_NUMERICAL_CHANGE_ALLOWED')
    generated = execution.revision.generate(bundle,original,CASE,PHYSICAL_SPEC)
    if generated['modelica'].encode('utf-8') != (BASE/'V11CGenerated.mo').read_bytes():
        raise ValueError('UNCHANGED_PRIOR_REFINED_MODELICA_BYTES_REQUIRED')
    for name,payload in (('V11PhysicalB1.mo',execution.vehicle.isolated_b().encode('utf-8')),
                         ('V11ContactC2.mo',execution.vehicle.CANDIDATE.read_bytes())):
        if payload != (BASE/name).read_bytes(): raise ValueError('UNCHANGED_B_C2_BYTES_REQUIRED')
    old = previous.build_script(generated['model_class'],35.,generated['variable_filter'],True)
    if old.encode('utf-8') != (BASE/'build.mos').read_bytes(): raise ValueError('EXACT_PRIOR_REFINED_BUILD_REQUIRED')
    new = build_script(generated['model_class'],35.,generated['variable_filter'],True)
    if new.replace(f'tolerance={NUMERICS["tolerance"]:.17g}',f'tolerance={prior.NUMERICS["tolerance"]:.17g}',1) != old:
        raise ValueError('EXACT_ONE_BUILD_TOLERANCE_CHANGE_REQUIRED')
    generated = deepcopy(generated)
    generated['trace'].update(numerical_level='stricter_reference',numerics=deepcopy(NUMERICS),
        generation_contract='original-contract.json',effective_contract='verification-contract.json')
    return generated,new


def prepare():
    """Exclusive source-only review; never create OUT or an authorization."""
    if OUT.exists(): raise ValueError('EXECUTION_DIRECTORY_MUST_NOT_EXIST_DURING_PREPARATION')
    old_identity,packet,bundle,original = prior.inputs()
    fixed = _fixed_sources(old_identity,packet); _check_reuse()
    for name in NEW_PATHS: ast.parse((ROOT/name).read_text(encoding='utf-8'))
    local_targets()
    effective = effective_contract(original)
    generated,build = _generated(packet,bundle,effective)
    paths = [OLD/n for n in ('final-result.json','authorization.json','authorization-source.json','approved-diagnostic.json','policy.json')]
    paths += [OLD_ROUND/n for n in ('batch-result.json','source-manifest.json','diagnostic-identity.json',
        'vehicle-confirmation-identity.json','proposal.json','verification-contract.json','readonly-manifest.json',
        'fixed-input-manifest.json','dependency-audit.json')]
    paths += [BASE/n for n in ('result.csv','physical.json','result.json','input-identity.json','V11CGenerated.mo',
        'V11PhysicalB1.mo','V11ContactC2.mo','build.mos','simulation-launch.json','event-codegen.json','owner-audit.json',
        'trace.json','failure-diagnostic.json')]
    paths += [OLD_ROUND/f'local-{i:02d}.{s}.log' for i in range(1,5) for s in ('stdout','stderr')]
    identity = {'scope':SCOPE,'physical_specification_digest':PHYSICAL_SPEC,'physical_digest':PHYSICAL_DIGEST,
        'vehicle_bundle_digest':digest(bundle),'vehicle_verification_digest':digest(original),
        'effective_verification_digest':digest(effective),'case_order':[CASE],'variant':'refined','stop_time_s':35,
        'numerical_level':'stricter_reference','numerics':deepcopy(NUMERICS),'base_numerics':deepcopy(prior.NUMERICS),
        'physical':deepcopy(original['physical']),'numerical_comparison':deepcopy(original['numerical_comparison']),
        'budget':deepcopy(BUDGET),'fixed_inputs':deepcopy(packet['fixed_inputs']),
        'readonly_manifest':deepcopy(packet['readonly_manifest']),'fixed_implementation_manifest':fixed,
        'implementation_manifest':[*fixed,*[{'path':str((ROOT/n).resolve()),'sha256':sha(ROOT/n)} for n in NEW_PATHS]],
        'reused_evidence':prior._bound_files(paths),'review_evidence':prior._bound_files([REVIEW/'decision-basis.json']),
        'prior_diagnostic_digest':old_identity['specification_digest'],'expected_base_failed_checks':sorted(EXPECTED_FAILED),
        'generated_modelica_sha256':sha(BASE/'V11CGenerated.mo'),'no_prior_budget_carry_over':True,
        'old_closed_batch_never_reopened':True,'complete_round_reserve_s':500,'vehicle_acceptance':'NOT_GRANTED',
        'physical_FAIL_remains_FAIL':True,'completed_diagnostic_retires_unused_budget':True,
        'postprocess_timeout_is_shared_between_verification_and_comparison':True,
        'comparison_roles':{'base':'previous_refined','refined':'stricter_reference'},
        'convergence_order_proven':False,'single_point_tightens_tolerance_and_maxstep_together':True,
        'repair_boundary':'only new three files; evidence-backed changed sources; same physics; complete round budget'}
    identity['specification_digest'] = digest(identity)
    validate_budget(identity); guard_sources(identity,packet,identity['implementation_manifest'])
    targets = ('diagnostic-review-packet.json','vehicle-confirmation-identity.json','prior-diagnostic-identity.json',
        'original-contract.json','proposal.json','verification-contract.json','confirmation-identity.json',
        'source-manifest.json','fixed-input-manifest.json','readonly-manifest.json','preparation.json',
        'source-snapshot','input-snapshot','readonly-snapshot',CASE+'-refined')
    if any((REVIEW/n).exists() for n in targets): raise ValueError('SOURCE_PREPARATION_ALREADY_EXISTS_NO_OVERWRITE')
    REVIEW.mkdir(exist_ok=True)
    previous.snapshot_inputs(REVIEW,identity,bundle,effective,identity['implementation_manifest'])
    execution.snapshot_readonly(REVIEW,packet)
    for name,value in (('original-contract.json',original),('vehicle-confirmation-identity.json',packet),
                       ('prior-diagnostic-identity.json',old_identity),('diagnostic-review-packet.json',identity)):
        save(REVIEW/name,value)
    folder = REVIEW/(CASE+'-refined'); folder.mkdir(exist_ok=False)
    for name,payload in (('V11CGenerated.mo',generated['modelica'].encode('utf-8')),
        ('V11PhysicalB1.mo',execution.vehicle.isolated_b().encode('utf-8')),
        ('V11ContactC2.mo',execution.vehicle.CANDIDATE.read_bytes()),('build-preview.mos',build.encode('utf-8'))):
        with (folder/name).open('xb') as stream: stream.write(payload)
    save(folder/'trace.json',generated['trace'])
    save(REVIEW/'preparation.json',{'status':'SOURCE_ONLY_PREPARED_NOT_AUTHORIZED','new_tests_run':0,
        'new_simulations':0,'new_sysml':0,'online':0,'identity_probes':0,'C_complete':False})
    return identity


def inputs():
    identity = read(REVIEW/'diagnostic-review-packet.json')
    raw = dict(identity); checksum = raw.pop('specification_digest')
    if digest(raw) != checksum: raise ValueError('STRICTER_REFERENCE_IDENTITY_DRIFT')
    old_identity,packet,bundle,original = prior.inputs()
    effective = effective_contract(original)
    if (read(REVIEW/'original-contract.json') != original or read(REVIEW/'verification-contract.json') != effective or
            read(REVIEW/'vehicle-confirmation-identity.json') != packet or read(REVIEW/'proposal.json') != bundle or
            read(REVIEW/'prior-diagnostic-identity.json') != old_identity or
            identity['prior_diagnostic_digest'] != old_identity['specification_digest'] or
            identity['physical_specification_digest'] != PHYSICAL_SPEC or identity['physical_digest'] != PHYSICAL_DIGEST or
            identity['vehicle_bundle_digest'] != digest(bundle) or identity['physical'] != original['physical'] or
            identity['numerical_comparison'] != original['numerical_comparison'] or identity['base_numerics'] != prior.NUMERICS or
            identity['numerical_level'] != 'stricter_reference' or identity['expected_base_failed_checks'] != sorted(EXPECTED_FAILED)):
        raise ValueError('UNCHANGED_VEHICLE_AND_ONLY_REFINED_NUMERICS_REQUIRED')
    validate_budget(identity)
    guard_sources(identity,packet,identity['implementation_manifest'],allow_new_drift=True)
    return identity,packet,bundle,effective


def local_targets():
    tree = ast.parse((ROOT/NEW_PATHS[2]).read_text(encoding='utf-8'))
    names = [f'week12.tests.test_c_dimension_reference.{cls.name}.{fn.name}' for cls in tree.body
        if isinstance(cls,ast.ClassDef) for fn in cls.body if isinstance(fn,ast.FunctionDef) and fn.name.startswith('test_')]
    if len(names) != 4 or len(set(names)) != 4: raise ValueError('EXACT_FOUR_REFERENCE_CHECKS_REQUIRED')
    return names


def execute_scene(batch, directory, bundle, contract, number, manifest, identity, packet):
    _, _, _, original_contract = prior.inputs()
    fn = previous.execute_scene
    namespace = dict(fn.__globals__,amendment=execution._VehicleProxy(original_contract,packet),
        build_script=build_script,guard_sources=lambda p,rows:guard_sources(identity,p,rows))
    isolated = FunctionType(fn.__code__,namespace,fn.__name__,fn.__defaults__,fn.__closure__)
    row,folder = isolated(_NumericsBatch(batch),directory,bundle,contract,CASE,True,number,manifest,packet,base=None)
    row.update(numerical_level='stricter_reference',numerics=deepcopy(NUMERICS))
    save(folder/'numerical-reference-binding.json',{'diagnostic_digest':identity['specification_digest'],
        'physical_specification_digest':PHYSICAL_SPEC,'effective_verification_digest':digest(contract),
        'original_verification_digest':identity['vehicle_verification_digest'],
        'numerical_level':'stricter_reference','numerics':deepcopy(NUMERICS),
        'runtime_folder_suffix_is_legacy':True,'generated_trace_is_original_physical_trace':True})
    return row,folder


class _NumericsBatch:
    """Read the generated init XML before simulation; delegate all metering."""
    def __init__(self, batch): self._batch = batch
    def __getattr__(self, name): return getattr(self._batch,name)
    def run(self, command, cwd, timeout, log_dir, label):
        if label == 'simulation':
            path = Path(cwd)/'model_init.xml'
            elements = list(ET.parse(path).getroot().iter('DefaultExperiment'))
            expected = {'startTime':0.,'stopTime':35.,'stepSize':.001,'tolerance':1e-8}
            if (len(elements) != 1 or any(float(elements[0].get(k,'nan')) != v for k,v in expected.items()) or
                    elements[0].get('solver') != 'dassl' or elements[0].get('outputFormat') != 'csv' or
                    command[1:] != ['-s=dassl','-maxStepSize=0.00025','-r=result.csv']):
                raise ValueError('ACTUAL_XML_OR_LAUNCH_NUMERICS_DRIFT_BEFORE_SIMULATION')
            save(Path(log_dir)/'actual-numerics.json',{'status':'PASS_BEFORE_SIMULATION',
                'numerical_level':'stricter_reference','numerics':deepcopy(NUMERICS),
                'init_xml_sha256':sha(path),'default_experiment':expected,'launch_command':command})
        return self._batch.run(command,cwd,timeout,log_dir,label)


# These frozen routines contain no old CLI/module literal and use our isolated globals.
for _name in ('validate_budget','initialize','approved_inputs','validate_repair','round_manifest',
              'require_complete_round','_check_refined','_classify_scene_error'):
    globals()[_name] = _reuse(_name)


def run_round(number):
    identity,packet,bundle,contract = approved_inputs()
    manifest = round_manifest(number,identity,packet)
    batch = Batch(OUT,identity['specification_digest']); require_complete_round(batch)
    directory = OUT/f'round-{number:02d}'
    if directory.exists(): raise ValueError('ROUND_DIRECTORY_ALREADY_EXISTS')
    lock,token = OUT/'.execution.lock',f'{os.getpid()}:{uuid.uuid4().hex}'
    fd = os.open(lock,os.O_CREAT|os.O_EXCL|os.O_WRONLY)
    try: os.write(fd,token.encode('ascii'))
    finally: os.close(fd)
    owns = False
    report = {'round':number,'status':'PENDING','local':[],'scenes':[],
        'diagnostic_digest':identity['specification_digest'],'physical_specification_digest':PHYSICAL_SPEC,
        'physical_digest':PHYSICAL_DIGEST,'numerical_level':'stricter_reference',
        'vehicle_acceptance':'NOT_GRANTED','C_complete':False,'sysml':0,'online':0,'identity_probes':0,'retry_eligible':False}
    try:
        directory.mkdir(exist_ok=False); owns = True
        guard_sources(identity,packet,manifest)
        previous.snapshot_inputs(directory,identity,bundle,contract,manifest)
        execution.snapshot_readonly(directory,packet)
        save(directory/'original-contract.json',read(REVIEW/'original-contract.json'))
        save(directory/'vehicle-confirmation-identity.json',packet)
        save(directory/'diagnostic-identity.json',identity)
        dependency_audit(directory)
        for index,target in enumerate(local_targets(),1):
            guard_sources(identity,packet,manifest); batch.reserve('local',target,number)
            proc = batch.run([sys.executable,'-B','-m','unittest',target],ROOT.parent,10,directory,f'local-{index:02d}')
            status = 'PASS' if proc['returncode'] == 0 and not proc['timed_out'] else 'FAIL'
            report['local'].append({'test':target,'status':status})
            if proc['timed_out']: raise RuntimeError('LOCAL_TOOL_TIMEOUT')
            if status != 'PASS':
                stderr = (directory/f'local-{index:02d}.stderr.log').read_text(encoding='utf-8')
                if proc['returncode'] == 1 and 'AssertionError' in stderr and 'test_c_dimension_reference' in stderr:
                    raise ImplementationFailure('NEW_LOCAL_CHECK_FAILURE')
                raise RuntimeError('UNKNOWN_LOCAL_TOOL_OR_GUARD_FAILURE')
        guard_sources(identity,packet,manifest); _generated(packet,bundle,contract)
        row,folder = execute_scene(batch,directory,bundle,contract,number,manifest,identity,packet)
        report['scenes'].append(row); guard_sources(identity,packet,manifest)
        _classify_scene_error(row,folder)
        used_post = _completed_verification(batch,row,folder); _check_refined(folder)
        remaining = BUDGET['postprocess_timeout_s']-used_post
        if not math.isfinite(remaining) or remaining <= 0: raise RuntimeError('SHARED_POSTPROCESS_BUDGET_EXHAUSTED')
        proc = batch.run([sys.executable,'-B','-m','week12.scripts.s3_c_dimension_reference','--compare',str(folder)],
            ROOT.parent,remaining,folder,'failure-comparison')
        if proc['returncode'] != 0 or proc['timed_out']: raise RuntimeError('DIAGNOSTIC_COMPARATOR_TOOL_FAILURE')
        comparison = read(folder/'failure-diagnostic.json'); report['classification'] = comparison['classification']
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
        raise ValueError('OWNED_EXACT_REFERENCE_FOLDER_REQUIRED')
    if not (OUT/'.execution.lock').is_file(): raise ValueError('ACTIVE_PARENT_DIAGNOSTIC_LOCK_REQUIRED')
    starts = sorted(OUT.glob('process-*.start.json'))
    if not starts: raise ValueError('METERED_COMPARISON_PROCESS_REQUIRED')
    started = read(starts[-1])
    expected = [sys.executable,'-B','-m','week12.scripts.s3_c_dimension_reference','--compare',str(folder)]
    if (started.get('label') != 'failure-comparison' or started.get('command') != expected or
            Path(started.get('cwd','')).resolve() != ROOT.parent.resolve() or
            not 0 < started.get('timeout_s',0) <= BUDGET['postprocess_timeout_s'] or
            starts[-1].with_name(starts[-1].name.replace('.start.json','.result.json')).exists()):
        raise ValueError('EXACT_ACTIVE_METERED_COMPARISON_PROCESS_REQUIRED')
    manifest = read(folder.parent/'source-manifest.json'); guard_sources(identity,packet,manifest)
    refined = _check_refined(folder)
    result = compare_failure(contract,BASE/'result.csv',folder/'result.csv',read(BASE/'physical.json'),refined)
    result.update(comparison_roles={'base':'previous_refined','refined':'stricter_reference'},
        prior_numerics=deepcopy(prior.NUMERICS),reference_numerics=deepcopy(NUMERICS),
        convergence_order_proven=False,single_point_tightens_tolerance_and_maxstep_together=True)
    guard_sources(identity,packet,manifest); save(folder/'failure-diagnostic.json',result)
    return result
