# 能力匹配器（Capability Matcher，D3）

实现：`idle_compute/matcher.py`　测试：`tests/test_matcher.py`（31 项）
状态：**匹配器已完成；D4 租约、D5 注册表、D6 双设备未开始。**

---

## 1. 职责与纯度

```python
match(requirement: TaskRequirement,
      candidates: Iterable[Candidate],
      *,
      now: int,
      verify_signatures: bool = True) -> MatchResult
```

- 纯函数：不读数据库、不访问网络、不启动子进程、不修改入参、不写全局状态。
- `now` 必须显式传入（int）；不使用 `time.time()`、随机数或字典插入顺序。
- 相同 `(requirement, candidates, now, verify_signatures)` → 完全相同的结果（`MatchResult` 可比较）。
- 排序完全确定：`(-score, node_id)`；输入顺序不影响输出。

`Candidate` 是适配层（`node_id + manifest + announcement + reachable + revoked`），
`Candidate.from_cache_entry(entry)` 可接 `discovery.CachedPeer`（鸭子类型，不 import DB）。

## 2. 使用的真实字段（以代码为准）

| 来源 | 字段 | 语义 |
| --- | --- | --- |
| `TaskRequirement` | `task_type`, `required_runtime` | 必须是白名单任务类型 / 指定运行时（二者都非空） |
| | `minimum_resources{cpu_cores,ram_mb,gpu_memory_mb}` | 硬下限（cpu ≥1，其余 ≥0） |
| | `constraints{max_execution_seconds,max_input_bytes,max_output_bytes}` | 执行时长与 I/O 上限 |
| | `data_policy{requires_private_data,allow_relay}` | 数据/中继策略 |
| | `verification_policy{mode,required}` | 验证要求（`mode` 当前只有 `RECOMPUTE`） |
| | `preferred_resources{gpu_vendor,gpu_compute_profile}` | 偏好，不参与硬过滤（GPU 厂商参与评分） |
| | `deadline` | Unix 秒 |
| `CapabilityManifest` | `supported_task_types`, `supported_runtimes` | 明确声明的支持集合 |
| | `hardware{cpu_arch,cpu_cores,ram_mb,gpu_vendor,gpu_memory_mb}`（后四项可缺省） | 自述硬件 |
| | `resource_limits{max_cpu_cores,max_ram_mb,max_gpu_memory_mb,max_task_duration_seconds}` | 自述可提供上限 |
| | `network{supported_transports,supports_relay}` | 传输/中继能力 |
| | `verification{level,benchmark_profile}` | 证据等级声明（`SELF_DECLARED/BENCHMARKED/VERIFIED`） |
| | `expires_at`, `signature` | 有效期 / Ed25519 |
| `CapabilityAnnouncement` | `available_resources{cpu_cores,ram_mb,gpu_memory_mb}` | 当前可用资源（自述，必填） |
| | `queue{running_tasks,queued_tasks}` | 当前队列（自述） |
| | `network_status{connectivity,supported_transports}` | `DIRECT/RELAY/UNKNOWN` |
| | `accepting_tasks`, `expires_at`, `signature` | 是否接单 / 有效期 / 签名 |

发现层提供的值：`reachable`（`offline_after_seconds` 内有已验签帧）、`revoked`（版本级撤销）。

**模型现状（重要）**：没有 benchmark 分数、带宽、RTT、历史成功率、信誉或硬件证明字段。
因此匹配器**不包含**这些维度，也不把 `level` 之外的任何东西当作证据。

## 3. 硬约束（先于评分，全部失败原因都收集）

| 代码 | 触发条件 | 说明 |
| --- | --- | --- |
| `INVALID_CANDIDATE` | 对象缺少 manifest/announcement | 单候选拒绝，不影响其余 |
| `NODE_ID_MISMATCH` | `candidate.node_id ≠ manifest.node_id ≠ announcement.node_id` | 绑定错误 |
| `SIGNATURE_INVALID` | manifest/announcement 验签失败 | `verify_signatures=True` 时 |
| `MALFORMED_CAPABILITY` | 结构/字段非法 | 不抛异常，按候选拒绝 |
| `MANIFEST_EXPIRED` / `ANNOUNCEMENT_EXPIRED` | `expires_at <= now` | 各自独立检查 |
| `CAPABILITY_REVOKED` | `candidate.revoked` | 版本级撤销 |
| `NODE_UNREACHABLE` | `candidate.reachable == False` | **公告有效 ≠ 当前在线** |
| `DEADLINE_PASSED` | `requirement.deadline <= now` | 边界：等于即失败 |
| `TASK_TYPE_UNSUPPORTED` | `task_type ∉ supported_task_types` | 必须显式声明 |
| `RUNTIME_MISMATCH` | `required_runtime ∉ supported_runtimes` | 缺省不通过 |
| `RESOURCE_UNSPECIFIED` | `available_resources` 缺键 | **未声明 ≠ 满足** |
| `RESOURCE_INSUFFICIENT` | `min(announced, manifest limit) < required` | 自报值还要受自身 limit 约束 |
| `DURATION_LIMIT_EXCEEDED` | `max_execution_seconds > max_task_duration_seconds` | 真实字段 |
| `NOT_ACCEPTING` | `accepting_tasks == False` | 明确不接单 |
| `RELAY_NOT_ALLOWED` | `allow_relay=False` 且 `connectivity == RELAY` | 保守 |
| `DATA_POLICY_UNPROVEN` | `requires_private_data == True` | **当前模型无法证明，全部 fail-closed** |
| `VERIFICATION_UNPROVEN` | `verification.required == True` 且 `level == SELF_DECLARED` | 不自动升级 |

拒绝原因去重、按检查顺序稳定；每条都带可读 `detail`（例如
`RESOURCE_INSUFFICIENT: cpu_cores: available 2 (min(announced 100, limit 4)) < required 8`）。

## 4. 评分（仅通过硬约束的候选）

整数计算，总范围 **0..100**，`sum(breakdown) == score`：

| 维度 | 分值 | 规则 | 为什么不更多 |
| --- | --- | --- | --- |
| `headroom` | 0..40 | 取所需维度中 `available/required` 的最小百分比；100% 得 0，200% 及以上得 40（线性） | 没有真实基准，只能奖励"余量" |
| `trust` | 0..25 | `SELF_DECLARED=0`，`BENCHMARKED=15`，`VERIFIED=25` | 只反映**声明**等级，不代表真实性能 |
| `idle` | 0..20 | `running+queued` 为 0→20，1→15，2→10，≥3→0 | 没有容量字段，不臆造"利用率" |
| `connectivity` | 0..5 | `DIRECT=5`，`RELAY=2`，`UNKNOWN=0` | 可达性已是硬约束，这里只是小权重 |
| `preference` | 0..10 | 仅当 requirement 指定 `gpu_vendor` **且** manifest 声明了同款厂商 | 缺失得 0，不奖励"没声明" |

明确不做的事：
- 不把 `SELF_DECLARED` 算成 benchmark；`BENCHMARKED` 也只是"对方声明有基准证据"，没有基准数据可比。
- 不重复计算可达性（它只做硬过滤，不进评分）。
- 不引入带宽/延迟/信誉/历史成功率（仓库没有这些字段）。

## 5. 解释输出

`MatchResult.as_dict()` 给出：候选数/通过数/拒绝数、排名（node_id、score、逐维 breakdown、
trust_level、`self_declared_fields` 列表）、拒绝列表（codes + details）。
`result.explain(node_id)` 返回单个 `CandidateEvaluation`；
`evaluation.scores()` 返回逐维分值，`admitted`/`rejection_codes`/`rejection_details` 说明原因。

`self_declared_fields` 明确列出"来自对端自述"的字段，调用方不得把它们当作已验证事实。

## 6. 与 D2 发现层的边界

- 匹配器不查询缓存、不做淘汰、不写 `discovery.db`；调用方把 `lookup()` 的结果包成
  `Candidate`（`from_cache_entry`）即可。
- `reachable` 与 `revoked` 由发现层给出；匹配器只消费这两个布尔值。
- 本阶段不产生租约、不选任务、不写任何状态（D4 才做）。

## 7. D3 审计结论

### 7.1 D2 淘汰与防回滚是否分离

**结论：已分离，无缺陷。** 证据：
- `discovery.py` 的淘汰只执行 `self._entries.pop(victim.node_id)`；`_last_sequences` 与
  `discovery.db` 的 `peer_announcement_state` 不受影响；`ingest` 计算
  `last_sequence = max(内存, 持久化)`。
- 新增回归测试：
  - `tests/test_discovery.py::EvictionAuditTests::test_eviction_does_not_reset_anti_rollback_state`
    （淘汰后重放旧 seq → `SEQ_ROLLBACK`，且 `store.last_sequence == 5`）；
  - `...::test_eviction_then_restart_still_rejects_the_old_sequence`（淘汰 + 重启后仍拒绝）。
- 未改动 discovery 代码（仅新增测试）。

### 7.2 `test_abuse.py` 的 `ignore_cleanup_errors=True`

- 变更范围：仅 `TemporaryDirectory(..., ignore_cleanup_errors=True)` 一行；所有功能断言未动。
- 可能掩盖的：**Windows 下进程退出后临时目录删除的瞬时句柄占用**（`WinError 32`）。
- 不可能掩盖的：进程未退出——清理仍执行 `proc.terminate()` + `proc.wait(10.0)`，且测试内
  `assertIsNone(proc.poll())` 仍要求节点存活；`wait` 超时会让用例报错。
- 残余风险：若将来出现"进程已退出但长期持锁"的真实泄漏，该用例不会在清理阶段报错；
  这一点记录在此，D6 双设备环境应保留人工检查（任务管理器/日志）。

## 8. 已知限制

- 无法验证自述硬件；`VERIFIED` 只是对端声明，不是本项目的验证结论。
- `requires_private_data=True` 的任务在当前模型下**没有候选**（明确 fail-closed）。
- 无真实基准/带宽/延迟/信誉数据，评分维度刻意保持最小。
- 匹配器不决定租约归属（D4 的 `one_active_lease` 仍是最终裁决者）。
- D6 真实双设备未执行。

## 9. 测试对照（D3 验收 20 项）

| # | 验收项 | 测试 |
| --- | --- | --- |
| 1 | 满足全部硬约束进入排名 | `test_fully_satisfied_candidate_is_ranked` |
| 2 | task_type 不支持被过滤 | `test_unsupported_task_type_is_rejected` |
| 3 | runtime 不匹配被过滤 | `test_runtime_mismatch_is_rejected` |
| 4 | 资源不足被过滤 | `test_resource_shortage_is_rejected`、`test_manifest_limit_caps_the_announced_resources` |
| 5 | 过期公告被过滤 | `test_expired_announcement_is_rejected`、`test_expired_manifest_is_rejected` |
| 6 | 撤销被过滤 | `test_revoked_candidate_is_rejected` |
| 7 | 不可达不被误认为可用 | `test_unreachable_candidate_is_not_treated_as_available` |
| 8 | 缺失资源字段不默认满足 | `test_missing_resource_field_does_not_satisfy_requirement` |
| 9 | SELF_DECLARED 不自动升级 | `test_self_declared_is_not_upgraded_to_verified` |
| 10 | 评分范围符合文档 | `test_score_range_and_breakdown_sum` |
| 11 | 每项评分都有解释 | `test_scores_are_explainable` |
| 12 | 重复运行完全一致 | `test_repeated_run_is_identical` |
| 13 | 输入顺序不影响排名 | `test_input_order_does_not_change_ranking` |
| 14 | 平分稳定 tie-breaker | `test_tie_breaker_is_node_id` |
| 15 | 空候选集明确结果 | `test_empty_candidate_set` |
| 16 | 全被过滤时每个都有原因 | `test_all_rejected_have_reasons` |
| 17 | 无效 requirement 不含糊 | `test_invalid_requirement_raises`、`test_now_must_be_an_int` |
| 18 | deadline 边界正确 | `test_deadline_boundary` |
| 19 | 数据/验证策略冲突拒绝 | `test_private_data_fails_closed`、`test_relay_is_refused_when_the_requirement_forbids_it`、`test_self_declared_is_not_upgraded_to_verified` |
| 20 | 不修改输入对象 | `test_does_not_mutate_inputs` |
