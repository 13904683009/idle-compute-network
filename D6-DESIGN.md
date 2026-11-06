# D6 设计：真实双设备验收（仅设计，未执行）

> **状态：D6 仍为 NOT TESTED。** 本文件、`scripts/d6_preflight.py` 与所有演示脚本都只是准备材料，
> 不包含任何双设备实测结果；
> 在真正完成两机测试并附证据前，不得声称“真实双设备已验证”或“D6 通过”。
> 当前已知限制：C5 只有“重启 fail-closed”最小语义测试，没有自动重连/恢复实现。

## 1. 目标与非目标

**目标**：两台物理设备（A=Coordinator，有公网可达地址；B=worker/client，只要能上网）
跨公网完成一次可复现的双签租约任务闭环，并留下可核对证据（发现、匹配、提议、接受、commit、
执行、验证、单次 CREDIT）。**本文件不代表已经执行过该测试。**

**非目标**（明确不做，也不宣称）：
- NAT 穿透 / relay / DHT / mDNS（**公网可达直连是支持的**，但严格对称 NAT 无中继不可用）；
- 自动断线重连或“断线可恢复”；
- 多 coordinator / 去中心化调度 / 链上注册表 / 代币支付；
- 跨设备性能基准（D5 的 loopback 数字不代表跨设备）。

## 2. 拓扑与角色

```
设备 A（公网 IP/域名 <PUBLIC_HOST>，云主机或端口转发）
  coordinator  node run   listen_host=0.0.0.0:<PORT>  allow_public_listen=true
设备 B（只要能上网，主动出站）
  worker       node run   coordinator_addr=<PUBLIC_HOST>:<PORT>
  client       node create-task / node status（可与 worker 同机）
```

传输：`transport=tcp`（libp2p 传输亦可，但 D6 第一轮只验 TCP）。
网络：安全组/防火墙只放行 `<PORT>/TCP`；优先限制来源 IP。当前 TCP 没有 TLS，
公网 D6 **必须启用 TLS**（cert/key + ca_file/主机名校验）或外层 VPN/TLS 隧道；中继/NAT 穿透未实现，严格对称 NAT 用户不可用。

## 3. 前置条件（检查表）

- [ ] 两机 `git rev-parse HEAD` 相同（当前发布版本）
- [ ] 两机 Python 3.11+，`pip install -r requirements.txt` 成功
- [ ] 两机 `python -m unittest discover -s tests -q` 全绿
- [ ] 两机时钟大致同步（NTP 或手动；D4 租约有效期依赖本机时钟）；记录跨机偏差
- [ ] A 的公网 IP/域名与端口转发已配置；B 能 `Test-NetConnection <PUBLIC_HOST> <PORT>`
- [ ] 两边都只交换 node_id（公钥标识）；**不交换 identity.key**
- [ ] coordinator `peers` 含 worker+client id；worker/client `peers` 含 coordinator id
- [ ] coordinator 配置 `credits.task_cost` 明确（0 或 ≥1；若 ≥1，requester 需先有 CREDIT）
- [ ] 公网路径已配置 TLS（cert/key；客户端 ca_file/主机名），或外层 VPN/TLS 隧道

## 4. 执行步骤（D6 时由团队执行，本轮不执行）

1. 两机生成身份：`python -m idle_compute identity --config var/team/..../node.json`
2. 交换 node_id，写配置（参见 `TEAM-SIMULATION-GUIDE.md`）。
3. A 启动 coordinator；B 启动 worker；B 用 client 提交白名单任务。
4. 观察日志与数据库：LEASE_PROPOSE/ACCEPT/COMMIT、任务 COMPLETED、单条 CREDIT。
5. 采集证据（见 §6）并填 §7 表。
6. 反向验证 fail-closed：
   - 未 commit 前 worker 不执行；
   - 取消/过期/撤销的租约不执行；
   - 错投/广播的租约消息被拒；
   - 重启 worker 后旧授权不可复用（C5 语义）。
7. 停止进程，保留日志，清理 `var/`（不提交日志/DB/密钥）。

## 5. 证据清单（每台设备）

| # | 证据 | 采集方式 |
| --- | --- | --- |
| 1 | OS / Python / commit | `python --version`；`git rev-parse HEAD` |
| 2 | node_id（三个角色） | `identity` 输出 |
| 3 | 配置摘要（脱敏） | role/listen_host/listen_port/peers 数量/coordinator_addr/credits.task_cost |
| 4 | 连接与 HELLO 日志 | worker.log / coordinator.log（DEBUG） |
| 5 | task_id / lease_id / terms_hash | client 输出 + A 的 `lease_negotiations` 查询 |
| 6 | 租约状态迁移 | DB：PROPOSED→ACCEPTED→ACTIVE→COMPLETED |
| 7 | 执行与验证 | `node status`：COMPLETED / validation=VALID |
| 8 | CREDIT 数量 | DB：该任务 `credit_events` 恰好 1 条 |
| 9 | 积分明细 | `python -m idle_compute credit --db ... --peer <requester>`（available/held/spent） |
| 10 | 失败/重试/断线 | 两侧日志中的 failed/retry/backoff，含 UTC 时间戳 |
| 11 | 反向用例证据 | 未 commit 不执行、取消/过期不执行、错投被拒、重启后旧授权不可用 |

## 6. PASS / FAIL 判据

**PASS（全部满足）**：
1. 两台真实设备（非 loopback、非同一进程/主机）实际建立 TCP 连接；
2. worker 收到 coordinator 的 LEASE_PROPOSE 并完成 ACCEPT；
3. coordinator 返回有效 LEASE_COMMIT，worker 验证后执行；
4. 白名单任务 `numeric_batch_v1` 完成，结果被独立重算验证为 VALID；
5. 同一任务恰好 1 条 CREDIT，且无重复执行/重复计分；
6. 无有效 commit 的租约从未执行；取消/过期/撤销的租约从未执行；
7. 证据包含两机 commit、node_id、时间戳（UTC）与上述 DB/日志。

任一条缺失 → **PARTIAL / NOT TESTED**，不得标 PASS。

## 7. 结果表（D6 时填写；当前为空）

| # | 项 | 结果（PASS/FAIL/PARTIAL/NOT TESTED） | 证据位置 |
| --- | --- | --- | --- |
| 1 | 两机真实通信 | NOT TESTED | — |
| 2 | LEASE_PROPOSE/ACCEPT | NOT TESTED | — |
| 3 | LEASE_COMMIT 验证后执行 | NOT TESTED | — |
| 4 | 独立验证 VALID | NOT TESTED | — |
| 5 | 恰好 1 条 CREDIT | NOT TESTED | — |
| 6 | 无 commit 不执行 | NOT TESTED | — |
| 7 | 取消/过期/撤销不执行 | NOT TESTED | — |
| 8 | 错投/广播拒绝（节点级） | NOT TESTED | — |
| 9 | worker 重启旧授权不可用 | NOT TESTED（已有库级 C5 测试，非双设备） | — |

## 8. 已知风险与假设

| 项 | 状态 |
| --- | --- |
| 时钟偏差（两机） | **待验证**；租约/公告/commit 均依赖本机时钟 |
| 防火墙/NAT/路由 | **待验证**；公网直连推荐，严格对称 NAT 无中继不可用 |
| 断线自动恢复 | **未实现**；只保证 fail-closed（C5 最小语义） |
| 注册表跨节点一致性 | **未实现**；D5 注册表是本地模拟 |
| 性能/带宽 | **未验证**；loopback 数据不外推 |
| Windows 密钥 ACL | 已知限制（0600 无效，依赖目录 ACL） |
| 双机并发/竞态 | D4 库级测试覆盖部分；真实双设备未验证 |

## 9. 与 D4/D5 的边界

- D4 的租约/执行门/结果验证是 D6 的被测对象，**不得在 D6 中修改**其语义；
- D5 注册表已接入节点与 matcher（registry-first），但仍是本地模拟；D6 若发现跨节点
  不一致，只记录，不在 D6 内改注册表设计；
- A6（本地 CLI 隔离）已由静态+语义测试覆盖；C5 只覆盖“重启 fail-closed”，
  **D6 不得把 C5 结果当作自动恢复能力**。

## 10. 单机预检（D6 前必须通过）

```bash
# 在 A、B 两台设备上分别执行，并保留 JSON 作为证据
python scripts/d6_preflight.py --json --out var/d6/preflight-<device>.json
```

- `--allow-dirty` 仅供开发自测；D6 当天不得使用（必须干净提交）。
- `--full` 额外跑完整测试套件（较慢，建议 D6 前最后一次使用）。
- 退出码：0 = 无 FAIL（WARN 可接受，但要在结果表中登记）；1 = 有 FAIL，**停止 D6**。
- 脚本**不建立任何跨设备连接**，不代替 D6。

预检共 16 项：

| # | id | 检查 | D6 前要求 |
| --- | --- | --- | --- |
| 1 | `python_version` | Python ≥ 3.11 | PASS |
| 2 | `cli_help` | `python -m idle_compute --help` | PASS |
| 3 | `cli_node_help` | `python -m idle_compute node --help` | PASS |
| 4 | `cli_credit_help` | `python -m idle_compute credit --help` | PASS |
| 5 | `git_revision` | HEAD 与工作树 | PASS（clean；记录 commit） |
| 6 | `source_doc_gates` | 源码/文档安全门禁 | PASS |
| 7 | `critical_regressions` | C5 + CREDIT + CLI 回归 | PASS |
| 8 | `db_schema` | tasks/leases/lease_negotiations/credit_holds/credit_events | PASS |
| 9 | `config_examples` | coordinator/worker/client 配置可解析 | PASS |
| 10 | `public_listen_guard` | 非 loopback 监听必须显式 opt-in | PASS |
| 11 | `capability_signing` | manifest 签名/验签 | PASS |
| 12 | `port_bind` | loopback 端口可绑定 | PASS |
| 13 | `lan_interface` | 存在非 loopback IPv4（候选 LAN 地址） | PASS 或 WARN（记录） |
| 14 | `firewall_reachability` | 防火墙/NAT/互达 | **WARN：只能在 D6 实测** |
| 15 | `clock_snapshot` | 设备 UTC/时区偏移 | PASS（记录） |
| 16 | `cross_device_clock_skew` | 两机时钟偏差 | **WARN：D6 当天实测** |

## 11. D6 通过所需的最小证据集

所有时间必须为 **UTC ISO-8601**；所有文件放在 `var/d6/evidence-<device>-<timestamp>/`
（不提交仓库）。**E1–E13 缺一不可**，缺任何一项即判 NOT PASS。

| ID | 证据 | 采集方式 | 通过条件 |
| --- | --- | --- | --- |
| E1 | 两机环境 | `git rev-parse HEAD`、`python --version`、OS 版本 | 同一 commit；Python ≥3.11；记录 OS |
| E2 | 脱敏配置快照 | 两机 node JSON（保留 role/listen/peers 数量/credits.task_cost，隐藏密钥） | coordinator 有 `allow_public_listen` 决策；peers 互相包含；付费时 task_cost 明确 |
| E3 | 预检报告 | `d6_preflight.py --json --out ...` 各一份 | 两机均 exit 0；记录 WARN 项 |
| E4 | 网络可达 | B→A `Test-NetConnection <A_IP> <PORT>`；双方 HELLO 日志 | TCP 可达；HELLO/HELLO_ACK 均有 UTC 时间戳 |
| E5 | 任务创建 | client `node create-task` 输出（task_id / client_ref） | 收到 TASK_CREATE_ACK；付费时 requester 有足够 available |
| E6 | 租约状态机 | A 上 `SELECT lease_id, state, updated_at FROM lease_negotiations WHERE task_id=?` | 出现 `PROPOSED → ACCEPTED → ACTIVE → COMPLETED`，lease_id/terms_hash 与日志一致 |
| E7 | commit 与执行 | worker/coordinator 日志（LEASE_PROPOSE/ACCEPT/COMMIT），worker 执行起止 UTC | 只有收到有效 LEASE_COMMIT 后才执行；日志时间顺序一致 |
| E8 | 结果验证 | A 上 `SELECT status FROM results WHERE task_id=?`；validation 行 | `VALIDATED`/`VALID`，expected_hash 与重算一致 |
| E9 | CREDIT 结算 | A 上 `SELECT COUNT(*) FROM credit_events WHERE task_id=?`；`credit --peer <requester>` 前后输出 | 该任务恰好 1 条 CREDIT；requester `held` 1→0、`spent` 0→1；worker earned +1 |
| E10 | 时钟偏差 | 两机 D6 前后各记录一次 UTC 与本地偏移；可用 NTP/`w32tm` 参考 | 记录 A/B 偏差；建议 |skew| ≤ 2 s，且必须小于租约窗口的安全余量 |
| E11 | 负向 fail-closed | 无 commit、取消/过期、错投/广播、worker 重启各一次 | 四类场景均未执行；有日志/DB 证据；不把 C5 当自动恢复 |
| E12 | TLS（公网必填） | 证书 SAN、双方 `tls` 配置、握手成功日志；负向：错误 CA/主机名被拒 | 公网部署必须 TLS；单机 loopback 可明文 |
| E13 | 清理与保密 | 证据目录清单；确认无 identity.key/ledger.key/私钥/DB 入库 | 只提交文档/脱敏日志；`PROJECT-STATUS-AND-HANDOFF.md` 不入库 |

### 11.1 建议的最小 SQL

```sql
SELECT lease_id, state, updated_at FROM lease_negotiations WHERE task_id = :task;
SELECT status, content_hash FROM results WHERE task_id = :task;
SELECT COUNT(*), MIN(created_at), MAX(created_at) FROM credit_events WHERE task_id = :task;
```

### 11.2 时钟偏差记录格式

```json
{"device": "A", "utc": "2026-11-06T03:00:00Z", "local_offset": "+08:00",
 "reference_utc": "2026-11-06T03:00:01Z", "skew_ms": 1000, "measured_before": true, "measured_after": true}
```

## 12. D6 当天执行顺序（仅清单，本轮未执行）

1. 两机 `git rev-parse HEAD` 相同、工作树 clean；
2. 两机各跑 `d6_preflight.py`，保存 E1/E3，登记 WARN；
3. 记录两机 D6 前时钟偏差（E10-before）；
4. A 启动 coordinator（显式 LAN 绑定/防火墙只放行测试网段），B 启动 worker；
5. B 跑免费路径 smoke（cost=0），保存 E4–E9；
6. 重启 A 的 coordinator 为 `task_cost≥1`（或使用独立付费 DB），
   requester 必须是已赚到 CREDIT 的 B 侧身份；保存付费路径 E5–E9；
7. 依次执行 E11 负向用例；
8. 记录 D6 后时钟偏差（E10-after）；
9. 停止进程、保存 E12、填写 §7 结果表；
10. 任何 FAIL/缺少证据 → 判 NOT PASS，停止并记录，不在 D6 当场改代码。

## 13. 判停与诚实性

- 本文件所有结果行仍为 **NOT TESTED**；`d6_preflight.py` 通过 ≠ D6 通过；
- 预检 WARN 项（防火墙/跨机时钟）必须由 D6 实测填补；
- 单机 loopback demo（`DEMO.md`）**不是** D6 证据；
- 未执行 D6 前，任何“真实双设备已验证”的表述都禁止。
