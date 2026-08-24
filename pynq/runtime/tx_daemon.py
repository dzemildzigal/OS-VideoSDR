"""HDMI AES TX daemon.

Loads the PYNQ overlay, configures the AES session sequencer and the B.2 DDR
packet ring, then holds the overlay and buffers for the B.3 C GSO sender."

Usage:
  python tx_daemon.py \\
    --bitstream /home/xilinx/overlays/hdmi_aes_tx/hdmi_aes_tx_wrapper.bit \\
    --dst-host 192.168.2.100 --dst-port 5600 \\
    --key-hex 000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f

The daemon:
  1. Loads the bitstream and programs the sequencer (key / session / nonce).
  2. Allocates one contiguous 2048-slot ring and one separate 4 KiB control
     page via pynq.allocate, then programs both physical addresses into the
     B.2 DDRRingWriter.
  3. Enables stream-source mode. The B.2 writer publishes complete 1280-byte
     slots; the B.3 tx_shim drains them with UDP GSO.
  4. In configure-only mode, holds the overlay and allocations open until
     tx_shim exits or the daemon receives Ctrl-C.
"""

from __future__ import annotations

import argparse
import importlib
import sys
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# DDRRingWriter / frame_writer_0 register offsets
# ---------------------------------------------------------------------------
REG_VERSION           = 0x0000
REG_CONTROL           = 0x0004   # [0] enable
REG_STATUS            = 0x0008   # [0] enabled [1] fault
REG_RING_BASE_LO      = 0x000C
REG_RING_BASE_HI      = 0x0010
REG_CTRL_BASE_LO      = 0x0014
REG_CTRL_BASE_HI      = 0x0018
REG_RING_LOG2         = 0x001C
REG_SLOT_STRIDE       = 0x0020
REG_PRODUCE_IDX       = 0x0024
REG_CONSUME_SHADOW    = 0x0028
REG_DROP_COUNT        = 0x002C
REG_COMPLETE_COUNT_LO = 0x0030
REG_COMPLETE_COUNT_HI = 0x0034
REG_FAULT_CODE        = 0x003C
REG_WRITER_STATUS     = 0x0008

# AXI AES-GCM stream register map (subset)
AES_REG_STATUS        = 0x0004

RING_LOG2 = 11
RING_SLOTS = 1 << RING_LOG2
SLOT_STRIDE = 1280
RING_BYTES = RING_SLOTS * SLOT_STRIDE
CONTROL_PAGE_BYTES = 4096

# AXI GPIO register map (xilinx.com:ip:axi_gpio:2.0)
GPIO_DATA     = 0x00  # channel 1 data (used for hdmi_in_hpd)
GPIO_TRI      = 0x04  # channel 1 tri-state (0=output)
GPIO2_DATA    = 0x08  # channel 2 data (used for aPixelClkLckd input)
GPIO2_TRI     = 0x0C  # channel 2 tri-state (1=input)


def _load_pynq() -> Any:
    """Import the board pynq package, bypassing any local shadow named 'pynq'."""
    pynq_mod = importlib.import_module("pynq")
    if hasattr(pynq_mod, "Overlay") and hasattr(pynq_mod, "MMIO"):
        return pynq_mod

    project_root = Path(__file__).resolve().parents[2]
    shadow_paths = {str(project_root.resolve()), str((project_root / "pynq").resolve())}

    def _norm(p: str) -> str:
        try:
            return str(Path(p if p else ".").resolve())
        except Exception:
            return p

    saved = list(sys.path)
    try:
        sys.modules.pop("pynq", None)
        sys.path = [p for p in saved if _norm(p) not in shadow_paths]
        pynq_mod = importlib.import_module("pynq")
    finally:
        sys.path = saved

    if not hasattr(pynq_mod, "Overlay") or not hasattr(pynq_mod, "MMIO"):
        raise RuntimeError(f"pynq package missing Overlay/MMIO: {getattr(pynq_mod, '__file__', '?')}")

    return pynq_mod


class DdrRingWriter:
    """Register-level driver for frame_writer_0 (B.2 DDRRingWriter)."""

    def __init__(self, mmio: Any) -> None:
        self._m = mmio

    def wr(self, off: int, val: int) -> None:
        self._m.write(off, int(val) & 0xFFFF_FFFF)

    def rd(self, off: int) -> int:
        return int(self._m.read(off)) & 0xFFFF_FFFF

    def soft_reset(self) -> None:
        self.wr(REG_CONTROL, 0)
        time.sleep(0.001)

    def configure_ring(self, ring_phys: int, ctrl_phys: int) -> None:
        self.wr(REG_RING_BASE_LO, ring_phys & 0xFFFF_FFFF)
        self.wr(REG_RING_BASE_HI, (ring_phys >> 32) & 0xFFFF_FFFF)
        self.wr(REG_CTRL_BASE_LO, ctrl_phys & 0xFFFF_FFFF)
        self.wr(REG_CTRL_BASE_HI, (ctrl_phys >> 32) & 0xFFFF_FFFF)

    def enable_stream_writer(self) -> None:
        self.wr(REG_CONTROL, 0x1)

    def ring_config(self) -> dict:
        ring = self.rd(REG_RING_BASE_LO) | (self.rd(REG_RING_BASE_HI) << 32)
        ctrl = self.rd(REG_CTRL_BASE_LO) | (self.rd(REG_CTRL_BASE_HI) << 32)
        return {
            "ring_base": ring,
            "ctrl_base": ctrl,
            "ring_log2": self.rd(REG_RING_LOG2),
            "slot_stride": self.rd(REG_SLOT_STRIDE),
        }

    def writer_status(self) -> dict:
        ws = self.rd(REG_STATUS)
        return {"enabled": ws & 0x1, "fault": (ws >> 1) & 0x1}


class HdmiFrontEndGpio:
    """Minimal helper for axi_gpio_hdmiin: HPD output and lock input."""

    def __init__(self, mmio: Any) -> None:
        self._m = mmio

    def wr(self, off: int, val: int) -> None:
        self._m.write(off, int(val) & 0xFFFF_FFFF)

    def rd(self, off: int) -> int:
        return int(self._m.read(off)) & 0xFFFF_FFFF

    def set_hpd(self, asserted: bool) -> None:
        # Channel 1 is configured as output in BD, but enforce it anyway.
        self.wr(GPIO_TRI, 0x0)
        self.wr(GPIO_DATA, 0x1 if asserted else 0x0)

    def pixel_lock(self) -> int:
        # Channel 2 is configured as input and carries dvi2rgb_0/aPixelClkLckd.
        self.wr(GPIO2_TRI, 0x1)
        return self.rd(GPIO2_DATA) & 0x1


class AesCoreStatus:
    """Read/format AXI_AES_GCM_Stream STATUS register."""

    def __init__(self, mmio: Any) -> None:
        self._m = mmio

    def raw(self) -> int:
        return int(self._m.read(AES_REG_STATUS)) & 0xFFFF_FFFF

    def decode(self) -> dict:
        v = self.raw()
        return {
            "raw": v,
            "keys_ready": v & 0xF,
            "session_ready": (v >> 4) & 0x1,
            "aad_ready": (v >> 5) & 0x1,
            "pt_ready": (v >> 6) & 0x1,
            "busy": (v >> 7) & 0x1,
            "h_valid": (v >> 8) & 0x1,
            "stream_mode": (v >> 17) & 0x1,
            "ct_fifo_overflow": (v >> 18) & 0x1,
        }


def run(args: argparse.Namespace) -> None:
    pynq = _load_pynq()
    Overlay = pynq.Overlay
    allocate = pynq.allocate

    bit_path = Path(args.bitstream).expanduser().resolve()
    if not bit_path.exists():
        raise FileNotFoundError(f"Bitstream not found: {bit_path}")
    if not args.configure_only:
        raise ValueError(
            "B.3 requires --configure-only; tx_shim is the only active UDP sender"
        )

    print(f"[tx_daemon] Loading overlay: {bit_path}")
    overlay = Overlay(str(bit_path))
    print("[tx_daemon] Overlay loaded.")

    # --- Select the design-domain clock frequency (50/75/100 MHz) ---
    # The PL derives the design clock from the fixed 100 MHz FCLK0 through an
    # MMCM + BUFGMUX_CTRL. axi_gpio_clkctrl sits on the STABLE FCLK0 branch of
    # the interconnect, so it stays reachable while the switched domain is
    # held in reset:
    #   gpio[0]   = 1 -> design domain held in reset (proc_sys_reset aux)
    #   gpio[2:1] = aes_clk_mux select (00=50, 01=75, 10=100 MHz)
    # Procedure: assert reset -> change select -> settle -> release reset.
    # MUST run before any AXI-Lite traffic to the design-domain slaves.
    aes_freq = args.aes_freq
    if "axi_gpio_clkctrl" in overlay.ip_dict:
        clk_info = overlay.ip_dict["axi_gpio_clkctrl"]
        clk_ctrl = pynq.MMIO(clk_info["phys_addr"], clk_info["addr_range"])
        sel_map = {50: 0b00, 75: 0b01, 100: 0b10}
        if aes_freq not in sel_map:
            raise ValueError(f"Unsupported --aes-freq {aes_freq}; choose 50, 75 or 100")
        sel = sel_map[aes_freq]
        # The old assert-reset -> switch -> release sequence is self-
        # defeating on this board: bit0 feeds rst_ps7_100m/aux_reset_in and
        # the reset gates the AXI branch to this very gpio, so the sel write
        # lands while the register is held in reset and reads back 0
        # (verified live: write 0x4 with bit0 untouched sticks; the daemon's
        # 0x1 -> 0x5 -> 0x4 sequence left DATA=0 and the design at 50 MHz).
        # BUFGMUX_CTRL switches glitchlessly on its own - just drive sel.
        clk_ctrl.write(0x00, sel << 1)
        time.sleep(0.050)
        got = (int(clk_ctrl.read(0x00)) >> 1) & 0x3
        if got != sel:
            raise RuntimeError(f"clkctrl sel readback 0b{got:02b} != 0b{sel:02b}"
                               f" @ 0x{clk_info['phys_addr']:08X}")
        print(f"[tx_daemon] Design clock set to {aes_freq} MHz (mux sel=0b{sel:02b}, "
              f"readback=0b{got:02b}).")
    else:
        print("[tx_daemon] WARNING: axi_gpio_clkctrl missing; running at the bitstream default clock.")

    # --- Configure AES session sequencer ---
    from aes_seq_ctrl import AesSeqController, AesSeqConfig  # type: ignore

    seq = AesSeqController(overlay, ip_name="aes_seq_0")
    cfg = AesSeqConfig(
        session_id=args.session_id,
        stream_id=args.stream_id,
        payload_type=args.payload_type,
        key_id=args.key_id,
        nonce_domain=args.nonce_domain,
        nonce_seed=args.nonce_seed,
        payload_bytes=args.payload_bytes,
        # configure-only: leave the sequencer DISABLED; tx_shim enables it
        # once it is draining, so the nonce counter cannot run ahead of the
        # writer while nobody drains (that gap breaks the -1/-2 pairing).
        enable=not args.configure_only,
    )
    seq.configure(cfg)
    if args.key_hex:
        seq.set_key_hex(args.key_hex)
        seq.request_key_load()
    seq.apply_nonce_seed()
    seq_status = seq.read_status()
    print(
        "[tx_daemon] Sequencer configured: "
        f"raw=0x{seq_status['status_raw']:08X} "
        f"enabled={seq_status['enabled']} busy={seq_status['seq_busy']} "
        f"key_dirty={seq_status['key_dirty']} nonce={seq_status['nonce_counter']}"
    )

    # --- Set up the B.2 DDR packet ring writer ---
    if "frame_writer_0" not in overlay.ip_dict:
        raise KeyError(f"frame_writer_0 not in overlay. Available: {list(overlay.ip_dict)}")

    fw_info = overlay.ip_dict["frame_writer_0"]
    import numpy as np  # noqa: PLC0415
    fw = DdrRingWriter(pynq.MMIO(fw_info["phys_addr"], fw_info["addr_range"]))
    fw.soft_reset()

    hdmi_gpio = None
    if "axi_gpio_hdmiin" in overlay.ip_dict:
        gpio_info = overlay.ip_dict["axi_gpio_hdmiin"]
        hdmi_gpio = HdmiFrontEndGpio(pynq.MMIO(gpio_info["phys_addr"], gpio_info["addr_range"]))
        if args.force_hpd:
            # Force the source to re-read the EDID after each overlay load.
            # Without a real HPD transition, an already-connected source can
            # keep its previous 720p60 mode.
            hdmi_gpio.set_hpd(False)
            time.sleep(0.250)
            hdmi_gpio.set_hpd(True)
            time.sleep(1.000)
            print("[tx_daemon] HDMI HPD pulsed and asserted via axi_gpio_hdmiin.")
        print(f"[tx_daemon] HDMI pixel lock={hdmi_gpio.pixel_lock()}")
    else:
        print("[tx_daemon] WARNING: axi_gpio_hdmiin missing; cannot drive HPD or read lock.")

    aes_dbg = None
    if "aes_gcm_0" in overlay.ip_dict:
        aes_info = overlay.ip_dict["aes_gcm_0"]
        aes_dbg = AesCoreStatus(pynq.MMIO(aes_info["phys_addr"], aes_info["addr_range"]))
        aes_s = aes_dbg.decode()
        print(
            "[tx_daemon] AES status "
            f"raw=0x{aes_s['raw']:08X} keys_ready=0x{aes_s['keys_ready']:X} "
            f"session_ready={aes_s['session_ready']} pt_ready={aes_s['pt_ready']} "
            f"stream_mode={aes_s['stream_mode']} h_valid={aes_s['h_valid']}"
        )
    else:
        print("[tx_daemon] WARNING: aes_gcm_0 missing; cannot read AES status.")

    # B.2 writes one 1240-byte authenticated body plus 40 zero transport
    # bytes into each 1280-byte slot. Allocate the ring and its control page
    # separately so the PS can map the control words independently.
    ring_buf = allocate(shape=(RING_BYTES,), dtype=np.uint8)
    ctrl_buf = allocate(shape=(CONTROL_PAGE_BYTES,), dtype=np.uint8)
    ring_phys = int(ring_buf.physical_address)
    ctrl_phys = int(ctrl_buf.physical_address)
    if ring_phys & 0x7F:
        raise RuntimeError(f"B.2 ring allocation is not 128-byte aligned: 0x{ring_phys:X}")
    if ctrl_phys & 0xFFF:
        raise RuntimeError(f"B.2 control allocation is not page aligned: 0x{ctrl_phys:X}")
    if ((ring_phys <= ctrl_phys < ring_phys + RING_BYTES) or
            (ctrl_phys <= ring_phys < ctrl_phys + CONTROL_PAGE_BYTES)):
        raise RuntimeError("B.2 control page overlaps the ring allocation")
    ctrl_buf[:] = 0
    if hasattr(ctrl_buf, "flush"):
        ctrl_buf.flush()
    print(f"[tx_daemon] DDR ring @ 0x{ring_phys:08X} ({RING_SLOTS} x {SLOT_STRIDE} = {RING_BYTES} bytes)")
    print(f"[tx_daemon] DDR ctrl @ 0x{ctrl_phys:08X} ({CONTROL_PAGE_BYTES} bytes)")

    fw.configure_ring(ring_phys, ctrl_phys)
    ring_cfg = fw.ring_config()
    if ring_cfg != {
        "ring_base": ring_phys,
        "ctrl_base": ctrl_phys,
        "ring_log2": RING_LOG2,
        "slot_stride": SLOT_STRIDE,
    }:
        raise RuntimeError(f"B.2 ring register readback mismatch: {ring_cfg}")
    fw.enable_stream_writer()
    print(f"[tx_daemon] DDR ring writer enabled. Config={ring_cfg} status={fw.writer_status()}")

    if args.configure_only:
        # Hold the overlay and allocations open so tx_shim can map and drain
        # the ring through the writer's physical-address registers.
        print(
            "[tx_daemon] configure-only mode: ring held, no Python send loop; "
            "sequencer DISABLED until tx_shim enables it",
            flush=True,
        )
        try:
            while True:
                time.sleep(3600)
        finally:
            fw.wr(REG_CONTROL, 0)
            ring_buf.freebuffer()
            ctrl_buf.freebuffer()



def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="HDMI AES TX daemon – board-side sender")
    p.add_argument("--bitstream", required=True, help="Path to .bit overlay file")
    p.add_argument("--dst-host",  required=True, help="Destination IP address")
    p.add_argument("--dst-port",  type=int, default=5600, help="Destination UDP port")
    # Sequencer params
    p.add_argument("--key-hex",      default="", help="64-char AES-256 key hex")
    p.add_argument("--session-id",   type=int, default=1)
    p.add_argument("--stream-id",    type=int, default=1)
    p.add_argument("--payload-type", type=int, default=1)
    p.add_argument("--key-id",       type=int, default=1)
    p.add_argument("--nonce-domain", type=lambda x: int(x, 0), default=1)
    p.add_argument("--nonce-seed",   type=lambda x: int(x, 0), default=1)
    p.add_argument("--payload-bytes",type=int, default=1176,
                   help="Packet payload bytes (default: 1176). Must make header+payload a "
                        "multiple of 16: the AES stream input only accepts full 16-byte "
                        "beats (pt_keep_ok = TKEEP==0xFFFF), so 40+payload must be %16==0.")
    p.add_argument("--force-hpd", action="store_true", default=True,
                   help="Assert HDMI HPD via axi_gpio_hdmiin (default: on)")
    p.add_argument("--no-force-hpd", action="store_false", dest="force_hpd",
                   help="Do not drive HDMI HPD from software")
    p.add_argument("--status-interval", type=float, default=1.0,
                   help="Seconds between idle status prints (0 disables)")
    p.add_argument("--configure-only", action="store_true",
                   help="Configure overlay/sequencer/writer, then wait for an external C sender (tx_shim)")
    p.add_argument("--aes-freq", type=int, default=50, choices=[50, 75, 100],
                   help="Design-domain clock frequency in MHz (default: 50). Switches the PL MMCM+BUFGMUX at runtime; no rebuild needed.")
    p.add_argument("--idle-exit-s", type=float, default=0.0,
                   help="Exit after this many seconds with no ready buffers (0 disables)")
    return p.parse_args()


if __name__ == "__main__":
    run(_parse_args())
