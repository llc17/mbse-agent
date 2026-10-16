"""TEST_ONLY payload/accounting checks; no HTTP or private credentials."""
import json,sqlite3,unittest,uuid
from pathlib import Path
from unittest.mock import patch
from week12.src.car_domain.artifact_runtime import ArtifactRuntime,RuntimeError
from week12.src.car_domain.artifact_session import prepare_run

ROOT=Path(__file__).resolve().parents[1]/'results/s5-fivefloor-proposal-20261012-01/test-only'

class ThinkingRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.root=ROOT/uuid.uuid4().hex;self.root.mkdir(parents=True)
        self.config=dict(schema_version='v10-runtime-1',model='deepseek-flash',api_base='https://api.deepseek.com',thinking='enabled',temperature=0,max_tokens=16384,timeout_s=120,max_requests=2,max_total_tokens=2000000,max_usd=.10,max_input_tokens=64000,input_usd_per_million=.3,output_usd_per_million=1.2,max_consecutive_transport_errors=1,automatic_retries=0,budget_profile='thinking-task-20261012')
        self.calls=[]
        self.response=dict(id='TEST_ONLY-response',model='DeepSeek-V4.1-Flash',choices=[dict(finish_reason='stop',message=dict(content='TEST_ONLY unverified proposal',reasoning_content='PRIVATE TEST_ONLY REASONING'))],usage=dict(prompt_tokens=4,completion_tokens=8,total_tokens=12,completion_tokens_details=dict(reasoning_tokens=5)))
        self.key=patch('week12.src.car_domain.artifact_runtime._api_key',lambda:'TEST_ONLY');self.key.start();self.addCleanup(self.key.stop)
    def runtime(self):
        path=self.root/'config.json';path.write_text(json.dumps(self.config),encoding='utf8')
        def send(payload,key,timeout):
            self.calls.append(payload);self.assertEqual(key,'TEST_ONLY');self.assertEqual(timeout,self.config['timeout_s']);return self.response
        return ArtifactRuntime(path,self.root/'ledger.sqlite',transport=send)
    def complete(self,runtime,rid='TEST_ONLY-request'):return runtime.complete([dict(role='user',content='test')],[],rid)
    def test_enabled_payload_usage_and_private_reasoning(self):
        r=self.runtime();v=self.complete(r)
        self.assertEqual(self.calls[0]['thinking'],dict(type='enabled'));self.assertNotIn('temperature',self.calls[0]);self.assertNotIn('tool_choice',self.calls[0]);self.assertNotIn('tools',self.calls[0])
        self.assertEqual(v['usage']['total_tokens'],12);self.assertEqual(v['usage']['reasoning_tokens'],5);self.assertEqual(v['response_id'],'TEST_ONLY-response')
        with sqlite3.connect(r.ledger_path) as db:self.assertEqual(db.execute('SELECT actual_tokens FROM requests').fetchone()[0],12)
        self.assertNotIn('PRIVATE TEST_ONLY REASONING',''.join(p.read_text(encoding='utf8') for p in (self.root/'provider-responses').glob('*.json')))
        self.assertEqual(self.complete(r),v);self.assertEqual(len(self.calls),1)
    def test_disabled_legacy_compatible(self):
        self.config.update(thinking='disabled',max_tokens=2048,max_total_tokens=16000,max_usd=.02,max_input_tokens=13952);self.config.pop('budget_profile')
        self.complete(self.runtime());self.assertEqual(self.calls[0]['thinking'],dict(type='disabled'));self.assertEqual(self.calls[0]['temperature'],0)
    def test_independent_quota_validation(self):
        for field,value in [('max_requests',3),('max_total_tokens',2000001),('max_usd',.1001),('max_tokens',32769),('max_input_tokens',64001),('timeout_s',121),('automatic_retries',1)]:
            old=self.config[field];self.config[field]=value
            with self.assertRaises(RuntimeError):self.runtime()
            self.config[field]=old
        self.assertEqual(self.calls,[])
    def test_two_request_cap(self):
        r=self.runtime();self.complete(r,'a');self.complete(r,'b')
        with self.assertRaisesRegex(RuntimeError,'budget exhausted'):self.complete(r,'c')
        self.assertEqual(len(self.calls),2)
    def test_length_records_usage_no_resend(self):
        self.response['choices'][0]['finish_reason']='length';r=self.runtime()
        with self.assertRaisesRegex(RuntimeError,'choices'):self.complete(r)
        with sqlite3.connect(r.ledger_path) as db:
            self.assertEqual(db.execute('SELECT actual_tokens,status FROM requests').fetchone(),(12,'unknown'));self.assertIsNotNone(db.execute("SELECT value FROM meta WHERE key='frozen'").fetchone())
        with self.assertRaisesRegex(RuntimeError,'resend'):self.complete(r)
        self.assertEqual(len(self.calls),1)
    def test_reasoning_cannot_exceed_completion(self):
        self.response['usage']['completion_tokens_details']['reasoning_tokens']=9
        with self.assertRaisesRegex(RuntimeError,'reasoning'):self.complete(self.runtime())
    def test_output_limit_includes_reasoning(self):
        self.config['max_tokens']=7
        with self.assertRaisesRegex(RuntimeError,'exceeded reservation'):self.complete(self.runtime())
    def test_new_run_profile_does_not_inflate_existing(self):
        with patch('week12.src.car_domain.artifact_session.run_path',lambda name:self.root/name):
            old=prepare_run('old');self.assertEqual(old['caps']['max_total_tokens'],16000)
            with self.assertRaisesRegex(ValueError,'改变额度'):prepare_run('old',runtime_profile='thinking-task-20261012')
            new=prepare_run('new',runtime_profile='thinking-task-20261012');self.assertEqual(new['caps']['max_total_tokens'],2000000)
            self.assertEqual(prepare_run('new'),new)

class KnownLengthRecoveryTests(unittest.TestCase):
    setUp=ThinkingRuntimeTests.setUp
    complete=ThinkingRuntimeTests.complete
    def fixture(self,*,transport_error=False):
        from week12.src.car_domain.artifact_session import digest
        config=self.root/'runtime.json';config.write_text(json.dumps(self.config),encoding='utf8')
        context=self.root/'context.json';context.write_text('{}',encoding='utf8')
        self.response['choices'][0]['finish_reason']='length'
        self.response['usage']=dict(prompt_tokens=4,completion_tokens=16384,total_tokens=16388,completion_tokens_details=dict(reasoning_tokens=15370))
        def send(payload,key,timeout):
            self.calls.append(payload)
            if transport_error:raise ConnectionError('TEST_ONLY unknown transport')
            return self.response
        r=ArtifactRuntime(config,self.root/'runtime.sqlite',transport=send)
        manifest=dict(status='ACTIVE',mode='LIVE',runtime_profile='thinking-task-20261012',caps=dict(max_requests=2,max_output_tokens=16384),runtime_config_sha256=digest(config),context_sha256=digest(context))
        path=self.root/'run.json';path.write_text(json.dumps(manifest),encoding='utf8')
        with sqlite3.connect(self.root/'projects.sqlite') as db:
            db.execute('CREATE TABLE proposals (id TEXT)');db.execute('CREATE TABLE tasks (id TEXT)')
        with self.assertRaises(RuntimeError):self.complete(r)
        return r,path
    def test_known_length_same_ledger_recovery_and_only_second_request(self):
        r,path=self.fixture();record=r.recover_known_length(path)
        self.assertEqual(record['maximum_remaining_requests'],1);self.assertEqual(r.config['max_tokens'],32768)
        with sqlite3.connect(r.ledger_path) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM requests').fetchone()[0],1);self.assertEqual(db.execute('SELECT status,actual_tokens FROM requests').fetchone(),('unknown',16388));self.assertIsNone(db.execute("SELECT value FROM meta WHERE key='frozen'").fetchone());self.assertIsNotNone(db.execute("SELECT value FROM meta WHERE key='known_length_recovery'").fetchone())
        self.response['choices'][0]['finish_reason']='stop';self.response['usage']=dict(prompt_tokens=4,completion_tokens=8,total_tokens=12)
        self.complete(r,'second')
        with self.assertRaisesRegex(RuntimeError,'budget exhausted'):self.complete(r,'third')
        self.assertEqual(len(self.calls),2)
    def test_unknown_transport_not_recoverable(self):
        r,path=self.fixture(transport_error=True)
        with self.assertRaisesRegex(RuntimeError,'not initial known'):r.recover_known_length(path)
        self.assertFalse((self.root/'known-length-recovery.json').exists());self.assertEqual(len(self.calls),1)
    def test_usage_mismatch_not_recoverable(self):
        r,path=self.fixture();from week11.src.runtime import _digest
        p=self.root/'provider-responses'/(_digest('TEST_ONLY-request')+'.json');v=json.loads(p.read_text(encoding='utf8'));v['response']['usage']['prompt_tokens']+=1;p.write_text(json.dumps(v),encoding='utf8')
        with self.assertRaisesRegex(RuntimeError,'usage mismatch'):r.recover_known_length(path)
        self.assertFalse((self.root/'known-length-recovery.json').exists());self.assertEqual(len(self.calls),1)

if __name__=='__main__':unittest.main()
