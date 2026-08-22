from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from SteamyLan.models import LocalMapping
from SteamyLan.settings import PreferenceStore


class BindAddressTests(unittest.TestCase):
    def test_new_preferences_notify_before_installing_updates(self):
        with tempfile.TemporaryDirectory() as td:
            with mock.patch("SteamyLan.settings.user_config_dir", return_value=td):
                store = PreferenceStore()
            self.assertEqual(store.prefs.update_mode, "notify")

    def test_normalize_bind_address_accepts_ipv4_and_ipv6(self):
        self.assertEqual(PreferenceStore.normalize_bind_address(" 0.0.0.0 "), "0.0.0.0")
        self.assertEqual(PreferenceStore.normalize_bind_address("127.0.0.1"), "127.0.0.1")
        self.assertEqual(PreferenceStore.normalize_bind_address("::"), "::")
        self.assertEqual(PreferenceStore.normalize_bind_address("0:0:0:0:0:0:0:1"), "::1")
        self.assertIsNone(PreferenceStore.normalize_bind_address("localhost"))
        self.assertIsNone(PreferenceStore.normalize_bind_address("not-an-ip"))

    def test_invalid_saved_bind_address_falls_back_to_default_listener(self):
        with tempfile.TemporaryDirectory() as td:
            settings_path = Path(td) / "settings.json"
            settings_path.write_text(json.dumps({"bind_address": "bad address"}), encoding="utf-8")
            with mock.patch("SteamyLan.settings.user_config_dir", return_value=td):
                store = PreferenceStore()
            self.assertEqual(store.prefs.bind_address, "0.0.0.0")
            self.assertFalse(store.prefs.lan_discovery_compatibility)

    def test_mapping_copy_address_is_connectable_for_wildcard_bind(self):
        ipv4 = LocalMapping("a", "Game", "TCP", 25565, 25565, "0.0.0.0")
        ipv6 = LocalMapping("b", "Game", "TCP", 25565, 25566, "::")
        lan = LocalMapping("c", "Game", "UDP", 27015, 27015, "192.168.1.20")
        self.assertEqual(ipv4.address, "127.0.0.1:25565")
        self.assertEqual(ipv6.address, "[::1]:25566")
        self.assertEqual(lan.address, "192.168.1.20:27015")


if __name__ == "__main__":
    unittest.main()
