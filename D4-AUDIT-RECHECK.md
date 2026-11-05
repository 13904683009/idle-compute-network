# D4 验收矩阵复核记录（PASS 逐项核验）

日期：2026-11-05　基线：`f2e6268`（479 测试）
方法：解析 `D4-ACCEPTANCE-MATRIX.md` 每行的 `test_*` 引用，逐个在 `tests/` 中查找
`def <name>(`；无测试引用的 PASS 行做源码核验；失败的引用记录为缺口并补测试。

## 1. 复核结果汇总

| 类别 | 行 | 结论 |
| --- | --- | --- |
| 有明确测试函数 | A2 A3 A5 B1 B2 B3 B4 B5 C1 C2 C3 D2 D3 D4 E1 E4 F1 F2 G1 | VERIFIED |
| 仅有测试模块名（引用到文件而非函数） | A1 B2 B4 C1 D2 E4 | 可解析到函数（同文件内），VERIFIED |
| 仅代码路径（无单测） | A4 A7 B10 D5 E3 E5 E6 F4 | 代码核验 PASS；E3/E6 另由新迁移/唯一索引测试覆盖 |
| 复核发现覆盖不足 | **E2** | 原证据只测 v1 账本 fixture，没有"含既有 task/lease 数据的 v2 库"；已补 `test_d4_v2_migration.py` |

**未发现"PASS 但完全没有依据"的条目**；E2 的表述超出了原测试实际覆盖，已在
`tests/test_d4_v2_migration.py` 补齐，并在矩阵中更新证据。

## 2. 代码核验备注

- **A4（claim 只能走双签）**：`node._claim_cycle` 无条件调用 `_claim_cycle_dual`；
  `CLAIM_GRANT` 仅作为兼容通知发送，worker 不再据此执行。
- **A7（Worker.execute 调用点）**：`grep -R "Worker(" idle_compute/` → `node._execute_and_submit`
  （网络唯一入口）、`coordinator.run_one`（本地 Phase-1 CLI）、测试；`worker.execute` 的
  `require_dual_signed=True` 只在网络路径使用。
- **B10（过期清理保留防重放状态）**：`_expire_locked` 只把状态改为 EXPIRED，不删除
  `lease_negotiations` 行；`discovery` 的 sequence 状态不受影响。
- **D5（账本未改）**：`git diff 8bf1162 -- idle_compute/ledger.py` = 0 行。
- **E3（重复迁移）**：`CREATE TABLE IF NOT EXISTS` + 无条件 `UPDATE meta`；新增
  `test_d4_v2_migration.py::test_repeated_open_does_not_remigrate` 直接验证。
- **E5（旧 lease 不被误认作双签授权）**：执行只接受 `LeaseCommit` 签名对象；旧 `leases`
  行没有 commit；`_claim_cycle_dual` 不会读取旧行。
- **E6（唯一约束）**：`one_active_lease`、`one_open_negotiation` 均为部分唯一索引；
  并发/重复测试已覆盖（B5、B6、B7）。
- **F4（seq 防重放）**：信封 seq 窗口与 `node_state` 持久化是既有 D1/E-1 行为，
  `test_protocol.py` 的 seq 窗口测试覆盖。

## 3. 本次补齐的 NOT TESTED 项（矩阵更新）

| # | 原状态 | 新状态 | 测试 |
| --- | --- | --- | --- |
| B6 两 worker 同时 ACCEPT | NOT TESTED | **PASS** | `test_d4_two_worker_accept.py`（2） |
| B7 accept vs expire | NOT TESTED | **PASS** | `test_d4_accept_expire.py`（4） |
| B8 cancel vs execute | NOT TESTED | **PASS（语义级）** | `test_d4_cancel_execute.py`（5） |
| B11 DB 写失败注入 | NOT TESTED | **PASS** | `test_d4_db_failure.py`（3） |
| F3 节点级租约错投/广播 | NOT TESTED | **PASS** | `test_d4_lease_recipient.py`（3） |
| C4 worker 重启语义 | PARTIAL | **PASS（语义级）** | `test_d4_worker_restart.py`（3） |
| E2 v2 数据迁移 | PASS（证据不足） | **PASS** | `test_d4_v2_migration.py`（2） |

“语义级”说明：B8/C4 验证的是已实现的取消门与"无持久化授权、重启必须重新 claim"的语义，
**不是**对运行中进程强杀后的自动恢复（该能力未实现，也不宣称）。

## 4. 测试与日志

| 项 | 结果 |
| --- | --- |
| 基线 | 479/479 OK（`d4-gap-baseline-479.log`） |
| 双 worker ACCEPT | 481/481 OK（`d4-gap-1-two-worker-accept.log`） |
| accept vs expire | 490/490 OK（`d4-gap-3-cancel-execute.log` 覆盖 1–3；gap-2 日志因并发加入未提交测试而含 1 个陈旧错误，已用 gap-3 全量替代） |
| DB 写失败 | 493/493 OK（`d4-gap-4-db-failure.log`） |
| 节点级错投/广播 | 496/496 OK（`d4-gap-5-lease-recipient.log`） |
| worker 重启 + v2 迁移 + 文档 | 501/501 OK（`d4-gap-6-worker-restart-final.log`；首次运行仅因 Windows 临时目录清理句柄抖动报 1 个 cleanup error，`test_log` 的 TemporaryDirectory 加 `ignore_cleanup_errors=True` 后重跑全绿，断言未改） |

注：gap-2 的日志保留原样（含失败）以保持审计可追溯；最终结论以 gap-3/4/5/6 的全量日志为准。
