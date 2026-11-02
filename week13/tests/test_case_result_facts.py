"""Result/report regressions using synthetic data only; no product or physics run."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock,patch

from week13.src.case_contact_review import current_summary,task_conclusion,with_review
from week13.src.case_console_result import prepare_result
from week13.src.case_semantic_guard import protect_explanation
from week13.src.case_taskflow import TaskFlow
from week13.tests.test_case_console_result_headline import saved_result
from week13.tests.test_case_contact_integration import write_fixture


OUT=Path(__file__).resolve().parents[1]/'results/result-verdict-fix-20261024-01/implementation'


class ResultFactsTests(unittest.TestCase):
    def fixture(self,**updates):return saved_result(**updates)['execution']

    def test_projection_is_idempotent_for_bound_unknown_unreviewed_imported_and_test_data(self):
        bound=self.fixture()
        missing=deepcopy(bound);missing.pop('current_contact_review')
        conflict=deepcopy(bound);conflict['current_contact_review']['binding']['project_id']='other'
        imported=dict(bound,record_kind='IMPORTED_REFERENCE')
        for row in (bound,missing,conflict,imported,dict(bound,test_only=True)):
            with self.subTest(row=row.get('record_kind',row.get('test_only'))):
                first=current_summary(row);self.assertEqual(current_summary(first),first)

    def test_historical_pass_is_explicitly_not_reviewed_not_failed(self):
        row=self.fixture();row.pop('current_contact_review')
        summary=current_summary(row)
        self.assertEqual((summary['recorded_task_status'],summary['task_status'],summary['current_review_state']),('PASS','UNKNOWN','NOT_REVIEWED'))
        self.assertEqual(summary['chain_status'],'NOT_REVIEWED')
        self.assertNotIn('INVALID_MODEL',task_conclusion(summary)[0])

    def test_compile_failure_never_means_vehicle_or_task_failure(self):
        row=self.fixture(tool_status='TOOL_FAIL',chain_status='BLOCKED_TOOL',task_status='UNKNOWN');row.pop('current_contact_review')
        headline=prepare_result(dict(execution=row))[3][0]
        self.assertIn('验证受阻',headline);self.assertNotIn('任务未完成',headline)

    def test_task_fail_and_current_invalid_model_remain_distinct(self):
        incomplete=self.fixture(task_status='FAIL',mission=dict(validity_status='PASS',task_status='FAIL',completion_status='OBSERVATION_INTERVAL_INCOMPLETE'))
        invalid=self.fixture(task_status='FAIL',mission=dict(validity_status='FAIL',task_status='FAIL',completion_status='INVALID_MODEL'))
        self.assertIn('观察区间内任务未完成',task_conclusion(current_summary(incomplete))[0])
        invalid=current_summary(invalid)
        self.assertEqual(invalid['task_status'],'FAIL');self.assertIn('\u6a21\u578b\u63a5\u89e6\u5f02\u5e38',task_conclusion(invalid)[0])

    def test_every_identity_and_csv_binding_is_required_and_checked(self):
        row=self.fixture()
        for field in ('project_id','version_id','task_id'):
            envelope=deepcopy(row);envelope.pop(field)
            with self.subTest(missing_envelope=field):self.assertEqual(current_summary(envelope)['task_status'],'UNKNOWN')
        for field in ('project_id','task_id','version_id','spec_digest','source_sha256','manifest_sha256','csv_sha256'):
            missing=deepcopy(row);missing['current_contact_review']['binding'].pop(field)
            with self.subTest(missing=field):self.assertEqual(current_summary(missing)['task_status'],'UNKNOWN')
            if field!='csv_sha256':
                changed=deepcopy(row);changed['current_contact_review']['binding'][field]='f'*64 if field.endswith(('_digest','_sha256')) else 'other'
                with self.subTest(conflict=field):self.assertEqual(current_summary(changed)['current_review_state'],'BINDING_CONFLICT')

    def test_csv_error_is_preserved_and_cannot_promote_recorded_pass(self):
        OUT.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='local-evidence-',dir=OUT) as folder:
            root=Path(folder);snapshot,base=write_fixture(root)
            (base/'tool/model_res.csv').unlink()
            with patch('week13.src.case_contact_review._base',return_value=base):summary=current_summary(with_review(snapshot,root))
            self.assertEqual(summary['task_status'],'UNKNOWN')
            self.assertTrue(any('model_res.csv' in issue for issue in summary['current_contact_review']['issues']))

    def test_cross_case_same_version_and_source_drift_are_not_display_pass(self):
        result=saved_result();current=deepcopy(result['execution'])
        for field,value in (('project_id','other_case'),('task_id','other_task'),('source_sha256','f'*64),('spec_digest','f'*64),('manifest_sha256','f'*64)):
            changed=deepcopy(current);changed[field]=value
            with self.subTest(field=field):
                view,_,_,lines=prepare_result(dict(result,state=dict(project_id=current['project_id'],latest_request_execution=changed)))
                self.assertIn('证据存在冲突',lines[0]);self.assertFalse(view['explanation']['explanation_validation']['body_displayed'])

    def test_nested_failed_tool_receipt_cannot_be_erased_by_current_pass(self):
        result=saved_result();result['state']=dict(latest_request_execution=deepcopy(result['execution']))
        result['execution']['outcome']=dict(tool_status='TOOL_FAIL')
        self.assertIn('证据存在冲突',prepare_result(result)[3][0])

    def test_stale_saved_validation_cannot_display_a_known_wrong_body(self):
        result=saved_result(task_status='FAIL',mission=dict(validity_status='PASS',task_status='FAIL',completion_status='OBSERVATION_INTERVAL_INCOMPLETE'))
        wrong='因此task_status=PASS，本次任务已经完成。'
        result['explanation']=dict(answer=wrong,explanation_validation=dict(status='NO_KNOWN_FACT_CONFLICT',body_displayed=True))
        before=deepcopy(result);view,_,_,lines=prepare_result(result)
        self.assertIn('观察区间内任务未完成',lines[0]);self.assertEqual(view['explanation']['raw_answer'],wrong)
        self.assertNotIn(wrong,view['explanation']['supplemental_analysis'])
        self.assertFalse(view['explanation']['explanation_validation']['body_displayed']);self.assertEqual(result,before)

    def test_chinese_adjacent_status_is_caught_without_word_boundary_blind_spot(self):
        row=self.fixture(task_status='FAIL',mission=dict(validity_status='PASS',task_status='FAIL',completion_status='OBSERVATION_INTERVAL_INCOMPLETE'))
        result=protect_explanation('所以task_status=PASS。',dict(project_id=row['project_id'],latest_request_execution=row))
        self.assertFalse(result['explanation_validation']['body_displayed'])
        self.assertIn('STRUCTURED_STATUS_CONTRADICTION',{x['code'] for x in result['explanation_validation']['issues']})

    def test_unknown_analysis_remains_visible_only_as_unverified_supplement(self):
        row=self.fixture();result=protect_explanation('这里是尚未校验的一般讨论。',dict(project_id=row['project_id'],latest_request_execution=row))
        self.assertTrue(result['explanation_validation']['body_displayed']);self.assertFalse(result['explanation_validation']['full_semantic_validation'])
        self.assertIn('【结果说明（模型生成）】',result['supplemental_analysis'])
        self.assertTrue(result['answer'].startswith('【任务结论】任务成功（当前简化模型内）'))

    def test_explicitly_reviewed_same_project_comparison_stays_available(self):
        result=saved_result();primary=result['execution'];other=deepcopy(primary)
        other.update(version_id='v000002',task_id='other')
        other['current_contact_review']['binding'].update(version_id='v000002',task_id='other')
        text='v000002仅作另一绑定结果的只读讨论。'
        result['explanation']['answer']=text
        result['current_fact_binding']=dict(project_id=primary['project_id'],latest_request_execution=current_summary(primary),explicitly_reviewed_executions=[current_summary(other)])
        view,_,_,lines=prepare_result(result)
        self.assertIn('任务成功（当前简化模型内）',lines[0]);self.assertTrue(view['explanation']['explanation_validation']['body_displayed'])
        self.assertIn(text,view['explanation']['supplemental_analysis'])
        result.pop('current_fact_binding')
        self.assertFalse(prepare_result(result)[0]['explanation']['explanation_validation']['body_displayed'])

    def test_success_logic_and_test_only_success_are_separate(self):
        row=self.fixture();self.assertTrue(task_conclusion(current_summary(row))[1])
        row['test_only']=True
        self.assertIn('TEST_ONLY',prepare_result(dict(execution=row))[3][0])
        self.assertFalse(task_conclusion(current_summary(row))[1])

    def test_state_reviews_only_current_and_keeps_history_recorded(self):
        OUT.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='local-state-',dir=OUT) as folder:
            old=self.fixture();old['version_id']='v000001';old['task_id']='old';old.pop('current_contact_review')
            latest=self.fixture(task_status='FAIL',mission=dict(validity_status='PASS',task_status='FAIL',completion_status='OBSERVATION_INTERVAL_INCOMPLETE'))
            rows=[dict(row,session_id='s') for row in (old,latest)]
            svc=SimpleNamespace(project_root=Path(folder),versions_index=lambda:dict(versions=dict(items=deepcopy(rows))),_artifact_version=lambda version:deepcopy(next(row for row in rows if row['version_id']==version)))
            flow=TaskFlow(dict(root=folder,request_id='r',project_id=latest['project_id'],session_id='s',baseline_ref={},baseline_version_id='v000001'))
            flow.open=Mock(return_value=svc);flow.http_state=lambda:{}
            with patch('week13.src.case_taskflow.with_review',side_effect=lambda row,root:row) as reviewed,patch('week13.src.case_taskflow.baseline_completion_status',return_value={}):state=flow.state(verify_current=True)
            self.assertEqual(reviewed.call_count,0)  # The selected fixture already carries its current review.
            self.assertEqual(state['versions'][0]['current_review_state'],'NOT_REVIEWED')
            self.assertEqual(state['versions'][0]['recorded_task_status'],'PASS');self.assertIsNone(state['last_task_pass'])
            self.assertEqual(state['latest_request_execution']['task_status'],'FAIL')


if __name__=='__main__':unittest.main()
