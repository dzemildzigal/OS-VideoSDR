"""HDMI AES TX daemon.

Loads the PYNQ overlay, configures the AES session sequencer and the PingPong
DDR writer, then continuously drains encrypted packets from DDR and fires them
as UDP datagrams over Ethernet.

Usage:
  python tx_daemon.py \\
    --bitstream /home/xilinx/overlays/hdmi_aes_tx/hdmi_aes_tx_wrapper.bit \\
    --dst-host 192.168.2.100 --dst-port 5600 \\
    --key-hex 000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f

The daemon:
  1. Loads the bitstream and programs the sequencer (key / session / nonce).
  2. Allocates two contiguous DDR buffers via pynq.allocate, programs their
     physical addresses into frame_writer_0.
  3. Enables stream-source mode so the DDR writer consumes AES ciphertext.
  4. Loops polling READY_MASK; when a buffer is ready it copies bytes out,
     splits them into MTU-sized UDP datagrams and sends, then marks consumed.
"""

from __future__ import annotations

import argparse
import importlib
import socket
import sys
import time
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# PingPong / frame_writer_0 register offsets
# ---------------------------------------------------------------------------
REG_VERSION           = 0x0000
REG_CONTROL           = 0x0004   # [0] enable  [1] soft_reset_pulse
REG_STATUS            = 0x0008   # [0] running  [1] fault
REG_FRAME_BYTES_CFG   = 0x0010
REG_WRITE_INDEX       = 0x0014
REG_READY_MASK        = 0x0018   # [1:0] buffer readiness flags
REG_CONSUMED_MASK     = 0x001C   # RW1C – write 1 to mark buffer consumed
REG_FRAME_ID_BUF0     = 0x0020
REG_FRAME_ID_BUF1     = 0x0024
REG_VALID_BYTES_BUF0  = 0x0028
REG_VALID_BYTES_BUF1  = 0x002C
REG_DROP_COUNT        = 0x0030
REG_IRQ_ENABLE        = 0x0034   # [0] enable IRQ output
REG_IRQ_STATUS        = 0x0038   # RW1C – write 1 to clear
REG_WRITER_ENABLE     = 0x0040   # [0] enable deterministic writer path
REG_BUF0_ADDR_LO      = 0x0044
REG_BUF0_ADDR_HI      = 0x0048
REG_BUF1_ADDR_LO      = 0x004C
REG_BUF1_ADDR_HI      = 0x0050
REG_WRITER_STATUS     = 0x0054   # [0] busy  [1] fault  [2] writer_enable
REG_WRITER_ERROR_COUNT= 0x0058
REG_WRITER_CMD        = 0x005C   # RW1C [0] clear_fault [1] clear_error_count
REG_WRITER_SRC_SEL    = 0x0060   # [0] 0=deterministic pattern  1=AXI-Stream

# AXI AES-GCM stream register map (subset)
AES_REG_STATUS        = 0x0004

# AXI GPIO register map (xilinx.com:ip:axi_gpio:2.0)
GPIO_DATA     = 0x00  # channel 1 data (used for hdmi_in_hpd)
GPIO_TRI      = 0x04  # channel 1 tri-state (0=output)
GPIO2_DATA    = 0x08  # channel 2 data (used for aPixelClkLckd input)
GPIO2_TRI     = 0x0C  # channel 2 tri-state (1=input)

UDP_MAX_PAYLOAD = 1400  # bytes per datagram – stay under typical MTU


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


class PingPongCtrl:
    """Thin register-level driver for frame_writer_0 (AXI_PingPong_Ctrl)."""

    def __init__(self, mmio: Any) -> None:
        self._m = mmio

    def wr(self, off: int, val: int) -> None:
        self._m.write(off, int(val) & 0xFFFF_FFFF)

    def rd(self, off: int) -> int:
        return int(self._m.read(off)) & 0xFFFF_FFFF

    def soft_reset(self) -> None:
        self.wr(REG_CONTROL, 0x2)  # soft_reset_pulse
        time.sleep(0.001)

    def configure_buffers(self, phys0: int, phys1: int) -> None:
        self.wr(REG_BUF0_ADDR_LO,  phys0 & 0xFFFF_FFFF)
        self.wr(REG_BUF0_ADDR_HI, (phys0 >> 32) & 0xFFFF_FFFF)
        self.wr(REG_BUF1_ADDR_LO,  phys1 & 0xFFFF_FFFF)
        self.wr(REG_BUF1_ADDR_HI, (phys1 >> 32) & 0xFFFF_FFFF)

    def enable_stream_writer(self, frame_bytes: int) -> None:
        self.wr(REG_FRAME_BYTES_CFG, frame_bytes)
        self.wr(REG_WRITER_SRC_SEL, 0x1)   # AXI-Stream source
        self.wr(REG_WRITER_ENABLE,  0x1)
        self.wr(REG_IRQ_ENABLE,     0x1)
        self.wr(REG_CONTROL,        0x1)   # enable

    def ready_mask(self) -> int:
        return self.rd(REG_READY_MASK) & 0x3

    def valid_bytes(self, buf_idx: int) -> int:
        off = REG_VALID_BYTES_BUF0 if buf_idx == 0 else REG_VALID_BYTES_BUF1
        return self.rd(off)

    def frame_id(self, buf_idx: int) -> int:
        off = REG_FRAME_ID_BUF0 if buf_idx == 0 else REG_FRAME_ID_BUF1
        return self.rd(off)

    def clear_irq(self) -> None:
        self.wr(REG_IRQ_STATUS, 0x1)

    def mark_consumed(self, buf_idx: int) -> None:
        self.wr(REG_CONSUMED_MASK, 1 << buf_idx)

    def writer_status(self) -> dict:
        ws = self.rd(REG_WRITER_STATUS)
        return {"busy": ws & 0x1, "fault": (ws >> 1) & 0x1, "enabled": (ws >> 2) & 0x1}


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


def send_buffer_udp(sock: socket.socket, dst: tuple, data: memoryview, frame_id: int) -> int:
    """Fragment *data* into UDP datagrams with a 6-byte header: [frame_id(4), seq(2)]."""
    sent_total = 0
    offset = 0
    seq = 0
    length = len(data)
    while offset < length:
        chunk = data[offset: offset + UDP_MAX_PAYLOAD]
        hdr = frame_id.to_bytes(4, "big") + seq.to_bytes(2, "big")
        sock.sendto(hdr + bytes(chunk), dst)
        sent_total += len(chunk)
        offset += len(chunk)
        seq += 1
    return sent_total


def run(args: argparse.Namespace) -> None:
    pynq = _load_pynq()
    Overlay = pynq.Overlay
    allocate = pynq.allocate

    bit_path = Path(args.bitstream).expanduser().resolve()
    if not bit_path.exists():
        raise FileNotFoundError(f"Bitstream not found: {bit_path}")

    print(f"[tx_daemon] Loading overlay: {bit_path}")
    overlay = Overlay(str(bit_path))
    print("[tx_daemon] Overlay loaded.")

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
        enable=True,
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

    # --- Set up PingPong DDR writer ---
    if "frame_writer_0" not in overlay.ip_dict:
        raise KeyError(f"frame_writer_0 not in overlay. Available: {list(overlay.ip_dict)}")

    fw_info = overlay.ip_dict["frame_writer_0"]
    import numpy as np  # noqa: PLC0415
    fw = PingPongCtrl(pynq.MMIO(fw_info["phys_addr"], fw_info["addr_range"]))
    fw.soft_reset()

    hdmi_gpio = None
    if "axi_gpio_hdmiin" in overlay.ip_dict:
        gpio_info = overlay.ip_dict["axi_gpio_hdmiin"]
        hdmi_gpio = HdmiFrontEndGpio(pynq.MMIO(gpio_info["phys_addr"], gpio_info["addr_range"]))
        if args.force_hpd:
            hdmi_gpio.set_hpd(True)
            print("[tx_daemon] HDMI HPD asserted via axi_gpio_hdmiin.")
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

    buf_bytes = args.payload_bytes + 64   # payload + generous header margin
    buf0 = allocate(shape=(buf_bytes,), dtype=np.uint8)
    buf1 = allocate(shape=(buf_bytes,), dtype=np.uint8)
    phys0 = buf0.physical_address
    phys1 = buf1.physical_address
    print(f"[tx_daemon] DDR buf0 @ 0x{phys0:08X}  buf1 @ 0x{phys1:08X}  ({buf_bytes} bytes each)")

    fw.configure_buffers(phys0, phys1)
    fw.enable_stream_writer(buf_bytes)
    print(f"[tx_daemon] PingPong writer enabled. Writer status: {fw.writer_status()}")

    # --- UDP socket ---
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
    dst = (args.dst_host, args.dst_port)
    print(f"[tx_daemon] Sending UDP to {dst[0]}:{dst[1]}")
    print("[tx_daemon] Running. Ctrl-C to stop.")

    frames_sent = 0
    bytes_sent = 0
    drops_last = 0
    idle_since = time.monotonic()
    last_status = time.monotonic()
    try:
        while True:
            mask = fw.ready_mask()
            if mask == 0:
                now = time.monotonic()
                if args.status_interval > 0 and (now - last_status) >= args.status_interval:
                    ws = fw.writer_status()
                    drops_now = fw.rd(REG_DROP_COUNT)
                    lock_str = "n/a"
                    if hdmi_gpio is not None:
                        lock_str = str(hdmi_gpio.pixel_lock())
                    aes_str = "n/a"
                    if aes_dbg is not None:
                        aes_s = aes_dbg.decode()
                        aes_str = (
                            f"raw=0x{aes_s['raw']:08X},k=0x{aes_s['keys_ready']:X},"
                            f"sess={aes_s['session_ready']},pt={aes_s['pt_ready']},"
                            f"strm={aes_s['stream_mode']},h={aes_s['h_valid']}"
                        )
                    seq_s = seq.read_status()
                    seq_str = (
                        f"raw=0x{seq_s['status_raw']:08X},busy={seq_s['seq_busy']},"
                        f"kdirty={seq_s['key_dirty']},nonce={seq_s['nonce_counter']},"
                        f"aes_raw=0x{seq_s['aes_status_raw']:08X},"
                        f"kready=0x{seq_s['aes_keys_ready']:X},sess={seq_s['aes_session_ready']},"
                        f"pt={seq_s['aes_pt_ready']},busy={seq_s['aes_busy']},"
                        f"h={seq_s['aes_h_valid']},strm={seq_s['aes_stream_mode']}"
                    )
                    print(
                        "[tx_daemon] idle "
                        f"pixel_lock={lock_str} "
                        f"seq={seq_str} "
                        f"aes={aes_str} "
                        f"ready_mask=0 writer_busy={ws['busy']} writer_fault={ws['fault']} "
                        f"drops={drops_now}"
                    )
                    last_status = now

                if args.idle_exit_s > 0 and (now - idle_since) >= args.idle_exit_s:
                    print(
                        "[tx_daemon] idle timeout reached "
                        f"({args.idle_exit_s:.1f}s) with no ready buffers; exiting."
                    )
                    break

                time.sleep(0.001)
                continue

            idle_since = time.monotonic()

            for buf_idx in range(2):
                if not (mask & (1 << buf_idx)):
                    continue

                nbytes = fw.valid_bytes(buf_idx)
                fid = fw.frame_id(buf_idx)
                if nbytes == 0:
                    fw.mark_consumed(buf_idx)
                    continue

                # Invalidate cache for this buffer before reading
                buf = buf0 if buf_idx == 0 else buf1
                buf.invalidate()

                sent = send_buffer_udp(sock, dst, memoryview(buf)[:nbytes], fid)
                fw.clear_irq()
                fw.mark_consumed(buf_idx)

                frames_sent += 1
                bytes_sent += sent
                last_status = time.monotonic()

            # Periodic status every 100 frames
            if frames_sent > 0 and frames_sent % 100 == 0:
                drops_now = fw.rd(REG_DROP_COUNT)
                dropped = drops_now - drops_last
                drops_last = drops_now
                print(
                    f"[tx_daemon] frames={frames_sent}  bytes={bytes_sent}"
                    f"  drops_delta={dropped}"
                )

    except KeyboardInterrupt:
        print(f"\n[tx_daemon] Stopped. frames={frames_sent}  bytes={bytes_sent}")
    finally:
        fw.wr(REG_CONTROL, 0)  # disable writer
        buf0.freebuffer()
        buf1.freebuffer()
        sock.close()


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
    p.add_argument("--payload-bytes",type=int, default=1200)
    p.add_argument("--force-hpd", action="store_true", default=True,
                   help="Assert HDMI HPD via axi_gpio_hdmiin (default: on)")
    p.add_argument("--no-force-hpd", action="store_false", dest="force_hpd",
                   help="Do not drive HDMI HPD from software")
    p.add_argument("--status-interval", type=float, default=1.0,
                   help="Seconds between idle status prints (0 disables)")
    p.add_argument("--idle-exit-s", type=float, default=0.0,
                   help="Exit after this many seconds with no ready buffers (0 disables)")
    return p.parse_args()


if __name__ == "__main__":
    run(_parse_args())
