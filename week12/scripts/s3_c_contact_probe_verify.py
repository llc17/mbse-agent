"""CLI for exclusive C2 probe physical and base/refined reports."""

import argparse
import json
from pathlib import Path

from week12.src.integration.contact_probe_verify import compare, verify


def _save_exclusive(path: Path, report: dict) -> None:
    with path.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("ident", choices=("T01", "T02", "T03", "T04"))
    parser.add_argument("csv", type=Path)
    parser.add_argument("--base", type=Path)
    args = parser.parse_args()

    targets = [args.csv.with_name("physical.json")]
    if args.base is not None:
        targets.append(args.csv.with_name("numerical.json"))
    existing = [path.name for path in targets if path.exists()]
    if existing:
        print("refusing to overwrite " + ",".join(existing), flush=True)
        return 1

    physical = verify(args.ident, args.csv)
    try:
        _save_exclusive(args.csv.with_name("physical.json"), physical)
    except FileExistsError:
        print("refusing to overwrite physical.json", flush=True)
        return 1
    print("physical", physical["status"], flush=True)
    if physical["status"] != "PASS":
        return 1

    if args.base is not None:
        numerical = compare(args.ident, args.base, args.csv)
        try:
            _save_exclusive(args.csv.with_name("numerical.json"), numerical)
        except FileExistsError:
            print("refusing to overwrite numerical.json", flush=True)
            return 1
        print("numerical", numerical["status"], flush=True)
        if numerical["status"] != "PASS":
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
