# D3 阶段报告（CapabilityMatcher）

日期：2026-11-04　基线：`phase2` @ `3ed2575`（D2 交付，386 测试）
状态：**D3 完成并在此暂停；未开始 D4/D5/D6。**

---

## 1. 开工检查与基线复现

| 项 | 实测 |
| --- | --- |
| 起始 HEAD | `3ed2575`（D2 report） |
| 工作区 | 干净；仅未跟踪 `PROJECT-STATUS-AND-HANDOFF.md`（26 KB，未删改） |
| remote | 无；未 push/fetch/pull，无历史改写 |
| D2 基线 | **386/386 OK，340.904 s**（`logs/idle-compute/d3-baseline-386.log`），改动前实测 |
| D2 审计 | 淘汰与防回滚**已分离**（见 §6.1）；`test_abuse` 清理改动**未掩盖功能断言**（§6.2） |

## 2. 真实字段清单（以代码为准，未新造模型）

- `TaskRequirement`：`task_id, task_type, task_version, input_manifest_hash, required_runtime,
  minimum_resources{cpu_cores,ram_mb,gpu_memory_mb}, preferred_resources{gpu_vendor,gpu_compute_profile},
  constraints{max_execution_seconds,max_input_bytes,max_output_bytes},
  data_policy{requires_private_data,allow_relay}, verification_policy{mode∈{RECOMPUTE},required},
  deadline, schema_version`。
- `CapabilityManifest`：`node_id, capability_version, issued_at, expires_at,
  supported_task_types, supported_runtimes, hardware{cpu_arch,cpu_cores?,ram_mb?,gpu_vendor?,gpu_memory_mb?},
  resource_limits{max_cpu_cores,max_ram_mb,max_gpu_memory_mb,max_task_duration_seconds},
  network{supported_transports,supports_relay}, verification{level∈{SELF_DECLARED,BENCHMARKED,VERIFIED},
  benchmark_profile}, signature`。
- `CapabilityAnnouncement`：`node_id, capability_version, sequence, observed_at, expires_at,
  available_resources{cpu_cores,ram_mb,gpu_memory_mb}, queue{running_tasks,queued_tasks},
  network_status{connectivity∈{DIRECT,RELAY,UNKNOWN},supported_transports}, accepting_tasks, signature`。
- 发现层：`CachedPeer.reachable`（liveness 窗口）、版本级 `revoked`。

**仓库没有** benchmark 分数、带宽、RTT、历史成功率、信誉、硬件证明字段 → 匹配器不包含这些维度。

## 3. 接口（`idle_compute/matcher.py`，纯函数）

```python
match(requirement, candidates, *, now: int, verify_signatures: bool = True) -> MatchResult
Candidate(node_id, manifest, announcement, reachable=True, revoked=False)
Candidate.from_cache_entry(entry)          # 适配 discovery.CachedPeer（鸭子类型）
MatchResult.ranking / .rejected / .evaluations / .as_dict() / .explain(node_id)
CandidateEvaluation.score / .scores() / .breakdown / .rejection_codes / .rejection_details
```

纯度：无数据库/网络/子进程/全局状态；不修改入参；`now` 显式传入；不依赖随机数或 `time.time()`。

## 4. 硬约束（先过滤，全部原因都收集）

`INVALID_CANDIDATE`、`NODE_ID_MISMATCH`、`SIGNATURE_INVALID`、`MALFORMED_CAPABILITY`、
`MANIFEST_EXPIRED`、`ANNOUNCEMENT_EXPIRED`、`CAPABILITY_REVOKED`、`NODE_UNREACHABLE`、
`DEADLINE_PASSED`、`TASK_TYPE_UNSUPPORTED`、`RUNTIME_MISMATCH`、`RESOURCE_UNSPECIFIED`、
`RESOURCE_INSUFFICIENT`、`DURATION_LIMIT_EXCEEDED`、`NOT_ACCEPTING`、`RELAY_NOT_ALLOWED`、
`DATA_POLICY_UNPROVEN`、`VERIFICATION_UNPROVEN`。

保守规则（缺声明一律不通过）：
- `available_resources` 缺键 → `RESOURCE_UNSPECIFIED`（**未声明 ≠ 满足**）；
- 资源按 `min(announced, manifest.resource_limits)` 生效（自报不能超过自身上限）；
- `required_runtime` 不在候选 `supported_runtimes` → 拒绝；
- `requires_private_data=True` → 无字段能证明 → 全部 `DATA_POLICY_UNPROVEN`；
- `verification.required=True` 且 `level=SELF_DECLARED` → `VERIFICATION_UNPROVEN`（不自动升级）；
- `allow_relay=False` 且 `connectivity=RELAY` → `RELAY_NOT_ALLOWED`；
- deadline `<= now` 即失败（边界含等于）。

## 5. 评分（仅通过硬约束的候选，整数 0..100，逐维可解释）

| 维度 | 范围 | 规则 |
| --- | --- | --- |
| `headroom` | 0..40 | 所需维度 `available/required` 最小百分比：100%→0，≥200%→40 |
| `trust` | 0..25 | SELF_DECLARED=0，BENCHMARKED=15，VERIFIED=25（仅声明等级） |
| `idle` | 0..20 | 队列 0→20，1→15，2→10，≥3→0 |
| `connectivity` | 0..5 | DIRECT=5，RELAY=2，UNKNOWN=0 |
| `preference` | 0..10 | requirement 指定 `gpu_vendor` 且 manifest 声明同款才得分，缺失=0 |

`sum(breakdown) == score`；`as_dict()` 输出逐维分值、trust_level、`self_declared_fields`。
**未引入** benchmark/带宽/延迟/信誉维度；可达性只做硬过滤，不重复计分。

## 6. 审计结论

### 6.1 D2 淘汰 vs sequence 防回滚

**无缺陷。** 证据：
- 代码：淘汰只 `self._entries.pop(...)`；`_last_sequences` 与 `discovery.db` 的
  `peer_announcement_state` 不受影响；`ingest` 用 `max(内存, 持久化)` 做序号下限。
- 新增回归测试（不改 discovery 实现）：
  - `test_eviction_does_not_reset_anti_rollback_state`：淘汰后重放旧 seq → `SEQ_ROLLBACK`，
    `store.last_sequence == 5`，随后 seq=6 可重新入缓存；
  - `test_eviction_then_restart_still_rejects_the_old_sequence`：淘汰 + 重启后仍拒绝。

### 6.2 `test_abuse.py` 的 `ignore_cleanup_errors=True`

- 变更仅限 `TemporaryDirectory(...)` 参数一行，功能断言（进程存活 `assertIsNone(proc.poll())`、
  PONG 响应）未动。
- 清理仍执行 `proc.terminate()` + `proc.wait(10.0)`；进程不退出会让 `wait` 超时并报错，因此
  **"进程未退出"不会被掩盖**。
- 唯一被掩盖的是 Windows 下进程退出后临时目录删除的瞬时 `WinError 32`；残余风险（长期持锁的真实
  泄漏不再在清理阶段报错）已记录，D6 物理设备验收时应保留人工检查。

## 7. 测试结果（原始日志）

**D2 基线**：`Ran 386 tests in 340.904s / OK`
**D3 最终**：

```
Ran 419 tests in 337.822s
OK
compileall -q idle_compute tests → exit 0
0 failed / 0 skipped
```

- 最终日志：`C:\Users\linxi\Desktop\DSH工作区\logs\idle-compute\d3-full-suite.log`
- 386 → **419**（+33）：`test_matcher.py` 31 项 + `test_discovery.py` 审计 2 项。
- 既有测试未删除/跳过/弱化；`tests/test_source_safety.py`、`tests/test_docs.py` 与 D0 逐字节一致
  （`git diff 8bf1162 -- tests/test_source_safety.py tests/test_docs.py` = 0 行）。

## 8. 确定性与 tie-breaker 证据

- `test_repeated_run_is_identical`：同一输入跑两次，`MatchResult` 完全相等。
- `test_input_order_does_not_change_ranking`：候选正序/逆序，排名、分数、评估顺序（按 node_id）一致。
- `test_tie_breaker_is_node_id`：两个同分候选按 node_id 升序，且与输入顺序无关。
- 实现：`evaluations.sort(key=node_id)`；`ranking.sort(key=(-score, node_id))`；无随机/时间/字典序依赖。

## 9. 文件变更

| 文件 | 性质 | 作用 |
| --- | --- | --- |
| `idle_compute/matcher.py` | **新增** | 纯函数匹配器：硬约束、评分、解释、确定性排序 |
| `tests/test_matcher.py` | **新增** | 31 项：约束/评分/解释/确定性/边界/不改入参 |
| `tests/test_discovery.py` | 追加 2 项 | D2 淘汰-防回滚审计回归测试 |
| `docs/phase2/MATCHER.md` | **新增** | 字段、约束、公式、审计结论、验收对照 |
| `D3-REPORT.md` | **新增** | 本报告 |

未改：`state_machine`、DB v2、账本、协议注册表、`p2p.py`、`node.py`、`discovery.py` 实现（仅加测试）。

## 10. 已知限制与风险

- 自述硬件无法验证；`VERIFIED` 只是对端声明，不是本项目验证结论。
- `requires_private_data=True` 的任务当前无候选（明确 fail-closed）。
- 无真实基准/带宽/延迟/信誉数据，评分维度保持最小；`idle` 分数没有容量分母，只作粗粒度排序。
- 匹配器不决定租约归属；D4 的 `one_active_lease` 仍是最终裁决者。
- **D6 真实双设备未执行**，不得宣称多设备通过。

## 11. D3 验收对照

| # | 验收项 | 状态 |
| --- | --- | --- |
| 1–4 | 合格入榜 / task_type / runtime / 资源不足 | ✅ 对应测试（见 MATCHER.md §9） |
| 5–9 | 过期 / 撤销 / 不可达 / 缺失字段 / 自述不升级 | ✅ |
| 10–11 | 评分范围 / 每项解释 | ✅ `sum(breakdown)==score` |
| 12–14 | 重复一致 / 顺序无关 / tie-breaker | ✅ |
| 15–17 | 空候选 / 全拒绝有原因 / 无效 requirement | ✅（无效 requirement 抛 `MatcherError`） |
| 18–20 | deadline 边界 / 策略冲突 / 不改入参 | ✅ |
| 全量 | 既有测试不回归 | ✅ 419/419，门禁逐字节不变 |

**结论：D3 满足验收。建议进入 D4（租约协商与执行接线），但不自动开始——等待审计确认。**
