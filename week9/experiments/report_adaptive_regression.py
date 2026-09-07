"""Pure report builder for the offline Stage-4 mechanism regression."""

from __future__ import annotations

from collections import Counter
from typing import Any, Mapping, Sequence

from week9.src.schemas import utc_now_iso


CLAIM_STATUS = {
    "simple_case_token_budget": "NOT_FROZEN",
    "simple_case_duration_budget_s": "NOT_FROZEN",
    "development_success_improvement_threshold": "NOT_FROZEN",
    "holdout_noninferiority_margin": "NOT_FROZEN",
    "formal_max_content_repairs": "NOT_FROZEN",
    "contemporaneous_v4_v6_pilot": "NOT_RUN",
    "v4_v6_v8_fair_comparison": "NOT_RUN",
    "online_smoke": "NOT_RUN",
    "formal_experiment": "NOT_RUN",
    "holdout_evaluation": "NOT_RUN",
    "judge_resampling": "NOT_RUN",
    "natural_language_end_to_end_generation": "NOT_RUN",
    "token_cost_comparison": "NOT_RUN",
    "duration_cost_comparison": "NOT_RUN",
    "cost_advantage_evaluation": "NOT_RUN",
    "generalization_evaluation": "NOT_RUN",
}


def build_adaptive_regression_report(
    *,
    suite_snapshot: Mapping[str, Any],
    suite_sha256: str,
    cases: Sequence[Mapping[str, Any]],
    offline_guard: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare declared mechanism expectations without making release claims."""

    guard = dict(offline_guard)
    required_false = (
        "online_api_used",
        "external_network_used",
        "holdout_accessed",
        "legacy_adapters_used",
        "cross_version_comparison_performed",
        "cases_module_imported",
    )
    if any(guard.get(name) is not False for name in required_false):
        raise ValueError("offline regression guard is incomplete or unsafe")
    if suite_snapshot.get("experiment_scope") != (
        "development_offline_mechanism_regression"
    ):
        raise ValueError("suite scope is not the Stage-4 development regression")
    if len(cases) != len(suite_snapshot.get("cases", ())):
        raise ValueError("executed case count differs from the frozen suite snapshot")

    rendered_cases: list[dict[str, Any]] = []
    path_matrix: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    invalid_fast_releases = 0
    for raw_case in cases:
        item = dict(raw_case)
        expectations = item.get("expectations")
        actual = item.get("actual")
        if not isinstance(expectations, Mapping) or not isinstance(actual, Mapping):
            raise TypeError("each case requires expectation and actual mappings")
        missing = sorted(set(expectations) - set(actual))
        mismatches = {
            name: {"expected": expected, "actual": actual.get(name)}
            for name, expected in expectations.items()
            if name in actual and actual.get(name) != expected
        }
        for name in missing:
            mismatches[name] = {
                "expected": expectations[name],
                "actual": "MISSING",
            }
        assertion_status = "PASS" if not mismatches else "FAIL"
        status_counts[assertion_status] += 1
        expected_path = str(expectations.get("path", "NOT_APPLICABLE"))
        actual_path = str(actual.get("path", "NOT_APPLICABLE"))
        path_matrix[f"{expected_path}->{actual_path}"] += 1
        if actual_path == "fast_path" and (
            actual.get("overall_success") is not True
            or actual.get("validation_count") != 1
            or actual.get("repair_attempted") is not False
            or actual.get("retrieval_triggered") is not False
        ):
            invalid_fast_releases += 1
        rendered_cases.append(
            {
                **item,
                "mechanism_assertion_status": assertion_status,
                "mismatches": mismatches,
            }
        )

    passed = status_counts["PASS"]
    verdict = (
        "MECHANISM_REGRESSION_PASS"
        if passed == len(rendered_cases) and invalid_fast_releases == 0
        else "MECHANISM_REGRESSION_FAIL"
    )
    return {
        "schema_version": "v8-stage4-adaptive-regression-1",
        "generated_at": utc_now_iso(),
        "experiment_scope": "development_offline_mechanism_regression",
        "verdict": verdict,
        "offline_guard": guard,
        "claim_status": dict(CLAIM_STATUS),
        "suite_snapshot": {
            "sha256": suite_sha256,
            "schema_version": suite_snapshot.get("schema_version", ""),
            "case_ids": [item.get("case_id", "") for item in suite_snapshot["cases"]],
            "suite_limits_status": suite_snapshot.get("suite_limits_status", ""),
        },
        "summary": {
            "planned_case_count": len(suite_snapshot["cases"]),
            "run_case_count": len(rendered_cases),
            "pass_count": passed,
            "fail_count": status_counts["FAIL"],
            "not_run_count": 0,
            "invalid_fast_release_count": invalid_fast_releases,
            "path_matrix": dict(sorted(path_matrix.items())),
            "cost_claims_evaluated": False,
        },
        "cases": rendered_cases,
        "limitations": [
            "This is a V8-only development mechanism regression, not a release experiment.",
            "No V4/V6 adapter, holdout case, online model, external network, or Judge was used.",
            "Suite repair and retrieval limits are local safety limits, not frozen release thresholds.",
            "Zero online-model tokens means no online model was called; it is not a relative cost claim.",
            "Observed duration is machine-local diagnostic evidence and is not compared with V4 or V6.",
        ],
    }


__all__ = ["CLAIM_STATUS", "build_adaptive_regression_report"]
