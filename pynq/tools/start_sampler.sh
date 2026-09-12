#!/bin/bash
# Start the persistent sampler unless it is already running.
# Avoids the pkill self-match trap: it inspects /proc and skips its own pid.
set -u
SELF=$$
for p in /proc/[0-9]*; do
    pid=${p#/proc/}
    [ "$pid" = "$SELF" ] && continue
    cmd=$(tr '\0' ' ' < "$p/cmdline" 2>/dev/null)
    case "$cmd" in
        *"bash /home/xilinx/sampler.sh"*)
            echo "sampler already running (pid $pid)"
            tail -2 /home/xilinx/sampler.log 2>/dev/null
            exit 0
            ;;
    esac
done
nohup bash /home/xilinx/sampler.sh > /dev/null 2>&1 &
sleep 4
echo "sampler started, log lines: $(wc -l < /home/xilinx/sampler.log 2>/dev/null)"
tail -2 /home/xilinx/sampler.log 2>/dev/null
