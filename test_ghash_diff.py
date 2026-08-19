"""Full differential GHASH analysis with recovered plaintexts (static screen)."""
import socket, subprocess, re, threading
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

R = 0xE1000000000000000000000000000000
def gf_mul(x, y):
    z = 0; v = y
    for i in range(128):
        if (x >> (127 - i)) & 1: z ^= v
        if v & 1: v = (v >> 1) ^ R
        else: v >>= 1
    return z

def ghash(H_int, blocks, lenC=9728, lenA=0):
    y = 0
    for b in blocks:
        y = gf_mul(y ^ int.from_bytes(b, 'big'), H_int)
    y = gf_mul(y ^ int.from_bytes(((lenA << 64) | lenC).to_bytes(16, 'big'), 'big'), H_int)
    return y.to_bytes(16, 'big')

KEY = bytes.fromhex('000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f')
def ecb_enc(blk16):
    return Cipher(algorithms.AES(KEY), modes.ECB()).encryptor().update(blk16)
H0 = int.from_bytes(ecb_enc(bytes(16)), 'big')

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(('0.0.0.0', 5600)); s.settimeout(0.2)
pkts = {}
stop = [False]
def cap():
    while not stop[0]:
        try:
            d, _ = s.recvfrom(65535)
            pkts[int.from_bytes(d[:8], 'big')] = d
        except socket.timeout: pass
t = threading.Thread(target=cap); t.start()
reads = []
for i in range(2):
    out = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
        'xilinx@192.168.0.123',
        "echo xilinx | sudo -S -p '' bash -lc 'python3 /tmp/rd_mir.py'"],
        capture_output=True, text=True)
    m = re.search(r'NONCE (\d+).*GHASH ([0-9a-f]{32})', out.stdout, re.S)
    if m: reads.append((int(m.group(1)), m.group(2)))
stop[0] = True; t.join()

samples = []
for nonce, gh_hex in reads:
    p = nonce - 1
    if p not in pkts:
        print('miss %x' % p); continue
    d = pkts[p]
    prefix = int.from_bytes(d[:8], 'big')
    ct = d[8:-16]
    n12 = b'\x00\x00\x00\x01' + prefix.to_bytes(8, 'big')
    ks = b''.join(ecb_enc(n12 + (2 + i).to_bytes(4, 'big')) for i in range(76))
    pt = bytes(a ^ b for a, b in zip(ct, ks))
    samples.append((prefix, ct, pt, ks, bytes.fromhex(gh_hex)))

for prefix, ct, pt, ks, mir in samples:
    print('prefix=%x' % prefix)
    print('  PT hdr:', pt[:40].hex())
    print('  PT payload constant?', len(set(pt[40:])) == 1, 'value=%02x' % pt[40] if len(set(pt[40:])) == 1 else '')

if len(samples) == 2:
    (p1, ct1, pt1, ks1, m1), (p2, ct2, pt2, ks2, m2) = samples
    dy = bytes(a ^ b for a, b in zip(m1, m2))
    print('dy = y1^y2:', dy.hex())
    for name, xa, xb in [
        ('PT', pt1, pt2), ('CT', ct1, ct2), ('KS', ks1, ks2)]:
        dx = bytes(a ^ b for a, b in zip(xa, xb))
        dbl = [dx[i:i+16] for i in range(0, len(dx), 16)]
        nz = sum(1 for b in dbl if b != bytes(16))
        lin = ghash(H0, dbl, lenC=0)
        print('LIN %-3s nonzero blocks=%d  ghash_H0(dX)=%s  %s' % (
            name, nz, lin.hex(), 'MATCH' if lin == dy else 'diff'))

    # single-packet direct tests with recovered PT
    for i, (prefix, ct, pt, ks, mir) in enumerate(samples):
        for name, data in [('PT', pt), ('CT', ct), ('KS', ks)]:
            bl = [data[j:j+16] for j in range(0, len(data), 16)]
            for lenC in (9728, 1216, 9856, 0):
                if ghash(H0, bl, lenC) == mir:
                    print('*** MATCH pkt%d %s lenC=%d' % (i, name, lenC))
