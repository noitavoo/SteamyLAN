from __future__ import annotations

import struct
from .constants import MAGIC, MAX_STEAM_PACKET, PROTOCOL_VERSION

HEADER = struct.Struct("!4sBBBBII")


def pack_packet(packet_type: int, proto: int, stream_id: int, payload: bytes = b"") -> bytes:
    if not isinstance(payload, (bytes, bytearray)):
        raise TypeError("payload must be bytes")
    max_payload = MAX_STEAM_PACKET - HEADER.size
    if len(payload) > max_payload:
        raise ValueError(f"Payload is too large ({len(payload)} > {max_payload}).")
    return HEADER.pack(
        MAGIC, PROTOCOL_VERSION, int(packet_type), int(proto), 0,
        int(stream_id) & 0xFFFFFFFF, len(payload),
    ) + bytes(payload)


def unpack_packet(data: bytes):
    if len(data) < HEADER.size:
        return None
    magic, version, packet_type, proto, _reserved, stream_id, length = HEADER.unpack_from(data)
    if magic != MAGIC or version != PROTOCOL_VERSION or _reserved != 0:
        return None
    if length != len(data) - HEADER.size:
        return None
    return packet_type, proto, stream_id, data[HEADER.size:]
