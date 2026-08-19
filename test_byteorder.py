"""Find the GHASH input byte order. Recover PT via CTR, then test software
GHASH over many byte-orderings of CT/PT vs the wire tag."""
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
def ecb(blk16):
    return Cipher(algorithms.AES(KEY), modes.ECB()).encryptor().update(blk16)
H = int.from_bytes(ecb(bytes(16)), 'big')

def blocks(b): return [b[i:i+16] for i in range(0, len(b), 16)]
def rev16(b): return b''.join(b[i:i+16][::-1] for i in range(0, len(b), 16))
def rev4(b):  return b''.join(b[i:i+4][::-1] for i in range(0, len(b), 4))
def rev2(b):  return b''.join(b[i:i+2][::-1] for i in range(0, len(b), 2))

s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(('0.0.0.0', 5600)); s.settimeout(15)
found = {}
for n in range(6):
    d, _ = s.recvfrom(65535)
    prefix = int.from_bytes(d[:8], 'big')
    ct = d[8:-16]; wt = d[-16:]
    n12 = b'\x00\x00\x00\x01' + prefix.to_bytes(8, 'big')
    ks = b''.join(ecb(n12 + (2 + i).to_bytes(4, 'big')) for i in range(76))
    pt = bytes(a ^ b for a, b in zip(ct, ks))
    M = ecb(n12 + (1).to_bytes(4, 'big'))
    cands = {
        'CT': ct, 'CT.rev': ct[::-1], 'CT.rev16': rev16(ct), 'CT.rev4': rev4(ct), 'CT.rev2': rev2(ct),
        'PT': pt, 'PT.rev': pt[::-1], 'PT.rev16': rev16(pt), 'PT.rev4': rev4(pt),
        'KS': ks,
    }
    for name, data in cands.items():
        tag = bytes(a ^ b for a, b in zip(ghash(H, blocks(data)), M))
        if tag == wt:
            found[name] = found.get(name, 0) + 1
        # also reversed-wire-tag form
        if tag[::-1] == wt:
            found[name + '+wire_rev'] = found.get(name + '+wire_rev', 0) + 1
print('tested 6 packets; matches:')
for k, v in sorted(found.items(), key=lambda x: -x[1]):
    print('  %-14s %d/6' % (k, v))
if not found:
    print('  none')
