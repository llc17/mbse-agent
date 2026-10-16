"""Negative admission/accounting fixtures only; no real runtime or physics."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime,timezone,timedelta
from pathlib import Path
import os, tempfile, unittest
from unittest.mock import patch
from week12.src.car_domain import reference_route as a

@contextmanager
def fake_transaction(scope):yield {},scope

class ReferenceRoutePolicy(unittest.TestCase):
    def test_closed_historical_ledger_drift_rejected(self):
        data=a.read(a.LEDGER);changed=deepcopy(data)
        changed['car_domain_increment']['status']='REOPENED'
        with self.assertRaises(a.Stop):a.validate_authority(changed)
        changed=deepcopy(data);changed[a.KEY]['caps']['scene_attempts']=3
        with self.assertRaises(a.Stop):a.validate_authority(changed)
    def test_source_closure_missing_and_added_rejected(self):
        rows=[dict(path='week12/src/car_domain/reference_route.py',sha256='0'*64)]
        with patch.object(a,'source_files',return_value=rows):
            with self.assertRaises(a.Stop):a.assert_binding(dict(files=[],source_file_set=[],third_party_versions={}))
    def test_milestone_missing_actual_new_csv_stops(self):
        s=deepcopy(a.read(a.LEDGER)[a.KEY]);s['status']='IMPLEMENTING_AUTHORIZED'
        with patch.object(a,'elapsed',return_value=7200):
            with self.assertRaises(a.Stop):a.assert_scope(s)
    def test_stopped_scope_cannot_reopen_with_flag(self):
        s=deepcopy(a.read(a.LEDGER)[a.KEY]);s['status']='STOPPED_REFERENCE_TOOL'
        with self.assertRaises(a.Stop):a.assert_scope(s,allow_affected=True)
    def test_unrelated_activity_not_subtracted_from_parent(self):
        now=datetime.now(timezone.utc);s=deepcopy(a.read(a.LEDGER)[a.KEY])
        s['parents']=[dict(token='test',label='fixture',pid=os.getpid(),status='RESERVED',parent_token=None,
            started_utc=(now-timedelta(seconds=35)).isoformat())]
        s['operations']=[dict(status='RETURNED',parent_token=None,started_utc=(now-timedelta(seconds=50)).isoformat(),ended_utc=now.isoformat())]
        s['product_requests']=[];before=s['external_s']
        with patch.object(a,'transaction',lambda:fake_transaction(s)):a.finish_parent('test',35,status='RETURNED')
        self.assertAlmostEqual(s['external_s']-before,35,places=5)
        self.assertEqual(s['status'],'STOPPED_PARENT_OR_CLEANUP')
        self.assertEqual(s['parents'][0]['status'],'OVER_BUDGET')
    def test_only_actual_descendant_intervals_deduplicated(self):
        now=datetime.now(timezone.utc);s=deepcopy(a.read(a.LEDGER)[a.KEY])
        s['parents']=[dict(token='test',label='fixture',pid=os.getpid(),status='RESERVED',parent_token=None,
            started_utc=(now-timedelta(seconds=20)).isoformat())]
        s['operations']=[dict(status='RETURNED',parent_token='test',started_utc=(now-timedelta(seconds=17)).isoformat(),ended_utc=(now-timedelta(seconds=2)).isoformat())]
        s['product_requests']=[];before=s['external_s']
        with patch.object(a,'transaction',lambda:fake_transaction(s)):a.finish_parent('test',20,status='RETURNED')
        self.assertAlmostEqual(s['external_s']-before,5,delta=.1)
    def test_unknown_product_usage_keeps_reserve_and_stops(self):
        s=deepcopy(a.read(a.LEDGER)[a.KEY]);before_tokens=s['product_tokens'];s['product_requests']=[dict(token='req',request_id='fixture',status='RESERVED',
            reserved_tokens=20096,reserved_usd=.0097152,started_utc=a.stamp())]
        with patch.object(a,'transaction',lambda:fake_transaction(s)):a.finish_product_request('req',1,status='UNKNOWN')
        self.assertEqual(s['product_requests'][0]['status'],'UNKNOWN_USAGE')
        self.assertEqual(s['product_tokens'],before_tokens+20096)
        self.assertEqual(s['status'],'STOPPED_PRODUCT_UNKNOWN_OR_LIMIT')
    def test_nonzero_or_timeout_cannot_pass_from_existing_report(self):
        for code,timeout in ((2,False),(0,True)):
            s=deepcopy(a.read(a.LEDGER)[a.KEY]);s['status']='EXECUTING_REFERENCE';s['operations']=[dict(kind='check',status='RETURNED',duration_s=.5)];s['counts']['scene_children']=0
            s['execution_claim']=dict(token='owned',task_id='task_fixture',owner='owner_fixture',pid=os.getpid())
            route=object.__new__(a.ReferenceRoute);route.claim_token='owned';route.task={'task_id':'task_fixture'};route.owner='owner_fixture';route.binding={}
            result=dict(returncode=code,stdout='{"status":"PASS"}',stderr='',duration_s=.01,timed_out=timeout,
                uncertain=False,interrupted=False,cleanup_succeeded=None,cleanup_error='',error='')
            with tempfile.TemporaryDirectory(dir=a.DEFAULT_OUT,prefix='process-policy-fixture-') as folder:
                with patch.object(a,'transaction',lambda:fake_transaction(s)),patch.object(a,'assert_scope'),patch.object(a,'assert_binding'),patch.object(a,'bounded_run',return_value=result):
                    with self.assertRaises(a.Stop):route.child('scene','fixture',['NOT_EXECUTED'],Path(folder),60)
            self.assertEqual(s['status'],'STOPPED_REFERENCE_TOOL')
    def test_successful_child_records_result_after_tokenless_readiness_operation(self):
        s=deepcopy(a.read(a.LEDGER)[a.KEY]);s['status']='EXECUTING_REFERENCE'
        check=dict(kind='check',status='RETURNED',duration_s=.5)
        s['operations']=[check.copy()];s['counts']['scene_children']=0
        s['execution_claim']=dict(token='owned',task_id='task_fixture',owner='owner_fixture',pid=os.getpid())
        route=object.__new__(a.ReferenceRoute);route.claim_token='owned';route.task={'task_id':'task_fixture'};route.owner='owner_fixture';route.binding={}
        result=dict(returncode=0,stdout='ok',stderr='',duration_s=.01,timed_out=False,uncertain=False,interrupted=False)
        before=s['external_s']
        with tempfile.TemporaryDirectory(dir=a.DEFAULT_OUT,prefix='mixed-operation-fixture-') as folder:
            with patch.object(a,'transaction',lambda:fake_transaction(s)),patch.object(a,'assert_scope'),patch.object(a,'assert_binding'),patch.object(a,'bounded_run',return_value=result):
                returned=route.child('scene','fixture',['NOT_EXECUTED'],Path(folder),60)
            self.assertEqual(returned,result)
            self.assertEqual(a.read(Path(folder)/'fixture-process.json')['returncode'],0)
        self.assertEqual(s['operations'][0],check)
        self.assertEqual(s['operations'][1]['status'],'RETURNED')
        self.assertAlmostEqual(s['external_s'],before+.01)

    def test_reference_backend_has_no_legacy_vehicle_execution_or_failure_audit(self):
        import ast
        tree=ast.parse(Path(a.__file__).read_text(encoding='utf8'))
        names=[n.module or '' for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]
        self.assertFalse(any('active_cluster' in n or 'failure_evidence' in n for n in names))
    def test_proven_local_cleanup_dependency_is_bound(self):
        paths={r['path'] for r in a.source_files()}
        for name in ('week9/src/modelica_tool.py','week9/src/hard_gate.py','week12/src/toolchain.py','week11/src/store.py'):
            self.assertIn(name,paths)

    def test_startup_deducted_from_absolute_process_deadline(self):
        from unittest.mock import Mock
        process=Mock();process.communicate.return_value=(b'ok',b'');process.returncode=0;process.pid=123
        # Simulated 2s Popen startup within a 20s whole-operation cap.
        times=iter([100.,102.,102.1])
        with patch('subprocess.Popen',return_value=process),patch.object(a.time,'monotonic',side_effect=lambda:next(times)),patch('week12.src.toolchain._run_result',return_value={'duration_s':2.1,'uncertain':False,'error':''}):
            # Use actual clock for _run_result; inspect only the communicated timeout via a separate fake result.
            with patch('week12.src.toolchain._run_result',return_value={'duration_s':2.1,'uncertain':False,'error':''}):
                a.bounded_run(['NOT_EXECUTED'],a.PROJECT,20)
        self.assertLessEqual(process.communicate.call_args.kwargs['timeout'],15)

    def test_completion_requires_actual_worker_exit_and_account(self):
        # Absence of actual artifacts/exit can never be a completed reference.
        with self.assertRaises(a.Stop):a.verify_completed_evidence({'files':{}},a.SUCCESS,
            {'project_id':'prj_fixture','version_id':'v1','task_id':'task_fixture'})

def completion_fixture(task):
    """Synthetic budget record, never a real execution receipt."""
    return dict(status='EXECUTING_REFERENCE',started_utc=datetime.now(timezone.utc).isoformat(),
        external_s=0,product_tokens=0,product_usd=0,counts={},operations=[],product_requests=[],
        execution_claim=dict(task_id=task['task_id'],owner=task['worker_owner'],pid=task['worker_pid']),
        parents=[dict(token='cli',label='fixture-live-cli',pid=456,status='RESERVED',parent_token=None),
                 dict(token='worker',label='car-worker:'+task['task_id'],pid=task['worker_pid'],
                      actual_exit_code=0,status='RETURNED',own_s=1,parent_token='cli')])


class ReferenceCompletionPolicy(unittest.TestCase):
    def setUp(self):
        self.task=dict(task_id='task_fixture',worker_owner='owner_fixture',worker_pid=123)
        self.scope=completion_fixture(self.task)

    def test_task_can_finish_while_cli_remains_open_and_counts_at_cap(self):
        self.scope['counts']={k:a.CAPS[k] for k in ('scenes','scene_children','parents','checks','product_requests')}
        with patch.object(a,'elapsed',return_value=10):
            a.assert_completion_scope(self.scope,self.task)

    def test_stopped_and_uncertain_scopes_cannot_complete_exit_zero(self):
        for state in ('STOPPED_PARENT_OR_CLEANUP','UNCERTAIN','CLOSED_BLOCKED'):
            with self.subTest(state=state):
                self.scope['status']=state
                with self.assertRaises(a.Stop):a.assert_completion_scope(self.scope,self.task)

    def test_worker_own_overrun_cannot_complete_even_if_scope_looks_live(self):
        self.scope['parents'][1]['own_s']=31
        with self.assertRaises(a.Stop):a.assert_completion_scope(self.scope,self.task)

    def test_total_and_product_budget_overruns_cannot_complete(self):
        with patch.object(a,'elapsed',return_value=10801):
            with self.assertRaises(a.Stop):a.assert_completion_scope(self.scope,self.task)
        for field,value in (('external_s',1861),('product_tokens',80385),('product_usd',.1001)):
            with self.subTest(field=field):
                scope=completion_fixture(self.task);scope[field]=value
                with self.assertRaises(a.Stop):a.assert_completion_scope(scope,self.task)

    def test_unfinished_worker_descendant_blocks_but_live_cli_does_not(self):
        self.scope['operations']=[dict(parent_token='worker',status='RESERVED')]
        with self.assertRaises(a.Stop):a.assert_completion_scope(self.scope,self.task)

    def test_owner_mismatch_or_nonzero_actual_worker_exit_rejected(self):
        self.scope['execution_claim']['owner']='different_owner'
        with self.assertRaises(a.Stop):a.assert_completion_scope(self.scope,self.task)
        self.scope=completion_fixture(self.task);self.scope['parents'][1]['actual_exit_code']=2
        with self.assertRaises(a.Stop):a.assert_completion_scope(self.scope,self.task)


class ReferenceExecutionBinding(unittest.TestCase):
    def test_new_scope_needs_matching_receipt_and_preserves_closed_snapshot(self):
        from week12.src.car_domain import reference_execution as e
        import hashlib,json
        with tempfile.TemporaryDirectory(prefix='execution-binding-fixture-',dir=a.DEFAULT_OUT) as tmp:
            out=Path(tmp);config=out/'active.json'
            caps=dict(e.DEFAULT_CAPS,active_s=3600,analysis_s=2720,external_s=880,
                product_requests=1,product_tokens=20096,product_usd=.01)
            scope=dict(e.DEFAULT,key='fixture_new_scope',scope_id='fixture-only-not-authorized',
                caps=caps,csv_deadline_s=1800,artifact_prefix='fixture-new',
                proposal_file='proposal.json',authorization_file='receipt.json',snapshot_file='old.json')
            def put(name,value):
                raw=json.dumps(value,sort_keys=True).encode();(out/name).write_bytes(raw)
                return hashlib.sha256(raw).hexdigest()
            scope['snapshot_sha256']=put('old.json',{'reference_core_chain':{'status':'CLOSED_BLOCKED'}})
            proposal=dict(reference_approval_sha256=e.REFERENCE_APPROVAL_SHA,caps=caps,
                csv_deadline_s=1800,ledger_key=scope['key'])
            scope['proposal_sha256']=put('proposal.json',proposal)
            receipt=dict(status='APPROVED',proposal_sha256=scope['proposal_sha256'],scope_id=scope['scope_id'],
                old_ledger_sha256=scope['snapshot_sha256'],old_ledger_snapshot_file='old.json',
                profile=a.PROFILE,reference_source_sha256=a.SOURCE_SHA,fixture_only=True)
            scope['authorization_sha256']=put('receipt.json',receipt)
            put('active.json',scope)
            with patch.object(e,'CONFIG',config),patch.object(e,'OUT',out):
                self.assertEqual(e.load_execution()['key'],'fixture_new_scope')
                receipt['status']='PROPOSED';scope['authorization_sha256']=put('receipt.json',receipt);put('active.json',scope)
                with self.assertRaises(ValueError):e.load_execution()
                receipt['status']='APPROVED';scope['authorization_sha256']=put('receipt.json',receipt)
                scope['key']='reference_core_chain';put('active.json',scope)
                with self.assertRaises(ValueError):e.load_execution()

    def test_execution_binding_rejects_paths_outside_result_directory(self):
        from week12.src.car_domain import reference_execution as e
        with self.assertRaises(ValueError):e._read_bound('../other.json','unused')


class ReferenceAcceptanceFinalAccount(unittest.TestCase):
    def test_host_overrun_after_case_prevents_final_success(self):
        from week12.scripts import s3_c_car_increment_acceptance as runner
        from unittest.mock import AsyncMock
        import sys
        for stopped in (False,True):
            with self.subTest(stopped=stopped),tempfile.TemporaryDirectory(prefix='acceptance-final-fixture-',dir=a.DEFAULT_OUT) as tmp:
                folder=Path(tmp);binding=folder/'binding.json';a.save(binding,{})
                candidate=dict(status='REFERENCE_CASE_COMPLETED',reference_chain_status='REFERENCE_CHAIN_PASS',fixture_only=True)
                a.save(folder/(runner.EXECUTION['artifact_prefix']+'-online-candidate.json'),candidate)
                scope=dict(status='EXECUTING_REFERENCE',started_utc=a.stamp(),external_s=0,
                    admission=dict(binding_path=str(binding)),parents=[dict(token='fixture-host',parent_token=None,status='RETURNED'),
                        dict(token='previous-failed-host',parent_token=None,status='UNKNOWN',actual_exit_code=1)])
                def finish(*args,**kwargs):
                    if stopped:scope['status']='STOPPED_PARENT_OR_CLEANUP'
                with patch.object(runner,'DEFAULT_OUT',folder),patch.object(sys,'argv',['fixture','online']),                     patch.object(runner,'online',new=AsyncMock(return_value=0)),                     patch.object(a,'transaction',lambda:fake_transaction(scope)),                     patch.object(a,'assert_binding'),patch.object(a,'reserve_parent',return_value='fixture-host'),                     patch.object(a,'set_parent_context'),patch.object(a,'finish_parent',side_effect=finish):
                    code=runner.main()
                report=a.read(folder/(runner.EXECUTION['artifact_prefix']+'-online-result.json'))
                self.assertEqual(code,2 if stopped else 0)
                self.assertEqual(report['reference_chain_status'],'UNKNOWN' if stopped else 'REFERENCE_CHAIN_PASS')


class ConfigurationAdmissionReporting(unittest.TestCase):
    def test_configuration_admission_does_not_turn_partial_independent_review_into_pass(self):
        with tempfile.TemporaryDirectory(prefix='admission-report-fixture-',dir=a.DEFAULT_OUT) as tmp:
            folder=Path(tmp);binding=folder/'binding.json';ready=folder/'ready.json';review=folder/'review.json'
            a.save(binding,{'fixture_only':True})
            a.save(ready,dict(status='PASS_IMPLEMENTATION_ONLY',binding_sha256=a.sha(binding)))
            a.save(review,dict(allow_execution=True,binding_sha256=a.sha(binding),
                review_kind='ROOT_CONFIGURATION_ADMISSION',independent_code_review_status='PARTIAL_NOT_PASS'))
            scope=dict(status='IMPLEMENTING_AUTHORIZED',started_utc=a.stamp(),external_s=0,gates={})
            with patch.object(a,'transaction',lambda:fake_transaction(scope)),patch.object(a,'assert_binding'):
                a.admit(binding,ready,review)
            self.assertEqual(scope['gates']['admission'],'PASS')
            self.assertEqual(scope['gates']['independent_code_review'],'PARTIAL_NOT_PASS')
            self.assertEqual(scope['admission']['review_kind'],'ROOT_CONFIGURATION_ADMISSION')


class ReferenceRuntimeConstruction(unittest.TestCase):
    def test_actual_runtime_constructor_copies_v10_readonly_config_and_does_not_send(self):
        from week12.src.car_domain.runtime_adapter import CarRuntime
        from week12.src.car_domain.reference_execution import EXECUTION
        from week11.src.runtime import _load_config
        config=a.PROJECT/'week12/configs/car_domain_runtime.json'
        original=dict(_load_config(config))
        with tempfile.TemporaryDirectory(prefix='runtime-constructor-fixture-',dir=a.DEFAULT_OUT) as tmp:
            with patch.object(CarRuntime,'_send',side_effect=AssertionError('no API in software test')) as send:
                runtime=CarRuntime(ledger_path=Path(tmp)/'runtime.sqlite')
                self.assertEqual(runtime.config['max_requests'],EXECUTION['caps']['product_requests'])
                self.assertEqual(runtime.config['max_total_tokens'],EXECUTION['caps']['product_tokens'])
                self.assertEqual(runtime.config['max_usd'],EXECUTION['caps']['product_usd'])
                self.assertTrue(runtime.ledger_path.is_file())
                send.assert_not_called()
        self.assertEqual(dict(_load_config(config)),original)


class SafeHTTPDiagnosticFlow(unittest.TestCase):
    def run_transport_fixture(self, error=None):
        import httpx,json,sqlite3
        from contextlib import closing
        from types import SimpleNamespace
        from week12.src.car_domain import runtime_adapter as runtime_module
        from week12.scripts import s3_c_car_increment_acceptance as runner
        from week11.src.runtime import DeepSeekRuntime
        secret='DO_NOT_LOG_TEST_SECRET'
        with tempfile.TemporaryDirectory(prefix='safe-http-fixture-',dir=a.DEFAULT_OUT) as tmp:
            out=Path(tmp);binding=out/'fixture-binding.json';a.save(binding,{})
            scope=dict(admission=dict(binding_path=str(binding)),product_requests=[dict(token='fixture-request',status='RESERVED')])
            class Accountant:
                def reserve_product_request(self,*args,**kwargs):return 'fixture-request'
                def finish_product_request(self,*args,**kwargs):self.finish=kwargs
            accountant=Accountant()
            valid={'model':'deepseek-flash','choices':[{'finish_reason':'stop','message':{'content':'fixture only','tool_calls':[]}}],
                   'usage':{'prompt_tokens':1,'completion_tokens':1,'total_tokens':2}}
            def local_child(command,cwd,cap):
                args=SimpleNamespace(source=Path(command[command.index('--source')+1]),
                    report=Path(command[command.index('--report')+1]),
                    request_token=command[command.index('--request-token')+1])
                code=runner.http_child(args)
                return dict(returncode=code,timed_out=False,uncertain=False,interrupted=False,
                    duration_s=.01,pid=123,stdout='',stderr=secret)
            with patch.object(runtime_module,'RESULT_ROOT',out),patch.object(runner,'DEFAULT_OUT',out),                 patch.object(runtime_module,'EXECUTION',{**runner.EXECUTION,'caps':{**runner.EXECUTION['caps'],'product_requests':1,'product_tokens':20096,'product_usd':.01}}),patch.object(runtime_module,'_api_key',return_value=secret),                 patch.object(a,'transaction',lambda:fake_transaction(scope)),patch.object(a,'assert_scope'),                 patch.object(a,'assert_binding'),patch.object(a,'bounded_run',side_effect=local_child),                 patch.object(DeepSeekRuntime,'_send',side_effect=error,return_value=valid) as transport:
                runtime=runtime_module.CarRuntime(ledger_path=out/'runtime.sqlite',accountant=accountant)
                if error is None:
                    result=runtime.complete([{'role':'user','content':'software fixture'}],[],'fixture-id')
                    self.assertEqual(result['content'],'fixture only')
                else:
                    with self.assertRaises(runtime_module.RuntimeError):
                        runtime.complete([{'role':'user','content':'software fixture'}],[],'fixture-id')
                    with self.assertRaises(runtime_module.RuntimeError):
                        runtime.complete([{'role':'user','content':'software fixture'}],[],'fixture-id')
                self.assertEqual(transport.call_count,1)
            job=out/(runner.EXECUTION['artifact_prefix']+'-http')/'fixture-request'
            process_report=a.read(job/'process.json')
            with closing(sqlite3.connect(out/'runtime.sqlite')) as db:
                status,reason,actual_tokens=db.execute('select status,reason,actual_tokens from requests').fetchone()
            if error is None:
                self.assertEqual(status,'succeeded');self.assertFalse((job/'error.json').exists())
                self.assertNotIn('child_diagnostic',process_report)
            else:
                child=a.read(job/'error.json');saved=json.loads(reason)
                self.assertEqual(status,'failed');self.assertIsNone(actual_tokens)
                self.assertEqual(child['phase'],'transport')
                self.assertEqual(saved['child_diagnostic'],process_report['child_diagnostic'])
                self.assertEqual(accountant.finish['status'],'UNKNOWN')
                text=json.dumps([child,saved,process_report])
                self.assertNotIn(secret,text);self.assertNotIn('Authorization',text)
                self.assertNotIn('example.invalid',text)
                return child['transport_exception_chain'][0]

    def test_http_status_preserved_without_body_headers_or_url_and_no_resend(self):
        import httpx
        secret='DO_NOT_LOG_TEST_SECRET'
        request=httpx.Request('POST','https://example.invalid/'+secret,headers={'Authorization':'Bearer '+secret})
        response=httpx.Response(429,request=request,text=secret)
        error=httpx.HTTPStatusError(secret,request=request,response=response)
        item=self.run_transport_fixture(error)
        self.assertEqual(item,{'type':'HTTPStatusError','http_status':429})

    def test_os_error_code_survives_child_parent_and_existing_runtime_ledger(self):
        item=self.run_transport_fixture(OSError(13,'DO_NOT_LOG_TEST_SECRET'))
        self.assertEqual(item,{'type':'PermissionError','errno':13})

    def test_successful_transport_keeps_original_response_path(self):
        self.run_transport_fixture()


class LocalOnlyExecutionCaps(unittest.TestCase):
    def test_zero_budget_requires_all_product_limits_zero(self):
        from week12.src.car_domain.reference_execution import validate_caps,DEFAULT_CAPS
        caps=dict(DEFAULT_CAPS,product_requests=0,product_tokens=0,product_usd=0)
        validate_caps(caps)
        validate_caps({**caps,'repairs':0})
        with self.assertRaises(ValueError):validate_caps({**caps,'repairs':-1})
        for change in ({"product_tokens":1},{"product_usd":.01},{"product_requests":1},{"scenes":0},{"product_usd":-1}):
            with self.subTest(change=change),self.assertRaises(ValueError):validate_caps({**caps,**change})

    def test_local_runtime_cannot_send_even_if_called_accidentally(self):
        from week12.src.car_domain.runtime_adapter import CarRuntime
        from week12.src.car_domain.reference_execution import EXECUTION
        from week11.src.runtime import RuntimeError as RuntimeBudgetError
        with tempfile.TemporaryDirectory(prefix="zero-product-fixture-",dir=a.DEFAULT_OUT) as tmp:
            runtime=CarRuntime(ledger_path=Path(tmp)/"runtime.sqlite")
            runtime.config.update(max_requests=0,max_total_tokens=0,max_usd=0)
            with patch.object(runtime,"_send") as send:
                with self.assertRaises(RuntimeBudgetError):runtime.complete([{"role":"user","content":"no send"}],[],"fixture")
                send.assert_not_called()


class LocalReusePreparation(unittest.TestCase):
    def test_changed_prior_artifact_rejected_before_database_copy(self):
        from week12.scripts import s3_c_car_increment_acceptance as runner
        with tempfile.TemporaryDirectory(prefix="reuse-binding-fixture-",dir=a.DEFAULT_OUT) as tmp:
            out=Path(tmp);original=out/"prior.sqlite";original.write_bytes(b"fixture only")
            a.save(out/"proposal.json",{"local_reuse":{"database":{"path":"prior.sqlite","sha256":"0"*64}}})
            execution={"caps":dict(product_requests=0,product_tokens=0,product_usd=0),"proposal_file":"proposal.json","artifact_prefix":"fixture"}
            with patch.object(runner,"DEFAULT_OUT",out),patch.object(runner,"EXECUTION",execution):
                with self.assertRaisesRegex(a.Stop,"HASH_DIFFERS"):runner.prepare_local_reuse()
            self.assertFalse((out/"fixture-agent-store.sqlite").exists())

    def test_local_case_uses_offline_cli_new_proposal_and_existing_completion_checks(self):
        import asyncio
        from unittest.mock import AsyncMock
        from types import SimpleNamespace
        from week11.src.store import digest_json
        from week12.scripts import s3_c_car_increment_acceptance as runner
        from week12.src.car_domain import cli_adapter
        spec={"fixture_only":True};reuse={"project_id":"fixture","session_id":"old-session"}
        cli=SimpleNamespace(_call=AsyncMock(return_value={"status":{"project":{"project_id":"fixture"}}}),
            _show_call=AsyncMock(return_value={"ok":True,"proposal":{"proposal_id":"new-proposal","spec_digest":digest_json(spec)}}))
        async def local_main(args,case):
            self.assertTrue(args.debug_offline);self.assertEqual(args.session,"old-session")
            return await case(cli)
        completion=AsyncMock(return_value={"status":"REFERENCE_CASE_COMPLETED"})
        with tempfile.TemporaryDirectory(prefix="local-reuse-fixture-",dir=a.DEFAULT_OUT) as tmp:
            out=Path(tmp)
            with patch.object(runner,"DEFAULT_OUT",out),patch.object(runner,"prepare_local_reuse",return_value=(out/"copy.sqlite",reuse,spec,{"new_model_calls":0})),patch.object(cli_adapter,"async_main",side_effect=local_main),patch.object(runner,"execute_prepared",completion):
                self.assertEqual(asyncio.run(runner.local_resume(None)),0)
            completion.assert_awaited_once()
            self.assertEqual(completion.call_args.args[1],"new-proposal")


class ActualInitializedParameterReadback(unittest.TestCase):
    def make_log(self,lines):
        return "LOG_INIT_V | info | parameter values\n"+"\n".join(lines)+"\nLOG_SUCCESS | info | The initialization finished successfully without homotopy method.\n"

    def test_actual_value_is_used_instead_of_start_or_expected_value(self):
        log=self.make_log(["| | [1] parameter Real m(start=999, fixed=true) = 85",
            "| | [2] parameter Real H_peak(start=0, fixed=true) = 3",
            "| | [3] parameter Integer n_steps(start=0, fixed=true) = 20"])
        self.assertEqual(a.read_initialized_parameters(log,{"m":85,"H_peak":3,"n_steps":20}),{"m":85.0,"H_peak":3.0,"n_steps":20.0})

    def test_missing_wrong_nonfinite_duplicate_and_missing_success_rejected(self):
        good="| | [1] parameter Real m(start=85, fixed=true) = 85"
        for log in (self.make_log([]),self.make_log([good.replace("= 85","= 84")]),
                    self.make_log([good.replace("= 85","= nan")]),self.make_log([good,good]),
                    "LOG_INIT_V | info | parameter values\n"+good):
            with self.subTest(log=log),self.assertRaises(a.Stop):a.read_initialized_parameters(log,{"m":85})

    def test_all_frozen_parameters_read_from_independent_runtime_log_fixture(self):
        from week12.src.car_domain.reference_profile import parameter_values
        expected=a.derived_values(parameter_values())
        lines=[f"| | [{i}] parameter {'Integer' if name=='n_steps' else 'Real'} {name}(start=0, fixed=true) = {value:.15g}"
               for i,(name,value) in enumerate(expected.items(),1)]
        self.assertEqual(len(a.read_initialized_parameters(self.make_log(lines),expected)),25)
