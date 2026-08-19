"""What does the engine's GHASH actually hash? Test computable candidates
against the correlated mirror ghash values."""
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
H_INT = int.from_bytes(ecb_enc(bytes(16)), 'big')

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

for nonce, gh_hex in reads:
    p = nonce - 1
    if p not in pkts:
        print('miss packet %x' % p); continue
    d = pkts[p]
    prefix = int.from_bytes(d[:8], 'big')
    ct = d[8:-16]
    bl = [ct[i:i+16] for i in range(0, len(ct), 16)]
    mir = bytes.fromhex(gh_hex)
    nonce12 = b'\x00\x00\x00\x01' + prefix.to_bytes(8, 'big')
    M = ecb_enc(nonce12 + (1).to_bytes(4, 'big'))
    len_blk = (9728).to_bytes(16, 'big')
    ks_blocks = [ecb_enc(nonce12 + (2 + i).to_bytes(4, 'big')) for i in range(76)]
    H_M = int.from_bytes(M, 'big')
    H_KS1 = int.from_bytes(ecb_enc(nonce12 + (2).to_bytes(4, 'big')), 'big')

    cands = {
        'wire CT (H=E0)': (bl, H_INT),
        'keystream (H=E0)': (ks_blocks, H_INT),
        'CT + len-as-data (H=E0)': (bl + [len_blk], H_INT),
        'CT rotl-block (H=E0)': (bl[1:] + bl[:1], H_INT),
        'wire CT (H=E(K,J0))': (bl, H_M),
        'wire CT (H=E(K,J0+1))': (bl, H_KS1),
        'keystream (H=E(K,J0))': (ks_blocks, H_M),
    }
    print('--- prefix=%x mirror=%s' % (prefix, gh_hex))
    for name, (blocks, H) in cands.items():
        if ghash(H, blocks) == mir:
            print('  *** MATCH:', name)
