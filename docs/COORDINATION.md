# 协调模型与诚实边界（Phase 2）

本文回答一个问题：**这套东西到底"去中心化"到什么程度？**
结论先行：**协调者是唯一权威；工人能独立验证，但不能参与共识。**
哈希链提供的是**防篡改证据**，不是共识机制。任何"去中心化计算网络""多设备已验证"
的表述，在 Phase 2 都不成立。

## 1. 三类能力（逐条对照代码）

| 类别 | 内容 | 代码/证据 |
| --- | --- | --- |
| **协调者权威**（唯一写入者） | 持有权威 SQLite 库；发布任务；仲裁领取（`one_active_lease` 部分唯一索引）；独立重算并验证结果；签发积分（`credit-v2` 签名）；扩展哈希链；持有租约时钟（`expires_at` 由协调者计算下发） | `idle_compute/db.py`（Store 唯一状态机写者）、`node.py: _handle_claim_request / _handle_submit_result / _handle_ledger_sync_request`、`ledger.append_credit` |
| **工人可验证**（可独立复核） | 逐事件校验哈希链（`prev_hash` 链接、`event_hash` 重算）、Ed25519 签名（内嵌于 `node_id`，无需分发公钥）、金额 > 0、`seq` 连续、幂等键唯一；据此重建自己的余额视图；结果正确性由协调者独立重算，不依赖工人自报 | `ledger.verify_and_append`、`ledger.verify_ledger`、`ledger.rebuild_ledger`、`identity.NodeIdentity.verify_signature` |
| **明确非去中心化**（不实现、不宣称） | 无共识算法、无 DHT、无 mDNS/UPnP 发现、无 NAT 穿透、无反 Sybil、无代币/支付；协调者是**单点故障**（宕机 = 全网络停止领取与记账） | 本文；`config.py` 静态 `peers` 白名单；`tests/test_source_safety.py` 与 `tests/test_docs.py` 的禁词/断言扫描 |

## 2. 单写者 + 协调者签名的哈希链 = 防篡改证据，不是共识

- 账本由协调者单线程追加（actor 模型），每个事件 `prev_hash` 指向前一事件 `event_hash`，
  链首为 `GENESIS_HASH`。这是**追加式日志**，不是多副本投票。
- 工人保留 `ledger_copy` 镜像，逐事件验签后才落库；镜像与链分歧时**停住并报警**，
  既不回卷也不分叉——它没有"另一个真相"可以切换过去。
- 因此：**能证明"谁在什么时候记了什么、有没有被改过"，不能证明"这条链是全网共识的"。**

## 3. 已知的、必须承认的缺口

| 缺口 | 后果 | 现状 |
| --- | --- | --- |
| 尾删无外部锚点 | 拥有协调者主机权限的人删掉最后 N 条事件后，剩余前缀仍是合法链，工人无法察觉 | **未解决**（Phase 1 遗留，`test_tail_deletion_is_not_detected_without_external_anchor` 显式记录）；需要外部锚定/审计导出 |
| 协调者可否认/替换历史 | 若同时改 DB + 密钥，可重写整条链 | 未解决；Ed25519 只保证"同一密钥下的连续性" |
| 无共识/无多副本 | 协调者磁盘损坏 = 账本丢失（工人镜像不含全部历史时） | 建议运维侧定期导出账本快照（未内置） |
| 无速率限制 | 白名单内恶意节点可高频发消息 | **F-1 计划中**，当前未实现 |
| 无跨设备验证 | 跨机、跨 NAT、时钟偏差行为未知 | `TWO-DEVICE-TEST.md` 标注**待完成**，从未在两台物理设备上执行 |

## 4. 冲突处置（运维视角）

工人侧一旦锁存冲突（`_ledger_conflict`），就会停止领取任务，直到某一轮同步完全通过。

```powershell
# 看镜像头、已验事件数、锁存原因与出错 seq、claim 是否被阻断
.venv\Scripts\python.exe -m idle_compute worker ledger status --config var\node\worker.json
```

输出示例（篡改场景）：

```json
{
  "coordinator_node_id": "12D3KooW...",
  "mirror_head": 2,
  "events": 2,
  "verified": 2,
  "conflict": {"at": "2026-11-04T09:00:00+00:00", "reason": "event evt_...: prev_hash linkage broken", "seq": 3},
  "claim_blocked": true
}
```

处置原则：

1. **不要**删工人的 `ledger_copy.db` 来"消掉"冲突——那只是把分歧藏起来，不是修复。
2. 先判断是"协调者链真的坏了"还是"工人在重审时读到了旧数据"：核对协调者
   `credit_events` 的 `event_hash`/`prev_hash` 与工人镜像同一 `seq` 是否一致。
3. 确认协调者链不可信时，恢复协调者库到已知良好备份（外部快照），再让工人重启重审；
   工人会从 `seq 0` 重新验证并在clean 后自动恢复领取（无需人工清锁）。
4. 若确认是工人镜像被本地篡改，删除 `ledger_copy.db`（派生数据）后重启即可全量重建。

## 5. 与"真正的去中心化"的差距（不承诺路线时间）

要让"去中心化"这个词成立，至少需要：多点写入或多副本共识、Sybil 抵抗、独立验证者
可审计的外部锚点、无单点故障的租约/任务仲裁。Phase 2 全部没有。后续若要推进，建议
按 `docs/phase2/TASK-BOARD.md` 的 E/F/G 顺序补齐可验证性，再讨论共识——**在没有外部
锚点之前，不要宣称任何"共识"。**
