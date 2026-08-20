#!/usr/bin/env python3
"""freq_sweep.py - one-shot 75-minute regression sweep: 25 min per frequency
at 50, 75 and 100 MHz, switching the board at runtime (no rebuilds).

Per leg: kill daemon+shim -> relaunch daemon with --aes-freq X -> relaunch
shim -> verify flow -> 25-min word-level tag check. On the FIRST failure the
board's tag-path probe registers (rd_dbg.py) are dumped twice (immediately
and 3 s later) so a sticky regression is pinned to its stage:
  wire != PUSH        -> corruption after the stream FIFO (writer/DDR/shim)
  wire == MXIS != PUSH-> FIFO/mapping corruption
  wire == PUSH        -> engine-side (mask/GHASH) or header/CT issue
"""
import subprocess
import socket
import sys
import time
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f")
BOARD = "xilinx@192.168.0.123"
FREQS = [50, 75, 100]
MINUTES_PER_FREQ = 25.0
R = 0xE1000000000000000000000000000000
DAEMON_CMD = (
    "echo xilinx | sudo -S -p '' bash -lc 'cd /home/xilinx/jupyter_notebooks/"
    "OS-VideoSDR/pynq/runtime && setsid nohup /usr/local/share/pynq-venv/bin/"
    "python3 tx_daemon.py --bitstream /home/xilinx/hdmi_aes_tx.bit "
    "--dst-host 192.168.0.37 --dst-port 5600 "
    "--key-hex 000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f "
    "--payload-bytes 1176 --configure-only --aes-freq {freq} "
    "</dev/null >/home/xilinx/tx_daemon.log 2>&1 &'"
)
SHIM_CMD = (
    "echo xilinx | sudo -S -p '' bash -lc 'cd /home/xilinx/jupyter_notebooks/"
    "OS-VideoSDR/pynq/ps_shim/src && setsid nohup ./tx_shim 192.168.0.37 5600 "
    "</dev/null >/home/xilinx/tx_shim.log 2>&1 &'"
)
KILL_CMD = (
    "echo xilinx | sudo -S -p '' bash -lc 'pids=$(pgrep -f \"[p]ython3 "
    "tx_daemon\"); [ -n \"$pids\" ] && kill $pids; pids=$(pgrep -f \"[t]x_shim\"); "
    "[ -n \"$pids\" ] && kill $pids'"
)
DUMP_CMD = (
    "echo xilinx | sudo -S -p '' bash -lc 'python3 /home/xilinx/jupyter_notebooks/"
    "OS-VideoSDR/pynq/runtime/rd_dbg.py; tail -2 /home/xilinx/tx_shim.log'"
)


def ssh(cmd, timeout=60):
    r = subprocess.run(
        ["ssh", "-o", "ConnectTimeout=10", "-o", "BatchMode=yes", BOARD, cmd],
        capture_output=True, text=True, timeout=timeout)
    return (r.stdout or "") + (r.stderr or "")


def board_kill():
    ssh(KILL_CMD, timeout=45)


def board_launch(freq):
    try:
        ssh(DAEMON_CMD.format(freq=freq), timeout=40)
    except subprocess.TimeoutExpired:
        pass
    time.sleep(25)
    out = ssh("grep -a 'Design clock' /home/xilinx/tx_daemon.log | tail -1", timeout=30)
    print("  daemon: %s" % out.strip().splitlines()[-1:] or "?", flush=True)
    try:
        ssh(SHIM_CMD, timeout=40)
    except subprocess.TimeoutExpired:
        pass
    time.sleep(12)
    out = ssh("tail -1 /home/xilinx/tx_shim.log", timeout=30)
    print("  shim: %s" % (out.strip().splitlines()[-1:] or "?"), flush=True)


def gf_mul(x, y):
    z = 0
    v = y
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


def monitor_leg(minutes, label, H):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 8 * 1024 * 1024)
    s.bind(("0.0.0.0", 5600))
    s.settimeout(1)
    t0 = time.monotonic()
    ok = bad = 0
    dumped = False
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
            print("FAIL %s t=%s nonce=%x wire=%s exp=%s" % (
                label, time.strftime("%H:%M:%S"), p, wt.hex(), exp.hex()), flush=True)
            for w in range(4):
                a = wt[4 * w:4 * w + 4]
                b = exp[4 * w:4 * w + 4]
                print("  word%d: wire=%s exp=%s %s" % (
                    w, a.hex(), b.hex(), "OK" if a == b else "MISMATCH"), flush=True)
            if not dumped:
                dumped = True
                try:
                    print("BOARD DUMP (+0s):\n%s" % ssh(DUMP_CMD, timeout=30), flush=True)
                except Exception as e:
                    print("BOARD DUMP FAILED: %r" % e, flush=True)
                time.sleep(3)
                try:
                    print("BOARD DUMP (+3s):\n%s" % ssh(DUMP_CMD, timeout=30), flush=True)
                except Exception as e:
                    print("BOARD DUMP FAILED: %r" % e, flush=True)
    print("RESULT %s: ok=%d bad=%d minutes=%.0f" % (label, ok, bad, minutes), flush=True)
    return ok, bad


def main():
    H = int.from_bytes(ecb(bytes(16)), "big")
    print("SWEEP START: %s freqs, %.0f min each" % (FREQS, MINUTES_PER_FREQ), flush=True)
    summary = []
    for f in FREQS:
        print("=== switching board to %d MHz ===" % f, flush=True)
        board_kill()
        time.sleep(3)
        board_launch(f)
        ok, bad = monitor_leg(MINUTES_PER_FREQ, "SWEEP_%d" % f, H)
        summary.append((f, ok, bad))
    print("=== SWEEP SUMMARY ===", flush=True)
    for f, ok, bad in summary:
        print("%3d MHz: ok=%d bad=%d" % (f, ok, bad), flush=True)


if __name__ == "__main__":
    main()
