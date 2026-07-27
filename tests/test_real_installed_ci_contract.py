"""Acceptance contract for validating the real BOM components in CI."""

import importlib.util
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github" / "workflows" / "validate.yml"
HELPER = ROOT / "scripts" / "validate_real_installed.py"
MODEL_BUILDER = ROOT / "scripts" / "build_readiness_model.py"
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
        self.assertIn("build_readiness_model.py", helper)
        self.assertIn("--local-model", helper)
        self.assertTrue(MODEL_BUILDER.is_file(), MODEL_BUILDER)

        readme = README.read_text(encoding="utf-8")
        self.assertIn("CODE_INTEL_COMPONENT_TOKEN", readme)
        normalized_readme = " ".join(readme.lower().split())
        self.assertIn("trusted `main` push", normalized_readme)
        self.assertIn("post-merge validation secret", normalized_readme)

    def test_trusted_job_uploads_successful_live_readiness_evidence(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        real_job = workflow.split("validate-installed-components:", 1)[1]
        normalized_job = " ".join(real_job.split())
        output = (
            "$RUNNER_TEMP/code-intel-ready-validation/"
            "readiness-evidence.json"
        )
        upload_action = (
            "actions/upload-artifact@"
            "ea165f8d65b6e75b540449e92b4886f43607fa02"
        )

        self.assertIn(
            "python3 scripts/validate_real_installed.py "
            f'--readiness-evidence-output "{output}"',
            normalized_job,
        )
        self.assertIn(f"uses: {upload_action} # v4.6.2", real_job)
        self.assertIn(
            "name: Validate exact installed components and live readiness",
            workflow,
        )
        self.assertIn("name: Upload trusted readiness evidence", real_job)
        self.assertIn("name: code-intel-ready-validation", real_job)
        self.assertIn(
            "path: ${{ runner.temp }}/code-intel-ready-validation/"
            "readiness-evidence.json",
            real_job,
        )
        self.assertIn("if-no-files-found: error", real_job)
        self.assertIn("retention-days: 30", real_job)
        self.assertLess(
            real_job.index("--readiness-evidence-output"),
            real_job.index(upload_action),
        )

    def test_validation_job_parses_powershell_installer_ast(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        validation_job = workflow.split("validate-installed-components:", 1)[0]

        self.assertIn("Parse install.ps1 with PowerShell AST", validation_job)
        self.assertIn("shell: pwsh", validation_job)
        self.assertIn(
            "[System.Management.Automation.Language.Parser]::ParseFile",
            validation_job,
        )
        self.assertIn("[ref]$parseErrors", validation_job)
        self.assertIn("$parseErrors.Count", validation_job)
        self.assertIn("throw", validation_job)

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

    def test_readiness_evidence_output_rejects_paths_outside_runner_temp(self):
        helper = load_helper()

        with tempfile.TemporaryDirectory() as tmp:
            runner_temp = Path(tmp).resolve()
            outside = runner_temp.parent / "readiness-evidence.json"

            with self.assertRaisesRegex(
                helper.RealInstallError,
                "beneath RUNNER_TEMP",
            ):
                helper.resolve_readiness_evidence_output(
                    str(outside),
                    runner_temp,
                )

    def test_failed_live_validation_does_not_publish_readiness_evidence(self):
        helper = load_helper()

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            source = directory / "live-readiness-evidence.json"
            output = directory / "artifact" / "readiness-evidence.json"
            source.write_text('{"schema_version": 1}\n', encoding="utf-8")

            with (
                mock.patch.object(
                    helper,
                    "run",
                    side_effect=subprocess.CalledProcessError(
                        1,
                        ["validate_plugin.py"],
                    ),
                ),
                self.assertRaises(subprocess.CalledProcessError),
            ):
                helper.validate_and_publish_live_evidence(
                    source,
                    output,
                    directory / "venv" / "bin" / "python",
                    {"PATH": os.environ.get("PATH", "")},
                )

            self.assertFalse(output.exists())

    def test_successful_live_validation_publishes_exact_readiness_evidence(self):
        helper = load_helper()

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            source = directory / "live-readiness-evidence.json"
            output = directory / "artifact" / "readiness-evidence.json"
            evidence = b'{"schema_version":1,"evidence_mode":"ready-validation"}\n'
            source.write_bytes(evidence)

            with mock.patch.object(helper, "run") as run:
                helper.validate_and_publish_live_evidence(
                    source,
                    output,
                    directory / "venv" / "bin" / "python",
                    {"PATH": os.environ.get("PATH", "")},
                )

            run.assert_called_once()
            self.assertEqual(output.read_bytes(), evidence)

    def test_requested_output_requires_live_readiness_evidence(self):
        helper = load_helper()

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            output = directory / "artifact" / "readiness-evidence.json"

            with (
                mock.patch.object(helper, "run") as run,
                self.assertRaisesRegex(
                    helper.RealInstallError,
                    "live readiness evidence",
                ),
            ):
                helper.validate_and_publish_live_evidence(
                    None,
                    output,
                    directory / "venv" / "bin" / "python",
                    {"PATH": os.environ.get("PATH", "")},
                )

            run.assert_not_called()
            self.assertFalse(output.exists())

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
                "VOYAGE_API_KEY": "must-not-reach-smoke",
                "OPENAI_API_KEY": "must-not-reach-smoke",
                "ANTHROPIC_API_KEY": "must-not-reach-smoke",
            }
            model = destination / "readiness-model"

            def generate(command, **_kwargs):
                generator = str(
                    ROOT / "scripts" / "generate_live_readiness_evidence.py"
                )
                if generator in command:
                    output_arg = Path(command[command.index("--output") + 1])
                    output_arg.write_text("{}\n", encoding="utf-8")

            with (
                mock.patch.object(
                    helper,
                    "build_readiness_model",
                    return_value=model,
                ) as build_model,
                mock.patch.object(helper, "run", side_effect=generate) as run,
            ):
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
                build_model.assert_not_called()

                output = helper.generate_live_readiness_evidence(
                    ready,
                    destination,
                    python,
                    code_search,
                    code_graph,
                    runtime_env,
                )
                build_model.assert_called_once()

            self.assertEqual(output, destination / "live-readiness-evidence.json")
            command = run.call_args.args[0]
            self.assertIn(
                str(ROOT / "scripts" / "generate_live_readiness_evidence.py"),
                command,
            )
            self.assertIn(f"code-search={code_search}", command)
            self.assertIn(f"code-graph={code_graph}", command)
            self.assertEqual(
                command[command.index("--local-model") + 1],
                str(model),
            )
            self.assertIn(str(output), command)
            called_env = run.call_args.kwargs["env"]
            self.assertNotIn("CODE_INTEL_LIVE_READINESS_EVIDENCE", called_env)
            self.assertNotIn("GH_TOKEN", called_env)
            self.assertNotIn("VOYAGE_API_KEY", called_env)
            self.assertNotIn("OPENAI_API_KEY", called_env)
            self.assertNotIn("ANTHROPIC_API_KEY", called_env)

        helper_source = HELPER.read_text(encoding="utf-8")
        self.assertNotIn(
            'os.environ.get("CODE_INTEL_LIVE_READINESS_EVIDENCE"',
            helper_source,
        )
        self.assertIn("CODE_INTEL_READINESS_EVIDENCE_OVERRIDE", helper_source)
        self.assertIn("validate_plugin.py", helper_source)


if __name__ == "__main__":
    unittest.main()
