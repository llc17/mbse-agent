"""Run a bounded one-step contact diagnostic without changing the confirmed spec."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from week12.src.s2_real import RealS2Executor, RealS2Generator


ROOT = Path(__file__).resolve().parents[1]
SPEC_PATH = ROOT / "results" / "s2-confirmed-20260918" / "spec.json"
OUTPUT = ROOT / "results" / "s3-r5-single-step-20260918-01"


def _write(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")


def main() -> int:
    if OUTPUT.exists():
        raise FileExistsError(f"diagnostic output already exists: {OUTPUT}")
    original = json.loads(SPEC_PATH.read_text(encoding="utf-8"))
    diagnostic = copy.deepcopy(original)
    original_digest = original["spec_digest"]
    diagnostic["stair"]["count"] = 1
    diagnostic["scenarios"]["ascent"]["steps"] = 1
    diagnostic["scenarios"]["descent"]["steps"] = 1
    diagnostic["diagnostic_override"] = {
        "purpose": "single_step_contact_only",
        "source_spec_digest": original_digest,
        "stair_count_override": 1,
        "product_acceptance": "not_claimed",
    }
    OUTPUT.mkdir(parents=True)
    _write(OUTPUT / "diagnostic-input.json", diagnostic)
    generator = RealS2Generator()
    executor = RealS2Executor(ascent_descent_stop_s=20.0, flat_stop_s=5.0, timeout_s=60.0)
    scenarios: dict[str, object] = {}
    for scenario in ("ascent", "descent"):
        directory = OUTPUT / scenario
        directory.mkdir()
        design = dict(diagnostic["candidate"]["design"])
        design["candidate_id"] = diagnostic["candidate"]["candidate_id"]
        scenario_input = {
            "schema_version": "v11-s3-single-step-diagnostic-1",
            "scenario": scenario,
            "candidate_id": diagnostic["candidate"]["candidate_id"],
            "source_spec_digest": original_digest,
            "design": design,
            "stair": diagnostic["stair"],
            "assumptions": diagnostic["assumptions"],
            "diagnostic_override": diagnostic["diagnostic_override"],
        }
        _write(directory / "input.json", scenario_input)
        generated = generator.generate(diagnostic, scenario, directory)
        _write(directory / "generated.json", generated)
        result = dict(executor.execute(scenario_input, generated, directory))
        _write(directory / "tool-result.json", result)
        _write(directory / "record.json", {"scenario": scenario, "physical_status": result.get("physical_status"), "tool_status": result.get("tool_status"), "diagnostic": True})
        scenarios[scenario] = result
    result_summary = {
        "schema_version": "v11-s3-single-step-diagnostic-result-1",
        "source_spec_digest": original_digest,
        "diagnostic_override": diagnostic["diagnostic_override"],
        "scenarios": {name: {"tool_status": value.get("tool_status"), "physical_status": value.get("physical_status"), "metrics": value.get("metrics"), "physical_criteria": value.get("physical_criteria")} for name, value in scenarios.items()},
        "product_acceptance": "not_claimed",
    }
    _write(OUTPUT / "result.json", result_summary)
    files = {}
    for path in sorted(OUTPUT.rglob("*")):
        if path.is_file() and path.name not in {"manifest.json"}:
            files[str(path.relative_to(OUTPUT)).replace("\\", "/")] = hashlib.sha256(path.read_bytes()).hexdigest()
    _write(OUTPUT / "manifest.json", {"schema_version": "v11-s3-single-step-diagnostic-manifest-1", "source_spec_digest": original_digest, "files": files})
    print({name: value.get("physical_status") for name, value in scenarios.items()})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
