from __future__ import annotations

import argparse
import ctypes
import json
import os
import socket
import threading
from pathlib import Path

from .broadcast_redirect import (
    BroadcastRedirectError,
    build_windivert_filter,
    local_broadcast_addresses,
    normalize_ipv4_addresses,
    normalize_udp_ports,
    redirect_ipv4_broadcast_packet,
    verify_windivert_files,
)


INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
WINDIVERT_LAYER_NETWORK = 0
WINDIVERT_SHUTDOWN_RECV = 1
WINDIVERT_PARAM_QUEUE_LENGTH = 0
WINDIVERT_PARAM_QUEUE_TIME = 1
WINDIVERT_PARAM_QUEUE_SIZE = 2
MAX_PACKET = 65535
GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
OPEN_EXISTING = 3
FILE_ATTRIBUTE_NORMAL = 0x00000080


class WinDivertAddress(ctypes.Structure):
    _fields_ = [
        ("timestamp", ctypes.c_int64),
        ("flags", ctypes.c_uint32),
        ("reserved", ctypes.c_uint32),
        ("data", ctypes.c_ubyte * 64),
    ]


class WinDivertApi:
    def __init__(self, dll_path: Path):
        self.dll = ctypes.WinDLL(str(dll_path), use_last_error=True)
        self.dll.WinDivertOpen.argtypes = [ctypes.c_char_p, ctypes.c_int, ctypes.c_int16, ctypes.c_uint64]
        self.dll.WinDivertOpen.restype = ctypes.c_void_p
        self.dll.WinDivertRecv.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(WinDivertAddress),
        ]
        self.dll.WinDivertRecv.restype = ctypes.c_bool
        self.dll.WinDivertSend.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(WinDivertAddress),
        ]
        self.dll.WinDivertSend.restype = ctypes.c_bool
        self.dll.WinDivertShutdown.argtypes = [ctypes.c_void_p, ctypes.c_int]
        self.dll.WinDivertShutdown.restype = ctypes.c_bool
        self.dll.WinDivertClose.argtypes = [ctypes.c_void_p]
        self.dll.WinDivertClose.restype = ctypes.c_bool
        self.dll.WinDivertSetParam.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_uint64]
        self.dll.WinDivertSetParam.restype = ctypes.c_bool
        self.dll.WinDivertHelperCalcChecksums.argtypes = [
            ctypes.c_void_p, ctypes.c_uint, ctypes.POINTER(WinDivertAddress), ctypes.c_uint64,
        ]
        self.dll.WinDivertHelperCalcChecksums.restype = ctypes.c_bool

    def open(self, packet_filter: str) -> int:
        handle = self.dll.WinDivertOpen(packet_filter.encode("ascii"), WINDIVERT_LAYER_NETWORK, 0, 0)
        if handle in {None, INVALID_HANDLE_VALUE}:
            error = ctypes.get_last_error()
            messages = {
                2: "The WinDivert driver file was not found.",
                5: "Administrator permission is required for LAN discovery compatibility.",
                87: "The LAN discovery packet filter was rejected.",
                577: "Windows rejected the WinDivert driver signature.",
                1275: "Security software or Windows blocked the WinDivert driver.",
            }
            raise BroadcastRedirectError(messages.get(error, f"WinDivert could not start (Windows error {error})."))
        self.dll.WinDivertSetParam(handle, WINDIVERT_PARAM_QUEUE_LENGTH, 256)
        self.dll.WinDivertSetParam(handle, WINDIVERT_PARAM_QUEUE_TIME, 500)
        self.dll.WinDivertSetParam(handle, WINDIVERT_PARAM_QUEUE_SIZE, 1_048_576)
        return int(handle)

    def shutdown(self, handle: int) -> None:
        self.dll.WinDivertShutdown(ctypes.c_void_p(handle), WINDIVERT_SHUTDOWN_RECV)

    def close(self, handle: int) -> None:
        self.dll.WinDivertClose(ctypes.c_void_p(handle))


def lock_verified_runtime(directory: Path) -> tuple[Path, tuple[int, int]]:
    """Pin and lock the elevated process's native inputs against replacement."""
    dll_path, driver_path = verify_windivert_files(directory)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
    ]
    kernel32.CreateFileW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    handles: list[int] = []
    try:
        for path in (dll_path, driver_path):
            handle = kernel32.CreateFileW(
                str(path), GENERIC_READ, FILE_SHARE_READ, None,
                OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, None,
            )
            if handle in {None, INVALID_HANDLE_VALUE}:
                raise BroadcastRedirectError(
                    f"The LAN discovery runtime could not be locked safely (Windows error {ctypes.get_last_error()})."
                )
            handles.append(int(handle))
        # Hash again after both files are locked. They cannot now be replaced
        # between validation, DLL loading, and driver installation.
        verify_windivert_files(directory)
        return dll_path, (handles[0], handles[1])
    except Exception:
        for handle in handles:
            kernel32.CloseHandle(ctypes.c_void_p(handle))
        raise


def close_runtime_locks(handles: tuple[int, ...]) -> None:
    if not handles:
        return
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    for handle in handles:
        kernel32.CloseHandle(ctypes.c_void_p(handle))


class JsonChannel:
    def __init__(self, connection: socket.socket):
        self.connection = connection
        self._send_lock = threading.Lock()
        self._buffer = b""

    def send(self, value: dict) -> None:
        payload = json.dumps(value, separators=(",", ":")).encode("utf-8") + b"\n"
        with self._send_lock:
            self.connection.sendall(payload)

    def receive(self) -> dict | None:
        while b"\n" not in self._buffer:
            chunk = self.connection.recv(4096)
            if not chunk:
                return None
            self._buffer += chunk
            if len(self._buffer) > 65536:
                raise BroadcastRedirectError("The controller sent too much data.")
        line, self._buffer = self._buffer.split(b"\n", 1)
        value = json.loads(line.decode("utf-8"))
        if not isinstance(value, dict):
            raise BroadcastRedirectError("The controller sent an invalid command.")
        return value


class RedirectWorker:
    def __init__(self, api: WinDivertApi, channel: JsonChannel, ports, addresses):
        self.api = api
        self.channel = channel
        self.ports = normalize_udp_ports(ports)
        self.addresses = normalize_ipv4_addresses(addresses)
        self.handle = 0
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        packet_filter = build_windivert_filter(self.ports, self.addresses)
        self.handle = self.api.open(packet_filter)
        self.thread = threading.Thread(target=self._run, name="SteamyLANBroadcastRedirect", daemon=True)
        self.thread.start()
        self.channel.send({
            "event": "ready",
            "ports": list(self.ports),
            "message": "LAN discovery compatibility is active.",
        })

    def stop(self) -> None:
        handle, self.handle = self.handle, 0
        if handle:
            self.api.shutdown(handle)
        if self.thread is not None and self.thread.is_alive():
            self.thread.join(timeout=1.5)
        if handle:
            self.api.close(handle)
        self.thread = None

    def _run(self) -> None:
        packet_buffer = (ctypes.c_ubyte * MAX_PACKET)()
        handle = self.handle
        while self.handle:
            received = ctypes.c_uint(0)
            address = WinDivertAddress()
            ok = self.api.dll.WinDivertRecv(
                ctypes.c_void_p(handle), packet_buffer, MAX_PACKET,
                ctypes.byref(received), ctypes.byref(address),
            )
            if not ok:
                if self.handle:
                    error = ctypes.get_last_error()
                    try:
                        self.channel.send({"event": "error", "message": f"LAN discovery packet receive failed (Windows error {error})."})
                    except OSError:
                        pass
                    self.handle = 0
                    self.api.close(handle)
                return
            packet = bytearray(bytes(packet_buffer[: received.value]))
            original = bytes(packet)
            modified = redirect_ipv4_broadcast_packet(packet, self.ports, self.addresses)
            if modified:
                send_buffer = (ctypes.c_ubyte * len(packet)).from_buffer(packet)
                if not self.api.dll.WinDivertHelperCalcChecksums(
                    send_buffer, len(packet), ctypes.byref(address), 0
                ):
                    packet[:] = original
                    send_buffer = (ctypes.c_ubyte * len(packet)).from_buffer(packet)
            else:
                send_buffer = (ctypes.c_ubyte * len(packet)).from_buffer(packet)
            sent = ctypes.c_uint(0)
            if not self.api.dll.WinDivertSend(
                ctypes.c_void_p(handle), send_buffer, len(packet),
                ctypes.byref(sent), ctypes.byref(address),
            ) and self.handle:
                error = ctypes.get_last_error()
                try:
                    self.channel.send({"event": "error", "message": f"LAN discovery packet reinjection failed (Windows error {error})."})
                except OSError:
                    pass
                self.handle = 0
                self.api.close(handle)
                return


def _is_administrator() -> bool:
    if os.name != "nt":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--controller-port", type=int, required=True)
    parser.add_argument("--token", required=True)
    parser.add_argument("--driver-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if not 1 <= args.controller_port <= 65535:
        parser.error("invalid controller port")
    if len(args.token) != 64 or any(ch not in "0123456789abcdef" for ch in args.token.casefold()):
        parser.error("invalid controller token")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    connection = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    connection.settimeout(10.0)
    runtime_locks: tuple[int, ...] = ()
    channel: JsonChannel | None = None
    try:
        connection.connect(("127.0.0.1", args.controller_port))
        connection.settimeout(None)
        channel = JsonChannel(connection)
        channel.send({"event": "hello", "token": args.token})
        if not _is_administrator():
            channel.send({"event": "error", "message": "Administrator permission was not granted."})
            return 5
        dll_path, runtime_locks = lock_verified_runtime(args.driver_dir)
        api = WinDivertApi(dll_path)
        worker: RedirectWorker | None = None
        while True:
            command = channel.receive()
            if command is None or command.get("command") == "stop":
                break
            if command.get("command") != "set":
                continue
            if worker is not None:
                worker.stop()
                worker = None
            ports = normalize_udp_ports(command.get("ports", ()))
            requested_addresses = set(normalize_ipv4_addresses(command.get("addresses", ())))
            addresses = tuple(
                address for address in local_broadcast_addresses()
                if address in requested_addresses
            )
            if not ports or not addresses:
                channel.send({"event": "inactive", "message": "No compatible UDP mappings are open."})
                continue
            try:
                worker = RedirectWorker(api, channel, ports, addresses)
                worker.start()
            except Exception as exc:
                if worker is not None:
                    worker.stop()
                    worker = None
                channel.send({"event": "error", "message": str(exc)[:500]})
        if worker is not None:
            worker.stop()
        return 0
    except Exception as exc:
        if channel is not None:
            try:
                channel.send({"event": "error", "message": str(exc)[:500]})
            except OSError:
                pass
        return 1
    finally:
        close_runtime_locks(runtime_locks)
        connection.close()
