from __future__ import annotations

import argparse
import ctypes
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
    return parser.parse_args()


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


def _extract_archive(archive: Path, parent: Path) -> Path:
    stage = Path(tempfile.mkdtemp(prefix="SteamyLAN-stage-", dir=str(parent)))
    try:
        with zipfile.ZipFile(archive) as source:
            names = source.namelist()
            if "SteamyLAN/SteamyLAN.exe" not in names:
                raise RuntimeError("The update archive is missing SteamyLAN.exe.")
            if "SteamyLAN/SteamyLANUpdate.exe" not in names:
                raise RuntimeError("The update archive is missing the updater helper.")
            for name in names:
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


def _swap_and_launch(new_dir: Path, install_dir: Path, executable: str) -> bool:
    backup = install_dir.with_name(f"{install_dir.name}.previous-{os.getpid()}")
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    _replace_with_retry(install_dir, backup)
    try:
        _replace_with_retry(new_dir, install_dir)
        target = install_dir / executable
        if not target.is_file():
            raise RuntimeError("The staged update did not contain the application executable.")
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
    try:
        _wait_for_parent(args.parent_pid)
        parent_stopped = True
        stage_root = _extract_archive(archive, install_dir.parent)
        _swap_and_launch(stage_root, install_dir, args.exe)
        succeeded = True
        return 0
    except Exception as exc:
        log_path = install_dir.parent / "SteamyLAN-update-error.log"
        try:
            log_path.write_text(f"{type(exc).__name__}: {exc}\n", encoding="utf-8")
        except OSError:
            pass
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


if __name__ == "__main__":
    raise SystemExit(main())
