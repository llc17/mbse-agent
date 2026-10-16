from __future__ import annotations
import argparse
from pathlib import Path
from .car_model import create_candidate

def main() -> int:
    parser = argparse.ArgumentParser(description="V11 independent car modeling CLI")
    parser.add_argument("command", nargs="?", default="first-car", choices=("first-car",))
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    result = create_candidate(args.root)
    print(result)
    return 0 if result["sysml_check"] == "passed" and result["modelica_static_check"] == "passed" else 1

if __name__ == "__main__":
    raise SystemExit(main())
