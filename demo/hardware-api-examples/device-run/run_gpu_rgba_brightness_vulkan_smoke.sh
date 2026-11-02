#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
HDC_BIN="${HDC_BIN:-${HETERO_HDC_BIN:-}}"
HDC_TARGET="${HDC_TARGET:?Set HDC_TARGET to the connected device serial.}"
DEVICE_LIB_DIR="${DEVICE_LIB_DIR:-${WEB_ROOT}/.local-artifacts/device-gpu-libs}"
OUT_DIR="${OUT_DIR:-${WEB_ROOT}/out/device-smoke-tests}"
REMOTE_BINARY="/data/local/tmp/gpu_rgba_brightness_vulkan_smoke"
LOCAL_BINARY="${OUT_DIR}/gpu_rgba_brightness_vulkan_smoke"

if [[ ! -x "${HDC_BIN}" ]]; then
  echo "HDC executable not found: ${HDC_BIN}" >&2
  exit 2
fi

mkdir -p "${DEVICE_LIB_DIR}"
echo "Pulling device libraries..."
"${HDC_BIN}" -t "${HDC_TARGET}" file recv /system/lib64/libvulkan.so "${DEVICE_LIB_DIR}/libvulkan.so"
"${HDC_BIN}" -t "${HDC_TARGET}" file recv /system/lib64/libc.so "${DEVICE_LIB_DIR}/libc.so"

DEVICE_LIB_DIR="${DEVICE_LIB_DIR}" OUT_DIR="${OUT_DIR}" bash "${SCRIPT_DIR}/build_gpu_rgba_brightness_vulkan_smoke.sh"

# Clean up any previous run and deploy fresh binary
"${HDC_BIN}" -t "${HDC_TARGET}" shell "rm -f ${REMOTE_BINARY}" || true
"${HDC_BIN}" -t "${HDC_TARGET}" file send "${LOCAL_BINARY}" "${REMOTE_BINARY}"
echo "Running on device ${HDC_TARGET}..."
"${HDC_BIN}" -t "${HDC_TARGET}" shell "chmod 0755 ${REMOTE_BINARY} && ${REMOTE_BINARY}"
