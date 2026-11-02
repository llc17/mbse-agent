"""Actual isolated request/authority/SQLite guards for zero-HTTP replay."""
from copy import deepcopy
from contextlib import closing
from datetime import datetime,timedelta,timezone
from pathlib import Path
import json,sqlite3,tempfile,unittest,uuid
from week13.src import case_paths,case_requests,case_taskflow
from week13.src.case_budget import CaseBudget
from week13.tests.test_case_agent_requests import Harness

EVIDENCE=case_paths.ROOT/'results/generation-protocol-repair-20261028-01'
CAPS=dict(http_requests=0,product_cny=0,total_tokens=0,total_output_tokens=0,compile=1,simulation=1,candidates=1,software_repair_rounds=0,parameter_revision_rounds=0,observation_extensions=0)

class ZeroHttpCandidate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.original=(case_requests.ROOT,case_requests.REQUESTS_ROOT,case_requests.LEGACY_PROJECT_ROOT)
        cls.temp=tempfile.TemporaryDirectory(prefix='TEST_ONLY_zero_http_',dir=EVIDENCE)
        cls.h=Harness(Path(cls.temp.name)/'fixture')
    @classmethod
    def tearDownClass(cls):
        case_requests.ROOT,case_requests.REQUESTS_ROOT,case_requests.LEGACY_PROJECT_ROOT=cls.original
        cls.temp.cleanup()
    def request(self,*,confirmed=True,planning=False,execution=True,expired=False):
        rid='TEST_ONLY_zero_'+uuid.uuid4().hex[:12]
        ctx=self.h.bootstrap(rid,self.h.project_id,'v000002','把总质量改为92kg，增加部分在原重心，几何控制不变。')
        protocol=self.h.transport(ctx,dict(messages=[{},dict(content=json.dumps(dict(raw_request=ctx['raw_request'])))],tools=[case_taskflow.PLAN_TOOL]))
        packet=json.loads(protocol['choices'][0]['message']['tool_calls'][0]['function']['arguments'])['packet']
        ctx=case_requests.save_plan(rid,packet,sources=ctx['sources'])
        if confirmed:case_requests.confirm_request(rid,ctx['displayed_digest'],'CONFIRM')
        now=datetime.now(timezone.utc)
        ctx=case_requests.authorize_budget(rid,rid,'CONFIRM',caps=CAPS,started_at_utc=(now-timedelta(hours=1)).isoformat() if expired else now.isoformat(),hard_deadline_utc=(now-timedelta(seconds=1)).isoformat() if expired else (now+timedelta(minutes=10)).isoformat(),planning_http=planning,execution=execution,test_only=True)
        return ctx,CaseBudget(task_context=ctx)
    def test_valid_zero_http_execution_counts_once_idempotently_and_enforces_cap(self):
        ctx,budget=self.request();detail=dict(candidate='TEST_ONLY_exact')
        budget.consume('candidate','TEST_ONLY_candidate',detail);budget.consume('candidate','TEST_ONLY_candidate',detail)
        with closing(sqlite3.connect(budget.path)) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM counters').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM requests').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM process_attempts').fetchone()[0],0)
        self.assertEqual(case_requests.authority_for(ctx)['caps']['http_requests'],0)
        self.assertFalse(case_requests.authority_for(ctx)['permissions']['planning_http'])
        with self.assertRaisesRegex(RuntimeError,'allowance exhausted'):budget.consume('candidate','TEST_ONLY_second',detail)
    def test_unconfirmed_no_execution_and_expired_reject_without_counter(self):
        for options in (dict(confirmed=False),dict(execution=False),dict(expired=True)):
            with self.subTest(options=options):
                ctx,budget=self.request(**options)
                with self.assertRaises((PermissionError,RuntimeError)):budget.consume('candidate','TEST_ONLY_rejected')
                with closing(sqlite3.connect(budget.path)) as db:self.assertEqual(db.execute('SELECT count(*) FROM counters').fetchone()[0],0)
    def test_other_event_types_still_require_planning(self):
        ctx,budget=self.request()
        for kind in ('software_repair','parameter_revision','observation_extension','source_revision'):
            with self.subTest(kind=kind),self.assertRaisesRegex(PermissionError,'planning HTTP'):budget.consume(kind,'TEST_ONLY_'+kind)
    def test_original_planning_path_preserved_without_execution_or_confirmation(self):
        ctx,budget=self.request(confirmed=False,planning=True,execution=False)
        budget.consume('candidate','TEST_ONLY_original_planning')
        with closing(sqlite3.connect(budget.path)) as db:self.assertEqual(db.execute('SELECT count(*) FROM counters').fetchone()[0],1)
    def test_readonly_context_cannot_use_execution_candidate_path(self):
        ctx,budget=self.request()
        readonly=deepcopy(ctx);readonly['access_mode']='RESULT_REVIEW_ONLY'
        # Exercise the actual full execution guard's first readonly boundary,
        # after this fixture budget's actual clock/authority check succeeds.
        from unittest.mock import patch
        original=case_requests.load_request
        with patch.object(case_requests,'load_request',side_effect=lambda rid:readonly if rid==ctx['request_id'] else original(rid)):
            with self.assertRaisesRegex(PermissionError,'readonly/plan-only'):budget.consume('candidate','TEST_ONLY_readonly')
        with closing(sqlite3.connect(budget.path)) as db:self.assertEqual(db.execute('SELECT count(*) FROM counters').fetchone()[0],0)

if __name__=='__main__':unittest.main()
