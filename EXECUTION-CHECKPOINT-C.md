# 执行检查点 — 阶段 C（传输层与多节点通信）

日期：2026-10-26（检查点记录于审计修复轮 1 提交后）

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
- 对抗审计（运行 wf_c4b211ee-5e6，4 维度 finders + 默认反驳 verifiers，28 代理）：
  24 项发现 → 21 项确认（3 高 / 8 中 / 10 低）、2 项证伪、1 项默认证伪。
- 修复轮 1（提交 `07e7fc2`）：21 项确认发现全部修复 + 回归测试。
  关键修复：对称默认拒绝（worker/client 的 peers 必须含协调者，配置期即失败）；
  SeqWindow 有界驱逐；seq 持久化 allowlist 门控 + 防御性失败；libp2p BAD_SENDER
  实时生效；parse-error ref_type 钳制（敌意 type 字符串不再产出违反 schema 的 ERROR）；
  协调者对 ERROR 永不回复；已知 claim id 不再铸新租约；同 peer 重复 SUBMIT →
  原 RESULT_ACK 重放（幂等积分），异 peer → 不泄露信息的拒绝；积分导出分页；
  _await 只接受所发消息的回复；SEQ_OUT_OF_WINDOW 自愈（回滚 seq.json 后自动跳变）；
  未绑定流允许 ERROR 先于 HELLO（自愈消息可达等待者）；claim 等待 2×poll+2s +
  零超时晚到清空；backoff 仅健康周期重置；shutdown 先 join 循环线程再关 DB；
  双传输 64 并发流上限。修复后全量回归：**249 测试全绿**。
- 文档：TASK-BOARD.md（C-1/C-3/C-4/D-1/D-2/D-3 已更新证据）、NODE-PROTOCOL.md、
  TWO-DEVICE-TEST.md（标注待完成）、C-REPORT.md。

## 待完成（按依赖顺序）

1. 测试证据日志归档（node E2E 日志）。
2. 双设备验证：按 TWO-DEVICE-TEST.md 执行（未完成前保持"待完成"标记）。
3. 最终交付报告给用户。
4. 记忆文件更新（idle-compute-phase2.md）。

## 硬约束（全程有效）

- GitHub 远端：**暂停**（用户账户尚不存在；不得 add remote/push/pull/fetch）。
- 分支 `phase2`，不 amend/reset/rebase；每轮修复为独立新提交。
- 安全红线（用户指令原文要点）：无任意代码执行；网络默认回环/显式私有地址；
  密钥不进消息/日志/版本库；消息长度/并发/超时/重试/磁盘写入严格受限；
  状态转换必须经现有状态机；积分账本幂等；不得为过测试而降断言。

## 回滚点

- `07e7fc2`（审计修复轮 1，249 绿）为当前安全里程碑。
- 更早回退：`14cc40a`（WAVE 1 全量，244 绿）、`50fb629`（阶段 B 完成）、
  `e3e6d5a`（阶段 A 基线，57 测试）。
- 备份 bundle 与分支信息见 docs/phase2/phase2-A-evidence.md §5。
