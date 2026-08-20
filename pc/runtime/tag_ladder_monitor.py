#!/usr/bin/env python3
"""tag_ladder_monitor.py - 15-minute word-level GCM tag catcher.

Verifies every received datagram's AES-GCM tag. On a failure, prints the
packet nonce, the timestamp, and the word-by-word diff (wire tag vs expected)
so a failing packet can be correlated with the board's rd_dbg.py dump.

Usage: python tag_ladder_monitor.py <minutes> <label>
"""
import socket, subprocess, sys, time
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f")
R = 0xE1000000000000000000000000000000

def gf_mul(x, y):
    z = 0; v = y
    for i in range(128):
        if (x >> (127 - i)) & 1:
            z ^= v
        v = (v >> 1) ^ R if v & 1 else v >> 1
    return z

def ghash(H, blocks):
    y = 0
    for b in blocks:
        y = gf_mul(y ^ int.from_bytes(b, "big"), H)
    y = gf_mul(y ^ int.from_bytes((9728).to_bytes(16, "big"), "big"), H)
    return y.to_bytes(16, "big")

def ecb(b):
    return Cipher(algorithms.AES(KEY), modes.ECB()).encryptor().update(b)

def main():
    minutes = float(sys.argv[1])
    label = sys.argv[2]
    H = int.from_bytes(ecb(bytes(16)), "big")
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
    s.bind(("0.0.0.0", 5600))
    s.settimeout(1)
    t0 = time.monotonic()
    ok = bad = 0
    fails = []
    while time.monotonic() - t0 < minutes * 60:
        try:
            d, _ = s.recvfrom(65535)
        except socket.timeout:
            continue
        p = int.from_bytes(d[:8], "big")
        ct, wt = d[8:-16], d[-16:]
        n = b"\x00\x00\x00\x01" + p.to_bytes(8, "big")
        try:
            AESGCM(KEY).decrypt(n, ct + wt, b"")
            ok += 1
        except Exception:
            bad += 1
            sw = ghash(H, [ct[i:i + 16] for i in range(0, len(ct), 16)])
            exp = bytes(a ^ b for a, b in zip(sw, ecb(n + (1).to_bytes(4, "big"))))
            t = time.strftime("%H:%M:%S")
            fails.append((t, p, wt.hex(), exp.hex()))
            print("FAIL t=%s nonce=%x wire=%s exp=%s" % (t, p, wt.hex(), exp.hex()))
            if len(fails) <= 5:
                for w in range(4):
                    a = wt[4 * w:4 * w + 4]; b = exp[4 * w:4 * w + 4]
                    print("  word%d: wire=%s exp=%s %s" % (w, a.hex(), b.hex(), "OK" if a == b else "MISMATCH"))
            if len(fails) == 1:
                # One-shot board-side dump for correlation: the sequencer's
                # mirrors + the tag-path debug probes + the shim counters.
                try:
                    r = subprocess.run(
                        ["ssh", "-o", "ConnectTimeout=10", "xilinx@192.168.0.123",
                         "echo xilinx | sudo -S -p '' bash -lc 'python3 /home/xilinx/jupyter_notebooks/OS-VideoSDR/pynq/runtime/rd_dbg.py; tail -2 /home/xilinx/tx_shim.log'", "2>/dev/null"],
                        capture_output=True, text=True, timeout=30)
                    print("BOARD DUMP:\n" + (r.stdout or r.stderr))
                except Exception as e:
                    print("BOARD DUMP FAILED: %r" % e)
    print("RESULT %s: ok=%d bad=%d minutes=%.1f" % (label, ok, bad, minutes))
    if fails:
        print("FAILS=%d" % len(fails))
        for f in fails[:20]:
            print("  %s nonce=%x wire=%s exp=%s" % f)

if __name__ == "__main__":
    main()
