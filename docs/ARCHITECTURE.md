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

---

# 第二阶段（Phase 2）：节点运行时与网络层

第一阶段（上）是**单进程本地原型**。第二阶段把同一套 Store/状态机/账本搬到**真实多进程**
上运行：协调者、工人、客户端是独立进程，通过真实 TCP 或 libp2p 回环 socket 通信；
不引入新的状态权威，仍由协调者的 Store 做唯一写者。

## 组件（新增）

```text
CLI ─┬─ 11 条 Phase 1 本地命令（HMAC/--db/--key，未改）
     ├─ identity / peers
     ├─ node run / create-task / status / ping
     └─ worker ledger status
          │
        Node（node.py，按 config.role 决定行为）
          ├─ coordinator：actor loop（处理线程只解析/鉴权 → 有界队列 → Store）
          ├─ worker：claim 循环 + 账本镜像（ledger_copy.db）
          └─ client：一次性操作
          │
        Transport（config.transport 一行切换）
          ├─ transport_tcp.py   纯标准库 IPv4，64 并发流
          └─ net_transport.py   libp2p 0.8.1 / trio 0.30.0 适配器
```

| 模块 | 职责 | 边界 |
| --- | --- | --- |
| `protocol.py` | 信封/帧编解码、验证管线、16 消息类型、16 错误码、SeqWindow | libp2p-free；纯 stdlib |
| `identity.py` | 32 字节 Ed25519 种子 → node_id（= libp2p peer id）、签名/验签、O_EXCL 竞态安全 | 唯一允许导入 libp2p 的两处之一 |
| `signing.py` | `Ed25519Policy` / `HmacLegacyPolicy`（统一 sign/verify 接口） | 网络路径只用 Ed25519 |
| `net_transport.py` | libp2p host/stream 适配器 | 另一处 libp2p 导入点 |
| `transport_tcp.py` | 回退传输（零非标准库导入） | 与 libp2p 同契约 |
| `config.py` | 强校验配置、对称默认拒绝、rendezvous 文件 | 未知键/重复 db_path 拒绝 |
| `node.py` | 三角色运行时、双向 HELLO、应用层处理器、工人循环、镜像与冲突锁存 | 单写者：Store 只在协调者 loop 线程 |

## 数据布局（每进程独立）

| 文件 | 角色 | 说明 |
| --- | --- | --- |
| `<data_dir>/identity.key` | 网络角色 | 32 字节种子；与配置 node_id 不符则拒启动 |
| `<data_dir>/coordinator.db` | 协调者 | 权威 Store（任务/租约/结果/事件/claims/node_state/client_refs） |
| `<data_dir>/ledger_copy.db` | 工人 | **派生**镜像（`ledger_copy` + `ledger_meta`），可删可重建 |
| `<data_dir>/seq.json` | 工人/client | 出站序号游标（`os.replace` 原子写） |
| `<data_dir>/run/<node_id>.addr` | 协调者 | rendezvous 地址文件 |

**不共享 DB 文件**：配置校验拒绝同一 `db_path` 分给多个角色（SQLite 单写者 + Windows 锁）。

## 线程模型

- 协调者 actor：网络处理线程只做「帧解析 → 验签 → 白名单 → 入队」，业务（Store 调用）
  全部在 loop 线程执行；`Store` 与镜像连接**懒创建于各自 loop 线程并在同一线程关闭**
  （sqlite3 线程亲和性）。
- 每个 peer 流一个处理线程（TCP）或 trio 任务（libp2p），上限 64。
- 工人循环：拨号 → 双向 HELLO → PING → 账本同步（启动时全链重审）→ claim → 执行 → 提交；
  1..8 s 有界退避重连。

## 信任边界（第二阶段）

- **网络输入不可信**：先验签后解析；序号窗 + 时间戳容差 + 64 KiB/32 KiB 上限。
- **协调者仍是唯一权威**：工人只验证、不裁决；分歧即停（见 `COORDINATION.md`）。
- **工人镜像是派生数据**：可删除重建；重建时逐事件验签，不信任本地文件。
- **身份 = 密钥**：`node_id` 内嵌 Ed25519 公钥，无密钥分发；白名单是 node_id 静态列表。
- **传输加密未验证**：应用层签名独立生效；不得把"有签名"当作"链路已加密"。

## 验证状态

- 已实现且测试通过：协议/双传输/运行时/任务线上生命周期/镜像/冲突隔离/重建（263 项测试）。
- 未验证：跨设备（LAN/WAN）通信；libp2p 传输层安全参数；长时间压测与限流（F/G 阶段）。

