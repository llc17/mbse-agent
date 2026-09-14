from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from week10.src import trace_cli


class _FakeGraph:
    def __init__(self):
        self.ids = []

    def add_run(self, run):
        self.ids.append(run.run_id)
        return run.run_id

    def to_dict(self): return {"graph": self.ids}
    def query_requirement(self, run_id, requirement_id): return {"trace": [run_id, requirement_id]}
    def impact(self, run_id, target_id): return {"impact": [run_id, target_id]}
    def reverse_check(self, run_id, check_type): return {"reverse": [run_id, check_type]}
    def compare_runs(self, before, after): return {"compare": [before, after]}


class TraceCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.results = self.root / "week10" / "results"
        self.folder = self.results / "public"
        self.folder.mkdir(parents=True)
        self.run_path = self.folder / "run.json"
        self.payload = {"run_id": "run-1"}
        self.run_path.write_text(json.dumps(self.payload), encoding="utf-8")
        self.manifest = self.folder / "manifest.json"
        self._write_manifest()
        self.roots = patch.multiple(trace_cli, PROJECT_ROOT=self.root, RESULTS_ROOT=self.results)
        self.roots.start()
        self.addCleanup(self.roots.stop)
        self.addCleanup(self.temp.cleanup)

    def _write_manifest(self, **overrides):
        entry = {"run_id": "run-1", "path": "week10/results/public/run.json",
                 "sha256": hashlib.sha256(self.run_path.read_bytes()).hexdigest()}
        manifest = {"schema_version": trace_cli.MANIFEST_SCHEMA, "public_data_only": True, "runs": [entry]}
        manifest.update(overrides)
        self.manifest.write_text(json.dumps(manifest), encoding="utf-8")

    @staticmethod
    def _run(data):
        link = SimpleNamespace(requirement_id="REQ-1", requirement_text="文本", target_id="parameter:x", status="VERIFIED")
        return SimpleNamespace(run_id=data["run_id"], traceability=SimpleNamespace(links=[link]))

    def test_load_rejects_bad_hash_before_deserialization(self):
        self._write_manifest(runs=[{"run_id": "run-1", "path": "week10/results/public/run.json", "sha256": "0" * 64}])
        with patch("week9.src.schemas.RunResult.from_dict") as parse:
            with self.assertRaisesRegex(ValueError, "hash mismatch"):
                trace_cli.load_runs(self.manifest)
        parse.assert_not_called()

    def test_load_rejects_escape_duplicate_and_missing_public_marker(self):
        cases = [
            ({"runs": [{"run_id": "run-1", "path": "week10/results/../secret.json", "sha256": "a" * 64}]}, "escapes"),
            ({"runs": [{"run_id": "run-1", "path": "week10/results/public/run.json", "sha256": "a" * 64}, {"run_id": "run-1", "path": "week10/results/public/run.json", "sha256": "a" * 64}]}, "duplicate"),
            ({"public_data_only": False}, "not marked public"),
        ]
        for changes, message in cases:
            with self.subTest(message=message):
                self._write_manifest(**changes)
                with self.assertRaisesRegex(ValueError, message):
                    trace_cli.load_runs(self.manifest)

    def test_dispatch_uses_validated_runs_and_requirements_need_no_graph(self):
        with patch("week9.src.schemas.RunResult.from_dict", side_effect=self._run):
            args = SimpleNamespace(manifest=self.manifest, command="requirements")
            output = trace_cli.dispatch(args)
        self.assertEqual(output["runs"][0]["requirements"][0]["requirement_id"], "REQ-1")

    def test_command_dispatches_to_graph(self):
        with patch("week9.src.schemas.RunResult.from_dict", side_effect=self._run), patch.object(trace_cli, "_graph", return_value=_FakeGraph()):
            args = SimpleNamespace(manifest=self.manifest, command="compare", before="before", after="after")
            self.assertEqual(trace_cli.dispatch(args), {"compare": ["before", "after"]})

    def test_main_emits_compact_json_error(self):
        stderr = io.StringIO()
        with patch.object(trace_cli.sys, "stderr", stderr):
            code = trace_cli.main(["--manifest", str(self.manifest), "trace"])
        self.assertEqual(code, 2)
        self.assertIn("error", json.loads(stderr.getvalue()))


if __name__ == "__main__":
    unittest.main()
