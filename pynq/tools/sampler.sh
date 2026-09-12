#!/bin/bash
# Persistent sampler for the wedge. Writes to the SD card every 2 s so the last
# state before a crash survives the power cycle.
# Run as root: sudo nohup bash /home/xilinx/sampler.sh &
LOG=/home/xilinx/sampler.log
RAW=/sys/bus/iio/devices/iio:device0/in_temp0_raw
: > "$LOG"
echo "sampler start $(date -Is) uptime=$(cut -d' ' -f1 /proc/uptime)s" >> "$LOG"

prev_s0=0; prev_s1=0; prev_irq=0
first=1
while true; do
    raw=$(cat "$RAW" 2>/dev/null)
    tc=$(awk -v r="$raw" 'BEGIN{ if (r != "") printf "%.1f", r*503.975/4096-273.15; else printf "?" }')
    read -r s0 s1 < <(awk '/^cpu0 /{a=$7} /^cpu1 /{b=$7} END{print a+0, b+0}' /proc/stat)
    irq=$(awk '/eth0/{print $2+$3}' /proc/interrupts)
    if [ "$first" = 1 ]; then
        ds0=0; ds1=0; dirq=0; first=0
    else
        ds0=$((s0 - prev_s0)); ds1=$((s1 - prev_s1)); dirq=$((irq - prev_irq))
    fi
    prev_s0=$s0; prev_s1=$s1; prev_irq=$irq
    la=$(cut -d' ' -f1-3 /proc/loadavg)
    mem=$(awk '/MemAvailable/{printf "%.0f", $2/1024}' /proc/meminfo)
    shim=$(tail -1 /home/xilinx/tx_run.log 2>/dev/null | cut -c1-78)
    printf '%s T=%sC load=%s mem=%sMB softirq/2s=%s+%s eth0irq/2s=%s | %s\n' \
        "$(date +%H:%M:%S)" "$tc" "$la" "$mem" "$ds0" "$ds1" "$dirq" "$shim" >> "$LOG"
    sleep 2
done
