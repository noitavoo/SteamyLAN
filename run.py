from __future__ import annotations

import sys

def main() -> int:
    if not ((3, 14, 7) <= sys.version_info[:3] < (3, 15, 0)):
        print("SteamyLAN requires Python 3.14.7 or newer within the 3.14 series.")
        return 2
    try:
        from SteamyLan.app import main as app_main
    except ModuleNotFoundError as exc:
        missing = getattr(exc, "name", "a dependency")
        print(f"Missing {missing} in the Python environment currently on PATH.")
        return 3
    return int(app_main(sys.argv))


if __name__ == "__main__":
    raise SystemExit(main())
