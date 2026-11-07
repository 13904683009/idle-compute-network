"""Deploy NodeRegistry to a local Anvil (or any JSON-RPC EVM) using eth_sendTransaction.

This is a prototype/dev helper, not production tooling. It uses an unlocked
RPC account (Anvil default), so no private key is handled here.

Usage:
    python scripts/deploy_node_registry.py --rpc http://127.0.0.1:8545
"""

from __future__ import annotations

import argparse
import http.client
import json
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent


def _rpc(rpc_url: str, method: str, params: list[Any], timeout: float = 10.0) -> Any:
    scheme, _, rest = rpc_url.partition("://")
    host_port, _, path = rest.partition("/")
    host, _, port_text = host_port.partition(":")
    port = int(port_text) if port_text else (443 if scheme == "https" else 80)
    conn_cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
    conn = conn_cls(host, port, timeout=timeout)
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode()
    try:
        conn.request("POST", "/" + path if path else "/", body=body,
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        payload = json.loads(response.read().decode("utf-8"))
    finally:
        conn.close()
    if "error" in payload:
        raise SystemExit(f"RPC error: {payload['error']}")
    return payload.get("result")


def main() -> int:
    parser = argparse.ArgumentParser(description="deploy NodeRegistry prototype")
    parser.add_argument("--rpc", default="http://127.0.0.1:8545")
    parser.add_argument("--bin", default=str(ROOT / "contracts" / "NodeRegistry.bin"))
    parser.add_argument("--account-index", type=int, default=0)
    args = parser.parse_args()

    bytecode = Path(args.bin).read_text(encoding="ascii").strip()
    if not bytecode.startswith("0x"):
        bytecode = "0x" + bytecode
    accounts = _rpc(args.rpc, "eth_accounts", [])
    if not accounts:
        raise SystemExit("RPC returned no unlocked accounts")
    sender = accounts[args.account_index % len(accounts)]
    tx_hash = _rpc(
        args.rpc,
        "eth_sendTransaction",
        [{"from": sender, "data": bytecode, "gas": "0x2dc6c0"}],
    )
    deadline = time.monotonic() + 30
    receipt = None
    while time.monotonic() < deadline:
        receipt = _rpc(args.rpc, "eth_getTransactionReceipt", [tx_hash])
        if receipt is not None:
            break
        time.sleep(0.2)
    if receipt is None:
        raise SystemExit("deployment receipt not found")
    if not receipt.get("contractAddress"):
        raise SystemExit(f"deployment failed: {receipt}")
    print(json.dumps({
        "contract_address": receipt["contractAddress"],
        "tx_hash": tx_hash,
        "block": int(receipt["blockNumber"], 16),
        "deployer": sender,
    }, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
