#!/usr/bin/env bash
# Build and flash the HID bridge (needs arduino-cli and the esp32 core). Usage: hardware/flash.sh [port] [--debug]
# A running bridge is a TinyUSB device, so it is first kicked into the ROM
# bootloader with a 1200-baud touch; a blank board is flashed directly.
set -euo pipefail
cd "$(dirname "$0")"
FQBN="esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=cdc,FlashSize=8M"
port() { for p in /dev/cu.usbmodem* /dev/ttyACM*; do [[ -e $p ]] && { echo "$p"; return; }; done; }
PORT=${1:-$(port)}
FLAGS=(--build-property "compiler.cpp.extra_flags="); [[ "${2:-}" == --debug ]] && FLAGS=(--build-property "compiler.cpp.extra_flags=-DDEBUG_SERIAL=1")

arduino-cli lib install "NimBLE-Arduino@2.5.1" >/dev/null
arduino-cli compile -b "$FQBN" "${FLAGS[@]}" --output-dir build hid_bridge
OLD=$PORT
stty -f "$PORT" 1200 2>/dev/null || stty -F "$PORT" 1200 2>/dev/null || true
for _ in $(seq 20); do sleep 0.5; PORT=$(port); [[ -n "$PORT" && "$PORT" != "$OLD" ]] && break; done
sleep 1  # let the ROM bootloader's serial come up
PORT=$(port)
arduino-cli upload -b "$FQBN" -p "$PORT" --input-dir build hid_bridge
