from __future__ import annotations

from contextlib import redirect_stderr
import hashlib
import io
import json
import math
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import requests

import week9.experiments.blinded_formal_execution as blinded
from week9.experiments.blinded_arm_worker import (
    _PinnedDeepSeekNaturalLanguageProvider,
    _pinned_legacy_transport,
    execute_private_arm,
)
from week9.experiments.run_blinded_formal_comparison import (
    build_worker_command,
    controlled_worker_environment,
    main as controller_main,
)
from week9.experiments.freeze_blinded_formal_protocol import (
    validate_blinded_formal_protocol,
)


CORE_CHECKS = (
    "requirement_normalization",
    "sysml_syntax",
    "modelica_compile",
    "parameter_unit_consistency",
    "simulation",
    "physical_behavior",
    "cross_model_consistency",
)
PRIVATE_MARKER = "SYNTHETIC_PRIVATE_REQUIREMENT_MARKER"


def _synthetic_case() -> blinded.BlindedCaseDefinition:
    return blinded.BlindedCaseDefinition(
        public_ref="sealed_h01",
        case_id="ih_aaaaaaaaaaaaaaaa",
        dataset="holdout",
        domain="electrical",
        raw_input=(
            "设计一个 RC 低通滤波器，截止频率 1800 Hz，电阻 1300 Ohm，"
            "电容根据截止频率计算，输入电压 4 V 阶跃信号。" + PRIVATE_MARKER
        ),
        required_checks=CORE_CHECKS,
        expected_physics={
            "validate_type": "rc_cutoff",
            "expected_value_source": "params.cutoff_freq",
            "tolerance_pct": 10.0,
        },
        tags=("synthetic_preflight",),
    )


class BlindedFormalExecutionTests(unittest.TestCase):
    def test_current_blinded_formal_protocol_is_frozen(self):
        protocol_path = blinded.DEFAULT_FINAL_PROTOCOL
        protocol = json.loads(protocol_path.read_text(encoding="utf-8"))
        self.assertEqual(validate_blinded_formal_protocol(protocol), [])

    def test_schedule_is_exactly_100_and_exposes_only_public_references(self):
        schedule = blinded.build_blinded_schedule(manifest_sha256="1" * 64)
        self.assertEqual(len(schedule), 100)
        self.assertEqual([item["sequence"] for item in schedule], list(range(1, 101)))
        groups = {item["group_ref"] for item in schedule}
        self.assertEqual(len(groups), 25)
        for group in groups:
            selected = [item for item in schedule if item["group_ref"] == group]
            self.assertEqual({item["arm_id"] for item in selected}, set(blinded.FORMAL_ARM_IDS))
            self.assertEqual({item["order_in_group"] for item in selected}, {1, 2, 3, 4})
        serialized = json.dumps(schedule)
        self.assertNotIn("raw_input", serialized)
        self.assertNotIn("oracle", serialized)
        self.assertNotIn("case_id", serialized)

    def test_sealed_case_repr_and_bundle_repr_do_not_disclose_content(self):
        case = _synthetic_case()
        bundle = blinded.SealedHoldoutBundle(
            manifest_sha256="2" * 64,
            private_artifact_sha256="3" * 64,
            cases=(case, blinded.BlindedCaseDefinition(
                public_ref="sealed_h02",
                case_id="ih_bbbbbbbbbbbbbbbb",
                dataset="holdout",
                domain="thermal",
                raw_input="private thermal marker",
                required_checks=CORE_CHECKS,
                expected_physics={"validate_type": "thermal_steady"},
                tags=("synthetic_preflight",),
            )),
        )
        rendered = repr(case) + repr(bundle)
        self.assertNotIn(PRIVATE_MARKER, rendered)
        self.assertNotIn("ih_aaaaaaaaaaaaaaaa", rendered)
        self.assertNotIn("private thermal marker", rendered)

    def test_worker_command_contains_reference_but_no_plaintext(self):
        item = blinded.build_blinded_schedule(manifest_sha256="4" * 64)[12]
        command = build_worker_command(
            item,
            protocol_path=Path("D:/mbse/week9/configs/protocol.json"),
            authorization_path=Path("D:/mbse/week9/configs/auth.json"),
            consumption_marker=Path("D:/mbse/week9/results/consumed.json"),
            binding_path=Path("D:/mbse/week9/configs/binding.json"),
            run_id="safe_run_id",
            work_root=Path("D:/v8_formal_private/batch/work"),
            candidate_path=Path("D:/v8_formal_private/batch/candidate.json"),
        )
        joined = " ".join(command)
        self.assertIn(str(item["case_ref"]), joined)
        self.assertNotIn("--raw-input", command)
        self.assertNotIn(PRIVATE_MARKER, joined)

    def test_worker_environment_forwards_only_deepseek_and_system_values(self):
        source = {
            "PATH": "system-path",
            "TEMP": "temp",
            "DEEPSEEK_API_KEY": "deepseek-secret",
            "DEEPSEEK_MODEL": blinded.PINNED_DEEPSEEK_MODEL,
            "OPENAI_API_KEY": "openai-secret",
            "KIMI_API_KEY": "kimi-secret",
            "ANTHROPIC_API_KEY": "anthropic-secret",
            "UNRELATED_SECRET": "other-secret",
        }
        result = controlled_worker_environment(source)
        self.assertEqual(result["DEEPSEEK_API_KEY"], "deepseek-secret")
        self.assertEqual(result["PATH"], "system-path")
        self.assertNotIn("OPENAI_API_KEY", result)
        self.assertNotIn("KIMI_API_KEY", result)
        self.assertNotIn("ANTHROPIC_API_KEY", result)
        self.assertNotIn("UNRELATED_SECRET", result)

    def test_v8_and_legacy_requests_pin_flash_nonthinking_mode(self):
        provider = _PinnedDeepSeekNaturalLanguageProvider(
            api_key="synthetic-key",
            api_base="https://api.deepseek.com",
            model=blinded.PINNED_DEEPSEEK_MODEL,
            transport=lambda _payload: {},
        )
        payload = provider._request_payload("synthetic requirement")
        self.assertEqual(payload["model"], blinded.PINNED_DEEPSEEK_MODEL)
        self.assertEqual(
            payload["thinking"],
            {"type": blinded.PINNED_DEEPSEEK_THINKING_MODE},
        )

        captured: dict[str, object] = {}

        def fake_post(url, *args, **kwargs):
            captured.update(kwargs["json"])
            return object()

        environment = {
            "DEEPSEEK_MODEL": blinded.PINNED_DEEPSEEK_MODEL,
            "DEEPSEEK_API_BASE": "https://api.deepseek.com",
        }
        with patch.dict("os.environ", environment, clear=False), patch.object(
            requests, "post", fake_post
        ):
            with _pinned_legacy_transport():
                requests.post(
                    "https://api.deepseek.com/v1/chat/completions",
                    json={
                        "model": blinded.PINNED_DEEPSEEK_MODEL,
                        "messages": [],
                        "temperature": 0.3,
                        "max_tokens": 4096,
                    },
                )
        self.assertEqual(
            captured["thinking"],
            {"type": blinded.PINNED_DEEPSEEK_THINKING_MODE},
        )

    def test_cli_without_execute_flag_blocks_before_artifact_access(self):
        stderr = io.StringIO()
        with redirect_stderr(stderr):
            code = controller_main(
                [
                    "--protocol",
                    "Z:/definitely-missing/private-protocol.json",
                    "--binding",
                    "Z:/definitely-missing/private-binding.json",
                ]
            )
        self.assertEqual(code, 2)
        self.assertIn("BLOCKED", stderr.getvalue())

    def test_additive_authorization_is_digest_bound_and_one_batch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            protocol_path = root / "protocol.json"
            protocol = {
                "schema_version": blinded.FINAL_PROTOCOL_SCHEMA,
                "status": "READY_AWAITING_USER_AUTHORIZATION",
                "formal_execution_allowed": False,
                "authorization_policy": {"additive_receipt_required": True},
                "holdout_binding": {"sha256": "5" * 64},
            }
            protocol_path.write_text(json.dumps(protocol), encoding="utf-8")
            protocol_digest = blinded.canonical_sha256(protocol)
            protocol_path.with_suffix(".sha256").write_text(
                protocol_digest + "\n", encoding="ascii"
            )
            confirmation = "synthetic-user-confirmation"
            authorization_path = root / "authorization.json"
            authorization = {
                "schema_version": blinded.AUTHORIZATION_SCHEMA,
                "status": "AUTHORIZED_FOR_ONE_BATCH",
                "blinded_protocol_sha256": protocol_digest,
                "holdout_binding_sha256": "5" * 64,
                "planned_run_count": 100,
                "confirmation_id_sha256": hashlib.sha256(
                    confirmation.encode("utf-8")
                ).hexdigest(),
                "user_explicitly_authorized": True,
                "one_batch_only": True,
                "credentials_read_after_validation_only": True,
                "execution_consumed": False,
            }
            authorization_path.write_text(json.dumps(authorization), encoding="utf-8")
            authorization_path.with_suffix(".sha256").write_text(
                blinded.file_sha256(authorization_path) + "\n", encoding="ascii"
            )
            _protocol, _authorization, errors = blinded.validate_execution_authorization(
                protocol_path,
                authorization_path,
                confirmation_id=confirmation,
            )
            self.assertEqual(errors, [])
            with patch.object(blinded, "PROJECT_ROOT", root):
                marker = blinded.consume_execution_authorization(
                    authorization_path,
                    protocol_sha256=protocol_digest,
                    batch_id="synthetic_batch",
                )
                self.assertTrue(marker.is_file())
                with self.assertRaises(FileExistsError):
                    blinded.consume_execution_authorization(
                        authorization_path,
                        protocol_sha256=protocol_digest,
                        batch_id="second_batch",
                    )

    def test_synthetic_local_worker_path_uses_no_online_api(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = execute_private_arm(
                arm_id="v8_default_local",
                case=_synthetic_case(),
                run_id="synthetic_blinded_local",
                work_root=Path(temporary),
            )
        self.assertTrue(result.overall_success)
        self.assertFalse(result.raw_metrics.get("online_api_used"))
        self.assertEqual(result.token_usage.total_tokens, 0)
        expected_capacitance = 1.0 / (2.0 * math.pi * 1300.0 * 1800.0)
        values = {item.canonical_name: item.value for item in result.parameters}
        self.assertTrue(
            math.isclose(values["capacitance"], expected_capacitance, rel_tol=1e-12)
        )


if __name__ == "__main__":
    unittest.main()
