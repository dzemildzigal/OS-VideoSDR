# Report — 1280-byte UDP GSO Measurement

Date: 2026-08-24
Status: measurement complete; B.2 code change not started.

## Question

Can the PS send 1280-byte UDP GSO segments over the existing 1500-byte MTU
network path, at the 720p30 packet rate, without changing the PL slot stride?

## Test setup

- Board: PYNQ-Z2, eth0 link 1000 Mb/s full duplex.
- PC: 192.168.0.37, Realtek PCIe GBE, normal MTU 1500.
- Switch: existing 4-port Gigabit switch.
- Old `tx_shim` stopped before the test. This prevented CPU1 contention and
  prevented old 1240-byte traffic from mixing with the result.
- Daemon/overlay remained loaded. No second `Overlay()` was created.
- Sender: board `/tmp/gso`.
- Sender CPU: CPU1.
- UDP destination: PC port 5604.
- GSO segment size: 1280 bytes.
- GSO batch: 32 segments.
- GSO request size: 32 x 1280 = 40,960 bytes.
- Test duration: 2 seconds.

## Results

Board sender:

```text
udp_gso(seg=1280,batch=32):
  packets submitted: 146,432
  reported rate:     73,209 packets/s
  Ethernet-frame estimate: 774.3 Mbit/s
```

The UDP payload rate is:

```text
73,209 x 1280 x 8 = 749.7 Mbit/s
```

Windows application receiver on port 5604:

```text
packets received: 146,432
packet lengths:   146,432 packets of exactly 1280 bytes
```

Windows PktMon capture at the NIC (`--comp nics`):

```text
Packets total:       146,432
Packet drop count:         0
Packets formatted:    146,432
```

Captured Ethernet frame length:

```text
1322 bytes = 14 Ethernet + 20 IPv4 + 8 UDP + 1280 UDP payload
```

The application count and the sender count matched exactly. The NIC capture
reported zero packet drops.

## Comparison with the requirement

Current 1176-byte OSV payload geometry:

```text
required: 70,560 packets/s
measured: 73,209 packets/s
margin:    2,649 packets/s = 3.75%
```

Therefore 1280-byte UDP GSO passes the measured packet-rate gate on the real
board, PC, switch, and normal-MTU network path.

## Decision

**Do not change B.2 to 1240-byte slots.**

Keep:

```text
B.2 slot stride = 1280 bytes
B.3 UDP GSO segment = 1280 bytes
```

But B.2 must be reopened for one controlled functional change:

```text
B.1 input:       1240 bytes
B.2 slot output: 1280 bytes
slot bytes 0..1239:  B.1 authenticated data
slot bytes 1240..1279: explicit zero transport padding
```

This is not a 1240-byte alignment redesign. It keeps the existing 1280-byte
slot address and burst layout. It makes the 40-byte area deterministic so
B.3 can send complete slots directly with UDP GSO.

## Required B.2 change before B.3

1. Keep `SLOT_STRIDE=1280`.
2. Keep the B.1 input packet at 1240 bytes.
3. Extend the internal packet buffer from 155 to 160 64-bit words.
4. Copy B.1 words 0..154 unchanged.
5. Set words 155..159 to zero after the B.1 TLAST.
6. Write ten complete 16-beat AXI bursts (1280 bytes).
7. Publish `produce_idx` only after the tenth burst B response.
8. Require the ring base to be 128-byte aligned.
9. Add testbench checks for:
   - every burst staying within a 4 KiB boundary;
   - bytes 0..1239 matching B.1;
   - bytes 1240..1279 equal to zero;
   - publication after the final padding burst;
   - forced-full packet-atomic drops.
10. Re-run standalone B.2 and B.1 -> B.2 composition tests.

## Required B.3 behavior after B.2 passes

```c
UDP_SEGMENT = 1280;
send(sock, ring_slot_start, batch_count * 1280, 0);
```

The PC receiver must:

1. Require a 1280-byte UDP payload.
2. Verify bytes 1240..1279 are all zero.
3. Pass only bytes 0..1239 to the existing nonce/GCM parser.
4. Preserve `bad=0` as the acceptance condition.

## Important limits

- The measured 73,209 packets/s has only a 3.75% margin over the current
  70,560 packets/s requirement. The test used a static payload and did not
  include PL ring reads, cache invalidation, or the final B.3 ring consumer.
- The B.2 padding write adds 40 DDR bytes per packet, but it removes the PS
  gather copy and makes the GSO source contiguous.
- No B.3 code has been accepted from this measurement.
- No B.2 code has been changed after the measurement.
