from __future__ import annotations

import time
from collections.abc import Iterable


class LobbyMembershipGuard:
    """Allow Steam lobby membership a short window to synchronize.

    SteamNetworkingMessages session/auth traffic can arrive before the local
    matchmaking member list reflects the newly joined peer.  ``check`` returns
    True for a confirmed member, None while synchronization is still within the
    grace window, and False only after the grace window expires.
    """

    def __init__(self, grace_seconds: float = 10.0):
        self.grace_seconds = max(0.0, float(grace_seconds))
        self._first_seen: dict[int, float] = {}

    def check(
        self,
        peer_id: int,
        member_ids: Iterable[int] | None,
        *,
        now: float | None = None,
    ) -> bool | None:
        sid = int(peer_id)
        if member_ids is not None and sid in {int(value) for value in member_ids}:
            self._first_seen.pop(sid, None)
            return True

        stamp = time.monotonic() if now is None else float(now)
        first = self._first_seen.setdefault(sid, stamp)
        if stamp - first < self.grace_seconds:
            return None
        self._first_seen.pop(sid, None)
        return False

    def forget(self, peer_id: int) -> None:
        self._first_seen.pop(int(peer_id), None)

    def clear(self) -> None:
        self._first_seen.clear()
