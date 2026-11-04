# Phase C — 节点运行时架构与线协议（NODE-PROTOCOL）

实现版本：提交 `14cc40a`（WAVE 1）；协议基元来自 `269e1cf`（WAVE 0）。
本文档只描述**代码中真实存在**的行为；每节末尾给出对应测试证据。

## 1. 角色与拓扑

三角色共享同一 `idle_compute/node.py` 运行时，由 `config.role` 决定行为：

| 角色 | 监听 | 主动拨号 | 行为 |
| --- | --- | --- | --- |
| `coordinator` | 是（`listen_addrs`） | 否 | 单线程协调者 loop（actor）：处理 TASK_CREATE / CLAIM_REQUEST / SUBMIT_RESULT / STATUS_REQUEST，周期 `expire_leases` |
| `worker` | 是 | 拨号 coordinator | 拉取 loop：连接→HELLO→PING→CLAIM→执行→SUBMIT→等 RESULT_ACK→镜像记账；1..8s 有界退避 |
| `client` | 否（纯出站） | 拨号 coordinator | 一次性操作：create-task / status / ping，之后 `shutdown()` |

- **静态配置、显式信任**：所有角色必须把对方 node_id 列入自己的 `peers` 表（对称默认拒绝）。没有 mDNS/UPnP/DHT/发现机制。
- **rendezvous 文件**：协调者把监听地址写到 `<data_dir>/run/<node_id>.addr`，工人/client 由配置指向该文件取址（测试与双机示例使用该机制交接回环/局域网地址）。
- **worker/client 的 `peers` 中必须包含协调者 node_id**（含 `role` 任意），否则拨号方自己的 gate 会拒绝协调者的 HELLO。
- 协调者一次只处理一个连接（actor 模型）：处理线程只做解析与鉴权，业务进入有界 `queue.Queue(1024)`，溢出回 `OVERLOAD`。

证据：tests/test_node.py（15 项多进程 E2E，TCP 与 libp2p 双传输）、tests/test_transport_tcp.py、tests/test_transport_libp2p.py。

## 2. 传输层

`config.transport` 一行切换：

- `"tcp"` → `transport_tcp.py`：纯标准库 TCP（IPv4），`start()` 监听才生效（client 调 `start()` 会抛 `TransportError`——纯出站角色不监听）。
- `"libp2p"` → `net_transport.py`：libp2p 适配器（多路复用流）；无 asyncio（libp2p 自身事件循环在线程内），外部契约与 TCP 相同（`connect/dial`、`read_frame/write_frame`、`close`、`start/stop`）。
- 帧格式（`protocol.py`，两种传输共用）：4 字节大端长度 + JSON 信封；帧上限 64 KiB、体上限 32 KiB，超限在分配缓冲前拒绝。
- 两种传输并发流上限 64（审计修复）：TCP 的 accept 循环与 libp2p 的流处理器在超限时直接关闭新流，不产生线程。

导入遏制：libp2p 仅出现在 `identity.py` 与 `net_transport.py`（grep 验证）；`transport_tcp.py` 零非标准库导入。

## 3. 信封与验证管线

协议域 `idle-compute-node-msg-v2`；信封字段：`protocol, version, type, sender, seq, ts, in_reply_to, body`（+签名相关字段）。接收方按**固定顺序**验证，先验签后解析：

1. `BAD_SENDER`：发送方声明 ≠ 传输对端身份（libp2p 用 `stream.muxed_conn.peer_id`；TCP 无对端身份，用 HELLO 绑定流后校验——见 §4）。
2. 签名验证（Ed25519，`NodeIdentity.verify_signature`）。
3. seq 窗口：`(last_seen, last_seen+4096]`，按 sender 持久化（node_state 表，重启后旧帧重放仍拒绝）。
4. 时间戳 ±300s（HELLO 握手期间放宽到 ±600s，容忍慢拨号）。
5. 精确键 body schema（`BODY_SCHEMAS`，14 种消息类型各自的允许键集合；多余键/缺失键拒绝）。

解析错误→尽力回 `ERROR`（仅当发送方身份有效），恶意/畸形输入一律关流，不影响其他流。

证据：tests/test_protocol.py（46 项：金标准哈希、seq 窗口、ts 容差、先验签后解析顺序、65537 字节帧、深度 17 拒绝、16 错误码）。

## 4. 流绑定：双向 HELLO 握手

每个新流的**第一帧双方都必须是 HELLO**：

1. 拨号方 connect 后立即发自己的 HELLO。
2. 接听方收到首个 HELLO：校验签名 + sender==body.node_id + sender∈peers → 先回自己的 HELLO，**再**回 HELLO_ACK。
   - 顺序不可颠倒：拨号方的 gate 若先收到 HELLO_ACK 会拒绝（协议先验签后解析的固定顺序）。
3. 拨号方收到对方 HELLO：同样校验白名单 → 绑定该流（TCP 场景从此将流与对端 node_id 绑定，此后发送方声明必须一致，否则 `BAD_SENDER`）。
4. 收到 HELLO_ACK 即交换完成；之后双方在绑定的流上发业务消息。
5. 已绑定流上重复 HELLO → 只回 HELLO_ACK 终止交换（幂等收尾）。
6. 协调者收到 HELLO_ACK/PONG 静默丢弃（这些是拨号方期待的回应，协调者不作为拨号方时不应答）。
7. **ERROR 允许先于 HELLO**：若本端首帧被对端解析期拒绝（如 seq.json 回滚后 HELLO 触发 `SEQ_OUT_OF_WINDOW`），对端的 ERROR 回复在未绑定流上被路由进回复缓冲（`_await` 可见，驱动 seq 自愈）；流保持未绑定，其余类型仍要求 HELLO 先到。
8. **协调者对 ERROR 永不回复**（防回音风暴）；`_on_parse_error` 只在 sender 可解析时回 ERROR，且 `ref_type` 钳制为注册表内的合法类型（敌意 type 字符串不产出违反 schema 的回复）。

未列名单 HELLO / 坏签名 HELLO / 畸形帧 / 超大帧 → 关流（可回 ERROR）。

## 5. 消息类型（16）

| 类型 | 方向 | 作用 |
| --- | --- | --- |
| HELLO / HELLO_ACK | 双向 | 流绑定（§4） |
| PING / PONG | 双向 | 心跳/连通性 |
| TASK_CREATE / TASK_CREATE_ACK | client→coord | 发布白名单任务（幂等：client_ref 重放返回同一 task_id） |
| CLAIM_REQUEST / CLAIM_GRANT / CLAIM_DENIED | worker→coord | 领取任务；grant 内联 lease + task{type,payload}；denied 结束本轮；已知 claim_request_id 永不新铸租约（§6） |
| SUBMIT_RESULT / RESULT_ACK | worker→coord | 提交签名结果；ack 内联 `validation ∈ {VALID, INVALID}` 与 credit；同 peer 重放→**原 RESULT_ACK 重放**（幂等积分）；异 peer→不泄露信息的拒绝（`TASK_UNAVAILABLE`） |
| STATUS_REQUEST / STATUS_RESPONSE | client→coord | 查询任务状态（含 result/validation/credits_count） |
| LEDGER_SYNC_REQUEST / LEDGER_SYNC_RESPONSE | worker→coord | 账本分页拉取：请求 `{after_seq}`；响应 `{after_seq, head_seq, events, more}`，单页 count≤100 且序列化≤24 KiB（必须装进 32 KiB body 上限）；事件为 13 列导出行的精确键 schema（`event_type` 必须等于 `CreditEventType.CREDIT.value`；`sig_algo` 只做长度约束，**非 ed25519 行由镜像侧结构化拒绝**，否则整页会被 schema 丢弃、退化成"静默超时"） |
| ERROR | 任意 | 16 种错误码，`in_reply_to` 指向触发消息的 seq |

续租（RENEW）**未实现**（阶段 F 计划）。
账本同步（LEDGER_SYNC）已在 E-1/E-2 交付：工人每轮主动分页拉取、逐事件验签后写入镜像；
**每次启动先从 seq 0 重审整条镜像链**（分页续审，可跨轮），已镜像事件的原地篡改会被
"内容不一致"捕获；任何分歧（含 hmac 行、链比镜像短）置冲突锁存：不跳过、不覆盖、不回卷，
并在锁存清除前停止 claim。运维面：`idle_compute worker ledger status --config <node.json>`。

## 6. 状态机集成（网络输入不绕过安全边界）

所有网络触发的状态变化都必须经过现有 `Store` 状态机：

- TASK_CREATE：白名单校验 → `store.create_task`（仅 `numeric_batch_v1`，其他类型拒）。
- CLAIM_REQUEST：`claims` 表 **INSERT OR IGNORE**（审计修复：不再 upsert）→ 已知 claim_request_id 从不铸新租约：行已存在且租约仍 ACTIVE 且同 worker → 返回原租约；否则 CLAIM_DENIED；task 处于 `LEASED` 时先 `start_task` 推进到 `RUNNING`（否则后续 submit 会 `InvalidTransition`）→ CLAIM_GRANT。
- SUBMIT_RESULT：**先查重复结果** → 同 worker 重放：回原 RESULT_ACK（含原 credit 事件，幂等，不改任何状态）；异 worker：租约归属门直接拒绝（`TASK_UNAVAILABLE`，不泄露结果是否存在）→ 租约校验（`LEASE_EXPIRED`）→ `validate_result`（独立重算 + 内容哈希 + HMAC/Ed25519 验签）→ VALID 才 `submit_result` 记账。
- 记账：协调者按现有 ledger 哈希链写事件；工人侧有两条入账路径——RESULT_ACK 内联 credit 事件、以及每轮循环先做的 `LEDGER_SYNC` 分页拉取——两条都走 `ledger.verify_and_append`（逐事件验签 + 哈希链 + seq 连续）通过才追加，`verified=1`。任何一条失败都置 `_ledger_conflict` 锁存：不跳过、不覆盖、不回卷，并在该锁存清除前停止 claim。

**失败任务不获得积分**（INVALID→无记账）、**重复任务不重复记分**（同 peer 重放→单记账）、**无效结果不进入已验证状态**——均有对抗测试覆盖（伪造结果、重复 claim、重复提交、异 peer 重放、错输出）。

## 7. 持久化与重启

| 文件 | 内容 | 说明 |
| --- | --- | --- |
| `<data_dir>/identity.key` | 32 字节 Ed25519 种子 | 与配置 node_id 不符则拒绝启动 |
| `<data_dir>/coordinator.db` | Store（tasks/leases/events/claims/node_state/client_refs） | 协调者角色 |
| `<data_dir>/ledger_copy.db` | 工人镜像账本 | 工人角色 |
| `<data_dir>/seq.json` | 出站 seq 游标 | `os.replace` 原子写，重启不重放 |
| node_state 表 | 每 sender 的 seq 窗口 | 重启后旧帧重放拒绝 |

- 工人重启：seq 继续、过期租约由协调者 `expire_leases` 重新排队、恢复后重新 claim（第二任务被重启工人接走的测试覆盖）。
- **seq 回滚自愈**（审计修复）：若 seq.json 丢失/回滚导致 HELLO 落后于协调者窗口，协调者回 `SEQ_OUT_OF_WINDOW`（消息含 `last_seen N`），工人解析后把出站 seq 跳到 N 并重试——不会永久卡死（测试：删除 seq.json 后第二个任务仍被验证）。
- 协调者重启：工人 1..8s 退避重拨，重连后恢复轮询（coordinator-restart 测试覆盖）。
- seq 持久化防御性处理：node_state 读写失败仅记日志（窗口退回内存态），永不杀死处理线程；仅白名单 peer 的窗口被持久化。
- 线程亲和性：Store 与镜像连接各自**懒创建于所属 loop 线程**（sqlite3 连接绑定创建线程），并**在同一个 loop 线程的 `finally` 里关闭**；关闭动作若跨线程执行会被 sqlite3 拒绝，句柄泄漏会让 DB 文件在 shutdown 后仍被锁（E-1 测试暴露并修复）。`_aux` 连接（node_state/claims/client_refs）`check_same_thread=False` + 锁，并在 `__init__` 里先 bootstrap 建 schema（处理线程可能先于 loop 查询）。
- 日志句柄：每个节点的 `log_file` FileHandler 挂在 `idle_compute` logger 上，`shutdown()` 时摘除并关闭；否则文件在节点停止后仍被占用（同上，Windows 下不可删除）。

## 8. 资源与安全限制（现网值）

- 帧 64 KiB / 体 32 KiB；JSON 深度 ≤16；工作队列 1024（满→OVERLOAD）。
- seq 窗口 4096；内存窗口有界（max_entries 1024，溢出时驱逐非白名单条目）；时间戳 ±300s（握手 ±600s）；重试/退避 1..8s 封顶。
- 任务超时/租约时长由任务参数决定（默认 30s 租约、max_attempts 3）。
- 网络默认监听**仅回环或显式配置的地址**；公网监听需独立安全评估与授权（本阶段未做）。
- 密钥/HMAC 密钥/令牌：不写入日志、不出现在任何消息体、不入库（B-4 密钥永不打印测试）。
- 执行器：仅白名单 `numeric_batch_v1`，无 eval/exec/subprocess/动态导入。

## 9. 已知边界（诚实声明）

- `RESULT_ACK` 仅携带 VALID/INVALID；`ValidationStatus.REVIEW` 对白名单任务不会出现在线上。
- TCP 传输仅 IPv4；TCP 接听流的对端身份是占位符 `tcp:host:port`，身份绑定在解析成功后的 HELLO 完成（libp2p 有 muxed-conn 身份，`BAD_SENDER` 先于验签实时生效——两种传输的绑定时点不同，属设计取舍）。
- libp2p 传输的 `start()` 对纯出站角色是 no-op（与 TCP 的抛错行为不对称，配置校验已约束角色与监听地址的组合）。
- 对称默认拒绝：worker/client 的 `peers` 必须包含 `coordinator_node_id`，配置期即报错——缺省配置不再存在"拨号后自我断流"的死循环。
- 双设备真实通信：单机多进程已验收；跨设备步骤文档（见 `TWO-DEVICE-TEST.md`）标注**待完成**，未宣称完成。
- 洪泛令牌桶（F-1）、模糊测试（F-2）、线边界全套（F-3）、日志脱敏（F-4）按计划属阶段 F。
