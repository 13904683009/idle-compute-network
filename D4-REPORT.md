# D4 阶段报告（双签租约闭环）— 收尾版

日期：2026-11-04　起点：`0c69e4e`（D4 core，445 测试）
状态：**节点级双签租约闭环已接通，默认 fail-closed；真实两进程执行/验证/结算通过。
部分并发/恢复验收项仍未覆盖（见 §5），建议审计后再进入 D5。**

---

## 1. 基线与结果

| 项 | 值 |
| --- | --- |
| 起始 HEAD | `0c69e4e` |
| 基线 | **445/445 OK，362.988 s**（`logs/idle-compute/d4-close-baseline-445.log`） |
| 最终 | **446/446 OK，390.806 s**（`logs/idle-compute/d4-final-full-suite.log`），0 failed / 0 skipped |
| compileall | exit 0 |
| 门禁 | `tests/test_source_safety.py`、`tests/test_docs.py` 相对 D0 逐字节未变；`state_machine.py`、`ledger.py` diff = 0 |

## 2. 真实链路（默认 `lease.mode=dual_signed`）

```
worker --CLAIM_REQUEST--> coordinator
       <--CLAIM_GRANT（兼容通知，不构成执行授权）
       <--LEASE_PROPOSE（terms 由 coordinator 以 requester 身份签名）
       --LEASE_ACCEPT（worker 签名，acceptance 内附 signature）-->
       <--LEASE_COMMIT（coordinator 签名，绑定 lease/task/双方/terms_hash）--
       → 构造 ExecutionAuthorization → Worker.execute(require_dual_signed=True)
       --SUBMIT_RESULT--> 现有独立验证 → 账本（单次 CREDIT）
```

证据：`tests/test_lease_network.py`（真实两个子进程、TCP 回环）断言：
任务 `COMPLETED`、`lease_negotiations.state == ACTIVE`、无残留 ACTIVE lease、
恰好 1 条 credit 事件、结果行存在、worker 进程仍存活。耗时 ~2.5 s。
过程中发现并修复：`acceptance` 未携带 worker 签名（coordinator 报
`NOT_ACCEPTED`），改为 `{**acceptance(), "signature": ...}`。

## 3. 执行授权门与 legacy

- 唯一执行入口：`idle_compute/worker.py::Worker.execute`；网络路径只经由
  `node._execute_and_submit`；CLI 无第二条执行路径。
- 双签模式：缺 authorization / 缺 commit / 身份错配 / terms_hash 不符 / 过期 → `LeaseError`
  （`NOT_AUTHORIZED`/`TASK_MISMATCH`/`WRONG_WORKER`/`EXPIRED`/`SIGNATURE`），全部在
  `test_lease_lifecycle.py::ExecutionGateTests` 覆盖。
- 默认配置即双签（`load_config` 缺省 `lease.mode="dual_signed"`）；legacy 只能显式配置，
  文档标注为本地兼容，且在双签模式下无法通过执行门（缺 commit 即拒）。
- **未彻底删除 legacy 分支**：`lease.mode="legacy"` 时 worker 仍可执行 CLAIM_GRANT 路径。
  这是本阶段明确保留的兼容路径（不默认、不进真实双签闭环），属已知残余风险。

## 4. 状态、持久化与迁移

- 状态机：∅→PROPOSED→ACCEPTED→ACTIVE；PROPOSED/ACCEPTED→REJECTED/CANCELLED/EXPIRED；
  非法迁移抛 `InvalidNegotiationState`；accept/reject/cancel/commit/activate 幂等。
- `one_open_negotiation`（部分唯一索引）与 `one_active_lease` 在事务内裁决；
  `create_negotiation_for_lease` 绑定既有 ACTIVE lease，`activate_negotiation` 仅 ACCEPTED→ACTIVE。
- DB v3：`lease_negotiations` 表 + v2→v3 迁移；`test_lease_lifecycle` 覆盖全新 v3、v2→v3、
  既有任务/lease 行保留、`schema_version=="3"`。
- 持久化失败：Store 抛错 → coordinator 回 `ERROR`，**不发送 LEASE_COMMIT**；worker 无 commit 不执行。

## 5. 未覆盖的验收项（必须如实列出）

| 项 | 状态 |
| --- | --- |
| 两个 worker 真实并发争抢同一 task（线程级） | 只有 DB 唯一约束 + 单写者保证；未写真实并发测试 |
| accept vs expire / cancel vs execute 竞态 | 只有 cancel 标记阻止执行；无确定性竞态测试 |
| coordinator 重启恢复（ACTIVE 重验、PROPOSED 不自动升级） | 状态持久化已具备；无专门重启测试 |
| worker 重启后不执行失效租约 | 无本地租约状态，重启只重 claim；无测试 |
| LEASE_REJECT / LEASE_CANCEL 节点级测试 | 处理已实现，未写节点级测试 |
| 节点级错投/广播拒绝（租约消息） | 协议层已测；节点级未测 |
| 结果重放到租约模式 | 沿用既有防重放（D2 测试），未在租约模式复测 |
| commit 已持久化但回复丢失后的重试 | 未专门测试 |
| 迁移中途失败 | SQLite 事务保证，未注入失败测试 |

## 6. 已知风险

1. legacy 分支仍存在（显式开启）；建议 D5 前删除或改为仅测试开关。
2. CRC/断线恢复、时钟偏差、真实双设备（D6）未验证。
3. 取消只阻止"尚未开始"的执行，**不能中断已开始的计算**。
4. 未实现多 coordinator/去中心化调度；coordinator 仍是单点权威。

## 7. 结论

- D4 的**核心安全闭环已成立**：默认双签、执行门唯一、无 commit 不执行、单次结算、
  真实两进程可运行；但验收表中并发/重启/取消/重放相关的若干项仍缺专门测试。
- **建议：先由审计确认上述缺口是否可接受；D5 暂不开始。**
