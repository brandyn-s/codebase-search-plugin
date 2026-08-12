"""Unit tests for the bounded public retrieval measurement."""

from __future__ import annotations

import json
import os
from pathlib import Path
import unittest

from bench.public_measure.run import (
    CATEGORIES,
    parse_sse,
    query_anchors,
    route_aware_compose,
    rrf,
    score_ranking,
    validate_inputs,
)


ROOT = Path(__file__).resolve().parents[1]
MEASURE = ROOT / "bench" / "public_measure"
EXTERNAL_PIN = Path(os.environ.get("CODE_INTEL_PUBLIC_SELECTION_PIN", ""))


class PublicMeasurementTests(unittest.TestCase):
    def test_checked_in_summary_preserves_scope_and_result_bindings(self):
        summary = json.loads(
            (
                MEASURE / "results" / "2026-08-12-summary.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(summary["cases"], 4)
        self.assertEqual(summary["language_model_calls"], 0)
        self.assertIn("no statistical superiority claim", summary["scope"])
        self.assertEqual(
            summary["current_run"]["aggregate"]["code-search"][
                "file_acc_at_1"
            ],
            1.0,
        )
        self.assertEqual(
            summary["current_run"]["aggregate"]["composed"][
                "file_mrr_at_10"
            ],
            1.0,
        )
        self.assertEqual(
            summary["provenance"]["code_search_release"], "v0.3.4"
        )
        self.assertEqual(summary["stability"]["code_graph_stable_cases"], 4)
        self.assertEqual(
            summary["prior_baseline"]["code_search_acc_at_1"], 0.0
        )

    def test_query_adapter_is_deterministic_and_oracle_blind(self):
        ux = query_anchors(
            "Optimize Face Centroid Calculations\n"
            "If `Grid.face_lon` does not exist, `_populate_face_centroids()` "
            "calls `_construct_face_centroids()`."
        )
        self.assertEqual(
            ux[:3],
            ["Grid.face_lon", "_populate_face_centroids", "_construct_face_centroids"],
        )
        chainlit = query_anchors(
            "Security: allowed origins should not be * by default\n"
            "CORS headers should be restricted to the current domain."
        )
        self.assertIn("CORS", chainlit)
        self.assertIn("allowed", [item.casefold() for item in chainlit])
        self.assertNotIn("Security", chainlit)
        self.assertLessEqual(len(chainlit), 8)

    def test_sse_parser_preserves_match_order_and_terminal_progress(self):
        body = (
            'event: matches\ndata: [{"type":"content","path":"b.py"}]\n\n'
            'event: matches\ndata: [{"type":"content","path":"a.py"}]\n\n'
            'event: progress\ndata: {"done":true,"matchCount":2}\n\n'
            'event: done\ndata: {}\n\n'
        )
        matches, progress, alerts = parse_sse(body)
        self.assertEqual([item["path"] for item in matches], ["b.py", "a.py"])
        self.assertEqual(progress["matchCount"], 2)
        self.assertEqual(alerts, [])

    def test_rrf_and_file_metrics_are_fixed(self):
        fused = rrf(
            [["a.py", "b.py", "c.py"], ["b.py", "d.py", "a.py"]],
            60,
            10,
        )
        self.assertEqual(fused[:2], ["b.py", "a.py"])
        score = score_ranking(fused, {"d.py"})
        self.assertEqual(score["rank"], 3)
        self.assertFalse(score["file_acc_at_1"])
        self.assertTrue(score["file_acc_at_3"])
        self.assertAlmostEqual(score["file_mrr_at_10"], 1 / 3)

    def test_route_aware_composition_preserves_the_selected_primary(self):
        conceptual, conceptual_method = route_aware_compose(
            "Where is request validation implemented?",
            ["search.py", "shared.py"],
            ["graph.py", "shared.py"],
            10,
        )
        structural, structural_method = route_aware_compose(
            "What calls validate_request?",
            ["search.py", "shared.py"],
            ["graph.py", "shared.py"],
            10,
        )

        self.assertEqual(conceptual[:3], ["search.py", "shared.py", "graph.py"])
        self.assertEqual(conceptual_method["primary"], "code-search")
        self.assertEqual(structural[:3], ["graph.py", "shared.py", "search.py"])
        self.assertEqual(structural_method["primary"], "code-graph")

    @unittest.skipUnless(
        EXTERNAL_PIN.is_file(),
        "set CODE_INTEL_PUBLIC_SELECTION_PIN to the external public pin",
    )
    def test_checked_in_cases_match_preexisting_balanced_selection(self):
        contract = json.loads((MEASURE / "contract.json").read_text())
        cases = json.loads((MEASURE / "cases.json").read_text())
        oracle = json.loads((MEASURE / "oracle.json").read_text())
        pin = json.loads(EXTERNAL_PIN.read_text())
        selected, by_id = validate_inputs(contract, cases, oracle, pin)
        self.assertEqual({item["category"] for item in selected}, CATEGORIES)
        self.assertEqual(set(by_id), {item["case_id"] for item in selected})


if __name__ == "__main__":
    unittest.main()
