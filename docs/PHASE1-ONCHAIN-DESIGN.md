# Phase 1 技术细化：链上注册与承诺（无代币）

> **状态：设计草案（DESIGN DRAFT），尚未实现。**
>
> 本文档是 `docs/PUBLIC-CHAIN-ADR.md` 的 Phase 1 细化，**不改变当前代码**。当前仓库仍是
> 单 Coordinator + 本地 SQLite + 本地 CREDIT；没有链上注册、没有链上任务承诺、没有代币/
> 转账/escrow。任何实现都需单独评审、测试网验证与安全审计。
>
> 本文所有合约接口/数据模型均为**草案**，不是可部署代码。

- 关联：`docs/PUBLIC-CHAIN-ADR.md`、`docs/COORDINATION.md`、`docs/THREAT_MODEL.md`
- 阶段：Phase 1（开放注册 + 最小承诺；无代币）
- 主网：**不部署**；仅测试网/本地链

---

## 0. Phase 1 一句话范围

**用现有 EVM L2 测试网记录“谁注册了哪个 node、承诺了什么能力、锚定了哪个任务的 terms hash”，
任务执行、双签租约、结果验证和 CREDIT 仍完全在现有本地系统内完成。**

明确不做：

- 不做代币、不转账、不做 escrow/支付；
- 不做链上任务状态机、不做链上结果验证、不做链上争议；
- 不做多 Coordinator/验证者共识（Phase 2）；
- 不把任务 payload、用户隐私数据或私钥上链。

---

## 1. 链选型建议

### 1.1 推荐：现有 EVM L2 测试网

**首选：Base Sepolia（OP Stack L2，chain id 84532）**

理由：

1. **EVM 等价**：Solidity/Foundry/Hardhat、EVM 工具链与现有 Python 代码无冲突；
2. **成本低**：测试网 gas 便宜，Phase 1 频繁注册/承诺可承受；
3. **生态成熟**：公开 RPC、浏览器、faucet、索引器（The Graph 等）齐全；
4. **迁移路径**：同一套合约可迁移到 Base 主网；Phase 1 不上主网；
5. **无需自建验证者**：Phase 1 不需要共识/验证者运维，先把开放注册跑通。

已知代价（Phase 1 可接受）：

- OP Stack 排序器当前是中心化的；Phase 1 无资金/无 escrow，只做公开承诺，**不把排序器
  去中心化当作 Phase 1 目标**；
- 需要等待若干确认（建议 3–5 个 L2 block）再信任锚定；
- 需要可配置 RPC（见 §5.3），不能硬编码单一公共端点。

**备选：Arbitrum Sepolia（Nitro L2，chain id 421614）**

理由：EVM 兼容成熟、低费用、工具/浏览器完善、与 Base 类似的迁移路径；若 Base Sepolia
RPC 在部署环境不可达或 faucet 受限，可切换。两者选一即可，**不在 Phase 1 同时维护多链**。

不推荐 Phase 1 使用 Ethereum Sepolia L1：gas/确认成本高、对“只做注册/承诺”过重。

> 选型不是永久承诺；Phase 2 若需要多验证者/独立治理，再评估 Cosmos/BFT 应用链或 L2 合约
> 方案；本 Phase 不锁定终局。

### 1.2 链上不变量（Phase 1）

- 合约**非 payable**，没有 `transfer`/`send`/`call{value:}`/代币接口；
- 没有可升级代理（推荐 **immutable contract + 版本化重新部署**，降低管理员风险）；
- 只有注册/更新/停用/任务承诺四类写入；所有写入有事件；
- 所有哈希使用确定性编码（见 §4.4），链下可独立复算。

---

## 2. Phase 1 最小链上功能

### 2.1 节点注册（`NodeRegistry`）

每条记录：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `nodeKey` | `bytes32` | `keccak256(utf8(node_id))`，唯一主键 |
| `owner` | `address` | 注册钱包；可更新/停用该记录 |
| `role` | `uint8` | 1=worker, 2=client；Phase 1 不接受 coordinator 注册 |
| `tlsFingerprint` | `bytes32` | 节点入站 TLS 证书 SHA-256；无入站监听则为 `0x0` |
| `endpoint` | `string` | `host:port`；无入站监听可为空字符串，长度 ≤255 |
| `capabilityHash` | `bytes32` | 能力清单 canonical JSON 的 SHA-256；client 可为 `0x0` |
| `capabilityVersion` | `uint32` | 能力清单版本 |
| `updatedAt` | `uint64` | 区块时间戳 |
| `active` | `bool` | 停用后不再被 Coordinator 采纳 |

接口草案：

```solidity
interface INodeRegistry {
    event NodeRegistered(
        bytes32 indexed nodeKey, address indexed owner, uint8 role,
        bytes32 tlsFingerprint, string endpoint,
        bytes32 capabilityHash, uint32 capabilityVersion, uint64 updatedAt
    );
    event NodeUpdated(bytes32 indexed nodeKey, bytes32 tlsFingerprint,
                      string endpoint, bytes32 capabilityHash, uint32 capabilityVersion);
    event NodeDeactivated(bytes32 indexed nodeKey);

    function register(bytes32 nodeKey, uint8 role, bytes32 tlsFingerprint,
                      string calldata endpoint, bytes32 capabilityHash,
                      uint32 capabilityVersion) external;      // 非 payable
    function update(bytes32 nodeKey, bytes32 tlsFingerprint,
                    string calldata endpoint, bytes32 capabilityHash,
                    uint32 capabilityVersion) external;        // only owner
    function deactivate(bytes32 nodeKey) external;             // only owner
    function get(bytes32 nodeKey) external view returns (
        address owner, uint8 role, bytes32 tlsFingerprint, string memory endpoint,
        bytes32 capabilityHash, uint32 capabilityVersion, uint64 updatedAt, bool active
    );
    function listActive(uint256 offset, uint256 limit) external view returns (bytes32[] memory);
    function activeCount() external view returns (uint256);
}
```

**开放注册与防抢注**：

- 任何钱包可直接调用 `register`；同一 `nodeKey` 只能注册一次（更新需 owner）；
- 钱包地址与 Ed25519 node_id **不是同一密钥体系**：链上 owner 是运营者，node_id 的真实性由
  节点在 HELLO 中的 Ed25519 签名证明；链上记录只是发现/承诺，**不是身份授权**；
- 因此存在“他人抢注我的 nodeKey”风险：Phase 1 用速率限制 + 冲突监控 + 链下挑战；
  被抢注的节点仍无法通过 HELLO（签名不匹配），但可能造成注册表噪声。Phase 2 用
  注册押金/服务历史/验证者准入解决；
- 可选增强（不进最小合约）：注册时附带 `registrarSignature`，由开放 registrar 服务
  验证 Ed25519 挑战后签名；合约验证 ECDSA，仍保持任何节点可自助注册，但增加一个
  可替换的信任角色。Phase 1 先不做，记录为待决。

### 2.2 能力承诺（capability hash）

- `capabilityHash = sha256(canonical_json(CapabilityManifest 去掉签名字段))`；
- 规范：UTF-8、键排序、无空白、数字规范化；与现有 `capability` 的 canonical JSON
  保持一致（实现时复用同一 canonicalization，避免双重编码）；
- 链上只存 **hash + version**，完整 manifest 在节点公告中提供；Coordinator 必须校验
  「公告 manifest 的 hash == 链上 hash」后才把该节点纳入候选；
- 能力变更：更新记录并递增 `capabilityVersion`；旧版本可被链下索引器保留用于审计。

### 2.3 任务承诺（`TaskCommitments`）

- 触发时机：Coordinator 生成并签名 `TaskLeaseTerms` 后、执行前（或异步）；
- 链上只存 **task id + terms hash**（以及审计所需的最小字段），不存 payload/结果：
  `taskId`（bytes32，来自本地 task_id 的 keccak）、`taskHash`（任务内容/白名单参数 hash）、
  `termsHash`（双签 terms 的 canonical hash）、`requirementHash`（TaskRequirement hash）、
  `requester`（address）、`deadline`（uint64）、`anchoredAt`。
- 可选 `workerKey`：在 ACCEPT 后由 Coordinator 二次锚定 `TaskWorkerAnchored`；最小范围
  可先不做，保留事件扩展位。

接口草案：

```solidity
interface ITaskCommitments {
    event TaskAnchored(
        bytes32 indexed taskId, bytes32 taskHash, bytes32 termsHash,
        bytes32 requirementHash, address indexed requester,
        uint64 deadline, uint64 anchoredAt
    );

    function anchorTask(bytes32 taskId, bytes32 taskHash, bytes32 termsHash,
                        bytes32 requirementHash, address requester,
                        uint64 deadline) external;   // 非 payable，Phase 1 仅 anchor 角色
    function get(bytes32 taskId) external view returns (
        bytes32 taskHash, bytes32 termsHash, bytes32 requirementHash,
        address requester, uint64 deadline, uint64 anchoredAt
    );
}
```

- **唯一性**：`taskId` 只能锚定一次；重复调用 revert，避免覆盖历史；
- **写入权限**：Phase 1 由 Coordinator 的 anchor 钱包写入（单一 relayer）；任务哈希/terms
  由 requester/worker 签名，链上只做公开承诺；Phase 2 再改为多签/多验证者；
- **不阻塞执行**：链上锚定是异步 best-effort；worker 执行前只校验本地双签 commit，
  不等待链上确认；链不可用时任务仍可执行，锚定重试并由本地 `chain_anchors` 表记录
  待处理项；链只提供事后可验证性，不作为执行门（避免把 Phase 1 变成主网依赖）。

### 2.4 明确无代币/无价值转移

- 合约无 `payable`、无 ERC-20/721、无 `transfer`、无 `selfdestruct`、无 `delegatecall`；
- 不收取注册费/押金（Phase 1 用速率限制；测试网押金实验另立合约，不进入最小范围）；
- 不托管任务预算，不做 escrow；
- 不发行任何可转让资产；CREDIT 仍只在本地账本。

---

## 3. 与现有系统的衔接

### 3.1 Coordinator 读取链上注册表

新增只读链客户端（设计名 `ChainRegistryClient`），不改变现有 Store/租约语义：

1. **配置**（草案，全部可选，默认关闭）：

```json
"admission": {
  "mode": "static",                  // static | invite | chain | chain_or_invite
  "chain": {
    "rpc_url": "https://sepolia.base.org",
    "chain_id": 84532,
    "registry_address": "0x...",
    "confirmations": 3,
    "refresh_seconds": 30,
    "fallback": "invite"
  }
}
```

2. **同步**：后台线程/任务周期性调用 `listActive` + 事件索引，写入本地只读缓存
   `chain_nodes`（nodeKey、role、tlsFingerprint、endpoint、capabilityHash、version、
   block/tx、updatedAt）；缓存用于断链时读旧值，TTL 到期标记 stale。
3. **HELLO 准入**：
   - `mode=chain`：未在配置 `peers` 的节点，查询缓存中 `nodeKey=keccak(node_id)` 且
     `active`、role 匹配；通过后再照常做 Ed25519 HELLO 验证；
   - 若链上记录存在但 TLS/端点/能力 hash 与公告不一致，拒绝并记录；
   - `mode=chain_or_invite`：chain 未命中时回退到现有邀请码路径；
   - `mode=invite/static`：完全保持现状。
4. **能力校验**：节点公告的 manifest canonical hash 必须等于链上 `capabilityHash`；
   不匹配则不进入 matcher 候选（保留现有本地缓存回退策略另议）。
5. **失效/撤销**：链上 `active=false` 或 TTL 过期 → 从候选移除；已建立连接不强制断开，
   但新 HELLO/重连必须重新校验。
6. **不信任链上端点做拨号白名单**：endpoint 仅用于发现；入站仍受 peers/allowlist 与
   HELLO 签名约束；避免恶意注册把流量引到任意主机（SSRF/放大）。

### 3.2 开放注册替代邀请码

- 新增 CLI（设计）：
  ```bash
  python -m idle_compute chain register --config ... --role worker --rpc ... --key ...
  python -m idle_compute join --config ... --chain   # 不再需要邀请码
  ```
- `join --chain`：生成/加载 Ed25519 身份，读取注册表确认自身记录（或先 register），
  写本地配置；不再调用 `invite create`；
- **邀请码保留**：`invite`/`join --invite` 不删除，作为：
  - 私有/离线部署；
  - 链不可用或测试网故障时的 fallback；
  - 灰度期的人工准入。
- 迁移期：`mode=chain_or_invite`，先让链上注册节点加入，邀请码继续可用；观察后
  再把默认切到 `chain`（仍需单独 ADR/评审）。
- 本地 `peers` 静态白名单继续作为最高优先级/兜底；`joined_peers` 继续存在，链上注册
  不重写它，只在运行时扩展允许集合。

### 3.3 本地 CREDIT 与链上承诺的关系

- **Phase 1 不把 CREDIT 上链**；`ledger.py`、`credit_holds`、`credit_summary` 不变；
- 链上 `TaskCommitment` 只记录任务/terms 承诺，不记录金额/支付/余额；
- 建立本地映射（设计表 `chain_anchors`）：`task_id → chain_tx_hash, task_hash, terms_hash,
  block_number, status`；用于审计与重试；
- 未来 Phase 3 才考虑把 CREDIT 快照/Merkle 根锚定或迁移；Phase 1 不做兑换/转账；
- 现有 `credit` / `client-tasks` / `worker-tasks` 命令输出不受影响。

---

## 4. 数据模型与关键接口草案

### 4.1 链上（Solidity，草案）

```solidity
struct NodeRecord {
    address owner;
    uint8 role;                 // 1 worker, 2 client
    bytes32 tlsFingerprint;     // zero for outbound-only nodes
    string endpoint;            // host:port or ""
    bytes32 capabilityHash;
    uint32 capabilityVersion;
    uint64 updatedAt;
    bool active;
}

struct TaskCommitment {
    bytes32 taskHash;
    bytes32 termsHash;
    bytes32 requirementHash;
    address requester;
    uint64 deadline;
    uint64 anchoredAt;
}
```

### 4.2 链下缓存（SQLite，草案）

```sql
CREATE TABLE chain_nodes (
  node_key TEXT PRIMARY KEY,
  role TEXT NOT NULL,
  tls_fingerprint TEXT,
  endpoint TEXT,
  capability_hash TEXT,
  capability_version INTEGER,
  block_number INTEGER,
  tx_hash TEXT,
  updated_at TEXT,
  stale INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE chain_anchors (
  task_id TEXT PRIMARY KEY,
  task_hash TEXT,
  terms_hash TEXT,
  requirement_hash TEXT,
  tx_hash TEXT,
  block_number INTEGER,
  status TEXT NOT NULL,       -- PENDING | ANCHORED | FAILED
  updated_at TEXT
);
```

### 4.3 事件与索引

- 事件：`NodeRegistered`、`NodeUpdated`、`NodeDeactivated`、`TaskAnchored`；
- 索引器可选：The Graph subgraph 或自建 JSON-RPC poller；索引器只读，不作为执行权威；
- 所有链上字符串（endpoint）限制 ≤255，避免 calldata/存储滥用。

### 4.4 哈希规范

```
nodeKey        = keccak256(utf8(node_id))          // 0x + 64 hex
capabilityHash = sha256(canonical_json(manifest_without_signature))
taskHash       = sha256(canonical_json(task_payload + task_type + version))
termsHash      = sha256(canonical_json(TaskLeaseTerms_without_signatures))
requirementHash= sha256(canonical_json(TaskRequirement))
```
- canonical JSON 与现有 `content_hash`/`canonical_json` 保持同一实现；
- 链上 `bytes32` 使用原始 32 字节；日志/CLI 用 `0x` hex；
- 任何哈希计算失败都 fail closed，不写链。

### 4.5 关键接口（链下客户端）

```python
class ChainRegistryClient:
    def refresh(self) -> None: ...                 # listActive + events -> chain_nodes
    def lookup(self, node_id: str) -> ChainNode | None: ...
    def verify_capability(self, node_id: str, manifest_hash: str) -> bool: ...
    def anchor_task(self, task_id: str, task_hash: str, terms_hash: str,
                    requirement_hash: str, requester: str, deadline: int) -> str: ...
```

- RPC 超时/错误：重试指数退避；缓存 stale 时按 `admission.mode` 决定 fail-open/closed；
- 不把 RPC URL/私钥写日志；anchor 钱包私钥必须走环境变量/密钥管理，不入库。

---

## 5. 安全与反滥用（Phase 1）

### 5.1 最小反滥用（不引入价值）

Phase 1 合约**非 payable**，不做押金/转账；用以下组合：

1. **地址速率限制**：每个地址 `register`/`update` 冷却 N 个区块；每 epoch 最多 M 条；
2. **全局注册上限**：`activeCount` 上限（测试网可调），防止状态爆炸；
3. **字段校验**：nodeKey/role/tlsFingerprint 非零规则；endpoint 长度/字符集；
   capabilityHash 非零（worker）；重复注册 revert；
4. **Coordinator 侧准入**：链上注册不等于可执行；仍要求 HELLO 的 Ed25519 签名、
   能力 hash 匹配、TLS（如适用）与速率限制；
5. **索引器/监控**：异常注册速率、同 endpoint 大量节点、nodeKey 冲突告警；
6. **可选测试网押金实验**：单独合约/单独测试网，使用无价值 faucet token；**不进入
   Phase 1 最小合约**，避免把“无代币”变成事实上的代币门槛。

### 5.2 威胁与缓解（Phase 1）

| 威胁 | Phase 1 处理 |
| --- | --- |
| 抢注他人 nodeKey | 链上 owner 只代表运营者；HELLO 签名仍验证真实节点；监控冲突；Phase 2 押金/挑战 |
| Sybil 大量注册 | 地址冷却/epoch 配额/active 上限；Phase 2 押金/信誉 |
| 垃圾任务承诺 | Phase 1 仅 Coordinator anchor；`taskId` 唯一 + 失败重试；不开放任意写入 |
| 虚假能力 | capabilityHash 与公告 manifest 强校验；不匹配不进候选 |
| 链上端点诱导 SSRF | endpoint 仅发现，不用于任意拨号；入站仍走 peers/HELLO |
| 链重组/最终性 | 等待 confirmations；缓存记录 block/tx；回滚时标记 stale 并重同步 |
| RPC 单点/审查 | 可配置多 RPC/自建节点；缓存 + invite fallback |
| 合约漏洞 | 非 upgradeable 最小合约、测试网、审计、暂停开关（仅停写） |
| 隐私 | 只上链哈希/端点；不传任务 payload/结果/私钥；端点公开需告知运营者 |

### 5.3 运维与测试网要求

- 独立测试网钱包/私钥，绝不使用主网资产；
- RPC 可配置（Base/Arbitrum 公共端点或自建），失败可切换；
- 监控：注册速率、anchor 失败、链高度滞后、RPC 错误；
- 每个合约写入操作都有幂等/重试与本地 `chain_anchors` 状态；
- 不把 Phase 1 测试网结果当作主网可用或生产就绪。

---

## 6. Phase 1 验收与退出标准（设计目标）

- [ ] 测试网部署 `NodeRegistry` + `TaskCommitments`，源码/ABI 公开；
- [ ] 任意新节点可用钱包 + Ed25519 身份自助注册，无需邀请码；
- [ ] Coordinator 在 `chain` 模式下从注册表发现节点，完成 HELLO/租约/任务闭环；
- [ ] 能力 hash/terms hash 与链上承诺一致，可通过链下脚本复算；
- [ ] 任务锚定异步、可重试，链不可用不影响现有任务执行（fallback 生效）；
- [ ] 无任何代币/转账/escrow 函数；合约非 payable；
- [ ] 速率限制/字段校验/冲突监控通过滥用演练；
- [ ] 现有 563 项测试与邀请码路径不回归；新增链客户端/合约测试；
- [ ] 独立安全评审通过，才考虑 Phase 2。

---

## 7. 明确不做 / 待决

**不做**：代币、escrow、链上结算、链上任务状态机、多 Coordinator 共识、链上结果验证、
任意代码执行、主网部署。

**待决**：

1. Base Sepolia 还是 Arbitrum Sepolia 作为唯一 Phase 1 测试网；
2. 是否引入开放 registrar 做 Ed25519 验签（减少 nodeKey 抢注）；
3. Coordinator anchor 钱包的运维/轮换；
4. 邀请码 fallback 的默认策略（`chain_or_invite` 还是 `chain` 严格模式）；
5. 能力/任务哈希的最终 canonical 规范与版本号；
6. 链上事件索引器（The Graph vs 自建 poller）；
7. 与 D6 双设备验收的顺序（建议先完成 D6，再启动 Phase 1 测试网）。

> 再次声明：**本文档为设计，尚未实现；当前代码没有链上注册/任务承诺/代币/结算能力。**
