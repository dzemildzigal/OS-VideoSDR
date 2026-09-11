#!/bin/bash
# Compare AXI attribute combinations by sender rate. The attribute register is
# live, so the stream never stops and no restart (and no TKEEP fault) happens.
# Run as root: sudo bash /tmp/attr_rate_sweep.sh
set -u
SHIM_LOG=${SHIM_LOG:-/home/xilinx/tx_run.log}
SECS=${SECS:-12}

COMBOS=(
  "0xF 0x1F"
  "0xF 0x01"
  "0xF 0x03"
  "0xF 0x0F"
  "0xB 0x1F"
  "0x3 0x01"
)

for combo in "${COMBOS[@]}"; do
    set -- $combo
    cache=$1; user=$2
    /usr/local/share/pynq-venv/bin/python3 /tmp/set_attr.py "$cache" "$user" >/dev/null
    sleep 3
    echo "===== AXCACHE=$cache AXUSER=$user ====="
    first=$(grep -c 'pkts/s' "$SHIM_LOG")
    sleep "$SECS"
    tail -n +"$((first + 1))" "$SHIM_LOG" | awk '
        /pkts\/s=/ {
            match($0, /pkts\/s=[0-9.]+/); r = substr($0, RSTART + 8, RLENGTH - 8);
            match($0, /drops_delta=[0-9]+/); d = substr($0, RSTART + 12, RLENGTH - 12);
            match($0, /send-us=[0-9.]+/); s = substr($0, RSTART + 8, RLENGTH - 8);
            n++; sum += r; dsum += d; ssum += s;
            if (n == 1 || r < lo) lo = r;
        }
        END { if (n) printf "  samples=%d mean_rate=%.0f min_rate=%.0f mean_drops_delta=%.0f send-us=%.0f\n", n, sum/n, lo, dsum/n, ssum/n }
    '
done
echo "===== sweep done (attributes left at the last combination) ====="
