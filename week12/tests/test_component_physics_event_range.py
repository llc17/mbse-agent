"""Regression checks for the interior-only vertex activation range; no OMC."""
import math
import re
import unittest
from unittest import mock

from week12.tests import modelica_scalar_harness as scalar


UP = [(-1, 0), (0, 0), (0, .3), (1, .3)]
KINDS = [0, -1, 1, 0]


class BEventRangeChecks(unittest.TestCase):
    def test_protocol_unilateral_law_at_event_tolerance_boundary(self):
        # Evaluate actual active-branch expressions with the old event relation
        # still active just outside contact. This is not a DAE/event simulator.
        source = scalar.section('PolylineWheelContact')
        for suffix in ('Face', 'Vertex'):
            statements = {}
            for prefix in ('delta', 'fe', 'fd', 'fn', 'dn'):
                name = prefix+suffix
                statements[name] = re.search(r'\b'+name+r'\[i\] := ([^;]+);', source).group(1)
            for separation, speed in ((1e-10, .3365), (1.1e-10, -.3365), (0, .1), (-.001, -.1)):
                env = {'i': 1, 'R': .1, 'k': 100000., 'c': 200.,
                       'distance'+suffix: {1: .1+separation}, 'vn'+suffix: {1: speed}}
                for name, expression in statements.items():
                    env[name] = {1: scalar.expression(expression, env)}
                self.assertGreaterEqual(env['delta'+suffix][1], 0)
                self.assertGreaterEqual(env['fn'+suffix][1], 0)
                self.assertGreaterEqual(env['dn'+suffix][1], 0)
                if separation >= 0:
                    for name in statements:
                        self.assertEqual(env[name][1], 0)
                else:
                    self.assertAlmostEqual(env['delta'+suffix][1], .001)
                    self.assertAlmostEqual(env['fe'+suffix][1], 100)

    def test_fixed_internal_endpoint_and_empty_interior_range(self):
        source = scalar.section('PolylineWheelContact')
        activation = source.index('for i in 2:nPoints - 1 loop')
        self.assertLess(source.index('vertexActive[i] := false;'), activation)
        self.assertGreater(source.index('if vertexActive[i] then'), activation)

        internal = scalar.contact(UP, KINDS, x=-.099, z=.3)
        self.assertEqual((internal['contactCount'], internal['featureId']), (1, 203))
        self.assertAlmostEqual(internal['normal'], 100, places=8)
        self.assertAlmostEqual(internal['Fx'], -100, places=8)
        self.assertAlmostEqual(internal['Fz'], 0, places=8)
        self.assertEqual(
            [internal['vertexActive'][i] for i in range(1, 5)],
            [False, False, True, False],
        )

        points, kinds = [(-1, 0), (1, 0)], [1, 1]
        endpoint = scalar.contact(points, kinds, x=-1, z=.099)
        self.assertEqual((endpoint['contactCount'], endpoint['normal']), (0, 0))
        flat = scalar.contact(points, kinds, x=0, z=.099)
        self.assertEqual((flat['contactCount'], flat['featureId']), (1, 101))
        self.assertAlmostEqual(flat['normal'], 100, places=8)
        self.assertEqual([flat['vertexActive'][i] for i in (1, 2)], [False, False])

        unsafe = source.replace(
            'for i in 2:nPoints - 1 loop', 'for i in 1:nPoints loop', 1
        )
        with mock.patch.object(scalar, 'section', return_value=unsafe):
            with self.assertRaisesRegex(IndexError, r'index 0 outside 1\.\.1'):
                scalar.contact(points, kinds, x=-1, z=.099)

    def test_existing_contact_semantics_at_fixed_boundaries(self):
        root2 = math.sqrt(2)
        for nx, nz in ((-1/root2, 1/root2), (-1, 0), (0, 1)):
            state = scalar.contact(UP, KINDS, x=.099*nx, z=.3+.099*nz)
            self.assertEqual((state['contactCount'], state['featureId']), (1, 203))
            self.assertAlmostEqual(state['normal'], 100, places=8)
            self.assertAlmostEqual(state['Fx'], 100*nx, places=8)
            self.assertAlmostEqual(state['Fz'], 100*nz, places=8)

        concave = scalar.contact(UP, KINDS, x=-.099, z=.099)
        self.assertEqual(concave['contactCount'], 2)
        self.assertTrue(all(value == 0 for value in concave['fnVertex'].values()))
        shared = scalar.contact(
            [(-1, 0), (0, 0), (1, 0)], [0, 0, 0], x=0, z=.099
        )
        self.assertEqual((shared['contactCount'], shared['featureId']), (1, 102))

        points, kinds = [(-2, 0), (2, 0)], [0, 0]
        separated = scalar.contact(
            points, kinds, x=0, z=.2, vx=10, vz=-10, omega=100, mu=.6
        )
        for name in ('normal', 'normalLossPower', 'frictionLossPower', 'elasticEnergy'):
            self.assertEqual(separated[name], 0)
        loading = scalar.contact(points, kinds, x=0, z=.099, vx=.01, vz=-10, mu=.6)
        unloading = scalar.contact(points, kinds, x=0, z=.099, vx=.01, vz=10, mu=.6)
        self.assertAlmostEqual(loading['normal'], 200, places=8)
        self.assertAlmostEqual(unloading['normal'], 0, places=8)
        self.assertAlmostEqual(loading['elasticEnergy'], .05, places=10)
        for state in (loading, unloading):
            self.assertGreaterEqual(state['normalLossPower'], 0)
            self.assertGreaterEqual(state['frictionLossPower'], 0)


if __name__ == '__main__':
    unittest.main()
