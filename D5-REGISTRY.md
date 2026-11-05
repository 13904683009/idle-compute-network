# D5 注册表（本地模拟）— 设计与边界

实现：`idle_compute/registry.py`　测试：`tests/test_registry.py`（20）
范围依据：D0 路线图「D5 注册表 | 接口 + 本地适配器」。**不是链上注册表，也不是共识系统。**

## 1. 接口

```python
class CapabilityRegistry(Protocol):
    def register(self, manifest: CapabilityManifest, *, now=None) -> RegistryRecord
    def update(self, manifest, *, now=None) -> RegistryRecord
    def revoke(self, revocation: CapabilityRevocation, *, now=None) -> RegistryRecord
    def get(self, node_id, *, now=None) -> RegistryRecord | None
    def verify(self, manifest, *, now=None) -> RegistryRecord
    def finalize(self, *, now=None) -> int
    def registry_version(self) -> int
```

`RegistryRecord` 只保存承诺与元数据：
`node_id, capability_version, manifest_hash, status(PENDING/FINAL/REVOKED/EXPIRED),
registered_at, finalize_at, finalized_at, expires_at, revoked_reason`。

**不保存原始清单**：清单走 P2P 公告，注册表按 `manifest_hash` 核对（D0 §7）。

## 2. 语义

| 操作 | 规则 |
| --- | --- |
| `register` | 验签 + 时间检查；未知节点→写入；同版本同哈希→幂等（不增版本）；更高版本→更新；更低→`VERSION_ROLLBACK`；同版本异哈希→`VERSION_CONFLICT`；已撤销→`REVOKED` |
| `update` | 必须已注册；版本必须递增（同版本同哈希幂等、同版本异哈希冲突、降低回滚） |
| `revoke` | 复用 D1 `CapabilityRevocation`（签名验证）；只能撤销**当前**版本；版本级生效、幂等 |
| `get` | 记录过期时返回 `EXPIRED`；未知返回 `None` |
| `verify` | 只接受 `FINAL`；`PENDING`→`NOT_FINAL`；过期→`EXPIRED`；撤销→`REVOKED`；版本/哈希不符→`SCHEMA`/`SIGNATURE` |
| `finalize` | PENDING 且 `finalize_at <= now` → FINAL；返回转换条数 |
| `registry_version` | 本地单调计数，每次成功写入 +1（幂等读写不增） |

最终性延迟：`finality_delay_seconds=0`（默认）时写入即 FINAL；>0 时先 PENDING，
`finalize()` 推进。**PENDING 不是可用授权**（`verify` 拒绝）。

## 3. 持久化与并发

- 独立 SQLite 文件（默认建议 `<data_dir>/registry.db`），不与协调者库共用，不复用
  `credit_events`（ADR-D0-15）。
- `check_same_thread=False` + 内部锁；写失败回滚并抛 `PERSIST_FAILED`，**不增版本**。
- 重启后记录与 `registry_version` 保留。

## 4. 已知限制

1. 本地模拟：没有复制、没有共识、没有跨节点最终性；`registry_version` 只是本地计数。
2. 只保留每节点**当前**版本，撤销历史旧版本会被拒绝（需要历史时另行设计）。
3. 未接入节点/匹配器：D5 不新增线上消息，注册表是库级能力；D6 集成需另行授权。
4. 不替代公告缓存：公告表达实时状态，注册表表达可验证承诺（D0 §6）。
5. 不宣称"链上"、不提供代币/支付；无 DHT/Gossip/Relay/mDNS。

## 5. 测试对照（20 项）

| 规则 | 测试 |
| --- | --- |
| 注册即 FINAL | `test_register_with_zero_delay_is_final` |
| 幂等注册不增版本 | `test_register_is_idempotent` |
| 同版本异哈希冲突 | `test_duplicate_with_different_content_conflicts` |
| 更高版本注册=更新 | `test_register_with_future_version_after_existing_is_an_update` |
| 过期清单拒绝 | `test_register_with_expired_manifest_is_rejected` |
| 更新/回滚/同版本幂等/冲突/未注册 | `UpdateTests`（5 项） |
| 撤销/幂等/旧版本拒绝/坏签名 | `RevokeTests`（4 项） |
| PENDING→FINAL、记录过期、非法延迟 | `FinalityAndExpiryTests`（3 项） |
| 重开保留、独立文件、写失败不增版本 | `PersistenceTests`（3 项） |
