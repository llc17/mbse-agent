"""Localize the raw event-row contact discrepancy without changing the verdict."""
import csv
from week12.src.integration.execution import OUT,save


def main():
    folder=OUT/'round-03/fixed_nose_three_up-base'
    rows=[]; bad=[]
    # At the first ascent convex corner (2,.15), the front wheel is just
    # above the top and its centre remains in the corner/horizontal domain.
    with (folder/'result.csv').open(encoding='utf-8-sig',newline='') as stream:
        for raw in csv.DictReader(stream):
            t=float(raw['time'])
            if 10.334<=t<=10.339:
                r={key:float(raw[key]) for key in ('time','body_front_x','body_front_z',
                    'wheel_front_penetration','wheel_front_normal','wheel_front_featureId')}
                x,z=r['body_front_x'],r['body_front_z']
                distance=((x-2)**2+(z-.15)**2)**.5 if x<2 else z-.15
                r['independent_circle_overlap_m']=max(0,.127-distance)
                r['difference_m']=abs(r['independent_circle_overlap_m']-r['wheel_front_penetration'])
                rows.append(r)
                if r['difference_m']>.0005: bad.append(r)
    report={'status':'LOCAL_EVENT_DIAGNOSIS_NO_ACCEPTANCE_CHANGE','window_s':[10.334,10.339],
        'rows':rows,'bad_rows':len(bad),
        'interpretation':'A zero contact observation at the convex-corner to upper-face transition disagrees with overlap geometry. Preserve the original failed check. Adjacent samples can localize it but cannot establish refined convergence or justify dropping event rows.',
        'no_model_or_threshold_change':True}
    save(OUT/'round-03/event-row-diagnosis.json',report)
    print(report)


if __name__=='__main__': main()
