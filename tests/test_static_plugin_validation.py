"""Acceptance tests for repository-local plugin contract validation."""

from pathlib import Path
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from released_bom import pending_bom, write_released_checkout  # noqa: E402

RELEASE_INSTALL_FIXTURE = (
    ROOT / "tests" / "fixtures" / "code-search-release-install.json"
)


class StaticPluginValidationTests(unittest.TestCase):
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
        # Start from a released fixture with deterministic fake digests so the
        # strict artifact policy under test does not depend on the live pins.
        write_released_checkout(checkout)

    def _copy_pending_checkout(self, checkout: Path) -> None:
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
        bom_path = checkout / "component-bom.json"
        bom = json.loads(bom_path.read_text(encoding="utf-8"))
        bom_path.write_text(json.dumps(pending_bom(bom)), encoding="utf-8")
        shutil.rmtree(checkout / "compatibility" / "attestations", ignore_errors=True)
        (checkout / "compatibility" / "readiness-evidence.json").unlink(missing_ok=True)
        for component in ("code-search", "code-graph"):
            self._rebind_component_descriptor(checkout, component)

    def _run_validator(self, checkout: Path) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "scripts/validate_plugin.py"],
            cwd=checkout,
            text=True,
            capture_output=True,
            check=False,
        )

    def test_validator_reads_the_explicit_candidate_bom_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            self._copy_checkout(checkout)
            candidate = checkout / "candidate-bom.json"
            candidate.write_text('{"not": "a component BOM"}\n', encoding="utf-8")

            completed = subprocess.run(
                [
                    sys.executable,
                    "scripts/validate_plugin.py",
                    "--component-bom",
                    str(candidate),
                ],
                cwd=checkout,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(
            completed.returncode,
            1,
            completed.stdout + completed.stderr,
        )
        self.assertIn("candidate-bom.json", completed.stdout)

    def test_committed_promoted_bom_validates_and_reports_ready(self):
        completed = self._run_validator(ROOT)

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertNotIn("promotion_state=", completed.stdout)
        bom = json.loads((ROOT / "component-bom.json").read_text(encoding="utf-8"))
        self.assertNotIn("promotion_state", bom)
        self.assertNotIn("promotion_reason", bom)
        self.assertEqual(bom["integrated_readiness"]["status"], "ready")
        self.assertEqual(
            bom["integrated_readiness"]["evidence"],
            "compatibility/readiness-evidence.json",
        )
        for component in ("code-search", "code-graph"):
            install = bom["components"][component]["install"]
            self.assertTrue(install["repository"].startswith("brandyn-s/"))
            self.assertRegex(install["source_revision"], r"^[0-9a-f]{40}$")
        self.assertNotIn('"pending"', json.dumps(bom["components"]))
        self.assertTrue(
            (
                ROOT / "compatibility" / "attestations" / "code-graph-v0.9.0-provenance.jsonl"
            ).is_file()
        )
        self.assertTrue((ROOT / "compatibility" / "readiness-evidence.json").is_file())

    def test_pending_bom_requires_pending_readiness_and_a_reason(self):
        mutations = {
            "ready status": (
                lambda bom: bom["integrated_readiness"].__setitem__("status", "ready"),
                "must be pending",
            ),
            "missing reason": (
                lambda bom: bom.pop("promotion_reason"),
                "promotion_reason",
            ),
            "unknown state": (
                lambda bom: bom.__setitem__("promotion_state", "pending-later"),
                "unknown promotion_state",
            ),
            "evidence path": (
                lambda bom: bom["integrated_readiness"].__setitem__(
                    "evidence", "compatibility/readiness-evidence.json"
                ),
                "cannot reference readiness evidence",
            ),
        }
        for label, (mutate, expected) in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                checkout = Path(tmp)
                self._copy_pending_checkout(checkout)
                bom_path = checkout / "component-bom.json"
                bom = json.loads(bom_path.read_text(encoding="utf-8"))
                mutate(bom)
                bom_path.write_text(json.dumps(bom), encoding="utf-8")
                for component in ("code-search", "code-graph"):
                    self._rebind_component_descriptor(checkout, component)

                completed = self._run_validator(checkout)

            self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
            self.assertIn(expected, completed.stdout)

    def test_released_bom_rejects_the_pending_placeholder_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            self._copy_checkout(checkout)
            bom_path = checkout / "component-bom.json"
            bom = json.loads(bom_path.read_text(encoding="utf-8"))
            bom["components"]["code-search"]["install"]["asset"]["sha256"] = "pending"
            bom_path.write_text(json.dumps(bom), encoding="utf-8")
            self._rebind_component_descriptor(checkout, "code-search")

            completed = self._run_validator(checkout)

        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("sha256", completed.stdout.lower())

    def test_validator_accepts_the_pinned_scip_generator_contracts(self):
        completed = self._run_validator(ROOT)

        self.assertEqual(
            completed.returncode,
            0,
            completed.stdout + completed.stderr,
        )
        bom = json.loads(
            (ROOT / "component-bom.json").read_text(encoding="utf-8")
        )
        generator = bom["precision_generators"]["go-scip"]
        self.assertEqual(generator["repository"], "scip-code/scip-go")
        self.assertEqual(generator["tag"], "v0.2.7")
        self.assertEqual(generator["version_output"], "0.2.7")
        self.assertEqual(
            set(generator["assets"]),
            {
                "darwin-arm64",
                "linux-amd64",
                "linux-arm64",
            },
        )
        for asset in generator["assets"].values():
            self.assertRegex(asset["archive_sha256"], r"^[0-9a-f]{64}$")
            self.assertRegex(asset["binary_sha256"], r"^[0-9a-f]{64}$")

        typescript = bom["precision_generators"]["typescript-scip"]
        self.assertEqual(
            typescript["package"], "@sourcegraph/scip-typescript"
        )
        self.assertEqual(typescript["version_output"], "0.4.0")
        self.assertEqual(typescript["supported_node_majors"], [22])
        self.assertRegex(typescript["lockfile_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(typescript["entrypoint_sha256"], r"^[0-9a-f]{64}$")
        self.assertEqual(
            set(typescript["node_runtime"]["assets"]),
            {
                "darwin-amd64",
                "darwin-arm64",
                "linux-amd64",
                "linux-arm64",
                "windows-amd64",
            },
        )

    def test_validator_rejects_unpinned_go_scip_binary_digest(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            self._copy_checkout(checkout)
            bom_path = checkout / "component-bom.json"
            bom = json.loads(bom_path.read_text(encoding="utf-8"))
            del bom["precision_generators"]["go-scip"]["assets"][
                "darwin-arm64"
            ]["binary_sha256"]
            bom_path.write_text(json.dumps(bom), encoding="utf-8")

            completed = self._run_validator(checkout)

        self.assertEqual(completed.returncode, 1)
        self.assertIn("go-scip", completed.stdout)
        self.assertIn("binary_sha256", completed.stdout)

    def _rebind_component_descriptor(
        self,
        checkout: Path,
        component: str,
    ) -> None:
        bom_path = checkout / "component-bom.json"
        bom = json.loads(bom_path.read_text(encoding="utf-8"))
        install = bom["components"][component]["install"]
        digest = hashlib.sha256(
            json.dumps(
                install,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        snapshot_path = (
            checkout / "compatibility" / f"{component}-tools.json"
        )
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        snapshot["source"]["install_descriptor_sha256"] = digest
        snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
        evidence_path = (
            checkout / "compatibility" / "readiness-evidence.json"
        )
        if evidence_path.is_file():
            evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence["components"][component][
                "install_descriptor_sha256"
            ] = digest
            evidence_path.write_text(json.dumps(evidence), encoding="utf-8")

    def _rewrite_tool_schema(
        self,
        checkout: Path,
        component: str,
        tool_name: str,
        schema: dict,
    ) -> dict:
        snapshot_path = (
            checkout / "compatibility" / f"{component}-tools.json"
        )
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
        tool = snapshot["tools"][tool_name]
        tool["input_schema"] = schema
        canonical = json.dumps(
            schema,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        tool["input_schema_sha256"] = hashlib.sha256(canonical).hexdigest()
        snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
        return snapshot

    def test_validator_rejects_skill_tool_outside_tested_snapshot(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            self._copy_checkout(checkout)

            skill = checkout / "skills" / "code-explore" / "SKILL.md"
            skill.write_text(
                skill.read_text(encoding="utf-8")
                + "\n`mcp__code-graph__not_in_tested_release`\n",
                encoding="utf-8",
            )
            completed = subprocess.run(
                [sys.executable, "scripts/validate_plugin.py"],
                cwd=checkout,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("not_in_tested_release", completed.stdout)

    def test_validator_rejects_ready_bom_when_graph_cannot_suppress_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            self._copy_checkout(checkout)
            snapshot_path = (
                checkout / "compatibility" / "code-graph-tools.json"
            )
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            schema = snapshot["tools"]["index_repository"]["input_schema"]
            del schema["properties"]["skip_report"]
            snapshot = self._rewrite_tool_schema(
                checkout, "code-graph", "index_repository", schema
            )
            snapshot["tested_capabilities"]["inputs"][
                "index_repository.skip_report"
            ] = False
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
            bom_path = checkout / "component-bom.json"
            bom = json.loads(bom_path.read_text(encoding="utf-8"))
            bom["components"]["code-graph"]["tested_capabilities"] = snapshot[
                "tested_capabilities"
            ]
            bom["integrated_readiness"]["status"] = "ready"
            bom_path.write_text(json.dumps(bom), encoding="utf-8")

            completed = subprocess.run(
                [sys.executable, "scripts/validate_plugin.py"],
                cwd=checkout,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("skip_report", completed.stdout)
        self.assertIn("blocked", completed.stdout)

    def test_validator_rejects_missing_pinned_asset_checksum(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            self._copy_checkout(checkout)
            bom_path = checkout / "component-bom.json"
            bom = json.loads(bom_path.read_text(encoding="utf-8"))
            del bom["components"]["code-graph"]["install"]["assets"]["linux-amd64"][
                "sha256"
            ]
            bom_path.write_text(json.dumps(bom), encoding="utf-8")

            completed = subprocess.run(
                [sys.executable, "scripts/validate_plugin.py"],
                cwd=checkout,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("linux-amd64", completed.stdout)
        self.assertIn("sha256", completed.stdout.lower())

    def test_validator_rejects_missing_vendored_graph_attestation_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            self._copy_checkout(checkout)
            bundle = (
                checkout
                / "compatibility"
                / "attestations"
                / "code-graph-v0.9.0-provenance.jsonl"
            )
            bundle.unlink()

            completed = self._run_validator(checkout)

        self.assertEqual(
            completed.returncode,
            1,
            completed.stdout + completed.stderr,
        )
        self.assertIn("attestation bundle", completed.stdout)
        self.assertIn("missing", completed.stdout)

    def test_validator_rejects_modified_vendored_graph_attestation_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            self._copy_checkout(checkout)
            bundle = (
                checkout
                / "compatibility"
                / "attestations"
                / "code-graph-v0.9.0-provenance.jsonl"
            )
            bundle.write_text('{"tampered":true}\n', encoding="utf-8")

            completed = self._run_validator(checkout)

        self.assertEqual(
            completed.returncode,
            1,
            completed.stdout + completed.stderr,
        )
        self.assertIn("attestation bundle", completed.stdout)
        self.assertIn("sha256", completed.stdout.lower())

    def test_validator_accepts_pinned_code_search_release_wheel(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            self._copy_checkout(checkout)
            bom_path = checkout / "component-bom.json"
            bom = json.loads(bom_path.read_text(encoding="utf-8"))
            bom["components"]["code-search"]["install"] = json.loads(
                RELEASE_INSTALL_FIXTURE.read_text(encoding="utf-8")
            )
            bom_path.write_text(json.dumps(bom), encoding="utf-8")

            snapshot_path = (
                checkout / "compatibility" / "code-search-tools.json"
            )
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            snapshot["source"] = {
                "install_descriptor_sha256": hashlib.sha256(
                    json.dumps(
                        bom["components"]["code-search"]["install"],
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=False,
                        allow_nan=False,
                    ).encode("utf-8")
                ).hexdigest(),
                "kind": "github-release",
                "version": "v0.0.0",
            }
            snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

            completed = self._run_validator(checkout)

        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)

    def test_validator_rejects_snapshot_from_different_install_descriptor(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            self._copy_checkout(checkout)
            bom_path = checkout / "component-bom.json"
            bom = json.loads(bom_path.read_text(encoding="utf-8"))
            bom["components"]["code-search"]["install"]["source_revision"] = (
                "f" * 40
            )
            bom_path.write_text(json.dumps(bom), encoding="utf-8")

            completed = self._run_validator(checkout)

        self.assertEqual(
            completed.returncode,
            1,
            completed.stdout + completed.stderr,
        )
        self.assertIn("install descriptor", completed.stdout)

    def test_validator_rejects_unknown_install_descriptor_fields(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            self._copy_checkout(checkout)
            bom_path = checkout / "component-bom.json"
            bom = json.loads(bom_path.read_text(encoding="utf-8"))
            bom["components"]["code-search"]["install"]["attestation"][
                "unexpected_policy"
            ] = True
            bom_path.write_text(json.dumps(bom), encoding="utf-8")
            self._rebind_component_descriptor(checkout, "code-search")

            completed = self._run_validator(checkout)

        self.assertEqual(
            completed.returncode,
            1,
            completed.stdout + completed.stderr,
        )
        self.assertIn("keys must exactly match", completed.stdout)

    def test_validator_rejects_weakened_search_artifact_policy(self):
        def wrong_wheel(install: dict) -> None:
            install["asset"]["name"] = (
                "code_search_mcp-9.9.9-py3-none-any.whl"
            )

        def missing_checksums(install: dict) -> None:
            install.pop("checksums")

        def wrong_bundle(install: dict) -> None:
            install["attestation"]["bundle"]["name"] = "other.jsonl"

        def allow_self_hosted(install: dict) -> None:
            install["attestation"]["deny_self_hosted_runners"] = False

        mutations = {
            "mislabeled wheel": (wrong_wheel, "wheel"),
            "missing checksums": (missing_checksums, "checksums"),
            "mislabeled provenance": (wrong_bundle, "bundle"),
            "self-hosted provenance": (
                allow_self_hosted,
                "deny_self_hosted_runners",
            ),
        }
        for label, (mutate, expected) in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                checkout = Path(tmp)
                self._copy_checkout(checkout)
                bom_path = checkout / "component-bom.json"
                bom = json.loads(bom_path.read_text(encoding="utf-8"))
                mutate(bom["components"]["code-search"]["install"])
                bom_path.write_text(json.dumps(bom), encoding="utf-8")
                self._rebind_component_descriptor(checkout, "code-search")

                completed = self._run_validator(checkout)

            self.assertEqual(
                completed.returncode,
                1,
                completed.stdout + completed.stderr,
            )
            self.assertIn(expected, completed.stdout)

    def test_validator_rejects_weakened_graph_artifact_policy(self):
        def wrong_revision(install: dict) -> None:
            install["source_revision"] = "not-a-commit"

        def missing_checksums(install: dict) -> None:
            install.pop("checksums")

        def wrong_signer(install: dict) -> None:
            install["attestation"]["signer_workflow"] = (
                "brandyn-s/code-graph/.github/workflows/other.yml"
            )

        def wrong_source_ref(install: dict) -> None:
            install["attestation"]["source_ref"] = "refs/tags/copied"

        def allow_self_hosted(install: dict) -> None:
            install["attestation"]["deny_self_hosted_runners"] = False

        mutations = {
            "wrong source revision": (wrong_revision, "source_revision"),
            "missing checksums": (missing_checksums, "checksums"),
            "wrong signer": (wrong_signer, "signer_workflow"),
            "wrong source ref": (wrong_source_ref, "source_ref"),
            "self-hosted provenance": (
                allow_self_hosted,
                "deny_self_hosted_runners",
            ),
        }
        for label, (mutate, expected) in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                checkout = Path(tmp)
                self._copy_checkout(checkout)
                bom_path = checkout / "component-bom.json"
                bom = json.loads(bom_path.read_text(encoding="utf-8"))
                mutate(bom["components"]["code-graph"]["install"])
                bom_path.write_text(json.dumps(bom), encoding="utf-8")
                self._rebind_component_descriptor(checkout, "code-graph")

                completed = self._run_validator(checkout)

            self.assertEqual(
                completed.returncode,
                1,
                completed.stdout + completed.stderr,
            )
            self.assertIn(expected, completed.stdout)

    def test_validator_rejects_weakened_code_search_release_policy(self):
        mutations = {
            "unversioned tag": ("tag", "release"),
            "different signer": (
                "signer_workflow",
                (
                    "brandyn-s/code-search/"
                    ".github/workflows/other.yml"
                ),
            ),
            "non-main source": ("source_ref", "refs/tags/v0.0.0"),
        }
        for label, (field, value) in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                checkout = Path(tmp)
                self._copy_checkout(checkout)
                bom_path = checkout / "component-bom.json"
                bom = json.loads(bom_path.read_text(encoding="utf-8"))
                release_install = json.loads(
                    RELEASE_INSTALL_FIXTURE.read_text(encoding="utf-8")
                )
                if field in {"signer_workflow", "source_ref"}:
                    release_install["attestation"][field] = value
                else:
                    release_install[field] = value
                bom["components"]["code-search"]["install"] = release_install
                bom_path.write_text(json.dumps(bom), encoding="utf-8")

                snapshot_path = (
                    checkout / "compatibility" / "code-search-tools.json"
                )
                snapshot = json.loads(
                    snapshot_path.read_text(encoding="utf-8")
                )
                snapshot["source"] = {
                    "kind": "github-release",
                    "version": release_install["tag"],
                }
                snapshot_path.write_text(
                    json.dumps(snapshot),
                    encoding="utf-8",
                )

                completed = self._run_validator(checkout)

            self.assertEqual(
                completed.returncode,
                1,
                completed.stdout + completed.stderr,
            )

    def test_validator_rejects_non_string_search_project_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            self._copy_checkout(checkout)
            snapshot_path = (
                checkout / "compatibility" / "code-search-tools.json"
            )
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            schema = snapshot["tools"]["get_index_status"]["input_schema"]
            schema["properties"]["project_path"] = {"type": "integer"}
            self._rewrite_tool_schema(
                checkout, "code-search", "get_index_status", schema
            )

            completed = self._run_validator(checkout)

        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("project_path", completed.stdout)
        self.assertIn("optional string", completed.stdout)

    def test_validator_accepts_nullable_optional_search_project_path(self):
        representations = {
            "anyOf": {
                "anyOf": [{"type": "string"}, {"type": "null"}],
                "default": None,
            },
            "type-list": {
                "type": ["string", "null"],
                "default": None,
            },
        }
        for label, property_schema in representations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                checkout = Path(tmp)
                self._copy_checkout(checkout)
                snapshot_path = (
                    checkout / "compatibility" / "code-search-tools.json"
                )
                snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
                schema = snapshot["tools"]["get_index_status"]["input_schema"]
                schema["properties"]["project_path"] = property_schema
                self._rewrite_tool_schema(
                    checkout, "code-search", "get_index_status", schema
                )

                completed = self._run_validator(checkout)

            self.assertEqual(
                completed.returncode, 0, completed.stdout + completed.stderr
            )

    def test_validator_rejects_nullable_project_path_with_extra_type(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            self._copy_checkout(checkout)
            snapshot_path = (
                checkout / "compatibility" / "code-search-tools.json"
            )
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            schema = snapshot["tools"]["get_index_status"]["input_schema"]
            schema["properties"]["project_path"] = {
                "anyOf": [
                    {"type": "string"},
                    {"type": "integer"},
                    {"type": "null"},
                ]
            }
            self._rewrite_tool_schema(
                checkout, "code-search", "get_index_status", schema
            )

            completed = self._run_validator(checkout)

        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("project_path", completed.stdout)
        self.assertIn("optional string", completed.stdout)

    def test_validator_rejects_constraints_that_negate_optional_capabilities(self):
        mutations = {
            "project-path-impossible": (
                "code-search",
                "get_index_status",
                "project_path",
                {"type": "string", "not": {"type": "string"}},
                "project_path",
            ),
            "skip-report-true-forbidden": (
                "code-graph",
                "index_repository",
                "skip_report",
                {
                    "type": ["boolean", "null"],
                    "not": {"const": True},
                },
                "skip_report",
            ),
        }
        for label, mutation in mutations.items():
            component, tool_name, property_name, property_schema, expected = mutation
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                checkout = Path(tmp)
                self._copy_checkout(checkout)
                snapshot_path = (
                    checkout / "compatibility" / f"{component}-tools.json"
                )
                snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
                schema = snapshot["tools"][tool_name]["input_schema"]
                schema["properties"][property_name] = property_schema
                self._rewrite_tool_schema(
                    checkout, component, tool_name, schema
                )

                completed = self._run_validator(checkout)

            self.assertEqual(
                completed.returncode, 1, completed.stdout + completed.stderr
            )
            self.assertIn(expected, completed.stdout)

    def test_validator_rejects_parent_constraint_that_requires_project_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            self._copy_checkout(checkout)
            snapshot_path = (
                checkout / "compatibility" / "code-search-tools.json"
            )
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            schema = snapshot["tools"]["get_index_status"]["input_schema"]
            schema["allOf"] = [{"required": ["project_path"]}]
            self._rewrite_tool_schema(
                checkout, "code-search", "get_index_status", schema
            )

            completed = self._run_validator(checkout)

        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("project_path", completed.stdout)

    def test_validator_rejects_project_path_on_a_nonobject_input(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            self._copy_checkout(checkout)
            snapshot_path = (
                checkout / "compatibility" / "code-search-tools.json"
            )
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            schema = snapshot["tools"]["get_index_status"]["input_schema"]
            schema["type"] = "string"
            schema["properties"]["project_path"] = {"type": "string"}
            self._rewrite_tool_schema(
                checkout, "code-search", "get_index_status", schema
            )

            completed = self._run_validator(checkout)

        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("project_path", completed.stdout)
        self.assertIn("optional string", completed.stdout)

    def test_validator_rejects_malformed_or_required_graph_skip_report(self):
        mutations = {
            "wrong-type": lambda schema: schema["properties"].update(
                {"skip_report": {"type": "string"}}
            ),
            "required": lambda schema: (
                schema["properties"].update({"skip_report": {"type": "boolean"}}),
                schema.setdefault("required", []).append("skip_report"),
            ),
            "non-object": lambda schema: (
                schema.update({"type": "string"}),
                schema["properties"].update(
                    {"skip_report": {"type": "boolean"}}
                ),
            ),
        }
        for label, mutate in mutations.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                checkout = Path(tmp)
                self._copy_checkout(checkout)
                snapshot_path = (
                    checkout / "compatibility" / "code-graph-tools.json"
                )
                snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
                schema = snapshot["tools"]["index_repository"]["input_schema"]
                mutate(schema)
                snapshot = self._rewrite_tool_schema(
                    checkout, "code-graph", "index_repository", schema
                )
                snapshot["tested_capabilities"]["inputs"][
                    "index_repository.skip_report"
                ] = True
                snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
                bom_path = checkout / "component-bom.json"
                bom = json.loads(bom_path.read_text(encoding="utf-8"))
                bom["components"]["code-graph"]["tested_capabilities"] = snapshot[
                    "tested_capabilities"
                ]
                bom_path.write_text(json.dumps(bom), encoding="utf-8")

                completed = self._run_validator(checkout)

            self.assertEqual(
                completed.returncode, 1, completed.stdout + completed.stderr
            )
            self.assertIn("skip_report", completed.stdout)
            self.assertIn("optional boolean", completed.stdout)

    def test_ready_contract_requires_optional_search_project_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            checkout = Path(tmp)
            self._copy_checkout(checkout)
            snapshot_path = (
                checkout / "compatibility" / "code-search-tools.json"
            )
            snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
            schema = snapshot["tools"]["get_index_status"]["input_schema"]
            del schema["properties"]["project_path"]
            self._rewrite_tool_schema(
                checkout, "code-search", "get_index_status", schema
            )
            bom_path = checkout / "component-bom.json"
            bom = json.loads(bom_path.read_text(encoding="utf-8"))
            bom["integrated_readiness"]["status"] = "ready"
            bom_path.write_text(json.dumps(bom), encoding="utf-8")

            completed = self._run_validator(checkout)

        self.assertEqual(completed.returncode, 1, completed.stdout + completed.stderr)
        self.assertIn("project_path", completed.stdout)


if __name__ == "__main__":
    unittest.main()
