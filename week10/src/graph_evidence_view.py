"""Render verified V9 graph paths directly from their original graph records."""

from __future__ import annotations

from copy import deepcopy
import re
from typing import Any


_REASONS = {
    "unsupported_question": "问题不属于可验证的声明、结构检查、反查或版本比较范围。请明确一个公开参数和所需路径。",
    "unknown_target": "未识别到公开参数目标。请使用参数的正式名称或已支持别名。",
    "ambiguous_target": "参数指代不明确。请说明具体对象，例如房间 A 或房间 B。",
    "incompatible_runs": "两次运行不属于同一公开案例和模板，不能比较。",
    "target_absent": "所选运行中没有该目标的已验证图证据。",
    "missing_evidence": "图中缺少完整的已验证结构证据。",
    "context_budget_exceeded": "证据卡数量或字符预算不足。请提高预算或缩小问题范围。",
}


def _node_maps(graph: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    nodes = graph.get("nodes")
    edges = graph.get("edges")
    if not isinstance(nodes, list) or not isinstance(edges, list):
        raise ValueError("invalid graph")
    return ({x["id"]: x for x in nodes if isinstance(x, dict) and isinstance(x.get("id"), str)},
            {x["id"]: x for x in edges if isinstance(x, dict) and isinstance(x.get("id"), str)})


def _reject(reason: str, retrieval: dict[str, Any]) -> dict[str, Any]:
    return {"retrieval": deepcopy(retrieval), "evidence_answer": {
        "status": "abstained", "origin": "deterministic_graph_evidence", "reason_code": reason,
        "items": [], "comparison": None}, "api_call_count": 0}


def _item_for_card(card: dict[str, Any], nodes: dict[str, dict[str, Any]], edges: dict[str, dict[str, Any]]) -> dict[str, Any] | None:
    node_ids, edge_ids = card.get("node_ids"), card.get("edge_ids")
    if not isinstance(node_ids, list) or not isinstance(edge_ids, list) or not node_ids or not edge_ids:
        return None
    selected_nodes = [nodes.get(value) if isinstance(value, str) else None for value in node_ids]
    selected_edges = [edges.get(value) if isinstance(value, str) else None for value in edge_ids]
    if any(value is None for value in selected_nodes) or any(value is None for value in selected_edges):
        return None
    run_id = card.get("run_id")
    if not isinstance(run_id, str) or any(n.get("run_id") != run_id for n in selected_nodes) or any(e.get("run_id") != run_id for e in selected_edges):
        return None
    hashes = {e.get("link_evidence_sha256") for e in selected_edges}
    if len(hashes) != 1 or not all(isinstance(x, str) and x for x in hashes):
        return None
    relations = [e.get("relation") for e in selected_edges]
    pairs = [(e.get("source"), e.get("target")) for e in selected_edges]
    types = [n.get("type") for n in selected_nodes]
    # Only the retrieval module's four explicitly bounded path shapes are accepted.
    if (relations == ["explicitly_bound_to", "structurally_represented_by"] and len(node_ids) == 3
            and pairs == [(node_ids[0], node_ids[1]), (node_ids[1], node_ids[2])]
            and types == ["requirement", "target", "element"]):
        pass
    elif (relations == ["structurally_represented_by"] and len(node_ids) == 2
          and pairs == [(node_ids[0], node_ids[1])] and types == ["target", "element"]):
        pass
    elif (relations == ["explicitly_checked_by"] and len(node_ids) == 2
          and pairs == [(node_ids[0], node_ids[1])] and types == ["target", "check"]):
        pass
    elif (relations == ["explicitly_bound_to", "explicitly_checked_by"] and len(node_ids) == 3
          and pairs == [(node_ids[0], node_ids[1]), (node_ids[1], node_ids[2])]
          and types == ["requirement", "target", "check"]):
        pass
    else:
        return None
    target = next((n for n in selected_nodes if n.get("type") == "target"), None)
    if target is None or not isinstance(target.get("target_id"), str):
        return None
    if any(e.get("provenance", {}).get("target_id") != target["target_id"] for e in selected_edges):
        return None
    result: dict[str, Any] = {"path_id": card.get("id"), "run_id": run_id, "target_id": target["target_id"],
                              "node_ids": list(node_ids), "edge_ids": list(edge_ids)}
    requirement = next((n for n in selected_nodes if n.get("type") == "requirement"), None)
    if requirement is not None:
        if not isinstance(requirement.get("requirement_id"), str) or not isinstance(requirement.get("text"), str):
            return None
        if any(e.get("provenance", {}).get("requirement_id") != requirement["requirement_id"] for e in selected_edges):
            return None
        result.update(requirement_id=requirement["requirement_id"], requirement_text=requirement["text"])
    element = next((n for n in selected_nodes if n.get("type") == "element"), None)
    if element is not None:
        for key in ("layer", "element_id", "exact_selector"):
            if not isinstance(element.get(key), str) or not element[key]:
                return None
            result[key] = element[key]
        if result["layer"] not in {"sysml", "modelica"}:
            return None
    check = next((n for n in selected_nodes if n.get("type") == "check"), None)
    if check is not None:
        for key, output_key in (("check_type", "check_type"), ("status", "check_status")):
            if not isinstance(check.get(key), str) or not check[key]:
                return None
            result[output_key] = check[key]
    return result


def _comparison(index: Any, retrieval: dict[str, Any], items: list[dict[str, Any]]) -> dict[str, Any] | None:
    plan = retrieval.get("plan", {})
    if plan.get("intent") != "compare":
        return None
    target_ids, run_ids = plan.get("target_ids"), retrieval.get("run_ids")
    if not isinstance(target_ids, list) or len(target_ids) != 1 or not isinstance(run_ids, list) or len(run_ids) != 2:
        return None
    target_id = target_ids[0]
    if not all(any(x["run_id"] == run and x["target_id"] == target_id for x in items) for run in run_ids):
        return None
    compared = index.graph.compare_runs(run_ids[0], run_ids[1])
    if not isinstance(compared, dict) or not isinstance(compared.get("parameter_changes"), list):
        return None
    name = target_id.removeprefix("parameter:")
    if any(not isinstance(x, dict) or not isinstance(x.get("canonical_name"), str)
           for x in compared["parameter_changes"]):
        return None
    record = next((x for x in compared["parameter_changes"] if x["canonical_name"] == name), None)
    if record is not None and ("before" not in record or "after" not in record):
        return None
    answer: dict[str, Any] = {"target_id": target_id, "before_run_id": run_ids[0], "after_run_id": run_ids[1],
                              "normalized_parameter_changed": record is not None}
    if record is not None:
        answer["change_record"] = deepcopy(record)
    return answer


def build_evidence_answer(index: Any, question: str, run_ids: list[str], max_cards: int = 24, max_chars: int = 12000) -> dict[str, Any]:
    """Return original graph fields for one retrieval result; never call a model."""
    retrieval = index.retrieve(question, run_ids, max_cards=max_cards, max_chars=max_chars)
    graph = index.graph.to_dict()
    if retrieval.get("status") != "ready":
        return _reject(str(retrieval.get("reason_code") or "missing_evidence"), retrieval)
    try:
        nodes, edges = _node_maps(graph)
        cards = retrieval.get("cards")
        plan = retrieval.get("plan")
        if (not isinstance(cards, list) or not cards or retrieval.get("run_ids") != run_ids
                or not isinstance(plan, dict) or not isinstance(plan.get("intent"), str)
                or not isinstance(plan.get("target_ids"), list)):
            return _reject("missing_evidence", retrieval)
        card_ids = [card.get("id") for card in cards if isinstance(card, dict)]
        if (len(card_ids) != len(cards) or any(not isinstance(value, str) or not value for value in card_ids)
                or len(set(card_ids)) != len(card_ids) or retrieval.get("required_evidence_ids") != card_ids):
            return _reject("missing_evidence", retrieval)
        items = [_item_for_card(card, nodes, edges) if isinstance(card, dict) else None for card in cards]
        if any(item is None for item in items):
            return _reject("missing_evidence", retrieval)
        if any(item["run_id"] not in run_ids for item in items):
            return _reject("missing_evidence", retrieval)
        intent, targets = plan["intent"], plan["target_ids"]
        if intent in {"declarations", "impact", "compare"}:
            if len(targets) != 1 or any(item["target_id"] != targets[0] for item in items):
                return _reject("missing_evidence", retrieval)
        elif intent == "reverse":
            check_type = plan.get("check_type")
            if (not isinstance(check_type, str) or any(item.get("check_type") != check_type or "requirement_id" not in item
                                                       or len(item["node_ids"]) != 3 for item in items)):
                return _reject("missing_evidence", retrieval)
        else:
            return _reject("missing_evidence", retrieval)
        if intent in {"declarations", "compare"}:
            for run_id in run_ids:
                if {item.get("layer") for item in items if item["run_id"] == run_id} != {"sysml", "modelica"}:
                    return _reject("missing_evidence", retrieval)
        if intent == "impact":
            if {item.get("layer") for item in items if "layer" in item} != {"sysml", "modelica"} or not any("check_type" in item for item in items):
                return _reject("missing_evidence", retrieval)
        comparison = _comparison(index, retrieval, items)
        if intent == "compare" and comparison is None:
            return _reject("missing_evidence", retrieval)
    except (KeyError, TypeError, ValueError):
        return _reject("missing_evidence", retrieval)
    return {"retrieval": deepcopy(retrieval), "evidence_answer": {
        "status": "ready", "origin": "deterministic_graph_evidence", "reason_code": None,
        "items": items, "comparison": comparison}, "api_call_count": 0}


def _fence(text: str) -> str:
    longest = max((len(match.group(0)) for match in re.finditer(r"`+", text)), default=0)
    return "`" * max(3, longest + 1)


def render_markdown(result: dict[str, Any]) -> str:
    answer = result.get("evidence_answer", {})
    lines = ["# 原图证据展示", "", "来源：确定性图证据；模型调用：0。"]
    if answer.get("status") != "ready":
        reason = answer.get("reason_code")
        lines.extend(["", "## 已拒答", "", f"原因代码：`{reason}`"])
        if reason in _REASONS:
            lines.append(_REASONS[reason])
        return "\n".join(lines) + "\n"
    comparison = answer.get("comparison")
    if comparison is not None:
        changed = comparison["normalized_parameter_changed"]
        state = "已变更" if changed else "未变更（规范化值和单位相同）"
        lines.extend(["", "## 版本比较", "", f"目标 `{comparison['target_id']}` 在 `{comparison['before_run_id']}` 与 `{comparison['after_run_id']}` 间{state}。原始声明仍按各运行逐条展示。"])
        if changed:
            change = comparison["change_record"]
            lines.append(f"前：`{change.get('before')}`；后：`{change.get('after')}`。")
    for number, item in enumerate(answer.get("items", []), 1):
        lines.extend(["", f"## 路径 {number}", "", f"运行：`{item['run_id']}`；目标：`{item['target_id']}`。"])
        if "requirement_id" in item:
            lines.append(f"需求 `{item['requirement_id']}`：{item['requirement_text']}")
        if "exact_selector" in item:
            code = item["exact_selector"]
            fence = _fence(code)
            lines.extend([f"{item.get('layer', 'model')} 原始声明（元素 `{item.get('element_id', '')}`）：", fence, code, fence])
        if "check_type" in item:
            lines.append(f"显式结构检查：`{item['check_type']}`；状态：`{item.get('check_status', '')}`。")
        if len(item["node_ids"]) == 3 and "check_type" in item:
            lines.append("这是显式多跳关联，不表示直接边或根因。")
        if "check_type" in item and "exact_selector" not in item:
            lines.append("影响仅表示显式结构检查关联，不保证全部检查重跑或数值行为。")
        lines.append("原图节点：`" + "`, `".join(item["node_ids"]) + "`。")
        lines.append("原图边：`" + "`, `".join(item["edge_ids"]) + "`。")
    return "\n".join(lines) + "\n"
