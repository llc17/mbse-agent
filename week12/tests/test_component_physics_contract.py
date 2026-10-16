"""Seven B preparation checks. Contact dynamics checks L05-L08/L10 remain unrun."""
from copy import deepcopy
import hashlib
import math
from pathlib import Path
import unittest

from week12.src.component_physics.contract import (
    ROOT, PROTOCOL_SHA, aggregate_component, fixture, mount_offset, msl_frame_wrench,
    require_dependency_hashes, require_frozen_fixture, shaft_mapping,
    terrain_segments, world_port,
)
from week12.src.component_physics.ledger import Ledger
from week12.src.component_physics.runner import execute_attempt

EVIDENCE = ROOT/'results/s3-b-local-20260922-01'


class BPreparationChecks(unittest.TestCase):
    def test_L01_coordinate_virtual_work(self):
        x, z, pitch = world_port((1, 2, math.pi/2), (3, 0, .1))
        self.assertAlmostEqual(x, 1)
        self.assertAlmostEqual(z, 5)
        self.assertAlmostEqual(pitch, math.pi/2+.1)
        force, moment = msl_frame_wrench(2, 3, 4)
        velocity, angular = (7, 0, 11), (0, -13, 0)
        spatial_power = sum(a*b for a, b in zip(force, velocity))+sum(a*b for a, b in zip(moment, angular))
        self.assertEqual(spatial_power, 99)

    def test_L02_single_to_pair_and_offset(self):
        mapped = aggregate_component(count=2, mass=1, inertia=.005, radius=.1, torque=.1,
                                     power=2, stiffness=50000, damping=100, efficiency=.8)
        self.assertEqual((mapped['mass'], mapped['inertia_yy'], mapped['radius']), (2, .01, .1))
        self.assertEqual((mapped['torque'], mapped['mechanical_power'], mapped['electrical_rating']), (.2, 4, 5))
        self.assertEqual((mapped['stiffness'], mapped['damping']), (100000, 200))
        offset = mount_offset((1, 2, math.pi/2), (1, 3, math.pi/2))
        self.assertAlmostEqual(offset[0], 1)
        self.assertAlmostEqual(offset[1], 0)
        self.assertAlmostEqual(mapped['inertia_yy']+mapped['mass']*(offset[0]**2+offset[1]**2), 2.01)
        with self.assertRaises(ValueError):
            aggregate_component(count=True, mass=1, inertia=.005)

    def test_L03_absolute_relative_speed_and_power(self):
        mapped = shaft_mapping(.4, 3, .2, .7)
        self.assertAlmostEqual(mapped['absolute_roll_speed'], 2.3)
        self.assertAlmostEqual(mapped['joint_torque']*mapped['joint_w'], .6)
        total_power = .2*mapped['absolute_roll_speed']+mapped['carrier_pitch_reaction']*.7
        self.assertAlmostEqual(total_power, mapped['shaft_power'])

    def test_L04_riser_outward_normals(self):
        up = terrain_segments([[-1, 0], [0, 0], [0, .3], [1, .3]])
        down = terrain_segments([[-1, .08], [0, .08], [0, 0], [2, 0]])
        self.assertEqual(up[1]['normal'], [-1, 0])
        self.assertEqual(down[1]['normal'], [1, 0])
        self.assertEqual(up[0]['normal'], [0, 1])
        with self.assertRaises(ValueError):
            terrain_segments([[0, 0], [0, 0]])

    def test_L09_descriptor_change_and_product_rejection(self):
        data = fixture('B03')
        self.assertEqual(require_frozen_fixture(data), data)
        changed = deepcopy(data)
        changed['parameters']['mu'] = .7
        with self.assertRaises(ValueError):
            require_frozen_fixture(changed)
        with self.assertRaises(ValueError):
            require_frozen_fixture(data, product=True)
        with self.assertRaises(ValueError):
            fixture('B07')

    def test_L11_dependency_content_drift(self):
        path = EVIDENCE/'dependency-standin.txt'
        with path.open('xb') as handle:
            handle.write(b'fixed-library-version-and-bytes')
        rows = [{'path': str(path), 'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}]
        require_dependency_hashes(rows)
        path.write_bytes(b'same-version-different-content')
        with self.assertRaises(ValueError):
            require_dependency_hashes(rows)

    def test_L12_reservation_conflict_and_interruption(self):
        ledger = Ledger(EVIDENCE/'synthetic-ledger', test_only=True)
        ledger.prepare(historical_attempts=1)
        with self.assertRaises(FileExistsError):
            ledger.prepare(historical_attempts=1)
        approval = {'purpose': 'b_component_execution', 'receipt_kind': 'synthetic_test',
                    'protocol_file_sha256': PROTOCOL_SHA, 'attempt_limit': 2,
                    'run_order': ['B01-base', 'B01-refined'], 'user_message': 'synthetic_test_not_human_approval'}
        slot = ledger.reserve('B01-base', approval=approval)
        self.assertEqual(slot['cumulative_slot'], 2)
        with self.assertRaises(ValueError):
            ledger.reserve('B01-refined', approval=approval)
        ledger.finish(1, 'INTERRUPTED', simulation_started=False, evidence='synthetic-no-process')
        with self.assertRaises(ValueError):
            ledger.reserve('B01-refined', approval=approval)
        with self.assertRaises(ValueError):
            ledger.finish(1, 'PASS')
        with self.assertRaises(ValueError):
            execute_attempt(ledger, 'B01', refined=False, approval=approval, frozen_sources=[], batch_started=0)
