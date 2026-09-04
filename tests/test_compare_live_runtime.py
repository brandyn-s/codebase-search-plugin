"""Tests for the inert live-evaluation control plane."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import tempfile
import threading
import time
import unittest
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from pathlib import Path
from unittest.mock import patch

LIVE_RUN_SEED = "1" * 64
LIVE_RUN_ID = "2" * 64


def canonical(value: object) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


class DigestSignatureVerifier:
    """Deterministic test verifier; production chooses the trust issuer later."""

    def __init__(self, key_material: bytes = b"fixture-authority-key"):
        self.key_material = key_material
        self.calls: list[dict] = []

    def signature(self, payload: bytes) -> str:
        return hashlib.sha256(self.key_material + payload).hexdigest()

    def verify(
        self,
        *,
        algorithm: str,
        issuer: str,
        key_id: str,
        payload: bytes,
        signature: str,
    ) -> bool:
        self.calls.append(
            {
                "algorithm": algorithm,
                "issuer": issuer,
                "key_id": key_id,
                "payload": payload,
                "signature": signature,
            }
        )
        return (
            algorithm == "ed25519"
            and issuer == "fixture-security-authority"
            and key_id == "fixture-key-1"
            and signature == self.signature(payload)
        )


class MutableClock:
    def __init__(self, current: datetime):
        self.current = current

    def now(self) -> datetime:
        return self.current


def signed_authority(
    *,
    kind: str,
    claims: dict,
    verifier: DigestSignatureVerifier,
) -> bytes:
    signed = {
        "schema_version": 1,
        "authority_kind": kind,
        "claims": claims,
    }
    envelope = {
        **signed,
        "signature": {
            "algorithm": "ed25519",
            "issuer": "fixture-security-authority",
            "key_id": "fixture-key-1",
            "value": verifier.signature(canonical(signed)),
        },
    }
    return canonical(envelope) + b"\n"


def auth_claims(**changes: object) -> dict:
    claims = {
        "run_seed": LIVE_RUN_SEED,
        "provider": "anthropic",
        "model_id": "claude-opus-4-1",
        "cli_version": "2.1.220",
        "cli_sha256": "a" * 64,
        "execution_mode": "bare",
        "credential_source": "api_key_helper",
        "account_scope": "account-code-intel",
        "endpoint": "https://api.anthropic.com",
        "issued_at": "2026-07-27T11:55:00Z",
        "expires_at": "2026-07-27T12:05:00Z",
    }
    claims.update(changes)
    return claims


def auth_expectation():
    from bench.compare.live_runtime import AuthExpectation

    return AuthExpectation(
        run_seed=LIVE_RUN_SEED,
        provider="anthropic",
        model_id="claude-opus-4-1",
        cli_version="2.1.220",
        cli_sha256="a" * 64,
        account_scope="account-code-intel",
        endpoint="https://api.anthropic.com",
    )


def cost_claims(**changes: object) -> dict:
    claims = {
        "run_seed": LIVE_RUN_SEED,
        "provider": "anthropic",
        "model_id": "claude-opus-4-1",
        "account_scope": "account-code-intel",
        "endpoint": "https://api.anthropic.com",
        "currency": "USD",
        "max_total_usd": "5.000000",
        "max_unit_usd": "0.010000",
        "expected_units": 500,
        "calibration_sha256": "c" * 64,
        "mechanism": "transactional_budget_proxy",
        "issued_at": "2026-07-27T11:55:00Z",
        "expires_at": "2026-07-27T12:05:00Z",
    }
    claims.update(changes)
    return claims


def cost_expectation():
    from bench.compare.live_runtime import CostExpectation

    return CostExpectation(
        run_seed=LIVE_RUN_SEED,
        provider="anthropic",
        model_id="claude-opus-4-1",
        account_scope="account-code-intel",
        endpoint="https://api.anthropic.com",
        max_total_usd=Decimal("5.000000"),
        max_unit_usd=Decimal("0.010000"),
        expected_units=500,
        calibration_sha256="c" * 64,
    )


def live_controls():
    from bench.compare.schema import FrozenControls

    return FrozenControls.live(
        provider="anthropic",
        model_id="claude-opus-4-1",
        cli_version="2.1.220",
        max_total_usd=Decimal("5.000000"),
        max_unit_usd=Decimal("0.010000"),
        calibration_sha256="c" * 64,
    )


def mcp_server_specs():
    from bench.compare.live_runtime import McpServerSpec

    search_tools = (
        "mcp__code-search__code_localize",
        "mcp__code-search__find_similar_code",
        "mcp__code-search__get_file_context",
        "mcp__code-search__get_index_status",
        "mcp__code-search__list_projects",
        "mcp__code-search__search_code",
        "mcp__code-search__verify_index_integrity",
    )
    graph_tools = (
        "mcp__code-graph__detect_changes",
        "mcp__code-graph__get_architecture",
        "mcp__code-graph__get_code_snippet",
        "mcp__code-graph__query_graph",
        "mcp__code-graph__query_security_surfaces",
        "mcp__code-graph__query_stig_evidence",
        "mcp__code-graph__search_code",
        "mcp__code-graph__search_graph",
        "mcp__code-graph__trace_call_path",
        "mcp__code-graph__trace_data_flow",
    )
    return {
        "code-search": McpServerSpec(
            name="code-search",
            command="/opt/code-intel/code-search-mcp",
            args=("--stdio",),
            tools=search_tools,
        ),
        "code-graph": McpServerSpec(
            name="code-graph",
            command="/opt/code-intel/code-graph-mcp",
            args=("--stdio",),
            tools=graph_tools,
        ),
    }


def execution_fixture(
    directory: Path,
    *,
    arm: str = "code-search",
    unit_key: str | None = None,
    controls=None,
    max_total_usd: Decimal = Decimal("5.000000"),
    max_unit_usd: Decimal = Decimal("0.010000"),
    expected_units: int = 500,
):
    from types import SimpleNamespace

    from bench.compare.live_runtime import (
        AuthorityBoundary,
        CostExpectation,
        ExecutionContract,
        ExecutionContractTemplate,
        build_child_environments,
        compile_claude_invocation,
        load_auth_authority,
        load_cost_authority,
    )
    from bench.compare.schema import FrozenControls

    verifier = DigestSignatureVerifier()
    trusted_root = Path(directory).resolve()
    auth_path = trusted_root / "execution-auth.json"
    cost_path = trusted_root / "execution-cost.json"
    auth_path.write_bytes(
        signed_authority(
            kind="claude_bare_auth_v2",
            claims=auth_claims(),
            verifier=verifier,
        )
    )
    cost_path.write_bytes(
        signed_authority(
            kind="operation_cost_authority_v2",
            claims=cost_claims(
                max_total_usd=format(max_total_usd, "f"),
                max_unit_usd=format(max_unit_usd, "f"),
                expected_units=expected_units,
            ),
            verifier=verifier,
        )
    )
    auth = load_auth_authority(
        auth_path,
        trusted_root=trusted_root,
        expected=auth_expectation(),
        verifier=verifier,
        now=datetime(2026, 7, 27, 12, tzinfo=UTC),
    )
    cost = load_cost_authority(
        cost_path,
        trusted_root=trusted_root,
        expected=CostExpectation(
            run_seed=LIVE_RUN_SEED,
            provider="anthropic",
            model_id="claude-opus-4-1",
            account_scope="account-code-intel",
            endpoint="https://api.anthropic.com",
            max_total_usd=max_total_usd,
            max_unit_usd=max_unit_usd,
            expected_units=expected_units,
            calibration_sha256="c" * 64,
        ),
        verifier=verifier,
        now=datetime(2026, 7, 27, 12, tzinfo=UTC),
    )
    frozen = (
        controls
        if controls is not None
        else FrozenControls.live(
            provider="anthropic",
            model_id="claude-opus-4-1",
            cli_version="2.1.220",
            max_total_usd=max_total_usd,
            max_unit_usd=max_unit_usd,
            calibration_sha256="c" * 64,
        )
    )
    environments = build_child_environments(
        arm=arm,
        ambient={"PATH": "/usr/bin:/bin"},
        trusted_root=trusted_root,
        isolated_root=trusted_root / f"children-{arm}",
        auth_authority=auth,
        fetch_credentials={},
        model_credentials={},
    )
    repository_root = Path(__file__).resolve().parents[1]
    response_schema = json.loads(
        (repository_root / "bench/compare/response-schema.json").read_text(
            encoding="utf-8"
        )
    )
    invocation = compile_claude_invocation(
        arm=arm,
        prompt="Locate the authorization boundary.",
        response_schema=response_schema,
        controls=frozen,
        mcp_servers=mcp_server_specs(),
        auth_authority=auth,
        child_environments=environments,
        root=repository_root,
    )
    unit = unit_key or f"case-1|r1|{arm}"
    template = ExecutionContractTemplate.create(
        run_seed=LIVE_RUN_SEED,
        unit_key=unit,
        auth_authority=auth,
        cost_authority=cost,
        controls=frozen,
        child_environments=environments,
        invocation=invocation,
        root=repository_root,
    )
    contract = ExecutionContract.create(
        run_id=LIVE_RUN_ID,
        template=template,
    )
    clock = MutableClock(datetime(2026, 7, 27, 12, tzinfo=UTC))
    authority_boundary = AuthorityBoundary(
        auth_authority=auth,
        cost_authority=cost,
        clock=clock,
    )
    return SimpleNamespace(
        trusted_root=trusted_root,
        repository_root=repository_root,
        auth=auth,
        cost=cost,
        controls=frozen,
        environments=environments,
        invocation=invocation,
        template=template,
        contract=contract,
        clock=clock,
        authority_boundary=authority_boundary,
    )


class ZeroDollarBroker:
    """Test-only idempotent broker; it cannot be selected by production code."""

    def __init__(self, *, authorized_usd: Decimal = Decimal("0.000000")):
        self.authorized_usd = authorized_usd
        self.reserve_calls = []
        self.reconcile_calls = []
        self.reservations = {}
        self.reconciliations = {}

    def reserve(self, request):
        from bench.compare.live_runtime import BudgetReservation

        self.reserve_calls.append(request)
        return self.reservations.setdefault(
            request.idempotency_key,
            BudgetReservation(
                reservation_id=f"zero-{request.attempt_number}",
                idempotency_key=request.idempotency_key,
                authorized_usd=self.authorized_usd,
            ),
        )

    def reconcile(self, *, idempotency_key):
        from bench.compare.live_runtime import DispatchReconciliation

        self.reconcile_calls.append(idempotency_key)
        return self.reconciliations.get(
            idempotency_key,
            DispatchReconciliation(state="unknown", receipt=None),
        )


class ScriptedExecutor:
    """Test-only injected executor that returns prebuilt receipts."""

    def __init__(self, receipts, *, journal_path: Path | None = None):
        self.receipts = list(receipts)
        self.journal_path = journal_path
        self.calls = []

    def dispatch(
        self,
        *,
        invocation,
        launch_environment,
        enforcement,
        reservation,
        idempotency_key,
    ):
        durable_phase = None
        if self.journal_path is not None:
            durable = json.loads(
                self.journal_path.read_text(encoding="utf-8")
            )
            durable_phase = durable["attempts"][-1]["phase"]
        self.calls.append(
            {
                "descriptor_sha256": invocation.descriptor_sha256,
                "environment_names": tuple(
                    name for name, _ in launch_environment.model
                ),
                "has_fetch_scope": hasattr(launch_environment, "fetch"),
                "enforcement": enforcement.contract.descriptor(),
                "reservation_id": reservation.reservation_id,
                "authorized_usd": reservation.authorized_usd,
                "idempotency_key": idempotency_key,
                "durable_phase": durable_phase,
            }
        )
        result = self.receipts.pop(0)
        if isinstance(result, BaseException):
            raise result
        enforcement.complete()
        return result


class BlockingConcurrentBroker:
    def __init__(self):
        self.reserve_calls = []
        self.reconcile_calls = []
        self.first_reserve_entered = threading.Event()
        self.release_first_reserve = threading.Event()
        self.receipt = None
        self._lock = threading.Lock()

    def reserve(self, request):
        from bench.compare.live_runtime import BudgetReservation

        with self._lock:
            self.reserve_calls.append(request)
            first = len(self.reserve_calls) == 1
        if first:
            self.first_reserve_entered.set()
            self.release_first_reserve.wait(timeout=2)
        return BudgetReservation(
            reservation_id="zero-1",
            idempotency_key=request.idempotency_key,
            authorized_usd=Decimal("0.000000"),
        )

    def reconcile(self, *, idempotency_key):
        from bench.compare.live_runtime import DispatchReconciliation

        self.reconcile_calls.append(idempotency_key)
        return DispatchReconciliation(
            state="settled",
            receipt=self.receipt,
        )


class RecordingConcurrentExecutor:
    def __init__(self, broker):
        self.broker = broker
        self.calls = []

    def dispatch(
        self,
        *,
        invocation,
        launch_environment,
        enforcement,
        reservation,
        idempotency_key,
    ):
        from bench.compare.live_runtime import DispatchReceipt

        self.calls.append(idempotency_key)
        receipt = DispatchReceipt(
            operation_id="provider-operation-001",
            idempotency_key=idempotency_key,
            status="ok",
            cost_usd=Decimal("0.000000"),
            response_sha256="e" * 64,
            error_class=None,
        )
        self.broker.receipt = receipt
        enforcement.complete()
        return receipt


class LiveAuthorityTests(unittest.TestCase):
    def test_trusted_entrypoints_reject_noncanonical_path_spellings(self):
        from bench.compare.live_runtime import (
            LiveControlError,
            build_child_environments,
            load_auth_authority,
        )

        verifier = DigestSignatureVerifier()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve()
            alias_directory = directory / "alias"
            alias_directory.mkdir()
            authority_path = directory / "auth.json"
            authority_path.write_bytes(
                signed_authority(
                    kind="claude_bare_auth_v2",
                    claims=auth_claims(),
                    verifier=verifier,
                )
            )
            spellings = [
                f"{directory}/./auth.json",
                f"{directory}//auth.json",
                alias_directory / ".." / authority_path.name,
            ]
            case_alias = directory / authority_path.name.upper()
            if case_alias.exists():
                spellings.append(case_alias)
            for spelling in spellings:
                with self.subTest(
                    spelling=str(spelling)
                ), self.assertRaisesRegex(
                    LiveControlError,
                    "noncanonical",
                ):
                    load_auth_authority(
                        spelling,
                        trusted_root=directory,
                        expected=auth_expectation(),
                        verifier=verifier,
                        now=datetime(
                            2026,
                            7,
                            27,
                            12,
                            tzinfo=UTC,
                        ),
                    )

            with self.assertRaisesRegex(LiveControlError, "noncanonical"):
                load_auth_authority(
                    authority_path,
                    trusted_root=f"{directory}/.",
                    expected=auth_expectation(),
                    verifier=verifier,
                    now=datetime(2026, 7, 27, 12, tzinfo=UTC),
                )

            authority = load_auth_authority(
                authority_path,
                trusted_root=directory,
                expected=auth_expectation(),
                verifier=verifier,
                now=datetime(2026, 7, 27, 12, tzinfo=UTC),
            )
            with self.assertRaisesRegex(LiveControlError, "noncanonical"):
                build_child_environments(
                    arm="code-search",
                    ambient={"PATH": "/usr/bin:/bin"},
                    trusted_root=directory,
                    isolated_root=(
                        alias_directory / ".." / "children-code-search"
                    ),
                    auth_authority=authority,
                    fetch_credentials={},
                    model_credentials={},
                )

    def test_authority_v2_binds_run_seed_and_rejects_run_id_only_claims(
        self,
    ):
        from bench.compare.live_runtime import (
            AuthExpectation,
            LiveControlError,
            load_auth_authority,
            load_cost_authority,
        )

        verifier = DigestSignatureVerifier()
        seeded_claims = auth_claims()
        expected = AuthExpectation(
            run_seed=LIVE_RUN_SEED,
            provider="anthropic",
            model_id="claude-opus-4-1",
            cli_version="2.1.220",
            cli_sha256="a" * 64,
            account_scope="account-code-intel",
            endpoint="https://api.anthropic.com",
        )
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve()
            seeded_path = directory / "seeded.json"
            seeded_path.write_bytes(
                signed_authority(
                    kind="claude_bare_auth_v2",
                    claims=seeded_claims,
                    verifier=verifier,
                )
            )

            authority = load_auth_authority(
                seeded_path,
                trusted_root=directory,
                expected=expected,
                verifier=verifier,
                now=datetime(2026, 7, 27, 12, tzinfo=UTC),
            )

            old_path = directory / "old-run-id-only.json"
            old_claims = auth_claims()
            old_claims["run_id"] = "run-five-arm-001"
            old_claims.pop("run_seed")
            old_path.write_bytes(
                signed_authority(
                    kind="claude_bare_auth_v1",
                    claims=old_claims,
                    verifier=verifier,
                )
            )
            with self.assertRaises(LiveControlError):
                load_auth_authority(
                    old_path,
                    trusted_root=directory,
                    expected=expected,
                    verifier=verifier,
                    now=datetime(2026, 7, 27, 12, tzinfo=UTC),
                )

            old_cost_claims = cost_claims()
            old_cost_claims["run_id"] = "run-five-arm-001"
            old_cost_claims.pop("run_seed")
            old_cost_path = directory / "old-cost-run-id-only.json"
            old_cost_path.write_bytes(
                signed_authority(
                    kind="operation_cost_authority_v1",
                    claims=old_cost_claims,
                    verifier=verifier,
                )
            )
            with self.assertRaises(LiveControlError):
                load_cost_authority(
                    old_cost_path,
                    trusted_root=directory,
                    expected=cost_expectation(),
                    verifier=verifier,
                    now=datetime(2026, 7, 27, 12, tzinfo=UTC),
                )

        self.assertEqual(authority.run_seed, LIVE_RUN_SEED)

    def test_auth_authority_parses_from_one_signed_snapshot(self):
        from bench.compare.live_runtime import (
            load_auth_authority,
        )

        verifier = DigestSignatureVerifier()
        claims = auth_claims()
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / "auth-authority.json"
            encoded = signed_authority(
                kind="claude_bare_auth_v2",
                claims=claims,
                verifier=verifier,
            )
            path.write_bytes(encoded)

            authority = load_auth_authority(
                path,
                trusted_root=path.parent,
                expected=auth_expectation(),
                verifier=verifier,
                now=datetime(2026, 7, 27, 12, tzinfo=UTC),
            )

        self.assertEqual(authority.credential_source, "api_key_helper")
        self.assertEqual(authority.execution_mode, "bare")
        self.assertEqual(authority.snapshot_sha256, hashlib.sha256(encoded).hexdigest())
        self.assertEqual(len(verifier.calls), 1)
        self.assertEqual(
            verifier.calls[0]["payload"],
            canonical(
                {
                    "schema_version": 1,
                    "authority_kind": "claude_bare_auth_v2",
                    "claims": claims,
                }
            ),
        )

    def test_auth_authority_rejects_non_authoritative_or_bare_incompatible_input(self):
        from bench.compare.live_runtime import LiveControlError, load_auth_authority

        verifier = DigestSignatureVerifier()
        valid = json.loads(
            signed_authority(
                kind="claude_bare_auth_v2",
                claims=auth_claims(),
                verifier=verifier,
            )
        )
        invalid_records = {}

        extra_envelope = json.loads(canonical(valid))
        extra_envelope["authenticated"] = True
        invalid_records["unsigned boolean"] = canonical(extra_envelope) + b"\n"

        extra_claim = json.loads(canonical(valid))
        extra_claim["claims"]["authenticated"] = True
        invalid_records["extra claim"] = signed_authority(
            kind="claude_bare_auth_v2",
            claims=extra_claim["claims"],
            verifier=verifier,
        )

        missing_claim = json.loads(canonical(valid))
        missing_claim["claims"].pop("endpoint")
        invalid_records["missing claim"] = signed_authority(
            kind="claude_bare_auth_v2",
            claims=missing_claim["claims"],
            verifier=verifier,
        )

        for label, changes in (
            ("provider drift", {"provider": "bedrock"}),
            ("model drift", {"model_id": "claude-other"}),
            ("CLI version drift", {"cli_version": "2.1.219"}),
            ("CLI hash drift", {"cli_sha256": "b" * 64}),
            ("interactive mode", {"execution_mode": "interactive"}),
            ("OAuth source", {"credential_source": "claude.ai_oauth"}),
            ("keychain source", {"credential_source": "keychain"}),
            ("expired", {"expires_at": "2026-07-27T12:00:00Z"}),
            ("future issued", {"issued_at": "2026-07-27T12:00:01Z"}),
        ):
            invalid_records[label] = signed_authority(
                kind="claude_bare_auth_v2",
                claims=auth_claims(**changes),
                verifier=verifier,
            )

        wrong_kind = json.loads(canonical(valid))
        wrong_kind["authority_kind"] = "self_attested_auth_v1"
        invalid_records["wrong kind"] = signed_authority(
            kind=wrong_kind["authority_kind"],
            claims=wrong_kind["claims"],
            verifier=verifier,
        )

        bad_signature = json.loads(canonical(valid))
        bad_signature["signature"]["value"] = "0" * 64
        invalid_records["bad signature"] = canonical(bad_signature) + b"\n"
        invalid_records["noncanonical whitespace"] = (
            json.dumps(valid, indent=2, sort_keys=True).encode("utf-8") + b"\n"
        )
        invalid_records["duplicate field"] = (
            canonical(valid)[:-1]
            + b',"schema_version":1}\n'
        )

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve()
            for label, encoded in invalid_records.items():
                with self.subTest(label=label):
                    path = directory / f"{label.replace(' ', '-')}.json"
                    path.write_bytes(encoded)
                    with self.assertRaises(LiveControlError):
                        load_auth_authority(
                            path,
                            trusted_root=directory,
                            expected=auth_expectation(),
                            verifier=verifier,
                            now=datetime(2026, 7, 27, 12, tzinfo=UTC),
                        )

            target = directory / "valid.json"
            target.write_bytes(
                signed_authority(
                    kind="claude_bare_auth_v2",
                    claims=auth_claims(),
                    verifier=verifier,
                )
            )
            symlink = directory / "authority-link.json"
            symlink.symlink_to(target)
            with self.assertRaisesRegex(LiveControlError, "symlink"):
                load_auth_authority(
                    symlink,
                    trusted_root=directory,
                    expected=auth_expectation(),
                    verifier=verifier,
                    now=datetime(2026, 7, 27, 12, tzinfo=UTC),
                )

    def test_cost_authority_preserves_exact_operation_caps(self):
        from bench.compare.live_runtime import load_cost_authority

        verifier = DigestSignatureVerifier()
        encoded = signed_authority(
            kind="operation_cost_authority_v2",
            claims=cost_claims(),
            verifier=verifier,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / "cost-authority.json"
            path.write_bytes(encoded)

            authority = load_cost_authority(
                path,
                trusted_root=path.parent,
                expected=cost_expectation(),
                verifier=verifier,
                now=datetime(2026, 7, 27, 12, tzinfo=UTC),
            )

        self.assertEqual(authority.currency, "USD")
        self.assertEqual(authority.max_total_usd, Decimal("5.000000"))
        self.assertEqual(authority.max_unit_usd, Decimal("0.010000"))
        self.assertEqual(authority.expected_units, 500)
        self.assertEqual(authority.mechanism, "transactional_budget_proxy")
        self.assertEqual(authority.snapshot_sha256, hashlib.sha256(encoded).hexdigest())

    def test_cost_authority_rejects_one_micro_capacity_deficit_at_low_precision(
        self,
    ):
        from dataclasses import replace

        from bench.compare.live_runtime import LiveControlError, load_cost_authority

        verifier = DigestSignatureVerifier()
        max_unit_usd = "1000000000000.000001"
        max_total_usd = "2000000000000.000001"
        expected_units = 2
        encoded = signed_authority(
            kind="operation_cost_authority_v2",
            claims=cost_claims(
                max_total_usd=max_total_usd,
                max_unit_usd=max_unit_usd,
                expected_units=expected_units,
            ),
            verifier=verifier,
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp).resolve() / "under-cap-cost-authority.json"
            path.write_bytes(encoded)
            with localcontext() as context:
                context.prec = 6
                with self.assertRaisesRegex(
                    LiveControlError,
                    "total cannot cover all units",
                ):
                    load_cost_authority(
                        path,
                        trusted_root=path.parent,
                        expected=replace(
                            cost_expectation(),
                            max_total_usd=Decimal(max_total_usd),
                            max_unit_usd=Decimal(max_unit_usd),
                            expected_units=expected_units,
                        ),
                        verifier=verifier,
                        now=datetime(2026, 7, 27, 12, tzinfo=UTC),
                    )

    def test_cost_authority_and_auth_source_fail_closed_on_semantic_drift(self):
        from bench.compare.live_runtime import (
            LiveControlError,
            load_auth_authority,
            load_cost_authority,
        )

        verifier = DigestSignatureVerifier()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve()
            mixed_source = directory / "mixed-auth-source.json"
            mixed_source.write_bytes(
                signed_authority(
                    kind="claude_bare_auth_v2",
                    claims=auth_claims(credential_source="aws_bedrock"),
                    verifier=verifier,
                )
            )
            with self.assertRaisesRegex(LiveControlError, "source"):
                load_auth_authority(
                    mixed_source,
                    trusted_root=directory,
                    expected=auth_expectation(),
                    verifier=verifier,
                    now=datetime(2026, 7, 27, 12, tzinfo=UTC),
                )

            invalid_claims = {
                "unsigned enforced flag": cost_claims(enforced=True),
                "client-side flag": cost_claims(
                    mechanism="claude_max_budget_usd"
                ),
                "wrong currency": cost_claims(currency="EUR"),
                "noncanonical total": cost_claims(max_total_usd="5"),
                "zero unit cap": cost_claims(max_unit_usd="0.000000"),
                "insufficient total": cost_claims(max_total_usd="4.999999"),
                "boolean unit count": cost_claims(expected_units=True),
                "calibration drift": cost_claims(calibration_sha256="d" * 64),
                "account drift": cost_claims(account_scope="other-account"),
                "endpoint drift": cost_claims(endpoint="https://proxy.invalid"),
                "expired": cost_claims(expires_at="2026-07-27T12:00:00Z"),
            }
            for label, claims in invalid_claims.items():
                with self.subTest(label=label):
                    path = directory / f"{label.replace(' ', '-')}.json"
                    path.write_bytes(
                        signed_authority(
                            kind="operation_cost_authority_v2",
                            claims=claims,
                            verifier=verifier,
                        )
                    )
                    with self.assertRaises(LiveControlError):
                        load_cost_authority(
                            path,
                            trusted_root=directory,
                            expected=cost_expectation(),
                            verifier=verifier,
                            now=datetime(2026, 7, 27, 12, tzinfo=UTC),
                        )

    def test_loaded_authorities_are_factory_sealed_over_every_claim(self):
        from dataclasses import fields, replace

        from bench.compare.live_runtime import (
            AuthAuthority,
            CostAuthority,
            LiveControlError,
        )

        with tempfile.TemporaryDirectory() as tmp:
            fixture = execution_fixture(Path(tmp).resolve())

        for label, authority in (
            (
                "auth CLI version",
                replace(fixture.auth, cli_version="2.1.221"),
            ),
            (
                "auth CLI hash",
                replace(fixture.auth, cli_sha256="b" * 64),
            ),
            (
                "auth credential source",
                replace(
                    fixture.auth,
                    credential_source="anthropic_api_key",
                ),
            ),
            (
                "auth execution mode",
                replace(fixture.auth, execution_mode="interactive"),
            ),
            (
                "cost total",
                replace(
                    fixture.cost,
                    max_total_usd=Decimal("6.000000"),
                ),
            ),
            (
                "cost unit",
                replace(
                    fixture.cost,
                    max_unit_usd=Decimal("0.009000"),
                ),
            ),
            (
                "cost calibration",
                replace(fixture.cost, calibration_sha256="d" * 64),
            ),
            (
                "cost currency",
                replace(fixture.cost, currency="EUR"),
            ),
        ):
            with self.subTest(label=label), self.assertRaisesRegex(
                LiveControlError,
                "factory-sealed",
            ):
                authority.validate()

        for authority_type, authority in (
            (AuthAuthority, fixture.auth),
            (CostAuthority, fixture.cost),
        ):
            kwargs = {
                item.name: getattr(authority, item.name)
                for item in fields(authority)
                if item.name != "_seal"
            }
            with self.assertRaises(TypeError):
                authority_type(**kwargs)


class LiveControlDescriptorTests(unittest.TestCase):
    def test_live_controls_freeze_authoritative_cost_and_retry_policy(self):
        from bench.compare.schema import FrozenControls

        controls = FrozenControls.live(
            provider="anthropic",
            model_id="claude-opus-4-1",
            cli_version="2.1.220",
            max_total_usd=Decimal("5.000000"),
            max_unit_usd=Decimal("0.010000"),
            calibration_sha256="c" * 64,
        )

        descriptor = controls.descriptor(Path(__file__).resolve().parents[1])

        self.assertEqual(
            descriptor["cost"],
            {
                "policy": "authoritative_operation_bound",
                "max_total_usd": "5.000000",
                "max_unit_usd": "0.010000",
                "calibration_sha256": "c" * 64,
            },
        )
        self.assertEqual(
            descriptor["retry"],
            {
                "policy": "reconcile_before_transient_retry_v1",
                "max_attempts": 2,
                "retryable_error_classes": [
                    "provider_overloaded",
                    "rate_limited",
                    "transport_interrupted",
                ],
            },
        )
        self.assertEqual(descriptor["temperature"], "0")

    def test_all_five_arms_compile_literal_inert_claude_descriptors(self):
        search_tools = (
            "mcp__code-search__code_localize",
            "mcp__code-search__find_similar_code",
            "mcp__code-search__get_file_context",
            "mcp__code-search__get_index_status",
            "mcp__code-search__list_projects",
            "mcp__code-search__search_code",
            "mcp__code-search__verify_index_integrity",
        )
        graph_tools = (
            "mcp__code-graph__detect_changes",
            "mcp__code-graph__get_architecture",
            "mcp__code-graph__get_code_snippet",
            "mcp__code-graph__query_graph",
            "mcp__code-graph__query_security_surfaces",
            "mcp__code-graph__query_stig_evidence",
            "mcp__code-graph__search_code",
            "mcp__code-graph__search_graph",
            "mcp__code-graph__trace_call_path",
            "mcp__code-graph__trace_data_flow",
        )
        servers = mcp_server_specs()
        expected_tools = {
            "corpus": (),
            "native": ("Glob", "Grep", "Read"),
            "code-search": ("Read", *search_tools),
            "code-graph": ("Read", *graph_tools),
            "composed": ("Read", *search_tools, *graph_tools),
        }
        expected_servers = {
            "corpus": (),
            "native": (),
            "code-search": ("code-search",),
            "code-graph": ("code-graph",),
            "composed": ("code-search", "code-graph"),
        }
        root = Path(__file__).resolve().parents[1]
        response_schema = json.loads(
            (root / "bench/compare/response-schema.json").read_text(
                encoding="utf-8"
            )
        )
        system_text = (root / "bench/compare/system.md").read_text(
            encoding="utf-8"
        )
        descriptors = {}
        expected_denied_tools = (
            "Bash",
            "Edit",
            "NotebookEdit",
            "Web",
            "WebFetch",
            "WebSearch",
            "Write",
            "mcp__code-graph__delete_project",
            "mcp__code-graph__generate_report",
            "mcp__code-graph__index_repository",
            "mcp__code-graph__ingest_traces",
            "mcp__code-graph__manage_adr",
            "mcp__code-search__cancel_indexing",
            "mcp__code-search__clear_index",
            "mcp__code-search__delete_project",
            "mcp__code-search__index_directory",
            "mcp__code-search__index_test_project",
            "mcp__code-search__switch_project",
        )
        environment_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(environment_tmp.cleanup)
        environment_root = Path(environment_tmp.name).resolve()
        for arm, arm_tools in expected_tools.items():
            arm_root = environment_root / arm
            arm_root.mkdir()
            fixture = execution_fixture(
                arm_root,
                arm=arm,
            )
            descriptor = fixture.invocation
            descriptors[arm] = descriptor
            mcp_environments = dict(fixture.environments.mcp)
            selected = {
                name: {
                    "args": ["--stdio"],
                    "command": servers[name].command,
                    "env": dict(mcp_environments[name]),
                    "type": "stdio",
                }
                for name in expected_servers[arm]
            }
            mcp_config = canonical({"mcpServers": selected}).decode("utf-8")
            builtins = tuple(
                tool for tool in arm_tools if not tool.startswith("mcp__")
            )
            expected_argv = (
                "/opt/anthropic/bin/claude",
                "--bare",
                "--print",
                "Locate the authorization boundary.",
                "--output-format",
                "json",
                "--json-schema",
                canonical(response_schema).decode("utf-8"),
                "--model",
                "claude-opus-4-1",
                "--system-prompt",
                system_text,
                "--permission-mode",
                "plan",
                "--no-session-persistence",
                "--strict-mcp-config",
                "--mcp-config",
                mcp_config,
                "--tools",
                ",".join(builtins),
                "--allowedTools",
                ",".join(arm_tools),
                "--disallowedTools",
                ",".join(expected_denied_tools),
                "--max-turns",
                "20",
                "--max-budget-usd",
                "0.010000",
            )
            self.assertEqual(descriptor.argv, expected_argv, arm)
            self.assertEqual(descriptor.allowed_tools, arm_tools, arm)
            self.assertEqual(descriptor.mcp_server_names, expected_servers[arm], arm)
            self.assertEqual(descriptor.mcp_config_json, mcp_config, arm)
            self.assertNotIn("--fallback-model", descriptor.argv)
            self.assertEqual(descriptor.max_discovery_tool_calls, 20)
            self.assertEqual(descriptor.evidence_token_budget, 64_000)
            self.assertEqual(descriptor.context_token_budget, 128_000)
            self.assertEqual(descriptor.max_budget_usd_role, "defense_in_depth_only")

        self.assertEqual(
            len({value.common_controls_sha256 for value in descriptors.values()}),
            1,
        )
        self.assertEqual(
            len({value.descriptor_sha256 for value in descriptors.values()}),
            5,
        )

    def test_compiler_rejects_schema_server_tool_and_credential_drift(self):
        from bench.compare.live_runtime import (
            LiveControlError,
            McpServerSpec,
            compile_claude_invocation,
        )
        from bench.compare.schema import FrozenControls

        root = Path(__file__).resolve().parents[1]
        response_schema = json.loads(
            (root / "bench/compare/response-schema.json").read_text(
                encoding="utf-8"
            )
        )
        environment_tmp = tempfile.TemporaryDirectory()
        self.addCleanup(environment_tmp.cleanup)
        fixture = execution_fixture(Path(environment_tmp.name).resolve())
        servers = mcp_server_specs()
        common = {
            "arm": "code-search",
            "prompt": "Locate the authorization boundary.",
            "response_schema": response_schema,
            "controls": live_controls(),
            "mcp_servers": servers,
            "auth_authority": fixture.auth,
            "child_environments": fixture.environments,
            "root": root,
        }

        changed_schema = json.loads(canonical(response_schema))
        changed_schema["title"] = "unbound schema"
        with self.assertRaisesRegex(LiveControlError, "schema"):
            compile_claude_invocation(**{**common, "response_schema": changed_schema})

        extra_servers = {
            **servers,
            "unreviewed": McpServerSpec(
                name="unreviewed",
                command="/opt/unreviewed",
                args=("--stdio",),
                tools=(),
            ),
        }
        with self.assertRaisesRegex(LiveControlError, "server set"):
            compile_claude_invocation(**{**common, "mcp_servers": extra_servers})

        search = servers["code-search"]
        extra_tool = McpServerSpec(
            name=search.name,
            command=search.command,
            args=search.args,
            tools=(*search.tools, "mcp__code-search__delete_project"),
        )
        with self.assertRaisesRegex(LiveControlError, "code-search"):
            compile_claude_invocation(
                **{
                    **common,
                    "mcp_servers": {**servers, "code-search": extra_tool},
                }
            )

        relative_command = McpServerSpec(
            name=search.name,
            command="code-search-mcp",
            args=search.args,
            tools=search.tools,
        )
        with self.assertRaisesRegex(LiveControlError, "code-search"):
            compile_claude_invocation(
                **{
                    **common,
                    "mcp_servers": {
                        **servers,
                        "code-search": relative_command,
                    },
                }
            )

        changed_command = McpServerSpec(
            name=search.name,
            command="/opt/code-intel/alternate-search-mcp",
            args=search.args,
            tools=search.tools,
        )
        with self.assertRaisesRegex(LiveControlError, "reviewed"):
            compile_claude_invocation(
                **{
                    **common,
                    "mcp_servers": {
                        **servers,
                        "code-search": changed_command,
                    },
                }
            )

        changed_args = McpServerSpec(
            name=search.name,
            command=search.command,
            args=("--stdio", "--verbose"),
            tools=search.tools,
        )
        with self.assertRaisesRegex(LiveControlError, "reviewed"):
            compile_claude_invocation(
                **{
                    **common,
                    "mcp_servers": {
                        **servers,
                        "code-search": changed_args,
                    },
                }
            )

        credential_argument = McpServerSpec(
            name=search.name,
            command=search.command,
            args=("--stdio", "--api-key=fixture-secret"),
            tools=search.tools,
        )
        with self.assertRaisesRegex(LiveControlError, "credential"):
            compile_claude_invocation(
                **{
                    **common,
                    "mcp_servers": {
                        **servers,
                        "code-search": credential_argument,
                    },
                }
            )

        with self.assertRaisesRegex(LiveControlError, "live controls"):
            compile_claude_invocation(
                **{**common, "controls": FrozenControls.fixture()}
            )
        with self.assertRaisesRegex(LiveControlError, "unknown"):
            compile_claude_invocation(**{**common, "arm": "fallback"})


class ChildEnvironmentTests(unittest.TestCase):
    def test_fetch_model_and_mcp_children_receive_separate_minimal_environments(self):
        from bench.compare.live_runtime import (
            build_child_environments,
            load_auth_authority,
        )

        verifier = DigestSignatureVerifier()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve()
            authority_path = directory / "auth.json"
            authority_path.write_bytes(
                signed_authority(
                    kind="claude_bare_auth_v2",
                    claims=auth_claims(
                        credential_source="anthropic_api_key"
                    ),
                    verifier=verifier,
                )
            )
            authority = load_auth_authority(
                authority_path,
                trusted_root=directory,
                expected=auth_expectation(),
                verifier=verifier,
                now=datetime(2026, 7, 27, 12, tzinfo=UTC),
            )
            isolated_root = directory / "children"
            environments = build_child_environments(
                arm="composed",
                ambient={
                    "PATH": "/usr/bin:/bin",
                    "LANG": "C.UTF-8",
                    "HOME": "/ambient/home",
                    "ANTHROPIC_API_KEY": "ambient-model-secret",
                    "VOYAGE_API_KEY": "ambient-embedding-secret",
                    "GH_TOKEN": "ambient-github-secret",
                    "AWS_SECRET_ACCESS_KEY": "ambient-cloud-secret",
                    "SSH_AUTH_SOCK": "/ambient/agent.sock",
                },
                trusted_root=directory,
                isolated_root=isolated_root,
                auth_authority=authority,
                fetch_credentials={"GH_TOKEN": "explicit-fetch-secret"},
                model_credentials={
                    "ANTHROPIC_API_KEY": "explicit-model-secret"
                },
            )

        fetch = dict(environments.fetch)
        model = dict(environments.model)
        mcp = {
            name: dict(values) for name, values in environments.mcp
        }
        self.assertEqual(
            tuple(mcp),
            ("code-search", "code-graph"),
        )
        self.assertEqual(fetch["GH_TOKEN"], "explicit-fetch-secret")
        self.assertNotIn("ANTHROPIC_API_KEY", fetch)
        self.assertEqual(model["ANTHROPIC_API_KEY"], "explicit-model-secret")
        self.assertNotIn("GH_TOKEN", model)
        self.assertNotIn("VOYAGE_API_KEY", model)
        self.assertEqual(fetch["PATH"], "/usr/bin:/bin")
        self.assertEqual(model["LANG"], "C.UTF-8")
        self.assertNotEqual(fetch["HOME"], model["HOME"])
        self.assertNotEqual(fetch["TMPDIR"], model["TMPDIR"])
        self.assertTrue(Path(fetch["HOME"]).is_relative_to(isolated_root))
        self.assertTrue(Path(model["HOME"]).is_relative_to(isolated_root))
        denied_names = {
            "ANTHROPIC_API_KEY",
            "VOYAGE_API_KEY",
            "GH_TOKEN",
            "AWS_SECRET_ACCESS_KEY",
            "SSH_AUTH_SOCK",
        }
        for name, child in mcp.items():
            self.assertFalse(denied_names & set(child), name)
            self.assertTrue(Path(child["HOME"]).is_relative_to(isolated_root))
            self.assertNotEqual(child["HOME"], fetch["HOME"])
            self.assertNotEqual(child["HOME"], model["HOME"])
        rendered = repr(environments)
        for secret in (
            "explicit-fetch-secret",
            "explicit-model-secret",
            "ambient-model-secret",
        ):
            self.assertNotIn(secret, rendered)

    def test_child_environment_builder_rejects_secret_and_path_escape_inputs(self):
        from bench.compare.live_runtime import (
            LiveControlError,
            build_child_environments,
            load_auth_authority,
        )

        verifier = DigestSignatureVerifier()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve()
            authority_path = directory / "auth.json"
            authority_path.write_bytes(
                signed_authority(
                    kind="claude_bare_auth_v2",
                    claims=auth_claims(
                        credential_source="anthropic_api_key"
                    ),
                    verifier=verifier,
                )
            )
            authority = load_auth_authority(
                authority_path,
                trusted_root=directory,
                expected=auth_expectation(),
                verifier=verifier,
                now=datetime(2026, 7, 27, 12, tzinfo=UTC),
            )
            common = {
                "arm": "code-search",
                "ambient": {"PATH": "/usr/bin:/bin"},
                "trusted_root": directory,
                "isolated_root": directory / "children",
                "auth_authority": authority,
                "fetch_credentials": {},
                "model_credentials": {
                    "ANTHROPIC_API_KEY": "explicit-model-secret"
                },
            }

            with self.assertRaisesRegex(LiveControlError, "fetch"):
                build_child_environments(
                    **{
                        **common,
                        "fetch_credentials": {
                            "VOYAGE_API_KEY": "embedding-secret"
                        },
                    }
                )
            with self.assertRaisesRegex(LiveControlError, "model"):
                build_child_environments(
                    **{**common, "model_credentials": {}}
                )
            with self.assertRaisesRegex(LiveControlError, "ambient"):
                build_child_environments(
                    **{**common, "ambient": {"PATH": "bad\x00path"}}
                )

            target = directory / "real-parent"
            target.mkdir()
            linked_parent = directory / "linked-parent"
            linked_parent.symlink_to(target, target_is_directory=True)
            with self.assertRaisesRegex(LiveControlError, "symlink"):
                build_child_environments(
                    **{
                        **common,
                        "isolated_root": linked_parent / "children",
                    }
                )

            file_root = directory / "not-a-directory"
            file_root.write_text("occupied", encoding="utf-8")
            with self.assertRaisesRegex(LiveControlError, "directory"):
                build_child_environments(
                    **{**common, "isolated_root": file_root}
                )


class AttemptLifecycleTests(unittest.TestCase):
    def test_successful_attempt_persists_every_transition_before_classification(self):
        from bench.compare.live_runtime import (
            AttemptJournal,
            DispatchReceipt,
            run_attempt_lifecycle,
        )

        with tempfile.TemporaryDirectory() as tmp:
            fixture = execution_fixture(Path(tmp).resolve())
            path = fixture.trusted_root / "attempts.json"
            journal = AttemptJournal(
                path,
                trusted_root=fixture.trusted_root,
                execution_contract=fixture.contract,
            )
            broker = ZeroDollarBroker()
            receipt = DispatchReceipt(
                operation_id="provider-operation-001",
                idempotency_key=hashlib.sha256(
                    (
                        f"{LIVE_RUN_ID}|case-1|r1|code-search|attempt-1"
                    ).encode()
                ).hexdigest(),
                status="ok",
                cost_usd=Decimal("0.000000"),
                response_sha256="e" * 64,
                error_class=None,
            )
            executor = ScriptedExecutor([receipt], journal_path=path)

            outcome = run_attempt_lifecycle(
                journal=journal,
                execution_contract=fixture.contract,
                authority_boundary=fixture.authority_boundary,
                invocation=fixture.invocation,
                child_environments=fixture.environments,
                controls=fixture.controls,
                root=fixture.repository_root,
                broker=broker,
                executor=executor,
            )
            durable = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(outcome.phase, "classified")
        self.assertEqual(outcome.classification, "success")
        self.assertFalse(outcome.retryable)
        self.assertEqual(
            durable["attempts"][0]["transitions"],
            ["reserved", "dispatching", "receipt", "classified"],
        )
        self.assertEqual(executor.calls[0]["durable_phase"], "dispatching")
        self.assertEqual(len(broker.reserve_calls), 1)
        self.assertEqual(len(executor.calls), 1)
        self.assertEqual(broker.reconcile_calls, [])

    def test_signed_zero_is_canonical_before_post_boundary_journal_writes(self):
        from bench.compare.live_runtime import (
            AttemptJournal,
            DispatchReceipt,
            LiveControlError,
            run_attempt_lifecycle,
        )

        for source in ("reservation", "receipt"):
            with self.subTest(source=source):  # noqa: SIM117
                with tempfile.TemporaryDirectory() as tmp:
                    fixture = execution_fixture(Path(tmp).resolve())
                    path = fixture.trusted_root / f"{source}.json"
                    idempotency_key = hashlib.sha256(
                        (
                            f"{LIVE_RUN_ID}|case-1|r1|code-search|attempt-1"
                        ).encode()
                    ).hexdigest()
                    broker = ZeroDollarBroker(
                        authorized_usd=Decimal(
                            "-0.000000"
                            if source == "reservation"
                            else "0.000000"
                        )
                    )
                    executor = ScriptedExecutor(
                        [
                            DispatchReceipt(
                                operation_id="provider-operation-001",
                                idempotency_key=idempotency_key,
                                status="ok",
                                cost_usd=Decimal(
                                    "-0.000000"
                                    if source == "receipt"
                                    else "0.000000"
                                ),
                                response_sha256="e" * 64,
                                error_class=None,
                            )
                        ],
                        journal_path=path,
                    )

                    try:
                        outcome = run_attempt_lifecycle(
                            journal=AttemptJournal(
                                path,
                                trusted_root=fixture.trusted_root,
                                execution_contract=fixture.contract,
                            ),
                            execution_contract=fixture.contract,
                            authority_boundary=fixture.authority_boundary,
                            invocation=fixture.invocation,
                            child_environments=fixture.environments,
                            controls=fixture.controls,
                            root=fixture.repository_root,
                            broker=broker,
                            executor=executor,
                        )
                    except LiveControlError as exc:
                        self.fail(
                            "signed zero passed a trusted boundary but failed "
                            f"during durable persistence: {exc}"
                        )

                    durable = json.loads(path.read_text(encoding="utf-8"))
                    attempt = durable["attempts"][0]
                    self.assertEqual(outcome.classification, "success")
                    self.assertEqual(
                        attempt["reservation"]["authorized_usd"],
                        "0.000000",
                    )
                    self.assertEqual(
                        attempt["receipt"]["cost_usd"],
                        "0.000000",
                    )
                    self.assertFalse(
                        executor.calls[0]["authorized_usd"].is_signed()
                    )
                    self.assertEqual(len(broker.reserve_calls), 1)
                    self.assertEqual(len(executor.calls), 1)

    def test_dispatch_rejects_isolated_root_replaced_by_symlink_after_sealing(
        self,
    ):
        from bench.compare.live_runtime import (
            AttemptJournal,
            DispatchReceipt,
            LiveControlError,
            run_attempt_lifecycle,
        )

        with tempfile.TemporaryDirectory() as tmp:
            fixture = execution_fixture(Path(tmp).resolve())
            isolated_root = Path(fixture.environments.isolated_root)
            self.assertTrue(isolated_root.is_dir())
            isolated_root.rename(
                fixture.trusted_root / "sealed-children-original"
            )
            attacker_root = fixture.trusted_root / "attacker-children"
            attacker_root.mkdir()
            isolated_root.symlink_to(
                attacker_root,
                target_is_directory=True,
            )
            journal_path = fixture.trusted_root / "symlink-swap.json"
            executor = ScriptedExecutor(
                [
                    DispatchReceipt(
                        operation_id="provider-operation-001",
                        idempotency_key=hashlib.sha256(
                            (
                                f"{LIVE_RUN_ID}|case-1|r1|code-search"
                                "|attempt-1"
                            ).encode()
                        ).hexdigest(),
                        status="ok",
                        cost_usd=Decimal("0.000000"),
                        response_sha256="e" * 64,
                        error_class=None,
                    )
                ],
                journal_path=journal_path,
            )

            with self.assertRaisesRegex(
                LiveControlError,
                "child isolation",
            ):
                run_attempt_lifecycle(
                    journal=AttemptJournal(
                        journal_path,
                        trusted_root=fixture.trusted_root,
                        execution_contract=fixture.contract,
                    ),
                    execution_contract=fixture.contract,
                    authority_boundary=fixture.authority_boundary,
                    invocation=fixture.invocation,
                    child_environments=fixture.environments,
                    controls=fixture.controls,
                    root=fixture.repository_root,
                    broker=ZeroDollarBroker(),
                    executor=executor,
                )

        self.assertEqual(executor.calls, [])

    def test_adapter_completion_failure_cannot_be_settled_into_terminal_resume(
        self,
    ):
        from bench.compare.live_runtime import (
            AttemptJournal,
            DispatchReceipt,
            DispatchReconciliation,
            LiveControlError,
            UnresolvedDispatchError,
            run_attempt_lifecycle,
        )

        class IncompleteAdapterExecutor:
            def __init__(self):
                self.calls = []
                self.receipt = None

            def dispatch(
                self,
                *,
                invocation,
                launch_environment,
                enforcement,
                reservation,
                idempotency_key,
            ):
                self.calls.append(idempotency_key)
                self.receipt = DispatchReceipt(
                    operation_id="provider-operation-without-adapter-proof",
                    idempotency_key=idempotency_key,
                    status="ok",
                    cost_usd=Decimal("0.000000"),
                    response_sha256="e" * 64,
                    error_class=None,
                )
                return self.receipt

        with tempfile.TemporaryDirectory() as tmp:
            fixture = execution_fixture(Path(tmp).resolve())
            path = fixture.trusted_root / "adapter-incomplete.json"
            broker = ZeroDollarBroker()
            executor = IncompleteAdapterExecutor()
            with self.assertRaisesRegex(
                LiveControlError,
                "adapter enforcement did not complete",
            ):
                run_attempt_lifecycle(
                    journal=AttemptJournal(
                        path,
                        trusted_root=fixture.trusted_root,
                        execution_contract=fixture.contract,
                    ),
                    execution_contract=fixture.contract,
                    authority_boundary=fixture.authority_boundary,
                    invocation=fixture.invocation,
                    child_environments=fixture.environments,
                    controls=fixture.controls,
                    root=fixture.repository_root,
                    broker=broker,
                    executor=executor,
                )
            self.assertIsNotNone(executor.receipt)
            idempotency_key = executor.calls[0]
            broker.reconciliations[idempotency_key] = DispatchReconciliation(
                state="settled",
                receipt=executor.receipt,
            )

            with self.assertRaisesRegex(
                UnresolvedDispatchError,
                "adapter-completion evidence",
            ):
                run_attempt_lifecycle(
                    journal=AttemptJournal(
                        path,
                        trusted_root=fixture.trusted_root,
                        execution_contract=fixture.contract,
                    ),
                    execution_contract=fixture.contract,
                    authority_boundary=fixture.authority_boundary,
                    invocation=fixture.invocation,
                    child_environments=fixture.environments,
                    controls=fixture.controls,
                    root=fixture.repository_root,
                    broker=broker,
                    executor=ScriptedExecutor([]),
                )

            durable = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(len(durable["attempts"]), 1)
            self.assertEqual(durable["attempts"][0]["phase"], "dispatching")
            self.assertFalse((fixture.trusted_root / ".done").exists())

    def test_resume_reconciles_unknown_dispatch_before_any_second_call(self):
        from bench.compare.live_runtime import (
            AttemptJournal,
            DispatchReceipt,
            DispatchReconciliation,
            UnresolvedDispatchError,
            run_attempt_lifecycle,
        )

        for reconciliation_state in ("settled", "not_dispatched", "unknown"):
            with self.subTest(state=reconciliation_state):  # noqa: SIM117
                with tempfile.TemporaryDirectory() as tmp:
                    fixture = execution_fixture(Path(tmp).resolve())
                    path = fixture.trusted_root / "attempts.json"
                    broker = ZeroDollarBroker()
                    first_executor = ScriptedExecutor(
                        [RuntimeError("connection lost after dispatch")],
                        journal_path=path,
                    )
                    with self.assertRaisesRegex(RuntimeError, "connection lost"):
                        run_attempt_lifecycle(
                            journal=AttemptJournal(
                                path,
                                trusted_root=fixture.trusted_root,
                                execution_contract=fixture.contract,
                            ),
                            execution_contract=fixture.contract,
                            authority_boundary=fixture.authority_boundary,
                            invocation=fixture.invocation,
                            child_environments=fixture.environments,
                            controls=fixture.controls,
                            root=fixture.repository_root,
                            broker=broker,
                            executor=first_executor,
                        )
                    interrupted = json.loads(
                        path.read_text(encoding="utf-8")
                    )
                    attempt = interrupted["attempts"][0]
                    idempotency_key = attempt["idempotency_key"]
                    self.assertEqual(attempt["phase"], "dispatching")
                    receipt = DispatchReceipt(
                        operation_id="provider-operation-001",
                        idempotency_key=idempotency_key,
                        status="ok",
                        cost_usd=Decimal("0.000000"),
                        response_sha256="e" * 64,
                        error_class=None,
                    )
                    broker.reconciliations[idempotency_key] = (
                        DispatchReconciliation(
                            state=reconciliation_state,
                            receipt=(
                                receipt
                                if reconciliation_state == "settled"
                                else None
                            ),
                        )
                    )
                    resumed_executor = ScriptedExecutor(
                        [receipt]
                        if reconciliation_state == "not_dispatched"
                        else [],
                        journal_path=path,
                    )
                    resumed = AttemptJournal(
                        path,
                        trusted_root=fixture.trusted_root,
                        execution_contract=fixture.contract,
                    )

                    if reconciliation_state in {"unknown", "settled"}:
                        with self.assertRaises(UnresolvedDispatchError):
                            run_attempt_lifecycle(
                                journal=resumed,
                                execution_contract=fixture.contract,
                                authority_boundary=fixture.authority_boundary,
                                invocation=fixture.invocation,
                                child_environments=fixture.environments,
                                controls=fixture.controls,
                                root=fixture.repository_root,
                                broker=broker,
                                executor=resumed_executor,
                            )
                        self.assertEqual(
                            json.loads(path.read_text())["attempts"][0]["phase"],
                            "dispatching",
                        )
                    else:
                        outcome = run_attempt_lifecycle(
                            journal=resumed,
                            execution_contract=fixture.contract,
                            authority_boundary=fixture.authority_boundary,
                            invocation=fixture.invocation,
                            child_environments=fixture.environments,
                            controls=fixture.controls,
                            root=fixture.repository_root,
                            broker=broker,
                            executor=resumed_executor,
                        )
                        self.assertEqual(outcome.classification, "success")

                    self.assertEqual(
                        broker.reconcile_calls,
                        [idempotency_key],
                    )
                    self.assertEqual(len(broker.reserve_calls), 1)
                    self.assertEqual(
                        len(resumed_executor.calls),
                        1 if reconciliation_state == "not_dispatched" else 0,
                    )

    def test_retry_policy_allows_only_settled_transients_with_remaining_cap(self):
        from bench.compare.live_runtime import (
            AttemptJournal,
            BudgetCapError,
            DispatchReceipt,
            run_attempt_lifecycle,
        )

        def receipt(
            idempotency_key: str,
            *,
            status: str,
            cost_usd: Decimal = Decimal("0.000000"),
            error_class: str | None = None,
            sequence: int = 1,
        ):
            return DispatchReceipt(
                operation_id=f"provider-operation-{sequence}",
                idempotency_key=idempotency_key,
                status=status,
                cost_usd=cost_usd,
                response_sha256="e" * 64 if status == "ok" else None,
                error_class=error_class,
            )

        run_id = LIVE_RUN_ID
        unit_key = "case-1|r1|code-search"
        first_key = hashlib.sha256(
            f"{run_id}|{unit_key}|attempt-1".encode()
        ).hexdigest()
        second_key = hashlib.sha256(
            f"{run_id}|{unit_key}|attempt-2".encode()
        ).hexdigest()

        with tempfile.TemporaryDirectory() as tmp:
            fixture = execution_fixture(Path(tmp).resolve())
            path = fixture.trusted_root / "transient.json"
            broker = ZeroDollarBroker()
            executor = ScriptedExecutor(
                [
                    receipt(
                        first_key,
                        status="error",
                        error_class="provider_overloaded",
                    ),
                    receipt(second_key, status="ok", sequence=2),
                ],
                journal_path=path,
            )
            outcome = run_attempt_lifecycle(
                journal=AttemptJournal(
                    path,
                    trusted_root=fixture.trusted_root,
                    execution_contract=fixture.contract,
                ),
                execution_contract=fixture.contract,
                authority_boundary=fixture.authority_boundary,
                invocation=fixture.invocation,
                child_environments=fixture.environments,
                controls=fixture.controls,
                root=fixture.repository_root,
                broker=broker,
                executor=executor,
            )
            durable = json.loads(path.read_text())

            self.assertEqual(outcome.classification, "success")
            self.assertEqual(outcome.attempt_number, 2)
            self.assertEqual(len(broker.reserve_calls), 2)
            self.assertEqual(len(executor.calls), 2)
            self.assertEqual(
                durable["attempts"][0]["classification"],
                {"retryable": True, "value": "transient_error"},
            )
            self.assertEqual(
                [item["idempotency_key"] for item in durable["attempts"]],
                [first_key, second_key],
            )

        for error_class in (
            "authentication",
            "cost_cap",
            "index_identity_mismatch",
            "schema_mismatch",
        ):
            with self.subTest(error_class=error_class):  # noqa: SIM117
                with tempfile.TemporaryDirectory() as tmp:
                    fixture = execution_fixture(Path(tmp).resolve())
                    path = fixture.trusted_root / "fatal.json"
                    broker = ZeroDollarBroker()
                    executor = ScriptedExecutor(
                        [
                            receipt(
                                first_key,
                                status="error",
                                error_class=error_class,
                            )
                        ],
                        journal_path=path,
                    )
                    outcome = run_attempt_lifecycle(
                        journal=AttemptJournal(
                            path,
                            trusted_root=fixture.trusted_root,
                            execution_contract=fixture.contract,
                        ),
                        execution_contract=fixture.contract,
                        authority_boundary=fixture.authority_boundary,
                        invocation=fixture.invocation,
                        child_environments=fixture.environments,
                        controls=fixture.controls,
                        root=fixture.repository_root,
                        broker=broker,
                        executor=executor,
                    )
                    self.assertEqual(outcome.classification, "fatal_error")
                    self.assertFalse(outcome.retryable)
                    self.assertEqual(len(broker.reserve_calls), 1)
                    self.assertEqual(len(executor.calls), 1)

        with tempfile.TemporaryDirectory() as tmp:
            fixture = execution_fixture(Path(tmp).resolve())
            path = fixture.trusted_root / "cap.json"
            broker = ZeroDollarBroker(
                authorized_usd=Decimal("0.010000")
            )
            executor = ScriptedExecutor(
                [
                    receipt(
                        first_key,
                        status="error",
                        cost_usd=Decimal("0.010000"),
                        error_class="provider_overloaded",
                    )
                ],
                journal_path=path,
            )
            with self.assertRaises(BudgetCapError):
                run_attempt_lifecycle(
                    journal=AttemptJournal(
                        path,
                        trusted_root=fixture.trusted_root,
                        execution_contract=fixture.contract,
                    ),
                    execution_contract=fixture.contract,
                    authority_boundary=fixture.authority_boundary,
                    invocation=fixture.invocation,
                    child_environments=fixture.environments,
                    controls=fixture.controls,
                    root=fixture.repository_root,
                    broker=broker,
                    executor=executor,
                )
            self.assertEqual(len(broker.reserve_calls), 1)
            self.assertEqual(len(executor.calls), 1)
            self.assertEqual(
                json.loads(path.read_text())["attempts"][0]["phase"],
                "classified",
            )

    def test_retry_outcome_reports_exact_cumulative_cost(self):
        from bench.compare.live_runtime import (
            AttemptJournal,
            BudgetReservation,
            DispatchReceipt,
            run_attempt_lifecycle,
        )

        class RemainingCapBroker(ZeroDollarBroker):
            def reserve(self, request):
                self.reserve_calls.append(request)
                return BudgetReservation(
                    reservation_id=f"remaining-{request.attempt_number}",
                    idempotency_key=request.idempotency_key,
                    authorized_usd=request.max_unit_usd,
                )

        run_id = LIVE_RUN_ID
        unit_key = "case-1|r1|code-search"
        keys = [
            hashlib.sha256(
                f"{run_id}|{unit_key}|attempt-{number}".encode()
            ).hexdigest()
            for number in (1, 2)
        ]
        for final_status, final_error, expected_classification in (
            ("ok", None, "success"),
            ("error", "timeout", "measured_error"),
        ):
            with self.subTest(final_status=final_status):  # noqa: SIM117
                with tempfile.TemporaryDirectory() as tmp:
                    fixture = execution_fixture(Path(tmp).resolve())
                    path = (
                        fixture.trusted_root
                        / f"cumulative-{final_status}.json"
                    )
                    broker = RemainingCapBroker()
                    executor = ScriptedExecutor(
                        [
                            DispatchReceipt(
                                operation_id="provider-operation-1",
                                idempotency_key=keys[0],
                                status="error",
                                cost_usd=Decimal("0.004000"),
                                response_sha256=None,
                                error_class="provider_overloaded",
                            ),
                            DispatchReceipt(
                                operation_id="provider-operation-2",
                                idempotency_key=keys[1],
                                status=final_status,
                                cost_usd=Decimal("0.003000"),
                                response_sha256=(
                                    "e" * 64
                                    if final_status == "ok"
                                    else None
                                ),
                                error_class=final_error,
                            ),
                        ],
                        journal_path=path,
                    )

                    outcome = run_attempt_lifecycle(
                        journal=AttemptJournal(
                            path,
                            trusted_root=fixture.trusted_root,
                            execution_contract=fixture.contract,
                        ),
                        execution_contract=fixture.contract,
                        authority_boundary=fixture.authority_boundary,
                        invocation=fixture.invocation,
                        child_environments=fixture.environments,
                        controls=fixture.controls,
                        root=fixture.repository_root,
                        broker=broker,
                        executor=executor,
                    )

                    self.assertEqual(
                        outcome.classification,
                        expected_classification,
                    )
                    self.assertEqual(
                        outcome.cost_usd,
                        Decimal("0.007000"),
                    )
                    self.assertEqual(
                        [request.max_unit_usd for request in broker.reserve_calls],
                        [Decimal("0.010000"), Decimal("0.006000")],
                    )
                    self.assertEqual(
                        [request.max_total_usd for request in broker.reserve_calls],
                        [Decimal("5.000000"), Decimal("4.996000")],
                    )

    def test_retry_arithmetic_is_exact_at_low_decimal_precision(self):
        from bench.compare.live_runtime import (
            AttemptJournal,
            BudgetReservation,
            DispatchReceipt,
            run_attempt_lifecycle,
        )

        class RemainingCapBroker(ZeroDollarBroker):
            def reserve(self, request):
                self.reserve_calls.append(request)
                return BudgetReservation(
                    reservation_id=f"remaining-{request.attempt_number}",
                    idempotency_key=request.idempotency_key,
                    authorized_usd=request.max_unit_usd,
                )

        run_id = LIVE_RUN_ID
        unit_key = "case-1|r1|code-search"
        keys = [
            hashlib.sha256(
                f"{run_id}|{unit_key}|attempt-{number}".encode()
            ).hexdigest()
            for number in (1, 2)
        ]
        max_cap = Decimal("1234567890123.456789")
        first_cost = Decimal("1000000000000.000001")
        second_cost = Decimal("0.000001")
        with tempfile.TemporaryDirectory() as tmp:
            fixture = execution_fixture(
                Path(tmp).resolve(),
                max_total_usd=max_cap,
                max_unit_usd=max_cap,
                expected_units=1,
            )
            path = fixture.trusted_root / "large-cumulative.json"
            broker = RemainingCapBroker()
            executor = ScriptedExecutor(
                [
                    DispatchReceipt(
                        operation_id="provider-operation-1",
                        idempotency_key=keys[0],
                        status="error",
                        cost_usd=first_cost,
                        response_sha256=None,
                        error_class="provider_overloaded",
                    ),
                    DispatchReceipt(
                        operation_id="provider-operation-2",
                        idempotency_key=keys[1],
                        status="ok",
                        cost_usd=second_cost,
                        response_sha256="e" * 64,
                        error_class=None,
                    ),
                ],
                journal_path=path,
            )

            with localcontext() as context:
                context.prec = 6
                outcome = run_attempt_lifecycle(
                    journal=AttemptJournal(
                        path,
                        trusted_root=fixture.trusted_root,
                        execution_contract=fixture.contract,
                    ),
                    execution_contract=fixture.contract,
                    authority_boundary=fixture.authority_boundary,
                    invocation=fixture.invocation,
                    child_environments=fixture.environments,
                    controls=fixture.controls,
                    root=fixture.repository_root,
                    broker=broker,
                    executor=executor,
                )

        self.assertEqual(outcome.cost_usd, Decimal("1000000000000.000002"))
        self.assertEqual(
            [request.max_unit_usd for request in broker.reserve_calls],
            [
                max_cap,
                Decimal("234567890123.456788"),
            ],
        )
        self.assertEqual(
            [request.max_total_usd for request in broker.reserve_calls],
            [
                max_cap,
                Decimal("234567890123.456788"),
            ],
        )

    def test_resume_from_each_phase_preserves_unit_and_descriptor_identity(self):
        from bench.compare.live_runtime import (
            AttemptJournal,
            BudgetRequest,
            BudgetReservation,
            DispatchReceipt,
            DispatchReconciliation,
            LiveControlError,
            run_attempt_lifecycle,
        )

        run_id = LIVE_RUN_ID
        unit_key = "case-1|r1|code-search"
        idempotency_key = hashlib.sha256(
            f"{run_id}|{unit_key}|attempt-1".encode()
        ).hexdigest()
        request = BudgetRequest(
            run_id=run_id,
            unit_key=unit_key,
            attempt_number=1,
            idempotency_key=idempotency_key,
            max_unit_usd=Decimal("0.010000"),
            max_total_usd=Decimal("5.000000"),
        )
        reservation = BudgetReservation(
            reservation_id="zero-1",
            idempotency_key=idempotency_key,
            authorized_usd=Decimal("0.000000"),
        )
        receipt = DispatchReceipt(
            operation_id="provider-operation-001",
            idempotency_key=idempotency_key,
            status="ok",
            cost_usd=Decimal("0.000000"),
            response_sha256="e" * 64,
            error_class=None,
        )

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve()
            fixture = execution_fixture(directory)

            reserved_path = directory / "reserved.json"
            reserved = AttemptJournal(
                reserved_path,
                trusted_root=fixture.trusted_root,
                execution_contract=fixture.contract,
            )
            reserved.record_reservation(request, reservation)
            reserved_executor = ScriptedExecutor(
                [receipt],
                journal_path=reserved_path,
            )
            reserved_outcome = run_attempt_lifecycle(
                journal=AttemptJournal(
                    reserved_path,
                    trusted_root=fixture.trusted_root,
                    execution_contract=fixture.contract,
                ),
                execution_contract=fixture.contract,
                authority_boundary=fixture.authority_boundary,
                invocation=fixture.invocation,
                child_environments=fixture.environments,
                controls=fixture.controls,
                root=fixture.repository_root,
                broker=ZeroDollarBroker(),
                executor=reserved_executor,
            )
            self.assertEqual(reserved_outcome.classification, "success")

            mismatched_path = directory / "mismatched-reserved.json"
            mismatched = AttemptJournal(
                mismatched_path,
                trusted_root=fixture.trusted_root,
                execution_contract=fixture.contract,
            )
            mismatched.record_reservation(request, reservation)
            graph_fixture = execution_fixture(
                directory,
                arm="code-graph",
                unit_key="case-1|r1|code-graph",
            )
            with self.assertRaisesRegex(LiveControlError, "contract|arm"):
                run_attempt_lifecycle(
                    journal=AttemptJournal(
                        mismatched_path,
                        trusted_root=fixture.trusted_root,
                        execution_contract=fixture.contract,
                    ),
                    execution_contract=fixture.contract,
                    authority_boundary=graph_fixture.authority_boundary,
                    invocation=graph_fixture.invocation,
                    child_environments=graph_fixture.environments,
                    controls=fixture.controls,
                    root=fixture.repository_root,
                    broker=ZeroDollarBroker(),
                    executor=ScriptedExecutor(
                        [receipt],
                        journal_path=mismatched_path,
                    ),
                )

            receipt_path = directory / "receipt.json"
            received = AttemptJournal(
                receipt_path,
                trusted_root=fixture.trusted_root,
                execution_contract=fixture.contract,
            )
            received.record_reservation(request, reservation)
            received.record_dispatching(
                descriptor_sha256=fixture.invocation.descriptor_sha256
            )
            received.record_receipt(receipt)
            receipt_executor = ScriptedExecutor([])
            receipt_broker = ZeroDollarBroker()
            receipt_broker.reconciliations[idempotency_key] = (
                DispatchReconciliation(state="settled", receipt=receipt)
            )
            receipt_outcome = run_attempt_lifecycle(
                journal=AttemptJournal(
                    receipt_path,
                    trusted_root=fixture.trusted_root,
                    execution_contract=fixture.contract,
                ),
                execution_contract=fixture.contract,
                authority_boundary=fixture.authority_boundary,
                invocation=fixture.invocation,
                child_environments=fixture.environments,
                controls=fixture.controls,
                root=fixture.repository_root,
                broker=receipt_broker,
                executor=receipt_executor,
            )
            self.assertEqual(receipt_outcome.classification, "success")
            self.assertEqual(
                receipt_broker.reconcile_calls,
                [idempotency_key],
            )
            self.assertEqual(receipt_executor.calls, [])

            classified_path = directory / "classified.json"
            first_broker = ZeroDollarBroker()
            first_executor = ScriptedExecutor(
                [receipt],
                journal_path=classified_path,
            )
            run_attempt_lifecycle(
                journal=AttemptJournal(
                    classified_path,
                    trusted_root=fixture.trusted_root,
                    execution_contract=fixture.contract,
                ),
                execution_contract=fixture.contract,
                authority_boundary=fixture.authority_boundary,
                invocation=fixture.invocation,
                child_environments=fixture.environments,
                controls=fixture.controls,
                root=fixture.repository_root,
                broker=first_broker,
                executor=first_executor,
            )
            resumed_broker = ZeroDollarBroker()
            resumed_broker.reconciliations[idempotency_key] = (
                DispatchReconciliation(state="settled", receipt=receipt)
            )
            resumed_executor = ScriptedExecutor([])
            classified_outcome = run_attempt_lifecycle(
                journal=AttemptJournal(
                    classified_path,
                    trusted_root=fixture.trusted_root,
                    execution_contract=fixture.contract,
                ),
                execution_contract=fixture.contract,
                authority_boundary=fixture.authority_boundary,
                invocation=fixture.invocation,
                child_environments=fixture.environments,
                controls=fixture.controls,
                root=fixture.repository_root,
                broker=resumed_broker,
                executor=resumed_executor,
            )
            self.assertEqual(classified_outcome.classification, "success")
            self.assertEqual(resumed_broker.reserve_calls, [])
            self.assertEqual(
                resumed_broker.reconcile_calls,
                [idempotency_key],
            )
            self.assertEqual(resumed_executor.calls, [])

    def test_attempt_journal_rejects_semantic_tampering_and_symlink_parents(self):
        from bench.compare.live_runtime import (
            AttemptJournal,
            DispatchReceipt,
            LiveControlError,
            _attempt_snapshot,
            _event_digest,
            run_attempt_lifecycle,
        )

        run_id = LIVE_RUN_ID
        unit_key = "case-1|r1|code-search"
        first_key = hashlib.sha256(
            f"{run_id}|{unit_key}|attempt-1".encode()
        ).hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp).resolve()
            fixture = execution_fixture(directory)
            path = directory / "attempts.json"
            run_attempt_lifecycle(
                journal=AttemptJournal(
                    path,
                    trusted_root=fixture.trusted_root,
                    execution_contract=fixture.contract,
                ),
                execution_contract=fixture.contract,
                authority_boundary=fixture.authority_boundary,
                invocation=fixture.invocation,
                child_environments=fixture.environments,
                controls=fixture.controls,
                root=fixture.repository_root,
                broker=ZeroDollarBroker(),
                executor=ScriptedExecutor(
                    [
                        DispatchReceipt(
                            operation_id="provider-operation-001",
                            idempotency_key=first_key,
                            status="ok",
                            cost_usd=Decimal("0.000000"),
                            response_sha256="e" * 64,
                            error_class=None,
                        )
                    ],
                    journal_path=path,
                ),
            )
            original = json.loads(path.read_text())
            mutations = {}

            def rebuild_events(state):
                state["events"] = []
                previous = state["journal_identity_sha256"]
                for attempt in state["attempts"]:
                    for phase in attempt["transitions"]:
                        snapshot = _attempt_snapshot(attempt, phase)
                        event = {
                            "sequence": len(state["events"]) + 1,
                            "previous_sha256": previous,
                            "attempt_number": attempt["attempt_number"],
                            "phase": phase,
                            "attempt": snapshot,
                        }
                        event["event_sha256"] = _event_digest(
                            state["journal_identity_sha256"],
                            event,
                        )
                        state["events"].append(event)
                        previous = event["event_sha256"]

            bad_transitions = json.loads(canonical(original))
            bad_transitions["attempts"][0]["transitions"].pop(1)
            mutations["transition"] = bad_transitions

            bad_number = json.loads(canonical(original))
            bad_number["attempts"][0]["attempt_number"] = 2
            mutations["sequence"] = bad_number

            bad_key = json.loads(canonical(original))
            bad_key["attempts"][0]["idempotency_key"] = "f" * 64
            mutations["idempotency"] = bad_key

            bad_phase_shape = json.loads(canonical(original))
            bad_phase_shape["attempts"][0]["phase"] = "dispatching"
            mutations["phase shape"] = bad_phase_shape

            extra_field = json.loads(canonical(original))
            extra_field["attempts"][0]["executor"] = "unreviewed"
            mutations["extra field"] = extra_field

            post_terminal = json.loads(canonical(original))
            second_attempt = json.loads(
                canonical(post_terminal["attempts"][0])
            )
            second_attempt["attempt_number"] = 2
            second_attempt["idempotency_key"] = hashlib.sha256(
                f"{run_id}|{unit_key}|attempt-2".encode()
            ).hexdigest()
            second_attempt["reservation"]["reservation_id"] = "zero-2"
            second_attempt["receipt"]["operation_id"] = (
                "provider-operation-002"
            )
            post_terminal["attempts"].append(second_attempt)
            rebuild_events(post_terminal)
            mutations["post-terminal success"] = post_terminal

            over_cap = json.loads(canonical(original))
            over_cap_attempt = over_cap["attempts"][0]
            over_cap_attempt["request"] = {
                "max_unit_usd": "9.000000",
                "max_total_usd": "9.000000",
            }
            over_cap_attempt["reservation"]["authorized_usd"] = "9.000000"
            over_cap_attempt["receipt"]["cost_usd"] = "9.000000"
            rebuild_events(over_cap)
            mutations["recomputed over cap"] = over_cap

            for label, mutated in mutations.items():
                with self.subTest(label=label):
                    changed = directory / f"{label.replace(' ', '-')}.json"
                    changed.write_bytes(canonical(mutated) + b"\n")
                    with self.assertRaisesRegex(LiveControlError, "journal"):
                        AttemptJournal(
                            changed,
                            trusted_root=fixture.trusted_root,
                            execution_contract=fixture.contract,
                        )

            real_parent = directory / "real-parent"
            real_parent.mkdir()
            linked_parent = directory / "linked-parent"
            linked_parent.symlink_to(real_parent, target_is_directory=True)
            with self.assertRaisesRegex(LiveControlError, "symlink"):
                AttemptJournal(
                    linked_parent / "attempts.json",
                    trusted_root=fixture.trusted_root,
                    execution_contract=fixture.contract,
                )

    def test_record_reservation_rejects_measured_or_fatal_terminal_attempt(self):
        from bench.compare.live_runtime import (
            AttemptJournal,
            BudgetRequest,
            BudgetReservation,
            DispatchReceipt,
            LiveControlError,
        )

        run_id = LIVE_RUN_ID
        unit_key = "case-1|r1|code-search"
        keys = [
            hashlib.sha256(
                f"{run_id}|{unit_key}|attempt-{number}".encode()
            ).hexdigest()
            for number in (1, 2)
        ]
        for error_class, classification in (
            ("timeout", "measured_error"),
            ("authentication", "fatal_error"),
        ):
            with self.subTest(classification=classification):  # noqa: SIM117
                with tempfile.TemporaryDirectory() as tmp:
                    fixture = execution_fixture(Path(tmp).resolve())
                    journal = AttemptJournal(
                        fixture.trusted_root / f"{classification}.json",
                        trusted_root=fixture.trusted_root,
                        execution_contract=fixture.contract,
                    )
                    first_request = BudgetRequest(
                        run_id=run_id,
                        unit_key=unit_key,
                        attempt_number=1,
                        idempotency_key=keys[0],
                        max_unit_usd=Decimal("0.010000"),
                        max_total_usd=Decimal("5.000000"),
                    )
                    journal.record_reservation(
                        first_request,
                        BudgetReservation(
                            reservation_id="zero-1",
                            idempotency_key=keys[0],
                            authorized_usd=Decimal("0.000000"),
                        ),
                    )
                    journal.record_dispatching(
                        descriptor_sha256=(
                            fixture.invocation.descriptor_sha256
                        )
                    )
                    journal.record_receipt(
                        DispatchReceipt(
                            operation_id="provider-operation-1",
                            idempotency_key=keys[0],
                            status="error",
                            cost_usd=Decimal("0.000000"),
                            response_sha256=None,
                            error_class=error_class,
                        )
                    )
                    journal.record_classification(
                        classification=classification,
                        retryable=False,
                    )
                    second_request = BudgetRequest(
                        run_id=run_id,
                        unit_key=unit_key,
                        attempt_number=2,
                        idempotency_key=keys[1],
                        max_unit_usd=Decimal("0.010000"),
                        max_total_usd=Decimal("5.000000"),
                    )

                    with self.assertRaisesRegex(
                        LiveControlError,
                        "terminal|retryable",
                    ):
                        journal.record_reservation(
                            second_request,
                            BudgetReservation(
                                reservation_id="zero-2",
                                idempotency_key=keys[1],
                                authorized_usd=Decimal("0.000000"),
                            ),
                        )


class ExecutionContractTests(unittest.TestCase):
    def test_execution_contract_binds_one_coherent_authority_set(self):
        from dataclasses import replace

        from bench.compare.live_runtime import (
            ExecutionContractTemplate,
            LiveControlError,
        )

        with tempfile.TemporaryDirectory() as tmp:
            fixture = execution_fixture(Path(tmp).resolve())
            mismatched_cost = replace(
                fixture.cost,
                account_scope="budget-account-B",
            )

            with self.assertRaisesRegex(LiveControlError, "authority"):
                ExecutionContractTemplate.create(
                    run_seed=LIVE_RUN_SEED,
                    unit_key="case-1|r1|code-search",
                    auth_authority=fixture.auth,
                    cost_authority=mismatched_cost,
                    controls=fixture.controls,
                    child_environments=fixture.environments,
                    invocation=fixture.invocation,
                    root=fixture.repository_root,
                )

        descriptor = fixture.contract.descriptor()
        self.assertEqual(descriptor["run_id"], LIVE_RUN_ID)
        self.assertEqual(descriptor["run_seed"], LIVE_RUN_SEED)
        self.assertEqual(
            descriptor["template_sha256"],
            fixture.template.template_sha256,
        )
        self.assertEqual(
            {
                "cli_version": descriptor["cli_version"],
                "cli_sha256": descriptor["cli_sha256"],
                "credential_source": descriptor["credential_source"],
                "account_scope": descriptor["account_scope"],
                "expected_units": descriptor["expected_units"],
                "cost_mechanism": descriptor["cost_mechanism"],
                "calibration_sha256": descriptor["calibration_sha256"],
                "auth_issuer": descriptor["auth_issuer"],
                "auth_key_id": descriptor["auth_key_id"],
                "auth_expires_at": descriptor["auth_expires_at"],
                "cost_issuer": descriptor["cost_issuer"],
                "cost_key_id": descriptor["cost_key_id"],
                "cost_expires_at": descriptor["cost_expires_at"],
            },
            {
                "cli_version": fixture.auth.cli_version,
                "cli_sha256": fixture.auth.cli_sha256,
                "credential_source": fixture.auth.credential_source,
                "account_scope": fixture.auth.account_scope,
                "expected_units": fixture.cost.expected_units,
                "cost_mechanism": fixture.cost.mechanism,
                "calibration_sha256": fixture.cost.calibration_sha256,
                "auth_issuer": fixture.auth.issuer,
                "auth_key_id": fixture.auth.key_id,
                "auth_expires_at": "2026-07-27T12:05:00Z",
                "cost_issuer": fixture.cost.issuer,
                "cost_key_id": fixture.cost.key_id,
                "cost_expires_at": "2026-07-27T12:05:00Z",
            },
        )

    def test_authority_boundary_rebinds_official_snapshot_claims_before_io(
        self,
    ):
        from dataclasses import replace

        from bench.compare.live_runtime import (
            AttemptJournal,
            AuthExpectation,
            AuthorityBoundary,
            CostExpectation,
            LiveControlError,
            load_auth_authority,
            load_cost_authority,
            run_attempt_lifecycle,
        )

        with tempfile.TemporaryDirectory() as tmp:
            fixture = execution_fixture(Path(tmp).resolve())
            verifier = DigestSignatureVerifier()
            altered_authorities = []

            for label, changes in (
                (
                    "cost total",
                    {"max_total_usd": "6.000000"},
                ),
                (
                    "cost unit",
                    {"max_unit_usd": "0.009000"},
                ),
                (
                    "cost calibration",
                    {"calibration_sha256": "d" * 64},
                ),
            ):
                claims = cost_claims(**changes)
                path = fixture.trusted_root / f"{label.replace(' ', '-')}.json"
                path.write_bytes(
                    signed_authority(
                        kind="operation_cost_authority_v2",
                        claims=claims,
                        verifier=verifier,
                    )
                )
                altered_authorities.append(
                    (
                        label,
                        fixture.auth,
                        load_cost_authority(
                            path,
                            trusted_root=fixture.trusted_root,
                            expected=CostExpectation(
                                run_seed=LIVE_RUN_SEED,
                                provider="anthropic",
                                model_id="claude-opus-4-1",
                                account_scope="account-code-intel",
                                endpoint="https://api.anthropic.com",
                                max_total_usd=Decimal(
                                    claims["max_total_usd"]
                                ),
                                max_unit_usd=Decimal(
                                    claims["max_unit_usd"]
                                ),
                                expected_units=500,
                                calibration_sha256=claims[
                                    "calibration_sha256"
                                ],
                            ),
                            verifier=verifier,
                            now=datetime(2026, 7, 27, 12, tzinfo=UTC),
                        ),
                    )
                )

            for label, changes in (
                ("auth CLI version", {"cli_version": "2.1.221"}),
                ("auth CLI hash", {"cli_sha256": "b" * 64}),
                (
                    "auth credential source",
                    {"credential_source": "anthropic_api_key"},
                ),
            ):
                claims = auth_claims(**changes)
                path = fixture.trusted_root / f"{label.replace(' ', '-')}.json"
                path.write_bytes(
                    signed_authority(
                        kind="claude_bare_auth_v2",
                        claims=claims,
                        verifier=verifier,
                    )
                )
                altered_authorities.append(
                    (
                        label,
                        load_auth_authority(
                            path,
                            trusted_root=fixture.trusted_root,
                            expected=AuthExpectation(
                                run_seed=LIVE_RUN_SEED,
                                provider="anthropic",
                                model_id="claude-opus-4-1",
                                cli_version=claims["cli_version"],
                                cli_sha256=claims["cli_sha256"],
                                account_scope="account-code-intel",
                                endpoint="https://api.anthropic.com",
                            ),
                            verifier=verifier,
                            now=datetime(2026, 7, 27, 12, tzinfo=UTC),
                        ),
                        fixture.cost,
                    )
                )

            altered_authorities.extend(
                (
                    (
                        "post-load cost total",
                        fixture.auth,
                        replace(
                            fixture.cost,
                            max_total_usd=Decimal("6.000000"),
                        ),
                    ),
                    (
                        "post-load cost unit",
                        fixture.auth,
                        replace(
                            fixture.cost,
                            max_unit_usd=Decimal("0.009000"),
                        ),
                    ),
                    (
                        "post-load cost calibration",
                        fixture.auth,
                        replace(
                            fixture.cost,
                            calibration_sha256="d" * 64,
                        ),
                    ),
                    (
                        "post-load auth CLI version",
                        replace(
                            fixture.auth,
                            cli_version="2.1.221",
                        ),
                        fixture.cost,
                    ),
                    (
                        "post-load auth CLI hash",
                        replace(
                            fixture.auth,
                            cli_sha256="b" * 64,
                        ),
                        fixture.cost,
                    ),
                    (
                        "post-load auth credential source",
                        replace(
                            fixture.auth,
                            credential_source="anthropic_api_key",
                        ),
                        fixture.cost,
                    ),
                )
            )

            for index, (label, auth, cost) in enumerate(
                altered_authorities,
                1,
            ):
                with self.subTest(label=label):
                    broker = ZeroDollarBroker()
                    executor = ScriptedExecutor([])
                    boundary = AuthorityBoundary(
                        auth_authority=auth,
                        cost_authority=cost,
                        clock=fixture.clock,
                    )
                    with self.assertRaises(LiveControlError):
                        run_attempt_lifecycle(
                            journal=AttemptJournal(
                                fixture.trusted_root
                                / f"altered-authority-{index}.json",
                                trusted_root=fixture.trusted_root,
                                execution_contract=fixture.contract,
                            ),
                            execution_contract=fixture.contract,
                            authority_boundary=boundary,
                            invocation=fixture.invocation,
                            child_environments=fixture.environments,
                            controls=fixture.controls,
                            root=fixture.repository_root,
                            broker=broker,
                            executor=executor,
                        )
                    self.assertEqual(broker.reserve_calls, [])
                    self.assertEqual(broker.reconcile_calls, [])
                    self.assertEqual(executor.calls, [])

    def test_authority_boundary_expires_at_the_exact_signed_deadline(self):
        from bench.compare.live_runtime import (
            AuthorityBoundary,
            LiveControlError,
        )

        with tempfile.TemporaryDirectory() as tmp:
            fixture = execution_fixture(Path(tmp).resolve())
            boundary = AuthorityBoundary(
                auth_authority=fixture.auth,
                cost_authority=fixture.cost,
                clock=MutableClock(
                    datetime(2026, 7, 27, 12, 5, tzinfo=UTC)
                ),
            )

            with self.assertRaisesRegex(LiveControlError, "expired"):
                boundary.validate(
                    fixture.contract,
                    controls=fixture.controls,
                )

    def test_expired_post_parse_authorities_block_before_reservation(self):
        from bench.compare.live_runtime import (
            AttemptJournal,
            AuthorityBoundary,
            DispatchReceipt,
            LiveControlError,
            run_attempt_lifecycle,
        )

        with tempfile.TemporaryDirectory() as tmp:
            fixture = execution_fixture(Path(tmp).resolve())
            boundary = AuthorityBoundary(
                auth_authority=fixture.auth,
                cost_authority=fixture.cost,
                clock=MutableClock(
                    datetime(2026, 7, 27, 12, 5, tzinfo=UTC)
                ),
            )
            broker = ZeroDollarBroker()
            idempotency_key = hashlib.sha256(
                (
                    f"{LIVE_RUN_ID}|case-1|r1|code-search|attempt-1"
                ).encode()
            ).hexdigest()
            executor = ScriptedExecutor(
                [
                    DispatchReceipt(
                        operation_id="provider-operation-001",
                        idempotency_key=idempotency_key,
                        status="ok",
                        cost_usd=Decimal("0.000000"),
                        response_sha256="e" * 64,
                        error_class=None,
                    )
                ]
            )

            with self.assertRaisesRegex(LiveControlError, "expired"):
                run_attempt_lifecycle(
                    journal=AttemptJournal(
                        fixture.trusted_root / "expired-authority.json",
                        trusted_root=fixture.trusted_root,
                        execution_contract=fixture.contract,
                    ),
                    execution_contract=fixture.contract,
                    authority_boundary=boundary,
                    invocation=fixture.invocation,
                    child_environments=fixture.environments,
                    controls=fixture.controls,
                    root=fixture.repository_root,
                    broker=broker,
                    executor=executor,
                )

        self.assertEqual(broker.reserve_calls, [])
        self.assertEqual(broker.reconcile_calls, [])
        self.assertEqual(executor.calls, [])

    def test_authority_freshness_is_rechecked_at_each_external_boundary(self):
        from bench.compare.live_runtime import (
            AttemptJournal,
            BudgetRequest,
            BudgetReservation,
            DispatchReceipt,
            DispatchReconciliation,
            LiveControlError,
            run_attempt_lifecycle,
        )

        expiry = datetime(2026, 7, 27, 12, 5, tzinfo=UTC)

        def durable_attempt(fixture, path, *, phase):
            key = hashlib.sha256(
                (
                    f"{LIVE_RUN_ID}|case-1|r1|code-search|attempt-1"
                ).encode()
            ).hexdigest()
            request = BudgetRequest(
                run_id=fixture.contract.run_id,
                unit_key=fixture.contract.unit_key,
                attempt_number=1,
                idempotency_key=key,
                max_unit_usd=Decimal("0.010000"),
                max_total_usd=Decimal("5.000000"),
            )
            reservation = BudgetReservation(
                reservation_id="zero-1",
                idempotency_key=key,
                authorized_usd=Decimal("0.000000"),
            )
            receipt = DispatchReceipt(
                operation_id="provider-operation-001",
                idempotency_key=key,
                status="ok",
                cost_usd=Decimal("0.000000"),
                response_sha256="e" * 64,
                error_class=None,
            )
            journal = AttemptJournal(
                path,
                trusted_root=fixture.trusted_root,
                execution_contract=fixture.contract,
            )
            journal.record_reservation(request, reservation)
            if phase in {"dispatching", "classified"}:
                journal.record_dispatching(
                    descriptor_sha256=fixture.invocation.descriptor_sha256
                )
            if phase == "classified":
                journal.record_receipt(receipt)
                journal.record_classification(
                    classification="success",
                    retryable=False,
                )
            return journal, receipt

        class ExpiringReserveBroker(ZeroDollarBroker):
            def __init__(self, clock):
                super().__init__()
                self.clock = clock

            def reserve(self, request):
                reservation = super().reserve(request)
                self.clock.current = expiry
                return reservation

        class ExpiringReconcileBroker(ZeroDollarBroker):
            def __init__(self, clock, receipt):
                super().__init__()
                self.clock = clock
                self.receipt = receipt

            def reconcile(self, *, idempotency_key):
                self.reconcile_calls.append(idempotency_key)
                self.clock.current = expiry
                return DispatchReconciliation(
                    state="settled",
                    receipt=self.receipt,
                )

        with tempfile.TemporaryDirectory() as tmp:
            fixture = execution_fixture(Path(tmp).resolve())
            key = hashlib.sha256(
                (
                    f"{LIVE_RUN_ID}|case-1|r1|code-search|attempt-1"
                ).encode()
            ).hexdigest()
            receipt = DispatchReceipt(
                operation_id="provider-operation-001",
                idempotency_key=key,
                status="ok",
                cost_usd=Decimal("0.000000"),
                response_sha256="e" * 64,
                error_class=None,
            )
            reserve_broker = ExpiringReserveBroker(fixture.clock)
            reserve_executor = ScriptedExecutor([receipt])

            with self.assertRaisesRegex(LiveControlError, "expired"):
                run_attempt_lifecycle(
                    journal=AttemptJournal(
                        fixture.trusted_root / "expire-before-dispatch.json",
                        trusted_root=fixture.trusted_root,
                        execution_contract=fixture.contract,
                    ),
                    execution_contract=fixture.contract,
                    authority_boundary=fixture.authority_boundary,
                    invocation=fixture.invocation,
                    child_environments=fixture.environments,
                    controls=fixture.controls,
                    root=fixture.repository_root,
                    broker=reserve_broker,
                    executor=reserve_executor,
                )
            self.assertEqual(len(reserve_broker.reserve_calls), 1)
            self.assertEqual(reserve_broker.reconcile_calls, [])
            self.assertEqual(reserve_executor.calls, [])

        with tempfile.TemporaryDirectory() as tmp:
            fixture = execution_fixture(Path(tmp).resolve())
            journal, _receipt = durable_attempt(
                fixture,
                fixture.trusted_root / "expired-before-reconcile.json",
                phase="dispatching",
            )
            fixture.clock.current = expiry
            reconcile_broker = ZeroDollarBroker()
            reconcile_executor = ScriptedExecutor([])

            with self.assertRaisesRegex(LiveControlError, "expired"):
                run_attempt_lifecycle(
                    journal=journal,
                    execution_contract=fixture.contract,
                    authority_boundary=fixture.authority_boundary,
                    invocation=fixture.invocation,
                    child_environments=fixture.environments,
                    controls=fixture.controls,
                    root=fixture.repository_root,
                    broker=reconcile_broker,
                    executor=reconcile_executor,
                )
            self.assertEqual(reconcile_broker.reserve_calls, [])
            self.assertEqual(reconcile_broker.reconcile_calls, [])
            self.assertEqual(reconcile_executor.calls, [])

        with tempfile.TemporaryDirectory() as tmp:
            fixture = execution_fixture(Path(tmp).resolve())
            journal, receipt = durable_attempt(
                fixture,
                fixture.trusted_root / "expire-before-accept.json",
                phase="classified",
            )
            accept_broker = ExpiringReconcileBroker(
                fixture.clock,
                receipt,
            )
            accept_executor = ScriptedExecutor([])

            with self.assertRaisesRegex(LiveControlError, "expired"):
                run_attempt_lifecycle(
                    journal=journal,
                    execution_contract=fixture.contract,
                    authority_boundary=fixture.authority_boundary,
                    invocation=fixture.invocation,
                    child_environments=fixture.environments,
                    controls=fixture.controls,
                    root=fixture.repository_root,
                    broker=accept_broker,
                    executor=accept_executor,
                )
            self.assertEqual(accept_broker.reserve_calls, [])
            self.assertEqual(len(accept_broker.reconcile_calls), 1)
            self.assertEqual(accept_executor.calls, [])

    def test_execution_contract_binds_sealed_environment_system_and_online_limits(
        self,
    ):
        from bench.compare.live_runtime import (
            AdapterLimitError,
            ExecutionContract,
            ExecutionContractTemplate,
            OnlineLimitGuard,
            build_child_environments,
            compile_claude_invocation,
            load_auth_authority,
            load_cost_authority,
        )

        verifier = DigestSignatureVerifier()
        repository_root = Path(__file__).resolve().parents[1]
        response_schema = json.loads(
            (repository_root / "bench/compare/response-schema.json").read_text(
                encoding="utf-8"
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            trusted_root = Path(tmp).resolve()
            auth_path = trusted_root / "auth.json"
            cost_path = trusted_root / "cost.json"
            auth_path.write_bytes(
                signed_authority(
                    kind="claude_bare_auth_v2",
                    claims=auth_claims(),
                    verifier=verifier,
                )
            )
            cost_path.write_bytes(
                signed_authority(
                    kind="operation_cost_authority_v2",
                    claims=cost_claims(),
                    verifier=verifier,
                )
            )
            auth = load_auth_authority(
                auth_path,
                trusted_root=trusted_root,
                expected=auth_expectation(),
                verifier=verifier,
                now=datetime(2026, 7, 27, 12, tzinfo=UTC),
            )
            cost = load_cost_authority(
                cost_path,
                trusted_root=trusted_root,
                expected=cost_expectation(),
                verifier=verifier,
                now=datetime(2026, 7, 27, 12, tzinfo=UTC),
            )
            environments = build_child_environments(
                arm="code-search",
                ambient={"PATH": "/usr/bin:/bin"},
                trusted_root=trusted_root,
                isolated_root=trusted_root / "children",
                auth_authority=auth,
                fetch_credentials={"GH_TOKEN": "explicit-fetch-secret"},
                model_credentials={},
            )
            invocation = compile_claude_invocation(
                arm="code-search",
                prompt="Locate the authorization boundary.",
                response_schema=response_schema,
                controls=live_controls(),
                mcp_servers=mcp_server_specs(),
                auth_authority=auth,
                child_environments=environments,
                root=repository_root,
            )
            template = ExecutionContractTemplate.create(
                run_seed=LIVE_RUN_SEED,
                unit_key="case-1|r1|code-search",
                auth_authority=auth,
                cost_authority=cost,
                controls=live_controls(),
                child_environments=environments,
                invocation=invocation,
                root=repository_root,
            )
            contract = ExecutionContract.create(
                run_id=LIVE_RUN_ID,
                template=template,
            )

        system_text = (
            repository_root / "bench/compare/system.md"
        ).read_text(encoding="utf-8")
        system_index = invocation.argv.index("--system-prompt")
        self.assertEqual(invocation.argv[system_index + 1], system_text)
        mcp_config = json.loads(invocation.mcp_config_json)
        self.assertEqual(
            mcp_config["mcpServers"]["code-search"]["env"],
            dict(dict(environments.mcp)["code-search"]),
        )
        self.assertEqual(
            contract.auth_snapshot_sha256,
            auth.snapshot_sha256,
        )
        self.assertEqual(
            contract.cost_snapshot_sha256,
            cost.snapshot_sha256,
        )
        self.assertEqual(
            contract.environment_sha256,
            environments.descriptor_sha256,
        )
        self.assertEqual(
            contract.invocation_descriptor_sha256,
            invocation.descriptor_sha256,
        )
        self.assertEqual(
            invocation.argv[0],
            "/opt/anthropic/bin/claude",
        )
        self.assertEqual(invocation.executable_sha256, auth.cli_sha256)

        guard = OnlineLimitGuard(invocation.adapter_enforcement)
        for _ in range(invocation.max_discovery_tool_calls):
            guard.authorize_tool_call()
        with self.assertRaises(AdapterLimitError):
            guard.authorize_tool_call()
        guard.accept_evidence_tokens(invocation.evidence_token_budget)
        with self.assertRaises(AdapterLimitError):
            guard.accept_evidence_tokens(1)
        guard.accept_context_tokens(invocation.context_token_budget)
        with self.assertRaises(AdapterLimitError):
            guard.accept_context_tokens(1)
        guard.check_wall_time(invocation.wall_timeout_seconds)
        with self.assertRaises(AdapterLimitError):
            guard.check_wall_time(invocation.wall_timeout_seconds + 1)

    def test_runtime_recomputes_invocation_and_requires_adapter_completion(self):
        from dataclasses import replace

        from bench.compare.live_runtime import (
            AttemptJournal,
            DispatchReceipt,
            LiveControlError,
            run_attempt_lifecycle,
        )

        class UnguardedExecutor:
            def __init__(self):
                self.launch_environment = None

            def dispatch(
                self,
                *,
                invocation,
                launch_environment,
                enforcement,
                reservation,
                idempotency_key,
            ):
                self.launch_environment = launch_environment
                return DispatchReceipt(
                    operation_id="provider-operation-001",
                    idempotency_key=idempotency_key,
                    status="ok",
                    cost_usd=Decimal("0.000000"),
                    response_sha256="e" * 64,
                    error_class=None,
                )

        with tempfile.TemporaryDirectory() as tmp:
            fixture = execution_fixture(Path(tmp).resolve())
            tampered = replace(
                fixture.invocation,
                argv=(*fixture.invocation.argv, "--unreviewed"),
            )
            tampered = replace(
                tampered,
                descriptor_sha256=hashlib.sha256(
                    canonical(tampered._descriptor())
                ).hexdigest(),
            )
            broker = ZeroDollarBroker()
            executor = ScriptedExecutor([])
            with self.assertRaisesRegex(LiveControlError, "invocation"):
                run_attempt_lifecycle(
                    journal=AttemptJournal(
                        fixture.trusted_root / "tampered-invocation.json",
                        trusted_root=fixture.trusted_root,
                        execution_contract=fixture.contract,
                    ),
                    execution_contract=fixture.contract,
                    authority_boundary=fixture.authority_boundary,
                    invocation=tampered,
                    child_environments=fixture.environments,
                    controls=fixture.controls,
                    root=fixture.repository_root,
                    broker=broker,
                    executor=executor,
                )
            self.assertEqual(broker.reserve_calls, [])
            self.assertEqual(executor.calls, [])

            unguarded = UnguardedExecutor()
            with self.assertRaisesRegex(LiveControlError, "enforcement"):
                run_attempt_lifecycle(
                    journal=AttemptJournal(
                        fixture.trusted_root / "unguarded.json",
                        trusted_root=fixture.trusted_root,
                        execution_contract=fixture.contract,
                    ),
                    execution_contract=fixture.contract,
                    authority_boundary=fixture.authority_boundary,
                    invocation=fixture.invocation,
                    child_environments=fixture.environments,
                    controls=fixture.controls,
                    root=fixture.repository_root,
                    broker=ZeroDollarBroker(),
                    executor=unguarded,
                )
            self.assertIsNotNone(unguarded.launch_environment)
            self.assertFalse(hasattr(unguarded.launch_environment, "fetch"))
            self.assertEqual(
                dict(unguarded.launch_environment.model),
                dict(fixture.environments.model),
            )

    def test_journal_persists_execution_contract_and_chains_every_transition(self):
        from dataclasses import replace

        from bench.compare.live_runtime import (
            AttemptJournal,
            DispatchReceipt,
            LiveControlError,
            run_attempt_lifecycle,
        )

        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            fixture = execution_fixture(directory)
            journal_path = fixture.trusted_root / "attempts.json"
            journal = AttemptJournal(
                journal_path,
                trusted_root=fixture.trusted_root,
                execution_contract=fixture.contract,
            )
            initial = json.loads(journal_path.read_text(encoding="utf-8"))
            self.assertEqual(
                initial["execution_contract_sha256"],
                fixture.contract.descriptor_sha256,
            )
            self.assertEqual(initial["run_seed"], LIVE_RUN_SEED)
            self.assertEqual(
                initial["execution_contract_template_sha256"],
                fixture.template.template_sha256,
            )
            self.assertEqual(
                initial["execution_contract"]["controls_sha256"],
                fixture.contract.controls_sha256,
            )
            self.assertEqual(initial["events"], [])

            key = hashlib.sha256(
                (
                    f"{fixture.contract.run_id}|"
                    f"{fixture.contract.unit_key}|attempt-1"
                ).encode()
            ).hexdigest()
            outcome = run_attempt_lifecycle(
                journal=journal,
                execution_contract=fixture.contract,
                authority_boundary=fixture.authority_boundary,
                invocation=fixture.invocation,
                child_environments=fixture.environments,
                controls=fixture.controls,
                root=fixture.repository_root,
                broker=ZeroDollarBroker(),
                executor=ScriptedExecutor(
                    [
                        DispatchReceipt(
                            operation_id="provider-operation-001",
                            idempotency_key=key,
                            status="ok",
                            cost_usd=Decimal("0.000000"),
                            response_sha256="e" * 64,
                            error_class=None,
                        )
                    ],
                    journal_path=journal_path,
                ),
            )

            self.assertEqual(outcome.classification, "success")
            durable = json.loads(journal_path.read_text(encoding="utf-8"))
            self.assertEqual(
                [event["phase"] for event in durable["events"]],
                ["reserved", "dispatching", "receipt", "classified"],
            )
            previous = durable["journal_identity_sha256"]
            for sequence, event in enumerate(durable["events"], 1):
                self.assertEqual(event["sequence"], sequence)
                self.assertEqual(event["previous_sha256"], previous)
                previous = event["event_sha256"]

            broker = ZeroDollarBroker()
            executor = ScriptedExecutor([])
            with self.assertRaisesRegex(LiveControlError, "frozen|contract"):
                run_attempt_lifecycle(
                    journal=AttemptJournal(
                        journal_path,
                        trusted_root=fixture.trusted_root,
                        execution_contract=fixture.contract,
                    ),
                    execution_contract=fixture.contract,
                    authority_boundary=fixture.authority_boundary,
                    invocation=fixture.invocation,
                    child_environments=fixture.environments,
                    controls=replace(fixture.controls, max_attempts=3),
                    root=fixture.repository_root,
                    broker=broker,
                    executor=executor,
                )
            self.assertEqual(broker.reserve_calls, [])
            self.assertEqual(broker.reconcile_calls, [])
            self.assertEqual(executor.calls, [])

    def test_journal_initial_creation_holds_the_cross_process_unit_lock(self):
        from bench.compare.live_runtime import AttemptJournal

        with tempfile.TemporaryDirectory() as tmp:
            fixture = execution_fixture(Path(tmp).resolve())
            journal_path = (
                fixture.trusted_root / "constructor-lock.json"
            )
            with patch(
                "bench.compare.live_runtime.fcntl.flock",
                wraps=fcntl.flock,
            ) as locked:
                AttemptJournal(
                    journal_path,
                    trusted_root=fixture.trusted_root,
                    execution_contract=fixture.contract,
                )
            root_state = os.stat(fixture.trusted_root)
            parent_state = os.stat(journal_path.parent)
            expected_lock = hashlib.sha256(
                canonical(
                    {
                        "schema_version": 1,
                        "trusted_root": {
                            "device": root_state.st_dev,
                            "inode": root_state.st_ino,
                        },
                        "parent": {
                            "device": parent_state.st_dev,
                            "inode": parent_state.st_ino,
                        },
                        "basename": journal_path.name,
                    }
                )
            ).hexdigest()
            lock_files = {
                path.name
                for path in (
                    fixture.trusted_root / ".attempt-locks"
                ).iterdir()
            }
            self.assertIn(f"{expected_lock}.lock", lock_files)

        self.assertTrue(
            any(
                call.args[1] == fcntl.LOCK_EX
                for call in locked.call_args_list
            )
        )

    def test_resumed_classified_success_requires_exact_broker_reconciliation(self):
        from bench.compare.live_runtime import (
            AttemptJournal,
            DispatchReceipt,
            DispatchReconciliation,
            UnresolvedDispatchError,
            run_attempt_lifecycle,
        )

        with tempfile.TemporaryDirectory() as tmp:
            fixture = execution_fixture(Path(tmp).resolve())
            path = fixture.trusted_root / "classified.json"
            key = hashlib.sha256(
                (
                    f"{fixture.contract.run_id}|"
                    f"{fixture.contract.unit_key}|attempt-1"
                ).encode()
            ).hexdigest()
            receipt = DispatchReceipt(
                operation_id="provider-operation-001",
                idempotency_key=key,
                status="ok",
                cost_usd=Decimal("0.000000"),
                response_sha256="e" * 64,
                error_class=None,
            )
            run_attempt_lifecycle(
                journal=AttemptJournal(
                    path,
                    trusted_root=fixture.trusted_root,
                    execution_contract=fixture.contract,
                ),
                execution_contract=fixture.contract,
                authority_boundary=fixture.authority_boundary,
                invocation=fixture.invocation,
                child_environments=fixture.environments,
                controls=fixture.controls,
                root=fixture.repository_root,
                broker=ZeroDollarBroker(),
                executor=ScriptedExecutor([receipt], journal_path=path),
            )

            forged_path = fixture.trusted_root / "forged-classified.json"
            forged_path.write_bytes(path.read_bytes())
            unknown_broker = ZeroDollarBroker()
            executor = ScriptedExecutor([])
            with self.assertRaises(UnresolvedDispatchError):
                run_attempt_lifecycle(
                    journal=AttemptJournal(
                        forged_path,
                        trusted_root=fixture.trusted_root,
                        execution_contract=fixture.contract,
                    ),
                    execution_contract=fixture.contract,
                    authority_boundary=fixture.authority_boundary,
                    invocation=fixture.invocation,
                    child_environments=fixture.environments,
                    controls=fixture.controls,
                    root=fixture.repository_root,
                    broker=unknown_broker,
                    executor=executor,
                )
            self.assertEqual(unknown_broker.reconcile_calls, [key])
            self.assertEqual(executor.calls, [])

            settled_broker = ZeroDollarBroker()
            settled_broker.reconciliations[key] = DispatchReconciliation(
                state="settled",
                receipt=receipt,
            )
            outcome = run_attempt_lifecycle(
                journal=AttemptJournal(
                    forged_path,
                    trusted_root=fixture.trusted_root,
                    execution_contract=fixture.contract,
                ),
                execution_contract=fixture.contract,
                authority_boundary=fixture.authority_boundary,
                invocation=fixture.invocation,
                child_environments=fixture.environments,
                controls=fixture.controls,
                root=fixture.repository_root,
                broker=settled_broker,
                executor=executor,
            )
            self.assertEqual(outcome.classification, "success")
            self.assertEqual(settled_broker.reconcile_calls, [key])
            self.assertEqual(executor.calls, [])

    def test_concurrent_callers_share_one_atomic_reservation_and_dispatch_claim(self):
        from bench.compare.live_runtime import (
            AttemptJournal,
            run_attempt_lifecycle,
        )

        with tempfile.TemporaryDirectory() as tmp:
            fixture = execution_fixture(Path(tmp).resolve())
            path = fixture.trusted_root / "concurrent.json"
            first = AttemptJournal(
                path,
                trusted_root=fixture.trusted_root,
                execution_contract=fixture.contract,
            )
            second = AttemptJournal(
                path,
                trusted_root=fixture.trusted_root,
                execution_contract=fixture.contract,
            )
            broker = BlockingConcurrentBroker()
            executor = RecordingConcurrentExecutor(broker)
            outcomes = []
            errors = []

            def invoke(journal):
                try:
                    outcomes.append(
                        run_attempt_lifecycle(
                            journal=journal,
                            execution_contract=fixture.contract,
                            authority_boundary=fixture.authority_boundary,
                            invocation=fixture.invocation,
                            child_environments=fixture.environments,
                            controls=fixture.controls,
                            root=fixture.repository_root,
                            broker=broker,
                            executor=executor,
                        )
                    )
                except (
                    AssertionError,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ) as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            first_thread = threading.Thread(target=invoke, args=(first,))
            second_thread = threading.Thread(target=invoke, args=(second,))
            first_thread.start()
            self.assertTrue(broker.first_reserve_entered.wait(timeout=2))
            second_thread.start()
            time.sleep(0.05)
            broker.release_first_reserve.set()
            first_thread.join(timeout=2)
            second_thread.join(timeout=2)

            self.assertFalse(first_thread.is_alive())
            self.assertFalse(second_thread.is_alive())
            self.assertEqual(errors, [])
            self.assertEqual(len(outcomes), 2)
            self.assertEqual(len(broker.reserve_calls), 1)
            self.assertEqual(len(executor.calls), 1)

    def test_noncanonical_alias_cannot_split_the_concurrent_unit_lock(self):
        from bench.compare.live_runtime import (
            AttemptJournal,
            LiveControlError,
            run_attempt_lifecycle,
        )

        with tempfile.TemporaryDirectory() as tmp:
            fixture = execution_fixture(Path(tmp).resolve())
            canonical_path = fixture.trusted_root / "alias-race.json"
            alias_directory = fixture.trusted_root / "alias"
            alias_directory.mkdir()
            alias_path = (
                alias_directory
                / ".."
                / canonical_path.name
            )
            broker = BlockingConcurrentBroker()
            executor = RecordingConcurrentExecutor(broker)
            outcomes = []
            errors = []

            def invoke(path):
                try:
                    journal = AttemptJournal(
                        path,
                        trusted_root=fixture.trusted_root,
                        execution_contract=fixture.contract,
                    )
                    outcomes.append(
                        run_attempt_lifecycle(
                            journal=journal,
                            execution_contract=fixture.contract,
                            authority_boundary=fixture.authority_boundary,
                            invocation=fixture.invocation,
                            child_environments=fixture.environments,
                            controls=fixture.controls,
                            root=fixture.repository_root,
                            broker=broker,
                            executor=executor,
                        )
                    )
                except (
                    AssertionError,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ) as exc:
                    errors.append(exc)

            canonical_thread = threading.Thread(
                target=invoke,
                args=(canonical_path,),
            )
            alias_thread = threading.Thread(
                target=invoke,
                args=(alias_path,),
            )
            canonical_thread.start()
            self.assertTrue(broker.first_reserve_entered.wait(timeout=2))
            alias_thread.start()
            time.sleep(0.05)
            broker.release_first_reserve.set()
            canonical_thread.join(timeout=2)
            alias_thread.join(timeout=2)

            self.assertFalse(canonical_thread.is_alive())
            self.assertFalse(alias_thread.is_alive())
            self.assertEqual(len(outcomes), 1)
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], LiveControlError)
            self.assertRegex(str(errors[0]), "noncanonical")
            self.assertEqual(len(broker.reserve_calls), 1)
            self.assertEqual(len(executor.calls), 1)

    def test_hardlink_alias_cannot_split_the_concurrent_unit_lock(self):
        from bench.compare.live_runtime import (
            AttemptJournal,
            LiveControlError,
            run_attempt_lifecycle,
        )

        with tempfile.TemporaryDirectory() as tmp:
            fixture = execution_fixture(Path(tmp).resolve())
            canonical_path = fixture.trusted_root / "hardlink-race.json"
            alias_path = fixture.trusted_root / "hardlink-alias.json"
            AttemptJournal(
                canonical_path,
                trusted_root=fixture.trusted_root,
                execution_contract=fixture.contract,
            )
            os.link(canonical_path, alias_path)
            broker = BlockingConcurrentBroker()
            executor = RecordingConcurrentExecutor(broker)
            outcomes = []
            errors = []
            start = threading.Barrier(3)

            def invoke(path):
                start.wait(timeout=2)
                try:
                    journal = AttemptJournal(
                        path,
                        trusted_root=fixture.trusted_root,
                        execution_contract=fixture.contract,
                    )
                    outcomes.append(
                        run_attempt_lifecycle(
                            journal=journal,
                            execution_contract=fixture.contract,
                            authority_boundary=fixture.authority_boundary,
                            invocation=fixture.invocation,
                            child_environments=fixture.environments,
                            controls=fixture.controls,
                            root=fixture.repository_root,
                            broker=broker,
                            executor=executor,
                        )
                    )
                except (
                    AssertionError,
                    RuntimeError,
                    TypeError,
                    ValueError,
                ) as exc:
                    errors.append(exc)

            canonical_thread = threading.Thread(
                target=invoke,
                args=(canonical_path,),
            )
            alias_thread = threading.Thread(
                target=invoke,
                args=(alias_path,),
            )
            canonical_thread.start()
            alias_thread.start()
            start.wait(timeout=2)
            broker.first_reserve_entered.wait(timeout=0.5)
            broker.release_first_reserve.set()
            canonical_thread.join(timeout=2)
            alias_thread.join(timeout=2)

            self.assertFalse(canonical_thread.is_alive())
            self.assertFalse(alias_thread.is_alive())
            self.assertEqual(outcomes, [])
            self.assertEqual(len(errors), 2)
            self.assertTrue(
                all(isinstance(error, LiveControlError) for error in errors)
            )
            self.assertTrue(
                all(
                    "single-link regular file" in str(error)
                    for error in errors
                )
            )
            self.assertEqual(broker.reserve_calls, [])
            self.assertEqual(executor.calls, [])

    def test_hardlinked_unit_lock_file_rejects_before_external_calls(self):
        from bench.compare.live_runtime import (
            AttemptJournal,
            LiveControlError,
        )

        with tempfile.TemporaryDirectory() as tmp:
            fixture = execution_fixture(Path(tmp).resolve())
            path = fixture.trusted_root / "hardlinked-lock.json"
            AttemptJournal(
                path,
                trusted_root=fixture.trusted_root,
                execution_contract=fixture.contract,
            )
            lock_files = tuple(
                (fixture.trusted_root / ".attempt-locks").glob("*.lock")
            )
            self.assertEqual(len(lock_files), 1)
            os.link(
                lock_files[0],
                lock_files[0].with_name("hardlink-alias.lock"),
            )
            broker = BlockingConcurrentBroker()
            executor = RecordingConcurrentExecutor(broker)

            with self.assertRaisesRegex(
                LiveControlError,
                "single-link regular file",
            ):
                AttemptJournal(
                    path,
                    trusted_root=fixture.trusted_root,
                    execution_contract=fixture.contract,
                )

            self.assertEqual(broker.reserve_calls, [])
            self.assertEqual(executor.calls, [])

    def test_compiler_requires_sealed_exact_child_and_mcp_launch_inputs(self):
        from dataclasses import replace

        from bench.compare.live_runtime import (
            LiveControlError,
            McpServerSpec,
            compile_claude_invocation,
        )

        with tempfile.TemporaryDirectory() as tmp:
            fixture = execution_fixture(Path(tmp).resolve())
            response_schema = json.loads(
                (
                    fixture.repository_root
                    / "bench/compare/response-schema.json"
                ).read_text(encoding="utf-8")
            )
            common = {
                "arm": "code-search",
                "prompt": "Locate the authorization boundary.",
                "response_schema": response_schema,
                "controls": fixture.controls,
                "mcp_servers": mcp_server_specs(),
                "auth_authority": fixture.auth,
                "root": fixture.repository_root,
            }
            with self.assertRaisesRegex(LiveControlError, "environment"):
                compile_claude_invocation(**common)

            search = mcp_server_specs()["code-search"]
            for argument in (
                "--header=Authorization: fixture-value",
                "--header=Bearer fixture-value",
            ):
                with self.subTest(argument=argument):
                    unsafe = McpServerSpec(
                        name=search.name,
                        command=search.command,
                        args=("--stdio", argument),
                        tools=search.tools,
                    )
                    with self.assertRaisesRegex(
                        LiveControlError,
                        "credential",
                    ):
                        compile_claude_invocation(
                            **{
                                **common,
                                "child_environments": fixture.environments,
                                "mcp_servers": {
                                    **mcp_server_specs(),
                                    "code-search": unsafe,
                                },
                            }
                        )

            tampered = replace(
                fixture.environments,
                model=(
                    *fixture.environments.model,
                    ("GH_TOKEN", "injected-fetch-secret"),
                ),
                descriptor_sha256="0" * 64,
            )
            tampered = replace(
                tampered,
                descriptor_sha256=hashlib.sha256(
                    canonical(tampered._descriptor())
                ).hexdigest(),
            )
            with self.assertRaisesRegex(LiveControlError, "environment"):
                compile_claude_invocation(
                    **{
                        **common,
                        "child_environments": tampered,
                    }
                )

            allowed_name_tamper = replace(
                fixture.environments,
                model=tuple(
                    (
                        name,
                        "/unreviewed/bin"
                        if name == "PATH"
                        else value,
                    )
                    for name, value in fixture.environments.model
                ),
            )
            allowed_name_tamper = replace(
                allowed_name_tamper,
                descriptor_sha256=hashlib.sha256(
                    canonical(allowed_name_tamper._descriptor())
                ).hexdigest(),
            )
            with self.assertRaisesRegex(LiveControlError, "environment"):
                compile_claude_invocation(
                    **{
                        **common,
                        "child_environments": allowed_name_tamper,
                    }
                )

    def test_filesystem_boundaries_require_explicit_roots_and_reject_ancestor_links(
        self,
    ):
        from bench.compare.live_runtime import (
            LiveControlError,
            build_child_environments,
            load_auth_authority,
        )

        verifier = DigestSignatureVerifier()
        with tempfile.TemporaryDirectory() as tmp:
            trusted_root = Path(tmp).resolve()
            authority_path = trusted_root / "auth.json"
            authority_path.write_bytes(
                signed_authority(
                    kind="claude_bare_auth_v2",
                    claims=auth_claims(),
                    verifier=verifier,
                )
            )
            common = {
                "expected": auth_expectation(),
                "verifier": verifier,
                "now": datetime(2026, 7, 27, 12, tzinfo=UTC),
            }
            with self.assertRaises(TypeError):
                load_auth_authority(authority_path, **common)

            real_parent = trusted_root / "real-parent"
            real_parent.mkdir()
            nested_authority = real_parent / "auth.json"
            nested_authority.write_bytes(authority_path.read_bytes())
            linked_parent = trusted_root / "linked-parent"
            linked_parent.symlink_to(real_parent, target_is_directory=True)
            with self.assertRaisesRegex(LiveControlError, "symlink"):
                load_auth_authority(
                    linked_parent / "auth.json",
                    trusted_root=trusted_root,
                    **common,
                )

            ancestor_target = trusted_root / "ancestor-target"
            ancestor_target.mkdir()
            nested_root = ancestor_target / "trusted"
            nested_root.mkdir()
            (nested_root / "auth.json").write_bytes(authority_path.read_bytes())
            ancestor_link = trusted_root / "ancestor-link"
            ancestor_link.symlink_to(
                ancestor_target,
                target_is_directory=True,
            )
            with self.assertRaisesRegex(LiveControlError, "symlink|canonical"):
                load_auth_authority(
                    ancestor_link / "trusted" / "auth.json",
                    trusted_root=ancestor_link / "trusted",
                    **common,
                )

            authority = load_auth_authority(
                authority_path,
                trusted_root=trusted_root,
                **common,
            )
            environment_common = {
                "arm": "code-search",
                "ambient": {"PATH": "/usr/bin:/bin"},
                "isolated_root": trusted_root / "children",
                "auth_authority": authority,
                "fetch_credentials": {},
                "model_credentials": {},
            }
            with self.assertRaises(TypeError):
                build_child_environments(**environment_common)
            with self.assertRaisesRegex(LiveControlError, "symlink"):
                build_child_environments(
                    **{
                        **environment_common,
                        "trusted_root": trusted_root,
                        "isolated_root": linked_parent / "children",
                    }
                )


if __name__ == "__main__":
    unittest.main()
