"""Supply-chain contracts for pinned code-graph release assets."""

import importlib.util
import json
from pathlib import Path
import re
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "validate_real_installed.py"

EXPECTED_ASSETS = {
    "darwin-amd64": {
        "name": "codebase-memory-mcp-darwin-amd64.tar.gz",
        "sha256": "5f7588f847a22a8602ccce355f7279c0bdf50808d184a5b6c082bf1b42a2e341",
    },
    "darwin-arm64": {
        "name": "codebase-memory-mcp-darwin-arm64.tar.gz",
        "sha256": "8ad13c146c54a12ea14de1fd82a4cfd1224af149c086cea27a7ab6ca88ffc23b",
    },
    "linux-amd64": {
        "name": "codebase-memory-mcp-linux-amd64.tar.gz",
        "sha256": "d23da061459f65d8f4a1fae0d906245d1bbb9cc37998836944397b9f8a791df8",
    },
    "linux-arm64": {
        "name": "codebase-memory-mcp-linux-arm64.tar.gz",
        "sha256": "48dff8f5f602d84ea7359187211ea7d98b1c3d1102aec8220fc3d38c6174b649",
    },
    "windows-amd64": {
        "name": "codebase-memory-mcp-windows-amd64.zip",
        "sha256": "3a850b7c311d114aa93012702e838c8d3da7b75ac1afdfb980c4b33e3e18b129",
    },
}


def load_helper():
    spec = importlib.util.spec_from_file_location("validate_real_installed", HELPER)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load real-install helper")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ComponentAssetChecksumTests(unittest.TestCase):
    def test_bom_pins_every_supported_asset_name_and_sha256(self):
        bom = json.loads((ROOT / "component-bom.json").read_text(encoding="utf-8"))
        graph_install = bom["components"]["code-graph"]["install"]

        self.assertEqual(graph_install["assets"], EXPECTED_ASSETS)

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

    def test_installers_authenticate_when_possible_and_never_skip_verification(self):
        shell = (ROOT / "install.sh").read_text(encoding="utf-8")
        powershell = (ROOT / "install.ps1").read_text(encoding="utf-8")

        for installer in (shell, powershell):
            self.assertIn("GH_TOKEN", installer)
            self.assertIn("gh release download", installer)
            self.assertIn("assets", installer)
            self.assertIn("checksum mismatch", installer)
            self.assertNotIn("checksums.txt", installer)
            self.assertNotIn("skipping verification", installer)

        self.assertIn("sha256sum", shell)
        self.assertIn("shasum", shell)
        self.assertIn("public release URL fallback", shell)
        self.assertIn("Get-FileHash", powershell)
        self.assertIn("Invoke-WebRequest", powershell)
        self.assertIn("public release URL fallback", powershell)

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
        self.assertIn("current production BOM still pins", readme)
        self.assertIn("Contents: read", readme)
        self.assertIn("does not need `Attestations: read`", readme)
        self.assertIn(
            "authenticated GitHub fetch/tag-resolution commands",
            normalized_readme,
        )
        self.assertNotIn(
            "only to authenticated `gh` clone/download commands",
            normalized_readme,
        )

    def test_powershell_release_install_strips_and_restores_github_tokens(self):
        powershell = (ROOT / "install.ps1").read_text(encoding="utf-8")
        function_match = re.search(
            r"(?ms)^function Invoke-WithoutGitHubTokens \{.*?^\}",
            powershell,
        )
        self.assertIsNotNone(function_match)
        release_branch = powershell.split('"github-release" {', 1)[1].split(
            "\n    default {",
            1,
        )[0]
        self.assertGreaterEqual(
            release_branch.count("Invoke-WithoutGitHubTokens"),
            2,
        )
        self.assertRegex(
            release_branch,
            re.compile(
                r"Invoke-WithoutGitHubTokens\s*\{\s*"
                r"& gh attestation verify",
            ),
        )
        self.assertRegex(
            release_branch,
            re.compile(
                r"Invoke-WithoutGitHubTokens\s*\{"
                r".*?& \$VenvPip install"
                r".*?verify_code_search_wheel\.py",
                re.DOTALL,
            ),
        )
        self.assertRegex(
            powershell,
            re.compile(
                r"Invoke-WithoutGitHubTokens\s*\{\s*"
                r"& \$VenvPython .*?validate_installed\.py",
            ),
        )
        shell = (ROOT / "install.sh").read_text(encoding="utf-8")
        final_validation = shell.split(
            "[5/5] Validating installed MCP tool contracts...",
            1,
        )[1]
        self.assertIn(
            "env -u GH_TOKEN -u GITHUB_TOKEN -u CODE_INTEL_COMPONENT_TOKEN",
            final_validation,
        )
        for token_name in (
            "GH_TOKEN",
            "GITHUB_TOKEN",
            "CODE_INTEL_COMPONENT_TOKEN",
        ):
            self.assertIn(token_name, function_match.group())

        pwsh = shutil.which("pwsh")
        if pwsh is None:
            self.skipTest("pwsh is unavailable")
        probe = (
            function_match.group()
            + "\n"
            + r"""
$Names = @("GH_TOKEN", "GITHUB_TOKEN", "CODE_INTEL_COMPONENT_TOKEN")
foreach ($Name in $Names) {
    [Environment]::SetEnvironmentVariable($Name, "sentinel-$Name", "Process")
}
Invoke-WithoutGitHubTokens {
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
    Invoke-WithoutGitHubTokens {
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
