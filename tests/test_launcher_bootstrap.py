"""The committed bin/ launchers install the pinned components on first launch."""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading
import unittest


ROOT = Path(__file__).resolve().parents[1]

FAKE_INSTALLER = """#!/usr/bin/env bash
# Stand-in for install.sh: records the invocation, then lays out the runtime
# the same way the real installer promotes it.
set -euo pipefail
PLUGIN_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "installer-run" >> "$PLUGIN_DIR/installer.calls"
echo "installer stdout must not reach the MCP channel"
sleep "${FAKE_INSTALL_DELAY:-0}"
mkdir -p "$PLUGIN_DIR/.venv/bin" "$PLUGIN_DIR/.runtime/bin"
printf '#!/usr/bin/env bash\\necho "code-search-mcp $*"\\n' > "$PLUGIN_DIR/.venv/bin/code-search-mcp"
printf '#!/usr/bin/env bash\\necho "code-graph $*"\\n' > "$PLUGIN_DIR/.runtime/bin/code-graph"
chmod +x "$PLUGIN_DIR/.venv/bin/code-search-mcp" "$PLUGIN_DIR/.runtime/bin/code-graph"
"""


class LauncherBootstrapTests(unittest.TestCase):
    def _plugin_copy(self, tmp: str, *, installer: str = FAKE_INSTALLER) -> Path:
        plugin = Path(tmp) / "plugin"
        plugin.mkdir()
        shutil.copytree(ROOT / "bin", plugin / "bin")
        (plugin / "install.sh").write_text(installer, encoding="utf-8")
        (plugin / "install.sh").chmod(0o755)
        return plugin

    def _run(self, plugin: Path, launcher: str, *args: str, env=None):
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": str(plugin.parent),
            "CODE_INTEL_BOOTSTRAP_WAIT_SECONDS": "60",
        }
        if env:
            environment.update(env)
        return subprocess.run(
            [str(plugin / "bin" / launcher), *args],
            capture_output=True,
            text=True,
            env=environment,
            timeout=120,
            check=False,
        )

    def test_committed_launchers_match_mcp_json_and_are_executable(self):
        import json

        mcp = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
        commands = {
            name: entry["command"] for name, entry in mcp["mcpServers"].items()
        }
        self.assertEqual(
            commands,
            {
                "code-search": "${CLAUDE_PLUGIN_ROOT}/bin/run-code-search",
                "code-graph": "${CLAUDE_PLUGIN_ROOT}/bin/code-graph",
            },
        )
        for launcher in ("run-code-search", "code-graph"):
            path = ROOT / "bin" / launcher
            self.assertTrue(path.is_file(), path)
            self.assertTrue(os.access(path, os.X_OK), f"{path} is not executable")
        self.assertTrue((ROOT / "bin" / "_bootstrap.sh").is_file())
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
        self.assertIn(".runtime/", ignore)
        self.assertNotIn("\nbin/\n", ignore)

    def test_first_launch_installs_once_then_execs_component(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = self._plugin_copy(tmp)
            first = self._run(plugin, "code-graph", "--version")
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(first.stdout, "code-graph --version\n")
            self.assertNotIn("installer stdout", first.stdout)
            self.assertIn("running install.sh", first.stderr)
            self.assertEqual(
                (plugin / "installer.calls").read_text().count("installer-run"), 1
            )
            log = (plugin / ".runtime" / "bootstrap.log").read_text()
            self.assertIn("installer stdout must not reach the MCP channel", log)
            self.assertFalse((plugin / ".runtime" / "bootstrap.lock").exists())

            second = self._run(plugin, "run-code-search", "--help")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(second.stdout, "code-search-mcp --help\n")
            self.assertNotIn("install.sh", second.stderr)
            self.assertEqual(
                (plugin / "installer.calls").read_text().count("installer-run"), 1
            )

    def test_concurrent_launchers_share_one_bootstrap(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = self._plugin_copy(tmp)
            results = {}

            def launch(name: str, launcher: str) -> None:
                results[name] = self._run(
                    plugin, launcher, env={"FAKE_INSTALL_DELAY": "3"}
                )

            threads = [
                threading.Thread(target=launch, args=("graph", "code-graph")),
                threading.Thread(target=launch, args=("search", "run-code-search")),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

            self.assertEqual(results["graph"].returncode, 0, results["graph"].stderr)
            self.assertEqual(results["search"].returncode, 0, results["search"].stderr)
            self.assertEqual(results["graph"].stdout, "code-graph \n")
            self.assertEqual(results["search"].stdout, "code-search-mcp \n")
            self.assertEqual(
                (plugin / "installer.calls").read_text().count("installer-run"), 1
            )

    def test_bootstrap_can_be_disabled_and_reports_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = self._plugin_copy(tmp)
            disabled = self._run(
                plugin, "code-graph", env={"CODE_INTEL_NO_BOOTSTRAP": "1"}
            )
            self.assertNotEqual(disabled.returncode, 0)
            self.assertEqual(disabled.stdout, "")
            self.assertIn("install.sh", disabled.stderr)
            self.assertFalse((plugin / "installer.calls").exists())

        with tempfile.TemporaryDirectory() as tmp:
            plugin = self._plugin_copy(
                tmp,
                installer="#!/usr/bin/env bash\necho boom\nexit 7\n",
            )
            failed = self._run(plugin, "run-code-search")
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(failed.stdout, "")
            self.assertIn("installation failed", failed.stderr)
            self.assertFalse((plugin / ".runtime" / "bootstrap.lock").exists())

    def test_stale_lock_from_dead_bootstrap_is_recovered(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = self._plugin_copy(tmp)
            lock = plugin / ".runtime" / "bootstrap.lock"
            lock.mkdir(parents=True)
            (lock / "pid").write_text("999999999\n", encoding="utf-8")
            result = self._run(plugin, "code-graph", "status")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "code-graph status\n")


if __name__ == "__main__":
    unittest.main()
