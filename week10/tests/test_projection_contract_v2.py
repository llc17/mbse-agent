from __future__ import annotations

from dataclasses import asdict
import json
import unittest

from week9.src.generation import DeterministicNaturalLanguageProvider
from week9.src.online_generation import ONLINE_PROJECTION_SCHEMA, _parse_projection, _verify_projection
from week9.src.schemas import sha256_text
from week10.experiments.projection_probe import inspect_response
from week10.src.projection_contract import PROMPT_SHA256 as V1_PROMPT_SHA256
from week10.src.projection_contract_v2 import (
    BASE_PROMPT_SHA256,
    PROMPT_SHA256,
    PROMPT_VERSION,
    SYSTEM_PROMPT,
    V1_SYSTEM_PROMPT,
)


SYNTHETIC_DUAL = (
    "设计一个双房间热传导系统：房间A热容 101 J/K，房间B热容 202 J/K，"
    "两房间之间墙体热阻 3 K/W，房间A外墙热阻 4 K/W，房间B外墙热阻 5 K/W，"
    "两房间初始温度均为 16 °C，室外温度 27 °C，室外温度 27 °C"
)


def _projection(requirement: str) -> dict[str, object]:
    local = DeterministicNaturalLanguageProvider().generate(requirement)
    return {
        "schema_version": ONLINE_PROJECTION_SCHEMA,
        "status": "ready" if local.ready else "blocked",
        "template_id": local.template_id,
        "observations": [asdict(item) for item in local.observations],
        "issues": [] if local.ready else [asdict(item) for item in local.issues],
    }


def _response(projection: dict[str, object]) -> dict[str, object]:
    return {
        "model": "synthetic-local-verifier",
        "choices": [{"message": {"content": json.dumps(projection, ensure_ascii=False, separators=(",", ":"))}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


class ProjectionContractV2Tests(unittest.TestCase):
    def test_v1_prompt_is_retained_and_v2_hash_is_versioned(self) -> None:
        self.assertTrue(SYSTEM_PROMPT.startswith(V1_SYSTEM_PROMPT))
        self.assertEqual(BASE_PROMPT_SHA256, V1_PROMPT_SHA256)
        self.assertEqual(PROMPT_VERSION, "v9-certified-requirement-projection-prompt-2")
        self.assertNotEqual(PROMPT_SHA256, V1_PROMPT_SHA256)
        self.assertEqual(PROMPT_SHA256, sha256_text(PROMPT_VERSION + "\n" + SYSTEM_PROMPT))
        for retained in (
            "(?<![A-Za-z])RC(?![A-Za-z])",
            "Equivalent repeats use the first occurrence",
            "C=1/((2*pi*f0)^2*L)",
            '"source_text":"电阻 3 kOhm"',
        ):
            self.assertIn(retained, SYSTEM_PROMPT)

    def test_checklist_has_exact_registry_counts_and_rlc_mapping(self) -> None:
        for requirement in (
            "rc_filter=4",
            "rlc_filter=5",
            "opamp_inverting=5",
            "thermal_single_room=4",
            "thermal_dual_room=8",
            "Each canonical name occurs exactly once",
            "f0 means the captured cutoff_frequency normalized by V8 to\nHz",
            "L means the captured inductance normalized by V8 to H",
            "separate resonance-frequency field",
        ):
            self.assertIn(requirement, SYSTEM_PROMPT)

    def test_synthetic_repeat_and_joint_initials_are_accepted_as_eight(self) -> None:
        projection = _projection(SYNTHETIC_DUAL)
        observations = projection["observations"]
        self.assertEqual(len(observations), 8)
        by_name = {item["name"]: item for item in observations}  # type: ignore[index]
        self.assertEqual(len(by_name), 8)
        self.assertEqual(by_name["outdoor_temperature"]["source_text"], "室外温度 27 °C")
        self.assertEqual(by_name["initial_temperature_a"]["source_text"], by_name["initial_temperature_b"]["source_text"])
        inspection = inspect_response(_response(projection), SYNTHETIC_DUAL)
        self.assertTrue(inspection["projection_accepted"])
        self.assertEqual(inspection["expected_parameter_count"], 8)
        self.assertEqual(inspection["observed_parameter_count"], 8)

    def test_frozen_verifier_rejects_missing_or_duplicate_observations(self) -> None:
        local = DeterministicNaturalLanguageProvider().generate(SYNTHETIC_DUAL)
        accepted = _projection(SYNTHETIC_DUAL)
        for mutation in ("missing", "duplicate"):
            with self.subTest(mutation=mutation):
                candidate = dict(accepted)
                observations = [dict(item) for item in accepted["observations"]]  # type: ignore[index]
                if mutation == "missing":
                    observations.pop()
                else:
                    observations.append(dict(observations[0]))
                candidate["observations"] = observations
                parsed = _parse_projection(json.dumps(candidate, ensure_ascii=False, separators=(",", ":")))
                self.assertTrue(_verify_projection(parsed, local))

    def test_source_value_and_unit_behavior_stays_with_frozen_verifier(self) -> None:
        local = DeterministicNaturalLanguageProvider().generate(SYNTHETIC_DUAL)
        projection = _projection(SYNTHETIC_DUAL)
        parsed = _parse_projection(json.dumps(projection, ensure_ascii=False, separators=(",", ":")))
        self.assertEqual(_verify_projection(parsed, local), ())
        outdoor = next(item for item in projection["observations"] if item["name"] == "outdoor_temperature")  # type: ignore[index]
        self.assertEqual((outdoor["value"], outdoor["unit"]), (27.0, "°C"))


if __name__ == "__main__":
    unittest.main()
