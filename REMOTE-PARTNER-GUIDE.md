# REMOTE PARTNER GUIDE（远程合作伙伴接入）

> **现状声明：真实跨公网双设备验证尚未完成。** 本文是从零接入的操作指南，不代表 D6 已通过；
> 单机非 loopback 演示（`DEMO.md`）不能当作跨公网结果。**D6 仍为 NOT TESTED。**

本指南面向：一名远程合作伙伴（Worker/Client，能上网即可）接入一台**公网可达的 Coordinator**。

---

## 1. 当前能力与限制（先读）

| 项 | 状态 |
| --- | --- |
| Coordinator 公网可达（云主机公网 IP / 端口转发） | **支持，产品推荐** |
| Worker/Client 主动出站连接 | **支持** |
| TCP TLS（服务端证书 + 客户端校验） | **支持；公网部署必须开启** |
| 双签租约、执行门、结果验证、CREDIT 结算 | 支持 |
| 中继 / NAT 穿透 / relay / DHT / mDNS | **未实现** |
| 严格对称 NAT（无法端口映射） | **暂不可用**（无中继） |
| 真实跨公网双设备验证 | **尚未完成** |
| 纯局域网模式 | 仍支持，但不是产品默认 |

安全底线：公网部署必须 TLS；只开放测试端口；不共享 `identity.key`/`ledger.key`；只跑白名单
`numeric_batch_v1`；不涉及真实资金/代币/链。

---

## 2. 准备环境

Coordinator（A）需要公网可达；Worker/Client（B）只需能上网。两边都：

```bash
python --version          # 3.11+
git clone <repo>
cd idle-compute-network
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m unittest discover -s tests -q   # 可选，确认环境
```

防火墙 / 云安全组：只放行 Coordinator 的 TCP 端口（示例 `17700`）。B 不需要入站规则。

---

## 3. 复制模板并生成身份

```bash
mkdir -p var/remote/{coordinator,worker,client}
cp examples/public-coordinator/node.json var/remote/coordinator/node.json
cp examples/public-worker/node.json      var/remote/worker/node.json
cp examples/public-client/node.json      var/remote/client/node.json
```

三个模板里的 `<...>` 是占位符，先不要改；`identity` 命令只读取 `data_dir` 生成/加载身份：

```bash
python -m idle_compute identity --config var/remote/coordinator/node.json
python -m idle_compute identity --config var/remote/worker/node.json
python -m idle_compute identity --config var/remote/client/node.json
```

记录三个 **node_id**（公开标识，可安全交换）。`identity.key` 留在各自 `data_dir`，**绝不外发**。

---

## 4. 生成 TLS 证书（公网必须）

在 Coordinator 上生成自签证书（把域名/IP 换为真实值；**IP 必须写进 SAN**）：

```bash
mkdir -p certs
openssl req -x509 -newkey rsa:2048 -nodes -days 365 \
  -keyout certs/server.key -out certs/server.crt \
  -subj "/CN=<PUBLIC_HOST>" \
  -addext "subjectAltName=DNS:<PUBLIC_HOST>,IP:<PUBLIC_IP>"
```

### 4a. 用脚本生成（推荐，支持域名 + IP SAN）

```bash
python scripts/setup_public_certs.py \
  --host <PUBLIC_HOST> --ip <PUBLIC_IP> --out-dir certs
```

- `--host` 可填域名或 IP；`--ip` 可重复，追加额外 IP SAN；
- `--json` 输出机器可读结果；`--force` 覆盖已有文件；`--days` 设置有效期（1..3650）；
- 文件不存在交互终端时也可用 `--interactive` 按提示输入；
- 输出会直接打印三端可粘贴的配置：

```text
Coordinator config:
  {"cert_file": "/repo/certs/server.crt", "key_file": "/repo/certs/server.key"}
Worker/Client config:
  {"ca_file": "/repo/certs/server.crt", "server_hostname": "<PUBLIC_HOST>"}
```

自签场景下 `server.crt` 同时作为客户端的 `ca_file`；`server_hostname` 必须与 SAN
中的域名/IP 一致。脚本只生成文件，不做任何网络或跨公网验证。

把 `certs/server.crt` 通过安全渠道发给 Worker/Client 作为 `ca_file`（自签场景）。检查 SAN：

```bash
openssl x509 -in certs/server.crt -noout -text | grep -A1 "Subject Alternative Name"
```

Coordinator 用 `cert_file` + `key_file`；Worker/Client 用 `ca_file` + `server_hostname`
（必须匹配 SAN 中的 DNS 名/IP）。**TLS 是公网部署的硬性要求**；不配置 `tls` 仅适用于单机/局域网。

---

## 5. 配置三端

编辑三份配置，替换全部 `<...>`：

### Coordinator（A）

```json
{
  "role": "coordinator",
  "transport": "tcp",
  "listen_host": "0.0.0.0",
  "listen_port": 17700,
  "allow_public_listen": true,
  "data_dir": "var/remote/coordinator",
  "peers": ["<WORKER_NODE_ID>", "<CLIENT_NODE_ID>"],
  "credits": {"task_cost": 0},
  "tls": {"cert_file": "certs/server.crt", "key_file": "certs/server.key"},
  "poll_interval_seconds": 1,
  "log_level": "INFO",
  "log_file": "var/remote/coordinator/coordinator.log"
}
```

### Worker（B）

```json
{
  "role": "worker",
  "transport": "tcp",
  "listen_host": "127.0.0.1",
  "listen_port": 0,
  "data_dir": "var/remote/worker",
  "peers": ["<COORDINATOR_NODE_ID>"],
  "coordinator_node_id": "<COORDINATOR_NODE_ID>",
  "coordinator_addr": "<PUBLIC_HOST>:17700",
  "capability": {
    "capability_version": 1,
    "ttl_seconds": 3600,
    "supported_task_types": ["numeric_batch_v1"],
    "supported_runtimes": ["python_sandbox_v1"],
    "hardware": {"cpu_arch": "x86_64", "cpu_cores": 8, "ram_mb": 16384},
    "resource_limits": {
      "max_cpu_cores": 4,
      "max_ram_mb": 8192,
      "max_gpu_memory_mb": 0,
      "max_task_duration_seconds": 600
    },
    "network": {"supported_transports": ["tcp"], "supports_relay": false},
    "verification": {"level": "SELF_DECLARED", "benchmark_profile": null},
    "available_resources": {"cpu_cores": 4, "ram_mb": 8192, "gpu_memory_mb": 0},
    "accepting_tasks": true
  },
  "tls": {"ca_file": "certs/server.crt", "server_hostname": "<PUBLIC_HOST>"},
  "poll_interval_seconds": 1,
  "log_level": "INFO",
  "log_file": "var/remote/worker/worker.log"
}
```

### Client（B，可与 Worker 同机）

```json
{
  "role": "client",
  "transport": "tcp",
  "listen_host": "127.0.0.1",
  "listen_port": 0,
  "data_dir": "var/remote/client",
  "peers": ["<COORDINATOR_NODE_ID>"],
  "coordinator_node_id": "<COORDINATOR_NODE_ID>",
  "coordinator_addr": "<PUBLIC_HOST>:17700",
  "tls": {"ca_file": "certs/server.crt", "server_hostname": "<PUBLIC_HOST>"},
  "log_level": "INFO"
}
```

要点：
- Coordinator `peers` 必须包含 Worker 和 Client 的 node_id；Worker/Client `peers` 必须包含
  Coordinator node_id。未列入的 HELLO 会被拒绝。
- `allow_public_listen: true` 是监听非 loopback 地址的显式授权。
- TLS 三端必须一致：一方 TLS、一方明文会握手失败。

---

## 6. 启动（A 先，B 后）

Coordinator（A）：

```bash
python -m idle_compute node run --config var/remote/coordinator/node.json
```

B 先验证可达，再启动 Worker：

```bash
# Windows: Test-NetConnection <PUBLIC_HOST> -Port 17700
nc -vz <PUBLIC_HOST> 17700
python -m idle_compute node run --config var/remote/worker/node.json
```

Worker 日志出现 `connected to coordinator` 即接入成功；Coordinator 日志应能收到 HELLO。

---

## 7. 免费路径（`credits.task_cost=0`）

B 上：

```bash
python -m idle_compute node create-task --config var/remote/client/node.json --numbers 1,2,3
python -m idle_compute node status --config var/remote/client/node.json --task-id <task_id>
```

`node status` 返回 `"status": "COMPLETED"`、`"validation": "VALID"`、`"credits_count": 1` 即成功。
Coordinator 上查看余额：

```bash
python -m idle_compute credit --db var/remote/coordinator/coordinator.db --peer <WORKER_NODE_ID>
```

免费任务不产生 hold：worker `earned=1, held=0, spent=0, available=1`。

---

## 8. 付费路径（`credits.task_cost>=1`）

付费前提：**requester 的 available ≥ task_cost**。新身份初始为 0，因此必须先通过完成免费任务
赚到 CREDIT，或复用已赚过 CREDIT 的 Worker 身份作为 Client。两种方式：

### 方式 A：单机快速演示（非跨设备）

```bash
python scripts/demo_network_loopback.py --dir var/demo-network --reset
```

脚本演示免费→付费（held→spent）→余额不足拒绝；**这不是跨公网验证**。

### 方式 B：远程付费（复用已赚 CREDIT 的 Worker 身份）

1. 先在 Coordinator 配置 `"credits": {"task_cost": 0}` 跑一次免费任务，让 Worker 身份赚 1 CREDIT；
2. 停止 Worker 进程；把 Worker 的 `data_dir` 复用为 Client（同一 identity/seq，避免重放问题）：

```json
{
  "role": "client",
  "data_dir": "var/remote/worker",
  "coordinator_addr": "<PUBLIC_HOST>:17700",
  "tls": {"ca_file": "certs/server.crt", "server_hostname": "<PUBLIC_HOST>"}
}
```

3. Coordinator 改 `"task_cost": 1` 并**重启**（同一 DB）；
4. 启动一个新的 Worker（不同 identity）负责执行；
5. 用上面的 Client 创建任务并观察积分：

```bash
python -m idle_compute node create-task --config var/remote/worker-client.json --numbers 4,5
python -m idle_compute credit --db var/remote/coordinator/coordinator.db --peer <WORKER_NODE_ID>
```

预期：创建后 `held=1, available=0`；完成后 `held=0, spent=1, available=0`。
余额不足时 `TASK_CREATE` 返回 **`INSUFFICIENT_CREDIT`（wire code 17）**，不会创建任务。

---

## 9. 查看积分

```bash
python -m idle_compute credit --db var/remote/coordinator/coordinator.db
python -m idle_compute credit --db var/remote/coordinator/coordinator.db --peer <NODE_ID>
```

字段：`earned`（累计赚取）、`held`（冻结）、`spent`（已消费）、`refunded`（已退回）、
`available = earned - held - spent`。未知 peer 返回全 0。

---

## 10. 常见问题与排查

| 症状 | 原因 / 处理 |
| --- | --- |
| TLS 握手失败 / `CERTIFICATE_VERIFY_FAILED` | `server_hostname` 与证书 SAN 不一致；按连接地址补 DNS/IP SAN；确认 `ca_file` 是签发该证书的 CA |
| TLS 握手失败但证书正确 | 双方 TLS 配置不一致（一方明文）；公网必须双方都配置 `tls` |
| `getaddrinfo` / 连接超时 | DNS、云安全组/防火墙未放行 `<PORT>/TCP`；先在 B 上 `Test-NetConnection <PUBLIC_HOST> <PORT>` |
| `HELLO` 被拒 / `UNAUTHORIZED` | `peers` 白名单缺失：Coordinator 加 Worker/Client node_id；Worker/Client 加 Coordinator node_id |
| `allow_public_listen` 报错 | `listen_host` 非 loopback 时必须显式 `"allow_public_listen": true` |
| 租约过期 / 时间戳异常 | 两机时钟偏差过大；启用 NTP，检查 `timedatectl`/`w32tm` |
| 任务一直 QUEUED | Worker 未连上/未公告；看 `connected to coordinator`、Coordinator 日志 HD/连接记录 |
| 付费任务被拒 `INSUFFICIENT_CREDIT` | requester 的 available 不足；先赚 CREDIT 或降低 `task_cost` |
| 严格对称 NAT 无法接入 | 中继/NAT 穿透未实现，暂不可用；需要公网可达 Coordinator 或端口映射 |

时钟建议：D6 前记录两机偏差；租约/公告都依赖本机时钟。防火墙只开必要端口，不要关闭整个防火墙。

---

## 11. 清理与安全

- 停止进程；`var/` 下 DB/日志/密钥不提交、不外发；`identity.key` 永不共享。
- 公网只保留 TLS 端口；测试结束关闭安全组规则。
- 不要把单机 loopback 或本指南的示例结果当作“真实跨公网验证通过”。

## 12. 相关文档

- `README.md` — 公网部署与 TLS
- `TEAM-SIMULATION-GUIDE.md` — 双设备/公网部署细节与证据
- `DEMO.md` — 单机 loopback 演示
- `D6-DESIGN.md` — 真实双设备验收设计与证据集（**NOT TESTED**）
