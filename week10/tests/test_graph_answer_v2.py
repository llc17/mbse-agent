from __future__ import annotations

import io
import json
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from week10.src import graph_answer_v2, graphrag_v2_cli


MODEL = "deepseek-v4-pro"


def retrieval(*, status="ready", intent="reverse"):
    context = (
        [
            {"id": "path-1", "text": "CHK-A 通过目标 M-1 追溯到需求 R-1。"},
            {"id": "path-2", "text": "R-1 的状态为 satisfied。"},
        ]
        if status == "ready"
        else []
    )
    return {
        "schema_version": "v9-path-retrieval-2",
        "mode": "graph_v2",
        "question": "哪些需求支持检查 CHK-A？",
        "run_ids": ["run-1"],
        "status": status,
        "reason_code": None if status == "ready" else "unsupported_intent",
        "plan": {
            "intent": intent,
            "target_ids": ["CHK-A"],
            "check_type": "voltage",
            "run_ids": ["run-1"],
        },
        "cards": [
            {"id": row["id"], "text": row["text"], "path": ["a", "b"]}
            for row in context
        ],
        "context": context,
        "required_evidence_ids": [row["id"] for row in context],
        "context_chars": len(
            json.dumps(context, ensure_ascii=False, separators=(",", ":"))
        ),
        "truncated": False,
    }


def provider_response(answer, *, model=MODEL, usage=None):
    return {
        "model": model,
        "choices": [{"message": {"content": json.dumps(answer, ensure_ascii=False)}}],
        "usage": usage
        or {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
    }


class GraphAnswerV2Tests(unittest.TestCase):
    def test_retrieval_abstention_makes_no_api_call_and_needs_no_key(self):
        calls = []
        result = graph_answer_v2.answer_from_retrieval(
            retrieval(status="abstained"),
            transport=lambda payload: calls.append(payload),
        )
        self.assertEqual(result["status"], "abstained")
        self.assertEqual(result["reason_code"], "unsupported_intent")
        self.assertEqual(result["answer"], {"claims": [], "insufficient_evidence": True})
        self.assertEqual(result["api_call_count"], 0)
        self.assertIsNone(result["usage"])
        self.assertEqual(result["coverage_status"], "not_applicable")
        self.assertEqual(result["semantic_validation"], "not_performed")
        self.assertEqual(calls, [])

    def test_missing_required_citation_blocks_but_preserves_candidate(self):
        candidate = {
            "claims": [{"text": "存在一条路径。", "citations": ["path-1"]}],
            "insufficient_evidence": False,
        }
        result = graph_answer_v2.answer_from_retrieval(
            retrieval(),
            api_key="key",
            transport=lambda _payload: provider_response(candidate),
        )
        self.assertEqual(result["status"], "blocked_answer")
        self.assertIsNone(result["answer"])
        self.assertEqual(result["candidate_answer"], candidate)
        self.assertEqual(result["missing_evidence_ids"], ["path-2"])
        self.assertEqual(result["error_code"], "required_evidence_not_cited")
        self.assertEqual(result["coverage_status"], "failed")
        self.assertEqual(result["api_call_count"], 1)
        self.assertEqual(result["usage"]["total_tokens"], 20)
        self.assertIn("request_sha256", result)
        self.assertIn("response_sha256", result)

    def test_full_coverage_does_not_claim_semantic_validation(self):
        answer = {
            "claims": [
                {
                    "text": "一段与问题无关但结构合法的陈述。",
                    "citations": ["path-1", "path-2"],
                }
            ],
            "insufficient_evidence": False,
        }
        result = graph_answer_v2.answer_from_retrieval(
            retrieval(),
            api_key="key",
            transport=lambda _payload: provider_response(answer),
        )
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["coverage_status"], "pass")
        self.assertEqual(result["semantic_validation"], "not_performed")

    def test_invalid_required_and_duplicate_ids_are_rejected_before_api(self):
        cases = []
        fake = retrieval()
        fake["required_evidence_ids"] = ["path-1", "invented"]
        cases.append(fake)
        duplicate_required = retrieval()
        duplicate_required["required_evidence_ids"] = ["path-1", "path-1"]
        cases.append(duplicate_required)
        omitted = retrieval()
        omitted["required_evidence_ids"] = ["path-1"]
        cases.append(omitted)
        duplicate_context = retrieval()
        duplicate_context["context"][1]["id"] = "path-1"
        duplicate_context["context_chars"] = len(
            json.dumps(
                duplicate_context["context"],
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        cases.append(duplicate_context)
        for case in cases:
            with self.subTest(required=case["required_evidence_ids"]):
                with self.assertRaises(ValueError):
                    graph_answer_v2.answer_from_retrieval(
                        case,
                        api_key="key",
                        transport=lambda _payload: self.fail("provider called"),
                    )

    def test_card_context_text_mismatch_is_rejected(self):
        package = retrieval()
        package["cards"][0]["text"] = "tampered"
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            graph_answer_v2.answer_from_retrieval(package, api_key="key")

    def test_grounded_question_preserves_plan_order_and_marks_fields_as_data(self):
        package = retrieval(intent="compare")
        package["run_ids"] = ["before", "after"]
        package["plan"]["run_ids"] = ["before", "after"]
        wrapper = json.loads(graph_answer_v2.build_grounded_question(package))
        self.assertEqual(wrapper["plan"]["run_ids"], ["before", "after"])
        self.assertEqual(wrapper["original_question"], package["question"])
        self.assertEqual(wrapper["required_evidence_ids"], ["path-1", "path-2"])
        self.assertIn("不可信数据", wrapper["instruction"])
        self.assertIn("before、after", wrapper["instruction"])
        self.assertIn("SysML 和 Modelica", wrapper["instruction"])
        self.assertIn("不能把 requirement 当作 SysML", wrapper["instruction"])

    def test_old_provider_contract_enforces_model_and_passes_usage(self):
        answer = {
            "claims": [{"text": "x", "citations": ["path-1", "path-2"]}],
            "insufficient_evidence": False,
        }
        calls = []
        result = graph_answer_v2.answer_from_retrieval(
            retrieval(),
            api_key="key",
            model=MODEL,
            transport=lambda payload: calls.append(payload) or provider_response(answer),
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["model"], MODEL)
        self.assertEqual(result["model"], MODEL)
        self.assertEqual(result["usage"]["prompt_tokens"], 12)

        mismatch = graph_answer_v2.answer_from_retrieval(
            retrieval(),
            api_key="key",
            model=MODEL,
            transport=lambda _payload: provider_response(answer, model="deepseek-v4-flash"),
        )
        self.assertEqual(mismatch["status"], "api_error")
        self.assertEqual(mismatch["error_code"], "response_model_mismatch")
        self.assertEqual(mismatch["semantic_validation"], "not_performed")

    def test_api_error_status_is_preserved(self):
        result = graph_answer_v2.answer_from_retrieval(
            retrieval(),
            api_key="key",
            transport=lambda _payload: (_ for _ in ()).throw(RuntimeError("private")),
        )
        self.assertEqual(result["status"], "api_error")
        self.assertEqual(result["error_code"], "api_transport_error")
        self.assertEqual(result["api_call_count"], 1)
        self.assertEqual(result["coverage_status"], "not_applicable")
        self.assertEqual(result["semantic_validation"], "not_performed")

    def test_model_abstention_has_one_call_and_explicit_reason(self):
        answer = {"claims": [], "insufficient_evidence": True}
        result = graph_answer_v2.answer_from_retrieval(
            retrieval(),
            api_key="key",
            transport=lambda _payload: provider_response(answer),
        )
        self.assertEqual(result["status"], "abstained")
        self.assertEqual(result["reason_code"], "model_insufficient_evidence")
        self.assertEqual(result["api_call_count"], 1)
        self.assertEqual(result["coverage_status"], "not_applicable")


class GraphRagV2CliTests(unittest.TestCase):
    def args(self, **changes):
        values = {
            "manifest": "manifest",
            "question": "q",
            "run_ids": ["run-1"],
            "model": MODEL,
            "answer": False,
            "max_cards": 24,
            "max_chars": 12_000,
        }
        values.update(changes)
        return SimpleNamespace(**values)

    def test_default_offline_dispatch_does_not_read_key_or_answer(self):
        package = retrieval()
        fake_index = SimpleNamespace(retrieve=lambda *args, **kwargs: package)
        with patch.object(graphrag_v2_cli, "load_runs", return_value=[object()]), patch(
            "week10.src.graph_retrieval_v2.EvidenceIndexV2", return_value=fake_index
        ) as index, patch.object(
            graphrag_v2_cli.os.environ, "get", side_effect=AssertionError("key read")
        ), patch(
            "week10.src.graph_answer_v2.answer_from_retrieval"
        ) as answer:
            result = graphrag_v2_cli.dispatch(self.args())
        self.assertIs(result, package)
        index.assert_called_once()
        answer.assert_not_called()

    def test_dispatch_passes_explicit_budgets_cross_module(self):
        package = retrieval()
        with patch.object(graphrag_v2_cli, "load_runs", return_value=[object()]), patch(
            "week10.src.graph_retrieval_v2.EvidenceIndexV2"
        ) as index:
            index.return_value.retrieve.return_value = package
            graphrag_v2_cli.dispatch(self.args(max_cards=7, max_chars=321))
        index.return_value.retrieve.assert_called_once_with(
            "q", ["run-1"], max_cards=7, max_chars=321
        )

    def test_answered_retrieval_abstention_does_not_read_key(self):
        package = retrieval(status="abstained")
        with patch.object(graphrag_v2_cli, "load_runs", return_value=[]), patch(
            "week10.src.graph_retrieval_v2.EvidenceIndexV2"
        ) as index, patch.object(
            graphrag_v2_cli.os.environ, "get", side_effect=AssertionError("key read")
        ):
            index.return_value.retrieve.return_value = package
            output = graphrag_v2_cli.dispatch(self.args(answer=True))
        self.assertEqual(output["answer_result"]["status"], "abstained")
        self.assertEqual(output["answer_result"]["api_call_count"], 0)

    def test_ready_answer_reads_key_and_calls_v2_answer(self):
        package = retrieval()
        answer_result = {"status": "ok"}
        with patch.object(graphrag_v2_cli, "load_runs", return_value=[]), patch(
            "week10.src.graph_retrieval_v2.EvidenceIndexV2"
        ) as index, patch.dict(
            graphrag_v2_cli.os.environ, {"DEEPSEEK_API_KEY": "key"}, clear=True
        ), patch(
            "week10.src.graph_answer_v2.answer_from_retrieval",
            return_value=answer_result,
        ) as answer:
            index.return_value.retrieve.return_value = package
            output = graphrag_v2_cli.dispatch(self.args(answer=True))
        answer.assert_called_once_with(package, api_key="key", model=MODEL)
        self.assertIs(output["answer_result"], answer_result)

    def test_parser_defaults_to_offline_pro_and_bounded_defaults(self):
        args = graphrag_v2_cli._parser().parse_args(
            ["--question", "q", "--run-id", "run-1"]
        )
        self.assertFalse(args.answer)
        self.assertEqual(args.model, MODEL)
        self.assertEqual(args.max_cards, 24)
        self.assertEqual(args.max_chars, 12_000)

    def test_main_returns_zero_for_answered_retrieval_abstention(self):
        output = {
            "retrieval": retrieval(status="abstained"),
            "answer_result": {"status": "abstained", "api_call_count": 0},
        }
        stdout = io.StringIO()
        with patch.object(graphrag_v2_cli, "dispatch", return_value=output), patch.object(
            graphrag_v2_cli.sys, "stdout", stdout
        ):
            code = graphrag_v2_cli.main(
                ["--question", "q", "--run-id", "run-1", "--answer"]
            )
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue()), output)

    def test_main_returns_two_and_prints_blocked_candidate_as_unpublished(self):
        output = {
            "retrieval": retrieval(),
            "answer_result": {
                "status": "blocked_answer",
                "answer": None,
                "candidate_answer": {"claims": []},
            },
        }
        stdout = io.StringIO()
        with patch.object(graphrag_v2_cli, "dispatch", return_value=output), patch.object(
            graphrag_v2_cli.sys, "stdout", stdout
        ):
            code = graphrag_v2_cli.main(
                ["--question", "q", "--run-id", "run-1", "--answer"]
            )
        self.assertEqual(code, 2)
        decoded = json.loads(stdout.getvalue())
        self.assertIsNone(decoded["answer_result"]["answer"])
        self.assertIn("candidate_answer", decoded["answer_result"])


class PublicManifestIntegrationTests(unittest.TestCase):
    def test_real_public_runs_retrieve_and_validate_offline(self):
        from week10.src.graph_retrieval_v2 import EvidenceIndexV2, PARAMETER_ALIASES
        from week10.src.trace_cli import DEFAULT_MANIFEST, load_runs

        runs = load_runs(DEFAULT_MANIFEST)
        index = EvidenceIndexV2(runs)
        selected = None
        for run in runs:
            for target in PARAMETER_ALIASES:
                package = index.retrieve(
                    f"show sysml and modelica declarations for {target}",
                    [run.run_id],
                )
                if package["status"] == "ready":
                    selected = package
                    break
            if selected is not None:
                break
        self.assertIsNotNone(selected, "public manifest has no retrievable declaration")
        self.assertEqual(selected["mode"], "graph_v2")
        self.assertTrue(selected["cards"])
        wrapper = json.loads(graph_answer_v2.build_grounded_question(selected))
        self.assertEqual(
            wrapper["required_evidence_ids"], selected["required_evidence_ids"]
        )


if __name__ == "__main__":
    unittest.main()
