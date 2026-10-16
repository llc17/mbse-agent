"""Four source/static checks for the exact dimension revision; no tool execution.

This file is supplied for a future authorized local-check batch.  Preparation
may AST-parse this file, but must not run or claim these tests as passed.
"""
from copy import deepcopy
import hashlib
import math
from types import FunctionType
import unittest
from unittest.mock import patch

from week12.src.composition.schema import digest, seal_design
from week12.src.integration import dimension_revision as revision
from week12.src.integration import model_generator as original


class DimensionRevisionTests(unittest.TestCase):
    def test_exact_candidate_preserves_c2_and_tasks(self):
        bundle, contract = revision.build_draft()
        old_bundle, old_contract = revision._seed()
        report = revision.validate(bundle, contract)
        design, physical = bundle['design'], bundle['physical']
        self.assertFalse(report['authorized_execution'])
        self.assertFalse(design['confirmation']['human_confirmed'])
        for key in ('contact_per_physical_wheel', 'wheel_pair_lowering', 'controller',
                    'initialization', 'rolling_resistance', 'supply', 'cases', 'user_facts'):
            self.assertEqual(physical[key], old_bundle['physical'][key])
        for key in ('physical', 'numerical', 'numerical_comparison'):
            self.assertEqual(contract[key], old_contract[key])
        self.assertEqual(physical['wheel_pair_lowering']['damping_Ns_m'], 200)
        self.assertEqual(contract['case_order'], list(revision.CASE_ORDER))
        self.assertEqual(len(physical['cases']), 7)
        self.assertEqual(contract['budget_proposal']['maximum_scene_attempts'], 12)
        self.assertEqual(contract['budget_proposal']['new_local_execution_checks_limit'], 24)
        self.assertEqual(contract['budget_proposal']['execution_total_wall_s'], 6000)
        self.assertNotIn('source_mapping_digest', physical)
        self.assertEqual(physical['source_history']['source_mapping_digest'], old_bundle['physical']['source_mapping_digest'])

    def test_mass_geometry_closure_and_inertia_bounds(self):
        bundle, contract = revision.build_draft()
        report = revision.validate(bundle, contract)
        self.assertEqual(report['mass_kg'], 85)
        self.assertEqual(report['payload_kg'], 20)
        self.assertEqual(report['wheel_and_drive_mass_kg'], 26)
        for value, target in zip(report['nominal_com_xz_m'], (.24, .26)):
            self.assertAlmostEqual(value, target, places=12)
        wheels = {w['id']: w for w in bundle['design']['wheels']}
        self.assertAlmostEqual(wheels['front']['pose']['x']['value'], 2*math.hypot(.28, .15), places=12)
        self.assertAlmostEqual(wheels['nose']['pose']['x']['value']-wheels['front']['pose']['x']['value'], .330, places=12)
        self.assertTrue(all(w['radius']['value'] == .16 and w['pose']['z']['value'] == .16 for w in wheels.values()))
        self.assertAlmostEqual(min(row['clear_gap_m'] for row in report['wheel_disk_clearances']), .010, places=12)
        self.assertTrue(all(row['maximum_error_m_or_rad'] <= 1e-12 for row in report['frame_connection_closure']))
        self.assertTrue(all(row['J_per_instance_kg_m2'] <= row['necessary_upper_bound_kg_m2'] for row in report['inertia_necessary_bounds']))
        self.assertAlmostEqual(report['reference']['nonwheel_initial_min_clearance_m'], .11)
        self.assertLess(report['initial_rightmost_wheel_disk_x_m'], report['first_riser_x_m'])
        self.assertEqual(report['derived_wheel_penetration_limit_m'], .02)
        self.assertTrue(report['unchanged_penetration_rule'])

    def test_isolated_generation_keeps_original_sources_and_globals(self):
        paths = [revision.SEED_DIRECTORY/name for name in revision.SEED_HASHES]
        paths.extend([original.B_SOURCE, revision.vehicle.CANDIDATE,
                      revision.ROOT/'src/integration/model_generator.py',
                      revision.ROOT/'src/integration/vehicle_amendment.py'])
        before_files = {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        bindings = ('VERSION', 'SPECIFICATION_DIGEST', 'DESIGN_DIGEST', 'PHYSICAL_DIGEST', 'VERIFICATION_DIGEST')
        before_globals = {name: getattr(original, name) for name in bindings}
        before_functions = {name: fn for name, fn in vars(original).items() if isinstance(fn, FunctionType)}
        bundle, contract = revision.build_draft()
        for case_id in revision.CASE_ORDER:
            generated = revision.generate(bundle, contract, case_id, '1'*64)
            trace = generated['trace']
            self.assertEqual(trace['frozen']['design_digest'], bundle['design']['design_digest'])
            self.assertEqual(trace['frozen']['physical_digest'], bundle['physical']['physical_digest'])
            self.assertEqual(trace['frozen']['verification_digest'], digest(contract))
            self.assertEqual(trace['frozen']['specification_digest'], '1'*64)
            self.assertEqual(trace['frozen']['contact_source_sha256'], revision.vehicle.C2_SHA)
            self.assertEqual(generated['modelica'].count('R='+original._f(.16)+','), 3)
            self.assertEqual(generated['modelica'].count('k=100000.0, c=200.0'), 3)
            self.assertIn('specification_digest='+'1'*64, generated['sysml'])
            self.assertTrue(trace['sysml_geometry_changed_requires_new_parse'])
            self.assertFalse(trace['executable_artifacts_run'])
        self.assertEqual(before_globals, {name: getattr(original, name) for name in bindings})
        for name, function in before_functions.items():
            self.assertIs(function, getattr(original, name))
        self.assertEqual(before_files, {str(path): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths})

    def test_rejects_rehashed_changes_and_never_executes(self):
        bundle, contract = revision.build_draft()
        changed = deepcopy(bundle)
        changed['design']['wheels'][0]['radius']['value'] = .161
        changed['design'] = seal_design(changed['design'])
        with self.assertRaisesRegex(ValueError, 'EXACT_DIMENSION'):
            revision.generate(changed, contract, revision.CASE_ORDER[0], '1'*64)
        changed_contract = deepcopy(contract)
        changed_contract['numerical']['base']['tolerance'] *= 10
        with self.assertRaisesRegex(ValueError, 'EXACT_DIMENSION'):
            revision.validate(bundle, changed_contract)
        with self.assertRaisesRegex(ValueError, 'EXACT_TWO'):
            revision.generate(bundle, contract, 'fixed_nose_three_down', '1'*64)
        with self.assertRaisesRegex(ValueError, 'SPECIFICATION_DIGEST'):
            revision.generate(bundle, contract, revision.CASE_ORDER[0], 'not-a-digest')
        with patch('subprocess.Popen', side_effect=AssertionError('no child process')) as popen, \
                patch('subprocess.run', side_effect=AssertionError('no subprocess')) as run:
            revision.generate(bundle, contract, revision.CASE_ORDER[0], '2'*64)
            popen.assert_not_called()
            run.assert_not_called()
        self.assertFalse(hasattr(revision, 'prepare'))
        self.assertFalse(hasattr(revision, 'execute'))


if __name__ == '__main__':
    unittest.main()
