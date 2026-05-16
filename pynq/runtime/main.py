"""Unified PYNQ runtime orchestrator.

V1 focus: TX path for HDMI-in/synthetic ingest -> AES encrypt -> UDP.
Uses unified config loader to pull network and crypto settings from YAML.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path
from typing import Iterator, Optional

import yaml

from protocol.constants import PAYLOAD_TYPE_RAW_RGB
from protocol.packet_schema import PacketHeader, build_datagram, pack_header

# Ensure config_loader is accessible from both direct and module execution
config_loader_candidates = [
    Path(__file__).parent.parent.parent / "config_loader.py",
    Path.cwd() / "config_loader.py",
]
for candidate in config_loader_candidates:
    if candidate.exists():
        sys.path.insert(0, str(candidate.parent))
        break

try:
    from config_loader import load_config, SessionConfig
except ImportError:
    raise RuntimeError(
        "config_loader module not found; ensure OS-VideoSDR/config_loader.py exists"
    )

try:
    from .crypto import CryptoAdapter
    from .transport import UdpTransport
except ImportError:
    from crypto import CryptoAdapter
    from transport import UdpTransport


def _segment(frame: bytes, segment_bytes: int) -> Iterator[bytes]:
    for idx in range(0, len(frame), segment_bytes):
        yield frame[idx : idx + segment_bytes]


def _nonce(counter: int) -> bytes:
    # 96-bit nonce: 32-bit domain prefix + 64-bit monotonic counter.
    # Must match PC-side generation in main_rx.py for decrypt validation.
    return b"\x00\x00\x00\x01" + counter.to_bytes(8, "big")


def _synthetic_frame(frame_bytes: int, frame_id: int) -> bytes:
    seed = frame_id & 0xFF
    return bytes(((seed + i) & 0xFF) for i in range(frame_bytes))


def _load_hdmi_source() -> Optional[object]:
    """Load HDMI capture adapter for board runtime.
    
    Returns:
        HdmiCapture instance configured for V1 capture, or None if not available.
        
    Raises:
        RuntimeError: If overlay loading fails (bitstream missing, wrong IP names, etc.)
    """
    try:
        from .hdmi_capture import HdmiCapture, HdmiCaptureConfig
    except ImportError:
        raise ImportError("hdmi_capture module not found; check pynq/runtime/hdmi_capture.py")
    
    # V1 default: 1080p grayscale capture
    cfg = HdmiCaptureConfig(
        width=1920,
        height=1080,
        fps=30,
        pixel_format="GRAY8",
        bitstream_path="hdmi_capture_wrapper.bit"
    )
    
    try:
        return HdmiCapture(cfg)
    except RuntimeError as exc:
        raise RuntimeError(
            f"HDMI ingest initialization failed: {exc}\n"
            "Troubleshooting:\n"
            "  1. Ensure bitstream file exists at configured path\n"
            "  2. Check overlay.bit and overlay.hwh have matching basenames\n"
            "  3. Verify IP names (expected: video.hdmi_in or hdmi_in) match your design"
        ) from exc


class NonceTracker:
    """Enforces monotonic nonce counter and validates key-id policy."""
    
    def __init__(self, initial_counter: int = 1, max_counter: int = 2**64 - 1):
        self.current = initial_counter - 1  # Will increment to initial on first call
        self.max_counter = max_counter
        self.wrap_count = 0
    
    def next(self) -> int:
        """Get next monotonic nonce counter."""
        self.current += 1
        if self.current > self.max_counter:
            self.wrap_count += 1
            self.current = 1
        return self.current


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="OS-VideoSDR unified PYNQ runtime",
        epilog="Config files (network.yaml, crypto.yaml) loaded from ./config/ by default."
    )
    parser.add_argument("--config-dir", default="config", help="Config directory (default: ./config/)")
    parser.add_argument("--source", choices=["synthetic", "hdmi"], default="synthetic",
                       help="Frame source (default: synthetic)")
    parser.add_argument("--crypto-mode", choices=["none", "aesgcm", "dma"], default="dma",
                       help="Crypto mode: none (plaintext), aesgcm (software), dma (hardware)")
    parser.add_argument("--bitstream", default="aes_gcm_dma_wrapper.bit",
                       help="Bitstream path for DMA mode")
    parser.add_argument("--key-hex", default=os.environ.get("OSV_AES_KEY_HEX", ""),
                       help="AES-256 key (hex), or use OSV_AES_KEY_HEX env var")
    parser.add_argument("--frames", type=int, default=120,
                       help="Number of frames to transmit")
    parser.add_argument("--fps", type=int, default=15,
                       help="Target FPS for frame generation")
    parser.add_argument("--frame-bytes", type=int, default=120000,
                       help="Synthetic frame size in bytes")
    parser.add_argument("--segment-bytes", type=int, default=1200,
                       help="Segment size for datagram payload")
    parser.add_argument("--session-id", type=int, default=1,
                       help="Session ID in packet header")
    parser.add_argument("--stream-id", type=int, default=1,
                       help="Stream ID in packet header")
    return parser.parse_args()


def run_tx(args: argparse.Namespace, config: SessionConfig) -> None:
    """Execute unified TX orchestrator.
    
    Args:
        args: CLI arguments
        config: Loaded SessionConfig with network and crypto settings
        
    Raises:
        ValueError: If required parameters are missing or invalid
        FileNotFoundError: If DMA bitstream not found
        RuntimeError: If hardware initialization fails
    """
    # Validate source selection
    if args.source == "hdmi":
        hdmi_source = _load_hdmi_source()
        if hdmi_source is None:
            raise NotImplementedError(
                "HDMI ingest adapter not yet wired; use --source synthetic for V1 bring-up"
            )
    else:
        hdmi_source = None
    if args.crypto_mode != "none" and not args.key_hex:
        raise ValueError(
            "Crypto mode '{mode}' requires --key-hex (or OSV_AES_KEY_HEX env var); "
            "use --crypto-mode none for plaintext testing".format(mode=args.crypto_mode)
        )
    
    # Validate key size for AES-256
    if args.key_hex:
        try:
            key_bytes = bytes.fromhex(args.key_hex)
        except ValueError as exc:
            raise ValueError(f"--key-hex must be valid hex: {exc}") from exc
        if len(key_bytes) != 32:
            raise ValueError(
                f"AES-256 requires 32-byte key; got {len(key_bytes)} bytes "
                f"({len(args.key_hex)} hex chars). Use 64-char hex string."
            )
    
    # Initialize transport with config
    transport = UdpTransport(
        bind_ip=config.network.bind_ip,
        bind_port=config.network.rx_port,
        send_ip=config.network.tx_ip,
        send_port=config.network.tx_port,
    )
    
    # Initialize crypto with config
    from dataclasses import dataclass
    
    @dataclass(slots=True)
    class CryptoAdapterConfig:
        mode: str
        key_hex: str
        bitstream_path: str = ""
    
    crypto = CryptoAdapter(
        CryptoAdapterConfig(
            mode=args.crypto_mode,
            key_hex=args.key_hex,
            bitstream_path=args.bitstream
        )
    )
    
    try:
        crypto.load()
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"Bitstream load failed; check --bitstream path: {exc}"
        ) from exc
    except RuntimeError as exc:
        raise RuntimeError(
            f"Hardware initialization failed (overlay/IP names correct?): {exc}"
        ) from exc
    
    # Initialize nonce tracker
    nonce_tracker = NonceTracker(initial_counter=1)
    
    # Validate key_id from config
    key_id = config.crypto.tx_to_rx_key_id
    if not (0 < key_id < 256):
        raise ValueError(
            f"Invalid tx_to_rx_key_id from config: {key_id}; must be 0 < id < 256"
        )
    
    # Setup frame source iterator
    if args.source == "hdmi":
        frame_source = hdmi_source.frames()
        frame_iterator = None
    else:
        # Synthetic source: yield indefinitely
        def synthetic_iterator():
            for frame_id in range(args.frames):
                frame = _synthetic_frame(args.frame_bytes, frame_id)
                yield (frame, frame_id)
        
        frame_source = None
        frame_iterator = synthetic_iterator()
    
    frame_interval_s = 1.0 / max(args.fps, 1)
    
    print(f"TX config: source={args.source} crypto={args.crypto_mode} "
          f"frames={args.frames} fps={args.fps} key_id={key_id}")
    print(f"Network: tx_ip={config.network.tx_ip}:{config.network.tx_port}")
    
    try:
        frame_id = 0
        
        if args.source == "hdmi":
            # HDMI source: iterate over readframe() calls indefinitely
            for frame in frame_source:
                if frame_id >= args.frames:
                    break
                
                # Segment frame
                segments = list(_segment(frame, args.segment_bytes))
                seg_count = len(segments)
                
                frame_start = time.perf_counter()
                for seg_id, segment in enumerate(segments):
                    # Generate monotonic nonce
                    nonce_counter = nonce_tracker.next()
                    
                    # Build header with enforced key_id
                    header = PacketHeader(
                        session_id=args.session_id,
                        stream_id=args.stream_id,
                        frame_id=frame_id,
                        segment_id=seg_id,
                        segment_count=seg_count,
                        source_timestamp_ns=time.time_ns(),
                        payload_type=PAYLOAD_TYPE_RAW_RGB,
                        key_id=key_id,
                        payload_length=len(segment),
                        nonce_counter=nonce_counter,
                        tag_length=16,
                    )
                    
                    # Encrypt and send
                    aad = pack_header(header)
                    ct, tag = crypto.encrypt(_nonce(nonce_counter), aad, segment)
                    datagram = build_datagram(header, ct, tag)
                    transport.send(datagram)
                
                elapsed = time.perf_counter() - frame_start
                sleep_s = frame_interval_s - elapsed
                if sleep_s > 0:
                    time.sleep(sleep_s)
                
                print(f"TX frame {frame_id + 1}/{args.frames} segments={seg_count} nonce_max={nonce_tracker.current} source=hdmi")
                frame_id += 1
        
        else:
            # Synthetic source: iterate over frame_iterator
            for frame, fid in frame_iterator:
                # Segment frame
                segments = list(_segment(frame, args.segment_bytes))
                seg_count = len(segments)
                
                frame_start = time.perf_counter()
                for seg_id, segment in enumerate(segments):
                    # Generate monotonic nonce
                    nonce_counter = nonce_tracker.next()
                    
                    # Build header with enforced key_id
                    header = PacketHeader(
                        session_id=args.session_id,
                        stream_id=args.stream_id,
                        frame_id=fid,
                        segment_id=seg_id,
                        segment_count=seg_count,
                        source_timestamp_ns=time.time_ns(),
                        payload_type=PAYLOAD_TYPE_RAW_RGB,
                        key_id=key_id,
                        payload_length=len(segment),
                        nonce_counter=nonce_counter,
                        tag_length=16,
                    )
                    
                    # Encrypt and send
                    aad = pack_header(header)
                    ct, tag = crypto.encrypt(_nonce(nonce_counter), aad, segment)
                    datagram = build_datagram(header, ct, tag)
                    transport.send(datagram)
                
                elapsed = time.perf_counter() - frame_start
                sleep_s = frame_interval_s - elapsed
                if sleep_s > 0:
                    time.sleep(sleep_s)
                
                print(f"TX frame {fid + 1}/{args.frames} segments={seg_count} nonce_max={nonce_tracker.current} source=synthetic")
    
    finally:
        crypto.close()
        transport.close()
        print("TX complete")


def main() -> None:
    args = parse_args()
    
    # Load unified config
    try:
        config = load_config(config_dir=args.config_dir)
    except (FileNotFoundError, yaml.YAMLError) as exc:
        print(f"Config load failed: {exc}", file=sys.stderr)
        sys.exit(1)
    
    run_tx(args, config)


if __name__ == "__main__":
    main()
