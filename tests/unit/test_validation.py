from __future__ import annotations

from protocol.constants import DEFAULT_TAG_LENGTH, PAYLOAD_TYPE_RAW_RGB
from protocol.packet_schema import PacketHeader
from protocol.validation import (
    is_frame_complete,
    validate_header,
    validate_nonce_monotonic,
    validate_replay_window,
)


def test_validate_header_accepts_valid_header() -> None:
    header = PacketHeader(
        session_id=1,
        stream_id=1,
        frame_id=10,
        segment_id=0,
        segment_count=2,
        source_timestamp_ns=42,
        payload_type=PAYLOAD_TYPE_RAW_RGB,
        key_id=1,
        payload_length=1024,
        nonce_counter=99,
        tag_length=DEFAULT_TAG_LENGTH,
    )

    errors = validate_header(header)
    assert errors == []


def test_validate_header_reports_multiple_errors() -> None:
    header = PacketHeader(
        segment_id=3,
        segment_count=2,
        payload_type=250,
        payload_length=50000,
        tag_length=8,
    )

    errors = validate_header(header)
    assert "segment_id out of range" in errors
    assert "unknown payload_type" in errors
    assert "payload_length exceeds configured maximum" in errors
    assert "tag_length does not match default policy" in errors


def test_nonce_monotonic_and_replay_window_rules() -> None:
    assert validate_nonce_monotonic(10, 11)
    assert not validate_nonce_monotonic(10, 10)

    assert validate_replay_window(latest_nonce=100, incoming_nonce=110, window=16)
    assert validate_replay_window(latest_nonce=100, incoming_nonce=95, window=16)
    assert not validate_replay_window(latest_nonce=100, incoming_nonce=70, window=16)


def test_is_frame_complete() -> None:
    assert is_frame_complete([0, 1, 2], segment_count=3)
    assert not is_frame_complete([0, 2], segment_count=3)
    assert not is_frame_complete([0, 1], segment_count=0)
