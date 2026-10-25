"""Transport contract for the node layer + rendezvous file helpers.

Two implementations share this one contract (single switch, no second
network protocol):

- Libp2pTransport (same module): the libp2p==0.8.1 trio-native adapter.
  A trio event loop runs on a daemon thread; ``send``/``dial`` bridge in
  from sync callers via trio.from_thread.
- TcpTransport (idle_compute.transport_tcp): pure-stdlib fallback with
  the same contract, so switching is one config line.

Threading model: transports are synchronous. Each incoming stream is
delivered to the handler on a fresh daemon thread as
``handler(peer_id, read, write)`` where ``read(n) -> bytes`` may return
fewer than n bytes and ``write(data)`` blocks until buffered/sent. The
node layer owns framing and validation (idle_compute.protocol); a
transport never interprets message content beyond relaying bytes.

Rendezvous: a node advertises its listen address in
``<runtime_dir>/<node_id>.addr`` as one JSON line
{node_id, addr, protocol}; other nodes read it to dial (pull topology).
The file is runtime data (var/ is gitignored) and carries no secrets.
"""

from __future__ import annotations

import json
import os
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Callable

from .protocol import NETWORK_PROTOCOL_ID  # re-exported for callers

__all__ = [
    "NETWORK_PROTOCOL_ID",
    "TransportError",
    "NetTransport",
    "write_rendezvous_file",
    "read_rendezvous_file",
]

# handler(peer_id, read, write) — read/write operate on ONE stream
StreamHandler = Callable[[str, Callable[[int], bytes], Callable[[bytes], None]], None]


class TransportError(Exception):
    """A transport-level failure (dial refused, stream closed, not started)."""


class NetTransport(ABC):
    """Byte-stream transport contract; thread-safe send/stop.

    ``start()`` must be called before ``dial``/``send``; ``stop()`` is
    idempotent and safe from any thread.
    """

    @abstractmethod
    def start(self) -> None:
        """Bind listeners and accept inbound streams."""

    @abstractmethod
    def stop(self) -> None:
        """Close listeners and streams; idempotent."""

    @property
    @abstractmethod
    def local_peer_id(self) -> str:
        """This node's identity string as seen on the wire."""

    @abstractmethod
    def advertised_addrs(self) -> list[str]:
        """Addresses remote nodes can dial (rendezvous content)."""

    @abstractmethod
    def dial(self, peer_id: str, addrs: list[str]) -> None:
        """Establish an outbound connection/association to ``peer_id``.

        ``addrs`` come from that peer's rendezvous file. Raises
        TransportError on failure.
        """

    @abstractmethod
    def send(self, peer_id: str, data: bytes) -> None:
        """Send one frame's bytes to ``peer_id``; raises TransportError."""

    @abstractmethod
    def set_handler(self, handler: StreamHandler | None) -> None:
        """Install the inbound-stream handler (one fresh thread per stream)."""


def write_rendezvous_file(path: Path | str, node_id: str, addr: str) -> None:
    """Atomically write {node_id, addr, protocol} to ``path``."""
    payload = json.dumps(
        {"node_id": node_id, "addr": addr, "protocol": NETWORK_PROTOCOL_ID},
        sort_keys=True,
    )
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(payload + "\n", encoding="utf-8")
    os.replace(tmp, target)


def read_rendezvous_file(path: Path | str) -> dict[str, Any]:
    """Read a rendezvous file; raises TransportError when unreadable."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.loads(handle.read())
    except (OSError, ValueError) as exc:
        raise TransportError(f"cannot read rendezvous file {path}: {exc}") from exc
    if not isinstance(data, dict) or not data.get("node_id") or not data.get("addr"):
        raise TransportError(f"rendezvous file {path} is malformed")
    return data


# ---------------------------------------------------------------------------
# Libp2pTransport (C-1): the libp2p==0.8.1 trio-native adapter.
#
# Threading model: ONE trio event loop lives on a daemon thread launched by
# ``start()``. Synchronous callers bridge in with ``trio.from_thread.run``;
# every inbound stream is delivered to the handler on a fresh daemon thread
# with read/write closures that bridge back to the trio loop. The transport
# relays bytes only — framing and validation are owned by idle_compute.protocol.
# ---------------------------------------------------------------------------

import logging
import threading

import trio
from libp2p import new_host
from libp2p.crypto.keys import KeyPair
from libp2p.peer.id import ID
from libp2p.peer.peerinfo import PeerInfo
from multiaddr import Multiaddr

logger = logging.getLogger(__name__)


class Libp2pTransport(NetTransport):
    """Byte-stream transport over a libp2p host (trio loop on a daemon thread).

    ``start()`` is idempotent (second call is a no-op); ``stop()`` is
    idempotent and safe from any thread; ``dial()``/``send()`` raise
    TransportError on failure. Outbound streams are reused per peer and
    recreated after a failure. A dialed outbound stream is served like an
    inbound one (handler on a fresh daemon thread), symmetric with
    TcpTransport, so request/reply over one stream needs no reverse dial.
    """

    _START_TIMEOUT = 30.0  # seconds to wait for the host to be listening
    _STOP_TIMEOUT = 10.0   # seconds to wait for the trio thread to exit
    _STOP_POLL = 0.05      # trio-side stop polling interval

    def __init__(self, key_pair: KeyPair, listen_addrs: list[str]) -> None:
        if key_pair is None:
            raise ValueError("Libp2pTransport requires a key_pair")
        self._key_pair = key_pair
        # Parsed once up front so a malformed listen address fails in the
        # constructor instead of inside the trio thread.
        self._listen_multiaddrs = [Multiaddr(addr) for addr in listen_addrs]
        self._local_id = ID.from_pubkey(key_pair.public_key).to_string()
        self._handler: StreamHandler | None = None
        self._streams: dict[str, Any] = {}          # peer_id -> cached outbound stream
        self._streams_lock = threading.Lock()
        self._write_lock = threading.Lock()          # serializes outbound writes
        self._dialed_addrs: dict[str, list[str]] = {}  # peer_id -> last dialed addrs
        self._dialed_lock = threading.Lock()
        # One handler thread per stream (inbound or dialed): cap concurrent
        # streams so hostile dials cannot spawn unbounded threads (audit
        # finding transport_tcp.py:322 applies here too).
        self._max_streams = 64
        self._active_streams = 0
        self._active_streams_lock = threading.Lock()
        self._lifecycle_lock = threading.Lock()
        self._host: Any = None
        self._trio_token: Any = None  # captured inside the trio loop
        self._thread: threading.Thread | None = None
        self._running = False
        self._stop_event: threading.Event | None = None
        self._ready_event: threading.Event | None = None
        self._start_error: BaseException | None = None

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Bind listeners and accept inbound streams; second call is a no-op."""
        self._check_trio_version()
        with self._lifecycle_lock:
            thread = self._thread
            if thread is not None and thread.is_alive():
                ready = self._ready_event
                if ready is not None:
                    ready.wait(timeout=self._START_TIMEOUT)
                if self._running:
                    return
                # The previous trio thread died (or is dying); fall through
                # and start a fresh one.
            ready_event = threading.Event()
            stop_event = threading.Event()
            self._start_error = None
            self._ready_event = ready_event
            self._stop_event = stop_event
            self._running = False
            with self._streams_lock:
                self._streams.clear()
            self._thread = threading.Thread(
                target=self._run_trio_main,
                args=(ready_event, stop_event),
                name="idle-compute-libp2p",
                daemon=True,
            )
            self._thread.start()
        if not ready_event.wait(timeout=self._START_TIMEOUT):
            raise TransportError(
                f"transport did not start listening within {self._START_TIMEOUT:.0f}s"
            )
        if not self._running:
            raise TransportError(f"transport failed to start: {self._start_error}")

    def stop(self) -> None:
        """Close listeners and streams; idempotent, safe from any thread."""
        with self._lifecycle_lock:
            stop_event = self._stop_event
            thread = self._thread
        if stop_event is not None:
            stop_event.set()
        with self._streams_lock:
            self._streams.clear()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=self._STOP_TIMEOUT)
            if thread.is_alive():
                # Teardown is taking too long; treat the transport as stopped
                # so callers stop using it even if the daemon thread lingers.
                self._running = False

    @staticmethod
    def _check_trio_version() -> None:
        parts = trio.__version__.split(".")
        if len(parts) < 2 or parts[0] != "0" or parts[1] != "30":
            raise RuntimeError(
                "Libp2pTransport requires trio 0.30.x, "
                f"found {trio.__version__}"
            )

    def _run_trio_main(
        self, ready_event: threading.Event, stop_event: threading.Event
    ) -> None:
        try:
            trio.run(self._trio_main, ready_event, stop_event)
        except BaseException as error:  # surfaced by start()
            self._start_error = error
        finally:
            self._trio_token = None
            self._running = False
            ready_event.set()

    async def _trio_main(
        self, ready_event: threading.Event, stop_event: threading.Event
    ) -> None:
        # The host is created on the trio thread so its trio primitives are
        # born inside the loop that will use them.
        host = new_host(
            key_pair=self._key_pair, listen_addrs=list(self._listen_multiaddrs)
        )
        self._host = host
        # trio 0.30: from_thread calls from threads trio did not create
        # require an explicit trio_token.
        self._trio_token = trio.lowlevel.current_trio_token()
        host.set_stream_handler(NETWORK_PROTOCOL_ID, self._stream_handler)
        async with host.run(list(self._listen_multiaddrs)):
            self._running = True
            ready_event.set()
            while not stop_event.is_set():
                await trio.sleep(self._STOP_POLL)

    def _from_thread(self, async_fn: Any, *args: Any) -> Any:
        """Run an async callable on the trio thread; TransportError on failure."""
        token = self._trio_token
        if token is None:
            raise TransportError("transport is not running")
        try:
            return trio.from_thread.run(async_fn, *args, trio_token=token)
        except TransportError:
            raise
        except (KeyboardInterrupt, SystemExit):
            raise
        except BaseException as error:  # incl. trio.Cancelled at shutdown
            raise TransportError(f"transport call failed: {error}") from error

    # -- identity -----------------------------------------------------------

    @property
    def local_peer_id(self) -> str:
        """This node's 52-char peer id ('12D3KooW...')."""
        return self._local_id

    def advertised_addrs(self) -> list[str]:
        """Multiaddr strings remote nodes can dial (empty before start)."""
        if not self._running:
            return []
        host = self._host
        if host is None:
            return []
        try:
            token = self._trio_token
            if token is None:
                return []
            addrs = trio.from_thread.run_sync(
                host.get_transport_addrs, trio_token=token
            )
        except BaseException:
            return []
        return [str(addr) for addr in addrs]

    # -- outbound -----------------------------------------------------------

    def dial(self, peer_id: str, addrs: list[str]) -> None:
        """Establish an outbound connection to ``peer_id`` (rendezvous addrs)."""
        self._require_started()
        try:
            pid = ID.from_base58(peer_id)
            multiaddrs = [Multiaddr(addr) for addr in addrs]
        except Exception as error:
            raise TransportError(f"cannot dial peer {peer_id!r}: {error}") from error
        with self._dialed_lock:
            self._dialed_addrs[peer_id] = list(addrs)
        try:
            self._from_thread(
                self._host.connect, PeerInfo(peer_id=pid, addrs=multiaddrs)
            )
        except TransportError as error:
            raise TransportError(f"dial to peer {peer_id!r} failed: {error}") from error

    def send(self, peer_id: str, data: bytes) -> None:
        """Send one frame's bytes to ``peer_id``; raises TransportError."""
        self._require_started()
        with self._streams_lock:
            stream = self._streams.get(peer_id)
        if stream is None:
            stream = self._new_out_stream(peer_id)
            with self._streams_lock:
                self._streams[peer_id] = stream
        try:
            self._write(stream, data)
            return
        except Exception as error:
            with self._streams_lock:
                self._streams.pop(peer_id, None)
            # Recreate once: the connection may have dropped while this
            # peer's stream sat idle in the cache.
            try:
                stream = self._new_out_stream(peer_id)
            except Exception as recreate_error:
                raise TransportError(
                    f"send to peer {peer_id!r} failed: {error}"
                ) from recreate_error
            with self._streams_lock:
                self._streams[peer_id] = stream
            try:
                self._write(stream, data)
            except Exception as second_error:
                with self._streams_lock:
                    self._streams.pop(peer_id, None)
                raise TransportError(
                    f"send to peer {peer_id!r} failed after stream "
                    f"recreation: {second_error}"
                ) from second_error

    def _write(self, stream: Any, data: bytes) -> None:
        # Serialize writes: stream.write() coroutines run on the trio thread,
        # so without the lock two concurrent sends could interleave chunks on
        # one stream.
        with self._write_lock:
            self._from_thread(stream.write, data)

    def _new_out_stream(self, peer_id: str) -> Any:
        try:
            pid = ID.from_base58(peer_id)
        except Exception as error:
            raise TransportError(f"invalid peer id {peer_id!r}: {error}") from error
        try:
            stream = self._from_thread(self._open_stream, pid)
        except TransportError as error:
            raise TransportError(
                f"cannot open stream to peer {peer_id!r}: {error}"
            ) from error
        # Contract symmetry with TcpTransport: a dialed outbound stream is
        # served exactly like an inbound one — the handler gets a fresh
        # daemon thread reading this stream, so replies from the peer reach
        # the node layer without a second dial in reverse.
        handler = self._handler
        if handler is not None:
            with self._active_streams_lock:
                self._active_streams += 1
            threading.Thread(
                target=self._serve_stream,
                args=(handler, peer_id, stream),
                name="idle-compute-outbound",
                daemon=True,
            ).start()
        return stream

    async def _open_stream(self, pid: ID) -> Any:
        try:
            return await self._host.new_stream(pid, [NETWORK_PROTOCOL_ID])
        except Exception:
            # No live connection: retry with the addresses recorded by the
            # last dial() (swarm's own auto-dial only knows peerstore addrs).
            with self._dialed_lock:
                addrs = list(self._dialed_addrs.get(str(pid), []))
            if not addrs:
                raise
            await self._host.connect(
                PeerInfo(peer_id=pid, addrs=[Multiaddr(addr) for addr in addrs])
            )
            return await self._host.new_stream(pid, [NETWORK_PROTOCOL_ID])

    # -- inbound ------------------------------------------------------------

    def set_handler(self, handler: StreamHandler | None) -> None:
        """Install the inbound-stream handler (one fresh thread per stream)."""
        self._handler = handler

    async def _stream_handler(self, stream: Any) -> None:
        """libp2p stream handler: hands the stream to the sync handler on a
        fresh daemon thread; runs on the trio thread."""
        handler = self._handler
        if handler is None:
            try:
                await stream.reset()
            except Exception:
                pass
            return
        try:
            peer_id = str(stream.muxed_conn.peer_id)
        except Exception:
            try:
                await stream.reset()
            except Exception:
                pass
            return
        with self._active_streams_lock:
            if self._active_streams >= self._max_streams:
                over_limit = True
            else:
                over_limit = False
                self._active_streams += 1
        if over_limit:
            try:
                await stream.reset()
            except Exception:
                pass
            return
        threading.Thread(
            target=self._serve_stream,
            args=(handler, peer_id, stream),
            name="idle-compute-inbound",
            daemon=True,
        ).start()

    def _serve_stream(self, handler: StreamHandler, peer_id: str, stream: Any) -> None:
        def read(n: int) -> bytes:
            return self._from_thread(stream.read, n)

        def write(data: bytes) -> None:
            return self._from_thread(stream.write, data)

        try:
            handler(peer_id, read, write)
        except BaseException:  # incl. trio.Cancelled raised at shutdown
            logger.debug("inbound stream handler failed for peer %s", peer_id)
        finally:
            try:
                self._from_thread(stream.close)
            except BaseException:
                pass
            with self._active_streams_lock:
                self._active_streams -= 1

    def _require_started(self) -> None:
        if not self._running:
            raise TransportError("transport is not started")
