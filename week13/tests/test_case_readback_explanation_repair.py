"""Finite repair checks; saved JSON only, no HTTP, MCP, OMC or CSV reads."""
import asyncio
from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock,patch

from week13.src import case_taskflow as tf,case_console as ui,case_console_data as data
from week13.src.case_progress import emit_progress
from week13.src.case_semantic_guard import protect_explanation,baseline_source_references

ROOT=Path(__file__).resolve().parents[1]
PKG=ROOT/'results/agent-readback-lifecycle-repair-20261101-01'
CASE=ROOT/'runs/case-projects/case_380e7315172241efa971/requests'
REQUESTS={'E3':'console_20261101T054937_4627267278','E4':'console_20261101T063050_143e464ed6','E5':'console_20261101T070128_753196545a'}

def saved(name):return json.loads((CASE/REQUESTS[name]/'pipeline.json').read_text(encoding='utf-8-sig'))

def source_only(project,version='v000000'):
    ref=dict(project_id=project,task_id='reference_TEST_ONLY',version_id=version,spec_digest='1'*64,source_sha256='2'*64,manifest_sha256='3'*64)
    return dict(kind='BASELINE_SOURCE',project_id=project,version_id=version,baseline_ref=ref,baseline_snapshot_sha256='4'*64,spec_digest=ref['spec_digest'],source_sha256=ref['source_sha256'],manifest_sha256=ref['manifest_sha256'],execution_evidence_present=False)

class ExplanationScopeRepair(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.records={name:saved(name) for name in REQUESTS}

    def check(self,text,binding=None,refs=None):
        record=self.records['E3'];binding=deepcopy(binding or record['current_fact_binding'])
        refs=deepcopy(refs if refs is not None else record['current_source_references'])
        return protect_explanation(text,binding,source_references=refs)['explanation_validation']

    def test_original_E3_E4_scope_replay_and_source_role(self):
        for name in ('E3','E4'):
            record=self.records[name];raw=record['explanation']['raw_answer'];before=deepcopy(record)
            without_original=self.check(raw,record['current_fact_binding'],record['current_source_references'])
            self.assertEqual({x['code'] for x in without_original['issues']},{'UNBOUND_VERSION_REFERENCE'})
            # Isolated source-role fixture, not a fresh physical/source audit.
            refs=record['current_source_references']+[source_only(record['state']['project_id'])]
            report=self.check(raw,record['current_fact_binding'],refs)
            self.assertTrue(report['body_displayed'],report['issues'])
            self.assertFalse(report['full_semantic_validation'])
            self.assertNotIn('v000000',report['known_execution_versions'])
            self.assertEqual(record,before)

    def test_list_scopes_headings_and_sibling_exit(self):
        positive=['- 最新达标版 v4：\n  - task_status=PASS\n  - physical_status=UNKNOWN',
                  '# v4\n## 结果\n1. 当前状态\n   - task_status=PASS',
                  '- v1：\n  - task_status=PASS\n- v4：\n  - task_status=PASS']
        for raw in positive:self.assertTrue(self.check(raw)['body_displayed'],raw)
        # E4 adds a real failed version; a sibling must not inherit v4 PASS.
        binding=deepcopy(self.records['E3']['current_fact_binding'])
        binding['explicitly_reviewed_executions'].append(self.records['E4']['current_fact_binding']['latest_request_execution'])
        for raw in ['- v4：\n  - task_status=PASS\n- v5：\n  - task_status=PASS',
                    '# v5\n## 子节\n- task_status=PASS',
                    '- v4 与 v5：\n  - task_status=PASS']:
            self.assertFalse(self.check(raw,binding)['body_displayed'],raw)

    def test_bound_source_is_not_execution_and_unknown_not_exempted(self):
        project=self.records['E3']['state']['project_id'];refs=[source_only(project)]
        for raw,expected in [('原v0保留，未修改。',True),('v0 task_status=NOT_RUN。',True),('v0 task_status=PASS。',False),('v0编译成功。',False),('v99 task_status=UNKNOWN。',False)]:
            self.assertEqual(self.check(raw,refs=refs)['body_displayed'],expected,raw)
        bad=source_only('FOREIGN_PROJECT')
        self.assertFalse(self.check('原v0保留，未修改。',refs=[bad])['body_displayed'])

    def test_negation_cross_version_and_same_version_foreign_project(self):
        record=self.records['E4'];binding=record['current_fact_binding'];refs=record['current_source_references']
        for raw,expected in [('# v5\n不能把任务FAIL说成任务PASS。',True),('# v5\n任务成功。',False),('# v5\n任务没有成功。',True),('# v50\ntask_status=PASS。',False)]:
            self.assertEqual(self.check(raw,binding,refs)['body_displayed'],expected,raw)
        foreign=deepcopy(binding['latest_request_execution']);foreign['project_id']='FOREIGN_PROJECT'
        changed=deepcopy(binding);changed['explicitly_reviewed_executions']=[foreign]
        report=self.check('v5 task_status=PASS。',changed,refs)
        self.assertFalse(report['body_displayed'])
        self.assertIn('CURRENT_FACT_BINDING_CONFLICT',{x['code'] for x in report['issues']})

class SelectedFactsRepair(unittest.TestCase):
    def ctx(self,root):
        return dict(root=str(root),baseline_version_id='v000001',baseline_ref=dict(version_id='v000001'),baseline_snapshot=dict(frozen_spec=dict(artifact=dict(sysml_source='package current end current;'))))

    def test_plan_and_console_share_selected_facts_and_historical_gap_is_not_current(self):
        with tempfile.TemporaryDirectory(dir=PKG/'B') as directory:
            ctx=self.ctx(Path(directory));obligations=dict(historical_missing=['尚无基准SysML文件'],missing=[])
            flow=tf.TaskFlow(ctx);first=flow._plan_source_facts(obligations);second=data.verified_source_facts(ctx)
            self.assertEqual(first,second);self.assertTrue(first['baseline_sysml_available'])
            self.assertEqual(first['generation_source_rule'],'CANDIDATE_ONLY')
            self.assertEqual(first['required_source_completion'],[])
            self.assertNotIn('historical_missing',first)

    def test_bound_completion_is_available_and_invalid_completion_fails_closed(self):
        with tempfile.TemporaryDirectory(dir=PKG/'B') as directory:
            ctx=self.ctx(Path(directory));ctx['baseline_snapshot']['frozen_spec']['artifact']['sysml_source']=''
            with patch.object(tf,'baseline_completion_status',return_value=dict(status='VERIFIED')),patch.object(tf,'baseline_sysml_for_context',return_value='valid current source'):
                facts=tf.selected_source_facts(ctx)
                self.assertFalse(facts['selected_baseline_sysml_present']);self.assertTrue(facts['baseline_sysml_available'])
                self.assertEqual(facts['generation_source_rule'],'CANDIDATE_ONLY')
            with patch.object(tf,'baseline_completion_status',side_effect=ValueError('TEST_ONLY binding drift')):
                with self.assertRaises(ValueError):tf.selected_source_facts(ctx)

    def test_E5_saved_false_prose_is_preserved_and_known_facts_win(self):
        record=saved('E5');packet=record['proposal']['plan'];before=deepcopy(packet)
        self.assertTrue(any('SysML' in x and '缺' in x for x in packet['defaults']))
        facts=record['proposal']['validation']['verified_source_facts']
        self.assertTrue(facts['selected_baseline_sysml_present']);self.assertEqual(facts['generation_source_rule'],'CANDIDATE_ONLY')
        self.assertEqual(packet,before)

class EarlyDisplayAndProgress(unittest.TestCase):
    def test_early_checkpoint_later_unknown_explanation_has_readonly_overlay(self):
        record=saved('E4');rid='TEST_ONLY_overlay'
        with tempfile.TemporaryDirectory(dir=PKG/'B') as directory:
            root=Path(directory);turn=root/'turns/explain_0';turn.mkdir(parents=True)
            (turn/'request.json').write_text(json.dumps(dict(request_id='TEST_ONLY_http',request_digest='bound')),encoding='utf8')
            flow=tf.TaskFlow(dict(root=directory,request_id=rid,request_digest='bound'))
            state=deepcopy(record['state']);state['http_state']=dict(unknown_requests=[dict(request_id='TEST_ONLY_http',reason='TEST_ONLY_timeout')])
            original=dict(partial=dict(explanation=dict(status='NOT_RUN')),state=state)
            before=deepcopy(original);value=flow._terminal_explanation_overlay(original,error='TEST_ONLY_transport')
            self.assertEqual(value['partial']['explanation']['status'],'UNKNOWN')
            self.assertEqual(value['partial']['historical_explanation']['status'],'NOT_RUN')
            self.assertEqual(original,before)

    def test_small_pointer_same_operation_only_and_sticky_after_http(self):
        record=saved('E4');rid=REQUESTS['E4'];project=record['state']['project_id'];latest=record['state']['latest_request_execution']
        with tempfile.TemporaryDirectory(dir=PKG/'B') as directory:
            root=Path(directory);request=root/'runs/requests'/rid;request.mkdir(parents=True)
            value=dict(stage='EXECUTION_TERMINAL_EXPLANATION_INCOMPLETE',request_id=rid,execution=deepcopy(latest),state=record['state'],explanation=dict(status='NOT_COMPLETED'))
            path=request/'pipeline-partial.json';raw=json.dumps(value,ensure_ascii=False).encode('utf8');path.write_bytes(raw)
            pointer=dict(path=str(path.resolve()),sha256=hashlib.sha256(raw).hexdigest(),request_id=rid,project_id=project,task_id=latest['task_id'],version_id=latest['version_id'])
            progress_path=root/'operation.progress.json'
            with patch.dict(os.environ,{'MBSE_V12_PROGRESS_FILE':str(progress_path),'MBSE_V12_REQUEST_ID':rid}),patch.object(ui,'ROOT',root):
                emit_progress('result_available','结果已保存',result_ready=pointer)
                emit_progress('protocol_validation','HTTP结束，正在解析',http_request_id='TEST_ONLY_http')
                progress=json.loads(progress_path.read_text(encoding='utf8'))
                early=ui.CliRunner.early_result(progress,['--request-id',rid])
                self.assertIsNotNone(early);self.assertIn('任务未完成',early[1][0])
                self.assertIsNone(ui.CliRunner.early_result(progress,['--request-id','OTHER_REQUEST']))
                bad=deepcopy(progress);bad['details']['result_ready']['task_id']='OTHER_TASK'
                self.assertIsNone(ui.CliRunner.early_result(bad,['--request-id',rid]))
                emit_progress('readback','其他操作',request_id='OTHER_REQUEST')
                self.assertNotIn('result_ready',json.loads(progress_path.read_text(encoding='utf8'))['details'])

    def test_snapshot_reuse_and_index_state_change_invalidate(self):
        flow=tf.TaskFlow(dict(root='TEST_ONLY',project_id='P'));svc=Mock(project_root='TEST_ONLY')
        first=dict(project_id='P',version_id='v1',task_id='T',state='succeeded',updated_at='a',current_contact_review={'status':'PASS'})
        svc._artifact_version.return_value=first
        flow._selected_snapshot('v1',svc=svc,index=first)
        flow._selected_snapshot('v1',svc=svc,index=first)
        self.assertEqual(svc._artifact_version.call_count,1)
        second=dict(first,updated_at='b');svc._artifact_version.return_value=second
        flow._selected_snapshot('v1',svc=svc,index=second)
        self.assertEqual(svc._artifact_version.call_count,2)
        with self.assertRaises(ValueError):flow._selected_snapshot('v1',svc=svc,snapshot=dict(second,project_id='FOREIGN'))

    def test_new_command_never_reuses_same_stat_changed_content(self):
        with tempfile.TemporaryDirectory(dir=PKG/'B') as directory:
            path=Path(directory)/'TEST_ONLY_evidence.txt';path.write_bytes(b'original');old=path.stat()
            expected=hashlib.sha256(path.read_bytes()).hexdigest()
            snapshot=dict(project_id='P',version_id='v1',task_id='T',state='succeeded',current_contact_review={'status':'PASS'})
            def verified(_):
                if hashlib.sha256(path.read_bytes()).hexdigest()!=expected:raise ValueError('TEST_ONLY content binding differs')
                return deepcopy(snapshot)
            svc=Mock(project_root='TEST_ONLY');svc._artifact_version.side_effect=verified
            first=tf.TaskFlow(dict(root=directory,project_id='P'));first._selected_snapshot('v1',svc=svc)
            path.write_bytes(b'modified');os.utime(path,ns=(old.st_atime_ns,old.st_mtime_ns))
            self.assertEqual(path.stat().st_size,old.st_size);self.assertEqual(path.stat().st_mtime_ns,old.st_mtime_ns)
            # The first operation describes its frozen verified snapshot.
            first._selected_snapshot('v1',svc=svc)
            second=tf.TaskFlow(dict(root=directory,project_id='P'))
            with self.assertRaisesRegex(ValueError,'content binding'):second._selected_snapshot('v1',svc=svc)

    def test_default_state_is_lightweight_and_keeps_recorded_fail(self):
        with tempfile.TemporaryDirectory(dir=PKG/'B') as directory:
            ctx=dict(root=directory,project_id='P',request_id='R',session_id='S',baseline_ref={},stage='DONE')
            row=dict(project_id='P',task_id='T',version_id='v5',session_id='S',state='succeeded',record_kind='ARTIFACT_EXECUTION_INDEX',recorded_tool_status='PASS',recorded_chain_status='PASS',recorded_task_status='FAIL',verification_status='NOT_RECHECKED')
            svc=Mock();svc.versions_index.return_value={'versions':{'items':[row]}}
            flow=tf.TaskFlow(ctx)
            with patch.object(flow,'open',return_value=svc),patch.object(flow,'http_state',return_value=None),patch.object(tf,'baseline_completion_status',return_value={'status':'VERIFIED'}),patch.object(flow,'_selected_snapshot',side_effect=AssertionError('no full snapshot in status')):
                state=flow.state()
            latest=state['latest_request_execution']
            self.assertEqual(latest['recorded_task_status'],'FAIL');self.assertEqual(latest['verification_status'],'NOT_RECHECKED')
            self.assertIn('已记录任务失败',ui.listed_result(latest))
            svc.versions_list.assert_not_called();svc.store.task.assert_not_called()

if __name__=='__main__':unittest.main()
