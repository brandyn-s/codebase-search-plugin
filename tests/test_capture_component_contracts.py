"""Acceptance tests for deterministic offline component-contract capture."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
CAPTURE = ROOT / "scripts" / "capture_component_contracts.py"
FAKE_SERVER = ROOT / "tests" / "fixtures" / "fake_mcp_server.py"


def load_capture_module():
    scripts = str(ROOT / "scripts")
    sys.path.insert(0, scripts)
    try:
        spec = importlib.util.spec_from_file_location(
            "capture_component_contracts", CAPTURE
        )
        if spec is None or spec.loader is None:
            raise AssertionError("could not load capture_component_contracts")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(scripts)


class CaptureComponentContractsTests(unittest.TestCase):
    def _wrapper(
        self, directory: Path, component: str, mode: str | None = None
    ) -> Path:
        wrapper = directory / (
            component if mode is None else f"{component}-{mode}"
        )
        mode_argument = "" if mode is None else f' "{mode}"'
        wrapper.write_text(
            "#!/bin/sh\n"
            f'exec "{sys.executable}" "{FAKE_SERVER}" "{component}"'
            f"{mode_argument}\n",
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        return wrapper

    def _candidate_bom(self, directory: Path) -> Path:
        bom = json.loads(
            (ROOT / "component-bom.json").read_text(encoding="utf-8")
        )
        bom["components"]["code-search"]["install"]["revision"] = "a" * 40
        bom["components"]["code-graph"]["install"]["tag"] = "v9.9.9-test"
        path = directory / "candidate-bom.json"
        path.write_text(json.dumps(bom), encoding="utf-8")
        return path

    def _run(
        self,
        *,
        candidate_bom: Path,
        code_search: Path,
        code_graph: Path,
        output: Path,
        write: bool,
        extra_env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        command = [
            sys.executable,
            str(CAPTURE),
            "--component-bom",
            str(candidate_bom),
            "--server",
            f"code-search={code_search}",
            "--server",
            f"code-graph={code_graph}",
            "--output-dir",
            str(output),
        ]
        if write:
            command.append("--write")
        return subprocess.run(
            command,
            cwd=ROOT,
            env={
                **os.environ,
                **(extra_env or {}),
                "PYTHONDONTWRITEBYTECODE": "1",
                "PYTHONUNBUFFERED": "1",
            },
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )

    def test_cli_captures_all_tools_but_writes_only_when_requested(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            candidate_bom = self._candidate_bom(directory)
            candidate = json.loads(candidate_bom.read_text(encoding="utf-8"))
            code_search = self._wrapper(directory, "code-search")
            code_graph = self._wrapper(directory, "code-graph")
            dry_output = directory / "dry-output"

            dry_run = self._run(
                candidate_bom=candidate_bom,
                code_search=code_search,
                code_graph=code_graph,
                output=dry_output,
                write=False,
            )

            self.assertEqual(
                dry_run.returncode, 0, dry_run.stdout + dry_run.stderr
            )
            self.assertFalse(dry_output.exists())
            dry_summary = json.loads(dry_run.stdout)
            self.assertFalse(dry_summary["written"])

            output = directory / "captured"
            written = self._run(
                candidate_bom=candidate_bom,
                code_search=code_search,
                code_graph=code_graph,
                output=output,
                write=True,
            )

            self.assertEqual(
                written.returncode, 0, written.stdout + written.stderr
            )
            proposed_bom = json.loads(
                (output / "component-bom.json").read_text(encoding="utf-8")
            )
            search = json.loads(
                (
                    output / "compatibility" / "code-search-tools.json"
                ).read_text(encoding="utf-8")
            )
            graph = json.loads(
                (
                    output / "compatibility" / "code-graph-tools.json"
                ).read_text(encoding="utf-8")
            )

        self.assertEqual(search["source"], {"kind": "git", "version": "a" * 40})
        self.assertEqual(
            graph["source"],
            {"kind": "github-release", "version": "v9.9.9-test"},
        )
        self.assertEqual(list(search["tools"]), sorted(search["tools"]))
        self.assertEqual(list(graph["tools"]), sorted(graph["tools"]))
        self.assertEqual(
            set(search["tools"]),
            set(
                json.loads(
                    (
                        ROOT / "compatibility" / "code-search-tools.json"
                    ).read_text(encoding="utf-8")
                )["tools"]
            ),
        )
        self.assertEqual(
            set(graph["tools"]),
            set(
                json.loads(
                    (
                        ROOT / "compatibility" / "code-graph-tools.json"
                    ).read_text(encoding="utf-8")
                )["tools"]
            ),
        )
        self.assertEqual(
            search["tested_capabilities"],
            {
                "outputs": {
                    "index_identity": {
                        "supported": False,
                        "schema_version": None,
                        "fields": [],
                    },
                    "semantic_index_ready": False,
                }
            },
        )
        self.assertFalse(
            graph["tested_capabilities"]["outputs"]["index_identity"][
                "supported"
            ]
        )
        self.assertFalse(
            graph["tested_capabilities"]["outputs"]["graph_status_ready"]
        )
        self.assertEqual(
            proposed_bom["components"]["code-search"]["tested_capabilities"],
            search["tested_capabilities"],
        )
        self.assertEqual(
            proposed_bom["components"]["code-graph"]["tested_capabilities"],
            graph["tested_capabilities"],
        )
        self.assertEqual(
            proposed_bom["integrated_readiness"]["status"], "blocked"
        )
        self.assertEqual(
            proposed_bom["integrated_readiness"]["requires"],
            candidate["integrated_readiness"]["requires"],
        )
        self.assertNotIn("evidence", proposed_bom["integrated_readiness"])

    def test_capture_is_deterministic_and_invalidates_old_behavioral_claims(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            candidate_bom = self._candidate_bom(directory)
            candidate = json.loads(candidate_bom.read_text(encoding="utf-8"))
            candidate["integrated_readiness"]["status"] = "ready"
            candidate["integrated_readiness"]["evidence"] = "old-evidence.json"
            candidate["behavioral_claim"] = True
            for details in candidate["components"].values():
                details["behavioral_claim"] = True
                details["install"]["behavioral_claim"] = True
            candidate["components"]["code-graph"]["install"]["assets"][
                "linux-amd64"
            ]["behavioral_claim"] = True
            candidate["components"]["code-search"]["tested_capabilities"][
                "outputs"
            ]["semantic_index_ready"] = True
            candidate["components"]["code-graph"]["tested_capabilities"][
                "outputs"
            ]["index_identity"] = {
                "supported": True,
                "schema_version": 1,
                "fields": ["stale"],
            }
            candidate_bom.write_text(json.dumps(candidate), encoding="utf-8")
            code_search = self._wrapper(directory, "code-search")
            code_graph = self._wrapper(directory, "code-graph")
            outputs = [directory / "first", directory / "second"]

            for output in outputs:
                completed = self._run(
                    candidate_bom=candidate_bom,
                    code_search=code_search,
                    code_graph=code_graph,
                    output=output,
                    write=True,
                )
                self.assertEqual(
                    completed.returncode, 0, completed.stdout + completed.stderr
                )

            first_files = {
                path.relative_to(outputs[0]): path.read_bytes()
                for path in outputs[0].rglob("*")
                if path.is_file()
            }
            second_files = {
                path.relative_to(outputs[1]): path.read_bytes()
                for path in outputs[1].rglob("*")
                if path.is_file()
            }

        self.assertEqual(first_files, second_files)
        proposed_bom = json.loads(first_files[Path("component-bom.json")])
        search = json.loads(
            first_files[Path("compatibility/code-search-tools.json")]
        )
        graph = json.loads(
            first_files[Path("compatibility/code-graph-tools.json")]
        )
        self.assertEqual(proposed_bom["integrated_readiness"]["status"], "blocked")
        self.assertNotIn("evidence", proposed_bom["integrated_readiness"])
        self.assertNotIn("behavioral_claim", proposed_bom)
        self.assertNotIn(
            "behavioral_claim", proposed_bom["components"]["code-search"]
        )
        self.assertNotIn(
            "behavioral_claim",
            proposed_bom["components"]["code-search"]["install"],
        )
        self.assertNotIn(
            "behavioral_claim",
            proposed_bom["components"]["code-graph"]["install"],
        )
        self.assertNotIn(
            "behavioral_claim",
            proposed_bom["components"]["code-graph"]["install"]["assets"][
                "linux-amd64"
            ],
        )
        self.assertFalse(
            search["tested_capabilities"]["outputs"]["semantic_index_ready"]
        )
        self.assertFalse(
            graph["tested_capabilities"]["outputs"]["index_identity"]["supported"]
        )
        self.assertNotIn("side_effects", graph["tested_capabilities"])

    def test_rejects_malformed_duplicate_and_empty_tool_lists(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            candidate_bom = self._candidate_bom(directory)

            for mode in ("empty", "duplicate", "malformed", "empty-name"):
                with self.subTest(mode=mode):
                    mode_search = self._wrapper(directory, "code-search", mode)
                    mode_graph = self._wrapper(directory, "code-graph", mode)
                    output = directory / f"output-{mode}"
                    completed = self._run(
                        candidate_bom=candidate_bom,
                        code_search=mode_search,
                        code_graph=mode_graph,
                        output=output,
                        write=True,
                    )
                    self.assertEqual(completed.returncode, 1)
                    self.assertFalse(output.exists())

    def test_capture_follows_tools_list_pagination(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            candidate_bom = self._candidate_bom(directory)
            code_search = self._wrapper(directory, "code-search", "paginated")
            code_graph = self._wrapper(directory, "code-graph", "paginated")
            output = directory / "captured"

            completed = self._run(
                candidate_bom=candidate_bom,
                code_search=code_search,
                code_graph=code_graph,
                output=output,
                write=True,
            )

            self.assertEqual(
                completed.returncode, 0, completed.stdout + completed.stderr
            )
            for component in ("code-search", "code-graph"):
                captured = json.loads(
                    (
                        output / "compatibility" / f"{component}-tools.json"
                    ).read_text(encoding="utf-8")
                )
                expected = json.loads(
                    (
                        ROOT / "compatibility" / f"{component}-tools.json"
                    ).read_text(encoding="utf-8")
                )
                self.assertEqual(set(captured["tools"]), set(expected["tools"]))

    def test_capture_subprocesses_do_not_inherit_credentials(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            candidate_bom = self._candidate_bom(directory)
            code_search = self._wrapper(directory, "code-search")
            code_graph = self._wrapper(directory, "code-graph")
            output = directory / "captured"
            secret = "capture-secret-sentinel"

            completed = self._run(
                candidate_bom=candidate_bom,
                code_search=code_search,
                code_graph=code_graph,
                output=output,
                write=True,
                extra_env={
                    "GH_TOKEN": secret,
                    "GITHUB_TOKEN": secret,
                    "CODE_INTEL_COMPONENT_TOKEN": secret,
                    "OPENAI_API_KEY": secret,
                    "MODEL_API_TOKEN": secret,
                },
            )

            self.assertEqual(
                completed.returncode, 0, completed.stdout + completed.stderr
            )
            self.assertTrue((output / "component-bom.json").is_file())
            self.assertNotIn(secret, completed.stdout)
            self.assertNotIn(secret, completed.stderr)

    def test_nonobject_input_schemas_cannot_attest_optional_properties(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            candidate_bom = self._candidate_bom(directory)
            code_search = self._wrapper(
                directory, "code-search", "nonobject-inputs"
            )
            code_graph = self._wrapper(
                directory, "code-graph", "nonobject-inputs"
            )
            output = directory / "captured"

            completed = self._run(
                candidate_bom=candidate_bom,
                code_search=code_search,
                code_graph=code_graph,
                output=output,
                write=True,
            )

            self.assertEqual(
                completed.returncode, 0, completed.stdout + completed.stderr
            )
            graph = json.loads(
                (
                    output / "compatibility" / "code-graph-tools.json"
                ).read_text(encoding="utf-8")
            )
            self.assertFalse(
                graph["tested_capabilities"]["inputs"][
                    "index_repository.skip_report"
                ]
            )

    def test_rejects_weakened_readiness_policy(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            candidate_bom = self._candidate_bom(directory)
            candidate = json.loads(candidate_bom.read_text(encoding="utf-8"))
            candidate["integrated_readiness"]["requires"] = {}
            candidate_bom.write_text(json.dumps(candidate), encoding="utf-8")
            code_search = self._wrapper(directory, "code-search")
            code_graph = self._wrapper(directory, "code-graph")
            output = directory / "captured"

            completed = self._run(
                candidate_bom=candidate_bom,
                code_search=code_search,
                code_graph=code_graph,
                output=output,
                write=True,
            )

            self.assertEqual(completed.returncode, 1)
            self.assertIn("readiness", completed.stderr)
            self.assertFalse(output.exists())

    def test_rejects_unsafe_snapshot_paths_and_unpinned_revisions(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            original_bom = json.loads(
                self._candidate_bom(directory).read_text(encoding="utf-8")
            )
            code_search = self._wrapper(directory, "code-search")
            code_graph = self._wrapper(directory, "code-graph")
            mutations = {
                "snapshot-traversal": lambda bom: bom["components"][
                    "code-search"
                ].update({"schema_snapshot": "../escape.json"}),
                "absolute-snapshot": lambda bom: bom["components"][
                    "code-graph"
                ].update({"schema_snapshot": "/tmp/escape.json"}),
                "floating-revision": lambda bom: bom["components"][
                    "code-search"
                ]["install"].update({"revision": "main"}),
                "unsafe-asset": lambda bom: bom["components"]["code-graph"][
                    "install"
                ]["assets"]["linux-amd64"].update({"name": "../../asset.tar.gz"}),
            }

            for label, mutate in mutations.items():
                with self.subTest(label=label):
                    candidate = json.loads(json.dumps(original_bom))
                    mutate(candidate)
                    candidate_path = directory / f"{label}.json"
                    candidate_path.write_text(
                        json.dumps(candidate), encoding="utf-8"
                    )
                    output = directory / f"output-{label}"
                    completed = self._run(
                        candidate_bom=candidate_path,
                        code_search=code_search,
                        code_graph=code_graph,
                        output=output,
                        write=True,
                    )
                    self.assertEqual(
                        completed.returncode,
                        1,
                        completed.stdout + completed.stderr,
                    )
                    self.assertFalse(output.exists())

    def test_failed_capture_never_publishes_a_partial_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            candidate_bom = self._candidate_bom(directory)
            code_search = self._wrapper(directory, "code-search")
            missing_graph = directory / "missing-code-graph"
            output = directory / "captured"

            completed = self._run(
                candidate_bom=candidate_bom,
                code_search=code_search,
                code_graph=missing_graph,
                output=output,
                write=True,
            )

            self.assertEqual(completed.returncode, 1)
            self.assertFalse(output.exists())
            self.assertEqual(
                list(directory.glob(f".{output.name}.staging-*")), []
            )

    def test_staged_write_failure_is_cleaned_up_before_publication(self):
        capture = load_capture_module()
        documents = {
            "component-bom.json": {"schema_version": 1},
            "compatibility/code-search-tools.json": {"component": "code-search"},
            "compatibility/code-graph-tools.json": {"component": "code-graph"},
        }
        real_write_bytes = Path.write_bytes

        def fail_during_staging(path: Path, data: bytes) -> int:
            if path.name == "code-search-tools.json":
                raise OSError("injected staging failure")
            return real_write_bytes(path, data)

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            output = directory / "captured"
            with mock.patch.object(Path, "write_bytes", fail_during_staging):
                with self.assertRaisesRegex(OSError, "injected staging failure"):
                    capture._write_transactionally(output, documents)

            self.assertFalse(output.exists())
            self.assertEqual(
                list(directory.glob(f".{output.name}.staging-*")), []
            )

    def test_capture_runtime_environment_is_minimal_and_isolated(self):
        capture = load_capture_module()
        dangerous = {
            "PYTHONPATH": "/sensitive/python",
            "PYTHONHOME": "/sensitive/python-home",
            "LD_PRELOAD": "/sensitive/inject.so",
            "DYLD_INSERT_LIBRARIES": "/sensitive/inject.dylib",
            "NODE_OPTIONS": "--require=/sensitive/inject.js",
            "CODE_SEARCH_STORAGE": "/sensitive/code-search",
            "HOME": "/sensitive/home",
            "GH_TOKEN": "capture-secret-sentinel",
        }
        with tempfile.TemporaryDirectory() as tmp:
            runtime_root = Path(tmp)
            with mock.patch.dict(os.environ, dangerous):
                runtime_env = capture._capture_environment(runtime_root)

            for name in dangerous:
                if name not in {"HOME", "CODE_SEARCH_STORAGE"}:
                    self.assertNotIn(name, runtime_env)
            self.assertEqual(
                runtime_env["HOME"], str(runtime_root / "home")
            )
            self.assertEqual(
                runtime_env["CODE_SEARCH_STORAGE"],
                str(runtime_root / "code-search-storage"),
            )
            self.assertEqual(runtime_env["PATH"], os.defpath)
            self.assertTrue(Path(runtime_env["HOME"]).is_dir())
            self.assertTrue(Path(runtime_env["CODE_SEARCH_STORAGE"]).is_dir())

    def test_requires_explicit_absolute_executable_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            candidate_bom = self._candidate_bom(directory)
            code_graph = self._wrapper(directory, "code-graph")
            output = directory / "captured"

            completed = self._run(
                candidate_bom=candidate_bom,
                code_search=Path("relative-code-search"),
                code_graph=code_graph,
                output=output,
                write=True,
            )

            self.assertEqual(completed.returncode, 1)
            self.assertIn("absolute", completed.stderr)
            self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
