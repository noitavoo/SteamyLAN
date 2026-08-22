from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
from pathlib import Path
from typing import Callable, Iterable


WINDIVERT_VERSION = "2.2.2-A"
WINDIVERT_HASHES = {
    "WinDivert.dll": "c1e060ee19444a259b2162f8af0f3fe8c4428a1c6f694dce20de194ac8d7d9a2",
    "WinDivert64.sys": "8da085332782708d8767bcace5327a6ec7283c17cfb85e40b03cd2323a90ddc2",
}


class BroadcastRedirectError(RuntimeError):
    pass


def normalize_udp_ports(values: Iterable[object]) -> tuple[int, ...]:
    ports: set[int] = set()
    for value in values:
        try:
            port = int(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if 1 <= port <= 65535:
            ports.add(port)
    return tuple(sorted(ports))


def normalize_ipv4_addresses(values: Iterable[object]) -> tuple[str, ...]:
    addresses: set[ipaddress.IPv4Address] = set()
    for value in values:
        try:
            address = ipaddress.ip_address(str(value or "").strip())
        except ValueError:
            continue
        if isinstance(address, ipaddress.IPv4Address):
            addresses.add(address)
    return tuple(str(value) for value in sorted(addresses, key=int))


def compatible_mapping_ports(mappings: Iterable[object]) -> tuple[int, ...]:
    """Return only safe IPv4 UDP mappings that preserve the host's port."""
    ports: set[int] = set()
    for mapping in mappings:
        try:
            protocol = str(mapping.protocol).upper()
            remote_port = int(mapping.remote_port)
            local_port = int(mapping.local_port)
            bind_host = str(mapping.bind_host)
        except (AttributeError, TypeError, ValueError, OverflowError):
            continue
        if (
            protocol == "UDP"
            and remote_port == local_port
            and bind_host in {"127.0.0.1", "0.0.0.0"}
            and 1 <= remote_port <= 65535
        ):
            ports.add(remote_port)
    return tuple(sorted(ports))


def local_broadcast_addresses() -> tuple[str, ...]:
    """Return the limited and directed IPv4 broadcasts for local interfaces."""
    addresses: set[str] = {"255.255.255.255"}
    try:
        import psutil

        for rows in psutil.net_if_addrs().values():
            for row in rows:
                if row.family != socket.AF_INET or not row.address or not row.netmask:
                    continue
                try:
                    interface_address = ipaddress.IPv4Address(row.address)
                    network = ipaddress.IPv4Network((row.address, row.netmask), strict=False)
                except (ValueError, TypeError):
                    continue
                if not interface_address.is_loopback and network.prefixlen < 31:
                    addresses.add(str(network.broadcast_address))
    except Exception:
        pass
    return normalize_ipv4_addresses(addresses)


def build_windivert_filter(ports: Iterable[object], addresses: Iterable[object]) -> str:
    safe_ports = normalize_udp_ports(ports)
    safe_addresses = normalize_ipv4_addresses(addresses)
    if not safe_ports or not safe_addresses:
        raise ValueError("At least one UDP port and IPv4 broadcast address are required.")
    port_filter = " or ".join(f"udp.DstPort == {port}" for port in safe_ports)
    address_filter = " or ".join(f"ip.DstAddr == {address}" for address in safe_addresses)
    return (
        "outbound and !loopback and ip and udp and udp.PayloadLength > 0 and "
        f"({port_filter}) and ({address_filter})"
    )


def redirect_ipv4_broadcast_packet(
    packet: bytearray,
    ports: Iterable[object],
    addresses: Iterable[object],
) -> bool:
    """Rewrite one allowed IPv4 UDP broadcast into a loopback datagram."""
    safe_ports = set(normalize_udp_ports(ports))
    safe_addresses = set(normalize_ipv4_addresses(addresses))
    if len(packet) < 28 or packet[0] >> 4 != 4 or packet[9] != socket.IPPROTO_UDP:
        return False
    header_len = (packet[0] & 0x0F) * 4
    if header_len < 20 or len(packet) < header_len + 8:
        return False
    destination = str(ipaddress.IPv4Address(bytes(packet[16:20])))
    destination_port = int.from_bytes(packet[header_len + 2:header_len + 4], "big")
    if destination not in safe_addresses or destination_port not in safe_ports:
        return False
    loopback = ipaddress.IPv4Address("127.0.0.1").packed
    # Rewriting both endpoints is necessary on Windows: an external-interface
    # source combined with a loopback destination is rejected by the IP stack.
    # The UDP source port remains unchanged so the local game receives replies.
    packet[12:16] = loopback
    packet[16:20] = loopback
    return True


def windivert_directory() -> Path:
    if getattr(sys, "frozen", False):
        executable_dir = Path(sys.executable).resolve().parent
        candidates = [executable_dir]
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            candidates.append(Path(meipass).resolve())
        for candidate in candidates:
            if all((candidate / filename).is_file() for filename in WINDIVERT_HASHES):
                return candidate
        return executable_dir
    return Path(__file__).resolve().parents[1] / "third_party" / "windivert" / "x64"


def verify_windivert_files(directory: Path | None = None) -> tuple[Path, Path]:
    base = Path(directory or windivert_directory()).resolve()
    verified: list[Path] = []
    for filename, expected in WINDIVERT_HASHES.items():
        path = base / filename
        if not path.is_file():
            raise BroadcastRedirectError(f"The bundled LAN discovery component is missing {filename}.")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if not secrets.compare_digest(digest, expected):
            raise BroadcastRedirectError(f"The bundled LAN discovery component failed its integrity check ({filename}).")
        verified.append(path)
    return verified[0], verified[1]


def _helper_launch_command(controller_port: int, token: str, driver_dir: Path) -> tuple[str, list[str], str]:
    args = [
        "--broadcast-helper",
        "--controller-port",
        str(int(controller_port)),
        "--token",
        str(token),
        "--driver-dir",
        str(driver_dir),
    ]
    if getattr(sys, "frozen", False):
        executable = str(Path(sys.executable).resolve())
        working_dir = str(Path(executable).parent)
    else:
        executable = str(Path(sys.executable).resolve())
        run_py = Path(__file__).resolve().parents[1] / "run.py"
        args.insert(0, str(run_py))
        working_dir = str(run_py.parent)
    return executable, args, working_dir


class BroadcastRedirectorController:
    """Own the authenticated loopback IPC channel to the elevated helper."""

    def __init__(self, logger, on_status: Callable[[str, str], None] | None = None):
        self.log = logger
        self.on_status = on_status
        self._lock = threading.RLock()
        self._send_lock = threading.Lock()
        self._listener: socket.socket | None = None
        self._connection: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._token = ""
        self._ports: tuple[int, ...] = ()
        self._addresses: tuple[str, ...] = ()
        self._ready_ports: tuple[int, ...] = ()
        self._failed = False
        self._stopping = False

    @property
    def ready_ports(self) -> tuple[int, ...]:
        with self._lock:
            return self._ready_ports

    def detach(self) -> None:
        """Prevent late helper events from reaching a subsequent lobby."""
        with self._lock:
            self.on_status = None

    def update(self, ports: Iterable[object]) -> None:
        safe_ports = normalize_udp_ports(ports)
        addresses = local_broadcast_addresses()
        with self._lock:
            self._ports = safe_ports
            self._addresses = addresses
            connection = self._connection
            running = bool(self._thread and self._thread.is_alive())
            failed = self._failed
        if connection is not None:
            try:
                self._send_config(connection)
            except OSError as exc:
                self._fail(f"The LAN discovery helper disconnected: {exc}")
        elif safe_ports and not running and not failed:
            self._start()

    def stop(self) -> None:
        with self._lock:
            self._stopping = True
            connection, self._connection = self._connection, None
            listener, self._listener = self._listener, None
            thread, self._thread = self._thread, None
            self._ready_ports = ()
        if connection is not None:
            try:
                self._send_json(connection, {"command": "stop"})
            except OSError:
                pass
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            connection.close()
        if listener is not None:
            listener.close()
        if thread is not None and thread is not threading.current_thread() and thread.is_alive():
            thread.join(timeout=1.5)
        with self._lock:
            self._token = ""
            self._ports = ()
            self._addresses = ()
            self._failed = False
            self._stopping = False

    def _start(self) -> None:
        try:
            driver_dir = windivert_directory()
            verify_windivert_files(driver_dir)
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            listener.settimeout(30.0)
        except Exception as exc:
            self._fail(str(exc))
            return
        token = secrets.token_hex(32)
        with self._lock:
            if self._stopping:
                listener.close()
                return
            self._listener = listener
            self._token = token
            thread = threading.Thread(
                target=self._launch_and_serve,
                args=(listener, token, driver_dir),
                name="SteamyLANBroadcastController",
                daemon=True,
            )
            self._thread = thread
        self._status("starting", "Windows will ask permission to enable LAN discovery compatibility.")
        thread.start()

    def _launch_and_serve(self, listener: socket.socket, token: str, driver_dir: Path) -> None:
        connection = None
        try:
            executable, args, working_dir = _helper_launch_command(listener.getsockname()[1], token, driver_dir)
            parameters = subprocess.list2cmdline(args)
            result = 0
            if os.name == "nt":
                import ctypes

                result = int(ctypes.windll.shell32.ShellExecuteW(
                    None, "runas", executable, parameters, working_dir, 0
                ))
            if os.name != "nt":
                raise BroadcastRedirectError("LAN discovery compatibility is available on Windows only.")
            if result <= 32:
                if result == 5:
                    raise BroadcastRedirectError("LAN discovery permission was declined.")
                raise BroadcastRedirectError(f"Windows could not start LAN discovery compatibility (code {result}).")
            connection, peer = listener.accept()
            if peer[0] != "127.0.0.1":
                raise BroadcastRedirectError("The LAN discovery helper did not connect locally.")
            connection.settimeout(10.0)
            hello = self._read_json_line(connection)
            if hello.get("event") != "hello" or not secrets.compare_digest(str(hello.get("token", "")), token):
                raise BroadcastRedirectError("The LAN discovery helper could not be authenticated.")
            connection.settimeout(None)
            with self._lock:
                if self._stopping:
                    return
                self._connection = connection
            self._send_config(connection)
            self._read_events(connection)
            with self._lock:
                stopping = self._stopping
            if not stopping:
                raise BroadcastRedirectError("The LAN discovery helper stopped unexpectedly.")
        except (OSError, ValueError, BroadcastRedirectError) as exc:
            with self._lock:
                stopping = self._stopping
            if not stopping:
                self._fail(str(exc))
        finally:
            if connection is not None:
                connection.close()
            listener.close()
            with self._lock:
                if self._connection is connection:
                    self._connection = None
                if self._listener is listener:
                    self._listener = None
                self._ready_ports = ()

    def _send_config(self, connection: socket.socket) -> None:
        with self._lock:
            ports, addresses = self._ports, self._addresses
        self._send_json(connection, {
            "command": "set",
            "ports": list(ports),
            "addresses": list(addresses),
        })

    def _read_events(self, connection: socket.socket) -> None:
        pending = b""
        while True:
            chunk = connection.recv(4096)
            if not chunk:
                return
            pending += chunk
            if len(pending) > 65536:
                raise BroadcastRedirectError("The LAN discovery helper sent too much data.")
            while b"\n" in pending:
                line, pending = pending.split(b"\n", 1)
                if not line:
                    continue
                event = json.loads(line.decode("utf-8"))
                if not isinstance(event, dict):
                    continue
                kind = str(event.get("event", ""))
                message = str(event.get("message", ""))[:500]
                if kind == "ready":
                    ports = normalize_udp_ports(event.get("ports", ()))
                    with self._lock:
                        self._ready_ports = ports
                    self._status("ready", message)
                elif kind == "inactive":
                    with self._lock:
                        self._ready_ports = ()
                    self._status("inactive", message)
                elif kind == "error":
                    raise BroadcastRedirectError(
                        message or "LAN discovery compatibility stopped unexpectedly."
                    )

    def _fail(self, message: str) -> None:
        with self._lock:
            self._failed = True
            self._ready_ports = ()
        self.log.warning("LAN discovery compatibility unavailable: %s", message)
        self._status("error", message)

    def _status(self, kind: str, message: str) -> None:
        if self.on_status is not None:
            try:
                self.on_status(str(kind), str(message))
            except Exception:
                self.log.debug("LAN discovery status callback failed", exc_info=True)

    def _send_json(self, connection: socket.socket, value: dict) -> None:
        payload = json.dumps(value, separators=(",", ":")).encode("utf-8") + b"\n"
        if len(payload) > 65536:
            raise ValueError("LAN discovery command is too large.")
        with self._send_lock:
            connection.sendall(payload)

    @staticmethod
    def _read_json_line(connection: socket.socket) -> dict:
        payload = b""
        while b"\n" not in payload:
            chunk = connection.recv(4096)
            if not chunk:
                raise BroadcastRedirectError("The LAN discovery helper disconnected during startup.")
            payload += chunk
            if len(payload) > 65536:
                raise BroadcastRedirectError("The LAN discovery helper sent too much startup data.")
        line, _remainder = payload.split(b"\n", 1)
        value = json.loads(line.decode("utf-8"))
        if not isinstance(value, dict):
            raise BroadcastRedirectError("The LAN discovery helper sent an invalid startup response.")
        return value
