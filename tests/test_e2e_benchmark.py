"""Acceptance tests for the recorded-trace routing/evidence benchmark."""

from __future__ import annotations

from copy import deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
BENCH = ROOT / "bench" / "e2e"


def load_scorer():
    scorer_path = BENCH / "score.py"
    spec = importlib.util.spec_from_file_location("e2e_score", scorer_path)
    if spec is None or spec.loader is None:
        raise AssertionError("could not load benchmark scorer")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class EndToEndBenchmarkTests(unittest.TestCase):
    def _score(
        self, run_name: str, *, run_path: Path | None = None
    ) -> subprocess.CompletedProcess:
        return subprocess.run(
            [
                sys.executable,
                str(BENCH / "score.py"),
                "--cases",
                str(BENCH / "cases.jsonl"),
                "--runs",
                str(run_path or BENCH / "runs" / run_name),
                "--thresholds",
                str(BENCH / "thresholds.json"),
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_good_fixture_scores_all_required_metrics_and_passes_thresholds(self):
        completed = self._score("fixture-good.jsonl")

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        for metric in (
            "routing_accuracy",
            "evidence_precision",
            "evidence_recall",
            "unsupported_claim_rate",
            "tool_calls",
            "latency_ms",
            "stale_index_errors",
        ):
            self.assertIn(metric, completed.stdout)
        self.assertIn("FIXTURE VALIDATION — NOT A LIVE BENCHMARK RESULT", completed.stdout)
        self.assertIn("thresholds: PASS", completed.stdout)
        self.assertIn('"unsupported_claim_rate": 0.0', completed.stdout)

    def test_bad_fixture_fails_thresholds(self):
        completed = self._score("fixture-bad.jsonl")

        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("thresholds: FAIL", completed.stdout)
        self.assertIn("routing_accuracy", completed.stdout)
        self.assertIn("stale_index_errors", completed.stdout)
        self.assertIn('"unsupported_claim_rate": 1.0', completed.stdout)

    def _load_jsonl(self, path: Path) -> list[dict]:
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def _score_mutation(self, mutator) -> subprocess.CompletedProcess:
        records = deepcopy(
            self._load_jsonl(BENCH / "runs" / "fixture-good.jsonl")
        )
        by_case = {record["case_id"]: record for record in records}
        mutator(by_case)
        with tempfile.TemporaryDirectory() as tmp:
            run_path = Path(tmp) / "mutated.jsonl"
            run_path.write_text(
                "\n".join(json.dumps(record) for record in records) + "\n",
                encoding="utf-8",
            )
            return self._score("mutated.jsonl", run_path=run_path)

    def test_cases_define_nonempty_adjudicated_claim_contracts(self):
        cases = self._load_jsonl(BENCH / "cases.jsonl")
        for case in cases:
            claims = case["expected_claims"]
            self.assertTrue(claims, case["case_id"])
            for claim in claims:
                self.assertTrue(claim["claim_id"])
                self.assertEqual(claim["text"], " ".join(claim["text"].split()))
                self.assertTrue(claim["required_evidence_ids"])

        covered = {case["category"] for case in cases if case["expected_claims"]}
        for category in ("graph", "lexical", "stale", "mismatch"):
            self.assertIn(category, covered)

    def test_cases_are_grounded_in_a_content_addressed_target_fixture(self):
        manifest_path = BENCH / "target-repo-manifest.json"
        self.assertTrue(manifest_path.is_file(), manifest_path)
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        target_root = BENCH / manifest["source_root"]
        recorded_hashes = manifest["files"]

        self.assertEqual(manifest["schema_version"], 1)
        self.assertEqual(manifest["repository"], "fixture://codebase-search-e2e-v1")
        self.assertTrue(target_root.is_dir())
        self.assertTrue(recorded_hashes)

        actual_hashes = {}
        for relative, expected_sha256 in recorded_hashes.items():
            source = target_root / relative
            self.assertTrue(source.is_file(), source)
            actual = hashlib.sha256(source.read_bytes()).hexdigest()
            self.assertEqual(actual, expected_sha256, relative)
            actual_hashes[relative] = actual

        canonical_tree = "\n".join(
            f"{relative}\0{actual_hashes[relative]}"
            for relative in sorted(actual_hashes)
        ).encode("utf-8")
        self.assertEqual(
            manifest["revision"],
            hashlib.sha256(canonical_tree).hexdigest(),
        )

        for case in self._load_jsonl(BENCH / "cases.jsonl"):
            for evidence_id in case["expected_evidence"]:
                if evidence_id.startswith("index:"):
                    continue
                relative, start_end = evidence_id.rsplit(":", 1)
                _, end = (int(value) for value in start_end.split("-", 1))
                self.assertIn(relative, recorded_hashes, evidence_id)
                line_count = len(
                    (target_root / relative)
                    .read_text(encoding="utf-8")
                    .splitlines()
                )
                self.assertLessEqual(end, line_count, evidence_id)

    def test_claim_omission_substitution_and_under_citation_are_unsupported(self):
        mutations = {
            "empty diagnostic claims": lambda runs: runs[
                "stale-semantic-index"
            ].__setitem__("claims", []),
            "false text with valid citations": lambda runs: runs[
                "semantic-auth"
            ]["claims"][0].__setitem__(
                "text", "Bearer tokens are never verified before forwarding."
            ),
            "unknown id leaves expected claim missing": lambda runs: runs[
                "graph-callers"
            ]["claims"][0].__setitem__("claim_id", "unknown-claim"),
            "under-cited mixed claim": lambda runs: runs["mixed-auth-flow"][
                "claims"
            ][0].__setitem__(
                "evidence_ids",
                [
                    "src/auth/login.py:20-73",
                    "src/api/session.py:14-39",
                ],
            ),
        }

        for label, mutator in mutations.items():
            with self.subTest(label=label):
                completed = self._score_mutation(mutator)
                self.assertEqual(
                    completed.returncode,
                    1,
                    completed.stdout + completed.stderr,
                )
                self.assertIn("unsupported_claim_rate", completed.stdout)
                self.assertIn("thresholds: FAIL", completed.stdout)

    def test_graph_lexical_search_is_in_the_canonical_retrieval_set(self):
        scorer = load_scorer()
        graph_lexical = "mcp__code-graph__search_code"

        self.assertIn(graph_lexical, scorer.LEXICAL_TOOLS)
        self.assertIn(graph_lexical, scorer.RETRIEVAL_TOOLS)

    def test_graph_lexical_retrieval_fails_stale_and_mismatch_cases(self):
        graph_lexical_call = {
            "tool": "mcp__code-graph__search_code",
            "arguments": {"query": "current code"},
            "latency_ms": 10,
        }
        for case_id in (
            "stale-semantic-index",
            "identity-generation-mismatch",
        ):
            with self.subTest(case_id=case_id):
                completed = self._score_mutation(
                    lambda runs, target=case_id: runs[target]["tool_calls"].append(
                        graph_lexical_call
                    )
                )
                self.assertEqual(
                    completed.returncode,
                    1,
                    completed.stdout + completed.stderr,
                )
                self.assertIn('"stale_index_errors": 1', completed.stdout)

    def test_live_mode_flip_requires_separate_provenance(self):
        completed = self._score_mutation(
            lambda runs: [
                run.__setitem__("run_mode", "live") for run in runs.values()
            ]
        )

        self.assertEqual(completed.returncode, 2, completed.stdout + completed.stderr)
        self.assertIn("live runs require --provenance", completed.stderr)
        self.assertNotIn("LIVE BENCHMARK RESULT", completed.stdout)


if __name__ == "__main__":
    unittest.main()
