"""Wired RX entrypoint for protocol and transport bring-up.

This receiver validates headers, enforces nonce replay policy, reassembles
frames, and prints runtime telemetry.
"""

from __future__ import annotations

import argparse
import heapq
import importlib
import math
from pathlib import Path
import socket
import struct
import sys
import time
from typing import Any, Dict, List, Set, Tuple

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


class _DmaAead:
    def __init__(
        self,
        key_hex: str,
        bitstream_path: str,
        ip_name: str,
        dma_name: str,
        timeout_s: float,
        decrypt_supported: bool,
    ) -> None:
        from aes_gcm_dma import AesGcmDmaEngine, DmaCryptoConfig

        config = DmaCryptoConfig(
            bitstream_path=bitstream_path,
            key_hex=key_hex,
            ip_name=ip_name,
            dma_name=dma_name,
            timeout_s=timeout_s,
            decrypt_supported=decrypt_supported,
        )
        self._engine = AesGcmDmaEngine(config)
        self._engine.load()

    def decrypt(self, nonce: bytes, aad: bytes, ciphertext: bytes, tag: bytes) -> bytes:
        return self._engine.decrypt(nonce, aad, ciphertext, tag)

    def close(self) -> None:
        self._engine.close()


class _ReplayState:
    def __init__(self) -> None:
        self.latest_nonce = -1
        self.seen: Set[int] = set()
        self.seen_heap: List[int] = []

    def accept(self, nonce_counter: int, replay_window: int) -> bool:
        latest = self.latest_nonce
        if latest >= 0 and not validate_replay_window(latest, nonce_counter, replay_window):
            return False

        if nonce_counter in self.seen:
            return False

        self.seen.add(nonce_counter)
        heapq.heappush(self.seen_heap, nonce_counter)

        if nonce_counter > latest:
            self.latest_nonce = nonce_counter

        cutoff = self.latest_nonce - replay_window
        while self.seen_heap and self.seen_heap[0] <= cutoff:
            stale_nonce = heapq.heappop(self.seen_heap)
            self.seen.discard(stale_nonce)

        return True


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


def _build_cipher(
    mode: str,
    key_hex: str,
    dma_bitstream: str,
    dma_ip_name: str,
    dma_name: str,
    dma_timeout_s: float,
    dma_decrypt_supported: bool,
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

    if not dma_decrypt_supported:
        raise ValueError(
            "--crypto-mode dma on RX requires a decrypt-capable overlay; "
            "rerun with --dma-decrypt-supported only when that bitstream is loaded"
        )

    return _DmaAead(
        key_hex=key.hex(),
        bitstream_path=dma_bitstream,
        ip_name=dma_ip_name,
        dma_name=dma_name,
        timeout_s=dma_timeout_s,
        decrypt_supported=dma_decrypt_supported,
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
    parser.add_argument("--max-runtime-s", type=float, default=0.0)
    parser.add_argument("--max-idle-s", type=float, default=0.0)

    parser.add_argument("--crypto-mode", choices=["none", "aesgcm", "dma"], default="none")
    parser.add_argument(
        "--crypto-granularity",
        choices=["packet", "frame"],
        default="packet",
        help="packet: decrypt each transport segment; frame: reassemble ciphertext then decrypt once",
    )
    parser.add_argument("--key-hex", default="")
    parser.add_argument("--dma-bitstream", default="aes_gcm_dma_wrapper.bit")
    parser.add_argument("--dma-ip-name", default="aes_gcm_0")
    parser.add_argument("--dma-name", default="axi_dma_0")
    parser.add_argument("--dma-timeout-s", type=float, default=5.0)
    parser.add_argument(
        "--dma-decrypt-supported",
        action="store_true",
        help="Enable only when running a decrypt-capable DMA overlay",
    )
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

    cipher = _build_cipher(
        mode=args.crypto_mode,
        key_hex=args.key_hex,
        dma_bitstream=args.dma_bitstream,
        dma_ip_name=args.dma_ip_name,
        dma_name=args.dma_name,
        dma_timeout_s=args.dma_timeout_s,
        dma_decrypt_supported=args.dma_decrypt_supported,
    )
    reassembler = FrameReassembler(max_active_frames=args.max_active_frames)
    telemetry = TelemetryCounters()
    bytes_received = 0
    bytes_since_print = 0

    rx = UdpRx(
        listen_port=listen_port,
        bind_ip=bind_ip,
        recv_buffer_bytes=recv_buffer_bytes,
        timeout_s=args.socket_timeout_s,
    )

    replay_state_by_key: Dict[int, _ReplayState] = {}
    frame_meta_by_key: Dict[Tuple[int, int, int], Dict[str, Any]] = {}
    frame_meta_limit = max(2, args.max_active_frames * 2)
    latency_ms: List[float] = []

    started = time.perf_counter()
    last_print = started
    last_packet_at = started

    print(
        "RX start:",
        f"listen={bind_ip}:{listen_port}",
        f"crypto_mode={args.crypto_mode}",
        f"crypto_granularity={args.crypto_granularity}",
        f"replay_window={args.replay_window}",
        f"max_runtime_s={args.max_runtime_s}",
        f"max_idle_s={args.max_idle_s}",
    )

    try:
        while True:
            now = time.perf_counter()
            if args.max_runtime_s > 0 and (now - started) >= args.max_runtime_s:
                break
            if args.max_packets > 0 and telemetry.packets_rx >= args.max_packets:
                break
            if args.max_frames > 0 and telemetry.frames_completed >= args.max_frames:
                break

            try:
                datagram, _peer = rx.recv(max_datagram_bytes=args.max_datagram_bytes)
            except socket.timeout:
                now = time.perf_counter()
                if args.max_idle_s > 0 and (now - last_packet_at) >= args.max_idle_s:
                    break
                if now - last_print >= args.print_interval_s:
                    elapsed = max(1e-9, now - started)
                    interval = max(1e-9, now - last_print)
                    avg_mbps = (bytes_received * 8.0) / (elapsed * 1_000_000.0)
                    inst_mbps = (bytes_since_print * 8.0) / (interval * 1_000_000.0)
                    print(
                        "RX stats:",
                        f"frames={telemetry.frames_completed}",
                        f"packets={telemetry.packets_rx}",
                        f"drops={telemetry.packets_dropped}",
                        f"decrypt_fail={telemetry.decrypt_failures}",
                        f"reorder={telemetry.reorder_events}",
                        f"latency_p95_ms={_p95(latency_ms):.2f}",
                        f"elapsed_s={elapsed:.1f}",
                        f"throughput_avg_mbps={avg_mbps:.2f}",
                        f"throughput_inst_mbps={inst_mbps:.2f}",
                    )
                    bytes_since_print = 0
                    last_print = now
                continue

            telemetry.packets_rx += 1
            bytes_received += len(datagram)
            bytes_since_print += len(datagram)
            last_packet_at = time.perf_counter()

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
            if args.crypto_granularity == "packet":
                replay_state = replay_state_by_key.setdefault(key_id, _ReplayState())
                if replay_state.latest_nonce >= 0 and header.nonce_counter < replay_state.latest_nonce:
                    telemetry.reorder_events += 1

                if not replay_state.accept(header.nonce_counter, args.replay_window):
                    telemetry.packets_dropped += 1
                    continue

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
            else:
                frame_key = (header.session_id, header.stream_id, header.frame_id)
                meta = frame_meta_by_key.get(frame_key)

                if meta is None:
                    if len(frame_meta_by_key) >= frame_meta_limit:
                        # Reassembler evicts old incomplete frames; keep metadata bounded too.
                        frame_meta_by_key.pop(next(iter(frame_meta_by_key)), None)
                    frame_meta_by_key[frame_key] = {
                        "nonce_counter": header.nonce_counter,
                        "key_id": header.key_id,
                        "payload_type": header.payload_type,
                        "tag": tag,
                        "source_timestamp_ns": header.source_timestamp_ns,
                        "session_id": header.session_id,
                        "stream_id": header.stream_id,
                        "frame_id": header.frame_id,
                    }
                else:
                    if (
                        header.nonce_counter != int(meta["nonce_counter"])
                        or header.key_id != int(meta["key_id"])
                        or header.payload_type != int(meta["payload_type"])
                        or tag != meta["tag"]
                    ):
                        telemetry.packets_dropped += 1
                        continue

                    if header.source_timestamp_ns > 0:
                        current_ts = int(meta.get("source_timestamp_ns", 0))
                        if current_ts == 0 or header.source_timestamp_ns < current_ts:
                            meta["source_timestamp_ns"] = header.source_timestamp_ns

                maybe_ciphertext_frame = reassembler.push(header, ciphertext)
                if maybe_ciphertext_frame is None:
                    continue

                meta = frame_meta_by_key.pop(frame_key, None)
                if meta is None:
                    telemetry.packets_dropped += 1
                    continue

                frame_nonce_counter = int(meta["nonce_counter"])
                frame_key_id = int(meta["key_id"])
                replay_state = replay_state_by_key.setdefault(frame_key_id, _ReplayState())
                if replay_state.latest_nonce >= 0 and frame_nonce_counter < replay_state.latest_nonce:
                    telemetry.reorder_events += 1

                if not replay_state.accept(frame_nonce_counter, args.replay_window):
                    telemetry.packets_dropped += 1
                    continue

                nonce = _nonce_bytes(int(meta["session_id"]), frame_nonce_counter)
                aad = _frame_aad(
                    session_id=int(meta["session_id"]),
                    stream_id=int(meta["stream_id"]),
                    frame_id=int(meta["frame_id"]),
                    key_id=frame_key_id,
                    payload_type=int(meta["payload_type"]),
                    payload_length=len(maybe_ciphertext_frame),
                    nonce_counter=frame_nonce_counter,
                )

                try:
                    _plaintext = cipher.decrypt(nonce, aad, maybe_ciphertext_frame, meta["tag"])
                except Exception:
                    telemetry.decrypt_failures += 1
                    telemetry.packets_dropped += 1
                    continue

                telemetry.frames_completed += 1
                source_ts = int(meta.get("source_timestamp_ns", 0))
                if source_ts > 0:
                    end_to_end_ms = (time.time_ns() - source_ts) / 1_000_000.0
                    latency_ms.append(end_to_end_ms)
                    if len(latency_ms) > 4096:
                        latency_ms = latency_ms[-2048:]

            now = time.perf_counter()
            if now - last_print >= args.print_interval_s:
                elapsed = max(1e-9, now - started)
                interval = max(1e-9, now - last_print)
                throughput_avg_mbps = (bytes_received * 8.0) / (elapsed * 1_000_000.0)
                throughput_inst_mbps = (bytes_since_print * 8.0) / (interval * 1_000_000.0)
                print(
                    "RX stats:",
                    f"frames={telemetry.frames_completed}",
                    f"packets={telemetry.packets_rx}",
                    f"drops={telemetry.packets_dropped}",
                    f"decrypt_fail={telemetry.decrypt_failures}",
                    f"reorder={telemetry.reorder_events}",
                    f"latency_p95_ms={_p95(latency_ms):.2f}",
                    f"elapsed_s={elapsed:.1f}",
                    f"throughput_avg_mbps={throughput_avg_mbps:.2f}",
                    f"throughput_inst_mbps={throughput_inst_mbps:.2f}",
                )
                bytes_since_print = 0
                last_print = now

    except KeyboardInterrupt:
        print("RX interrupted by user")
    finally:
        rx.close()
        close_fn = getattr(cipher, "close", None)
        if callable(close_fn):
            close_fn()

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
