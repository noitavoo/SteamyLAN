from __future__ import annotations

import unittest

from SteamyLan.peer_guard import LobbyMembershipGuard


class LobbyMembershipGuardTests(unittest.TestCase):
    def test_member_can_arrive_after_network_session_request(self):
        guard = LobbyMembershipGuard(10.0)
        self.assertIsNone(guard.check(42, {1}, now=100.0))
        self.assertIsNone(guard.check(42, {1}, now=104.0))
        self.assertTrue(guard.check(42, {1, 42}, now=105.0))

    def test_non_member_is_rejected_only_after_grace_period(self):
        guard = LobbyMembershipGuard(10.0)
        self.assertIsNone(guard.check(42, set(), now=100.0))
        self.assertIsNone(guard.check(42, set(), now=109.999))
        self.assertFalse(guard.check(42, set(), now=110.0))

    def test_failed_member_lookup_uses_same_grace(self):
        guard = LobbyMembershipGuard(3.0)
        self.assertIsNone(guard.check(7, None, now=20.0))
        self.assertFalse(guard.check(7, None, now=23.0))
