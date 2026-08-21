from __future__ import annotations

import os
import ntpath
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import psutil

from .models import ProgramInfo

try:
    import win32api
    import win32gui
    import win32process
except Exception:
    win32api = None
    win32gui = None
    win32process = None

try:
    import winreg
except Exception:
    winreg = None

try:
    import vdf
except Exception:
    vdf = None


GENERIC_PRODUCT_NAMES = {
    "java", "java(tm) platform se binary", "openjdk platform binary",
    "python", "python.exe", "python launcher", "application", "program",
    "launcher", "game", "untitled",
}

MINECRAFT_CLIENT_MARKERS = (
    "net.minecraft.client.main.main",
    "net.fabricmc.loader.impl.launch.knotclient",
    "net.fabricmc.loader.impl.launch.knot",
    "cpw.mods.modlauncher.launcher",
    "net.neoforged.fml.loading.targets",
    "minecraft\\versions",
    "minecraft/versions",
)
MINECRAFT_SERVER_MARKERS = (
    "net.minecraft.server.main",
    "minecraft_server",
    "server.jar",
)
MINECRAFT_LAUNCHERS = {
    "prismlauncher.exe", "multimc.exe", "minecraftlauncher.exe",
    "minecraft.exe", "curseforge.exe", "modrinth app.exe", "modrinth.exe",
    "atlauncher.exe", "gdlauncher.exe", "ftb-app.exe",
}

SYSTEM_PATH_PARTS = (
    "\\windows\\system32\\",
    "\\windows\\syswow64\\",
    "\\windows\\winsxs\\",
)


def _clean_label(value: str) -> str:
    value = re.sub(r"\s+", " ", (value or "").strip())
    value = re.sub(r"\s+\(TM\)\s*", " ", value, flags=re.I).strip()
    return value


def minecraft_identity(cmdline: Iterable[str], parent_names: Iterable[str] = ()) -> tuple[str, str] | None:
    text = " ".join(cmdline).casefold()
    parents = {ntpath.basename(str(x)).casefold() for x in parent_names}

    is_server = any(marker in text for marker in MINECRAFT_SERVER_MARKERS)
    if is_server or ("minecraft" in text and " nogui" in f" {text}"):
        return "Minecraft Server", "Minecraft Java server"

    is_client = any(marker in text for marker in MINECRAFT_CLIENT_MARKERS)
    launched_by_mc = bool(parents & {x.casefold() for x in MINECRAFT_LAUNCHERS})
    if is_client or ("minecraft" in text and launched_by_mc):
        version = ""
        parts = list(cmdline)
        for flag in ("--version", "--gameVersion"):
            if flag in parts:
                try:
                    version = parts[parts.index(flag) + 1]
                except (ValueError, IndexError):
                    pass
        subtitle = "Minecraft Java Edition"
        if version and len(version) <= 32:
            subtitle += f" • {version}"
        return "Minecraft: Java Edition", subtitle
    return None


def friendly_program_name(
    *,
    exe: str,
    process_name: str,
    cmdline: Iterable[str] = (),
    parent_names: Iterable[str] = (),
    steam_name: str = "",
    product_name: str = "",
    file_description: str = "",
    window_title: str = "",
) -> tuple[str, str]:
    """Resolve a user-facing application name without exposing launcher internals."""
    raw_name = ntpath.basename(exe) if exe else ntpath.basename(process_name)
    base = os.path.splitext(raw_name)[0]
    base_fold = base.casefold()

    if base_fold in {"java", "javaw"}:
        mc = minecraft_identity(cmdline, parent_names)
        if mc:
            return mc

    steam_name = _clean_label(steam_name)
    if steam_name:
        return steam_name, "Steam game"

    for candidate, source in (
        (product_name, "Application"),
        (file_description, "Application"),
    ):
        cleaned = _clean_label(candidate)
        if cleaned and cleaned.casefold() not in GENERIC_PRODUCT_NAMES:
            if cleaned.casefold() not in {base_fold, process_name.casefold()}:
                return cleaned, source

    title = _clean_label(window_title)
    if title:

        title = re.sub(r"\s+[-–—]\s+(paused|not responding)$", "", title, flags=re.I)
        if 2 <= len(title) <= 100 and title.casefold() not in GENERIC_PRODUCT_NAMES:
            return title, "Running window"

    display = re.sub(r"[_\-.]+", " ", base).strip()
    display = " ".join(word if word.isupper() else word.capitalize() for word in display.split())
    return display or process_name or "Application", "Running application"


@dataclass(frozen=True, slots=True)
class SteamGame:
    appid: int
    name: str
    install_dir: Path
    icon_path: str = ""


class SteamLibraryIndex:
    def __init__(self):
        self.steam_root: Path | None = self._find_steam_root()
        self.games: list[SteamGame] = []
        self._last_scan = 0.0
        self._match_cache: dict[str, SteamGame | None] = {}

    @staticmethod
    def _find_steam_root() -> Path | None:
        candidates: list[Path] = []
        if winreg is not None:
            for key_path in (r"Software\Valve\Steam", r"Software\WOW6432Node\Valve\Steam"):
                try:
                    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                        value, _ = winreg.QueryValueEx(key, "SteamPath")
                        candidates.append(Path(value))
                except OSError:
                    pass
        for env_path in (
            os.environ.get("PROGRAMFILES(X86)"),
            os.environ.get("PROGRAMFILES"),
        ):
            if env_path:
                candidates.append(Path(env_path) / "Steam")
        for p in candidates:
            if (p / "steam.exe").exists() or (p / "steamapps").exists():
                return p
        return None

    def refresh(self, force: bool = False) -> None:
        if not self.steam_root or vdf is None:
            return
        if not force and time.monotonic() - self._last_scan < 120:
            return
        self._last_scan = time.monotonic()

        libraries = [self.steam_root]
        folders_file = self.steam_root / "steamapps" / "libraryfolders.vdf"
        try:
            with folders_file.open("r", encoding="utf-8", errors="replace") as handle:
                data = vdf.load(handle)
            folders = data.get("libraryfolders", data)
            for value in folders.values():
                if isinstance(value, dict) and value.get("path"):
                    libraries.append(Path(value["path"]))
                elif isinstance(value, str):
                    libraries.append(Path(value))
        except Exception:
            pass

        games: list[SteamGame] = []
        seen: set[int] = set()
        for library in libraries:
            steamapps = library / "steamapps"
            if not steamapps.exists():
                continue
            for manifest in steamapps.glob("appmanifest_*.acf"):
                try:
                    with manifest.open("r", encoding="utf-8", errors="replace") as handle:
                        raw = vdf.load(handle).get("AppState", {})
                    appid = int(raw.get("appid", 0))
                    name = str(raw.get("name", "")).strip()
                    installdir = str(raw.get("installdir", "")).strip()
                    if not appid or not name or not installdir or appid in seen:
                        continue
                    install_dir = (steamapps / "common" / installdir).resolve()
                    icon = self._find_cached_icon(appid)
                    games.append(SteamGame(appid, name, install_dir, icon))
                    seen.add(appid)
                except Exception:
                    continue
        games.sort(key=lambda g: len(str(g.install_dir)), reverse=True)
        self.games = games
        self._match_cache.clear()

    def _find_cached_icon(self, appid: int) -> str:
        if not self.steam_root:
            return ""
        cache = self.steam_root / "appcache" / "librarycache"
        candidates = [
            cache / f"{appid}_icon.jpg",
            cache / f"{appid}_icon.png",
            cache / str(appid) / "icon.jpg",
            cache / str(appid) / "icon.png",
        ]
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
        return ""

    def match(self, exe: str, *, refresh: bool = True) -> SteamGame | None:
        if refresh:
            self.refresh()
        try:
            path = Path(exe).resolve()
        except Exception:
            return None
        path_text = os.path.normcase(str(path))
        if path_text in self._match_cache:
            return self._match_cache[path_text]
        folded = path_text.casefold()
        match = None
        for game in self.games:
            root = os.path.normcase(str(game.install_dir)).casefold()
            if folded == root or folded.startswith(root + os.sep.casefold()):
                match = game
                break
        self._match_cache[path_text] = match
        return match


class WindowsMetadata:
    def __init__(self):
        self._cache: dict[tuple[str, int], tuple[str, str]] = {}

    def get(self, exe: str) -> tuple[str, str]:
        if win32api is None or not exe:
            return "", ""
        try:
            stat = os.stat(exe)
            key = (os.path.normcase(exe), int(stat.st_mtime_ns))
        except OSError:
            return "", ""
        if key in self._cache:
            return self._cache[key]
        product = description = ""
        try:
            translations = win32api.GetFileVersionInfo(exe, r"\VarFileInfo\Translation")
            if translations:
                lang, codepage = translations[0]
                prefix = "\\StringFileInfo\\{:04x}{:04x}\\".format(lang, codepage)
                for field, target in (("ProductName", "product"), ("FileDescription", "description")):
                    try:
                        value = str(win32api.GetFileVersionInfo(exe, prefix + field) or "")
                    except Exception:
                        value = ""
                    if target == "product":
                        product = value
                    else:
                        description = value
        except Exception:
            pass
        self._cache = {k: v for k, v in self._cache.items() if k[0] != key[0]}
        self._cache[key] = (product, description)
        return product, description


class WindowIndex:
    @staticmethod
    def titles_by_pid() -> dict[int, str]:
        if win32gui is None or win32process is None:
            return {}
        result: dict[int, str] = {}

        def visit(hwnd, _):
            try:
                if not win32gui.IsWindowVisible(hwnd):
                    return True
                title = win32gui.GetWindowText(hwnd).strip()
                if not title:
                    return True
                _thread, pid = win32process.GetWindowThreadProcessId(hwnd)
                if pid and pid not in result:
                    result[int(pid)] = title
            except Exception:
                pass
            return True

        try:
            win32gui.EnumWindows(visit, None)
        except Exception:
            pass
        return result


class ProgramCatalog:
    """Discovers running applications and resolves them to human-friendly games/apps.

    Process metadata is cached by PID/create-time/window-title. SteamyLAN scans often
    while the Create tab is enabled, and executable metadata, parent chains and Steam
    library matching are effectively static for the lifetime of a process. Reusing
    that data keeps background discovery inexpensive without changing what is shown.
    """

    def __init__(self):
        self.steam = SteamLibraryIndex()
        self.metadata = WindowsMetadata()
        self._process_cache: dict[tuple[int, float, str], ProgramInfo] = {}
        self._minecraft_launchers_folded = frozenset(x.casefold() for x in MINECRAFT_LAUNCHERS)

    @staticmethod
    def _parents(process: psutil.Process, limit: int = 5) -> tuple[str, ...]:
        names: list[str] = []
        current = process
        for _ in range(limit):
            try:
                current = current.parent()
                if current is None:
                    break
                names.append(current.name())
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                break
        return tuple(names)

    @staticmethod
    def _is_system_exe(exe: str) -> bool:
        folded = exe.casefold().replace("/", "\\")
        return any(part in folded for part in SYSTEM_PATH_PARTS)

    def scan(self, pids: Iterable[int] | None = None) -> list[ProgramInfo]:


        self.steam.refresh()
        windows = WindowIndex.titles_by_pid()
        now = time.time()
        result: list[ProgramInfo] = []
        live_cache: dict[tuple[int, float, str], ProgramInfo] = {}

        target_pids = None if pids is None else {int(pid) for pid in pids if int(pid) > 0}
        if target_pids is None:
            process_items = psutil.process_iter(["pid", "name", "exe", "cmdline", "create_time"])
        else:


            process_items = sorted(target_pids)

        for item in process_items:
            try:
                proc = item if target_pids is None else psutil.Process(int(item))
                if target_pids is None:
                    info = proc.info
                    pid = int(info["pid"])
                    started = float(info.get("create_time") or 0.0)
                    window_title = windows.get(pid, "")
                    cache_key = (pid, started, window_title)
                    cached = self._process_cache.get(cache_key)
                    if cached is not None:
                        result.append(cached)
                        live_cache[cache_key] = cached
                        continue
                    exe = str(info.get("exe") or "")
                    process_name = str(info.get("name") or (Path(exe).name if exe else "Application"))
                    cmdline = tuple(str(x) for x in (info.get("cmdline") or ()))
                else:
                    pid = int(proc.pid)
                    started = float(proc.create_time() or 0.0)
                    window_title = windows.get(pid, "")
                    cache_key = (pid, started, window_title)
                    cached = self._process_cache.get(cache_key)
                    if cached is not None:
                        result.append(cached)
                        live_cache[cache_key] = cached
                        continue
                    with proc.oneshot():
                        exe = str(proc.exe() or "")
                        process_name = str(proc.name() or (Path(exe).name if exe else "Application"))
                        cmdline = tuple(str(x) for x in (proc.cmdline() or ()))

                if not exe or not exe.lower().endswith(".exe"):
                    continue

                parent_names = self._parents(proc)
                steam_game = self.steam.match(exe, refresh=False)
                if self._is_system_exe(exe) and not window_title and steam_game is None:
                    continue

                product, description = self.metadata.get(exe)
                display, source = friendly_program_name(
                    exe=exe,
                    process_name=process_name,
                    cmdline=cmdline,
                    parent_names=parent_names,
                    steam_name=steam_game.name if steam_game else "",
                    product_name=product,
                    file_description=description,
                    window_title=window_title,
                )

                exe_stem = os.path.splitext(ntpath.basename(exe))[0].casefold()
                minecraft = minecraft_identity(cmdline, parent_names) if exe_stem in {"java", "javaw"} else None
                is_recent = bool(started and now - started <= 24 * 3600)
                recommended = bool(
                    steam_game
                    or minecraft
                    or window_title
                    or (is_recent and not self._is_system_exe(exe))
                )
                subtitle_parts = [source]
                if window_title and window_title.casefold() != display.casefold() and len(window_title) <= 90:
                    subtitle_parts.append(window_title)
                subtitle = " • ".join(dict.fromkeys(x for x in subtitle_parts if x))

                icon_path = steam_game.icon_path if steam_game else ""
                if minecraft and not icon_path:
                    try:
                        parent = proc.parent()
                        for _ in range(5):
                            if parent is None:
                                break
                            if parent.name().casefold() in self._minecraft_launchers_folded:
                                parent_exe = parent.exe()
                                if parent_exe:
                                    icon_path = parent_exe
                                    break
                            parent = parent.parent()
                    except (psutil.NoSuchProcess, psutil.AccessDenied):
                        pass

                if steam_game is not None:
                    group_key = f"steam:{steam_game.appid}"
                else:
                    group_key = f"{display.casefold()}|{os.path.normcase(exe)}"

                program = ProgramInfo(
                    pid=pid,
                    exe=exe,
                    process_name=process_name,
                    display_name=display,
                    subtitle=subtitle,
                    started_at=started,
                    window_title=window_title,
                    steam_appid=steam_game.appid if steam_game else None,
                    icon_path=icon_path,
                    cmdline=cmdline,
                    parent_names=parent_names,
                    recommended=recommended,
                    group_key=group_key,
                )
                result.append(program)
                live_cache[cache_key] = program
            except (psutil.NoSuchProcess, psutil.AccessDenied, OSError):
                continue
            except Exception:
                continue



        self._process_cache = live_cache
        result.sort(key=lambda p: (0 if p.recommended else 1, -p.started_at, p.display_name.casefold()))
        return result


def rank_recent_programs(programs: Iterable[ProgramInfo], recent_keys: Iterable[str], limit: int = 6) -> list[ProgramInfo]:
    recency = {key: index for index, key in enumerate(recent_keys)}
    unique: dict[str, ProgramInfo] = {}
    for program in programs:
        current = unique.get(program.key)
        if current is None or program.started_at > current.started_at:
            unique[program.key] = program

    values = list(unique.values())
    values.sort(
        key=lambda p: (
            0 if p.key in recency else 1,
            recency.get(p.key, 9999),
            0 if p.recommended else 1,
            -p.started_at,
            p.display_name.casefold(),
        )
    )
    return values[:limit]
