"""Signing-scheme policies.

Two signing schemes coexist, tagged per row via ``sig_algo``:

- ``hmac-sha256`` — LEGACY-ONLY: the Phase 1 single-process local path (the
  11 legacy CLI commands over a Phase 1 DB). Never used on any network path;
  the network store and worker mirrors hard-reject hmac-tagged rows.
- ``ed25519`` — all network paths: wire envelopes, worker-signed results,
  coordinator-signed credit events. Keyed by the node identity in
  identity.py; verification uses the public key embedded in the claimed
  node_id (no key distribution).

Both policies implement the same surface so Store/ledger call sites are
scheme-agnostic:

- ``sign(payload) -> hex str`` / ``verify(payload, sig) -> bool``
  (self-verification: the node verifying what it signed itself),
- ``verify_result(peer_id, payload, sig) -> bool`` (worker-signed results),
- ``verify_credit(payload, sig) -> bool`` (coordinator-signed credit events),
- ``verify_hmac`` / ``verify_ed25519`` (per-row scheme dispatch in
  verify_ledger),
- ``result_payload(...)`` (the payload builder matching the scheme),
- ``scheme`` (the sig_algo tag written to new rows).

Neither module imports libp2p; Ed25519 goes through identity.py only.
"""

from __future__ import annotations

from typing import Any, Protocol

from .crypto import LocalSigner, result_payload, result_signing_payload
from .identity import Ed25519Signer, NodeIdentity


class SigningPolicy(Protocol):
    scheme: str
    hmac_capable: bool
    ed25519_capable: bool

    def sign(self, payload: str) -> str: ...

    def verify(self, payload: str, signature: str) -> bool: ...

    def verify_result(self, peer_id: str, payload: str, signature: str) -> bool: ...

    def verify_credit(self, payload: str, signature: str) -> bool: ...

    def verify_hmac(self, payload: str, signature: str) -> bool: ...

    def verify_ed25519(self, payload: str, signature: str) -> bool: ...

    def result_payload(self, task_id: str, lease_id: str, peer_id: str, result_hash: str) -> str: ...


class HmacLegacyPolicy:
    """LEGACY-ONLY policy wrapping a LocalSigner for the Phase 1 local path.

    verify_result is the same-key local verification (Phase 1 semantics: the
    in-process worker and the coordinator share the local key file).
    ed25519-tagged rows are unverifiable here and fail loudly in
    verify_ledger (a mixed chain can never silently pass).
    """

    scheme = "hmac-sha256"
    hmac_capable = True
    ed25519_capable = False

    def __init__(self, signer: LocalSigner) -> None:
        self._signer = signer

    def sign(self, payload: str) -> str:
        return self._signer.sign(payload)

    def verify(self, payload: str, signature: str) -> bool:
        return self._signer.verify(payload, signature)

    def verify_result(self, peer_id: str, payload: str, signature: str) -> bool:
        return self._signer.verify(payload, signature)

    def verify_credit(self, payload: str, signature: str) -> bool:
        return self._signer.verify(payload, signature)

    def verify_hmac(self, payload: str, signature: str) -> bool:
        return self._signer.verify(payload, signature)

    def verify_ed25519(self, payload: str, signature: str) -> bool:
        return False  # no Ed25519 verifier configured on the legacy path

    @staticmethod
    def result_payload(task_id: str, lease_id: str, peer_id: str, result_hash: str) -> str:
        return result_signing_payload(task_id, lease_id, peer_id, result_hash)


class Ed25519Policy:
    """Network signing policy: this node's Ed25519 identity.

    - ``verify_result`` extracts the worker's public key from the peer_id
      (identity multihash) — no key distribution.
    - ``verify_credit`` verifies against the pinned coordinator node_id when
      one is set (worker mirror), else against this node's own key
      (the coordinator verifying the chain it signed itself).
    - hmac-tagged rows are unverifiable here and fail loudly (the
      hmac->ed25519 downgrade path is closed by construction).
    """

    scheme = "ed25519"
    hmac_capable = False
    ed25519_capable = True

    def __init__(self, signer: Ed25519Signer, coordinator_node_id: str | None = None) -> None:
        self._signer = signer
        self._coordinator_node_id = coordinator_node_id

    @classmethod
    def from_identity(
        cls, identity: NodeIdentity, coordinator_node_id: str | None = None
    ) -> "Ed25519Policy":
        return cls(identity.signer(), coordinator_node_id)

    def sign(self, payload: str) -> str:
        return self._signer.sign(payload)

    def verify(self, payload: str, signature: str) -> bool:
        return self._signer.verify(payload, signature)

    def verify_result(self, peer_id: str, payload: str, signature: str) -> bool:
        return NodeIdentity.verify_signature(peer_id, payload, signature)

    def verify_credit(self, payload: str, signature: str) -> bool:
        if self._coordinator_node_id is None:
            return self._signer.verify(payload, signature)
        return NodeIdentity.verify_signature(self._coordinator_node_id, payload, signature)

    def verify_hmac(self, payload: str, signature: str) -> bool:
        return False  # hmac rows are rejected on network paths

    def verify_ed25519(self, payload: str, signature: str) -> bool:
        return self.verify_credit(payload, signature)

    @staticmethod
    def result_payload(task_id: str, lease_id: str, peer_id: str, result_hash: str) -> str:
        return result_payload(task_id, lease_id, peer_id, result_hash)
