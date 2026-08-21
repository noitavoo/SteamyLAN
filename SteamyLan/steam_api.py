from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import psutil

from .constants import (
    APP_NAME,
    CALLBACK_GAME_LOBBY_JOIN_REQUESTED,
    CALLBACK_LOBBY_CREATED,
    CALLBACK_LOBBY_ENTER,
    CALLBACK_LOBBY_INVITE,
    CALLBACK_LOBBY_MATCH_LIST,
    CALLBACK_P2P_SESSION_CONNECT_FAIL,
    CALLBACK_P2P_SESSION_REQUEST,
    CALLBACK_PERSONA_STATE_CHANGE,
    CALLBACK_STEAM_API_CALL_COMPLETED,
    DEFAULT_APP_ID,
    FRIEND_IMMEDIATE,
    LOBBY_FRIENDS_ONLY,
    LOBBY_PUBLIC,
    MAX_STEAM_PACKET,
    P2P_RELIABLE,
    P2P_UNRELIABLE,
)
from .models import FriendInfo





_STEAM_NET_CONFIG_GLOBAL = 1
_STEAM_NET_CONFIG_INT32 = 1
_STEAM_NET_CONFIG_STRING = 4
_STEAM_NET_CONFIG_SEND_RATE_MIN = 10
_STEAM_NET_CONFIG_SEND_RATE_MAX = 11
_STEAM_NET_CONFIG_P2P_ICE_ENABLE = 104
_STEAM_NET_CONFIG_P2P_ICE_PENALTY = 105
_STEAM_NET_CONFIG_P2P_SDR_PENALTY = 106
_STEAM_NET_CONFIG_SDR_FORCE_RELAY_CLUSTER = 29
_STEAM_NET_ICE_DISABLE = 0
_STEAM_NET_ICE_ALL = 0x7FFFFFFF


class _RateLimiter:
    """Thread-safe shared byte-rate limiter used for user bandwidth caps."""

    def __init__(self, bytes_per_second: int = 0):
        self._lock = threading.Lock()
        self._rate = max(0, int(bytes_per_second))
        self._next_time = 0.0

    def set_rate(self, bytes_per_second: int) -> None:
        with self._lock:
            self._rate = max(0, int(bytes_per_second))
            self._next_time = 0.0

    def wait(self, byte_count: int) -> None:
        size = max(0, int(byte_count))
        if size <= 0:
            return
        delay = 0.0
        with self._lock:
            rate = self._rate
            if rate <= 0:
                return
            now = time.monotonic()
            start = max(now, self._next_time)
            self._next_time = start + (size / rate)
            delay = start - now
        if delay > 0:
            time.sleep(delay)


class SteamError(RuntimeError):
    pass


class SteamInitError(SteamError):
    def __init__(self, message: str, result_code: int | None = None):
        super().__init__(message)
        self.result_code = result_code


def application_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def runtime_directory() -> Path:
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home())) / APP_NAME / "runtime"
    else:
        base = Path.home() / ".steamylan" / "runtime"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _unique_paths(paths) -> list[Path]:
    result: list[Path] = []
    seen: set[str] = set()
    for item in paths:
        if not item:
            continue
        try:
            path = Path(item).expanduser().resolve()
        except Exception:
            continue
        key = str(path).casefold()
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def find_steam_dll(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    """Find the user-supplied Steamworks redistributable near SteamyLAN.

    SteamyLAN intentionally does not copy a steam_api64.dll from random installed
    games. That DLL belongs to the Steamworks SDK version the application was
    built against. We only look in SteamyLAN-controlled/nearby locations.
    """
    root = application_dir()
    package_dir = Path(__file__).resolve().parent
    executable_dir = Path(sys.executable).resolve().parent
    env_override = os.environ.get("STEAMYLAN_STEAM_API64") or os.environ.get("STEAM_API64_DLL")

    base_dirs = [
        root,
        root / "bin",
        root / "lib",
        root / "_internal",
        package_dir,
        executable_dir,
        Path.cwd(),
    ]
    if getattr(sys, "_MEIPASS", None):
        base_dirs.append(Path(sys._MEIPASS))

    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    if env_override:
        candidates.append(Path(env_override))
    candidates.extend(directory / "steam_api64.dll" for directory in _unique_paths(base_dirs))

    for candidate in _unique_paths(candidates):
        if candidate.is_file():
            return candidate
    return None


def find_steam_executable() -> Path | None:
    """Locate the installed Steam client without relying on a hard-coded path."""
    if os.name != "nt":
        return None



    try:
        for proc in psutil.process_iter(["name", "exe"]):
            try:
                if (proc.info.get("name") or "").casefold() != "steam.exe":
                    continue
                exe = proc.info.get("exe")
                if exe and Path(exe).is_file():
                    return Path(exe).resolve()
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
    except Exception:
        pass

    candidates: list[Path] = []
    try:
        import winreg

        registry_locations = [
            (winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam"),
        ]
        views = [0]
        if hasattr(winreg, "KEY_WOW64_32KEY"):
            views.extend([winreg.KEY_WOW64_32KEY, winreg.KEY_WOW64_64KEY])
        for hive, key_name in registry_locations:
            for view in views:
                try:
                    with winreg.OpenKey(hive, key_name, 0, winreg.KEY_READ | view) as key:
                        for value_name in ("SteamExe", "InstallPath", "SteamPath"):
                            try:
                                value, _ = winreg.QueryValueEx(key, value_name)
                            except OSError:
                                continue
                            if not value:
                                continue
                            value_path = Path(str(value).replace("/", os.sep))
                            candidates.append(
                                value_path if value_path.name.casefold() == "steam.exe" else value_path / "steam.exe"
                            )
                except OSError:
                    continue
    except Exception:
        pass

    for env_name in ("ProgramFiles(x86)", "ProgramFiles"):
        base = os.environ.get(env_name)
        if base:
            candidates.append(Path(base) / "Steam" / "steam.exe")

    for candidate in _unique_paths(candidates):
        if candidate.is_file():
            return candidate
    return None


def steam_process_running() -> bool:
    if os.name != "nt":
        return False
    try:
        for proc in psutil.process_iter(["name"]):
            try:
                if (proc.info.get("name") or "").casefold() == "steam.exe":
                    return True
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
    except Exception:
        pass
    return False


def start_steam_client(*, show_window: bool = False) -> bool:
    """Ensure Steam is running without disturbing an existing client window.

    If Steam is already running, this function is deliberately a no-op.  In
    particular, it does not open a ``steam://`` URI, activate Steam's main
    window, or otherwise steal focus from SteamyLAN.  ``show_window`` only
    controls how a *new* Steam process is launched when no client is running.
    """
    if os.name != "nt":
        return False

    if steam_process_running():
        return True

    exe = find_steam_executable()
    if exe:
        try:
            flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
            args = [str(exe)]
            if not show_window:
                args.append("-silent")
            subprocess.Popen(
                args,
                cwd=str(exe.parent),
                close_fds=True,
                creationflags=flags,
            )
            return True
        except Exception:
            pass

    try:
        os.startfile("steam://open/main")
        return True
    except Exception:
        return False


class CallbackMsg(ctypes.Structure):
    _fields_ = [
        ("m_hSteamUser", ctypes.c_int32),
        ("m_iCallback", ctypes.c_int32),
        ("m_pubParam", ctypes.c_void_p),
        ("m_cubParam", ctypes.c_int32),
    ]


class SteamAPICallCompleted(ctypes.Structure):
    _fields_ = [
        ("m_hAsyncCall", ctypes.c_uint64),
        ("m_iCallback", ctypes.c_int32),
        ("m_cubParam", ctypes.c_uint32),
    ]


class LobbyInvite(ctypes.Structure):
    _fields_ = [
        ("m_ulSteamIDUser", ctypes.c_uint64),
        ("m_ulSteamIDLobby", ctypes.c_uint64),
        ("m_ulGameID", ctypes.c_uint64),
    ]


class PersonaStateChange(ctypes.Structure):
    _fields_ = [
        ("m_ulSteamID", ctypes.c_uint64),
        ("m_nChangeFlags", ctypes.c_int32),
    ]


class GameLobbyJoinRequested(ctypes.Structure):
    _fields_ = [
        ("m_steamIDLobby", ctypes.c_uint64),
        ("m_steamIDFriend", ctypes.c_uint64),
    ]


class LobbyEnter(ctypes.Structure):
    _fields_ = [
        ("m_ulSteamIDLobby", ctypes.c_uint64),
        ("m_rgfChatPermissions", ctypes.c_uint32),
        ("m_bLocked", ctypes.c_uint8),
        ("_pad", ctypes.c_uint8 * 3),
        ("m_EChatRoomEnterResponse", ctypes.c_uint32),
    ]


class LobbyCreated(ctypes.Structure):
    _fields_ = [
        ("m_eResult", ctypes.c_int32),
        ("_pad", ctypes.c_uint32),
        ("m_ulSteamIDLobby", ctypes.c_uint64),
    ]


class LobbyMatchList(ctypes.Structure):
    _fields_ = [("m_nLobbiesMatching", ctypes.c_uint32)]


class SteamFriendGameInfo(ctypes.Structure):
    _fields_ = [
        ("m_gameID", ctypes.c_uint64),
        ("m_unGameIP", ctypes.c_uint32),
        ("m_usGamePort", ctypes.c_uint16),
        ("m_usQueryPort", ctypes.c_uint16),
        ("m_steamIDLobby", ctypes.c_uint64),
    ]


class P2PSessionRequest(ctypes.Structure):
    _fields_ = [("m_steamIDRemote", ctypes.c_uint64)]


class P2PSessionConnectFail(ctypes.Structure):
    _fields_ = [
        ("m_steamIDRemote", ctypes.c_uint64),
        ("m_eP2PSessionError", ctypes.c_uint8),
        ("_pad", ctypes.c_uint8 * 7),
    ]


class SteamNetworkingIdentityData(ctypes.Union):
    _fields_ = [
        ("m_steamID64", ctypes.c_uint64),
        ("m_reserved", ctypes.c_uint32 * 32),
    ]


class SteamNetworkingIdentity(ctypes.Structure):
    _anonymous_ = ("data",)
    _fields_ = [
        ("m_eType", ctypes.c_int32),
        ("m_cbSize", ctypes.c_int32),
        ("data", SteamNetworkingIdentityData),
    ]

    @classmethod
    def for_steam_id(cls, steam_id: int) -> "SteamNetworkingIdentity":
        identity = cls()
        identity.m_eType = 16
        identity.m_cbSize = ctypes.sizeof(ctypes.c_uint64)
        identity.m_steamID64 = ctypes.c_uint64(int(steam_id)).value
        return identity

    def steam_id(self) -> int:
        if int(self.m_eType) != 16 or int(self.m_cbSize) < ctypes.sizeof(ctypes.c_uint64):
            return 0
        return int(self.m_steamID64)


class SteamNetworkingMessage(ctypes.Structure):
    pass


SteamNetworkingMessageRelease = ctypes.CFUNCTYPE(None, ctypes.POINTER(SteamNetworkingMessage))

SteamNetworkingMessage._fields_ = [
    ("m_pData", ctypes.c_void_p),
    ("m_cbSize", ctypes.c_int32),
    ("m_conn", ctypes.c_uint32),
    ("m_identityPeer", SteamNetworkingIdentity),
    ("m_nConnUserData", ctypes.c_int64),
    ("m_usecTimeReceived", ctypes.c_int64),
    ("m_nMessageNumber", ctypes.c_int64),
    ("m_pfnFreeData", ctypes.c_void_p),
    ("m_pfnRelease", SteamNetworkingMessageRelease),
    ("m_nChannel", ctypes.c_int32),
    ("m_nFlags", ctypes.c_int32),
    ("m_nUserData", ctypes.c_int64),
    ("m_idxLane", ctypes.c_uint16),
    ("_pad1", ctypes.c_uint16),
]


class SteamNetworkingMessagesSessionRequest(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("m_identityRemote", SteamNetworkingIdentity)]


class SteamNetworkingIPAddr(ctypes.Structure):
    _fields_ = [
        ("m_ipv6", ctypes.c_uint8 * 16),
        ("m_port", ctypes.c_uint16),
    ]


class SteamNetConnectionInfoPrefix(ctypes.Structure):
    """Native SteamNetConnectionInfo_t fields through the failure debug text."""

    _pack_ = 8
    _fields_ = [
        ("m_identityRemote", SteamNetworkingIdentity),
        ("m_nUserData", ctypes.c_int64),
        ("m_hListenSocket", ctypes.c_uint32),
        ("m_addrRemote", SteamNetworkingIPAddr),
        ("m__pad1", ctypes.c_uint16),
        ("m_idPOPRemote", ctypes.c_uint32),
        ("m_idPOPRelay", ctypes.c_uint32),
        ("m_eState", ctypes.c_int32),
        ("m_eEndReason", ctypes.c_int32),
        ("m_szEndDebug", ctypes.c_char * 128),
    ]


class SteamNetworkingMessagesSessionFailedPrefix(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("m_info", SteamNetConnectionInfoPrefix)]


class SteamNetConnectionRealTimeStatus(ctypes.Structure):


    _pack_ = 8
    _fields_ = [
        ("m_eState", ctypes.c_int32),
        ("m_nPing", ctypes.c_int32),
        ("m_flConnectionQualityLocal", ctypes.c_float),
        ("m_flConnectionQualityRemote", ctypes.c_float),
        ("m_flOutPacketsPerSec", ctypes.c_float),
        ("m_flOutBytesPerSec", ctypes.c_float),
        ("m_flInPacketsPerSec", ctypes.c_float),
        ("m_flInBytesPerSec", ctypes.c_float),
        ("m_nSendRateBytesPerSecond", ctypes.c_int32),
        ("m_cbPendingUnreliable", ctypes.c_int32),
        ("m_cbPendingReliable", ctypes.c_int32),
        ("m_cbSentUnackedReliable", ctypes.c_int32),
        ("m_usecQueueTime", ctypes.c_int64),
        ("m_usecMaxJitter", ctypes.c_int32),
        ("reserved", ctypes.c_uint32 * 15),
    ]


class SteamClient:
    """Purpose-built flat Steamworks binding for SteamyLAN.

    The rest of the application never touches ctypes. All Steam-specific details
    live here so the GUI and tunneling code remain independently testable.
    """

    def __init__(
        self,
        logger,
        *,
        app_id: int = DEFAULT_APP_ID,
        relay_mode: str = "automatic",
        upload_limit_kbps: int = 0,
        download_limit_kbps: int = 0,
        relay_location: str = "automatic",
    ):
        self.log = logger
        self.app_id = max(1, min(0xFFFFFFFF, int(app_id)))
        self.dll = None
        self.user = None
        self.friends = None
        self.utils = None
        self.matchmaking = None
        self.networking = None
        self.networking_utils = None
        self.pipe = 0
        self.initialized = False
        self._api_started = False
        self.dll_path: Path | None = None

        self._relay_mode = "automatic"
        self._upload_limit_kbps = 0
        self._download_limit_kbps = 0
        self._relay_location = "automatic"
        self._upload_limiter = _RateLimiter()
        self._download_limiter = _RateLimiter()

        self._stop = threading.Event()
        self._dispatch_thread: threading.Thread | None = None
        self._event_handlers: list[Callable[[str, dict], None]] = []
        self._friend_name_cache: dict[int, str] = {}
        self._avatar_cache: dict[int, tuple[bytes, int, int]] = {}
        self._avatar_retry_after: dict[int, float] = {}
        self._rich_presence_value: str | None = None

        self._pending_calls: dict[int, tuple[int, type[ctypes.Structure], Callable]] = {}
        self._completed_unclaimed: dict[int, SteamAPICallCompleted] = {}
        self._pending_lock = threading.RLock()
        self._networking_lock = threading.RLock()
        self._peer_traffic: dict[int, dict[str, float]] = {}
        self._dll_directory_handles: list[object] = []
        self.configure_network(
            relay_mode=relay_mode,
            upload_limit_kbps=upload_limit_kbps,
            download_limit_kbps=download_limit_kbps,
            relay_location=relay_location,
        )

    @staticmethod
    def _find_export(dll, names: list[str]):
        for name in names:
            try:
                return getattr(dll, name), name
            except AttributeError:
                continue
        raise SteamError(
            "Required Steam export not found:\n"
            + "\n".join(names)
            + "\n\nYour steam_api64.dll may be from an incompatible Steamworks SDK."
        )

    def add_event_handler(self, fn: Callable[[str, dict], None]) -> None:
        if fn not in self._event_handlers:
            self._event_handlers.append(fn)

    def remove_event_handler(self, fn: Callable[[str, dict], None]) -> None:
        try:
            self._event_handlers.remove(fn)
        except ValueError:
            pass

    def _emit(self, event: str, **data) -> None:
        for fn in tuple(self._event_handlers):
            try:
                fn(event, data)
            except Exception:
                self.log.exception("Steam event handler failed")

    def initialize(self, dll_path: str | os.PathLike[str] | None = None) -> None:
        if os.name != "nt":
            raise SteamError("This build currently requires Windows 64-bit.")
        if ctypes.sizeof(ctypes.c_void_p) != 8:
            raise SteamError("SteamyLAN requires 64-bit Python to load steam_api64.dll.")
        if self.initialized:
            return

        found = find_steam_dll(dll_path)
        if not found:
            raise SteamError(
                "steam_api64.dll was not found. Place the official 64-bit DLL beside "
                "run.py/SteamyLAN.exe, in bin/, or set STEAMYLAN_STEAM_API64 to its full path."
            )
        self.dll_path = found





        app_id_text = str(self.app_id)
        os.environ["SteamAppId"] = app_id_text
        os.environ["SteamGameId"] = app_id_text
        runtime = runtime_directory()
        (runtime / "steam_appid.txt").write_text(app_id_text, encoding="ascii")
        os.chdir(runtime)

        root = application_dir()
        self._dll_directory_handles.clear()
        if hasattr(os, "add_dll_directory"):
            for folder in _unique_paths([found.parent, root, Path(sys.executable).resolve().parent]):
                try:


                    self._dll_directory_handles.append(os.add_dll_directory(str(folder)))
                except OSError:
                    pass

        try:
            self.dll = ctypes.WinDLL(str(found))
        except Exception as exc:
            raise SteamError(
                f"SteamyLAN found {found.name} but Windows could not load it. "
                f"Use the official 64-bit Steamworks redistributable. Details: {exc}"
            ) from exc






        if not steam_process_running():
            start_steam_client(show_window=True)
        process_deadline = time.monotonic() + 30.0
        while time.monotonic() < process_deadline and not steam_process_running():
            time.sleep(0.25)

        d = self.dll
        init_used = ""
        init_detail = ""
        init_result: int | None = None

        try:
            init_flat = d.SteamAPI_InitFlat
        except AttributeError:
            init_flat = None

        if init_flat is not None:
            init_flat.argtypes = [ctypes.c_void_p]
            init_flat.restype = ctypes.c_int
            init_used = "SteamAPI_InitFlat"




            init_deadline = time.monotonic() + 45.0
            while True:
                errbuf = ctypes.create_string_buffer(1024)
                result = int(init_flat(ctypes.cast(errbuf, ctypes.c_void_p)))
                detail = errbuf.value.decode("utf-8", "replace").strip()
                init_result = result
                init_detail = detail
                if result == 0:
                    self._api_started = True
                    break
                if result == 3 or time.monotonic() >= init_deadline:
                    break
                if result in (1, 2):
                    if not steam_process_running():
                        start_steam_client(show_window=True)
                    time.sleep(0.5)
                    continue
                break

            if not self._api_started:
                result_names = {
                    1: "Steam could not initialize for this app",
                    2: "Steam client was not available",
                    3: "Steamworks DLL/client version mismatch",
                }
                summary = result_names.get(init_result, f"Steam initialization returned {init_result}")
                detail_text = init_detail or summary
                raise SteamInitError(
                    f"{summary}. {detail_text}",
                    result_code=init_result,
                )
        else:




            try:
                init_fn = d.SteamAPI_Init
            except AttributeError as exc:
                raise SteamError(
                    "This steam_api64.dll has no supported Steam initialization export. "
                    "Use a current official 64-bit Steamworks redistributable."
                ) from exc
            init_fn.argtypes = []
            init_fn.restype = ctypes.c_bool
            init_used = getattr(init_fn, "__name__", "SteamAPI_Init")
            init_deadline = time.monotonic() + 45.0
            while True:
                if bool(init_fn()):
                    self._api_started = True
                    break
                if time.monotonic() >= init_deadline:
                    break
                if not steam_process_running():
                    start_steam_client(show_window=True)
                time.sleep(0.5)
            if not self._api_started:
                raise SteamInitError(
                    "Steam could not initialize. Make sure Steam is signed in under the same Windows user as SteamyLAN."
                )

        try:
            self._bind_common_exports()


            d.SteamAPI_ManualDispatch_Init()
            self.pipe = int(d.SteamAPI_GetHSteamPipe())
            if not self.pipe:
                raise SteamError("Steam initialized but did not provide a callback pipe.")

            self._resolve_interfaces()
            self._bind_interface_exports()
            self._apply_network_config()
        except Exception:
            if self._api_started:
                try:
                    shutdown = d.SteamAPI_Shutdown
                    shutdown.argtypes = []
                    shutdown.restype = None
                    shutdown()
                except Exception:
                    pass
            self._api_started = False
            self.user = self.friends = self.utils = self.matchmaking = self.networking = self.networking_utils = None
            self.pipe = 0
            raise

        self.initialized = True
        self._stop.clear()
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_loop,
            name="SteamDispatch",
            daemon=True,
        )
        self._dispatch_thread.start()

        self.log.info(
            "Steam initialized via %s as %s (%s), DLL=%s",
            init_used,
            self.persona_name(),
            self.steam_id(),
            found,
        )

    def _bind_common_exports(self) -> None:
        d = self.dll
        d.SteamAPI_Shutdown.argtypes = []
        d.SteamAPI_Shutdown.restype = None
        d.SteamAPI_GetHSteamPipe.argtypes = []
        d.SteamAPI_GetHSteamPipe.restype = ctypes.c_int32
        d.SteamAPI_ManualDispatch_Init.argtypes = []
        d.SteamAPI_ManualDispatch_Init.restype = None
        d.SteamAPI_ManualDispatch_RunFrame.argtypes = [ctypes.c_int32]
        d.SteamAPI_ManualDispatch_RunFrame.restype = None
        d.SteamAPI_ManualDispatch_GetNextCallback.argtypes = [
            ctypes.c_int32,
            ctypes.POINTER(CallbackMsg),
        ]
        d.SteamAPI_ManualDispatch_GetNextCallback.restype = ctypes.c_bool
        d.SteamAPI_ManualDispatch_FreeLastCallback.argtypes = [ctypes.c_int32]
        d.SteamAPI_ManualDispatch_FreeLastCallback.restype = None
        d.SteamAPI_ManualDispatch_GetAPICallResult.argtypes = [
            ctypes.c_int32,
            ctypes.c_uint64,
            ctypes.c_void_p,
            ctypes.c_int32,
            ctypes.c_int32,
            ctypes.POINTER(ctypes.c_bool),
        ]
        d.SteamAPI_ManualDispatch_GetAPICallResult.restype = ctypes.c_bool

    def _resolve_interfaces(self) -> None:
        d = self.dll
        user_fn, _ = self._find_export(d, ["SteamAPI_SteamUser_v023"])
        # The newest interface version depends on the Steamworks DLL supplied
        # with the build. Keep older compatible versions as fallbacks so one
        # missing version does not disable all Steam features.
        friends_fn, _ = self._find_export(
            d,
            [
                "SteamAPI_SteamFriends_v018",
                "SteamAPI_SteamFriends_v017",
                "SteamAPI_SteamFriends_v016",
                "SteamAPI_SteamFriends_v015",
            ],
        )
        utils_fn, _ = self._find_export(d, ["SteamAPI_SteamUtils_v011"])
        matchmaking_fn, _ = self._find_export(d, ["SteamAPI_SteamMatchmaking_v009"])
        networking_fn, _ = self._find_export(d, ["SteamAPI_SteamNetworking_v006"])
        networking_utils_fn, _ = self._find_export(
            d, ["SteamAPI_SteamNetworkingUtils_SteamAPI_v004"]
        )
        for fn in (
            user_fn,
            friends_fn,
            utils_fn,
            matchmaking_fn,
            networking_fn,
            networking_utils_fn,
        ):
            fn.argtypes = []
            fn.restype = ctypes.c_void_p

        self.user = user_fn()
        self.friends = friends_fn()
        self.utils = utils_fn()
        self.matchmaking = matchmaking_fn()
        self.networking = networking_fn()
        self.networking_utils = networking_utils_fn()
        if not all((
            self.user,
            self.friends,
            self.utils,
            self.matchmaking,
            self.networking,
            self.networking_utils,
        )):
            raise SteamError("One or more Steam interfaces returned NULL.")

    def _bind_interface_exports(self) -> None:
        d = self.dll

        d.SteamAPI_ISteamUser_GetSteamID.argtypes = [ctypes.c_void_p]
        d.SteamAPI_ISteamUser_GetSteamID.restype = ctypes.c_uint64
        d.SteamAPI_ISteamUser_BLoggedOn.argtypes = [ctypes.c_void_p]
        d.SteamAPI_ISteamUser_BLoggedOn.restype = ctypes.c_bool

        d.SteamAPI_ISteamFriends_GetPersonaName.argtypes = [ctypes.c_void_p]
        d.SteamAPI_ISteamFriends_GetPersonaName.restype = ctypes.c_char_p
        d.SteamAPI_ISteamFriends_GetFriendCount.argtypes = [ctypes.c_void_p, ctypes.c_int]
        d.SteamAPI_ISteamFriends_GetFriendCount.restype = ctypes.c_int
        d.SteamAPI_ISteamFriends_GetFriendByIndex.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        d.SteamAPI_ISteamFriends_GetFriendByIndex.restype = ctypes.c_uint64
        d.SteamAPI_ISteamFriends_GetFriendPersonaName.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        d.SteamAPI_ISteamFriends_GetFriendPersonaName.restype = ctypes.c_char_p
        d.SteamAPI_ISteamFriends_GetFriendPersonaState.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        d.SteamAPI_ISteamFriends_GetFriendPersonaState.restype = ctypes.c_int
        d.SteamAPI_ISteamFriends_HasFriend.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_int]
        d.SteamAPI_ISteamFriends_HasFriend.restype = ctypes.c_bool
        d.SteamAPI_ISteamFriends_GetFriendGamePlayed.argtypes = [
            ctypes.c_void_p, ctypes.c_uint64, ctypes.POINTER(SteamFriendGameInfo)
        ]
        d.SteamAPI_ISteamFriends_GetFriendGamePlayed.restype = ctypes.c_bool
        d.SteamAPI_ISteamFriends_RequestUserInformation.argtypes = [
            ctypes.c_void_p, ctypes.c_uint64, ctypes.c_bool
        ]
        d.SteamAPI_ISteamFriends_RequestUserInformation.restype = ctypes.c_bool
        d.SteamAPI_ISteamFriends_GetMediumFriendAvatar.argtypes = [
            ctypes.c_void_p, ctypes.c_uint64
        ]
        d.SteamAPI_ISteamFriends_GetMediumFriendAvatar.restype = ctypes.c_int

        d.SteamAPI_ISteamFriends_SetRichPresence.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p]
        d.SteamAPI_ISteamFriends_SetRichPresence.restype = ctypes.c_bool
        d.SteamAPI_ISteamFriends_ClearRichPresence.argtypes = [ctypes.c_void_p]
        d.SteamAPI_ISteamFriends_ClearRichPresence.restype = None

        d.SteamAPI_ISteamUtils_GetImageSize.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint32)
        ]
        d.SteamAPI_ISteamUtils_GetImageSize.restype = ctypes.c_bool
        d.SteamAPI_ISteamUtils_GetImageRGBA.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_int
        ]
        d.SteamAPI_ISteamUtils_GetImageRGBA.restype = ctypes.c_bool

        d.SteamAPI_ISteamMatchmaking_CreateLobby.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        d.SteamAPI_ISteamMatchmaking_CreateLobby.restype = ctypes.c_uint64
        d.SteamAPI_ISteamMatchmaking_RequestLobbyList.argtypes = [ctypes.c_void_p]
        d.SteamAPI_ISteamMatchmaking_RequestLobbyList.restype = ctypes.c_uint64
        d.SteamAPI_ISteamMatchmaking_AddRequestLobbyListStringFilter.argtypes = [
            ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int
        ]
        d.SteamAPI_ISteamMatchmaking_AddRequestLobbyListStringFilter.restype = None
        d.SteamAPI_ISteamMatchmaking_AddRequestLobbyListResultCountFilter.argtypes = [ctypes.c_void_p, ctypes.c_int]
        d.SteamAPI_ISteamMatchmaking_AddRequestLobbyListResultCountFilter.restype = None
        d.SteamAPI_ISteamMatchmaking_AddRequestLobbyListDistanceFilter.argtypes = [ctypes.c_void_p, ctypes.c_int]
        d.SteamAPI_ISteamMatchmaking_AddRequestLobbyListDistanceFilter.restype = None
        d.SteamAPI_ISteamMatchmaking_GetLobbyByIndex.argtypes = [ctypes.c_void_p, ctypes.c_int]
        d.SteamAPI_ISteamMatchmaking_GetLobbyByIndex.restype = ctypes.c_uint64
        d.SteamAPI_ISteamMatchmaking_JoinLobby.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        d.SteamAPI_ISteamMatchmaking_JoinLobby.restype = ctypes.c_uint64
        d.SteamAPI_ISteamMatchmaking_LeaveLobby.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        d.SteamAPI_ISteamMatchmaking_LeaveLobby.restype = None
        d.SteamAPI_ISteamMatchmaking_InviteUserToLobby.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_uint64]
        d.SteamAPI_ISteamMatchmaking_InviteUserToLobby.restype = ctypes.c_bool
        d.SteamAPI_ISteamMatchmaking_SetLobbyType.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_int]
        d.SteamAPI_ISteamMatchmaking_SetLobbyType.restype = ctypes.c_bool
        d.SteamAPI_ISteamMatchmaking_SetLobbyMemberLimit.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_int]
        d.SteamAPI_ISteamMatchmaking_SetLobbyMemberLimit.restype = ctypes.c_bool
        d.SteamAPI_ISteamMatchmaking_GetLobbyMemberLimit.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        d.SteamAPI_ISteamMatchmaking_GetLobbyMemberLimit.restype = ctypes.c_int
        d.SteamAPI_ISteamMatchmaking_SetLobbyData.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_char_p, ctypes.c_char_p]
        d.SteamAPI_ISteamMatchmaking_SetLobbyData.restype = ctypes.c_bool
        d.SteamAPI_ISteamMatchmaking_GetLobbyData.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_char_p]
        d.SteamAPI_ISteamMatchmaking_GetLobbyData.restype = ctypes.c_char_p
        d.SteamAPI_ISteamMatchmaking_GetLobbyOwner.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        d.SteamAPI_ISteamMatchmaking_GetLobbyOwner.restype = ctypes.c_uint64
        d.SteamAPI_ISteamMatchmaking_RequestLobbyData.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        d.SteamAPI_ISteamMatchmaking_RequestLobbyData.restype = ctypes.c_bool
        d.SteamAPI_ISteamMatchmaking_GetNumLobbyMembers.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        d.SteamAPI_ISteamMatchmaking_GetNumLobbyMembers.restype = ctypes.c_int
        d.SteamAPI_ISteamMatchmaking_GetLobbyMemberByIndex.argtypes = [ctypes.c_void_p, ctypes.c_uint64, ctypes.c_int]
        d.SteamAPI_ISteamMatchmaking_GetLobbyMemberByIndex.restype = ctypes.c_uint64

        d.SteamAPI_ISteamNetworking_SendP2PPacket.argtypes = [
            ctypes.c_void_p, ctypes.c_uint64, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_int, ctypes.c_int,
        ]
        d.SteamAPI_ISteamNetworking_SendP2PPacket.restype = ctypes.c_bool
        d.SteamAPI_ISteamNetworking_IsP2PPacketAvailable.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_int,
        ]
        d.SteamAPI_ISteamNetworking_IsP2PPacketAvailable.restype = ctypes.c_bool
        d.SteamAPI_ISteamNetworking_ReadP2PPacket.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
            ctypes.POINTER(ctypes.c_uint32), ctypes.POINTER(ctypes.c_uint64), ctypes.c_int,
        ]
        d.SteamAPI_ISteamNetworking_ReadP2PPacket.restype = ctypes.c_bool
        d.SteamAPI_ISteamNetworking_AcceptP2PSessionWithUser.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        d.SteamAPI_ISteamNetworking_AcceptP2PSessionWithUser.restype = ctypes.c_bool
        d.SteamAPI_ISteamNetworking_CloseP2PSessionWithUser.argtypes = [ctypes.c_void_p, ctypes.c_uint64]
        d.SteamAPI_ISteamNetworking_CloseP2PSessionWithUser.restype = ctypes.c_bool
        d.SteamAPI_ISteamNetworking_AllowP2PPacketRelay.argtypes = [ctypes.c_void_p, ctypes.c_bool]
        d.SteamAPI_ISteamNetworking_AllowP2PPacketRelay.restype = ctypes.c_bool
        d.SteamAPI_ISteamNetworkingUtils_SetConfigValue.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_ssize_t,
            ctypes.c_int,
            ctypes.c_void_p,
        ]
        d.SteamAPI_ISteamNetworkingUtils_SetConfigValue.restype = ctypes.c_bool
        try:
            d.SteamAPI_ISteamNetworkingUtils_InitRelayNetworkAccess.argtypes = [ctypes.c_void_p]
            d.SteamAPI_ISteamNetworkingUtils_InitRelayNetworkAccess.restype = None
        except AttributeError:
            pass
        d.SteamAPI_ISteamNetworkingUtils_GetPOPCount.argtypes = [ctypes.c_void_p]
        d.SteamAPI_ISteamNetworkingUtils_GetPOPCount.restype = ctypes.c_int
        d.SteamAPI_ISteamNetworkingUtils_GetPOPList.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_int]
        d.SteamAPI_ISteamNetworkingUtils_GetPOPList.restype = ctypes.c_int
        d.SteamAPI_ISteamNetworkingUtils_GetPingToDataCenter.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.POINTER(ctypes.c_uint32)]
        d.SteamAPI_ISteamNetworkingUtils_GetPingToDataCenter.restype = ctypes.c_int

    @staticmethod
    def _kbps_to_bytes_per_second(value: int) -> int:
        return max(0, int(value)) * 1000 // 8

    def configure_network(
        self,
        *,
        relay_mode: str,
        upload_limit_kbps: int,
        download_limit_kbps: int,
        relay_location: str = "automatic",
    ) -> None:
        mode = str(relay_mode or "automatic")
        if mode not in {"automatic", "prefer_direct", "force_direct", "prefer_relay", "force_relay"}:
            mode = "automatic"
        self._relay_mode = mode
        self._upload_limit_kbps = max(0, min(1_000_000, int(upload_limit_kbps)))
        self._download_limit_kbps = max(0, min(1_000_000, int(download_limit_kbps)))
        location = str(relay_location or "automatic").strip().casefold()
        self._relay_location = location if location == "automatic" or (3 <= len(location) <= 4 and location.isalnum()) else "automatic"
        self._upload_limiter.set_rate(self._kbps_to_bytes_per_second(self._upload_limit_kbps))
        self._download_limiter.set_rate(self._kbps_to_bytes_per_second(self._download_limit_kbps))
        if self.initialized and self.networking_utils:
            self._apply_network_config()

    def _set_global_int32(self, value_id: int, value: int | None) -> bool:
        if not self.networking_utils or not self.dll:
            return False
        ptr = None
        native = None
        if value is not None:
            native = ctypes.c_int32(int(value))
            ptr = ctypes.cast(ctypes.byref(native), ctypes.c_void_p)
        return bool(
            self.dll.SteamAPI_ISteamNetworkingUtils_SetConfigValue(
                self.networking_utils,
                int(value_id),
                _STEAM_NET_CONFIG_GLOBAL,
                0,
                _STEAM_NET_CONFIG_INT32,
                ptr,
            )
        )

    def _set_global_string(self, value_id: int, value: str | None) -> bool:
        if not self.networking_utils or not self.dll:
            return False
        if value is None:
            ptr = None
            buffer = None
        else:
            buffer = ctypes.create_string_buffer(str(value).encode("ascii", "ignore"))
            ptr = ctypes.cast(buffer, ctypes.c_void_p)
        return bool(
            self.dll.SteamAPI_ISteamNetworkingUtils_SetConfigValue(
                self.networking_utils,
                int(value_id),
                _STEAM_NET_CONFIG_GLOBAL,
                0,
                _STEAM_NET_CONFIG_STRING,
                ptr,
            )
        )

    @staticmethod
    def _pop_code(pop_id: int) -> str:
        value = int(pop_id) & 0xFFFFFFFF
        chars = [value >> 16, value >> 8, value, value >> 24]
        return "".join(chr(ch & 0xFF) for ch in chars if ch & 0xFF).casefold()

    def relay_locations(self) -> list[tuple[str, int]]:
        if not self.initialized or not self.networking_utils or not self.dll:
            return []
        try:
            self.dll.SteamAPI_ISteamNetworkingUtils_InitRelayNetworkAccess(self.networking_utils)
            count = int(self.dll.SteamAPI_ISteamNetworkingUtils_GetPOPCount(self.networking_utils))
            if count <= 0:
                return []
            count = min(count, 512)
            values = (ctypes.c_uint32 * count)()
            filled = int(self.dll.SteamAPI_ISteamNetworkingUtils_GetPOPList(self.networking_utils, values, count))
            rows: list[tuple[str, int]] = []
            for index in range(max(0, min(filled, count))):
                pop_id = int(values[index])
                code = self._pop_code(pop_id)
                if not code or not code.isalnum():
                    continue
                via = ctypes.c_uint32(0)
                ping = int(self.dll.SteamAPI_ISteamNetworkingUtils_GetPingToDataCenter(self.networking_utils, pop_id, ctypes.byref(via)))
                rows.append((code, ping))
            rows.sort(key=lambda item: (item[1] < 0, item[1] if item[1] >= 0 else 1_000_000, item[0]))
            return rows
        except Exception:
            self.log.exception("Could not enumerate Steam relay locations")
            return []

    @staticmethod
    def _rich_presence_text(value: str, max_bytes: int = 255) -> str:
        """Normalize and UTF-8 truncate a Steam rich-presence value."""
        normalized = " ".join(str(value or "").replace("\x00", " ").split())
        encoded = normalized.encode("utf-8")[: max(1, int(max_bytes))]
        while encoded:
            try:
                return encoded.decode("utf-8")
            except UnicodeDecodeError:
                encoded = encoded[:-1]
        return ""

    def set_rich_presence(self, status: str) -> bool:
        if not self.initialized or not self.friends or not self.dll:
            return False
        try:
            if not self.logged_on():
                self.log.debug("Steam is not signed in; rich presence will be retried")
                return False
        except Exception:
            self.log.debug("Steam sign-in state could not be checked", exc_info=True)
            return False

        text = self._rich_presence_text(status)
        if not text:
            self.clear_rich_presence()
            return True
        if text == self._rich_presence_value:
            return True
        try:
            accepted = bool(
                self.dll.SteamAPI_ISteamFriends_SetRichPresence(
                    self.friends,
                    b"status",
                    text.encode("utf-8"),
                )
            )
            if accepted:
                self._rich_presence_value = text
            else:
                self.log.warning("Steam rejected rich presence status for AppID %s", self.app_id)
            return accepted
        except Exception:
            self.log.exception("Could not set Steam rich presence")
            return False

    def clear_rich_presence(self) -> None:
        if not self.initialized or not self.friends or not self.dll:
            return
        try:
            self.dll.SteamAPI_ISteamFriends_ClearRichPresence(self.friends)
            self._rich_presence_value = None
        except Exception:
            self.log.exception("Could not clear Steam rich presence")

    def _apply_network_config(self) -> None:
        if not self.dll:
            return
        try:
            if self.networking:
                self.dll.SteamAPI_ISteamNetworking_AllowP2PPacketRelay(
                    self.networking,
                    self._relay_mode != "force_direct",
                )
            if not self.networking_utils:
                return
            if self._relay_mode == "force_relay":
                self._set_global_int32(_STEAM_NET_CONFIG_P2P_ICE_ENABLE, _STEAM_NET_ICE_DISABLE)
                self._set_global_int32(_STEAM_NET_CONFIG_P2P_ICE_PENALTY, None)
                self._set_global_int32(_STEAM_NET_CONFIG_P2P_SDR_PENALTY, None)
            elif self._relay_mode == "prefer_relay":
                self._set_global_int32(_STEAM_NET_CONFIG_P2P_ICE_ENABLE, _STEAM_NET_ICE_ALL)
                self._set_global_int32(_STEAM_NET_CONFIG_P2P_ICE_PENALTY, 1000)
                self._set_global_int32(_STEAM_NET_CONFIG_P2P_SDR_PENALTY, 0)
            elif self._relay_mode == "force_direct":
                self._set_global_int32(_STEAM_NET_CONFIG_P2P_ICE_ENABLE, _STEAM_NET_ICE_ALL)
                self._set_global_int32(_STEAM_NET_CONFIG_P2P_ICE_PENALTY, 0)
                self._set_global_int32(_STEAM_NET_CONFIG_P2P_SDR_PENALTY, 0x7FFFFFFF)
            elif self._relay_mode == "prefer_direct":
                self._set_global_int32(_STEAM_NET_CONFIG_P2P_ICE_ENABLE, _STEAM_NET_ICE_ALL)
                self._set_global_int32(_STEAM_NET_CONFIG_P2P_ICE_PENALTY, 0)
                self._set_global_int32(_STEAM_NET_CONFIG_P2P_SDR_PENALTY, 1000)
            else:
                self._set_global_int32(_STEAM_NET_CONFIG_P2P_ICE_ENABLE, None)
                self._set_global_int32(_STEAM_NET_CONFIG_P2P_ICE_PENALTY, None)
                self._set_global_int32(_STEAM_NET_CONFIG_P2P_SDR_PENALTY, None)

            # Prime Steam's relay/network configuration before the first P2P
            # message. Automatic/prefer modes can still select a direct ICE
            # route, but fetching relay configuration early avoids adding that
            # work to the first peer handshake.
            self.prime_networking()

            if self._relay_location == "automatic":
                self._set_global_string(_STEAM_NET_CONFIG_SDR_FORCE_RELAY_CLUSTER, None)
            else:
                self._set_global_string(_STEAM_NET_CONFIG_SDR_FORCE_RELAY_CLUSTER, self._relay_location)

            send_rate = self._kbps_to_bytes_per_second(self._upload_limit_kbps)
            if send_rate > 0:
                send_rate = max(1024, min(0x7FFFFFFF, send_rate))
                self._set_global_int32(_STEAM_NET_CONFIG_SEND_RATE_MIN, send_rate)
                self._set_global_int32(_STEAM_NET_CONFIG_SEND_RATE_MAX, send_rate)
            else:
                self._set_global_int32(_STEAM_NET_CONFIG_SEND_RATE_MIN, None)
                self._set_global_int32(_STEAM_NET_CONFIG_SEND_RATE_MAX, None)
        except Exception:
            self.log.exception("Could not apply Steam networking preferences")

    def prime_networking(self) -> None:
        """Ask Steam to prepare SDR/relay access once networking is usable.

        The initial config pass can occur before SteamUser reports logged on.
        Calling this again after sign-in is cheap and makes sure relay metadata
        is ready before the first cross-network peer handshake.
        """
        if self._relay_mode == "force_direct" or not self.networking_utils or not self.dll:
            return
        try:
            fn = getattr(self.dll, "SteamAPI_ISteamNetworkingUtils_InitRelayNetworkAccess", None)
            if fn is not None:
                fn(self.networking_utils)
        except Exception:
            self.log.debug("Could not prewarm Steam relay networking", exc_info=True)

    def shutdown(self) -> None:
        self._stop.set()
        thread = self._dispatch_thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=1.0)
        if self._api_started and self.dll:
            try:
                self.dll.SteamAPI_Shutdown()
            except Exception:
                self.log.exception("Steam shutdown failed")
        self._dispatch_thread = None
        self._api_started = False
        self.initialized = False
        self._rich_presence_value = None
        self.pipe = 0
        self.user = self.friends = self.utils = self.matchmaking = self.networking = self.networking_utils = None
        with self._networking_lock:
            self._peer_traffic.clear()
        with self._pending_lock:
            self._pending_calls.clear()
            self._completed_unclaimed.clear()
        self._friend_name_cache.clear()
        self._avatar_cache.clear()
        self._avatar_retry_after.clear()
        for handle in self._dll_directory_handles:
            try:
                handle.close()
            except Exception:
                pass
        self._dll_directory_handles.clear()

    @staticmethod
    def _decode(value) -> str:
        if not value:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", "replace")
        return ctypes.cast(value, ctypes.c_char_p).value.decode("utf-8", "replace")

    def steam_id(self) -> int:
        return int(self.dll.SteamAPI_ISteamUser_GetSteamID(self.user))

    def logged_on(self) -> bool:
        return bool(self.dll.SteamAPI_ISteamUser_BLoggedOn(self.user))

    def persona_name(self) -> str:
        return self._decode(self.dll.SteamAPI_ISteamFriends_GetPersonaName(self.friends))

    def friend_name(self, steam_id: int, refresh: bool = False) -> str:
        steam_id = int(steam_id)
        if not refresh and steam_id in self._friend_name_cache:
            return self._friend_name_cache[steam_id]
        name = self._decode(
            self.dll.SteamAPI_ISteamFriends_GetFriendPersonaName(
                self.friends, ctypes.c_uint64(steam_id)
            )
        ) or str(steam_id)
        self._friend_name_cache[steam_id] = name
        return name

    def is_immediate_friend(self, steam_id: int) -> bool:
        return bool(self.dll.SteamAPI_ISteamFriends_HasFriend(
            self.friends, ctypes.c_uint64(int(steam_id)), FRIEND_IMMEDIATE
        ))

    def friend_game_lobby(self, steam_id: int) -> tuple[int, int] | None:
        """Return (AppID, lobby SteamID) for a friend currently in a game."""
        info = SteamFriendGameInfo()
        ok = self.dll.SteamAPI_ISteamFriends_GetFriendGamePlayed(
            self.friends, ctypes.c_uint64(int(steam_id)), ctypes.byref(info)
        )
        if not ok:
            return None

        app_id = int(info.m_gameID) & 0xFFFFFF
        return app_id, int(info.m_steamIDLobby)

    def avatar_rgba(self, steam_id: int) -> tuple[bytes, int, int]:
        sid = int(steam_id)
        cached = self._avatar_cache.get(sid)
        if cached is not None:
            return cached
        now = time.monotonic()
        if now < self._avatar_retry_after.get(sid, 0.0):
            return b"", 0, 0
        try:



            self.dll.SteamAPI_ISteamFriends_RequestUserInformation(
                self.friends, ctypes.c_uint64(sid), False
            )
        except Exception:
            pass
        handle = int(
            self.dll.SteamAPI_ISteamFriends_GetMediumFriendAvatar(
                self.friends, ctypes.c_uint64(sid)
            )
        )
        if handle <= 0:
            self._avatar_retry_after[sid] = now + 10.0
            return b"", 0, 0
        width = ctypes.c_uint32(0)
        height = ctypes.c_uint32(0)
        if not self.dll.SteamAPI_ISteamUtils_GetImageSize(
            self.utils, handle, ctypes.byref(width), ctypes.byref(height)
        ):
            self._avatar_retry_after[sid] = now + 10.0
            return b"", 0, 0
        w, h = int(width.value), int(height.value)
        if not (0 < w <= 512 and 0 < h <= 512):
            self._avatar_retry_after[sid] = now + 10.0
            return b"", 0, 0
        size = w * h * 4
        buffer = (ctypes.c_uint8 * size)()
        if not self.dll.SteamAPI_ISteamUtils_GetImageRGBA(
            self.utils, handle, ctypes.cast(buffer, ctypes.c_void_p), size
        ):
            self._avatar_retry_after[sid] = now + 10.0
            return b"", 0, 0
        result = (bytes(buffer), w, h)
        self._avatar_retry_after.pop(sid, None)
        self._avatar_cache[sid] = result
        return result

    def friends_list(self) -> list[FriendInfo]:
        count = int(self.dll.SteamAPI_ISteamFriends_GetFriendCount(self.friends, FRIEND_IMMEDIATE))
        if count <= 0:
            return []
        states = {
            0: "Offline", 1: "Online", 2: "Busy", 3: "Away", 4: "Snooze",
            5: "Looking to trade", 6: "Looking to play",
        }
        result: list[FriendInfo] = []
        for i in range(count):
            sid = int(self.dll.SteamAPI_ISteamFriends_GetFriendByIndex(self.friends, i, FRIEND_IMMEDIATE))
            if not sid:
                continue
            state_num = int(
                self.dll.SteamAPI_ISteamFriends_GetFriendPersonaState(
                    self.friends, ctypes.c_uint64(sid)
                )
            )


            name = self.friend_name(sid, refresh=(sid not in self._friend_name_cache))
            if state_num == 0:



                avatar, avatar_width, avatar_height = self._avatar_cache.get(sid, (b"", 0, 0))
            else:
                avatar, avatar_width, avatar_height = self.avatar_rgba(sid)
            result.append(
                FriendInfo(
                    sid,
                    name,
                    states.get(state_num, "Unknown"),
                    state_num,
                    avatar,
                    avatar_width,
                    avatar_height,
                )
            )
        category_order = {"online": 0, "away": 1, "offline": 2}
        result.sort(key=lambda f: (category_order.get(f.category, 3), f.name.casefold(), f.steam_id))
        return result



    def create_lobby(self, max_members: int = 2, lobby_type: int = LOBBY_FRIENDS_ONLY) -> int:
        max_members = max(2, min(250, int(max_members)))
        handle = int(
            self.dll.SteamAPI_ISteamMatchmaking_CreateLobby(
                self.matchmaking,
                int(lobby_type),
                max_members,
            )
        )
        if not handle:
            raise SteamError("Steam did not return a CreateLobby call handle.")
        return handle

    def request_lobby_list(
        self,
        *,
        marker_key: str | None = None,
        marker_value: str | None = None,
        version: str | None = None,
        version_key: str | None = None,
        visibility: str | None = None,
        visibility_key: str | None = None,
        max_results: int = 50,
        worldwide: bool = True,
    ) -> int:

        if marker_key is not None and marker_value is not None:
            self.dll.SteamAPI_ISteamMatchmaking_AddRequestLobbyListStringFilter(
                self.matchmaking, marker_key.encode("utf-8"), marker_value.encode("utf-8"), 0
            )
        if version is not None and version_key is not None:
            self.dll.SteamAPI_ISteamMatchmaking_AddRequestLobbyListStringFilter(
                self.matchmaking, version_key.encode("utf-8"), version.encode("utf-8"), 0
            )
        if visibility is not None and visibility_key is not None:
            self.dll.SteamAPI_ISteamMatchmaking_AddRequestLobbyListStringFilter(
                self.matchmaking, visibility_key.encode("utf-8"), visibility.encode("utf-8"), 0
            )
        self.dll.SteamAPI_ISteamMatchmaking_AddRequestLobbyListResultCountFilter(
            self.matchmaking, max(1, min(250, int(max_results)))
        )
        if worldwide:
            self.dll.SteamAPI_ISteamMatchmaking_AddRequestLobbyListDistanceFilter(self.matchmaking, 3)
        handle = int(self.dll.SteamAPI_ISteamMatchmaking_RequestLobbyList(self.matchmaking))
        if not handle:
            raise SteamError("Steam did not return a RequestLobbyList call handle.")
        return handle

    def lobby_by_index(self, index: int) -> int:
        return int(self.dll.SteamAPI_ISteamMatchmaking_GetLobbyByIndex(self.matchmaking, int(index)))

    def join_lobby(self, lobby_id: int) -> int:
        handle = int(
            self.dll.SteamAPI_ISteamMatchmaking_JoinLobby(
                self.matchmaking,
                ctypes.c_uint64(int(lobby_id)),
            )
        )
        if not handle:
            raise SteamError("Steam did not return a JoinLobby call handle.")
        return handle

    def leave_lobby(self, lobby_id: int) -> None:
        if lobby_id:
            self.dll.SteamAPI_ISteamMatchmaking_LeaveLobby(
                self.matchmaking,
                ctypes.c_uint64(int(lobby_id)),
            )

    def invite_to_lobby(self, lobby_id: int, friend_id: int) -> bool:
        return bool(
            self.dll.SteamAPI_ISteamMatchmaking_InviteUserToLobby(
                self.matchmaking,
                ctypes.c_uint64(int(lobby_id)),
                ctypes.c_uint64(int(friend_id)),
            )
        )

    def set_lobby_type(self, lobby_id: int, lobby_type: int) -> bool:
        return bool(self.dll.SteamAPI_ISteamMatchmaking_SetLobbyType(
            self.matchmaking, ctypes.c_uint64(int(lobby_id)), int(lobby_type)
        ))

    def set_lobby_member_limit(self, lobby_id: int, max_members: int) -> bool:
        return bool(self.dll.SteamAPI_ISteamMatchmaking_SetLobbyMemberLimit(
            self.matchmaking, ctypes.c_uint64(int(lobby_id)), max(2, min(250, int(max_members)))
        ))

    def lobby_member_limit(self, lobby_id: int) -> int:
        return int(self.dll.SteamAPI_ISteamMatchmaking_GetLobbyMemberLimit(
            self.matchmaking, ctypes.c_uint64(int(lobby_id))
        ))

    def set_lobby_data(self, lobby_id: int, key: str, value: str) -> bool:
        return bool(
            self.dll.SteamAPI_ISteamMatchmaking_SetLobbyData(
                self.matchmaking,
                ctypes.c_uint64(int(lobby_id)),
                key.encode("utf-8"),
                value.encode("utf-8"),
            )
        )

    def get_lobby_data(self, lobby_id: int, key: str) -> str:
        raw = self.dll.SteamAPI_ISteamMatchmaking_GetLobbyData(
            self.matchmaking,
            ctypes.c_uint64(int(lobby_id)),
            key.encode("utf-8"),
        )
        return self._decode(raw)

    def lobby_owner(self, lobby_id: int) -> int:
        return int(
            self.dll.SteamAPI_ISteamMatchmaking_GetLobbyOwner(
                self.matchmaking,
                ctypes.c_uint64(int(lobby_id)),
            )
        )

    def request_lobby_data(self, lobby_id: int) -> bool:
        return bool(
            self.dll.SteamAPI_ISteamMatchmaking_RequestLobbyData(
                self.matchmaking, ctypes.c_uint64(int(lobby_id))
            )
        )

    def lobby_members(self, lobby_id: int) -> list[int]:
        count = int(self.dll.SteamAPI_ISteamMatchmaking_GetNumLobbyMembers(
            self.matchmaking, ctypes.c_uint64(int(lobby_id))
        ))
        result: list[int] = []
        for i in range(max(0, count)):
            sid = int(self.dll.SteamAPI_ISteamMatchmaking_GetLobbyMemberByIndex(
                self.matchmaking, ctypes.c_uint64(int(lobby_id)), int(i)
            ))
            if sid:
                result.append(sid)
        return result



    def _record_peer_traffic(self, steam_id: int, *, sent: int = 0, received: int = 0) -> None:
        sid = int(steam_id)
        now = time.monotonic()
        row = self._peer_traffic.setdefault(
            sid,
            {
                "sample_time": now,
                "sent": 0.0,
                "received": 0.0,
                "sample_sent": 0.0,
                "sample_received": 0.0,
                "upload_bps": 0.0,
                "download_bps": 0.0,
            },
        )
        row["sent"] += max(0, int(sent))
        row["received"] += max(0, int(received))

    def peer_network_stats(self, steam_id: int) -> tuple[int, float, float]:
        if not self.initialized or not self.networking:
            return -1, 0.0, 0.0
        with self._networking_lock:
            self._record_peer_traffic(steam_id)
            row = self._peer_traffic[int(steam_id)]
            now = time.monotonic()
            elapsed = now - row["sample_time"]
            if elapsed >= 0.25:
                row["upload_bps"] = max(0.0, (row["sent"] - row["sample_sent"]) / elapsed)
                row["download_bps"] = max(0.0, (row["received"] - row["sample_received"]) / elapsed)
                row["sample_sent"] = row["sent"]
                row["sample_received"] = row["received"]
                row["sample_time"] = now
            return -1, row["upload_bps"], row["download_bps"]

    def accept_peer(self, steam_id: int) -> bool:
        with self._networking_lock:
            return bool(
                self.dll.SteamAPI_ISteamNetworking_AcceptP2PSessionWithUser(
                    self.networking,
                    ctypes.c_uint64(int(steam_id)),
                )
            )

    def close_peer(self, steam_id: int) -> None:
        try:
            with self._networking_lock:
                self.dll.SteamAPI_ISteamNetworking_CloseP2PSessionWithUser(
                    self.networking,
                    ctypes.c_uint64(int(steam_id)),
                )
                self._peer_traffic.pop(int(steam_id), None)
        except Exception:
            self.log.exception("Failed to close Steam P2P session")

    def send_packet(self, steam_id: int, packet: bytes, channel: int, reliable: bool = True) -> bool:
        if len(packet) > MAX_STEAM_PACKET:
            raise ValueError(f"Steam P2P packet exceeds SteamyLAN's {MAX_STEAM_PACKET}-byte limit.")
        if not reliable and len(packet) > 1200:
            raise ValueError("Unreliable Steam P2P packets are limited to 1200 bytes.")
        self._upload_limiter.wait(len(packet))
        buf = ctypes.create_string_buffer(packet)
        mode = P2P_RELIABLE if reliable else P2P_UNRELIABLE
        with self._networking_lock:
            accepted = bool(
                self.dll.SteamAPI_ISteamNetworking_SendP2PPacket(
                    self.networking,
                    ctypes.c_uint64(int(steam_id)),
                    ctypes.cast(buf, ctypes.c_void_p),
                    len(packet),
                    mode,
                    int(channel),
                )
            )
            if accepted:
                self._record_peer_traffic(steam_id, sent=len(packet))
            return accepted

    def recv_packets(self, channel: int, max_messages: int = 32) -> list[tuple[int, bytes]]:
        limit = max(1, min(64, int(max_messages)))
        results: list[tuple[int, bytes]] = []
        total_bytes = 0
        for _ in range(limit):
            with self._networking_lock:
                size = ctypes.c_uint32(0)
                if not self.dll.SteamAPI_ISteamNetworking_IsP2PPacketAvailable(
                    self.networking, ctypes.byref(size), int(channel)
                ):
                    break
                capacity = int(size.value)
                if not (0 < capacity <= MAX_STEAM_PACKET):
                    discard = ctypes.create_string_buffer(1)
                    read = ctypes.c_uint32(0)
                    sender = ctypes.c_uint64(0)
                    self.dll.SteamAPI_ISteamNetworking_ReadP2PPacket(
                        self.networking, ctypes.cast(discard, ctypes.c_void_p), 1,
                        ctypes.byref(read), ctypes.byref(sender), int(channel),
                    )
                    continue
                buf = ctypes.create_string_buffer(capacity)
                read = ctypes.c_uint32(0)
                sender = ctypes.c_uint64(0)
                ok = bool(self.dll.SteamAPI_ISteamNetworking_ReadP2PPacket(
                    self.networking, ctypes.cast(buf, ctypes.c_void_p), capacity,
                    ctypes.byref(read), ctypes.byref(sender), int(channel),
                ))
                if not ok:
                    continue
                payload = bytes(buf.raw[: int(read.value)])
                sid = int(sender.value)
                if not sid or not payload:
                    continue
                results.append((sid, payload))
                total_bytes += len(payload)
                self._record_peer_traffic(sid, received=len(payload))
        if total_bytes:
            self._download_limiter.wait(total_bytes)
        return results

    def recv_packet(self, channel: int):
        packets = self.recv_packets(channel, 1)
        return packets[0] if packets else None



    def await_call(self, handle: int, callback_id: int, struct_cls, done: Callable) -> None:
        completed = None
        with self._pending_lock:
            completed = self._completed_unclaimed.pop(int(handle), None)
            if completed is None:
                self._pending_calls[int(handle)] = (int(callback_id), struct_cls, done)
        if completed is not None:
            self._resolve_api_call(completed, override=(int(callback_id), struct_cls, done))

    def _dispatch_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.dll.SteamAPI_ManualDispatch_RunFrame(self.pipe)
                msg = CallbackMsg()
                processed = 0
                while (
                    processed < 128
                    and self.dll.SteamAPI_ManualDispatch_GetNextCallback(
                        self.pipe,
                        ctypes.byref(msg),
                    )
                ):
                    try:
                        self._handle_callback(msg)
                    finally:
                        self.dll.SteamAPI_ManualDispatch_FreeLastCallback(self.pipe)
                    processed += 1
            except Exception:
                self.log.exception("Steam callback dispatch failed")
            time.sleep(0.03)

    @staticmethod
    def _copy_callback(msg: CallbackMsg, struct_cls):
        if not msg.m_pubParam or msg.m_cubParam < ctypes.sizeof(struct_cls):
            return None
        return struct_cls.from_buffer_copy(
            ctypes.string_at(msg.m_pubParam, ctypes.sizeof(struct_cls))
        )

    def _handle_callback(self, msg: CallbackMsg) -> None:
        cid = int(msg.m_iCallback)
        if cid == CALLBACK_STEAM_API_CALL_COMPLETED:
            data = self._copy_callback(msg, SteamAPICallCompleted)
            if data:
                self._resolve_api_call(data)
            return
        if cid == CALLBACK_LOBBY_INVITE:
            data = self._copy_callback(msg, LobbyInvite)
            if data:
                friend_id = int(data.m_ulSteamIDUser)
                self._emit(
                    "lobby_invite",
                    friend_id=friend_id,
                    friend_name=self.friend_name(friend_id),
                    lobby_id=int(data.m_ulSteamIDLobby),
                )
            return
        if cid == CALLBACK_GAME_LOBBY_JOIN_REQUESTED:
            data = self._copy_callback(msg, GameLobbyJoinRequested)
            if data:
                friend_id = int(data.m_steamIDFriend)
                self._emit(
                    "join_requested",
                    friend_id=friend_id,
                    friend_name=self.friend_name(friend_id) if friend_id else "Steam friend",
                    lobby_id=int(data.m_steamIDLobby),
                )
            return
        if cid == CALLBACK_LOBBY_ENTER:
            data = self._copy_callback(msg, LobbyEnter)
            if data:
                self._emit(
                    "lobby_enter",
                    lobby_id=int(data.m_ulSteamIDLobby),
                    response=int(data.m_EChatRoomEnterResponse),
                )
            return
        if cid == CALLBACK_P2P_SESSION_REQUEST:
            data = self._copy_callback(msg, P2PSessionRequest)
            if data:
                steam_id = int(data.m_steamIDRemote)
                if steam_id:
                    self._emit("networking_session_request", steam_id=steam_id)
            return
        if cid == CALLBACK_P2P_SESSION_CONNECT_FAIL:
            data = self._copy_callback(msg, P2PSessionConnectFail)
            if data:
                steam_id = int(data.m_steamIDRemote)
                reason = int(data.m_eP2PSessionError)
                if steam_id:
                    debug = f"Legacy Steam P2P error {reason}"
                    self.log.warning("Steam P2P session with %s failed (reason %s)", steam_id, reason)
                    self._emit(
                        "networking_session_failed",
                        steam_id=steam_id,
                        error=reason,
                        detail=debug,
                    )
            return
        if cid == CALLBACK_PERSONA_STATE_CHANGE:
            data = self._copy_callback(msg, PersonaStateChange)
            if data:
                sid = int(data.m_ulSteamID)
                if sid:



                    self._friend_name_cache.pop(sid, None)
                    self._avatar_cache.pop(sid, None)
                    self._avatar_retry_after.pop(sid, None)
            self._emit("friends_changed")

    def _resolve_api_call(self, completed: SteamAPICallCompleted, override=None) -> None:
        handle = int(completed.m_hAsyncCall)
        if override is None:
            with self._pending_lock:
                pending = self._pending_calls.pop(handle, None)
                if pending is None:




                    # The dispatch thread can receive completion before the
                    # caller has registered await_call(). Preserve every async
                    # result type SteamyLAN currently awaits so a fast Steam
                    # response cannot be lost to this registration race.
                    if int(completed.m_iCallback) in {
                        CALLBACK_LOBBY_CREATED,
                        CALLBACK_LOBBY_ENTER,
                        CALLBACK_LOBBY_MATCH_LIST,
                    }:
                        self._completed_unclaimed[handle] = SteamAPICallCompleted(
                            completed.m_hAsyncCall,
                            completed.m_iCallback,
                            completed.m_cubParam,
                        )
                    return
        else:
            pending = override

        callback_id, struct_cls, done = pending
        obj = struct_cls()
        failed = ctypes.c_bool(False)
        ok = self.dll.SteamAPI_ManualDispatch_GetAPICallResult(
            self.pipe,
            ctypes.c_uint64(handle),
            ctypes.byref(obj),
            ctypes.sizeof(obj),
            int(callback_id),
            ctypes.byref(failed),
        )
        try:
            done(obj if ok and not failed.value else None)
        except Exception:
            self.log.exception("Steam API call result handler failed")
