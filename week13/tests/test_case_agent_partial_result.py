"""Actual isolated Store, injected transport/RPC; no live product or OMC."""
import asyncio,hashlib,json,sqlite3,tempfile,unittest
from contextlib import closing
from pathlib import Path
import httpx
from week13.tests.test_case_agent_requests import Harness
from week13.src import case_paths,case_requests
from week13.src.case_budget import CaseBudget
from week13.src.case_taskflow import TaskFlow

class PartialResult(unittest.TestCase):
    def test_pass_then_read_timeout_retains_result_and_repeated_run_has_no_calls(self):
        saved=(case_requests.ROOT,case_requests.REQUESTS_ROOT,case_requests.LEGACY_PROJECT_ROOT)
        try:
            with tempfile.TemporaryDirectory(dir=case_paths.ROOT/'results') as temp:
                h=Harness(Path(temp)/'fixture');rid='fixture_partial'
                h.bootstrap(rid,h.project_id,'v000002','把总质量改为92kg，增加部分在原重心，几何控制不变。');h.authorize(rid)
                flow=h.flow(rid);planned=asyncio.run(flow.plan());flow.confirm(planned['displayed_digest'],'CONFIRM');h.outcome_status='PASS'
                original=h.transport;calls=[]
                def timeout_only_narration(ctx,payload):
                    calls.append(bool(payload.get('tools')))
                    if not payload.get('tools'):raise httpx.ReadTimeout('TEST_ONLY narrative stream')
                    return original(ctx,payload)
                h.transport=timeout_only_narration
                partial=asyncio.run(flow.run())
                self.assertEqual(partial['execution']['task_status'],'PASS');self.assertEqual(partial['explanation']['status'],'UNKNOWN');self.assertFalse(partial['product_explanation_success'])
                self.assertEqual(partial['state']['last_task_pass']['version_id'],'v000003');self.assertEqual(partial['state']['http_state']['unknown_requests'][0]['category'],'explanation')
                budget=CaseBudget(task_context=case_requests.load_request(rid));before=budget.usage();self.assertEqual(before['requests'],3);self.assertEqual(before['unknown_requests'],1)
                def binding():
                    with closing(sqlite3.connect(h.db)) as db:counts=[db.execute('SELECT count(*) FROM '+name).fetchone()[0] for name in ('sources','proposals','tasks')]
                    hashes={str(p):hashlib.sha256(p.read_bytes()).hexdigest() for p in h.projectroot.glob('projects/*/versions/*/frozen-model.*')}
                    return counts,hashes
                frozen=binding();count=len(calls)
                h.transport=lambda *a:self.fail('readback cannot call a model')
                async def no_rpc(*args):self.fail('readback cannot execute or call MCP')
                again=h.flow(rid);again.rpc=no_rpc;result=asyncio.run(again.run())
                self.assertTrue(result['reused']);self.assertEqual(result['partial']['execution']['task_status'],'PASS');self.assertEqual(result['product_http_added'],0);self.assertEqual(result['execution_added'],0)
                self.assertEqual(len(calls),count);self.assertEqual(binding(),frozen);self.assertEqual(budget.usage(),before)
                complete=TaskFlow.__new__(TaskFlow);complete.root=Path(temp)/'already-complete';complete.root.mkdir();complete.state=lambda:dict(marker='complete fixture readonly binding')
                saved_complete=dict(stage='RESULT_READ_BACK',fixture_only=True)
                (complete.root/'pipeline.json').write_text(json.dumps(saved_complete),encoding='utf8')
                complete.call=lambda *a:self.fail('complete result cannot call a model')
                result=complete.terminal_readback();self.assertEqual(result['pipeline'],saved_complete);self.assertEqual(result['product_http_added'],0);self.assertFalse((complete.root/'pipeline-partial.json').exists())
        finally:case_requests.ROOT,case_requests.REQUESTS_ROOT,case_requests.LEGACY_PROJECT_ROOT=saved

if __name__=='__main__':unittest.main()
