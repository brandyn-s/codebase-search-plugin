"""Unit tests for the bounded index lifecycle/resource measurement."""

from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from bench.lifecycle_measure import (
    LifecycleError,
    graph_semantic_fingerprint,
    component_storage_roots,
    storage_usage,
    summarize_components,
    temporary_mutation,
    validate_comment_only_graph_cardinality,
    validate_phase_result,
)


class FakeGraphClient:
    def call_tool(self, name, arguments):
        if name == "get_graph_schema":
            return {
                "projects": [{
                    "project": "fixture",
                    "schema": {
                        "relationship_types": [
                            {"type": "CALLS", "count": 1},
                            {"type": "IMPORTS", "count": 1},
                        ]
                    },
                }]
            }
        query = arguments["query"]
        if query.startswith("MATCH (n)"):
            return {
                "rows": [
                    {"LABELS(n)": ["Function"], "n.qualified_name": "p.b"},
                    {"LABELS(n)": ["Function"], "n.qualified_name": "p.a"},
                ],
                "capped": False,
            }
        edge_type = "CALLS" if ":CALLS" in query else "IMPORTS"
        return {
            "rows": [{
                "a.qualified_name": "p.a",
                "b.qualified_name": "p.b",
            }],
            "capped": False,
            "edge_type": edge_type,
        }


class LifecycleMeasurementTests(unittest.TestCase):
    def test_graph_fingerprint_canonicalizes_nodes_and_edges(self):
        observed = graph_semantic_fingerprint(FakeGraphClient(), "fixture")
        self.assertEqual(observed["node_count"], 2)
        self.assertEqual(observed["edge_count"], 2)
        self.assertEqual(len(observed["sha256"]), 64)

    def test_storage_usage_reports_logical_and_allocated_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "ordinary.bin").write_bytes(b"abc")
            sparse = root / "sparse.bin"
            with sparse.open("wb") as handle:
                handle.seek(1024 * 1024)
                handle.write(b"x")
            (root / "ignored-link").symlink_to(root / "ordinary.bin")

            expected_allocated = sum(
                path.stat().st_blocks * 512 for path in (root / "ordinary.bin", sparse)
            )

            observed = storage_usage(root)

        self.assertEqual(observed["files"], 2)
        self.assertEqual(observed["logical_bytes"], 1_048_580)
        self.assertEqual(observed["allocated_bytes"], expected_allocated)

    def test_temporary_mutation_is_exactly_restored_after_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "src" / "module.ts"
            target.parent.mkdir()
            original = b"export const value = 1;\n"
            target.write_bytes(original)
            subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
            subprocess.run(["git", "add", "src/module.ts"], cwd=root, check=True)

            with self.assertRaisesRegex(RuntimeError, "stop"):
                with temporary_mutation(
                    root, "src/module.ts", "code_intel_lifecycle_probe"
                ) as metadata:
                    self.assertNotEqual(target.read_bytes(), original)
                    self.assertEqual(metadata["relative_path"], "src/module.ts")
                    self.assertNotEqual(
                        metadata["original_sha256"], metadata["modified_sha256"]
                    )
                    raise RuntimeError("stop")

            self.assertEqual(target.read_bytes(), original)

    def test_phase_validation_uses_backend_observations(self):
        validate_phase_result(
            "code-search",
            "no_op",
            {"files_added": 0, "files_modified": 0, "files_removed": 0},
        )
        validate_phase_result(
            "code-graph",
            "small_change",
            {
                "index_delta": {
                    "mode": "incremental",
                    "files_discovered": 9,
                    "files_changed": 1,
                    "files_unchanged": 8,
                }
            },
        )
        with self.assertRaises(LifecycleError):
            validate_phase_result(
                "code-graph",
                "no_op",
                {
                    "index_delta": {
                        "mode": "incremental",
                        "files_discovered": 9,
                        "files_changed": 1,
                        "files_unchanged": 8,
                    }
                },
            )

    def test_comment_only_graph_cardinality_fails_closed(self):
        validate_comment_only_graph_cardinality(
            {"nodes": 41, "edges": 73}, {"nodes": 41, "edges": 73}
        )
        with self.assertRaisesRegex(LifecycleError, "comment-only"):
            validate_comment_only_graph_cardinality(
                {"nodes": 41, "edges": 73}, {"nodes": 40, "edges": 69}
            )

    def test_search_storage_separates_index_from_runtime_bytes(self):
        root = Path("/runtime")
        index, runtime = component_storage_roots("code-search", root)
        self.assertEqual(index, root / "code-search-storage" / "projects")
        self.assertEqual(runtime, root / "code-search-storage")

    def test_summary_identifies_measured_dominant_index_cell(self):
        summary = summarize_components(
            {
                "code-search": {
                    "phases": {
                        "clean": {
                            "index_elapsed_ns": 100,
                            "index_storage": {"allocated_bytes": 1000},
                        },
                        "no_op": {
                            "index_elapsed_ns": 10,
                            "index_storage": {"allocated_bytes": 1000},
                        },
                        "small_change": {
                            "index_elapsed_ns": 30,
                            "index_storage": {"allocated_bytes": 1100},
                        },
                    }
                },
                "code-graph": {
                    "phases": {
                        "clean": {
                            "index_elapsed_ns": 400,
                            "index_storage": {"allocated_bytes": 2000},
                        },
                        "no_op": {
                            "index_elapsed_ns": 20,
                            "index_storage": {"allocated_bytes": 2000},
                        },
                        "small_change": {
                            "index_elapsed_ns": 80,
                            "index_storage": {"allocated_bytes": 2100},
                        },
                    }
                },
            }
        )

        self.assertEqual(
            summary["dominant_index_time_cell"],
            {"component": "code-graph", "phase": "clean", "elapsed_ns": 400},
        )
        self.assertEqual(
            summary["components"]["code-graph"]["no_op_to_clean_ratio"], 0.05
        )
        self.assertEqual(
            summary["components"]["code-search"]["update_to_clean_ratio"], 0.3
        )


if __name__ == "__main__":
    unittest.main()
