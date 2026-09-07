"""Offline Stage-2 comparison: targeted restoration versus unchanged retry."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import sys
from typing import Callable


PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from week9.src.modelica_templates import render_template, representative_parameters  # noqa: E402
from week9.src.modelica_tool import resolve_omc_path  # noqa: E402
from week9.src.pipeline import (  # noqa: E402
    CertifiedTemplateRepairPipeline,
    run_offline_repair_pipeline,
)
from week9.src.repair_loop import (  # noqa: E402
    ArtifactBundle,
    RepairLoopConfig,
    RepairProposal,
)
from week9.src.schemas import ModelConfiguration, sha256_text, utc_now_iso  # noqa: E402


@dataclass(frozen=True, slots=True)
class RepairScenario:
    scenario_id: str
    template_id: str
    model_name: str
    target_check: str
    injection: Callable[[str, str], str]


def _syntax_error(source: str, model_name: str) -> str:
    return source.replace(f"end {model_name};", f"end {model_name}")


def _wrong_component_path(source: str, _: str) -> str:
    return source.replace(
        "Modelica.Thermal.HeatTransfer.Sources.FixedTemperature",
        "Modelica.Thermal.HeatTransfer.Components.FixedTemperature",
    )


def _port_mismatch(source: str, _: str) -> str:
    return source.replace(
        "connect(source.p, resistor.p);",
        "connect(source.p, resistor.R);",
    )


def _wrong_rlc_stimulus(source: str, _: str) -> str:
    return source.replace(
        """Modelica.Electrical.Analog.Sources.SineVoltage source(
    V=input_voltage,
    f=cutoff_frequency,
    phase=0,
    offset=0,
    startTime=0);""",
        """Modelica.Electrical.Analog.Sources.StepVoltage source(
    V=input_voltage,
    startTime=0);""",
    )


REPAIR_SCENARIOS = (
    RepairScenario(
        "modelica_syntax_error",
        "rc_filter",
        "Stage2SyntaxRc",
        "modelica_compile",
        _syntax_error,
    ),
    RepairScenario(
        "wrong_component_path",
        "thermal_single_room",
        "Stage2WrongPathThermal",
        "modelica_compile",
        _wrong_component_path,
    ),
    RepairScenario(
        "port_mismatch",
        "rc_filter",
        "Stage2PortRc",
        "modelica_compile",
        _port_mismatch,
    ),
    RepairScenario(
        "compile_success_wrong_stimulus",
        "rlc_filter",
        "Stage2WrongStimulusRlc",
        "physical_behavior",
        _wrong_rlc_stimulus,
    ),
)


DEFAULT_CONFIG = RepairLoopConfig(
    max_repairs=2,
    max_content_repairs=2,
    max_upstream_redos=1,
    max_infrastructure_retries=1,
)


def run_regression(*, workspace_root: Path) -> dict:
    if resolve_omc_path() is None:
        raise RuntimeError("OpenModelica is unavailable; Stage-2 regression is not verifiable")
    cases = []
    for scenario in REPAIR_SCENARIOS:
        parameters = representative_parameters(scenario.template_id)
        canonical = render_template(
            scenario.template_id,
            parameters,
            model_name=scenario.model_name,
        )
        injected = scenario.injection(canonical.code, scenario.model_name)
        if injected == canonical.code:
            raise RuntimeError(f"scenario {scenario.scenario_id} did not change its source")
        initial = ArtifactBundle(
            requirement=f"offline Stage-2 injection: {scenario.scenario_id}",
            sysml="",
            modelica=injected,
        )

        targeted_pipeline = CertifiedTemplateRepairPipeline(
            template_id=scenario.template_id,
            model_name=scenario.model_name,
            parameters=parameters,
            workspace_root=workspace_root / "targeted" / scenario.scenario_id,
        )
        initial = ArtifactBundle(
            initial.requirement,
            targeted_pipeline.canonical_sysml,
            initial.modelica,
        )
        targeted = targeted_pipeline.run(
            case_id=scenario.scenario_id,
            run_id="targeted",
            initial_artifacts=initial,
            config=DEFAULT_CONFIG,
        )
        if not targeted.repairs:
            raise RuntimeError(
                f"scenario {scenario.scenario_id} did not reach a content repair"
            )
        initial_checks = {
            item["check_type"]: item
            for item in targeted.repairs[0].original_evidence["validation_snapshot"][
                "checks"
            ]
        }
        target = initial_checks.get(scenario.target_check)
        if target is None or target["status"] == "PASS":
            raise RuntimeError(
                f"scenario {scenario.scenario_id} did not fail {scenario.target_check}"
            )

        baseline_pipeline = CertifiedTemplateRepairPipeline(
            template_id=scenario.template_id,
            model_name=scenario.model_name,
            parameters=parameters,
            workspace_root=workspace_root / "unchanged" / scenario.scenario_id,
        )
        unchanged = run_offline_repair_pipeline(
            case_id=scenario.scenario_id,
            run_id="unchanged_retry",
            initial_artifacts=initial,
            parameters=parameters,
            validator=baseline_pipeline.validate,
            repair_provider=lambda request: RepairProposal(
                request.artifact_before,
                rationale="control arm repeats the unchanged artifact",
                metadata={"arm": "unchanged_retry", "online_api_used": False},
            ),
            config=DEFAULT_CONFIG,
            model_configuration=ModelConfiguration(
                provider="deterministic_offline",
                model="unchanged_retry_control",
            ),
            tool_versions={
                "openmodelica": baseline_pipeline.tool.version,
                "sysmlpy": baseline_pipeline.sysml_parser.version,
            },
        )
        cases.append(
            {
                "scenario_id": scenario.scenario_id,
                "template_id": scenario.template_id,
                "target_check": scenario.target_check,
                "initial_bundle_sha256": initial.sha256,
                "initial_modelica_sha256": sha256_text(injected),
                "canonical_modelica_sha256": canonical.rendered_sha256,
                "targeted": targeted.to_dict(),
                "unchanged_retry": unchanged.to_dict(),
            }
        )
        print(
            f"[{scenario.scenario_id}] targeted={targeted.overall_success} "
            f"unchanged={unchanged.overall_success} "
            f"repairs={targeted.counters.content_repair_count}",
            flush=True,
        )

    targeted_successes = sum(item["targeted"]["overall_success"] for item in cases)
    baseline_successes = sum(
        item["unchanged_retry"]["overall_success"] for item in cases
    )
    return {
        "schema_version": "v8-stage2-repair-regression-1",
        "generated_at": utc_now_iso(),
        "environment": {
            "python": platform.python_version(),
            "platform": platform.platform(),
            "online_api_used": False,
        },
        "policy": {
            "same_initial_snapshot": True,
            "same_repair_budget": True,
            "targeted_modelica_policy": "slot_only_certified_rerender",
            "control_policy": "unchanged_artifact_retry",
            "holdout_accessed": False,
        },
        "config": {
            "max_repairs": DEFAULT_CONFIG.max_repairs,
            "max_content_repairs": DEFAULT_CONFIG.max_content_repairs,
            "max_upstream_redos": DEFAULT_CONFIG.max_upstream_redos,
            "max_infrastructure_retries": DEFAULT_CONFIG.max_infrastructure_retries,
        },
        "summary": {
            "scenario_count": len(cases),
            "targeted_successes": targeted_successes,
            "unchanged_retry_successes": baseline_successes,
            "targeted_gain": targeted_successes - baseline_successes,
            "all_targeted_passed": targeted_successes == len(cases),
            "no_false_control_success": baseline_successes == 0,
        },
        "cases": cases,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", default=None, help="optional exact JSON output path")
    args = parser.parse_args(argv)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = (
        Path(args.out).resolve()
        if args.out
        else PROJECT_ROOT
        / "week9"
        / "results"
        / f"repair-regression-{stamp}"
        / "repair-regression.json"
    )
    output.parent.mkdir(parents=True, exist_ok=bool(args.out))
    result = run_regression(workspace_root=output.parent / "runs")
    output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False),
        encoding="utf-8",
    )
    passed = bool(
        result["summary"]["all_targeted_passed"]
        and result["summary"]["no_false_control_success"]
    )
    print(f"Stage-2 repair regression {'PASS' if passed else 'FAIL'}: {output}")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
