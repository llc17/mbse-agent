"""Evidence-bounded V2 path retrieval.

Supported intents are deliberately small: ``declarations`` (requirement to a
SysML/Modelica representation), ``impact`` (explicit structural/check links),
``reverse`` (a check back to explicitly joined requirements), and ``compare``.
The static aliases below are the complete public parameter vocabulary; question
text is never used to rank arbitrary graph text into a target selection.
"""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import re
from typing import Any, Iterable

from week10.src.trace_graph import TraceGraph


SCHEMA_VERSION = "v9-path-retrieval-2"

# Each value is specific to one canonical parameter.  Generic Chinese phrases
# whose room/location is omitted deliberately have no entry and are ambiguous.
PARAMETER_ALIASES: dict[str, tuple[str, ...]] = {
    "resistance": ("resistance", "parameter:resistance", "电阻"),
    "capacitance": ("capacitance", "parameter:capacitance", "电容"),
    "cutoff_frequency": ("cutoff_frequency", "cutoff frequency", "截止频率"),
    "input_voltage": ("input_voltage", "input voltage", "输入电压"),
    "heat_capacity_a": ("heat_capacity_a", "room a heat capacity", "房间a热容", "房间a的热容", "a室热容"),
    "heat_capacity_b": ("heat_capacity_b", "room b heat capacity", "房间b热容", "房间b的热容", "b室热容"),
    "shared_resistance": ("shared_resistance", "shared resistance", "共享热阻", "房间间热阻"),
    "outdoor_resistance_a": ("outdoor_resistance_a", "room a outdoor resistance", "outdoor resistance a", "房间a外墙热阻", "a室外墙热阻", "a外墙热阻"),
    "outdoor_resistance_b": ("outdoor_resistance_b", "room b outdoor resistance", "outdoor resistance b", "房间b外墙热阻", "b室外墙热阻", "b外墙热阻"),
    "initial_temperature_a": ("initial_temperature_a", "room a initial temperature", "房间a初始温度", "a室初始温度", "a初始温度"),
    "initial_temperature_b": ("initial_temperature_b", "room b initial temperature", "房间b初始温度", "b室初始温度", "b初始温度"),
    "outdoor_temperature": ("outdoor_temperature", "outdoor temperature", "室外温度"),
}
_AMBIGUOUS = ("初始温度", "热容", "外墙热阻")
_UNKNOWN_IDENTIFIER = re.compile(r"(?<![A-Za-z0-9_])[A-Za-z][A-Za-z0-9_]*(?:_[A-Za-z0-9_]+)+(?![A-Za-z0-9_])")


def _stable_id(value: Any) -> str:
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "path:" + hashlib.sha256(raw).hexdigest()


def _contains(question: str, alias: str) -> bool:
    if re.fullmatch(r"[A-Za-z0-9_: ]+", alias):
        return re.search(r"(?<![A-Za-z0-9_])" + re.escape(alias) + r"(?![A-Za-z0-9_])", question, re.I) is not None
    return alias.lower() in question.lower()


class EvidenceIndexV2:
    """Build path cards only from genuine, same-link TraceGraph edges."""

    def __init__(self, runs: Iterable[Any]) -> None:
        if isinstance(runs, (str, bytes)):
            raise ValueError("runs must be an iterable of RunResult snapshots")
        self.graph = TraceGraph()
        try:
            for run in runs:
                self.graph.add_run(run)
        except TypeError as exc:
            raise ValueError("runs must be an iterable of RunResult snapshots") from exc
        graph = self.graph.to_dict()
        self._run_meta = {item["run_id"]: item for item in graph["runs"]}
        self._nodes = {item["id"]: item for item in graph["nodes"]}
        self._edges = {item["id"]: item for item in graph["edges"]}

    def retrieve(self, question: str, run_ids: list[str], max_cards: int = 24, max_chars: int = 12000) -> dict[str, Any]:
        self._validate(question, run_ids, max_cards, max_chars)
        intent = self._intent(question)
        targets, target_issue = self._targets(question)
        check_type = self._check_type(question, run_ids)
        plan = {"intent": intent, "target_ids": targets, "check_type": check_type, "run_ids": list(run_ids)}
        if intent is None:
            return self._abstain(question, run_ids, plan, "unsupported_question")
        if target_issue and not (intent == "reverse" and self._reverse_without_target(question, targets)):
            return self._abstain(question, run_ids, plan, target_issue)
        if intent == "reverse":
            if len(run_ids) != 1 or not check_type or targets:
                return self._abstain(question, run_ids, plan, "unsupported_question")
            cards, reason = self._reverse(run_ids[0], check_type)
        elif intent == "compare":
            if len(run_ids) != 2 or len(targets) != 1:
                return self._abstain(question, run_ids, plan, "unsupported_question")
            first, second = (self._run_meta[item] for item in run_ids)
            if first["case_id"] != second["case_id"] or first["template_id"] != second["template_id"]:
                return self._abstain(question, run_ids, plan, "incompatible_runs")
            cards, reason = self._compare(run_ids, targets[0])
        else:
            if len(run_ids) != 1 or len(targets) != 1:
                return self._abstain(question, run_ids, plan, "unsupported_question")
            cards, reason = (self._declarations(run_ids[0], targets[0]) if intent == "declarations"
                             else self._impact(run_ids[0], targets[0]))
        if reason:
            return self._abstain(question, run_ids, plan, reason)
        context = [{"id": card["id"], "text": card["text"]} for card in cards]
        context_chars = len(json.dumps(context, ensure_ascii=False, separators=(",", ":")))
        if len(cards) > max_cards or context_chars > max_chars:
            return self._abstain(question, run_ids, plan, "context_budget_exceeded", truncated=True)
        return {"schema_version": SCHEMA_VERSION, "mode": "graph_v2", "question": question,
                "run_ids": list(run_ids), "status": "ready", "reason_code": None, "plan": plan,
                "cards": deepcopy(cards), "context": context,
                "required_evidence_ids": [card["id"] for card in cards], "context_chars": context_chars,
                "truncated": False}

    def _validate(self, question: str, run_ids: list[str], max_cards: int, max_chars: int) -> None:
        if not isinstance(question, str) or not question.strip():
            raise ValueError("question must be a non-empty string")
        if not isinstance(run_ids, list) or not run_ids or any(not isinstance(x, str) or not x for x in run_ids):
            raise ValueError("run_ids must be a non-empty list of strings")
        if len(set(run_ids)) != len(run_ids):
            raise ValueError("run_ids must not contain duplicates")
        if any(x not in self._run_meta for x in run_ids):
            raise ValueError("unknown run_id")
        if not isinstance(max_cards, int) or isinstance(max_cards, bool) or max_cards < 0:
            raise ValueError("max_cards must be a non-negative integer")
        if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 2:
            raise ValueError("max_chars must be an integer of at least 2")

    def _intent(self, question: str) -> str | None:
        q = question.lower()
        # Check type names are data, not claims.  A name such as
        # certified_template_integrity must not be mistaken for certification.
        scope_text = q
        for node in self._nodes.values():
            if node["type"] == "check" and isinstance(node.get("check_type"), str):
                scope_text = re.sub(re.escape(node["check_type"].lower()), " ", scope_text)
        # This is intentionally a finite, conservative vocabulary.  It catches
        # common requests for causal, certification, or dynamic-performance
        # conclusions; it is not a general Chinese semantic classifier.
        if any(word in scope_text for word in (
            "因果", "原因", "为什么", "证明", "故障根因", "认证", "量产", "生产",
            "性能", "仿真曲线", "动态响应", "cause", "certif", "production", "root cause",
        )):
            return None
        if any(word in q for word in ("比较", "对比", "compare", "difference", "版本差异")):
            return "compare"
        if any(word in q for word in ("反查", "追溯", "反向", "reverse")) and any(word in q for word in ("需求", "路径", "requirement", "trace")):
            return "reverse"
        if any(word in q for word in ("影响", "impact", "检查", "check")):
            return "impact"
        if any(word in q for word in ("声明", "对应", "关联", "元素", "模型", "sysml", "modelica", "需求", "trace")):
            return "declarations"
        return None

    def _targets(self, question: str) -> tuple[list[str], str | None]:
        matches: list[tuple[str, int, int]] = []
        for name, aliases in PARAMETER_ALIASES.items():
            for alias in aliases:
                flags = re.I if re.fullmatch(r"[A-Za-z0-9_: ]+", alias) else re.I
                pattern = (r"(?<![A-Za-z0-9_])" + re.escape(alias) + r"(?![A-Za-z0-9_])"
                           if re.fullmatch(r"[A-Za-z0-9_: ]+", alias) else re.escape(alias))
                matches.extend((name, item.start(), item.end()) for item in re.finditer(pattern, question, flags))
        # A longer recognised alias owns every shorter overlapping alias.  Thus
        # "shared resistance" cannot also select generic "resistance".
        retained = [item for item in matches if not any(
            other[2] - other[1] > item[2] - item[1]
            and other[1] <= item[1] and other[2] >= item[2]
            for other in matches
        )]
        found = sorted({name for name, _, _ in retained})
        known = {a.lower() for aliases in PARAMETER_ALIASES.values() for a in aliases}
        known.update(str(node.get("check_type", "")).lower() for node in self._nodes.values() if node["type"] == "check")
        unknown = [token for token in _UNKNOWN_IDENTIFIER.findall(question) if token.lower() not in known]
        if unknown:
            return [], "unknown_target"
        if len(set(found)) > 1:
            return [], "ambiguous_target"
        if not found and any(word in question for word in _AMBIGUOUS):
            return [], "ambiguous_target"
        return (["parameter:" + found[0]] if found else []), ("unknown_target" if not found else None)

    def _reverse_without_target(self, question: str, targets: list[str]) -> bool:
        """A reverse request names a check, so it legitimately has no parameter."""
        if targets or any(word in question for word in _AMBIGUOUS):
            return False
        known_checks = {str(node.get("check_type", "")).lower() for node in self._nodes.values()
                        if node["type"] == "check"}
        identifiers = _UNKNOWN_IDENTIFIER.findall(question)
        return all(token.lower() in known_checks for token in identifiers)

    def _check_type(self, question: str, run_ids: list[str]) -> str | None:
        values = {node.get("check_type") for node in self._nodes.values() if node["run_id"] in run_ids and node["type"] == "check"}
        matched = [value for value in values if isinstance(value, str) and _contains(question, value)]
        return matched[0] if len(matched) == 1 else None

    def _abstain(self, question: str, run_ids: list[str], plan: dict[str, Any], reason: str, truncated: bool = False) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION, "mode": "graph_v2", "question": question,
                "run_ids": list(run_ids), "status": "abstained", "reason_code": reason,
                "plan": deepcopy(plan), "cards": [], "context": [], "required_evidence_ids": [],
                "context_chars": len(json.dumps([], ensure_ascii=False, separators=(",", ":"))), "truncated": truncated}

    def _edges_for(self, run_id: str, target_id: str, relation: str | None = None) -> list[dict[str, Any]]:
        return [edge for edge in self._edges.values() if edge["run_id"] == run_id and edge["provenance"].get("target_id") == target_id and (relation is None or edge["relation"] == relation)]

    def _card(self, run_id: str, nodes: list[str], edges: list[str], text: str, **data: Any) -> dict[str, Any]:
        payload = {"run_id": run_id, "node_ids": nodes, "edge_ids": edges, "text": text, "data": data}
        return {"id": _stable_id(payload), "kind": "path", **payload}

    def _declarations(self, run_id: str, target_id: str) -> tuple[list[dict[str, Any]], str | None]:
        binding = self._edges_for(run_id, target_id, "explicitly_bound_to")
        cards: list[dict[str, Any]] = []
        for edge in binding:
            link = edge["link_evidence_sha256"]
            elements = [e for e in self._edges_for(run_id, target_id, "structurally_represented_by")
                        if e["link_evidence_sha256"] == link and e["source"] == edge["target"]
                        and e["provenance"].get("requirement_id") == edge["provenance"].get("requirement_id")
                        and e["provenance"].get("target_id") == edge["provenance"].get("target_id")]
            layers = {self._nodes[e["target"]].get("layer") for e in elements}
            if not {"sysml", "modelica"}.issubset(layers):
                return [], "missing_evidence"
            req = self._nodes[edge["source"]]
            for element_edge in elements:
                element = self._nodes[element_edge["target"]]
                text = (f"run {run_id}; requirement {req['requirement_id']}: {req['text']}; "
                        f"target {target_id}; layer {element['layer']}; exact_selector {element['exact_selector']}")
                cards.append(self._card(run_id, [edge["source"], edge["target"], element_edge["target"]],
                                        [edge["id"], element_edge["id"]], text, requirement_id=req["requirement_id"], target_id=target_id, layer=element["layer"], exact_selector=element["exact_selector"]))
        return (cards, None) if cards else ([], "target_absent")

    def _impact(self, run_id: str, target_id: str) -> tuple[list[dict[str, Any]], str | None]:
        binding = self._edges_for(run_id, target_id, "explicitly_bound_to")
        element_edges = self._edges_for(run_id, target_id, "structurally_represented_by")
        check_edges = self._edges_for(run_id, target_id, "explicitly_checked_by")
        if not binding:
            return [], "target_absent"
        if not element_edges or not check_edges:
            return [], "missing_evidence"
        for bound in binding:
            linked = [edge for edge in element_edges if edge["link_evidence_sha256"] == bound["link_evidence_sha256"]
                      and edge["source"] == bound["target"]
                      and edge["provenance"].get("requirement_id") == bound["provenance"].get("requirement_id")]
            layers = {self._nodes[edge["target"]].get("layer") for edge in linked}
            if not {"sysml", "modelica"}.issubset(layers):
                return [], "missing_evidence"
        cards: list[dict[str, Any]] = []
        for edge in element_edges + check_edges:
            node = self._nodes[edge["target"]]
            if edge["relation"] == "structurally_represented_by":
                text = (f"run {run_id}; target {target_id}; layer {node['layer']}; "
                        f"element_id {node['element_id']}; exact_selector {node['exact_selector']}; structural association only.")
            else:
                text = (f"run {run_id}; target {target_id}; check_type {node['check_type']}; "
                        f"status {node['status']}; explicit check association only.")
            cards.append(self._card(run_id, [edge["source"], edge["target"]], [edge["id"]], text, target_id=target_id, relation=edge["relation"]))
        return cards, None

    def _reverse(self, run_id: str, check_type: str) -> tuple[list[dict[str, Any]], str | None]:
        checks = [e for e in self._edges.values() if e["run_id"] == run_id and e["relation"] == "explicitly_checked_by" and self._nodes[e["target"]].get("check_type") == check_type]
        cards: list[dict[str, Any]] = []
        for check in checks:
            matches = [e for e in self._edges.values() if e["run_id"] == run_id and e["relation"] == "explicitly_bound_to"
                       and e["link_evidence_sha256"] == check["link_evidence_sha256"]
                       and e["target"] == check["source"]
                       and e["provenance"].get("requirement_id") == check["provenance"].get("requirement_id")
                       and e["provenance"].get("target_id") == check["provenance"].get("target_id")]
            for binding in matches:
                req = self._nodes[binding["source"]]
                target_id = binding["provenance"]["target_id"]
                text = (f"run {run_id}; requirement {req['requirement_id']}: {req['text']}; target {target_id}; "
                        f"check {check_type}. This is an explicit multi-hop association, not root cause.")
                cards.append(self._card(run_id, [binding["source"], binding["target"], check["target"]], [binding["id"], check["id"]], text, requirement_id=req["requirement_id"], check_type=check_type))
        return (cards, None) if cards else ([], "missing_evidence")

    def _compare(self, run_ids: list[str], target_id: str) -> tuple[list[dict[str, Any]], str | None]:
        cards: list[dict[str, Any]] = []
        for run_id in run_ids:
            declaration, reason = self._declarations(run_id, target_id)
            if reason:
                return [], reason
            cards.extend(declaration)
        return cards, None
