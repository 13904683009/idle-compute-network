# NodeRegistry 原型（Anvil 本地链 / Base Sepolia 脚本）

> **原型，不是生产实现。**
> **当前状态：Anvil 原型已跑通；Base Sepolia 尚未部署**（部署地址测试币余额为 0，
> 未获得 faucet 资金）。无代币、无转账、无 escrow；`NodeRegistry.sol` 未审计。

## 1. 合约

- 源码：`contracts/NodeRegistry.sol`
- 编译产物：`contracts/NodeRegistry.abi.json`、`contracts/NodeRegistry.bin`
- 功能：
  - `register(bytes32 nodeKey, uint8 role, bytes32 tlsFingerprint, bytes32 capabilityHash, bytes32 endpointHash)`
  - `get(bytes32)`、`listActive()`、`activeCount()`、`activeAt(uint256)`、`active(bytes32)`
  - `NodeRegistered` / `NodeDeactivated` 事件
  - 非 payable；无 ERC-20/721、无 transfer/escrow

**本机 Anvil 合约地址（本次演示）：**

```text
0x5fbdb2315678afecb367f032d93f642f64180aa3
```

> 这是 Anvil 默认账户在全新链上的确定性部署地址；Anvil 重启/清空后状态与地址不再保留。
> 它不是 Base Sepolia 地址。

## 2. 启动本地链

```bash
anvil --host 127.0.0.1 --port 8545 --chain-id 31337
```

（Anvil 安装：Foundry；仅测试网/本地使用。）

## 3. 部署

```bash
python scripts/deploy_node_registry.py --rpc http://127.0.0.1:8545
# 输出 contract_address；记录该地址
```

脚本使用 Anvil 的 unlocked 账户（`eth_accounts` + `eth_sendTransaction`），不处理私钥；
只适用于本地/测试链，**不适用于 Base Sepolia**。

## 3a. Base Sepolia 部署（脚本已就绪，尚未实际上链）

### 获取测试币

使用任意 Base Sepolia faucet（需要登录/账号，按各站点要求）：

- Alchemy: <https://www.alchemy.com/faucets/base-sepolia>
- QuickNode: <https://faucet.quicknode.com/base/sepolia>
- Coinbase CDP faucet: <https://portal.cdp.coinbase.com/products/faucet>

只需少量测试 ETH（例如 0.01–0.05）用于部署与几次注册。**只使用测试网私钥**，
不要使用主网私钥。

### 部署（私钥签名）

```bash
pip install "web3>=7"
# 私钥写入文件，权限 600；或使用环境变量 BASE_SEPOLIA_PRIVATE_KEY
python scripts/deploy_node_registry_signed.py \
  --rpc https://sepolia.base.org \
  --chain-id 84532 \
  --private-key-file ~/.base-sepolia.key
# 先 dry-run（不发交易）：
python scripts/deploy_node_registry_signed.py ... --dry-run
```

脚本会校验 RPC chain id、打印部署地址余额、用 EIP-1559 签名交易、等待 receipt，
并输出：

```json
{"contract_address": "0x...", "tx_hash": "0x...", "block": 123, "explorer": "https://sepolia.basescan.org/address/0x..."}
```

私钥来源优先级：`--private-key` > `--private-key-file` > `BASE_SEPOLIA_PRIVATE_KEY` /
`EVM_PRIVATE_KEY`；私钥不会写入日志。

### Coordinator 读取 Base Sepolia 合约

```json
"chain_registry": {
  "enabled": true,
  "rpc_url": "https://sepolia.base.org",
  "registry_address": "<DEPLOYED_ADDRESS>",
  "refresh_seconds": 30,
  "timeout_seconds": 10
}
```

### 当前状态

- **尚未部署**：测试网部署地址余额为 0，未从 faucet 取得测试币；
- 部署脚本与 dry-run 已提供；充值后执行上面的命令即可，实际地址需填入 Coordinator 配置；
- 未在 Base Sepolia 上做过注册/任务演示，不能声称公链验证完成。

## 4. 端到端演示

```bash
python scripts/demo_onchain_registry.py \
  --rpc http://127.0.0.1:8545 \
  --registry <DEPLOYED_ADDRESS> \
  --dir var/onchain-registry-demo --reset
```

脚本会：

1. 在链上注册一个 Worker（role=1）和一个 Client（role=2）的 nodeKey；
2. 启动真实 Coordinator，配置 `chain_registry.enabled=true`、`peers=[]`；
3. 启动 Worker，Client 创建任务；
4. Coordinator 在 HELLO 时从链上注册表放行两个 node（无邀请码）；
5. 完成一次 `numeric_batch_v1` 任务（结果为 VALID、1 条 CREDIT）；
6. 打印链上活跃节点数、任务状态、注册校验。

本次实测输出：

```json
{
  "contract": "0x5fbdb2315678afecb367f032d93f642f64180aa3",
  "active_nodes": 2,
  "task_id": "tsk_56cd2cda5b9449bfb4b96950d17381c1",
  "status": {
    "credits_count": 1,
    "result": "VALIDATED",
    "status": "COMPLETED",
    "validation": "VALID"
  },
  "worker_registered": true,
  "client_registered": true
}
```

## 5. Coordinator 配置示例

```json
"chain_registry": {
  "enabled": true,
  "rpc_url": "http://127.0.0.1:8545",
  "registry_address": "0x5fbdb2315678afecb367f032d93f642f64180aa3",
  "refresh_seconds": 2,
  "timeout_seconds": 5
}
```

- `peers` 为空时，Coordinator 仍可通过链上注册表放行活跃节点（仍要 Ed25519 HELLO 签名）；
- 静态 `peers` 和邀请码路径保留，优先级高于链上查找；
- 链 RPC 失败时链上探测返回 false（链模式 fail-closed），静态/邀请码 fallback 仍可用。

## 6. 已知限制

- 仅 Anvil 本地链；**未部署 Base Sepolia**，因为没有测试币；不能声称公链验证完成；
- 注册表不验证 Ed25519 签名与钱包的绑定，nodeKey 可被抢注（Coordinator 仍用 HELLO 验签，
  但注册表可能被噪声污染）；Phase 2 需押金/挑战/验证者准入；
- 无链上任务/结算；任务承诺仍只在本设计文档中，当前原型只做节点注册；
- 合约未审计、无升级机制；仅原型。
