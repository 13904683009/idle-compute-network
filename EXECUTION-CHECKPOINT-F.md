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

- **F-2**：错误隔离 + 洪泛/超大帧/资源上限（运行中节点）、200 畸形帧模糊测试、1001 任务 OVERLOAD、进程虐待后存活。
- **F-3**：线边界全套输入校验 + 注册表分发唯一性 + 扩展源码安全扫描。
- **F-4**：日志脱敏（node id 截断/签名与哈希截断/只记计数）+ 3 进程对抗日志捕获。
- G-1/G-2、H 独立审计、I 最终交付；双设备验证仍未执行（保持"未验证"）。
