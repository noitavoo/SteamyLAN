from __future__ import annotations

import base64
import hashlib
import hmac
import secrets
import struct
import zlib

_PREFIX = "STLN"
_SECRET_BYTES = 10

_PAYLOAD = struct.Struct("!QQ10s")


def new_invite_secret() -> bytes:
    return secrets.token_bytes(_SECRET_BYTES)


def invite_secret_hash(secret: bytes) -> str:
    if not isinstance(secret, (bytes, bytearray)) or len(secret) != _SECRET_BYTES:
        raise ValueError("Invalid SteamyLAN invite secret.")
    return hashlib.sha256(bytes(secret)).hexdigest()[:24]


def make_invite_code(lobby_id: int, host_id: int, secret: bytes) -> str:
    lobby_id = int(lobby_id)
    host_id = int(host_id)
    if lobby_id < 0:
        raise ValueError("Invalid Steam lobby ID.")
    if host_id <= 0:
        raise ValueError("Invalid Steam host ID.")
    if not isinstance(secret, (bytes, bytearray)) or len(secret) != _SECRET_BYTES:
        raise ValueError("Invalid SteamyLAN invite secret.")
    body = _PAYLOAD.pack(lobby_id, host_id, bytes(secret))
    checksum = (zlib.crc32(body) & 0xFFFF).to_bytes(2, "big")
    encoded = base64.b32encode(body + checksum).decode("ascii").rstrip("=")
    groups = "-".join(encoded[i:i + 4] for i in range(0, len(encoded), 4))
    return f"{_PREFIX}-{groups}"


def parse_invite_code(code: str) -> tuple[int, int, bytes]:
    compact = "".join(ch for ch in str(code or "").upper() if ch.isalnum())
    if not compact.startswith(_PREFIX):
        raise ValueError("That does not look like a SteamyLAN share code.")
    encoded = compact[len(_PREFIX):]
    expected_encoded_len = len(base64.b32encode(b"\0" * (_PAYLOAD.size + 2)).decode("ascii").rstrip("="))
    if len(encoded) != expected_encoded_len:
        raise ValueError("That SteamyLAN share code is incomplete.")
    try:
        padding = "=" * ((8 - len(encoded) % 8) % 8)
        raw = base64.b32decode(encoded + padding, casefold=True)
    except Exception as exc:
        raise ValueError("That SteamyLAN share code contains invalid characters.") from exc
    if len(raw) != _PAYLOAD.size + 2:
        raise ValueError("That SteamyLAN share code has the wrong length.")
    body, supplied = raw[:-2], raw[-2:]
    expected = (zlib.crc32(body) & 0xFFFF).to_bytes(2, "big")
    if not hmac.compare_digest(supplied, expected):
        raise ValueError("That SteamyLAN share code is not valid.")
    lobby_id, host_id, secret = _PAYLOAD.unpack(body)
    if host_id <= 0:
        raise ValueError("That SteamyLAN share code does not contain a valid lobby host.")
    return int(lobby_id), int(host_id), bytes(secret)


def invite_request_proof(secret: bytes, lobby_id: int, steam_id: int) -> str:
    """Proof used before a private-lobby Steam invite is issued."""
    if not isinstance(secret, (bytes, bytearray)) or len(secret) != _SECRET_BYTES:
        raise ValueError("Invalid SteamyLAN invite secret.")
    message = f"invite:{int(lobby_id)}:{int(steam_id)}".encode("ascii")
    return hmac.new(bytes(secret), message, hashlib.sha256).hexdigest()[:32]


def invite_proof(secret: bytes, session_id: str, steam_id: int) -> str:
    """Defense-in-depth proof repeated after the user has joined the lobby."""
    if not isinstance(secret, (bytes, bytearray)) or len(secret) != _SECRET_BYTES:
        raise ValueError("Invalid SteamyLAN invite secret.")
    message = f"{session_id}:{int(steam_id)}".encode("utf-8")
    return hmac.new(bytes(secret), message, hashlib.sha256).hexdigest()[:32]
