"""Bounded disclosure tests; no HTTP, worker, compiler or dynamic integration."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from week13.src.case_contact_validity import (
    CAPABILITY_LIMITATION, VERIFICATION_SCOPE, diagnose_contact_rows,
    verification_disclosure,
)
from week13.src.case_contact_review import current_summary, review_snapshot
from week13.src.case_console import Console
from week13.src.case_console_result import prepare_result
from week13.src.case_semantic_guard import explanation_facts, protect_explanation
from week13.tests.test_case_contact_validity import VALUES, samples
from week13.tests.test_case_contact_integration import write_fixture
from week13.tests.test_case_console_result_headline import saved_result

OUT = (Path(__file__).resolve().parents[1] /
       'results/contact-closure-primitives-20261023-01/integration-audit')


class ContactCapabilityDisclosureTests(unittest.TestCase):
    def test_sample_pass_never_assesses_wheel_or_friction_closure(self):
        for family in (None, 'source-review-bound-stair-contact-v1'):
            rows = samples(omega_wheel=12., slip_ratio=.2)
            before = deepcopy(rows)
            report = diagnose_contact_rows(rows, VALUES, source_family=family)
            self.assertEqual(report['status'], 'PASS')
            self.assertEqual(report['physical_status'], 'UNKNOWN')
            self.assertEqual(report['verification_scope'], VERIFICATION_SCOPE)
            self.assertEqual(report['capabilities'], verification_disclosure()['capabilities'])
            self.assertEqual(set(report['capabilities'].values()), {'NOT_ASSESSED'})
            self.assertIn(CAPABILITY_LIMITATION, report['limitations'])
            self.assertEqual(rows, before)

    def test_hard_contradiction_keeps_existing_failure_semantics(self):
        report = diagnose_contact_rows(samples(2.1, .2), VALUES)
        self.assertEqual(report['status'], 'FAIL')
        self.assertTrue(report['hard_contradictions'])
        self.assertEqual(report['physical_status'], 'UNKNOWN')
        self.assertEqual(set(report['capabilities'].values()), {'NOT_ASSESSED'})

    def test_manifest_review_preserves_disclosure_and_is_readonly(self):
        OUT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='capability-review-', dir=OUT) as folder:
            root = Path(folder)
            snapshot, base = write_fixture(root)
            before = deepcopy(snapshot)
            hashes = {p: hashlib.sha256(p.read_bytes()).hexdigest()
                      for p in base.rglob('*') if p.is_file()}
            # Only the test path gate is replaced; manifest/source/CSV binding,
            # assessment and the real projection all run against local fixtures.
            with patch('week13.src.case_contact_review._base', return_value=base):
                review = review_snapshot(snapshot, root)
            contact = review['contact_validity']
            self.assertIn(CAPABILITY_LIMITATION, contact['limitations'])
            self.assertEqual(review['capabilities'], contact['capabilities'])
            self.assertEqual(review['verification_scope'], contact['verification_scope'])
            self.assertEqual(review['physical_status'], 'UNKNOWN')
            self.assertEqual(snapshot, before)
            self.assertEqual(hashes, {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in hashes})

    def test_legacy_summary_and_fact_projection_stay_bounded_and_do_not_promote(self):
        execution = saved_result()['execution']
        execution['current_contact_review']['unrelated_history'] = {'nested': ['x'] * 10000}
        before = deepcopy(execution)
        summary = current_summary(execution)
        binding = dict(request_id='TEST_ONLY_request', project_id='TEST_ONLY_project',
                       latest_request_execution=summary)
        facts = explanation_facts(binding)
        self.assertEqual(facts[0]['verification_scope'], VERIFICATION_SCOPE)
        self.assertEqual(facts[0]['capabilities'], verification_disclosure()['capabilities'])
        self.assertEqual(facts[0]['physical_status'], 'UNKNOWN')
        self.assertLess(len(json.dumps(facts)), 2500)
        self.assertNotIn('unrelated_history', facts[0])
        self.assertEqual(execution, before)
        answer = protect_explanation('仅解释保存的路线结果与检查范围。', binding)
        self.assertEqual(answer['answer'].count(CAPABILITY_LIMITATION), 1)
        self.assertIn('有限采样检查 PASS', answer['answer'])
        self.assertFalse(answer['explanation_validation']['full_semantic_validation'])

    def test_assumed_route_complete_and_readonly_result_disclose_immediately(self):
        for readonly in (False, True):
            result = saved_result()
            if readonly:
                result['state'] = dict(reviewed_execution=result.pop('execution'))
            before = deepcopy(result)
            _, execution, reviewed, lines = prepare_result(result)
            self.assertIn('任务成功（当前简化模型内）', lines[0])
            self.assertNotIn('UNKNOWN', lines[0])
            self.assertEqual(lines[1], CAPABILITY_LIMITATION)
            self.assertEqual(reviewed, readonly)
            self.assertEqual(execution['task_status'], 'PASS')
            self.assertEqual(result, before)

    def test_task_failure_is_preserved_and_never_relabelled_invalid_for_capability_gap(self):
        result = saved_result(task_status='FAIL', mission=dict(
            validity_status='PASS', task_status='FAIL',
            completion_status='OBSERVATION_INTERVAL_INCOMPLETE'))
        _, execution, _, lines = prepare_result(result)
        self.assertIn('观察区间内任务未完成', lines[0])
        self.assertNotIn('INVALID_MODEL', lines[0])
        self.assertEqual(lines[1], CAPABILITY_LIMITATION)
        self.assertEqual((execution['validity_status'], execution['task_status']), ('PASS', 'FAIL'))

    def test_console_result_and_readonly_question_both_show_scope_before_prose(self):
        OUT.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='capability-console-', dir=OUT) as folder:
            output = []
            execution = saved_result()['execution']
            class Runner:
                def call(self, *args, **kwargs):
                    return dict(answer='TEST_ONLY 已核对的解释。',
                                explanation_validation=dict(status='NO_KNOWN_FACT_CONFLICT'),
                                binding=dict(latest_request_execution=execution))
            ui = Console(Runner(), console_root=Path(folder), output=output.append)
            ui.ctx = dict(request_id='TEST_ONLY_request', root=folder)
            ui.selection['request_id']='TEST_ONLY_request'
            ui.show_result(saved_result())
            self.assertTrue(output[0].startswith('【任务结论】'))
            self.assertEqual(output[-1], '适用范围：当前简化模型仿真；真实车辆能力尚未验证。')
            self.assertIn('有限采样检查 PASS', '\n'.join(output))
            self.assertNotIn('模型有效性：通过', output)
            output.clear()
            ui.budget_guard = lambda: True
            ui.refresh = lambda: None
            ui.ask('TEST_ONLY 检查是否验证了滑移？')
            first = next(i for i, line in enumerate(output) if line.startswith('【任务结论】'))
            self.assertEqual(output[first + 1], CAPABILITY_LIMITATION)


if __name__ == '__main__':
    unittest.main()
