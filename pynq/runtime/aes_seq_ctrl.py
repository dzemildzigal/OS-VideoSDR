"""PS-side control helper for AES_GCM_Session_Sequencer (aes_seq_0).

This module provides a small register-level API for configuring runtime
packet policy (session/stream/key-id/nonce/payload bytes) and key rotation.
"""

from __future__ import annotations

import argparse
import importlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict


REG_CTRL = 0x00
REG_STATUS = 0x04
REG_SESSION_ID = 0x08
REG_STREAM_PAYLOAD = 0x0C
REG_NONCE_DOMAIN = 0x10
REG_NONCE_SEED_HI = 0x14
REG_NONCE_SEED_LO = 0x18
REG_PAYLOAD_BYTES = 0x1C
REG_KEY0 = 0x20
REG_KEY1 = 0x24
REG_KEY2 = 0x28
REG_KEY3 = 0x2C
REG_KEY4 = 0x30
REG_KEY5 = 0x34
REG_KEY6 = 0x38
REG_KEY7 = 0x3C
REG_NONCE_CUR_HI = 0x40
REG_NONCE_CUR_LO = 0x44

CTRL_ENABLE = 1 << 0
CTRL_LOAD_KEY_REQ = 1 << 1
CTRL_APPLY_NONCE_SEED = 1 << 2
CTRL_FORCE_KEY_DIRTY = 1 << 3


def _import_board_pynq() -> Any:
    pynq_mod = importlib.import_module("pynq")
    if hasattr(pynq_mod, "Overlay") and hasattr(pynq_mod, "MMIO"):
        return pynq_mod

    project_root = Path(__file__).resolve().parents[2]
    shadow_paths = {str(project_root.resolve()), str((project_root / "pynq").resolve())}

    def _norm(value: str) -> str:
        try:
            return str(Path(value if value else ".").resolve())
        except Exception:
            return value

    saved = list(importlib.import_module("sys").path)
    try:
        importlib.import_module("sys").modules.pop("pynq", None)
        importlib.import_module("sys").path = [p for p in saved if _norm(p) not in shadow_paths]
        pynq_mod = importlib.import_module("pynq")
    finally:
        importlib.import_module("sys").path = saved

    if not hasattr(pynq_mod, "Overlay") or not hasattr(pynq_mod, "MMIO"):
        mod_file = getattr(pynq_mod, "__file__", "<unknown>")
        raise RuntimeError(f"pynq package is missing Overlay/MMIO support: {mod_file}")

    return pynq_mod


@dataclass(slots=True)
class AesSeqConfig:
    session_id: int = 1
    stream_id: int = 1
    payload_type: int = 1
    key_id: int = 1
    nonce_domain: int = 1
    nonce_seed: int = 1
    payload_bytes: int = 1200
    enable: bool = True


class AesSeqController:
    def __init__(self, overlay: Any, ip_name: str = "aes_seq_0") -> None:
        if ip_name not in overlay.ip_dict:
            available = ", ".join(sorted(overlay.ip_dict.keys()))
            raise KeyError(f"{ip_name} not found in overlay.ip_dict. Available: {available}")

        pynq_mod = _import_board_pynq()
        MMIO = getattr(pynq_mod, "MMIO")
        info = overlay.ip_dict[ip_name]
        self._mmio = MMIO(info["phys_addr"], info["addr_range"])
        self._ctrl_enable = True

    def write(self, offset: int, value: int) -> None:
        self._mmio.write(offset, value & 0xFFFFFFFF)

    def read(self, offset: int) -> int:
        return int(self._mmio.read(offset) & 0xFFFFFFFF)

    def configure(self, cfg: AesSeqConfig) -> None:
        self.write(REG_SESSION_ID, cfg.session_id)
        stream_payload = ((cfg.key_id & 0xFF) << 24) | ((cfg.payload_type & 0xFF) << 16) | (cfg.stream_id & 0xFFFF)
        self.write(REG_STREAM_PAYLOAD, stream_payload)
        self.write(REG_NONCE_DOMAIN, cfg.nonce_domain)
        self.write(REG_NONCE_SEED_HI, (cfg.nonce_seed >> 32) & 0xFFFFFFFF)
        self.write(REG_NONCE_SEED_LO, cfg.nonce_seed & 0xFFFFFFFF)
        self.write(REG_PAYLOAD_BYTES, cfg.payload_bytes & 0xFFFF)
        self._ctrl_enable = bool(cfg.enable)
        self.write(REG_CTRL, CTRL_ENABLE if cfg.enable else 0)

    def set_key_hex(self, key_hex: str) -> None:
        key = bytes.fromhex(key_hex)
        if len(key) != 32:
            raise ValueError(f"AES-256 key must be 32 bytes, got {len(key)}")

        for i in range(8):
            word = int.from_bytes(key[i * 4 : (i + 1) * 4], "big")
            self.write(REG_KEY0 + (i * 4), word)

    def request_key_load(self) -> None:
        ctrl = CTRL_LOAD_KEY_REQ
        if self._ctrl_enable:
            ctrl |= CTRL_ENABLE
        self.write(REG_CTRL, ctrl)

    def apply_nonce_seed(self) -> None:
        ctrl = CTRL_APPLY_NONCE_SEED
        if self._ctrl_enable:
            ctrl |= CTRL_ENABLE
        self.write(REG_CTRL, ctrl)

    def force_key_dirty(self) -> None:
        ctrl = CTRL_FORCE_KEY_DIRTY
        if self._ctrl_enable:
            ctrl |= CTRL_ENABLE
        self.write(REG_CTRL, ctrl)

    def read_status(self) -> Dict[str, int]:
        status = self.read(REG_STATUS)
        nonce_hi = self.read(REG_NONCE_CUR_HI)
        nonce_lo = self.read(REG_NONCE_CUR_LO)
        nonce = (nonce_hi << 32) | nonce_lo
        # REG_STATUS packs control bits and a compact mirror of AES status:
        #   [12:0]  = aes_status[12:0]
        #   [13]    = cfg_enable
        #   [14]    = seq_busy
        #   [15]    = key_dirty
        #   [22:19] = aes_status[19:16]
        aes_status = (status & 0x1FFF) | (((status >> 19) & 0xF) << 16)
        return {
            "status_raw": status,
            "enabled": (status >> 13) & 0x1,
            "seq_busy": (status >> 14) & 0x1,
            "key_dirty": (status >> 15) & 0x1,
            "nonce_counter": nonce,
            "aes_status_raw": aes_status,
            "aes_keys_ready": aes_status & 0xF,
            "aes_session_ready": (aes_status >> 4) & 0x1,
            "aes_aad_ready": (aes_status >> 5) & 0x1,
            "aes_pt_ready": (aes_status >> 6) & 0x1,
            "aes_busy": (aes_status >> 7) & 0x1,
            "aes_h_valid": (aes_status >> 8) & 0x1,
            "aes_stream_mode": (aes_status >> 17) & 0x1,
        }


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Configure aes_seq_0 registers from PS")
    parser.add_argument("--bitstream", required=True, help="Path to .bit overlay")
    parser.add_argument("--ip-name", default="aes_seq_0", help="Sequencer IP instance name")
    parser.add_argument("--key-hex", default="", help="Optional 64-char AES-256 key in hex")
    parser.add_argument("--session-id", type=int, default=1)
    parser.add_argument("--stream-id", type=int, default=1)
    parser.add_argument("--payload-type", type=int, default=1)
    parser.add_argument("--key-id", type=int, default=1)
    parser.add_argument("--nonce-domain", type=lambda x: int(x, 0), default=1)
    parser.add_argument("--nonce-seed", type=lambda x: int(x, 0), default=1)
    parser.add_argument("--payload-bytes", type=int, default=1200)
    parser.add_argument("--disable", action="store_true")
    return parser.parse_args()


def main() -> None:
    args = _parse_args()
    pynq_mod = _import_board_pynq()
    Overlay = getattr(pynq_mod, "Overlay")

    bit_path = Path(args.bitstream).expanduser().resolve()
    if not bit_path.exists():
        raise FileNotFoundError(f"Bitstream not found: {bit_path}")

    overlay = Overlay(str(bit_path))
    seq = AesSeqController(overlay, ip_name=args.ip_name)

    cfg = AesSeqConfig(
        session_id=args.session_id,
        stream_id=args.stream_id,
        payload_type=args.payload_type,
        key_id=args.key_id,
        nonce_domain=args.nonce_domain,
        nonce_seed=args.nonce_seed,
        payload_bytes=args.payload_bytes,
        enable=(not args.disable),
    )
    seq.configure(cfg)

    if args.key_hex:
        seq.set_key_hex(args.key_hex)
        seq.request_key_load()

    seq.apply_nonce_seed()
    print(seq.read_status())


if __name__ == "__main__":
    main()
