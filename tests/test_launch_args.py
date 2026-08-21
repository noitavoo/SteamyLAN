from __future__ import annotations

import unittest

from SteamyLan.launch_args import connect_lobby_id_from_argv


class LaunchArgsTests(unittest.TestCase):
    def test_parses_steam_connect_lobby_pair(self):
        self.assertEqual(connect_lobby_id_from_argv(["+connect_lobby", "109775241234567890"]), 109775241234567890)

    def test_parses_connect_lobby_equals_form(self):
        self.assertEqual(connect_lobby_id_from_argv(["+connect_lobby=109775241234567891"]), 109775241234567891)

    def test_parses_steam_joinlobby_url(self):
        self.assertEqual(
            connect_lobby_id_from_argv(["steam://joinlobby/480/109775241234567892/76561198000000000"]),
            109775241234567892,
        )

    def test_ignores_invalid_lobby_id(self):
        self.assertEqual(connect_lobby_id_from_argv(["+connect_lobby", "not-a-number"]), 0)
        self.assertEqual(connect_lobby_id_from_argv(["+connect_lobby", "0"]), 0)


if __name__ == "__main__":
    unittest.main()
