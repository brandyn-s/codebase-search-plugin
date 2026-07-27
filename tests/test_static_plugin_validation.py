"""Acceptance tests for repository-local plugin contract validation."""

from pathlib import Path
import json
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class StaticPluginValidationTests(unittest.TestCase):
    def _copy_checkout(self, checkout: Path) -> None:
        for directory in (
            ".claude-plugin",
            "compatibility",
            "scripts",
            "skills",
        ):
            shutil.copytree(ROOT / directory, checkout / directory)
        for filename in (
            ".mcp.json",
            "component-bom.json",
            "install.sh",
            "install.ps1",
        ):
            shutil.copy2(ROOT / filename, checkout / filename)

    def test_validator_rejects_skill_tool_outside_tested_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            self._copy_checkout(checkout)

            skill = checkout / "skills" / "code-explore" / "SKILL.md"
            skill.write_text(
                skill.read_text(encoding="utf-8")
                + "\n`mcp__code-graph__not_in_tested_release`\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [sys.executable, "scripts/validate_plugin.py"],
                cwd=checkout,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("not_in_tested_release", completed.stdout)

    def test_validator_rejects_ready_bom_when_graph_cannot_suppress_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            self._copy_checkout(checkout)
            bom_path = checkout / "component-bom.json"
            bom = json.loads(bom_path.read_text(encoding="utf-8"))
            bom["integrated_readiness"]["status"] = "ready"
            bom_path.write_text(json.dumps(bom), encoding="utf-8")

            completed = subprocess.run(
                [sys.executable, "scripts/validate_plugin.py"],
                cwd=checkout,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("skip_report", completed.stdout)
        self.assertIn("blocked", completed.stdout)

    def test_validator_rejects_missing_pinned_asset_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            self._copy_checkout(checkout)
            bom_path = checkout / "component-bom.json"
            bom = json.loads(bom_path.read_text(encoding="utf-8"))
            del bom["components"]["code-graph"]["install"]["assets"]["linux-amd64"][
                "sha256"
            ]
            bom_path.write_text(json.dumps(bom), encoding="utf-8")

            completed = subprocess.run(
                [sys.executable, "scripts/validate_plugin.py"],
                cwd=checkout,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("linux-amd64", completed.stdout)
        self.assertIn("sha256", completed.stdout.lower())


if __name__ == "__main__":
    unittest.main()
