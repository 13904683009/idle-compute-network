# D4 阶段报告（租约协商与执行接线）— **部分完成 / 未通过验收**

日期：2026-11-04　基线：`phase2` @ `886e7dc`（D3，419 测试）
状态：**D4 核心模型、协议消息、DB 状态机与执行授权门已实现并通过测试；
节点接线（claim 流程改造、LEASE_* 消息分发、配置开关、取消/恢复语义）尚未完成。
D4 验收 27 项中约一半有实现与测试，其余未实现。未开始 D5/D6。**

---

## 1. 开工检查与基线

| 项 | 实测 |
| --- | --- |
| 起始 HEAD | `886e7dc`（D3 report） |
| 工作区 | 干净；仅未跟踪 `PROJECT-STATUS-AND-HANDOFF.md`（26 KB，未删改） |
| remote | 无；未 push/fetch/pull，无历史改写 |
| D3 基线 | **419/419 OK，341.497 s**（`logs/idle-compute/d4-baseline-419.log`） |
| 本轮全量 | **445/445 OK，344.957 s**（`logs/idle-compute/d4-full-suite.log`），0 failed / 0 skipped，compileall 0 |

## 2. 已完成（有代码 + 测试）

### 2.1 协议（封闭注册表 19 → 24，全部定向、禁止广播）

| 类型 | body（精确键） | recipient |
| --- | --- | --- |
| `LEASE_PROPOSE` | `lease_id, task_id, terms, task{task_type,payload}` | 必须定向 |
| `LEASE_ACCEPT` | `lease_id, terms_hash, acceptance` | 必须定向 |
| `LEASE_REJECT` | `lease_id, reason` | 必须定向 |
| `LEASE_COMMIT` | `lease_id, terms_hash, commit` | 必须定向 |
| `LEASE_CANCEL` | `lease_id, reason` | 必须定向 |

- Schema：嵌套对象 ≤8 KiB，`terms_hash` 必须 64 hex，`lease_id` ≤64，`reason` ≤200；
  `DIRECTED_TYPES` 已包含全部 5 种 → `recipient=null` 直接 `INVALID_BODY`，错投 `UNAUTHORIZED`。
- `tests/test_protocol.py` 更新类型计数 19→24、新增 5 种 shape 样本。

### 2.2 签名模型（`idle_compute/lease.py`）

- 复用 D1 `TaskLeaseTerms`（requester 签条款；worker 签 5 字段接受绑定），未改域/规范化。
- 新增 `LeaseCommit`（域 `idle-compute-lease-commit-v1`）：授权方（MVP 即 coordinator/requester）
  对 `{lease_id, task_id, requester_node_id, worker_node_id, terms_hash, committed_at, expires_at}` 签名，
  把"coordinator 生效确认"变成可验证对象。
- 新增 `ExecutionAuthorization` + `require_authorization(...)`：校验 task/worker 绑定、terms_hash、
  过期、双签模式；双签模式必须携带并验证 `LeaseCommit` 签名，否则 `NOT_AUTHORIZED`。
- `authorization_from_commit(commit, terms, ...)`：只有 commit 与 terms 完全绑定才产出授权。

### 2.3 执行授权门（不可绕过的工作入口）

- `Worker.execute(task, lease, *, authorization=None, now=None, require_dual_signed=False)`
  是所有白名单任务的**唯一执行入口**（测试中无其他路径能执行任务）。
- 双签模式（`require_dual_signed=True`）：无授权 → `NOT_AUTHORIZED`；授权必须通过
  `require_authorization`（含 `LeaseCommit.verify`）。
- legacy 模式：仍强制 `lease.task_id == task.task_id` 且 `lease.peer_id == worker`，
  但**不是双方签名**，文档明确标注为较弱路径。

### 2.4 持久化（DB schema v2 → v3，增量）

- 新表 `lease_negotiations`（lease_id PK、task/requester/worker/authority、terms_json/hash、
  双方签名、acceptance_json、state、reason、时间戳），索引：
  `one_open_negotiation UNIQUE(task_id) WHERE state IN ('PROPOSED','ACCEPTED')`。
- 状态机（Store 方法，全部在 `transaction()` 内）：`PROPOSED → ACCEPTED → ACTIVE`，
  `PROPOSED/ACCEPTED → REJECTED | CANCELLED | EXPIRED`。
- `commit_negotiation` 原子地：校验 ACCEPTED + 未过期 + task QUEUED → 任务 LEASED（attempts+1）
  → 用**协商的 lease_id** 插入 `leases`（active）→ 协商 ACTIVE；唯一索引保证不会有两个 ACTIVE。
- `accept_negotiation` / `reject_negotiation` / `cancel_negotiation` 幂等；
  `commit_negotiation` 对已 ACTIVE 幂等。
- 迁移：`CREATE TABLE IF NOT EXISTS` + `schema_version=3`；旧 v2 库打开即升级；
  新增 v2→v3 迁移测试。**注意**：为配合受控的 schema 升级，既有
  `test_store.py::test_v1_database_migrates_in_place_to_schema_v2` 断言由 `"2"` 改为 `"3"` 并改名为
  `..._to_schema_v3`（这是 schema 版本升级的必要更新，不是安全断言弱化，已在报告中披露）。

### 2.5 测试（新增 26 项，`tests/test_lease_lifecycle.py`）

签名：完整双签流程、条款篡改、接受签名重放到其他 lease/task、错误 worker、commit 过期、
commit/terms 绑定；执行门：合法双签通过、缺授权、legacy 在双签模式被拒、task/worker 错配、
授权过期、commit 签名篡改、`Worker.execute` 双签模式下无 commit 拒绝、legacy 绑定检查；
Store：v3 schema、v2→v3 迁移、PROPOSED→ACCEPTED→ACTIVE、一个 task 只能有一个开放协商、
accept/reject/cancel 幂等、未 accept 不能 commit、commit 幂等、过期不能 commit、
cancel/reject 后不能 commit、未知协商。

## 3. 未完成（D4 未通过验收的原因）

| 缺失项 | 影响 |
| --- | --- |
| **节点接线**：coordinator 在 `CLAIM_REQUEST` 后发 `LEASE_PROPOSE`、处理 `LEASE_ACCEPT` 后 `LEASE_COMMIT`；worker 端 `_claim_cycle` 双签分支 | 目前线上仍走 D2 的 `CLAIM_GRANT`；双签租约**没有真实网络闭环** |
| `config.lease`（mode/ttl）与角色校验 | 无法通过配置启用双签模式 |
| `LEASE_REJECT` / `LEASE_CANCEL` 的节点处理与取消竞态 | 取消/拒绝语义只在 Store 层可用 |
| 恢复语义：coordinator 重启后协商状态、worker 重启后不执行失效租约 | 未实现/未测试 |
| 并发竞争（两个 worker 同时 accept、accept vs expire、cancel vs execute） | 只有 Store 层唯一约束，缺真实并发测试 |
| 结果防重放/重复计分与租约的联合验证 | 沿用既有结果路径，未针对租约模式补测试 |
| 错投/广播的**节点级**拒绝测试 | 协议层已有，节点级未覆盖 |
| 文档 | `TASK-LEASE-PROTOCOL.md` 的 D4 章节未写 |

因此：**D4 验收表中第 1、10、11、13、16、17、19、20、22、23、26、27 等项未满足**；
已满足的是签名、执行门、Store 状态机与迁移相关项。

## 4. 风险与限制

1. 协议注册表已有 5 种租约消息，但 `node.py` 分发集合未包含它们 → 当前节点收到会回
   `REJECTED: does not handle ...`，不会执行或改状态（fail-closed），但也不构成功能。
2. 双签模式尚未启用，生产路径仍是 legacy `CLAIM_GRANT`；**不得宣称当前任务调度已经是
   双方签名授权**。
3. DB 版本已升到 3；旧 v2 库可升级，但**降级回 v2 未验证**（本项目无降级需求，需明确）。
4. `Worker.execute` 的 legacy 分支只做绑定检查，安全性弱于双签；后续应把双签设为唯一模式。
5. D6 双设备未执行。

## 5. 结论与建议

- **D4 未完成，未通过验收。** 本轮交付的是"可验证的租约核心 + 持久化状态机 + 执行授权门"，
  尚缺把 `LEASE_PROPOSE/ACCEPT/COMMIT` 接到真实 claim 流程，以及配套的并发/恢复/取消测试。
- **建议下一步（D4 收尾，仍属 D4 范围）**：
  1. `config.lease.mode`（默认 legacy，双签可选）+ 角色/对端校验；
  2. coordinator：`CLAIM_REQUEST` → 构造并签名 terms → `LEASE_PROPOSE`；`LEASE_ACCEPT` →
     校验 terms_hash/worker/签名 → `commit_negotiation` → 签名 `LeaseCommit` → `LEASE_COMMIT`；
  3. worker：`_claim_cycle` 双签分支 → 校验 terms/过期/身份 → 签名 accept → 等 commit →
     `authorization_from_commit` → `Worker.execute(require_dual_signed=True)`；
  4. 取消/拒绝/过期/重启/并发（两个 worker 抢同一 task、accept vs expire、cancel vs execute）；
  5. 节点级 recipient/广播拒绝 + 结果防重放测试；补 `TASK-LEASE-PROTOCOL.md` D4 章节。
- **不开始 D5/D6，等待审计确认后再继续 D4 收尾。**
