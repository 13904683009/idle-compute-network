# 网络协议 v2（Phase 2 实现版）

本文件描述**已实现并测试**的线协议。协议 id：`/idle-compute/1.0.0`，
签名域版本 `v2`。Phase 1 的本地 HMAC 路径另见 §11；两者不混用。

## 1. 传输与分帧

| 项 | 规则 |
| --- | --- |
| 传输 | `transport=tcp`（纯标准库 IPv4）或 `transport=libp2p`（libp2p 0.8.1 / trio 0.30.0）；一行配置切换 |
| 监听 | 默认仅回环；跨设备需显式配置私有地址并做独立安全评估（本阶段未做） |
| 帧 | 4 字节大端长度前缀 + UTF-8 JSON；**长度先校验，再分配读缓冲** |
| 上限 | 帧 64 KiB（65536）、body 32 KiB（32768）、JSON 嵌套深度 16 |
| 并发 | 每个 peer 最多 64 条并发流；有序性不依赖单流 |
| 关闭 | 任一校验失败 → 回 ERROR（可回复时）并关闭该 peer 的流；节点本身继续服务其他 peer |

## 2. 信封

```json
{
  "v": 2,
  "type": "SUBMIT_RESULT",
  "sender": "12D3KooW…",
  "recipient": "12D3KooW…",
  "seq": 7,
  "ts": "2026-11-04T09:00:00+00:00",
  "in_reply_to": 6,
  "body": { },
  "signature": "<128 hex chars, Ed25519>"
}
```

- `seq`：发送方单调递增；持久化在 `seq.json`（`os.replace` 原子写），重启不重放。
- `ts`：UTC ISO-8601；容差 ±300 s，>600 s 直接丢弃。
- `in_reply_to`：请求/响应对应关系；回复必须指向被回复消息的 `seq`（可为 `null`）。
- `recipient`：显式收件人，防误投；`null` 表示广播语义（当前仅握手前使用）。

### 签名构造

```
canonical = canonical_json({domain:"idle-compute-node-msg-v2", v, type, sender,
                            recipient, seq, ts, in_reply_to, body})
signature = Ed25519(sender_private_key, canonical)      # 128 hex
```

- `canonical_json`：键排序、无多余空白、UTF-8、无 NaN/Infinity。
- 验签公钥**不需要分发**：`node_id` 是 libp2p peer id，内含 Ed25519 公钥；
  接收方 `ID.from_base58(sender).extract_public_key()` 直接取公钥验签。
- 签名覆盖 `seq/ts/recipient/body` 全部字段，因此**任何一处被改都会验签失败**。

## 3. 入站验证顺序（先验签后解析，顺序可观测并被测试固定）

1. 帧长度 ≤ 64 KiB（分配缓冲前判断）；
2. 长度前缀对应的字节完整读到；
3. JSON 深度 ≤ 16，UTF-8 解码；
4. 信封形状/类型/`v` 合法（`MALFORMED_ENVELOPE`）；
5. `sender` 必须等于传输层对端身份（TCP 在 HELLO 绑定后生效，libp2p 用 `muxed_conn.peer_id`；失败 → `BAD_SENDER`）；
6. Ed25519 验签（失败 → `BAD_SIGNATURE`，**此前不解释 body**）；
7. 每 sender 的序号窗 `(last_seen, last_seen+4096]`（越界 → `SEQ_OUT_OF_WINDOW`，窗口持久化在 `node_state`）；
8. 时间戳容差（`STALE_TIMESTAMP`）；
9. 封闭注册表里的 body 精确键 schema（多余/缺失键 → `INVALID_BODY`；未知类型 → `UNSUPPORTED_TYPE`）。

> 顺序的意义：任何"解释 body"的动作（含 schema 校验）都发生在验签之后，
> 未知/伪造消息不会进入业务解析路径。

## 4. 消息目录（16 种，封闭注册表）

| 类型 | 方向 | body 键（精确） |
| --- | --- | --- |
| `HELLO` / `HELLO_ACK` | 双向 | `node_id, role, listen_addrs` |
| `PING` / `PONG` | 双向 | `nonce` |
| `ERROR` | 任意 | `code, message, ref_type, ref_seq` |
| `TASK_CREATE` | client→coord | `client_ref, task_type, payload, max_attempts, lease_seconds` |
| `TASK_CREATE_ACK` | coord→client | `client_ref, task_id` |
| `CLAIM_REQUEST` | worker→coord | `claim_request_id, task_id`（`task_id` 可为 `null`） |
| `CLAIM_GRANT` | coord→worker | `claim_request_id, lease, task` |
| `CLAIM_DENIED` | coord→worker | `claim_request_id` |
| `SUBMIT_RESULT` | worker→coord | `lease_id, output, content_hash, signature` |
| `RESULT_ACK` | coord→worker | `result_id, lease_id, task_id, validation, reason, credit` |
| `STATUS_REQUEST` / `STATUS_RESPONSE` | client→coord | `task_id` / `task_id, status, result, validation, credits_count` |
| `LEDGER_SYNC_REQUEST` | worker→coord | `after_seq` |
| `LEDGER_SYNC_RESPONSE` | coord→worker | `after_seq, head_seq, events, more` |

- `*_ACK`/`DENIED` 的 `in_reply_to` 必须指向对应请求的 `seq`。
- `ERROR` 永不回复 `ERROR`（防回声风暴）。
- `RENEW`/`RENEW_ACK` **未实现**（阶段 F 计划）。

## 5. 错误码（16）

| code | 名称 | 触发 |
| --- | --- | --- |
| 1 | `UNAUTHORIZED` | 发送者不在 `peers` 白名单 |
| 2 | `BAD_SIGNATURE` | 信封签名无效 |
| 3 | `MALFORMED_ENVELOPE` | 形状/类型/版本/编码错误 |
| 4 | `UNSUPPORTED_TYPE` | 不在封闭注册表 |
| 5 | `INVALID_BODY` | body schema 不合法（含超长/深嵌套） |
| 6 | `STALE_TIMESTAMP` | 时间戳超出容差 |
| 7 | `SEQ_OUT_OF_WINDOW` | 序号不在窗口内（worker 据此自愈跳变） |
| 8 | `BAD_SENDER` | `sender` ≠ 传输层对端 |
| 9 | `FRAME_TOO_LARGE` | 帧超 64 KiB |
| 10 | `TASK_UNAVAILABLE` | 无可领取任务 / 状态不符 / 非本人租约 |
| 11 | `LEASE_EXPIRED` | 租约已过期 |
| 12 | `DUPLICATE` | 冲突性重复（同 task+lease 但内容不同） |
| 13 | `REJECTED` | 结果验证失败 |
| 14 | `NOT_FOUND` | 未知 task/lease/result |
| 15 | `OVERLOAD` | 资源上限（工作队列/任务数） |
| 16 | `INTERNAL` | 未预期内部错误（不回内部细节） |

## 6. 关键流程

### 6.1 握手（双向 HELLO）

worker/client 拨号后，**首帧必须是 `HELLO`**；协调者校验白名单后回 `HELLO_ACK`，
双方把该流绑定到对端 `node_id`。未绑定流上除 `HELLO`/`ERROR` 外的消息一律拒绝。
TCP 的对端身份是占位符，绑定发生在 HELLO **之后**；libp2p 的 `muxed_conn.peer_id`
在帧解析前就可用，故 `BAD_SENDER` 可先于验签生效（两种传输的绑定时点不同，属设计取舍）。

### 6.2 发布任务

`TASK_CREATE` → 白名单校验（仅 `numeric_batch_v1`）→ `client_ref` 幂等（重试返回同一
`task_id`）→ `TASK_CREATE_ACK`。协调者任务队列上限 1000，超出 → `OVERLOAD`。

### 6.3 领取与开始

`CLAIM_REQUEST{claim_request_id, task_id?}` →
`claims` 表 `INSERT OR IGNORE`（**已知 claim_request_id 永不新铸租约**，重传返回同一租约）→
租约窗口内 → `CLAIM_GRANT{lease, task}`；无可领取 → `CLAIM_DENIED`。
同一 worker 同时只能持有 1 个活跃租约（`one_active_lease` 部分唯一索引兜底）。

### 6.4 提交、验证、记账

`SUBMIT_RESULT{lease_id, output, content_hash, signature}`：

1. 同 `(task_id, lease_id)` 且内容相同 → **重放原 `RESULT_ACK`**（幂等，不重复计分）；
2. 内容不同 → `DUPLICATE`（并记为篡改信号）；
3. 租约过期 → `LEASE_EXPIRED`（按 Phase 1 语义重排队/EXPIRED）；
4. 协调者**独立重算** + 内容哈希 + 工人 Ed25519 验签（域 `idle-compute-result-v2`）→
   通过才写 result/validation 并追加 `credit-v2` 记账事件；
5. `RESULT_ACK` 内联 `validation ∈ {VALID, INVALID}`、原因与（VALID 时）已签名 credit 事件。

### 6.5 账本同步

worker 每轮先 `LEDGER_SYNC_REQUEST{after_seq}` → 协调者回一页
`LEDGER_SYNC_RESPONSE{after_seq, head_seq, events, more}`：

- 每页 count ≤ 100 且序列化 ≤ 24 KiB（必须装进 32 KiB body 上限）；
- 每个事件是 `credit_events` 的 13 列导出行（精确键）；
- `more=true` 时用本页最后 `seq` 继续拉；`after_seq` 超头 → 空页 + 当前 `head_seq`；
- worker 对每个事件执行：形状 → 方案 → 重复 → `seq` 连续 → `prev_hash` 链 → 哈希 →
  签名 → 金额 > 0，全部通过才写入 `ledger_copy`（`verified=1`）；
- **每次 worker 启动先从 `seq 0` 重审整条镜像链**（分页、可跨轮），再转增量；
- 任何分歧（篡改、`sig_algo` 非 ed25519、链比镜像短）→ 锁存冲突（记录 offending `seq`）→
  **停止 claim**；不跳过、不覆盖、不回卷。运维面：`worker ledger status`。

### 6.6 幂等与重放

| 场景 | 语义 |
| --- | --- |
| 重复 `CLAIM_REQUEST`（同 id） | 返回同一租约，不铸新租约 |
| 重复 `SUBMIT_RESULT`（同内容） | 返回原 `RESULT_ACK`，不重复计分 |
| 冲突 `SUBMIT_RESULT`（同键不同内容） | `DUPLICATE`，不记账 |
| 旧帧/旧 ACK 重放 | 序号窗口拒绝（`SEQ_OUT_OF_WINDOW`）+ 应用层幂等兜底 |
| 重复同步页 | `verify_and_append` 幂等（内容相同即 no-op，内容不同即 CONFLICT） |

## 7. 签名域与方案标记

| 域字符串 | 签名者 | 覆盖内容 | 存放位置 |
| --- | --- | --- | --- |
| `idle-compute-node-msg-v2` | 每条消息的发送方 | 整个信封（除 `signature`） | 信封 `signature` |
| `idle-compute-result-v2` | worker | `task_id, lease_id, peer_id, result_hash` | `results.signature` |
| `idle-compute-credit-v2` | 协调者 | 事件体（`event_id…metadata`） | `credit_events.signature`、镜像 `event_json.signature` |

- `credit_events.sig_algo` / `results.sig_algo` 标记方案：网络行为 `ed25519`，
  Phase 1 行为 `hmac-sha256`；**网络镜像只接受 `ed25519` 行**，其余为结构化 CONFLICT。
- 域字符串区分 v1/v2，防止与 Phase 1 HMAC 载荷串味。

## 8. 资源与速率（现网值）

| 限制 | 值 | 状态 |
| --- | --- | --- |
| 帧 / body / JSON 深度 | 64 KiB / 32 KiB / 16 | 已实现并有测试 |
| 每 sender 序号窗 | 4096 | 已实现（`node_state` 持久化） |
| 时间戳容差 | ±300 s（>600 s 丢弃） | 已实现 |
| 每页账本同步 | ≤100 事件且 ≤24 KiB | 已实现 |
| 协调者工作队列 | 1024（溢出 → `OVERLOAD`） | 已实现 |
| 任务队列上限 | 1000 | 已实现 |
| 每 peer 并发流 | 64 | 已实现（两种传输） |
| 令牌桶 20 msg/s、突发 40；2 次触发 / 60 s → 关流 + 30 s 隔离 | 已实现（F-1，`ratelimit.py`；键为绑定后的 node_id） |
| 日志脱敏：node id 12 字符 / 哈希与签名 8 字符 / 载荷只记计数 / key 路径 → `<key-file>` | 已实现（F-4，`log.py`；handler 级过滤器兜底） |

## 9. 发现与寻址

静态 `peers` 白名单（`node_id` → 角色/地址）+ rendezvous 文件
`<data_dir>/run/<node_id>.addr`（协调者启动后写入实际监听地址）。
**无 mDNS、无 UPnP、无 DHT、无 NAT 穿透、无 TOFU**；未列名单的 HELLO 直接 `UNAUTHORIZED`。

## 10. 明确未实现 / 未验证

- `RENEW` 续租（长任务会依赖租约过期重试，当前未做续租）。
- 跨设备（LAN/WAN）通信、时钟偏差、防火墙/NAT 行为：**未验证**。
- 传输层加密：依赖 libp2p 默认 security transport，**本项目未显式配置或验证**；
  应用层的信封签名与结果/记账签名是独立于传输的安全边界。
- 多分片、结果聚合、反 Sybil、监控：未实现。

## 11. 与 Phase 1 本地格式的关系

Phase 1 的 11 条本地命令使用 `LocalLoopbackTransport`（进程内队列）与 HMAC
（`ledger.key`，域 `idle-compute-result-v1` / `credit-v1`）。该路径**未改动**、语义不变；
v2 网络路径与它在签名域、`sig_algo` 标记、传输实现上完全分离，互不降级。
