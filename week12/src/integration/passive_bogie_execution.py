"""Frozen passive-bogie previews, external approval and one finite ledger.

No old generator is called and no archived proposal or result is rewritten.
The historical pending labels are provenance; only an external receipt grants
execution. All test/parser/build/simulation/checker children go through Batch.
"""
from copy import deepcopy
import ast
import os
from pathlib import Path
import re
import sys
from types import FunctionType, SimpleNamespace
import unittest
import uuid
import xml.etree.ElementTree as ET

from . import nose_geometry_execution as inherited
from . import dimension_execution as reuse
from . import vehicle_amendment_execution as previous
from . import vehicle_amendment as vehicle
from .execution import Batch, read, save, sha, require_sources, dependency_audit
from .review import check_review_receipt
from .output_contract import vehicle_output_names, variable_filter
from ..composition.schema import digest, load_library, require_valid
from .preview import pose
from ..component_physics.contract import world_port

ROOT = reuse.ROOT
REVIEW = ROOT/'results/s3-c-passive-bogie-preparation-20261006-01'
PREP = ROOT/'results/s3-c-passive-bogie-execution-preparation-20261006-01'
OUT = ROOT/'results/s3-c-passive-bogie-execution-20261006-01'
SPEC = '3048bd7ac34cb8814114743fc84b8636e6ece72b18d55033624763be1333d030'
SCOPE = 'v11_s3_c_single_passive_bogie_candidate'
CASE_ORDER = ('passive_spring_probe','passive_bogie_flat_brake','passive_bogie_three_up')
SCENES = tuple((case,refined) for case in CASE_ORDER for refined in (False,True))
NEW_PATHS = ('src/integration/passive_bogie_execution.py','src/integration/passive_bogie_verify.py',
             'scripts/s3_c_passive_bogie.py','tests/test_c_passive_bogie_execution.py')
REVIEW_SHA = 'd6cdfed0e265bbb20832b65981dd4989b6b737ef12cb00dcb3e494982856bb84'
CONTRACT_SHA = '575f85c1a7dfd5504789f56050420464dc6040b5aba181b7ef1fa404e1051ed9'
PROPOSAL_SHA = '03e723ff17291bd705cb062cdec30268e16e766638057166944783a1a3abe483'
INHERITED_SHA = '50e739144521272fe7b948a47ed511420762dbae04232c3339805c8c1e6cb624'
BUDGET = read(REVIEW/'verification-contract.json')['budget_proposal']
NUMERICS = read(REVIEW/'verification-contract.json')['numerical']
JOINT_OUTPUTS = ('bogieRelativeAngle','bogieRelativeSpeed','bogieSpringEnergy','bogieRestoringTorque')
PROBE_OUTPUTS = ('time','q','w','alpha','springTorque','elasticEnergy','totalEnergy',
                 'negativeTorque','zeroTorque','positiveTorque')
REPAIR_CODES = reuse.REPAIR_CODES | frozenset(('NEW_POSTPROCESS_INTERFACE',))
TerminalFailure, ImplementationFailure = reuse.TerminalFailure,reuse.ImplementationFailure
_maps = reuse._maps


def _clone(fn, namespace):
    result = FunctionType(fn.__code__,namespace,fn.__name__,fn.__defaults__,fn.__closure__)
    result.__kwdefaults__ = deepcopy(fn.__kwdefaults__)
    return result


def _fixed():
    fixed,inputs,readonly = inherited._fixed()
    path = ROOT/'src/integration/nose_geometry_execution.py'
    if sha(path) != INHERITED_SHA: raise ValueError('IMMUTABLE_REUSE_HELPER_DRIFT')
    return [*fixed,{'path':str(path.resolve()),'sha256':INHERITED_SHA}],inputs,readonly


def _review_rows():
    for name,expected in (('review-packet.json',REVIEW_SHA),('proposal.json',PROPOSAL_SHA),
                           ('verification-contract.json',CONTRACT_SHA)):
        if sha(REVIEW/name) != expected: raise ValueError('FROZEN_APPROVED_REVIEW_DRIFT')
    packet = read(REVIEW/'review-packet.json')
    rows = [{'path':str((REVIEW/name).relative_to(ROOT)),'sha256':checksum}
            for name,checksum in packet['source_preview_sha256'].items()]
    rows += [{'path':str((REVIEW/name).relative_to(ROOT)),'sha256':checksum}
             for name,checksum in (('review-packet.json',REVIEW_SHA),('proposal.json',PROPOSAL_SHA),
                                    ('verification-contract.json',CONTRACT_SHA))]
    for row in rows:
        path = (ROOT/row['path']).resolve(strict=True); path.relative_to(REVIEW.resolve())
        if path.name == '.env' or sha(path) != row['sha256']: raise ValueError('FROZEN_PREVIEW_BYTES_DRIFT')
    return rows


def validate_budget(packet,contract=None):
    if (packet.get('budget') != BUDGET or packet.get('case_order') != list(CASE_ORDER) or
            packet.get('variants') != ['base','refined'] or
            any(type(packet['budget'][k]) is not int for k,v in BUDGET.items() if type(v) is int)):
        raise ValueError('EXACT_18_SCENE_3_ROUND_9000S_NO_ONLINE_BUDGET_REQUIRED')
    if contract is not None and (contract['budget_proposal'] != BUDGET or
                                contract['numerical'] != NUMERICS):
        raise ValueError('APPROVED_CONTRACT_BUDGET_OR_NUMERICS_DRIFT')
    return deepcopy(BUDGET)


def validate_candidate(bundle,contract,packet):
    if (digest(bundle) != SPEC or packet['bundle_digest'] != SPEC or
            packet['design_digest'] != bundle['design']['design_digest'] or
            packet['physical_digest'] != bundle['physical']['physical_digest'] or
            packet['verification_digest'] != digest(contract)):
        raise ValueError('EXACT_APPROVED_BUNDLE_AND_CONTRACT_REQUIRED')
    require_valid(bundle['design'],load_library())
    design = bundle['design']
    components = [*design['bodies'],*design['wheels'],*design['drives'],design['power_supply']]
    total = sum(p['count']*p['mass']['value'] for p in components)
    com = [sum(p['count']*p['mass']['value']*pose(p['pose'])[axis] for p in components)/total for axis in (0,1)]
    inertia = sum(p['count']*(p['inertia']['value']+p['mass']['value']*
                  sum((pose(p['pose'])[axis]-com[axis])**2 for axis in (0,1))) for p in components)
    expected = bundle['static_preparation']
    if (total != 85 or max(abs(a-b) for a,b in zip(com,expected['COM_nominal_ground_xz_m']))>1e-12 or
            abs(inertia-expected['locked_inertia_nominal_kg_m2'])>1e-12):
        raise ValueError('INDEPENDENT_MASS_COM_SEPARATE_INERTIA_MISMATCH')
    items = {p['id']:p for p in [*components,*design['joints']]}
    closed = 0
    for connection in design['connections']:
        aid,aport = connection['from'].split('.'); bid,bport = connection['to'].split('.')
        a,b = items[aid],items[bid]; ap,bp = a['ports'][aport],b['ports'][bport]
        if ap['type'] == bp['type'] == 'frame2d':
            if max(abs(x-y) for x,y in zip(world_port(pose(a['pose']),pose(ap['pose'])),
                    world_port(pose(b['pose']),pose(bp['pose']))))>1e-10:
                raise ValueError('MECHANICAL_FRAME_CLOSURE_FAILURE')
            closed += 1
    validate_budget(packet,contract)
    return {'mass_kg':total,'COM_xz_m':com,'locked_inertia_kg_m2':inertia,'frame_connections':closed}


def reviewed_inputs():
    _review_rows()
    review,bundle,contract = (read(REVIEW/name) for name in ('review-packet.json','proposal.json','verification-contract.json'))
    packet = {'scope':SCOPE,'specification_digest':SPEC,'bundle_digest':SPEC,
        'design_digest':review['design_digest'],'physical_digest':review['physical_digest'],
        'verification_digest':digest(contract),'case_order':list(CASE_ORDER),'variants':['base','refined'],
        'budget':deepcopy(BUDGET)}
    validate_candidate(bundle,contract,packet)
    refs = [row for probe in contract['reused_probe_evidence'] for row in probe['files']]
    if len(refs) != 18: raise ValueError('EXACT_18_REUSED_PROBE_REFS_REQUIRED')
    for row in refs:
        path = (ROOT/row['path']).resolve(strict=True); path.relative_to(ROOT.resolve())
        if sha(path) != row['sha256']: raise ValueError('REUSED_PROBE_EVIDENCE_DRIFT')
    return packet,bundle,contract


def guard_sources(packet,manifest,*,allow_new_drift=False):
    fixed,inputs,readonly = _fixed()
    expected = previous._manifest_paths(fixed) | {str((ROOT/p).resolve()) for p in NEW_PATHS}
    if (packet['fixed_implementation_manifest'] != fixed or packet['fixed_inputs'] != inputs or
            packet['readonly_manifest'] != readonly or packet['review_evidence'] != _review_rows() or
            previous._manifest_paths(packet['implementation_manifest']) != expected or
            previous._manifest_paths(manifest) != expected):
        raise ValueError('EXACT_IMMUTABLE_AND_NEW_MANIFEST_BINDING_REQUIRED')
    if any(_maps(manifest)[p] != h for p,h in _maps(fixed).items()):
        raise ValueError('OLD_SOURCES_CANNOT_BE_REPAIRED')
    previous.guard_sources(packet,fixed if allow_new_drift else manifest)
    require_sources(readonly)
    reviewed_inputs()


def generate(bundle,contract,case_id,specification_digest):
    if case_id not in CASE_ORDER or specification_digest != SPEC:
        raise ValueError('EXACT_APPROVED_CASE_AND_SPEC_REQUIRED')
    packet,_,_ = reviewed_inputs(); validate_candidate(bundle,contract,packet)
    preview_case = 'passive_bogie_flat_brake' if case_id == CASE_ORDER[0] else case_id
    preview = REVIEW/(preview_case+'-preview')
    source = (preview/'V11CGenerated.mo').read_bytes().decode('utf-8')
    names = PROBE_OUTPUTS if case_id == CASE_ORDER[0] else (*vehicle_output_names(bundle['design']),*JOINT_OUTPUTS)
    result = {'modelica':source,'sysml':(preview/'vehicle.sysml').read_bytes().decode('utf-8'),
              'model_class':'V11PassiveBogieDraft.SpringProbe' if case_id == CASE_ORDER[0] else 'V11CGenerated.Scene',
              'variable_filter':variable_filter(names),'trace':read(preview/'preview-trace.json')}
    result['trace']['execution_binding'] = {'specification_digest':SPEC,'case_id':case_id,
        'frozen_preview_bytes_preserved':True,'preview_case':preview_case,'approved_receipt_supersedes_pending_labels':True,
        'modelica_sha256':sha(preview/'V11CGenerated.mo'),'sysml_sha256':sha(preview/'vehicle.sysml'),
        'runtime_B_preview_sha256':sha(preview/'V11PhysicalB1.mo'),
        'runtime_C2_preview_sha256':sha(preview/'V11ContactC2.mo'),
        'spring_sha256':sha(preview/'V11PassiveBogieDraft.mo'),'model_class':result['model_class'],
        'runtime_output_filter':result['variable_filter'],
        'old_B_runtime_hash_is_historical_provenance':True}
    if (preview/'V11PhysicalB1.mo').read_text(encoding='utf-8') != vehicle.isolated_b().replace('\r\n','\n'):
        raise ValueError('B_PREVIEW_NORMALIZED_SOURCE_DRIFT')
    if (preview/'V11ContactC2.mo').read_bytes() != vehicle.CANDIDATE.read_bytes():
        raise ValueError('ACCEPTED_C2_BYTES_DRIFT')
    return result


revision = SimpleNamespace(generate=generate)


def runtime_packet():
    packet,bundle,contract = reviewed_inputs(); fixed,inputs,readonly = _fixed()
    packet.update(fixed_implementation_manifest=fixed,fixed_inputs=inputs,readonly_manifest=readonly,
        review_evidence=_review_rows(),implementation_manifest=[*fixed,*[
            {'path':str((ROOT/p).resolve()),'sha256':sha(ROOT/p)} for p in NEW_PATHS]])
    guard_sources(packet,packet['implementation_manifest'])
    return packet,bundle,contract


def snapshot_readonly(directory,packet):
    return _clone(reuse.snapshot_readonly,globals())(directory,packet)


def prepare():
    packet,bundle,contract = runtime_packet()
    for name in NEW_PATHS: ast.parse((ROOT/name).read_text(encoding='utf-8'))
    local_targets()
    generated = {case:generate(bundle,contract,case,SPEC) for case in CASE_ORDER}
    binding = {'physical_review_specification_digest':SPEC,'approved_review_packet_sha256':REVIEW_SHA,
        'approved_contract_sha256':CONTRACT_SHA,'runtime_packet':packet,
        'source_manifest_digest':digest(packet['implementation_manifest']),
        'no_old_ledger_reopened':True,'source_only_no_tools':True}
    binding['execution_manifest_digest'] = digest(binding)
    PREP.mkdir(exist_ok=False)
    previous.snapshot_inputs(PREP,packet,bundle,contract,packet['implementation_manifest'])
    snapshot_readonly(PREP,packet); save(PREP/'execution-manifest.json',binding)
    for case,result in generated.items(): save(PREP/(case+'-trace.json'),result['trace'])
    save(PREP/'preparation.json',{'status':'SOURCE_ONLY_RUNTIME_BOUND_NOT_RUN','specification_digest':SPEC,
        'new_tests':0,'new_sysml':0,'new_scenes':0,'online':0,'identity_probes':0})
    return binding


def inputs():
    expected,bundle,contract = reviewed_inputs(); binding = read(PREP/'execution-manifest.json')
    raw = dict(binding); checksum = raw.pop('execution_manifest_digest')
    packet = binding['runtime_packet']
    if (digest(raw) != checksum or binding['physical_review_specification_digest'] != SPEC or
            {k:packet[k] for k in expected} != expected or
            binding['source_manifest_digest'] != digest(packet['implementation_manifest']) or
            binding['approved_review_packet_sha256'] != REVIEW_SHA or
            binding['approved_contract_sha256'] != CONTRACT_SHA): raise ValueError('RUNTIME_BINDING_DRIFT')
    guard_sources(packet,packet['implementation_manifest'],allow_new_drift=True)
    return packet,bundle,contract


def check_receipt(receipt,packet):
    check_review_receipt(receipt,packet)
    if (receipt.get('source_review_packet_sha256') != REVIEW_SHA or
            receipt.get('verification_contract_sha256') != CONTRACT_SHA):
        raise ValueError('EXACT_REVIEW_AND_CONTRACT_APPROVAL_REQUIRED')


def initialize(receipt_path):
    path = Path(receipt_path)
    if not path.is_absolute() or path.name == '.env': raise ValueError('EXPLICIT_ABSOLUTE_EXTERNAL_RECEIPT_REQUIRED')
    path = path.resolve(strict=True); packet,bundle,contract = inputs(); receipt = read(path)
    check_receipt(receipt,packet); guard_sources(packet,packet['implementation_manifest'])
    OUT.mkdir(exist_ok=False)
    save(OUT/'authorization.json',receipt)
    save(OUT/'authorization-source.json',{'path':str(path),'sha256':sha(path),'receipt_not_created_by_runner':True})
    save(OUT/'approved-packet.json',packet); save(OUT/'proposal.json',bundle)
    save(OUT/'verification-contract.json',contract); save(OUT/'execution-manifest.json',read(PREP/'execution-manifest.json'))
    save(OUT/'policy.json',{'budget':deepcopy(BUDGET),'scope':SCOPE,'specification_digest':SPEC,
        'physical_digest':packet['physical_digest'],'proposal_digest':SPEC,'C_complete':False,
        'no_prior_budget_carry_over':True,'hard_failure_stops_immediately':True,'unknown_failure_is_terminal':True,
        'accounting':'Sum actual child wall seconds; old ledgers retired; source preparation is not approval'})
    save(OUT/'initial-state.json',{'status':'APPROVED_NOT_RUN','scenes':0,'local':0,'sysml':0,'online':0})
    return receipt


def approved_inputs():
    packet,bundle,contract = _clone(reuse.approved_inputs,{**globals(),'check_review_receipt':check_receipt})()
    if read(OUT/'execution-manifest.json') != read(PREP/'execution-manifest.json'):
        raise ValueError('APPROVED_EXECUTION_MANIFEST_DRIFT')
    return packet,bundle,contract


def validate_repair(packet,prior_manifest,result,diagnosis):
    return _clone(reuse.validate_repair,globals())(packet,prior_manifest,result,diagnosis)


def round_manifest(number,packet):
    return _clone(reuse.round_manifest,globals())(number,packet)


def require_complete_round(batch):
    used = batch.usage()
    if (batch.budget != BUDGET or used['unresolved_processes'] or used['probe'] or
            used['scene']+6>BUDGET['maximum_scene_attempts'] or used['local']+12>BUDGET['new_local_execution_checks_limit'] or
            used['sysml']+1>BUDGET['sysml_parse_limit'] or used['process_seconds']+3000>BUDGET['execution_total_wall_s']):
        raise ValueError('COMPLETE_12_LOCAL_1_SYSML_6_SCENE_3000S_ROUND_REQUIRED')


def local_targets():
    def flatten(suite):
        for item in suite:
            if isinstance(item,unittest.TestSuite): yield from flatten(item)
            else: yield item.id()
    rows = list(flatten(unittest.defaultTestLoader.loadTestsFromName('week12.tests.test_c_passive_bogie_execution')))
    if len(rows) != 12 or len(set(rows)) != 12 or any('_FailedTest' in row for row in rows):
        raise ValueError('EXACT_TWELVE_NEW_PREFLIGHT_CHECKS_REQUIRED')
    return rows


def build_script(model_class,stop,variable_filter,refined):
    script = inherited.build_script(model_class,stop,variable_filter,refined)
    marker = 'ok := loadFile("V11PhysicalB1.mo");'
    insertion = ('spring := loadFile("V11PassiveBogieDraft.mo");\n'
        'print("__C_SPRING_LOAD__="+String(spring)+"\\n");\n'
        'print(getErrorString()+"\\n");\nif not spring then exit(1); end if;\n')
    if script.count(marker) != 1: raise ValueError('B_LOAD_MARKER_REQUIRED')
    script = script.replace(marker,insertion+marker,1)
    if model_class == 'V11PassiveBogieDraft.SpringProbe':
        old = 'numberOfIntervals='+str(round(stop/.001))
        if script.count(old) != 1: raise ValueError('OUTPUT_INTERVAL_MARKER_REQUIRED')
        script = script.replace(old,'numberOfIntervals='+str(round(stop/.0001)),1)
    return script


def case_definition(bundle,contract,case_id):
    if case_id == CASE_ORDER[0]: return contract['passive_spring_probe']
    return next(c for c in bundle['physical']['cases'] if c['id'] == case_id)


def check_actual_numerics(folder,command,case,refined):
    path = Path(folder)/'model_init.xml'; nodes = list(ET.parse(path).getroot().iter('DefaultExperiment'))
    variant = 'refined' if refined else 'base'
    numerical = deepcopy(case['variants'][variant] if case['id'] == CASE_ORDER[0] else NUMERICS[variant])
    numerical['keep_event_rows'] = True
    expected = {'startTime':0.,'stopTime':case['stop_time_s'],'stepSize':numerical['output_interval_s'],'tolerance':1e-8}
    if (len(nodes) != 1 or any(float(nodes[0].get(k,'nan')) != v for k,v in expected.items()) or
            nodes[0].get('solver') != 'dassl' or nodes[0].get('outputFormat') != 'csv' or
            command[1:] != ['-s=dassl',f'-maxStepSize={numerical["max_step_s"]}','-r=result.csv']):
        raise ValueError('ACTUAL_XML_OR_ARGV_NUMERICS_DRIFT_BEFORE_SIMULATION')
    return {'status':'PASS_BEFORE_SIMULATION','numerics':numerical,'default_experiment':expected,
        'init_xml_sha256':sha(path),'runner_command':command,'launch_command':[*command,'-lv=LOG_STATS'],
        'specification_digest':SPEC}


def execute_scene(batch,directory,bundle,contract,case_id,refined,number,manifest,packet,base=None):
    from ..component_physics.runner import OMC,MSL,build_observations
    from ..component_physics.event_codegen import audit_source
    from ..toolchain import _resolve_generated
    from .contact_repair_execution import ownership_audit
    guard_sources(packet,manifest); case = case_definition(bundle,contract,case_id)
    name = case_id+('-refined' if refined else '-base')
    reservation = batch.reserve('scene',name,number); folder = directory/name; folder.mkdir(exist_ok=False)
    result = {'case':case_id,'variant':'refined' if refined else 'base','status':'FAIL','reservation':reservation,
              'compile':'NOT_RUN','simulation':'NOT_RUN','physical':'UNVERIFIED','numeric':'UNVERIFIED'}
    try:
        generated = generate(bundle,contract,case_id,SPEC)
        preview = REVIEW/(generated['trace']['execution_binding']['preview_case']+'-preview')
        for filename in ('V11CGenerated.mo','V11PhysicalB1.mo','V11ContactC2.mo','V11PassiveBogieDraft.mo'):
            with (folder/filename).open('xb') as stream: stream.write((preview/filename).read_bytes())
            if sha(folder/filename) != sha(preview/filename): raise ValueError('ACTUAL_FROZEN_BYTES_DRIFT')
        save(folder/'trace.json',generated['trace'])
        save(folder/'input-identity.json',{'specification_digest':SPEC,'proposal_digest':SPEC,
            'physical_digest':packet['physical_digest'],'design_digest':packet['design_digest'],
            'implementation_digest':digest(manifest),'actual_bytes':{name:sha(folder/name) for name in
                ('V11CGenerated.mo','V11PhysicalB1.mo','V11ContactC2.mo','V11PassiveBogieDraft.mo')}})
        with (folder/'build.mos').open('x',encoding='utf-8',newline='\n') as stream:
            stream.write(build_script(generated['model_class'],case['stop_time_s'],generated['variable_filter'],refined))
        proc = batch.run([str(OMC),'build.mos'],folder,BUDGET['build_timeout_s'],folder,'build')
        result.update(build_observations(proc['stdout']))
        if proc['returncode'] or proc['timed_out']: raise RuntimeError('BUILD_TOOL_FAILURE')
        if result['model_check'] != 'PASS': raise RuntimeError('MODEL_CHECK_FAILED')
        if result['observed_msl_version'] != '4.0.0' or Path(result['observed_msl_source']).resolve() != MSL.resolve():
            raise RuntimeError('MSL_IDENTITY_DRIFT')
        executable = _resolve_generated(folder,result['reported_executable'])
        if executable is None: raise RuntimeError('NO_GENERATED_EXECUTABLE')
        result['compile'] = 'PASS'
        event = audit_source((folder/'model_05evt.c').read_text(encoding='utf-8'))
        if case_id == CASE_ORDER[0]:
            owner = {'status':'NOT_APPLICABLE_ANALYTIC_SPRING_NO_TERRAIN','scope':'no terrain/contact components instantiated'}
            if event['issues'] == ['No cached zero-crossing relations observed']:
                event['status'] = 'NOT_APPLICABLE_NO_RELATIONS_IN_ANALYTIC_PROBE'
        elif all(point[1] == case['terrain_points'][0][1] for point in case['terrain_points']):
            owner = {'status':'NOT_APPLICABLE_NO_CONVEX_CORNER','scope':'flat terrain'}
        else: owner = ownership_audit(folder,case_id)
        save(folder/'event-codegen.json',event); save(folder/'owner-audit.json',owner)
        result.update(event_codegen=event['status'],owner_audit=owner['status'])
        if event['status'] != 'PASS' and not event['status'].startswith('NOT_APPLICABLE'):
            raise RuntimeError('GENERATED_EVENT_CACHE_UNVERIFIED')
        if owner['status'] not in ('PASS','NOT_APPLICABLE_NO_CONVEX_CORNER','NOT_APPLICABLE_ANALYTIC_SPRING_NO_TERRAIN'):
            raise RuntimeError('GENERATED_CONTACT_OWNER_UNVERIFIED')
        step = NUMERICS[result['variant']]['max_step_s']
        actual = check_actual_numerics(folder,[str(executable),'-s=dassl',f'-maxStepSize={step}','-r=result.csv'],case,refined)
        save(folder/'actual-numerics.json',actual); result['simulation'] = 'LAUNCH_REQUESTED'
        proc = batch.run(actual['launch_command'],folder,BUDGET['simulation_timeout_s'],folder,'simulation')
        result['simulation'] = 'COMPLETED' if not proc['returncode'] and not proc['timed_out'] else 'FAILED_OR_TIMED_OUT'
        if result['simulation'] != 'COMPLETED': raise RuntimeError('SIMULATION_FAILURE_REQUIRES_DIAGNOSIS')
        command = [sys.executable,'-B','-m','week12.scripts.s3_c_passive_bogie','--verify-scene',str(folder),
            '--bundle',str(directory/'proposal.json'),'--contract',str(directory/'verification-contract.json'),'--case',case_id]
        if base is not None: command += ['--base',str(base)]
        proc = batch.run(command,ROOT.parent,BUDGET['postprocess_timeout_s'],folder,'verification')
        for key,filename in (('physical','physical.json'),('numeric','numerical.json')):
            if (folder/filename).is_file(): result[key] = read(folder/filename)['status']
        if proc['returncode'] or proc['timed_out']: raise RuntimeError('PHYSICAL_NUMERIC_OR_POSTPROCESS_FAILURE')
        if result['physical'] != 'PASS' or (refined and result['numeric'] != 'PASS'):
            raise RuntimeError('INCOMPLETE_PHYSICAL_OR_NUMERIC_EVIDENCE')
        guard_sources(packet,manifest); result['status'] = 'PASS'
    except Exception as exc: result['error'] = type(exc).__name__+': '+str(exc)
    finally: save(folder/'result.json',result)
    print(name,result['status'],result.get('error',''),flush=True)
    return result,folder


def _completed_build_evidence(folder):
    return _clone(reuse._completed_build_evidence,globals())(folder)


def classify_scene(row,folder):
    if row.get('physical') == 'FAIL' or row.get('numeric') == 'FAIL':
        raise TerminalFailure('PHYSICAL_TASK_OR_NUMERICAL_FAIL')
    if row.get('error') == 'RuntimeError: PHYSICAL_NUMERIC_OR_POSTPROCESS_FAILURE' and folder is not None:
        records = []
        for path in OUT.glob('process-*.start.json'):
            started = read(path)
            command = started.get('command',[])
            matches = (command.count('--verify-scene') == 1 and
                command.index('--verify-scene')+1 < len(command) and
                Path(command[command.index('--verify-scene')+1]).resolve() == folder.resolve())
            if started.get('label') == 'verification' and matches:
                finished = path.with_name(path.name.replace('.start.json','.result.json'))
                if finished.is_file(): records.append(read(finished))
        text = (folder/'verification.stderr.log').read_text(encoding='utf-8') if (folder/'verification.stderr.log').is_file() else ''
        local_paths = [str((ROOT/p).resolve()) for p in NEW_PATHS if not p.startswith('tests/')]
        if (len(records) == 1 and type(records[0].get('returncode')) is int and records[0]['returncode'] != 0 and
                all(records[0].get(k) is False for k in ('timed_out','interrupted','uncertain')) and
                'Traceback (most recent call last)' in text and any(p in text for p in local_paths) and
                re.search(r'(?m)^(ImportError|ModuleNotFoundError|KeyError|TypeError|AttributeError|NameError|SyntaxError):',text)):
            raise ImplementationFailure('NEW_POSTPROCESS_INTERFACE')
    return _clone(reuse.classify_scene,globals())(row,folder)


def run_round(number):
    try:
        # This audited sequential runner uses this namespace, the three explicit
        # cases, twelve local methods and full-round budget. generate(probe)
        # deliberately supplies the actual articulated flat vehicle SysML.
        return _clone(reuse.run_round,globals())(number)
    except Exception as exc:
        if ((OUT/'policy.json').is_file() and not (OUT/'final-result.json').exists() and
                not (OUT/'.execution.lock').exists()):
            policy = read(OUT/'policy.json')
            if policy.get('specification_digest') == SPEC and policy.get('scope') == SCOPE:
                batch = Batch(OUT,SPEC)
                final = {'round':number,'status':'STOPPED','failure_code':'TERMINAL',
                    'error':type(exc).__name__+': '+str(exc),'retry_eligible':False,
                    'specification_digest':SPEC,'usage':batch.usage(),'C_complete':False,
                    'unused_budget_disposition':'RETIRED','automatic_additional_scene_permitted':False,
                    'stop_boundary':'before_new_round; all process records retained'}
                save(OUT/'final-result.json',final); return final
        raise
