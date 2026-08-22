from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import psutil


@dataclass(slots=True)
class TraceSummary:
    build_dir: str
    executable: str
    started_at: float
    finished_at: float
    poll_ms: int
    observed_files: list[str]
    essential_files: list[str]
    kept_files: list[str]
    original_bytes: int
    trimmed_bytes: int


def _norm(path: Path) -> str:
    return os.path.normcase(os.path.abspath(os.fspath(path)))


def _relative_if_inside(path: str | Path, root: Path) -> Path | None:
    try:
        candidate = Path(path)


        candidate_abs = Path(os.path.abspath(os.fspath(candidate)))
        root_abs = Path(os.path.abspath(os.fspath(root)))
        if os.path.commonpath([_norm(candidate_abs), _norm(root_abs)]) != _norm(root_abs):
            return None
        return Path(os.path.relpath(candidate_abs, root_abs))
    except (OSError, ValueError, TypeError):
        return None


def _observe_process(proc: psutil.Process, build_dir: Path, used: set[Path]) -> None:
    try:
        exe = proc.exe()
    except (psutil.Error, OSError):
        exe = ""
    if exe:
        rel = _relative_if_inside(exe, build_dir)
        if rel is not None:
            used.add(rel)

    try:
        mappings = proc.memory_maps(grouped=False)
    except (psutil.Error, OSError, NotImplementedError):
        mappings = ()
    for mapping in mappings:
        path = getattr(mapping, "path", "")
        if not path or path.startswith("["):
            continue
        rel = _relative_if_inside(path, build_dir)
        if rel is not None:
            used.add(rel)



    try:
        opened = proc.open_files()
    except (psutil.Error, OSError, NotImplementedError):
        opened = ()
    for item in opened:
        path = getattr(item, "path", "")
        if not path:
            continue
        rel = _relative_if_inside(path, build_dir)
        if rel is not None:
            used.add(rel)


def _process_tree(root_pid: int) -> list[psutil.Process]:
    try:
        root = psutil.Process(root_pid)
    except psutil.Error:
        return []

    result = [root]
    try:
        result.extend(root.children(recursive=True))
    except psutil.Error:
        pass
    return result


def _essential_files(build_dir: Path, executable_name: str) -> set[Path]:
    """Files that should survive even if a polling sample misses a very fast read.

    This list is intentionally small. It protects PyInstaller bootstrap pieces,
    SteamyLAN-owned data, and the user-supplied Steam DLL. Qt/Python extension
    modules and plugins are otherwise kept only when they are observed loaded.
    """
    keep: set[Path] = set()

    exact_names = {
        executable_name.casefold(),
        "steam_api64.dll",
        "windivert.dll",
        "windivert64.sys",
        "steam_appid.txt",
    }

    for path in build_dir.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(build_dir)
        name = path.name.casefold()
        rel_parts = tuple(part.casefold() for part in rel.parts)

        if name in exact_names:
            keep.add(rel)
            continue



        if name == "base_library.zip" or (name.startswith("python3") and name.endswith(".dll")):
            keep.add(rel)
            continue


        if "steamylan" in rel_parts[:-1]:
            keep.add(rel)
            continue

        if "windivert" in rel_parts[:-1]:
            keep.add(rel)
            continue


        if len(rel.parts) == 1 and path.suffix.casefold() in {".manifest", ".json", ".ini"}:
            keep.add(rel)

    return keep


def _load_previous_manifest(path: Path, build_dir: Path) -> set[Path]:
    if not path.is_file():
        return set()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return set()

    previous_build = payload.get("build_dir")
    if previous_build and _norm(Path(previous_build)) != _norm(build_dir):
        return set()

    result: set[Path] = set()
    for value in payload.get("kept_files", []):
        rel = Path(value)
        if not rel.is_absolute() and ".." not in rel.parts:
            result.add(rel)
    return result


def _dir_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            pass
    return total


def _format_size(value: int) -> str:
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB"):
        if size < 1024.0 or unit == "GiB":
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{value} B"


def _copy_trimmed(build_dir: Path, output_dir: Path, keep: set[Path]) -> None:
    if output_dir.exists():
        shutil.rmtree(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    for rel in sorted(keep, key=lambda p: os.fspath(p).casefold()):
        source = build_dir / rel
        if not source.is_file():
            continue
        target = output_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def _write_manifest(path: Path, summary: TraceSummary) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(summary), indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Trace which files a PyInstaller Windows build actually maps/opens, "
            "then create a much smaller copy containing only those files plus "
            "a minimal bootstrap safety set."
        )
    )
    parser.add_argument("--build", type=Path, default=Path("dist/SteamyLAN"), help="PyInstaller onedir folder")
    parser.add_argument("--exe", default="SteamyLAN.exe", help="Executable name inside --build")
    parser.add_argument("--output", type=Path, default=Path("dist/SteamyLAN_trimmed"), help="Trimmed output folder")
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("dist/SteamyLAN_usage.json"),
        help="Usage manifest path",
    )
    parser.add_argument("--poll-ms", type=int, default=40, help="Sampling interval in milliseconds")
    parser.add_argument(
        "--reset",
        action="store_true",
        help="Do not merge files from a previous usage manifest",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if os.name != "nt":
        print("This utility is for the Windows PyInstaller build only.")
        return 2

    build_dir = args.build.resolve()
    output_dir = args.output.resolve()
    manifest_path = args.manifest.resolve()
    executable = build_dir / args.exe

    if not executable.is_file():
        print(f"Build executable not found: {executable}")
        print("Run build_windows.bat first, then run this script again.")
        return 2

    output_inside_build = _relative_if_inside(output_dir, build_dir) is not None
    build_inside_output = _relative_if_inside(build_dir, output_dir) is not None
    if output_inside_build or build_inside_output:
        print("Refusing overlapping build and output folders.")
        print("Use a separate --output folder so trimming cannot delete or recursively copy the source build.")
        return 2

    poll_seconds = max(0.01, args.poll_ms / 1000.0)
    used: set[Path] = set()
    if not args.reset:
        used.update(_load_previous_manifest(manifest_path, build_dir))

    print()
    print("SteamyLAN build usage tracer")
    print("===========================")
    print(f"Build:  {build_dir}")
    print(f"Output: {output_dir}")
    print()
    print("SteamyLAN will open now.")
    print("Use every feature that the final build must support during this run.")
    print("Recommended coverage: Join, Settings, Create/process list, tray menu, and sharing.")
    print("When finished, EXIT SteamyLAN normally. The trim starts after it closes.")
    print()

    started_at = time.time()
    try:
        child = subprocess.Popen([os.fspath(executable)], cwd=os.fspath(build_dir))
    except OSError as exc:
        print(f"Could not launch {executable.name}: {exc}")
        return 3

    try:
        while child.poll() is None:
            for proc in _process_tree(child.pid):
                _observe_process(proc, build_dir, used)
            time.sleep(poll_seconds)


        for proc in _process_tree(child.pid):
            _observe_process(proc, build_dir, used)
    except KeyboardInterrupt:
        print()
        print("Tracing stopped. SteamyLAN was not force-closed.")
        print("No trimmed build was created.")
        return 130

    finished_at = time.time()
    observed = set(used)
    essential = _essential_files(build_dir, args.exe)
    keep = observed | essential


    keep = {rel for rel in keep if (build_dir / rel).is_file()}

    if not keep:
        print("No build files were observed. Nothing was changed.")
        return 4

    original_bytes = _dir_size(build_dir)
    _copy_trimmed(build_dir, output_dir, keep)
    trimmed_bytes = _dir_size(output_dir)

    summary = TraceSummary(
        build_dir=os.fspath(build_dir),
        executable=args.exe,
        started_at=started_at,
        finished_at=finished_at,
        poll_ms=max(10, args.poll_ms),
        observed_files=sorted(os.fspath(p).replace("\\", "/") for p in observed if (build_dir / p).is_file()),
        essential_files=sorted(os.fspath(p).replace("\\", "/") for p in essential if (build_dir / p).is_file()),
        kept_files=sorted(os.fspath(p).replace("\\", "/") for p in keep),
        original_bytes=original_bytes,
        trimmed_bytes=trimmed_bytes,
    )
    _write_manifest(manifest_path, summary)

    saved = max(0, original_bytes - trimmed_bytes)
    percent = (saved / original_bytes * 100.0) if original_bytes else 0.0

    print()
    print("Trim complete")
    print("=============")
    print(f"Original: {_format_size(original_bytes)}")
    print(f"Trimmed:  {_format_size(trimmed_bytes)}")
    print(f"Saved:    {_format_size(saved)} ({percent:.1f}%)")
    print(f"Files kept: {len(keep)}")
    print(f"Manifest: {manifest_path}")
    print(f"Trimmed build: {output_dir}")
    print()
    print("The original dist\\SteamyLAN folder was not modified.")
    print("If a feature was not exercised during tracing, run the tracer again and use that feature;")
    print("the manifest is merged by default so coverage accumulates across runs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
