"""Bounded read-only mechanics review; no model execution or design approval."""
import csv
import json
import math
import time
from pathlib import Path
from week12.src.integration.execution import OUT,ROOT,read,save,sha
from week12.src.integration.timing_amendment import inputs

TARGET=ROOT/'results/s3-c-evidence-review-20260926-01'


def contacts(row,w,points,contact):
    ident=w['id']; R=w['radius']['value']; count=w['count']
    x,z,vx,vz=[float(row['body_'+ident+'_'+s]) for s in ('x','z','vx','vz')]
    omega=float(row['wheel_'+ident+'_omega'])
    stiffness=count*contact['stiffness_N_m']; damping=count*contact['damping_Ns_m']
    segments=[]; features=[]
    for a,b in zip(points,points[1:]):
        length=math.dist(a,b); tx=(b[0]-a[0])/length; tz=(b[1]-a[1])/length
        segments.append((a,b,length,tx,tz,-tz,tx))
    def feature(fid,distance,nx,nz):
        depth=R-distance
        vn=vx*nx+vz*nz; vt=vx*nz-vz*nx-R*omega
        elastic=stiffness*depth
        fn=elastic+max(-elastic,min(elastic,-damping*vn))
        ft=-contact['friction']*fn*math.tanh(vt/contact['vreg_m_s'])
        features.append({'id':fid,'depth_m':depth,'normal_N':fn,'tangent_N':ft,
                         'normal_xz':[nx,nz],'slip_m_s':vt,
                         'Fx_N':fn*nx+ft*nz,'Fz_N':fn*nz-ft*nx})
    for index,(a,b,length,tx,tz,nx,nz) in enumerate(segments,1):
        q=(x-a[0])*tx+(z-a[1])*tz; distance=(x-a[0])*nx+(z-a[1])*nz
        if 0<=q<length and 0<=distance<R: feature(100+index,distance,nx,nz)
    for i in range(1,len(points)-1):
        p=points[i]; before=segments[i-1]; after=segments[i]
        turn=before[3]*after[4]-before[4]*after[3]
        if turn>=0: continue  # clockwise convex corner for ground below path
        dx,dz=x-p[0],z-p[1]; d=math.hypot(dx,dz)
        if 0<d<R:
            nx,nz=dx/d,dz/d
            if nx*before[5]+nz*before[6]>=0 and nx*after[5]+nz*after[6]>=0:
                feature(200+i+1,d,nx,nz)
    return features


def main():
    start=time.monotonic()
    TARGET.mkdir(exist_ok=False)
    save(TARGET/'read-only-policy.json',{'scope':'one existing CSV and frozen source evidence, no simulator/compiler/parser/tests',
        'maximum_analysis_passes':1,'wall_limit_s':60,'new_Modelica':0,'new_SysML':0,'new_acceptance_tests':0,
        'online_model_calls':0,'old_budget':'closed, not reset or charged as a new simulation'})
    _,bundle,_=inputs()
    source=OUT/'round-03/fixed_nose_three_up-base/result.csv'
    final=None; collision=None
    with source.open(encoding='utf-8-sig',newline='') as stream:
        for row in csv.DictReader(stream):
            final=row
            if abs(float(row['time'])-8.711)<1e-9: collision=row
            if time.monotonic()-start>60: raise TimeoutError('READ_ONLY_REVIEW_LIMIT')
    assert final is not None and collision is not None
    wheels=bundle['design']['wheels']; points=bundle['physical']['cases'][1]['terrain_points']
    masses=bundle['design']['bodies']+wheels+bundle['design']['drives']+[bundle['design']['power_supply']]
    centre={s:sum(item['count']*item['mass']['value']*float(final['body_'+item['id']+'_'+s]) for item in masses)/85 for s in ('x','z')}
    rows=[]; fx=fz=moment=0
    for wheel in wheels:
        ident=wheel['id']; feats=contacts(final,wheel,points,bundle['physical']['contact_per_physical_wheel'])
        force_x=sum(f['Fx_N'] for f in feats); force_z=sum(f['Fz_N'] for f in feats)
        fn=sum(f['normal_N'] for f in feats); tangent=sum(f['tangent_N'] for f in feats)
        rr=float(final['wheel_'+ident+'_rollingTorque']); torque=float(final['wheel_'+ident+'_torque'])
        x,z=[float(final['body_'+ident+'_'+s]) for s in ('x','z')]
        fx+=force_x; fz+=force_z
        moment+=(x-centre['x'])*force_z-(z-centre['z'])*force_x+wheel['radius']['value']*tangent-rr
        rows.append({'wheel':ident,'features':feats,'Fx_N':force_x,'Fz_N':force_z,
            'force_reconstruction_error_N':max(abs(force_x-float(final['wheel_'+ident+'_Fx'])),abs(force_z-float(final['wheel_'+ident+'_Fz']))),
            'sum_normals_N':fn,'drive_pair_torque_Nm':torque,
            'steady_spin_torque_residual_Nm':torque+rr-wheel['radius']['value']*tangent})
    envelope=bundle['physical']['envelopes_local_xz']['nose_drive'][0]
    theta=float(collision['body_nose_drive_pitch']); c,s=math.cos(theta),math.sin(theta)
    x,z=[float(collision['body_nose_drive_'+axis]) for axis in ('x','z')]
    vertices=[[x+c*a-s*b,z+s*a+c*b] for a,b in envelope['vertices']]
    overlap_vertices=[p for p in vertices if 2<=p[0]<=2.28 and p[1]<.15]
    nose=next(w for w in wheels if w['id']=='nose')
    d=next(v for v in bundle['design']['drives'] if v['id']=='nose_drive')
    report={'status':'READ_ONLY_REVIEW_NOT_NEW_ACCEPTANCE','time_s':float(final['time']),
        'source_csv_sha256':sha(source),'unchanged_specification_digest':'aee33eb72f551dd60cd1652f48a93b054bc5f36444f74f04cd9db25bf833ded9',
        'terminal_COM_m':centre,'contact_decomposition':rows,
        'static_force_residual_xz_N':[fx,fz-85*9.81],'static_pitch_moment_residual_Nm':moment,
        'envelope':{'at_s':8.711,'component':'nose_drive','world_vertices_m':vertices,'inside_step_vertices':overlap_vertices,
            'old_square_circumradius_m':math.hypot(.05,.05),'wheel_radius_m':nose['radius']['value'],
            'interpretation':'Envelope is checked after simulation; it does not apply a collision reaction. Thus its overlap is an independent invalidity, not the demonstrated cause of the later stall.'},
        'packaging_candidate_not_approved':{'shape':'axle-centred circular x-z envelope','radius_m':.05,
            'same_mass_kg_per_motor':d['mass']['value'],'same_inertia_kg_m2_per_motor':d['inertia']['value'],
            'necessary_inertia_bound_kg_m2':d['mass']['value']*.05**2,
            'conditional_minimum_clearance_m':nose['radius']['value']-min(.02,.15*nose['radius']['value'])-.05,
            'bound_assumptions':['true distance to terrain satisfies existing wheel penetration gate','axle centres remain coincident','physical motor fits the circular envelope'],
            'not_proven':['real motor procurement or fit','stair traversal','event continuity','3D width/cabling']},
        'interpretation':['Rear torque saturation alone does not establish a new required motor rating.',
            'Rear has simultaneous ground and vertical-face contacts; front touches next convex corner, nose is unloaded.',
            'The static force and wheel torque reconstruction explains the sustained stall under current equations; not hardware certification.',
            'A packaging-only change cannot alter this simulated trajectory because body envelopes currently do not exert contact forces.'],
        'actual_wall_s':time.monotonic()-start,'new_simulations':0,'new_acceptance_tests':0,'product_online_calls':0}
    save(TARGET/'mechanics-review.json',report)
    save(TARGET/'source-evidence.json',{'files':[{'path':str(p.relative_to(ROOT)),'sha256':sha(p)} for p in
        (source,OUT/'round-03/proposal.json',ROOT/'models/components/V11PhysicalB1/package.mo',
         ROOT/'src/integration/model_generator.py',ROOT/'src/integration/vehicle_verify.py',Path(__file__))]})
    print(json.dumps(report,ensure_ascii=False,indent=2))


if __name__=='__main__': main()
