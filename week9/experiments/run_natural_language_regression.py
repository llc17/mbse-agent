"""Run the frozen Stage-0 natural-language development regression offline."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime, timezone
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from week9.experiments.cases import get_case  # noqa: E402
from week9.experiments.freeze_protocol import (  # noqa: E402
    DEFAULT_PROTOCOL,
    validate_protocol,
)
from week9.src.schemas import RunResult  # noqa: E402


SUITE_PATH = (
    PROJECT_ROOT
    / "week9"
    / "configs"
    / "stage0_natural_language_regression_suite.json"
)
RUNNER_PATH = PROJECT_ROOT / "week9" / "src" / "runner.py"
RESULTS_ROOT = PROJECT_ROOT / "week9" / "results"
_SENSITIVE_ENV_KEYS = (
    "KIMI_API_KEY",
    "KIMI_API_BASE",
    "KIMI_MODEL",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
)


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{path} must contain a JSON object")
    return value


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _raw_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _assert_offline_suite(suite: dict[str, Any]) -> None:
    guard = suite.get("offline_guard")
    expected = {
        "external_network_allowed": False,
        "online_model_allowed": False,
        "holdout_access_allowed": False,
        "allowed_generation_provider": "v8_deterministic_local_grammar",
    }
    if guard != expected:
        raise ValueError("natural-language regression offline guard is not exact")
    if suite.get("scope") != "development_regression_only":
        raise ValueError("natural-language regression may use only development cases")
    cases = suite.get("cases")
    if not isinstance(cases, list) or not cases:
        raise ValueError("natural-language regression requires frozen cases")
    case_ids = [item.get("case_id") for item in cases if isinstance(item, dict)]
    if len(case_ids) != len(cases) or len(case_ids) != len(set(case_ids)):
        raise ValueError("natural-language regression case ids must be unique")


def _actual(result: RunResult) -> dict[str, Any]:
    return {
        "overall_success": result.overall_success,
        "execution_contract": result.execution_contract,
        "input_kind": result.routing.input_kind.value,
        "path": result.routing.path.value,
        "final_state": (
            result.routing.final_state.value
            if result.routing.final_state is not None
            else None
        ),
        "natural_language_interpretation_performed": (
            result.routing.natural_language_interpretation_performed
        ),
        "online_api_used": result.routing.online_api_used,
        "generation_count": result.counters.generation_count,
        "model_tokens": result.token_usage.total_tokens,
        "retrieval_triggered": result.retrieval_triggered,
        "trace_integrity": bool(
            result.traceability is not None
            and result.traceability.integrity_passed
        ),
        "token_budget_status": (
            result.budget_ledger.token_budget_status.value
            if result.budget_ledger is not None
            else None
        ),
        "duration_budget_status": (
            result.budget_ledger.duration_budget_status.value
            if result.budget_ledger is not None
            else None
        ),
    }


def run_suite(*, output_root: Path | None = None) -> tuple[dict[str, Any], Path]:
    suite = _read_object(SUITE_PATH)
    _assert_offline_suite(suite)
    protocol = _read_object(DEFAULT_PROTOCOL)
    protocol_errors = validate_protocol(protocol)
    if protocol_errors:
        raise ValueError("frozen protocol is invalid: " + "; ".join(protocol_errors))

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    root = (output_root or RESULTS_ROOT).resolve()
    result_dir = root / f"stage0-natural-language-offline-{timestamp}"
    result_dir.mkdir(parents=True, exist_ok=False)
    runs_dir = result_dir / "runs"
    runs_dir.mkdir()

    expected = suite["exact_expectations"]
    records: list[dict[str, Any]] = []
    environment = os.environ.copy()
    for key in _SENSITIVE_ENV_KEYS:
        environment.pop(key, None)
    environment["V8_OFFLINE_ONLY"] = "1"

    for index, case_spec in enumerate(suite["cases"], start=1):
        case_id = case_spec["case_id"]
        case = get_case(case_id)
        if case is None or case.dataset != "development_regression":
            raise ValueError(f"suite case is not a development case: {case_id}")
        if case.sha256 != case_spec["case_sha256"]:
            raise ValueError(f"frozen case hash changed: {case_id}")
        run_id = f"stage0_nl_{index:02d}_{case_id}"
        output_path = runs_dir / f"{index:02d}-{case_id}.run-result.json"
        command = (
            sys.executable,
            str(RUNNER_PATH),
            "--case",
            case_id,
            "--run-id",
            run_id,
            "--raw-input",
            case.raw_input,
            "--case-sha256",
            case.sha256,
            "--out",
            str(output_path),
        )
        process = subprocess.run(
            command,
            cwd=str((PROJECT_ROOT / "week9").resolve()),
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=240.0,
            check=False,
        )
        parse_error = ""
        result = None
        try:
            payload = _read_object(output_path)
            result = RunResult.from_dict(payload)
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            parse_error = f"{type(exc).__name__}: {exc}"

        actual = _actual(result) if result is not None else {}
        mismatches = {
            key: {"expected": value, "actual": actual.get(key)}
            for key, value in expected.items()
            if actual.get(key) != value
        }
        if result is not None:
            actual_template = result.routing.certified_template_id
            if actual_template != case_spec["expected_template_id"]:
                mismatches["template_id"] = {
                    "expected": case_spec["expected_template_id"],
                    "actual": actual_template,
                }
        if process.returncode != 0:
            mismatches["runner_returncode"] = {
                "expected": 0,
                "actual": process.returncode,
            }
        if parse_error:
            mismatches["result_contract"] = {
                "expected": "valid RunResult",
                "actual": parse_error,
            }
        records.append(
            {
                "case_id": case_id,
                "case_sha256": case.sha256,
                "expected_template_id": case_spec["expected_template_id"],
                "actual": actual,
                "mismatches": mismatches,
                "mechanism_assertion_status": "PASS" if not mismatches else "FAIL",
                "run_result_file": str(output_path.relative_to(result_dir)).replace("\\", "/"),
                "run_result_sha256": (
                    _raw_sha256(output_path) if output_path.is_file() else ""
                ),
                "stdout_tail": process.stdout[-1000:],
                "stderr_tail": process.stderr[-1000:],
            }
        )

    pass_count = sum(item["mechanism_assertion_status"] == "PASS" for item in records)
    report = {
        "schema_version": "v8-stage0-natural-language-regression-1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "verdict": (
            "MECHANISM_REGRESSION_PASS"
            if pass_count == len(records)
            else "MECHANISM_REGRESSION_FAIL"
        ),
        "experiment_scope": "development_offline_natural_language_mechanism",
        "suite_sha256": _raw_sha256(SUITE_PATH),
        "offline_guard": suite["offline_guard"],
        "summary": {
            "planned_case_count": len(suite["cases"]),
            "run_case_count": len(records),
            "pass_count": pass_count,
            "fail_count": len(records) - pass_count,
        },
        "cases": records,
        "claim_status": {
            "natural_language_development_entry": "RUN",
            "online_model_generation": "NOT_RUN",
            "holdout_evaluation": "NOT_RUN",
            "v4_v6_v8_fair_comparison": "NOT_RUN",
            "cost_advantage_evaluation": "NOT_RUN",
            "generalization_evaluation": "NOT_RUN",
        },
        "limitations": [
            "The provider is an audited local grammar for certified templates, not general language understanding.",
            "Only frozen development requirements were used; holdout access remained disabled.",
            "Zero model tokens is an absolute local-run observation, not a cross-version cost claim.",
            "This report cannot establish V8 superiority, generalization, or formal release readiness.",
        ],
    }
    report_path = result_dir / "natural-language-regression-report.json"
    _write_json(report_path, report)
    digest = _raw_sha256(report_path)
    (result_dir / "natural-language-regression-report.sha256").write_text(
        f"{digest}  {report_path.name}\n", encoding="ascii"
    )
    summary_path = result_dir / "natural-language-regression-summary.md"
    summary_path.write_text(
        "# V8 Stage 0 自然语言离线机制回归\n\n"
        f"- 结论：`{report['verdict']}`\n"
        f"- 通过：{pass_count}/{len(records)}\n"
        "- 范围：冻结开发需求、本地确定性解释器、完整认证 pipeline。\n"
        "- 未运行：在线模型、V4/V6 对比、留出集、成本优势与泛化评估。\n",
        encoding="utf-8",
    )
    return report, result_dir


def main() -> int:
    report, result_dir = run_suite()
    print(
        f"Stage-0 natural-language regression: {report['verdict']} "
        f"({report['summary']['pass_count']}/{report['summary']['run_case_count']}) "
        f"-> {result_dir}",
        flush=True,
    )
    return 0 if report["verdict"] == "MECHANISM_REGRESSION_PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
