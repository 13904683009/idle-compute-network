# D2 阶段报告（P2P 能力发现与公告缓存）

日期：2026-11-04　基线：`phase2` @ `eb6e7e9`（D1 交付，342 测试）
状态：**D2 完成并在此暂停；未开始 D3/D4/D5/D6。**

---

## 1. 工作前检查（实测）

| 项 | 结果 |
| --- | --- |
| HEAD | `eb6e7e9`（D1 report） |
| 工作区 | 干净，仅未跟踪 `PROJECT-STATUS-AND-HANDOFF.md`（26 KB，D0 时点的交付报告，未删除/未改写） |
| remote | 无（未配置、未 push/fetch/pull） |
| 基线复现 | **342/342 OK，336.949 s**（`logs/idle-compute/d2-baseline-342.log`），编辑前实测，可复现 |
| 只本地提交 | 是 |

---

## 2. D2-1：peers 配置形状（ADR-D0-13）

提交 `f33887f`。规则全文见 `docs/phase2/D2-CONFIG-MIGRATION.md`。

- `peers` = node-id 列表（allowlist，唯一信任来源）；新增可选 `directory = {node_id: {role, addrs}}`
  （元数据，不参与信任）；`directory` 的 key 必须在 `peers` 中（不允许"描述一个不被允许的 peer"）。
- **旧对象格式仍可读**：加载时迁移为 list + directory，role/addrs 全保留，`NodeConfig.migration_hint`
  给出可操作提示；`peers add` 会把旧文件改写成新形状（迁移提示写 stderr）。
- 旧对象 + `directory` 同时出现 → `load_config` 拒绝（不猜测）；另：旧对象条目缺 `role`、重复 id、
  `directory` 未知键/非法 addr、`peers` 是字符串 → 都有明确报错。
- **写出后可回读**：`peers add` → `load_config` 成功（`test_cli.py::test_peers_add_output_is_loadable_by_load_config`）；
  `NodeConfig.to_dict()` → `from_dict()` 相等（`test_peer_directory.py::test_new_shape_roundtrips_exactly`）。
- 既有 `test_cli.py` 中 3 个 peers 用例**按批准的语义更新**（旧断言写的是旧形状），并新增 1 个更强的
  round-trip 用例；`tests/test_peer_directory.py` 新增 10 项。

## 3. D2-2：discovery.py + 3 条新消息

### 3.1 新消息（封闭注册表 16 → 19）

| 类型 | recipient | body（精确键） | 上限 |
| --- | --- | --- | --- |
| `CAPABILITY_ANNOUNCE` | `null`（广播）或本机 id | `manifest`, `announcement` | 每个嵌套对象 24 KiB |
| `CAPABILITY_QUERY` | 必须指定 | `task_type`, `runtime`（可 null） | 字段 ≤64 字符 |
| `CAPABILITY_RESPONSE` | 必须指定 | `query_seq`, `peers[]` | 候选 ≤32 |

- 签名：**两层**——信封 Ed25519（域 `idle-compute-node-msg-v2`，既有）+ manifest/announcement 各自的 D1 签名域；
  转发方无法伪造（验签按 manifest.node_id，而不是信封 sender）。
- recipient 策略：`BROADCAST_TYPES={ANNOUNCE}`，`DIRECTED_TYPES={QUERY,RESPONSE}`；定向消息
  `recipient=null` → `INVALID_BODY`，收件人非本机 → `UNAUTHORIZED`（均在验签之后）。
- 防重放/时间戳：完全复用信封 seq 窗口（每 sender 4096 窗口，持久化）+ ±300 s；公告层再加
  `sequence` 单调与 `expires_at`，二者独立。

### 3.2 缓存（`AnnouncementCache`）

| 项 | 值 |
| --- | --- |
| 容量 | `max_entries=1024`（可配）；满时先淘汰过期，再淘汰最久未收（同值按 node_id），仍无可淘汰 → `CACHE_FULL`（不插入、不持久化） |
| 单条上限 | `max_entry_bytes=64 KiB`（超出 `ENTRY_TOO_LARGE`） |
| 过期 | 远端 `expires_at` 被本地钳制为 `min(远端, received_at + 120 s)`；`observed_at` 超前 300 s 拒绝 |
| 在线判定 | 与"公告有效"分离：`offline_after_seconds=90` 内收到任意已验签帧才算 reachable；`lookup` 默认排除不可达 |
| 持久化 | `<data_dir>/discovery.db` 表 `peer_announcement_state`；**先写库后写内存**，失败 → `PERSIST_FAILED` 且缓存不变 |
| 线程安全 | 连接 `check_same_thread=False` + 内部锁（D2 实测暴露过 loop 线程与 stream 线程共用 sqlite 的亲和性错误，已修复） |

### 3.3 节点接线（`node.py`）

- 所有角色：能力消息在 `_on_envelope` 里优先处理；公告失败只记日志、不回包（防放大）。
- coordinator / worker：每个 poll tick `sweep`；配置了 `capability` 段则按间隔发布自己的公告
  （序号**先持久化后发送**，重启不会被判回滚）。
- client：`query_capabilities(peer_id, task_type=…, runtime=…)` 同步查询。
- 额外加固：`_on_envelope` 抛异常不再静默关流（记日志 + 回 `INTERNAL`），把本次发现的
  "handler 异常杀死流"变成一个可诊断错误。

## 4. 修改/新增文件

| 文件 | 性质 | 作用 |
| --- | --- | --- |
| `idle_compute/config.py` | 改 | peers/directory 双读兼容、`capability`/`discovery` 段校验、`to_dict` 新形状 |
| `idle_compute/cli.py` | 改 | `peers add/list` 写/读新形状 + 旧文件修复 + 迁移提示 |
| `idle_compute/protocol.py` | 改 | 3 条新消息 + 精确 schema/大小上限 + 广播/定向 recipient 策略（16 → 19） |
| `idle_compute/discovery.py` | **新增** | 缓存、校验顺序、持久化状态、撤销、可达性、manifest/announcement 构造器 |
| `idle_compute/node.py` | 改 | 发现消息分发、发布/查询 API、周期 sweep/发布、关闭时释放发现库、handler 异常兜底 |
| `tests/helpers/peers.py` | 改 | RawPeer 支持自定义 recipient（供定向消息测试） |
| `tests/test_peer_directory.py` | 新增 | 10 项配置迁移/回读测试 |
| `tests/test_discovery.py` | 新增 | 20 项缓存/校验/过期/容量/持久化测试 |
| `tests/test_discovery_network.py` | 新增 | 2 项真实两进程测试 + 定向 recipient 线测 |
| `tests/test_protocol.py` | 改 | 类型计数 16→19、3 个样本、11 项新消息测试 |
| `tests/test_cli.py` | 改 | peers 用例改为新形状 + round-trip（见 §2） |
| `tests/test_abuse.py` | 改 | 仅清理健壮性：`TemporaryDirectory(ignore_cleanup_errors=True)`（Windows 句柄释放延迟，断言未变） |
| `docs/PROTOCOL.md` | 改 | 消息目录 19 种、D2 节、广播/定向规则 |
| `docs/phase2/DISCOVERY-PROTOCOL.md` | **新增** | 发现协议全文（校验顺序/缓存/持久化/未实现边界/验收对照） |
| `docs/phase2/D2-CONFIG-MIGRATION.md` | **新增** | 配置迁移规则 |

## 5. 测试结果（原始日志）

**最终运行（全绿）**

```
Ran 386 tests in 339.007s
OK
compileall -q idle_compute tests → exit 0
```

- 日志：`C:\Users\linxi\Desktop\DSH工作区\logs\idle-compute\d2-full-suite-final.log`
- 基线 342 → **386**（+44）：`test_peer_directory` 10、`test_cli` +1、`test_protocol` +11、
  `test_discovery` 20、`test_discovery_network` 2。
- 0 failed / 0 skipped。

### 5.1 失败证据（保留）

| 轮次 | 结果 | 原因与处理 |
| --- | --- | --- |
| 基线（编辑前） | 342/342 OK | `d2-baseline-342.log`，耗时 336.949 s |
| 第一次全量 | 386 项，1 failure | `test_bad_revocation_signature_is_rejected` 里我篡改签名的写法可能"改回原值"（末两位恰为 00）；**改测试实现**（翻转首字符），不是放松断言。日志 `d2-full-suite.log` |
| 第二次全量 | 386 项，1 error | `test_node_process_survives_abuse` 仅在 Windows 临时目录清理阶段报句柄占用（断言已通过）；`test_abuse` 改为 `ignore_cleanup_errors=True`，断言未变。日志 `d2-full-suite-2.log` |
| 第三次全量 | **386/386 OK** | `d2-full-suite-final.log` |

### 5.2 源码门禁

- `tests/test_source_safety.py` 与 D0 版本**逐字节一致**（`git diff 8bf1162 -- tests/test_source_safety.py` = 0 行）。
- `tests/test_docs.py` 同样 0 行差异。
- 新模块 `discovery.py` 未 import `libp2p` / `asyncio` / `socket`（门禁通过）。

## 6. 真实进程测试（D2 验收 #13）

拓扑：

```
worker 子进程（capability 配置） --dial+HELLO--> coordinator 子进程，随后发 CAPABILITY_ANNOUNCE
测试进程的 client Node        --dial+HELLO--> coordinator，发 CAPABILITY_QUERY（定向）
                                            <-- CAPABILITY_RESPONSE（只含 worker）
```

启动方式：两个 `python -m idle_compute node run --config …` 子进程（独立 data_dir、identity、
DB），TCP 回环，固定端口 + 就绪探测；测试结束 terminate + wait。

断言：响应中恰好 1 条、node_id=worker、manifest/announcement 都是 128 hex 签名；
不匹配的 `task_type` 查询返回空；coordinator 的 `discovery.db` 里存在该 worker 的
`last_sequence/ capability_version` 行；worker 进程仍存活。耗时 ~3.9 s。
另有 1 条真实 socket 用例断言定向查询的 recipient 规则（错投 `UNAUTHORIZED`、
`recipient=null` `INVALID_BODY`）。

## 7. 已知限制与风险

1. **发现依赖静态 bootstrap**：只有 `peers` 中已有连接的节点才能收到公告；没有 DHT/Gossip/Relay/mDNS。
2. **广播是尽力而为**：coordinator 无出站流时发不出公告（`failed` 计数）；当前拓扑是 worker→coordinator 单向发现。
3. **缓存不含信誉/基准**：`SELF_DECLARED` 只是自述；匹配器（D3）不得把它当硬件证明。
4. **公告内容不加密**：其他 allowlisted 节点可读到设备档位信息；隐私档位已按 D1 建议默认最小化。
5. **时钟**：过期钳制降低了远端谎报的影响，但本机时钟被改仍会影响过期判断。
6. **重启语义**：公告本身不持久化（缓存空），只有序号/版本/撤销状态持久化；重启后需重新收公告。
7. **单机多进程 ≠ 双设备**：D6 未执行，不得宣称多设备通过。

## 8. D2 验收对照

| # | 验收项 | 状态 |
| --- | --- | --- |
| 1–12 | 公告合法/非法签名/身份不匹配/重复/旧序号/重启回滚/撤销/过期/缓存上限/定向响应/广播/离线 | ✅ 全部有测试（见 `DISCOVERY-PROTOCOL.md` §8） |
| 13 | 两个独立进程交换公告、验证签名、缓存、查询 | ✅ worker+coordinator 子进程 + client 查询（真实 socket） |
| 14 | peers 旧读/新写/回读 | ✅ `test_peer_directory.py`（10）+ CLI round-trip |
| 15 | 新消息 schema/长度/recipient | ✅ `test_protocol.py::TestCapabilityMessages`（11） |
| 全量 | 既有 342 项不回归 | ✅ 386/386，0 跳过；门禁逐字节未变 |
| 双设备 | D6 | ❌ 未执行（明确标注） |

**结论：D2 满足验收。建议进入 D3（匹配器），但不自动开始——等待审计/负责人确认。**

---

## 9. Git

| 提交 | 内容 |
| --- | --- |
| `f33887f` | D2-1：peers 列表 + directory、旧对象迁移、配置/CLI 测试、迁移文档 |
| `d67601e` | D2-2：三条发现消息、`discovery.py`、node 接线、44 项测试、协议文档 |
| （本报告单独提交） | `D2-REPORT.md` |

基线：`eb6e7e9`（D1）。无 remote、无 push/fetch/pull、无历史改写。
本轮只做本地提交；`PROJECT-STATUS-AND-HANDOFF.md` 保持未跟踪、未改动。

