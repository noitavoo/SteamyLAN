from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets

_PASSWORD_SALT_BYTES = 16
_PASSWORD_KEY_BYTES = 32
_PASSWORD_MAX_CHARS = 128


def new_password_salt() -> str:
    return _b64(secrets.token_bytes(_PASSWORD_SALT_BYTES))


def validate_password_salt(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    try:
        raw = _unb64(text)
    except Exception as exc:
        raise ValueError("Invalid lobby password metadata.") from exc
    if len(raw) != _PASSWORD_SALT_BYTES:
        raise ValueError("Invalid lobby password metadata.")
    return _b64(raw)


def derive_password_key(password: str, salt: str) -> bytes:
    password = str(password or "")
    if len(password) > _PASSWORD_MAX_CHARS:
        raise ValueError(f"Lobby passwords are limited to {_PASSWORD_MAX_CHARS} characters.")
    if not password:
        raise ValueError("Enter the lobby password.")
    salt_raw = _unb64(validate_password_salt(salt))
    return hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt_raw,
        n=1 << 14,
        r=8,
        p=1,
        dklen=_PASSWORD_KEY_BYTES,
    )


def password_proof(key: bytes, session_id: str, steam_id: int) -> str:
    if len(bytes(key)) != _PASSWORD_KEY_BYTES:
        raise ValueError("Invalid lobby password key.")
    message = (
        b"SteamyLAN/password-auth/v1\x00"
        + str(session_id).encode("utf-8", "strict")
        + b"\x00"
        + str(int(steam_id)).encode("ascii")
    )
    return _b64(hmac.new(bytes(key), message, hashlib.sha256).digest())


def make_auth_payload(*, invite: str = "", password: str = "") -> str:
    invite = str(invite or "")[:160]
    password = str(password or "")[:160]
    if not invite and not password:
        return ""


    if invite and not password:
        return invite
    return json.dumps({"i": invite, "p": password}, separators=(",", ":"))


def parse_auth_payload(text: str) -> tuple[str, str]:
    """Return (invite_proof, password_proof), accepting legacy invite-only text."""
    text = str(text or "").strip()
    if not text:
        return "", ""
    if not text.startswith("{"):
        return text[:160], ""
    try:
        raw = json.loads(text)
    except Exception:
        return "", ""
    if not isinstance(raw, dict):
        return "", ""
    invite = str(raw.get("i") or "")[:160]
    password = str(raw.get("p") or "")[:160]
    return invite, password


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(bytes(raw)).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    value = str(text or "")
    return base64.urlsafe_b64decode(value + ("=" * (-len(value) % 4)))
