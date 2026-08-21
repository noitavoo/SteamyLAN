from __future__ import annotations

"""The local socket <-> Steam Networking Messages transport.

Lobby and authorization policy deliberately live outside this module.  Each
engine owns one authenticated peer and one derived Steam channel, so no tunnel
can consume traffic belonging to another peer or mapped service.
"""

import queue
import secrets
import socket
import struct
import threading
import time
from collections.abc import Callable

from .constants import (
    PROTO_NONE, PROTO_TCP, PROTO_UDP, PT_ACCESS_REVOKED, PT_AUTH_DENIED,
    PT_AUTH_GRANTED, PT_AUTH_REQUEST, PT_CLOSE, PT_DATA, PT_DISCONNECTED,
    PT_CONFIG_UPDATE, PT_DISCONNECT_ACK, PT_HEARTBEAT, PT_HEARTBEAT_ACK,
    PT_HELLO, PT_OPEN_TCP, PT_UDP, TCP_CHUNK,
)
from .protocol import pack_packet, unpack_packet


class TunnelError(RuntimeError):
    pass


class ControlLink:
    """Reliable authorization transport with application-level P2P health."""

    _HEARTBEAT = struct.Struct("!Q")
    _HEARTBEAT_INTERVAL = 2.0
    _HEARTBEAT_TIMEOUT = 8.0

    def __init__(
        self, steam, logger, *, role: str, channel: int, peer_id: int = 0,
        on_request: Callable[[int, str], None] | None = None,
        on_granted: Callable[[], None] | None = None,
        on_denied: Callable[[str], None] | None = None,
        on_revoked: Callable[[str], None] | None = None,
        on_disconnected: Callable[[str], None] | None = None,
        on_disconnect_ack: Callable[[int], None] | None = None,
        on_config_update: Callable[[str], None] | None = None,
        on_health: Callable[[int, int, str], None] | None = None,
        auth_payload: str = "",
    ):
        if role not in {"host", "client"}:
            raise ValueError("role must be 'host' or 'client'")
        self.steam, self.log, self.role = steam, logger, role
        self.channel, self.peer_id = int(channel), int(peer_id)
        self.on_request, self.on_granted, self.on_denied = on_request, on_granted, on_denied
        self.on_revoked, self.on_disconnected = on_revoked, on_disconnected
        self.on_disconnect_ack = on_disconnect_ack
        self.on_config_update = on_config_update
        self.on_health = on_health
        self.auth_payload = str(auth_payload or "")[:512]
        self._stop, self._granted = threading.Event(), threading.Event()
        self._threads: list[threading.Thread] = []
        self._lock = threading.RLock()
        self._peers: set[int] = {self.peer_id} if self.role == "client" and self.peer_id > 0 else set()
        self._pending_heartbeats: dict[tuple[int, int], float] = {}
        self._last_seen: dict[int, float] = {}
        self._health: dict[int, tuple[int, str]] = {}

    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        self._spawn(self._receive, f"SteamyLANControlRx-{self.channel}")
        self._spawn(self._heartbeat_loop, f"SteamyLANHeartbeat-{self.channel}")
        if self.role == "client":
            self._report_health(self.peer_id, -1, "connecting")
            self._spawn(self._authorize, f"SteamyLANAuth-{self.channel}")

    def stop(self) -> None:
        self._stop.set()
        with self._lock:
            threads, self._threads = self._threads, []
        current = threading.current_thread()
        for thread in threads:
            if thread is not current and thread.is_alive():
                thread.join(timeout=0.25)
        with self._lock:
            self._peers.clear()
            self._pending_heartbeats.clear()
            self._last_seen.clear()
            self._health.clear()

    def reconnect(self) -> None:
        if self.role != "client" or self._stop.is_set():
            return
        self._granted.clear()
        self.add_peer(self.peer_id)
        self._report_health(self.peer_id, -1, "connecting")
        with self._lock:
            running = any(t.is_alive() and t.name == f"SteamyLANAuth-{self.channel}" for t in self._threads)
        if not running:
            self._spawn(self._authorize, f"SteamyLANAuth-{self.channel}")

    def add_peer(self, peer_id: int) -> None:
        peer_id = int(peer_id)
        if peer_id <= 0:
            return
        with self._lock:
            is_new = peer_id not in self._peers
            self._peers.add(peer_id)
        if is_new:
            self._report_health(peer_id, -1, "connecting")

    def remove_peer(self, peer_id: int) -> None:
        peer_id = int(peer_id)
        with self._lock:
            self._peers.discard(peer_id)
            self._last_seen.pop(peer_id, None)
            self._health.pop(peer_id, None)
            for key in tuple(self._pending_heartbeats):
                if key[0] == peer_id:
                    self._pending_heartbeats.pop(key, None)

    def _spawn(self, target, name: str, args: tuple = ()) -> None:
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        with self._lock:
            self._threads = [item for item in self._threads if item.is_alive()]
            self._threads.append(thread)
        thread.start()

    def _send_payload(self, peer_id: int, kind: int, payload: bytes = b"") -> bool:
        if self._stop.is_set():
            return False
        try:
            return bool(self.steam.send_packet(
                int(peer_id), pack_packet(kind, PROTO_NONE, 0, bytes(payload)),
                self.channel, reliable=True,
            ))
        except Exception:
            if not self._stop.is_set():
                self.log.debug("SteamyLAN control send failed", exc_info=True)
            return False

    def send(self, peer_id: int, kind: int, text: str = "") -> bool:
        payload = str(text or "").encode("utf-8", "replace")[:512]
        return self._send_payload(peer_id, kind, payload)

    def grant(self, peer_id: int) -> bool:
        self.add_peer(peer_id)
        return self.send(peer_id, PT_AUTH_GRANTED)

    def deny(self, peer_id: int, reason: str = "The host did not allow this connection.") -> bool:
        return self.send(peer_id, PT_AUTH_DENIED, reason)

    def revoke(self, peer_id: int, reason: str = "The host removed your access.") -> bool:
        return self.send(peer_id, PT_ACCESS_REVOKED, reason)

    def disconnect(self, peer_id: int, reason: str = "The host disconnected you.") -> bool:
        return self.send(peer_id, PT_DISCONNECTED, reason)

    def send_config(self, peer_id: int, config_json: str) -> bool:
        return self._send_payload(peer_id, PT_CONFIG_UPDATE, str(config_json or "").encode("utf-8"))

    def _authorize(self) -> None:
        while not self._stop.is_set() and not self._granted.is_set():
            self.send(self.peer_id, PT_AUTH_REQUEST, self.auth_payload)
            self._stop.wait(1.5)

    def _report_health(self, peer_id: int, ping_ms: int, state: str) -> None:
        peer_id, ping_ms, state = int(peer_id), int(ping_ms), str(state)
        current = (ping_ms, state)
        with self._lock:
            previous = self._health.get(peer_id)
            self._health[peer_id] = current
        if self.on_health and previous != current:
            try:
                self.on_health(peer_id, ping_ms, state)
            except Exception:
                self.log.exception("SteamyLAN health callback failed")

    def _mark_seen(self, peer_id: int, ping_ms: int | None = None) -> None:
        peer_id = int(peer_id)
        with self._lock:
            self._last_seen[peer_id] = time.monotonic()
            previous_ping = self._health.get(peer_id, (-1, "connecting"))[0]
        self._report_health(peer_id, previous_ping if ping_ms is None else ping_ms, "connected")

    def _heartbeat_loop(self) -> None:
        # Steam posts no success event for an implicit message session. A
        # matching peer reply is therefore the only authoritative ready signal.
        while not self._stop.is_set():
            now = time.monotonic()
            with self._lock:
                peers = tuple(self._peers)
                for key, sent in tuple(self._pending_heartbeats.items()):
                    if now - sent > self._HEARTBEAT_TIMEOUT:
                        self._pending_heartbeats.pop(key, None)
                last_seen = dict(self._last_seen)
                health = dict(self._health)
            for peer_id in peers:
                seen = last_seen.get(peer_id)
                if seen is not None and now - seen > self._HEARTBEAT_TIMEOUT:
                    self._report_health(peer_id, health.get(peer_id, (-1, "connecting"))[0], "unresponsive")
                nonce = secrets.randbits(64)
                if self._send_payload(peer_id, PT_HEARTBEAT, self._HEARTBEAT.pack(nonce)):
                    with self._lock:
                        self._pending_heartbeats[(peer_id, nonce)] = time.monotonic()
                elif seen is None:
                    self._report_health(peer_id, -1, "connecting")
            self._stop.wait(self._HEARTBEAT_INTERVAL)

    def _receive(self) -> None:
        while not self._stop.is_set():
            try:
                packets = self.steam.recv_packets(self.channel, 32)
                if not packets:
                    self._stop.wait(0.01)
                    continue
                for sender, wire in packets:
                    sender = int(sender)
                    parsed = unpack_packet(wire)
                    if parsed is None:
                        continue
                    kind, proto, stream_id, payload = parsed
                    if proto != PROTO_NONE or stream_id:
                        continue
                    if kind == PT_HEARTBEAT and len(payload) == self._HEARTBEAT.size:
                        self._send_payload(sender, PT_HEARTBEAT_ACK, payload)
                        self._mark_seen(sender)
                        continue
                    if kind == PT_HEARTBEAT_ACK and len(payload) == self._HEARTBEAT.size:
                        nonce = self._HEARTBEAT.unpack(payload)[0]
                        with self._lock:
                            sent = self._pending_heartbeats.pop((sender, nonce), None)
                        if sent is not None:
                            ping_ms = max(0, min(60_000, round((time.monotonic() - sent) * 1000)))
                            self._mark_seen(sender, ping_ms)
                        continue
                    text = payload.decode("utf-8", "replace").strip()
                    if self.role == "host":
                        if kind == PT_AUTH_REQUEST and self.on_request:
                            self.add_peer(sender)
                            self._mark_seen(sender)
                            self.on_request(sender, text)
                        elif kind == PT_DISCONNECT_ACK and self.on_disconnect_ack:
                            self.on_disconnect_ack(sender)
                    elif sender == self.peer_id:
                        self._mark_seen(sender)
                        if kind == PT_AUTH_GRANTED:
                            self._granted.set()
                            if self.on_granted:
                                self.on_granted()
                        elif kind == PT_AUTH_DENIED and self.on_denied:
                            self.on_denied(text or "The host did not allow this connection.")
                        elif kind == PT_ACCESS_REVOKED and self.on_revoked:
                            self.on_revoked(text or "The host removed your access.")
                        elif kind == PT_DISCONNECTED:
                            self.send(self.peer_id, PT_DISCONNECT_ACK)
                            if self.on_disconnected:
                                self.on_disconnected(text or "The host disconnected you.")
                        elif kind == PT_CONFIG_UPDATE and self.on_config_update:
                            self.on_config_update(payload.decode("utf-8", "replace"))
            except Exception:
                if not self._stop.is_set():
                    self.log.exception("SteamyLAN control receive failed")
                    self._stop.wait(0.05)


class TunnelEngine:
    """One fixed local TCP/UDP endpoint mapped to one authenticated Steam peer."""

    _MAX_TCP = 128
    _MAX_UDP = 256
    _QUEUE_SIZE = 128
    _UDP_IDLE = 300.0

    def __init__(
        self, steam, logger, *, role: str, protocol: str, peer_id: int, channel: int,
        target_host: str, target_port: int, bind_host: str = "127.0.0.1",
        bind_port: int | None = None, on_activity: Callable[[int], None] | None = None,
    ):
        self.steam, self.log, self.role = steam, logger, role
        self.protocol, self.peer_id, self.channel = protocol.upper(), int(peer_id), int(channel)
        self.target_host, self.target_port = str(target_host), int(target_port)
        self.bind_host, self.bind_port = str(bind_host), int(target_port if bind_port is None else bind_port)
        self.on_activity = on_activity
        if role not in {"host", "client"} or self.protocol not in {"TCP", "UDP"}:
            raise ValueError("invalid tunnel role or protocol")
        if not 1 <= self.target_port <= 65535 or not 1 <= self.bind_port <= 65535:
            raise ValueError("port out of range")
        self._stop, self._lock = threading.Event(), threading.RLock()
        self._threads: list[threading.Thread] = []
        self._listener: socket.socket | None = None
        self._next_stream = 1
        self._tcp: dict[int, socket.socket] = {}
        self._writes: dict[int, queue.Queue] = {}
        self._connecting: set[int] = set()
        self._cancelled: set[int] = set()
        self._udp: dict[int, socket.socket] = {}
        self._clients: dict[int, tuple] = {}
        self._by_address: dict[tuple, int] = {}
        self._seen: dict[int, float] = {}

    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        if self.role == "client":
            self._listener = self._open_listener()
        self._spawn(self._receive, f"SteamyLANTunnelRx-{self.channel}")
        if self.role == "client":
            target = self._client_tcp if self.protocol == "TCP" else self._client_udp
            self._spawn(target, f"SteamyLANLocal-{self.protocol}-{self.bind_port}")

    def stop(self) -> None:
        self._stop.set()
        listener, self._listener = self._listener, None
        self._close(listener)
        with self._lock:
            sockets = [*self._tcp.values(), *self._udp.values()]
            queues = list(self._writes.values())
            threads, self._threads = self._threads, []
            self._tcp.clear(); self._writes.clear(); self._connecting.clear(); self._cancelled.clear()
            self._udp.clear(); self._clients.clear(); self._by_address.clear(); self._seen.clear()
        for item in queues: self._offer(item, None)
        for item in sockets: self._close(item)
        current = threading.current_thread()
        for thread in threads:
            if thread is not current and thread.is_alive():
                thread.join(timeout=0.25)

    def _open_listener(self) -> socket.socket:
        family = socket.AF_INET6 if ":" in self.bind_host else socket.AF_INET
        kind = socket.SOCK_STREAM if self.protocol == "TCP" else socket.SOCK_DGRAM
        listener = socket.socket(family, kind)
        try:
            if self.protocol == "TCP":
                listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            listener.bind((self.bind_host, self.bind_port))
            if self.protocol == "TCP": listener.listen(64)
            listener.settimeout(0.5)
            return listener
        except OSError as exc:
            listener.close()
            raise TunnelError(f"Could not open local {self.protocol} endpoint: {exc}") from exc

    def _spawn(self, target, name: str, args: tuple = ()) -> None:
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        with self._lock:
            self._threads = [item for item in self._threads if item.is_alive()]
            self._threads.append(thread)
        thread.start()

    def _new_stream(self) -> int:
        with self._lock:
            stream_id = self._next_stream
            self._next_stream = 1 if stream_id >= 0x7fffffff else stream_id + 1
            return stream_id

    def _send(self, kind: int, proto: int, stream_id: int, payload: bytes = b"", *, reliable: bool = True) -> bool:
        if self._stop.is_set(): return False
        try:
            return bool(self.steam.send_packet(self.peer_id, pack_packet(kind, proto, stream_id, payload), self.channel, reliable=reliable))
        except Exception:
            if not self._stop.is_set(): self.log.debug("SteamyLAN tunnel send failed", exc_info=True)
            return False

    def _receive(self) -> None:
        while not self._stop.is_set():
            try:
                packets = self.steam.recv_packets(self.channel, 64)
                if not packets:
                    self._stop.wait(0.01)
                    continue
                active = False
                for sender, wire in packets:
                    if int(sender) != self.peer_id: continue
                    parsed = unpack_packet(wire)
                    if parsed is None: continue
                    active = True
                    self._handle(*parsed)
                if active and self.on_activity: self.on_activity(self.peer_id)
            except Exception:
                if not self._stop.is_set():
                    self.log.exception("SteamyLAN tunnel receive failed")
                    self._stop.wait(0.05)

    def _handle(self, kind: int, proto: int, stream_id: int, payload: bytes) -> None:
        if kind == PT_HELLO: return
        if not 1 <= int(stream_id) <= 0x7fffffff: return
        if self.role == "host":
            if kind == PT_OPEN_TCP and proto == PROTO_TCP: self._open_target_tcp(stream_id)
            elif kind == PT_DATA and proto == PROTO_TCP: self._queue_tcp(stream_id, payload)
            elif kind == PT_CLOSE and proto == PROTO_TCP: self._remote_close_tcp(stream_id)
            elif kind == PT_UDP and proto == PROTO_UDP: self._target_udp(stream_id, payload)
        elif kind == PT_DATA and proto == PROTO_TCP: self._queue_tcp(stream_id, payload)
        elif kind == PT_CLOSE and proto == PROTO_TCP: self._remote_close_tcp(stream_id)
        elif kind == PT_UDP and proto == PROTO_UDP: self._reply_udp(stream_id, payload)

    def _client_tcp(self) -> None:
        listener = self._listener
        if listener is None: return
        while not self._stop.is_set():
            try: sock, _ = listener.accept()
            except socket.timeout: continue
            except OSError: return
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            stream_id, writes = self._new_stream(), queue.Queue(self._QUEUE_SIZE)
            with self._lock: self._tcp[stream_id], self._writes[stream_id] = sock, writes
            if not self._send(PT_OPEN_TCP, PROTO_TCP, stream_id):
                self._close_tcp(stream_id, notify=False)
            else:
                self._start_tcp(stream_id, sock, writes)

    def _open_target_tcp(self, stream_id: int) -> None:
        with self._lock:
            if stream_id in self._tcp or stream_id in self._connecting: return
            if len(self._tcp) + len(self._connecting) >= self._MAX_TCP:
                self._send(PT_CLOSE, PROTO_TCP, stream_id, b"too many streams"); return
            self._connecting.add(stream_id); self._writes[stream_id] = queue.Queue(self._QUEUE_SIZE)
        self._spawn(self._connect_target_tcp, f"SteamyLANTCPConnect-{stream_id}", (stream_id,))

    def _connect_target_tcp(self, stream_id: int) -> None:
        sock = None
        try:
            sock = socket.create_connection((self.target_host, self.target_port), timeout=8.0)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        except OSError:
            self.log.warning("Local TCP target %s:%s is unavailable", self.target_host, self.target_port)
            self._close_tcp(stream_id, notify=True)
            return
        finally:
            with self._lock: self._connecting.discard(stream_id)
        with self._lock:
            writes = self._writes.get(stream_id)
            cancelled = self._stop.is_set() or stream_id in self._cancelled or writes is None
            self._cancelled.discard(stream_id)
            if not cancelled: self._tcp[stream_id] = sock
        if cancelled:
            self._close(sock)
        else:
            self._start_tcp(stream_id, sock, writes)

    def _start_tcp(self, stream_id: int, sock: socket.socket, writes: queue.Queue) -> None:
        self._spawn(self._read_tcp, f"SteamyLANTCPRead-{stream_id}", (stream_id, sock))
        self._spawn(self._write_tcp, f"SteamyLANTCPWrite-{stream_id}", (stream_id, sock, writes))

    def _read_tcp(self, stream_id: int, sock: socket.socket) -> None:
        try:
            while not self._stop.is_set():
                payload = sock.recv(TCP_CHUNK)
                if not payload or not self._send(PT_DATA, PROTO_TCP, stream_id, payload): break
        except OSError: pass
        finally: self._close_tcp(stream_id, notify=True)

    def _write_tcp(self, stream_id: int, sock: socket.socket, writes: queue.Queue) -> None:
        while not self._stop.is_set():
            try: payload = writes.get(timeout=0.5)
            except queue.Empty: continue
            if payload is None:
                try: sock.shutdown(socket.SHUT_WR)
                except OSError: pass
                self._close(sock)
                return
            try: sock.sendall(payload)
            except OSError:
                self._close_tcp(stream_id, notify=True)
                return

    def _queue_tcp(self, stream_id: int, payload: bytes) -> None:
        with self._lock: writes = self._writes.get(stream_id)
        if writes is not None and not self._offer(writes, bytes(payload)):
            self.log.warning("Closing backpressured TCP stream %s", stream_id)
            self._close_tcp(stream_id, notify=True)

    def _remote_close_tcp(self, stream_id: int) -> None:
        with self._lock:
            if stream_id in self._connecting: self._cancelled.add(stream_id)
            sock, writes = self._tcp.pop(stream_id, None), self._writes.pop(stream_id, None)
        if sock:
            try: sock.shutdown(socket.SHUT_RD)
            except OSError: pass
        if writes is not None and not self._offer(writes, None): self._close(sock)

    def _close_tcp(self, stream_id: int, *, notify: bool) -> None:
        with self._lock:
            if stream_id in self._connecting: self._cancelled.add(stream_id)
            sock, writes = self._tcp.pop(stream_id, None), self._writes.pop(stream_id, None)
        existed = sock is not None or writes is not None
        if writes is not None: self._offer(writes, None)
        self._close(sock)
        if notify and existed and not self._stop.is_set(): self._send(PT_CLOSE, PROTO_TCP, stream_id)

    def _client_udp(self) -> None:
        listener = self._listener
        if listener is None: return
        while not self._stop.is_set():
            try: payload, address = listener.recvfrom(65535)
            except socket.timeout:
                self._expire_udp(); continue
            except OSError: return
            now = time.monotonic(); self._expire_udp(now)
            with self._lock:
                stream_id = self._by_address.get(address)
                if stream_id is None:
                    if len(self._by_address) >= self._MAX_UDP: continue
                    stream_id = self._new_stream()
                    self._by_address[address], self._clients[stream_id] = stream_id, address
                self._seen[stream_id] = now
            self._send(PT_UDP, PROTO_UDP, stream_id, payload, reliable=False)

    def _target_udp(self, stream_id: int, payload: bytes) -> None:
        with self._lock: sock = self._udp.get(stream_id)
        if sock is None:
            with self._lock:
                if len(self._udp) >= self._MAX_UDP: return
            family = socket.AF_INET6 if ":" in self.target_host else socket.AF_INET
            try:
                candidate = socket.socket(family, socket.SOCK_DGRAM)
                candidate.connect((self.target_host, self.target_port)); candidate.settimeout(0.5)
            except OSError:
                self.log.debug("Could not create local UDP target", exc_info=True); return
            with self._lock:
                sock = self._udp.setdefault(stream_id, candidate)
                created = sock is candidate
            if created: self._spawn(self._read_target_udp, f"SteamyLANUDPRead-{stream_id}", (stream_id, sock))
            else: self._close(candidate)
        try:
            sock.send(payload)
            with self._lock: self._seen[stream_id] = time.monotonic()
        except OSError: pass

    def _read_target_udp(self, stream_id: int, sock: socket.socket) -> None:
        try:
            while not self._stop.is_set():
                try: payload = sock.recv(65535)
                except socket.timeout:
                    with self._lock: idle = time.monotonic() - self._seen.get(stream_id, 0.0)
                    if idle > self._UDP_IDLE: return
                    continue
                except OSError: return
                with self._lock: self._seen[stream_id] = time.monotonic()
                self._send(PT_UDP, PROTO_UDP, stream_id, payload, reliable=False)
        finally:
            with self._lock:
                if self._udp.get(stream_id) is sock: self._udp.pop(stream_id, None)
                self._seen.pop(stream_id, None)
            self._close(sock)

    def _reply_udp(self, stream_id: int, payload: bytes) -> None:
        with self._lock: address, listener = self._clients.get(stream_id), self._listener
        if address is None or listener is None: return
        try: listener.sendto(payload, address)
        except OSError: pass

    def _expire_udp(self, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        with self._lock:
            expired = [sid for sid, seen in self._seen.items() if sid in self._clients and now - seen > self._UDP_IDLE]
            for stream_id in expired:
                address = self._clients.pop(stream_id, None); self._seen.pop(stream_id, None)
                if address is not None and self._by_address.get(address) == stream_id: self._by_address.pop(address, None)

    @staticmethod
    def _offer(target: queue.Queue, value) -> bool:
        try: target.put_nowait(value); return True
        except queue.Full: return False

    @staticmethod
    def _close(sock: socket.socket | None) -> None:
        if sock is None: return
        try: sock.shutdown(socket.SHUT_RDWR)
        except OSError: pass
        try: sock.close()
        except OSError: pass
