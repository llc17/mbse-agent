"""Finite convention adaptation; no product calls or Modelica execution."""
import csv
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from week13.src.case_checks import (
    REQUIRED, REVIEWED_CONTACT_FAMILY, assess, assess_rows, read_rows,
    terrain_binding,
)
from week13.tests.test_case_contact_validity import VALUES, known_source, samples


SUPPORT = dict(family=REVIEWED_CONTACT_FAMILY,
               source_review_sha256='review-fixture',
               approved_package_sha256='approval-fixture')
SOURCE = 'model ArithmeticFixture ' + ''.join(
    'parameter Real ' + k + '=' + str(v) + ';' for k, v in VALUES.items()
) + ' end ArithmeticFixture;'


def terrain_rows(points):
    return [dict(time=i * .05, **{
        key + e: value for e in ('n', 'f', 'r')
        for key, value in (('x_', x), ('h_', h))
    }) for i, (x, h) in enumerate(points)]


def write_csv(path, rows, optional=()):
    with path.open('w', encoding='utf8', newline='') as handle:
        writer=csv.DictWriter(handle, fieldnames=['time', *REQUIRED, *optional])
        writer.writeheader(); writer.writerows(rows)


class ReviewedContactConventionChecks(unittest.TestCase):
    def binding(self, rows, source=SOURCE, support=SUPPORT):
        with patch('week13.src.case_contact_family.support_for_reviewed_source', return_value=support):
            return terrain_binding(rows, VALUES, source)

    def test_registered_common_height_is_identical_for_every_entity(self):
        report=self.binding(terrain_rows([(0., 0.), (2.13, .3), (2.39, .6),
                                         (2.65, .9), (3.5, .9), (4.91, .6),
                                         (5.17, .3), (5.43, 0.), (6., 0.)]))
        self.assertEqual(report['status'], 'PASS')
        self.assertTrue(report['same_solid_for_all_entities'])
        self.assertEqual(report['source_family'], REVIEWED_CONTACT_FAMILY)
        self.assertEqual(report['source_review_sha256'], 'review-fixture')
        self.assertAlmostEqual(report['final_down_riser_x_m'], 5.3)
        self.assertEqual(report['route_exit_x_m'], VALUES['x_end_down'])

    def test_new_family_does_not_accept_old_main_wheel_one_step_delay(self):
        rows=terrain_rows([(2.13, .3)]); rows[0]['h_f']=0.
        report=self.binding(rows)
        self.assertEqual(report['status'], 'FAIL')
        self.assertEqual(report['mismatches'][0]['entity'], 'f')

    def test_new_family_does_not_accept_any_intermediate_height_at_riser(self):
        for h in (0., .3):
            self.assertEqual(self.binding(terrain_rows([(2., h)]))['status'], 'PASS')
        self.assertEqual(self.binding(terrain_rows([(2., .15)]))['status'], 'FAIL')

    def test_all_down_stair_intervals_end_at_ground_before_route_exit(self):
        for x in (5.3, 5.31, VALUES['x_end_down'], 5.8):
            self.assertEqual(self.binding(terrain_rows([(x, 0.)]))['status'], 'PASS')
        self.assertEqual(self.binding(terrain_rows([(5.43, .3)]))['status'], 'FAIL')

    def test_stair_size_is_not_an_acceptance_cutoff(self):
        for h, d in ((.15, .28), (.18, .26), (.3, .26)):
            values=dict(VALUES, stepH=h, stepD=d, H_peak=3*h,
                        x_end_up=2.+3*d, x_start_down=4.+3*d,
                        x_end_down=4.+6*d)
            with patch('week13.src.case_contact_family.support_for_reviewed_source', return_value=SUPPORT):
                report=terrain_binding(terrain_rows([(2.+d/2,h)]),values,SOURCE)
            self.assertEqual(report['status'], 'PASS')

    def test_unknown_source_and_free_text_claim_do_not_become_registered_family(self):
        source=SOURCE + ' // source_review_bound_stair_contact_v1'
        self.assertEqual(self.binding(terrain_rows([(2.13,.3)]), source, None)['status'], 'UNKNOWN')

    def test_legacy_source_retains_original_split_height_conventions(self):
        row=terrain_rows([(2.13,.3)])[0]; row.update(h_f=0., h_r=0.)
        report=self.binding([row], known_source(), None)
        self.assertEqual(report['status'], 'PASS')
        self.assertEqual(report['source_family'], 'inherited-split-height-formulas')
        self.assertEqual(terrain_binding([row],VALUES)['status'], 'PASS')

    def test_registration_drift_is_not_treated_as_legacy(self):
        with patch('week13.src.case_contact_family.support_for_reviewed_source', side_effect=ValueError('review receipt drift')):
            with self.assertRaisesRegex(ValueError,'drift'):
                terrain_binding(terrain_rows([(2.13,.3)]), VALUES, SOURCE)

    def test_reviewed_limitations_acknowledge_contact_but_never_certify_physics(self):
        report=assess_rows(samples(),VALUES,source_family=REVIEWED_CONTACT_FAMILY)
        self.assertEqual(report['physical_status'],'UNKNOWN')
        self.assertIn('已审核源码补入', ''.join(report['limitations']))
        self.assertNotIn('原模型没有完整立面', ''.join(report['limitations']))
        self.assertIn('已审核源码补入', ''.join(report['contact_validity']['limitations']))

    def test_hard_center_interior_still_overrides_repaired_source_route(self):
        report=assess_rows(samples(2.13,.2),VALUES,source_family=REVIEWED_CONTACT_FAMILY)
        self.assertEqual(report['validity_status'],'FAIL')
        self.assertEqual(report['task_status'],'UNKNOWN')
        self.assertEqual(report['completion_status'],'INVALID_MODEL')

    def test_optional_reported_horizontal_force_is_read_without_new_requirement(self):
        rows=[]
        for i in range(3):
            row={k:0. for k in REQUIRED};row.update(time=i*.05)
            rows.append(row)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'samples.csv';write_csv(path,rows)
            self.assertNotIn('Fx_n',read_rows(path)[0])
            for row in rows:row['Fx_n']=-10.
            write_csv(path,rows,('Fx_n',))
            self.assertEqual(read_rows(path)[0]['Fx_n'],-10.)
            rows[0]['Fx_n']=math.nan;write_csv(path,rows,('Fx_n',))
            with self.assertRaisesRegex(ValueError,'nonfinite'):
                read_rows(path)

    def test_unknown_source_assessment_stays_unknown_even_if_geometry_is_clear(self):
        rows=[]
        for i in range(3):
            row={k:0. for k in REQUIRED};row.update(time=i*.05,x_n=.5,x_f=.3,x_r=.1,z_n=.07,z_f=.127,z_r=.127)
            rows.append(row)
        spec=dict(artifact=dict(modelica_source=SOURCE),settings=dict(stop_time=.1,intervals=2))
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'samples.csv';write_csv(path,rows)
            with patch('week13.src.case_contact_family.support_for_reviewed_source',return_value=None):
                report=assess(spec,path)
        self.assertEqual(report['terrain_binding']['status'],'UNKNOWN')
        self.assertEqual(report['task_status'],'UNKNOWN')
        self.assertEqual(report['validity_status'],'UNKNOWN')

    def test_read_only_review_does_not_mask_source_binding_unknown_or_fail(self):
        from week13.tests.test_case_contact_integration import OUT, write_fixture
        from week13.src.case_contact_review import review_snapshot, current_summary
        OUT.mkdir(parents=True,exist_ok=True)
        with tempfile.TemporaryDirectory(prefix='integration-checker-',dir=OUT) as tmp:
            root=Path(tmp);snapshot,_=write_fixture(root)
            for status,completion,chain in (('UNKNOWN','CONTACT_EVIDENCE_UNKNOWN','BLOCKED_CONTACT_EVIDENCE'),
                                             ('FAIL','INVALID_MODEL','BLOCKED_INVALID_MODEL')):
                assessment=dict(validity_status=status,task_status='UNKNOWN',completion_status=completion,
                                contact_validity=dict(status='PASS',physical_status='UNKNOWN'),issues=['binding fixture'])
                with patch('week13.src.case_checks.assess',return_value=assessment):
                    review=review_snapshot(snapshot,root)
                self.assertEqual(review['status'],status)
                current=current_summary(dict(snapshot,current_contact_review=review))
                self.assertEqual(current['chain_status'],chain)
                self.assertEqual(current['task_status'],'UNKNOWN')


if __name__=='__main__':
    unittest.main()
