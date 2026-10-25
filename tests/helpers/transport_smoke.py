"""C-1 subprocess smoke helper: start a Libp2pTransport on an OS-assigned
loopback port, publish a rendezvous file, echo wire frames until the parent
closes our stdin, then exit 0.

Usage: python tests/helpers/transport_smoke.py <rendezvous_path>
"""
from __future__ import annotations

import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

warnings.filterwarnings(
    "ignore", message="POSIX 0600 permissions cannot be enforced on Windows"
)

from idle_compute.identity import NodeIdentity
from idle_compute.net_transport import Libp2pTransport, write_rendezvous_file
from idle_compute.protocol import encode_frame, read_frame


def make_echo(transport):
    """Echo each inbound wire frame back to the SENDER over a fresh outbound
    stream (``transport.send``), so the parent receives the reply through its
    own inbound handler — the exact path a real node uses."""

    def echo(peer_id: str, read, write) -> None:
        while True:
            try:
                body = read_frame(read)
            except Exception:
                return
            if body is None:
                return
            try:
                transport.send(peer_id, encode_frame(body))
            except Exception:
                return

    return echo


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: transport_smoke.py <rendezvous_path>", file=sys.stderr)
        return 2
    rendezvous_path = Path(sys.argv[1])
    identity = NodeIdentity.load_or_create(
        rendezvous_path.parent / "smoke-identity" / "identity.key",
        node_name="smoke-child",
    )
    transport = Libp2pTransport(identity.key_pair, ["/ip4/127.0.0.1/tcp/0"])
    transport.set_handler(make_echo(transport))
    transport.start()
    addrs = transport.advertised_addrs()
    if not addrs:
        print("smoke child: no advertised addresses after start", file=sys.stderr)
        transport.stop()
        return 3
    write_rendezvous_file(rendezvous_path, transport.local_peer_id, addrs[0])
    print(f"READY {transport.local_peer_id}", flush=True)
    try:
        sys.stdin.read()  # serve until the parent closes our stdin
    finally:
        transport.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
