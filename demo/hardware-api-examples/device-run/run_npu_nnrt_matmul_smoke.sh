#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
HDC_BIN="${HDC_BIN:-${HETERO_HDC_BIN:-}}"
HDC_TARGET="${HDC_TARGET:?Set HDC_TARGET to the connected device serial.}"
DEVICE_LIB_DIR="${DEVICE_LIB_DIR:-${WEB_ROOT}/.local-artifacts/device-nnrt-libs}"
OUT_DIR="${OUT_DIR:-${WEB_ROOT}/out/device-smoke-tests}"
REMOTE_BINARY="/data/local/tmp/npu_nnrt_matmul_smoke"
LOCAL_BINARY="${OUT_DIR}/npu_nnrt_matmul_smoke"

if [[ ! -x "${HDC_BIN}" ]]; then
  echo "HDC executable not found: ${HDC_BIN}" >&2
  exit 2
fi

cleanup() {
  "${HDC_BIN}" -t "${HDC_TARGET}" shell "rm -f ${REMOTE_BINARY}" >/dev/null 2>&1 || true
}
trap cleanup EXIT

mkdir -p "${DEVICE_LIB_DIR}"
"${HDC_BIN}" -t "${HDC_TARGET}" file recv /system/lib64/ndk/libneural_network_runtime.so "${DEVICE_LIB_DIR}/libneural_network_runtime.so"
"${HDC_BIN}" -t "${HDC_TARGET}" file recv /system/lib64/ndk/libneural_network_core.so "${DEVICE_LIB_DIR}/libneural_network_core.so"
"${HDC_BIN}" -t "${HDC_TARGET}" file recv /system/lib64/libc.so "${DEVICE_LIB_DIR}/libc.so"

DEVICE_LIB_DIR="${DEVICE_LIB_DIR}" OUT_DIR="${OUT_DIR}" bash "${SCRIPT_DIR}/build_npu_nnrt_matmul_smoke.sh"
"${HDC_BIN}" -t "${HDC_TARGET}" file send "${LOCAL_BINARY}" "${REMOTE_BINARY}"
"${HDC_BIN}" -t "${HDC_TARGET}" shell "chmod 0755 ${REMOTE_BINARY} && ${REMOTE_BINARY}"
