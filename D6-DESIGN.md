# D6 设计：真实双设备验收（仅设计，未执行）

> **状态：D6 未执行。** 本文件只是验收设计与检查表，不包含任何双设备实测结果；
> 在真正完成两机测试并附证据前，不得声称“真实双设备已验证”或“D6 通过”。
> 当前已知限制：C5 只有“重启 fail-closed”最小语义测试，没有自动重连/恢复实现。

## 1. 目标与非目标

**目标**：两台物理设备（A=coordinator，B=worker/client）在局域网内完成一次可复现的
双签租约任务闭环，并留下可核对证据（发现、匹配、提议、接受、commit、执行、验证、单次 CREDIT）。

**非目标**（明确不做，也不宣称）：
- 公网 / NAT 穿透 / relay / DHT / mDNS；
- 自动断线重连或“断线可恢复”；
- 多 coordinator / 去中心化调度 / 链上注册表 / 代币支付；
- 跨设备性能基准（D5 的 loopback 数字不代表跨设备）。

## 2. 拓扑与角色

```
设备 A（LAN IP: <A_LAN_IP>）
  coordinator  node run   listen_host=<A_LAN_IP>:<PORT>  allow_public_listen=true（仅私网）
设备 B
  worker       node run   coordinator_addr=<A_LAN_IP>:<PORT>（仅出站）
  client       node create-task / node status（可与 worker 同机）
```

传输：`transport=tcp`（libp2p 传输亦可，但 D6 第一轮只验 TCP）。
防火墙：仅设备 A 的专用网络放行 `<PORT>` TCP；不开放公网、不关闭防火墙。

## 3. 前置条件（检查表）

- [ ] 两机 `git rev-parse HEAD` 相同（当前发布版本）
- [ ] 两机 Python 3.11+，`pip install -r requirements.txt` 成功
- [ ] 两机 `python -m unittest discover -s tests -q` 全绿
- [ ] 两机时钟大致同步（NTP 或手动；D4 租约有效期依赖本机时钟）
- [ ] A 的私网 IP 已知；B 能 `ping`/`Test-NetConnection <A_LAN_IP> <PORT>`
- [ ] 两边都只交换 node_id（公钥标识）；**不交换 identity.key**
- [ ] coordinator `peers` 含 worker+client id；worker/client `peers` 含 coordinator id
- [ ] coordinator 配置 `credits.task_cost` 明确（0 或 ≥1；若 ≥1，requester 需先有 CREDIT）

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
| 防火墙/NAT/路由 | **待验证**；仅支持可路由私网 |
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
