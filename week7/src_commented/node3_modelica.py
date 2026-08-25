"""
=============================================================================
node3_modelica.py — Agent ③ Modelica Agent（V6 LLM 选域 + 根因分析 + 审查版）
=============================================================================
定位: 流水线最复杂的节点——负责 Modelica 代码生成、编译、仿真、修复。

V4 → V6 变更:
  - generate_mo: LLM 选域 + Python 模板注入（替代纯 LLM 自由发挥）
  - repair_mo: 增加 LLM 根因分析（读 errors+req+sysml+modelica 三段上下文）
  - 新增审查阶段: _review_modelica_code() 在编译前检查代码质量
  - 子图结构保留（generate→compile→simulate→repair 循环）

Agent ③ 内部流程:
  Python 初筛候选模板 → LLM 确认选域 → Python 注入模板骨架
  → LLM 微调参数 → 审查 → 编译 → 仿真
  → (失败 → LLM 根因分析 → 针对性修复 / 打回上游)

根因分析:
  V4: 关键词匹配（"parameter"→node1, "connect"→node2）→ 经常判断错
  V6: LLM 综合分析错误日志 + req + sysml + modelica 三段上下文
        → 判断是需求缺参数、SysML拓扑错、还是Modelica代码错
        → 连续两次分析结果矛盾 → 标记转 HITL
=============================================================================
"""

import csv, io, json, logging, re, subprocess, time
from pathlib import Path

from langgraph.graph import StateGraph, START, END

from src.llm_client import chat, chat_with, user_msg
from src.agent_loop import parse_review_json, ReviewResult, ReviewIssue
from src.schemas import StructuredRequirement, ModelicaArtifact
from src.utils import load_prompt, clean_code_block, get_stop_time_for_domain
from src.modelica_templates import (
    get_candidate_templates,
    build_template_selection_prompt,
    inject_template,
)

logger = logging.getLogger("node3")


# ============================================================================
# V6: Modelica 代码审查（生成后、编译前）
# ============================================================================

def _review_modelica_code(mo_code: str, req: StructuredRequirement,
                          sysml_code: str, temperature: float,
                          max_rounds: int = 2) -> str:
    """
    V6: 生成 Modelica 代码后审查，发现问题则修正。

    这是 Agent ③ 的审查阶段 —— 在编译前检查代码质量，减少编译失败。
    审查用 chat_with("review", ...) → 支持智谱独立审查。

    流程:
      1. 构建审查 prompt（7 条检查项: 结构/import/组件/参数/拓扑/接地/传感器）
      2. 调用 LLM 审查 → 返回 {ok, score, issues[]}
      3. ok=True → 返回原代码
      4. ok=False + issues 非空 → 构建修正 prompt → LLM 修正 → 回到步骤 2
      5. 超轮上限 → 返回当前代码（断路器）

    为什么是 2 轮而不是 3 轮？
      Modelica 审查在编译前，编译后还有 repair 循环。这里 2 轮检查
      + 编译后 5 轮 repair → 总共最多 7 轮保障。
    """
    if not mo_code or len(mo_code) < 20:                               # 空代码或太短 → 跳过
        return mo_code

    params_str = "\n".join(f"  {k} = {v}" for k, v in req.parameters.items())

    for round_num in range(1, max_rounds + 1):
        # ── 构建审查 prompt ──
        review_prompt = (
            load_prompt("node3_review.txt")
            .replace("{component_type}", req.component_type)
            .replace("{parameters}", params_str)
            .replace("{topology}", req.topology)
            .replace("{sysml_code}", sysml_code[:2000])
            .replace("{modelica_code}", mo_code[:3000])
        )

        logger.info("节点3 V6: Modelica 审查 (第%s/%s轮)...", round_num, max_rounds)
        review_raw = chat_with("review", [user_msg(review_prompt)], temperature=0.1, max_tokens=1024)
        review = parse_review_json(review_raw)

        # ok → 通过，返回代码
        if review.ok:
            logger.info("节点3 V6: Modelica 审查通过 (score=%s)", review.score)
            return mo_code

        # 无具体问题 → 视为通过
        if not review.issues:
            logger.info("节点3 V6: 审查未通过但无具体问题，视为通过")
            return mo_code

        # 断路器: 达到上限 → 返回当前代码（不做无意义的更多修正）
        if round_num >= max_rounds:
            logger.warning("节点3 V6: Modelica 审查 %s 轮未通过，使用当前版本", max_rounds)
            return mo_code

        # ── 修正 ──
        issues_text = "\n".join(
            f"- [{i.severity}] {i.description} → {i.suggestion}"
            for i in review.issues
        )
        revise_prompt = (
            f"## 角色\n你是 Modelica 代码修正专家。\n\n"
            f"## 当前代码（需要修正）\n```\n{mo_code}\n```\n\n"
            f"## 审查发现的问题（只修正这些问题）\n{issues_text}\n\n"
            f"## 组件信息\n- 类型: {req.component_type}\n- 参数:\n{params_str}\n\n"
            f"## SysML 参考拓扑\n```\n{sysml_code[:1500]}\n```\n\n"
            f"请修正问题，输出完整的 Modelica 代码。只输出代码。"
        )
        logger.info("节点3 V6: Modelica 修正 (第%s轮)...", round_num)
        try:
            mo_code = chat([user_msg(revise_prompt)], temperature=0.2, max_tokens=4096).strip()
            mo_code = clean_code_block(mo_code, "modelica")
        except Exception as e:
            logger.warning("节点3 V6: Modelica 修正失败 (%s)，保持原代码", e)

    return mo_code


# ============================================================================
# V4 遗留: _always_pass 模板（V6 不再需要，保留作文档）
# ============================================================================

def _always_pass(state: dict) -> dict:
    """V4: 每个子图节点返回时确保计数器字段不丢。
    V6: 节点已直接返回 node3_attempts/node3_step_ok，不再调用此函数。"""
    return {
        "node3_attempts": state.get("node3_attempts", 0),
        "node3_step_ok": state.get("node3_step_ok", False),
    }


# ============================================================================
# 构建子图（编译→仿真→修复 循环）
# ============================================================================

def build_node3_subgraph() -> StateGraph:
    """构建 node3 子图: generate → compile → simulate → repair（循环）。"""
    builder = StateGraph(dict)

    builder.add_node("generate_mo", _generate_mo)
    builder.add_node("compile_mo", _compile_mo)
    builder.add_node("simulate_mo", _simulate_mo)
    builder.add_node("repair_mo", _repair_mo)

    builder.add_edge(START, "generate_mo")
    builder.add_edge("generate_mo", "compile_mo")

    builder.add_conditional_edges("compile_mo", _route_after_compile, {
        "simulate_mo": "simulate_mo",
        "repair_mo": "repair_mo",
        "end_fail": END,
    })
    builder.add_conditional_edges("simulate_mo", _route_after_simulate, {
        "end_success": END,
        "repair_mo": "repair_mo",
        "end_fail": END,
    })
    builder.add_conditional_edges("repair_mo", _route_after_repair, {
        "compile_mo": "compile_mo",
        "end_fail": END,
    })

    return builder.compile()


# ============================================================================
# V6: 生成 — LLM 选域 + 模板注入 + LLM 微调 + 审查
# ============================================================================

def _generate_mo(state: dict) -> dict:
    """
    V6 生成流程（4 步）:
      1. Python 初筛候选模板（关键词匹配，确定性）
      2. LLM 确认选域（从候选池中选最匹配的，解决域歧义）
      3. Python 注入模板骨架（参数填入，保证编译通过）
      4. LLM 微调参数（反算精确值、加注释）
      5. 审查（检查代码质量）
    """
    t0 = time.time()
    req_dict = state.get("req", {})
    sysml_dict = state.get("sysml", {})
    sysml_code = sysml_dict.get("sysml_code", "")
    temperature = state.get("temperature", 0.3)

    req = StructuredRequirement(**req_dict) if req_dict else StructuredRequirement(
        component_type="unknown", raw_input="")

    component_type = req.component_type or "unknown"
    params = req.parameters
    component_name = (req.component_name or
                      re.sub(r'[^a-zA-Z0-9_]', '_', component_type)[:30] or "MyModel")

    # ── 第 1 步: Python 初筛候选模板 ──
    # 这是确定性的关键词匹配——不依赖 LLM
    candidates = get_candidate_templates(component_type)
    logger.info("节点3 V6: 候选模板 %s (来自 '%s')", candidates, component_type)

    # ── 第 2 步: LLM 确认选域 ──
    # 只有 1 个候选 → 不需要 LLM，直接选择
    if len(candidates) == 1:
        selected_template = candidates[0]
        logger.info("节点3 V6: 仅1个候选，直接选择 '%s'", selected_template)
    else:
        # 多个候选 → LLM 看描述选最匹配的
        selection_prompt = build_template_selection_prompt(component_type, params, candidates)
        selection_result = chat([user_msg(selection_prompt)], temperature=0.0, max_tokens=128).strip()
        selected_template = _extract_template_name(selection_result, candidates)
        logger.info("节点3 V6: LLM 选择模板 '%s' (候选=%s)", selected_template, candidates)

    # ── 第 3 步: Python 注入模板 ──
    # 模板已经 OMC 验证过编译通过，只是参数值需要填入
    mo_code = inject_template(selected_template, params, component_name)

    # ── 第 4 步: LLM 微调参数 ──
    # LLM 可以反算精确值（如根据 f_c=1kHz、R=1kΩ → C=1/(2π×1000×1000)=1.5915e-7F）
    params_str = "\n".join(f"  {k} = {v}" for k, v in req.parameters.items())
    constraints_str = "\n".join(f"  - {c}" for c in req.constraints)

    refine_prompt = (
        f"## 角色\n你是 Modelica 仿真建模专家。\n\n"
        f"## 组件信息\n- 类型: {component_type}\n- 参数:\n{params_str}\n"
        f"- 拓扑: {req.topology}\n- 约束:\n{constraints_str}\n\n"
        f"## SysML 系统模型（参考拓扑）\n```\n{sysml_code[:2000]}\n```\n\n"
        f"## Modelica 模板骨架（已填充参数）\n```\n{mo_code}\n```\n\n"
        f"## 要求\n1. 检查参数值是否与需求一致\n"
        f"2. 如需精确定义（如根据截止频率反推电容值），可以微调\n"
        f"3. 添加必要注释\n4. 不要大幅改动模板连接结构（保证编译通过）\n"
        f"5. 只输出完整的 Modelica 代码，不要包含解释"
    )

    logger.info("节点3 V6: LLM 微调模板代码...")
    mo_code = chat([user_msg(refine_prompt)], temperature=temperature, max_tokens=4096).strip()
    mo_code = clean_code_block(mo_code, "modelica")

    # ── 第 5 步: Modelica 代码审查（编译前质量检查）──
    mo_code = _review_modelica_code(mo_code, req, sysml_code, temperature)

    model_name = _extract_model_name(mo_code) or component_name
    logger.info("节点3 V6 generate 完成, 模板=%s, 模型名=%s", selected_template, model_name)

    return {
        "mo": {
            "modelica_code": mo_code, "file_path": "", "csv_path": "",
            "plot_path": "", "attempts": 0, "errors": [], "success": False,
            "_template": selected_template,                                  # V6: 记录使用的模板名
        },
        "node3_attempts": 0,
        "node3_step_ok": False,
        "run_dir": state.get("run_dir", ""),
        "timing": {**state.get("timing", {}), "node3_generate": time.time() - t0},
    }


# ============================================================================
# 编译 & 仿真（V4 保留，逻辑不变）
# ============================================================================

def _compile_mo(state: dict) -> dict:
    t0 = time.time()
    mo_dict = state.get("mo", {})
    run_dir = Path(state.get("run_dir", ".")).resolve()
    modelica_dir = run_dir / "modelica"

    modelica_code = mo_dict.get("modelica_code", "")
    model_name = _extract_model_name(modelica_code) or "MyModel"

    modelica_dir.mkdir(parents=True, exist_ok=True)
    mo_path = modelica_dir / "model.mo"
    mo_path.write_text(modelica_code, encoding="utf-8")

    logger.info("节点3 compile: 编译 %s...", model_name)
    compile_ok, compile_err = _compile(str(mo_path), model_name)

    errors = list(mo_dict.get("errors", []))

    if not compile_ok:
        attempts = state.get("node3_attempts", 0) + 1
        logger.warning("节点3 compile 失败 (第%s次): %s", attempts, compile_err[:200])
        errors.append(f"[编译错误 #{attempts}] {compile_err[:500]}")
        return {
            "mo": {**mo_dict, "errors": errors, "attempts": attempts,
                   "file_path": str(mo_path.resolve()) if mo_path else ""},
            "node3_attempts": attempts, "node3_step_ok": False,
            "run_dir": state.get("run_dir", ""),
            "timing": {**state.get("timing", {}), "node3_compile": time.time() - t0},
        }

    logger.info("节点3 compile 成功")
    return {
        "mo": {**mo_dict, "errors": errors,
               "file_path": str(mo_path.resolve()) if mo_path else ""},
        "node3_attempts": state.get("node3_attempts", 0),
        "node3_step_ok": True,
        "run_dir": state.get("run_dir", ""),
        "timing": {**state.get("timing", {}), "node3_compile": time.time() - t0},
    }


def _simulate_mo(state: dict) -> dict:
    t0 = time.time()
    mo_dict = state.get("mo", {})
    run_dir = Path(state.get("run_dir", ".")).resolve()
    results_dir = run_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    modelica_code = mo_dict.get("modelica_code", "")
    model_name = _extract_model_name(modelica_code) or "MyModel"
    mo_path = str(run_dir / "modelica" / "model.mo")

    req = state.get("req", {})
    stop_time = get_stop_time_for_domain(req.get("component_type", ""))

    logger.info("节点3 simulate: 仿真 %s...", model_name)
    sim_ok, sim_err = _simulate(mo_path, model_name, results_dir, stop_time)

    errors = list(mo_dict.get("errors", []))

    if not sim_ok:
        attempts = state.get("node3_attempts", 0) + 1
        logger.warning("节点3 simulate 失败 (第%s次): %s", attempts, sim_err[:200])
        errors.append(f"[仿真错误 #{attempts}] {sim_err[:500]}")
        return {
            "mo": {**mo_dict, "errors": errors, "attempts": attempts},
            "node3_attempts": attempts, "node3_step_ok": False,
            "run_dir": state.get("run_dir", ""),
            "timing": {**state.get("timing", {}), "node3_simulate": time.time() - t0},
        }

    csv_path = results_dir / "simulation.csv"
    plot_path = results_dir / "simulation.png"

    if csv_path.exists():
        _plot_csv(str(csv_path), str(plot_path),
                  state.get("req", {}).get("component_type", "System"))

    logger.info("节点3 simulate 成功, PNG: %s", plot_path)

    # 保存修复日志
    repair_log = state.get("repair_log", [])
    if repair_log:
        log_path = results_dir / "repair_log.json"
        log_path.write_text(json.dumps(repair_log, ensure_ascii=False, indent=2), encoding="utf-8")

    return {
        "mo": {**mo_dict, "errors": errors, "success": True,
               "csv_path": str(csv_path.resolve()), "plot_path": str(plot_path.resolve())},
        "node3_attempts": state.get("node3_attempts", 0),
        "node3_step_ok": True,
        "run_dir": state.get("run_dir", ""),
        "timing": {**state.get("timing", {}), "node3_simulate": time.time() - t0},
    }


# ============================================================================
# V6: 修复 — LLM 根因分析 + 针对性修复
# ============================================================================

def _repair_mo(state: dict) -> dict:
    """
    V6 修复: LLM 根因分析 + 针对性修复。

    V4: 把错误日志贴回去让 LLM 重新生成（"对着错误改"）→ 经常治标不治本
    V6: LLM 分析错误的根因 → 判断是参数/连接/语法问题 → 针对性修复
         → 如果是缺参数/SysML拓扑错 → 标记打回上游节点

    根因分析的核心价值: 知道该打回哪个节点——不是所有错误都在 Modelica 层。
    """
    t0 = time.time()
    req_dict = state.get("req", {})
    sysml_dict = state.get("sysml", {})
    mo_dict = state.get("mo", {})
    sysml_code = sysml_dict.get("sysml_code", "")
    temperature = state.get("temperature", 0.3)
    attempts = state.get("node3_attempts", 0)

    req = StructuredRequirement(**req_dict) if req_dict else StructuredRequirement(
        component_type="unknown", raw_input="")

    errors = mo_dict.get("errors", [])
    code_before = mo_dict.get("modelica_code", "")
    errors_before = errors[-3:]

    # ── V6: LLM 根因分析（替代 V4 关键词匹配）──
    root_cause = _analyze_root_cause(errors, req, sysml_code, code_before)

    logger.info("节点3 V6 根因分析: category=%s, confidence=%s, detail=%s",
                root_cause.get("category"), root_cause.get("confidence"),
                root_cause.get("detail", "")[:100])

    # ── 根据根因决定修复策略 ──
    if root_cause.get("category") == "missing_parameters" and root_cause.get("confidence", 0) >= 0.7:
        # 缺参数 → 在 node3 内部无法修复 → 打回 node1
        logger.warning("节点3 V6: 根因=缺参数 → 标记打回 node1")
        new_attempts = attempts + 1
        feedback = f"[V6根因分析: 缺参数] {root_cause.get('detail', '')} {root_cause.get('suggestion', '')}"
        return {
            "mo": {**mo_dict, "root_cause": "missing_parameters",
                   "root_cause_detail": root_cause.get("detail", ""), "attempts": new_attempts},
            "node3_attempts": new_attempts, "node3_step_ok": False,
            "human_feedback": feedback,                                     # V6: 根因分析结果传给 node1
            "repair_log": list(state.get("repair_log", [])),
            "run_dir": state.get("run_dir", ""),
            "timing": {**state.get("timing", {}), "node3_repair": time.time() - t0},
        }

    if root_cause.get("category") == "sysml_topology" and root_cause.get("confidence", 0) >= 0.7:
        # SysML 拓扑问题 → 在 node3 内部无法修复 → 打回 node2
        logger.warning("节点3 V6: 根因=SysML拓扑 → 标记打回 node2")
        new_attempts = attempts + 1
        feedback = f"[V6根因分析: SysML拓扑问题] {root_cause.get('detail', '')} {root_cause.get('suggestion', '')}"
        return {
            "mo": {**mo_dict, "root_cause": "sysml_topology",
                   "root_cause_detail": root_cause.get("detail", ""), "attempts": new_attempts},
            "node3_attempts": new_attempts, "node3_step_ok": False,
            "human_feedback": feedback,                                     # V6: 根因分析结果传给 node2
            "repair_log": list(state.get("repair_log", [])),
            "run_dir": state.get("run_dir", ""),
            "timing": {**state.get("timing", {}), "node3_repair": time.time() - t0},
        }

    # ── 默认: Modelica 代码问题 → node3 内修复 ──
    params_str = "\n".join(f"  {k} = {v}" for k, v in req.parameters.items())
    constraints_str = "\n".join(f"  - {c}" for c in req.constraints)

    error_section = "## 编译/仿真错误日志\n```\n" + "\n".join(errors[-5:]) + "\n```"

    repair_prompt = (
        f"## 角色\n你是 Modelica 代码修复专家。\n\n"
        f"## 组件信息\n- 类型: {req.component_type}\n- 参数:\n{params_str}\n"
        f"- 拓扑: {req.topology}\n- 约束:\n{constraints_str}\n\n"
        f"## 当前 Modelica 代码（有错误）\n```\n{code_before[:3000]}\n```\n\n"
        f"{error_section}\n\n"
        f"## 根因分析结果\n- 错误类别: {root_cause.get('category', 'unknown')}\n"
        f"- 分析: {root_cause.get('detail', '')}\n"
        f"- 修复建议: {root_cause.get('suggestion', '')}\n\n"
        f"## SysML 参考（系统拓扑）\n```\n{sysml_code[:2000]}\n```\n\n"
        f"## 要求\n1. 根据根因分析的结果修复 Modelica 代码\n"
        f"2. 只改动与错误相关的部分，不要重写整个模型\n"
        f"3. 确保修复后的代码与 SysML 系统模型的拓扑一致\n"
        f"4. 只输出完整的 Modelica 代码，不要包含解释"
    )

    logger.info("节点3 V6 repair: 第%s次 LLM 修复...", attempts)
    mo_code = chat([user_msg(repair_prompt)], temperature=temperature, max_tokens=4096).strip()
    mo_code = clean_code_block(mo_code, "modelica")

    # ── 记录修复日志 ──
    repair_entry = {
        "attempt": attempts, "errors_before": errors_before,
        "root_cause": root_cause,
        "code_before_snippet": code_before[:200] if code_before else "",
        "code_after_snippet": mo_code[:200],
    }
    repair_log = list(state.get("repair_log", []))
    repair_log.append(repair_entry)

    return {
        "mo": {**mo_dict, "modelica_code": mo_code},
        "node3_attempts": attempts, "node3_step_ok": False,
        "repair_log": repair_log,
        "run_dir": state.get("run_dir", ""),
        "timing": {**state.get("timing", {}), "node3_repair": time.time() - t0},
    }


# ============================================================================
# V6: 根因分析（LLM 替代 V4 关键词匹配）
# ============================================================================

def _analyze_root_cause(errors: list[str], req: StructuredRequirement,
                        sysml_code: str, modelica_code: str) -> dict:
    """
    V6: LLM 分析编译/仿真失败的根因。

    与 V4 关键词匹配的关键区别:
      V4: 只看错误信息的第一行 → 关键词匹配 → 可能判断错
      V6: 同时看错误日志 + req + sysml + modelica 三段上下文 → 判断更准

    返回三类:
      modelica_code      — node3 内部修复（语法/库使用/单位问题）
      missing_parameters  — 打回 node1（需求本身缺参数）
      sysml_topology      — 打回 node2（SysML 拓扑/连接有误）
    """
    if not errors:
        return {"category": "unknown", "confidence": 0.5,
                "detail": "无错误信息", "suggestion": "重新生成 Modelica 代码"}

    error_text = "\n".join(errors[-5:])
    req_json = json.dumps({
        "component_type": req.component_type,
        "parameters": req.parameters,
        "topology": req.topology,
        "constraints": req.constraints,
    }, ensure_ascii=False, indent=2)

    analysis_prompt = (
        f"## 角色\n你是 Modelica 编译错误根因分析专家。\n\n"
        f"## 编译/仿真错误日志\n```\n{error_text}\n```\n\n"
        f"## 需求（req JSON）\n```\n{req_json}\n```\n\n"
        f"## SysML 系统模型\n```\n{sysml_code[:2000]}\n```\n\n"
        f"## Modelica 代码\n```\n{modelica_code[:2000]}\n```\n\n"
        f"## 分析要求\n判断根因属于哪一类:\n"
        f"1. missing_parameters: 需求缺少必要物理参数 → 打回 node1\n"
        f"2. sysml_topology: SysML 拓扑/连接有误 → 打回 node2\n"
        f"3. modelica_code: 需求和 SysML 都对，Modelica 代码本身有问题 → 在 node3 内修复\n\n"
        f"## 输出 JSON\n"
        f'{{"category": "modelica_code|missing_parameters|sysml_topology|unknown",\n'
        f' "confidence": 0.0-1.0, "detail": "分析说明", "suggestion": "修复建议"}}\n'
        f"只输出 JSON。"
    )

    try:
        result = chat([user_msg(analysis_prompt)], temperature=0.1, max_tokens=512)
        json_match = re.search(r"\{.*\}", result, re.DOTALL)
        if json_match:
            return json.loads(json_match.group())
    except Exception as e:
        logger.warning("根因分析 JSON 解析失败: %s，回退关键词匹配", e)

    # ── Fallback: V4 关键词匹配（LLM 分析失败时的兜底）──
    return _keyword_root_cause(errors)


def _keyword_root_cause(errors: list[str]) -> dict:
    """V4 关键词匹配（LLM 分析失败时的 fallback）"""
    error_text = " ".join(errors[-3:]).lower()
    if any(kw in error_text for kw in ["parameter", "not found", "undeclared", "missing"]):
        return {"category": "missing_parameters", "confidence": 0.5,
                "detail": "错误含参数未定义关键字", "suggestion": "检查需求中的参数是否完整"}
    if any(kw in error_text for kw in ["connect", "port", "type mismatch", "equation"]):
        return {"category": "sysml_topology", "confidence": 0.5,
                "detail": "错误含连接/端口关键字", "suggestion": "检查 SysML 的 connect 和 port 定义"}
    return {"category": "modelica_code", "confidence": 0.5,
            "detail": "默认归类为 Modelica 代码问题", "suggestion": "检查 Modelica 语法和库使用"}


# ============================================================================
# 路由 & 编译 & 仿真 & 工具函数（V4 保留，微调）
# ============================================================================
def _route_after_compile(state): return (
    "simulate_mo" if state.get("node3_step_ok") else
    "repair_mo" if state.get("node3_attempts", 0) < state.get("max_retries", 5) else "end_fail")
def _route_after_simulate(state): return (
    "end_success" if state.get("node3_step_ok") else
    "repair_mo" if state.get("node3_attempts", 0) < state.get("max_retries", 5) else "end_fail")
def _route_after_repair(state):
    if state.get("node3_attempts", 0) >= state.get("max_retries", 5): return "end_fail"
    if state.get("mo", {}).get("root_cause") in ("missing_parameters", "sysml_topology"): return "end_fail"
    return "compile_mo"

def _extract_model_name(code): m = re.search(r"model\s+(\w+)", code); return m.group(1) if m else None
def _extract_template_name(llm_output, candidates):
    for name in candidates:
        if name.lower() in llm_output.strip().lower(): return name
    return candidates[0]
def _safe_str(e):
    try: return str(e)[:500]
    except: return f"{type(e).__name__}"[:500]

def _compile(mo_path, model_name):
    import io as _io
    try:
        from OMPython import ModelicaSystem
        ompy_logger = logging.getLogger("OMPython")
        old_level = ompy_logger.level
        ompy_logger.setLevel(logging.DEBUG)
        log_stream = _io.StringIO()
        handler = logging.StreamHandler(log_stream)
        handler.setLevel(logging.DEBUG)
        ompy_logger.addHandler(handler)
        try:
            ModelicaSystem(mo_path, model_name); return True, ""
        except ImportError: return False, "OMPython 未安装"
        except Exception as e:
            log_content = log_stream.getvalue()
            error_lines = []
            for line in log_content.split("\n"):
                if any(kw in line.lower() for kw in ["error", "warning", "syntax", "undefined", "unknown", "missing", "cannot", "invalid"]):
                    if "omc log" in line.lower():
                        parts = line.split("]:", 1)
                        error_lines.append(parts[-1].strip() if len(parts) > 1 else line.strip())
                    else: error_lines.append(line.strip())
            if error_lines: return False, "[OMC编译错误]\n" + "\n".join(error_lines[-10:])
            else: return False, f"[OMPython] {_safe_str(e)}"
        finally: ompy_logger.removeHandler(handler); ompy_logger.setLevel(old_level)
    except ImportError: return False, "OMPython 未安装"

def _simulate(mo_path, model_name, results_dir, stop_time=0.01):
    try:
        from OMPython import ModelicaSystem
        sim = ModelicaSystem(mo_path, model_name)
        sim.setSimulationOptions({"stopTime": str(stop_time), "stepSize": str(stop_time/500.0)})
        result_mat = str(results_dir / f"{model_name}.mat")
        sim.simulate(resultfile=result_mat)
        if Path(result_mat).exists(): _convert_mat_to_csv(sim, result_mat, model_name, results_dir); return True, ""
        else: return True, ""
    except ImportError: return False, "OMPython 未安装"
    except Exception as e: return False, f"[OMPython] {_safe_str(e)}"

def _plot_csv(csv_path, plot_path, title):
    import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
    with open(csv_path, "r") as f: rows = list(csv.reader(f))
    if len(rows) < 2: return
    header = rows[0]; data = {col: [] for col in header}
    for row in rows[1:]:
        for i, col in enumerate(header):
            try: data[col].append(float(row[i]))
            except: pass
    plt.figure(figsize=(10, 5))
    for col in header[1:]:
        if data[col]: plt.plot(data[header[0]][:len(data[col])], data[col], label=col)
    plt.xlabel(header[0]); plt.ylabel("Value"); plt.title(f"Simulation: {title}")
    plt.legend(); plt.grid(True); plt.tight_layout(); plt.savefig(plot_path, dpi=150); plt.close()

def _convert_mat_to_csv(sim, mat_path, model_name, results_dir):
    csv_path = str(results_dir / "simulation.csv")
    try:
        sols = sim.getSolutions()
        if not sols: return
        def _is_param(col):
            suffixes = [".C",".R",".L",".LossPower",".alpha",".T",".T_ref",".T_heatPort",".offset",".startTime",".Vpp",".Vnn",".signalSource.",".constantVoltage.",".gain"]
            return any(col.endswith(s) for s in suffixes[:10]) or any(s in col for s in suffixes[10:])
        signal_vars, other_vars = [], []
        for v in sols:
            if v == "time" or "der(" in v: continue
            if _is_param(v) or ".n.v" in v or ".n.i" in v or "ground" in v.lower(): continue
            (signal_vars.insert(0, v) if "opAmp.out" in v or "opamp.out" in v else
             signal_vars.append(v) if "sensor.v" in v or v.endswith((".p.v",".v")) else
             other_vars.append(v))
        vars_to_read = ["time"] + signal_vars[:4] + other_vars[:2]
        names_str = "{" + ",".join(vars_to_read) + "}"
        mat_forward = mat_path.replace("\\", "/")
        raw = sim.sendExpression(f'readSimulationResult("{mat_forward}", {names_str})')
        if not raw or len(raw) < 2: return
        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(vars_to_read)
            for i in range(len(raw[0])):
                writer.writerow([str(raw[j][i]) if j < len(raw) and i < len(raw[j]) else "0" for j in range(len(vars_to_read))])
        logger.info("MAT→CSV 完成: %s", csv_path)
    except Exception as e:
        logger.warning("MAT→CSV 失败: %s", e)
