"""Offline, paired Stage-3 retrieval mechanism ablation.

This development probe starts both branches from the same immutable structured
parameter snapshot.  The local alias registry deliberately does not recognize
``source_amplitude``.  Retrieval-off therefore stops at the required
normalization gate; retrieval-on may use one frozen, source-attributed alias
fragment and must then pass the complete real certified-template hard gate.

The probe does not access holdout cases, use an online model, or authorize
retrieval by default.  Repeating one synthetic knowledge gap demonstrates only
mechanism stability, not end-to-end generalization.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import Any, Sequence

from week9.src.parameter_normalization import (
    ParameterObservation,
    normalize_parameters,
    normalize_parameters_with_retrieval,
    parameter_observations_sha256,
)
from week9.src.pipeline import CertifiedTemplateRepairPipeline
from week9.src.repair_loop import ArtifactBundle, RepairLoopConfig
from week9.src.retrieval import (
    DEFAULT_POLICY_PATH,
    RetrievalQuery,
    load_default_corpus,
    load_default_policy,
    retrieve_knowledge,
)
from week9.src.schemas import (
    ArtifactRecord,
    CheckStatus,
    FailureKind,
    ModelConfiguration,
    QualityCheckResult,
    RepairStopReason,
    ReproductionManifest,
    RunCounters,
    RunResult,
    sha256_text,
)


WEEK9_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RESULTS_ROOT = WEEK9_ROOT / "results"
CASE_ID = "stage3_dev_rlc_source_amplitude_alias"
TEMPLATE_ID = "rlc_filter"
REQUIREMENT_TEXT = (
    "Development-only structured RLC probe: R=100 Ohm, L=10 mH, "
    "C=25.33029591058444 nF, f0=10 kHz, source_amplitude=5 V."
)


def development_observations() -> tuple[ParameterObservation, ...]:
    return (
        ParameterObservation("R", 100.0, "Ohm", "R=100 Ohm"),
        ParameterObservation("L", 10.0, "mH", "L=10 mH"),
        ParameterObservation(
            "C", 25.33029591058444, "nF", "C=25.33029591058444 nF"
        ),
        ParameterObservation("f0", 10.0, "kHz", "f0=10 kHz"),
        ParameterObservation(
            "source_amplitude", 5.0, "V", "source_amplitude=5 V"
        ),
    )


def run_ablation(
    *,
    output_directory: Path,
    repeats: int = 3,
) -> dict[str, Any]:
    if isinstance(repeats, bool) or not isinstance(repeats, int) or repeats < 1:
        raise ValueError("repeats must be a positive integer")
    output_directory = output_directory.resolve()
    output_directory.mkdir(parents=True, exist_ok=False)
    run_root = output_directory / "runs"
    run_root.mkdir()

    corpus = load_default_corpus()
    installed_policy = load_default_policy(corpus)
    if installed_policy.enabled:
        raise RuntimeError("installed retrieval policy must remain disabled")
    off_policy = installed_policy
    on_policy = replace(installed_policy, enabled=True)
    observations = development_observations()
    snapshot_sha256 = parameter_observations_sha256(TEMPLATE_ID, observations)
    input_snapshot_sha256 = sha256_text(
        json.dumps(
            {
                "template_id": TEMPLATE_ID,
                "requirement_text": REQUIREMENT_TEXT,
                "parameter_observation_sha256": snapshot_sha256,
            },
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    local_failure = normalize_parameters(TEMPLATE_ID, observations)
    local_issue_codes = [item.code for item in local_failure.issues]
    local_missing = [
        item.canonical_name
        for item in local_failure.parameters
        if item.status.value == "missing"
    ]
    if (
        local_failure.ready
        or local_issue_codes.count("unknown_parameter_alias") != 1
        or local_issue_codes.count("missing_parameter") != 1
        or local_missing != ["input_voltage"]
        or [item.name for item in local_failure.unmapped_observations]
        != ["source_amplitude"]
    ):
        raise RuntimeError(
            "development probe no longer demonstrates one precise local alias gap"
        )
    local_failure_fingerprint = sha256_text(
        json.dumps(
            local_failure.evidence(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    # Retrieval is invoked only after the deterministic local resolver has
    # produced and fingerprinted the exact unknown-alias failure above.
    query = RetrievalQuery(
        template_id=TEMPLATE_ID,
        domain="electrical",
        target_language="structured_parameter",
        repair_layer="requirement",
        check_type="requirement_normalization",
        failure_category="parameter_normalization_failed",
        diagnostic_subcategory="unknown_parameter_alias",
        text="rlc_filter unknown structured parameter source_amplitude",
        trigger_reason="explicit_knowledge_gap",
        failed_snapshot_sha256=snapshot_sha256,
        local_resolution_attempted=True,
        symbols=("source_amplitude",),
        library="Modelica Standard Library",
        library_version="4.0.0",
        allowed_scope="alias_resolution",
    )
    off_retrieval = retrieve_knowledge(query, corpus, off_policy)
    on_retrieval = retrieve_knowledge(query, corpus, on_policy)
    if off_retrieval.triggered or not on_retrieval.triggered:
        raise RuntimeError("paired retrieval switch did not create the intended fork")

    budget = RepairLoopConfig(
        max_repairs=1,
        max_content_repairs=1,
        max_upstream_redos=1,
        max_infrastructure_retries=1,
    )
    repetitions: list[dict[str, Any]] = []
    for repetition in range(1, repeats + 1):
        off_normalization = normalize_parameters_with_retrieval(
            TEMPLATE_ID, observations, off_retrieval
        )
        if off_normalization.ready:
            raise RuntimeError("retrieval-off unexpectedly resolved the knowledge gap")
        off_result = _normalization_failure_result(
            repetition=repetition,
            normalization=off_normalization,
            retrieval_evidence=off_retrieval.evidence(),
            corpus_sha256=corpus.sha256,
            budget=budget,
        )

        on_workspace = run_root / f"repeat-{repetition:02d}-on"
        on_workspace.mkdir()
        pipeline = CertifiedTemplateRepairPipeline.from_observations(
            template_id=TEMPLATE_ID,
            model_name=f"Stage3RetrievedRlc{repetition:02d}",
            observations=observations,
            workspace_root=on_workspace,
            retrieval_result=on_retrieval,
        )
        initial = ArtifactBundle(
            requirement=REQUIREMENT_TEXT,
            sysml=pipeline.canonical_sysml,
            modelica=pipeline.canonical_render.code,
        )
        if parameter_observations_sha256(TEMPLATE_ID, observations) != snapshot_sha256:
            raise RuntimeError("parameter snapshot changed between paired branches")
        on_result = pipeline.run(
            case_id=CASE_ID,
            run_id=f"retrieval-on-{repetition:02d}",
            initial_artifacts=initial,
            config=budget,
        )

        off_path = output_directory / f"repeat-{repetition:02d}-off.json"
        on_path = output_directory / f"repeat-{repetition:02d}-on.json"
        off_sha256 = _write_json(off_path, off_result.to_dict())
        on_sha256 = _write_json(on_path, on_result.to_dict())
        repetitions.append(
            {
                "repetition": repetition,
                "parameter_observation_snapshot_sha256": snapshot_sha256,
                "input_snapshot_sha256": input_snapshot_sha256,
                "same_input_observation_snapshot": True,
                "same_budget": True,
                "off": _branch_summary(off_result, off_path, off_sha256),
                "on": _branch_summary(on_result, on_path, on_sha256),
            }
        )

    off_successes = sum(item["off"]["overall_success"] for item in repetitions)
    on_successes = sum(item["on"]["overall_success"] for item in repetitions)
    hard_regressions = sum(
        item["off"]["overall_success"] and not item["on"]["overall_success"]
        for item in repetitions
    )
    report = {
        "schema_version": "v8-retrieval-ablation-1",
        "experiment_scope": "development_mechanism_probe",
        "case_id": CASE_ID,
        "template_id": TEMPLATE_ID,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "formal_holdout_access": False,
        "formal_experiment_authorized": False,
        "online_api_used": False,
        "parameter_observation_snapshot": {
            "sha256": snapshot_sha256,
            "combined_input_sha256": input_snapshot_sha256,
            "requirement_text": REQUIREMENT_TEXT,
            "observations": [asdict(item) for item in observations],
        },
        "pre_retrieval_local_failure": {
            "attempted": True,
            "ready": local_failure.ready,
            "failure_fingerprint": local_failure_fingerprint,
            "issue_codes": local_issue_codes,
            "missing_parameters": local_missing,
            "unmapped_names": [
                item.name for item in local_failure.unmapped_observations
            ],
        },
        "retrieval": {
            "corpus_id": corpus.corpus_id,
            "corpus_sha256": corpus.sha256,
            "policy_path": str(DEFAULT_POLICY_PATH),
            "policy_sha256": _file_sha256(DEFAULT_POLICY_PATH),
            "installed_default_enabled": installed_policy.enabled,
            "off_enabled": off_policy.enabled,
            "on_enabled": on_policy.enabled,
            "fragment_ids": list(on_retrieval.fragment_ids),
            "estimated_tokens": on_retrieval.estimated_tokens,
            "token_estimator": on_policy.token_estimator,
            "limits": {
                "top_k": on_policy.top_k,
                "max_fragment_tokens": on_policy.max_fragment_tokens,
                "max_total_retrieval_tokens": on_policy.max_total_retrieval_tokens,
                "max_retrieval_events_per_run": on_policy.max_retrieval_events_per_run,
            },
        },
        "fairness": {
            "same_parameter_observation_snapshot": all(
                item["same_input_observation_snapshot"] for item in repetitions
            ),
            "same_maximum_repair_budget": all(
                item["same_budget"] for item in repetitions
            ),
            "same_normalization_validator_before_fork": True,
            "downstream_validation_policy": (
                "retrieval-off is correctly blocked at required normalization; "
                "retrieval-on must additionally pass the complete hard gate"
            ),
            "same_downstream_validator_claimed": False,
            "online_model_calls_per_branch": 0,
            "only_intended_difference_after_local_failure": "retrieval enabled flag",
            "budget": {
                "max_repairs": budget.max_repairs,
                "max_content_repairs": budget.max_content_repairs,
                "max_upstream_redos": budget.max_upstream_redos,
                "max_infrastructure_retries": budget.max_infrastructure_retries,
            },
        },
        "repetitions": repetitions,
        "summary": {
            "paired_repetitions": repeats,
            "retrieval_off_hard_successes": off_successes,
            "retrieval_on_hard_successes": on_successes,
            "paired_hard_success_gain": on_successes - off_successes,
            "hard_regressions": hard_regressions,
            "all_on_runs_passed": on_successes == repeats,
            "all_off_runs_blocked": off_successes == 0,
            "mechanism_gain_observed": on_successes > off_successes
            and hard_regressions == 0,
            "stage3_default_enablement_gate_claimed": False,
            "default_enablement_decision": "KEEP_DISABLED",
            "decision_reason": (
                "one synthetic development knowledge gap, even when repeated, "
                "does not establish independent end-to-end generalization"
            ),
        },
    }
    _write_json(output_directory / "retrieval-ablation.json", report)
    return report


def _normalization_failure_result(
    *,
    repetition: int,
    normalization,
    retrieval_evidence: dict[str, Any],
    corpus_sha256: str,
    budget: RepairLoopConfig,
) -> RunResult:
    issues = [item.message for item in normalization.issues]
    check = QualityCheckResult(
        check_type="requirement_normalization",
        status=CheckStatus.FAIL,
        required=True,
        issues=issues,
        evidence={
            "reason": "parameter_normalization_failed",
            "normalization": normalization.evidence(),
        },
        tool_name="V8 deterministic parameter normalizer",
        tool_version="stage3",
        failure_kind=FailureKind.CONTENT,
        failure_category="parameter_normalization_failed",
    )
    return RunResult(
        version="v8",
        case_id=CASE_ID,
        run_id=f"retrieval-off-{repetition:02d}",
        requirement_text=REQUIREMENT_TEXT,
        parameters=list(normalization.parameters),
        artifacts={
            "requirement": ArtifactRecord(
                "requirement",
                content=REQUIREMENT_TEXT,
                sha256=sha256_text(REQUIREMENT_TEXT),
            )
        },
        checks=[check],
        repair_stop_reason=RepairStopReason.UPSTREAM_REQUIRED,
        overall_success=False,
        primary_failure_category="parameter_normalization_failed",
        primary_failure_evidence=dict(check.evidence),
        counters=RunCounters(validation_count=1),
        model_configuration=ModelConfiguration(
            provider="deterministic_offline",
            model="retrieval_off_parameter_normalization",
            parameters={"template_id": TEMPLATE_ID, "retrieval_enabled": False},
        ),
        reproduction=ReproductionManifest(
            corpus_sha256=corpus_sha256,
            budgets={
                "max_repairs": budget.max_repairs,
                "max_content_repairs": budget.max_content_repairs,
                "max_upstream_redos": budget.max_upstream_redos,
                "max_infrastructure_retries": budget.max_infrastructure_retries,
            },
        ),
        raw_metrics={
            "online_api_used": False,
            "retrieval_decision": retrieval_evidence,
        },
    )


def _branch_summary(result: RunResult, path: Path, sha256: str) -> dict[str, Any]:
    required = [check for check in result.checks if check.required]
    return {
        "overall_success": result.overall_success,
        "required_checks": {
            check.check_type: check.status.value for check in required
        },
        "required_blockers": [
            check.check_type
            for check in required
            if check.status is not CheckStatus.PASS
        ],
        "repair_stop_reason": result.repair_stop_reason.value,
        "retrieval_triggered": result.retrieval_triggered,
        "retrieval_fragment_ids": list(result.retrieval_fragment_ids),
        "retrieval_tokens": result.token_usage.retrieval_tokens,
        "content_repair_count": result.counters.content_repair_count,
        "online_api_used": bool(result.raw_metrics.get("online_api_used")),
        "result_path": str(path),
        "result_sha256": sha256,
    }


def _write_json(path: Path, payload: Any) -> str:
    text = json.dumps(
        payload,
        ensure_ascii=False,
        allow_nan=False,
        indent=2,
    ) + "\n"
    path.write_text(text, encoding="utf-8")
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _default_output_directory() -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return DEFAULT_RESULTS_ROOT / f"retrieval-ablation-{stamp}"


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--out", type=Path, default=None)
    arguments = parser.parse_args(argv)
    output = arguments.out or _default_output_directory()
    report = run_ablation(output_directory=output, repeats=arguments.repeats)
    print(output)
    print(json.dumps(report["summary"], ensure_ascii=False, allow_nan=False))
    return 0 if report["summary"]["all_on_runs_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
