# 威胁模型

## 资产与信任边界

| 资产 | 说明 |
| --- | --- |
| 宿主机 | 不得被任务执行任意代码、访问任意文件或默认外网 |
| SQLite 状态 | 任务、租约、结果、验证、贡献事件 |
| `ledger.key` | 本地 HMAC 签名密钥；不得输出/提交 |
| 贡献账本 | 防重复计分、可验证、可重建 |
| Peer 身份 | 第一阶段是本地字符串，不是生产身份 |

信任假设：单机、本地 CLI 操作者可信；Python 运行时/SQLite 可信；本地系统时钟
可信但不防篡改。网络、Peer、任务输入和结果内容均视为不可信。

## 威胁与缓解

| 威胁 | 缓解 | 证据/限制 |
| --- | --- | --- |
| 任意代码/Shell | 只允许 `numeric_batch_v1`；无 eval/exec/subprocess/shell | 封闭任务注册表 + 人工审计为主要保障；`test_tasks.py`、`test_source_safety.py`（补充性禁词扫描） |
| 任务输入注入 | 严格 schema；只接受 numbers 列表；拒绝 bool/float/string/额外字段 | `test_tasks.py` |
| 超大输入 DoS | 1..4096 个数；绝对值 ≤1e9 | `test_tasks.py` |
| 宿主机文件读取 | 任务 payload 没有路径字段；只读写 DB/key | 架构审查；本地 CLI 可指定 DB/key，属本地管理员权限 |
| SQL 注入 | 全部 SQL 使用参数化查询 | 代码审查 |
| 结果伪造 | 协调器独立重算；内容哈希；HMAC 签名 | `test_wrong_output_rejected`、`test_invalid_signature_rejected` |
| 重放结果 | 签名覆盖 task/lease/peer/result_hash；结果唯一约束 | `test_duplicate_submission_rejected` |
| 重复领取 | SQLite `BEGIN IMMEDIATE` + 单活跃租约部分唯一索引 | `test_duplicate_claim_does_not_create_second_active_lease` |
| 重复计分 | `credit_events.idempotency_key` 唯一 | `test_duplicate_credit_is_idempotent`、`test_happy_path_and_credit_once` |
| 过期租约 | 租约时间检查；过期结果拒绝；未超次数回队，否则 EXPIRED；SUBMITTED 崩溃窗口同样回队 | `test_lease_expiry_retries_then_expires`、`test_expired_lease_rejects_result_without_credit`、`test_submitted_task_with_expired_lease_returns_to_queue`、`test_submitted_task_with_expired_lease_expires_after_max_attempts` |
| 进程中断 | WAL；重启恢复；active lease 可过期重试 | `test_restart_restores_state_and_key` |
| 账本篡改 | 事件哈希链 + HMAC；验证/重建 | `test_tampered_signature_fails_verification`、`test_broken_prev_hash_fails_verification`、`test_tampered_amount_fails_verification`、`test_deleted_middle_event_fails_verification` |
| 密钥泄露 | 密钥文件在 `var/`，gitignore；CLI 不输出；创建时 Windows 上显式警告 ACL 限制 | `test_windows_creation_warns_about_acl_limitation`；Windows 无 0600 语义，机密性依赖目录 ACL |
| P2P Sybil/女巫 | **未实现**；当前没有网络层 | 明确标记 |
| 网络窃听/中间人 | **未实现**；无网络传输 | 明确标记 |
| OS 级逃逸 | 无 OS 沙箱；依赖白名单，不是任意代码执行环境 | 威胁模型明确 |
| 账本尾部删除 | 有链但无外部锚定；本地管理员可删尾 | 剩余风险；`test_tail_deletion_is_not_detected_without_external_anchor` 显式记录该限制 |
| 时钟操纵 | 本地时钟；无 NTP/可信时间 | 剩余风险 |
| HMAC 对称性 | Peer/协调器共享密钥；无不可否认性 | 生产需 Ed25519 |

## 设计假设

1. 第一阶段只在本机运行，不连接真实 Peer。
2. 本地 CLI 操作者是管理员，可指定 DB/key 路径；任务本身不能指定路径。
3. HMAC 是本地完整性机制，不是生产级身份或不可否认签名。
4. 没有代币/支付；CreditEvent 只是内部计数事件。
5. 失败的任务可能被拒绝；MVP 不自动重试错误结果，租约过期才重试。
6. SQLite 是本地单写者事务存储；不是分布式共识。

## 剩余风险

- HMAC 密钥泄露可伪造结果/账本；生产需非对称密钥、密钥轮换、硬件保护。
- Windows 上 `0o600` 权限无效，密钥继承目录 ACL；多用户机器上其他本地用户
  可能读取 `ledger.key`。创建时会警告；第二阶段需 DPAPI/CNG 或 icacls 加固。
- 本地管理员可修改数据库和密钥；没有外部审计锚点。
- 没有 OS 沙箱。若白名单/解析实现出现漏洞，理论上可影响宿主；需要持续测试和审计。
- 没有速率限制、配额、反女巫；在接入网络前必须补齐。
- 没有备份/恢复工具；生产前必须增加 DB 备份和灾难恢复。
- 本地时钟可被调整，租约过期语义受影响。
- P2P 协议只有接口，没有真实传输/加密/身份；不得把本阶段当成多设备已实现。
