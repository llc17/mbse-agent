from __future__ import annotations

from dataclasses import replace
import unittest

from week9.src.repair_loop import (
    ArtifactBundle,
    RepairLoopConfig,
    RepairProposal,
    ValidationSnapshot,
    run_repair_loop,
)
from week9.src.routing import (
    CERTIFIED_HARD_GATE_PROFILE,
    build_adaptive_routing_evidence,
    build_budget_ledger,
    build_routing_trace,
    is_complete_hard_gate_profile,
    resolution_from_route,
)
from week9.src.schemas import (
    BudgetDimension,
    BudgetEvaluationStatus,
    CheckStatus,
    FailureKind,
    QualityCheckResult,
    RoutingAction,
    RoutingInputKind,
    RoutingPath,
    RoutingState,
    sha256_text,
)


def _pass(check_type: str) -> QualityCheckResult:
    return QualityCheckResult(
        check_type=check_type,
        status=CheckStatus.PASS,
        required=True,
        evidence={"profile": "certified-stage4-test"},
        tool_name="deterministic-test-validator",
        tool_version="1",
    )


def _failure(
    check_type: str,
    *,
    status: CheckStatus = CheckStatus.FAIL,
    kind: FailureKind = FailureKind.CONTENT,
    category: str = "injected_content_failure",
) -> QualityCheckResult:
    return QualityCheckResult(
        check_type=check_type,
        status=status,
        required=True,
        issues=[category],
        evidence={"reason": category},
        tool_name="deterministic-test-validator",
        tool_version="1",
        failure_kind=kind,
        failure_category=category,
    )


def _profile(
    replacement: QualityCheckResult | None = None,
    *,
    reverse: bool = False,
) -> ValidationSnapshot:
    checks = [
        replacement if replacement is not None and replacement.check_type == name else _pass(name)
        for name in CERTIFIED_HARD_GATE_PROFILE
    ]
    if reverse:
        checks.reverse()
    return ValidationSnapshot(tuple(checks))


def _normalization() -> dict:
    return {
        "template_id": "rc_filter",
        "ready": True,
        "parameters": [],
        "issues": [],
        "unmapped_observations": [],
        "retrieval": {"attempted": False, "triggered": False},
        "policy": "no_defaults_no_value_inference",
    }


class AdaptiveRoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.artifacts = ArtifactBundle("structured requirement", "sysml", "modelica")
        self.input_sha256 = sha256_text("ordered structured input")

    def _fast_loop(self, snapshot: ValidationSnapshot | None = None):
        initial = snapshot or _profile()
        loop = run_repair_loop(
            self.artifacts,
            lambda _: self.fail("a passing initial gate must not revalidate"),
            lambda _: self.fail("a passing initial gate must not repair"),
            initial_validation=initial,
        )
        return initial, loop

    def _route(self, initial, loop, **changes):
        values = {
            "loop": loop,
            "initial_artifacts": self.artifacts,
            "initial_validation": initial,
            "input_kind": RoutingInputKind.STRUCTURED_OBSERVATIONS,
            "input_snapshot_sha256": self.input_sha256,
            "certified_template_id": "rc_filter",
            "normalization_evidence": _normalization(),
        }
        values.update(changes)
        return build_routing_trace(**values)

    def test_complete_profile_is_exact_and_order_independent(self):
        forward = _profile()
        reverse = _profile(reverse=True)
        self.assertTrue(is_complete_hard_gate_profile(forward.checks))
        self.assertTrue(is_complete_hard_gate_profile(reverse.checks))
        self.assertFalse(is_complete_hard_gate_profile((_pass("modelica_compile"),)))

        forward_initial, forward_loop = self._fast_loop(forward)
        reverse_initial, reverse_loop = self._fast_loop(reverse)
        forward_route = self._route(forward_initial, forward_loop)
        reverse_route = self._route(reverse_initial, reverse_loop)

        self.assertEqual(
            forward_route.initial_validation_sha256,
            reverse_route.initial_validation_sha256,
        )
        self.assertEqual(
            [item.event_sha256 for item in forward_route.events],
            [item.event_sha256 for item in reverse_route.events],
        )

    def test_initial_complete_pass_is_the_only_fast_path(self):
        initial, loop = self._fast_loop()
        route = self._route(initial, loop)

        self.assertEqual(route.path, RoutingPath.FAST_PATH)
        self.assertEqual(route.final_state, RoutingState.SUCCEEDED)
        self.assertTrue(route.full_hard_gate_profile)
        self.assertFalse(route.escalation_reasons)
        self.assertEqual(loop.counters.validation_count, 1)
        self.assertEqual(resolution_from_route(route), "initial_hard_gate")
        self.assertEqual(route.events[-1].action, RoutingAction.RELEASE_INITIAL_GATE)

    def test_single_passing_check_cannot_claim_certified_success(self):
        initial = ValidationSnapshot((_pass("modelica_compile"),))
        loop = run_repair_loop(
            self.artifacts,
            lambda _: initial,
            lambda _: self.fail("provider must not run"),
            initial_validation=initial,
        )
        with self.assertRaisesRegex(ValueError, "complete hard-gate profile"):
            self._route(initial, loop)

    def test_structured_fast_path_requires_ready_local_normalization(self):
        initial, loop = self._fast_loop()
        unresolved = {
            **_normalization(),
            "ready": False,
            "issues": [{"code": "missing_parameter"}],
        }
        with self.assertRaisesRegex(ValueError, "inconsistent with loop escalation"):
            self._route(initial, loop, normalization_evidence=unresolved)

    def test_retrieval_assisted_initial_pass_is_escalated_not_fast(self):
        initial, loop = self._fast_loop()
        retrieval = {
            "attempted": True,
            "triggered": True,
            "reason": "fragments_selected",
            "failed_snapshot_sha256": self.input_sha256,
            "estimated_tokens": 7,
            "corpus_sha256": "a" * 64,
        }
        route = self._route(
            initial,
            loop,
            retrieval_evidence=retrieval,
        )

        self.assertEqual(route.path, RoutingPath.ESCALATION_PATH)
        self.assertTrue(route.retrieval_attempted)
        self.assertTrue(route.retrieval_triggered)
        self.assertIn(
            RoutingAction.RETRIEVE_CERTIFIED_ALIAS,
            [item.action for item in route.events],
        )
        self.assertEqual(
            resolution_from_route(route),
            "retrieval_assisted_parameter_normalization",
        )

    def test_infrastructure_retry_has_its_own_resolution(self):
        initial = _profile(
            _failure(
                "execution_provenance",
                status=CheckStatus.ERROR,
                kind=FailureKind.INFRASTRUCTURE,
                category="execution_provenance_missing",
            )
        )
        passing = _profile()
        loop = run_repair_loop(
            self.artifacts,
            lambda _: passing,
            lambda _: self.fail("infrastructure errors must not reach repair"),
            RepairLoopConfig(max_infrastructure_retries=1),
            initial_validation=initial,
        )
        route = self._route(initial, loop)

        self.assertEqual(route.path, RoutingPath.ESCALATION_PATH)
        self.assertTrue(route.infrastructure_retry_attempted)
        self.assertFalse(route.repair_attempted)
        self.assertIn(
            RoutingAction.RETRY_IDENTICAL_SNAPSHOT,
            [item.action for item in route.events],
        )
        self.assertEqual(resolution_from_route(route), "infrastructure_retry")

    def test_modelica_content_failure_routes_to_certified_rerender(self):
        initial = _profile(
            _failure("modelica_compile", category="modelica_build_failed")
        )
        passing = _profile()
        loop = run_repair_loop(
            self.artifacts,
            lambda _: passing,
            lambda _: RepairProposal("certified modelica"),
            initial_validation=initial,
        )
        route = self._route(initial, loop)

        self.assertTrue(route.repair_attempted)
        self.assertIn(
            RoutingAction.RERENDER_CERTIFIED_MODELICA,
            [item.action for item in route.events],
        )
        self.assertEqual(
            resolution_from_route(route), "deterministic_offline_repair"
        )

    def test_fast_budget_ledger_uses_derived_limits_and_not_frozen_status(self):
        initial, loop = self._fast_loop()
        config = RepairLoopConfig(
            max_repairs=3,
            max_content_repairs=2,
            max_upstream_redos=1,
            max_infrastructure_retries=2,
        )
        route = self._route(initial, loop)
        ledger = build_budget_ledger(
            loop=loop,
            config=config,
            initial_artifacts=self.artifacts,
            input_snapshot_sha256=self.input_sha256,
            route=route,
            retrieval_evidence=None,
            max_retrieval_events=1,
            max_retrieval_tokens=480,
            observed_duration_s=0.25,
        )
        limits = {item.dimension: item.limit for item in ledger.limits}

        self.assertEqual(limits[BudgetDimension.VALIDATION], 6)
        self.assertEqual(ledger.usage(BudgetDimension.VALIDATION), 1)
        self.assertEqual(
            ledger.token_budget_status, BudgetEvaluationStatus.NOT_FROZEN
        )
        self.assertEqual(
            ledger.duration_budget_status, BudgetEvaluationStatus.NOT_FROZEN
        )
        self.assertEqual(
            [item.sequence for item in ledger.events],
            list(range(1, len(ledger.events) + 1)),
        )
        for index, event in enumerate(ledger.events):
            expected_previous = "" if index == 0 else ledger.events[index - 1].event_sha256
            self.assertEqual(event.previous_event_sha256, expected_previous)

    def test_escalation_ledger_counts_every_auditable_record(self):
        initial = _profile(
            _failure("modelica_compile", category="modelica_build_failed")
        )
        passing = _profile()
        config = RepairLoopConfig(
            max_repairs=2,
            max_content_repairs=1,
            max_upstream_redos=1,
            max_infrastructure_retries=1,
        )
        loop = run_repair_loop(
            self.artifacts,
            lambda _: passing,
            lambda _: RepairProposal("fixed"),
            config,
            initial_validation=initial,
        )
        evidence = build_adaptive_routing_evidence(
            loop=loop,
            config=config,
            initial_artifacts=self.artifacts,
            initial_validation=initial,
            input_kind=RoutingInputKind.STRUCTURED_OBSERVATIONS,
            input_snapshot_sha256=self.input_sha256,
            certified_template_id="rc_filter",
            normalization_evidence=_normalization(),
            max_retrieval_events=1,
            max_retrieval_tokens=480,
            observed_model_tokens=0,
            observed_duration_s=0.5,
        )
        ledger = evidence.budget_ledger

        self.assertEqual(ledger.usage(BudgetDimension.VALIDATION), 2)
        self.assertEqual(ledger.usage(BudgetDimension.REPAIR_ATTEMPT), 1)
        self.assertEqual(
            ledger.usage(BudgetDimension.MODELICA_CHANGED_REPAIR), 1
        )
        self.assertEqual(ledger.usage(BudgetDimension.UPSTREAM_CHANGED_REPAIR), 0)
        self.assertEqual(
            ledger.token_budget_status, BudgetEvaluationStatus.NOT_APPLICABLE
        )
        self.assertEqual(
            ledger.duration_budget_status, BudgetEvaluationStatus.NOT_APPLICABLE
        )

    def test_retrieval_budget_records_attempt_and_exact_context_tokens(self):
        initial, loop = self._fast_loop()
        retrieval = {
            "attempted": True,
            "triggered": True,
            "reason": "fragments_selected",
            "failed_snapshot_sha256": self.input_sha256,
            "estimated_tokens": 11,
            "corpus_sha256": "b" * 64,
        }
        route = self._route(initial, loop, retrieval_evidence=retrieval)
        ledger = build_budget_ledger(
            loop=loop,
            config=RepairLoopConfig(),
            initial_artifacts=self.artifacts,
            input_snapshot_sha256=self.input_sha256,
            route=route,
            retrieval_evidence=retrieval,
            max_retrieval_events=1,
            max_retrieval_tokens=11,
        )

        self.assertEqual(ledger.usage(BudgetDimension.RETRIEVAL_EVENT), 1)
        self.assertEqual(ledger.usage(BudgetDimension.RETRIEVAL_CONTEXT_TOKEN), 11)

    def test_retrieval_evidence_must_bind_the_exact_input_snapshot(self):
        initial, loop = self._fast_loop()
        retrieval = {
            "attempted": True,
            "triggered": False,
            "reason": "no_valid_fragment_match",
            "failed_snapshot_sha256": "0" * 64,
            "estimated_tokens": 0,
        }
        with self.assertRaisesRegex(ValueError, "different input snapshot"):
            self._route(initial, loop, retrieval_evidence=retrieval)

    def test_raw_natural_language_entry_fails_closed(self):
        initial = ValidationSnapshot(
            (
                _failure(
                    "pipeline.available",
                    status=CheckStatus.ERROR,
                    kind=FailureKind.NOT_IMPLEMENTED,
                    category="not_implemented",
                ),
            )
        )
        loop = run_repair_loop(
            self.artifacts,
            lambda _: initial,
            lambda _: self.fail("not-implemented input must not repair"),
            initial_validation=initial,
        )
        route = build_routing_trace(
            loop=loop,
            initial_artifacts=self.artifacts,
            initial_validation=initial,
            input_kind=RoutingInputKind.RAW_NATURAL_LANGUAGE,
            input_snapshot_sha256=sha256_text("raw natural language"),
        )

        self.assertEqual(route.path, RoutingPath.UNAVAILABLE_ENTRY)
        self.assertEqual(route.final_state, RoutingState.FAILED)
        self.assertFalse(route.natural_language_interpretation_performed)
        self.assertEqual(route.events[-1].action, RoutingAction.STOP_NOT_IMPLEMENTED)
        self.assertEqual(resolution_from_route(route), "")

    def test_routing_and_budget_hash_tampering_is_rejected(self):
        initial, loop = self._fast_loop()
        route = self._route(initial, loop)
        with self.assertRaisesRegex(ValueError, "event hash"):
            replace(route.events[0], reason_code="forged")

        ledger = build_budget_ledger(
            loop=loop,
            config=RepairLoopConfig(),
            initial_artifacts=self.artifacts,
            input_snapshot_sha256=self.input_sha256,
            route=route,
            retrieval_evidence=None,
            max_retrieval_events=1,
            max_retrieval_tokens=480,
        )
        with self.assertRaisesRegex(ValueError, "event hash"):
            replace(ledger.events[0], action="forged")


if __name__ == "__main__":
    unittest.main()
