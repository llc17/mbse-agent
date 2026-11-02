"""Atomic quota/process launch tests on isolated temporary SQLite ledgers."""
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from week13.src.case_budget import CaseBudget, connection
from week13.src.case_paths import CONFIG, read_json


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.budget = CaseBudget(Path(self.folder.name) / 'fixture.sqlite')
        self.config = read_json(CONFIG)

    def request(self, rid, category='design'):
        self.budget.reserve_http(rid, rid, category, self.config)
        self.budget.launch_http(rid)
        self.budget.finish_http(rid, response=dict(ok=True), tokens=30, output=10, cost=.001)

    def test_protect_explanation_and_reopen_not_arbitrary_consultation(self):
        for index in range(10):
            self.request(str(index))
        with self.assertRaisesRegex(RuntimeError, 'protects'):
            self.request('extra_design')
        with self.assertRaisesRegex(RuntimeError, 'protects'):
            self.request('extra_consultation', 'consultation')
        self.request('explain', 'explanation')
        with self.assertRaisesRegex(RuntimeError, 'protects'):
            self.request('extra_consultation', 'consultation')
        self.request('reopen', 'reopen')
        self.assertEqual(self.budget.usage()['requests'], 12)

    def test_reservation_is_not_process_launch_and_failed_popen_releases_slot(self):
        attempt = self.budget.reserve_process('compile', dict(task_id='a'))
        self.assertEqual(self.budget.usage()['processes'], {})
        self.assertEqual(self.budget.finish_process(attempt, dict(error='Popen failed'))['state'], 'NOT_LAUNCHED')
        for index in range(6):
            attempt = self.budget.reserve_process('compile', dict(task_id=str(index)))
            self.budget.launch_process(attempt, 123)
            self.budget.finish_process(attempt, dict(returncode=1))
        with self.assertRaisesRegex(RuntimeError, 'quota'):
            self.budget.reserve_process('compile', dict(task_id='7'))
        self.assertEqual(self.budget.usage()['processes']['compile'], 6)

    def test_unknown_started_process_blocks_new_launch(self):
        attempt = self.budget.reserve_process('simulation', dict(task_id='a'))
        self.budget.launch_process(attempt, 123)
        self.budget.finish_process(attempt, dict(uncertain=True))
        with self.assertRaisesRegex(RuntimeError, 'unknown process'):
            self.budget.reserve_process('compile', dict(task_id='b'))

    def test_not_dispatched_http_is_not_real_request_or_unknown_charge(self):
        self.budget.reserve_http('a', 'hash', 'design', self.config)
        self.budget.finish_http('a', error='child failed before HTTP')
        self.assertEqual(self.budget.usage()['requests'], 0)
        self.assertEqual(self.budget.usage()['cost_cny_reserved_or_actual'], 0)

    def test_counters_same_event_no_reset_and_hard_bound(self):
        self.budget.consume('parameter_revision', 'a', dict(reason='evidence'))
        self.budget.consume('parameter_revision', 'a', dict(reason='evidence'))
        with self.assertRaisesRegex(RuntimeError, 'exhausted'):
            self.budget.consume('parameter_revision', 'b')
        with self.assertRaisesRegex(ValueError, 'changed'):
            self.budget.consume('parameter_revision', 'a', dict(reason='different'))

    def test_deadline_stops_before_new_request(self):
        with patch('week13.src.case_paths.now', return_value=datetime(2026,10,19,16,40,39,tzinfo=timezone.utc)):
            with self.assertRaisesRegex(RuntimeError, 'deadline'):
                self.budget.reserve_http('a', 'hash', 'design', self.config)
        self.assertEqual(self.budget.usage()['requests'], 0)


if __name__ == '__main__':
    unittest.main()
