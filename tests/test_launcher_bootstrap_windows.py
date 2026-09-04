"""The committed bin/*.cmd launchers install the pinned components on first launch.

Windows counterpart of test_launcher_bootstrap.py. Skipped off Windows; the
validate workflow runs it on windows-latest.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]

# Stand-in for install.ps1: records the invocation, then lays out the runtime
# the way the real installer promotes it. cmd.exe doubles as the fake server
# executable so "component /c echo ..." echoes its arguments.
FAKE_INSTALLER = r"""
$ErrorActionPreference = "Stop"
$PluginDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Add-Content -Path (Join-Path $PluginDir "installer.calls") -Value "installer-run"
Write-Output "installer stdout must not reach the MCP channel"
if ($env:FAKE_INSTALL_DELAY) { Start-Sleep -Seconds ([int]$env:FAKE_INSTALL_DELAY) }
New-Item -ItemType Directory -Force -Path (Join-Path $PluginDir ".venv\Scripts") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $PluginDir ".runtime\bin") | Out-Null
$cmd = Join-Path $env:SystemRoot "System32\cmd.exe"
Copy-Item $cmd (Join-Path $PluginDir ".venv\Scripts\code-search-mcp.exe")
Copy-Item $cmd (Join-Path $PluginDir ".runtime\bin\code-graph.exe")
"""

FAILING_INSTALLER = 'Write-Output "boom"\nexit 7\n'


@unittest.skipUnless(sys.platform == "win32", "Windows launcher shims")
class WindowsLauncherBootstrapTests(unittest.TestCase):
    def _plugin_copy(self, tmp: str, *, installer: str = FAKE_INSTALLER) -> Path:
        plugin = Path(tmp) / "plugin"
        plugin.mkdir()
        shutil.copytree(ROOT / "bin", plugin / "bin")
        (plugin / "install.ps1").write_text(installer, encoding="utf-8")
        return plugin

    def _run(self, plugin: Path, launcher: str, *args: str, env=None):
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "SystemRoot": os.environ.get("SystemRoot", r"C:\Windows"),
            "USERPROFILE": str(plugin.parent),
            "TEMP": str(plugin.parent),
            "TMP": str(plugin.parent),
            "CODE_INTEL_BOOTSTRAP_WAIT_SECONDS": "60",
        }
        if env:
            environment.update(env)
        return subprocess.run(
            ["cmd.exe", "/c", str(plugin / "bin" / launcher), *args],
            capture_output=True,
            text=True,
            env=environment,
            timeout=180,
            check=False,
        )

    def test_shims_are_committed_and_named_after_the_bash_launchers(self):
        mcp = json.loads((ROOT / ".mcp.json").read_text(encoding="utf-8"))
        for name, entry in mcp["mcpServers"].items():
            launcher = Path(entry["command"]).name
            self.assertTrue((ROOT / "bin" / f"{launcher}.cmd").is_file(), launcher)
        self.assertTrue((ROOT / "bin" / "_bootstrap.cmd").is_file())
        attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
        self.assertIn("*.cmd text eol=crlf", attributes)

    def test_first_launch_installs_once_then_runs_component(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = self._plugin_copy(tmp)
            first = self._run(plugin, "code-graph.cmd", "/c", "echo", "graph-ran")
            self.assertEqual(first.returncode, 0, first.stderr)
            self.assertEqual(first.stdout.strip(), "graph-ran")
            self.assertNotIn("installer stdout", first.stdout)
            self.assertIn("running install.ps1", first.stderr)
            calls = (plugin / "installer.calls").read_text(encoding="utf-8")
            self.assertEqual(calls.count("installer-run"), 1)
            log = (plugin / ".runtime" / "bootstrap.log").read_text(encoding="utf-8", errors="replace")
            self.assertIn("installer stdout must not reach the MCP channel", log)
            self.assertFalse((plugin / ".runtime" / "bootstrap.lock").exists())

            second = self._run(plugin, "run-code-search.cmd", "/c", "echo", "search-ran")
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertEqual(second.stdout.strip(), "search-ran")
            self.assertNotIn("install.ps1", second.stderr)
            calls = (plugin / "installer.calls").read_text(encoding="utf-8")
            self.assertEqual(calls.count("installer-run"), 1)

    def test_bootstrap_can_be_disabled_and_reports_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = self._plugin_copy(tmp)
            disabled = self._run(
                plugin, "code-graph.cmd", env={"CODE_INTEL_NO_BOOTSTRAP": "1"}
            )
            self.assertNotEqual(disabled.returncode, 0)
            self.assertEqual(disabled.stdout, "")
            self.assertIn("install.ps1", disabled.stderr)
            self.assertFalse((plugin / "installer.calls").exists())

        with tempfile.TemporaryDirectory() as tmp:
            plugin = self._plugin_copy(tmp, installer=FAILING_INSTALLER)
            failed = self._run(plugin, "run-code-search.cmd")
            self.assertNotEqual(failed.returncode, 0)
            self.assertEqual(failed.stdout, "")
            self.assertIn("installation failed", failed.stderr)
            self.assertIn("boom", failed.stderr)
            self.assertFalse((plugin / ".runtime" / "bootstrap.lock").exists())

    def test_stale_lock_is_reclaimed_after_the_wait_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            plugin = self._plugin_copy(tmp)
            (plugin / ".runtime" / "bootstrap.lock").mkdir(parents=True)
            result = self._run(
                plugin,
                "code-graph.cmd",
                "/c",
                "echo",
                "after-stale-lock",
                env={"CODE_INTEL_BOOTSTRAP_WAIT_SECONDS": "4"},
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "after-stale-lock")
            self.assertIn("stale lock", result.stderr)


if __name__ == "__main__":
    unittest.main()
