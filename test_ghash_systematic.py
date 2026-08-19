"""Systematic GHASH/tag test: correlate wire packets with sequencer mirrors.

Captures packets continuously (thread), reads the sequencer mirrors 3 times via
SSH (each read = the last completed packet = nonce-1), then runs all checks with
the NIST-validated gf_mul. Prints a clear verdict table.
"""
import socket, subprocess, re, threading, sys
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
H_INT = int.from_bytes(ecb_enc(bytes(16)), 'big')

def w32(b, i):  # 32-bit word i of bytes b
    return int.from_bytes(b[4*i:4*i+4], 'big')

# ---- capture + mirror reads ----
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(('0.0.0.0', 5600)); s.settimeout(0.2)
pkts = {}
stop = [False]
def cap():
    while not stop[0]:
        try:
            d, _ = s.recvfrom(65535)
            pkts[int.from_bytes(d[:8], 'big')] = d
        except socket.timeout:
            pass
t = threading.Thread(target=cap); t.start()

reads = []
for i in range(3):
    out = subprocess.run(['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10',
        'xilinx@192.168.0.123',
        "echo xilinx | sudo -S -p '' bash -lc 'python3 /tmp/rd_mir.py'"],
        capture_output=True, text=True)
    m = re.search(r'NONCE (\d+).*GHASH ([0-9a-f]{32}).*TAG   ([0-9a-f]{32})', out.stdout, re.S)
    if m:
        reads.append((int(m.group(1)), m.group(2), m.group(3)))
    else:
        print('mirror read failed:', out.stdout, out.stderr)
stop[0] = True; t.join()
print('captured %d packets, %d mirror reads' % (len(pkts), len(reads)))

def blocks_of(ct):
    return [ct[i:i+16] for i in range(0, len(ct), 16)]

samples = []
for nonce, gh_hex, tg_hex in reads:
    p = nonce - 1
    if p not in pkts:
        print('read nonce=%d: packet %x NOT in capture window' % (nonce, p))
        continue
    d = pkts[p]
    samples.append((p, d, bytes.fromhex(gh_hex), bytes.fromhex(tg_hex)))
print('correlated samples:', len(samples))

if not samples:
    sys.exit(1)

# ---- per-sample checks ----
print()
for p, d, gh_mir, tg_mir in samples:
    prefix = int.from_bytes(d[:8], 'big')
    ct = d[8:-16]; wt = d[-16:]
    bl = blocks_of(ct)
    sw = ghash(H_INT, bl)
    M = ecb_enc(b'\x00\x00\x00\x01' + prefix.to_bytes(8, 'big') + (1).to_bytes(4, 'big'))
    print('--- packet prefix=%x' % prefix)
    print('  A  sw GHASH(CT)   :', sw.hex())
    print('     mirror ghash  :', gh_mir.hex(), ' MATCH' if sw == gh_mir else ' diff')
    # B: mask extraction from wire tag words 0-2
    ext = [w32(wt, i) ^ w32(gh_mir, i+1) for i in range(3)]
    exp = [w32(M, i) for i in range(3)]
    print('  B  extracted mask :', ['%08x' % x for x in ext])
    print('     E(K,J0)[0:2]  :', ['%08x' % x for x in exp], ' MATCH' if ext == exp else ' diff')
    # wire word3 == mirror ghash word0?
    print('     wire w3 == mir g0:', '%s' % (w32(wt, 3) == w32(gh_mir, 0)))
    # full convention check: wt == (y[4:16]^M[0:12]) + y[0:4]
    conv = bytes(a ^ b for a, b in zip(gh_mir[4:16], M[0:12])) + gh_mir[0:4]
    print('     convention    :', 'MATCH' if conv == wt else 'diff')

# ---- linearity (input/H check, mask-free) ----
print()
if len(samples) >= 2:
    (p1, d1, g1, _), (p2, d2, g2, _) = samples[0], samples[1]
    ct1, ct2 = d1[8:-16], d2[8:-16]
    xor_ct = bytes(a ^ b for a, b in zip(ct1, ct2))
    lin = ghash(H_INT, blocks_of(xor_ct), lenC=0)  # length block cancels in the XOR
    mir_xor = bytes(a ^ b for a, b in zip(g1, g2))
    print('LIN ghash(CT1^CT2):', lin.hex())
    print('    mir1 ^ mir2   :', mir_xor.hex(), ' MATCH' if lin == mir_xor else ' diff')

# ---- permutation sweep vs mirror (first sample) ----
print()
p, d, gh_mir, tg_mir = samples[0]
ct = d[8:-16]; bl = blocks_of(ct)
variants = {
  'wire': bl,
  'rev16': [b[::-1] for b in bl],
  'rev32': [b''.join(b[i:i+4][::-1] for i in range(0, 16, 4)) for b in bl],
  'swap8': [b[8:] + b[:8] for b in bl],
  'rotl4': [b[4:] + b[:4] for b in bl],
  'rotl12': [b[12:] + b[:12] for b in bl],
  'rev_order': bl[::-1],
}
for k in range(0, 5):
    variants['drop_first_%d' % k] = bl[k:]
    variants['drop_last_%d' % k] = bl[:len(bl) - k] if k else bl
for name, v in variants.items():
    sw = ghash(H_INT, v)
    if sw == gh_mir:
        print('PERM MATCH:', name)
        break
else:
    print('PERM: no match (mirror=%s)' % gh_mir.hex())
