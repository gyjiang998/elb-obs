#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
WEB_ROOT="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
OPENHARMONY_ROOT="${OPENHARMONY_ROOT:-${HETERO_OPENHARMONY_SDK_ROOT:-}}"
LLVM_BIN="${LLVM_BIN:-${HETERO_LLVM_BIN_ROOT:-}}"
GLSLANG="${GLSLANG:-${WEB_ROOT}/.local-artifacts/glslang/bin/glslang}"
TOOLCHAIN_HEADERS="${TOOLCHAIN_HEADERS:-${WEB_ROOT}/.local-artifacts/toolchain-headers/aarch64-ohos-musl-v1/include}"
DEVICE_LIB_DIR="${DEVICE_LIB_DIR:-${WEB_ROOT}/.local-artifacts/device-gpu-libs}"
OUT_DIR="${OUT_DIR:-${WEB_ROOT}/out/device-smoke-tests}"

CLANG="${LLVM_BIN}/clang"
LLD="${LLVM_BIN}/ld.lld"
READELF="${LLVM_BIN}/llvm-readelf"
SOURCE="${SCRIPT_DIR}/gpu_rgba_brightness_vulkan_smoke.c"
SHADER="${SCRIPT_DIR}/brightness.comp"
SPV="${OUT_DIR}/brightness.spv"
SPV_HEADER="${OUT_DIR}/brightness_spv.h"
OBJECT="${OUT_DIR}/gpu_rgba_brightness_vulkan_smoke.o"
BINARY="${OUT_DIR}/gpu_rgba_brightness_vulkan_smoke"

if [[ ! -x "${CLANG}" || ! -x "${LLD}" || ! -x "${READELF}" ]]; then
  echo "LLVM toolchain not found. Set LLVM_BIN to a directory containing clang, ld.lld, and llvm-readelf." >&2
  exit 2
fi
if [[ ! -x "${GLSLANG}" ]]; then
  echo "glslang not found: ${GLSLANG}. Download a release or set GLSLANG." >&2
  exit 2
fi
if [[ ! -f "${DEVICE_LIB_DIR}/libvulkan.so" || ! -f "${DEVICE_LIB_DIR}/libc.so" ]]; then
  echo "Device vulkan/libc libraries not found in ${DEVICE_LIB_DIR}. Run the deploy script first." >&2
  exit 2
fi
if [[ ! -d "${TOOLCHAIN_HEADERS}" ]]; then
  echo "Toolchain headers not found: ${TOOLCHAIN_HEADERS}" >&2
  exit 2
fi

mkdir -p "${OUT_DIR}"

# Compile GLSL compute shader to SPIR-V for Vulkan 1.0
"${GLSLANG}" -V --target-env vulkan1.0 -o "${SPV}" "${SHADER}"

# Convert SPIR-V binary to a C header with an aligned byte array
python3 - <<PY
from pathlib import Path
spv = Path("${SPV}").read_bytes()
out = Path("${SPV_HEADER}")
lines = [
    "#ifndef BRIGHTNESS_SPV_H_",
    "#define BRIGHTNESS_SPV_H_",
    "static const unsigned char kBrightnessSpv[] __attribute__((aligned(4))) = {",
]
for i in range(0, len(spv), 12):
    vals = [f"0x{b:02x}" for b in spv[i:i+12]]
    lines.append("    " + ", ".join(vals) + ",")
lines.extend([
    "};",
    f"static const unsigned int kBrightnessSpvSize = {len(spv)};",
    "#endif // BRIGHTNESS_SPV_H_",
    "",
])
out.write_text("\n".join(lines))
print(f"Generated {out} ({len(spv)} bytes SPIR-V)")
PY

"${CLANG}" --target=aarch64-linux-musl -O2 -std=c17 -fPIE -ffreestanding -fno-stack-protector \
  -fno-builtin -fno-unwind-tables -fno-asynchronous-unwind-tables \
  -isystem "${OPENHARMONY_ROOT}/interface/sdk_c/third_party/vulkan-headers" \
  -isystem "${TOOLCHAIN_HEADERS}" \
  -I "${OUT_DIR}" \
  -c "${SOURCE}" -o "${OBJECT}"
"${LLD}" -m aarch64linux -pie -z max-page-size=4096 -e _start -dynamic-linker /lib/ld-musl-aarch64.so.1 \
  -rpath /system/lib64 -rpath /vendor/lib64 -o "${BINARY}" "${OBJECT}" \
  "${DEVICE_LIB_DIR}/libvulkan.so" "${DEVICE_LIB_DIR}/libc.so"
"${READELF}" -h "${BINARY}"
"${READELF}" -d "${BINARY}"
echo "Built ${BINARY}"
