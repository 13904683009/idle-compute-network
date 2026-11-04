# D0 架构决策（Architecture Decision）

日期：2026-11-04　基线：`phase2` @ `479e8de`（293 测试全绿）
输入：`D0-CURRENT-STATE-AUDIT.md`（实测）+ 架构/安全两个只读审计 Agent 的审查（§10）。
**本文件冻结 D 阶段的模块边界与接口；D1 起才允许改代码，且必须按本文件的所有权表分工。**

---

## 1. 决策摘要（ADR-D0）

| # | 决策 | 理由（证据） |
| --- | --- | --- |
| ADR-D0-1 | **D 阶段是加法，不重写**：任务状态机、v2 数据库列、账本链、`node.py` 角色循环保持语义不变；新增能力/公告/匹配/租约/注册表模块与 additive 迁移 | `state_machine.ALLOWED` 与 `db.SCHEMA` 被 293 项测试锁定；重写等于把已验证的正确性推倒 |
| ADR-D0-2 | **协调者仍是任务权威与"请求方"**：D 阶段不引入"请求方与工人绕开协调者直连"的第二种权威；P2P 匹配发生在请求方侧（协调者或任一 client 角色），双方租约 = 请求方（协调者）+ 工人 | 现有 `one_active_lease`/claims 幂等/Store 单写者都是协调者中心；绕开会产生双权威与双花面 |
| ADR-D0-3 | **能力注册与匹配解耦于链**：`CapabilityRegistry` 是接口，MVP 用 `LocalRegistryAdapter`（独立 SQLite 文件 + 可配置最终性延迟），不得把本地表称为链上注册 | 方案第十节要求可替换；本轮不部署任何链 |
| ADR-D0-4 | **发现 MVP 不引入 DHT/Gossip/Relay**：复用现有签名信封与传输，新增 `CAPABILITY_ANNOUNCE / CAPABILITY_QUERY / CAPABILITY_RESPONSE` 三种消息；bootstrap 用静态 `peers` + rendezvous | libp2p 0.8.1 虽已装 `discovery/kad_dht/pubsub/relay`（实测目录存在），但本项目未验证其在 trio 0.30 下可用；MVP 先证明小规模闭环 |
| ADR-D0-5 | **公告 = 动态、短时效、单调序号**；清单 = 稳定、版本化、可撤销；两者**永不混用**：匹配器只接受"未过期公告 + 有效且未撤销的清单" | 方案第二/五节；过期公告参与匹配是明确的作弊面 |
| ADR-D0-6 | **规范化/哈希/签名沿用现有实现**：`crypto.canonical_json`（键排序、无多余空白、UTF-8）+ `sha256` 十六进制 + Ed25519（公钥内嵌 node_id）；新增独立域字符串 `idle-compute-capability-v1`、`idle-compute-announcement-v1`、`idle-compute-lease-v1` | 复用 `crypto.py`/`identity.py` 已被 17+9 项测试覆盖的实现，避免第二套规范化 |
| ADR-D0-7 | **版本语义**：`schema_version`（结构，破坏性变更才升）+ `capability_version`（节点自增，单调）+ `issued_at/expires_at`；同版本不同内容 = `CONFLICT`；低版本 = `ROLLBACK_REJECTED`；撤销 = `REVOKED` | 方案第四/十节；测试必须逐条固定 |
| ADR-D0-8 | **不引入**：新 P2P 框架、新区块链、代币/提现/金融功能、远程任意代码执行、OS 级"沙箱"宣称 | 项目硬约束；白名单任务 ≠ 沙箱（`THREAT_MODEL.md` 已写明） |
| ADR-D0-9 | **设备隐私默认最小化**：清单默认只发布任务类型/运行时/粗粒度硬件档位；不写真实 IP、主机名、用户名、绝对路径、序列号；精确型号需显式开启 | 安全审计；能力清单是公开可验证数据 |
| ADR-D0-10 | **一切新增状态可恢复**：公告缓存、注册表、租约协商都要有"过期/重启/断线"路径与测试；不接受只存在内存的权威状态 | 现有 `seq.json`/DB/镜像的先例 |
| ADR-D0-11 | **D 阶段所有入站消息必须校验 `recipient == self.node_id`**（现网只校验形状，`node.py` 的 `recipient` 全部是出站构造） | 安全审计：发给第三方的签名消息可被投递到本机；租约尤其必须绑定收件人 |
| ADR-D0-12 | **D1 显式 pin libp2p 安全选项**：`new_host(sec_opt=...)` 移除 `/plaintext/1.0.0`，并新增"协商出 Noise/TLS"的断言测试；在此之前对外只说"应用层签名，不保证链路加密" | 安全审计：默认注册表含 plaintext，multistream 可降级（`net_transport.py:263-265`；libp2p `__init__.py:653-665`、`security/generic_selector.py:99-106`） |
| ADR-D0-13 | **D2 先修 `peers` 配置形状冲突**：`peers add` 写字典、`load_config` 只收列表（实测报错）。统一为列表 + 新增可选 `directory: {node_id: {role, addrs}}`；必须带迁移说明与"写出后能被加载"的测试 | 实测：`ValidationError: peers must be a list of node ids, got dict`；`test_cli` 只查文件内容未加载 |
| ADR-D0-14 | **新模块禁止 import `libp2p` / `asyncio` / `socket`**：`capability.py`、`discovery.py`、`matcher.py`、`registry.py`、`lease.py` 只能依赖现有抽象（`crypto`、`identity`、`protocol`、`net_transport` 暴露的接口），否则 `test_source_safety` 直接红 | `tests/test_source_safety.py:61-80` 的既有门禁 |
| ADR-D0-15 | **注册表/结算不复用 `credit_events`**：该表只表达 `CREDIT`、`amount>0`、幂等键固定（`credit:{task}:{peer}`）。注册表自建表；若需锚定，单独 `registry_anchors`，不改账本格式 | `ledger.py:82-88`、`protocol.py:613` |
| ADR-D0-16 | **租约协商不并发写 `leases` 表**：提案/接受/拒绝先落 `lease_negotiations`；只有胜者在协调者 actor 线程内创建 `leases` 行（`one_active_lease` 仍是最终裁决）。候选并发提案是允许的，重复接受由唯一索引裁决 | `db.py:96-97`；方案第九节"多节点同时接受"要求 |
| ADR-D0-17 | **MVP 不支持续租**：租约时长与任务时长绑定并有上限校验；到期即回收重排。RENEW 列入 D6 之后，不在 D1–D4 增加 | grep `RENEW` = 0；`_expire_locked` 是既有语义 |

---

## 2. 目标架构（分层，MVP 边界）

```
┌─────────────────────────────── 链上抽象（MVP=本地模拟） ───────────────────────────────┐
│ CapabilityRegistry (接口)                                                              │
│   register / update / revoke / get / verify / registry_version                          │
│ LocalRegistryAdapter: 独立 SQLite + 最终性延迟模拟 + 版本/撤销/冲突/过期校验            │
│ 只存：node_id、manifest_hash、capability_version、注册状态、有效期、撤销、可选结算承诺  │
│ 不存：实时负载、显存、心跳、大输入/输出                                                 │
└───────────────────────────────────────┬─────────────────────────────────────────────────┘
                                        │ （只读校验）
┌───────────────────────────────────────▼─────────────────────────────────────────────────┐
│ 节点（Node）                                                                             │
│  身份 = Ed25519 / node_id 内嵌公钥（复用 identity.py）                                    │
│                                                                                          │
│  ┌── 能力层（新增） ───────────────────────────────────────────────────────────────┐    │
│  │ capability.py：CapabilityManifest（稳定）、CapabilityAnnouncement（动态）        │    │
│  │   规范化/签名/哈希/版本/过期/撤销/冲突                                            │    │
│  │ discovery.py：公告发布/接收/缓存/去重/过期清理/查询（复用签名信封与既有传输）      │    │
│  │ matcher.py：硬约束过滤 + 可解释评分（纯函数、确定性）                              │    │
│  └──────────────────────────────────────────────────────────────────────────────────┘    │
│                                                                                          │
│  ┌── 租约层（新增，叠加在现有任务状态机之上） ─────────────────────────────────────┐    │
│  │ lease.py：TaskLeaseTerms（请求方+工人双签）、提案/接受/拒绝/过期                   │    │
│  │ 协商状态：PROPOSED → ACCEPTED/REJECTED/EXPIRED（新表 lease_negotiations）           │    │
│  │ 工人接受后 → 现有 CLAIM/lease 行 + 现有状态机推进（不新增 TaskStatus）              │    │
│  └──────────────────────────────────────────────────────────────────────────────────┘    │
│                                                                                          │
│  ┌── 既有执行/验证/结算（复用） ────────────────────────────────────────────────────┐    │
│  │ node.py 角色循环、protocol.py 信封与 16 消息、db.py 状态机/租约/结果/验证            │    │
│  │ ledger.py 哈希链 + 幂等计分、worker 镜像与冲突隔离                                  │    │
│  └──────────────────────────────────────────────────────────────────────────────────┘    │
└───────────────────────────────────────┬─────────────────────────────────────────────────┘
                                        │ 传输（复用：transport_tcp / net_transport）
                            TCP / libp2p（回环已验证；跨设备未验证）
```

### 层的职责边界（必须遵守）

| 层 | 做什么 | 明确不做 |
| --- | --- | --- |
| 注册表抽象 | 保存可公开验证的**承诺**（哈希/版本/有效期/撤销/状态） | 实时资源、心跳、大文件、匹配查询 |
| P2P/发现 | 公告、查询、缓存、过期清理、节点状态 | 不裁决"谁可信"，只提供"可联系 + 有声明" |
| 匹配 | 硬约束 + 确定性评分 + 解释 | 不自行派发任务、不绕过租约 |
| 租约 | 双方签名、冲突仲裁（配合现有索引）、状态可恢复 | 不重写 `TaskStatus` |
| 执行/验证/结算 | 白名单执行、独立重算、幂等计分 | 不执行任意代码、不发币 |

---

## 3. 与现有代码的集成点（最小改动面）

| 文件 | 改动性质 | 内容 | 负责 Agent |
| --- | --- | --- | --- |
| `idle_compute/capability.py` | **新增** | Manifest/Announcement 模型、规范化、签名、校验、版本/撤销语义 | A2 |
| `idle_compute/registry.py` | **新增** | `CapabilityRegistry` 接口 + `LocalRegistryAdapter`（独立 SQLite） | A4 |
| `idle_compute/discovery.py` | **新增** | 公告缓存、发布/接收、去重、过期清理、查询、bootstrap 列表 | A3 |
| `idle_compute/matcher.py` | **新增** | `TaskRequirement`、硬约束、评分、解释 | A3 |
| `idle_compute/lease.py` | **新增** | 租约条款模型、双方签名载荷、提案/接受校验 | A2 |
| `idle_compute/protocol.py` | 扩展（**只加不改**） | 新消息类型 `CAPABILITY_ANNOUNCE / CAPABILITY_QUERY / CAPABILITY_RESPONSE / LEASE_PROPOSE / LEASE_ACCEPT / LEASE_REJECT` + body schema；封闭注册表 16 → 22 | A2 |
| `idle_compute/db.py` | additive 迁移 v3 | 新表 `capabilities`、`lease_negotiations`、`announcements`（如需持久）；**不改 v2 列** | A2/A4 |
| `idle_compute/node.py` | 扩展 | 挂新 handler；worker 接受租约；请求方匹配流程；启动时公告发布/清理循环 | A3 |
| `idle_compute/config.py` | additive | `capability`、`announce`、`discovery.bootstrap`、`registry` 段（全部可选，缺省值保持现行为） | A3 |
| `idle_compute/cli.py` | additive | `capability show/publish/revoke`、`discover list`、`match explain`、`lease list` | A7/A2 |
| `tests/*` | 新增 | 见 §11 计划；**不得删改既有 293 项** | A6 |

> **禁止**：改 `state_machine.py` 的 `ALLOWED`；改 `db.py` v2 列语义；改 `ledger.py` 链格式；
> 改 `protocol.py` 已有 16 种消息的 body 键；在 `p2p.py` 上扩展新协议。

---

## 4. 统一协议（D1 冻结的规则）

### 4.1 规范化 / 哈希 / 签名（全局唯一，禁止各 Agent 自行定义）

- **规范化**：`crypto.canonical_json(obj)` = `json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":"))`（实测：`crypto.py` 现有实现，金标准哈希测试锁定）。
- **哈希**：`sha256(canonical_json(payload).encode("utf-8")).hexdigest()`（= 现有 `content_hash`）。
- **签名**：Ed25519，签名对象是 `canonical_json({"domain": <域字符串>, ...payload})`；验证公钥来自 `node_id` 内嵌（`ID.from_base58(node_id).extract_public_key()`）。
- **域字符串**：`idle-compute-capability-v1`（Manifest）、`idle-compute-announcement-v1`（Announcement）、`idle-compute-lease-v1`（租约提案）、`idle-compute-lease-accept-v1`（工人接受）。
- **签名放置**：对象内 `signature` 字段（128 hex），**不参与**被签名载荷（先剔除 `signature` 再规范化）。
- **无效签名**：直接拒绝并计数；不进入缓存、不进入匹配、不回显内容。

### 4.2 `CapabilityManifest`（稳定能力，链上承诺）

字段（schema_version=1）：`schema_version, node_id, capability_version, issued_at, expires_at, supported_task_types[], supported_runtimes[], hardware{cpu_arch, cpu_cores?, ram_mb?, gpu_vendor?, gpu_memory_mb?}, resource_limits{max_cpu_cores, max_ram_mb, max_gpu_memory_mb, max_task_duration_seconds}, network{supported_transports[], supports_relay}, verification{level: SELF_DECLARED|BENCHMARKED|VERIFIED, benchmark_profile}, signature`。

规则：
1. `capability_version` 单调递增；低版本 = `ROLLBACK_REJECTED`；同版本不同内容 = `CONFLICT`（不是静默覆盖）。
2. `expires_at` 到期即失效；到期/撤销后**不得**参与匹配。
3. 注册表只存 `manifest_hash` 与元数据（版本/有效期/撤销/状态），原始清单由节点在 P2P 上提供并按哈希核对。
4. 隐私默认：`hardware` 的精确字段可省略/档位化；默认不写 IP/主机名/用户名/路径/序列号（ADR-D0-9）。

### 4.3 `CapabilityAnnouncement`（动态状态，P2P 传播）

字段：`schema_version, node_id, capability_version, sequence, observed_at, expires_at, available_resources{cpu_cores, ram_mb, gpu_memory_mb}, queue{running_tasks, queued_tasks}, network_status{connectivity, supported_transports}, accepting_tasks, signature`。

规则：
1. `sequence` 每节点单调递增；**旧序号拒绝**（缓存按 `(node_id, sequence)` 去重）。
2. 默认 `expires_at = observed_at + 60 s`、心跳 20 s；**可配置**，测试用极小值。
3. 过期公告不参与匹配；离线由"公告过期 + 连接失败"共同判定。
4. 公告是"自我声明"，**不构成可信证据**（层级见 §5.3）。

### 4.4 `TaskRequirement`（任务需求）

字段：`schema_version, task_id, task_type, task_version, input_manifest_hash, required_runtime, minimum_resources{cpu_cores, ram_mb, gpu_memory_mb}, preferred_resources{...}, constraints{max_execution_seconds, max_input_bytes, max_output_bytes}, data_policy{requires_private_data, allow_relay}, verification_policy{mode: RECOMPUTE, required}, deadline`。

规则：任务类型必须在 `TASK_TYPES` 白名单内；`task_type` 不在白名单 → 拒绝（不得进入匹配/执行）。

### 4.5 `TaskLease`（双方签名）

字段：`lease_id, task_id, requester_node_id, worker_node_id, task_spec_hash, input_manifest_hash, lease_version, issued_at, expires_at, execution_deadline, verification_policy, reward_policy, requester_signature, worker_signature`。

规则：
1. 提案签名（请求方）覆盖除 `worker_signature` 外的全部字段；接受签名（工人）覆盖 `{domain: lease-accept, lease_id, worker_node_id, task_spec_hash, expires_at}`。
2. 工人执行前必须校验：任务存在/未过期/租约有效/输入哈希匹配/未被本节点接受/本地资源足够/白名单内/双签有效（方案第九节 8 条）。
3. 多个工人同时接受 → 由现有 `one_active_lease` 唯一索引 + claims 幂等裁决；租约协商表记录败者（`REJECTED_CONFLICT`），不产生第二条 active 租约。

---

## 5. 发现与匹配（MVP）

### 5.1 发现

- **Bootstrap**：`config.discovery.bootstrap = [node_id|addr]`（缺省回落到现有 `peers`）；协调者/工人启动后与之 HELLO，交换 `CAPABILITY_QUERY/RESPONSE`。
- **公告传播**：向已知对端广播 `CAPABILITY_ANNOUNCE`；收到后验证签名/序号/有效期 → 入缓存 → （可选）转发给其他已知对端一轮（**限制跳数=1**，防放大）。
- **缓存**：按 `node_id` 保存最新 manifest hash + 最新公告；过期即失效；`capacity` 上限（默认 1024）防内存膨胀。
- **不做**：DHT、GossipSub、Relay、NAT 穿透、mDNS/UPnP（ADR-D0-4）。libp2p 包里有这些模块，D2 只做"是否可用的评估"，不接线。

### 5.2 匹配（两阶段，纯函数）

- **硬约束**（返回布尔，不评分）：task_type ∈ manifest.supported_task_types；runtime 匹配；`available.cpu_cores >= min.cpu_cores`；`ram_mb`；`gpu_memory_mb`；公告未过期；清单已注册且未撤销；`accepting_tasks == true`；`deadline` 可行。
- **评分**（0..100，可解释）：`compute_fit + memory_fit + network_fit + reliability_bonus - queue_penalty - transfer_penalty`；每项归一化区间写死并单测；输出 `explanation` 结构（每个分量与依据），便于日志与测试断言**确定性排序**。
- **协商**：候选集 > 0 后逐个发 `LEASE_PROPOSE`；工人回 `LEASE_ACCEPT/REJECT`；任一接受即锁定（幂等 + 唯一索引）；全部拒绝/超时 → `NO_CANDIDATE` 明确结果。

### 5.3 能力可信度分层（诚实标注）

`SELF_DECLARED`（默认）／`BENCHMARKED`（跑基准任务并记录）／`VERIFIED`（更强证据，MVP 不实现）。
**基准通过 ≠ 永久诚实**；信誉只作为评分辅助项，不作为唯一信任来源。

---

## 6. 状态机映射（不重写现有任务状态机）

| D 阶段概念 | 落在哪里 | 与现有 `TaskStatus` 的关系 |
| --- | --- | --- |
| `CREATED` | 请求方本地 | 映射现有 `QUEUED`（create_task 直接落 QUEUED） |
| `DISCOVERING / MATCHED` | **租约协商层**（新表 `lease_negotiations`） | 不影响 `tasks.status` |
| `LEASE_PROPOSED / LEASE_ACCEPTED` | 租约协商层 | 接受后协调者写 `claims` + 现有 `CLAIM_GRANT` 语义 |
| `RUNNING` | 现有 `TaskStatus.RUNNING` | 完全复用 |
| `RESULT_SUBMITTED / VERIFYING / VERIFIED` | 现有 `SUBMITTED / VALIDATED` | 完全复用 |
| `SETTLED` | 现有 `COMPLETED` + `credit_events` | 完全复用 |
| `REJECTED / CANCELLED / EXPIRED / FAILED / VERIFICATION_FAILED` | 现有 `REJECTED/CANCELLED/EXPIRED`；`VERIFICATION_FAILED` 映射 `REJECTED` | 不加新 `TaskStatus` |

> 若 D1 审查发现必须新增 `TaskStatus`，则**必须**同时提交"状态迁移表 + 兼容性说明 + 迁移测试"，
> 并等待项目负责人确认（本文件不授权）。

---

## 7. 注册表抽象（MVP）

```python
class CapabilityRegistry(Protocol):
    def register(self, manifest: CapabilityManifest) -> RegistryRecord: ...
    def update(self, manifest: CapabilityManifest) -> RegistryRecord: ...   # 版本语义
    def revoke(self, node_id: str, capability_version: int, reason: str, signer: NodeIdentity) -> RegistryRecord: ...
    def get(self, node_id: str) -> RegistryRecord | None: ...
    def verify(self, manifest: CapabilityManifest, now: int) -> VerificationResult: ...
    def registry_version(self) -> int: ...
```

`LocalRegistryAdapter`：
- 独立 SQLite 文件（默认 `<data_dir>/registry.db`），**不写协调者库**；
- 模拟最终性延迟：`finality_delay_ms`（默认 0，测试可设 50–500 ms）；`register/update` 返回 `PENDING` → 到期后 `FINALIZED`；
- 冲突/回滚/撤销/过期语义与 §4.2 一致；**只读查询**给匹配器；
- 文档必须标注"本地模拟注册表 ≠ 链上注册"。

---

## 8. 模块接口清单（D1 冻结）

```python
# capability.py
@dataclass(frozen=True)
class CapabilityManifest: ...
def manifest_payload(m: CapabilityManifest) -> dict          # 剔除 signature
def manifest_hash(m: CapabilityManifest) -> str             # sha256(canonical_json(payload))
def sign_manifest(m: CapabilityManifest, identity) -> CapabilityManifest
def verify_manifest(m, now: int) -> None                    # 抛 CapabilityError(code)
def manifest_from_dict(d: dict) -> CapabilityManifest

# announcement.py（或并入 capability.py）
@dataclass(frozen=True)
class CapabilityAnnouncement: ...
def sign_announcement(a, identity) -> CapabilityAnnouncement
def verify_announcement(a, now, last_seq: int | None) -> None

# discovery.py
class AnnouncementCache:
    def ingest(self, manifest, announcement, now) -> IngestResult   # 验证/去重/旧序号拒绝
    def lookup(self, task_type=None, runtime=None, now=None) -> list[PeerCapability]
    def expire(self, now) -> int
class DiscoveryService:
    def bootstrap_peers(self) -> list[str]
    def publish(self, announcement) -> None
    def query(self, peer_id, task_type=None, runtime=None) -> list[PeerCapability]

# matcher.py
@dataclass(frozen=True)
class TaskRequirement: ...
class CapabilityMatcher:
    def filter(self, req, peers, now) -> list[Candidate]
    def rank(self, req, candidates) -> list[ScoredCandidate]    # 确定性 + explanation
    def match(self, req, peers, now) -> MatchResult             # {"candidates": [...], "rejected": [...]}

# lease.py
@dataclass(frozen=True)
class TaskLeaseTerms: ...
def proposal_payload(t) -> dict; def accept_payload(t, worker_id) -> dict
def sign_proposal(t, requester) -> TaskLeaseTerms
def verify_proposal(t, now) -> None
def sign_accept(t, worker) -> TaskLeaseTerms
def verify_accept(t, now) -> None
```

---

## 9. Agent 分工与文件所有权（防冲突）

| Agent | 角色 | 独占文件（同一阶段只有它能改） | 交付 |
| --- | --- | --- | --- |
| A1 | 架构负责人（本会话主控） | `D0-*.md`、接口冻结、冲突裁决 | 架构/接口/决策、D1 审查 |
| A2 | 协议与数据模型 | `idle_compute/capability.py`、`lease.py`、`protocol.py`（新消息段） | 协议文档 + schema + 协议测试 |
| A3 | 发现与匹配 | `idle_compute/discovery.py`、`matcher.py`、`node.py`（新 handler 段）、`config.py` | 发现/匹配实现 + 测试 |
| A4 | 注册表抽象 | `idle_compute/registry.py`、`db.py`（v3 迁移段） | 接口 + LocalAdapter + 测试 |
| A5 | 安全审计（只读） | 无代码所有权 | 威胁模型增补、问题清单（转任务） |
| A6 | 测试与集成 | `tests/test_capability.py`、`test_registry.py`、`test_matcher.py`、`test_discovery.py`、`test_lease.py`、`tests/integration/*` | 测试计划 + 套件 + 原始日志 |
| A7 | 文档与交付 | `docs/phase2/D*.md`、`CAPABILITY-PROTOCOL.md` 等 | 协议/运行/交付文档 |

**规则**：A2/A3/A4 的改动文件不相交；`node.py` 由 A3 独占（A2/A4 只提供函数）；
每个 Agent 提交前必须跑 `unittest discover` 全量；A6 有否决权（测试不绿不得合并）。

---

## 10. 架构与安全审查摘要

两个只读审计 Agent 各读了一遍 HEAD 全仓（未改任何文件、未执行 git 写命令）。以下为要点；
每条都能在 `D0-CURRENT-STATE-AUDIT.md` 或对应源码行号复核。

### 10.1 架构审计（Agent 1）

- **模块地图**：21 文件 6399 行；`p2p.py` 在运行时**零 import**（仅 `test_p2p.py` 使用），其 4 种
  `MessageType` 与现行 16 消息线协议无关——不得作为 D 阶段协议基础。
- **复用/新增裁决**（与本文档一致，且更细）：
  Manifest/Matcher/Registry = NEW；公告/Requirement/Lease = EXTEND `protocol.py`/`models.py`/`db.py`；
  discovery 复用 `net_transport` 的 rendezvous + `config.peers` 目录，**不引入第二套 P2P 框架**。
- **八大风险**（原文顺序）：
  1. 封闭协议无版本协商（`v==1` 硬拒、键集精确）→ 新消息只能整网同步升级；
  2. 传输层无发现承载（`new_host` 未开 mDNS/DHT/pubsub，HELLO 只发 1 个地址）；
  3. 静态白名单拓扑使"发现"的价值被信任模型抵消；
  4. 单活跃租约 + 无 RENEW，"多候选并发发租约"必须先在协商层做（ADR-D0-16）；
  5. 租约是单方、未签名对象（`CLAIM_GRANT.lease` 仅 4 键）→ 两方租约必然触及线协议；
  6. 源码安全/文档门禁会拦住最自然实现（不得 import libp2p/asyncio/socket；文档不得声称 DHT/mDNS）；
  7. **`peers` 配置自相矛盾**（实测复现，见 §10.3）；
  8. 账本只表达 CREDIT + README 仍写 Phase 1 的"未实现"。

### 10.2 安全审计（Agent 2）

- **已防护**（有测试）：信封重放（持久化 seq 窗）、`sender≠transport peer`、时间偏斜、帧/体上限、
  令牌桶、任务池上限、日志脱敏、账本篡改、镜像冲突隔离。
- **未防护（按层）**：
  1. 传输加密**不可保证**（默认 security 注册表含 plaintext，可降级；`sec_opt` 未设置）；
  2. 入站**不校验 `recipient`**；
  3. 无所有权检查：任意 allowlisted worker 可 claim 任意任务并读到明文 payload；`STATUS_REQUEST`
     可查任意 task_id；`client_refs` 全局去重不含 sender；
  4. 协调者单点权威（无共识）；新 worker 首次镜像会接受"同钥重写的整链"；
  5. 尾删不可检测（既有测试已承认）；
  6. seq 持久化 best-effort，读写失败静默退回内存/0；同 DB 双 coordinator 可超发任务池；
  7. 限流可绕过：HELLO 前按连接计桶、64 流可被占满、`MAX_PEERS` 逐出隔离中的 peer；
  8. 执行器与 `identity.key` 同进程，无 OS 隔离（白名单 ≠ 沙箱）；
  9. payload 明文落库/上线；
  10. `lease_seconds` 由客户端给，可设 1 s 制造过期 churn。
- **D 阶段威胁 → MVP 缓解**（Agent 2 建议，已并入 ADR）：
  manifest 重放/回滚/同版本冲突 → `(node_id,version)` CAS + 冲突隔离；
  公告 seq 回滚 → 复用 SeqWindow 并持久化，命名空间 `ann:<node_id>`；
  伪造硬件 → 声明只作 hint，用挑战任务降权；matcher 投毒 → clamp 单节点份额；
  租约重放/双重接受 → 双签 + `one_active_lease` + worker 持久化已接受 lease；Sybil → 注册准入；
  registry 宕机 → 有界缓存 + 新租约 fail-closed。
- **CapabilityManifest 隐私建议**：高危字段（精确 RAM/显存/运行时补丁版本/GPU 型号/hostname/MAC）
  默认省略或分桶（cores ≤4 / 5-8 / 9-16 / >16；RAM 4/8/16/32/64+；显存 ≤8 / 9-16 / 17-24 / >24），
  只保留低危字段（cpu_arch、runtime 名 + 主次版本、能力 token）。已写入 ADR-D0-9 与 §4.2 第 4 条。

### 10.3 父 Agent 独立复现的审计发现（不是转述）

| 发现 | 复现方式 | 结果 |
| --- | --- | --- |
| `peers add` 产物不可加载 | 用 CLI 写出的字典形态配置调用 `load_config` | `ValidationError: peers must be a list of node ids, got dict` |
| 入站不校验 `recipient` | `node.py` 全文 6 处 `recipient` 全为出站构造 | 无任何 `env.recipient == self.identity.node_id` 比较 |
| HELLO 只广播 1 个地址 | `node.py:560` `addrs[:1]`（协议允许 8） | 确认 |
| `VALIDATED` 不落库 | `db.py:668-669` 只有 `ensure_transition`，无 UPDATE | 确认 |
| 无 RENEW | `grep -c 'RENEW\|renew'` = 0 | 确认 |
| `p2p.py` 运行时死代码 | `grep 'import p2p\|from \.p2p' idle_compute/` = 0 | 确认（仅测试引用） |

### 10.4 对 ADR 的影响

审计直接产生 ADR-D0-11（recipient 校验）、ADR-D0-12（pin 安全选项）、ADR-D0-13（peers 形状修复）、
ADR-D0-14（新模块不得 import libp2p/asyncio/socket）、ADR-D0-15（注册表不复用 credit_events）、
ADR-D0-16（协商层与 `one_active_lease` 分工）、ADR-D0-17（MVP 不支持续租）。

---

## 11. 25 个关键问题的逐项回答

| # | 问题 | MVP 处理 | 当前限制 | 后续升级 | 对应测试 |
| --- | --- | --- | --- | --- | --- |
| 1 | 节点如何获得 Bootstrap Peer | 静态 `config.discovery.bootstrap`（缺省回落 `peers`） | 手动配置 | 硬编码种子 + DNS + 链上引导 | `test_discovery::bootstrap` |
| 2 | Bootstrap 不可用怎么办 | 指数退避重试 + 保留上轮缓存（允许离线匹配旧公告？**不允许**，只允许已连接节点） | 无种子时节点孤立 | 多种子 + 链上兜底 + Relay | `test_discovery::bootstrap_down` |
| 3 | 如何发现新节点 | 已知对端交换 `CAPABILITY_RESPONSE`（跳数 1） | 规模小、无路由 | DHT/GossipSub（评估后） | `test_discovery::peer_exchange` |
| 4 | 公告如何传播 | 定向广播给已知对端 + 1 跳转发 | 非全网广播 | GossipSub | `test_discovery::announce_fanout` |
| 5 | 节点离线如何清理 | 公告 `expires_at`（60 s）+ 连接失败标记 | 最长 60 s 陈旧 | 心跳 + 主动注销 + 信誉 | `test_discovery::expiry` |
| 6 | 过期公告如何处理 | 匹配器过滤；缓存清理线程 | 时钟依赖 | 单调时钟/服务端时间 | `test_discovery::expired_not_matched` |
| 7 | 如何防止伪造能力 | 签名 + node_id 绑定 + 清单哈希注册 | 自报硬件无法证伪 | BENCHMARKED/VERIFIED + 抽样复跑 | `test_capability::forged_manifest` |
| 8 | 如何防止声明重放 | `capability_version` + `issued_at/expires_at` + 注册表版本 | 无链上时间 | 链上时间戳 + 撤销列表 | `test_capability::replay` |
| 9 | 如何防止版本回滚 | 低版本 `ROLLBACK_REJECTED`；同版本不同内容 `CONFLICT` | 本地注册表可被管理员改 | 链上不可变 + 多副本 | `test_registry::rollback` |
| 10 | 如何防止同一任务被多节点重复接受 | 现有 `one_active_lease` 唯一索引 + claims 幂等 + 租约协商表 | 协调者单点 | 分片/共识 | `test_lease::double_accept` |
| 11 | 如何防止同一结果重复计分 | `idempotency_key` UNIQUE + 镜像按 event_id 去重 | 协调者权威 | 外部锚点 | 既有 `test_ledger::duplicate_credit` |
| 12 | 不同节点提交不同结果怎么办 | 以协调者独立重算为准；冲突结果 `DUPLICATE`/`REJECTED` | 无第三方仲裁 | 多验证者抽样 | `test_lease::conflicting_results` |
| 13 | 输入数据太大怎么办 | 先传 `input_manifest_hash`，数据分块/离线传输不在 MVP；`max_input_bytes` 硬限 | 无内容寻址存储 | IPFS/分块 + 断点续传 | `test_matcher::input_too_large` |
| 14 | 上传带宽不足怎么办 | 评分含 `transfer_penalty`；数据在匹配前不传 | 无带宽测量 | 测速 + 直连优先 | `test_matcher::transfer_penalty` |
| 15 | NAT/防火墙怎么办 | MVP 仅回环/局域网直连；`supports_relay` 仅声明 | 无穿透 | Relay/AutoNAT（评估） | `test_discovery::nat_not_supported`（负向） |
| 16 | Relay 怎么办 | 只声明能力，不实现 | 无中继 | libp2p relay（评估后） | 负向测试 |
| 17 | 任务执行中断怎么办 | 现有租约过期重排队 + 协商表过期 | 无检查点 | 分片/续跑 | 既有 `test_node::abandoned_lease` |
| 18 | 节点重启怎么办 | 现有 seq/DB/镜像恢复 + 公告重新发布 | 公告 60 s 窗口 | 持久化公告 | 既有 `test_node::restart` |
| 19 | 恶意请求方怎么办 | 公告/租约签名；工人本地策略可拒绝；限流 | 无抵押/惩罚 | 信誉 + 押金（不引入代币） | `test_lease::malicious_requester` |
| 20 | 恶意 Worker 怎么办 | 结果独立重算 + 签名 + 租约过期 + 冲突隔离 | 无罚没 | 抽样复算 + 信誉 | 既有 `test_ledger_conflict` |
| 21 | 如何限制 CPU/内存/GPU | 白名单任务 + 输入上限 + 超时；**无 OS 级隔离** | 无资源 cgroup/Job Object | 子进程/容器/WASM | 既有 `test_tasks`；新增 `test_capability::limits` |
| 22 | 如何防止消息洪泛 | 令牌桶 + 帧/体上限 + 工作队列上限 | 洪泛仍耗带宽 | 每 IP 限速 + Relay 限速 | 既有 `test_ratelimit` |
| 23 | 如何保护设备隐私 | 清单默认粗粒度、不写指纹；公告不写地址 | 能力仍可指纹化 | 零知识/选择性披露 | `test_capability::privacy_defaults` |
| 24 | 注册表不可用怎么办 | 只读缓存 + 明确错误；匹配降级为"无注册信息则拒绝" | 无高可用 | 多副本/链上兜底 | `test_registry::unavailable` |
| 25 | 链上/链下状态冲突怎么办 | 注册表以 `manifest_hash` 为准；P2P 公告必须携带并匹配该哈希；不一致即拒绝 | 本地链可被改 | 链上最终性 + 审计锚点 | `test_registry::hash_mismatch` |

### 补充 backlog（安全审计新增，按风险排序，进 D1/D2）

1. 传输安全硬钉：`sec_opt` 去 plaintext + 协商断言（ADR-12，D1）。
2. 入站 `recipient` 校验（ADR-11，D1）。
3. 公告 seq 持久化（`ann:<node_id>` 命名空间，D2）。
4. registry 写入准入（allowlist/认证 + 按 node 限速），压 Sybil（D5）。
5. matcher 单节点份额 clamp + 历史准确率加权（D3）。
6. 限流绕过面：HELLO 前按连接计桶、64 流占满、`MAX_PEERS` 逐出隔离 peer（D2/D3 评估）。
7. payload 明文落库/上线（D4 起评估字段级加密，MVP 明确标注）。
8. `lease_seconds` 客户端可设 1 s → 服务端加下限/上限校验（D4）。
9. 外部锚点（定期把 `head_seq/hash` 签到独立日志）以关闭尾删（D6 之后）。

---

## 12. D1–D6 实施计划

| 阶段 | 目标 | 交付 | 验收（摘要） | 依赖 |
| --- | --- | --- | --- | --- |
| D1 统一协议 | Manifest/Announcement/Requirement/Lease 协议冻结；**先补两个现存缺口**：入站 `recipient` 校验（ADR-11）、libp2p `sec_opt` 硬钉去 plaintext（ADR-12） | 3 份协议文档 + schema + 规范化/签名/版本规则 + 协议测试 + 两个缺口修复 | 无效签名/过期/回滚/同版本冲突/重复消息全部被拒；`recipient` 不匹配被拒；安全协议协商有断言测试；293 项既有测试不回归 | D0 |
| D2 能力发现 | **先修 `peers` 形状冲突**（ADR-13），再做公告发布/接收/缓存/过期/查询 + bootstrap | `discovery.py` + `config` 迁移 + 测试 | `peers add` 写出的配置能被 `load_config` 加载；两节点互相发现；多进程发现；过期清理；旧序号拒绝；无效签名拒绝；重复公告不重复记录 | D1 |
| D3 能力匹配 | 硬约束 + 评分 + 解释 | `matcher.py` + 测试 | 硬约束排除；确定性排序；任务类型差异化；无候选明确返回；离线节点不参与；单节点份额 clamp（防投毒） | D1 |
| D4 租约闭环 | 双签租约 + 端到端 | `lease.py` + 协商表 + node 集成 + 集成测试 | 同一任务不被重复接受；过期/无效/错收件人租约不能启动；失败不计分；重复结果不重复计分；重启恢复；多进程 E2E | D2+D3 |
| D5 注册表 | 接口 + 本地适配器 | `registry.py` + 测试 | 匹配器不依赖链 SDK；注册/更新/撤销/版本冲突/过期/延迟语义；明确标注模拟 | D1 |
| D6 多设备验证 | 真实设备/网络 | 验证报告 + 证据 | 两设备发现/交换声明/匹配/执行/验证/积分；断线恢复有证据；**不得用本地多进程冒充** | D2–D5 |

每阶段独立提交；每提交附测试原始日志；A6 测试负责人有否决权。

---

## 13. D0 验收结果（对照方案第十六节）

| 验收项 | 状态 | 证据 |
| --- | --- | --- |
| 所有结论有代码路径或测试证据 | ✅ | 本文件与审计文档逐条引用 file:line / 测试名 / 命令输出 |
| 不得只根据历史报告判断 | ✅ | §10 列出 7 处历史报告与代码不一致，并给出实测 |
| 不得修改核心代码 | ✅ | 本轮仅新增 `D0-*.md`；`git status` 无代码改动 |
| 架构负责人与安全 Agent 完成审查 | ✅ | §10 两个只读审计 Agent 的审查摘要（见下） |
| 当前模块图 | ✅ | 审计文档 §3 |
| 当前任务生命周期 | ✅ | 审计文档 §4 |
| 当前 P2P 通信实现说明 | ✅ | 审计文档 §8（含 libp2p 已装但未用模块清单、`new_host` 默认值实测） |
| 当前身份与签名机制 | ✅ | 审计文档 §6 |
| 当前数据库结构 | ✅ | 审计文档 §7 |
| 当前测试结果 | ✅ | 审计文档 §9（293 项，分文件计数） |
| 差异清单 / 必改 / 可复用 / 风险 | ✅ | 审计文档 §11 + 本文档 §3/§10 |

---

## 14. 下一步（等负责人确认后才动代码）

1. 负责人确认 ADR-D0-1..10 与接口冻结（尤其 ADR-D0-2 "协调者仍是请求方"）。
2. 确认后进入 **D1**：只写协议文档与 schema 测试，不改 `node.py`/`db.py`。
3. D1 通过 A5 安全审查（签名/回滚/冲突/隐私逐条）后，才允许 D2–D4 并行实现。
