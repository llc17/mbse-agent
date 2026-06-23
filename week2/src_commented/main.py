# -*- coding: utf-8 -*-
"""
=============================================================================
main.py — Week 2 主入口（V1 Sequential 版本）
=============================================================================

跑 `python main.py` 启动完整 4 节点流水线。

V1 特点（和 V2 对比）:
  - 4 个函数顺序调用（不是 LangGraph 图）
  - 无状态对象（变量直接传）
  - 无中断/暂停（一路走到底）
  - 无 checkpoint（崩溃就重来）
  - 无实验框架

流程:
  输入 → 节点1 多轮对话精炼需求 → 节点2 生成 .sysml
       → 节点3 生成 .mo + 编译仿真 + 自修复 → 节点4 生成 summary.md
"""

# ====================================================================
# 导入
# ====================================================================

import json                                              # JSON 处理
import sys                                               # sys.exit() 退出程序
from datetime import datetime                            # 生成时间戳
from pathlib import Path

from src.schemas import StructuredRequirement            # 节点1 产出的类型
from src.node1_requirement import refine_requirement      # 节点1 函数
from src.node2_sysml import generate_sysml                # 节点2 函数
from src.node3_modelica import generate_and_simulate      # 节点3 函数
from src.node4_summary import generate_summary            # 节点4 函数


# ====================================================================
# main() — 程序入口
# ====================================================================
def main():
    # ---- 打印横幅 ----
    print("=" * 60)
    print("  MBSE+AI 自动化闭环系统 — 第一版最丑跑通")
    print("=" * 60)

    # ========== 创建输出目录 ==========
    # strftime: 把 datetime 格式化为字符串
    # 例如: "2026-05-26_165806"
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    run_dir = Path("outputs") / f"run_{timestamp}"      # 拼接路径

    sysml_dir = run_dir / "sysml"                        # SysML 文件子目录
    modelica_dir = run_dir / "modelica"                  # Modelica 文件子目录
    results_dir = run_dir / "results"                    # 结果子目录

    for d in [sysml_dir, modelica_dir, results_dir]:
        d.mkdir(parents=True, exist_ok=True)             # 创建所有子目录

    print(f"\n输出目录: {run_dir}")
    print(f"  SysML 代码 → {sysml_dir}")
    print(f"  Modelica 代码 → {modelica_dir}")
    print(f"  结果 → {results_dir}")

    # ============================================================
    # 节点 1: 需求解析（多轮对话）
    # ============================================================
    print("\n" + "=" * 60)
    print("  节点 1 — 需求解析（多轮对话）")
    print("=" * 60)
    print("请输入你的系统需求，例如: 做一个 1kHz 截止频率的 RC 低通滤波器")

    raw_input = input("\n你的需求: ").strip()             # input() 从终端读一行
    if not raw_input:                                    # 用户直接按回车
        print("未输入需求，退出。")
        sys.exit(0)                                      # 正常退出（返回码 0）

    # ---- 调用节点1 ----
    req = refine_requirement(raw_input)
    print(f"\n[节点1] 需求精炼完成，轮数: {req.clarification_rounds}")
    print(f"        类型: {req.component_type}")
    print(f"        参数: {req.parameters}")
    print(f"        完整: {req.is_complete}")

    # ---- 保存到 JSON ----
    req_path = results_dir / "requirement.json"
    req_path.write_text(
        req.model_dump_json(indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    print(f"        已保存: {req_path}")

    # ============================================================
    # 节点 2: SysML v2 代码生成
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
    # 节点 3: Modelica 生成 + 编译 + 仿真 + 自修复
    # ============================================================
    print("\n" + "=" * 60)
    print("  节点 3 — Modelica 生成 + 仿真 + 自修复")
    print("=" * 60)

    mo_artifact = generate_and_simulate(
        req, sysml_artifact, modelica_dir, results_dir
    )
    print(f"[节点3] Modelica 仿真完成")
    print(f"        成功: {mo_artifact.success}")
    print(f"        尝试次数: {mo_artifact.attempts}")
    print(f"        .mo 文件: {mo_artifact.file_path}")
    if mo_artifact.success:
        print(f"        仿真 PNG: {mo_artifact.plot_path}")
    if mo_artifact.errors:
        print(f"        错误记录: {len(mo_artifact.errors)} 条")

    # ============================================================
    # 节点 4: 生成总结
    # ============================================================
    print("\n" + "=" * 60)
    print("  节点 4 — 生成总结")
    print("=" * 60)

    summary = generate_summary(req, sysml_artifact, mo_artifact, results_dir)
    print(f"[节点4] 总结已生成: {summary.file_path}")

    # ============================================================
    # 完成 — 打印产出目录
    # ============================================================
    print("\n" + "=" * 60)
    print("  全流程完成!")
    print("=" * 60)

    print(f"\n产出目录: {run_dir}")
    for sub in sorted(run_dir.iterdir()):                # 遍历子目录（按名字排序）
        if sub.is_dir():
            print(f"  {sub.name}/")
            for f in sorted(sub.iterdir()):
                if f.name.startswith("_"):               # 跳过临时文件（_sim.mos 等）
                    continue
                size = f.stat().st_size                  # f.stat().st_size = 文件大小（字节）
                print(f"    {f.name} ({size:,} bytes)")
                # :, 格式化 → 1,234（千位分隔符）


# ====================================================================
# Python 魔术变量 — "只有直接运行这个文件时才执行 main()"
# ====================================================================
if __name__ == "__main__":
    # __name__ 是 Python 内置变量
    # 如果你执行 "python main.py" → __name__ == "__main__" → 调用 main()
    # 如果你执行 "from main import something" → __name__ == "main" → 不调用 main()
    main()
