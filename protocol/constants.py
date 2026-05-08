"""Shared protocol constants for OS-VideoSDR."""

from __future__ import annotations

MAGIC = 0x4F56
VERSION = 0

# Header format (network byte order):
# magic, version, flags, session_id, stream_id, frame_id,
# segment_id, segment_count, source_timestamp_ns,
# payload_type, key_id, payload_length,
# nonce_counter, tag_length, reserved
HEADER_STRUCT_FORMAT = "!HBBIHIHHQBBHQBB"
HEADER_SIZE_BYTES = 40

DEFAULT_TAG_LENGTH = 16
MAX_PAYLOAD_BYTES = 1200

PAYLOAD_TYPE_RAW_RGB = 1
PAYLOAD_TYPE_RAW_YUV = 2
PAYLOAD_TYPE_H264 = 3

VALID_PAYLOAD_TYPES = {
    PAYLOAD_TYPE_RAW_RGB,
    PAYLOAD_TYPE_RAW_YUV,
    PAYLOAD_TYPE_H264,
}

FLAG_KEYFRAME = 0x01
FLAG_END_OF_FRAME = 0x02
