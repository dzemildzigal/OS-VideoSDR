# OS-SDR
Open Source - Software Defined Radio - 0 to full SDR capability from scratch. Xilinx and Espressif powered.

# Encrypted FHSS Software-Defined Radio — Project Plan

**Platform:** Zynq-7020 (PYNQ-Z2) + ESP-32  
**Encryption:** AES-256-GCM (FPGA-verified at 761 MiB/s; ESP-32 hardware AES for Phase 0)  
**Target frequency:** 2.4 GHz band  
**Goal:** High-range, high-throughput, frequency-hopping encrypted radio link  
**Architecture:** Phase 0 (ESP-32 proof) → Phase 1 (PYNQ-Z2 + nRF) → Phase 2 (full SDR)  
**Field reprogramming:** Two FPGA bitstreams — TX and RX — swappable via PYNQ overlay load  
**One PYNQ-Z2 is enough to start.** The ESP-32 from Phase 0 acts as the second node for all Phase 1 testing.

---

## Table of Contents

1. [Problem Definition](#problem-definition)
2. [The Fundamental Challenges](#the-fundamental-challenges)
3. [What Goes Where — PL vs PS Split](#what-goes-where)
4. [The Signal Chain](#the-signal-chain)
5. [Phase 0 — Two ESP-32s + nRF24L01+PA (Prove the Radio Link Fast)](#phase-0)
6. [Phase 1 — PYNQ-Z2 + nRF24L01+PA (Protocol Radio, FPGA FHSS)](#phase-1)
7. [Phase 2 — LMS6002D (True SDR, Full Baseband Control)](#phase-2)
8. [Link Budget and Range Reality](#link-budget-and-range)
9. [FHSS Synchronisation — The Hardest Problem](#fhss-synchronisation)
10. [Full Duplex Path (Phase 2 Goal)](#full-duplex)
11. [Bitstream Strategy](#bitstream-strategy)
12. [Recommended Build Order](#recommended-build-order)
13. [Bill of Materials Summary](#bill-of-materials)

---

## Problem Definition

The goal is a complete encrypted radio communication system built around the Zynq-7020 FPGA. The Zynq programmable logic (PL) handles all real-time signal processing and encryption. The processor system (PS) handles configuration, key management, and orchestration.

The system must:
- Operate in the 2.4 GHz ISM band
- Use frequency-hopping spread spectrum (FHSS) for robustness and security
- Encrypt all payload data using the AES-256-GCM core already built and verified
- Achieve multi-kilometre range
- Support reasonably high data throughput
- Be field-reprogrammable (TX ↔ RX role swap by loading a different bitstream)

The Zynq-7020 has no RF capability on its own. It is a digital device. An external RF transceiver chip is mandatory — this is a hardware fact, not a design choice.

---

## The Fundamental Challenges

These are the real problems you will encounter, in order of difficulty.

### 1. FHSS Synchronisation — Hardest Problem in the System

Both radios must jump to the same frequency at the same moment. If one is on channel 47 and the other is on channel 12, no data is received. You need either:

- A **shared absolute time reference** — GPS 1PPS on both ends is reliable but requires GPS hardware
- A **synchronisation burst protocol** — one radio periodically broadcasts a sync frame, both sides lock to it before beginning the hop schedule
- **Pre-agreed epoch + hop schedule** — both radios derive the same PN sequence from the same key, and both use a real-time clock as the epoch. They hop together because the math gives the same sequence at the same time.

Without solving this first, FHSS does not work. Plan the synchronisation protocol before writing any baseband DSP.

### 2. Multi-kilometre Range at 2.4 GHz

The physics work against you at this frequency over long distances. Free-space path loss at 2.4 GHz over 5 km is approximately:

```
L = 20·log10(5000) + 20·log10(2.4×10⁹) + 20·log10(4π/c)
L ≈ 128 dB
```

That means your signal arrives 128 dB weaker than it was transmitted. For comparison, going from 1 watt to 6 picowatts.

You can recover this with:
- **High TX power** — 1 W (30 dBm) amplifier
- **Directional antennas** — a pair of 12 dBi Yagi antennas adds 24 dB to the link
- **Good receiver sensitivity** — a quality LNA + RF front end reaches −100 dBm at reasonable bandwidths
- **FEC (forward error correction)** — lets you recover packets even at very low SNR

With all of the above: 30 + 24 − 128 = −74 dBm received. Versus −100 dBm sensitivity. That is 26 dB of link margin — workable in clear line-of-sight.

**Realistic warnings:**
- 2.4 GHz is extremely congested. WiFi, Bluetooth, baby monitors, and microwave ovens all share this band. In urban environments, interference will dominate.
- True line-of-sight is required. Trees, buildings, and hills at this frequency cause significant additional loss.
- High throughput and multi-km range are in tension. Narrowing bandwidth improves sensitivity (lowers noise floor) but reduces throughput. You must pick a balance point.

### 3. Full Duplex — Phase 2 Problem, Not Phase 1

True simultaneous transmit and receive at the same frequency requires cancelling your own transmitted signal, which arrives at your own RX port roughly 80–100 dB stronger than the incoming signal from the other radio. This is called self-interference cancellation (SIC) and is an active research area.

Practical options for Phase 2:
- **TDD (time-division duplex)** — TX and RX take turns on the same frequency, very fast alternation gives the appearance of full duplex. AD9361/LMS6002D both support this natively.
- **FDD (frequency-division duplex)** — TX and RX use different frequencies simultaneously, separated by a duplexer filter. Requires two frequency allocations but is true simultaneous operation.
- True SIC full duplex on one frequency — very advanced, not Phase 1 scope.

### 4. Baseband DSP Complexity (Phase 2 only)

In Phase 1 with the nRF24L01+, the chip handles modulation/demodulation internally. You send bytes and receive bytes. This is easy.

In Phase 2 with a true SDR chip, you receive raw I/Q samples and must implement in PL:
- **Matched filter** — optimal filtering for your modulation
- **Timing recovery** — figure out exactly where each symbol starts, to within a fraction of a sample period
- **Carrier phase/frequency recovery** — the TX and RX oscillators are never perfectly matched; you must track and correct the offset continuously
- **Equalisation** — compensate for channel distortion (multipath, fading)

These algorithms (Gardner loop, Costas loop, Viterbi, etc.) are well-understood but each one takes careful implementation and tuning. This is the majority of the Phase 2 engineering work.

### 5. Analog RF Design

Getting clean RF in and out of any chip requires careful PCB layout — proper ground planes, controlled impedance transmission lines, decoupling capacitors in the right places. If you are using a module (Phase 1), this is handled for you. If you are designing a custom PCB around a bare chip (Phase 2), RF PCB layout becomes a non-trivial skill requirement.

---

## What Goes Where

The Zynq has two parts: the PS (ARM processor running Linux) and the PL (the FPGA fabric). This is how work splits between them.

### PL Handles (Real-time, deterministic, hardware speed)

| Function | Details |
|----------|---------|
| AES-256-GCM encryption/decryption | Already implemented, 761 MiB/s verified |
| SPI master to RF transceiver | Controls frequency, power, configuration |
| FHSS hop controller | Executes pre-computed hop schedule, triggers SPI writes on schedule |
| DMA data movement | Already implemented with AXI DMA |
| Digital upconversion (DUC) | Phase 2 — interpolation FIR filters, I/Q mixing to IF |
| Digital downconversion (DDC) | Phase 2 — decimation FIR filters, I/Q mixing to baseband |
| Modulator | Phase 2 — QPSK/QAM symbol mapping and pulse shaping |
| Demodulator | Phase 2 — symbol detection, timing recovery |
| Forward error correction | Phase 2 — convolutional encoder/Viterbi decoder |
| Packet framer/deframer | Preamble, sync word, header, CRC |
| LVDS I/Q interface to ADC/DAC | Phase 2 — high-speed parallel digital interface to SDR chip |

### PS Handles (Non-real-time, software)

| Function | Why PS |
|----------|--------|
| Key exchange and key loading | Security-sensitive, needs cryptographic protocol |
| Hop schedule generation | Computed from session key, handed to PL at session start |
| System configuration at boot | Load parameters, frequencies, modulation scheme |
| PYNQ overlay management | Load TX or RX bitstream as needed |
| Logging and diagnostics | Python on Linux |
| Higher-layer protocols | ARQ, flow control, application framing |

---

## The Signal Chain

### TX Path (complete, both phases)

```
[Application data]
        │
        ▼
[PS: framing + session setup]
        │
        ▼
[PL: AES-256-GCM encrypt]        ← session key from PS
        │
        ▼
[PL: FEC encode]                 ← Phase 2; skip in Phase 1
        │
        ▼
[PL: Packet framer]              ← adds preamble, sync word, length, CRC
        │
        ▼
[Phase 1]──────────────────────────────────────────────────────┐
        │                                                       │
[PL: SPI → nRF24L01+ TX]                                       │
        │                                                       │
[nRF24L01+ module: GFSK modulation, PA, antenna]               │
                                                                │
[Phase 2]──────────────────────────────────────────────────────┘
        │
[PL: QPSK/QAM modulator]
        │
[PL: DUC — interpolation FIR, mix to IF]
        │
[PL: LVDS I/Q → LMS6002D DAC]
        │
[LMS6002D: mix to 2.4 GHz]
        │
[External PA: amplify to 1 W]
        │
[BPF: 2.4 GHz band-pass filter]
        │
[Antenna]
```

### RX Path (complete, both phases)

```
[Antenna]
        │
[BPF: 2.4 GHz band-pass filter]
        │
[Phase 1]──────────────────────────────────────────────────────┐
        │                                                       │
[nRF24L01+ module: LNA, GFSK demodulation]                     │
        │                                                       │
[PL: SPI ← nRF24L01+ RX]                                       │
                                                                │
[Phase 2]──────────────────────────────────────────────────────┘
        │
[External LNA: low-noise amplification]
        │
[LMS6002D: mix down from 2.4 GHz, ADC]
        │
[PL: LVDS I/Q ← LMS6002D]
        │
[PL: DDC — decimation FIR, mix to baseband]
        │
[PL: QPSK/QAM demodulator + timing recovery]
        │
        ▼
[PL: Packet deframer]            ← check preamble, sync, CRC
        │
        ▼
[PL: FEC decode]                 ← Phase 2; skip in Phase 1
        │
        ▼
[PL: AES-256-GCM decrypt]        ← session key from PS
        │
        ▼
[PS: application data]
```

---

## Phase 0

### Two ESP-32s + nRF24L01+PA — Prove the Radio Link Fast

**Goal:** Get two nodes transmitting and receiving encrypted, frequency-hopping data over RF as fast as possible. No FPGA required. Total hardware cost under $25.

**Philosophy:** Before spending time on FPGA bitstreams, verify that the nRF24L01+ modules work at range, that the FHSS timing is sane, and that the throughput meets your expectations. The ESP-32 is fast enough (240 MHz dual-core Xtensa LX6) to run the FHSS scheduler and AES encryption entirely in software. This phase also gives you a fully working second radio node — so you only ever need **one PYNQ-Z2** board.

---

### The Hardware: ESP-32

The ESP-32 (specifically the ESP32-WROOM-32 module or any ESP32-DevKitC board) is the right chip for this phase.

Key specs relevant here:
- CPU: 240 MHz dual-core, no pipeline stalls on simple loops
- Hardware SPI: up to 80 MHz (nRF24L01+ needs max 10 MHz — trivially within spec)
- Hardware AES accelerator: AES-128/192/256, ECB/CBC/CTR modes — built into silicon, about 10× faster than software AES
- GPIO: 3.3 V logic — directly compatible with nRF24L01+ (no level shifter needed)
- Price: $3–6 for a DevKit board on AliExpress, $8–12 on Amazon
- Toolchain: ESP-IDF (C/C++) or Arduino framework

The ESP-32's hardware AES accelerator means you can encrypt every packet before transmission with near-zero CPU overhead, even at maximum nRF throughput.

---

### Wiring: ESP-32 to nRF24L01+PA

Connect over hardware SPI. Use the VSPI or HSPI peripheral — both work.

| nRF24L01+ Pin | ESP-32 Pin (VSPI) | Notes |
|---------------|------------------|-------|
| VCC | 3.3 V | Do NOT use 5 V — chip is 3.3 V max |
| GND | GND | — |
| CE | GPIO 4 (any GPIO) | Active high, controls TX/RX mode |
| CSN | GPIO 5 (SS) | Active low chip select |
| SCK | GPIO 18 | SPI clock |
| MOSI | GPIO 23 | SPI data out |
| MISO | GPIO 19 | SPI data in |
| IRQ | GPIO 2 (any GPIO) | Active low, packet RX/TX done interrupt |

The PA+LNA modules draw up to 130 mA during TX. Power them from a dedicated 3.3 V regulator, not from the ESP32's on-board LDO (which is typically rated 500 mA and shared with everything else). A small AMS1117-3.3 regulator off a USB power bank is sufficient.

---

### Software Stack (ESP-IDF or Arduino)

The full Phase 0 firmware stack is pure C/C++:

```
┌────────────────────────────────────────────────┐
│  Application layer                             │
│  - Packet generator (test data or real payload)│
│  - Throughput counter + UART logger            │
├────────────────────────────────────────────────┤
│  AES-256 encryption layer                      │
│  - Uses ESP-IDF mbedTLS (wraps HW accelerator) │
│  - Encrypts each packet payload before TX      │
│  - Decrypts and verifies tag after RX          │
├────────────────────────────────────────────────┤
│  FHSS scheduler                                │
│  - dwell timer (FreeRTOS tick or hardware timer)│
│  - PN sequence from AES-derived seed           │
│  - Writes RF_CH register on each hop           │
├────────────────────────────────────────────────┤
│  nRF24L01+ driver                              │
│  - SPI register read/write                     │
│  - TX FIFO load, CE pulse, IRQ poll/interrupt  │
│  - RX FIFO drain                               │
├────────────────────────────────────────────────┤
│  ESP-IDF SPI master driver                     │
│  (hardware peripheral, DMA-capable)            │
└────────────────────────────────────────────────┘
```

Good open-source nRF24L01+ drivers for ESP-IDF exist (e.g. nrf24 by nopnop2002 on GitHub). Use one as a starting point and add your FHSS + AES layer on top.

---

### Maximum Throughput: What to Expect

The nRF24L01+ at 2 Mbps air data rate with 32-byte packets:

| Parameter | Value |
|-----------|-------|
| Air data rate | 2 Mbps |
| Packet payload | 32 bytes |
| Packet overhead (preamble + address + CRC) | ~9 bytes |
| Effective bits per packet on air | ~256 bits payload |
| Time per packet on air | ~256 µs |
| SPI load time (10 MHz, 32 bytes) | ~26 µs |
| Auto-ACK round trip (if enabled) | +250 µs |
| Practical throughput **without** ACK | ~800 kbps |
| Practical throughput **with** auto-ACK | ~400 kbps |

For Phase 0 max throughput testing: **disable auto-ACK**, use 2 Mbps, 32-byte payloads, and measure how many packets per second arrive correctly. Target is 25,000+ packets/second = 800 kbps effective.

The FHSS dwell time is the other constraint. At 300 µs dwell (minimum safe settling time), 1000 hops per second, each dwell carries approximately 1 packet. For a longer dwell of 5 ms, ~15 packets per dwell, much more efficient.

**Recommended starting point for Phase 0:** 5 ms dwell, 2 Mbps, ACK disabled, 32-byte packets. Measure PER (packet error rate) and throughput over the air at 10 m first, then increase range.

---

### FHSS in Software on ESP-32

The hop schedule runs on a FreeRTOS hardware timer (not the tick timer — use the `esp_timer` or a GPTimer for microsecond accuracy):

```c
// Pseudocode — ESP-IDF
void hop_timer_callback(void *arg) {
    uint8_t next_channel = hop_table[hop_index % HOP_TABLE_LEN];
    nrf24_set_channel(next_channel);   // SPI write to RF_CH register
    hop_index++;
}

// hop_table[] is generated at session start:
void generate_hop_table(const uint8_t *session_key, uint8_t *table, int len) {
    // Use AES-CTR with session_key to produce pseudorandom bytes
    // Map each byte to a valid channel (0-124)
    for (int i = 0; i < len; i++)
        table[i] = aes_prng_byte(session_key, i) % 125;
}
```

The ESP-32 hardware AES block generates the hop table in microseconds. The timer callback itself takes under 10 µs (one SPI transaction). This is deterministic enough for FHSS at dwell times above 1 ms.

---

### AES Integration in Phase 0

The ESP-32 has a hardware AES-256 accelerator accessible through ESP-IDF's mbedTLS:

```c
#include "mbedtls/aes.h"

// Encrypt one 16-byte block (or chain for longer payloads)
mbedtls_aes_context ctx;
mbedtls_aes_init(&ctx);
mbedtls_aes_setkey_enc(&ctx, session_key, 256);
mbedtls_aes_crypt_ecb(&ctx, MBEDTLS_AES_ENCRYPT, plaintext, ciphertext);
```

For a proper authenticated encryption scheme matching the FPGA AES-GCM core, use AES-GCM mode:

```c
#include "mbedtls/gcm.h"

mbedtls_gcm_context gcm;
mbedtls_gcm_init(&gcm);
mbedtls_gcm_setkey(&gcm, MBEDTLS_CIPHER_ID_AES, session_key, 256);
mbedtls_gcm_crypt_and_tag(&gcm, MBEDTLS_GCM_ENCRYPT,
    payload_len, nonce, 12,
    aad, aad_len,
    plaintext, ciphertext,
    16, tag);
```

This uses the same AES-256-GCM algorithm as the FPGA core, meaning Phase 0 ciphertext is directly interoperable with Phase 1 (PYNQ-Z2 decrypts what the ESP-32 encrypted, and vice versa).

**Encryption overhead at max nRF throughput:** encrypting 32 bytes with hardware AES-GCM on ESP-32 takes approximately 2–4 µs — negligible compared to the 256 µs packet air time.

---

### Phase 0 Goals and Exit Criteria

Phase 0 is complete when:

- [ ] Two ESP-32 + nRF modules communicate reliably at 10 m range
- [ ] FHSS hopping is running (both nodes on same schedule, same timing)
- [ ] AES-256-GCM encrypt on TX, decrypt + tag verify on RX — all in software
- [ ] Throughput measured and logged over UART: target >400 kbps with ACK, >700 kbps without
- [ ] Range test at 100 m (line of sight), PER < 1%
- [ ] Both nodes use identical firmware with only TX/RX role flag different

At this point the RF link is validated. The nRF modules, antennas, and power supply are known-good. Moving to Phase 1, the ESP-32 remains as the second node while the PYNQ-Z2 replaces one ESP-32.

---

### Phase 0 Block Diagram

```
Node A (ESP-32 TX)                    Node B (ESP-32 RX)
┌──────────────────────┐              ┌──────────────────────┐
│  Application data    │              │  Application data    │
│         │            │              │         ▲            │
│  AES-256-GCM encrypt │              │  AES-256-GCM decrypt │
│  (HW accelerator)    │              │  (HW accelerator)    │
│         │            │              │         │            │
│  Packet framer       │              │  Packet deframer     │
│  (add seq num, len)  │              │  (check len, seq)    │
│         │            │              │         │            │
│  FHSS scheduler      │              │  FHSS scheduler      │
│  (same hop table)    │              │  (same hop table)    │
│         │            │              │         │            │
│  nRF24 SPI driver    │              │  nRF24 SPI driver    │
└─────────┬────────────┘              └─────────┬────────────┘
          │ SPI (10 MHz)                        │ SPI (10 MHz)
   ┌──────┴──────┐                       ┌──────┴──────┐
   │ nRF24L01+   │   ~~~~ RF ~~~~        │ nRF24L01+   │
   │ PA+LNA      │ ─────────────────────►│ PA+LNA      │
   └─────────────┘  2.4 GHz FHSS GFSK   └─────────────┘
```

---

### Phase 0 Resource Summary

| Item | Notes |
|------|-------|
| Hardware | 2× ESP32-DevKitC + 2× nRF24L01+PA+LNA module |
| Firmware | ESP-IDF C/C++, FreeRTOS |
| AES | Hardware accelerator via mbedTLS |
| FHSS | Software scheduler, hardware timer, 1–10 ms dwell |
| Encryption interop | AES-256-GCM — same algorithm as FPGA core |
| Second node needed for Phase 1 | YES — the Phase 0 ESP-32 node stays as-is |

---

## Phase 1

### nRF24L01+PA Module — PYNQ-Z2 FPGA FHSS Radio

**Goal:** Replace one ESP-32 node with the PYNQ-Z2 FPGA. The FPGA handles FHSS timing and AES-GCM in hardware, achieving far higher determinism and throughput than the software implementation. The second node remains the Phase 0 ESP-32 — no second PYNQ-Z2 required.

**Philosophy:** The nRF24L01+ chip handles everything RF — modulation, demodulation, CRC, automatic retransmit. The FPGA just controls it over SPI and manages the hop schedule. This bypasses the entire baseband DSP problem for Phase 1. Because AES-256-GCM is the same algorithm on both sides, the PYNQ-Z2 and the ESP-32 are fully interoperable from day one.

---

### The Chip: nRF24L01+

The nRF24L01+ is a single-chip 2.4 GHz transceiver made by Nordic Semiconductor. It implements GFSK modulation, handles packet framing, ACK, and retransmit internally. You communicate with it via SPI.

Key parameters:
- Frequency range: 2.400 – 2.525 GHz
- Channel spacing: 1 MHz, 125 channels available
- Modulation: GFSK (fixed, not programmable)
- Data rates: 250 kbps, 1 Mbps, 2 Mbps
- Packet size: up to 32 bytes per packet
- CRC: built-in 1 or 2 byte CRC
- ACK and auto-retransmit: built-in (can be disabled)
- SPI interface: up to 10 MHz, standard 4-wire + CE + IRQ
- Supply voltage: 1.9 – 3.6 V

---

### The Module: nRF24L01+LNA+PA variant

Do not buy the bare nRF24L01+ breakout (range is only ~100 m indoors). Buy the **PA+LNA module** version. These have an integrated power amplifier (+20 dBm / 100 mW output) and a low-noise amplifier on the receive path, plus a PCB antenna or u.FL connector for an external antenna.

Common part names: E01-ML01DP5, NRF24L01+PA+LNA, Si24R1 PA module.

**Price:** $2–5 per module on AliExpress, $5–10 on Amazon.

With directional antennas, these modules reliably reach 1–2 km in clear conditions.

---

### SPI Interface to Zynq-7020

The nRF24L01+ connects to the Zynq via a PMOD header — no level shifter needed if your PMOD is 3.3 V (PYNQ-Z2 PMODs are 3.3 V).

Pinout needed:
- MOSI, MISO, SCK, CSN (chip select, active low) — SPI bus
- CE — chip enable (active high during TX/RX)
- IRQ — interrupt from chip when packet received or TX complete (active low)

The SPI controller in PL is a small (~100 line) RTL block. It writes register addresses and data to the chip at up to 10 MHz. All configuration (frequency channel, data rate, TX power, addresses) is done via SPI register writes.

---

### FHSS Implementation with nRF24L01+

Frequency hopping with this chip is straightforward:

1. The chip has a register (`RF_CH`) that sets the RF channel (0–124, meaning 2.400–2.524 GHz in 1 MHz steps).
2. Writing a new value to this register over SPI changes the frequency.
3. The PL SPI master writes a new channel number at each hop interval.
4. The hop schedule is a pseudorandom sequence derived from the AES session key, generated by the PS at session start and loaded into PL memory.

SPI register write takes about 2 µs. After the write, the chip needs a small settling time (~130 µs to switch from standby to TX or RX). So minimum practical dwell time per channel is about 200–300 µs.

The PL hop controller is essentially:
- A counter that counts down the dwell time
- On expiry: assert SPI write with next channel number from hop table
- When SPI write completes: start new dwell timer

This runs entirely in PL with cycle-accurate timing.

---

### AES-256-GCM Integration in Phase 1

Since the nRF24L01+ handles at most 32 bytes per packet, the AES-GCM integration is simple:

- Each packet payload is 32 bytes or less.
- The PS encrypts a buffer of plaintext using the AES-GCM core over DMA.
- The resulting ciphertext + tag is split into 32-byte chunks.
- Each chunk is handed to the nRF SPI master for transmission.
- On the RX side, received chunks are reassembled in a PS buffer.
- When a complete message is received, AES-GCM decrypts and verifies the tag.

The authentication tag (16 bytes) can be appended as the final packet in a message sequence, or sent out-of-band, or incorporated into a framing header.

---

### Phase 1 Full Block Diagram

```
Zynq-7020 PL:
┌──────────────────────────────────────────────────────┐
│                                                      │
│  ┌─────────────┐    ┌──────────────┐                 │
│  │  PS DMA     │◄──►│ AES-256-GCM  │                 │
│  │  (existing) │    │ encrypt/dec  │                 │
│  └──────┬──────┘    └──────────────┘                 │
│         │                                            │
│  ┌──────▼──────┐                                     │
│  │  TX buffer  │                                     │
│  │  / RX buffer│                                     │
│  └──────┬──────┘                                     │
│         │                                            │
│  ┌──────▼────────────────────────────────────┐       │
│  │  nRF24L01 SPI Controller                  │       │
│  │  - SPI master (10 MHz)                    │       │
│  │  - CE control                             │       │
│  │  - IRQ handler                            │       │
│  └──────┬────────────────────────────────────┘       │
│         │                                            │
│  ┌──────▼──────────────────┐                         │
│  │  FHSS Hop Controller    │                         │
│  │  - dwell timer          │                         │
│  │  - hop table (from PS)  │                         │
│  │  - writes RF_CH via SPI │                         │
│  └─────────────────────────┘                         │
│                                                      │
└──────────────────────────┬───────────────────────────┘
                           │ SPI + CE + IRQ (PMOD header)
                    ┌──────┴──────┐
                    │ nRF24L01+   │
                    │ PA+LNA      │
                    │ module      │
                    └──────┬──────┘
                           │ RF (SMA or u.FL)
                      [Directional antenna]
```

---

### Phase 1 Resource Estimate

The nRF SPI controller and hop controller are tiny. Resources used by Phase 1 additions:

| Block | Estimated LUTs | Notes |
|-------|---------------|-------|
| SPI master | ~200 | Standard shift-register design |
| FHSS hop controller | ~300 | Counter + address generator |
| TX/RX packet buffers | 2–4 BRAM | 32-byte depth, trivial |
| **Total additions** | **~500 LUTs** | On top of existing AES-GCM |

The existing AES-GCM core uses 26,657 LUTs. Total Phase 1 design will be under 30,000 LUTs on the 7020's 53,200 available — plenty of headroom.

---

### Phase 1 Limitations (Honest)

- Maximum 2 Mbps data rate, but nRF24L01+ has 32-byte packet size limit. With protocol overhead and hop guard time, effective throughput is roughly 50–200 kbps in practice.
- Modulation scheme (GFSK) is fixed. You cannot implement custom waveforms.
- No raw I/Q access. You cannot do signal analysis or implement advanced receiver algorithms.
- These are acceptable limitations for Phase 1 proof-of-concept.

---

## Phase 2

### LMS6002D — True SDR, Full Baseband in PL

**Goal:** Remove all protocol-chip limitations. The FPGA generates and receives the actual RF waveform. Custom modulation, custom coding, full DSP pipeline in PL.

---

### The Chip: LMS6002D

The LMS6002D is a fully integrated RF transceiver made by Lime Microsystems. It is the chip used in the original HackRF One. The hardware design is open-source.

Key parameters:
- Frequency range: 300 MHz – 3.8 GHz (covers 2.4 GHz)
- Integrated ADC and DAC: 12-bit resolution
- Maximum sample rate: 28 MSPS (I and Q each)
- Channel bandwidth: programmable, up to 28 MHz
- TX output power: approximately 0 dBm (requires external PA)
- RX noise figure: approximately 3.5 dB
- Interface to FPGA: parallel LVDS, 12-bit I and 12-bit Q each direction
- Configuration: SPI
- TDD and FDD: both supported
- Reference design: HackRF One schematic (fully open-source, KiCad)
- Price: approximately $15–20 on Digi-Key or Mouser

The LMS6002D successor is the LMS7002M (used in LimeSDR), which is more capable but also more expensive (~$60). For Phase 2 initial work, the LMS6002D is the right starting point.

---

### Additional Analog Components for Phase 2

Because the LMS6002D outputs only ~0 dBm, you need an analog signal chain around it. For a 2.4 GHz km-range system:

**TX chain:**
| Component | Purpose | Example parts |
|-----------|---------|--------------|
| LMS6002D | I/Q DAC, upconversion to 2.4 GHz | — |
| 2.4 GHz band-pass filter | Suppress harmonics and spurious before PA | TDK or Murata SMD filters |
| Power amplifier (PA) | Amplify from 0 dBm to +30 dBm (1 W) | SKY65111-11, RFX2401C module |
| Low-pass filter after PA | Suppress PA harmonics | Required for regulatory compliance |
| Balun (if antenna is unbalanced) | LMS6002D TX output is differential | Johanson or Murata 2.4 GHz balun |

**RX chain:**
| Component | Purpose | Example parts |
|-----------|---------|--------------|
| Band-pass filter | Reject out-of-band before LNA | Same as TX |
| Low-noise amplifier (LNA) | Boost signal before ADC, critical for sensitivity | SPF5189Z, PGA-103+ |
| LMS6002D | Downconversion from 2.4 GHz, 12-bit ADC | — |

**Common to both:**
| Component | Purpose | Notes |
|-----------|---------|-------|
| TCXO or VCTCXO | Clean reference clock for LMS6002D | Cheap crystal oscillators degrade phase noise significantly. Use a 26 or 40 MHz TCXO. |
| RF switch | Switch antenna between TX/RX in TDD mode | SKY13414 or similar SPDT |
| SMA connectors | RF port connections | Use proper 50 Ω footprints |

**Total Phase 2 analog BOM cost estimate:** $40–60 for all RF components (using COTS modules for PA/LNA).

---

### FPGA Interface to LMS6002D

The LMS6002D connects to the FPGA via a parallel LVDS interface:
- 12-bit I data, 12-bit Q data, clock — TX direction (26 signal lines)
- 12-bit I data, 12-bit Q data, clock — RX direction (26 signal lines)
- SPI (4 wires) for configuration

The FPGA drives the TX I/Q samples directly. The ADC samples arrive at the FPGA at the configured sample rate (up to 28 MSPS).

This interface uses LVDS differential pairs, which requires proper IO bank configuration on the Zynq-7020. The HP (high-performance) IO banks support LVDS natively.

The HackRF One open-source hardware provides the full schematic and PCB layout for exactly this interface. Using it as a reference eliminates most of the RF PCB design uncertainty.

---

### Phase 2 PL DSP Pipeline

Once you have I/Q samples flowing, the PL must implement the complete baseband. Each block below is a separate RTL module:

#### TX path in PL:

```
[Plaintext data from PS/DMA]
        │
[AES-256-GCM encrypt]            ← existing core
        │
[FEC encoder]                    ← convolutional, rate 1/2
        │
[Scrambler]                      ← PN sequence to whiten spectrum
        │
[Packet framer]                  ← preamble + sync word + header + CRC
        │
[Symbol mapper]                  ← e.g. QPSK: 2 bits → (I, Q) symbol
        │
[Root raised cosine filter]      ← pulse shaping, limits bandwidth
        │
[Interpolation FIR (DUC)]        ← upsample to TX sample rate
        │
[Numeric controlled oscillator]  ← mix to IF if needed
        │
[12-bit I/Q → LMS6002D DAC]      ← LVDS parallel output
```

#### RX path in PL:

```
[12-bit I/Q ← LMS6002D ADC]      ← LVDS parallel input
        │
[Automatic gain control]         ← adjust LMS6002D RX gain via SPI
        │
[Decimation FIR (DDC)]           ← downsample to symbol rate
        │
[Carrier frequency offset (CFO) correction]  ← Costas loop or correlation
        │
[Timing recovery]                ← Gardner loop or Mueller-Müller
        │
[Matched filter]                 ← root raised cosine, receiver side
        │
[Symbol decision + demapper]     ← (I, Q) → bits
        │
[Packet deframer]                ← detect preamble, extract payload
        │
[FEC decoder]                    ← Viterbi decoder
        │
[Descrambler]
        │
[AES-256-GCM decrypt]            ← existing core
        │
[Plaintext data to PS/DMA]
```

#### FHSS in Phase 2:

Same principle as Phase 1 — SPI master writes the LO frequency register of the LMS6002D on a timed schedule. LMS6002D frequency change latency is approximately 1 ms, setting the minimum hop dwell time to ~2–3 ms to allow settling.

---

### Phase 2 Resource Estimate

| Block | Estimated LUTs | DSP48 slices | BRAM |
|-------|---------------|-------------|------|
| AES-256-GCM (existing) | 26,657 | 0 | 17 |
| LMS6002D LVDS interface | ~500 | 0 | 0 |
| DDC/DUC FIR filters | ~2,000 | 40–60 | 4 |
| Modulator (QPSK) | ~500 | 4 | 0 |
| Demodulator + timing recovery | ~3,000 | 10–20 | 4 |
| FEC (convolutional + Viterbi) | ~2,500 | 0 | 8 |
| Packet framer/deframer | ~800 | 0 | 2 |
| FHSS controller + SPI | ~600 | 0 | 2 |
| **Total estimate** | **~36,600** | **50–80** | **37** |

The Zynq-7020 has 53,200 LUTs, 220 DSP48, and 140 BRAMs. Phase 2 is tight on LUTs but fits. DSP48 usage increases significantly — these slices are idle in the current AES-GCM design, so this is good news. If LUTs become too tight, FEC complexity can be reduced (shorter constraint length) or DDC/DUC filter taps can be shortened.

---

## Link Budget and Range

This section helps you make informed trade-offs between range, bandwidth, and hardware choices.

### Example: 5 km link at 2.4 GHz

| Parameter | Value |
|-----------|-------|
| TX power (PA module) | +30 dBm (1 W) |
| TX antenna gain (12 dBi Yagi) | +12 dBi |
| Free-space path loss @ 5 km, 2.4 GHz | −128 dB |
| RX antenna gain (12 dBi Yagi) | +12 dBi |
| Received power | −74 dBm |
| Receiver noise figure (LNA + chip) | 3 dB |
| Noise floor at 10 MHz bandwidth | −104 dBm |
| Required Eb/N0 for QPSK + FEC | ~6 dB |
| Sensitivity | −98 dBm |
| **Link margin** | **+24 dB** |

24 dB of margin is comfortable. This means the link can tolerate 24 dB of additional loss (rain, partial obstructions, antenna misalignment) before breaking.

For the nRF24L01+PA in Phase 1 at 250 kbps (narrower bandwidth → lower noise floor → better sensitivity):
- Sensitivity at 250 kbps: −95 dBm
- Same link budget with −95 dBm sensitivity: margin ≈ +21 dB — still very workable.

### Throughput vs Range Trade-off

| Bandwidth | Noise floor | Throughput | Impact on range |
|-----------|------------|-----------|----------------|
| 1 MHz | −114 dBm | ~500 kbps (QPSK + FEC) | Best range |
| 5 MHz | −107 dBm | ~2.5 Mbps | −7 dB margin penalty |
| 10 MHz | −104 dBm | ~5 Mbps | −10 dB margin penalty |
| 20 MHz | −101 dBm | ~10 Mbps | −13 dB margin penalty |

For multi-km range, 1–5 MHz channel bandwidth is the right operating point. 10+ MHz bandwidth trades range for throughput.

---

## FHSS Synchronisation

This is the hardest problem in the system and must be solved before FHSS is useful.

### The Problem

Both radios derive the hop sequence from the same algorithm and the same session key. The sequence is pseudorandom but deterministic — given the same seed and the same position in the sequence, both radios get the same channel. The problem is knowing **when** to advance to the next channel.

Both radios must be at the same position in the sequence at the same time.

### Option A: GPS 1PPS Reference (Best for long-range, high-reliability)

Both radios have a GPS module. The GPS outputs a 1 pulse-per-second (1PPS) signal accurate to within ~100 ns across the globe. Both radios align their hop schedule epoch to a GPS second. Since both have the same session key, they derive identical schedules and hop together.

- **Pros:** Very robust, no over-the-air sync needed, works even after long communication gaps.
- **Cons:** Both units need GPS receiver (~$10–20 each), GPS must acquire lock before operation.
- **Recommended for:** Fixed installations, field deployments where GPS lock is predictable.

### Option B: Over-the-Air Synchronisation Burst

One radio is the "master". It periodically broadcasts a sync frame on a known fixed channel (not part of the hop sequence). The other radio listens on that channel, receives the sync frame, extracts the epoch counter from it, and begins hopping in sync.

```
Master TX:  [sync burst on fixed ch] → [hop 1] → [hop 2] → [hop 3] ...
Slave RX:   [listen fixed ch]         → [lock]  → [hop 1] → [hop 2] → ...
```

- **Pros:** No GPS needed, works anywhere.
- **Cons:** The sync channel is predictable to an adversary. Brief vulnerability window at sync time.
- **Mitigation:** Encrypt the sync frame content using AES-GCM. The sync channel number can also be derived from the session key.

### Option C: Agreed Epoch + RTC

Both radios have a real-time clock (RTC) chip or use the Zynq PS clock. At session key exchange time, both sides set their clocks to the same epoch. Hop sequence position is `floor(current_time / dwell_time)`. 

RTC drift is the problem — cheap RTCs drift by ~10 ppm. Over 1 hour that is 36 ms of drift. With a 5 ms dwell time, you drift by 7 dwell positions per hour without correction. Periodic re-sync is required.

### Implementation Note

Whatever synchronisation method is chosen, the session key derivation should look like:

```
session_key = KDF(master_key, session_nonce)
hop_seed    = KDF(session_key, "FHSS_SEED")
hop_sequence[i] = PRNG(hop_seed, i) mod num_channels
```

Where KDF is a key derivation function (e.g. HKDF-SHA256). This ensures:
- The hop sequence is cryptographically unpredictable to an observer without the master key.
- Compromising the hop sequence does not directly reveal the encryption key.
- Each session has a unique sequence.

---

## Full Duplex

Phase 1 is simplex (one direction at a time). Full duplex is the Phase 2 goal.

### TDD (Time-Division Duplex) — Recommended for Phase 2

TX and RX alternate on the same frequency. At 2.4 GHz with fast switching, a 10 ms frame split 50/50 gives 5 ms each direction. At 5 Mbps, that is 25 kbytes per direction per frame — effectively 2.5 MB/s bidirectional.

The LMS6002D supports TDD natively. The FPGA gates the TX/RX datapath according to the frame timing. An RF switch (e.g. SKY13414) on the board connects the antenna to either PA or LNA depending on the frame slot.

### FDD (Frequency-Division Duplex) — Higher Performance

TX and RX operate simultaneously on different frequencies (e.g. 2.410 GHz TX, 2.450 GHz RX). A duplexer filter (e.g. Johanson 2450BL14B0100E) provides the necessary isolation between the two paths.

- **Pros:** True simultaneous TX/RX, no frame overhead.
- **Cons:** Needs two frequency allocations, duplexer adds cost and complexity, requires careful isolation between TX and RX paths on the PCB.

---

## Bitstream Strategy

Two bitstreams, field-swappable via PYNQ overlay load:

### `tx_aes_sdr.bit`
Contains:
- AES-256-GCM encrypt path
- TX baseband pipeline (Phase 2) or nRF SPI controller (Phase 1)
- FHSS hop controller
- DMA for plaintext input

### `rx_aes_sdr.bit`
Contains:
- AES-256-GCM decrypt path
- RX baseband pipeline (Phase 2) or nRF SPI controller (Phase 1)
- FHSS hop controller (same schedule, same timing)
- DMA for plaintext output

### Combined TDD bitstream (Phase 2 option)
A single bitstream with both TX and RX paths. The FHSS controller gates each path according to the TDD frame slot. Lower operational complexity in the field.

### Overlay Loading (PYNQ)
```python
from pynq import Overlay

# Switch unit to RX mode
ol = Overlay("rx_aes_sdr.bit")

# Or switch to TX mode
ol = Overlay("tx_aes_sdr.bit")
```

This works with the existing PYNQ infrastructure already in place.

---

## Recommended Build Order

### Phase 0 Steps

1. **Acquire hardware:** 2× ESP32-DevKitC, 2× nRF24L01+PA+LNA modules, 2× 3.3 V regulators (AMS1117), jumper wires
2. **Wire nRF to ESP-32:** VSPI pins, CE, IRQ as per the wiring table above. Power nRF module from dedicated regulator.
3. **Flash nRF24L01+ driver:** Use an existing ESP-IDF or Arduino nRF24 library. Test basic TX/RX at fixed channel, no FHSS yet.
4. **Measure baseline throughput:** 2 Mbps, 32-byte packets, ACK disabled. Log packets per second over UART. Target: >700 kbps.
5. **Add FHSS:** Implement hop table generator (AES-CTR derived) and hardware timer callback. Both nodes must use the same pre-shared key at this stage.
6. **Add AES-256-GCM:** Encrypt payload on TX using mbedTLS GCM. Decrypt and verify tag on RX. Confirm interoperability.
7. **Add Yagi antennas and do range test:** Start at 100 m, verify PER < 1%. Increase to maximum achievable range.
8. **Lock the protocol:** Finalise packet format, sequence numbering, and hop schedule format. This becomes the interoperability spec for Phase 1.

### Phase 1 Steps

9. **Wire nRF module to PYNQ-Z2 PMOD:** MOSI, MISO, SCK, CSN, CE, IRQ — same pinout as ESP-32 but on PMOD header.
10. **Implement nRF SPI controller in PL:** Configure chip, test loopback (PYNQ-Z2 TX → ESP-32 RX using Phase 0 firmware).
11. **Implement FHSS hop controller in PL:** SPI write to RF_CH register on a timer, same dwell timing and hop table as Phase 0.
12. **Implement packet framer in PL:** Same packet format as Phase 0 — now the PYNQ-Z2 and ESP-32 are directly interoperable.
13. **Integrate AES-256-GCM in PL:** Hook the existing FPGA core into the TX/RX path. Verify tag authentication end-to-end with the ESP-32 as the other node.
14. **Implement synchronisation protocol:** Start with option B (over-the-air sync burst on fixed channel).
15. **Range test:** Both Yagi antennas, PYNQ-Z2 on one end, ESP-32 on the other. Measure throughput and PER at increasing distances.

### Phase 2 Steps

9. **Acquire LMS6002D evaluation board or design breakout:** Start with an existing board (e.g. HackRF clone boards) before doing a custom PCB
10. **Bring up LMS6002D interface:** LVDS I/Q interface in PL, SPI configuration. Verify I/Q samples arrive/depart correctly (loopback test in chip)
11. **Implement DDC:** Decimation FIR filter from 28 MSPS to ~2 MSPS baseband. Verify in simulation first.
12. **Implement DUC:** Interpolation FIR filter for TX. Test full TX loopback (DAC → ADC through a cable attenuator)
13. **Implement QPSK modulator:** Symbol mapper + pulse shaping filter. Verify constellation on the DAC output.
14. **Implement QPSK demodulator:** Timing recovery + symbol decision. This is the most time-consuming step.
15. **Add Viterbi FEC decoder**
16. **Re-integrate AES-256-GCM:** Already done, just plug into new pipeline
17. **End-to-end test:** Full encrypted QPSK link over the air at short range first
18. **Add PA/LNA and range test**

---

## Bill of Materials

### Phase 0 — ESP-32 Proof of Concept

| Item | Quantity | Unit Price | Total |
|------|----------|-----------|-------|
| ESP32-DevKitC board | 2 | $5 | $10 |
| nRF24L01+PA+LNA module | 2 | $3 | $6 |
| AMS1117-3.3 regulator module | 2 | $1 | $2 |
| Jumper wires (assorted) | 1 pack | $3 | $3 |
| USB power bank (field power for one node) | 1 | $10 | $10 |
| **Phase 0 total** | | | **~$31** |

At the end of Phase 0 you have two fully working encrypted FHSS radio nodes. Both ESP-32 nodes remain operational — one becomes the permanent second node for all later phases.

### Phase 1 — PYNQ-Z2 FPGA Node (additions over Phase 0)

| Item | Quantity | Unit Price | Total |
|------|----------|-----------|-------|
| nRF24L01+PA+LNA module (one for PYNQ-Z2) | 1 | $3 | $3 |
| 2.4 GHz Yagi antenna (12 dBi) | 2 | $15 | $30 |
| SMA cable, 30 cm | 2 | $3 | $6 |
| SMA to u.FL pigtail (if module uses u.FL) | 2 | $2 | $4 |
| Male-to-female jumper wires (PMOD) | 1 pack | $3 | $3 |
| **Phase 1 total (additions)** | | | **~$46** |

One PYNQ-Z2 is assumed to be on hand. The second node uses the ESP-32 from Phase 0 — no second FPGA board required.

### Phase 2 — SDR Hardware

| Item | Quantity | Unit Price | Total |
|------|----------|-----------|-------|
| LMS6002D chip or evaluation board | 2 | $20 | $40 |
| 2.4 GHz PA module (e.g. RFX2401C) | 2 | $8 | $16 |
| LNA module (e.g. SPF5189Z breakout) | 2 | $5 | $10 |
| 2.4 GHz BPF (SMD, PCB) | 4 | $2 | $8 |
| TCXO 26 MHz | 2 | $3 | $6 |
| RF switch module | 2 | $3 | $6 |
| PCB fabrication (if custom) | 2 | $20 | $40 |
| 2.4 GHz Yagi antenna (12 dBi) | 2 | $15 | $30 |
| Misc SMA connectors, cables | — | — | $20 |
| **Phase 2 total** | | | **~$175** |

Phase 2 total can be reduced significantly by reusing Phase 1 antennas and starting with an off-the-shelf LMS6002D-based board (e.g. a low-cost HackRF clone) as a development platform before committing to custom PCB design.

---

## Reference Designs and Resources

### RF Transceiver

- **nRF24L01+ datasheet:** https://www.nordicsemi.com/Products/nRF24L01
- **nRF24L01+ register map:** Section 9 of datasheet — 40 registers, SPI read/write
- **LMS6002D datasheet and programming guide:** https://limemicro.com/technology/lms6002d/
- **HackRF One open-source hardware (uses LMS6002D):** https://github.com/greatscottgadgets/hackrf
- **LimeSDR (uses LMS7002M, successor to LMS6002D):** https://limemicro.com/boards/limesdr/

### Analog Devices ADI HDL (future reference if upgrading to AD9361)

- ADI open-source HDL for Zynq: https://github.com/analogdevicesinc/hdl
- Includes complete AD9361 IP core with documented AXI interface

### FPGA DSP Algorithms

- **Synchronisation algorithms (Costas, Gardner, Mueller-Müller):** Michael Rice, "Digital Communications: A Discrete-Time Approach" — this book covers all standard timing/carrier recovery implementations
- **GNU Radio signal processing blocks (as algorithm reference):** https://github.com/gnuradio/gnuradio — the C++ implementation of each block is readable and maps directly to RTL

### FHSS and Spread Spectrum

- **Proakis, "Digital Communications"** — Chapter 13 covers spread spectrum in depth
- IEEE 802.15.4 standard uses DSSS at 2.4 GHz and is a readable reference for frame structure and synchronisation

---

*This document was generated from architecture analysis of the AES-256-GCM Zynq-7020 core project as a starting point for the encrypted SDR development. The AES-GCM encryption core is fully implemented, verified at 761 MiB/s throughput, and ready to integrate into either phase of this SDR system.*
