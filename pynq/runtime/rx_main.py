"""Wired RX entrypoint for protocol and transport bring-up.

This receiver validates headers, enforces nonce replay policy, reassembles
frames, and prints runtime telemetry.
"""

from __future__ import annotations

import argparse
import importlib
import math
from pathlib import Path
import socket
import sys
import time
from typing import Any, Dict, List, Set

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from protocol.constants import DEFAULT_TAG_LENGTH  # noqa: E402
from protocol.packet_schema import pack_header, split_datagram  # noqa: E402
from protocol.validation import (  # noqa: E402
    validate_header,
    validate_replay_window,
)
from reassembly import FrameReassembler  # noqa: E402
from telemetry import TelemetryCounters  # noqa: E402
from udp_rx import UdpRx  # noqa: E402

DEFAULT_NETWORK = {
    "bind_ip": "0.0.0.0",
    "rx_port": 5000,
    "recv_buffer_bytes": 8 * 1024 * 1024,
}


class _NullAead:
    def decrypt(self, _nonce: bytes, _aad: bytes, ciphertext: bytes, tag: bytes) -> bytes:
        if len(tag) != DEFAULT_TAG_LENGTH:
            raise ValueError(f"Unexpected tag length {len(tag)}")
        return ciphertext


class _AesGcmAead:
    def __init__(self, key: bytes) -> None:
        try:
            aead_module = importlib.import_module("cryptography.hazmat.primitives.ciphers.aead")
            AESGCM = getattr(aead_module, "AESGCM")
        except ImportError as exc:
            raise RuntimeError(
                "cryptography package is required for --crypto-mode aesgcm"
            ) from exc

        self._aesgcm = AESGCM(key)

    def decrypt(self, nonce: bytes, aad: bytes, ciphertext: bytes, tag: bytes) -> bytes:
        return self._aesgcm.decrypt(nonce, ciphertext + tag, aad)


def _load_yaml_dict(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}

    try:
        import yaml  # type: ignore
    except ImportError:
        return {}

    with path.open("r", encoding="utf-8") as handle:
        loaded = yaml.safe_load(handle)

    return loaded if isinstance(loaded, dict) else {}


def _load_network(network_path: Path) -> Dict[str, Any]:
    merged = dict(DEFAULT_NETWORK)
    loaded = _load_yaml_dict(network_path)

    udp_cfg = loaded.get("udp", {})
    if isinstance(udp_cfg, dict):
        for key in merged:
            if key in udp_cfg:
                merged[key] = udp_cfg[key]

    return merged


def _build_cipher(mode: str, key_hex: str):
    if mode == "none":
        return _NullAead()

    if mode != "aesgcm":
        raise ValueError(f"Unsupported crypto mode: {mode}")

    if not key_hex:
        raise ValueError("--key-hex is required when --crypto-mode aesgcm")

    key = bytes.fromhex(key_hex)
    if len(key) != 32:
        raise ValueError(f"AES-256 key must be 32 bytes, got {len(key)}")

    return _AesGcmAead(key)


def _nonce_bytes(session_id: int, nonce_counter: int) -> bytes:
    return session_id.to_bytes(4, byteorder="big", signed=False) + nonce_counter.to_bytes(
        8, byteorder="big", signed=False
    )


def _p95(values: List[float]) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, math.ceil(0.95 * len(ordered)) - 1)
    return ordered[index]


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OS-VideoSDR wired RX bring-up receiver")
    parser.add_argument("--profile", default="U10")
    parser.add_argument("--profiles", default="config/profiles.yaml")
    parser.add_argument("--network", default="config/network.yaml")
    parser.add_argument("--crypto", default="config/crypto.yaml")

    parser.add_argument("--bind-ip", default=None)
    parser.add_argument("--listen-port", type=int, default=None)
    parser.add_argument("--recv-buffer-bytes", type=int, default=None)
    parser.add_argument("--socket-timeout-s", type=float, default=0.25)
    parser.add_argument("--max-datagram-bytes", type=int, default=4096)

    parser.add_argument("--max-frames", type=int, default=0)
    parser.add_argument("--max-packets", type=int, default=0)
    parser.add_argument("--max-active-frames", type=int, default=16)

    parser.add_argument("--crypto-mode", choices=["none", "aesgcm"], default="none")
    parser.add_argument("--key-hex", default="")
    parser.add_argument("--replay-window", type=int, default=1024)
    parser.add_argument("--print-interval-s", type=float, default=1.0)
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    network = _load_network(Path(args.network))
    bind_ip = args.bind_ip if args.bind_ip else str(network["bind_ip"])
    listen_port = args.listen_port if args.listen_port else int(network["rx_port"])
    recv_buffer_bytes = (
        int(args.recv_buffer_bytes)
        if args.recv_buffer_bytes is not None
        else int(network.get("recv_buffer_bytes", 8 * 1024 * 1024))
    )

    if args.replay_window <= 0:
        raise ValueError("replay-window must be > 0")

    cipher = _build_cipher(args.crypto_mode, args.key_hex)
    reassembler = FrameReassembler(max_active_frames=args.max_active_frames)
    telemetry = TelemetryCounters()
    bytes_received = 0

    rx = UdpRx(
        listen_port=listen_port,
        bind_ip=bind_ip,
        recv_buffer_bytes=recv_buffer_bytes,
        timeout_s=args.socket_timeout_s,
    )

    nonce_latest_by_key: Dict[int, int] = {}
    nonce_seen_by_key: Dict[int, Set[int]] = {}
    latency_ms: List[float] = []

    started = time.perf_counter()
    last_print = started

    print(
        "RX start:",
        f"listen={bind_ip}:{listen_port}",
        f"crypto_mode={args.crypto_mode}",
        f"replay_window={args.replay_window}",
    )

    try:
        while True:
            if args.max_packets > 0 and telemetry.packets_rx >= args.max_packets:
                break
            if args.max_frames > 0 and telemetry.frames_completed >= args.max_frames:
                break

            try:
                datagram, _peer = rx.recv(max_datagram_bytes=args.max_datagram_bytes)
            except socket.timeout:
                now = time.perf_counter()
                if now - last_print >= args.print_interval_s:
                    elapsed = max(1e-9, now - started)
                    mbps = (bytes_received * 8.0) / (elapsed * 1_000_000.0)
                    print(
                        "RX stats:",
                        f"frames={telemetry.frames_completed}",
                        f"packets={telemetry.packets_rx}",
                        f"drops={telemetry.packets_dropped}",
                        f"decrypt_fail={telemetry.decrypt_failures}",
                        f"reorder={telemetry.reorder_events}",
                        f"latency_p95_ms={_p95(latency_ms):.2f}",
                        f"elapsed_s={elapsed:.1f}",
                        f"throughput_mbps={mbps:.2f}",
                    )
                    last_print = now
                continue

            telemetry.packets_rx += 1
            bytes_received += len(datagram)

            try:
                header, ciphertext, tag = split_datagram(datagram)
            except ValueError:
                telemetry.packets_dropped += 1
                continue

            header_errors = validate_header(header)
            if header_errors:
                telemetry.packets_dropped += 1
                continue

            key_id = header.key_id
            latest_nonce = nonce_latest_by_key.get(key_id, -1)
            if latest_nonce >= 0 and not validate_replay_window(
                latest_nonce, header.nonce_counter, args.replay_window
            ):
                telemetry.packets_dropped += 1
                continue

            seen = nonce_seen_by_key.setdefault(key_id, set())
            if header.nonce_counter in seen:
                telemetry.packets_dropped += 1
                continue

            if latest_nonce >= 0 and header.nonce_counter < latest_nonce:
                telemetry.reorder_events += 1

            seen.add(header.nonce_counter)
            if header.nonce_counter > latest_nonce:
                nonce_latest_by_key[key_id] = header.nonce_counter

            latest_nonce = nonce_latest_by_key.get(key_id, header.nonce_counter)
            cutoff = latest_nonce - args.replay_window
            if cutoff > 0:
                stale = {value for value in seen if value <= cutoff}
                if stale:
                    seen.difference_update(stale)

            aad = pack_header(header)
            nonce = _nonce_bytes(header.session_id, header.nonce_counter)

            try:
                plaintext = cipher.decrypt(nonce, aad, ciphertext, tag)
            except Exception:
                telemetry.decrypt_failures += 1
                telemetry.packets_dropped += 1
                continue

            maybe_frame = reassembler.push(header, plaintext)
            if maybe_frame is not None:
                telemetry.frames_completed += 1
                if header.source_timestamp_ns > 0:
                    end_to_end_ms = (time.time_ns() - header.source_timestamp_ns) / 1_000_000.0
                    latency_ms.append(end_to_end_ms)
                    if len(latency_ms) > 4096:
                        latency_ms = latency_ms[-2048:]

            now = time.perf_counter()
            if now - last_print >= args.print_interval_s:
                elapsed = max(1e-9, now - started)
                throughput_mbps = (bytes_received * 8.0) / (elapsed * 1_000_000.0)
                print(
                    "RX stats:",
                    f"frames={telemetry.frames_completed}",
                    f"packets={telemetry.packets_rx}",
                    f"drops={telemetry.packets_dropped}",
                    f"decrypt_fail={telemetry.decrypt_failures}",
                    f"reorder={telemetry.reorder_events}",
                    f"latency_p95_ms={_p95(latency_ms):.2f}",
                    f"elapsed_s={elapsed:.1f}",
                    f"throughput_mbps={throughput_mbps:.2f}",
                )
                last_print = now

    except KeyboardInterrupt:
        print("RX interrupted by user")
    finally:
        rx.close()

    elapsed = max(1e-9, time.perf_counter() - started)
    print(
        "RX done:",
        f"frames={telemetry.frames_completed}",
        f"packets={telemetry.packets_rx}",
        f"drops={telemetry.packets_dropped}",
        f"decrypt_fail={telemetry.decrypt_failures}",
        f"reorder={telemetry.reorder_events}",
        f"latency_p95_ms={_p95(latency_ms):.2f}",
        f"elapsed_s={elapsed:.1f}",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
