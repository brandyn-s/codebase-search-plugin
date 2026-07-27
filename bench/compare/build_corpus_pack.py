#!/usr/bin/env python3
"""Build an oracle-blind, deterministic corpus prompt from one pinned checkout."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys
from typing import Iterable


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

from bench.compare.token_accounting import (
    TokenAccountingError,
    count_tokens,
    tokenizer_descriptor,
)


BLOCK_LINES = 200
BLOCK_OVERLAP = 20
QUERY_TOKEN = re.compile(r"[A-Za-z0-9_]+")


class CorpusPackError(ValueError):
    """The checkout or requested pack violates the corpus-arm contract."""


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _git(repository: Path, *arguments: str, binary: bool = False) -> str | bytes:
    try:
        return subprocess.check_output(
            ["git", "-C", str(repository), *arguments],
            text=not binary,
            stderr=subprocess.PIPE,
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CorpusPackError(f"git {' '.join(arguments)} failed: {exc}") from exc


def _require_pinned_checkout(repository: Path, expected_revision: str) -> Path:
    root = repository.resolve()
    if not root.is_dir():
        raise CorpusPackError(f"repository is not a directory: {root}")
    if re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", expected_revision) is None:
        raise CorpusPackError("expected revision must be a full lowercase object ID")
    git_root = Path(str(_git(root, "rev-parse", "--show-toplevel")).strip()).resolve()
    if git_root != root:
        raise CorpusPackError(
            f"repository must be the pinned Git repository root: {git_root}"
        )
    actual = str(_git(root, "rev-parse", "HEAD")).strip()
    if actual != expected_revision:
        raise CorpusPackError(
            f"checkout revision mismatch: expected {expected_revision}, got {actual}"
        )
    for arguments in (("diff", "--quiet", "HEAD", "--"), ("diff", "--cached", "--quiet", "HEAD", "--")):
        try:
            subprocess.run(
                ["git", "-C", str(root), *arguments],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise CorpusPackError(
                "tracked checkout content differs from the pinned revision"
            ) from exc
    return root


def _tracked_text_files(repository: Path) -> list[tuple[str, str]]:
    raw = _git(repository, "ls-files", "-z", binary=True)
    assert isinstance(raw, bytes)
    records: list[tuple[str, str]] = []
    for encoded in raw.split(b"\0"):
        if not encoded:
            continue
        try:
            relative = encoded.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CorpusPackError("tracked path is not UTF-8") from exc
        candidate = repository / relative
        if candidate.is_symlink() or not candidate.is_file():
            continue
        resolved = candidate.resolve()
        try:
            resolved.relative_to(repository)
        except ValueError as exc:
            raise CorpusPackError(f"tracked path escapes checkout: {relative}") from exc
        data = resolved.read_bytes()
        if b"\0" in data:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            continue
        records.append((relative, text))
    return sorted(records)


def _blocks(files: Iterable[tuple[str, str]]) -> list[dict]:
    blocks: list[dict] = []
    step = BLOCK_LINES - BLOCK_OVERLAP
    for path, text in files:
        lines = text.splitlines()
        if not lines:
            lines = [""]
        for start_index in range(0, len(lines), step):
            selected = lines[start_index : start_index + BLOCK_LINES]
            if not selected:
                break
            start_line = start_index + 1
            end_line = start_index + len(selected)
            body = "\n".join(selected) + "\n"
            rendered = (
                f"===== {path}:{start_line}-{end_line} =====\n"
                f"{body}"
            )
            blocks.append(
                {
                    "path": path,
                    "start_line": start_line,
                    "end_line": end_line,
                    "body": body,
                    "rendered": rendered,
                }
            )
            if end_line == len(lines):
                break
    return blocks


def _query_tokens(query: str) -> set[str]:
    tokens = {token.casefold() for token in QUERY_TOKEN.findall(query)}
    if not tokens:
        raise CorpusPackError("query must contain at least one searchable token")
    return tokens


def _rank(blocks: list[dict], query: str) -> list[dict]:
    query_tokens = _query_tokens(query)
    for block in blocks:
        block_tokens = {
            token.casefold()
            for token in QUERY_TOKEN.findall(block["path"] + "\n" + block["body"])
        }
        block["query_token_matches"] = len(query_tokens & block_tokens)
    return sorted(
        blocks,
        key=lambda block: (
            -block["query_token_matches"],
            len(block["path"]),
            block["path"],
            block["start_line"],
        ),
    )


def build_pack(
    *,
    repository: Path,
    expected_revision: str,
    query: str,
    evidence_token_budget: int,
    context_token_budget: int,
) -> dict:
    """Return a corpus pack built without accepting labels or model/tool output."""
    for name, value in (
        ("evidence token budget", evidence_token_budget),
        ("context token budget", context_token_budget),
    ):
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise CorpusPackError(f"{name} must be a positive integer")
    if evidence_token_budget > context_token_budget:
        raise CorpusPackError("evidence token budget exceeds the context ceiling")
    root = _require_pinned_checkout(Path(repository), expected_revision)
    files = _tracked_text_files(root)
    if not files:
        raise CorpusPackError("pinned checkout has no tracked UTF-8 text files")
    paths = [path for path, _text in files]
    tree = "===== REPOSITORY TREE =====\n" + "\n".join(paths) + "\n"
    compare_root = Path(__file__).resolve().parent
    try:
        fixed_context = (
            (compare_root / "system.md").read_text(encoding="utf-8")
            + (compare_root / "prompt.md").read_text(encoding="utf-8")
            + (compare_root / "response-schema.json").read_text(encoding="utf-8")
            + query
        )
        fixed_context_tokens = count_tokens(fixed_context)
        tree_tokens = count_tokens(tree)
    except (OSError, UnicodeDecodeError, TokenAccountingError) as exc:
        raise CorpusPackError(f"exact token accounting failed: {exc}") from exc
    if tree_tokens > evidence_token_budget:
        raise CorpusPackError("repository tree alone exceeds evidence token budget")
    if fixed_context_tokens + tree_tokens > context_token_budget:
        raise CorpusPackError("repository tree alone exceeds context token ceiling")

    ranked = _rank(_blocks(files), query)
    included: list[dict] = []
    content = tree
    novel_tokens = tree_tokens
    covered_lines: dict[str, set[int]] = {path: set() for path in paths}
    for block in ranked:
        header = (
            f"===== {block['path']}:{block['start_line']}-"
            f"{block['end_line']} =====\n"
        )
        source_lines = block["body"].splitlines(keepends=True)
        marginal = header + "".join(
            line
            for line_number, line in zip(
                range(block["start_line"], block["end_line"] + 1),
                source_lines,
                strict=True,
            )
            if line_number not in covered_lines[block["path"]]
        )
        try:
            marginal_tokens = count_tokens(marginal)
            projected_content = content + block["rendered"]
            projected_context_tokens = (
                fixed_context_tokens + count_tokens(projected_content)
            )
        except TokenAccountingError as exc:
            raise CorpusPackError(f"exact token accounting failed: {exc}") from exc
        if (
            novel_tokens + marginal_tokens <= evidence_token_budget
            and projected_context_tokens <= context_token_budget
        ):
            included.append(block)
            content = projected_content
            novel_tokens += marginal_tokens
            covered_lines[block["path"]].update(
                range(block["start_line"], block["end_line"] + 1)
            )

    pack_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    eligible_lines = 0
    for path, text in files:
        eligible_lines += max(1, len(text.splitlines()))
    covered_line_count = sum(len(lines) for lines in covered_lines.values())
    try:
        included_content_tokens = count_tokens(content)
    except TokenAccountingError as exc:
        raise CorpusPackError(f"exact token accounting failed: {exc}") from exc
    metadata = {
        "schema_version": 1,
        "producer": "bench/compare/build_corpus_pack.py:v1",
        "repository_revision": expected_revision,
        "query_sha256": hashlib.sha256(query.encode("utf-8")).hexdigest(),
        "construction": (
            "whole_repository"
            if len(included) == len(ranked)
            else "query_conditioned_pack"
        ),
        "block_lines": BLOCK_LINES,
        "block_overlap": BLOCK_OVERLAP,
        "tokenizer": tokenizer_descriptor(),
        "evidence_token_budget": evidence_token_budget,
        "context_token_budget": context_token_budget,
        "fixed_context_tokens": fixed_context_tokens,
        "included_novel_evidence_tokens": novel_tokens,
        "included_evidence_tokens": included_content_tokens,
        "included_context_tokens": fixed_context_tokens + included_content_tokens,
        "included_evidence_bytes": len(content.encode("utf-8")),
        "eligible_files": paths,
        "eligible_bytes": sum(len(text.encode("utf-8")) for _path, text in files),
        "eligible_tokens": sum(count_tokens(text) for _path, text in files),
        "candidate_blocks": len(ranked),
        "included_blocks": len(included),
        "effective_k": len(included),
        "truncated": len(included) != len(ranked),
        "line_coverage": covered_line_count / eligible_lines,
        "ranked_blocks": [
            {
                "rank": rank,
                "path": block["path"],
                "start_line": block["start_line"],
                "end_line": block["end_line"],
                "query_token_matches": block["query_token_matches"],
                "included": block in included,
            }
            for rank, block in enumerate(ranked, 1)
        ],
    }
    metadata["metadata_sha256"] = hashlib.sha256(_canonical_json(metadata)).hexdigest()
    return {
        "content": content,
        "pack_sha256": pack_sha256,
        "metadata": metadata,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", type=Path, required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--evidence-token-budget", type=int, required=True)
    parser.add_argument("--context-token-budget", type=int, required=True)
    arguments = parser.parse_args(argv)
    try:
        pack = build_pack(
            repository=arguments.repository,
            expected_revision=arguments.expected_revision,
            query=arguments.query,
            evidence_token_budget=arguments.evidence_token_budget,
            context_token_budget=arguments.context_token_budget,
        )
    except CorpusPackError as exc:
        parser.exit(1, f"ERROR: {exc}\n")
    print(json.dumps(pack, sort_keys=True, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
