# What is next (2026-09-12, after the 1408 geometry work)

## The measurement that changes the plan

The datagram size is a free variable, and we chose it badly.

```text
geometry               segs/frame   packets/s   on the wire
720p30 RGB888 1320 B       2,095      62,850   717 Mbit/s  (72% of 1 GbE)  today
720p30 RGB888 2752 B       1,005      30,150   698 Mbit/s  (70% of 1 GbE)  same video
720p30 RGB888 5488 B         504      15,120   689 Mbit/s  (69% of 1 GbE)  same video
```

Same picture, same bandwidth (slightly less overhead), **half or a quarter of
the packets per second**. Per-packet cost is what limits both ends:

```text
PS sender   ~1.8 s of CPU per second of video at 62,850 pkt/s (two cores)
PC receiver Windows kernel plus the socket path loses about 20% of 62,850
            packets/s; the bare receive loop without any crypto reached
            34,000-53,000 pkt/s in tests, so the loss is per-packet cost, not
            the wire, not the NIC's error counters (they stayed at zero)
```

1408 bytes was chosen to stay inside one Ethernet frame. That reason does not
hold: a larger datagram becomes two IP fragments, which costs about 1% of
overhead and nothing else, because a lost packet was already a lost segment.
This also means jumbo frames were never the blocker.

## Tier 0 — cheap, and they decide the rest

```text
0.1  Bigger slot: 2,816-byte slots (22 x 128) with a 2,752-byte payload.
     1,005 segments per frame, 30,150 pkt/s.
     RTL: SLOT_STRIDE and PX_PER_SEG parameters, one rebuild (~1 h).
     Shim: SLOT_STRIDE, MAX_GSO_SLOTS (32 x 2,816 = 90 KB exceeds the 64 KB
     GSO limit, so 16-23 per send).
     PC: one place for the geometry instead of constants in five files.
     Expected: both ends pay half the per-packet cost. 1080p becomes tractable
     for the same reason.

0.2  Measure the PC's receive ceiling with the bigger datagrams. This decides
     whether the Python receiver is enough or whether a C receiver is needed
     at all. Your instinct about C may well be right, but it may no longer be
     necessary once there are half as many packets.

0.3  A 30-minute soak with the sampler running. This is Phase B's own
     acceptance criterion (G3c) and we have never run it. It either passes or
     it explains the crash.
```

## Tier 1 — do these only if Tier 0 says so

```text
1.1  C receiver for the PC          only if 0.2 still shows material loss
1.2  Ring depth 33 ms -> 130 ms     only if the soak shows scheduling noise
                                    (one RTL constant, same rebuild window)
1.3  IRQ affinity experiments       only if 0.2 shows softirq pressure
```

## Tier 2 — the roadmap, for a scope decision by you

```text
Full HD, honestly measured:

1080p30 RGB888   1,613 Mbit/s   161% of 1 GbE   impossible
1080p30 YUV422   1,075 Mbit/s   108%           just over
1080p30 YUV420     785 Mbit/s    79%           fits, with headroom
1080p60 YUV420   1,570 Mbit/s   157%           impossible on 1 GbE

So the full HD answer is 1080p30 in YUV420, and it needs three things:

  a) an RGB -> YUV420 converter in the PL. The colour matrix needs about
     11 DSPs at 1080p30 rates, which the 7020 has, plus line buffers for the
     chroma subsampling.
  b) a faster AES path. Today the core moves about 0.88 bytes per cycle
     (88 MB/s at 100 MHz). 1080p30 YUV420 needs ~96 MB/s of ciphertext plus
     headers, about 1.1 bytes per cycle, so the GHASH/AES path must be
     pipelined or the design clock raised. This makes the timing work
     load-bearing rather than optional.
  c) the geometry change from 0.1, which is why 0.1 comes first.

1080p60 needs more than 1 GbE and is therefore a hardware change, not a
software one.
```

## Ghosts - stop chasing these

```text
NIC driver version        the 2016 driver was fine; only the 2026 driver's
                          power-saving defaults (Gigabit Lite, Green Ethernet)
                          ever mattered, and they are off
heat                      55-60 C flat through full-rate runs
jumbo frames              not needed, IP fragments are fine (see above)
PC back-pressure          the real mechanisms were ICMP on connected sockets
                          and my own probe load, both now fixed and understood
MTU 1500 as a wall        a red herring of our own design choice
```

## Recommended next slice

```text
Tier 0.1 + 0.2 in one session: one rebuild, one shim change, one place for the
geometry on the PC, then measure with the same recorder. If the loss falls as
the packet count falls, we have our answer without writing a C receiver, and
1080p30 becomes a realistic next target.
```
