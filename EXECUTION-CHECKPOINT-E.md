# 执行检查点 — 阶段 E-1（账本同步）

日期：2026-11-04
分支：`phase2`　提交：`d2046bf`（父提交 `c5f738a`，tag `checkpoint-E-pre`）
基线：`c5f738a`（249 测试）→ 本阶段 `d2046bf`（259 测试）

---

## 1. 阶段目标与实际完成

**目标**：LEDGER_SYNC 分页拉取 + 工人链验证至与协调者链字节一致镜像（E-1 验收标准）。

**实际完成**：

- **协议**：新增 `LEDGER_SYNC_REQUEST` / `LEDGER_SYNC_RESPONSE` 两种消息（封闭注册表现 16 种），
  请求体 `{after_seq}`，响应体 `{after_seq, head_seq, events, more}`；单页同时受
  count（≤100）与序列化字节（≤24 KiB，`LEDGER_PAGE_BYTES`）钳制，保证响应装进 32 KiB
  body 上限；每个事件是导出行的 13 键精确 schema（多余/缺失键拒绝，`sig_algo` 必须
  `ed25519`，`event_type` 必须等于 `CreditEventType.CREDIT.value`）。
- **账本**：`export_page(conn, after_seq, limit, max_bytes) -> (events, more)`、`head_seq(conn)`；
  事件按 `seq` 升序，`more` 表示游标之后仍有行。
- **运行时**：协调者 `_handle_ledger_sync_request`（只读，loop 线程内用自己的 Store 连接）；
  工人在每轮 claim 之前先 `_sync_ledger`：从镜像头开始分页拉取，逐事件
  `ledger.verify_and_append`（形状→方案→重复→seq 连续→prev_hash 链→哈希→签名→金额），
  任何一步失败置 `_ledger_conflict` 锁存：不跳过、不覆盖、不回卷，并在锁存清除前停止 claim；
  `head_seq < 镜像头`（协调者链比镜像短）同样锁存冲突，绝不静默截断镜像。
- **测试**：新增 `tests/test_ledger_sync.py`（9 项）与 `ShutdownTests` 的 DB 句柄释放回归。

## 2. 根因记录（本阶段发现并修复的三个缺陷）

1. **`event_type` 大小写不一致（E-1 同步卡死的直接原因）**
   数据库 `credit_events.event_type` 存放的是枚举规范值 `"CREDIT"`（且该字符串参与
   `credit-v2` 签名体），而 WIP 的线 schema 写成 `== "credit"`。结果：协调者**正常回了**
   响应（实测 2369 字节），接收方 schema 全部拒绝 → 工人侧表现为 `ledger sync timed out`，
   两端日志都没有报错，极难定位。定位方式：仓库外裸 TCP 对等方抓原始回帧
   （`HELLO`/`HELLO_ACK` 正常，第 3 帧解析报 `LEDGER_SYNC_RESPONSE event shape invalid`），
   再逐字段比对 13 列与实际值。
   修复：改为比对 `CreditEventType.CREDIT.value`，并加"漂移守卫"测试：
   存储值必须被接受、小写值必须被拒绝。

2. **sqlite 连接跨线程关闭（句柄泄漏，DB 文件在 shutdown 后被锁）**
   `Node.shutdown()` 在调用者线程关闭 loop 线程创建的 `Store`/镜像连接；sqlite3 线程亲和性
   使 `close()` 抛 `ProgrammingError`，而当时 `except: pass` 吞掉了它，于是
   `coordinator.db` 在干净 shutdown 后仍被占用（Windows 下无法删除/改名）。
   修复：两个角色循环在各自的 `finally` 里关闭自己创建的连接；`shutdown()` 只 join 循环线程
   并关闭构建线程上创建的 `_aux`（`check_same_thread=False`）。
   回归测试：`ShutdownTests.test_shutdown_closes_the_store_on_the_loop_thread`（用"改名成功"
   作为可观测断言）。

3. **日志 FileHandler 泄漏（日志文件在 shutdown 后被锁）**
   原来 `logging.basicConfig(handlers=[Stream, FileHandler])` 把每个节点的文件句柄永久挂在
   root logger 上；进程内的多个节点/测试因此长期占用各自的 `*.log`。
   修复：文件 handler 挂在 `idle_compute` logger 上并由节点持有，`shutdown()` 摘除并关闭。

## 3. 修改文件

**实现（3）**：`idle_compute/protocol.py`、`idle_compute/ledger.py`、`idle_compute/node.py`
**测试（3）**：`tests/test_protocol.py`（16 类型 fixture + `CREDIT` 规范值）、
`tests/test_node.py`（+1 回归）、`tests/test_ledger_sync.py`（新增，9 项）
**文档（3）**：`docs/phase2/TASK-BOARD.md`、`docs/phase2/NODE-PROTOCOL.md`、本文件
**删除**：无。**提交**：代码测试 `d2046bf`；文档随其后单独提交。

## 4. 测试命令与结果（真实执行）

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
→ Ran 259 tests in 170.703s → OK（exit 0，0 failed，0 skipped，0 errors）
.venv\Scripts\python.exe -m compileall -q idle_compute tests
→ exit 0
```

- 全量日志：`C:\Users\linxi\Desktop\DSH工作区\logs\idle-compute\e1-full-suite-2.log`
- E-1 专项：`python -m unittest tests.test_ledger_sync -v` → 9/9 通过
- 计数变化：249（阶段 C）→ 259（+9 新模块 +1 shutdown 回归）
- 环境说明：本机 DSH 沙箱下 `tempfile` 的 0700 目录不可写，测试经工作区外
  `sitecustomize.py` 垫片把 `os.mkdir(0o700)` 改为 `0750`（仅组读位差异）；
  **仓库代码/断言/验收标准未改**，无沙箱机器上直接运行即可。

## 5. 分页口径修正（如实记录）

验收原文写"150 事件→2 批（≤100）"。实测单条 ed25519 记账事件序列化约 600 B，
而响应必须装进 32 KiB body 上限，故分页受 count（≤100）与字节（≤24 KiB）双钳制，
150 事件实际为 4 页（约 42/42/42/24）。"2 批"的假设不成立；可验收的是分页契约
（无缺无重、尾页 `more=False`、超头空页），已由测试固定。

## 6. Git 状态

- 分支 `phase2`，HEAD `d2046bf`；无 remote、未推送；无 amend/reset/rebase。
- 工作区：仅 `PROJECT-STATUS-AND-HANDOFF.md`（交付报告，未跟踪，非仓库内容）。
- 回滚点：`c5f738a`（阶段 C，249 绿）、`07e7fc2`（阶段 C 审计修复）、`e3e6d5a`（Phase 1）。

## 7. 未完成（按计划顺序）

- E-3：`docs/COORDINATION.md` 诚实表 + 架构/协议/威胁模型更新 + grep 级反过度声明测试。
- F-1..F-4、G-1..G-2、H 独立审计、I 最终交付；双设备验证仍未执行（保持"未验证"）。

---

# 追加：阶段 E-2（同日完成）

提交：`bf35356`（代码+测试）。基线：`daa2835`（259 测试）→ `bf35356`（263 测试）。

## 8. E-2 交付内容

- **镜像元数据**：worker 的 `ledger_copy.db` 增加 `ledger_meta` 表（仅工人侧、非链上）：
  `ledger.record_conflict / clear_conflict / read_conflict / mirror_status`。
- **启动全链重审**：工人每次启动从 `seq 0` 重审整条镜像链（`_audit_cursor` 可跨轮续审，
  受 `MAX_SYNC_PAGES_PER_ROUND` 限制），再转入增量拉取。`verify_and_append` 对"同
  event_id 但内容不同"的已镜像行返回内容不一致冲突，因此**已镜像事件的原地篡改可检出**，
  而不是被当作幂等重放静默跳过。
- **隔离语义**：任何分歧（链篡改、hmac 行、协调者链比镜像短）→ 锁存冲突（含 offending
  seq，持久化于 `ledger_meta`）→ 停止 claim；不跳过、不覆盖、不回卷。
  锁存期间协调者侧任务保持 `QUEUED`、无 ACTIVE 租约（测试断言）。
- **运维面**：新增 `worker ledger status --config <node.json>`，输出
  `mirror_head / events / verified / conflict{seq,reason,at} / claim_blocked`。
- **线 schema 调整（重要）**：`LEDGER_SYNC` 事件的 `sig_algo` 不再被钉死为 `ed25519`。
  原因：整页 schema 拒绝会让恶意降级行退化成"工人侧静默超时"，既无 CONFLICT 也无 seq；
  现在降级行会到达镜像侧并由 `verify_and_append` 以结构化 CONFLICT 拒绝。安全性不降：
  镜像依旧只接受 ed25519 行。

## 9. E-2 测试证据

```powershell
.venv\Scripts\python.exe -m unittest tests.test_ledger_conflict -v
→ Ran 4 tests in 54.635s → OK
.venv\Scripts\python.exe -m unittest discover -s tests -v
→ Ran 263 tests in 226.439s → OK（exit 0）
.venv\Scripts\python.exe -m compileall -q idle_compute tests
→ exit 0
```

- 日志：`C:\Users\linxi\Desktop\DSH工作区\logs\idle-compute\e2-full-suite.log`
- 覆盖：篡改链 → 新镜像在 seq 3 停住且 claim 被阻断 + CLI 报告 seq 3；hmac 行 → 结构化
  CONFLICT 且**不截断**已镜像内容；删除 worker.db → 从 0 重建逐列一致；重复同步轮次/重启
  → 双方各恰 1 事件、幂等键不重复。

## 10. E-2 后的 Git 状态

- 分支 `phase2`，HEAD `bf35356`；无 remote；无 amend/reset/rebase。
- 回滚点：`daa2835`（E-1 完成，259 绿）、`c5f738a`（阶段 C，249 绿）。

