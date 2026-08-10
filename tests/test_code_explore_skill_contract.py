"""Acceptance tests for the literal /code-explore preflight contract."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "code-explore" / "SKILL.md"


class CodeExploreSkillContractTests(unittest.TestCase):
    def test_exact_relationships_use_one_directed_trace_then_source_read(self):
        text = SKILL.read_text(encoding="utf-8")

        self.assertIn("one `trace_call_path` call", text)
        self.assertIn('`direction="inbound"` for callers', text)
        self.assertIn('`direction="outbound"` for callees', text)
        self.assertIn("Do not add `search_graph`", text)
        self.assertIn("Use `Read` to corroborate", text)
        self.assertIn("every named relationship endpoint", text)
        self.assertIn("both caller and callee", text)

    def test_conceptual_security_and_mixed_queries_have_unambiguous_precedence(self):
        text = SKILL.read_text(encoding="utf-8")
        normalized = " ".join(text.split())

        self.assertIn("Security vocabulary alone does not make", normalized)
        self.assertIn("Conceptual how, why, or whether behavior", normalized)
        self.assertIn("even when it names an exact symbol", normalized)
        self.assertIn(
            "Do not call graph security tools for conceptual behavior",
            normalized,
        )
        self.assertIn("semantic/default retrieval first", normalized)
        self.assertIn("exactly one directed graph relationship query", normalized)

    def test_identity_mismatch_blocks_cross_engine_routing_and_chaining(self):
        text = SKILL.read_text(encoding="utf-8")
        preflight = text.split("## Pre-flight Check", 1)[1].split(
            "## Routing Decision Tree", 1
        )[0]

        self.assertIn("mcp__code-search__get_index_status", preflight)
        self.assertIn("mcp__code-graph__index_status", preflight)
        for field in (
            "schema_version",
            "repository_id",
            "checkout_id",
            "source_revision",
            "dirty_fingerprint",
            "index_generation",
        ):
            self.assertIn(field, preflight)

        self.assertIn("Compare every field exactly", preflight)
        self.assertIn("block mixed or chained retrieval", preflight)
        self.assertIn("Report the exact missing, stale, or mismatched fields", preflight)
        self.assertIn("not cross-engine coherent", preflight)
        self.assertIn("Do not combine evidence", preflight)


if __name__ == "__main__":
    unittest.main()
