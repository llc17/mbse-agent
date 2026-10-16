"""CLI entry point for the confirmed V11 S2 real-toolchain run."""
from __future__ import annotations

import argparse
from pathlib import Path

from .s2_real import RealS2Executor, RealS2Generator
from .s2_runner import S2Runner


def main() -> int:
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description="Run one confirmed V11 S2 candidate through SysML/OpenModelica")
    parser.add_argument("--spec", type=Path, default=root / "results" / "s1-spec-20260918" / "spec.json")
    parser.add_argument("--output", type=Path, required=True, help="new evidence directory inside week12")
    args = parser.parse_args()
    result = S2Runner(RealS2Generator(), RealS2Executor()).run(args.spec, args.output)
    print(result["candidate_id"])
    print(result["physical_statuses"] if "physical_statuses" in result else {name: row["physical_status"] for name, row in result["scenarios"].items()})
    return 0 if all(row["status"] in {"PASS", "FAIL", "UNVERIFIED"} for row in result["scenarios"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
