"""Acceptance tests for the literal /index-repo skill contract."""

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "index-repo" / "SKILL.md"


class IndexRepoSkillContractTests(unittest.TestCase):
    def test_reports_backend_lifecycle_deltas_without_making_them_readiness_gates(self):
        text = SKILL.read_text(encoding="utf-8")
        normalized = " ".join(text.split())

        for semantic_delta in (
            "files_added",
            "files_modified",
            "files_removed",
            "chunks_added",
            "chunks_removed",
        ):
            self.assertIn(semantic_delta, text)

        for graph_delta in (
            "index_delta.mode",
            "full",
            "noop",
            "incremental",
            "files_discovered",
            "files_changed",
            "files_unchanged",
        ):
            self.assertIn(graph_delta, text)

        self.assertIn("non-gating lifecycle telemetry", normalized)
        self.assertIn("do not infer semantic equivalence", normalized.lower())
        self.assertNotIn("peak RSS", text)

    def test_auto_scip_is_explicit_pinned_and_never_silently_compiler_grade(self):
        text = SKILL.read_text(encoding="utf-8")
        normalized = " ".join(text.split())

        for contract in (
            "--graph-precision heuristic|scip|auto",
            "--scip-policy preferred|required",
            "scripts/prepare_scip_index.py",
            "component-bom.json",
            "scip-go",
            "go.mod",
            "scip-typescript",
            "tsconfig.json",
            "node_modules",
            "index_generation",
        ):
            self.assertIn(contract, text)

        self.assertIn(
            "Automatic SCIP generation is explicit opt-in",
            normalized,
        )
        self.assertIn(
            "required` policy stops before code-graph",
            normalized,
        )
        self.assertIn(
            "preferred` policy may continue only as requested/effective "
            "`heuristic`",
            normalized,
        )
        self.assertIn(
            "Never describe that fallback as compiler-grade",
            text,
        )
        self.assertIn(
            "scip_status.index_sha256` to equal the preparation receipt's",
            normalized,
        )
        self.assertIn("nonzero SCIP insertions", normalized)
        self.assertIn(
            "Nonzero `drifted_documents` is coverage telemetry, not a "
            "global failure",
            normalized,
        )
        self.assertIn("resolution_artifact_sha256", text)
        self.assertIn("`compiler_resolution` lattice capability", normalized)
        self.assertIn("never installs dependencies in the target", normalized)

    def test_waits_for_semantic_completion_before_graph_and_verifies_identity(self):
        text = SKILL.read_text(encoding="utf-8")

        ordered_markers = [
            "mcp__code-search__index_directory",
            "mcp__code-search__get_indexing_progress",
            "mcp__code-graph__index_repository",
            "mcp__code-search__get_index_status",
            "mcp__code-graph__index_status",
            "mcp__code-search__switch_project",
        ]
        positions = [text.find(marker) for marker in ordered_markers]
        self.assertNotIn(-1, positions, ordered_markers)
        self.assertEqual(positions, sorted(positions))

        for terminal_failure in ("failed", "cancelled", "unknown", "timeout"):
            self.assertIn(terminal_failure, text.lower())

        for identity_field in (
            "repository_id",
            "checkout_id",
            "source_revision",
            "dirty_fingerprint",
            "index_generation",
            "captured_at",
        ):
            self.assertIn(identity_field, text)

        self.assertIn("Legacy or missing identity fields are incompatible", text)
        self.assertIn("Do not run code-graph indexing", text)
        self.assertNotIn("mcp__code-graph__rank_by_query", text)
        self.assertNotIn("mcp__code-graph__code_localize", text)

    def test_requires_graph_report_suppression_before_either_index_starts(self):
        text = SKILL.read_text(encoding="utf-8")
        compatibility_path = ROOT / "compatibility" / "README.md"
        self.assertTrue(compatibility_path.is_file(), compatibility_path)
        compatibility = compatibility_path.read_text(encoding="utf-8")

        self.assertIn("skip_report=true", text)
        self.assertIn("Before starting either index", text)
        self.assertIn("do not start code-search", text.lower())
        self.assertIn("v0.8.0-redacted.9", compatibility)
        self.assertIn(
            "integrated readiness status is `ready`",
            compatibility.lower(),
        )
        self.assertIn("skip_report", compatibility)

    def test_preflight_requires_live_schemas_matching_the_bom_snapshot(self):
        text = SKILL.read_text(encoding="utf-8")
        preflight = text.split("2. **Before starting either index**", 1)[1].split(
            "3. Start **code-search** indexing:", 1
        )[0]
        normalized_preflight = " ".join(preflight.split())

        for gate in (
            "installed live host tool metadata",
            "snapshot alone is insufficient",
            "exact canonical input-schema fingerprint",
            "boolean `skip_report`",
            "optional string `project_path`",
        ):
            self.assertIn(gate, normalized_preflight)

    def test_binds_new_semantic_job_to_resolved_git_root_on_every_response(self):
        text = SKILL.read_text(encoding="utf-8")
        normalized_text = " ".join(text.split())

        self.assertIn(
            "git -C <candidate-path> rev-parse --show-toplevel",
            text,
        )
        self.assertNotIn("has `.git/` directory", text)
        self.assertIn("resolved repository root", normalized_text)

        for binding in (
            "job_id == <semantic-job-id>",
            "directory == <resolved-root>",
            "project_name == <resolved-project-name>",
        ):
            self.assertIn(binding, text)

        self.assertIn("Do not adopt any pre-existing job", text)
        self.assertIn("indexing_conflict", text)
        self.assertIn("requested_directory", text)
        self.assertIn("Every polling response, including a terminal response", text)
        self.assertIn("binding mismatch", normalized_text.lower())

    def test_completed_semantic_job_requires_explicit_success_and_readiness(self):
        text = SKILL.read_text(encoding="utf-8")

        for terminal_gate in (
            'status == "completed"',
            "result.success == true",
            "index_ready == true",
            "result.index_ready == true",
            "result.error is absent, null, or empty",
        ):
            self.assertIn(terminal_gate, text)

        self.assertIn("Do not start code-graph", text)

    def test_final_semantic_status_uses_exact_fail_closed_gates(self):
        text = SKILL.read_text(encoding="utf-8")
        verification = text.split(
            "6. Verify both engines independently after indexing:", 1
        )[1].split(
            "7. Only after both indexes and identities verify", 1
        )[0]

        for final_gate in (
            "mcp__code-search__get_index_status(project_path=<resolved-root>)",
            "index_ready == true",
            'index_identity_status == "ready"',
            "semantic error is absent, null, or empty",
        ):
            self.assertIn(final_gate, verification)
        self.assertNotIn("report a usable index", verification)

    def test_final_statuses_are_bound_to_the_requested_targets(self):
        text = SKILL.read_text(encoding="utf-8")
        verification = text.split(
            "6. Verify both engines independently after indexing:", 1
        )[1].split(
            "7. Only after both indexes and identities verify", 1
        )[0]

        for binding in (
            "project_path == <resolved-root>",
            "project == <graph-project>",
            "root_path == <resolved-root>",
            "Missing or different binding fields fail closed",
        ):
            self.assertIn(binding, verification)

    def test_graph_completion_identity_is_verified_before_cross_engine_match(self):
        text = SKILL.read_text(encoding="utf-8")
        verification = text.split(
            "6. Verify both engines independently after indexing:", 1
        )[1].split(
            "7. Only after both indexes and identities verify", 1
        )[0]

        completion_check = (
            "First compare the graph completion identity with the final graph "
            "identity"
        )
        cross_engine_check = (
            "Only after that comparison passes, compare the final semantic "
            "and graph identities"
        )
        self.assertIn(completion_check, verification)
        self.assertIn(cross_engine_check, verification)
        self.assertLess(
            verification.index(completion_check),
            verification.index(cross_engine_check),
        )
        self.assertIn(
            "`repository_id`, `checkout_id`, `source_revision`, "
            "`dirty_fingerprint`, and `index_generation`",
            verification,
        )

    def test_graph_indexing_uses_actual_response_contract(self):
        text = SKILL.read_text(encoding="utf-8")
        graph_step = text.split(
            "5. Run **code-graph** indexing without mutating the checkout:", 1
        )[1].split(
            "6. Verify both engines independently after indexing:", 1
        )[0]

        for gate in (
            "MCP `isError` is absent or false",
            "error is absent, null, or empty",
            '`status` is absent or exactly `"ready"`',
            'explicit null, `"failed"`, `"degraded"`, or unknown',
            'identity_status == "captured"',
            "non-empty `project`",
        ):
            self.assertIn(gate, graph_step)
        self.assertNotIn('status` is not `"degraded"`', graph_step)
        self.assertNotIn("success == true", graph_step)


if __name__ == "__main__":
    unittest.main()
