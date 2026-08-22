from __future__ import annotations

import argparse
import ctypes
from ctypes import wintypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="SteamyLAN staged update helper")
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--install-dir", type=Path, required=True)
    parser.add_argument("--parent-pid", type=int, required=True)
    parser.add_argument("--exe", default="SteamyLAN.exe")
    parser.add_argument("--version", required=True)
    parser.add_argument("--attempt-file", type=Path, required=True)
    return parser.parse_args()


class _TrayProgress:
    """Small native Windows tray indicator for the detached update process."""

    _NIM_ADD = 0x00000000
    _NIM_MODIFY = 0x00000001
    _NIM_DELETE = 0x00000002
    _NIF_ICON = 0x00000002
    _NIF_TIP = 0x00000004
    _IDI_INFORMATION = 32516

    class _NotifyIconData(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
            ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
            ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
            ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
            ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
            ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
            ("dwInfoFlags", wintypes.DWORD), ("guidItem", ctypes.c_byte * 16),
            ("hBalloonIcon", wintypes.HICON),
        ]

    def __init__(self):
        self._data = None
        self._active = False
        if os.name != "nt":
            return
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            shell32 = ctypes.WinDLL("shell32", use_last_error=True)
            user32.CreateWindowExW.argtypes = [
                wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
                ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID,
            ]
            user32.CreateWindowExW.restype = wintypes.HWND
            hwnd = user32.CreateWindowExW(0, "STATIC", "SteamyLAN Update", 0, 0, 0, 0, 0, None, None, None, None)
            if not hwnd:
                return
            user32.LoadIconW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR]
            user32.LoadIconW.restype = wintypes.HICON
            shell32.Shell_NotifyIconW.argtypes = [wintypes.DWORD, ctypes.POINTER(self._NotifyIconData)]
            shell32.Shell_NotifyIconW.restype = wintypes.BOOL
            self._user32, self._shell32 = user32, shell32
            self._data = self._NotifyIconData()
            self._data.cbSize = ctypes.sizeof(self._NotifyIconData)
            self._data.hWnd = hwnd
            self._data.uID = 1
            self._data.uFlags = self._NIF_ICON | self._NIF_TIP
            resource_id = ctypes.cast(ctypes.c_void_p(self._IDI_INFORMATION), wintypes.LPCWSTR)
            self._data.hIcon = user32.LoadIconW(None, resource_id)
            self.update("Starting update…", add=True)
        except Exception:
            self.close()

    def update(self, status: str, *, add: bool = False) -> None:
        if self._data is None:
            return
        try:
            self._data.szTip = f"SteamyLAN update — {status}"[:127]
            operation = self._NIM_ADD if add and not self._active else self._NIM_MODIFY
            self._active = bool(self._shell32.Shell_NotifyIconW(operation, ctypes.byref(self._data)))
        except Exception:
            pass

    def close(self) -> None:
        if self._data is not None:
            try:
                self._shell32.Shell_NotifyIconW(self._NIM_DELETE, ctypes.byref(self._data))
                self._user32.DestroyWindow(self._data.hWnd)
            except Exception:
                pass
        self._data = None
        self._active = False


def _write_attempt(path: Path, version: str, phase: str) -> None:
    try:
        payload = {"version": str(version), "phase": str(phase), "updated_at": time.time()}
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        pass


def _record_failure(install_dir: Path, version: str, error: Exception, attempt: Path) -> None:
    try:
        path = install_dir.parent / "SteamyLAN-update-failed.json"
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps({
            "version": str(version), "reason": f"{type(error).__name__}: {error}"[:2000],
            "failed_at": time.time(),
        }, sort_keys=True), encoding="utf-8")
        temporary.replace(path)
    except OSError:
        pass
    attempt.unlink(missing_ok=True)


def _wait_for_parent(pid: int) -> None:
    if int(pid) <= 0:
        return

    # On Windows, os.kill(pid, 0) is not a reliable process-liveness check.
    # Wait on the actual process handle so replacement cannot start while the
    # main executable still has DLLs or its working directory open.
    if os.name == "nt":
        try:
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_bool, ctypes.c_uint32]
            kernel32.OpenProcess.restype = ctypes.c_void_p
            kernel32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
            kernel32.WaitForSingleObject.restype = ctypes.c_uint32
            kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
            kernel32.CloseHandle.restype = ctypes.c_bool
            handle = kernel32.OpenProcess(0x00100000, False, int(pid))  # SYNCHRONIZE
            if handle:
                try:
                    result = int(kernel32.WaitForSingleObject(handle, 60_000))
                finally:
                    kernel32.CloseHandle(handle)
                if result == 0:  # WAIT_OBJECT_0
                    return
                if result == 0x102:  # WAIT_TIMEOUT
                    raise RuntimeError("The running SteamyLAN process did not exit in time.")
            elif ctypes.get_last_error() == 87:  # ERROR_INVALID_PARAMETER: process is gone
                return
        except RuntimeError:
            raise
        except Exception:
            # Fall through to the portable check if the native wait is not
            # available, for example when this helper is tested off Windows.
            pass

    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.25)
    raise RuntimeError("The running SteamyLAN process did not exit in time.")


def _replace_with_retry(source: Path, target: Path, timeout: float = 45.0) -> None:
    deadline = time.monotonic() + max(1.0, float(timeout))
    last_error: OSError | None = None
    while time.monotonic() < deadline:
        try:
            os.replace(str(source), str(target))
            return
        except OSError as exc:
            last_error = exc
            time.sleep(0.25)
    if last_error is not None:
        raise RuntimeError(f"Could not replace {target}: {last_error}") from last_error
    raise RuntimeError(f"Could not replace {target}.")


def _extract_archive(archive: Path, parent: Path, progress: _TrayProgress | None = None) -> Path:
    stage = Path(tempfile.mkdtemp(prefix="SteamyLAN-stage-", dir=str(parent)))
    try:
        with zipfile.ZipFile(archive) as source:
            names = source.namelist()
            if "SteamyLAN/SteamyLAN.exe" not in names:
                raise RuntimeError("The update archive is missing SteamyLAN.exe.")
            if "SteamyLAN/SteamyLANUpdate.exe" not in names:
                raise RuntimeError("The update archive is missing the updater helper.")
            total = max(1, len(names))
            for index, name in enumerate(names, start=1):
                if progress and (index == 1 or index == total or index % 20 == 0):
                    progress.update(f"Extracting files ({index}/{total})…")
                relative = Path(name)
                if relative.is_absolute() or ".." in relative.parts or not name.startswith("SteamyLAN/"):
                    raise RuntimeError("The update archive contains an unsafe path.")
                target = stage / relative
                if name.endswith("/"):
                    target.mkdir(parents=True, exist_ok=True)
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                with source.open(name) as input_file, target.open("wb") as output_file:
                    shutil.copyfileobj(input_file, output_file, length=1024 * 1024)
        return stage / "SteamyLAN"
    except Exception:
        shutil.rmtree(stage, ignore_errors=True)
        raise


def _swap_and_launch(new_dir: Path, install_dir: Path, executable: str, progress: _TrayProgress | None = None) -> bool:
    backup = install_dir.with_name(f"{install_dir.name}.previous-{os.getpid()}")
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    if progress:
        progress.update("Replacing the previous version…")
    _replace_with_retry(install_dir, backup)
    try:
        if progress:
            progress.update("Installing the new version…")
        _replace_with_retry(new_dir, install_dir)
        target = install_dir / executable
        if not target.is_file():
            raise RuntimeError("The staged update did not contain the application executable.")
        if progress:
            progress.update("Starting the updated app…")
        process = subprocess.Popen([str(target)], cwd=str(install_dir), close_fds=True)
        time.sleep(8)
        if process.poll() is not None:
            raise RuntimeError("The updated application closed immediately after launch.")
        shutil.rmtree(backup, ignore_errors=True)
        return True
    except Exception:
        if install_dir.exists():
            failed = install_dir.with_name(f"{install_dir.name}.failed-{os.getpid()}")
            try:
                _replace_with_retry(install_dir, failed, timeout=10.0)
            except Exception:
                pass
            shutil.rmtree(failed, ignore_errors=True)
        if backup.exists():
            _replace_with_retry(backup, install_dir, timeout=10.0)
        raise


def _launch_existing(install_dir: Path, executable: str) -> None:
    target = install_dir / executable
    if not target.is_file():
        raise RuntimeError("The previous SteamyLAN executable could not be found after the update failed.")
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    subprocess.Popen(
        [str(target)],
        cwd=str(install_dir.parent),
        creationflags=flags,
        close_fds=True,
    )


def main() -> int:
    args = _parse_args()
    archive = args.archive.resolve()
    install_dir = args.install_dir.resolve()
    stage_root = None
    succeeded = False
    parent_stopped = False
    tray = _TrayProgress()
    try:
        _write_attempt(args.attempt_file, args.version, "waiting-for-app")
        tray.update("Waiting for SteamyLAN to close…")
        _wait_for_parent(args.parent_pid)
        parent_stopped = True
        _write_attempt(args.attempt_file, args.version, "extracting")
        tray.update("Preparing the update…")
        stage_root = _extract_archive(archive, install_dir.parent, tray)
        _write_attempt(args.attempt_file, args.version, "installing")
        _swap_and_launch(stage_root, install_dir, args.exe, tray)
        succeeded = True
        args.attempt_file.unlink(missing_ok=True)
        (install_dir.parent / "SteamyLAN-update-failed.json").unlink(missing_ok=True)
        return 0
    except Exception as exc:
        log_path = install_dir.parent / "SteamyLAN-update-error.log"
        try:
            log_path.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
        except OSError:
            pass
        _record_failure(install_dir, args.version, exc, args.attempt_file)
        tray.update("Update failed — restoring the previous version…")
        if parent_stopped:
            try:
                _launch_existing(install_dir, args.exe)
            except Exception as restart_exc:
                try:
                    with log_path.open("a", encoding="utf-8") as handle:
                        handle.write(f"Restart failed: {type(restart_exc).__name__}: {restart_exc}\n")
                except OSError:
                    pass
        return 1
    finally:
        if succeeded:
            archive.unlink(missing_ok=True)
        if stage_root is not None:
            shutil.rmtree(stage_root.parent, ignore_errors=True)
        tray.close()


if __name__ == "__main__":
    raise SystemExit(main())
