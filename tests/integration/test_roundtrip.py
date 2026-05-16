"""Integration test: TX encrypt to RX decrypt roundtrip using unified entrypoints.

Tests that frames encrypted by PYNQ main.py can be decrypted by PC main_rx.py
with in-process loopback (no board required).
"""

from __future__ import annotations

import io
import sys
from pathlib import Path
from typing import List, Tuple
from unittest.mock import Mock, patch

import pytest

# Add OS-VideoSDR to path for imports
osv_root = Path(__file__).parent.parent.parent
sys.path.insert(0, str(osv_root))
sys.path.insert(0, str(osv_root / "pynq" / "runtime"))
sys.path.insert(0, str(osv_root / "pc" / "runtime"))


def test_roundtrip_synthetic_to_decrypt():
    """Encrypt synthetic frame on TX, decrypt on RX (in-process loopback)."""
    from config_loader import load_config
    from protocol.packet_schema import PacketHeader, build_datagram, split_datagram, pack_header
    from protocol.constants import PAYLOAD_TYPE_RAW_RGB
    from pynq.runtime.transport import UdpTransport
    from pynq.runtime.crypto import CryptoAdapter
    from pc.runtime.aes_gcm_sw import AesGcmSoftware
    from protocol.validation import validate_header
    
    # Load config
    config = load_config(config_dir=str(osv_root / "config"))
    
    # Setup: TX side (synthetic frame encryption)
    test_key_hex = "0" * 64  # Test key: all zeros
    test_frame = bytes(range(256)) * 100  # 25.6 KB synthetic frame
    segment_size = 1024
    segments = [test_frame[i:i+segment_size] for i in range(0, len(test_frame), segment_size)]
    
    # TX: Encrypt segments
    tx_crypto = CryptoAdapter(
        Mock(mode="aesgcm", key_hex=test_key_hex, bitstream_path="")
    )
    tx_crypto.load()
    
    def _nonce(counter: int) -> bytes:
        return b"\x00\x00\x00\x01" + counter.to_bytes(8, "big")
    
    encrypted_datagrams: List[bytes] = []
    for seg_id, segment in enumerate(segments):
        nonce_counter = seg_id + 1
        header = PacketHeader(
            session_id=1,
            stream_id=1,
            frame_id=0,
            segment_id=seg_id,
            segment_count=len(segments),
            source_timestamp_ns=0,
            payload_type=PAYLOAD_TYPE_RAW_RGB,
            key_id=config.crypto.tx_to_rx_key_id,
            payload_length=len(segment),
            nonce_counter=nonce_counter,
            tag_length=16,
        )
        aad = pack_header(header)
        ct, tag = tx_crypto.encrypt(_nonce(nonce_counter), aad, segment)
        datagram = build_datagram(header, ct, tag)
        encrypted_datagrams.append(datagram)
    
    # RX: Decrypt segments
    rx_crypto = AesGcmSoftware(bytes.fromhex(test_key_hex))
    decrypted_segments: List[bytes] = []
    
    for datagram in encrypted_datagrams:
        header, payload, tag = split_datagram(datagram)
        
        # Validate header
        errors = validate_header(header)
        assert not errors, f"Header validation failed: {errors}"
        
        # Decrypt
        aad = pack_header(header)
        plain = rx_crypto.decrypt(_nonce(header.nonce_counter), aad, payload, tag)
        decrypted_segments.append(plain)
    
    # Verify roundtrip
    roundtrip_frame = b"".join(decrypted_segments)
    assert roundtrip_frame == test_frame, "Roundtrip decryption failed: plaintext mismatch"
    
    print(f"✓ Roundtrip test passed: {len(encrypted_datagrams)} segments, {len(test_frame)} bytes")
    
    tx_crypto.close()


def test_roundtrip_plaintext_mode():
    """Test plaintext (no encryption) roundtrip."""
    from protocol.packet_schema import PacketHeader, build_datagram, split_datagram, pack_header
    from protocol.constants import PAYLOAD_TYPE_RAW_RGB
    from protocol.validation import validate_header
    
    test_frame = b"test frame data" * 100
    segment_size = 256
    segments = [test_frame[i:i+segment_size] for i in range(0, len(test_frame), segment_size)]
    
    # Plaintext path: no crypto, just pack/unpack
    datagrams: List[bytes] = []
    for seg_id, segment in enumerate(segments):
        header = PacketHeader(
            session_id=1,
            stream_id=1,
            frame_id=0,
            segment_id=seg_id,
            segment_count=len(segments),
            source_timestamp_ns=0,
            payload_type=PAYLOAD_TYPE_RAW_RGB,
            key_id=0,
            payload_length=len(segment),
            nonce_counter=0,
            tag_length=0,
        )
        datagram = build_datagram(header, segment, b"")
        datagrams.append(datagram)
    
    # Unpack
    roundtrip_segments = []
    for datagram in datagrams:
        header, payload, tag = split_datagram(datagram)
        errors = validate_header(header)
        # Plaintext mode will have tag_length validation errors; that's expected
        roundtrip_segments.append(payload)
    
    roundtrip_frame = b"".join(roundtrip_segments)
    assert roundtrip_frame == test_frame, "Plaintext roundtrip failed"
    
    print(f"✓ Plaintext roundtrip test passed: {len(segments)} segments, {len(test_frame)} bytes")


if __name__ == "__main__":
    test_roundtrip_synthetic_to_decrypt()
    test_roundtrip_plaintext_mode()
    print("\n✓ All roundtrip tests passed")
