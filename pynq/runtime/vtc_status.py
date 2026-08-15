"""Read-only VTC (Video Timing Controller) status dump — HDMI bring-up diagnostic.

Purpose:
  Tell us if HDMI video timing actually reaches the PL, without touching any
  RTL and without rebuilding the bitstream. Pure MMIO reads, no writes to VTC.

Why this exists:
  axi_gpio_hdmiin pixel_lock only proves the TMDS clock locked. It does not
  prove video data (sync/active video) is being decoded. vtc_in sits right
  after the video-in chain and mirrors the incoming timing, so its registers
  tell us whether real video is arriving.

Honesty note on register meaning:
  The offsets below match the standard Xilinx Video Timing Controller (v_tc)
  detector register block. Treat single-bit interpretations as best-effort.
  This tool does not ask you to trust one bit. It saves a snapshot and diffs
  it against the previous run, so the evidence is "this changed / did not
  change between two real conditions", not "trust my decode of bit 7".

Usage (on the PYNQ board, same overlay tx_daemon.py uses):

  Run 1 - HDMI source unplugged or powered off:
    python vtc_status.py --bitstream <path-to-hdmi_aes_tx.bit> --label no_source

  Run 2 - known-good source plugged in, forced to 720p60:
    python vtc_status.py --bitstream <path-to-hdmi_aes_tx.bit> --label with_source

  Read the "diff vs previous snapshot" section:
    - Registers changed        -> video timing is reaching the VTC.
    - Nothing changed          -> video timing is NOT reaching the VTC; the
                                   problem is upstream (source/EDID/cable),
                                   not the packetizer/sequencer/AES chain.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional

from tx_daemon import _load_pynq, GPIO2_DATA, GPIO2_TRI  # reuse, no 5th copy of this helper

# ---------------------------------------------------------------------------
# Xilinx Video Timing Controller (v_tc) register offsets - detector block.
# ---------------------------------------------------------------------------
REGS: Dict[str, int] = {
    "CTRL": 0x000,
    "ISR": 0x004,
    "IER": 0x008,
    "DET_STATUS": 0x060,
    "DET_TIMEBASE": 0x064,
    "DET_ENCODING": 0x068,
    "DET_HORIZ1": 0x06C,
    "DET_VERT1_F0": 0x070,
    "DET_VERT1_F1": 0x074,
    "DET_VSYNC_F0": 0x078,
    "DET_VSYNC_F1": 0x07C,
}

DEFAULT_SNAPSHOT = Path(__file__).resolve().parents[2] / "artifacts" / "vtc_status" / "last_snapshot.json"


def read_all(mmio: Any) -> Dict[str, int]:
    regs: Dict[str, int] = {}
    for name, off in REGS.items():
        print(f"[vtc_status] about to read {name} (offset 0x{off:03X})", flush=True)
        regs[name] = int(mmio.read(off)) & 0xFFFFFFFF
        print(f"[vtc_status] read {name} OK = 0x{regs[name]:08X}", flush=True)
    return regs


def _low13(v: int) -> int:
    return v & 0x1FFF


def heuristic_verdict(regs: Dict[str, int]) -> str:
    h = _low13(regs["DET_HORIZ1"])
    v = _low13(regs["DET_VERT1_F0"])
    if all(val == 0 for val in regs.values()):
        return "ALL ZERO - detector block looks unreset/unpowered or sees nothing."
    if 64 <= h <= 4096 and 64 <= v <= 4096:
        return f"HEURISTIC: plausible active size ~{h}x{v}. Compare against your real source resolution."
    return f"HEURISTIC: decoded size {h}x{v} is not a plausible active video size."


def load_snapshot(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def save_snapshot(path: Path, label: str, regs: Dict[str, int], pixel_lock: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "label": label,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "pixel_lock": pixel_lock,
        "regs": regs,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def print_report(regs: Dict[str, int], pixel_lock: int, prev: Optional[dict]) -> None:
    print("=== VTC status (read-only, no writes) ===")
    print(f"axi_gpio_hdmiin pixel_lock = {pixel_lock}")
    for name, off in REGS.items():
        print(f"  0x{off:03X} {name:14s} = 0x{regs[name]:08X}")
    print()
    print(heuristic_verdict(regs))

    if prev is None:
        print()
        print("No previous snapshot found - this is the baseline.")
        print("Run again after changing the HDMI source (plug/unplug or force 720p60)")
        print("to see what changed.")
        return

    print()
    print(f"--- diff vs previous snapshot (label={prev.get('label')!r}, {prev.get('timestamp')}) ---")
    changed = False
    prev_regs = prev.get("regs", {})
    for name in REGS:
        old = prev_regs.get(name)
        new = regs[name]
        if old is not None and int(old) != new:
            changed = True
            print(f"  {name:14s} 0x{int(old):08X} -> 0x{new:08X}")
    old_lock = prev.get("pixel_lock")
    if old_lock is not None and int(old_lock) != pixel_lock:
        changed = True
        print(f"  pixel_lock     {old_lock} -> {pixel_lock}")

    if not changed:
        print("  NOTHING changed since the previous snapshot.")
        print("  If the HDMI source condition changed between runs, this means")
        print("  video timing is NOT reaching the VTC. Look at source/EDID/cable next.")
    else:
        print("  Registers changed: video timing activity reached the VTC.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only VTC + HDMI lock status dump")
    parser.add_argument("--bitstream", required=True, help="Path to .bit overlay file (same one tx_daemon uses)")
    parser.add_argument("--label", default=None, help="Tag for this snapshot (e.g. no_source, with_source_720p60)")
    parser.add_argument("--snapshot-file", default=str(DEFAULT_SNAPSHOT), help="Where to store/compare snapshots")
    parser.add_argument("--force", action="store_true",
                        help="Read vtc_in even if pixel_lock=0 (no HDMI clock). May crash the process (Bus error).")
    args = parser.parse_args()

    pynq = _load_pynq()
    Overlay = pynq.Overlay
    MMIO = pynq.MMIO

    bit_path = Path(args.bitstream).expanduser().resolve()
    if not bit_path.exists():
        raise FileNotFoundError(f"Bitstream not found: {bit_path}")

    print(f"[vtc_status] Loading overlay: {bit_path}")
    overlay = Overlay(str(bit_path))
    print("[vtc_status] Overlay loaded OK.", flush=True)

    if "vtc_in" not in overlay.ip_dict:
        raise KeyError(f"vtc_in not found in overlay. Available: {list(overlay.ip_dict)}")
    vtc_info = overlay.ip_dict["vtc_in"]
    print(f"[vtc_status] vtc_in phys_addr=0x{vtc_info['phys_addr']:08X} addr_range=0x{vtc_info['addr_range']:X}", flush=True)
    vtc = MMIO(vtc_info["phys_addr"], vtc_info["addr_range"])
    print("[vtc_status] vtc_in MMIO mapped OK (mmap succeeded, no register read yet).", flush=True)

    pixel_lock = 0
    if "axi_gpio_hdmiin" in overlay.ip_dict:
        gpio_info = overlay.ip_dict["axi_gpio_hdmiin"]
        gpio = MMIO(gpio_info["phys_addr"], gpio_info["addr_range"])
        gpio.write(GPIO2_TRI, 0x1)
        pixel_lock = int(gpio.read(GPIO2_DATA)) & 0x1
        print(f"[vtc_status] axi_gpio_hdmiin read OK, pixel_lock={pixel_lock}", flush=True)
    else:
        print("[vtc_status] WARNING: axi_gpio_hdmiin missing; pixel_lock unavailable.")

    if pixel_lock == 0 and not args.force:
        print()
        print("[vtc_status] pixel_lock=0: no HDMI clock is present.")
        print("[vtc_status] Reading vtc_in with no live pixel clock has caused a Bus error")
        print("[vtc_status] (external abort) on this board before. Refusing to read vtc_in.")
        print("[vtc_status] Plug in and power on a real HDMI source, then re-run.")
        print("[vtc_status] Pass --force to attempt it anyway (may crash the process).")
        return

    regs = read_all(vtc)

    snapshot_path = Path(args.snapshot_file)
    prev = load_snapshot(snapshot_path)
    label = args.label or time.strftime("run_%H%M%S")

    print_report(regs, pixel_lock, prev)
    save_snapshot(snapshot_path, label, regs, pixel_lock)
    print()
    print(f"[vtc_status] Snapshot saved: {snapshot_path} (label={label})")


if __name__ == "__main__":
    main()
