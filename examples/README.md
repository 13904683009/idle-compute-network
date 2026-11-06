# 公网部署配置模板（占位符 + 字段说明）

这三个 `node.json` 是**合法 JSON 模板**，但含 `<...>` 占位符，复制后必须替换才能启动。
完整步骤见 [REMOTE-PARTNER-GUIDE.md](../REMOTE-PARTNER-GUIDE.md)。

## 使用方法

```bash
mkdir -p var/remote/{coordinator,worker,client}
cp examples/public-coordinator/node.json var/remote/coordinator/node.json
cp examples/public-worker/node.json      var/remote/worker/node.json
cp examples/public-client/node.json      var/remote/client/node.json
# 先用 identity 生成/读取 node_id（此命令不校验占位符）
python -m idle_compute identity --config var/remote/coordinator/node.json
python -m idle_compute identity --config var/remote/worker/node.json
python -m idle_compute identity --config var/remote/client/node.json
# 再替换 node_id / 公网地址 / 证书路径，然后启动
```

## 字段说明

| 字段 | 说明 |
| --- | --- |
| `role` / `transport` | 固定为 coordinator/worker/client + `tcp`；TLS 仅支持 tcp |
| `listen_host` / `listen_port` | Coordinator 公网监听 `0.0.0.0:17700`；Worker/Client 本地监听即可 |
| `allow_public_listen` | 监听非 loopback 地址时必须为 `true`（显式授权） |
| `peers` | **必填**：Coordinator 填 Worker+Client 的 node_id；Worker/Client 填 Coordinator node_id |
| `coordinator_node_id` / `coordinator_addr` | Worker/Client 主动连接的 Coordinator 身份与公网地址 |
| `credits.task_cost` | `0`=免费；`>=1`=付费（requester 需先有 CREDIT） |
| `tls.cert_file` / `tls.key_file` | Coordinator 服务端证书/私钥（公网必须） |
| `tls.ca_file` / `tls.server_hostname` | Worker/Client 校验服务端证书；hostname 必须匹配证书 SAN |
| `capability` | Worker 能力公告；模板已给白名单任务/资源上限 |
| `log_file` | 日志路径；排查连接/TLS/租约问题时设为 `DEBUG` |

## 注释约定

JSON 不允许注释，因此“注释”放在本文件与 `REMOTE-PARTNER-GUIDE.md` 中：

- `<COORDINATOR_NODE_ID>` / `<WORKER_NODE_ID>` / `<CLIENT_NODE_ID>`：`identity` 输出；
- `<PUBLIC_HOST>` / `<PUBLIC_IP>`：Coordinator 的公网域名/IP（证书 SAN 必须包含）；
- `certs/server.crt` / `certs/server.key`：按 `REMOTE-PARTNER-GUIDE.md` §4 生成；
- TLS 公网必须开启；不配置 `tls` 的明文仅用于单机/局域网兼容。
