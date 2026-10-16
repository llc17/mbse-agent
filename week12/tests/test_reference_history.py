"""Historical query regression: real archived evidence, no model/API execution."""
import asyncio
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

from week12.src.car_domain.contracts import RESULT_ROOT, WEEK12_ROOT
from week12.src.car_domain.service_adapter import CarService, _verify_manifest
from week12.src.car_domain.reference_history import verify_historical_reference

DB = RESULT_ROOT/'reference-parameters-run-agent-store.sqlite'
TASK = 'task_d890b6dd517f446b8c7b80114004cbe7'
SESSION = 'reference_command_route'
OUT = WEEK12_ROOT/'results/s3-c-history-query-20261010-01'

def digest(p):return hashlib.sha256(Path(p).read_bytes()).hexdigest()


class HistoricalEvidence(unittest.TestCase):
    def setUp(self):
        self.service = CarService(DB,SESSION,read_only=True)
        self.task = self.service.store.task(TASK)
        self.proposal = self.service.store.proposal(self.task['proposal_id'])
        self.result = self.task['result']
        self.manifest = json.loads(Path(self.result['outcome_manifest_path']).read_bytes())

    def test_closed_expired_account_queries_without_live_admission_or_writes(self):
        paths=[DB,RESULT_ROOT/'ledger.json']
        before=[digest(p) for p in paths]
        with patch('week12.src.car_domain.reference_route.assert_scope',side_effect=AssertionError('must not admit')):
            response=self.service.evidence_query('task:'+TASK)
        self.assertTrue(response['ok'])
        self.assertEqual(response['evidence']['outcome']['reference_chain_status'],'REFERENCE_CHAIN_PASS')
        self.assertEqual(before,[digest(p) for p in paths])

    def test_new_active_execution_identity_does_not_invalidate_history(self):
        with patch('week12.src.car_domain.reference_route.AUTH_SHA','different-new-approval'),patch('week12.src.car_domain.reference_route.CONTRACT_SHA','different-new-contract'):
            self.assertTrue(self.service.evidence_query('task:'+TASK)['ok'])

    def test_real_prior_failed_task_stays_failed_and_queryable(self):
        response=self.service.evidence_query('task:task_31ec162fa9e44059aaf0e662a17a817a')
        self.assertTrue(response['ok'])
        outcome=response['evidence']['outcome']
        self.assertNotEqual(outcome['reference_chain_status'],'REFERENCE_CHAIN_PASS')
        self.assertEqual(outcome['agent_execution_status'],'BLOCKED_REFERENCE')

    def test_readonly_database_rejects_write(self):
        before=digest(DB)
        with self.assertRaises(sqlite3.OperationalError):
            with self.service.store.transaction() as db:
                db.execute("UPDATE tasks SET state='failed' WHERE task_id=?",(TASK,))
        self.assertEqual(before,digest(DB))

    def test_task_identity_proposal_and_outcome_corruption_rejected(self):
        for kind in ('project','session','proposal','confirmation','spec','manifest','outcome','failed_success','running'):
            task=deepcopy(self.task);proposal=deepcopy(self.proposal);manifest=deepcopy(self.manifest);result=deepcopy(self.result)
            if kind=='project':task['project_id']='prj_other'
            if kind=='session':task['session_id']='other_session'
            if kind=='proposal':task['proposal_id']='prop_other'
            if kind=='confirmation':proposal['confirmation']['authorization_sha256']='0'*64
            if kind=='spec':proposal['spec']['parameters']['m']=86
            if kind=='manifest':manifest['binding']['owner']='other_owner'
            if kind=='outcome':result['vehicle_task_status']='PASS'
            if kind=='failed_success':task['state']='failed'
            if kind=='running':task['state']='running'
            with self.subTest(kind=kind),self.assertRaises(ValueError):
                verify_historical_reference(manifest,result,task,proposal)

    def test_bad_historical_exit_stopped_scope_and_exceeded_counts_rejected(self):
        original=json.loads((RESULT_ROOT/'ledger.json').read_bytes())
        for kind in ('exit','owner','stopped','count','completion_time','admission'):
            ledger=deepcopy(original);scope=ledger['reference_parameter_execution'];task=deepcopy(self.task)
            worker=next(p for p in scope['parents'] if p['label']=='car-worker:'+TASK)
            if kind=='exit':worker['actual_exit_code']=1
            if kind=='owner':scope['execution_claim']['owner']='wrong'
            if kind=='stopped':scope['status']='CLOSED_BLOCKED'
            if kind=='count':scope['counts']['scenes']=2
            if kind=='completion_time':task['updated_at']='2099-01-01T00:00:00+00:00'
            if kind=='admission':scope['admission']['binding_sha256']='0'*64
            with tempfile.TemporaryDirectory(dir=OUT) as temp:
                path=Path(temp)/'fixture-ledger.json';path.write_text(json.dumps(ledger),encoding='utf8')
                with self.subTest(kind=kind),self.assertRaises(ValueError):
                    verify_historical_reference(self.manifest,self.result,task,self.proposal,ledger_path=path)

    def test_artifact_mutation_and_path_escape_rejected(self):
        with tempfile.TemporaryDirectory(dir=OUT) as temp:
            base=Path(temp);item=base/'artifact.txt';item.write_text('original',encoding='utf8')
            manifest=base/'manifest.json';payload={'files':{'artifact.txt':{'sha256':digest(item),'bytes':8}}}
            manifest.write_text(json.dumps(payload),encoding='utf8');bound=digest(manifest)
            item.write_text('changed',encoding='utf8')
            with self.assertRaises(ValueError):_verify_manifest(manifest,bound,base)
            payload['files']={'../escaped':{'sha256':'0'*64,'bytes':0}}
            manifest.write_text(json.dumps(payload),encoding='utf8')
            with self.assertRaises(ValueError):_verify_manifest(manifest,digest(manifest),base)

    def test_service_rejects_cross_project_task(self):
        with patch.object(self.service.store,'current_project',return_value={'project_id':'prj_other'}):
            with self.assertRaises(ValueError):self.service.evidence_query('task:'+TASK)


class HistoricalMCP(unittest.IsolatedAsyncioTestCase):
    async def test_real_stdio_readonly_query_cannot_expose_execution(self):
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
        parameters=StdioServerParameters(command=sys.executable,args=['-X','utf8','-B','-m',
            'week12.src.car_domain.mcp_adapter','--read-only','--db',str(DB),'--session',SESSION])
        before={str(p):digest(p) for p in (DB,RESULT_ROOT/'ledger.json')}
        with (OUT/'mcp-stderr.log').open('w',encoding='utf8') as errors:
            async with asyncio.timeout(30):
                async with stdio_client(parameters,errlog=errors) as (read,write):
                    async with ClientSession(read,write) as session:
                        await session.initialize()
                        listed=await session.list_tools()
                        self.assertEqual([t.name for t in listed.tools],['evidence_query'])
                        response=await session.call_tool('evidence_query',{'question':'task:'+TASK})
                        self.assertFalse(response.isError)
                        data=response.structuredContent
                        if data is None:data=json.loads(response.content[0].text)
                        self.assertTrue(data['ok'])
                        self.assertEqual(data['evidence']['outcome']['reference_chain_status'],'REFERENCE_CHAIN_PASS')
                        rejected=await session.call_tool('execution_start',{'proposal_id':self.__class__.__name__,'request_id':'not_authorized'})
                        self.assertTrue(rejected.isError)
        after={p:digest(p) for p in before}
        self.assertEqual(before,after)
        (OUT/'actual-mcp-query.json').write_text(json.dumps(dict(status='PASS',real_stdio_mcp=True,
            task_id=TASK,reference_chain_status=data['evidence']['outcome']['reference_chain_status'],
            vehicle_task_status=data['evidence']['outcome']['vehicle_task_status'],tools=[t.name for t in listed.tools],
            execution_tool_rejected=rejected.isError,before=before,after=after,new_simulation=0,new_model_calls=0),indent=2)+'\n',encoding='utf8')
