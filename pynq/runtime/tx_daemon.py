"""HDMI AES TX daemon.

Loads the PYNQ overlay and configures the AES session sequencer, then
holds the overlay open for the B.3 tx_shim process.

Usage:
  python tx_daemon.py \\
    --bitstream /home/xilinx/overlays/hdmi_aes_tx/hdmi_aes_tx_wrapper.bit \\
    --dst-host 192.168.2.100 --dst-port 5600 \\
    --key-hex 000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f

The daemon:
  1. Loads the bitstream and programs the sequencer (key / session / nonce),
     leaving it DISABLED until tx_shim enables it.
  2. Pulses HDMI HPD and reads pixel lock.
  3. In configure-only mode, holds the overlay open and waits.

tx_shim (a separate native-XRT C++ process, not this daemon) now owns the
B.2 DDR ring and control-page buffers: it allocates them as XRT buffer
objects, writes their physical addresses into frame_writer_0's registers,
and enables the ring writer, before enabling the sequencer. This daemon
never allocates ring memory and never touches frame_writer_0's registers.
See tx_shim.cpp for why: a single owner with one consistently-attributed
mapping replaces the previous split ownership (this daemon allocating,
tx_shim separately mapping the same physical pages via /dev/mem), which
risked ARM's undefined "mismatched memory attribute" condition.
"""

from __future__ import annotations

import argparse
import importlib
import sys
import time
from pathlib import Path
from typing import Any

# AXI AES-GCM stream register map (subset)
AES_REG_STATUS        = 0x0004

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

    bit_path = Path(args.bitstream).expanduser().resolve()

    # Cross-check the software payload size against the hardware geometry before
    # touching the PL. The authenticated body is 8 (nonce prefix) + 40 (header)
    # + payload + 16 (tag), and the ring writer writes exactly that many bytes
    # per slot. If --payload-bytes disagrees, the packetizer emits a wrong
    # header length and, worse, the AES core computes its GHASH length from it,
    # so every packet fails authentication while the ciphertext still looks
    # perfect. That cost two hours of debugging once; it must not happen twice.
    hwh_path = bit_path.with_suffix(".hwh")
    if hwh_path.exists():
        import re

        handoff = hwh_path.read_text(errors="ignore")
        found = re.findall(r'PACKET_BYTES"?\s+VALUE="(\d+)"', handoff)
        if found:
            hw_body = int(found[0])
            sw_body = args.payload_bytes + 64
            if sw_body != hw_body:
                raise SystemExit(
                    f"[tx_daemon] payload mismatch: --payload-bytes {args.payload_bytes} "
                    f"implies an authenticated body of {sw_body} bytes, but {hwh_path.name} "
                    f"was built for {hw_body}. Set --payload-bytes {hw_body - 64} "
                    f"(or use the matching bitstream)."
                )
            print(
                f"[tx_daemon] geometry check: payload {args.payload_bytes} + 64 = "
                f"{sw_body} bytes matches the handoff"
            )
        else:
            print(f"[tx_daemon] WARNING: {hwh_path.name} has no PACKET_BYTES to check")
    else:
        print(f"[tx_daemon] WARNING: no handoff next to {bit_path.name}; skipping geometry check")
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
    hdmi_gpio = None
    if "axi_gpio_hdmiin" in overlay.ip_dict:
        gpio_info = overlay.ip_dict["axi_gpio_hdmiin"]
        hdmi_gpio = HdmiFrontEndGpio(pynq.MMIO(gpio_info["phys_addr"], gpio_info["addr_range"]))
        if args.force_hpd:
            # Force the source to re-read the EDID after each overlay load,
            # so an already-connected source always renegotiates against the
            # current EDID rather than keeping a stale prior mode. The board
            # now advertises the stock 720p60 EDID (see the AES repository's
            # build script); 30 fps is produced by 2:1 packetizer decimation
            # against that real 60 Hz source, not by a custom 30 Hz EDID.
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

    if args.configure_only:
        # tx_shim owns the B.2 ring/control buffers and frame_writer_0's
        # registers now (see the module docstring). This process only
        # holds the overlay and the sequencer configuration open.
        print(
            "[tx_daemon] configure-only mode: overlay and sequencer held; "
            "ring ownership and writer/sequencer enable belong to tx_shim now",
            flush=True,
        )
        while True:
            time.sleep(3600)



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
    p.add_argument("--payload-bytes",type=int, default=1320,
                   help="Packet payload bytes (default: 1320). Must make header+payload a "
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
