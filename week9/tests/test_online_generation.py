from __future__ import annotations

from dataclasses import asdict
import json
import unittest

from week9.experiments.cases import DEVELOPMENT_REGRESSION_CASES
from week9.src.generation import DeterministicNaturalLanguageProvider
from week9.src.online_generation import (
    DeepSeekNaturalLanguageProvider,
    ONLINE_PROJECTION_SCHEMA,
    OnlineGenerationError,
)


MODEL = "frozen-generator"


def _response(requirement: str, *, mutate=None):
    local = DeterministicNaturalLanguageProvider().generate(requirement)
    projection = {
        "schema_version": ONLINE_PROJECTION_SCHEMA,
        "status": "ready" if local.ready else "blocked",
        "template_id": local.template_id,
        "observations": [asdict(item) for item in local.observations],
        "issues": (
            []
            if local.ready
            else [asdict(item) for item in local.issues]
        ),
    }
    if mutate is not None:
        mutate(projection)
    content = json.dumps(
        projection,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return {
        "model": MODEL,
        "choices": [{"message": {"content": content}}],
        "usage": {
            "prompt_tokens": 40,
            "completion_tokens": 20,
            "total_tokens": 60,
        },
    }


class OnlineGenerationTests(unittest.TestCase):
    def setUp(self):
        self.requirement = DEVELOPMENT_REGRESSION_CASES["rc_lowpass"].raw_input

    def _provider(self, transport):
        return DeepSeekNaturalLanguageProvider(
            api_key="not-a-real-key",
            api_base="https://invalid.example",
            model=MODEL,
            transport=transport,
        )

    def test_verified_projection_returns_online_hash_bound_contract(self):
        requests = []

        def transport(payload):
            requests.append(payload)
            return _response(self.requirement)

        result = self._provider(transport).generate(self.requirement)
        self.assertTrue(result.ready)
        self.assertTrue(result.online_api_used)
        self.assertEqual(result.api_call_count, 1)
        self.assertEqual(result.provider, "deepseek")
        self.assertEqual(result.model, MODEL)
        self.assertEqual(result.total_tokens, 60)
        self.assertEqual(len(result.request_sha256), 64)
        self.assertEqual(len(result.response_sha256), 64)
        self.assertEqual(len(result.verifier_sha256), 64)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["temperature"], 0.3)
        self.assertEqual(requests[0]["max_tokens"], 8192)
        self.assertNotIn("not-a-real-key", json.dumps(result.evidence()))

    def test_model_and_local_verifier_disagreement_blocks_content(self):
        def mutate(projection):
            projection["observations"][0]["value"] += 1

        result = self._provider(
            lambda _: _response(self.requirement, mutate=mutate)
        ).generate(self.requirement)
        self.assertFalse(result.ready)
        self.assertEqual(result.issues[0].code, "model_verifier_disagreement")
        self.assertTrue(result.online_api_used)

    def test_unknown_topology_can_only_return_verified_block(self):
        requirement = "设计三个房间组成的任意热网络，参数稍后再给"
        result = self._provider(lambda _: _response(requirement)).generate(requirement)
        self.assertFalse(result.ready)
        self.assertEqual(result.issues[0].code, "no_certified_template")
        self.assertTrue(result.online_api_used)

    def test_malformed_or_wrong_model_response_is_infrastructure_error(self):
        malformed = {
            "model": MODEL,
            "choices": [{"message": {"content": "```json\n{}\n```"}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        with self.assertRaises(OnlineGenerationError):
            self._provider(lambda _: malformed).generate(self.requirement)
        wrong = _response(self.requirement)
        wrong["model"] = "different-model"
        with self.assertRaises(OnlineGenerationError):
            self._provider(lambda _: wrong).generate(self.requirement)


if __name__ == "__main__":
    unittest.main()
