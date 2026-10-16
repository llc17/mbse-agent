"""Collect already tested C proposals into a review packet; never approve them."""
import hashlib
import json
from pathlib import Path
from week12.src.component_physics.contract import ROOT, canonical, require_dependency_hashes
from week12.src.integration.review import review_identity


def main():
    root=ROOT/'results/s3-c-proposals-20260924-01'
    rounds=sorted(root.glob('round-[0-9][0-9]'))
    last=rounds[-1]
    result=json.loads((last/'result.json').read_text(encoding='utf-8'))
    if result['status']!='PASS_STATIC_PROPOSALS_PENDING_CONFIRMATION':
        raise ValueError('No accepted static preparation result')
    require_dependency_hashes(json.loads((last/'source-manifest.json').read_text(encoding='utf-8')))
    files=[last/'source-manifest.json',last/'result.json',last/'verification-contract.json',
           ROOT/'任务规划/C-两结构集成与验证协议.md',root/'policy.json']
    summaries=[]; bundles=[]
    for topology in ('fixed_nose','rocker_bogie'):
        path=last/(topology+'-proposal.json')
        bundle=json.loads(path.read_text(encoding='utf-8'))
        bundles.append(bundle)
        files.extend([path,last/(topology+'-static.json')])
        summaries.append({'topology':topology,'design_digest':bundle['design']['design_digest'],
                          'physical_digest':bundle['physical']['physical_digest'],
                          'pending':bundle['physical']['status'],'mass':bundle['binding']['mass_check']})
    originals=[ROOT/'models/user-4w2n/source-v1/运动/6lun_zhenggeliucheng.mo',
               ROOT/'models/user-4w2n/source-v1/能量/power_limit.mo']
    manifest=[{'path':str(p.relative_to(ROOT.parent)),'sha256':hashlib.sha256(p.read_bytes()).hexdigest()} for p in [*files,*originals]]
    identity=review_identity(bundles,json.loads((last/'verification-contract.json').read_text(encoding='utf-8')))
    packet={'status':'READY_FOR_CONCRETE_REVIEW_NOT_APPROVED','proposals':summaries,'confirmation_identity':identity,
            'manifest':manifest,'modelica_calls':0,'product_online_calls':0,
            'prior_C_local_attempts':9,'current_local_attempts':len(list(root.glob('local-reservation-*.json'))),
            'approval_required_for':'both complete physical descriptors, verification-contract and finite execution budget',
            'user_facts_receipt':'2026-09-24 total85kg and full-flow stairs150x280mm; not whole physical plan approval',
            'implementation_gate':'executable generation, shared bus, rolling resistance, geometry/energy verifier and frozen source gate must be implemented/reviewed before first tool run',
            'repair_policy':'implementation-only repair may rebind exact source under same physical descriptor; physics or thresholds changes invalidate plan confirmation'}
    packet['review_packet_digest']=hashlib.sha256(canonical(packet)).hexdigest()
    with (root/'review-packet.json').open('xb') as stream:
        stream.write(canonical(packet))
    print(json.dumps({'status':packet['status'],'digest':packet['review_packet_digest'],'local_attempts':packet['current_local_attempts']}))


if __name__=='__main__':
    main()
