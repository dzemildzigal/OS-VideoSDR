"""Crypto adapter for board-side runtime.

Supports passthrough, software AES-GCM, or DMA-backed AES-GCM.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple

from .aes_gcm_dma import AesGcmDmaEngine, DmaCryptoConfig


@dataclass(slots=True)
class CryptoConfig:
    mode: str
    key_hex: str
    bitstream_path: str = ""


class CryptoAdapter:
    def __init__(self, cfg: CryptoConfig) -> None:
        self._cfg = cfg
        self._sw = None
        self._dma = None

    def load(self) -> None:
        if self._cfg.mode == "none":
            return

        if self._cfg.mode == "dma":
            self._dma = AesGcmDmaEngine(
                DmaCryptoConfig(
                    bitstream_path=self._cfg.bitstream_path,
                    key_hex=self._cfg.key_hex,
                )
            )
            self._dma.load()
            return

        if self._cfg.mode == "aesgcm":
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM

            key = bytes.fromhex(self._cfg.key_hex)
            if len(key) != 32:
                raise ValueError("AES-256 key must be 32 bytes")
            self._sw = AESGCM(key)
            return

        raise ValueError(f"Unsupported crypto mode: {self._cfg.mode}")

    def encrypt(self, nonce: bytes, aad: bytes, payload: bytes) -> Tuple[bytes, bytes]:
        if self._cfg.mode == "none":
            return payload, b"\x00" * 16

        if self._cfg.mode == "dma":
            assert self._dma is not None
            return self._dma.encrypt(nonce=nonce, aad=aad, plaintext=payload)

        assert self._sw is not None
        blob = self._sw.encrypt(nonce, payload, aad)
        return blob[:-16], blob[-16:]

    def decrypt(self, nonce: bytes, aad: bytes, payload: bytes, tag: bytes) -> bytes:
        if self._cfg.mode == "none":
            return payload

        if self._cfg.mode == "dma":
            assert self._dma is not None
            return self._dma.decrypt(nonce=nonce, aad=aad, ciphertext=payload, tag=tag)

        assert self._sw is not None
        return self._sw.decrypt(nonce, payload + tag, aad)

    def close(self) -> None:
        if self._dma is not None:
            self._dma.close()
