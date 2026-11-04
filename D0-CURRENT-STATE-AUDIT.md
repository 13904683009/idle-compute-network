# D0 现状审计（Current State Audit）

日期：2026-11-04　审计对象：`phase2` @ `479e8de`（293 测试全绿）
审计方式：逐文件阅读 + 实测命令（`git`、`grep`、`python -c inspect`、测试日志解析）。
**本文件只描述代码里真实存在的东西；历史报告的说法一律重新核对，不一致处单列（§10）。**

> 说明：本次执行在 DSH 工作区副本
> `C:\Users\linxi\Desktop\DSH工作区\workspace\idle-compute-network` 进行
> （正本 `C:\Users\linxi\AI-Work\workspace\idle-compute-network` 未写入，HEAD 与树哈希一致）。
> D0 阶段**未修改任何核心代码**，本文件与 D0 决策文档是唯一新增内容。

---

## 1. 一句话结论

现有代码是一个**单协调者、双向签名、可验证账本**的原型：任务协议、状态机、SQLite 持久化、
Ed25519 身份、16 消息线协议、TCP/libp2p 双传输、协调者/工人/客户端运行时、账本镜像与冲突隔离
都已存在且有测试。**但"能力声明 / 发现 / 匹配 / 双方租约 / 注册表"在代码里完全不存在**——
这些是 Stage D 要从零建立的部分，而不是"改造现有实现"。

---

## 2. 仓库与 Git 实测

| 项 | 实测值 |
| --- | --- |
| 分支 / HEAD | `phase2` / `479e8de`（"Phase 2 F-4 docs: phase F complete (293 tests)"） |
| 其他分支 | `main@74ed80f`（基线）、`dev@e3e6d5a`（Phase 1 MVP） |
| 远端 | 无（`git remote -v` 为空） |
| 跟踪文件 | 68 |
| 未提交 | 仅未跟踪的 `PROJECT-STATUS-AND-HANDOFF.md`（交付报告，非代码） |
| 测试 | `Ran 293 tests ... OK`（`logs/idle-compute/f4-full-suite-2.log`，332 s，0 跳过） |

---

## 3. 模块清单（实测 LOC，`idle_compute/` 共 20 个模块）

| 模块 | LOC | 职责 | 归属 |
| --- | --- | --- | --- |
| `node.py` | 1240 | 三角色运行时（coordinator actor / worker 循环 / client 操作）、HELLO 绑定、应用层处理器、账本同步与冲突锁存、限流接入、任务池上限 | Phase 2 C/E/F |
| `db.py` | 700 | Store：schema v2、状态机写入、租约、结果/验证、幂等、任务池计数 | Phase 1 + B/C |
| `protocol.py` | 656 | 帧编解码、信封、验证管线、16 消息/16 错误码、SeqWindow | Phase 2 C |
| `ledger.py` | 434 | 哈希链、幂等计分、逐行按方案验签、分页导出、镜像校验、冲突元数据 | Phase 1 + B/E |
| `net_transport.py` | 424 | libp2p 0.8.1 / trio 适配器（host + stream，64 流上限） | Phase 2 C |
| `cli.py` | 333 | 11 条 Phase 1 本地命令 + identity/peers/node/worker 命令 | Phase 1 + B/C/E |
| `transport_tcp.py` | 317 | 纯标准库 TCP 传输（64 流上限） | Phase 2 C |
| `config.py` | 286 | 强校验配置、对称默认拒绝、rendezvous 文件 | Phase 2 C |
| `coordinator.py` | 189 | Phase 1 本地编排（单进程路径） | Phase 1 |
| `crypto.py` | 173 | canonical_json、sha256、v1/v2 签名载荷构造 | Phase 1 + B |
| `identity.py` | 157 | Ed25519 身份：32 字节种子、node_id = libp2p peer id、无分发验签 | Phase 2 B |
| `ratelimit.py` | 136 | 每 peer 令牌桶 + 触发/隔离 | Phase 2 F-1 |
| `models.py` | 108 | 数据模型（Task/Lease/Result/Validation/CreditEvent/TaskStatus/LeaseStatus…） | Phase 1 |
| `signing.py` | 101 | `Ed25519Policy` / `HmacLegacyPolicy` | Phase 2 B |
| `log.py` | 86 | 日志脱敏规则 + handler 级过滤器 | Phase 2 F-4 |
| `p2p.py` | 63 | **Phase 1 遗留**的 Transport/PeerDirectory 抽象（`TASK_ANNOUNCE` 等 4 个名字，**不是**现行线协议） | Phase 1 |
| `state_machine.py` | 54 | 有界任务状态机（`ALLOWED` 表） | Phase 1 |
| `tasks.py` | 49 | `numeric_batch_v1` 执行器 + `TASK_TYPES` 封闭注册表 | Phase 1 |
| `transport_tcp.py`（重复） | — | 同上，见上 | — |
| `__init__.py` / `__main__.py` | 5 | 包入口 | Phase 1 |

> `p2p.py` 与 `protocol.py` 是两套东西：前者是 Phase 1 的占位抽象（4 种消息名），
> 后者是真正在跑的 16 消息线协议。D 阶段**不得**基于 `p2p.py` 设计新协议，
> 否则会造出第二套通信系统。

---

## 4. 任务生命周期（代码实测）

`state_machine.py:12-45` 的 `ALLOWED` 表：

```
CREATED → QUEUED → LEASED → RUNNING → SUBMITTED → VALIDATED → COMPLETED
            ↘        ↘        ↘          ↘
           CANCELLED/EXPIRED（按表规定）
SUBMITTED → REJECTED
```

- **写入路径**：`create_task` 直接以 `QUEUED` 落库（`CREATED` 只存在于表里，见
  `db.py::create_task`；`ARCHITECTURE.md` 也这样记录）。
- **租约**：`leases(lease_id, task_id, peer_id, status, expires_at, created_at)`，
  `one_active_lease` 部分唯一索引保证同一任务最多一个 active 租约（`db.py` schema）。
  **租约是协调者单方写库，没有请求方/工人双方签名**。
- **过期/重试**：`expire_leases` 把过期租约置 `expired`；任务 `attempts < max_attempts`
  回 `QUEUED`，否则 `EXPIRED`；SUBMITTED 崩溃窗口有专门处理（C 阶段审计修复）。
- **与 D 阶段目标状态的差距**：D 方案提出的 `DISCOVERING / MATCHED / LEASE_PROPOSED /
  LEASE_ACCEPTED / SETTLED` **在现有状态机里不存在**，而且现有 `ALLOWED` 表不允许这些状态。
  结论：**不重写任务状态机**；把"发现/匹配/报价/接受"建模为**租约协商层的状态**
  （新表/新内存对象），任务状态仍只在"租约被接受且 Worker 开始执行"时由协调者推进。
  映射见 `D0-ARCHITECTURE-DECISION.md §6`。

---

## 5. 线协议（代码实测）

- 帧：4 字节大端长度 + UTF-8 JSON；帧 64 KiB、body 32 KiB、JSON 深度 16（`protocol.py:42-44`）。
- 信封：`{v,type,sender,recipient,seq,ts,in_reply_to,body,signature}`，签名域
  `idle-compute-node-msg-v2`（`protocol.py::build_envelope` / `signing_payload`）。
- 16 种消息（`protocol.py:101-...`，`_BODY_KEYS` 精确键）：
  HELLO/HELLO_ACK、PING/PONG、ERROR、TASK_CREATE/TASK_CREATE_ACK、
  CLAIM_REQUEST/CLAIM_GRANT/CLAIM_DENIED、SUBMIT_RESULT/RESULT_ACK、
  STATUS_REQUEST/STATUS_RESPONSE、LEDGER_SYNC_REQUEST/LEDGER_SYNC_RESPONSE。
- 验证顺序（`protocol.py::parse_message`，测试 `test_protocol.py` 固定）：
  帧长 → JSON/深度 → 信封形状/类型 → BAD_SENDER（libp2p 传输身份）→ Ed25519 验签 →
  seq 窗口 `(last_seen, last_seen+4096]` → 时间戳 ±300 s（>600 s 丢弃）→ body 精确键 schema。
- **不存在**：能力公告、发现查询、任务需求、租约报价/接受消息。D 阶段要加新的消息类型
  （封闭注册表会从 16 增加）或复用现有 `TASK_CREATE/CLAIM` 语义——见决策文档。

---

## 6. 身份与签名（代码实测）

- `identity.py`：32 字节种子 `identity.key`（O_EXCL 竞态安全）；`node_id = ID.from_pubkey(kp.public_key)`，
  即 **node_id 就是 libp2p peer id，公钥内嵌**；验签不需要分发公钥
  （`ID.from_base58(sender).extract_public_key().verify`）。
- 三个签名域：信封 `idle-compute-node-msg-v2`、结果 `idle-compute-result-v2`、
  记账 `idle-compute-credit-v2`（`crypto.py` 的 builder）。
- 存储：`results.signature`、`credit_events.signature`、`credit_events.sig_algo`
  （`hmac-sha256` 或 `ed25519`）；网络路径只接受 `ed25519` 行。
- Phase 1 HMAC（`ledger.key`）仅本地 11 条命令使用，网络镜像硬拒 hmac 行。

---

## 7. 持久化（`db.py` SCHEMA，代码实测）

| 表 | 关键约束/字段 | 权威性 |
| --- | --- | --- |
| `meta` | `schema_version = '2'` | 迁移标记 |
| `peers` | PK peer_id | 协调者 |
| `tasks` | PK task_id、status、attempts、max_attempts、lease_seconds、result_id | 权威 |
| `task_shards` | FK task_id；单分片占位 | 权威（未用） |
| `leases` | FK task_id/peer_id；`one_active_lease` 部分唯一索引 | 权威 |
| `results` | `UNIQUE(task_id, lease_id)`、signature、sig_algo | 权威 |
| `validations` | `UNIQUE(result_id)` | 权威 |
| `credit_events` | `UNIQUE(idempotency_key)`、哈希链、signature、sig_algo | 权威账本 |
| `node_state` | 每 sender 的 `last_seq_sent/last_seq_seen` | 重放窗口 |
| `claims` / `client_refs` | 幂等表（租约/任务创建重试） | 幂等 |

工人侧另有 `ledger_copy` + `ledger_meta`（`ledger.py::LEDGER_COPY_SCHEMA`，派生数据，可删重建）。

**与 D 阶段的差距**：没有任何 `capabilities` / `announcements` / `leases(requester, worker, signatures)`
表。D 阶段需要 **additive 迁移（schema v3）**，不得改 v2 现有列语义。

---

## 8. 传输层能力边界（实测）

| 项 | TCP（`transport_tcp.py`） | libp2p（`net_transport.py`） |
| --- | --- | --- |
| 监听/拨号/双向流 | 有（IPv4、64 流上限、长度前缀） | 有（trio 线程 + `new_stream`，64 流上限） |
| 对端身份 | 占位符 `tcp:host:port`，HELLO 后绑定 | `muxed_conn.peer_id`，BAD_SENDER 先于验签 |
| 发现 | 无（配置静态地址 + rendezvous 文件） | 无（同样静态） |
| 库自带可复用的发现/路由面 | — | **已安装但未使用**：`libp2p/discovery`、`kad_dht`、`pubsub`、`relay`、`security`、`rcmgr`、`request_response`（`.venv/Lib/site-packages/libp2p/`） |
| `new_host(...)` 实参 | — | 只传 `key_pair`、`listen_addrs`；`inspect.signature(new_host)` 的默认值实测为 `enable_mDNS=False, enable_upnp=False, enable_autotls=False, enable_quic=False, bootstrap=None, disc_opt=None` |

**D0 结论**：
1. 不需要引入新 P2P 框架；libp2p 已有 DHT/gossip/relay 面，**MVP 也先不用**（先做静态 bootstrap + 公告）。
2. 代码**没有显式传** `enable_mDNS=False/enable_upnp=False`，目前靠库默认值——文档
   `DECISION-LOG.md:103` 的"处处 enable_mDNS=False"是**行为等价、非代码显式**。D2 应显式传参并加断言测试。
3. 传输层加密（Noise/TLS）由库默认 security 决定，**本项目未配置、未验证**，不得对外宣称已加密。
   安全审计进一步指出降级风险：`new_host` 未传 `sec_opt`（`net_transport.py:263-265`），而 libp2p 0.8.1 的
   默认 security 注册表包含 `/plaintext/1.0.0`；multistream 协商由应答方在注册列表中选择，发起方逐个尝试，
   安全协议被拒后**仍可能落到明文**（`.venv/Lib/site-packages/libp2p/__init__.py:653-665`、
   `security/generic_selector.py:99-106`）。仓库没有任何"协商出 Noise/TLS"的测试。
   **D1 必须显式 pin `sec_opt` 并把协商结果写成断言测试**（ADR-D0-12）；在那之前，对外口径只能是
   "应用层签名，不保证链路加密"。

---

## 9. 测试基线（实测，293 项）

| 测试文件 | 数量 | 覆盖主题 |
| --- | --- | --- |
| `test_protocol.py` | 46 | 帧/信封/验签顺序/seq 窗口/时间戳/16 类型/16 错误码 |
| `test_config.py` | 41 | 配置校验、对称默认拒绝、rendezvous |
| `test_store.py` | 30 | 状态机、租约、结果、验证、迁移、幂等 |
| `test_node.py` | 21 | 真实多进程 E2E、对抗线协议、重启恢复、shutdown |
| `test_ledger.py` | 21 | 哈希链、篡改、分页导出、镜像追加 |
| `test_identity.py` | 17 | 种子/派发/签名/交叉验签/ACL 警告 |
| `test_net_transport.py` | 16 | libp2p 适配器 + 真实子进程冒烟 |
| `test_cli.py` | 13 | 11 条本地命令 + identity/peers |
| `test_transport_tcp.py` | 10 | TCP 回退传输契约 |
| `test_crypto.py` | 9 | 载荷构造/金标准哈希/密钥管理 |
| `test_ledger_sync.py` | 9 | LEDGER_SYNC 分页/镜像一致/坏事件/断点续传 |
| `test_ratelimit.py` | 8 | 令牌桶/隔离/重连不可重置 |
| `test_docs.py` | 7 | 文档禁词门禁 |
| `test_signing.py` | 7 | 双策略 |
| `test_models.py` / `test_tasks.py` / `test_p2p.py` | 6 / 7 / 4 | 模型、白名单任务、Phase 1 抽象 |
| `test_log.py` | 5 | 脱敏规则 + 三进程捕获 |
| `test_source_safety.py` | 5 | 源码禁词/导入遏制/注册表分发 |
| `test_ledger_conflict.py` | 4 | 冲突隔离/重建/重放 |
| `test_abuse.py` | 3 | 任务池上限/流隔离/子进程虐待存活 |
| `test_wire_boundary.py` | 3 | 恶意载荷不进执行器 |
| **合计** | **293** | 0 跳过 |

---

## 10. 历史报告 vs 代码：实测不一致清单

| # | 历史说法 | 实测 | 处理建议 |
| --- | --- | --- | --- |
| 1 | `DECISION-LOG.md:103`"enable_mDNS=False、enable_upnp=False 处处" | 代码未显式传参，靠 libp2p 默认值（默认确实是 False） | D2 显式传参 + 断言测试 |
| 2 | 早期文档称"任务队列上限 1000" | F-2 之前代码里**不存在**该检查；F-2 已补（`node.py:MAX_ACTIVE_TASKS`） | 已修正，记录在案 |
| 3 | `README.md` 仍写 Phase 1（57 测试、HMAC 单机） | 实际 293 测试、双传输、网络命令 | D6/交付阶段更新 README |
| 4 | `docs/PROTOCOL.md` 旧版描述 v0.1 信封与 4 种消息 | 已重写为 v2 16 消息（E-3 完成） | 保持 |
| 5 | `p2p.py` 的 4 种 `MessageType` | 与现行 16 消息线协议**无关**，仅被 `test_p2p.py` 使用 | D1 明确冻结；不得在其上扩展 |
| 6 | Phase 1 文档称"P2P 未实现" | 传输与节点运行时就绪，但**发现/匹配**仍无 | D0 文档区分"传输已实现 / 发现未实现" |
| 7 | 双设备验证 | 从未在两台物理设备上执行 | 保持"未验证"标注 |
| 8 | `peers add` 写出的配置能被节点加载 | **不能**：`cli.py:85` 写成 `{node_id: {role, addrs}}` 字典，`config.py:196-197` 要求 `peers` 是 node_id **列表**。实测：带 role 的字典配置 `load_config` 报 `ValidationError: peers must be a list of node ids, got dict`。`test_cli` 只检查文件内容，从未加载它 | D2 必须先修（见决策 ADR-D0-13） |
| 9 | 入站消息校验 `recipient == 本机 node_id` | **不校验**：`node.py` 的 6 处 `recipient` 全部是**出站构造**；`protocol.py` 只校验形状 | D1 补校验（ADR-D0-11） |
| 10 | `VALIDATED` 作为任务行状态存在 | **不存在**：`db.py:668-669` 只做 `ensure_transition`，从不 `UPDATE tasks SET status='VALIDATED'`；行状态直接从 SUBMITTED → COMPLETED | 文档/图示按实况修正 |
| 11 | HELLO 广播全部监听地址 | **只发 1 个**（`node.py:560` `addrs[:1]`），而协议允许 8 个（`MAX_LISTEN_ADDRS`） | D2 若要 Relay/多地址需修正 |
| 12 | `p2p.py` 是现行 P2P 抽象 | **运行时死代码**：`idle_compute/` 内零 import；只有 `tests/test_p2p.py` 在用，且其 `MessageType`（TASK_ANNOUNCE 等 4 种）与现行 16 消息线协议无关 | D1 冻结，不得在其上扩展 |
| 13 | 长任务可续租 | **无 RENEW/心跳**：全包 grep `RENEW|renew` = 0 命中；租约到期即按 `_expire_locked` 回收 | D4 明确"不支持续租"的边界 |

---

## 11. 与 D 阶段目标的能力矩阵（逐项实测）

| D 阶段概念 | 代码中存在？ | 现有最接近的东西 | 结论 |
| --- | --- | --- | --- |
| Node Identity / 公钥绑定 | **有** | `identity.py`：node_id = libp2p peer id，公钥内嵌 | 直接复用 |
| 签名信封与验签顺序 | **有** | `protocol.py` + `identity.verify_signature` | 直接复用 |
| CapabilityManifest（版本/有效期/撤销/签名） | **无**（`grep capab/manifest` 仅命中 signer capability 标志） | 无 | 新增协议 + 新模块 |
| CapabilityAnnouncement（短时效/单调 seq） | **无** | `SeqWindow` 是消息级 seq，不是公告序号 | 新增（可复用 seq 窗口思路） |
| 节点发现（bootstrap/公告传播/缓存） | **无**（`grep discover` = 0） | 静态 `config.peers` + rendezvous 文件 | 新增发现层（复用现有传输） |
| CapabilityMatcher（硬约束+评分） | **无**（`grep matcher` = 0） | 无 | 新增纯函数模块 |
| TaskRequirement（资源需求协议） | **无**（`grep requirement` = 0） | `TASK_CREATE{task_type,payload,max_attempts,lease_seconds}` | 扩展消息体（新消息或 v3 字段） |
| TaskLease（双方签名） | **部分**：有 `leases` 表但**单方**、无签名 | `leases` 表 + `CLAIM_GRANT/RESULT_ACK` | 扩展：新 `lease_contract` 结构 + 双方签名，DB additive |
| CapabilityRegistry（可替换接口/模拟链） | **无**（`grep registry` 均为消息注册表/任务白名单） | 无 | 新增 `registry.py` 抽象 + `LocalRegistryAdapter` |
| 结果验证 / 贡献账本 | **有** | `db.validate_result` 独立重算 + `ledger.append_credit` 哈希链 | 直接复用 |
| 信誉 | **无** | 无 | D3+ 可选，纯本地统计 |
| 结算 | 内部积分（无代币） | `credit_events` | 复用，禁止代币/提现 |

**关键判断**：D 阶段是**加法**（新协议 + 新模块 + 一条新匹配/租约流程），
不需要重写 `state_machine.py`、`db.py` 的 v2 列、`ledger.py` 的链、`node.py` 的角色循环。
唯一的"改现有文件"是：`protocol.py` 增加消息类型/体 schema、`node.py` 挂新 handler、
`db.py` 做 v3 additive 迁移、`config.py` 增加可选字段、`cli.py` 增加命令。

---

## 12. D0 审计范围与限制

- 只读代码与文档，**未运行**需要网络的真实多设备场景（本机单机，无法验证 LAN/NAT）。
- libp2p 的 DHT/gossip/relay 只核对"包里有这些模块"，**未验证其在本项目 trio 0.30 环境下可用**；
  D2 若要用必须单独评估（与 Phase 2 的 libp2p 评估同规格）。
- 性能/并发数字未测（D0 不含压测）；293 项测试耗时 332 s 为单机回环。
- 架构与安全的独立审查意见见 `D0-ARCHITECTURE-DECISION.md` §10（两个只读审计 Agent 的输出摘要）。
