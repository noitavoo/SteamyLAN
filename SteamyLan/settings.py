from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import secrets
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from platformdirs import user_config_dir

from .constants import APP_NAME, DEFAULT_APP_ID


@dataclass
class Preferences:
    auto_allow_friends: bool = True
    auto_accept_ports: bool = True
    keep_in_tray: bool = True
    notifications: bool = True
    start_with_computer: bool = False
    check_updates_on_start: bool = True
    update_mode: str = "automatic"
    custom_app_id: str = ""
    relay_mode: str = "automatic"
    relay_location: str = "automatic"
    bind_address: str = "127.0.0.1"
    show_steam_status: bool = True
    upload_limit_kbps: int = 0
    download_limit_kbps: int = 0
    last_page: str = "join"
    window_geometry: str = ""
    last_service_key: str = ""


class PreferenceStore:
    def __init__(self):
        self.dir = Path(user_config_dir(APP_NAME, appauthor=False))
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "settings.json"
        self.prefs = self.load()

    def load(self) -> Preferences:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return Preferences()
        if not isinstance(raw, dict):
            return Preferences()

        defaults = Preferences()
        values: dict[str, object] = {}
        for key in Preferences.__dataclass_fields__:
            if key not in raw:
                continue
            value = raw[key]
            default = getattr(defaults, key)
            if isinstance(default, bool):
                if type(value) is bool:
                    values[key] = value
            elif isinstance(default, int):
                if type(value) is int:
                    values[key] = value
            elif isinstance(default, str):
                if isinstance(value, str):
                    values[key] = value

        prefs = Preferences(**values)
        if prefs.last_page not in {"join", "create", "server"}:
            prefs.last_page = "join"
        if prefs.update_mode not in {"automatic", "notify", "disabled"}:
            prefs.update_mode = "automatic"
        if prefs.relay_mode not in {"automatic", "prefer_direct", "force_direct", "prefer_relay", "force_relay"}:
            prefs.relay_mode = "automatic"
        relay_location = str(prefs.relay_location or "automatic").strip().casefold()
        prefs.relay_location = relay_location if relay_location == "automatic" or (3 <= len(relay_location) <= 4 and relay_location.isalnum()) else "automatic"
        prefs.bind_address = self.normalize_bind_address(prefs.bind_address) or "127.0.0.1"
        prefs.upload_limit_kbps = max(0, min(1_000_000, int(prefs.upload_limit_kbps)))
        prefs.download_limit_kbps = max(0, min(1_000_000, int(prefs.download_limit_kbps)))
        prefs.custom_app_id = self._normalize_app_id_text(prefs.custom_app_id)
        return prefs

    @staticmethod
    def normalize_bind_address(value: object) -> str | None:
        """Return a canonical numeric IP address that can be used for socket.bind()."""
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return ipaddress.ip_address(text).compressed
        except ValueError:
            return None

    @staticmethod
    def _normalize_app_id_text(value: object) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            app_id = int(text, 10)
        except (TypeError, ValueError, OverflowError):
            return ""
        return str(app_id) if 0 < app_id <= 0xFFFFFFFF else ""

    def effective_app_id(self) -> int:
        text = self._normalize_app_id_text(self.prefs.custom_app_id)
        return int(text) if text else DEFAULT_APP_ID

    def save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self.prefs), indent=2), encoding="utf-8")
        tmp.replace(self.path)


    def static_share_secret(self, server_key: str, *, create: bool = False) -> bytes | None:
        key = hashlib.sha256(str(server_key or "server").encode("utf-8", "replace")).hexdigest()[:32]
        path = self.dir / "static_share_codes.json"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            raw = {}
        if not isinstance(raw, dict):
            raw = {}
        encoded = str(raw.get(key, "") or "")
        if encoded:
            try:
                secret = base64.b64decode(encoded.encode("ascii"), validate=True)
                if len(secret) == 10:
                    return secret
            except Exception:
                pass
        if not create:
            return None
        secret = secrets.token_bytes(10)
        raw[key] = base64.b64encode(secret).decode("ascii")
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
        return secret

    @staticmethod
    def server_key(service) -> str:
        exe = os.path.normcase(str(getattr(service, "exe_path", "") or "").strip())
        process = str(getattr(service, "process_name", "") or "").strip().casefold()
        name = str(getattr(service, "name", "") or "").strip().casefold()
        if exe:
            return f"exe:{exe}"
        return f"manual:{process}:{name}"

    def set_startup(self, enabled: bool) -> tuple[bool, str]:
        if os.name != "nt":
            return False, "Start with computer is currently supported on Windows only."
        try:
            import winreg
            key_path = r"Software\Microsoft\Windows\CurrentVersion\Run"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path, 0, winreg.KEY_SET_VALUE) as key:
                if enabled:
                    if getattr(sys, "frozen", False):
                        cmd = f'"{Path(sys.executable).resolve()}"'
                    else:
                        run_py = Path(__file__).resolve().parents[1] / "run.py"
                        cmd = f'"{Path(sys.executable).resolve()}" "{run_py}"'
                    winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
                else:
                    try:
                        winreg.DeleteValue(key, APP_NAME)
                    except FileNotFoundError:
                        pass
            return True, ""
        except Exception as exc:
            return False, str(exc)


class AccessStore:
    def __init__(self, directory: Path | None = None):
        self.dir = directory or Path(user_config_dir(APP_NAME, appauthor=False))
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "allowed_friends.json"
        self._ids = self._load()

    def _load(self) -> set[int]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return set()
        if not isinstance(raw, dict):
            return set()
        values = raw.get("allowed_steam_ids", [])
        if not isinstance(values, list):
            return set()
        result: set[int] = set()
        for value in values:
            try:
                steam_id = int(value)
            except (TypeError, ValueError, OverflowError):
                continue
            if steam_id > 0:
                result.add(steam_id)
        return result

    def _save(self) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps({"allowed_steam_ids": sorted(self._ids)}, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def is_allowed(self, steam_id: int) -> bool:
        return int(steam_id) in self._ids

    def allow(self, steam_id: int) -> None:
        sid = int(steam_id)
        if sid > 0:
            self._ids.add(sid)
            self._save()

    def remove(self, steam_id: int) -> None:
        self._ids.discard(int(steam_id))
        self._save()

    def all_ids(self) -> set[int]:
        return set(self._ids)
