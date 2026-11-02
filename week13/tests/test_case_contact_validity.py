import copy
import json
import math
import unittest

from week13.src.case_contact_validity import diagnose_contact_rows, diagnose_contact_source


VALUES = dict(stepH=.3, stepD=.26, n_steps=3, x_start_up=2.,
              x_end_up=2.78, len_flat=2., x_start_down=4.78,
              x_end_down=5.56, H_peak=.9, R_nose=.07, R_main=.127)


def samples(x=0., z=.127, **changes):
    row = dict(time=0.)
    for entity in ('n', 'f', 'r'):
        row.update({'x_' + entity: x, 'z_' + entity: z,
                    'Fz_' + entity: 10., 'Fx_' + entity: 0.})
    row.update(changes)
    return [row, dict(row, time=.05)]


def known_source():
    nose = ('if x_n < x_start_up then 0 '
            'else if x_n < x_end_up then (floor((x_n - x_start_up)/stepD) + 1) * stepH '
            'else if x_n < x_start_down then H_peak '
            'else if x_n < x_end_down then H_peak - (floor((x_n - x_start_down)/stepD) + 1) * stepH '
            'else 0')
    main = ('if x_f < x_start_up then 0 '
            'else if x_f < x_end_up then floor((x_f - x_start_up)/stepD + 1e-3) * stepH '
            'else if x_f < x_start_down then H_peak '
            'else if x_f < x_end_down then max(0, H_peak - floor((x_f - x_start_down)/stepD + 1e-3) * stepH) '
            'else 0')
    return ('model M equation h_n = noEvent(' + nose + '); h_f = noEvent(' + main +
            '); h_r = noEvent(' + main.replace('x_f', 'x_r') + '); end M;')


class ContactValidityChecks(unittest.TestCase):
    def test_no_hard_contradiction_is_never_physical_acceptance(self):
        report = diagnose_contact_rows(samples(), VALUES)
        self.assertEqual(report['status'], 'PASS')
        self.assertEqual(report['conclusion'], 'NO_HARD_CONTRADICTION_OBSERVED')
        self.assertEqual(report['physical_status'], 'UNKNOWN')
        self.assertEqual(report['counters']['sample_count'], 6)

    def test_all_wheels_use_one_solid_not_entity_height_formula(self):
        rows = samples(x=2.1, z=.2, h_n=.3, h_f=0., h_r=0.)
        report = diagnose_contact_rows(rows, VALUES)
        self.assertEqual(report['status'], 'FAIL')
        for entity in ('n', 'f', 'r'):
            self.assertEqual(report['per_entity'][entity]['counters']['center_inside_solid_sample_count'], 2)
        self.assertEqual(report['counters']['legacy_height_disagreement_sample_count'], 4)

    def test_front_rim_intersects_first_riser_before_center_crosses(self):
        report = diagnose_contact_rows(samples(x_n=1.95, z_n=.17, Fz_n=0., Fx_n=0.), VALUES)
        nose = report['per_entity']['n']
        self.assertEqual(report['status'], 'PASS')
        self.assertEqual(nose['counters']['riser_overlap_sample_count'], 2)
        self.assertEqual(nose['counters']['riser_overlap_with_zero_vertical_force_sample_count'], 2)
        self.assertEqual(nose['counters']['overlap_with_zero_reported_vector_sample_count'], 2)
        self.assertAlmostEqual(nose['maximum_boundary_overlap_m'], .02)

    def test_edge_intersection_is_reported_separately(self):
        report = diagnose_contact_rows(samples(x_n=1.96, z_n=.34, Fz_n=0.), VALUES)
        nose = report['per_entity']['n']
        self.assertEqual(nose['counters']['edge_overlap_sample_count'], 2)
        self.assertAlmostEqual(nose['maximum_boundary_overlap_m'], .07 - math.hypot(.04, .04))
        self.assertEqual(report['status'], 'PASS')

    def test_center_inside_is_observation_and_uses_shortest_boundary_distance(self):
        report = diagnose_contact_rows(samples(x_n=2.01, z_n=.2), VALUES)
        self.assertEqual(report['status'], 'PASS')
        self.assertEqual(report['counters']['full_circle_inside_solid_sample_count'], 0)
        evidence = report['per_entity']['n']['evidence']['maximum_center_depth']
        self.assertAlmostEqual(evidence['center_to_boundary_distance_m'], .01)
        self.assertEqual(evidence['nearest_boundary']['kind'], 'riser')
        self.assertEqual(report['physical_status'], 'UNKNOWN')

    def test_small_flat_soft_contact_is_observation(self):
        report = diagnose_contact_rows(samples(z=.12, z_n=.065), VALUES)
        self.assertEqual(report['status'], 'PASS')
        self.assertEqual(report['counters']['center_inside_solid_sample_count'], 0)
        self.assertEqual(report['counters']['tread_overlap_sample_count'], 6)
        self.assertAlmostEqual(report['per_entity']['f']['maximum_boundary_overlap_m'], .007)

    def test_exact_ground_and_riser_boundaries_are_not_strict_interior(self):
        for x, z in ((0., 0.), (2., 0.), (2., .15), (2., .3), (2.26, .3), (2.26, .6)):
            with self.subTest(x=x, z=z):
                self.assertEqual(diagnose_contact_rows(samples(x, z), VALUES)['status'], 'PASS')

    def test_at_riser_below_both_treads_is_interior(self):
        for x, z in ((2., -.01), (2.26, .29), (4.78, .59), (5.3, -.01)):
            with self.subTest(x=x, z=z):
                self.assertEqual(diagnose_contact_rows(samples(x, z), VALUES)['status'], 'PASS')

    def test_ascending_step_count_and_final_high_tread(self):
        for x, z in ((2.01, .29), (2.27, .59), (2.53, .89), (2.77, .89)):
            with self.subTest(x=x, z=z):
                self.assertEqual(diagnose_contact_rows(samples(x, z), VALUES)['status'], 'PASS')
        self.assertEqual(diagnose_contact_rows(samples(2.53, .9), VALUES)['status'], 'PASS')

    def test_platform_uses_peak_for_every_wheel(self):
        self.assertEqual(diagnose_contact_rows(samples(3.2, .89), VALUES)['status'], 'PASS')
        self.assertEqual(diagnose_contact_rows(samples(3.2, .91), VALUES)['status'], 'PASS')

    def test_first_descent_at_start_not_one_tread_later(self):
        self.assertEqual(diagnose_contact_rows(samples(4.77, .8), VALUES)['status'], 'PASS')
        self.assertEqual(diagnose_contact_rows(samples(4.78, .8), VALUES)['status'], 'PASS')
        self.assertEqual(diagnose_contact_rows(samples(4.79, .8), VALUES)['status'], 'PASS')
        self.assertEqual(diagnose_contact_rows(samples(4.79, .59), VALUES)['status'], 'PASS')

    def test_final_descent_and_end_interval_are_ground(self):
        for x in (5.3, 5.31, 5.56, 5.57):
            with self.subTest(x=x):
                self.assertEqual(diagnose_contact_rows(samples(x, .01), VALUES)['status'], 'PASS')
                self.assertEqual(diagnose_contact_rows(samples(x, -.01), VALUES)['status'], 'PASS')

    def test_tangency_is_observed_without_overlap(self):
        report = diagnose_contact_rows(samples(z=.127, z_n=.07), VALUES)
        self.assertEqual(report['counters']['tangent_boundary_sample_count'], 6)
        self.assertEqual(report['counters']['circle_solid_overlap_sample_count'], 0)

    def test_legacy_vertical_delta_is_not_disk_penetration_or_failure_threshold(self):
        report = diagnose_contact_rows(samples(h_n=1., h_f=1., h_r=1.), VALUES)
        self.assertEqual(report['status'], 'PASS')
        self.assertGreater(report['per_entity']['n']['maximum_legacy_vertical_compression_m'], 2 * VALUES['R_nose'])
        self.assertEqual(report['counters']['legacy_height_disagreement_sample_count'], 6)

    def test_no_stair_height_cutoff(self):
        bigger = dict(VALUES, stepH=.8, H_peak=2.4)
        self.assertEqual(diagnose_contact_rows(samples(), bigger)['status'], 'PASS')

    def test_nonfinite_and_boolean_row_fields_are_unknown(self):
        for key, value in (('x_n', math.nan), ('z_r', math.inf), ('time', True), ('Fz_f', '0'), ('h_n', math.nan), ('Fx_n', math.inf)):
            with self.subTest(key=key):
                rows = samples(); rows[0][key] = value
                self.assertEqual(diagnose_contact_rows(rows, VALUES)['status'], 'UNKNOWN')

    def test_missing_mandatory_fields_are_unknown(self):
        for key in ('time', 'x_n', 'z_f', 'Fz_r'):
            rows = samples(); del rows[0][key]
            self.assertEqual(diagnose_contact_rows(rows, VALUES)['status'], 'UNKNOWN')

    def test_bad_and_inconsistent_parameters_fail_closed(self):
        for key, value in (('stepH', 0.), ('stepD', -1.), ('n_steps', 2.5),
                           ('n_steps', 100000000), ('R_main', 0.), ('H_peak', 1.),
                           ('x_end_up', 2.7), ('x_end_down', 5.7), ('len_flat', 1.),
                           ('stepH', math.nan), ('R_nose', True)):
            with self.subTest(key=key, value=value):
                self.assertEqual(diagnose_contact_rows(samples(), dict(VALUES, **{key: value}))['status'], 'UNKNOWN')
        missing = dict(VALUES); del missing['x_end_up']
        self.assertEqual(diagnose_contact_rows(samples(), missing)['status'], 'UNKNOWN')

    def test_invalid_container_insufficient_or_bad_time_is_unknown(self):
        for rows in (None, {}, [], samples()[:1], [None, None]):
            self.assertEqual(diagnose_contact_rows(rows, VALUES)['status'], 'UNKNOWN')
        for times in ((0., 0.), (.1, 0.)):
            rows = samples()
            for row, time in zip(rows, times):
                row['time'] = time
            self.assertEqual(diagnose_contact_rows(rows, VALUES)['status'], 'UNKNOWN')

    def test_equal_event_times_can_precede_later_time(self):
        rows = samples(); rows.insert(0, dict(rows[0]))
        self.assertEqual(diagnose_contact_rows(rows, VALUES)['status'], 'PASS')

    def test_arithmetic_epsilon_does_not_create_a_soft_contact_threshold(self):
        self.assertEqual(diagnose_contact_rows(samples(0., -1e-14), VALUES)['status'], 'PASS')
        self.assertEqual(diagnose_contact_rows(samples(0., -1e-8), VALUES)['status'], 'PASS')

    def test_input_unchanged_deterministic_and_json_finite(self):
        rows = samples(2.01, .2); before = copy.deepcopy(rows); values_before = dict(VALUES)
        a = diagnose_contact_rows(rows, VALUES); b = diagnose_contact_rows(rows, VALUES)
        self.assertEqual(a, b); self.assertEqual(rows, before); self.assertEqual(VALUES, values_before)
        json.dumps(a, allow_nan=False)

    def test_evidence_lists_are_bounded_without_losing_counters(self):
        rows = samples(3.2, .6)
        rows = [dict(rows[0], time=i * .05) for i in range(30)]
        report = diagnose_contact_rows(rows, VALUES)
        self.assertEqual(len(report['hard_contradictions']), 20)
        self.assertEqual(report['counters']['center_inside_solid_sample_count'], 90)

    def test_known_source_height_disagreement_is_a_separate_narrow_diagnostic(self):
        report = diagnose_contact_source(known_source(), VALUES)
        self.assertEqual(report['status'], 'FAIL')
        self.assertEqual(report['conclusion'], 'INCONSISTENT_TERRAIN_FORMULAS')
        self.assertEqual(report['same_global_x_evidence'][0]['front_height_m'], 0.)
        self.assertEqual(report['physical_status'], 'UNKNOWN')

    def test_unknown_source_is_not_claimed_safe(self):
        for source in ('', 'model M end M;', known_source().replace('+ 1e-3', '+ 2e-3'),
                       '"' + known_source() + '"', known_source() + ' h_n=0;'):
            self.assertEqual(diagnose_contact_source(source, VALUES)['status'], 'UNKNOWN')

    def test_finite_inputs_with_overflowed_derived_observation_are_unknown(self):
        report = diagnose_contact_rows(samples(z_n=-1e308, h_n=1e308), VALUES)
        self.assertEqual(report['status'], 'UNKNOWN')
        json.dumps(report, allow_nan=False)

    def test_riser_evidence_identifies_actual_riser_even_when_ground_is_nearer(self):
        report = diagnose_contact_rows(samples(x_n=1.94, z_n=.02), VALUES)
        evidence = report['per_entity']['n']['evidence']['first_riser_overlap']
        self.assertEqual(evidence['nearest_boundary']['kind'], 'tread')
        self.assertEqual(evidence['overlap_boundary']['kind'], 'riser')
        self.assertAlmostEqual(evidence['overlap_boundary']['distance_m'], .06)


if __name__ == '__main__':
    unittest.main()
