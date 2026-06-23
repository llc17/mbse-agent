# -*- coding: utf-8 -*-
"""
=============================================================================
main.py — V2 程序入口
=============================================================================

这是你运行 `python main.py` 时执行的第一个文件。
它负责：解析命令行参数 → 检查环境 → 创建输出目录 → 构建图 → 运行 + HITL 交互循环。

══════════════════════════════════════════════════════════════
HITL 交互循环 — 最关键的运行时逻辑:

  1. graph.invoke(state, config)  → 图开始执行
  2. 图跑到 interrupt() → 暂停，抛出 GraphInterrupt
  3. graph.get_state(config) → 检查快照，发现有一个 interrupt
  4. handle_interrupt() → 在终端展示中断信息，等待用户输入
  5. graph.invoke(Command(resume=decision), config) → 图从断点恢复
  6. 重复步骤 2-5，直到没有 interrupt
══════════════════════════════════════════════════════════════
"""

# ====================================================================
# 导入
# ====================================================================

import sys                                                 # 系统操作（退出程序、路径管理）
from pathlib import Path

# ═════════════════════════════════════════════════════════════════
# 路径修复: 确保 from src.xxx import 能找到模块
# 问题: 在 week3/ 目录下运行 python src/main.py 时，
#       Python 的搜索路径不含 week3/，导致 import src.pipeline 报 No module named 'src'
# 修复: 手动把项目根目录(D:\mbse\week3)加到 sys.path 最前面
# ═════════════════════════════════════════════════════════════════
_src_dir = Path(__file__).resolve().parent               # D:\mbse\week3\src
_project_dir = _src_dir.parent                            # D:\mbse\week3
if str(_project_dir) not in sys.path:
    sys.path.insert(0, str(_project_dir))                # 插入到搜索路径最前面

import argparse                                            # 解析命令行参数
import json                                                # JSON 处理
import logging                                             # 日志
import uuid                                                # 生成唯一 ID（用于 checkpoint thread_id）
from datetime import datetime                              # 时间戳

from langgraph.types import Command                        # HITL 恢复指令
# Command(resume=value): 告诉 LangGraph "从断点恢复，把 value 作为 interrupt() 的返回值"
# 这是在调用 graph.invoke(Command(...), config) 时用的

from src.pipeline import build_pipeline, PipelineState     # 图构建 + 状态类型
from src.utils import check_prerequisites, make_run_dir    # 环境检查 + 目录创建


# ====================================================================
# setup_logging() — 配置日志系统
# ====================================================================
def setup_logging(run_dir: Path) -> None:
    """
    配置 Python logging 模块。

    两个输出通道（handler）:
      1. StreamHandler → 终端（你看到的实时输出）
      2. FileHandler   → 文件（outputs/run_xxx/results/run.log）

    这解决了 V1 "没有日志，出问题找不到根因"的问题。
    """
    log_path = run_dir / "results" / "run.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    logging.basicConfig(                                   # 一次性配置所有 logger
        level=logging.INFO,                                # 只记录 INFO 及以上级别
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        # %(asctime)s  = 时间戳
        # %(name)s     = logger 名称（如 "llm_client", "pipeline"）
        # %(levelname)s = 级别（INFO/WARNING/ERROR）
        # %(message)s  = 日志内容
        handlers=[
            logging.StreamHandler(sys.stdout),             # 输出到终端
            logging.FileHandler(log_path, encoding="utf-8"),  # 输出到文件
        ],
    )


# ====================================================================
# print_banner() — 打印启动横幅
# ====================================================================
def print_banner():
    """打印程序启动画面。"""
    print("=" * 60)                                        # 打印 60 个等号
    print("  MBSE+AI 自动化闭环系统 — V2 LangGraph 版")
    print("=" * 60)


# ====================================================================
# handle_interrupt() — 处理 HITL 中断
# ====================================================================
def handle_interrupt(interrupt_data: dict) -> dict:
    """
    当图在 interrupt() 处暂停时，这个函数被调用。

    它负责:
      1. 在终端展示中断信息（让用户看到节点产出了什么）
      2. 获取用户的决策（确认 or 打回）
      3. 返回决策字典

    参数:
      interrupt_data: interrupt() 传入的值（就是 HITL 节点的 decision 变量）
        例如: {"node": "node1", "data": {"component_type": "RC低通", ...}}

    返回:
      {"action": "approve"}  或  {"action": "reject", "feedback": "..."}
    """
    node = interrupt_data.get("node", "")                  # 哪个节点触发的
    message = interrupt_data.get("message", "")            # 暂停消息
    data = interrupt_data.get("data", {})                  # 节点的产出数据

    # ---- 打印分隔线和标题 ----
    print("\n" + "─" * 50)
    print(f"  ⏸️  {message}")
    print("─" * 50)

    # ---- 根据节点类型展示不同的信息 ----
    if node == "node1":
        # 节点1：展示需求摘要
        print(f"  组件类型: {data.get('component_type', '?')}")
        print(f"  参数: {json.dumps(data.get('parameters', {}), ensure_ascii=False)}")
        # json.dumps: 字典 → 格式化的 JSON 字符串
        # ensure_ascii=False: 允许中文显示（否则会变成 \uXXXX 格式）
        print(f"  拓扑: {data.get('topology', '?')}")
        print(f"  约束: {data.get('constraints', [])}")
        print(f"  精炼轮数: {data.get('clarification_rounds', 0)}")
        print()
        choice = input("  [回车=确认 / r+回车=打回并输入反馈]: ").strip()
        if choice.lower().startswith("r"):                # 用户输入以 r 开头
            feedback = input("  反馈内容: ").strip()       #   读反馈
            return {"action": "reject", "feedback": feedback}
        return {"action": "approve"}

    elif node == "node2":
        # 节点2：展示 SysML 文件路径
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

    return {"action": "approve"}                           # 未知节点，默认确认


# ====================================================================
# print_summary() — 打印全流程总结
# ====================================================================
def print_summary(state: dict):
    """打印最终结果汇总。"""
    print("\n" + "=" * 60)
    print("  全流程完成!")
    print("=" * 60)

    # ---- 列出产出目录 ----
    run_dir = Path(state.get("run_dir", ""))
    if run_dir.exists():
        print(f"\n产出目录: {run_dir}")
        for sub in sorted(run_dir.iterdir()):             # 遍历子目录（按名字排序）
            if sub.is_dir():                               # 只处理目录
                print(f"  {sub.name}/")
                for f in sorted(sub.iterdir()):
                    # 跳过临时文件
                    if f.name.startswith("_") or f.name.startswith("run."):
                        continue
                    size = f.stat().st_size                # 文件大小（字节）
                    print(f"    {f.name} ({size:,} bytes)")
                    # :, 格式化 → 1,234（加上千位分隔符）

    # ---- 打印耗时 ----
    timing = state.get("timing", {})
    if timing:
        print(f"\n耗时统计:")
        for k, v in timing.items():
            print(f"  {k}: {v:.1f}s")                     # :.1f = 保留 1 位小数

    # ---- 打印仿真结果 ----
    mo = state.get("mo", {})
    emoji = "✅" if mo.get("success") else "❌"            # 三元表达式选 emoji
    print(f"\n仿真结果: {emoji} {'成功' if mo.get('success') else '失败'}")
    print(f"  节点3 总尝试: {mo.get('attempts', '?')} 次")


# ====================================================================
# main() — 程序入口
# ====================================================================
def main():
    """
    程序主函数。

    执行顺序:
      1. 解析命令行参数
      2. 检查运行环境
      3. 创建输出目录 + 配置日志
      4. 获取用户输入
      5. 保存 prompt 快照
      6. 构建初始 state
      7. 编译 + 运行图（含 HITL 循环）
      8. 打印结果
    """
    # ========== 步骤 1: 解析命令行参数 ==========
    parser = argparse.ArgumentParser(
        description="MBSE+AI V2 — LangGraph 闭环流水线"
    )
    parser.add_argument(
        "--mode", choices=["interactive", "experiment"],
        default="interactive",
        help="运行模式 (default: interactive)"
    )
    parser.add_argument(
        "--temperature", type=float, default=0.3,
        help="LLM 温度 (default: 0.3)"
    )
    parser.add_argument(
        "--max-retries", type=int, default=5,
        help="节点3 最大自修复次数 (default: 5)"
    )
    parser.add_argument(
        "--max-rejects", type=int, default=3,
        help="最大打回次数 (default: 3)"
    )
    parser.add_argument(
        "--thread-id", type=str, default=None,
        help="线程 ID（用于 checkpoint 恢复）"
    )
    args = parser.parse_args()                             # 解析命令行输入

    print_banner()

    # ========== 步骤 2: 环境检查 ==========
    missing = check_prerequisites()
    if missing:
        print("\n❌ 环境检查失败，缺失项:")
        for m in missing:
            print(f"  - {m}")
        print("\n请安装缺失的依赖后重试。")
        sys.exit(1)                                        # 退出程序，返回码 1（异常退出）
    print("✅ 环境检查通过")

    # ========== 步骤 3: 创建输出目录 + 配置日志 ==========
    run_dir = make_run_dir("outputs")
    setup_logging(run_dir)
    logging.getLogger("pipeline").info(
        "启动 V2 流水线, mode=%s, temp=%.2f, retries=%s",
        args.mode, args.temperature, args.max_retries
    )

    # ========== 步骤 4: 获取用户输入 ==========
    raw_input = input(
        "\n请输入系统需求（例: 做一个 1kHz 截止频率的 RC 低通滤波器）:\n> "
    ).strip()

    if not raw_input:
        print("未输入需求，退出。")
        sys.exit(0)                                        # 返回码 0（正常退出）

    # ========== 步骤 5: 保存 prompt 快照（版本追溯） ==========
    import shutil                                           # 文件复制工具
    prompts_src = Path(__file__).parent.parent / "prompts"  # 源目录
    prompts_dst = run_dir / "results" / "prompts_snapshot"  # 目标目录
    prompts_dst.mkdir(parents=True, exist_ok=True)
    for pf in prompts_src.glob("*.txt"):                   # 遍历所有 .txt 文件
        shutil.copy2(pf, prompts_dst / pf.name)             # copy2 保留文件元信息（修改时间等）
    # 这样以后打开 run_dir 就知道是哪个版本的 prompt 跑出来的

    # ========== 步骤 6: 构建初始 state ==========
    # 生成唯一的 thread_id（用于 checkpoint 隔离不同的运行）
    thread_id = args.thread_id or str(uuid.uuid4())[:8]
    # uuid.uuid4() 生成类似 "a1b2c3d4-e5f6-..." 的随机ID
    # str(...)[:8] 取前 8 个字符

    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": 100,                          # 子图内部节点多，默认25不够
                                                         # 节点3 子图每次循环 4 个节点 × 5 次重试 = 20+，
                                                         # 总流程轻松超 25，提到 100 安全
    }
    # config 是 LangGraph 的配置字典
    # thread_id 用于 checkpoint：同一个 thread_id 可以恢复之前的执行状态
    # recursion_limit: 图执行的最大步数，超过抛 GraphRecursionError

    initial_state: PipelineState = {
        "raw_input": raw_input,
        "req": None,                                       # 所有产物初始为 None
        "sysml": None,
        "mo": None,
        "summary": None,
        "node_status": {                                   # 所有节点初始为 pending
            "node1": "pending", "node2": "pending",
            "node3": "pending", "node4": "pending",
        },
        "human_feedback": "",
        "reject_count_per_node": {},                       # 初始未被打回过
        "temperature": args.temperature,
        "max_retries": args.max_retries,
        "max_rejects": args.max_rejects,
        "dialogue_history": [],
        "timing": {},
        "run_dir": str(run_dir),                           # 存为字符串（Path 不能 JSON 序列化）
        "mode": args.mode,
    }

    # ========== 步骤 7: 编译 + 运行图 ==========
    graph = build_pipeline()

    # 打印 Mermaid 流程图
    print(f"\n📊 流程图 (Mermaid):")
    print(graph.get_graph().draw_mermaid())
    # draw_mermaid() 输出 Mermaid 格式的图定义
    # 复制到 mermaid.live 或 GitHub markdown 就可以看到图

    # ── 首次调用：启动图 ──
    state = graph.invoke(initial_state, config)
    # invoke 会执行图直到遇到 interrupt 或 END

    snapshot = graph.get_state(config)                     # 获取当前快照
    # snapshot.interrupts: 如果图在 interrupt() 处暂停了，这里会有数据

    # ── HITL 循环：只要还有 interrupt 就不停 -─
    while snapshot.interrupts:                             # 检查是否有中断待处理
        for intr in snapshot.interrupts:                   # 遍历所有中断
            decision = handle_interrupt(intr.value)         #   intr.value 是 interrupt() 传入的数据
            # 用 Command(resume=decision) 恢复执行
            # decision 作为 interrupt() 的返回值传给 HITL 节点
            state = graph.invoke(Command(resume=decision), config)
            snapshot = graph.get_state(config)             #   获取最新快照

    # ========== 步骤 8: 打印结果 ==========
    print_summary(state)


# ====================================================================
# Python 魔术变量 — 程序的真正入口
# ====================================================================
if __name__ == "__main__":
    # __name__ 是一个内置变量，表示当前模块的名字
    # 如果这个文件是直接运行的（python main.py），__name__ == "__main__" 为 True
    # 如果这个文件是被 import 的（from main import xxx），__name__ == "main"，不等于 "__main__"
    # 所以这行代码的意思是：只有直接运行这个文件时才执行 main()
    main()
