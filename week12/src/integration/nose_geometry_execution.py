"""One externally approved R100/L270 candidate; isolated frozen-runner reuse.

Preparation only binds sources. It never changes the reviewed physical packet,
creates approval, runs a tool, or opens a retired ledger. All child execution is
metered by Batch; each repair requires a changed, diagnosed new-file manifest.
"""
from copy import deepcopy
import ast
import os
from pathlib import Path
import sys
from types import FunctionType, SimpleNamespace
import unittest
import uuid
import xml.etree.ElementTree as ET

from . import dimension_execution as reuse
from . import dimension_revision
from . import vehicle_amendment as vehicle
from . import vehicle_amendment_execution as previous
from .execution import Batch, read, save, sha, require_sources, dependency_audit
from .review import check_review_receipt
from ..composition.schema import digest, load_library, require_valid
from .preview import pose
from ..component_physics.contract import world_port

ROOT = reuse.ROOT
REVIEW = ROOT/'results/s3-c-nose-geometry-review-20261006-01'
PREP = ROOT/'results/s3-c-nose-geometry-execution-preparation-20261006-01'
OUT = ROOT/'results/s3-c-nose-geometry-execution-20261006-01'
SPEC = 'b0c22b5759cadbdd9c5fe226be397429601ed457670ead1779d5057e431f7ec3'
SCOPE = 'v11_s3_c_single_nose_geometry_candidate'
CASE_ORDER = ('fixed_nose_flat_brake', 'fixed_nose_three_up')
SCENES = tuple((case, refined) for case in CASE_ORDER for refined in (False, True))
NEW_PATHS = ('src/integration/nose_geometry_execution.py',
             'scripts/s3_c_nose_geometry.py', 'tests/test_c_nose_geometry_execution.py')
PRIOR = ROOT/'results/s3-c-dimension-factorial-execution-20261006-01/round-01'
FROZEN = {'source-manifest.json':'90b03bc9e00c0fecda8e545d0b46dfbc797fd0e7baa8e35ad3167660df293702',
          'fixed-input-manifest.json':'0415736cdf8a02b0d24b0b7d2ad83ff13ce8743e07b268aed657d32220373dba',
          'readonly-manifest.json':'373529f7d50eeb344fa264dd9e5f89898ffa169076b8d618e476ee1b55154852'}
REVIEW_MANIFEST_SHA = '0123303a9b0cdc0528b6bb9e25e92420b2a71063d43ba148bd7c7315f335a10e'
BUDGET = {
    'scene_attempts_per_complete_round':4, 'maximum_rounds':3,
    'maximum_scene_attempts':12, 'local_checks_per_complete_round':8,
    'new_local_execution_checks_limit':24, 'new_local_timeout_s':10,
    'sysml_parse_limit':3, 'sysml_parse_timeout_s':60, 'identity_probe_limit':0,
    'build_timeout_s':180, 'simulation_timeout_s':180, 'postprocess_timeout_s':90,
    'complete_round_reserve_s':2100, 'execution_total_wall_s':6300,
    'product_online_calls':0, 'online_token_limit':0, 'online_cost_limit':0,
    'automatic_implementation_repair_rounds':2,
    'repair_scope':'New adapter/CLI only; same candidate, task, physical laws and criteria; reserve a full 4-scene round before retry',
    'stop':'Physical/task/numerical failure, dependency drift, unresolved process or budget exhaustion stops affected execution; preserve evidence and continue read-only diagnosis',
    'success_closes_unused_budget':True, 'no_old_budget_carry_over':True}
NUMERICS = {v:{'solver':'dassl', 'tolerance':1e-8, 'max_step_s':step,
               'output_interval_s':.001} for v, step in (('base',.0005), ('refined',.00025))}
REPAIR_CODES = reuse.REPAIR_CODES
TerminalFailure, ImplementationFailure = reuse.TerminalFailure, reuse.ImplementationFailure
_maps = reuse._maps


def _clone(fn, namespace):
    result = FunctionType(fn.__code__, namespace, fn.__name__, fn.__defaults__, fn.__closure__)
    result.__kwdefaults__ = deepcopy(fn.__kwdefaults__)
    return result


def _fixed():
    for name, expected in FROZEN.items():
        if sha(PRIOR/name) != expected: raise ValueError('FROZEN_FACTORIAL_MANIFEST_DRIFT')
    fixed, inputs, readonly = (read(PRIOR/name) for name in FROZEN)
    if len(fixed) != 36 or len(previous._manifest_paths(fixed)) != 36 or len(inputs) != 68 or len(readonly) != 1:
        raise ValueError('EXACT_36_SOURCE_68_INPUT_1_HELPER_REQUIRED')
    return fixed, inputs, readonly


def _review_rows():
    manifest = REVIEW/'review-manifest.json'
    if sha(manifest) != REVIEW_MANIFEST_SHA: raise ValueError('FROZEN_REVIEW_MANIFEST_DRIFT')
    rows = read(manifest)['files']
    if len(rows) != 28 or len({row['path'] for row in rows}) != 28:
        raise ValueError('EXACT_28_REVIEW_FILES_REQUIRED')
    result = []
    for row in rows:
        path = (REVIEW/row['path']).resolve(strict=True)
        path.relative_to(REVIEW.resolve())
        if path.name == '.env' or sha(path) != row['sha256']:
            raise ValueError('FROZEN_REVIEW_EVIDENCE_DRIFT')
        result.append({'path':str(path.relative_to(ROOT)), 'sha256':row['sha256']})
    return result


def validate_budget(packet, contract=None):
    if (packet.get('budget') != BUDGET or packet.get('case_order') != list(CASE_ORDER) or
            packet.get('variants') != ['base','refined'] or
            any(type(packet['budget'][k]) is not int for k,v in BUDGET.items() if type(v) is int)):
        raise ValueError('EXACT_FOUR_SCENE_FINITE_NO_ONLINE_BUDGET_REQUIRED')
    if contract is not None and (contract.get('budget_proposal') != BUDGET or
            contract['numerical'] != {**NUMERICS,'keep_event_rows':True}):
        raise ValueError('EXACT_APPROVED_NUMERICS_OR_BUDGET_REQUIRED')
    return deepcopy(BUDGET)


def validate_candidate(bundle, contract, packet):
    """Digest/schema plus independent mass and mechanical frame closure checks."""
    if any(packet[key] != value for key,value in (
            ('bundle_digest',digest(bundle)), ('design_digest',bundle['design']['design_digest']),
            ('physical_digest',bundle['physical']['physical_digest']), ('verification_digest',digest(contract)))):
        raise ValueError('ACTUAL_APPROVED_CANDIDATE_OR_CONTRACT_DRIFT')
    require_valid(bundle['design'],load_library())
    physical = dict(bundle['physical']); checksum = physical.pop('physical_digest')
    if digest(physical) != checksum: raise ValueError('PHYSICAL_SEAL_DRIFT')
    design = bundle['design']
    components = [*design['bodies'],*design['wheels'],*design['drives'],design['power_supply']]
    total = sum(p['count']*p['mass']['value'] for p in components)
    com = [sum(p['count']*p['mass']['value']*pose(p['pose'])[a] for p in components)/total for a in (0,1)]
    inertia = sum(p['count']*(p['inertia']['value']+p['mass']['value']*
                  sum((pose(p['pose'])[a]-com[a])**2 for a in (0,1))) for p in components)
    expected = bundle['reuse_static_reference']['nominal_mass_properties']
    if (total != 85 or total != expected['mass_kg'] or
            any(abs(a-b)>1e-12 for a,b in zip(com,expected['COM_nominal_ground_coordinates_xz_m'])) or
            abs(inertia-expected['locked_inertia_kg_m2'])>1e-12):
        raise ValueError('INDEPENDENT_MASS_COM_INERTIA_MISMATCH')
    all_items = {p['id']:p for p in [*components,*design['joints']]}
    closed = 0
    for connection in design['connections']:
        a_id,a_port = connection['from'].split('.')
        b_id,b_port = connection['to'].split('.')
        a,b = all_items[a_id],all_items[b_id]
        ap,bp = a['ports'][a_port],b['ports'][b_port]
        if ap['type'] == bp['type'] == 'frame2d':
            aw,bw = world_port(pose(a['pose']),pose(ap['pose'])),world_port(pose(b['pose']),pose(bp['pose']))
            if max(abs(x-y) for x,y in zip(aw,bw))>1e-10: raise ValueError('MECHANICAL_FRAME_CLOSURE_FAILURE')
            closed += 1
    if closed != 10: raise ValueError('EXACT_TEN_MECHANICAL_FRAME_CONNECTIONS_REQUIRED')
    wheels = {w['id']:w for w in design['wheels']}
    if wheels['nose']['radius']['value'] != .1 or abs(pose(wheels['nose']['pose'])[0]-pose(wheels['front']['pose'])[0]-.27)>1e-12:
        raise ValueError('EXACT_R100_L270_REQUIRED')
    validate_budget(packet,contract)
    return {'mass_kg':total, 'COM_xz_m':com, 'locked_inertia_kg_m2':inertia, 'frame_connections':closed}


def reviewed_inputs():
    _review_rows()
    packet,bundle,contract = (read(REVIEW/name) for name in ('review-packet.json','proposal.json','verification-contract.json'))
    raw = dict(packet); checksum = raw.pop('specification_digest')
    if checksum != SPEC or digest(raw) != SPEC or packet.get('scope') != SCOPE:
        raise ValueError('EXACT_APPROVED_PHYSICAL_REVIEW_DIGEST_REQUIRED')
    validate_candidate(bundle,contract,packet)
    for row in [*packet['source_identities'],*packet['inputs']]:
        path = (ROOT/row['path']).resolve(strict=True); path.relative_to(ROOT.resolve())
        if path.name == '.env' or sha(path) != row['sha256']: raise ValueError('REVIEW_SOURCE_OR_INPUT_DRIFT')
    return packet,bundle,contract


def guard_sources(packet, manifest, *, allow_new_drift=False):
    fixed,fixed_inputs,readonly = _fixed()
    expected = previous._manifest_paths(fixed) | {str((ROOT/p).resolve()) for p in NEW_PATHS}
    if (packet['fixed_implementation_manifest'] != fixed or packet['fixed_inputs'] != fixed_inputs or
            packet['readonly_manifest'] != readonly or packet['review_evidence'] != _review_rows() or
            previous._manifest_paths(packet['implementation_manifest']) != expected or
            previous._manifest_paths(manifest) != expected or len(expected) != 39):
        raise ValueError('EXACT_36_OLD_PLUS_3_NEW_AND_FROZEN_INPUT_BINDING_REQUIRED')
    current = _maps(manifest)
    if any(current[p] != h for p,h in _maps(fixed).items()): raise ValueError('OLD_36_SOURCES_CANNOT_BE_REPAIRED')
    previous.guard_sources(packet, fixed if allow_new_drift else manifest)
    require_sources(readonly)
    reviewed_inputs()


def generate(bundle, contract, case_id, specification_digest):
    """Regenerate bytes in memory, assert exact frozen previews, retain provenance."""
    if case_id not in CASE_ORDER or specification_digest != SPEC:
        raise ValueError('EXACT_APPROVED_CASE_AND_SPECIFICATION_REQUIRED')
    packet = read(REVIEW/'review-packet.json')
    validate_candidate(bundle,contract,packet)
    result = dimension_revision._isolated_generator(bundle,contract,SPEC)(bundle,case_id)
    preview = REVIEW/(case_id+'-preview')
    for key,name in (('modelica','V11CGenerated.mo'),('sysml','vehicle.sysml')):
        if result[key] != (preview/name).read_text(encoding='utf-8'):
            raise ValueError('FROZEN_PREVIEW_AND_RUNTIME_GENERATION_MISMATCH')
        # Windows source preparation wrote CRLF. Return the exact byte-decoded
        # frozen text, which old runtime writes with newline='\n' unchanged.
        result[key] = (preview/name).read_bytes().decode('utf-8')
    for name,payload in (('V11PhysicalB1.mo',vehicle.isolated_b().encode('utf-8')),
                         ('V11ContactC2.mo',vehicle.CANDIDATE.read_bytes())):
        if payload != (preview/name).read_bytes(): raise ValueError('EXACT_ACCEPTED_B_C2_PREVIEW_REQUIRED')
    result['trace'] = read(preview/'trace.json')
    # Historical source-only preview trace remains frozen; this separate field
    # binds actual candidate execution without relabelling the old preview.
    result['trace']['execution_binding'] = {'specification_digest':SPEC,
        'source_preview_sha256':sha(preview/'trace.json'), 'modelica_sha256':sha(preview/'V11CGenerated.mo'),
        'sysml_sha256':sha(preview/'vehicle.sysml'), 'preview_bytes_preserved':True,
        'execution_adapter':'v11-c-nose-geometry-execution-1'}
    fixed,_,_ = _fixed()
    actual_sources = [*fixed,*[{'path':str((ROOT/p).resolve()),'sha256':sha(ROOT/p)} for p in NEW_PATHS]]
    result['trace']['execution_binding']['actual_source_manifest_digest'] = digest(actual_sources)
    return result


revision = SimpleNamespace(generate=generate)


def prepare():
    packet,bundle,contract = reviewed_inputs()
    fixed,fixed_inputs,readonly = _fixed()
    for name in NEW_PATHS: ast.parse((ROOT/name).read_text(encoding='utf-8'))
    local_targets()
    runtime = deepcopy(packet)
    runtime.update(fixed_implementation_manifest=fixed, fixed_inputs=fixed_inputs,
        readonly_manifest=readonly, review_evidence=_review_rows(),
        implementation_manifest=[*fixed,*[{'path':str((ROOT/p).resolve()),'sha256':sha(ROOT/p)} for p in NEW_PATHS]])
    guard_sources(runtime,runtime['implementation_manifest'])
    generated = {case:generate(bundle,contract,case,SPEC) for case in CASE_ORDER}
    binding = {'physical_review_specification_digest':SPEC, 'approved_review_packet_sha256':sha(REVIEW/'review-packet.json'),
        'runtime_packet':runtime, 'review_manifest_sha256':REVIEW_MANIFEST_SHA,
        'source_manifest_digest':digest(runtime['implementation_manifest']),
        'no_old_ledger_reopened':True, 'source_only_no_tools':True}
    binding['execution_manifest_digest'] = digest(binding)
    PREP.mkdir(exist_ok=False)
    previous.snapshot_inputs(PREP,runtime,bundle,contract,runtime['implementation_manifest'])
    snapshot_readonly(PREP,runtime)
    save(PREP/'execution-manifest.json',binding)
    for case,result in generated.items():
        save(PREP/(case+'-trace.json'),result['trace'])
    save(PREP/'preparation.json',{'status':'SOURCE_ONLY_RUNTIME_BOUND_NOT_RUN', 'specification_digest':SPEC,
        'source_files':39,'fixed_inputs':68,'review_files':28,'new_tests':0,'new_sysml':0,'new_scenes':0,'online':0})
    return binding


def inputs():
    approved,bundle,contract = reviewed_inputs()
    binding = read(PREP/'execution-manifest.json'); raw = dict(binding); checksum = raw.pop('execution_manifest_digest')
    if digest(raw) != checksum or binding['physical_review_specification_digest'] != SPEC:
        raise ValueError('RUNTIME_MANIFEST_DIGEST_DRIFT')
    packet = binding['runtime_packet']
    physical = {k:v for k,v in packet.items() if k in approved}
    if physical != approved or binding['source_manifest_digest'] != digest(packet['implementation_manifest']):
        raise ValueError('APPROVED_PACKET_CHANGED_BY_EXECUTION_PREPARATION')
    guard_sources(packet,packet['implementation_manifest'],allow_new_drift=True)
    return packet,bundle,contract


def initialize(receipt_path):
    path = Path(receipt_path)
    if not path.is_absolute() or path.name == '.env': raise ValueError('EXPLICIT_ABSOLUTE_EXTERNAL_RECEIPT_REQUIRED')
    path = path.resolve(strict=True)
    packet,bundle,contract = inputs(); receipt = read(path)
    check_review_receipt(receipt,packet)
    guard_sources(packet,packet['implementation_manifest'])
    OUT.mkdir(exist_ok=False)
    save(OUT/'authorization.json',receipt)
    save(OUT/'authorization-source.json',{'path':str(path),'sha256':sha(path),'receipt_not_created_by_runner':True})
    save(OUT/'approved-packet.json',packet)
    save(OUT/'approved-physical-review-packet.json',read(REVIEW/'review-packet.json'))
    save(OUT/'execution-manifest.json',read(PREP/'execution-manifest.json'))
    save(OUT/'proposal.json',bundle); save(OUT/'verification-contract.json',contract)
    save(OUT/'policy.json',{'budget':deepcopy(BUDGET),'scope':SCOPE,'specification_digest':SPEC,
        'physical_digest':packet['physical_digest'],'proposal_digest':packet['bundle_digest'],
        'no_prior_budget_carry_over':True,'hard_failure_stops_immediately':True,
        'unknown_failure_is_terminal':True,'C_complete':False,
        'accounting':'actual child wall seconds including failed tools; old balances retired'})
    save(OUT/'initial-state.json',{'status':'APPROVED_NOT_RUN','scenes':0,'local':0,'sysml':0,'online':0})
    return receipt


def approved_inputs():
    packet,bundle,contract = _clone(reuse.approved_inputs,globals())()
    if (read(OUT/'execution-manifest.json') != read(PREP/'execution-manifest.json') or
            read(OUT/'approved-physical-review-packet.json') != read(REVIEW/'review-packet.json')):
        raise ValueError('APPROVED_RUNTIME_OR_PHYSICAL_COPY_DRIFT')
    return packet,bundle,contract


def validate_repair(packet,prior_manifest,result,diagnosis):
    return _clone(reuse.validate_repair,globals())(packet,prior_manifest,result,diagnosis)


def round_manifest(number,packet):
    return _clone(reuse.round_manifest,globals())(number,packet)


def snapshot_readonly(directory,packet):
    return _clone(reuse.snapshot_readonly,globals())(directory,packet)


def require_complete_round(batch):
    used = batch.usage()
    if (batch.budget != BUDGET or used['unresolved_processes'] or used['probe'] or
            used['scene']+4>BUDGET['maximum_scene_attempts'] or
            used['local']+8>BUDGET['new_local_execution_checks_limit'] or
            used['sysml']+1>BUDGET['sysml_parse_limit'] or
            used['process_seconds']+2100>BUDGET['execution_total_wall_s']):
        raise ValueError('COMPLETE_8_LOCAL_1_SYSML_4_SCENE_2100S_ROUND_REQUIRED')


def local_targets():
    def flatten(suite):
        for item in suite:
            if isinstance(item,unittest.TestSuite): yield from flatten(item)
            else: yield item.id()
    rows = list(flatten(unittest.defaultTestLoader.loadTestsFromName('week12.tests.test_c_nose_geometry_execution')))
    if len(rows) != 8 or len(set(rows)) != 8 or any('_FailedTest' in row for row in rows):
        raise ValueError('EXACT_EIGHT_NEW_PREFLIGHT_METHODS_REQUIRED')
    return rows


def build_script(model_class,stop,variable_filter,refined):
    script = previous.build_script(model_class,stop,variable_filter,refined)
    old = f'tolerance={1e-7 if refined else 1e-6:.17g}'
    if script.count(old) != 1: raise ValueError('EXACT_ONE_OLD_BUILD_TOLERANCE_REQUIRED')
    return script.replace(old,f'tolerance={1e-8:.17g}',1)


def check_actual_numerics(folder,command,case,refined):
    path = Path(folder)/'model_init.xml'
    nodes = list(ET.parse(path).getroot().iter('DefaultExperiment'))
    numerics = NUMERICS['refined' if refined else 'base']
    expected = {'startTime':0.,'stopTime':case['stop_time_s'],'stepSize':.001,'tolerance':1e-8}
    if (len(nodes) != 1 or any(float(nodes[0].get(k,'nan')) != v for k,v in expected.items()) or
            nodes[0].get('solver') != 'dassl' or nodes[0].get('outputFormat') != 'csv' or
            command[1:] != ['-s=dassl',f'-maxStepSize={numerics["max_step_s"]}','-r=result.csv']):
        raise ValueError('ACTUAL_XML_OR_ARGV_NUMERICS_DRIFT_BEFORE_SIMULATION')
    return {'status':'PASS_BEFORE_SIMULATION','numerics':deepcopy(numerics),'default_experiment':expected,
            'init_xml_sha256':sha(path),'runner_command':command,
            'launch_command':[*command,'-lv=LOG_STATS'],'specification_digest':SPEC}


class _NumericsBatch:
    def __init__(self,batch,case,refined): self._batch,self._case,self._refined = batch,case,refined
    def __getattr__(self,name): return getattr(self._batch,name)
    def run(self,command,cwd,timeout,log_dir,label):
        if label == 'simulation':
            actual = check_actual_numerics(cwd,command,self._case,self._refined)
            save(Path(log_dir)/'actual-numerics.json',actual)
            command = actual['launch_command']
        return self._batch.run(command,cwd,timeout,log_dir,label)


def execute_scene(batch,directory,bundle,contract,case_id,refined,number,manifest,packet,base=None):
    # _VehicleProxy.generate itself references revision in its own frozen globals:
    # use a new proxy instead of silently leaving the old revision validation.
    class CandidateProxy:
        def __getattr__(self,name):
            if name == 'generate': return lambda b,c:generate(b,contract,c,SPEC)
            return getattr(vehicle,name)
    namespace = dict(previous.execute_scene.__globals__,amendment=CandidateProxy(),
                     guard_sources=guard_sources,build_script=build_script)
    case = next(c for c in bundle['physical']['cases'] if c['id']==case_id)
    return _clone(previous.execute_scene,namespace)(_NumericsBatch(batch,case,refined),directory,
        bundle,contract,case_id,refined,number,manifest,packet,base)


def _completed_build_evidence(folder):
    return _clone(reuse._completed_build_evidence,globals())(folder)


def classify_scene(row,folder):
    return _clone(reuse.classify_scene,globals())(row,folder)


def run_round(number):
    """Execute the audited old sequential round through this private namespace."""
    try:
        return _clone(reuse.run_round,globals())(number)
    except Exception as exc:
        # The frozen round checks approval, manifests and unresolved launches
        # before acquiring a round directory. Preserve and close a genuinely
        # initialized idle ledger even when failure occurs at that boundary.
        # An existing lock might belong to a running owner; never unlink it or
        # overwrite its report based only on a second command's observation.
        if ((OUT/'policy.json').is_file() and not (OUT/'final-result.json').exists() and
                not (OUT/'.execution.lock').exists()):
            policy = read(OUT/'policy.json')
            if policy.get('specification_digest') == SPEC and policy.get('scope') == SCOPE:
                batch = Batch(OUT,SPEC)
                final = {'round':number,'status':'STOPPED','failure_code':'TERMINAL',
                    'error':type(exc).__name__+': '+str(exc),'retry_eligible':False,
                    'specification_digest':SPEC,'usage':batch.usage(),'C_complete':False,
                    'unused_budget_disposition':'RETIRED','automatic_additional_scene_permitted':False,
                    'stop_boundary':'before_new_round; existing reservations and process records preserved'}
                save(OUT/'final-result.json',final)
                return final
        raise
