"""Bind the full C review specification. No function here creates user approval."""
from week12.src.composition.schema import digest
from week12.src.component_physics.contract import ROOT
import hashlib


def protocol_hash():
    return hashlib.sha256((ROOT/'任务规划/C-两结构集成与验证协议.md').read_bytes()).hexdigest()


def review_identity(bundles, contract):
    entries=[]
    for b in sorted(bundles,key=lambda v:v['design']['design_id']):
        d,p=b['design'],b['physical']
        entries.append({'design_id':d['design_id'],'project_id':d['project_id'],
                        'baseline_version_id':d['baseline_version_id'],'design_digest':d['design_digest'],
                        'physical_digest':p['physical_digest'],'b_source_sha256':p['b_source_sha256'],
                        'binding_version':p['binding_version']})
    identity={'scope':'v11_s3_c_concrete_physical_and_verification_plan',
              'entries':entries,'verification_digest':digest(contract),'protocol_sha256':protocol_hash()}
    identity['specification_digest']=digest(identity)
    return identity


def check_review_receipt(receipt, expected_identity):
    """Boundary check only; a future trusted UI must supply the actual user reply.

    JSON labels cannot authenticate a user. Do not use an A-only confirmation or
    a synthetic development receipt to authorize C models.
    """
    if receipt.get('origin')!='user_message' or receipt.get('human_confirmed') is not True:
        raise ValueError('EXTERNAL_USER_CONFIRMATION_REQUIRED')
    if not receipt.get('message_reference') or not receipt.get('user_text'):
        raise ValueError('USER_MESSAGE_EVIDENCE_REQUIRED')
    if receipt.get('scope')!=expected_identity['scope'] or receipt.get('specification_digest')!=expected_identity['specification_digest']:
        raise ValueError('STALE_FULL_C_CONFIRMATION')
    return {'status':'USER_MESSAGE_REFERENCE_BOUND_NOT_AUTHENTICATED_BY_JSON',
            'specification_digest':expected_identity['specification_digest']}
