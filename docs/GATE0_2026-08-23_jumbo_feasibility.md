# Gate 0 Report — Jumbo Feasibility (2026-08-23, night)

Question: can Option A (jumbo frames) work on this network path?

## Measurements

PC NIC (Realtek PCIe GBE):
  - Jumbo Frame property exists; set to "9KB MTU" (verified DisplayValue).
  - Interface MTU set to 9000 (verified via Get-NetIPInterface: 9000).
  - Verdict: PC PASSES.

Board (PYNQ-Z2, kernel 6.6.10-xilinx-v2024.1, CONFIG_MACB=y, compatible
xlnx,zynq-gem/cdns,gem):
  - ip link set eth0 mtu 9000  -> "mtu greater than device maximum"
  - 8000/7000/6000/4000/2024 all rejected; 1500 accepted.
  - Driver has no JUMBO caps for this compatible; macb is BUILT-IN
    (CONFIG_MACB=y), so fixing it = full kernel rebuild, not a module.
  - Verdict: board FAILS (software limit; GEM hardware itself is
    jumbo-capable per Zynq-7000 TRM).

Switch path (PC -> router 192.168.0.1, DF-set pings):
  - 1472 B (1500 wire): 0% loss            (path healthy)
  - 2000/3000/4000/6000/8972 B: 100% loss
  - The path carries nothing above 1500. Either the switch or the router
    NIC caps at 1500 (indistinguishable from the PC, but the PC->board
    leg shares this switch).

## Verdict

Option A (jumbo) is blocked by BOTH:
  1. Board kernel (macb, no jumbo caps; fix = kernel rebuild).
  2. Network path (switch or router drops everything >1500; fix = new
     hardware or direct PC<->board cable - and the kernel rebuild would
     still be required).

Only path to Option A: kernel rebuild AND direct cable (bypass switch).
That is more work than Option B and touches the boot chain.

## Decision per plan section 5

Option B becomes the PRIMARY path: PL frame-builder + DDR ring writer +
AF_PACKET TX_RING shim at normal 1500 MTU. It requires:
  - no jumbo, no switch change, no kernel change, no boot-chain change.
  - RTL/BD work (one rebuild) + new shim.

Next steps (from plan section 6, Phase B):
  B.1 RTL frame-builder (ETH/IP/UDP headers + IP checksum) behind sequencer
  B.2 RTL DDR ring writer (N slots, control block, consumed pointer)
  B.3 PS AF_PACKET TX_RING shim
  B.4 Pattern-flood gate: >= 75k pps before video attach
  B.5 Video + auth gates (bad=0, 30 fps)

Independent, same rebuild: 720p30 EDID + ST_SKIP removal.
Stale video-path evidence must be re-measured at verified 100 MHz first.
