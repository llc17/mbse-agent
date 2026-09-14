"""Build a new, relocatable package for bounded V9 public-evidence queries."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import stat
import sys
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from week10.src import trace_cli


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = PROJECT_ROOT / "week10" / "results"
DELIVERABLES_ROOT = PROJECT_ROOT / "week10" / "deliverables"
PACKAGE_SCOPE = "offline_queries_only"

SOURCE_RELATIVE_PATHS = (
    Path("week9/src/schemas.py"),
    Path("week10/src/trace_graph.py"),
    Path("week10/src/trace_cli.py"),
    Path("week10/src/graph_retrieval_v2.py"),
    Path("week10/src/graph_answer.py"),
    Path("week10/src/graph_answer_v2.py"),
    Path("week10/src/graphrag_v2_cli.py"),
    Path("week10/src/graph_evidence_view.py"),
    Path("week10/src/graph_evidence_cli.py"),
    Path("week10/src/dependency_trace.py"),
    Path("week10/src/dependency_cli.py"),
    Path("week10/src/v9_cli.py"),
)
OPTIONAL_INIT_PATHS = (
    Path("week9/__init__.py"),
    Path("week9/src/__init__.py"),
    Path("week10/__init__.py"),
    Path("week10/src/__init__.py"),
)


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise ValueError(message)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _is_reparse(path: Path) -> bool:
    try:
        info = path.lstat()
    except OSError as exc:
        raise ValueError(f"unavailable path: {path}") from exc
    attributes = getattr(info, "st_file_attributes", 0)
    return path.is_symlink() or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _reject_reparse_chain(path: Path, boundary: Path) -> None:
    """Reject links/junctions from an inclusive boundary down to an existing path."""
    unresolved_boundary = boundary.absolute()
    unresolved_path = path.absolute()
    try:
        relative = unresolved_path.relative_to(unresolved_boundary)
    except ValueError as exc:
        raise ValueError("path lies outside its required boundary") from exc
    current = unresolved_boundary
    if current.exists() and _is_reparse(current):
        raise ValueError("reparse points are not allowed")
    for part in relative.parts:
        current = current / part
        if current.exists() and _is_reparse(current):
            raise ValueError("reparse points are not allowed")


def _checked_file(path: Path, boundary: Path, label: str) -> tuple[Path, bytes]:
    _reject_reparse_chain(path, boundary)
    try:
        resolved_boundary = boundary.resolve(strict=True)
        resolved = path.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc
    if not _inside(resolved, resolved_boundary) or not resolved.is_file():
        raise ValueError(f"{label} must be a regular file inside its allowed root")
    try:
        return resolved, resolved.read_bytes()
    except OSError as exc:
        raise ValueError(f"{label} is unavailable") from exc


def _destination_path(destination: Path) -> Path:
    try:
        project_root = PROJECT_ROOT.resolve(strict=True)
        delivery_root = DELIVERABLES_ROOT.resolve(strict=True)
    except OSError as exc:
        raise ValueError("project or deliverables root is unavailable") from exc
    if not _inside(delivery_root, project_root):
        raise ValueError("deliverables root must be inside the project")
    candidate = destination.resolve(strict=False)
    if candidate == delivery_root or not _inside(candidate, delivery_root):
        raise ValueError("destination must be a new directory under week10/deliverables")
    if candidate.exists():
        raise ValueError("destination already exists")
    if not candidate.parent.exists() or not candidate.parent.is_dir():
        raise ValueError("destination parent directory must already exist")
    _reject_reparse_chain(candidate.parent, DELIVERABLES_ROOT)
    if candidate.parent.resolve(strict=True) != candidate.parent.resolve(strict=False):
        raise ValueError("destination parent is not stable")
    return candidate


def _source_payloads() -> dict[Path, tuple[bytes, str]]:
    payloads: dict[Path, tuple[bytes, str]] = {}
    for relative in SOURCE_RELATIVE_PATHS:
        source, raw = _checked_file(PROJECT_ROOT / relative, PROJECT_ROOT, "query source")
        if source.relative_to(PROJECT_ROOT.resolve(strict=True)) != relative:
            raise ValueError("query source resolved to an unexpected location")
        payloads[relative] = (raw, relative.as_posix())
    for relative in OPTIONAL_INIT_PATHS:
        source_path = PROJECT_ROOT / relative
        if not source_path.exists():
            continue
        source, raw = _checked_file(source_path, PROJECT_ROOT, "package initializer")
        if source.relative_to(PROJECT_ROOT.resolve(strict=True)) != relative:
            raise ValueError("package initializer resolved to an unexpected location")
        payloads[relative] = (raw, relative.as_posix())
    return payloads


def _manifest_payloads(manifest_paths: list[Path]) -> tuple[dict[Path, tuple[bytes, str]], list[str]]:
    if not manifest_paths:
        raise ValueError("at least one --manifest is required")
    payloads: dict[Path, tuple[bytes, str]] = {}
    manifest_relatives: list[str] = []
    seen_manifests: set[Path] = set()
    project_root = PROJECT_ROOT.resolve(strict=True)
    results_root = RESULTS_ROOT.resolve(strict=True)
    for supplied in manifest_paths:
        _reject_reparse_chain(supplied, RESULTS_ROOT)
        manifest = supplied.resolve(strict=True)
        if manifest in seen_manifests:
            raise ValueError("duplicate manifest")
        seen_manifests.add(manifest)
        if not _inside(manifest, results_root):
            raise ValueError("manifest must be under week10/results")

        public_paths = trace_cli._public_run_paths(manifest)
        runs = trace_cli.load_runs(manifest)
        if [run.run_id for run in runs] != [run_id for run_id, _, _ in public_paths]:
            raise ValueError("loaded runs do not match manifest order")
        manifest_file, manifest_raw = _checked_file(manifest, RESULTS_ROOT, "manifest")
        manifest_relative = manifest_file.relative_to(project_root)
        manifest_relatives.append(manifest_relative.as_posix())
        payloads[manifest_relative] = (manifest_raw, manifest_relative.as_posix())

        try:
            manifest_data = json.loads(manifest_raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("invalid manifest") from exc
        entries = manifest_data["runs"]
        for entry, (run_id, resolved_run, expected_sha) in zip(entries, public_paths, strict=True):
            if entry.get("run_id") != run_id:
                raise ValueError("manifest run order changed during validation")
            raw_relative = Path(entry["path"].replace("\\", "/"))
            unresolved_run = PROJECT_ROOT / raw_relative
            _reject_reparse_chain(unresolved_run, RESULTS_ROOT)
            checked_run, run_raw = _checked_file(unresolved_run, RESULTS_ROOT, "public RunResult")
            if checked_run != resolved_run or hashlib.sha256(run_raw).hexdigest() != expected_sha:
                raise ValueError("public RunResult changed during validation")
            if raw_relative in payloads and payloads[raw_relative][0] != run_raw:
                raise ValueError("conflicting package-relative inputs")
            payloads[raw_relative] = (run_raw, raw_relative.as_posix())
    return payloads, manifest_relatives


def _readme(manifests: list[str]) -> bytes:
    default_manifest = manifests[0]
    text = f"""# V9 portable public-evidence query package

This package contains hash-bound public RunResult records and the query-only Python
modules needed to inspect them. It does not reproduce generation, authentication,
OpenModelica simulation, or a clean-machine rerun. No API call was made while the
package was built.

The acceptance environment uses Python 3.13. Run from the package root:

```text
python -B -m week10.src.v9_cli --help
python -B -m week10.src.v9_cli trace --manifest {default_manifest} graph
python -B -m week10.src.v9_cli dependency --manifest {default_manifest} graph
python -B -m week10.src.v9_cli evidence --manifest {default_manifest} --question "..." --run-id RUN_ID
python -B -m week10.src.v9_cli ask --manifest {default_manifest} --question "..." --run-id RUN_ID
```

`evidence` defaults to Markdown. `ask` defaults to offline retrieval. Its optional
`--answer` flag explicitly enables a model request and requires credentials supplied
by the operator; no credentials are included. Model answers are checked for required
evidence citations, but semantic correctness is not validated. Trace and dependency
results describe explicit recorded evidence and do not prove arbitrary physical
causality or the minimal system rerun set.
"""
    return text.encode("utf-8")


def build_bundle(destination: Path, manifest_paths: list[Path]) -> dict[str, Any]:
    """Validate every input, then create a new package without overwriting files."""
    target = _destination_path(Path(destination))
    payloads = _source_payloads()
    manifest_payloads, manifests = _manifest_payloads([Path(item) for item in manifest_paths])
    for relative, payload in manifest_payloads.items():
        if relative in payloads and payloads[relative][0] != payload[0]:
            raise ValueError("conflicting package-relative inputs")
        payloads[relative] = payload
    readme = _readme(manifests)
    payloads[Path("PACKAGE_README.md")] = (readme, "generated:PACKAGE_README.md")

    file_records = [
        {
            "path": relative.as_posix(),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "source": source,
        }
        for relative, (raw, source) in sorted(payloads.items(), key=lambda item: item[0].as_posix())
    ]
    package_manifest: dict[str, Any] = {
        "schema_version": "v9-portable-query-package-1",
        "package_scope": PACKAGE_SCOPE,
        "api_calls": 0,
        "manifests": manifests,
        "files": file_records,
        "limitations": [
            "offline query and evidence reproduction only",
            "does not claim clean-machine OpenModelica resimulation",
            "model answers are not semantically validated",
        ],
    }
    package_manifest_raw = (json.dumps(
        package_manifest, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2
    ) + "\n").encode("utf-8")

    target.mkdir(exist_ok=False)
    for relative, (raw, _) in sorted(payloads.items(), key=lambda item: item[0].as_posix()):
        output = target / relative
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("xb") as stream:
            stream.write(raw)
    with (target / "package-manifest.json").open("xb") as stream:
        stream.write(package_manifest_raw)
    return package_manifest


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -B -m week10.tools.build_portable_v9")
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, action="append", required=True, dest="manifests")
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if hasattr(sys.stderr, "reconfigure"):
        sys.stderr.reconfigure(encoding="utf-8")
    try:
        args = _parser().parse_args(argv)
        result = build_bundle(args.destination, args.manifests)
    except KeyboardInterrupt:
        raise
    except (ValueError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, allow_nan=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
