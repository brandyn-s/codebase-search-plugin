"""Acceptance contract for validating the real BOM components in CI."""

import importlib.util
import hashlib
import json
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
                "GITHUB_TOKEN": "ambient-actions-token",
                "CODE_INTEL_COMPONENT_TOKEN": "private-token",
            },
        )
        self.assertEqual(fetch_env["GH_TOKEN"], "private-token")
        self.assertNotIn("CODE_INTEL_COMPONENT_TOKEN", fetch_env)
        self.assertNotIn("GITHUB_TOKEN", fetch_env)
        self.assertNotIn("GH_TOKEN", runtime_env)
        self.assertNotIn("GITHUB_TOKEN", runtime_env)
        self.assertNotIn("CODE_INTEL_COMPONENT_TOKEN", runtime_env)

        helper_source = HELPER.read_text(encoding="utf-8")
        self.assertNotIn('"git", "-C", str(source), "fetch"', helper_source)

    def test_release_wheel_is_verified_offline_before_secret_free_install(self):
        helper = load_helper()
        wheel_bytes = b"pinned wheel"
        bundle_bytes = b"offline attestation bundle"
        wheel_name = "redacted_code_search-0.2.0-py3-none-any.whl"
        bundle_name = f"{wheel_name}.jsonl"
        install = {
            "kind": "github-release",
            "repository": "redacted-org/code-search",
            "tag": "v0.2.0",
            "source_revision": "a" * 40,
            "asset": {
                "name": wheel_name,
                "sha256": hashlib.sha256(wheel_bytes).hexdigest(),
            },
            "attestation": {
                "bundle": {
                    "name": bundle_name,
                    "sha256": hashlib.sha256(bundle_bytes).hexdigest(),
                },
                "signer_workflow": (
                    "redacted-org/code-search/"
                    ".github/workflows/release.yml"
                ),
                "source_ref": "refs/heads/main",
            },
        }
        fetch_env = {"PATH": "/bin", "GH_TOKEN": "private-token"}
        runtime_env = {"PATH": "/bin"}
        calls = []
        events = []

        def emulate(command, *, env, cwd=None):
            calls.append((command, env, cwd))
            if command[:3] == ["gh", "release", "download"]:
                events.append("download")
                download_dir = Path(command[command.index("--dir") + 1])
                download_dir.mkdir(parents=True, exist_ok=True)
                (download_dir / wheel_name).write_bytes(wheel_bytes)
                (download_dir / bundle_name).write_bytes(bundle_bytes)
            elif command[1:4] == ["-m", "pip", "install"]:
                executable = (
                    Path(command[0]).parents[1] / "bin" / "code-search-mcp"
                )
                executable.parent.mkdir(parents=True, exist_ok=True)
                executable.write_text("#!/bin/sh\n", encoding="utf-8")

        def resolve_tag(*_args, **_kwargs):
            events.append("tag")
            return "a" * 40

        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp)
            with (
                mock.patch.object(helper, "run", side_effect=emulate),
                mock.patch.object(
                    helper,
                    "resolve_release_tag_commit",
                    create=True,
                    side_effect=resolve_tag,
                ) as resolve,
            ):
                python, executable = helper.install_code_search(
                    install,
                    destination,
                    fetch_env,
                    runtime_env,
                )

        resolve.assert_called_once_with(
            "redacted-org/code-search",
            "v0.2.0",
            fetch_env,
        )
        self.assertLess(events.index("tag"), events.index("download"))
        commands = [call[0] for call in calls]
        environments = [call[1] for call in calls]
        release_download = next(
            index
            for index, command in enumerate(commands)
            if command[:3] == ["gh", "release", "download"]
        )
        attestation = next(
            index
            for index, command in enumerate(commands)
            if command[:3] == ["gh", "attestation", "verify"]
        )
        pip_install = next(
            index
            for index, command in enumerate(commands)
            if command[1:4] == ["-m", "pip", "install"]
        )
        installed_provenance = next(
            index
            for index, command in enumerate(commands)
            if any(
                argument.endswith("verify_code_search_wheel.py")
                for argument in command
            )
        )

        self.assertFalse(
            any(
                command[:3] == ["gh", "release", "verify-asset"]
                for command in commands
            )
        )
        self.assertLess(release_download, attestation)
        self.assertLess(attestation, pip_install)
        self.assertLess(pip_install, installed_provenance)
        self.assertEqual(environments[release_download], fetch_env)
        self.assertEqual(environments[attestation], runtime_env)
        self.assertEqual(environments[pip_install], runtime_env)
        self.assertEqual(environments[installed_provenance], runtime_env)
        self.assertNotIn("GH_TOKEN", environments[pip_install])
        self.assertIn("--bundle", commands[attestation])
        self.assertIn(bundle_name, commands[attestation])
        self.assertIn("--source-digest", commands[attestation])
        self.assertIn("a" * 40, commands[attestation])
        self.assertIn("--source-ref", commands[attestation])
        self.assertIn("refs/heads/main", commands[attestation])
        self.assertIn("--deny-self-hosted-runners", commands[attestation])
        self.assertIn("--force-reinstall", commands[pip_install])
        self.assertEqual(
            commands[pip_install][-1],
            str(destination / "code-search-download" / wheel_name),
        )
        self.assertIn("--asset-name", commands[installed_provenance])
        self.assertIn("--sha256", commands[installed_provenance])
        self.assertEqual(
            python,
            destination / "code-search-venv" / "bin" / "python",
        )
        self.assertEqual(
            executable,
            destination / "code-search-venv" / "bin" / "code-search-mcp",
        )

    def test_tag_resolution_peels_nested_annotated_tags(self):
        helper = load_helper()
        self.assertTrue(
            hasattr(helper, "resolve_release_tag_commit"),
            "trusted installer must resolve and peel the release tag",
        )
        tag_object = "b" * 40
        nested_tag_object = "c" * 40
        commit = "a" * 40
        responses = [
            subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps(
                    {"object": {"type": "tag", "sha": tag_object}}
                ),
            ),
            subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps(
                    {"object": {"type": "tag", "sha": nested_tag_object}}
                ),
            ),
            subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps(
                    {"object": {"type": "commit", "sha": commit}}
                ),
            ),
        ]
        fetch_env = {"PATH": "/bin", "GH_TOKEN": "private-token"}

        with mock.patch.object(
            helper.subprocess,
            "run",
            side_effect=responses,
        ) as run:
            resolved = helper.resolve_release_tag_commit(
                "redacted-org/code-search",
                "v0.2.0",
                fetch_env,
            )

        self.assertEqual(resolved, commit)
        self.assertEqual(run.call_count, 3)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(
            "repos/redacted-org/code-search/git/ref/tags/v0.2.0",
            commands[0],
        )
        self.assertIn(
            f"repos/redacted-org/code-search/git/tags/{tag_object}",
            commands[1],
        )
        self.assertIn(
            (
                "repos/redacted-org/code-search/git/tags/"
                f"{nested_tag_object}"
            ),
            commands[2],
        )
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["env"], fetch_env)

    def test_release_install_rejects_tag_commit_mismatch_before_download(self):
        helper = load_helper()
        fixture = (
            ROOT / "tests" / "fixtures" / "code-search-release-install.json"
        )
        install = json.loads(fixture.read_text(encoding="utf-8"))

        with (
            tempfile.TemporaryDirectory() as tmp,
            mock.patch.object(
                helper,
                "resolve_release_tag_commit",
                create=True,
                return_value="d" * 40,
            ),
            mock.patch.object(helper, "run") as run,
            self.assertRaisesRegex(
                helper.RealInstallError,
                "tag source revision mismatch",
            ),
        ):
            helper.install_code_search(
                install,
                Path(tmp),
                {"PATH": "/bin", "GH_TOKEN": "private-token"},
                {"PATH": "/bin"},
            )

        run.assert_not_called()

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
