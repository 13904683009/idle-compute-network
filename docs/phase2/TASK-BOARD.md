# Phase 2 任务看板

状态图例：`todo` 未开始 / `doing` 进行中 / `done` 完成（验收通过）/ `blocked` 阻塞（原因见决策日志）
每项任务记录：负责人、输入、输出、验收标准、状态。
规划来源：规划工作流 wf_e43542ca-921（3 架构师 + 仲裁者）输出，
见 `C:\Users\linxi\AI-Work\logs\idle-compute\phase2-plan-arbiter.json`。

## 阶段 A（基线审计与备份）— 完成 2026-10-26

| ID | 任务 | 负责人 | 状态 | 验收标准 | 证据 |
| --- | --- | --- | --- | --- | --- |
| A-1 | 工作区/Git/依赖真实核验 | 主循环 | done | git=dev@e3e6d5a 干净；57 测试 exit 0 | phase2-A-evidence.md §1–2 |
| A-2 | 复核三处未复审修复 | 主循环 | done | 三处在代码中存在且测试覆盖 | 同上 §3 |
| A-3 | 备份 + 回滚点 + phase2 分支 | 主循环 | done | bundle 存在；分支创建 | 同上 §5 |
| A-4 | libp2p 真实兼容性评估 | 主循环 | done | 安装/导入/双 host 回环收发冒烟通过 | logs/idle-compute/libp2p-eval/EVALUATION.md |

## 阶段 B（Ed25519 节点身份）— 里程碑检查点阶段

| ID | 任务 | 负责人 | 状态 | 验收标准 | 证据 |
| --- | --- | --- | --- | --- | --- |
| B-1 | NodeIdentity + Ed25519Signer：种子文件、node_id 派生、签名/验签 | 主循环 | done | tests.test_identity 全绿：load_or_create 幂等且 node_id 跨重载稳定；O_EXCL 创建竞态（输家等待完整密钥）；部分密钥文件响亮失败；Windows ACL 警告；签名往返/篡改拒绝/错钥拒绝；node_id 与 ID.from_pubkey 一致且经 ID.from_base58 往返；extract_public_key().verify 可用；cryptography 50.0.2 交叉验签；identity.key 恰 32 字节 | 本阶段测试日志 |
| B-2 | crypto.py v2 签名载荷构造 + signing.py 方案策略；LocalSigner 标注 LEGACY-ONLY | 主循环 | done | tests.test_crypto+test_signing 全绿；node_msg_payload/result_payload/credit_body 三个 v2 域构造器 + 金标准哈希固定；canonical_json/content_hash/sha256_hex/GENESIS_HASH 不变；Ed25519Policy/HmacLegacyPolicy 同接口；grep 证明 crypto.py/signing.py 不导入 libp2p | 本阶段测试日志 |
| B-3 | db.py schema v2 附加迁移 + node_state/claims 表 + 可注入签名/验签策略；ledger.py 逐行按方案验签 + 批量导出 | 主循环 | done | 57 项既有测试原样全过（HMAC 默认）；v1 DB 原地迁移 schema_version='2' 且 sig_algo 默认 'hmac-sha256'；网络模式 Ed25519 记账+验账通过；篡改 sig/prev_hash/amount 验账失败；hmac 行在 Ed25519Policy 下结构化 CONFLICT；validate_result 接受工人 Ed25519 签名并拒绝异钥；node_state/claims/client_refs 三表存在；one_active_lease/UNIQUE 约束存在；ledger.py export_since+verify_and_append | 本阶段测试日志 |
| B-4 | CLI：identity + peers 命令；密钥永不打印测试 | 主循环 | done | identity 两次同 node_id、删钥后不同；stdout/stderr 无 identity.key/ledger.key 字节；peers add/list 幂等；畸形 node_id 结构化报错；11 条旧命令测试全过 | 本阶段测试日志 |

## 阶段 C（传输层 + 节点运行时）

> 执行说明：WAVE 0（提交 269e1cf）交付 C-1/C-2/C-4；WAVE 1（提交 14cc40a）把
> C-3 与 D-1/D-2/D-3 的应用协议核心合并进 `idle_compute/node.py` 单一运行时
> （协调者 actor / 工人拉取循环 / client 一次性操作），以 15 项真实多进程 E2E
> 测试（tests/test_node.py）验收。RENEW 续租与 LEDGER_SYNC 按原计划留在阶段 E/F。
> 审计修复轮 1（提交 07e7fc2）：对抗审计（wf_c4b211ee-5e6）24 项发现中 21 项确认
> 全部修复（对称默认拒绝 / 有界 seq 窗口 / claim 幂等不铸新租约 / 同 peer 重放原
> RESULT_ACK / seq 回滚自愈 / ERROR 不回 / 流上限 64 / 等等），新增对抗回归测试，
> 全量 **249 测试全绿**。

| ID | 任务 | 负责人 | 状态 | 验收标准 | 证据 |
| --- | --- | --- | --- | --- | --- |
| C-1 | net_transport.py Libp2pTransport 适配器 + 双真实进程冒烟 + 导入遏制测试 | 主循环 | done | 依赖钉版核对；两个独立进程经 PeerInfo+临时端口+addr 文件交接回环互发 PING/PONG；stream.muxed_conn.peer_id 解析远程对等方；帧编解码单测无 libp2p；libp2p 仅出现在 identity.py+net_transport.py；无 asyncio | tests/test_transport_libp2p.py + test_net_transport.py（16/16 绿，含真实子进程冒烟）；提交 269e1cf |
| C-2 | protocol.py：信封、帧编解码、封闭类型注册表、先验签后解析管线 | 主循环 | done | 信封金标准哈希；域 'idle-compute-node-msg-v2'；seq 窗口 (last_seen, last_seen+4096]；ts ±300/600s；先验签后解析顺序可观测；65537 字节帧不分配缓冲即拒；16 个 ERROR 码；JSON 深度 17 拒 | tests/test_protocol.py 46/46 绿；金标准 log 见 logs/idle-compute/phase2-C-golden.log |
| C-3 | config.py + node.py 运行时 + HELLO/HELLO_ACK/PING/PONG + 白名单 + rendezvous 文件 | 主循环 | done | 配置校验（未知键/重复 db_path/畸形 node_id）；trio 版本启动断言；三真实进程 HELLO 互见；无 mDNS/UPnP/DHT；未列名单 HELLO→UNAUTHORIZED；PING→PONG；SIGINT 路径退出码 0；坏签名 HELLO 丢弃 | tests/test_config.py 41/41 绿；tests/test_node.py 15/15 绿（多进程）；未列名单/坏签名/畸形帧/超大帧关闭流均验证；提交 14cc40a |
| C-4 | transport_tcp.py 纯标准库 TCP 回退模块（单模块应急预案） | 主循环 | done | 与 NetTransport 同契约测试（真实子进程回环）；零非标准库导入；compileall 通过；一行配置可切换（config.transport） | tests/test_transport_tcp.py 全绿（真实回环）；提交 269e1cf |

## 阶段 D（应用协议）

> 已随 WAVE 1 交付（见上注）；RENEW/续租、并发任务上限等按原计划属于阶段 F，未提前实现；
> LEDGER_SYNC 已在阶段 E-1 交付（见下表）。

| ID | 任务 | 负责人 | 状态 | 验收标准 | 证据 |
| --- | --- | --- | --- | --- | --- |
| D-1 | 线上处理器（HELLO/CLAIM/SUBMIT/STATUS/PING/TASK_CREATE，node.py 协调者侧） | 主循环 | done | TASK_CREATE 先过白名单；CLAIM 幂等（已知 claim id 不铸新租约，INSERT OR IGNORE）；正确结果→RESULT_ACK VALID+内联签名记账；错输出→INVALID 无记账；同 peer 重复提交→原 RESULT_ACK 重放单记账、异 peer→TASK_UNAVAILABLE 不泄露；过期租约→重排队语义；全部状态转移经 Store 状态机（LEASED→RUNNING→SUBMITTED→VALIDATED） | tests/test_node.py：duplicate claim/submit、foreign replay、forged result、abandoned-lease requeue 等对抗测试绿；提交 07e7fc2 |
| D-2 | 工人循环 + ledger_copy 镜像（claim/execute/sign/submit/backoff/resume，node.py 工人侧） | 主循环 | done | 以本节点密钥签 result-v2；credit 事件经 verify_and_append（验签+哈希链）后才入 ledger_copy；协调者死亡→1..8s 退避重拨；重启续跑（seq.json 持久化）；畸形 grant 不执行（schema 先校验） | tests/test_node.py：restart recovery、coordinator restart reconnect、mirror verified=1 断言绿 |
| D-3 | cli.py node 命令 + 3 真实进程端到端幸福路径 | 主循环 | done | 真实多进程（协调者+工人子进程+client 驱动）：create-task 返回 task_id；工人执行提交；DB COMPLETED/恰 1 记账事件；status 显示 VALID+credits_count；第二任务被重启工人接走；杀工人→过期重排队→他人接走；seq.json 回滚后自愈；249 测试全量回归绿 | tests/test_node.py 15/15（TCP 与 libp2p 双传输）；提交 14cc40a + 07e7fc2 |

## 阶段 E（账本同步）

| ID | 任务 | 负责人 | 状态 | 验收标准 | 证据 |
| --- | --- | --- | --- | --- | --- |
| E-1 | LEDGER_SYNC 分页拉取 + 工人链验证至字节一致镜像 | 主循环 | done | 同步后 ledger_copy 与协调者链逐列一致（13 列逐字段比对 + 规范序列化逐字节一致）；分页 count≤100 且单页序列化≤24 KiB，无缺无重、尾页 `more=False`；after_seq 超头→空页+当前头；每事件验签+prev_hash+amount>0 后才追加；坏事件不追加且错误信息带 event_id/seq | tests/test_ledger_sync.py 9/9（真实子进程 150 事件镜像逐列一致、断点续传、坏事件截断、线 schema 一致 + `event_type` 漂移守卫）；tests/test_node.py ShutdownTests 新增 DB 句柄释放回归；全量 **259/259**，compileall exit 0；提交 `d2046bf` |
| E-2 | 工人冲突检测 + 隔离 + 删除重建恢复 | 主循环 | done | 篡改链→工人锁存 CONFLICT、在坏事件处停住（不跳过/不覆盖/不回卷）并停止 claim；每次启动从 seq 0 重审整条镜像链（分页续审、可跨轮），已镜像事件的原地篡改可检出；`worker ledger status` 输出镜像头/已验数/冲突 seq+原因/claim_blocked；删 worker.db→从 0 重建逐列一致；重复轮次与重启不重复计分；hmac 行→结构化 CONFLICT（线 schema 不整页拒绝，避免退化成静默超时） | tests/test_ledger_conflict.py 4/4（真实子进程 + CLI 断言）；全量 **263/263**，compileall exit 0；提交 `bf35356` |
| E-3 | docs/COORDINATION.md 诚实表 + 架构/协议/威胁模型更新 | 主循环 | done | 三类表逐字落档（协调者权威/工人可验证/明确非去中心化）；"单写者 + 协调者签名的哈希链 = 防篡改证据，不是共识"表述；THREAT_MODEL 第二阶段新增 13 行威胁（含"未实现/未验证"标注）；grep 级测试（tests/test_docs.py，7 项）禁止任何文档声称共识/DHT/mDNS/UPnP/多设备已验证/生产可用 | 新增 `docs/COORDINATION.md`；重写 `docs/PROTOCOL.md`（v2 线协议全量：帧/信封/验签顺序/16 消息/16 错误码/流程/限额）；`docs/ARCHITECTURE.md` 增第二阶段；`docs/THREAT_MODEL.md` 增第二阶段；全量 **270/270**，compileall exit 0；提交 `c555ea5` |

> **E-1 分页口径修正（实测）**：验收原文写"150 事件→2 批（≤100）"。实测单条 ed25519
> 记账事件序列化约 600 B，而 `LEDGER_SYNC_RESPONSE` 必须装进 32 KiB 的 body 上限，
> 因此分页同时受 count（≤100）与字节（≤24 KiB）双钳制，150 事件实际为 4 页
> （约 42/42/42/24）。"2 批"的分页假设不成立；可验收的是分页契约本身
> （无缺无重、尾页 `more=False`、超头空页），已由 tests/test_ledger_sync.py 固定。

## 阶段 F（安全加固）

| ID | 任务 | 负责人 | 状态 | 验收标准 | 证据 |
| --- | --- | --- | --- | --- | --- |
| F-1 | ratelimit.py + 持久化 seq 窗口 + ts 容差对抗测试 | 主循环 | done | 令牌桶数学（20 msg/s、突发 40）用注入时钟确定性验证；真实洪泛 → 2 次触发/60 s → 关流 + node_id 隔离 30 s（TCP 重连在 HELLO 阶段即被拒），其他 peer 不受影响；重启后重放旧帧被**持久化**窗口拒绝（`SEQ_OUT_OF_WINDOW`）；窗口缺口/越界、±300 s STALE、BAD_SENDER 先于验签、旧 ACK 重放不双发由既有测试覆盖并在此引用 | tests/test_ratelimit.py 8/8（6 确定性单元 + 2 真实 socket：洪泛隔离、重启重放拒绝）；既有覆盖：test_protocol.py（窗口/时间戳/顺序）、test_node.py（BAD_SENDER、重复提交单积分）；全量 **278/278**，compileall exit 0；提交 `0644870` |
| F-2 | 错误隔离 + 洪泛/超大帧/资源上限测试（运行中的节点） | 待定 | todo | 50 消息/秒洪泛触发令牌桶；超大帧分配前重置；第 1001 任务→OVERLOAD；单流异常不影响他流；200 畸形帧模糊测试后节点仍服务；节点进程在虐待后仍存活 | — |
| F-3 | 线边界输入校验 + 仅注册表分发 + 扩展源码安全扫描 | 待定 | todo | 垃圾帧/未知类型/非白名单任务类型/5000 数载荷/bool 载荷/深嵌套/多余键/shell 元字符均拒绝且不到达执行器；全源码扫描无 eval/exec/subprocess/importlib 等；libp2p 仅两处；无 asyncio；TASK_TYPES 唯一分发点 | — |
| F-4 | log.py 脱敏 + 白名单策略测试 | 待定 | todo | 每条脱敏规则测试（node id 12 字符/签名 8 字符/载荷只记计数）；3 进程对抗运行捕获日志无密钥字节/无密钥路径/无完整签名；未列名单 HELLO→UNAUTHORIZED 关流 | — |

## 阶段 G（集成验收）

| ID | 任务 | 负责人 | 状态 | 验收标准 | 证据 |
| --- | --- | --- | --- | --- | --- |
| G-1 | 10 场景真实子进程集成套件 | 待定 | todo | 每场景真实子进程（独立进程/配置/DB/密钥，仅回环，port 0+addr 文件，看门狗清理）：并发 claim 恰 1 租约；重复提交单记账；断连重排队；运行中过期拒付；双端重启恢复无双花；错误结果 REJECTED；伪造身份双路径断言；账本篡改检测+隔离；重放无双花；2 工人×20 任务终态合法+余额一致。套件连跑两遍全绿 | — |
| G-2 | scripts/run_three_nodes.py + 证据采集 + 全量套件门禁 + 文档冻结 | 待定 | todo | 脚本连跑两遍干净；证据日志+JSON 转录；unittest discover 全绿；compileall 通过；无 *.db/*.key 入库；ACCEPTANCE.md 列出每项声明对应命令与输出；STATUS.md 标注回环已验证、LAN 明确"设计未验证"；PHASE2-DESIGN.md 冻结设计供独立审计 | — |

## 阶段 H（独立审计）与 I（最终交付）— 待前序阶段完成后细化

- H：独立审计 Agent（只依据真实 diff 与测试日志，不复述执行方结论），缺陷 ≤3 轮修复，证据保全。
- I：最终报告 + 全部交付物 + 追加 AI-WORK-COMPLETE-REPORTS-20261022.txt（先确认文件存在与格式，绝不覆盖历史条目）。

## 关键分波次序（仲裁者输出，绑定）

- WAVE 0（并行，无共享文件）：B-1 ‖ C-1 ‖ C-2 ‖ C-4
- WAVE 1（核心迁移，串行）：B-2 → B-3（db.py/ledger.py/coordinator.py/worker.py 仅在 B-3 内编辑；ledger.py 第二次编辑推迟到 E-1）；并行：C-3 的 config 部分、C-4、C-2 进行中的协议工作
- WAVE 2（节点运行时）：C-3（依赖 C-1+C-2+B-1）；B-4（依赖 B-1+B-3，cli.py 本轮唯一编辑者）
- WAVE 3（应用协议）：D-1（依赖 B-3+C-3）→ D-2 → D-3（依赖 D-2+C-3+B-4）；cli.py 编辑次序 B-4→D-3 严格串行
- WAVE 4（账本同步+文档）：E-1（依赖 D-1，ledger.py 在 B-3 后串行）→ E-2；E-3 与 E-2、全部 F 并行（文件不相交）
- WAVE 5（安全加固，多数并行）：F-1（依赖 C-2+D-3）‖ F-2（依赖 D-3）‖ F-3（依赖 D-3）‖ F-4（依赖 C-3+F-2）；protocol.py 次序 C-2→F-1→F-3 串行；node.py 次序 C-3→D-3→F-2 串行
- WAVE 6（集成门禁）：G-1（依赖 D-3+E-2+F-4）→ G-2；G-2 的文档工作可与 G-1 并行起草

## 共享核心文件串行规则（绑定）

crypto.py 仅 B-2；db.py 仅 B-3；coordinator.py/worker.py 仅 B-3；ledger.py B-3→E-1 严格串行；
cli.py B-4→D-3 严格串行；node.py C-3→D-3→F-2 严格串行；net_transport.py 仅 C-1；
protocol.py C-2→F-1→F-3 串行；p2p.py 冻结；config.py 创建于 C-3。
