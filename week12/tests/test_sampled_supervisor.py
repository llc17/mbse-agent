import json
import re
import unittest
from pathlib import Path
from week12.src.active_cluster.modelica import lower
from week12.src.active_cluster.verify import _supervisor_frame, EvidenceMissing

ROOT = Path(__file__).resolve().parents[2]
CAND = ROOT / 'week12/results/s3-c-active-eightwheel-execution-20261009-01/eightwheel-signed-work-execution-20261010/module-event-candidate-02'


class SampledSupervisorTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.design = json.loads((CAND / 'design.json').read_bytes())
        cls.original = (CAND / 'V11GeneratedActive.mo').read_text(encoding='utf-8')

    def test_existing_profile_is_byte_identical(self):
        text, contract = lower(self.design)
        self.assertEqual(text, self.original)
        self.assertEqual(json.loads(json.dumps(contract)), json.loads((CAND / 'output-contract.json').read_bytes()))

    def test_new_control_requires_explicit_accepted_binding(self):
        for control in ({}, {'schema':'v11-approved-sampled-supervisor-1','period_s':.02,'accepted':False,'authorization_sha256':'0'*64}):
            with self.assertRaises(ValueError): lower(self.design, control)

    def test_sampled_control_preserves_continuous_plant_and_observes_intertick_events(self):
        text, contract = lower(self.design, dict(schema='v11-approved-sampled-supervisor-1',period_s=.02,accepted=True,authorization_sha256='0'*64))
        # Compare actual connection and motor command equations, independently
        # of new supervisory bookkeeping. No contact/drive source is altered.
        plant = lambda s: re.findall(r'^.*(?:connect\(|\.command=|FeatureContact |C\.MotorDrive ).*$', s, re.M)
        self.assertEqual(plant(text), plant(self.original))
        for name, scene in contract['scenes'].items():
            if name in ('active_joint_energy','flat_energy_and_brake'):
                self.assertNotIn('sampled_supervisor', scene)
                continue
            self.assertEqual(scene['sampled_supervisor']['period_s'], .02)
            for key in ('loadLossCount','loadLossTime','loadedSince','failureSeen','failureTime','snapSegment','decisionTime'):
                self.assertIn(key, scene['sampled_supervisor']['columns'])
        self.assertEqual(text.count('when {change(segment),change(loadOK)}'), 5)
        self.assertEqual(text.count('elsewhen failed then failureSeen:=true'), 5)
        self.assertEqual(text.count('if pre(supervisorTick)<integer(floor(time/0.02+0.5)) then'), 5)
        self.assertNotIn('delay(', text)

    def replay_fixture(self, target=False):
        class Harness:
            def __init__(self): self.failures=[]
            def check(self, code, okay, detail=None):
                if not okay: self.failures.append(code)
        keys=('supervisorTick snapSegment snapPaused snapTracking snapSupport snapSaturated snapFailed snapFailureSeen snapOwner snapOwner2 snapForce snapForce2 snapClock snapLoadedSince snapLoad snapGuard snapEndpoint decisionTime stageEntryTime stageEntryCount failureSeen failureTime failureCount lastFailureEventTime loadLossTime loadGainTime loadLossCount loadGainCount loadedSince saturated referenceClock').split()
        columns={key:key for key in keys}
        local={'columns':{key:key for key in 'segment paused task_done failure_code load_ok tracking_ok old_support_ok failed'.split()}}
        targets=[dict(wheel='F_A',face=1,tread=1)] if target else []
        info=dict(direction='up',reference_segments=[dict(start=0,end=1,targets=targets)],features=[dict(kind='face',owner_id='F_A',index=1,point=[0,.15],normal=[0,1],columns={'Fn':'Fn'})] if target else [],sampled_supervisor=dict(columns=columns,initial_segment=1,stage_count=1))
        base={key:0. for key in keys}
        base.update(time=0.,segment=1.,paused=1.,task_done=0.,failure_code=0.,load_ok=1.,tracking_ok=1.,old_support_ok=1.,failed=0.,loadedSince=0.,loadGainTime=0.,loadLossTime=-1.,failureTime=-1.,lastFailureEventTime=-1.,Fn=50.)
        return Harness(),local,info,base

    def test_forged_snapshot_is_rejected(self):
        s,local,info,base=self.replay_fixture()
        _supervisor_frame(s,dict(base),local,info)
        left=dict(base,time=.02,referenceClock=.02)
        _supervisor_frame(s,left,local,info)
        right=dict(left,supervisorTick=1.,decisionTime=.02,snapSegment=1.,snapPaused=1.,snapTracking=1.,snapSupport=1.,snapLoad=1.,snapGuard=1.,snapClock=.02,snapLoadedSince=.01,paused=0.)
        _supervisor_frame(s,right,local,info)
        self.assertIn('supervisor_raw_left_binding_snapLoadedSince',s.failures)

    def test_intertick_loss_with_unreset_timer_is_rejected(self):
        s,local,info,base=self.replay_fixture(True)
        for row in (dict(base),dict(base,time=.011,load_ok=0.,Fn=10.),dict(base,time=.015,load_ok=1.,Fn=50.),dict(base,time=.02,referenceClock=.02)):
            _supervisor_frame(s,row,local,info)
        self.assertIn('supervisor_raw_event_reconstruction_loadLossCount',s.failures)
        self.assertIn('supervisor_raw_event_reconstruction_loadedSince',s.failures)

    def test_regular_grid_without_tick_left_evidence_is_rejected(self):
        s,local,info,base=self.replay_fixture()
        _supervisor_frame(s,base,local,info)
        with self.assertRaises(EvidenceMissing):
            _supervisor_frame(s,dict(base,time=.02,supervisorTick=1.,decisionTime=.02),local,info)


if __name__ == '__main__': unittest.main()
