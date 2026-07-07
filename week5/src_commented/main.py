# -*- coding: utf-8 -*-
"""
=============================================================================
main.py — V4 主入口
=============================================================================

整个系统的启动文件。命令行用法:

  # 交互模式（带 HITL 确认，每个节点都暂停让用户确认/打回）
  python main.py --mode interactive

  # 实验模式（自动确认，不做中断，适合批量测试）
  python main.py --mode experiment

  # 带参数
  python main.py --mode experiment --temperature 0.3 --max-retries 5 --max-rejects 3

V4 新增:
  - node3_hitl 的交互处理（回车=确认 / r=打回Modelica / s=打回SysML）
  - expected_physics 字段初始化
  - 标题改为 V4 质量深化版
=============================================================================
"""

import sys
from pathlib import Path

# ── Windows UTF-8 强制适配 ──
# 中文 Windows 默认用 GBK 编码，print 带 emoji/中文时会崩溃。
# 这行把 stdout 编码强行改成 UTF-8。
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# ── 把项目根目录加入 sys.path ──
# 确保 `from src.xxx import` 能正常工作
_src_dir = Path(__file__).resolve().parent         # D:\mbse\week5\src
_project_dir = _src_dir.parent                      # D:\mbse\week5
if str(_project_dir) not in sys.path:
    sys.path.insert(0, str(_project_dir))

import argparse
import json
import logging

from langgraph.types import Command  # LangGraph: HITL 中断恢复命令

from src.pipeline import build_pipeline, PipelineState
from src.utils import check_prerequisites, make_run_dir


# ==========================================================================
# 日志配置
# ==========================================================================

def setup_logging(run_dir: Path) -> None:
    """配置日志: 终端彩色输出 + 文件持久化。"""
    log_path = run_dir / "results" / "run.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),                         # 终端输出
            logging.FileHandler(log_path, encoding="utf-8"),           # 日志文件
        ],
    )


# ==========================================================================
# Banner
# ==========================================================================

def print_banner():
    print("=" * 60)
    print("  MBSE+AI 自动化闭环系统 — V4 质量深化版")
    print("=" * 60)


# ==========================================================================
# HITL 中断处理
# ==========================================================================

def handle_interrupt(interrupt_data: dict) -> dict:
    """
    处理 HITL 中断: 展示当前节点的产出，获取用户的确认/打回决定。

    这是 interactive 模式的"交互控制台"。每个 HITL 节点暂停时，
    LangGraph 调用 interrupt() 把控制权交到这里。本函数打印数据、
    等待用户输入、返回 decision dict。

    V4 新增 node3 的处理分支 — 支持三选一: 确认/打回Modelica/打回SysML。
    """
    node = interrupt_data.get("node", "")        # 哪个节点触发了中断
    message = interrupt_data.get("message", "")  # 给用户的提示信息
    data = interrupt_data.get("data", {})        # 节点传出的数据

    print("\n" + "─" * 50)
    print(f"  [暂停] {message}")
    print("─" * 50)

    # ── 节点1 HITL: 需求确认 ──
    if node == "node1":
        print(f"  组件类型: {data.get('component_type', '?')}")
        print(f"  参数: {json.dumps(data.get('parameters', {}), ensure_ascii=False)}")
        print(f"  拓扑: {data.get('topology', '?')}")
        print(f"  约束: {data.get('constraints', [])}")
        print(f"  精炼轮数: {data.get('clarification_rounds', 0)}")
        print()
        choice = input("  [回车=确认 / r+回车=打回并输入反馈]: ").strip()
        if choice.lower().startswith("r"):
            feedback = input("  反馈内容: ").strip()
            return {"action": "reject", "feedback": feedback}
        return {"action": "approve"}

    # ── 节点2 HITL: SysML 确认 ──
    elif node == "node2":
        print(f"  SysML 文件: {data.get('file_path', '?')}")
        print(f"  生成尝试: {data.get('attempts', '?')} 次")
        if data.get("errors"):
            print(f"  语法警告: {data['errors']}")
        print(f"  请用 Eclipse 打开 .sysml 文件查看模型图。")
        print()
        choice = input("  [回车=确认 / r+回车=打回并输入反馈]: ").strip()
        if choice.lower().startswith("r"):
            feedback = input("  反馈内容: ").strip()
            return {"action": "reject", "feedback": feedback}
        return {"action": "approve"}

    # ── V4: 节点3 HITL: 仿真确认 ──
    elif node == "node3":
        print(f"  仿真结果: {'成功' if data.get('success') else '失败'}")
        print(f"  尝试次数: {data.get('attempts', '?')}")
        print(f"  修复次数: {data.get('repair_count', 0)}")
        if data.get("errors"):
            print(f"  最近错误: {data['errors'][-1][:200] if data['errors'] else '无'}")
        # 打印文件路径（用户可复制到文件管理器打开）
        print(f"  仿真曲线: {data.get('plot_path', '?')}")
        print(f"  仿真数据: {data.get('csv_path', '?')}")
        # 物理验证预检（如果可用的話）
        qp = data.get("quality_preview", {})
        if qp.get("physics_passed") is not None:
            status = "通过" if qp['physics_passed'] else f"偏差 {qp.get('physics_deviation', '?')}%"
            print(f"  物理预检: {status}")
        print()
        # V4 三选项: 回车=确认, r=modelica, s=sysml
        print("  [回车=确认 / r+回车=打回Modelica重做 / s+回车=打回SysML重做]")
        choice = input("> ").strip()
        if choice.lower().startswith("s"):
            feedback = input("  反馈内容: ").strip()
            return {"action": "reject_sysml", "feedback": feedback}
        elif choice.lower().startswith("r"):
            feedback = input("  反馈内容: ").strip()
            return {"action": "reject", "feedback": feedback}
        return {"action": "approve"}

    # 未知节点 → 默认确认放行
    return {"action": "approve"}


# ==========================================================================
# 全流程总结打印
# ==========================================================================

def print_summary(state: dict):
    """打印全流程结束后的产出目录树和关键指标。"""
    print("\n" + "=" * 60)
    print("  全流程完成!")
    print("=" * 60)

    # 列出产出目录的所有文件
    run_dir = Path(state.get("run_dir", ""))
    if run_dir.exists():
        print(f"\n产出目录: {run_dir}")
        for sub in sorted(run_dir.iterdir()):
            if sub.is_dir():
                print(f"  {sub.name}/")
                for f in sorted(sub.iterdir()):
                    if f.name.startswith("_") or f.name.startswith("run."):
                        continue  # 跳过内部文件
                    size = f.stat().st_size
                    print(f"    {f.name} ({size:,} bytes)")

    # 打印各节点耗时
    timing = state.get("timing", {})
    if timing:
        print(f"\n耗时统计:")
        for k, v in timing.items():
            print(f"  {k}: {v:.1f}s")

    # 仿真结果汇总
    mo = state.get("mo", {})
    print(f"\n仿真结果: {'成功' if mo.get('success') else '失败'}")
    print(f"  节点3 总尝试: {mo.get('attempts', '?')} 次")

    # V3: 质量检查汇总
    qc = state.get("quality_checks", {})
    if qc:
        cross = qc.get("cross_validate", {})
        physics = qc.get("physics_validate", {})
        print(f"交叉校验: {'通过' if cross.get('passed') else '未通过'}")
        print(f"物理验证: {'通过' if physics.get('passed') else '未通过'}"
              f" (偏差 {physics.get('deviation_percent', '?')}%)")

    # V3: 修复日志
    repair_log = state.get("repair_log", [])
    if repair_log:
        print(f"node3 修复: {len(repair_log)} 次")


# ==========================================================================
# main() — 命令行入口
# ==========================================================================

def main():
    """解析命令行参数 → 构建图 → 运行 → HITL 循环 → 打印总结。"""

    # ── 解析命令行参数 ──
    parser = argparse.ArgumentParser(description="MBSE+AI V4 — LangGraph 闭环流水线")
    parser.add_argument("--mode", choices=["interactive", "experiment"], default="interactive",
                        help="运行模式 (default: interactive)")
    parser.add_argument("--temperature", type=float, default=0.3, help="LLM 温度 (default: 0.3)")
    parser.add_argument("--max-retries", type=int, default=5, help="节点3 最大自修复次数 (default: 5)")
    parser.add_argument("--max-rejects", type=int, default=3, help="最大打回次数 (default: 3)")
    parser.add_argument("--thread-id", type=str, default=None, help="线程 ID（用于 checkpoint 恢复）")
    args = parser.parse_args()

    print_banner()

    # ── 环境检查（Python 包 / OMPython / API Key）──
    missing = check_prerequisites()
    if missing:
        print("\n环境检查失败，缺失项:")
        for m in missing:
            print(f"  - {m}")
        sys.exit(1)
    print("环境检查通过")

    # ── 创建输出目录 ──
    run_dir = make_run_dir("outputs").resolve()  # 创建 outputs/run_2026-07-07_143000/
    setup_logging(run_dir)
    logging.getLogger("pipeline").info("启动 V4 流水线, mode=%s, temp=%.2f, retries=%s",
                                       args.mode, args.temperature, args.max_retries)

    # ── 获取用户输入 ──
    if args.mode == "experiment":
        # 实验模式: test_case 由 run_experiment.py 传入（这里只作 fallback）
        raw_input = input("\n请输入系统需求: ").strip()
    else:
        raw_input = input("\n请输入系统需求（例: 做一个 1kHz 截止频率的 RC 低通滤波器）:\n> ").strip()

    if not raw_input:
        print("未输入需求，退出。")
        sys.exit(0)

    # ── 保存本次运行的 prompt 副本（事后追溯版本差异）──
    import shutil
    prompts_src = Path(__file__).parent.parent / "prompts"
    prompts_dst = run_dir / "results" / "prompts_snapshot"
    prompts_dst.mkdir(parents=True, exist_ok=True)
    for pf in prompts_src.glob("*.txt"):
        shutil.copy2(pf, prompts_dst / pf.name)

    # ── 构建初始状态 ──
    import uuid
    thread_id = args.thread_id or str(uuid.uuid4())[:8]  # 每次运行一个唯一 ID
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}
    # ↑ recursion_limit: LangGraph 默认 25，这里设 100 防止复杂循环超限

    initial_state: PipelineState = {
        # 核心数据
        "raw_input": raw_input,
        "req": None, "sysml": None, "mo": None, "summary": None,
        # HITL 控制
        "node_status": {"node1": "pending", "node2": "pending",
                        "node3": "pending", "node4": "pending"},
        "human_feedback": "",
        "reject_count_per_node": {},
        "mode": args.mode,
        # LLM 参数
        "temperature": args.temperature,
        "max_retries": args.max_retries,
        "max_rejects": args.max_rejects,
        "dialogue_history": [],
        # 运行管理
        "timing": {}, "run_dir": str(run_dir),
        # V3 质量设施
        "quality_checks": {}, "repair_log": [], "physics_feedback": "",
        # V4: interactive 用自动检测，experiment 用 run_experiment.py 传的配置
        "expected_physics": None,
    }

    # ── 编译图并打印 Mermaid 结构 ──
    graph = build_pipeline()
    print(f"\n流程图 (Mermaid):")
    print(graph.get_graph().draw_mermaid())

    # ── 运行 + HITL 循环 ──
    # 首次 invoke: 启动流水线，遇到 interrupt() 会暂停
    state = graph.invoke(initial_state, config)
    snapshot = graph.get_state(config)  # 获取当前快照

    # HITL 循环: 只要还有未处理的中断，就继续处理
    while snapshot.interrupts:
        for intr in snapshot.interrupts:
            decision = handle_interrupt(intr.value)       # 向用户展示，获取决定
            state = graph.invoke(Command(resume=decision), config)  # 恢复执行
            snapshot = graph.get_state(config)             # 更新快照

    # ── 打印总结 ──
    print_summary(state)


if __name__ == "__main__":
    main()
