"""Crash-visible append-only ledgers and content-addressed run bundles."""

from __future__ import annotations

import hashlib
import json
import os
import signal
import stat
import tempfile
from collections.abc import Iterable, Mapping
from pathlib import Path

from .schema import (
    NONFINALIZABLE_ERROR_CLASSES,
    ContractError,
    canonical_json,
    usd_decimal_from_micros,
    usd_micros,
)

LEDGER_NAMES = (
    "cases.jsonl",
    "setup.jsonl",
    "observations.jsonl",
    "errors.jsonl",
)
AUTHORITATIVE_CONSUMER = "bench.compare.score:score_bundle_v1"
SHA256_HEX = frozenset("0123456789abcdef")
ATTEMPT_JOURNAL_POLICY = (
    "required_terminal_precommitted_per_expected_unit_v2"
)
LIVE_RUN_SEED_DOMAIN = "bench.compare.live-run-seed.v1"


class ProvenanceError(ValueError):
    """A run artifact is incomplete, corrupted, duplicated, or inconsistent."""


def _file_lock_module():
    try:
        import fcntl
    except (ImportError, OSError) as exc:
        raise ProvenanceError(
            "provenance ledger mutation requires POSIX fcntl locking; "
            "no safe file-lock implementation is available on this platform"
        ) from exc
    if (
        not callable(getattr(fcntl, "flock", None))
        or not isinstance(getattr(fcntl, "LOCK_EX", None), int)
        or not isinstance(getattr(fcntl, "LOCK_UN", None), int)
    ):
        raise ProvenanceError(
            "provenance ledger mutation requires POSIX fcntl locking; "
            "the platform file-lock implementation is incomplete"
        )
    return fcntl


def _acquire_file_lock(fcntl, descriptor: int) -> None:
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
    except (AttributeError, OSError, NotImplementedError) as exc:
        raise ProvenanceError(
            "provenance ledger locking is unavailable on this platform"
        ) from exc


def _serialized_usd_micros(value: object, *, label: str) -> int:
    try:
        return usd_micros(
            value,
            label,
            positive=False,
            serialized=True,
        )
    except ContractError as exc:
        raise ProvenanceError(f"{label} is malformed") from exc


def _format_usd_micros(micros: int, *, label: str) -> str:
    try:
        value = usd_decimal_from_micros(
            micros,
            label,
            positive=False,
        )
    except ContractError as exc:
        raise ProvenanceError(f"{label} is malformed") from exc
    return format(value, "f")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise ProvenanceError(f"cannot hash {path}: {exc}") from exc


def _load_json_file(path: Path, context: str) -> object:
    try:
        snapshot = path.read_bytes()
    except OSError as exc:
        raise ProvenanceError(f"{context} is invalid: {exc}") from exc
    try:
        text = snapshot.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProvenanceError(
            f"{context} is invalid: malformed UTF-8 input"
        ) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProvenanceError(f"{context} is invalid: {exc}") from exc


def _read_unique_regular_file(path: Path, *, label: str) -> bytes:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            raise ProvenanceError(
                f"{label} must be a single-link regular file"
            )
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 65_536)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        current = os.stat(path, follow_symlinks=False)
    except ProvenanceError:
        raise
    except OSError as exc:
        raise ProvenanceError(f"{label} is unreadable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    raw = b"".join(chunks)
    if (
        not stat.S_ISREG(after.st_mode)
        or after.st_nlink != 1
        or not stat.S_ISREG(current.st_mode)
        or current.st_nlink != 1
        or (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        or (after.st_dev, after.st_ino)
        != (current.st_dev, current.st_ino)
        or len(raw) != after.st_size
    ):
        raise ProvenanceError(f"{label} changed while it was read")
    return raw


def final_result_id(run_id: str, artifacts: dict) -> str:
    return sha256_bytes(
        canonical_json(
            {
                "schema_version": 1,
                "run_id": run_id,
                "artifacts": artifacts,
            }
        )
    )


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_json(path: Path, value: dict, *, replace: bool = True) -> None:
    if path.is_symlink():
        raise ProvenanceError(f"refusing symlink artifact path: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = canonical_json(value) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        offset = 0
        while offset < len(encoded):
            offset += os.write(descriptor, encoded[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        if not replace and path.exists():
            raise ProvenanceError(f"artifact already exists: {path}")
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


class AppendOnlyLedger:
    """A stable-key JSONL ledger that verifies every record before resuming."""

    def __init__(self, path: Path, *, seal_path: Path | None = None):
        self.path = Path(path)
        self.pending_path = self.path.with_name(f".{self.path.name}.pending")
        self.seal_path = Path(seal_path) if seal_path is not None else None
        if self.path.is_symlink() or self.pending_path.is_symlink():
            raise ProvenanceError(f"refusing symlink ledger: {self.path}")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            descriptor = os.open(
                self.path,
                os.O_CREAT | os.O_APPEND | os.O_WRONLY,
                0o600,
            )
        except OSError as exc:
            raise ProvenanceError(f"cannot create ledger {self.path}: {exc}") from exc
        try:
            os.fchmod(descriptor, 0o600)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(self.path.parent)
        self.recovered_tail_bytes = 0
        self.records = self._load(
            recover_incomplete_tail=not (
                self.seal_path is not None and self.seal_path.exists()
            )
        )

    @staticmethod
    def _with_digest(record: dict) -> dict:
        if not isinstance(record, dict):
            raise ProvenanceError("ledger record must be an object")
        if "record_sha256" in record:
            raise ProvenanceError("caller may not supply record_sha256")
        stable_key = record.get("stable_key")
        if (
            not isinstance(stable_key, str)
            or not stable_key
            or "\n" in stable_key
            or "\r" in stable_key
        ):
            raise ProvenanceError("ledger stable_key must be a nonempty single line")
        stored = dict(record)
        stored["record_sha256"] = sha256_bytes(canonical_json(record))
        return stored

    @staticmethod
    def _read_descriptor(descriptor: int) -> bytes:
        os.lseek(descriptor, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)

    def _load_pending_append(self) -> tuple[int, bytes]:
        if self.pending_path.is_symlink():
            raise ProvenanceError(
                f"{self.path}: refusing symlink pending-append marker"
            )
        try:
            pending = json.loads(self.pending_path.read_bytes())
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProvenanceError(
                f"{self.path}: pending-append marker is corrupt"
            ) from exc
        required = {
            "schema_version",
            "ledger",
            "prefix_size",
            "record_size",
            "record_sha256",
            "record_hex",
        }
        if (
            not isinstance(pending, dict)
            or set(pending) != required
            or pending.get("schema_version") != 1
            or pending.get("ledger") != self.path.name
            or isinstance(pending.get("prefix_size"), bool)
            or not isinstance(pending.get("prefix_size"), int)
            or pending["prefix_size"] < 0
            or isinstance(pending.get("record_size"), bool)
            or not isinstance(pending.get("record_size"), int)
            or pending["record_size"] < 1
            or not isinstance(pending.get("record_sha256"), str)
            or not isinstance(pending.get("record_hex"), str)
        ):
            raise ProvenanceError(
                f"{self.path}: pending-append marker is malformed"
            )
        try:
            encoded = bytes.fromhex(pending["record_hex"])
        except ValueError as exc:
            raise ProvenanceError(
                f"{self.path}: pending-append record is malformed"
            ) from exc
        if (
            len(encoded) != pending["record_size"]
            or sha256_bytes(encoded) != pending["record_sha256"]
            or not encoded.endswith(b"\n")
            or encoded.count(b"\n") != 1
        ):
            raise ProvenanceError(
                f"{self.path}: pending-append record digest mismatch"
            )
        return pending["prefix_size"], encoded

    def _recover_incomplete_tail(self) -> bytes:
        fcntl = _file_lock_module()
        try:
            descriptor = os.open(self.path, os.O_RDWR)
        except OSError as exc:
            raise ProvenanceError(
                f"cannot recover interrupted ledger {self.path}: {exc}"
            ) from exc
        try:
            _acquire_file_lock(fcntl, descriptor)
            data = self._read_descriptor(descriptor)
            if self.pending_path.exists():
                prefix_end, expected_record = self._load_pending_append()
                if (
                    prefix_end > len(data)
                    or (prefix_end > 0 and data[prefix_end - 1 : prefix_end] != b"\n")
                ):
                    raise ProvenanceError(
                        f"{self.path}: pending-append prefix is invalid"
                    )
                prefix = data[:prefix_end]
                tail = data[prefix_end:]
                self._parse_records(prefix)
                if tail == expected_record:
                    self._parse_records(data)
                    recovered = data
                elif (
                    len(tail) < len(expected_record)
                    and expected_record.startswith(tail)
                ):
                    os.ftruncate(descriptor, prefix_end)
                    self.recovered_tail_bytes = len(tail)
                    recovered = prefix
                else:
                    raise ProvenanceError(
                        f"{self.path}: pending append differs from durable tail"
                    )
                os.fsync(descriptor)
                try:
                    self.pending_path.unlink()
                except OSError as exc:
                    raise ProvenanceError(
                        f"{self.path}: cannot clear recovered append marker"
                    ) from exc
                _fsync_directory(self.path.parent)
                return recovered
            if not data or data.endswith(b"\n"):
                return data
            prefix_end = data.rfind(b"\n") + 1
            prefix = data[:prefix_end]
            tail = data[prefix_end:]
            # Validate every completed record before changing the file. A bad
            # completed line is corruption, not a crash tail.
            self._parse_records(prefix)
            try:
                json.loads(tail)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise ProvenanceError(
                    f"{self.path}: ambiguous trailing record has no "
                    "durable pending-append marker"
                )
            else:
                # The JSON payload reached disk in full and only its newline
                # was interrupted. Validate its digest/key before preserving
                # it; a complete tampered object is never silently erased.
                recovered = prefix + tail + b"\n"
                self._parse_records(recovered)
                os.lseek(descriptor, 0, os.SEEK_END)
                written = os.write(descriptor, b"\n")
                if written != 1:
                    raise ProvenanceError("cannot repair missing ledger newline")
            os.fsync(descriptor)
            return recovered
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except (AttributeError, OSError, NotImplementedError):
                pass
            os.close(descriptor)

    def _load(
        self,
        *,
        recover_incomplete_tail: bool = False,
    ) -> dict[str, dict]:
        try:
            data = self.path.read_bytes()
        except OSError as exc:
            raise ProvenanceError(f"cannot read ledger {self.path}: {exc}") from exc
        pending_exists = self.pending_path.exists() or self.pending_path.is_symlink()
        if pending_exists:
            if not recover_incomplete_tail:
                raise ProvenanceError(f"{self.path}: interrupted pending append")
            data = self._recover_incomplete_tail()
        elif data and not data.endswith(b"\n"):
            if not recover_incomplete_tail:
                raise ProvenanceError(f"{self.path}: interrupted trailing record")
            data = self._recover_incomplete_tail()
        if not data:
            return {}
        return self._parse_records(data)

    def _parse_records(self, data: bytes) -> dict[str, dict]:
        records: dict[str, dict] = {}
        for line_number, encoded in enumerate(data.splitlines(), 1):
            if not encoded:
                raise ProvenanceError(f"{self.path}:{line_number}: blank record")
            try:
                record = json.loads(encoded)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ProvenanceError(
                    f"{self.path}:{line_number}: invalid JSON: {exc}"
                ) from exc
            if not isinstance(record, dict):
                raise ProvenanceError(
                    f"{self.path}:{line_number}: record must be an object"
                )
            expected = record.get("record_sha256")
            payload = dict(record)
            payload.pop("record_sha256", None)
            if expected != sha256_bytes(canonical_json(payload)):
                raise ProvenanceError(
                    f"{self.path}:{line_number}: record SHA mismatch"
                )
            stable_key = record.get("stable_key")
            if not isinstance(stable_key, str) or not stable_key:
                raise ProvenanceError(
                    f"{self.path}:{line_number}: invalid stable_key"
                )
            if stable_key in records:
                raise ProvenanceError(
                    f"{self.path}:{line_number}: duplicate stable key {stable_key}"
                )
            records[stable_key] = record
        return records

    def append(
        self,
        record: dict,
        *,
        _fault_after_bytes: int | None = None,
    ) -> bool:
        fcntl = _file_lock_module()
        stored = self._with_digest(record)
        stable_key = stored["stable_key"]
        encoded = canonical_json(stored) + b"\n"
        if _fault_after_bytes is not None and (
            isinstance(_fault_after_bytes, bool)
            or _fault_after_bytes < 1
            or _fault_after_bytes >= len(encoded)
        ):
            raise ProvenanceError("fault byte offset must split a record")
        try:
            descriptor = os.open(self.path, os.O_APPEND | os.O_RDWR)
        except OSError as exc:
            raise ProvenanceError(f"cannot append {self.path}: {exc}") from exc
        try:
            _acquire_file_lock(fcntl, descriptor)
            data = self._read_descriptor(descriptor)
            if data and not data.endswith(b"\n"):
                raise ProvenanceError(f"{self.path}: interrupted trailing record")
            self.records = self._parse_records(data)
            existing = self.records.get(stable_key)
            if existing is not None:
                if canonical_json(existing) == canonical_json(stored):
                    return False
                raise ProvenanceError(
                    f"{self.path}: conflicting record for stable key {stable_key}"
                )
            if self.seal_path is not None and self.seal_path.exists():
                raise ProvenanceError(
                    f"{self.path}: finalized run is sealed against new records"
                )
            if self.pending_path.exists() or self.pending_path.is_symlink():
                raise ProvenanceError(
                    f"{self.path}: unresolved pending append requires reopening"
                )
            atomic_write_json(
                self.pending_path,
                {
                    "schema_version": 1,
                    "ledger": self.path.name,
                    "prefix_size": len(data),
                    "record_size": len(encoded),
                    "record_sha256": sha256_bytes(encoded),
                    "record_hex": encoded.hex(),
                },
                replace=False,
            )
            if _fault_after_bytes is not None:
                written = os.write(descriptor, encoded[:_fault_after_bytes])
                if written != _fault_after_bytes:
                    raise ProvenanceError("fault injection short write")
                os.fsync(descriptor)
                os.kill(os.getpid(), signal.SIGKILL)
            offset = 0
            while offset < len(encoded):
                offset += os.write(descriptor, encoded[offset:])
            os.fsync(descriptor)
            try:
                self.pending_path.unlink()
            except OSError as exc:
                raise ProvenanceError(
                    f"{self.path}: cannot clear committed append marker"
                ) from exc
            _fsync_directory(self.path.parent)
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            except (AttributeError, OSError, NotImplementedError):
                pass
            os.close(descriptor)
        self.records[stable_key] = stored
        return True


def _validated_keys(name: str, values: Iterable[str]) -> tuple[str, ...]:
    keys = tuple(values)
    if (
        not keys
        or len(set(keys)) != len(keys)
        or any(not isinstance(key, str) or not key for key in keys)
    ):
        raise ProvenanceError(f"{name} must contain unique nonempty stable keys")
    return keys


def _is_sha256(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and set(value) <= SHA256_HEX
    )


def _attempt_journal_relative_path(unit_key: str) -> str:
    digest = sha256_bytes(unit_key.encode("utf-8"))
    return f"attempt-journals/{digest}.json"


def _execution_contract_sha256(
    *,
    run_id: str,
    run_seed: str,
    unit_key: str,
    template_sha256: str,
) -> str:
    from .live_runtime import execution_contract_identity_sha256

    return execution_contract_identity_sha256(
        run_id=run_id,
        run_seed=run_seed,
        unit_key=unit_key,
        template_sha256=template_sha256,
    )


def _validated_execution_contract_templates(
    *,
    expected_unit_keys: tuple[str, ...],
    expected_templates: Mapping[str, dict] | None,
    run_seed: str,
) -> dict[str, dict]:
    if not isinstance(expected_templates, Mapping):
        raise ProvenanceError(
            "expected execution contract templates are required"
        )
    expected_keys = set(expected_unit_keys)
    if set(expected_templates) != expected_keys:
        raise ProvenanceError(
            "execution contract templates must bind every expected unit"
        )
    normalized: dict[str, dict] = {}
    from .live_runtime import (
        LiveControlError,
        validate_execution_contract_template_descriptor,
    )

    for unit_key in expected_unit_keys:
        template = expected_templates.get(unit_key)
        if not isinstance(template, dict):
            raise ProvenanceError(
                f"execution contract template is malformed: {unit_key}"
            )
        try:
            safe_template = json.loads(canonical_json(template))
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise ProvenanceError(
                f"execution contract template is not canonical: {unit_key}"
            ) from exc
        try:
            validate_execution_contract_template_descriptor(safe_template)
        except LiveControlError as exc:
            raise ProvenanceError(
                f"execution contract template is invalid: {unit_key}"
            ) from exc
        if (
            safe_template.get("unit_key") != unit_key
            or safe_template.get("run_seed") != run_seed
        ):
            raise ProvenanceError(
                f"execution contract template identity mismatch: {unit_key}"
            )
        if safe_template["expected_units"] != len(expected_unit_keys):
            raise ProvenanceError(
                "execution contract template expected unit count "
                f"mismatch: {unit_key}"
            )
        normalized[unit_key] = safe_template
    if (
        len(
            {
                template["run_fingerprint_sha256"]
                for template in normalized.values()
            }
        )
        != 1
    ):
        raise ProvenanceError(
            "execution contract templates have mixed run-scoped fingerprints"
        )
    return normalized


def _live_run_seed(payload: dict) -> str:
    paths_by_unit_key = {
        unit_key: _attempt_journal_relative_path(unit_key)
        for unit_key in payload["expected_unit_keys"]
    }
    return sha256_bytes(
        canonical_json(
            {
                "schema_version": 1,
                "domain": LIVE_RUN_SEED_DOMAIN,
                "manifest": payload,
                "attempt_journal_policy": ATTEMPT_JOURNAL_POLICY,
                "attempt_journal_paths_by_unit_key": paths_by_unit_key,
            }
        )
    )


def _validated_attempt_journal_contract(
    *,
    expected_unit_keys: tuple[str, ...],
    expected_journal_keys: Iterable[str] | None,
    expected_execution_contract_templates: Mapping[str, dict] | None,
    run_seed: str | None,
    run_id: str | None,
) -> dict | None:
    if expected_journal_keys is None:
        if expected_execution_contract_templates is not None:
            raise ProvenanceError(
                "execution contract templates require attempt journals"
            )
        return None
    if not _is_sha256(run_seed):
        raise ProvenanceError("live run seed is malformed")
    journal_keys = _validated_keys(
        "expected attempt journal keys",
        expected_journal_keys,
    )
    if (
        set(journal_keys) != set(expected_unit_keys)
        or len(journal_keys) != len(expected_unit_keys)
    ):
        raise ProvenanceError(
            "expected attempt journals must bind every expected unit"
        )
    templates = _validated_execution_contract_templates(
        expected_unit_keys=expected_unit_keys,
        expected_templates=expected_execution_contract_templates,
        run_seed=run_seed,
    )
    paths_by_unit_key = {
        unit_key: _attempt_journal_relative_path(unit_key)
        for unit_key in expected_unit_keys
    }
    identity_sha256_by_unit_key = {
        unit_key: sha256_bytes(
            canonical_json(
                {
                    "schema_version": 1,
                    "run_seed": run_seed,
                    "unit_key": unit_key,
                    "relative_path": paths_by_unit_key[unit_key],
                }
            )
        )
        for unit_key in expected_unit_keys
    }
    template_sha256_by_unit_key = {
        unit_key: templates[unit_key]["template_sha256"]
        for unit_key in expected_unit_keys
    }
    run_fingerprint_sha256 = next(
        iter(
            {
                templates[unit_key]["run_fingerprint_sha256"]
                for unit_key in expected_unit_keys
            }
        )
    )
    contract = {
        "schema_version": 2,
        "policy": ATTEMPT_JOURNAL_POLICY,
        "run_seed": run_seed,
        "paths_by_unit_key": paths_by_unit_key,
        "identity_sha256_by_unit_key": identity_sha256_by_unit_key,
        "execution_contract_template_sha256_by_unit_key": (
            template_sha256_by_unit_key
        ),
        "controls_sha256_by_unit_key": {
            unit_key: templates[unit_key]["controls_sha256"]
            for unit_key in expected_unit_keys
        },
        "run_fingerprint_sha256": run_fingerprint_sha256,
    }
    if run_id is not None:
        if not _is_sha256(run_id):
            raise ProvenanceError("live run ID is malformed")
        contract["execution_contract_sha256_by_unit_key"] = {
            unit_key: _execution_contract_sha256(
                run_id=run_id,
                run_seed=run_seed,
                unit_key=unit_key,
                template_sha256=template_sha256_by_unit_key[unit_key],
            )
            for unit_key in expected_unit_keys
        }
    return contract


def _validate_attempt_journal_contract(
    contract: object,
    *,
    expected_unit_keys: tuple[str, ...],
    run_id: str,
) -> None:
    if not isinstance(contract, dict) or set(contract) != {
        "schema_version",
        "policy",
        "run_seed",
        "paths_by_unit_key",
        "identity_sha256_by_unit_key",
        "execution_contract_template_sha256_by_unit_key",
        "controls_sha256_by_unit_key",
        "run_fingerprint_sha256",
        "execution_contract_sha256_by_unit_key",
    }:
        raise ProvenanceError("attempt journal manifest contract is malformed")
    if (
        contract.get("schema_version") != 2
        or contract.get("policy") != ATTEMPT_JOURNAL_POLICY
    ):
        raise ProvenanceError("attempt journal manifest policy is unsupported")
    paths = contract.get("paths_by_unit_key")
    identity_hashes = contract.get("identity_sha256_by_unit_key")
    template_hashes = contract.get(
        "execution_contract_template_sha256_by_unit_key"
    )
    controls_hashes = contract.get("controls_sha256_by_unit_key")
    run_fingerprint_sha256 = contract.get("run_fingerprint_sha256")
    execution_contract_hashes = contract.get(
        "execution_contract_sha256_by_unit_key"
    )
    expected_keys = set(expected_unit_keys)
    if (
        not isinstance(paths, dict)
        or not isinstance(identity_hashes, dict)
        or not isinstance(template_hashes, dict)
        or not isinstance(controls_hashes, dict)
        or not isinstance(execution_contract_hashes, dict)
        or set(paths) != expected_keys
        or set(identity_hashes) != expected_keys
        or set(template_hashes) != expected_keys
        or set(controls_hashes) != expected_keys
        or set(execution_contract_hashes) != expected_keys
    ):
        raise ProvenanceError("attempt journal manifest coverage is incomplete")
    run_seed = contract.get("run_seed")
    if (
        not _is_sha256(run_id)
        or not _is_sha256(run_seed)
        or not _is_sha256(run_fingerprint_sha256)
        or any(not _is_sha256(value) for value in template_hashes.values())
        or any(not _is_sha256(value) for value in controls_hashes.values())
        or any(
            not _is_sha256(value)
            for value in execution_contract_hashes.values()
        )
    ):
        raise ProvenanceError("attempt journal manifest hashes are malformed")
    expected_paths = {
        unit_key: _attempt_journal_relative_path(unit_key)
        for unit_key in expected_unit_keys
    }
    expected_identities = {
        unit_key: sha256_bytes(
            canonical_json(
                {
                    "schema_version": 1,
                    "run_seed": run_seed,
                    "unit_key": unit_key,
                    "relative_path": expected_paths[unit_key],
                }
            )
        )
        for unit_key in expected_unit_keys
    }
    expected_contract_hashes = {
        unit_key: _execution_contract_sha256(
            run_id=run_id,
            run_seed=run_seed,
            unit_key=unit_key,
            template_sha256=template_hashes[unit_key],
        )
        for unit_key in expected_unit_keys
    }
    if (
        paths != expected_paths
        or identity_hashes != expected_identities
        or execution_contract_hashes != expected_contract_hashes
    ):
        raise ProvenanceError("attempt journal manifest identity mismatch")


def _base_manifest_payload(
    *,
    manifest_core: dict,
    expected_case_keys: tuple[str, ...],
    expected_setup_keys: tuple[str, ...],
    expected_unit_keys: tuple[str, ...],
) -> dict:
    if not isinstance(manifest_core, dict) or manifest_core.get("schema_version") != 1:
        raise ProvenanceError("manifest core schema_version must be 1")
    return {
        "schema_version": 1,
        "producer": "bench/compare/provenance.py:v1",
        "manifest_core": manifest_core,
        "expected_case_keys": list(expected_case_keys),
        "expected_setup_keys": list(expected_setup_keys),
        "expected_unit_keys": list(expected_unit_keys),
        "ledgers": list(LEDGER_NAMES),
    }


def _manifest_document(
    *,
    manifest_core: dict,
    expected_case_keys: tuple[str, ...],
    expected_setup_keys: tuple[str, ...],
    expected_unit_keys: tuple[str, ...],
    expected_attempt_journal_keys: Iterable[str] | None,
    expected_execution_contract_templates: Mapping[str, dict] | None,
    expected_live_run_seed: str | None,
) -> dict:
    payload = _base_manifest_payload(
        manifest_core=manifest_core,
        expected_case_keys=expected_case_keys,
        expected_setup_keys=expected_setup_keys,
        expected_unit_keys=expected_unit_keys,
    )
    run_seed = (
        _live_run_seed(payload)
        if expected_attempt_journal_keys is not None
        else None
    )
    if (
        (run_seed is None and expected_live_run_seed is not None)
        or (
            run_seed is not None
            and expected_live_run_seed != run_seed
        )
    ):
        raise ProvenanceError(
            "expected live run seed does not match the manifest skeleton"
        )
    precommit = _validated_attempt_journal_contract(
        expected_unit_keys=expected_unit_keys,
        expected_journal_keys=expected_attempt_journal_keys,
        expected_execution_contract_templates=(
            expected_execution_contract_templates
        ),
        run_seed=run_seed,
        run_id=None,
    )
    if precommit is not None:
        payload["attempt_journal_contract"] = precommit
    run_id = sha256_bytes(canonical_json(payload))
    if precommit is not None:
        payload["attempt_journal_contract"] = (
            _validated_attempt_journal_contract(
                expected_unit_keys=expected_unit_keys,
                expected_journal_keys=expected_unit_keys,
                expected_execution_contract_templates=(
                    expected_execution_contract_templates
                ),
                run_seed=run_seed,
                run_id=run_id,
            )
        )
    payload["run_id"] = run_id
    payload["manifest_sha256"] = sha256_bytes(canonical_json(payload))
    return payload


def _validate_manifest(manifest: dict) -> None:
    if not isinstance(manifest, dict):
        raise ProvenanceError("manifest must be an object")
    expected = manifest.get("manifest_sha256")
    payload = dict(manifest)
    payload.pop("manifest_sha256", None)
    if expected != sha256_bytes(canonical_json(payload)):
        raise ProvenanceError("manifest SHA mismatch")
    run_payload = dict(payload)
    run_id = run_payload.pop("run_id", None)
    expected_unit_keys = manifest.get("expected_unit_keys")
    if (
        not isinstance(expected_unit_keys, list)
        or any(not isinstance(value, str) or not value for value in expected_unit_keys)
    ):
        raise ProvenanceError("manifest expected unit keys are malformed")
    journal_contract = manifest.get("attempt_journal_contract")
    if journal_contract is not None:
        _validate_attempt_journal_contract(
            journal_contract,
            expected_unit_keys=tuple(expected_unit_keys),
            run_id=run_id,
        )
        precommit = dict(journal_contract)
        precommit.pop(
            "execution_contract_sha256_by_unit_key",
            None,
        )
        run_payload["attempt_journal_contract"] = precommit
        base_payload = dict(run_payload)
        base_payload.pop("attempt_journal_contract", None)
        if journal_contract.get("run_seed") != _live_run_seed(base_payload):
            raise ProvenanceError("manifest live run seed mismatch")
    if run_id != sha256_bytes(canonical_json(run_payload)):
        raise ProvenanceError("manifest run ID mismatch")


class RunBundle:
    """The four-ledger run bundle with exact-coverage finalization."""

    def __init__(self, run_dir: Path, manifest: dict):
        self.run_dir = run_dir
        self.manifest = manifest
        seal_path = run_dir / ".done"
        self.cases = AppendOnlyLedger(
            run_dir / "cases.jsonl",
            seal_path=seal_path,
        )
        self.setup = AppendOnlyLedger(
            run_dir / "setup.jsonl",
            seal_path=seal_path,
        )
        self.observations = AppendOnlyLedger(
            run_dir / "observations.jsonl",
            seal_path=seal_path,
        )
        self.errors = AppendOnlyLedger(
            run_dir / "errors.jsonl",
            seal_path=seal_path,
        )

    @classmethod
    def open_existing(cls, run_dir: Path) -> RunBundle:
        requested = Path(run_dir)
        if requested.is_symlink() or not requested.is_dir():
            raise ProvenanceError(f"run directory is missing or unsafe: {requested}")
        run_dir = requested.resolve()
        manifest_path = run_dir / "manifest.json"
        if manifest_path.is_symlink():
            raise ProvenanceError("refusing symlink manifest")
        manifest = _load_json_file(manifest_path, "existing manifest")
        _validate_manifest(manifest)
        try:
            bundle = cls(run_dir, manifest)
            if (run_dir / ".done").exists():
                bundle.verify_finalized()
            return bundle
        except ProvenanceError as exc:
            if (run_dir / ".done").exists():
                raise ProvenanceError(
                    f"finalized artifact verification failed: {exc}"
                ) from exc
            raise

    @classmethod
    def derive_live_run_seed(
        cls,
        *,
        manifest_core: dict,
        expected_case_keys: Iterable[str],
        expected_setup_keys: Iterable[str],
        expected_unit_keys: Iterable[str],
    ) -> str:
        """Derive the identity signed authorities bind before a live run."""
        case_keys = _validated_keys(
            "expected case keys",
            expected_case_keys,
        )
        setup_keys = _validated_keys(
            "expected setup keys",
            expected_setup_keys,
        )
        unit_keys = _validated_keys(
            "expected unit keys",
            expected_unit_keys,
        )
        payload = _base_manifest_payload(
            manifest_core=manifest_core,
            expected_case_keys=case_keys,
            expected_setup_keys=setup_keys,
            expected_unit_keys=unit_keys,
        )
        return _live_run_seed(payload)

    @classmethod
    def create(
        cls,
        run_dir: Path,
        *,
        manifest_core: dict,
        expected_case_keys: Iterable[str],
        expected_setup_keys: Iterable[str],
        expected_unit_keys: Iterable[str],
        expected_attempt_journal_keys: Iterable[str] | None = None,
        expected_execution_contract_templates: (
            Mapping[str, dict] | None
        ) = None,
        expected_live_run_seed: str | None = None,
    ) -> RunBundle:
        requested = Path(run_dir)
        if requested.is_symlink():
            raise ProvenanceError(f"refusing symlink run directory: {requested}")
        requested.mkdir(parents=True, exist_ok=True)
        run_dir = requested.resolve()
        case_keys = _validated_keys("expected case keys", expected_case_keys)
        setup_keys = _validated_keys("expected setup keys", expected_setup_keys)
        unit_keys = _validated_keys("expected unit keys", expected_unit_keys)
        manifest = _manifest_document(
            manifest_core=manifest_core,
            expected_case_keys=case_keys,
            expected_setup_keys=setup_keys,
            expected_unit_keys=unit_keys,
            expected_attempt_journal_keys=expected_attempt_journal_keys,
            expected_execution_contract_templates=(
                expected_execution_contract_templates
            ),
            expected_live_run_seed=expected_live_run_seed,
        )
        manifest_path = run_dir / "manifest.json"
        if manifest_path.is_symlink():
            raise ProvenanceError("refusing symlink manifest")
        if manifest_path.exists():
            current = _load_json_file(manifest_path, "existing manifest")
            _validate_manifest(current)
            if canonical_json(current) != canonical_json(manifest):
                raise ProvenanceError(
                    "existing run manifest differs; refusing unsafe resume"
                )
        else:
            atomic_write_json(manifest_path, manifest, replace=False)
        bundle = cls(run_dir, manifest)
        if (run_dir / ".done").exists():
            bundle.verify_finalized()
        return bundle

    def _attempt_journal_contract(self) -> dict | None:
        contract = self.manifest.get("attempt_journal_contract")
        if contract is None:
            return None
        _validate_attempt_journal_contract(
            contract,
            expected_unit_keys=tuple(self.manifest["expected_unit_keys"]),
            run_id=self.manifest["run_id"],
        )
        return contract

    def attempt_journal_path(self, unit_key: str) -> Path:
        contract = self._attempt_journal_contract()
        if contract is None:
            raise ProvenanceError("run has no expected attempt journals")
        relative_path = contract["paths_by_unit_key"].get(unit_key)
        if not isinstance(relative_path, str):
            raise ProvenanceError("unit has no expected attempt journal")
        return self._safe_artifact_path(relative_path)

    def _safe_artifact_path(self, relative_path: str) -> Path:
        candidate = Path(relative_path)
        if (
            not isinstance(relative_path, str)
            or not relative_path
            or candidate.is_absolute()
            or candidate.as_posix() != relative_path
            or any(part in {"", ".", ".."} for part in candidate.parts)
        ):
            raise ProvenanceError("attempt journal artifact path is unsafe")
        current = self.run_dir
        for part in candidate.parts:
            current = current / part
            if current.is_symlink():
                raise ProvenanceError(
                    f"attempt journal path traverses a symlink: {relative_path}"
                )
        try:
            resolved = current.resolve(strict=False)
            resolved.relative_to(self.run_dir)
        except (OSError, ValueError) as exc:
            raise ProvenanceError(
                f"attempt journal path escapes the run: {relative_path}"
            ) from exc
        return current

    def _resolved_attempt_journals(self) -> dict[str, dict]:
        contract = self._attempt_journal_contract()
        if contract is None:
            return {}
        descriptors: dict[str, dict] = {}
        aggregate_cost_micros = 0
        run_total_cap_micros: set[int] = set()
        expected_paths = set(contract["paths_by_unit_key"].values())
        journal_dir = self.run_dir / "attempt-journals"
        if journal_dir.is_symlink():
            raise ProvenanceError("attempt journal directory is a symlink")
        if journal_dir.exists():
            actual_paths = {
                path.relative_to(self.run_dir).as_posix()
                for path in journal_dir.iterdir()
                if path.is_file() or path.is_symlink()
            }
            if actual_paths - expected_paths:
                raise ProvenanceError("unexpected attempt journal artifact")
        for unit_key in self.manifest["expected_unit_keys"]:
            relative_path = contract["paths_by_unit_key"][unit_key]
            path = self._safe_artifact_path(relative_path)
            if path.is_symlink() or not path.is_file():
                raise ProvenanceError(
                    f"expected attempt journal is missing: {unit_key}"
                )
            raw = _read_unique_regular_file(
                path,
                label=f"attempt journal {unit_key}",
            )
            from .live_runtime import (
                LiveControlError,
                validate_terminal_attempt_journal,
            )

            try:
                journal = validate_terminal_attempt_journal(
                    raw,
                    expected_run_id=self.manifest["run_id"],
                    expected_unit_key=unit_key,
                )
            except LiveControlError as exc:
                failure = (
                    "unresolved"
                    if "unresolved" in str(exc)
                    else "integrity validation failed"
                )
                raise ProvenanceError(
                    f"attempt journal {failure}: {unit_key}"
                ) from exc
            if (
                journal["execution_contract"]["expected_units"]
                != len(self.manifest["expected_unit_keys"])
            ):
                raise ProvenanceError(
                    "attempt journal execution contract expected unit count "
                    f"mismatch: {unit_key}"
                )
            if (
                journal["execution_contract_sha256"]
                != contract["execution_contract_sha256_by_unit_key"][
                    unit_key
                ]
                or journal["run_seed"] != contract["run_seed"]
                or journal["execution_contract_template_sha256"]
                != contract[
                    "execution_contract_template_sha256_by_unit_key"
                ][unit_key]
                or journal["execution_contract"]["controls_sha256"]
                != contract["controls_sha256_by_unit_key"][unit_key]
                or journal["execution_contract"]["run_fingerprint_sha256"]
                != contract["run_fingerprint_sha256"]
            ):
                raise ProvenanceError(
                    "attempt journal execution contract commitment "
                    f"mismatch: {unit_key}"
                )
            final_attempt = journal["attempts"][-1]
            receipt = final_attempt["receipt"]
            cumulative_cost_micros = sum(
                _serialized_usd_micros(
                    attempt["receipt"]["cost_usd"],
                    label="attempt journal receipt cost",
                )
                for attempt in journal["attempts"]
                if "receipt" in attempt
            )
            aggregate_cost_micros += cumulative_cost_micros
            run_total_cap_micros.add(
                _serialized_usd_micros(
                    journal["execution_contract"]["max_total_usd"],
                    label="attempt journal run total cap",
                )
            )
            if (
                final_attempt["classification"]["value"] == "fatal_error"
                or receipt["error_class"] in NONFINALIZABLE_ERROR_CLASSES
            ):
                raise ProvenanceError(
                    f"fatal attempt journal blocks finalization: {unit_key}"
                )
            descriptors[unit_key] = {
                "relative_path": relative_path,
                "execution_contract_sha256": journal[
                    "execution_contract_sha256"
                ],
                "identity_sha256": contract["identity_sha256_by_unit_key"][
                    unit_key
                ],
                "run_fingerprint_sha256": contract[
                    "run_fingerprint_sha256"
                ],
                "sha256": sha256_bytes(raw),
                "bytes": len(raw),
                "terminal_result": {
                    "classification": final_attempt["classification"][
                        "value"
                    ],
                    "operation_id": receipt["operation_id"],
                    "status": receipt["status"],
                    "response_sha256": receipt["response_sha256"],
                    "error_class": receipt["error_class"],
                    "cost_usd": _format_usd_micros(
                        cumulative_cost_micros,
                        label="attempt journal cumulative cost",
                    ),
                },
            }
        if len(run_total_cap_micros) != 1:
            raise ProvenanceError(
                "attempt journals disagree on the run total cost cap"
            )
        if aggregate_cost_micros > next(iter(run_total_cap_micros)):
            raise ProvenanceError(
                "attempt journal aggregate cost exceeds the run total cap"
            )
        return descriptors

    def _validate_attempt_journal_results(
        self,
        journals: dict[str, dict],
    ) -> None:
        if not journals:
            return
        observations = self.observations._load()
        errors = self.errors._load()
        for unit_key, descriptor in journals.items():
            terminal = descriptor["terminal_result"]
            if terminal["classification"] == "success":
                outcome = observations.get(unit_key)
                valid = (
                    isinstance(outcome, dict)
                    and outcome.get("stable_key") == unit_key
                    and outcome.get("unit_key") == unit_key
                    and outcome.get("status") == "ok"
                    and outcome.get("operation_id")
                    == terminal["operation_id"]
                    and outcome.get("raw_response_sha256")
                    == terminal["response_sha256"]
                    and outcome.get("cost_usd") == terminal["cost_usd"]
                )
            else:
                outcome = errors.get(unit_key)
                valid = (
                    isinstance(outcome, dict)
                    and outcome.get("stable_key") == unit_key
                    and outcome.get("unit_key") == unit_key
                    and outcome.get("status") == "error"
                    and outcome.get("operation_id")
                    == terminal["operation_id"]
                    and outcome.get("error_class")
                    == terminal["error_class"]
                    and outcome.get("cost_usd") == terminal["cost_usd"]
                )
            if not valid:
                raise ProvenanceError(
                    f"attempt journal result binding mismatch: {unit_key}"
                )

    def require_exact_coverage(self) -> None:
        case_keys = set(self.manifest["expected_case_keys"])
        setup_keys = set(self.manifest["expected_setup_keys"])
        unit_keys = set(self.manifest["expected_unit_keys"])
        actual_cases = set(self.cases._load())
        actual_setup = set(self.setup._load())
        observations = set(self.observations._load())
        error_records = self.errors._load()
        errors = set(error_records)
        fatal = sorted(
            key
            for key, record in error_records.items()
            if record.get("error_class") in NONFINALIZABLE_ERROR_CLASSES
        )
        if fatal:
            raise ProvenanceError(
                "fatal configuration, identity, or budget outcomes leave the "
                f"run incomplete: {', '.join(fatal)}"
            )
        if observations & errors:
            raise ProvenanceError(
                "a unit appears in both observations and errors"
            )
        if (
            actual_cases != case_keys
            or actual_setup != setup_keys
            or observations | errors != unit_keys
        ):
            raise ProvenanceError(
                "exact stable-key coverage is incomplete or contains unknown keys"
            )

    def _authoritative_consumer(self) -> str | None:
        manifest_core = self.manifest.get("manifest_core")
        if not isinstance(manifest_core, dict):
            raise ProvenanceError("manifest has no core")
        consumer = manifest_core.get("authoritative_consumer")
        if manifest_core.get("benchmark") == "five-arm-code-localization":
            if consumer != AUTHORITATIVE_CONSUMER:
                raise ProvenanceError(
                    "five-arm benchmark has no recognized authoritative consumer"
                )
            return consumer
        if consumer is not None:
            raise ProvenanceError(
                "non-five-arm bundle declares an unsupported authoritative consumer"
            )
        return None

    def _verify_authoritative_summary(self, summary: dict) -> None:
        consumer = self._authoritative_consumer()
        if consumer is None:
            return
        from .score import (  # Imported lazily to keep ledger primitives acyclic.
            verify_finalized_bundle_semantics,
        )

        try:
            verify_finalized_bundle_semantics(
                self,
                self.manifest,
                summary,
            )
        except ProvenanceError:
            raise
        except ValueError as exc:
            raise ProvenanceError(
                f"authoritative semantic validation failed: {exc}"
            ) from exc

    def verify_finalized(self) -> tuple[dict, dict]:
        done_path = self.run_dir / ".done"
        provenance_path = self.run_dir / "provenance.json"
        summary_path = self.run_dir / "summary.json"
        for path in (done_path, provenance_path, summary_path):
            if path.is_symlink() or not path.is_file():
                raise ProvenanceError(
                    f"finalized artifact is missing or unsafe: {path.name}"
                )
        done = _load_json_file(done_path, "finalized artifact")
        provenance = _load_json_file(
            provenance_path,
            "finalized artifact",
        )
        summary = _load_json_file(summary_path, "finalized artifact")
        journal_contract = self._attempt_journal_contract()
        provenance_fields = {
            "schema_version",
            "producer",
            "run_id",
            "result_id",
            "authoritative_consumer",
            "artifacts",
        }
        if journal_contract is not None:
            provenance_fields.add("attempt_journals")
        if (
            not isinstance(done, dict)
            or set(done)
            != {
                "schema_version",
                "run_id",
                "result_id",
                "provenance_sha256",
            }
            or done.get("schema_version") != 1
            or done.get("run_id") != self.manifest["run_id"]
            or done.get("provenance_sha256") != sha256_file(provenance_path)
            or not isinstance(provenance, dict)
            or set(provenance) != provenance_fields
            or provenance.get("schema_version") != 1
            or provenance.get("run_id") != self.manifest["run_id"]
            or not isinstance(summary, dict)
        ):
            raise ProvenanceError("finalized artifact identity mismatch")
        journals = self._resolved_attempt_journals()
        self._validate_attempt_journal_results(journals)
        expected_names = {*LEDGER_NAMES, "manifest.json", "summary.json"}
        expected_names.update(
            descriptor["relative_path"] for descriptor in journals.values()
        )
        artifacts = provenance.get("artifacts")
        if not isinstance(artifacts, dict) or set(artifacts) != expected_names:
            raise ProvenanceError("finalized artifact inventory mismatch")
        journal_artifacts = {
            descriptor["relative_path"]: {
                "sha256": descriptor["sha256"],
                "bytes": descriptor["bytes"],
            }
            for descriptor in journals.values()
        }
        for name, descriptor in artifacts.items():
            path = (
                self._safe_artifact_path(name)
                if "/" in name
                else self.run_dir / name
            )
            if name in journal_artifacts:
                valid = descriptor == journal_artifacts[name]
            else:
                valid = (
                    isinstance(descriptor, dict)
                    and not path.is_symlink()
                    and path.is_file()
                    and descriptor.get("sha256") == sha256_file(path)
                    and descriptor.get("bytes") == path.stat().st_size
                )
            if not valid:
                raise ProvenanceError(
                    f"finalized artifact hash or size mismatch: {name}"
                )
        if journal_contract is not None and provenance.get(
            "attempt_journals"
        ) != journals:
            raise ProvenanceError("finalized attempt journal inventory mismatch")
        expected_result_id = final_result_id(
            self.manifest["run_id"],
            artifacts,
        )
        if (
            provenance.get("result_id") != expected_result_id
            or done.get("result_id") != expected_result_id
            or provenance.get("authoritative_consumer")
            != self._authoritative_consumer()
        ):
            raise ProvenanceError("finalized result identity mismatch")
        self.require_exact_coverage()
        self._verify_authoritative_summary(summary)
        return provenance, summary

    def finalize(self, summary: dict) -> dict:
        if (self.run_dir / ".done").is_symlink():
            raise ProvenanceError("refusing symlink .done marker")
        if (self.run_dir / ".done").exists():
            provenance, current_summary = self.verify_finalized()
            if canonical_json(current_summary) == canonical_json(summary):
                return provenance
            raise ProvenanceError(
                "finalized run cannot be rescored with different inputs"
            )
        self.require_exact_coverage()
        journals = self._resolved_attempt_journals()
        self._validate_attempt_journal_results(journals)
        expected_count = len(self.manifest["expected_unit_keys"])
        if (
            not isinstance(summary, dict)
            or summary.get("schema_version") != 1
            or summary.get("intent_to_treat") is not True
            or summary.get("expected_units") != expected_count
            or summary.get("accounted_units") != expected_count
        ):
            raise ProvenanceError(
                "summary does not attest exact intent-to-treat coverage"
            )
        self._verify_authoritative_summary(summary)
        summary_path = self.run_dir / "summary.json"
        atomic_write_json(summary_path, summary)
        artifact_names = [
            *LEDGER_NAMES,
            "manifest.json",
            "summary.json",
            *(
                descriptor["relative_path"]
                for descriptor in journals.values()
            ),
        ]
        journal_artifacts = {
            descriptor["relative_path"]: {
                "sha256": descriptor["sha256"],
                "bytes": descriptor["bytes"],
            }
            for descriptor in journals.values()
        }
        artifacts = {
            name: (
                journal_artifacts[name]
                if name in journal_artifacts
                else {
                    "sha256": sha256_file(
                        self._safe_artifact_path(name)
                        if "/" in name
                        else self.run_dir / name
                    ),
                    "bytes": (
                        self._safe_artifact_path(name)
                        if "/" in name
                        else self.run_dir / name
                    ).stat().st_size,
                }
            )
            for name in sorted(artifact_names)
        }
        provenance = {
            "schema_version": 1,
            "producer": "bench/compare/provenance.py:v1",
            "run_id": self.manifest["run_id"],
            "result_id": final_result_id(
                self.manifest["run_id"],
                artifacts,
            ),
            "authoritative_consumer": self._authoritative_consumer(),
            "artifacts": artifacts,
        }
        if self._attempt_journal_contract() is not None:
            provenance["attempt_journals"] = journals
        provenance_path = self.run_dir / "provenance.json"
        atomic_write_json(provenance_path, provenance)
        done = {
            "schema_version": 1,
            "run_id": self.manifest["run_id"],
            "result_id": provenance["result_id"],
            "provenance_sha256": sha256_file(provenance_path),
        }
        atomic_write_json(self.run_dir / ".done", done)
        return provenance
