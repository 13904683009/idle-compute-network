"""Pure-stdlib TCP transport: the zero-dependency fallback transport.

This module implements the exact ``NetTransport`` contract on top of
plain TCP sockets. The wire is a raw byte stream: the transport relays
bytes only and never interprets message content; framing and validation
are the node layer's job (idle_compute.protocol). Switching between
Libp2pTransport and TcpTransport is one config line, because both expose
the same ``handler(peer_id, read, write)`` callbacks and the same errors.

Threading model (all synchronous, no event loop):

- ``start()`` binds a listening socket (SO_REUSEADDR) on
  ``listen_host:listen_port`` (port 0 = OS-assigned) and spawns ONE
  daemon accept thread.
- Every accepted connection is served on its own fresh daemon thread as
  ``handler(peer_id, read, write)`` with ``read=conn.recv``, ``write``
  being a locked ``conn.sendall``, and the placeholder peer id
  ``tcp:<remote-host>:<port>``. A dialed (outbound) connection is served
  the same way, with the dialed ``peer_id`` as the handler's peer id.
- ``read(n)`` may return fewer than ``n`` bytes and returns ``b""`` once
  the peer closed the connection; handlers treat ``b""`` as
  end-of-stream and return quietly (the serve thread then exits and the
  connection is dropped).
- Both sides of every connection carry a 300 s socket timeout, so a
  half-open (silently dead) peer cannot pin a handler thread forever:
  the blocked read eventually times out, the thread exits, and the
  connection is closed.
- ``stop()`` closes the listener and every live connection (accepted and
  pooled outbound alike). Closing a socket unblocks any thread parked in
  ``recv``/``sendall`` on it, so handler threads exit promptly; no
  thread is ever joined, which makes ``stop()`` safe to call from any
  thread — including a handler thread. ``stop()`` is idempotent and
  ``start()`` may be called again afterwards.

Identity note (by design, WAVE 1): the on-wire peer id of an accepted
connection is the placeholder ``tcp:<host>:<port>``. The transport
relays bytes only and cannot know who is on the other end; HELLO-first
identity binding is the node layer's job.

Reconnect note: ``send`` raises TransportError when no connection exists
for ``peer_id`` (the node layer dials first) or when the write fails; a
dead connection is then dropped from the pool. Reconnect policy belongs
to the node layer — this transport never re-dials on its own.

Concurrency note: every connection has a write lock shared by ``send``
and the handler's ``write`` closure, so two threads writing on the same
connection cannot interleave the bytes of two frames.
"""

from __future__ import annotations

import logging
import socket
import ssl
import threading
from typing import Any, Callable, Optional

from .net_transport import (
    NetTransport,
    TransportError,
    read_rendezvous_file,
    write_rendezvous_file,
)

__all__ = [
    "TcpTransport",
    "TransportError",
    "fetch_server_certificate",
    "write_rendezvous_file",
    "read_rendezvous_file",
]

# A read or write on a live connection blocks at most this long before
# the serve thread gives up on a half-open peer (see module docstring).
_READ_WRITE_TIMEOUT_SECONDS = 300.0
_CONNECT_TIMEOUT_SECONDS = 10.0
_TLS_HANDSHAKE_TIMEOUT_SECONDS = 10.0
_ACCEPT_BACKLOG = 16

logger = logging.getLogger(__name__)

# handler(peer_id, read, write); read(n) -> bytes, write(data) -> None
_StreamHandler = Callable[[str, Callable[[int], bytes], Callable[[bytes], None]], None]


def _quiet_close(sock: socket.socket) -> None:
    """Shut down and close a socket without raising."""
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _parse_addr(addr: str) -> tuple[str, int]:
    """Split ``host:port``; raise ValueError when malformed or out of range."""
    host, sep, port_text = addr.rpartition(":")
    if not sep or not host:
        raise ValueError(f"address must be 'host:port', got {addr!r}")
    try:
        port = int(port_text)
    except ValueError:
        raise ValueError(f"invalid port in address {addr!r}") from None
    if not 0 < port < 65536:
        raise ValueError(f"port out of range in address {addr!r}")
    return host, port


def fetch_server_certificate(addr: str, timeout: float = 10.0) -> bytes:
    """Fetch the server certificate DER without verification.

    Only for the invite bootstrap: the caller MUST compare the returned DER
    against the SHA-256 fingerprint carried in the out-of-band invite before
    trusting it. This is the only networking helper outside TcpTransport.
    """
    host, port = _parse_addr(addr)
    context = ssl._create_unverified_context()
    with socket.create_connection((host, port), timeout=timeout) as raw:
        with context.wrap_socket(raw, server_hostname=host) as tls:
            der = tls.getpeercert(binary_form=True)
    if not der:
        raise ValueError("server did not present a certificate")
    return der


class _Conn:
    """One established connection plus the lock that serializes its writes."""

    __slots__ = ("peer_id", "sock", "_write_lock")

    def __init__(self, peer_id: str, sock: socket.socket) -> None:
        self.peer_id = peer_id
        self.sock = sock
        self._write_lock = threading.Lock()

    def write(self, data: bytes) -> None:
        """``sendall`` under the per-connection write lock (frame atomicity)."""
        with self._write_lock:
            self.sock.sendall(data)

    def close(self) -> None:
        _quiet_close(self.sock)


class TcpTransport(NetTransport):
    """Pure-stdlib TCP transport; see the module docstring for the model."""

    def __init__(
        self,
        peer_id: str,
        listen_host: str = "127.0.0.1",
        listen_port: int = 0,
        tls: Any | None = None,
    ) -> None:
        if not isinstance(peer_id, str) or not peer_id:
            raise ValueError("peer_id must be a non-empty string")
        if not isinstance(listen_port, int) or not 0 <= listen_port < 65536:
            raise ValueError(f"listen_port out of range: {listen_port!r}")
        self._peer_id = peer_id
        self._listen_host = listen_host
        self._listen_port = listen_port
        self._tls = tls
        self._server_context: ssl.SSLContext | None = None
        self._client_context: ssl.SSLContext | None = None
        self._lock = threading.RLock()
        self._listener: Optional[socket.socket] = None
        self._bound_port: Optional[int] = None
        self._stopped = False
        self._handler: Optional[_StreamHandler] = None
        self._pool: dict[str, _Conn] = {}  # dialed conns, keyed by peer_id
        self._live: set[socket.socket] = set()  # every open conn, for stop()
        # One handler thread per connection: cap concurrent streams so a
        # flood of accepted connections cannot spawn unbounded threads
        # (audit finding transport_tcp.py:322).
        self._max_streams = 64

    # -- NetTransport contract -------------------------------------------

    @property
    def local_peer_id(self) -> str:
        return self._peer_id

    def set_handler(self, handler: Optional[_StreamHandler]) -> None:
        """Install the inbound-stream handler (captured per connection).

        ``None`` means new connections are closed on arrival. Connections
        already being served keep the handler they were started with.
        """
        with self._lock:
            self._handler = handler

    def start(self) -> None:
        """Bind the listener and start the accept thread."""
        with self._lock:
            if self._listener is not None:
                raise TransportError(
                    f"{self._peer_id}: start() called twice without stop()"
                )
            if self._tls is not None and getattr(self._tls, "cert_file", None):
                self._server_context = self._build_server_context()
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                listener.bind((self._listen_host, self._listen_port))
                listener.listen(_ACCEPT_BACKLOG)
            except OSError as exc:
                _quiet_close(listener)
                raise TransportError(
                    f"{self._peer_id}: cannot listen on "
                    f"{self._listen_host}:{self._listen_port}: {exc}"
                ) from exc
            self._listener = listener
            self._bound_port = int(listener.getsockname()[1])
            self._stopped = False
            thread = threading.Thread(
                target=self._accept_loop,
                name=f"tcp-accept-{self._peer_id}",
                daemon=True,
            )
        thread.start()

    def stop(self) -> None:
        """Close the listener and every live connection; idempotent.

        No thread is joined, so this is safe from any thread, including
        handler threads. Blocked readers/writers wake with EOF/OSError
        as their sockets are shut down and exit quietly.
        """
        with self._lock:
            listener, self._listener = self._listener, None
            self._bound_port = None
            self._stopped = True
            pooled = list(self._pool.values())
            self._pool.clear()
            live = list(self._live)
            self._live.clear()
        if listener is not None:
            _quiet_close(listener)
        for conn in pooled:
            conn.close()
        for sock in live:
            _quiet_close(sock)

    def advertised_addrs(self) -> list[str]:
        """Rendezvous content: ``["host:port"]`` while started, else ``[]``."""
        with self._lock:
            if self._listener is None or self._bound_port is None:
                return []
            return [f"{self._listen_host}:{self._bound_port}"]

    def dial(self, peer_id: str, addrs: list[str]) -> None:
        """Connect to the first reachable ``host:port`` in ``addrs``.

        The winning connection is registered in the pool under
        ``peer_id`` (replacing any earlier one, which is closed) and gets
        its own handler thread. Raises TransportError when none of the
        addresses connect or when the transport is not started.
        """
        with self._lock:
            if self._listener is None:
                raise TransportError(f"{self._peer_id}: dial({peer_id}) before start()")
        if not addrs:
            raise TransportError(f"{self._peer_id}: dial({peer_id}): no addresses given")
        failures: list[str] = []
        for addr in addrs:
            try:
                host, port = _parse_addr(addr)
            except ValueError as exc:
                failures.append(str(exc))
                continue
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            try:
                sock.settimeout(_CONNECT_TIMEOUT_SECONDS)
                sock.connect((host, port))
                if self._tls is not None:
                    try:
                        context = self._client_tls_context()
                        server_hostname = getattr(self._tls, "server_hostname", None) or host
                        sock = context.wrap_socket(sock, server_hostname=server_hostname)
                    except (ssl.SSLError, OSError, ValueError, TransportError) as exc:
                        failures.append(f"{addr}: TLS {exc}")
                        _quiet_close(sock)
                        continue
            except OSError as exc:
                failures.append(f"{addr}: {exc}")
                _quiet_close(sock)
                continue
            conn = _Conn(peer_id, sock)
            with self._lock:
                if self._stopped:
                    old = None
                else:
                    old = self._pool.get(peer_id)
                    self._pool[peer_id] = conn
                handler = self._handler
                stopped = self._stopped
            if old is not None:
                old.close()
            if stopped:
                conn.close()
                raise TransportError(f"{self._peer_id}: dial({peer_id}): transport stopped")
            if handler is not None:
                threading.Thread(
                    target=self._serve_conn,
                    args=(conn, handler),
                    name=f"tcp-dial-{self._peer_id}-{peer_id}",
                    daemon=True,
                ).start()
            return
        raise TransportError(
            f"{self._peer_id}: dial({peer_id}) failed: {'; '.join(failures)}"
        )

    def send(self, peer_id: str, data: bytes) -> None:
        """Send one frame's bytes to ``peer_id``; raises TransportError.

        The node layer dials first; there is no auto-connect here. A
        failed write drops the connection from the pool and raises
        TransportError (reconnect policy belongs to the node layer).
        """
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        with self._lock:
            conn = self._pool.get(peer_id)
        if conn is None:
            raise TransportError(
                f"{self._peer_id}: no connection to {peer_id} (dial first)"
            )
        try:
            conn.write(data)
        except OSError as exc:
            with self._lock:
                if self._pool.get(peer_id) is conn:
                    del self._pool[peer_id]
            conn.close()
            raise TransportError(
                f"{self._peer_id}: send to {peer_id} failed: {exc}"
            ) from exc

    # -- internals -------------------------------------------------------

    # -- TLS helpers -----------------------------------------------------

    def _build_server_context(self) -> ssl.SSLContext:
        cert = getattr(self._tls, "cert_file", None)
        key = getattr(self._tls, "key_file", None)
        if not cert or not key:
            raise TransportError(f"{self._peer_id}: TLS server requires cert_file and key_file")
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        try:
            context.load_cert_chain(certfile=cert, keyfile=key)
        except (OSError, ssl.SSLError, ValueError) as exc:
            raise TransportError(
                f"{self._peer_id}: cannot load TLS cert/key: {exc}"
            ) from exc
        return context

    def _client_tls_context(self) -> ssl.SSLContext:
        if self._client_context is None:
            ca_file = getattr(self._tls, "ca_file", None)
            try:
                context = (
                    ssl.create_default_context(cafile=ca_file)
                    if ca_file
                    else ssl.create_default_context()
                )
            except (OSError, ssl.SSLError) as exc:
                raise TransportError(
                    f"{self._peer_id}: cannot load TLS CA file: {exc}"
                ) from exc
            context.check_hostname = True
            context.verify_mode = ssl.CERT_REQUIRED
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            self._client_context = context
        return self._client_context

    def _accept_loop(self) -> None:
        listener = self._listener
        if listener is None:
            return
        while True:
            try:
                sock, remote = listener.accept()
            except OSError:
                return  # listener closed by stop(); exit quietly
            with self._lock:
                handler = self._handler
                stopped = self._stopped
                over_limit = len(self._live) >= self._max_streams
            if handler is None or stopped or over_limit:
                _quiet_close(sock)
                continue
            if self._tls is not None:
                if self._server_context is None:
                    _quiet_close(sock)
                    continue
                try:
                    sock.settimeout(_TLS_HANDSHAKE_TIMEOUT_SECONDS)
                    sock = self._server_context.wrap_socket(sock, server_side=True)
                except (ssl.SSLError, OSError) as exc:
                    logger.warning("TLS handshake failed from %s: %s", remote, exc)
                    _quiet_close(sock)
                    continue
            # Placeholder peer id by design (WAVE 1): the transport relays
            # bytes only; HELLO-first identity binding is the node layer's job.
            peer_id = f"tcp:{remote[0]}:{remote[1]}"
            conn = _Conn(peer_id, sock)
            threading.Thread(
                target=self._serve_conn,
                args=(conn, handler),
                name=f"tcp-serve-{self._peer_id}-{remote[1]}",
                daemon=True,
            ).start()

    def _serve_conn(self, conn: _Conn, handler: _StreamHandler) -> None:
        """Run the handler on one connection until it ends, then drop it."""
        sock = conn.sock
        try:
            sock.settimeout(_READ_WRITE_TIMEOUT_SECONDS)
        except OSError:
            pass
        with self._lock:
            if self._stopped:
                conn.close()
                return
            self._live.add(sock)
        try:
            # read=conn.recv may return fewer bytes than asked and returns
            # b"" when the peer closed; write is the locked sendall.
            handler(conn.peer_id, sock.recv, conn.write)
        except OSError as exc:
            logger.debug("stream %s ended: %s", conn.peer_id, exc)
        finally:
            with self._lock:
                self._live.discard(sock)
                if self._pool.get(conn.peer_id) is conn:
                    del self._pool[conn.peer_id]
            conn.close()
