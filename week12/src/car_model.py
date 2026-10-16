"""V11 first car modeling closure.

This module deliberately keeps the first release small and auditable: a
two-dimensional rigid four-wheel car, discrete stair geometry, and explicit
contact support events.  It does not claim a full vehicle dynamics model.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
from typing import Any

try:
    from week9.src.modelica_tool import resolve_omc_path
except Exception:  # pragma: no cover - old dependency may be unavailable
    resolve_omc_path = None
try:
    from week9.src.hard_gate import SysMLParserAdapter
except Exception:  # pragma: no cover
    SysMLParserAdapter = None


@dataclass(frozen=True)
class Stair:
    riser_m: float = 0.16
    tread_m: float = 0.30
    count: int = 6
    width_m: float = 1.0


@dataclass(frozen=True)
class Car:
    candidate_id: str = "v11-4w-01"
    wheel_count: int = 4
    wheel_radius_m: float = 0.13
    wheelbase_m: float = 0.42
    track_m: float = 0.34
    mass_kg: float = 18.0
    drive_torque_nm: float = 12.0
    motor_continuous_torque_nm: float = 4.0
    gear_efficiency: float = 0.88
    tire_mu: float = 0.62
    battery_wh: float = 240.0
    cruise_speed_mps: float = 0.6
    rolling_coefficient: float = 0.025


def public_input() -> dict[str, Any]:
    return {
        "stair": asdict(Stair()),
        "constraints": {"allowed_wheel_counts": [4, 6], "max_width_m": 0.80},
        "assumptions": [
            {"name": "payload_mass_kg", "value": 2.0, "source": "AI assumption", "status": "needs_user_confirmation"},
            {"name": "gravity_mps2", "value": 9.81, "source": "physical constant", "status": "assumption"},
            {"name": "contact_model", "value": "2D rigid wheels with unilateral support and Coulomb friction", "source": "AI design", "status": "needs_user_confirmation"},
            {"name": "flat_ground", "value": "level, constant speed, no wind", "source": "AI assumption", "status": "needs_user_confirmation"},
        ],
    }


def _check_car(car: Car, stair: Stair) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    clearance = 2 * car.wheel_radius_m - stair.riser_m
    checks.append({"id": "geometry_riser_clearance", "value_m": clearance, "limit_m": 0.0, "passed": clearance >= 0, "formula": "2*wheel_radius-riser"})
    width_ok = car.track_m + 2 * car.wheel_radius_m <= stair.width_m
    checks.append({"id": "width", "value_m": car.track_m + 2 * car.wheel_radius_m, "limit_m": stair.width_m, "passed": width_ok, "formula": "track+2*radius <= stair_width"})
    required = car.mass_kg * 9.81 * stair.riser_m / max(car.wheelbase_m, 1e-9)
    available = car.drive_torque_nm * car.gear_efficiency / max(car.wheel_radius_m, 1e-9)
    checks.append({"id": "drive_force", "required_n": required, "available_n": available, "passed": available >= required, "formula": "m*g*riser/wheelbase <= drive_torque*eff/radius"})
    normal = car.mass_kg * 9.81
    friction = car.tire_mu * normal
    checks.append({"id": "friction", "required_n": required, "available_n": friction, "passed": friction >= required, "formula": "mu*m*g >= required_force"})
    stable = car.wheelbase_m - stair.riser_m
    checks.append({"id": "longitudinal_stability", "margin_m": stable, "passed": stable > 0, "formula": "wheelbase-riser > 0"})
    return checks


def build_sysml(car: Car, stair: Stair) -> str:
    safe = car.candidate_id.replace("-", "_")
    return f'''package V11Car_{safe} {{
  requirement def StairTask {{ attribute riser_m : Real; attribute tread_m : Real; attribute count : Integer; }}
  part def FourWheelCar {{ attribute wheelRadius_m : Real; attribute wheelbase_m : Real; attribute mass_kg : Real; attribute driveTorque_Nm : Real; }}
  part def System {{
    attribute riser_m : Real = {stair.riser_m};
    attribute tread_m : Real = {stair.tread_m};
    attribute step_count : Integer = {stair.count};
    attribute wheelRadius_m : Real = {car.wheel_radius_m};
    attribute wheelbase_m : Real = {car.wheelbase_m};
    attribute mass_kg : Real = {car.mass_kg};
    attribute driveTorque_Nm : Real = {car.drive_torque_nm};
  }}
  part car : FourWheelCar;
  satisfy car by StairTask;
}}\n'''


def build_modelica(car: Car, stair: Stair) -> str:
    return f'''model {car.candidate_id.replace("-", "_")}_StairCruise
  parameter Integer nSteps={stair.count};
  parameter Real riser={stair.riser_m};
  parameter Real tread={stair.tread_m};
  parameter Real wheelRadius={car.wheel_radius_m};
  parameter Real wheelbase={car.wheelbase_m};
  parameter Real mass={car.mass_kg};
  parameter Real driveTorque={car.drive_torque_nm};
  parameter Real mu={car.tire_mu};
  parameter Real g=9.81;
  Real x(start=0), v(start=0), z(start={car.wheel_radius_m});
  Real vz(start=0), theta(start=0), omega(start=0);
  Boolean frontContact, rearContact;
  Integer stepIndex;
equation
  // Discrete stairs: each tread has constant height; contact/support changes at edges.
  stepIndex = integer(floor(max(x,0)/tread));
  frontContact = z <= wheelRadius + stepIndex*riser + 1e-4;
  rearContact = z <= wheelRadius + max(stepIndex-1,0)*riser + 1e-4;
  der(x)=v;
  der(v)=if frontContact or rearContact then (driveTorque/wheelRadius - mass*g*sin(atan(riser/max(tread,1e-6))) - 0.025*mass*g)/mass else -0.1*v;
  der(z)=vz;
  der(vz)=if frontContact or rearContact then 0 else -g;
  der(theta)=omega;
  der(omega)=0;
  when frontContact <> pre(frontContact) or rearContact <> pre(rearContact) then
    assert(v >= -0.01, "slip or slide event");
  end when;
  annotation(experiment(StartTime=0, StopTime=20, Interval=0.01));
end {car.candidate_id.replace("-", "_")}_StairCruise;
'''


def run_openmodelica(model_file: Path, model_name: str, output_dir: Path) -> dict[str, Any]:
    omc_path = resolve_omc_path() if resolve_omc_path is not None else None
    omc = str(omc_path) if omc_path else shutil.which("omc")
    if not omc:
        return {"status": "blocked", "reason": "OpenModelica omc not found on PATH", "command": None}
    mos = output_dir / "run.mos"
    csv = output_dir / "simulation_res_res.csv"
    csv_prefix = "simulation_res"
    mos.write_text(
        f'loadModel(Modelica);\nloadFile("{model_file.as_posix()}");\n'
        f'checkModel({model_name});\n'
        f'simulate({model_name}, startTime=0, stopTime=20, numberOfIntervals=200, outputFormat="csv", fileNamePrefix="{csv_prefix}");\n'
        'getErrorString();\n', encoding="utf-8")
    result = subprocess.run([omc, str(mos)], cwd=output_dir, capture_output=True, text=True, timeout=120)
    ok = result.returncode == 0 and "Check of" in result.stdout and "LOG_SUCCESS" in result.stdout and csv.exists()
    return {"status": "passed" if ok else "failed", "returncode": result.returncode, "stdout_tail": result.stdout[-4000:], "stderr_tail": result.stderr[-4000:], "command": [omc, str(mos)], "csv_exists": csv.exists(), "tool_reference": "OpenModelica + Modelica Standard Library; exact version recorded in stdout"}


def create_candidate(root: Path, car: Car | None = None, stair: Stair | None = None) -> dict[str, Any]:
    raise RuntimeError("Withdrawn draft: slope equations and static checks cannot establish stair feasibility. Use the new confirmed development pipeline; see results/correction-notice.json.")
    car, stair = car or Car(), stair or Stair()
    out = root / "results" / car.candidate_id
    out.mkdir(parents=True, exist_ok=True)
    checks = _check_car(car, stair)
    spec = {"schema_version": "v11-car-1", "candidate": asdict(car), "stair": asdict(stair), "input": public_input(), "checks": checks, "status": "ready_for_user_confirmation"}
    (out / "spec.json").write_text(json.dumps(spec, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    (out / "model.sysml").write_text(build_sysml(car, stair), encoding="utf-8")
    (out / "model.mo").write_text(build_modelica(car, stair), encoding="utf-8")
    sysml_text = (out / "model.sysml").read_text(encoding="utf-8")
    sysml_ok = False
    sysml_detail: dict[str, Any] = {"status": "blocked", "reason": "sysmlpy unavailable"}
    if SysMLParserAdapter is not None:
        try:
            quality, structure = SysMLParserAdapter().inspect(sysml_text)
            status_value = getattr(getattr(quality, "status", None), "value", getattr(quality, "status", ""))
            sysml_ok = status_value in ("PASS", "pass", "passed")
            sysml_detail = {"status": "passed" if sysml_ok else "failed", "message": getattr(quality, "message", ""), "tool": "sysmlpy ANTLR parser"}
        except Exception as exc:
            sysml_detail = {"status": "failed", "reason": type(exc).__name__}
    modelica_ok = all(token in (out / "model.mo").read_text(encoding="utf-8") for token in ("stepIndex", "frontContact", "rearContact", "when"))
    sim = run_openmodelica(out / "model.mo", car.candidate_id.replace("-", "_")+"_StairCruise", out)
    cruise_power = car.mass_kg * 9.81 * car.rolling_coefficient * car.cruise_speed_mps / max(car.gear_efficiency, 1e-9)
    cruise_hours = car.battery_wh * 0.8 / max(cruise_power, 1e-9)
    climb_status = sim.get("status")
    if not sysml_ok:
        climb_verdict = "blocked"
    elif climb_status == "passed":
        climb_verdict = "pass" if all(x["passed"] for x in checks) else "fail"
    elif climb_status == "blocked":
        climb_verdict = "blocked"
    else:
        climb_verdict = "fail"
    result = {"schema_version": "v11-car-result-1", "candidate_id": car.candidate_id, "references": {"sysml": "SysML v2 grammar via sysmlpy ANTLR adapter", "modelica": "Modelica Language + Modelica Standard Library", "compiler": "OpenModelica (version recorded only when executable runs)"}, "sysml_check": sysml_detail, "modelica_static_check": "passed" if modelica_ok else "failed", "modelica_compile_simulation": sim, "stair_checks": checks, "climb_verdict": climb_verdict, "cruise": {"power_w": cruise_power, "usable_energy_wh": car.battery_wh*0.8, "estimated_hours": cruise_hours, "method": "steady-state estimate; not a full discharge simulation"}, "limitations": ["2D longitudinal model; lateral stability and steering are unverified", "OpenModelica execution is required before claiming dynamic climb evidence"]}
    (out / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    files = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(out.iterdir()) if p.is_file() and p.name != "manifest.json"}
    (out / "manifest.json").write_text(json.dumps({"schema_version":"v11-manifest-1","files":files}, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    return result


__all__ = ["Car", "Stair", "create_candidate", "public_input"]
