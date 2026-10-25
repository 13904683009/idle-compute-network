# 双设备真实通信验证步骤（状态：待完成）

**重要声明**：本阶段验收只完成了**单机多进程**（独立进程、独立端口、独立 DB/密钥，
真实 TCP/libp2p 回环）。以下步骤是跨设备验证的操作手册，**尚未在两台设备上执行**；
执行完成前，不得宣称"真实多设备验收已完成"。步骤基于提交 `14cc40a` 的 CLI。

## 前提

- 两台设备（A=协调者，B=工人+client），同一局域网，防火墙放行所用端口（建议 127xx 或自选）。
- 两设备代码版本一致（提交 `14cc40a`），Python 环境一致（venv 已装依赖）。
- 禁用公网：全程仅使用私有地址（192.168.x.x / 10.x.x.x），**不监听公网地址**。

## 步骤

### 0. 生成身份（两台设备各执行）

```bash
python -m idle_compute identity --config var/node/coordinator.json   # 设备 A，记录 node_id
python -m idle_compute identity --config var/node/worker.json        # 设备 B，记录 node_id
python -m idle_compute identity --config var/node/client.json        # 设备 B，记录 node_id
```

### 1. 编写配置（例）

设备 A `var/node/coordinator.json`：

```json
{
  "role": "coordinator",
  "transport": "tcp",
  "data_dir": "var/node/coordinator",
  "db_path": "var/node/coordinator.db",
  "listen_addrs": ["/ip4/0.0.0.0/tcp/17700"],
  "peers": {
    "<B的worker node_id>": {"role": "worker", "addrs": []},
    "<B的client node_id>": {"role": "client", "addrs": []}
  }
}
```

设备 B `var/node/worker.json`：

```json
{
  "role": "worker",
  "transport": "tcp",
  "data_dir": "var/node/worker",
  "listen_addrs": [],
  "rendezvous_file": "<协调者data_dir>/run/<协调者node_id>.addr",
  "peers": {"<A的coordinator node_id>": {"role": "coordinator", "addrs": []}}
}
```

设备 B `var/node/client.json`：同 worker.json，但 `role: "client"`、
`data_dir: var/node/client`。

> 说明：`rendezvous_file` 是协调者启动时写的 `<data_dir>/run/<node_id>.addr`；
> 跨设备时先用单机启动协调者拿到该文件内容（形如 `{"addrs": ["/ip4/.../tcp/17700"]}`），
> 再把地址以相同格式放到 B 设备的文件或改用共享目录。`listen_addrs: []` 表示不监听
> （worker 不需要监听时可直接省略）。

### 2. 启动协调者（设备 A）

```bash
python -m idle_compute node run --config var/node/coordinator.json
```

预期输出：绑定 17700 端口、写出 rendezvous 文件、日志不含任何密钥材料。

### 3. 启动工人（设备 B）

```bash
python -m idle_compute node run --config var/node/worker.json
```

预期输出：拨号协调者 → 双向 HELLO → PING/PONG 心跳。

### 4. 发布并观察任务（设备 B）

```bash
python -m idle_compute node create-task --config var/node/client.json --numbers 1,2,3 --lease-seconds 60
# → {"task_id": "...", ...}
python -m idle_compute node ping --config var/node/client.json
python -m idle_compute node status --config var/node/client.json --task-id <task_id>
# 预期最终：status COMPLETED、validation "VALID"、credits_count 1
```

### 5. 验证记账（设备 A 与 B）

```bash
python -m idle_compute balances --db var/node/coordinator.db     # 设备 A：余额恰 +1
# 设备 B：ledger_copy.db 中 SELECT COUNT(*), MIN(verified) FROM ledger_copy;  → 1 行且 verified=1
```

### 6. 对抗步骤（可选，建议执行）

- 设备 B 停掉工人 → 租约到期后重新启动 → 任务被重新排队并由新租约完成（无双记账）。
- 在配置外增加第三台设备的 client 试图 HELLO → 关流（UNAUTHORIZED 语义）。
- 抓包确认全程无明文密钥/签名材料泄漏到日志。

## 预期结果与证据采集

- 每步命令输出保存到日志目录（建议 `logs/idle-compute/two-device/<步骤>.log`）。
- 验收通过标准：§4 的 status 达到 COMPLETED+VALID 且两侧账本各恰 1 条验证事件；
  §6 各对抗步骤结果符合预期；两设备均无崩溃、无密钥泄漏。
