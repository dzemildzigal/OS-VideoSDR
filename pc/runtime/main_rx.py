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

from protocol.constants import AUTHENTICATED_BODY_BYTES, TRANSPORT_SLOT_BYTES
from protocol.packet_schema import split_datagram, pack_header, unpack_header
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
    """Generate monotonic nonce matching TX side format."""
    return b"\x00\x00\x00\x01" + counter.to_bytes(8, "big")


def strip_transport_padding(datagram: bytes) -> bytes:
    """Remove the unauthenticated B.2/B.3 tail from one GSO segment."""
    if len(datagram) != TRANSPORT_SLOT_BYTES:
        raise ValueError(
            f"B.3 segment size mismatch: expected {TRANSPORT_SLOT_BYTES}, got {len(datagram)}"
        )
    if any(datagram[AUTHENTICATED_BODY_BYTES:]):
        raise ValueError("B.3 transport padding is not zero")
    return datagram[:AUTHENTICATED_BODY_BYTES]


class NonceValidator:
    """Tracks nonces and rejects replays for the receive stream.

    Two sender threads put packets on the wire at the same time, so packets
    arrive slightly out of order (measured: about 0.5% of packets). A nonce
    below the highest one seen is therefore accepted while it is still inside
    the reorder window and has not been seen before. Duplicates, and packets
    older than the window, are rejected. Use reorder_window_packets=0 for a
    strictly monotonic single-sender stream.
    """

    def __init__(self, replay_window_packets: int = 1024, reorder_window_packets: int = None):
        self.latest_nonce: int = 0
        self.replay_window = replay_window_packets
        self.reorder_window = (
            replay_window_packets if reorder_window_packets is None else reorder_window_packets
        )
        self.seen_nonces: Dict[int, bool] = {}
        self.rejects_monotonic = 0
        self.rejects_replay = 0
        self.reordered = 0

    def validate_and_track(self, nonce_counter: int) -> bool:
        """Check if nonce is valid (no duplicate, inside the tracking window).

        Returns:
            True if nonce is valid, False if rejected.
        """
        # Replay of a packet whose entry is still tracked.
        if nonce_counter in self.seen_nonces:
            self.rejects_replay += 1
            return False

        if nonce_counter > self.latest_nonce:
            pass
        elif (nonce_counter + self.reorder_window) > self.latest_nonce:
            # Late arrival from the other sender thread. Legal, but it must not
            # move the high-water mark backwards.
            self.reordered += 1
        else:
            self.rejects_monotonic += 1
            return False

        self.seen_nonces[nonce_counter] = True
        if nonce_counter > self.latest_nonce:
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
    parser.add_argument("--key-hex", default=os.environ.get("OSV_AES_KEY_HEX", "000102030405060708090A0B0C0D0E0F000102030405060708090A0B0C0D0E0F"),
                       help="AES-256 key (hex), or use OSV_AES_KEY_HEX env var")
    parser.add_argument("--max-frames", type=int, default=120,
                       help="Maximum frames to receive before exit")
    parser.add_argument("--display-mode", choices=["opencv", "headless"], default="opencv",
                       help="Display mode: opencv (live) or headless (no output)")
    parser.add_argument("--strict-nonce", action="store_true",
                       help="Require strictly increasing nonces (single sender); "
                            "default accepts out-of-order arrivals inside the "
                            "replay window")
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
    
    # Validate expected key_id for inbound TX->RX traffic.
    key_id = config.crypto.tx_to_rx_key_id
    if not (0 < key_id < 256):
        raise ValueError(f"Invalid tx_to_rx_key_id from config: {key_id}")
    
    # Initialize components
    crypto = AesGcmSoftware(key)
    reasm = FrameReassembler()
    display = FrameDisplay(display_mode=args.display_mode, width=1280, height=720)
    # strict-nonce keeps the old strictly monotonic behaviour (single sender).
    nonce_validator = NonceValidator(
        replay_window_packets=config.crypto.replay_window_packets,
        reorder_window_packets=0 if args.strict_nonce else None,
    )
    
    print(f"RX config: crypto=aesgcm display={args.display_mode} max_frames={args.max_frames}")
    print(f"Network: bind_ip={config.network.bind_ip}:{config.network.rx_port}")
    
    # Create UDP socket
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, config.network.recv_buffer_bytes)
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
            
            # B.3 sends one complete 1280-byte slot per UDP segment. The
            # final 40 bytes are explicit transport padding and are outside
            # the GCM body. Remove and validate them before parsing the nonce,
            # ciphertext, and tag. Legacy non-padded datagrams remain valid.
            if len(datagram) == TRANSPORT_SLOT_BYTES:
                try:
                    datagram = strip_transport_padding(datagram)
                except ValueError:
                    dropped += 1
                    continue

            # PL packets encrypt header+payload with AAD=0 and carry an
            # 8-byte cleartext nonce prefix. Detect by the missing OSV magic,
            # decrypt, then parse the recovered plaintext header.
            is_pl_format = len(datagram) >= 40 and datagram[0:2] != b"\x4f\x56"
            if is_pl_format:
                try:
                    if len(datagram) < 8 + 16:
                        raise ValueError("PL datagram too short")
                    nonce_prefix = int.from_bytes(datagram[0:8], "big")
                    pl_ct = datagram[8:-16]
                    pl_tag = datagram[-16:]
                    pl_plain = crypto.decrypt(_nonce(nonce_prefix), b"", pl_ct, pl_tag)
                    if len(pl_plain) < 40:
                        raise ValueError("decrypted plaintext too short")
                    # Inner plaintext = OSV header + segment payload. The
                    # outer GCM tag authenticated the full encrypted body.
                    header = unpack_header(pl_plain[:40])
                    payload = pl_plain[40:]
                    tag = pl_tag
                except Exception:
                    dropped += 1
                    continue
            else:
                try:
                    header, payload, tag = split_datagram(datagram)
                except Exception:
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
                if is_pl_format:
                    # Already decrypted above; payload/tag from the recovered
                    # plaintext are the authenticated segment + trailing tag of
                    # the inner OSV packet (ignore the inner tag - the outer GCM
                    # already verified).
                    plain = payload
                else:
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
                display.show(frame, frame_id=completed, format_hint="rgb24")
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
        if nonce_validator.reordered > 0:
            print(f"Nonce reordering: {nonce_validator.reordered} packets arrived out of "
                  f"order and were accepted inside the window")


if __name__ == "__main__":
    main()
