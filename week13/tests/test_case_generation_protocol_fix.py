"""Isolated source-reference migration; no real HTTP/MCP/compiler/ledger."""
import asyncio
from contextlib import ExitStack,redirect_stdout,closing
from copy import deepcopy
from datetime import datetime,timedelta,timezone
import io,json,os,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch

from week13.src import case_paths,case_requests,case_taskflow,case_projects,case_cli,case_progress
from week13.src.case_budget import CaseBudget
from week13.tests.test_case_agent_requests import Harness,synthetic_spec,write

EVIDENCE=case_paths.ROOT/'results/cli-flow-fixes-20261020-01'


class GenerationProtocolFix(unittest.TestCase):
    def setUp(self):
        EVIDENCE.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='backend-generation-',dir=EVIDENCE)
        self.stack=ExitStack()
        for key in ('ROOT','REQUESTS_ROOT','LEGACY_PROJECT_ROOT'):
            self.stack.enter_context(patch.object(case_requests,key,getattr(case_requests,key)))
        self.h=Harness(Path(self.temp.name)/'fixture')
        self.rid='TEST_ONLY_source_migration'
        self.h.bootstrap(self.rid,self.h.project_id,'v000002','把总质量改为92kg，增加部分在原重心，几何控制不变。')
        self.h.authorize(self.rid)
        self.flow=self.h.flow(self.rid)
        planned=asyncio.run(self.flow.plan());self.flow.confirm(planned['displayed_digest'],'CONFIRM')
        self.flow.refresh();self.root=Path(self.flow.ctx['root'])
        self.original_transport=self.h.transport
    def tearDown(self):
        self.stack.close();self.temp.cleanup()
    def failures(self,repair=False,extra=None):
        def wire(ctx,payload):
            response=self.original_transport(ctx,payload)
            args=json.loads(response['choices'][0]['message']['tool_calls'][0]['function']['arguments'])
            args['spec']['sources'][1]['text']='完整已知回复的历史引用被模型改写。'
            if extra:extra(args)
            response['choices'][0]['message']['tool_calls'][0]['function']['arguments']=json.dumps(args,ensure_ascii=False)
            return response
        self.h.transport=wire
        with self.assertRaisesRegex(ValueError,'source text differs'):asyncio.run(self.flow.generate())
        if repair:
            with self.assertRaisesRegex(ValueError,'source text differs'):asyncio.run(self.flow.repair('generate',1))
    def rewrite_response(self,change,attempt=0):
        stage='generate' if not attempt else 'generate_repair_'+str(attempt)
        response_path=self.root/'turns'/stage/'response.json';response=case_paths.read_json(response_path)
        args=json.loads(response['tool_calls'][0]['function']['arguments']);change(args)
        response['tool_calls'][0]['function']['arguments']=json.dumps(args,ensure_ascii=False)
        # Fixture-only: bind the same known reply into its local failure/ledger.
        failure_path=self.root/'failures'/('generate-'+str(attempt)+'.json');failure=case_paths.read_json(failure_path);failure['response']=response
        write(response_path,response);write(failure_path,failure)
        with closing(sqlite3.connect(self.flow.ctx['runtime_authority']['ledger_path'])) as db:
            with db:db.execute('UPDATE requests SET response=? WHERE request_id=?',(json.dumps(response,ensure_ascii=False),'case_'+stage+'_'+self.rid))
    def snapshot(self):
        return {str(p.relative_to(self.root)):case_paths.sha_file(p) for p in self.root.rglob('*') if p.is_file()}
    def test_id_only_contract_and_explicit_text_guard(self):
        self.assertIn('不得返回text',case_taskflow.GEN_PROMPT)
        schema=case_taskflow.GEN_TOOL['function']['parameters']['properties']['spec']['properties']['sources']['items']
        self.assertEqual(schema['required'],['id']);self.assertFalse(schema['additionalProperties'])
        svc=self.flow.open(read_only=True);spec=synthetic_spec(self.flow.ctx['sources'][0],92)
        spec['sources']=[dict(id=x['id']) for x in self.flow.ctx['sources']]
        self.assertEqual(svc.normalize_candidate(spec)[0]['sources'],self.flow.ctx['sources'])
        spec['sources'][0]['text']='changed'
        with self.assertRaisesRegex(ValueError,'source text differs'):svc.normalize_candidate(spec)
    def test_saved_reply_migration_zero_counters_state_or_http_and_immutable_originals(self):
        self.failures(repair=True);before=self.snapshot()
        ledger=Path(self.flow.ctx['runtime_authority']['ledger_path']);ledger_before=ledger.read_bytes()
        with patch.object(self.flow,'_runtime',side_effect=AssertionError('no HTTP')),patch.object(self.flow,'mcp',side_effect=AssertionError('no MCP')),patch.object(case_requests,'require_execution',side_effect=AssertionError('no writable execution binding')):
            result=self.flow.revalidate_generation(attempt=1)
        self.assertFalse(result['proposal_prepared']);self.assertFalse(result['product_generation_success']);self.assertEqual(result['product_http_added'],0)
        self.assertEqual(ledger.read_bytes(),ledger_before)
        for path,digest in before.items():self.assertEqual(case_paths.sha_file(self.root/path),digest,path)
        self.assertFalse((self.root/'generation.json').exists())
        self.assertEqual(self.flow.revalidate_generation(attempt=1),result)
        self.assertEqual(self.flow._read_revalidated_generation()[0],result['spec'])
    def test_run_consumes_candidate_after_repair_exhausted_without_generate_or_repair2(self):
        self.failures(repair=True);self.flow.revalidate_generation(attempt=1)
        before=CaseBudget(task_context=self.flow.ctx).usage();self.h.transport=self.original_transport
        with patch.object(self.flow,'call',wraps=self.flow.call) as calls,patch.object(self.flow,'repair',side_effect=AssertionError('repair2 forbidden')):
            result=asyncio.run(self.flow.run())
        self.assertEqual(result['stage'],'RESULT_READ_BACK')
        self.assertTrue(all(not call.args[0].startswith('generate') for call in calls.call_args_list))
        after=CaseBudget(task_context=self.flow.ctx).usage()
        self.assertEqual(after['counters'],before['counters']);self.assertEqual(after['requests'],before['requests']+1) # explanation only
        self.assertTrue((self.root/'generation.json').exists());self.assertTrue((self.root/'failures/generate-1.json').exists())
    def test_unknown_and_missing_candidate_event_refused(self):
        self.failures()
        ledger=self.flow.ctx['runtime_authority']['ledger_path']
        with closing(sqlite3.connect(ledger)) as db:
            with db:db.execute("UPDATE requests SET status='unknown' WHERE category='generation'")
        with self.assertRaisesRegex(ValueError,'known complete'):self.flow.revalidate_generation()
        with closing(sqlite3.connect(ledger)) as db:
            with db:
                db.execute("UPDATE requests SET status='succeeded' WHERE category='generation'");db.execute("DELETE FROM counters WHERE kind='candidate'")
        with self.assertRaisesRegex(ValueError,'candidate event'):self.flow.revalidate_generation()
    def test_source_id_order_or_non_source_physics_change_refused(self):
        self.failures();original=case_paths.read_json(self.root/'turns/generate/response.json')
        self.rewrite_response(lambda args:args['spec']['sources'].reverse())
        with self.assertRaisesRegex(ValueError,'source IDs'):self.flow.revalidate_generation()
        self.rewrite_response(lambda args:args['spec']['sources'].reverse())
        self.rewrite_response(lambda args:args['spec']['artifact'].update(modelica_source=args['spec']['artifact']['modelica_source'].replace('der(signal)=0','der(signal)=1')))
        with self.assertRaisesRegex(ValueError,'frozen physics'):self.flow.revalidate_generation()
        self.assertFalse((self.root/'generation-revalidated.json').exists())
    def test_incomplete_cross_request_and_confirmation_drift_refused(self):
        self.failures();response_path=self.root/'turns/generate/response.json';response=case_paths.read_json(response_path);changed=deepcopy(response);changed['finish_reason']='length';write(response_path,changed)
        with self.assertRaisesRegex(ValueError,'binding'):self.flow.revalidate_generation()
        write(response_path,response);request_path=self.root/'turns/generate/request.json';request=case_paths.read_json(request_path);changed=deepcopy(request);changed['request_id']='case_generate_OTHER';write(request_path,changed)
        with self.assertRaisesRegex(ValueError,'binding'):self.flow.revalidate_generation()
        write(request_path,request);context=case_paths.read_json(self.root/'task-context.json');context['stage']='PROPOSED';write(self.root/'task-context.json',context)
        with self.assertRaisesRegex(PermissionError,'confirmed'):self.flow.revalidate_generation()
    def test_expired_authority_refused_without_writes(self):
        self.failures();before=self.snapshot()
        class Future(datetime):
            @classmethod
            def now(cls,tz=None):return datetime.now(timezone.utc)+timedelta(hours=2)
        with patch.object(case_taskflow,'datetime',Future),self.assertRaisesRegex(PermissionError,'active original budget'):self.flow.revalidate_generation()
        self.assertEqual(before,self.snapshot())
    def test_saved_stage_payload_cannot_be_rebuilt_by_new_prompt(self):
        self.failures();before=self.snapshot()
        with self.assertRaisesRegex(ValueError,'stage request changed'):
            asyncio.run(self.flow.call('generate',[dict(role='system',content='new prompt')],[]))
        self.assertEqual(before,self.snapshot())
    def test_modified_revalidated_candidate_rejected_before_mcp(self):
        self.failures();saved=self.flow.revalidate_generation();saved['spec']['design']['summary']='modified outside source migration';write(self.root/'generation-revalidated.json',saved)
        with patch.object(self.flow,'mcp',side_effect=AssertionError('no MCP')),self.assertRaisesRegex(ValueError,'outside source reference'):asyncio.run(self.flow.generate())
    def test_cli_entry_is_explicit_offline(self):
        self.failures();output=io.StringIO()
        with redirect_stdout(output):code=case_cli.main(['revalidate-generation','--request-id',self.rid,'--attempt','0'],flow_factory=lambda rid:self.flow)
        self.assertEqual(code,0,output.getvalue());self.assertEqual(json.loads(output.getvalue())['stage'],'VALIDATED_CANDIDATE_PENDING_PROPOSAL')


class Progress(unittest.TestCase):
    def test_progress_is_opt_in_atomic_and_stdout_silent(self):
        with tempfile.TemporaryDirectory(prefix='backend-progress-',dir=EVIDENCE) as folder:
            path=Path(folder)/'operation.progress.json';output=io.StringIO()
            with patch.dict(os.environ,{},clear=True),redirect_stdout(output):case_progress.emit_progress('plan','整理需求')
            self.assertEqual(list(Path(folder).iterdir()),[])
            with patch.dict(os.environ,MBSE_V12_PROGRESS_FILE=str(path)),redirect_stdout(output):
                case_progress.emit_progress('compile','真实编译',task_id='TEST_ONLY')
                case_progress.emit_progress('independent_check','检查输出')
            self.assertEqual(output.getvalue(),'');self.assertEqual(case_paths.read_json(path)['stage'],'independent_check');self.assertEqual(list(Path(folder).iterdir()),[path])
    def test_progress_permission_error_does_not_interrupt_work(self):
        with patch.dict(os.environ,MBSE_V12_PROGRESS_FILE=str(EVIDENCE/'backend-progress-error.json')),patch.object(Path,'write_text',side_effect=PermissionError()),patch.object(Path,'unlink',side_effect=PermissionError()):case_progress.emit_progress('plan','整理需求')


class ImportedReferenceWithoutSuccessStatement(unittest.TestCase):
    def setUp(self):
        from week13.tests import test_case_projects
        self.evidence=patch.object(test_case_projects,'EVIDENCE',EVIDENCE);self.evidence.start()
        self.helper=test_case_projects.CaseProjects();self.helper.setUp()
    def tearDown(self):
        self.helper.tearDown();self.evidence.stop()
    def test_import_discover_plan_without_historical_statement(self):
        h=self.helper
        inspection=case_projects.inspect_case(h.inputs)
        self.assertEqual(inspection['historical_success']['status'],'USER_PROVIDED_REFERENCE_HISTORY_NOT_INDEPENDENTLY_VERIFIED')
        case=case_projects.import_case(h.inputs,'TEST_ONLY no success statement')['case']
        self.assertEqual(case['historical_success_statement'],'');self.assertFalse(case['historical_success_verified'])
        self.assertFalse(any('请提供用户确认' in x for x in case['missing']))
        self.assertTrue(case_projects.discover_case(case['case_id'])['default_baseline']['selectable'])
        ctx=h.bootstrap(case,'TEST_ONLY_no_success');h.authorize(ctx['request_id']);flow=h.flow(ctx['request_id'])
        result=asyncio.run(flow.plan());self.assertTrue(result['validation']['executable'])
        self.assertEqual(case_taskflow.imported_obligations(flow.ctx)['historical_success_statement'],'')
    def test_missing_original_sysml_migration_retains_product_sidecar_and_normal_run(self):
        h=self.helper;case=case_projects.import_case(h.inputs,'TEST_ONLY no success statement')['case']
        ctx=h.bootstrap(case,'TEST_ONLY_missing_sysml');h.authorize(ctx['request_id']);flow=h.flow(ctx['request_id'])
        planned=asyncio.run(flow.plan());flow.confirm(planned['displayed_digest'],'CONFIRM')
        transport=h.transport
        def bad_text(ctx,payload):
            response=transport(ctx,payload);args=json.loads(response['choices'][0]['message']['tool_calls'][0]['function']['arguments'])
            args['spec']['sources'][1]['text']='改写的历史原文不能通过常规校验。'
            response['choices'][0]['message']['tool_calls'][0]['function']['arguments']=json.dumps(args,ensure_ascii=False)
            return response
        h.transport=bad_text
        with self.assertRaisesRegex(ValueError,'source text differs'):asyncio.run(flow.generate())
        state_before=case_paths.read_json(Path(ctx['root'])/'task-context.json');ledger=Path(flow.ctx['runtime_authority']['ledger_path']);before=ledger.read_bytes()
        saved=flow.revalidate_generation();self.assertEqual(ledger.read_bytes(),before)
        self.assertEqual(case_paths.read_json(Path(ctx['root'])/'task-context.json'),state_before)
        sidecar=case_paths.read_json(Path(ctx['root'])/'baseline-sysml-completion.json')
        self.assertEqual(saved['baseline_completion_sha256'],case_paths.sha_value(sidecar));self.assertTrue(case_taskflow.baseline_sysml_for_context(flow.ctx))
        self.assertNotIn('sysml_source',ctx['baseline_snapshot']['frozen_spec']['artifact'])
        h.transport=transport;result=asyncio.run(flow.run());self.assertEqual(result['stage'],'RESULT_READ_BACK')
        self.assertEqual(result['generation']['baseline_completion_sha256'],saved['baseline_completion_sha256'])


if __name__=='__main__':unittest.main()
