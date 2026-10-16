"""Exercise the real owned child admission path; no real subprocess or OMC."""
import unittest
import tempfile
from pathlib import Path
from contextlib import contextmanager
from unittest.mock import patch
from week12.src.car_domain import eightwheel_route as a

class ChildDeadlineRegression(unittest.TestCase):
    def exercise(self, elapsed, whole_vehicle=False, allowed=True):
        route=object.__new__(a.EightwheelRoute)
        route.claim_token='claim';route.task_id='task';route.owner='owner';route.pid=1;route.binding={}
        row=dict(kind='scene',label='one_twin_module_load_cycle-base',status='RUNNING',
                 owner_token='claim',reserved_wall_s=210,processes=[])
        state=dict(status='EXECUTING_EIGHTWHEEL',external_s=0,counts={'scene_children':0},
                   execution_claim=dict(token='claim',task_id='task',owner='owner',pid=1),
                   scene_attempts=[row],operations=[],actual_whole_vehicle_dynamics=whole_vehicle)
        caps=dict(a.CAPS,active_s=100000,analysis_s=100000,external_s=100000)
        result=dict(returncode=0,duration_s=.1,stdout='',stderr='',uncertain=False,
                    interrupted=False,timed_out=False)
        @contextmanager
        def transaction():yield {},state
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(a,'transaction',transaction),patch.object(a,'elapsed',return_value=elapsed), \
                 patch.object(a,'RECOVERY_DEADLINE',6000),patch.object(a,'CAPS',caps), \
                 patch.object(a,'assert_binding'),patch.object(a,'save'), \
                 patch.object(a,'bounded_run',return_value=result) as child:
                if allowed:
                    actual=route.child(('scene_attempts',0),['fixture-only'],Path(folder),60,Path(folder),'build')
                    self.assertEqual(actual['returncode'],0)
                    child.assert_called_once()
                    self.assertEqual(child.call_args.args[2],60)
                    self.assertEqual(state['counts']['scene_children'],1)
                    self.assertEqual(state['operations'][0]['status'],'RETURNED')
                    self.assertAlmostEqual(state['external_s'],.1)
                else:
                    with self.assertRaises(a.Stop):
                        route.child(('scene_attempts',0),['fixture-only'],Path(folder),60,Path(folder),'build')
                    child.assert_not_called()
                    self.assertEqual(state['operations'],[])
                    self.assertEqual(state['counts']['scene_children'],0)
    def test_child_starts_after_old_deadline_before_approved_deadline(self):
        self.exercise(5500)
    def test_child_rejects_exact_approved_deadline(self):
        self.exercise(6000,allowed=False)
    def test_child_preserves_cleanup_reserve_before_deadline(self):
        self.exercise(5996,allowed=False)
    def test_whole_vehicle_milestone_does_not_limit_later_stair_children(self):
        self.exercise(6001,whole_vehicle=True)
