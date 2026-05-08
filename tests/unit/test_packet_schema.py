from __future__ import annotations

import pytest

from protocol.constants import DEFAULT_TAG_LENGTH, PAYLOAD_TYPE_RAW_RGB
from protocol.packet_schema import (
    PacketHeader,
    build_datagram,
    pack_header,
    split_datagram,
    unpack_header,
)


def _sample_header() -> PacketHeader:
    return PacketHeader(
        session_id=11,
        stream_id=2,
        frame_id=33,
        segment_id=1,
        segment_count=3,
        source_timestamp_ns=123456789,
        payload_type=PAYLOAD_TYPE_RAW_RGB,
        key_id=7,
        payload_length=5,
        nonce_counter=44,
        tag_length=DEFAULT_TAG_LENGTH,
    )


def test_pack_and_unpack_round_trip() -> None:
    header = _sample_header()
    packed = pack_header(header)
    unpacked = unpack_header(packed)
    assert unpacked == header


def test_build_and_split_datagram_round_trip() -> None:
    header = _sample_header()
    payload = b"abcde"
    tag = b"\xAA" * DEFAULT_TAG_LENGTH

    datagram = build_datagram(header, payload, tag)
    got_header, got_payload, got_tag = split_datagram(datagram)

    assert got_header == header
    assert got_payload == payload
    assert got_tag == tag


def test_split_datagram_rejects_size_mismatch() -> None:
    header = _sample_header()
    payload = b"abcde"
    tag = b"\xAA" * DEFAULT_TAG_LENGTH

    datagram = build_datagram(header, payload, tag)
    truncated = datagram[:-1]

    with pytest.raises(ValueError):
        split_datagram(truncated)
