"""Read-only localization of the round3 stair failure; no acceptance rerun."""
import csv
import hashlib
import math
from week12.src.integration.execution import OUT,read,save
from week12.src.integration import vehicle_verify as v


def main():
    root=OUT/'round-03'; folder=root/'fixed_nose_three_up-base'
    b=read(root/'proposal.json'); case=b['physical']['cases'][1]
    terrain=v._terrain(case['terrain_points'])
    envelope={'clearance_m':float('inf')}; mismatch={'error_m':-1}; first=None
    snapshots={}; last=None
    with (folder/'result.csv').open(encoding='utf-8-sig',newline='') as stream:
        for raw in csv.DictReader(stream):
            r={k:float(x) for k,x in raw.items()}; last=r
            t=r['time']
            for cid,shapes in b['physical']['envelopes_local_xz'].items():
                pose=tuple(r['body_'+cid+'_'+s] for s in ('x','z','pitch'))
                for index,shape in enumerate(shapes):
                    gap=v._shape_clearance(shape,pose,terrain)
                    if gap<envelope['clearance_m']:
                        envelope={'time_s':t,'component':cid,'shape_index':index,'pose_x_z_pitch':pose,'shape':shape,'clearance_m':gap}
                    if gap<-.0001 and first is None: first={'time_s':t,'component':cid,'clearance_m':gap}
            for w in b['design']['wheels']:
                cid=w['id']; x=r['body_'+cid+'_x']; z=r['body_'+cid+'_z']; radius=w['radius']['value']
                geometric,features=v._wheel_geometric_penetration(x,z,radius,terrain)
                reported=r['wheel_'+cid+'_penetration']; err=abs(geometric-reported)
                if err>mismatch['error_m']:
                    mismatch={'time_s':t,'wheel':cid,'x_m':x,'z_m':z,'R_m':radius,'geometric_m':geometric,
                              'reported_m':reported,'error_m':err,'feature':r['wheel_'+cid+'_featureId'],
                              'normal_N':r['wheel_'+cid+'_normal'],'nearest_features':sorted(features)}
            for when in (8,10,12,15,20,30,35):
                if abs(t-when)<1e-8:
                    snapshots[str(when)]={k:x for k,x in r.items() if k=='time' or
                        k in ['body_chassis_x','body_chassis_z','chassisPitch','body_chassis_vx'] or
                        any(k=='wheel_'+cid+'_'+s for cid in ('front','rear','nose') for s in ('normal','slip','torque','omega','shaftPower','featureId'))}
    # Endpoint projection near the geometric discrepancy, independent vectors.
    x,z=mismatch['x_m'],mismatch['z_m']; R=mismatch['R_m']
    nearby=[]
    for index,(a,c) in enumerate(terrain['segments'],1):
        dx,dz=c[0]-a[0],c[1]-a[1]; length=math.hypot(dx,dz)
        tx,tz=dx/length,dz/length; nx,nz=-tz,tx
        q=(x-a[0])*tx+(z-a[1])*tz; distance=(x-a[0])*nx+(z-a[1])*nz
        euclid=v._point_segment_distance(x,z,a,c)
        if euclid<R+.005:
            nearby.append({'face_id':100+index,'a':a,'b':c,'length_m':length,'projection_m':q,
                'signed_normal_distance_m':distance,'euclidean_distance_m':euclid,
                'within_half_open_projection':0<=q<length})
    mismatch['nearby_faces']=nearby
    report={'status':'DIAGNOSIS_NOT_NEW_ACCEPTANCE','csv_sha256':hashlib.sha256((folder/'result.csv').read_bytes()).hexdigest(),
            'first_nonwheel_overlap':first,'worst_nonwheel_overlap':envelope,
            'worst_wheel_geometric_discrepancy':mismatch,'snapshots':snapshots,
            'frozen_parameters_unchanged':True,'no_additional_simulation':True}
    save(root/'stair-diagnosis-details.json',report)
    print('worst envelope',envelope)
    print('worst geometry',mismatch)
    print('final',snapshots.get('35'))


if __name__=='__main__': main()
