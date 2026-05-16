"""PC-side receive/decrypt/display runtime.

V1 focus: RX path for UDP packets -> AES decrypt -> frame reassembly -> display.
Uses unified config loader to pull network and crypto settings from YAML.
"""

from __future__ import annotations

import argparse
import os
import sys
import socket
from pathlib import Path
from typing import Dict, Optional

import yaml

from protocol.packet_schema import split_datagram, pack_header
from protocol.validation import validate_header, validate_nonce_monotonic, validate_replay_window

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
    from .aes_gcm_sw import AesGcmSoftware
    from .reassembly import FrameReassembler
    from .video_io import FrameDisplay
except ImportError:
    from aes_gcm_sw import AesGcmSoftware
    from reassembly import FrameReassembler
    from video_io import FrameDisplay


def _nonce(counter: int) -> bytes:
    """Generate monotonic nonce matching TX side format.
    
    Must match pynq/runtime/main.py for validation.
    """
    return b"\x00\x00\x00\x01" + counter.to_bytes(8, "big")


class NonceValidator:
    """Tracks and validates nonce monotonicity and replay window."""
    
    def __init__(self, replay_window_packets: int = 1024):
        self.latest_nonce: int = 0
        self.replay_window = replay_window_packets
        self.seen_nonces: Dict[int, bool] = {}
        self.rejects_monotonic = 0
        self.rejects_replay = 0
    
    def validate_and_track(self, nonce_counter: int) -> bool:
        """Check if nonce is valid (monotonic + within replay window).
        
        Returns:
            True if nonce is valid, False if rejected.
        """
        # Check monotonicity
        if not validate_nonce_monotonic(self.latest_nonce, nonce_counter):
            self.rejects_monotonic += 1
            return False
        
        # Check replay window
        if not validate_replay_window(self.latest_nonce, nonce_counter, self.replay_window):
            self.rejects_replay += 1
            return False
        
        # Track in window
        self.seen_nonces[nonce_counter] = True
        self.latest_nonce = nonce_counter
        
        # Prune old entries beyond window
        if len(self.seen_nonces) > self.replay_window:
            cutoff = self.latest_nonce - self.replay_window
            self.seen_nonces = {k: v for k, v in self.seen_nonces.items() if k > cutoff}
        
        return True


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="OS-VideoSDR PC RX runtime",
        epilog="Config files (network.yaml, crypto.yaml) loaded from ./config/ by default."
    )
    parser.add_argument("--config-dir", default="config",
                       help="Config directory (default: ./config/)")
    parser.add_argument("--key-hex", default=os.environ.get("OSV_AES_KEY_HEX", ""),
                       help="AES-256 key (hex), or use OSV_AES_KEY_HEX env var")
    parser.add_argument("--max-frames", type=int, default=120,
                       help="Maximum frames to receive before exit")
    parser.add_argument("--display-mode", choices=["opencv", "headless"], default="opencv",
                       help="Display mode: opencv (live) or headless (no output)")
    parser.add_argument("--strict-nonce", action="store_true",
                       help="Reject any nonce validation failures (default: warn)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    
    # Load unified config
    try:
        config = load_config(config_dir=args.config_dir)
    except (FileNotFoundError, yaml.YAMLError) as exc:
        print(f"Config load failed: {exc}", file=sys.stderr)
        sys.exit(1)
    
    # Validate crypto setup
    if not args.key_hex:
        raise ValueError("--key-hex (or OSV_AES_KEY_HEX env var) is required")
    
    try:
        key = bytes.fromhex(args.key_hex)
    except ValueError as exc:
        raise ValueError(f"--key-hex must be valid hex: {exc}") from exc
    
    if len(key) != 32:
        raise ValueError(
            f"AES-256 requires 32-byte key; got {len(key)} bytes "
            f"({len(args.key_hex)} hex chars). Use 64-char hex string."
        )
    
    # Validate key_id from config
    key_id = config.crypto.rx_to_tx_key_id
    if not (0 < key_id < 256):
        raise ValueError(f"Invalid rx_to_tx_key_id from config: {key_id}")
    
    # Initialize components
    crypto = AesGcmSoftware(key)
    reasm = FrameReassembler()
    display = FrameDisplay(display_mode=args.display_mode)
    nonce_validator = NonceValidator(replay_window_packets=config.crypto.replay_window_packets)
    
    print(f"RX config: crypto=aesgcm display={args.display_mode} max_frames={args.max_frames}")
    print(f"Network: bind_ip={config.network.bind_ip}:{config.network.rx_port}")
    
    # Create UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((config.network.bind_ip, config.network.rx_port))
    
    completed = 0
    dropped = 0
    
    try:
        while completed < args.max_frames:
            try:
                datagram, addr = sock.recvfrom(65535)
            except socket.timeout:
                print(f"RX timeout; incomplete={completed}/{args.max_frames}")
                break
            
            # Parse and validate header
            try:
                header, payload, tag = split_datagram(datagram)
            except Exception as exc:
                dropped += 1
                continue
            
            errors = validate_header(header)
            if errors:
                dropped += 1
                continue
            
            # Validate nonce
            if not nonce_validator.validate_and_track(header.nonce_counter):
                if args.strict_nonce:
                    dropped += 1
                    print(f"RX nonce rejected: {header.nonce_counter} "
                          f"(monotonic_rejects={nonce_validator.rejects_monotonic} "
                          f"replay_rejects={nonce_validator.rejects_replay})")
                    continue
                else:
                    print(f"RX nonce warning: {header.nonce_counter}")
            
            # Validate key_id
            if header.key_id != key_id:
                dropped += 1
                print(f"RX key_id mismatch: got {header.key_id}, expected {key_id}")
                continue
            
            # Decrypt
            try:
                aad = pack_header(header)
                plain = crypto.decrypt(_nonce(header.nonce_counter), aad, payload, tag)
            except Exception as exc:
                dropped += 1
                print(f"RX decrypt failed: {exc}")
                continue
            
            # Reassemble and display
            frame = reasm.push(header, plain)
            if frame is None:
                continue
            
            completed += 1
            try:
                display.show(frame, frame_id=completed)
            except Exception as exc:
                print(f"RX display failed: {exc}")
            
            print(f"RX frame {completed}/{args.max_frames} bytes={len(frame)} "
                  f"nonce={header.nonce_counter} dropped={dropped}")
    
    finally:
        display.close()
        sock.close()
        print(f"RX complete: {completed} frames, {dropped} dropped")
        if nonce_validator.rejects_monotonic > 0 or nonce_validator.rejects_replay > 0:
            print(f"Nonce validation: {nonce_validator.rejects_monotonic} monotonic, "
                  f"{nonce_validator.rejects_replay} replay")


if __name__ == "__main__":
    main()
