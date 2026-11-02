"""Local projection and unsent journal fixtures; no product/tool processes."""
import asyncio
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from week13.src import case_paths, case_requests, case_taskflow
from week13.src.case_product_context import PREFIX, bootstrap_source, project_messages, project_context, projected_context_matches
from week13.src.case_runtime import CaseRuntime, _estimate_input
from week13.tests.test_case_agent_requests import synthetic_spec, write

EVIDENCE = case_paths.ROOT / 'results/context-growth-20261022-01'


def context():
    original = dict(id='leaf', text='普通原始资料独有硬约束：禁止改变接触参数，电缆不低于机架。')
    spec = synthetic_spec(original, 85)
    spec['trajectory_contract'] = dict(required=['complete original trajectory'])
    ref = dict(project_id='TEST_ONLY_project', version_id='v000001', spec_digest=case_paths.sha_value(spec))
    snapshot = dict(ref, frozen_spec=spec, task_status='PASS', outcome=dict(task_status='PASS', mission_assessment=dict(events=[dict(x=1)], limitations=['fixture limitation'])))
    raw = '本次楼梯230mm×250mm，其余继承。'
    return dict(request_id='TEST_ONLY_context', request_digest='TEST_ONLY_digest', project_id=ref['project_id'], baseline_ref=ref,
                baseline_snapshot=snapshot, raw_request=raw, sources=[dict(id='current', text=raw), dict(id='history', text=PREFIX+json.dumps(dict(reference=ref, spec=spec, task_status='PASS', limitations='fixture'), ensure_ascii=False))])


class ProductProjection(unittest.TestCase):
    def test_current_model_contract_raw_and_source_quotes_are_exact(self):
        ctx = context(); before = deepcopy(ctx)
        raw = dict(raw_request=ctx['raw_request'], trusted_sources=ctx['sources'], baseline_snapshot=ctx['baseline_snapshot'])
        projected = project_context(ctx, raw); spec = projected['baseline_snapshot']['frozen_spec']
        for key, value in ctx['baseline_snapshot']['frozen_spec'].items():
            if key != 'sources': self.assertEqual(spec[key], value, key)
        self.assertEqual(projected['raw_request'], ctx['raw_request'])
        current = next(s for s in projected['product_context_projection']['source_catalog'] if s['id']=='current')
        self.assertEqual(current['text'], ctx['raw_request']); self.assertEqual(ctx, before)
        self.assertTrue(projected_context_matches(ctx, projected))
        projected['baseline_snapshot']['frozen_spec']['artifact']['modelica_source'] += 'drift'
        self.assertFalse(projected_context_matches(ctx, projected))

    def test_user_input_that_looks_like_envelope_is_still_full_original_text(self):
        ctx=context(); ctx['raw_request']=ctx['sources'][1]['text'];ctx['sources'][0]['text']=ctx['raw_request']
        value=project_context(ctx,dict(raw_request=ctx['raw_request'],baseline_snapshot=ctx['baseline_snapshot'],trusted_sources=ctx['sources']))
        current=next(s for s in value['product_context_projection']['source_catalog'] if s['id']=='current')
        self.assertEqual(current['text'],ctx['raw_request']);self.assertEqual(current['role'],'ORIGINAL_SOURCE_TEXT')

    def test_unrecognized_envelope_and_ordinary_constraints_not_summarized(self):
        ctx=context();ctx['sources'][1]['text']=PREFIX+'{"ordinary":"not the generated schema"}'
        value=project_context(ctx,dict(baseline_snapshot=ctx['baseline_snapshot']))
        catalog=value['product_context_projection']['source_catalog']
        self.assertEqual(next(s for s in catalog if s['id']=='history')['text'],ctx['sources'][1]['text'])
        self.assertIn('电缆不低于机架',next(s for s in catalog if s['id']=='leaf')['text'])

    def test_future_generations_are_linear_and_retain_unmapped_original_constraint(self):
        ctx=context(); snapshot=ctx['baseline_snapshot']; lengths=[]
        original=snapshot['frozen_spec']['sources'][0]['text']
        for n in range(12):
            ref=dict(project_id='TEST_ONLY_project',version_id='v'+str(n),spec_digest=case_paths.sha_value(snapshot['frozen_spec']))
            text=bootstrap_source(snapshot,ref);lengths.append(len(text.encode()))
            spec=deepcopy(snapshot['frozen_spec']);spec['sources']=[dict(id='user_'+str(n),text='原始用户第'+str(n)+'代约束'),dict(id='envelope_'+str(n),text=text)]
            spec['requirements']=[dict(text='当代要求',source_id=spec['sources'][0]['id'],source_text=spec['sources'][0]['text'])]
            snapshot=dict(ref,frozen_spec=spec,task_status='PASS')
        latest=dict(ctx,baseline_snapshot=snapshot,baseline_ref=ref,sources=[dict(id='latest',text='本次原文'),dict(id='final',text=bootstrap_source(snapshot,dict(ref,spec_digest=case_paths.sha_value(snapshot['frozen_spec']))))])
        value=project_context(latest,dict(baseline_snapshot=snapshot))
        self.assertEqual(next(s for s in value['product_context_projection']['source_catalog'] if s['id']=='leaf')['text'],original)
        self.assertLess(lengths[-1],lengths[0]+len(lengths)*10000)
        self.assertLess(max(b-a for a,b in zip(lengths,lengths[1:])),10000)

    def test_all_stage_messages_share_projection_and_history_is_untouched(self):
        ctx=context();ctx['baseline_snapshot']['outcome']['tool_result']={'stdout':'repeat'*10000}
        history=[dict(role='assistant',content='complete text',reasoning_content='FULL_REASONING'*1000),dict(role='tool',content='FULL_TOOL_CONTENT'*1000,tool_call_id='fixture')]
        for stage in ('plan','generate','ask','reopen'):
            content=dict(raw_request=ctx['raw_request'],baseline_snapshot=ctx['baseline_snapshot'])
            if stage=='plan':content['trusted_sources']=ctx['sources']
            if stage=='generate':content['confirmed_plan']=dict(requirements=[])
            messages=[dict(role='system',content='fixture'),dict(role='user',content=json.dumps(content,ensure_ascii=False)),*history]
            result=project_messages(ctx,messages)
            self.assertLess(_estimate_input(result,[]),_estimate_input(messages,[]))
            self.assertEqual(result[2:],history)
            self.assertEqual(json.loads(result[1]['content'])['baseline_snapshot']['frozen_spec']['artifact'],ctx['baseline_snapshot']['frozen_spec']['artifact'])

    def test_really_large_current_source_still_hits_original_window_guard(self):
        from week13.src.case_strict_tools import FLAT_PLAN_PROFILE, STRICT_BASE
        ctx=context();ctx['baseline_snapshot']['frozen_spec']['artifact']['modelica_source']='x'*1100000
        messages=project_messages(ctx,[dict(role='system',content='fixture'),dict(role='user',content=json.dumps(dict(baseline_snapshot=ctx['baseline_snapshot'])))])
        rt=CaseRuntime.__new__(CaseRuntime);rt.config=dict(context_window_tokens=1000000,tool_output_profile=FLAT_PLAN_PROFILE,api_base=STRICT_BASE);rt.authorization={}
        with self.assertRaisesRegex(ValueError,'no HTTP, no truncation'):rt.build_payload(messages,[])


class UnsentProjectionJournal(unittest.TestCase):
    def setUp(self):
        EVIDENCE.mkdir(parents=True,exist_ok=True)
        self.temp=tempfile.TemporaryDirectory(dir=EVIDENCE,prefix='TEST_ONLY-journal-');self.addCleanup(self.temp.cleanup)
        self.ctx=context();self.root=Path(self.temp.name);self.ctx['root']=str(self.root)
        self.ctx.update(displayed_digest=None,confirmation_path=None,runtime_authority=dict(path=str(self.root/'authorization.json'),sha256='TEST_ONLY_auth',ledger_path=str(self.root/'ledger.sqlite')))
        self.authority=dict(started_at_utc=(datetime.now(timezone.utc)-timedelta(minutes=1)).isoformat(),hard_deadline_utc=(datetime.now(timezone.utc)+timedelta(minutes=20)).isoformat())
        with closing(sqlite3.connect(self.root/'ledger.sqlite')) as db, db:
            db.execute('CREATE TABLE requests(request_id TEXT,status TEXT)');db.execute('CREATE TABLE meta(key TEXT,value TEXT)');db.execute('INSERT INTO meta VALUES(?,?)',('authorization_sha256','TEST_ONLY_auth'))
        self.ctx['baseline_snapshot']['outcome']['tool_result']={'stdout':'fixture tool log '*75000}
        messages=[dict(role='system',content='fixture'),dict(role='user',content=json.dumps(dict(raw_request=self.ctx['raw_request'],baseline_snapshot=self.ctx['baseline_snapshot'],trusted_sources=self.ctx['sources']),ensure_ascii=False))]
        self.old=dict(request_id='case_plan_'+self.ctx['request_id'],request_digest=self.ctx['request_digest'],messages=messages,tools=[])
        self.new=dict(self.old,messages=project_messages(self.ctx,messages))
        self.flow=case_taskflow.TaskFlow(self.ctx);self.location=self.root/'turns/plan';write(self.location/'request.json',self.old)
        self.patch=patch.object(case_requests,'authority_for',return_value=self.authority);self.patch.start();self.addCleanup(self.patch.stop)

    def append(self):self.flow._append_preflight_projection(self.location,self.old,self.new)

    def test_empty_ledger_appends_same_identity_and_old_journal_remains(self):
        before=(self.location/'request.json').read_bytes();ledger=(self.root/'ledger.sqlite').read_bytes()
        self.append();self.assertEqual(self.flow.effective_stage_request(self.location),self.new)
        self.assertEqual((self.location/'request.json').read_bytes(),before);self.assertEqual((self.root/'ledger.sqlite').read_bytes(),ledger)
        # Saving the eventual plan changes task state, not the immutable
        # preflight projection binding used by charged-response recovery.
        self.ctx['displayed_digest']='LATER_PRODUCT_PLAN'
        self.assertEqual(self.flow.effective_stage_request(self.location),self.new)
        self.append()

    def test_every_existing_ledger_status_blocks_automatic_resend(self):
        for status in ('not_dispatched','reserved','dispatched','succeeded','rejected','unknown'):
            with self.subTest(status=status):
                with closing(sqlite3.connect(self.root/'ledger.sqlite')) as db, db:
                    db.execute('DELETE FROM requests');db.execute('INSERT INTO requests VALUES(?,?)',(self.old['request_id'],status))
                with self.assertRaisesRegex(ValueError,'HTTP ledger'):self.append()
                self.assertFalse((self.location/'request-projection.json').exists())

    def test_response_protocol_or_http_directory_blocks(self):
        for name in ('response.json','protocol-response.json','unknown.json'):
            path=self.location/name;write(path,{})
            with self.assertRaisesRegex(ValueError,'response/protocol/HTTP'):self.append()
            path.unlink()
        directory=self.root/'http'/case_paths.sha_value(self.old['request_id']);directory.mkdir(parents=True)
        with self.assertRaisesRegex(ValueError,'response/protocol/HTTP'):self.append()

    def test_expired_authority_drifted_task_or_different_content_is_not_migrated(self):
        self.authority['hard_deadline_utc']=(datetime.now(timezone.utc)-timedelta(seconds=1)).isoformat()
        with self.assertRaises(PermissionError):self.append()
        self.authority['hard_deadline_utc']=(datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat()
        changed=deepcopy(self.new);changed['messages'][0]['content']='different instruction'
        with self.assertRaisesRegex(ValueError,'outside structured projection'):self.flow._append_preflight_projection(self.location,self.old,changed)
        self.ctx['baseline_ref']=dict(self.ctx['baseline_ref'],version_id='different')
        with self.assertRaises(ValueError):self.append()

    def test_receipt_or_original_tamper_cannot_change_effective_payload(self):
        self.append();receipt=self.location/'request-projection-receipt.json';value=case_paths.read_json(receipt);value['same_http_request_id']=False;write(receipt,value)
        with self.assertRaisesRegex(ValueError,'binding drift'):self.flow.effective_stage_request(self.location)

    def test_ordinary_call_uses_append_then_same_rid_without_changing_guards(self):
        sent=[]
        class FakeRuntime:
            def require_credentials(self):pass
            def complete(self,messages,tools,rid):
                sent.append((messages,tools,rid));return {'content':'TEST_ONLY'}
        with patch.object(self.flow,'refresh',return_value=self.ctx),patch.object(case_requests,'require_planning',return_value=self.ctx),patch.object(self.flow,'_runtime',return_value=FakeRuntime()):
            result=asyncio.run(self.flow.call('plan',self.old['messages'],[]))
        self.assertEqual(result,{'content':'TEST_ONLY'});self.assertEqual(sent[0][2],self.old['request_id'])
        self.assertEqual(sent[0][0],self.new['messages']);self.assertEqual(self.flow.effective_stage_request(self.location),self.new)


if __name__=='__main__':unittest.main()
