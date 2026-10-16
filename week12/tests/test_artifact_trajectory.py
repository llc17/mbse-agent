"""Synthetic trajectory fixtures are observations, never successful real cars."""
from copy import deepcopy
from pathlib import Path
import csv,json,tempfile,unittest
from week12.src.car_domain import model_artifact as artifact
from week12.src.car_domain.artifact_trajectory import evaluate_trajectory,supplemental_assessment
from week12.src.car_domain.artifact_service import ArtifactService
from week12.tests import test_model_artifact as fixtures

ROOT=Path(__file__).resolve().parents[1]
RESULTS=ROOT/'results/s5-trajectory-outcome-20261012-01'

def fixture():
    spec=fixtures.fixture();spec['checks']=[];spec['settings']=dict(stop_time=4,intervals=8)
    spec['outputs']=dict(front_x='fx',front_z='fz',rear_x='rx',rear_z='rz')
    contract=dict(schema='v11-artifact-trajectory-1',route_semantics='ordered_forward_waypoints',entities=dict(front=dict(x='front_x',z='front_z'),rear=dict(x='rear_x',z='rear_z')),milestones=[],start_time=0,end_time=4,max_sample_gap=.51,max_speed=3,position_tolerance=.001,time_tolerance=1e-8,coverage_note='TEST_ONLY two independently tracked points, not a real vehicle',model_basis='der(signal)=level;',requirement_refs=[dict(source_id='src_fixture',source_text=fixtures.SOURCE_TEXT)])
    for index,(x,z) in enumerate(((0.,1.),(1.,2.),(2.,1.))):
        contract['milestones'].append(dict(id='step'+str(index),phase='up' if index<2 else 'down',targets={name:dict(x_min=x,x_max=x+.25,z_min=z-.1,z_max=z+.1) for name in ('front','rear')}))
    spec['trajectory_contract']=contract;return spec

class TrajectoryTests(unittest.TestCase):
    def setUp(self):
        RESULTS.mkdir(exist_ok=True);self.tmp=tempfile.TemporaryDirectory(dir=RESULTS);self.path=Path(self.tmp.name)
        self.spec=fixture();self.csv=self.path/'trace.csv'
    def tearDown(self):self.tmp.cleanup()
    def write(self,rows=None,fields=('time','fx','fz','rx','rz')):
        rows=rows or [[i*.5,-1+i*.5,1 if i<4 else 2 if i<6 else 1,-1.5+i*.5,1 if i<5 else 2 if i<7 else 1] for i in range(9)]
        with self.csv.open('w',newline='') as h:
            writer=csv.writer(h);writer.writerow(fields);writer.writerows(rows)
    def assess(self):return evaluate_trajectory(self.spec,self.csv)
    def test_ordered_synthetic_observations_pass_vehicle_unknown(self):
        self.write();result=self.assess();self.assertEqual(result['task_status'],'PASS');self.assertEqual(result['vehicle_task_status'],'UNKNOWN')
        summary=artifact.summarize_execution(self.spec,dict(compile=dict(status='PASS'),simulation=dict(status='PASS')),self.csv)
        self.assertEqual(summary['task_status'],'PASS');self.assertEqual(summary['physical_acceptance_status'],'NOT_VERIFIED')
    def test_stationary_first_stage_fail_later_not_reached(self):
        self.write([[i*.5,-.2,.1,-.7,.1] for i in range(9)])
        result=self.assess();self.assertEqual(result['task_status'],'FAIL');self.assertEqual([s['status'] for s in result['milestones']],['FAIL','NOT_REACHED','NOT_REACHED'])
    def test_only_front_cannot_pass(self):
        self.write()
        with self.csv.open() as handle:rows=list(csv.reader(handle))
        self.write([[*r[:3],-2,1] for r in rows[1:]])
        self.assertEqual(self.assess()['task_status'],'FAIL')
    def test_wrong_height_cannot_pass(self):
        self.write([[i*.5,-1+i*.5,0,-1.5+i*.5,0] for i in range(9)])
        self.assertEqual(self.assess()['task_status'],'FAIL')
    def test_reverse_cannot_pass(self):
        self.write([[i*.5,2-i*.5,1,1.5-i*.5,1] for i in range(9)])
        self.assertEqual(self.assess()['task_status'],'UNKNOWN')
    def test_missing_field_and_nan_and_truncated_unknown(self):
        self.write([[0,0],[4,1]],('time','fx'));self.assertEqual(self.assess()['task_status'],'UNKNOWN')
        self.write();text=self.csv.read_text();self.csv.write_text(text.replace('0.0,-1.0','0.0,nan'));self.assertEqual(self.assess()['task_status'],'UNKNOWN')
        self.write();lines=self.csv.read_text().splitlines();self.csv.write_text('\n'.join(lines[:-1]));self.assertEqual(self.assess()['task_status'],'UNKNOWN')
    def test_discontinuity_unknown(self):
        self.write([[0,-1,1,-1.5,1],[.01,0,1,-.5,1],[4,2,1,2,1]])
        self.assertEqual(self.assess()['task_status'],'UNKNOWN')
    def test_reverse_entry_and_overlapping_boxes_cannot_pass(self):
        self.spec['trajectory_contract']['max_speed']=5
        self.write([[0,-1,1,-1.5,1],[.5,.5,1,.5,1],[1,0,1,0,1],[1.5,.5,1,.5,1],[2,1,2,1,2],[2.5,1.5,2,1.5,2],[3,2,1,2,1],[3.5,2.5,1,2.5,1],[4,3,1,3,1]])
        result=self.assess();self.assertEqual(result['milestones'][0]['status'],'FAIL')
        self.spec['trajectory_contract']['milestones'][0]['targets']['front']['x_max']=1.1
        self.assertFalse(artifact.validate_artifact(self.spec)['ready'])
    def test_unknown_route_no_completion_or_wrong_coverage_refused(self):
        for mutate in (lambda c:c.update(route_semantics='guess_from_name'),lambda c:c.update(milestones=[]),lambda c:c['milestones'][0]['targets'].pop('rear')):
            spec=fixture();mutate(spec['trajectory_contract']);self.assertFalse(artifact.validate_artifact(spec)['ready'])
    def test_normal_proposal_display_and_digest_bind_contract(self):
        ctx=fixtures.context(self.path/'context.json',allow=False);service=ArtifactService(self.path/'db.sqlite','test',context=ctx,worker_manager=object())
        service.project_create('TEST_ONLY_trajectory');source=service.record_source(fixtures.SOURCE_TEXT)
        self.spec['sources'][0]['id']=source['source_id'];self.spec['requirements'][0]['source_id']=source['source_id'];self.spec['trajectory_contract']['requirement_refs'][0]['source_id']=source['source_id']
        # Current proposals require dual source; this remains a synthetic fixture.
        requirement=dict(self.spec['requirements'][0],model_name=self.spec['artifact']['model_name'])
        self.spec['artifact']['sysml_source']='package TrajectoryTest { part def System { attribute level : Real = 0.6; } part system : System; requirement req_1 { '+''.join('attribute '+k+' : String = '+json.dumps(v,ensure_ascii=False)+'; ' for k,v in requirement.items())+'} }'
        proposal=service.proposal_prepare(self.spec)['proposal'];self.assertEqual(proposal['display']['trajectory_contract'],self.spec['trajectory_contract'])
        old=proposal['spec_digest'];self.spec['trajectory_contract']['max_speed']=4;self.assertNotEqual(old,artifact.validate_artifact(self.spec)['spec_digest'])
    def test_supplemental_binding_drift_rejected(self):
        root=ROOT/'projects/prj_10ec168259ce4969aabeca4ccbe6f585/versions/v000001'
        if not root.exists():self.skipTest('existing CSV not present')
        # The public service resolves and verifies the full real manifest; this
        # explicit negative cannot proceed with a fabricated binding.
        record=dict(evidence_status='COMPLETE',recorded_conclusions_verified=True,project_id=root.parent.parent.name,version_id='v000001',task_id='task_d895313379c0420eaac032f45296b4e9',spec_digest='x',source_sha256='x',manifest_sha256='x')
        with self.assertRaisesRegex(ValueError,'binding drift'):supplemental_assessment(record,dict(files={'tool/model_res.csv':{}}),dict(schema='v11-supplemental-trajectory-1',binding={},contract={},interpretation_note='TEST_ONLY'))
    def test_actual_csv_current_assessment_fail_and_tool_fail_unknown(self):
        path=RESULTS/'supplemental-contract.json'
        if not path.exists():self.skipTest('supplemental case capture not present')
        document=json.loads(path.read_bytes());binding=document['binding'];base=ROOT/'projects'/binding['project_id']/'versions'/binding['version_id']
        spec=json.loads((base/'spec.json').read_bytes());manifest=json.loads((base/'artifact-manifest.json').read_bytes())
        record=dict(binding,evidence_status='COMPLETE',recorded_conclusions_verified=True,frozen_spec=spec,task_status='UNKNOWN',tool_status='PASS',chain_status='PASS')
        result=supplemental_assessment(record,manifest,document)
        self.assertEqual(result['assessment']['task_status'],'FAIL');self.assertEqual(result['recorded_task_status'],'UNKNOWN')
        self.assertEqual(result['assessment']['milestones'][0]['status'],'FAIL');self.assertTrue(all(s['status']=='NOT_REACHED' for s in result['assessment']['milestones'][1:]))
        record['tool_status']='TOOL_FAIL';result=supplemental_assessment(record,manifest,document)
        self.assertEqual(result['assessment']['task_status'],'UNKNOWN')

if __name__=='__main__':unittest.main()
