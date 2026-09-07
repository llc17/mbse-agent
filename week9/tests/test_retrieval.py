from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

from week9.src.retrieval import (
    DEFAULT_CERTIFICATION_PATH,
    DEFAULT_CORPUS_PATH,
    RetrievalConfigurationError,
    RetrievalQuery,
    estimate_tokens,
    load_default_corpus,
    load_default_policy,
    load_knowledge_corpus,
    retrieve_knowledge,
)


WEEK9_ROOT = Path(__file__).resolve().parents[1]


def _snapshot_hash(text: str = "same frozen failed snapshot") -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _alias_query(**changes) -> RetrievalQuery:
    values = {
        "template_id": "rlc_filter",
        "domain": "electrical",
        "target_language": "structured_parameter",
        "repair_layer": "requirement",
        "check_type": "requirement_normalization",
        "failure_category": "parameter_normalization_failed",
        "diagnostic_subcategory": "unknown_parameter_alias",
        "text": "rlc_filter has unknown structured parameter source_amplitude",
        "trigger_reason": "explicit_knowledge_gap",
        "failed_snapshot_sha256": _snapshot_hash(),
        "local_resolution_attempted": True,
        "symbols": ("source_amplitude",),
        "library": "Modelica Standard Library",
        "library_version": "4.0.0",
        "allowed_scope": "alias_resolution",
    }
    values.update(changes)
    return RetrievalQuery(**values)


class RetrievalTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.corpus = load_default_corpus()
        cls.disabled_policy = load_default_policy(cls.corpus)
        cls.enabled_policy = replace(cls.disabled_policy, enabled=True)

    def test_frozen_corpus_is_small_certified_and_excludes_evaluation_sources(self):
        self.assertEqual(len(self.corpus.fragments), 5)
        self.assertFalse(self.corpus.formal_experiment_authorized)
        self.assertFalse(self.corpus.holdout_content_included)
        for fragment in self.corpus.fragments:
            with self.subTest(fragment=fragment.fragment_id):
                self.assertLessEqual(estimate_tokens(fragment.prompt_text), 160)
                source = fragment.source.path.casefold().replace("\\", "/")
                self.assertNotIn("results/", source)
                self.assertNotIn("holdout", source)
                self.assertFalse(source.endswith("cases.py"))
                self.assertIn(
                    fragment.validation.status,
                    {"compile_verified", "behavior_verified", "certified"},
                )

    def test_default_policy_is_disabled_and_returns_no_context(self):
        result = retrieve_knowledge(
            _alias_query(), self.corpus, self.disabled_policy
        )
        self.assertFalse(result.attempted)
        self.assertFalse(result.triggered)
        self.assertEqual(result.reason, "retrieval_disabled")
        self.assertEqual(result.context, "")
        self.assertEqual(result.estimated_tokens, 0)

    def test_explicit_alias_gap_retrieves_one_attributed_fragment(self):
        result = retrieve_knowledge(
            _alias_query(), self.corpus, self.enabled_policy
        )
        self.assertTrue(result.attempted)
        self.assertTrue(result.triggered)
        self.assertEqual(
            result.fragment_ids, ("parameter.rlc.source-amplitude.001",)
        )
        self.assertIn("source_amplitude denotes canonical input_voltage", result.context)
        self.assertLessEqual(result.estimated_tokens, 160)
        evidence = result.evidence()
        self.assertEqual(evidence["failed_snapshot_sha256"], _snapshot_hash())
        self.assertEqual(evidence["corpus_sha256"], self.corpus.sha256)
        self.assertEqual(evidence["token_estimator"], "unicode-regex-estimator-1")
        self.assertEqual(evidence["query"]["template_id"], "rlc_filter")
        self.assertEqual(
            evidence["query"]["diagnostic_subcategory"],
            "unknown_parameter_alias",
        )

    def test_retrieval_result_rejects_forged_tokens_or_disabled_policy(self):
        result = retrieve_knowledge(
            _alias_query(), self.corpus, self.enabled_policy
        )
        with self.assertRaises(RetrievalConfigurationError):
            replace(result, estimated_tokens=result.estimated_tokens + 1)
        with self.assertRaises(RetrievalConfigurationError):
            replace(result, policy=replace(result.policy, enabled=False))

    def test_same_error_type_without_exact_alias_match_returns_empty(self):
        result = retrieve_knowledge(
            _alias_query(
                text="rlc_filter has unknown structured parameter mystery_gain",
                symbols=("mystery_gain",),
            ),
            self.corpus,
            self.enabled_policy,
        )
        self.assertTrue(result.attempted)
        self.assertFalse(result.triggered)
        self.assertEqual(result.reason, "no_valid_fragment_match")

    def test_template_identity_is_a_hard_filter(self):
        result = retrieve_knowledge(
            _alias_query(template_id="rc_filter"),
            self.corpus,
            self.enabled_policy,
        )
        self.assertTrue(result.attempted)
        self.assertFalse(result.triggered)
        self.assertEqual(result.reason, "no_valid_fragment_match")

    def test_domain_language_error_and_library_are_hard_filters(self):
        mismatches = (
            {"domain": "thermal"},
            {"target_language": "modelica", "repair_layer": "modelica"},
            {"diagnostic_subcategory": "unknown_port_or_member"},
            {"failure_category": "modelica_build_failed"},
            {"library_version": "3.2.3"},
        )
        for change in mismatches:
            with self.subTest(change=change):
                result = retrieve_knowledge(
                    _alias_query(**change), self.corpus, self.enabled_policy
                )
                self.assertTrue(result.attempted)
                self.assertFalse(result.triggered)
                self.assertEqual(result.reason, "no_valid_fragment_match")

    def test_modelica_fragment_is_blocked_on_certified_template_fast_path(self):
        query = RetrievalQuery(
            template_id="thermal_single_room",
            domain="thermal",
            target_language="modelica",
            repair_layer="modelica",
            check_type="modelica_compile",
            failure_category="modelica_build_failed",
            diagnostic_subcategory="unknown_class_or_library",
            text="FixedTemperatureBoundary is an unknown class",
            trigger_reason="local_repair_failed",
            failed_snapshot_sha256=_snapshot_hash("thermal model"),
            local_resolution_attempted=True,
            symbols=("FixedTemperature",),
            library="Modelica Standard Library",
            library_version="4.0.0",
            allowed_scope="component_class_path",
            active_contexts=("certified_template_slot_only_path",),
        )
        result = retrieve_knowledge(query, self.corpus, self.enabled_policy)
        self.assertTrue(result.attempted)
        self.assertFalse(result.triggered)
        self.assertEqual(result.reason, "no_valid_fragment_match")

    def test_local_resolution_is_required_before_retrieval(self):
        result = retrieve_knowledge(
            _alias_query(local_resolution_attempted=False),
            self.corpus,
            self.enabled_policy,
        )
        self.assertFalse(result.attempted)
        self.assertEqual(result.reason, "local_resolution_required_first")

    def test_infrastructure_failure_never_retrieves(self):
        result = retrieve_knowledge(
            _alias_query(failure_kind="infrastructure"),
            self.corpus,
            self.enabled_policy,
        )
        self.assertFalse(result.attempted)
        self.assertEqual(result.reason, "infrastructure_failure_not_retrievable")

    def test_unknown_diagnostic_and_second_event_fail_closed(self):
        unknown = retrieve_knowledge(
            _alias_query(diagnostic_subcategory="unknown_diagnostic"),
            self.corpus,
            self.enabled_policy,
        )
        exhausted = retrieve_knowledge(
            _alias_query(),
            self.corpus,
            self.enabled_policy,
            prior_retrieval_events=1,
        )
        self.assertEqual(unknown.reason, "diagnostic_subcategory_not_allowed")
        self.assertEqual(exhausted.reason, "retrieval_event_budget_exhausted")
        self.assertFalse(unknown.triggered)
        self.assertFalse(exhausted.triggered)

    def test_fragment_is_never_truncated_to_fit_budget(self):
        tiny = replace(
            self.enabled_policy,
            max_fragment_tokens=10,
            max_total_retrieval_tokens=10,
        )
        result = retrieve_knowledge(_alias_query(), self.corpus, tiny)
        self.assertTrue(result.attempted)
        self.assertFalse(result.triggered)
        self.assertEqual(result.context, "")

    def test_frozen_hash_tamper_is_rejected(self):
        with self.assertRaisesRegex(RetrievalConfigurationError, "corpus hash"):
            load_knowledge_corpus(
                DEFAULT_CORPUS_PATH,
                certification_path=DEFAULT_CERTIFICATION_PATH,
                project_root=WEEK9_ROOT,
                expected_sha256="0" * 64,
            )

    def test_duplicate_json_keys_are_rejected(self):
        original = DEFAULT_CORPUS_PATH.read_text(encoding="utf-8")
        duplicated = original.replace(
            '"schema_version": "v8-retrieval-corpus-1",',
            '"schema_version": "v8-retrieval-corpus-1",\n'
            '  "schema_version": "v8-retrieval-corpus-1",',
            1,
        )
        with tempfile.TemporaryDirectory(prefix="v8-retrieval-duplicate-") as root:
            path = Path(root) / "duplicate.json"
            path.write_text(duplicated, encoding="utf-8")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with self.assertRaisesRegex(RetrievalConfigurationError, "duplicate JSON key"):
                load_knowledge_corpus(
                    path,
                    certification_path=DEFAULT_CERTIFICATION_PATH,
                    project_root=WEEK9_ROOT,
                    expected_sha256=digest,
                )

    def test_nonstandard_nan_json_is_rejected(self):
        original = DEFAULT_CORPUS_PATH.read_text(encoding="utf-8")
        nonstandard = original.replace('"revision": 1', '"revision": NaN', 1)
        with tempfile.TemporaryDirectory(prefix="v8-retrieval-nan-") as root:
            path = Path(root) / "nan.json"
            path.write_text(nonstandard, encoding="utf-8")
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            with self.assertRaisesRegex(
                RetrievalConfigurationError, "non-standard numeric constant"
            ):
                load_knowledge_corpus(
                    path,
                    certification_path=DEFAULT_CERTIFICATION_PATH,
                    project_root=WEEK9_ROOT,
                    expected_sha256=digest,
                )

    def test_content_source_and_certificate_tampering_are_rejected(self):
        original = json.loads(DEFAULT_CORPUS_PATH.read_text(encoding="utf-8"))
        certification = json.loads(
            DEFAULT_CERTIFICATION_PATH.read_text(encoding="utf-8")
        )
        mutations = []

        content_tamper = json.loads(json.dumps(original))
        content_tamper["fragments"][0]["content"] += " forged"
        mutations.append((content_tamper, certification, "content hash"))

        source_tamper = json.loads(json.dumps(original))
        source_tamper["fragments"][0]["source"]["source_sha256"] = "0" * 64
        mutations.append((source_tamper, certification, "source hash"))

        certificate_tamper = json.loads(json.dumps(certification))
        certificate_tamper["templates"]["rlc_filter"]["full_gate"] = "FAIL"
        mutations.append((original, certificate_tamper, "not fully passing"))

        implementation_tamper = json.loads(json.dumps(certification))
        implementation_tamper["implementation_sha256"][
            "src/modelica_templates.py"
        ] = "0" * 64
        mutations.append((original, implementation_tamper, "implementation is stale"))

        with tempfile.TemporaryDirectory(prefix="v8-retrieval-tamper-") as root:
            root_path = Path(root)
            for index, (corpus_data, certificate_data, message) in enumerate(mutations):
                with self.subTest(message=message):
                    corpus_path = root_path / f"corpus-{index}.json"
                    certificate_path = root_path / f"certificate-{index}.json"
                    corpus_path.write_text(
                        json.dumps(corpus_data, ensure_ascii=False), encoding="utf-8"
                    )
                    certificate_path.write_text(
                        json.dumps(certificate_data, ensure_ascii=False),
                        encoding="utf-8",
                    )
                    digest = hashlib.sha256(corpus_path.read_bytes()).hexdigest()
                    with self.assertRaisesRegex(RetrievalConfigurationError, message):
                        load_knowledge_corpus(
                            corpus_path,
                            certification_path=certificate_path,
                            project_root=WEEK9_ROOT,
                            expected_sha256=digest,
                        )


if __name__ == "__main__":
    unittest.main()
