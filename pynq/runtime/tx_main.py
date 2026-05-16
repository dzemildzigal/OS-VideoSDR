"""Wired TX entrypoint for protocol and transport bring-up.

This sender currently generates synthetic frames and exercises the full packet,
segmentation, and transport contract. HDMI capture integration should replace
the synthetic frame generator in later milestones.
"""

from __future__ import annotations

import argparse
import importlib
import math
from pathlib import Path
import struct
import sys
import time
from typing import Any, Dict, Iterable, Iterator, Optional, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from protocol.constants import (  # noqa: E402
    DEFAULT_TAG_LENGTH,
    PAYLOAD_TYPE_H264,
    PAYLOAD_TYPE_RAW_RGB,
    PAYLOAD_TYPE_RAW_YUV,
)
from protocol.packet_schema import PacketHeader, build_datagram, pack_header  # noqa: E402
from telemetry import TelemetryCounters  # noqa: E402
from udp_tx import UdpTx  # noqa: E402

PAYLOAD_TYPE_MAP = {
    "raw_rgb": PAYLOAD_TYPE_RAW_RGB,
    "raw_yuv": PAYLOAD_TYPE_RAW_YUV,
    "h264": PAYLOAD_TYPE_H264,
}

DEFAULT_PROFILES: Dict[str, Dict[str, Any]] = {
    "U10": {
        "width": 1920,
        "height": 1080,
        "fps": 10,
        "pixel_format": "RGB888",
    },
    "U15": {
        "width": 1920,
        "height": 1080,
        "fps": 15,
        "pixel_format": "RGB888",
    },
    "C60": {
        "width": 1920,
        "height": 1080,
        "fps": 60,
        "pixel_format": "H264",
    },
}

DEFAULT_NETWORK = {
    "bind_ip": "0.0.0.0",
    "tx_ip": "127.0.0.1",
    "tx_port": 5000,
    "max_payload_bytes": 1200,
    "send_buffer_bytes": 8 * 1024 * 1024,
    "inter_packet_gap_us": 0,
}


class _NullAead:
    def encrypt(self, _nonce: bytes, _aad: bytes, plaintext: bytes) -> Tuple[bytes, bytes]:
        return plaintext, b"\x00" * DEFAULT_TAG_LENGTH


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

    def encrypt(self, nonce: bytes, aad: bytes, plaintext: bytes) -> Tuple[bytes, bytes]:
        blob = self._aesgcm.encrypt(nonce, plaintext, aad)
        return blob[:-DEFAULT_TAG_LENGTH], blob[-DEFAULT_TAG_LENGTH:]


class _DmaAead:
    def __init__(
        self,
        key_hex: str,
        bitstream_path: str,
        ip_name: str,
        dma_name: str,
        timeout_s: float,
    ) -> None:
        from aes_gcm_dma import AesGcmDmaEngine, DmaCryptoConfig

        config = DmaCryptoConfig(
            bitstream_path=bitstream_path,
            key_hex=key_hex,
            ip_name=ip_name,
            dma_name=dma_name,
            timeout_s=timeout_s,
        )
        self._engine = AesGcmDmaEngine(config)
        self._engine.load()

    def encrypt(self, nonce: bytes, aad: bytes, plaintext: bytes) -> Tuple[bytes, bytes]:
        return self._engine.encrypt(nonce, aad, plaintext)

    def stats_snapshot(self) -> Dict[str, float]:
        return self._engine.performance_stats()

    def close(self) -> None:
        self._engine.close()


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


def _load_profile(profile_name: str, profiles_path: Path) -> Dict[str, Any]:
    merged = {k: dict(v) for k, v in DEFAULT_PROFILES.items()}
    loaded = _load_yaml_dict(profiles_path)

    loaded_profiles = loaded.get("profiles", {})
    if isinstance(loaded_profiles, dict):
        for name, cfg in loaded_profiles.items():
            if isinstance(cfg, dict):
                merged[name] = dict(cfg)

    profile = merged.get(profile_name)
    if profile is None:
        raise ValueError(f"Unknown profile '{profile_name}'")

    return profile


def _load_network(network_path: Path) -> Dict[str, Any]:
    merged = dict(DEFAULT_NETWORK)
    loaded = _load_yaml_dict(network_path)

    udp_cfg = loaded.get("udp", {})
    if isinstance(udp_cfg, dict):
        for key in merged:
            if key in udp_cfg:
                merged[key] = udp_cfg[key]

    pacing_cfg = loaded.get("pacing", {})
    if isinstance(pacing_cfg, dict) and pacing_cfg.get("enabled", False):
        if "inter_packet_gap_us" in pacing_cfg:
            merged["inter_packet_gap_us"] = pacing_cfg["inter_packet_gap_us"]

    return merged


def _build_cipher(
    mode: str,
    key_hex: str,
    dma_bitstream: str,
    dma_ip_name: str,
    dma_name: str,
    dma_timeout_s: float,
):
    if mode == "none":
        return _NullAead()

    if mode not in {"aesgcm", "dma"}:
        raise ValueError(f"Unsupported crypto mode: {mode}")

    if not key_hex:
        raise ValueError(f"--key-hex is required when --crypto-mode {mode}")

    try:
        key = bytes.fromhex(key_hex)
    except ValueError as exc:
        raise ValueError("--key-hex must contain valid hex bytes") from exc

    if len(key) != 32:
        raise ValueError(f"AES-256 key must be 32 bytes, got {len(key)}")

    if mode == "aesgcm":
        return _AesGcmAead(key)

    if dma_timeout_s <= 0:
        raise ValueError("--dma-timeout-s must be > 0")

    return _DmaAead(
        key_hex=key.hex(),
        bitstream_path=dma_bitstream,
        ip_name=dma_ip_name,
        dma_name=dma_name,
        timeout_s=dma_timeout_s,
    )


def _nonce_bytes(session_id: int, nonce_counter: int) -> bytes:
    return session_id.to_bytes(4, byteorder="big", signed=False) + nonce_counter.to_bytes(
        8, byteorder="big", signed=False
    )


def _frame_aad(
    session_id: int,
    stream_id: int,
    frame_id: int,
    key_id: int,
    payload_type: int,
    payload_length: int,
    nonce_counter: int,
) -> bytes:
    # Stable frame-level AAD for frame-granularity crypto mode.
    return struct.pack(
        "!IHIBBQI",
        session_id,
        stream_id,
        frame_id,
        key_id,
        payload_type,
        nonce_counter,
        payload_length,
    )


def _segment_payload(payload: bytes, chunk_size: int) -> Iterator[bytes]:
    for offset in range(0, len(payload), chunk_size):
        yield payload[offset : offset + chunk_size]


def _synthetic_frame(frame_id: int, frame_bytes: int) -> bytes:
    return bytes([frame_id & 0xFF]) * frame_bytes


def _infer_raw_frame_bytes(profile: Dict[str, Any]) -> int:
    width = int(profile.get("width", 1920))
    height = int(profile.get("height", 1080))
    pixel_format = str(profile.get("pixel_format", "RGB888")).upper()

    if "RGB" in pixel_format:
        bpp = 3
    elif "YUV" in pixel_format:
        bpp = 2
    else:
        bpp = 1

    return width * height * bpp


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OS-VideoSDR wired TX bring-up sender")
    parser.add_argument("--profile", default="U10")
    parser.add_argument("--profiles", default="config/profiles.yaml")
    parser.add_argument("--network", default="config/network.yaml")
    parser.add_argument("--crypto", default="config/crypto.yaml")

    parser.add_argument("--target-ip", default=None)
    parser.add_argument("--target-port", type=int, default=None)
    parser.add_argument("--bind-ip", default=None)

    parser.add_argument("--fps", type=int, default=None)
    parser.add_argument("--frames", type=int, default=0)
    parser.add_argument("--frame-source", choices=["synthetic", "hdmi"], default="synthetic")
    parser.add_argument("--synthetic-frame-bytes", type=int, default=120_000)
    parser.add_argument("--hdmi-bitstream", default="")
    parser.add_argument("--hdmi-capture-timeout-s", type=float, default=2.0)
    parser.add_argument("--segment-bytes", type=int, default=None)

    parser.add_argument("--session-id", type=int, default=0)
    parser.add_argument("--stream-id", type=int, default=1)
    parser.add_argument("--payload-type", choices=sorted(PAYLOAD_TYPE_MAP.keys()), default="raw_rgb")

    parser.add_argument("--crypto-mode", choices=["none", "aesgcm", "dma"], default="none")
    parser.add_argument(
        "--crypto-granularity",
        choices=["packet", "frame", "chunk"],
        default="packet",
        help=(
            "packet: encrypt each transport segment; "
            "frame: encrypt full frame then segment ciphertext; "
            "chunk: encrypt medium-size blocks then segment ciphertext"
        ),
    )
    parser.add_argument("--crypto-chunk-bytes", type=int, default=24_000)
    parser.add_argument("--key-hex", default="")
    parser.add_argument("--key-id", type=int, default=1)
    parser.add_argument("--dma-bitstream", default="aes_gcm_dma_wrapper.bit")
    parser.add_argument("--dma-ip-name", default="aes_gcm_0")
    parser.add_argument("--dma-name", default="axi_dma_0")
    parser.add_argument("--dma-timeout-s", type=float, default=5.0)

    parser.add_argument("--send-buffer-bytes", type=int, default=None)
    parser.add_argument("--inter-packet-gap-us", type=int, default=None)
    parser.add_argument("--print-interval-s", type=float, default=1.0)
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()

    profile = _load_profile(args.profile, Path(args.profiles))
    network = _load_network(Path(args.network))

    target_ip = args.target_ip if args.target_ip else str(network["tx_ip"])
    target_port = args.target_port if args.target_port else int(network["tx_port"])
    bind_ip = args.bind_ip if args.bind_ip else str(network["bind_ip"])

    fps = int(args.fps) if args.fps else int(profile.get("fps", 10))
    if fps <= 0:
        raise ValueError("fps must be > 0")

    segment_bytes = (
        int(args.segment_bytes)
        if args.segment_bytes is not None
        else int(network.get("max_payload_bytes", 1200))
    )
    if segment_bytes <= 0:
        raise ValueError("segment-bytes must be > 0")

    frame_bytes = int(args.synthetic_frame_bytes)
    if frame_bytes <= 0:
        frame_bytes = _infer_raw_frame_bytes(profile)

    capture = None
    capture_frames: Optional[Iterator[bytes]] = None
    if args.frame_source == "hdmi":
        from hdmi_capture import HdmiCapture, HdmiCaptureConfig

        capture_cfg = HdmiCaptureConfig(
            width=int(profile.get("width", 1920)),
            height=int(profile.get("height", 1080)),
            fps=fps,
            pixel_format=str(profile.get("pixel_format", "RGB888")),
            bitstream_path=args.hdmi_bitstream or None,
            frame_timeout_s=float(args.hdmi_capture_timeout_s),
        )
        capture = HdmiCapture(capture_cfg)
        capture_frames = capture.frames()
        # HDMI source uses profile-derived frame size expectations.
        frame_bytes = _infer_raw_frame_bytes(profile)

    crypto_chunk_bytes = int(args.crypto_chunk_bytes)
    if crypto_chunk_bytes <= 0:
        raise ValueError("crypto-chunk-bytes must be > 0")

    send_buffer_bytes = (
        int(args.send_buffer_bytes)
        if args.send_buffer_bytes is not None
        else int(network.get("send_buffer_bytes", 8 * 1024 * 1024))
    )

    inter_packet_gap_us = (
        int(args.inter_packet_gap_us)
        if args.inter_packet_gap_us is not None
        else int(network.get("inter_packet_gap_us", 0))
    )
    if inter_packet_gap_us < 0:
        raise ValueError("inter-packet-gap-us must be >= 0")

    session_id = args.session_id if args.session_id > 0 else (int(time.time()) & 0xFFFFFFFF)
    payload_type = PAYLOAD_TYPE_MAP[args.payload_type]
    cipher = _build_cipher(
        mode=args.crypto_mode,
        key_hex=args.key_hex,
        dma_bitstream=args.dma_bitstream,
        dma_ip_name=args.dma_ip_name,
        dma_name=args.dma_name,
        dma_timeout_s=args.dma_timeout_s,
    )

    tx = UdpTx(
        target_ip=target_ip,
        target_port=target_port,
        bind_ip=bind_ip,
        send_buffer_bytes=send_buffer_bytes,
    )

    telemetry = TelemetryCounters()
    bytes_sent = 0
    frame_id = 0
    nonce_counter = 0

    frame_period_s = 1.0 / fps
    next_frame_deadline = time.perf_counter()

    started = time.perf_counter()
    last_print = started

    print(
        "TX start:",
        f"profile={args.profile}",
        f"frame_source={args.frame_source}",
        f"target={target_ip}:{target_port}",
        f"fps={fps}",
        f"frame_bytes={frame_bytes}",
        f"segment_bytes={segment_bytes}",
        f"crypto_mode={args.crypto_mode}",
        f"crypto_granularity={args.crypto_granularity}",
        f"crypto_chunk_bytes={crypto_chunk_bytes}",
        f"inter_packet_gap_us={inter_packet_gap_us}",
    )

    try:
        while args.frames <= 0 or frame_id < args.frames:
            if capture_frames is None:
                frame = _synthetic_frame(frame_id, frame_bytes)
            else:
                try:
                    frame = next(capture_frames)
                    telemetry.frames_captured += 1
                except StopIteration:
                    print("TX capture source exhausted")
                    break
                except Exception as exc:
                    telemetry.capture_failures += 1
                    print(f"TX capture failure: {exc}")
                    continue
            frame_timestamp_ns = time.time_ns()
            if args.crypto_granularity == "packet":
                segments = list(_segment_payload(frame, segment_bytes))
                segment_count = len(segments)

                for segment_id, segment_payload in enumerate(segments):
                    nonce_counter += 1
                    header = PacketHeader(
                        session_id=session_id,
                        stream_id=args.stream_id,
                        frame_id=frame_id,
                        segment_id=segment_id,
                        segment_count=segment_count,
                        source_timestamp_ns=frame_timestamp_ns,
                        payload_type=payload_type,
                        key_id=args.key_id,
                        payload_length=len(segment_payload),
                        nonce_counter=nonce_counter,
                        tag_length=DEFAULT_TAG_LENGTH,
                    )

                    aad = pack_header(header)
                    nonce = _nonce_bytes(session_id, nonce_counter)
                    ciphertext, tag = cipher.encrypt(nonce, aad, segment_payload)

                    header.payload_length = len(ciphertext)
                    datagram = build_datagram(header, ciphertext, tag)
                    bytes_sent += tx.send(datagram)
                    telemetry.packets_tx += 1
                    if inter_packet_gap_us > 0:
                        time.sleep(inter_packet_gap_us / 1_000_000.0)
            elif args.crypto_granularity == "frame":
                nonce_counter += 1
                nonce = _nonce_bytes(session_id, nonce_counter)
                frame_level_aad = _frame_aad(
                    session_id=session_id,
                    stream_id=args.stream_id,
                    frame_id=frame_id,
                    key_id=args.key_id,
                    payload_type=payload_type,
                    payload_length=len(frame),
                    nonce_counter=nonce_counter,
                )
                frame_ciphertext, tag = cipher.encrypt(nonce, frame_level_aad, frame)

                segments = list(_segment_payload(frame_ciphertext, segment_bytes))
                segment_count = len(segments)

                for segment_id, segment_payload in enumerate(segments):
                    header = PacketHeader(
                        session_id=session_id,
                        stream_id=args.stream_id,
                        frame_id=frame_id,
                        segment_id=segment_id,
                        segment_count=segment_count,
                        source_timestamp_ns=frame_timestamp_ns,
                        payload_type=payload_type,
                        key_id=args.key_id,
                        payload_length=len(segment_payload),
                        nonce_counter=nonce_counter,
                        tag_length=DEFAULT_TAG_LENGTH,
                    )

                    datagram = build_datagram(header, segment_payload, tag)
                    bytes_sent += tx.send(datagram)
                    telemetry.packets_tx += 1
                    if inter_packet_gap_us > 0:
                        time.sleep(inter_packet_gap_us / 1_000_000.0)
            else:
                chunks = list(_segment_payload(frame, crypto_chunk_bytes))
                chunk_segment_counts = [math.ceil(len(chunk) / segment_bytes) for chunk in chunks]
                segment_count = sum(chunk_segment_counts)
                segment_id = 0

                for chunk_payload in chunks:
                    nonce_counter += 1
                    nonce = _nonce_bytes(session_id, nonce_counter)
                    chunk_level_aad = _frame_aad(
                        session_id=session_id,
                        stream_id=args.stream_id,
                        frame_id=frame_id,
                        key_id=args.key_id,
                        payload_type=payload_type,
                        payload_length=len(chunk_payload),
                        nonce_counter=nonce_counter,
                    )
                    chunk_ciphertext, tag = cipher.encrypt(nonce, chunk_level_aad, chunk_payload)

                    for segment_payload in _segment_payload(chunk_ciphertext, segment_bytes):
                        header = PacketHeader(
                            session_id=session_id,
                            stream_id=args.stream_id,
                            frame_id=frame_id,
                            segment_id=segment_id,
                            segment_count=segment_count,
                            source_timestamp_ns=frame_timestamp_ns,
                            payload_type=payload_type,
                            key_id=args.key_id,
                            payload_length=len(segment_payload),
                            nonce_counter=nonce_counter,
                            tag_length=DEFAULT_TAG_LENGTH,
                        )

                        datagram = build_datagram(header, segment_payload, tag)
                        bytes_sent += tx.send(datagram)
                        telemetry.packets_tx += 1
                        segment_id += 1
                        if inter_packet_gap_us > 0:
                            time.sleep(inter_packet_gap_us / 1_000_000.0)

            telemetry.frames_completed += 1
            frame_id += 1

            now = time.perf_counter()
            if now - last_print >= args.print_interval_s:
                elapsed = max(1e-9, now - started)
                mbps = (bytes_sent * 8.0) / (elapsed * 1_000_000.0)
                print(
                    "TX stats:",
                    f"frames={telemetry.frames_completed}",
                    f"captured={telemetry.frames_captured}",
                    f"capture_fail={telemetry.capture_failures}",
                    f"packets={telemetry.packets_tx}",
                    f"throughput_mbps={mbps:.2f}",
                )

                stats_fn = getattr(cipher, "stats_snapshot", None)
                if callable(stats_fn):
                    dma_stats = stats_fn()
                    print(
                        "TX dma:",
                        f"avg_encrypt_ms={dma_stats['avg_encrypt_ms']:.3f}",
                        f"avg_dma_ms={dma_stats['avg_dma_ms']:.3f}",
                        f"avg_control_ms={dma_stats['avg_control_ms']:.3f}",
                        f"key_loads={int(dma_stats['key_loads'])}",
                        f"buffer_pool_entries={int(dma_stats['buffer_pool_entries'])}",
                    )
                last_print = now

            next_frame_deadline += frame_period_s
            sleep_s = next_frame_deadline - time.perf_counter()
            if sleep_s > 0:
                time.sleep(sleep_s)

    except KeyboardInterrupt:
        print("TX interrupted by user")
    finally:
        tx.close()
        if capture is not None:
            capture.close()
        close_fn = getattr(cipher, "close", None)
        if callable(close_fn):
            close_fn()

    elapsed = max(1e-9, time.perf_counter() - started)
    mbps = (bytes_sent * 8.0) / (elapsed * 1_000_000.0)
    print(
        "TX done:",
        f"frames={telemetry.frames_completed}",
        f"captured={telemetry.frames_captured}",
        f"capture_fail={telemetry.capture_failures}",
        f"packets={telemetry.packets_tx}",
        f"throughput_mbps={mbps:.2f}",
    )

    stats_fn = getattr(cipher, "stats_snapshot", None)
    if callable(stats_fn):
        dma_stats = stats_fn()
        print(
            "TX dma done:",
            f"calls={int(dma_stats['encrypt_calls'])}",
            f"avg_encrypt_ms={dma_stats['avg_encrypt_ms']:.3f}",
            f"avg_dma_ms={dma_stats['avg_dma_ms']:.3f}",
            f"avg_control_ms={dma_stats['avg_control_ms']:.3f}",
            f"avg_tag_wait_ms={dma_stats['avg_tag_wait_ms']:.3f}",
            f"key_loads={int(dma_stats['key_loads'])}",
            f"buffer_pool_entries={int(dma_stats['buffer_pool_entries'])}",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
