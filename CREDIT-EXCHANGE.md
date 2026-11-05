# CREDIT 兑换算力（最小闭环）

实现：`idle_compute/db.py`（`create_task_paid` / `credit_holds` / `available_credit`）、
`idle_compute/node.py`（`TASK_CREATE` 扣费 + `INSUFFICIENT_CREDIT` 错误码）。
测试：`tests/test_credit_exchange.py`（10 项）。

**不是链、不是代币、不是支付**：CREDIT 只是内部积分；本机制只在协调者本地数据库中记录
"谁为哪个任务冻结/消费了多少积分"，不改变 `ledger.py`、账本哈希链或 CREDIT 语义。

## 1. 余额定义

```
available(peer) = earned(peer) - Σ(credit_holds.amount WHERE state ∈ {HELD, SPENT})
```

- `earned`：现有账本中的 CREDIT 总和（`ledger.rebuild_ledger` 计算，不修改）。
- `HELD`：任务创建时冻结；`SPENT`：任务成功完成后永久消费；`REFUNDED`：释放。
- 因此 `idle_compute balances` 仍显示**赚取总额**；"可用余额"是另一个只读概念
  （`Store.available_credit`），CLI 暂未提供单独命令。

## 2. 事务边界

`Store.create_task_paid(task_type, payload, requester_id=..., cost=...)`：

1. 在**同一个** `BEGIN IMMEDIATE` 事务内计算 `available`；
2. `cost > 0` 且余额不足 → 抛 `InsufficientCredit`（域错误码 `INSUFFICIENT_CREDIT`），
   事务回滚，**不会**留下 task/shard/hold；
3. 余额足够 → 插入 task + shard + `credit_holds(HELD)`，一起提交；
4. 输入非法（白名单/schema）同样回滚，不扣费。

节点 `TASK_CREATE` 使用 `credits.task_cost`（配置，默认 **0** 表示不收费）调用该 API；
不足时回 `ERROR code=17 INSUFFICIENT_CREDIT`（wire 错误码，见 `docs/PROTOCOL.md` §5），
并附带 `cost/available`。

## 3. 结算规则（二选一：**成功消费，失败/取消/过期退回**）

| 事件 | hold 状态 | 可用余额 |
| --- | --- | --- |
| 任务创建 | `HELD` | 减少 `cost` |
| 结果验证 `VALID` → 任务 `COMPLETED` | `SPENT` | 保持减少（永久消费） |
| 结果验证 `INVALID` → 任务 `REJECTED` | `REFUNDED` | 恢复 |
| 用户取消（`cancel_task`） | `REFUNDED` | 恢复 |
| 租约过期且尝试次数耗尽 → `EXPIRED` | `REFUNDED` | 恢复 |
| 租约过期但还有重试次数 → 任务回 `QUEUED` | 保持 `HELD` | 不变（重试仍占用预算） |

所有迁移都只从 `HELD` 出发（`UPDATE ... WHERE state='HELD'`），因此**最多结算一次**；
已 `SPENT` 的任务再次取消/过期不会退款，已 `REFUNDED` 的任务也不会再次扣费。

## 4. 配置

```json
{"credits": {"task_cost": 1}}
```

- `task_cost`：整数 0..1000，默认 **0**。
- 为了不破坏既有部署/测试，默认 0（免费）；团队模拟或生产语义应显式设置 ≥1。
- 计费对象是 `TASK_CREATE` 的发送方（requester/client 的 node_id）。

## 5. 崩溃与恢复

- `credit_holds` 随任务同事务持久化；进程重启后 `HELD` 仍然有效。
- 任务最终进入 `COMPLETED/REJECTED/CANCELLED/EXPIRED` 时由对应事务结算；
  未结算的 `HELD` 不会自动消失（不会"偷偷退款"），协调者继续持有该预算。
- 账本不变：赚取的 CREDIT 仍按原规则写入、可验证、可重建；消费记录单独存放。

## 6. 已知限制

1. 只有本地 SQLite 记录，没有跨节点/链上结算；协调者单点。
2. CLI `balances` 显示赚取额，不显示可用额；`Store.available_credit` 目前只有库/测试入口。
3. 默认 `task_cost=0`（兼容）；不配置时不会真正扣费。
4. 失败退款是"任务未产生有效结果"的补偿，不构成对恶意 requester 的经济惩罚。
5. 未与 D5 注册表/匹配器联动定价；价格是全局常量 `task_cost`。
