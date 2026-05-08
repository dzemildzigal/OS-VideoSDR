"""Packet header schema and datagram packing helpers."""

from __future__ import annotations

from dataclasses import dataclass
import struct
from typing import Tuple

from .constants import HEADER_SIZE_BYTES, HEADER_STRUCT_FORMAT, MAGIC, VERSION

HEADER_STRUCT = struct.Struct(HEADER_STRUCT_FORMAT)
if HEADER_STRUCT.size != HEADER_SIZE_BYTES:
    raise RuntimeError(
        f"Header size mismatch: expected {HEADER_SIZE_BYTES}, got {HEADER_STRUCT.size}"
    )


@dataclass(slots=True)
class PacketHeader:
    magic: int = MAGIC
    version: int = VERSION
    flags: int = 0
    session_id: int = 0
    stream_id: int = 0
    frame_id: int = 0
    segment_id: int = 0
    segment_count: int = 1
    source_timestamp_ns: int = 0
    payload_type: int = 0
    key_id: int = 0
    payload_length: int = 0
    nonce_counter: int = 0
    tag_length: int = 16
    reserved: int = 0


def pack_header(header: PacketHeader) -> bytes:
    return HEADER_STRUCT.pack(
        header.magic,
        header.version,
        header.flags,
        header.session_id,
        header.stream_id,
        header.frame_id,
        header.segment_id,
        header.segment_count,
        header.source_timestamp_ns,
        header.payload_type,
        header.key_id,
        header.payload_length,
        header.nonce_counter,
        header.tag_length,
        header.reserved,
    )


def unpack_header(buf: bytes) -> PacketHeader:
    if len(buf) < HEADER_SIZE_BYTES:
        raise ValueError(
            f"Buffer too small for header: need {HEADER_SIZE_BYTES}, got {len(buf)}"
        )

    values = HEADER_STRUCT.unpack_from(buf, 0)
    return PacketHeader(
        magic=values[0],
        version=values[1],
        flags=values[2],
        session_id=values[3],
        stream_id=values[4],
        frame_id=values[5],
        segment_id=values[6],
        segment_count=values[7],
        source_timestamp_ns=values[8],
        payload_type=values[9],
        key_id=values[10],
        payload_length=values[11],
        nonce_counter=values[12],
        tag_length=values[13],
        reserved=values[14],
    )


def build_datagram(header: PacketHeader, payload: bytes, tag: bytes) -> bytes:
    if len(payload) != header.payload_length:
        raise ValueError(
            f"Payload length mismatch: header={header.payload_length}, actual={len(payload)}"
        )
    if len(tag) != header.tag_length:
        raise ValueError(f"Tag length mismatch: header={header.tag_length}, actual={len(tag)}")

    return pack_header(header) + payload + tag


def split_datagram(datagram: bytes) -> Tuple[PacketHeader, bytes, bytes]:
    header = unpack_header(datagram)
    payload_start = HEADER_SIZE_BYTES
    payload_end = payload_start + header.payload_length
    expected_size = payload_end + header.tag_length

    if len(datagram) != expected_size:
        raise ValueError(
            f"Datagram size mismatch: expected {expected_size}, got {len(datagram)}"
        )

    payload = datagram[payload_start:payload_end]
    tag = datagram[payload_end:expected_size]
    return header, payload, tag
