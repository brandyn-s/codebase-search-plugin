"""Acceptance tests for installed MCP schema validation."""

import importlib.util
import os
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR = ROOT / "scripts" / "validate_installed.py"
REVISION_VERIFIER = ROOT / "scripts" / "verify_code_search_revision.py"
WHEEL_VERIFIER = ROOT / "scripts" / "verify_code_search_wheel.py"
FAKE_SERVER = ROOT / "tests" / "fixtures" / "fake_mcp_server.py"


def load_revision_verifier():
    spec = importlib.util.spec_from_file_location(
        "verify_code_search_revision", REVISION_VERIFIER
    )
    if spec is None or spec.loader is None:
        raise AssertionError("could not load code-search revision verifier")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if hasattr(module, "_gh_authenticated_cache"):
        module._gh_authenticated_cache[""] = True  # emulate an authenticated gh
    return module


def load_wheel_verifier():
    spec = importlib.util.spec_from_file_location(
        "verify_code_search_wheel", WHEEL_VERIFIER
    )
    if spec is None or spec.loader is None:
        raise AssertionError("could not load code-search wheel verifier")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class InstalledContractTests(unittest.TestCase):
    def _wrapper(self, directory: Path, component: str) -> Path:
        wrapper = directory / component
        wrapper.write_text(
            "#!/bin/sh\n"
            f'exec "{sys.executable}" "{FAKE_SERVER}" "{component}"\n',
            encoding="utf-8",
        )
        wrapper.chmod(0o755)
        return wrapper

    def test_cli_accepts_servers_matching_tested_component_snapshots(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            code_search = self._wrapper(tmp_path, "code-search")
            code_graph = self._wrapper(tmp_path, "code-graph")

            completed = subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR),
                    "--server",
                    f"code-search={code_search}",
                    "--server",
                    f"code-graph={code_graph}",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                env={**os.environ, "PYTHONUNBUFFERED": "1"},
                timeout=20,
                check=False,
            )

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("Installed MCP contract validation passed", completed.stdout)

    def test_installers_consume_fixed_bom_and_run_contract_validation(self):
        bom = json.loads((ROOT / "component-bom.json").read_text(encoding="utf-8"))
        code_search_install = bom["components"]["code-search"]["install"]
        code_search_version = (
            code_search_install["tag"]
            if code_search_install["kind"] == "github-release"
            else code_search_install["revision"]
        )
        graph_tag = bom["components"]["code-graph"]["install"]["tag"]

        shell = (ROOT / "install.sh").read_text(encoding="utf-8")
        powershell = (ROOT / "install.ps1").read_text(encoding="utf-8")
        for installer in (shell, powershell):
            self.assertIn("component-bom.json", installer)
            self.assertIn("validate_plugin.py", installer)
            self.assertIn("validate_installed.py", installer)
            self.assertIn("verify_code_search_revision.py", installer)
            self.assertNotIn("release list", installer)
            self.assertNotIn("releases/latest", installer)
            self.assertNotIn(code_search_version, installer)
            self.assertNotIn(graph_tag, installer)
            self.assertLess(
                installer.index("validate_plugin.py"),
                installer.index("Installing code-search"),
            )

        self.assertIn(
            "env -u CODE_INTEL_READINESS_EVIDENCE_OVERRIDE",
            shell,
        )
        self.assertIn(
            "Remove-Item Env:CODE_INTEL_READINESS_EVIDENCE_OVERRIDE",
            powershell,
        )

        self.assertRegex(
            powershell,
            re.compile(
                r"& \$VenvPip install --quiet \$CodeSearchRequirement\r?\n"
                r"\s*if \(\$LASTEXITCODE -ne 0\)",
            ),
        )

    def test_installers_bind_the_optional_go_scip_generator_to_the_bom(self):
        shell = (ROOT / "install.sh").read_text(encoding="utf-8")
        powershell = (ROOT / "install.ps1").read_text(encoding="utf-8")
        bom = json.loads((ROOT / "component-bom.json").read_text(encoding="utf-8"))
        generator = bom["precision_generators"]["go-scip"]

        for installer in (shell, powershell):
            for contract in (
                "precision_generators",
                "go-scip",
                "version_output",
                "binary_sha256",
                "prepare_scip_index.py",
                "Auto SCIP precision unavailable",
            ):
                self.assertIn(contract, installer)
            self.assertNotIn(generator["tag"], installer)
            self.assertNotIn(generator["source_revision"], installer)

        scip_block = shell.split(
            "Installing optional Go SCIP precision generator", 1
        )[1].split("# ------------------------------------------------------------------", 1)[0]
        for contract in (
            "resolve_release_tag_commit",
            "download_release_asset",
            "verify_sha256",
            "tar xzf",
            "prepare_scip_index.py\" verify",
        ):
            self.assertIn(contract, scip_block)
        self.assertLess(
            scip_block.index("verify_sha256"),
            scip_block.index("tar xzf"),
        )

    def test_installers_bind_typescript_scip_to_isolated_runtime_and_lockfile(self):
        shell = (ROOT / "install.sh").read_text(encoding="utf-8")
        powershell = (ROOT / "install.ps1").read_text(encoding="utf-8")

        for installer in (shell, powershell):
            for contract in (
                "typescript-scip",
                "node_runtime",
                "lockfile_sha256",
                "entrypoint_sha256",
                "ignore-scripts",
                "prepare_scip_index.py",
                "--language typescript",
            ):
                self.assertIn(contract, installer)
            self.assertIn("never runs npm in a target", installer)

        block = shell.split(
            "Installing optional TypeScript SCIP precision generator", 1
        )[1].split("# ------------------------------------------------------------------", 1)[0]
        self.assertLess(block.index("verify_sha256"), block.index("npm-cli.js"))
        self.assertIn("$BIN_DIR/scip-typescript-runtime", block)

    def test_revision_verifier_accepts_only_the_exact_installed_commit(self):
        verifier = load_revision_verifier()
        expected = "a" * 40
        valid = {
            "url": "https://github.com/brandyn-s/code-search.git",
            "vcs_info": {
                "vcs": "git",
                "requested_revision": expected,
                "commit_id": expected,
            },
        }
        repository = "https://github.com/brandyn-s/code-search.git"

        verifier.verify_direct_url(valid, expected, repository)
        for invalid in (
            {},
            {
                **valid,
                "url": "https://github.com/example/code-search.git",
            },
            {"vcs_info": {"vcs": "git", "commit_id": "0" * 40}},
            {"vcs_info": {"vcs": "hg", "commit_id": expected}},
            {"vcs_info": {"vcs": "git", "commit_id": expected[:12]}},
        ):
            with self.subTest(invalid=invalid):
                with self.assertRaises(verifier.RevisionError):
                    verifier.verify_direct_url(invalid, expected, repository)

    def test_wheel_verifier_accepts_pinned_local_archive(self):
        verifier = load_wheel_verifier()
        expected_sha256 = "a" * 64
        verifier.verify_wheel_install(
            {
                "archive_info": {
                    "hash": f"sha256={expected_sha256}",
                    "hashes": {"sha256": expected_sha256},
                },
                "url": (
                    "file:///tmp/"
                    "code_search_mcp-0.2.0-py3-none-any.whl"
                ),
            },
            installed_version="0.2.0",
            expected_tag="v0.2.0",
            expected_asset_name=(
                "code_search_mcp-0.2.0-py3-none-any.whl"
            ),
            expected_sha256=expected_sha256,
        )

    def test_manual_install_uses_bom_release_and_verifies_provenance(self):
        install_doc = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
        manual = install_doc.split("## Manual install (alternative)", 1)[1].split(
            "## Trusted component validation", 1
        )[0]

        self.assertIn("github-release", manual)
        self.assertIn("gh release download", manual)
        self.assertIn("gh attestation verify", manual)
        self.assertIn("verify_code_search_wheel.py", manual)
        self.assertIn("--source-digest", manual)
        self.assertIn("--deny-self-hosted-runners", manual)

    def _run_fake_contract(
        self,
        fixture_env: dict[str, str],
    ) -> subprocess.CompletedProcess:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            code_search = self._wrapper(tmp_path, "code-search")
            code_graph = self._wrapper(tmp_path, "code-graph")
            return subprocess.run(
                [
                    sys.executable,
                    str(VALIDATOR),
                    "--server",
                    f"code-search={code_search}",
                    "--server",
                    f"code-graph={code_graph}",
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                env={**os.environ, **fixture_env, "PYTHONUNBUFFERED": "1"},
                timeout=20,
                check=False,
            )

    def test_cli_fails_closed_when_installed_tool_is_absent(self):
        completed = self._run_fake_contract({"FAKE_MCP_DROP_TOOL": "search_code"})

        self.assertEqual(completed.returncode, 1)
        self.assertIn("missing tool 'search_code'", completed.stderr)

    def test_cli_fails_closed_when_installed_schema_drifted(self):
        completed = self._run_fake_contract(
            {"FAKE_MCP_MUTATE_SCHEMA": "search_code"}
        )

        self.assertEqual(completed.returncode, 1)
        self.assertIn("schema mismatch for 'search_code'", completed.stderr)

    def test_cli_fails_closed_when_installed_server_adds_an_unreviewed_tool(self):
        completed = self._run_fake_contract({"FAKE_MCP_MODE": "extra"})

        self.assertEqual(completed.returncode, 1)
        self.assertIn("unexpected tool 'unreviewed_extra_tool'", completed.stderr)


if __name__ == "__main__":
    unittest.main()
