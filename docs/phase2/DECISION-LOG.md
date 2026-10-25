# Phase 2 架构决策记录（ADR）

格式：编号 / 决策 / 理由 / 取舍（放弃的方案）/ 证据或来源。

## D-001：Phase 2 在 `phase2` 分支推进

- 决策：从 dev@e3e6d5a 创建 `phase2` 分支，回滚点 = dev。
- 理由：隔离分支提供可回滚点；基线（57 测试）已备份为 git bundle。
- 取舍：直接在 dev 上开发（更快但无干净回滚）。
- 证据：backups/idle-compute-phase2-20261026/idle-compute-dev-e3e6d5a.bundle

## D-002：节点身份采用 Ed25519（cryptography 库）

- 决策：Ed25519 密钥对作为节点身份；消息用 Ed25519 签名；HMAC LocalSigner
  保留为阶段 1 单机完整性机制的遗留用途，**不作为**节点身份（文档显式标注）。
- 理由：指令要求"独立的 Ed25519 身份"；cryptography 48.0.0 已安装（零新增安装），
  有维护、PyPI 活跃、Windows wheel 可用。
- 取舍：PyNaCl（额外安装）；纯 Python ed25519 包（无维护）。
- 证据：phase2-A-evidence.md §4（cryptography 48.0.0 INSTALLED）

## D-003：libp2p 评估（完成，采用）

- 决策：**采用 libp2p==0.8.1 作为 Phase 2 传输层**，封装为薄适配器
  `Libp2pTransport`（实现既有 `Transport`/`PeerDirectory` 接口）；
  标准库 TCP 回退保留为单模块应急预案；依赖精确钉版（trio==0.30.0）。
- 理由：真实评估通过——2026-10-22 发布（有维护）；py3.12/Windows 安装成功；
  双 host 回环连接 + stream 收发冒烟测试真实通过（CONNECT OK / REPLIES）。
  指令要求"有维护、可复现、与现有技术栈兼容"，三项证据均满足。
- 约束（评估实测）：节点事件循环必须是 trio（asyncio 桥接崩溃）；必须钉
  trio==0.30.0（默认 0.34 崩溃，libp2p pin 无上限）；connect 必须传 PeerInfo。
- 取舍：标准库 TCP（完全可控但非 libp2p、需自造多路复用/协议协商）；
  PyNaCl/其他 P2P 库（无维护或无 Windows 支持）。
- 证据：logs/idle-compute/libp2p-eval/EVALUATION.md（含全部真实输出）

## D-002 修订（仲裁者）：单一 Ed25519 密钥对，libp2p peer-id 即 node_id

- 修订内容：每个节点身份 = 一个 libp2p Ed25519 密钥对，由 32 字节随机种子
  （`var/<node>/identity.key` 原始字节，与 Phase 1 ledger.key 同纪律）派生。
  密钥对 = `libp2p.crypto.ed25519.create_new_key_pair(seed)`（确定性，已验证）。
  node_id = `ID.from_pubkey(kp.public_key).to_string()`（'12D3KooW...'）。
  同一密钥对传入 `new_host(key_pair=kp)`，因此传输层 peer id 就是 node id
  （实测 `host.get_id() == ID.from_pubkey(kp.public_key)`）。
- 理由：拆分身份（node_id + 独立 peer-id + 绑定表）多一套身份系统、一个可失步
  映射表、每消息一次解析——纯增攻击面，且与已验证的 `new_host(key_pair=...)`
  能力矛盾。统一身份使 sender==transport-peer 不变量成为直接字符串比较。
- 关键实现约束：
  - 不用 libp2p 的 protobuf `save_identity`（O_TRUNC 破坏创建竞态保证）；
    自写 O_EXCL + 部分文件重试（沿用 LocalSigner 纪律）。
  - cryptography==50.0.2 保留为（a）测试交叉验签（已验证双向互通、同种子同公钥）、
    （b）应急派生路径（`Ed25519PrivateKey.from_private_bytes(seed)`）。
  - 验签无密钥分发：`ID.from_base58(sender).extract_public_key().verify`（已验证）。
- 证据：仲裁计划 final_decisions #1；libp2p-eval/EVALUATION.md。

## D-004：传输与协议决策（仲裁者输出 2026-10-26，18 项，摘要）

完整 18 项决策、任务看板、12 风险、6 开放问题、分波次序见
`C:\Users\linxi\AI-Work\logs\idle-compute\phase2-plan-arbiter.json`。
以下为落地摘要：

1. **节点身份**：见上 D-002 修订（单一 libp2p Ed25519 密钥对）。
2. **HMAC 范围**：HMAC 仅保留给 11 条 Phase 1 本地单机命令（代码不变、57 测试不动）；
   网络签名全 Ed25519（信封 node-msg-v2 / 结果 result-v2 / 记账 credit-v2）。
   schema v2 迁移纯附加：credit_events/results 各加
   `sig_algo TEXT NOT NULL DEFAULT 'hmac-sha256'`；旧事件绝不重签；
   网络侧硬拒 hmac 行（结构化 CONFLICT）；网络协调者使用全新 store（零遗留行）。
   两条隔离测试：LocalSigner 仅被遗留路径引用；网络 store 进不了 hmac 行。
3. **账本权威与诚实边界**：协调者是唯一权威（DB、发布、仲裁、验证、发分、扩链、
   租约时钟、过期）；工人持已验证本地镜像、分歧即停 claim 并报 CONFLICT，绝不回卷。
   哈希链 = 防篡改证据，**不是**共识——docs/COORDINATION.md 逐字三类表；
   Phase 1 尾删不可测发现照实继承。
4. **传输适配与导入遏制**：新增 net_transport.py（唯一 host/stream 适配器）；
   libp2p 仅允许出现在 identity.py（密码原语）与 net_transport.py（host/stream），
   由扩展源码安全测试强制；p2p.py ABC 保持纯标准库不变；
   新增 transport_tcp.py 纯标准库 TCP 回退（同契约测试，零非标准库导入）。
   生产路径 trio 原生，无 asyncio（grep 门禁）。
5. **线上协议**：单协议 id '/idle-compute/1.0.0'；帧 = 4 字节 BE 长度前缀 + UTF-8 JSON；
   硬上限 65536 B/帧、32768 B/体、JSON 深度 16；长度先于分配验证。
   信封 `{v:2,type,sender,recipient,seq,ts,in_reply_to,body,sig}`；
   签整个信封减 sig（域 idle-compute-node-msg-v2），seq/ts/recipient 均受完整性保护。
   入站检查顺序（任何失败即弃/ERROR，绝不深入解析）：transport peer==sender →
   帧/体上限 → JSON 解码 → seq 窗口 (last_seen, last_seen+4096]（持久化）→
   ts ±300s 拒 / ±600s 硬弃 → Ed25519 验签（先于任何体解释）→ 封闭注册表 → 令牌桶
   （20 msg/s 突发 40）→ 体模式校验。任务类型先对照 TASK_TYPES 注册表再解析载荷。
6. **拓扑**：严格请求/响应；工人拨协调者（协调者永不拨号、永不推送）；
   无 TASK_PUBLISH 推送（工人轮询 CLAIM min(5s, lease/4)）；无 CREDIT_NOTICE
   （RESULT_ACK 内联携带签名记账事件）；非参与者经 LEDGER_SYNC 拉取。
   消息集：HELLO/HELLO_ACK、TASK_CREATE/TASK_CREATE_ACK、CLAIM/CLAIM_GRANT、
   START/START_ACK（保留 QUEUED->LEASED->RUNNING）、RENEW/RENEW_ACK、
   RESULT_SUBMIT/RESULT_ACK、STATUS_REQ/STATUS_RESP、LEDGER_SYNC/LEDGER_SYNC_RESP、
   PING/PONG、ERROR。
7. **内部签名**：仅两个域——result-v2（工人签，持久化于 results.signature）、
   credit-v2（协调者签记账事件体）；**放弃**租约签名（信封签名+钉定协调者 node_id
   已是强制信任锚；claims 表幂等重取覆盖重启场景）。域升 v2 防与 Phase 1 载荷串味。
8. **幂等契约**：CLAIM 带 worker 生成的 claim_request_id（claims 表持久化，重传返回
   同一租约）；重复 RESULT_SUBMIT 同 content_hash 返回存回判定、异 hash 报
   ERROR(DUPLICATE)；记账幂等沿用 Phase 1（validate_result 早退 + UNIQUE(idempotency_key)）；
   START 幂等；TASK_CREATE 带 client_ref（client_refs 表持久化，300s 清理）。
9. **状态机**：Store 是唯一 ensure_transition 调用者；ACK 镜像 Store 观察到的转移；
   验证同步运行于 RESULT_SUBMIT 处理器内；Phase 1 SUBMITTED 崩溃窗口恢复原样运行。
10. **节点准入**：静态 node_id 白名单，默认拒绝；无令牌/口令/注册协议/TOFU；
    白名单只存 node_id 字符串（公钥嵌于 multihash，验签时提取）；配置无秘密材料。
11. **发现机制**：静态配置 + rendezvous 文件（`var/run/<node_id>.addr` 写实际监听
    multiaddr）；enable_mDNS=False、enable_upnp=False 处处；仅绑定 127.0.0.1；
    无对等方交换协议、无 DHT、无 NAT 穿越（grep 级验收测试）。
12. **时间与租约权威**：协调者时钟权威；工人把 expires_at 当不透明截止时间、
    50% 续租；续租帽 2x/3600s/10 次；过期/重排队仅在协调者。
13. **工人本地 DB**：var/<node>/worker.db（worker_tasks/ledger_copy/node_state/meta），
    纯派生状态、随时可删；启动时若 sync_cursor 起的链可对钉定协调者公钥验证则增量，
    否则丢弃 ledger_copy 从 seq 0 全量重建；余额只从已验证 ledger_copy 计算；
    无进程打开他人 DB（配置校验拒绝重复 db_path）。
14. **CLI 面**：11 条 Phase 1 命令在遗留本地路径字节兼容；新命令全 JSON：
    identity / peers add|list / node run / client create-task|status|balances /
    worker ledger sync|status。网络命令只收 --config（绝不 --db/--key）。
15. **资源与速率限制**：一个可审计常量块（protocol.py+ratelimit.py+config.py）：
    帧 65536/体 32768/深度 16；入站 20 msg/s 突发 40；60s 内 2 次触发关流 30s；
    每对等方最多 8 并发流；出站队列 100；工人 1 并发任务/1 活跃租约；协调者最多
    1000 排队任务（#1001→OVERLOAD）；lease_seconds 5..3600；LEDGER_SYNC 每批 ≤100；
    流读超时 max(10s, lease)；ts 容差 ±300/±600；seq 窗口 4096。全部机器可测。
16. **错误隔离与日志脱敏**：每入站流处理器单一隔离边界（异常→脱敏 ERROR 帧→仅关
    该流，节点循环绝不为对等方错误退出）；13 个 ERROR 码；日志经 log.py 脱敏
    （node id 12 字符、签名/哈希 8 字符、体与数字只记计数），捕获测试证明任意级别
    日志无密钥字节/密钥路径。
17. **依赖与 API 钉版**：requirements.txt 钉 libp2p==0.8.1、trio==0.30.0、
    cryptography==50.0.2（与仓库内已提交 lock 文件一致；提案中 48.0.0 为过时信息）；
    节点启动断言 trio.__version__=='0.30.0' 快速失败；idle_compute/ 内无 asyncio。
18. **范围与诚实边界**：Phase 2 验收 = 回环 3 真实进程；LAN 多设备明确为
    Phase 3 可选项（操作者钉定地址），Phase 2 不触碰防火墙/mDNS/UPnP；
    无共识、无 DHT、无 NAT 穿越、无 OS 沙箱、无外部锚点；密钥跨用途复用以域分隔
    理由记录在案。
