"""Explicit frozen eightwheel profile; no new design or old execution authority."""
from __future__ import annotations
from copy import deepcopy
import hashlib
import json
from typing import Mapping
from .contracts import (REPO_ROOT, WEEK12_ROOT, RESULT_ROOT, PHYSICAL_PATH, PHYSICAL_SHA,
                        SPEC_SCHEMA, CORE, _legacy_validate_spec)
from .reference_profile import digest_json, _numeric_source, _check_explicit_conflicts

PROFILE = 'active_eightwheel_v1'
CONTRACT_SHA = '11a6698bd0127fb4bf2be8ec961eb97531717ea7e08be1ddc47f496a993a7b04'
AUTH_SHA = "83c0b645d4f46e16624fc88316e3638cb54bd07037073d558137b67440dca9db"
GENERATED_BODY_SHA = 'eb80da656b5598f9708136c7469e8cfe0e75aea6aa8a4528af2e81c02d802066'
SCOPE_ID = "eightwheel-signed-work-d1ec4d91-d9c8-41ff-b533-286665a49591"
RUN_ROOT = RESULT_ROOT / 'eightwheel-signed-work-execution-20261010'
ACTIVATION_SHA = "de1bc77f1c0fa9e656ec0144038776a67a5f5965e3c87d1a9cfef5401c42a420"
OLD_LEDGER_SHA = "ee78e866410b2b46d3bb6bcbffe3d31b1b822105dc4eff180356946bdd0d7ab8"
PROPOSAL_PATH = RUN_ROOT / 'next-execution-proposal.json'


def _bound_json(path, expected):
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('eightwheel approved source/receipt drift: ' + str(path))
    return json.loads(raw)


def base_authorization():
    CONTRACT_SHA = '11a6698bd0127fb4bf2be8ec961eb97531717ea7e08be1ddc47f496a993a7b04'
    AUTH_SHA = '83c0b645d4f46e16624fc88316e3638cb54bd07037073d558137b67440dca9db'
    GENERATED_BODY_SHA = 'eb80da656b5598f9708136c7469e8cfe0e75aea6aa8a4528af2e81c02d802066'
    PROPOSAL_PATH = RUN_ROOT / 'next-execution-proposal.json'
    receipt = _bound_json(RUN_ROOT / 'authorization-amendment-01.json', AUTH_SHA)
    proposal = _bound_json(PROPOSAL_PATH, CONTRACT_SHA)
    activation = _bound_json(RUN_ROOT / 'activation-amendment-01.json', ACTIVATION_SHA)
    base_receipt=_bound_json(RUN_ROOT/'authorization.json', '7e48a4e31f50527d0530697044c31ce59134a54942c1916d2b287c3ec9b97f98')
    _bound_json(RUN_ROOT/'activation.json', 'dbd2be24ebccbef7a4eb670be63fc8b0925546e98f983c80cf8b06cabc968450')
    _bound_json(RUN_ROOT/'ledger-before-amendment-01.json', 'c75e69a206b49cdaeca2a576ea99f949888683ecdddef5deefa11e4b4d3ff0f5')
    if (receipt.get('base_authorization_sha256') != '7e48a4e31f50527d0530697044c31ce59134a54942c1916d2b287c3ec9b97f98'
            or proposal.get('base_proposal_sha256') != base_receipt['proposal_sha256']
            or proposal.get('started_utc_preserved') != base_receipt['started_utc']
            or receipt.get('started_utc') != base_receipt['started_utc']
            or receipt.get('same_clock_and_cumulative_counts') is not True):
        raise ValueError('same-scope amendment lineage mismatch')
    _bound_json(REPO_ROOT / PHYSICAL_PATH, PHYSICAL_SHA)
    _bound_json(RUN_ROOT / 'ledger-before.json', OLD_LEDGER_SHA)
    if (receipt.get('status') != 'APPROVED_IMPLEMENTATION_STARTED'
            or receipt.get('proposal_sha256') != CONTRACT_SHA
            or receipt.get('no_physical_change') is not True
            or receipt.get('budget') != proposal['budget']
            or receipt.get('approved_numerical_addendum') != proposal['numerical_acceptance_addendum']
            or receipt.get('acceptance_criteria_unchanged') is not True
            or activation.get('authorization_sha256') != AUTH_SHA
            or activation.get('proposal_sha256') != CONTRACT_SHA
            or activation.get('scope_id') != SCOPE_ID
            or activation.get('key') != 'eightwheel_signed_work_execution'
            or activation.get('snapshot_sha256') != OLD_LEDGER_SHA
            or activation.get('started_utc') != receipt.get('started_utc')
            or proposal['physical']['sha256'] != PHYSICAL_SHA
            or proposal['physical']['body_sha256'] != GENERATED_BODY_SHA):
        raise ValueError('eightwheel combined authorization mismatch')
    return {**receipt, **activation, 'physical_spec_sha256': PHYSICAL_SHA,
            'generated_body_sha256': GENERATED_BODY_SHA}


BASE_CONTRACT_SHA = CONTRACT_SHA
BASE_AUTH_SHA = AUTH_SHA
BASE_BODY_SHA = GENERATED_BODY_SHA
CONTRACT_SHA = '59f0177e27e4e1175c241a377d22bdebf04508d2a81f50e565518b6e4e730f5d'
AUTH_SHA = 'b2e8ec55728f1cde98f5b4690fa8f5cc76641188a79e93881cb58d9ae7b0aebb'
GENERATED_BODY_SHA = '8b552d569926f0a2bdf05f473544dd58b095f50eb9f2b0225b0bc9219cf6311b'
PROPOSAL_PATH = RUN_ROOT / 'module-continuation-proposal.json'
MODULE_BEFORE_SHA = '317c9912906309570a4cc6900502c6fc3208c513b6f416f825515f6f15ba4efa'
PROBE_REUSE_SHA = 'bc618c872a6d30c2b1b62b391cc571e9acb8de1da1bd7ec98bdd9a7fe95e1eac'


def module_authorization():
    CONTRACT_SHA = '59f0177e27e4e1175c241a377d22bdebf04508d2a81f50e565518b6e4e730f5d'
    AUTH_SHA = 'b2e8ec55728f1cde98f5b4690fa8f5cc76641188a79e93881cb58d9ae7b0aebb'
    PROPOSAL_PATH = RUN_ROOT/'module-continuation-proposal.json'
    base = base_authorization()
    receipt = _bound_json(RUN_ROOT/'module-authorization.json', AUTH_SHA)
    proposal = _bound_json(PROPOSAL_PATH, CONTRACT_SHA)
    activation = json.loads((RUN_ROOT/'module-activation.json').read_bytes())
    _bound_json(RUN_ROOT/'ledger-before-module-amendment.json', MODULE_BEFORE_SHA)
    if (receipt.get('previous_authorization_sha256') != BASE_AUTH_SHA
            or receipt.get('proposal_sha256') != CONTRACT_SHA
            or receipt.get('scope_id') != SCOPE_ID
            or receipt.get('started_utc') != base['started_utc']
            or receipt.get('clock_preserved') is not True
            or receipt.get('no_total_time_increase') is not True
            or proposal.get('base_amendment_sha256') != BASE_CONTRACT_SHA
            or proposal.get('started_utc_preserved') != base['started_utc']
            or proposal['physical']['sha256'] != PHYSICAL_SHA
            or proposal['physical']['body_sha256'] != BASE_BODY_SHA
            or proposal['numerical_acceptance_addendum'] != base['approved_numerical_addendum']
            or activation.get('proposal_sha256') != CONTRACT_SHA
            or activation.get('authorization_sha256') != AUTH_SHA
            or activation.get('before_sha256') != MODULE_BEFORE_SHA
            or activation.get('caps') != proposal['amendment_caps']):
        raise ValueError('module continuation amendment lineage mismatch')
    return {**base, **receipt, 'authorization_sha256': AUTH_SHA, 'proposal_sha256': CONTRACT_SHA,
            'caps': proposal['amendment_caps'],
            'physical_spec_sha256': PHYSICAL_SHA, 'generated_body_sha256': GENERATED_BODY_SHA}


CONTRACT_SHA = 'b9dc2f172e875e79729437da8a96cc73e4cd1184707180533f1dfd9d8a7f3f67'
AUTH_SHA = 'cadd1c1f4ee8090837b664536141683abf336293f0be33500107aaccc0949cf3'
PROPOSAL_PATH = RUN_ROOT/'module-recovery-proposal.json'
RECOVERY_ACTIVATION_SHA = '07440bc29259a8a7696ddc487e2ffd7876cb2abf3990ccfa8637c4b0de74ec20'
RECOVERY_BEFORE_SHA = '95e0c6c3f70f1f61c9eb9e895083d78ed955162cead58a7bfc906cdb7d881015'

def recovery_authorization():
    CONTRACT_SHA = 'b9dc2f172e875e79729437da8a96cc73e4cd1184707180533f1dfd9d8a7f3f67'
    PROPOSAL_PATH = RUN_ROOT/'module-recovery-proposal.json'
    AUTH_SHA = 'cadd1c1f4ee8090837b664536141683abf336293f0be33500107aaccc0949cf3'
    base = module_authorization()
    receipt = _bound_json(RUN_ROOT/'module-recovery-authorization.json', AUTH_SHA)
    proposal = _bound_json(PROPOSAL_PATH, CONTRACT_SHA)
    activation = _bound_json(RUN_ROOT/'module-recovery-activation.json', RECOVERY_ACTIVATION_SHA)
    before = _bound_json(RUN_ROOT/'module-recovery-before.json', RECOVERY_BEFORE_SHA)[proposal['scope_key']]
    budget = proposal['budget']
    caps = dict(before['caps'])
    caps.update(proposal['cumulative_caps_after_activation'])
    caps.update(active_s=receipt['activation_elapsed_s']+budget['additional_future_total_s'],
        analysis_s=receipt['activation_elapsed_s']-receipt['activation_external_s']+budget['additional_future_analysis_s'],
        external_s=receipt['activation_external_s']+budget['additional_future_external_s'])
    if (receipt.get('status') != 'APPROVED_IMPLEMENTATION_STARTED'
        or receipt.get('proposal_sha256') != CONTRACT_SHA
        or receipt.get('previous_authorization_sha256') != base['authorization_sha256']
        or receipt.get('previous_proposal_sha256') != base['proposal_sha256']
        or receipt.get('before_sha256') != RECOVERY_BEFORE_SHA
        or receipt.get('scope_id') != SCOPE_ID or receipt.get('started_utc') != base['started_utc']
        or receipt.get('budget') != budget or receipt.get('caps') != caps
        or receipt.get('activation_external_s') != before['external_s']
        or receipt.get('no_physical_change') is not True
        or receipt.get('acceptance_criteria_unchanged') is not True
        or receipt.get('approved_numerical_addendum') != base['approved_numerical_addendum']
        or proposal.get('numerical_acceptance_addendum') != base['approved_numerical_addendum']
        or proposal.get('generated_candidate_sha256') != GENERATED_BODY_SHA
        or proposal['physical_spec']['sha256'] != PHYSICAL_SHA
        or activation.get('authorization_sha256') != AUTH_SHA
        or activation.get('proposal_sha256') != CONTRACT_SHA
        or activation.get('before_sha256') != RECOVERY_BEFORE_SHA
        or activation.get('caps') != caps
        or activation.get('first_whole_vehicle_deadline_elapsed_s') != receipt['activation_elapsed_s']+1800
        or receipt.get('first_whole_vehicle_deadline_elapsed_s') != activation['first_whole_vehicle_deadline_elapsed_s']):
        raise ValueError('module recovery authorization/budget lineage mismatch')
    return {**base, **receipt, 'authorization_sha256':AUTH_SHA, 'proposal_sha256':CONTRACT_SHA,
        'physical_spec_sha256':PHYSICAL_SHA, 'generated_body_sha256':GENERATED_BODY_SHA}


AUTH_SHA = 'b9c57232f7c8bfc9950ab2b00b2e307b9ced17c53d847174731c3fc68b512f8e'
SUPPLEMENT_BEFORE_SHA = 'e241bd0839679eb0e9bfab9af08cfdea994afb49e67d1ccc5474854a8a5a4a20'

def supplement_authorization():
    AUTH_SHA = 'b9c57232f7c8bfc9950ab2b00b2e307b9ced17c53d847174731c3fc68b512f8e'
    CONTRACT_SHA = 'b9dc2f172e875e79729437da8a96cc73e4cd1184707180533f1dfd9d8a7f3f67'
    base=recovery_authorization()
    receipt=_bound_json(RUN_ROOT/'module-recovery-supplement-authorization.json',AUTH_SHA)
    before=_bound_json(RUN_ROOT/'module-recovery-supplement-before.json',SUPPLEMENT_BEFORE_SHA)
    caps=dict(base['caps']);caps['checks']+=1;caps['repairs']+=1
    if (receipt.get('previous_authorization_sha256')!=base['authorization_sha256']
        or receipt.get('proposal_sha256')!=CONTRACT_SHA
        or receipt.get('scope_id')!=SCOPE_ID or receipt.get('started_utc')!=base['started_utc']
        or receipt.get('before_sha256')!=SUPPLEMENT_BEFORE_SHA
        or receipt.get('caps')!=caps or receipt.get('extra_total_seconds')!=0
        or receipt.get('physical_and_acceptance_unchanged') is not True
        or before['eightwheel_signed_work_execution']['authorization_sha256']!=base['authorization_sha256']):
        raise ValueError('single assertion supplement authorization mismatch')
    return {**base,'caps':caps,'authorization_sha256':AUTH_SHA}


CONTRACT_SHA = '04738243ae95e9b3ee62eaee1bdad66f756361310f24def9258f44b11f41a3a3'
AUTH_SHA = 'b14db7e0f5d3111e173ca695ab88837c60fdfff2e9e5e2b74444fa55bfbc4435'
PROPOSAL_PATH = RUN_ROOT/'deadline-recovery-proposal.json'
DEADLINE_ACTIVATION_SHA = '5c3880d81b1a2fae7228fcf54e7f38a021c225fce3e0577a1f6aefaa2495cb1f'
DEADLINE_BEFORE_SHA = 'c3bc19c227a5bcf417a3771172ede971f54e8cd4a838df2aa8e01fa5a8779674'

def authorization():
    base=supplement_authorization()
    proposal=_bound_json(PROPOSAL_PATH,CONTRACT_SHA)
    receipt=_bound_json(RUN_ROOT/'deadline-recovery-authorization.json',AUTH_SHA)
    activation=_bound_json(RUN_ROOT/'deadline-recovery-activation.json',DEADLINE_ACTIVATION_SHA)
    before=_bound_json(RUN_ROOT/'deadline-recovery-before.json',DEADLINE_BEFORE_SHA)[proposal['scope_key']]
    budget=proposal['budget']
    caps=dict(proposal['cumulative_count_caps'])
    caps.update(active_s=receipt['activation_elapsed_s']+budget['future_total_s'],
        analysis_s=receipt['activation_elapsed_s']-receipt['activation_external_s']+budget['future_analysis_s'],
        external_s=receipt['activation_external_s']+budget['future_external_s'])
    deadline=receipt['activation_elapsed_s']+budget['first_whole_vehicle_csv_deadline_from_activation_s']
    if (receipt.get('status')!='APPROVED_IMPLEMENTATION_STARTED'
        or receipt.get('previous_authorization_sha256')!=base['authorization_sha256']
        or receipt.get('previous_proposal_sha256')!=base['proposal_sha256']
        or receipt.get('proposal_sha256')!=CONTRACT_SHA
        or receipt.get('scope_id')!=SCOPE_ID or receipt.get('started_utc')!=base['started_utc']
        or receipt.get('before_sha256')!=DEADLINE_BEFORE_SHA
        or receipt.get('activation_external_s')!=before['external_s']
        or receipt.get('caps')!=caps or receipt.get('budget')!=budget
        or receipt.get('no_physical_change') is not True or receipt.get('acceptance_criteria_unchanged') is not True
        or receipt.get('approved_numerical_addendum')!=base['approved_numerical_addendum']
        or proposal['numerical_acceptance_addendum']!=base['approved_numerical_addendum']
        or proposal['physical_spec']['sha256']!=PHYSICAL_SHA
        or proposal['generated_candidate_sha256']!=GENERATED_BODY_SHA
        or activation.get('authorization_sha256')!=AUTH_SHA or activation.get('proposal_sha256')!=CONTRACT_SHA
        or activation.get('before_sha256')!=DEADLINE_BEFORE_SHA or activation.get('caps')!=caps
        or activation.get('scope_id')!=SCOPE_ID or activation.get('started_utc')!=base['started_utc']
        or receipt.get('first_whole_vehicle_deadline_elapsed_s')!=deadline
        or activation.get('first_whole_vehicle_deadline_elapsed_s')!=deadline):
        raise ValueError('deadline recovery authorization lineage or caps mismatch')
    return {**base,**receipt,'authorization_sha256':AUTH_SHA,'proposal_sha256':CONTRACT_SHA,
        'physical_spec_sha256':PHYSICAL_SHA,'generated_body_sha256':GENERATED_BODY_SHA}


def physical_spec():
    authorization()
    return _bound_json(REPO_ROOT / PHYSICAL_PATH, PHYSICAL_SHA)


def validate_eightwheel_spec(spec):
    try:
        authorization()
        if not isinstance(spec, Mapping) or spec.get('profile') != PROFILE:
            raise ValueError('explicit approved eightwheel profile required')
        legacy = deepcopy(dict(spec)); legacy.pop('profile')
        checked = _legacy_validate_spec(legacy)
        if not checked.get('ready'):
            return checked
        canonical = checked['spec']
        if any(canonical[field] for field in ('constraints','preferences','ideas','assumptions')):
            raise ValueError('frozen eightwheel first case accepts no additional design inputs')
        sources = {s['id']: s['text'] for s in canonical['sources']}
        if any(not key.strip() for key in sources):
            raise ValueError('empty source id')
        _check_explicit_conflicts(sources)
        for row in canonical['requirements']:
            if row['name'] != 'passage_task':
                _numeric_source(row['source_text'], row['value'], row['unit'])
            if row['name'] == 'continuous_steps' and type(row['value']) is not int:
                raise ValueError('continuous_steps must be an integer')
        canonical['profile'] = PROFILE
        canonical['sources'] = sorted(canonical['sources'], key=lambda s:s['id'])
        return {**checked, 'spec': canonical, 'spec_digest': digest_json(canonical),
                'profile': PROFILE, 'execution_scope_id': SCOPE_ID,
                'support': 'EXISTING_APPROVED_EIGHTWHEEL_ONLY', 'new_design_synthesis': False}
    except (KeyError, TypeError, ValueError, OSError) as exc:
        return {'ready': False, 'issues': [{'message': str(exc)}]}


def approved_eightwheel_context():
    physical = physical_spec()
    return {'profile': PROFILE, 'schema_version': SPEC_SCHEMA,
            'design_mode': 'REUSE_APPROVED_DESIGN',
            'physical_ref': {'path': PHYSICAL_PATH, 'sha256': PHYSICAL_SHA},
            'candidate_id': physical['candidate_id'],
            'supported_requirements': {k: {'value':v,'unit':u} for k,(v,u) in CORE.items()},
            'design_permission': {'wheel_count':'delegated','architecture':'delegated','tracks_allowed':True},
            'execution_scope_id': SCOPE_ID, 'execution_contract_sha256': CONTRACT_SHA,
            'authorization_sha256': AUTH_SHA, 'generated_body_sha256': GENERATED_BODY_SHA,
            'new_design_synthesis': False, 'vehicle_task_status':'UNKNOWN',
            'limitations': ['Only the approved frozen eightwheel case is supported.',
                            'Any failed or incomplete check remains vehicle UNKNOWN.',
                            'No failure audit, automatic simulation retry or product API is authorized.',
                            'Hardware supplier and failsafe remain UNKNOWN; V11 is not completed.']}


SOURCE_TEXT = ('EXISTING_APPROVED_SPEC_IMPORT: imported facts and design delegation from the existing user-approved '
               'frozen vehicle specification '+PHYSICAL_PATH+' sha256='+PHYSICAL_SHA+'; '
               '设备载荷20kg；台阶高度0.15m；台阶深度0.28m；连续级数20级；'
               'ascend_and_forward_head_down_front_first_descend；'
               '轮数与构型委派Agent决定，允许履带及任何方式。')


def make_approved_spec(source_id, text=SOURCE_TEXT):
    """Import archived approved facts, never fabricate a new human utterance."""
    facts = {'payload_mass':'设备载荷20kg','step_height':'台阶高度0.15m',
             'step_depth':'台阶深度0.28m','continuous_steps':'连续级数20级',
             'passage_task':'ascend_and_forward_head_down_front_first_descend'}
    permission = '轮数与构型委派Agent决定，允许履带及任何方式。'
    spec = {'schema_version':SPEC_SCHEMA,'profile':PROFILE,'design_mode':'REUSE_APPROVED_DESIGN',
            'physical_ref':{'path':PHYSICAL_PATH,'sha256':PHYSICAL_SHA},
            'sources':[{'id':source_id,'text':text}],
            'requirements':[{'name':name,'value':value,'unit':unit,'source_id':source_id,
                             'source_text':facts[name],'origin':'user','classification':'hard'}
                            for name,(value,unit) in CORE.items()],
            'design_permission':{'wheel_count':'delegated','architecture':'delegated','tracks_allowed':True,
                                 'source_id':source_id,'source_text':permission},
            'constraints':[],'preferences':[],'ideas':[],'assumptions':[]}
    checked = validate_eightwheel_spec(spec)
    if not checked.get('ready'):
        raise ValueError(checked['issues'])
    return checked['spec']
