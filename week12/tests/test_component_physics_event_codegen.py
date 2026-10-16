import unittest
from week12.src.component_physics.contract import ROOT
from week12.src.component_physics.event_codegen import audit_source


class EventCodegenChecks(unittest.TestCase):
    def test_detect_recorded_alias_and_accept_distinct_slots(self):
        old = ROOT/'results/s3-b-auto-repair-20260923-01/round-02/runs/B06-base/model_05evt.c'
        actual = audit_source(old.read_text(encoding='utf-8'))
        self.assertEqual(actual['status'], 'FAIL')
        self.assertIn('Relation cache 19 aliases distinct comparison operands', actual['issues'])
        template = 'LessZC(x[{i}], R, scale, zero, data->simulationInfo->storedRelations[{slot}]);'
        distinct = '\n'.join(template.format(i=i, slot=i+19) for i in range(3))
        self.assertEqual(audit_source(distinct)['status'], 'PASS')
        self.assertEqual(audit_source(distinct+'\n'+distinct)['status'], 'PASS')
        self.assertEqual(audit_source('')['status'], 'FAIL')
