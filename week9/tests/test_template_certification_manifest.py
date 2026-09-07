from __future__ import annotations

import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from week9.experiments.cases import ERROR_INJECTION_CASES
import week9.src.hard_gate as hard_gate_module
from week9.src.hard_gate import check_certification_anchor
from week9.src.modelica_templates import (
    TEMPLATE_REGISTRY,
    render_template,
    representative_parameters,
)
from week9.src.schemas import CheckStatus


MANIFEST_PATH = (
    Path(__file__).resolve().parents[1] / "configs" / "template_certifications.json"
)


class TemplateCertificationManifestTests(unittest.TestCase):
    def _load_manifest(self):
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))

    def test_manifest_covers_current_template_hashes(self):
        manifest = self._load_manifest()
        self.assertTrue(manifest["summary"]["all_certified"])
        self.assertTrue(manifest["summary"]["stage1_gate_passed"])
        self.assertFalse(manifest["environment"]["online_api_used"])
        self.assertEqual(set(manifest["templates"]), set(TEMPLATE_REGISTRY))
        for template_id, spec in TEMPLATE_REGISTRY.items():
            with self.subTest(template_id=template_id):
                certificate = manifest["templates"][template_id]
                rendered = render_template(
                    template_id,
                    representative_parameters(template_id),
                    model_name=f"Certified_{template_id}",
                )
                self.assertEqual(certificate["template_sha256"], spec.source_sha256)
                self.assertEqual(
                    certificate["rendered_sha256"], rendered.rendered_sha256
                )
                self.assertEqual(certificate["stimulus_type"], spec.stimulus_type)
                self.assertEqual(
                    certificate["expected_output_variables"],
                    list(spec.output_variables),
                )
                self.assertEqual(certificate["compile"], "PASS")
                self.assertEqual(certificate["simulation"], "PASS")
                self.assertEqual(certificate["physical_behavior"], "PASS")
                self.assertEqual(certificate["full_gate"], "PASS")
                self.assertTrue(certificate["overall_success"])
                self.assertEqual(certificate["blocking_checks"], [])
                self.assertEqual(
                    set(certificate["checks"]), set(manifest["required_checks"])
                )
                for check in certificate["checks"].values():
                    self.assertEqual(check["status"], "PASS")
                    self.assertTrue(check["required"])
                provenance = certificate["checks"]["execution_provenance"]
                compile_check = certificate["checks"]["modelica_compile"]
                simulation = certificate["checks"]["simulation"]
                self.assertEqual(
                    provenance["evidence"]["source_sha256"],
                    certificate["rendered_sha256"],
                )
                self.assertEqual(
                    compile_check["evidence"]["source_sha256"],
                    certificate["rendered_sha256"],
                )
                self.assertEqual(
                    provenance["evidence"]["csv_sha256"],
                    simulation["evidence"]["csv_sha256"],
                )

    def test_manifest_is_anchored_to_current_gate_implementation(self):
        manifest = self._load_manifest()
        week9_root = MANIFEST_PATH.parents[1]
        current_hashes = {
            relative_path: hashlib.sha256(
                (week9_root / relative_path).read_bytes()
            ).hexdigest()
            for relative_path in (
                "src/modelica_templates.py",
                "src/modelica_tool.py",
                "src/diagnostics.py",
                "src/hard_gate.py",
                "src/quality_gate.py",
                "src/schemas.py",
                "src/repair_loop.py",
                "src/parameter_normalization.py",
                "src/retrieval.py",
                "src/generation.py",
                "src/pipeline.py",
                "src/routing.py",
                "src/traceability.py",
                "src/runner.py",
                "configs/retrieval_corpus.json",
                "configs/retrieval_corpus.sha256",
                "configs/retrieval_policy.json",
                "experiments/certify_templates.py",
                "experiments/repair_regression.py",
                "experiments/run_retrieval_ablation.py",
                "experiments/report_adaptive_regression.py",
                "experiments/run_adaptive_regression.py",
                "experiments/run_natural_language_regression.py",
                "experiments/cases.py",
                "configs/stage4_adaptive_regression_suite.json",
                "configs/stage0_natural_language_regression_suite.json",
                "configs/reproducibility_manifest.template.json",
            )
        }
        self.assertEqual(manifest["implementation_sha256"], current_hashes)
        self.assertEqual(
            set(manifest["required_checks"]),
            {
                "requirement_normalization",
                "sysml_syntax",
                "certified_template_integrity",
                "template_certification_anchor",
                "execution_provenance",
                "modelica_compile",
                "parameter_unit_consistency",
                "simulation",
                "physical_behavior",
                "cross_model_consistency",
            },
        )

    def test_release_consumer_accepts_each_current_certificate(self):
        for template_id, spec in TEMPLATE_REGISTRY.items():
            with self.subTest(template_id=template_id):
                check = check_certification_anchor(spec, None)
                self.assertEqual(check.status, CheckStatus.PASS, check.issues)

    def test_release_consumer_rejects_false_stage1_summary(self):
        manifest = self._load_manifest()
        mutations = (
            ("stage1_gate_passed", False),
            ("known_hard_error_false_release_count", 1),
        )
        for field, value in mutations:
            with self.subTest(field=field):
                mutated = json.loads(json.dumps(manifest))
                mutated["summary"][field] = value
                with tempfile.TemporaryDirectory(prefix="v8-stale-manifest-") as root:
                    path = Path(root) / "manifest.json"
                    path.write_text(
                        json.dumps(mutated, ensure_ascii=False), encoding="utf-8"
                    )
                    with patch.object(
                        hard_gate_module, "CERTIFICATION_MANIFEST_PATH", path
                    ):
                        check = check_certification_anchor(
                            TEMPLATE_REGISTRY["rc_filter"], None
                        )
                self.assertEqual(check.status, CheckStatus.UNVERIFIED)

    def test_manifest_metrics_are_recomputed_from_recorded_outcomes(self):
        manifest = self._load_manifest()
        summary = manifest["summary"]
        certificates = manifest["templates"]
        injections = manifest["error_injections"]

        false_rejections = sum(
            1 for certificate in certificates.values()
            if certificate["full_gate"] != "PASS"
        )
        false_releases = sum(1 for item in injections if item["overall_success"])
        self.assertEqual(summary["correct_sample_count"], len(certificates))
        self.assertEqual(
            summary["correct_sample_false_rejection_count"], false_rejections
        )
        self.assertEqual(summary["known_hard_error_count"], len(injections))
        self.assertEqual(
            summary["known_hard_error_false_release_count"], false_releases
        )
        self.assertEqual(false_rejections, 0)
        self.assertEqual(false_releases, 0)

    def test_all_frozen_error_injections_have_matching_blocking_evidence(self):
        manifest = self._load_manifest()
        recorded = {
            item["injection_id"]: item for item in manifest["error_injections"]
        }
        self.assertEqual(set(recorded), set(ERROR_INJECTION_CASES))
        for injection_id, specification in ERROR_INJECTION_CASES.items():
            with self.subTest(injection_id=injection_id):
                item = recorded[injection_id]
                self.assertEqual(item["target_check"], specification.target_check)
                self.assertEqual(
                    item["expected_status"], specification.expected_status.value
                )
                self.assertEqual(item["actual_status"], item["expected_status"])
                self.assertEqual(
                    item["expected_failure_kind"],
                    specification.expected_failure_kind,
                )
                self.assertEqual(
                    item["actual_failure_kind"], item["expected_failure_kind"]
                )
                self.assertTrue(item["expectation_matched"])
                self.assertFalse(item["overall_success"])
                self.assertTrue(item["blocking_checks"])


if __name__ == "__main__":
    unittest.main()
