from __future__ import annotations

from pathlib import Path
import tempfile
import unittest

from week9.experiments.external_evaluator import (
    EvaluationContext,
    ValidatorSpec,
    evaluate,
)
from week9.src.schemas import (
    ArtifactRecord,
    CheckStatus,
    QualityCheckResult,
)


def artifacts(*, include_csv: bool = True):
    result = {
        "sysml": ArtifactRecord("sysml", content="package P { part def A; }"),
        "modelica": ArtifactRecord("modelica", content="model A\nend A;"),
    }
    if include_csv:
        result["simulation_csv"] = ArtifactRecord(
            "simulation_csv", content="time,y\n0,0\n1,1\n", media_type="text/csv"
        )
    return result


def pass_check(name: str):
    return QualityCheckResult(name, CheckStatus.PASS, True, evidence={"executed": True})


class ExternalEvaluatorTests(unittest.TestCase):
    def context(self, version: str, *, include_csv: bool = True):
        return EvaluationContext(
            version=version,
            case_id="same-case",
            run_id=f"{version}-run",
            requirement_text="same requirement",
            artifacts=artifacts(include_csv=include_csv),
        )

    def test_same_artifacts_get_same_decision_across_versions(self):
        results = [
            evaluate(
                context=self.context(version),
                required_check_types=("modelica_compile",),
                precomputed_external_checks=[pass_check("modelica_compile")],
            )
            for version in ("v4", "v6", "v8")
        ]
        self.assertEqual([item.overall_success for item in results], [True, True, True])
        statuses = [
            [(check.check_type, check.status) for check in item.checks]
            for item in results
        ]
        self.assertEqual(statuses[0], statuses[1])
        self.assertEqual(statuses[1], statuses[2])

    def test_missing_csv_is_unverified_and_blocks(self):
        result = evaluate(
            context=self.context("v8", include_csv=False),
            required_check_types=("modelica_compile",),
            precomputed_external_checks=[pass_check("modelica_compile")],
        )
        check_map = {item.check_type: item for item in result.checks}
        self.assertFalse(result.overall_success)
        self.assertEqual(
            check_map["artifact.simulation_csv.present"].status,
            CheckStatus.UNVERIFIED,
        )

    def test_unimplemented_required_check_is_unverified(self):
        result = evaluate(
            context=self.context("v8"),
            required_check_types=("physical_behavior",),
        )
        check_map = {item.check_type: item for item in result.checks}
        self.assertEqual(check_map["physical_behavior"].status, CheckStatus.UNVERIFIED)
        self.assertFalse(result.overall_success)

    def test_empty_core_check_configuration_fails_closed(self):
        with self.assertRaises(ValueError):
            evaluate(context=self.context("v8"), required_check_types=())

    def test_whitespace_and_empty_file_do_not_count_as_artifacts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            empty_csv = Path(temp_dir) / "empty.csv"
            empty_csv.write_bytes(b"")
            context = EvaluationContext(
                version="v8",
                case_id="empty-artifacts",
                run_id="run",
                requirement_text="requirement",
                artifacts={
                    "sysml": ArtifactRecord("sysml", content="   \n"),
                    "modelica": ArtifactRecord("modelica", content="\t"),
                    "simulation_csv": ArtifactRecord(
                        "simulation_csv", path=str(empty_csv), media_type="text/csv"
                    ),
                },
            )
            result = evaluate(
                context=context,
                required_check_types=("modelica_compile",),
                precomputed_external_checks=[pass_check("modelica_compile")],
            )
        self.assertFalse(result.overall_success)
        presence = {
            item.check_type: item.status
            for item in result.checks
            if item.check_type.startswith("artifact.")
        }
        self.assertTrue(all(status is CheckStatus.UNVERIFIED for status in presence.values()))

    def test_validator_exception_becomes_blocking_error(self):
        def explode(_context):
            raise RuntimeError("tool unavailable")

        result = evaluate(
            context=self.context("v8"),
            required_check_types=("physical_behavior",),
            validators=[ValidatorSpec("physical_behavior", explode, tool_name="fake")],
        )
        check_map = {item.check_type: item for item in result.checks}
        self.assertEqual(check_map["physical_behavior"].status, CheckStatus.ERROR)
        self.assertEqual(check_map["physical_behavior"].failure_category, "validator_exception")
        self.assertFalse(result.overall_success)


if __name__ == "__main__":
    unittest.main()
