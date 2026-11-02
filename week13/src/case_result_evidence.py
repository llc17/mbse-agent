"""Owned final-output evidence and an explicitly derived CSV checking view.

Pure file processing only: this module never launches a tool or loads runtime
authority. A final value may fill a missing series only when the same run's XML
classifies that output as a nonchangeable Boolean parameter.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
from pathlib import Path
import re
import xml.etree.ElementTree as ET


RECEIPT_SCHEMA = "v12-owned-runtime-parameter-output-2"


def output_names(names=None, *, parameter_names=()):
    if names is None:
        return ()
    if not isinstance(names, (list, tuple)):
        raise ValueError("output names must be a list or tuple")
    names = tuple(names)
    if any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_]\w*", name, re.ASCII)
           or name == "time" for name in names):
        raise ValueError("output names must be scalar identifiers; time is reserved")
    if len(set(names)) != len(names) or set(names) & set(parameter_names):
        raise ValueError("duplicate/overlapping runtime output name")
    return names


def _sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _number(value, label):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError("nonfinite " + label)
    return value


def constant_boolean_metadata(xml_path, names):
    """Read classification only. Boolean fixed/start attributes are not values."""
    names = output_names(names)
    tree = ET.parse(xml_path)
    found = {}
    for node in tree.iter("ScalarVariable"):
        name = node.get("name")
        if name not in names:
            continue
        if name in found:
            raise ValueError("duplicate compiled output: " + name)
        required = {"causality": "output", "variability": "parameter", "classType": "bPar",
                    "alias": "noAlias", "isValueChangeable": "false"}
        if any(node.get(key) != value for key, value in required.items()) or node.find("Boolean") is None:
            raise ValueError("output is not a nonchangeable Boolean parameter: " + name)
        found[name] = dict(node.attrib)
    if set(found) != set(names):
        raise ValueError("compiled constant Boolean output absent")
    return found


def runtime_file_bindings(command, out, persisted_stdout):
    """Called by the owned runner after return; stdout is persisted immediately after."""
    out = Path(out).resolve()
    files = {"source": out / "model.mo", "executable": Path(command[0]).resolve(),
             "init_xml": out / "model_init.xml", "raw_csv": out / "model_res.csv",
             "stdout": out / "simulation.stdout.log"}
    if not files["executable"].is_relative_to(out):
        raise ValueError("runtime executable outside current run")
    return {key: {"path": str(path), "sha256": hashlib.sha256(persisted_stdout).hexdigest()
                  if key == "stdout" else _sha(path) if path.is_file() else None}
            for key, path in files.items()}


def read_runtime_outputs(source, out, *, parameter_names, names, stop_time):
    """Validate the owned receipt and parse exact final stdout values, never expectations."""
    out = Path(out).resolve()
    parameters = output_names(parameter_names)
    names = output_names(names, parameter_names=parameters)
    if not names:
        raise ValueError("constant output evidence requires an explicit nonempty output list")
    path = out / "runtime-parameter-output.json"
    receipt = json.loads(path.read_text(encoding="utf8"))
    if receipt.get("schema") != RECEIPT_SCHEMA or receipt.get("requested_parameters") != list(parameters) or receipt.get("requested_outputs") != list(names):
        raise ValueError("runtime output receipt/list binding differs")
    if not receipt.get("attempt_id") or not receipt.get("task_id") or type(receipt.get("pid")) is not int or receipt["pid"] <= 0:
        raise ValueError("owned runtime process identity absent")
    if receipt.get("returncode") != 0 or receipt.get("timed_out") is not False or receipt.get("uncertain") is not False:
        raise ValueError("owned runtime process unsuccessful/uncertain")
    command = receipt.get("command")
    if not isinstance(command, list) or not command or any(not isinstance(arg, str) for arg in command):
        raise ValueError("owned runtime command absent")
    if [arg for arg in command if arg.startswith("-output=")] != ["-output=" + ",".join(parameters + names)] or any(arg.startswith(("-override", "-f=")) for arg in command):
        raise ValueError("owned runtime output command differs")
    if [arg for arg in command if arg.startswith("-r=")] != ["-r=" + str(out / "model_res.csv")] or command.count("-s=dassl") != 1:
        raise ValueError("owned runtime CSV/solver command differs")
    raw_stdout = (out / "simulation.stdout.log").read_bytes()
    files = runtime_file_bindings(command, out, raw_stdout)
    if files != receipt.get("result_files") or any(item["sha256"] is None for item in files.values()):
        raise ValueError("runtime file path/hash binding differs")
    source_sha = hashlib.sha256(source.encode("utf8")).hexdigest()
    if receipt.get("source_sha256") != source_sha or files["source"]["sha256"] != source_sha:
        raise ValueError("runtime source binding differs")
    if receipt.get("stdout_sha256") != files["stdout"]["sha256"]:
        raise ValueError("runtime stdout hash binding differs")
    metadata = constant_boolean_metadata(out / "model_init.xml", names)
    expected_time = _number(stop_time, "requested stop time")
    default = ET.parse(out / "model_init.xml").find(".//DefaultExperiment")
    if default is None or not math.isclose(_number(default.get("stopTime"), "XML stop time"), expected_time, rel_tol=0, abs_tol=max(1e-12, abs(expected_time) * 1e-9)):
        raise ValueError("compiled/requested stop time differs")
    lines = [line.strip() for line in raw_stdout.decode("utf8").splitlines() if re.match(r"^\s*time\s*=", line)]
    if len(lines) != 1:
        raise ValueError("exactly one final runtime output line required")
    values = {}
    for item in lines[0].split(","):
        match = re.fullmatch(r"([A-Za-z_]\w*)\s*=\s*([^,\s]+)", item.strip(), re.ASCII)
        if not match or match[1] in values:
            raise ValueError("malformed/duplicate runtime output value")
        name, token = match.groups()
        if name not in {"time", *parameters, *names}:
            raise ValueError("unexpected runtime output: " + name)
        if name in names:
            if token not in ("0", "1"):
                raise ValueError("Boolean runtime output must be explicit 0/1: " + name)
            values[name] = int(token)
        else:
            values[name] = _number(token, "runtime output " + name)
    if set(values) != {"time", *parameters, *names}:
        raise ValueError("runtime output values incomplete")
    if not math.isclose(values["time"], expected_time, rel_tol=0, abs_tol=max(1e-12, abs(expected_time) * 1e-9)):
        raise ValueError("runtime output is not at requested stop time")
    return {"status": "PASS", "parameters": {key: values[key] for key in parameters},
            "outputs": {key: values[key] for key in names}, "time": values["time"],
            "output_line": lines[0], "compiled_constant_outputs": metadata,
            "receipt_path": str(path), "receipt_sha256": _sha(path), "receipt": receipt,
            "result_files": files}


def _validate_process(record, receipt, out):
    if not isinstance(record, dict):
        raise ValueError("same-ledger owned process record required")
    if any(record.get(key) != value for key, value in {"attempt_id": receipt["attempt_id"], "kind": "simulation", "state": "FINISHED", "pid": receipt["pid"]}.items()):
        raise ValueError("same-ledger owned process identity/state differs")
    detail = json.loads(record["detail_json"])
    result = json.loads(record["result_json"])
    if detail.get("task_id") != receipt["task_id"] or detail.get("command") != receipt["command"] or Path(detail.get("cwd", "")).resolve() != out:
        raise ValueError("same-ledger owned process command/task/path differs")
    if result.get("returncode") != 0 or result.get("timed_out") is not False or result.get("uncertain") is not False:
        raise ValueError("same-ledger runtime result unsuccessful/uncertain")
    stdout = result.get("stdout")
    if not isinstance(stdout, str) or hashlib.sha256(stdout.replace("\n", os.linesep).encode("utf8")).hexdigest() != receipt["stdout_sha256"]:
        raise ValueError("same-ledger stdout differs")


def build_check_input_view(source, out, destination, *, parameter_names, names, stop_time,
                           required_columns, process_record):
    """Append certified constant outputs to a new view, preserving original record bytes."""
    out = Path(out).resolve()
    destination = Path(destination).resolve()
    if destination.exists():
        raise FileExistsError("checking view destination must be new")
    evidence = read_runtime_outputs(source, out, parameter_names=parameter_names, names=names, stop_time=stop_time)
    _validate_process(process_record, evidence["receipt"], out)
    raw_path = out / "model_res.csv"
    raw = raw_path.read_bytes()
    records = list(csv.reader(io.StringIO(raw.decode("utf-8-sig"), newline=""), strict=True))
    physical = raw.splitlines(keepends=True)
    if not records or len(records) != len(physical) or any(not row for row in records):
        raise ValueError("CSV must have one nonempty record per physical line")
    headers = [name.strip().strip('"') for name in records[0]]
    if len(set(headers)) != len(headers) or any(len(row) != len(headers) for row in records):
        raise ValueError("duplicate/ragged CSV fields")
    required = tuple(required_columns)
    if not required or len(set(required)) != len(required) or "time" not in required:
        raise ValueError("explicit unique required CSV columns with time required")
    missing = [name for name in required if name not in headers]
    if not missing or any(name not in evidence["outputs"] for name in missing):
        raise ValueError("missing raw column has no certified constant output evidence")
    for name, value in evidence["outputs"].items():
        if name in headers:
            for row in records[1:]:
                token = row[headers.index(name)].strip().lower()
                observed = 1 if token == "true" else 0 if token == "false" else _number(token, name)
                if observed != value:
                    raise ValueError("raw/runtime Boolean output conflict: " + name)
    additions = [(',"' + name + '"').encode("ascii") for name in missing]
    values = ["," + str(evidence["outputs"][name]) for name in missing]
    view_lines = []
    for index, original in enumerate(physical):
        ending = b"\r\n" if original.endswith(b"\r\n") else b"\n" if original.endswith(b"\n") else b"\r" if original.endswith(b"\r") else b""
        body = original[:-len(ending)] if ending else original
        suffix = b"".join(additions) if index == 0 else "".join(values).encode("ascii")
        view_lines.append(body + suffix + ending)
    view = b"".join(view_lines)
    view_records = list(csv.reader(io.StringIO(view.decode("utf-8-sig"), newline=""), strict=True))
    if len(view_records) != len(records) or any(original != expanded[:len(headers)] for original, expanded in zip(records, view_records)):
        raise ValueError("checking view changed original records")
    if raw != raw_path.read_bytes():
        raise ValueError("raw CSV changed while deriving checking view")
    if evidence != read_runtime_outputs(source, out, parameter_names=parameter_names, names=names, stop_time=stop_time):
        raise ValueError("runtime evidence changed while deriving checking view")
    manifest = {"schema": "v12-derived-constant-output-check-input-1",
                "input_type": "DERIVED_CHECK_INPUT_VIEW_NOT_NATIVE_OMC_CSV",
                "numerical_result": "NOT_ASSESSED_BY_ADAPTER", "source_files": evidence["result_files"],
                "owned_output_receipt": {"path": evidence["receipt_path"], "sha256": evidence["receipt_sha256"]},
                "owned_process_record": process_record,
                "runtime_output_line": evidence["output_line"], "runtime_output_time": evidence["time"],
                "new_columns": {name: {"value": evidence["outputs"][name], "type": "Boolean",
                    "origin": "SAME_OWNED_SIMULATION_FINAL_STDOUT_CERTIFIED_CONSTANT_BY_COMPILED_XML",
                    "compiled_classification": evidence["compiled_constant_outputs"][name]} for name in missing},
                "original_header_fields": records[0], "raw_data_rows": len(records) - 1,
                "view_data_rows": len(view_records) - 1, "original_record_bytes_preserved_before_appended_fields": True,
                "event_duplicate_rows_preserved": True,
                "checking_view": {"path": str(destination / "check-input.csv"), "sha256": hashlib.sha256(view).hexdigest()}}
    destination.mkdir(parents=False)
    (destination / "check-input.csv").write_bytes(view)
    (destination / "source-manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf8", newline="\n")
    return manifest
