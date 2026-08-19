"""Pure-software tag-formula test. No mirrors (readback path is suspect).
Recover PT via CTR, compute candidate tag formulas, compare with the wire tag."""
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
def ecb_enc(blk16):
    return Cipher(algorithms.AES(KEY), modes.ECB()).encryptor().update(blk16)
H0 = int.from_bytes(ecb_enc(bytes(16)), 'big')

def rotl32(b):
    return b[4:] + b[:4]

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(('0.0.0.0', 5600)); s.settimeout(15)

formulas = ['ghash(CT)^M', 'rotl32(ghash(CT))^[M0..M2,0]', 'rotl32(ghash(PT))^[M0..M2,0]',
            'ghash(PT)^M', 'rotr32(ghash(CT))^M', 'ghash(KS)^M']
results = {f: 0 for f in formulas}
N = 5
for n in range(N):
    d, _ = s.recvfrom(65535)
    prefix = int.from_bytes(d[:8], 'big')
    ct = d[8:-16]; wt = d[-16:]
    n12 = b'\x00\x00\x00\x01' + prefix.to_bytes(8, 'big')
    ks = b''.join(ecb_enc(n12 + (2 + i).to_bytes(4, 'big')) for i in range(76))
    pt = bytes(a ^ b for a, b in zip(ct, ks))
    M = ecb_enc(n12 + (1).to_bytes(4, 'big'))
    gct = ghash(H0, [ct[i:i+16] for i in range(0, len(ct), 16)])
    gpt = ghash(H0, [pt[i:i+16] for i in range(0, len(pt), 16)])
    gks = ghash(H0, [ks[i:i+16] for i in range(0, len(ks), 16)])
    cands = {
        'ghash(CT)^M': bytes(a ^ b for a, b in zip(gct, M)),
        'rotl32(ghash(CT))^[M0..M2,0]': bytes(a ^ b for a, b in zip(rotl32(gct), M[:12] + b'\x00' * 4)),
        'rotl32(ghash(PT))^[M0..M2,0]': bytes(a ^ b for a, b in zip(rotl32(gpt), M[:12] + b'\x00' * 4)),
        'ghash(PT)^M': bytes(a ^ b for a, b in zip(gpt, M)),
        'rotr32(ghash(CT))^M': bytes(a ^ b for a, b in zip(gct[-4:] + gct[:-4], M)),
        'ghash(KS)^M': bytes(a ^ b for a, b in zip(gks, M)),
    }
    for name, v in cands.items():
        if v == wt:
            results[name] += 1
print('tested %d packets' % N)
for name, cnt in results.items():
    print('  %-40s matches: %d/%d' % (name, cnt, N))
