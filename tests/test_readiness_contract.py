"""Fail-closed integrated-readiness contracts for the tested component BOM."""

from copy import deepcopy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
REQUIRED_IDENTITY_FIELDS = [
    "repository_id",
    "checkout_id",
    "source_revision",
    "dirty_fingerprint",
    "index_generation",
    "captured_at",
]
EQUAL_IDENTITY_FIELDS = [
    "repository_id",
    "checkout_id",
    "source_revision",
    "dirty_fingerprint",
    "index_generation",
]

CURRENT_SEARCH_CAPABILITIES = {
    "outputs": {
        "index_identity": {
            "supported": True,
            "schema_version": 1,
            "fields": REQUIRED_IDENTITY_FIELDS,
        },
        "semantic_index_ready": True,
    }
}
CURRENT_GRAPH_CAPABILITIES = {
    "inputs": {"index_repository.skip_report": True},
    "outputs": {
        "index_identity": {
            "supported": True,
            "schema_version": 1,
            "fields": REQUIRED_IDENTITY_FIELDS,
        },
        "graph_status_ready": True,
    },
    "side_effects": {"index_repository.writes_architecture_report": True},
}
READY_SEARCH_CAPABILITIES = {
    "outputs": {
        "index_identity": {
            "supported": True,
            "schema_version": 1,
            "fields": REQUIRED_IDENTITY_FIELDS,
        },
        "semantic_index_ready": True,
    }
}
READY_GRAPH_CAPABILITIES = {
    "inputs": {"index_repository.skip_report": True},
    "outputs": {
        "index_identity": {
            "supported": True,
            "schema_version": 1,
            "fields": REQUIRED_IDENTITY_FIELDS,
        },
        "graph_status_ready": True,
    },
    "side_effects": {"index_repository.writes_architecture_report": True},
}
READINESS_REQUIREMENTS = {
    "index_identity": {
        "schema_version": 1,
        "required_fields": REQUIRED_IDENTITY_FIELDS,
        "equal_fields": EQUAL_IDENTITY_FIELDS,
    },
    "code-search": {
        "completion.success": True,
        "completion.error": "empty",
        "index_ready": True,
    },
    "code-graph": {
        "index_status.status": "ready",
        "index_repository.skip_report": True,
    },
    "readiness_evidence": {
        "schema_version": 1,
        "component_versions_match_bom": True,
        "checkout_unchanged": True,
    },
}


def code_search_version(bom: dict) -> str:
    install = bom["components"]["code-search"]["install"]
    return (
        install["tag"]
        if install["kind"] == "github-release"
        else install["revision"]
    )


class ReadinessContractTests(unittest.TestCase):
    def _copy_checkout(self, checkout: Path) -> None:
        for directory in (
            ".claude-plugin",
            "compatibility",
            "scripts",
            "skills",
        ):
            shutil.copytree(ROOT / directory, checkout / directory)
        for filename in (
            ".mcp.json",
            "component-bom.json",
            "install.sh",
            "install.ps1",
        ):
            shutil.copy2(ROOT / filename, checkout / filename)

    def _run_validator(
        self,
        checkout: Path,
        extra_environment: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess:
        environment = dict(os.environ)
        environment.pop("CODE_INTEL_READINESS_EVIDENCE_OVERRIDE", None)
        if extra_environment:
            environment.update(extra_environment)
        return subprocess.run(
            [sys.executable, "scripts/validate_plugin.py"],
            cwd=checkout,
            text=True,
            capture_output=True,
            env=environment,
            check=False,
        )

    def _write_json(self, path: Path, value: dict) -> None:
        path.write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")

    def _promote_to_valid_ready_fixture(self, checkout: Path) -> None:
        bom_path = checkout / "component-bom.json"
        bom = json.loads(bom_path.read_text(encoding="utf-8"))
        search_path = checkout / "compatibility" / "code-search-tools.json"
        graph_path = checkout / "compatibility" / "code-graph-tools.json"
        search = json.loads(search_path.read_text(encoding="utf-8"))
        graph = json.loads(graph_path.read_text(encoding="utf-8"))

        search["tested_capabilities"] = deepcopy(READY_SEARCH_CAPABILITIES)
        graph["tested_capabilities"] = deepcopy(READY_GRAPH_CAPABILITIES)
        search_schema = search["tools"]["get_index_status"]["input_schema"]
        search_schema["properties"]["project_path"] = {
            "description": "Select the indexed project explicitly.",
            "type": "string",
        }
        canonical = json.dumps(
            search_schema,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        search["tools"]["get_index_status"]["input_schema_sha256"] = hashlib.sha256(
            canonical
        ).hexdigest()
        graph_schema = graph["tools"]["index_repository"]["input_schema"]
        graph_schema["properties"]["skip_report"] = {
            "description": "Skip writing the architecture report.",
            "type": "boolean",
        }
        canonical = json.dumps(
            graph_schema,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        graph["tools"]["index_repository"]["input_schema_sha256"] = hashlib.sha256(
            canonical
        ).hexdigest()

        bom["components"]["code-search"]["tested_capabilities"] = deepcopy(
            READY_SEARCH_CAPABILITIES
        )
        bom["components"]["code-graph"]["tested_capabilities"] = deepcopy(
            READY_GRAPH_CAPABILITIES
        )
        bom["integrated_readiness"] = {
            "status": "ready",
            "reason": "Fixture attests every integrated readiness gate.",
            "requires": deepcopy(READINESS_REQUIREMENTS),
            "evidence": "compatibility/readiness-evidence.json",
        }

        repository_id = "a" * 64
        source_revision = "c" * 40
        dirty_fingerprint = "clean"
        index_generation = hashlib.sha256(
            (
                repository_id
                + "\0"
                + source_revision
                + "\0"
                + dirty_fingerprint
            ).encode("utf-8")
        ).hexdigest()
        identity = {
            "schema_version": 1,
            "repository_id": repository_id,
            "checkout_id": "b" * 64,
            "source_revision": source_revision,
            "dirty_fingerprint": dirty_fingerprint,
            "index_generation": index_generation,
            "captured_at": "2026-07-26T18:00:00Z",
        }
        graph_identity = deepcopy(identity)
        graph_identity["captured_at"] = "2026-07-26T18:00:01Z"
        evidence = {
            "schema_version": 1,
            "producer": "scripts/generate_live_readiness_evidence.py:v2",
            "evidence_mode": "promotion-candidate",
            "bom_readiness_status": "blocked",
            "components": {
                "code-search": {
                    "version": code_search_version(bom),
                    "completion": {"success": True, "error": None},
                    "index_ready": True,
                    "index_identity": deepcopy(identity),
                },
                "code-graph": {
                    "version": bom["components"]["code-graph"]["install"]["tag"],
                    "status": "ready",
                    "index_identity": graph_identity,
                },
            },
            "checkout_unchanged": True,
        }

        self._write_json(search_path, search)
        self._write_json(graph_path, graph)
        self._write_json(bom_path, bom)
        self._write_json(
            checkout / "compatibility" / "readiness-evidence.json", evidence
        )

    def _mutate_capability(
        self,
        checkout: Path,
        component: str,
        section: str,
        capability: str,
        value,
    ) -> None:
        bom_path = checkout / "component-bom.json"
        snapshot_path = (
            checkout / "compatibility" / f"{component}-tools.json"
        )
        bom = json.loads(bom_path.read_text(encoding="utf-8"))
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        snapshot["tested_capabilities"][section][capability] = deepcopy(value)
        bom["components"][component]["tested_capabilities"] = deepcopy(
            snapshot["tested_capabilities"]
        )
        self._write_json(snapshot_path, snapshot)
        self._write_json(bom_path, bom)

    def _mutate_evidence(self, checkout: Path, mutator) -> None:
        path = checkout / "compatibility" / "readiness-evidence.json"
        evidence = json.loads(path.read_text(encoding="utf-8"))
        mutator(evidence)
        self._write_json(path, evidence)

    def test_current_snapshots_and_evidence_attest_integrated_readiness(self):
        bom = json.loads((ROOT / "component-bom.json").read_text(encoding="utf-8"))
        expected = {
            "code-search": CURRENT_SEARCH_CAPABILITIES,
            "code-graph": CURRENT_GRAPH_CAPABILITIES,
        }
        for component, capabilities in expected.items():
            snapshot = json.loads(
                (ROOT / "compatibility" / f"{component}-tools.json").read_text(
                    encoding="utf-8"
                )
            )
            self.assertEqual(snapshot["tested_capabilities"], capabilities)
            self.assertEqual(
                bom["components"][component]["tested_capabilities"],
                capabilities,
            )

        readiness = bom["integrated_readiness"]
        self.assertEqual(readiness["status"], "ready")
        self.assertEqual(readiness["requires"], READINESS_REQUIREMENTS)
        self.assertEqual(
            readiness["evidence"],
            "compatibility/readiness-evidence.json",
        )
        self.assertIn(
            "committed promotion-candidate",
            readiness["reason"].lower(),
        )
        self.assertIn("trusted post-merge ci", readiness["reason"].lower())

    def test_valid_ready_fixture_passes_every_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            self._copy_checkout(checkout)
            self._promote_to_valid_ready_fixture(checkout)
            completed = self._run_validator(checkout)

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_live_override_requires_ready_validation_mode_and_ready_bom_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            self._copy_checkout(checkout)
            self._promote_to_valid_ready_fixture(checkout)
            candidate_path = (
                checkout / "compatibility" / "readiness-evidence.json"
            )
            live_path = checkout / "live-readiness-evidence.json"
            live = json.loads(candidate_path.read_text(encoding="utf-8"))
            live["evidence_mode"] = "ready-validation"
            live["bom_readiness_status"] = "ready"
            self._write_json(live_path, live)
            environment = {
                "RUNNER_TEMP": str(checkout),
                "CODE_INTEL_READINESS_EVIDENCE_OVERRIDE": str(live_path),
            }

            completed = self._run_validator(checkout, environment)
            self.assertEqual(
                completed.returncode,
                0,
                completed.stdout + completed.stderr,
            )

            live["evidence_mode"] = "promotion-candidate"
            self._write_json(live_path, live)
            completed = self._run_validator(checkout, environment)
            output = completed.stdout + completed.stderr
            self.assertEqual(completed.returncode, 1, output)
            self.assertIn("evidence_mode", output)

            live["evidence_mode"] = "ready-validation"
            live["bom_readiness_status"] = "blocked"
            self._write_json(live_path, live)
            completed = self._run_validator(checkout, environment)
            output = completed.stdout + completed.stderr
            self.assertEqual(completed.returncode, 1, output)
            self.assertIn("bom_readiness_status", output)

    def test_unsafe_ready_promotions_are_rejected(self):
        def missing_identity_field(checkout: Path) -> None:
            value = deepcopy(READY_SEARCH_CAPABILITIES["outputs"]["index_identity"])
            value["fields"] = REQUIRED_IDENTITY_FIELDS[:-1]
            self._mutate_capability(
                checkout, "code-search", "outputs", "index_identity", value
            )

        def semantic_not_ready(checkout: Path) -> None:
            self._mutate_capability(
                checkout, "code-search", "outputs", "semantic_index_ready", False
            )

        def graph_status_not_ready(checkout: Path) -> None:
            self._mutate_capability(
                checkout, "code-graph", "outputs", "graph_status_ready", False
            )

        def graph_skip_report_not_attested(checkout: Path) -> None:
            self._mutate_capability(
                checkout,
                "code-graph",
                "inputs",
                "index_repository.skip_report",
                False,
            )

        def version_mismatch(checkout: Path) -> None:
            self._mutate_evidence(
                checkout,
                lambda evidence: evidence["components"]["code-graph"].__setitem__(
                    "version", "wrong-version"
                ),
            )

        def incomplete_evidence_identity(checkout: Path) -> None:
            self._mutate_evidence(
                checkout,
                lambda evidence: evidence["components"]["code-search"][
                    "index_identity"
                ].pop("captured_at"),
            )

        def mismatched_evidence_identity(checkout: Path) -> None:
            def mutate(evidence: dict) -> None:
                identity = evidence["components"]["code-graph"]["index_identity"]
                identity["repository_id"] = "d" * 64
                identity["index_generation"] = hashlib.sha256(
                    (
                        identity["repository_id"]
                        + "\0"
                        + identity["source_revision"]
                        + "\0"
                        + identity["dirty_fingerprint"]
                    ).encode("utf-8")
                ).hexdigest()

            self._mutate_evidence(checkout, mutate)

        def checkout_changed(checkout: Path) -> None:
            self._mutate_evidence(
                checkout,
                lambda evidence: evidence.__setitem__("checkout_unchanged", False),
            )

        def semantic_evidence_not_ready(checkout: Path) -> None:
            self._mutate_evidence(
                checkout,
                lambda evidence: evidence["components"]["code-search"].__setitem__(
                    "index_ready", False
                ),
            )

        def invalid_repository_hash(checkout: Path) -> None:
            self._mutate_evidence(
                checkout,
                lambda evidence: evidence["components"]["code-search"][
                    "index_identity"
                ].__setitem__("repository_id", "not-a-sha256"),
            )

        def invalid_source_revision(checkout: Path) -> None:
            self._mutate_evidence(
                checkout,
                lambda evidence: evidence["components"]["code-search"][
                    "index_identity"
                ].__setitem__("source_revision", "main"),
            )

        def invalid_dirty_fingerprint(checkout: Path) -> None:
            self._mutate_evidence(
                checkout,
                lambda evidence: evidence["components"]["code-search"][
                    "index_identity"
                ].__setitem__("dirty_fingerprint", "dirty"),
            )

        def invalid_captured_at(checkout: Path) -> None:
            self._mutate_evidence(
                checkout,
                lambda evidence: evidence["components"]["code-search"][
                    "index_identity"
                ].__setitem__("captured_at", "2026-07-26T13:00:00-05:00"),
            )

        def invalid_generation_derivation(checkout: Path) -> None:
            self._mutate_evidence(
                checkout,
                lambda evidence: evidence["components"]["code-search"][
                    "index_identity"
                ].__setitem__("index_generation", "0" * 64),
            )

        def graph_evidence_not_ready(checkout: Path) -> None:
            self._mutate_evidence(
                checkout,
                lambda evidence: evidence["components"]["code-graph"].__setitem__(
                    "status", "stale"
                ),
            )

        def invalid_evidence_producer(checkout: Path) -> None:
            self._mutate_evidence(
                checkout,
                lambda evidence: evidence.__setitem__("producer", "unknown"),
            )

        def invalid_evidence_mode(checkout: Path) -> None:
            self._mutate_evidence(
                checkout,
                lambda evidence: evidence.__setitem__(
                    "evidence_mode", "ready-validation"
                ),
            )

        def invalid_evidence_bom_status(checkout: Path) -> None:
            self._mutate_evidence(
                checkout,
                lambda evidence: evidence.__setitem__(
                    "bom_readiness_status", "ready"
                ),
            )

        def weakened_requirements(checkout: Path) -> None:
            bom_path = checkout / "component-bom.json"
            bom = json.loads(bom_path.read_text(encoding="utf-8"))
            del bom["integrated_readiness"]["requires"]["readiness_evidence"][
                "checkout_unchanged"
            ]
            self._write_json(bom_path, bom)

        cases = {
            "identity capability incomplete": (
                missing_identity_field,
                "index_identity",
            ),
            "semantic readiness capability false": (
                semantic_not_ready,
                "semantic_index_ready",
            ),
            "graph status capability false": (
                graph_status_not_ready,
                "graph_status_ready",
            ),
            "skip-report capability false": (
                graph_skip_report_not_attested,
                "skip_report",
            ),
            "evidence version mismatch": (version_mismatch, "version"),
            "evidence identity incomplete": (
                incomplete_evidence_identity,
                "index_identity",
            ),
            "evidence identity mismatch": (
                mismatched_evidence_identity,
                "identities",
            ),
            "checkout changed": (checkout_changed, "checkout_unchanged"),
            "invalid repository hash": (
                invalid_repository_hash,
                "repository_id",
            ),
            "invalid source revision": (
                invalid_source_revision,
                "source_revision",
            ),
            "invalid dirty fingerprint": (
                invalid_dirty_fingerprint,
                "dirty_fingerprint",
            ),
            "invalid capture timestamp": (invalid_captured_at, "captured_at"),
            "invalid generation derivation": (
                invalid_generation_derivation,
                "index_generation",
            ),
            "semantic evidence not ready": (
                semantic_evidence_not_ready,
                "index_ready",
            ),
            "graph evidence not ready": (
                graph_evidence_not_ready,
                "status",
            ),
            "evidence producer invalid": (
                invalid_evidence_producer,
                "producer",
            ),
            "committed evidence mode invalid": (
                invalid_evidence_mode,
                "evidence_mode",
            ),
            "committed evidence BOM status invalid": (
                invalid_evidence_bom_status,
                "bom_readiness_status",
            ),
            "requirements weakened": (weakened_requirements, "requires"),
        }

        for label, (mutator, expected_error) in cases.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                checkout = Path(tmp)
                self._copy_checkout(checkout)
                self._promote_to_valid_ready_fixture(checkout)
                mutator(checkout)
                completed = self._run_validator(checkout)
                output = completed.stdout + completed.stderr
                self.assertEqual(completed.returncode, 1, output)
                self.assertIn(expected_error, output)

    def test_installers_and_docs_report_current_integrated_readiness(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        normalized_readme = " ".join(readme.split())
        compatibility = (ROOT / "compatibility" / "README.md").read_text(
            encoding="utf-8"
        )
        normalized_compatibility = " ".join(compatibility.split())
        bom = json.loads(
            (ROOT / "component-bom.json").read_text(encoding="utf-8")
        )
        skill = (ROOT / "skills" / "index-repo" / "SKILL.md").read_text(
            encoding="utf-8"
        )
        shell = (ROOT / "install.sh").read_text(encoding="utf-8")
        powershell = (ROOT / "install.ps1").read_text(encoding="utf-8")

        self.assertIn("INTEGRATED READINESS: READY", readme)
        self.assertIn("This runs both semantic and structural indexing", readme)
        self.assertIn("`voyage` maps to `voyage-4-large`", normalized_readme)
        self.assertIn("CODE_GRAPH_SKIP_EMBEDDINGS", readme)
        self.assertNotIn("code-graph is fully local", readme)
        self.assertIn("promotion-candidate", normalized_compatibility)
        self.assertIn("ready-validation", normalized_compatibility)
        self.assertIn("do not independently prove", normalized_compatibility)
        self.assertNotIn(
            "using separately verified exact components",
            normalized_compatibility,
        )
        self.assertIn(
            "trusted post-merge CI",
            bom["integrated_readiness"]["reason"],
        )
        self.assertIn("current BOM attests", skill)
        for installer in (shell, powershell):
            self.assertIn("INTEGRATED READINESS: READY", installer)
            self.assertIn("3. Index a repo", installer)


if __name__ == "__main__":
    unittest.main()
