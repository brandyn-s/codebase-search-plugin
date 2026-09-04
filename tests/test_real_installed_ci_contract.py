"""Acceptance contract for validating the real BOM components in CI."""

import importlib.util
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from released_bom import pending_bom, released_bom  # noqa: E402
WORKFLOW = ROOT / ".github" / "workflows" / "validate.yml"
TRUSTED_WORKFLOW = (
    ROOT / ".github" / "workflows" / "trusted-component-promotion.yml"
)
HELPER = ROOT / "scripts" / "validate_real_installed.py"
MODEL_BUILDER = ROOT / "scripts" / "build_readiness_model.py"
README = ROOT / "README.md"


def load_helper():
    scripts = str(ROOT / "scripts")
    sys.path.insert(0, scripts)
    try:
        spec = importlib.util.spec_from_file_location(
            "validate_real_installed_ci",
            HELPER,
        )
        if spec is None or spec.loader is None:
            raise AssertionError("could not load real-install helper")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(scripts)


class RealInstalledCIContractTests(unittest.TestCase):
    def test_load_bom_requires_the_optional_generator_contract(self):
        helper = load_helper()
        bom = released_bom(
            json.loads((ROOT / "component-bom.json").read_text(encoding="utf-8"))
        )
        del bom["precision_generators"]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "component-bom.json"
            path.write_text(json.dumps(bom), encoding="utf-8")
            with self.assertRaisesRegex(
                helper.RealInstallError,
                "component BOM is malformed",
            ):
                helper.load_bom(path)

    def test_validation_publishes_an_independently_verified_proof_packet(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        validate_job = workflow.split("  live-control-plane:", 1)[0]
        upload_action = (
            "actions/upload-artifact@"
            "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
        )

        self.assertIn("name: Export and verify portable proof packet", validate_job)
        self.assertIn(
            "python3 scripts/export_proof.py export "
            "bench/e2e/proof-fixture-v1.json",
            validate_job,
        )
        self.assertIn(
            'python3 scripts/export_proof.py verify "$RUNNER_TEMP/proof-packet"',
            validate_job,
        )
        self.assertIn(f"uses: {upload_action} # v7.0.1", validate_job)
        self.assertIn("name: code-intelligence-proof-packet", validate_job)
        self.assertIn("path: ${{ runner.temp }}/proof-packet", validate_job)
        self.assertIn("if-no-files-found: error", validate_job)
        self.assertIn("retention-days: 30", validate_job)
        self.assertLess(
            validate_job.index("scripts/export_proof.py verify"),
            validate_job.index(upload_action),
        )

    def test_distinct_ci_job_installs_and_validates_real_components(self):
        workflow = TRUSTED_WORKFLOW.read_text(encoding="utf-8")
        pull_request_workflow = WORKFLOW.read_text(encoding="utf-8")

        self.assertIn("validate-installed-components:", workflow)
        self.assertNotIn("secrets.", workflow)
        self.assertIn("python3 scripts/validate_real_installed.py", workflow)
        self.assertNotIn(
            "fake_mcp_server",
            workflow.split("validate-installed-components:", 1)[1],
        )
        self.assertNotIn("validate-installed-components:", pull_request_workflow)
        self.assertNotIn("secrets.", pull_request_workflow)
        self.assertNotIn("pull_request:", workflow)

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

        readme = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
        normalized_readme = " ".join(readme.lower().split())
        self.assertIn("trusted `main` push", normalized_readme)
        self.assertIn("publicly reachable brandyn-s releases", normalized_readme)
        self.assertNotIn("component token", normalized_readme)

    def test_trusted_job_uploads_successful_live_readiness_evidence(self):
        workflow = TRUSTED_WORKFLOW.read_text(encoding="utf-8")
        real_job = workflow.split("validate-installed-components:", 1)[1]
        normalized_job = " ".join(real_job.split())
        output = (
            "$RUNNER_TEMP/code-intel-ready-validation/"
            "readiness-evidence.json"
        )
        upload_action = (
            "actions/upload-artifact@"
            "043fb46d1a93c77aae656e7c1c64a875d1fc6a0a"
        )

        self.assertIn(
            "python3 scripts/validate_real_installed.py "
            "--component-bom component-bom.json "
            '--contract-evidence-output "$RUNNER_TEMP/'
            'code-intel-ready-validation/contracts" '
            f'--readiness-evidence-output "{output}"',
            normalized_job,
        )
        self.assertIn(f"uses: {upload_action} # v7.0.1", real_job)
        self.assertIn(
            "name: Validate exact installed components and live readiness",
            workflow,
        )
        self.assertIn("name: Upload trusted readiness evidence", real_job)
        self.assertIn("name: code-intel-ready-validation", real_job)
        self.assertIn(
            "path: ${{ runner.temp }}/code-intel-ready-validation",
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

    def test_trusted_job_is_gated_on_public_pins_and_uses_no_secrets(self):
        workflow = TRUSTED_WORKFLOW.read_text(encoding="utf-8")
        real_job = workflow.split("validate-installed-components:", 1)[1]

        self.assertNotIn("attestations: read", workflow)
        self.assertIn("workflow_dispatch:", workflow)
        self.assertIn("github.ref == 'refs/heads/main'", real_job)
        self.assertIn("github.event_name == 'push'", real_job)
        self.assertIn("github.event_name == 'workflow_dispatch'", real_job)
        self.assertNotIn("secrets.", workflow)
        self.assertNotIn("example-org/", workflow)

        # The gate runs before anything that touches a component release, and
        # every later step is conditioned on it.
        gate = "python3 scripts/promotion_gate.py --component-bom component-bom.json"
        self.assertIn("name: Gate on publicly reachable component pins", real_job)
        self.assertIn("id: gate", real_job)
        self.assertIn(gate, real_job)
        self.assertLess(real_job.index(gate), real_job.index("actions/setup-python@"))
        self.assertLess(
            real_job.index(gate),
            real_job.index("scripts/validate_real_installed.py"),
        )
        conditioned = real_job.split("id: gate", 1)[1]
        step_names = re.findall(r"^\s+- name: (.+)$", conditioned, flags=re.MULTILINE)
        self.assertEqual(
            step_names,
            [
                "Set up Python 3.12",
                "Install exact candidate and capture trusted evidence",
                "Upload trusted readiness evidence",
            ],
        )
        self.assertEqual(
            conditioned.count("if: steps.gate.outputs.run == 'true'"),
            len(step_names),
        )

        helper = load_helper()
        fetch_env, runtime_env = helper.build_subprocess_environments(
            "private-token",
            {
                "PATH": os.environ.get("PATH", ""),
                "LANG": "C.UTF-8",
                "GH_TOKEN": "ambient-token",
                "GITHUB_TOKEN": "ambient-actions-token",
                "AWS_SECRET_ACCESS_KEY": "must-not-leak",
                "OPENAI_API_KEY": "must-not-leak",
                "UNRELATED_CREDENTIAL": "must-not-leak",
                "SSH_AUTH_SOCK": "/tmp/credential-agent.sock",
            },
        )
        self.assertEqual(fetch_env["GH_TOKEN"], "private-token")
        self.assertEqual(fetch_env["PATH"], os.environ.get("PATH", ""))
        self.assertEqual(fetch_env["LANG"], "C.UTF-8")
        self.assertNotIn("GITHUB_TOKEN", fetch_env)
        self.assertNotIn("GH_TOKEN", runtime_env)
        self.assertNotIn("GITHUB_TOKEN", runtime_env)

        # Without an operator token the fetch environment carries no credential.
        unauthenticated_fetch, _ = helper.build_subprocess_environments(
            "",
            {"PATH": os.environ.get("PATH", ""), "GH_TOKEN": "ambient-token"},
        )
        self.assertNotIn("GH_TOKEN", unauthenticated_fetch)
        for environment in (fetch_env, runtime_env):
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", environment)
            self.assertNotIn("OPENAI_API_KEY", environment)
            self.assertNotIn("UNRELATED_CREDENTIAL", environment)
            self.assertNotIn("SSH_AUTH_SOCK", environment)

        helper_source = HELPER.read_text(encoding="utf-8")
        self.assertNotIn('"git", "-C", str(source), "fetch"', helper_source)

    def test_runtime_home_is_isolated_beneath_runner_temp(self):
        helper = load_helper()

        with tempfile.TemporaryDirectory() as tmp:
            runner_temp = Path(tmp).resolve()
            fetch_env, runtime_env = helper.build_subprocess_environments(
                "private-token",
                {
                    "PATH": os.environ.get("PATH", ""),
                    "RUNNER_TEMP": str(runner_temp),
                    "HOME": "/home/ambient-runner",
                    "GH_TOKEN": "ambient-token",
                    "AWS_SECRET_ACCESS_KEY": "must-not-leak",
                },
            )

            self.assertNotIn("HOME", fetch_env)
            self.assertNotIn("HOME", runtime_env)

            isolated = helper.build_isolated_runtime_environment(
                runtime_env,
                runner_temp,
            )
            isolated_home = Path(isolated["HOME"]).resolve()
            isolated_storage = Path(isolated["CODE_SEARCH_STORAGE"]).resolve()
            self.assertTrue(isolated_home.is_relative_to(runner_temp))
            self.assertTrue(isolated_storage.is_relative_to(runner_temp))
            self.assertEqual(isolated_home.name, "home")
            self.assertEqual(isolated_storage.name, "code-search-storage")
            self.assertEqual(isolated_home.parent, isolated_storage.parent)
            self.assertTrue(isolated_home.is_dir())
            self.assertTrue(isolated_storage.is_dir())
            self.assertNotIn("GH_TOKEN", isolated)
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", isolated)

            round_trip = helper.allowlisted_runtime_environment(isolated)
            self.assertEqual(round_trip["HOME"], str(isolated_home))
            self.assertEqual(round_trip["RUNNER_TEMP"], str(runner_temp))

    def test_runtime_paths_reject_symlink_file_and_escape_targets(self):
        helper = load_helper()

        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as outside_tmp,
        ):
            runner_temp = Path(tmp).resolve()
            outside = Path(outside_tmp).resolve()
            outside_home = outside / "home"
            outside_storage = outside / "storage"
            outside_home.mkdir()
            outside_storage.mkdir()
            linked_home = runner_temp / "linked-home"
            linked_storage = runner_temp / "linked-storage"
            linked_home.symlink_to(outside_home, target_is_directory=True)
            linked_storage.symlink_to(outside_storage, target_is_directory=True)
            regular_file = runner_temp / "not-a-directory"
            regular_file.write_text("not a directory\n", encoding="utf-8")

            sanitized = helper.allowlisted_runtime_environment(
                {
                    "PATH": os.environ.get("PATH", ""),
                    "RUNNER_TEMP": str(runner_temp),
                    "HOME": str(linked_home),
                    "CODE_SEARCH_STORAGE": str(linked_storage),
                }
            )
            self.assertNotIn("HOME", sanitized)
            self.assertNotIn("CODE_SEARCH_STORAGE", sanitized)

            file_sanitized = helper.allowlisted_runtime_environment(
                {
                    "PATH": os.environ.get("PATH", ""),
                    "RUNNER_TEMP": str(runner_temp),
                    "HOME": str(regular_file),
                    "CODE_SEARCH_STORAGE": str(regular_file),
                }
            )
            self.assertNotIn("HOME", file_sanitized)
            self.assertNotIn("CODE_SEARCH_STORAGE", file_sanitized)

            with (
                mock.patch.object(
                    helper.tempfile,
                    "mkdtemp",
                    return_value=str(outside),
                ),
                self.assertRaisesRegex(
                    helper.RealInstallError,
                    "escaped RUNNER_TEMP",
                ),
            ):
                helper.build_isolated_runtime_environment(
                    {"PATH": os.environ.get("PATH", "")},
                    runner_temp,
                )

    def test_pull_request_merge_gate_is_stable_and_fail_closed(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("pull_request:", workflow)
        self.assertIn("merge-gate:", workflow)
        merge_gate = workflow.split("merge-gate:", 1)[1]
        self.assertIn("name: merge-gate", merge_gate)
        self.assertIn("if: ${{ always() }}", merge_gate)
        self.assertIn(
            "needs: [validate, live-control-plane, windows-launchers]",
            merge_gate,
        )
        self.assertIn(
            "VALIDATE_RESULT: ${{ needs.validate.result }}",
            merge_gate,
        )
        self.assertIn(
            "LIVE_CONTROL_PLANE_RESULT: ${{ needs.live-control-plane.result }}",
            merge_gate,
        )
        self.assertIn('test "$VALIDATE_RESULT" = "success"', merge_gate)
        self.assertIn(
            'test "$LIVE_CONTROL_PLANE_RESULT" = "success"',
            merge_gate,
        )
        self.assertIn(
            "WINDOWS_LAUNCHERS_RESULT: ${{ needs.windows-launchers.result }}",
            merge_gate,
        )
        self.assertIn(
            'test "$WINDOWS_LAUNCHERS_RESULT" = "success"',
            merge_gate,
        )
        self.assertIn("  windows-launchers:", workflow)
        self.assertIn("runs-on: windows-latest", workflow)
        self.assertIn("tests.test_launcher_bootstrap_windows", workflow)
        self.assertNotIn("secrets.", workflow)

    def test_zero_cost_live_control_plane_job_is_secret_free_and_offline(self):
        workflow = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("  live-control-plane:", workflow)
        live_job = workflow.split("  live-control-plane:", 1)[1].split(
            "  merge-gate:",
            1,
        )[0]

        self.assertIn("persist-credentials: false", live_job)
        self.assertIn("egress-policy: block", live_job)
        allowed_endpoints = live_job.split(
            "allowed-endpoints: >",
            1,
        )[1].split("\n\n", 1)[0]
        endpoint_hosts = {
            line.strip().rsplit(":", 1)[0]
            for line in allowed_endpoints.splitlines()
            if line.strip()
        }
        self.assertEqual(
            endpoint_hosts,
            {
                "github.com",
                "api.github.com",
                "objects.githubusercontent.com",
            },
        )
        self.assertTrue(
            all(
                host == "github.com"
                or host.endswith(".github.com")
                or host.endswith(".githubusercontent.com")
                for host in endpoint_hosts
            )
        )
        for name in (
            "ANTHROPIC_API_KEY",
            "ANTHROPIC_AUTH_TOKEN",
            "VOYAGE_API_KEY",
            "AWS_ACCESS_KEY_ID",
            "AWS_SECRET_ACCESS_KEY",
            "AWS_SESSION_TOKEN",
            "GOOGLE_APPLICATION_CREDENTIALS",
            "ANTHROPIC_VERTEX_PROJECT_ID",
            "GH_TOKEN",
            "GITHUB_TOKEN",
        ):
            self.assertIn(f'{name}: ""', live_job)
        for test_target in (
            "tests.test_compare_live_runtime",
            "tests.test_compare_provenance",
            (
                "tests.test_compare_run.ComparisonRunTests."
                "test_live_preflight_without_authorities_reports_every_zero_cost_blocker"
            ),
            (
                "tests.test_compare_run.ComparisonRunTests."
                "test_live_preflight_rejects_self_attested_and_unverified_signed_authorities"
            ),
            (
                "tests.test_compare_run.ComparisonRunTests."
                "test_live_preflight_rejects_a_symlink_swapped_between_mkdir_and_open"
            ),
            (
                "tests.test_compare_run.ComparisonRunTests."
                "test_live_preflight_rejects_a_path_swap_immediately_after_directory_open"
            ),
            (
                "tests.test_compare_run.ComparisonRunTests."
                "test_live_preflight_rejects_a_path_swap_immediately_before_publication"
            ),
            (
                "tests.test_compare_run.ComparisonRunTests."
                "test_live_preflight_never_clobbers_a_diagnostic_won_by_a_racer"
            ),
            (
                "tests.test_compare_run.ComparisonRunTests."
                "test_live_preflight_removes_only_its_diagnostic_when_inventory_races_link"
            ),
            (
                "tests.test_compare_run.ComparisonRunTests."
                "test_live_preflight_rejects_unsupported_fd_capabilities_before_writing"
            ),
            (
                "tests.test_compare_run.ComparisonRunTests."
                "test_live_preflight_closes_directory_fd_when_evidence_hashing_fails"
            ),
            (
                "tests.test_compare_run.ComparisonRunTests."
                "test_live_preflight_rejects_a_stale_manifest_without_writing"
            ),
            (
                "tests.test_compare_run.ComparisonRunTests."
                "test_live_preflight_rejects_a_hardlinked_diagnostic_without_replacing_it"
            ),
            (
                "tests.test_compare_run.ComparisonRunTests."
                "test_live_preflight_rejects_every_unexpected_preexisting_entry"
            ),
            (
                "tests.test_compare_run.ComparisonRunTests."
                "test_live_preflight_rejects_unsafe_diagnostic_entry_types"
            ),
            (
                "tests.test_compare_run.ComparisonRunTests."
                "test_live_preflight_normalizes_a_non_directory_run_path"
            ),
            (
                "tests.test_compare_run.ComparisonRunTests."
                "test_live_preflight_never_overwrites_a_mismatched_existing_diagnostic"
            ),
            (
                "tests.test_compare_run.ComparisonRunTests."
                "test_live_preflight_normalizes_an_unwritable_diagnostic_path"
            ),
            (
                "tests.test_compare_run.ComparisonRunTests."
                "test_live_preflight_cleans_an_unsafe_new_temporary_diagnostic"
            ),
            (
                "tests.test_compare_run.ComparisonRunTests."
                "test_live_preflight_rejects_inventory_race_during_existing_snapshot"
            ),
            (
                "tests.test_compare_run.ComparisonRunTests."
                "test_live_preflight_reuses_its_only_exact_prior_diagnostic_without_writing"
            ),
            "tests.test_compare_documentation",
        ):
            self.assertIn(test_target, live_job)
        for forbidden in (
            "anthropic.com",
            "voyageai.com",
            "amazonaws.com",
            "googleapis.com",
            "/opt/anthropic/bin/claude",
            "claude -p",
            "python3 bench/compare/run.py",
            "pip install",
            "pyarrow",
            "curl ",
            "wget ",
        ):
            self.assertNotIn(forbidden, live_job)
        self.assertNotIn("secrets.", live_job)
        self.assertNotIn("continue-on-error", live_job)

    def test_load_bom_uses_the_exact_requested_candidate_path(self):
        helper = load_helper()
        with tempfile.TemporaryDirectory() as tmp:
            candidate_path = Path(tmp) / "candidate-bom.json"
            candidate = released_bom(
                json.loads(
                    (ROOT / "component-bom.json").read_text(encoding="utf-8")
                )
            )
            candidate["components"]["code-graph"]["install"]["tag"] = (
                "v9.9.9-candidate"
            )
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")

            loaded = helper.load_bom(candidate_path)

        self.assertEqual(
            loaded["components"]["code-graph"]["install"]["tag"],
            "v9.9.9-candidate",
        )

    def test_load_bom_refuses_a_pending_first_release_bom(self):
        helper = load_helper()
        with tempfile.TemporaryDirectory() as tmp:
            candidate_path = Path(tmp) / "component-bom.json"
            pending = pending_bom(
                json.loads((ROOT / "component-bom.json").read_text(encoding="utf-8"))
            )
            candidate_path.write_text(json.dumps(pending), encoding="utf-8")
            with self.assertRaisesRegex(
                helper.RealInstallError, "pending-first-release"
            ):
                helper.load_bom(candidate_path)

    def test_load_bom_rejects_unknown_install_descriptor_fields(self):
        helper = load_helper()
        with tempfile.TemporaryDirectory() as tmp:
            candidate_path = Path(tmp) / "candidate-bom.json"
            candidate = released_bom(
                json.loads(
                    (ROOT / "component-bom.json").read_text(encoding="utf-8")
                )
            )
            candidate["components"]["code-graph"]["install"]["assets"][
                "linux-amd64"
            ]["unexpected_policy"] = True
            candidate_path.write_text(json.dumps(candidate), encoding="utf-8")

            with self.assertRaisesRegex(
                helper.RealInstallError,
                "keys must exactly match",
            ):
                helper.load_bom(candidate_path)

    def test_release_wheel_is_verified_offline_before_secret_free_install(self):
        helper = load_helper()
        wheel_bytes = b"pinned wheel"
        bundle_bytes = b"offline attestation bundle"
        wheel_name = "code_search_mcp-0.2.1-py3-none-any.whl"
        bundle_name = "code_search_mcp-0.2.1-provenance.jsonl"
        checksums_name = "SHA256SUMS"
        checksums_bytes = (
            f"{hashlib.sha256(wheel_bytes).hexdigest()}  {wheel_name}\n"
        ).encode()
        install = {
            "kind": "github-release",
            "repository": "brandyn-s/code-search",
            "tag": "v0.2.1",
            "source_revision": "a" * 40,
            "asset": {
                "name": wheel_name,
                "sha256": hashlib.sha256(wheel_bytes).hexdigest(),
            },
            "checksums": {
                "name": checksums_name,
                "sha256": hashlib.sha256(checksums_bytes).hexdigest(),
            },
            "attestation": {
                "bundle": {
                    "name": bundle_name,
                    "sha256": hashlib.sha256(bundle_bytes).hexdigest(),
                },
                "deny_self_hosted_runners": True,
                "signer_workflow": (
                    "brandyn-s/code-search/"
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
                (download_dir / checksums_name).write_bytes(checksums_bytes)
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
                mock.patch.object(
                    helper,
                    "verify_checksum_manifest",
                    wraps=helper.verify_checksum_manifest,
                ) as verify_manifest,
            ):
                python, executable = helper.install_code_search(
                    install,
                    destination,
                    fetch_env,
                    runtime_env,
                )

        resolve.assert_called_once_with(
            "brandyn-s/code-search",
            "v0.2.1",
            fetch_env,
        )
        verify_manifest.assert_called_once_with(
            destination / "code-search-download" / checksums_name,
            wheel_name,
            hashlib.sha256(wheel_bytes).hexdigest(),
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
        self.assertIn(checksums_name, commands[release_download])
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
            "code-search-mcp[local] @ "
            + (destination / "code-search-download" / wheel_name).resolve().as_uri(),
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
                "brandyn-s/code-search",
                "v0.2.0",
                fetch_env,
            )

        self.assertEqual(resolved, commit)
        self.assertEqual(run.call_count, 3)
        commands = [call.args[0] for call in run.call_args_list]
        self.assertIn(
            "repos/brandyn-s/code-search/git/ref/tags/v0.2.0",
            commands[0],
        )
        self.assertIn(
            f"repos/brandyn-s/code-search/git/tags/{tag_object}",
            commands[1],
        )
        self.assertIn(
            (
                "repos/brandyn-s/code-search/git/tags/"
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

    def test_graph_release_uses_hash_bound_offline_attestation_before_extraction(self):
        helper = load_helper()
        archive_buffer = io.BytesIO()
        binary_bytes = b"verified graph binary"
        with tarfile.open(fileobj=archive_buffer, mode="w:gz") as archive:
            entry = tarfile.TarInfo("code-graph")
            entry.mode = 0o755
            entry.size = len(binary_bytes)
            archive.addfile(entry, io.BytesIO(binary_bytes))
        archive_bytes = archive_buffer.getvalue()
        archive_name = "code-graph-linux-amd64.tar.gz"
        archive_sha256 = hashlib.sha256(archive_bytes).hexdigest()
        checksums_name = "checksums.txt"
        checksums_bytes = f"{archive_sha256}  {archive_name}\n".encode()
        attestation_bundle_bytes = b'{"bundle":"operator-fetched"}\n'
        attestation_bundle_path = (
            "compatibility/attestations/"
            "code-graph-v0.7.0-internal.3-provenance.jsonl"
        )
        install = {
            "kind": "github-release",
            "repository": "brandyn-s/code-graph",
            "tag": "v0.7.0-internal.3",
            "source_revision": "a" * 40,
            "assets": {
                "linux-amd64": {
                    "name": archive_name,
                    "sha256": archive_sha256,
                }
            },
            "checksums": {
                "name": checksums_name,
                "sha256": hashlib.sha256(checksums_bytes).hexdigest(),
            },
            "attestation": {
                "bundle": {
                    "path": attestation_bundle_path,
                    "sha256": hashlib.sha256(
                        attestation_bundle_bytes
                    ).hexdigest(),
                },
                "deny_self_hosted_runners": True,
                "signer_workflow": (
                    "brandyn-s/code-graph/"
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
                (download_dir / archive_name).write_bytes(archive_bytes)
                (download_dir / checksums_name).write_bytes(checksums_bytes)
            elif command[:3] == ["gh", "attestation", "download"]:
                raise AssertionError(
                    "regression: the release-only token cannot call the "
                    "cross-repository Attestations API"
                )
            elif command[:3] == ["gh", "attestation", "verify"]:
                events.append("attestation")

        def resolve_tag(*_args, **_kwargs):
            events.append("tag")
            return "a" * 40

        real_tarfile_open = helper.tarfile.open

        def open_archive(*args, **kwargs):
            events.append("extract")
            return real_tarfile_open(*args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp)
            plugin_root = destination / "plugin"
            bundle = plugin_root / attestation_bundle_path
            bundle.parent.mkdir(parents=True)
            bundle.write_bytes(attestation_bundle_bytes)
            with (
                mock.patch.object(helper, "run", side_effect=emulate),
                mock.patch.object(helper, "ROOT", plugin_root),
                mock.patch.object(
                    helper,
                    "resolve_release_tag_commit",
                    side_effect=resolve_tag,
                ) as resolve,
                mock.patch.object(
                    helper,
                    "verify_checksum_manifest",
                    wraps=helper.verify_checksum_manifest,
                ) as verify_manifest,
                mock.patch.object(helper.platform, "system", return_value="Linux"),
                mock.patch.object(helper.platform, "machine", return_value="x86_64"),
                mock.patch.object(
                    helper.tarfile,
                    "open",
                    side_effect=open_archive,
                ),
            ):
                executable = helper.install_code_graph(
                    install,
                    destination,
                    fetch_env,
                    runtime_env,
                )

            self.assertEqual(executable.read_bytes(), binary_bytes)

        resolve.assert_called_once_with(
            "brandyn-s/code-graph",
            "v0.7.0-internal.3",
            fetch_env,
        )
        verify_manifest.assert_called_once_with(
            destination / "code-graph-download" / checksums_name,
            archive_name,
            archive_sha256,
        )
        self.assertEqual(
            events,
            [
                "tag",
                "download",
                "attestation",
                "extract",
            ],
        )
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
        self.assertEqual(environments[release_download], fetch_env)
        self.assertEqual(environments[attestation], runtime_env)
        self.assertNotIn(
            ["gh", "attestation", "download"],
            [command[:3] for command in commands],
        )
        self.assertIn(archive_name, commands[release_download])
        self.assertIn(checksums_name, commands[release_download])
        self.assertIn("--bundle", commands[attestation])
        self.assertIn(str(bundle), commands[attestation])
        for required in (
            "--repo",
            "brandyn-s/code-graph",
            "--signer-workflow",
            "brandyn-s/code-graph/.github/workflows/release.yml",
            "--source-digest",
            "a" * 40,
            "--source-ref",
            "refs/heads/main",
            "--deny-self-hosted-runners",
        ):
            self.assertIn(required, commands[attestation])

    def test_graph_release_rejects_tag_commit_mismatch_before_download(self):
        helper = load_helper()
        bom = released_bom(
            json.loads((ROOT / "component-bom.json").read_text(encoding="utf-8"))
        )
        install = bom["components"]["code-graph"]["install"]
        bundle_bytes = b'{"bundle":"fixture"}\n'
        install["attestation"]["bundle"]["sha256"] = hashlib.sha256(
            bundle_bytes
        ).hexdigest()

        with (
            tempfile.TemporaryDirectory() as tmp,
            tempfile.TemporaryDirectory() as plugin_tmp,
            mock.patch.object(
                helper,
                "resolve_release_tag_commit",
                return_value="d" * 40,
            ),
            mock.patch.object(helper.platform, "system", return_value="Linux"),
            mock.patch.object(helper.platform, "machine", return_value="x86_64"),
            mock.patch.object(helper, "run") as run,
            mock.patch.object(helper, "ROOT", Path(plugin_tmp)),
            self.assertRaisesRegex(
                helper.RealInstallError,
                "tag source revision mismatch",
            ),
        ):
            bundle = Path(plugin_tmp) / install["attestation"]["bundle"]["path"]
            bundle.parent.mkdir(parents=True)
            bundle.write_bytes(bundle_bytes)
            helper.install_code_graph(
                install,
                Path(tmp),
                {"PATH": "/bin", "GH_TOKEN": "private-token"},
                {"PATH": "/bin"},
            )

        run.assert_not_called()

    def test_go_scip_release_is_hash_bound_and_verified_by_trusted_install(self):
        helper = load_helper()
        archive_buffer = io.BytesIO()
        binary_bytes = b"verified scip-go binary"
        with tarfile.open(fileobj=archive_buffer, mode="w:gz") as archive:
            entry = tarfile.TarInfo("scip-go")
            entry.mode = 0o755
            entry.size = len(binary_bytes)
            archive.addfile(entry, io.BytesIO(binary_bytes))
        archive_bytes = archive_buffer.getvalue()
        archive_name = "scip-go-linux-amd64.tar.gz"
        generator = {
            "kind": "github-release",
            "repository": "scip-code/scip-go",
            "tag": "v0.2.7",
            "source_revision": "a" * 40,
            "version_output": "0.2.7",
            "assets": {
                "linux-amd64": {
                    "name": archive_name,
                    "archive_sha256": hashlib.sha256(archive_bytes).hexdigest(),
                    "binary_sha256": hashlib.sha256(binary_bytes).hexdigest(),
                }
            },
        }
        fetch_env = {"PATH": "/bin", "GH_TOKEN": "private-token"}
        runtime_env = {"PATH": "/bin"}
        events = []

        def emulate(command, *, env, cwd=None):
            if command[:3] == ["gh", "release", "download"]:
                events.append("download")
                download_dir = Path(command[command.index("--dir") + 1])
                download_dir.mkdir(parents=True, exist_ok=True)
                (download_dir / archive_name).write_bytes(archive_bytes)
            elif str(ROOT / "scripts" / "prepare_scip_index.py") in command:
                events.append("verify")

        def resolve_tag(*_args, **_kwargs):
            events.append("tag")
            return "a" * 40

        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp)
            candidate_bom = destination / "component-bom.json"
            candidate_bom.write_text("{}\n", encoding="utf-8")
            with (
                mock.patch.object(helper, "run", side_effect=emulate) as run,
                mock.patch.object(
                    helper,
                    "resolve_release_tag_commit",
                    side_effect=resolve_tag,
                ) as resolve,
                mock.patch.object(helper.platform, "system", return_value="Linux"),
                mock.patch.object(helper.platform, "machine", return_value="x86_64"),
            ):
                executable = helper.install_go_scip(
                    generator,
                    candidate_bom,
                    destination,
                    fetch_env,
                    runtime_env,
                )

            self.assertEqual(executable.read_bytes(), binary_bytes)

        resolve.assert_called_once_with(
            "scip-code/scip-go",
            "v0.2.7",
            fetch_env,
        )
        self.assertEqual(events, ["tag", "download", "verify"])
        verify_command = run.call_args_list[-1].args[0]
        self.assertIn("verify", verify_command)
        self.assertIn("--generator", verify_command)
        self.assertIn(str(candidate_bom), verify_command)

        helper_source = HELPER.read_text(encoding="utf-8")
        self.assertIn(
            'bom["precision_generators"]["go-scip"]',
            helper_source,
        )

    def test_typescript_scip_runtime_is_hash_bound_and_verified_by_trusted_install(self):
        helper = load_helper()
        node_bytes = b"verified pinned node runtime"
        npm_cli_bytes = b"// npm cli fixture\n"
        archive_buffer = io.BytesIO()
        with tarfile.open(fileobj=archive_buffer, mode="w:xz") as archive:
            for name, content, mode in (
                ("node-v22.23.2-linux-x64/bin/node", node_bytes, 0o755),
                (
                    "node-v22.23.2-linux-x64/lib/node_modules/npm/bin/npm-cli.js",
                    npm_cli_bytes,
                    0o644,
                ),
            ):
                entry = tarfile.TarInfo(name)
                entry.mode = mode
                entry.size = len(content)
                archive.addfile(entry, io.BytesIO(content))
            npm_link = tarfile.TarInfo("node-v22.23.2-linux-x64/bin/npm")
            npm_link.type = tarfile.SYMTYPE
            npm_link.linkname = "../lib/node_modules/npm/bin/npm-cli.js"
            archive.addfile(npm_link)
        archive_bytes = archive_buffer.getvalue()
        generator_bytes = b"// verified scip-typescript entrypoint\n"
        asset_name = "node-v22.23.2-linux-x64.tar.xz"
        lockfile = ROOT / "compatibility" / "scip-typescript-package-lock.json"
        generator = {
            "kind": "npm-lockfile",
            "package": "@sourcegraph/scip-typescript",
            "version_output": "0.4.0",
            "source_repository": "sourcegraph/scip-typescript",
            "source_revision": "1962a68386220dd669c3839b69d64fb5ce34f2a6",
            "package_integrity": (
                "sha512-k+AtsrqmS41Sd5qjkZlHcmvoSQIvBOonRj4jpgp0"
                "KNFM6aqvMGpdSuPUqrUcg8ENTKjUbfaUVszgQwq3bCOvwA=="
            ),
            "package_manifest": "compatibility/scip-typescript-package.json",
            "lockfile": "compatibility/scip-typescript-package-lock.json",
            "lockfile_sha256": hashlib.sha256(lockfile.read_bytes()).hexdigest(),
            "entrypoint": (
                "node_modules/@sourcegraph/scip-typescript/dist/src/main.js"
            ),
            "entrypoint_sha256": hashlib.sha256(generator_bytes).hexdigest(),
            "supported_node_majors": [22],
            "node_runtime": {
                "version": "v22.23.2",
                "base_url": "https://nodejs.org/download/release/v22.23.2",
                "assets": {
                    "linux-amd64": {
                        "name": asset_name,
                        "archive_sha256": hashlib.sha256(archive_bytes).hexdigest(),
                        "binary_sha256": hashlib.sha256(node_bytes).hexdigest(),
                    }
                },
            },
        }
        events = []

        def emulate(command, *, env, cwd=None):
            if command[0] == "curl":
                events.append("download")
                output = Path(command[command.index("--output") + 1])
                output.write_bytes(archive_bytes)
            elif any("npm-cli.js" in argument for argument in command):
                events.append("npm-ci")
                package_root = Path(command[command.index("--prefix") + 1])
                entrypoint = package_root / generator["entrypoint"]
                entrypoint.parent.mkdir(parents=True, exist_ok=True)
                entrypoint.write_bytes(generator_bytes)
            elif str(ROOT / "scripts" / "prepare_scip_index.py") in command:
                events.append("verify")

        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp)
            candidate_bom = destination / "component-bom.json"
            candidate_bom.write_text("{}\n", encoding="utf-8")
            with (
                mock.patch.object(helper, "run", side_effect=emulate),
                mock.patch.object(helper.platform, "system", return_value="Linux"),
                mock.patch.object(helper.platform, "machine", return_value="x86_64"),
            ):
                node, entrypoint = helper.install_typescript_scip(
                    generator,
                    candidate_bom,
                    destination,
                    {"PATH": "/bin"},
                )

            self.assertEqual(node.read_bytes(), node_bytes)
            self.assertEqual(entrypoint.read_bytes(), generator_bytes)
            self.assertEqual(events, ["download", "npm-ci", "verify"])

        helper_source = HELPER.read_text(encoding="utf-8")
        self.assertIn(
            'bom["precision_generators"]["typescript-scip"]',
            helper_source,
        )

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

    def test_contract_capture_binds_exact_candidate_and_installed_servers(self):
        helper = load_helper()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            candidate_bom = directory / "candidate-bom.json"
            candidate_bom.write_text("{}\n", encoding="utf-8")
            code_search = directory / "venv" / "bin" / "code-search-mcp"
            code_graph = directory / "bin" / "codebase-memory-mcp"
            output = directory / "evidence" / "contracts"
            polluted_env = {
                "PATH": os.environ.get("PATH", ""),
                "AWS_SECRET_ACCESS_KEY": "must-not-leak",
                "UNRELATED_CREDENTIAL": "must-not-leak",
            }

            def emulate(command, *, env, cwd=None):
                self.assertTrue(output.parent.is_dir())
                (output / "compatibility").mkdir(parents=True)
                (output / "component-bom.json").write_text(
                    "{}\n",
                    encoding="utf-8",
                )
                for component in ("code-search", "code-graph"):
                    (output / "compatibility" / f"{component}-tools.json").write_text(
                        "{}\n",
                        encoding="utf-8",
                    )

            with mock.patch.object(helper, "run", side_effect=emulate) as run:
                helper.capture_installed_contract_evidence(
                    candidate_bom,
                    output,
                    directory / "venv" / "bin" / "python",
                    code_search,
                    code_graph,
                    polluted_env,
                )

            command = run.call_args.args[0]
            self.assertEqual(
                command[command.index("--component-bom") + 1],
                str(candidate_bom),
            )
            self.assertEqual(
                command[command.index("--output-dir") + 1],
                str(output),
            )
            self.assertIn(f"code-search={code_search}", command)
            self.assertIn(f"code-graph={code_graph}", command)
            self.assertIn("--write", command)
            called_env = run.call_args.kwargs["env"]
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", called_env)
            self.assertNotIn("UNRELATED_CREDENTIAL", called_env)

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
                    directory / "candidate-bom.json",
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
                    directory / "candidate-bom.json",
                    output,
                    directory / "venv" / "bin" / "python",
                    {"PATH": os.environ.get("PATH", "")},
                )

            run.assert_called_once()
            command = run.call_args.args[0]
            self.assertEqual(
                command[command.index("--component-bom") + 1],
                str(directory / "candidate-bom.json"),
            )
            self.assertEqual(output.read_bytes(), evidence)

    def test_live_validation_retains_runner_temp_without_credentials(self):
        helper = load_helper()

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            source = directory / "live-readiness-evidence.json"
            output = directory / "artifact" / "readiness-evidence.json"
            source.write_text('{"schema_version": 1}\n', encoding="utf-8")
            runtime_env = {
                "PATH": os.environ.get("PATH", ""),
                "RUNNER_TEMP": str(directory),
                "AWS_SECRET_ACCESS_KEY": "must-not-leak",
                "OPENAI_API_KEY": "must-not-leak",
                "UNRELATED_CREDENTIAL": "must-not-leak",
                "SSH_AUTH_SOCK": "/tmp/credential-agent.sock",
            }

            with mock.patch.object(helper, "run") as run:
                helper.validate_and_publish_live_evidence(
                    source,
                    directory / "candidate-bom.json",
                    output,
                    directory / "venv" / "bin" / "python",
                    runtime_env,
                )

            called_env = run.call_args.kwargs["env"]
            self.assertEqual(called_env["RUNNER_TEMP"], str(directory))
            self.assertEqual(
                called_env["CODE_INTEL_READINESS_EVIDENCE_OVERRIDE"],
                str(source),
            )
            for secret_name in (
                "AWS_SECRET_ACCESS_KEY",
                "OPENAI_API_KEY",
                "UNRELATED_CREDENTIAL",
                "SSH_AUTH_SOCK",
            ):
                self.assertNotIn(secret_name, called_env)

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
                    directory / "candidate-bom.json",
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
            candidate_bom = destination / "candidate-bom.json"
            candidate_bom.write_text("{}\n", encoding="utf-8")
            runtime_env = {
                "PATH": os.environ.get("PATH", ""),
                "CODE_INTEL_LIVE_READINESS_EVIDENCE": "/attacker/supplied.json",
                "VOYAGE_API_KEY": "must-not-reach-smoke",
                "OPENAI_API_KEY": "must-not-reach-smoke",
                "ANTHROPIC_API_KEY": "must-not-reach-smoke",
                "AWS_SECRET_ACCESS_KEY": "must-not-reach-smoke",
                "UNRELATED_CREDENTIAL": "must-not-reach-smoke",
                "SSH_AUTH_SOCK": "/tmp/credential-agent.sock",
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
                        candidate_bom,
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
                    candidate_bom,
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
                command[command.index("--component-bom") + 1],
                str(candidate_bom),
            )
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
            self.assertNotIn("AWS_SECRET_ACCESS_KEY", called_env)
            self.assertNotIn("UNRELATED_CREDENTIAL", called_env)
            self.assertNotIn("SSH_AUTH_SOCK", called_env)

        helper_source = HELPER.read_text(encoding="utf-8")
        self.assertNotIn(
            'os.environ.get("CODE_INTEL_LIVE_READINESS_EVIDENCE"',
            helper_source,
        )
        self.assertIn("CODE_INTEL_READINESS_EVIDENCE_OVERRIDE", helper_source)
        self.assertIn("validate_plugin.py", helper_source)


if __name__ == "__main__":
    unittest.main()
