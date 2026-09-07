from __future__ import annotations

from dataclasses import replace
import json
import math
import tempfile
from types import SimpleNamespace
import unittest

from week9.src.modelica_templates import TEMPLATE_REGISTRY
from week9.src.hard_gate import REQUIRED_HARD_CHECK_TYPES
from week9.src.parameter_normalization import (
    ParameterAliasProjectionError,
    ParameterNormalizationError,
    ParameterObservation,
    aliases_for_template,
    normalize_parameters,
    normalize_parameters_with_retrieval,
    normalize_unit_label,
    parameter_observations_sha256,
)
from week9.src.pipeline import CertifiedTemplateRepairPipeline
from week9.src.repair_loop import ArtifactBundle, ValidationSnapshot
from week9.src.retrieval import (
    RetrievalQuery,
    load_default_corpus,
    load_default_policy,
    retrieve_knowledge,
)
from week9.src.schemas import CheckStatus, ParameterStatus, QualityCheckResult


def _observation(name, value, unit, source=None):
    return ParameterObservation(
        name=name,
        value=value,
        unit=unit,
        source_text=source or f"structured observation {name}",
    )


class ParameterNormalizationTests(unittest.TestCase):
    def _retrieved_rlc_alias(self, observations):
        corpus = load_default_corpus()
        policy = replace(load_default_policy(corpus), enabled=True)
        query = RetrievalQuery(
            template_id="rlc_filter",
            domain="electrical",
            target_language="structured_parameter",
            repair_layer="requirement",
            check_type="requirement_normalization",
            failure_category="parameter_normalization_failed",
            diagnostic_subcategory="unknown_parameter_alias",
            text="rlc_filter unknown source_amplitude",
            trigger_reason="explicit_knowledge_gap",
            failed_snapshot_sha256=parameter_observations_sha256(
                "rlc_filter", observations
            ),
            local_resolution_attempted=True,
            symbols=("source_amplitude",),
            library="Modelica Standard Library",
            library_version="4.0.0",
            allowed_scope="alias_resolution",
        )
        result = retrieve_knowledge(query, corpus, policy)
        self.assertTrue(result.triggered)
        return result

    def test_retrieved_alias_resolves_only_the_unknown_observation(self):
        observations = (
            _observation("R", 100.0, "Ohm"),
            _observation("L", 10.0, "mH"),
            _observation("C", 25.33029591058444, "nF"),
            _observation("f0", 10.0, "kHz"),
            _observation("source_amplitude", 5.0, "V"),
        )
        local = normalize_parameters("rlc_filter", observations)
        self.assertFalse(local.ready)
        self.assertEqual(
            {item.code for item in local.issues},
            {"unknown_parameter_alias", "missing_parameter"},
        )

        retrieval = self._retrieved_rlc_alias(observations)
        assisted = normalize_parameters_with_retrieval(
            "rlc_filter", observations, retrieval
        )
        self.assertTrue(assisted.ready, assisted.issues)
        voltage = {
            item.canonical_name: item for item in assisted.parameters
        }["input_voltage"]
        alias_evidence = voltage.conversion_evidence["observations"][0][
            "alias_resolution"
        ]
        self.assertEqual(
            alias_evidence["fragment_id"],
            "parameter.rlc.source-amplitude.001",
        )
        self.assertEqual(
            assisted.retrieval_evidence["failed_snapshot_sha256"],
            parameter_observations_sha256("rlc_filter", observations),
        )

    def test_pipeline_factory_accepts_only_snapshot_bound_retrieval(self):
        observations = (
            _observation("R", 100.0, "Ohm"),
            _observation("L", 10.0, "mH"),
            _observation("C", 25.33029591058444, "nF"),
            _observation("f0", 10.0, "kHz"),
            _observation("source_amplitude", 5.0, "V"),
        )
        retrieval = self._retrieved_rlc_alias(observations)
        with tempfile.TemporaryDirectory(prefix="v8-rag-normalized-") as temporary:
            pipeline = CertifiedTemplateRepairPipeline.from_observations(
                template_id="rlc_filter",
                model_name="RetrievedRlc",
                observations=observations,
                workspace_root=temporary,
                retrieval_result=retrieval,
            )
        self.assertTrue(pipeline.normalization_result.ready)
        self.assertIs(pipeline.retrieval_result, retrieval)
        self.assertIn("input_voltage = 5", pipeline.canonical_render.code)

        forged_query = replace(
            retrieval.query, failed_snapshot_sha256="0" * 64
        )
        forged = replace(retrieval, query=forged_query)
        with self.assertRaisesRegex(ParameterAliasProjectionError, "exact"):
            normalize_parameters_with_retrieval(
                "rlc_filter", observations, forged
            )

    def test_run_result_binds_retrieval_fragments_tokens_and_corpus(self):
        observations = (
            _observation("R", 100.0, "Ohm"),
            _observation("L", 10.0, "mH"),
            _observation("C", 25.33029591058444, "nF"),
            _observation("f0", 10.0, "kHz"),
            _observation("source_amplitude", 5.0, "V"),
        )
        retrieval = self._retrieved_rlc_alias(observations)
        with tempfile.TemporaryDirectory(prefix="v8-bound-retrieval-") as temporary:
            pipeline = CertifiedTemplateRepairPipeline.from_observations(
                template_id="rlc_filter",
                model_name="BoundRetrieval",
                observations=observations,
                workspace_root=temporary,
                retrieval_result=retrieval,
            )
            pipeline.tool = SimpleNamespace(version="mock-openmodelica")
            pipeline.sysml_parser = SimpleNamespace(version="mock-sysmlpy")
            pipeline.validate = lambda _: ValidationSnapshot(
                checks=tuple(
                    QualityCheckResult(
                        check_type, CheckStatus.PASS, True
                    )
                    for check_type in REQUIRED_HARD_CHECK_TYPES
                )
            )
            artifacts = ArtifactBundle(
                "requirement",
                pipeline.canonical_sysml,
                pipeline.canonical_render.code,
            )
            result = pipeline.run(
                case_id="retrieval-contract",
                run_id="retrieval-contract-run",
                initial_artifacts=artifacts,
            )
        self.assertTrue(result.overall_success)
        self.assertTrue(result.retrieval_triggered)
        self.assertEqual(
            result.retrieval_fragment_ids,
            ["parameter.rlc.source-amplitude.001"],
        )
        self.assertEqual(
            result.token_usage.retrieval_tokens, retrieval.estimated_tokens
        )
        self.assertEqual(result.reproduction.corpus_sha256, retrieval.corpus_sha256)
        self.assertEqual(
            result.resolved_by, "retrieval_assisted_parameter_normalization"
        )
        json.dumps(result.to_dict(), ensure_ascii=False, allow_nan=False)

    def test_retrieved_alias_cannot_forge_fragment_or_hide_conflict(self):
        observations = (
            _observation("R", 100.0, "Ohm"),
            _observation("L", 10.0, "mH"),
            _observation("C", 25.33029591058444, "nF"),
            _observation("f0", 10.0, "kHz"),
            _observation("source_amplitude", 5.0, "V"),
            _observation("source_amplitude", 6.0, "V"),
        )
        retrieval = self._retrieved_rlc_alias(observations)
        conflicted = normalize_parameters_with_retrieval(
            "rlc_filter", observations, retrieval
        )
        voltage = {
            item.canonical_name: item for item in conflicted.parameters
        }["input_voltage"]
        self.assertEqual(voltage.status, ParameterStatus.CONFLICT)
        self.assertIsNone(voltage.value)

        selected = retrieval.fragments[0]
        overlapping_fragment = replace(
            selected.fragment, parameter_aliases={"V": "input_voltage"}
        )
        overlapping_retrieval = replace(
            retrieval,
            fragments=(replace(selected, fragment=overlapping_fragment),),
        )
        with self.assertRaisesRegex(
            ParameterAliasProjectionError, "deterministic recomputation"
        ):
            normalize_parameters_with_retrieval(
                "rlc_filter", observations, overlapping_retrieval
            )

    def test_retrieved_alias_can_merge_an_equivalent_local_value(self):
        observations = (
            _observation("R", 100.0, "Ohm"),
            _observation("L", 10.0, "mH"),
            _observation("C", 25.33029591058444, "nF"),
            _observation("f0", 10.0, "kHz"),
            _observation("V", 5.0, "V"),
            _observation("source_amplitude", 5000.0, "mV"),
        )
        retrieval = self._retrieved_rlc_alias(observations)
        assisted = normalize_parameters_with_retrieval(
            "rlc_filter", observations, retrieval
        )
        self.assertTrue(assisted.ready, assisted.issues)
        voltage = {
            item.canonical_name: item for item in assisted.parameters
        }["input_voltage"]
        self.assertEqual(
            voltage.conversion_evidence["resolution"], "equivalent_duplicates"
        )
        self.assertEqual(voltage.aliases, ["V", "source_amplitude"])

    def test_pipeline_factory_uses_one_normalized_mapping_for_both_models(self):
        observations = (
            _observation("R", 1.0, "kOhm"),
            _observation("C", 159.15494309189535, "nF"),
            _observation("fc", 1.0, "kHz"),
            _observation("Vin_step", 5000.0, "mV"),
        )
        with tempfile.TemporaryDirectory(prefix="v8-normalized-pipeline-") as temporary:
            pipeline = CertifiedTemplateRepairPipeline.from_observations(
                template_id="rc_filter",
                model_name="NormalizedRc",
                observations=observations,
                workspace_root=temporary,
            )
        self.assertIsNotNone(pipeline.normalization_result)
        self.assertTrue(pipeline.normalization_result.ready)
        self.assertIn("resistance = 1000", pipeline.canonical_render.code)
        self.assertIn("attribute resistance : Real = 1000.0", pipeline.canonical_sysml)

    def test_pipeline_factory_refuses_unresolved_observations(self):
        with tempfile.TemporaryDirectory(prefix="v8-normalized-fail-") as temporary:
            with self.assertRaises(ParameterNormalizationError) as caught:
                CertifiedTemplateRepairPipeline.from_observations(
                    template_id="rc_filter",
                    model_name="MissingRc",
                    observations=(_observation("R", 1000.0, "Ohm"),),
                    workspace_root=temporary,
                )
        self.assertFalse(caught.exception.result.ready)
        self.assertEqual(
            sum(item.code == "missing_parameter" for item in caught.exception.result.issues),
            3,
        )

    def test_alias_registry_exactly_covers_each_template_schema(self):
        for template_id, spec in TEMPLATE_REGISTRY.items():
            with self.subTest(template_id=template_id):
                aliases = aliases_for_template(template_id)
                self.assertEqual(set(aliases), set(spec.parameter_names))
                self.assertTrue(all(name in values for name, values in aliases.items()))

    def test_rc_aliases_and_units_normalize_to_canonical_si(self):
        result = normalize_parameters(
            "rc_filter",
            (
                _observation("R", 1.0, "k Ω", "电阻 1 kΩ"),
                _observation("C", 159.15494309189535, "nF", "电容 159.154943 nF"),
                _observation("f_cutoff", 1.0, "kHz", "截止频率 1 kHz"),
                _observation("Vin_step", 5000.0, "mV", "输入 5000 mV"),
            ),
        )
        self.assertTrue(result.ready, result.issues)
        indexed = {item.canonical_name: item for item in result.parameters}
        self.assertEqual(indexed["resistance"].value, 1000.0)
        self.assertAlmostEqual(indexed["capacitance"].value, 1.5915494309189535e-7)
        self.assertEqual(indexed["cutoff_frequency"].value, 1000.0)
        self.assertEqual(indexed["input_voltage"].value, 5.0)
        self.assertEqual(indexed["input_voltage"].aliases, ["Vin_step"])

    def test_equivalent_duplicate_aliases_merge_with_full_evidence(self):
        result = normalize_parameters(
            "rc_filter",
            (
                _observation("R", 1.0, "kOhm"),
                _observation("resistance", 1000.0, "Ohm"),
                _observation("C", 1.5915494309189535e-7, "F"),
                _observation("fc", 1000.0, "Hz"),
                _observation("V_in", 5.0, "V"),
            ),
        )
        self.assertTrue(result.ready, result.issues)
        resistance = result.parameters[0]
        self.assertEqual(resistance.status, ParameterStatus.KNOWN)
        self.assertEqual(resistance.aliases, ["R", "resistance"])
        self.assertEqual(
            resistance.conversion_evidence["resolution"], "equivalent_duplicates"
        )
        self.assertEqual(len(resistance.conversion_evidence["observations"]), 2)

    def test_conflicting_alias_values_are_not_silently_selected(self):
        result = normalize_parameters(
            "rc_filter",
            (
                _observation("R", 1.0, "kOhm"),
                _observation("resistance", 2.0, "kOhm"),
                _observation("C", 1.0, "uF"),
                _observation("fc", 1000.0, "Hz"),
                _observation("V", 5.0, "V"),
            ),
        )
        resistance = result.parameters[0]
        self.assertFalse(result.ready)
        self.assertEqual(resistance.status, ParameterStatus.CONFLICT)
        self.assertIsNone(resistance.value)
        self.assertIn("conflicting_parameter_values", {item.code for item in result.issues})

    def test_missing_parameters_are_explicit_and_have_no_defaults(self):
        result = normalize_parameters(
            "rc_filter", (_observation("R", 1000.0, "Ohm"),)
        )
        indexed = {item.canonical_name: item for item in result.parameters}
        self.assertFalse(result.ready)
        self.assertEqual(indexed["capacitance"].status, ParameterStatus.MISSING)
        self.assertIsNone(indexed["capacitance"].value)
        self.assertEqual(
            sum(item.code == "missing_parameter" for item in result.issues), 3
        )

    def test_unknown_alias_is_preserved_as_unmapped_evidence(self):
        result = normalize_parameters(
            "rc_filter",
            (
                _observation("R", 1000.0, "Ohm"),
                _observation("C", 1.0, "uF"),
                _observation("fc", 1000.0, "Hz"),
                _observation("V", 5.0, "V"),
                _observation("mystery_gain", 3.0, "V"),
            ),
        )
        self.assertFalse(result.ready)
        self.assertEqual(result.unmapped_observations[0].name, "mystery_gain")
        self.assertIn("unknown_parameter_alias", {item.code for item in result.issues})

    def test_invalid_unit_and_nonfinite_value_become_unknown(self):
        result = normalize_parameters(
            "rc_filter",
            (
                _observation("R", 1000.0, "V"),
                _observation("C", math.nan, "F"),
                _observation("fc", 1000.0, "Hz"),
                _observation("V", 5.0, "V"),
            ),
        )
        indexed = {item.canonical_name: item for item in result.parameters}
        self.assertEqual(indexed["resistance"].status, ParameterStatus.UNKNOWN)
        self.assertEqual(indexed["capacitance"].status, ParameterStatus.UNKNOWN)
        self.assertFalse(result.ready)

    def test_generic_r_is_not_guessed_for_dual_room_topology(self):
        result = normalize_parameters(
            "thermal_dual_room", (_observation("R", 0.05, "K/W"),)
        )
        self.assertEqual(result.unmapped_observations[0].name, "R")
        self.assertTrue(
            all(item.status is ParameterStatus.MISSING for item in result.parameters)
        )

    def test_temperature_and_heat_capacity_aliases_record_conversions(self):
        result = normalize_parameters(
            "thermal_single_room",
            (
                _observation("Cth", 500.0, "kJ/K"),
                _observation("R_wall", 0.02, "K/W"),
                _observation("T_start", 20.0, "°C"),
                _observation("T_ambient", 35.0, "Celsius"),
            ),
        )
        self.assertTrue(result.ready, result.issues)
        indexed = {item.canonical_name: item for item in result.parameters}
        self.assertEqual(indexed["heat_capacity"].value, 500000.0)
        self.assertAlmostEqual(indexed["initial_temperature"].value, 293.15)
        self.assertAlmostEqual(indexed["outdoor_temperature"].value, 308.15)
        json.dumps(result.evidence(), ensure_ascii=False, allow_nan=False)

    def test_unit_label_normalization_is_explicit_not_case_guessing(self):
        self.assertEqual(normalize_unit_label(" k Ω "), "kOhm")
        self.assertEqual(normalize_unit_label("μF"), "uF")
        self.assertEqual(normalize_unit_label("℃"), "degC")
        with self.assertRaises(ValueError):
            normalize_unit_label("MV")

    def test_extended_prefixes_and_chinese_aliases_remain_template_scoped(self):
        result = normalize_parameters(
            "rc_filter",
            (
                _observation("电阻", 1.0, "MΩ"),
                _observation("电容", 159.15494309189535, "pF"),
                _observation("截止频率", 1.0, "kHz"),
                _observation("输入电压", 5.0, "V"),
            ),
        )
        self.assertTrue(result.ready, result.issues)
        indexed = {item.canonical_name: item for item in result.parameters}
        self.assertEqual(indexed["resistance"].value, 1_000_000.0)
        self.assertAlmostEqual(
            indexed["capacitance"].value, 159.15494309189535e-12
        )
        self.assertEqual(normalize_unit_label("MΩ"), "MOhm")
        self.assertEqual(normalize_unit_label("pF"), "pF")
        with self.assertRaises(ValueError):
            normalize_unit_label("mOhm")


if __name__ == "__main__":
    unittest.main()
