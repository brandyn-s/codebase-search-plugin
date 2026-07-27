"""Supply-chain contracts for pinned code-graph release assets."""

import importlib.util
import json
from pathlib import Path
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "validate_real_installed.py"

EXPECTED_ASSETS = {
    "darwin-amd64": {
        "name": "codebase-memory-mcp-darwin-amd64.tar.gz",
        "sha256": "3891facf8e8a7c7d5345a09a5938e9b14af2fae4c53c93d5a3960c9f2bf8ca8c",
    },
    "darwin-arm64": {
        "name": "codebase-memory-mcp-darwin-arm64.tar.gz",
        "sha256": "77fa65569309c42b82bc24f6638921d01612b4509e6763dd26be6c5d0c0e835b",
    },
    "linux-amd64": {
        "name": "codebase-memory-mcp-linux-amd64.tar.gz",
        "sha256": "45c23c40c569b9c406af2a74b6ad6516c7abdfca4d9f6fab87dbfce68a446cb5",
    },
    "linux-arm64": {
        "name": "codebase-memory-mcp-linux-arm64.tar.gz",
        "sha256": "8393854ff5ae3d48e7c7110659ca42396ebeb8024f7b375446180b52da304fcf",
    },
    "windows-amd64": {
        "name": "codebase-memory-mcp-windows-amd64.zip",
        "sha256": "d58464c4622a49c2a04ccef3e36fde7c493e1f522d052ff6a9c2b7bc900c8cfe",
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


if __name__ == "__main__":
    unittest.main()
