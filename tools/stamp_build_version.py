from __future__ import annotations

import argparse
import re
import tomllib
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Stamp SteamyLAN's runtime APP_VERSION with the GitHub Actions build number."
    )
    parser.add_argument("--build-number", required=True, type=int)
    parser.add_argument("--pyproject", type=Path, default=Path("pyproject.toml"))
    parser.add_argument("--constants", type=Path, default=Path("SteamyLan/constants.py"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.build_number < 1:
        raise SystemExit("--build-number must be at least 1")

    with args.pyproject.open("rb") as handle:
        payload = tomllib.load(handle)

    base_version = str(payload["project"]["version"]).strip()
    if not re.fullmatch(r"\d+\.\d+\.\d+", base_version):
        raise SystemExit(
            f"Expected project.version to be numeric MAJOR.MINOR.PATCH, got {base_version!r}"
        )

    build_version = f"{base_version}.{args.build_number}"
    text = args.constants.read_text(encoding="utf-8")
    updated, count = re.subn(
        r'(?m)^APP_VERSION\s*=\s*["\'][^"\']+["\']\s*$',
        f'APP_VERSION = "{build_version}"',
        text,
        count=1,
    )
    if count != 1:
        raise SystemExit(f"Could not find exactly one APP_VERSION assignment in {args.constants}")

    args.constants.write_text(updated, encoding="utf-8")
    print(build_version)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
