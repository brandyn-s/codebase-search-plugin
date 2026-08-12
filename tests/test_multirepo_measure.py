"""Unit tests for direct multi-repository measurement scoring."""

from pathlib import Path
import unittest

from bench.multirepo_measure import graph_observation, search_observation


class MultiRepositoryMeasurementTests(unittest.TestCase):
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
                    {"project_path": "/tmp/alpha", "file_path": "one.py"},
                    {"project_path": "/tmp/beta", "file_path": "wrong.py"},
                    {"project_path": "/tmp/beta", "file_path": "src/right.py"},
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
