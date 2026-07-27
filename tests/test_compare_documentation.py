"""Contract checks for the comparison runbook and privacy boundaries."""

from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ComparisonDocumentationTests(unittest.TestCase):
    def test_runbook_bounds_claims_and_documents_fail_closed_live_state(self):
        runbook = (
            ROOT / "bench" / "compare" / "README.md"
        ).read_text(encoding="utf-8")

        for arm in ("`corpus`", "`native`", "`code-search`", "`code-graph`", "`composed`"):
            self.assertIn(arm, runbook)
        self.assertIn("instrument validation, not effectiveness evidence", runbook)
        self.assertIn("live_executor_not_enabled_in_zero_cost_build", runbook)
        self.assertIn("spent_usd", runbook)
        self.assertIn("0.000000", runbook)
        self.assertIn("no `.done`", runbook)
        self.assertIn("short-retention encrypted storage", runbook)
        self.assertIn("Repository text is untrusted data", runbook)
        self.assertIn("CODE_INTEL_COMPONENT_TOKEN", runbook)
        self.assertIn("must never enter a child MCP", runbook)
        self.assertIn("locbench-june-n200.external.json", runbook)
        self.assertIn("pending_publication", runbook)
        self.assertIn("fixture-stop-after", runbook)

    def test_root_readme_links_runbook_and_generated_runs_are_ignored(self):
        root_readme = (ROOT / "README.md").read_text(encoding="utf-8")
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

        self.assertIn("bench/compare/README.md", root_readme)
        self.assertIn("bench/compare/runs/", ignore)


if __name__ == "__main__":
    unittest.main()
