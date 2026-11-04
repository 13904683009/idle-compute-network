# 任务租约协议（Two-Party Task Lease Protocol）

版本：`schema_version = 1`（D1 冻结）　实现：`idle_compute/lease.py`　测试：`tests/test_lease.py`
状态：**协议、双签与校验已实现；协商消息与执行闭环（D4）尚未实现**。

---

## 1. 定位

租约把"匹配选中了某个工人"变成**双方签名的执行承诺**，并在不改变既有任务状态机
（`state_machine.ALLOWED`）的前提下接入现有任务权威：

- 请求方（MVP = 协调者，见 ADR-D0-2）签**条款**；
- 工人签**接受**（只覆盖少量字段，防止把接受重放到别的条款上）；
- 只有胜者的租约才会在协调者 actor 线程里创建 `leases` 行（`one_active_lease` 仍是最终裁决）；
- 租约协商本身记录在单独的 `lease_negotiations`（D4 迁移），**不参与** `TaskStatus`。

---

## 2. 字段（精确键集）

| 字段 | 类型 | 规则 |
| --- | --- | --- |
| `schema_version` | int | 1 |
| `lease_id` / `task_id` | str | 1..128 字符 |
| `requester_node_id` / `worker_node_id` | str | 合法 node id，且**必须不同** |
| `task_spec_hash` | str | 64 位小写十六进制（任务条款/需求的规范化哈希） |
| `input_manifest_hash` | str | 空串或 64 位小写十六进制 |
| `lease_version` | int | ≥1 |
| `issued_at` / `expires_at` | int | `expires_at > issued_at` |
| `execution_deadline` | int | **≥ `expires_at`**（执行截止不早于租约到期） |
| `verification_policy` | object | 精确两键：`mode="RECOMPUTE"`、`required: bool` |
| `reward_policy` | object | 必须含 `amount ≥1` 的 int；序列化 ≤1024 字节；MVP 只用于内部积分 |
| `requester_signature` | str | 128 hex；见 §3 |
| `worker_signature` | str | 128 hex；见 §3 |

---

## 3. 签名构造（域隔离）

```
requester_payload = canonical_json({"domain":"idle-compute-lease-v1", **terms})
requester_signature = Ed25519(requester_key, requester_payload)

acceptance = {lease_id, worker_node_id, task_spec_hash, lease_version, expires_at}
worker_payload = canonical_json({"domain":"idle-compute-lease-accept-v1", **acceptance})
worker_signature = Ed25519(worker_key, worker_payload)
```

- `terms` = 除两个签名外的全部字段；任何条款被改都会使请求方签名失效。
- 接受签名只覆盖 5 个字段：工人**不可能**把接受重放到不同条款（改 `task_spec_hash`
  或 `expires_at` 即验签失败）。
- 没有请求方签名时，工人**拒绝**签名接受（`NOT_ACCEPTED`）。
- 域字符串与信封/结果/记账域不同，避免跨用途复用。

---

## 4. 校验

### 4.1 条款层（`_validate_terms`，签名前也执行）

节点 id 合法且不相同；哈希格式；`expires_at > issued_at`；`execution_deadline ≥ expires_at`；
`verification_policy` 精确键 + `RECOMPUTE`；`reward_policy.amount ≥1`。

### 4.2 请求方视角（`verify_proposal(now)`）

条款合法 → 请求方签名有效 → `issued_at ≤ now` → `expires_at > now`。

### 4.3 工人视角（`verify_for_worker(worker_id, now)`，方案第九节 1–8 条中本层能覆盖的部分）

1. 租约发给**本工人**（否则 `WRONG_WORKER`）；
2. `expires_at > now`（否则 `EXPIRED`）；
3. `execution_deadline > now`（否则 `EXPIRED`）；
4. 请求方签名有效；
5. 若已签接受，则接受签名有效。

任务是否存在、输入哈希是否匹配、本地资源是否足够、任务是否白名单 —— 这些需要任务
上下文，属于 **D4** 在工人执行前调用 `verify_for_worker` 之后再检查的事项，本层不代替。

### 4.4 拒绝码

| code | 含义 |
| --- | --- |
| `SCHEMA` | 字段/键集/时间关系不合法 |
| `SIGNATURE` | 签名无效或与载荷不匹配（含**重放到其它条款**） |
| `EXPIRED` | 租约或执行截止已过 |
| `WRONG_WORKER` | 租约不是给本工人的 / 签名者不是该工人 |
| `NOT_ACCEPTED` | 缺请求方签名时签接受，或缺接受签名时验接受 |

---

## 5. 冲突与状态（D4 落地方式，已在 D0 冻结）

| 场景 | 处理 |
| --- | --- |
| 多工人同时接受同一提案 | 各自签名有效即都"接受"；协调者 actor 线程内以 `one_active_lease` 唯一索引裁决，胜者建 `leases` 行，败者记 `REJECTED_CONFLICT` |
| 重复接受同一租约 | 幂等（同 `lease_id` + 同接受签名 → 返回原结果） |
| 租约重放 | `lease_id` 唯一 + 接受签名绑定 `task_spec_hash/expires_at`；过期即拒 |
| 任务状态 | 不新增 `TaskStatus`：接受后走现有 `CLAIM_GRANT → RUNNING → SUBMITTED → …` |

---

## 6. 未实现（诚实边界）

- **协商消息未接线**：`LEASE_PROPOSE / LEASE_ACCEPT / LEASE_REJECT` 尚未加入封闭消息注册表
  （D4 添加，届时 `test_protocol` 的类型计数会更新）。
- **持久化未实现**：`lease_negotiations` 表、`db.py` v3 附加迁移属 D4。
- **奖励只是内部积分**：没有代币、提现或金融语义；`amount` 不得被解释为可交易价值。
- **时钟**：时间来自本机；跨设备时钟偏差未验证（D6 双设备）。

---

## 7. 测试对照

| 规则 | 测试 |
| --- | --- |
| 双签验签 + 往返 | `test_proposal_and_acceptance_verify` |
| 域隔离 | `test_domains_are_distinct` |
| 条款篡改 | `test_tampered_terms_break_the_proposal_signature` |
| 错误工人 / 无提案接受 | `test_wrong_worker_cannot_accept` / `test_accept_without_proposal_is_refused` |
| 接受重放到其它条款 | `test_acceptance_cannot_be_replayed_onto_other_terms` |
| 过期 / 工人门 | `test_expired_lease_is_rejected` / `test_wrong_worker_gate` |
| 时间关系 / 精确键集 | `test_execution_deadline_must_not_precede_expiry` / `test_schema_is_exact` |
| 同一节点不得双角色 | `test_requester_and_worker_must_differ` |
