"""Local, evidence-bounded retrieval over the public V9 trace graph."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import re
from collections import Counter, deque
from typing import Any, Iterable

from week10.src.trace_graph import TraceGraph


_ASCII = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_ZH = re.compile(r"[\u4e00-\u9fff]+")
_STOP = frozenset({"什么", "哪些", "哪个", "如何", "有关", "相关", "情况", "信息", "证据", "请问", "查看", "查询", "the", "and", "for", "with", "about", "what", "which", "show", "tell"})
_TYPE_LABEL = {"requirement": "需求", "target": "目标", "element": "模型元素", "artifact": "证据工件", "check": "检查"}
_TARGET_LABEL = {"parameter": "参数", "constraint": "约束", "topology": "拓扑"}
_RELATION_LABEL = {
    "explicitly_bound_to": "明确绑定到",
    "sourced_from_exact_span": "来源于精确文本范围",
    "structurally_represented_by": "由模型元素结构表示",
    "evidenced_in_artifact": "在工件中取证",
    "explicitly_checked_by": "由检查明确验证",
}


def _terms(text: str) -> list[str]:
    """Identifiers and Chinese bigrams give a small, deterministic local vocabulary."""
    if not isinstance(text, str):
        return []
    result: list[str] = []
    for item in _ASCII.findall(text):
        lowered = item.lower()
        result.append(lowered)
        # Keep the exact identifier and its stable underscore components.  This
        # permits a user to ask for either ``input_voltage`` or "input voltage".
        result.extend(part for part in lowered.split("_") if len(part) > 1)
    for group in _ZH.findall(text):
        result.extend(group[i:i + 2] for i in range(max(0, len(group) - 1)))
        if len(group) == 1:
            result.append(group)
    return [item for item in result if item not in _STOP and len(item) > 1]


def _short_id(kind: str, graph_id: str, seen: set[str]) -> str:
    digest = hashlib.sha256((kind + "\0" + graph_id).encode("utf-8")).hexdigest()
    for size in range(12, len(digest) + 1, 4):
        candidate = f"{kind[:1]}_{digest[:size]}"
        if candidate not in seen:
            seen.add(candidate)
            return candidate
    raise ValueError("card id collision")


def _node_text(node: dict[str, Any]) -> str:
    kind = node["type"]
    if kind == "requirement":
        return f"需求 {node['requirement_id']}：{node['text']}"
    if kind == "target":
        target = node["target_id"]
        prefix, _, canonical = target.partition(":")
        return f"目标（{_TARGET_LABEL.get(prefix, prefix)}）：{canonical or target}"
    if kind == "element":
        return (f"模型元素（{node['layer']}）：{node['element_id']}；"
                f"精确选择器：{node['exact_selector']}；类型：{node['kind']}")
    if kind == "artifact":
        return f"证据工件（{node['artifact_type']}）：sha256={node['sha256']}"
    if kind == "check":
        return (f"检查：{node['check_type']}；状态：{node['status']}；"
                f"必需：{node['required']}")
    return f"{_TYPE_LABEL.get(kind, kind)}：{node['id']}"


class EvidenceIndex:
    """A flat corpus plus a bounded graph traversal over one validated graph."""

    def __init__(self, runs: Iterable[Any]) -> None:
        self.graph = TraceGraph()
        for run in runs:
            self.graph.add_run(run)
        graph = self.graph.to_dict()
        self._run_ids = {item["run_id"] for item in graph["runs"]}
        nodes = {item["id"]: item for item in graph["nodes"]}
        seen: set[str] = set()
        cards: list[dict[str, Any]] = []
        for graph_id in sorted(nodes):
            node = nodes[graph_id]
            cards.append({"id": _short_id("node", graph_id, seen), "run_id": node["run_id"],
                          "graph_id": graph_id, "kind": "node",
                          "text": f"运行 {node['run_id']}：{_node_text(node)}",
                          "data": deepcopy(node)})
        self._node_cards = {card["graph_id"]: card for card in cards}
        self._edges: dict[str, dict[str, Any]] = {}
        for edge in sorted(graph["edges"], key=lambda item: item["id"]):
            source, target = nodes[edge["source"]], nodes[edge["target"]]
            relation = _RELATION_LABEL.get(edge["relation"], edge["relation"])
            text = (f"运行 {edge['run_id']}：{_node_text(source)} "
                    f"{relation} {_node_text(target)}")
            card = {"id": _short_id("edge", edge["id"], seen), "run_id": edge["run_id"],
                    "graph_id": edge["id"], "kind": "edge", "text": text,
                    "data": deepcopy(edge)}
            cards.append(card)
            self._edges[edge["id"]] = edge
        self.cards = sorted(cards, key=lambda item: item["id"])
        self._cards_by_graph_id = {item["graph_id"]: item for item in self.cards}
        self._adj: dict[str, list[tuple[str, str]]] = {key: [] for key in nodes}
        for edge_id, edge in self._edges.items():
            self._adj[edge["source"]].append((edge_id, edge["target"]))
            self._adj[edge["target"]].append((edge_id, edge["source"]))
        for value in self._adj.values():
            value.sort()
        self._doc_terms = {card["id"]: Counter(_terms(card["text"])) for card in self.cards}
        self._df = Counter(term for counts in self._doc_terms.values() for term in counts)

    def _validate(self, question: str, run_ids: list[str], mode: str, max_cards: int, max_chars: int) -> list[str]:
        if not isinstance(question, str):
            raise ValueError("question must be a string")
        if mode not in {"graph", "lexical"}:
            raise ValueError("mode must be graph or lexical")
        if not isinstance(run_ids, list) or any(not isinstance(item, str) or not item for item in run_ids):
            raise ValueError("run_ids must be a list of non-empty strings")
        if len(set(run_ids)) != len(run_ids):
            raise ValueError("run_ids must not contain duplicates")
        unknown = set(run_ids) - self._run_ids
        if unknown:
            raise ValueError("unknown run_id")
        if not isinstance(max_cards, int) or isinstance(max_cards, bool) or max_cards < 0:
            raise ValueError("max_cards must be a non-negative integer")
        if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars < 2:
            raise ValueError("max_chars must be an integer of at least 2")
        return sorted(run_ids)

    def _score(self, card: dict[str, Any], terms: list[str], allowed: set[str]) -> float:
        if card["run_id"] not in allowed:
            return 0.0
        counts = self._doc_terms[card["id"]]
        total = 0.0
        count = max(1, len(self.cards))
        for term in terms:
            if term in counts:
                total += (1.0 + math.log(counts[term])) * (math.log((count + 1) / (self._df[term] + 1)) + 1.0)
        return total

    @staticmethod
    def _context(cards: list[dict[str, Any]], max_cards: int, max_chars: int) -> tuple[list[dict[str, str]], list[dict[str, Any]], bool]:
        context: list[dict[str, str]] = []
        selected: list[dict[str, Any]] = []
        truncated = False
        for card in cards:
            if len(context) >= max_cards:
                truncated = True
                break
            candidate = context + [{"id": card["id"], "text": card["text"]}]
            encoded = json.dumps(candidate, ensure_ascii=False, separators=(",", ":"))
            if len(encoded) > max_chars:
                truncated = True
                continue
            context = candidate
            selected.append(card)
        return context, selected, truncated

    def retrieve(self, question: str, run_ids: list[str], mode: str = "graph", max_cards: int = 24, max_chars: int = 12000) -> dict[str, Any]:
        selected_runs = self._validate(question, run_ids, mode, max_cards, max_chars)
        allowed = set(selected_runs)
        terms = _terms(question)
        scored = [(self._score(card, terms, allowed), card) for card in self.cards]
        positive = [(score, card) for score, card in scored if score > 0]
        seeds: list[str] = []
        selected_graph: set[str] = set()
        if mode == "lexical":
            ordered = [card for score, card in sorted(positive, key=lambda item: (-item[0], item[1]["id"]))]
        else:
            node_scores = [(score, card) for score, card in positive if card["kind"] == "node"]
            seed_cards = [card for _, card in sorted(node_scores, key=lambda item: (-item[0], item[1]["graph_id"]))[:4]]
            seeds = [card["graph_id"] for card in seed_cards]
            distances = {seed: 0 for seed in seeds}
            queue = deque(seeds)
            selected_nodes = set(seeds)
            selected_edges: set[str] = set()
            while queue:
                current = queue.popleft()
                distance = distances[current]
                node_type = self._node_cards[current]["data"]["type"]
                if (distance >= 2 or node_type == "artifact"
                        or (node_type == "element" and current not in seeds)
                        or (node_type == "check" and distance > 0)):
                    continue
                for edge_id, other in self._adj[current]:
                    edge = self._edges[edge_id]
                    if edge["run_id"] not in allowed:
                        continue
                    selected_edges.add(edge_id)
                    selected_nodes.add(other)
                    if other not in distances:
                        distances[other] = distance + 1
                        queue.append(other)
            selected_graph = selected_nodes | selected_edges
            candidates = [self._cards_by_graph_id[item] for item in selected_graph]
            def graph_key(card: dict[str, Any]) -> tuple[float, int, str]:
                if card["kind"] == "node":
                    score = self._score(card, terms, allowed)
                    distance = distances.get(card["graph_id"], 3)
                else:
                    edge = card["data"]
                    score = max(self._score(self._node_cards[edge["source"]], terms, allowed), self._score(self._node_cards[edge["target"]], terms, allowed))
                    distance = min(distances.get(edge["source"], 3), distances.get(edge["target"], 3))
                return (-score, distance, card["id"])
            ordered = sorted(candidates, key=graph_key)
        context, selected, truncated = self._context(ordered, max_cards, max_chars)
        return {"mode": mode, "question": question, "run_ids": selected_runs,
                "cards": deepcopy(selected), "context": context,
                "context_chars": len(json.dumps(context, ensure_ascii=False, separators=(",", ":"))),
                "truncated": truncated, "seed_node_ids": seeds,
                "selected_graph_ids": [card["graph_id"] for card in selected] if mode == "graph" else [],
                "candidate_graph_ids": [card["graph_id"] for card in ordered] if mode == "graph" else []}
