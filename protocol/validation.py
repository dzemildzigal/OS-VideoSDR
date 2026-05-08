"""Validation helpers for packet headers, nonces, and reassembly behavior."""

from __future__ import annotations

from typing import Iterable, List

from .constants import (
    DEFAULT_TAG_LENGTH,
    MAGIC,
    MAX_PAYLOAD_BYTES,
    VALID_PAYLOAD_TYPES,
    VERSION,
)
from .packet_schema import PacketHeader


def validate_header(header: PacketHeader) -> List[str]:
    errors: List[str] = []

    if header.magic != MAGIC:
        errors.append("invalid magic")
    if header.version != VERSION:
        errors.append("unsupported protocol version")
    if header.segment_count == 0:
        errors.append("segment_count must be > 0")
    if header.segment_id >= header.segment_count:
        errors.append("segment_id out of range")
    if header.payload_type not in VALID_PAYLOAD_TYPES:
        errors.append("unknown payload_type")
    if header.payload_length > MAX_PAYLOAD_BYTES:
        errors.append("payload_length exceeds configured maximum")
    if header.tag_length <= 0:
        errors.append("tag_length must be > 0")
    if header.tag_length > 32:
        errors.append("tag_length too large")
    if header.tag_length != DEFAULT_TAG_LENGTH:
        errors.append("tag_length does not match default policy")

    return errors


def validate_nonce_monotonic(last_nonce: int, current_nonce: int) -> bool:
    return current_nonce > last_nonce


def validate_replay_window(latest_nonce: int, incoming_nonce: int, window: int) -> bool:
    if incoming_nonce > latest_nonce:
        return True
    return (incoming_nonce + window) > latest_nonce


def is_frame_complete(received_segment_ids: Iterable[int], segment_count: int) -> bool:
    if segment_count <= 0:
        return False

    seen = set(received_segment_ids)
    if len(seen) != segment_count:
        return False

    for idx in range(segment_count):
        if idx not in seen:
            return False

    return True
