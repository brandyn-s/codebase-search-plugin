"""Crash-visible append-only ledgers and content-addressed run bundles."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path
import signal
import tempfile
from typing import Iterable

from .schema import NONFINALIZABLE_ERROR_CLASSES, canonical_json


LEDGER_NAMES = (
    "cases.jsonl",
    "setup.jsonl",
    "observations.jsonl",
    "errors.jsonl",
)
AUTHORITATIVE_CONSUMER = "bench.compare.score:score_bundle_v1"


class ProvenanceError(ValueError):
    """A run artifact is incomplete, corrupted, duplicated, or inconsistent."""


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path) -> str:
    try:
        return sha256_bytes(path.read_bytes())
    except OSError as exc:
        raise ProvenanceError(f"cannot hash {path}: {exc}") from exc


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
        try:
            descriptor = os.open(self.path, os.O_RDWR)
        except OSError as exc:
            raise ProvenanceError(
                f"cannot recover interrupted ledger {self.path}: {exc}"
            ) from exc
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
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
            except OSError:
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
            fcntl.flock(descriptor, fcntl.LOCK_EX)
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
            except OSError:
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


def _manifest_document(
    *,
    manifest_core: dict,
    expected_case_keys: tuple[str, ...],
    expected_setup_keys: tuple[str, ...],
    expected_unit_keys: tuple[str, ...],
) -> dict:
    if not isinstance(manifest_core, dict) or manifest_core.get("schema_version") != 1:
        raise ProvenanceError("manifest core schema_version must be 1")
    payload = {
        "schema_version": 1,
        "producer": "bench/compare/provenance.py:v1",
        "manifest_core": manifest_core,
        "expected_case_keys": list(expected_case_keys),
        "expected_setup_keys": list(expected_setup_keys),
        "expected_unit_keys": list(expected_unit_keys),
        "ledgers": list(LEDGER_NAMES),
    }
    payload["run_id"] = sha256_bytes(canonical_json(payload))
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
    def open_existing(cls, run_dir: Path) -> "RunBundle":
        requested = Path(run_dir)
        if requested.is_symlink() or not requested.is_dir():
            raise ProvenanceError(f"run directory is missing or unsafe: {requested}")
        run_dir = requested.resolve()
        manifest_path = run_dir / "manifest.json"
        if manifest_path.is_symlink():
            raise ProvenanceError("refusing symlink manifest")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProvenanceError(f"existing manifest is invalid: {exc}") from exc
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
    def create(
        cls,
        run_dir: Path,
        *,
        manifest_core: dict,
        expected_case_keys: Iterable[str],
        expected_setup_keys: Iterable[str],
        expected_unit_keys: Iterable[str],
    ) -> "RunBundle":
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
        )
        manifest_path = run_dir / "manifest.json"
        if manifest_path.is_symlink():
            raise ProvenanceError("refusing symlink manifest")
        if manifest_path.exists():
            try:
                current = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ProvenanceError(f"existing manifest is invalid: {exc}") from exc
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
        try:
            done = json.loads(done_path.read_text(encoding="utf-8"))
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProvenanceError(f"finalized artifact is invalid: {exc}") from exc
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
            or set(provenance)
            != {
                "schema_version",
                "producer",
                "run_id",
                "result_id",
                "authoritative_consumer",
                "artifacts",
            }
            or provenance.get("schema_version") != 1
            or provenance.get("run_id") != self.manifest["run_id"]
            or not isinstance(summary, dict)
        ):
            raise ProvenanceError("finalized artifact identity mismatch")
        expected_names = set((*LEDGER_NAMES, "manifest.json", "summary.json"))
        artifacts = provenance.get("artifacts")
        if not isinstance(artifacts, dict) or set(artifacts) != expected_names:
            raise ProvenanceError("finalized artifact inventory mismatch")
        for name, descriptor in artifacts.items():
            path = self.run_dir / name
            if (
                not isinstance(descriptor, dict)
                or path.is_symlink()
                or not path.is_file()
                or descriptor.get("sha256") != sha256_file(path)
                or descriptor.get("bytes") != path.stat().st_size
            ):
                raise ProvenanceError(
                    f"finalized artifact hash or size mismatch: {name}"
                )
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
        artifact_names = (*LEDGER_NAMES, "manifest.json", "summary.json")
        artifacts = {
            name: {
                "sha256": sha256_file(self.run_dir / name),
                "bytes": (self.run_dir / name).stat().st_size,
            }
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
