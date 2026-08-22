from __future__ import annotations

import ctypes
import ipaddress
import shutil
import socket
import struct
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from SteamyLan import broadcast_helper
from SteamyLan.broadcast_helper import WinDivertAddress, close_runtime_locks, lock_verified_runtime
from SteamyLan.broadcast_redirect import (
    BroadcastRedirectError,
    build_windivert_filter,
    compatible_mapping_ports,
    normalize_ipv4_addresses,
    normalize_udp_ports,
    redirect_ipv4_broadcast_packet,
    verify_windivert_files,
    windivert_directory,
)


def udp_packet(source: str, destination: str, source_port: int, destination_port: int, payload=b"discover") -> bytearray:
    total_length = 20 + 8 + len(payload)
    ip_header = struct.pack(
        "!BBHHHBBH4s4s",
        0x45,
        0,
        total_length,
        1,
        0,
        64,
        17,
        0,
        ipaddress.IPv4Address(source).packed,
        ipaddress.IPv4Address(destination).packed,
    )
    udp_header = struct.pack("!HHHH", source_port, destination_port, 8 + len(payload), 0)
    return bytearray(ip_header + udp_header + payload)


class BroadcastRedirectTests(unittest.TestCase):
    def test_helper_fails_closed_without_administrator_permission(self):
        listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        token = "a" * 64
        result: list[int] = []

        def run_helper():
            with mock.patch("SteamyLan.broadcast_helper._is_administrator", return_value=False):
                result.append(broadcast_helper.main([
                    "--controller-port", str(listener.getsockname()[1]),
                    "--token", token,
                    "--driver-dir", str(windivert_directory()),
                ]))

        thread = threading.Thread(target=run_helper, daemon=True)
        thread.start()
        connection, peer = listener.accept()
        try:
            connection.settimeout(2.0)
            payload = b""
            while payload.count(b"\n") < 2:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                payload += chunk
            self.assertEqual(peer[0], "127.0.0.1")
            self.assertIn(b'"event":"hello"', payload)
            self.assertIn(token.encode("ascii"), payload)
            self.assertIn(b'"event":"error"', payload)
            self.assertIn(b"Administrator permission was not granted", payload)
        finally:
            connection.close()
            listener.close()
            thread.join(timeout=2.0)
        self.assertEqual(result, [5])

    def test_only_original_port_ipv4_udp_mappings_are_eligible(self):
        mappings = [
            SimpleNamespace(protocol="UDP", remote_port=42805, local_port=42805, bind_host="127.0.0.1"),
            SimpleNamespace(protocol="UDP", remote_port=27015, local_port=27016, bind_host="127.0.0.1"),
            SimpleNamespace(protocol="TCP", remote_port=42805, local_port=42805, bind_host="127.0.0.1"),
            SimpleNamespace(protocol="UDP", remote_port=1234, local_port=1234, bind_host="192.168.1.20"),
            SimpleNamespace(protocol="UDP", remote_port=7777, local_port=7777, bind_host="0.0.0.0"),
        ]
        self.assertEqual(compatible_mapping_ports(mappings), (7777, 42805))

    def test_normalization_rejects_invalid_network_values(self):
        self.assertEqual(normalize_udp_ports([27015, "27015", 0, 65536, "bad"]), (27015,))
        self.assertEqual(
            normalize_ipv4_addresses(["255.255.255.255", "192.168.1.255", "::1", "bad"]),
            ("192.168.1.255", "255.255.255.255"),
        )

    def test_filter_is_limited_to_outbound_udp_ports_and_broadcast_addresses(self):
        packet_filter = build_windivert_filter(
            [27015, 42805],
            ["255.255.255.255", "192.168.1.255"],
        )
        self.assertIn("outbound and !loopback and ip and udp", packet_filter)
        self.assertIn("udp.DstPort == 27015", packet_filter)
        self.assertIn("udp.DstPort == 42805", packet_filter)
        self.assertIn("ip.DstAddr == 255.255.255.255", packet_filter)
        self.assertNotIn("inbound", packet_filter)
        self.assertNotEqual(packet_filter, "true")

    def test_only_allowed_broadcast_packet_is_redirected_to_loopback(self):
        packet = udp_packet("192.168.1.20", "255.255.255.255", 50123, 42805)
        self.assertTrue(redirect_ipv4_broadcast_packet(packet, [42805], ["255.255.255.255"]))
        self.assertEqual(bytes(packet[12:16]), ipaddress.IPv4Address("127.0.0.1").packed)
        self.assertEqual(bytes(packet[16:20]), ipaddress.IPv4Address("127.0.0.1").packed)
        self.assertEqual(int.from_bytes(packet[20:22], "big"), 50123)
        self.assertEqual(int.from_bytes(packet[22:24], "big"), 42805)
        self.assertEqual(bytes(packet[28:]), b"discover")

        unrelated = udp_packet("192.168.1.20", "203.0.113.9", 50123, 42805)
        original = bytes(unrelated)
        self.assertFalse(redirect_ipv4_broadcast_packet(unrelated, [42805], ["255.255.255.255"]))
        self.assertEqual(bytes(unrelated), original)

    def test_official_runtime_hashes_are_pinned(self):
        dll_path, driver_path = verify_windivert_files()
        self.assertEqual(dll_path.name, "WinDivert.dll")
        self.assertEqual(driver_path.name, "WinDivert64.sys")
        self.assertEqual(ctypes.sizeof(WinDivertAddress), 80)

        with tempfile.TemporaryDirectory() as temp:
            copied = Path(temp)
            shutil.copy2(dll_path, copied / dll_path.name)
            shutil.copy2(driver_path, copied / driver_path.name)
            (copied / dll_path.name).write_bytes(b"tampered")
            with self.assertRaises(BroadcastRedirectError):
                verify_windivert_files(copied)

    @unittest.skipUnless(hasattr(ctypes, "WinDLL"), "Native runtime locks require Windows")
    def test_elevated_runtime_inputs_are_locked_after_revalidation(self):
        dll_path, handles = lock_verified_runtime(windivert_directory())
        try:
            self.assertEqual(dll_path.name, "WinDivert.dll")
            self.assertEqual(len(handles), 2)
            self.assertTrue(all(handle > 0 for handle in handles))
        finally:
            close_runtime_locks(handles)

    @unittest.skipUnless(hasattr(ctypes, "WinDLL"), "WinDivert filter compilation requires Windows")
    def test_official_dll_accepts_the_scoped_filter(self):
        dll_path = windivert_directory() / "WinDivert.dll"
        dll = ctypes.WinDLL(str(dll_path))
        compile_filter = dll.WinDivertHelperCompileFilter
        compile_filter.argtypes = [
            ctypes.c_char_p,
            ctypes.c_int,
            ctypes.c_void_p,
            ctypes.c_uint,
            ctypes.POINTER(ctypes.c_char_p),
            ctypes.POINTER(ctypes.c_uint),
        ]
        compile_filter.restype = ctypes.c_bool
        error = ctypes.c_char_p()
        position = ctypes.c_uint(0)
        packet_filter = build_windivert_filter([42805], ["255.255.255.255"])
        self.assertTrue(compile_filter(
            packet_filter.encode("ascii"), 0, None, 0,
            ctypes.byref(error), ctypes.byref(position),
        ), msg=f"filter error at {position.value}: {error.value!r}")


if __name__ == "__main__":
    unittest.main()
