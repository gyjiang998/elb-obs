#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
LLVM_BIN="${LLVM_BIN:-${HETERO_LLVM_BIN_ROOT:-}}"
OUT_DIR="${OUT_DIR:-${WEB_ROOT}/out/device-smoke-tests}"

CLANGXX="${LLVM_BIN}/clang++"
LLD="${LLVM_BIN}/ld.lld"
READELF="${LLVM_BIN}/llvm-readelf"
SOURCE="${SCRIPT_DIR}/dsp_pcm_equalizer_smoke.cpp"
OBJECT="${OUT_DIR}/dsp_pcm_equalizer_smoke.o"
BINARY="${OUT_DIR}/dsp_pcm_equalizer_smoke"

if [[ ! -x "${CLANGXX}" || ! -x "${LLD}" || ! -x "${READELF}" ]]; then
  echo "LLVM toolchain not found. Set LLVM_BIN to a directory containing clang++, ld.lld, and llvm-readelf." >&2
  exit 2
fi

mkdir -p "${OUT_DIR}"
"${CLANGXX}" --target=aarch64-linux-gnu -O2 -std=c++20 -ffreestanding -fno-exceptions -fno-rtti \
  -fno-stack-protector -fno-unwind-tables -fno-asynchronous-unwind-tables -c "${SOURCE}" -o "${OBJECT}"
"${LLD}" -m aarch64linux -static -e _start -o "${BINARY}" "${OBJECT}"
"${READELF}" -h "${BINARY}"
echo "Built ${BINARY}"
