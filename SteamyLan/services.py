from __future__ import annotations

import secrets
import threading
import time
import uuid
from collections import defaultdict
from dataclasses import replace

from PySide6.QtCore import QObject, QThreadPool, QTimer, Signal, Slot

from .constants import (
    AUTO_DISCOVERY_INTERVAL_MS,
    CALLBACK_LOBBY_CREATED,
    CALLBACK_LOBBY_ENTER,
    CALLBACK_LOBBY_MATCH_LIST,
    CHANNEL_MAX,
    CHANNEL_MIN,
    CHAT_CHANNEL_MAX,
    CHAT_CHANNEL_MIN,
    CHAT_ROOM_ENTER_SUCCESS,
    CONTROL_CHANNEL_MAX,
    CONTROL_CHANNEL_MIN,
    ERESULT_OK,
    LOBBY_DATA_CONFIG_KEY,
    LOBBY_DATA_INVITE_HASH_KEY,
    LOBBY_DATA_STATIC_HASH_KEY,
    LOBBY_DATA_MAX_KEY,
    LOBBY_DATA_MEMBER_COUNT_KEY,
    LOBBY_DATA_NAME_KEY,
    LOBBY_DATA_MARKER_KEY,
    LOBBY_DATA_MARKER_VALUE,
    LOBBY_DATA_VERSION_KEY,
    LOBBY_DATA_VISIBILITY_KEY,
    LOBBY_FRIENDS_ONLY,
    LOBBY_PRIVATE,
    LOBBY_PUBLIC,
    MEMBER_SCAN_INTERVAL_MS,
    PROTOCOL_VERSION,
    PUBLIC_LOBBY_RESULT_LIMIT,
    VISIBILITY_FRIENDS,
    VISIBILITY_INVITE,
    VISIBILITY_PUBLIC,
)
from .chat import ChatError, EncryptedLobbyChat
from .invite_broker import InviteBroker
from .detector import ServiceDetector
from .models import (
    AppSnapshot,
    ChatMessage,
    DetectedService,
    Endpoint,
    FriendInfo,
    LocalMapping,
    PeerState,
    SessionConfig,
    SharedServiceSpec,
    SharingHost,
)
from .lobby_code import invite_proof, invite_secret_hash, make_invite_code, new_invite_secret, parse_invite_code
from .lobby_password import (
    derive_password_key,
    make_auth_payload,
    new_password_salt,
    parse_auth_payload,
    password_proof,
)
from .settings import AccessStore, PreferenceStore
from .peer_guard import LobbyMembershipGuard
from .steam_api import LobbyCreated, LobbyEnter, LobbyMatchList, SteamClient
from .tunnel import ControlLink, TunnelEngine
from .util import bind_available, derive_peer_channel, find_free_port, target_host_for
from .workers import FunctionWorker


class DetectionService(QObject):
    updated = Signal(object)
    failed = Signal(str)

    def __init__(self, logger, parent=None):
        super().__init__(parent)
        self.log = logger
        self.detector = ServiceDetector(logger)
        self.pool = QThreadPool.globalInstance()
        self._running = False
        self._again = False
        self._last_services: tuple[DetectedService, ...] = ()
        self._failure_active = False

    def refresh(self) -> None:
        if self._running:
            self._again = True
            return
        self._running = True
        worker = FunctionWorker(self.detector.scan)
        worker.signals.result.connect(self._result)
        worker.signals.error.connect(self._error)
        worker.signals.finished.connect(self._finished)
        self.pool.start(worker)


    @Slot(object)
    def _result(self, services) -> None:
        self._failure_active = False
        rows = tuple(services or ())
        if rows == self._last_services:
            return
        self._last_services = rows
        self.updated.emit(rows)

    @Slot(str)
    def _error(self, text: str) -> None:
        self.log.error("Service discovery failed\n%s", text)
        if not self._failure_active:
            self._failure_active = True
            self.failed.emit("Couldn't look for running servers right now.")

    @Slot()
    def _finished(self) -> None:
        self._running = False
        if self._again:
            self._again = False
            QTimer.singleShot(250, self.refresh)


class SteamService(QObject):
    ready = Signal(str, object)
    failed = Signal(str)
    friendsUpdated = Signal(object)
    hostsUpdated = Signal(object)
    lobbiesUpdated = Signal(object)
    lobbyEntered = Signal(object, int)
    lobbyInviteReceived = Signal(object, str)
    joinRequested = Signal(object, str)
    networkingSessionRequested = Signal(object)
    networkingSessionFailed = Signal(object, int, str)
    warning = Signal(str)

    _friendsDirty = Signal()

    def __init__(self, steam: SteamClient, logger, parent=None):
        super().__init__(parent)
        self.steam = steam
        self.log = logger
        self.pool = QThreadPool.globalInstance()
        self._friend_refresh_running = False
        self._friend_refresh_again = False
        self._avatar_refresh_running = False
        self._avatar_refresh_pending: set[int] = set()
        self._discovery_running = False
        self._public_discovery_running = False
        self._friends: dict[int, FriendInfo] = {}
        self._hosts: list[SharingHost] = []
        self._public_lobbies: list[SharingHost] = []
        self._lobby_discovery_active = True
        self._initializing = False
        self._last_dll_path: str | None = None
        self._last_init_message = ""
        self._shutting_down = False

        self._friendsDirty.connect(self._schedule_friend_refresh)
        self.steam.add_event_handler(self._steam_event)

        self._friend_timer = QTimer(self)
        self._friend_timer.setInterval(60_000)
        self._friend_timer.timeout.connect(self.refresh_friends)

        self._discover_timer = QTimer(self)
        self._discover_timer.setInterval(AUTO_DISCOVERY_INTERVAL_MS)
        self._discover_timer.timeout.connect(self.refresh_lobbies)

        self._dirty_timer = QTimer(self)
        self._dirty_timer.setSingleShot(True)
        self._dirty_timer.setInterval(300)
        self._dirty_timer.timeout.connect(self.refresh_friends)

    @property
    def initialized(self) -> bool:
        return self.steam.initialized

    @property
    def friend_ids(self) -> set[int]:
        return set(self._friends)

    @property
    def friends(self) -> tuple[FriendInfo, ...]:
        return tuple(self._friends.values())

    def friend_name(self, steam_id: int) -> str:
        info = self._friends.get(int(steam_id))
        if info:
            return info.name
        try:
            name = self.steam.friend_name(int(steam_id), refresh=True)
            if name:
                return str(name)
        except TypeError:
            try:
                name = self.steam.friend_name(int(steam_id))
                if name:
                    return str(name)
            except Exception:
                pass
        except Exception:
            pass
        return f"Steam {int(steam_id)}"

    def friend_avatar(self, steam_id: int) -> tuple[bytes, int, int]:
        info = self._friends.get(int(steam_id))
        if info and info.avatar_rgba:
            return info.avatar_rgba, info.avatar_width, info.avatar_height
        try:
            return self.steam.avatar_rgba(int(steam_id))
        except Exception:
            return b"", 0, 0

    def hydrate_friend_avatars(self, steam_ids) -> None:
        if not self.steam.initialized:
            return
        for value in steam_ids or ():
            sid = int(value)
            info = self._friends.get(sid)
            if info is not None and not info.avatar_rgba:
                self._avatar_refresh_pending.add(sid)
        self._start_avatar_refresh()

    def _start_avatar_refresh(self) -> None:
        if self._avatar_refresh_running or not self._avatar_refresh_pending or not self.steam.initialized:
            return
        steam_ids = tuple(self._avatar_refresh_pending)
        self._avatar_refresh_pending.clear()
        self._avatar_refresh_running = True

        def load():
            result = {}
            for sid in steam_ids:
                try:
                    result[int(sid)] = self.steam.avatar_rgba(int(sid))
                except Exception:
                    continue
            return result

        worker = FunctionWorker(load)
        worker.signals.result.connect(self._avatar_result)
        worker.signals.error.connect(lambda text: self.log.debug("Friend avatar refresh failed: %s", text))
        worker.signals.finished.connect(self._avatar_finished)
        self.pool.start(worker)

    @Slot(object)
    def _avatar_result(self, avatars) -> None:
        changed = False
        for sid, value in dict(avatars or {}).items():
            info = self._friends.get(int(sid))
            if info is None:
                continue
            try:
                rgba, width, height = value
            except Exception:
                continue
            if not rgba:
                continue
            updated = replace(
                info,
                avatar_rgba=bytes(rgba),
                avatar_width=int(width),
                avatar_height=int(height),
            )
            if updated != info:
                self._friends[int(sid)] = updated
                changed = True
        if changed:
            self.friendsUpdated.emit(list(self._friends.values()))

    @Slot()
    def _avatar_finished(self) -> None:
        self._avatar_refresh_running = False
        if self._avatar_refresh_pending:
            QTimer.singleShot(0, self._start_avatar_refresh)

    def peer_network_stats(self, steam_id: int) -> tuple[int, float, float]:
        try:
            return self.steam.peer_network_stats(int(steam_id))
        except Exception:
            return -1, 0.0, 0.0

    def initialize(self, dll_path: str | None = None) -> None:
        """Perform one background Steam startup handshake.

        Temporary client/startup states stay inside the worker. The UI therefore
        never enters a retry cycle and no user action is required when Steam is
        merely taking a few seconds to start or sign in.
        """
        if self._shutting_down or self._initializing:
            return
        if dll_path:
            self._last_dll_path = dll_path
        self._initializing = True
        worker = FunctionWorker(self._initialize_until_ready, dll_path or self._last_dll_path)
        worker.signals.result.connect(self._init_ready_result)
        worker.signals.error.connect(self._init_error)
        worker.signals.finished.connect(self._init_finished)
        self.pool.start(worker)

    def _initialize_until_ready(self, dll_path: str | None = None):
        if not self.steam.initialized:
            self.steam.initialize(dll_path)





        while not self._shutting_down:
            if self.steam.logged_on():
                # The first networking-config pass can run while Steam is still
                # signing in. Re-prime SDR after BLoggedOn becomes authoritative
                # so the first remote peer does not pay the relay setup delay.
                self.steam.prime_networking()
                return self.steam.persona_name(), self.steam.steam_id()
            time.sleep(0.25)
        return None

    def _emit_init_message(self, message: str) -> None:
        message = str(message or "")
        if message == self._last_init_message:
            return
        self._last_init_message = message
        self.failed.emit(message)

    @Slot(object)
    def _init_ready_result(self, result) -> None:
        if not result or self._shutting_down:
            return
        name, steam_id = result
        self._last_init_message = ""
        self.ready.emit(str(name), int(steam_id))
        self._friend_timer.start()
        if self._lobby_discovery_active:
            self._discover_timer.start()
        self.refresh_friends()
        if self._lobby_discovery_active:
            QTimer.singleShot(500, self.refresh_lobbies)

    @Slot()
    def _init_finished(self) -> None:
        self._initializing = False

    @Slot(str)
    def _init_error(self, text: str) -> None:
        self.log.error("Steam initialization failed\n%s", text)
        lowered = (text or "").casefold()
        if "steam_api64.dll was not found" in lowered:
            self._emit_init_message("SteamyLAN needs your official 64-bit steam_api64.dll beside the app.")
            return
        if "windows could not load" in lowered or "no supported steam initialization export" in lowered:
            self._emit_init_message("SteamyLAN found steam_api64.dll, but it isn't a compatible 64-bit Steamworks DLL.")
            return
        if "version mismatch" in lowered or "result_code=3" in lowered:
            self._emit_init_message("Steam is running, but steam_api64.dll doesn't match the installed Steam client. Replace it with the current official 64-bit Steamworks DLL.")
            return
        if "same windows user" in lowered or "user context" in lowered or "administrator" in lowered:
            self._emit_init_message("Steam and SteamyLAN must run under the same Windows account and privilege level. Run both applications under the same Windows account and privilege level.")
            return
        if "callback pipe" in lowered or "required steam export" in lowered or "interface" in lowered:
            self._emit_init_message("SteamyLAN connected to Steam, but this steam_api64.dll is not compatible with the Steam interfaces SteamyLAN needs.")
            return




        self._emit_init_message("Steam could not finish initializing. Make sure Steam is signed in under the same Windows account as SteamyLAN.")

    def refresh_friends(self) -> None:
        if not self.steam.initialized:
            return
        if self._friend_refresh_running:
            self._friend_refresh_again = True
            return
        self._friend_refresh_running = True
        worker = FunctionWorker(self.steam.friends_list)
        worker.signals.result.connect(self._friend_result)
        worker.signals.error.connect(lambda text: self.log.error("Friend refresh failed\n%s", text))
        worker.signals.finished.connect(self._friend_finished)
        self.pool.start(worker)

    @Slot(object)
    def _friend_result(self, friends) -> None:
        rows = list(friends or [])
        new_friends = {int(f.steam_id): f for f in rows}
        changed = new_friends != self._friends
        self._friends = new_friends
        if changed:
            self.friendsUpdated.emit(rows)
            if self._lobby_discovery_active:
                QTimer.singleShot(150, self.refresh_lobbies)

    @Slot()
    def _friend_finished(self) -> None:
        self._friend_refresh_running = False
        if self._friend_refresh_again:
            self._friend_refresh_again = False
            QTimer.singleShot(500, self.refresh_friends)

    @Slot()
    def _schedule_friend_refresh(self) -> None:
        self._dirty_timer.start()

    def set_lobby_discovery_active(self, active: bool) -> None:
        active = bool(active)
        changed = active != self._lobby_discovery_active
        self._lobby_discovery_active = active
        if not self.steam.initialized:
            return
        if active:
            if not self._discover_timer.isActive():
                self._discover_timer.start()
            if changed:
                QTimer.singleShot(0, self.refresh_lobbies)
        elif self._discover_timer.isActive():
            self._discover_timer.stop()

    def refresh_lobbies(self) -> None:
        if not self._lobby_discovery_active:
            return
        self.discover_hosts()
        self.discover_public_lobbies()

    def _load_lobby(self, lobby_id: int, *, expected_owner: int = 0, timeout: float = 2.0) -> SharingHost | None:
        lobby_id = int(lobby_id)
        if lobby_id <= 0:
            return None
        try:
            self.steam.request_lobby_data(lobby_id)
        except Exception:
            pass
        deadline = time.monotonic() + max(0.2, timeout)
        while time.monotonic() < deadline:
            try:
                marker = self.steam.get_lobby_data(lobby_id, LOBBY_DATA_MARKER_KEY)
                text = self.steam.get_lobby_data(lobby_id, LOBBY_DATA_CONFIG_KEY)
                if marker != LOBBY_DATA_MARKER_VALUE or not text:
                    time.sleep(0.08)
                    continue
                config = SessionConfig.from_json(text)




                if expected_owner and config.host_id != int(expected_owner):
                    return None
                count_text = self.steam.get_lobby_data(lobby_id, LOBBY_DATA_MEMBER_COUNT_KEY)
                try:
                    member_count = max(1, min(config.max_members, int(count_text or 1)))
                except (TypeError, ValueError):
                    member_count = 1
                return SharingHost(
                    lobby_id=lobby_id,
                    host_id=config.host_id,
                    host_name=config.host_name or self.friend_name(config.host_id),
                    services=config.services,
                    session_id=config.session_id,
                    lobby_name=config.lobby_name,
                    visibility=config.visibility,
                    max_members=config.max_members,
                    member_count=member_count,
                    password_protected=bool(config.password_salt),
                )
            except Exception:
                time.sleep(0.08)
        return None

    def discover_hosts(self) -> None:
        if not self.steam.initialized or self._discovery_running:
            return
        self._discovery_running = True

        def load() -> list[SharingHost]:
            candidates: set[int] = set()
            for sid in self.friend_ids:
                try:
                    game = self.steam.friend_game_lobby(sid)
                    if not game:
                        continue
                    app_id, lobby_id = game
                    if app_id == self.steam.app_id and lobby_id:
                        candidates.add(int(lobby_id))
                except Exception:
                    continue
            result: list[SharingHost] = []
            for lobby_id in candidates:
                host = self._load_lobby(lobby_id, timeout=0.6)
                if host is not None and self.steam.is_immediate_friend(host.host_id):
                    result.append(host)
            result.sort(key=lambda h: (h.lobby_name.casefold(), h.host_name.casefold()))
            return result

        worker = FunctionWorker(load)
        worker.signals.result.connect(self._hosts_result)
        worker.signals.error.connect(lambda text: self.log.debug("Friend lobby discovery failed: %s", text))
        worker.signals.finished.connect(self._hosts_finished)
        self.pool.start(worker)

    @Slot(object)
    def _hosts_result(self, hosts) -> None:
        rows = list(hosts or [])
        if rows == self._hosts:
            return
        self._hosts = rows
        self.hostsUpdated.emit(self._hosts)

    @Slot()
    def _hosts_finished(self) -> None:
        self._discovery_running = False

    def discover_public_lobbies(self) -> None:
        if not self.steam.initialized or self._public_discovery_running:
            return
        self._public_discovery_running = True

        def load() -> list[SharingHost]:
            event = threading.Event()
            result_box: dict[str, object] = {}
            handle = self.steam.request_lobby_list(
                marker_key=LOBBY_DATA_MARKER_KEY,
                marker_value=LOBBY_DATA_MARKER_VALUE,
                version=str(PROTOCOL_VERSION),
                version_key=LOBBY_DATA_VERSION_KEY,
                visibility=VISIBILITY_PUBLIC,
                visibility_key=LOBBY_DATA_VISIBILITY_KEY,
                max_results=PUBLIC_LOBBY_RESULT_LIMIT,
                worldwide=True,
            )

            def done(result) -> None:
                result_box["result"] = result
                event.set()

            self.steam.await_call(handle, CALLBACK_LOBBY_MATCH_LIST, LobbyMatchList, done)
            if not event.wait(6.0):
                raise TimeoutError("Steam lobby search timed out.")
            call_result = result_box.get("result")
            if call_result is None:
                return []
            count = min(PUBLIC_LOBBY_RESULT_LIMIT, int(call_result.m_nLobbiesMatching))
            rows: list[SharingHost] = []
            seen: set[int] = set()
            for index in range(max(0, count)):
                lobby_id = self.steam.lobby_by_index(index)
                if not lobby_id or lobby_id in seen:
                    continue
                seen.add(lobby_id)
                host = self._load_lobby(lobby_id, timeout=0.35)
                if host is not None and host.visibility == VISIBILITY_PUBLIC:
                    rows.append(host)
            rows.sort(key=lambda h: (h.open_slots <= 0, h.lobby_name.casefold(), h.host_name.casefold()))
            return rows

        worker = FunctionWorker(load)
        worker.signals.result.connect(self._public_lobbies_result)
        worker.signals.error.connect(lambda text: self.log.debug("Public lobby discovery failed: %s", text))
        worker.signals.finished.connect(self._public_lobbies_finished)
        self.pool.start(worker)

    @Slot(object)
    def _public_lobbies_result(self, lobbies) -> None:
        rows = list(lobbies or [])
        if rows == self._public_lobbies:
            return
        self._public_lobbies = rows
        self.lobbiesUpdated.emit(rows)

    @Slot()
    def _public_lobbies_finished(self) -> None:
        self._public_discovery_running = False

    def _steam_event(self, event: str, data: dict) -> None:
        if event == "friends_changed":
            self._friendsDirty.emit()
        elif event == "lobby_enter":
            self.lobbyEntered.emit(int(data["lobby_id"]), int(data["response"]))
        elif event == "lobby_invite":
            self.lobbyInviteReceived.emit(int(data["lobby_id"]), str(data.get("friend_name") or "Steam friend"))
        elif event == "join_requested":
            self.joinRequested.emit(int(data["lobby_id"]), str(data.get("friend_name") or "Steam friend"))
        elif event == "networking_session_request":
            self.networkingSessionRequested.emit(int(data["steam_id"]))
        elif event == "networking_session_failed":
            self.networkingSessionFailed.emit(
                int(data["steam_id"]),
                int(data["error"]),
                str(data.get("detail") or ""),
            )

    def shutdown(self) -> None:
        self._shutting_down = True
        self._friend_timer.stop()
        self._discover_timer.stop()
        self._dirty_timer.stop()
        if not self.pool.waitForDone(25_000):
            self.log.warning("Background workers did not finish before shutdown; skipping explicit SteamAPI_Shutdown.")
            return
        self.steam.shutdown()


class SessionManager(QObject):
    changed = Signal(object)
    approvalRequested = Signal(object, str)
    error = Signal(str)
    notice = Signal(str)
    chatChanged = Signal(object)
    chatStateChanged = Signal(bool)
    passwordRequested = Signal(str)

    _lobbyCreated = Signal(object)
    _joinCallResult = Signal(object)
    _authRequest = Signal(object)
    _joinConfigReady = Signal(object)
    _joinConfigFailed = Signal(str)
    _authGranted = Signal()
    _authDenied = Signal(str)
    _authRevoked = Signal(str)
    _authDisconnected = Signal(str)
    _configUpdated = Signal(str)
    _peerActivity = Signal(object)
    _peerHealth = Signal(object)
    _chatMessage = Signal(object)
    _chatReady = Signal(object)
    _codeInviteGranted = Signal(object)
    _codeInviteDenied = Signal(str)
    _disconnectAck = Signal(object)

    def __init__(
        self,
        steam_service: SteamService,
        prefs: PreferenceStore,
        access: AccessStore,
        logger,
        parent=None,
    ):
        super().__init__(parent)
        self.service = steam_service
        self.steam = steam_service.steam
        self.prefs = prefs
        self.access = access
        self.log = logger
        self._snapshot = AppSnapshot()
        self._pending_share: tuple[object, DetectedService, SessionConfig, dict[str, object], bytes, bytes | None, bytes | None, str] | None = None
        self._config: SessionConfig | None = None
        self._shared_service: DetectedService | None = None
        self._endpoint_map: dict[str, object] = {}
        self._host_control: ControlLink | None = None
        self._client_control: ControlLink | None = None
        self._chat: EncryptedLobbyChat | None = None
        self._invite_broker: InviteBroker | None = None
        self._chat_messages: list[ChatMessage] = []
        self._invite_secret: bytes | None = None
        self._static_invite_secret: bytes | None = None
        self._password_key: bytes | None = None
        self._host_password = ""
        self._join_secret: bytes | None = None
        self._join_password = ""
        self._pending_password_config: SessionConfig | None = None
        self._code_invite_pending = False
        self._join_call_started = False
        self._peer_engines: dict[int, list[TunnelEngine]] = defaultdict(list)
        self._client_engines: dict[str, TunnelEngine] = {}
        self._pending_approvals: set[int] = set()
        self._joining_host: SharingHost | None = None
        self._join_loading = False
        self._kicked_members: set[int] = set()
        self._disconnect_acked: set[int] = set()
        self._membership_guard = LobbyMembershipGuard(10.0)
        self._peer_health: dict[int, tuple[int, str, float]] = {}
        self._join_attempt_id = 0

        self._lobbyCreated.connect(self._finish_share)
        self._joinCallResult.connect(self._on_join_call)
        self._authRequest.connect(self._on_auth_request)
        self._joinConfigReady.connect(self._finish_join_config)
        self._joinConfigFailed.connect(self._join_config_failed)
        self._authGranted.connect(self._on_auth_granted)
        self._authDenied.connect(self._on_auth_denied)
        self._authRevoked.connect(self._on_auth_revoked)
        self._authDisconnected.connect(self._on_auth_disconnected)
        self._configUpdated.connect(self._on_config_updated)
        self._peerActivity.connect(self._on_peer_activity)
        self._peerHealth.connect(self._on_peer_health)
        self._chatMessage.connect(self._on_chat_message)
        self._chatReady.connect(self._on_chat_ready)
        self._codeInviteGranted.connect(self._on_code_invite_granted)
        self._codeInviteDenied.connect(self._on_code_invite_denied)
        self._disconnectAck.connect(self._on_disconnect_ack)
        self.service.lobbyEntered.connect(self._on_lobby_entered)
        self.service.lobbyInviteReceived.connect(self._on_lobby_invite_for_code)
        self.service.networkingSessionRequested.connect(self._on_networking_request)
        self.service.networkingSessionFailed.connect(self._on_networking_fail)
        self.service.ready.connect(self._steam_ready_for_presence)

        self._member_timer = QTimer(self)
        self._member_timer.setInterval(MEMBER_SCAN_INTERVAL_MS)
        self._member_timer.timeout.connect(self._check_members)

        self._steam_status_timer = QTimer(self)
        self._steam_status_timer.setInterval(3000)
        self._steam_status_timer.timeout.connect(self.refresh_steam_status)

    @Slot(str, object)
    def _steam_ready_for_presence(self, _name: str, _steam_id) -> None:
        """Retry a session status that was created before Steam signed in."""
        self.refresh_steam_status()

    @property
    def snapshot(self) -> AppSnapshot:
        return self._snapshot

    @property
    def shared_service(self) -> DetectedService | None:
        return self._shared_service

    @property
    def session_config(self) -> SessionConfig | None:
        return self._config

    @property
    def chat_messages(self) -> tuple[ChatMessage, ...]:
        return tuple(self._chat_messages)

    @property
    def chat_ready(self) -> bool:
        return bool(self._chat is not None and self._chat.ready)

    @property
    def static_share_code_enabled(self) -> bool:
        return self._static_invite_secret is not None

    @property
    def remote_services(self) -> tuple[SharedServiceSpec, ...]:
        if self._config and self._snapshot.mode in {"joining", "connected"}:
            return self._config.services
        return ()

    def _set(self, **changes) -> None:
        snapshot = replace(self._snapshot, **changes)
        if snapshot == self._snapshot:
            return
        self._snapshot = snapshot
        self.changed.emit(snapshot)

    @staticmethod
    def _steam_lobby_type(visibility: str) -> int:
        if visibility == VISIBILITY_PUBLIC:
            return LOBBY_PUBLIC
        if visibility == VISIBILITY_INVITE:



            return LOBBY_PRIVATE
        return LOBBY_FRIENDS_ONLY

    def share(
        self,
        service: DetectedService,
        *,
        lobby_name: str | None = None,
        visibility: str = VISIBILITY_FRIENDS,
        max_members: int = 8,
        password: str = "",
        static_code: bool = False,
    ) -> None:
        if not self.steam.initialized:
            self.error.emit("Steam needs to be running.")
            return
        if self._snapshot.mode != "idle":
            self.error.emit("Stop the current SteamyLAN session first.")
            return
        if not service.endpoints:
            self.error.emit("SteamyLAN couldn't find a usable server connection.")
            return
        visibility = str(visibility or VISIBILITY_FRIENDS).casefold()
        if visibility not in {VISIBILITY_PUBLIC, VISIBILITY_FRIENDS, VISIBILITY_INVITE}:
            self.error.emit("Choose a valid lobby visibility.")
            return
        max_members = max(2, min(250, int(max_members)))
        lobby_name = " ".join(str(lobby_name or service.name).replace("\x00", " ").split())[:80]
        if not lobby_name:
            lobby_name = "SteamyLAN Server"
        host_name = " ".join(str(self.steam.persona_name() or "Steam user").replace("\x00", " ").split())[:120] or "Steam user"
        service_name = " ".join(str(service.name or "Server").replace("\x00", " ").split())[:120] or "Server"
        password = str(password or "")
        if password and len(password) < 4:
            self.error.emit("Lobby passwords must be at least 4 characters.")
            return
        if len(password) > 128:
            self.error.emit("Lobby passwords are limited to 128 characters.")
            return
        password_salt = new_password_salt() if password else ""
        password_key = derive_password_key(password, password_salt) if password else None

        seeds: list[int] = []
        used: set[int] = set()
        for _ in service.endpoints:
            while True:
                seed = CHANNEL_MIN + secrets.randbelow(CHANNEL_MAX - CHANNEL_MIN + 1)
                if seed not in used:
                    used.add(seed)
                    seeds.append(seed)
                    break
        control_channel = CONTROL_CHANNEL_MIN + secrets.randbelow(CONTROL_CHANNEL_MAX - CONTROL_CHANNEL_MIN + 1)
        chat_channel = CHAT_CHANNEL_MIN + secrets.randbelow(CHAT_CHANNEL_MAX - CHAT_CHANNEL_MIN + 1)
        specs: list[SharedServiceSpec] = []
        endpoint_map: dict[str, object] = {}
        for index, (endpoint, seed) in enumerate(zip(service.endpoints, seeds), start=1):
            service_id = f"{uuid.uuid4().hex[:10]}-{index}"
            specs.append(SharedServiceSpec(service_id, service_name, endpoint.protocol, endpoint.port, seed))
            endpoint_map[service_id] = endpoint
        config = SessionConfig(
            session_id=uuid.uuid4().hex,
            host_id=self.steam.steam_id(),
            host_name=host_name,
            control_channel=control_channel,
            chat_channel=chat_channel,
            services=tuple(specs),
            lobby_name=lobby_name,
            visibility=visibility,
            max_members=max_members,
            password_salt=password_salt,
        )
        invite_secret = new_invite_secret()
        static_secret = self.prefs.static_share_secret(self.prefs.server_key(service), create=True) if static_code else None
        token = object()
        self._pending_share = (token, service, config, endpoint_map, invite_secret, static_secret, password_key, password)
        self._set(
            mode="starting",
            status="Starting lobby…",
            service_name=service.name,
            lobby_name=lobby_name,
            visibility=visibility,
            max_members=max_members,
            member_count=1,
        )
        try:
            self.steam.prime_networking()
            handle = self.steam.create_lobby(max_members, self._steam_lobby_type(visibility))
            self.steam.await_call(
                handle,
                CALLBACK_LOBBY_CREATED,
                LobbyCreated,
                lambda result: self._lobbyCreated.emit((token, result)),
            )
        except Exception as exc:
            self._pending_share = None
            self._snapshot = AppSnapshot()
            self.changed.emit(self._snapshot)
            self.error.emit(str(exc))

    @Slot(object)
    def _finish_share(self, payload) -> None:
        token, result = payload
        pending = self._pending_share
        if pending is None or pending[0] is not token:
            if result is not None and int(result.m_eResult) == ERESULT_OK:
                try:
                    self.steam.leave_lobby(int(result.m_ulSteamIDLobby))
                except Exception:
                    pass
            return
        _token, service, config, endpoint_map, invite_secret, static_secret, password_key, host_password = pending
        self._pending_share = None
        if result is None or int(result.m_eResult) != ERESULT_OK:
            self._snapshot = AppSnapshot()
            self.changed.emit(self._snapshot)
            self.error.emit("Steam couldn't start the lobby.")
            return
        lobby_id = int(result.m_ulSteamIDLobby)
        try:
            metadata = {
                LOBBY_DATA_MARKER_KEY: LOBBY_DATA_MARKER_VALUE,
                LOBBY_DATA_VERSION_KEY: str(PROTOCOL_VERSION),
                LOBBY_DATA_CONFIG_KEY: config.to_json(),
                LOBBY_DATA_NAME_KEY: config.lobby_name,
                LOBBY_DATA_VISIBILITY_KEY: config.visibility,
                LOBBY_DATA_MAX_KEY: str(config.max_members),
                LOBBY_DATA_MEMBER_COUNT_KEY: "1",
                LOBBY_DATA_INVITE_HASH_KEY: invite_secret_hash(invite_secret),
                LOBBY_DATA_STATIC_HASH_KEY: invite_secret_hash(static_secret) if static_secret else "",
            }
            for key, value in metadata.items():
                if not self.steam.set_lobby_data(lobby_id, key, value):
                    raise RuntimeError(f"Steam refused lobby metadata: {key}.")
            if not self.steam.set_lobby_member_limit(lobby_id, config.max_members):
                raise RuntimeError("Steam refused the lobby member limit.")
            if not self.steam.set_lobby_type(lobby_id, self._steam_lobby_type(config.visibility)):
                raise RuntimeError("Steam refused the lobby visibility.")
        except Exception as exc:
            self.steam.leave_lobby(lobby_id)
            self._snapshot = AppSnapshot()
            self.changed.emit(self._snapshot)
            self.error.emit(str(exc))
            return

        self._kicked_members.clear()
        self._membership_guard.clear()
        self._config = config
        self._shared_service = service
        self._endpoint_map = endpoint_map
        self._invite_secret = invite_secret
        self._static_invite_secret = static_secret
        self._password_key = password_key
        self._host_password = host_password
        self._chat_messages.clear()
        self.chatChanged.emit(tuple())
        self._host_control = ControlLink(
            self.steam,
            self.log,
            role="host",
            channel=config.control_channel,
            on_request=lambda sid, text: self._authRequest.emit((int(sid), str(text))),
            on_disconnect_ack=lambda sid: self._disconnectAck.emit(int(sid)),
            on_health=lambda sid, ping, state: self._peerHealth.emit(
                (int(sid), int(ping), str(state))
            ),
        )
        self._host_control.start()
        self._chat = EncryptedLobbyChat(
            self.steam,
            self.log,
            role="host",
            channel=config.chat_channel,
            session_id=config.session_id,
            local_id=config.host_id,
            local_name=config.host_name,
            on_message=lambda sid, name, text, created: self._chatMessage.emit(
                ChatMessage(int(sid), str(name), str(text), float(created))
            ),
            on_ready=lambda sid: self._chatReady.emit(int(sid)),
        )
        self._chat.start()
        self.chatStateChanged.emit(True)
        self._invite_broker = InviteBroker(
            self.steam,
            self.log,
            role="host",
            lobby_id=lobby_id,
            local_id=config.host_id,
            secret=invite_secret,
            static_secret=static_secret,
        )
        self._invite_broker.start()
        self._member_timer.start()
        join_code = (
            make_invite_code(0, config.host_id, static_secret)
            if static_secret
            else make_invite_code(lobby_id, config.host_id, invite_secret)
        )
        own_member = self._peer_state(config.host_id, config.host_name, "Host")
        visibility_text = {
            VISIBILITY_PUBLIC: "Public",
            VISIBILITY_FRIENDS: "Friends only",
            VISIBILITY_INVITE: "Invite only",
        }[config.visibility]
        self._set(
            mode="sharing",
            status=f"{visibility_text} lobby is live.",
            lobby_id=lobby_id,
            lobby_name=config.lobby_name,
            visibility=config.visibility,
            max_members=config.max_members,
            member_count=1,
            host_name=config.host_name,
            service_name=self._display_service_name(config.services),
            peers=(),
            members=(own_member,),
            join_code=join_code,
        )
        self.notice.emit(f"{config.lobby_name} is live.")
        self.refresh_steam_status()
        QTimer.singleShot(500, self.service.refresh_lobbies)

    def join(self, host: SharingHost, password: str = "") -> None:
        self._start_join(host, invite_secret=None, password=password)

    def join_code(self, code: str, password: str = "") -> None:
        try:
            lobby_id, host_id, invite_secret = parse_invite_code(code)
        except ValueError as exc:
            self.error.emit(str(exc))
            return
        if not self.steam.initialized:
            self.error.emit("Steam needs to be running.")
            return
        if self._snapshot.mode != "idle":
            self.error.emit("Disconnect first.")
            return
        if host_id == self.steam.steam_id():
            self.error.emit("You are already the host of that lobby.")
            return
        self._join_password = str(password or "")
        placeholder = SharingHost(
            lobby_id=lobby_id,
            host_id=host_id,
            host_name="Share-code host",
            services=(),
            session_id="",
            lobby_name="SteamyLAN share",
            visibility=VISIBILITY_INVITE,
            max_members=8,
            member_count=1,
        )
        self._joining_host = placeholder
        self._join_secret = invite_secret
        self._join_loading = False
        self._join_call_started = False
        self._code_invite_pending = True
        self.steam.prime_networking()
        self._set(
            mode="joining",
            status="Verifying share code…",
            lobby_id=lobby_id,
            lobby_name=placeholder.lobby_name,
            visibility=VISIBILITY_INVITE,
            max_members=placeholder.max_members,
            member_count=1,
            host_name=placeholder.host_name,
        )
        self._invite_broker = InviteBroker(
            self.steam,
            self.log,
            role="client",
            lobby_id=lobby_id,
            local_id=self.steam.steam_id(),
            host_id=host_id,
            secret=invite_secret,
            on_granted=lambda actual_lobby: self._codeInviteGranted.emit(int(actual_lobby)),
            on_denied=lambda text: self._codeInviteDenied.emit(str(text)),
        )
        self._invite_broker.start()

    def join_lobby_id(self, lobby_id: int, host_name: str = "Steam friend") -> None:
        placeholder = SharingHost(
            lobby_id=int(lobby_id),
            host_id=0,
            host_name=host_name,
            services=(),
            session_id="",
        )
        self._start_join(placeholder, invite_secret=None, password="")

    @Slot(object)
    def _on_code_invite_granted(self, actual_lobby_id) -> None:
        if not self._code_invite_pending or self._joining_host is None:
            return
        actual_lobby_id = int(actual_lobby_id or 0)
        if actual_lobby_id <= 0:
            self._on_code_invite_denied("The host did not return a valid Steam lobby.")
            return
        expected = int(self._joining_host.lobby_id)
        if expected > 0 and expected != actual_lobby_id:
            self._on_code_invite_denied("That share code no longer points to the expected Steam lobby.")
            return
        if expected == 0:
            self._joining_host = replace(self._joining_host, lobby_id=actual_lobby_id)
            self._set(lobby_id=actual_lobby_id)
        self._set(status="Share code accepted. Joining lobby…")
        QTimer.singleShot(300, self._request_join_current)

    @Slot(str)
    def _on_code_invite_denied(self, text: str) -> None:
        if not self._code_invite_pending:
            return
        self.error.emit(text or "That SteamyLAN share code was rejected.")
        self.stop()

    @Slot(object, str)
    def _on_lobby_invite_for_code(self, lobby_id, _friend_name: str) -> None:
        if self._code_invite_pending and self._joining_host is not None:
            expected = int(self._joining_host.lobby_id)
            if expected in {0, int(lobby_id)}:
                if expected == 0:
                    self._joining_host = replace(self._joining_host, lobby_id=int(lobby_id))
                    self._set(lobby_id=int(lobby_id))
                self._request_join_current()

    def _request_join_current(self) -> None:
        host = self._joining_host
        if host is None or self._join_call_started:
            return
        if int(host.lobby_id) <= 0:
            return
        self._join_call_started = True
        if self._invite_broker and self._invite_broker.role == "client":
            self._invite_broker.stop()
            self._invite_broker = None
        try:
            self.steam.prime_networking()
            handle = self.steam.join_lobby(host.lobby_id)
            self.steam.await_call(
                handle,
                CALLBACK_LOBBY_ENTER,
                LobbyEnter,
                lambda result: self._joinCallResult.emit(result),
            )
            self._join_attempt_id += 1
            attempt_id = self._join_attempt_id
            lobby_id = int(host.lobby_id)
            QTimer.singleShot(20_000, lambda attempt_id=attempt_id, lobby_id=lobby_id: self._join_watchdog(
                attempt_id, lobby_id
            ))
        except Exception as exc:
            self._join_call_started = False
            self._joining_host = None
            self._join_secret = None
            self._join_password = ""
            self._pending_password_config = None
            self._code_invite_pending = False
            self._snapshot = AppSnapshot()
            self.changed.emit(self._snapshot)
            self.error.emit(str(exc))

    def _start_join(self, host: SharingHost, invite_secret: bytes | None, password: str = "") -> None:
        if not self.steam.initialized:
            self.error.emit("Steam needs to be running.")
            return
        if self._snapshot.mode != "idle":
            self.error.emit("Disconnect first.")
            return
        if int(host.lobby_id) <= 0:
            self.error.emit("That Steam lobby ID is invalid.")
            return
        self._joining_host = host
        self._join_secret = invite_secret
        self._join_password = str(password or "")
        self._pending_password_config = None
        self._join_loading = False
        self._join_call_started = False
        self._code_invite_pending = False
        service_name = self._display_service_name(host.services) if host.services else ""
        self._set(
            mode="joining",
            status=f"Connecting to {host.lobby_name or host.host_name}…",
            lobby_id=host.lobby_id,
            lobby_name=host.lobby_name,
            visibility=host.visibility,
            max_members=host.max_members,
            member_count=host.member_count,
            host_name=host.host_name,
            service_name=service_name,
        )
        self._request_join_current()

    @Slot(object)
    def _on_join_call(self, result) -> None:
        if self._joining_host is None:
            if result is not None:
                try:
                    self.steam.leave_lobby(int(result.m_ulSteamIDLobby))
                except Exception:
                    pass
            return
        if result is None:
            # Manual dispatch can occasionally fail to claim the API-call result
            # even though the ordinary LobbyEnter callback is still in flight.
            # Treat this as inconclusive; LobbyEnter is the authoritative event.
            if not self._join_loading:
                self._set(status="Waiting for Steam lobby confirmation…")
            return
        self._begin_join(int(result.m_ulSteamIDLobby), int(result.m_EChatRoomEnterResponse))

    def _join_watchdog(self, attempt_id: int, lobby_id: int) -> None:
        host = self._joining_host
        if (
            int(attempt_id) != self._join_attempt_id
            or host is None
            or int(host.lobby_id) != int(lobby_id)
            or self._join_loading
        ):
            return
        self._joining_host = None
        self._join_secret = None
        self._join_password = ""
        self._pending_password_config = None
        self._join_call_started = False
        self._code_invite_pending = False
        self._snapshot = AppSnapshot()
        self.changed.emit(self._snapshot)
        self.error.emit("Steam did not confirm the lobby join before the connection timeout.")

    @Slot(object, int)
    def _on_lobby_entered(self, lobby_id, response: int) -> None:
        lobby_id = int(lobby_id)
        response = int(response)
        if self._joining_host is None and self._snapshot.mode == "idle" and response == CHAT_ROOM_ENTER_SUCCESS:
            try:
                self.steam.leave_lobby(lobby_id)
            except Exception:
                pass
            return
        self._begin_join(lobby_id, response)

    def _begin_join(self, lobby_id: int, response: int) -> None:
        host = self._joining_host
        if host is None or host.lobby_id != lobby_id or self._join_loading:
            return
        if response != CHAT_ROOM_ENTER_SUCCESS:
            self._joining_host = None
            self._join_secret = None
            self._join_password = ""
            self._pending_password_config = None
            self._join_loading = False
            self._join_call_started = False
            self._code_invite_pending = False
            self._snapshot = AppSnapshot()
            self.changed.emit(self._snapshot)
            self.error.emit("That SteamyLAN lobby is no longer joinable.")
            return
        self._join_loading = True
        join_secret = self._join_secret

        worker = FunctionWorker(self._load_join_config, lobby_id, host, join_secret)
        worker.signals.result.connect(self._joinConfigReady)
        worker.signals.error.connect(self._joinConfigFailed)
        QThreadPool.globalInstance().start(worker)

    def _load_join_config(
        self,
        lobby_id: int,
        host: SharingHost,
        join_secret: bytes | None,
        *,
        timeout: float = 12.0,
        refresh_interval: float = 1.0,
    ) -> SessionConfig:
        """Wait for validated lobby metadata after Steam confirms entry."""
        lobby_id = int(lobby_id)
        deadline = time.monotonic() + max(0.2, float(timeout))
        next_refresh = 0.0
        last_error = None
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_refresh:
                try:
                    self.steam.request_lobby_data(lobby_id)
                except Exception as exc:
                    last_error = exc
                next_refresh = now + max(0.01, float(refresh_interval))
            try:
                marker = self.steam.get_lobby_data(lobby_id, LOBBY_DATA_MARKER_KEY)
                text = self.steam.get_lobby_data(lobby_id, LOBBY_DATA_CONFIG_KEY)
                if marker == LOBBY_DATA_MARKER_VALUE and text:
                    config = SessionConfig.from_json(text)
                    owner = self.steam.lobby_owner(lobby_id)
                    if owner != config.host_id or (host.host_id and owner != host.host_id):
                        raise ValueError("The Steam lobby owner did not match the advertised host.")
                    actual_limit = int(self.steam.lobby_member_limit(lobby_id) or 0)
                    if actual_limit and actual_limit != config.max_members:
                        raise ValueError("The Steam lobby member limit did not match its advertised configuration.")
                    if config.visibility == VISIBILITY_FRIENDS and not self.steam.is_immediate_friend(config.host_id):
                        raise ValueError("This Friends Only lobby is not hosted by one of your Steam friends.")
                    if join_secret is not None:
                        digest = invite_secret_hash(join_secret)
                        dynamic_hash = self.steam.get_lobby_data(lobby_id, LOBBY_DATA_INVITE_HASH_KEY)
                        static_hash = self.steam.get_lobby_data(lobby_id, LOBBY_DATA_STATIC_HASH_KEY)
                        if digest not in {dynamic_hash, static_hash}:
                            raise ValueError("That share code has expired or no longer matches this lobby.")
                    return config
            except Exception as exc:
                last_error = exc
            time.sleep(0.15)
        if last_error:
            raise last_error
        raise TimeoutError("SteamyLAN lobby information was not available.")

    @Slot(object)
    def _finish_join_config(self, config: SessionConfig) -> None:
        host = self._joining_host
        if host is None:
            return
        if config.password_salt and not self._join_password:
            self._pending_password_config = config
            self._set(status=f"Password required for {config.lobby_name}…")
            self.passwordRequested.emit(config.lobby_name)
            return
        self._pending_password_config = None
        self._config = config
        invite_auth = ""
        if self._join_secret is not None:
            invite_auth = invite_proof(self._join_secret, config.session_id, self.steam.steam_id())
        password_auth = ""
        if config.password_salt:
            try:
                key = derive_password_key(self._join_password, config.password_salt)
                password_auth = password_proof(key, config.session_id, self.steam.steam_id())
            except ValueError as exc:
                self.error.emit(str(exc))
                self.stop()
                return
        auth_payload = make_auth_payload(invite=invite_auth, password=password_auth)
        self._client_control = ControlLink(
            self.steam,
            self.log,
            role="client",
            channel=config.control_channel,
            peer_id=config.host_id,
            on_granted=lambda: self._authGranted.emit(),
            on_denied=lambda text: self._authDenied.emit(text),
            on_revoked=lambda text: self._authRevoked.emit(text),
            on_disconnected=lambda text: self._authDisconnected.emit(text),
            on_config_update=lambda text: self._configUpdated.emit(str(text)),
            on_health=lambda sid, ping, state: self._peerHealth.emit(
                (int(sid), int(ping), str(state))
            ),
            auth_payload=auth_payload,
        )
        self._client_control.start()
        self._member_timer.start()
        try:
            members = self.steam.lobby_members(host.lobby_id)
        except Exception:
            members = [config.host_id, self.steam.steam_id()]
        member_rows = self._member_rows(members, config.host_id)
        self._set(
            mode="joining",
            status=f"Waiting for access to {config.lobby_name}…",
            lobby_id=host.lobby_id,
            lobby_name=config.lobby_name,
            visibility=config.visibility,
            max_members=config.max_members,
            member_count=max(1, len(members)),
            host_name=config.host_name,
            service_name=self._display_service_name(config.services),
            members=member_rows,
        )

    def provide_password(self, password: str) -> None:
        config = self._pending_password_config
        if config is None or self._snapshot.mode != "joining":
            return
        password = str(password or "")
        if not password:
            self.error.emit("A password is required to join this lobby.")
            self.stop()
            return
        self._join_password = password
        self._finish_join_config(config)

    @Slot(str)
    def _join_config_failed(self, text: str) -> None:
        host = self._joining_host
        self.log.warning("Join validation failed: %s", text)
        if host:
            try:
                self.steam.leave_lobby(host.lobby_id)
            except Exception:
                pass
        self._joining_host = None
        self._join_secret = None
        self._join_password = ""
        self._pending_password_config = None
        self._join_loading = False
        self._join_call_started = False
        self._code_invite_pending = False
        self._config = None
        self._snapshot = AppSnapshot()
        self.changed.emit(self._snapshot)
        self.error.emit(str(text or "That SteamyLAN lobby is no longer available."))

    def _lobby_membership_state(self, sid: int) -> bool | None:
        """Return True/False once membership is authoritative, None while Steam syncs."""
        try:
            members = self.steam.lobby_members(self._snapshot.lobby_id)
        except Exception:
            members = None
        return self._membership_guard.check(int(sid), members)

    @Slot(object)
    def _on_networking_request(self, steam_id) -> None:
        sid = int(steam_id)
        if self._snapshot.mode == "sharing" and self._snapshot.lobby_id:
            # SteamNetworkingMessages can signal the P2P request before the local
            # matchmaking member list contains a just-joined user.  Accept the
            # transport provisionally and enforce lobby membership/auth below.
            # Closing here turns a harmless cache race into a broken P2P session.
            if sid in self._kicked_members:
                self.steam.close_peer(sid)
            else:
                self.steam.accept_peer(sid)
        elif (
            self._code_invite_pending
            and self._joining_host is not None
            and sid == self._joining_host.host_id
        ):
            self.steam.accept_peer(sid)
        elif self._snapshot.mode in {"joining", "connected"} and self._config and sid == self._config.host_id:
            self.steam.accept_peer(sid)
        else:
            self.steam.close_peer(sid)

    @Slot(object, int, str)
    def _on_networking_fail(self, steam_id, error_code: int, detail: str = "") -> None:
        sid = int(steam_id)
        detail = " ".join(str(detail or "").split())[:160]
        failure = f"Steam P2P error {int(error_code)}"
        if detail:
            failure += f": {detail}"
        # Clear the failed SteamNetworkingMessages session.  The next control
        # send uses AutoRestartBrokenSession and creates a fresh rendezvous.
        try:
            self.steam.close_peer(sid)
        except Exception:
            self.log.debug("Could not clear failed Steam P2P session for %s", sid, exc_info=True)
        if self._config and sid == self._config.host_id and self._snapshot.mode in {"joining", "connected"}:
            self._set(status=f"{failure}. Reconnecting…")
            if self._client_control:
                self._client_control.reconnect()
        elif self._snapshot.mode == "sharing" and sid in self._peer_engines:
            self.notice.emit(f"{failure} for {self.service.friend_name(sid)}. Reconnecting…")
            self._refresh_peer_snapshot(connecting_sid=sid)

    @Slot(object)
    def _on_auth_request(self, payload) -> None:
        try:
            sid, auth_text = int(payload[0]), str(payload[1] or "")
            invite_auth, password_auth = parse_auth_payload(auth_text)
        except Exception:
            return
        if self._snapshot.mode != "sharing" or not self._config or not self._host_control:
            return
        if sid in self._kicked_members:
            self._membership_guard.forget(sid)
            self._host_control.deny(sid, "You were kicked from this SteamyLAN lobby.")
            QTimer.singleShot(1200, lambda sid=sid: self.steam.close_peer(sid))
            return

        membership = self._lobby_membership_state(sid)
        if membership is None:
            # Do not deny while Steam is still synchronizing lobby membership.
            # The client authorization loop retries every 1.5 seconds.
            return
        if not membership:
            self._host_control.deny(sid, "You are no longer in this Steam lobby.")
            QTimer.singleShot(1200, lambda sid=sid: self.steam.close_peer(sid))
            return

        if self._config.visibility == VISIBILITY_FRIENDS and not self.steam.is_immediate_friend(sid):
            self._host_control.deny(sid, "This lobby is limited to the host's Steam friends.")
            QTimer.singleShot(1200, lambda sid=sid: self.steam.close_peer(sid))
            return
        if invite_auth:
            valid_invite = False
            for secret in (self._invite_secret, self._static_invite_secret):
                if secret is None:
                    continue
                expected = invite_proof(secret, self._config.session_id, sid)
                if secrets.compare_digest(invite_auth, expected):
                    valid_invite = True
                    break
            if not valid_invite:
                self._host_control.deny(sid, "That SteamyLAN share-code proof is no longer valid.")
                QTimer.singleShot(1200, lambda sid=sid: self.steam.close_peer(sid))
                return

        if self._config.password_salt:
            if self._password_key is None:
                self._host_control.deny(sid, "This password-protected lobby is no longer accepting connections.")
                return
            if not password_auth:
                self._host_control.deny(sid, "This lobby requires a password.")
                QTimer.singleShot(1200, lambda sid=sid: self.steam.close_peer(sid))
                return
            expected_password = password_proof(self._password_key, self._config.session_id, sid)
            if not secrets.compare_digest(password_auth, expected_password):
                self._host_control.deny(sid, "Incorrect lobby password.")
                QTimer.singleShot(1200, lambda sid=sid: self.steam.close_peer(sid))
                return

        if sid in self._peer_engines:
            self._host_control.grant(sid)
            if self._chat:
                self._chat.add_peer(sid)
            return



        if self._config.visibility in {VISIBILITY_PUBLIC, VISIBILITY_INVITE}:
            self._grant_peer(sid, membership_confirmed=True)
            return
        if self.prefs.prefs.auto_allow_friends or self.access.is_allowed(sid):
            self._grant_peer(sid, membership_confirmed=True)
            return
        if sid not in self._pending_approvals:
            self._pending_approvals.add(sid)
            name = self.service.friend_name(sid)
            self._refresh_peer_snapshot()
            self.approvalRequested.emit(sid, name)

    def allow_peer(self, steam_id: int, remember: bool = True) -> None:
        sid = int(steam_id)
        # _grant_peer removes the pending row on success. Keeping it until then
        # avoids losing a user's approval if Steam's member cache is briefly stale.
        if self._grant_peer(sid) and remember:
            self.access.allow(sid)

    def deny_peer(self, steam_id: int) -> None:
        sid = int(steam_id)
        self._pending_approvals.discard(sid)
        if self._host_control:
            self._host_control.deny(sid)
            self._host_control.remove_peer(sid)
        self._refresh_peer_snapshot()
        QTimer.singleShot(400, lambda sid=sid: self.steam.close_peer(sid))

    @Slot(object)
    def _on_disconnect_ack(self, steam_id) -> None:
        sid = int(steam_id)
        if self._snapshot.mode != "sharing":
            return
        # Engines are removed before a normal disconnect/kick is announced.
        # Only close sessions that are actually in teardown; ignore stray ACKs
        # from peers that are still active.
        if sid in self._peer_engines and sid not in self._kicked_members:
            return
        self._disconnect_acked.add(sid)
        try:
            self.steam.close_peer(sid)
        except Exception:
            self.log.debug("Could not close acknowledged Steam peer %s", sid, exc_info=True)

    def _repeat_disconnect_notice(self, sid: int, reason: str, *, require_kicked: bool = False) -> None:
        if self._snapshot.mode != "sharing" or not self._host_control:
            return
        if int(sid) in self._disconnect_acked:
            return
        if require_kicked and int(sid) not in self._kicked_members:
            return
        self._host_control.disconnect(int(sid), reason)

    def disconnect_peer(self, steam_id: int) -> None:
        sid = int(steam_id)
        self._disconnect_acked.discard(sid)
        reason = "The host disconnected you."
        if self._host_control:
            self._host_control.disconnect(sid, reason)
            QTimer.singleShot(350, lambda sid=sid, reason=reason: self._repeat_disconnect_notice(sid, reason))
            QTimer.singleShot(1000, lambda sid=sid: self._host_control.remove_peer(sid) if self._host_control else None)
        if self._chat:
            self._chat.remove_peer(sid)
        self._stop_peer_engines(sid)
        # Give the reliable control packet time to leave Steam's send queue
        # before destroying the underlying peer session.
        QTimer.singleShot(1400, lambda sid=sid: self.steam.close_peer(sid))
        self._refresh_peer_snapshot()

    def remove_access(self, steam_id: int) -> None:
        sid = int(steam_id)
        self.access.remove(sid)
        self._pending_approvals.discard(sid)
        if self._host_control:
            self._host_control.revoke(sid)
            self._host_control.remove_peer(sid)
        if self._chat:
            self._chat.remove_peer(sid)
        self._stop_peer_engines(sid)
        QTimer.singleShot(1200, lambda sid=sid: self.steam.close_peer(sid))
        self._refresh_peer_snapshot()

    def kick_peer(self, steam_id: int, reason: str = "You were kicked from this SteamyLAN lobby.") -> None:
        sid = int(steam_id)
        if self._snapshot.mode != "sharing" or not self._config or sid <= 0 or sid == self._config.host_id:
            return
        self._disconnect_acked.discard(sid)
        self._kicked_members.add(sid)
        self._membership_guard.forget(sid)
        self._pending_approvals.discard(sid)
        if self._host_control:
            # Send twice before closing the Steam session.  A kick must reach the
            # client so it leaves the lobby, while the host-side kicked set also
            # prevents reauthorization if the notification is delayed/lost.
            self._host_control.disconnect(sid, reason)
            QTimer.singleShot(350, lambda sid=sid, reason=reason: self._repeat_disconnect_notice(
                sid, reason, require_kicked=True
            ))
            QTimer.singleShot(1000, lambda sid=sid: self._host_control.remove_peer(sid) if self._host_control else None)
        if self._chat:
            self._chat.remove_peer(sid)
        self._stop_peer_engines(sid)
        QTimer.singleShot(1500, lambda sid=sid: self.steam.close_peer(sid))
        self._refresh_peer_snapshot()

    def invite_friend(self, steam_id: int) -> bool:
        if self._snapshot.mode != "sharing" or not self._snapshot.lobby_id:
            self.error.emit("Start a lobby before inviting somebody.")
            return False
        try:
            ok = bool(self.steam.invite_to_lobby(self._snapshot.lobby_id, int(steam_id)))
        except Exception as exc:
            self.error.emit(str(exc))
            return False
        if not ok:
            self.error.emit("Steam did not send that lobby invite.")
        return ok

    def invite_friends(self, steam_ids) -> tuple[tuple[int, ...], tuple[int, ...]]:
        if self._snapshot.mode != "sharing" or not self._snapshot.lobby_id:
            self.error.emit("Start a lobby before inviting friends.")
            return (), tuple(int(sid) for sid in steam_ids)

        unique_ids = tuple(dict.fromkeys(int(sid) for sid in steam_ids if int(sid) > 0))
        sent: list[int] = []
        failed: list[int] = []
        last_error = ""
        for sid in unique_ids:
            try:
                ok = bool(self.steam.invite_to_lobby(self._snapshot.lobby_id, sid))
            except Exception as exc:
                ok = False
                last_error = str(exc)
            if ok:
                sent.append(sid)
            else:
                failed.append(sid)

        if failed:
            detail = f" Last error: {last_error}" if last_error else ""
            noun = "invite" if len(failed) == 1 else "invites"
            self.error.emit(f"Steam did not send {len(failed)} lobby {noun}.{detail}")
        return tuple(sent), tuple(failed)

    def reconfigure_server(
        self,
        service: DetectedService,
        *,
        lobby_name: str,
        visibility: str,
        max_members: int,
        password: str | None,
        static_code: bool,
        endpoint_names: dict[tuple[str, int], str] | None = None,
    ) -> bool:
        if self._snapshot.mode != "sharing" or not self._config or not self._snapshot.lobby_id:
            self.error.emit("Start a server before changing server settings.")
            return False
        if not service.endpoints:
            self.error.emit("Select at least one port to share.")
            return False
        if len(service.endpoints) > 32:
            self.error.emit("A server can share at most 32 TCP/UDP endpoints.")
            return False
        visibility = str(visibility or VISIBILITY_FRIENDS).casefold()
        if visibility not in {VISIBILITY_PUBLIC, VISIBILITY_FRIENDS, VISIBILITY_INVITE}:
            self.error.emit("Choose a valid lobby visibility.")
            return False
        max_members = max(2, min(250, int(max_members)))
        lobby_name = " ".join(str(lobby_name or service.name).replace("\x00", " ").split())[:80] or "SteamyLAN Server"
        service_name = " ".join(str(service.name or "Server").replace("\x00", " ").split())[:120] or "Server"

        if password is None:
            password_salt = self._config.password_salt
            password_key = self._password_key
            host_password = self._host_password
        else:
            password = str(password or "")
            if password and len(password) < 4:
                self.error.emit("Lobby passwords must be at least 4 characters.")
                return False
            if len(password) > 128:
                self.error.emit("Lobby passwords are limited to 128 characters.")
                return False
            password_salt = new_password_salt() if password else ""
            password_key = derive_password_key(password, password_salt) if password else None
            host_password = password

        old_specs = {(spec.protocol.upper(), int(spec.port)): spec for spec in self._config.services}
        used_channels = {self._config.control_channel, self._config.chat_channel}
        specs: list[SharedServiceSpec] = []
        endpoint_map: dict[str, Endpoint] = {}
        for index, endpoint in enumerate(service.endpoints, start=1):
            key = (endpoint.protocol.upper(), int(endpoint.port))
            old = old_specs.get(key)
            if old is not None:
                service_id = old.service_id
                channel = old.channel
            else:
                service_id = f"{uuid.uuid4().hex[:10]}-{index}"
                while True:
                    channel = CHANNEL_MIN + secrets.randbelow(CHANNEL_MAX - CHANNEL_MIN + 1)
                    if channel not in used_channels:
                        break
            used_channels.add(channel)
            endpoint_name = service_name
            if old is not None:
                endpoint_name = old.name
            if endpoint_names is not None:
                endpoint_name = endpoint_names.get(key, endpoint_name)
            endpoint_name = " ".join(str(endpoint_name or service_name).replace("\x00", " ").split())[:120] or service_name
            specs.append(SharedServiceSpec(service_id, endpoint_name, endpoint.protocol.upper(), int(endpoint.port), channel))
            endpoint_map[service_id] = endpoint

        config = replace(
            self._config,
            services=tuple(specs),
            lobby_name=lobby_name,
            visibility=visibility,
            max_members=max_members,
            password_salt=password_salt,
        )
        static_secret = (
            self.prefs.static_share_secret(self.prefs.server_key(service), create=True)
            if static_code
            else None
        )
        invite_secret = self._invite_secret or new_invite_secret()
        lobby_id = int(self._snapshot.lobby_id)
        try:
            metadata = {
                LOBBY_DATA_CONFIG_KEY: config.to_json(),
                LOBBY_DATA_NAME_KEY: config.lobby_name,
                LOBBY_DATA_VISIBILITY_KEY: config.visibility,
                LOBBY_DATA_MAX_KEY: str(config.max_members),
                LOBBY_DATA_INVITE_HASH_KEY: invite_secret_hash(invite_secret),
                LOBBY_DATA_STATIC_HASH_KEY: invite_secret_hash(static_secret) if static_secret else "",
            }
            for key, value in metadata.items():
                if not self.steam.set_lobby_data(lobby_id, key, value):
                    raise RuntimeError(f"Steam refused lobby metadata: {key}.")
            if not self.steam.set_lobby_member_limit(lobby_id, config.max_members):
                raise RuntimeError("Steam refused the lobby member limit.")
            if not self.steam.set_lobby_type(lobby_id, self._steam_lobby_type(config.visibility)):
                raise RuntimeError("Steam refused the lobby visibility.")
        except Exception as exc:
            self.error.emit(str(exc))
            return False

        active_peers = tuple(self._peer_engines)
        previous_config = self._config
        previous_endpoints = self._endpoint_map
        previous_transport = tuple(
            (item.service_id, item.protocol.upper(), int(item.port), int(item.channel))
            for item in previous_config.services
        )
        updated_transport = tuple(
            (item.service_id, item.protocol.upper(), int(item.port), int(item.channel))
            for item in config.services
        )
        transport_changed = (
            previous_transport != updated_transport
            or any(
                previous_endpoints.get(old.service_id) != endpoint_map.get(old.service_id)
                for old in previous_config.services
            )
        )
        if transport_changed:
            for sid in active_peers:
                self._stop_peer_engines(sid)
        self._pending_approvals.clear()
        self._config = config
        self._shared_service = service
        self._endpoint_map = endpoint_map
        self._invite_secret = invite_secret
        self._static_invite_secret = static_secret
        self._password_key = password_key
        self._host_password = host_password

        # Keep the existing Steam sessions and authorization state alive. The
        # transport channels are rebuilt from the new mapping, then clients get
        # the same validated configuration over the control channel.
        config_json = config.to_json()
        for sid in active_peers:
            if transport_changed and not self._start_peer_engines(sid):
                self.log.error("Could not apply updated forwarding for peer %s", sid)
            if self._host_control:
                self._host_control.send_config(sid, config_json)
            if self._chat:
                self._chat.add_peer(sid)

        if self._invite_broker:
            self._invite_broker.stop()
        self._invite_broker = InviteBroker(
            self.steam,
            self.log,
            role="host",
            lobby_id=lobby_id,
            local_id=config.host_id,
            secret=invite_secret,
            static_secret=static_secret,
        )
        self._invite_broker.start()
        join_code = (
            make_invite_code(0, config.host_id, static_secret)
            if static_secret
            else make_invite_code(lobby_id, config.host_id, invite_secret)
        )
        try:
            members = self.steam.lobby_members(lobby_id)
        except Exception:
            members = [config.host_id]
        self._set(
            status="Server settings updated.",
            lobby_name=config.lobby_name,
            visibility=config.visibility,
            max_members=config.max_members,
            member_count=max(1, len(members)),
            service_name=service.name,
            peers=tuple(self._peer_state(sid, self.service.friend_name(sid), "Connecting…") for sid in active_peers if sid in self._peer_engines),
            members=self._member_rows(members, config.host_id),
            join_code=join_code,
        )
        self.notice.emit("Server settings updated. Existing members stayed connected.")
        self.refresh_steam_status()
        QTimer.singleShot(500, self.service.refresh_lobbies)
        return True

    def _grant_peer(self, sid: int, *, membership_confirmed: bool = False) -> bool:
        if not self._config or not self._host_control or self._snapshot.mode != "sharing":
            return False
        membership = True if membership_confirmed else self._lobby_membership_state(sid)
        if membership is None:
            # Steam can briefly return an incomplete member list while a manual
            # approval is being processed. Keep authorization pending; the
            # client's control loop will retry without showing another prompt.
            return False
        if not membership:
            self._pending_approvals.discard(sid)
            self._host_control.deny(sid, "You are no longer in this Steam lobby.")
            self._refresh_peer_snapshot()
            QTimer.singleShot(1200, lambda sid=sid: self.steam.close_peer(sid))
            return False
        if self._config.visibility == VISIBILITY_FRIENDS and not self.steam.is_immediate_friend(sid):
            self._pending_approvals.discard(sid)
            self._host_control.deny(sid, "This lobby is limited to Steam friends.")
            self._refresh_peer_snapshot()
            return False
        if sid in self._peer_engines:
            self._host_control.grant(sid)
            if self._chat:
                self._chat.add_peer(sid)
            return True

        if not self._start_peer_engines(sid):
            self.log.error("Could not start host forwarding for %s", sid)
            self._host_control.deny(sid, "SteamyLAN couldn't open the shared server.")
            self.error.emit(f"Couldn't connect {self.service.friend_name(sid)} to the shared server.")
            return False
        self._pending_approvals.discard(sid)
        if self._chat:
            self._chat.add_peer(sid)
        self._host_control.grant(sid)
        self._refresh_peer_snapshot(connecting_sid=sid)
        return True

    def _start_peer_engines(self, sid: int) -> bool:
        """Start the current configuration for an already-authorized peer."""
        if not self._config:
            return False
        engines: list[TunnelEngine] = []
        try:
            for spec in self._config.services:
                endpoint = self._endpoint_map[spec.service_id]
                engine = TunnelEngine(
                    self.steam,
                    self.log,
                    role="host",
                    protocol=spec.protocol,
                    peer_id=int(sid),
                    channel=derive_peer_channel(spec.channel, int(sid)),
                    target_host=target_host_for(endpoint.local_ip),
                    target_port=spec.port,
                    on_activity=lambda peer, self=self: self._peerActivity.emit(int(peer)),
                )
                engine.start()
                engines.append(engine)
        except Exception:
            for engine in engines:
                engine.stop()
            return False
        self._peer_engines[int(sid)] = engines
        return True

    @Slot()
    def _on_auth_granted(self) -> None:
        if not self._config:
            return
        self._join_password = ""
        if self._snapshot.mode == "connected":
            status = (
                f"{self._snapshot.service_name} is ready."
                if self._snapshot.mappings
                else "Connected. No shared ports are open on this computer."
            )
            self._set(status=status)
            if self._chat is None:
                self._start_client_chat()
            return
        if self._snapshot.mode != "joining":
            return

        mappings: list[LocalMapping] = []
        started: dict[str, TunnelEngine] = {}
        if self.prefs.prefs.auto_accept_ports:
            try:
                for spec in self._config.services:
                    engine, mapping = self._start_client_service(spec)
                    started[spec.service_id] = engine
                    mappings.append(mapping)
            except Exception as exc:
                for engine in started.values():
                    try:
                        engine.stop()
                    except Exception:
                        pass
                self.log.exception("Client forwarding failed")
                self.error.emit(f"SteamyLAN couldn't prepare the local connection: {exc}")
                self.stop()
                return

        self._client_engines = started
        self._join_loading = False
        self._joining_host = None
        self._join_secret = None
        self._join_call_started = False
        self._code_invite_pending = False
        status = (
            f"{self._snapshot.service_name} is ready."
            if mappings
            else "Connected. Choose Open locally beside any shared port when you need it."
        )
        self._set(mode="connected", status=status, mappings=tuple(mappings))
        self._start_client_chat()
        self.refresh_steam_status()

    def _start_client_chat(self) -> None:
        if self._chat is not None or not self._config or self._snapshot.mode != "connected":
            return
        self._chat_messages.clear()
        self.chatChanged.emit(tuple())
        self._chat = EncryptedLobbyChat(
            self.steam,
            self.log,
            role="client",
            channel=self._config.chat_channel,
            session_id=self._config.session_id,
            local_id=self.steam.steam_id(),
            local_name=self.steam.persona_name(),
            host_id=self._config.host_id,
            on_message=lambda sid, name, text, created: self._chatMessage.emit(
                ChatMessage(int(sid), str(name), str(text), float(created))
            ),
            on_ready=lambda sid: self._chatReady.emit(int(sid)),
        )
        self._chat.start()
        self.chatStateChanged.emit(self._chat.ready)

    def send_chat(self, text: str) -> bool:
        if self._snapshot.mode not in {"sharing", "connected"} or self._chat is None:
            self.error.emit("Join or host a lobby before using chat.")
            return False
        try:
            ok = self._chat.send_message(text)
        except ChatError as exc:
            self.error.emit(str(exc))
            return False
        except Exception as exc:
            self.log.exception("Chat send failed")
            self.error.emit(f"Couldn't send the chat message: {exc}")
            return False
        if not ok and self._snapshot.mode == "connected":
            self.notice.emit("Secure chat is still connecting. Try again in a moment.")
            return False
        # A host message is valid locally even when nobody else has joined yet.
        return True

    @Slot(object)
    def _on_chat_ready(self, _steam_id) -> None:
        self.chatStateChanged.emit(self.chat_ready)

    @Slot(object)
    def _on_chat_message(self, message) -> None:
        if not isinstance(message, ChatMessage):
            return
        self._chat_messages.append(message)
        if len(self._chat_messages) > 200:
            del self._chat_messages[:-200]
        self.chatChanged.emit(tuple(self._chat_messages))

    def _find_remote_service(self, service_id: str) -> SharedServiceSpec | None:
        if not self._config:
            return None
        for spec in self._config.services:
            if spec.service_id == service_id:
                return spec
        return None

    def _start_client_service(
        self,
        spec: SharedServiceSpec,
        *,
        local_port: int | None = None,
        bind_host: str | None = None,
    ) -> tuple[TunnelEngine, LocalMapping]:
        if not self._config:
            raise RuntimeError("There is no active lobby connection.")
        bind_host = str(bind_host or self.prefs.prefs.bind_address)
        requested_port = int(local_port or 0)
        if requested_port <= 0:
            preferred = spec.port
            requested_port = (
                preferred
                if bind_available(spec.protocol, bind_host, preferred)
                else find_free_port(spec.protocol, bind_host)
            )
        elif not 1 <= requested_port <= 65535:
            raise ValueError("Local port must be between 1 and 65535, or blank for an automatic port.")
        engine = TunnelEngine(
            self.steam,
            self.log,
            role="client",
            protocol=spec.protocol,
            peer_id=self._config.host_id,
            channel=derive_peer_channel(spec.channel, self.steam.steam_id()),
            target_host="127.0.0.1",
            target_port=spec.port,
            bind_host=bind_host,
            bind_port=requested_port,
            on_activity=lambda peer, self=self: self._peerActivity.emit(int(peer)),
        )
        engine.start()
        return engine, LocalMapping(
            spec.service_id, spec.name, spec.protocol, spec.port, requested_port, bind_host
        )

    def accept_client_service(self, service_id: str) -> None:
        if self._snapshot.mode != "connected" or not self._config:
            self.error.emit("Connect to a lobby first.")
            return
        if service_id in self._client_engines:
            return
        spec = self._find_remote_service(service_id)
        if spec is None:
            self.error.emit("That shared port is no longer available.")
            return
        try:
            engine, mapping = self._start_client_service(spec)
        except Exception as exc:
            self.log.exception("Could not open shared port locally")
            self.error.emit(f"Couldn't open that port on this computer: {exc}")
            return
        self._client_engines[service_id] = engine
        mappings = tuple(list(self._snapshot.mappings) + [mapping])
        status = f"{self._snapshot.service_name} is ready." if mappings else self._snapshot.status
        self._set(status=status, mappings=mappings)

    def revoke_client_service(self, service_id: str) -> None:
        engine = self._client_engines.pop(service_id, None)
        if engine is not None:
            try:
                engine.stop()
            except Exception:
                self.log.exception("Client port cleanup failed")
        mappings = tuple(m for m in self._snapshot.mappings if m.service_id != service_id)
        status = (
            f"{self._snapshot.service_name} is ready."
            if mappings
            else "Connected. No shared ports are open on this computer."
        )
        self._set(status=status, mappings=mappings)

    def remap_client_service(self, service_id: str, local_port: int = 0, bind_host: str | None = None) -> None:
        """Move one local listener while keeping the Steam session alive."""
        if self._snapshot.mode != "connected" or not self._config:
            self.error.emit("Connect to a lobby first.")
            return
        service_id = str(service_id)
        old_engine = self._client_engines.get(service_id)
        spec = self._find_remote_service(service_id)
        old_mapping = next((item for item in self._snapshot.mappings if item.service_id == service_id), None)
        if old_engine is None or spec is None or old_mapping is None:
            self.error.emit("That shared port is not currently open.")
            return
        host = str(bind_host or old_mapping.bind_host or self.prefs.prefs.bind_address)
        requested = int(local_port or 0)
        if requested <= 0:
            try:
                requested = find_free_port(spec.protocol, host)
            except OSError as exc:
                self.error.emit(f"Couldn't find a free local port: {exc}")
                return
        if not 1 <= requested <= 65535:
            self.error.emit("Local port must be between 1 and 65535.")
            return
        if requested == old_mapping.local_port and host == old_mapping.bind_host:
            return
        if not bind_available(spec.protocol, host, requested):
            self.error.emit(f"Local {spec.protocol} port {requested} is already in use.")
            return
        try:
            # Open the replacement first. The old listener remains usable until
            # the new one is confirmed, so a remap does not drop the Steam peer.
            replacement, mapping = self._start_client_service(
                spec, local_port=requested, bind_host=host
            )
        except Exception as exc:
            self.log.exception("Could not remap shared port")
            self.error.emit(f"Couldn't remap that local port: {exc}")
            return
        self._client_engines[service_id] = replacement
        try:
            old_engine.stop()
        except Exception:
            self.log.exception("Old remapped tunnel cleanup failed")
        mappings = tuple(mapping if item.service_id == service_id else item for item in self._snapshot.mappings)
        self._set(status=f"{self._snapshot.service_name} is ready.", mappings=mappings)

    @Slot(str)
    def _on_auth_denied(self, text: str) -> None:
        self.error.emit(text or "The host didn't allow this connection.")
        self.stop()

    @Slot(str)
    def _on_auth_revoked(self, text: str) -> None:
        self.error.emit(text or "The host removed your access.")
        self.stop()

    @Slot(str)
    def _on_auth_disconnected(self, text: str) -> None:
        self.error.emit(text or "The host disconnected you.")
        self.stop()

    @Slot(str)
    def _on_config_updated(self, text: str) -> None:
        """Apply a host reconfiguration without leaving the Steam lobby."""
        if self._snapshot.mode not in {"joining", "connected"} or not self._config:
            return
        try:
            updated = SessionConfig.from_json(str(text or ""))
            if int(updated.host_id) != int(self._config.host_id):
                raise ValueError("The updated lobby configuration has a different host.")
        except Exception as exc:
            self.log.warning("Ignoring invalid lobby configuration update: %s", exc)
            return

        old_config = self._config
        old_specs = {spec.service_id: spec for spec in old_config.services}
        old_mappings = {item.service_id: item for item in self._snapshot.mappings}
        selected = set(self._client_engines)
        if self.prefs.prefs.auto_accept_ports:
            selected = {spec.service_id for spec in updated.services}
        updated_specs = {spec.service_id: spec for spec in updated.services}
        started: dict[str, TunnelEngine] = {}
        mappings_by_id: dict[str, LocalMapping] = {}
        for service_id, engine in self._client_engines.items():
            unchanged = (
                service_id in selected
                and service_id in old_specs
                and service_id in updated_specs
                and (
                    old_specs[service_id].protocol.upper(), int(old_specs[service_id].port), int(old_specs[service_id].channel)
                ) == (
                    updated_specs[service_id].protocol.upper(), int(updated_specs[service_id].port), int(updated_specs[service_id].channel)
                )
                and service_id in old_mappings
            )
            if unchanged:
                started[service_id] = engine
                mappings_by_id[service_id] = replace(
                    old_mappings[service_id], name=updated_specs[service_id].name,
                    remote_port=int(updated_specs[service_id].port),
                )
                continue
            try:
                engine.stop()
            except Exception:
                self.log.exception("Client forwarding cleanup failed during reconfiguration")

        try:
            for spec in updated.services:
                if spec.service_id not in selected or spec.service_id in started:
                    continue
                engine, mapping = self._start_client_service(spec)
                started[spec.service_id] = engine
                mappings_by_id[spec.service_id] = mapping
        except Exception as exc:
            for service_id, engine in started.items():
                if service_id not in self._client_engines:
                    engine.stop()
            self.log.exception("Updated client forwarding failed")
            self.error.emit(f"The host changed its shared ports, but they could not be reopened: {exc}")
            self._config = updated
            preserved = {
                service_id: engine for service_id, engine in started.items()
                if service_id in self._client_engines and service_id in mappings_by_id
            }
            self._client_engines = preserved
            preserved_mappings = tuple(
                mappings_by_id[spec.service_id]
                for spec in updated.services if spec.service_id in preserved
            )
            self._set(
                mappings=preserved_mappings,
                status=(f"{self._snapshot.service_name} is ready." if preserved_mappings
                        else "Connected. No shared ports are open on this computer."),
            )
            return

        self._config = updated
        self._client_engines = started
        mappings = [mappings_by_id[spec.service_id] for spec in updated.services if spec.service_id in mappings_by_id]
        status = (
            f"{self._snapshot.service_name} is ready."
            if mappings else "Connected. No shared ports are open on this computer."
        )
        self._set(
            status=status,
            lobby_name=updated.lobby_name,
            visibility=updated.visibility,
            max_members=updated.max_members,
            host_name=updated.host_name,
            service_name=self._display_service_name(updated.services),
            mappings=tuple(mappings),
        )

    @Slot(object)
    def _on_peer_activity(self, steam_id) -> None:
        sid = int(steam_id)
        if sid in self._peer_engines:




            current = next((peer for peer in self._snapshot.peers if int(peer.steam_id) == sid), None)
            if current is None or current.status != "Connected":
                self._refresh_peer_snapshot(connected_sid=sid)
        elif (
            self._snapshot.mode == "connected"
            and self._config
            and sid == self._config.host_id
            and self._snapshot.status.startswith("Connection lost")
        ):
            status = (
                f"{self._snapshot.service_name} is ready."
                if self._snapshot.mappings
                else "Connected. No shared ports are open on this computer."
            )
            self._set(status=status)

    @Slot(object)
    def _on_peer_health(self, payload) -> None:
        try:
            sid, ping_ms, state = int(payload[0]), int(payload[1]), str(payload[2])
        except (TypeError, ValueError, IndexError):
            return
        if sid <= 0 or state not in {"connecting", "connected", "unresponsive"}:
            return
        self._peer_health[sid] = (max(-1, ping_ms), state, time.monotonic())
        if self._snapshot.mode == "sharing" and (
            sid in self._peer_engines or sid in self._pending_approvals
        ):
            self._refresh_peer_snapshot()
            return
        if (
            self._config
            and sid == int(self._config.host_id)
            and self._snapshot.mode in {"joining", "connected"}
        ):
            rows = self._member_telemetry_rows(self._snapshot.members)
            changes = {"members": rows}
            if self._snapshot.mode == "connected" and state == "unresponsive":
                changes["status"] = "P2P connection interrupted. Reconnecting…"
            elif self._snapshot.mode == "connected" and state == "connected" and self._snapshot.status.startswith(
                ("P2P connection interrupted", "Connection lost", "Steam P2P error")
            ):
                changes["status"] = (
                    f"{self._snapshot.service_name} is ready."
                    if self._snapshot.mappings
                    else "Connected. No shared ports are open on this computer."
                )
            self._set(**changes)

    def _peer_state(self, steam_id: int, name: str, status: str) -> PeerState:
        sid = int(steam_id)
        own = int(self.steam.steam_id()) if self.steam.initialized else 0
        ping_ms, upload_bps, download_bps = (-1, 0.0, 0.0)
        if sid != own:
            ping_ms, upload_bps, download_bps = self.service.peer_network_stats(sid)
        health_ping, health_state, _seen = self._peer_health.get(sid, (-1, "unknown", 0.0))
        if ping_ms < 0 and health_ping >= 0:
            ping_ms = health_ping
        network_state = health_state
        if sid == own:
            network_state = "local"
        elif network_state == "unknown" and ping_ms >= 0:
            network_state = "connected"
        avatar, avatar_width, avatar_height = self.service.friend_avatar(sid)
        return PeerState(
            steam_id=sid,
            name=str(name),
            status=str(status),
            ping_ms=ping_ms,
            upload_bps=upload_bps,
            download_bps=download_bps,
            avatar_rgba=avatar,
            avatar_width=avatar_width,
            avatar_height=avatar_height,
            network_state=network_state,
        )

    def _refresh_peer_snapshot(self, connecting_sid: int | None = None, connected_sid: int | None = None) -> None:
        if self._snapshot.mode != "sharing":
            return
        old_status = {p.steam_id: p.status for p in self._snapshot.peers}
        peers: list[PeerState] = []
        for sid in sorted(self._pending_approvals, key=lambda x: self.service.friend_name(x).casefold()):
            peers.append(self._peer_state(sid, self.service.friend_name(sid), "Waiting for approval"))
        for sid in sorted(self._peer_engines, key=lambda x: self.service.friend_name(x).casefold()):
            status = old_status.get(sid, "Connecting…")
            health_state = self._peer_health.get(sid, (-1, "connecting", 0.0))[1]
            if health_state == "connected":
                status = "Connected"
            elif health_state == "unresponsive":
                status = "Reconnecting…"
            if sid == connecting_sid:
                status = "Connecting…"
            if sid == connected_sid:
                status = "Connected"
            peers.append(self._peer_state(sid, self.service.friend_name(sid), status))
        self._set(peers=tuple(peers))
        # Keep the Members card synchronized with access/kick state changes even
        # when Steam's member-ID set itself has not changed.
        if self._config and self._snapshot.lobby_id:
            try:
                members = self.steam.lobby_members(self._snapshot.lobby_id)
            except Exception:
                return
            self._set(
                member_count=max(1, len(members)),
                members=self._member_rows(members, self._config.host_id),
            )

    def _stop_peer_engines(self, sid: int) -> None:
        self._peer_health.pop(int(sid), None)
        engines = self._peer_engines.pop(int(sid), [])
        for engine in engines:
            try:
                engine.stop()
            except Exception:
                self.log.exception("Tunnel cleanup failed")

    def _member_rows(self, members, host_id: int) -> tuple[PeerState, ...]:
        rows: list[PeerState] = []
        own = int(self.steam.steam_id()) if self.steam.initialized else 0
        for sid in sorted({int(x) for x in members if int(x) > 0}, key=lambda x: (x != host_id, self.service.friend_name(x).casefold())):
            if sid == host_id:
                status = "Host"
            elif sid == own:
                status = "You"
            elif sid in self._kicked_members:
                status = "Kicked"
            elif sid in self._peer_engines:
                status = next((p.status for p in self._snapshot.peers if p.steam_id == sid), "Connected")
            elif sid in self._pending_approvals:
                status = "Awaiting approval"
            else:
                status = "In lobby"
            name = self._config.host_name if self._config and sid == self._config.host_id else self.service.friend_name(sid)
            rows.append(self._peer_state(sid, name, status))
        return tuple(rows)

    def _member_telemetry_rows(self, members: tuple[PeerState, ...]) -> tuple[PeerState, ...]:
        """Refresh only volatile network statistics for an unchanged member set."""
        own = int(self.steam.steam_id()) if self.steam.initialized else 0
        rows: list[PeerState] = []
        for member in members:
            sid = int(member.steam_id)
            if sid == own:
                ping_ms, upload_bps, download_bps = -1, 0.0, 0.0
            else:
                ping_ms, upload_bps, download_bps = self.service.peer_network_stats(sid)
                health_ping, health_state, _seen = self._peer_health.get(sid, (-1, "unknown", 0.0))
                if ping_ms < 0 and health_ping >= 0:
                    ping_ms = health_ping
            network_state = "local" if sid == own else self._peer_health.get(
                sid, (-1, member.network_state, 0.0)
            )[1]
            if (
                int(member.ping_ms) == int(ping_ms)
                and float(member.upload_bps) == float(upload_bps)
                and float(member.download_bps) == float(download_bps)
                and member.network_state == network_state
            ):
                rows.append(member)
            else:
                rows.append(
                    replace(
                        member,
                        ping_ms=int(ping_ms),
                        upload_bps=float(upload_bps),
                        download_bps=float(download_bps),
                        network_state=network_state,
                    )
                )
        return tuple(rows)

    @staticmethod
    def _same_member_ids(current: tuple[PeerState, ...], member_ids: set[int]) -> bool:
        return {int(member.steam_id) for member in current} == {int(sid) for sid in member_ids if int(sid) > 0}

    @Slot()
    def _check_members(self) -> None:
        if not self._snapshot.lobby_id:
            return
        try:
            members = set(self.steam.lobby_members(self._snapshot.lobby_id))
        except Exception:
            return

        if self._snapshot.mode in {"joining", "connected"} and self._config:
            host_membership = self._membership_guard.check(self._config.host_id, members)
            if host_membership is None:
                # Immediately after LobbyEnter, Steam can briefly expose only a
                # partial member list. Do not turn that synchronization window
                # into a false "host stopped" disconnect.
                return
            if not host_membership:
                self.error.emit("The host stopped this SteamyLAN lobby.")
                self.stop()
                return
            if self._same_member_ids(self._snapshot.members, members):
                rows = self._member_telemetry_rows(self._snapshot.members)
            else:
                rows = self._member_rows(members, self._config.host_id)
            self._set(member_count=max(1, len(members)), members=rows)
            return
        if self._snapshot.mode != "sharing" or not self._config:
            return

        own = self.steam.steam_id()
        remote_members = set(members)
        remote_members.discard(own)
        changed = False
        for sid in list(self._peer_engines):
            if sid not in remote_members:
                self._stop_peer_engines(sid)
                if self._chat:
                    self._chat.remove_peer(sid)
                if self._host_control:
                    self._host_control.remove_peer(sid)
                try:
                    self.steam.close_peer(sid)
                except Exception:
                    pass
                changed = True
        for sid in list(self._pending_approvals):
            if sid not in remote_members:
                self._pending_approvals.discard(sid)
                changed = True
        if changed:
            self._refresh_peer_snapshot()
        if not changed and self._same_member_ids(self._snapshot.members, members):
            rows = self._member_telemetry_rows(self._snapshot.members)
        else:
            rows = self._member_rows(members, self._config.host_id)
        member_count = max(1, len(members))
        if member_count != self._snapshot.member_count:
            try:
                self.steam.set_lobby_data(self._snapshot.lobby_id, LOBBY_DATA_MEMBER_COUNT_KEY, str(member_count))
            except Exception:
                self.log.debug("Could not publish lobby member count", exc_info=True)
        self._set(member_count=member_count, members=rows)

    def stop(self) -> None:
        old = self._snapshot
        self._member_timer.stop()
        self._pending_share = None
        self._joining_host = None
        self._join_loading = False
        self._pending_approvals.clear()
        self._kicked_members.clear()
        self._disconnect_acked.clear()
        self._membership_guard.clear()
        self._peer_health.clear()
        self._join_attempt_id += 1
        self._join_secret = None
        self._invite_secret = None
        self._static_invite_secret = None
        self._password_key = None
        self._host_password = ""
        self._join_password = ""
        self._pending_password_config = None
        self._code_invite_pending = False
        self._join_call_started = False

        if self._invite_broker:
            self._invite_broker.stop()
            self._invite_broker = None
        if self._chat:
            self._chat.stop()
            self._chat = None
        self.chatStateChanged.emit(False)
        self._chat_messages.clear()
        self.chatChanged.emit(tuple())

        if self._host_control:
            if old.mode == "sharing":
                for sid in tuple(self._peer_engines):
                    try:
                        self._host_control.disconnect(sid, "The host stopped sharing.")
                    except Exception:
                        self.log.debug("Could not notify peer %s before stopping", sid, exc_info=True)
            self._host_control.stop()
            self._host_control = None
        if self._client_control:
            self._client_control.stop()
            self._client_control = None

        for sid in list(self._peer_engines):
            self._stop_peer_engines(sid)
            try:
                self.steam.close_peer(sid)
            except Exception:
                pass
        for engine in self._client_engines.values():
            try:
                engine.stop()
            except Exception:
                pass
        self._client_engines.clear()

        if self._config and old.mode in {"joining", "connected"}:
            try:
                self.steam.close_peer(self._config.host_id)
            except Exception:
                pass
        if old.lobby_id:
            try:
                self.steam.leave_lobby(old.lobby_id)
            except Exception:
                pass

        self._config = None
        self._shared_service = None
        self._endpoint_map = {}
        self._snapshot = AppSnapshot()
        self.changed.emit(self._snapshot)
        self.refresh_steam_status()
        QTimer.singleShot(500, self.service.refresh_lobbies)

    def refresh_steam_status(self) -> None:
        if not self.steam.initialized:
            if self._snapshot.mode in {"sharing", "connected"}:
                self._steam_status_timer.start()
            return
        try:
            if not self.prefs.prefs.show_steam_status:
                self.steam.clear_rich_presence()
                self._steam_status_timer.stop()
                return
            if self._snapshot.mode == "sharing":
                detail = self._snapshot.service_name or self._snapshot.lobby_name or "Server"
                updated = self.steam.set_rich_presence(f"SteamyLAN - Sharing {detail}")
            elif self._snapshot.mode == "connected":
                detail = self._snapshot.service_name or self._snapshot.lobby_name or "Server"
                updated = self.steam.set_rich_presence(f"SteamyLAN - Joined {detail}")
            else:
                self.steam.clear_rich_presence()
                self._steam_status_timer.stop()
                return
            if updated:
                self._steam_status_timer.stop()
            else:
                self._steam_status_timer.start()
        except Exception:
            self.log.debug("Could not update Steam rich presence", exc_info=True)
            self._steam_status_timer.start()

    @staticmethod
    def _display_service_name(services) -> str:
        names: list[str] = []
        for service in services:
            if service.name not in names:
                names.append(service.name)
        return names[0] if len(names) == 1 else ", ".join(names[:3])
