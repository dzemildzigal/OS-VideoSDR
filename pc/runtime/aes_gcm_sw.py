"""Software AES-256-GCM helper for host-side encryption and decryption."""

from __future__ import annotations

from typing import Tuple


class AesGcmSoftware:
    def __init__(self, key: bytes) -> None:
        if len(key) != 32:
            raise ValueError(f"AES-256 key must be 32 bytes, got {len(key)}")

        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError as exc:
            raise RuntimeError(
                "cryptography package is required for software AES-GCM"
            ) from exc

        self._aesgcm = AESGCM(key)

    def encrypt(self, nonce: bytes, aad: bytes, plaintext: bytes) -> Tuple[bytes, bytes]:
        blob = self._aesgcm.encrypt(nonce, plaintext, aad)
        ciphertext = blob[:-16]
        tag = blob[-16:]
        return ciphertext, tag

    def decrypt(self, nonce: bytes, aad: bytes, ciphertext: bytes, tag: bytes) -> bytes:
        return self._aesgcm.decrypt(nonce, ciphertext + tag, aad)
