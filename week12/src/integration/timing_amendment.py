"""Exact approved timing amendment; history and cumulative budget stay intact."""
from .execution import OUT, approved_inputs, read, save, sha
from .review import review_identity, check_review_receipt

DRAFT = OUT/'timing-amendment-draft'
RECEIPT = OUT/'timing-amendment-authorization.json'
SPEC = 'aee33eb72f551dd60cd1652f48a93b054bc5f36444f74f04cd9db25bf833ded9'
MANIFEST_SHA = 'a48a79e68ff2d8bd711fded161a19e621aa2a783eeb44fe38fe339110c309b2f'


def inputs(require_receipt=True):
    approved_inputs()  # Original protected evidence must still be unchanged.
    if sha(DRAFT/'manifest.json') != MANIFEST_SHA:
        raise ValueError('TIMING_REVIEW_MANIFEST_DRIFT')
    for row in read(DRAFT/'manifest.json')['files']:
        if sha(DRAFT/row['name']) != row['sha256']:
            raise ValueError('TIMING_REVIEW_FILE_DRIFT: '+row['name'])
    bundle=read(DRAFT/'proposal.json'); contract=read(DRAFT/'verification-contract.json')
    identity=read(DRAFT/'review-identity.json')
    if identity['specification_digest'] != SPEC or review_identity([bundle],contract) != identity:
        raise ValueError('TIMING_SPECIFICATION_DRIFT')
    if require_receipt:
        check_review_receipt(read(RECEIPT),identity)
    return {'confirmation_identity':identity},bundle,contract


def bind_confirmation():
    packet,_,_=inputs(False)
    receipt={'origin':'user_message','human_confirmed':True,'user_text':'确认',
             'message_reference':'2026-09-24 current conversation reply immediately after the specific 4s settling amendment and last-round approval question',
             'scope':packet['confirmation_identity']['scope'],'specification_digest':SPEC,
             'execution_round':3,'budget_policy':'Original cumulative totals; final round at most18 scenes,8 local,1 SysML; no added repair round',
             'previous_authorization_sha256':sha(OUT/'authorization.json'),
             'previous_checkpoint_sha256':sha(OUT/'checkpoint-result.json')}
    check_review_receipt(receipt,packet['confirmation_identity'])
    save(RECEIPT,receipt)
    return receipt
