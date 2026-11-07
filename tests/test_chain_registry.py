"""Phase 1 NodeRegistry prototype: contract shape, RPC client, chain admission."""

from __future__ import annotations

import json
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from idle_compute.chain_registry import (
    ChainRegistryClient,
    ChainRegistryError,
    ROLE_CODES,
    _selector,
    node_key,
)
from idle_compute.config import ValidationError, from_dict
from idle_compute.node import Node

ROOT = Path(__file__).resolve().parent.parent
NODE_ID = "12D3KooW" + "A" * 44


class _RegistryState:
    def __init__(self) -> None:
        self.nodes: dict[str, list[int]] = {}

    def add(self, node_id: str, role: str, active: bool = True) -> str:
        key = node_key(node_id)
        self.nodes[key.lower()] = [0, ROLE_CODES[role], 0, 0, 0, int(active)]
        return key


def _word(value: int) -> str:
    return f"{value:064x}"


class _RpcHandler(BaseHTTPRequestHandler):
    state: _RegistryState

    def log_message(self, *_args) -> None:  # silence test server
        return

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length).decode("utf-8"))
        data = payload["params"][0]["data"]
        selector = data[:10]
        if selector == _selector("activeCount()"):
            result = "0x" + _word(len([n for n in self.state.nodes.values() if n[5]]))
        elif selector == _selector("activeAt(uint256)"):
            index = int(data[10:], 16)
            active = [key for key, node in self.state.nodes.items() if node[5]]
            result = "0x" + active[index][2:]
        elif selector == _selector("active(bytes32)"):
            key = "0x" + data[10:]
            node = self.state.nodes.get(key.lower())
            result = "0x" + _word(int(bool(node and node[5])))
        elif selector == _selector("get(bytes32)"):
            key = "0x" + data[10:]
            node = self.state.nodes.get(key.lower())
            if node is None:
                result = "0x" + _word(0) * 6
            else:
                owner, role, tls, cap, endpoint, active = node
                result = "0x" + "".join(
                    [
                        _word(owner),
                        _word(role),
                        _word(tls),
                        _word(cap),
                        _word(endpoint),
                        _word(active),
                    ]
                )
        else:
            result = "0x"
        body = json.dumps({"jsonrpc": "2.0", "id": payload.get("id"), "result": result}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ChainRegistryClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.state = _RegistryState()
        handler = type("Handler", (_RpcHandler,), {"state": self.state})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        self.client = ChainRegistryClient(self.url, "0x" + "1" * 40, refresh_seconds=1)

    def test_refresh_and_role_aware_lookup(self) -> None:
        self.state.add(NODE_ID, "worker")
        self.assertEqual(self.client.refresh(force=True), 1)
        self.assertTrue(self.client.is_active(NODE_ID, "worker"))
        self.assertFalse(self.client.is_active(NODE_ID, "client"))
        record = self.client.lookup(NODE_ID)
        self.assertIsNotNone(record)
        self.assertEqual(record.role, "worker")

    def test_invalid_config_and_unknown_node(self) -> None:
        with self.assertRaises(ChainRegistryError):
            ChainRegistryClient("ftp://127.0.0.1:1", "0x" + "1" * 40)
        with self.assertRaises(ChainRegistryError):
            ChainRegistryClient(self.url, "0x1234")
        self.assertFalse(self.client.is_active(NODE_ID))
        self.assertEqual(self.client.refresh(force=True), 0)


class ChainConfigAndGateTests(unittest.TestCase):
    def test_chain_config_roundtrip(self) -> None:
        cfg = from_dict(
            {
                "role": "coordinator",
                "transport": "tcp",
                "data_dir": "var/chain",
                "peers": [],
                "chain_registry": {
                    "enabled": True,
                    "rpc_url": "http://127.0.0.1:8545",
                    "registry_address": "0x" + "2" * 40,
                    "refresh_seconds": 5,
                },
            }
        )
        self.assertTrue(cfg.chain_registry.enabled)
        self.assertEqual(cfg.to_dict()["chain_registry"]["refresh_seconds"], 5)
        with self.assertRaises(ValidationError):
            from_dict(
                {
                    "role": "coordinator",
                    "transport": "tcp",
                    "data_dir": "var/chain",
                    "peers": [],
                    "chain_registry": {"enabled": True, "registry_address": ""},
                }
            )

    def test_node_chain_admission_helper(self) -> None:
        class FakeChain:
            def is_active(self, peer_id, role):
                return peer_id == NODE_ID and role == "worker"

        with tempfile.TemporaryDirectory(prefix="chain-gate-", ignore_cleanup_errors=True) as tmp:
            cfg = from_dict(
                {
                    "role": "coordinator",
                    "transport": "tcp",
                    "data_dir": str(Path(tmp) / "coord"),
                    "peers": [],
                    "chain_registry": {
                        "enabled": True,
                        "rpc_url": "http://127.0.0.1:1",
                        "registry_address": "0x" + "3" * 40,
                    },
                }
            )
            node = Node(cfg)
            self.addCleanup(node.shutdown)
            node._chain = FakeChain()
            self.assertTrue(node._chain_admits(NODE_ID, "worker"))
            self.assertFalse(node._chain_admits(NODE_ID, "client"))


class ContractShapeTests(unittest.TestCase):
    def test_registry_contract_has_minimal_surface_and_no_value_transfer(self) -> None:
        source = (ROOT / "contracts" / "NodeRegistry.sol").read_text(encoding="utf-8")
        for marker in ("function register", "function get", "function listActive", "event NodeRegistered"):
            self.assertIn(marker, source)
        for forbidden in ("transfer(", "selfdestruct", "delegatecall", "ERC20"):
            self.assertNotIn(forbidden, source)


if __name__ == "__main__":
    unittest.main()
