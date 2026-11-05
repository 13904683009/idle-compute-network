"""Capability registry abstraction + a LOCAL SIMULATED adapter (D5).

This is **not** a blockchain and must never be described as one:

- ``CapabilityRegistry`` is the interface the matcher/D6 integration will use;
- ``LocalRegistryAdapter`` stores a *commitment* (manifest hash + version +
  validity window + revoke state) in its own SQLite file, with a configurable
  finality delay to simulate the difference between "submitted" and "final".
  There is no consensus, no replication, no chain, no token.

Design limits (see D0 §7 and ADR-D0-3/14/15):

- only ``manifest_hash`` and metadata are stored; the manifest itself travels
  over P2P and is checked against the hash;
- the registry DB is independent of the coordinator DB and must never reuse
  ``credit_events``;
- signature/version/expiry/revocation semantics are the D1 ones
  (``capability.CapabilityManifest`` / ``CapabilityRevocation`` /
  ``check_manifest_update``), not a second scheme.
"""

from __future__ import annotations

import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .capability import (
    CapabilityError,
    CapabilityManifest,
    CapabilityRevocation,
    check_manifest_update,
)

REGISTRY_SCHEMA_VERSION = 1
MAX_FINALITY_DELAY_SECONDS = 3600

STATUS_PENDING = "PENDING"
STATUS_FINAL = "FINAL"
STATUS_REVOKED = "REVOKED"
STATUS_EXPIRED = "EXPIRED"


class RegistryError(Exception):
    SCHEMA = "SCHEMA"
    SIGNATURE = "SIGNATURE"
    EXPIRED = "EXPIRED"
    VERSION_ROLLBACK = "VERSION_ROLLBACK"
    VERSION_CONFLICT = "VERSION_CONFLICT"
    REVOKED = "REVOKED"
    NOT_FOUND = "NOT_FOUND"
    NOT_FINAL = "NOT_FINAL"
    PERSIST_FAILED = "PERSIST_FAILED"

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class RegistryRecord:
    """A registry commitment for one node (never the raw manifest)."""

    node_id: str
    capability_version: int
    manifest_hash: str
    status: str
    registered_at: int
    finalize_at: int
    finalized_at: int | None
    expires_at: int
    revoked_reason: str = ""

    def is_final(self) -> bool:
        return self.status == STATUS_FINAL


class CapabilityRegistry(Protocol):
    """Interface only; see ``LocalRegistryAdapter`` for the MVP implementation."""

    def register(self, manifest: CapabilityManifest, *, now: int | None = None) -> RegistryRecord: ...

    def update(self, manifest: CapabilityManifest, *, now: int | None = None) -> RegistryRecord: ...

    def revoke(self, revocation: CapabilityRevocation, *, now: int | None = None) -> RegistryRecord: ...

    def get(self, node_id: str, *, now: int | None = None) -> RegistryRecord | None: ...

    def verify(self, manifest: CapabilityManifest, *, now: int | None = None) -> RegistryRecord: ...

    def finalize(self, *, now: int | None = None) -> int: ...

    def registry_version(self) -> int: ...


class LocalRegistryAdapter:
    """Single-writer SQLite implementation of ``CapabilityRegistry`` (local only)."""

    def __init__(
        self,
        path: str | Path,
        *,
        finality_delay_seconds: int = 0,
        clock: Callable[[], float] = time.time,
    ) -> None:
        if isinstance(finality_delay_seconds, bool) or not isinstance(finality_delay_seconds, int):
            raise ValueError("finality_delay_seconds must be an int")
        if not 0 <= finality_delay_seconds <= MAX_FINALITY_DELAY_SECONDS:
            raise ValueError(f"finality_delay_seconds must be in 0..{MAX_FINALITY_DELAY_SECONDS}")
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), timeout=10, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._delay = int(finality_delay_seconds)
        self._clock = clock
        with self._lock:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS registry_entries (
                    node_id TEXT PRIMARY KEY,
                    capability_version INTEGER NOT NULL,
                    manifest_hash TEXT NOT NULL,
                    status TEXT NOT NULL,
                    registered_at INTEGER NOT NULL,
                    finalize_at INTEGER NOT NULL,
                    finalized_at INTEGER,
                    expires_at INTEGER NOT NULL,
                    revoked_reason TEXT NOT NULL DEFAULT '',
                    updated_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS registry_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                INSERT OR IGNORE INTO registry_meta(key,value) VALUES ('registry_version','0');
                INSERT OR IGNORE INTO registry_meta(key,value) VALUES ('schema_version','1');
                """
            )
            self._conn.commit()

    # ------------------------------------------------------------- writes

    def register(self, manifest: CapabilityManifest, *, now: int | None = None) -> RegistryRecord:
        moment = self._now(now)
        self._verify_manifest(manifest, moment)
        with self._lock:
            try:
                existing = self._get_locked(manifest.node_id, moment)
                if existing is None:
                    record = self._write_locked(manifest, moment)
                else:
                    decision = check_manifest_update(
                        {
                            "capability_version": existing.capability_version,
                            "manifest_hash": existing.manifest_hash,
                            "revoked": existing.status == STATUS_REVOKED,
                        },
                        manifest,
                        moment,
                    )
                    if decision == "IDEMPOTENT":
                        return existing
                    if decision == "ROLLBACK":
                        raise RegistryError(RegistryError.VERSION_ROLLBACK, "manifest version went backwards")
                    if decision == "CONFLICT":
                        raise RegistryError(
                            RegistryError.VERSION_CONFLICT, "same version with a different manifest"
                        )
                    if decision == "REVOKED":
                        raise RegistryError(RegistryError.REVOKED, "this capability version is revoked")
                    record = self._write_locked(manifest, moment)
                self._bump_locked()
                return record
            except sqlite3.Error as exc:
                try:
                    try:
                        self._conn.rollback()
                    except sqlite3.Error:
                        pass
                except sqlite3.Error:
                    pass
                raise RegistryError(RegistryError.PERSIST_FAILED, f"registry write failed: {exc}") from exc

    def update(self, manifest: CapabilityManifest, *, now: int | None = None) -> RegistryRecord:
        moment = self._now(now)
        self._verify_manifest(manifest, moment)
        with self._lock:
            try:
                existing = self._get_locked(manifest.node_id, moment)
                if existing is None:
                    raise RegistryError(RegistryError.NOT_FOUND, "node is not registered")
                if manifest.capability_version < existing.capability_version:
                    raise RegistryError(RegistryError.VERSION_ROLLBACK, "manifest version went backwards")
                if manifest.capability_version == existing.capability_version:
                    if manifest.hash() == existing.manifest_hash:
                        return existing
                    raise RegistryError(
                        RegistryError.VERSION_CONFLICT, "same version with a different manifest"
                    )
                if existing.status == STATUS_REVOKED:
                    raise RegistryError(RegistryError.REVOKED, "this capability version is revoked")
                record = self._write_locked(manifest, moment)
                self._bump_locked()
                return record
            except sqlite3.Error as exc:
                try:
                    try:
                        self._conn.rollback()
                    except sqlite3.Error:
                        pass
                except sqlite3.Error:
                    pass
                raise RegistryError(RegistryError.PERSIST_FAILED, f"registry write failed: {exc}") from exc

    def revoke(
        self, revocation: CapabilityRevocation, *, now: int | None = None
    ) -> RegistryRecord:
        moment = self._now(now)
        try:
            revocation.verify(moment)
        except CapabilityError as exc:
            raise RegistryError(exc.code, f"revocation rejected: {exc.message}") from exc
        with self._lock:
            try:
                record = self._get_locked(revocation.node_id, moment)
                if record is None:
                    raise RegistryError(RegistryError.NOT_FOUND, "node is not registered")
                if record.capability_version != revocation.capability_version:
                    raise RegistryError(
                        RegistryError.SCHEMA,
                        "only the current capability version can be revoked",
                    )
                if record.status == STATUS_REVOKED:
                    return record
                self._conn.execute(
                    "UPDATE registry_entries SET status=?, revoked_reason=?, updated_at=? WHERE node_id=?",
                    (STATUS_REVOKED, revocation.reason[:200], moment, revocation.node_id),
                )
                self._conn.commit()
                self._bump_locked()
                return self._get_locked(revocation.node_id, moment)  # type: ignore[return-value]
            except sqlite3.Error as exc:
                try:
                    try:
                        self._conn.rollback()
                    except sqlite3.Error:
                        pass
                except sqlite3.Error:
                    pass
                raise RegistryError(RegistryError.PERSIST_FAILED, f"registry write failed: {exc}") from exc

    def finalize(self, *, now: int | None = None) -> int:
        moment = self._now(now)
        with self._lock:
            try:
                cursor = self._conn.execute(
                    "UPDATE registry_entries SET status=?, finalized_at=?, updated_at=? "
                    "WHERE status=? AND finalize_at <= ?",
                    (STATUS_FINAL, moment, moment, STATUS_PENDING, moment),
                )
                self._conn.commit()
            except sqlite3.Error as exc:
                try:
                    try:
                        self._conn.rollback()
                    except sqlite3.Error:
                        pass
                except sqlite3.Error:
                    pass
                raise RegistryError(RegistryError.PERSIST_FAILED, f"registry write failed: {exc}") from exc
            pending_before = cursor.rowcount
            if pending_before:
                self._bump_locked()
            return int(pending_before)

    # -------------------------------------------------------------- reads

    def get(self, node_id: str, *, now: int | None = None) -> RegistryRecord | None:
        moment = self._now(now)
        with self._lock:
            record = self._get_locked(node_id, moment)
        return record

    def verify(self, manifest: CapabilityManifest, *, now: int | None = None) -> RegistryRecord:
        moment = self._now(now)
        self._verify_manifest(manifest, moment)
        record = self.get(manifest.node_id, now=moment)
        if record is None:
            raise RegistryError(RegistryError.NOT_FOUND, "node is not registered")
        if record.status == STATUS_REVOKED:
            raise RegistryError(RegistryError.REVOKED, "capability version is revoked")
        if record.status == STATUS_EXPIRED or record.expires_at <= moment:
            raise RegistryError(RegistryError.EXPIRED, "registry record has expired")
        if record.status != STATUS_FINAL:
            raise RegistryError(
                RegistryError.NOT_FINAL, f"registry record is {record.status}, not FINAL"
            )
        if record.capability_version != manifest.capability_version:
            raise RegistryError(RegistryError.SCHEMA, "version does not match the registry")
        if record.manifest_hash != manifest.hash():
            raise RegistryError(RegistryError.SIGNATURE, "manifest hash does not match the registry")
        return record

    def registry_version(self) -> int:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM registry_meta WHERE key='registry_version'"
            ).fetchone()
        return int(row["value"])

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass

    # ----------------------------------------------------------- internal

    def _now(self, now: int | None) -> int:
        return int(self._clock()) if now is None else int(now)

    def _verify_manifest(self, manifest: CapabilityManifest, moment: int) -> None:
        if not isinstance(manifest, CapabilityManifest):
            raise RegistryError(RegistryError.SCHEMA, "manifest must be a CapabilityManifest")
        try:
            manifest.verify(moment)
        except CapabilityError as exc:
            raise RegistryError(exc.code, f"manifest rejected: {exc.message}") from exc

    def _write_locked(self, manifest: CapabilityManifest, moment: int) -> RegistryRecord:
        status = STATUS_PENDING if self._delay > 0 else STATUS_FINAL
        finalize_at = moment + self._delay
        finalized_at = None if self._delay > 0 else moment
        self._conn.execute(
            "INSERT INTO registry_entries(node_id,capability_version,manifest_hash,status,registered_at,"
            "finalize_at,finalized_at,expires_at,revoked_reason,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?, '',?) "
            "ON CONFLICT(node_id) DO UPDATE SET capability_version=excluded.capability_version,"
            "manifest_hash=excluded.manifest_hash,status=excluded.status,"
            "registered_at=excluded.registered_at,finalize_at=excluded.finalize_at,"
            "finalized_at=excluded.finalized_at,expires_at=excluded.expires_at,"
            "revoked_reason='',updated_at=excluded.updated_at",
            (
                manifest.node_id,
                manifest.capability_version,
                manifest.hash(),
                status,
                moment,
                finalize_at,
                finalized_at,
                manifest.expires_at,
                moment,
            ),
        )
        self._conn.commit()
        return self._get_locked(manifest.node_id, moment)  # type: ignore[return-value]

    def _get_locked(self, node_id: str, moment: int) -> RegistryRecord | None:
        row = self._conn.execute(
            "SELECT * FROM registry_entries WHERE node_id=?", (node_id,)
        ).fetchone()
        if row is None:
            return None
        status = row["status"]
        if status != STATUS_REVOKED and int(row["expires_at"]) <= moment:
            status = STATUS_EXPIRED
        return RegistryRecord(
            node_id=row["node_id"],
            capability_version=int(row["capability_version"]),
            manifest_hash=row["manifest_hash"],
            status=status,
            registered_at=int(row["registered_at"]),
            finalize_at=int(row["finalize_at"]),
            finalized_at=None if row["finalized_at"] is None else int(row["finalized_at"]),
            expires_at=int(row["expires_at"]),
            revoked_reason=row["revoked_reason"],
        )

    def _bump_locked(self) -> None:
        self._conn.execute(
            "UPDATE registry_meta SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT) "
            "WHERE key='registry_version'"
        )
        self._conn.commit()
