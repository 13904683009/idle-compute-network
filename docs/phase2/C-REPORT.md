# 阶段 C 交付报告（传输层与多节点通信）

日期：2026-10-26
分支：`phase2`
提交：`269e1cf`（WAVE 0）→ `14cc40a`（WAVE 1）→ `07e7fc2`（审计修复轮 1）

> 本报告只陈述有代码/测试/运行日志佐证的事实；未经验证的功能一律标注"未实现/待完成"。

## 1. 实现概览

- **WAVE 0（269e1cf）**：线协议 `idle_compute/protocol.py`（`/idle-compute/1.0.0`，
  4 字节大端帧 + JSON 信封；14 消息类型 / 16 错误码；固定验证顺序
  BAD_SENDER → 验签 → seq 窗口 → 时间戳 → 精确键 body schema；帧 64 KiB / 体 32 KiB /
  JSON 深度 16）；`net_transport.py`（libp2p==0.8.1 trio 适配器）、
  `transport_tcp.py`（纯标准库 TCP 回退，同一 NetTransport 契约）、
  `config.py`（强校验节点配置，白名单默认拒绝）。
- **WAVE 1（14cc40a）**：`idle_compute/node.py` 单一运行时——协调者 actor
  （handler 线程只解析/认证，有界工作队列 1024，单线程 Store 循环做状态转换 +
  `expire_leases`）、工人拉取循环（dial → 双向 HELLO → PING → CLAIM → 执行 →
  SUBMIT → RESULT_ACK → `ledger_copy` 镜像验账）、client 一次性操作；
  双向 HELLO 流绑定；`cli.py` 增加 `node run / create-task / status / ping`。
- **审计修复轮 1（07e7fc2）**：对抗审计（运行 `wf_c4b211ee-5e6`，28 个代理）
  产出 24 项发现，21 项确认全部修复（见 §5），新增对抗回归测试。

## 2. 架构与协议文档

- 完整架构/协议规范：`docs/phase2/NODE-PROTOCOL.md`（角色拓扑、传输切换、
  验证管线、双向 HELLO、14 消息类型、状态机集成、持久化、限制、已知边界）。
- 双设备验证步骤：`docs/phase2/TWO-DEVICE-TEST.md`（标注待完成，见 §8）。
- 任务看板：`docs/phase2/TASK-BOARD.md`（C-1/C-2/C-3/C-4/D-1/D-2/D-3 证据已更新）。

## 3. 变更文件

| 文件 | 变更 |
| --- | --- |
| idle_compute/protocol.py | 信封/帧/管线（WAVE 0）；SeqWindow 有界驱逐 max_entries=1024 + keep 白名单回调（修复轮 1） |
| idle_compute/net_transport.py | libp2p 适配器（WAVE 0）；64 并发流上限（修复轮 1） |
| idle_compute/transport_tcp.py | TCP 回退（WAVE 0）；64 并发流上限（修复轮 1） |
| idle_compute/config.py | 强校验配置（WAVE 0）；对称默认拒绝（worker/client 的 peers 必须含协调者，修复轮 1） |
| idle_compute/node.py | 节点运行时（WAVE 1）；12 处审计修复（修复轮 1，见 §5） |
| idle_compute/db.py | 重复提交检查前移（WAVE 1）；get_result_by_lease（修复轮 1） |
| idle_compute/cli.py | node 子命令（WAVE 1） |
| tests/test_protocol.py、test_net_transport.py、test_transport_tcp.py、test_config.py、test_node.py、test_cli.py | 全量对抗/协议/配置/E2E 测试（随各波次） |
| docs/phase2/* | 本报告、NODE-PROTOCOL.md、TWO-DEVICE-TEST.md、TASK-BOARD.md |
| EXECUTION-CHECKPOINT-C.md | 检查点与回滚点 |

## 4. 测试结果（实际运行，2026-10-26，venv Python，`python -m unittest discover -s tests`）

**总计 249 项，全部通过（exit 0）；compileall 通过。**

| 模块 | 数量 | 结果 | 说明 |
| --- | --- | --- | --- |
| test_protocol | 46 | PASS | 金标准哈希、seq 窗口、ts 容差、先验签后解析、65537 字节帧、深度 17、16 错误码 |
| test_config | 41 | PASS | 未知键/类型/范围校验、公网监听授权、peers 对称默认拒绝、roundtrip |
| test_store | 30 | PASS | Phase 1 遗留，未改动 |
| test_ledger | 21 | PASS | Phase 1 遗留，未改动 |
| test_node | 20 | PASS | 真实多进程 E2E（TCP+libp2p）+ 手动敌意 peer 对抗（见下） |
| test_identity | 17 | PASS | Phase B：Ed25519 身份 |
| test_net_transport | 16 | PASS | libp2p 真实子进程冒烟 |
| test_cli | 13 | PASS | 含 Phase 1 遗留 5 项 + node 子命令 |
| test_transport_tcp | 10 | PASS | TCP 真实回环 |
| test_crypto | 9 | PASS | Phase 1 遗留 |
| test_tasks | 7 | PASS | Phase 1 遗留 |
| test_signing | 7 | PASS | Phase B |
| test_models | 6 | PASS | Phase 1 遗留 |
| test_p2p | 4 | PASS | Phase 1 遗留 |
| test_source_safety | 2 | PASS | 无 eval/exec/subprocess 扫描 |

**旧测试是否仍通过：是。** 阶段 C 之前的遗留套件共 116 项
（store 30 / ledger 21 / cli 13 / crypto 9 / tasks 7 / signing 7 / models 6 / p2p 4 /
source_safety 2 / identity 17），其中 Phase A 基线（e3e6d5a，57 项）原样未动。
全部在阶段 C 代码上通过，且**未删除、未降低任何原有安全断言**（约束要求）。

**test_node.py 关键对抗证据（全部真实 socket，多数真实子进程）：**

- 幸福路径：client 进程建任务 → worker 子进程 claim/执行/提交 → VALID + 恰 1 记账
  （TCP 与 libp2p 双传输）。
- 伪造结果（有效签名错内容）→ INVALID，无记账。
- 篡改 HELLO 签名 → BAD_SIGNATURE，流不绑定。
- 未列名单 HELLO → UNAUTHORIZED，node_state 无该 peer 行。
- 垃圾帧/超大帧 → 关流或 FRAME_TOO_LARGE，流存活测试通过。
- 重复 CLAIM_REQUEST → 同一租约，不新铸（含第二 peer 用相同 claim id 重放 → CLAIM_DENIED）。
- 同 peer 重复 SUBMIT → **原 RESULT_ACK 重放**（同 result_id、VALID、credit），
  credit_events 仍恰 1 行；异 peer 重放 → TASK_UNAVAILABLE（不泄露结果存在）。
- 放弃租约 → 过期重排队，他人接走。
- 工人重启 → seq/node_state 双侧持久化断言 + 继续接任务。
- 协调者重启 → 工人 1..8s 退避重连并完成第二任务。
- seq.json 回滚（删除）→ 协调者回 SEQ_OUT_OF_WINDOW，工人自愈跳变并完成第二任务。
- 协调者对 ERROR 永不回复（PING/PONG 证明流存活）。
- 敌意 100 字符 wire "type" → 回复（若有）符合 ERROR schema，ref_type 钳制。
- HELLO 身份不符（body.node_id=协调者 id）→ BAD_SENDER + 关流。
- 干净关闭退出码 0（SIGINT 同路径）。

## 5. 对抗审计与修复（wf_c4b211ee-5e6）

24 项发现 → **21 确认（3 高 / 8 中 / 10 低）全部修复**、2 项经代码复读证伪
（libp2p 写锁：yamux/mplex 帧原子性 + 连接级 _write_lock 已保证；SeqWindow
保存失败：10s busy 超时不可达、烧 seq 无后果）、1 项默认证伪。

关键修复（节点/传输）：

- 对称默认拒绝：worker/client 配置缺协调者 peers → 加载即报错（原会自我断流死循环）。
- SeqWindow 内存有界：max_entries 1024，溢出驱逐非白名单条目。
- seq 持久化：仅白名单 peer 落库；DB 错误仅告警，不杀处理线程。
- libp2p BAD_SENDER 实时生效（muxed-conn 身份先于验签）；TCP 保持解析后绑定（设计取舍，文档注明）。
- 解析错误回复 ref_type 钳制 + 协调者对 ERROR 永不回复。
- claim 幂等：已知 claim_request_id 永不铸新租约（INSERT OR IGNORE）。
- 重复提交幂等：同 peer 重放原 RESULT_ACK（分页导出 credit 行）；异 peer 不泄露拒绝。
- `_await` 只接受本消息的回复（in_reply_to 严格匹配）+ SEQ_OUT_OF_WINDOW 自愈。
- 未绑定流允许 ERROR 先于 HELLO（自愈消息可达等待者——修复轮 1 内发现并修复的
  回归，见测试 test_worker_seq_regression_self_heals）。
- claim 等待 2×poll+2s + 零超时晚到清空（不烧 attempt）。
- backoff 仅健康周期重置；shutdown 先 join 循环线程再关 DB。
- 双传输 64 并发流上限。

修复轮数：1（≤3 界内）。修复后全量回归 249 绿；`git log` 保留完整历史，无 amend。

## 6. 性能与安全限制（现网值）

- 帧 ≤64 KiB / 体 ≤32 KiB / JSON 深度 ≤16 / 工作队列 1024（满→OVERLOAD）/ 并发流 ≤64。
- seq 窗口 4096、内存窗口 ≤1024 条目；时间戳 ±300s（握手 ±600s）；退避 1..8s 封顶。
- 执行器仅白名单 `numeric_batch_v1`，无 eval/exec/subprocess/动态导入（源码扫描测试）。
- 监听默认仅回环或显式配置地址；公网监听需独立安全评估与授权（本阶段未做）。
- 密钥/HMAC 密钥/令牌不写日志、不进消息体、不入库；积分账本幂等（重试/重放/重复结果不重复记分）。
- 所有网络输入的状态变化经现有 Store 状态机；验证顺序先验签后解析。

## 7. 双设备真实通信验收

**未完成，如实标注。** 单机多进程（真实 socket、独立解释器进程）已验收；
跨设备步骤见 `docs/phase2/TWO-DEVICE-TEST.md`，待两台设备执行后才会更新为完成。
本报告不宣称多设备已验证。

## 8. 剩余风险（诚实声明）

1. TCP 传输接听流的对端身份为占位符，身份绑定发生在 HELLO 之后（设计取舍；
   libp2p 有传输级身份）。伪造 HELLO 的敌意方仍受白名单+验签约束。
2. seq 持久化是尽力而为：node_state 写失败会退回内存窗口（重启后重放窗口重置
   到内存态），极端磁盘故障下的重放保护弱于正常态。
3. 未实现洪泛令牌桶（F-1 计划内）：恶意白名单节点可高频发消息，靠队列/流/窗口
   上限兜底，不是速率限制。
4. 白名单信任模型：白名单内的恶意 worker 可消耗任务池（每次 attempt 需租约过期
   循环；属已知可接受面，阶段 F/G 再评估）。
5. 双设备/公网场景未验证；RENEW 续租与 LEDGER_SYNC 未实现（阶段 E/F）。
6. libp2p 0.8.1 为钉版依赖；其上游漏洞不在本阶段评估范围。

## 9. 恢复步骤（回滚点）

- 当前安全里程碑：**`07e7fc2`（249 绿）**。
- 回退 `14cc40a`（WAVE 1，244 绿）：`git checkout 14cc40a -- idle_compute tests`
  （新提交，禁止 amend/reset/rebase）。
- 更早：`50fb629`（阶段 B 完成）、`e3e6d5a`（阶段 A 基线）。
- 数据目录：删除 `<data_dir>/seq.json` 会让节点出站 seq 从 0 重启——自愈机制
  （§5）会把它拉回协调者窗口，无需人工干预；协调者 `node_state` 在
  `coordinator.db` 内，删除整个 data_dir 等同重装节点（身份密钥 `identity.key`
  也在其中，**先备份再删除**）。
- 备份 bundle 见 `docs/phase2/phase2-A-evidence.md` §5。

## 10. 后续阶段建议（E/F/G，按 TASK-BOARD）

- 先做 **E-1/E-2 账本同步与冲突检测**：镜像账本目前只随 RESULT_ACK 增补，
  工人重启/协调者换库后的全量重建还没有，E 是该系统的完整性兜底。
- 再做 **F-1 令牌桶**（限制白名单内恶意节点的高频流量）与 **F-2 运行中虐待测试**
  （现对抗测试偏静态+短时；F-2 的"虐待后仍服务"是第 3 条风险的直接闭环）。
- **F-3 线边界全套 + 扩展源码扫描**顺延执行（本阶段已覆盖大部分单点，F-3 做系统化）。
- **G-1 十场景集成套件**建议在 E/F 完成后跑两遍，作为阶段 H 独立审计的输入。
- 双设备验证（TWO-DEVICE-TEST.md）建议在 F 完成后、阶段 G 前执行，并更新 STATUS.md。
