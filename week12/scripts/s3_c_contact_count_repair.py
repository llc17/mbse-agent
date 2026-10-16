"""Preserve the first-round observer failure and explicitly rebind its narrow fix."""
import csv
from week12.src.integration.contact_repair_execution import OUT,REVIEW,review_inputs,CANDIDATE
from week12.src.integration.execution import read,save,sha


def main():
    packet,protocol=review_inputs()
    directory=OUT/'round-01'; folder=directory/'T01-base'
    report=read(folder/'physical.json')
    rows=list(csv.DictReader((folder/'result.csv').open(encoding='utf-8',newline='')))
    bad=[r for r in rows if float(r['contactCount'])!=1]
    failed=[v['name'] for v in report['checks'] if not v['passed']]
    if failed!=['exactly_one_active_feature'] or not bad:
        raise ValueError('Different failure requires a new diagnosis')
    changed=[]; manifest=[]
    for row in packet['implementation_manifest']:
        new={'path':row['path'],'sha256':sha(row['path'])}; manifest.append(new)
        if new!=row: changed.append(row['path'])
    allowed={str(CANDIDATE),str(CANDIDATE.parents[3]/'tests/test_c_contact_repair.py')}
    if set(changed)!=allowed: raise ValueError('Unexpected implementation changes: '+repr(changed))
    save(directory/'diagnosis.json',{
        'classification':'implementation_observer_event_synchronization',
        'failed_checks':failed,'csv_sha256':sha(folder/'result.csv'),
        'bad_count_rows':len(bad),'first_bad':bad[0],'last_bad':bad[-1],
        'force_geometry_energy':'All unchanged independent checks passed; force remains one-contact magnitude',
        'generated_source':'model.c contactVertex[3] uses relationhysteresis slot35; fnVertex goes to exact zero on owner exit, positive-force observer can retain old relation value',
        'repair':'Conjoin each force-positive diagnostic count with its actual shared geometric owner; inactive feature always counts zero',
        'not_changed':['force/energy/geometry laws','positive-force count criterion','physical parameters','acceptance tolerances','CSV/event rows','original B'],
        'official_source':'https://specification.modelica.org/maint/3.6/equations.html#events-and-synchronization',
        'noEvent_rejected':'Direct continuous noEvent relation is unsuitable for Integer equations; preserve discrete owner and force-positive relation',
        'verification':'Next full affected round required; this diagnosis is not a rerun or PASS'})
    save(directory/'repair.json',{'classification':'implementation','proposal_digest':protocol['proposal_digest'],
        'reason':'Diagnostic positive-force relations retained an inactive corner count; explicitly gate count by shared owner without modifying force law or criteria',
        'changed_paths':changed,'rebound_implementation_manifest':manifest,
        'next_round':2,'maximum_rounds_total':3,'no_budget_reset':True})
    print('NARROW_OBSERVER_REPAIR_REBOUND',len(bad),failed)


if __name__=='__main__': main()
