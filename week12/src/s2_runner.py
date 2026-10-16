"""V11 S2 single-candidate execution and evidence orchestration.

The runner owns the evidence boundary around a confirmed S1 specification.  It
does not implement a model or decide that a tool completed successfully means
that the vehicle is physically capable.  A model generator and executor are
injected so this module can be tested without OpenModelica, SysML services, or
network access.

The injected interfaces are intentionally small::

    generator.generate(spec, scenario, scenario_dir) -> generated evidence
    executor.execute(scenario_input, generated, scenario_dir) -> dict

Plain callables with the same arguments are accepted as well.  The executor's
mapping may contain ``tool_status``/``status``, an explicit ``physical_status``
or a ``physical_criteria`` list, and optional ``power``/``energy``/
``continuous_capability`` mappings.  Physical status is never inferred from a
tool status.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Any, Callable, Mapping, Protocol

from .specification import assert_generation_ready, load_spec


SCENARIOS = ("ascent", "descent", "flat")
PHYSICAL_STATUSES = {"PASS", "FAIL", "UNVERIFIED", "ERROR"}
TOOL_STATUSES = PHYSICAL_STATUSES
_WEEK12_ROOT = Path(__file__).resolve().parents[1]


class ModelGenerator(Protocol):
    def generate(self, spec: Mapping[str, Any], scenario: str, scenario_dir: Path) -> Any: ...


class ModelExecutor(Protocol):
    def execute(self, scenario_input: Mapping[str, Any], generated: Any, scenario_dir: Path) -> Mapping[str, Any]: ...


class S2RunnerError(ValueError):
    """Raised before execution when the S2 contract cannot be satisfied."""


@dataclass(frozen=True)
class S2Budget:
    """A finite, auditable execution budget; the default is one call per scenario."""

    max_scenarios: int = 3
    max_generator_calls: int = 3
    max_executor_calls: int = 3

    def as_dict(self) -> dict[str, Any]:
        return {
            "max_scenarios": self.max_scenarios,
            "max_generator_calls": self.max_generator_calls,
            "max_executor_calls": self.max_executor_calls,
            "retry_policy": "none",
        }


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str) + "\n").encode("utf-8")


def _write_json(path: Path, value: Any) -> None:
    path.write_bytes(_json_bytes(value))


def _safe_id(value: str) -> str:
    value = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
    return value or "candidate"


def _status(value: Any, default: str = "UNVERIFIED") -> str:
    normalized = str(value or default).upper()
    return normalized if normalized in TOOL_STATUSES else default


def _call_generator(generator: Any, spec: Mapping[str, Any], scenario: str, directory: Path) -> Any:
    method = getattr(generator, "generate", None)
    return method(spec, scenario, directory) if callable(method) else generator(spec, scenario, directory)


def _call_executor(executor: Any, scenario_input: Mapping[str, Any], generated: Any, directory: Path) -> Mapping[str, Any]:
    method = getattr(executor, "execute", None)
    result = method(scenario_input, generated, directory) if callable(method) else executor(scenario_input, generated, directory)
    if not isinstance(result, Mapping):
        raise TypeError("executor result must be a mapping")
    return result


def _physical_status(result: Mapping[str, Any], tool_status: str) -> tuple[str, str]:
    """Resolve explicit physical evidence without promoting a tool PASS."""
    explicit_status = "UNVERIFIED"
    source = "no explicit physical evidence"
    if "physical_status" in result:
        explicit_status = _status(result.get("physical_status"))
        source = "executor.physical_status"
    else:
        physical = result.get("physical")
        if isinstance(physical, Mapping) and "status" in physical:
            explicit_status = _status(physical.get("status"))
            source = "executor.physical.status"
        else:
            criteria = result.get("physical_criteria")
            if isinstance(criteria, list) and criteria:
                statuses = []
                for criterion in criteria:
                    if isinstance(criterion, Mapping):
                        statuses.append(_status(criterion.get("status")))
                    else:
                        statuses.append(_status(criterion))
                source = "executor.physical_criteria"
                if "ERROR" in statuses:
                    explicit_status = "ERROR"
                elif "FAIL" in statuses:
                    explicit_status = "FAIL"
                elif all(item == "PASS" for item in statuses):
                    explicit_status = "PASS"
    if explicit_status == "PASS" and tool_status != "PASS":
        return "UNVERIFIED", source + "; blocked by non-PASS tool status"
    return explicit_status, source


def _serializable_generated(generated: Any) -> Any:
    if isinstance(generated, (str, int, float, bool)) or generated is None:
        return generated
    if isinstance(generated, Mapping):
        return dict(generated)
    return {"repr": repr(generated)}


def _persist_generated(directory: Path, generated: Any) -> str:
    if isinstance(generated, str):
        path = directory / "generated-model.txt"
        path.write_text(generated, encoding="utf-8", newline="\n")
    else:
        path = directory / "generated-model.json"
        _write_json(path, _serializable_generated(generated))
    return path.name


def _hash_files(root: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            result[str(path.relative_to(root)).replace("\\", "/")] = hashlib.sha256(path.read_bytes()).hexdigest()
    return result


def _power_summary(result: Mapping[str, Any]) -> dict[str, Any]:
    keys = ("power", "energy", "continuous_capability", "power_summary", "energy_summary", "power_and_continuous_capability")
    return {key: result[key] for key in keys if key in result}


def _prepare_output(path: Path) -> Path:
    output = path.resolve()
    try:
        output.relative_to(_WEEK12_ROOT.resolve())
    except ValueError as exc:
        raise S2RunnerError("output_dir must be inside week12") from exc
    if output.exists():
        raise FileExistsError("output_dir must not already exist: " + str(output))
    if not output.parent.is_dir():
        raise FileNotFoundError("output_dir parent must already exist: " + str(output.parent))
    output.mkdir()
    return output


class S2Runner:
    """Run one confirmed candidate once through ascent, descent, and flat cases."""

    def __init__(self, generator: ModelGenerator | Callable[..., Any], executor: ModelExecutor | Callable[..., Mapping[str, Any]], *, budget: S2Budget | None = None):
        self.generator = generator
        self.executor = executor
        self.budget = budget or S2Budget()

    def run(self, spec_path: Path, output_dir: Path) -> dict[str, Any]:
        spec_path = Path(spec_path)
        output_dir = Path(output_dir)
        # load_spec performs schema/digest validation; this explicit call keeps
        # the generation gate visible at this orchestration boundary.
        spec = load_spec(spec_path, require_generation_ready=True)
        assert_generation_ready(spec)
        if self.budget.max_scenarios < len(SCENARIOS):
            raise S2RunnerError("S2 budget must permit all three scenarios")
        if self.budget.max_generator_calls < len(SCENARIOS) or self.budget.max_executor_calls < len(SCENARIOS):
            raise S2RunnerError("S2 budget must permit one generator and executor call per scenario")
        candidate = spec.get("candidate", {})
        candidate_id = _safe_id(str(candidate.get("candidate_id", "candidate")))
        design = candidate.get("design")
        if not isinstance(design, Mapping):
            raise S2RunnerError("confirmed specification has no candidate design")
        output_dir = _prepare_output(output_dir)
        scenario_root = output_dir / candidate_id
        scenario_root.mkdir()
        _write_json(scenario_root / "spec.json", spec)
        scenarios: dict[str, dict[str, Any]] = {}
        counts = {"generator_calls": 0, "executor_calls": 0}
        scenario_inputs: dict[str, dict[str, Any]] = {}
        for scenario in SCENARIOS:
            directory = scenario_root / scenario
            directory.mkdir()
            scenario_input = {"schema_version": "v11-s2-scenario-input-1", "candidate_id": candidate_id, "scenario": scenario, "spec_digest": spec["spec_digest"], "design": dict(design), "scenario_definition": spec.get("scenarios", {}).get(scenario, {}), "stair": spec.get("stair"), "assumptions": spec.get("assumptions", {})}
            _write_json(directory / "input.json", scenario_input)
            scenario_inputs[scenario] = scenario_input
        state: dict[str, Any] = {"schema_version": "v11-s2-run-state-1", "status": "PREPARED", "current_scenario": None, "current_operation": None, "interrupted": False, "budget": self.budget.as_dict(), "usage": counts, "retry_policy": "none"}
        state_path = scenario_root / "run-state.json"
        _write_json(state_path, state)
        interrupted = False
        for scenario in SCENARIOS:
            directory = scenario_root / scenario
            scenario_input = scenario_inputs[scenario]
            record: dict[str, Any] = {"scenario": scenario, "status": "UNVERIFIED", "tool_status": "UNVERIFIED", "physical_status": "UNVERIFIED", "attempts": 0}
            try:
                counts["generator_calls"] += 1
                state.update({"status": "RUNNING", "current_scenario": scenario, "current_operation": "generator", "usage": counts})
                _write_json(state_path, state)
                generated = _call_generator(self.generator, spec, scenario, directory)
                record["generated_file"] = _persist_generated(directory, generated)
                counts["executor_calls"] += 1
                record["attempts"] = 1
                state.update({"current_operation": "executor", "usage": counts})
                _write_json(state_path, state)
                tool_result = dict(_call_executor(self.executor, scenario_input, generated, directory))
                record["tool_result"] = tool_result
                record["tool_status"] = _status(tool_result.get("tool_status", tool_result.get("status")))
                record["physical_status"], record["physical_status_source"] = _physical_status(tool_result, record["tool_status"])
                record["status"] = record["physical_status"]
                record["power_continuous"] = _power_summary(tool_result)
                _write_json(directory / "tool-result.json", tool_result)
            except KeyboardInterrupt:
                interrupted = True
                error_result = {"status": "ERROR", "tool_status": "ERROR", "physical_status": "ERROR", "interrupted": True, "issues": ["KeyboardInterrupt: execution interrupted"]}
                record.update({"status": "ERROR", "tool_status": "ERROR", "physical_status": "ERROR", "interrupted": True, "error": error_result["issues"][0], "tool_result": error_result})
                _write_json(directory / "tool-result.json", error_result)
                _write_json(directory / "error.json", {"type": "KeyboardInterrupt", "message": "execution interrupted", "interrupted": True})
            except Exception as exc:  # preserve a failed scenario and continue the finite run
                error_result = {"status": "ERROR", "tool_status": "ERROR", "physical_status": "ERROR", "issues": [f"{type(exc).__name__}: {exc}"]}
                record.update({"status": "ERROR", "tool_status": "ERROR", "physical_status": "ERROR", "error": error_result["issues"][0], "tool_result": error_result})
                _write_json(directory / "tool-result.json", error_result)
                _write_json(directory / "error.json", {"type": type(exc).__name__, "message": str(exc)})
            _write_json(directory / "record.json", record)
            scenarios[scenario] = record
            state.update({"status": "INTERRUPTED" if interrupted else "RUNNING", "current_operation": None, "interrupted": interrupted, "usage": counts})
            _write_json(state_path, state)
            if interrupted:
                break
        if interrupted:
            for scenario in SCENARIOS:
                if scenario in scenarios:
                    continue
                record = {"scenario": scenario, "status": "UNVERIFIED", "tool_status": "UNVERIFIED", "physical_status": "UNVERIFIED", "attempts": 0, "skipped": "run_interrupted"}
                _write_json(scenario_root / scenario / "tool-result.json", {"status": "UNVERIFIED", "physical_status": "UNVERIFIED", "issues": ["not executed because run was interrupted"]})
                _write_json(scenario_root / scenario / "record.json", record)
                scenarios[scenario] = record
        aggregate = {"scenarios": {name: item.get("power_continuous", {}) for name, item in scenarios.items()}, "physical_statuses": {name: item["physical_status"] for name, item in scenarios.items()}, "note": "power and continuous capability are reported evidence; they do not imply physical PASS"}
        result = {"schema_version": "v11-s2-run-1", "status": "INTERRUPTED" if interrupted else "COMPLETE", "interrupted": interrupted, "candidate_id": candidate_id, "spec_digest": spec["spec_digest"], "scenarios": scenarios, "power_continuous_capability": aggregate, "budget": self.budget.as_dict(), "usage": counts, "retry_policy": "none"}
        _write_json(scenario_root / "result.json", result)
        state.update({"status": result["status"], "current_scenario": None, "current_operation": None, "interrupted": interrupted, "usage": counts})
        _write_json(state_path, state)
        manifest = {"schema_version": "v11-s2-manifest-1", "status": result["status"], "interrupted": interrupted, "candidate_id": candidate_id, "spec_digest": spec["spec_digest"], "scenarios": list(SCENARIOS), "budget": self.budget.as_dict(), "usage": counts, "files": _hash_files(scenario_root)}
        _write_json(scenario_root / "manifest.json", manifest)
        return result


def run_s2(spec_path: Path, output_dir: Path, generator: ModelGenerator | Callable[..., Any], executor: ModelExecutor | Callable[..., Mapping[str, Any]], *, budget: S2Budget | None = None) -> dict[str, Any]:
    """Convenience entry point for the S2 runner."""
    return S2Runner(generator, executor, budget=budget).run(spec_path, output_dir)


__all__ = ["ModelExecutor", "ModelGenerator", "S2Budget", "S2Runner", "S2RunnerError", "SCENARIOS", "run_s2"]
