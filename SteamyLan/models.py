from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable

from .lobby_password import validate_password_salt

from .constants import (
    CHANNEL_MAX,
    CHANNEL_MIN,
    CHAT_CHANNEL_MAX,
    CHAT_CHANNEL_MIN,
    CONTROL_CHANNEL_MAX,
    CONTROL_CHANNEL_MIN,
    LOBBY_CONFIG_MAX_BYTES,
    PROTOCOL_VERSION,
    VISIBILITY_VALUES,
)


def _one_line(value, limit: int, fallback: str = "") -> str:
    """Normalize untrusted/network-provided display text for compact Qt labels."""
    text = " ".join(str(value or "").replace("\x00", " ").split())[: int(limit)]
    return text or fallback


@dataclass(frozen=True, slots=True)
class ProgramInfo:
    pid: int
    exe: str
    process_name: str
    display_name: str
    subtitle: str
    started_at: float
    window_title: str = ""
    steam_appid: int | None = None
    icon_path: str = ""
    cmdline: tuple[str, ...] = ()
    parent_names: tuple[str, ...] = ()
    recommended: bool = False
    group_key: str = ""

    @property
    def key(self) -> str:
        return self.group_key or f"{self.exe.casefold()}|{self.display_name.casefold()}"


@dataclass(frozen=True, slots=True)
class FriendInfo:
    steam_id: int
    name: str
    state: str = "Offline"
    state_num: int = 0
    avatar_rgba: bytes = b""
    avatar_width: int = 0
    avatar_height: int = 0

    @property
    def online(self) -> bool:
        return self.state_num != 0

    @property
    def category(self) -> str:
        if self.state_num == 0:
            return "offline"
        if self.state_num in {2, 3, 4}:
            return "away"
        return "online"


@dataclass(frozen=True, slots=True)
class Endpoint:
    protocol: str
    port: int
    local_ip: str = "127.0.0.1"

    @property
    def key(self) -> tuple[str, int]:
        return self.protocol.upper(), int(self.port)


@dataclass(frozen=True, slots=True)
class DetectedService:
    key: str
    name: str
    process_name: str
    pid: int
    endpoints: tuple[Endpoint, ...]
    confidence: int = 0
    known_game: bool = False
    exe_path: str = ""
    description: str = ""
    window_title: str = ""
    icon_path: str = ""
    started_at: float = 0.0
    cmdline: tuple[str, ...] = ()
    steam_appid: int | None = None
    warning: str = ""


@dataclass(frozen=True, slots=True)
class SharedServiceSpec:
    service_id: str
    name: str
    protocol: str
    port: int
    channel: int

    def to_dict(self) -> dict:
        return {
            "service_id": self.service_id,
            "name": _one_line(self.name, 120, "Server"),
            "protocol": self.protocol.upper(),
            "port": int(self.port),
            "channel": int(self.channel),
        }

    @classmethod
    def from_dict(cls, raw: dict) -> "SharedServiceSpec":
        if not isinstance(raw, dict):
            raise ValueError("Invalid shared service entry.")
        service_id = _one_line(raw.get("service_id", ""), 80)
        name = _one_line(raw.get("name", "Server"), 120, "Server")
        protocol = str(raw.get("protocol", "")).upper()
        port = int(raw.get("port", 0))
        channel = int(raw.get("channel", 0))
        if not service_id or len(service_id) > 80:
            raise ValueError("Invalid service identifier.")
        if protocol not in {"TCP", "UDP"}:
            raise ValueError("Unsupported service protocol.")
        if not 1 <= port <= 65535:
            raise ValueError("Invalid service port.")
        if not CHANNEL_MIN <= channel <= CHANNEL_MAX:
            raise ValueError("Invalid Steam channel.")
        return cls(service_id, name, protocol, port, channel)


@dataclass(frozen=True, slots=True)
class SessionConfig:
    session_id: str
    host_id: int
    host_name: str
    control_channel: int
    chat_channel: int
    services: tuple[SharedServiceSpec, ...]
    lobby_name: str = "SteamyLAN Server"
    visibility: str = "friends"
    max_members: int = 8
    password_salt: str = ""
    version: int = PROTOCOL_VERSION

    def to_json(self) -> str:
        text = json.dumps(
            {
                "version": int(self.version),
                "session_id": self.session_id,
                "host_id": str(int(self.host_id)),
                "host_name": _one_line(self.host_name, 120, "Steam user"),
                "control_channel": int(self.control_channel),
                "chat_channel": int(self.chat_channel),
                "lobby_name": _one_line(self.lobby_name, 80, "SteamyLAN Server"),
                "visibility": self.visibility,
                "max_members": int(self.max_members),
                "password_salt": validate_password_salt(self.password_salt),
                "services": [s.to_dict() for s in self.services],
            },
            separators=(",", ":"),
            ensure_ascii=False,
        )
        if len(text.encode("utf-8")) > LOBBY_CONFIG_MAX_BYTES:
            raise ValueError("The lobby configuration is too large for Steam metadata. Share fewer ports or use shorter names.")
        return text

    @classmethod
    def from_json(cls, text: str) -> "SessionConfig":
        if len(str(text).encode("utf-8")) > LOBBY_CONFIG_MAX_BYTES:
            raise ValueError("SteamyLAN lobby configuration is too large.")
        raw = json.loads(text)
        if not isinstance(raw, dict):
            raise ValueError("Invalid SteamyLAN session data.")
        version = int(raw.get("version", -1))
        if version != PROTOCOL_VERSION:
            raise ValueError(f"Incompatible SteamyLAN version ({version}).")
        session_id = _one_line(raw.get("session_id", ""), 80)
        host_id = int(raw.get("host_id", 0))
        host_name = _one_line(raw.get("host_name", "Steam user"), 120, "Steam user")
        control_channel = int(raw.get("control_channel", 0))
        chat_channel = int(raw.get("chat_channel", 0))
        lobby_name = _one_line(raw.get("lobby_name", "SteamyLAN Server"), 80, "SteamyLAN Server")
        visibility = str(raw.get("visibility", "friends")).strip().casefold()
        max_members = int(raw.get("max_members", 8))
        password_salt = validate_password_salt(raw.get("password_salt", ""))
        if not session_id or len(session_id) > 80 or host_id <= 0:
            raise ValueError("Invalid SteamyLAN session.")
        if not CONTROL_CHANNEL_MIN <= control_channel <= CONTROL_CHANNEL_MAX:
            raise ValueError("Invalid SteamyLAN control channel.")
        if not CHAT_CHANNEL_MIN <= chat_channel <= CHAT_CHANNEL_MAX:
            raise ValueError("Invalid SteamyLAN chat channel.")
        if chat_channel == control_channel:
            raise ValueError("SteamyLAN control and chat channels must be distinct.")
        if visibility not in VISIBILITY_VALUES:
            raise ValueError("Invalid SteamyLAN lobby visibility.")
        if not 2 <= max_members <= 250:
            raise ValueError("Invalid SteamyLAN lobby member limit.")
        raw_services = raw.get("services", [])
        if not isinstance(raw_services, list):
            raise ValueError("Invalid SteamyLAN service list.")
        services = tuple(SharedServiceSpec.from_dict(x) for x in raw_services)
        if not services or len(services) > 32:
            raise ValueError("SteamyLAN session does not contain a valid service list.")
        endpoint_keys = {(s.protocol, s.port) for s in services}
        if len(endpoint_keys) != len(services):
            raise ValueError("Duplicate service mappings are not allowed.")
        service_ids = {s.service_id for s in services}
        if len(service_ids) != len(services):
            raise ValueError("Duplicate service identifiers are not allowed.")
        channels = {s.channel for s in services}
        if len(channels) != len(services):
            raise ValueError("Duplicate service channels are not allowed.")
        if control_channel in channels or chat_channel in channels:
            raise ValueError("Reserved SteamyLAN channels overlap a service channel.")
        return cls(
            session_id=session_id,
            host_id=host_id,
            host_name=host_name,
            control_channel=control_channel,
            chat_channel=chat_channel,
            services=services,
            lobby_name=lobby_name,
            visibility=visibility,
            max_members=max_members,
            password_salt=password_salt,
            version=version,
        )


@dataclass(frozen=True, slots=True)
class SharingHost:
    lobby_id: int
    host_id: int
    host_name: str
    services: tuple[SharedServiceSpec, ...]
    session_id: str
    lobby_name: str = "SteamyLAN Server"
    visibility: str = "friends"
    max_members: int = 8
    member_count: int = 1
    password_protected: bool = False

    @property
    def open_slots(self) -> int:
        return max(0, int(self.max_members) - int(self.member_count))


@dataclass(frozen=True, slots=True)
class LocalMapping:
    service_id: str
    name: str
    protocol: str
    remote_port: int
    local_port: int
    bind_host: str = "127.0.0.1"

    @property
    def address(self) -> str:
        # Wildcard bind addresses are listeners, not useful destination
        # addresses.  Copy a loopback destination for the local game while
        # still allowing 0.0.0.0/:: to expose the tunnel on other interfaces.
        host = str(self.bind_host or "127.0.0.1")
        if host == "0.0.0.0":
            host = "127.0.0.1"
        elif host in {"::", "::0"}:
            host = "::1"
        return f"[{host}]:{self.local_port}" if ":" in host else f"{host}:{self.local_port}"


@dataclass(frozen=True, slots=True)
class PeerState:
    steam_id: int
    name: str
    status: str
    ping_ms: int = -1
    upload_bps: float = 0.0
    download_bps: float = 0.0
    avatar_rgba: bytes = b""
    avatar_width: int = 0
    avatar_height: int = 0
    network_state: str = "unknown"


@dataclass(frozen=True, slots=True)
class ChatMessage:
    sender_id: int
    sender_name: str
    text: str
    created_at: float


@dataclass(frozen=True, slots=True)
class AppSnapshot:
    mode: str = "idle"
    status: str = ""
    lobby_id: int = 0
    lobby_name: str = ""
    visibility: str = ""
    max_members: int = 0
    member_count: int = 0
    host_name: str = ""
    service_name: str = ""
    mappings: tuple[LocalMapping, ...] = ()
    peers: tuple[PeerState, ...] = ()
    members: tuple[PeerState, ...] = ()
    join_code: str = ""


def unique_endpoints(items: Iterable[Endpoint]) -> tuple[Endpoint, ...]:
    seen: set[tuple[str, int]] = set()
    out: list[Endpoint] = []
    for item in items:
        proto = item.protocol.upper()
        port = int(item.port)
        key = (proto, port)
        if proto not in {"TCP", "UDP"} or not 1 <= port <= 65535 or key in seen:
            continue
        seen.add(key)
        out.append(Endpoint(proto, port, item.local_ip))
    return tuple(sorted(out, key=lambda x: (x.protocol, x.port)))
