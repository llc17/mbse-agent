"""Isolated SQLite guard fixtures; never opens or changes a live ledger."""
from copy import deepcopy
from datetime import datetime,timezone
from pathlib import Path
import json,sqlite3,tempfile,unittest
from unittest.mock import patch
from week13.src.case_paths import ROOT,REPO,sha_value,sha_file,read_json
from week13.src.case_budget import CaseBudget,connection

PACKAGE=ROOT/'results/strict-output-contact-followup-20261022-01'


class RejectionGuard(unittest.TestCase):
    def setUp(self):
        PACKAGE.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(prefix='rejection-guard-local-',dir=PACKAGE);self.root=Path(self.temp.name)
        self.old='case_plan_fixture';self.new=self.old+'_http400_diagnostic_1';self.digest='fixture_payload'
        self.b=object.__new__(CaseBudget);self.b.path=self.root/'fixture.sqlite';self.b.task_context={'request_id':'fixture','request_digest':'fixture_digest'}
        controls=read_json(ROOT/'configs/case_runtime.json')
        self.b.authority=dict(test_only=False,model_controls=controls,caps=dict(http_requests=10,total_tokens=10000000,total_output_tokens=3932160,product_cny=55,software_repair_rounds=3),
            repair_allocation=dict(maximum_before_first_valid_generated_source=1,reserved_for_post_generation_or_runtime=2))
        self.b._require_time=lambda:None;self.b._authorization_sha=lambda:'fixture_authority'
        self.patch=patch('week13.src.case_budget.require_planning',return_value=self.b.task_context);self.patch.start()
        with connection(self.b.path) as db:
            db.execute('CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT NOT NULL)')
            db.execute('CREATE TABLE requests(request_id TEXT PRIMARY KEY,content_hash TEXT NOT NULL,category TEXT NOT NULL,status TEXT NOT NULL,reserve_tokens INTEGER NOT NULL,reserve_output INTEGER NOT NULL,reserve_cost REAL NOT NULL,actual_tokens INTEGER,actual_output INTEGER,actual_cost REAL,response TEXT,reason TEXT,reserved_at TEXT NOT NULL,launched_at TEXT,finished_at TEXT)')
            db.execute('CREATE TABLE counters(kind TEXT,event_id TEXT,detail_json TEXT,recorded_at TEXT,PRIMARY KEY(kind,event_id))')
            db.execute('CREATE TABLE process_attempts(kind TEXT,state TEXT)')
            db.execute('CREATE TABLE connect_retries(old_request_id TEXT,new_request_id TEXT,content_hash TEXT,launched_at TEXT)')
            db.execute("INSERT INTO meta VALUES('frozen','unknown product HTTP; usage reservation retained')")
            now=datetime.now(timezone.utc).isoformat()
            db.execute('INSERT INTO requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',(self.old,self.digest,'design','unknown',1000000,393216,5.24288,None,None,None,None,'RuntimeError',now,now,now))
        self.evidence=self.root/'evidence.json';self.evidence.write_text('{"fixture":true}',encoding='utf8')
        self.original=self.b.request(self.old)
        self.proof=dict(schema='v12-http400-diagnostic-proof-1',authority_sha256='fixture_authority',ledger_path=str(self.b.path.resolve()),
            request_context_id='fixture',diagnostic_request_id=self.new,payload_sha256=self.digest,original_request_row_sha256=sha_value(self.original),
            request_digest='fixture_digest',http_status=400,local_process_terminated=True,no_local_tool_side_effects=True,maximum_diagnostic_requests=1,recursive_diagnostics_forbidden=True,
            original_freeze='unknown product HTTP; usage reservation retained',evidence_hashes={str(self.evidence):sha_file(self.evidence)})
    def tearDown(self):
        self.patch.stop();self.assertTrue(self.root.resolve().is_relative_to(PACKAGE.resolve()));self.temp.cleanup()
    def authorize(self):return self.b.authorize_http_rejection_diagnostic(self.old,self.new,self.digest,self.proof)
    def count(self,query):
        with connection(self.b.path) as db:return db.execute(query).fetchone()[0]
    def test_one_proof_charges_one_repair_and_preserves_old_unknown_fullreserve_freeze(self):
        self.authorize()
        self.assertEqual(self.b.request(self.old),self.original)
        self.assertEqual(self.count("SELECT count(*) FROM counters WHERE kind='software_repair'"),1)
        with connection(self.b.path) as db:
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='frozen'").fetchone()[0],self.proof['original_freeze'])
            self.assertTrue(self.b._http_rejection_diagnostic_allowed(db,self.new))
            with self.assertRaisesRegex(RuntimeError,'frozen'):self.b._blocked(db,'ordinary_other_request')
        self.b.reserve_http(self.new,self.digest,'design',self.b.authority['model_controls'])
        with connection(self.b.path) as db:
            totals=db.execute('SELECT count(*),sum(reserve_tokens),sum(reserve_output),sum(reserve_cost) FROM requests').fetchone()
            self.assertEqual(tuple(totals)[:3],(2,2000000,786432));self.assertAlmostEqual(totals[3],10.48576)
        self.b.launch_http(self.new)
        with self.assertRaises(RuntimeError):self.b.launch_http(self.new)
        self.assertEqual(self.b.request(self.old),self.original)
    def test_different_payload_or_RID_or_authority_never_dispatches(self):
        self.authorize()
        with self.assertRaisesRegex(ValueError,'exact original payload'):self.b.reserve_http(self.new,'different','design',self.b.authority['model_controls'])
        with self.assertRaises(RuntimeError):self.b.reserve_http(self.new+'_other',self.digest,'design',self.b.authority['model_controls'])
        with self.assertRaises((ValueError,PermissionError)):self.b.authorize_http_rejection_diagnostic(self.old,self.new+'_other',self.digest,self.proof)
        self.b._authorization_sha=lambda:'different_authority'
        with connection(self.b.path) as db:self.assertFalse(self.b._http_rejection_diagnostic_allowed(db,self.new))
        self.b._authorization_sha=lambda:'fixture_authority'
        self.b.task_context['request_id']='sibling'
        with connection(self.b.path) as db:self.assertFalse(self.b._http_rejection_diagnostic_allowed(db,self.new))
        self.b.task_context['request_id']='fixture';self.b.task_context['request_digest']='other_digest'
        with connection(self.b.path) as db:self.assertFalse(self.b._http_rejection_diagnostic_allowed(db,self.new))
    def test_other_unknown_or_inflight_and_other_freeze_or_closed_are_rejected(self):
        for case in ('unknown','reserved','dispatched','other_freeze','closed','inflight_process'):
            with self.subTest(case=case):
                with connection(self.b.path) as db:
                    if case in ('unknown','reserved','dispatched'):
                        db.execute('INSERT INTO requests SELECT ?,content_hash,category,?,reserve_tokens,reserve_output,reserve_cost,actual_tokens,actual_output,actual_cost,response,reason,reserved_at,launched_at,finished_at FROM requests WHERE request_id=?',('other',case,self.old))
                    elif case=='other_freeze':db.execute("UPDATE meta SET value='different_reason' WHERE key='frozen'")
                    elif case=='closed':db.execute("INSERT INTO meta VALUES('scope_status','CLOSED')")
                    else:db.execute("INSERT INTO process_attempts VALUES('worker','LAUNCHED')")
                with self.assertRaises(PermissionError):self.authorize()
                with connection(self.b.path) as db:
                    db.execute("DELETE FROM requests WHERE request_id='other'");db.execute("DELETE FROM meta WHERE key='scope_status'");db.execute('DELETE FROM process_attempts')
                    db.execute("UPDATE meta SET value=? WHERE key='frozen'",(self.proof['original_freeze'],))
        self.assertEqual(self.count('SELECT count(*) FROM counters'),0)
    def test_expiry_repair_limit_and_each_quota_final_slots_are_rejected(self):
        original=deepcopy(self.b.authority)
        cases=[('http_requests',3),('total_tokens',1500000),('total_output_tokens',700000),('product_cny',10)]
        for key,value in cases:
            with self.subTest(cap=key):
                self.b.authority=deepcopy(original);self.b.authority['caps'][key]=value
                with self.assertRaisesRegex(PermissionError,'reservation|slots'):self.authorize()
        self.b.authority=original
        with connection(self.b.path) as db:db.execute("INSERT INTO counters VALUES('software_repair','already_used','{}','fixture')")
        with self.assertRaisesRegex(PermissionError,'repair'):self.authorize()
        def expired():raise RuntimeError('hard deadline reached')
        self.b._require_time=expired
        with self.assertRaisesRegex(RuntimeError,'deadline'):self.authorize()
    def test_duplicate_proof_evidence_drift_and_post_diagnostic_unknown_block_recursion(self):
        self.authorize()
        with self.assertRaises(PermissionError):self.authorize()
        self.evidence.write_text('tampered',encoding='utf8')
        with self.assertRaises(RuntimeError):self.b.reserve_http(self.new,self.digest,'design',self.b.authority['model_controls'])
        self.evidence.write_text('{"fixture":true}',encoding='utf8')
        self.b.reserve_http(self.new,self.digest,'design',self.b.authority['model_controls']);self.b.launch_http(self.new)
        self.b.finish_http(self.new,error='HTTPStatusError',uncertain=True)
        with connection(self.b.path) as db:self.assertFalse(self.b._http_rejection_diagnostic_allowed(db,self.new))
        with self.assertRaises(RuntimeError):self.b.reserve_http(self.new+'_recursive',self.digest,'design',self.b.authority['model_controls'])
        self.assertEqual(self.b.request(self.old),self.original)
    def test_diagnostic_success_does_not_unfreeze_or_allow_ordinary_tool_work(self):
        self.authorize();self.b.reserve_http(self.new,self.digest,'design',self.b.authority['model_controls']);self.b.launch_http(self.new)
        self.b.finish_http(self.new,response={'fixture':'COMPLETE_NOT_DISPATCHED'},tokens=10,output=5,cost=.001)
        self.assertEqual(self.b.request(self.old),self.original)
        with connection(self.b.path) as db:
            self.assertEqual(db.execute("SELECT value FROM meta WHERE key='frozen'").fetchone()[0],self.proof['original_freeze'])
            with self.assertRaises(RuntimeError):self.b._blocked(db,'case_generate_fixture')
    def test_proof_builder_reads_actual_400_evidence_without_mutation_and_rejects_killed_exit(self):
        from week13.src.case_requests import load_request,authority_for
        from week13.src.case_runtime import CaseRuntime
        from week13.src.case_http_rejection import prepare_rejection_proof
        ctx=load_request('strict85_180_260_20261022_01');authority=authority_for(ctx)
        original='case_plan_'+ctx['request_id'];diagnostic=original+'_http400_diagnostic_1'
        with sqlite3.connect(Path(authority['ledger_path']).resolve().as_uri()+'?mode=ro',uri=True) as db:
            db.row_factory=sqlite3.Row;old=dict(db.execute('SELECT * FROM requests WHERE request_id=?',(original,)).fetchone())
        independent=PACKAGE/'known-http-rejection-recovery-review.json'
        primary=self.root/'TEST_ONLY_NOT_AN_ACTUAL_PRIMARY_AUTHORIZATION.json'
        sources={'week13/src/'+name:sha_file(REPO/'week13/src'/name) for name in ('case_http.py','case_stream.py','case_runtime.py','case_budget.py','case_taskflow.py','case_http_rejection.py')}
        value=dict(schema='v12-http400-diagnostic-primary-review-1',decision='APPROVE_ONE_SAME_PAYLOAD_HTTP400_DIAGNOSTIC',channel='TRUSTED_LOCAL_DEVELOPMENT_REVIEW',
            fixture_only=True,request_id=ctx['request_id'],original_http_request_id=original,diagnostic_request_id=diagnostic,
            authority_sha256=ctx['runtime_authority']['sha256'],execution_approval_sha256=authority['execution_package']['sha256'],
            independent_review_sha256=sha_file(independent),same_payload_only=True,maximum_diagnostic_HTTP=1,implementation_source_hashes=sources)
        primary.write_text(json.dumps(value),encoding='utf8')
        http=Path(ctx['root'])/'http'/sha_value(original);saved=read_json(http/'request.json')
        rt=object.__new__(CaseRuntime);rt.config=authority['model_controls'];rt.authorization=authority;rt.task_context=ctx
        rebuilt,_=rt.build_payload(saved['payload']['messages'],saved['internal_tools'])
        before=sha_file(Path(authority['ledger_path']))
        proof=prepare_rejection_proof(ctx,authority,original,diagnostic,primary,independent,old,rebuilt)
        self.assertEqual(proof['payload_sha256'],old['content_hash']);self.assertEqual(proof['request_digest'],ctx['request_digest'])
        self.assertEqual(sha_file(Path(authority['ledger_path'])),before)
        def killed(path):
            result=read_json(path)
            return dict(result,termination_requested=True) if Path(path).name=='process-exit.json' else result
        with patch('week13.src.case_http_rejection.read_json',side_effect=killed):
            with self.assertRaisesRegex(PermissionError,'naturally terminated'):
                prepare_rejection_proof(ctx,authority,original,diagnostic,primary,independent,old,rebuilt)


if __name__=='__main__':unittest.main()
