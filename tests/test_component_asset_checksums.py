"""Supply-chain contracts for pinned code-graph release assets."""

import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "validate_real_installed.py"

EXPECTED_ASSETS = {
    "darwin-amd64": {
        "name": "codebase-memory-mcp-darwin-amd64.tar.gz",
        "sha256": "7a3b4fd97c2b5628a1f745065424e73a4eda718fd5c7febc5c9128422a764b25",
    },
    "darwin-arm64": {
        "name": "codebase-memory-mcp-darwin-arm64.tar.gz",
        "sha256": "77f91cb45cd179bd0f11bc9ed0c87b4334354a096e89a321303e1df0a70b6fc9",
    },
    "linux-amd64": {
        "name": "codebase-memory-mcp-linux-amd64.tar.gz",
        "sha256": "be9731783b2aead9bb26eb7851d7c614f82176469a6881d7e5ded1e1573356ff",
    },
    "linux-arm64": {
        "name": "codebase-memory-mcp-linux-arm64.tar.gz",
        "sha256": "2091cf6e26373843eda84dedeee747eb528b75ceee0d3a4ca5595441a1cd14cd",
    },
    "windows-amd64": {
        "name": "codebase-memory-mcp-windows-amd64.zip",
        "sha256": "cb99adcd8e7e9b2b4f199ccd0f17b9ea6a4ebd4904597879cd219a19e1fe563b",
    },
}

EXPECTED_GRAPH_INSTALL = {
    "assets": EXPECTED_ASSETS,
    "attestation": {
        "bundle": {
            "path": (
                "compatibility/attestations/"
                "code-graph-v0.8.0-redacted.6-provenance.jsonl"
            ),
            "sha256": (
                "7bde5e65123cc413dd17ef5ec917178d3be1ce75ee8e3201d52afdb43be4c455"
            ),
        },
        "deny_self_hosted_runners": True,
        "signer_workflow": (
            "redacted-org/code-graph/.github/workflows/release.yml"
        ),
        "source_ref": "refs/heads/main",
    },
    "checksums": {
        "name": "checksums.txt",
        "sha256": (
            "fb6eab7ea9cce568ff4b271954b8befef22331c0c449f00c3dbc97d04edefbb6"
        ),
    },
    "kind": "github-release",
    "repository": "redacted-org/code-graph",
    "source_revision": "3eae292836f45932be7faa237a1c2adc6721c69b",
    "tag": "v0.8.0-redacted.6",
}

EXPECTED_SEARCH_INSTALL = {
    "asset": {
        "name": "redacted_code_search-0.3.5-py3-none-any.whl",
        "sha256": (
            "6d0b7475eab8d53b55f0c1457848edf813d66368ff412fc662ab824eef17a82f"
        ),
    },
    "attestation": {
        "bundle": {
            "name": "redacted_code_search-0.3.5-provenance.jsonl",
            "sha256": (
                "c3225e28e13d4251d1a6f7d1a23eb3f9002cd8f860fa455f323a0f281df2bde2"
            ),
        },
        "deny_self_hosted_runners": True,
        "signer_workflow": (
            "redacted-org/code-search/.github/workflows/release.yml"
        ),
        "source_ref": "refs/heads/main",
    },
    "checksums": {
        "name": "SHA256SUMS",
        "sha256": (
            "01c0a96d37dc16b569fde6de1e38dcce90c3a4a4a57d2dcea87221b77f6b74d9"
        ),
    },
    "kind": "github-release",
    "repository": "redacted-org/code-search",
    "source_revision": "71764535b7bbf8f2f2d9c113f76f955fa48dda59",
    "tag": "v0.3.5",
}


def load_helper():
    scripts = str(HELPER.parent)
    added = scripts not in sys.path
    if added:
        sys.path.insert(0, scripts)
    spec = importlib.util.spec_from_file_location("validate_real_installed", HELPER)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load real-install helper")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    finally:
        if added:
            sys.path.remove(scripts)
    return module


class ComponentAssetChecksumTests(unittest.TestCase):
    def test_graph_snapshot_matches_released_pagination_and_trace_contract(self):
        snapshot = json.loads(
            (ROOT / "compatibility" / "code-graph-tools.json").read_text(
                encoding="utf-8"
            )
        )
        search_code = snapshot["tools"]["search_code"]
        trace_call_path = snapshot["tools"]["trace_call_path"]

        self.assertEqual(
            search_code["input_schema_sha256"],
            "3975a99e2dfab690a9f01c22d68f61e486c53c8ccd1ea5fb47e7ae58a316d7c8",
        )
        self.assertEqual(
            search_code["input_schema"]["properties"]["max_results"],
            {
                "description": (
                    "Max matches per page (default: 10, max: 1000). "
                    "Response includes has_more flag for pagination."
                ),
                "maximum": 1000,
                "minimum": 1,
                "type": "integer",
            },
        )
        self.assertEqual(
            search_code["input_schema"]["properties"]["offset"],
            {
                "description": (
                    "Skip N matches for pagination (default: 0). "
                    "Check has_more in response."
                ),
                "maximum": 1000000,
                "minimum": 0,
                "type": "integer",
            },
        )

        self.assertEqual(
            trace_call_path["input_schema_sha256"],
            "d60814c5e8755a72bb148fa61dba8f9de4c7c6b42acc4dd739e97b10d1543214",
        )
        self.assertEqual(
            trace_call_path["input_schema"]["properties"]["edge_types"],
            {
                "default": ["CALLS", "HTTP_CALLS", "ASYNC_CALLS"],
                "description": (
                    "Relationship types to traverse. Defaults to call-like "
                    "relationships: CALLS, HTTP_CALLS, ASYNC_CALLS. Non-call "
                    "relationships are opt-in: USAGE, OVERRIDE."
                ),
                "items": {
                    "enum": [
                        "CALLS",
                        "HTTP_CALLS",
                        "ASYNC_CALLS",
                        "USAGE",
                        "OVERRIDE",
                    ],
                    "type": "string",
                },
                "maxItems": 5,
                "minItems": 1,
                "type": "array",
                "uniqueItems": True,
            },
        )
        self.assertEqual(
            trace_call_path["input_schema"]["properties"]["min_confidence"],
            {
                "description": (
                    "Minimum confidence threshold (0.0-1.0) for all selected "
                    "edge types. Filters out low-confidence fuzzy matches. "
                    "Edges with missing or null confidence remain traversable; "
                    "an explicit numeric zero is filtered when the threshold "
                    "is positive. Bands: high (>=0.7), medium (>=0.45), "
                    "speculative (<0.45). Default 0.45 — filters speculative "
                    "cross-crate name-only matches that frequently resolve to "
                    "wrong-crate same-named methods. Pass 0 explicitly to "
                    "disable filtering and see the full unfiltered trace. "
                    "confidence_band is unknown whenever min_confidence is "
                    "positive because the resolved-edge numerator is filtered "
                    "while unresolved_call_count is not."
                ),
                "maximum": 1,
                "minimum": 0,
                "type": "number",
            },
        )

    def test_bom_pins_exact_code_search_release_descriptor(self):
        bom = json.loads((ROOT / "component-bom.json").read_text(encoding="utf-8"))

        self.assertEqual(
            bom["components"]["code-search"]["install"],
            EXPECTED_SEARCH_INSTALL,
        )

    def test_bom_pins_exact_code_graph_release_descriptor(self):
        bom = json.loads((ROOT / "component-bom.json").read_text(encoding="utf-8"))
        graph_install = bom["components"]["code-graph"]["install"]

        self.assertEqual(graph_install, EXPECTED_GRAPH_INSTALL)

    def test_ci_helper_fails_closed_for_missing_invalid_and_mismatched_digest(self):
        helper = load_helper()
        with tempfile.TemporaryDirectory() as tmp:
            archive = Path(tmp) / "asset.tar.gz"

            with self.assertRaisesRegex(helper.RealInstallError, "missing"):
                helper.verify_sha256(archive, "0" * 64)

            archive.write_bytes(b"pinned release asset")
            with self.assertRaisesRegex(helper.RealInstallError, "SHA-256"):
                helper.verify_sha256(archive, "")
            with self.assertRaisesRegex(helper.RealInstallError, "mismatch"):
                helper.verify_sha256(archive, "0" * 64)

            helper.verify_sha256(
                archive,
                "2130f976dae862442a2e3ec8090d052b7e75320e58d8c374ec137c228637dcbf",
            )

    def test_installer_checksum_helpers_never_delete_vendored_inputs(self):
        shell = (ROOT / "install.sh").read_text(encoding="utf-8")
        powershell = (ROOT / "install.ps1").read_text(encoding="utf-8")
        shell_helper = shell.split("verify_sha256() {", 1)[1].split("\n}", 1)[0]
        powershell_helper = powershell.split(
            "function Assert-Sha256 {", 1
        )[1].split("\n}", 1)[0]

        self.assertNotIn('rm -f "$file"', shell_helper)
        self.assertNotIn("Remove-Item -LiteralPath $Path", powershell_helper)

    def test_ci_helper_requires_one_exact_checksum_manifest_entry(self):
        helper = load_helper()
        artifact_name = "component.tar.gz"
        artifact_digest = "a" * 64

        with tempfile.TemporaryDirectory() as tmp:
            manifest = Path(tmp) / "checksums.txt"

            manifest.write_text(
                f"{artifact_digest}  {artifact_name}\n",
                encoding="utf-8",
            )
            helper.verify_checksum_manifest(
                manifest,
                artifact_name,
                artifact_digest,
            )

            for contents in (
                "",
                f"{artifact_digest}  copied-{artifact_name}\n",
                f"{'b' * 64}  {artifact_name}\n",
                (
                    f"{artifact_digest}  {artifact_name}\n"
                    f"{artifact_digest}  {artifact_name}\n"
                ),
            ):
                manifest.write_text(contents, encoding="utf-8")
                with self.assertRaisesRegex(
                    helper.RealInstallError,
                    "checksum manifest",
                ):
                    helper.verify_checksum_manifest(
                        manifest,
                        artifact_name,
                        artifact_digest,
                    )

    def test_graph_installers_fail_closed_and_attest_before_extraction(self):
        shell = (ROOT / "install.sh").read_text(encoding="utf-8")
        powershell = (ROOT / "install.ps1").read_text(encoding="utf-8")

        for installer in (shell, powershell):
            self.assertIn("GH_TOKEN", installer)
            self.assertIn("gh release download", installer)
            self.assertIn("assets", installer)
            self.assertIn("checksums", installer)
            self.assertIn("source_revision", installer)
            self.assertIn("signer_workflow", installer)
            self.assertIn("source_ref", installer)
            self.assertIn("--deny-self-hosted-runners", installer)
            self.assertIn("checksum mismatch", installer)
            self.assertIn("checksums.txt", installer)
            self.assertNotIn("skipping verification", installer)
            self.assertNotIn("public release URL fallback", installer)

        self.assertIn("sha256sum", shell)
        self.assertIn("shasum", shell)
        self.assertIn('["attestation"]["bundle"]', shell)
        self.assertIn(".attestation.bundle", powershell)
        self.assertIn("Get-FileHash", powershell)
        self.assertNotIn("Invoke-WebRequest", powershell)

        shell_graph = shell.split(
            "[3/5] Installing code-graph (structural analysis)...",
            1,
        )[1].split("[4/5] Creating launcher scripts...", 1)[0]
        powershell_graph = powershell.split(
            "[3/5] Installing code-graph (structural analysis)...",
            1,
        )[1].split("[4/5] Creating launcher scripts", 1)[0]
        for graph, resolver, checksum_verifier, extraction in (
            (
                shell_graph,
                "resolve_release_tag_commit",
                "verify_checksum_manifest",
                "tar xzf",
            ),
            (
                powershell_graph,
                "Resolve-ReleaseTagCommit",
                "Assert-ChecksumManifest",
                "Expand-Archive",
            ),
        ):
            self.assertIn(resolver, graph)
            self.assertIn(checksum_verifier, graph)
            self.assertNotIn("gh attestation download", graph)
            self.assertIn("gh attestation verify", graph)
            self.assertIn("--bundle", graph)
            self.assertLess(graph.index(resolver), graph.index("gh release download"))
            self.assertLess(
                graph.index(checksum_verifier),
                graph.index("gh attestation verify"),
            )
            self.assertLess(
                graph.index("gh attestation verify"),
                graph.index(extraction),
            )
        self.assertIn("GRAPH_ATTESTATION_BUNDLE_PATH", shell_graph)
        self.assertIn("GRAPH_ATTESTATION_BUNDLE_SHA256", shell_graph)
        self.assertIn("$GraphAttestationBundlePath", powershell_graph)
        self.assertIn("$GraphAttestationBundleSha256", powershell_graph)

    def test_installers_verify_release_wheel_offline_before_install(
        self,
    ):
        shell = (ROOT / "install.sh").read_text(encoding="utf-8")
        powershell = (ROOT / "install.ps1").read_text(encoding="utf-8")

        for installer in (shell, powershell):
            for required in (
                "github-release",
                "source_revision",
                "attestation",
                "bundle",
                "checksums",
                "signer_workflow",
                "source_ref",
                "gh auth status",
                "gh api",
                "/git/ref/tags/",
                "/git/tags/",
                "gh attestation verify",
                "--bundle",
                "--signer-workflow",
                "--source-digest",
                "--source-ref",
                "--deny-self-hosted-runners",
                "verify_code_search_wheel.py",
                "--force-reinstall",
                "tag source revision mismatch",
            ):
                self.assertIn(required, installer)

            self.assertIn("checksum manifest", installer)
            self.assertNotIn("gh release verify-asset", installer)
            self.assertNotIn("release membership", installer)
            self.assertLess(
                installer.index("gh api"),
                installer.index("gh release download"),
            )
            self.assertLess(
                installer.index("gh release download"),
                installer.index("gh attestation verify"),
            )
            self.assertLess(
                installer.index("gh attestation verify"),
                installer.index("--force-reinstall"),
            )
            self.assertLess(
                installer.index("--force-reinstall"),
                installer.index("verify_code_search_wheel.py"),
            )

        shell_release = shell.split("github-release)", 1)[1].split(";;", 1)[0]
        powershell_release = powershell.split('"github-release" {', 1)[1].split(
            "\n    default {",
            1,
        )[0]
        self.assertIn("CODE_SEARCH_CHECKSUMS", shell_release)
        self.assertIn("verify_checksum_manifest", shell_release)
        self.assertLess(
            shell_release.index("gh release download"),
            shell_release.index("verify_checksum_manifest"),
        )
        self.assertLess(
            shell_release.index("verify_checksum_manifest"),
            shell_release.index("gh attestation verify"),
        )
        self.assertIn("CodeSearchChecksums", powershell_release)
        self.assertIn("Assert-ChecksumManifest", powershell_release)
        self.assertLess(
            powershell_release.index("gh release download"),
            powershell_release.index("Assert-ChecksumManifest"),
        )
        self.assertLess(
            powershell_release.index("Assert-ChecksumManifest"),
            powershell_release.index("gh attestation verify"),
        )

    def test_release_wheel_promotion_and_token_scope_are_documented(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        compatibility = (
            ROOT / "compatibility" / "README.md"
        ).read_text(encoding="utf-8")
        combined = f"{readme}\n{compatibility}"
        normalized_readme = " ".join(readme.split())

        for required in (
            "GitHub Release wheel",
            "attestation bundle",
            "gh attestation verify",
            "--deny-self-hosted-runners",
            "refs/heads/main",
            "PEP 610",
            "Attestations: read",
            "production BOM",
            "tag resolves to the pinned source commit",
        ):
            self.assertIn(required, combined)
        self.assertNotIn("gh release verify-asset", combined)
        self.assertNotIn("release membership", combined)
        self.assertNotIn("gh attestation download", combined)
        self.assertIn("operator-fetched", combined)
        self.assertIn("vendored", combined)
        self.assertIn("production BOM pins code-search release", readme)
        self.assertIn(
            "code-search/releases/tag/v0.3.5",
            readme,
        )
        self.assertIn("`install.kind: github-release`", readme)
        self.assertIn("Contents: read", readme)
        self.assertIn("does not need `Attestations: read`", readme)
        self.assertIn(
            "authenticated GitHub fetch/tag-resolution commands",
            normalized_readme,
        )
        prerequisites = readme.split("## Prerequisites", 1)[1].split(
            "### Manual install",
            1,
        )[0]
        normalized_prerequisites = " ".join(prerequisites.split())
        self.assertIn(
            "both private component repositories",
            normalized_prerequisites,
        )
        self.assertNotIn("curl", prerequisites)
        self.assertIn("`tar`", prerequisites)
        self.assertNotIn(
            "only to authenticated `gh` clone/download commands",
            normalized_readme,
        )

    def test_installers_allowlist_post_download_child_environments(self):
        powershell = (ROOT / "install.ps1").read_text(encoding="utf-8")
        function_match = re.search(
            r"(?ms)^function Invoke-WithAllowedEnvironment \{.*?^\}",
            powershell,
        )
        self.assertIsNotNone(function_match)
        release_branch = powershell.split('"github-release" {', 1)[1].split(
            "\n    default {",
            1,
        )[0]
        self.assertGreaterEqual(
            release_branch.count("Invoke-WithAllowedEnvironment"),
            2,
        )
        self.assertRegex(
            release_branch,
            re.compile(
                r"Invoke-WithAllowedEnvironment\s*\{\s*"
                r"& gh attestation verify",
            ),
        )
        self.assertRegex(
            release_branch,
            re.compile(
                r"Invoke-WithAllowedEnvironment\s*\{"
                r".*?& \$VenvPip install"
                r".*?verify_code_search_wheel\.py",
                re.DOTALL,
            ),
        )
        self.assertRegex(
            powershell,
            re.compile(
                r"Invoke-WithAllowedEnvironment\s*\{\s*"
                r"& \$VenvPython .*?validate_installed\.py",
            ),
        )
        shell = (ROOT / "install.sh").read_text(encoding="utf-8")
        shell_function_match = re.search(
            r"(?ms)^run_with_allowed_environment\(\) \{.*?^\}",
            shell,
        )
        self.assertIsNotNone(shell_function_match)
        final_validation = shell.split(
            "[5/5] Validating installed MCP tool contracts...",
            1,
        )[1]
        self.assertIn("run_with_allowed_environment", final_validation)
        graph_extraction = shell.split(
            'if [ "$EXT" = "tar.gz" ]; then',
            1,
        )[1].split("fi", 1)[0]
        self.assertRegex(
            graph_extraction,
            re.compile(
                r"run_with_allowed_environment\s+"
                r"tar xzf",
            ),
        )
        self.assertRegex(
            graph_extraction,
            re.compile(
                r"run_with_allowed_environment\s+"
                r"unzip -qo",
            ),
        )
        self.assertIn("env -i", shell)
        self.assertIn(
            'allowed_environment+=("HOME=$INSTALL_RUNTIME_HOME")',
            shell_function_match.group(),
        )
        self.assertIn(
            '$env:USERPROFILE = $script:InstallRuntimeHome',
            function_match.group(),
        )
        for secret_name in (
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "CODE_INTEL_COMPONENT_TOKEN",
            "AWS_SECRET_ACCESS_KEY",
            "OPENAI_API_KEY",
            "UNRELATED_CREDENTIAL",
            "SSH_AUTH_SOCK",
        ):
            self.assertNotIn(secret_name, function_match.group())
        self.assertIn('"PATH"', function_match.group())

        with tempfile.TemporaryDirectory() as tmp:
            runtime_home = Path(tmp) / "isolated-home"
            runtime_home.mkdir()
            probe = (
                shell_function_match.group()
                + "\n"
                + r'''
INSTALL_RUNTIME_HOME="$1"
run_with_allowed_environment sh -c '
    test "$HOME" = "$1"
    test -d "$HOME"
    test -z "${GH_TOKEN+x}"
' sh "$INSTALL_RUNTIME_HOME"
'''
            )
            environment = dict(os.environ)
            environment["HOME"] = "/ambient-home-must-not-leak"
            environment["GH_TOKEN"] = "ambient-token-must-not-leak"
            completed = subprocess.run(
                ["bash", "-c", probe, "probe", str(runtime_home)],
                text=True,
                capture_output=True,
                check=False,
                env=environment,
            )

        self.assertEqual(
            completed.returncode,
            0,
            completed.stdout + completed.stderr,
        )

        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("pwsh is unavailable")
        probe = (
            function_match.group()
            + "\n"
            + r"""
$script:InstallRuntimeHome = Join-Path $PSScriptRoot "isolated-home"
New-Item -ItemType Directory -Path $script:InstallRuntimeHome | Out-Null
[Environment]::SetEnvironmentVariable(
    "USERPROFILE",
    "ambient-home-must-not-leak",
    "Process"
)
$Names = @(
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "CODE_INTEL_COMPONENT_TOKEN",
    "AWS_SECRET_ACCESS_KEY",
    "OPENAI_API_KEY",
    "UNRELATED_CREDENTIAL",
    "SSH_AUTH_SOCK"
)
foreach ($Name in $Names) {
    [Environment]::SetEnvironmentVariable($Name, "sentinel-$Name", "Process")
}
Invoke-WithAllowedEnvironment {
    if ($env:USERPROFILE -ne $script:InstallRuntimeHome) {
        throw "isolated runtime home was not applied"
    }
    foreach ($Name in $Names) {
        if ($null -ne [Environment]::GetEnvironmentVariable($Name, "Process")) {
            throw "$Name leaked into secret-free operation"
        }
    }
}
foreach ($Name in $Names) {
    $Expected = "sentinel-$Name"
    $Actual = [Environment]::GetEnvironmentVariable($Name, "Process")
    if ($Actual -ne $Expected) {
        throw "$Name was not restored after success"
    }
}
try {
    Invoke-WithAllowedEnvironment {
        if ($env:USERPROFILE -ne $script:InstallRuntimeHome) {
            throw "isolated runtime home was not applied"
        }
        foreach ($Name in $Names) {
            if ($null -ne [Environment]::GetEnvironmentVariable($Name, "Process")) {
                throw "$Name leaked into failing secret-free operation"
            }
        }
        throw "expected-operation-failure"
    }
} catch {
    if ($_ -notlike "*expected-operation-failure*") {
        throw
    }
}
foreach ($Name in $Names) {
    $Expected = "sentinel-$Name"
    $Actual = [Environment]::GetEnvironmentVariable($Name, "Process")
    if ($Actual -ne $Expected) {
        throw "$Name was not restored after failure"
    }
}
if ($env:USERPROFILE -ne "ambient-home-must-not-leak") {
    throw "ambient home was not restored"
}
Write-Output "token-isolation-ok"
"""
        )
        with tempfile.TemporaryDirectory() as tmp:
            probe_path = Path(tmp) / "probe.ps1"
            probe_path.write_text(probe, encoding="utf-8")
            completed = subprocess.run(
                [pwsh, "-NoProfile", "-File", str(probe_path)],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(
            completed.returncode,
            0,
            completed.stdout + completed.stderr,
        )
        self.assertIn("token-isolation-ok", completed.stdout)

    def test_installers_preserve_previous_install_until_staging_validates(self):
        shell = (ROOT / "install.sh").read_text(encoding="utf-8")
        powershell = (ROOT / "install.ps1").read_text(encoding="utf-8")

        for installer in (shell, powershell):
            self.assertIn(".install-staging", installer)
            self.assertIn(".rollback", installer)
            self.assertIn("Restoring previous installation", installer)
            self.assertIn("Promoting validated installation", installer)
            self.assertLess(
                installer.index(
                    "[5/5] Validating installed MCP tool contracts..."
                ),
                installer.index("Promoting validated installation"),
            )

        self.assertIn("trap rollback_install EXIT", shell)
        self.assertIn("finally", powershell)

    def test_powershell_tag_resolution_peels_annotated_tags(self):
        powershell = (ROOT / "install.ps1").read_text(encoding="utf-8")
        api_function = re.search(
            r"(?ms)^function Invoke-GitHubApiJson \{.*?^\}",
            powershell,
        )
        resolver_function = re.search(
            r"(?ms)^function Resolve-ReleaseTagCommit \{.*?^\}",
            powershell,
        )
        self.assertIsNotNone(api_function)
        self.assertIsNotNone(resolver_function)

        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("pwsh is unavailable")
        tag_object = "b" * 40
        nested_tag_object = "c" * 40
        commit = "a" * 40
        probe = (
            api_function.group()
            + "\n"
            + resolver_function.group()
            + "\n"
            + f"""
$ErrorActionPreference = "Stop"
$script:Responses = [System.Collections.Generic.Queue[object]]::new()
$script:Responses.Enqueue([string[]]@(
    '{{"object": {{',
    '  "type": "tag",',
    '  "sha": "{tag_object}"',
    '}}}}'
))
$script:Responses.Enqueue([string[]]@(
    '{{"object": {{',
    '  "type": "tag",',
    '  "sha": "{nested_tag_object}"',
    '}}}}'
))
$script:Responses.Enqueue([string[]]@(
    '{{"object": {{',
    '  "type": "commit",',
    '  "sha": "{commit}"',
    '}}}}'
))
$script:Calls = [System.Collections.Generic.List[string]]::new()
function gh {{
    $script:Calls.Add(($args -join " "))
    $global:LASTEXITCODE = 0
    foreach ($Line in $script:Responses.Dequeue()) {{
        Write-Output $Line
    }}
}}
$Resolved = Resolve-ReleaseTagCommit `
    -Repository "redacted-org/code-search" `
    -Tag "v0.2.0"
if ($Resolved -ne "{commit}") {{
    throw "wrong resolved commit: $Resolved"
}}
if ($script:Calls.Count -ne 3) {{
    throw "wrong API call count: $($script:Calls.Count)"
}}
if ($script:Calls[0] -notlike "*git/ref/tags/v0.2.0*") {{
    throw "lightweight tag ref endpoint was not queried"
}}
if ($script:Calls[1] -notlike "*git/tags/{tag_object}*") {{
    throw "first annotated tag was not peeled"
}}
if ($script:Calls[2] -notlike "*git/tags/{nested_tag_object}*") {{
    throw "nested annotated tag was not peeled"
}}
Write-Output "tag-resolution-ok"
"""
        )
        with tempfile.TemporaryDirectory() as tmp:
            probe_path = Path(tmp) / "tag-probe.ps1"
            probe_path.write_text(probe, encoding="utf-8")
            completed = subprocess.run(
                [pwsh, "-NoProfile", "-File", str(probe_path)],
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(
            completed.returncode,
            0,
            completed.stdout + completed.stderr,
        )
        self.assertIn("tag-resolution-ok", completed.stdout)


if __name__ == "__main__":
    unittest.main()
