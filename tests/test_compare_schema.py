"""Tests for frozen controls, arm isolation, and deterministic execution order."""

from __future__ import annotations

import unittest
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal, localcontext
from pathlib import Path
from unittest.mock import ANY

ROOT = Path(__file__).resolve().parents[1]
COMPARE = ROOT / "bench" / "compare"


class CompareSchemaTests(unittest.TestCase):
    def test_microdollar_round_trip_is_exact_at_low_decimal_precision(self):
        from bench.compare.schema import usd_decimal_from_micros, usd_micros

        value = Decimal("1000000000000.000001")
        with localcontext() as context:
            context.prec = 3
            micros = usd_micros(
                value,
                "cost",
                positive=True,
            )
            restored = usd_decimal_from_micros(
                micros,
                "cost",
                positive=True,
            )
            canonical_zero = usd_decimal_from_micros(
                0,
                "cost",
                positive=False,
            )

        self.assertEqual(micros, 1_000_000_000_000_000_001)
        self.assertEqual(restored, value)
        self.assertEqual(format(restored, "f"), "1000000000000.000001")
        self.assertEqual(canonical_zero, Decimal("0.000000"))
        self.assertFalse(canonical_zero.is_signed())

    def test_live_usd_controls_require_exact_six_place_decimals(self):
        from bench.compare.schema import ContractError, FrozenControls

        arguments = {
            "provider": "anthropic",
            "model_id": "claude-opus-4-1",
            "cli_version": "2.1.220",
            "max_total_usd": Decimal("5.000000"),
            "max_unit_usd": Decimal("0.010000"),
            "calibration_sha256": "c" * 64,
        }
        with self.assertRaisesRegex(ContractError, "six-place"):
            FrozenControls.live(
                **{**arguments, "max_total_usd": Decimal("5.0")}
            )

        controls = FrozenControls.live(**arguments)
        with self.assertRaisesRegex(ContractError, "six-place"):
            replace(
                controls,
                max_unit_usd=Decimal("0.01"),
            ).descriptor(ROOT)

    def test_five_arms_expose_only_their_pre_registered_read_only_tools(self):
        from bench.compare.schema import ARM_CONTRACTS, FORBIDDEN_TOOLS

        self.assertEqual(
            tuple(ARM_CONTRACTS),
            ("corpus", "native", "code-search", "code-graph", "composed"),
        )
        self.assertEqual(ARM_CONTRACTS["corpus"].allowed_tools, ())
        self.assertEqual(
            ARM_CONTRACTS["native"].allowed_tools,
            ("Glob", "Grep", "Read"),
        )
        self.assertIn(
            "mcp__code-search__search_code",
            ARM_CONTRACTS["code-search"].allowed_tools,
        )
        self.assertNotIn(
            "mcp__code-graph__search_graph",
            ARM_CONTRACTS["code-search"].allowed_tools,
        )
        self.assertIn(
            "mcp__code-graph__search_graph",
            ARM_CONTRACTS["code-graph"].allowed_tools,
        )
        self.assertNotIn(
            "mcp__code-search__search_code",
            ARM_CONTRACTS["code-graph"].allowed_tools,
        )
        composed = set(ARM_CONTRACTS["composed"].allowed_tools)
        self.assertTrue(
            set(ARM_CONTRACTS["code-search"].allowed_tools) <= composed
        )
        self.assertTrue(
            set(ARM_CONTRACTS["code-graph"].allowed_tools) <= composed
        )
        for contract in ARM_CONTRACTS.values():
            self.assertFalse(set(contract.allowed_tools) & FORBIDDEN_TOOLS)
            self.assertTrue(contract.read_only)
            self.assertFalse(contract.network)

    def test_control_hash_is_identical_across_arms_and_binds_every_control(self):
        from bench.compare.schema import (
            ARM_CONTRACTS,
            FrozenControls,
            build_unit_contract,
        )

        controls = FrozenControls.fixture()
        contracts = [
            build_unit_contract(
                case_id="fixture-1",
                query="Where is identity verified?",
                repository_revision="a" * 40,
                arm=arm,
                replicate=1,
                controls=controls,
                root=ROOT,
            )
            for arm in ARM_CONTRACTS
        ]
        self.assertEqual(len({item["control_sha256"] for item in contracts}), 1)
        self.assertEqual(len({item["unit_key"] for item in contracts}), 5)
        descriptor = contracts[0]["controls"]
        self.assertEqual(descriptor["top_k"], 10)
        self.assertEqual(descriptor["max_discovery_tool_calls"], 20)
        self.assertEqual(
            descriptor["repository_evidence"],
            {
                "budget": 64_000,
                "unit": "novel_tokens",
                "tokenizer": {
                    "id": "code_intel_ascii_lexeme_unicode_scalar_v1",
                    "pattern_sha256": ANY,
                    "source_sha256": ANY,
                },
            },
        )
        self.assertEqual(descriptor["context_token_budget"], 128_000)
        self.assertEqual(descriptor["wall_timeout_seconds"], 600)
        self.assertEqual(descriptor["permission_mode"], "plan")
        self.assertTrue(descriptor["fresh_session"])
        self.assertFalse(descriptor["memory"])
        self.assertEqual(
            set(descriptor["hashes"]),
            {"prompt", "response_schema", "system"},
        )

        changed = FrozenControls.fixture(model_id="different-model")
        mutation = build_unit_contract(
            case_id="fixture-1",
            query="Where is identity verified?",
            repository_revision="a" * 40,
            arm="corpus",
            replicate=1,
            controls=changed,
            root=ROOT,
        )
        self.assertNotEqual(contracts[0]["control_sha256"], mutation["control_sha256"])

    def test_component_identity_binds_bom_tool_snapshots_and_routing_policy(self):
        from bench.compare.schema import (
            ContractError,
            component_identity,
            validate_component_identity,
        )

        identity = component_identity(ROOT)
        self.assertEqual(identity["code-search"]["version"], "v0.4.0")
        self.assertEqual(identity["code-graph"]["version"], "v0.9.2")
        self.assertEqual(len(identity["bom_sha256"]), 64)
        self.assertEqual(len(identity["routing_policy_sha256"]), 64)
        self.assertEqual(len(identity["code-search"]["tool_snapshot_sha256"]), 64)
        self.assertEqual(len(identity["code-graph"]["tool_snapshot_sha256"]), 64)
        validate_component_identity(identity, component_identity(ROOT))

        mutated = deepcopy(identity)
        mutated["code-graph"]["version"] = "v0.7.0-internal.2"
        with self.assertRaisesRegex(ContractError, "identity mismatch"):
            validate_component_identity(identity, mutated)

    def test_latin_square_balances_each_arm_at_each_position(self):
        from bench.compare.schema import ARM_CONTRACTS, latin_square_units

        case_ids = [f"case-{index}" for index in range(5)]
        units = latin_square_units(case_ids, replicates=1)
        rows = {
            case_id: [
                unit["arm"]
                for unit in units
                if unit["case_id"] == case_id
            ]
            for case_id in case_ids
        }
        self.assertTrue(all(len(row) == 5 for row in rows.values()))
        for position in range(5):
            self.assertEqual(
                {rows[case_id][position] for case_id in case_ids},
                set(ARM_CONTRACTS),
            )

    def test_response_contract_rejects_hidden_truncation_or_identity_drift(self):
        from bench.compare.schema import ContractError, validate_observation

        observation = {
            "status": "ok",
            "ranked_entities": [
                {"rank": 1, "file": "src/main.py", "symbol": "run"}
            ],
            "requested_k": 10,
            "candidate_count": 1,
            "effective_k": 1,
            "truncated": False,
            "tool_calls": 1,
            "component_identity_sha256": "a" * 64,
            "control_sha256": "b" * 64,
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_tokens": 0,
            "tool_result_tokens": 2,
            "evidence_tokens": 20,
            "evidence_bytes": 100,
            "context_tokens": 30,
            "egress_bytes": 0,
            "cost_usd": "0.000000",
            "latency_ms": 1,
        }
        validate_observation(
            observation,
            expected_control_sha256="b" * 64,
            expected_component_identity_sha256="a" * 64,
        )
        bad = deepcopy(observation)
        bad["candidate_count"] = 20
        with self.assertRaisesRegex(ContractError, "truncation"):
            validate_observation(
                bad,
                expected_control_sha256="b" * 64,
                expected_component_identity_sha256="a" * 64,
            )
        bad = deepcopy(observation)
        bad["control_sha256"] = "c" * 64
        with self.assertRaisesRegex(ContractError, "control"):
            validate_observation(
                bad,
                expected_control_sha256="b" * 64,
                expected_component_identity_sha256="a" * 64,
            )
        bad = deepcopy(observation)
        bad["cost_usd"] = 0
        with self.assertRaisesRegex(ContractError, "decimal string"):
            validate_observation(
                bad,
                expected_control_sha256="b" * 64,
                expected_component_identity_sha256="a" * 64,
            )
        bad = deepcopy(observation)
        bad["evidence_tokens"] = 64_001
        with self.assertRaisesRegex(ContractError, "evidence"):
            validate_observation(
                bad,
                expected_control_sha256="b" * 64,
                expected_component_identity_sha256="a" * 64,
            )

    def test_outcome_binding_rejects_cross_arm_or_unit_attribution(self):
        from bench.compare.schema import ContractError, validate_unit_binding

        expected = {
            "unit_key": "case-1|r1|native",
            "case_id": "case-1",
            "arm": "native",
            "replicate": 1,
            "position": 2,
            "arm_contract_sha256": "a" * 64,
        }
        record = {
            "stable_key": expected["unit_key"],
            **expected,
        }
        validate_unit_binding(record, **expected)

        for field, value in (
            ("stable_key", "case-1|r1|corpus"),
            ("unit_key", "case-1|r1|corpus"),
            ("case_id", "case-2"),
            ("arm", "corpus"),
            ("replicate", 2),
            ("position", 3),
            ("arm_contract_sha256", "0" * 64),
        ):
            mutated = deepcopy(record)
            mutated[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(
                ContractError,
                "binding",
            ):
                validate_unit_binding(mutated, **expected)


if __name__ == "__main__":
    unittest.main()
