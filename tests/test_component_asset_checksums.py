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


if __name__ == "__main__":
    unittest.main()
