# Idle Compute Network

一个**本地可测试、协调者仲裁**的闲置算力网络 MVP。目前在单机，或 coordinator 公网可达时，可以完成：
能力公告、匹配、**双方签名的任务租约**、白名单任务执行、独立结果验证与内部积分账本。

- 任务权威：协调者（coordinator）是唯一任务权威；当前**不是**去中心化调度。
- 执行授权：worker 必须持有协调者签名的 `LEASE_COMMIT` 才执行（默认 `dual_signed`）。
- 任务类型：仅 `numeric_batch_v1`（`{"numbers": [int...]}`），**没有** Shell / eval / 插件。
- 奖励：仅内部积分（CREDIT），**没有**代币、钱包、真实支付或链上交易。
- 算力兑换：创建任务时可按 `credits.task_cost` 从 requester 的可用 CREDIT 中冻结/扣费（默认 0=免费）；失败/取消/过期退回，成功消费。详见 [CREDIT-EXCHANGE.md](CREDIT-EXCHANGE.md)。

## 当前状态（诚实声明）

| 项 | 状态 |
| --- | --- |
| D1–D5 | 已完成（能力/公告/匹配器/双签租约/本地模拟注册表，已接入节点与 matcher） |
| 测试 | **479 项**标准库 unittest，全部通过（`python -m unittest discover -s tests -v`） |
| 真实双设备（D6） | **未执行**；文档与测试仅覆盖单机多进程 |
| 链上注册表 / 共识 / 代币 | **未实现**；`LocalRegistryAdapter` 只是本地 SQLite 模拟，已接入节点告示与 matcher 的 registry-first 校验，**不是**链上/共识 |
| 公网可达部署 | **支持（产品推荐）**：coordinator 监听 `0.0.0.0`/公网 IP（`allow_public_listen=true`），worker/client 主动连接公网地址；单机已用非 loopback 地址跑通全流程 |
| TCP TLS | **可选**：coordinator 配 `cert_file`/`key_file`，worker/client 用 `ca_file` 校验服务端；默认明文仅用于本地/兼容，公网部署必须开启 |
| NAT 穿透 / relay / DHT / mDNS | **未实现**；严格对称 NAT 用户暂不可用（无中继）；纯局域网仍支持 |
| 生产就绪 | **否**；未做安全审计与负载测试 |

## 环境要求

- Python **3.11+**（开发与测试环境：Python 3.12.4，Windows 11）。
- 依赖见 `requirements.txt`（`libp2p`、`trio`、`cryptography`）；完整可复现版本见 `requirements.lock.txt`。
- Linux/macOS 同样适用；命令中的 `.venv/Scripts` 换成 `.venv/bin`。

## 安装

```bash
git clone <your-fork-url> idle-compute-network
cd idle-compute-network
python -m venv .venv
# Windows:
.venv\Scripts\activate
# Linux/macOS:
source .venv/bin/activate
pip install -r requirements.txt
```

## 单机快速开始（loopback）

```bash
python -m idle_compute identity --config var/quick/coord/node.json   # 记录输出的 node_id
python -m idle_compute identity --config var/quick/worker/node.json
python -m idle_compute identity --config var/quick/client/node.json
```

把各自的 node_id 填入配置（`peers` 列表；worker/client 还需要 `coordinator_node_id`）：

```json
{
  "role": "coordinator",
  "transport": "tcp",
  "listen_host": "127.0.0.1",
  "listen_port": 17700,
  "data_dir": "var/quick/coord",
  "peers": ["<worker-id>", "<client-id>"],
  "poll_interval_seconds": 1,
  "log_level": "INFO",
  "log_file": "var/quick/coord/coordinator.log"
}
```

```json
{
  "role": "worker",
  "transport": "tcp",
  "listen_host": "127.0.0.1",
  "listen_port": 0,
  "data_dir": "var/quick/worker",
  "peers": ["<coordinator-id>"],
  "coordinator_node_id": "<coordinator-id>",
  "coordinator_addr": "127.0.0.1:17700",
  "poll_interval_seconds": 1,
  "log_level": "INFO",
  "log_file": "var/quick/worker/worker.log"
}
```

```json
{
  "role": "client",
  "transport": "tcp",
  "listen_host": "127.0.0.1",
  "listen_port": 0,
  "data_dir": "var/quick/client",
  "peers": ["<coordinator-id>"],
  "coordinator_node_id": "<coordinator-id>",
  "coordinator_addr": "127.0.0.1:17700",
  "log_level": "INFO"
}
```

启动（三个终端）：

```bash
python -m idle_compute node run --config var/quick/coord/node.json
python -m idle_compute node run --config var/quick/worker/node.json
python -m idle_compute node create-task --config var/quick/client/node.json --numbers "1,2,3"
python -m idle_compute node status --config var/quick/client/node.json --task-id <task_id>
```

成功时 `node status` 返回 `"status": "COMPLETED"`、`"validation": "VALID"`、`"credits_count": 1`。
`peers add` 可辅助维护 peers/directory（`python -m idle_compute peers add <node_id> --role worker --config ...`）。

## 公网部署（产品推荐模式）

Worker/Client 只要能上网即可主动连接 Coordinator，不需要与 Coordinator 同一局域网。
Coordinator 需要有公网可达地址：云主机公网 IP，或路由器端口转发到内网机器。

Coordinator 配置（`var/prod/coord/node.json`）：

```json
{
  "role": "coordinator",
  "transport": "tcp",
  "listen_host": "0.0.0.0",
  "listen_port": 17700,
  "allow_public_listen": true,
  "data_dir": "var/prod/coord",
  "peers": ["<worker-node-id>", "<client-node-id>"],
  "credits": {"task_cost": 0}
}
```

Worker/Client 配置把 `coordinator_addr` 指向 Coordinator 的公网地址：

```json
{"coordinator_addr": "<PUBLIC_IP_OR_DOMAIN>:17700"}
```

要点：

- `allow_public_listen: true` 是显式授权；监听非 loopback 地址必须设置它。
- 防火墙/安全组只放行 `<PORT>/TCP`；peer 白名单仍需提前交换 node_id，未列入的 HELLO 会被拒绝。
- **公网部署必须开启 TLS**（默认明文仅为本地/向后兼容）。配置见下方“公网 TLS”；也可在外层使用 VPN/隧道/TLS 反向代理。
- **纯局域网模式仍支持**（`listen_host` 填私网 IP，`allow_public_listen` 按需），但**不是产品默认**。
- **中继/NAT 穿透尚未实现**：严格对称 NAT、无法做端口映射的用户暂不可用。
- 尚未完成真实跨公网双设备验证；不要把单机非 loopback 演示当作跨设备结果（见 `D6-DESIGN.md`）。

### 公网 TLS（公网部署必须开启）

Coordinator（服务端证书/私钥）：

```json
"tls": {
  "cert_file": "certs/server.crt",
  "key_file": "certs/server.key"
}
```

Worker/Client（校验服务端证书；`server_hostname` 必须匹配证书 SAN）：

```json
"tls": {
  "ca_file": "certs/server.crt",
  "server_hostname": "coordinator.example.com"
}
```

自签证书示例（把 DNS/IP 换成你的真实地址；IP 必须写入 SAN）：

```bash
mkdir -p certs
openssl req -x509 -newkey rsa:2048 -nodes -days 365   -keyout certs/server.key -out certs/server.crt   -subj "/CN=coordinator.example.com"   -addext "subjectAltName=DNS:coordinator.example.com,IP:203.0.113.10"
```

- 双方都必须配置 `tls`；一方 TLS、另一方明文会握手失败并被拒绝。
- 客户端始终验证证书与主机名（`CERT_REQUIRED` + `check_hostname`）。
- 不配置 `tls` 时保持明文，仅用于单机/局域网兼容；**公网不得明文暴露**。
- TLS 只保护传输层；节点身份仍由现有 Ed25519 双签/租约机制保证。

## 两台设备 / 团队模拟

邀请码接入：Coordinator 运行 `python -m idle_compute invite create ...`，合作伙伴运行 `python -m idle_compute join --invite <code>`（详见指南）。\n\n链上注册表原型（Anvil 本地链，非生产）见 `contracts/README.md`。

远程合作伙伴从零接入（证书、三端配置、免费/付费、排查）见 **[REMOTE-PARTNER-GUIDE.md](REMOTE-PARTNER-GUIDE.md)**。

见 **[TEAM-SIMULATION-GUIDE.md](TEAM-SIMULATION-GUIDE.md)**：公网/局域网地址、防火墙、启动顺序、
证据采集表、PASS/FAIL 判据、清理与常见故障。**当前尚未在两台真实设备上执行**，
指南中的跨设备结果必须由团队实测填写，不能引用 loopback 结果。

## CREDIT 兑换算力

查看余额：`python -m idle_compute credit --db var/idle.db [--peer <node_id>]`，输出
`earned/held/spent/refunded/available`。

`credits.task_cost`（默认 0）> 0 时，`TASK_CREATE` 会先检查 requester 的可用余额，不足返回 `INSUFFICIENT_CREDIT`（wire code 17）；任务创建与冻结在同一事务内，成功 `SPENT`、失败/取消/过期 `REFUNDED`。语义与限制见 [CREDIT-EXCHANGE.md](CREDIT-EXCHANGE.md)。

## 测试

```bash
python -m unittest discover -s tests -v
python -m compileall -q idle_compute tests
```

测试不访问网络（节点测试使用 127.0.0.1）、不使用外部服务、不需要任何凭据。

## 安全模型（摘要）

- 任务类型是封闭白名单；payload 仅接受整数列表，无任意代码执行、无子进程、无文件路径参数。
- 网络节点使用 Ed25519 节点身份；信封、结果、能力公告、租约均为签名对象。
- **双签租约**：requester 签条款，worker 签接受，协调者签 `LEASE_COMMIT`；worker 只在
  验证 commit 后执行。`lease.mode` 只接受 `dual_signed`。
- recipient 定向校验；能力/租约广播有明确限制；未知/非法消息 fail-closed。
- 结果由协调者独立重算；同一任务只产生一条 CREDIT。
- 能力与硬件信息是**节点自述**，不是可信证明；注册表是本地模拟（非链上/共识），只用于 matcher 的 registry-first 校验。
- 详见 [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md)、[TASK-LEASE-PROTOCOL.md](TASK-LEASE-PROTOCOL.md)。

## 目录

- `idle_compute/`：协议、节点、租约、能力、发现、匹配器、注册表、账本。
- `tests/`：479 项 unittest。
- `docs/`：架构、协议、威胁模型、阶段报告、阶段 2 文档。
- `benchmarks/transport_bench.py`：loopback 传输基准（非跨设备）。

## 许可

本项目采用 **MIT License**，见 [LICENSE](LICENSE)。
