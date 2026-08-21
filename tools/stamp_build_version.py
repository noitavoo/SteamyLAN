from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Resolve and stamp SteamyLAN's next three-part release version."
    )
    parser.add_argument(
        "--previous-version",
        default="",
        help="Latest three-part release version. The next decimal release is stamped.",
    )
    parser.add_argument(
        "--reuse-version",
        default="",
        help="Existing version for this exact commit. It is stamped without incrementing.",
    )
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--constants", type=Path, default=Path("SteamyLan/constants.py"))
    return parser.parse_args()


VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")


def normalize_version(value: str) -> str:
    version = str(value or "").strip().lstrip("vV")
    if not VERSION_RE.fullmatch(version):
        raise ValueError(f"Expected a numeric MAJOR.MINOR.PATCH version, got {value!r}")
    return version


def next_release_version(value: str) -> str:
    """Advance one release, carrying patch 9 into the minor number."""
    major, minor, patch = (int(part) for part in normalize_version(value).split("."))
    if patch >= 9:
        minor += 1
        patch = 0
    else:
        patch += 1
    return f"{major}.{minor}.{patch}"


def resolve_release_version(
    base_version: str,
    *,
    previous_version: str = "",
    reuse_version: str = "",
) -> str:
    base = normalize_version(base_version)
    if reuse_version:
        return normalize_version(reuse_version)
    if not previous_version:
        return base
    previous = normalize_version(previous_version)
    # A deliberately raised project baseline starts a new sequence. Otherwise
    # continue from the latest published three-part release.
    base_parts = tuple(int(part) for part in base.split("."))
    previous_parts = tuple(int(part) for part in previous.split("."))
    return base if previous_parts < base_parts else next_release_version(previous)


def stamp_version(constants: Path, version: str) -> None:
    version = normalize_version(version)
    text = constants.read_text(encoding="utf-8")
    updated, count = re.subn(
        r'(?m)^APP_VERSION\s*=\s*["\'][^"\']+["\']\s*$',
        f'APP_VERSION = "{version}"',
        text,
        count=1,
    )
    if count != 1:
        raise ValueError(f"Could not find exactly one APP_VERSION assignment in {constants}")
    constants.write_text(updated, encoding="utf-8")


def main() -> int:
    args = parse_args()
    if args.previous_version and args.reuse_version:
        raise SystemExit("Use either --previous-version or --reuse-version, not both")

    with args.pyproject.open("rb") as handle:
        payload = tomllib.load(handle)

    base_version = str(payload["project"]["version"]).strip()
    try:
        release_version = resolve_release_version(
            base_version,
            previous_version=args.previous_version,
            reuse_version=args.reuse_version,
        )
        stamp_version(args.constants, release_version)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc
    print(release_version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
