"""Freeze the public binding for the independently sealed V8 holdout.

This verifier reads the public manifest and the content-free validation report.
It hashes the private artifact as opaque bytes but never parses or emits it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import stat
import sys
from typing import Any, Mapping

from week9.experiments.validate_independent_holdout_seal import (
    CHECK_NAMES,
    MANIFEST_NAME,
    MANIFEST_SIDECAR_NAME,
    PASS_VERDICT,
    PRIVATE_NAME,
    REPORT_SCHEMA,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
RECOVERY_PROTOCOL = PROJECT_ROOT / "week9" / "configs" / "formal_recovery_protocol_v2.json"
RECOVERY_DIGEST = RECOVERY_PROTOCOL.with_suffix(".sha256")
DEFAULT_BINDING = (
    PROJECT_ROOT / "week9" / "configs" / "independent_holdout_binding_v1.json"
)
BINDING_DIGEST = DEFAULT_BINDING.with_suffix(".sha256")
INTAKE_ROOT = Path(r"D:\v8_holdout_private")
SUBMISSION_DIRECTORY = re.compile(r"^submission_\d{8}T\d{6}Z$")
LOWER_SHA256 = re.compile(r"^[0-9a-f]{64}$")
IMPLEMENTATION_FILES = (
    "experiments/validate_independent_holdout_seal.py",
    "experiments/freeze_independent_holdout_binding.py",
    "tests/test_independent_holdout_seal.py",
)

EXPECTED_PRIVACY_GUARD = {
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
}
EXPECTED_GATE_STATUS = {
    "offline_preflight_passed": True,
    "outer_harness_implemented": True,
    "formal_adapter_integration_complete": True,
    "independent_holdout_manifest_bound": True,
    "development_dry_run_passed": True,
    "new_user_confirmation_recorded": False,
}


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_sha256(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_binding_protocol(binding: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    recovery = _read_object(RECOVERY_PROTOCOL, errors, "recovery protocol")
    recovery_digest = _read_digest(RECOVERY_DIGEST, errors, "recovery digest")
    if recovery and recovery_digest and canonical_sha256(recovery) != recovery_digest:
        errors.append("frozen recovery protocol digest differs")

    if binding.get("schema_version") != "v8-independent-holdout-binding-1":
        errors.append("binding schema differs")
    if binding.get("status") != "INDEPENDENT_HOLDOUT_MANIFEST_BOUND_OFFLINE":
        errors.append("binding status differs")
    if binding.get("formal_online_runs_allowed") is not False:
        errors.append("binding must not enable formal online runs")
    if binding.get("formal_judge_allowed") is not False:
        errors.append("binding must not enable the formal Judge")

    expected_binding_digest = _read_digest(BINDING_DIGEST, errors, "binding digest")
    if expected_binding_digest and canonical_sha256(binding) != expected_binding_digest:
        errors.append("binding differs from its frozen digest")

    baseline = binding.get("baseline_recovery_protocol")
    expected_baseline = {
        "path": "week9/configs/formal_recovery_protocol_v2.json",
        "sha256": recovery_digest,
        "status": "offline_recovery_complete_new_holdout_required",
        "unchanged": True,
    }
    if baseline != expected_baseline:
        errors.append("baseline recovery binding differs")

    source = binding.get("source_submission")
    if not isinstance(source, Mapping):
        errors.append("source submission must be an object")
        source = {}
    manifest_path = _safe_external_manifest(source.get("manifest_path"), errors)
    manifest: dict[str, Any] = {}
    if manifest_path is not None:
        manifest_digest = file_sha256(manifest_path) if manifest_path.is_file() else ""
        if manifest_digest != source.get("manifest_sha256"):
            errors.append("external manifest hash differs")
        sidecar = manifest_path.with_name(MANIFEST_SIDECAR_NAME)
        if _read_digest(sidecar, errors, "external manifest sidecar") != manifest_digest:
            errors.append("external manifest sidecar differs")
        manifest = _read_object(manifest_path, errors, "external public manifest")
        _validate_source_manifest(source, manifest, errors)
        private_path = manifest_path.with_name(PRIVATE_NAME)
        if not private_path.is_file() or _is_reparse(private_path):
            errors.append("private artifact is unavailable or is a reparse point")
        else:
            if file_sha256(private_path) != source.get("private_artifact_sha256"):
                errors.append("opaque private artifact hash differs")
            if private_path.stat().st_size != source.get("private_artifact_byte_count"):
                errors.append("opaque private artifact byte count differs")

    evidence = binding.get("validation_evidence")
    _validate_evidence(evidence, source, errors)

    privacy = binding.get("privacy_boundary")
    expected_privacy = {
        "developer_content_access_allowed": False,
        "private_content_stored_in_project": False,
        "automated_non_disclosing_validation_performed": True,
        "private_content_emitted": False,
        "old_holdout_access_allowed": False,
        "historical_records_reusable": False,
    }
    if privacy != expected_privacy:
        errors.append("privacy boundary differs")

    gate = binding.get("gate_transition")
    expected_gate = {
        "gate_status": EXPECTED_GATE_STATUS,
        "credentials_read": False,
        "user_confirmation_id": None,
        "formal_execution_allowed": False,
        "formal_judge_allowed": False,
        "next_permitted_action": "implement_and_freeze_blinded_formal_execution_protocol",
    }
    if gate != expected_gate:
        errors.append("post-binding gate transition differs")

    implementation = binding.get("binding_implementation")
    if not isinstance(implementation, Mapping):
        errors.append("binding implementation must be an object")
        implementation = {}
    files = implementation.get("files")
    if (
        implementation.get("status") != "NON_DISCLOSING_BINDING_READY"
        or not isinstance(files, Mapping)
        or set(files) != set(IMPLEMENTATION_FILES)
    ):
        errors.append("binding implementation scope differs")
    else:
        for relative in IMPLEMENTATION_FILES:
            path = PROJECT_ROOT / "week9" / relative
            if not path.is_file() or files.get(relative) != file_sha256(path):
                errors.append(f"binding implementation hash differs: {relative}")
    return errors


def _validate_source_manifest(
    source: Mapping[str, Any], manifest: Mapping[str, Any], errors: list[str]
) -> None:
    artifact = manifest.get("private_artifact")
    declarations = manifest.get("declarations")
    expected_source = {
        "manifest_path": source.get("manifest_path"),
        "manifest_sha256": source.get("manifest_sha256"),
        "private_artifact_file_name": PRIVATE_NAME,
        "private_artifact_sha256": (
            artifact.get("sha256") if isinstance(artifact, Mapping) else None
        ),
        "private_artifact_byte_count": (
            artifact.get("byte_count") if isinstance(artifact, Mapping) else None
        ),
        "case_count": manifest.get("case_count"),
        "source_model_id": manifest.get("source_model_id"),
        "created_utc": manifest.get("created_utc"),
        "declarations": dict(declarations) if isinstance(declarations, Mapping) else None,
    }
    if dict(source) != expected_source:
        errors.append("source submission differs from public manifest")
    expected_declarations = {
        "project_files_read": False,
        "prior_v8_content_seen": False,
        "independently_created": True,
        "plaintext_disclosed_to_v8_developer": False,
    }
    if (
        manifest.get("schema_version") != "v8-independent-holdout-manifest-1"
        or manifest.get("case_count") != 2
        or declarations != expected_declarations
        or not isinstance(artifact, Mapping)
        or artifact.get("file_name") != PRIVATE_NAME
    ):
        errors.append("external public manifest semantics differ")


def _validate_evidence(value: Any, source: Mapping[str, Any], errors: list[str]) -> None:
    if not isinstance(value, Mapping):
        errors.append("validation evidence must be an object")
        return
    path = _safe_project_path(value.get("report_path"), errors, "validation report")
    if path is None or not path.is_file():
        errors.append("validation report is unavailable")
        return
    digest = file_sha256(path)
    if digest != value.get("report_sha256"):
        errors.append("validation report hash differs")
    if _read_digest(path.with_suffix(".sha256"), errors, "validation report sidecar") != digest:
        errors.append("validation report sidecar differs")
    report = _read_object(path, errors, "validation report")
    checks = report.get("checks")
    public = report.get("public_binding")
    if (
        value.get("verdict") != PASS_VERDICT
        or value.get("check_count") != len(CHECK_NAMES)
        or report.get("schema_version") != REPORT_SCHEMA
        or report.get("verdict") != PASS_VERDICT
        or not isinstance(checks, Mapping)
        or set(checks) != set(CHECK_NAMES)
        or any(item is not True for item in checks.values())
        or report.get("failure_codes") != []
        or report.get("privacy_guard") != EXPECTED_PRIVACY_GUARD
        or not isinstance(public, Mapping)
        or public.get("manifest_path") != source.get("manifest_path")
        or public.get("manifest_sha256") != source.get("manifest_sha256")
        or public.get("private_artifact_sha256")
        != source.get("private_artifact_sha256")
        or public.get("private_artifact_byte_count")
        != source.get("private_artifact_byte_count")
        or public.get("declared_case_count") != source.get("case_count")
        or public.get("source_model_id") != source.get("source_model_id")
        or public.get("created_utc") != source.get("created_utc")
    ):
        errors.append("non-disclosing validation evidence differs or does not pass")


def _safe_external_manifest(value: Any, errors: list[str]) -> Path | None:
    if not isinstance(value, str) or not value or not Path(value).is_absolute():
        errors.append("external manifest path must be absolute")
        return None
    try:
        root = INTAKE_ROOT.resolve(strict=True)
        path = Path(value).resolve(strict=True)
    except OSError:
        errors.append("external manifest path is unavailable")
        return None
    if (
        path.name != MANIFEST_NAME
        or path.parent.parent != root
        or SUBMISSION_DIRECTORY.fullmatch(path.parent.name) is None
        or not path.is_relative_to(root)
        or any(_is_reparse(item) for item in (root, path.parent, path))
    ):
        errors.append("external manifest path escapes the isolated intake layout")
        return None
    return path


def _safe_project_path(value: Any, errors: list[str], label: str) -> Path | None:
    if not isinstance(value, str) or not value or Path(value).is_absolute():
        errors.append(f"{label} path must be project-relative")
        return None
    path = (PROJECT_ROOT / value).resolve()
    if not path.is_relative_to(PROJECT_ROOT):
        errors.append(f"{label} path escapes the project")
        return None
    return path


def _read_object(path: Path, errors: list[str], label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        errors.append(f"{label} is unavailable or invalid")
        return {}
    if not isinstance(value, dict):
        errors.append(f"{label} root must be an object")
        return {}
    return value


def _read_digest(path: Path, errors: list[str], label: str) -> str:
    try:
        value = path.read_text(encoding="ascii").strip()
    except (OSError, UnicodeError):
        errors.append(f"{label} is unavailable")
        return ""
    if LOWER_SHA256.fullmatch(value) is None:
        errors.append(f"{label} is not a lowercase SHA-256")
        return ""
    return value


def _is_reparse(path: Path) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return True
    marker = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    attributes = getattr(metadata, "st_file_attributes", 0)
    return path.is_symlink() or bool(marker and attributes & marker)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binding", default=str(DEFAULT_BINDING))
    parser.add_argument("--print-binding-digest", action="store_true")
    args = parser.parse_args(argv)
    try:
        binding = json.loads(Path(args.binding).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        print("ERROR: binding is unavailable or invalid", file=sys.stderr)
        return 1
    if not isinstance(binding, dict):
        print("ERROR: binding root must be an object", file=sys.stderr)
        return 1
    if args.print_binding_digest:
        print(canonical_sha256(binding))
        return 0
    errors = validate_binding_protocol(binding)
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 1
    print("V8 independent holdout manifest is bound; formal execution remains blocked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "BINDING_DIGEST",
    "DEFAULT_BINDING",
    "IMPLEMENTATION_FILES",
    "canonical_sha256",
    "file_sha256",
    "main",
    "validate_binding_protocol",
]
