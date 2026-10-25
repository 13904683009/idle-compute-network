"""Local worker for whitelisted tasks only."""

from __future__ import annotations

from .crypto import content_hash, result_signing_payload
from .models import Lease, Task
from .signing import SigningPolicy
from .tasks import get_task_type


class Worker:
    def __init__(self, peer_id: str, signer: SigningPolicy) -> None:
        self.peer_id = peer_id
        self.signer = signer
        # The payload builder follows the signer's scheme: v1 for the legacy
        # HMAC path, v2 for the network Ed25519 path.
        self._result_payload = getattr(signer, "result_payload", None) or result_signing_payload

    def execute(self, task: Task, lease: Lease) -> tuple[dict, str, str]:
        # Dispatch through the closed whitelist registry.
        task_type = get_task_type(task.task_type)
        output = task_type.execute(task.payload)
        digest = content_hash(output)
        signature = self.signer.sign(
            self._result_payload(task.task_id, lease.lease_id, self.peer_id, digest)
        )
        return output, digest, signature
