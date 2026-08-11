import json
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class MarketplaceInstallTests(unittest.TestCase):
    def test_local_marketplace_exposes_the_exact_plugin_version(self):
        marketplace = json.loads(
            (ROOT / ".claude-plugin" / "marketplace.json").read_text()
        )
        plugin = json.loads(
            (ROOT / ".claude-plugin" / "plugin.json").read_text()
        )

        self.assertEqual(plugin["version"], "0.4.9")
        self.assertEqual(marketplace["name"], "redacted-code-intelligence")
        self.assertEqual(len(marketplace["plugins"]), 1)
        entry = marketplace["plugins"][0]
        self.assertEqual(entry["name"], plugin["name"])
        self.assertEqual(entry["version"], plugin["version"])
        self.assertEqual(entry["source"], "./")

    def test_quick_start_uses_supported_namespaced_install_commands(self):
        readme = (ROOT / "README.md").read_text()
        shell_installer = (ROOT / "install.sh").read_text()
        powershell_installer = (ROOT / "install.ps1").read_text()

        self.assertIn(
            "claude plugin marketplace add "
            "redacted-org/codebase-search-plugin",
            readme,
        )
        self.assertIn(
            "claude plugin install codebase-search@redacted-code-intelligence",
            readme,
        )
        self.assertNotIn('claude plugin marketplace add "$PWD"', readme)
        self.assertNotIn("/install-plugin", readme)
        self.assertNotIn("/install-plugin", shell_installer)
        self.assertNotIn("/install-plugin", powershell_installer)


if __name__ == "__main__":
    unittest.main()
