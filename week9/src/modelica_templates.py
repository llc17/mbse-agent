"""Strict, certification-ready Modelica templates for V8.

Unlike the V6 template helper, this module never supplies silent defaults and
never hands a certified skeleton to an unconstrained full-file rewriter.  A
render is reproducible from its template id, model name, and normalized
parameters; :func:`verify_rendered_template` detects any later full-file edit.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re
from string import Template
from types import MappingProxyType
from typing import Iterable, Mapping

from week9.src.schemas import NormalizedParameter, ParameterStatus, sha256_text


_MODELICA_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class TemplateInputError(ValueError):
    """Raised when a certified template cannot be filled without guessing."""

    def __init__(self, issues: Iterable[str]):
        self.issues = tuple(issues)
        super().__init__("; ".join(self.issues))


@dataclass(frozen=True, slots=True)
class ParameterSpec:
    name: str
    unit: str
    minimum: float | None = None
    maximum: float | None = None
    minimum_inclusive: bool = False
    maximum_inclusive: bool = False


@dataclass(frozen=True, slots=True)
class RepresentativeValue:
    name: str
    value: float
    unit: str


@dataclass(frozen=True, slots=True)
class SimulationPlan:
    start_time: float
    stop_time: float
    intervals: int


@dataclass(frozen=True, slots=True)
class TemplateSpec:
    template_id: str
    domain: str
    description: str
    stimulus_type: str
    source: str
    parameters: tuple[ParameterSpec, ...]
    representative_values: tuple[RepresentativeValue, ...]
    output_variables: tuple[str, ...]
    sysml_components: tuple[str, ...]
    sysml_connections: tuple[tuple[str, str], ...]
    simulation_stop_time: float
    simulation_intervals: int
    relation: str = ""

    @property
    def source_sha256(self) -> str:
        return sha256_text(self.source)

    @property
    def parameter_names(self) -> tuple[str, ...]:
        return tuple(item.name for item in self.parameters)


@dataclass(frozen=True, slots=True)
class CanonicalParameterValue:
    name: str
    value: float
    unit: str
    source_text: str


@dataclass(frozen=True, slots=True)
class RenderedTemplate:
    template_id: str
    model_name: str
    code: str
    template_sha256: str
    rendered_sha256: str
    parameters: tuple[CanonicalParameterValue, ...]

    @property
    def parameter_map(self) -> dict[str, float]:
        return {item.name: item.value for item in self.parameters}


_RC_FILTER = """model $MODEL_NAME
  // V8-CERTIFIED-TEMPLATE:rc_filter
  final parameter Modelica.Units.SI.Resistance resistance = $resistance;
  final parameter Modelica.Units.SI.Capacitance capacitance = $capacitance;
  final parameter Modelica.Units.SI.Frequency cutoff_frequency = $cutoff_frequency;
  final parameter Modelica.Units.SI.Voltage input_voltage = $input_voltage;

  Modelica.Electrical.Analog.Basic.Resistor resistor(R=resistance);
  Modelica.Electrical.Analog.Basic.Capacitor capacitor(
    C=capacitance,
    v(start=0, fixed=true));
  Modelica.Electrical.Analog.Basic.Ground ground;
  Modelica.Electrical.Analog.Sources.StepVoltage source(
    V=input_voltage,
    startTime=0);
  output Modelica.Units.SI.Voltage output_voltage;
equation
  connect(source.p, resistor.p);
  connect(resistor.n, capacitor.p);
  connect(capacitor.n, ground.p);
  connect(source.n, ground.p);
  output_voltage = capacitor.v;
end $MODEL_NAME;
"""


_RLC_FILTER = """model $MODEL_NAME
  // V8-CERTIFIED-TEMPLATE:rlc_filter
  final parameter Modelica.Units.SI.Resistance resistance = $resistance;
  final parameter Modelica.Units.SI.Inductance inductance = $inductance;
  final parameter Modelica.Units.SI.Capacitance capacitance = $capacitance;
  final parameter Modelica.Units.SI.Frequency cutoff_frequency = $cutoff_frequency;
  final parameter Modelica.Units.SI.Voltage input_voltage = $input_voltage;

  Modelica.Electrical.Analog.Basic.Resistor resistor(R=resistance);
  Modelica.Electrical.Analog.Basic.Inductor inductor(
    L=inductance,
    i(start=0, fixed=true));
  Modelica.Electrical.Analog.Basic.Capacitor capacitor(
    C=capacitance,
    v(start=0, fixed=true));
  Modelica.Electrical.Analog.Basic.Ground ground;
  Modelica.Electrical.Analog.Sources.SineVoltage source(
    V=input_voltage,
    f=cutoff_frequency,
    phase=0,
    offset=0,
    startTime=0);
  output Modelica.Units.SI.Voltage input_signal;
  output Modelica.Units.SI.Voltage output_voltage;
equation
  connect(source.p, resistor.p);
  connect(resistor.n, inductor.p);
  connect(inductor.n, capacitor.p);
  connect(capacitor.n, ground.p);
  connect(source.n, ground.p);
  input_signal = source.v;
  output_voltage = capacitor.v;
end $MODEL_NAME;
"""


_OPAMP_INVERTING = """model $MODEL_NAME
  // V8-CERTIFIED-TEMPLATE:opamp_inverting
  final parameter Modelica.Units.SI.Voltage input_voltage = $input_voltage;
  final parameter Modelica.Units.SI.Resistance input_resistance = $input_resistance;
  final parameter Modelica.Units.SI.Resistance feedback_resistance = $feedback_resistance;
  final parameter Modelica.Units.SI.Voltage positive_supply = $positive_supply;
  final parameter Modelica.Units.SI.Voltage negative_supply = $negative_supply;

  Modelica.Electrical.Analog.Basic.Ground ground;
  Modelica.Electrical.Analog.Sources.StepVoltage source(
    V=input_voltage,
    startTime=0);
  Modelica.Electrical.Analog.Ideal.IdealizedOpAmpLimited opAmp(
    V0=100000,
    useSupply=false,
    Vps=positive_supply,
    Vns=negative_supply);
  Modelica.Electrical.Analog.Basic.Resistor inputResistor(R=input_resistance);
  Modelica.Electrical.Analog.Basic.Resistor feedbackResistor(R=feedback_resistance);
  Modelica.Electrical.Analog.Sensors.VoltageSensor outputSensor;
  output Modelica.Units.SI.Voltage output_voltage;
equation
  connect(source.p, inputResistor.p);
  connect(source.n, ground.p);
  connect(inputResistor.n, opAmp.in_n);
  connect(feedbackResistor.p, opAmp.in_n);
  connect(feedbackResistor.n, opAmp.out);
  connect(opAmp.in_p, ground.p);
  connect(opAmp.out, outputSensor.p);
  connect(outputSensor.n, ground.p);
  output_voltage = outputSensor.v;
end $MODEL_NAME;
"""


_THERMAL_SINGLE_ROOM = """model $MODEL_NAME
  // V8-CERTIFIED-TEMPLATE:thermal_single_room
  final parameter Modelica.Units.SI.HeatCapacity heat_capacity = $heat_capacity;
  final parameter Modelica.Units.SI.ThermalResistance thermal_resistance = $thermal_resistance;
  final parameter Modelica.Units.SI.Temperature initial_temperature = $initial_temperature;
  final parameter Modelica.Units.SI.Temperature outdoor_temperature = $outdoor_temperature;

  Modelica.Thermal.HeatTransfer.Components.HeatCapacitor room(
    C=heat_capacity,
    T(start=initial_temperature, fixed=true));
  Modelica.Thermal.HeatTransfer.Components.ThermalResistor wall(
    R=thermal_resistance);
  Modelica.Thermal.HeatTransfer.Sources.FixedTemperature outdoor(
    T=outdoor_temperature);
  Modelica.Thermal.HeatTransfer.Sensors.TemperatureSensor roomSensor;
  output Modelica.Units.SI.Temperature room_temperature;
equation
  connect(outdoor.port, wall.port_a);
  connect(wall.port_b, room.port);
  connect(room.port, roomSensor.port);
  room_temperature = roomSensor.T;
end $MODEL_NAME;
"""


_THERMAL_DUAL_ROOM = """model $MODEL_NAME
  // V8-CERTIFIED-TEMPLATE:thermal_dual_room
  final parameter Modelica.Units.SI.HeatCapacity heat_capacity_a = $heat_capacity_a;
  final parameter Modelica.Units.SI.HeatCapacity heat_capacity_b = $heat_capacity_b;
  final parameter Modelica.Units.SI.ThermalResistance shared_resistance = $shared_resistance;
  final parameter Modelica.Units.SI.ThermalResistance outdoor_resistance_a = $outdoor_resistance_a;
  final parameter Modelica.Units.SI.ThermalResistance outdoor_resistance_b = $outdoor_resistance_b;
  final parameter Modelica.Units.SI.Temperature initial_temperature_a = $initial_temperature_a;
  final parameter Modelica.Units.SI.Temperature initial_temperature_b = $initial_temperature_b;
  final parameter Modelica.Units.SI.Temperature outdoor_temperature = $outdoor_temperature;

  Modelica.Thermal.HeatTransfer.Components.HeatCapacitor roomA(
    C=heat_capacity_a,
    T(start=initial_temperature_a, fixed=true));
  Modelica.Thermal.HeatTransfer.Components.HeatCapacitor roomB(
    C=heat_capacity_b,
    T(start=initial_temperature_b, fixed=true));
  Modelica.Thermal.HeatTransfer.Components.ThermalResistor sharedWall(
    R=shared_resistance);
  Modelica.Thermal.HeatTransfer.Components.ThermalResistor outdoorWallA(
    R=outdoor_resistance_a);
  Modelica.Thermal.HeatTransfer.Components.ThermalResistor outdoorWallB(
    R=outdoor_resistance_b);
  Modelica.Thermal.HeatTransfer.Sources.FixedTemperature outdoor(
    T=outdoor_temperature);
  Modelica.Thermal.HeatTransfer.Sensors.TemperatureSensor roomASensor;
  Modelica.Thermal.HeatTransfer.Sensors.TemperatureSensor roomBSensor;
  output Modelica.Units.SI.Temperature room_a_temperature;
  output Modelica.Units.SI.Temperature room_b_temperature;
equation
  connect(outdoor.port, outdoorWallA.port_a);
  connect(outdoorWallA.port_b, roomA.port);
  connect(roomA.port, sharedWall.port_a);
  connect(sharedWall.port_b, roomB.port);
  connect(roomB.port, outdoorWallB.port_a);
  connect(outdoorWallB.port_b, outdoor.port);
  connect(roomA.port, roomASensor.port);
  connect(roomB.port, roomBSensor.port);
  room_a_temperature = roomASensor.T;
  room_b_temperature = roomBSensor.T;
end $MODEL_NAME;
"""


def _positive(name: str, unit: str) -> ParameterSpec:
    return ParameterSpec(name=name, unit=unit, minimum=0.0)


_TEMPLATE_REGISTRY: dict[str, TemplateSpec] = {
    "rc_filter": TemplateSpec(
        template_id="rc_filter",
        domain="electrical",
        description="Series RC low-pass filter with a step-voltage stimulus",
        stimulus_type="step_voltage",
        source=_RC_FILTER,
        parameters=(
            _positive("resistance", "Ohm"),
            _positive("capacitance", "F"),
            _positive("cutoff_frequency", "Hz"),
            _positive("input_voltage", "V"),
        ),
        representative_values=(
            RepresentativeValue("resistance", 1000.0, "Ohm"),
            RepresentativeValue("capacitance", 1.0 / (2.0 * math.pi * 1000.0 * 1000.0), "F"),
            RepresentativeValue("cutoff_frequency", 1000.0, "Hz"),
            RepresentativeValue("input_voltage", 5.0, "V"),
        ),
        output_variables=("time", "output_voltage"),
        sysml_components=("source", "resistor", "capacitor", "ground"),
        sysml_connections=(
            ("source", "resistor"),
            ("resistor", "capacitor"),
            ("capacitor", "ground"),
            ("source", "ground"),
        ),
        simulation_stop_time=0.002,
        simulation_intervals=2000,
        relation="rc_cutoff",
    ),
    "rlc_filter": TemplateSpec(
        template_id="rlc_filter",
        domain="electrical",
        description="Series RLC low-pass filter with a sine-voltage stimulus",
        stimulus_type="sine_voltage",
        source=_RLC_FILTER,
        parameters=(
            _positive("resistance", "Ohm"),
            _positive("inductance", "H"),
            _positive("capacitance", "F"),
            _positive("cutoff_frequency", "Hz"),
            _positive("input_voltage", "V"),
        ),
        representative_values=(
            RepresentativeValue("resistance", 100.0, "Ohm"),
            RepresentativeValue("inductance", 0.01, "H"),
            RepresentativeValue("capacitance", 1.0 / ((2.0 * math.pi * 10000.0) ** 2 * 0.01), "F"),
            RepresentativeValue("cutoff_frequency", 10000.0, "Hz"),
            RepresentativeValue("input_voltage", 5.0, "V"),
        ),
        output_variables=("time", "input_signal", "output_voltage"),
        sysml_components=("source", "resistor", "inductor", "capacitor", "ground"),
        sysml_connections=(
            ("source", "resistor"),
            ("resistor", "inductor"),
            ("inductor", "capacitor"),
            ("capacitor", "ground"),
            ("source", "ground"),
        ),
        simulation_stop_time=0.003,
        simulation_intervals=3000,
        relation="rlc_resonance",
    ),
    "opamp_inverting": TemplateSpec(
        template_id="opamp_inverting",
        domain="electrical",
        description="Inverting amplifier using the three-pin limited idealized op-amp",
        stimulus_type="step_voltage",
        source=_OPAMP_INVERTING,
        parameters=(
            ParameterSpec("input_voltage", "V"),
            _positive("input_resistance", "Ohm"),
            _positive("feedback_resistance", "Ohm"),
            _positive("positive_supply", "V"),
            ParameterSpec("negative_supply", "V", maximum=0.0),
        ),
        representative_values=(
            RepresentativeValue("input_voltage", 0.5, "V"),
            RepresentativeValue("input_resistance", 1000.0, "Ohm"),
            RepresentativeValue("feedback_resistance", 10000.0, "Ohm"),
            RepresentativeValue("positive_supply", 15.0, "V"),
            RepresentativeValue("negative_supply", -15.0, "V"),
        ),
        output_variables=("time", "output_voltage"),
        sysml_components=(
            "source",
            "inputResistor",
            "feedbackResistor",
            "opAmp",
            "outputSensor",
            "ground",
        ),
        sysml_connections=(
            ("source", "inputResistor"),
            ("source", "ground"),
            ("inputResistor", "opAmp"),
            ("feedbackResistor", "opAmp"),
            ("opAmp", "ground"),
            ("opAmp", "outputSensor"),
            ("outputSensor", "ground"),
        ),
        simulation_stop_time=0.01,
        simulation_intervals=200,
        relation="opamp_supplies",
    ),
    "thermal_single_room": TemplateSpec(
        template_id="thermal_single_room",
        domain="thermal",
        description="Single thermal capacitance connected to a fixed-temperature boundary",
        stimulus_type="fixed_temperature_boundary",
        source=_THERMAL_SINGLE_ROOM,
        parameters=(
            _positive("heat_capacity", "J/K"),
            _positive("thermal_resistance", "K/W"),
            _positive("initial_temperature", "K"),
            _positive("outdoor_temperature", "K"),
        ),
        representative_values=(
            RepresentativeValue("heat_capacity", 500000.0, "J/K"),
            RepresentativeValue("thermal_resistance", 0.02, "K/W"),
            RepresentativeValue("initial_temperature", 293.15, "K"),
            RepresentativeValue("outdoor_temperature", 308.15, "K"),
        ),
        output_variables=("time", "room_temperature"),
        sysml_components=("outdoor", "wall", "room", "roomSensor"),
        sysml_connections=(
            ("outdoor", "wall"),
            ("wall", "room"),
            ("room", "roomSensor"),
        ),
        simulation_stop_time=100000.0,
        simulation_intervals=1000,
        relation="",
    ),
    "thermal_dual_room": TemplateSpec(
        template_id="thermal_dual_room",
        domain="thermal",
        description="Two thermal capacitances coupled to each other and one outdoor boundary",
        stimulus_type="fixed_temperature_boundary",
        source=_THERMAL_DUAL_ROOM,
        parameters=(
            _positive("heat_capacity_a", "J/K"),
            _positive("heat_capacity_b", "J/K"),
            _positive("shared_resistance", "K/W"),
            _positive("outdoor_resistance_a", "K/W"),
            _positive("outdoor_resistance_b", "K/W"),
            _positive("initial_temperature_a", "K"),
            _positive("initial_temperature_b", "K"),
            _positive("outdoor_temperature", "K"),
        ),
        representative_values=(
            RepresentativeValue("heat_capacity_a", 300000.0, "J/K"),
            RepresentativeValue("heat_capacity_b", 200000.0, "J/K"),
            RepresentativeValue("shared_resistance", 0.05, "K/W"),
            RepresentativeValue("outdoor_resistance_a", 0.03, "K/W"),
            RepresentativeValue("outdoor_resistance_b", 0.04, "K/W"),
            RepresentativeValue("initial_temperature_a", 293.15, "K"),
            RepresentativeValue("initial_temperature_b", 293.15, "K"),
            RepresentativeValue("outdoor_temperature", 308.15, "K"),
        ),
        output_variables=("time", "room_a_temperature", "room_b_temperature"),
        sysml_components=(
            "outdoor",
            "outdoorWallA",
            "outdoorWallB",
            "sharedWall",
            "roomA",
            "roomB",
            "roomASensor",
            "roomBSensor",
        ),
        sysml_connections=(
            ("outdoor", "outdoorWallA"),
            ("outdoorWallA", "roomA"),
            ("roomA", "sharedWall"),
            ("sharedWall", "roomB"),
            ("roomB", "outdoorWallB"),
            ("outdoorWallB", "outdoor"),
            ("roomA", "roomASensor"),
            ("roomB", "roomBSensor"),
        ),
        simulation_stop_time=100000.0,
        simulation_intervals=1000,
        relation="",
    ),
}

TEMPLATE_REGISTRY: Mapping[str, TemplateSpec] = MappingProxyType(_TEMPLATE_REGISTRY)


# source unit -> (canonical unit, multiplicative scale, additive offset)
_UNIT_CONVERSIONS: Mapping[str, tuple[str, float, float]] = {
    "Ohm": ("Ohm", 1.0, 0.0),
    "ohm": ("Ohm", 1.0, 0.0),
    "Ω": ("Ohm", 1.0, 0.0),
    "kOhm": ("Ohm", 1000.0, 0.0),
    "kohm": ("Ohm", 1000.0, 0.0),
    "kΩ": ("Ohm", 1000.0, 0.0),
    "MOhm": ("Ohm", 1e6, 0.0),
    "MΩ": ("Ohm", 1e6, 0.0),
    "F": ("F", 1.0, 0.0),
    "mF": ("F", 1e-3, 0.0),
    "uF": ("F", 1e-6, 0.0),
    "µF": ("F", 1e-6, 0.0),
    "μF": ("F", 1e-6, 0.0),
    "nF": ("F", 1e-9, 0.0),
    "pF": ("F", 1e-12, 0.0),
    "H": ("H", 1.0, 0.0),
    "mH": ("H", 1e-3, 0.0),
    "uH": ("H", 1e-6, 0.0),
    "V": ("V", 1.0, 0.0),
    "mV": ("V", 1e-3, 0.0),
    "Hz": ("Hz", 1.0, 0.0),
    "kHz": ("Hz", 1000.0, 0.0),
    "MHz": ("Hz", 1e6, 0.0),
    "K": ("K", 1.0, 0.0),
    "degC": ("K", 1.0, 273.15),
    "°C": ("K", 1.0, 273.15),
    "J/K": ("J/K", 1.0, 0.0),
    "kJ/K": ("J/K", 1000.0, 0.0),
    "MJ/K": ("J/K", 1e6, 0.0),
    "K/W": ("K/W", 1.0, 0.0),
}


def get_template_spec(template_id: str) -> TemplateSpec:
    try:
        return TEMPLATE_REGISTRY[template_id]
    except KeyError as exc:
        raise TemplateInputError((f"unknown template: {template_id}",)) from exc


def representative_parameters(template_id: str) -> list[NormalizedParameter]:
    """Return fresh, unit-bearing parameters used for template certification."""

    spec = get_template_spec(template_id)
    return [
        NormalizedParameter(
            canonical_name=item.name,
            value=item.value,
            unit=item.unit,
            source_text=f"V8 representative matrix for {template_id}",
            status=ParameterStatus.KNOWN,
        )
        for item in spec.representative_values
    ]


def canonicalize_parameters(
    template_id: str,
    parameters: Iterable[NormalizedParameter],
) -> tuple[CanonicalParameterValue, ...]:
    """Validate completeness, status, units, ranges, and template relations."""

    spec = get_template_spec(template_id)
    indexed: dict[str, NormalizedParameter] = {}
    issues: list[str] = []
    for parameter in parameters:
        name = parameter.canonical_name
        if name in indexed:
            issues.append(f"duplicate parameter: {name}")
        else:
            indexed[name] = parameter

    expected = set(spec.parameter_names)
    received = set(indexed)
    for name in sorted(expected - received):
        issues.append(f"missing parameter: {name}")
    for name in sorted(received - expected):
        issues.append(f"unknown template parameter: {name}")

    values: list[CanonicalParameterValue] = []
    for parameter_spec in spec.parameters:
        parameter = indexed.get(parameter_spec.name)
        if parameter is None:
            continue
        if parameter.status is not ParameterStatus.KNOWN:
            issues.append(
                f"parameter {parameter_spec.name} has non-known status: {parameter.status.value}"
            )
            continue
        if isinstance(parameter.value, bool) or not isinstance(parameter.value, (int, float)):
            issues.append(f"parameter {parameter_spec.name} must be a real number")
            continue
        numeric = float(parameter.value)
        if not math.isfinite(numeric):
            issues.append(f"parameter {parameter_spec.name} must be finite")
            continue
        try:
            canonical = convert_unit(numeric, parameter.unit, parameter_spec.unit)
        except ValueError as exc:
            issues.append(f"parameter {parameter_spec.name}: {exc}")
            continue
        if not _inside_bounds(canonical, parameter_spec):
            issues.append(
                f"parameter {parameter_spec.name}={canonical:g} {parameter_spec.unit} is outside its allowed range"
            )
            continue
        values.append(
            CanonicalParameterValue(
                name=parameter_spec.name,
                value=canonical,
                unit=parameter_spec.unit,
                source_text=parameter.source_text,
            )
        )

    if not issues and len(values) == len(spec.parameters):
        issues.extend(_relation_issues(spec, {item.name: item.value for item in values}))
    if issues:
        raise TemplateInputError(issues)
    return tuple(values)


def render_template(
    template_id: str,
    parameters: Iterable[NormalizedParameter],
    *,
    model_name: str,
) -> RenderedTemplate:
    """Fill only declared numeric slots in an immutable certified skeleton."""

    if not _MODELICA_IDENTIFIER.fullmatch(model_name):
        raise TemplateInputError((f"invalid Modelica model name: {model_name!r}",))
    spec = get_template_spec(template_id)
    canonical = canonicalize_parameters(template_id, parameters)
    substitutions = {item.name: _modelica_real(item.value) for item in canonical}
    substitutions["MODEL_NAME"] = model_name
    try:
        code = Template(spec.source).substitute(substitutions)
    except (KeyError, ValueError) as exc:
        raise RuntimeError(f"invalid built-in template {template_id}: {exc}") from exc
    return RenderedTemplate(
        template_id=template_id,
        model_name=model_name,
        code=code,
        template_sha256=spec.source_sha256,
        rendered_sha256=sha256_text(code),
        parameters=canonical,
    )


def verify_rendered_template(rendered: RenderedTemplate) -> tuple[bool, tuple[str, ...]]:
    """Re-render and compare byte-for-byte to detect unrestricted rewriting."""

    issues: list[str] = []
    try:
        spec = get_template_spec(rendered.template_id)
    except TemplateInputError as exc:
        return False, exc.issues
    if rendered.template_sha256 != spec.source_sha256:
        issues.append("template source hash does not match the current certified skeleton")
    if rendered.rendered_sha256 != sha256_text(rendered.code):
        issues.append("rendered code hash does not match its content")
    reconstructed = [
        NormalizedParameter(
            canonical_name=item.name,
            value=item.value,
            unit=item.unit,
            source_text=item.source_text,
            status=ParameterStatus.KNOWN,
        )
        for item in rendered.parameters
    ]
    try:
        expected = render_template(
            rendered.template_id,
            reconstructed,
            model_name=rendered.model_name,
        )
    except TemplateInputError as exc:
        issues.extend(exc.issues)
    else:
        if expected.code != rendered.code:
            issues.append("rendered code differs from the slot-only certified render")
        if expected.rendered_sha256 != rendered.rendered_sha256:
            issues.append("rendered hash differs from the reproducible certified render")
    return not issues, tuple(issues)


def convert_unit(value: float, source_unit: str, target_unit: str) -> float:
    """Convert a deliberately small, audited set of units to a template unit."""

    conversion = _UNIT_CONVERSIONS.get(source_unit.strip())
    if conversion is None:
        raise ValueError(f"unsupported or missing unit {source_unit!r}")
    canonical_unit, scale, offset = conversion
    if canonical_unit != target_unit:
        raise ValueError(
            f"unit {source_unit!r} is incompatible with required unit {target_unit!r}"
        )
    result = value * scale + offset
    if not math.isfinite(result):
        raise ValueError("unit conversion produced a non-finite value")
    return result


def relation_issues(template_id: str, values: Mapping[str, float]) -> tuple[str, ...]:
    """Expose deterministic parameter-relation checks to the quality gate."""

    return tuple(_relation_issues(get_template_spec(template_id), values))


def simulation_plan(
    template_id: str, values: Mapping[str, float]
) -> SimulationPlan:
    """Derive a sufficient simulation window from the rendered SI parameters."""

    spec = get_template_spec(template_id)
    stop_time = spec.simulation_stop_time
    intervals = spec.simulation_intervals
    if template_id == "rc_filter":
        stop_time = max(stop_time, 12.5 * values["resistance"] * values["capacitance"])
    elif template_id == "rlc_filter":
        frequency = values["cutoff_frequency"]
        damping_time = 2.0 * values["inductance"] / values["resistance"]
        stop_time = max(stop_time, 30.0 / frequency, 10.0 * damping_time)
        intervals = max(intervals, math.ceil(stop_time * frequency * 100.0))
    elif template_id == "thermal_single_room":
        stop_time = max(
            stop_time,
            10.0 * values["heat_capacity"] * values["thermal_resistance"],
        )
    elif template_id == "thermal_dual_room":
        slow_outer_time = max(
            values["heat_capacity_a"] * values["outdoor_resistance_a"],
            values["heat_capacity_b"] * values["outdoor_resistance_b"],
        )
        stop_time = max(stop_time, 10.0 * slow_outer_time)
    if not math.isfinite(stop_time) or stop_time <= 0 or intervals < 2:
        raise TemplateInputError(("derived simulation plan is invalid",))
    return SimulationPlan(start_time=0.0, stop_time=stop_time, intervals=intervals)


def _relation_issues(spec: TemplateSpec, values: Mapping[str, float]) -> list[str]:
    if spec.relation == "rc_cutoff":
        actual = 1.0 / (2.0 * math.pi * values["resistance"] * values["capacitance"])
        return _relative_relation_issue("RC cutoff frequency", actual, values["cutoff_frequency"])
    if spec.relation == "rlc_resonance":
        actual = 1.0 / (
            2.0 * math.pi * math.sqrt(values["inductance"] * values["capacitance"])
        )
        return _relative_relation_issue("RLC resonant frequency", actual, values["cutoff_frequency"])
    if spec.relation == "opamp_supplies":
        if values["negative_supply"] >= values["positive_supply"]:
            return ["negative_supply must be lower than positive_supply"]
    return []


def _relative_relation_issue(label: str, actual: float, expected: float) -> list[str]:
    relative_error = abs(actual - expected) / max(abs(expected), 1e-30)
    if relative_error > 0.02:
        return [
            f"{label} mismatch: derived={actual:.12g}, declared={expected:.12g}, "
            f"relative_error={relative_error:.6g}"
        ]
    return []


def _inside_bounds(value: float, spec: ParameterSpec) -> bool:
    if spec.minimum is not None:
        if spec.minimum_inclusive:
            if value < spec.minimum:
                return False
        elif value <= spec.minimum:
            return False
    if spec.maximum is not None:
        if spec.maximum_inclusive:
            if value > spec.maximum:
                return False
        elif value >= spec.maximum:
            return False
    return True


def _modelica_real(value: float) -> str:
    text = format(value, ".17g")
    return text.replace("e+", "e")
