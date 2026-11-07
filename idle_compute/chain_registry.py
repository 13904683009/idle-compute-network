"""Read-only JSON-RPC client for the Phase 1 NodeRegistry prototype.

This module performs public reads only (no signing, no private keys). It is
deliberately dependency-light: ``http.client`` + pycryptodome keccak, matching
the repository's existing canonical-hash stack. HTTP(S) RPC is the only
network I/O; no P2P framework is introduced.
"""

from __future__ import annotations

import http.client
import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import Any

from Crypto.Hash import keccak

logger = logging.getLogger(__name__)

ROLE_CODES = {"worker": 1, "client": 2}
ROLE_NAMES = {1: "worker", 2: "client"}


class ChainRegistryError(Exception):
    """RPC/decoding/configuration error; callers fail closed or fall back."""


@dataclass(frozen=True)
class ChainNode:
    node_key: str
    owner: str
    role: str
    tls_fingerprint: str
    capability_hash: str
    endpoint_hash: str
    active: bool


def _keccak(data: bytes) -> bytes:
    return keccak.new(digest_bits=256, data=data).digest()


def node_key(node_id: str) -> str:
    """bytes32 registry key for an Ed25519 libp2p node id."""
    return "0x" + _keccak(node_id.encode("utf-8")).hex()


def _selector(signature: str) -> str:
    return "0x" + _keccak(signature.encode("ascii"))[:4].hex()


_SELECTOR_ACTIVE = _selector("active(bytes32)")
_SELECTOR_GET = _selector("get(bytes32)")
_SELECTOR_ACTIVE_COUNT = _selector("activeCount()")
_SELECTOR_ACTIVE_AT = _selector("activeAt(uint256)")


def _word_from_hex(value: str) -> str:
    raw = value[2:] if value.startswith("0x") else value
    if len(raw) > 64:
        raise ChainRegistryError("return word too long")
    return raw.rjust(64, "0")


def _hex_payload(value: str) -> str:
    return _word_from_hex(value)


class ChainRegistryClient:
    """Caches active registrations and answers ``is_active`` queries."""

    def __init__(
        self,
        rpc_url: str,
        registry_address: str,
        *,
        refresh_seconds: int = 30,
        timeout_seconds: float = 5.0,
    ) -> None:
        scheme, sep, rest = rpc_url.partition("://")
        if not sep or scheme not in ("http", "https"):
            raise ChainRegistryError(f"unsupported RPC URL: {rpc_url!r}")
        host_port, _, path = rest.partition("/")
        host, colon, port_text = host_port.partition(":")
        if not host:
            raise ChainRegistryError(f"RPC URL has no host: {rpc_url!r}")
        try:
            port = int(port_text) if colon else (443 if scheme == "https" else 80)
        except ValueError:
            raise ChainRegistryError(f"invalid RPC port: {rpc_url!r}") from None
        if not registry_address.startswith("0x") or len(registry_address) != 42:
            raise ChainRegistryError("registry_address must be 0x + 40 hex chars")
        int(registry_address[2:], 16)
        self._scheme = scheme
        self._host = host
        self._port = port
        self._path = "/" + path if path else "/"
        self._address = registry_address
        self._refresh_seconds = max(1, int(refresh_seconds))
        self._timeout = float(timeout_seconds)
        self._lock = threading.RLock()
        self._cache: dict[str, ChainNode] = {}
        self._last_refresh = 0.0
        self._last_error: str | None = None

    # --------------------------------------------------------------- RPC
    def _rpc(self, method: str, params: list[Any]) -> Any:
        body = json.dumps(
            {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        ).encode("utf-8")
        if self._scheme == "https":
            conn: http.client.HTTPConnection = http.client.HTTPSConnection(
                self._host, self._port, timeout=self._timeout
            )
        else:
            conn = http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)
        try:
            conn.request(
                "POST",
                self._path,
                body=body,
                headers={"Content-Type": "application/json"},
            )
            response = conn.getresponse()
            raw = response.read()
        except OSError as exc:
            raise ChainRegistryError(f"RPC transport failed: {exc}") from exc
        finally:
            conn.close()
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ChainRegistryError(f"RPC returned malformed JSON: {exc}") from exc
        if "error" in payload:
            raise ChainRegistryError(f"RPC error: {payload['error']}")
        if "result" not in payload:
            raise ChainRegistryError("RPC response missing result")
        return payload["result"]

    def _eth_call(self, data: str) -> str:
        result = self._rpc("eth_call", [{"to": self._address, "data": data}, "latest"])
        if not isinstance(result, str) or not result.startswith("0x"):
            raise ChainRegistryError("eth_call returned non-hex result")
        return result

    # ----------------------------------------------------------- decoding
    def active(self, node_id: str) -> bool:
        result = self._eth_call(_SELECTOR_ACTIVE + _hex_payload(node_key(node_id)))
        return int(result, 16) != 0

    def active_count(self) -> int:
        return int(self._eth_call(_SELECTOR_ACTIVE_COUNT), 16)

    def active_at(self, index: int) -> str:
        if index < 0:
            raise ChainRegistryError("active index must be >= 0")
        result = self._eth_call(_SELECTOR_ACTIVE_AT + _word_from_hex(hex(index)))
        return "0x" + result[-64:]

    def get(self, node_key_hex: str) -> ChainNode:
        result = self._eth_call(_SELECTOR_GET + _hex_payload(node_key_hex))
        raw = result[2:]
        if len(raw) < 6 * 64:
            raise ChainRegistryError("get() return data too short")
        words = [raw[i * 64 : (i + 1) * 64] for i in range(6)]
        owner = "0x" + words[0][-40:]
        role = ROLE_NAMES.get(int(words[1], 16), "unknown")
        return ChainNode(
            node_key=node_key_hex,
            owner=owner,
            role=role,
            tls_fingerprint="0x" + words[2],
            capability_hash="0x" + words[3],
            endpoint_hash="0x" + words[4],
            active=int(words[5], 16) != 0,
        )

    # ------------------------------------------------------------- cache
    def refresh(self, *, force: bool = False) -> int:
        with self._lock:
            now = time.monotonic()
            if not force and self._cache and now - self._last_refresh < self._refresh_seconds:
                return len(self._cache)
            try:
                count = self.active_count()
                cache: dict[str, ChainNode] = {}
                for index in range(count):
                    key = self.active_at(index)
                    record = self.get(key)
                    if record.active:
                        cache[key.lower()] = record
                self._cache = cache
                self._last_refresh = now
                self._last_error = None
                logger.info("chain registry refreshed: %d active node(s)", len(cache))
                return len(cache)
            except ChainRegistryError as exc:
                self._last_error = str(exc)
                logger.warning("chain registry refresh failed: %s", exc)
                raise

    def lookup(self, node_id: str) -> ChainNode | None:
        key = node_key(node_id).lower()
        with self._lock:
            return self._cache.get(key)

    def is_active(self, node_id: str, role: str | None = None) -> bool:
        try:
            self.refresh()
        except ChainRegistryError:
            # Chain unavailable: caller decides fallback; chain-only admission
            # fails closed for this probe.
            return False
        record = self.lookup(node_id)
        if record is None or not record.active:
            return False
        if role is not None and record.role not in (role, "any"):
            return False
        return True

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "rpc": f"{self._scheme}://{self._host}:{self._port}{self._path}",
                "registry": self._address,
                "cached": len(self._cache),
                "last_refresh": self._last_refresh,
                "last_error": self._last_error,
            }


__all__ = [
    "ChainNode",
    "ChainRegistryClient",
    "ChainRegistryError",
    "ROLE_CODES",
    "ROLE_NAMES",
    "node_key",
]
