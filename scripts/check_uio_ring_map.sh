#!/usr/bin/env bash

set -euo pipefail

usage() {
  echo "Usage: $0 <uio-dev> <map-index> <slot-count> <slot-payload-bytes> [ring-offset-bytes]"
  echo "Example: $0 /dev/uio2 1 8192 4096"
  echo "Example: $0 /dev/uio2 2 8192 4096"
}

if [ "$#" -lt 4 ] || [ "$#" -gt 5 ]; then
  usage
  exit 2
fi

UIO_DEV="$1"
MAP_INDEX="$2"
SLOT_COUNT="$3"
SLOT_PAYLOAD_BYTES="$4"
RING_OFFSET_BYTES="${5:-0}"

case "$UIO_DEV" in
  /dev/uio*)
    UIO_INDEX="${UIO_DEV#/dev/uio}"
    ;;
  uio*)
    UIO_INDEX="${UIO_DEV#uio}"
    ;;
  *)
    echo "ERROR: uio-dev must look like /dev/uioX or uioX"
    exit 2
    ;;
esac

if ! [[ "$UIO_INDEX" =~ ^[0-9]+$ ]]; then
  echo "ERROR: invalid UIO index parsed from $UIO_DEV"
  exit 2
fi

if ! [[ "$MAP_INDEX" =~ ^[0-9]+$ ]]; then
  echo "ERROR: map-index must be a non-negative integer"
  exit 2
fi

if ! [[ "$SLOT_COUNT" =~ ^[0-9]+$ ]] || [ "$SLOT_COUNT" -lt 2 ]; then
  echo "ERROR: slot-count must be an integer >= 2"
  exit 2
fi

if ! [[ "$SLOT_PAYLOAD_BYTES" =~ ^[0-9]+$ ]] || [ "$SLOT_PAYLOAD_BYTES" -lt 256 ]; then
  echo "ERROR: slot-payload-bytes must be an integer >= 256"
  exit 2
fi

if ! [[ "$RING_OFFSET_BYTES" =~ ^[0-9]+$ ]]; then
  echo "ERROR: ring-offset-bytes must be a non-negative integer"
  exit 2
fi

SYSFS_BASE="/sys/class/uio/uio${UIO_INDEX}"
MAP_PATH="${SYSFS_BASE}/maps/map${MAP_INDEX}"

if [ ! -d "$MAP_PATH" ]; then
  echo "ERROR: map path does not exist: $MAP_PATH"
  exit 1
fi

MAP_SIZE_HEX="$(cat "${MAP_PATH}/size")"
MAP_ADDR_HEX="$(cat "${MAP_PATH}/addr")"
UIO_NAME="unknown"
if [ -f "${SYSFS_BASE}/name" ]; then
  UIO_NAME="$(cat "${SYSFS_BASE}/name")"
fi

MAP_SIZE_BYTES=$((MAP_SIZE_HEX))
MAP_ADDR=$((MAP_ADDR_HEX))

# Keep these constants aligned with pynq/ps_shim/src/ring_backend.c layout.
RING_HEADER_BYTES=32
RING_SLOT_BYTES=64

REQUIRED_BYTES=$((RING_HEADER_BYTES + SLOT_COUNT * (RING_SLOT_BYTES + SLOT_PAYLOAD_BYTES)))
REQUIRED_WITH_OFFSET=$((RING_OFFSET_BYTES + REQUIRED_BYTES))

echo "UIO device        : /dev/uio${UIO_INDEX} (${UIO_NAME})"
echo "Map index         : ${MAP_INDEX}"
echo "Map address       : ${MAP_ADDR_HEX} (${MAP_ADDR})"
echo "Map size          : ${MAP_SIZE_HEX} (${MAP_SIZE_BYTES} bytes)"
echo "Ring offset       : ${RING_OFFSET_BYTES} bytes"
echo "Slot count        : ${SLOT_COUNT}"
echo "Slot payload      : ${SLOT_PAYLOAD_BYTES} bytes"
echo "Required ring     : ${REQUIRED_BYTES} bytes"
echo "Required + offset : ${REQUIRED_WITH_OFFSET} bytes"

if [ "$MAP_SIZE_BYTES" -lt "$REQUIRED_WITH_OFFSET" ]; then
  echo "RESULT            : FAIL (map too small)"
  exit 1
fi

echo "RESULT            : PASS"
