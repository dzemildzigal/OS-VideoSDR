"""Sweep H candidates x length variants against the wire tag (pure software)."""
import socket
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
ZKEY = bytes(32)
def ecb_enc_k(key, blk16):
    return Cipher(algorithms.AES(key), modes.ECB()).encryptor().update(blk16)
def ecb_enc(blk16):
    return ecb_enc_k(KEY, blk16)

H_cands = {
    'E(K,0)': int.from_bytes(ecb_enc(bytes(16)), 'big'),
    'E(0key,0)': int.from_bytes(ecb_enc_k(ZKEY, bytes(16)), 'big'),
    'E(K,ff)': int.from_bytes(ecb_enc(b'\xff' * 16), 'big'),
}

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(('0.0.0.0', 5600)); s.settimeout(15)

N = 4
hits = {}
for n in range(N):
    d, _ = s.recvfrom(65535)
    prefix = int.from_bytes(d[:8], 'big')
    ct = d[8:-16]; wt = d[-16:]
    n12 = b'\x00\x00\x00\x01' + prefix.to_bytes(8, 'big')
    M = ecb_enc(n12 + (1).to_bytes(4, 'big'))
    bl = [ct[i:i+16] for i in range(0, len(ct), 16)]
    H_cands['E(K,J0)'] = int.from_bytes(ecb_enc(n12 + (1).to_bytes(4, 'big')), 'big')
    for hn, H in H_cands.items():
        for lenC in (9728, 9856, 9920, 9744, 9712, 0):
            y = ghash(H, bl, lenC)
            # convention form: wire == rotl32(y) ^ [M0 M1 M2 0]
            if bytes(a ^ b for a, b in zip(y[4:] + y[:4], M[:12] + b'\x00' * 4)) == wt:
                hits.setdefault((hn, lenC), 0)
                hits[(hn, lenC)] += 1
            # plain form: wire == y ^ M
            if bytes(a ^ b for a, b in zip(y, M)) == wt:
                hits.setdefault((hn, lenC, 'plain'), 0)
                hits[(hn, lenC, 'plain')] += 1
print('tested %d packets' % N)
if hits:
    for k, v in hits.items():
        print('  HIT %s : %d/%d' % (k, v, N))
else:
    print('no hits')
