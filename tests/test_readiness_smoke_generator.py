"""End-to-end contract for live readiness evidence generation."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from typing import ClassVar
import unittest


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts" / "generate_live_readiness_evidence.py"
FAKE_SERVER = ROOT / "tests" / "fixtures" / "fake_readiness_mcp.py"
GENERATOR_SPEC = importlib.util.spec_from_file_location(
    "generate_live_readiness_evidence", GENERATOR
)
assert GENERATOR_SPEC is not None and GENERATOR_SPEC.loader is not None
GENERATOR_MODULE = importlib.util.module_from_spec(GENERATOR_SPEC)
GENERATOR_SPEC.loader.exec_module(GENERATOR_MODULE)


class ToolResultNormalizationTests(unittest.TestCase):
    PAYLOAD: ClassVar[dict] = {
        "status": "completed",
        "result": {"success": True},
    }

    def _call(self, result: dict) -> dict:
        client = GENERATOR_MODULE.MCPClient.__new__(GENERATOR_MODULE.MCPClient)
        client.component = "fixture"
        client.request = lambda _method, _params: result
        return client.call_tool("fixture_tool", {})

    def _content(self, payload: object) -> list[dict]:
        return [{"type": "text", "text": json.dumps(payload, sort_keys=True)}]

    def test_accepts_supported_content_and_structured_shapes(self):
        content = self._content(self.PAYLOAD)
        cases = {
            "content-only": {
                "content": content,
            },
            "fastmcp-string-wrapper": {
                "content": content,
                "isError": False,
                "structuredContent": {
                    "result": json.dumps(self.PAYLOAD, sort_keys=True)
                },
            },
            "canonical-structured": {
                "content": content,
                "structuredContent": self.PAYLOAD,
            },
        }
        for name, result in cases.items():
            with self.subTest(name=name):
                self.assertEqual(self._call(result), self.PAYLOAD)

    def test_rejects_ambiguous_or_malformed_tool_results(self):
        content = self._content(self.PAYLOAD)
        mismatch = {
            "status": "completed",
            "result": {"success": False},
        }
        cases = {
            "is-error": {
                "content": content,
                "isError": True,
            },
            "non-boolean-is-error": {
                "content": content,
                "isError": 0,
            },
            "missing-content": {
                "structuredContent": self.PAYLOAD,
            },
            "empty-content": {
                "content": [],
                "structuredContent": self.PAYLOAD,
            },
            "multiple-content": {
                "content": content + content,
                "structuredContent": self.PAYLOAD,
            },
            "non-text-content": {
                "content": [{"type": "image", "data": "unused"}],
                "structuredContent": self.PAYLOAD,
            },
            "non-string-text": {
                "content": [{"type": "text", "text": self.PAYLOAD}],
                "structuredContent": self.PAYLOAD,
            },
            "malformed-text-json": {
                "content": [{"type": "text", "text": "{"}],
                "structuredContent": self.PAYLOAD,
            },
            "non-object-text-json": {
                "content": self._content(["not", "an", "object"]),
            },
            "non-object-structured": {
                "content": content,
                "structuredContent": ["not", "an", "object"],
            },
            "wrapper-non-string-result": {
                "content": content,
                "structuredContent": {"result": self.PAYLOAD},
            },
            "wrapper-malformed-json": {
                "content": content,
                "structuredContent": {"result": "{"},
            },
            "wrapper-non-object-json": {
                "content": content,
                "structuredContent": {"result": json.dumps(["not-object"])},
            },
            "wrapper-extra-key": {
                "content": content,
                "structuredContent": {
                    "result": json.dumps(self.PAYLOAD),
                    "extra": True,
                },
            },
            "wrapper-content-mismatch": {
                "content": content,
                "structuredContent": {
                    "result": json.dumps(mismatch, sort_keys=True)
                },
            },
            "direct-content-mismatch": {
                "content": content,
                "structuredContent": mismatch,
            },
            "nested-wrapper": {
                "content": content,
                "structuredContent": {
                    "result": json.dumps(
                        {"result": json.dumps(self.PAYLOAD, sort_keys=True)}
                    )
                },
            },
        }
        for name, result in cases.items():
            with self.subTest(name=name), self.assertRaises(
                GENERATOR_MODULE.SmokeError
            ):
                self._call(result)


class ReadinessSmokeGeneratorTests(unittest.TestCase):
    def _wrapper(
        self,
        directory: Path,
        component: str,
        marker: Path | None = None,
        behavior: str = "",
    ) -> Path:
        wrapper = directory / component
        marker_command = (
            f'printf started > "{marker}"\n' if marker is not None else ""
        )
        behavior_command = (
            f"export FAKE_READINESS_BEHAVIOR={behavior}\n" if behavior else ""
        )
        wrapper.write_text(
            "#!/bin/sh\n"
            f"{marker_command}"
            f"{behavior_command}"
            f'exec "{sys.executable}" "{FAKE_SERVER}" "{component}"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        return wrapper

    def _fake_local_model(self, directory: Path) -> Path:
        model = directory / "offline-model"
        (model / "0_BoW").mkdir(parents=True)
        for path in (
            model / "modules.json",
            model / "config_sentence_transformers.json",
            model / "0_BoW" / "config.json",
        ):
            path.write_text("{}\n", encoding="utf-8")
        return model

    def _run_generator(
        self,
        directory: Path,
        readiness_status: str,
        *,
        candidate_evidence: bool = False,
        marker: Path | None = None,
        behavior: str = "",
    ):
        fixture = directory / "fixture"
        shutil.copytree(ROOT / "bench" / "e2e" / "target-repo", fixture)
        bom_path = directory / "component-bom.json"
        bom = json.loads(
            (ROOT / "component-bom.json").read_text(encoding="utf-8")
        )
        bom["integrated_readiness"]["status"] = readiness_status
        bom_path.write_text(json.dumps(bom), encoding="utf-8")
        code_search = self._wrapper(directory, "code-search", marker, behavior)
        code_graph = self._wrapper(directory, "code-graph", marker, behavior)
        local_model = self._fake_local_model(directory)
        output = directory / "live-readiness-evidence.json"
        command = [
            sys.executable,
            str(GENERATOR),
            "--component-bom",
            str(bom_path),
            "--fixture",
            str(fixture),
            "--server",
            f"code-search={code_search}",
            "--server",
            f"code-graph={code_graph}",
            "--local-model",
            str(local_model),
            "--output",
            str(output),
            "--timeout",
            "10",
        ]
        if candidate_evidence:
            command.append("--candidate-evidence")
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env={
                **os.environ,
                "HOME": str(directory / "hostile-home"),
                "USERPROFILE": str(directory / "hostile-user-profile"),
                "CODE_SEARCH_STORAGE": str(directory / "hostile-storage"),
                "EMBEDDING_PROVIDER": "voyage",
                "VOYAGE_API_KEY": "must-not-reach-smoke",
                "OPENAI_API_KEY": "must-not-reach-smoke",
                "ANTHROPIC_API_KEY": "must-not-reach-smoke",
                "PYTHONUNBUFFERED": "1",
                "GH_TOKEN": "must-not-reach-smoke",
                "CODE_INTEL_COMPONENT_TOKEN": "must-not-reach-smoke",
            },
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        return completed, output, bom

    def test_generator_indexes_fixture_with_both_just_installed_servers(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            completed, output, bom = self._run_generator(directory, "ready")

            self.assertEqual(
                completed.returncode,
                0,
                completed.stdout + completed.stderr,
            )
            evidence = json.loads(output.read_text(encoding="utf-8"))
            self.assertNotIn(
                str(directory.resolve()),
                json.dumps(evidence, sort_keys=True),
            )

        self.assertEqual(evidence["schema_version"], 1)
        self.assertEqual(evidence["evidence_mode"], "ready-validation")
        self.assertEqual(evidence["bom_readiness_status"], "ready")
        self.assertEqual(
            evidence["producer"],
            "scripts/generate_live_readiness_evidence.py:v2",
        )
        self.assertTrue(evidence["checkout_unchanged"])
        search = evidence["components"]["code-search"]
        graph = evidence["components"]["code-graph"]
        self.assertEqual(
            search["version"],
            bom["components"]["code-search"]["install"]["revision"],
        )
        self.assertEqual(
            graph["version"],
            bom["components"]["code-graph"]["install"]["tag"],
        )
        self.assertTrue(search["completion"]["success"])
        self.assertTrue(search["index_ready"])
        self.assertEqual(
            set(search["completion"]),
            {
                "chunks_added",
                "error",
                "files_added",
                "index_identity_status",
                "index_ready",
                "pipeline_version",
                "success",
            },
        )
        self.assertEqual(graph["status"], "ready")
        for field in (
            "repository_id",
            "checkout_id",
            "source_revision",
            "dirty_fingerprint",
            "index_generation",
        ):
            self.assertEqual(
                search["index_identity"][field],
                graph["index_identity"][field],
            )
        self.assertNotIn("must-not-reach-smoke", completed.stdout + completed.stderr)

    def test_blocked_bom_refuses_to_start_servers_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            marker = directory / "server-started"
            completed, output, _bom = self._run_generator(
                directory,
                "blocked",
                marker=marker,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse(output.exists())
            self.assertFalse(marker.exists())

    def test_candidate_mode_generates_evidence_for_blocked_bom(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            completed, output, _bom = self._run_generator(
                directory,
                "blocked",
                candidate_evidence=True,
            )

            self.assertEqual(
                completed.returncode,
                0,
                completed.stdout + completed.stderr,
            )
            evidence = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(evidence["evidence_mode"], "promotion-candidate")
            self.assertEqual(evidence["bom_readiness_status"], "blocked")

    def test_candidate_mode_rejects_an_already_ready_bom(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            marker = directory / "server-started"
            completed, output, _bom = self._run_generator(
                directory,
                "ready",
                candidate_evidence=True,
                marker=marker,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertFalse(output.exists())
            self.assertFalse(marker.exists())

    def test_generator_rejects_unknown_graph_status_and_unbound_final_status(self):
        for behavior in (
            "graph-completion-failed",
            "graph-completion-null",
            "search-status-wrong-project",
            "graph-status-wrong-project",
            "graph-status-wrong-root",
        ):
            with self.subTest(behavior=behavior), tempfile.TemporaryDirectory() as tmp:
                completed, output, _bom = self._run_generator(
                    Path(tmp),
                    "ready",
                    behavior=behavior,
                )

                self.assertNotEqual(
                    completed.returncode,
                    0,
                    completed.stdout + completed.stderr,
                )
                self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
