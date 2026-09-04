"""Inert contracts for a future live comparison executor.

This module compiles and persists control-plane data only.  It does not spawn
processes, contact providers, or expose a production executor factory.
"""

from __future__ import annotations

import errno
import fcntl
import hashlib
import hmac
import json
import os
import re
import stat
import threading
import time
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Protocol

from .schema import (
    ARM_CONTRACTS,
    FORBIDDEN_TOOLS,
    GRAPH_TOOLS,
    LIVE_RETRYABLE_ERROR_CLASSES,
    MEASURED_OUTCOME_ERROR_CLASSES,
    NONFINALIZABLE_ERROR_CLASSES,
    SEARCH_TOOLS,
    SHA256,
    ContractError,
    FrozenControls,
    canonical_json,
    canonical_usd_decimal,
    format_usd_decimal,
    tokenizer_descriptor,
    usd_decimal_from_micros,
    usd_micros,
)

_FACTORY_SEAL_KEY = os.urandom(32)


def _factory_seal(label: str, descriptor: dict) -> str:
    return hmac.new(
        _FACTORY_SEAL_KEY,
        label.encode("utf-8") + b"\0" + canonical_json(descriptor),
        hashlib.sha256,
    ).hexdigest()


class LiveControlError(ContractError):
    """A live authority or attempt contract is absent or inconsistent."""


class UnresolvedDispatchError(LiveControlError):
    """A prior dispatch cannot yet be proven settled or safe to repeat."""


class BudgetCapError(LiveControlError):
    """No cumulative per-unit or run budget remains for another attempt."""


class AdapterLimitError(LiveControlError):
    """An online adapter limit would be exceeded."""


class SignatureVerifier(Protocol):
    """Injected verifier for an issuer selected outside this zero-cost phase."""

    def verify(
        self,
        *,
        algorithm: str,
        issuer: str,
        key_id: str,
        payload: bytes,
        signature: str,
    ) -> bool: ...


@dataclass(frozen=True)
class AuthExpectation:
    run_seed: str
    provider: str
    model_id: str
    cli_version: str
    cli_sha256: str
    account_scope: str
    endpoint: str


@dataclass(frozen=True)
class AuthAuthority:
    run_seed: str
    provider: str
    model_id: str
    cli_version: str
    cli_sha256: str
    execution_mode: str
    credential_source: str
    account_scope: str
    endpoint: str
    issued_at: datetime
    expires_at: datetime
    issuer: str
    key_id: str
    snapshot_sha256: str
    _seal: str = field(repr=False, compare=False)

    def _descriptor(self) -> dict:
        return {
            "schema_version": 1,
            "authority_kind": "claude_bare_auth_v2",
            "run_seed": self.run_seed,
            "provider": self.provider,
            "model_id": self.model_id,
            "cli_version": self.cli_version,
            "cli_sha256": self.cli_sha256,
            "execution_mode": self.execution_mode,
            "credential_source": self.credential_source,
            "account_scope": self.account_scope,
            "endpoint": self.endpoint,
            "issued_at": _authority_expiry(self.issued_at),
            "expires_at": _authority_expiry(self.expires_at),
            "issuer": self.issuer,
            "key_id": self.key_id,
            "snapshot_sha256": self.snapshot_sha256,
        }

    def validate(self) -> None:
        try:
            descriptor = self._descriptor()
            _require_provider_credential_compatibility(
                provider=self.provider,
                credential_source=self.credential_source,
                context="authentication authority",
            )
        except (AttributeError, LiveControlError) as exc:
            raise LiveControlError(
                "authentication authority is not factory-sealed"
            ) from exc
        if (
            any(
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or "\n" in value
                or "\r" in value
                for value in (
                    self.run_seed,
                    self.provider,
                    self.model_id,
                    self.cli_version,
                    self.cli_sha256,
                    self.execution_mode,
                    self.credential_source,
                    self.account_scope,
                    self.endpoint,
                    self.issuer,
                    self.key_id,
                    self.snapshot_sha256,
                )
            )
            or SHA256.fullmatch(self.run_seed) is None
            or SHA256.fullmatch(self.cli_sha256) is None
            or SHA256.fullmatch(self.snapshot_sha256) is None
            or self.execution_mode != "bare"
            or not isinstance(self.issued_at, datetime)
            or not isinstance(self.expires_at, datetime)
            or self.issued_at.tzinfo is None
            or self.issued_at.utcoffset() is None
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() is None
            or self.issued_at.microsecond != 0
            or self.expires_at.microsecond != 0
            or self.issued_at >= self.expires_at
            or not isinstance(self._seal, str)
            or not hmac.compare_digest(
                self._seal,
                _factory_seal(
                    "auth-authority-v2",
                    descriptor,
                ),
            )
        ):
            raise LiveControlError(
                "authentication authority is not factory-sealed"
            )


@dataclass(frozen=True)
class CostExpectation:
    run_seed: str
    provider: str
    model_id: str
    account_scope: str
    endpoint: str
    max_total_usd: Decimal
    max_unit_usd: Decimal
    expected_units: int
    calibration_sha256: str


@dataclass(frozen=True)
class CostAuthority:
    run_seed: str
    provider: str
    model_id: str
    account_scope: str
    endpoint: str
    currency: str
    max_total_usd: Decimal
    max_unit_usd: Decimal
    expected_units: int
    calibration_sha256: str
    mechanism: str
    issued_at: datetime
    expires_at: datetime
    issuer: str
    key_id: str
    snapshot_sha256: str
    _seal: str = field(repr=False, compare=False)

    def _descriptor(self) -> dict:
        return {
            "schema_version": 1,
            "authority_kind": "operation_cost_authority_v2",
            "run_seed": self.run_seed,
            "provider": self.provider,
            "model_id": self.model_id,
            "account_scope": self.account_scope,
            "endpoint": self.endpoint,
            "currency": self.currency,
            "max_total_usd": _decimal_6(
                self.max_total_usd,
                "cost authority max_total_usd",
                positive=True,
            ),
            "max_unit_usd": _decimal_6(
                self.max_unit_usd,
                "cost authority max_unit_usd",
                positive=True,
            ),
            "expected_units": self.expected_units,
            "calibration_sha256": self.calibration_sha256,
            "mechanism": self.mechanism,
            "issued_at": _authority_expiry(self.issued_at),
            "expires_at": _authority_expiry(self.expires_at),
            "issuer": self.issuer,
            "key_id": self.key_id,
            "snapshot_sha256": self.snapshot_sha256,
        }

    def validate(self) -> None:
        try:
            descriptor = self._descriptor()
            _require_expected_cost_capacity(
                max_total_usd=self.max_total_usd,
                max_unit_usd=self.max_unit_usd,
                expected_units=self.expected_units,
                context="cost authority",
            )
        except (AttributeError, LiveControlError) as exc:
            raise LiveControlError(
                "cost authority is not factory-sealed"
            ) from exc
        if (
            any(
                not isinstance(value, str)
                or not value
                or value != value.strip()
                or "\n" in value
                or "\r" in value
                for value in (
                    self.run_seed,
                    self.provider,
                    self.model_id,
                    self.account_scope,
                    self.endpoint,
                    self.currency,
                    self.calibration_sha256,
                    self.mechanism,
                    self.issuer,
                    self.key_id,
                    self.snapshot_sha256,
                )
            )
            or SHA256.fullmatch(self.run_seed) is None
            or SHA256.fullmatch(self.calibration_sha256) is None
            or SHA256.fullmatch(self.snapshot_sha256) is None
            or self.currency != "USD"
            or self.mechanism not in COST_MECHANISMS
            or not isinstance(self.issued_at, datetime)
            or not isinstance(self.expires_at, datetime)
            or self.issued_at.tzinfo is None
            or self.issued_at.utcoffset() is None
            or self.expires_at.tzinfo is None
            or self.expires_at.utcoffset() is None
            or self.issued_at.microsecond != 0
            or self.expires_at.microsecond != 0
            or self.issued_at >= self.expires_at
            or not isinstance(self._seal, str)
            or not hmac.compare_digest(
                self._seal,
                _factory_seal(
                    "cost-authority-v2",
                    descriptor,
                ),
            )
        ):
            raise LiveControlError("cost authority is not factory-sealed")


class TrustedClock(Protocol):
    """Injected clock selected by the trusted live-operation boundary."""

    def now(self) -> datetime: ...


@dataclass(frozen=True)
class AuthorityBoundary:
    """One signed authority set plus its trusted freshness clock."""

    auth_authority: AuthAuthority
    cost_authority: CostAuthority
    clock: TrustedClock

    def validate(
        self,
        execution_contract: ExecutionContract,
        *,
        controls: FrozenControls,
    ) -> None:
        auth = self.auth_authority
        cost = self.cost_authority
        if not isinstance(auth, AuthAuthority) or not isinstance(
            cost,
            CostAuthority,
        ):
            raise LiveControlError(
                "authority boundary differs from the execution contract"
            )
        auth.validate()
        cost.validate()
        if not isinstance(
            execution_contract,
            ExecutionContract,
        ) or not isinstance(controls, FrozenControls):
            raise LiveControlError(
                "authority boundary differs from the execution contract"
            )
        execution_contract.validate()
        try:
            control_total = _decimal_6(
                controls.max_total_usd,
                "frozen max_total_usd",
                positive=True,
            )
            control_unit = _decimal_6(
                controls.max_unit_usd,
                "frozen max_unit_usd",
                positive=True,
            )
        except (AttributeError, LiveControlError) as exc:
            raise LiveControlError(
                "authority boundary differs from frozen controls"
            ) from exc
        if (
            (
                execution_contract.run_seed,
                execution_contract.provider,
                execution_contract.model_id,
                execution_contract.cli_version,
                execution_contract.cli_sha256,
                execution_contract.credential_source,
                execution_contract.endpoint,
                execution_contract.account_scope,
                execution_contract.expected_units,
                execution_contract.cost_mechanism,
                execution_contract.max_total_usd,
                execution_contract.max_unit_usd,
                execution_contract.calibration_sha256,
                execution_contract.auth_issuer,
                execution_contract.auth_key_id,
                execution_contract.auth_expires_at,
                execution_contract.cost_issuer,
                execution_contract.cost_key_id,
                execution_contract.cost_expires_at,
                execution_contract.auth_snapshot_sha256,
                execution_contract.cost_snapshot_sha256,
            )
            != (
                auth.run_seed,
                auth.provider,
                auth.model_id,
                auth.cli_version,
                auth.cli_sha256,
                auth.credential_source,
                auth.endpoint,
                auth.account_scope,
                cost.expected_units,
                cost.mechanism,
                _decimal_6(
                    cost.max_total_usd,
                    "cost authority max_total_usd",
                    positive=True,
                ),
                _decimal_6(
                    cost.max_unit_usd,
                    "cost authority max_unit_usd",
                    positive=True,
                ),
                cost.calibration_sha256,
                auth.issuer,
                auth.key_id,
                _authority_expiry(auth.expires_at),
                cost.issuer,
                cost.key_id,
                _authority_expiry(cost.expires_at),
                auth.snapshot_sha256,
                cost.snapshot_sha256,
            )
            or (
                auth.run_seed,
                auth.provider,
                auth.model_id,
                auth.endpoint,
                auth.account_scope,
            )
            != (
                cost.run_seed,
                cost.provider,
                cost.model_id,
                cost.endpoint,
                cost.account_scope,
            )
            or (
                execution_contract.provider,
                execution_contract.model_id,
                execution_contract.cli_version,
                execution_contract.max_total_usd,
                execution_contract.max_unit_usd,
                execution_contract.calibration_sha256,
            )
            != (
                controls.provider,
                controls.model_id,
                controls.cli_version,
                control_total,
                control_unit,
                controls.calibration_sha256,
            )
        ):
            raise LiveControlError(
                "authority boundary differs from the execution contract"
            )
        now = self.clock.now()
        if (
            not isinstance(now, datetime)
            or now.tzinfo is None
            or now.utcoffset() is None
        ):
            raise LiveControlError("trusted authority clock is invalid")
        now = now.astimezone(UTC)
        if now < auth.issued_at or now < cost.issued_at:
            raise LiveControlError("live authority is not yet valid")
        if now >= auth.expires_at or now >= cost.expires_at:
            raise LiveControlError("live authority has expired")


@dataclass(frozen=True)
class McpServerSpec:
    """Credential-free stdio server material for one generated MCP config."""

    name: str
    command: str
    args: tuple[str, ...]
    tools: tuple[str, ...]


@dataclass(frozen=True)
class ClaudeInvocation:
    """Pure data describing a future Claude invocation; never executable here."""

    arm: str
    argv: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    mcp_server_names: tuple[str, ...]
    mcp_config_json: str
    mcp_config_sha256: str
    common_controls_sha256: str
    environment_sha256: str
    executable_sha256: str
    descriptor_sha256: str
    max_discovery_tool_calls: int
    evidence_token_budget: int
    context_token_budget: int
    wall_timeout_seconds: int
    max_budget_usd_role: str
    adapter_enforcement: AdapterEnforcement
    _seal: str = field(repr=False, compare=False)

    def _descriptor(self) -> dict:
        return {
            "schema_version": 1,
            "arm": self.arm,
            "argv": list(self.argv),
            "allowed_tools": list(self.allowed_tools),
            "mcp_server_names": list(self.mcp_server_names),
            "mcp_config_sha256": self.mcp_config_sha256,
            "common_controls_sha256": self.common_controls_sha256,
            "environment_sha256": self.environment_sha256,
            "executable_sha256": self.executable_sha256,
            "adapter_enforcement": self.adapter_enforcement.descriptor(),
        }

    def validate(self) -> None:
        try:
            mcp_config = json.loads(
                self.mcp_config_json,
                object_pairs_hook=_strict_object,
            )
        except (TypeError, json.JSONDecodeError) as exc:
            raise LiveControlError("invocation MCP configuration is malformed") from exc
        if (
            self.arm not in ARM_CONTRACTS
            or not self.argv
            or self.argv[0] != REVIEWED_CLAUDE_EXECUTABLE
            or self.allowed_tools != ARM_CONTRACTS[self.arm].allowed_tools
            or self.mcp_server_names != ARM_MCP_SERVERS[self.arm]
            or canonical_json(mcp_config).decode("utf-8")
            != self.mcp_config_json
            or hashlib.sha256(self.mcp_config_json.encode()).hexdigest()
            != self.mcp_config_sha256
            or any(
                SHA256.fullmatch(value) is None
                for value in (
                    self.mcp_config_sha256,
                    self.common_controls_sha256,
                    self.environment_sha256,
                    self.executable_sha256,
                    self.descriptor_sha256,
                )
            )
            or self.max_discovery_tool_calls
            != self.adapter_enforcement.max_tool_calls
            or self.evidence_token_budget
            != self.adapter_enforcement.max_evidence_tokens
            or self.context_token_budget
            != self.adapter_enforcement.max_context_tokens
            or self.wall_timeout_seconds
            != self.adapter_enforcement.max_wall_seconds
            or self.max_budget_usd_role != "defense_in_depth_only"
            or hashlib.sha256(canonical_json(self._descriptor())).hexdigest()
            != self.descriptor_sha256
            or not hmac.compare_digest(
                self._seal,
                _factory_seal("claude-invocation-v1", self._descriptor()),
            )
        ):
            raise LiveControlError("invocation descriptor is invalid")


@dataclass(frozen=True)
class ChildEnvironments:
    """Secret-bearing child environments with a redacted representation."""

    fetch: tuple[tuple[str, str], ...] = field(repr=False)
    model: tuple[tuple[str, str], ...] = field(repr=False)
    mcp: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = field(
        repr=False
    )
    arm: str
    auth_snapshot_sha256: str
    credential_source: str
    trusted_root: str
    trusted_root_device: int
    trusted_root_inode: int
    isolated_root: str
    directory_identities: tuple[tuple[str, int, int], ...]
    descriptor_sha256: str
    _seal: str = field(repr=False, compare=False)

    def _descriptor(self) -> dict:
        return {
            "schema_version": 2,
            "arm": self.arm,
            "auth_snapshot_sha256": self.auth_snapshot_sha256,
            "credential_source": self.credential_source,
            "trusted_root": self.trusted_root,
            "trusted_root_device": self.trusted_root_device,
            "trusted_root_inode": self.trusted_root_inode,
            "isolated_root": self.isolated_root,
            "directory_identities": [
                {
                    "relative_path": relative_path,
                    "device": device,
                    "inode": inode,
                }
                for relative_path, device, inode in self.directory_identities
            ],
            "fetch": [list(item) for item in self.fetch],
            "model": [list(item) for item in self.model],
            "mcp": [
                {
                    "name": name,
                    "environment": [list(item) for item in values],
                }
                for name, values in self.mcp
            ],
        }

    def validate(self) -> None:
        base_names = frozenset(
            {
                *SAFE_AMBIENT_ENVIRONMENT,
                "HOME",
                "TMPDIR",
                "XDG_CACHE_HOME",
                "XDG_CONFIG_HOME",
                "PYTHONNOUSERSITE",
                "CLAUDE_CONFIG_DIR",
            }
        )
        model_policy = MODEL_CREDENTIALS.get(self.credential_source)
        mcp = dict(self.mcp)
        if (
            self.arm not in ARM_MCP_SERVERS
            or SHA256.fullmatch(self.auth_snapshot_sha256) is None
            or model_policy is None
            or not isinstance(self.trusted_root, str)
            or type(self.trusted_root_device) is not int
            or self.trusted_root_device < 0
            or type(self.trusted_root_inode) is not int
            or self.trusted_root_inode <= 0
            or not _valid_child_directory_identities(
                self.directory_identities
            )
            or tuple(sorted(self.fetch)) != self.fetch
            or tuple(sorted(self.model)) != self.model
            or tuple(mcp) != ARM_MCP_SERVERS[self.arm]
            or any(tuple(sorted(values)) != values for values in mcp.values())
            or not set(dict(self.fetch)) <= base_names | FETCH_CREDENTIALS
            or not set(dict(self.model)) <= base_names | model_policy[0]
            or any(not set(dict(values)) <= base_names for values in mcp.values())
            or hashlib.sha256(canonical_json(self._descriptor())).hexdigest()
            != self.descriptor_sha256
            or not hmac.compare_digest(
                self._seal,
                _factory_seal("child-environments-v2", self._descriptor()),
            )
        ):
            raise LiveControlError("sealed child environment is invalid")
        trusted_root = Path(self.trusted_root)
        isolated_root = Path(self.isolated_root)
        if not trusted_root.is_absolute() or not isolated_root.is_absolute():
            raise LiveControlError("sealed child environment root is invalid")
        try:
            isolated_root.relative_to(trusted_root)
        except ValueError as exc:
            raise LiveControlError(
                "sealed child environment root escapes trust"
            ) from exc
        for environment in (
            self.fetch,
            self.model,
            *(values for values in mcp.values()),
        ):
            values = dict(environment)
            for name in ("HOME", "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME"):
                try:
                    Path(values[name]).relative_to(isolated_root)
                except (KeyError, ValueError) as exc:
                    raise LiveControlError(
                        "sealed child environment path escapes isolation"
                    ) from exc
        expected_relatives = _child_directory_relatives(
            trusted_root=trusted_root,
            isolated_root=isolated_root,
            environments=(
                self.fetch,
                self.model,
                *(values for values in mcp.values()),
            ),
        )
        if (
            tuple(item[0] for item in self.directory_identities)
            != expected_relatives
        ):
            raise LiveControlError(
                "sealed child environment directory set is invalid"
            )

    def revalidate_isolation(self) -> None:
        """Reopen every sealed child directory without following links."""
        self.validate()
        _revalidate_child_directories(
            trusted_root=Path(self.trusted_root),
            trusted_root_identity=(
                self.trusted_root_device,
                self.trusted_root_inode,
            ),
            directory_identities=self.directory_identities,
        )

    def model_launch_environment(self) -> ModelLaunchEnvironment:
        self.validate()
        descriptor = {
            "schema_version": 1,
            "model": [list(item) for item in self.model],
            "mcp": [
                {
                    "name": name,
                    "environment": [list(item) for item in values],
                }
                for name, values in self.mcp
            ],
            "arm": self.arm,
            "credential_source": self.credential_source,
            "isolated_root": self.isolated_root,
            "source_environment_sha256": self.descriptor_sha256,
        }
        launch = ModelLaunchEnvironment(
            model=self.model,
            mcp=self.mcp,
            arm=self.arm,
            credential_source=self.credential_source,
            isolated_root=self.isolated_root,
            source_environment_sha256=self.descriptor_sha256,
            _seal=_factory_seal(
                "model-launch-environment-v1",
                descriptor,
            ),
        )
        launch.validate()
        return launch


@dataclass(frozen=True)
class ModelLaunchEnvironment:
    """Least-privilege model/MCP launch view; fetch credentials are absent."""

    model: tuple[tuple[str, str], ...] = field(repr=False)
    mcp: tuple[tuple[str, tuple[tuple[str, str], ...]], ...] = field(
        repr=False
    )
    arm: str
    credential_source: str
    isolated_root: str
    source_environment_sha256: str
    _seal: str = field(repr=False, compare=False)

    def _descriptor(self) -> dict:
        return {
            "schema_version": 1,
            "model": [list(item) for item in self.model],
            "mcp": [
                {
                    "name": name,
                    "environment": [list(item) for item in values],
                }
                for name, values in self.mcp
            ],
            "arm": self.arm,
            "credential_source": self.credential_source,
            "isolated_root": self.isolated_root,
            "source_environment_sha256": self.source_environment_sha256,
        }

    def validate(self) -> None:
        base_names = frozenset(
            {
                *SAFE_AMBIENT_ENVIRONMENT,
                "HOME",
                "TMPDIR",
                "XDG_CACHE_HOME",
                "XDG_CONFIG_HOME",
                "PYTHONNOUSERSITE",
                "CLAUDE_CONFIG_DIR",
            }
        )
        model_policy = MODEL_CREDENTIALS.get(self.credential_source)
        mcp = dict(self.mcp)
        if (
            self.arm not in ARM_MCP_SERVERS
            or model_policy is None
            or SHA256.fullmatch(self.source_environment_sha256) is None
            or tuple(sorted(self.model)) != self.model
            or tuple(mcp) != ARM_MCP_SERVERS[self.arm]
            or any(tuple(sorted(values)) != values for values in mcp.values())
            or not set(dict(self.model)) <= base_names | model_policy[0]
            or any(not set(dict(values)) <= base_names for values in mcp.values())
            or not hmac.compare_digest(
                self._seal,
                _factory_seal(
                    "model-launch-environment-v1",
                    self._descriptor(),
                ),
            )
        ):
            raise LiveControlError("model launch environment is invalid")
        isolated_root = Path(self.isolated_root)
        if not isolated_root.is_absolute():
            raise LiveControlError("model launch environment root is invalid")
        for environment in (
            self.model,
            *(values for values in mcp.values()),
        ):
            values = dict(environment)
            for name in ("HOME", "TMPDIR", "XDG_CACHE_HOME", "XDG_CONFIG_HOME"):
                try:
                    Path(values[name]).relative_to(isolated_root)
                except (KeyError, ValueError) as exc:
                    raise LiveControlError(
                        "model launch environment path escapes isolation"
                    ) from exc


@dataclass(frozen=True)
class AdapterEnforcement:
    max_tool_calls: int
    max_evidence_tokens: int
    max_context_tokens: int
    max_wall_seconds: int
    tool_policy: str = "authorize_before_tool_call_v1"
    token_policy: str = "reject_before_token_accept_v1"
    wall_policy: str = "cancel_at_monotonic_deadline_v1"

    def descriptor(self) -> dict:
        return {
            "schema_version": 1,
            "max_tool_calls": self.max_tool_calls,
            "max_evidence_tokens": self.max_evidence_tokens,
            "max_context_tokens": self.max_context_tokens,
            "max_wall_seconds": self.max_wall_seconds,
            "tool_policy": self.tool_policy,
            "token_policy": self.token_policy,
            "wall_policy": self.wall_policy,
        }


class OnlineLimitGuard:
    """Stateful adapter-side guard checked before accepting online work."""

    def __init__(self, contract: AdapterEnforcement):
        if not isinstance(contract, AdapterEnforcement):
            raise AdapterLimitError("adapter enforcement contract is required")
        self.contract = contract
        self.tool_calls = 0
        self.evidence_tokens = 0
        self.context_tokens = 0
        self._started_at = time.monotonic()
        self._deadline = self._started_at + contract.max_wall_seconds
        self._completed = False
        self._lock = threading.Lock()

    @staticmethod
    def _positive_increment(value: int, label: str) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise AdapterLimitError(f"{label} increment is invalid")
        return value

    def authorize_tool_call(self) -> None:
        with self._lock:
            self._require_active()
            if self.tool_calls >= self.contract.max_tool_calls:
                raise AdapterLimitError("online tool-call cap reached")
            self.tool_calls += 1

    def accept_evidence_tokens(self, count: int) -> None:
        count = self._positive_increment(count, "evidence token")
        with self._lock:
            self._require_active()
            if self.evidence_tokens + count > self.contract.max_evidence_tokens:
                raise AdapterLimitError("online evidence-token cap reached")
            self.evidence_tokens += count

    def accept_context_tokens(self, count: int) -> None:
        count = self._positive_increment(count, "context token")
        with self._lock:
            self._require_active()
            if self.context_tokens + count > self.contract.max_context_tokens:
                raise AdapterLimitError("online context-token cap reached")
            self.context_tokens += count

    def check_wall_time(self, elapsed_seconds: int) -> None:
        elapsed_seconds = self._positive_increment(
            elapsed_seconds,
            "wall-time",
        )
        with self._lock:
            self._require_active()
            if elapsed_seconds > self.contract.max_wall_seconds:
                raise AdapterLimitError("online wall-time cap reached")

    def _require_active(self) -> None:
        if self._completed:
            raise AdapterLimitError("adapter enforcement is already complete")
        if time.monotonic() > self._deadline:
            raise AdapterLimitError("online wall-time cap reached")

    def complete(self) -> None:
        with self._lock:
            self._require_active()
            self._completed = True

    def verify_complete(self) -> None:
        with self._lock:
            if not self._completed:
                raise LiveControlError(
                    "adapter enforcement did not complete"
                )


def _authority_expiry(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise LiveControlError("authority expiry must be timezone-aware")
    return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


EXECUTION_CONTRACT_TEMPLATE_IDENTITY = "execution_contract_template_v1"
EXECUTION_CONTRACT_IDENTITY = "execution_contract_v2"
EXECUTION_CONTRACT_RUN_FINGERPRINT = "execution_contract_run_fingerprint_v1"


def execution_contract_run_fingerprint_sha256(
    template: Mapping[str, object],
) -> str:
    """Hash every run-scoped live input while excluding unit-specific inputs."""
    payload = {
        "run_seed": template.get("run_seed"),
        "provider": template.get("provider"),
        "model_id": template.get("model_id"),
        "cli_version": template.get("cli_version"),
        "cli_sha256": template.get("cli_sha256"),
        "credential_source": template.get("credential_source"),
        "endpoint": template.get("endpoint"),
        "account_scope": template.get("account_scope"),
        "expected_units": template.get("expected_units"),
        "cost_mechanism": template.get("cost_mechanism"),
        "auth_issuer": template.get("auth_issuer"),
        "auth_key_id": template.get("auth_key_id"),
        "auth_expires_at": template.get("auth_expires_at"),
        "cost_issuer": template.get("cost_issuer"),
        "cost_key_id": template.get("cost_key_id"),
        "cost_expires_at": template.get("cost_expires_at"),
        "auth_snapshot_sha256": template.get("auth_snapshot_sha256"),
        "cost_snapshot_sha256": template.get("cost_snapshot_sha256"),
        "max_total_usd": template.get("max_total_usd"),
        "max_unit_usd": template.get("max_unit_usd"),
        "calibration_sha256": template.get("calibration_sha256"),
        "controls_sha256": template.get("controls_sha256"),
        "retry_policy": template.get("retry_policy"),
        "max_attempts": template.get("max_attempts"),
        "adapter_enforcement_sha256": template.get(
            "adapter_enforcement_sha256"
        ),
    }
    return hashlib.sha256(
        canonical_json(
            {
                "schema_version": 1,
                "identity_kind": EXECUTION_CONTRACT_RUN_FINGERPRINT,
                "run": payload,
            }
        )
    ).hexdigest()


def execution_contract_template_sha256(template: Mapping[str, object]) -> str:
    """Hash the pre-run contract fields without duplicating full controls."""
    payload = {
        key: value
        for key, value in template.items()
        if key not in {"controls", "template_sha256"}
    }
    return hashlib.sha256(
        canonical_json(
            {
                "schema_version": 1,
                "identity_kind": EXECUTION_CONTRACT_TEMPLATE_IDENTITY,
                "template": payload,
            }
        )
    ).hexdigest()


def execution_contract_identity_sha256(
    *,
    run_id: str,
    run_seed: str,
    unit_key: str,
    template_sha256: str,
) -> str:
    """Derive the final contract identity from the precommitted template."""
    return hashlib.sha256(
        canonical_json(
            {
                "schema_version": 2,
                "identity_kind": EXECUTION_CONTRACT_IDENTITY,
                "run_id": run_id,
                "run_seed": run_seed,
                "unit_key": unit_key,
                "template_sha256": template_sha256,
            }
        )
    ).hexdigest()


@dataclass(frozen=True)
class ExecutionContractTemplate:
    """Pre-authorization identity for one unit before final run binding."""

    run_seed: str
    unit_key: str
    arm: str
    provider: str
    model_id: str
    cli_version: str
    cli_sha256: str
    credential_source: str
    endpoint: str
    account_scope: str
    expected_units: int
    cost_mechanism: str
    auth_issuer: str
    auth_key_id: str
    auth_expires_at: str
    cost_issuer: str
    cost_key_id: str
    cost_expires_at: str
    auth_snapshot_sha256: str
    cost_snapshot_sha256: str
    controls_descriptor_json: str
    controls_sha256: str
    environment_sha256: str
    invocation_descriptor_sha256: str
    max_total_usd: str
    max_unit_usd: str
    calibration_sha256: str
    retry_policy: str
    max_attempts: int
    adapter_enforcement_sha256: str
    run_fingerprint_sha256: str
    template_sha256: str

    @classmethod
    def create(
        cls,
        *,
        run_seed: str,
        unit_key: str,
        auth_authority: AuthAuthority,
        cost_authority: CostAuthority,
        controls: FrozenControls,
        child_environments: ChildEnvironments,
        invocation: ClaudeInvocation,
        root: Path,
    ) -> ExecutionContractTemplate:
        if not isinstance(auth_authority, AuthAuthority) or not isinstance(
            cost_authority,
            CostAuthority,
        ):
            raise LiveControlError(
                "execution contract authority inputs do not bind"
            )
        auth_authority.validate()
        cost_authority.validate()
        child_environments.validate()
        invocation.validate()
        controls_descriptor = controls.descriptor(Path(root))
        controls_json = canonical_json(controls_descriptor).decode("utf-8")
        controls_sha256 = hashlib.sha256(controls_json.encode()).hexdigest()
        enforcement_sha256 = hashlib.sha256(
            canonical_json(invocation.adapter_enforcement.descriptor())
        ).hexdigest()
        unit_parts = unit_key.rsplit("|", 1)
        if (
            not isinstance(child_environments, ChildEnvironments)
            or not isinstance(invocation, ClaudeInvocation)
            or len(unit_parts) != 2
            or unit_parts[1] != invocation.arm
            or child_environments.arm != invocation.arm
            or child_environments.auth_snapshot_sha256
            != auth_authority.snapshot_sha256
            or invocation.environment_sha256
            != child_environments.descriptor_sha256
            or invocation.executable_sha256 != auth_authority.cli_sha256
            or controls.cli_version != auth_authority.cli_version
            or child_environments.credential_source
            != auth_authority.credential_source
            or SHA256.fullmatch(run_seed) is None
            or run_seed != auth_authority.run_seed
            or run_seed != cost_authority.run_seed
            or auth_authority.provider != cost_authority.provider
            or auth_authority.provider != controls.provider
            or auth_authority.model_id != cost_authority.model_id
            or auth_authority.model_id != controls.model_id
            or auth_authority.endpoint != cost_authority.endpoint
            or auth_authority.account_scope != cost_authority.account_scope
            or controls.max_total_usd != cost_authority.max_total_usd
            or controls.max_unit_usd != cost_authority.max_unit_usd
            or controls.calibration_sha256
            != cost_authority.calibration_sha256
        ):
            raise LiveControlError(
                "execution contract authority inputs do not bind"
            )
        payload = {
            "schema_version": 1,
            "run_seed": run_seed,
            "unit_key": unit_key,
            "arm": invocation.arm,
            "provider": controls.provider,
            "model_id": controls.model_id,
            "cli_version": auth_authority.cli_version,
            "cli_sha256": auth_authority.cli_sha256,
            "credential_source": auth_authority.credential_source,
            "endpoint": auth_authority.endpoint,
            "account_scope": auth_authority.account_scope,
            "expected_units": cost_authority.expected_units,
            "cost_mechanism": cost_authority.mechanism,
            "auth_issuer": auth_authority.issuer,
            "auth_key_id": auth_authority.key_id,
            "auth_expires_at": _authority_expiry(
                auth_authority.expires_at
            ),
            "cost_issuer": cost_authority.issuer,
            "cost_key_id": cost_authority.key_id,
            "cost_expires_at": _authority_expiry(
                cost_authority.expires_at
            ),
            "auth_snapshot_sha256": auth_authority.snapshot_sha256,
            "cost_snapshot_sha256": cost_authority.snapshot_sha256,
            "controls_sha256": controls_sha256,
            "environment_sha256": child_environments.descriptor_sha256,
            "invocation_descriptor_sha256": invocation.descriptor_sha256,
            "max_total_usd": format(controls.max_total_usd, "f"),
            "max_unit_usd": format(controls.max_unit_usd, "f"),
            "calibration_sha256": controls.calibration_sha256,
            "retry_policy": controls.retry_policy,
            "max_attempts": controls.max_attempts,
            "adapter_enforcement_sha256": enforcement_sha256,
        }
        run_fingerprint_sha256 = execution_contract_run_fingerprint_sha256(
            payload
        )
        payload["run_fingerprint_sha256"] = run_fingerprint_sha256
        template = cls(
            run_seed=run_seed,
            unit_key=unit_key,
            arm=invocation.arm,
            provider=controls.provider,
            model_id=controls.model_id,
            cli_version=auth_authority.cli_version,
            cli_sha256=auth_authority.cli_sha256,
            credential_source=auth_authority.credential_source,
            endpoint=auth_authority.endpoint,
            account_scope=auth_authority.account_scope,
            expected_units=cost_authority.expected_units,
            cost_mechanism=cost_authority.mechanism,
            auth_issuer=auth_authority.issuer,
            auth_key_id=auth_authority.key_id,
            auth_expires_at=_authority_expiry(
                auth_authority.expires_at
            ),
            cost_issuer=cost_authority.issuer,
            cost_key_id=cost_authority.key_id,
            cost_expires_at=_authority_expiry(
                cost_authority.expires_at
            ),
            auth_snapshot_sha256=auth_authority.snapshot_sha256,
            cost_snapshot_sha256=cost_authority.snapshot_sha256,
            controls_descriptor_json=controls_json,
            controls_sha256=controls_sha256,
            environment_sha256=child_environments.descriptor_sha256,
            invocation_descriptor_sha256=invocation.descriptor_sha256,
            max_total_usd=format(controls.max_total_usd, "f"),
            max_unit_usd=format(controls.max_unit_usd, "f"),
            calibration_sha256=controls.calibration_sha256,
            retry_policy=controls.retry_policy,
            max_attempts=controls.max_attempts,
            adapter_enforcement_sha256=enforcement_sha256,
            run_fingerprint_sha256=run_fingerprint_sha256,
            template_sha256=execution_contract_template_sha256(payload),
        )
        template.validate()
        return template

    def descriptor(self) -> dict:
        return {
            "schema_version": 1,
            "run_seed": self.run_seed,
            "unit_key": self.unit_key,
            "arm": self.arm,
            "provider": self.provider,
            "model_id": self.model_id,
            "cli_version": self.cli_version,
            "cli_sha256": self.cli_sha256,
            "credential_source": self.credential_source,
            "endpoint": self.endpoint,
            "account_scope": self.account_scope,
            "expected_units": self.expected_units,
            "cost_mechanism": self.cost_mechanism,
            "auth_issuer": self.auth_issuer,
            "auth_key_id": self.auth_key_id,
            "auth_expires_at": self.auth_expires_at,
            "cost_issuer": self.cost_issuer,
            "cost_key_id": self.cost_key_id,
            "cost_expires_at": self.cost_expires_at,
            "auth_snapshot_sha256": self.auth_snapshot_sha256,
            "cost_snapshot_sha256": self.cost_snapshot_sha256,
            "controls": json.loads(self.controls_descriptor_json),
            "controls_sha256": self.controls_sha256,
            "environment_sha256": self.environment_sha256,
            "invocation_descriptor_sha256": (
                self.invocation_descriptor_sha256
            ),
            "max_total_usd": self.max_total_usd,
            "max_unit_usd": self.max_unit_usd,
            "calibration_sha256": self.calibration_sha256,
            "retry_policy": self.retry_policy,
            "max_attempts": self.max_attempts,
            "adapter_enforcement_sha256": (
                self.adapter_enforcement_sha256
            ),
            "run_fingerprint_sha256": self.run_fingerprint_sha256,
            "template_sha256": self.template_sha256,
        }

    def validate(self) -> None:
        validate_execution_contract_template_descriptor(self.descriptor())

    def verify_runtime(
        self,
        *,
        controls: FrozenControls,
        child_environments: ChildEnvironments,
        invocation: ClaudeInvocation,
        root: Path,
    ) -> None:
        self.validate()
        child_environments.validate()
        invocation.validate()
        try:
            controls_json = canonical_json(
                controls.descriptor(Path(root))
            ).decode("utf-8")
        except ContractError as exc:
            raise LiveControlError("runtime controls are not frozen") from exc
        enforcement_sha256 = hashlib.sha256(
            canonical_json(invocation.adapter_enforcement.descriptor())
        ).hexdigest()
        if (
            controls_json != self.controls_descriptor_json
            or hashlib.sha256(controls_json.encode()).hexdigest()
            != self.controls_sha256
            or child_environments.descriptor_sha256
            != self.environment_sha256
            or invocation.descriptor_sha256
            != self.invocation_descriptor_sha256
            or invocation.environment_sha256 != self.environment_sha256
            or invocation.arm != self.arm
            or child_environments.arm != self.arm
            or controls.cli_version != self.cli_version
            or invocation.executable_sha256 != self.cli_sha256
            or child_environments.credential_source
            != self.credential_source
            or child_environments.auth_snapshot_sha256
            != self.auth_snapshot_sha256
            or enforcement_sha256 != self.adapter_enforcement_sha256
            or format(controls.max_total_usd, "f") != self.max_total_usd
            or format(controls.max_unit_usd, "f") != self.max_unit_usd
            or controls.calibration_sha256 != self.calibration_sha256
            or controls.retry_policy != self.retry_policy
            or controls.max_attempts != self.max_attempts
        ):
            raise LiveControlError("runtime differs from the execution contract")


@dataclass(frozen=True)
class ExecutionContract:
    """Final run binding for one precommitted live-unit template."""

    run_id: str
    run_seed: str
    unit_key: str
    arm: str
    provider: str
    model_id: str
    cli_version: str
    cli_sha256: str
    credential_source: str
    endpoint: str
    account_scope: str
    expected_units: int
    cost_mechanism: str
    auth_issuer: str
    auth_key_id: str
    auth_expires_at: str
    cost_issuer: str
    cost_key_id: str
    cost_expires_at: str
    auth_snapshot_sha256: str
    cost_snapshot_sha256: str
    controls_descriptor_json: str
    controls_sha256: str
    environment_sha256: str
    invocation_descriptor_sha256: str
    max_total_usd: str
    max_unit_usd: str
    calibration_sha256: str
    retry_policy: str
    max_attempts: int
    adapter_enforcement_sha256: str
    run_fingerprint_sha256: str
    template_sha256: str
    descriptor_sha256: str

    @classmethod
    def create(
        cls,
        *,
        run_id: str,
        template: ExecutionContractTemplate,
    ) -> ExecutionContract:
        if (
            SHA256.fullmatch(run_id) is None
            or not isinstance(template, ExecutionContractTemplate)
        ):
            raise LiveControlError("final execution contract identity is malformed")
        template.validate()
        return cls(
            run_id=run_id,
            run_seed=template.run_seed,
            unit_key=template.unit_key,
            arm=template.arm,
            provider=template.provider,
            model_id=template.model_id,
            cli_version=template.cli_version,
            cli_sha256=template.cli_sha256,
            credential_source=template.credential_source,
            endpoint=template.endpoint,
            account_scope=template.account_scope,
            expected_units=template.expected_units,
            cost_mechanism=template.cost_mechanism,
            auth_issuer=template.auth_issuer,
            auth_key_id=template.auth_key_id,
            auth_expires_at=template.auth_expires_at,
            cost_issuer=template.cost_issuer,
            cost_key_id=template.cost_key_id,
            cost_expires_at=template.cost_expires_at,
            auth_snapshot_sha256=template.auth_snapshot_sha256,
            cost_snapshot_sha256=template.cost_snapshot_sha256,
            controls_descriptor_json=template.controls_descriptor_json,
            controls_sha256=template.controls_sha256,
            environment_sha256=template.environment_sha256,
            invocation_descriptor_sha256=(
                template.invocation_descriptor_sha256
            ),
            max_total_usd=template.max_total_usd,
            max_unit_usd=template.max_unit_usd,
            calibration_sha256=template.calibration_sha256,
            retry_policy=template.retry_policy,
            max_attempts=template.max_attempts,
            adapter_enforcement_sha256=(
                template.adapter_enforcement_sha256
            ),
            run_fingerprint_sha256=template.run_fingerprint_sha256,
            template_sha256=template.template_sha256,
            descriptor_sha256=execution_contract_identity_sha256(
                run_id=run_id,
                run_seed=template.run_seed,
                unit_key=template.unit_key,
                template_sha256=template.template_sha256,
            ),
        )

    def template_descriptor(self) -> dict:
        return {
            "schema_version": 1,
            "run_seed": self.run_seed,
            "unit_key": self.unit_key,
            "arm": self.arm,
            "provider": self.provider,
            "model_id": self.model_id,
            "cli_version": self.cli_version,
            "cli_sha256": self.cli_sha256,
            "credential_source": self.credential_source,
            "endpoint": self.endpoint,
            "account_scope": self.account_scope,
            "expected_units": self.expected_units,
            "cost_mechanism": self.cost_mechanism,
            "auth_issuer": self.auth_issuer,
            "auth_key_id": self.auth_key_id,
            "auth_expires_at": self.auth_expires_at,
            "cost_issuer": self.cost_issuer,
            "cost_key_id": self.cost_key_id,
            "cost_expires_at": self.cost_expires_at,
            "auth_snapshot_sha256": self.auth_snapshot_sha256,
            "cost_snapshot_sha256": self.cost_snapshot_sha256,
            "controls": json.loads(self.controls_descriptor_json),
            "controls_sha256": self.controls_sha256,
            "environment_sha256": self.environment_sha256,
            "invocation_descriptor_sha256": (
                self.invocation_descriptor_sha256
            ),
            "max_total_usd": self.max_total_usd,
            "max_unit_usd": self.max_unit_usd,
            "calibration_sha256": self.calibration_sha256,
            "retry_policy": self.retry_policy,
            "max_attempts": self.max_attempts,
            "adapter_enforcement_sha256": (
                self.adapter_enforcement_sha256
            ),
            "run_fingerprint_sha256": self.run_fingerprint_sha256,
            "template_sha256": self.template_sha256,
        }

    def descriptor(self) -> dict:
        template = self.template_descriptor()
        return {
            "schema_version": 2,
            "run_id": self.run_id,
            **{
                key: value
                for key, value in template.items()
                if key != "schema_version"
            },
            "descriptor_sha256": self.descriptor_sha256,
        }

    def verify_runtime(
        self,
        *,
        controls: FrozenControls,
        child_environments: ChildEnvironments,
        invocation: ClaudeInvocation,
        root: Path,
    ) -> None:
        template = ExecutionContractTemplate(
            run_seed=self.run_seed,
            unit_key=self.unit_key,
            arm=self.arm,
            provider=self.provider,
            model_id=self.model_id,
            cli_version=self.cli_version,
            cli_sha256=self.cli_sha256,
            credential_source=self.credential_source,
            endpoint=self.endpoint,
            account_scope=self.account_scope,
            expected_units=self.expected_units,
            cost_mechanism=self.cost_mechanism,
            auth_issuer=self.auth_issuer,
            auth_key_id=self.auth_key_id,
            auth_expires_at=self.auth_expires_at,
            cost_issuer=self.cost_issuer,
            cost_key_id=self.cost_key_id,
            cost_expires_at=self.cost_expires_at,
            auth_snapshot_sha256=self.auth_snapshot_sha256,
            cost_snapshot_sha256=self.cost_snapshot_sha256,
            controls_descriptor_json=self.controls_descriptor_json,
            controls_sha256=self.controls_sha256,
            environment_sha256=self.environment_sha256,
            invocation_descriptor_sha256=(
                self.invocation_descriptor_sha256
            ),
            max_total_usd=self.max_total_usd,
            max_unit_usd=self.max_unit_usd,
            calibration_sha256=self.calibration_sha256,
            retry_policy=self.retry_policy,
            max_attempts=self.max_attempts,
            adapter_enforcement_sha256=(
                self.adapter_enforcement_sha256
            ),
            run_fingerprint_sha256=self.run_fingerprint_sha256,
            template_sha256=self.template_sha256,
        )
        template.validate()
        template.verify_runtime(
            controls=controls,
            child_environments=child_environments,
            invocation=invocation,
            root=root,
        )

    def validate(self) -> None:
        _validate_execution_contract_descriptor(self.descriptor())


@dataclass(frozen=True)
class BudgetRequest:
    run_id: str
    unit_key: str
    attempt_number: int
    idempotency_key: str
    max_unit_usd: Decimal
    max_total_usd: Decimal


@dataclass(frozen=True)
class BudgetReservation:
    reservation_id: str
    idempotency_key: str
    authorized_usd: Decimal


@dataclass(frozen=True)
class DispatchReceipt:
    operation_id: str
    idempotency_key: str
    status: str
    cost_usd: Decimal
    response_sha256: str | None
    error_class: str | None


@dataclass(frozen=True)
class DispatchReconciliation:
    state: str
    receipt: DispatchReceipt | None


@dataclass(frozen=True)
class AttemptOutcome:
    attempt_number: int
    idempotency_key: str
    phase: str
    classification: str
    retryable: bool
    cost_usd: Decimal


class BudgetBroker(Protocol):
    def reserve(self, request: BudgetRequest) -> BudgetReservation: ...

    def reconcile(
        self,
        *,
        idempotency_key: str,
    ) -> DispatchReconciliation: ...


class LiveExecutor(Protocol):
    def dispatch(
        self,
        *,
        invocation: ClaudeInvocation,
        launch_environment: ModelLaunchEnvironment,
        enforcement: OnlineLimitGuard,
        reservation: BudgetReservation,
        idempotency_key: str,
    ) -> DispatchReceipt: ...


AUTH_CLAIM_FIELDS = frozenset(
    {
        "run_seed",
        "provider",
        "model_id",
        "cli_version",
        "cli_sha256",
        "execution_mode",
        "credential_source",
        "account_scope",
        "endpoint",
        "issued_at",
        "expires_at",
    }
)
COST_CLAIM_FIELDS = frozenset(
    {
        "run_seed",
        "provider",
        "model_id",
        "account_scope",
        "endpoint",
        "currency",
        "max_total_usd",
        "max_unit_usd",
        "expected_units",
        "calibration_sha256",
        "mechanism",
        "issued_at",
        "expires_at",
    }
)
BARE_CREDENTIAL_SOURCES = {
    "anthropic": frozenset({"anthropic_api_key", "api_key_helper"}),
    "aws-bedrock": frozenset({"aws_bedrock"}),
    "google-vertex": frozenset({"google_vertex"}),
}
AUTHORITY_ENVELOPE_FIELDS = frozenset(
    {"schema_version", "authority_kind", "claims", "signature"}
)
SIGNATURE_FIELDS = frozenset({"algorithm", "issuer", "key_id", "value"})
RFC3339_SECONDS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
COST_MECHANISMS = frozenset(
    {"provider_hard_limit", "transactional_budget_proxy"}
)
MCP_TOOLS = {
    "code-search": SEARCH_TOOLS,
    "code-graph": GRAPH_TOOLS,
}
REVIEWED_CLAUDE_EXECUTABLE = "/opt/anthropic/bin/claude"
REVIEWED_MCP_LAUNCHES = {
    "code-search": (
        "/opt/code-intel/code-search-mcp",
        ("--stdio",),
    ),
    "code-graph": (
        "/opt/code-intel/code-graph-mcp",
        ("--stdio",),
    ),
}
ARM_MCP_SERVERS = {
    "corpus": (),
    "native": (),
    "code-search": ("code-search",),
    "code-graph": ("code-graph",),
    "composed": ("code-search", "code-graph"),
}
CREDENTIAL_ARGUMENT = re.compile(
    r"(?:api[-_]?key|credential|password|secret|token)",
    re.IGNORECASE,
)
SAFE_AMBIENT_ENVIRONMENT = (
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "PATH",
    "SSL_CERT_DIR",
    "SSL_CERT_FILE",
    "TZ",
)
FETCH_CREDENTIALS = frozenset({"GH_TOKEN", "GITHUB_TOKEN"})
MODEL_CREDENTIALS = {
    "anthropic_api_key": (
        frozenset({"ANTHROPIC_API_KEY"}),
        frozenset({"ANTHROPIC_API_KEY"}),
    ),
    "api_key_helper": (frozenset(), frozenset()),
    "aws_bedrock": (
        frozenset(
            {
                "AWS_ACCESS_KEY_ID",
                "AWS_SECRET_ACCESS_KEY",
                "AWS_SESSION_TOKEN",
                "AWS_REGION",
                "AWS_DEFAULT_REGION",
            }
        ),
        frozenset({"AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY"}),
    ),
    "google_vertex": (
        frozenset(
            {
                "GOOGLE_APPLICATION_CREDENTIALS",
                "ANTHROPIC_VERTEX_PROJECT_ID",
                "CLOUD_ML_REGION",
            }
        ),
        frozenset(
            {
                "GOOGLE_APPLICATION_CREDENTIALS",
                "ANTHROPIC_VERTEX_PROJECT_ID",
                "CLOUD_ML_REGION",
            }
        ),
    ),
}


def _require_provider_credential_compatibility(
    *,
    provider: object,
    credential_source: object,
    context: str,
) -> None:
    compatible_sources = BARE_CREDENTIAL_SOURCES.get(provider, frozenset())
    if credential_source not in compatible_sources:
        raise LiveControlError(
            f"{context} provider and credential source are bare-incompatible"
        )


def _require_expected_cost_capacity(
    *,
    max_total_usd: object,
    max_unit_usd: object,
    expected_units: object,
    context: str,
) -> None:
    try:
        total_micros = usd_micros(
            max_total_usd,
            f"{context} max_total_usd",
            positive=True,
        )
        unit_micros = usd_micros(
            max_unit_usd,
            f"{context} max_unit_usd",
            positive=True,
        )
    except ContractError as exc:
        raise LiveControlError(f"{context} cost capacity is malformed") from exc
    if (
        isinstance(expected_units, bool)
        or not isinstance(expected_units, int)
        or expected_units < 1
    ):
        raise LiveControlError(f"{context} expected_units must be positive")
    if unit_micros * expected_units > total_micros:
        raise LiveControlError(f"{context} total cannot cover all units")


def _strict_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict = {}
    for key, value in pairs:
        if key in result:
            raise LiveControlError(f"authority has duplicate field {key!r}")
        result[key] = value
    return result


def _canonical_absolute_path(value: Path, *, label: str) -> Path:
    try:
        raw = os.fspath(value)
    except TypeError as exc:
        raise LiveControlError(f"{label} path is invalid") from exc
    if (
        not isinstance(raw, str)
        or not raw
        or "\x00" in raw
    ):
        raise LiveControlError(f"{label} path is invalid")
    path = Path(raw)
    if (
        not path.is_absolute()
        or raw != path.as_posix()
        or any(part in {"", ".", ".."} for part in path.parts[1:])
    ):
        raise LiveControlError(
            f"{label} path spelling is noncanonical"
        )
    return path


def _validate_exact_filesystem_spelling(
    path: Path,
    *,
    label: str,
) -> None:
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptors: list[int] = []
    try:
        current = os.open(path.anchor, directory_flags)
        descriptors.append(current)
        parts = path.parts[1:]
        for index, part in enumerate(parts):
            try:
                names = os.listdir(current)
                before = os.stat(
                    part,
                    dir_fd=current,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return
            if part not in names:
                raise LiveControlError(
                    f"{label} path spelling is noncanonical"
                )
            if stat.S_ISLNK(before.st_mode):
                raise LiveControlError(
                    f"{label} may not traverse a symlink"
                )
            if index == len(parts) - 1:
                return
            if not stat.S_ISDIR(before.st_mode):
                raise LiveControlError(
                    f"{label} parent is not a directory"
                )
            current = os.open(
                part,
                directory_flags,
                dir_fd=current,
            )
            descriptors.append(current)
            after = os.fstat(current)
            if (
                not stat.S_ISDIR(after.st_mode)
                or (before.st_dev, before.st_ino)
                != (after.st_dev, after.st_ino)
            ):
                raise LiveControlError(
                    f"{label} path changed while validating"
                )
    except LiveControlError:
        raise
    except OSError as exc:
        raise LiveControlError(
            f"cannot validate {label} path spelling"
        ) from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _trusted_path(
    path: Path,
    *,
    trusted_root: Path,
    label: str,
) -> tuple[Path, Path]:
    path = _canonical_absolute_path(path, label=label)
    root = _canonical_absolute_path(
        trusted_root,
        label=f"{label} trusted root",
    )
    if root.is_symlink():
        raise LiveControlError(f"{label} trusted root is a symlink")
    if not root.is_dir():
        raise LiveControlError(f"{label} trusted root is unsafe")
    _validate_exact_filesystem_spelling(
        root,
        label=f"{label} trusted root",
    )
    try:
        path.relative_to(root)
        resolved_root = root.resolve(strict=True)
    except (OSError, ValueError) as exc:
        raise LiveControlError(f"{label} escapes its trusted root") from exc
    if root != resolved_root:
        raise LiveControlError(
            f"{label} trusted root must be canonical without ancestor symlinks"
        )
    _validate_exact_filesystem_spelling(path, label=label)
    try:
        resolved_path = path.resolve(strict=False)
        resolved_path.relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise LiveControlError(f"{label} escapes its trusted root") from exc
    if path != resolved_path:
        raise LiveControlError(
            f"{label} path spelling is noncanonical"
        )
    return path, resolved_root


def _trusted_lock_identity(
    path: Path,
    *,
    trusted_root: Path,
    label: str,
) -> tuple[Path, tuple[int, int], str]:
    path, root = _trusted_path(
        path,
        trusted_root=trusted_root,
        label=label,
    )
    relative = path.relative_to(root)
    if not relative.parts:
        raise LiveControlError(f"{label} path is not a file")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptors: list[int] = []
    try:
        before = os.stat(root, follow_symlinks=False)
        current = os.open(root, directory_flags)
        descriptors.append(current)
        root_state = os.fstat(current)
        if (
            not stat.S_ISDIR(before.st_mode)
            or not stat.S_ISDIR(root_state.st_mode)
            or (before.st_dev, before.st_ino)
            != (root_state.st_dev, root_state.st_ino)
        ):
            raise LiveControlError(
                f"{label} trusted root identity changed"
            )
        for part in relative.parts[:-1]:
            current = os.open(
                part,
                directory_flags,
                dir_fd=current,
            )
            descriptors.append(current)
            state = os.fstat(current)
            if not stat.S_ISDIR(state.st_mode):
                raise LiveControlError(
                    f"{label} parent is not a directory"
                )
        parent_state = os.fstat(current)
        lock_name = hashlib.sha256(
            canonical_json(
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
                    "basename": relative.parts[-1],
                }
            )
        ).hexdigest()
        return (
            root,
            (root_state.st_dev, root_state.st_ino),
            lock_name,
        )
    except LiveControlError:
        raise
    except OSError as exc:
        raise LiveControlError(
            f"cannot securely identify {label} path"
        ) from exc
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)


def _unique_regular_file_state(
    descriptor: int,
    *,
    label: str,
) -> os.stat_result:
    try:
        state = os.fstat(descriptor)
    except OSError as exc:
        raise LiveControlError(f"cannot inspect {label}") from exc
    if not stat.S_ISREG(state.st_mode) or state.st_nlink != 1:
        raise LiveControlError(
            f"{label} must be a single-link regular file"
        )
    return state


def _verify_open_file_path(
    descriptor: int,
    *,
    parent_descriptor: int,
    name: str,
    label: str,
) -> os.stat_result:
    opened = _unique_regular_file_state(descriptor, label=label)
    try:
        current = os.stat(
            name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
    except OSError as exc:
        raise LiveControlError(f"{label} path changed") from exc
    if (
        not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or (current.st_dev, current.st_ino)
        != (opened.st_dev, opened.st_ino)
    ):
        raise LiveControlError(
            f"{label} must be a single-link regular file"
        )
    return opened


_CHILD_DIRECTORY_ENVIRONMENT_NAMES = frozenset(
    {
        "HOME",
        "TMPDIR",
        "XDG_CACHE_HOME",
        "XDG_CONFIG_HOME",
        "CLAUDE_CONFIG_DIR",
    }
)


def _valid_child_directory_identities(
    identities: object,
) -> bool:
    if not isinstance(identities, tuple):
        return False
    paths: list[str] = []
    for identity in identities:
        if (
            not isinstance(identity, tuple)
            or len(identity) != 3
            or not isinstance(identity[0], str)
            or not identity[0]
            or "\x00" in identity[0]
            or type(identity[1]) is not int
            or identity[1] < 0
            or type(identity[2]) is not int
            or identity[2] <= 0
        ):
            return False
        relative = Path(identity[0])
        if (
            relative.is_absolute()
            or relative.as_posix() != identity[0]
            or any(part in {"", ".", ".."} for part in relative.parts)
        ):
            return False
        paths.append(identity[0])
    return paths == sorted(paths) and len(paths) == len(set(paths))


def _child_directory_relatives(
    *,
    trusted_root: Path,
    isolated_root: Path,
    environments: tuple[tuple[tuple[str, str], ...], ...],
) -> tuple[str, ...]:
    targets = {isolated_root}
    for environment in environments:
        for name, value in environment:
            if name in _CHILD_DIRECTORY_ENVIRONMENT_NAMES:
                targets.add(Path(value))
    relatives: set[str] = set()
    for target in targets:
        if not target.is_absolute():
            raise LiveControlError(
                "sealed child environment directory is not absolute"
            )
        try:
            relative = target.relative_to(trusted_root)
        except ValueError as exc:
            raise LiveControlError(
                "sealed child environment directory escapes trust"
            ) from exc
        if any(part in {"", ".", ".."} for part in relative.parts):
            raise LiveControlError(
                "sealed child environment directory is not canonical"
            )
        for length in range(1, len(relative.parts) + 1):
            relatives.add(
                Path(*relative.parts[:length]).as_posix()
            )
    return tuple(sorted(relatives))


def _directory_open_flags() -> int:
    return (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )


def _secure_create_child_directories(
    *,
    trusted_root: Path,
    relative_paths: tuple[str, ...],
) -> tuple[tuple[int, int], tuple[tuple[str, int, int], ...]]:
    try:
        before = os.stat(trusted_root, follow_symlinks=False)
        root_descriptor = os.open(
            trusted_root,
            _directory_open_flags(),
        )
    except OSError as exc:
        raise LiveControlError(
            "cannot open trusted child isolation root"
        ) from exc
    try:
        root_state = os.fstat(root_descriptor)
        root_identity = (root_state.st_dev, root_state.st_ino)
        if (
            not stat.S_ISDIR(before.st_mode)
            or not stat.S_ISDIR(root_state.st_mode)
            or root_identity != (before.st_dev, before.st_ino)
        ):
            raise LiveControlError(
                "trusted child isolation root changed while opening"
            )
        identities: dict[str, tuple[int, int]] = {}
        ordered_paths = sorted(
            relative_paths,
            key=lambda value: (len(Path(value).parts), value),
        )
        for relative_path in ordered_paths:
            current_descriptor = os.dup(root_descriptor)
            traversed: list[str] = []
            try:
                for part in Path(relative_path).parts:
                    traversed.append(part)
                    try:
                        os.mkdir(
                            part,
                            mode=0o700,
                            dir_fd=current_descriptor,
                        )
                    except FileExistsError:
                        pass
                    next_descriptor = os.open(
                        part,
                        _directory_open_flags(),
                        dir_fd=current_descriptor,
                    )
                    os.close(current_descriptor)
                    current_descriptor = next_descriptor
                    state = os.fstat(current_descriptor)
                    if not stat.S_ISDIR(state.st_mode):
                        raise LiveControlError(
                            "sealed child isolation component is not a directory"
                        )
                    traversed_path = Path(*traversed).as_posix()
                    identity = (state.st_dev, state.st_ino)
                    prior_identity = identities.get(traversed_path)
                    if (
                        prior_identity is not None
                        and prior_identity != identity
                    ):
                        raise LiveControlError(
                            "child isolation changed while it was created"
                        )
                    identities[traversed_path] = identity
            except OSError as exc:
                raise LiveControlError(
                    "cannot securely create sealed child isolation"
                ) from exc
            finally:
                os.close(current_descriptor)
        if set(identities) != set(relative_paths):
            raise LiveControlError(
                "sealed child isolation directory set is incomplete"
            )
        return root_identity, tuple(
            (relative_path, *identities[relative_path])
            for relative_path in sorted(identities)
        )
    finally:
        os.close(root_descriptor)


def _revalidate_child_directories(
    *,
    trusted_root: Path,
    trusted_root_identity: tuple[int, int],
    directory_identities: tuple[tuple[str, int, int], ...],
) -> None:
    expected = {
        relative_path: (device, inode)
        for relative_path, device, inode in directory_identities
    }
    root_descriptor = -1
    try:
        root_descriptor = os.open(
            trusted_root,
            _directory_open_flags(),
        )
        root_state = os.fstat(root_descriptor)
        if (
            not stat.S_ISDIR(root_state.st_mode)
            or (root_state.st_dev, root_state.st_ino)
            != trusted_root_identity
        ):
            raise LiveControlError(
                "trusted child isolation root identity changed"
            )
        ordered_paths = sorted(
            expected,
            key=lambda value: (len(Path(value).parts), value),
        )
        for relative_path in ordered_paths:
            current_descriptor = os.dup(root_descriptor)
            traversed: list[str] = []
            try:
                for part in Path(relative_path).parts:
                    traversed.append(part)
                    next_descriptor = os.open(
                        part,
                        _directory_open_flags(),
                        dir_fd=current_descriptor,
                    )
                    os.close(current_descriptor)
                    current_descriptor = next_descriptor
                    state = os.fstat(current_descriptor)
                    traversed_path = Path(*traversed).as_posix()
                    if (
                        not stat.S_ISDIR(state.st_mode)
                        or expected.get(traversed_path)
                        != (state.st_dev, state.st_ino)
                    ):
                        raise LiveControlError(
                            "sealed child isolation identity changed"
                        )
            finally:
                os.close(current_descriptor)
    except LiveControlError:
        raise
    except OSError as exc:
        raise LiveControlError(
            "sealed child isolation changed or is unsafe"
        ) from exc
    finally:
        if root_descriptor >= 0:
            os.close(root_descriptor)


def _read_snapshot(
    path: Path,
    *,
    trusted_root: Path,
    label: str = "authority",
    require_single_link: bool = False,
) -> bytes:
    path, root = _trusted_path(
        path,
        trusted_root=trusted_root,
        label=label,
    )
    relative = path.relative_to(root)
    if not relative.parts:
        raise LiveControlError(f"{label} path is not a file: {path}")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    directories: list[int] = []
    descriptor = -1
    try:
        current = os.open(root, directory_flags)
        directories.append(current)
        for part in relative.parts[:-1]:
            current = os.open(part, directory_flags, dir_fd=current)
            directories.append(current)
        descriptor = os.open(
            relative.parts[-1],
            file_flags,
            dir_fd=current,
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise LiveControlError(f"{label} is not a regular file: {path}")
        if require_single_link and before.st_nlink != 1:
            raise LiveControlError(
                f"{label} must be a single-link regular file"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        current_state = os.stat(
            relative.parts[-1],
            dir_fd=current,
            follow_symlinks=False,
        )
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise LiveControlError(f"refusing symlink {label}: {path}") from exc
        raise LiveControlError(f"cannot read {label} {path}: {exc}") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        for directory in reversed(directories):
            os.close(directory)
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_nlink if require_single_link else None,
        before.st_size,
        before.st_mtime_ns,
        before.st_ctime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_nlink if require_single_link else None,
        after.st_size,
        after.st_mtime_ns,
        after.st_ctime_ns,
    )
    raw = b"".join(chunks)
    if (
        identity_before != identity_after
        or len(raw) != before.st_size
        or not stat.S_ISREG(current_state.st_mode)
        or (current_state.st_dev, current_state.st_ino)
        != (after.st_dev, after.st_ino)
        or (require_single_link and current_state.st_nlink != 1)
    ):
        raise LiveControlError(f"{label} changed while its snapshot was read")
    return raw


def _atomic_write_trusted_json(
    path: Path,
    value: dict,
    *,
    trusted_root: Path,
) -> None:
    path, root = _trusted_path(
        path,
        trusted_root=trusted_root,
        label="attempt journal",
    )
    relative = path.relative_to(root)
    if not relative.parts:
        raise LiveControlError("attempt journal path is not a file")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    file_flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    directories: list[int] = []
    temporary_name = (
        f".{relative.parts[-1]}.{os.urandom(12).hex()}.tmp"
    )
    temporary_created = False
    descriptor = -1
    destination_descriptor = -1
    published_descriptor = -1
    destination_identity: tuple[int, int, int, int, int] | None = None
    try:
        current = os.open(root, directory_flags)
        directories.append(current)
        for part in relative.parts[:-1]:
            current = os.open(part, directory_flags, dir_fd=current)
            directories.append(current)
        try:
            destination_descriptor = os.open(
                relative.parts[-1],
                file_flags,
                dir_fd=current,
            )
        except FileNotFoundError:
            destination_descriptor = -1
        if destination_descriptor >= 0:
            destination_state = _verify_open_file_path(
                destination_descriptor,
                parent_descriptor=current,
                name=relative.parts[-1],
                label="attempt journal",
            )
            destination_identity = (
                destination_state.st_dev,
                destination_state.st_ino,
                destination_state.st_size,
                destination_state.st_mtime_ns,
                destination_state.st_ctime_ns,
            )
        descriptor = os.open(
            temporary_name,
            os.O_CREAT
            | os.O_EXCL
            | os.O_WRONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=current,
        )
        temporary_created = True
        _verify_open_file_path(
            descriptor,
            parent_descriptor=current,
            name=temporary_name,
            label="attempt journal temporary file",
        )
        encoded = canonical_json(value) + b"\n"
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fsync(descriptor)
        temporary_state = _verify_open_file_path(
            descriptor,
            parent_descriptor=current,
            name=temporary_name,
            label="attempt journal temporary file",
        )
        if destination_descriptor >= 0:
            current_destination = _verify_open_file_path(
                destination_descriptor,
                parent_descriptor=current,
                name=relative.parts[-1],
                label="attempt journal",
            )
            if (
                current_destination.st_dev,
                current_destination.st_ino,
                current_destination.st_size,
                current_destination.st_mtime_ns,
                current_destination.st_ctime_ns,
            ) != destination_identity:
                raise LiveControlError(
                    "attempt journal changed before publication"
                )
        else:
            try:
                os.stat(
                    relative.parts[-1],
                    dir_fd=current,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                pass
            else:
                raise LiveControlError(
                    "attempt journal appeared before publication"
                )
        os.replace(
            temporary_name,
            relative.parts[-1],
            src_dir_fd=current,
            dst_dir_fd=current,
        )
        temporary_created = False
        published_descriptor = os.open(
            relative.parts[-1],
            file_flags,
            dir_fd=current,
        )
        published_state = _verify_open_file_path(
            published_descriptor,
            parent_descriptor=current,
            name=relative.parts[-1],
            label="attempt journal",
        )
        if (
            published_state.st_dev,
            published_state.st_ino,
        ) != (
            temporary_state.st_dev,
            temporary_state.st_ino,
        ):
            raise LiveControlError(
                "attempt journal publication identity changed"
            )
        if (
            destination_descriptor >= 0
            and os.fstat(destination_descriptor).st_nlink != 0
        ):
            raise LiveControlError(
                "attempt journal became hard-linked during publication"
            )
        os.fsync(current)
    except LiveControlError:
        raise
    except OSError as exc:
        raise LiveControlError(
            f"cannot persist attempt journal: {exc}"
        ) from exc
    finally:
        if published_descriptor >= 0:
            os.close(published_descriptor)
        if destination_descriptor >= 0:
            os.close(destination_descriptor)
        if descriptor >= 0:
            os.close(descriptor)
        if temporary_created and directories:
            try:
                os.unlink(temporary_name, dir_fd=directories[-1])
            except OSError:
                pass
        for directory in reversed(directories):
            os.close(directory)


def _signed_authority(
    path: Path,
    *,
    trusted_root: Path,
    expected_kind: str,
    verifier: SignatureVerifier,
) -> tuple[dict, dict, str, str, str, str]:
    raw = _read_snapshot(path, trusted_root=trusted_root)
    try:
        record = json.loads(
            raw,
            object_pairs_hook=_strict_object,
            parse_constant=lambda value: (_ for _ in ()).throw(
                LiveControlError(f"authority contains invalid number {value}")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiveControlError("authority is not valid JSON") from exc
    if (
        not isinstance(record, dict)
        or set(record) != AUTHORITY_ENVELOPE_FIELDS
        or record.get("schema_version") != 1
        or record.get("authority_kind") != expected_kind
        or not isinstance(record.get("claims"), dict)
        or not isinstance(record.get("signature"), dict)
    ):
        raise LiveControlError("authority envelope is malformed")
    if raw != canonical_json(record) + b"\n":
        raise LiveControlError("authority snapshot must be canonical JSON")
    claims = record["claims"]
    signature = record["signature"]
    if set(signature) != SIGNATURE_FIELDS:
        raise LiveControlError("authority signature descriptor is malformed")
    algorithm = signature.get("algorithm")
    issuer = signature.get("issuer")
    key_id = signature.get("key_id")
    value = signature.get("value")
    if (
        algorithm != "ed25519"
        or any(
            not isinstance(item, str) or not item or item != item.strip()
            for item in (issuer, key_id, value)
        )
    ):
        raise LiveControlError("authority signature descriptor is malformed")
    signed = {
        "schema_version": 1,
        "authority_kind": expected_kind,
        "claims": claims,
    }
    try:
        verified = verifier.verify(
            algorithm=algorithm,
            issuer=issuer,
            key_id=key_id,
            payload=canonical_json(signed),
            signature=value,
        )
    except Exception as exc:
        raise LiveControlError("authority signature verification failed") from exc
    if verified is not True:
        raise LiveControlError("authority signature is invalid")
    return (
        claims,
        signature,
        hashlib.sha256(raw).hexdigest(),
        issuer,
        key_id,
        algorithm,
    )


def _required_string(record: dict, field: str) -> str:
    value = record.get(field)
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or "\n" in value
        or "\r" in value
    ):
        raise LiveControlError(f"authority {field} must be a nonempty string")
    return value


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str) or RFC3339_SECONDS.fullmatch(value) is None:
        raise LiveControlError("authority timestamp must be RFC 3339 UTC seconds")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise LiveControlError("authority timestamp is malformed") from exc


def _positive_usd(value: object, field: str) -> Decimal:
    try:
        return canonical_usd_decimal(
            value,
            f"authority {field}",
            positive=True,
            serialized=True,
        )
    except ContractError as exc:
        raise LiveControlError(str(exc)) from exc


def load_auth_authority(
    path: Path,
    *,
    trusted_root: Path,
    expected: AuthExpectation,
    verifier: SignatureVerifier,
    now: datetime,
) -> AuthAuthority:
    """Load and verify one authentication-authority byte snapshot."""
    claims, _signature, snapshot_sha256, issuer, key_id, _algorithm = (
        _signed_authority(
            path,
            trusted_root=trusted_root,
            expected_kind="claude_bare_auth_v2",
            verifier=verifier,
        )
    )
    if set(claims) != AUTH_CLAIM_FIELDS:
        raise LiveControlError("authentication authority claims are malformed")
    for field_name in AUTH_CLAIM_FIELDS:
        _required_string(claims, field_name)
    if (
        SHA256.fullmatch(claims["run_seed"]) is None
        or SHA256.fullmatch(claims["cli_sha256"]) is None
    ):
        raise LiveControlError("authentication authority CLI hash is malformed")
    if claims["execution_mode"] != "bare":
        raise LiveControlError("authentication authority is not for bare mode")
    _require_provider_credential_compatibility(
        provider=claims["provider"],
        credential_source=claims["credential_source"],
        context="authentication authority",
    )
    bound = (
        claims["run_seed"],
        claims["provider"],
        claims["model_id"],
        claims["cli_version"],
        claims["cli_sha256"],
        claims["account_scope"],
        claims["endpoint"],
    )
    if bound != (
        expected.run_seed,
        expected.provider,
        expected.model_id,
        expected.cli_version,
        expected.cli_sha256,
        expected.account_scope,
        expected.endpoint,
    ):
        raise LiveControlError("authentication authority binding mismatch")
    issued_at = _timestamp(claims.get("issued_at"))
    expires_at = _timestamp(claims.get("expires_at"))
    if now.tzinfo is None or now.utcoffset() is None:
        raise LiveControlError("authority validation time must be timezone-aware")
    now = now.astimezone(UTC)
    if not issued_at <= now < expires_at:
        raise LiveControlError("authentication authority is stale")
    authority = AuthAuthority(
        run_seed=claims["run_seed"],
        provider=claims["provider"],
        model_id=claims["model_id"],
        cli_version=claims["cli_version"],
        cli_sha256=claims["cli_sha256"],
        execution_mode=claims["execution_mode"],
        credential_source=claims["credential_source"],
        account_scope=claims["account_scope"],
        endpoint=claims["endpoint"],
        issued_at=issued_at,
        expires_at=expires_at,
        issuer=issuer,
        key_id=key_id,
        snapshot_sha256=snapshot_sha256,
        _seal="",
    )
    object.__setattr__(
        authority,
        "_seal",
        _factory_seal("auth-authority-v2", authority._descriptor()),
    )
    authority.validate()
    return authority


def load_cost_authority(
    path: Path,
    *,
    trusted_root: Path,
    expected: CostExpectation,
    verifier: SignatureVerifier,
    now: datetime,
) -> CostAuthority:
    """Load and verify one operation-scoped cost-authority byte snapshot."""
    claims, _signature, snapshot_sha256, issuer, key_id, _algorithm = (
        _signed_authority(
            path,
            trusted_root=trusted_root,
            expected_kind="operation_cost_authority_v2",
            verifier=verifier,
        )
    )
    if set(claims) != COST_CLAIM_FIELDS:
        raise LiveControlError("cost authority claims are malformed")
    for field_name in COST_CLAIM_FIELDS - {
        "expected_units",
        "max_total_usd",
        "max_unit_usd",
    }:
        _required_string(claims, field_name)
    max_total = _positive_usd(claims["max_total_usd"], "max_total_usd")
    max_unit = _positive_usd(claims["max_unit_usd"], "max_unit_usd")
    expected_units = claims["expected_units"]
    _require_expected_cost_capacity(
        max_total_usd=max_total,
        max_unit_usd=max_unit,
        expected_units=expected_units,
        context="cost authority",
    )
    if claims["currency"] != "USD":
        raise LiveControlError("cost authority currency must be USD")
    if claims["mechanism"] not in COST_MECHANISMS:
        raise LiveControlError("cost authority mechanism is not enforceable")
    if (
        SHA256.fullmatch(claims["run_seed"]) is None
        or SHA256.fullmatch(claims["calibration_sha256"]) is None
    ):
        raise LiveControlError("cost authority calibration hash is malformed")
    bound = (
        claims["run_seed"],
        claims["provider"],
        claims["model_id"],
        claims["account_scope"],
        claims["endpoint"],
        max_total,
        max_unit,
        expected_units,
        claims["calibration_sha256"],
    )
    if bound != (
        expected.run_seed,
        expected.provider,
        expected.model_id,
        expected.account_scope,
        expected.endpoint,
        expected.max_total_usd,
        expected.max_unit_usd,
        expected.expected_units,
        expected.calibration_sha256,
    ):
        raise LiveControlError("cost authority binding mismatch")
    issued_at = _timestamp(claims["issued_at"])
    expires_at = _timestamp(claims["expires_at"])
    if now.tzinfo is None or now.utcoffset() is None:
        raise LiveControlError("authority validation time must be timezone-aware")
    now = now.astimezone(UTC)
    if not issued_at <= now < expires_at:
        raise LiveControlError("cost authority is stale")
    authority = CostAuthority(
        run_seed=claims["run_seed"],
        provider=claims["provider"],
        model_id=claims["model_id"],
        account_scope=claims["account_scope"],
        endpoint=claims["endpoint"],
        currency=claims["currency"],
        max_total_usd=max_total,
        max_unit_usd=max_unit,
        expected_units=expected_units,
        calibration_sha256=claims["calibration_sha256"],
        mechanism=claims["mechanism"],
        issued_at=issued_at,
        expires_at=expires_at,
        issuer=issuer,
        key_id=key_id,
        snapshot_sha256=snapshot_sha256,
        _seal="",
    )
    object.__setattr__(
        authority,
        "_seal",
        _factory_seal("cost-authority-v2", authority._descriptor()),
    )
    authority.validate()
    return authority


def _validate_mcp_servers(
    servers: Mapping[str, McpServerSpec],
) -> None:
    if set(servers) != set(MCP_TOOLS):
        raise LiveControlError("MCP server set must contain exactly both components")
    for name, expected_tools in MCP_TOOLS.items():
        spec = servers.get(name)
        if isinstance(spec, McpServerSpec) and any(
            CREDENTIAL_ARGUMENT.search(value)
            or "authorization" in value.casefold()
            or "bearer " in value.casefold()
            or "bearer=" in value.casefold()
            for value in (spec.command, *spec.args)
        ):
            raise LiveControlError(
                f"{name} MCP server arguments may not contain credentials"
            )
        if (
            not isinstance(spec, McpServerSpec)
            or spec.name != name
            or not isinstance(spec.command, str)
            or not Path(spec.command).is_absolute()
            or "\x00" in spec.command
            or not isinstance(spec.args, tuple)
            or any(
                not isinstance(argument, str)
                or not argument
                or "\x00" in argument
                for argument in spec.args
            )
            or spec.tools != expected_tools
        ):
            raise LiveControlError(f"{name} MCP server specification is invalid")
        if (spec.command, spec.args) != REVIEWED_MCP_LAUNCHES[name]:
            raise LiveControlError(
                f"{name} MCP server launch is not the reviewed specification"
            )


def compile_claude_invocation(
    *,
    arm: str,
    prompt: str,
    response_schema: dict,
    controls: FrozenControls,
    mcp_servers: Mapping[str, McpServerSpec],
    auth_authority: AuthAuthority,
    child_environments: ChildEnvironments | None = None,
    root: Path,
) -> ClaudeInvocation:
    """Compile one exact arm descriptor without executing or writing it."""
    if not isinstance(auth_authority, AuthAuthority):
        raise LiveControlError(
            "sealed child environment does not bind the invocation"
        )
    auth_authority.validate()
    if arm not in ARM_CONTRACTS:
        raise LiveControlError(f"unknown comparison arm {arm!r}")
    if (
        not isinstance(prompt, str)
        or not prompt.strip()
        or "\x00" in prompt
        or not isinstance(response_schema, dict)
    ):
        raise LiveControlError("prompt and response schema are required")
    _validate_mcp_servers(mcp_servers)
    controls_descriptor = controls.descriptor(Path(root))
    if controls.cost_policy != "authoritative_operation_bound":
        raise LiveControlError("Claude descriptors require authoritative live controls")
    if (
        not isinstance(child_environments, ChildEnvironments)
        or child_environments.arm != arm
        or SHA256.fullmatch(child_environments.descriptor_sha256) is None
        or child_environments.auth_snapshot_sha256
        != auth_authority.snapshot_sha256
        or controls.provider != auth_authority.provider
        or controls.model_id != auth_authority.model_id
        or controls.cli_version != auth_authority.cli_version
        or SHA256.fullmatch(auth_authority.cli_sha256) is None
    ):
        raise LiveControlError(
            "sealed child environment does not bind the invocation"
        )
    child_environments.validate()
    response_schema_path = Path(root) / "bench" / "compare" / "response-schema.json"
    try:
        expected_response_schema = json.loads(
            response_schema_path.read_bytes(),
            object_pairs_hook=_strict_object,
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiveControlError("frozen response schema cannot be loaded") from exc
    if canonical_json(response_schema) != canonical_json(expected_response_schema):
        raise LiveControlError("response schema differs from the frozen contract")
    selected_names = ARM_MCP_SERVERS[arm]
    mcp_environments = dict(child_environments.mcp)
    if set(mcp_environments) != set(selected_names):
        raise LiveControlError("MCP launch environments do not match the arm")
    selected_servers = {
        name: {
            "args": list(mcp_servers[name].args),
            "command": mcp_servers[name].command,
            "env": dict(mcp_environments[name]),
            "type": "stdio",
        }
        for name in selected_names
    }
    mcp_config_json = canonical_json(
        {"mcpServers": selected_servers}
    ).decode("utf-8")
    response_schema_json = canonical_json(response_schema).decode("utf-8")
    system_path = Path(root) / "bench" / "compare" / "system.md"
    try:
        system_bytes = system_path.read_bytes()
        system_prompt = system_bytes.decode("utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise LiveControlError("frozen system prompt cannot be loaded") from exc
    expected_system_sha256 = controls_descriptor["hashes"]["system"]
    if hashlib.sha256(system_bytes).hexdigest() != expected_system_sha256:
        raise LiveControlError("system prompt differs from the frozen contract")
    allowed_tools = ARM_CONTRACTS[arm].allowed_tools
    builtins = tuple(
        tool for tool in allowed_tools if not tool.startswith("mcp__")
    )
    disallowed_tools = tuple(sorted(FORBIDDEN_TOOLS))
    argv = (
        REVIEWED_CLAUDE_EXECUTABLE,
        "--bare",
        "--print",
        prompt,
        "--output-format",
        "json",
        "--json-schema",
        response_schema_json,
        "--model",
        controls.model_id,
        "--system-prompt",
        system_prompt,
        "--permission-mode",
        "plan",
        "--no-session-persistence",
        "--strict-mcp-config",
        "--mcp-config",
        mcp_config_json,
        "--tools",
        ",".join(builtins),
        "--allowedTools",
        ",".join(allowed_tools),
        "--disallowedTools",
        ",".join(disallowed_tools),
        "--max-turns",
        str(controls.max_discovery_tool_calls),
        "--max-budget-usd",
        format(controls.max_unit_usd, "f"),
    )
    common = {
        "schema_version": 1,
        "executable": REVIEWED_CLAUDE_EXECUTABLE,
        "executable_sha256": auth_authority.cli_sha256,
        "controls": controls_descriptor,
        "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
        "response_schema_sha256": hashlib.sha256(
            response_schema_json.encode("utf-8")
        ).hexdigest(),
        "system_sha256": expected_system_sha256,
        "max_budget_usd_role": "defense_in_depth_only",
    }
    common_sha256 = hashlib.sha256(canonical_json(common)).hexdigest()
    descriptor = {
        "schema_version": 1,
        "arm": arm,
        "argv": list(argv),
        "allowed_tools": list(allowed_tools),
        "mcp_server_names": list(selected_names),
        "mcp_config_sha256": hashlib.sha256(
            mcp_config_json.encode("utf-8")
        ).hexdigest(),
        "common_controls_sha256": common_sha256,
        "environment_sha256": child_environments.descriptor_sha256,
        "executable_sha256": auth_authority.cli_sha256,
    }
    enforcement = AdapterEnforcement(
        max_tool_calls=controls.max_discovery_tool_calls,
        max_evidence_tokens=controls.evidence_token_budget,
        max_context_tokens=controls.context_token_budget,
        max_wall_seconds=controls.wall_timeout_seconds,
    )
    descriptor["adapter_enforcement"] = enforcement.descriptor()
    invocation = ClaudeInvocation(
        arm=arm,
        argv=argv,
        allowed_tools=allowed_tools,
        mcp_server_names=selected_names,
        mcp_config_json=mcp_config_json,
        mcp_config_sha256=descriptor["mcp_config_sha256"],
        common_controls_sha256=common_sha256,
        environment_sha256=child_environments.descriptor_sha256,
        executable_sha256=auth_authority.cli_sha256,
        descriptor_sha256=hashlib.sha256(canonical_json(descriptor)).hexdigest(),
        max_discovery_tool_calls=controls.max_discovery_tool_calls,
        evidence_token_budget=controls.evidence_token_budget,
        context_token_budget=controls.context_token_budget,
        wall_timeout_seconds=controls.wall_timeout_seconds,
        max_budget_usd_role="defense_in_depth_only",
        adapter_enforcement=enforcement,
        _seal=_factory_seal("claude-invocation-v1", descriptor),
    )
    invocation.validate()
    return invocation


def _validated_environment_values(
    values: Mapping[str, str],
    *,
    allowed: frozenset[str],
    required: frozenset[str] = frozenset(),
    label: str,
) -> dict[str, str]:
    if not isinstance(values, Mapping) or not set(values) <= allowed:
        raise LiveControlError(f"{label} environment contains an unlisted name")
    if not required <= set(values):
        raise LiveControlError(f"{label} environment is missing required credentials")
    result: dict[str, str] = {}
    for name, value in values.items():
        if (
            not isinstance(name, str)
            or not isinstance(value, str)
            or not value
            or "\x00" in value
        ):
            raise LiveControlError(f"{label} environment value is invalid")
        result[name] = value
    return result


def _child_base(
    ambient: Mapping[str, str],
    *,
    isolated_root: Path,
    child_name: str,
) -> dict[str, str]:
    base = _validated_environment_values(
        {
            name: ambient[name]
            for name in SAFE_AMBIENT_ENVIRONMENT
            if name in ambient
        },
        allowed=frozenset(SAFE_AMBIENT_ENVIRONMENT),
        label="ambient base",
    )
    child_root = isolated_root / child_name
    base.update(
        {
            "HOME": str(child_root / "home"),
            "TMPDIR": str(child_root / "tmp"),
            "XDG_CACHE_HOME": str(child_root / "cache"),
            "XDG_CONFIG_HOME": str(child_root / "config"),
            "PYTHONNOUSERSITE": "1",
        }
    )
    return base


def build_child_environments(
    *,
    arm: str,
    ambient: Mapping[str, str],
    trusted_root: Path,
    isolated_root: Path,
    auth_authority: AuthAuthority,
    fetch_credentials: Mapping[str, str],
    model_credentials: Mapping[str, str],
) -> ChildEnvironments:
    """Build isolated fetch/model/MCP environments without ambient credentials."""
    if not isinstance(auth_authority, AuthAuthority):
        raise LiveControlError(
            "authentication authority is required for child isolation"
        )
    auth_authority.validate()
    if arm not in ARM_MCP_SERVERS:
        raise LiveControlError(f"unknown comparison arm {arm!r}")
    isolated_root = Path(isolated_root)
    isolated_root, resolved_trusted_root = _trusted_path(
        isolated_root,
        trusted_root=trusted_root,
        label="isolated child root",
    )
    if isolated_root.exists() and not isolated_root.is_dir():
        raise LiveControlError("isolated child root must be a directory")
    fetch_secrets = _validated_environment_values(
        fetch_credentials,
        allowed=FETCH_CREDENTIALS,
        label="fetch",
    )
    model_policy = MODEL_CREDENTIALS.get(auth_authority.credential_source)
    if model_policy is None:
        raise LiveControlError("authentication source has no model environment policy")
    model_secrets = _validated_environment_values(
        model_credentials,
        allowed=model_policy[0],
        required=model_policy[1],
        label="model",
    )
    fetch = _child_base(
        ambient,
        isolated_root=isolated_root,
        child_name="fetch",
    )
    fetch.update(fetch_secrets)
    model = _child_base(
        ambient,
        isolated_root=isolated_root,
        child_name="model",
    )
    model["CLAUDE_CONFIG_DIR"] = str(
        isolated_root / "model" / "claude-config"
    )
    model.update(model_secrets)
    mcp = tuple(
        (
            server,
            tuple(
                sorted(
                    _child_base(
                        ambient,
                        isolated_root=isolated_root,
                        child_name=f"mcp-{server}",
                    ).items()
                )
            ),
        )
        for server in ARM_MCP_SERVERS[arm]
    )
    fetch_items = tuple(sorted(fetch.items()))
    model_items = tuple(sorted(model.items()))
    directory_relatives = _child_directory_relatives(
        trusted_root=resolved_trusted_root,
        isolated_root=isolated_root,
        environments=(
            fetch_items,
            model_items,
            *(values for _, values in mcp),
        ),
    )
    (
        trusted_root_identity,
        directory_identities,
    ) = _secure_create_child_directories(
        trusted_root=resolved_trusted_root,
        relative_paths=directory_relatives,
    )
    descriptor = {
        "schema_version": 2,
        "arm": arm,
        "auth_snapshot_sha256": auth_authority.snapshot_sha256,
        "credential_source": auth_authority.credential_source,
        "trusted_root": str(resolved_trusted_root),
        "trusted_root_device": trusted_root_identity[0],
        "trusted_root_inode": trusted_root_identity[1],
        "isolated_root": str(isolated_root),
        "directory_identities": [
            {
                "relative_path": relative_path,
                "device": device,
                "inode": inode,
            }
            for relative_path, device, inode in directory_identities
        ],
        "fetch": [list(item) for item in fetch_items],
        "model": [list(item) for item in model_items],
        "mcp": [
            {
                "name": name,
                "environment": [list(item) for item in values],
            }
            for name, values in mcp
        ],
    }
    environments = ChildEnvironments(
        fetch=fetch_items,
        model=model_items,
        mcp=mcp,
        arm=arm,
        auth_snapshot_sha256=auth_authority.snapshot_sha256,
        credential_source=auth_authority.credential_source,
        trusted_root=str(resolved_trusted_root),
        trusted_root_device=trusted_root_identity[0],
        trusted_root_inode=trusted_root_identity[1],
        isolated_root=str(isolated_root),
        directory_identities=directory_identities,
        descriptor_sha256=hashlib.sha256(canonical_json(descriptor)).hexdigest(),
        _seal=_factory_seal("child-environments-v2", descriptor),
    )
    environments.revalidate_isolation()
    return environments


def _canonical_decimal_6(
    value: object,
    field_name: str,
    *,
    positive: bool,
    serialized: bool = False,
) -> Decimal:
    try:
        return canonical_usd_decimal(
            value,
            field_name,
            positive=positive,
            serialized=serialized,
        )
    except ContractError as exc:
        raise LiveControlError(str(exc)) from exc


def _decimal_6(value: Decimal, field_name: str, *, positive: bool) -> str:
    try:
        return format_usd_decimal(
            value,
            field_name,
            positive=positive,
        )
    except ContractError as exc:
        raise LiveControlError(str(exc)) from exc


def _microdollars(
    value: object,
    field_name: str,
    *,
    positive: bool,
    serialized: bool = False,
) -> int:
    try:
        return usd_micros(
            value,
            field_name,
            positive=positive,
            serialized=serialized,
        )
    except ContractError as exc:
        raise LiveControlError(str(exc)) from exc


def _decimal_from_microdollars(
    micros: int,
    field_name: str,
    *,
    positive: bool,
) -> Decimal:
    try:
        return usd_decimal_from_micros(
            micros,
            field_name,
            positive=positive,
        )
    except ContractError as exc:
        raise LiveControlError(str(exc)) from exc


ATTEMPT_PHASE_FIELDS = {
    "reserved": frozenset(
        {
            "attempt_number",
            "idempotency_key",
            "phase",
            "transitions",
            "request",
            "reservation",
        }
    ),
    "dispatching": frozenset(
        {
            "attempt_number",
            "idempotency_key",
            "phase",
            "transitions",
            "request",
            "reservation",
            "descriptor_sha256",
        }
    ),
    "receipt": frozenset(
        {
            "attempt_number",
            "idempotency_key",
            "phase",
            "transitions",
            "request",
            "reservation",
            "descriptor_sha256",
            "receipt",
        }
    ),
    "classified": frozenset(
        {
            "attempt_number",
            "idempotency_key",
            "phase",
            "transitions",
            "request",
            "reservation",
            "descriptor_sha256",
            "receipt",
            "classification",
        }
    ),
}
ATTEMPT_TRANSITIONS = {
    "reserved": ["reserved"],
    "dispatching": ["reserved", "dispatching"],
    "receipt": ["reserved", "dispatching", "receipt"],
    "classified": ["reserved", "dispatching", "receipt", "classified"],
}


def _journal_micros(value: object, *, positive: bool) -> int:
    return _microdollars(
        value,
        "attempt journal decimal",
        positive=positive,
        serialized=True,
    )


def _validate_journal_attempts(
    state: dict,
) -> None:
    attempts = state["attempts"]
    execution_contract = state["execution_contract"]
    max_attempts = execution_contract["max_attempts"]
    if len(attempts) > max_attempts:
        raise LiveControlError("attempt journal exceeds the frozen retry limit")
    max_unit_micros = _journal_micros(
        execution_contract["max_unit_usd"],
        positive=True,
    )
    max_total_micros = _journal_micros(
        execution_contract["max_total_usd"],
        positive=True,
    )
    if max_unit_micros > max_total_micros:
        raise LiveControlError("attempt journal contract caps are malformed")
    cumulative_cost_micros = 0
    allowed_errors = (
        set(LIVE_RETRYABLE_ERROR_CLASSES)
        | NONFINALIZABLE_ERROR_CLASSES
        | MEASURED_OUTCOME_ERROR_CLASSES
    )
    for index, attempt in enumerate(attempts, 1):
        if not isinstance(attempt, dict):
            raise LiveControlError("attempt journal entry must be an object")
        phase = attempt.get("phase")
        expected_fields = ATTEMPT_PHASE_FIELDS.get(phase)
        if (
            expected_fields is None
            or set(attempt) != expected_fields
            or attempt.get("attempt_number") != index
            or attempt.get("transitions") != ATTEMPT_TRANSITIONS[phase]
            or attempt.get("idempotency_key")
            != _attempt_idempotency_key(
                state["run_id"],
                state["unit_key"],
                index,
            )
        ):
            raise LiveControlError("attempt journal phase or identity is malformed")
        if index < len(attempts) and phase != "classified":
            raise LiveControlError("attempt journal has an unsettled prior attempt")
        request = attempt.get("request")
        reservation = attempt.get("reservation")
        if (
            not isinstance(request, dict)
            or set(request) != {"max_unit_usd", "max_total_usd"}
            or not isinstance(reservation, dict)
            or set(reservation) != {"reservation_id", "authorized_usd"}
            or not isinstance(reservation.get("reservation_id"), str)
            or not reservation["reservation_id"]
        ):
            raise LiveControlError("attempt journal reservation is malformed")
        request_unit_micros = _journal_micros(
            request["max_unit_usd"],
            positive=True,
        )
        request_total_micros = _journal_micros(
            request["max_total_usd"],
            positive=True,
        )
        expected_unit_micros = max_unit_micros - cumulative_cost_micros
        expected_total_micros = max_total_micros - cumulative_cost_micros
        if (
            expected_unit_micros <= 0
            or expected_total_micros <= 0
            or request_unit_micros != expected_unit_micros
            or request_total_micros != expected_total_micros
        ):
            raise LiveControlError(
                "attempt journal request differs from its remaining cap"
            )
        authorized_micros = _journal_micros(
            reservation["authorized_usd"],
            positive=False,
        )
        if (
            authorized_micros > request_unit_micros
            or authorized_micros > request_total_micros
        ):
            raise LiveControlError("attempt journal reservation exceeds its request")
        if phase in {"dispatching", "receipt", "classified"} and SHA256.fullmatch(
            attempt.get("descriptor_sha256", "")
        ) is None:
            raise LiveControlError("attempt journal descriptor is malformed")
        if (
            phase in {"dispatching", "receipt", "classified"}
            and attempt["descriptor_sha256"]
            != state["execution_contract"]["invocation_descriptor_sha256"]
        ):
            raise LiveControlError("attempt journal descriptor binding is malformed")
        if phase not in {"receipt", "classified"}:
            continue
        receipt = attempt.get("receipt")
        if (
            not isinstance(receipt, dict)
            or set(receipt)
            != {
                "operation_id",
                "status",
                "cost_usd",
                "response_sha256",
                "error_class",
            }
            or not isinstance(receipt.get("operation_id"), str)
            or not receipt["operation_id"]
        ):
            raise LiveControlError("attempt journal receipt is malformed")
        cost_micros = _journal_micros(
            receipt["cost_usd"],
            positive=False,
        )
        if (
            cost_micros > authorized_micros
            or cost_micros > request_unit_micros
            or cost_micros > request_total_micros
        ):
            raise LiveControlError("attempt journal receipt exceeds its reservation")
        cumulative_cost_micros += cost_micros
        if (
            cumulative_cost_micros > max_unit_micros
            or cumulative_cost_micros > max_total_micros
        ):
            raise LiveControlError(
                "attempt journal cumulative cost exceeds its contract"
            )
        if receipt.get("status") == "ok":
            valid_receipt = (
                isinstance(receipt.get("response_sha256"), str)
                and SHA256.fullmatch(receipt["response_sha256"]) is not None
                and receipt.get("error_class") is None
            )
            expected_classification = {
                "value": "success",
                "retryable": False,
            }
        elif receipt.get("status") == "error":
            error_class = receipt.get("error_class")
            valid_receipt = (
                receipt.get("response_sha256") is None
                and error_class in allowed_errors
            )
            if error_class in LIVE_RETRYABLE_ERROR_CLASSES:
                expected_classification = {
                    "value": "transient_error",
                    "retryable": index < max_attempts,
                }
            elif error_class in NONFINALIZABLE_ERROR_CLASSES:
                expected_classification = {
                    "value": "fatal_error",
                    "retryable": False,
                }
            else:
                expected_classification = {
                    "value": "measured_error",
                    "retryable": False,
                }
        else:
            valid_receipt = False
            expected_classification = {}
        if not valid_receipt:
            raise LiveControlError("attempt journal receipt is malformed")
        if phase == "classified" and (
            not isinstance(attempt.get("classification"), dict)
            or attempt["classification"] != expected_classification
        ):
            raise LiveControlError("attempt journal classification is malformed")
        if index < len(attempts) and (
            phase != "classified"
            or receipt.get("status") != "error"
            or receipt.get("error_class")
            not in LIVE_RETRYABLE_ERROR_CLASSES
            or attempt.get("classification")
            != {
                "value": "transient_error",
                "retryable": True,
            }
        ):
            raise LiveControlError(
                "attempt journal continues after a terminal result"
            )


EXECUTION_CONTRACT_TEMPLATE_FIELDS = frozenset(
    {
        "schema_version",
        "run_seed",
        "unit_key",
        "arm",
        "provider",
        "model_id",
        "cli_version",
        "cli_sha256",
        "credential_source",
        "endpoint",
        "account_scope",
        "expected_units",
        "cost_mechanism",
        "auth_issuer",
        "auth_key_id",
        "auth_expires_at",
        "cost_issuer",
        "cost_key_id",
        "cost_expires_at",
        "auth_snapshot_sha256",
        "cost_snapshot_sha256",
        "controls",
        "controls_sha256",
        "environment_sha256",
        "invocation_descriptor_sha256",
        "max_total_usd",
        "max_unit_usd",
        "calibration_sha256",
        "retry_policy",
        "max_attempts",
        "adapter_enforcement_sha256",
        "run_fingerprint_sha256",
        "template_sha256",
    }
)
EXECUTION_CONTRACT_FIELDS = frozenset(
    {
        *EXECUTION_CONTRACT_TEMPLATE_FIELDS,
        "run_id",
        "descriptor_sha256",
    }
)


def _is_required_identity_string(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value == value.strip()
        and "\n" not in value
        and "\r" not in value
    )


def _is_sha256_string(value: object) -> bool:
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _validate_live_controls_descriptor(controls: object) -> dict:
    if not isinstance(controls, dict) or set(controls) != {
        "provider",
        "model_id",
        "cli_version",
        "temperature",
        "top_k",
        "max_discovery_tool_calls",
        "repository_evidence",
        "context_token_budget",
        "wall_timeout_seconds",
        "permission_mode",
        "fresh_session",
        "memory",
        "network_tool",
        "cost",
        "retry",
        "hashes",
    }:
        raise LiveControlError("execution contract controls are malformed")
    repository_evidence = controls.get("repository_evidence")
    cost = controls.get("cost")
    retry = controls.get("retry")
    hashes = controls.get("hashes")
    if (
        any(
            not _is_required_identity_string(controls.get(field))
            for field in ("provider", "model_id", "cli_version")
        )
        or controls.get("temperature") != "0"
        or type(controls.get("top_k")) is not int
        or controls.get("top_k") != 10
        or type(controls.get("max_discovery_tool_calls")) is not int
        or controls.get("max_discovery_tool_calls") != 20
        or type(controls.get("context_token_budget")) is not int
        or controls.get("context_token_budget") != 128_000
        or type(controls.get("wall_timeout_seconds")) is not int
        or controls.get("wall_timeout_seconds") != 600
        or controls.get("permission_mode") != "plan"
        or controls.get("fresh_session") is not True
        or controls.get("memory") is not False
        or controls.get("network_tool") is not False
        or not isinstance(repository_evidence, dict)
        or set(repository_evidence) != {"unit", "tokenizer", "budget"}
        or repository_evidence.get("unit") != "novel_tokens"
        or repository_evidence.get("tokenizer") != tokenizer_descriptor()
        or type(repository_evidence.get("budget")) is not int
        or repository_evidence.get("budget") != 64_000
        or not isinstance(cost, dict)
        or set(cost)
        != {
            "policy",
            "max_total_usd",
            "max_unit_usd",
            "calibration_sha256",
        }
        or cost.get("policy") != "authoritative_operation_bound"
        or SHA256.fullmatch(cost.get("calibration_sha256", "")) is None
        or not isinstance(retry, dict)
        or set(retry)
        != {
            "policy",
            "max_attempts",
            "retryable_error_classes",
        }
        or retry.get("policy")
        != "reconcile_before_transient_retry_v1"
        or type(retry.get("max_attempts")) is not int
        or retry.get("max_attempts") != 2
        or retry.get("retryable_error_classes")
        != list(LIVE_RETRYABLE_ERROR_CLASSES)
        or not isinstance(hashes, dict)
        or set(hashes) != {"prompt", "response_schema", "system"}
        or any(
            not _is_sha256_string(hashes.get(field))
            for field in ("prompt", "response_schema", "system")
        )
    ):
        raise LiveControlError("execution contract controls are malformed")
    for cap_field in ("max_total_usd", "max_unit_usd"):
        value = cost.get(cap_field)
        try:
            _canonical_decimal_6(
                value,
                "execution contract controls cost cap",
                positive=True,
                serialized=True,
            )
        except LiveControlError as exc:
            raise LiveControlError(
                "execution contract controls cost cap is malformed"
            ) from exc
    if _canonical_decimal_6(
        cost["max_unit_usd"],
        "execution contract controls unit cap",
        positive=True,
        serialized=True,
    ) > _canonical_decimal_6(
        cost["max_total_usd"],
        "execution contract controls total cap",
        positive=True,
        serialized=True,
    ):
        raise LiveControlError(
            "execution contract controls unit cap exceeds total cap"
        )
    return controls


def validate_execution_contract_template_descriptor(
    descriptor: object,
) -> dict:
    if not isinstance(descriptor, dict) or set(descriptor) != (
        EXECUTION_CONTRACT_TEMPLATE_FIELDS
    ):
        raise LiveControlError("execution contract template is malformed")
    controls = descriptor.get("controls")
    unit_key = descriptor.get("unit_key")
    arm = descriptor.get("arm")
    unit_parts = unit_key.rsplit("|", 1) if isinstance(unit_key, str) else ()
    if (
        type(descriptor.get("schema_version")) is not int
        or descriptor.get("schema_version") != 1
        or not isinstance(controls, dict)
        or hashlib.sha256(canonical_json(controls)).hexdigest()
        != descriptor.get("controls_sha256")
        or any(
            not _is_sha256_string(descriptor.get(field))
            for field in (
                "run_seed",
                "cli_sha256",
                "calibration_sha256",
                "auth_snapshot_sha256",
                "cost_snapshot_sha256",
                "controls_sha256",
                "environment_sha256",
                "invocation_descriptor_sha256",
                "adapter_enforcement_sha256",
                "run_fingerprint_sha256",
                "template_sha256",
            )
        )
        or any(
            not _is_required_identity_string(descriptor.get(field))
            for field in (
                "unit_key",
                "arm",
                "provider",
                "model_id",
                "cli_version",
                "credential_source",
                "endpoint",
                "account_scope",
                "cost_mechanism",
                "auth_issuer",
                "auth_key_id",
                "auth_expires_at",
                "cost_issuer",
                "cost_key_id",
                "cost_expires_at",
                "retry_policy",
            )
        )
        or arm not in ARM_CONTRACTS
        or len(unit_parts) != 2
        or not unit_parts[0]
        or unit_parts[1] != arm
        or type(descriptor.get("max_attempts")) is not int
        or descriptor.get("max_attempts") != 2
        or descriptor.get("retry_policy")
        != "reconcile_before_transient_retry_v1"
        or type(descriptor.get("expected_units")) is not int
        or descriptor["expected_units"] < 1
        or descriptor.get("cost_mechanism") not in COST_MECHANISMS
    ):
        raise LiveControlError("execution contract template is malformed")
    controls = _validate_live_controls_descriptor(controls)
    cost = controls["cost"]
    retry = controls["retry"]
    descriptor_caps: dict[str, Decimal] = {}
    for cap_field in ("max_total_usd", "max_unit_usd"):
        value = descriptor.get(cap_field)
        try:
            descriptor_caps[cap_field] = _canonical_decimal_6(
                value,
                "execution contract template cost cap",
                positive=True,
                serialized=True,
            )
        except LiveControlError as exc:
            raise LiveControlError(
                "execution contract template cost cap is malformed"
            ) from exc
    _require_provider_credential_compatibility(
        provider=descriptor["provider"],
        credential_source=descriptor["credential_source"],
        context="execution contract template",
    )
    _require_expected_cost_capacity(
        max_total_usd=descriptor_caps["max_total_usd"],
        max_unit_usd=descriptor_caps["max_unit_usd"],
        expected_units=descriptor["expected_units"],
        context="execution contract template",
    )
    if (
        descriptor_caps["max_unit_usd"]
        > descriptor_caps["max_total_usd"]
        or descriptor.get("provider") != controls["provider"]
        or descriptor.get("model_id") != controls["model_id"]
        or descriptor.get("cli_version") != controls["cli_version"]
        or descriptor.get("max_total_usd") != cost["max_total_usd"]
        or descriptor.get("max_unit_usd") != cost["max_unit_usd"]
        or descriptor.get("calibration_sha256")
        != cost["calibration_sha256"]
        or descriptor.get("retry_policy") != retry["policy"]
        or descriptor.get("max_attempts") != retry["max_attempts"]
    ):
        raise LiveControlError(
            "execution contract template differs from frozen controls"
        )
    expected_adapter_enforcement = AdapterEnforcement(
        max_tool_calls=controls["max_discovery_tool_calls"],
        max_evidence_tokens=controls["repository_evidence"]["budget"],
        max_context_tokens=controls["context_token_budget"],
        max_wall_seconds=controls["wall_timeout_seconds"],
    )
    if hashlib.sha256(
        canonical_json(expected_adapter_enforcement.descriptor())
    ).hexdigest() != descriptor["adapter_enforcement_sha256"]:
        raise LiveControlError(
            "execution contract adapter limits differ from frozen controls"
        )
    for expiry_field in ("auth_expires_at", "cost_expires_at"):
        _timestamp(descriptor[expiry_field])
    if execution_contract_run_fingerprint_sha256(
        descriptor
    ) != descriptor.get("run_fingerprint_sha256"):
        raise LiveControlError("execution contract run fingerprint mismatch")
    if execution_contract_template_sha256(descriptor) != descriptor.get(
        "template_sha256"
    ):
        raise LiveControlError("execution contract template digest mismatch")
    return descriptor


def _validate_execution_contract_descriptor(descriptor: object) -> dict:
    if not isinstance(descriptor, dict) or set(descriptor) != (
        EXECUTION_CONTRACT_FIELDS
    ):
        raise LiveControlError("journal execution contract is malformed")
    if (
        descriptor.get("schema_version") != 2
        or SHA256.fullmatch(descriptor.get("run_id", "")) is None
        or SHA256.fullmatch(descriptor.get("descriptor_sha256", "")) is None
    ):
        raise LiveControlError("journal execution contract is malformed")
    template = {
        "schema_version": 1,
        **{
            key: value
            for key, value in descriptor.items()
            if key not in {"schema_version", "run_id", "descriptor_sha256"}
        },
    }
    validate_execution_contract_template_descriptor(template)
    expected = execution_contract_identity_sha256(
        run_id=descriptor["run_id"],
        run_seed=descriptor["run_seed"],
        unit_key=descriptor["unit_key"],
        template_sha256=descriptor["template_sha256"],
    )
    if expected != descriptor["descriptor_sha256"]:
        raise LiveControlError("journal execution contract digest mismatch")
    return descriptor


def _journal_identity(
    *,
    run_id: str,
    run_seed: str,
    unit_key: str,
    execution_contract_template_sha256: str,
    execution_contract_sha256: str,
) -> str:
    return hashlib.sha256(
        canonical_json(
            {
                "schema_version": 3,
                "run_id": run_id,
                "run_seed": run_seed,
                "unit_key": unit_key,
                "execution_contract_template_sha256": (
                    execution_contract_template_sha256
                ),
                "execution_contract_sha256": execution_contract_sha256,
            }
        )
    ).hexdigest()


def _attempt_snapshot(attempt: dict, phase: str) -> dict:
    snapshot = {
        key: value
        for key, value in json.loads(canonical_json(attempt)).items()
        if key in ATTEMPT_PHASE_FIELDS[phase]
    }
    snapshot["phase"] = phase
    snapshot["transitions"] = ATTEMPT_TRANSITIONS[phase]
    return snapshot


def _event_digest(journal_identity_sha256: str, event: dict) -> str:
    payload = {
        "journal_identity_sha256": journal_identity_sha256,
        **{
            key: value
            for key, value in event.items()
            if key != "event_sha256"
        },
    }
    return hashlib.sha256(canonical_json(payload)).hexdigest()


def _validate_event_chain(state: dict) -> None:
    events = state["events"]
    if not isinstance(events, list):
        raise LiveControlError("attempt journal event chain is malformed")
    expected_snapshots = [
        _attempt_snapshot(attempt, phase)
        for attempt in state["attempts"]
        for phase in attempt["transitions"]
    ]
    if len(events) != len(expected_snapshots):
        raise LiveControlError("attempt journal event chain is incomplete")
    previous = state["journal_identity_sha256"]
    for sequence, (event, expected_attempt) in enumerate(
        zip(events, expected_snapshots, strict=True),
        1,
    ):
        if (
            not isinstance(event, dict)
            or set(event)
            != {
                "sequence",
                "previous_sha256",
                "attempt_number",
                "phase",
                "attempt",
                "event_sha256",
            }
            or event.get("sequence") != sequence
            or event.get("previous_sha256") != previous
            or event.get("attempt_number")
            != expected_attempt["attempt_number"]
            or event.get("phase") != expected_attempt["phase"]
            or event.get("attempt") != expected_attempt
            or event.get("event_sha256")
            != _event_digest(state["journal_identity_sha256"], event)
        ):
            raise LiveControlError("attempt journal event digest chain is malformed")
        previous = event["event_sha256"]


def _validate_attempt_journal_bytes(
    raw: bytes,
    *,
    expected_run_id: str,
    expected_unit_key: str,
    require_terminal: bool,
) -> dict:
    try:
        state = json.loads(raw, object_pairs_hook=_strict_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LiveControlError("attempt journal is unreadable") from exc
    if raw != canonical_json(state) + b"\n":
        raise LiveControlError("attempt journal must be canonical JSON")
    if (
        not isinstance(state, dict)
        or set(state)
        != {
            "schema_version",
            "run_id",
            "run_seed",
            "unit_key",
            "execution_contract",
            "execution_contract_template_sha256",
            "execution_contract_sha256",
            "journal_identity_sha256",
            "events",
            "attempts",
        }
        or state.get("schema_version") != 3
        or state.get("run_id") != expected_run_id
        or state.get("unit_key") != expected_unit_key
        or not isinstance(state.get("attempts"), list)
    ):
        raise LiveControlError("attempt journal identity mismatch")
    contract = _validate_execution_contract_descriptor(
        state["execution_contract"]
    )
    contract_sha256 = contract["descriptor_sha256"]
    expected_identity = _journal_identity(
        run_id=expected_run_id,
        run_seed=contract["run_seed"],
        unit_key=expected_unit_key,
        execution_contract_template_sha256=contract["template_sha256"],
        execution_contract_sha256=contract_sha256,
    )
    if (
        contract.get("run_id") != expected_run_id
        or contract.get("unit_key") != expected_unit_key
        or state.get("run_seed") != contract["run_seed"]
        or state.get("execution_contract_template_sha256")
        != contract["template_sha256"]
        or state.get("execution_contract_sha256") != contract_sha256
        or state.get("journal_identity_sha256") != expected_identity
    ):
        raise LiveControlError("attempt journal execution contract mismatch")
    _validate_journal_attempts(state)
    _validate_event_chain(state)
    if require_terminal:
        final_attempt = state["attempts"][-1] if state["attempts"] else None
        if (
            not isinstance(final_attempt, dict)
            or final_attempt.get("phase") != "classified"
            or not isinstance(final_attempt.get("classification"), dict)
            or final_attempt["classification"].get("retryable") is not False
        ):
            raise LiveControlError("attempt journal is unresolved")
    return state


def validate_terminal_attempt_journal(
    raw: bytes,
    *,
    expected_run_id: str,
    expected_unit_key: str,
) -> dict:
    """Validate one canonical terminal journal for provenance finalization."""

    return _validate_attempt_journal_bytes(
        raw,
        expected_run_id=expected_run_id,
        expected_unit_key=expected_unit_key,
        require_terminal=True,
    )


class AttemptJournal:
    """Crash-safe state for one unit's reservation and dispatch attempts."""

    def __init__(
        self,
        path: Path,
        *,
        trusted_root: Path,
        execution_contract: ExecutionContract,
    ):
        self.path = Path(path)
        self.trusted_root = Path(trusted_root)
        self._execution_contract = execution_contract
        self._lock_local = threading.local()
        _trusted_path(
            self.path,
            trusted_root=self.trusted_root,
            label="attempt journal",
        )
        if any(
            not isinstance(value, str)
            or not value
            or "\n" in value
            or "\r" in value
            for value in (
                execution_contract.run_id,
                execution_contract.unit_key,
            )
        ):
            raise LiveControlError("attempt journal identity is malformed")
        contract_descriptor = _validate_execution_contract_descriptor(
            execution_contract.descriptor()
        )
        with self._unit_lock():
            if self.path.exists():
                self._state = self._read_state(execution_contract)
            else:
                self._state = {
                    "schema_version": 3,
                    "run_id": execution_contract.run_id,
                    "run_seed": execution_contract.run_seed,
                    "unit_key": execution_contract.unit_key,
                    "execution_contract": contract_descriptor,
                    "execution_contract_template_sha256": (
                        execution_contract.template_sha256
                    ),
                    "execution_contract_sha256": (
                        execution_contract.descriptor_sha256
                    ),
                    "journal_identity_sha256": _journal_identity(
                        run_id=execution_contract.run_id,
                        run_seed=execution_contract.run_seed,
                        unit_key=execution_contract.unit_key,
                        execution_contract_template_sha256=(
                            execution_contract.template_sha256
                        ),
                        execution_contract_sha256=(
                            execution_contract.descriptor_sha256
                        ),
                    ),
                    "events": [],
                    "attempts": [],
                }
                self._write()

    def _read_state(self, execution_contract: ExecutionContract) -> dict:
        self._assert_unit_lock_integrity()
        _trusted_path(
            self.path,
            trusted_root=self.trusted_root,
            label="attempt journal",
        )
        raw = _read_snapshot(
            self.path,
            trusted_root=self.trusted_root,
            label="attempt journal",
            require_single_link=True,
        )
        state = _validate_attempt_journal_bytes(
            raw,
            expected_run_id=execution_contract.run_id,
            expected_unit_key=execution_contract.unit_key,
            require_terminal=False,
        )
        expected_contract = execution_contract.descriptor()
        if (
            state.get("execution_contract") != expected_contract
            or state.get("execution_contract_sha256")
            != execution_contract.descriptor_sha256
        ):
            raise LiveControlError("attempt journal identity mismatch")
        return state

    def verify_execution_contract(
        self,
        execution_contract: ExecutionContract,
    ) -> None:
        if not self._unit_lock_is_active():
            with self._unit_lock():
                self._state = self._read_state(execution_contract)
            return
        self._state = self._read_state(execution_contract)

    def _unit_lock_is_active(self) -> bool:
        return getattr(self._lock_local, "descriptor", None) is not None

    def _assert_unit_lock_integrity(self) -> None:
        descriptor = getattr(self._lock_local, "descriptor", None)
        parent_descriptor = getattr(
            self._lock_local,
            "parent_descriptor",
            None,
        )
        name = getattr(self._lock_local, "name", None)
        if (
            not isinstance(descriptor, int)
            or not isinstance(parent_descriptor, int)
            or not isinstance(name, str)
        ):
            raise LiveControlError("attempt journal unit lock is not held")
        _verify_open_file_path(
            descriptor,
            parent_descriptor=parent_descriptor,
            name=name,
            label="attempt journal unit-lock file",
        )

    @contextmanager
    def _transition_lock(self):
        if self._unit_lock_is_active():
            self._state = self._read_state(self._execution_contract)
            yield
            return
        with self._unit_lock():
            self._state = self._read_state(self._execution_contract)
            yield

    @contextmanager
    def _unit_lock(self):
        root, root_identity, lock_name = _trusted_lock_identity(
            self.path,
            trusted_root=self.trusted_root,
            label="attempt journal",
        )
        directory_flags = (
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        flags = (
            os.O_CREAT
            | os.O_RDWR
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0)
        )
        root_descriptor = -1
        lock_directory_descriptor = -1
        descriptor = -1
        try:
            root_descriptor = os.open(root, directory_flags)
            root_state = os.fstat(root_descriptor)
            if (
                not stat.S_ISDIR(root_state.st_mode)
                or (root_state.st_dev, root_state.st_ino)
                != root_identity
            ):
                raise LiveControlError(
                    "attempt journal trusted root identity changed"
                )
            try:
                os.mkdir(
                    ".attempt-locks",
                    mode=0o700,
                    dir_fd=root_descriptor,
                )
            except FileExistsError:
                pass
            lock_directory_descriptor = os.open(
                ".attempt-locks",
                directory_flags,
                dir_fd=root_descriptor,
            )
            descriptor = os.open(
                f"{lock_name}.lock",
                flags,
                0o600,
                dir_fd=lock_directory_descriptor,
            )
            lock_file_name = f"{lock_name}.lock"
            _verify_open_file_path(
                descriptor,
                parent_descriptor=lock_directory_descriptor,
                name=lock_file_name,
                label="attempt journal unit-lock file",
            )
        except LiveControlError:
            if descriptor >= 0:
                os.close(descriptor)
            if lock_directory_descriptor >= 0:
                os.close(lock_directory_descriptor)
            if root_descriptor >= 0:
                os.close(root_descriptor)
            raise
        except OSError as exc:
            if descriptor >= 0:
                os.close(descriptor)
            if lock_directory_descriptor >= 0:
                os.close(lock_directory_descriptor)
            if root_descriptor >= 0:
                os.close(root_descriptor)
            raise LiveControlError("cannot open per-unit attempt lock") from exc
        try:
            os.fchmod(descriptor, 0o600)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            _verify_open_file_path(
                descriptor,
                parent_descriptor=lock_directory_descriptor,
                name=lock_file_name,
                label="attempt journal unit-lock file",
            )
            self._lock_local.descriptor = descriptor
            self._lock_local.parent_descriptor = (
                lock_directory_descriptor
            )
            self._lock_local.name = lock_file_name
            yield
        finally:
            self._lock_local.descriptor = None
            self._lock_local.parent_descriptor = None
            self._lock_local.name = None
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)
                os.close(lock_directory_descriptor)
                os.close(root_descriptor)

    @contextmanager
    def locked(self, execution_contract: ExecutionContract):
        with self._unit_lock():
            self.verify_execution_contract(execution_contract)
            yield

    @property
    def run_id(self) -> str:
        return self._state["run_id"]

    @property
    def unit_key(self) -> str:
        return self._state["unit_key"]

    @property
    def attempts(self) -> tuple[dict, ...]:
        return tuple(self._state["attempts"])

    def _write(self) -> None:
        self._assert_unit_lock_integrity()
        _validate_journal_attempts(self._state)
        _validate_event_chain(self._state)
        _atomic_write_trusted_json(
            self.path,
            self._state,
            trusted_root=self.trusted_root,
        )
        self._assert_unit_lock_integrity()

    def _append_event(self) -> None:
        attempt = self._state["attempts"][-1]
        previous = (
            self._state["events"][-1]["event_sha256"]
            if self._state["events"]
            else self._state["journal_identity_sha256"]
        )
        event = {
            "sequence": len(self._state["events"]) + 1,
            "previous_sha256": previous,
            "attempt_number": attempt["attempt_number"],
            "phase": attempt["phase"],
            "attempt": json.loads(canonical_json(attempt)),
        }
        event["event_sha256"] = _event_digest(
            self._state["journal_identity_sha256"],
            event,
        )
        self._state["events"].append(event)

    def record_reservation(
        self,
        request: BudgetRequest,
        reservation: BudgetReservation,
    ) -> None:
        with self._transition_lock():
            self._record_reservation_locked(request, reservation)

    def _record_reservation_locked(
        self,
        request: BudgetRequest,
        reservation: BudgetReservation,
    ) -> None:
        if request.attempt_number != len(self._state["attempts"]) + 1:
            raise LiveControlError("attempt reservation sequence is invalid")
        if self._state["attempts"]:
            prior = self._state["attempts"][-1]
            classification = prior.get("classification")
            receipt = prior.get("receipt")
            if (
                prior.get("phase") != "classified"
                or not isinstance(classification, dict)
                or classification
                != {
                    "value": "transient_error",
                    "retryable": True,
                }
                or not isinstance(receipt, dict)
                or receipt.get("status") != "error"
                or receipt.get("error_class")
                not in LIVE_RETRYABLE_ERROR_CLASSES
            ):
                raise LiveControlError(
                    "attempt journal cannot continue after a terminal result"
                )
        self._state["attempts"].append(
            {
                "attempt_number": request.attempt_number,
                "idempotency_key": request.idempotency_key,
                "phase": "reserved",
                "transitions": ["reserved"],
                "request": {
                    "max_unit_usd": _decimal_6(
                        request.max_unit_usd,
                        "request max_unit_usd",
                        positive=True,
                    ),
                    "max_total_usd": _decimal_6(
                        request.max_total_usd,
                        "request max_total_usd",
                        positive=True,
                    ),
                },
                "reservation": {
                    "reservation_id": reservation.reservation_id,
                    "authorized_usd": _decimal_6(
                        reservation.authorized_usd,
                        "reservation authorized_usd",
                        positive=False,
                    ),
                },
            }
        )
        self._append_event()
        self._write()

    def record_dispatching(
        self,
        *,
        descriptor_sha256: str,
    ) -> None:
        with self._transition_lock():
            self._record_dispatching_locked(
                descriptor_sha256=descriptor_sha256,
            )

    def _record_dispatching_locked(
        self,
        *,
        descriptor_sha256: str,
    ) -> None:
        attempt = self._state["attempts"][-1]
        if attempt["phase"] != "reserved" or SHA256.fullmatch(
            descriptor_sha256
        ) is None:
            raise LiveControlError("attempt cannot transition to dispatching")
        attempt["phase"] = "dispatching"
        attempt["transitions"].append("dispatching")
        attempt["descriptor_sha256"] = descriptor_sha256
        self._append_event()
        self._write()

    def record_receipt(self, receipt: DispatchReceipt) -> None:
        with self._transition_lock():
            self._record_receipt_locked(receipt)

    def _record_receipt_locked(self, receipt: DispatchReceipt) -> None:
        attempt = self._state["attempts"][-1]
        if (
            attempt["phase"] != "dispatching"
            or receipt.idempotency_key != attempt["idempotency_key"]
        ):
            raise LiveControlError("receipt does not bind the dispatching attempt")
        attempt["phase"] = "receipt"
        attempt["transitions"].append("receipt")
        attempt["receipt"] = {
            "operation_id": receipt.operation_id,
            "status": receipt.status,
            "cost_usd": _decimal_6(
                receipt.cost_usd,
                "receipt cost_usd",
                positive=False,
            ),
            "response_sha256": receipt.response_sha256,
            "error_class": receipt.error_class,
        }
        self._append_event()
        self._write()

    def record_classification(
        self,
        *,
        classification: str,
        retryable: bool,
    ) -> AttemptOutcome:
        with self._transition_lock():
            return self._record_classification_locked(
                classification=classification,
                retryable=retryable,
            )

    def _record_classification_locked(
        self,
        *,
        classification: str,
        retryable: bool,
    ) -> AttemptOutcome:
        attempt = self._state["attempts"][-1]
        if attempt["phase"] != "receipt":
            raise LiveControlError("attempt has no receipt to classify")
        attempt["phase"] = "classified"
        attempt["transitions"].append("classified")
        attempt["classification"] = {
            "value": classification,
            "retryable": retryable,
        }
        self._append_event()
        self._write()
        return AttemptOutcome(
            attempt_number=attempt["attempt_number"],
            idempotency_key=attempt["idempotency_key"],
            phase="classified",
            classification=classification,
            retryable=retryable,
            cost_usd=_cumulative_cost(tuple(self._state["attempts"])),
        )


def _attempt_idempotency_key(
    run_id: str,
    unit_key: str,
    attempt_number: int,
) -> str:
    return hashlib.sha256(
        f"{run_id}|{unit_key}|attempt-{attempt_number}".encode()
    ).hexdigest()


def _reservation_from_attempt(attempt: dict) -> BudgetReservation:
    reservation = attempt.get("reservation")
    if not isinstance(reservation, dict):
        raise LiveControlError("durable reservation is malformed")
    try:
        authorized = Decimal(reservation["authorized_usd"])
    except (KeyError, InvalidOperation, TypeError) as exc:
        raise LiveControlError("durable reservation is malformed") from exc
    return BudgetReservation(
        reservation_id=reservation.get("reservation_id"),
        idempotency_key=attempt.get("idempotency_key"),
        authorized_usd=authorized,
    )


def _receipt_from_attempt(attempt: dict) -> DispatchReceipt:
    receipt = attempt.get("receipt")
    if not isinstance(receipt, dict):
        raise LiveControlError("durable receipt is malformed")
    try:
        cost = Decimal(receipt["cost_usd"])
    except (KeyError, InvalidOperation, TypeError) as exc:
        raise LiveControlError("durable receipt is malformed") from exc
    return DispatchReceipt(
        operation_id=receipt.get("operation_id"),
        idempotency_key=attempt.get("idempotency_key"),
        status=receipt.get("status"),
        cost_usd=cost,
        response_sha256=receipt.get("response_sha256"),
        error_class=receipt.get("error_class"),
    )


def _validate_reservation(
    reservation: object,
    *,
    idempotency_key: str,
    controls: FrozenControls,
) -> BudgetReservation:
    if not isinstance(reservation, BudgetReservation):
        raise LiveControlError("budget reservation violates the frozen controls")
    authorized_usd = _canonical_decimal_6(
        reservation.authorized_usd,
        "reservation authorized_usd",
        positive=False,
    )
    if (
        reservation.idempotency_key != idempotency_key
        or not isinstance(reservation.reservation_id, str)
        or not reservation.reservation_id
        or authorized_usd > controls.max_unit_usd
        or authorized_usd > controls.max_total_usd
    ):
        raise LiveControlError("budget reservation violates the frozen controls")
    return BudgetReservation(
        reservation_id=reservation.reservation_id,
        idempotency_key=reservation.idempotency_key,
        authorized_usd=authorized_usd,
    )


def _validate_receipt(
    receipt: object,
    *,
    idempotency_key: str,
    reservation: BudgetReservation,
    controls: FrozenControls,
) -> DispatchReceipt:
    if not isinstance(receipt, DispatchReceipt):
        raise LiveControlError("executor receipt violates the frozen contract")
    cost_usd = _canonical_decimal_6(
        receipt.cost_usd,
        "receipt cost_usd",
        positive=False,
    )
    common_invalid = (
        receipt.idempotency_key != idempotency_key
        or not isinstance(receipt.operation_id, str)
        or not receipt.operation_id
        or cost_usd > reservation.authorized_usd
        or cost_usd > controls.max_unit_usd
        or cost_usd > controls.max_total_usd
    )
    if receipt.status == "ok":
        status_invalid = (
            receipt.response_sha256 is None
            or SHA256.fullmatch(receipt.response_sha256) is None
            or receipt.error_class is not None
        )
    elif receipt.status == "error":
        allowed_errors = (
            set(LIVE_RETRYABLE_ERROR_CLASSES)
            | NONFINALIZABLE_ERROR_CLASSES
            | MEASURED_OUTCOME_ERROR_CLASSES
        )
        status_invalid = (
            receipt.response_sha256 is not None
            or receipt.error_class not in allowed_errors
        )
    else:
        status_invalid = True
    if common_invalid or status_invalid:
        raise LiveControlError("executor receipt violates the frozen contract")
    return DispatchReceipt(
        operation_id=receipt.operation_id,
        idempotency_key=receipt.idempotency_key,
        status=receipt.status,
        cost_usd=cost_usd,
        response_sha256=receipt.response_sha256,
        error_class=receipt.error_class,
    )


def _classified_outcome(
    attempt: dict,
    *,
    attempts: tuple[dict, ...],
) -> AttemptOutcome:
    classification = attempt.get("classification")
    if (
        attempt.get("phase") != "classified"
        or not isinstance(classification, dict)
        or not isinstance(classification.get("value"), str)
        or not isinstance(classification.get("retryable"), bool)
    ):
        raise LiveControlError("durable classification is malformed")
    return AttemptOutcome(
        attempt_number=attempt["attempt_number"],
        idempotency_key=attempt["idempotency_key"],
        phase="classified",
        classification=classification["value"],
        retryable=classification["retryable"],
        cost_usd=_cumulative_cost(attempts),
    )


def _classify_receipt(
    receipt: DispatchReceipt,
    *,
    attempt_number: int,
    controls: FrozenControls,
) -> tuple[str, bool]:
    if receipt.status == "ok":
        return "success", False
    if receipt.error_class in LIVE_RETRYABLE_ERROR_CLASSES:
        return (
            "transient_error",
            attempt_number < controls.max_attempts,
        )
    if receipt.error_class in NONFINALIZABLE_ERROR_CLASSES:
        return "fatal_error", False
    return "measured_error", False


def _cumulative_cost(attempts: tuple[dict, ...]) -> Decimal:
    return _decimal_from_microdollars(
        _cumulative_cost_micros(attempts),
        "cumulative receipt cost",
        positive=False,
    )


def _cumulative_cost_micros(attempts: tuple[dict, ...]) -> int:
    total_micros = 0
    for attempt in attempts:
        if "receipt" in attempt:
            total_micros += _microdollars(
                _receipt_from_attempt(attempt).cost_usd,
                "receipt cost_usd",
                positive=False,
            )
    return total_micros


def _reconcile_resumed_receipt(
    *,
    journal: AttemptJournal,
    execution_contract: ExecutionContract,
    authority_boundary: AuthorityBoundary,
    invocation: ClaudeInvocation,
    child_environments: ChildEnvironments,
    controls: FrozenControls,
    root: Path,
    broker: BudgetBroker,
    idempotency_key: str,
    reservation: BudgetReservation,
    durable_receipt: DispatchReceipt,
) -> DispatchReceipt:
    execution_contract.verify_runtime(
        controls=controls,
        child_environments=child_environments,
        invocation=invocation,
        root=root,
    )
    journal.verify_execution_contract(execution_contract)
    authority_boundary.validate(execution_contract, controls=controls)
    reconciliation = broker.reconcile(idempotency_key=idempotency_key)
    if (
        not isinstance(reconciliation, DispatchReconciliation)
        or reconciliation.state
        not in {"settled", "not_dispatched", "unknown"}
        or (
            reconciliation.state == "settled"
            and reconciliation.receipt is None
        )
        or (
            reconciliation.state != "settled"
            and reconciliation.receipt is not None
        )
    ):
        raise LiveControlError("dispatch reconciliation is malformed")
    if reconciliation.state == "unknown":
        raise UnresolvedDispatchError(
            "resumed success remains unresolved; acceptance is forbidden"
        )
    if reconciliation.state != "settled":
        raise LiveControlError(
            "durable success conflicts with trusted broker reconciliation"
        )
    reconciled = _validate_receipt(
        reconciliation.receipt,
        idempotency_key=idempotency_key,
        reservation=reservation,
        controls=controls,
    )
    if reconciled != durable_receipt:
        raise LiveControlError(
            "durable receipt differs from trusted broker reconciliation"
        )
    authority_boundary.validate(execution_contract, controls=controls)
    return reconciled


def _reserve_next_attempt(
    *,
    journal: AttemptJournal,
    execution_contract: ExecutionContract,
    authority_boundary: AuthorityBoundary,
    invocation: ClaudeInvocation,
    child_environments: ChildEnvironments,
    controls: FrozenControls,
    root: Path,
    broker: BudgetBroker,
) -> None:
    execution_contract.verify_runtime(
        controls=controls,
        child_environments=child_environments,
        invocation=invocation,
        root=root,
    )
    journal.verify_execution_contract(execution_contract)
    attempts = journal.attempts
    attempt_number = len(attempts) + 1
    if attempt_number > controls.max_attempts:
        raise LiveControlError("retry count exceeds the frozen attempt limit")
    cumulative_cost_micros = _cumulative_cost_micros(attempts)
    remaining_unit_micros = _microdollars(
        controls.max_unit_usd,
        "frozen max_unit_usd",
        positive=True,
    ) - cumulative_cost_micros
    remaining_total_micros = _microdollars(
        controls.max_total_usd,
        "frozen max_total_usd",
        positive=True,
    ) - cumulative_cost_micros
    if remaining_unit_micros <= 0 or remaining_total_micros <= 0:
        raise BudgetCapError("no cumulative budget remains for another attempt")
    remaining_unit = _decimal_from_microdollars(
        remaining_unit_micros,
        "remaining unit budget",
        positive=True,
    )
    remaining_total = _decimal_from_microdollars(
        remaining_total_micros,
        "remaining total budget",
        positive=True,
    )
    idempotency_key = _attempt_idempotency_key(
        journal.run_id,
        journal.unit_key,
        attempt_number,
    )
    request = BudgetRequest(
        run_id=journal.run_id,
        unit_key=journal.unit_key,
        attempt_number=attempt_number,
        idempotency_key=idempotency_key,
        max_unit_usd=remaining_unit,
        max_total_usd=remaining_total,
    )
    authority_boundary.validate(execution_contract, controls=controls)
    reservation = _validate_reservation(
        broker.reserve(request),
        idempotency_key=idempotency_key,
        controls=controls,
    )
    if reservation.authorized_usd > remaining_unit:
        raise BudgetCapError("reservation exceeds the cumulative unit cap")
    journal.record_reservation(request, reservation)


def _run_attempt_lifecycle_locked(
    *,
    journal: AttemptJournal,
    execution_contract: ExecutionContract,
    authority_boundary: AuthorityBoundary,
    invocation: ClaudeInvocation,
    child_environments: ChildEnvironments,
    controls: FrozenControls,
    root: Path,
    broker: BudgetBroker,
    executor: LiveExecutor,
) -> AttemptOutcome:
    """Run one lifecycle while the caller holds its per-unit lock."""
    execution_contract.verify_runtime(
        controls=controls,
        child_environments=child_environments,
        invocation=invocation,
        root=root,
    )
    journal.verify_execution_contract(execution_contract)
    unit_parts = journal.unit_key.rsplit("|", 1)
    if (
        len(unit_parts) != 2
        or unit_parts[1] != invocation.arm
        or execution_contract.run_id != journal.run_id
        or execution_contract.unit_key != journal.unit_key
    ):
        raise LiveControlError("attempt journal arm does not match the invocation")
    while True:
        attempts = journal.attempts
        if not attempts:
            _reserve_next_attempt(
                journal=journal,
                execution_contract=execution_contract,
                authority_boundary=authority_boundary,
                invocation=invocation,
                child_environments=child_environments,
                controls=controls,
                root=root,
                broker=broker,
            )
            attempts = journal.attempts
        attempt = attempts[-1]
        phase = attempt.get("phase")
        idempotency_key = attempt.get("idempotency_key")
        if not isinstance(idempotency_key, str) or SHA256.fullmatch(
            idempotency_key
        ) is None:
            raise LiveControlError("durable attempt idempotency key is malformed")
        reservation = _validate_reservation(
            _reservation_from_attempt(attempt),
            idempotency_key=idempotency_key,
            controls=controls,
        )
        if phase == "classified":
            outcome = _classified_outcome(
                attempt,
                attempts=attempts,
            )
            if not outcome.retryable:
                if outcome.classification == "success":
                    _reconcile_resumed_receipt(
                        journal=journal,
                        execution_contract=execution_contract,
                        authority_boundary=authority_boundary,
                        invocation=invocation,
                        child_environments=child_environments,
                        controls=controls,
                        root=root,
                        broker=broker,
                        idempotency_key=idempotency_key,
                        reservation=reservation,
                        durable_receipt=_receipt_from_attempt(attempt),
                    )
                return outcome
            _reserve_next_attempt(
                journal=journal,
                execution_contract=execution_contract,
                authority_boundary=authority_boundary,
                invocation=invocation,
                child_environments=child_environments,
                controls=controls,
                root=root,
                broker=broker,
            )
            continue
        if phase == "receipt":
            durable_receipt = _validate_receipt(
                _receipt_from_attempt(attempt),
                idempotency_key=idempotency_key,
                reservation=reservation,
                controls=controls,
            )
            receipt = _reconcile_resumed_receipt(
                journal=journal,
                execution_contract=execution_contract,
                authority_boundary=authority_boundary,
                invocation=invocation,
                child_environments=child_environments,
                controls=controls,
                root=root,
                broker=broker,
                idempotency_key=idempotency_key,
                reservation=reservation,
                durable_receipt=durable_receipt,
            )
        else:
            should_dispatch = False
            if phase == "reserved":
                journal.record_dispatching(
                    descriptor_sha256=invocation.descriptor_sha256,
                )
                should_dispatch = True
            elif phase == "dispatching":
                if attempt.get("descriptor_sha256") != invocation.descriptor_sha256:
                    raise LiveControlError("resumed invocation descriptor mismatch")
                execution_contract.verify_runtime(
                    controls=controls,
                    child_environments=child_environments,
                    invocation=invocation,
                    root=root,
                )
                journal.verify_execution_contract(execution_contract)
                authority_boundary.validate(
                    execution_contract,
                    controls=controls,
                )
                reconciliation = broker.reconcile(
                    idempotency_key=idempotency_key
                )
                if (
                    not isinstance(reconciliation, DispatchReconciliation)
                    or reconciliation.state
                    not in {"settled", "not_dispatched", "unknown"}
                    or (
                        reconciliation.state == "settled"
                        and reconciliation.receipt is None
                    )
                    or (
                        reconciliation.state != "settled"
                        and reconciliation.receipt is not None
                    )
                ):
                    raise LiveControlError("dispatch reconciliation is malformed")
                if reconciliation.state == "unknown":
                    raise UnresolvedDispatchError(
                        "dispatch remains unresolved; retry is forbidden"
                    )
                if reconciliation.state == "settled":
                    raise UnresolvedDispatchError(
                        "settled dispatch lacks durable adapter-completion "
                        "evidence; terminal acceptance is forbidden"
                    )
                else:
                    should_dispatch = True
            else:
                raise LiveControlError("durable attempt phase is malformed")
            if should_dispatch:
                execution_contract.verify_runtime(
                    controls=controls,
                    child_environments=child_environments,
                    invocation=invocation,
                    root=root,
                )
                journal.verify_execution_contract(execution_contract)
                enforcement = OnlineLimitGuard(
                    invocation.adapter_enforcement
                )
                launch_environment = (
                    child_environments.model_launch_environment()
                )
                authority_boundary.validate(
                    execution_contract,
                    controls=controls,
                )
                child_environments.revalidate_isolation()
                dispatched = executor.dispatch(
                    invocation=invocation,
                    launch_environment=launch_environment,
                    enforcement=enforcement,
                    reservation=reservation,
                    idempotency_key=idempotency_key,
                )
                enforcement.verify_complete()
                authority_boundary.validate(
                    execution_contract,
                    controls=controls,
                )
                receipt = _validate_receipt(
                    dispatched,
                    idempotency_key=idempotency_key,
                    reservation=reservation,
                    controls=controls,
                )
                journal.record_receipt(receipt)
        classification, retryable = _classify_receipt(
            receipt,
            attempt_number=attempt["attempt_number"],
            controls=controls,
        )
        outcome = journal.record_classification(
            classification=classification,
            retryable=retryable,
        )
        if not outcome.retryable:
            return outcome


def run_attempt_lifecycle(
    *,
    journal: AttemptJournal,
    execution_contract: ExecutionContract,
    authority_boundary: AuthorityBoundary,
    invocation: ClaudeInvocation,
    child_environments: ChildEnvironments,
    controls: FrozenControls,
    root: Path,
    broker: BudgetBroker,
    executor: LiveExecutor,
) -> AttemptOutcome:
    """Serialize one unit, re-read it under lock, then execute its lifecycle."""
    with journal.locked(execution_contract):
        return _run_attempt_lifecycle_locked(
            journal=journal,
            execution_contract=execution_contract,
            authority_boundary=authority_boundary,
            invocation=invocation,
            child_environments=child_environments,
            controls=controls,
            root=root,
            broker=broker,
            executor=executor,
        )
