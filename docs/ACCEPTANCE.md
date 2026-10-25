# 验收标准与证据

## 验收矩阵

| 验收标准 | 实现 | 证据 |
| --- | --- | --- |
| 单机创建、执行、验证、状态持久化、贡献事件 | 是 | `test_happy_path_and_credit_once`、`test_end_to_end_cli`、`test_restart_restores_state_and_key` |
| 重复提交不能重复计分 | 是 | `test_duplicate_submission_rejected`、`test_happy_path_and_credit_once`、`test_duplicate_credit_is_idempotent`；单活跃租约唯一索引有直测 `test_second_active_lease_insert_is_rejected_by_index` |
| 无效签名被拒绝 | 是 | `test_invalid_signature_rejected`、`test_tampered_signature_fails_verification`、`test_null_signature_and_hash_treated_as_invalid_signature` |
| 过期租约被拒绝/重试 | 是 | `test_lease_expiry_retries_then_expires`、`test_expired_lease_rejects_result_without_credit` |
| 错误结果被拒绝 | 是 | `test_wrong_output_rejected` |
| 进程重启恢复本地状态 | 是 | `test_restart_restores_state_and_key` |
| 正常 + 主要故障路径测试 | 是 | 57 项 unittest；见 `python -m unittest discover -s tests -v` |
| 文档标记已实现/未实现/假设/风险 | 是 | `README.md`、`ARCHITECTURE.md`、`PROTOCOL.md`、`THREAT_MODEL.md`、`STATUS.md` |
| Auditor 基于真实代码/测试给出结论 | 是（conditional，0 blocker/major） | 6 轮独立只读多 Agent 审计（finder + 每发现 2 对抗验证者 + 脚本内确定性裁决），基于实际 staged diff 与真实测试日志；第 6 轮最终裁决 conditional、2 个 minor（均 2/2 确认）。该 2 个 minor 已按验证者处方修复并有回归测试覆盖（57 项全过），但修复本身未再经独立审计——详见最终报告 |

## 运行命令

```powershell
cd C:\Users\linxi\AI-Work\workspace\idle-compute-network
python -m unittest discover -s tests -v
python -m compileall -q .
```

## 当前明确未验证

- 多设备 / 真实 P2P / libp2p / NAT / DHT。
- 非对称 Peer 身份、生产密钥管理、不可否认签名。
- 多分片调度与分布式共识。
- 生产限流、Sybil 防护、监控、备份/灾难恢复。
- 操作系统级沙箱。
- 真实资金/代币（明确不在范围内）。

## 验收判定

只有在审计 Agent 基于实际 diff 和测试日志给出 `pass` 或 `conditional`，且没有
`blocker/major` 发现时，才可提交。若审计失败，修复最多 3 轮；仍失败则保留证据并停止。
