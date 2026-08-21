from __future__ import annotations

import socket
import time
from pathlib import Path


def elapsed_label(started_at: float) -> str:
    if not started_at:
        return "Running"
    seconds = max(0, int(time.time() - started_at))
    if seconds < 60:
        return "Started just now"
    minutes = seconds // 60
    if minutes < 60:
        return f"Started {minutes} min ago"
    hours = minutes // 60
    if hours < 24:
        return f"Started {hours} hr ago"
    days = hours // 24
    return f"Started {days} day{'s' if days != 1 else ''} ago"


def short_path(path: str, max_len: int = 70) -> str:
    if len(path) <= max_len:
        return path
    p = Path(path)
    tail = str(Path(p.parent.name) / p.name)
    if len(tail) + 2 <= max_len:
        return "…\\" + tail
    return "…" + path[-(max_len - 1):]


def target_host_for(local_ip: str) -> str:
    if local_ip in {"0.0.0.0", "127.0.0.1", ""}:
        return "127.0.0.1"
    if local_ip in {"::", "::0", "::1"}:
        return "::1"
    return local_ip


def bind_available(protocol: str, host: str, port: int) -> bool:
    protocol = protocol.upper()
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock_type = socket.SOCK_STREAM if protocol == "TCP" else socket.SOCK_DGRAM
    sock = socket.socket(family, sock_type)
    try:
        sock.bind((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def find_free_port(protocol: str, host: str = "127.0.0.1") -> int:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock_type = socket.SOCK_STREAM if protocol.upper() == "TCP" else socket.SOCK_DGRAM
    sock = socket.socket(family, sock_type)
    try:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def derive_peer_channel(seed: int, steam_id: int) -> int:
    """Derive a stable per-peer data channel from a lobby-advertised seed.

    This prevents multiple host-side TunnelEngine instances from competing for
    messages on one shared Steam Networking Messages channel.
    """
    import hashlib
    raw = f"SteamyLan:{int(seed)}:{int(steam_id)}".encode("ascii")
    value = int.from_bytes(hashlib.blake2s(raw, digest_size=4).digest(), "big")
    return 100_000 + (value % 1_900_000_000)
