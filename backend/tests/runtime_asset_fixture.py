"""Small filesystem fixtures for portable build-profile unit tests."""

from __future__ import annotations

from pathlib import Path


COMPILER_HEADERS = (
    "arm_neon.h",
    "float.h",
    "limits.h",
    "stdalign.h",
    "stdarg.h",
    "stdatomic.h",
    "stdbool.h",
    "stddef.h",
    "stdint.h",
)
LIBC_HEADERS = (
    "assert.h",
    "dlfcn.h",
    "errno.h",
    "math.h",
    "stdio.h",
    "stdlib.h",
    "string.h",
    "sys/time.h",
    "time.h",
    "unistd.h",
)


def create_minimal_runtime_assets(root: Path) -> tuple[Path, Path]:
    """Create header surfaces only; no executable compiler is fabricated."""

    llvm_root = root / "runtime-assets" / "llvm"
    llvm_bin = llvm_root / "bin"
    compiler_headers = llvm_root / "lib" / "clang" / "test" / "include"
    llvm_bin.mkdir(parents=True)
    compiler_headers.mkdir(parents=True)
    for header in COMPILER_HEADERS:
        (compiler_headers / header).write_text("/* test compiler header */\n", encoding="utf-8")

    sdk_root = root / "runtime-assets" / "openharmony-sdk"
    musl_root = sdk_root / "third_party" / "musl"
    (musl_root / "arch" / "aarch64" / "bits").mkdir(parents=True)
    (musl_root / "arch" / "generic").mkdir(parents=True)
    (musl_root / "include").mkdir(parents=True)
    (musl_root / "arch" / "aarch64" / "bits" / "alltypes.h.in").write_text(
        "TYPEDEF unsigned long size_t;\n",
        encoding="utf-8",
    )
    (musl_root / "include" / "alltypes.h.in").write_text(
        "TYPEDEF long ssize_t;\n",
        encoding="utf-8",
    )
    (musl_root / "arch" / "aarch64" / "bits" / "syscall.h.in").write_text(
        "#define __NR_write 64\n",
        encoding="utf-8",
    )

    libc_root = sdk_root / "interface" / "sdk_c" / "third_party" / "musl" / "ndk_musl_include"
    (libc_root / "fortify").mkdir(parents=True)
    for header in LIBC_HEADERS:
        libc_header = libc_root / header
        fortify_header = libc_root / "fortify" / header
        libc_header.parent.mkdir(parents=True, exist_ok=True)
        fortify_header.parent.mkdir(parents=True, exist_ok=True)
        libc_header.write_text("/* test libc header */\n", encoding="utf-8")
        fortify_header.write_text("/* test fortify header */\n", encoding="utf-8")

    sdk_headers = (
        "interface/sdk_c/third_party/egl/EGL/egl.h",
        "interface/sdk_c/third_party/egl/EGL/eglext.h",
        "interface/sdk_c/third_party/openGLES/GLES3/gl3.h",
        "interface/sdk_c/third_party/openGLES/GLES3/gl31.h",
        "interface/sdk_c/third_party/openGLES/GLES3/gl32.h",
        "interface/sdk_c/third_party/vulkan-headers/vulkan/vulkan.h",
        "interface/sdk_c/ai/neural_network_runtime.h",
        "interface/sdk_c/ai/neural_network_runtime/neural_network_runtime.h",
        "interface/sdk_c/graphic/graphic_2d/native_buffer/buffer_common.h",
        "interface/sdk_c/multimedia/av_codec/native_avcapability.h",
        "interface/sdk_c/multimedia/av_codec/native_avcodec_base.h",
        "interface/sdk_c/multimedia/media_foundation/native_avbuffer.h",
    )
    for relative in sdk_headers:
        path = sdk_root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("/* test SDK header */\n", encoding="utf-8")
    return llvm_bin, sdk_root
