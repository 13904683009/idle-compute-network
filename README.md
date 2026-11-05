# Idle Compute Network

一个**本地可测试、协调者仲裁**的闲置算力网络 MVP。目前在单机/局域网内可以完成：
能力公告、匹配、**双方签名的任务租约**、白名单任务执行、独立结果验证与内部积分账本。

- 任务权威：协调者（coordinator）是唯一任务权威；当前**不是**去中心化调度。
- 执行授权：worker 必须持有协调者签名的 `LEASE_COMMIT` 才执行（默认 `dual_signed`）。
- 任务类型：仅 `numeric_batch_v1`（`{"numbers": [int...]}`），**没有** Shell / eval / 插件。
- 奖励：仅内部积分（CREDIT），**没有**代币、钱包、真实支付或链上交易。

## 当前状态（诚实声明）

| 项 | 状态 |
| --- | --- |
| D1–D5 | 已完成（能力/公告/匹配器/双签租约/本地模拟注册表） |
| 测试 | **479 项**标准库 unittest，全部通过（`python -m unittest discover -s tests -v`） |
| 真实双设备（D6） | **未执行**；文档与测试仅覆盖单机多进程 |
| 链上注册表 / 共识 / 代币 | **未实现**；`LocalRegistryAdapter` 只是本地 SQLite 模拟，且**尚未接入**节点/匹配器 |
| 公网 / NAT 穿透 / relay / DHT / mDNS | **未实现**；仅支持可路由的局域网直连 |
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

## 两台设备 / 团队模拟

见 **[TEAM-SIMULATION-GUIDE.md](TEAM-SIMULATION-GUIDE.md)**：局域网地址、防火墙、启动顺序、
证据采集表、PASS/FAIL 判据、清理与常见故障。**当前尚未在两台真实设备上执行**，
指南中的跨设备结果必须由团队实测填写，不能引用 loopback 结果。

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
- 能力与硬件信息是**节点自述**，不是可信证明；注册表是本地模拟，未参与调度。
- 详见 [docs/THREAT_MODEL.md](docs/THREAT_MODEL.md)、[TASK-LEASE-PROTOCOL.md](TASK-LEASE-PROTOCOL.md)。

## 目录

- `idle_compute/`：协议、节点、租约、能力、发现、匹配器、注册表、账本。
- `tests/`：479 项 unittest。
- `docs/`：架构、协议、威胁模型、阶段报告、阶段 2 文档。
- `benchmarks/transport_bench.py`：loopback 传输基准（非跨设备）。

## 许可

仓库当前**未附许可证**；公开发布前请由仓库所有者确认许可证与版权声明。
