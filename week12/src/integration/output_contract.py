"""Stable public result names for the approved V11 S3-C generator.

The names in this module are the only variables requested from OpenModelica.
They are raw public outputs; no generated PASS flag is part of the contract.
"""
from __future__ import annotations

import re


BODY_SUFFIXES = ("x", "z", "vx", "vz", "pitch", "pitchRate")
WHEEL_SUFFIXES = (
    "omega", "omegaRelative", "Fx", "Fz", "normal", "penetration",
    "slip", "elasticEnergy", "normalLoss", "frictionLoss", "featureId",
    "contactCount", "rollingTorque", "rollingLoss", "torque",
    "shaftPower", "dcPower", "driveLoss",
)
GLOBAL_OUTPUTS = (
    "chassisPitch", "chassisPitchRate", "busPower", "busEnergy",
    "busCurrent", "auxPower", "refSpeed",
)
PROBE_OUTPUTS = (
    "probeOmega", "probeLoss", "probeKinetic", "probeShaftPower",
    "probeDcPower", "probeDriveLoss", "probeBusPower", "probeBusEnergy",
    "probeBusCurrent", "probeRollingTorque", "probeRollingLoss",
    "probeMechanicalEnergyResidual", "probeElectricalEnergyResidual",
)


def _identifier(value: str) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is None:
        raise ValueError("UNSUPPORTED: output component id is not a Modelica identifier")
    return value


def vehicle_output_names(design) -> tuple[str, ...]:
    """Return the ordered public-output contract for one vehicle scene."""
    mass_items = [*design["bodies"], *design["wheels"], *design["drives"], design["power_supply"]]
    names = ["time"]
    for item in mass_items:
        ident = _identifier(item["id"])
        names.extend(f"body_{ident}_{suffix}" for suffix in BODY_SUFFIXES)
    for item in design["wheels"]:
        ident = _identifier(item["id"])
        names.extend(f"wheel_{ident}_{suffix}" for suffix in WHEEL_SUFFIXES)
    names.extend(GLOBAL_OUTPUTS)
    if len(names) != len(set(names)):
        raise ValueError("INVALID: duplicate public output name")
    return tuple(names)


def probe_output_names() -> tuple[str, ...]:
    return ("time", *PROBE_OUTPUTS)


def variable_filter(names: tuple[str, ...]) -> str:
    """Build the conservative capture-group syntax supported by OMC.

    Every generated name is alphanumeric/underscore, so escaping is both
    unnecessary and avoided to keep the runtime expression portable.
    """
    if not names or any(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) is None for name in names):
        raise ValueError("INVALID: variable filter name")
    return "^(" + "|".join(names) + ")$"

