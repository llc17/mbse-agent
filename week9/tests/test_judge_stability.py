from __future__ import annotations

import json
import unittest

from week9.experiments.run_judge_stability import (
    blind_payload,
    build_judge_stability_report,
    main,
    validate_score,
)
from week9.src.schemas import ArtifactRecord, NormalizedParameter, RunResult


def _artifacts():
    return [
        {
            "artifact_id": f"artifact-{index:03d}",
            "source_case_id": f"case-{index}",
            "source_version": "v6",
            "source_run_id": f"run-{index}",
            "source_result_sha256": "a" * 64,
            "blind_input_file": f"blind-inputs/artifact-{index:03d}.json",
            "blind_input_sha256": f"{index}" * 64,
            "blind_marker_count": 0,
        }
        for index in range(1, 4)
    ]


def _trials(totals_by_artifact):
    records = []
    sequence = 0
    for artifact_id, totals in totals_by_artifact.items():
        for repeat, total in enumerate(totals, start=1):
            sequence += 1
            records.append(
                {
                    "sequence": sequence,
                    "artifact_id": artifact_id,
                    "repeat": repeat,
                    "valid": True,
                    "scores": {
                        "syntax": total // 4,
                        "consistency": total // 4,
                        "topology": total // 4,
                        "traceability": total - 3 * (total // 4),
                    },
                    "total": total,
                    "provider": "kimi",
                    "model": "frozen-judge",
                    "error": "",
                }
            )
    return records


class JudgeStabilityTests(unittest.TestCase):
    def test_blind_payload_removes_version_agent_and_path_markers(self):
        result = RunResult(
            version="v6",
            case_id="case",
            run_id="run",
            requirement_text="V8 Agent should build a filter",
            parameters=[NormalizedParameter("resistance", 1000.0, "Ohm", "R")],
            artifacts={
                "sysml": ArtifactRecord(
                    "sysml", content="package P { // V6 Agent C:\\secret\\x\n part def A; }"
                ),
                "modelica": ArtifactRecord(
                    "modelica", content="model A // V4\n Real x; end A;"
                ),
            },
            raw_metrics={"template_id": "rc_filter"},
        )
        payload = blind_payload(result, artifact_id="artifact-001")
        serialized = json.dumps(payload, ensure_ascii=False)
        self.assertNotRegex(serialized.lower(), r"\bv[468]\b")
        self.assertNotIn("agent", serialized.lower())
        self.assertNotIn("C:\\secret", serialized)
        self.assertNotIn("version", payload)

    def test_score_validation_is_strict(self):
        valid, normalized, issue = validate_score(
            {
                "scores": {
                    "syntax": 20,
                    "consistency": 21,
                    "topology": 22,
                    "traceability": 23,
                },
                "total": 86,
                "comment": "ok",
                "provider": "kimi",
                "model": "judge",
            }
        )
        self.assertTrue(valid, issue)
        self.assertEqual(normalized["total"], 86)
        invalid, _, _ = validate_score(
            {
                "scores": {
                    "syntax": 20,
                    "consistency": 21,
                    "topology": 22,
                    "traceability": 23,
                },
                "total": 99,
                "provider": "kimi",
                "model": "judge",
            }
        )
        self.assertFalse(invalid)

    def test_report_passes_measurement_and_selects_variance_policy(self):
        artifacts = _artifacts()
        totals = {
            item["artifact_id"]: [80, 81, 80]
            for item in artifacts
        }
        report = build_judge_stability_report(
            artifacts=artifacts,
            trials=_trials(totals),
            repeats_per_artifact=3,
            protocol_sha256="b" * 64,
            confirmation_id="confirmed",
        )
        self.assertEqual(report["verdict"], "JUDGE_STABILITY_PASS")
        self.assertEqual(
            report["formal_evidence_policy"], "SINGLE_SCORE_AUXILIARY_ALLOWED"
        )
        self.assertFalse(report["guard"]["generator_called"])

        noisy = {item["artifact_id"]: [70, 80, 90] for item in artifacts}
        noisy_report = build_judge_stability_report(
            artifacts=artifacts,
            trials=_trials(noisy),
            repeats_per_artifact=3,
            protocol_sha256="b" * 64,
            confirmation_id="confirmed",
        )
        self.assertEqual(
            noisy_report["formal_evidence_policy"], "QUALITATIVE_AUXILIARY_ONLY"
        )

    def test_cli_without_execute_online_is_blocked(self):
        self.assertEqual(main(["--pilot-report", "unused.json"]), 2)


if __name__ == "__main__":
    unittest.main()
