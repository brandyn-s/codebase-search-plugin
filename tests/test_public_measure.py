"""Unit tests for the bounded public retrieval measurement."""

from __future__ import annotations

import json
import os
from pathlib import Path
import unittest
from unittest import mock

from bench.public_measure.run import (
    CATEGORIES,
    exact_paired_binary_test,
    parse_sse,
    query_anchors,
    route_aware_compose,
    rrf,
    score_ranking,
    sourcegraph_search,
    validate_inputs,
    wilson_interval,
)


ROOT = Path(__file__).resolve().parents[1]
MEASURE = ROOT / "bench" / "public_measure"
EXTERNAL_PIN = Path(os.environ.get("CODE_INTEL_PUBLIC_SELECTION_PIN", ""))


class PublicMeasurementTests(unittest.TestCase):
    def test_sourcegraph_uses_only_bounded_identical_query_retries(self):
        contract = {
            "sourcegraph": {
                "endpoint": "https://example.invalid/search",
                "max_attempts": 3,
                "query_version": "V3",
                "requested_matches": 100,
                "timeout_seconds": 1,
            },
            "top_k_files": 10,
        }
        case = {
            "case_id": "owner__repo-1",
            "repository": "owner/repo",
            "revision": "a" * 40,
        }
        body = (
            'event: matches\ndata: [{"path":"src/right.py","commit":"'
            + "a" * 40
            + '"}]\n\n'
            'event: progress\ndata: {"done":true,"matchCount":1}\n\n'
        ).encode()
        response = mock.MagicMock()
        response.__enter__.return_value.read.return_value = body
        with mock.patch(
            "bench.public_measure.run.urllib.request.urlopen",
            side_effect=[TimeoutError("transient"), response],
        ) as opened:
            files, _elapsed, metadata = sourcegraph_search(
                contract, case, ["right_symbol"]
            )

        self.assertEqual(files, ["src/right.py"])
        self.assertEqual(metadata["attempts"], 2)
        self.assertEqual(len(metadata["retry_failures"]), 1)
        self.assertEqual(opened.call_count, 2)

    def test_balanced_n20_summary_preserves_claim_and_scale_boundaries(self):
        summary = json.loads(
            (
                MEASURE / "results" / "2026-08-12-n20-summary.json"
            ).read_text(encoding="utf-8")
        )

        self.assertEqual(summary["cases"], 20)
        self.assertEqual(summary["cases_per_category"], 5)
        self.assertEqual(summary["language_model_calls"], 0)
        self.assertFalse(
            summary["interpretation"][
                "general_platform_superiority_claim_allowed"
            ]
        )
        self.assertFalse(
            summary["result"]["paired_code_search_vs_sourcegraph_acc_at_1"]
            ["significant_at_0_05"]
        )
        self.assertEqual(
            summary["result"]["aggregate"]["code-search"]["file_acc_at_1"],
            0.4,
        )
        self.assertGreaterEqual(
            summary["scale"]["largest_line_count"]["utf8_text_lines"],
            1_000_000,
        )
        self.assertFalse(
            summary["interpretation"][
                "very_large_monorepo_or_distributed_scale_demonstrated"
            ]
        )
        self.assertEqual(
            summary["unavailable_competitors"],
            ["Cursor", "Augment", "Greptile"],
        )

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

    def test_public_comparison_reports_bounded_statistical_uncertainty(self):
        interval = wilson_interval(successes=15, total=20)
        self.assertLess(interval["lower"], 0.75)
        self.assertGreater(interval["upper"], 0.75)
        self.assertEqual(interval["confidence"], 0.95)

        comparison = exact_paired_binary_test(wins=10, losses=0, ties=10)
        self.assertEqual(comparison["discordant_pairs"], 10)
        self.assertAlmostEqual(comparison["two_sided_p_value"], 0.001953125)
        self.assertTrue(comparison["significant_at_0_05"])

        tied = exact_paired_binary_test(wins=0, losses=0, ties=20)
        self.assertEqual(tied["two_sided_p_value"], 1.0)

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

    def test_selection_contract_supports_predocumented_oracle_exclusions(self):
        categories = sorted(CATEGORIES)
        selected_ids = [f"selected-{index}" for index in range(len(categories))]
        excluded_id = "excluded-before-first-bug"
        pin_cases = [
            {"instance_id": excluded_id, "category": categories[0]},
            *[
                {"instance_id": case_id, "category": category}
                for case_id, category in zip(selected_ids, categories)
            ],
        ]
        cases = []
        oracles = []
        for index, (case_id, category) in enumerate(
            zip(selected_ids, categories), start=1
        ):
            revision = f"{index:040x}"
            cases.append(
                {
                    "case_id": case_id,
                    "category": category,
                    "repository": "example/repository",
                    "revision": revision,
                    "query": f"Query {index}",
                }
            )
            label = f"src/file{index}.py:symbol{index}"
            oracles.append(
                {
                    "case_id": case_id,
                    "expected_files": [f"src/file{index}.py"],
                    "expected_functions": [label],
                    "github_pr": {
                        "base_revision": revision,
                        "hunk_functions": [label],
                    },
                }
            )
        contract = {
            "schema_version": 1,
            "language_model_calls": 0,
            "selection": {
                "count": 4,
                "per_category": 1,
                "excluded_case_ids": [excluded_id],
            },
        }

        selected, by_id = validate_inputs(
            contract,
            {"cases": cases},
            {"cases": oracles},
            {"n": 200, "cases": pin_cases},
        )

        self.assertEqual([item["case_id"] for item in selected], selected_ids)
        self.assertEqual(set(by_id), set(selected_ids))

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
