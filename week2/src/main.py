"""
MBSE+AI 自动化闭环系统 — 主入口。

跑 `python main.py` 启动完整 4 节点流水线：
  节点 1: 多轮对话精炼需求
  节点 2: 生成 SysML v2 .sysml 文件
  节点 3: 生成 Modelica .mo 文件 + 编译仿真 + 自修复
  节点 4: 生成 summary.md

所有产出存到 outputs/run_<时间戳>/ 目录。
"""

import json
import sys
from datetime import datetime
from pathlib import Path

from src.schemas import StructuredRequirement
from src.node1_requirement import refine_requirement
from src.node2_sysml import generate_sysml
from src.node3_modelica import generate_and_simulate
from src.node4_summary import generate_summary


def main():
    print("=" * 60)
    print("  MBSE+AI 自动化闭环系统 — 第一版最丑跑通")
    print("=" * 60)

    # ---- 创建输出目录 ----
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    run_dir = Path("outputs") / f"run_{timestamp}"
    sysml_dir = run_dir / "sysml"
    modelica_dir = run_dir / "modelica"
    results_dir = run_dir / "results"
    for d in [sysml_dir, modelica_dir, results_dir]:
        d.mkdir(parents=True, exist_ok=True)
    print(f"\n输出目录: {run_dir}")
    print(f"  SysML 代码 → {sysml_dir}")
    print(f"  Modelica 代码 → {modelica_dir}")
    print(f"  结果 → {results_dir}")

    # ============================================================
    # 节点 1: 需求精炼
    # ============================================================
    print("\n" + "=" * 60)
    print("  节点 1 — 需求解析（多轮对话）")
    print("=" * 60)
    print("请输入你的系统需求，例如: 做一个 1kHz 截止频率的 RC 低通滤波器")

    raw_input = input("\n你的需求: ").strip()
    if not raw_input:
        print("未输入需求，退出。")
        sys.exit(0)

    req = refine_requirement(raw_input)
    print(f"\n[节点1] 需求精炼完成，轮数: {req.clarification_rounds}")
    print(f"        类型: {req.component_type}")
    print(f"        参数: {req.parameters}")
    print(f"        完整: {req.is_complete}")

    req_path = results_dir / "requirement.json"
    req_path.write_text(req.model_dump_json(indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"        已保存: {req_path}")

    # ============================================================
    # 节点 2: SysML v2 生成
    # ============================================================
    print("\n" + "=" * 60)
    print("  节点 2 — SysML v2 代码生成")
    print("=" * 60)
    sysml_artifact = generate_sysml(req, sysml_dir)
    print(f"[节点2] SysML 生成完成，尝试次数: {sysml_artifact.attempts}")
    print(f"        文件: {sysml_artifact.file_path}")
    if sysml_artifact.errors:
        print(f"        警告: {sysml_artifact.errors}")
    print(f"        请手动打开 Eclipse 看图。")

    # ============================================================
    # 节点 3: Modelica 生成 + 仿真 + 自修复
    # ============================================================
    print("\n" + "=" * 60)
    print("  节点 3 — Modelica 生成 + 仿真 + 自修复")
    print("=" * 60)
    mo_artifact = generate_and_simulate(req, sysml_artifact, modelica_dir, results_dir)
    print(f"[节点3] Modelica 仿真完成")
    print(f"        成功: {mo_artifact.success}")
    print(f"        尝试次数: {mo_artifact.attempts}")
    print(f"        .mo 文件: {mo_artifact.file_path}")
    if mo_artifact.success:
        print(f"        仿真 PNG: {mo_artifact.plot_path}")
    if mo_artifact.errors:
        print(f"        错误记录: {len(mo_artifact.errors)} 条")

    # ============================================================
    # 节点 4: 总结
    # ============================================================
    print("\n" + "=" * 60)
    print("  节点 4 — 生成总结")
    print("=" * 60)
    summary = generate_summary(req, sysml_artifact, mo_artifact, results_dir)
    print(f"[节点4] 总结已生成: {summary.file_path}")

    # ============================================================
    # 完成
    # ============================================================
    print("\n" + "=" * 60)
    print("  全流程完成!")
    print("=" * 60)
    print(f"\n产出目录: {run_dir}")
    for sub in sorted(run_dir.iterdir()):
        if sub.is_dir():
            print(f"  {sub.name}/")
            for f in sorted(sub.iterdir()):
                if f.name.startswith("_"):
                    continue
                size = f.stat().st_size
                print(f"    {f.name} ({size:,} bytes)")


if __name__ == "__main__":
    main()
