#!/usr/bin/env python3
"""Build the deterministic offline embedding model used by readiness smoke tests."""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys


VOCABULARY = (
    "api",
    "audit",
    "auth",
    "bearer",
    "claims",
    "config",
    "create",
    "db",
    "def",
    "dict",
    "execute",
    "fixture",
    "forward",
    "from",
    "function",
    "id",
    "import",
    "invalid",
    "list",
    "login",
    "middleware",
    "name",
    "order",
    "os",
    "print",
    "process",
    "processed",
    "query",
    "raise",
    "record",
    "records",
    "request",
    "return",
    "search",
    "select",
    "session",
    "signature",
    "signed",
    "sql",
    "src",
    "storage",
    "str",
    "subject",
    "token",
    "user_id",
    "validate",
    "ValueError",
    "where",
)
REQUIRED_FILES = (
    Path("modules.json"),
    Path("config_sentence_transformers.json"),
    Path("0_BoW/config.json"),
)


def build(output: Path) -> None:
    if os.path.lexists(output):
        raise RuntimeError(f"model output already exists: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"

    from sentence_transformers import SentenceTransformer
    from sentence_transformers.sentence_transformer.modules import BoW, Normalize

    model = SentenceTransformer(
        modules=[
            BoW(
                vocab=list(VOCABULARY),
                word_weights={},
                unknown_word_weight=1,
                cumulative_term_frequency=True,
            ),
            Normalize(),
        ]
    )
    model.save(str(output))
    missing = [
        str(relative)
        for relative in REQUIRED_FILES
        if not (output / relative).is_file()
    ]
    if missing:
        raise RuntimeError("model build omitted files: " + ", ".join(missing))

    loaded = SentenceTransformer(str(output), local_files_only=True)
    vectors = loaded.encode(
        ["def validate token", "select records where id"],
        convert_to_numpy=True,
    )
    if vectors.shape != (2, len(VOCABULARY)):
        raise RuntimeError(
            f"unexpected readiness model output shape: {vectors.shape!r}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Build a deterministic local readiness embedding model"
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        build(args.output.resolve())
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"Readiness model build FAILED: {exc}", file=sys.stderr)
        return 1
    print(f"Readiness model written to {args.output}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
