# D5 计划（能力注册表 + 传输基准审计）

日期：2026-11-05　前置：`f911af3`（D4 审计，459 测试）
路线图依据：`D0-ARCHITECTURE-DECISION.md` §12「D5 注册表 | 接口 + 本地适配器 |
`registry.py` + 测试 | 匹配器不依赖链 SDK；注册/更新/撤销/版本冲突/过期/延迟语义；
明确标注模拟」。**不自行发明新目标。**

---

## 1. 范围（已知事实）

| 项 | 事实来源 |
| --- | --- |
| D5 = `CapabilityRegistry` 接口 + `LocalRegistryAdapter`（独立 SQLite） | D0 路线图第 12 节、ADR-D0-3、ADR-D0-14、ADR-D0-15 |
| 注册表只存 `manifest_hash` + 版本/有效期/撤销/状态，不存原始能力清单 | D0 §7 |
| 本地模拟 ≠ 链上注册；不引入链 SDK / 钱包 / 代币 | ADR-D0-3 |
| 新的注册表存储不得复用 `credit_events`；独立文件（默认 `<data_dir>/registry.db`） | ADR-D0-15 |
| 新模块不得 import libp2p / asyncio / socket | ADR-D0-14 + `test_source_safety` |
| 不改 `state_machine`、TaskStatus、DB v2 列、ledger/CREDIT | 约束 |
| D5 不接线新的 P2P 框架、不做 DHT/Gossip/Relay/mDNS | 约束 |
| 真实双设备（D6）不在本阶段；loopback 结果不得冒充跨设备 | 约束 |

## 2. 目标（D5 验收标准，草案）

1. `idle_compute/registry.py`：`CapabilityRegistry` 协议/接口 + `LocalRegistryAdapter`
   实现 `register / update / revoke / get / verify / registry_version / finalize`。
2. 语义：注册（幂等）、版本升级（单调）、版本回滚拒绝、同版本异哈希冲突、撤销（版本级、
   签名验证）、过期（manifest 过期/记录过期）、最终性延迟模拟（PENDING→FINAL）。
3. 持久化：独立 SQLite 文件；重启后版本号与记录保留；写入失败不返回成功。
4. 明确标注"本地模拟注册表 ≠ 链上注册"，文档与代码 docstring 均需说明。
5. 测试：新增 `tests/test_registry.py`（≥14 项），覆盖上述语义 + 重启 + 失败注入。
6. 传输基准：可重复脚本，测量 loopback 上连接建立、PING RTT、控制消息延迟、任务端到端
   延迟、消息大小、吞吐；明确环境，**不冒充跨设备**。
7. 文档：`docs/phase2/D5-REGISTRY.md` + D5 报告 + 验收矩阵；更新 `PROTOCOL`/架构文档边界。

## 3. 依赖与集成（已核对代码）

- 复用 `capability.CapabilityManifest / CapabilityRevocation`（签名、域分离、时间检查、
  版本语义 `check_manifest_update`），不新造签名规范。
- 复用 `crypto.canonical_json`、`identity.NodeIdentity`；不依赖 `db.Store`。
- 与 `matcher` 解耦：匹配器继续消费 `CapabilityManifest`/公告缓存，D5 不改变匹配输入。
- 与 `node.py`：D5 的注册表为**库级能力**，不新增线上消息；节点接线留给 D6（路线图
  D5 交付物仅接口 + 适配器 + 测试）。若审计要求节点接线，需另行授权。
- 传输层不替换：`TcpTransport`/`Libp2pTransport` 保持；基准只测量，不引入新框架。

## 4. 当前架构限制（已知事实）

- coordinator 单写者、单一任务权威；无共识、无多副本。
- 注册表是**本地模拟**：没有跨节点复制，没有链上最终性；`registry_version` 只是本地单调计数。
- 租约执行门（D4）不读取注册表；D5 不改变"双签 + commit 才执行"的边界。
- 传输：TCP 回环/局域网；libp2p 已 pin Noise/TLS；无 NAT 穿透、无 relay、无 DHT。
- 消息大小：帧 64 KiB / body 32 KiB；租约对象 ≤8 KiB；能力对象 ≤24 KiB。

## 5. 风险清单

| 风险 | 影响 | 缓解 |
| --- | --- | --- |
| 把本地注册表误称为"链上" | 安全/合规误导 | docstring + 文档 + 报告显式标注；测试名不含 chain |
| 版本回滚/撤销被绕过 | 旧能力复活 | 复用 `check_manifest_update` + 版本级撤销 + 测试 |
| 注册表与公告缓存不一致 | 匹配到已撤销能力 | D5 不接匹配路径；文档标注为 D6 集成前置 |
| 最终性延迟被误当"已确认" | 提前使用 PENDING 记录 | `verify()` 只接受 FINAL；PENDING 测试 |
| 独立 SQLite 并发写 | 锁冲突 | 单写 + `BEGIN IMMEDIATE`；线程测试 |
| 基准结果被冒充跨设备 | 结论失真 | 报告标注 loopback；不做带宽/异构结论 |
| 范围蔓延到传输重写 | 破坏 D1–D4 安全边界 | 仅测量；无证据不改传输 |

## 6. 每项的置信状态

| 项 | 状态 |
| --- | --- |
| D5 范围 = 注册表接口+本地适配器 | **已知事实**（路线图） |
| 复用 capability 签名/撤销对象可行 | **已知事实**（代码已核对） |
| 注册表不需要线上消息 | **假设**（路线图只列模块+测试；D6 再集成） |
| 独立 SQLite + 单写者足够 | **假设**（与 discovery.db 同模式，已有先例） |
| loopback 基准能定位瓶颈 | **待验证**（先测量再决定是否优化） |
| 传输需要背压/重试优化 | **待验证**（无证据前不实施） |
| 节点级注册表接线 | **待验证/超范围**（需授权） |

## 7. 执行步骤

1. 编写 `registry.py` + 测试；跑 `test_registry`。
2. 编写 `benchmarks/transport_bench.py`（loopback、可重复、输出 JSON），运行并保存日志。
3. 根据基准决定是否需要最小传输优化；**默认不改传输**（无证据）。
4. 文档 `D5-REGISTRY.md`、`D5-REPORT.md`、验收矩阵；更新协议文档边界。
5. 全量测试 + compileall + 源码/文档门禁；独立提交；停止，等待审计。

## 8. 基准设计（先于优化）

- 环境：Windows 本机、loopback 127.0.0.1、单进程内 3 个 Node（coordinator/worker/client）
  或子进程；报告明确写"非跨设备"。
- 指标：连接+HELLO 延迟、`PING/PONG` RTT、`TASK_CREATE→ACK` 控制延迟、任务端到端延迟、
  代表性消息字节数（HELLO/PING/TASK_CREATE/LEASE_*）、失败/重试计数。
- 统计：N 次取 min/median/p95（确定性阈值只用于报告，不做 sleep-based 正确性断言）。
- 结论只用于"是否需要优化"，不用于宣称性能优于其他框架。

## 9. 验收矩阵草案（详见 D5-REPORT）

| # | 项 | 预期 |
| --- | --- | --- |
| R1 | register 成功（delay=0 → FINAL） | PASS |
| R2 | 幂等重复注册 | PASS |
| R3 | 版本升级 / 回滚拒绝 / 同版本冲突 | PASS |
| R4 | 撤销（签名、版本级） | PASS |
| R5 | 过期（manifest/记录） | PASS |
| R6 | 最终性延迟 PENDING→FINAL | PASS |
| R7 | 持久化 + 重启恢复 | PASS |
| R8 | 独立文件、不写协调者库 | PASS |
| R9 | 失败注入不返回成功 | PASS |
| R10 | 源码门禁/文档门禁 | PASS |
| B1 | loopback 基准可重复、环境标注 | PASS |
| B2 | 是否需传输优化 | 待测量 |
| N1 | 节点级注册表接线 | NOT TESTED（超范围） |
| N2 | 真实双设备 | NOT TESTED（D6） |
