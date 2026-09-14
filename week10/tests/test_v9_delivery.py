from __future__ import annotations

import contextlib
import hashlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from week10.src import trace_cli, v9_cli
from week10.tools import build_portable_v9 as portable


class V9CliTests(unittest.TestCase):
    def test_forwards_remaining_argv_and_exit_code(self) -> None:
        cases = (
            ("evidence", "week10.src.graph_evidence_cli", 11),
            ("trace", "week10.src.trace_cli", 12),
            ("dependency", "week10.src.dependency_cli", 13),
            ("ask", "week10.src.graphrag_v2_cli", 14),
        )
        remaining = ["--manifest", "absolute manifest.json", "graph", "--sentinel"]
        for command, module_name, child_code in cases:
            with self.subTest(command=command):
                calls: list[list[str]] = []

                def fake_import(name: str) -> SimpleNamespace:
                    self.assertEqual(name, module_name)
                    return SimpleNamespace(
                        main=lambda argv: calls.append(argv) or child_code
                    )

                with patch.object(
                    v9_cli.importlib, "import_module", side_effect=fake_import
                ):
                    self.assertEqual(v9_cli.main([command, *remaining]), child_code)
                self.assertEqual(calls, [remaining])

    def test_does_not_enable_answer_or_change_evidence_defaults(self) -> None:
        calls: list[tuple[str, list[str]]] = []

        def fake_import(name: str) -> SimpleNamespace:
            return SimpleNamespace(
                main=lambda argv: calls.append((name, argv)) or 0
            )

        with patch.object(
            v9_cli.importlib, "import_module", side_effect=fake_import
        ):
            self.assertEqual(
                v9_cli.main(["ask", "--question", "q", "--run-id", "r"]), 0
            )
            self.assertEqual(
                v9_cli.main(["evidence", "--question", "q", "--run-id", "r"]), 0
            )
        self.assertEqual(calls[0][1], ["--question", "q", "--run-id", "r"])
        self.assertNotIn("--answer", calls[0][1])
        self.assertEqual(calls[1][1], ["--question", "q", "--run-id", "r"])
        self.assertNotIn("--format", calls[1][1])

    def test_help_states_semantic_limit(self) -> None:
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(v9_cli.main(["--help"]), 0)
        text = output.getvalue()
        self.assertIn("evidence", text)
        self.assertIn("dependency", text)
        self.assertIn("不构成语义正确性验证", text)


class PortableBundleTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name) / "project"
        self.results = self.root / "week10" / "results"
        self.deliveries = self.root / "week10" / "deliverables"
        self.results.mkdir(parents=True)
        self.deliveries.mkdir(parents=True)

        source_paths = (
            Path("week9/src/schemas.py"),
            Path("week10/src/v9_cli.py"),
        )
        patches = (
            patch.object(portable, "PROJECT_ROOT", self.root),
            patch.object(portable, "RESULTS_ROOT", self.results),
            patch.object(portable, "DELIVERABLES_ROOT", self.deliveries),
            patch.object(portable, "SOURCE_RELATIVE_PATHS", source_paths),
            patch.object(portable, "OPTIONAL_INIT_PATHS", ()),
            patch.object(trace_cli, "PROJECT_ROOT", self.root),
            patch.object(trace_cli, "RESULTS_ROOT", self.results),
        )
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

        for relative in source_paths:
            path = self.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                f"# safe source: {relative.as_posix()}\n", encoding="utf-8"
            )

    def _write_manifest(
        self,
        *,
        public: bool = True,
        run_path: str = "week10/results/stage/run.json",
    ) -> Path:
        run_file = self.root / Path(run_path)
        run_file.parent.mkdir(parents=True, exist_ok=True)
        raw = b'{"fixture":true}\n'
        run_file.write_bytes(raw)
        manifest = self.results / "stage" / "manifest.json"
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": trace_cli.MANIFEST_SCHEMA,
                    "public_data_only": public,
                    "runs": [
                        {
                            "run_id": "public-run",
                            "path": run_path,
                            "sha256": hashlib.sha256(raw).hexdigest(),
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        return manifest

    def test_rejects_outside_and_existing_destination(self) -> None:
        with self.assertRaisesRegex(ValueError, "under week10/deliverables"):
            portable.build_bundle(
                Path(tempfile.gettempdir()) / "outside-v9-bundle",
                [self.root / "unused.json"],
            )
        existing = self.deliveries / "existing"
        existing.mkdir()
        with self.assertRaisesRegex(ValueError, "already exists"):
            portable.build_bundle(existing, [self.root / "unused.json"])

    def test_rejects_nonpublic_and_escaping_manifest(self) -> None:
        with patch.object(
            trace_cli,
            "load_runs",
            return_value=[SimpleNamespace(run_id="public-run")],
        ):
            nonpublic = self._write_manifest(public=False)
            with self.assertRaisesRegex(ValueError, "not marked public"):
                portable.build_bundle(self.deliveries / "nonpublic", [nonpublic])

            outside = self.root / "outside.json"
            outside.write_bytes(b"{}")
            escaping = self.results / "escape" / "manifest.json"
            escaping.parent.mkdir()
            escaping.write_text(
                json.dumps(
                    {
                        "schema_version": trace_cli.MANIFEST_SCHEMA,
                        "public_data_only": True,
                        "runs": [
                            {
                                "run_id": "public-run",
                                "path": "week10/results/../outside.json",
                                "sha256": hashlib.sha256(b"{}").hexdigest(),
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "escapes"):
                portable.build_bundle(self.deliveries / "escape", [escaping])

    def test_contains_only_validated_hashed_payloads(self) -> None:
        manifest = self._write_manifest()
        with patch.object(
            trace_cli,
            "load_runs",
            return_value=[SimpleNamespace(run_id="public-run")],
        ):
            destination = self.deliveries / "portable"
            result = portable.build_bundle(destination, [manifest.absolute()])

        self.assertEqual(result["package_scope"], "offline_queries_only")
        self.assertEqual(result["api_calls"], 0)
        records = {row["path"]: row for row in result["files"]}
        actual_files = {
            path.relative_to(destination).as_posix()
            for path in destination.rglob("*")
            if path.is_file()
        }
        self.assertEqual(actual_files, set(records) | {"package-manifest.json"})
        for relative, record in records.items():
            self.assertEqual(
                hashlib.sha256((destination / relative).read_bytes()).hexdigest(),
                record["sha256"],
            )
            self.assertFalse(Path(record["source"]).is_absolute())
        package_paths = [
            path.relative_to(destination).as_posix()
            for path in destination.rglob("*")
            if path.is_file()
        ]
        self.assertFalse(any(".env" in path for path in package_paths))
        self.assertFalse(any("holdout_private" in path for path in package_paths))
        self.assertFalse(any("formal_private" in path for path in package_paths))


if __name__ == "__main__":
    unittest.main()
