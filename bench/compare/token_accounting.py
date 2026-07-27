"""Exact, dependency-free token accounting shared by every benchmark arm."""

from __future__ import annotations

import hashlib
from pathlib import Path
import re


_IDENTIFIER = "code_intel_ascii_lexeme_unicode_scalar_v1"
_PATTERN_TEXT = (
    r"\r\n"
    r"|[ \t\f\v]{1,8}"
    r"|[A-Za-z_][A-Za-z0-9_]{0,15}"
    r"|[0-9]{1,8}"
    r"|[\u0080-\U0010ffff]"
    r"|[\x00-\x7f]"
)
_PATTERN = re.compile(_PATTERN_TEXT)


class TokenAccountingError(ValueError):
    """Input cannot be accounted for exactly under the frozen tokenizer."""


def tokenize(text: str) -> tuple[str, ...]:
    if not isinstance(text, str):
        raise TokenAccountingError("tokenizer input must be text")
    pieces = tuple(match.group(0) for match in _PATTERN.finditer(text))
    if "".join(pieces) != text:
        raise TokenAccountingError("tokenizer failed to cover input exactly")
    return pieces


def count_tokens(text: str) -> int:
    return len(tokenize(text))


def tokenizer_descriptor() -> dict:
    source = Path(__file__)
    try:
        source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    except OSError as exc:
        raise TokenAccountingError(f"cannot hash tokenizer source: {exc}") from exc
    return {
        "id": _IDENTIFIER,
        "pattern_sha256": hashlib.sha256(_PATTERN_TEXT.encode("ascii")).hexdigest(),
        "source_sha256": source_digest,
    }
