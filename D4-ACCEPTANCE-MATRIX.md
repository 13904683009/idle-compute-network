# D4 验收矩阵（最终审计；复核记录见 D4-AUDIT-RECHECK.md）

HEAD：`7faa7b5` + 本轮审计修复（提交见最终报告）
基线：446/446；本轮：460/460（446 + 13 审计回归 + 1 未见？见日志）
日志：`logs/idle-compute/d4-audit-baseline-446.log`、`d4-audit-final-suite.log`

标记：**PASS** = 有代码 + 有测试证据；**PARTIAL** = 有代码但只有 Store/单元级证据或覆盖不完整；
**NOT TESTED** = 未验证（实现可能存在，但无测试证据）。

## A. 执行授权与 legacy 关闭

| # | 项 | 结论 | 证据 |
| --- | --- | --- | --- |
| A1 | 默认 `dual_signed` | PASS | `config.py::LEASE_MODES`；`test_lease_audit.py::test_config_default_is_dual_signed` |
| A2 | 配置 `legacy` 被拒 | PASS | `config.py::_parse_lease`；`test_config_rejects_legacy_mode` |
| A3 | 手工 NodeConfig 不能启用 legacy | PASS | `node.py::Node.__init__` guard；`test_node_rejects_hand_built_legacy_config` |
| A4 | 节点 claim 只能走双签 | PASS | `node.py::_claim_cycle` → `_claim_cycle_dual`；legacy body 不可达 |
| A5 | 无 commit 不执行 | PASS | `worker.py::Worker.execute` + `lease.require_authorization`；`test_lease_lifecycle.py::test_worker_execute_requires_commit_in_dual_mode` |
| A6 | 本地 Phase-1 CLI 路径隔离 | PARTIAL | `coordinator.py::run_one` 直接调用 `Worker.execute`（本地、无网络），无测试证明"不可由网络触发"（代码上不经过 transport） |
| A7 | 全部 Worker.execute 调用点 | PASS | `node.py::_execute_and_submit`（唯一网络路径）、`coordinator.py`（本地 CLI）、tests；README 未新增入口 |

## B. 状态机 / 事务 / 并发

| # | 项 | 结论 | 证据 |
| --- | --- | --- | --- |
| B1 | 合法迁移 PROPOSED→ACCEPTED→ACTIVE | PASS | `test_lease_lifecycle.py::test_happy_path_proposed_accepted_committed` |
| B2 | 非法迁移拒绝 | PASS | `test_commit_requires_accept`、`test_cancelled_or_rejected_cannot_commit`、`test_lease_audit.py::test_expired_or_cancelled_lease_cannot_be_revived` |
| B3 | 重复 ACCEPT 幂等 | PASS | `test_accept_is_idempotent_and_guards_other_signatures` |
| B4 | 重复 COMMIT/activate 幂等 | PASS | `test_commit_is_idempotent`、`test_lease_audit.py::test_concurrent_activate_is_idempotent` |
| B5 | 同一 task 不产生两个 ACTIVE lease（并发） | PASS | 唯一索引 `one_active_lease`；`test_lease_audit.py::test_two_threads_claiming_the_same_task_never_create_two_leases` |
| B6 | 两个 worker 同时 ACCEPT 同一 task | PASS | `test_d4_two_worker_accept.py`（并发/顺序：仅被寻址 worker 被接受） |
| B7 | accept vs expire | PASS | `test_d4_accept_expire.py`（边界/回滚/barrier 竞态，永不 ACTIVE） |
| B8 | cancel vs execute | PASS（语义级） | `test_d4_cancel_execute.py`（取消门/幂等/ACTIVE 不可取消/accept-cancel 竞态） |
| B9 | CANCEL 精确语义 | PARTIAL | 非 ACTIVE 可取消（幂等）；ACTIVE 拒绝取消（不假装中断），见 `TASK-LEASE-PROTOCOL.md` §8.2；`test_expired_or_cancelled_lease_cannot_be_revived` |
| B10 | 过期清理不删防重放状态 | PASS | negotiation 行保留为 EXPIRED；`_expire_locked` 仅改状态 |
| B11 | DB 写失败不确认成功 | PASS | `test_d4_db_failure.py`（Store + node handler：ERROR 且无 LEASE_COMMIT） |

## C. 重启恢复

| # | 项 | 结论 | 证据 |
| --- | --- | --- | --- |
| C1 | PROPOSED/ACCEPTED 重启不自动 ACTIVE | PASS | `test_lease_audit.py::test_unfinished_negotiation_does_not_become_active_on_reopen` |
| C2 | 过期/取消不能恢复执行 | PASS | `test_expired_or_cancelled_lease_cannot_be_revived`、`test_old_authorization_is_rejected_after_expiry` |
| C3 | ACTIVE 过期后协商级联 EXPIRED | PASS | `test_expiring_the_lease_expires_the_active_negotiation`（`db.py::_expire_locked`） |
| C4 | worker 重启不执行旧授权 | PASS（语义级） | `test_d4_worker_restart.py`（无持久化授权、必须重新 claim、过期授权拒绝） |
| C5 | 断线期间 fail-closed | PASS（最小语义） | `tests/test_c5_restart.py`：重启后任务仍 QUEUED、hold 仍 HELD、无 lease/negotiation；必须显式 claim+提议+接受+激活；不实现自动恢复 |

## D. 结果防重放与账本

| # | 项 | 结论 | 证据 |
| --- | --- | --- | --- |
| D1 | 结果绑定 task/lease/worker | PASS | `results` 表 `UNIQUE(task_id, lease_id)` + `peer_id`；`test_lease_network.py` 断言结果行 |
| D2 | 重复提交幂等、不重复计分 | PASS | `test_lease_audit.py::test_duplicate_result_submission_yields_one_credit` |
| D3 | 旧租约结果不能用于新租约 | PASS | `UNIQUE(task_id, lease_id)` + `submit_result` 校验 lease/peer；`test_commit_no_longer_matches_a_new_lease` |
| D4 | 完成流程仍走既有验证 | PASS | `test_lease_network.py`（独立重算 + CREDIT 1 条） |
| D5 | 未修改 ledger/哈希链/CREDIT | PASS | `git diff 8bf1162 -- idle_compute/ledger.py` = 0 |

## E. DB v2→v3 迁移

| # | 项 | 结论 | 证据 |
| --- | --- | --- | --- |
| E1 | 空 v2 库升级 | PASS | `test_store.py::test_v1_database_migrates_in_place_to_schema_v3` |
| E2 | 含既有数据的 v2 升级 | PASS | `test_d4_v2_migration.py`（既有 task/lease 保留、重复打开不重复迁移） |
| E3 | v3 重复启动不重复迁移 | PASS | `CREATE TABLE IF NOT EXISTS` + `UPDATE meta`（幂等） |
| E4 | 迁移失败不留半升级 | PASS | `test_lease_audit.py::test_failed_migration_keeps_schema_version_2_and_retries`（失败时仍为 v2） |
| E5 | 旧 lease 不被误认作双签授权 | PASS | 执行只认 `LeaseCommit` 签名；旧行无 commit |
| E6 | `one_open_negotiation` / `one_active_lease` 生效 | PASS | 部分唯一索引 + 并发测试 |
| E7 | 不支持 v3→v2 降级 | PASS（记录） | `TASK-LEASE-PROTOCOL.md` §8.5 |

## F. 协议与消息

| # | 项 | 结论 | 证据 |
| --- | --- | --- | --- |
| F1 | 5 种消息精确 schema/大小 | PASS | `protocol.py` + `test_protocol.py`（count 24） |
| F2 | 全部定向，禁止广播 | PASS | `DIRECTED_TYPES`；`test_protocol.py::TestCapabilityMessages`/recipient 断言 |
| F3 | 节点级错投/广播拒绝（租约消息） | PASS | `test_d4_lease_recipient.py`（真实 TCP coordinator + ManualPeer） |
| F4 | seq/防重放 | PASS | 既有信封 seq 窗口 + 公告侧测试 |

## G. 真实链路

| # | 项 | 结论 | 证据 |
| --- | --- | --- | --- |
| G1 | 两进程 PROPOSE→ACCEPT→COMMIT→执行→结算 | PASS | `test_lease_network.py`（真实子进程/TCP，2.5 s） |
| G2 | 真实双设备（D6） | NOT TESTED | 未执行，不得宣称通过 |

## 结论

- **执行授权绕过已关闭**（A1–A5、A7）：节点无法启用 legacy，claim 只能走双签，执行只认 commit。
- **状态机/重启/结果/迁移**核心项 PASS；B6/B7/B8/B11/C4/E2/F3 已补回归；C5 最小语义 PASS；G2 真实双设备仍 NOT TESTED，
  以及 A6 的本地 CLI 隔离仅有代码论证。
- **D4 仍不满足"全部 27 项"验收**；建议审计针对上述 NOT TESTED 项决定是否接受。

## D5 接入后的 D4 状态复核

- A6（本地 Phase-1 CLI 隔离）：仍 **PARTIAL**。D5 未改变 `coordinator.run_one` 的本地执行路径，也没有新增“不可由网络触发”的测试。
- C5（断线恢复）：**PASS（最小语义）**。`tests/test_c5_restart.py` 证明重启后不自动 ACTIVE/执行、必须重新 claim 与显式双签流程；**未实现自动重连/恢复**，不得宣称断线可恢复。
- 其余 D4 条目不因 D5 改变；D6 仍未执行。
