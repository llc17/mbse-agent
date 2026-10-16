"""Read-only diagnosis of the failed frozen settling gate; no simulation."""
import csv
import hashlib
import json
from pathlib import Path

from week12.src.integration.execution import OUT, save


def main():
    folder=OUT/'round-02'/'fixed_nose_flat_brake-base'
    proposal=json.loads((OUT/'proposal.json').read_text(encoding='utf-8'))
    d=proposal['design']; p=proposal['physical']
    components=d['bodies']+d['wheels']+d['drives']+[d['power_supply']]
    masses={c['id']:c['count']*c['mass']['value'] for c in components}
    total=sum(masses.values()); weight=total*p['gravity_m_s2']
    rows=[]
    with (folder/'result.csv').open(encoding='utf-8-sig',newline='') as f:
        for raw in csv.DictReader(f):
            t=float(raw['time'])
            if t>3: break
            row={k:float(v) for k,v in raw.items()}
            # For finite differences retain the post-event value only.
            if rows and rows[-1]['time']==t: rows[-1]=row
            else: rows.append(row)
    wheel_ids=[w['id'] for w in d['wheels']]
    def measure(r):
        fx=sum(r['wheel_'+w+'_Fx'] for w in wheel_ids)
        fz=sum(r['wheel_'+w+'_Fz'] for w in wheel_ids)
        law_errors={}
        c=p['contact_per_physical_wheel']
        for w in d['wheels']:
            ident=w['id']; z=r['body_'+ident+'_z']; vz=r['body_'+ident+'_vz']
            elastic=w['count']*c['stiffness_N_m']*max(0,w['radius']['value']-z)
            damping=max(-elastic,min(elastic,-w['count']*c['damping_Ns_m']*vz))
            expected=elastic+damping if 0<=z<w['radius']['value'] else 0
            law_errors[ident]=abs(r['wheel_'+ident+'_Fz']-expected)
        return {'time_s':r['time'],'Fx_N':fx,'Fz_N':fz,'weight_N':weight,
                'static_force_residual_N':(fx*fx+(fz-weight)**2)**.5,
                'normal_law_absolute_errors_N':law_errors,
                'main_vertical_speeds_m_s':{w:r['body_'+w+'_vz'] for w in wheel_ids},
                'pair_drive_torques_Nm':{w:r['wheel_'+w+'_torque'] for w in wheel_ids}}
    window=[r for r in rows if 2<=r['time']<=3]
    worst=max(window,key=lambda r:measure(r)['static_force_residual_N'])
    inertial_errors=[]
    for i,r in enumerate(rows[1:-1],1):
        if not 2<=r['time']<3: continue
        a,b=rows[i-1],rows[i+1]; dt=b['time']-a['time']
        inertia=sum(m*(b['body_'+ident+'_vz']-a['body_'+ident+'_vz'])/dt for ident,m in masses.items())
        external=measure(r)['Fz_N']-weight
        inertial_errors.append(abs(external-inertia))
    intervals={}
    for start,stop in ((2,2.25),(2.25,2.5),(2.5,2.75),(2.75,2.999)):
        subset=[measure(r) for r in window if start<=r['time']<=stop]
        intervals[f'{start}-{stop}s']=max(r['static_force_residual_N'] for r in subset)
    report={'classification':'physical_precondition_failure','csv_sha256':hashlib.sha256((folder/'result.csv').read_bytes()).hexdigest(),
            'frozen_window_s':[2,3],'frozen_force_limit_N':.02*weight,'worst_sample':measure(worst),
            'subwindow_peak_residual_N':intervals,
            'max_vertical_newton_balance_error_N':max(inertial_errors),
            'scope':'independent raw CSV + frozen mass/contact law; central differences for diagnosis, no new pass or threshold change',
            'conclusion':'The free release transient remains above the 2 percent static-load tolerance early in the approved 2-3s window. This is not missing weight or a compiler/connector failure. The frozen gate is FAIL; no refined or stair case is authorized past it.',
            'recommended_review':'Propose extending zero-drive settling to4s, measuring static balance in3-4s, ramping4-6s; preserve1s window,2 percent force tolerance, all mass/contact/drive ratings. New physical initialization/controller timing needs concrete user confirmation before execution; this is not verified by the current data.'}
    save(OUT/'round-02'/'settling-diagnosis.json',report)
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
