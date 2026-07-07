"""
V4 验收脚本 — 不依赖 LLM API 的完整性检查。

用法:
    cd D:/mbse/week5
    python verify_v4.py

检查项:
    1. Pipeline 构建完整性（所有 V4 节点注册）
    2. sysmlpy 可用性 + V4 语法通过
    3. 测试用例完整性（6 个用例 + expected_physics）
    4. Prompt 模板升级验证（无残留 V3 语法）
    5. 验证函数注册表完整性
    6. 物理验证自动检测路由
    7. CSV 列过滤逻辑
    8. 参数占位符覆盖
"""

import sys
from pathlib import Path

_project_dir = Path(__file__).resolve().parent  # D:\mbse\week5
if str(_project_dir) not in sys.path:
    sys.path.insert(0, str(_project_dir))

import json
import logging

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger("verify_v4")

PASS = 0
FAIL = 0


def check(name: str, condition: bool, detail: str = "") -> bool:
    global PASS, FAIL
    if condition:
        PASS += 1
        logger.info("  [PASS] %s", name)
    else:
        FAIL += 1
        logger.error("  [FAIL] %s — %s", name, detail)
    return condition


def main():
    global PASS, FAIL
    print("=" * 60)
    print("  V4 验收脚本")
    print("=" * 60)

    # ── 1. Pipeline 构建 ──
    print("\n1. Pipeline 构建")
    try:
        from src.pipeline import build_pipeline, PipelineState
        g = build_pipeline()
        nodes = list(g.get_graph().nodes.keys())
        check("Pipeline 构建成功", True)
        check("node3_hitl 已注册", "node3_hitl" in nodes)
        check("Q_cross_validate 已注册", "Q_cross_validate" in nodes)
        check("Q_physics_validate 已注册", "Q_physics_validate" in nodes)
        check("总共 11 个节点", len(nodes) == 11, f"实际: {len(nodes)}")
    except Exception as e:
        check("Pipeline 构建", False, str(e))

    # ── 2. sysmlpy 可用性 ──
    print("\n2. sysmlpy 语法检查")
    from src.node2_sysml import _check_sysmlpy, _syntax_check
    ok, ver = _check_sysmlpy()
    check(f"sysmlpy 可用 (v{ver})", ok)

    # V4 正确语法应通过
    v4_code = """package TestPackage {
    private import ScalarValues::*;
    port def TestPort;
    part def TestPart {
        attribute value : Real;
        port p : TestPort;
    }
}"""
    errs = _syntax_check(v4_code)
    fatal = [e for e in errs if "[fatal]" in e]
    check("V4 标准语法 0 fatal", len(fatal) == 0, f"fatal={fatal}")

    # V3 旧语法应被拦截
    v3_code = """package Test { import ISQ::*; import SI::*; part def X {{ attribute r :> ISQ::resistance; }} }"""
    errs = _syntax_check(v3_code)
    check("V3 旧语法 import ISQ 拦截", any("ISQ" in e for e in errs))
    check("V3 旧语法 import SI 拦截", any("SI" in e for e in errs))
    check("V3 旧语法 :> ISQ:: 拦截", any("ISQ::" in e for e in errs))

    # ── 3. 测试用例完整性 ──
    print("\n3. 测试用例 + expected_physics")
    tc_path = Path("experiments/test_cases.json")
    with open(tc_path, encoding="utf-8") as f:
        cases = json.load(f)["test_cases"]
    check(f"测试用例数量: {len(cases)}", len(cases) >= 3, f"实际: {len(cases)}")

    expected_ids = {"rc_lowpass", "rc_lowpass_v2", "single_room_thermal",
                    "rlc_lowpass", "dual_room_thermal", "opamp_inverting"}
    actual_ids = {c["id"] for c in cases}
    check("所有预期用例存在", expected_ids.issubset(actual_ids),
          f"缺失: {expected_ids - actual_ids}")

    physics_count = sum(1 for c in cases if c.get("expected_physics"))
    check(f"含 expected_physics 的用例: {physics_count}", physics_count >= 4)

    expected_types = {"rc_cutoff", "thermal_steady", "rlc_resonant_freq", "opamp_gain"}
    actual_types = {c.get("expected_physics", {}).get("validate_type") for c in cases if c.get("expected_physics")}
    check("所有验证类型覆盖", expected_types.issubset(actual_types),
          f"缺失: {expected_types - actual_types}")

    # ── 4. Prompt 模板检查 ──
    print("\n4. Prompt 模板升级验证")
    from src.utils import load_prompt
    sysml_prompt = load_prompt("node2_sysml.txt")
    check("含 private import ScalarValues", "private import ScalarValues" in sysml_prompt)
    check("不含 import ISQ::* (旧语法)", "import ISQ::*" not in sysml_prompt or
          sysml_prompt.count("禁止") > 0 and "ISQ" in sysml_prompt)
    check("不含 import SI::* (旧语法)", "import SI::*" not in sysml_prompt or
          sysml_prompt.count("禁止") > 0 and "SI" in sysml_prompt)
    check("不含 :> ISQ:: (旧语法)", ":> ISQ::" not in sysml_prompt or
          "禁止" in sysml_prompt.split(":> ISQ::")[0][-50:])
    check("不含双花括号 {{", "{{" not in sysml_prompt)

    modelica_prompt = load_prompt("node3_modelica.txt")
    check("含 RLC 示例", "RLCSeries" in modelica_prompt or "RLC" in modelica_prompt)
    check("含运放示例", "OpAmp" in modelica_prompt or "运放" in modelica_prompt)

    # ── 5. 验证函数注册表 ──
    print("\n5. 物理验证函数注册表")
    from src.node_quality import VALIDATOR_REGISTRY, _auto_detect_type
    check(f"注册表项数: {len(VALIDATOR_REGISTRY)}", len(VALIDATOR_REGISTRY) >= 4)

    for vtype in ["rc_cutoff", "thermal_steady", "rlc_resonant_freq", "opamp_gain"]:
        check(f"  {vtype} 已注册", vtype in VALIDATOR_REGISTRY)

    check("自动检测 RC→rc_cutoff", _auto_detect_type("RC低通滤波器", {}) == "rc_cutoff")
    check("自动检测 RLC→rlc_resonant", _auto_detect_type("RLC低通滤波器", {}) == "rlc_resonant_freq")
    check("自动检测 热→thermal_steady", _auto_detect_type("单房间热传导", {}) == "thermal_steady")
    check("自动检测 运放→opamp_gain", _auto_detect_type("运放反相放大器", {}) == "opamp_gain")

    # ── 6. 参数占位符 ──
    print("\n6. 参数占位符覆盖")
    from src.node2_sysml import _build_parameter_replacements
    reps = _build_parameter_replacements({"R": 1000, "C": 1e-6, "L": 0.01, "Rf": 10000, "Rin": 1000})
    check(f"占位符生成: {len(reps)} keys", len(reps) >= 10)
    check("含 {parameters_R}", "{parameters_R}" in reps)
    check("含 {parameters_L}", "{parameters_L}" in reps)
    check("含 {parameters_Rf}", "{parameters_Rf}" in reps)

    # ── 7. CSV 列过滤 ──
    print("\n7. CSV 列过滤更新")
    mo_path = Path("src/node3_modelica.py")
    mo_text = mo_path.read_text(encoding="utf-8")
    check("含 opAmp.out 优先", "opAmp.out" in mo_text or "opamp.out" in mo_text)
    check("含 Vpp/Vnn 过滤", ".Vpp" in mo_text and ".Vnn" in mo_text)

    # ── 8. 分支和环境 ──
    print("\n8. Git 分支和环境")
    import subprocess
    try:
        r = subprocess.run(["git", "branch", "--show-current"], capture_output=True, text=True)
        branch = r.stdout.strip()
        check(f"当前分支: {branch}", "v4" in branch or "week5" in branch or "week4" in branch or "master" in branch)
    except Exception:
        check("Git 检查", False, "git 不可用")

    # ── 总结 ──
    print("\n" + "=" * 60)
    total = PASS + FAIL
    print(f"  结果: {PASS}/{total} 通过, {FAIL} 失败")
    if FAIL == 0:
        print("  [OK] V4 验收全部通过!")
    else:
        print(f"  [WARN]  {FAIL} 项未通过，请检查")
    print("=" * 60)

    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
