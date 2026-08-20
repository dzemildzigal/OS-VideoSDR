"""Read vtc_in detector registers WITHOUT loading the overlay.

The tx_daemon already holds the overlay; this script only mmaps the VTC and
GPIO registers and reads them. Never writes to the VTC.

Run on the board while the daemon is running:
    sudo bash -lc 'python3 vtc_read.py'
"""
import sys

VTC_BASE = 0x43C00000
GPIO_BASE = 0x41200000

REGS = {
    "STAT": 0x004,    # bit0 = LOCKED, bit2 = active-video event
    "DASIZE": 0x020,  # [31:16]=v_active, [15:0]=h_active
    "DTSTAT": 0x024,  # detector timing status (raw)
    "DFENC": 0x028,   # detected encoding
    "DPOL": 0x02C,    # detected polarity
    "DHSIZE": 0x030,  # h_total+1 in [15:0]
    "DVSIZE": 0x034,  # v_total+1 in [15:0]
    "DHSYNC": 0x038,  # hsync start
}


def main() -> int:
    pynq = __import__("pynq")
    MMIO = getattr(pynq, "MMIO")

    gpio = MMIO(GPIO_BASE, 0x1000)
    # gpio2 (pixel lock) = channel 2 data register
    gpio.write(0x08, 0x1)  # GPIO2_TRI: input
    pixel_lock = int(gpio.read(0x08)) & 0x1
    print(f"pixel_lock = {pixel_lock}", flush=True)

    if pixel_lock == 0:
        print("pixel_lock=0: no HDMI clock. Refusing to touch vtc_in (bus-error risk).")
        return 1

    vtc = MMIO(VTC_BASE, 0x10000)
    vals = {}
    for name, off in REGS.items():
        vals[name] = int(vtc.read(off)) & 0xFFFFFFFF
        print(f"0x{off:03X} {name:8s} = 0x{vals[name]:08X}", flush=True)

    stat = vals["STAT"]
    locked = stat & 0x1
    avideo = (stat >> 2) & 0x1
    h_active = vals["DASIZE"] & 0xFFFF
    v_active = (vals["DASIZE"] >> 16) & 0xFFFF
    h_total = (vals["DHSIZE"] & 0xFFFF) - 1
    v_total = (vals["DVSIZE"] & 0xFFFF) - 1
    print(f"decoded: active={h_active}x{v_active} total={h_total}x{v_total}", flush=True)

    if locked and avideo and 100 <= h_active <= 4096 and 100 <= v_active <= 4096:
        print("VERDICT: LOCKED + ACTIVE VIDEO with plausible size.")
    elif locked and not avideo:
        print("VERDICT: locked but NO ACTIVE VIDEO (vde not toggling).")
    else:
        print("VERDICT: detector not locked onto video timing.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
