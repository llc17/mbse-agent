import math
from pathlib import Path
import re
import unittest
from unittest.mock import patch

from week12.tests import modelica_scalar_harness as old
from week12.tests.modelica_equation_harness import contact, equation, evaluate
from week12.src.component_physics.contract import ROOT

UP = [(-1, 0), (0, 0), (0, .3), (1, .3)]
KINDS = [0, -1, 1, 0]


class ScalarEventChecks(unittest.TestCase):
    def test_flat_unilateral_and_low_platform_landing(self):
        s = contact([(-2, 0), (2, 0)], [0, 0], x=0, z=.099, vz=-.1)
        self.assertAlmostEqual(s['normal'], 120)
        self.assertAlmostEqual(s['normalLossPower'], 2)
        self.assertAlmostEqual(s['elasticEnergy'], .05)
        points = [(-1, .08), (0, .08), (0, 0), (2, 0)]
        # Recorded B06 failure state: the low face must exert an upward force.
        x, z, vz = .2772462979108173, .0869276489829514, -1.349889986974858
        s = contact(points, [0, 1, -1, 0], x=x, z=z, vx=2.000336595733232, vz=vz, omega=20, mu=.6)
        self.assertEqual((s['featureId'], s['contactCount']), (103, 1))
        self.assertAlmostEqual(s['Fz'], 100000*(.1-z)-200*vz, places=8)
        self.assertGreater(s['Fz'], 1500)
        source = old.section('PolylineWheelContact')
        for suffix in ('Face', 'Vertex'):
            env = {'i': 1, 'R': .1, suffix.lower()+'Active': {1: True}, 'distance'+suffix: {1: .1000000001}}
            self.assertEqual(evaluate(equation(source, 'delta'+suffix, 'i'), env), 0)

    def test_corners_free_flight_and_old_source_equivalence(self):
        baseline = (ROOT/'results/s3-b-auto-repair-20260923-02/before/package.mo').read_text(encoding='utf-8')
        old_contact = re.search(r'  model PolylineWheelContact\b(.*?)  end PolylineWheelContact;', baseline, re.S).group(1)
        cases = [(UP, KINDS, {'x': -.099, 'z': .3}),
                 (UP, KINDS, {'x': -.099/math.sqrt(2), 'z': .3+.099/math.sqrt(2)}),
                 (UP, KINDS, {'x': 0, 'z': .399}),
                 (UP, KINDS, {'x': -.099, 'z': .099}),
                 ([(-1, 0), (0, 0), (1, 0)], [0, 0, 0], {'x': 0, 'z': .099}),
                 ([(-1, .08), (0, .08), (0, 0), (2, 0)], [0, 1, -1, 0], {'x': .15, 'z': .16, 'vx': 2, 'vz': -.7, 'omega': 20, 'mu': .6})]
        names = ('Fx', 'Fz', 'normal', 'penetration', 'slip', 'contactCount', 'featureId', 'elasticEnergy', 'normalLossPower', 'frictionLossPower', 'contactTorqueY')
        for points, kinds, state in cases:
            current = contact(points, kinds, **state)
            with patch.object(old, 'section', return_value=old_contact):
                prior = old.contact(points, kinds, **state)
            for name in names:
                self.assertAlmostEqual(current[name], prior[name], places=9, msg=name)
        self.assertEqual(contact(UP, KINDS, x=-.099, z=.099)['contactCount'], 2)
        air = contact(cases[-1][0], cases[-1][1], **cases[-1][2])
        self.assertEqual((air['normal'], air['contactCount'], air['elasticEnergy']), (0, 0, 0))

    def test_unchanged_other_classes_and_event_preserving_structure(self):
        source = (ROOT/'models/components/V11PhysicalB1/package.mo').read_text(encoding='utf-8')
        prior = (ROOT/'results/s3-b-auto-repair-20260923-02/before/package.mo').read_text(encoding='utf-8')
        pattern = r'  model PolylineWheelContact\b.*?  end PolylineWheelContact;'
        self.assertEqual(re.sub(pattern, '', source, flags=re.S), re.sub(pattern, '', prior, flags=re.S))
        block = re.search(pattern, source, re.S).group()
        self.assertNotIn('  algorithm', block)
        self.assertNotIn('noEvent', block)
        self.assertNotRegex(block, r'\[0:|Prefix\[0\]|(?:Face|Vertex)\[0\]')
        assertions = r'assert\(.*?\);'
        self.assertEqual(re.findall(assertions, block, re.S), re.findall(assertions, re.search(pattern, prior, re.S).group(), re.S))
        # Endpoint activation is explicitly false; the interior loop remains generic.
        self.assertIn('for i in 2:nPoints - 1 loop', block)
        self.assertEqual(equation(block, 'vertexActive', '1'), 'false')
        self.assertEqual(equation(block, 'vertexActive', 'nPoints'), 'false')
