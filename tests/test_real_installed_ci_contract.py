"""Acceptance contract for validating the real BOM components in CI."""

import importlib.util
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "validate.yml"
HELPER = ROOT / "scripts" / "validate_real_installed.py"
README = ROOT / "README.md"


def load_helper():
    spec = importlib.util.spec_from_file_location("validate_real_installed_ci", HELPER)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load real-install helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RealInstalledCIContractTests(unittest.TestCase):
    def test_distinct_ci_job_installs_and_validates_real_private_components(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("validate-installed-components:", workflow)
        self.assertIn(
            "CODE_INTEL_COMPONENT_TOKEN: "
            "${{ secrets.CODE_INTEL_COMPONENT_TOKEN }}",
            workflow,
        )
        self.assertIn("python3 scripts/validate_real_installed.py", workflow)
        self.assertNotIn(
            "fake_mcp_server",
            workflow.split("validate-installed-components:", 1)[1],
        )

        self.assertTrue(HELPER.is_file(), HELPER)
        helper = HELPER.read_text(encoding="utf-8")
        for required in (
            "component-bom.json",
            "RUNNER_TEMP",
            "gh",
            "repo",
            "clone",
            "release",
            "download",
            "validate_installed.py",
        ):
            self.assertIn(required, helper)
        self.assertNotIn("fake_mcp_server", helper)

        readme = README.read_text(encoding="utf-8")
        self.assertIn("CODE_INTEL_COMPONENT_TOKEN", readme)
        normalized_readme = " ".join(readme.lower().split())
        self.assertIn("trusted `main` push", normalized_readme)
        self.assertIn("post-merge validation secret", normalized_readme)

    def test_private_token_is_limited_to_trusted_fetch_operations(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        real_job = workflow.split("validate-installed-components:", 1)[1]

        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("github.ref == 'refs/heads/main'", real_job)
        self.assertIn("github.event_name == 'push'", real_job)
        self.assertIn("github.event_name == 'workflow_dispatch'", real_job)
        self.assertNotIn(
            "GH_TOKEN: ${{ secrets.CODE_INTEL_COMPONENT_TOKEN }}",
            real_job,
        )
        self.assertIn(
            "CODE_INTEL_COMPONENT_TOKEN: "
            "${{ secrets.CODE_INTEL_COMPONENT_TOKEN }}",
            real_job,
        )

        helper = load_helper()
        fetch_env, runtime_env = helper.build_subprocess_environments(
            "private-token",
            {
                "PATH": os.environ.get("PATH", ""),
                "GH_TOKEN": "ambient-token",
                "CODE_INTEL_COMPONENT_TOKEN": "private-token",
            },
        )
        self.assertEqual(fetch_env["GH_TOKEN"], "private-token")
        self.assertNotIn("CODE_INTEL_COMPONENT_TOKEN", fetch_env)
        self.assertNotIn("GH_TOKEN", runtime_env)
        self.assertNotIn("CODE_INTEL_COMPONENT_TOKEN", runtime_env)

        helper_source = HELPER.read_text(encoding="utf-8")
        self.assertNotIn('"git", "-C", str(source), "fetch"', helper_source)

    def test_ready_bom_generates_evidence_from_just_installed_servers(self):
        helper = load_helper()
        blocked = {"integrated_readiness": {"status": "blocked"}}
        ready = {"integrated_readiness": {"status": "ready"}}

        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp).resolve()
            python = destination / "venv" / "bin" / "python"
            code_search = destination / "venv" / "bin" / "code-search-mcp"
            code_graph = destination / "bin" / "codebase-memory-mcp"
            runtime_env = {
                "PATH": os.environ.get("PATH", ""),
                "CODE_INTEL_LIVE_READINESS_EVIDENCE": "/attacker/supplied.json",
            }

            def generate(command, **_kwargs):
                output_arg = Path(command[command.index("--output") + 1])
                output_arg.write_text("{}\n", encoding="utf-8")

            with mock.patch.object(helper, "run", side_effect=generate) as run:
                self.assertIsNone(
                    helper.generate_live_readiness_evidence(
                        blocked,
                        destination,
                        python,
                        code_search,
                        code_graph,
                        runtime_env,
                    )
                )
                run.assert_not_called()

                output = helper.generate_live_readiness_evidence(
                    ready,
                    destination,
                    python,
                    code_search,
                    code_graph,
                    runtime_env,
                )

            self.assertEqual(output, destination / "live-readiness-evidence.json")
            command = run.call_args.args[0]
            self.assertIn(str(ROOT / "scripts" / "generate_live_readiness_evidence.py"), command)
            self.assertIn(f"code-search={code_search}", command)
            self.assertIn(f"code-graph={code_graph}", command)
            self.assertIn(str(output), command)
            called_env = run.call_args.kwargs["env"]
            self.assertNotIn("CODE_INTEL_LIVE_READINESS_EVIDENCE", called_env)
            self.assertNotIn("GH_TOKEN", called_env)

        helper_source = HELPER.read_text(encoding="utf-8")
        self.assertNotIn(
            'os.environ.get("CODE_INTEL_LIVE_READINESS_EVIDENCE"',
            helper_source,
        )
        self.assertIn("CODE_INTEL_READINESS_EVIDENCE_OVERRIDE", helper_source)
        self.assertIn("validate_plugin.py", helper_source)


if __name__ == "__main__":
    unittest.main()
