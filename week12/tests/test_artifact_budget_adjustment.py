"""Four TEST_ONLY checks of the new approval/reservation/deadline boundary."""
import gc,io,json,sqlite3,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from week12.src.car_domain.artifact_runtime import ArtifactRuntime,RuntimeError
from week12.src.car_domain.artifact_session import digest,ledger_state
from week12.src.car_domain.artifact_deadline import run_bounded

OUT=Path(__file__).resolve().parents[1]/'results/s5-fivefloor-proposal-20261012-01/design-stage-test-only'
LIMITS=dict(max_requests=30,max_total_tokens=2000000,max_usd=3.,max_tokens=131072,max_input_tokens=64000,whole_request_deadline_s=900)

class BudgetAdjustmentChecks(unittest.TestCase):
    def setUp(self):
        OUT.mkdir(exist_ok=True);self.tmp=tempfile.TemporaryDirectory(dir=OUT);self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        self.config=dict(schema_version='v10-runtime-1',model='deepseek-flash',api_base='https://api.deepseek.com',thinking='enabled',temperature=0,max_tokens=32768,timeout_s=120,max_requests=2,max_total_tokens=2000000,max_usd=.10,max_input_tokens=64000,input_usd_per_million=.3,output_usd_per_million=1.2,max_consecutive_transport_errors=1,automatic_retries=0,budget_profile='thinking-task-20261012')
        (self.root/'runtime.json').write_text(json.dumps(self.config),encoding='utf8');(self.root/'context.json').write_text('{}',encoding='utf8')
        self.path=self.root/'run.json';self.path.write_text(json.dumps(dict(status='ACTIVE',mode='LIVE',runtime_profile='thinking-task-20261012',caps={},runtime_config_sha256=digest(self.root/'runtime.json'),context_sha256=digest(self.root/'context.json'))),encoding='utf8')
        with sqlite3.connect(self.root/'projects.sqlite') as db:
            db.execute('CREATE TABLE proposals(id TEXT)');db.execute('CREATE TABLE tasks(id TEXT)')
        self.sent=[];self.response=dict(id='TEST_ONLY_known_length',model='deepseek-flash',choices=[dict(finish_reason='length',message=dict(content=''))],usage=dict(prompt_tokens=4,completion_tokens=32768,total_tokens=32772))
        self.runtime=ArtifactRuntime(self.root/'runtime.json',self.root/'runtime.sqlite',transport=lambda payload,key,timeout:self.sent.append(payload) or self.response)
        with patch('week12.src.car_domain.artifact_runtime._api_key',return_value='TEST_ONLY'),self.assertRaises(RuntimeError):self.runtime.complete([dict(role='user',content='TEST_ONLY')],[],'TEST_ONLY_length')
        self.addCleanup(gc.collect)
    def approve(self):return self.runtime.authorize_budget_adjustment(self.path,approval_verbatim='批准',limits=LIMITS)
    def test_approval_preserves_old_rows_and_independent_reservations(self):
        with sqlite3.connect(self.runtime.ledger_path) as db:old=db.execute('SELECT * FROM requests').fetchall()
        record=self.approve();self.assertEqual(record['original_totals']['tokens'],32772);self.assertFalse(ledger_state(self.runtime.ledger_path)['frozen'])
        r=ArtifactRuntime(self.root/'runtime.json',self.root/'runtime.sqlite')
        with sqlite3.connect(r.ledger_path) as db:self.assertEqual(db.execute('SELECT * FROM requests').fetchall(),old)
        for tokens,cost in [(2000000,.1),(5,3.01)]:
            with self.assertRaisesRegex(RuntimeError,'budget exhausted'):r._reserve('TEST_ONLY_over','hash',tokens,cost)
        r._reserve('TEST_ONLY_allowed','hash',131100,.1573)
        with self.assertRaisesRegex(RuntimeError,'already recorded'):self.approve()
    def test_unknown_transport_and_usage_mismatch_remain_frozen(self):
        with sqlite3.connect(self.runtime.ledger_path) as db:db.execute("UPDATE requests SET status='failed',reason='transport' ");db.execute("UPDATE meta SET value='transport_errors' WHERE key='frozen'")
        with self.assertRaisesRegex(RuntimeError,'known length'):self.approve()
        with sqlite3.connect(self.runtime.ledger_path) as db:db.execute("UPDATE requests SET status='unknown',reason='invalid response or budget overrun',actual_tokens=1");db.execute("UPDATE meta SET value='invalid response or budget overrun' WHERE key='frozen'")
        with self.assertRaisesRegex(RuntimeError,'usage/cost mismatch'):self.approve()
        self.assertTrue(ledger_state(self.runtime.ledger_path)['frozen']);self.assertFalse((self.root/'budget-adjustment-authorization.json').exists())
    def test_unapproved_inflation_rejected_and_no_tool_payload(self):
        self.config.update(max_requests=30,max_tokens=131072,max_usd=3.)
        (self.root/'unapproved.json').write_text(json.dumps(self.config),encoding='utf8')
        with self.assertRaisesRegex(RuntimeError,'exceeds'):ArtifactRuntime(self.root/'unapproved.json',self.root/'new.sqlite')
        self.assertNotIn('tools',self.sent[0]);self.assertNotIn('tool_choice',self.sent[0]);self.assertEqual(self.sent[0]['thinking'],dict(type='enabled'))
    def test_external_process_deadline_terminates_child(self):
        with (self.root/'deadline.txt').open('w') as console:result=run_bounded([sys.executable,'-c','import time; time.sleep(30)'],timeout_s=.2,cwd=self.root,stdout=console)
        self.assertTrue(result['timed_out']);self.assertLess(result['elapsed_s'],10);self.assertNotEqual(result['exit_code'],0)

if __name__=='__main__':unittest.main()
