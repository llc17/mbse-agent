"""
V5 自动化测试 — 模拟 Streamlit 用户操作，跑通全链路。
用法: python test_run.py
"""

import json
import sys
import os
import uuid
from pathlib import Path

# Windows UTF-8
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

os.chdir(Path(__file__).parent.parent)
_week6 = Path(__file__).parent
if str(_week6) not in sys.path:
    sys.path.insert(0, str(_week6))

from src.pipeline import build_pipeline
from src.utils import make_run_dir
from langgraph.types import Command


def run_full_test():
    """模拟用户：输入需求 → 回答澄清 → 一路确认 → 看结果。"""
    print("=" * 60)
    print("V5 Streamlit 模式自动化测试")
    print("=" * 60)

    # ─ 准备 ─
    graph = build_pipeline()
    thread_id = str(uuid.uuid4())[:8]
    config = {"configurable": {"thread_id": thread_id}, "recursion_limit": 100}

    run_dir = make_run_dir(str(Path("week6/outputs").resolve()))
    #raw_input = "一个 RC 低通滤波器，截止频率 1kHz"
    raw_input = "一个双房间热传导模型，房间A初始温度20°C，房间B初始30°C，中间的墙热阻为0.01 K/W"

    initial_state = {
        "raw_input": raw_input,
        "req": None, "sysml": None, "mo": None, "summary": None,
        "node_status": {"node1": "pending", "node2": "pending", "node3": "pending", "node4": "pending"},
        "human_feedback": "",
        "reject_count_per_node": {},
        "temperature": 0.3,
        "max_retries": 5,
        "max_rejects": 3,
        "dialogue_history": [],
        "timing": {},
        "run_dir": str(run_dir),
        "mode": "streamlit",
        "quality_checks": {},
        "repair_log": [],
        "physics_feedback": "",
        "expected_physics": None,
    }

    step = 0
    max_steps = 30

    print(f"\n[输入] {raw_input}")
    print(f"[输出目录] {run_dir}\n")

    # 第一步 invoke
    state = None
    try:
        state = graph.invoke(initial_state, config)
        step += 1
        print(f"[步骤 {step}] graph.invoke(initial_state) 完成")
    except Exception as e:
        print(f"ERROR 初始 invoke 失败: {e}")
        return

    while step < max_steps:
        snapshot = graph.get_state(config)

        if snapshot is None:
            print(f"[步骤 {step}] ERROR snapshot 为 None")
            break

        values = snapshot.values or {}
        interrupts = snapshot.interrupts

        ns = values.get("node_status", {})
        print(f"\n-- 状态 --")
        print(f"  node1={ns.get('node1')}  node2={ns.get('node2')}  node3={ns.get('node3')}  node4={ns.get('node4')}")
        print(f"  中断数: {len(interrupts) if interrupts else 0}")

        # 检查完成
        if not interrupts:
            has_summary = bool(values.get("summary"))
            if ns.get("node4") == "approved" or has_summary:
                print(f"\n*** 全流程完成! ***")
                _print_result(values)
                return
            else:
                print(f"WARN 没有中断但也没完成。values keys: {list(values.keys())}")
                import time
                time.sleep(2)
                continue
                break

        # 处理中断
        for intr in interrupts:
            data = intr.value
            node = data.get("node", "")
            msg = data.get("message", "")
            payload = data.get("data", {})

            print(f"\n   中断类型: {node}")
            print(f"   消息: {msg[:100]}...")

            decision = None

            if node == "node1_clarify":
                round_num = payload.get("round", 1)
                question = payload.get("question", "")
                print(f"   第 {round_num} 轮澄清: {question[:200]}")

                # 模拟回答
                if round_num <= 2:
                    answer = "电压源激励，幅值 5V"
                    print(f"   → 模拟回答: {answer}")
                else:
                    answer = "不需要补充，用已有信息即可。"
                    print(f"   → 跳过")
                decision = {"answer": answer}

            elif node == "node1":
                ct = payload.get("component_type", "?")
                params = payload.get("parameters", {})
                print(f"   组件类型: {ct}, 参数: {params}")
                decision = {"action": "approve"}
                print(f"   → 确认")

            elif node == "node2":
                fp = payload.get("file_path", "?")
                print(f"   SysML 文件: {fp}")
                decision = {"action": "approve"}
                print(f"   → 确认")

            elif node == "node3":
                success = payload.get("success")
                attempts = payload.get("attempts", "?")
                repair_count = payload.get("repair_count", 0)
                sim_goal = payload.get("sim_goal", "?")
                print(f"   {sim_goal}")
                print(f"   仿真: {'成功' if success else '失败'}, 尝试: {attempts}, 修复: {repair_count}")
                # 展示 CSV 数据（翻译列名）
                csv_path = payload.get("csv_path")
                if csv_path and Path(csv_path).exists():
                    try:
                        import csv
                        with open(csv_path, "r") as _f:
                            _reader = csv.reader(_f)
                            _rows = list(_reader)
                        if len(_rows) > 1:
                            _header = _rows[0]
                            _last = _rows[-1]
                            # 简单翻译
                            _tr = {"time": "时间", "capacitor": "电容", "resistor": "电阻",
                                   "inductor": "电感", "mass": "质量", "spring": "弹簧",
                                   "damper": "阻尼", "room": "房间"}
                            _readable = []
                            for h in _header:
                                for k, v in _tr.items():
                                    if k in h.lower():
                                        h = v + " " + h.split(".")[-1]
                                        break
                                _readable.append(h)
                            for _h, _v in zip(_readable, _last):
                                try:
                                    print(f"   {_h}: {float(_v):.4g}")
                                except ValueError:
                                    pass
                    except Exception:
                        pass
                decision = {"action": "approve"}
                print(f"   → 确认")

            else:
                print(f"   ERROR 未知中断类型: {node}")
                decision = {"action": "approve"}

            # 恢复执行
            step += 1
            print(f"\n[步骤 {step}] graph.invoke(Command(resume={decision})) ...")
            try:
                state = graph.invoke(Command(resume=decision), config)
                print(f"   → 执行完成")
            except Exception as e:
                print(f"   ERROR 执行失败: {e}")
                import traceback
                traceback.print_exc()
                # 改策略重试
                if node == "node1_clarify":
                    # 可能 JSON 解析失败，再试
                    decision2 = {"answer": "电压源 5V，仿真时间 0.1 秒"}
                    try:
                        state = graph.invoke(Command(resume=decision2), config)
                        print(f"   → 重试成功")
                    except Exception as e2:
                        print(f"   ERROR 重试也失败: {e2}")
                        return
                else:
                    return


def _print_result(values):
    mo = values.get("mo", {})
    print(f"\n仿真: {'✅' if mo.get('success') else '❌'}")
    summary = values.get("summary", {})
    if summary:
        print(f"总结: {summary.get('summary_text', '')[:300] if summary.get('summary_text') else '无'}")

    timing = values.get("timing", {})
    if timing:
        print(f"\n耗时:")
        for k, v in timing.items():
            print(f"  {k}: {v:.1f}s")


if __name__ == "__main__":
    run_full_test()
