from __future__ import annotations

import logging
import queue
import socket
import threading
import time
import unittest
from unittest import mock

from SteamyLan.tunnel import TunnelEngine


LOG = logging.getLogger("test")


class PacketHub:
    def __init__(self):
        self._lock = threading.Lock()
        self._queues: dict[tuple[int, int], queue.Queue] = {}

    def _queue(self, recipient: int, channel: int) -> queue.Queue:
        key = (int(recipient), int(channel))
        with self._lock:
            return self._queues.setdefault(key, queue.Queue())

    def send(self, sender: int, recipient: int, channel: int, raw: bytes) -> None:
        self._queue(recipient, channel).put((int(sender), bytes(raw)))

    def recv(self, recipient: int, channel: int, limit: int):
        q = self._queue(recipient, channel)
        rows = []
        for _ in range(max(1, int(limit))):
            try:
                rows.append(q.get_nowait())
            except queue.Empty:
                break
        return rows


class FakeSteam:
    def __init__(self, steam_id: int, hub: PacketHub):
        self.id = int(steam_id)
        self.hub = hub

    def send_packet(self, peer_id: int, raw: bytes, channel: int, reliable: bool = True):
        self.hub.send(self.id, int(peer_id), int(channel), bytes(raw))
        return True

    def recv_packets(self, channel: int, max_messages: int = 32):
        return self.hub.recv(self.id, int(channel), max_messages)


class TunnelForwardingTests(unittest.TestCase):
    def test_tcp_preserves_first_packet_while_host_target_is_connecting(self):
        hub = PacketHub()
        host_steam = FakeSteam(1, hub)
        client_steam = FakeSteam(2, hub)

        target = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        target.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        target.bind(("127.0.0.1", 0))
        target.listen(4)
        target_port = int(target.getsockname()[1])
        target_stop = threading.Event()

        def echo_server():
            while not target_stop.is_set():
                try:
                    target.settimeout(0.2)
                    conn, _ = target.accept()
                except socket.timeout:
                    continue
                except OSError:
                    return
                try:
                    conn.settimeout(1.0)
                    while not target_stop.is_set():
                        try:
                            data = conn.recv(4096)
                        except socket.timeout:
                            continue
                        if not data:
                            break
                        conn.sendall(data)
                finally:
                    conn.close()

        thread = threading.Thread(target=echo_server, daemon=True)
        thread.start()

        local_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        local_probe.bind(("127.0.0.1", 0))
        local_port = int(local_probe.getsockname()[1])
        local_probe.close()

        host = TunnelEngine(
            host_steam,
            LOG,
            role="host",
            protocol="TCP",
            peer_id=2,
            channel=12345,
            target_host="127.0.0.1",
            target_port=target_port,
        )
        client = TunnelEngine(
            client_steam,
            LOG,
            role="client",
            protocol="TCP",
            peer_id=1,
            channel=12345,
            target_host="127.0.0.1",
            target_port=target_port,
            bind_host="0.0.0.0",
            bind_port=local_port,
        )

        original_create_connection = socket.create_connection

        def delayed_create_connection(address, timeout=None, source_address=None, *, all_errors=False):
            if tuple(address) == ("127.0.0.1", target_port):
                time.sleep(0.20)
            return original_create_connection(
                address,
                timeout=timeout,
                source_address=source_address,
                all_errors=all_errors,
            )

        try:
            with mock.patch("SteamyLan.tunnel.socket.create_connection", side_effect=delayed_create_connection):
                host.start()
                client.start()
                local = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                local.settimeout(2.0)
                local.connect(("127.0.0.1", local_port))
                local.sendall(b"first-packet")
                self.assertEqual(local.recv(4096), b"first-packet")
                local.close()
        finally:
            client.stop()
            host.stop()
            target_stop.set()
            target.close()
            thread.join(timeout=0.5)


    def test_tcp_flushes_final_server_data_before_remote_close(self):
        hub = PacketHub()
        host_steam = FakeSteam(1, hub)
        client_steam = FakeSteam(2, hub)

        target = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        target.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        target.bind(("127.0.0.1", 0))
        target.listen(1)
        target_port = int(target.getsockname()[1])

        def one_shot_server():
            try:
                conn, _ = target.accept()
                with conn:
                    conn.recv(4096)
                    conn.sendall(b"final-response")
            except OSError:
                pass

        thread = threading.Thread(target=one_shot_server, daemon=True)
        thread.start()

        local_probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        local_probe.bind(("127.0.0.1", 0))
        local_port = int(local_probe.getsockname()[1])
        local_probe.close()

        host = TunnelEngine(
            host_steam, LOG, role="host", protocol="TCP", peer_id=2, channel=12347,
            target_host="127.0.0.1", target_port=target_port,
        )
        client = TunnelEngine(
            client_steam, LOG, role="client", protocol="TCP", peer_id=1, channel=12347,
            target_host="127.0.0.1", target_port=target_port,
            bind_host="127.0.0.1", bind_port=local_port,
        )

        local = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        local.settimeout(2.0)
        try:
            host.start()
            client.start()
            local.connect(("127.0.0.1", local_port))
            local.sendall(b"request")
            chunks = []
            while True:
                data = local.recv(4096)
                if not data:
                    break
                chunks.append(data)
            self.assertEqual(b"".join(chunks), b"final-response")
        finally:
            local.close()
            client.stop()
            host.stop()
            target.close()
            thread.join(timeout=0.5)

    def test_udp_round_trip(self):
        hub = PacketHub()
        host_steam = FakeSteam(1, hub)
        client_steam = FakeSteam(2, hub)

        target = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        target.bind(("127.0.0.1", 0))
        target_port = int(target.getsockname()[1])
        target_stop = threading.Event()

        def echo_server():
            target.settimeout(0.2)
            while not target_stop.is_set():
                try:
                    data, addr = target.recvfrom(65535)
                except socket.timeout:
                    continue
                except OSError:
                    return
                target.sendto(data, addr)

        thread = threading.Thread(target=echo_server, daemon=True)
        thread.start()

        local_probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        local_probe.bind(("127.0.0.1", 0))
        local_port = int(local_probe.getsockname()[1])
        local_probe.close()

        host = TunnelEngine(
            host_steam,
            LOG,
            role="host",
            protocol="UDP",
            peer_id=2,
            channel=12346,
            target_host="127.0.0.1",
            target_port=target_port,
        )
        client = TunnelEngine(
            client_steam,
            LOG,
            role="client",
            protocol="UDP",
            peer_id=1,
            channel=12346,
            target_host="127.0.0.1",
            target_port=target_port,
            bind_host="0.0.0.0",
            bind_port=local_port,
        )

        local = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        local.settimeout(2.0)
        try:
            host.start()
            client.start()
            local.sendto(b"udp-first-packet", ("127.0.0.1", local_port))
            data, _ = local.recvfrom(4096)
            self.assertEqual(data, b"udp-first-packet")
        finally:
            local.close()
            client.stop()
            host.stop()
            target_stop.set()
            target.close()
            thread.join(timeout=0.5)


if __name__ == "__main__":
    unittest.main()
