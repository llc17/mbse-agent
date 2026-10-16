"""Source-only C2/contact and circular nose-housing amendment, pending approval.

The old generator and approved inputs remain immutable.  This adapter reuses
their identical vehicle dynamics and records the separately loaded contact
replacement.  Housing geometry is consumed by the independent verifier only.
"""
from copy import deepcopy
import hashlib
from .execution import ROOT,read,sha
from .timing_amendment import inputs as previous_inputs, SPEC as PREVIOUS_SPEC
from . import model_generator as previous_generator
from .contact_repair_execution import isolated_b as contact_bound_b
from ..composition.schema import digest

REVIEW=ROOT/'results/s3-c-vehicle-c2-review-20261005-01'
OUT=ROOT/'results/s3-c-vehicle-c2-execution-20261005-01'
CANDIDATE=ROOT/'models/components/V11ContactC2/package.mo'
B_SOURCE=ROOT/'models/components/V11PhysicalB1/package.mo'
CONTACT_EVIDENCE=ROOT/'results/s3-c-contact-repair-execution-20260926-01'
C2_SHA='d7f0cea839635c595c2d1ee5a3a807c144330015257399da7fc306c35b851929'
VERSION='v11-c2-circular-housing-amendment-1'
CASE_ORDER=['fixed_nose_flat_brake','fixed_nose_three_up','fixed_nose_three_down',
    'fixed_nose_twenty_up','fixed_nose_twenty_down','fixed_nose_flat_energy','fixed_nose_full_mission']
CIRCLE={'type':'capsule','start':[0.,0.],'end':[0.,0.],'radius':.05}


def _seal_physical(physical):
    physical.pop('physical_digest',None)
    physical['physical_digest']=digest(physical)


def build_draft():
    _,original,old_contract=previous_inputs()
    accepted=read(CONTACT_EVIDENCE/'final-result.json')
    if accepted['status']!='PASS_ISOLATED_CONTACT_VARIANT_C_INCOMPLETE' or sha(CANDIDATE)!=C2_SHA or accepted['candidate_sha256']!=C2_SHA:
        raise ValueError('ACCEPTED_C2_REQUIRED')
    bundle=deepcopy(original); physical=bundle['physical']; contract=deepcopy(old_contract)
    physical['envelopes_local_xz']['nose_drive']=[deepcopy(CIRCLE)]
    physical['status']='PROPOSED_C2_AND_CIRCULAR_HOUSING_NOT_AUTHORIZED'
    physical['contact_implementation']={
        'class':'V11ContactC2.PolylineWheelContact','source_sha256':C2_SHA,
        'original_B_sha256':sha(B_SOURCE),
        'runtime_B_copy_sha256':hashlib.sha256(contact_bound_b().encode('utf-8')).hexdigest(),
        'binding':'isolated V11PhysicalB1 copy: contact model alias to accepted C2; original B file unchanged',
        'evidence':str((CONTACT_EVIDENCE/'final-result.json').relative_to(ROOT)),
        'evidence_sha256':sha(CONTACT_EVIDENCE/'final-result.json')}
    physical['housing_amendment']={
        'component_id':'nose_drive','origin':'AI design requirement pending user confirmation',
        'xz_envelope':'axle-centred circle, maximum radius50mm; no protrusion beyond this envelope',
        'mass_inertia_pose_ratings':'unchanged from approved inputs',
        'limits':'not a selected commercial motor; 3D axial layout, thermal performance and actual packaging not validated',
        'dynamic_effect':'envelope has no contact reaction; changing it alone does not alter the trajectory',
        'conditional_clearance_m':.0095,
        'clearance_condition':'same centre as R70mm nose wheel and true wheel geometric penetration <=10.5mm',
        'clearance_bound_from_existing_two_gates_m':.009,
        'gate_bound_condition':'reported penetration<=10.5mm plus absolute geometry discrepancy<=0.5mm; roundoff excluded; direct nonwheel clearance still checked separately',
        'inertia_condition':'original Jyy <= m*(0.05m)^2 is necessary only; not a claim of solid-disc mass distribution'}
    _seal_physical(physical)
    contract['case_order']=list(CASE_ORDER)
    contract['scope']='one unchanged 85kg fixed-nose vehicle, accepted C2 plus explicit circular nose-drive housing; no tuning or second topology'
    contract['budget_proposal']={
        'scene_attempts_per_complete_round':14,'maximum_rounds':3,'maximum_scene_attempts':42,
        'build_timeout_s':180,'simulation_timeout_s':180,'postprocess_timeout_s':60,
        'execution_total_wall_s':3600,'product_online_calls':0,
        'new_local_execution_checks_limit':24,'new_local_timeout_s':10,
        'sysml_parse_limit':3,'sysml_parse_timeout_s':30,'identity_probe_limit':0,
        'note':'NEW finite proposal, pending confirmation; 8 local +1 SysML +14 vehicle scenes per full round, at most3 rounds; no prior balances inherited',
        'repair_boundary':'first failure pauses dependent execution; implementation-only repairs within remaining complete rounds; physical/task/numerical hard failure stops without tuning',
        'success_closes_unused_budget':True}
    reused=[]
    base=ROOT/'results/s3-c-user-vehicle-execution-20260924-01/round-03'
    for probe in previous_generator.PROBES:
        for variant in ('base','refined'):
            folder=base/(probe+'-'+variant)
            report=read(folder/'result.json')
            if report['status']!='PASS': raise ValueError('REUSED_PROBE_NOT_PASS')
            paths=[folder/'result.json',folder/'physical.json',folder/'V11CGenerated.mo',folder/'trace.json']
            if variant=='refined': paths.append(folder/'numerical.json')
            reused.append({'case':probe,'variant':variant,'status':'REUSE_EXISTING_PASS_NOT_RERUN',
                'reason':'probe model does not instantiate terrain contact or body envelopes; relevant shared source unchanged',
                'files':[{'path':str(p.relative_to(ROOT)),'sha256':sha(p)} for p in paths]})
    contract['reused_probe_evidence']=reused
    return bundle,contract


def inputs():
    packet=read(REVIEW/'execution-review-packet.json')
    raw=dict(packet); specification=raw.pop('specification_digest')
    if digest(raw)!=specification: raise ValueError('REVIEW_PACKET_DRIFT')
    for row in packet['fixed_inputs']:
        if sha(ROOT/row['path'])!=row['sha256']: raise ValueError('FIXED_INPUT_DRIFT: '+row['path'])
    bundle=read(REVIEW/'proposal.json'); contract=read(REVIEW/'verification-contract.json')
    expected_bundle,expected_contract=build_draft()
    if bundle!=expected_bundle or contract!=expected_contract:
        raise ValueError('UNAPPROVED_PHYSICAL_OR_CONTRACT_CHANGE')
    if digest(bundle)!=packet['bundle_digest'] or digest(contract)!=packet['verification_digest']:
        raise ValueError('INPUT_IDENTITY_DRIFT')
    return packet,bundle,contract


def isolated_b():
    if sha(CANDIDATE)!=C2_SHA: raise ValueError('ACCEPTED_CONTACT_DRIFT')
    return contact_bound_b()


def generate(bundle,case_id):
    packet,expected,contract=inputs()
    if bundle!=expected: raise ValueError('UNAPPROVED_AMENDMENT_BUNDLE')
    if case_id not in CASE_ORDER: raise ValueError('UNSUPPORTED_AMENDMENT_CASE')
    _,original,_=previous_inputs()
    result=previous_generator.generate(original,case_id)
    # Physical dynamics outside the accepted contact library are unchanged.
    # The previous source is reused verbatim except for its identity comments.
    for key in ('modelica','sysml'):
        result[key]=result[key].replace(PREVIOUS_SPEC,packet['specification_digest'])
    trace=result['trace']; trace['generator_version']=VERSION
    trace['frozen'].update(specification_digest=packet['specification_digest'],
        physical_digest=bundle['physical']['physical_digest'],verification_digest=digest(contract),
        contact_source_sha256=C2_SHA)
    trace['contact_binding']=deepcopy(bundle['physical']['contact_implementation'])
    trace['housing_amendment']=deepcopy(bundle['physical']['housing_amendment'])
    trace['previous_generator_version']=previous_generator.VERSION
    trace['modelica_source_sha256']=hashlib.sha256(result['modelica'].encode('utf-8')).hexdigest()
    trace['sysml_source_sha256']=hashlib.sha256(result['sysml'].encode('utf-8')).hexdigest()
    return result
