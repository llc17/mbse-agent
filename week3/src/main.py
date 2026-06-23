"""
MBSE+AI 自动化闭环系统 V2 — 主入口。

用法:
    python main.py                          # 交互模式（带 HITL 确认）
    python main.py --mode experiment        # 实验模式（自动确认）
    python main.py --temperature 0.3 --max-retries 5 --max-rejects 3

LangGraph 状态图 + HITL interrupt + 节点3子图自修复。
"""

import sys
from pathlib import Path

# 把项目根目录加入 Python 搜索路径，确保 from src.xxx import 能正常工作
_src_dir = Path(__file__).resolve().parent        # D:\mbse\week3\src
_project_dir = _src_dir.parent                     # D:\mbse\week3
if str(_project_dir) not in sys.path:
    sys.path.insert(0, str(_project_dir))

import argparse
import json
import logging

from langgraph.types import Command

from src.pipeline import build_pipeline, PipelineState
from src.utils import check_prerequisites, make_run_dir


def setup_logging(run_dir: Path) -> None:
    """配置日志：终端 + 文件。"""
    log_path = run_dir / "results" / "run.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(log_path, encoding="utf-8"),
        ],
    )


def print_banner():
    print("=" * 60)
    print("  MBSE+AI 自动化闭环系统 — V2 LangGraph 版")
    print("=" * 60)


def handle_interrupt(interrupt_data: dict) -> dict:
    """处理 HITL 中断：展示数据，获取用户决策。"""
    node = interrupt_data.get("node", "")
    message = interrupt_data.get("message", "")
    data = interrupt_data.get("data", {})

    print("\n" + "─" * 50)
    print(f"  ⏸️  {message}")
    print("─" * 50)

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

    return {"action": "approve"}


def print_summary(state: dict):
    """打印全流程总结。"""
    print("\n" + "=" * 60)
    print("  全流程完成!")
    print("=" * 60)
    run_dir = Path(state.get("run_dir", ""))
    if run_dir.exists():
        print(f"\n产出目录: {run_dir}")
        for sub in sorted(run_dir.iterdir()):
            if sub.is_dir():
                print(f"  {sub.name}/")
                for f in sorted(sub.iterdir()):
                    if f.name.startswith("_") or f.name.startswith("run."):
                        continue
                    size = f.stat().st_size
                    print(f"    {f.name} ({size:,} bytes)")

    timing = state.get("timing", {})
    if timing:
        print(f"\n耗时统计:")
        for k, v in timing.items():
            print(f"  {k}: {v:.1f}s")

    mo = state.get("mo", {})
    print(f"\n仿真结果: {'✅ 成功' if mo.get('success') else '❌ 失败'}")
    print(f"  节点3 总尝试: {mo.get('attempts', '?')} 次")


def main():
    parser = argparse.ArgumentParser(description="MBSE+AI V2 — LangGraph 闭环流水线")
    parser.add_argument("--mode", choices=["interactive", "experiment"], default="interactive",
                        help="运行模式 (default: interactive)")
    parser.add_argument("--temperature", type=float, default=0.3, help="LLM 温度 (default: 0.3)")
    parser.add_argument("--max-retries", type=int, default=5, help="节点3 最大自修复次数 (default: 5)")
    parser.add_argument("--max-rejects", type=int, default=3, help="最大打回次数 (default: 3)")
    parser.add_argument("--thread-id", type=str, default=None, help="线程 ID（用于 checkpoint 恢复）")
    args = parser.parse_args()

    print_banner()

    # 环境检查
    missing = check_prerequisites()
    if missing:
        print("\n❌ 环境检查失败，缺失项:")
        for m in missing:
            print(f"  - {m}")
        print("\n请安装缺失的依赖后重试。")
        sys.exit(1)
    print("✅ 环境检查通过")

    # 创建输出目录
    run_dir = make_run_dir("outputs")
    setup_logging(run_dir)
    logging.getLogger("pipeline").info("启动 V2 流水线, mode=%s, temp=%.2f, retries=%s",
                                       args.mode, args.temperature, args.max_retries)

    # 获取用户输入（实验模式下从 test_case 读）
    if args.mode == "experiment":
        test_case = args.test_case if hasattr(args, 'test_case') else None
        if test_case:
            raw_input = test_case
        else:
            raw_input = input("\n请输入系统需求: ").strip()
    else:
        raw_input = input("\n请输入系统需求（例: 做一个 1kHz 截止频率的 RC 低通滤波器）:\n> ").strip()

    if not raw_input:
        print("未输入需求，退出。")
        sys.exit(0)

    # 保存 prompt 模板到 run_dir（版本追溯）
    import shutil
    prompts_src = Path(__file__).parent.parent / "prompts"
    prompts_dst = run_dir / "results" / "prompts_snapshot"
    prompts_dst.mkdir(parents=True, exist_ok=True)
    for pf in prompts_src.glob("*.txt"):
        shutil.copy2(pf, prompts_dst / pf.name)

    # 构建初始状态
    import uuid
    thread_id = args.thread_id or str(uuid.uuid4())[:8]
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}

    initial_state: PipelineState = {
        "raw_input": raw_input,
        "req": None,
        "sysml": None,
        "mo": None,
        "summary": None,
        "node_status": {"node1": "pending", "node2": "pending", "node3": "pending", "node4": "pending"},
        "human_feedback": "",
        "reject_count_per_node": {},
        "temperature": args.temperature,
        "max_retries": args.max_retries,
        "max_rejects": args.max_rejects,
        "dialogue_history": [],
        "timing": {},
        "run_dir": str(run_dir),
        "mode": args.mode,
    }

    # 编译图
    graph = build_pipeline()
    print(f"\n📊 流程图 (Mermaid):")
    print(graph.get_graph().draw_mermaid())

    # ── 运行 + HITL 循环 ──
    state = graph.invoke(initial_state, config)
    snapshot = graph.get_state(config)

    while snapshot.interrupts:
        for intr in snapshot.interrupts:
            decision = handle_interrupt(intr.value)
            state = graph.invoke(Command(resume=decision), config)
            snapshot = graph.get_state(config)

    print_summary(state)


if __name__ == "__main__":
    main()
