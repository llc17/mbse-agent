"""Write only source-bound artifacts; caller owns every external process and gate."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
from .design import APPROVED_SHA, ROOT, UnsupportedDesign, load_design
from .modelica import lower, SCENES


def _sysml(design):
    # SysML's unquoted `frame` is a reserved word in the pinned ANTLR grammar.
    # This changes only the SysML name; Modelica frame interfaces stay physical.
    lines=["package ActiveTwinWheelCandidate {", "  // Raw graph/source IDs trace to executable Modelica; parse success is not physics PASS.","  port def FramePort;", "  port def ShaftPort;", "  part def RigidEntity { port mechanicalFrame : FramePort; attribute mass : Real; attribute jy : Real; }", "  part def RevoluteEntity { port parentFrame : FramePort; port childFrame : FramePort; port axis : ShaftPort; port support : ShaftPort; }", "  part def FiniteDriveEntity { port shaft : ShaftPort; port support : ShaftPort; }", "  part vehicle {"]
    for b in design["graph"]["bodies"]:
        lines.extend(["    // physical IDs: "+",".join(b["physical_ids"]),"    part "+b["id"]+" : RigidEntity { attribute mass = "+format(b["m"],".17g")+"; attribute jy = "+format(b["inertia"][1][1],".17g")+"; }"])
    for j in design["graph"]["joints"]:
        lines.extend(["    part "+j["id"]+" : RevoluteEntity;","    part "+j["drive_id"]+" : FiniteDriveEntity;","    connect "+j["parent"]+".mechanicalFrame to "+j["id"]+".parentFrame;","    connect "+j["id"]+".childFrame to "+j["child"]+".mechanicalFrame;","    connect "+j["drive_id"]+".shaft to "+j["id"]+".axis;","    connect "+j["drive_id"]+".support to "+j["id"]+".support;"])
    lines.extend(["  }","}",""])
    return "\n".join(lines)

def generate(spec_path: Path, output: Path) -> dict:
    spec_path=Path(spec_path).resolve()
    output=Path(output).resolve()
    approved=(ROOT/"week12/results/s3-c-open-configuration-proposal-20261009-01/proposal-final.json").resolve()
    if spec_path != approved:
        raise UnsupportedDesign("UNBOUND_SPEC_PATH: only this approved frozen input is registered")
    target=(ROOT/"week12/results/s3-c-active-eightwheel-execution-20261009-01").resolve()
    try:output.relative_to(target)
    except ValueError as exc:raise UnsupportedDesign("OUTPUT_OUTSIDE_AUTHORIZED_EXECUTION_PACKAGE") from exc
    if output==target or output.exists():
        raise UnsupportedDesign("OUTPUT_MUST_BE_NEW_CHILD: preserve previous generated and failed evidence")
    design=load_design(spec_path)
    model,contract=lower(design)
    # All validation and lowering occurs before any write.
    output.mkdir(parents=True,exist_ok=False)
    # package.mo is special: OpenModelica requires its parent directory name
    # to equal the declared package. Our immutable run-directory name differs.
    # A standalone package file named for its root preserves both conventions.
    design_path=output/"design.json";sysml_path=output/"candidate.sysml";package_path=output/"V11GeneratedActive.mo"
    design_path.write_text(json.dumps(design,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    sysml_path.write_text(_sysml(design),encoding="utf-8")
    package_path.write_text(model,encoding="utf-8")
    contract_path=output/"output-contract.json"
    contract_path.write_text(json.dumps(contract,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    dependencies=[ROOT/"week12/models/components/V11PhysicalB1/package.mo",ROOT/"week12/models/components/V11ContactC2/package.mo",ROOT/"week12/models/components/V11ActiveClusterC3/package.mo"]
    sources=[Path(__file__).with_name(n) for n in ("__init__.py","design.py","generator.py","modelica.py")]+dependencies+[design_path,sysml_path,package_path,contract_path]
    rows=[dict(path=str(p),sha256=hashlib.sha256(p.read_bytes()).hexdigest()) for p in sources]
    traces=[]
    for j in design["graph"]["joints"]:
        traces.append(dict(source_id=j["id"],required_snippets=["MB.Joints.Revolute "+j["id"]+"(n={0,-1,0},useAxisFlange=true", "connect("+j["drive_id"]+".shaft,"+j["id"]+".axis)","connect("+j["drive_id"]+".support,"+j["id"]+".support)"]))
    manifest=dict(schema="v11-active-cluster-generation-manifest-1",spec_path=str(spec_path),spec_sha256=APPROVED_SHA,
        design_path=str(design_path),sysml_path=str(sysml_path),package_path=str(package_path),output_contract=contract,output_contract_path=str(contract_path),
        model_class_by_scene={name:"V11GeneratedActive."+data[0] for name,data in SCENES.items()},dependency_paths=[str(p) for p in dependencies],sources=rows,source_sha={r["path"]:r["sha256"] for r in rows},modelica_trace=traces,
        variable_filter=contract["variable_filter"],physical_status="UNVERIFIED",compile_status="NOT_RUN",sysml_parse_status="NOT_RUN",hardware_status="UNKNOWN",approval_record="External root receipt; backend never self approves")
    (output/"manifest.json").write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+"\n",encoding="utf-8")
    return manifest
