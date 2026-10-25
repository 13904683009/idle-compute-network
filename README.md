# Idle Compute Network MVP

本地、单机、可测试的闲置算力网络第一阶段原型。它只做**任务协议、数据模型、
状态机、结果验证和内部贡献账本**；不伪造 P2P 多设备能力，不执行任意代码，
不发行代币，不涉及真实资金。

## 已实现

- `Peer / Task / TaskShard / Lease / Result / Validation / CreditEvent` 数据模型。
- SQLite 持久化；`main` 为基线、`dev` 为开发分支。
- 有界任务状态机、UUID 任务 ID、租约过期、重试、重复领取/提交防护。
- 白名单任务 `numeric_batch_v1`：对整数列表计算 count/sum/sum_squares。
  只有这一种任务；没有 Shell、`eval`、`exec`、动态导入或任意代码执行。
- 独立结果重算校验、内容哈希、HMAC-SHA256 结果签名。
- 追加式、哈希链式、HMAC 签名的贡献账本；幂等计分，可重建 balances。
- 本地 CLI：init / create-task / claim / run / status / events / cancel /
  expire / verify-ledger / rebuild-ledger / balances。
- 57 项标准库 unittest，覆盖正常、重复、过期、错误结果、错误签名、取消、
  重启恢复、账本篡改（prev_hash、金额、中间删除、尾部删除限制、event_type、
  NULL 篡改，CLI 端到端结构化报错）、损坏 DB 文件的结构化报错与句柄释放、
  结果表 NULL 签名/哈希守卫、单活跃租约唯一索引直测（含 UNIQUE 来源断言）、
  密钥管理（幂等加载、半截密钥报错、Windows ACL 警告、创建竞争重试）和
  SUBMITTED 崩溃窗口恢复（租约过期后回队或按次数过期）。验证通过/拒绝后
  租约置为 RELEASED。
- 显式 P2P 边界：`Transport` / `PeerDirectory` 接口；`NetworkTransportNotImplemented`
  明确抛错，`LocalLoopbackTransport` 仅用于本地测试。

## 未实现（明确标记）

- 真实 libp2p / TCP / QUIC 网络层、DHT、NAT 穿透、跨设备发现。
- 真实 Peer 身份、非对称签名（当前为本地 HMAC）、密钥轮换。
- 多分片调度；`TaskShard` 目前只建模、单分片执行。
- 去中心化共识、防 Sybil/女巫、信誉、代币或真实支付。
- 生产级限流、监控、远程 API、Web UI。
- 操作系统级沙箱；当前的安全来自白名单任务和受控数据模型，不是 OS 隔离。

## 快速开始

```powershell
cd C:\Users\linxi\AI-Work\workspace\idle-compute-network

python -m idle_compute init --db var/idle.db
python -m idle_compute create-task --db var/idle.db --numbers "1,2,3,4"
# 记下输出的 task_id
python -m idle_compute run --db var/idle.db --peer local-peer-a --task-id <task_id>
python -m idle_compute status --db var/idle.db --task-id <task_id>
python -m idle_compute events --db var/idle.db
python -m idle_compute verify-ledger --db var/idle.db
python -m idle_compute rebuild-ledger --db var/idle.db
```

也可以让 `run --peer` 不带 `--task-id`，它会领取最早的排队任务。
`--db` 默认 `var/idle.db`；签名密钥默认是数据库同目录的 `ledger.key`。
`var/`、`*.db`、`*.key` 都在 `.gitignore` 中，不会提交。

## 测试

```powershell
python -m unittest discover -s tests -v
python -m compileall -q .
```

测试不使用第三方依赖，不访问网络，不读写项目外文件（临时目录除外）。

## 安全模型（摘要）

- 任务类型是封闭白名单；payload 只接受 `{"numbers": [int...]}`，长度 1..4096，
  绝对值 ≤ 1e9；布尔、浮点、字符串、额外字段均拒绝。
- 没有 Shell、`eval`、`exec`、子进程、socket、HTTP 客户端或任意文件路径参数。
- 结果由协调器独立重算；签名覆盖 task_id、lease_id、peer_id 和内容哈希。
- 重复领取由 SQLite 事务 + 单活跃租约唯一索引阻止；重复提交由唯一约束阻止；
  重复计分由账本幂等键阻止。
- HMAC 是对称本地完整性机制，不是生产级 Peer 身份；详见威胁模型。
- Windows 上 0600 权限无效：密钥创建时会显式警告，密钥机密性依赖目录
  ACL（`docs/THREAT_MODEL.md` 已记录该限制）。

完整细节：[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)、
[`docs/PROTOCOL.md`](docs/PROTOCOL.md)、[`docs/THREAT_MODEL.md`](docs/THREAT_MODEL.md)、
[`docs/ACCEPTANCE.md`](docs/ACCEPTANCE.md)、[`docs/STATUS.md`](docs/STATUS.md)。
