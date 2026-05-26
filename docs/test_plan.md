# Test Plan

## Gate Order

1. Crypto baseline gate.
2. U10 wired TX gate.
3. U10 wired RX gate.
4. U15 wired TX and RX gates.
5. Latency gate (p95 < 50 ms).
6. C60 gate after H.264.
7. AntSDR non-hopping gate.
8. FHSS gate.

## Core Test Suites

- Unit tests: packet parsing, validation, nonce handling.
- Integration tests: wired TX and wired RX interoperability.
- Soak tests: 30-minute and multi-hour continuity.
- Fault tests: packet drop, reorder, jitter stress.

## V1 Hardening Matrix Execution

Matrix definition:

- `scripts/v1_hardening_matrix.json`

Primary runner (PC side):

- `scripts/run_v1_hardening_matrix.ps1`

Required parameters:

- `-PynqHost`: SSH target for board (for example `xilinx@192.168.0.50`)
- `-PcTargetIp`: PC IP reachable by the board TX path

Nominal mandatory row subset (fast sign-off precheck):

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_v1_hardening_matrix.ps1 \
	-PynqHost "xilinx@192.168.0.50" \
	-PcTargetIp "192.168.0.36" \
	-CaseIds M1,M2,M5
```

Full matrix without soak rows:

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_v1_hardening_matrix.ps1 \
	-PynqHost "xilinx@192.168.0.50" \
	-PcTargetIp "192.168.0.36"
```

Full matrix including soak rows (M8/M9):

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_v1_hardening_matrix.ps1 \
	-PynqHost "xilinx@192.168.0.50" \
	-PcTargetIp "192.168.0.36" \
	-IncludeSoak
```

Artifacts are written to:

- `artifacts/metrics/v1_hardening/<timestamp>/`
- One subdirectory per matrix case with TX and RX logs.
- `summary.json`, `results.json`, and `results.csv` for pass/fail and metrics review.

## V2 HDMI Baseline Execution

Matrix definition:

- `scripts/v2_hdmi_baseline_matrix.json`

Primary runner (PC side):

- `scripts/run_v2_hdmi_baseline.ps1`

Required parameters:

- `-PynqHost`: SSH target for board (for example `xilinx@192.168.0.50`)
- `-PcTargetIp`: PC IP reachable by the board TX path

Baseline short-run gate (H1 only):

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_v2_hdmi_baseline.ps1 \
	-PynqHost "xilinx@192.168.0.50" \
	-PcTargetIp "192.168.0.36" \
	-CaseIds H1 \
	-SshKeyPath "$HOME/.ssh/id_ed25519" \
	-SshKeyOnly
```

Baseline plus soak gate (H1 + H2):

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_v2_hdmi_baseline.ps1 \
	-PynqHost "xilinx@192.168.0.50" \
	-PcTargetIp "192.168.0.36" \
	-CaseIds H1,H2 \
	-IncludeSoak \
	-SshKeyPath "$HOME/.ssh/id_ed25519" \
	-SshKeyOnly
```

Exploratory expansion rows (adds H3/H4):

```powershell
powershell -ExecutionPolicy Bypass -File scripts/run_v2_hdmi_baseline.ps1 \
	-PynqHost "xilinx@192.168.0.50" \
	-PcTargetIp "192.168.0.36" \
	-IncludeSoak \
	-IncludeExploratory \
	-SshKeyPath "$HOME/.ssh/id_ed25519" \
	-SshKeyOnly
```

V2 artifacts are written to:

- `artifacts/metrics/v2_hdmi_baseline/<timestamp>/`
- One subdirectory per matrix case with TX and RX logs.
- `preflight.out.log` and `preflight.err.log` from board-side HDMI preflight.
- `summary.json`, `results.json`, and `results.csv`.

## Required Evidence per Gate

- Run configuration snapshot.
- Metrics summary (loss, auth failures, latency percentiles).
- Short pass or fail report.
- Artifacts stored under artifacts/metrics and artifacts/logs.

## Failure Triage Order

1. Crypto correctness.
2. Packet continuity and reassembly.
3. Latency and queue behavior.
4. Visual quality and frame stability.
