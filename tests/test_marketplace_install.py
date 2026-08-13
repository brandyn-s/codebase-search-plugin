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

        self.assertEqual(plugin["version"], "0.4.30")
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

        quick_start = readme.split("## Quick Start", 1)[1].split(
            "This runs both semantic", 1
        )[0]
        plugin_install = (
            "claude plugin install "
            "codebase-search@redacted-code-intelligence --scope user"
        )
        installed_path = 'next(x["installPath"]'
        native_install = 'bash "$PLUGIN_DIR/install.sh"'
        self.assertLess(quick_start.index(plugin_install), quick_start.index(installed_path))
        self.assertLess(quick_start.index(installed_path), quick_start.index(native_install))
        self.assertNotIn('next(x["installLocation"]', quick_start)

    def test_upgrade_installs_native_components_inside_exact_plugin_cache(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        upgrade = readme.split("## Upgrade", 1)[1].split(
            "## Current measured state", 1
        )[0]

        marketplace_update = (
            "claude plugin marketplace update redacted-code-intelligence"
        )
        plugin_update = (
            "claude plugin update "
            "codebase-search@redacted-code-intelligence --scope user"
        )
        installed_path = 'next(x["installPath"]'
        native_install = 'bash "$PLUGIN_DIR/install.sh"'
        for command in (
            marketplace_update,
            plugin_update,
            installed_path,
            native_install,
        ):
            self.assertIn(command, upgrade)
        self.assertLess(upgrade.index(marketplace_update), upgrade.index(plugin_update))
        self.assertLess(upgrade.index(plugin_update), upgrade.index(installed_path))
        self.assertLess(upgrade.index(installed_path), upgrade.index(native_install))
        self.assertNotIn('next(x["installLocation"]', upgrade)
        self.assertIn("self-contained", upgrade)
        self.assertIn("Restart Claude Code", upgrade)


if __name__ == "__main__":
    unittest.main()
