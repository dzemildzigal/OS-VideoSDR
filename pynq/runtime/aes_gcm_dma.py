"""PYNQ AES-GCM DMA adapter interface.

This module defines the runtime contract between endpoint code and the
board-side AES-GCM DMA implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
import importlib
from pathlib import Path
import time
from typing import Any, Callable, Optional, Tuple


@dataclass(slots=True)
class DmaCryptoConfig:
    bitstream_path: str
    key_hex: str
    ip_name: str = "aes_gcm_0"
    dma_name: str = "axi_dma_0"
    timeout_s: float = 5.0
    decrypt_supported: bool = False


# Register map
CTRL = 0x00
STATUS = 0x04

KEY_BASE = 0x08
NONCE_BASE = 0x28
AAD_LEN_HI = 0x34
AAD_LEN_LO = 0x38
PT_LEN_HI = 0x3C
PT_LEN_LO = 0x40
AAD_BASE = 0x44
TAG_BASE = 0x88

# CTRL bits
CTRL_LOAD_KEY = 1 << 1
CTRL_START_SESSION = 1 << 2
CTRL_PUSH_AAD = 1 << 3
CTRL_AAD_LAST = 1 << 4
CTRL_SET_STREAM = 1 << 7

# STATUS bits
STS_KEYS_READY_MASK = 0xF
STS_SESSION_READY = 1 << 4
STS_AAD_READY = 1 << 5
STS_H_VALID = 1 << 8
STS_TAG_VALID = 1 << 12
STS_AAD_DROP = 1 << 13
STS_PT_DROP = 1 << 14
STS_SESSION_DROP = 1 << 15
STS_STREAM_MODE = 1 << 17
STS_CT_FIFO_OVERFLOW = 1 << 18


def _pad_to_block(value: bytes) -> bytes:
    if not value:
        return value

    remainder = len(value) % 16
    if remainder == 0:
        return value

    return value + (b"\x00" * (16 - remainder))


def _resolve_path(value: str) -> Path:
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate

    project_root = Path(__file__).resolve().parents[2]
    sibling_root = project_root.parent

    search_paths = [
        (Path.cwd() / candidate).resolve(),
        (project_root / candidate).resolve(),
        (project_root / "pynq" / "overlays" / "tx" / candidate.name).resolve(),
        (project_root / "pynq" / "overlays" / "rx" / candidate.name).resolve(),
        (sibling_root / "AES256" / candidate).resolve(),
        (sibling_root / "AES256" / candidate.name).resolve(),
        (sibling_root / "AES256" / "pynq" / candidate.name).resolve(),
        (sibling_root / "AES-256-SystemVerilog" / candidate).resolve(),
        (sibling_root / "AES-256-SystemVerilog" / candidate.name).resolve(),
        (sibling_root / "AES-256-SystemVerilog" / "pynq" / candidate.name).resolve(),
    ]

    for path in search_paths:
        if path.exists():
            return path

    if candidate.name == "aes_gcm_dma_wrapper.bit":
        return (sibling_root / "AES256" / candidate.name).resolve()

    return (project_root / candidate).resolve()


class AesGcmDmaEngine:
    def __init__(self, config: DmaCryptoConfig) -> None:
        self.config = config
        self._key = bytes.fromhex(config.key_hex)
        if len(self._key) != 32:
            raise ValueError(f"AES-256 key must be 32 bytes, got {len(self._key)}")

        self.loaded = False
        self._overlay: Any = None
        self._aes: Any = None
        self._dma: Any = None
        self._allocate: Optional[Callable[..., Any]] = None

    def load(self) -> None:
        """Load overlay and bind DMA resources."""
        bitstream = _resolve_path(self.config.bitstream_path)
        if not bitstream.exists():
            raise FileNotFoundError(f"DMA bitstream not found: {bitstream}")

        try:
            pynq_mod = importlib.import_module("pynq")
            Overlay = getattr(pynq_mod, "Overlay")
            allocate = getattr(pynq_mod, "allocate")
        except Exception as exc:
            raise RuntimeError(
                "pynq package is required for --crypto-mode dma on board runtime"
            ) from exc

        self._overlay = Overlay(str(bitstream))

        try:
            self._aes = getattr(self._overlay, self.config.ip_name)
        except AttributeError as exc:
            raise RuntimeError(
                f"IP instance '{self.config.ip_name}' not found in overlay"
            ) from exc

        try:
            self._dma = getattr(self._overlay, self.config.dma_name)
        except AttributeError as exc:
            raise RuntimeError(
                f"DMA instance '{self.config.dma_name}' not found in overlay"
            ) from exc

        self._allocate = allocate
        self.loaded = True

    def encrypt(self, nonce: bytes, aad: bytes, plaintext: bytes) -> Tuple[bytes, bytes]:
        """Return ciphertext and tag.

        Implement integration against the board's DMA control path.
        """
        self._require_loaded()

        if len(nonce) != 12:
            raise ValueError(f"Nonce must be 12 bytes, got {len(nonce)}")

        if not plaintext:
            raise ValueError("Plaintext must not be empty")

        self._set_stream_mode(True)

        padded_aad = _pad_to_block(aad)
        padded_plaintext = _pad_to_block(plaintext)

        self._write_key(self._key)
        self._write_nonce(nonce)
        self._write_lengths(aad_len_bits=len(aad) * 8, pt_len_bits=len(plaintext) * 8)

        self._load_key_and_wait()
        self._start_session_and_wait_ready()

        aad_blocks = max(1, len(padded_aad) // 16) if len(aad) > 0 else 0
        for index in range(aad_blocks):
            start = index * 16
            end = start + 16
            block = padded_aad[start:end]
            self._push_aad_block(block, is_last=(index == aad_blocks - 1))

        ciphertext = self._stream_pt_collect_ct_dma(padded_plaintext)[: len(plaintext)]
        tag = self._wait_tag()
        self._assert_no_drops()
        return ciphertext, tag

    def decrypt(self, nonce: bytes, aad: bytes, ciphertext: bytes, tag: bytes) -> bytes:
        """Return plaintext after tag verification.

        Implement integration against the board's DMA control path.
        """
        self._require_loaded()

        if not self.config.decrypt_supported:
            raise RuntimeError(
                "DMA decrypt path is not available in the current encrypt-only overlay"
            )

        raise NotImplementedError("Decrypt wiring not implemented for this overlay yet")

    def _require_loaded(self) -> None:
        if not self.loaded or self._aes is None or self._dma is None or self._allocate is None:
            raise RuntimeError("DMA engine not loaded; call load() first")

    def _status(self) -> int:
        return int(self._aes.read(STATUS))

    def _wait_until(self, cond: Callable[[], bool], timeout_s: float, what: str) -> None:
        t0 = time.perf_counter()
        while not cond():
            if (time.perf_counter() - t0) > timeout_s:
                s = self._status()
                raise TimeoutError(f"Timeout waiting for {what} (status=0x{s:08x})")

    def _write_block(self, base: int, block16: bytes) -> None:
        if len(block16) != 16:
            raise ValueError(f"Block must be 16 bytes, got {len(block16)}")

        for i in range(4):
            word = int.from_bytes(block16[i * 4 : (i + 1) * 4], byteorder="big")
            self._aes.write(base + i * 4, word)

    def _read_block(self, base: int) -> bytes:
        out = bytearray()
        for i in range(4):
            word = int(self._aes.read(base + i * 4))
            out.extend(word.to_bytes(4, byteorder="big"))
        return bytes(out)

    def _write_key(self, key: bytes) -> None:
        for i in range(8):
            word = int.from_bytes(key[i * 4 : (i + 1) * 4], byteorder="big")
            self._aes.write(KEY_BASE + i * 4, word)

    def _write_nonce(self, nonce12: bytes) -> None:
        for i in range(3):
            word = int.from_bytes(nonce12[i * 4 : (i + 1) * 4], byteorder="big")
            self._aes.write(NONCE_BASE + i * 4, word)

    def _write_lengths(self, aad_len_bits: int, pt_len_bits: int) -> None:
        self._aes.write(AAD_LEN_HI, (aad_len_bits >> 32) & 0xFFFFFFFF)
        self._aes.write(AAD_LEN_LO, aad_len_bits & 0xFFFFFFFF)
        self._aes.write(PT_LEN_HI, (pt_len_bits >> 32) & 0xFFFFFFFF)
        self._aes.write(PT_LEN_LO, pt_len_bits & 0xFFFFFFFF)

    def _set_stream_mode(self, enable: bool) -> None:
        self._aes.write(CTRL, CTRL_SET_STREAM)
        if enable:
            self._wait_until(
                lambda: (self._status() & STS_STREAM_MODE) != 0,
                self.config.timeout_s,
                "stream_mode=1",
            )

    def _load_key_and_wait(self) -> None:
        self._aes.write(CTRL, CTRL_LOAD_KEY)
        self._wait_until(
            lambda: (self._status() & STS_KEYS_READY_MASK) == STS_KEYS_READY_MASK,
            self.config.timeout_s,
            "keys_ready==0xF",
        )
        self._wait_until(
            lambda: (self._status() & STS_H_VALID) != 0,
            self.config.timeout_s,
            "h_valid",
        )

    def _start_session_and_wait_ready(self) -> None:
        self._wait_until(
            lambda: (self._status() & STS_SESSION_READY) != 0,
            self.config.timeout_s,
            "session_ready",
        )
        self._aes.write(CTRL, CTRL_START_SESSION)

        if self._status() & STS_SESSION_DROP:
            raise RuntimeError("Session start was dropped by hardware")

    def _push_aad_block(self, block: bytes, is_last: bool) -> None:
        self._wait_until(
            lambda: (self._status() & STS_AAD_READY) != 0,
            self.config.timeout_s,
            "aad_ready",
        )
        self._write_block(AAD_BASE, block)
        ctrl = CTRL_PUSH_AAD | (CTRL_AAD_LAST if is_last else 0)
        self._aes.write(CTRL, ctrl)

    def _stream_pt_collect_ct_dma(self, pt: bytes) -> bytes:
        tx = self._allocate(shape=(len(pt),), dtype="u1")
        rx = self._allocate(shape=(len(pt),), dtype="u1")

        try:
            tx[:] = bytearray(pt)
            tx.flush()

            self._dma.recvchannel.transfer(rx)
            self._dma.sendchannel.transfer(tx)
            self._dma.sendchannel.wait()
            self._dma.recvchannel.wait()

            rx.invalidate()
            return bytes(rx)
        finally:
            tx.freebuffer()
            rx.freebuffer()

    def _wait_tag(self) -> bytes:
        self._wait_until(
            lambda: (self._status() & STS_TAG_VALID) != 0,
            self.config.timeout_s,
            "tag_valid",
        )
        return self._read_block(TAG_BASE)

    def _assert_no_drops(self) -> None:
        s = self._status()
        if s & STS_AAD_DROP:
            raise RuntimeError("aad_drop_sticky set: AAD push attempted when aad_ready=0")
        if s & STS_PT_DROP:
            raise RuntimeError("pt_drop_sticky set: PT path rejected data")
        if s & STS_SESSION_DROP:
            raise RuntimeError("session_drop_sticky set: session start attempted when not ready")
        if s & STS_CT_FIFO_OVERFLOW:
            raise RuntimeError("ct_fifo_overflow set: CT stream path overflowed")
