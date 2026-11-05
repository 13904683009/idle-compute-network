# D5 报告：能力注册表（本地模拟）+ 传输基准审计

日期：2026-11-05　起点：`f911af3`（D4 审计）
状态：**D5 模块级交付完成；注册表未接入节点/匹配器（路线图 D5 范围为接口+适配器+测试），
真实双设备（D6）未执行。D4 的 PARTIAL/NOT TESTED 项不因 D5 被默认解决。**

## 1. 基线与结果

| 项 | 值 |
| --- | --- |
| 基线 | **459/459 OK，392.662 s**（`logs/idle-compute/d5-baseline-459.log`） |
| 最终 | **479/479 OK，392.175 s**（`logs/idle-compute/d5-final-suite.log`），0 failed / 0 skipped |
| compileall | `compileall -q idle_compute tests benchmarks` → exit 0 |
| 新增测试 | 20（`tests/test_registry.py`） |
| 基准日志 | `logs/idle-compute/d5-transport-bench.log` |

## 2. 交付物

| 文件 | 性质 | 说明 |
| --- | --- | --- |
| `idle_compute/registry.py` | 新增 | `CapabilityRegistry` 接口 + `LocalRegistryAdapter`（独立 SQLite、单写、最终性延迟） |
| `tests/test_registry.py` | 新增 | 20 项：注册/幂等/冲突/回滚/撤销/过期/最终性/持久化/失败注入 |
| `benchmarks/transport_bench.py` | 新增 | loopback 可重复基准（连接、RTT、控制消息、任务端到端、消息大小） |
| `D5-PLAN.md` | 新增 | 范围、依赖、限制、风险、事实/假设/待验证、验收草案 |
| `D5-REGISTRY.md` | 新增 | 接口/语义/持久化/限制/测试对照 |
| `CAPABILITY-PROTOCOL.md` | 追加 | D5 注册表边界（不是链上、不接节点、不改 D2–D4） |
| `D5-REPORT.md` | 新增 | 本报告 |

未改动：`state_machine.py`、`ledger.py`、`db.py` v2 列语义、`node.py`/`coordinator.py` 执行路径、
`protocol.py` 消息注册表、`discovery.py`、`matcher.py`、`lease.py`、`worker.py`。

## 3. 实际调用链（D5）

注册表是**库级能力**，无线上消息：

```
matcher/discovery (D3/D2)  -- 不依赖 -->  registry (D5，未被节点调用)
operator/D6:  LocalRegistryAdapter(path, finality_delay_seconds=...)
  .register(manifest)  -> manifest.verify(now) -> check_manifest_update -> SQLite 写入 -> registry_version+1
  .finalize(now)       -> PENDING -> FINAL
  .revoke(revocation)  -> CapabilityRevocation.verify -> REVOKED
  .verify(manifest)    -> FINAL + hash/version/expiry 校验
```

与 D4 边界：注册表**不参与**租约签发或执行授权；`Worker.execute` 仍只认 `LeaseCommit`。

## 4. 关键设计决策

1. **复用 D1 签名语义**：`register/update` 调 `manifest.verify()`，`revoke` 调
   `CapabilityRevocation.verify()`；版本决策复用 `check_manifest_update`，不新造规则。
2. **只存承诺**：`manifest_hash + 版本/有效期/撤销/状态`；不存原始清单（D0 §7）。
3. **最终性延迟是显式模拟**：`PENDING` 不是授权，`verify()` 拒绝；`finalize()` 推进。
4. **独立 SQLite + 单写者 + 内部锁**；写失败回滚并抛 `PERSIST_FAILED`，版本不增。
5. **不接线**：D5 不新增消息、不改节点路径；节点/匹配器集成留给 D6 并需评审。

## 5. 传输基准（loopback，非跨设备）

环境：Windows 本机、`127.0.0.1`、**1 台主机 / 3 个 Node 对象（真实 TCP）**、
TCP transport、无 NAT/relay/DHT；`cross_device=false`。原始 JSON 在基准日志。

| 指标 | 结果 |
| --- | --- |
| 连接 + HELLO | 11.7 ms（单次） |
| PING/PONG RTT | n=30，min 1.77 / median 2.05 / p95 2.45 / max 17.2 ms |
| `TASK_CREATE→ACK` | n=5，min 2.70 / median 2.96 / p95 3.17 ms |
| 任务端到端（提交→COMPLETED） | n=4，min 1007 / median 1023 / p95 21186 ms |
| 吞吐 | 386.85 pings/s；4 个任务完成 |
| 代表性帧大小 | PING 399 B、HELLO 485 B、CLAIM_REQUEST 454 B、TASK_CREATE 792 B |

**结论（证据驱动）**：loopback 上 RTT/控制延迟均为毫秒级，消息体 <1 KiB；
任务端到端中位数约 1 s，与 worker `poll_interval_seconds=1` 一致，**瓶颈是轮询周期与
重试退避，不是传输框架或序列化**。因此 D5 **不引入**新的队列/重试/分块/传输机制
（无证据支持，且会扩大 D4 安全边界风险）。21 s 的离群值疑为一次重连/退避，
**未定位，列为 D6 待验证项**，不宣称已修复。

## 6. 验收矩阵

| # | 项 | 结论 | 证据 |
| --- | --- | --- | --- |
| R1 | 注册即 FINAL（delay=0） | PASS | `test_register_with_zero_delay_is_final` |
| R2 | 幂等注册不增版本 | PASS | `test_register_is_idempotent` |
| R3 | 版本升级/回滚/同版本冲突 | PASS | `UpdateTests`、`test_duplicate_with_different_content_conflicts` |
| R4 | 撤销（签名/版本级/幂等） | PASS | `RevokeTests` |
| R5 | 过期（清单/记录） | PASS | `test_register_with_expired_manifest_is_rejected`、`test_record_expiry` |
| R6 | 最终性延迟 PENDING→FINAL | PASS | `test_finality_delay_pending_then_final` |
| R7 | 持久化 + 重启 | PASS | `test_reopen_keeps_version_and_record` |
| R8 | 独立文件、不写协调者库 | PASS | `test_registry_uses_its_own_file` |
| R9 | 写失败不返回成功/不增版本 | PASS | `test_write_failure_does_not_bump_version` |
| R10 | 源码/文档门禁 | PASS | 两门禁 diff = 0；`test_source_safety` 通过 |
| B1 | 基准可重复、环境标注 | PASS | `d5-transport-bench.log`（cross_device=false） |
| B2 | 传输优化是否需要 | PASS（结论：不需要） | 第 5 节数据 |
| I1 | 注册表接入节点/匹配器 | NOT TESTED | 路线图 D5 未要求；需 D6 授权 |
| I2 | 注册表撤销联动公告缓存 | NOT TESTED | 未实现（D6 设计项） |
| I3 | 真实双设备 | NOT TESTED | D6，未执行 |
| D4 | D4 遗留 PARTIAL/NOT TESTED | 不变 | 见 `D4-ACCEPTANCE-MATRIX.md`；D5 未触碰租约路径 |

## 7. 已知限制与风险

1. `LocalRegistryAdapter` 是本地模拟：无复制、无共识、无跨节点最终性；`registry_version`
   只是本地计数，**不得称为链上版本**。
2. 只保留每节点当前版本；撤销历史版本会被拒绝（`SCHEMA`）。
3. 未接线到节点/匹配器；当前无生产调用者，也就没有新增的远程攻击面。
4. 基准为 loopback 单机；不能据此推断跨设备带宽/延迟/异构表现。
5. D4 的租约级竞态、节点级错投/广播、worker 重启/断线恢复仍未测试；D5 未改变这些结论。

## 8. 结论

- D5 按路线图交付：注册表接口 + 本地适配器 + 20 项测试 + 基准与文档。
- **不宣称**已实现链上注册表、跨设备性能结论、D5 节点集成或 D6。
- 建议：D6 前先决定注册表是否/如何接入匹配器与撤销联动，并补 D4 遗留竞态测试。
  现在停止，等待审计。
