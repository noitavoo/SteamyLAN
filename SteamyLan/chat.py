from __future__ import annotations

import json
import os
import struct
import threading
import time
from collections import defaultdict, deque
from collections.abc import Callable

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .constants import (
    CHAT_TEXT_MAX,
    PROTO_CHAT,
    PT_CHAT_HELLO,
    PT_CHAT_HELLO_ACK,
    PT_CHAT_MESSAGE,
)
from .protocol import pack_packet, unpack_packet


class ChatError(RuntimeError):
    pass


class EncryptedLobbyChat:
    """Pairwise-encrypted lobby chat transported over Steam Networking Messages.

    The host acts as the relay. Every client has an independent X25519/ChaCha20-
    Poly1305 link to the host, so messages are encrypted in transit even though
    Steam Networking Messages is used as the peer-to-peer carrier.
    """

    def __init__(
        self,
        steam,
        logger,
        *,
        role: str,
        channel: int,
        session_id: str,
        local_id: int,
        local_name: str,
        host_id: int = 0,
        on_message: Callable[[int, str, str, float], None] | None = None,
        on_ready: Callable[[int], None] | None = None,
    ):
        role = str(role).casefold()
        if role not in {"host", "client"}:
            raise ValueError("Chat role must be host or client.")
        self.steam = steam
        self.log = logger
        self.role = role
        self.channel = int(channel)
        self.session_id = str(session_id)
        self.local_id = int(local_id)
        self.local_name = str(local_name or "Steam user")[:120]
        self.host_id = int(host_id)
        self.on_message = on_message
        self.on_ready = on_ready

        self._private = X25519PrivateKey.generate()
        self._public = self._private.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        self._keys: dict[int, bytes] = {}
        self._authorized: set[int] = set()
        self._seen_nonces: dict[int, deque[bytes]] = defaultdict(deque)
        self._seen_nonce_sets: dict[int, set[bytes]] = defaultdict(set)
        self._stop = threading.Event()
        self._lock = threading.RLock()
        self._threads: list[threading.Thread] = []

    @property
    def ready(self) -> bool:
        if self.role == "client":
            with self._lock:
                return self.host_id in self._keys
        return True

    def start(self) -> None:
        self._stop.clear()
        self._spawn(self._rx_loop, f"SteamyLANChatRx-{self.channel}")
        if self.role == "client":



            self._send_plain(self.host_id, PT_CHAT_HELLO, self._public)
            self._spawn(self._hello_loop, f"SteamyLANChatHello-{self.channel}")

    def stop(self) -> None:
        self._stop.set()
        current = threading.current_thread()
        for thread in list(self._threads):
            if thread is not current and thread.is_alive():
                thread.join(timeout=0.2)
        self._threads.clear()
        with self._lock:
            self._keys.clear()
            self._authorized.clear()
            self._seen_nonces.clear()
            self._seen_nonce_sets.clear()

    def add_peer(self, steam_id: int) -> None:
        if self.role != "host":
            return
        sid = int(steam_id)
        if sid <= 0 or sid == self.local_id:
            return
        with self._lock:
            self._authorized.add(sid)




    def remove_peer(self, steam_id: int) -> None:
        sid = int(steam_id)
        with self._lock:
            self._authorized.discard(sid)
            self._keys.pop(sid, None)
            self._seen_nonces.pop(sid, None)
            self._seen_nonce_sets.pop(sid, None)

    def send_message(self, text: str) -> bool:
        text = " ".join(str(text or "").replace("\r", " ").replace("\n", " ").split())
        if not text:
            return False
        encoded = text.encode("utf-8")
        if len(encoded) > CHAT_TEXT_MAX:
            raise ChatError(f"Chat messages are limited to {CHAT_TEXT_MAX} UTF-8 bytes.")
        created = time.time()
        if self.role == "host":
            delivered = self._relay_from_host(self.local_id, self.local_name, text, created)
            self._emit(self.local_id, self.local_name, text, created)
            return delivered

        payload = json.dumps({"text": text, "time": created}, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        return self._send_encrypted(self.host_id, payload)

    def _spawn(self, fn, name: str) -> None:
        t = threading.Thread(target=fn, name=name, daemon=True)
        t.start()
        self._threads.append(t)

    def _hello_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                ready = self.host_id in self._keys
            if ready:
                return
            self._send_plain(self.host_id, PT_CHAT_HELLO, self._public)
            self._stop.wait(1.0)

    def _send_plain(self, peer_id: int, kind: int, payload: bytes) -> bool:
        if self._stop.is_set():
            return False
        try:
            return bool(
                self.steam.send_packet(
                    int(peer_id),
                    pack_packet(kind, PROTO_CHAT, 0, payload),
                    self.channel,
                    reliable=True,
                )
            )
        except Exception:
            if not self._stop.is_set():
                self.log.debug("SteamyLAN chat send failed", exc_info=True)
            return False

    def _derive(self, peer_id: int, peer_public: bytes) -> bytes:
        if len(peer_public) != 32:
            raise ValueError("Invalid X25519 public key.")
        remote = X25519PublicKey.from_public_bytes(peer_public)
        shared = self._private.exchange(remote)
        low, high = sorted((self.local_id, int(peer_id)))
        info = f"SteamyLAN-chat-v1:{low}:{high}".encode("ascii")
        return HKDF(
            algorithm=hashes.SHA256(),
            length=32,
            salt=self.session_id.encode("utf-8")[:128],
            info=info,
        ).derive(shared)

    def _aad(self, sender: int, recipient: int) -> bytes:
        return f"{self.session_id}:{self.channel}:{int(sender)}:{int(recipient)}".encode("utf-8")

    def _send_encrypted(self, peer_id: int, plaintext: bytes) -> bool:
        peer_id = int(peer_id)
        with self._lock:
            key = self._keys.get(peer_id)
        if key is None:
            if self.role == "client":
                self._send_plain(peer_id, PT_CHAT_HELLO, self._public)
            return False
        nonce = os.urandom(12)
        ciphertext = ChaCha20Poly1305(key).encrypt(nonce, plaintext, self._aad(self.local_id, peer_id))
        return self._send_plain(peer_id, PT_CHAT_MESSAGE, nonce + ciphertext)

    def _decrypt(self, peer_id: int, payload: bytes) -> bytes | None:
        if len(payload) < 12 + 16:
            return None
        peer_id = int(peer_id)
        with self._lock:
            key = self._keys.get(peer_id)
        if key is None:
            return None
        nonce, ciphertext = payload[:12], payload[12:]
        try:
            plaintext = ChaCha20Poly1305(key).decrypt(
                nonce,
                ciphertext,
                self._aad(peer_id, self.local_id),
            )
        except Exception:
            self.log.warning("Dropped an invalid encrypted lobby chat packet from %s", peer_id)
            return None



        with self._lock:
            seen = self._seen_nonce_sets[peer_id]
            if nonce in seen:
                self.log.warning("Dropped a replayed encrypted lobby chat packet from %s", peer_id)
                return None
            order = self._seen_nonces[peer_id]
            seen.add(nonce)
            order.append(nonce)
            while len(order) > 256:
                seen.discard(order.popleft())
        return plaintext

    def _rx_loop(self) -> None:
        while not self._stop.is_set():
            try:
                items = self.steam.recv_packets(self.channel, 32)
                if not items:
                    self._stop.wait(0.010)
                    continue
                for sender, raw in items:
                    sender = int(sender)
                    parsed = unpack_packet(raw)
                    if parsed is None:
                        continue
                    kind, proto, stream_id, payload = parsed
                    if proto != PROTO_CHAT or stream_id != 0:
                        continue
                    self._handle(sender, kind, payload)
            except Exception:
                if not self._stop.is_set():
                    self.log.exception("SteamyLAN encrypted chat receive loop failed")
                    self._stop.wait(0.05)

    def _handle(self, sender: int, kind: int, payload: bytes) -> None:
        if self.role == "host":
            with self._lock:
                allowed = sender in self._authorized
            if not allowed:
                return
            if kind == PT_CHAT_HELLO:
                try:
                    key = self._derive(sender, payload)
                except Exception:
                    return
                with self._lock:
                    first = sender not in self._keys
                    self._keys[sender] = key
                self._send_plain(sender, PT_CHAT_HELLO_ACK, self._public)
                if first and self.on_ready:
                    self.on_ready(sender)
                return
            if kind != PT_CHAT_MESSAGE:
                return
            plain = self._decrypt(sender, payload)
            if plain is None:
                return
            try:
                raw = json.loads(plain.decode("utf-8"))
                text = " ".join(str(raw.get("text", "")).replace("\r", " ").replace("\n", " ").split())


                created = time.time()
            except Exception:
                return
            if not text or len(text.encode("utf-8")) > CHAT_TEXT_MAX:
                return

            name = self._safe_name(sender)
            self._emit(sender, name, text, created)
            self._relay_from_host(sender, name, text, created)
            return

        if sender != self.host_id:
            return
        if kind == PT_CHAT_HELLO_ACK:
            try:
                key = self._derive(sender, payload)
            except Exception:
                return
            with self._lock:
                first = sender not in self._keys
                self._keys[sender] = key
            if first and self.on_ready:
                self.on_ready(sender)
            return
        if kind != PT_CHAT_MESSAGE:
            return
        plain = self._decrypt(sender, payload)
        if plain is None:
            return
        try:
            raw = json.loads(plain.decode("utf-8"))
            source = int(raw.get("sender", 0))
            name = " ".join(str(raw.get("name", "Steam user")).replace("\x00", " ").split())[:120] or "Steam user"
            text = " ".join(str(raw.get("text", "")).replace("\r", " ").replace("\n", " ").split())
            created = float(raw.get("time", time.time()))
        except Exception:
            return
        if source <= 0 or not text or len(text.encode("utf-8")) > CHAT_TEXT_MAX:
            return
        now = time.time()


        if created != created or abs(created - now) > 7 * 24 * 60 * 60:
            created = now
        self._emit(source, name, text, created)

    def _relay_from_host(self, sender_id: int, sender_name: str, text: str, created: float) -> bool:
        payload = json.dumps(
            {
                "sender": int(sender_id),
                "name": str(sender_name)[:120],
                "text": text,
                "time": float(created),
            },
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
        with self._lock:
            peers = tuple(self._authorized)
        delivered = False
        for sid in peers:
            delivered = self._send_encrypted(sid, payload) or delivered
        return delivered

    def _safe_name(self, steam_id: int) -> str:
        try:
            value = self.steam.friend_name(int(steam_id), refresh=True)
            if value:
                return " ".join(str(value).replace("\x00", " ").split())[:120]
        except TypeError:
            try:
                value = self.steam.friend_name(int(steam_id))
                if value:
                    return " ".join(str(value).replace("\x00", " ").split())[:120]
            except Exception:
                pass
        except Exception:
            pass
        return f"Steam {int(steam_id)}"

    def _emit(self, sender_id: int, sender_name: str, text: str, created: float) -> None:
        if self.on_message:
            try:
                self.on_message(int(sender_id), str(sender_name), str(text), float(created))
            except Exception:
                self.log.exception("SteamyLAN chat callback failed")
