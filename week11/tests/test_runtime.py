import json
import sqlite3
from contextlib import closing
from pathlib import Path
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import patch

from week11.src.runtime import DeepSeekRuntime, RuntimeError, build_system_prompt


ROOT = Path(__file__).resolve().parents[1]
TMP = ROOT / "results" / "tests-runtime-v10"


def config(path, **changes):
    data = json.loads((ROOT / "configs" / "runtime.json").read_text())
    data.update(changes); path.write_text(json.dumps(data), encoding="utf-8")


def response(model="deepseek-flash", prompt=10, completion=5):
    return {"model": model, "choices": [{"finish_reason": "tool_calls", "message": {"content": "ok", "tool_calls": [{"id": "x", "type": "function", "function": {"name": "project_list", "arguments": "{}"}}]}}], "usage": {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}}


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        TMP.mkdir(parents=True, exist_ok=True)
        self._temporary = tempfile.TemporaryDirectory(dir=TMP, ignore_cleanup_errors=True)
        self.tmp = Path(self._temporary.name)
        self.cfg = self.tmp / "runtime.json"; config(self.cfg)
        self.calls = 0
    def tearDown(self): self._temporary.cleanup()
    def runtime(self, result=None):
        def mock(payload, key, timeout):
            self.calls += 1
            self.assertEqual(payload["thinking"], {"type": "disabled"}); self.assertEqual(payload["tool_choice"], "auto")
            if isinstance(result, Exception): raise result
            return result or response()
        return DeepSeekRuntime(self.cfg, self.tmp / "ledger.sqlite", transport=mock)
    def test_success_idempotency_and_prompt(self):
        runtime = self.runtime()
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test"}, clear=False): first = runtime.complete([{"role": "user", "content": "hello"}], [], "a")
        self.assertEqual(first["response_model"], "deepseek-flash")
        self.assertEqual(runtime.complete([{"role": "user", "content": "hello"}], [], "a"), first); self.assertEqual(self.calls, 1)
        with self.assertRaises(RuntimeError): runtime.complete([{"role": "user", "content": "different"}], [], "a")
        self.assertIn("thermal_dual_room", build_system_prompt())
    def test_usage_identity_and_transport_failure_are_blocking(self):
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "identity"):
                self.runtime(response(model="other")).complete([], [], "bad-model")
            def fail(*_):
                self.calls += 1
                raise OSError("mock network failure")
            transport_runtime = DeepSeekRuntime(self.cfg, self.tmp / "transport.sqlite", transport=fail)
            with self.assertRaisesRegex(RuntimeError, "transport failed"):
                transport_runtime.complete([], [], "failed")
        self.assertEqual(self.calls, 2)
        with self.assertRaisesRegex(RuntimeError, "prior failed or unknown"):
            transport_runtime.complete([], [], "failed")
        self.assertEqual(self.calls, 2)
    def test_budget_and_config_hash(self):
        config(self.cfg, max_requests=1)
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test"}, clear=False): runtime = self.runtime(); runtime.complete([], [], "one")
        with self.assertRaises(RuntimeError): runtime.complete([], [], "two")
        config(self.cfg, max_requests=2)
        with self.assertRaises(RuntimeError): DeepSeekRuntime(self.cfg, self.tmp / "ledger.sqlite", transport=lambda *_: response())

    def test_transport_diagnostic_keeps_numeric_cause_without_sensitive_text(self):
        def fail(*_):
            try:
                raise OSError(10013, "SECRET_KEY and private body")
            except OSError as cause:
                raise ConnectionError("https://private.example/SECRET_KEY") from cause
        runtime = DeepSeekRuntime(self.cfg, self.tmp / "diagnostic.sqlite", transport=fail)
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "test"}):
            with self.assertRaises(RuntimeError) as caught:
                runtime.complete([], [], "diagnostic-request")
        with closing(sqlite3.connect(runtime.ledger_path)) as db:
            row = db.execute("SELECT reason,actual_tokens,actual_cost,reserve_tokens FROM requests").fetchone()
        for text in (str(caught.exception), row[0]):
            self.assertIn("10013", text)
            self.assertIn("ConnectionError", text)
            self.assertNotIn("SECRET_KEY", text)
            self.assertNotIn("private", text)
        self.assertIsNone(row[1])
        self.assertIsNone(row[2])
        self.assertGreater(row[3], 0)

    def test_construction_does_not_load_credentials(self):
        with patch("week11.src.runtime._api_key") as key:
            DeepSeekRuntime(self.cfg, self.tmp / "no-key.sqlite", transport=lambda *_: response())
            key.assert_not_called()

    def test_parallel_different_ids_reserve_once_at_last_request(self):
        config(self.cfg, max_requests=1); ledger = self.tmp / "shared.sqlite"; calls = []
        def transport(*_): calls.append(1); return response()
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY":"test"}, clear=False):
            def invoke(name):
                try: return DeepSeekRuntime(self.cfg, ledger, transport=transport).complete([], [], name)
                except RuntimeError: return None
            with ThreadPoolExecutor(max_workers=2) as pool: results = list(pool.map(invoke, ("one", "two")))
        self.assertEqual(len(calls), 1); self.assertEqual(sum(x is not None for x in results), 1)
        import sqlite3
        with closing(sqlite3.connect(ledger)) as conn: self.assertEqual(conn.execute("select count(*) from requests").fetchone()[0], 1)

    def test_parallel_same_id_sends_once(self):
        ledger = self.tmp / "same.sqlite"; calls = []
        def transport(*_): calls.append(1); return response()
        first = DeepSeekRuntime(self.cfg, ledger, transport=transport); second = DeepSeekRuntime(self.cfg, ledger, transport=transport); gate = Barrier(2)
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY":"test"}, clear=False):
            def invoke(instance):
                gate.wait()
                try:
                    return instance.complete([], [], "same")
                except RuntimeError as exc:
                    self.assertTrue("already reserved" in str(exc) or "prior failed or unknown" in str(exc))
                    return None
            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(invoke, (first, second)))
        self.assertEqual(len(calls), 1)
        successes = [item for item in results if item is not None]
        self.assertGreaterEqual(len(successes), 1)
        self.assertTrue(all(item == successes[0] for item in successes))

    def test_freezes_after_transport_identity_and_usage_failures(self):
        ledger = self.tmp / "freeze.sqlite"; sent = []
        def failing(*_): sent.append(1); raise OSError("offline")
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY":"test"}, clear=False):
            for ident in ("a", "b"):
                with self.assertRaises(RuntimeError): DeepSeekRuntime(self.cfg, ledger, transport=failing).complete([], [], ident)
            with self.assertRaises(RuntimeError): DeepSeekRuntime(self.cfg, ledger, transport=failing).complete([], [], "c")
        self.assertEqual(len(sent), 2)
        ledger = self.tmp / "identity.sqlite"; sent = []
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY":"test"}, clear=False):
            with self.assertRaises(RuntimeError): DeepSeekRuntime(self.cfg, ledger, transport=lambda *_: (sent.append(1) or response(model="bad"))).complete([], [], "x")
            with self.assertRaises(RuntimeError): DeepSeekRuntime(self.cfg, ledger, transport=lambda *_: (sent.append(1) or response())).complete([], [], "y")
        self.assertEqual(len(sent), 1)
        ledger = self.tmp / "usage.sqlite"; over = response(prompt=999999, completion=5); sent = []
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY":"test"}, clear=False):
            with self.assertRaisesRegex(RuntimeError, "usage exceeded"): DeepSeekRuntime(self.cfg, ledger, transport=lambda *_: (sent.append(1) or over)).complete([], [], "u")
            with self.assertRaisesRegex(RuntimeError, "frozen"): DeepSeekRuntime(self.cfg, ledger, transport=lambda *_: (sent.append(1) or response())).complete([], [], "u2")
        self.assertEqual(sent, [1])
        import sqlite3
        with closing(sqlite3.connect(ledger)) as conn:
            self.assertEqual(conn.execute("select actual_tokens from requests where request_id='u'").fetchone()[0], 1000004)
            self.assertEqual(conn.execute("select value from meta where key='frozen'").fetchone()[0], "usage exceeded reservation")

    def test_token_cost_and_malformed_response_are_blocked_before_or_after_send(self):
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY":"test"}, clear=False):
            config(self.cfg, max_total_tokens=1)
            sent=[]
            with self.assertRaisesRegex(RuntimeError, "budget exhausted"): DeepSeekRuntime(self.cfg, self.tmp / "tokens.sqlite", transport=lambda *_: sent.append(1)).complete([], [], "tokens")
            self.assertEqual(sent, [])
            cfg = self.tmp / "cost.json"; config(cfg, max_usd=0.000001)
            with self.assertRaisesRegex(RuntimeError, "budget exhausted"): DeepSeekRuntime(cfg, self.tmp / "cost.sqlite", transport=lambda *_: sent.append(1)).complete([], [], "cost")
            self.assertEqual(sent, [])
            config(self.cfg)
            bad = response(); bad["choices"][0]["finish_reason"] = "length"
            with self.assertRaisesRegex(RuntimeError, "choices"): DeepSeekRuntime(self.cfg, self.tmp / "finish.sqlite", transport=lambda *_: bad).complete([], [], "finish")
            bad = response(); bad["choices"][0]["message"]["tool_calls"][0]["id"] = ""
            with self.assertRaisesRegex(RuntimeError, "tool call"): DeepSeekRuntime(self.cfg, self.tmp / "call.sqlite", transport=lambda *_: bad).complete([], [], "call")


if __name__ == "__main__": unittest.main()
