import importlib.util
from pathlib import Path
import unittest


SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "proof_evaluator.py"
spec = importlib.util.spec_from_file_location("proof_evaluator", SCRIPT)
module = importlib.util.module_from_spec(spec)
assert spec.loader is not None
spec.loader.exec_module(module)


class RelationshipVectorTests(unittest.TestCase):
    def test_go_and_python_relationship_evidence_vectors_match(self):
        repository_id = "a" * 64
        source_revision = "b" * 40
        index_generation = "c" * 64

        source_payload = {
            "schema_version": 1,
            "repository_id": repository_id,
            "source_revision": source_revision,
            "relative_path": "src/admin.py",
            "symbol_kind": "function",
            "qualified_name": "admin_handler",
            "start_line": 1,
            "end_line": 8,
        }
        source = {
            "id": module._stable_id("sym", source_payload),
            **source_payload,
        }
        self.assertEqual(
            source["id"],
            "sym:v1:dcd59dece0d2fa6fa01aeb378946cc77ac7608b3c759299ae31e2bef787dd636",
        )

        target_payload = {
            "schema_version": 1,
            "repository_id": repository_id,
            "source_revision": source_revision,
            "relative_path": "src/auth.py",
            "symbol_kind": "method",
            "qualified_name": "Auth.verify",
            "start_line": 10,
            "end_line": 20,
        }
        target = {
            "id": module._stable_id("sym", target_payload),
            **target_payload,
        }
        self.assertEqual(
            target["id"],
            "sym:v1:228a411de5eb1f52b61bd1abb05f9b7b4d20680cd35163f3e8934a47ce6952a0",
        )

        relationship_payload = {
            "schema_version": 1,
            "repository_id": repository_id,
            "source_revision": source_revision,
            "index_generation": index_generation,
            "relation_type": "calls",
            "source_symbol_ref": source,
            "target_symbol_ref": target,
            "resolution_source": "go_lsp_cross_file",
            "confidence_band": "high",
            "runtime_observed": True,
            "observation_count": 17,
        }
        relationship = {
            "id": module._stable_id("rel", relationship_payload),
            **relationship_payload,
        }
        self.assertEqual(
            relationship["id"],
            "rel:v1:cde3496a62834bd1d9a9c4c5f39d91af458133023b31dcb3c4bcb51f8a5dfb39",
        )

        evidence_payload = {
            "schema_version": 1,
            "repository_id": repository_id,
            "source_revision": source_revision,
            "index_generation": index_generation,
            "relative_path": source["relative_path"],
            "start_line": source["start_line"],
            "end_line": source["end_line"],
            "evidence_type": "relationship",
            "symbol_ref": source,
            "relationship_ref": relationship,
        }
        evidence = {
            "id": module._stable_id("ev", evidence_payload),
            **evidence_payload,
        }
        self.assertEqual(
            evidence["id"],
            "ev:v1:bdc8d319a1c3ca96bbc4f3be166c500a0f12076a2e64dc4c2470cb17b7d91730",
        )

        observation_payload = {
            "schema_version": 1,
            "evidence_ref": evidence,
            "stance": "support",
            "source_engine": "code-graph",
            "derivation": "go_lsp_cross_file",
            "confidence_band": "high",
        }
        self.assertEqual(
            module._stable_id("obs", observation_payload),
            "obs:v1:3f199e7202bb70d076c1a363b3caf4fbe96209f0a537ecfe1f53502a448fac45",
        )

    def test_windows_and_posix_paths_validate_to_same_refs(self):
        repository_id = "r" * 64
        source_revision = "s" * 40
        index_generation = "g" * 64

        symbol_payload = {
            "schema_version": 1,
            "repository_id": repository_id,
            "source_revision": source_revision,
            "relative_path": "src/auth.py",
            "symbol_kind": "method",
            "qualified_name": "Auth.verify",
            "start_line": 10,
            "end_line": 20,
        }
        posix_symbol = {
            "id": module._stable_id("sym", symbol_payload),
            **symbol_payload,
        }
        windows_symbol = {
            **posix_symbol,
            "relative_path": r".\src\auth.py",
        }
        self.assertEqual(
            module._validate_symbol_ref(windows_symbol, "windows_symbol"),
            module._validate_symbol_ref(posix_symbol, "posix_symbol"),
        )

        evidence_payload = {
            "schema_version": 1,
            "repository_id": repository_id,
            "source_revision": source_revision,
            "index_generation": index_generation,
            "relative_path": "src/auth.py",
            "start_line": 10,
            "end_line": 20,
            "evidence_type": "semantic_match",
            "symbol_ref": posix_symbol,
        }
        posix_evidence = {
            "id": module._stable_id("ev", evidence_payload),
            **evidence_payload,
        }
        windows_evidence = {
            **posix_evidence,
            "relative_path": r".\src\auth.py",
            "symbol_ref": windows_symbol,
        }
        self.assertEqual(
            module._validate_evidence_ref(windows_evidence, "windows_evidence"),
            module._validate_evidence_ref(posix_evidence, "posix_evidence"),
        )


if __name__ == "__main__":
    unittest.main()
