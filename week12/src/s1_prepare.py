"""Prepare the auditable, unconfirmed V11 S1 candidate record."""
from __future__ import annotations

import argparse
from pathlib import Path

from .specification import write_s1_artifact


def main() -> int:
    parser = argparse.ArgumentParser(description="Prepare V11 S1 specification without simulation")
    parser.add_argument("--mission", type=Path, default=Path(__file__).resolve().parents[1] / "configs" / "patrol_mission_v1.json")
    parser.add_argument("--output", type=Path, default=Path(__file__).resolve().parents[1] / "results" / "s1-spec-20260918")
    parser.add_argument("--project-id", default="v11-patrol-design")
    parser.add_argument("--baseline-version-id", default="s1-mission-requirements-v1")
    args = parser.parse_args()
    write_s1_artifact(args.mission, args.output, project_id=args.project_id, baseline_version_id=args.baseline_version_id)
    print(f"prepared pending S1 specification: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
