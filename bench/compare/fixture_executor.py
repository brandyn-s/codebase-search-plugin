#!/usr/bin/env python3
"""Oracle-blind deterministic executor for the instrument-only target repository."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
from pathlib import Path
import re
import sys


def _reject_package_initializer() -> None:
    initializer = Path(__file__).resolve().parent / "__init__.py"
    if initializer.exists() or initializer.is_symlink():
        print(
            "ERROR: bench/compare must remain a namespace package; "
            "refusing executable package initializer",
            file=sys.stderr,
        )
        raise SystemExit(1)


_reject_package_initializer()

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bench.compare.schema import ARM_CONTRACTS  # noqa: E402


class FixtureExecutorError(ValueError):
    """The instrument repository or requested fixture arm is invalid."""


_WORD = re.compile(r"[A-Za-z0-9]+")
_TRACES = {
    "corpus": [],
    "native": ["Glob", "Grep", "Read"],
    "code-search": ["mcp__code-search__search_code", "Read"],
    "code-graph": ["mcp__code-graph__search_graph", "Read"],
    "composed": [
        "mcp__code-search__search_code",
        "mcp__code-graph__search_graph",
        "Read",
    ],
}


def _terms(value: str) -> set[str]:
    terms: set[str] = set()
    for raw in _WORD.findall(value.replace("_", " ")):
        term = raw.casefold()
        terms.add(term)
        for suffix in ("ing", "ed", "es", "s"):
            if term.endswith(suffix) and len(term) > len(suffix) + 2:
                terms.add(term[: -len(suffix)])
    return terms


def _verified_sources(source_root: Path, manifest: dict) -> dict[str, str]:
    if (
        manifest.get("schema_version") != 1
        or manifest.get("repository") != "fixture://codebase-search-e2e-v1"
        or not isinstance(manifest.get("files"), dict)
        or not manifest["files"]
    ):
        raise FixtureExecutorError("fixture manifest is malformed")
    root = Path(source_root).resolve()
    actual_paths: set[str] = set()
    for candidate in root.rglob("*"):
        if candidate.is_symlink():
            raise FixtureExecutorError("fixture repository contains a symlink")
        if candidate.is_file():
            actual_paths.add(candidate.relative_to(root).as_posix())
    if actual_paths != set(manifest["files"]):
        raise FixtureExecutorError("fixture repository file set differs from manifest")
    sources: dict[str, str] = {}
    actual_hashes: dict[str, str] = {}
    for relative, expected in manifest["files"].items():
        path = root / relative
        try:
            encoded = path.read_bytes()
            text = encoded.decode("utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise FixtureExecutorError(f"{relative}: fixture source is not text") from exc
        digest = hashlib.sha256(encoded).hexdigest()
        if digest != expected:
            raise FixtureExecutorError(f"{relative}: fixture source hash mismatch")
        sources[relative] = text
        actual_hashes[relative] = digest
    canonical_tree = "\n".join(
        f"{relative}\0{actual_hashes[relative]}"
        for relative in sorted(actual_hashes)
    ).encode("utf-8")
    if hashlib.sha256(canonical_tree).hexdigest() != manifest.get("revision"):
        raise FixtureExecutorError("fixture repository revision is not reproducible")
    return sources


def _entities(path: str, source: str) -> list[dict]:
    if not path.endswith(".py"):
        return [{"file": path, "symbol": None, "kind": "module"}]
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        raise FixtureExecutorError(f"{path}: invalid Python fixture source") from exc
    entities = [
        {
            "file": path,
            "symbol": node.name,
            "kind": "class" if isinstance(node, ast.ClassDef) else "function",
        }
        for node in tree.body
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    return entities or [{"file": path, "symbol": None, "kind": "module"}]


def execute_unit(
    *,
    arm: str,
    query: str,
    source_root: Path,
    manifest: dict,
    canary_relative: str,
    canary_challenge: str,
) -> dict:
    if arm not in ARM_CONTRACTS or arm not in _TRACES:
        raise FixtureExecutorError(f"unknown fixture arm: {arm}")
    if not isinstance(query, str) or not query.strip():
        raise FixtureExecutorError("fixture query must be nonempty")
    sources = _verified_sources(source_root, manifest)
    if canary_relative not in sources:
        raise FixtureExecutorError("injection canary is absent from fixture repository")
    if re.fullmatch(r"[0-9a-f]{64}", canary_challenge) is None:
        raise FixtureExecutorError("canary challenge must be a SHA-256 value")
    canary_read_proof = hashlib.sha256(
        bytes.fromhex(canary_challenge)
        + b"\0"
        + sources[canary_relative].encode("utf-8")
    ).hexdigest()
    query_terms = _terms(query)
    candidates: list[dict] = []
    for path, source in sources.items():
        content_terms = _terms(source)
        path_terms = _terms(path)
        for entity in _entities(path, source):
            symbol_terms = _terms(entity["symbol"] or "")
            score = (
                4 * len(query_terms & symbol_terms)
                + 2 * len(query_terms & path_terms)
                + len(query_terms & content_terms)
            )
            candidates.append({**entity, "score": score})
    ranked = sorted(
        candidates,
        key=lambda item: (
            -item["score"],
            len(item["file"]),
            item["file"],
            item["symbol"] or "",
        ),
    )
    selected = ranked[:10]
    evidence_files = sorted({item["file"] for item in selected})
    return {
        "schema_version": 1,
        "executor": "deterministic_instrumented_fixture_v1",
        "arm": arm,
        "ranked_entities": [
            {
                "rank": rank,
                "file": item["file"],
                "symbol": item["symbol"],
            }
            for rank, item in enumerate(selected, 1)
        ],
        "candidate_count": len(ranked),
        "effective_k": len(selected),
        "truncated": len(ranked) > len(selected),
        "trace": list(_TRACES[arm]),
        "files_read": sorted(sources),
        "evidence_files": evidence_files,
        "canary_read": canary_relative in sources,
        "canary_read_proof": canary_read_proof,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--canary-relative", required=True)
    parser.add_argument("--canary-challenge", required=True)
    arguments = parser.parse_args(argv)
    try:
        manifest = json.loads(arguments.manifest.read_text(encoding="utf-8"))
        result = execute_unit(
            arm=arguments.arm,
            query=arguments.query,
            source_root=arguments.source_root,
            manifest=manifest,
            canary_relative=arguments.canary_relative,
            canary_challenge=arguments.canary_challenge,
        )
    except (OSError, json.JSONDecodeError, FixtureExecutorError) as exc:
        parser.exit(1, f"ERROR: {exc}\n")
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
