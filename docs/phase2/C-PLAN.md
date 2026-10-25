# 阶段 C 计划 — 传输层与多节点通信

- 日期:2026-10-26
- 依据:阶段 C 用户指令 + 仲裁计划(phase2-plan-arbiter.json)+ TASK-BOARD.md C 行
- 基线:phase2@50fb629,115 项测试全绿(2026-10-26 复跑确认,1.913s,OK)

## 1. 范围(本阶段做/不做)

**做(本阶段验收):**
- C-1 `net_transport.py`:NetTransport 抽象契约 + Libp2pTransport 薄适配器
  (libp2p==0.8.1 实测 API:`new_host(key_pair=...)`、`host.run([Multiaddr(...)])`
  异步上下文、`connect(PeerInfo)`、`new_stream(peer_id,[proto])`、
  `stream.muxed_conn.peer_id`、trio==0.30.0 必须钉版);双真实进程冒烟;导入遏制
  (libp2p 仅 identity.py + net_transport.py)
- C-2 `protocol.py`:版本化信封(wire 协议 `/idle-compute/1.0.0`)、4 字节 BE 帧、
  64KiB 帧/32KiB 体上限、JSON 深度 16、**先验签后解析**管线、封闭类型注册表、
  16 个 ERROR 码、seq 窗口 (last_seen, last_seen+4096]、ts 容差 ±300/±600s
- C-3 `config.py` + `node.py`:三真实进程 HELLO 互见;白名单默认拒绝(未列名单
  HELLO→UNAUTHORIZED);PING→PONG;SIGINT 2 秒内退出码 0;坏签名 HELLO 丢弃;
  rendezvous 文件 `var/run/<node_id>.addr`;无 mDNS/UPnP/DHT
- C-4 `transport_tcp.py`:纯标准库 TCP 回退,与 NetTransport 同契约,单模块开关切换
- 最小线上集成(用户指令第四/五步):TASK_CREATE/ACK、CLAIM_REQUEST/GRANT/DENIED、
  SUBMIT_RESULT/RESULT_ACK(内联记账)、STATUS_REQUEST/RESPONSE、ERROR、PING/PONG、
  HELLO/HELLO_ACK —— 全部经过现有 Store 状态机/白名单/验签/幂等账本,无旁路
- 多进程端到端测试:重复领取恰 1 活跃租约、重复提交不双发、伪造结果/错签名不获分、
  无效结果不进 VALIDATED、掉线重连/重启恢复、超时

**不做(阶段 D/E,文档已注明):**
- RENEW 续租处理器与工人 50% 续租循环(D-1/D-2)
- LEDGER_SYNC 线上处理器与工人全量镜像(E-1/E-2;ledger.py 的 export_since/
  verify_and_append 已在 B-3 备好)
- 工人侧持久化 seq 窗口(F-1 经 node_state 表);工人侧本阶段用内存窗口 + 发送
  seq 落 `var/<node>/seq.json`(原子写),D-2 worker_store 落地后迁表
- 客户端 CLI 完整命令集(D-3);本阶段客户端功能经 node.py 的 TASK_CREATE/STATUS 验证
- 真实多设备(LAN/公网):**本阶段不做、不声称**;文档给出两台设备测试步骤(第六步)

## 2. 以代码为准的现状盘点(2026-10-26 逐文件读证)

| 能力 | 状态 | 证据 |
| --- | --- | --- |
| Ed25519 身份(node_id=peer id、内嵌公钥验签) | **已存在** | identity.py:111 `verify_signature`;identity.py:76 `load_or_create`;52 字符 id |
| v2 信封签名载荷(域 idle-compute-node-msg-v2) | **已存在** | crypto.py:77 `node_msg_payload`;test_crypto 金标准哈希 |
| 结果签名 v2 / 记账签名 v2 策略 | **已存在** | signing.py:95 Ed25519Policy;test_store/network 路径全绿 |
| 网络模式 Store(注入策略、无 HMAC 键文件) | **已存在** | db.py:198 `Store(db_path, signer=policy)` |
| 任务白名单 + 有界状态机 + 独立重算 + 幂等账本 | **已存在** | tasks.py:63 注册表;state_machine.py ALLOWED;db.py:567 validate_result;ledger.py 幂等键 |
| 租约过期回队/SUBMITTED 崩溃窗口恢复 | **已存在** | db.py:392 `_expire_locked`;db.py:437 `expire_leases` |
| 线上信封/帧/注册表/校验管线 | **缺失** | p2p.py:48 仍是 Phase 1 显式 NotImplemented 占位 |
| 节点运行时/配置/多进程 | **缺失** | 无 node.py/config.py |
| libp2p 传输适配器 | **缺失** | 仅 libp2p-eval 冒烟证据(EVALUATION.md) |
| seq 窗口/ts 容差/限额 | **缺失(需新建)** | node_state 表已在 db.py:141(B-3)备用 |
| 发送 seq 持久化 | **缺失(需新建)** | 见"不做"说明 |

**需修改的既有文件:仅 `cli.py`**(新增 `node run/...` 子命令,不动 11 条旧命令)。
coordinator.py/worker.py/db.py/ledger.py/state_machine.py/tasks.py 本阶段**零改动**,
网络处理器直接复用其现有 API(用户指令第四步:网络输入不得绕过现有安全边界)。

## 3. 设计要点(承 D-004 仲裁决策)

- 契约先行:`NetTransport`(async 字节流 + 帧由 protocol.py 编解码)由本阶段主循环
  先写死接口,Libp2pTransport 与 TcpTransport 各自实现同一契约,单开关切换
- 信封:{v,type,sender,recipient,seq,ts,in_reply_to,body,signature};签名域
  `idle-compute-node-msg-v2`(crypto.node_msg_payload);验签公钥从 sender 的
  node_id 多哈希提取(无密钥分发)
- 校验顺序(可观测):帧→深度受限 JSON→信封形状→BAD_SENDER(sender≠传输对端)→
  **验签(先于 body 解析)**→seq 窗口→ts 容差→body 注册表校验
- 消息类型(封闭注册表,14 型):HELLO/HELLO_ACK/PING/PONG/ERROR/TASK_CREATE/
  TASK_CREATE_ACK/CLAIM_REQUEST/CLAIM_GRANT/CLAIM_DENIED/SUBMIT_RESULT/
  RESULT_ACK/STATUS_REQUEST/STATUS_RESPONSE;body 按类型精确键集校验(可空键
  也必须存在,杜绝字段走私)
- 实现注:transport_tcp.py 是唯一获准使用原始 socket 的模块——test_source_safety.py
  中新增文件级豁免(仅 `socket.`,其余禁止令牌全模块全禁)+ 豁免边界断言,
  是对原扫描的精化而非放宽
- RESULT_ACK 内联 credit 事件(export_since 行格式),工人侧经 verify_and_append
  (B-3)验签+验链后入 ledger_copy —— 复用,不新建第二套账本路径
- 白名单:config peers 静态允许名单,默认拒绝;HELLO 时双向核对 sender==body.node_id
- 监听:默认 127.0.0.1 或显式私有地址;公网地址需 config 显式
  `allow_public_listen: true`(拒绝静默默认),本阶段测试仅回环
- 限额:帧 64KiB、体 32KiB、深度 16、任务参数沿用 tasks.py 上限(4096 数/±1e9)、
  每对端并发流由 libp2p 资源管理器、重试/退避 1..8s、poll 间隔 min(5s, lease/4)

## 4. 风险(承仲裁风险登记)

| 风险 | 缓解 |
| --- | --- |
| libp2p 0.8.1 无文档,API 靠逆向 | 已逐条读 .venv 内安装源码核实签名(本计划 §1);传输层薄封装可整模块替换(C-4 回退) |
| trio 版本漂移崩溃 | 钉 trio==0.30.0;node.py 启动断言版本,不符即报错退出 |
| Windows 子进程测试慢/残留进程 | 每测用例超时+teardown 强制 terminate;端口用 /tcp/0 临时分配 |
| 工人重启后发送 seq 归零被对端窗口拒绝 | 发送 seq 原子落盘 var/<node>/seq.json;对端 seq 持久化见"不做" |
| 帧解码被超大长度前缀打爆内存 | 先读 4 字节头,超限立即拒绝并 reset,不分配缓冲(测试断言) |
| 网络输入绕过状态机/白名单 | 处理器只调 Store/Coordinator 既有 API;新增"处理器无 eval/exec/导入"断言测试 |

## 5. 验收标准(测试可测,逐条对应)

见 TASK-BOARD.md C-1..C-4 行 + 本文件 §1;全部落为自动化测试:
tests/test_protocol.py、tests/test_net_transport.py、tests/test_transport_tcp.py、
tests/test_config.py、tests/test_node.py(多进程端到端)+ 既有 115 项回归。

## 6. 执行批次

- WAVE 0(并行 Agent×3,互不触碰同一文件):C-1 net_transport.py + 测试‖
  C-2 protocol.py + 测试‖ C-4 transport_tcp.py + 测试 → 主循环审查+全量回归 → 提交
- WAVE 1(主循环串行):config.py + node.py + cli.py `node` 子命令 + 多进程 E2E
  测试(幸福路径 + 攻击路径)→ 全量回归 → 提交
- WAVE 2(审计工作流):独立审计 Agent 对抗检查实际 diff+测试证据 → ≤3 轮修复
- 收尾:docs/phase2/C-REPORT.md、docs/phase2/NODE-PROTOCOL.md、测试日志、
  EXECUTION-CHECKPOINT-C.md、记忆更新、交付报告
