"""Parameter-slot rendering of the user-provided frozen aggregate template."""
from __future__ import annotations
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping
from .reference_three_profile import (
    PROFILE, SOURCE_PATH, SOURCE_SHA, MODEL_NAME, CONTRACT_SHA, AUTH_SHA, SCOPE_ID,
    WEEK12_ROOT, LIMITATIONS, parameter_descriptors, parameter_values, source_bytes,
    sha256_bytes, validate_reference_spec,
)

_DECLARATION = re.compile(r"(?m)^([ \t]*parameter[ \t]+(Real|Integer)[ \t]+([A-Za-z_][A-Za-z0-9_]*)[ \t]*=[ \t]*)([^;\r\n]+)(;)")


def _readback(raw: bytes) -> list[dict[str,str]]:
    return [{"type":m.group(2),"name":m.group(3),"expression":m.group(4).strip()}
            for m in _DECLARATION.finditer(raw.decode("utf-8"))]


from .reference_generator import render_model


def _new_output_dir(output_dir: str|Path) -> Path:
    output=Path(output_dir).resolve()
    relative=output.relative_to(WEEK12_ROOT.resolve())
    in_results=relative.parts and relative.parts[0]=='results' and len(relative.parts)>2 and relative.parts[1]!='user-4w2n'
    # Exact owned version generation path: projects/<id>/versions/<v>/generated.
    in_version=(len(relative.parts)==5 and relative.parts[0]=='projects'
        and relative.parts[2]=='versions' and relative.parts[4]=='generated'
        and re.fullmatch(r'prj_[0-9a-f]+',relative.parts[1])
        and re.fullmatch(r'v[0-9]+',relative.parts[3]))
    if not (in_results or in_version) or output.exists() or 'source-v1' in relative.parts:
        raise ValueError('reference generation requires a new results or exact reserved version directory')
    if not output.parent.exists() or not output.parent.is_dir():
        raise ValueError("output parent must already exist")
    return output


def generate_reference(spec: Mapping[str,Any], output_dir: str|Path) -> dict[str,Any]:
    validation=validate_reference_spec(spec)
    if not validation["ready"]:
        raise ValueError("reference spec not ready: "+json.dumps(validation["issues"],ensure_ascii=False))
    output=_new_output_dir(output_dir)
    canonical=validation["spec"]
    rendered=render_model(canonical["parameters"])
    output.mkdir(exist_ok=False)
    model_path=output/"candidate.mo"
    model_path.write_bytes(rendered)
    # Aggregate structural description only; no unsupported payload body or Multibody claims.
    attrs="\n".join("    attribute "+d["name"]+" : ScalarValues::"+
                    ("Integer" if d["type"]=="Integer" else "Real")+" = "+d["expression"]+";"
                    for d in parameter_descriptors())
    sysml=("package ReferenceAggregate {\n"
           "  private import ScalarValues::*;\n"
           "  // User reference aggregate, three equivalent 2D contact groups.\n"
           "  // Independent 20kg payload body, actual contacts and hardware NOT_VERIFIED.\n"
           "  part def UserReferenceTemplate {\n"+attrs+"\n  }\n"
           "  part referenceModel : UserReferenceTemplate;\n}\n").encode("utf-8")
    sysml_path=output/"candidate.sysml"
    sysml_path.write_bytes(sysml)
    source_readback=source_bytes()
    model_readback=model_path.read_bytes()
    declarations=_readback(model_readback)
    expected=[{"type":d["type"],"name":d["name"],"expression":d["expression"]} for d in parameter_descriptors()]
    if (sha256_bytes(source_readback)!=SOURCE_SHA or sha256_bytes(model_readback)!=sha256_bytes(rendered)
            or declarations!=expected or model_readback!=rendered):
        raise ValueError("independent generated model readback failed")
    source_map={"schema_version":"v11-reference-parameter-map-1","profile":PROFILE,
                "source":{"path":SOURCE_PATH,"sha256":SOURCE_SHA},
                "spec_digest":validation["spec_digest"],"parameters":parameter_descriptors(),
                "literal_values":canonical["parameters"],"requirements":canonical["requirements"],
                "limitations":list(LIMITATIONS)}
    map_path=output/"parameter-source-map.json"
    map_path.write_text(json.dumps(source_map,ensure_ascii=False,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    provenance={"schema_version":"v11-reference-generation-provenance-1","profile":PROFILE,
                "execution_scope_id":SCOPE_ID,"spec_digest":validation["spec_digest"],
                "contract_sha256":CONTRACT_SHA,"authorization_sha256":AUTH_SHA,
                "reference_source_sha256":SOURCE_SHA,"generated_model_sha256":sha256_bytes(model_readback),
                "source_body_sha256":sha256_bytes(source_readback),
                "generated_body_sha256":sha256_bytes(model_readback),
                "model_name":MODEL_NAME,"render_method":"TWENTY_LITERAL_SLOTS_FIVE_FIXED_DERIVED_EXPRESSIONS",
                "readback_status":"EXACT_INPUT_BOUND_THREE_STEP_VARIANT","numeric_convergence":"NOT_VERIFIED",
                "vehicle_task_status":"UNKNOWN","limitations":list(LIMITATIONS)}
    provenance_path=output/"generation-provenance.json"
    provenance_path.write_text(json.dumps(provenance,ensure_ascii=False,sort_keys=True,indent=2)+"\n",encoding="utf-8")
    artifacts={}
    for file in (model_path,sysml_path,map_path,provenance_path):
        artifacts[file.name]={"path":str(file),"sha256":sha256_bytes(file.read_bytes())}
    return {**provenance,"output_dir":str(output),"model_path":str(model_path),
            "sysml_path":str(sysml_path),"parameter_map_path":str(map_path),
            "provenance_path":str(provenance_path),"artifacts":artifacts,
            "generation_status":"PASS_IN_REFERENCE_SCOPE","sysml_syntax_status":"NOT_RUN"}
