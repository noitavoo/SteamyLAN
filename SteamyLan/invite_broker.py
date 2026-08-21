from __future__ import annotations

import json
import secrets
import threading
import time
from collections.abc import Callable

from .constants import INVITE_CHANNEL, PROTO_INVITE, PT_INVITE_DENIED, PT_INVITE_GRANTED, PT_INVITE_REQUEST
from .lobby_code import invite_request_proof
from .protocol import pack_packet, unpack_packet


class InviteBroker:
    def __init__(
        self,
        steam,
        logger,
        *,
        role: str,
        lobby_id: int,
        local_id: int,
        secret: bytes,
        static_secret: bytes | None = None,
        host_id: int = 0,
        on_granted: Callable | None = None,
        on_denied: Callable[[str], None] | None = None,
        on_invited: Callable[[int], None] | None = None,
    ):
        role = str(role).casefold()
        if role not in {"host", "client"}:
            raise ValueError("Invite broker role must be host or client.")
        self.steam = steam
        self.log = logger
        self.role = role
        self.lobby_id = int(lobby_id)
        self.local_id = int(local_id)
        self.host_id = int(host_id)
        self.secret = bytes(secret)
        self.static_secret = bytes(static_secret) if static_secret is not None else None
        self.on_granted = on_granted
        self.on_denied = on_denied
        self.on_invited = on_invited
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._request_thread: threading.Thread | None = None
        self._done = threading.Event()
        self._last_request: dict[int, float] = {}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._done.clear()
        self._thread = threading.Thread(target=self._rx_loop, name="SteamyLANInviteRx", daemon=True)
        self._thread.start()
        if self.role == "client":
            self._request_thread = threading.Thread(target=self._request_loop, name="SteamyLANInviteRequest", daemon=True)
            self._request_thread.start()

    def stop(self) -> None:
        self._stop.set()
        current = threading.current_thread()
        for thread in (self._thread, self._request_thread):
            if thread and thread is not current and thread.is_alive():
                thread.join(timeout=0.25)
        self._thread = None
        self._request_thread = None

    def _send(self, peer_id: int, kind: int, payload: bytes = b"") -> bool:
        if self._stop.is_set():
            return False
        try:
            return bool(
                self.steam.send_packet(
                    int(peer_id),
                    pack_packet(kind, PROTO_INVITE, 0, payload),
                    INVITE_CHANNEL,
                    reliable=True,
                )
            )
        except Exception:
            if not self._stop.is_set():
                self.log.debug("SteamyLAN invite rendezvous send failed", exc_info=True)
            return False

    def _request_payload(self) -> bytes:
        proof = invite_request_proof(self.secret, self.lobby_id, self.local_id)
        return json.dumps({"lobby": self.lobby_id, "proof": proof}, separators=(",", ":")).encode("ascii")

    def _request_loop(self) -> None:
        deadline = time.monotonic() + 30.0
        payload = self._request_payload()
        while not self._stop.is_set() and not self._done.is_set() and time.monotonic() < deadline:
            self._send(self.host_id, PT_INVITE_REQUEST, payload)
            self._done.wait(1.0)
        if not self._stop.is_set() and not self._done.is_set():
            self._done.set()
            if self.on_denied:
                try:
                    self.on_denied("The lobby invite request timed out. Check that the host is still online.")
                except Exception:
                    self.log.exception("Invite timeout callback failed")

    def _rx_loop(self) -> None:
        while not self._stop.is_set():
            try:
                items = self.steam.recv_packets(INVITE_CHANNEL, 32)
                if not items:
                    self._stop.wait(0.012)
                    continue
                for sender, raw in items:
                    parsed = unpack_packet(raw)
                    if parsed is None:
                        continue
                    kind, proto, stream_id, payload = parsed
                    if proto != PROTO_INVITE or stream_id != 0:
                        continue
                    if self.role == "host":
                        self._handle_host(int(sender), kind, payload)
                    else:
                        self._handle_client(int(sender), kind, payload)
            except Exception:
                if not self._stop.is_set():
                    self.log.exception("SteamyLAN invite rendezvous receive loop failed")
                    self._stop.wait(0.05)

    def _handle_host(self, sender: int, kind: int, payload: bytes) -> None:
        if kind != PT_INVITE_REQUEST or sender <= 0 or sender == self.local_id:
            return
        now = time.monotonic()
        previous = self._last_request.get(sender, 0.0)
        if now - previous < 0.5:
            return
        self._last_request[sender] = now
        if len(self._last_request) > 512:
            cutoff = now - 60.0
            self._last_request = {sid: stamp for sid, stamp in self._last_request.items() if stamp >= cutoff}
        try:
            raw = json.loads(payload.decode("ascii"))
            requested_lobby = int(raw.get("lobby", -1))
            proof = str(raw.get("proof", ""))
        except Exception:
            self._deny(sender)
            return
        valid = False
        if requested_lobby == self.lobby_id:
            expected = invite_request_proof(self.secret, self.lobby_id, sender)
            valid = secrets.compare_digest(proof, expected)
        elif requested_lobby == 0 and self.static_secret is not None:
            expected = invite_request_proof(self.static_secret, 0, sender)
            valid = secrets.compare_digest(proof, expected)
        if not valid:
            self._deny(sender)
            return
        try:
            limit = int(self.steam.lobby_member_limit(self.lobby_id) or 0)
            if limit and len(self.steam.lobby_members(self.lobby_id)) >= limit:
                self._deny(sender, "That lobby is full.")
                return
        except Exception:
            pass
        try:
            invited = bool(self.steam.invite_to_lobby(self.lobby_id, sender))
        except Exception:
            invited = False
            self.log.debug("Steam lobby invite failed", exc_info=True)
        if not invited:
            self._deny(sender, "Steam could not issue the lobby invite.")
            return
        if self.on_invited:
            try:
                self.on_invited(sender)
            except Exception:
                self.log.exception("Invite accepted callback failed")
        self._send(sender, PT_INVITE_GRANTED, str(self.lobby_id).encode("ascii"))

    def _deny(self, sender: int, reason: str = "That SteamyLAN share code was rejected.") -> None:
        self._send(sender, PT_INVITE_DENIED, reason.encode("utf-8")[:240])

    def _handle_client(self, sender: int, kind: int, payload: bytes) -> None:
        if sender != self.host_id or self._done.is_set():
            return
        if kind == PT_INVITE_GRANTED:
            self._done.set()
            try:
                granted_lobby = int(payload.decode("ascii").strip()) if payload else self.lobby_id
            except Exception:
                granted_lobby = self.lobby_id
            if self.on_granted:
                try:
                    self.on_granted(granted_lobby)
                except TypeError:
                    try:
                        self.on_granted()
                    except Exception:
                        self.log.exception("Invite granted callback failed")
                except Exception:
                    self.log.exception("Invite granted callback failed")
            return
        if kind == PT_INVITE_DENIED:
            self._done.set()
            try:
                reason = payload.decode("utf-8", errors="replace").strip()
            except Exception:
                reason = ""
            if self.on_denied:
                try:
                    self.on_denied(reason or "That SteamyLAN share code was rejected.")
                except Exception:
                    self.log.exception("Invite denied callback failed")
