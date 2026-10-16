"""Three targeted capacity checks; no HTTP/MCP/Worker/OMC or old-suite rerun."""
import asyncio,hashlib,io,json,unittest
from contextlib import redirect_stdout
from copy import deepcopy
from unittest.mock import patch
from week12.tests import test_artifact_transport_recovery as fixture_module
from week12.src.car_domain.artifact_runtime import ArtifactRuntime,RuntimeError,_estimate_input
from week12.src.car_domain.artifact_continuous import capacity_budget_matches
from week12.src.car_domain.artifact_session import digest,now
from week12.src.car_domain.model_artifact import save

LIMITS=dict(max_requests=12,max_total_tokens=10000000,max_usd=2.,max_tokens=65536,max_input_tokens=128000)

class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.case=fixture_module.RecoveryTests();self.case.setUp();self.addCleanup(self.case.doCleanups)
        self.root=self.case.root;recovery=self.case.recover();runtime=self.case.runtime()
        runtime._reserve(recovery['new_request_id'],recovery['request_content_hash'],43611,.0425745)
        runtime._succeed(recovery['new_request_id'],dict(content='TEST_ONLY valid design'),12,.00001)
        cli=self.case.cli(runtime);cli.context.allow_execution=True;source=cli._flow_sources()[0]
        comm=dict(requirements=[dict(text='TEST_ONLY requirement',source_id=source['id'],source_text=source['text'])],design=dict(summary='TEST_ONLY declaration fixture',assumptions=[],parameters=[dict(name='testvalue',value=1,unit='1',origin='model',basis='TEST_ONLY')]),libraries=[],settings=dict(stop_time=1,intervals=20),outputs=dict(value='testvalue'),checks=[],literal_bindings=[dict(name='testvalue',value=1,unit='1',origin='model',source_id=None,source_text=None)])
        with redirect_stdout(io.StringIO()):record=cli._save_flow_design(recovery['new_request_id'],dict(project=dict(project_id='TEST_ONLY_project')),dict(content=json.dumps(dict(schema='v11-continuous-design-1',state='READY',body='TEST_ONLY ready design',commitment=comm))))
        runtime.finish_transport_recovery_design(recovery['new_request_id'],record['design_id'])
        approval=dict(schema='v11-continuous-design-approval-1',design_id=record['design_id'],displayed_digest=record['displayed_digest'],session_id='daily',project_id=record['project_id'],source_snapshot=record['sources'],budget=record['budget'],channel='trusted_local_design_delegation',approval_text='确认',source_reviewed_by_human=False,scope='generate and execute only a candidate matching the confirmed finite commitment')
        self.design=record;self.rid='flow_generate_'+record['displayed_digest']
        self.request=dict(messages=[dict(role='system',content='TEST_ONLY exact saved generation context '+('x'*15000))],tools=[])
        request_path=self.root/'cli-turns'/(hashlib.sha256(self.rid.encode()).hexdigest()+'-request.json');request_path.parent.mkdir();save(request_path,self.request)
        approval_path=self.root/'cli-continuations'/(record['design_id']+'-approval.json');save(approval_path,approval)
        self.unchanged={p:p.read_bytes() for p in (self.root/'budget-adjustment-authorization.json',self.root/'transport-recovery-authorization.json',self.root/'cli-continuations'/(record['design_id']+'.json'),approval_path,request_path)}
        self.rows=self.case.rows();self.runtime=runtime

    def adjust(self):return self.runtime.authorize_capacity_adjustment(self.root/'run.json',approval_messages=['TEST_ONLY 用户扩大容量5M','TEST_ONLY 最新累计10M覆盖5M'],limits=dict(LIMITS),idle_process_evidence=dict(checked_at=now(),matching_processes=[]))
    def test_same_ledger_caps_and_precise_design_budget_mapping(self):
        audit=self.adjust();runtime=self.case.runtime();cli=self.case.cli(runtime)
        self.assertEqual(self.case.rows(),self.rows)
        for p,raw in self.unchanged.items():self.assertEqual(p.read_bytes(),raw)
        for key,value in LIMITS.items():self.assertEqual(runtime.config[key],value)
        cli._flow_bind_current(self.design)
        self.assertTrue(capacity_budget_matches(self.root,self.design,cli._flow_budget()))
        self.assertEqual(audit['generation_request_id'],self.rid);self.assertEqual(runtime.config['read_timeout_s'],600)
    def test_unapproved_old_profile_and_budget_drift_still_refused(self):
        self.adjust();runtime=self.case.runtime();current=self.case.cli(runtime)._flow_budget();changed=deepcopy(current);changed['worker_timeout_s']=301
        self.assertFalse(capacity_budget_matches(self.root,self.design,changed))
        config=dict(runtime.config);config.pop('capacity_adjustment_authorization');save(self.root/'runtime.json',config)
        with self.assertRaisesRegex(RuntimeError,'selected finite batch'):self.case.runtime()
        config=dict(runtime.config);config['max_total_tokens']=10000001;save(self.root/'runtime.json',config)
        with self.assertRaisesRegex(RuntimeError,'capacity adjustment configuration chain'):self.case.runtime()
    def test_repeat_confirmation_reuses_exact_saved_generation_no_send(self):
        self.adjust();runtime=self.case.runtime();cli=self.case.cli(runtime);cli.context.allow_execution=True;calls=[]
        cli._receipt=lambda rid,kind:self.request if rid==self.rid and kind=='request' else None
        async def preflight(messages,tools,rid,**kwargs):
            calls.append(dict(messages=messages,tools=tools,rid=rid))
            self.assertGreater(_estimate_input(messages,tools),13952);self.assertLessEqual(_estimate_input(messages,tools),runtime.config['max_input_tokens'])
            raise ValueError('TEST_ONLY preflight only; no send')
        cli.submit_prepared_request=preflight
        with self.assertRaisesRegex(ValueError,'preflight only'):asyncio.run(cli.handle_line('确认'))
        self.assertEqual(calls,[dict(messages=self.request['messages'],tools=self.request['tools'],rid=self.rid)])
        self.assertEqual(self.case.rows(),self.rows)
        for p,raw in self.unchanged.items():self.assertEqual(p.read_bytes(),raw)

if __name__=='__main__':unittest.main()
