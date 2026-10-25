# 执行检查点 — 阶段 C（传输层与多节点通信）

日期：2026-10-26（检查点记录于 WAVE 1 提交后、WAVE 2 审计进行中）

## 已完成

- WAVE 0（提交 `269e1cf`）：`protocol.py`（信封/帧编解码/验证管线/14 消息类型/16 错误码）、
  `net_transport.py`（libp2p 适配器）、`transport_tcp.py`（纯标准库 TCP 回退）、`config.py`
  （强校验配置）；227 测试绿（tests/test_protocol.py 46、tests/test_config.py 41、
  transport 双传输测试等）。
- WAVE 1（提交 `14cc40a`）：`node.py` 运行时（协调者 actor / 工人拉取循环 / client 一次性操作；
  双向 HELLO 流绑定；TASK_CREATE/CLAIM/SUBMIT/STATUS/PING 线上处理；ledger_copy 镜像验账；
  seq/租约持久化与重启恢复）；`cli.py` 新增 `node run/create-task/status/ping`；
  `db.py` submit_result 重复检查前移；tests/test_node.py 15 项真实多进程 E2E。
- 全量回归：**244 测试全绿**（含 Phase 1 遗留 115 项，证明 db.py 重排安全）；compileall 通过。
- 文档：TASK-BOARD.md（C-1/C-3/C-4/D-1/D-2/D-3 已更新证据）、NODE-PROTOCOL.md、
  TWO-DEVICE-TEST.md（标注待完成）。

## 进行中

- WAVE 2 对抗审计工作流（任务 wet57nhkt / 运行 wf_c4b211ee-5e6，4 维度 finders +
  默认反驳的 adversarial verifiers）。产出处理后进入 ≤3 轮有界修复。

## 待完成（按依赖顺序）

1. 审计工作流完成 → 处理确认发现 → 修复轮（每轮后全量回归）→ 新提交（不 amend）。
2. 测试证据日志归档（node E2E 日志）。
3. `docs/phase2/C-REPORT.md`（交付报告：实现概览/架构协议/变更文件/逐测试结果/
   244 含 115 旧测试/性能与安全上限/遗留风险/恢复步骤/阶段 D 建议）。
4. 双设备验证：按 TWO-DEVICE-TEST.md 执行（未完成前保持"待完成"标记）。
5. 最终交付报告给用户。
6. 记忆文件更新（idle-compute-phase2.md）。

## 硬约束（全程有效）

- GitHub 远端：**暂停**（用户账户尚不存在；不得 add remote/push/pull/fetch）。
- 分支 `phase2`，不 amend/reset/rebase；每轮修复为独立新提交。
- 安全红线（用户指令原文要点）：无任意代码执行；网络默认回环/显式私有地址；
  密钥不进消息/日志/版本库；消息长度/并发/超时/重试/磁盘写入严格受限；
  状态转换必须经现有状态机；积分账本幂等；不得为过测试而降断言。

## 回滚点

- `14cc40a`（WAVE 1 全量，244 绿）为当前安全里程碑。
- 更早回退：`50fb629`（阶段 B 完成）、`e3e6d5a`（阶段 A 基线，57 测试）。
- 备份 bundle 与分支信息见 docs/phase2/phase2-A-evidence.md §5。
