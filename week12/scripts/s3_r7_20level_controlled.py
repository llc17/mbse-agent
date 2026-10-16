"""Run the one-shot V11 S3 R7 20-level controlled batch.

This script keeps the confirmed specification and physical acceptance limits
unchanged.  It applies only an explicitly recorded execution-horizon override
so the generated command remains active long enough to traverse 20 steps.
There is one generator and one executor call per scenario, with no retry.
"""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys
import time
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from week12.src.s2_real import RealS2Executor, RealS2Generator
from week12.src.specification import load_spec


ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "results" / "s2-confirmed-20260918" / "spec.json"
OUTPUT = ROOT / "results" / "s3-r7-20level-controlled-20260922-01"
SCENARIOS = ("ascent", "descent", "flat")


def _write(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def _hash_files(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name not in {"manifest.json", "sha256sums.txt"}:
            result[str(path.relative_to(root)).replace("\\", "/")] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def main() -> int:
    if OUTPUT.exists():
        raise FileExistsError(f"controlled batch output already exists: {OUTPUT}")

    # Validate the actual confirmed input before making the execution-only copy.
    original = load_spec(SPEC_PATH, require_generation_ready=True)
    source_digest = str(original["spec_digest"])
    batch = copy.deepcopy(original)
    batch["execution_control"] = {
        "purpose": "s3_r7_full_20_level_controlled_batch",
        "source_spec_digest": source_digest,
        "product_acceptance": "not_claimed",
        "stair_execution_horizon_s": 180.0,
        "flat_execution_horizon_s": 10.0,
        "scenario_wall_timeout_s": 240.0,
        "request_budget": {"generator_calls": 3, "executor_calls": 3, "online_requests": 0},
        "retry_policy": "none",
        "stop_conditions": [
            "stop the current scenario on compile, simulation, nonfinite CSV, or timeout failure",
            "preserve all partial evidence; do not retry",
            "run each remaining scheduled scenario at most once if the process remains within its finite budget",
        ],
    }
    # These values only extend the generated command/simulation horizon.  The
    # confirmed 20 kg design, geometry, and physical acceptance thresholds are
    # carried over verbatim and remain visible in batch-input.json.
    batch["assumptions"]["stair_stop_s"] = 180.0
    batch["assumptions"]["flat_stop_s"] = 10.0

    OUTPUT.mkdir(parents=True)
    _write(OUTPUT / "batch-input.json", batch)
    _write(OUTPUT / "run-control.json", batch["execution_control"])

    generator = RealS2Generator()
    executor = RealS2Executor(ascent_descent_stop_s=180.0, flat_stop_s=10.0, timeout_s=240.0)
    scenarios: dict[str, dict[str, Any]] = {}
    started = time.monotonic()

    for scenario in SCENARIOS:
        directory = OUTPUT / scenario
        directory.mkdir()
        design = dict(batch["candidate"]["design"])
        design["candidate_id"] = batch["candidate"]["candidate_id"]
        scenario_input = {
            "schema_version": "v11-s3-r7-20level-controlled-input-1",
            "scenario": scenario,
            "candidate_id": batch["candidate"]["candidate_id"],
            "source_spec_digest": source_digest,
            "design": design,
            "stair": batch["stair"],
            "assumptions": batch["assumptions"],
            "execution_control": batch["execution_control"],
        }
        _write(directory / "input.json", scenario_input)
        record: dict[str, Any] = {
            "scenario": scenario,
            "attempts": 0,
            "tool_status": "UNVERIFIED",
            "physical_status": "UNVERIFIED",
        }
        try:
            generated = generator.generate(batch, scenario, directory)
            _write(directory / "generated.json", generated)
            record["generated_file"] = "generated.json"
            record["attempts"] = 1
            result = dict(executor.execute(scenario_input, generated, directory))
            _write(directory / "tool-result.json", result)
            record.update({
                "tool_status": result.get("tool_status", "UNVERIFIED"),
                "physical_status": result.get("physical_status", "UNVERIFIED"),
                "physical_criteria": result.get("physical_criteria", []),
                "metrics": result.get("metrics", {}),
                "result_file": "tool-result.json",
            })
        except Exception as exc:  # preserve a failed scenario and continue once
            error = {"status": "ERROR", "tool_status": "ERROR", "physical_status": "ERROR", "issues": [f"{type(exc).__name__}: {exc}"]}
            _write(directory / "tool-result.json", error)
            record.update({"tool_status": "ERROR", "physical_status": "ERROR", "error": error["issues"][0], "result_file": "tool-result.json"})
        _write(directory / "record.json", record)
        scenarios[scenario] = record

    result_summary = {
        "schema_version": "v11-s3-r7-20level-controlled-result-1",
        "source_spec_digest": source_digest,
        "execution_control": batch["execution_control"],
        "product_acceptance": "not_claimed",
        "elapsed_wall_s": time.monotonic() - started,
        "scenarios": scenarios,
    }
    _write(OUTPUT / "result.json", result_summary)
    manifest = {
        "schema_version": "v11-s3-r7-20level-controlled-manifest-1",
        "source_spec_digest": source_digest,
        "product_acceptance": "not_claimed",
        "scenarios": list(SCENARIOS),
        "execution_control": batch["execution_control"],
        "files": _hash_files(OUTPUT),
    }
    _write(OUTPUT / "manifest.json", manifest)
    sums = {**manifest["files"], "manifest.json": hashlib.sha256((OUTPUT / "manifest.json").read_bytes()).hexdigest()}
    (OUTPUT / "sha256sums.txt").write_text("".join(f"{digest}  {name}\n" for name, digest in sorted(sums.items())), encoding="utf-8", newline="\n")
    print(json.dumps({name: row["physical_status"] for name, row in scenarios.items()}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
