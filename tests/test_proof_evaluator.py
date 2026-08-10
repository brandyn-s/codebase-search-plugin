import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "proof_evaluator.py"
spec = importlib.util.spec_from_file_location("proof_evaluator", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)

REPOSITORY_ID = "r" * 64
SOURCE_REVISION = "s" * 40
INDEX_GENERATION = "g" * 64


def _claim():
    payload = {
        "schema_version": 1,
        "repository_id": REPOSITORY_ID,
        "claim_kind": "security_invariant",
        "claim_text": "All administrative routes pass through authorization.",
    }
    return {"id": module._stable_id("claim", payload), **payload}


def _observation(
    *,
    path: str,
    qualified_name: str,
    source_engine: str,
    derivation: str,
    stance: str = "support",
    generation: str = INDEX_GENERATION,
):
    symbol_payload = {
        "schema_version": 1,
        "repository_id": REPOSITORY_ID,
        "source_revision": SOURCE_REVISION,
        "relative_path": path,
        "symbol_kind": "function",
        "qualified_name": qualified_name,
        "start_line": 1,
        "end_line": 10,
    }
    symbol = {"id": module._stable_id("sym", symbol_payload), **symbol_payload}
    evidence_payload = {
        "schema_version": 1,
        "repository_id": REPOSITORY_ID,
        "source_revision": SOURCE_REVISION,
        "index_generation": generation,
        "relative_path": path,
        "start_line": 1,
        "end_line": 10,
        "evidence_type": derivation,
        "symbol_ref": symbol,
    }
    evidence = {
        "id": module._stable_id("ev", evidence_payload),
        **evidence_payload,
    }
    observation_payload = {
        "schema_version": 1,
        "evidence_ref": evidence,
        "stance": stance,
        "source_engine": source_engine,
        "derivation": derivation,
        "confidence_band": "high",
    }
    return {
        "id": module._stable_id("obs", observation_payload),
        **observation_payload,
    }


def _bundle():
    return {
        "schema_version": 1,
        "claim": _claim(),
        "index_state": {
            "coherent": True,
            "freshness": "current",
            "index_generation": INDEX_GENERATION,
        },
        "observations": [
            _observation(
                path="src/api/admin.py",
                qualified_name="admin_handler",
                source_engine="code-graph",
                derivation="resolved_call_path",
            ),
            _observation(
                path="src/auth/middleware.py",
                qualified_name="AuthMiddleware.verify",
                source_engine="code-search",
                derivation="hybrid_match",
            ),
        ],
        "contradiction_search": {
            "performed": True,
            "strategy": "enumerate_routes_and_search_bypasses",
            "candidate_count": 13,
        },
        "coverage": {
            "state": "complete",
            "examined": 13,
            "expected": 13,
            "unresolved": 0,
        },
        "invariant": {
            "id": "SEC-AUTH-001",
            "status": "pass",
            "checked": 13,
            "violations": 0,
            "unresolved": 0,
        },
    }


class ProofEvaluatorTests(unittest.TestCase):
    def test_verified_claim_requires_complete_contradiction_checked_evidence(self):
        result = module.evaluate(_bundle())
        self.assertEqual(result["verdict"], "verified")
        self.assertEqual(result["confidence"]["band"], "high")
        self.assertEqual(
            result["proof_id"],
            "proof:v1:5ad05cdd391d4c55f3bc7bbf6cabecefea17d423d9c7802c886ed29b44175df7",
        )
        self.assertEqual(len(result["supporting_observation_ids"]), 2)

    def test_counterexample_contradicts_claim(self):
        bundle = _bundle()
        bundle["observations"].append(
            _observation(
                path="src/api/debug.py",
                qualified_name="debug_admin_handler",
                source_engine="code-graph",
                derivation="authorization_bypass",
                stance="contradict",
            )
        )
        bundle["invariant"] = {
            "id": "SEC-AUTH-001",
            "status": "fail",
            "checked": 13,
            "violations": 1,
            "unresolved": 0,
        }
        result = module.evaluate(bundle)
        self.assertEqual(result["verdict"], "contradicted")
        self.assertEqual(result["confidence"]["band"], "high")
        self.assertTrue(result["contradicting_observation_ids"])

    def test_missing_contradiction_pass_remains_unresolved(self):
        bundle = _bundle()
        bundle["contradiction_search"]["performed"] = False
        result = module.evaluate(bundle)
        self.assertEqual(result["verdict"], "unresolved")
        self.assertIn(
            "contradiction_search_not_performed",
            result["caveats"],
        )

    def test_complete_coverage_requires_a_known_expected_count(self):
        bundle = _bundle()
        bundle["coverage"]["expected"] = None
        with self.assertRaisesRegex(
            module.ProofInputError,
            "complete coverage requires a known expected count",
        ):
            module.evaluate(bundle)

    def test_complete_coverage_requires_every_expected_subject(self):
        bundle = _bundle()
        bundle["coverage"]["examined"] = 12
        with self.assertRaisesRegex(
            module.ProofInputError,
            "complete coverage requires examined to equal expected",
        ):
            module.evaluate(bundle)

    def test_incoherent_indexes_block_proof(self):
        bundle = _bundle()
        bundle["index_state"]["coherent"] = False
        result = module.evaluate(bundle)
        self.assertEqual(result["verdict"], "blocked")
        self.assertEqual(
            result["blockers"],
            ["cross_engine_index_incoherent"],
        )

    def test_observation_from_other_generation_is_rejected(self):
        bundle = _bundle()
        bundle["observations"][0] = _observation(
            path="src/api/admin.py",
            qualified_name="admin_handler",
            source_engine="code-graph",
            derivation="resolved_call_path",
            generation="x" * 64,
        )
        with self.assertRaisesRegex(
            module.ProofInputError,
            "different index generation",
        ):
            module.evaluate(bundle)

    def test_forged_reference_id_is_rejected(self):
        bundle = _bundle()
        bundle["observations"][0]["evidence_ref"]["id"] = (
            "ev:v1:" + "0" * 64
        )
        with self.assertRaisesRegex(
            module.ProofInputError,
            "canonical contents",
        ):
            module.evaluate(bundle)

    def test_proof_id_is_deterministic(self):
        first = module.evaluate(_bundle())
        second = module.evaluate(_bundle())
        self.assertEqual(first["proof_id"], second["proof_id"])


if __name__ == "__main__":
    unittest.main()
