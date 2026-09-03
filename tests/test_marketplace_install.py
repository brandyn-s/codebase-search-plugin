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

        self.assertEqual(plugin["version"], "0.5.0")
        self.assertEqual(marketplace["name"], "code-intelligence")
        self.assertEqual(len(marketplace["plugins"]), 1)
        entry = marketplace["plugins"][0]
        self.assertEqual(entry["name"], plugin["name"])
        self.assertEqual(entry["version"], plugin["version"])
        self.assertEqual(entry["source"], "./")

    def test_quick_start_uses_supported_namespaced_install_commands(self):
        readme = (ROOT / "README.md").read_text()
        shell_installer = (ROOT / "install.sh").read_text()
        powershell_installer = (ROOT / "install.ps1").read_text()

        marketplace_add = (
            "claude plugin marketplace add brandyn-s/codebase-search-plugin"
        )
        plugin_install = (
            "claude plugin install codebase-search@code-intelligence --scope user"
        )
        self.assertIn(marketplace_add, readme)
        self.assertIn(plugin_install, readme)
        self.assertNotIn('claude plugin marketplace add "$PWD"', readme)
        self.assertNotIn("/install-plugin", readme)
        self.assertNotIn("/install-plugin", shell_installer)
        self.assertNotIn("/install-plugin", powershell_installer)

        install = readme.split("## Install", 1)[1].split("## Use", 1)[0]
        self.assertLess(install.index(marketplace_add), install.index(plugin_install))
        # The launchers bootstrap the components; the README must not ask
        # users to locate the plugin cache or run the installer by hand.
        self.assertNotIn('next(x["installPath"]', readme)
        self.assertNotIn('next(x["installLocation"]', readme)
        self.assertNotIn('bash "$PLUGIN_DIR/install.sh"', readme)
        self.assertIn("install themselves", install)
        self.assertIn(".runtime/bootstrap.log", install)
        for installer in (shell_installer, powershell_installer):
            self.assertIn(marketplace_add, installer)
            self.assertIn(plugin_install, installer)

    def test_upgrade_refreshes_native_components_inside_exact_plugin_cache(self):
        install_doc = (ROOT / "docs" / "INSTALL.md").read_text(encoding="utf-8")
        upgrade = install_doc.split("## Upgrade", 1)[1].split(
            "## Readiness record", 1
        )[0]

        marketplace_update = "claude plugin marketplace update code-intelligence"
        plugin_update = (
            "claude plugin update codebase-search@code-intelligence --scope user"
        )
        for command in (marketplace_update, plugin_update):
            self.assertIn(command, upgrade)
        self.assertLess(upgrade.index(marketplace_update), upgrade.index(plugin_update))
        self.assertIn(".runtime/", upgrade)
        self.assertIn("install.sh", upgrade)
        self.assertNotIn('next(x["installLocation"]', upgrade)
        self.assertIn("self-contained", upgrade)
        self.assertIn("Restart Claude Code", upgrade)


if __name__ == "__main__":
    unittest.main()
