from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import tempfile
import unittest

from week9.experiments.validate_independent_holdout_seal import (
    FAIL_VERDICT,
    PASS_VERDICT,
    validate_independent_holdout_seal,
)


SOURCE_MODEL = "independent-curator-test-model"
CREATED_UTC = "2026-09-01T00:00:00Z"
RAW_MARKER = "PRIVATE_TEST_MARKER_NEVER_EMIT"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _case_payload() -> list[dict]:
    resistance = 1234.0
    frequency = 1777.0
    capacitance = 1.0 / (2.0 * math.pi * resistance * frequency)
    return [
        {
            "case_id": "ih_0123456789abcdef",
            "dataset": "holdout",
            "domain": "electrical",
            "raw_input": (
                "设计一个 RC 低通滤波器，截止频率 1777 Hz，电阻 1234 Ohm，"
                "电容根据截止频率计算，输入电压 3.7 V 阶跃信号。" + RAW_MARKER
            ),
            "expected_physics": {
                "validate_type": "rc_cutoff",
                "expected_value_source": "params.cutoff_freq",
                "tolerance_pct": 10.0,
            },
            "oracle": {
                "template_id": "rc_filter",
                "canonical_parameters": {
                    "resistance": {"value": resistance, "unit": "Ohm"},
                    "capacitance": {"value": capacitance, "unit": "F"},
                    "cutoff_frequency": {"value": frequency, "unit": "Hz"},
                    "input_voltage": {"value": 3.7, "unit": "V"},
                },
                "derived_formula": "C=1/(2*pi*R*f)",
                "expected_behavior": "private electrical behavior",
            },
            "tags": ["independent_new_holdout", "do_not_use_for_development"],
        },
        {
            "case_id": "ih_fedcba9876543210",
            "dataset": "holdout",
            "domain": "thermal",
            "raw_input": (
                "设计一个双房间热传导系统，房间A热容 223457 J/K，房间B热容 314159 J/K，"
                "两房间之间墙体热阻 0.037 K/W，房间A外墙热阻 0.029 K/W，"
                "房间B外墙热阻 0.043 K/W，房间A初始温度 291.2 K，"
                "房间B初始温度 294.1 K，室外温度 309.7 K。" + RAW_MARKER
            ),
            "expected_physics": {
                "validate_type": "thermal_steady",
                "expected_value_source": "params.outdoor_temp",
                "tolerance_pct": 20.0,
            },
            "oracle": {
                "template_id": "thermal_dual_room",
                "canonical_parameters": {
                    "heat_capacity_a": {"value": 223457, "unit": "J/K"},
                    "heat_capacity_b": {"value": 314159, "unit": "J/K"},
                    "shared_resistance": {"value": 0.037, "unit": "K/W"},
                    "outdoor_resistance_a": {"value": 0.029, "unit": "K/W"},
                    "outdoor_resistance_b": {"value": 0.043, "unit": "K/W"},
                    "initial_temperature_a": {"value": 291.2, "unit": "K"},
                    "initial_temperature_b": {"value": 294.1, "unit": "K"},
                    "outdoor_temperature": {"value": 309.7, "unit": "K"},
                },
                "derived_formula": "none",
                "expected_behavior": "private thermal behavior",
            },
            "tags": ["independent_new_holdout", "do_not_use_for_development"],
        },
    ]


class IndependentHoldoutSealTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.submission = self.root / "submission_20260901T000000Z"
        self.submission.mkdir()
        self.private_path = self.submission / "independent_holdout.private.json"
        self.manifest_path = self.submission / "independent_holdout.manifest.json"
        self.sidecar_path = self.submission / "independent_holdout.manifest.sha256"
        self.private = {
            "schema_version": "v8-independent-holdout-private-1",
            "curation": {
                "source_model_id": SOURCE_MODEL,
                "created_utc": CREATED_UTC,
                "project_files_read": False,
                "prior_v8_content_seen": False,
                "independently_created": True,
                "selection_method": "os_cryptographic_random",
            },
            "required_checks": [
                "requirement_normalization",
                "sysml_syntax",
                "modelica_compile",
                "parameter_unit_consistency",
                "simulation",
                "physical_behavior",
                "cross_model_consistency",
            ],
            "cases": _case_payload(),
        }
        self._write_submission()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _write_submission(self) -> str:
        self.private_path.write_text(
            json.dumps(self.private, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        private_digest = _sha256(self.private_path)
        manifest = {
            "schema_version": "v8-independent-holdout-manifest-1",
            "private_artifact": {
                "file_name": self.private_path.name,
                "sha256": private_digest,
                "byte_count": self.private_path.stat().st_size,
            },
            "case_count": 2,
            "source_model_id": self.private["curation"]["source_model_id"],
            "created_utc": CREATED_UTC,
            "declarations": {
                "project_files_read": False,
                "prior_v8_content_seen": False,
                "independently_created": True,
                "plaintext_disclosed_to_v8_developer": False,
            },
        }
        self.manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        digest = _sha256(self.manifest_path)
        self.sidecar_path.write_text(digest + "\n", encoding="ascii")
        return digest

    def _validate(self, digest: str | None = None):
        return validate_independent_holdout_seal(
            self.manifest_path,
            expected_manifest_sha256=digest or _sha256(self.manifest_path),
            intake_root=self.root,
        )

    def test_valid_seal_passes_without_disclosing_private_fields(self):
        report = self._validate()
        self.assertEqual(report["verdict"], PASS_VERDICT)
        self.assertTrue(all(report["checks"].values()))
        serialized = json.dumps(report, ensure_ascii=False)
        self.assertNotIn(RAW_MARKER, serialized)
        self.assertNotIn("ih_0123456789abcdef", serialized)
        self.assertNotIn("private electrical behavior", serialized)
        self.assertFalse(report["privacy_guard"]["private_content_emitted"])

    def test_formula_mismatch_fails_with_code_only(self):
        self.private["cases"][0]["oracle"]["canonical_parameters"]["capacitance"]["value"] *= 2
        digest = self._write_submission()
        report = self._validate(digest)
        self.assertEqual(report["verdict"], FAIL_VERDICT)
        self.assertFalse(report["checks"]["formula_recomputation_valid"])
        self.assertFalse(report["checks"]["oracle_matches_parser"])

    def test_forbidden_source_role_fails(self):
        self.private["curation"]["source_model_id"] = "gpt-test-curator"
        digest = self._write_submission()
        report = self._validate(digest)
        self.assertEqual(report["verdict"], FAIL_VERDICT)
        self.assertFalse(report["checks"]["role_separation_valid"])

    def test_manifest_hash_mismatch_fails(self):
        report = self._validate("0" * 64)
        self.assertEqual(report["verdict"], FAIL_VERDICT)
        self.assertFalse(report["checks"]["manifest_hash_matches_expected"])

    def test_extra_submission_file_fails_closed(self):
        (self.submission / "unexpected.txt").write_text("synthetic", encoding="utf-8")
        report = self._validate()
        self.assertEqual(report["verdict"], FAIL_VERDICT)
        self.assertFalse(report["checks"]["submission_files_exact"])


if __name__ == "__main__":
    unittest.main()
