"""Acceptance tests for the pinned multi-language SCIP preparation boundary."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import textwrap
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "prepare_scip_index.py"


def _run(*args: str, cwd: Path, env: dict[str, str] | None = None):
    return subprocess.run(
        [*args],
        cwd=cwd,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def _platform_key() -> str:
    system = platform.system().lower()
    machine = platform.machine().lower()
    architecture = {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }[machine]
    return f"{system}-{architecture}"


class PrepareSCIPIndexAcceptanceTests(unittest.TestCase):
    def _repository(self, root: Path) -> Path:
        repository = root / "repository"
        repository.mkdir()
        _run("git", "init", "-q", str(repository), cwd=root)
        _run(
            "git",
            "-C",
            str(repository),
            "config",
            "user.email",
            "test@example.com",
            cwd=root,
        )
        _run(
            "git",
            "-C",
            str(repository),
            "config",
            "user.name",
            "SCIP Test",
            cwd=root,
        )
        (repository / "go.mod").write_text(
            "module example.com/fixture\n\ngo 1.24\n",
            encoding="utf-8",
        )
        (repository / "main.go").write_text(
            "package main\n\nfunc main() {}\n",
            encoding="utf-8",
        )
        _run("git", "-C", str(repository), "add", ".", cwd=root)
        committed = _run(
            "git",
            "-C",
            str(repository),
            "commit",
            "-qm",
            "fixture",
            cwd=root,
        )
        self.assertEqual(committed.returncode, 0, committed.stderr)
        return repository

    def _generator(self, root: Path) -> Path:
        generator = root / "scip-go"
        generator.write_text(
            textwrap.dedent(
                """\
                #!/usr/bin/env python3
                import os
                from pathlib import Path
                import sys

                if sys.argv[1:] == ["--version"]:
                    print("0.2.7")
                    raise SystemExit(0)
                if not sys.argv[1:] or sys.argv[1] != "index":
                    raise SystemExit(64)
                counter = os.environ.get("FAKE_SCIP_COUNTER")
                if counter:
                    path = Path(counter)
                    prior = int(path.read_text() or "0") if path.exists() else 0
                    path.write_text(str(prior + 1))
                if os.environ.get("FAKE_SCIP_MUTATE") == "1":
                    Path("main.go").write_text("package main\\n// mutated\\n")
                if os.environ.get("FAKE_SCIP_FAIL") == "1":
                    raise SystemExit(7)
                output = Path(sys.argv[sys.argv.index("--output") + 1])
                output.write_bytes(b"canonical-scip-fixture")
                """
            ),
            encoding="utf-8",
        )
        generator.chmod(0o755)
        return generator

    def _typescript_repository(self, root: Path) -> Path:
        repository = root / "typescript-repository"
        repository.mkdir()
        _run("git", "init", "-q", str(repository), cwd=root)
        _run(
            "git",
            "-C",
            str(repository),
            "config",
            "user.email",
            "test@example.com",
            cwd=root,
        )
        _run(
            "git",
            "-C",
            str(repository),
            "config",
            "user.name",
            "SCIP Test",
            cwd=root,
        )
        (repository / ".gitignore").write_text("node_modules/\n", encoding="utf-8")
        (repository / "package.json").write_text(
            '{"name":"typed-fixture","private":true,"version":"1.0.0"}\n',
            encoding="utf-8",
        )
        (repository / "package-lock.json").write_text(
            '{"name":"typed-fixture","lockfileVersion":3,"packages":{}}\n',
            encoding="utf-8",
        )
        (repository / "tsconfig.json").write_text(
            '{"compilerOptions":{"strict":true},"include":["src/**/*.ts"]}\n',
            encoding="utf-8",
        )
        source = repository / "src"
        source.mkdir()
        (source / "main.ts").write_text(
            "export function greet(name: string): string { return `hi ${name}`; }\n",
            encoding="utf-8",
        )
        dependency = repository / "node_modules" / "typescript"
        dependency.mkdir(parents=True)
        (dependency / "package.json").write_text(
            '{"name":"typescript","version":"5.9.3"}\n',
            encoding="utf-8",
        )
        _run("git", "-C", str(repository), "add", ".", cwd=root)
        committed = _run(
            "git",
            "-C",
            str(repository),
            "commit",
            "-qm",
            "fixture",
            cwd=root,
        )
        self.assertEqual(committed.returncode, 0, committed.stderr)
        return repository

    def _typescript_generator(self, root: Path) -> Path:
        generator = root / "scip-typescript.js"
        generator.write_text(
            textwrap.dedent(
                """\
                const fs = require("fs");
                const path = require("path");
                const args = process.argv.slice(2);
                if (args.length === 1 && args[0] === "--version") {
                  console.log("0.4.0");
                  process.exit(0);
                }
                if (args[0] !== "index") process.exit(64);
                const counter = process.env.FAKE_SCIP_COUNTER;
                if (counter) {
                  const prior = fs.existsSync(counter) ? Number(fs.readFileSync(counter)) : 0;
                  fs.writeFileSync(counter, String(prior + 1));
                }
                if (process.env.FAKE_SCIP_MUTATE === "1") {
                  fs.writeFileSync(path.join("src", "main.ts"), "// mutated\\n");
                }
                if (process.env.FAKE_SCIP_FAIL === "1") process.exit(7);
                const output = args[args.indexOf("--output") + 1];
                fs.writeFileSync(output, Buffer.from("canonical-typescript-scip-fixture"));
                """
            ),
            encoding="utf-8",
        )
        return generator

    def _bom(self, root: Path, generator: Path) -> Path:
        digest = hashlib.sha256(generator.read_bytes()).hexdigest()
        key = _platform_key()
        bom = root / "component-bom.json"
        bom.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "precision_generators": {
                        "go-scip": {
                            "kind": "github-release",
                            "repository": "scip-code/scip-go",
                            "tag": "v0.2.7",
                            "source_revision": "2" * 40,
                            "version_output": "0.2.7",
                            "assets": {
                                key: {
                                    "name": f"scip-go-{key}.tar.gz",
                                    "archive_sha256": "a" * 64,
                                    "binary_sha256": digest,
                                }
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return bom

    def _typescript_bom(self, root: Path, generator: Path) -> Path:
        digest = hashlib.sha256(generator.read_bytes()).hexdigest()
        node_major = int(
            subprocess.run(
                ["node", "--version"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.removeprefix("v").split(".", 1)[0]
        )
        bom = root / "typescript-component-bom.json"
        bom.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "precision_generators": {
                        "typescript-scip": {
                            "kind": "npm-lockfile",
                            "package": "@sourcegraph/scip-typescript",
                            "version_output": "0.4.0",
                            "source_repository": "sourcegraph/scip-typescript",
                            "source_revision": "1962a68386220dd669c3839b69d64fb5ce34f2a6",
                            "package_integrity": (
                                "sha512-k+AtsrqmS41Sd5qjkZlHcmvoSQIvBOonRj4jpgp0"
                                "KNFM6aqvMGpdSuPUqrUcg8ENTKjUbfaUVszgQwq3bCOvwA=="
                            ),
                            "lockfile_sha256": "b" * 64,
                            "entrypoint_sha256": digest,
                            "supported_node_majors": [node_major],
                            "node_runtime": {
                                "version": subprocess.run(
                                    ["node", "--version"],
                                    check=True,
                                    capture_output=True,
                                    text=True,
                                ).stdout.strip(),
                                "assets": {
                                    _platform_key(): {
                                        "binary_sha256": hashlib.sha256(
                                            Path(
                                                subprocess.run(
                                                    ["which", "node"],
                                                    check=True,
                                                    capture_output=True,
                                                    text=True,
                                                ).stdout.strip()
                                            ).read_bytes()
                                        ).hexdigest()
                                    }
                                },
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        return bom

    def _prepare(
        self,
        repository: Path,
        generator: Path,
        bom: Path,
        cache: Path,
        *,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return _run(
            sys.executable,
            str(SCRIPT),
            "prepare",
            str(repository),
            "--generator",
            str(generator),
            "--component-bom",
            str(bom),
            "--cache-root",
            str(cache),
            cwd=ROOT,
            env=env,
        )

    def _prepare_typescript(
        self,
        repository: Path,
        generator: Path,
        bom: Path,
        cache: Path,
        *,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return _run(
            sys.executable,
            str(SCRIPT),
            "prepare",
            str(repository),
            "--language",
            "typescript",
            "--generator",
            str(generator),
            "--runtime",
            subprocess.run(
                ["which", "node"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout.strip(),
            "--component-bom",
            str(bom),
            "--cache-root",
            str(cache),
            cwd=ROOT,
            env=env,
        )

    def test_prepares_typescript_without_installing_or_mutating_the_target(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self._typescript_repository(root)
            generator = self._typescript_generator(root)
            bom = self._typescript_bom(root, generator)
            cache = root / "cache"
            counter = root / "counter"
            before = _run(
                "git", "-C", str(repository), "status", "--porcelain=v1", cwd=root
            ).stdout
            environment = os.environ.copy()
            environment["FAKE_SCIP_COUNTER"] = str(counter)

            completed = self._prepare_typescript(
                repository, generator, bom, cache, env=environment
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            receipt = json.loads(completed.stdout)
            self.assertEqual(receipt["status"], "ready")
            self.assertEqual(receipt["precision_tier"], "scip")
            self.assertEqual(
                receipt["generator"]["repository"],
                "sourcegraph/scip-typescript",
            )
            self.assertEqual(receipt["generator"]["version"], "0.4.0")
            self.assertTrue(receipt["generator"]["runtime_version"].startswith("v"))
            self.assertTrue(Path(receipt["index"]["path"]).is_file())
            self.assertFalse(Path(receipt["index"]["path"]).is_relative_to(repository))
            self.assertEqual(counter.read_text(), "1")
            after = _run(
                "git", "-C", str(repository), "status", "--porcelain=v1", cwd=root
            ).stdout
            self.assertEqual(after, before)

    def test_typescript_requires_an_existing_dependency_tree(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self._typescript_repository(root)
            shutil.rmtree(repository / "node_modules")
            generator = self._typescript_generator(root)
            bom = self._typescript_bom(root, generator)
            counter = root / "counter"
            environment = os.environ.copy()
            environment["FAKE_SCIP_COUNTER"] = str(counter)

            completed = self._prepare_typescript(
                repository,
                generator,
                bom,
                root / "cache",
                env=environment,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("existing node_modules dependency tree", completed.stderr)
            self.assertFalse(counter.exists())

    def test_auto_requires_an_explicit_language_for_mixed_roots(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self._repository(root)
            (repository / "tsconfig.json").write_text(
                '{"compilerOptions":{"strict":true}}\n',
                encoding="utf-8",
            )
            _run("git", "-C", str(repository), "add", "tsconfig.json", cwd=root)
            committed = _run(
                "git",
                "-C",
                str(repository),
                "commit",
                "-qm",
                "add TypeScript root",
                cwd=root,
            )
            self.assertEqual(committed.returncode, 0, committed.stderr)
            generator = self._generator(root)
            bom = self._bom(root, generator)

            completed = self._prepare(
                repository,
                generator,
                bom,
                root / "cache",
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("select --language go or typescript", completed.stderr)

    def test_prepares_and_reuses_a_revision_and_generator_bound_index(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self._repository(root)
            generator = self._generator(root)
            bom = self._bom(root, generator)
            cache = root / "cache"
            counter = root / "counter"
            environment = os.environ.copy()
            environment["FAKE_SCIP_COUNTER"] = str(counter)

            first = self._prepare(
                repository,
                generator,
                bom,
                cache,
                env=environment,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            first_receipt = json.loads(first.stdout)
            self.assertEqual(first_receipt["status"], "ready")
            self.assertEqual(first_receipt["precision_tier"], "scip")
            self.assertFalse(first_receipt["cached"])
            self.assertEqual(first_receipt["dirty_fingerprint"], "clean")
            self.assertEqual(first_receipt["generator"]["version"], "0.2.7")
            index_path = Path(first_receipt["index"]["path"])
            self.assertTrue(index_path.is_file())
            self.assertFalse(index_path.is_relative_to(repository))
            self.assertEqual(counter.read_text(), "1")

            second = self._prepare(
                repository,
                generator,
                bom,
                cache,
                env=environment,
            )
            self.assertEqual(second.returncode, 0, second.stderr)
            second_receipt = json.loads(second.stdout)
            self.assertTrue(second_receipt["cached"])
            self.assertEqual(second_receipt["index"], first_receipt["index"])
            self.assertEqual(counter.read_text(), "1")

    def test_rejects_a_cache_receipt_bound_to_different_generator_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self._repository(root)
            generator = self._generator(root)
            bom = self._bom(root, generator)
            cache = root / "cache"
            counter = root / "counter"
            environment = os.environ.copy()
            environment["FAKE_SCIP_COUNTER"] = str(counter)

            first = self._prepare(
                repository,
                generator,
                bom,
                cache,
                env=environment,
            )
            self.assertEqual(first.returncode, 0, first.stderr)
            receipt_path = next(cache.rglob("receipt.json"))
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            receipt["generator"]["source_revision"] = "f" * 40
            receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

            second = self._prepare(
                repository,
                generator,
                bom,
                cache,
                env=environment,
            )

            self.assertNotEqual(second.returncode, 0)
            self.assertIn("invalid SCIP cache entry", second.stderr)
            self.assertEqual(counter.read_text(), "1")

    def test_verify_subcommand_accepts_only_the_pinned_host_binary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            generator = self._generator(root)
            bom = self._bom(root, generator)

            completed = _run(
                sys.executable,
                str(SCRIPT),
                "verify",
                "--generator",
                str(generator),
                "--component-bom",
                str(bom),
                cwd=ROOT,
            )

            self.assertEqual(completed.returncode, 0, completed.stderr)
            receipt = json.loads(completed.stdout)
            self.assertEqual(receipt["status"], "ready")
            self.assertEqual(receipt["generator"]["version"], "0.2.7")
            self.assertEqual(receipt["platform"], _platform_key())
            self.assertEqual(
                receipt["generator"]["binary_sha256"],
                hashlib.sha256(generator.read_bytes()).hexdigest(),
            )

    def test_rejects_an_unpinned_generator_before_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self._repository(root)
            generator = self._generator(root)
            bom = self._bom(root, generator)
            cache = root / "cache"
            counter = root / "counter"
            generator.write_text(
                generator.read_text(encoding="utf-8") + "\n# changed\n",
                encoding="utf-8",
            )
            environment = os.environ.copy()
            environment["FAKE_SCIP_COUNTER"] = str(counter)

            completed = self._prepare(
                repository,
                generator,
                bom,
                cache,
                env=environment,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("binary SHA-256", completed.stderr)
            self.assertFalse(counter.exists())
            self.assertFalse(any(cache.rglob("index.scip")) if cache.exists() else False)

    def test_rejects_a_generator_that_mutates_the_checkout(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self._repository(root)
            generator = self._generator(root)
            bom = self._bom(root, generator)
            cache = root / "cache"
            environment = os.environ.copy()
            environment["FAKE_SCIP_MUTATE"] = "1"

            completed = self._prepare(
                repository,
                generator,
                bom,
                cache,
                env=environment,
            )

            self.assertNotEqual(completed.returncode, 0)
            self.assertIn("checkout changed", completed.stderr)
            self.assertFalse(any(cache.rglob("index.scip")) if cache.exists() else False)

    def test_rejects_dirty_or_non_module_inputs_before_generator_execution(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repository = self._repository(root)
            generator = self._generator(root)
            bom = self._bom(root, generator)
            cache = root / "cache"
            counter = root / "counter"
            environment = os.environ.copy()
            environment["FAKE_SCIP_COUNTER"] = str(counter)
            (repository / "main.go").write_text("package main\n// dirty\n")

            dirty = self._prepare(
                repository,
                generator,
                bom,
                cache,
                env=environment,
            )
            self.assertNotEqual(dirty.returncode, 0)
            self.assertIn("clean Git checkout", dirty.stderr)
            self.assertFalse(counter.exists())

            _run(
                "git",
                "-C",
                str(repository),
                "restore",
                "main.go",
                cwd=root,
            )
            (repository / "go.mod").unlink()
            _run(
                "git",
                "-C",
                str(repository),
                "add",
                "-u",
                cwd=root,
            )
            _run(
                "git",
                "-C",
                str(repository),
                "commit",
                "-qm",
                "remove module",
                cwd=root,
            )

            non_module = self._prepare(
                repository,
                generator,
                bom,
                cache,
                env=environment,
            )
            self.assertNotEqual(non_module.returncode, 0)
            self.assertIn("root go.mod", non_module.stderr)
            self.assertFalse(counter.exists())


if __name__ == "__main__":
    unittest.main()
