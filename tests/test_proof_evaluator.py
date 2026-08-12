import importlib.util
from copy import deepcopy
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


def _symbol(
    *,
    path: str,
    qualified_name: str,
    symbol_kind: str = "function",
    start_line: int = 1,
    end_line: int = 10,
):
    payload = {
        "schema_version": 1,
        "repository_id": REPOSITORY_ID,
        "source_revision": SOURCE_REVISION,
        "relative_path": path,
        "symbol_kind": symbol_kind,
        "qualified_name": qualified_name,
        "start_line": start_line,
        "end_line": end_line,
    }
    return {"id": module._stable_id("sym", payload), **payload}


def _observation(
    *,
    path: str,
    qualified_name: str,
    source_engine: str,
    derivation: str,
    stance: str = "support",
    generation: str = INDEX_GENERATION,
    confidence_band: str = "high",
):
    symbol = _symbol(path=path, qualified_name=qualified_name)
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
        "confidence_band": confidence_band,
    }
    return {
        "id": module._stable_id("obs", observation_payload),
        **observation_payload,
    }


def _relationship_observation(
    *,
    relationship_generation: str = INDEX_GENERATION,
    evidence_generation: str = INDEX_GENERATION,
    resolution_source: str = "go_lsp_cross_file+runtime_trace",
    resolution_artifact_sha256: str | None = None,
    confidence_band: str = "high",
    runtime_observed: bool = True,
    observation_count: int = 17,
):
    source = _symbol(
        path="src/api/admin.py",
        qualified_name="repo.src.api.admin.admin_handler",
    )
    target = _symbol(
        path="src/auth/middleware.py",
        qualified_name="repo.src.auth.middleware.AuthMiddleware.verify",
        symbol_kind="method",
    )
    relationship_payload = {
        "schema_version": 1,
        "repository_id": REPOSITORY_ID,
        "source_revision": SOURCE_REVISION,
        "index_generation": relationship_generation,
        "relation_type": "calls",
        "source_symbol_ref": source,
        "target_symbol_ref": target,
        "resolution_source": resolution_source,
        "confidence_band": confidence_band,
        "runtime_observed": runtime_observed,
        "observation_count": observation_count,
    }
    if resolution_artifact_sha256 is not None:
        relationship_payload["resolution_artifact_sha256"] = (
            resolution_artifact_sha256
        )
    relationship = {
        "id": module._stable_id("rel", relationship_payload),
        **relationship_payload,
    }
    evidence_payload = {
        "schema_version": 1,
        "repository_id": REPOSITORY_ID,
        "source_revision": SOURCE_REVISION,
        "index_generation": evidence_generation,
        "relative_path": source["relative_path"],
        "start_line": source["start_line"],
        "end_line": source["end_line"],
        "evidence_type": (
            "runtime_validated_relationship"
            if runtime_observed
            else "static_relationship"
        ),
        "symbol_ref": source,
        "relationship_ref": relationship,
    }
    evidence = {
        "id": module._stable_id("ev", evidence_payload),
        **evidence_payload,
    }
    observation_payload = {
        "schema_version": 1,
        "evidence_ref": evidence,
        "stance": "support",
        "source_engine": "code-graph",
        "derivation": resolution_source,
        "confidence_band": confidence_band,
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
        self.assertEqual(
            result["relationship_evidence"],
            {
                "count": 0,
                "runtime_confirmed": 0,
                "resolution_sources": [],
            },
        )

    def test_legacy_bundle_validation_omits_the_optional_assurance_requirement(self):
        validated = module.validate_bundle(_bundle())

        self.assertNotIn("assurance_requirement", validated)

    def test_runtime_confirmed_relationship_can_corroborate_one_engine(self):
        bundle = _bundle()
        bundle["observations"] = [_relationship_observation()]
        result = module.evaluate(bundle)
        self.assertEqual(result["verdict"], "verified")
        self.assertEqual(result["confidence"]["band"], "high")
        self.assertEqual(result["relationship_evidence"]["count"], 1)
        self.assertEqual(
            result["relationship_evidence"]["runtime_confirmed"],
            1,
        )
        self.assertEqual(
            result["relationship_evidence"]["resolution_sources"],
            ["go_lsp_cross_file+runtime_trace"],
        )
        self.assertIn(
            "at least one static relationship is confirmed by runtime traces",
            result["confidence"]["rationale"],
        )

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

    def test_relationship_from_other_generation_is_rejected(self):
        bundle = _bundle()
        bundle["observations"] = [
            _relationship_observation(
                relationship_generation="x" * 64,
                evidence_generation=INDEX_GENERATION,
            )
        ]
        with self.assertRaisesRegex(
            module.ProofInputError,
            "relationship_ref has a different index_generation",
        ):
            module.evaluate(bundle)

    def test_runtime_relationship_requires_observations(self):
        bundle = _bundle()
        bundle["observations"] = [
            _relationship_observation(observation_count=0)
        ]
        with self.assertRaisesRegex(
            module.ProofInputError,
            "requires a positive observation_count",
        ):
            module.evaluate(bundle)

    def test_relationship_derivation_must_match_canonical_ref(self):
        bundle = _bundle()
        observation = _relationship_observation()
        observation["derivation"] = "rewritten_after_capture"
        payload = {
            key: observation[key]
            for key in (
                "schema_version",
                "evidence_ref",
                "stance",
                "source_engine",
                "derivation",
                "confidence_band",
            )
        }
        observation["id"] = module._stable_id("obs", payload)
        bundle["observations"] = [observation]
        with self.assertRaisesRegex(
            module.ProofInputError,
            "derivation disagrees with relationship_ref",
        ):
            module.evaluate(bundle)

    def test_forged_relationship_provenance_is_rejected(self):
        bundle = _bundle()
        observation = _relationship_observation()
        observation["evidence_ref"]["relationship_ref"][
            "resolution_source"
        ] = "forged_compiler_result"
        bundle["observations"] = [observation]
        with self.assertRaisesRegex(
            module.ProofInputError,
            "canonical contents",
        ):
            module.evaluate(bundle)

    def test_speculative_only_support_cannot_verify_claim(self):
        bundle = _bundle()
        bundle["observations"] = [
            _relationship_observation(
                resolution_source="fuzzy_suffix_match",
                confidence_band="speculative",
                runtime_observed=False,
                observation_count=0,
            )
        ]
        result = module.evaluate(bundle)
        self.assertEqual(result["verdict"], "unresolved")
        self.assertIn(
            "supporting_evidence_not_trustworthy",
            result["caveats"],
        )

    def test_required_compiler_capability_rejects_heuristic_support(self):
        bundle = _bundle()
        bundle["assurance_requirement"] = {
            "required_capabilities": ["compiler_resolution"],
        }
        bundle["observations"] = [
            _relationship_observation(
                resolution_source="heuristic_static_resolution",
                confidence_band="high",
                runtime_observed=False,
                observation_count=0,
            )
        ]

        result = module.evaluate(bundle)

        self.assertEqual(result["verdict"], "unresolved")
        self.assertIn(
            "required_assurance_not_satisfied",
            result["caveats"],
        )
        self.assertEqual(
            result["assurance_lattice"],
            {
                "required_capabilities": ["compiler_resolution"],
                "supporting_capabilities": [
                    "source_coordinates",
                    "structural_relationship",
                ],
                "contradicting_capabilities": [],
                "missing_supporting_capabilities": ["compiler_resolution"],
                "missing_contradicting_capabilities": ["compiler_resolution"],
                "satisfied_by": None,
            },
        )

    def test_required_compiler_capability_accepts_scip_support(self):
        bundle = _bundle()
        bundle["assurance_requirement"] = {
            "required_capabilities": [
                "source_coordinates",
                "structural_relationship",
                "compiler_resolution",
            ],
        }
        bundle["observations"] = [
            _relationship_observation(
                resolution_source="scip-ingest",
                resolution_artifact_sha256="a" * 64,
                confidence_band="high",
                runtime_observed=False,
                observation_count=0,
            )
        ]

        result = module.evaluate(bundle)

        self.assertEqual(result["verdict"], "verified")
        self.assertEqual(
            result["proof_id"],
            "proof:v1:6a0310be2366d2696dc6645546cc137bd32dc5d490e504c983c3d71c01c04bd1",
        )
        self.assertEqual(result["assurance_lattice"]["satisfied_by"], "support")
        self.assertEqual(
            result["assurance_lattice"]["missing_supporting_capabilities"],
            [],
        )
        without_requirement = deepcopy(bundle)
        del without_requirement["assurance_requirement"]
        self.assertNotEqual(
            result["proof_id"],
            module.evaluate(without_requirement)["proof_id"],
        )

    def test_required_compiler_capability_rejects_unbound_legacy_scip_support(self):
        bundle = _bundle()
        bundle["assurance_requirement"] = {
            "required_capabilities": ["compiler_resolution"],
        }
        bundle["observations"] = [
            _relationship_observation(
                resolution_source="scip-ingest",
                confidence_band="high",
                runtime_observed=False,
                observation_count=0,
            )
        ]

        result = module.evaluate(bundle)

        self.assertEqual(result["verdict"], "unresolved")
        self.assertEqual(
            result["assurance_lattice"]["missing_supporting_capabilities"],
            ["compiler_resolution"],
        )

    def test_resolution_artifact_requires_canonical_scip_digest(self):
        for digest, expected in (
            ("A" * 64, "64 lowercase hex characters"),
            ("a" * 63, "64 lowercase hex characters"),
        ):
            with self.subTest(digest=digest):
                bundle = _bundle()
                bundle["observations"] = [
                    _relationship_observation(
                        resolution_source="scip-ingest",
                        resolution_artifact_sha256=digest,
                        runtime_observed=False,
                        observation_count=0,
                    )
                ]
                with self.assertRaisesRegex(module.ProofInputError, expected):
                    module.evaluate(bundle)

        bundle = _bundle()
        bundle["observations"] = [
            _relationship_observation(
                resolution_source="go_lsp_cross_file",
                resolution_artifact_sha256="a" * 64,
                runtime_observed=False,
                observation_count=0,
            )
        ]
        with self.assertRaisesRegex(
            module.ProofInputError,
            "requires scip-ingest provenance",
        ):
            module.evaluate(bundle)

    def test_required_runtime_capability_rejects_unobserved_relationship(self):
        bundle = _bundle()
        bundle["assurance_requirement"] = {
            "required_capabilities": ["runtime_observation"],
        }
        bundle["observations"] = [
            _relationship_observation(
                resolution_source="scip-ingest",
                confidence_band="high",
                runtime_observed=False,
                observation_count=0,
            )
        ]

        result = module.evaluate(bundle)

        self.assertEqual(result["verdict"], "unresolved")
        self.assertEqual(
            result["assurance_lattice"]["missing_supporting_capabilities"],
            ["runtime_observation"],
        )

    def test_unknown_assurance_capability_is_rejected(self):
        bundle = _bundle()
        bundle["assurance_requirement"] = {
            "required_capabilities": ["plausible_model_reasoning"],
        }

        with self.assertRaisesRegex(
            module.ProofInputError,
            "unsupported capabilities",
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
