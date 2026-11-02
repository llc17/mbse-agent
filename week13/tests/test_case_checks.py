import csv,tempfile,unittest,math
from pathlib import Path
from week13.src.case_checks import assess_route_rows as assess_rows,assess,REQUIRED,compare_numerics
# These fixtures intentionally isolate the inherited route primitive. Their
# ground-level z values are not valid stair-contact trajectories; integration
# tests independently prove they cannot become a physical-validity PASS.
from week13.src.case_paths import PACKAGE

VALUES=dict(R_main=.127,R_nose=.07,x_start_up=2.,x_start_down=9.6,x_end_down=15.2,stepD=.28,stepH=.15,n_steps=20)

def trajectory(end=18.,reverse=False):
    rows=[]
    for i in range(int(end/.02)+1):
        x=i*.02;row={k:0. for k in REQUIRED};row.update(time=i*.05,x=x,z=.127,v=.4,psi=-.1 if 9.<x<15.5 else 0.,x_n=x+.54,x_f=x+.22,x_r=x-.23,z_n=.07,z_f=.127,z_r=.127)
        for entity in ('n','f','r'):
            position=row['x_'+entity]
            if position<2:height=0.
            elif position<7.6:height=(math.floor((position-2)/.28)+(1 if entity=='n' else 0))*.15
            elif position<9.6:height=3.
            elif position<15.2:height=max(0.,3.-(math.floor((position-9.6)/.28)+(1 if entity=='n' else 0))*.15)
            else:height=0.
            row['h_'+entity]=height
        if reverse and 11.<x<11.1:row['v']=-.01
        rows.append(row)
    return rows

class MissionChecks(unittest.TestCase):
    def test_whole_route_passes_and_local_rebound_is_not_new_gate(self):
        report=assess_rows(trajectory(reverse=True),VALUES)
        self.assertEqual(report['task_status'],'PASS');self.assertEqual(len(report['events']),120)
        self.assertGreater(report['rear_edge_at_completion'],15.2);self.assertGreater(report['post_exit_distinct_times'],2)
    def test_terminal_value_cannot_replace_route(self):
        rows=trajectory();rows=rows[-20:]
        self.assertEqual(assess_rows(rows,VALUES)['task_status'],'FAIL')
    def test_wheel_center_crossing_is_not_wheel_edge_completion(self):
        rows=trajectory(15.45)
        self.assertEqual(assess_rows(rows,VALUES)['completion_status'],'OBSERVATION_INTERVAL_INCOMPLETE')
    def test_inversion_stops_but_soft_compression_reports(self):
        rows=trajectory();rows[40]['z_r']=-.1
        self.assertEqual(assess_rows(rows,VALUES)['task_status'],'PASS')
        rows[40]['x_r']=rows[40]['x_f']+.1
        self.assertEqual(assess_rows(rows,VALUES)['validity_status'],'FAIL')
    def test_nonfinite_and_stuck_time_rejected(self):
        rows=trajectory();rows[30]['v']=float('nan')
        self.assertEqual(assess_rows(rows,VALUES)['task_status'],'UNKNOWN')
        rows=trajectory()
        for row in rows:row['time']=0
        self.assertEqual(assess_rows(rows,VALUES)['task_status'],'UNKNOWN')
    def test_numerical_time_difference_is_report_only(self):
        a=assess_rows(trajectory(),VALUES);b=dict(a,completion_time=a['completion_time']+10)
        result=compare_numerics(a,b)
        self.assertEqual(result['status'],'PASS');self.assertEqual(result['completion_time_difference_s'],10)
        self.assertIsNone(result['time_difference_acceptance_threshold'])
    def test_truncated_horizon_cannot_be_complete_simulation(self):
        source='model M '+''.join('parameter Real '+k+'='+str(v)+';' for k,v in VALUES.items())+' end M;'
        spec=dict(artifact=dict(modelica_source=source),settings=dict(stop_time=120,intervals=2400))
        with tempfile.TemporaryDirectory(dir=PACKAGE) as tmp:
            path=Path(tmp)/'data.csv';rows=trajectory()
            with path.open('w',newline='',encoding='utf8') as handle:
                writer=csv.DictWriter(handle,fieldnames=['time',*REQUIRED]);writer.writeheader();writer.writerows(rows)
            report=assess(spec,path)
        self.assertEqual(report['task_status'],'UNKNOWN');self.assertIn('start/end',report['issues'][0])

if __name__=='__main__':unittest.main()
