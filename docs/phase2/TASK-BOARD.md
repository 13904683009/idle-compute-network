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

## 阶段 C（传输层）

| ID | 任务 | 负责人 | 状态 | 验收标准 | 证据 |
| --- | --- | --- | --- | --- | --- |
| C-1 | net_transport.py Libp2pTransport 适配器 + 双真实进程冒烟 + 导入遏制测试 | 待定 | todo | 依赖钉版核对；两个独立进程经 PeerInfo+临时端口+addr 文件交接回环互发 PING/PONG；stream.muxed_conn.peer_id 解析远程对等方；帧编解码单测无 libp2p；libp2p 仅出现在 identity.py+net_transport.py；无 asyncio | — |
| C-2 | protocol.py：信封、帧编解码、封闭类型注册表、先验签后解析管线 | 待定 | todo | 信封金标准哈希；域 'idle-compute-node-msg-v2'；seq 窗口 (last_seen, last_seen+4096]；ts ±300/600s；先验签后解析顺序可观测；65537 字节帧不分配缓冲即拒；16 个 ERROR 码；JSON 深度 17 拒 | — |
| C-3 | config.py + node.py 运行时 + HELLO/HELLO_ACK/PING/PONG + 白名单 + rendezvous 文件 | 待定 | todo | 配置校验（未知键/重复 db_path/畸形 node_id）；trio 版本启动断言；三真实进程 HELLO 互见；无 mDNS/UPnP/DHT；未列名单 HELLO→UNAUTHORIZED；PING→PONG；SIGINT 2 秒内退出码 0；坏签名 HELLO 丢弃 | — |
| C-4 | transport_tcp.py 纯标准库 TCP 回退模块（单模块应急预案） | 待定 | todo | 与 LocalLoopbackTransport 同契约测试（真实子进程回环）；零非标准库导入；compileall 通过；ARCHITECTURE.md 记录为一行开关可切换 | — |

## 阶段 D（应用协议）

| ID | 任务 | 负责人 | 状态 | 验收标准 | 证据 |
| --- | --- | --- | --- | --- | --- |
| D-1 | coordinator_server.py 线上处理器（HELLO/CLAIM/START/RENEW/SUBMIT/STATUS/LEDGER_SYNC/PING/TASK_CREATE） | 待定 | todo | 模拟层测试：TASK_CREATE 先过白名单；CLAIM 幂等（重传同 lease）；持有租约再 CLAIM→TASK_UNAVAILABLE；并发 CLAIM 恰 1 条活跃租约；START 幂等；RENEW 限帽；正确结果→RESULT_ACK VALID+内联签名记账；错输出/坏签名→REJECTED；重复提交同 hash→存回同结果；异 hash→ERROR(DUPLICATE)；过期租约→LEASE_EXPIRED+重排队语义；全部状态转移在 ALLOWED 表内 | — |
| D-2 | worker_node.py 循环 + worker_store.py 镜像（claim/start/execute/sign/submit/renew/backoff/resume） | 待定 | todo | 轮询 CLAIM(min(5s, lease/4))；以本节点密钥签 result-v2；仅接受钉定协调者 node_id 签名的 RESULT_ACK；credit 事件验签+验哈希链后才入 ledger_copy；50% 续租；协调者死亡→1..8s 退避重拨；重启续跑或过期放弃；畸形 grant 不执行；并发任务上限 1；worker_store 单测（seq 连续性/hash 链/去重/verified 标记/游标） | — |
| D-3 | cli.py client/node 命令 + 3 真实进程端到端幸福路径 | 待定 | todo | 真实 3 进程（协调者+2 工人+client 驱动）：create-task 返回 task_id；一工人执行提交；DB COMPLETED/RELEASED/恰 1 记账事件；余额恰 1 次；并发 claim 无第二活跃租约；client_ref 幂等；status 显示 COMPLETED；第二任务被空闲工人接走；杀工人→清扫重排队→幸存者完成；脚本连跑两遍干净；57 旧测试仍全过 | — |

## 阶段 E（账本同步）

| ID | 任务 | 负责人 | 状态 | 验收标准 | 证据 |
| --- | --- | --- | --- | --- | --- |
| E-1 | LEDGER_SYNC 分页拉取 + 工人链验证至字节一致镜像 | 待定 | todo | 同步后 ledger_copy 与协调者链逐列一致；150 事件→2 批（≤100）无缺无重；after_seq 超头→空页+当前头；每事件验签+prev_hash+amount>0 后才追加；坏事件不追加并报告 seq | — |
| E-2 | 工人冲突检测 + 隔离 + 删除重建恢复 | 待定 | todo | 协调者链被篡改→工人 CONFLICT 停止 claim；工人不回卷不分叉；删 worker.db→从 0 全量重建一致链；双花重放→双方恰 1 事件；hmac 行注入→CONFLICT | — |
| E-3 | docs/COORDINATION.md 诚实表 + 架构/协议/威胁模型更新 | 待定 | todo | 三类表逐字落档（协调者权威/工人可验/明确非去中心化）；"防篡改 ≠ 共识"表述；THREAT_MODEL 新增行；grep 级测试禁止文档声称共识/DHT/mDNS/UPnP | — |

## 阶段 F（安全加固）

| ID | 任务 | 负责人 | 状态 | 验收标准 | 证据 |
| --- | --- | --- | --- | --- | --- |
| F-1 | ratelimit.py + 持久化 seq 窗口 + ts 容差对抗测试 | 待定 | todo | 令牌桶数学（20/s 突发 40）；重启后旧帧重放拒绝（last_seen 持久化）；窗口内缺口接受、窗口下拒绝；±300s STALE；sender≠transport peer→BAD_SENDER（先于验签）；旧 RESULT_ACK 重放不双发 | — |
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
