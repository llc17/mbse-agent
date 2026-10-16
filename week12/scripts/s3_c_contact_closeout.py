"""Close the contact-only package from existing evidence; never run verification."""
from week12.src.integration.contact_repair_execution import OUT,review_inputs,CASE_ORDER,CANDIDATE,B_SOURCE
from week12.src.integration.execution import read,save,sha,Batch,require_sources


def main():
    packet,_=review_inputs()
    reports=sorted(OUT.glob('round-*/batch-result.json'))
    if not reports: raise ValueError('NO_COMPLETED_ROUND')
    rounds=[read(p) for p in reports]; last=rounds[-1]; directory=reports[-1].parent
    manifest=read(directory/'source-manifest.json'); require_sources(manifest)
    usage=Batch(OUT,packet['specification_digest']).usage()
    if usage['unresolved_processes'] or (OUT/'.execution.lock').exists():
        raise ValueError('UNRESOLVED_EXECUTION')
    accepted=last['status']=='PASS_CONTACT_VARIANT_NOT_VEHICLE_ACCEPTANCE'
    if accepted:
        expected=[i+s for i in CASE_ORDER for s in ('-base','-refined')]
        if [r['case'] for r in last['scenes']]!=expected: raise ValueError('INCOMPLETE_ACCEPTED_ROUND')
        if any(r['status']!='PASS' for r in last['scenes']): raise ValueError('FAILED_ACCEPTED_SCENE')
    scenes=[s for r in rounds for s in r['scenes']]
    files=[]
    for report in reports:
        files.append(report)
        for s in read(report)['scenes']:
            folder=report.parent/s['case']
            files.extend(p for p in (folder/'result.json',folder/'physical.json',folder/'numerical.json',
                folder/'result.csv',folder/'event-cache-audit.json',folder/'owner-audit.json') if p.exists())
    files.extend([OUT/'authorization.json',OUT/'policy.json',directory/'source-manifest.json',CANDIDATE,B_SOURCE])
    evidence=[{'path':str(p),'sha256':sha(p)} for p in files]
    save(OUT/'closeout-manifest.json',{'files':evidence,'scope':'original failed evidence plus isolated contact variant'})
    result={
        'status':'PASS_ISOLATED_CONTACT_VARIANT_C_INCOMPLETE' if accepted else 'STOPPED_CONTACT_VALIDATION_C_INCOMPLETE',
        'specification_digest':packet['specification_digest'],
        'rounds_used':len(rounds),'last_round':last['round'],'usage':usage,
        'local_passes':sum(v['pass'] for r in rounds for v in r['local']),
        'local_failures':sum(not v['pass'] for r in rounds for v in r['local']),
        'scene_passes':sum(v['status']=='PASS' for v in scenes),
        'scene_failures':[{'round':r['round'],'case':s['case'],'error':s.get('error')} for r in rounds for s in r['scenes'] if s['status']!='PASS'],
        'compile_passes':sum(v['compile']=='PASS' for v in scenes),
        'simulation_completed':sum(v['simulation']=='COMPLETED' for v in scenes),
        'accepted_round':last['round'] if accepted else None,
        'accepted_scene_count':len(last['scenes']) if accepted else 0,
        'accepted_numerical_pairs':sum(v['numeric']=='PASS' for v in last['scenes']) if accepted else 0,
        'candidate_sha256':sha(CANDIDATE),'original_B_sha256':sha(B_SOURCE),
        'C_cumulative_local_attempts':54+usage['local'],'product_online_calls':0,'whole_vehicle_runs_this_package':0,
        'unused_budget':'retired on success; never transferred to vehicle execution' if accepted else 'not increased; stop reason retained',
        'limitations':['only frozen C2 probe and isolated B02-B06 parameters','original B remains unchanged',
                      'C2 not registered or bound into vehicle','whole-car ascent failure and packaging gap remain unresolved',
                      'C/S3/V11 incomplete; no D/E or V12'],
        'last_round_error':last.get('error')}
    save(OUT/'final-result.json',result)
    print(result)


if __name__=='__main__': main()
