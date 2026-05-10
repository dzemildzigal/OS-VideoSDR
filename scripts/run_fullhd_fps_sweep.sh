#!/usr/bin/env bash

set -u

# One-shot sweep for full-HD raw synthetic frames.
# Purpose: find sustainable FPS envelope for current runtime configuration.

ROOT_DIR="${ROOT_DIR:-/home/xilinx/jupyter_notebooks/OS-VideoSDR}"
cd "$ROOT_DIR"

KEY_HEX="${KEY_HEX:-000102030405060708090A0B0C0D0E0F000102030405060708090A0B0C0D0E0F}"
FPS_LIST="${FPS_LIST:-1 2 3 5}"
FRAMES="${FRAMES:-30}"
FRAME_BYTES="${FRAME_BYTES:-6220800}"
SEGMENT_BYTES="${SEGMENT_BYTES:-1200}"
INTER_PACKET_GAP_US="${INTER_PACKET_GAP_US:-0}"
RX_BUFFER_BYTES="${RX_BUFFER_BYTES:-67108864}"
TX_BUFFER_BYTES="${TX_BUFFER_BYTES:-67108864}"
LISTEN_IP="${LISTEN_IP:-127.0.0.1}"
TARGET_IP="${TARGET_IP:-127.0.0.1}"
PORT="${PORT:-5000}"

# Current proven best setting from prior sweeps for this stack.
CRYPTO_GRANULARITY="${CRYPTO_GRANULARITY:-frame}"
CRYPTO_CHUNK_BYTES="${CRYPTO_CHUNK_BYTES:-96000}"
TX_CRYPTO_MODE="${TX_CRYPTO_MODE:-dma}"
RX_CRYPTO_MODE="${RX_CRYPTO_MODE:-aesgcm}"

RUN_TS="$(date +%Y%m%d_%H%M%S)"
RUN_DIR="artifacts/logs/${RUN_TS}_fullhd_fps_sweep"
mkdir -p "$RUN_DIR"

udp_counter() {
  local pattern="$1"
  local value
  value=$(netstat -su | sed -n "s/^[[:space:]]*\([0-9][0-9]*\)[[:space:]]\+${pattern}$/\1/p" | head -n1)
  if [ -z "$value" ]; then
    echo 0
  else
    echo "$value"
  fi
}

extract_field() {
  local line="$1"
  local name="$2"
  echo "$line" | sed -n "s/.*${name}=\([0-9][0-9]*\).*/\1/p"
}

run_case() {
  local fps="$1"
  local label="fps_${fps}"
  local expected_s
  local rx_runtime_s
  local rx_idle_s
  local before_err
  local before_buf
  local after_err
  local after_buf
  local delta_err
  local delta_buf
  local tx_rc
  local rx_rc
  local tx_done
  local rx_done
  local rx_frames
  local tx_frames
  local status

  expected_s=$(( (FRAMES + fps - 1) / fps ))
  rx_runtime_s=$(( expected_s + 120 ))
  rx_idle_s=$(( expected_s + 30 ))

  pkill -f "pynq/runtime/rx_main.py" >/dev/null 2>&1 || true

  before_err="$(udp_counter "packet receive errors")"
  before_buf="$(udp_counter "receive buffer errors")"

  {
    echo "=== CASE ${label} ==="
    echo "config: fps=${fps} frames=${FRAMES} frame_bytes=${FRAME_BYTES} segment_bytes=${SEGMENT_BYTES} ipg_us=${INTER_PACKET_GAP_US}"
    echo "crypto: tx_mode=${TX_CRYPTO_MODE} rx_mode=${RX_CRYPTO_MODE} granularity=${CRYPTO_GRANULARITY} chunk_bytes=${CRYPTO_CHUNK_BYTES}"
    echo "udp_before: packet_receive_errors=${before_err} receive_buffer_errors=${before_buf}"
  } | tee -a "$RUN_DIR/summary.txt"

  timeout "${rx_runtime_s}s" python pynq/runtime/rx_main.py \
    --bind-ip "$LISTEN_IP" \
    --listen-port "$PORT" \
    --max-frames "$FRAMES" \
    --max-runtime-s "$rx_runtime_s" \
    --max-idle-s "$rx_idle_s" \
    --crypto-mode "$RX_CRYPTO_MODE" \
    --crypto-granularity "$CRYPTO_GRANULARITY" \
    --crypto-chunk-bytes "$CRYPTO_CHUNK_BYTES" \
    --key-hex "$KEY_HEX" \
    --recv-buffer-bytes "$RX_BUFFER_BYTES" \
    > "$RUN_DIR/rx_${label}.txt" 2>&1 &
  RX_PID=$!

  sleep 2

  python pynq/runtime/tx_main.py \
    --target-ip "$TARGET_IP" \
    --target-port "$PORT" \
    --frames "$FRAMES" \
    --fps "$fps" \
    --synthetic-frame-bytes "$FRAME_BYTES" \
    --segment-bytes "$SEGMENT_BYTES" \
    --inter-packet-gap-us "$INTER_PACKET_GAP_US" \
    --crypto-mode "$TX_CRYPTO_MODE" \
    --crypto-granularity "$CRYPTO_GRANULARITY" \
    --crypto-chunk-bytes "$CRYPTO_CHUNK_BYTES" \
    --key-hex "$KEY_HEX" \
    --send-buffer-bytes "$TX_BUFFER_BYTES" \
    > "$RUN_DIR/tx_${label}.txt" 2>&1
  tx_rc=$?

  wait "$RX_PID"
  rx_rc=$?

  after_err="$(udp_counter "packet receive errors")"
  after_buf="$(udp_counter "receive buffer errors")"
  delta_err=$((after_err - before_err))
  delta_buf=$((after_buf - before_buf))

  tx_done="$(grep -E "TX done:" "$RUN_DIR/tx_${label}.txt" | tail -n1)"
  rx_done="$(grep -E "RX done:" "$RUN_DIR/rx_${label}.txt" | tail -n1)"

  tx_frames="$(extract_field "$tx_done" "frames")"
  rx_frames="$(extract_field "$rx_done" "frames")"

  status="PASS"
  if [ "$tx_rc" -ne 0 ]; then
    status="FAIL_TX_RC"
  elif [ -z "$rx_frames" ] || [ "$rx_frames" -lt "$FRAMES" ]; then
    status="FAIL_RX_FRAMES"
  elif [ "$delta_err" -gt 0 ] || [ "$delta_buf" -gt 0 ]; then
    status="FAIL_UDP_DROPS"
  fi

  {
    echo "tx_done: ${tx_done}"
    grep -E "TX dma done:" "$RUN_DIR/tx_${label}.txt" | tail -n1 || true
    echo "rx_done: ${rx_done}"
    echo "udp_after: packet_receive_errors=${after_err} receive_buffer_errors=${after_buf}"
    echo "udp_delta: packet_receive_errors=${delta_err} receive_buffer_errors=${delta_buf}"
    echo "exit_codes: tx=${tx_rc} rx_wait=${rx_rc}"
    echo "status: ${status}"
    echo
  } | tee -a "$RUN_DIR/summary.txt"
}

echo "Run directory: $RUN_DIR"
echo "FPS list: $FPS_LIST"
echo "Frame bytes: $FRAME_BYTES"
echo "Granularity: $CRYPTO_GRANULARITY"

for fps in $FPS_LIST; do
  run_case "$fps"
done

echo "=== FINAL SUMMARY ==="
grep -E "^=== CASE|^status:|^tx_done:|^rx_done:|^udp_delta:" "$RUN_DIR/summary.txt"
echo "Detailed logs: $RUN_DIR"
