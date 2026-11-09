"""Deploy NodeRegistry to Base Sepolia (or another EVM RPC) with a signed tx.

Private key sources (never logged):
  --private-key        0x-prefixed hex key on the command line (avoid shells history)
  --private-key-file   file containing the key (preferred)
  BASE_SEPOLIA_PRIVATE_KEY / EVM_PRIVATE_KEY environment variables

Requires web3 + eth-account:
    pip install "web3>=7"

This is prototype tooling. Testnet only; never use a mainnet key.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def _load_private_key(args: argparse.Namespace) -> str:
    key = args.private_key
    if not key and args.private_key_file:
        key = Path(args.private_key_file).read_text(encoding="utf-8").strip()
    if not key:
        key = os.environ.get("BASE_SEPOLIA_PRIVATE_KEY") or os.environ.get("EVM_PRIVATE_KEY")
    if not key:
        raise SystemExit(
            "no private key: use --private-key-file, --private-key, or "
            "BASE_SEPOLIA_PRIVATE_KEY/EVM_PRIVATE_KEY"
        )
    key = key.strip()
    if not key.startswith("0x"):
        key = "0x" + key
    if len(key) != 66:
        raise SystemExit("private key must be 32 bytes hex (0x + 64 chars)")
    return key


def main() -> int:
    parser = argparse.ArgumentParser(description="deploy NodeRegistry with a signed EVM transaction")
    parser.add_argument("--rpc", default="https://sepolia.base.org")
    parser.add_argument("--chain-id", type=int, default=84532)
    parser.add_argument("--private-key", default=None, help="0x private key (testnet only)")
    parser.add_argument("--private-key-file", default=None, help="file containing the private key")
    parser.add_argument("--bin", default=str(ROOT / "contracts" / "NodeRegistry.bin"))
    parser.add_argument("--abi", default=str(ROOT / "contracts" / "NodeRegistry.abi.json"))
    parser.add_argument("--explorer", default="https://sepolia.basescan.org")
    parser.add_argument("--dry-run", action="store_true", help="estimate/build tx but do not send")
    args = parser.parse_args()

    try:
        from eth_account import Account
        from web3 import Web3
    except ImportError as exc:
        raise SystemExit('missing dependency: pip install "web3>=7"') from exc

    key = _load_private_key(args)
    w3 = Web3(Web3.HTTPProvider(args.rpc, request_kwargs={"timeout": 30}))
    if not w3.is_connected():
        raise SystemExit(f"cannot connect to RPC: {args.rpc}")
    chain_id = w3.eth.chain_id
    if args.chain_id and chain_id != args.chain_id:
        raise SystemExit(f"wrong chain id: RPC={chain_id}, expected {args.chain_id}")
    account = Account.from_key(key)
    balance = w3.eth.get_balance(account.address)
    print(f"deployer: {account.address}")
    print(f"chain_id: {chain_id}")
    print(f"balance:  {w3.from_wei(balance, 'ether')} ETH")
    if balance == 0:
        raise SystemExit(
            "deployer has 0 testnet ETH; fund it from a Base Sepolia faucet first "
            "(see contracts/README.md)"
        )
    bytecode = Path(args.bin).read_text(encoding="ascii").strip()
    if not bytecode.startswith("0x"):
        bytecode = "0x" + bytecode
    abi = Path(args.abi).read_text(encoding="utf-8")
    factory = w3.eth.contract(abi=abi, bytecode=bytecode)
    tx = factory.constructor().build_transaction(
        {
            "from": account.address,
            "nonce": w3.eth.get_transaction_count(account.address),
            "chainId": chain_id,
            "maxFeePerGas": w3.to_wei(2, "gwei"),
            "maxPriorityFeePerGas": w3.to_wei(1, "gwei"),
        }
    )
    if "gas" not in tx:
        tx["gas"] = int(w3.eth.estimate_gas(tx) * 1.2)
    print(f"estimated gas: {tx['gas']}")
    if args.dry_run:
        print("dry-run: transaction built, not sent")
        return 0
    signed = account.sign_transaction(tx)
    tx_hash = w3.eth.send_raw_transaction(signed.raw_transaction)
    print(f"tx: {tx_hash.hex()}")
    receipt = w3.eth.wait_for_transaction_receipt(tx_hash, timeout=180)
    if receipt.status != 1 or not receipt.contractAddress:
        raise SystemExit(f"deployment failed: {receipt}")
    print(
        json.dumps(
            {
                "contract_address": receipt.contractAddress,
                "tx_hash": tx_hash.hex(),
                "block": receipt.blockNumber,
                "explorer": f"{args.explorer}/address/{receipt.contractAddress}",
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
