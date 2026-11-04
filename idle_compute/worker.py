"""Local worker for whitelisted tasks only."""

from __future__ import annotations

import time

from .crypto import content_hash, result_signing_payload
from .lease import ExecutionAuthorization, LeaseError, require_authorization
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

    def execute(
        self,
        task: Task,
        lease: Lease,
        *,
        authorization: ExecutionAuthorization | None = None,
        now: int | None = None,
        require_dual_signed: bool = False,
    ) -> tuple[dict, str, str]:
        # D4 execution gate: this is the ONLY path that runs a whitelisted task.
        # Dual-signed mode additionally requires a verified coordinator commit;
        # the legacy coordinator-grant path still binds the lease to this task
        # and worker, but is not a two-party signature and is labelled as such.
        moment = int(time.time()) if now is None else int(now)
        if authorization is None:
            if require_dual_signed:
                raise LeaseError(
                    LeaseError.NOT_AUTHORIZED,
                    "dual-signed lease mode requires a coordinator commit before execution",
                )
            if lease.task_id != task.task_id or lease.peer_id != self.peer_id:
                raise LeaseError(LeaseError.TASK_MISMATCH, "lease does not bind this task/worker")
        else:
            require_authorization(
                authorization,
                task_id=task.task_id,
                worker_node_id=self.peer_id,
                now=moment,
                require_dual_signed=require_dual_signed,
            )
        # Dispatch through the closed whitelist registry.
        task_type = get_task_type(task.task_type)
        output = task_type.execute(task.payload)
        digest = content_hash(output)
        signature = self.signer.sign(
            self._result_payload(task.task_id, lease.lease_id, self.peer_id, digest)
        )
        return output, digest, signature
