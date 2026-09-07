from __future__ import annotations

import unittest

from week9.src.schemas import (
    ArtifactRecord,
    CheckStatus,
    FailureKind,
    NormalizedParameter,
    ParameterStatus,
    QualityCheckResult,
    RepairAction,
    RepairLayer,
    RepairOutcome,
    RepairRecord,
    RepairStopReason,
    ReproductionManifest,
    RunCounters,
    RunResult,
    TokenUsage,
    sha256_text,
)


class RunContractTests(unittest.TestCase):
    def test_check_status_has_exact_five_states(self):
        self.assertEqual(
            {item.value for item in CheckStatus},
            {"PASS", "FAIL", "NOT_APPLICABLE", "UNVERIFIED", "ERROR"},
        )

    def test_known_parameter_requires_value(self):
        with self.assertRaises(ValueError):
            NormalizedParameter(
                canonical_name="resistance",
                value=None,
                status=ParameterStatus.KNOWN,
            )

    def test_not_applicable_requires_reason(self):
        with self.assertRaises(ValueError):
            QualityCheckResult(
                check_type="optional",
                status=CheckStatus.NOT_APPLICABLE,
                required=False,
            )

    def test_not_applicable_cannot_be_required(self):
        with self.assertRaises(ValueError):
            QualityCheckResult(
                check_type="compile",
                status=CheckStatus.NOT_APPLICABLE,
                required=True,
                evidence={"reason": "incorrect case configuration"},
            )

    def test_forged_success_with_missing_checks_is_rejected(self):
        with self.assertRaises(ValueError):
            RunResult(
                version="v8",
                case_id="case",
                run_id="run",
                requirement_text="requirement",
                checks=[],
                overall_success=True,
            )

    def test_success_cannot_claim_an_exhausted_stop_reason(self):
        with self.assertRaisesRegex(ValueError, "all checks pass"):
            RunResult(
                version="v8",
                case_id="case",
                run_id="run",
                requirement_text="requirement",
                checks=[
                    QualityCheckResult(
                        check_type="execution_provenance",
                        status=CheckStatus.PASS,
                        required=True,
                    )
                ],
                overall_success=True,
                repair_stop_reason=RepairStopReason.INFRASTRUCTURE_RETRY_EXHAUSTED,
            )

    def test_retrieval_fields_cannot_exist_without_matching_raw_evidence(self):
        passing = QualityCheckResult(
            check_type="requirement_normalization",
            status=CheckStatus.PASS,
            required=True,
        )
        with self.assertRaisesRegex(ValueError, "raw retrieval evidence"):
            RunResult(
                version="v8",
                case_id="case",
                run_id="run",
                requirement_text="requirement",
                checks=[passing],
                overall_success=True,
                repair_stop_reason=RepairStopReason.ALL_CHECKS_PASS,
                resolved_by="retrieval_assisted_parameter_normalization",
                retrieval_triggered=True,
                retrieval_fragment_ids=["fragment.001"],
                token_usage=TokenUsage(retrieval_tokens=1),
                reproduction=ReproductionManifest(corpus_sha256="a" * 64),
            )

    def test_nontriggered_run_cannot_claim_retrieval_resolution(self):
        with self.assertRaisesRegex(ValueError, "cannot claim retrieval"):
            RunResult(
                version="v8",
                case_id="case",
                run_id="run",
                requirement_text="requirement",
                checks=[
                    QualityCheckResult(
                        check_type="requirement_normalization",
                        status=CheckStatus.PASS,
                        required=True,
                    )
                ],
                overall_success=True,
                repair_stop_reason=RepairStopReason.ALL_CHECKS_PASS,
                resolved_by="retrieval_assisted_parameter_normalization",
            )

    def test_artifact_hash_must_match_content(self):
        with self.assertRaises(ValueError):
            ArtifactRecord("modelica", content="model A end A;", sha256="forged")

    def test_non_json_metadata_is_rejected(self):
        result = RunResult(
            version="v8",
            case_id="case",
            run_id="run",
            requirement_text="requirement",
            raw_metrics={"bad": {"a", "set"}},
        )
        with self.assertRaises(TypeError):
            result.to_dict()

    def test_run_result_json_round_trip_preserves_nested_contracts(self):
        check = QualityCheckResult(
            check_type="modelica_compile",
            status=CheckStatus.PASS,
            required=True,
            evidence={"compiler": "omc"},
        )
        repair = RepairRecord(
            attempt=1,
            failed_check="modelica_compile",
            failure_kind=FailureKind.CONTENT,
            error_category="syntax",
            repair_layer=RepairLayer.MODELICA,
            action=RepairAction.RESTORE_CERTIFIED_TEMPLATE,
            artifact_type="modelica",
            failure_fingerprint_before=sha256_text("compile syntax failure"),
            previous_actions=[],
            original_evidence={"stderr": "unexpected token"},
            allowed_scope=["modelica.syntax"],
            artifact_before="model A end A;",
            artifact_after="model A\nend A;",
            before_sha256=sha256_text("model A end A;"),
            after_sha256=sha256_text("model A\nend A;"),
            diff="@@",
            required_revalidation_checks=["modelica_compile"],
            revalidation_results=[check],
            accepted=True,
            effective_after_sha256=sha256_text("model A\nend A;"),
            outcome=RepairOutcome.PASSED,
            stop_reason=RepairStopReason.ALL_CHECKS_PASS,
        )
        original = RunResult(
            version="v8",
            case_id="rc_lowpass",
            run_id="run-1",
            requirement_text="requirement",
            parameters=[
                NormalizedParameter(
                    canonical_name="resistance",
                    value=1000,
                    unit="ohm",
                    source_text="电阻 1kΩ",
                    status=ParameterStatus.KNOWN,
                )
            ],
            artifacts={
                "modelica": ArtifactRecord("modelica", content="model A\nend A;")
            },
            checks=[check],
            repairs=[repair],
            repair_stop_reason=RepairStopReason.ALL_CHECKS_PASS,
            counters=RunCounters(
                validation_count=2,
                repair_attempt_count=1,
                content_repair_count=1,
                accepted_content_repair_count=1,
            ),
            overall_success=True,
        )

        restored = RunResult.from_dict(original.to_dict())

        self.assertEqual(restored.to_dict(), original.to_dict())
        self.assertEqual(restored.checks[0].status, CheckStatus.PASS)
        self.assertEqual(restored.parameters[0].status, ParameterStatus.KNOWN)

    def test_repair_noop_uses_full_artifact_hashes(self):
        record = RepairRecord(
            attempt=1,
            failed_check="modelica_compile",
            failure_kind=FailureKind.CONTENT,
            error_category="syntax",
            repair_layer=RepairLayer.MODELICA,
            action=RepairAction.RESTORE_CERTIFIED_TEMPLATE,
            artifact_type="modelica",
            failure_fingerprint_before=sha256_text("same failure"),
            previous_actions=[],
            original_evidence={},
            allowed_scope=["modelica.syntax"],
            artifact_before="same",
            artifact_after="same",
            before_sha256=sha256_text("same"),
            after_sha256=sha256_text("same"),
            diff="",
            required_revalidation_checks=[],
            failure_fingerprint_after=sha256_text("same failure"),
            effective_after_sha256=sha256_text("same"),
            outcome=RepairOutcome.NO_OP,
            stop_reason=RepairStopReason.NO_OP,
        )
        self.assertTrue(record.is_noop)

    def test_repair_hashes_cannot_be_forged(self):
        check = QualityCheckResult(
            check_type="modelica_compile",
            status=CheckStatus.PASS,
            required=True,
        )
        with self.assertRaisesRegex(ValueError, "hashes"):
            RepairRecord(
                attempt=1,
                failed_check="modelica_compile",
                failure_kind=FailureKind.CONTENT,
                error_category="syntax",
                repair_layer=RepairLayer.MODELICA,
                action=RepairAction.RESTORE_CERTIFIED_TEMPLATE,
                artifact_type="modelica",
                failure_fingerprint_before=sha256_text("failure"),
                previous_actions=[],
                original_evidence={},
                allowed_scope=["modelica.syntax"],
                artifact_before="before",
                artifact_after="after",
                before_sha256="0" * 64,
                after_sha256=sha256_text("after"),
                diff="@@",
                required_revalidation_checks=["modelica_compile"],
                revalidation_results=[check],
                accepted=True,
                effective_after_sha256=sha256_text("after"),
                outcome=RepairOutcome.PASSED,
            )


if __name__ == "__main__":
    unittest.main()
