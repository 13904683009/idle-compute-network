# 单机 Loopback 演示（CREDIT 兑换算力）

> **范围**：单机、loopback/库级演示；**不启动跨设备网络**，不是 D6，不证明“真实双设备已验证”。
> 核心语义未为本演示做任何修改。

## 0. 前置

```bash
python --version          # 3.11+
python -m unittest discover -s tests -q   # 可选：确认环境
```

仓库根目录执行；Windows PowerShell / macOS / Linux 均可（命令中的路径用 `/`）。

## 1. 一键演示（推荐）

```bash
python scripts/demo_loopback.py --db var/demo/idle.db --reset
```

脚本依次展示：

1. **免费路径**：`task_cost=0`，创建并完成一个任务，`demo-worker` 获得 1 CREDIT；
2. **付费路径**：读取 coordinator 配置 `credits.task_cost=1`；`demo-buyer` 先赚 1 CREDIT，
   创建付费任务后余额变为 `held=1, available=0`；完成后 `held=0, spent=1`；
3. **余额不足**：同一配置下 buyer 可用 0，创建被拒，打印 `INSUFFICIENT_CREDIT`；
4. **取消退款**：再赚 1 后创建付费任务并取消 → `refunded=1, available=1`。

每次状态变化后都会调用真实 CLI：

```bash
python -m idle_compute credit --db var/demo/idle.db --peer <peer>
```

输出示例（节选）：

```json
{"credit": {"demo-buyer": {"earned": 1, "held": 1, "spent": 0, "refunded": 0, "available": 0}}}
{"credit": {"demo-buyer": {"earned": 1, "held": 0, "spent": 1, "refunded": 0, "available": 0}}}
{"credit": {"demo-buyer": {"earned": 2, "held": 0, "spent": 1, "refunded": 1, "available": 1}}}
```

## 1a. 真实节点 loopback 网络演示（三进程）

```bash
python scripts/demo_network_loopback.py --dir var/demo-network --reset
# 模拟公网部署形态：coordinator 绑 0.0.0.0，worker/client 拨非 loopback 本机地址
python scripts/demo_network_loopback.py --dir var/demo-network --reset --host <LAN_IP>
```

脚本在**同一台机器**启动真实进程并走完整 wire 路径；默认 127.0.0.1，`--host <LAN_IP>`
会以 `0.0.0.0` 监听并用非 loopback 地址连接（模拟“公网可达 Coordinator”的部署形态）：

1. **免费路径**：coordinator（`credits.task_cost=0`）+ worker + client；
   `node create-task` → `TASK_CREATE_ACK` → claim → `LEASE_PROPOSE` → `LEASE_ACCEPT`
   → `LEASE_COMMIT` → 执行 → 验证 → 1 CREDIT；
2. **付费路径**：重启同一个 coordinator DB，`credits.task_cost=1`；client 复用免费阶段
   **已赚到 1 CREDIT 的 worker 身份**，先创建付费任务（此时 worker 尚未启动，便于观察
   `held=1`），再启动新 worker 完成该任务 → `spent=1`；
3. **余额不足**：同一 client 再创建付费任务 → `INSUFFICIENT_CREDIT`（wire code 17），
   不产生任务；
4. 每一步后调用 `python -m idle_compute credit --db var/demo-network/coord/coordinator.db --peer <worker_id>`。

关键输出示例（节选）：

```text
[1] Free path (coordinator credits.task_cost=0)
    client: TASK_CREATE -> tsk_...
    client: task tsk_... -> COMPLETED
    credit: earned=1 held=0 spent=0 available=1

[2] Paid path (same DB restarted with credits.task_cost=1)
    client: TASK_CREATE -> tsk_...
    credit: earned=1 held=1 spent=0 available=0
    coord: lease lea_... final negotiation state=COMPLETED
    credit: earned=1 held=0 spent=1 available=0

[3] Paid path: second task with 0 available credit
    rejected with INSUFFICIENT_CREDIT (wire code 17)
```

> **范围**：单机 loopback / 127.0.0.1；**非跨设备、非 D6**。它验证真实三进程 wire 路径，
> 但没有验证两台物理设备、防火墙、NAT、时钟偏差或断线恢复，不能作为 D6 证据。
> 日志在 `var/demo-network/*.log`；失败时脚本会终止所有子进程。

### 公网部署配置示意

真实公网部署只需把 Coordinator 监听改为 `0.0.0.0` 并显式授权，Worker/Client 填公网地址：

```json
// coordinator
{"listen_host": "0.0.0.0", "listen_port": 17700, "allow_public_listen": true}
// worker / client
{"coordinator_addr": "<PUBLIC_IP_OR_DOMAIN>:17700"}
```

本演示仅在本机用非 loopback 地址模拟，**不是**真实跨公网验证；NAT 穿透/relay 未实现。

## 2. 手动免费路径（只用 Phase-1 CLI）

```bash
python -m idle_compute init        --db var/demo/manual.db
python -m idle_compute create-task --db var/demo/manual.db --numbers 1,2,3
python -m idle_compute run         --db var/demo/manual.db --peer demo-worker
python -m idle_compute credit      --db var/demo/manual.db --peer demo-worker
python -m idle_compute balances    --db var/demo/manual.db
```

预期：`run` 领取/执行/验证任务并记 1 CREDIT；`credit` 显示
`earned=1, held=0, spent=0, refunded=0, available=1`（免费任务没有 hold）。

## 3. 手动付费路径

付费金额来自 coordinator 配置（网络模式）：

```json
{"credits": {"task_cost": 1}}
```

本地库级等价步骤（先让 requester 赚到 CREDIT）：

```bash
# 1) 让 buyer 作为 worker 先赚 1 CREDIT
python -m idle_compute run --db var/demo/manual.db --peer buyer

# 2) 以 buyer 为 requester 创建付费任务（cost=1，与上面配置一致）
python -c "from idle_compute.db import Store; s=Store('var/demo/manual.db','var/demo/ledger.key'); t=s.create_task_paid('numeric_batch_v1', {'numbers':[4,5]}, requester_id='buyer', cost=1); print(t.task_id); s.close()"

# 3) 此时 hold=1，available 减少
python -m idle_compute credit --db var/demo/manual.db --peer buyer

# 4) 用另一个 peer 执行该任务（把上一步 task_id 填进来）
python -m idle_compute run --db var/demo/manual.db --peer worker --task-id <task_id>

# 5) 完成后 hold 转 spent
python -m idle_compute credit --db var/demo/manual.db --peer buyer
```

预期：步骤 3 `held=1, available=0`；步骤 5 `held=0, spent=1, available=0`。
创建时余额不足会返回 `INSUFFICIENT_CREDIT`（wire code 17），且不会产生任务。

> 网络模式（coordinator/worker/client 三进程）见 `TEAM-SIMULATION-GUIDE.md`；
> 本页**不**运行双设备，也不覆盖 D6。

## 4. 清理

```bash
rm -rf var/demo          # PowerShell: Remove-Item -Recurse -Force var\demo
```

`var/` 已在 `.gitignore` 中；不要提交 DB、密钥或日志。

## 5. 边界

- 单机 loopback / 库级；**非跨设备**，不能作为 D6 证据；
- 未实现自动断线恢复；C5 只保证重启后 fail-closed；
- 默认 `task_cost=0`（免费）；付费是显式配置；
- 无链、无代币、无钱包、无真实支付。
