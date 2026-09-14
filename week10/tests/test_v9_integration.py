from __future__ import annotations

from dataclasses import asdict
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError

from week9.src.generation import DeterministicNaturalLanguageProvider
from week9.src.online_generation import (
    DeepSeekNaturalLanguageProvider as V8Provider,
    ONLINE_PROMPT_SHA256,
    OnlineGenerationError,
)
from week9.src.schemas import (
    CheckStatus,
    FailureKind,
    QualityCheckResult,
    RoutingInputKind,
    RoutingPath,
    RunResult,
)
from week10.src.online_generation import (
    DeepSeekNaturalLanguageProvider,
    PROMPT_SHA256,
    V8_ONLINE_GENERATION_SHA256,
)
from week10.src.projection_contract_v2 import (
    PROMPT_SHA256 as V2_PROMPT_SHA256,
    SYSTEM_PROMPT as V2_SYSTEM_PROMPT,
)
from week10.src.runner import PUBLIC_SCHEMA_VERSION, RunStage, run_case


RC = "设计一个 RC 低通滤波器，截止频率 1kHz，电阻 1kΩ，电容根据截止频率计算，输入电压 5V 阶跃信号"
BLOCKED = "RC 低通滤波器，截止频率 2kHz，电阻 2kΩ，输入电压 4V 阶跃信号"
MODEL = "fixed-model"


def _projection(text: str) -> dict:
    local = DeterministicNaturalLanguageProvider().generate(text)
    if local.ready:
        return {
            "schema_version": "v8-certified-requirement-projection-1",
            "status": "ready",
            "template_id": local.template_id,
            "observations": [asdict(item) for item in local.observations],
            "issues": [],
        }
    return {
        "schema_version": "v8-certified-requirement-projection-1",
        "status": "blocked",
        "template_id": local.template_id,
        "observations": [],
        "issues": [{"code": "blocked", "message": "blocked"}],
    }


def _response(content, *, model=MODEL, usage=None):
    return {
        "model": model,
        "choices": [{"message": {"content": content}}],
        "usage": usage or {
            "prompt_tokens": 11,
            "completion_tokens": 7,
            "total_tokens": 18,
        },
    }


def _provider(transport, *, arm="B"):
    return DeepSeekNaturalLanguageProvider(
        api_key="test-key",
        api_base="https://invalid.example",
        model=MODEL,
        transport=transport,
        prompt_arm=arm,
    )


def _pass_check(name: str) -> QualityCheckResult:
    return QualityCheckResult(
        check_type=name,
        status=CheckStatus.PASS,
        required=True,
        tool_name="test",
        failure_kind=FailureKind.NONE,
    )


def _run_result(*, overall_success: bool, missing: str | None = None):
    from week9.src.hard_gate import REQUIRED_HARD_CHECK_TYPES

    result = Mock(spec=RunResult)
    result.version = "v8"
    result.overall_success = overall_success
    result.checks = [
        _pass_check(name)
        for name in REQUIRED_HARD_CHECK_TYPES
        if name != missing
    ]
    if not overall_success and result.checks:
        result.checks[0] = QualityCheckResult(
            check_type=result.checks[0].check_type,
            status=CheckStatus.FAIL,
            required=True,
            tool_name="test",
            failure_kind=FailureKind.CONTENT,
            failure_category="injected",
            evidence={"reason": "injected"},
        )
    return result


class _FakePipeline:
    def __init__(self, result):
        self.canonical_sysml = "package P { part def A; part a : A; }"
        self.canonical_render = SimpleNamespace(code="model A end A;")
        self._result = result
        self.run_kwargs = None

    def run(self, **kwargs):
        self.run_kwargs = kwargs
        return self._result


class ProviderContractTests(unittest.TestCase):
    def test_arms_change_only_system_prompt_and_both_disable_thinking(self):
        a = _provider(lambda payload: {}, arm="A")
        b = _provider(lambda payload: {}, arm="B")
        payload_a = a._request_payload(RC)
        payload_b = b._request_payload(RC)
        self.assertEqual(payload_a["thinking"], {"type": "disabled"})
        self.assertEqual(payload_a["thinking"], payload_b["thinking"])
        self.assertNotEqual(payload_a["messages"][0], payload_b["messages"][0])
        edited = json.loads(json.dumps(payload_a, ensure_ascii=False))
        edited["messages"][0] = payload_b["messages"][0]
        self.assertEqual(edited, payload_b)
        self.assertEqual(a.prompt_sha256, ONLINE_PROMPT_SHA256)
        self.assertEqual(b.prompt_sha256, PROMPT_SHA256)
        self.assertEqual(PROMPT_SHA256, V2_PROMPT_SHA256)
        self.assertEqual(
            payload_b["messages"][0],
            {"role": "system", "content": V2_SYSTEM_PROMPT},
        )
        v8 = V8Provider(
            api_key="test-key",
            api_base="https://invalid.example",
            model=MODEL,
            transport=lambda payload: {},
        )
        self.assertEqual(
            payload_a["messages"], v8._request_payload(RC)["messages"]
        )
        self.assertRegex(V8_ONLINE_GENERATION_SHA256, r"^[0-9a-f]{64}$")

    def test_one_call_accepts_and_records_safe_state(self):
        calls = []

        def transport(payload):
            calls.append(payload)
            return _response(json.dumps(_projection(RC), ensure_ascii=False))

        provider = _provider(transport)
        result = provider.generate(RC)
        self.assertTrue(result.ready)
        self.assertEqual(len(calls), 1)
        self.assertEqual(provider.api_call_count, 1)
        self.assertEqual(provider.last_usage["total_tokens"], 18)
        self.assertTrue(provider.last_response_valid)
        self.assertEqual(provider.last_response_model, MODEL)
        self.assertEqual(provider.last_diagnostic["reason_code"], "accepted")

    def test_blocked_local_input_remains_blocked_and_not_projection_accepted(self):
        content = json.dumps(_projection(BLOCKED), ensure_ascii=False)
        result = _provider(lambda payload: _response(content)).generate(BLOCKED)
        self.assertFalse(result.ready)
        provider = _provider(lambda payload: _response(content))
        provider.generate(BLOCKED)
        self.assertEqual(provider.last_diagnostic["reason_code"], "local_input_blocked")
        self.assertFalse(provider.last_diagnostic["projection_accepted"])
        self.assertTrue(provider.last_response_valid)

    def test_transport_error_keeps_count_and_has_no_usage_or_secret(self):
        secret = "transport-secret-payload"

        def explode(payload):
            raise RuntimeError(secret)

        provider = _provider(explode)
        with self.assertRaisesRegex(OnlineGenerationError, "^api_transport_error$") as caught:
            provider.generate(RC)
        self.assertNotIn(secret, str(caught.exception))
        self.assertEqual(provider.api_call_count, 1)
        self.assertIsNone(provider.last_usage)
        self.assertFalse(provider.last_response_valid)

    def test_usage_model_and_schema_failures_have_fixed_categories(self):
        valid = json.dumps(_projection(RC), ensure_ascii=False)
        scenarios = (
            (
                _response(valid, usage={"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 99}),
                "usage_contract_invalid",
            ),
            (_response(valid, model="secret-untrusted-model"), "response_model_mismatch"),
            (_response("not-json"), "response_json_invalid"),
            (_response(json.dumps({"secret": "value"})), "projection_schema_invalid"),
        )
        for response, reason in scenarios:
            with self.subTest(reason=reason):
                provider = _provider(lambda payload, response=response: response)
                with self.assertRaisesRegex(OnlineGenerationError, f"^{reason}$"):
                    provider.generate(RC)
                self.assertEqual(provider.api_call_count, 1)
                self.assertEqual(provider.last_diagnostic["reason_code"], reason)
                if reason == "usage_contract_invalid":
                    self.assertIsNone(provider.last_usage)
                if reason == "response_model_mismatch":
                    self.assertIsNone(provider.last_response_model)

    def test_http_decode_and_transport_stages_stay_distinct(self):
        class Response:
            def __init__(self, raw):
                self.raw = raw

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return self.raw

        provider = DeepSeekNaturalLanguageProvider(
            api_key="test", api_base="https://invalid.example", model=MODEL
        )
        opener = SimpleNamespace(open=Mock(return_value=Response(b"not-json")))
        with patch("week10.src.online_generation.build_opener", return_value=opener):
            with self.assertRaisesRegex(OnlineGenerationError, "^response_contract_invalid$"):
                provider.generate(RC)
        self.assertEqual(provider.api_call_count, 1)

    def test_http_redirect_is_rejected_as_transport_error(self):
        provider = DeepSeekNaturalLanguageProvider(
            api_key="test", api_base="https://api.deepseek.com", model=MODEL
        )
        redirect = HTTPError(
            "https://api.deepseek.com/v1/chat/completions",
            302,
            "redirect",
            {"Location": "https://untrusted.example"},
            None,
        )
        opener = SimpleNamespace(open=Mock(side_effect=redirect))
        with patch("week10.src.online_generation.build_opener", return_value=opener):
            with self.assertRaisesRegex(OnlineGenerationError, "^api_transport_error$"):
                provider.generate(RC)
        self.assertEqual(provider.api_call_count, 1)

        provider = DeepSeekNaturalLanguageProvider(
            api_key="test", api_base="https://invalid.example", model=MODEL
        )
        opener = SimpleNamespace(open=Mock(side_effect=TimeoutError("secret")))
        with patch("week10.src.online_generation.build_opener", return_value=opener):
            with self.assertRaisesRegex(OnlineGenerationError, "^api_transport_error$"):
                provider.generate(RC)
        self.assertEqual(provider.api_call_count, 1)

    def test_v8_differential_ready_blocked_and_rejections(self):
        scenarios = []
        ready = _projection(RC)
        scenarios.append((RC, ready))
        scenarios.append((BLOCKED, _projection(BLOCKED)))
        wrong_unit = json.loads(json.dumps(ready, ensure_ascii=False))
        wrong_unit["observations"][0]["unit"] = "Ohm"
        scenarios.append((RC, wrong_unit))
        wrong_value = json.loads(json.dumps(ready, ensure_ascii=False))
        wrong_value["observations"][0]["value"] = 123456
        scenarios.append((RC, wrong_value))
        ready_cross = json.loads(json.dumps(ready, ensure_ascii=False))
        ready_cross["status"] = "blocked"
        ready_cross["observations"] = []
        ready_cross["issues"] = [{"code": "blocked", "message": "blocked"}]
        scenarios.append((RC, ready_cross))

        for text, projection in scenarios:
            with self.subTest(text=text[:12], status=projection["status"]):
                response = _response(json.dumps(projection, ensure_ascii=False))
                old = V8Provider(
                    api_key="test",
                    api_base="https://invalid.example",
                    model=MODEL,
                    transport=lambda payload, response=response: response,
                ).generate(text)
                new = _provider(
                    lambda payload, response=response: response, arm="A"
                ).generate(text)
                self.assertEqual(old.ready, new.ready)
                self.assertEqual(old.template_id, new.template_id)
                self.assertEqual(old.model_name, new.model_name)
                self.assertEqual(old.observations, new.observations)
                self.assertEqual(old.issues, new.issues)
                self.assertEqual(old.derivations, new.derivations)

    def test_v8_differential_schema_error_stays_online_generation_error(self):
        response = _response('{"bad":"schema"}')
        for cls in (V8Provider, DeepSeekNaturalLanguageProvider):
            with self.subTest(provider=cls.__module__):
                provider = cls(
                    api_key="test",
                    api_base="https://invalid.example",
                    model=MODEL,
                    transport=lambda payload: response,
                )
                with self.assertRaises(OnlineGenerationError):
                    provider.generate(RC)


class RunnerContractTests(unittest.TestCase):
    def _temp_workspace(self):
        return tempfile.TemporaryDirectory(prefix="v9-integration-")

    def test_default_local_block_is_zero_api_and_never_enters_pipeline(self):
        with self._temp_workspace() as directory, patch(
            "week10.src.runner.CertifiedTemplateRepairPipeline.from_natural_language_generation"
        ) as pipeline:
            result = run_case("negative_missing", BLOCKED, workspace_root=directory)
        public = result.public_dict()
        pipeline.assert_not_called()
        self.assertEqual(result.stage, RunStage.GENERATION_BLOCKED)
        self.assertEqual(public["api_call_count"], 0)
        self.assertIsNone(public["projection_accepted"])
        self.assertIsNone(public["response_valid"])
        self.assertFalse(public["pipeline_attempted"])
        self.assertIsNone(public["pipeline_success"])
        self.assertIsNone(public["hard_gate_passed"])
        self.assertIsNone(public["quality_decision_passed"])
        self.assertFalse(public["published"])

    def test_fake_online_calls_once_and_projection_rejection_skips_pipeline(self):
        calls = []

        def transport(payload):
            calls.append(payload)
            return _response(json.dumps(_projection(BLOCKED), ensure_ascii=False))

        provider = _provider(transport)
        with self._temp_workspace() as directory, patch(
            "week10.src.runner.CertifiedTemplateRepairPipeline.from_natural_language_generation"
        ) as pipeline:
            result = run_case(
                "negative_missing", BLOCKED, workspace_root=directory,
                generation_provider=provider,
            )
        pipeline.assert_not_called()
        self.assertEqual(len(calls), 1)
        self.assertFalse(result.projection_accepted)
        self.assertTrue(result.response_valid)

    def test_online_acceptance_enters_v8_as_verified_structured_observations(self):
        provider = _provider(
            lambda payload: _response(json.dumps(_projection(RC), ensure_ascii=False))
        )
        fake = _FakePipeline(_run_result(overall_success=True))
        with self._temp_workspace() as directory, patch(
            "week10.src.runner.CertifiedTemplateRepairPipeline.from_observations",
            return_value=fake,
        ) as structured, patch(
            "week10.src.runner.CertifiedTemplateRepairPipeline.from_natural_language_generation"
        ) as natural_language:
            result = run_case(
                "rc_lowpass",
                RC,
                workspace_root=directory,
                generation_provider=provider,
                run_id="v9pd-002",
            )
        structured.assert_called_once_with(
            template_id=result.generation.template_id,
            model_name=result.generation.model_name,
            observations=result.generation.observations,
            workspace_root=Path(directory).resolve(),
        )
        natural_language.assert_not_called()
        self.assertEqual(fake.run_kwargs["run_id"], "v9pd-002")
        self.assertEqual(
            fake.run_kwargs["traceability_input"],
            result.generation.traceability_input,
        )

    def test_default_local_ready_keeps_natural_language_v8_entry(self):
        fake = _FakePipeline(_run_result(overall_success=True))
        with self._temp_workspace() as directory, patch(
            "week10.src.runner.CertifiedTemplateRepairPipeline.from_natural_language_generation",
            return_value=fake,
        ) as natural_language, patch(
            "week10.src.runner.CertifiedTemplateRepairPipeline.from_observations"
        ) as structured:
            result = run_case("rc_lowpass", RC, workspace_root=directory)
        natural_language.assert_called_once()
        structured.assert_not_called()
        self.assertEqual(result.provider, "v8_deterministic_local_grammar")
        self.assertNotIn("traceability_input", fake.run_kwargs)

    def test_s4_b_equivalent_response_reaches_complete_v8_result(self):
        calls = []

        def transport(payload):
            calls.append(payload)
            return _response(json.dumps(_projection(RC), ensure_ascii=False))

        provider = _provider(transport, arm="B")
        with self._temp_workspace() as directory:
            result = run_case(
                "rc_lowpass",
                RC,
                workspace_root=directory,
                generation_provider=provider,
                run_id="v9pd-002",
            )
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.stage, RunStage.PIPELINE_COMPLETE)
        self.assertIsNone(result.error)
        self.assertTrue(result.pipeline_success)
        self.assertTrue(result.hard_gate_passed)
        self.assertTrue(result.quality_decision_passed)
        self.assertTrue(result.published)
        self.assertEqual(result.run_id, "v9pd-002")
        self.assertEqual(result.run_result.run_id, "v9pd-002")
        self.assertEqual(
            result.run_result.routing.input_kind,
            RoutingInputKind.STRUCTURED_OBSERVATIONS,
        )
        self.assertEqual(result.run_result.routing.path, RoutingPath.FAST_PATH)
        self.assertFalse(result.run_result.routing.online_api_used)
        self.assertTrue(result.generation.online_api_used)
        self.assertEqual(result.public_dict()["api_call_count"], 1)
        self.assertEqual(result.public_dict()["usage"]["total_tokens"], 18)

    def test_projection_passed_but_pipeline_rejected_is_not_published(self):
        provider = _provider(
            lambda payload: _response(json.dumps(_projection(RC), ensure_ascii=False))
        )
        fake = _FakePipeline(_run_result(overall_success=False))
        with self._temp_workspace() as directory, patch(
            "week10.src.runner.CertifiedTemplateRepairPipeline.from_observations",
            return_value=fake,
        ):
            result = run_case(
                "rc_lowpass", RC, workspace_root=directory,
                generation_provider=provider,
            )
        self.assertTrue(result.projection_accepted)
        self.assertTrue(result.pipeline_attempted)
        self.assertFalse(result.pipeline_success)
        self.assertFalse(result.hard_gate_passed)
        self.assertFalse(result.published)

    def test_missing_hard_gate_check_cannot_publish(self):
        from week9.src.hard_gate import REQUIRED_HARD_CHECK_TYPES

        local = DeterministicNaturalLanguageProvider().generate(RC)
        fake = _FakePipeline(
            _run_result(overall_success=True, missing=REQUIRED_HARD_CHECK_TYPES[-1])
        )
        provider = Mock()
        provider.provider = "v8_deterministic_local_grammar"
        provider.generate.return_value = local
        with self._temp_workspace() as directory, patch(
            "week10.src.runner.CertifiedTemplateRepairPipeline.from_natural_language_generation",
            return_value=fake,
        ):
            result = run_case(
                "rc_lowpass", RC, workspace_root=directory,
                generation_provider=provider,
            )
        self.assertTrue(result.pipeline_success)
        self.assertFalse(result.hard_gate_passed)
        self.assertFalse(result.published)
        self.assertIn("MISSING", result.required_check_statuses.values())

    def test_recomputed_quality_blocks_false_internal_success_claim(self):
        provider = _provider(
            lambda payload: _response(json.dumps(_projection(RC), ensure_ascii=False))
        )
        fake = _FakePipeline(_run_result(overall_success=False))
        fake._result.overall_success = True
        with self._temp_workspace() as directory, patch(
            "week10.src.runner.CertifiedTemplateRepairPipeline.from_observations",
            return_value=fake,
        ):
            result = run_case(
                "rc_lowpass", RC, workspace_root=directory,
                generation_provider=provider,
                run_id="scheduled_run_1",
            )
        self.assertEqual(result.run_id, "scheduled_run_1")
        self.assertTrue(result.pipeline_success)
        self.assertFalse(result.quality_decision_passed)
        self.assertFalse(result.published)

    def test_generation_bound_to_another_requirement_never_enters_pipeline(self):
        wrong = DeterministicNaturalLanguageProvider().generate(RC)
        provider = Mock()
        provider.provider = "v8_deterministic_local_grammar"
        provider.generate.return_value = wrong
        with self._temp_workspace() as directory, patch(
            "week10.src.runner.CertifiedTemplateRepairPipeline.from_natural_language_generation"
        ) as pipeline:
            result = run_case(
                "negative_missing", BLOCKED, workspace_root=directory,
                generation_provider=provider,
            )
        pipeline.assert_not_called()
        self.assertEqual(result.stage, RunStage.GENERATION_ERROR)
        self.assertEqual(result.reason_code, "local_generation_error")

    def test_public_serializer_is_fixed_and_does_not_leak_text_or_exception(self):
        requirement_secret = "requirement-secret-value"
        exception_secret = "exception-secret-value"

        class FailingProvider:
            provider = "deepseek"
            model = MODEL
            prompt_arm = "B"
            prompt_sha256 = PROMPT_SHA256
            api_call_count = 1
            last_usage = None
            last_response_valid = False
            last_response_model = None
            last_diagnostic = {
                "reason_code": "api_transport_error",
                "parameter_keys": (),
                "projection_accepted": False,
                "legacy_disposition": "infrastructure_error",
            }

            def generate(self, text):
                raise RuntimeError(exception_secret)

        with self._temp_workspace() as directory:
            result = run_case(
                "synthetic_private_case",
                requirement_secret,
                workspace_root=directory,
                generation_provider=FailingProvider(),
            )
        encoded = json.dumps(result.public_dict(), ensure_ascii=False)
        self.assertNotIn(requirement_secret, encoded)
        self.assertNotIn(exception_secret, encoded)
        self.assertEqual(result.public_dict()["case_id"], "unknown_case")
        self.assertEqual(result.public_dict()["schema_version"], PUBLIC_SCHEMA_VERSION)
        self.assertIsNone(result.public_dict()["usage"])
        self.assertEqual(result.public_dict()["api_call_count"], 1)


if __name__ == "__main__":
    unittest.main()
