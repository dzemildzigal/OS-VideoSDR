# GHASH / TAG anomaly — debug state (survives session death)

## Pipeline goal
HDMI in -> PL AES-256-GCM encrypt -> UDP -> PC decrypt + display (PYNQ-Z2).

## What is VERIFIED WORKING (on the wire)
- Video pipeline, packetizer, pacer: ~500 pkt/s, drops=0, steady.
- Key load works (fixed the sequencer key_dirty guard that skipped the real key load).
- CTR path: PL keystream == E(real_key, nonce||2) byte-exact. Key, nonce, counter all correct.
- Nonce pairing: SEQ_NONCE = frame_id + 1 exactly. Daemon prefix = nonce-1 (or -2) is right.
- Wire CT byte order == software order (keystream check on bytes 0-7).

## The anomaly (tag path)
Wire format per datagram: [8B nonce prefix][1216B CT][16B tag] = 1240B.

Correlated capture (packet prefix P, sequencer mirrors read at same instant):
- GHASH mirror (seq 0x74-0x80 <- AES 0x78-0x84): random per packet. Call it y = [y0 y1 y2 y3] (4x32b).
- TAG mirror (seq 0x60-0x6C <- AES 0x88-0x94): [0x00031F1F, t1, t2, t3].
  - word0 = 0x00031F1F = EXACTLY the AES STATUS word (proven: seq status reg packs AES mirror;
    aes_status = 0x1F1F | (3<<16) = 0x00031F1F). CONSTANT across packets.
- Wire tag = [t1, t2, t3, y0].
- Word pairing: t1 = y1^M0, t2 = y2^M1, t3 = y3^M2, where M = E(K, nonce||1) (computable, verified).

So: wire_tag = [y1^M0, y2^M1, y3^M2, y0]
           = rotl32(y) ^ [M0, M1, M2, 0]
Mirror tag = [STATUS, y1^M0, y2^M1, y3^M2]

## Suspects
1. Engine's tag = rotl32(y_acc) ^ mask  (mask correct, y_acc rotated in the XOR), OR
   engine's tag = y_acc ^ rotr32(mask)  (y_acc correct, mask rotated at capture).
   Both produce words 1-3 = y_i ^ M_{i-1}. Indistinguishable from data so far.
2. Tag mirror word0 = STATUS: separate readback/aliasing anomaly (or same shift).
3. Wire tag word3 = y0 (ghash word0), i.e. the pushed tag beat = {tag[32:127], ghash[0:31]}.

## CRITICAL: my Python gf_mul was WRONG until fixed (deepseek's early sweeps are garbage).
Correct version (NIST-validated, matches cryptography lib):
  R = 0xE1000000000000000000000000000000
  def gf_mul(x, y):
      z = 0; v = y
      for i in range(128):
          if (x >> (127 - i)) & 1: z ^= v
          if v & 1: v = (v >> 1) ^ R
          else: v >>= 1
      return z

## KEY INSIGHT for the demo
CTR decryption does NOT need the tag: PT = CT ^ keystream. The payload can be
decrypted TODAY without the tag. The tag only gates authentication.

## PC workaround (if the structure is stable across packets)
PC: y = GHASH_H(CT), M = E(K, nonce||1); expected_tag = (y[4:16] ^ M[0:12]) + y[0:4].
Verify == wire_tag. If stable -> decrypt + display works with zero RTL changes.

## Open question the systematic test must answer
A) Is mirror ghash == software GHASH_H(wire CT) with H = E(K,0)?
   (linearity check: y_a ^ y_b == GHASH_H(CT_a ^ CT_b))
B) Do extracted mask words (wire[i] ^ mirror[i+1], i=0..2) == E(K,J0)[0:2], stable across packets?

## Files
- Test data captures in OS-VideoSDR/: pl_pkt2.bin, pl_pkt3.bin (old, zero-key-era and key-fixed)
- Board helper: /tmp/rd_mir.py (reads seq nonce+ghash+tag mirrors), /tmp/rd_mir2.py
- Sequencer reg map: GHASH0-3 @ 0x74/0x78/0x7C/0x80, TAG0-3 @ 0x60/0x64/0x68/0x6C,
  TAG_VALID @ 0x70, NONCE_CUR @ 0x40/0x44.
- Daemon: /home/xilinx/jupyter_notebooks/OS-VideoSDR/pynq/runtime/tx_daemon.py (running,
  prints ghash+tag every 100 frames to /home/xilinx/tx_daemon.log)

## ROOT CAUSE (FINAL, verified)

### Hardware bug (the tag failure)
`GcmMode.launch_h` fired ~1 cycle after `new_masterkey`, so the H = E_K(0)
block flowed through the EncryptPipelined WHILE the KeyExpansion was still
computing round keys -> H was garbage -> GHASH used a wrong H -> the tag never
verified against software.

Why everything else looked right:
- CTR keystream: computed per-session, long after the expansion finished -> correct.
- E_K(J0) mask: computed at session start, after the expansion -> correct (verified 3/3).
- Only H is computed at key-load time, racing the expansion -> wrong.

FIX (GcmMode.sv, one line):
  wire launch_h = (!sched_block) && pending_h && (keys_ready == 4'd15);

### Simulation-only bug (does NOT affect hardware)
`EncryptionRound` uses `expanded_key[i*128 +:128]` (parameter-indexed part-select
on a [0:1919] port). In xsim, across hierarchy, this resolves to [0:128] (round
key 0) for ALL rounds -> EncryptPipelined NIST testbench FAILS in sim, and the
sim produces wrong AES. On real hardware the wire CT verified byte-exact, so
Vivado SYNTHESIS evaluates the slice correctly. xsim-only. The deployed design
is unaffected; only sim-based AES testing is blocked until the slice is reworked.

Verified via:
- tb_ep_kat (old pipeline vs new): old = NIST-correct, new = fails.
- tb_round2 (isolated round, i=2, reg-driven ek): CORRECT (rk2).
- tb_ep_kat_new probes: round2.i=2, ek[256:384]=rk2, yet round2 used rk0.

## Verification status
- GHashEngine TB: PASS (NIST gf_mul).
- GcmMode TB: PASS (but self-referential reference pipeline - shares the RTL).
- EncryptPipelined TB (current RTL): FAILS in xsim only (part-select bug).
- Wrapper-level KAT (tb_wrapper_stream.sv): reproduces the whole chain in sim.

## FINAL RESOLUTION (verified on board 2nd fix)
Root cause: GcmMode allowed PT to fire during sess_pending (before the GHASH
session started). The first CT blocks emerged with ct_valid_i gated by
sess_running=0 and were silently DROPPED from the GHASH -> tag over truncated
ciphertext. CT on the wire stayed byte-correct (keystream verified) which made
it deceptive. FIX: pt_base_ready = key_present && sess_running && gh_ct_ready
(removed pt_path_pending). Verified in sim (immediate-stream worst case -> tag
correct) and on board (10/10, then 50/50 RX frames, 100/100 OpenCV frames).

Other changes:
- EncryptionRound.sv: localparam KEY_BASE (xsim part-select quirk workaround;
  logically identical, keeps sim == synth).
- launch_h keys_ready==15 gate REVERTED (lockstep just-in-time is correct).

KNOWN REMAINING ISSUE: intermittent InvalidTag on the wire (~rare, data-
dependent). The GHASH GF-multiply path is timing-marginal (~9.8/10 ns at
100 MHz). Deferred hardening: pipeline GFMult128 or drop AES-domain clock to
50/75 MHz.
