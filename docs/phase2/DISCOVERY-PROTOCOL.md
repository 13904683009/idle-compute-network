# 能力发现协议（Discovery Protocol，D2）

版本：`schema_version = 1`（D2 冻结）　实现：`idle_compute/discovery.py` + `protocol.py` 的三条消息
状态：**公告缓存/校验/查询/持久化已实现并测试；匹配（D3）、租约（D4）、注册表（D5）未实现。**

---

## 1. 三条新消息（封闭注册表 16 → 19）

| 类型 | 方向 | recipient | body（精确键） |
| --- | --- | --- | --- |
| `CAPABILITY_ANNOUNCE` | 任意 → 任意（广播/定向投递） | **null（广播）** 或本机 node_id | `manifest`, `announcement` |
| `CAPABILITY_QUERY` | 请求方 → 被查询方 | **必须显式指定**，为被查询方 node_id | `task_type`, `runtime`（均可为 null） |
| `CAPABILITY_RESPONSE` | 被查询方 → 请求方 | **必须显式指定**，为请求方 node_id | `query_seq`, `peers[]` |

规则（`protocol.py`）：
- `BROADCAST_TYPES = {CAPABILITY_ANNOUNCE}`；`DIRECTED_TYPES = {CAPABILITY_QUERY, CAPABILITY_RESPONSE}`。
- 定向消息 `recipient=null` → `INVALID_BODY`；收件人不是本机 → `UNAUTHORIZED`（验签之后检查）。
- 广播消息 `recipient=null` 允许；若显式写给某节点，按定向规则校验。
- 嵌套对象上限 `MAX_CAPABILITY_OBJECT_BYTES = 24 KiB`（manifest/announcement 各自）；
  一个响应最多 `MAX_DISCOVERY_PEERS = 32` 条候选；整帧仍受 64 KiB / body 32 KiB 约束。
- 每条消息的签名/序号/时间戳语义与既有 16 种完全一致（信封签名 + 序号窗 + ±300 s）。

`CAPABILITY_RESPONSE.peers[]` 里每个条目是 `{manifest, announcement}`，二者是**各自签名**的 D1 对象。
**转发不改变信任**：relay 可以扣留，但不能伪造（签名绑 node_id）；接收方按 manifest.node_id 验签，
而不是按信封 sender。

---

## 2. 接收与校验顺序（`AnnouncementCache.ingest`）

```
schema（from_dict，精确键集）
  → 身份绑定（manifest.node_id == 信封 sender；announcement.node_id == manifest.node_id）
  → manifest 签名 + 时间（issued_at/expires_at）
  → announcement 签名 + 时间（observed_at/expires_at）
  → 精确重复判定（同 seq + 同 manifest hash → DUPLICATE，不写缓存）
  → 序号（<= 已接受的最大序号 → SEQ_ROLLBACK）
  → 版本（低于缓存版本 → VERSION_ROLLBACK；同版本异 hash → VERSION_CONFLICT）
  → 撤销（该版本已撤销 → REVOKED）
  → 单条大小（> 64 KiB → ENTRY_TOO_LARGE）
  → 容量（满时淘汰；无可淘汰 → CACHE_FULL）
  → **持久化**（sequence/version/hash 写入 discovery.db；失败 → PERSIST_FAILED，缓存不变）
  → 写入内存缓存
```

失败只返回结构化 code，**不抛异常、不写缓存**；调用方（node）只记日志，不向广播方回包
（避免放大）。

### 2.1 时钟与过期（不假设远端时间可信）

- 远端 `expires_at` 会被**本地钳制**：`effective_expiry = min(announcement.expires_at, received_at + max_announcement_ttl)`。
  默认 `max_announcement_ttl = 120 s`，因此一个谎报"一年有效"的公告最多只活 2 分钟。
- `observed_at` 超前本机 300 s 以上直接拒绝（复用 D1 的 skew 规则）。
- 过期条目在 `lookup` 与 `sweep` 双重过滤，且 `manifest.expires_at` 也检查。

### 2.2 重复 / 回滚 / 撤销

| 情况 | 行为 |
| --- | --- |
| 完全相同公告再次到达 | `DUPLICATE`：不新增记录，仅刷新 `last_alive`（不推进序号） |
| `sequence <= 已接受最大` | `SEQ_ROLLBACK`，拒绝 |
| 进程重启后旧公告重放 | 被 `discovery.db` 里的 last_sequence 拒绝（跨重启） |
| 清单版本回退 | `VERSION_ROLLBACK` |
| 同版本不同 manifest hash | `VERSION_CONFLICT`（隔离，不覆盖） |
| 已撤销版本 | `REVOKED`；更高 `capability_version` 可重新声明（撤销按版本生效） |

---

## 3. 缓存：有界、确定性

| 参数 | 默认 | 说明 |
| --- | --- | --- |
| `max_entries` | 1024 | 条目上限；新增时若满 → 淘汰 |
| `max_entry_bytes` | 64 KiB | 单条（manifest+announcement 规范化 JSON）上限 |
| 淘汰顺序 | 先过期（expires_at 最小），再最久未收（received_at 最小），同值按 node_id | 确定性、可测试 |
| 无法淘汰 | `CACHE_FULL` | 不插入、不持久化（不假装成功） |

---

## 4. 在线 vs 公告有效（两个状态）

- **公告有效**：未过期、未撤销、manifest 未过期 → 可以出现在"候选集"里。
- **当前可达**：最近一次来自该 peer 的**已验签帧**（任何类型）在 `offline_after_seconds`（默认 90 s）内。
- `lookup(include_unreachable=False)`（默认）只返回可达节点；`include_unreachable=True` 供运维观察。
- `sweep()` 每次循环调用：删除过期条目、把静默 peer 标记 `reachable=False`。
  **过期公告不会被当成在线节点**，离线节点也不会被当成当前可派发节点。

---

## 5. 持久化

- 文件：`<data_dir>/discovery.db`（与账本库分离；不改 v2 schema）。
- 表：`peer_announcement_state(node_id PK, last_sequence, capability_version, manifest_hash, revoked_version, updated_at)`。
- 连接：`check_same_thread=False` + 内部锁 —— 缓存同时被节点 loop 线程（sweep/publish）与
  stream 处理线程（ingest）使用（D2 实测暴露过 sqlite 线程亲和性错误，已修复）。
- 写序：**先持久化、后写内存**；持久化失败返回 `PERSIST_FAILED` 且缓存不变。

---

## 6. 节点行为（`node.py`）

| 角色 | 行为 |
| --- | --- |
| 全部 | 收到 `CAPABILITY_ANNOUNCE` → 校验入缓存；收到 `CAPABILITY_QUERY` → 从缓存回 `CAPABILITY_RESPONSE`；收到 `CAPABILITY_RESPONSE` → 逐条入缓存并入回复缓冲 |
| coordinator / worker | 每个 poll tick 调用 `sweep`；若配置了 `capability` 段，按 `announce_interval_seconds` 发布自己的公告（序号持久化后再发） |
| client | 不发布；可用 `query_capabilities(peer_id, ...)` 主动查询 |

发布是**尽力而为**广播：只对 `peers` 中已有连接的 peer 发送（worker→coordinator 可行；
coordinator 无出站流时 `failed` 计数增加，不视为错误）。

---

## 7. 未实现（诚实边界）

- **匹配器（D3）**：`lookup` 只做过滤，不评分、不排序候选（排序仅为 node_id 字典序的确定性输出）。
- **任务需求/租约（D4）**：发现结果还没有消费者；`TaskRequirement`/`TaskLeaseTerms` 尚未上线。
- **注册表（D5）**：没有链上承诺校验；公告只证明"某节点自述"，不是硬件证明。
- **DHT/GossipSub/Relay/mDNS/UPnP**：未实现；发现依赖静态 `peers` + 定向查询 + 公告广播。
- **跨设备**：所有测试是单机多进程真实 socket；真实双设备未验证（不得宣称 D6 通过）。

---

## 8. 测试对照（D2 验收 15 项）

| # | 验收项 | 测试 |
| --- | --- | --- |
| 1 | 合法公告可验证并进入缓存 | `test_discovery.py::test_valid_announcement_is_cached` |
| 2 | 无效签名被拒绝 | `test_bad_signature_is_rejected` |
| 3 | node_id/公钥/签名身份不匹配被拒 | `test_manifest_signer_must_match_sender`、`test_announcement_must_match_manifest_identity` |
| 4 | 重复公告不重复记录 | `test_duplicate_is_not_recorded_twice` |
| 5 | 旧 sequence 被拒 | `test_old_sequence_is_rejected` |
| 6 | 重启后仍拒绝旧 sequence | `test_restart_rejects_old_sequence` |
| 7 | 撤销生效 | `test_revocation_removes_and_persists`、`test_bad_revocation_signature_is_rejected` |
| 8 | 过期不参与发现 | `test_expired_announcement_is_rejected`、`test_expired_entries_do_not_participate`、`test_remote_expiry_is_clamped_by_local_ttl` |
| 9 | 缓存上限行为有界确定 | `test_cache_is_bounded_and_evicts_deterministically`、`test_cache_full_when_nothing_is_evictable`、`test_oversized_entry_is_rejected`、`test_persistence_failure_does_not_update_the_cache` |
| 10 | 定向响应只被请求方接受 | `test_protocol.py::TestCapabilityMessages`（recipient 规则）、`test_discovery_network.py::test_directed_query_recipient_rules_on_the_wire` |
| 11 | 广播公告可被多节点接受 | `test_protocol.py::test_broadcast_announce_is_accepted` + 两进程用例 |
| 12 | 离线不被当作在线 | `test_offline_is_not_online` |
| 13 | 两个独立进程交换公告/验证/缓存/查询 | `test_discovery_network.py::test_worker_announcement_reaches_the_coordinator_and_is_queryable`（worker 子进程 → coordinator 子进程，client 查询） |
| 14 | peers 旧格式读/新格式写/回读 | `test_peer_directory.py`（8 项）+ `test_cli.py::test_peers_add_output_is_loadable_by_load_config` |
| 15 | 新消息 schema/长度/recipient | `test_protocol.py::TestCapabilityMessages`（11 项） |
