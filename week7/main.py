"""
V6 5-Agent MBSE Pipeline — 入口。

用法:
    # 实验模式（自动运行，不等待人工确认）
    python main.py --mode experiment --case "做一个 1kHz RC 低通滤波器，R=1kΩ"

    # 交互模式（每个节点后人工确认）
    python main.py --mode interactive

    # 列出预定义用例
    python main.py --list-cases

    # 运行预定义用例
    python main.py --mode experiment --case rc_filter

    # 切换 LLM 提供商
    python main.py --mode experiment --case rc_filter --provider zhipu
"""

# 修复 Windows GBK 编码问题
import sys
import io
if sys.stdout.encoding != 'utf-8':
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

import argparse
import json
import logging
from pathlib import Path

# 确保 src 目录在 sys.path 中
sys.path.insert(0, str(Path(__file__).parent))

from src.pipeline import build_pipeline, PipelineState
from src.llm_client import set_provider, get_provider, get_token_stats, reset_token_stats
from src.utils import make_run_dir, check_prerequisites
from src.modelica_templates import list_all_templates

# ── 预定义用例（V4 的 6 用例）──
PREDEFINED_CASES = {
    "rc_filter": {
        "raw_input": "设计一个 1kHz RC 低通滤波器，电阻 R=1000Ω，电容 C=0.159μF，输入 5V 阶跃信号",
        "expected_physics": {
            "validate_type": "rc_cutoff",
            "tolerance_pct": 30.0,
        },
    },
    "rlc_circuit": {
        "raw_input": "设计一个 RLC 串联谐振电路，R=100Ω，L=10mH，C=100nF，输入 5V 阶跃信号",
        "expected_physics": {
            "validate_type": "rlc_resonant_freq",
            "tolerance_pct": 30.0,
            "extra_params": {"L": 0.01, "C_expected": 1e-7},
        },
    },
    "thermal_single": {
        "raw_input": "模拟一个单房间热传导系统，室外温度 35°C (308.15K)，室内初始温度 25°C (298.15K)，墙壁热阻 0.02 K/W，房间热容 500000 J/K",
        "expected_physics": {
            "validate_type": "thermal_steady",
            "tolerance_pct": 20.0,
            "expected_value": 308.15,
        },
    },
    "thermal_dual": {
        "raw_input": "模拟双房间热传导系统，室外 35°C (308.15K)，房间1初始 25°C (298.15K)，房间2初始 20°C (293.15K)，外墙热阻 0.02 K/W，共享墙热阻 0.01 K/W，两房间热容各 500000 J/K",
        "expected_physics": {
            "validate_type": "thermal_steady",
            "tolerance_pct": 20.0,
        },
    },
    "opamp_inverting": {
        "raw_input": "设计一个反相运算放大器，输入电阻 Rin=1kΩ，反馈电阻 Rf=10kΩ，输入电压 0.5V，正电源+15V，负电源-15V",
        "expected_physics": {
            "validate_type": "opamp_gain",
            "tolerance_pct": 30.0,
            "extra_params": {"expected_gain": -10.0, "expected_vout": -5.0},
        },
    },
    "rc_filter_custom": {
        "raw_input": "做一个截至频率大约 1591Hz 的低通滤波器，用 10kΩ 的电阻",
        "expected_physics": {
            "validate_type": "rc_cutoff",
            "tolerance_pct": 30.0,
        },
    },
}


def setup_logging(verbose: bool = False):
    """配置日志。"""
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%H:%M:%S",
    )


def main():
    parser = argparse.ArgumentParser(description="V6 5-Agent MBSE Pipeline")
    parser.add_argument("--mode", choices=["experiment", "interactive"],
                       default="experiment", help="运行模式")
    parser.add_argument("--case", type=str, help="预定义用例名 或 自定义需求文本")
    parser.add_argument("--list-cases", action="store_true", help="列出所有预定义用例")
    parser.add_argument("--provider", type=str, help="LLM 提供商 (deepseek 或 zhipu)")
    parser.add_argument("--temperature", type=float, default=0.3, help="LLM temperature")
    parser.add_argument("--max-retries", type=int, default=5, help="节点3最大重试次数")
    parser.add_argument("--max-rejects", type=int, default=3, help="HITL最大打回次数")
    parser.add_argument("--output-dir", type=str, default="outputs", help="输出目录")
    parser.add_argument("--verbose", "-v", action="store_true", help="详细日志")
    parser.add_argument("--check-env", action="store_true", help="检查运行环境")
    parser.add_argument("--list-templates", action="store_true", help="列出所有 Modelica 模板")
    args = parser.parse_args()

    # ── 列出预定义用例 ──
    if args.list_cases:
        print("\n预定义用例:")
        for name, case in PREDEFINED_CASES.items():
            print(f"  {name}: {case['raw_input'][:80]}...")
        return

    # ── 列出模板 ──
    if args.list_templates:
        print("\nModelica 领域模板:")
        for t in list_all_templates():
            print(f"  {t['name']} ({t['domain']}): {t['description']}")
        return

    # ── 环境检查 ──
    if args.check_env:
        missing = check_prerequisites()
        if missing:
            print("❌ 环境不完整:")
            for m in missing:
                print(f"  - {m}")
        else:
            print("✅ 环境完整，可运行")
        return

    # ── 配置 ──
    setup_logging(args.verbose)
    logger = logging.getLogger("main")

    # LLM 提供商
    if args.provider:
        set_provider(args.provider)

    provider = get_provider()
    logger.info("V6 5-Agent Pipeline 启动")
    logger.info("LLM: %s / %s", provider.provider_name, provider.model_name)
    logger.info("模式: %s", args.mode)

    # ── 确定用例 ──
    raw_input = ""
    expected_physics = None

    if args.case:
        if args.case in PREDEFINED_CASES:
            case = PREDEFINED_CASES[args.case]
            raw_input = case["raw_input"]
            expected_physics = case.get("expected_physics")
            logger.info("用例: %s", args.case)
        else:
            raw_input = args.case
            logger.info("自定义需求: %s", raw_input[:100])
    elif args.mode == "experiment":
        # 默认用例
        case = PREDEFINED_CASES["rc_filter"]
        raw_input = case["raw_input"]
        expected_physics = case.get("expected_physics")
        logger.info("默认用例: rc_filter")

    if not raw_input:
        print("请输入系统需求描述:")
        raw_input = sys.stdin.readline().strip()
        if not raw_input:
            print("错误: 需求不能为空")
            sys.exit(1)

    # ── 创建输出目录 ──
    run_dir = make_run_dir(args.output_dir)
    logger.info("输出目录: %s", run_dir)

    # ── 构建初始状态 ──
    reset_token_stats()
    initial_state: PipelineState = {
        "raw_input": raw_input,
        "mode": args.mode,
        "temperature": args.temperature,
        "max_retries": args.max_retries,
        "max_rejects": args.max_rejects,
        "run_dir": str(run_dir),
        "node_status": {},
        "reject_count_per_node": {},
        "dialogue_history": [],
        "timing": {},
        "quality_checks": {},
        "repair_log": [],
        "expected_physics": expected_physics,
        "circuit_breaker_triggered": False,
        "breaker_details": "",
    }

    # ── 构建并运行流水线 ──
    logger.info("构建 V6 流水线...")
    graph = build_pipeline()

    config = {"configurable": {"thread_id": f"v6_{run_dir.name}"}}

    logger.info("开始执行 V6 流水线...\n")
    try:
        final_state = graph.invoke(initial_state, config)
    except KeyboardInterrupt:
        logger.info("\n用户中断")
        sys.exit(0)
    except Exception as e:
        logger.error("流水线执行异常: %s", e, exc_info=args.verbose)
        sys.exit(1)

    # ── 输出结果 ──
    print("\n" + "=" * 60)
    print("V6 流水线执行完成")
    print("=" * 60)

    # 状态摘要
    ns = final_state.get("node_status", {})
    for node in ["node1", "node2", "node3"]:
        status = ns.get(node, "pending")
        icon = "✅" if status == "approved" else "❌" if status == "rejected" else "⏳"
        print(f"  {icon} {node}: {status}")

    # 质量检查
    qc = final_state.get("quality_checks", {})
    print("\n质量检查:")
    for check_name, check_data in qc.items():
        passed = check_data.get("passed", False)
        icon = "✅" if passed else "❌"
        issues_count = len(check_data.get("issues", []))
        print(f"  {icon} {check_name}: {'通过' if passed else '失败'} ({issues_count} 个问题)")

    # 仿真结果
    mo = final_state.get("mo", {})
    if mo:
        print(f"\n仿真: {'✅ 成功' if mo.get('success') else '❌ 失败'} "
              f"(尝试 {mo.get('attempts', 0)} 次)")
        if mo.get("plot_path"):
            print(f"  曲线图: {mo['plot_path']}")
        if mo.get("csv_path"):
            print(f"  数据: {mo['csv_path']}")

    # V6 根因分析
    if mo.get("root_cause"):
        print(f"\n根因分析: {mo['root_cause']}")
        if mo.get("root_cause_detail"):
            print(f"  详情: {mo['root_cause_detail']}")

    # Token 统计
    stats = get_token_stats()
    print(f"\nToken 消耗:")
    print(f"  API 调用: {stats.get('api_calls', 0)} 次")
    print(f"  Prompt tokens: {stats.get('prompt_tokens', 0):,}")
    print(f"  Completion tokens: {stats.get('completion_tokens', 0):,}")
    print(f"  Total tokens: {stats.get('total_tokens', 0):,}")

    # 耗时
    timing = final_state.get("timing", {})
    total_time = sum(timing.values())
    print(f"\n总耗时: {total_time:.1f}s")
    for node_name, elapsed in sorted(timing.items()):
        print(f"  {node_name}: {elapsed:.1f}s")

    # 输出文件
    summary = final_state.get("summary", {})
    if summary.get("file_path"):
        print(f"\n总结报告: {summary['file_path']}")

    print(f"\n所有文件: {run_dir}")


if __name__ == "__main__":
    main()
