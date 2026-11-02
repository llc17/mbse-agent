"""Independent offline entry checks. Synthetic records below are TEST fixtures.

The two saved CSVs are read by the existing checker only. Product transport is
stopped at a mocked call before HTTP or persistence; no physical result is made.
"""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from week13.src.case_contact_review import current_summary, with_review
from week13.src.case_console_result import prepare_result
from week13.src.case_semantic_guard import explanation_facts
from week13.src.case_taskflow import TaskFlow, module


ROOT = Path(__file__).resolve().parents[1]
PAIR = ROOT / 'results/paired-stairs-150270-250260-20261023-01/comparison.json'


class StopBeforeHTTP(Exception):
    pass


def synthetic_current(*, invalid=False):
    """TEST fixture; a display branch, never evidence of vehicle success."""
    record = dict(project_id='TEST_PROJECT', version_id='vTEST_CURRENT', task_id='TEST_TASK',
                  state='succeeded', evidence_status='COMPLETE', tool_status='PASS',
                  chain_status='PASS', task_status='PASS', spec_digest='1' * 64,
                  source_sha256='2' * 64, manifest_sha256='3' * 64)
    assessment = dict(task_status='UNKNOWN' if invalid else 'FAIL',
                      validity_status='FAIL' if invalid else 'PASS',
                      completion_status='INVALID_MODEL' if invalid else 'OBSERVATION_INTERVAL_INCOMPLETE',
                      physical_status='UNKNOWN')
    record['outcome'] = dict(tool_status='PASS', chain_status='PASS', task_status='PASS',
                             mission_assessment=dict(task_status='PASS', validity_status='PASS',
                             completion_status='COMPLETE_FORWARD_UP20_DOWN20_EXIT_AND_CONTINUE'))
    record['current_contact_review'] = dict(schema='v12-current-contact-review-1',
        status=assessment['validity_status'], **assessment,
        current_assessment=assessment, contact_validity=dict(hard_contradictions=[]),
        binding={key: record[key] for key in ('project_id', 'version_id', 'task_id',
                                            'spec_digest', 'source_sha256', 'manifest_sha256')})
    record['current_contact_review']['binding']['csv_sha256'] = '4' * 64
    return record


class VerdictProjectionIndependentTests(unittest.TestCase):
    def test_unreviewed_history_stays_explicitly_historical(self):
        source = synthetic_current()
        source.pop('current_contact_review')
        before = deepcopy(source)
        row = current_summary(source)
        self.assertEqual(source, before)
        self.assertEqual(row.get('current_review_state'), 'NOT_REVIEWED')
        self.assertEqual(row.get('recorded_task_status'), 'PASS')
        self.assertEqual(row.get('task_status'), 'UNKNOWN')
        self.assertNotEqual(row.get('chain_status'), 'BLOCKED_CONTACT_EVIDENCE')

    def test_cross_instance_and_source_binding_review_never_promote_success(self):
        for field in ('project_id', 'task_id', 'version_id', 'spec_digest', 'source_sha256', 'manifest_sha256'):
            with self.subTest(field=field):
                source = synthetic_current()
                source['current_contact_review']['binding'][field] = 'TEST_WRONG_BINDING'
                row = current_summary(source)
                self.assertNotEqual(row.get('current_review_state'), 'CURRENT_BOUND_REVIEW')
                self.assertEqual(row.get('task_status'), 'UNKNOWN')
                facts = explanation_facts(dict(project_id=source['project_id'], latest_request_execution=source))
                self.assertEqual(facts[0]['task_status'], 'UNKNOWN')

    def test_current_incomplete_overrides_historical_mission_and_saved_validation(self):
        source = synthetic_current()
        current = current_summary(source)
        raw = 'TEST：历史记录虽有PASS，当前任务成功。'
        result = dict(request_id='TEST_REQUEST', execution=source,
                      state=dict(request_id='TEST_REQUEST', project_id='TEST_PROJECT',
                                 latest_request_execution=current),
                      explanation=dict(raw_answer=raw, answer=raw,
                          explanation_validation=dict(status='NO_KNOWN_FACT_CONFLICT',
                          body_displayed=True, full_semantic_validation=True)))
        before = deepcopy(result)
        view, execution, _, lines = prepare_result(result, request_id='TEST_REQUEST')
        self.assertEqual(result, before)
        self.assertIn('观察区间内任务未完成', lines[0])
        self.assertEqual(execution['task_status'], 'FAIL')
        self.assertEqual(view['explanation'].get('raw_answer'), raw)
        self.assertIs(view['explanation']['explanation_validation']['full_semantic_validation'], False)
        self.assertNotIn('路线已完成', lines[0])

    def test_current_invalid_is_not_historical_task_success(self):
        source = synthetic_current(invalid=True)
        result = dict(request_id='TEST_REQUEST', execution=source,
                      state=dict(project_id='TEST_PROJECT', latest_request_execution=current_summary(source)))
        _, execution, _, lines = prepare_result(result, request_id='TEST_REQUEST')
        self.assertIn('INVALID_MODEL', lines[0])
        self.assertEqual(execution['task_status'], 'UNKNOWN')
        self.assertNotIn('路线已完成', lines[0])

    def test_outer_project_cannot_repair_cross_project_record_identity(self):
        source = synthetic_current()
        source['project_id'] = 'TEST_OTHER_PROJECT'
        facts = explanation_facts(dict(project_id='TEST_PROJECT', latest_request_execution=source))
        self.assertEqual(facts[0]['task_status'], 'UNKNOWN')
        self.assertEqual(facts[0]['current_review_state'], 'BINDING_CONFLICT')

    def test_display_rejects_same_version_from_different_project(self):
        source = synthetic_current()
        other = current_summary(source)
        other['project_id'] = 'TEST_OTHER_PROJECT'
        _, _, _, lines = prepare_result(dict(request_id='TEST_REQUEST', execution=source,
            state=dict(project_id='TEST_OTHER_PROJECT', latest_request_execution=other)), request_id='TEST_REQUEST')
        self.assertIn('证据存在冲突', lines[0])

    def test_test_fixture_success_branch_and_useful_unverified_answer_are_preserved(self):
        source = synthetic_current()
        complete = 'COMPLETE_FORWARD_UP20_DOWN20_EXIT_AND_CONTINUE'
        review = source['current_contact_review']
        review.update(task_status='PASS', completion_status=complete)
        review['current_assessment'].update(task_status='PASS', completion_status=complete, completion_time=12.5)
        answer = 'TEST只读说明：可比较已保存参数，新的执行仍须相应授权。'
        result = dict(request_id='TEST_REQUEST', execution=source,
                      explanation=dict(raw_answer=answer, answer=answer))
        view, _, _, lines = prepare_result(result, request_id='TEST_REQUEST')
        self.assertIn('路线已完成', lines[0])
        self.assertIn('12.5 s', '\n'.join(lines))
        self.assertIn(answer, view['explanation']['supplemental_analysis'])
        self.assertIn('未验证', view['explanation']['supplemental_analysis'])
        self.assertEqual(view['explanation']['raw_answer'], answer)

    def test_project_history_pass_is_not_a_new_request_result(self):
        result = dict(request_id='TEST_NEW_REQUEST', stage='PLAN_READY',
                      state=dict(project_id='TEST_PROJECT', latest_request_execution=None,
                                 latest_project_execution=synthetic_current(), last_task_pass=synthetic_current()),
                      explanation=dict(raw_answer='TEST当前任务已经完成。', answer='TEST当前任务已经完成。'))
        _, _, _, lines = prepare_result(result, request_id='TEST_NEW_REQUEST')
        self.assertIn('尚未运行', lines[0])
        self.assertNotIn('路线已完成', lines[0])

    def test_fresh_explicit_other_version_analysis_survives_without_trusting_old_validation(self):
        source = synthetic_current()
        source['version_id'] = 'v000009'
        source['current_contact_review']['binding']['version_id'] = 'v000009'
        other = synthetic_current()
        other.update(version_id='v000010', task_id='TEST_OTHER_TASK')
        other['current_contact_review']['binding'].update(version_id='v000010', task_id='TEST_OTHER_TASK')
        main = current_summary(source)
        additional = current_summary(other)
        raw = 'TEST比较v000009与v000010：两者观察区间内任务未完成。'
        result = dict(request_id='TEST_REQUEST', execution=source,
            state=dict(project_id='TEST_PROJECT', latest_request_execution=main),
            current_fact_binding=dict(project_id='TEST_PROJECT', latest_request_execution=main,
                                      explicitly_reviewed_executions=[additional]),
            explanation=dict(raw_answer=raw, answer=raw))
        view, _, _, lines = prepare_result(result, request_id='TEST_REQUEST')
        self.assertIn('观察区间内任务未完成', lines[0])
        self.assertTrue(view['explanation']['explanation_validation']['body_displayed'])
        self.assertIn(raw, view['explanation']['supplemental_analysis'])
        result.pop('current_fact_binding')
        result['explanation']['explanation_validation'] = dict(status='NO_KNOWN_FACT_CONFLICT',
            body_displayed=True, facts=[additional])
        old, _, _, _ = prepare_result(result, request_id='TEST_REQUEST')
        self.assertFalse(old['explanation']['explanation_validation']['body_displayed'])
        self.assertNotIn(raw, old['explanation']['supplemental_analysis'])


class SavedRealEntryIndependentTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rows = json.loads(PAIR.read_text(encoding='utf8'))['rows']
        cls.flows = {row['version_id']: TaskFlow.load(row['request_id']) for row in cls.rows}

    def assert_incomplete(self, row):
        self.assertEqual(row['tool_status'], 'PASS')
        self.assertEqual(row['chain_status'], 'PASS')
        self.assertEqual(row['task_status'], 'FAIL')
        self.assertEqual(row['validity_status'], 'PASS')
        self.assertEqual(row['completion_status'], 'OBSERVATION_INTERVAL_INCOMPLETE')
        self.assertEqual(row['physical_status'], 'UNKNOWN')

    def test_saved_real_state_and_console_share_current_bound_verdict(self):
        for expected in self.rows:
            with self.subTest(version=expected['version_id']):
                flow = self.flows[expected['version_id']]
                state = flow.state()
                latest = state['latest_request_execution']
                self.assertEqual(latest['version_id'], expected['version_id'])
                self.assertEqual(latest['task_id'], expected['task_id'])
                self.assert_incomplete(latest)
                self.assertEqual(latest['current_review_state'], 'CURRENT_BOUND_REVIEW')
                facts = explanation_facts(state)
                self.assert_incomplete(facts[0])
                _, execution, _, lines = prepare_result(dict(request_id=flow.ctx['request_id'], state=state),
                                                         request_id=flow.ctx['request_id'])
                self.assertIn('观察区间内任务未完成', lines[0])
                self.assertEqual(execution['task_status'], 'FAIL')

    def test_saved_real_ask_payload_stops_before_http_and_preserves_current_facts(self):
        for expected in self.rows:
            with self.subTest(version=expected['version_id']):
                flow = self.flows[expected['version_id']]
                fake_call = AsyncMock(side_effect=StopBeforeHTTP('offline transport interception'))
                with patch.object(flow, 'call', fake_call), patch('week13.src.case_taskflow.emit_progress'):
                    with self.assertRaises(StopBeforeHTTP):
                        asyncio.run(flow.ask('TEST仅核对事实，不执行。'))
                self.assertEqual(fake_call.await_count, 1)
                messages = fake_call.await_args.args[1]
                payload = json.loads(messages[1]['content'])
                actual = payload['actual_request_execution']
                self.assert_incomplete(actual)
                self.assertEqual(actual['current_contact_review']['binding']['version_id'], expected['version_id'])
                self.assertEqual(actual['current_contact_review']['contact_validity']['hard_contradictions'], [])

    def test_saved_real_review_evidence_and_review_prompt_agree(self):
        # RESULT_REVIEW_ONLY object is a TEST-only envelope over the real v9
        # service. It is not saved as a request and cannot acquire authority.
        expected = self.rows[0]
        origin = self.flows[expected['version_id']]
        svc = origin.open(read_only=True)
        snapshot = svc._artifact_version(expected['version_id'])
        reference = module('artifact_baseline').reference(snapshot)
        ctx = dict(origin.ctx, access_mode='RESULT_REVIEW_ONLY', step_id='explain',
                   baseline_version_id=expected['version_id'], baseline_snapshot=snapshot,
                   review_binding=dict(reference=reference, origin_request_id=origin.ctx['request_id'],
                     origin_request_digest=origin.ctx['request_digest'],
                     original_partial_path=expected['evidence']['pipeline']['path']))
        review = TaskFlow(ctx)
        bound = current_summary(with_review(snapshot, svc.project_root))
        state = dict(request_id='TEST_READONLY_REVIEW', project_id=ctx['project_id'],
                     latest_request_execution=None, reviewed_execution=bound)
        fake_call = AsyncMock(side_effect=StopBeforeHTTP('offline review interception'))
        with patch.object(review, 'refresh', return_value=ctx), patch.object(review, 'open', return_value=svc):
            evidence = review.review_evidence()
            self.assert_incomplete(evidence)
            self.assertEqual(evidence['mission']['completion_status'], 'OBSERVATION_INTERVAL_INCOMPLETE')
            with patch.object(review, 'state', return_value=state), patch.object(review, 'call', fake_call), \
                 patch('week13.src.case_stage_facts.read_stage_facts', return_value={}):
                with self.assertRaises(StopBeforeHTTP):
                    asyncio.run(review.ask_review('TEST仅核对已有结果。'))
        payload = json.loads(fake_call.await_args.args[1][1]['content'])
        self.assert_incomplete(payload['bound_existing_execution'])
        self.assertEqual(payload['new_review_state']['reviewed_execution']['task_status'], 'FAIL')


if __name__ == '__main__':
    unittest.main()
