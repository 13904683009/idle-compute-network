"""Ed25519 node identity built on libp2p crypto primitives.

Each node's identity is ONE libp2p Ed25519 KeyPair derived from a 32-byte
random seed persisted as raw bytes at var/<node>/identity.key (same file
discipline as the Phase 1 ledger.key). The keypair is derived with
``libp2p.crypto.ed25519.create_new_key_pair(seed)`` (deterministic), and the
node_id IS the libp2p peer id derived from that same keypair
(``ID.from_pubkey(kp.public_key).to_string()``, '12D3KooW...'). Passing the
same keypair to ``new_host(key_pair=kp)`` in net_transport.py guarantees the
transport peer id and the application node_id cannot desynchronize.

Receivers verify an envelope signature with NO key distribution: the Ed25519
public key is embedded in the node_id multihash and extracted at verify time
via ``ID.from_base58(sender).extract_public_key()``.

Deliberately NOT used: libp2p's ``save_identity`` persistence (its O_TRUNC
write breaks the creation-race guarantee) and libp2p's protobuf key format
(the raw 32-byte seed keeps the key format under our control).

This module is one of the two places allowed to import libp2p (the other is
net_transport.py); the import is quarantined to crypto primitives (no host or
stream logic here).
"""

from __future__ import annotations

import os
import time
import warnings
from pathlib import Path

from libp2p.crypto.ed25519 import create_new_key_pair
from libp2p.crypto.keys import KeyPair
from libp2p.peer.id import ID

SEED_BYTES = 32

# libp2p Ed25519 peer ids: base58 multihash with embedded pubkey (identity
# multihash), 52 chars total, always starting with 12D3KooW. Validated
# character-by-character to stay clear of the source-safety scan's
# forbidden dynamic-code tokens.
_B58_ALPHABET = frozenset("123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz")


def is_valid_node_id(value: str) -> bool:
    """True if ``value`` is a structurally valid libp2p Ed25519 node id."""
    return (
        isinstance(value, str)
        and len(value) == 52
        and value.startswith("12D3KooW")
        and all(ch in _B58_ALPHABET for ch in value)
    )


class Ed25519Signer:
    """Raw Ed25519 signing over strings, backed by a libp2p KeyPair.

    ``sign(payload) -> hex str`` / ``verify(payload, sig) -> bool`` mirrors the
    LocalSigner interface so call sites (Store, policies) are scheme-agnostic.
    """

    def __init__(self, key_pair: KeyPair) -> None:
        self._key_pair = key_pair

    def sign(self, payload: str) -> str:
        return self._key_pair.private_key.sign(payload.encode("utf-8")).hex()

    def verify(self, payload: str, signature: str) -> bool:
        try:
            sig = bytes.fromhex(signature)
        except ValueError:
            return False
        return self._key_pair.public_key.verify(payload.encode("utf-8"), sig)


class NodeIdentity:
    """Race-safe 32-byte seed file -> libp2p Ed25519 keypair -> node_id."""

    def __init__(self, key_pair: KeyPair, seed_path: Path | None = None) -> None:
        self._key_pair = key_pair
        self._seed_path = seed_path

    @property
    def key_pair(self) -> KeyPair:
        """The libp2p KeyPair also handed to ``new_host(key_pair=...)``."""
        return self._key_pair

    @property
    def seed_path(self) -> Path | None:
        return self._seed_path

    @property
    def node_id(self) -> str:
        """libp2p peer id derived from the public key ('12D3KooW...')."""
        return ID.from_pubkey(self._key_pair.public_key).to_string()

    @property
    def public_key_bytes(self) -> bytes:
        """Raw 32-byte Ed25519 public key (for cross-checks/tests)."""
        return self._key_pair.public_key.to_bytes()

    def signer(self) -> Ed25519Signer:
        return Ed25519Signer(self._key_pair)

    def sign(self, payload: str) -> str:
        return self.signer().sign(payload)

    def verify(self, payload: str, signature: str) -> bool:
        return self.signer().verify(payload, signature)

    @staticmethod
    def verify_signature(sender_node_id: str, payload: str, signature: str) -> bool:
        """Verify a signature against the public key embedded in ``sender_node_id``.

        No key distribution needed: the Ed25519 public key travels inside the
        node_id multihash. Returns False for malformed ids or signatures.
        """
        if not is_valid_node_id(sender_node_id):
            return False
        try:
            sig = bytes.fromhex(signature)
        except ValueError:
            return False
        public_key = ID.from_base58(sender_node_id).extract_public_key()
        if public_key is None:
            return False
        return public_key.verify(payload.encode("utf-8"), sig)

    @classmethod
    def load_or_create(cls, path: Path, node_name: str = "node") -> "NodeIdentity":
        """Load the identity from ``path`` or create it race-safely.

        The O_EXCL creation race and partial-file retry follow the Phase 1
        LocalSigner discipline: whichever process loses the race waits (briefly)
        for the winner's full write instead of reading a partial seed.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        created = False
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(os.urandom(SEED_BYTES))
            created = True
        except FileExistsError:
            pass
        if created:
            # 0o600 has POSIX semantics only; on Windows it is inert and the
            # seed inherits the directory ACLs. Warn so the limitation is
            # observable (os.name read at call time so tests can mock it).
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass
            if os.name == "nt":
                warnings.warn(
                    "POSIX 0600 permissions cannot be enforced on Windows; "
                    f"identity key confidentiality at {path} depends on the "
                    "directory ACLs (see docs/THREAT_MODEL.md).",
                    RuntimeWarning,
                    stacklevel=2,
                )
            seed = path.read_bytes()
        else:
            seed = cls._read_with_retry(path)
        if len(seed) != SEED_BYTES:
            raise ValueError(
                f"identity key is corrupt (expected {SEED_BYTES} bytes, found "
                f"{len(seed)}): {path} (delete the file to regenerate)"
            )
        try:
            key_pair = create_new_key_pair(seed)
        except ValueError as exc:
            raise ValueError(
                f"identity key at {path} is not a valid Ed25519 seed "
                f"(delete the file to regenerate): {exc}"
            ) from exc
        return cls(key_pair, seed_path=path)

    @staticmethod
    def _read_with_retry(path: Path, attempts: int = 10, delay: float = 0.05) -> bytes:
        for _ in range(attempts):
            try:
                seed = path.read_bytes()
            except OSError:
                seed = b""
            if len(seed) >= SEED_BYTES:
                return seed
            time.sleep(delay)
        return path.read_bytes()
