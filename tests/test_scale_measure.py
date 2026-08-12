import json
import unittest
from pathlib import Path

from bench.scale_measure import COMPONENTS, selected_components


ROOT = Path(__file__).resolve().parents[1]


class ScaleMeasureTests(unittest.TestCase):
    def test_default_selects_both_components_in_canonical_order(self):
        self.assertEqual(selected_components(None), COMPONENTS)

    def test_single_component_avoids_redundant_backend_work(self):
        self.assertEqual(
            selected_components(["code-graph"]),
            ("code-graph",),
        )

    def test_repeated_selection_is_deduplicated_and_canonicalized(self):
        self.assertEqual(
            selected_components(["code-graph", "code-search", "code-graph"]),
            COMPONENTS,
        )

    def test_checked_in_llvm_summary_preserves_scale_and_efficiency_limits(self):
        summary = json.loads(
            (
                ROOT
                / "bench"
                / "public_measure"
                / "results"
                / "2026-08-12-llvm-scale-summary.json"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(summary["repository"]["utf8_text_lines"], 39_222_246)
        self.assertEqual(summary["repository"]["tracked_files"], 160_123)
        self.assertEqual(summary["language_model_calls"], 0)
        self.assertEqual(summary["components"]["code-search"]["status"], "completed")
        self.assertEqual(summary["components"]["code-graph"]["status"], "completed")
        self.assertTrue(
            summary["interpretation"]["very_large_single_host_demonstrated"]
        )
        self.assertFalse(
            summary["interpretation"]["distributed_or_org_fleet_demonstrated"]
        )
        self.assertFalse(
            summary["interpretation"]["class_leading_efficiency_claim_allowed"]
        )


if __name__ == "__main__":
    unittest.main()
