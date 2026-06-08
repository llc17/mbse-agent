"""
╔══════════════════════════════════════════════════════════════════════════════╗
║  main.py — 主入口，串联 4 节点流水线                                          ║
║                                                                            ║
║  这是整条流水线的"总控"：创建输出目录 → 依次调用4个节点 → 打印结果               ║
║                                                                            ║
║  完整调用链（数据流向）：                                                      ║
║                                                                            ║
║   用户输入                                                                   ║
║     │                                                                       ║
║     ▼                                                                       ║
║   raw_input = input("你的需求: ")                                            ║
║     │                                                                       ║
║     ▼                                                                       ║
║   req = refine_requirement(raw_input)          ← 节点1                       ║
║     │  req = StructuredRequirement(component_type, parameters, ...)           ║
║     │  保存: results_dir/requirement.json                                    ║
║     │                                                                       ║
║     ├──────────────────────────────────┐                                    ║
║     ▼                                  │                                    ║
║   sysml = generate_sysml(req, sysml_dir)  ← 节点2                           ║
║     │  sysml = SysMLArtifact(sysml_code, file_path, ...)                     ║
║     │  保存: sysml_dir/model.sysml                                           ║
║     │                                                                       ║
║     ├──────────────────────────────────┐                                    ║
║     ▼                                  ▼                                    ║
║   mo = generate_and_simulate(req, sysml, modelica_dir, results_dir)  ← 节点3 ║
║     │  mo = ModelicaArtifact(modelica_code, success, csv_path, ...)          ║
║     │  保存: modelica_dir/model.mo                                           ║
║     │        results_dir/simulation.csv                                      ║
║     │        results_dir/simulation.png                                      ║
║     │                                                                       ║
║     ├──────────────────┬──────────────────┐                                 ║
║     ▼                  ▼                  ▼                                 ║
║   summary = generate_summary(req, sysml, mo, results_dir)  ← 节点4           ║
║     │  summary = SummaryArtifact(summary_text, file_path, ...)               ║
║     │  保存: results_dir/summary.md                                          ║
║     │                                                                       ║
║     ▼                                                                       ║
║   打印最终产物目录                                                             ║
║                                                                            ║
║  运行命令：cd week2 && python -m src.main                                     ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""

import json             # 标准库：JSON（本文件未直接使用，保留备用）
import sys              # 标准库：sys.exit() 退出程序
from datetime import datetime  # 标准库：生成时间戳
from pathlib import Path  # 标准库：Path 路径对象

# 导入 4 个节点的核心函数
from src.node1_requirement import refine_requirement  # 节点1
from src.node2_sysml import generate_sysml             # 节点2
from src.node3_modelica import generate_and_simulate  # 节点3
from src.node4_summary import generate_summary         # 节点4


# ===== 主函数 =====
def main():                                # 程序入口
    """启动完整 4 节点流水线。"""

    # 第26-28行：打印横幅
    print("=" * 60)                        # "=" 重复60次 = 分隔线
    print("  MBSE+AI 自动化闭环系统 — 第一版最丑跑通")
    print("=" * 60)

    # ============================================================
    #  第30-40行：创建输出目录结构
    # ============================================================
    timestamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")  # "2026-05-26_163000"
    run_dir = Path("outputs") / f"run_{timestamp}"          # outputs/run_2026-05-26_163000/
    sysml_dir = run_dir / "sysml"          # outputs/run_xxx/sysml/
    modelica_dir = run_dir / "modelica"    # outputs/run_xxx/modelica/
    results_dir = run_dir / "results"      # outputs/run_xxx/results/

    for d in [sysml_dir, modelica_dir, results_dir]:  # 遍历三个子目录
        d.mkdir(parents=True, exist_ok=True)           # 创建目录，父目录不存在也创建
    #   └────────── 创建目录的方法 ──────────┘

    print(f"\n输出目录: {run_dir}")
    print(f"  SysML 代码 → {sysml_dir}")
    print(f"  Modelica 代码 → {modelica_dir}")
    print(f"  结果 → {results_dir}")

    # ============================================================
    #  节点 1：需求精炼
    # ============================================================
    print("\n" + "=" * 60)
    print("  节点 1 — 需求解析（多轮对话）")
    print("=" * 60)
    print("请输入你的系统需求，例如: 做一个 1kHz 截止频率的 RC 低通滤波器")

    raw_input = input("\n你的需求: ").strip()  # 程序暂停，等用户打字
    if not raw_input:                          # 用户直接按回车 = 空输入
        print("未输入需求，退出。")
        sys.exit(0)                            # 退出程序，返回码0

    req = refine_requirement(raw_input)        # ← 调用节点1
    #    req = StructuredRequirement 对象

    print(f"\n[节点1] 需求精炼完成，轮数: {req.clarification_rounds}")
    print(f"        类型: {req.component_type}")
    print(f"        参数: {req.parameters}")
    print(f"        完整: {req.is_complete}")

    # 保存需求 JSON 到 results/
    req_path = results_dir / "requirement.json"
    req_path.write_text(                     # 写文本文件
        req.model_dump_json(indent=2, ensure_ascii=False),  # Pydantic → JSON字符串
        encoding="utf-8"
    )
    print(f"        已保存: {req_path}")

    # ============================================================
    #  节点 2：SysML v2 生成
    # ============================================================
    print("\n" + "=" * 60)
    print("  节点 2 — SysML v2 代码生成")
    print("=" * 60)

    sysml_artifact = generate_sysml(req, sysml_dir)  # ← 调用节点2
    #                sysml_artifact = SysMLArtifact 对象

    print(f"[节点2] SysML 生成完成，尝试次数: {sysml_artifact.attempts}")
    print(f"        文件: {sysml_artifact.file_path}")
    if sysml_artifact.errors:              # errors 非空 = 有语法警告
        print(f"        警告: {sysml_artifact.errors}")
    print(f"        请手动打开 Eclipse 看图。")

    # ============================================================
    #  节点 3：Modelica 生成 + 仿真 + 自修复
    # ============================================================
    print("\n" + "=" * 60)
    print("  节点 3 — Modelica 生成 + 仿真 + 自修复")
    print("=" * 60)

    mo_artifact = generate_and_simulate(   # ← 调用节点3
        req,                               # 传节点1产出
        sysml_artifact,                    # 传节点2产出
        modelica_dir,                      # .mo 保存位置
        results_dir                        # CSV/PNG 保存位置
    )
    # mo_artifact = ModelicaArtifact 对象

    print(f"[节点3] Modelica 仿真完成")
    print(f"        成功: {mo_artifact.success}")
    print(f"        尝试次数: {mo_artifact.attempts}")
    print(f"        .mo 文件: {mo_artifact.file_path}")
    if mo_artifact.success:                # 仿真跑通了
        print(f"        仿真 PNG: {mo_artifact.plot_path}")
    if mo_artifact.errors:                 # 有错误记录
        print(f"        错误记录: {len(mo_artifact.errors)} 条")

    # ============================================================
    #  节点 4：总结
    # ============================================================
    print("\n" + "=" * 60)
    print("  节点 4 — 生成总结")
    print("=" * 60)

    summary = generate_summary(            # ← 调用节点4
        req,                               # 需求
        sysml_artifact,                    # SysML
        mo_artifact,                       # Modelica 仿真
        results_dir                        # 保存位置
    )
    # summary = SummaryArtifact 对象

    print(f"[节点4] 总结已生成: {summary.file_path}")

    # ============================================================
    #  完成：打印最终产物目录
    # ============================================================
    print("\n" + "=" * 60)
    print("  全流程完成!")
    print("=" * 60)
    print(f"\n产出目录: {run_dir}")

    for sub in sorted(run_dir.iterdir()):  # 遍历 outputs/run_xxx/ 下所有文件和文件夹
        # sorted() 保证按名字排序
        if sub.is_dir():                   # 是文件夹
            print(f"  {sub.name}/")        # 打印文件夹名
            for f in sorted(sub.iterdir()):  # 遍历文件夹内的文件
                if f.name.startswith("_"):   # 以下划线开头的文件跳过（如 _sim.mos）
                    continue
                size = f.stat().st_size     # 文件大小（字节）
                print(f"    {f.name} ({size:,} bytes)")


# ===== Python 标准写法：只有直接运行此文件时才执行 main() =====
if __name__ == "__main__":                 # 如果是直接运行 python main.py
    main()                                 # 则执行 main()
#   如果是从别的文件 import main，则不执行 — 防止意外启动
