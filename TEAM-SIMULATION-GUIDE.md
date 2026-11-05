# 团队两台设备模拟指南（局域网）

> 目标：两位团队成员在**各自真实设备**上，用同一份源码完成一次 coordinator + worker 的
> 双签租约任务闭环，并留下可核对证据。
>
> **本指南尚未在两台真实设备上执行过**（截至提交 `9541126`，仅单机 loopback 预检通过）。
> 团队执行后请把结果填进第 7 节表格；在拿到两台设备的日志与证据前，**不得**宣称
> 跨设备验证通过，也不得把 loopback 结果当作跨设备结果。

---

## 0. 安全须知（先读）

- **只在可信局域网内测试**。coordinator 监听本机私网地址（如 `192.168.x.x` 或 `10.x.x.x`），
  **不要**绑定公网地址、不要做路由器端口映射、不要开放公网入站。
- **不要关闭防火墙**。只在本机为 coordinator 的 TCP 端口添加"仅专用/私有网络"的入站允许；
  worker/client 不需要入站规则。
- **不要**在群里发送 `identity.key`、`.venv`、`var/` 下的数据库或任何私钥；节点身份文件
  只留在各自设备本地。
- 配置里只需要交换 **node_id**（公开的 Ed25519 peer id）和 coordinator 的私网地址。
- 仅使用白名单任务 `numeric_batch_v1` 和测试数据（小整数列表）；不涉及真实资金、代币或链。
- 任何人要求"提供私钥/关闭防火墙/开放公网"都应拒绝并停止测试。

---

## 1. 两台设备的前置检查

两台设备都执行：

```bash
git clone <团队仓库地址> idle-compute-network
cd idle-compute-network
git rev-parse HEAD          # 两台设备必须输出同一个 commit
python --version            # 需要 3.11+；本仓库在 3.12.4 上验证
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -q     # 期望 OK（479 项）
```

记录（填表用）：`git rev-parse HEAD`、`python --version`、操作系统版本。

> 两台设备 **commit 必须一致**；不一致时不要继续，先对齐代码。

---

## 2. 拓扑与角色

| 设备 | 角色 | 职责 |
| --- | --- | --- |
| A | `coordinator` | 任务权威、租约仲裁、验证结果、记账；监听私网端口 |
| B | `worker` + `client` | worker 领取任务并执行；client 提交任务/查询状态 |

当前架构**不需要**第三个设备：client 可与 worker 在同一台设备（不同 `data_dir`/config）。

---

## 3. 生成身份并交换 node_id

两台设备各自执行（示例路径；`var/` 已被 `.gitignore` 忽略）：

设备 A：

```bash
python -m idle_compute identity --config var/team/coord/node.json
```

设备 B：

```bash
python -m idle_compute identity --config var/team/worker/node.json
python -m idle_compute identity --config var/team/client/node.json
```

命令只输出 `{"node_id": "12D3KooW..."}`。把 **node_id** 互相发给对方即可（这是公钥标识，
不是私钥）。私钥文件在同目录的 `identity.key`，**不得外发**。

---

## 4. 配置（设备 A 与 B）

把 `<...>` 替换为实际 node_id；把 `<A_LAN_IP>` 替换为设备 A 的私网 IPv4。

### 设备 A：`var/team/coord/node.json`

```json
{
  "role": "coordinator",
  "transport": "tcp",
  "listen_host": "<A_LAN_IP>",
  "listen_port": 17700,
  "allow_public_listen": true,
  "data_dir": "var/team/coord",
  "peers": ["<worker-node-id>", "<client-node-id>"],
  "poll_interval_seconds": 1,
  "log_level": "INFO",
  "log_file": "var/team/coord/coordinator.log"
}
```

`allow_public_listen: true` 只是因为 `listen_host` 不是回环地址（配置校验要求显式确认）；
**它不代表允许公网**——请用防火墙把 17700 限制在私有网络，且 `listen_host` 填私网 IP 而
不是 `0.0.0.0`。

### 设备 B：`var/team/worker/node.json`

```json
{
  "role": "worker",
  "transport": "tcp",
  "listen_host": "127.0.0.1",
  "listen_port": 0,
  "data_dir": "var/team/worker",
  "peers": ["<coordinator-node-id>"],
  "coordinator_node_id": "<coordinator-node-id>",
  "coordinator_addr": "<A_LAN_IP>:17700",
  "poll_interval_seconds": 1,
  "log_level": "INFO",
  "log_file": "var/team/worker/worker.log"
}
```

### 设备 B：`var/team/client/node.json`

```json
{
  "role": "client",
  "transport": "tcp",
  "listen_host": "127.0.0.1",
  "listen_port": 0,
  "data_dir": "var/team/client",
  "peers": ["<coordinator-node-id>"],
  "coordinator_node_id": "<coordinator-node-id>",
  "coordinator_addr": "<A_LAN_IP>:17700",
  "log_level": "INFO"
}
```

说明：
- coordinator 的 `peers` 必须包含 worker 与 client 的 node_id，否则 HELLO 会被拒绝。
- worker/client 的 `peers` 必须包含 coordinator，否则自己的拨号流会被对端拒绝。
- worker/client 只需出站连接，`listen_host` 保持回环即可。
- 传输固定 `tcp`（libp2p 传输亦可用，但本指南只验证 `tcp`）。

防火墙（设备 A，按需）：允许"专用"网络入站 TCP `17700`；不要对"公用"开放。
不需要关闭系统防火墙。

---

## 5. 启动顺序

1. 设备 A（先启动 coordinator）：

   ```bash
   python -m idle_compute node run --config var/team/coord/node.json
   ```

   日志出现 `node started role=coordinator`。

2. 设备 B（再启动 worker）：

   ```bash
   python -m idle_compute node run --config var/team/worker/node.json
   ```

   日志应出现 `connected to coordinator`；设备 A 的日志可看到 HELLO 相关记录（DEBUG 级别更完整）。

3. 设备 B（第三个终端，提交任务）：

   ```bash
   python -m idle_compute node create-task --config var/team/client/node.json --numbers "1,2,3"
   ```

   输出包含 `task_id`。

4. 设备 B 查询：

   ```bash
   python -m idle_compute node status --config var/team/client/node.json --task-id <task_id>
   ```

   期望：`"status": "COMPLETED"`、`"validation": "VALID"`、`"credits_count": 1`。

---

## 6. 证据采集（两台设备都做）

请只用 UTC 或明确标注时区；不要采集与测试无关的个人数据。

| 证据 | 采集命令（在仓库根目录） | 记录到表 |
| --- | --- | --- |
| 源码版本 | `git rev-parse HEAD` | commit |
| 环境 | `python --version`；系统版本 | OS / Python |
| 设备角色 | — | A=coordinator, B=worker/client |
| node_id | 见第 3 节输出 | 三个 id |
| 配置摘要 | 打开 JSON，**只抄 role/listen_host/listen_port/peers 数量/coordinator_addr** | 摘要 |
| 连接日志 | `Get-Content var/team/worker/worker.log -Tail 20`（或 `tail -n 20`） | 含 `connected`、UTC 时间 |
| 任务提交 | `create-task` 输出 | task_id、UTC 时间 |
| 任务状态 | `node status ...` 的 JSON | COMPLETED/VALID/credits_count |
| 租约/terms | 设备 A 执行下面的 SQL 查询 | lease_id、terms_hash、worker_id、state |
| CREDIT 数量 | 同一 SQL 的第二条查询 | 必须为 1 |
| 断线/重试 | 两侧日志中 `failed`/`retry`/`backoff` 关键字 | 次数与时间 |

设备 A（coordinator 本机）用 Python 标准库查询（无需安装 sqlite3 CLI）：

```bash
python -c "import sqlite3,json;c=sqlite3.connect('var/team/coord/coordinator.db');print(json.dumps(c.execute(\"SELECT lease_id,task_id,requester_id,worker_id,state,terms_hash FROM lease_negotiations\").fetchall(),indent=2));print(c.execute(\"SELECT COUNT(*) FROM credit_events\").fetchone()[0])"
```

`state` 在完成后期望为 `COMPLETED`；`credit_events` 计数在同一任务上必须为 **1**。

---

## 7. 团队模拟结果表（执行后填写）

| # | 判据 | 证据来源 | 结果（PASS/FAIL/PARTIAL） | 备注 |
| --- | --- | --- | --- | --- |
| 1 | 两台设备 commit/环境一致 | 第 1 节输出 |  |  |
| 2 | 两机实际 TCP 通信（非 loopback） | worker 日志 `connected` + A 的私网 IP |  |  |
| 3 | worker 收到 coordinator 的 LEASE_PROPOSE | 设备 B 日志/DB 协商记录 |  |  |
| 4 | 双签租约生效（ACCEPTED→ACTIVE→COMPLETED） | 第 6 节 SQL + 日志 |  |  |
| 5 | worker 执行白名单任务 `numeric_batch_v1` | task/status 输出 |  |  |
| 6 | 结果被独立验证（`validation=VALID`） | `node status` |  |  |
| 7 | 同一任务仅 1 条 CREDIT | SQL 计数 |  |  |
| 8 | 无 commit / 过期 / 取消的租约不能执行 | 见第 9 节"未覆盖项" |  |  |
| 9 | 断线/重试有记录且不影响正确性 | 两侧日志 |  |  |

**只有当第 2–7 项都有两台设备上的证据（含时间戳与 node_id）时，才能把本次模拟标为 PASS。**
任一缺证据 → PARTIAL/NOT TESTED。

---

## 8. 停止与清理

1. 两个终端各按 `Ctrl+C` 停止 `node run`。
2. 保留 `var/team/*/*.log` 作为证据（不要提交到 git；`var/` 已被忽略）。
3. 清理（可选，确认日志已归档后）：

   ```bash
   rm -rf var/team          # Windows: Remove-Item -Recurse -Force var\team
   ```

4. 不要把 `identity.key`、`*.db`、日志或 `.venv` 提交/发给他人。

---

## 9. 常见故障

| 现象 | 原因/处理 |
| --- | --- |
| `listen_host ... is not loopback: ... set allow_public_listen=true` | 设备 A 的 `listen_host` 是私网地址；确认是私网 IP 并加 `allow_public_listen: true`（勿开公网） |
| `coordinator_node_id must be listed in peers` | worker/client 的 `peers` 缺少 coordinator id |
| HELLO 被拒 / `BAD_SENDER` / `UNAUTHORIZED` | 双方 node_id 或 `peers` 填错；确认交换的是对方 `identity` 输出的 id |
| 连接超时/拒绝 | A 的 IP/端口不对，或防火墙未放行 17700（仅私有网络） |
| worker 不领取任务 | 检查 worker 是否连上；上一个 lease 可能未过期（`lease_seconds` 默认 30 s + 重试） |
| `lease expired` / 结果 INVALID | 任务在租约内未完成；增大 `--lease-seconds`（如 60）后重试 |
| 一台机器上跑两个角色 | 每个角色用独立 `data_dir`；不要共用同一个 DB |

---

## 10. 本指南**不**覆盖的内容

- 公网、NAT 穿透、relay、DHT/mDNS（均未实现）。
- 真实断线重连/进程重启恢复的自动验证（D4 仍有 NOT TESTED 项，见 `D4-ACCEPTANCE-MATRIX.md`）。
- 能力公告/匹配器参与调度、D5 注册表接入（**注册表尚未接线**，不得声称参与调度）。
- 性能/容量结论（本指南不测吞吐；不要用 loopback 基准代表跨设备性能）。
- 安全审计结论（本仓库未做生产级安全审计）。

任何"跨设备验证通过"的结论，必须附两台设备的第 7 节表格与第 6 节证据；否则只能标注
**未验证 / PARTIAL**。
