"""The five remaining fixed local checks; never invoke a Modelica tool."""
from copy import deepcopy
import math
import unittest

from week12.src.component_physics.contract import fixture, require_frozen_fixture
from week12.tests.modelica_scalar_harness import contact, drive

UP = [(-1, 0), (0, 0), (0, .3), (1, .3)]
KINDS = [0, -1, 1, 0]


class BRemainingChecks(unittest.TestCase):
    def test_L05_convex_unique_feature(self):
        # Middle and both boundaries of the upper-left convex normal sector.
        for nx, nz in ((-1/math.sqrt(2), 1/math.sqrt(2)), (-1, 0), (0, 1)):
            s = contact(UP, KINDS, x=.099*nx, z=.3+.099*nz)
            self.assertEqual(s['contactCount'], 1)
            self.assertEqual(s['featureId'], 203)
            self.assertAlmostEqual(s['normal'], 100, places=8)
            self.assertAlmostEqual(s['Fx'], 100*nx, places=8)
            self.assertAlmostEqual(s['Fz'], 100*nz, places=8)
        # Outside that sector only the adjacent finite upper face can act.
        s = contact(UP, KINDS, x=.02, z=.399)
        self.assertEqual((s['contactCount'], s['featureId']), (1, 103))
        self.assertEqual(s['Fx'], 0)

    def test_L06_concave_and_endpoint(self):
        s = contact(UP, KINDS, x=-.099, z=.099)
        self.assertEqual(s['contactCount'], 2)
        self.assertAlmostEqual(s['Fx'], -100, places=8)
        self.assertAlmostEqual(s['Fz'], 100, places=8)
        self.assertTrue(all(value == 0 for value in s['fnVertex'].values()))
        # At a collinear shared endpoint exactly the outgoing face owns contact.
        s = contact([(-1, 0), (0, 0), (1, 0)], [0, 0, 0], x=0, z=.099)
        self.assertEqual((s['contactCount'], s['featureId']), (1, 102))
        self.assertAlmostEqual(s['Fz'], 100, places=8)

    def test_L07_no_contact_no_tension_dissipation(self):
        points, kinds = [(-2, 0), (2, 0)], [0, 0]
        separated = contact(points, kinds, x=0, z=.2, vx=10, vz=-10, omega=100, mu=.6)
        for key in ('Fx', 'Fz', 'normal', 'normalLossPower', 'frictionLossPower', 'elasticEnergy'):
            self.assertEqual(separated[key], 0)
        # 1 mm compression stores 0.05 J; bounded damping gives 200 N loading,
        # 0 N unloading, never tension. Fast slip remains dissipative.
        loading = contact(points, kinds, x=0, z=.099, vx=.01, vz=-10, mu=.6)
        unloading = contact(points, kinds, x=0, z=.099, vx=.01, vz=10, mu=.6)
        self.assertAlmostEqual(loading['normal'], 200, places=8)
        self.assertAlmostEqual(unloading['normal'], 0, places=8)
        self.assertAlmostEqual(loading['elasticEnergy'], .05, places=10)
        for state in (loading, unloading):
            self.assertGreaterEqual(state['normalLossPower'], 0)
            self.assertGreaterEqual(state['frictionLossPower'], 0)
            power = state['Fx']*.01 + state['Fz']*state['vzCenter']
            stored_rate = 100000*.001*(-state['vzCenter'])
            self.assertAlmostEqual(power+stored_rate+state['normalLossPower']+state['frictionLossPower'], 0, places=7)

    def test_L08_drive_brake_energy(self):
        for torque, omega, shaft, dc, loss in ((.2, 20, 4, 5, 1), (-.2, 20, -4, 0, 4), (0, 20, 0, 0, 0)):
            s = drive(torque, omega)
            self.assertEqual((s['shaftPower'], s['dcPower'], s['driveLoss']), (shaft, dc, loss))
            self.assertEqual(s['der_energyAvailable'], -dc)
            self.assertEqual(s['der_driveDissipatedEnergy'], loss)
            self.assertAlmostEqual(s['busCurrent'], dc/24)
            self.assertEqual(dc, shaft+loss)
        for torque, speed, available in ((.201, 0, 100), (.2, 21, 100), (0, 0, -1)):
            with self.assertRaises(ValueError):
                drive(torque, speed, available=available)

    def test_L10_unsupported_and_inside_terrain(self):
        # The B boundary accepts only the frozen planar, open-chain fixtures.
        for field, value in (('planar_axis', [0, 0, 1]), ('connections', [{'closed_loop': True}])):
            data = deepcopy(fixture('B02'))
            data[field] = value
            with self.assertRaises(ValueError):
                require_frozen_fixture(data)
        for x, z in ((0, -.01), (.2, .2)):
            with self.assertRaisesRegex(ValueError, 'inside terrain'):
                contact(UP, KINDS, x=x, z=z)
        data = deepcopy(fixture('B02'))
        data['parameters']['z0'] = -.01
        with self.assertRaises(ValueError):
            require_frozen_fixture(data)
