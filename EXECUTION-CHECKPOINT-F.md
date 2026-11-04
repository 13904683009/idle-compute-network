# 执行检查点 — 阶段 F（安全加固）

日期：2026-11-04
分支：`phase2`　状态：**进行中（F-1 完成）**
基线：`f52a502`（阶段 E 完成，270 测试）

---

## 1. F-1：每 peer 令牌桶与隔离（已完成）

提交：`0644870`

### 实现

- **`idle_compute/ratelimit.py`（新增）**
  - `TokenBucket`：速率 20 msg/s、容量 40，时钟可注入（测试确定性，不靠 sleep）。
  - `PeerRateLimiter`：
    - **一次连续丢帧 = 一次触发**（episode 语义）：突发洪水只记 1 次，不会"每丢一帧记一次"；
    - **2 次触发 / 60 s → 关流**，并把该 peer 隔离 **30 s**；
    - 状态有界：最多跟踪 1024 个 peer，按最后活动时间淘汰；
    - 隔离期内 `blocked()` 为真。
- **`idle_compute/node.py`**
  - 每个入站帧在**解析/验签之前**先过限速（超限帧直接丢弃，不回包，避免反射放大）；
  - 限速键：HELLO 绑定前用传输对端标识，绑定后用 **node_id** —— 因此 TCP 重连**不能重置**桶；
  - 第 2 次触发时回 `ERROR(OVERLOAD)` 并关流；被隔离的 node_id 在 HELLO 阶段即被拒（回 `OVERLOAD` 并关流）。

### 测试（`tests/test_ratelimit.py`，8 项）

| 类型 | 覆盖 |
| --- | --- |
| 确定性单元（注入时钟） | 突发 40 → 第 41 帧被拒；0.5 s 后补 10 个；长期空闲封顶 40；episode 语义；2 次触发→隔离；60 s 窗口外的旧触发不累计；30 s 后恢复；peer 间互不影响 |
| 真实 socket | 洪泛 peer 被关流/隔离（新连接在 HELLO 阶段被拒），同时另一 allowlisted peer 连发 20 个 PING 全部收到 PONG（隔离不影响他人） |
| 真实 socket | **重启后重放旧帧被持久化窗口拒绝**：重启协调者（同一数据目录），重放重启前 seq=1 的 HELLO → `SEQ_OUT_OF_WINDOW`，且不绑定流 |

### F-1 验收项与既有测试的对应

| 验收项 | 覆盖位置 |
| --- | --- |
| 令牌桶数学 | `test_ratelimit.py`（注入时钟） |
| 重启后旧帧重放拒绝 | `test_ratelimit.py::test_replayed_frame_is_rejected_after_coordinator_restart` |
| 窗口内缺口接受 / 窗口下拒绝 | `test_protocol.py`（`SeqWindow` 边界用例） |
| ±300 s STALE | `test_protocol.py::TestTimestamp` |
| sender ≠ transport peer → BAD_SENDER（先于验签） | `test_node.py::test_hello_identity_mismatch_rejected` 等 |
| 旧 RESULT_ACK 重放不双发 | `test_node.py::test_duplicate_submission_no_double_credit` + 账本幂等测试 |

## 2. 测试命令与结果（真实执行）

```powershell
.venv\Scripts\python.exe -m unittest tests.test_ratelimit -v
→ Ran 8 tests in 27.327s → OK
.venv\Scripts\python.exe -m unittest discover -s tests -v
→ Ran 278 tests in 257.892s → OK（exit 0）
.venv\Scripts\python.exe -m compileall -q idle_compute tests
→ exit 0
```

- 日志：`C:\Users\linxi\Desktop\DSH工作区\logs\idle-compute\f1-full-suite.log`
- 计数：270（阶段 E）→ **278**（+8）

## 3. 文档同步（诚实性门禁联动）

F-1 完成后，以下"未实现"陈述已同步改写，否则 `tests/test_docs.py` 会判定文档与代码不一致：

- `docs/PROTOCOL.md` 限额表：令牌桶行改为"已实现（F-1，`ratelimit.py`；键为绑定后的 node_id）"；
  §10 的"限流未实现"从清单移除。
- `docs/COORDINATION.md` 缺口表：速率限制行改为"F-1 已实现"并写明数值与重连不可重置。
- `docs/THREAT_MODEL.md` 洪泛行：改为已实现 + 引用 `test_ratelimit.py`，并保留"洪泛仍消耗有限带宽"的诚实说明。
- `docs/phase2/NODE-PROTOCOL.md`：补 F-1 的具体规则，并把 F-2/F-3/F-4 标为仍未实现。
- `tests/test_docs.py`：原"F-1 不得声称完成"的断言**翻转**为"限额必须写明数值 + 剩余缺口仍须标注未实现"。

## 4. Git 状态

- HEAD `0644870`；无 remote；无 amend/reset/rebase；工作区仅剩未跟踪的交付报告。
- 回滚点：`f52a502`（阶段 E 完成，270 绿）、`c5f738a`（阶段 C，249 绿）。

## 5. 未完成

- **F-3**：线边界全套输入校验 + 注册表分发唯一性 + 扩展源码安全扫描。
- **F-4**：日志脱敏（node id 截断/签名与哈希截断/只记计数）+ 3 进程对抗日志捕获。
- G-1/G-2、H 独立审计、I 最终交付；双设备验证仍未执行（保持"未验证"）。

---

# 追加：F-2（错误隔离与资源上限）

提交：`0f4fc5e`。基线：`5af245e`（278 测试）→ `0f4fc5e`（**282 测试**）。

## 6. F-2 交付内容

- **任务池上限（新实现）**：协调者对**非终态任务**（QUEUED/LEASED/RUNNING/SUBMITTED）
  设 `MAX_ACTIVE_TASKS = 1000`；第 1001 个 `TASK_CREATE` 回
  `ERROR(OVERLOAD, "task pool is full")`。计数在 actor 线程内完成 → 无 check-then-insert
  竞态；终态任务不计入。
  > 说明：此前 PROTOCOL/THREAT_MODEL 已把"任务队列上限 1000"写成现网值，但代码里
  > **并不存在**该检查。F-2 才发现并补上——这是"文档先于实现"的一次实例，已按实现修正。
- **错误隔离（验证 + 测试固化）**：单条流在框架层损坏只关该流；另一条已绑定流继续
  正常 PING/PONG；200 个畸形帧后节点仍服务；真实子进程（`node run`）在 200 帧虐待后
  仍存活并成功响应真实 client 的 PING。

## 7. F-2 测试证据

```powershell
.venv\Scripts\python.exe -m unittest tests.test_abuse -v
→ Ran 4 tests in 34.685s → OK
.venv\Scripts\python.exe -m unittest discover -s tests -v
→ Ran 282 tests in 293.199s → OK（exit 0）
.venv\Scripts\python.exe -m compileall -q idle_compute tests
→ exit 0
```

日志：`C:\Users\linxi\Desktop\DSH工作区\logs\idle-compute\f2-full-suite.log`

| 测试 | 断言 |
| --- | --- |
| `test_task_pool_cap_returns_overload_on_the_1001st` | 预置 1000 个任务 → 线上 TASK_CREATE 收到 code 15 + "full" |
| `test_stream_failure_does_not_affect_another_bound_stream` | 一条流被截断帧打死，另一条**已绑定**流仍收到 PONG |
| `test_200_malformed_frames_leave_the_node_serving` | 200 个合法帧头+非法 JSON 后，新 peer 仍能握手并 PING |
| `test_node_process_survives_abuse` | 真实子进程被 200 帧虐待后 `poll() is None`，且真实 client 仍收到 PONG |

## 8. F-2 后的 Git 状态

- HEAD `0f4fc5e`；无 remote；无 amend/reset/rebase。
- 回滚点：`5af245e`（F-1 完成，278 绿）、`f52a502`（阶段 E，270 绿）。
- 剩余：F-3（线边界 + 源码扫描）、F-4（日志脱敏），随后 G-1/G-2、H、I。

---

# 追加：F-3（线边界 + 注册表分发 + 源码门禁）

提交：`1ff7d4f`。基线：`4fda429`（282 测试）→ `1ff7d4f`（**288 测试**）。

## 9. F-3 交付内容

- **`tests/helpers/peers.py`（新增）**：可复用的裸 TCP 对等方（真实帧、真实 socket），
  兼容 `unittest discover` 与 `python -m unittest tests.x` 两种导入方式。
- **`tests/test_wire_boundary.py`（新增 3 项）**：对真实协调者发送恶意帧，并**给执行器打点**：
  - 10 类载荷（5000 个数、bool、float、字符串/shell 元字符、多余键、嵌套列表、
    非白名单类型、带 shell 元字符的类型名）全部被拒，`NumericBatchTask.execute`
    **一次都没被调用**；随后直接调用一次执行器作为**正控**，证明打点有效；
  - 未知消息类型：线上被拒（撤销注册表级断言，独立于签名顺序验证 `UNSUPPORTED_TYPE`）；
  - 深度 20 的 JSON 炸弹：被丢弃、不当作 PING 处理、节点继续服务
    （并记录根因：`_on_parse_error` 需要重新解析原始帧才能寻址回复，解析不了就不猜、静默丢弃）。
- **`tests/test_source_safety.py`（+3 项，共 5 项）**：
  - libp2p 导入仅限 `identity.py` / `net_transport.py`；
  - 包内无 `asyncio` 导入；
  - `TASK_TYPES` 只在 `tasks.py` 定义；Phase-2 运行时（`worker.py`/`node.py`/两种传输）
    不得直接引用具体任务类，只能经 `get_task_type()` 取执行器。
    （`coordinator.py` 属 Phase 1 本地编排，允许直接引用——已在断言与注释中标明。）

## 10. F-3 测试证据

```powershell
.venv\Scripts\python.exe -m unittest tests.test_wire_boundary tests.test_source_safety
→ Ran 8 tests → OK
.venv\Scripts\python.exe -m unittest discover -s tests -v
→ Ran 288 tests in 326.460s → OK（exit 0）
.venv\Scripts\python.exe -m compileall -q idle_compute tests
→ exit 0
```

日志：`C:\Users\linxi\Desktop\DSH工作区\logs\idle-compute\f3-full-suite.log`

## 11. F-3 后的 Git 状态

- HEAD `1ff7d4f`；无 remote；无 amend/reset/rebase。
- 回滚点：`4fda429`（F-2 完成，282 绿）、`5af245e`（F-1，278 绿）。
- 剩余：F-4（日志脱敏）→ G-1/G-2 → H → I；双设备验证仍未执行。
