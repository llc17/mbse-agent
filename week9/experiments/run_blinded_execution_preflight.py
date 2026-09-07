"""Run an offline, synthetic preflight for the blinded formal execution stack."""

from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import tempfile
from typing import Any

from week9.experiments.blinded_arm_worker import (
    _PinnedDeepSeekNaturalLanguageProvider,
    execute_private_arm,
)
from week9.experiments.blinded_formal_execution import (
    AUTHORIZATION_SCHEMA,
    FINAL_PROTOCOL_SCHEMA,
    PINNED_DEEPSEEK_MODEL,
    PINNED_DEEPSEEK_THINKING_MODE,
    BlindedCaseDefinition,
    build_blinded_schedule,
    canonical_sha256,
    file_sha256,
    validate_execution_authorization,
)
from week9.experiments.run_blinded_formal_comparison import (
    build_worker_command,
    controlled_worker_environment,
)


PROJECT_ROOT = Path(__file__).resolve().parents[2]
REPORT_NAME = "blinded-execution-preflight-report.json"
PASS_VERDICT = "BLINDED_EXECUTION_PREFLIGHT_PASS"
FAIL_VERDICT = "BLINDED_EXECUTION_PREFLIGHT_FAIL"
CORE_CHECKS = (
    "requirement_normalization",
    "sysml_syntax",
    "modelica_compile",
    "parameter_unit_consistency",
    "simulation",
    "physical_behavior",
    "cross_model_consistency",
)


def run_preflight(output_directory: Path) -> tuple[dict[str, Any], Path]:
    """Exercise only synthetic content and write a content-free report."""

    output = output_directory.resolve()
    if not output.is_relative_to(PROJECT_ROOT):
        raise ValueError("preflight output must remain inside the project")
    output.mkdir(parents=True, exist_ok=False)
    manifest_digest = "a" * 64
    schedule = build_blinded_schedule(manifest_sha256=manifest_digest)
    synthetic = BlindedCaseDefinition(
        public_ref="sealed_h01",
        case_id="ih_cccccccccccccccc",
        dataset="holdout",
        domain="electrical",
        raw_input=(
            "设计一个 RC 低通滤波器，截止频率 1900 Hz，电阻 1400 Ohm，"
            "电容根据截止频率计算，输入电压 4.2 V 阶跃信号。"
        ),
        required_checks=CORE_CHECKS,
        expected_physics={
            "validate_type": "rc_cutoff",
            "expected_value_source": "params.cutoff_freq",
            "tolerance_pct": 10.0,
        },
        tags=("synthetic_preflight",),
    )
    selected = next(item for item in schedule if item["dataset"] == "holdout")
    command = build_worker_command(
        selected,
        protocol_path=Path("D:/synthetic/protocol.json"),
        authorization_path=Path("D:/synthetic/authorization.json"),
        consumption_marker=Path("D:/synthetic/consumed.json"),
        binding_path=Path("D:/synthetic/binding.json"),
        run_id="synthetic_preflight_run",
        work_root=Path("D:/synthetic/private/work"),
        candidate_path=Path("D:/synthetic/private/candidate.json"),
    )
    environment = controlled_worker_environment(
        {
            "PATH": "synthetic-system-path",
            "TEMP": "synthetic-temp",
            "DEEPSEEK_API_KEY": "synthetic-deepseek-key",
            "DEEPSEEK_API_BASE": "https://api.deepseek.com",
            "DEEPSEEK_MODEL": PINNED_DEEPSEEK_MODEL,
            "OPENAI_API_KEY": "must-not-forward",
            "KIMI_API_KEY": "must-not-forward",
            "ANTHROPIC_API_KEY": "must-not-forward",
            "UNRELATED_SECRET": "must-not-forward",
        }
    )
    pinned_provider = _PinnedDeepSeekNaturalLanguageProvider(
        api_key="synthetic-key",
        api_base="https://api.deepseek.com",
        model=PINNED_DEEPSEEK_MODEL,
        transport=lambda _payload: {},
    )
    pinned_payload = pinned_provider._request_payload("synthetic requirement")
    with tempfile.TemporaryDirectory(prefix="v8bf-") as temporary:
        temporary_path = Path(temporary)
        authorization_ok = _synthetic_authorization_check(temporary_path)
        _protocol, _authorization, blocked_errors = validate_execution_authorization(
            temporary_path / "missing-protocol.json",
            temporary_path / "missing-authorization.json",
            confirmation_id="not-authorized",
        )
        result = execute_private_arm(
            arm_id="v8_default_local",
            case=synthetic,
            run_id="synthetic_preflight_run",
            work_root=temporary_path / "worker",
        )
    serialized_schedule = json.dumps(schedule, ensure_ascii=False)
    serialized_command = json.dumps(command, ensure_ascii=False)
    pass_conditions = {
        "schedule_is_exactly_100": len(schedule) == 100,
        "schedule_uses_public_references_only": all(
            item["case_ref"] in {
                "dual_room_thermal",
                "rc_lowpass",
                "rlc_lowpass",
                "sealed_h01",
                "sealed_h02",
            }
            for item in schedule
        )
        and "raw_input" not in serialized_schedule
        and "oracle" not in serialized_schedule
        and "case_id" not in serialized_schedule,
        "worker_command_has_no_plaintext_argument": "--raw-input" not in command
        and synthetic.raw_input not in serialized_command
        and synthetic.case_id not in serialized_command,
        "worker_environment_is_minimal": (
            environment.get("DEEPSEEK_API_KEY") == "synthetic-deepseek-key"
            and "OPENAI_API_KEY" not in environment
            and "KIMI_API_KEY" not in environment
            and "ANTHROPIC_API_KEY" not in environment
            and "UNRELATED_SECRET" not in environment
        ),
        "deepseek_v4_flash_nonthinking_profile_is_pinned": (
            environment.get("DEEPSEEK_MODEL") == PINNED_DEEPSEEK_MODEL
            and pinned_payload.get("model") == PINNED_DEEPSEEK_MODEL
            and pinned_payload.get("thinking")
            == {"type": PINNED_DEEPSEEK_THINKING_MODE}
        ),
        "authorization_receipt_contract_accepts_valid_synthetic": authorization_ok,
        "missing_authorization_fails_closed": bool(blocked_errors),
        "synthetic_local_worker_succeeds": result.overall_success,
        "synthetic_local_worker_uses_no_model_api": (
            result.raw_metrics.get("online_api_used") is False
            and result.token_usage.total_tokens == 0
        ),
    }
    passed = all(pass_conditions.values())
    report = {
        "schema_version": "v8-blinded-formal-preflight-2",
        "verdict": PASS_VERDICT if passed else FAIL_VERDICT,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "planned_run_count": len(schedule),
        "scenario_count": len(pass_conditions),
        "pass_conditions": pass_conditions,
        "synthetic_local_result_sha256": canonical_sha256(result.to_dict()),
        "guard": {
            "synthetic_content_only": True,
            "independent_holdout_loaded": False,
            "old_holdout_loaded": False,
            "credentials_read": False,
            "network_used": False,
            "deepseek_called": False,
            "kimi_called": False,
            "private_content_emitted": False,
            "case_ids_emitted": False,
            "requirements_emitted": False,
            "parameters_emitted": False,
        },
    }
    report_path = output / REPORT_NAME
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2, allow_nan=False)
        + "\n",
        encoding="utf-8",
    )
    report_path.with_suffix(".sha256").write_text(
        file_sha256(report_path) + "\n", encoding="ascii"
    )
    return report, report_path


def _synthetic_authorization_check(root: Path) -> bool:
    protocol_path = root / "synthetic-protocol.json"
    protocol = {
        "schema_version": FINAL_PROTOCOL_SCHEMA,
        "status": "READY_AWAITING_USER_AUTHORIZATION",
        "formal_execution_allowed": False,
        "authorization_policy": {"additive_receipt_required": True},
        "holdout_binding": {"sha256": "b" * 64},
    }
    protocol_path.write_text(json.dumps(protocol), encoding="utf-8")
    protocol_digest = canonical_sha256(protocol)
    protocol_path.with_suffix(".sha256").write_text(protocol_digest + "\n", encoding="ascii")
    confirmation = "synthetic-confirmation"
    authorization_path = root / "synthetic-authorization.json"
    authorization = {
        "schema_version": AUTHORIZATION_SCHEMA,
        "status": "AUTHORIZED_FOR_ONE_BATCH",
        "blinded_protocol_sha256": protocol_digest,
        "holdout_binding_sha256": "b" * 64,
        "planned_run_count": 100,
        "confirmation_id_sha256": hashlib.sha256(confirmation.encode("utf-8")).hexdigest(),
        "user_explicitly_authorized": True,
        "one_batch_only": True,
        "credentials_read_after_validation_only": True,
        "execution_consumed": False,
    }
    authorization_path.write_text(json.dumps(authorization), encoding="utf-8")
    authorization_path.with_suffix(".sha256").write_text(
        file_sha256(authorization_path) + "\n", encoding="ascii"
    )
    _protocol, _authorization, errors = validate_execution_authorization(
        protocol_path,
        authorization_path,
        confirmation_id=confirmation,
    )
    return errors == []


def main() -> int:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    report, path = run_preflight(
        PROJECT_ROOT / "week9" / "results" / f"blinded-execution-preflight-{stamp}"
    )
    print(report["verdict"])
    print(f"report_path={path}")
    print(f"report_sha256={file_sha256(path)}")
    return 0 if report["verdict"] == PASS_VERDICT else 1


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["FAIL_VERDICT", "PASS_VERDICT", "REPORT_NAME", "main", "run_preflight"]
