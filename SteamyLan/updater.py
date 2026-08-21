from __future__ import annotations

import json
import ctypes
import hashlib
import os
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path

from .constants import APP_NAME, APP_VERSION, GITHUB_REPOSITORY


class UpdateCheckError(RuntimeError):
    """Raised when GitHub responds but no usable release version is available."""


@dataclass(frozen=True, slots=True)
class UpdateInfo:
    current_version: str
    latest_version: str
    download_url: str
    release_url: str
    asset_name: str = ""
    sha256: str = ""
    release_notes: str = ""

    @property
    def newer(self) -> bool:
        return _version_key(self.latest_version) > _version_key(self.current_version)

    @property
    def same_version(self) -> bool:
        return _version_key(self.latest_version) == _version_key(self.current_version)

    @property
    def installable(self) -> bool:
        return self.download_url.lower().endswith(".zip") and bool(self.asset_name)


def update_cache_directory() -> Path:
    """Return the download location used by the packaged updater.

    Keeping the archive beside the installed executable means the helper can
    stage and replace that installation without crossing user-profile or
    volume boundaries.
    """
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(tempfile.gettempdir()) / APP_NAME / "updates"


def _version_key(value: str) -> tuple[int, ...]:
    text = str(value or "").strip().lstrip("vV")
    parts = [int(x) for x in re.findall(r"\d+", text)]
    return tuple((parts + [0, 0, 0])[:4])


def _request_json(url: str):
    request = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"{APP_NAME}/{APP_VERSION}",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        return json.loads(response.read().decode("utf-8"))


def _request_text(url: str) -> str:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": f"{APP_NAME}/{APP_VERSION}"},
    )
    with urllib.request.urlopen(request, timeout=8) as response:
        return response.read().decode("utf-8", "replace")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _validate_archive(path: Path) -> None:
    total_size = 0
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if "SteamyLAN/SteamyLAN.exe" not in names:
            raise UpdateCheckError("The update archive does not contain SteamyLAN.exe.")
        if "SteamyLAN/SteamyLANUpdate.exe" not in names:
            raise UpdateCheckError("The update archive does not contain the updater helper.")
        for name in names:
            item = Path(name)
            if item.is_absolute() or ".." in item.parts or not name.startswith("SteamyLAN/"):
                raise UpdateCheckError("The update archive contains an unsafe path.")
            info = archive.getinfo(name)
            total_size += int(info.file_size)
            if total_size > 1024 * 1024 * 1024:
                raise UpdateCheckError("The update archive is unexpectedly large.")


def download_update(info: UpdateInfo, cache_dir: Path | None = None) -> Path:
    """Download and validate an update archive into a temporary location."""
    if not info.installable:
        raise UpdateCheckError("This release does not contain an installable Windows package.")
    if cache_dir is not None:
        cache_dir = Path(cache_dir)
        cache_dir.mkdir(parents=True, exist_ok=True)
        path = cache_dir / f"SteamyLAN-update-{_version_key(info.latest_version)}.zip"
        for stale in cache_dir.glob("SteamyLAN-update-*.zip"):
            if stale != path:
                stale.unlink(missing_ok=True)
        for partial in cache_dir.glob("SteamyLAN-update-*.zip.part"):
            if partial != path.with_suffix(path.suffix + ".part"):
                partial.unlink(missing_ok=True)
        if path.is_file():
            try:
                if (not info.sha256 or _sha256(path) == info.sha256):
                    _validate_archive(path)
                    return path
            except Exception:
                path.unlink(missing_ok=True)
    else:
        fd, raw_path = tempfile.mkstemp(prefix="SteamyLAN-update-", suffix=".zip")
        os.close(fd)
        path = Path(raw_path)
    partial = path.with_suffix(path.suffix + ".part")
    try:
        request = urllib.request.Request(
            info.download_url,
            headers={
                "Accept": "application/octet-stream",
                "User-Agent": f"{APP_NAME}/{APP_VERSION}",
            },
        )
        with urllib.request.urlopen(request, timeout=60) as response, partial.open("wb") as handle:
            length = int(response.headers.get("Content-Length") or 0)
            if length > 1024 * 1024 * 1024:
                raise UpdateCheckError("The update download is unexpectedly large.")
            for chunk in iter(lambda: response.read(1024 * 1024), b""):
                handle.write(chunk)
        partial.replace(path)
        if info.sha256 and _sha256(path) != info.sha256:
            raise UpdateCheckError("The update download failed its SHA-256 verification.")
        _validate_archive(path)
        return path
    except Exception:
        partial.unlink(missing_ok=True)
        path.unlink(missing_ok=True)
        raise


def launch_update_helper(archive: Path, parent_pid: int) -> None:
    """Start the detached Windows updater and return before the app exits."""
    if not getattr(sys, "frozen", False):
        raise UpdateCheckError("Automatic installation is available in the packaged Windows app.")
    install_dir = Path(sys.executable).resolve().parent
    helper = install_dir / "SteamyLANUpdate.exe"
    if not helper.is_file():
        raise UpdateCheckError("The updater helper is missing from this installation.")
    fd, helper_copy_name = tempfile.mkstemp(prefix="SteamyLANUpdate-", suffix=".exe")
    os.close(fd)
    helper_copy = Path(helper_copy_name)
    try:
        helper_copy.unlink(missing_ok=True)
        shutil.copy2(helper, helper_copy)
    except Exception:
        helper_copy.unlink(missing_ok=True)
        raise UpdateCheckError("Could not prepare the updater helper.")
    command = [
        str(helper_copy),
        "--archive", str(Path(archive).resolve()),
        "--install-dir", str(install_dir),
        "--parent-pid", str(int(parent_pid)),
        "--exe", "SteamyLAN.exe",
    ]
    flags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) | getattr(subprocess, "DETACHED_PROCESS", 0)
    # Do not use install_dir as the helper's current directory. Windows can
    # keep a current-directory handle open, preventing the helper from moving
    # the directory it is about to replace.
    subprocess.Popen(command, cwd=str(install_dir.parent), creationflags=flags, close_fds=True)


def check_for_update() -> UpdateInfo:
    api = f"https://api.github.com/repos/{GITHUB_REPOSITORY}"
    release_url = f"https://github.com/{GITHUB_REPOSITORY}/releases"
    latest_version = ""
    download_url = ""
    asset_name = ""
    sha256 = ""
    release_notes = ""

    try:
        release = _request_json(f"{api}/releases/latest")
        if isinstance(release, dict):
            raw_version = str(release.get("tag_name") or "").strip()
            if raw_version:
                latest_version = raw_version.lstrip("vV")
            release_url = str(release.get("html_url") or release_url)
            release_notes = str(release.get("body") or "").strip()
            assets = release.get("assets") or []
            if isinstance(assets, list):
                zip_assets = [
                    item for item in assets
                    if isinstance(item, dict)
                    and str(item.get("browser_download_url") or "").lower().endswith(".zip")
                ]
                if zip_assets:
                    asset = zip_assets[0]
                    download_url = str(asset.get("browser_download_url") or "")
                    asset_name = str(asset.get("name") or "")
                    checksum_assets = [
                        item for item in assets
                        if isinstance(item, dict)
                        and str(item.get("name") or "").casefold() in {
                            f"{asset_name}.sha256".casefold(),
                            "sha256.txt",
                        }
                    ]
                    if checksum_assets:
                        checksum_url = str(checksum_assets[0].get("browser_download_url") or "")
                        try:
                            match = re.search(r"\b([0-9a-fA-F]{64})\b", _request_text(checksum_url))
                            sha256 = match.group(1).lower() if match else ""
                        except Exception:
                            sha256 = ""
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
        tags = _request_json(f"{api}/tags?per_page=1")
        if isinstance(tags, list) and tags and isinstance(tags[0], dict):
            tag = tags[0]
            raw_name = str(tag.get("name") or "").strip()
            if raw_name:
                latest_version = raw_name.lstrip("vV")
                release_url = f"https://github.com/{GITHUB_REPOSITORY}/releases/tag/{raw_name}"
                download_url = str(tag.get("zipball_url") or "")

    if not latest_version:
        raise UpdateCheckError(
            f"No published release or tag was found for {GITHUB_REPOSITORY}."
        )
    if not download_url:
        download_url = release_url
    return UpdateInfo(APP_VERSION, latest_version, download_url, release_url, asset_name, sha256, release_notes)
