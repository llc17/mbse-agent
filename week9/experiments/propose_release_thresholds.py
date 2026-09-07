"""Derive an auditable threshold proposal from a verified V4/V6 pilot.

This command never edits the frozen protocol.  It emits a proposal whose
formulas use only contemporaneous baseline data and preregistered policy.  A
separate explicit protocol update is still required before formal V8 results.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import sys
from typing import Any, Mapping


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = PROJECT_ROOT / "week9" / "results"


def derive_thresholds(pilot_report: Mapping[str, Any]) -> dict[str, Any]:
    if pilot_report.get("schema_version") != "v8-contemporaneous-pilot-1":
        raise ValueError("unsupported pilot report schema")
    if pilot_report.get("verdict") != "PILOT_PASS":
        raise ValueError("thresholds require a passing pilot")
    guard = pilot_report.get("guard", {})
    if (
        guard.get("versions") != ["v4", "v6"]
        or guard.get("development_cases_only") is not True
        or guard.get("holdout_accessed") is not False
        or guard.get("external_evaluator")
        != "week9.cross_version_validation.v1"
    ):
        raise ValueError("pilot guard is not eligible for threshold derivation")
    conditions = pilot_report.get("pass_conditions", {})
    if not conditions or any(value is not True for value in conditions.values()):
        raise ValueError("pilot pass conditions are incomplete")
    v6 = pilot_report.get("summary_by_version", {}).get("v6", {})
    run_count = v6.get("run_count")
    success_rate = v6.get("success_rate")
    token_median = v6.get("simple_case_total_tokens", {}).get("median")
    duration_median = v6.get("simple_case_duration_s", {}).get("median")
    repair_max = v6.get("content_repair_count", {}).get("maximum")
    for name, value in (
        ("v6 run_count", run_count),
        ("v6 success_rate", success_rate),
        ("v6 simple token median", token_median),
        ("v6 simple duration median", duration_median),
        ("v6 repair maximum", repair_max),
    ):
        if type(value) not in {int, float} or not math.isfinite(float(value)):
            raise ValueError(f"pilot lacks finite {name}")
    if int(run_count) < 1 or float(token_median) <= 0 or float(duration_median) <= 0:
        raise ValueError("pilot baseline counts and budgets must be positive")
    improvement = 1.0 / int(run_count)
    max_repairs = max(1, int(math.ceil(float(repair_max))))
    thresholds = {
        "development_success_improvement_min": improvement,
        "holdout_noninferiority_margin": 0.0,
        "simple_case_token_budget": int(math.ceil(float(token_median))),
        "simple_case_duration_budget_s": float(duration_median),
        "max_content_repairs": max_repairs,
    }
    return {
        "thresholds": thresholds,
        "derivation": {
            "development_success_improvement_min": (
                "one additional externally successful run divided by the V6 pilot run count"
            ),
            "holdout_noninferiority_margin": (
                "strict preregistered no-regression policy; holdout was not accessed"
            ),
            "simple_case_token_budget": "ceiling of V6 simple-case median total tokens",
            "simple_case_duration_budget_s": "V6 simple-case median pipeline duration",
            "max_content_repairs": (
                "maximum of one directed repair and the observed V6 pilot maximum"
            ),
        },
        "baseline": {
            "v6_run_count": int(run_count),
            "v6_success_rate": float(success_rate),
            "v6_simple_token_median": float(token_median),
            "v6_simple_duration_median_s": float(duration_median),
            "v6_content_repair_maximum": int(repair_max),
        },
        "comparators": {
            "development_success": "v8_rate >= v6_rate + development_success_improvement_min",
            "holdout_noninferiority": "v8_rate >= v6_rate - holdout_noninferiority_margin",
            "simple_token": "v8_median <= simple_case_token_budget",
            "simple_duration": "v8_median <= simple_case_duration_budget_s",
            "repairs": "each_v8_run.content_repair_count <= max_content_repairs",
        },
    }


def propose(
    pilot_report_path: Path, *, output_root: Path | None = None
) -> tuple[dict[str, Any], Path]:
    pilot = _verified_pilot(pilot_report_path.resolve())
    derived = derive_thresholds(pilot)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = (output_root or RESULTS_ROOT).resolve()
    result_dir = root / f"release-threshold-proposal-{timestamp}"
    result_dir.mkdir(parents=True, exist_ok=False)
    report = {
        "schema_version": "v8-release-threshold-proposal-1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "verdict": "THRESHOLD_PROPOSAL_READY",
        "source_pilot_sha256": _raw_sha256(pilot_report_path),
        "source_pilot_protocol_sha256": pilot.get("protocol_sha256"),
        "implementation_sha256": _raw_sha256(Path(__file__).resolve()),
        **derived,
        "guard": {
            "v8_formal_result_read": False,
            "holdout_accessed": False,
            "protocol_modified": False,
        },
        "next_action": (
            "Review and explicitly copy these numeric values into the frozen protocol "
            "before any formal V8 result is generated."
        ),
    }
    report_path = result_dir / "release-threshold-proposal.json"
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    report_path.with_suffix(".sha256").write_text(
        f"{_raw_sha256(report_path)}  {report_path.name}\n", encoding="ascii"
    )
    return report, result_dir


def _verified_pilot(path: Path) -> dict[str, Any]:
    pilot = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(pilot, dict):
        raise ValueError("pilot report root must be an object")
    expected = path.with_suffix(".sha256").read_text(encoding="ascii").strip().split()[0]
    if expected != _raw_sha256(path):
        raise ValueError("pilot report hash mismatch")
    return pilot


def _raw_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pilot-report", required=True)
    parser.add_argument("--out-root", default=None)
    args = parser.parse_args(argv)
    try:
        report, result_dir = propose(
            Path(args.pilot_report),
            output_root=Path(args.out_root) if args.out_root else None,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(f"BLOCKED: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    print(f"thresholds={report['verdict']} -> {result_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["derive_thresholds", "propose"]
