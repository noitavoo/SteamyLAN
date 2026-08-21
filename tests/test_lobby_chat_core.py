from __future__ import annotations

import logging
from types import SimpleNamespace
import secrets
import threading
import time
import unittest
from collections import defaultdict, deque

from SteamyLan.chat import EncryptedLobbyChat
from SteamyLan.constants import (
    LOBBY_DATA_CONFIG_KEY,
    LOBBY_DATA_MARKER_KEY,
    LOBBY_DATA_MARKER_VALUE,
    PT_CHAT_MESSAGE,
)
from SteamyLan.invite_broker import InviteBroker
from SteamyLan.lobby_code import (
    invite_proof,
    invite_request_proof,
    invite_secret_hash,
    make_invite_code,
    new_invite_secret,
    parse_invite_code,
)
from SteamyLan.models import SessionConfig, SharedServiceSpec, SharingHost
from SteamyLan.services import SessionManager
from SteamyLan.lobby_password import (
    derive_password_key,
    make_auth_payload,
    new_password_salt,
    parse_auth_payload,
    password_proof,
)
from SteamyLan.protocol import unpack_packet
from SteamyLan.tunnel import ControlLink


LOG = logging.getLogger("test")


class DelayedLobbyMetadataTests(unittest.TestCase):
    def test_join_refreshes_lobby_metadata_until_steam_cache_is_ready(self):
        config = SessionConfig(
            session_id="delayed-cache",
            host_id=1,
            host_name="Host",
            control_channel=30_101,
            chat_channel=40_101,
            services=(SharedServiceSpec("game", "Game", "UDP", 27015, 12_001),),
        )

        class DelayedSteam:
            requests = 0

            def request_lobby_data(self, _lobby_id):
                self.requests += 1
                return True

            def get_lobby_data(self, _lobby_id, key):
                if self.requests < 2:
                    return ""
                if key == LOBBY_DATA_MARKER_KEY:
                    return LOBBY_DATA_MARKER_VALUE
                if key == LOBBY_DATA_CONFIG_KEY:
                    return config.to_json()
                return ""

            @staticmethod
            def lobby_owner(_lobby_id):
                return 1

            @staticmethod
            def lobby_member_limit(_lobby_id):
                return config.max_members

            @staticmethod
            def is_immediate_friend(_steam_id):
                return True

        steam = DelayedSteam()
        manager = SimpleNamespace(steam=steam)
        host = SharingHost(
            lobby_id=99,
            host_id=1,
            host_name="Host",
            services=config.services,
            session_id=config.session_id,
        )
        loaded = SessionManager._load_join_config(
            manager,
            99,
            host,
            None,
            timeout=0.6,
            refresh_interval=0.01,
        )
        self.assertEqual(loaded, config)
        self.assertGreaterEqual(steam.requests, 2)


class PacketHub:
    def __init__(self):
        self._lock = threading.Lock()
        self._queues = defaultdict(deque)

    def send(self, sender: int, recipient: int, channel: int, raw: bytes) -> None:
        with self._lock:
            self._queues[(int(recipient), int(channel))].append((int(sender), bytes(raw)))

    def recv(self, recipient: int, channel: int):
        with self._lock:
            q = self._queues[(int(recipient), int(channel))]
            return q.popleft() if q else None


class FakeSteam:
    def __init__(self, steam_id: int, hub: PacketHub):
        self.id = int(steam_id)
        self.hub = hub
        self.sent = []
        self.invites = []

    def send_packet(self, peer_id: int, raw: bytes, channel: int, reliable: bool = True):
        self.sent.append((int(peer_id), int(channel), bytes(raw), bool(reliable)))
        self.hub.send(self.id, int(peer_id), int(channel), bytes(raw))
        return True

    def recv_packet(self, channel: int):
        return self.hub.recv(self.id, int(channel))

    def recv_packets(self, channel: int, max_messages: int = 32):
        rows = []
        for _ in range(max(1, int(max_messages))):
            item = self.recv_packet(channel)
            if item is None:
                break
            rows.append(item)
        return rows

    def invite_to_lobby(self, lobby_id: int, steam_id: int):
        self.invites.append((int(lobby_id), int(steam_id)))
        return True

    def friend_name(self, steam_id: int, refresh: bool = False):
        return {1: "Host", 2: "Alice", 3: "Bob"}.get(int(steam_id), f"User {steam_id}")


def wait_for(predicate, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


class LobbyCodeTests(unittest.TestCase):
    def test_code_round_trip_and_tamper_detection(self):
        secret = new_invite_secret()
        code = make_invite_code(109876543210, 76561198000000000, secret)
        self.assertEqual(parse_invite_code(code), (109876543210, 76561198000000000, secret))
        self.assertTrue(code.startswith("STLN-"))
        self.assertEqual(len(invite_secret_hash(secret)), 24)
        self.assertNotEqual(
            invite_request_proof(secret, 109876543210, 2),
            invite_request_proof(secret, 109876543210, 3),
        )
        self.assertNotEqual(invite_proof(secret, "session-a", 2), invite_proof(secret, "session-b", 2))
        chars = list(code)
        idx = next(i for i, ch in enumerate(chars) if i > 5 and ch not in "-")
        chars[idx] = "A" if chars[idx] != "A" else "B"
        with self.assertRaises(ValueError):
            parse_invite_code("".join(chars))

    def test_static_code_uses_zero_lobby_sentinel(self):
        secret = new_invite_secret()
        code = make_invite_code(0, 76561198000000000, secret)
        self.assertEqual(parse_invite_code(code), (0, 76561198000000000, secret))

    def test_config_validates_and_normalizes_remote_text(self):
        spec = SharedServiceSpec.from_dict(
            {"service_id": "game", "name": "  <b>Game</b>\n server  ", "protocol": "tcp", "port": 27015, "channel": 12000}
        )
        config = SessionConfig.from_json(
            SessionConfig(
                session_id="session",
                host_id=76561198000000000,
                host_name=" Host\n Name ",
                control_channel=32000,
                chat_channel=40000,
                services=(spec,),
                lobby_name=" My\n Lobby ",
                visibility="public",
                max_members=12,
            ).to_json()
        )
        self.assertEqual(config.host_name, "Host Name")
        self.assertEqual(config.lobby_name, "My Lobby")
        self.assertEqual(config.services[0].name, "<b>Game</b> server")

        raw = config.to_json().replace('"channel":12000', '"channel":32000')
        with self.assertRaises(ValueError):
            SessionConfig.from_json(raw)


class LobbyPasswordTests(unittest.TestCase):
    def test_password_proof_round_trip_and_binding(self):
        salt = new_password_salt()
        key = derive_password_key("correct horse", salt)
        proof = password_proof(key, "session-a", 2)
        payload = make_auth_payload(invite="invite-proof", password=proof)
        self.assertEqual(parse_auth_payload(payload), ("invite-proof", proof))
        self.assertEqual(make_auth_payload(invite="legacy-invite"), "legacy-invite")
        self.assertEqual(parse_auth_payload("legacy-invite"), ("legacy-invite", ""))
        self.assertNotEqual(proof, password_proof(key, "session-b", 2))
        self.assertNotEqual(proof, password_proof(key, "session-a", 3))
        self.assertNotEqual(key, derive_password_key("wrong password", salt))

    def test_password_metadata_round_trip_without_plaintext(self):
        salt = new_password_salt()
        config = SessionConfig(
            session_id="pw-session",
            host_id=76561198000000000,
            host_name="Host",
            control_channel=32001,
            chat_channel=40001,
            services=(SharedServiceSpec("svc", "Server", "TCP", 27015, 12001),),
            lobby_name="Protected",
            visibility="public",
            max_members=8,
            password_salt=salt,
        )
        serialized = config.to_json()
        self.assertNotIn("correct horse", serialized)
        decoded = SessionConfig.from_json(serialized)
        self.assertEqual(decoded.password_salt, salt)
        with self.assertRaises(ValueError):
            SessionConfig.from_json(serialized.replace(salt, "not-a-valid-salt"))



class InviteBrokerTests(unittest.TestCase):
    def test_valid_code_proof_causes_native_private_lobby_invite(self):
        hub = PacketHub()
        secret = new_invite_secret()
        host_steam = FakeSteam(1, hub)
        client_steam = FakeSteam(2, hub)
        granted = threading.Event()
        invited = []
        host = InviteBroker(
            host_steam, LOG, role="host", lobby_id=555, local_id=1, secret=secret,
            on_invited=invited.append,
        )
        client = InviteBroker(
            client_steam, LOG, role="client", lobby_id=555, local_id=2, host_id=1, secret=secret,
            on_granted=granted.set,
        )
        try:
            host.start()
            client.start()
            self.assertTrue(granted.wait(2.0))
            self.assertIn((555, 2), host_steam.invites)
            self.assertIn(2, invited)
        finally:
            client.stop()
            host.stop()

    def test_static_code_resolves_to_current_lobby(self):
        hub = PacketHub()
        dynamic_secret = new_invite_secret()
        static_secret = new_invite_secret()
        host_steam = FakeSteam(1, hub)
        client_steam = FakeSteam(2, hub)
        granted = []
        host = InviteBroker(
            host_steam, LOG, role="host", lobby_id=777, local_id=1,
            secret=dynamic_secret, static_secret=static_secret,
        )
        client = InviteBroker(
            client_steam, LOG, role="client", lobby_id=0, local_id=2, host_id=1,
            secret=static_secret, on_granted=granted.append,
        )
        try:
            host.start()
            client.start()
            self.assertTrue(wait_for(lambda: bool(granted)))
            self.assertEqual(granted, [777])
            self.assertEqual(host_steam.invites, [(777, 2)])
        finally:
            client.stop()
            host.stop()

    def test_wrong_secret_is_denied(self):
        hub = PacketHub()
        host_steam = FakeSteam(1, hub)
        client_steam = FakeSteam(3, hub)
        denied = []
        host = InviteBroker(host_steam, LOG, role="host", lobby_id=777, local_id=1, secret=new_invite_secret())
        client = InviteBroker(
            client_steam, LOG, role="client", lobby_id=777, local_id=3, host_id=1, secret=new_invite_secret(),
            on_denied=denied.append,
        )
        try:
            host.start()
            client.start()
            self.assertTrue(wait_for(lambda: bool(denied)))
            self.assertEqual(host_steam.invites, [])
        finally:
            client.stop()
            host.stop()


class ControlDisconnectTests(unittest.TestCase):
    def test_host_disconnect_reaches_client(self):
        hub = PacketHub()
        host_steam = FakeSteam(1, hub)
        client_steam = FakeSteam(2, hub)
        disconnected = []
        acked = []
        host = ControlLink(
            host_steam, LOG, role="host", channel=32123,
            on_disconnect_ack=acked.append,
        )
        client = ControlLink(
            client_steam,
            LOG,
            role="client",
            channel=32123,
            peer_id=1,
            on_disconnected=disconnected.append,
        )
        try:
            host.start()
            client.start()
            reason = "You were kicked from this SteamyLAN lobby."
            self.assertTrue(host.disconnect(2, reason))
            self.assertTrue(wait_for(lambda: disconnected == [reason]))
            self.assertTrue(wait_for(lambda: acked == [2]))
        finally:
            client.stop()
            host.stop()


class ControlHealthTests(unittest.TestCase):
    def test_heartbeat_proves_bidirectional_session_and_reports_ping(self):
        hub = PacketHub()
        host_health = []
        client_health = []
        host = ControlLink(
            FakeSteam(1, hub), LOG, role="host", channel=32124,
            on_health=lambda sid, ping, state: host_health.append((sid, ping, state)),
        )
        client = ControlLink(
            FakeSteam(2, hub), LOG, role="client", channel=32124, peer_id=1,
            on_health=lambda sid, ping, state: client_health.append((sid, ping, state)),
        )
        # Keep the test quick without weakening production timings.
        host._HEARTBEAT_INTERVAL = client._HEARTBEAT_INTERVAL = 0.05
        host._HEARTBEAT_TIMEOUT = client._HEARTBEAT_TIMEOUT = 0.5
        try:
            host.start()
            host.add_peer(2)
            client.start()
            self.assertTrue(wait_for(
                lambda: any(sid == 1 and ping >= 0 and state == "connected" for sid, ping, state in client_health)
            ))
            self.assertTrue(wait_for(
                lambda: any(sid == 2 and ping >= 0 and state == "connected" for sid, ping, state in host_health)
            ))
        finally:
            client.stop()
            host.stop()


class EncryptedChatTests(unittest.TestCase):
    def test_pairwise_encrypted_group_relay_and_replay_rejection(self):
        hub = PacketHub()
        host_steam = FakeSteam(1, hub)
        a_steam = FakeSteam(2, hub)
        b_steam = FakeSteam(3, hub)
        host_messages = []
        a_messages = []
        b_messages = []
        channel = 40123
        session = "chat-test-session"
        host = EncryptedLobbyChat(
            host_steam, LOG, role="host", channel=channel, session_id=session,
            local_id=1, local_name="Host", on_message=lambda *x: host_messages.append(x),
        )
        alice = EncryptedLobbyChat(
            a_steam, LOG, role="client", channel=channel, session_id=session,
            local_id=2, local_name="Alice", host_id=1, on_message=lambda *x: a_messages.append(x),
        )
        bob = EncryptedLobbyChat(
            b_steam, LOG, role="client", channel=channel, session_id=session,
            local_id=3, local_name="Bob", host_id=1, on_message=lambda *x: b_messages.append(x),
        )
        host.add_peer(2)
        host.add_peer(3)
        try:
            host.start()
            alice.start()
            bob.start()
            self.assertTrue(wait_for(lambda: alice.ready and bob.ready))
            self.assertTrue(alice.send_message("hello\n lobby"))
            self.assertTrue(wait_for(lambda: any(m[2] == "hello lobby" for m in host_messages)))
            self.assertTrue(wait_for(lambda: any(m[0] == 2 and m[2] == "hello lobby" for m in b_messages)))

            encrypted = None
            for peer_id, sent_channel, raw, reliable in reversed(a_steam.sent):
                parsed = unpack_packet(raw)
                if peer_id == 1 and sent_channel == channel and parsed and parsed[0] == PT_CHAT_MESSAGE:
                    encrypted = raw
                    break
            self.assertIsNotNone(encrypted)
            before = len(host_messages)
            hub.send(2, 1, channel, encrypted)
            time.sleep(0.15)
            self.assertEqual(len(host_messages), before, "replayed encrypted message must be discarded")

            self.assertTrue(host.send_message("host announcement"))
            self.assertTrue(wait_for(lambda: any(m[2] == "host announcement" for m in a_messages)))
            self.assertTrue(wait_for(lambda: any(m[2] == "host announcement" for m in b_messages)))
        finally:
            alice.stop()
            bob.stop()
            host.stop()


if __name__ == "__main__":
    unittest.main()
