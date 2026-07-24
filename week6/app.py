"""
MBSE+AI Streamlit Web UI — V5

把 V4 的命令行 HITL 交互替换为 Streamlit 网页界面。
核心思路: LangGraph checkpoint 存状态 + st.session_state 驱分步执行。

每次按钮点击 = 一次 script re-run:
  读取 checkpoint → invoke(Command(resume=...)) → 跑到下一个 interrupt → 存档 → 画页面

用法:
    cd D:/mbse/week6
    streamlit run app.py
"""

import csv as _csv
import json
import logging
import os
import sys
import time as _t
import uuid
from datetime import datetime
from pathlib import Path

# ============================================================================
# CWD 修正 — 确保所有相对路径从项目根目录解析
# ============================================================================
os.chdir(Path(__file__).parent.parent)

# ============================================================================
# Python 路径 — week6/src/ 包含 V5 的所有后端代码
# ============================================================================
_week6_src = Path(__file__).parent / "src"
if str(_week6_src.parent) not in sys.path:
    sys.path.insert(0, str(_week6_src.parent))

import streamlit as st
from langgraph.types import Command

from src.llm_client import get_available_providers, set_provider, get_token_stats, reset_token_stats
from src.pipeline import build_pipeline
from src.utils import make_run_dir


# ============================================================================
# 工具: Modelica 变量名 → 可读中文标签
# ============================================================================

def _translate_varname(name: str) -> str:
    """将 Modelica 内部变量名翻译为可读的中文标签。"""
    if name in ("time", "Time"):
        return "时间 [s]"

    t = name.lower().replace(".", " ")
    # 电路类
    t = t.replace("resistor", "电阻").replace("capacitor", "电容")
    t = t.replace("inductor", "电感").replace("opamp", "运放").replace("ground", "地")
    t = t.replace("voltage", "电压源").replace("current", "电流源").replace("source", "源")
    t = t.replace("sine", "正弦").replace("pulse", "脉冲").replace("constant", "恒定")
    # 机械类
    t = t.replace("mass", "质量块").replace("spring", "弹簧")
    t = t.replace("damper", "阻尼器").replace("fixed", "固定端").replace("force", "力")
    # 热学类
    t = t.replace("room", "房间").replace("wall", "墙").replace("heater", "加热器")
    t = t.replace("thermal", "热").replace("conductor", "导体").replace("capacitance", "热容")
    # 单位标注
    if " v" in t or t.endswith("v"):
        t += " [V]"
    elif " i" in t or t.endswith("i"):
        t += " [A]"
    elif " s" in t or t.endswith("s"):
        t += " [m]"
    elif " t" in t or t.endswith("t"):
        t += " [K]"
    elif " f" in t or t.endswith("f"):
        t += " [N]"
    return t.strip()

# ============================================================================
# 页面配置
# ============================================================================
st.set_page_config(
    layout="wide",
    page_title="MBSE+AI V5",
    page_icon="🔧",
    initial_sidebar_state="expanded",
)


# ============================================================================
# Session State 初始化（只在首次 load 时执行）
# ============================================================================

def _init_session():
    """初始化 session state。st.rerun() 后仍保留。"""
    st.session_state.initialized = True
    st.session_state.graph = build_pipeline()
    # SqliteSaver 开关（注释掉用 MemorySaver）
    # from langgraph.checkpoint.sqlite import SqliteSaver
    # st.session_state.graph.checkpointer = SqliteSaver.from_conn_string("week6/checkpoints.db")
    st.session_state.thread_id = str(uuid.uuid4())[:8]
    st.session_state.config = {
        "configurable": {"thread_id": st.session_state.thread_id},
        "recursion_limit": 100,
    }
    st.session_state.phase = "input"       # input | running | executing | done | error
    st.session_state.run_dir = None
    st.session_state.error_msg = None
    st.session_state.state_values = None
    st.session_state.node_status = {}
    st.session_state.provider_name = "deepseek"
    # 延迟执行: 按钮设这个标志，rerun 后主流程执行
    st.session_state.pending_resume = None
    st.session_state.pending_initial = None
    st.session_state.current_step = ""     # 当前正在执行什么


if "initialized" not in st.session_state:
    _init_session()


# ============================================================================
# 工具函数
# ============================================================================

def _build_initial_state(raw_input: str, temperature: float) -> dict:
    """构造 PipelineState 初始值。"""
    run_dir = make_run_dir(str(Path("week6/outputs").resolve()))
    return {
        "raw_input": raw_input,
        "req": None, "sysml": None, "mo": None, "summary": None,
        "node_status": {
            "node1": "pending", "node2": "pending",
            "node3": "pending", "node4": "pending",
        },
        "human_feedback": "",
        "reject_count_per_node": {},
        "temperature": temperature,
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


def _get_snapshot():
    """获取当前 checkpoint 快照。"""
    return st.session_state.graph.get_state(st.session_state.config)


def _do_invoke(callable, *args):
    """执行 graph.invoke() 并用 st.status() 包装，提供进度反馈。"""
    with st.status("⏳ 流水线执行中...", expanded=True) as status:
        try:
            result = callable(*args)
            status.update(label="✅ 执行完成", state="complete")
            return result
        except Exception as e:
            status.update(label=f"❌ 执行失败: {e}", state="error")
            raise


def _reset():
    """清空 session state 回到初始状态。"""
    for key in list(st.session_state.keys()):
        del st.session_state[key]
    _init_session()


# ============================================================================
# 延迟执行: 按钮回调只设标志，不直接调用 graph.invoke()
# ============================================================================

def _schedule_resume(decision: dict, step_label: str):
    """按钮回调: 设 pending 标志，rerun 后由主流程执行。"""
    st.session_state.pending_resume = decision
    st.session_state.current_step = step_label


def _schedule_initial(raw_input: str, temperature: float, provider_name: str):
    """初始运行按钮回调。"""
    st.session_state.pending_initial = (raw_input, temperature, provider_name)
    st.session_state.current_step = "正在精炼需求..."


# ============================================================================
# 主流程: 在渲染 UI 之前先处理延迟执行
# ============================================================================

def _execute_pending():
    """如果有 pending 操作，立即执行（阻塞）。执行完后 rerun 展示结果。"""
    # 先处理 pending_initial
    if st.session_state.pending_initial:
        raw_input, temperature, provider_name = st.session_state.pending_initial
        st.session_state.pending_initial = None
        st.session_state.phase = "running"
        st.session_state.error_msg = None

        set_provider(provider_name)
        st.session_state.provider_name = provider_name

        initial_state = _build_initial_state(raw_input, temperature)
        st.session_state.run_dir = initial_state["run_dir"]

        _do_invoke(
            st.session_state.graph.invoke, initial_state, st.session_state.config
        )
        st.rerun()

    # 再处理 pending_resume
    if st.session_state.pending_resume is not None:
        decision = st.session_state.pending_resume
        st.session_state.pending_resume = None

        _do_invoke(
            st.session_state.graph.invoke,
            Command(resume=decision),
            st.session_state.config,
        )
        st.rerun()


_execute_pending()


# ============================================================================
# 侧栏: 输入控制
# ============================================================================

with st.sidebar:
    st.title("⚙️ 配置")

    raw_input = st.text_area(
        "📝 系统需求",
        placeholder="例: 做一个 1kHz 截止频率的 RC 低通滤波器",
        height=120,
        key="input_text",
    )

    temperature = st.slider(
        "🌡️ Temperature", 0.0, 1.0, 0.3, 0.05, key="temp_slider",
        help="越低越确定，越高越随机",
    )

    available = get_available_providers()
    provider_name = st.selectbox(
        "🤖 LLM 提供商",
        list(available.keys()),
        key="provider_select",
    )
    if available:
        st.caption(f"模型: {available[provider_name]}")

    st.divider()

    run_disabled = st.session_state.phase == "running"
    if st.button("🚀 开始运行", type="primary", use_container_width=True, disabled=run_disabled):
        if not st.session_state.input_text.strip():
            st.error("请输入系统需求")
        else:
            _schedule_initial(st.session_state.input_text, temperature, provider_name)
            st.rerun()

    if st.session_state.phase in ("done", "error"):
        if st.button("🔄 新任务", use_container_width=True):
            _reset()
            st.rerun()

    if st.session_state.phase == "running":
        if st.button("🛑 放弃当前任务", use_container_width=True):
            _reset()
            st.rerun()


# ============================================================================
# 主区域
# ============================================================================

st.title("🔧 MBSE+AI 自动化闭环系统")
st.caption("V5 — Streamlit Web UI  |  自然语言需求 → SysML 模型 → Modelica 仿真 → 物理验证")

# ── 输入阶段 ──
if st.session_state.phase == "input":
    st.info("👈 请在左侧输入系统需求，然后点击「开始运行」")
    with st.expander("📋 示例需求（点击展开）"):
        st.markdown("""
        - **RC 低通滤波器**: 做一个 1kHz 截止频率的 RC 低通滤波器
        - **RLC 带通滤波器**: 设计一个 RLC 带通滤波器，中心频率 10kHz，带宽 2kHz
        - **双房间热传导**: 两个房间的热传导模型，房间A初始20°C，房间B初始30°C
        - **运放放大电路**: 设计一个同相比例运算放大器，增益10倍
        """)

# ── 运行中 ──
elif st.session_state.phase == "running":
    snapshot = _get_snapshot()

    if snapshot is None:
        st.error("无法获取流水线状态，请点击「放弃当前任务」重试")
        st.stop()

    values = snapshot.values or {}
    st.session_state.node_status = values.get("node_status", {})
    interrupts = snapshot.interrupts

    # ── 4 阶段进度条 ──
    phases = [
        ("node1", "需求精炼"),
        ("node2", "SysML 生成"),
        ("node3", "仿真验证"),
        ("node4", "总结报告"),
    ]

    cols = st.columns(4)
    for i, (node_key, label) in enumerate(phases):
        status = st.session_state.node_status.get(node_key, "pending")
        with cols[i]:
            if status == "approved":
                st.success(f"✅ {label}")
            elif status in ("rejected",):
                st.warning(f"🔄 {label}")
            else:
                st.info(f"⬜ {label}")

    st.divider()

    # ── 中断处理 ──
    if interrupts:
        for intr in interrupts:
            data = intr.value
            node = data.get("node", "")
            message = data.get("message", "")
            payload = data.get("data", {})

            # 简短标题（不用完整 message，避免太长的 subheader）
            _short_labels = {
                "node1_clarify": "🤔 需求澄清",
                "node1": "📋 节点1完成 — 请确认结构化需求",
                "node2": "📐 节点2完成 — 请确认 SysML 模型",
                "node3": "🔬 节点3仿真完成 — 请确认仿真结果",
            }
            st.subheader(_short_labels.get(node, f"⏸️  {message}"))

            # --- 节点1 澄清对话 ---
            if node == "node1_clarify":
                round_num = payload.get("round", 1)
                question_text = payload.get("question", "").strip()
                missing = payload.get("missing_fields", [])

                # 问题为空 → 强制提取需求，不再问
                if not question_text or len(question_text) < 10:
                    _schedule_resume({"answer": "不需要补充，用已有信息即可。"}, "强制跳过空问题")
                    st.rerun()

                with st.container(border=True):
                    st.write(f"### 🤔 需求澄清 — 第 {round_num} 轮")

                    # 缺失字段
                    if missing:
                        st.caption(f"⚠️ 缺失信息: {'、'.join(missing)}")

                    # 问题正文 — 用 st.markdown 直接渲染（不用 HTML 包裹）
                    st.markdown(question_text)

                    st.divider()

                    # 两个回答框
                    st.caption("请分别回答：")

                    cq1, cq2 = st.columns(2)
                    with cq1:
                        answer1 = st.text_area(
                            "回答 ①",
                            key=f"clarify_a1_{round_num}",
                            placeholder="例如: A",
                            height=60,
                        )
                    with cq2:
                        answer2 = st.text_area(
                            "回答 ②",
                            key=f"clarify_a2_{round_num}",
                            placeholder="例如: 无源RC",
                            height=60,
                        )

                    c1, c2 = st.columns(2)
                    with c1:
                        if st.button("📤 提交回答", key=f"submit_clarify_{round_num}", use_container_width=True):
                            combined = answer1.strip()
                            if answer2.strip():
                                combined += "; " + answer2.strip()
                            _schedule_resume({"answer": combined}, "需求澄清回答")
                            st.rerun()
                    with c2:
                        if st.button("⏭️ 跳过（用现有信息）", key=f"skip_clarify_{round_num}", use_container_width=True):
                            _schedule_resume({"answer": "不需要补充，用已有信息即可。"}, "跳过澄清")
                            st.rerun()

            # --- 节点1 HITL：需求确认 ---
            elif node == "node1":
                _history = values.get("dialogue_history", [])

                with st.container(border=True):
                    col_a, col_b = st.columns(2)
                    with col_a:
                        st.write("**组件类型**")
                        st.code(payload.get("component_type", "?"))
                        st.write("**参数**")
                        st.json(payload.get("parameters", {}))
                    with col_b:
                        st.write("**拓扑**")
                        st.code(payload.get("topology", "?"))
                        st.write("**约束**")
                        st.write(payload.get("constraints", []))
                        st.caption(f"精炼轮数: {payload.get('clarification_rounds', 0)}")

                    # 对话历史
                    if _history:
                        with st.expander("📜 需求精炼对话记录"):
                            for _msg in _history:
                                _role = _msg.get("role", "?")
                                _icon = "🧑" if _role == "user" else "🤖"
                                _content = _msg.get("content", "")
                                if len(_content) > 500:
                                    _content = _content[:500] + "..."
                                st.caption(f"{_icon} {_content}")

                    fb = st.text_input(
                        "反馈（打回时填写）",
                        key="feedback_node1",
                        placeholder="描述哪里需要修改...",
                    )

                    c1, c2 = st.columns(2)
                    with c1:
                        if st.button("✅ 确认，继续", key="approve_node1", use_container_width=True):
                            _schedule_resume({"action": "approve"}, "需求确认→生成SysML")
                            st.rerun()
                    with c2:
                        if st.button("🔄 打回修改", key="reject_node1", use_container_width=True):
                            _schedule_resume({"action": "reject", "feedback": fb}, "打回需求重做")
                            st.rerun()

            # --- 节点2 HITL：SysML 确认 ---
            elif node == "node2":
                with st.container(border=True):
                    st.write(f"**SysML 文件:** `{payload.get('file_path', '?')}`")
                    st.write(f"**生成尝试次数:** {payload.get('attempts', '?')}")
                    if payload.get("errors"):
                        st.warning(f"语法警告: {payload['errors']}")

                    st.info("💡 用 Eclipse 打开 .sysml 文件可查看模型图")

                    file_path = payload.get("file_path")
                    if file_path and Path(file_path).exists():
                        with st.expander("📄 预览 SysML 代码"):
                            st.code(Path(file_path).read_text(encoding="utf-8"), language="sysml")

                    fb = st.text_input(
                        "反馈（打回时填写）",
                        key="feedback_node2",
                        placeholder="描述需要修改的内容...",
                    )

                    c1, c2 = st.columns(2)
                    with c1:
                        if st.button("✅ 确认，继续仿真", key="approve_node2", use_container_width=True):
                            _schedule_resume({"action": "approve"}, "SysML确认→Modelica仿真")
                            st.rerun()
                    with c2:
                        if st.button("🔄 打回重生成", key="reject_node2", use_container_width=True):
                            _schedule_resume({"action": "reject", "feedback": fb}, "打回SysML重生成")
                            st.rerun()

            # --- 节点3 HITL：仿真确认 ---
            elif node == "node3":
                sim_goal = payload.get("sim_goal", "")
                component_type = payload.get("component_type", "?")
                rp = payload.get("parameters", {})
                mo_code = payload.get("modelica_code", "")

                with st.container(border=True):
                    # ---- V5: 仿真目标（物理意义清晰） ----
                    if sim_goal:
                        st.markdown(sim_goal)
                    st.divider()

                    # 系统参数卡片
                    if rp:
                        st.write("**系统设计参数**")
                        param_cols = st.columns(min(len(rp), 4))
                        for i, (k, v) in enumerate(rp.items()):
                            with param_cols[i % 4]:
                                st.metric(k, f"{v:.4g}" if isinstance(v, float) else str(v))

                    # ---- 仿真结果摘要 ----
                    success = payload.get("success")
                    attempts = payload.get("attempts", "?")
                    repair_count = payload.get("repair_count", 0)

                    st.divider()
                    st.write(f"**仿真状态:** {'✅ 成功' if success else '❌ 失败'}  |  "
                             f"**尝试次数:** {attempts}  |  **修复次数:** {repair_count}")

                    errors = payload.get("errors", [])
                    if errors:
                        with st.expander("⚠️ 编译/仿真错误详情"):
                            for e in errors:
                                st.text(e[:400])

                    # ---- CSV 数据表格（可读列名） ----
                    csv_path = payload.get("csv_path")
                    if csv_path and Path(csv_path).exists():
                        try:
                            with open(csv_path, "r") as _f:
                                _reader = _csv.reader(_f)
                                _rows = list(_reader)
                            if len(_rows) > 1:
                                _header = _rows[0]
                                _last = _rows[-1]

                                # 翻译列名
                                _translated = [f"**{_translate_varname(h)}**" for h in _header]

                                # 只显示首行 + 最后 5 行
                                _display_rows = [_rows[0]] + _rows[-5:]
                                st.write(f"**仿真数据**（共 {len(_rows)-1} 个时间点，显示首尾）：")

                                # Markdown 表格
                                _md = "| " + " | ".join(_translated) + " |\n"
                                _md += "|" + "|".join(["------"] * len(_translated)) + "|\n"
                                for _row in _display_rows:
                                    _cells = []
                                    for _c in _row:
                                        try:
                                            _cells.append(f"{float(_c):.4g}")
                                        except (ValueError, TypeError):
                                            _cells.append(_c[:12])
                                    _md += "| " + " | ".join(_cells) + " |\n"
                                st.markdown(_md)

                                # 关键指标：首个时间点和最后一个时间点对比
                                if len(_rows) > 5:
                                    with st.expander("📊 关键物理量变化（初值 → 终值）"):
                                        _key_cols = st.columns(3)
                                        _ki = 0
                                        for i, (_h, _first, _last) in enumerate(
                                            zip(_header, _rows[1], _rows[-1])
                                        ):
                                            try:
                                                _fv, _lv = float(_first), float(_last)
                                                _delta = _lv - _fv
                                                _label = _translate_varname(_h)
                                                with _key_cols[_ki % 3]:
                                                    st.metric(_label, f"{_lv:.4g}", f"{_delta:+.4g}")
                                                _ki += 1
                                            except (ValueError, TypeError):
                                                pass
                        except Exception:
                            st.caption("CSV 数据解析失败")

                    # ---- 仿真曲线图 ----
                    plot_path = payload.get("plot_path")
                    if plot_path and Path(plot_path).exists():
                        curve_desc = f"{sim_goal}\n横轴: 时间 [s]"
                        st.image(str(plot_path),
                                 caption=curve_desc,
                                 use_container_width=True)
                    else:
                        st.caption("📈 仿真曲线未生成（可能仿真失败或 MAT→CSV 转换异常）")

                    # ---- CSV 下载 + Modelica 代码预览 ----
                    _col_dl, _col_code = st.columns(2)
                    with _col_dl:
                        if csv_path and Path(csv_path).exists():
                            with open(csv_path, "rb") as f:
                                st.download_button(
                                    "📥 下载仿真数据 (CSV)",
                                    f, file_name=Path(csv_path).name, key="dl_csv",
                                    use_container_width=True,
                                )
                    with _col_code:
                        if mo_code:
                            with st.expander("📄 Modelica 仿真代码"):
                                st.code(mo_code[:2000], language="modelica")

                    # ---- 物理预检 ----
                    qp = payload.get("quality_preview", {})
                    if qp.get("physics_passed") is not None:
                        dev = qp.get("physics_deviation", "?")
                        if qp["physics_passed"]:
                            st.success(f"物理预检通过 (偏差 {dev}%)")
                        else:
                            st.warning(f"物理预检偏差 {dev}%")

                    col1, col2, col3 = st.columns(3)
                    with col1:
                        if st.button("✅ 确认，继续", key="approve_node3", use_container_width=True):
                            _schedule_resume({"action": "approve"}, "仿真确认→物理验证")
                            st.rerun()
                    with col2:
                        if st.button("🔄 重做 Modelica", key="reject_modelica", use_container_width=True):
                            _schedule_resume({"action": "reject", "feedback": "用户打回 Modelica"}, "重做Modelica仿真")
                            st.rerun()
                    with col3:
                        if st.button("🔙 重做 SysML", key="reject_sysml", use_container_width=True):
                            _schedule_resume({"action": "reject_sysml", "feedback": "用户打回 SysML"}, "打回重做SysML")
                            st.rerun()

    else:
        # 没有中断 → 检查是否完成
        ns = st.session_state.node_status
        has_summary = bool(values.get("summary"))
        if ns.get("node4") == "approved" or has_summary:
            st.session_state.phase = "done"
            st.session_state.state_values = values
            st.rerun()
        else:
            # 可能节点还在执行中
            st.info("⏳ 流水线执行中...")
            st.caption("如果长时间无变化，终端可能有编译/仿真日志在刷屏")
            st.caption(f"上次检查: {_t.strftime('%H:%M:%S')}")
            # 每 3 秒自动刷新
            st.caption("页面每 3 秒自动刷新")
            st.markdown(
                '<meta http-equiv="refresh" content="3">',
                unsafe_allow_html=True,
            )

# ── 完成 ──
elif st.session_state.phase == "done":
    state = st.session_state.state_values or {}

    st.success("🎉 全流程完成！")

    mo = state.get("mo", {})
    timing = state.get("timing", {})
    quality = state.get("quality_checks", {})

    col1, col2, col3, col4 = st.columns(4)
    with col1:
        st.metric("仿真结果", "✅ 成功" if mo.get("success") else "❌ 失败")
    with col2:
        st.metric("节点3 尝试", mo.get("attempts", "?"))
    with col3:
        physics = quality.get("physics_validate", {})
        dev = physics.get("deviation_percent", "--")
        st.metric("物理验证偏差", f"{dev}%")
    with col4:
        token_stats = get_token_stats()
        st.metric("Token 消耗", f"{token_stats.get('total_tokens', 0):,}")

    if timing:
        with st.expander("⏱️ 耗时统计"):
            for k, v in sorted(timing.items(), key=lambda x: x[1], reverse=True):
                st.write(f"- {k}: {v:.1f}s")

    token_stats = get_token_stats()
    with st.expander("💰 Token 详情"):
        st.write(f"- 提供商: {token_stats.get('provider', '?')}")
        st.write(f"- 模型: {token_stats.get('model', '?')}")
        st.write(f"- Prompt tokens: {token_stats.get('prompt_tokens', 0):,}")
        st.write(f"- Completion tokens: {token_stats.get('completion_tokens', 0):,}")
        st.write(f"- 总 tokens: {token_stats.get('total_tokens', 0):,}")
        st.write(f"- API 调用次数: {token_stats.get('api_calls', 0)}")

    cross = quality.get("cross_validate", {})
    if cross:
        with st.expander("🔍 交叉校验报告"):
            st.json(cross)

    physics = quality.get("physics_validate", {})
    if physics:
        with st.expander("🔬 物理验证报告"):
            st.json(physics)

    run_dir = Path(state.get("run_dir", ""))
    if run_dir.exists():
        with st.expander("📁 产出文件"):
            for sub in sorted(run_dir.iterdir()):
                if sub.is_dir():
                    st.write(f"**{sub.name}/**")
                    for f in sorted(sub.iterdir()):
                        if f.is_file() and not f.name.startswith("run."):
                            size = f.stat().st_size
                            st.write(f"  - {f.name} ({size:,} bytes)")
                            if f.suffix in (".csv", ".png", ".sysml", ".mo", ".txt"):
                                with open(f, "rb") as fh:
                                    st.download_button(
                                        f"📥 下载 {f.name}",
                                        fh,
                                        file_name=f.name,
                                        key=f"dl_{f.name}",
                                    )

    summary = state.get("summary", {})
    if summary.get("text"):
        with st.expander("📊 总结报告"):
            st.markdown(summary["text"])

    if st.button("🔄 开始新任务", type="primary", use_container_width=True):
        reset_token_stats()
        _reset()
        st.rerun()

# ── 错误 ──
elif st.session_state.phase == "error":
    st.error("❌ 运行出错")
    st.code(st.session_state.error_msg)
    if st.button("🔄 重置", type="primary"):
        _reset()
        st.rerun()
