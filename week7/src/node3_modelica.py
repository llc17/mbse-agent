"""
节点 3 — Modelica Agent（V6 LLM选域 + 根因分析 + 三阶段审查版）。

V4 → V6 变更:
  - generate_mo: LLM 选域 + Python 模板注入（替代纯 LLM 自由发挥）
  - repair_mo: 增加根因分析（LLM 分析错误 → 针对性修复 vs 打回上游）
  - 新增审查阶段：生成后审查 Modelica 代码质量
  - 子图结构保留（generate→compile→simulate→repair 循环）

Agent ③ 内部流程:
  Python 初筛候选模板 → LLM 确认选域 → Python 注入模板骨架
  → LLM 填空参数 → 审查 → 编译 → 仿真 → (失败→根因分析→修复/打回)

根因分析:
  V4: 关键词匹配（"parameter" → node1, "connect" → node2）
  V6: LLM 综合分析错误日志 + req + sysml + modelica_code 三段上下文
       判断是需求缺参数、SysML拓扑错、还是Modelica代码错
       连续两次分析结果矛盾 → 标记转 HITL
"""

import csv
import io
import json
import logging
import re
import subprocess
import time
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


# ============================================================
# 工具: 确保关键字段不丢
# ============================================================
# V6: Modelica 代码审查（生成后、编译前）
# ============================================================

def _review_modelica_code(
    mo_code: str,
    req: StructuredRequirement,
    sysml_code: str,
    temperature: float,
    max_rounds: int = 2,
) -> str:
    """V6: 生成 Modelica 代码后审查，发现问题则修正。

    这是 Agent ③ 的审查阶段 — 在编译前检查代码质量，减少编译失败。
    使用 chat_with("review", ...) 让审查可以用不同模型。
    """
    if not mo_code or len(mo_code) < 20:
        return mo_code

    params_str = "\n".join(f"  {k} = {v}" for k, v in req.parameters.items())

    for round_num in range(1, max_rounds + 1):
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

        if review.ok:
            logger.info("节点3 V6: Modelica 审查通过 (score=%s)", review.score)
            return mo_code

        if not review.issues:
            logger.info("节点3 V6: 审查未通过但无具体问题，视为通过")
            return mo_code

        if round_num >= max_rounds:
            logger.warning("节点3 V6: Modelica 审查 %s 轮未通过，使用当前版本", max_rounds)
            return mo_code

        # 修正
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


# ============================================================
# V4 遗留: _always_pass 模板（V6 不再需要，各节点直接返回状态字段）
# 保留此函数作为文档——展示子图节点必须返回的关键字段
# ============================================================
def _always_pass(state: dict) -> dict:
    """V4: 每个子图节点返回时确保计数器字段不丢。
    V6: 节点已直接返回 node3_attempts/node3_step_ok，不再调用此函数。"""
    return {
        "node3_attempts": state.get("node3_attempts", 0),
        "node3_step_ok": state.get("node3_step_ok", False),
    }


# ============================================================
# 构建子图
# ============================================================
def build_node3_subgraph() -> StateGraph:
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


# ============================================================
# V6: 生成 — LLM选域 + 模板注入 + 审核
# ============================================================

def _generate_mo(state: dict) -> dict:
    """V6 生成：Python 初筛候选模板 → LLM 确认选域 → Python 注入模板 → LLM 填空。

    如果模板匹配度高（候选第一名得分远高于其他），跳过 LLM 确认直接注入。
    """
    t0 = time.time()
    req_dict = state.get("req", {})
    sysml_dict = state.get("sysml", {})
    sysml_code = sysml_dict.get("sysml_code", "")
    temperature = state.get("temperature", 0.3)

    req = StructuredRequirement(**req_dict) if req_dict else StructuredRequirement(
        component_type="unknown", raw_input=""
    )

    component_type = req.component_type or "unknown"
    params = req.parameters
    component_name = (req.component_name or
                      re.sub(r'[^a-zA-Z0-9_]', '_', component_type)[:30] or
                      "MyModel")

    # ── 第 1 步: Python 初筛候选模板 ──
    candidates = get_candidate_templates(component_type)
    logger.info("节点3 V6: 候选模板 %s (来自 '%s')", candidates, component_type)

    # ── 第 2 步: LLM 确认选域 ──
    if len(candidates) == 1:
        selected_template = candidates[0]
        logger.info("节点3 V6: 仅1个候选，直接选择 '%s'", selected_template)
    else:
        selection_prompt = build_template_selection_prompt(component_type, params, candidates)
        selection_result = chat([user_msg(selection_prompt)], temperature=0.0, max_tokens=128).strip()
        # 提取模板名
        selected_template = _extract_template_name(selection_result, candidates)
        logger.info("节点3 V6: LLM 选择模板 '%s' (候选=%s)", selected_template, candidates)

    # ── 第 3 步: Python 注入模板 ──
    mo_code = inject_template(selected_template, params, component_name)

    # ── 第 4 步: LLM 填空/微调 ──
    # 模板注入后，LLM 可以根据 sysml 代码和需求微调（如增减组件、调整连接）
    params_str = "\n".join(f"  {k} = {v}" for k, v in req.parameters.items())
    constraints_str = "\n".join(f"  - {c}" for c in req.constraints)

    refine_prompt = (
        f"## 角色\n你是 Modelica 仿真建模专家。\n\n"
        f"## 组件信息\n- 类型: {component_type}\n- 参数:\n{params_str}\n"
        f"- 拓扑: {req.topology}\n- 约束:\n{constraints_str}\n\n"
        f"## SysML 系统模型（参考拓扑）\n```sysml\n{sysml_code[:2000]}\n```\n\n"
        f"## Modelica 模板骨架（已填充参数）\n```modelica\n{mo_code}\n```\n\n"
        f"## 要求\n"
        f"1. 检查模板骨架的参数值是否与需求一致\n"
        f"2. 如有需要，微调参数值使其更精确（如根据截止频率公式反推精确的R/C值）\n"
        f"3. 添加必要的注释说明\n"
        f"4. 不要大幅改动模板的连接结构（保证编译通过）\n"
        f"5. 只输出完整的 Modelica 代码，不要包含解释"
    )

    logger.info("节点3 V6: LLM 微调模板代码...")
    mo_code = chat([user_msg(refine_prompt)], temperature=temperature, max_tokens=4096).strip()
    mo_code = clean_code_block(mo_code, "modelica")

    # ── V6: Modelica 代码审查（生成后、编译前）──
    mo_code = _review_modelica_code(
        mo_code, req, sysml_code, temperature
    )

    model_name = _extract_model_name(mo_code) or component_name
    logger.info("节点3 V6 generate 完成, 模板=%s, 模型名=%s", selected_template, model_name)

    return {
        "mo": {
            "modelica_code": mo_code,
            "file_path": "",
            "csv_path": "",
            "plot_path": "",
            "attempts": 0,
            "errors": [],
            "success": False,
            "_template": selected_template,  # V6: 记录使用的模板
        },
        "node3_attempts": 0,
        "node3_step_ok": False,
        "run_dir": state.get("run_dir", ""),
        "timing": {**state.get("timing", {}), "node3_generate": time.time() - t0},
    }


# ============================================================
# 编译 & 仿真（V4 保留，微调）
# ============================================================

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
            "node3_attempts": attempts,
            "node3_step_ok": False,
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
    modelica_dir = run_dir / "modelica"
    results_dir = run_dir / "results"
    results_dir.mkdir(parents=True, exist_ok=True)

    modelica_code = mo_dict.get("modelica_code", "")
    model_name = _extract_model_name(modelica_code) or "MyModel"
    mo_path = str(modelica_dir / "model.mo")

    logger.info("节点3 simulate: 仿真 %s...", model_name)
    req = state.get("req", {})
    stop_time = get_stop_time_for_domain(req.get("component_type", ""))
    sim_ok, sim_err = _simulate(mo_path, model_name, results_dir, stop_time)

    errors = list(mo_dict.get("errors", []))

    if not sim_ok:
        attempts = state.get("node3_attempts", 0) + 1
        logger.warning("节点3 simulate 失败 (第%s次): %s", attempts, sim_err[:200])
        errors.append(f"[仿真错误 #{attempts}] {sim_err[:500]}")

        return {
            "mo": {**mo_dict, "errors": errors, "attempts": attempts},
            "node3_attempts": attempts,
            "node3_step_ok": False,
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
        logger.info("修复日志已保存: %s (%s 次修复)", log_path, len(repair_log))

    return {
        "mo": {**mo_dict, "errors": errors, "success": True,
               "csv_path": str(csv_path.resolve()), "plot_path": str(plot_path.resolve())},
        "node3_attempts": state.get("node3_attempts", 0),
        "node3_step_ok": True,
        "run_dir": state.get("run_dir", ""),
        "timing": {**state.get("timing", {}), "node3_simulate": time.time() - t0},
    }


# ============================================================
# V6: 修复 — 根因分析 + 针对性修复
# ============================================================

def _repair_mo(state: dict) -> dict:
    """V6 修复：LLM 根因分析 + 针对性修复。

    V4: 把错误日志贴回去让 LLM 重新生成（"对着错误改"）
    V6: LLM 分析错误的根因 → 判断是参数/连接/语法问题 → 针对性修复
    """
    t0 = time.time()
    req_dict = state.get("req", {})
    sysml_dict = state.get("sysml", {})
    mo_dict = state.get("mo", {})
    sysml_code = sysml_dict.get("sysml_code", "")
    temperature = state.get("temperature", 0.3)
    attempts = state.get("node3_attempts", 0)

    req = StructuredRequirement(**req_dict) if req_dict else StructuredRequirement(
        component_type="unknown", raw_input=""
    )

    errors = mo_dict.get("errors", [])
    code_before = mo_dict.get("modelica_code", "")
    errors_before = errors[-3:]

    # ── V6: LLM 根因分析 ──
    root_cause = _analyze_root_cause(errors, req, sysml_code, code_before)

    logger.info("节点3 V6 根因分析: category=%s, confidence=%s, detail=%s",
                root_cause.get("category"), root_cause.get("confidence"),
                root_cause.get("detail", "")[:100])

    # ── 根据根因决定修复策略 ──
    if root_cause.get("category") == "missing_parameters" and root_cause.get("confidence", 0) >= 0.7:
        # 缺参数 → 标记需要打回 node1（在 mo 中记录，由 pipeline 路由处理）
        logger.warning("节点3 V6: 根因=缺参数 → 标记打回 node1")
        new_attempts = attempts + 1
        feedback = f"[V6根因分析: 缺参数] {root_cause.get('detail', '')} {root_cause.get('suggestion', '')}"
        return {
            "mo": {**mo_dict, "root_cause": "missing_parameters",
                   "root_cause_detail": root_cause.get("detail", ""),
                   "attempts": new_attempts},
            "node3_attempts": new_attempts,
            "node3_step_ok": False,
            "human_feedback": feedback,  # V6: 把根因分析结果传给 node1
            "repair_log": list(state.get("repair_log", [])),
            "run_dir": state.get("run_dir", ""),
            "timing": {**state.get("timing", {}), "node3_repair": time.time() - t0},
        }

    if root_cause.get("category") == "sysml_topology" and root_cause.get("confidence", 0) >= 0.7:
        # SysML 拓扑问题 → 标记打回 node2
        logger.warning("节点3 V6: 根因=SysML拓扑 → 标记打回 node2")
        new_attempts = attempts + 1
        feedback = f"[V6根因分析: SysML拓扑问题] {root_cause.get('detail', '')} {root_cause.get('suggestion', '')}"
        return {
            "mo": {**mo_dict, "root_cause": "sysml_topology",
                   "root_cause_detail": root_cause.get("detail", ""),
                   "attempts": new_attempts},
            "node3_attempts": new_attempts,
            "node3_step_ok": False,
            "human_feedback": feedback,  # V6: 把根因分析结果传给 node2
            "repair_log": list(state.get("repair_log", [])),
            "run_dir": state.get("run_dir", ""),
            "timing": {**state.get("timing", {}), "node3_repair": time.time() - t0},
        }

    # ── 默认：Modelica 代码问题 → 针对性修复 ──
    params_str = "\n".join(f"  {k} = {v}" for k, v in req.parameters.items())
    constraints_str = "\n".join(f"  - {c}" for c in req.constraints)

    error_section = "## 编译/仿真错误日志\n```\n" + "\n".join(errors[-5:]) + "\n```"

    repair_prompt = (
        f"## 角色\n你是 Modelica 代码修复专家。\n\n"
        f"## 组件信息\n- 类型: {req.component_type}\n- 参数:\n{params_str}\n"
        f"- 拓扑: {req.topology}\n- 约束:\n{constraints_str}\n\n"
        f"## 当前 Modelica 代码（有错误）\n```modelica\n{code_before[:3000]}\n```\n\n"
        f"{error_section}\n\n"
        f"## 根因分析结果\n- 错误类别: {root_cause.get('category', 'unknown')}\n"
        f"- 分析: {root_cause.get('detail', '')}\n"
        f"- 修复建议: {root_cause.get('suggestion', '')}\n\n"
        f"## SysML 参考（系统拓扑）\n```sysml\n{sysml_code[:2000]}\n```\n\n"
        f"## 要求\n"
        f"1. 根据根因分析的结果修复 Modelica 代码\n"
        f"2. 只改动与错误相关的部分，不要重写整个模型\n"
        f"3. 确保修复后的代码与 SysML 系统模型的拓扑一致\n"
        f"4. 只输出完整的 Modelica 代码，不要包含解释"
    )

    logger.info("节点3 V6 repair: 第%s次 LLM 修复...", attempts)
    mo_code = chat([user_msg(repair_prompt)], temperature=temperature, max_tokens=4096).strip()
    mo_code = clean_code_block(mo_code, "modelica")

    # 记录修复日志
    repair_entry = {
        "attempt": attempts,
        "errors_before": errors_before,
        "root_cause": root_cause,
        "code_before_snippet": code_before[:200] if code_before else "",
        "code_after_snippet": mo_code[:200],
    }
    repair_log = list(state.get("repair_log", []))
    repair_log.append(repair_entry)

    logger.info("节点3 V6 repair 完成, 新模型名=%s, 已记录修复日志",
                _extract_model_name(mo_code) or "未识别")

    return {
        "mo": {**mo_dict, "modelica_code": mo_code},
        "node3_attempts": attempts,
        "node3_step_ok": False,
        "repair_log": repair_log,
        "run_dir": state.get("run_dir", ""),
        "timing": {**state.get("timing", {}), "node3_repair": time.time() - t0},
    }


# ============================================================
# V6: 根因分析（LLM 替代关键词匹配）
# ============================================================

def _analyze_root_cause(
    errors: list[str],
    req: StructuredRequirement,
    sysml_code: str,
    modelica_code: str,
) -> dict:
    """V6: LLM 分析编译/仿真失败的根因。

    与 V4 关键词匹配的关键区别:
      - V4: 只看错误信息的第一行，关键词匹配 → 可能判断错
      - V6: 同时看错误日志 + req + sysml + modelica 三段上下文 → 判断更准

    Returns:
        {
            "category": "modelica_code" | "missing_parameters" | "sysml_topology" | "unknown",
            "confidence": 0.0-1.0,
            "detail": "分析说明",
            "suggestion": "修复建议"
        }
    """
    if not errors:
        return {
            "category": "unknown",
            "confidence": 0.5,
            "detail": "无错误信息",
            "suggestion": "重新生成 Modelica 代码",
        }

    error_text = "\n".join(errors[-5:])  # 最近 5 个错误
    req_json = json.dumps({
        "component_type": req.component_type,
        "parameters": req.parameters,
        "topology": req.topology,
        "constraints": req.constraints,
    }, ensure_ascii=False, indent=2)

    analysis_prompt = (
        f"## 角色\n你是 Modelica 编译错误根因分析专家。\n\n"
        f"## 编译/仿真错误日志\n```\n{error_text}\n```\n\n"
        f"## 需求（req JSON）\n```json\n{req_json}\n```\n\n"
        f"## SysML 系统模型\n```sysml\n{sysml_code[:2000]}\n```\n\n"
        f"## Modelica 代码\n```modelica\n{modelica_code[:2000]}\n```\n\n"
        f"## 分析要求\n"
        f"判断错误的根因属于以下哪一类：\n"
        f"1. **missing_parameters**: 需求本身缺少必要的物理参数（如缺电阻值、缺温度），"
        f"   导致 Modelica 无法生成正确代码 → 应打回需求分析 node1\n"
        f"2. **sysml_topology**: SysML 的拓扑/连接关系有误，"
        f"   导致 Modelica 按照错误的拓扑生成 → 应打回 SysML node2\n"
        f"3. **modelica_code**: 需求和 SysML 都是对的，"
        f"   但 Modelica 代码本身有问题（语法错误、单位不匹配、库使用不当）→ 在 node3 内修复\n\n"
        f"## 输出格式（严格 JSON）\n"
        f"```json\n{{\n"
        f'  "category": "modelica_code|missing_parameters|sysml_topology|unknown",\n'
        f'  "confidence": 0.0-1.0,\n'
        f'  "detail": "简短分析说明",\n'
        f'  "suggestion": "修复建议"\n'
        f"}}\n```\n\n"
        f"只输出 JSON，不要解释。"
    )

    try:
        result = chat([user_msg(analysis_prompt)], temperature=0.1, max_tokens=512)
        # 提取 JSON
        json_match = re.search(r"\{.*\}", result, re.DOTALL)
        if json_match:
            return json.loads(json_match.group(0))
    except Exception as e:
        logger.warning("根因分析 JSON 解析失败: %s，回退关键词匹配", e)

    # Fallback: V4 关键词匹配
    return _keyword_root_cause(errors)


def _keyword_root_cause(errors: list[str]) -> dict:
    """V4 关键词匹配（LLM 分析失败时的 fallback）。"""
    error_text = " ".join(errors[-3:]).lower()

    if any(kw in error_text for kw in ["parameter", "not found", "undeclared", "missing", "未定义"]):
        return {
            "category": "missing_parameters",
            "confidence": 0.5,
            "detail": "错误含参数未定义关键字，可能是需求缺参数",
            "suggestion": "检查需求中的参数是否完整",
        }
    if any(kw in error_text for kw in ["connect", "port", "type mismatch", "equation", "连接"]):
        return {
            "category": "sysml_topology",
            "confidence": 0.5,
            "detail": "错误含连接/端口关键字，可能是 SysML 拓扑有误",
            "suggestion": "检查 SysML 的 connect 和 port 定义",
        }

    return {
        "category": "modelica_code",
        "confidence": 0.5,
        "detail": "默认归类为 Modelica 代码问题",
        "suggestion": "检查 Modelica 语法和库使用",
    }


# ============================================================
# 路由（保持不变）
# ============================================================

def _route_after_compile(state: dict) -> str:
    if state.get("node3_step_ok", False):
        return "simulate_mo"
    attempts = state.get("node3_attempts", 0)
    max_retries = state.get("max_retries", 5)
    if attempts >= max_retries:
        logger.warning("节点3: 编译重试耗尽 (%s/%s)", attempts, max_retries)
        return "end_fail"
    return "repair_mo"


def _route_after_simulate(state: dict) -> str:
    if state.get("node3_step_ok", False):
        return "end_success"
    attempts = state.get("node3_attempts", 0)
    max_retries = state.get("max_retries", 5)
    if attempts >= max_retries:
        logger.warning("节点3: 仿真重试耗尽 (%s/%s)", attempts, max_retries)
        return "end_fail"
    return "repair_mo"


def _route_after_repair(state: dict) -> str:
    attempts = state.get("node3_attempts", 0)
    max_retries = state.get("max_retries", 5)
    if attempts >= max_retries:
        logger.warning("节点3: 修复重试耗尽 (%s/%s)", attempts, max_retries)
        return "end_fail"
    # 检查是否被根因分析标记为打回上游
    mo = state.get("mo", {})
    if mo.get("root_cause") in ("missing_parameters", "sysml_topology"):
        logger.info("节点3: 根因分析标记打回上游 (%s)", mo.get("root_cause"))
        return "end_fail"  # 结束子图，让 pipeline 路由处理
    return "compile_mo"


# ============================================================
# 编译 & 仿真（V4 保留，不变）
# ============================================================

def _safe_str(e: Exception) -> str:
    try:
        s = str(e)
    except (UnicodeEncodeError, UnicodeDecodeError):
        try:
            s = repr(e)
        except Exception:
            s = f"{type(e).__name__}"
    return s[:500]


def _compile(mo_path: str, model_name: str) -> tuple[bool, str]:
    import io
    try:
        from OMPython import ModelicaSystem

        ompy_logger = logging.getLogger("OMPython")
        old_level = ompy_logger.level
        ompy_logger.setLevel(logging.DEBUG)
        log_stream = io.StringIO()
        handler = logging.StreamHandler(log_stream)
        handler.setLevel(logging.DEBUG)
        ompy_logger.addHandler(handler)

        try:
            ModelicaSystem(mo_path, model_name)
            return True, ""
        except ImportError:
            return False, "OMPython 未安装"
        except Exception as e:
            log_content = log_stream.getvalue()
            error_lines = []
            for line in log_content.split("\n"):
                lower = line.lower()
                if any(kw in lower for kw in [
                    "error", "warning", "syntax", "undefined",
                    "unknown", "missing", "cannot", "invalid",
                    "unmatched", "unexpected", "not found",
                ]):
                    if "omc log" in lower:
                        parts = line.split("]:", 1)
                        error_lines.append(parts[-1].strip() if len(parts) > 1 else line.strip())
                    else:
                        error_lines.append(line.strip())

            if error_lines:
                return False, "[OMC编译错误]\n" + "\n".join(error_lines[-10:])
            else:
                return False, f"[OMPython] {_safe_str(e)}"
        finally:
            ompy_logger.removeHandler(handler)
            ompy_logger.setLevel(old_level)

    except ImportError:
        return False, "OMPython 未安装"


def _simulate(mo_path: str, model_name: str, results_dir: Path,
              stop_time: float = 0.01) -> tuple[bool, str]:
    try:
        from OMPython import ModelicaSystem
        sim = ModelicaSystem(mo_path, model_name)
        step_size = stop_time / 500.0
        sim.setSimulationOptions({
            "stopTime": str(stop_time),
            "stepSize": str(step_size),
        })

        result_mat = str(results_dir / f"{model_name}.mat")
        sim.simulate(resultfile=result_mat)

        if Path(result_mat).exists():
            _convert_mat_to_csv(sim, result_mat, model_name, results_dir)
            return True, ""
        else:
            logger.warning("仿真成功但 MAT 文件未生成: %s", result_mat)
            return True, ""

    except ImportError:
        return False, "OMPython 未安装"
    except Exception as e:
        return False, f"[OMPython] {_safe_str(e)}"


# ============================================================
# 工具函数
# ============================================================

def _extract_model_name(code: str) -> str | None:
    m = re.search(r"model\s+(\w+)", code)
    return m.group(1) if m else None


def _extract_template_name(llm_output: str, candidates: list[str]) -> str:
    """从 LLM 返回中提取模板名。"""
    output = llm_output.strip().lower()
    for name in candidates:
        if name.lower() in output:
            return name
    # 尝试匹配完整词
    for name in candidates:
        if name.lower().replace("_", " ") in output:
            return name
    logger.warning("LLM 返回未匹配已知模板名: '%s'，回退第一个候选", llm_output)
    return candidates[0]


def _plot_csv(csv_path: str, plot_path: str, title: str):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    with open(csv_path, "r") as f:
        rows = list(csv.reader(f))

    if len(rows) < 2:
        return

    header = rows[0]
    data = {col: [] for col in header}
    for row in rows[1:]:
        for i, col in enumerate(header):
            try:
                data[col].append(float(row[i]))
            except (ValueError, IndexError):
                pass

    time_col = header[0]
    plt.figure(figsize=(10, 5))
    for col in header[1:]:
        if data[col]:
            plt.plot(data[time_col][:len(data[col])], data[col], label=col)

    plt.xlabel(time_col)
    plt.ylabel("Value")
    plt.title(f"Simulation: {title}")
    plt.legend()
    plt.grid(True)
    plt.tight_layout()
    plt.savefig(plot_path, dpi=150)
    plt.close()


def _convert_mat_to_csv(sim, mat_path: str, model_name: str, results_dir: Path):
    csv_path = str(results_dir / "simulation.csv")

    try:
        sols = sim.getSolutions()
        if not sols:
            logger.warning("getSolutions 返回空，跳过 MAT→CSV")
            return

        def _is_param_or_meta(col: str) -> bool:
            suffixes = [".C", ".R", ".R_actual", ".L", ".LossPower", ".alpha",
                        ".T", ".T_ref", ".T_heatPort", ".offset", ".startTime",
                        ".signalSource.height", ".signalSource.offset", ".signalSource.y",
                        ".Vpp", ".Vnn", ".out", ".in_n", ".in_p",
                        ".signalSource.", ".constantVoltage.", ".gain"]
            return any(col.endswith(s) for s in suffixes) or any(s in col for s in [".signalSource.", ".constantVoltage."])

        signal_vars = []
        other_signal_vars = []
        for v in sols:
            if v == "time" or "der(" in v:
                continue
            if _is_param_or_meta(v):
                continue
            if ".n.v" in v or ".n.i" in v or "ground" in v.lower():
                continue
            if "opAmp.out" in v or "opamp.out" in v:
                signal_vars.insert(0, v)
            elif "sensor.v" in v:
                signal_vars.append(v)
            elif v.endswith(".p.v") or v.endswith(".v"):
                signal_vars.append(v)
            elif v.endswith(".i"):
                other_signal_vars.append(v)

        vars_to_read = ["time"] + signal_vars[:4] + other_signal_vars[:2]

        names_str = "{" + ",".join(vars_to_read) + "}"
        mat_forward = mat_path.replace("\\", "/")
        cmd = f'readSimulationResult("{mat_forward}", {names_str})'
        raw = sim.sendExpression(cmd)

        if not raw or len(raw) < 2:
            logger.warning("readSimulationResult 返回为空")
            return

        time_vals = raw[0]
        n_points = len(time_vals)

        with open(csv_path, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(vars_to_read)
            for i in range(n_points):
                row = []
                for j in range(len(vars_to_read)):
                    if j < len(raw) and i < len(raw[j]):
                        row.append(str(raw[j][i]))
                    else:
                        row.append("0")
                writer.writerow(row)

        logger.info("MAT→CSV 完成: %s (%d 变量 × %d 点)", csv_path, len(vars_to_read), n_points)

    except Exception as e:
        logger.warning("MAT→CSV 转换失败: %s", e)
        for pat in ["*.csv", f"{model_name}_res.csv"]:
            candidates = list(results_dir.glob(pat))
            if candidates and candidates[0] != Path(csv_path):
                import shutil
                shutil.copy2(str(candidates[0]), csv_path)
                logger.info("Fallback CSV from %s", candidates[0])
                return
