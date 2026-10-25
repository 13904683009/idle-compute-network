"""P2P protocol boundary.

Only in-memory loopback is implemented. A real libp2p transport is NOT
implemented; NetworkTransportNotImplemented explicitly fails instead of
pretending multi-device support exists.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

PROTOCOL_VERSION = "0.1"


class MessageType(str, Enum):
    TASK_ANNOUNCE = "TASK_ANNOUNCE"
    LEASE_OFFER = "LEASE_OFFER"
    RESULT_SUBMIT = "RESULT_SUBMIT"
    CREDIT_NOTICE = "CREDIT_NOTICE"


@dataclass(frozen=True)
class ProtocolMessage:
    version: str
    message_type: MessageType
    sender: str
    task_id: str
    payload: dict[str, Any] = field(default_factory=dict)


class Transport(ABC):
    @abstractmethod
    def send(self, peer_id: str, message: ProtocolMessage) -> None:
        raise NotImplementedError

    @abstractmethod
    def receive(self, peer_id: str) -> ProtocolMessage | None:
        raise NotImplementedError

    @abstractmethod
    def close(self) -> None:
        raise NotImplementedError


class NetworkTransportNotImplemented(Transport):
    """Explicit placeholder for the future libp2p layer."""

    def send(self, peer_id: str, message: ProtocolMessage) -> None:
        raise NotImplementedError("network transport is not implemented in phase 1")

    def receive(self, peer_id: str) -> ProtocolMessage | None:
        raise NotImplementedError("network transport is not implemented in phase 1")

    def close(self) -> None:
        return None


class LocalLoopbackTransport(Transport):
    """In-process queue used only by tests and local demos."""

    def __init__(self) -> None:
        self._queues: dict[str, list[ProtocolMessage]] = {}

    def send(self, peer_id: str, message: ProtocolMessage) -> None:
        if message.version != PROTOCOL_VERSION:
            raise ValueError(f"unsupported protocol version: {message.version}")
        self._queues.setdefault(peer_id, []).append(message)

    def receive(self, peer_id: str) -> ProtocolMessage | None:
        queue = self._queues.get(peer_id, [])
        return queue.pop(0) if queue else None

    def close(self) -> None:
        self._queues.clear()


class PeerDirectory(ABC):
    @abstractmethod
    def list_peers(self) -> list[str]:
        raise NotImplementedError


class LocalPeerDirectory(PeerDirectory):
    def __init__(self, peers: list[str] | None = None) -> None:
        self._peers = list(peers or [])

    def list_peers(self) -> list[str]:
        return list(self._peers)
