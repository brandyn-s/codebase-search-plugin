"""Acceptance tests for repository-local plugin contract validation."""

from pathlib import Path
import hashlib
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

    def _run_validator(self, checkout: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "scripts/validate_plugin.py"],
            cwd=checkout,
            text=True,
            capture_output=True,
            check=False,
        )

    def _rewrite_tool_schema(
        self,
        checkout: Path,
        component: str,
        tool_name: str,
        schema: dict,
    ) -> dict:
        snapshot_path = (
            checkout / "compatibility" / f"{component}-tools.json"
        )
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        tool = snapshot["tools"][tool_name]
        tool["input_schema"] = schema
        canonical = json.dumps(
            schema,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        tool["input_schema_sha256"] = hashlib.sha256(canonical).hexdigest()
        snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
        return snapshot

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

    def test_validator_rejects_non_string_search_project_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            self._copy_checkout(checkout)
            snapshot_path = (
                checkout / "compatibility" / "code-search-tools.json"
            )
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            schema = snapshot["tools"]["get_index_status"]["input_schema"]
            schema["properties"]["project_path"] = {"type": "integer"}
            self._rewrite_tool_schema(
                checkout, "code-search", "get_index_status", schema
            )

            completed = self._run_validator(checkout)

        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("project_path", completed.stdout)
        self.assertIn("optional string", completed.stdout)

    def test_validator_rejects_project_path_on_a_nonobject_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            self._copy_checkout(checkout)
            snapshot_path = (
                checkout / "compatibility" / "code-search-tools.json"
            )
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            schema = snapshot["tools"]["get_index_status"]["input_schema"]
            schema["type"] = "string"
            schema["properties"]["project_path"] = {"type": "string"}
            self._rewrite_tool_schema(
                checkout, "code-search", "get_index_status", schema
            )

            completed = self._run_validator(checkout)

        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("project_path", completed.stdout)
        self.assertIn("optional string", completed.stdout)

    def test_validator_rejects_malformed_or_required_graph_skip_report(self):
        mutations = {
            "wrong-type": lambda schema: schema["properties"].update(
                {"skip_report": {"type": "string"}}
            ),
            "required": lambda schema: (
                schema["properties"].update({"skip_report": {"type": "boolean"}}),
                schema.setdefault("required", []).append("skip_report"),
            ),
            "non-object": lambda schema: (
                schema.update({"type": "string"}),
                schema["properties"].update(
                    {"skip_report": {"type": "boolean"}}
                ),
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                checkout = Path(tmp)
                self._copy_checkout(checkout)
                snapshot_path = (
                    checkout / "compatibility" / "code-graph-tools.json"
                )
                snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
                schema = snapshot["tools"]["index_repository"]["input_schema"]
                mutate(schema)
                snapshot = self._rewrite_tool_schema(
                    checkout, "code-graph", "index_repository", schema
                )
                snapshot["tested_capabilities"]["inputs"][
                    "index_repository.skip_report"
                ] = True
                snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
                bom_path = checkout / "component-bom.json"
                bom = json.loads(bom_path.read_text(encoding="utf-8"))
                bom["components"]["code-graph"]["tested_capabilities"] = snapshot[
                    "tested_capabilities"
                ]
                bom_path.write_text(json.dumps(bom), encoding="utf-8")

                completed = self._run_validator(checkout)

            self.assertEqual(
                completed.returncode, 1, completed.stdout + completed.stderr
            )
            self.assertIn("skip_report", completed.stdout)
            self.assertIn("optional boolean", completed.stdout)

    def test_ready_contract_requires_optional_search_project_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            self._copy_checkout(checkout)
            bom_path = checkout / "component-bom.json"
            bom = json.loads(bom_path.read_text(encoding="utf-8"))
            bom["integrated_readiness"]["status"] = "ready"
            bom_path.write_text(json.dumps(bom), encoding="utf-8")

            completed = self._run_validator(checkout)

        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("project_path", completed.stdout)


if __name__ == "__main__":
    unittest.main()
