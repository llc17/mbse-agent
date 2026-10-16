import copy
import unittest
from week12.src.active_cluster.execution import DEFAULT_OUT, read, Stop
from week12.src.active_cluster.vehicle_recovery import validate, extra_process

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.p=read(DEFAULT_OUT/'vehicle-recovery-proposal.json')
        self.a=read(DEFAULT_OUT/'vehicle-recovery-authorization.json')
        self.b=read(DEFAULT_OUT/'ledger-before-vehicle-recovery.json')
        self.d=copy.deepcopy(self.b)
    def test_valid_original_history(self):
        validate(self.p,self.a,self.b,self.d)
    def test_missing_receipt(self):
        with self.assertRaises(Stop):validate(self.p,{},self.b,self.d)
    def test_stale_receipt(self):
        self.a['proposal_sha256']='bad'
        with self.assertRaises(Stop):validate(self.p,self.a,self.b,self.d)
    def test_history_deletion(self):
        self.d['scene_attempts']=[]
        with self.assertRaises(Stop):validate(self.p,self.a,self.b,self.d)
    def test_cap_inflation(self):
        self.d['future_cap_s']+=1
        with self.assertRaises(Stop):validate(self.p,self.a,self.b,self.d)
    def test_fourth_repair(self):
        self.d['repairs'].append({})
        with self.assertRaises(Stop):validate(self.p,self.a,self.b,self.d)
    def test_no_diagnostic_time_omission(self):
        self.d['vehicle_recovery']={'status':'CLOSED','parent_overhead_s':0}
        self.assertAlmostEqual(extra_process(self.d),7.4799357)
    def test_no_child_double_count(self):
        leaf=sum(p.get('duration_s',0) for r in self.d['scene_attempts'] for p in r.get('processes',[]))
        self.d['vehicle_recovery']={'status':'DISPATCHING','old_process_before':0,'scene_leaf_before':leaf-10}
        self.assertAlmostEqual(extra_process(self.d,12),9.4799357)
