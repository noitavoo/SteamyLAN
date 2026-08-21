from __future__ import annotations

from collections.abc import Iterable


def connect_lobby_id_from_argv(argv: Iterable[object]) -> int:
    """Extract Steam's lobby-join launch request from command-line arguments.

    Steam lobby invitations launch an app with ``+connect_lobby <lobby id>``
    when the app is not already running.  Accept a few harmless equivalent
    spellings as well so forwarded single-instance arguments remain robust.
    """
    args = [str(value).strip() for value in argv if str(value).strip()]
    keys = {"+connect_lobby", "-connect_lobby", "--connect_lobby"}

    for index, raw in enumerate(args):
        lowered = raw.casefold()
        if lowered in keys:
            if index + 1 < len(args):
                lobby_id = _positive_int(args[index + 1])
                if lobby_id:
                    return lobby_id
            continue
        for key in keys:
            prefix = key + "="
            if lowered.startswith(prefix):
                lobby_id = _positive_int(raw[len(prefix):])
                if lobby_id:
                    return lobby_id

        # Also accept an explicit Steam lobby URL when one is forwarded by a
        # launcher/shortcut. Typical form: steam://joinlobby/<appid>/<lobby>/<friend>.
        if lowered.startswith("steam://joinlobby/"):
            parts = raw.split("/")
            if len(parts) >= 5:
                lobby_id = _positive_int(parts[4])
                if lobby_id:
                    return lobby_id
    return 0


def _positive_int(value: object) -> int:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return 0
    return number if number > 0 else 0
