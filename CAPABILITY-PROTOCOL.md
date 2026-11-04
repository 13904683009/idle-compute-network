# 能力声明协议（Capability Protocol）

版本：`schema_version = 1`（D1 冻结）　实现：`idle_compute/capability.py`　测试：`tests/test_capability.py`
状态：**协议与校验已实现；注册表（D5）与 P2P 传播（D2）尚未实现**。

---

## 1. 范围

本协议定义三样东西，并且**只**定义它们：

| 对象 | 用途 | 生命周期 |
| --- | --- | --- |
| `CapabilityManifest` | 节点相对稳定的能力承诺（可注册、可撤销） | 长（分钟~天），带 `expires_at` |
| `CapabilityAnnouncement` | 节点实时状态（负载/队列/是否接单） | 短（默认 60 s，心跳 20 s） |
| `CapabilityRevocation` | 撤销某个 `capability_version` 的签名声明 | 一次性、永久生效 |

**不包含**：实时 CPU/GPU 负载（属 Announcement）、任务数据、高频心跳、匹配结果、链上交易。

---

## 2. 规范化 / 哈希 / 签名（全局唯一，禁止各模块自行定义）

| 项 | 规则 | 实现 |
| --- | --- | --- |
| 规范化 | 键排序、无多余空白、UTF-8、不做浮点 | `crypto.canonical_json` |
| 哈希 | `sha256(canonical_json(payload))` 的 64 位小写十六进制 | `crypto.content_hash` |
| 签名 | Ed25519，签 `canonical_json({"domain": D, **payload})` | `identity.NodeIdentity.sign` |
| 验签 | 公钥来自 `node_id` 内嵌 multihash，无需分发 | `NodeIdentity.verify_signature` |
| 域隔离 | `idle-compute-capability-v1` / `-announcement-v1` / `-capability-revoke-v1` | `capability.py` 常量 |
| 签名位置 | 对象顶层 `signature`，**自身不参与**被签名载荷 | `payload()` 不含 `signature` |

**失败处理**：签名无效 → `CapabilityError(SIGNATURE)`；字段/类型/键集不符 → `SCHEMA`；
过期 → `EXPIRED`；版本回滚 → `VERSION_ROLLBACK`；同版本异内容 → `VERSION_CONFLICT`；
公告序号回滚 → `SEQ_ROLLBACK`。调用方必须拒绝，不得"记录后继续"。

---

## 3. `CapabilityManifest`

### 3.1 字段（精确键集，缺一或多一都拒绝）

| 字段 | 类型 | 规则 |
| --- | --- | --- |
| `schema_version` | int | 必须是 1 |
| `node_id` | str | 合法 libp2p Ed25519 peer id（52 字符，`12D3KooW…`） |
| `capability_version` | int | ≥1，按节点单调递增 |
| `issued_at` / `expires_at` | int | Unix 秒；`expires_at > issued_at`；`issued_at` 不得超前本机时钟 300 s |
| `supported_task_types` | list[str] | 1..32 项，每项 ≤64 字符；必须都在 `TASK_TYPES` 白名单内（执行层另有校验） |
| `supported_runtimes` | list[str] | 0..32 项 |
| `hardware` | object | 允许键仅 `cpu_arch,cpu_cores,ram_mb,gpu_vendor,gpu_memory_mb`；`cpu_arch ∈ {x86_64,aarch64,other}` |
| `resource_limits` | object | 精确四键：`max_cpu_cores,max_ram_mb,max_gpu_memory_mb,max_task_duration_seconds` |
| `network` | object | `supported_transports ⊆ {tcp,libp2p,quic}`；`supports_relay: bool`（仅声明，MVP 不实现中继） |
| `verification` | object | `level ∈ {SELF_DECLARED,BENCHMARKED,VERIFIED}`；`benchmark_profile: str|null` |
| `signature` | str | 128 hex；`payload()` 不含它 |

### 3.2 版本语义（`check_manifest_update`）

| 情况 | 结果 | 说明 |
| --- | --- | --- |
| 无既有记录 | `ACCEPT` | 需先验签 + 未过期 |
| 标记撤销 | `REVOKED` | 撤销优先于一切 |
| `incoming.version < stored.version` | `ROLLBACK` | 拒绝，不得覆盖新版本 |
| 版本相同且 `manifest_hash` 相同 | `IDEMPOTENT` | 重复注册/重复公告无害 |
| 版本相同但 `manifest_hash` 不同 | `CONFLICT` | **冲突隔离**，不静默覆盖 |
| `incoming.version > stored.version` | `ACCEPT` | 必须携带有效签名 |

`manifest_hash = sha256(canonical_json(payload))`，注册表只存哈希与元数据（D5）。

### 3.3 过期与撤销

- `expires_at <= now` → `EXPIRED`：**不得**参与任何匹配/租约/展示为"在线"。
- 撤销是独立签名对象：`node_id + capability_version + issued_at + reason(1..200)`；
  撤销对**该版本**永久生效，重启不复活；更高版本可重新注册（新版本号）。
- 撤销不能撤回（协议层无"撤销的撤销"）；如需恢复，发布新版本。

---

## 4. `CapabilityAnnouncement`

| 字段 | 类型 | 规则 |
| --- | --- | --- |
| `schema_version` | int | 1 |
| `node_id` | str | 与 manifest 相同 |
| `capability_version` | int | 必须等于当前 manifest 的版本 |
| `sequence` | int | ≥1，每节点单调递增；旧序号一律丢弃 |
| `observed_at` / `expires_at` | int | 默认 TTL 60 s（可配置）；`expires_at > observed_at` |
| `available_resources` | object | `cpu_cores,ram_mb,gpu_memory_mb`，均 ≥0 |
| `queue` | object | `running_tasks,queued_tasks`，均 ≥0 |
| `network_status` | object | `connectivity ∈ {DIRECT,RELAY,UNKNOWN}`；`supported_transports` 同 manifest 约束 |
| `accepting_tasks` | bool | 是否接单 |
| `signature` | str | 128 hex |

**规则**：过期公告不参与匹配；`sequence <= last_seen` → `SEQ_ROLLBACK`；公告必须绑定
同一 `node_id` 且 `capability_version` 与清单一致；公告是**自我声明**，不是可信证据。

---

## 5. 隐私（默认最小披露）

| 风险 | 字段 | 默认策略 |
| --- | --- | --- |
| 高（近唯一指纹） | 精确 RAM/显存、运行时补丁版本、GPU 型号、hostname、MAC、序列号 | **默认省略**；需要时按档位（RAM 4/8/16/32/64+，显存 ≤8/9-16/17-24/>24，cores ≤4/5-8/9-16/>16） |
| 中 | 精确 cores、GPU vendor | 可省略；发布前提示组合风险 |
| 低 | `cpu_arch`、runtime 名称 + 主次版本、能力 token | 允许公开 |

禁止默认写入：真实 IP、主机名、用户名、绝对路径、设备序列号、密钥材料。

---

## 6. 未实现 / 未验证（诚实边界）

- **注册表（D5）未实现**：没有链、没有交易、没有最终性；本协议只定义被注册的数据形态。
- **P2P 传播（D2）未实现**：公告如何发送/缓存/查询尚未接线；当前无任何节点间能力交换。
- **硬件真实性未验证**：签名只证明"某节点这样声明"；`SELF_DECLARED` 不是硬件证明。
- 基准测试（`BENCHMARKED`）与更强验证（`VERIFIED`）只有字段定义，没有实现。

---

## 7. 测试对照

| 规则 | 测试 |
| --- | --- |
| 规范化与哈希稳定 | `test_capability.py::test_canonical_hash_is_stable_across_roundtrip` |
| 签名/验签、篡改拒绝、错签者 | `test_sign_and_verify` / `test_tampered_field_breaks_the_signature` / `test_wrong_signer_is_rejected` |
| 过期、未来时间 | `test_expired_manifest_is_rejected` / `test_future_issued_at_is_rejected` |
| 回滚 / 冲突 / 幂等 / 撤销 | `test_lower_version_is_rollback_and_higher_is_accept` / `test_same_version_different_content_is_conflict` / `test_revoked_version_stays_revoked` |
| 公告序号/过期/绑定 | `AnnouncementTests` 5 项 |
| 撤销签名 | `RevocationTests` 3 项 |
