from __future__ import annotations

import ctypes
import os
import sys
from pathlib import Path

from PySide6.QtGui import QIcon

from .constants import APP_NAME


def _candidate_paths(filename: str) -> list[Path]:
    package_dir = Path(__file__).resolve().parent
    candidates = [package_dir / filename]
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        base = Path(meipass)
        candidates.extend([base / "SteamyLan" / filename, base / filename])
    if getattr(sys, "frozen", False):
        exe_dir = Path(sys.executable).resolve().parent
        candidates.extend([exe_dir / "SteamyLan" / filename, exe_dir / "_internal" / "SteamyLan" / filename])
    result: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        key = str(path).casefold()
        if key not in seen:
            seen.add(key)
            result.append(path)
    return result


def application_icon() -> QIcon:
    icon = QIcon()


    for filename in ("steamylan.png", "steamylan.ico"):
        for path in _candidate_paths(filename):
            if path.is_file():
                icon.addFile(str(path))
    return icon


def set_windows_app_user_model_id() -> None:
    if os.name != "nt":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(f"noitavoo.{APP_NAME}.Desktop")
    except Exception:
        pass
