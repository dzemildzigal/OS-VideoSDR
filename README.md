# OS-VideoSDR

Live video over Ethernet, encrypted end to end with AES-256-GCM.

A PYNQ-Z2 board captures an HDMI input, encrypts every video packet in the
FPGA, parks the packets in a DDR ring, and pushes them out of its Gigabit
port. A PC receives them, checks every authentication tag, rebuilds the frames
and shows or records the video.

```text
   HDMI source            PYNQ-Z2 (Zynq-7020)                       PC
  ┌───────────┐   ┌──────────────────────────────────┐   ┌────────────────────┐
  │ 720p60    │   │ packetizer → AES-256-GCM → DDR   │   │ UDP → authenticate │
  │ video out ├──►│              ring      ring → UDP ├──►│ → reassemble → MP4 │
  └───────────┘   │   (FPGA)          (FPGA)  (PS)    │   │   or live window   │
                  └──────────────────────────────────┘   └────────────────────┘
```

The FPGA side lives in the companion repository
**[AES-256-SystemVerilog](https://github.com/dzemildzigal/AES-256-SystemVerilog)** —
that is where the AES core, the packetizer, the DDR ring writer and the
bitstream build live. This repository is the system around it: the PS sender,
the protocol, the PC receiver and the tests.

## What works today

```text
video         1280x720 RGB888 at 30 fps
on the wire   62,850 packets/s (2,095 segments per frame), about 88 MB/s
crypto        AES-256-GCM per packet, 100% of packets authenticate
board         sends with zero drops while the machine is quiet
PC            receives about 80% of packets on the test NIC (see Limits)
```

## How it works

```text
1. The packetizer cuts every video frame into 2,095 segments of 440 pixels
   and wraps each one in a small header. The source runs at 60 Hz, so every
   second frame is dropped: that is where the 30 fps comes from.

2. The AES core encrypts each segment and appends a 16-byte tag, so a packet
   that is altered or replayed on the wire fails the tag check on the PC.

3. The ring writer writes each finished packet into a slot of a DDR ring
   (2,048 slots of 1,408 bytes) and publishes a counter. The ring holds about
   33 ms of video, which is what absorbs moment-to-moment timing noise.

4. The PS sender (tx_shim) follows that counter, and sends the slots in order
   with UDP GSO, 32 slots per system call, from two sockets on two cores.
   It tells the writer which slots it has finished with, so the FPGA can
   reuse them.

5. The PC authenticates every packet, puts the segments back in order and
   hands complete frames to a display or a video file.
```

## Quick start

### 1. Get both repositories

```bash
git clone https://github.com/dzemildzigal/OS-VideoSDR
git clone https://github.com/dzemildzigal/AES-256-SystemVerilog
```

### 2. Put the overlay on the board

Build it in the AES repository (about an hour in Vivado), or copy a released
pair. The board expects them here:

```text
/home/xilinx/jupyter_notebooks/OS-VideoSDR/pynq/overlays/tx/hdmi_aes_tx.bit
/home/xilinx/jupyter_notebooks/OS-VideoSDR/pynq/overlays/tx/hdmi_aes_tx.hwh
```

### 3. Start the board side

```bash
# as root on the board, in a login shell with XRT available
cd /home/xilinx/jupyter_notebooks/OS-VideoSDR/pynq/runtime
export XILINX_XRT=/usr
python3 tx_daemon.py \
    --bitstream ../overlays/tx/hdmi_aes_tx.bit \
    --dst-host 192.168.0.37 --dst-port 5600 \
    --key-hex 000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f \
    --payload-bytes 1320 --configure-only --aes-freq 100
```

The daemon loads the overlay, sets the design clock, programmes the AES
session and then holds the overlay. It deliberately does not touch the ring.

```bash
# build and start the sender (same login shell, as root)
cd /home/xilinx/jupyter_notebooks/OS-VideoSDR/pynq/ps_shim
./build.sh                     # produces build/tx_shim
./build/tx_shim 192.168.0.37 5600 --workers 2 --pin --sndbuf 180224
```

`--payload-bytes 1320` must match the bitstream. The daemon checks it against
the handoff file and refuses to start on a mismatch, because a wrong value
makes the AES hash the wrong length and every tag fails while the ciphertext
still looks perfect.

### 4. Receive on the PC

```bash
cd OS-VideoSDR
pip install cryptography numpy opencv-python

# record 60 s to recordings/osv_720p30_<timestamp>.mp4
PYTHONPATH=. python pc/runtime/rx_record.py 60

# or watch it live (opens an "OS-VideoSDR" window; q or Esc quits)
PYTHONPATH=. python pc/runtime/rx_display.py 120
```

Both print packets per second, authentication failures and frame
completeness while they run.

## Tools on the PC

| file | what it does |
|------|--------------|
| `pc/runtime/rx_record.py` | records the stream to an MP4 |
| `pc/runtime/rx_display.py` | live OpenCV window |
| `pc/runtime/rx_snapshot.py` | saves one frame as a PNG and prints pixel samples |
| `pc/runtime/rx_lossless.py` | lean receiver that only counts frames, for measurements |
| `pc/runtime/main_rx.py` | the full runtime with config files and a display abstraction |
| `pc/runtime/aes_gcm_sw.py` | software AES-GCM helper |

## Repository layout

```text
protocol/        packet header, constants, validation, replay window
pc/runtime/      everything that runs on the PC
pynq/runtime/    tx_daemon.py: overlay, clock, AES session configuration
pynq/ps_shim/    tx_shim: the DDR-ring-to-UDP sender (C++)
pynq/tools/      board samplers used to debug stability
tests/           unit and integration tests (python -m pytest tests)
docs/            the design, plans, status reports and measurements
config/          network.yaml, crypto.yaml
```

## Limits you should know

```text
1. The PC side loses about 20% of packets on the test NIC (Windows, Realtek
   GbE). A frame needs all 2,095 of its segments, so the picture is live but
   shows patches where packets were lost. Fewer, larger packets would fix it,
   but the 1500-byte MTU blocks that, and jumbo frames are not available: the
   board's macb driver refuses MTU above 1500 and the test switch drops them.

2. There is no FEC and no retransmission. Every lost packet costs part of a
   frame. That is the main thing standing between this and a production link.

3. The design expects exactly 720p60 on the HDMI input; the packetizer closes
   frames by counting pixels. A different source mode would need work.

4. 720p is where the geometry sits today, not full HD. 1080p would need a
   different pixel format or a bigger budget for CPU and bandwidth.

5. The AES key is passed on the command line, so it is visible in `ps`. Use a
   file with permissions before using this anywhere real.
```

## Documentation

```text
docs/architecture.md                     the system and its building blocks
docs/protocol_spec.md                    wire format and header fields
docs/DESIGN_2026-08-23_phase_B.md        the transport redesign rationale
docs/PLAN_2026-08-24_phase_B_exact.md    the exact plan and acceptance criteria
docs/STATUS_2026-09-12_geometry_1408.md  latest status, measurements, root causes
docs/PHASE_C_D_COMPLETION.md             runtime, config and test scaffolding
docs/crypto_policy.md                    key, nonce and AAD policy
```

## Tests

```bash
python -m pytest tests -q
```

Hardware measurements are in `docs/STATUS_*.md`; each one lists the commands
that produced the numbers.
