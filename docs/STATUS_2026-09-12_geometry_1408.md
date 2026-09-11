# STATUS 2026-09-12 — 1408-byte slot geometry and the tag-length trap

## 1. Why the geometry changed

The PS sender is CPU bound in the kernel TX path. With coherent ACP writes the
chain sustained 68.4-69.0k packets/s against the 70,560 that 2,352 segments per
frame needs, so it stayed about 2% short and the ring dropped ~1,500 packets/s,
which breaks frames. `AWCACHE = 4'b1111` (write-allocate) is the only setting
that proved coherent, and it costs about 3.5% CPU, so the only remaining lever
was fewer, larger packets.

| item                | was        | now        |
|---------------------|------------|------------|
| payload             | 1176 B (392 px) | 1320 B (440 px) |
| authenticated body  | 1240 B     | 1384 B     |
| slot                | 1280 B     | 1408 B (11 x 128-byte bursts) |
| segments per frame  | 2352       | 2095 (2094 x 440 px + one 240 px) |
| packets/s at 30 fps | 70,560     | 62,850     |
| CPU needed (of 2.00 s/s) | 2.05 (102%) | 1.80 (90%) |
| wire packet         | 1308 B     | 1436 B (64 B under MTU) |

## 2. Implementation

First implementation missed timing: WNS −0.067 ns, 1 failing endpoint of 73,152,
on the GHASH GF-multiply path with 82% of the delay in routing — placement
variance, not the geometry. Post-route `phys_opt_design -directive
AggressiveExplore` plus a reroute on the placed checkpoint fixed it:
**WNS +0.013 ns, 0 failing endpoints** (`impl_1_timing_fix1.rpt`). Margin is
thin, so pipelining the GHASH GF multiply remains the durable fix.

Artifacts: bit `e5f015ad…`, handoff `9b6cbc1c…` (PACKET_BYTES=1384,
SLOT_STRIDE=1408 confirmed inside it).

## 3. Measured on hardware

```text
publish          62,528 slots/s (the probe's "need 70560" label was stale)
shim rate        61,764 - 63,038 pkt/s
drops_delta      0 in every sample        <- the 2% shortfall is gone
bo-sync-us       0 (coherent path)
send-us          1.82 s of 2.00 s per second (91%, as predicted)
wire             90 MB/s
```

## 4. The tag-length trap (two hours of debugging)

Every packet failed authentication while:

```text
all datagrams 1408 bytes, padding bytes zero
nonce prefixes sequential, step 1
ciphertext decrypts correctly: header magic 0x4F56, session 1, seg 1527/2095
key and nonce confirmed by decrypting the first block (GCM data starts at
  inc32(J0), counter 2 — a detail worth remembering)
attribute register 0x50 reads 0xf8f -> AWUSER[0]=1, AWCACHE[1]=1, coherent
```

The header revealed `plen = 1176`: the running daemon had been started with
`--payload-bytes 1176` (the 1280-slot value) against the 1408-slot overlay,
because the updated start script was never copied to the board. The AES core
derives its GHASH length from `(HEADER_BYTES + reg_payload_bytes) * 8`
(AES_GCM_Session_Sequencer.sv), so it hashed 9,728 bits instead of 10,880 and
every tag was wrong while the ciphertext stayed perfect.

Guard added in `tx_daemon.py`: it reads `PACKET_BYTES` from the `.hwh` beside
the bitstream and refuses to start unless `--payload-bytes + 64` matches it,
printing the value to use. That turns this failure into a one-line message.

## 5. The wedge, and a fix for it

`ssh` stopped answering (ping fine, banner timeouts) because both sender
workers ran at nice 0 and needed 90% of both cores; an SSH handshake needs
several round trips and never completed. This happened three times now, each
time needing a power cycle. The shim now runs its workers at **nice 5** by
default (`--nice` overrides), which costs a fraction of a percent of throughput
and keeps `sshd` responsive.

## 6. State and next steps

```text
code       committed: AES a88fe03 + artifacts 6ea77b2 + impl_retry e911cc7,
           OS 4dbeb45 + d8cc8c0 + b2df2ae, board at d8cc8c0 with the new overlay
blocked    the board is CPU-starved and needs a power cycle
then       1. re-copy /tmp/reset_run.sh (reboot clears /tmp) and recompiled shim
           2. start the daemon with --payload-bytes 1320 (now guarded)
           3. start one shim, expect ~62,850 pkt/s, drops 0
           4. authentication monitor: expect auth_ok = all, 0 gaps
           5. completed frames: expect 30 per second once the PC receiver keeps up
```

The OpenCV display comes after that, wired as a separate stage so the receive
loop never blocks on `imshow`.
