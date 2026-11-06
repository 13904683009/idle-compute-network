# D2 配置迁移：`peers` 列表 + `directory`（ADR-D0-13）

## 1. 两种形状

```json
{
  "role": "coordinator",
  "transport": "tcp",
  "data_dir": "var/n1",

  "peers": ["12D3KooW…A", "12D3KooW…B"],
  "directory": {
    "12D3KooW…A": {"role": "worker", "addrs": ["/ip4/127.0.0.1/tcp/7001"]}
  }
}
```

| 字段 | 语义 | 谁说了算 |
| --- | --- | --- |
| `peers` | **allowlist**：只有列出的 node id 能完成 HELLO | 唯一信任来源（默认拒绝） |
| `directory` | 元数据：对端声明的 role 与地址 | 仅用于路由/展示，不参与信任判断 |

规则：
- `directory` 的每个 key **必须**同时出现在 `peers` 中（不允许描述一个不被允许的 peer）；
- `directory[*]` 只允许 `role`、`addrs` 两个键；role ∈ `coordinator|worker|client`；addrs 最多 8 条短字符串；
- `peers` 不允许重复；
- 两者描述的是**不同事实**（信任 vs 元数据），不是同一状态的两份副本，因此不会冲突。

## 2. 旧格式（D1 之前 `peers add` 写出的）仍可读

```json
{"peers": {"12D3KooW…A": {"role": "worker", "addrs": ["…"]}}}
```

- 加载时**自动迁移**为 `peers=[key…]` + `directory={key: {role, addrs}}`，
  role/addrs **不丢失**；
- `NodeConfig.migration_hint` 给出可操作提示，任何 `peers`/`identity` 命令
  （或下次写配置）都会把文件改写成新形状；
- 若同一文件里同时存在旧对象 `peers` 与新的 `directory`，`load_config` **拒绝**并提示
  二选一（不猜测、不静默合并）；`peers add` 是修复入口：它会把旧条目并入 `directory`
  （显式的新值优先），然后写出新形状。
- 旧的 `peers` 对象条目缺少 `role` 时拒绝加载（无法判断语义，给出字段名）。

## 3. 写出与回读

- 所有写出路径（`peers add`、任何 `NodeConfig.to_dict()`）只写新形状；
- 写出后必须能被 `load_config` 直接加载（有 round-trip 测试：
  `tests/test_peer_directory.py::test_cli_repairs_a_legacy_file_and_preserves_metadata`、
  `tests/test_cli.py::test_peers_add_output_is_loadable_by_load_config`）；
- `NodeConfig.to_dict()` 之后 `from_dict()` 必须得到相等对象
  （`test_new_shape_roundtrips_exactly`）。

## 4. 兼容性边界

- 不改动任何既有字段的语义（`transport/listen_*/poll_*/backoff_*/log_*` 等）；
- 新字段全部可选，缺省时行为与 D1 完全一致（`directory={}`、`capability=null`、
  `discovery` 取默认值）；
- 没有 remote、没有自动迁移写盘脚本：迁移只在显式运行 CLI 或写配置时发生。

## 5. 新增的可选配置段（D2-2 使用）

| 段 | 键 | 默认 | 含义 |
| --- | --- | --- | --- |
| `capability` | `capability_version` | 1 | 本节点的能力声明版本 |
| | `ttl_seconds` | 120 | 清单有效期 |
| | `supported_task_types` / `supported_runtimes` | — | 必填的任务类型/运行时声明 |
| | `hardware` / `resource_limits` / `network` / `verification` | — | 与 `CAPABILITY-PROTOCOL.md` 一致 |
| | `available_resources` | `{cpu_cores:0,ram_mb:0,gpu_memory_mb:0}` | 公告里的可用资源（自报） |
| | `accepting_tasks` | `false` | 是否接受新任务（自报） |
| `credits` | `task_cost` | 0 | 创建任务时向 requester 收取的 CREDIT（0=免费） |
| `discovery` | `announce_interval_seconds` | 20 | 公告间隔 |
| | `max_entries` | 1024 | 缓存条数上限 |
| | `max_announcement_ttl_seconds` | 120 | 本地允许的最长公告有效期（钳制远端时间） |
| | `offline_after_seconds` | 90 | 多久没活动标记为不可达（必须 > 公告间隔） |
| | `query_timeout_seconds` | 10 | 定向查询超时 |

## 公网部署配置（产品推荐）

```json
{
  "role": "coordinator",
  "transport": "tcp",
  "listen_host": "0.0.0.0",
  "listen_port": 17700,
  "allow_public_listen": true,
  "peers": ["<worker-node-id>", "<client-node-id>"]
}
```

- Worker/Client 的 `coordinator_addr` 填 `<PUBLIC_IP_OR_DOMAIN>:17700`。
- `allow_public_listen` 默认 false；监听任何非 loopback 地址都必须显式设为 true。
- 纯局域网仍支持：`listen_host` 填私网 IP；不是产品默认。
- NAT 穿透/relay 未实现；严格对称 NAT 用户不可用。

### TLS（公网必须）

Coordinator：

```json
{"tls": {"cert_file": "certs/server.crt", "key_file": "certs/server.key"}}
```

Worker/Client：

```json
{"tls": {"ca_file": "certs/server.crt", "server_hostname": "coordinator.example.com"}}
```

- 仅 `transport: "tcp"` 支持；省略 `tls` 保持明文（仅本地/兼容）。
- 客户端始终验证证书与主机名；证书 SAN 必须匹配 `server_hostname`/连接地址。
