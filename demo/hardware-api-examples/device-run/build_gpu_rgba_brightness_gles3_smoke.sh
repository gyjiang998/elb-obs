#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
OPENHARMONY_ROOT="${OPENHARMONY_ROOT:-${HETERO_OPENHARMONY_SDK_ROOT:-}}"
LLVM_BIN="${LLVM_BIN:-${HETERO_LLVM_BIN_ROOT:-}}"
DEVICE_LIB_DIR="${DEVICE_LIB_DIR:-${WEB_ROOT}/.local-artifacts/device-gpu-libs}"
OUT_DIR="${OUT_DIR:-${WEB_ROOT}/out/device-smoke-tests}"

CLANG="${LLVM_BIN}/clang"
LLD="${LLVM_BIN}/ld.lld"
READELF="${LLVM_BIN}/llvm-readelf"
SOURCE="${SCRIPT_DIR}/gpu_rgba_brightness_gles3_smoke.c"
OBJECT="${OUT_DIR}/gpu_rgba_brightness_gles3_smoke.o"
BINARY="${OUT_DIR}/gpu_rgba_brightness_gles3_smoke"

if [[ ! -x "${CLANG}" || ! -x "${LLD}" || ! -x "${READELF}" ]]; then
  echo "LLVM toolchain not found. Set LLVM_BIN to a directory containing clang, ld.lld, and llvm-readelf." >&2
  exit 2
fi
if [[ ! -f "${DEVICE_LIB_DIR}/libEGL.so" || ! -f "${DEVICE_LIB_DIR}/libGLESv3.so" || ! -f "${DEVICE_LIB_DIR}/libc.so" ]]; then
  echo "Device EGL/GLES/libc libraries not found. Pull them into ${DEVICE_LIB_DIR} before building." >&2
  exit 2
fi

mkdir -p "${OUT_DIR}"
"${CLANG}" --target=aarch64-linux-musl -O2 -std=c17 -fPIE -ffreestanding -fno-stack-protector \
  -fno-builtin -fno-unwind-tables -fno-asynchronous-unwind-tables \
  -isystem "${OPENHARMONY_ROOT}/interface/sdk_c/third_party/egl" \
  -isystem "${OPENHARMONY_ROOT}/interface/sdk_c/third_party/openGLES" \
  -c "${SOURCE}" -o "${OBJECT}"
"${LLD}" -m aarch64linux -pie -z max-page-size=4096 -e _start -dynamic-linker /lib/ld-musl-aarch64.so.1 \
  -rpath /system/lib64 -rpath /vendor/lib64 -o "${BINARY}" "${OBJECT}" \
  "${DEVICE_LIB_DIR}/libEGL.so" "${DEVICE_LIB_DIR}/libGLESv3.so" "${DEVICE_LIB_DIR}/libc.so"
"${READELF}" -h "${BINARY}"
"${READELF}" -d "${BINARY}"
echo "Built ${BINARY}"
