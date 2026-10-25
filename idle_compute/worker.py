"""Local worker for whitelisted tasks only."""

from __future__ import annotations

from .crypto import LocalSigner, content_hash, result_signing_payload
from .models import Lease, Task
from .tasks import get_task_type


class Worker:
    def __init__(self, peer_id: str, signer: LocalSigner) -> None:
        self.peer_id = peer_id
        self.signer = signer

    def execute(self, task: Task, lease: Lease) -> tuple[dict, str, str]:
        # Dispatch through the closed whitelist registry.
        task_type = get_task_type(task.task_type)
        output = task_type.execute(task.payload)
        digest = content_hash(output)
        signature = self.signer.sign(
            result_signing_payload(task.task_id, lease.lease_id, self.peer_id, digest)
        )
        return output, digest, signature
