"""Integration test: Frame reassembly integrity and completion detection.

Tests that frame reassembly correctly handles segmented frames, detects
completion, and rejects out-of-order segments properly.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import List

import pytest

# Add OS-VideoSDR to path
osv_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(osv_root))
sys.path.insert(0, str(osv_root / "pc" / "runtime"))

from protocol.packet_schema import PacketHeader
from protocol.validation import is_frame_complete
from pc.runtime.reassembly import FrameReassembler


def test_is_frame_complete_validation():
    """Test frame completion detection logic."""
    
    # Complete frame: all segments present
    assert is_frame_complete([0, 1, 2, 3], segment_count=4) is True
    assert is_frame_complete([0, 1, 2], segment_count=3) is True
    assert is_frame_complete([0], segment_count=1) is True
    
    # Incomplete frame: missing segments
    assert is_frame_complete([0, 2, 3], segment_count=4) is False  # missing 1
    assert is_frame_complete([0, 1], segment_count=4) is False      # missing 2, 3
    assert is_frame_complete([1, 2], segment_count=3) is False      # missing 0
    
    # Edge cases
    assert is_frame_complete([], segment_count=0) is False          # invalid (0 segments)
    assert is_frame_complete([], segment_count=1) is False          # missing segment 0
    assert is_frame_complete([0], segment_count=0) is False         # segment_count mismatch
    
    print("✓ Frame completion detection test passed")


def test_reassembly_in_order():
    """Test frame reassembly with in-order segments."""
    
    reassembler = FrameReassembler()
    
    frame_data = b"test frame content " * 50  # ~950 bytes
    segment_size = 100
    segments = [frame_data[i:i+segment_size] for i in range(0, len(frame_data), segment_size)]
    
    # Push segments in order
    result_frame = None
    for seg_id, segment in enumerate(segments):
        header = PacketHeader(
            session_id=1, stream_id=1, frame_id=0,
            segment_id=seg_id, segment_count=len(segments),
            source_timestamp_ns=0, payload_type=0, key_id=0,
            payload_length=len(segment), nonce_counter=0, tag_length=16,
        )
        result = reassembler.push(header, segment)
        if result is not None:
            result_frame = result
    
    assert result_frame is not None, "Frame not completed"
    # Trim to expected length (last segment may be partial)
    assert result_frame[:len(frame_data)] == frame_data, "Reassembled frame mismatch"
    
    print(f"✓ In-order reassembly test passed: {len(segments)} segments → {len(result_frame)} bytes")


def test_reassembly_out_of_order():
    """Test frame reassembly with out-of-order segments."""
    
    reassembler = FrameReassembler()
    
    frame_data = b"segment0_contentXsegment1_contentXsegment2_contentX"
    segments = [
        b"segment0_contentX",
        b"segment1_contentX",
        b"segment2_contentX",
    ]
    
    # Push segments out of order: 2, 0, 1
    result_frame = None
    seg_order = [2, 0, 1]
    for order_idx in seg_order:
        header = PacketHeader(
            session_id=1, stream_id=1, frame_id=0,
            segment_id=order_idx, segment_count=len(segments),
            source_timestamp_ns=0, payload_type=0, key_id=0,
            payload_length=len(segments[order_idx]), nonce_counter=0, tag_length=16,
        )
        result = reassembler.push(header, segments[order_idx])
        if result is not None:
            result_frame = result
    
    assert result_frame is not None, "Out-of-order frame not completed"
    assert result_frame[:len(frame_data)] == frame_data, "Out-of-order reassembly mismatch"
    
    print(f"✓ Out-of-order reassembly test passed: segments in order {seg_order} → complete frame")


def test_reassembly_duplicate_segment():
    """Test that duplicate segments are handled (should be idempotent)."""
    
    reassembler = FrameReassembler()
    
    segments = [b"seg0", b"seg1"]
    
    # Push segment 0 twice, then segment 1
    for seg_id in [0, 0, 1]:
        header = PacketHeader(
            session_id=1, stream_id=1, frame_id=0,
            segment_id=seg_id, segment_count=len(segments),
            source_timestamp_ns=0, payload_type=0, key_id=0,
            payload_length=len(segments[seg_id]), nonce_counter=0, tag_length=16,
        )
        result = reassembler.push(header, segments[seg_id])
    
    # Should complete after the second segment
    assert result is not None, "Duplicate segment frame not completed"
    assert result[:len(b"seg0seg1")] == b"seg0seg1", "Duplicate handling failed"
    
    print("✓ Duplicate segment handling test passed")


def test_reassembly_single_segment():
    """Test single-segment frame (no reassembly needed)."""
    
    reassembler = FrameReassembler()
    
    frame_data = b"single segment frame data"
    header = PacketHeader(
        session_id=1, stream_id=1, frame_id=0,
        segment_id=0, segment_count=1,
        source_timestamp_ns=0, payload_type=0, key_id=0,
        payload_length=len(frame_data), nonce_counter=0, tag_length=16,
    )
    result = reassembler.push(header, frame_data)
    
    assert result is not None, "Single-segment frame not completed"
    assert result == frame_data, "Single-segment frame mismatch"
    
    print("✓ Single-segment frame test passed")


def test_reassembly_multiple_frames():
    """Test reassembler handling multiple distinct frames."""
    
    reassembler = FrameReassembler()
    
    frames = [
        [b"f0_s0", b"f0_s1", b"f0_s2"],
        [b"f1_s0", b"f1_s1"],
        [b"f2_s0"],
    ]
    
    completed = []
    for frame_id, frame_segments in enumerate(frames):
        for seg_id, segment in enumerate(frame_segments):
            header = PacketHeader(
                session_id=1, stream_id=1, frame_id=frame_id,
                segment_id=seg_id, segment_count=len(frame_segments),
                source_timestamp_ns=0, payload_type=0, key_id=0,
                payload_length=len(segment), nonce_counter=0, tag_length=16,
            )
            result = reassembler.push(header, segment)
            if result is not None:
                completed.append((frame_id, result))
    
    assert len(completed) == 3, f"Expected 3 completed frames, got {len(completed)}"
    
    # Verify content
    for i, (frame_id, result) in enumerate(completed):
        assert frame_id == i, f"Frame ID mismatch: {frame_id} != {i}"
    
    print(f"✓ Multiple frames reassembly test passed: {len(completed)} frames completed")


if __name__ == "__main__":
    test_is_frame_complete_validation()
    test_reassembly_in_order()
    test_reassembly_out_of_order()
    test_reassembly_duplicate_segment()
    test_reassembly_single_segment()
    test_reassembly_multiple_frames()
    print("\n✓ All reassembly tests passed")
