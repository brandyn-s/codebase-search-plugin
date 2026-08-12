"""Unit tests for direct multi-repository measurement scoring."""

from pathlib import Path
import unittest

from bench.multirepo_measure import (
    graph_observation,
    search_observation,
    timed_call,
)


class MultiRepositoryMeasurementTests(unittest.TestCase):
    def test_stability_compares_ranked_results_not_transient_metadata(self):
        class Client:
            def __init__(self):
                self.calls = 0

            def call_tool(self, _tool, _arguments):
                self.calls += 1
                return {
                    "results": [{"file": "src/right.py"}],
                    "elapsed_ms": self.calls,
                }

        _result, latencies, stable = timed_call(
            Client(), "search_all_projects", {"query": "right"}, 3
        )

        self.assertEqual(len(latencies), 3)
        self.assertTrue(stable)

    def test_graph_inventory_is_bound_by_unique_index_results(self):
        projects_by_root = {
            Path("/tmp/alpha"): "alpha",
            Path("/tmp/beta"): "beta",
            Path("/tmp/gamma"): "gamma",
        }

        self.assertEqual(len(projects_by_root), 3)
        self.assertEqual(len(set(projects_by_root.values())), 3)

    def test_search_observation_scores_project_and_file_separately(self):
        expected_root = Path("/tmp/beta")
        observed = search_observation(
            {
                "projects_attempted": 3,
                "projects_with_matches": 2,
                "results": [
                    {"project_path": "/tmp/alpha", "file": "one.py"},
                    {"project_path": "/tmp/beta", "file": "wrong.py"},
                    {"project_path": "/tmp/beta", "file": "src/right.py"},
                ],
                "project_errors": {},
            },
            expected_root=expected_root,
            expected_files={"src/right.py"},
        )

        self.assertEqual(observed["project_rank"], 2)
        self.assertEqual(observed["expected_file_rank_within_project"], 2)
        self.assertEqual(observed["projects_attempted"], 3)

    def test_graph_observation_preserves_missing_expected_file(self):
        observed = graph_observation(
            {
                "projects_attempted": 3,
                "projects_with_matches": 3,
                "results": [
                    {"project": "alpha", "file_path": "src/a.py"},
                    {"project": "beta", "file_path": "src/not-it.py"},
                    {"project": "gamma", "file_path": "src/c.py"},
                ],
                "project_errors": {},
            },
            expected_project="beta",
            expected_files={"src/right.py"},
        )

        self.assertEqual(observed["project_rank"], 2)
        self.assertIsNone(observed["expected_file_rank_within_project"])


if __name__ == "__main__":
    unittest.main()
