# 架构

## 范围

第一阶段是**单机、本地、可测试**的闲置算力网络原型。它验证协议数据模型、
任务生命周期、结果验证和贡献账本，不声称已经实现多设备 P2P。

## 组件

```text
CLI (idle_compute/cli.py)
  -> Coordinator (coordinator.py)
       -> Store (db.py)                 SQLite: tasks/leases/results/validations/events
       -> Worker (worker.py)            only whitelisted numeric_batch_v1
       -> Ledger (ledger.py)            hash chain + HMAC signatures + idempotency
  -> P2P boundary (p2p.py)              interfaces only; network not implemented
```

| 组件 | 职责 | 边界 |
| --- | --- | --- |
| `models.py` | Peer/Task/TaskShard/Lease/Result/Validation/CreditEvent | 纯数据，不含执行 |
| `state_machine.py` | 允许状态转换、终态 | 非法转换立即拒绝 |
| `db.py` | SQLite 持久化、事务、租约、重试、校验 | 只接受白名单任务类型 |
| `tasks.py` | `numeric_batch_v1` 确定性计算 | 输入结构/长度/数值边界 |
| `worker.py` | 本地执行白名单任务并签名 | 无 Shell/动态代码 |
| `ledger.py` | 事件链、幂等计分、验证、重建 | HMAC 本地完整性 |
| `coordinator.py` | 本地编排、状态视图 | 不自动联网 |
| `cli.py` | 本地命令 | 只输出 JSON，不输出密钥 |
| `p2p.py` | Transport/PeerDirectory 接口 | 网络层未实现，显式抛错 |

## 状态机

```text
CREATED -> QUEUED -> LEASED -> RUNNING -> SUBMITTED -> VALIDATED -> COMPLETED
                         |          |           |
                         +----------+-----------+-> QUEUED (retry) / EXPIRED (max attempts)
                                                \-> REJECTED (invalid result/signature)
QUEUED/LEASED/RUNNING/SUBMITTED -> CANCELLED
```

- `CREATED` 仅存在于状态机模型中；`create_task` 直接以 `QUEUED` 落库，
  任何写路径都不会产生 `CREATED` 状态。
- `max_attempts` 默认 3，范围 1..10；每次领取 attempts+1。
- 租约过期：若 attempts < max_attempts，回到 QUEUED；否则 EXPIRED。
- `SUBMITTED` 的结果在过期租约上会被拒绝，不产生贡献。
- 终态：COMPLETED、REJECTED、CANCELLED、EXPIRED。

## 数据库

| 表 | 关键约束 |
| --- | --- |
| `tasks` | PK task_id；状态/次数/租约秒数 |
| `task_shards` | 当前单分片；为后续多分片预留 |
| `leases` | `one_active_lease` 部分唯一索引 |
| `results` | `UNIQUE(task_id, lease_id)` |
| `validations` | `UNIQUE(result_id)` |
| `credit_events` | `UNIQUE(idempotency_key)`、哈希链、签名 |

SQLite 使用 WAL、外键、`BEGIN IMMEDIATE` 和 5 秒 busy timeout。
数据库与 `ledger.key` 默认在 `var/`，不提交 Git。

## 信任边界

- **任务输入不可信**：只接受白名单 schema，长度和数值有上限。
- **结果是声明，不是事实**：协调器独立重算后才可能计分。
- **CLI 操作者是本地管理员**：可以指定 DB/key 路径；任务本身不能指定文件路径。
- **P2P 网络不可信**：第一阶段未实现；未来必须在网络层做身份、重放和速率控制。
- **HMAC 密钥是本地秘密**：不得输出/提交；不是用户凭证。
