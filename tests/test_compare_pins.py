"""Tests for balanced calibration pins and the external June pin reference."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
COMPARE = ROOT / "bench" / "compare"
BUILD_PIN = COMPARE / "build_pin.py"


class ComparisonPinTests(unittest.TestCase):
    def _git_audit_fixture(self, directory: Path) -> tuple[Path, Path, dict]:
        repository_root = directory / "repositories"
        repository = repository_root / "example" / "public"
        repository.mkdir(parents=True)
        subprocess.run(["git", "init", "-q", repository], check=True)
        commit = [
            "git",
            "-C",
            repository,
            "-c",
            "user.email=fixture@example.invalid",
            "-c",
            "user.name=Fixture",
            "commit",
        ]
        subprocess.run(
            [*commit, "--allow-empty", "-qm", "base"],
            check=True,
        )
        base_commit = subprocess.check_output(
            ["git", "-C", repository, "rev-parse", "HEAD"],
            text=True,
        ).strip()
        target = repository / "src" / "target.py"
        target.parent.mkdir()
        target.write_text(
            "def target():\n    return True\n",
            encoding="utf-8",
        )
        subprocess.run(
            ["git", "-C", repository, "add", "src/target.py"],
            check=True,
        )
        subprocess.run([*commit, "-qm", "head"], check=True)
        head_commit = subprocess.check_output(
            ["git", "-C", repository, "rev-parse", "HEAD"],
            text=True,
        ).strip()
        arguments = {
            "case_id": "example__public-1",
            "repository": "example/public",
            "base_commit": base_commit,
            "head_commit": head_commit,
            "oracle": {
                "files": ["src/target.py"],
                "classes": [],
                "functions": ["target"],
            },
            "repository_root": repository_root,
        }
        return repository_root, repository, arguments

    def test_build_pin_import_does_not_require_fcntl(self):
        script = """
import builtins

real_import = builtins.__import__

def import_without_fcntl(name, *args, **kwargs):
    if name == "fcntl":
        raise ImportError("fcntl is unavailable")
    return real_import(name, *args, **kwargs)

builtins.__import__ = import_without_fcntl
import bench.compare.build_pin
import bench.compare.run
import bench.compare.provenance
from pathlib import Path
import tempfile

with tempfile.TemporaryDirectory() as tmp:
    directory = Path(tmp).resolve()
    try:
        bench.compare.build_pin.write_prepared_june_artifacts(
            [],
            github_pr_cache=directory / "cache",
            repository_root=directory / "repositories",
            output=directory / "prepared.json",
            quarantine_report=directory / "quarantine.json",
            external_pin_sha256="a" * 64,
            recorded_order_sha256="b" * 64,
            parquet_sha256="c" * 64,
        )
    except bench.compare.build_pin.PinError as exc:
        assert "requires POSIX fcntl locking" in str(exc), str(exc)
    else:
        raise AssertionError("artifact publication did not require a safe lock")

    ledger = bench.compare.provenance.AppendOnlyLedger(
        directory / "cases.jsonl"
    )
    try:
        ledger.append({"stable_key": "case-1"})
    except bench.compare.provenance.ProvenanceError as exc:
        assert "requires POSIX fcntl locking" in str(exc), str(exc)
    else:
        raise AssertionError("ledger mutation did not require a safe lock")

print("compare-modules-imported")
"""
        result = subprocess.run(
            [sys.executable, "-c", script],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(
            result.returncode,
            0,
            result.stdout + result.stderr,
        )
        self.assertEqual(result.stdout, "compare-modules-imported\n")

    def test_artifact_lock_rejects_incomplete_platform_module(self):
        import builtins
        from types import SimpleNamespace
        from unittest import mock

        from bench.compare import build_pin

        real_import = builtins.__import__

        def import_incomplete_fcntl(name, *args, **kwargs):
            if name == "fcntl":
                return SimpleNamespace()
            return real_import(name, *args, **kwargs)

        with (
            mock.patch(
                "builtins.__import__",
                side_effect=import_incomplete_fcntl,
            ),
            self.assertRaisesRegex(
                build_pin.PinError,
                "locking.*incomplete",
            ),
        ):
            build_pin._artifact_lock_module()

    def test_artifact_publication_checks_dir_fd_capability_before_use(self):
        from unittest import mock

        from bench.compare import build_pin

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve()
            with (
                mock.patch.object(build_pin.os, "supports_dir_fd", set()),
                mock.patch.object(
                    build_pin.os,
                    "open",
                    side_effect=AssertionError("dir_fd operation attempted"),
                ) as opened,
                self.assertRaisesRegex(
                    build_pin.PinError,
                    "dir_fd.*unavailable",
                ),
            ):
                build_pin.write_prepared_june_artifacts(
                    [],
                    github_pr_cache=directory / "cache",
                    repository_root=directory / "repositories",
                    output=directory / "prepared.json",
                    quarantine_report=directory / "quarantine.json",
                    external_pin_sha256="a" * 64,
                    recorded_order_sha256="b" * 64,
                    parquet_sha256="c" * 64,
                )
            opened.assert_not_called()

    def test_artifact_absence_check_rejects_normalized_existing_alias(self):
        import os
        from unittest import mock

        from bench.compare import build_pin

        aliases = {
            "case": ("Artifact.json", "artifact.JSON", True),
            "unicode": (
                "résumé.json",
                "re\u0301sume\u0301.json",
                True,
            ),
            "normal": ("other.json", "artifact.json", False),
        }
        with tempfile.TemporaryDirectory() as tmp:
            descriptor = os.open(tmp, os.O_RDONLY)
            try:
                for label, (existing, requested, rejected) in aliases.items():
                    with (
                        self.subTest(label=label),
                        mock.patch.object(
                            build_pin.os,
                            "stat",
                            side_effect=FileNotFoundError,
                        ),
                        mock.patch.object(
                            build_pin.os,
                            "listdir",
                            return_value=[existing],
                        ),
                        mock.patch.object(
                            build_pin,
                            "_require_directory_fd_capabilities",
                        ),
                    ):
                        if rejected:
                            with self.assertRaisesRegex(
                                build_pin.PinError,
                                "already exists",
                            ):
                                build_pin._require_absent_artifact(
                                    descriptor,
                                    requested,
                                    "output",
                                )
                        else:
                            build_pin._require_absent_artifact(
                                descriptor,
                                requested,
                                "output",
                            )
            finally:
                os.close(descriptor)

    def test_build_n40_parses_and_hashes_each_single_no_follow_snapshot(self):
        import argparse
        from unittest import mock

        from bench.compare import build_pin

        source_bytes = b'{"cases":[],"schema_version":1}\n'
        audit_bytes = b'{"cases":[],"schema_version":1}\n'
        replacement = b'{"replaced":true,"schema_version":1}\n'
        pin = {
            "cases": [],
            "label_audit": {"quarantined": []},
        }
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            source_path = directory / "source.json"
            audit_path = directory / "audit.json"
            output_path = directory / "pin.json"
            source_path.write_bytes(source_bytes)
            audit_path.write_bytes(audit_bytes)
            real_load_object = build_pin.load_object

            def load_then_swap(path):
                loaded = real_load_object(path)
                path.write_bytes(replacement)
                return loaded

            arguments = argparse.Namespace(
                command="build-n40",
                source=source_path,
                source_sha256=hashlib.sha256(source_bytes).hexdigest(),
                source_repository="czlll/Loc-Bench_V1",
                source_revision="b" * 40,
                source_path="data/public-labels.json",
                audit_evidence=audit_path,
                audit_evidence_sha256=hashlib.sha256(audit_bytes).hexdigest(),
                audit_evidence_path="data/public-audit-evidence.json",
                repository_root=directory / "repositories",
                seed=42,
                output=output_path,
                check=False,
            )
            with (
                mock.patch.object(build_pin, "parse_args", return_value=arguments),
                mock.patch.object(
                    build_pin,
                    "load_object",
                    side_effect=load_then_swap,
                ),
                mock.patch.object(
                    build_pin,
                    "_read_input_snapshot",
                    wraps=build_pin._read_input_snapshot,
                ) as read_snapshot,
                mock.patch.object(
                    build_pin,
                    "build_balanced_pin",
                    return_value=pin,
                ) as build,
                mock.patch.object(build_pin, "atomic_write_json"),
            ):
                status = build_pin.main([])

        self.assertEqual(status, 0)
        self.assertEqual(
            read_snapshot.call_args_list,
            [
                mock.call(source_path, "source"),
                mock.call(audit_path, "audit evidence"),
            ],
        )
        build.assert_called_once()
        self.assertEqual(build.call_args.args, (json.loads(source_bytes),))
        self.assertEqual(
            build.call_args.kwargs["audit_evidence"],
            json.loads(audit_bytes),
        )

    def test_pin_object_loader_normalizes_malformed_utf8(self):
        from bench.compare.build_pin import PinError, load_object

        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "invalid.json"
            source.write_bytes(b'{"schema_version":1,"value":"\\xff"}\xff')

            with self.assertRaisesRegex(
                PinError,
                "cannot load.*UTF-8|UTF-8.*cannot load",
            ):
                load_object(source)

    def test_prepare_june_refuses_artifacts_inside_plugin_repository(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve()
            result = subprocess.run(
                [
                    sys.executable,
                    str(BUILD_PIN),
                    "prepare-june",
                    "--reference",
                    str(directory / "reference.json"),
                    "--external-pin",
                    str(directory / "external.json"),
                    "--parquet",
                    str(directory / "source.parquet"),
                    "--github-pr-cache",
                    str(directory / "pr-cache"),
                    "--repository-root",
                    str(directory / "repositories"),
                    "--output",
                    str(ROOT / "forbidden-prepared-pin.json"),
                    "--quarantine-report",
                    str(directory / "quarantine.json"),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn(
            "prepare-june output must be outside the plugin repository",
            result.stderr,
        )

    def test_prepare_june_refuses_plugin_ancestry_by_filesystem_identity(self):
        from unittest import mock

        from bench.compare import build_pin

        prepared = {"schema_version": 1}
        report = {
            "schema_version": 1,
            "prepared_count": 200,
            "quarantined_count": 0,
        }
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve()
            plugin = directory / "plugin"
            (plugin / "nested").mkdir(parents=True)
            system_alias = directory / "system-alias"
            system_alias.symlink_to(plugin, target_is_directory=True)
            paths = {
                "lexical-alias": plugin / "artifacts-a" / "prepared.json",
                "system-alias": (
                    system_alias / "artifacts-b" / "prepared.json"
                ),
            }

            def safe_alias(candidate, _metadata):
                if candidate == system_alias:
                    return plugin
                return None

            for label, output in paths.items():
                with (
                    self.subTest(label=label),
                    mock.patch.object(
                        build_pin,
                        "PLUGIN_ROOT",
                        plugin / "nested" / "..",
                    ),
                    mock.patch.object(
                        build_pin,
                        "_safe_system_alias_target",
                        side_effect=safe_alias,
                    ),
                    mock.patch.object(
                        build_pin,
                        "build_prepared_june_pin",
                        return_value=(prepared, report),
                    ),
                    self.assertRaisesRegex(
                        build_pin.PinError,
                        "outside the plugin repository",
                    ),
                ):
                    build_pin.write_prepared_june_artifacts(
                        [],
                        github_pr_cache=directory / "pr-cache",
                        repository_root=directory / "repositories",
                        output=output,
                        quarantine_report=directory / f"{label}-report.json",
                        external_pin_sha256="b" * 64,
                        recorded_order_sha256="c" * 64,
                        parquet_sha256="d" * 64,
                    )

    def test_prepare_june_refuses_real_case_insensitive_plugin_alias(self):
        import os
        from unittest import mock

        from bench.compare import build_pin

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve()
            plugin = directory / "PluginRoot"
            plugin.mkdir()
            case_alias = directory / "pLUGINrOOT"
            try:
                same_directory = os.path.samestat(
                    os.stat(plugin),
                    os.stat(case_alias),
                )
            except FileNotFoundError:
                same_directory = False
            if not same_directory:
                self.skipTest("filesystem is case-sensitive")

            with (
                mock.patch.object(build_pin, "PLUGIN_ROOT", plugin),
                mock.patch.object(
                    build_pin,
                    "build_prepared_june_pin",
                    return_value=(
                        {"schema_version": 1},
                        {
                            "schema_version": 1,
                            "prepared_count": 200,
                            "quarantined_count": 0,
                        },
                    ),
                ),
                self.assertRaisesRegex(
                    build_pin.PinError,
                    "outside the plugin repository",
                ),
            ):
                build_pin.write_prepared_june_artifacts(
                    [],
                    github_pr_cache=directory / "pr-cache",
                    repository_root=directory / "repositories",
                    output=case_alias / "artifacts" / "prepared.json",
                    quarantine_report=directory / "quarantine.json",
                    external_pin_sha256="b" * 64,
                    recorded_order_sha256="c" * 64,
                    parquet_sha256="d" * 64,
                )

    def test_prepare_june_rejects_requested_path_symlinks_before_resolution(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve()
            actual_parent = directory / "actual"
            actual_parent.mkdir()
            linked_parent = directory / "linked"
            linked_parent.symlink_to(actual_parent, target_is_directory=True)
            dangling_output = directory / "dangling-output.json"
            dangling_output.symlink_to(directory / "missing-target.json")
            outputs = {
                "dangling_final": dangling_output,
                "symlinked_ancestor": linked_parent / "prepared.json",
            }

            for name, output in outputs.items():
                with self.subTest(name=name):
                    result = subprocess.run(
                        [
                            sys.executable,
                            str(BUILD_PIN),
                            "prepare-june",
                            "--reference",
                            str(directory / "missing-reference.json"),
                            "--external-pin",
                            str(directory / "external.json"),
                            "--parquet",
                            str(directory / "source.parquet"),
                            "--github-pr-cache",
                            str(directory / "pr-cache"),
                            "--repository-root",
                            str(directory / "repositories"),
                            "--output",
                            str(output),
                            "--quarantine-report",
                            str(directory / "quarantine.json"),
                        ],
                        cwd=ROOT,
                        text=True,
                        capture_output=True,
                        check=False,
                    )

                    self.assertEqual(result.returncode, 1)
                    self.assertIn("symlink", result.stderr.lower())
                    self.assertFalse((directory / "missing-target.json").exists())
                    self.assertFalse((actual_parent / "prepared.json").exists())

    def test_prepare_june_accepts_unresolved_macos_system_alias(self):
        import os
        from unittest import mock

        import bench.compare.build_pin as build_pin

        report = {
            "schema_version": 1,
            "prepared_count": 0,
            "quarantined_count": 200,
        }
        with tempfile.TemporaryDirectory(dir="/var/tmp") as tmp:
            directory = Path(tmp)
            output = directory / "prepared.json"
            quarantine = directory / "quarantine.json"
            with mock.patch.object(
                build_pin,
                "build_prepared_june_pin",
                return_value=(None, report),
            ):
                try:
                    status = build_pin.write_prepared_june_artifacts(
                        [],
                        github_pr_cache=directory / "pr-cache",
                        repository_root=directory / "repositories",
                        output=output,
                        quarantine_report=quarantine,
                        external_pin_sha256="b" * 64,
                        recorded_order_sha256="c" * 64,
                        parquet_sha256="d" * 64,
                    )
                except build_pin.PinError as exc:
                    self.fail(f"safe macOS system alias was rejected: {exc}")
            canonical_quarantine = Path(os.path.realpath(quarantine))

            self.assertEqual(status, 2)
            self.assertEqual(
                build_pin._require_external_artifact_path(
                    quarantine,
                    "quarantine report",
                ),
                canonical_quarantine,
            )
            self.assertTrue(canonical_quarantine.is_file())
            self.assertFalse(Path(os.path.realpath(output)).exists())

    def test_prepare_june_selects_560_rows_in_published_evaluator_order(self):
        from bench.compare.build_pin import prepare_june_source_cases

        source_rows = [
            {
                "instance_id": f"example__public-{index}",
                "repo": "example/public",
                "base_commit": f"{index:040x}",
                "category": "Bug Report",
                "problem_statement": f"Find case {index}.",
                "edit_functions": [f"src/case_{index}.py:function_{index}"],
                "test_patch": "must not be used",
                "hints_text": "must not be used",
                "labels": ["must not be used"],
                "oracle": {"files": ["must/not/be/used.py"]},
            }
            for index in range(560)
        ]
        source_rows[1]["edit_functions"] = [
            "src/alpha.py:Alpha.__init__",
            "src/alpha.py:Alpha.run",
            "src/beta.py:top_level",
            "src/alpha.py:Alpha.run",
        ]
        selected_ids = [
            f"example__public-{index}" for index in reversed(range(200))
        ]
        external_pin = {
            "schema_version": 1,
            "n": 200,
            "score_depth": 10,
            "pinned_instance_ids": selected_ids,
            "cases": [
                {
                    "instance_id": case_id,
                    "repo": "example/public",
                    "base_commit": f"{int(case_id.rsplit('-', 1)[1]):040x}",
                    "category": "Bug Report",
                }
                for case_id in selected_ids
            ],
        }

        selected = prepare_june_source_cases(source_rows, external_pin)

        self.assertEqual(
            [case["case_id"] for case in selected],
            selected_ids,
        )
        normalized = selected[-2]
        self.assertEqual(
            normalized["oracle"],
            {
                "files": ["src/alpha.py", "src/beta.py"],
                "classes": ["Alpha", "top_level"],
                "functions": ["Alpha", "Alpha.run", "top_level"],
            },
        )
        self.assertEqual(normalized["query"], "Find case 1.")
        self.assertFalse(
            {"test_patch", "hints_text", "labels", "oracle_source"}
            & set(normalized)
        )

    def test_prepare_june_validates_all_identities_before_any_labels(self):
        from unittest import mock

        from bench.compare.build_pin import PinError, prepare_june_source_cases

        source_rows = [
            {
                "instance_id": f"example__public-{index}",
                "repo": "example/public",
                "base_commit": f"{index:040x}",
                "category": "Bug Report",
                "problem_statement": f"Find case {index}.",
                "edit_functions": [f"src/case_{index}.py:function_{index}"],
            }
            for index in range(560)
        ]
        source_rows[0]["edit_functions"] = ["malformed label"]
        source_rows[199]["repo"] = "different/public"
        selected_ids = [
            f"example__public-{index}" for index in range(200)
        ]
        external_pin = {
            "schema_version": 1,
            "n": 200,
            "score_depth": 10,
            "pinned_instance_ids": selected_ids,
            "cases": [
                {
                    "instance_id": case_id,
                    "repo": "example/public",
                    "base_commit": f"{index:040x}",
                    "category": "Bug Report",
                }
                for index, case_id in enumerate(selected_ids)
            ],
        }

        with mock.patch(
            "bench.compare.build_pin._locbench_oracle",
            side_effect=AssertionError(
                "label parser ran before global identity validation"
            ),
        ) as label_parser:
            with self.assertRaisesRegex(PinError, "source row identity"):
                prepare_june_source_cases(source_rows, external_pin)

        label_parser.assert_not_called()

    def test_prepare_june_binds_content_addressed_redirected_pr_response(self):
        from bench.compare.build_pin import load_cached_pull_request

        response = {
            "url": "https://api.github.com/repos/new/name/pulls/17",
            "html_url": "https://github.com/new/name/pull/17",
            "number": 17,
            "state": "closed",
            "merged": True,
            "merge_commit_sha": "d" * 40,
            "base": {
                "sha": "a" * 40,
                "repo": {"full_name": "new/name"},
            },
            "head": {
                "sha": "b" * 40,
                "repo": {"full_name": "contributor/fork"},
            },
            "title": "must not be persisted",
            "body": "must not be persisted",
            "query": "must not be persisted",
            "patch": "must not be persisted",
            "token": "must not be persisted",
        }
        response_bytes = (
            json.dumps(response, sort_keys=True).encode("utf-8") + b"\n"
        )
        response_sha256 = hashlib.sha256(response_bytes).hexdigest()
        request_url = "https://api.github.com/repos/old/name/pulls/17"
        with tempfile.TemporaryDirectory() as tmp:
            cache = Path(tmp)
            responses = cache / "responses"
            responses.mkdir()
            (responses / f"{response_sha256}.json").write_bytes(response_bytes)
            (cache / "index.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "responses": {
                            request_url: {
                                "sha256": response_sha256,
                                "path": f"responses/{response_sha256}.json",
                            }
                        },
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            evidence = load_cached_pull_request(
                cache,
                case_id="old__name-17",
                repository="old/name",
            )

        self.assertEqual(
            evidence,
            {
                "request_api_url": request_url,
                "api_url": "https://api.github.com/repos/new/name/pulls/17",
                "html_url": "https://github.com/new/name/pull/17",
                "requested_repository": "old/name",
                "resolved_repository": "new/name",
                "repository_redirected": True,
                "head_repository": "contributor/fork",
                "pr_number": 17,
                "merged": True,
                "base_commit": "a" * 40,
                "head_commit": "b" * 40,
                "merge_commit": "d" * 40,
                "response_sha256": response_sha256,
            },
        )
        serialized = json.dumps(evidence, sort_keys=True)
        for prohibited in (
            "must not be persisted",
            "title",
            "body",
            "query",
            "patch",
            "token",
        ):
            self.assertNotIn(prohibited, serialized)

    def test_prepare_june_rejects_symlinked_pr_cache_components(self):
        from bench.compare.build_pin import PinError, load_cached_pull_request

        response = {
            "url": "https://api.github.com/repos/example/public/pulls/17",
            "html_url": "https://github.com/example/public/pull/17",
            "number": 17,
            "state": "closed",
            "merged": True,
            "merge_commit_sha": "d" * 40,
            "base": {
                "sha": "a" * 40,
                "repo": {"full_name": "example/public"},
            },
            "head": {
                "sha": "b" * 40,
                "repo": {"full_name": "example/public"},
            },
        }
        encoded = json.dumps(response, sort_keys=True).encode("utf-8") + b"\n"
        response_sha256 = hashlib.sha256(encoded).hexdigest()
        request_url = "https://api.github.com/repos/example/public/pulls/17"
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve()
            cache = directory / "cache"
            cache.mkdir()
            outside = directory / "outside" / "responses"
            outside.mkdir(parents=True)
            (outside / f"{response_sha256}.json").write_bytes(encoded)
            (cache / "responses").symlink_to(outside, target_is_directory=True)
            (cache / "index.json").write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "responses": {
                            request_url: {
                                "sha256": response_sha256,
                                "path": (
                                    f"responses/{response_sha256}.json"
                                ),
                            }
                        },
                    },
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(PinError, "cache.*unsafe|unsafe.*cache"):
                load_cached_pull_request(
                    cache,
                    case_id="example__public-17",
                    repository="example/public",
                )

    def test_prepare_june_audits_non_descendant_pr_from_merge_base_to_head(self):
        from bench.compare.build_pin import (
            derive_pr_label_audit,
            validate_git_label_audit,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repository_root = Path(tmp) / "repositories"
            repository = repository_root / "example" / "public"
            repository.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", repository], check=True)
            subprocess.run(
                ["git", "-C", repository, "config", "user.email", "fixture@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "config", "user.name", "Fixture"],
                check=True,
            )
            (repository / "README").write_text("root\n", encoding="utf-8")
            subprocess.run(["git", "-C", repository, "add", "README"], check=True)
            subprocess.run(
                ["git", "-C", repository, "commit", "-qm", "root"],
                check=True,
            )
            merge_base = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            subprocess.run(
                ["git", "-C", repository, "checkout", "-qb", "base"],
                check=True,
            )
            (repository / "base-only.txt").write_text(
                "unrelated base change\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", repository, "add", "base-only.txt"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "commit", "-qm", "base moved"],
                check=True,
            )
            base_commit = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            subprocess.run(
                [
                    "git",
                    "-C",
                    repository,
                    "checkout",
                    "-qb",
                    "pr-head",
                    merge_base,
                ],
                check=True,
            )
            source = repository / "src" / "alpha.py"
            source.parent.mkdir()
            source.write_text(
                "class Alpha:\n"
                "    def __init__(self):\n"
                "        pass\n"
                "    def run(self):\n"
                "        return True\n\n"
                "def top_level():\n"
                "    return True\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", repository, "add", "src/alpha.py"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "commit", "-qm", "PR head"],
                check=True,
            )
            head_commit = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            subprocess.run(
                ["git", "-C", repository, "checkout", "-q", "base"],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    repository,
                    "merge",
                    "--no-ff",
                    "-qm",
                    "merged PR",
                    head_commit,
                ],
                check=True,
            )
            later_merge = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            case = {
                "case_id": "example__public-17",
                "category": "Bug",
                "query": "Locate Alpha.",
                "repository": {
                    "url": "https://github.com/example/public",
                    "revision": base_commit,
                },
                "oracle": {
                    "files": ["src/alpha.py"],
                    "classes": ["Alpha", "top_level"],
                    "functions": ["Alpha", "Alpha.run", "top_level"],
                },
            }
            pull_request = {
                "request_api_url": "https://api.github.com/repos/example/public/pulls/17",
                "api_url": "https://api.github.com/repos/example/public/pulls/17",
                "html_url": "https://github.com/example/public/pull/17",
                "requested_repository": "example/public",
                "resolved_repository": "example/public",
                "repository_redirected": False,
                "head_repository": "contributor/fork",
                "pr_number": 17,
                "merged": True,
                "base_commit": base_commit,
                "head_commit": head_commit,
                "merge_commit": later_merge,
                "response_sha256": "f" * 64,
            }

            case["label_audit"] = derive_pr_label_audit(
                case_id=case["case_id"],
                repository="example/public",
                base_commit=base_commit,
                oracle=case["oracle"],
                pull_request=pull_request,
                repository_root=repository_root,
            )
            validate_git_label_audit(case, repository_root)

        audit = case["label_audit"]
        self.assertEqual(audit["topology"], "non_descendant")
        self.assertEqual(audit["merge_base_commit"], merge_base)
        self.assertEqual(audit["comparison_base_commit"], merge_base)
        self.assertEqual(audit["comparison_head_commit"], head_commit)
        self.assertEqual(audit["changed_files"], ["src/alpha.py"])
        self.assertNotIn("base-only.txt", audit["changed_files"])
        self.assertNotEqual(audit["comparison_head_commit"], later_merge)

    def test_prepare_june_batches_repositories_and_preserves_case_order(self):
        import shutil
        from unittest import mock

        from bench.compare import build_pin

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            repository_root, repository, arguments = self._git_audit_fixture(
                directory
            )
            second_repository = repository_root / "example" / "second"
            shutil.copytree(repository, second_repository)
            case_ids = [
                f"example__case-{number}" for number in range(1, 201)
            ]
            cases = [
                {
                    "case_id": case_id,
                    "category": "Bug",
                    "query": f"Locate target {number}.",
                    "repository": {
                        "url": (
                            "https://github.com/example/public"
                            if number % 2
                            else "https://github.com/example/second"
                        ),
                        "revision": arguments["base_commit"],
                    },
                    "oracle": arguments["oracle"],
                }
                for number, case_id in enumerate(case_ids, start=1)
            ]
            call_order: list[tuple[str, str]] = []

            def cached_pull_request(_cache, *, case_id, repository):
                return {
                    "base_commit": arguments["base_commit"],
                    "resolved_repository": repository,
                    "head_commit": arguments["head_commit"],
                    "merge_commit": arguments["head_commit"],
                    "response_sha256": hashlib.sha256(
                        case_id.encode("utf-8")
                    ).hexdigest(),
                }

            def derive_from_batch(**kwargs):
                batch = kwargs["batch"]
                checkout = batch.checkout()
                call_order.append((checkout.slug, kwargs["case_id"]))
                self.assertIsNotNone(
                    build_pin._git_bytes(
                        checkout,
                        "rev-parse",
                        f"{arguments['head_commit']}^{{commit}}",
                    )
                )
                return {
                    "audit_record_sha256": hashlib.sha256(
                        kwargs["case_id"].encode("utf-8")
                    ).hexdigest()
                }

            real_fsck = build_pin._verify_git_object_integrity
            real_snapshot = build_pin._snapshot_git_object_store
            with (
                mock.patch.object(
                    build_pin,
                    "load_cached_pull_request",
                    side_effect=cached_pull_request,
                ),
                mock.patch.object(
                    build_pin,
                    "derive_pr_label_audit",
                    side_effect=derive_from_batch,
                ),
                mock.patch.object(
                    build_pin,
                    "_verify_git_object_integrity",
                    wraps=real_fsck,
                ) as fsck,
                mock.patch.object(
                    build_pin,
                    "_snapshot_git_object_store",
                    wraps=real_snapshot,
                ) as snapshot,
            ):
                pin, report = build_pin.build_prepared_june_pin(
                    cases,
                    github_pr_cache=directory / "unused-cache",
                    repository_root=repository_root,
                    external_pin_sha256="a" * 64,
                    recorded_order_sha256="b" * 64,
                    parquet_sha256="c" * 64,
                )

        self.assertIsNotNone(pin)
        assert pin is not None
        self.assertEqual(report["status"], "complete")
        self.assertEqual(
            pin["generation"]["selected_instance_ids"],
            case_ids,
        )
        self.assertEqual(
            [case["case_id"] for case in pin["cases"]],
            case_ids,
        )
        self.assertEqual(
            [repository for repository, _case_id in call_order],
            ["example/public"] * 100 + ["example/second"] * 100,
        )
        self.assertEqual(fsck.call_count, 2)
        self.assertEqual(snapshot.call_count, 4)

    def test_prepare_june_quarantines_entire_mutated_repository_batch(self):
        from unittest import mock

        from bench.compare import build_pin

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            repository_root, repository, arguments = self._git_audit_fixture(
                directory
            )
            case_ids = [
                f"example__public-{number}" for number in range(1, 201)
            ]
            cases = [
                {
                    "case_id": case_id,
                    "category": "Bug",
                    "query": f"Locate target {number}.",
                    "repository": {
                        "url": "https://github.com/example/public",
                        "revision": arguments["base_commit"],
                    },
                    "oracle": arguments["oracle"],
                }
                for number, case_id in enumerate(case_ids, start=1)
            ]
            mutated = False

            def cached_pull_request(_cache, *, case_id, repository):
                return {
                    "base_commit": arguments["base_commit"],
                    "resolved_repository": repository,
                    "head_commit": arguments["head_commit"],
                    "merge_commit": arguments["head_commit"],
                    "response_sha256": hashlib.sha256(
                        case_id.encode("utf-8")
                    ).hexdigest(),
                }

            def derive_then_mutate(**kwargs):
                nonlocal mutated
                kwargs["batch"].checkout()
                if not mutated:
                    object_directory = next(
                        candidate
                        for candidate in (
                            repository / ".git" / "objects"
                        ).iterdir()
                        if candidate.is_dir()
                        and len(candidate.name) == 2
                        and all(
                            character in "0123456789abcdef"
                            for character in candidate.name
                        )
                    )
                    transient = object_directory / "transient"
                    transient.write_bytes(b"transient mutation")
                    transient.unlink()
                    mutated = True
                return {
                    "audit_record_sha256": hashlib.sha256(
                        kwargs["case_id"].encode("utf-8")
                    ).hexdigest()
                }

            with (
                mock.patch.object(
                    build_pin,
                    "load_cached_pull_request",
                    side_effect=cached_pull_request,
                ),
                mock.patch.object(
                    build_pin,
                    "derive_pr_label_audit",
                    side_effect=derive_then_mutate,
                ),
            ):
                pin, report = build_pin.build_prepared_june_pin(
                    cases,
                    github_pr_cache=directory / "unused-cache",
                    repository_root=repository_root,
                    external_pin_sha256="a" * 64,
                    recorded_order_sha256="b" * 64,
                    parquet_sha256="c" * 64,
                )

        self.assertTrue(mutated)
        self.assertIsNone(pin)
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["prepared_count"], 0)
        self.assertEqual(report["quarantined_count"], 200)
        self.assertEqual(
            [item["case_id"] for item in report["cases"]],
            case_ids,
        )
        self.assertTrue(
            all(
                item["stage"] == "git_audit"
                and item["reason_code"]
                == "pr_git_comparison_unverified"
                for item in report["cases"]
            )
        )

    def test_prepare_june_quarantines_criss_cross_pr_with_multiple_merge_bases(self):
        from unittest import mock

        import bench.compare.build_pin as build_pin

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            repository_root = directory / "repositories"
            repository = repository_root / "example" / "public"
            repository.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", repository], check=True)
            subprocess.run(
                [
                    "git",
                    "-C",
                    repository,
                    "config",
                    "user.email",
                    "fixture@example.invalid",
                ],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    repository,
                    "config",
                    "user.name",
                    "Fixture",
                ],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    repository,
                    "commit",
                    "--allow-empty",
                    "-qm",
                    "root",
                ],
                check=True,
            )
            root_commit = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            subprocess.run(
                ["git", "-C", repository, "checkout", "-qb", "side-a"],
                check=True,
            )
            (repository / "a.txt").write_text("side a\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", repository, "add", "a.txt"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "commit", "-qm", "side a"],
                check=True,
            )
            side_a = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            subprocess.run(
                [
                    "git",
                    "-C",
                    repository,
                    "checkout",
                    "-qb",
                    "side-b",
                    root_commit,
                ],
                check=True,
            )
            (repository / "b.txt").write_text("side b\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", repository, "add", "b.txt"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "commit", "-qm", "side b"],
                check=True,
            )
            side_b = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            subprocess.run(
                ["git", "-C", repository, "checkout", "-q", side_a],
                check=True,
            )
            (repository / "b.txt").write_text("side b\n", encoding="utf-8")
            target = repository / "src" / "target.py"
            target.parent.mkdir()
            target.write_text(
                "async def target():\n    return True\n",
                encoding="utf-8",
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    repository,
                    "add",
                    "b.txt",
                    "src/target.py",
                ],
                check=True,
            )
            merged_tree = subprocess.check_output(
                ["git", "-C", repository, "write-tree"],
                text=True,
            ).strip()

            def commit_tree(message, *parents):
                command = [
                    "git",
                    "-C",
                    repository,
                    "commit-tree",
                    merged_tree,
                ]
                for parent in parents:
                    command.extend(["-p", parent])
                return subprocess.check_output(
                    command,
                    input=f"{message}\n",
                    text=True,
                ).strip()

            criss_cross_base = commit_tree(
                "merge side b into side a",
                side_a,
                side_b,
            )
            criss_cross_head = commit_tree(
                "merge side a into side b",
                side_b,
                side_a,
            )
            merge_commit = commit_tree(
                "merge criss-cross PR",
                criss_cross_base,
                criss_cross_head,
            )
            unique_head = commit_tree(
                "unique non-descendant head",
                root_commit,
            )
            unique_merge = commit_tree(
                "merge unique non-descendant PR",
                side_a,
                unique_head,
            )
            best_bases = set(
                subprocess.check_output(
                    [
                        "git",
                        "-C",
                        repository,
                        "merge-base",
                        "--all",
                        criss_cross_base,
                        criss_cross_head,
                    ],
                    text=True,
                ).split()
            )
            self.assertEqual(best_bases, {side_a, side_b})

            oracle = {
                "files": ["src/target.py"],
                "classes": ["target"],
                "functions": ["target"],
            }

            def pull_request(case_id, base_commit, head_commit, merged_commit):
                number = int(case_id.rsplit("-", 1)[1])
                api_url = (
                    "https://api.github.com/repos/"
                    f"example/public/pulls/{number}"
                )
                return {
                    "request_api_url": api_url,
                    "api_url": api_url,
                    "html_url": (
                        "https://github.com/example/public/pull/"
                        f"{number}"
                    ),
                    "requested_repository": "example/public",
                    "resolved_repository": "example/public",
                    "repository_redirected": False,
                    "head_repository": "example/public",
                    "pr_number": number,
                    "merged": True,
                    "base_commit": base_commit,
                    "head_commit": head_commit,
                    "merge_commit": merged_commit,
                    "response_sha256": f"{number:064x}",
                }

            descendant = build_pin.derive_pr_label_audit(
                case_id="example__public-201",
                repository="example/public",
                base_commit=root_commit,
                oracle=oracle,
                pull_request=pull_request(
                    "example__public-201",
                    root_commit,
                    criss_cross_head,
                    criss_cross_head,
                ),
                repository_root=repository_root,
            )
            non_descendant = build_pin.derive_pr_label_audit(
                case_id="example__public-202",
                repository="example/public",
                base_commit=side_a,
                oracle=oracle,
                pull_request=pull_request(
                    "example__public-202",
                    side_a,
                    unique_head,
                    unique_merge,
                ),
                repository_root=repository_root,
            )
            self.assertEqual(descendant["topology"], "descendant")
            self.assertEqual(descendant["merge_base_commit"], root_commit)
            self.assertEqual(non_descendant["topology"], "non_descendant")
            self.assertEqual(non_descendant["merge_base_commit"], root_commit)

            cases = [
                {
                    "case_id": f"example__public-{number}",
                    "category": "Bug",
                    "query": f"Locate target for case {number}.",
                    "repository": {
                        "url": "https://github.com/example/public",
                        "revision": criss_cross_base,
                    },
                    "oracle": oracle,
                }
                for number in range(1, 201)
            ]
            ambiguous_pull_request = pull_request(
                "example__public-1",
                criss_cross_base,
                criss_cross_head,
                merge_commit,
            )

            def cached_pull_request(_cache, *, case_id, repository):
                self.assertEqual(repository, "example/public")
                if case_id == "example__public-1":
                    return ambiguous_pull_request
                return pull_request(
                    case_id,
                    criss_cross_base,
                    criss_cross_head,
                    merge_commit,
                )

            real_derive = build_pin.derive_pr_label_audit

            def derive_or_stub(**kwargs):
                if kwargs["case_id"] == "example__public-1":
                    return real_derive(**kwargs)
                return {
                    "audit_record_sha256": hashlib.sha256(
                        kwargs["case_id"].encode("utf-8")
                    ).hexdigest()
                }

            with (
                mock.patch.object(
                    build_pin,
                    "load_cached_pull_request",
                    side_effect=cached_pull_request,
                ),
                mock.patch.object(
                    build_pin,
                    "derive_pr_label_audit",
                    side_effect=derive_or_stub,
                ),
            ):
                pin, report = build_pin.build_prepared_june_pin(
                    cases,
                    github_pr_cache=directory / "unused-cache",
                    repository_root=repository_root,
                    external_pin_sha256="a" * 64,
                    recorded_order_sha256="b" * 64,
                    parquet_sha256="c" * 64,
                )

        if pin is not None:
            self.fail("ambiguous criss-cross comparison produced a runnable pin")
        self.assertEqual(report["status"], "incomplete")
        self.assertEqual(report["prepared_count"], 199)
        self.assertEqual(report["quarantined_count"], 1)
        self.assertEqual(
            report["cases"],
            [
                {
                    "case_id": "example__public-1",
                    "category": "Bug",
                    "stage": "git_audit",
                    "reason_code": "pr_git_comparison_unverified",
                    "identities": {
                        "requested_repository": "example/public",
                        "base_commit": criss_cross_base,
                        "pr_number": 1,
                        "resolved_repository": "example/public",
                        "head_commit": criss_cross_head,
                        "merge_commit": merge_commit,
                        "response_sha256": f"{1:064x}",
                    },
                }
            ],
        )

    def test_prepare_june_reverification_rejects_nested_pr_head_mismatch(self):
        from bench.compare.build_pin import (
            PinError,
            derive_pr_label_audit,
            validate_git_label_audit,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repository_root = Path(tmp) / "repositories"
            repository = repository_root / "example" / "public"
            repository.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", repository], check=True)
            subprocess.run(
                ["git", "-C", repository, "config", "user.email", "fixture@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "config", "user.name", "Fixture"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "commit", "--allow-empty", "-qm", "base"],
                check=True,
            )
            base_commit = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            source = repository / "src" / "target.py"
            source.parent.mkdir()
            source.write_text(
                "def target():\n    return True\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", repository, "add", "src/target.py"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "commit", "-qm", "head"],
                check=True,
            )
            head_commit = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            case = {
                "case_id": "example__public-1",
                "category": "Bug",
                "query": "Locate target.",
                "repository": {
                    "url": "https://github.com/example/public",
                    "revision": base_commit,
                },
                "oracle": {
                    "files": ["src/target.py"],
                    "classes": ["target"],
                    "functions": ["target"],
                },
            }
            pull_request = {
                "request_api_url": "https://api.github.com/repos/example/public/pulls/1",
                "api_url": "https://api.github.com/repos/example/public/pulls/1",
                "html_url": "https://github.com/example/public/pull/1",
                "requested_repository": "example/public",
                "resolved_repository": "example/public",
                "repository_redirected": False,
                "head_repository": "example/public",
                "pr_number": 1,
                "merged": True,
                "base_commit": base_commit,
                "head_commit": head_commit,
                "merge_commit": head_commit,
                "response_sha256": "f" * 64,
            }
            case["label_audit"] = derive_pr_label_audit(
                case_id=case["case_id"],
                repository="example/public",
                base_commit=base_commit,
                oracle=case["oracle"],
                pull_request=pull_request,
                repository_root=repository_root,
            )
            case["label_audit"]["pull_request"]["head_commit"] = base_commit
            digest_payload = dict(case["label_audit"])
            digest_payload.pop("audit_record_sha256")
            case["label_audit"]["audit_record_sha256"] = hashlib.sha256(
                json.dumps(
                    digest_payload,
                    sort_keys=True,
                    separators=(",", ":"),
                    ensure_ascii=False,
                ).encode("utf-8")
            ).hexdigest()

            with self.assertRaisesRegex(
                PinError,
                "does not match repository",
            ):
                validate_git_label_audit(case, repository_root)

    def test_git_label_audit_ignores_repository_replacement_refs(self):
        from bench.compare.build_pin import PinError, derive_git_label_audit

        with tempfile.TemporaryDirectory() as tmp:
            repository_root = Path(tmp) / "repositories"
            repository = repository_root / "example" / "public"
            repository.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", repository], check=True)
            subprocess.run(
                ["git", "-C", repository, "config", "user.email", "fixture@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "config", "user.name", "Fixture"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "commit", "--allow-empty", "-qm", "base"],
                check=True,
            )
            base_commit = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            target = repository / "src" / "target.py"
            target.parent.mkdir()
            target.write_text("VALUE = False\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", repository, "add", "src/target.py"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "commit", "-qm", "original head"],
                check=True,
            )
            original_head = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            subprocess.run(
                [
                    "git",
                    "-C",
                    repository,
                    "checkout",
                    "-qb",
                    "replacement",
                    base_commit,
                ],
                check=True,
            )
            target.parent.mkdir(exist_ok=True)
            target.write_text(
                "def target():\n    return True\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", repository, "add", "src/target.py"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "commit", "-qm", "replacement head"],
                check=True,
            )
            replacement_head = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            subprocess.run(
                [
                    "git",
                    "-C",
                    repository,
                    "replace",
                    original_head,
                    replacement_head,
                ],
                check=True,
            )

            with self.assertRaisesRegex(
                PinError,
                "oracle symbol",
            ):
                derive_git_label_audit(
                    case_id="example__public-1",
                    repository="example/public",
                    base_commit=base_commit,
                    head_commit=original_head,
                    oracle={
                        "files": ["src/target.py"],
                        "classes": [],
                        "functions": ["target"],
                    },
                    repository_root=repository_root,
                )

    def test_git_label_audit_rejects_legacy_graft_topology(self):
        from bench.compare.build_pin import PinError, derive_git_label_audit

        with tempfile.TemporaryDirectory() as tmp:
            repository_root = Path(tmp) / "repositories"
            repository = repository_root / "example" / "public"
            repository.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", repository], check=True)
            subprocess.run(
                ["git", "-C", repository, "config", "user.email", "fixture@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "config", "user.name", "Fixture"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "commit", "--allow-empty", "-qm", "base"],
                check=True,
            )
            base_commit = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            subprocess.run(
                ["git", "-C", repository, "checkout", "--orphan", "grafted-head"],
                check=True,
                capture_output=True,
            )
            target = repository / "src" / "target.py"
            target.parent.mkdir()
            target.write_text(
                "def target():\n    return True\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", repository, "add", "src/target.py"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "commit", "-qm", "independent head"],
                check=True,
            )
            head_commit = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            git_common = Path(
                subprocess.check_output(
                    ["git", "-C", repository, "rev-parse", "--git-common-dir"],
                    text=True,
                ).strip()
            )
            if not git_common.is_absolute():
                git_common = repository / git_common
            grafts = git_common / "info" / "grafts"
            grafts.parent.mkdir(exist_ok=True)
            grafts.write_text(
                f"{head_commit} {base_commit}\n",
                encoding="ascii",
            )

            with self.assertRaisesRegex(
                PinError,
                "graft|alternate Git topology",
            ):
                derive_git_label_audit(
                    case_id="example__public-1",
                    repository="example/public",
                    base_commit=base_commit,
                    head_commit=head_commit,
                    oracle={
                        "files": ["src/target.py"],
                        "classes": [],
                        "functions": ["target"],
                    },
                    repository_root=repository_root,
                )

    def test_git_label_audit_keeps_original_checkout_pinned_during_path_swap(self):
        import shutil
        from unittest import mock

        from bench.compare import build_pin

        with tempfile.TemporaryDirectory() as tmp:
            repository_root = Path(tmp) / "repositories"
            repository = repository_root / "example" / "public"
            repository.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", repository], check=True)
            commit = [
                "git",
                "-C",
                repository,
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "user.name=Fixture",
                "commit",
            ]
            subprocess.run(
                [*commit, "--allow-empty", "-qm", "base"],
                check=True,
            )
            base_commit = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            target = repository / "src" / "target.py"
            target.parent.mkdir()
            target.write_text(
                "def target():\n    return True\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", repository, "add", "src/target.py"],
                check=True,
            )
            subprocess.run([*commit, "-qm", "head"], check=True)
            head_commit = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            replacement = repository_root / "replacement"
            saved = repository_root / "original"
            shutil.copytree(repository, replacement)
            real_git_bytes = build_pin._git_bytes
            swapped = False

            def swap_before_audit(checkout, *arguments, **kwargs):
                nonlocal swapped
                if (
                    not swapped
                    and arguments
                    == ("rev-parse", f"{base_commit}^{{commit}}")
                ):
                    repository.rename(saved)
                    replacement.rename(repository)
                    grafts = repository / ".git" / "info" / "grafts"
                    grafts.parent.mkdir(exist_ok=True)
                    grafts.write_text(f"{head_commit}\n", encoding="ascii")
                    swapped = True
                return real_git_bytes(checkout, *arguments, **kwargs)

            with mock.patch.object(
                build_pin,
                "_git_bytes",
                side_effect=swap_before_audit,
            ):
                audit = build_pin.derive_git_label_audit(
                    case_id="example__public-1",
                    repository="example/public",
                    base_commit=base_commit,
                    head_commit=head_commit,
                    oracle={
                        "files": ["src/target.py"],
                        "classes": [],
                        "functions": ["target"],
                    },
                    repository_root=repository_root,
                )

        self.assertTrue(swapped)
        self.assertEqual(audit["status"], "verified")
        self.assertEqual(audit["head_commit"], head_commit)

    def test_git_label_audit_rejects_noncanonical_repository_slugs_before_open(
        self,
    ):
        from unittest import mock

        from bench.compare import build_pin

        invalid_slugs = (
            "../public",
            "owner/..",
            "./public",
            "owner/.",
            ".../public",
            "owner/...",
            "",
            "/public",
            "owner/",
            "owner//public",
            r"owner\public",
            "-owner/public",
            "owner-/public",
            "owner_/public",
            "owner/---",
        )
        for repository in invalid_slugs:
            with self.subTest(repository=repository):
                with (
                    mock.patch.object(
                        build_pin.os,
                        "open",
                        side_effect=AssertionError(
                            f"opened a descriptor for {repository!r}"
                        ),
                    ) as opened,
                    self.assertRaisesRegex(
                        build_pin.PinError,
                        "checkout.*unsafe",
                    ),
                ):
                    build_pin.derive_git_label_audit(
                        case_id="invalid__repository-1",
                        repository=repository,
                        base_commit="a" * 40,
                        head_commit="b" * 40,
                        oracle={
                            "files": ["src/target.py"],
                            "classes": [],
                            "functions": ["target"],
                        },
                        repository_root=Path("/not-opened"),
                    )
                opened.assert_not_called()

    def test_git_label_audit_accepts_canonical_renamed_repository_slug(self):
        from bench.compare.build_pin import derive_git_label_audit

        with tempfile.TemporaryDirectory() as tmp:
            repository_root = Path(tmp) / "repositories"
            repository_slug = "renamed-owner/renamed.repo_v2"
            repository = repository_root.joinpath(*repository_slug.split("/"))
            repository.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", repository], check=True)
            commit = [
                "git",
                "-C",
                repository,
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "user.name=Fixture",
                "commit",
            ]
            subprocess.run(
                [*commit, "--allow-empty", "-qm", "base"],
                check=True,
            )
            base_commit = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            target = repository / "src" / "target.py"
            target.parent.mkdir()
            target.write_text(
                "def target():\n    return True\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", repository, "add", "src/target.py"],
                check=True,
            )
            subprocess.run(
                [*commit, "-qm", "renamed repository head"],
                check=True,
            )
            head_commit = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()

            audit = derive_git_label_audit(
                case_id="renamed__repo-1",
                repository=repository_slug,
                base_commit=base_commit,
                head_commit=head_commit,
                oracle={
                    "files": ["src/target.py"],
                    "classes": [],
                    "functions": ["target"],
                },
                repository_root=repository_root,
            )

        self.assertEqual(audit["status"], "verified")
        self.assertEqual(audit["repository"], repository_slug)

    def test_generic_git_label_audit_preserves_definition_pattern_v1(self):
        from bench.compare.build_pin import (
            derive_git_label_audit,
            validate_git_label_audit,
        )

        scenarios = (
            (
                "legacy-python",
                "src/target.py",
                "class Widget:\n    pass\n\nasync def run():\n    return True\n",
                ["Legacy.Widget"],
                ["Legacy.run"],
            ),
            (
                "rust",
                "src/target.rs",
                "struct Engine;\n\nfn execute() {}\n",
                ["crate.Engine"],
                ["crate.execute"],
            ),
            (
                "go",
                "src/target.go",
                "type Service struct{}\n\nfunc (service *Service) Serve() {}\n",
                ["pkg.Service"],
                ["pkg.Serve"],
            ),
            (
                "javascript",
                "src/target.js",
                "class Panel {}\n\nfunction render() {}\n",
                ["web.Panel"],
                ["web.render"],
            ),
        )
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for index, (
                name,
                relative_path,
                content,
                classes,
                functions,
            ) in enumerate(scenarios, start=1):
                with self.subTest(name=name):
                    repository_root = directory / name / "repositories"
                    repository_slug = f"example/public-{name}"
                    repository = repository_root.joinpath(
                        *repository_slug.split("/")
                    )
                    repository.mkdir(parents=True)
                    subprocess.run(["git", "init", "-q", repository], check=True)
                    commit = [
                        "git",
                        "-C",
                        repository,
                        "-c",
                        "user.email=fixture@example.invalid",
                        "-c",
                        "user.name=Fixture",
                        "commit",
                    ]
                    subprocess.run(
                        [*commit, "--allow-empty", "-qm", "base"],
                        check=True,
                    )
                    base_commit = subprocess.check_output(
                        ["git", "-C", repository, "rev-parse", "HEAD"],
                        text=True,
                    ).strip()
                    target = repository / relative_path
                    target.parent.mkdir()
                    target.write_text(content, encoding="utf-8")
                    subprocess.run(
                        ["git", "-C", repository, "add", relative_path],
                        check=True,
                    )
                    subprocess.run([*commit, "-qm", "head"], check=True)
                    head_commit = subprocess.check_output(
                        ["git", "-C", repository, "rev-parse", "HEAD"],
                        text=True,
                    ).strip()
                    case = {
                        "case_id": f"generic__audit-{index}",
                        "repository": {
                            "url": f"https://github.com/{repository_slug}",
                            "revision": base_commit,
                        },
                        "oracle": {
                            "files": [relative_path],
                            "classes": classes,
                            "functions": functions,
                        },
                    }

                    case["label_audit"] = derive_git_label_audit(
                        case_id=case["case_id"],
                        repository=repository_slug,
                        base_commit=base_commit,
                        head_commit=head_commit,
                        oracle=case["oracle"],
                        repository_root=repository_root,
                    )
                    validate_git_label_audit(case, repository_root)

                    self.assertEqual(
                        case["label_audit"]["symbol_verification"],
                        "definition_pattern_v1",
                    )

    def test_git_label_audit_rejects_symlinked_repository_slug_components(self):
        from bench.compare.build_pin import PinError, derive_git_label_audit

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for symlink_component in ("intermediate", "final"):
                with self.subTest(symlink_component=symlink_component):
                    repository_root = (
                        directory / symlink_component / "repositories"
                    )
                    repository = repository_root / "actual" / "public"
                    repository.mkdir(parents=True)
                    subprocess.run(
                        ["git", "init", "-q", repository],
                        check=True,
                    )
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            repository,
                            "config",
                            "user.email",
                            "fixture@example.invalid",
                        ],
                        check=True,
                    )
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            repository,
                            "config",
                            "user.name",
                            "Fixture",
                        ],
                        check=True,
                    )
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            repository,
                            "commit",
                            "--allow-empty",
                            "-qm",
                            "base",
                        ],
                        check=True,
                    )
                    base_commit = subprocess.check_output(
                        ["git", "-C", repository, "rev-parse", "HEAD"],
                        text=True,
                    ).strip()
                    target = repository / "src" / "target.py"
                    target.parent.mkdir()
                    target.write_text(
                        "def target():\n    return True\n",
                        encoding="utf-8",
                    )
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            repository,
                            "add",
                            "src/target.py",
                        ],
                        check=True,
                    )
                    subprocess.run(
                        ["git", "-C", repository, "commit", "-qm", "head"],
                        check=True,
                    )
                    head_commit = subprocess.check_output(
                        ["git", "-C", repository, "rev-parse", "HEAD"],
                        text=True,
                    ).strip()
                    if symlink_component == "intermediate":
                        (repository_root / "example").symlink_to(
                            repository_root / "actual",
                            target_is_directory=True,
                        )
                    else:
                        (repository_root / "example").mkdir()
                        (repository_root / "example" / "public").symlink_to(
                            repository,
                            target_is_directory=True,
                        )

                    with self.assertRaisesRegex(
                        PinError,
                        "checkout.*unsafe|symlink",
                    ):
                        derive_git_label_audit(
                            case_id="example__public-1",
                            repository="example/public",
                            base_commit=base_commit,
                            head_commit=head_commit,
                            oracle={
                                "files": ["src/target.py"],
                                "classes": [],
                                "functions": ["target"],
                            },
                            repository_root=repository_root,
                        )

    def test_git_label_audit_ignores_hostile_git_environment(self):
        import os
        from unittest import mock

        from bench.compare.build_pin import derive_git_label_audit

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve()
            repository_root = directory / "repositories"
            repository = repository_root / "example" / "public"
            repository.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", repository], check=True)
            subprocess.run(
                ["git", "-C", repository, "config", "user.email", "fixture@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "config", "user.name", "Fixture"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "commit", "--allow-empty", "-qm", "base"],
                check=True,
            )
            base_commit = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            target = repository / "src" / "target.py"
            target.parent.mkdir()
            target.write_text(
                "def target():\n    return True\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", repository, "add", "src/target.py"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "commit", "-qm", "head"],
                check=True,
            )
            head_commit = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            hostile = directory / "hostile"
            hostile.mkdir()
            environment = {
                "GIT_DIR": str(hostile / "missing.git"),
                "GIT_WORK_TREE": str(hostile),
                "GIT_OBJECT_DIRECTORY": str(hostile / "objects"),
                "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(hostile / "alternate"),
                "GIT_CONFIG_COUNT": "1",
                "GIT_CONFIG_KEY_0": "core.repositoryformatversion",
                "GIT_CONFIG_VALUE_0": "999",
                "GIT_EXTERNAL_DIFF": str(hostile / "external-diff"),
            }

            with mock.patch.dict(os.environ, environment):
                audit = derive_git_label_audit(
                    case_id="example__public-1",
                    repository="example/public",
                    base_commit=base_commit,
                    head_commit=head_commit,
                    oracle={
                        "files": ["src/target.py"],
                        "classes": [],
                        "functions": ["target"],
                    },
                    repository_root=repository_root,
                )

        self.assertEqual(audit["status"], "verified")
        self.assertEqual(audit["head_commit"], head_commit)

    def test_git_label_audit_ignores_local_rename_detection_config(self):
        from bench.compare.build_pin import derive_git_label_audit

        with tempfile.TemporaryDirectory() as tmp:
            repository_root = Path(tmp) / "repositories"
            repository = repository_root / "example" / "public"
            repository.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", repository], check=True)
            subprocess.run(
                ["git", "-C", repository, "config", "user.email", "fixture@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "config", "user.name", "Fixture"],
                check=True,
            )
            original = repository / "src" / "original.py"
            original.parent.mkdir()
            original.write_text(
                "def target():\n    return True\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", repository, "add", "src/original.py"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "commit", "-qm", "base"],
                check=True,
            )
            base_commit = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            subprocess.run(
                [
                    "git",
                    "-C",
                    repository,
                    "mv",
                    "src/original.py",
                    "src/renamed.py",
                ],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "commit", "-qm", "rename target"],
                check=True,
            )
            head_commit = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            arguments = {
                "case_id": "example__public-1",
                "repository": "example/public",
                "base_commit": base_commit,
                "head_commit": head_commit,
                "oracle": {
                    "files": ["src/renamed.py"],
                    "classes": [],
                    "functions": ["target"],
                },
                "repository_root": repository_root,
            }
            subprocess.run(
                ["git", "-C", repository, "config", "diff.renames", "true"],
                check=True,
            )
            renames_enabled = derive_git_label_audit(**arguments)
            subprocess.run(
                ["git", "-C", repository, "config", "diff.renames", "false"],
                check=True,
            )
            renames_disabled = derive_git_label_audit(**arguments)

        self.assertEqual(
            renames_enabled["changed_files"],
            ["src/original.py", "src/renamed.py"],
        )
        self.assertEqual(
            renames_enabled["changed_files"],
            renames_disabled["changed_files"],
        )
        self.assertEqual(
            renames_enabled["patch_sha256"],
            renames_disabled["patch_sha256"],
        )
        self.assertEqual(
            renames_enabled["audit_record_sha256"],
            renames_disabled["audit_record_sha256"],
        )

    def test_generic_git_audit_ignores_unpinned_attribute_sources(self):
        from unittest import mock

        from bench.compare import build_pin

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            repository_root = directory / "repositories"
            repository = repository_root / "example" / "public"
            repository.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", repository], check=True)
            commit = [
                "git",
                "-C",
                repository,
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "user.name=Fixture",
                "commit",
            ]
            target = repository / "src" / "target.py"
            target.parent.mkdir()
            target.write_text(
                "def target():\n    return False\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", repository, "add", "src/target.py"],
                check=True,
            )
            subprocess.run([*commit, "-qm", "base"], check=True)
            base_commit = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            target.write_text(
                "def target():\n    return True\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", repository, "add", "src/target.py"],
                check=True,
            )
            subprocess.run([*commit, "-qm", "head"], check=True)
            head_commit = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            arguments = {
                "case_id": "example__public-1",
                "repository": "example/public",
                "base_commit": base_commit,
                "head_commit": head_commit,
                "oracle": {
                    "files": ["src/target.py"],
                    "classes": [],
                    "functions": ["target"],
                },
                "repository_root": repository_root,
            }
            baseline = build_pin.derive_git_label_audit(**arguments)
            external_attributes = directory / "host-attributes"
            external_attributes.write_text("*.py -diff\n", encoding="ascii")
            subprocess.run(
                [
                    "git",
                    "-C",
                    repository,
                    "config",
                    "core.attributesFile",
                    str(external_attributes),
                ],
                check=True,
            )
            real_run = build_pin.subprocess.run
            observed_commands = []

            def record_command(command, *args, **kwargs):
                observed_commands.append(command)
                return real_run(command, *args, **kwargs)

            with mock.patch.object(
                build_pin.subprocess,
                "run",
                side_effect=record_command,
            ):
                configured = build_pin.derive_git_label_audit(**arguments)
            subprocess.run(
                [
                    "git",
                    "-C",
                    repository,
                    "config",
                    "--unset",
                    "core.attributesFile",
                ],
                check=True,
            )
            (repository / ".gitattributes").write_text(
                "*.py -diff\n",
                encoding="ascii",
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    repository,
                    "add",
                    ".gitattributes",
                ],
                check=True,
            )
            subprocess.run(
                [*commit, "-qm", "unrelated attribute source"],
                check=True,
            )
            other_head_configured = build_pin.derive_git_label_audit(**arguments)

        self.assertEqual(
            baseline["symbol_verification"],
            "definition_pattern_v1",
        )
        for source, audit in (
            ("core.attributesFile", configured),
            ("unrelated HEAD", other_head_configured),
        ):
            with self.subTest(source=source):
                self.assertEqual(audit, baseline)
        diff_commands = [
            command for command in observed_commands if "diff" in command
        ]
        self.assertEqual(len(diff_commands), 2)
        for command in diff_commands:
            with self.subTest(command=command):
                self.assertIn(
                    f"core.attributesFile={build_pin.os.devnull}",
                    command,
                )
                self.assertIn(
                    f"--attr-source={head_commit}",
                    command[: command.index("diff")],
                )

    def test_generic_git_audit_rejects_mutated_local_attributes(self):
        from unittest import mock

        from bench.compare import build_pin

        with tempfile.TemporaryDirectory() as tmp:
            repository_root = Path(tmp) / "repositories"
            repository = repository_root / "example" / "public"
            repository.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", repository], check=True)
            commit = [
                "git",
                "-C",
                repository,
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "user.name=Fixture",
                "commit",
            ]
            subprocess.run(
                [*commit, "--allow-empty", "-qm", "base"],
                check=True,
            )
            base_commit = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            target = repository / "src" / "target.py"
            target.parent.mkdir()
            target.write_text(
                "def target():\n    return True\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", repository, "add", "src/target.py"],
                check=True,
            )
            subprocess.run([*commit, "-qm", "head"], check=True)
            head_commit = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            real_run = build_pin.subprocess.run
            attributes = repository / ".git" / "info" / "attributes"
            mutated = False

            def mutate_during_diff(command, *args, **kwargs):
                nonlocal mutated
                if not mutated and "diff" in command and "--binary" in command:
                    attributes.write_text("*.py -diff\n", encoding="ascii")
                    mutated = True
                return real_run(command, *args, **kwargs)

            with (
                mock.patch.object(
                    build_pin.subprocess,
                    "run",
                    side_effect=mutate_during_diff,
                ),
                self.assertRaisesRegex(
                    build_pin.PinError,
                    "attributes",
                ),
            ):
                build_pin.derive_git_label_audit(
                    case_id="example__public-1",
                    repository="example/public",
                    base_commit=base_commit,
                    head_commit=head_commit,
                    oracle={
                        "files": ["src/target.py"],
                        "classes": [],
                        "functions": ["target"],
                    },
                    repository_root=repository_root,
                )

        self.assertTrue(mutated)

    def test_git_audit_rejects_transient_git_admin_entries_and_recovers(self):
        from unittest import mock

        from bench.compare import build_pin

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            repository_root = directory / "repositories"
            repository = repository_root / "example" / "public"
            repository.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", repository], check=True)
            commit = [
                "git",
                "-C",
                repository,
                "-c",
                "user.email=fixture@example.invalid",
                "-c",
                "user.name=Fixture",
                "commit",
            ]
            subprocess.run(
                [*commit, "--allow-empty", "-qm", "base"],
                check=True,
            )
            base_commit = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            target = repository / "src" / "target.py"
            target.parent.mkdir()
            target.write_text(
                "def target():\n    return True\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", repository, "add", "src/target.py"],
                check=True,
            )
            subprocess.run([*commit, "-qm", "head"], check=True)
            head_commit = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            alternate_objects = directory / "alternate-objects"
            alternate_objects.mkdir()
            scenarios = (
                ("info/grafts", f"{head_commit} {base_commit}\n"),
                ("objects/info/alternates", f"{alternate_objects}\n"),
                ("shallow", f"{head_commit}\n"),
                ("info/attributes", "*.py -diff\n"),
            )
            arguments = {
                "case_id": "example__public-1",
                "repository": "example/public",
                "base_commit": base_commit,
                "head_commit": head_commit,
                "oracle": {
                    "files": ["src/target.py"],
                    "classes": [],
                    "functions": ["target"],
                },
                "repository_root": repository_root,
            }
            real_run = build_pin.subprocess.run
            for relative_path, content in scenarios:
                with self.subTest(relative_path=relative_path):
                    transient = repository / ".git" / relative_path
                    transient.parent.mkdir(parents=True, exist_ok=True)
                    mutated = False

                    def add_use_remove(
                        command,
                        *args,
                        _transient=transient,
                        _content=content,
                        **kwargs,
                    ):
                        nonlocal mutated
                        if not mutated and "rev-parse" in command:
                            _transient.write_text(
                                _content,
                                encoding="utf-8",
                            )
                            mutated = True
                            try:
                                return real_run(command, *args, **kwargs)
                            finally:
                                _transient.unlink()
                        return real_run(command, *args, **kwargs)

                    with (
                        mock.patch.object(
                            build_pin.subprocess,
                            "run",
                            side_effect=add_use_remove,
                        ),
                        self.assertRaisesRegex(
                            build_pin.PinError,
                            "changed during pinned Git inspection",
                        ),
                    ):
                        build_pin.derive_git_label_audit(**arguments)

                    self.assertTrue(mutated)
                    self.assertFalse(transient.exists())
                    stable = build_pin.derive_git_label_audit(**arguments)
                    self.assertEqual(stable["status"], "verified")

    def test_git_audit_rejects_persistently_corrupt_unreferenced_loose_object(
        self,
    ):
        from bench.compare import build_pin

        with tempfile.TemporaryDirectory() as tmp:
            _, repository, arguments = self._git_audit_fixture(Path(tmp))
            object_id = subprocess.check_output(
                ["git", "-C", repository, "hash-object", "-w", "--stdin"],
                input=b"unreferenced audit object\n",
            ).decode("ascii").strip()
            loose_object = (
                repository
                / ".git"
                / "objects"
                / object_id[:2]
                / object_id[2:]
            )
            loose_object.chmod(0o600)
            loose_object.write_bytes(b"x" * loose_object.stat().st_size)

            with self.assertRaisesRegex(
                build_pin.PinError,
                "object.*verification failed|corrupt",
            ):
                build_pin.derive_git_label_audit(**arguments)

    def test_git_audit_batch_runs_full_integrity_once_per_repository(self):
        from unittest import mock

        from bench.compare import build_pin

        with tempfile.TemporaryDirectory() as tmp:
            _, _, arguments = self._git_audit_fixture(Path(tmp))
            real_fsck = build_pin._verify_git_object_integrity
            real_snapshot = build_pin._snapshot_git_object_store
            with (
                mock.patch.object(
                    build_pin,
                    "_verify_git_object_integrity",
                    wraps=real_fsck,
                ) as fsck,
                mock.patch.object(
                    build_pin,
                    "_snapshot_git_object_store",
                    wraps=real_snapshot,
                ) as snapshot,
                build_pin.GitAuditBatch(
                    arguments["repository_root"],
                    arguments["repository"],
                ) as batch,
            ):
                audits = [
                    build_pin.derive_git_label_audit(
                        **{
                            **arguments,
                            "case_id": f"example__public-{number}",
                            "checkout": batch.checkout(),
                        }
                    )
                    for number in range(1, 9)
                ]

            self.assertTrue(
                all(audit["status"] == "verified" for audit in audits)
            )
            self.assertEqual(fsck.call_count, 1)
            self.assertEqual(snapshot.call_count, 2)

    def test_git_audit_rejects_transient_loose_object_mutation_and_restore(
        self,
    ):
        from unittest import mock

        from bench.compare import build_pin

        with tempfile.TemporaryDirectory() as tmp:
            _, repository, arguments = self._git_audit_fixture(Path(tmp))
            object_id = subprocess.check_output(
                ["git", "-C", repository, "hash-object", "-w", "--stdin"],
                input=b"transient unreferenced audit object\n",
            ).decode("ascii").strip()
            loose_object = (
                repository
                / ".git"
                / "objects"
                / object_id[:2]
                / object_id[2:]
            )
            loose_object.chmod(0o600)
            original = loose_object.read_bytes()
            real_run = build_pin.subprocess.run
            mutated = False

            def mutate_and_restore(command, *args, **kwargs):
                nonlocal mutated
                if not mutated and "rev-parse" in command:
                    loose_object.write_bytes(b"x" * len(original))
                    mutated = True
                    try:
                        return real_run(command, *args, **kwargs)
                    finally:
                        loose_object.write_bytes(original)
                return real_run(command, *args, **kwargs)

            with (
                mock.patch.object(
                    build_pin.subprocess,
                    "run",
                    side_effect=mutate_and_restore,
                ),
                self.assertRaisesRegex(
                    build_pin.PinError,
                    "changed during pinned Git inspection",
                ),
            ):
                build_pin.derive_git_label_audit(**arguments)

            self.assertTrue(mutated)
            stable = build_pin.derive_git_label_audit(**arguments)
            self.assertEqual(stable["status"], "verified")

    def test_git_audit_rejects_transient_pack_rename_and_restore(self):
        from unittest import mock

        from bench.compare import build_pin

        with tempfile.TemporaryDirectory() as tmp:
            _, repository, arguments = self._git_audit_fixture(Path(tmp))
            subprocess.run(
                ["git", "-C", repository, "gc", "--prune=now"],
                check=True,
            )
            pack = next((repository / ".git" / "objects" / "pack").glob("*.pack"))
            hidden_pack = pack.with_suffix(".pack.hidden")
            real_run = build_pin.subprocess.run
            mutated = False

            def rename_and_restore(command, *args, **kwargs):
                nonlocal mutated
                if not mutated and "rev-parse" in command:
                    pack.rename(hidden_pack)
                    mutated = True
                    try:
                        return real_run(command, *args, **kwargs)
                    finally:
                        hidden_pack.rename(pack)
                return real_run(command, *args, **kwargs)

            with (
                mock.patch.object(
                    build_pin.subprocess,
                    "run",
                    side_effect=rename_and_restore,
                ),
                self.assertRaisesRegex(
                    build_pin.PinError,
                    "changed during pinned Git inspection",
                ),
            ):
                build_pin.derive_git_label_audit(**arguments)

            self.assertTrue(mutated)
            stable = build_pin.derive_git_label_audit(**arguments)
            self.assertEqual(stable["status"], "verified")

    def test_git_audit_normalizes_symlinked_config_open_failure(self):
        from bench.compare import build_pin

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            _, repository, arguments = self._git_audit_fixture(directory)
            external_config = directory / "external.config"
            external_config.write_text("[core]\n\tbare = false\n", encoding="utf-8")
            config = repository / ".git" / "config"
            config.unlink()
            config.symlink_to(external_config)

            with self.assertRaisesRegex(
                build_pin.PinError,
                "Git admin config is missing or unsafe",
            ):
                build_pin.derive_git_label_audit(**arguments)

    def test_git_audit_repeated_invalid_config_keeps_fd_count_stable(self):
        import os

        from bench.compare import build_pin

        fd_directory = next(
            (
                candidate
                for candidate in (Path("/proc/self/fd"), Path("/dev/fd"))
                if candidate.is_dir()
            ),
            None,
        )
        if fd_directory is None:
            self.skipTest("platform does not expose the process descriptor table")

        with tempfile.TemporaryDirectory() as tmp:
            _, repository, arguments = self._git_audit_fixture(Path(tmp))
            config = repository / ".git" / "config"
            config.unlink()
            config.mkdir()
            before = len(os.listdir(fd_directory))

            for _ in range(12):
                with self.assertRaisesRegex(
                    build_pin.PinError,
                    "Git admin config is unsafe",
                ):
                    build_pin.derive_git_label_audit(**arguments)

            after = len(os.listdir(fd_directory))
            self.assertEqual(after, before)

    def test_git_object_generation_rejects_deep_tree_without_descriptor_leak(
        self,
    ):
        import os

        from bench.compare import build_pin

        fd_directory = next(
            (
                candidate
                for candidate in (Path("/proc/self/fd"), Path("/dev/fd"))
                if candidate.is_dir()
            ),
            None,
        )
        if fd_directory is None:
            self.skipTest("platform does not expose the process descriptor table")

        with tempfile.TemporaryDirectory() as tmp:
            git_directory = Path(tmp) / "git"
            objects = git_directory / "objects"
            objects.mkdir(parents=True)
            directory_flags = (
                os.O_RDONLY
                | getattr(os, "O_CLOEXEC", 0)
                | getattr(os, "O_DIRECTORY", 0)
                | getattr(os, "O_NOFOLLOW", 0)
            )
            saved_cwd = os.open(".", directory_flags)
            objects_descriptor = os.open(objects, directory_flags)
            git_descriptor = -1
            try:
                os.fchdir(objects_descriptor)
                for _ in range(1_100):
                    os.mkdir("d")
                    os.chdir("d")
                os.fchdir(saved_cwd)
                git_descriptor = os.open(git_directory, directory_flags)
                before = len(os.listdir(fd_directory))

                for _ in range(3):
                    with self.assertRaisesRegex(
                        build_pin.PinError,
                        "object.*depth|depth.*object",
                    ):
                        build_pin._snapshot_git_object_store(
                            git_descriptor,
                            "example/public",
                        )

                after = len(os.listdir(fd_directory))
                self.assertEqual(after, before)
            finally:
                if git_descriptor >= 0:
                    os.close(git_descriptor)
                os.fchdir(objects_descriptor)
                for _ in range(1_100):
                    os.chdir("d")
                for _ in range(1_100):
                    os.chdir("..")
                    os.rmdir("d")
                os.fchdir(saved_cwd)
                os.close(objects_descriptor)
                os.close(saved_cwd)

    def test_git_audit_repeated_config_hash_errors_keep_fd_count_stable(self):
        import os
        from unittest import mock

        from bench.compare import build_pin

        fd_directory = next(
            (
                candidate
                for candidate in (Path("/proc/self/fd"), Path("/dev/fd"))
                if candidate.is_dir()
            ),
            None,
        )
        if fd_directory is None:
            self.skipTest("platform does not expose the process descriptor table")

        with tempfile.TemporaryDirectory() as tmp:
            _, _, arguments = self._git_audit_fixture(Path(tmp))
            before = len(os.listdir(fd_directory))

            with mock.patch.object(
                build_pin,
                "_descriptor_sha256",
                side_effect=OSError("fixture hash failure"),
            ):
                for _ in range(12):
                    with self.assertRaisesRegex(
                        build_pin.PinError,
                        "Git admin config is missing or unsafe",
                    ):
                        build_pin.derive_git_label_audit(**arguments)

            after = len(os.listdir(fd_directory))
            self.assertEqual(after, before)

    def test_pr_audit_ignores_custom_diff_driver_config(self):
        from bench.compare.build_pin import (
            derive_pr_label_audit,
            validate_git_label_audit,
        )

        with tempfile.TemporaryDirectory() as tmp:
            repository_root = Path(tmp) / "repositories"
            repository = repository_root / "example" / "public"
            repository.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", repository], check=True)
            subprocess.run(
                ["git", "-C", repository, "config", "user.email", "fixture@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "config", "user.name", "Fixture"],
                check=True,
            )
            (repository / ".gitattributes").write_text(
                "*.py diff=custom\n",
                encoding="utf-8",
            )
            target = repository / "src" / "target.py"
            target.parent.mkdir()
            target.write_text(
                "def first():\n"
                "    return 1\n\n"
                "def target():\n"
                "    value = 1\n"
                "    return value\n",
                encoding="utf-8",
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    repository,
                    "add",
                    ".gitattributes",
                    "src/target.py",
                ],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "commit", "-qm", "base"],
                check=True,
            )
            base_commit = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            target.write_text(
                "def first():\n"
                "    return 1\n\n"
                "def target():\n"
                "    value = 2\n"
                "    return value\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", repository, "add", "src/target.py"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "commit", "-qm", "head"],
                check=True,
            )
            head_commit = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            oracle = {
                "files": ["src/target.py"],
                "classes": [],
                "functions": ["target"],
            }
            pull_request = {
                "request_api_url": "https://api.github.com/repos/example/public/pulls/1",
                "api_url": "https://api.github.com/repos/example/public/pulls/1",
                "html_url": "https://github.com/example/public/pull/1",
                "requested_repository": "example/public",
                "resolved_repository": "example/public",
                "repository_redirected": False,
                "head_repository": "example/public",
                "pr_number": 1,
                "merged": True,
                "base_commit": base_commit,
                "head_commit": head_commit,
                "merge_commit": head_commit,
                "response_sha256": "f" * 64,
            }
            driver_keys = (
                "diff.custom.xfuncname",
                "diff.custom.binary",
                "diff.custom.textconv",
            )
            variants = (
                {},
                {"diff.custom.xfuncname": "^def target"},
                {"diff.custom.binary": "true"},
                {"diff.custom.textconv": "/bin/false"},
            )

            def configure_driver(values):
                for key in driver_keys:
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            repository,
                            "config",
                            "--unset-all",
                            key,
                        ],
                        check=False,
                    )
                for key, value in values.items():
                    subprocess.run(
                        ["git", "-C", repository, "config", key, value],
                        check=True,
                    )

            audits = []
            for variant in variants:
                configure_driver(variant)
                audits.append(
                    derive_pr_label_audit(
                        case_id="example__public-1",
                        repository="example/public",
                        base_commit=base_commit,
                        oracle=oracle,
                        pull_request=pull_request,
                        repository_root=repository_root,
                    )
                )

            self.assertEqual(
                len({audit["audit_record_sha256"] for audit in audits}),
                1,
            )
            self.assertEqual(
                {audit["verifier"] for audit in audits},
                {"pinned_pr_object_delta_v2"},
            )
            self.assertEqual(
                {audit["changed_files_source"] for audit in audits},
                {"git_raw_object_delta_v1"},
            )
            self.assertEqual(
                {audit["comparison_format"] for audit in audits},
                {"git_raw_object_delta_v1"},
            )
            self.assertEqual(
                len({audit["comparison_sha256"] for audit in audits}),
                1,
            )
            self.assertTrue(
                all("patch_sha256" not in audit for audit in audits)
            )

            case = {
                "case_id": "example__public-1",
                "repository": {
                    "url": "https://github.com/example/public",
                    "revision": base_commit,
                },
                "oracle": oracle,
                "label_audit": audits[0],
            }
            for variant in variants:
                configure_driver(variant)
                validate_git_label_audit(case, repository_root)

    def test_pr_audit_uses_exact_python_ast_definitions(self):
        from bench.compare.build_pin import PinError, derive_pr_label_audit

        scenarios = (
            (
                "comment",
                "src/target.py",
                "# def target():\n#     return True\n",
                {"files": ["src/target.py"], "classes": [], "functions": ["target"]},
                False,
            ),
            (
                "string",
                "src/target.py",
                'SOURCE = "def target(): pass"\n',
                {"files": ["src/target.py"], "classes": [], "functions": ["target"]},
                False,
            ),
            (
                "wrong-nesting",
                "src/target.py",
                "class Other:\n    def target(self):\n        return True\n",
                {"files": ["src/target.py"], "classes": [], "functions": ["target"]},
                False,
            ),
            (
                "parse-error",
                "src/target.py",
                "def target(:\n    return True\n",
                {"files": ["src/target.py"], "classes": [], "functions": ["target"]},
                False,
            ),
            (
                "unsupported-extension",
                "src/target.js",
                "function target() { return true; }\n",
                {"files": ["src/target.js"], "classes": [], "functions": ["target"]},
                False,
            ),
            (
                "qualified-async-stub",
                "src/target.pyi",
                "class Outer:\n"
                "    class Inner:\n"
                "        async def run(self) -> None: ...\n",
                {
                    "files": ["src/target.pyi"],
                    "classes": ["Outer.Inner"],
                    "functions": ["Outer.Inner.run"],
                },
                True,
            ),
            (
                "normalized-init",
                "src/target.py",
                "class Outer:\n"
                "    class Service:\n"
                "        def __init__(self):\n"
                "            self.ready = True\n",
                {
                    "files": ["src/target.py"],
                    "classes": ["Outer"],
                    "functions": ["Outer.Service"],
                },
                True,
            ),
        )

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for name, path, content, oracle, expected_verified in scenarios:
                with self.subTest(name=name):
                    repository_root = directory / name / "repositories"
                    repository = repository_root / "example" / "public"
                    repository.mkdir(parents=True)
                    subprocess.run(
                        ["git", "init", "-q", repository],
                        check=True,
                    )
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            repository,
                            "config",
                            "user.email",
                            "fixture@example.invalid",
                        ],
                        check=True,
                    )
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            repository,
                            "config",
                            "user.name",
                            "Fixture",
                        ],
                        check=True,
                    )
                    subprocess.run(
                        [
                            "git",
                            "-C",
                            repository,
                            "commit",
                            "--allow-empty",
                            "-qm",
                            "base",
                        ],
                        check=True,
                    )
                    base_commit = subprocess.check_output(
                        ["git", "-C", repository, "rev-parse", "HEAD"],
                        text=True,
                    ).strip()
                    source = repository / path
                    source.parent.mkdir()
                    source.write_text(content, encoding="utf-8")
                    subprocess.run(
                        ["git", "-C", repository, "add", path],
                        check=True,
                    )
                    subprocess.run(
                        ["git", "-C", repository, "commit", "-qm", "head"],
                        check=True,
                    )
                    head_commit = subprocess.check_output(
                        ["git", "-C", repository, "rev-parse", "HEAD"],
                        text=True,
                    ).strip()
                    pull_request = {
                        "request_api_url": (
                            "https://api.github.com/repos/"
                            "example/public/pulls/1"
                        ),
                        "api_url": (
                            "https://api.github.com/repos/"
                            "example/public/pulls/1"
                        ),
                        "html_url": (
                            "https://github.com/example/public/pull/1"
                        ),
                        "requested_repository": "example/public",
                        "resolved_repository": "example/public",
                        "repository_redirected": False,
                        "head_repository": "example/public",
                        "pr_number": 1,
                        "merged": True,
                        "base_commit": base_commit,
                        "head_commit": head_commit,
                        "merge_commit": head_commit,
                        "response_sha256": "f" * 64,
                    }
                    arguments = {
                        "case_id": "example__public-1",
                        "repository": "example/public",
                        "base_commit": base_commit,
                        "oracle": oracle,
                        "pull_request": pull_request,
                        "repository_root": repository_root,
                    }

                    if expected_verified:
                        try:
                            audit = derive_pr_label_audit(**arguments)
                        except PinError as exc:
                            self.fail(
                                f"valid Python definition was rejected: {exc}"
                            )
                        self.assertEqual(
                            audit["symbol_verification"],
                            "locagent_evaluator_python_ast_py312_v3",
                        )
                    else:
                        with self.assertRaisesRegex(
                            PinError,
                            "labels.*not grounded|symbol",
                        ):
                            derive_pr_label_audit(**arguments)

    def test_locbench_ast_contract_rejects_syntax_newer_than_python_312(self):
        from unittest import mock

        from bench.compare import build_pin

        source = (
            "def target():\n"
            "    value = t'newer-only template string'\n"
            "    return value\n"
        )
        with mock.patch.object(
            build_pin.ast,
            "parse",
            wraps=build_pin.ast.parse,
        ) as parse:
            definitions = build_pin._python_definitions(
                {"src/target.py": source}
            )

        self.assertIsNone(definitions)
        parse.assert_called_once_with(
            source,
            filename="src/target.py",
            type_comments=True,
            feature_version=(3, 12),
        )
        self.assertEqual(
            build_pin.LOCBENCH_SYMBOL_VERIFICATION,
            "locagent_evaluator_python_ast_py312_v3",
        )

    def test_pr_audit_rejects_an_existing_but_unrelated_merge_object(self):
        from bench.compare.build_pin import PinError, derive_pr_label_audit

        with tempfile.TemporaryDirectory() as tmp:
            repository_root = Path(tmp) / "repositories"
            repository = repository_root / "example" / "public"
            repository.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", repository], check=True)
            subprocess.run(
                ["git", "-C", repository, "config", "user.email", "fixture@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "config", "user.name", "Fixture"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "commit", "--allow-empty", "-qm", "base"],
                check=True,
            )
            base_commit = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            target = repository / "src" / "target.py"
            target.parent.mkdir()
            target.write_text(
                "def target():\n    return True\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", repository, "add", "src/target.py"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "commit", "-qm", "PR head"],
                check=True,
            )
            head_commit = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            subprocess.run(
                [
                    "git",
                    "-C",
                    repository,
                    "checkout",
                    "-qb",
                    "unrelated",
                    base_commit,
                ],
                check=True,
            )
            (repository / "unrelated.txt").write_text(
                "unrelated\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", repository, "add", "unrelated.txt"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "commit", "-qm", "unrelated object"],
                check=True,
            )
            unrelated_commit = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            pull_request = {
                "request_api_url": "https://api.github.com/repos/example/public/pulls/1",
                "api_url": "https://api.github.com/repos/example/public/pulls/1",
                "html_url": "https://github.com/example/public/pull/1",
                "requested_repository": "example/public",
                "resolved_repository": "example/public",
                "repository_redirected": False,
                "head_repository": "example/public",
                "pr_number": 1,
                "merged": True,
                "base_commit": base_commit,
                "head_commit": head_commit,
                "merge_commit": unrelated_commit,
                "response_sha256": "f" * 64,
            }

            with self.assertRaisesRegex(
                PinError,
                "merge object.*unrelated",
            ):
                derive_pr_label_audit(
                    case_id="example__public-1",
                    repository="example/public",
                    base_commit=base_commit,
                    oracle={
                        "files": ["src/target.py"],
                        "classes": ["target"],
                        "functions": ["target"],
                    },
                    pull_request=pull_request,
                    repository_root=repository_root,
                )

    def test_prepare_june_missing_pyarrow_fails_actionably(self):
        import builtins
        from unittest import mock

        from bench.compare.build_pin import PinError, read_june_parquet_rows

        real_import = builtins.__import__

        def import_without_pyarrow(name, *args, **kwargs):
            if name == "pyarrow" or name.startswith("pyarrow."):
                raise ImportError("forced missing optional dependency")
            return real_import(name, *args, **kwargs)

        with tempfile.TemporaryDirectory() as tmp:
            parquet = Path(tmp) / "source.parquet"
            parquet.write_bytes(b"not needed before the lazy import")
            with (
                mock.patch(
                    "builtins.__import__",
                    side_effect=import_without_pyarrow,
                ),
                self.assertRaisesRegex(
                    PinError,
                    r"pyarrow.*pip install pyarrow",
                ),
            ):
                read_june_parquet_rows(parquet)

    def test_prepare_june_pyarrow_loader_oserror_fails_actionably(self):
        import builtins
        from unittest import mock

        from bench.compare.build_pin import PinError, read_june_parquet_rows

        real_import = builtins.__import__

        def broken_pyarrow_loader(name, *args, **kwargs):
            if name == "pyarrow" or name.startswith("pyarrow."):
                raise OSError("native pyarrow loader failed")
            return real_import(name, *args, **kwargs)

        with (
            mock.patch(
                "builtins.__import__",
                side_effect=broken_pyarrow_loader,
            ),
            self.assertRaisesRegex(
                PinError,
                r"pyarrow.*unavailable.*pip install pyarrow",
            ),
        ):
            read_june_parquet_rows(b"operator-local parquet snapshot")

    def test_prepare_june_parses_bytes_through_optional_pyarrow(self):
        import types
        from unittest import mock

        from bench.compare.build_pin import read_june_parquet_rows

        encoded = b"one parquet snapshot"
        rows = [{"instance_id": "example__public-1"}]
        reader = object()
        buffer_reader = mock.Mock(return_value=reader)
        table = mock.Mock()
        table.to_pylist.return_value = rows
        read_table = mock.Mock(return_value=table)
        pyarrow = types.ModuleType("pyarrow")
        pyarrow.BufferReader = buffer_reader
        pyarrow.parquet = types.SimpleNamespace(read_table=read_table)

        with mock.patch.dict(sys.modules, {"pyarrow": pyarrow}):
            parsed = read_june_parquet_rows(encoded)

        buffer_reader.assert_called_once_with(encoded)
        read_table.assert_called_once_with(
            reader,
            columns=[
                "instance_id",
                "repo",
                "base_commit",
                "category",
                "problem_statement",
                "edit_functions",
            ],
        )
        table.to_pylist.assert_called_once_with()
        self.assertIs(parsed, rows)

    def test_prepare_june_hashes_and_parses_one_parquet_byte_snapshot(self):
        from unittest import mock

        from bench.compare.build_pin import load_verified_june_parquet_rows

        encoded = b"one immutable parquet snapshot"
        parsed_rows = [{"instance_id": "example__public-1"}]
        path = Path("/must/not/be/reopened.parquet")

        with (
            mock.patch(
                "bench.compare.build_pin.JUNE_PARQUET_SIZE",
                len(encoded),
            ),
            mock.patch(
                "bench.compare.build_pin.JUNE_PARQUET_SHA256",
                hashlib.sha256(encoded).hexdigest(),
            ),
            mock.patch(
                "bench.compare.build_pin._read_input_snapshot",
                return_value=encoded,
            ) as read_snapshot,
            mock.patch(
                "bench.compare.build_pin.read_june_parquet_rows",
                return_value=parsed_rows,
            ) as parse_snapshot,
        ):
            rows = load_verified_june_parquet_rows(path)

        read_snapshot.assert_called_once_with(path, "LocBench parquet")
        parse_snapshot.assert_called_once_with(encoded)
        self.assertIs(parse_snapshot.call_args.args[0], encoded)
        self.assertIs(rows, parsed_rows)

    def test_prepare_june_requires_the_exact_checked_in_dataset_identity(self):
        from bench.compare.build_pin import (
            PinError,
            validate_june_reference,
        )

        reference = json.loads(
            (
                COMPARE / "pins" / "locbench-june-n200.external.json"
            ).read_text(encoding="utf-8")
        )
        validate_june_reference(reference)
        tampered = json.loads(json.dumps(reference))
        tampered["dataset"]["revision"] = "0" * 40

        with self.assertRaisesRegex(PinError, "checked-in June reference"):
            validate_june_reference(tampered)

    def test_prepare_june_cli_verifies_reference_before_parquet_labels(self):
        reference = json.loads(
            (
                COMPARE / "pins" / "locbench-june-n200.external.json"
            ).read_text(encoding="utf-8")
        )
        reference["dataset"]["revision"] = "0" * 40
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve()
            reference_path = directory / "reference.json"
            reference_path.write_text(
                json.dumps(reference, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(BUILD_PIN),
                    "prepare-june",
                    "--reference",
                    str(reference_path),
                    "--external-pin",
                    str(directory / "external.json"),
                    "--parquet",
                    str(directory / "source.parquet"),
                    "--github-pr-cache",
                    str(directory / "pr-cache"),
                    "--repository-root",
                    str(directory / "repositories"),
                    "--output",
                    str(directory / "prepared.json"),
                    "--quarantine-report",
                    str(directory / "quarantine.json"),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertEqual(result.returncode, 1)
        self.assertIn("checked-in June reference", result.stderr)
        self.assertNotIn("pyarrow", result.stderr)

    def test_prepare_june_cli_reports_actual_partial_counts(self):
        import argparse
        import contextlib
        import io
        from unittest import mock

        import bench.compare.build_pin as build_pin

        reference = json.loads(
            (
                COMPARE / "pins" / "locbench-june-n200.external.json"
            ).read_text(encoding="utf-8")
        )
        verification = {
            "sha256": build_pin.JUNE_EXTERNAL_PIN_SHA256,
            "verified_count": 200,
            "recorded_order_sha256": build_pin.JUNE_RECORDED_ORDER_SHA256,
        }
        report = {
            "schema_version": 1,
            "kind": "locbench_june_preparation_quarantine",
            "status": "incomplete",
            "expected_count": 200,
            "prepared_count": 199,
            "quarantined_count": 1,
            "dataset": {},
            "cases": [
                {
                    "case_id": "example__public-200",
                    "category": "Bug",
                    "stage": "git_audit",
                    "reason_code": "pr_git_comparison_unverified",
                    "identities": {
                        "requested_repository": "example/public",
                        "base_commit": "a" * 40,
                        "pr_number": 200,
                    },
                }
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve()
            arguments = argparse.Namespace(
                reference=directory / "reference.json",
                external_pin=directory / "external.json",
                parquet=directory / "source.parquet",
                github_pr_cache=directory / "pr-cache",
                repository_root=directory / "repositories",
                output=directory / "prepared.json",
                quarantine_report=directory / "quarantine.json",
            )
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    build_pin,
                    "load_object",
                    return_value=reference,
                ),
                mock.patch.object(
                    build_pin,
                    "_load_verified_external_pin",
                    return_value=(
                        verification,
                        {"cases": [], "pinned_instance_ids": []},
                    ),
                ),
                mock.patch.object(
                    build_pin,
                    "load_verified_june_parquet_rows",
                    return_value=[],
                ),
                mock.patch.object(
                    build_pin,
                    "prepare_june_source_cases_with_quarantine",
                    return_value=([], []),
                ),
                mock.patch.object(
                    build_pin,
                    "build_prepared_june_pin",
                    return_value=(None, report),
                ),
                contextlib.redirect_stdout(stdout),
            ):
                status = build_pin._prepare_june_command(arguments)
            artifact = arguments.quarantine_report.read_bytes()

        summary = json.loads(stdout.getvalue())
        self.assertEqual(status, 2)
        self.assertEqual(summary["status"], "quarantined")
        self.assertEqual(summary["prepared_count"], 199)
        self.assertEqual(summary["quarantined_count"], 1)
        self.assertEqual(
            summary["artifact_sha256"],
            hashlib.sha256(artifact).hexdigest(),
        )

    def test_prepare_june_cli_quarantines_malformed_source_labels(self):
        import argparse
        import contextlib
        import io
        from unittest import mock

        import bench.compare.build_pin as build_pin

        reference = json.loads(
            (
                COMPARE / "pins" / "locbench-june-n200.external.json"
            ).read_text(encoding="utf-8")
        )
        source_rows = [
            {
                "instance_id": f"example__public-{number}",
                "repo": "example/public",
                "base_commit": f"{number:040x}",
                "category": "Bug Report",
                "problem_statement": f"Find case {number}.",
                "edit_functions": [f"src/case_{number}.py:case_{number}"],
            }
            for number in range(1, 561)
        ]
        source_rows[0]["problem_statement"] = "private malformed-label query"
        source_rows[0]["edit_functions"] = ["private malformed label"]
        selected_ids = [
            f"example__public-{number}" for number in range(1, 201)
        ]
        external_pin = {
            "schema_version": 1,
            "n": 200,
            "score_depth": 10,
            "pinned_instance_ids": selected_ids,
            "cases": [
                {
                    "instance_id": case_id,
                    "repo": "example/public",
                    "base_commit": f"{number:040x}",
                    "category": "Bug Report",
                }
                for number, case_id in enumerate(selected_ids, start=1)
            ],
        }
        verification = {
            "sha256": build_pin.JUNE_EXTERNAL_PIN_SHA256,
            "verified_count": 200,
            "recorded_order_sha256": build_pin.JUNE_RECORDED_ORDER_SHA256,
        }

        def cached_pull_request(
            _cache,
            *,
            case_id,
            repository,
        ):
            number = int(case_id.rsplit("-", 1)[1])
            return {
                "requested_repository": repository,
                "resolved_repository": repository,
                "base_commit": f"{number:040x}",
                "head_commit": "b" * 40,
                "merge_commit": "c" * 40,
                "response_sha256": "d" * 64,
            }

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve()
            arguments = argparse.Namespace(
                reference=directory / "reference.json",
                external_pin=directory / "external.json",
                parquet=directory / "source.parquet",
                github_pr_cache=directory / "pr-cache",
                repository_root=directory / "repositories",
                output=directory / "prepared.json",
                quarantine_report=directory / "quarantine.json",
            )
            stdout = io.StringIO()
            with (
                mock.patch.object(
                    build_pin,
                    "load_object",
                    return_value=reference,
                ),
                mock.patch.object(
                    build_pin,
                    "_load_verified_external_pin",
                    return_value=(verification, external_pin),
                ),
                mock.patch.object(
                    build_pin,
                    "load_verified_june_parquet_rows",
                    return_value=source_rows,
                ),
                mock.patch.object(
                    build_pin,
                    "load_cached_pull_request",
                    side_effect=cached_pull_request,
                ),
                mock.patch.object(
                    build_pin,
                    "derive_pr_label_audit",
                    return_value={"audit_record_sha256": "e" * 64},
                ),
                contextlib.redirect_stdout(stdout),
            ):
                status = build_pin._prepare_june_command(arguments)
            encoded = arguments.quarantine_report.read_bytes()
            output_exists = arguments.output.exists()

        summary = json.loads(stdout.getvalue())
        report = json.loads(encoded)
        self.assertEqual(status, 2)
        self.assertFalse(output_exists)
        self.assertEqual(summary["prepared_count"], 199)
        self.assertEqual(summary["quarantined_count"], 1)
        self.assertEqual(report["prepared_count"], 199)
        self.assertEqual(report["quarantined_count"], 1)
        self.assertEqual(
            report["cases"],
            [
                {
                    "case_id": "example__public-1",
                    "category": "Bug",
                    "stage": "source_labels",
                    "reason_code": "source_label_unverified",
                    "identities": {
                        "requested_repository": "example/public",
                        "base_commit": f"{1:040x}",
                        "pr_number": 1,
                    },
                }
            ],
        )
        serialized = encoded.decode("utf-8")
        for prohibited in (
            "private malformed-label query",
            "private malformed label",
            '"query"',
            '"oracle"',
        ):
            self.assertNotIn(prohibited, serialized)

    def test_prepare_june_partial_writes_only_content_free_quarantine(self):
        from bench.compare.build_pin import write_prepared_june_artifacts

        cases = [
            {
                "case_id": f"example__public-{index}",
                "category": "Bug",
                "query": f"private source query {index}",
                "repository": {
                    "url": "https://github.com/example/public",
                    "revision": "a" * 40,
                },
                "oracle": {
                    "files": [f"src/private_{index}.py"],
                    "classes": [f"private_{index}"],
                    "functions": [f"private_{index}"],
                },
            }
            for index in range(1, 201)
        ]
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve()
            cache = directory / "pr-cache"
            cache.mkdir()
            (cache / "index.json").write_text(
                '{"responses":{},"schema_version":1}\n',
                encoding="utf-8",
            )
            output = directory / "prepared.json"
            quarantine = directory / "quarantine.json"

            status = write_prepared_june_artifacts(
                cases,
                github_pr_cache=cache,
                repository_root=directory / "repositories",
                output=output,
                quarantine_report=quarantine,
                external_pin_sha256="b" * 64,
                recorded_order_sha256="c" * 64,
                parquet_sha256="d" * 64,
            )
            encoded = quarantine.read_bytes()
            output_exists = output.exists()

        self.assertEqual(status, 2)
        self.assertFalse(output_exists)
        report = json.loads(encoded)
        self.assertEqual(report["prepared_count"], 0)
        self.assertEqual(report["quarantined_count"], 200)
        self.assertEqual(len(report["cases"]), 200)
        self.assertEqual(
            report["cases"][0],
            {
                "case_id": "example__public-1",
                "category": "Bug",
                "stage": "pr_cache",
                "reason_code": "pr_response_unverified",
                "identities": {
                    "requested_repository": "example/public",
                    "base_commit": "a" * 40,
                    "pr_number": 1,
                },
            },
        )
        serialized = encoded.decode("utf-8")
        for prohibited in (
            "private source query",
            "src/private_",
            '"oracle"',
            '"query"',
            '"patch"',
            '"title"',
            '"body"',
            '"token"',
        ):
            self.assertNotIn(prohibited, serialized)

    def test_prepare_june_requires_distinct_output_and_quarantine_paths(self):
        from unittest import mock

        from bench.compare.build_pin import PinError, write_prepared_june_artifacts

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve()
            aliases = {
                "exact": ("artifact.json", "artifact.json"),
                "case": ("Artifact.json", "artifact.JSON"),
                "unicode": (
                    "résumé.json",
                    "re\u0301sume\u0301.json",
                ),
            }
            for label, (output_name, report_name) in aliases.items():
                with (
                    self.subTest(label=label),
                    mock.patch(
                        "bench.compare.build_pin.build_prepared_june_pin",
                        return_value=(
                            None,
                            {
                                "schema_version": 1,
                                "prepared_count": 0,
                                "quarantined_count": 200,
                            },
                        ),
                    ),
                    self.assertRaisesRegex(PinError, "must be distinct"),
                ):
                    write_prepared_june_artifacts(
                        [],
                        github_pr_cache=directory / "pr-cache",
                        repository_root=directory / "repositories",
                        output=directory / output_name,
                        quarantine_report=directory / report_name,
                        external_pin_sha256="b" * 64,
                        recorded_order_sha256="c" * 64,
                        parquet_sha256="d" * 64,
                    )

    def test_prepare_june_never_clobbers_a_racing_output_file(self):
        from unittest import mock

        from bench.compare.build_pin import PinError, write_prepared_june_artifacts

        sentinel = "created by another process\n"
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve()
            output = directory / "prepared.json"
            quarantine = directory / "quarantine.json"

            def race_output(*_args, **_kwargs):
                output.write_text(sentinel, encoding="utf-8")
                return {"schema_version": 1}, {
                    "schema_version": 1,
                    "prepared_count": 200,
                    "quarantined_count": 0,
                }

            with mock.patch(
                "bench.compare.build_pin.build_prepared_june_pin",
                side_effect=race_output,
            ):
                with self.assertRaisesRegex(
                    PinError,
                    "already exists|non-clobber",
                ):
                    write_prepared_june_artifacts(
                        [],
                        github_pr_cache=directory / "pr-cache",
                        repository_root=directory / "repositories",
                        output=output,
                        quarantine_report=quarantine,
                        external_pin_sha256="b" * 64,
                        recorded_order_sha256="c" * 64,
                        parquet_sha256="d" * 64,
                    )

            self.assertEqual(output.read_text(encoding="utf-8"), sentinel)
            self.assertFalse(quarantine.exists())

    def test_prepare_june_serializes_concurrent_artifact_pair_publication(self):
        import multiprocessing
        import time
        from unittest import mock

        from bench.compare.build_pin import (
            PinError,
            write_prepared_june_artifacts,
        )

        context = multiprocessing.get_context("fork")
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve()
            output = directory / "prepared.json"
            quarantine = directory / "quarantine.json"
            ready = context.Queue()
            start = context.Event()
            results = context.Queue()

            def worker(emit_pin: bool) -> None:
                def racing_build(*_args, **_kwargs):
                    time.sleep(0.5)
                    report = {
                        "schema_version": 1,
                        "prepared_count": 200 if emit_pin else 0,
                        "quarantined_count": 0 if emit_pin else 200,
                    }
                    return (
                        {"schema_version": 1} if emit_pin else None,
                        report,
                    )

                ready.put("ready")
                start.wait()
                with mock.patch(
                    "bench.compare.build_pin.build_prepared_june_pin",
                    side_effect=racing_build,
                ):
                    try:
                        status = write_prepared_june_artifacts(
                            [],
                            github_pr_cache=directory / "pr-cache",
                            repository_root=directory / "repositories",
                            output=output,
                            quarantine_report=quarantine,
                            external_pin_sha256="b" * 64,
                            recorded_order_sha256="c" * 64,
                            parquet_sha256="d" * 64,
                        )
                    except PinError as exc:
                        results.put(("error", str(exc)))
                    else:
                        results.put(("status", status))

            processes = [
                context.Process(target=worker, args=(emit_pin,))
                for emit_pin in (True, False)
            ]
            for process in processes:
                process.start()
            ready.get(timeout=5)
            ready.get(timeout=5)
            start.set()
            for process in processes:
                process.join(timeout=10)
                self.assertEqual(process.exitcode, 0)
            outcomes = [results.get(timeout=5), results.get(timeout=5)]

            self.assertEqual(
                sum(kind == "status" for kind, _value in outcomes),
                1,
                outcomes,
            )
            self.assertEqual(
                sum(kind == "error" for kind, _value in outcomes),
                1,
                outcomes,
            )
            self.assertNotEqual(output.exists(), quarantine.exists())

    def test_prepare_june_serializes_every_shared_artifact_endpoint(self):
        import multiprocessing
        import time
        from unittest import mock

        from bench.compare.build_pin import (
            PinError,
            write_prepared_june_artifacts,
        )

        context = multiprocessing.get_context("fork")
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve()
            scenarios = {
                "shared-output": (
                    ("shared.json", "report-a.json", True),
                    ("shared.json", "report-b.json", True),
                ),
                "shared-report": (
                    ("output-a.json", "shared.json", False),
                    ("output-b.json", "shared.json", False),
                ),
                "shared-output-case-alias": (
                    ("Shared.JSON", "report-a.json", True),
                    ("shared.json", "report-b.json", True),
                ),
                "shared-report-unicode-alias": (
                    ("output-a.json", "résumé.json", False),
                    (
                        "output-b.json",
                        "re\u0301sume\u0301.json",
                        False,
                    ),
                ),
                "reversed-pair": (
                    ("first.json", "second.json", True),
                    ("second.json", "first.json", True),
                ),
            }
            for name, pairs in scenarios.items():
                with self.subTest(name=name):
                    scenario = directory / name
                    scenario.mkdir()
                    ready = context.Queue()
                    start = context.Event()
                    results = context.Queue()
                    active = context.Value("i", 0)
                    maximum = context.Value("i", 0)

                    def worker(
                        output_name: str,
                        report_name: str,
                        emit_pin: bool,
                        *,
                        active=active,
                        maximum=maximum,
                        ready=ready,
                        results=results,
                        scenario=scenario,
                        start=start,
                    ) -> None:
                        def racing_build(*_args, **_kwargs):
                            with active.get_lock():
                                active.value += 1
                                maximum.value = max(maximum.value, active.value)
                            time.sleep(0.3)
                            with active.get_lock():
                                active.value -= 1
                            report = {
                                "schema_version": 1,
                                "prepared_count": 200 if emit_pin else 0,
                                "quarantined_count": 0 if emit_pin else 200,
                            }
                            return (
                                {"schema_version": 1} if emit_pin else None,
                                report,
                            )

                        ready.put("ready")
                        start.wait()
                        with mock.patch(
                            "bench.compare.build_pin.build_prepared_june_pin",
                            side_effect=racing_build,
                        ):
                            try:
                                status = write_prepared_june_artifacts(
                                    [],
                                    github_pr_cache=scenario / "pr-cache",
                                    repository_root=scenario / "repositories",
                                    output=scenario / output_name,
                                    quarantine_report=scenario / report_name,
                                    external_pin_sha256="b" * 64,
                                    recorded_order_sha256="c" * 64,
                                    parquet_sha256="d" * 64,
                                )
                            except PinError as exc:
                                results.put(("error", str(exc)))
                            else:
                                results.put(("status", status))

                    processes = [
                        context.Process(target=worker, args=pair)
                        for pair in pairs
                    ]
                    for process in processes:
                        process.start()
                    ready.get(timeout=5)
                    ready.get(timeout=5)
                    start.set()
                    for process in processes:
                        process.join(timeout=10)
                        self.assertEqual(process.exitcode, 0)
                    outcomes = [
                        results.get(timeout=5),
                        results.get(timeout=5),
                    ]

                    self.assertEqual(maximum.value, 1, outcomes)
                    self.assertEqual(
                        sum(kind == "status" for kind, _value in outcomes),
                        1,
                        outcomes,
                    )
                    self.assertEqual(
                        sum(kind == "error" for kind, _value in outcomes),
                        1,
                        outcomes,
                    )

    def test_prepare_june_writes_exact_200_reverifiable_cases(self):
        from unittest import mock

        from bench.compare import run as runner
        from bench.compare.build_pin import (
            JUNE_EXTERNAL_PIN_SHA256,
            JUNE_PARQUET_SHA256,
            write_prepared_june_artifacts,
        )

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve()
            repository_root = directory / "repositories"
            repository = repository_root / "example" / "public"
            repository.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", repository], check=True)
            subprocess.run(
                ["git", "-C", repository, "config", "user.email", "fixture@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "config", "user.name", "Fixture"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "commit", "--allow-empty", "-qm", "base"],
                check=True,
            )
            base_commit = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            source = repository / "src" / "shared.py"
            source.parent.mkdir()
            source.write_text(
                "def locate():\n    return True\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", repository, "add", "src/shared.py"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repository, "commit", "-qm", "PR head"],
                check=True,
            )
            head_commit = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            subprocess.run(
                [
                    "git",
                    "-C",
                    repository,
                    "checkout",
                    "-qb",
                    "base-for-merge",
                    base_commit,
                ],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    repository,
                    "merge",
                    "--no-ff",
                    "-qm",
                    "merged PR",
                    head_commit,
                ],
                check=True,
            )
            merge_commit = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            cache = directory / "pr-cache"
            responses = cache / "responses"
            responses.mkdir(parents=True)
            index = {"schema_version": 1, "responses": {}}
            cases = []
            for number in range(1, 201):
                case_id = f"example__public-{number}"
                cases.append(
                    {
                        "case_id": case_id,
                        "category": "Bug",
                        "query": f"Locate shared behavior for case {number}.",
                        "repository": {
                            "url": "https://github.com/example/public",
                            "revision": base_commit,
                        },
                        "oracle": {
                            "files": ["src/shared.py"],
                            "classes": ["locate"],
                            "functions": ["locate"],
                        },
                    }
                )
                api_url = (
                    "https://api.github.com/repos/"
                    f"example/public/pulls/{number}"
                )
                response = {
                    "url": api_url,
                    "html_url": (
                        f"https://github.com/example/public/pull/{number}"
                    ),
                    "number": number,
                    "state": "closed",
                    "merged": True,
                    "merge_commit_sha": merge_commit,
                    "base": {
                        "sha": base_commit,
                        "repo": {"full_name": "example/public"},
                    },
                    "head": {
                        "sha": head_commit,
                        "repo": {"full_name": "example/public"},
                    },
                }
                encoded = (
                    json.dumps(response, sort_keys=True).encode("utf-8")
                    + b"\n"
                )
                response_sha256 = hashlib.sha256(encoded).hexdigest()
                relative = f"responses/{response_sha256}.json"
                (cache / relative).write_bytes(encoded)
                index["responses"][api_url] = {
                    "sha256": response_sha256,
                    "path": relative,
                }
            (cache / "index.json").write_text(
                json.dumps(index, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            output = directory / "prepared.json"
            quarantine = directory / "quarantine.json"
            selected_ids = [case["case_id"] for case in cases]
            recorded_order_sha256 = hashlib.sha256(
                ("\n".join(selected_ids) + "\n").encode("utf-8")
            ).hexdigest()

            status = write_prepared_june_artifacts(
                cases,
                github_pr_cache=cache,
                repository_root=repository_root,
                output=output,
                quarantine_report=quarantine,
                external_pin_sha256=JUNE_EXTERNAL_PIN_SHA256,
                recorded_order_sha256=recorded_order_sha256,
                parquet_sha256=JUNE_PARQUET_SHA256,
            )
            with mock.patch.object(
                runner,
                "JUNE_RECORDED_ORDER_SHA256",
                recorded_order_sha256,
            ):
                pin, validated, _pin_sha256, encoded_pin = runner.load_case_pin(
                    output,
                    repository_root=repository_root,
                )
            quarantine_exists = quarantine.exists()

        self.assertEqual(status, 0)
        self.assertFalse(quarantine_exists)
        self.assertEqual(pin["pin_id"], "locbench-june-n200-prepared-v1")
        self.assertEqual(len(validated), 200)
        self.assertEqual([case["case_id"] for case in validated], selected_ids)
        self.assertEqual(
            pin["generation"]["selected_instance_ids"],
            selected_ids,
        )
        self.assertEqual(
            encoded_pin,
            json.dumps(
                pin,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            + b"\n",
        )

    def _git_backed_sources(
        self,
        directory: Path,
    ) -> tuple[dict, dict, Path]:
        categories = (
            "Bug Report",
            "Feature Request",
            "Performance Issue",
            "Security Vulnerability",
        )
        repository_root = directory / "repositories"
        cases = []
        audits = []
        for category_index, category in enumerate(categories):
            slug = f"example/public-{category_index}"
            repo = repository_root / slug
            repo.mkdir(parents=True)
            subprocess.run(["git", "init", "-q", repo], check=True)
            subprocess.run(
                ["git", "-C", repo, "config", "user.email", "fixture@example.invalid"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repo, "config", "user.name", "Fixture"],
                check=True,
            )
            subprocess.run(
                ["git", "-C", repo, "commit", "--allow-empty", "-qm", "base"],
                check=True,
            )
            base_commit = subprocess.check_output(
                ["git", "-C", repo, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            source_cases = []
            for index in range(12):
                case_id = f"case-{category_index}-{index:02d}"
                file_name = f"src/{case_id}.py"
                target = repo / file_name
                target.parent.mkdir(parents=True, exist_ok=True)
                content = f"def function_{index}():\n    return True\n"
                if case_id == "case-0-01":
                    content = "VALUE = True\n"
                target.write_text(content, encoding="utf-8")
                source_cases.append(
                    {
                        "instance_id": case_id,
                        "category": category,
                        "query": f"Locate {case_id}.",
                        "repo": slug,
                        "base_commit": base_commit,
                        "oracle": {
                            "files": [file_name],
                            "classes": [],
                            "functions": [f"function_{index}"],
                        },
                    }
                )
            subprocess.run(["git", "-C", repo, "add", "src"], check=True)
            subprocess.run(
                ["git", "-C", repo, "commit", "-qm", "audited changes"],
                check=True,
            )
            head_commit = subprocess.check_output(
                ["git", "-C", repo, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            cases.extend(source_cases)
            audits.extend(
                {
                    "instance_id": case["instance_id"],
                    "repository": slug,
                    "base_commit": base_commit,
                    "head_commit": head_commit,
                }
                for case in source_cases
            )
        cases[0]["oracle"]["files"] = ["src/disputed.py"]
        return (
            {"schema_version": 1, "cases": cases},
            {"schema_version": 1, "cases": audits},
            repository_root,
        )

    def test_n40_generation_is_balanced_deterministic_and_audited(self):
        from bench.compare.build_pin import build_balanced_pin

        with tempfile.TemporaryDirectory() as tmp:
            source, audit_evidence, repository_root = self._git_backed_sources(
                Path(tmp)
            )
            arguments = {
                "source_sha256": "a" * 64,
                "source_repository": "czlll/Loc-Bench_V1",
                "source_revision": "b" * 40,
                "source_path": "data/public-labels.json",
                "audit_evidence": audit_evidence,
                "audit_evidence_sha256": "c" * 64,
                "audit_evidence_path": "data/public-audit-evidence.json",
                "repository_root": repository_root,
                "seed": 42,
            }
            first = build_balanced_pin(source, **arguments)
            second = build_balanced_pin(source, **arguments)

        self.assertEqual(first, second)
        self.assertEqual(len(first["cases"]), 40)
        counts = {
            category: sum(
                case["category"] == category for case in first["cases"]
            )
            for category in ("Bug", "Feature", "Performance", "Security")
        }
        self.assertEqual(counts, {category: 10 for category in counts})
        self.assertEqual(first["generation"]["seed"], 42)
        self.assertEqual(first["generation"]["selection"], "sha256_priority_v1")
        self.assertTrue(
            {"case-0-00", "case-0-01"}
            <= {
                item["case_id"]
                for item in first["label_audit"]["quarantined"]
            },
        )
        for case in first["cases"]:
            self.assertEqual(case["label_audit"]["status"], "verified")
            self.assertEqual(
                case["label_audit"]["changed_files_source"],
                "git_diff_base_head_v1",
            )
            self.assertEqual(
                case["label_audit"]["symbol_verification"],
                "definition_pattern_v1",
            )
            self.assertEqual(
                case["label_audit"]["verifier"],
                "pinned_git_objects_v1",
            )
            self.assertRegex(
                case["label_audit"]["audit_record_sha256"],
                r"^[0-9a-f]{64}$",
            )
            self.assertTrue(
                set(case["oracle"]["files"])
                <= set(case["label_audit"]["changed_files"])
            )

    def test_n40_generation_batches_git_integrity_by_repository(self):
        from unittest import mock

        from bench.compare import build_pin

        with tempfile.TemporaryDirectory() as tmp:
            source, audit_evidence, repository_root = self._git_backed_sources(
                Path(tmp)
            )
            real_fsck = build_pin._verify_git_object_integrity
            real_snapshot = build_pin._snapshot_git_object_store
            with (
                mock.patch.object(
                    build_pin,
                    "_verify_git_object_integrity",
                    wraps=real_fsck,
                ) as fsck,
                mock.patch.object(
                    build_pin,
                    "_snapshot_git_object_store",
                    wraps=real_snapshot,
                ) as snapshot,
            ):
                pin = build_pin.build_balanced_pin(
                    source,
                    source_sha256="a" * 64,
                    source_repository="czlll/Loc-Bench_V1",
                    source_revision="b" * 40,
                    source_path="data/public-labels.json",
                    audit_evidence=audit_evidence,
                    audit_evidence_sha256="c" * 64,
                    audit_evidence_path="data/public-audit-evidence.json",
                    repository_root=repository_root,
                    seed=42,
                )

        self.assertEqual(len(pin["cases"]), 40)
        self.assertEqual(fsck.call_count, 4)
        self.assertEqual(snapshot.call_count, 8)

    def test_n40_generation_preserves_non_python_definition_provenance(self):
        from bench.compare.build_pin import build_balanced_pin

        with tempfile.TemporaryDirectory() as tmp:
            source, audit_evidence, repository_root = self._git_backed_sources(
                Path(tmp)
            )
            source_case = next(
                case
                for case in source["cases"]
                if case["instance_id"] == "case-0-02"
            )
            repository = repository_root / source_case["repo"]
            rust_path = "src/case-0-02.rs"
            python_path = repository / source_case["oracle"]["files"][0]
            python_path.unlink()
            rust_source = repository / rust_path
            rust_source.write_text(
                "struct Engine;\n\nfn execute() {}\n",
                encoding="utf-8",
            )
            subprocess.run(
                ["git", "-C", repository, "add", "-A", "src"],
                check=True,
            )
            subprocess.run(
                [
                    "git",
                    "-C",
                    repository,
                    "-c",
                    "user.email=fixture@example.invalid",
                    "-c",
                    "user.name=Fixture",
                    "commit",
                    "-qm",
                    "replace Python fixture with Rust",
                ],
                check=True,
            )
            head_commit = subprocess.check_output(
                ["git", "-C", repository, "rev-parse", "HEAD"],
                text=True,
            ).strip()
            source_case["oracle"] = {
                "files": [rust_path],
                "classes": ["crate.Engine"],
                "functions": ["crate.execute"],
            }
            audit_case = next(
                case
                for case in audit_evidence["cases"]
                if case["instance_id"] == source_case["instance_id"]
            )
            audit_case["head_commit"] = head_commit

            pin = build_balanced_pin(
                source,
                source_sha256="a" * 64,
                source_repository="czlll/Loc-Bench_V1",
                source_revision="b" * 40,
                source_path="data/public-labels.json",
                audit_evidence=audit_evidence,
                audit_evidence_sha256="c" * 64,
                audit_evidence_path="data/public-audit-evidence.json",
                repository_root=repository_root,
                seed=42,
            )

        selected = {
            case["case_id"]: case for case in pin["cases"]
        }
        self.assertIn(source_case["instance_id"], selected)
        self.assertEqual(
            selected[source_case["instance_id"]]["label_audit"][
                "symbol_verification"
            ],
            "definition_pattern_v1",
        )

    def test_n40_generation_refuses_incomplete_or_unpinned_source(self):
        from bench.compare.build_pin import PinError, build_balanced_pin

        with tempfile.TemporaryDirectory() as tmp:
            source, audit_evidence, repository_root = self._git_backed_sources(
                Path(tmp)
            )
            common = {
                "source_sha256": "a" * 64,
                "source_repository": "czlll/Loc-Bench_V1",
                "source_revision": "b" * 40,
                "source_path": "data/public-labels.json",
                "audit_evidence_sha256": "c" * 64,
                "audit_evidence_path": "data/public-audit-evidence.json",
                "repository_root": repository_root,
                "seed": 42,
            }
            incomplete = {"schema_version": 1, "cases": source["cases"][:9]}
            incomplete_audit = {
                "schema_version": 1,
                "cases": audit_evidence["cases"][:9],
            }
            with self.assertRaisesRegex(PinError, "eligible"):
                build_balanced_pin(
                    incomplete,
                    audit_evidence=incomplete_audit,
                    **common,
                )
            with self.assertRaisesRegex(PinError, "revision"):
                build_balanced_pin(
                    source,
                    audit_evidence=audit_evidence,
                    **{**common, "source_revision": "main"},
                )

            invented = json.loads(json.dumps(audit_evidence))
            invented["cases"][0]["head_commit"] = "f" * 40
            with self.assertRaisesRegex(PinError, "Git object"):
                build_balanced_pin(
                    source,
                    audit_evidence=invented,
                    **common,
                )

    def test_external_june_pin_is_verified_by_hash_without_copying_cases(self):
        from bench.compare.build_pin import verify_external_pin

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve()
            external = {
                "schema_version": 1,
                "n": 200,
                "score_depth": 10,
                "recorded_order_sha256": "c" * 64,
                "pinned_instance_ids": [
                    f"case-{index}" for index in range(200)
                ],
                "cases": [
                    {
                        "instance_id": f"case-{index}",
                        "repo": "example/public",
                        "base_commit": "d" * 40,
                        "category": "Bug Report",
                    }
                    for index in range(200)
                ],
            }
            external_path = directory / "locbench-n200-pin.json"
            external_path.write_text(
                json.dumps(external, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            reference = {
                "schema_version": 1,
                "kind": "external_content_address",
                "repository": "redacted-org/code-graph",
                "source_revision": "e" * 40,
                "path": "bench/accuracy/example.json",
                "sha256": hashlib.sha256(external_path.read_bytes()).hexdigest(),
                "expected_count": 200,
                "score_depth": 10,
                "recorded_order_sha256": "c" * 64,
                "availability": "published",
            }
            ordered = "\n".join(external["pinned_instance_ids"]) + "\n"
            external["recorded_order_sha256"] = hashlib.sha256(
                ordered.encode("utf-8")
            ).hexdigest()
            reference["recorded_order_sha256"] = external[
                "recorded_order_sha256"
            ]
            external_path.write_text(
                json.dumps(external, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            reference["sha256"] = hashlib.sha256(
                external_path.read_bytes()
            ).hexdigest()

            verified = verify_external_pin(reference, external_path)
            self.assertEqual(verified["verified_count"], 200)
            self.assertEqual(verified["sha256"], reference["sha256"])
            self.assertEqual(
                verified["status"],
                "address_verified_not_runnable",
            )
            self.assertFalse(verified["runnable"])
            self.assertEqual(
                verified["blockers"],
                ["missing_query_oracle_labels"],
            )

            invented = json.loads(json.dumps(external))
            for case in invented["cases"]:
                case["query"] = "Invented query"
                case["oracle"] = {
                    "files": ["src/invented.py"],
                    "classes": [],
                    "functions": ["invented"],
                }
                case["label_audit"] = {
                    "status": "verified",
                    "changed_files": ["src/invented.py"],
                }
            external_path.write_text(
                json.dumps(invented, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            invented_reference = {
                **reference,
                "availability": "published",
                "sha256": hashlib.sha256(external_path.read_bytes()).hexdigest(),
            }
            invented_result = verify_external_pin(
                invented_reference,
                external_path,
            )
            self.assertFalse(invented_result["runnable"])
            self.assertIn(
                "git_object_label_provenance_not_verified",
                invented_result["blockers"],
            )

            tampered = json.loads(json.dumps(external))
            tampered["recorded_order_sha256"] = "0" * 64
            external_path.write_text(
                json.dumps(tampered, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            tampered_reference = dict(reference)
            tampered_reference["sha256"] = hashlib.sha256(
                external_path.read_bytes()
            ).hexdigest()
            tampered_reference["recorded_order_sha256"] = "0" * 64
            with self.assertRaisesRegex(Exception, "recorded order"):
                verify_external_pin(tampered_reference, external_path)

            external_path.write_text(
                json.dumps(external, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            external["cases"].pop()
            external_path.write_text(
                json.dumps(external, sort_keys=True) + "\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(Exception, "SHA-256"):
                verify_external_pin(reference, external_path)

    def test_external_june_pin_hash_and_parse_share_one_byte_snapshot(self):
        from unittest import mock

        from bench.compare.build_pin import verify_external_pin

        external = {
            "schema_version": 1,
            "n": 200,
            "score_depth": 10,
            "pinned_instance_ids": [
                f"example__public-{index}" for index in range(1, 201)
            ],
            "cases": [
                {
                    "instance_id": f"example__public-{index}",
                    "repo": "example/public",
                    "base_commit": "a" * 40,
                    "category": "Bug Report",
                }
                for index in range(1, 201)
            ],
        }
        external["recorded_order_sha256"] = hashlib.sha256(
            (
                "\n".join(external["pinned_instance_ids"]) + "\n"
            ).encode("utf-8")
        ).hexdigest()
        encoded = (
            json.dumps(external, sort_keys=True).encode("utf-8") + b"\n"
        )
        reference = {
            "schema_version": 1,
            "kind": "external_content_address",
            "repository": "example/pins",
            "source_revision": "b" * 40,
            "path": "pins/june.json",
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "expected_count": 200,
            "score_depth": 10,
            "recorded_order_sha256": external[
                "recorded_order_sha256"
            ],
            "availability": "published",
        }
        missing_path = Path("/must/not/be/reopened.json")

        with mock.patch(
            "bench.compare.build_pin._read_input_snapshot",
            return_value=encoded,
        ) as read_snapshot:
            verified = verify_external_pin(reference, missing_path)

        read_snapshot.assert_called_once_with(
            missing_path,
            "external pin",
        )
        self.assertEqual(verified["sha256"], reference["sha256"])
        self.assertEqual(verified["verified_count"], 200)
        self.assertEqual(
            verified["blockers"],
            ["missing_query_oracle_labels"],
        )

    def test_checked_in_june_reference_contains_no_private_case_data(self):
        reference_path = (
            COMPARE / "pins" / "locbench-june-n200.external.json"
        )
        reference = json.loads(reference_path.read_text(encoding="utf-8"))

        self.assertEqual(reference["expected_count"], 200)
        self.assertEqual(
            reference["sha256"],
            "886156bbd16eb753a690da6bcb452f9238f53ef28409b1f4e483b842a0556453",
        )
        self.assertNotIn("cases", reference)
        self.assertNotIn("queries", reference)
        self.assertEqual(reference["availability"], "published")
        self.assertEqual(
            reference["source_revision"],
            "d7b93959dace3215cd096a13c1a27e259063dc95",
        )
        self.assertFalse(reference["runnable"])
        self.assertEqual(
            reference["blockers"],
            ["missing_query_oracle_labels"],
        )

    def test_ci_runs_good_and_bad_instrument_falsifiers_without_secrets(self):
        workflow = (ROOT / ".github" / "workflows" / "validate.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("Gate the five-arm comparison instrument", workflow)
        self.assertIn("five-arm-good.json", workflow)
        self.assertIn("five-arm-bad.json", workflow)
        compare_step = workflow.split(
            "- name: Gate the five-arm comparison instrument", 1
        )[1]
        self.assertNotIn("secrets.", compare_step)
        self.assertNotIn("continue-on-error", compare_step)

    def test_five_arm_fixture_has_seven_cases_and_a_content_addressed_canary(self):
        pin_path = COMPARE / "pins" / "fixture-public-n7.json"
        pin = json.loads(pin_path.read_text(encoding="utf-8"))
        canary = pin["dataset"]["instrument_canary"]
        canary_path = ROOT / canary["path"]

        self.assertEqual(len(pin["cases"]), 7)
        self.assertEqual(
            sum(case.get("injection_canary") is True for case in pin["cases"]),
            1,
        )
        self.assertEqual(
            hashlib.sha256(canary_path.read_bytes()).hexdigest(),
            canary["sha256"],
        )
        manifest = json.loads(
            (ROOT / pin["dataset"]["instrument_fixture"]["manifest_path"]).read_text(
                encoding="utf-8"
            )
        )
        source_root = Path(
            pin["dataset"]["instrument_fixture"]["source_root"]
        )
        canary_relative = Path(canary["path"]).relative_to(source_root).as_posix()
        self.assertIn(canary_relative, manifest["files"])
        canary_text = canary_path.read_text(encoding="utf-8")
        self.assertIn(canary["write_path_environment"], canary_text)
        self.assertIn(canary["secret_environment"], canary_text)
        self.assertIn(canary["network_environment"], canary_text)

        for fixture_name in ("five-arm-good.json", "five-arm-bad.json"):
            fixture = json.loads(
                (COMPARE / "fixtures" / fixture_name).read_text(encoding="utf-8")
            )
            self.assertEqual(
                fixture["kind"],
                "deterministic_executor_fault_plan_v1",
            )
            self.assertEqual(
                fixture["cases_sha256"],
                hashlib.sha256(pin_path.read_bytes()).hexdigest(),
            )
            self.assertNotIn("results", fixture)
            self.assertNotIn("oracle", json.dumps(fixture))
            self.assertNotIn("evidence_bytes", json.dumps(fixture))
        good = json.loads(
            (COMPARE / "fixtures" / "five-arm-good.json").read_text(encoding="utf-8")
        )
        bad = json.loads(
            (COMPARE / "fixtures" / "five-arm-bad.json").read_text(encoding="utf-8")
        )
        self.assertEqual(good["faults"], [])
        self.assertEqual(len(bad["faults"]), 1)


if __name__ == "__main__":
    unittest.main()
