"""Canonical hashing and local HMAC signatures.

HMAC is used for the local MVP because it is standard-library-only. It is a
symmetric integrity mechanism, not a production peer identity scheme; see
docs/THREAT_MODEL.md. Keys are local files and are never printed. On Windows
the POSIX 0600 mode is not enforceable; key creation warns about this once and
key confidentiality rests on the directory ACLs (documented MVP limitation).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import time
import warnings
from pathlib import Path
from typing import Any

GENESIS_HASH = "0" * 64
KEY_BYTES = 32

_POSIX_PERMISSIONS_UNSUPPORTED = os.name == "nt"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def content_hash(value: Any) -> str:
    return sha256_hex(canonical_json(value))


def result_signing_payload(task_id: str, lease_id: str, peer_id: str, result_hash: str) -> str:
    return canonical_json(
        {
            "domain": "idle-compute-result-v1",
            "task_id": task_id,
            "lease_id": lease_id,
            "peer_id": peer_id,
            "result_hash": result_hash,
        }
    )


class LocalSigner:
    """HMAC-SHA256 signer backed by a local key file."""

    def __init__(self, key: bytes) -> None:
        if len(key) < KEY_BYTES:
            raise ValueError("signing key must be at least 32 bytes")
        self._key = key

    def sign(self, payload: str) -> str:
        return hmac.new(self._key, payload.encode("utf-8"), hashlib.sha256).hexdigest()

    def verify(self, payload: str, signature: str) -> bool:
        expected = self.sign(payload)
        return hmac.compare_digest(expected, signature)

    @classmethod
    def load_or_create(cls, path: Path) -> "LocalSigner":
        path.parent.mkdir(parents=True, exist_ok=True)
        created = False
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(os.urandom(KEY_BYTES))
            created = True
        except FileExistsError:
            pass
        if created:
            # The 0o600 mode has POSIX semantics only; on Windows it is inert
            # and the key inherits the directory ACLs. Warn so the limitation
            # is observable instead of silently swallowing it.
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            if _POSIX_PERMISSIONS_UNSUPPORTED:
                warnings.warn(
                    "POSIX 0600 permissions cannot be enforced on Windows; "
                    f"key confidentiality at {path} depends on the directory "
                    "ACLs (see docs/THREAT_MODEL.md).",
                    RuntimeWarning,
                    stacklevel=2,
                )
            key = path.read_bytes()
        else:
            # Another process won the O_EXCL creation race; its write may not
            # be flushed yet, so wait briefly instead of reading a partial key.
            key = cls._read_with_retry(path)
        if len(key) < KEY_BYTES:
            raise ValueError(
                f"signing key is too short: {path} (delete the file to regenerate)"
            )
        return cls(key)

    @staticmethod
    def _read_with_retry(path: Path, attempts: int = 10, delay: float = 0.05) -> bytes:
        for _ in range(attempts):
            try:
                key = path.read_bytes()
            except OSError:
                key = b""
            if len(key) >= KEY_BYTES:
                return key
            time.sleep(delay)
        return path.read_bytes()
