from __future__ import annotations

import ctypes
import unittest

from SteamyLan.steam_api import (
    SteamNetConnectionInfoPrefix,
    SteamNetworkingIdentity,
    SteamNetworkingIPAddr,
    SteamNetworkingMessagesSessionFailedPrefix,
    SteamNetworkingMessagesSessionRequest,
    SteamNetworkingMessage,
    SteamNetConnectionRealTimeStatus,
    SteamClient,
)


class SteamNetworkingAbiTests(unittest.TestCase):
    def test_networking_identity_layout_matches_steamworks(self):
        self.assertEqual(ctypes.sizeof(SteamNetworkingIdentity), 136)
        self.assertEqual(SteamNetworkingIdentity.m_eType.offset, 0)
        self.assertEqual(SteamNetworkingIdentity.m_cbSize.offset, 4)
        self.assertEqual(SteamNetworkingIdentity.data.offset, 8)

        identity = SteamNetworkingIdentity.for_steam_id(76561198000000000)
        self.assertEqual(identity.steam_id(), 76561198000000000)

    def test_received_message_and_realtime_status_layouts(self):
        # 64-bit SteamNetworkingMessage_t offsets from Valve's current header.
        self.assertEqual(ctypes.sizeof(SteamNetworkingMessage), 216)
        self.assertEqual(SteamNetworkingMessage.m_identityPeer.offset, 16)
        self.assertEqual(SteamNetworkingMessage.m_nConnUserData.offset, 152)
        self.assertEqual(SteamNetworkingMessage.m_nChannel.offset, 192)
        self.assertEqual(SteamNetworkingMessage.m_idxLane.offset, 208)
        self.assertEqual(ctypes.sizeof(SteamNetConnectionRealTimeStatus), 120)

    def test_session_callbacks_use_expected_native_prefix_layout(self):
        self.assertEqual(ctypes.sizeof(SteamNetworkingIPAddr), 18)
        self.assertEqual(ctypes.sizeof(SteamNetworkingMessagesSessionRequest), 136)
        self.assertEqual(SteamNetConnectionInfoPrefix.m_eEndReason.offset, 180)
        self.assertEqual(SteamNetConnectionInfoPrefix.m_szEndDebug.offset, 184)
        self.assertEqual(SteamNetworkingMessagesSessionFailedPrefix.m_info.offset, 0)

    def test_rich_presence_values_are_utf8_safe_and_normalized(self):
        value = SteamClient._rich_presence_text("  Sharing\x00  世界  ")
        self.assertEqual(value, "Sharing 世界")

        truncated = SteamClient._rich_presence_text("世界" * 200, max_bytes=7)
        self.assertLessEqual(len(truncated.encode("utf-8")), 7)
        truncated.encode("utf-8").decode("utf-8")


if __name__ == "__main__":
    unittest.main()
