import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "code-intel" / "SKILL.md"


class CodeIntelSkillContractTests(unittest.TestCase):
    def test_final_evidence_is_exactly_pinned_and_cited(self):
        normalized = " ".join(SKILL.read_text(encoding="utf-8").split())

        self.assertIn("deletion test", normalized)
        self.assertIn("successful exact `Read`", normalized)
        self.assertIn("`offset=start`", normalized)
        self.assertIn("`limit=end-start+1`", normalized)
        self.assertIn("whole-file or unbounded Read is inspection-only", normalized)
        self.assertIn("Cite every final evidence ID verbatim", normalized)

    def test_semantic_route_does_not_accept_graph_text_corroboration(self):
        normalized = " ".join(SKILL.read_text(encoding="utf-8").split())

        self.assertIn(
            "Keep semantic and lexical work within the selected route", normalized
        )
        self.assertIn(
            "graph corroboration belongs only in graph, mixed, or security work",
            normalized,
        )


if __name__ == "__main__":
    unittest.main()
