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
        self.assertIn("GH_TOKEN", runbook)
        self.assertIn("must never enter a child MCP", runbook)
        self.assertIn("locbench-june-n200.external.json", runbook)
        self.assertIn("published at code-graph merge", runbook)
        self.assertIn("fixture-stop-after", runbook)

    def test_root_readme_links_runbook_and_generated_runs_are_ignored(self):
        root_readme = (ROOT / "README.md").read_text(encoding="utf-8")
        ignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

        self.assertIn("bench/compare/README.md", root_readme)
        self.assertIn("bench/compare/runs/", ignore)

    def test_runbook_separates_local_calibration_from_future_decision_evidence(self):
        runbook = (
            ROOT / "bench" / "compare" / "README.md"
        ).read_text(encoding="utf-8")
        normalized_runbook = " ".join(runbook.split())

        for phrase in (
            "retrospective calibration and regression only",
            "not final decision evidence",
            "possible training overlap",
            "license is unspecified",
            "does not grant redistribution rights",
            "parquet, pins, queries, patches, PR-response cache, and repository cache",
            "remain local and uncommitted",
            "python3 bench/compare/build_pin.py prepare-june",
            "--github-pr-cache /absolute/operator-only/github-pr-cache",
            "--repository-root /absolute/operator-only/repositories",
            "--output /absolute/operator-only/locbench-june-n200.prepared.json",
            "--quarantine-report /absolute/operator-only/locbench-june-n200.quarantine.json",
            "10 Bug, 10 Feature, 10 Performance, and 10 Security",
            "post-development public merged pull requests",
            "immutable base, head, and unique merge-base",
            "two independent reviewers",
            "oracle remains hidden during retrieval",
            "managed Claude.ai or keychain OAuth",
            "does not satisfy `--bare`",
            "`--max-budget-usd` is defense in depth only",
            "no production trusted signature verifier",
            "transactional broker",
            "provider hard limit",
            "reviewed real executor",
            "encrypted response store",
            "credentials and authority claims are never printed or forwarded",
        ):
            self.assertIn(phrase, normalized_runbook)


if __name__ == "__main__":
    unittest.main()
