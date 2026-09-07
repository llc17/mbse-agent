"""Validate an independently prepared V8 holdout without disclosing its content.

The validator reads the private artifact only inside this process.  Its return
value, report, console output, and failure codes never include case identifiers,
requirements, selected families, oracle values, or parameters.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any, Mapping

from week9.src.generation import DeterministicNaturalLanguageProvider
from week9.src.parameter_normalization import normalize_parameters


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_INTAKE_ROOT = Path(r"D:\v8_holdout_private")
MANIFEST_NAME = "independent_holdout.manifest.json"
MANIFEST_SIDECAR_NAME = "independent_holdout.manifest.sha256"
PRIVATE_NAME = "independent_holdout.private.json"
REPORT_NAME = "independent-holdout-validation-report.json"
REPORT_SCHEMA = "v8-independent-holdout-validation-report-1"
PASS_VERDICT = "INDEPENDENT_HOLDOUT_SEAL_PASS"
FAIL_VERDICT = "INDEPENDENT_HOLDOUT_SEAL_FAIL"

CORE_REQUIRED_CHECKS = [
    "requirement_normalization",
    "sysml_syntax",
    "modelica_compile",
    "parameter_unit_consistency",
    "simulation",
    "physical_behavior",
    "cross_model_consistency",
]
CHECK_NAMES = (
    "intake_path_isolated",
    "submission_files_exact",
    "manifest_contract_valid",
    "manifest_hash_matches_expected",
    "manifest_sidecar_matches",
    "private_artifact_integrity",
    "independence_declarations_valid",
    "role_separation_valid",
    "private_contract_valid",
    "required_checks_exact",
    "case_count_exact",
    "case_ids_valid_unique",
    "domain_balance_exact",
    "case_metadata_valid",
    "parameter_ranges_valid",
    "formula_recomputation_valid",
    "raw_requirement_grammar_valid",
    "oracle_matches_parser",
)
FORBIDDEN_SOURCE_MARKERS = (
    "openai",
    "gpt",
    "codex",
    "deepseek",
    "kimi",
    "moonshot",
)
CASE_ID = re.compile(r"^ih_[0-9a-f]{16}$")
SUBMISSION_DIRECTORY = re.compile(r"^submission_\d{8}T\d{6}Z$")
LOWER_SHA256 = re.compile(r"^[0-9a-f]{64}$")

EXPECTED_PHYSICS = {
    "rc_filter": {
        "validate_type": "rc_cutoff",
        "expected_value_source": "params.cutoff_freq",
        "tolerance_pct": 10.0,
    },
    "rlc_filter": {
        "validate_type": "rlc_resonant_freq",
        "expected_value_source": "params.cutoff_freq",
        "tolerance_pct": 15.0,
    },
    "thermal_dual_room": {
        "validate_type": "thermal_steady",
        "expected_value_source": "params.outdoor_temp",
        "tolerance_pct": 20.0,
    },
}
PARAMETER_UNITS = {
    "rc_filter": {
        "resistance": "Ohm",
        "capacitance": "F",
        "cutoff_frequency": "Hz",
        "input_voltage": "V",
    },
    "rlc_filter": {
        "resistance": "Ohm",
        "inductance": "H",
        "capacitance": "F",
        "cutoff_frequency": "Hz",
        "input_voltage": "V",
    },
    "thermal_dual_room": {
        "heat_capacity_a": "J/K",
        "heat_capacity_b": "J/K",
        "shared_resistance": "K/W",
        "outdoor_resistance_a": "K/W",
        "outdoor_resistance_b": "K/W",
        "initial_temperature_a": "K",
        "initial_temperature_b": "K",
        "outdoor_temperature": "K",
    },
}
PARAMETER_RANGES = {
    "rc_filter": {
        "resistance": (300.0, 10000.0),
        "cutoff_frequency": (700.0, 5000.0),
        "input_voltage": (1.0, 12.0),
    },
    "rlc_filter": {
        "resistance": (20.0, 200.0),
        "inductance": (0.002, 0.03),
        "cutoff_frequency": (2000.0, 15000.0),
        "input_voltage": (1.0, 10.0),
    },
    "thermal_dual_room": {
        "heat_capacity_a": (120000.0, 400000.0),
        "heat_capacity_b": (120000.0, 400000.0),
        "shared_resistance": (0.015, 0.08),
        "outdoor_resistance_a": (0.015, 0.08),
        "outdoor_resistance_b": (0.015, 0.08),
        "initial_temperature_a": (288.0, 299.0),
        "initial_temperature_b": (288.0, 299.0),
        "outdoor_temperature": (307.0, 315.0),
    },
}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_independent_holdout_seal(
    manifest_path: Path,
    *,
    expected_manifest_sha256: str,
    intake_root: Path = DEFAULT_INTAKE_ROOT,
) -> dict[str, Any]:
    """Return a content-free validation report for one sealed submission."""

    checks = {name: False for name in CHECK_NAMES}
    failures: list[str] = []
    manifest: dict[str, Any] = {}
    private: dict[str, Any] = {}
    manifest_digest = ""
    private_digest = ""
    private_byte_count = 0
    source_model_id = ""
    created_utc = ""
    declared_case_count: int | None = None

    try:
        root = intake_root.resolve(strict=True)
        path = manifest_path.resolve(strict=True)
        submission = path.parent
        isolated = (
            path.name == MANIFEST_NAME
            and submission.parent == root
            and SUBMISSION_DIRECTORY.fullmatch(submission.name) is not None
            and path.is_relative_to(root)
            and not any(_is_reparse(item) for item in (root, submission, path))
        )
        _record(checks, failures, "intake_path_isolated", isolated)
        names = {item.name for item in submission.iterdir() if item.is_file()}
        _record(
            checks,
            failures,
            "submission_files_exact",
            names == {MANIFEST_NAME, MANIFEST_SIDECAR_NAME, PRIVATE_NAME},
        )
    except OSError:
        _record(checks, failures, "intake_path_isolated", False)
        _record(checks, failures, "submission_files_exact", False)
        path = manifest_path
        submission = manifest_path.parent

    try:
        manifest_digest = file_sha256(path)
        manifest = _read_object(path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
        manifest = {}

    _record(
        checks,
        failures,
        "manifest_hash_matches_expected",
        bool(LOWER_SHA256.fullmatch(expected_manifest_sha256))
        and manifest_digest == expected_manifest_sha256,
    )

    sidecar = submission / MANIFEST_SIDECAR_NAME
    try:
        sidecar_digest = sidecar.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        sidecar_digest = ""
    _record(
        checks,
        failures,
        "manifest_sidecar_matches",
        bool(LOWER_SHA256.fullmatch(sidecar_digest))
        and sidecar_digest == manifest_digest,
    )

    manifest_contract = _manifest_contract_valid(manifest)
    _record(checks, failures, "manifest_contract_valid", manifest_contract)
    if manifest_contract:
        source_model_id = str(manifest["source_model_id"])
        created_utc = str(manifest["created_utc"])
        declared_case_count = int(manifest["case_count"])
    declarations = manifest.get("declarations")
    _record(
        checks,
        failures,
        "independence_declarations_valid",
        declarations
        == {
            "project_files_read": False,
            "prior_v8_content_seen": False,
            "independently_created": True,
            "plaintext_disclosed_to_v8_developer": False,
        },
    )
    source_lower = source_model_id.lower()
    _record(
        checks,
        failures,
        "role_separation_valid",
        bool(source_model_id.strip())
        and not any(marker in source_lower for marker in FORBIDDEN_SOURCE_MARKERS),
    )

    private_path: Path | None = None
    artifact = manifest.get("private_artifact")
    if isinstance(artifact, Mapping):
        file_name = artifact.get("file_name")
        if (
            isinstance(file_name, str)
            and file_name == PRIVATE_NAME
            and Path(file_name).name == file_name
        ):
            candidate = (submission / file_name).resolve()
            if candidate.parent == submission and candidate.is_file() and not _is_reparse(candidate):
                private_path = candidate
    if private_path is not None:
        try:
            private_digest = file_sha256(private_path)
            private_byte_count = private_path.stat().st_size
        except OSError:
            private_digest = ""
            private_byte_count = 0
    integrity = (
        private_path is not None
        and isinstance(artifact, Mapping)
        and artifact.get("sha256") == private_digest
        and artifact.get("byte_count") == private_byte_count
        and bool(LOWER_SHA256.fullmatch(private_digest))
    )
    _record(checks, failures, "private_artifact_integrity", integrity)

    if integrity and private_path is not None:
        try:
            private = _read_object(private_path)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError):
            private = {}
    private_results = _validate_private_contract(
        private,
        manifest_source=source_model_id,
        manifest_created_utc=created_utc,
    )
    for name in CHECK_NAMES[8:]:
        _record(checks, failures, name, private_results[name])

    verdict = PASS_VERDICT if all(checks.values()) else FAIL_VERDICT
    report = {
        "schema_version": REPORT_SCHEMA,
        "verdict": verdict,
        "validated_utc": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
        "public_binding": {
            "manifest_path": str(path),
            "manifest_sha256": manifest_digest,
            "private_artifact_sha256": private_digest,
            "private_artifact_byte_count": private_byte_count,
            "declared_case_count": declared_case_count,
            "source_model_id": source_model_id,
            "created_utc": created_utc,
        },
        "checks": checks,
        "failure_codes": sorted(set(failures)),
        "privacy_guard": {
            "private_content_emitted": False,
            "private_content_copied_into_project": False,
            "case_ids_emitted": False,
            "selected_families_emitted": False,
            "requirements_emitted": False,
            "parameters_emitted": False,
            "oracle_values_emitted": False,
            "old_holdout_accessed": False,
            "network_used": False,
            "model_api_called": False,
        },
    }
    return report


def write_validation_report(report: Mapping[str, Any], output_directory: Path) -> tuple[Path, str]:
    root = output_directory.resolve()
    if not root.is_relative_to(PROJECT_ROOT):
        raise ValueError("validation output directory must remain inside the project")
    root.mkdir(parents=True, exist_ok=False)
    report_path = root / REPORT_NAME
    payload = json.dumps(
        report,
        ensure_ascii=False,
        sort_keys=True,
        indent=2,
        allow_nan=False,
    ) + "\n"
    report_path.write_text(payload, encoding="utf-8", newline="\n")
    digest = file_sha256(report_path)
    report_path.with_suffix(".sha256").write_text(digest + "\n", encoding="ascii")
    return report_path, digest


def _validate_private_contract(
    private: Mapping[str, Any],
    *,
    manifest_source: str,
    manifest_created_utc: str,
) -> dict[str, bool]:
    result = {name: False for name in CHECK_NAMES[8:]}
    if not isinstance(private, Mapping):
        return result
    top_valid = _exact_keys(private, {"schema_version", "curation", "required_checks", "cases"})
    top_valid = top_valid and private.get("schema_version") == "v8-independent-holdout-private-1"
    curation = private.get("curation")
    expected_curation_keys = {
        "source_model_id",
        "created_utc",
        "project_files_read",
        "prior_v8_content_seen",
        "independently_created",
        "selection_method",
    }
    curation_valid = isinstance(curation, Mapping) and _exact_keys(
        curation, expected_curation_keys
    )
    if curation_valid:
        curation_valid = (
            curation.get("source_model_id") == manifest_source
            and curation.get("created_utc") == manifest_created_utc
            and curation.get("project_files_read") is False
            and curation.get("prior_v8_content_seen") is False
            and curation.get("independently_created") is True
            and curation.get("selection_method") == "os_cryptographic_random"
        )
    result["private_contract_valid"] = bool(top_valid and curation_valid)
    result["required_checks_exact"] = private.get("required_checks") == CORE_REQUIRED_CHECKS

    cases = private.get("cases")
    result["case_count_exact"] = isinstance(cases, list) and len(cases) == 2
    if not result["case_count_exact"]:
        return result

    ids: list[str] = []
    domains: list[str] = []
    templates: list[str] = []
    metadata_valid = True
    ranges_valid = True
    formulas_valid = True
    grammar_valid = True
    oracle_matches = True
    provider = DeterministicNaturalLanguageProvider()

    for case in cases:
        if not isinstance(case, Mapping) or not _exact_keys(
            case,
            {
                "case_id",
                "dataset",
                "domain",
                "raw_input",
                "expected_physics",
                "oracle",
                "tags",
            },
        ):
            metadata_valid = False
            ranges_valid = False
            formulas_valid = False
            grammar_valid = False
            oracle_matches = False
            continue
        case_id = case.get("case_id")
        domain = case.get("domain")
        raw_input = case.get("raw_input")
        oracle = case.get("oracle")
        if isinstance(case_id, str):
            ids.append(case_id)
        if isinstance(domain, str):
            domains.append(domain)
        if not isinstance(oracle, Mapping) or not _exact_keys(
            oracle,
            {"template_id", "canonical_parameters", "derived_formula", "expected_behavior"},
        ):
            metadata_valid = False
            ranges_valid = False
            formulas_valid = False
            grammar_valid = False
            oracle_matches = False
            continue
        template_id = oracle.get("template_id")
        if isinstance(template_id, str):
            templates.append(template_id)
        expected_domain = (
            "electrical" if template_id in {"rc_filter", "rlc_filter"}
            else "thermal" if template_id == "thermal_dual_room"
            else ""
        )
        metadata_valid = metadata_valid and (
            case.get("dataset") == "holdout"
            and domain == expected_domain
            and isinstance(raw_input, str)
            and bool(raw_input.strip())
            and case.get("tags")
            == ["independent_new_holdout", "do_not_use_for_development"]
            and isinstance(oracle.get("derived_formula"), str)
            and bool(str(oracle.get("derived_formula")).strip())
            and isinstance(oracle.get("expected_behavior"), str)
            and bool(str(oracle.get("expected_behavior")).strip())
            and case.get("expected_physics") == EXPECTED_PHYSICS.get(template_id)
        )
        values, parameters_valid = _oracle_values(template_id, oracle.get("canonical_parameters"))
        ranges_valid = ranges_valid and parameters_valid and _ranges_valid(template_id, values)
        formulas_valid = formulas_valid and parameters_valid and _formula_valid(template_id, values)

        try:
            projection = provider.generate(raw_input) if isinstance(raw_input, str) else None
            local_grammar_ok = (
                projection is not None
                and not projection.issues
                and projection.template_id == template_id
            )
        except (TypeError, ValueError, ArithmeticError):
            projection = None
            local_grammar_ok = False
        grammar_valid = grammar_valid and local_grammar_ok
        if local_grammar_ok and projection is not None and parameters_valid:
            try:
                normalized = normalize_parameters(template_id, projection.observations)
                actual = {
                    item.canonical_name: (float(item.value), item.unit)
                    for item in normalized.parameters
                    if item.value is not None
                }
                expected = {
                    name: (value, PARAMETER_UNITS[template_id][name])
                    for name, value in values.items()
                }
                same = normalized.ready and set(actual) == set(expected)
                if same:
                    same = all(
                        actual[name][1] == unit
                        and math.isclose(actual[name][0], value, rel_tol=1e-9, abs_tol=1e-12)
                        for name, (value, unit) in expected.items()
                    )
            except (TypeError, ValueError, ArithmeticError):
                same = False
            oracle_matches = oracle_matches and same
        else:
            oracle_matches = False

    result["case_ids_valid_unique"] = (
        len(ids) == 2
        and len(set(ids)) == 2
        and all(CASE_ID.fullmatch(case_id) is not None for case_id in ids)
    )
    result["domain_balance_exact"] = (
        sorted(domains) == ["electrical", "thermal"]
        and len(templates) == 2
        and templates.count("thermal_dual_room") == 1
        and sum(item in {"rc_filter", "rlc_filter"} for item in templates) == 1
    )
    result["case_metadata_valid"] = metadata_valid
    result["parameter_ranges_valid"] = ranges_valid
    result["formula_recomputation_valid"] = formulas_valid
    result["raw_requirement_grammar_valid"] = grammar_valid
    result["oracle_matches_parser"] = oracle_matches
    return result


def _manifest_contract_valid(manifest: Mapping[str, Any]) -> bool:
    if not isinstance(manifest, Mapping) or not _exact_keys(
        manifest,
        {
            "schema_version",
            "private_artifact",
            "case_count",
            "source_model_id",
            "created_utc",
            "declarations",
        },
    ):
        return False
    artifact = manifest.get("private_artifact")
    if not isinstance(artifact, Mapping) or not _exact_keys(
        artifact, {"file_name", "sha256", "byte_count"}
    ):
        return False
    timestamp = manifest.get("created_utc")
    return (
        manifest.get("schema_version") == "v8-independent-holdout-manifest-1"
        and artifact.get("file_name") == PRIVATE_NAME
        and isinstance(artifact.get("sha256"), str)
        and LOWER_SHA256.fullmatch(str(artifact.get("sha256"))) is not None
        and type(artifact.get("byte_count")) is int
        and artifact.get("byte_count") > 0
        and manifest.get("case_count") == 2
        and isinstance(manifest.get("source_model_id"), str)
        and bool(str(manifest.get("source_model_id")).strip())
        and isinstance(timestamp, str)
        and _valid_utc(timestamp)
        and isinstance(manifest.get("declarations"), Mapping)
    )


def _oracle_values(template_id: Any, parameters: Any) -> tuple[dict[str, float], bool]:
    if template_id not in PARAMETER_UNITS or not isinstance(parameters, Mapping):
        return {}, False
    expected_units = PARAMETER_UNITS[template_id]
    if set(parameters) != set(expected_units):
        return {}, False
    values: dict[str, float] = {}
    for name, unit in expected_units.items():
        record = parameters.get(name)
        if not isinstance(record, Mapping) or not _exact_keys(record, {"value", "unit"}):
            return {}, False
        value = record.get("value")
        if type(value) not in {int, float} or not math.isfinite(float(value)):
            return {}, False
        if record.get("unit") != unit:
            return {}, False
        values[name] = float(value)
    return values, True


def _ranges_valid(template_id: Any, values: Mapping[str, float]) -> bool:
    if template_id not in PARAMETER_RANGES:
        return False
    for name, (minimum, maximum) in PARAMETER_RANGES[template_id].items():
        if name not in values or not minimum <= values[name] <= maximum:
            return False
    if values.get("capacitance", 1.0) <= 0:
        return False
    if template_id == "thermal_dual_room":
        return (
            values["heat_capacity_a"] != values["heat_capacity_b"]
            and values["initial_temperature_a"] != values["initial_temperature_b"]
            and values["outdoor_temperature"] >= values["initial_temperature_a"] + 8.0
            and values["outdoor_temperature"] >= values["initial_temperature_b"] + 8.0
        )
    return True


def _formula_valid(template_id: Any, values: Mapping[str, float]) -> bool:
    try:
        if template_id == "rc_filter":
            expected = 1.0 / (
                2.0 * math.pi * values["resistance"] * values["cutoff_frequency"]
            )
        elif template_id == "rlc_filter":
            expected = 1.0 / (
                ((2.0 * math.pi * values["cutoff_frequency"]) ** 2)
                * values["inductance"]
            )
        elif template_id == "thermal_dual_room":
            return True
        else:
            return False
        return math.isclose(values["capacitance"], expected, rel_tol=1e-9, abs_tol=0.0)
    except (KeyError, TypeError, ValueError, ZeroDivisionError, OverflowError):
        return False


def _record(
    checks: dict[str, bool], failures: list[str], name: str, passed: Any
) -> None:
    value = bool(passed)
    checks[name] = value
    if not value:
        failures.append(name)


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON root must be an object")
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str]) -> bool:
    return set(value) == expected


def _valid_utc(value: str) -> bool:
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        return False
    return True


def _is_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return True
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return path.is_symlink() or bool(marker and attributes & marker)


def _default_output_directory() -> Path:
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    return PROJECT_ROOT / "week9" / "results" / f"independent-holdout-intake-{stamp}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument("--intake-root", default=str(DEFAULT_INTAKE_ROOT))
    parser.add_argument("--output-dir")
    args = parser.parse_args(argv)
    try:
        report = validate_independent_holdout_seal(
            Path(args.manifest),
            expected_manifest_sha256=args.expected_manifest_sha256,
            intake_root=Path(args.intake_root),
        )
        report_path, digest = write_validation_report(
            report,
            Path(args.output_dir) if args.output_dir else _default_output_directory(),
        )
    except Exception:  # fail closed without printing private values or exception text
        print("INDEPENDENT_HOLDOUT_VALIDATOR_ERROR", file=sys.stderr)
        return 2
    print(report["verdict"])
    print(f"report_path={report_path}")
    print(f"report_sha256={digest}")
    if report["verdict"] != PASS_VERDICT:
        print("failure_codes=" + ",".join(report["failure_codes"]), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "DEFAULT_INTAKE_ROOT",
    "FAIL_VERDICT",
    "PASS_VERDICT",
    "REPORT_NAME",
    "REPORT_SCHEMA",
    "file_sha256",
    "main",
    "validate_independent_holdout_seal",
    "write_validation_report",
]
