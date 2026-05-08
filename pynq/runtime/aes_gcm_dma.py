"""PYNQ AES-GCM DMA adapter interface.

This module defines the runtime contract between endpoint code and the
board-side AES-GCM DMA implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Tuple


@dataclass(slots=True)
class DmaCryptoConfig:
    bitstream_path: str
    ip_name: str = "aes_gcm_0"
    dma_name: str = "axi_dma_0"


class AesGcmDmaEngine:
    def __init__(self, config: DmaCryptoConfig) -> None:
        self.config = config
        self.loaded = False

    def load(self) -> None:
        """Load overlay and bind DMA resources."""
        self.loaded = True

    def encrypt(self, nonce: bytes, aad: bytes, plaintext: bytes) -> Tuple[bytes, bytes]:
        """Return ciphertext and tag.

        Implement integration against the board's DMA control path.
        """
        raise NotImplementedError("Wire this to AES-GCM DMA encrypt path")

    def decrypt(self, nonce: bytes, aad: bytes, ciphertext: bytes, tag: bytes) -> bytes:
        """Return plaintext after tag verification.

        Implement integration against the board's DMA control path.
        """
        raise NotImplementedError("Wire this to AES-GCM DMA decrypt path")
