# 任务需求协议（Task Requirement Protocol）

版本：`schema_version = 1`（D1 冻结）　实现：`idle_compute/requirement.py`　测试：`tests/test_requirement.py`
状态：**协议与校验已实现；匹配器使用（D3）尚未实现**。

---

## 1. 目标

用"任务真正需要什么"（运行时、最低资源、上限、验证策略、截止时间）描述任务，
而不是指定某块显卡。任务类型必须在既有白名单 `TASK_TYPES` 内，因此**该协议不允许
任意代码执行**。

---

## 2. 字段（精确键集）

| 字段 | 类型 | 规则 |
| --- | --- | --- |
| `schema_version` | int | 必须是 1 |
| `task_id` | str | 1..128 字符 |
| `task_type` | str | 必须在 `tasks.TASK_TYPES`（当前仅 `numeric_batch_v1`） |
| `task_version` | int | ≥1（任务实现版本，不是协议版本） |
| `input_manifest_hash` | str | 空串（内联输入）或 64 位小写十六进制 |
| `required_runtime` | str | 1..64 字符的能力 token（如 `python_sandbox_v1`） |
| `minimum_resources` | object | 精确三键：`cpu_cores ≥1`、`ram_mb ≥0`、`gpu_memory_mb ≥0` |
| `preferred_resources` | object | 允许键仅 `gpu_vendor`、`gpu_compute_profile`；可空 |
| `constraints` | object | 精确三键：`max_execution_seconds ≥1`、`max_input_bytes ≥0`、`max_output_bytes ≥0` |
| `data_policy` | object | `requires_private_data: bool`、`allow_relay: bool` |
| `verification_policy` | object | 精确两键：`mode="RECOMPUTE"`（MVP 唯一取值）、`required: bool` |
| `deadline` | int | Unix 秒；`now >= deadline` 即过期 |

`requirement_hash = sha256(canonical_json(payload))`，用于租约的 `task_spec_hash` 之外的
可复算引用（租约绑定的 `task_spec_hash` 由调用方给定并双向校验）。

---

## 3. 校验与失败

| 情况 | 结果 |
| --- | --- |
| 未知 `task_type` | `RequirementError(UNKNOWN_TASK_TYPE)` |
| 任何多余/缺失键、错误类型、bool 冒充 int、负值 | `RequirementError(SCHEMA)` |
| `verification_policy.mode != "RECOMPUTE"` | `SCHEMA`（MVP 不接受"信任声明"） |
| 过期（`now >= deadline`） | `is_expired(now) == True`；匹配/派发必须拒绝 |

**不传**：真实 IP、绝对路径、设备序列号；任务输入数据本体（只传哈希）。

---

## 4. 与现有协议的关系

- 现有 `TASK_CREATE` 体（`client_ref, task_type, payload, max_attempts, lease_seconds`）
  **不修改**（精确键集 + `v==1` 硬拒，加字段会破坏兼容）。
- D1 只冻结需求对象与其校验；把 `TaskRequirement` 接入线上（新消息或 `TASK_CREATE`
  的 v3 扩展）属于 D4，届时必须新增消息类型并更新封闭注册表与 `test_protocol` 计数。
- `task_type` 的最终执行仍由 `tasks.get_task_type()` 唯一分发（源码门禁保证）。

---

## 5. 未实现（诚实边界）

- 匹配器（D3）：如何用 `minimum_resources` 过滤/评分。
- 数据传输（D4 之后）：`input_manifest_hash` 目前只表示"有哈希"，没有内容寻址存储、
  分块、断点续传或带宽测量。
- 资源执行限制：本协议只声明上限；**进程内执行器没有 OS 级隔离**（白名单 ≠ 沙箱），
  超时/内存上限目前依赖租约与任务参数，不是 rlimit。

---

## 6. 测试对照

| 规则 | 测试 |
| --- | --- |
| 合法对象 roundtrip + 稳定哈希 | `test_valid_requirement_roundtrips_with_stable_hash` |
| 白名单 | `test_unknown_task_type_is_rejected` |
| 精确键集 | `test_extra_key_is_rejected` / `test_preferred_resources_are_optional_but_strict` |
| 类型与边界 | `test_bool_is_not_an_int` / `test_negative_gpu_memory_is_rejected` |
| 验证策略 | `test_unknown_verification_mode_is_rejected` |
| 输入哈希格式 | `test_bad_input_hash_is_rejected` |
| 截止时间 | `test_deadline_expiry` |
