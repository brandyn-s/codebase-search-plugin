"""Tests for exact, shared benchmark token accounting."""

from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class CompareTokenAccountingTests(unittest.TestCase):
    def test_tokenizer_is_deterministic_content_addressed_and_not_byte_counting(self):
        from bench.compare.token_accounting import (
            count_tokens,
            tokenizer_descriptor,
        )

        text = "alpha_beta(123) café\n"
        descriptor = tokenizer_descriptor()

        self.assertEqual(count_tokens(text), 8)
        self.assertLess(count_tokens(text), len(text.encode("utf-8")))
        self.assertEqual(
            descriptor["id"],
            "code_intel_ascii_lexeme_unicode_scalar_v1",
        )
        self.assertRegex(descriptor["pattern_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(descriptor["source_sha256"], r"^[0-9a-f]{64}$")

    def test_tokenizer_splits_long_runs_to_bound_context_drift(self):
        from bench.compare.token_accounting import count_tokens

        self.assertEqual(count_tokens("a" * 33), 3)
        self.assertEqual(count_tokens(" " * 17), 3)


if __name__ == "__main__":
    unittest.main()
