from __future__ import annotations

import argparse
import hashlib
import re
import shutil
import zipfile
from pathlib import Path


SKIP_DIRS = {"__pycache__", ".pytest_cache"}
SKIP_SUFFIXES = {".pyc", ".pyo", ".pdb", ".exp", ".lib"}
SKIP_NAMES = {"Thumbs.db", ".DS_Store"}
VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Create the CI release package. The resulting ZIP always contains a single "
            "top-level SteamyLAN folder and is named SteamyLAN_v<version>.zip."
        )
    )
    parser.add_argument("--build", type=Path, default=Path("dist/SteamyLAN"))
    parser.add_argument("--output-root", type=Path, default=Path("release"))
    parser.add_argument(
        "--version",
        required=True,
        help="Three-part release version without the leading v, for example 1.0.0",
    )
    return parser.parse_args()


def release_zip_name(version: str) -> str:
    version = version.strip()
    if not VERSION_RE.fullmatch(version):
        raise ValueError(f"Invalid release version: {version!r}")
    return f"SteamyLAN_v{version}.zip"


def should_skip(relative: Path) -> bool:
    if any(part in SKIP_DIRS for part in relative.parts):
        return True
    if relative.name in SKIP_NAMES:
        return True
    return relative.suffix.casefold() in SKIP_SUFFIXES


def copy_runtime(build_dir: Path, runtime_dir: Path) -> None:
    if runtime_dir.exists():
        shutil.rmtree(runtime_dir)
    runtime_dir.mkdir(parents=True, exist_ok=True)

    for source in build_dir.rglob("*"):
        relative = source.relative_to(build_dir)
        if should_skip(relative):
            continue
        target = runtime_dir / relative
        if source.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif source.is_file():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def zip_runtime(runtime_dir: Path, zip_path: Path) -> None:
    if zip_path.exists():
        zip_path.unlink()
    with zipfile.ZipFile(
        zip_path,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for source in sorted(runtime_dir.rglob("*")):
            if not source.is_file():
                continue
            relative = source.relative_to(runtime_dir)
            archive.write(source, Path("SteamyLAN") / relative)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def main() -> int:
    args = parse_args()
    build_dir = args.build.resolve()
    output_root = args.output_root.resolve()
    runtime_dir = output_root / "SteamyLAN"
    try:
        zip_name = release_zip_name(args.version)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    zip_path = output_root / zip_name

    if not (build_dir / "SteamyLAN.exe").is_file():
        raise SystemExit(f"Missing built executable: {build_dir / 'SteamyLAN.exe'}")
    if not (build_dir / "steam_api64.dll").is_file():
        raise SystemExit(f"Missing Steamworks runtime DLL: {build_dir / 'steam_api64.dll'}")
    if not (build_dir / "SteamyLANUpdate.exe").is_file():
        raise SystemExit(f"Missing updater helper: {build_dir / 'SteamyLANUpdate.exe'}")

    output_root.mkdir(parents=True, exist_ok=True)
    copy_runtime(build_dir, runtime_dir)

    if not (runtime_dir / "SteamyLAN.exe").is_file():
        raise SystemExit("Packaging removed SteamyLAN.exe unexpectedly")
    if not (runtime_dir / "steam_api64.dll").is_file():
        raise SystemExit("Packaging removed steam_api64.dll unexpectedly")
    if not (runtime_dir / "SteamyLANUpdate.exe").is_file():
        raise SystemExit("Packaging removed SteamyLANUpdate.exe unexpectedly")

    zip_runtime(runtime_dir, zip_path)
    print(f"Release folder: {runtime_dir}")
    print(f"Release ZIP:    {zip_path}")
    print(f"SHA-256:        {sha256(zip_path)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
