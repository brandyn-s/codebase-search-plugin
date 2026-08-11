import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_proof_schema_references_pinned_evidence_schema():
    proof_schema = json.loads(
        (ROOT / "compatibility" / "proof-schema-v1.json").read_text(
            encoding="utf-8"
        )
    )
    evidence_schema = ROOT / "compatibility" / "evidence-schema-v1.json"
    assert evidence_schema.is_file()
    assert proof_schema["$id"].endswith("proof-schema-v1.json")
    assert (
        proof_schema["$defs"]["observation"]["properties"]["evidence_ref"][
            "$ref"
        ]
        == "evidence-schema-v1.json"
    )
    assert "contradiction_search" in proof_schema["required"]
    assert "coverage" in proof_schema["required"]


def test_checked_in_invariant_examples_have_expected_fail_closed_results():
    evaluator = _load_module(
        "invariant_evaluator_contract",
        ROOT / "scripts" / "invariant_evaluator.py",
    )
    admin_bundle = json.loads(
        (
            ROOT
            / "invariants"
            / "examples"
            / "admin-routes-require-auth.json"
        ).read_text(encoding="utf-8")
    )
    sql_bundle = json.loads(
        (
            ROOT
            / "invariants"
            / "examples"
            / "no-unsanitized-input-to-sql.json"
        ).read_text(encoding="utf-8")
    )
    assert evaluator.evaluate(admin_bundle)["invariant"]["status"] == "unresolved"
    assert evaluator.evaluate(sql_bundle)["invariant"]["status"] == "fail"


def test_code_intel_skill_requires_deterministic_proof_gate():
    skill = (ROOT / "skills" / "code-intel" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    assert "scripts/invariant_evaluator.py" in skill
    assert "scripts/proof_evaluator.py" in skill
    assert "contradiction pass is mandatory" in skill
    assert 'verdict="verified"' in skill


def test_code_intel_skill_routes_callers_to_the_supported_trace_tool():
    skill = (ROOT / "skills" / "code-intel" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(skill.split())

    assert "Callers of an exact function" in skill
    assert "`mcp__code-graph__trace_call_path`" in skill
    assert '`direction="inbound"`' in skill
    assert "Call the directed trace once" in skill
    assert "Do not add `search_graph`" in skill
    assert "Use `Read` only to corroborate" in skill
    assert "does not support the full Cypher function surface" in normalized


def test_code_intel_skill_disambiguates_semantic_security_and_mixed_routes():
    skill = (ROOT / "skills" / "code-intel" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(skill.split())

    assert "Security vocabulary alone does not make" in normalized
    assert "Conceptual how, why, or whether behavior" in normalized
    assert "even when it names an exact symbol" in normalized
    assert "Do not call graph security tools for conceptual behavior" in normalized
    assert "semantic/default retrieval first" in normalized
    assert "exactly one directed graph relationship query" in normalized


def test_code_intel_skill_binds_relationship_evidence_and_claim_identity():
    skill = (ROOT / "skills" / "code-intel" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(skill.split())

    assert "every named relationship endpoint" in normalized
    assert "both caller and callee" in normalized
    assert "byte-for-byte, including terminal punctuation" in normalized


def test_code_intel_skill_requires_minimal_exact_evidence_ranges():
    skill = (ROOT / "skills" / "code-intel" / "SKILL.md").read_text(
        encoding="utf-8"
    )
    normalized = " ".join(skill.split())

    assert "smallest sufficient set" in normalized
    assert "Shrink every `path:start-end` range" in normalized
    assert "imports, blank lines, or surrounding context" in normalized
    assert "deletion test" in normalized
    assert "claim-scoped, not flow-scoped" in normalized
    assert "imports or aliases are discovery context" in normalized
    assert "upstream or downstream" in normalized
    assert "directly contradicts at least one atomic clause" in normalized
    assert "disposition must be supported" in normalized
