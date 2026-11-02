"""Offline worker exit facts precede task publication; no real Popen or ledger."""
from types import SimpleNamespace
import subprocess
import threading
import unittest
from unittest.mock import patch

from week13.src import case_worker
from week13.src.inherited import service_adapter


class WorkerReceiptTests(unittest.TestCase):
    def manager(self,events,*,publish_interrupt=False):
        task=dict(state='running',project_id='project',version_id='v000001',worker_pid=20712,
                  result=dict(outcome_manifest_path='mock-manifest.json',outcome_manifest_sha256='mock',chain_status='PASS'))
        def get_task(task_id):events.append(('task_read',task['state']));return dict(task)
        def finish(task_id,state,**kwargs):
            events.append(('publish',state));task['state']=state
            if publish_interrupt:raise KeyboardInterrupt('MOCK ancestor shutdown immediately after terminal publication')
            return dict(task)
        manager=case_worker.ArtifactWorkerManager.__new__(case_worker.ArtifactWorkerManager)
        manager.owner='owner_test';manager.timeout_s=30
        manager.store=SimpleNamespace(task=get_task,finish_task=finish,request_cancel=lambda task_id:events.append(('cancel',task_id)))
        manager._attempts={'task':'worker_attempt'};manager._exit_receipts={'task':'MOCK_EXIT_RECEIPT.json'}
        manager._processes={'task':object()};manager._lock=threading.Lock()
        return manager

    def patches(self,events,*,bad_manifest=False):
        stack=__import__('contextlib').ExitStack();self.addCleanup(stack.close)
        stack.enter_context(patch.object(case_worker,'save_json',lambda path,value,**kwargs:events.append(('receipt_file',dict(value)))))
        stack.enter_context(patch.object(case_worker,'finish_process',lambda attempt,value:events.append(('ledger_finish',dict(value)))))
        stack.enter_context(patch.object(service_adapter,'_verify_manifest',side_effect=ValueError('MOCK invalid evidence')) if bad_manifest else
            patch.object(service_adapter,'_verify_manifest',return_value=dict(binding=dict(task_id='task',owner='owner_test'))))

    def test_exit_receipt_commits_before_any_task_read_or_success_publication(self):
        events=[];manager=self.manager(events);self.patches(events)
        process=SimpleNamespace(pid=21904,wait=lambda **kwargs:0,poll=lambda:0)
        manager._monitor('task',process)
        names=[event[0] for event in events]
        self.assertLess(names.index('ledger_finish'),names.index('task_read'))
        self.assertLess(names.index('ledger_finish'),names.index('publish'))
        receipt=next(x[1] for x in events if x[0]=='ledger_finish')
        self.assertEqual(receipt['pid'],21904)  # launcher PID differs from interpreter task PID
        self.assertEqual(receipt['returncode'],0);self.assertFalse(receipt['uncertain'])
        self.assertEqual(receipt['exit_observation'],'OWNED_POPEN_WAIT_RETURN')
        self.assertEqual(names.count('ledger_finish'),1)
        self.assertNotIn('task',manager._processes)

    def test_terminal_reader_interruption_cannot_leave_receipt_pending(self):
        events=[];manager=self.manager(events,publish_interrupt=True);self.patches(events)
        process=SimpleNamespace(pid=21904,wait=lambda **kwargs:0,poll=lambda:0)
        with self.assertRaises(KeyboardInterrupt):manager._monitor('task',process)
        self.assertEqual(sum(x[0]=='ledger_finish' for x in events),1)
        self.assertLess(next(i for i,x in enumerate(events) if x[0]=='ledger_finish'),next(i for i,x in enumerate(events) if x[0]=='publish'))
        self.assertNotIn('task',manager._processes)

    def test_bad_artifact_does_not_relabel_observed_process_exit(self):
        events=[];manager=self.manager(events);self.patches(events,bad_manifest=True)
        manager._monitor('task',SimpleNamespace(pid=21904,wait=lambda **kwargs:0,poll=lambda:0))
        self.assertIn(('publish','failed'),events)
        receipt=next(x[1] for x in events if x[0]=='ledger_finish')
        self.assertEqual(receipt['returncode'],0);self.assertFalse(receipt['uncertain'])

    def test_timeout_cleaned_process_records_actual_failure_before_task(self):
        events=[];manager=self.manager(events);self.patches(events)
        def wait(**kwargs):raise subprocess.TimeoutExpired('MOCK',30)
        manager._terminate_owned=lambda task,process:True
        manager._monitor('task',SimpleNamespace(pid=21904,wait=wait,poll=lambda:1))
        receipt=next(x[1] for x in events if x[0]=='ledger_finish')
        self.assertTrue(receipt['timed_out']);self.assertTrue(receipt['cleanup_confirmed'])
        self.assertEqual(receipt['returncode'],1);self.assertFalse(receipt['uncertain'])
        self.assertIn(('publish','failed'),events)

    def test_timeout_without_proven_exit_keeps_unknown(self):
        events=[];manager=self.manager(events);self.patches(events)
        def wait(**kwargs):raise subprocess.TimeoutExpired('MOCK',30)
        manager._terminate_owned=lambda task,process:False
        manager._monitor('task',SimpleNamespace(pid=21904,wait=wait,poll=lambda:None))
        receipt=next(x[1] for x in events if x[0]=='ledger_finish')
        self.assertIsNone(receipt['returncode']);self.assertTrue(receipt['uncertain'])
        self.assertIn(('publish','uncertain'),events)


if __name__=='__main__':unittest.main()
