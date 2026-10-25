# 第一阶段状态

## 已实现

- 独立 Git 仓库；`main` 基线、`dev` 开发分支。
- 数据模型：Peer、Task、TaskShard、Lease、Result、Validation、CreditEvent。
- 有界状态机与 SQLite 持久化。
- 唯一任务 ID、租约过期、最多 3 次尝试、重复领取/提交防护。
- 白名单 `numeric_batch_v1` 确定性计算；禁止任意 Shell/代码。
- 结果独立重算、内容哈希、HMAC 签名验证。
- 哈希链 + HMAC + 幂等键贡献账本，可验证、可重建。
- CLI：init、create-task、claim、run、status、events、cancel、expire、
  verify-ledger、rebuild-ledger、balances。
- 57 项标准库测试：正常路径、重复领取/提交/计分、租约过期/重试（含 SUBMITTED
  崩溃窗口恢复）、错误结果、错误签名、取消、重启恢复、账本篡改（prev_hash/
  金额/中间删除/尾部删除限制/event_type/NULL 篡改 + CLI 端到端结构化报错）、
  损坏 DB 结构化报错与失败初始化句柄释放、结果表 NULL 签名/哈希守卫、
  单活跃租约唯一索引直测（含 UNIQUE 来源断言）、
  密钥管理（幂等/半截密钥/Windows ACL 警告/创建竞争重试）、recover 直接测试、
  租约验证后 RELEASED、CLI、P2P 边界、源码安全扫描。
- 显式 P2P 接口；网络层未实现并显式抛错。

## 未实现

- 真实 libp2p/TCP/QUIC、DHT、NAT 穿透、跨设备发现。
- 生产级 Peer 身份/非对称签名/密钥轮换。
- 多分片调度/共识/Sybil 防护/信誉。
- 生产限流、监控、DB 备份/灾难恢复、Web/远程 API。
- OS 级沙箱。

## 设计假设

- 单机、本地可信操作者、本地 SQLite、本地时钟。
- HMAC 作为第一阶段本地完整性机制，不是生产身份。
- 单分片执行；`TaskShard` 为后续预留。
- 无代币、无真实支付。

## 剩余风险

- HMAC 密钥泄露可伪造结果/账本；需 Ed25519 + 密钥管理。
- 本地管理员可改 DB/key；无外部审计锚点。
- 无 OS 沙箱；安全依赖白名单实现正确。
- 无速率限制/反女巫；接入网络前必须补齐。
- 无备份工具；生产前必须增加。
- 租约依赖本地时钟。

## 下一阶段建议

1. 引入 libp2p 传输/身份（Ed25519）和消息加密。
2. 多分片调度、任务分配、结果聚合。
3. 速率限制、配额、反 Sybil、信誉。
4. 账本外部锚定/审计导出、DB 备份与恢复。
5. 浏览器/数据分析等新任务类型必须独立沙箱，不能直接复用本执行器。
