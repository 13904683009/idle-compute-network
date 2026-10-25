"""Tests for idle_compute.config: strict validated node configuration (C-3).

Covers every rule in the NodeConfig schema: unknown-key rejection (typo
protection), the inline node-id shape check (52 chars, 12D3KooW prefix,
base58 alphabet), role/transport/log_level enums, the role vs
coordinator_node_id rules, public-listen authorization, port and interval
ranges, the db_path/data_dir exclusivity rule, the peers allowlist shape
check, defaults, to_dict roundtrips for all three roles, and the
ConfigError-vs-ValidationError split in load_config.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from idle_compute.config import (
    ConfigError,
    NodeConfig,
    ValidationError,
    from_dict,
    is_valid_node_id,
    load_config,
)

# Fixed 52-char node-id-shaped strings (not real keypairs; shape only).
# Base58 alphabet: no 0, O, I, l.
NODE_A = "12D3KooWAbCdEf1234GhJkMn5678PqRsTu9WXYZabcefgmnpqrst"
NODE_B = "12D3KooWstuvwxyz123456789ABCDEFGHJKLMNPQRSTUVWXYZabc"
COORD = NODE_A


def worker_cfg(**overrides):
    cfg = {
        "role": "worker",
        "data_dir": "var/idle-node",
        "coordinator_node_id": COORD,
        # Symmetric default-deny: the coordinator must be allowlisted.
        "peers": [COORD],
    }
    cfg.update(overrides)
    return cfg


class NodeIdShapeTests(unittest.TestCase):
    def test_shape_helper_accepts_valid_and_rejects_malformed(self) -> None:
        self.assertTrue(is_valid_node_id(NODE_A))
        self.assertTrue(is_valid_node_id(NODE_B))
        self.assertFalse(is_valid_node_id(NODE_A[:-1]))  # 51 chars
        self.assertFalse(is_valid_node_id(NODE_A + "a"))  # 53 chars
        self.assertFalse(is_valid_node_id("11" + NODE_A[2:]))  # bad prefix
        self.assertFalse(is_valid_node_id(NODE_A[:40] + "0" + NODE_A[41:]))  # '0' not in base58
        self.assertFalse(is_valid_node_id(None))
        self.assertFalse(is_valid_node_id(123))


class ConfigValidationTests(unittest.TestCase):
    def test_unknown_key_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            from_dict(worker_cfg(typo_key="oops"))
        self.assertIn("typo_key", str(ctx.exception))

    def test_from_dict_requires_a_dict(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            from_dict(["role", "worker"])  # type: ignore[arg-type]
        self.assertIn("JSON object", str(ctx.exception))

    def test_node_id_bad_length_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            from_dict(worker_cfg(node_id=NODE_A[:-1]))
        self.assertIn("node_id", str(ctx.exception))

    def test_node_id_bad_prefix_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            from_dict(worker_cfg(node_id="11" + NODE_A[2:]))
        self.assertIn("node_id", str(ctx.exception))

    def test_node_id_bad_alphabet_char_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            from_dict(worker_cfg(node_id=NODE_A[:40] + "0" + NODE_A[41:]))
        self.assertIn("node_id", str(ctx.exception))

    def test_node_id_wrong_type_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            from_dict(worker_cfg(node_id=123))
        self.assertIn("node_id", str(ctx.exception))

    def test_role_required(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            from_dict({"data_dir": "var/x", "coordinator_node_id": COORD})
        self.assertIn("role", str(ctx.exception))

    def test_role_invalid_value_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            from_dict(worker_cfg(role="supervisor"))
        self.assertIn("role", str(ctx.exception))
        with self.assertRaises(ValidationError) as ctx:
            from_dict(worker_cfg(role=7))
        self.assertIn("role", str(ctx.exception))

    def test_transport_invalid_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            from_dict(worker_cfg(transport="udp"))
        self.assertIn("transport", str(ctx.exception))
        with self.assertRaises(ValidationError) as ctx:
            from_dict(worker_cfg(transport=3))
        self.assertIn("transport", str(ctx.exception))

    def test_log_level_invalid_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            from_dict(worker_cfg(log_level="TRACE"))
        self.assertIn("log_level", str(ctx.exception))
        with self.assertRaises(ValidationError) as ctx:
            from_dict(worker_cfg(log_level="info"))
        self.assertIn("log_level", str(ctx.exception))

    def test_worker_without_coordinator_node_id_rejected(self) -> None:
        cfg = worker_cfg()
        del cfg["coordinator_node_id"]
        with self.assertRaises(ValidationError) as ctx:
            from_dict(cfg)
        self.assertIn("coordinator_node_id", str(ctx.exception))

    def test_client_without_coordinator_node_id_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            from_dict({"role": "client", "data_dir": "var/x"})
        self.assertIn("coordinator_node_id", str(ctx.exception))

    def test_coordinator_with_coordinator_node_id_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            from_dict({"role": "coordinator", "data_dir": "var/x", "coordinator_node_id": NODE_B})
        self.assertIn("coordinator_node_id", str(ctx.exception))

    def test_coordinator_node_id_bad_shape_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            from_dict(worker_cfg(coordinator_node_id=NODE_A[:-1]))
        self.assertIn("coordinator_node_id", str(ctx.exception))

    def test_public_listen_host_requires_flag(self) -> None:
        for host in ("0.0.0.0", "example.com", "192.168.1.10"):
            with self.subTest(host=host):
                with self.assertRaises(ValidationError) as ctx:
                    from_dict(worker_cfg(listen_host=host))
                self.assertIn("listen_host", str(ctx.exception))
                self.assertIn("allow_public_listen", str(ctx.exception))

    def test_public_listen_host_with_flag_ok(self) -> None:
        cfg = from_dict(worker_cfg(listen_host="0.0.0.0", allow_public_listen=True))
        self.assertEqual(cfg.listen_host, "0.0.0.0")
        self.assertTrue(cfg.allow_public_listen)

    def test_loopback_hosts_without_flag_ok(self) -> None:
        for host in ("127.0.0.1", "127.0.0.0", "127.255.255.254", "::1", "localhost", "Localhost"):
            with self.subTest(host=host):
                cfg = from_dict(worker_cfg(listen_host=host))
                self.assertEqual(cfg.listen_host, host)
                self.assertFalse(cfg.allow_public_listen)

    def test_listen_host_must_be_a_host_string(self) -> None:
        for bad in ("", "  ", "two words", 42):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError) as ctx:
                    from_dict(worker_cfg(listen_host=bad))
                self.assertIn("listen_host", str(ctx.exception))

    def test_allow_public_listen_wrong_type_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            from_dict(worker_cfg(allow_public_listen="yes"))
        self.assertIn("allow_public_listen", str(ctx.exception))

    def test_listen_port_out_of_range_rejected(self) -> None:
        for port in (-1, 65536):
            with self.subTest(port=port):
                with self.assertRaises(ValidationError) as ctx:
                    from_dict(worker_cfg(listen_port=port))
                self.assertIn("listen_port", str(ctx.exception))
        for port in (0, 65535):
            with self.subTest(port=port):
                self.assertEqual(from_dict(worker_cfg(listen_port=port)).listen_port, port)

    def test_listen_port_wrong_type_rejected(self) -> None:
        for bad in ("4000", True):
            with self.subTest(bad=bad):
                with self.assertRaises(ValidationError) as ctx:
                    from_dict(worker_cfg(listen_port=bad))
                self.assertIn("listen_port", str(ctx.exception))

    def test_db_path_and_data_dir_conflict_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            from_dict(worker_cfg(data_dir="var/x", db_path="var/db/ledger.sqlite"))
        message = str(ctx.exception)
        self.assertIn("db_path", message)
        self.assertIn("data_dir", message)

    def test_db_path_with_empty_data_dir_ok(self) -> None:
        cfg = from_dict(worker_cfg(data_dir="", db_path="var/db/ledger.sqlite"))
        self.assertEqual(cfg.db_path, "var/db/ledger.sqlite")

    def test_data_dir_required(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            from_dict({"role": "worker", "coordinator_node_id": COORD})
        self.assertIn("data_dir", str(ctx.exception))

    def test_peers_bad_entry_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            from_dict(worker_cfg(peers=[NODE_A, "not-a-node-id"]))
        self.assertIn("peers[1]", str(ctx.exception))
        with self.assertRaises(ValidationError) as ctx:
            from_dict(worker_cfg(peers=["12D3KooW" + "0" * 44]))
        self.assertIn("peers[0]", str(ctx.exception))

    def test_peers_wrong_type_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            from_dict(worker_cfg(peers="peer-string"))
        self.assertIn("peers", str(ctx.exception))

    def test_peers_default_deny_is_the_default(self) -> None:
        # For the coordinator (no mandatory peer) an omitted peers list
        # still means "nobody may connect": default-deny.
        self.assertEqual(from_dict({"role": "coordinator", "data_dir": "var/x"}).peers, [])
        # Worker/client REQUIRE the coordinator in peers (symmetric
        # default-deny); a worker that omits it fails loudly at load time.
        self.assertEqual(from_dict(worker_cfg()).peers, [COORD])
        with self.assertRaises(ValidationError) as ctx:
            from_dict(worker_cfg(peers=[]))
        self.assertIn("peers", str(ctx.exception))
        with self.assertRaises(ValidationError) as ctx:
            from_dict(worker_cfg(peers=[NODE_B]))
        self.assertIn("peers", str(ctx.exception))
        with self.assertRaises(ValidationError) as ctx:
            from_dict(
                {
                    "role": "client",
                    "data_dir": "var/x",
                    "coordinator_node_id": COORD,
                    "peers": [NODE_B],
                }
            )
        self.assertIn("peers", str(ctx.exception))

    def test_poll_interval_out_of_range_rejected(self) -> None:
        for value in (0, 61):
            with self.subTest(value=value):
                with self.assertRaises(ValidationError) as ctx:
                    from_dict(worker_cfg(poll_interval_seconds=value))
                self.assertIn("poll_interval_seconds", str(ctx.exception))
        for value in (1, 60):
            with self.subTest(value=value):
                cfg = from_dict(worker_cfg(poll_interval_seconds=value))
                self.assertEqual(cfg.poll_interval_seconds, value)

    def test_poll_interval_wrong_type_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            from_dict(worker_cfg(poll_interval_seconds="5"))
        self.assertIn("poll_interval_seconds", str(ctx.exception))

    def test_backoff_min_must_be_positive(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            from_dict(worker_cfg(backoff_min=0.0))
        self.assertIn("backoff_min", str(ctx.exception))

    def test_backoff_min_must_not_exceed_max(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            from_dict(worker_cfg(backoff_min=8.0, backoff_max=1.0))
        message = str(ctx.exception)
        self.assertIn("backoff_min", message)
        self.assertIn("backoff_max", message)
        cfg = from_dict(worker_cfg(backoff_min=4.0, backoff_max=4.0))
        self.assertEqual(cfg.backoff_min, cfg.backoff_max)

    def test_backoff_wrong_type_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            from_dict(worker_cfg(backoff_min="1.0"))
        self.assertIn("backoff_min", str(ctx.exception))
        with self.assertRaises(ValidationError) as ctx:
            from_dict(worker_cfg(backoff_max=None))
        self.assertIn("backoff_max", str(ctx.exception))

    def test_coordinator_addr_wrong_type_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            from_dict(worker_cfg(coordinator_addr=43123))
        self.assertIn("coordinator_addr", str(ctx.exception))

    def test_log_file_wrong_type_rejected(self) -> None:
        with self.assertRaises(ValidationError) as ctx:
            from_dict(worker_cfg(log_file=Path("x")))
        self.assertIn("log_file", str(ctx.exception))

    def test_defaults_applied(self) -> None:
        cfg = from_dict(
            {"role": "worker", "data_dir": "var/n", "coordinator_node_id": COORD, "peers": [COORD]}
        )
        self.assertIsNone(cfg.node_id)
        self.assertEqual(cfg.transport, "libp2p")
        self.assertEqual(cfg.listen_host, "127.0.0.1")
        self.assertEqual(cfg.listen_port, 0)
        self.assertFalse(cfg.allow_public_listen)
        self.assertIsNone(cfg.db_path)
        self.assertEqual(cfg.peers, [COORD])
        self.assertIsNone(cfg.coordinator_addr)
        self.assertEqual(cfg.poll_interval_seconds, 5)
        self.assertEqual(cfg.backoff_min, 1.0)
        self.assertEqual(cfg.backoff_max, 8.0)
        self.assertEqual(cfg.log_level, "INFO")
        self.assertIsNone(cfg.log_file)

    def test_valid_configs_roundtrip_for_all_roles(self) -> None:
        configs = {
            "coordinator": {
                "node_id": NODE_A,
                "role": "coordinator",
                "transport": "libp2p",
                "listen_host": "127.0.0.1",
                "listen_port": 40120,
                "allow_public_listen": False,
                "data_dir": "var/coordinator",
                "db_path": None,
                "peers": [NODE_B],
                "coordinator_node_id": None,
                "coordinator_addr": None,
                "poll_interval_seconds": 10,
                "backoff_min": 2.0,
                "backoff_max": 16.0,
                "log_level": "DEBUG",
                "log_file": "var/coordinator.log",
            },
            "worker": {
                "node_id": NODE_B,
                "role": "worker",
                "transport": "tcp",
                "listen_host": "localhost",
                "listen_port": 0,
                "allow_public_listen": False,
                "data_dir": "var/worker",
                "db_path": None,
                "peers": [NODE_A],
                "coordinator_node_id": NODE_A,
                "coordinator_addr": "127.0.0.1:43123",
                "poll_interval_seconds": 5,
                "backoff_min": 1.0,
                "backoff_max": 8.0,
                "log_level": "INFO",
                "log_file": None,
            },
            "client": {
                "node_id": None,
                "role": "client",
                "transport": "libp2p",
                "listen_host": "127.0.0.1",
                "listen_port": 0,
                "allow_public_listen": False,
                "data_dir": "var/client",
                "db_path": None,
                "peers": [NODE_A, NODE_B],
                "coordinator_node_id": NODE_A,
                "coordinator_addr": "/ip4/127.0.0.1/tcp/43123",
                "poll_interval_seconds": 60,
                "backoff_min": 1.0,
                "backoff_max": 8.0,
                "log_level": "WARNING",
                "log_file": None,
            },
        }
        for role, raw in configs.items():
            with self.subTest(role=role):
                cfg = from_dict(raw)
                self.assertIsInstance(cfg, NodeConfig)
                self.assertEqual(from_dict(cfg.to_dict()), cfg)


class LoadConfigTests(unittest.TestCase):
    def test_load_config_reads_json(self) -> None:
        raw = {
            "role": "worker",
            "data_dir": "var/n",
            "coordinator_node_id": COORD,
            "peers": [COORD],
            "transport": "tcp",
            "poll_interval_seconds": 9,
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "node.json"
            path.write_text(json.dumps(raw), encoding="utf-8")
            cfg = load_config(path)
        self.assertEqual(cfg, from_dict(raw))
        self.assertEqual(cfg.poll_interval_seconds, 9)

    def test_load_config_missing_file_raises_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "does-not-exist.json"
            with self.assertRaises(ConfigError) as ctx:
                load_config(missing)
            self.assertNotIsInstance(ctx.exception, ValidationError)

    def test_load_config_malformed_json_raises_config_error(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{not json", encoding="utf-8")
            with self.assertRaises(ConfigError) as ctx:
                load_config(path)
            self.assertNotIsInstance(ctx.exception, ValidationError)

    def test_load_config_non_object_json_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "list.json"
            path.write_text("[1, 2]", encoding="utf-8")
            with self.assertRaises(ValidationError) as ctx:
                load_config(path)
            self.assertIn("JSON object", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
