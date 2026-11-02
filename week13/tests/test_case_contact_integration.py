"""Local contact-gate integration; all files are isolated test fixtures."""
from copy import deepcopy
import csv
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from week13.src.case_checks import REQUIRED,assess_rows
from week13.src.case_contact_review import review_snapshot,current_summary,with_review
from week13.src.case_console_result import prepare_result
from week13.src.case_paths import sha_value
from week13.tests.test_case_contact_validity import known_source

OUT=Path(__file__).resolve().parents[1]/'results/contact-validity-fix-20261022-01'
VALUES=dict(stepH=.3,stepD=.5,n_steps=1,x_start_up=2.,x_end_up=2.5,x_start_down=4.5,x_end_down=5.,H_peak=.3,R_nose=.07,R_main=.127,len_flat=2.)


def rows():
    result=[]
    for index in range(3):
        row={key:0. for key in REQUIRED}
        row.update(time=index*.05,x_n=2.1,z_n=.2,x_f=1.5,z_f=.4,x_r=1.,z_r=.4,h_n=.3)
        result.append(row)
    return result


def write_fixture(root):
    declarations='model M\n'+''.join('parameter Real '+k+'='+str(v)+';\n' for k,v in VALUES.items())+'equation '
    source=known_source().replace('model M equation ',declarations)
    spec=dict(artifact=dict(modelica_source=source,model_name='M'),settings=dict(stop_time=.1,intervals=2),outputs=dict(x='x'),checks=[])
    old_mission=dict(task_status='PASS',validity_status='PASS',completion_status='COMPLETE_FORWARD_UP20_DOWN20_EXIT_AND_CONTINUE',completion_time=.1)
    outcome=dict(tool_status='PASS',chain_status='PASS',task_status='PASS',mission_assessment=old_mission,tool_result=dict(compile=dict(status='PASS'),simulation=dict(status='PASS'),runtime_parameter_readback=dict(status='PASS')))
    snapshot=dict(project_id='prj_TEST',task_id='task_TEST',version_id='v000001',record_kind='ARTIFACT_EXECUTION_SNAPSHOT',frozen_spec=spec,spec_digest=sha_value(spec),source_sha256=hashlib.sha256(source.encode()).hexdigest(),evidence_status='COMPLETE',recorded_conclusions_verified=True,state='succeeded',outcome=outcome,tool_status='PASS',chain_status='PASS',task_status='PASS')
    base=root/'projects/prj_TEST/versions/v000001';(base/'tool').mkdir(parents=True)
    (base/'frozen-model.mo').write_bytes(source.encode('utf8'))
    for name,value in (('spec.json',spec),('outcome.json',outcome)):(base/name).write_text(json.dumps(value),encoding='utf8')
    with (base/'tool/model_res.csv').open('w',encoding='utf8',newline='') as file:
        writer=csv.DictWriter(file,fieldnames=['time',*REQUIRED]);writer.writeheader();writer.writerows(rows())
    files={name:dict(sha256=hashlib.sha256((base/name).read_bytes()).hexdigest(),bytes=(base/name).stat().st_size) for name in ('frozen-model.mo','spec.json','outcome.json','tool/model_res.csv')}
    manifest=dict(schema='v11-artifact-manifest-1',binding={k:snapshot[k] for k in ('project_id','task_id','spec_digest','source_sha256')},files=files,outcome=outcome)
    path=base/'artifact-manifest.json';path.write_text(json.dumps(manifest),encoding='utf8')
    snapshot.update(manifest_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),source_files=dict(Modelica=str(base/'frozen-model.mo')),data_files=dict(CSV=str(base/'tool/model_res.csv')))
    return snapshot,base


class ContactIntegrationTests(unittest.TestCase):
    def setUp(self):
        OUT.mkdir(parents=True,exist_ok=True)
        self.tmp=tempfile.TemporaryDirectory(prefix='integration-tests-',dir=OUT)
        self.root=Path(self.tmp.name);self.snapshot,self.base=write_fixture(self.root)
    def tearDown(self):self.tmp.cleanup()

    def test_route_pass_cannot_override_center_inside_solid(self):
        route=dict(task_status='PASS',validity_status='PASS',completion_status='COMPLETE_FORWARD_UP20_DOWN20_EXIT_AND_CONTINUE',issues=[])
        with patch('week13.src.case_checks.assess_route_rows',return_value=route):report=assess_rows(rows(),VALUES)
        self.assertEqual(report['route_assessment']['task_status'],'PASS')
        self.assertEqual(report['contact_validity']['status'],'FAIL')
        self.assertEqual((report['task_status'],report['validity_status'],report['completion_status']),('UNKNOWN','FAIL','CONTACT_ANOMALY_ROUTE_COMPLETE'))

    def test_manifest_bound_review_does_not_write_and_overrides_saved_pass(self):
        protected={p:hashlib.sha256(p.read_bytes()).hexdigest() for p in self.base.rglob('*') if p.is_file()}
        old=deepcopy(self.snapshot);review=review_snapshot(self.snapshot,self.root)
        self.assertEqual(review['status'],'FAIL');self.assertEqual(review['task_status'],'UNKNOWN')
        result=dict(request_id='TEST_request',execution=self.snapshot,state=dict(latest_request_execution=current_summary(with_review(self.snapshot,self.root))))
        _,execution,_,lines=prepare_result(result,request_id='TEST_request')
        self.assertIn('模型接触异常',lines[0]);self.assertNotIn('任务已完成',lines[0])
        self.assertEqual(execution['tool_status'],'PASS');self.assertEqual(execution['task_status'],'UNKNOWN')
        self.assertEqual(self.snapshot,old)
        self.assertEqual(protected,{p:hashlib.sha256(p.read_bytes()).hexdigest() for p in protected})
        self.assertIn('csv_sha256',review['binding']);self.assertIn('checker_source_sha256',review['binding'])
        self.assertNotIn('events',review['current_assessment']);self.assertNotIn('detailed_assessment',review)
        from week13.src.case_console import Console
        output=[];ui=Console(console_root=self.root/'ui',output=output.append);ui.ctx=dict(request_id='TEST_request',root=str(self.root));ui.show_result(result,full=True)
        evidence=[line for line in output if line.startswith('几何证据：')]
        self.assertGreater(len(evidence),0);self.assertLessEqual(len(evidence),3)
        self.assertIn('鼻轮整圆',evidence[0]);self.assertIn('d-R=',evidence[0]);self.assertIn('mm',evidence[0])
        self.assertFalse(any('strict_center_inside_solid' in line for line in output))

    def test_missing_csv_unknown_and_saved_historical_pass_is_never_headline(self):
        (self.base/'tool/model_res.csv').unlink()
        reviewed=with_review(self.snapshot,self.root)
        self.assertEqual(reviewed['current_contact_review']['status'],'UNKNOWN')
        result=dict(execution=self.snapshot,state=dict(latest_request_execution=current_summary(reviewed)))
        self.assertNotIn('任务已完成',prepare_result(result)[3][0])

    def test_source_or_manifest_drift_is_unknown_not_vehicle_fail(self):
        for field in ('source_sha256','manifest_sha256','spec_digest'):
            changed=dict(self.snapshot);changed[field]='0'*64
            result=review_snapshot(changed,self.root)
            self.assertEqual((result['status'],result['task_status']),('UNKNOWN','UNKNOWN'))

    def test_snapshot_outcome_cannot_forge_a_legacy_failure_attribution(self):
        changed=deepcopy(self.snapshot);changed['outcome']['chain_status']='BLOCKED_INVALID_MODEL'
        review=review_snapshot(changed,self.root)
        self.assertEqual((review['status'],review['task_status']),('UNKNOWN','UNKNOWN'))
        self.assertIn('snapshot outcome differs',review['issues'][0])

    def test_outside_or_cross_version_path_not_followed(self):
        for root in (OUT.parent,self.root/'../outside'):
            self.assertEqual(review_snapshot(self.snapshot,root)['status'],'UNKNOWN')
        changed=deepcopy(self.snapshot);changed['data_files']['CSV']=str(self.root/'arbitrary.csv')
        self.assertEqual(review_snapshot(changed,self.root)['status'],'UNKNOWN')

    def test_test_only_remains_test_only(self):
        changed=deepcopy(self.snapshot);changed['outcome']['test_only']=True
        self.assertEqual(review_snapshot(changed,self.root)['status'],'TEST_ONLY')
        self.assertIn('TEST_ONLY',prepare_result(dict(execution=changed))[3][0])

    def test_signal_check_cannot_overwrite_invalid_mission(self):
        from week13.src.inherited.model_artifact import summarize_execution
        spec=dict(checks=[dict(output='x',kind='final_ge',threshold=0)],outputs=dict(x='x'))
        tool=dict(compile=dict(status='PASS'),simulation=dict(status='PASS'),runtime_parameter_readback=dict(status='PASS'))
        mission=dict(task_status='UNKNOWN',validity_status='FAIL',completion_status='INVALID_MODEL')
        with patch('week13.src.inherited.artifact_delivery.read_signals',return_value=dict(mapping=dict(x=dict(resolved='x')),series=dict(x=[10]))),patch('week13.src.case_checks.assess',return_value=mission):
            report=summarize_execution(spec,tool,'TEST_ONLY_UNUSED.csv')
        self.assertEqual(report['observations'][0]['status'],'PASS')
        self.assertEqual(report['task_status'],'UNKNOWN');self.assertEqual(report['chain_status'],'BLOCKED_INVALID_MODEL')

    def test_product_false_pass_text_cannot_override_system_prefix(self):
        from week13.src.case_console import Console
        summary=current_summary(with_review(self.snapshot,self.root))
        answer='是，300mm通过。这是故意注入的错误产品回答。'
        class Runner:
            def call(self,*args,**kwargs):return dict(answer=answer,binding=dict(latest_request_execution=summary))
        output=[];ui=Console(Runner(),console_root=self.root/'ui',output=output.append)
        ui.ctx=dict(request_id='TEST_request');ui.budget_guard=lambda:True;ui.refresh=lambda:None
        ui.selection['request_id']='TEST_request'
        ui.ask('真的爬过去了吗？')
        first=next(index for index,line in enumerate(output) if line.startswith('【任务结论】'))
        self.assertIn('模型接触异常',output[first])
        self.assertNotIn(answer,output,'未经核对的冲突原文默认不展示；原证据不改写')
        self.assertIn('原文保留在本请求证据中','\n'.join(output))

    def test_new_worker_invalid_state_is_geometry_unknown_but_tool_failure_is_tool_block(self):
        reviewed=with_review(self.snapshot,self.root)
        execution=dict(self.snapshot,state='failed',task_status='UNKNOWN',chain_status='BLOCKED_INVALID_STATE')
        execution['outcome']=dict(execution['outcome'],task_status='UNKNOWN',chain_status='BLOCKED_INVALID_STATE')
        result=dict(execution=execution,state=dict(latest_request_execution=current_summary(reviewed)))
        self.assertIn('证据存在冲突',prepare_result(result)[3][0])
        execution['tool_status']='TOOL_FAIL';execution['outcome']['tool_status']='TOOL_FAIL'
        result['state']['latest_request_execution']['tool_status']='TOOL_FAIL'
        self.assertIn('证据存在冲突',prepare_result(result)[3][0])
        self.assertNotIn('INVALID_MODEL',prepare_result(result)[3][0])


if __name__=='__main__':unittest.main()
