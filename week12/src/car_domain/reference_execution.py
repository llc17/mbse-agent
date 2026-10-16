"""Local execution binding for the existing reference adapter; no new runner."""
from pathlib import Path
import hashlib
import json
import math

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / 'week12/results/s3-c-active-eightwheel-execution-20261009-01'
CONFIG = ROOT / 'week12/configs/reference_execution.json'
REFERENCE_APPROVAL_SHA = '49b6d88da1d3155aae6e9ee7bb64ef9ee84f9c73ac0903a477bbe6552b999c39'
DEFAULT_CAPS = dict(active_s=10800,analysis_s=8940,external_s=1860,scenes=2,
    scene_children=6,repairs=1,parents=8,parent_own_s=30,frontend=1,frontend_s=60,
    parse=2,parse_s=30,checks=3,check_s=20,product_requests=4,
    product_tokens=80384,product_usd=.1)
DEFAULT = dict(key='reference_core_chain',proposal_file='reference-core-chain-proposal-final.json',
    proposal_sha256=REFERENCE_APPROVAL_SHA,authorization_file='reference-core-chain-authorization.json',
    authorization_sha256='1df784103cc626a73ade56e68593b870064a3f369073a0c96d6d7c73a4388b84',
    snapshot_file='ledger-before-reference-core-chain.json',
    snapshot_sha256='3257dead0c2472583103d8d48bf48ca5761236d19604d37c684e4495627455bf',
    scope_id='reference-core-ad9eb814-f49d-4467-864c-7fd4df602ba5',
    caps=DEFAULT_CAPS,csv_deadline_s=7200,artifact_prefix='reference')


def _read_bound(name,digest):
    if not isinstance(name,str) or Path(name).name!=name or not name.endswith('.json'):
        raise ValueError('execution binding must name a JSON in the existing result directory')
    raw=(OUT/name).read_bytes()
    if hashlib.sha256(raw).hexdigest()!=digest:
        raise ValueError('execution approval/snapshot digest mismatch')
    return json.loads(raw)


def validate_caps(caps):
    product={'product_requests','product_tokens','product_usd'}
    if set(caps)!=set(DEFAULT_CAPS):
        raise ValueError('unsupported execution cap fields')
    for key,maximum in DEFAULT_CAPS.items():
        value=caps[key]
        if (type(value) not in (int,float) or not math.isfinite(value)
                or value<0 or (key not in product|{'repairs'} and value==0)
                or value>(2 if key=='repairs' else maximum)):
            raise ValueError('execution caps must be finite and inside supported runner limits')
    if any(caps[k]==0 for k in product) and not all(caps[k]==0 for k in product):
        raise ValueError('local-only execution requires all product caps zero')


def load_execution():
    if not CONFIG.exists():
        return dict(DEFAULT)
    value=json.loads(CONFIG.read_bytes())
    if set(value)!=set(DEFAULT):
        raise ValueError('unsupported local execution binding fields')
    if value['key']=='reference_core_chain' or not value['key'].isidentifier():
        raise ValueError('closed original execution cannot be selected as new scope')
    prefix=value['artifact_prefix']
    if not isinstance(prefix,str) or not prefix.replace('-','_').isidentifier() or prefix=='reference':
        raise ValueError('new execution needs a distinct artifact prefix')
    caps=value['caps']
    validate_caps(caps)
    if type(value['csv_deadline_s']) not in (int,float) or not 0<value['csv_deadline_s']<=caps['active_s']:
        raise ValueError('invalid finite CSV deadline')
    proposal=_read_bound(value['proposal_file'],value['proposal_sha256'])
    receipt=_read_bound(value['authorization_file'],value['authorization_sha256'])
    snapshot=_read_bound(value['snapshot_file'],value['snapshot_sha256'])
    if value['key'] in snapshot:
        raise ValueError('new execution namespace already exists in closed snapshot')
    expected=dict(status='APPROVED',proposal_sha256=value['proposal_sha256'],
        scope_id=value['scope_id'],old_ledger_sha256=value['snapshot_sha256'],
        old_ledger_snapshot_file=value['snapshot_file'],profile='REFERENCE_TEMPLATE_GENERATION_ONLY')
    if any(receipt.get(k)!=v for k,v in expected.items()):
        raise ValueError('new execution receipt differs from displayed scope')
    if (proposal.get('reference_approval_sha256')!=REFERENCE_APPROVAL_SHA
            or proposal.get('caps')!=caps or proposal.get('csv_deadline_s')!=value['csv_deadline_s']
            or proposal.get('ledger_key')!=value['key']
            or receipt.get('reference_source_sha256')!='a9bcd9d260342bde94d74b26b76af586682cd70c2a353d611e68bb5cd44b452f'):
        raise ValueError('new execution changed reference approval or budget')
    return value


EXECUTION=load_execution()


def execution_authorization():
    if load_execution()!=EXECUTION:
        raise ValueError('execution configuration changed after process startup')
    return _read_bound(EXECUTION['authorization_file'],EXECUTION['authorization_sha256'])
