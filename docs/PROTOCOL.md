# 本地协议 v0.1

## Envelope

```json
{
  "version": "0.1",
  "message_type": "TASK_ANNOUNCE | LEASE_OFFER | RESULT_SUBMIT | CREDIT_NOTICE",
  "sender": "peer-id",
  "task_id": "tsk_...",
  "payload": {}
}
```

当前 `LocalLoopbackTransport` 只做进程内队列，用于协议/测试；
`NetworkTransportNotImplemented` 对 send/receive 明确抛 `NotImplementedError`。
**没有 TCP/QUIC/libp2p socket 实现，也没有多设备验证。**

## Task

```json
{
  "task_id": "tsk_<uuid>",
  "task_type": "numeric_batch_v1",
  "payload": {"numbers": [1, 2, 3]},
  "status": "QUEUED",
  "attempts": 0,
  "max_attempts": 3,
  "lease_seconds": 30
}
```

任务类型封闭白名单：`numeric_batch_v1`。输入约束：

- `payload` 只能有一个键 `numbers`；
- 1..4096 个整数；
- 每个整数绝对值 ≤ 1,000,000,000；
- 拒绝 bool/float/string/额外字段。

输出：

```json
{"count": 3, "sum": 6, "sum_squares": 14}
```

## Lease

```json
{
  "lease_id": "lea_<uuid>",
  "task_id": "tsk_...",
  "peer_id": "peer-a",
  "status": "active",
  "expires_at": "2026-01-01T00:00:30+00:00"
}
```

单任务同时最多一个 active 租约；领取/过期在 SQLite 事务中完成。

## Result

```json
{
  "result_id": "res_<uuid>",
  "task_id": "tsk_...",
  "lease_id": "lea_...",
  "peer_id": "peer-a",
  "output": {"count": 3, "sum": 6, "sum_squares": 14},
  "content_hash": "<sha256 of canonical output>",
  "signature": "<hmac-sha256 over domain-separated payload>"
}
```

签名 payload：

```json
{"domain":"idle-compute-result-v1","task_id":"...","lease_id":"...","peer_id":"...","result_hash":"..."}
```

## Validation

- 协调器独立重算 `numeric_batch_v1`；
- 检查 output 相等、content_hash 相等、HMAC 签名有效；
- 任何一项失败 → `REJECTED`，不计分；
- 过期租约 → `REJECTED`，不计分。

## CreditEvent

```json
{
  "event_id": "evt_<uuid>",
  "event_type": "CREDIT",
  "peer_id": "peer-a",
  "task_id": "tsk_...",
  "amount": 1,
  "idempotency_key": "credit:<task_id>:<peer_id>",
  "prev_hash": "<previous event_hash or GENESIS>",
  "event_hash": "<sha256 of event body>",
  "signature": "<hmac-sha256 over event_hash>"
}
```

`idempotency_key` 唯一，重复验证/重复调用不会重复计分。

## P2P 接口（未实现）

```python
class Transport:
    def send(peer_id, message) -> None: ...
    def receive(peer_id) -> ProtocolMessage | None: ...
    def close() -> None: ...
```

未来 libp2p 层负责：Peer 身份、连接、发现、消息认证/加密、NAT、速率限制。
本地执行层只负责白名单任务、租约、结果和账本。两者通过上述消息边界连接。
第一阶段没有伪造这些能力。
