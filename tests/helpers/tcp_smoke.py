"""Real-process echo helper for tests.test_transport_tcp.

Spawned by the test suite as::

    python tests/helpers/tcp_smoke.py [port]

Binds 127.0.0.1:port (default 0, i.e. OS-assigned), prints
"READY <port>" on stdout, then accepts ONE connection and echoes
idle_compute.protocol frames (4-byte big-endian length + payload) until
the peer closes the connection, then exits 0.

If the peer vanishes mid-session (the test stops its transport first)
that is a normal end for an echo helper, also exit 0. Anything else
that goes wrong prints "ERROR <message>" and exits 1 so the test fails
loudly instead of hanging.
"""

import socket
import sys
from pathlib import Path

# Make the repo importable no matter which directory this script is run from.
_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from idle_compute.protocol import encode_frame, read_frame  # noqa: E402


def main() -> int:
    try:
        port = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    except ValueError:
        print(f"ERROR bad port argument {sys.argv[1]!r}")
        return 1
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        listener.bind(("127.0.0.1", port))
        listener.listen(1)
        bound_port = int(listener.getsockname()[1])
        print(f"READY {bound_port}", flush=True)
        conn, _ = listener.accept()
        listener.close()
        with conn:
            conn.settimeout(30.0)
            while True:
                frame = read_frame(conn.recv)
                if frame is None:  # clean EOF from the test client
                    return 0
                conn.sendall(encode_frame(frame))
    except OSError:
        # Peer vanished mid-session: normal end for an echo helper.
        return 0
    except Exception as exc:  # keep the helper honest about real breakage
        print(f"ERROR {exc}", flush=True)
        return 1


if __name__ == "__main__":
    sys.exit(main())
