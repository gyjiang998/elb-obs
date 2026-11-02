"""Agent-authored native experiments with a workstation-bound execution path.

The Page Agent may author a complete device-side experiment and decide what to
run after deployment.  This module deliberately keeps the *workstation*
boundary narrow: source is staged below the platform artifact root, compiler
profiles and SDK include roots are fixed, and HDC receives only a server-owned
local artifact plus a probe-bound device serial.  It never invokes a host
shell or accepts a host path, compiler flag, linker input, or HDC binary from
the model.
"""

from __future__ import annotations

import difflib
import hashlib
import os
import re
import secrets
import time
from pathlib import Path
from typing import Any

from .core import PlatformError, sha256_file
from .device_verification import (
    DEVICE_VALIDATION_TIMEOUT_SECONDS,
    _available_devices,
    _bounded,
    _clean_output,
    _run_hdc,
    _run_process,
    _safe_serial,
    resolve_hdc_binary,
)
from .platform_paths import glslang_binary, llvm_bin_root, openharmony_sdk_root, state_root as platform_state_root


MAX_EXPERIMENT_SOURCE_BYTES = 192 * 1024
MAX_EXPERIMENT_SHADER_BYTES = 128 * 1024
MAX_EXPERIMENT_TITLE_LENGTH = 160
MAX_EXPERIMENT_TIMEOUT_SECONDS = 900
GLSLANG_RELATIVE_PATH = Path("glslang/bin/glslang")
EXPERIMENT_ROOT = Path("agent-device-experiments")
LLD_THREAD_COUNT = 1
LLD_THREAD_ARGUMENT = f"--threads={LLD_THREAD_COUNT}"
MUSL_NDK_INCLUDE_RELATIVE_ROOT = Path("interface/sdk_c/third_party/musl/ndk_musl_include")
MUSL_SOURCE_RELATIVE_ROOT = Path("third_party/musl")
MUSL_HEADER_OVERLAY_RELATIVE_ROOT = Path("toolchain-headers/aarch64-ohos-musl-v1/include")

_FORBIDDEN_PREPROCESSOR = re.compile(r"^\s*#\s*(?:include_next|embed|line|pragma)\b", re.MULTILINE)
_INCLUDE_DIRECTIVE = re.compile(r"^\s*#\s*include\b([^\r\n]*)", re.MULTILINE)
_LITERAL_INCLUDE = re.compile(r'^\s*(?:<([^>\r\n]+)>|"([^"\r\n]+)")\s*(?://[^\r\n]*)?$')
_FORBIDDEN_INCLUDE_QUERY = re.compile(r"__has_include(?:_next)?\s*\(")
_HEADER_SUFFIXES = frozenset({".h", ".hh", ".hpp", ".hxx", ".inc"})
_RECOMMENDED_COMPILER_HEADERS = (
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
_RECOMMENDED_LIBC_HEADERS = (
    "assert.h",
    "errno.h",
    "math.h",
    "stdio.h",
    "stdlib.h",
    "string.h",
    "time.h",
    "unistd.h",
)
_INTERNAL_COMPILER_HEADER_PREFIXES = (
    "cuda_wrappers/",
    "llvm_libc_wrappers/",
    "openmp_wrappers/",
)


def _sdk_provisioning_details(sdk_root: Path, missing: list[Path]) -> dict[str, Any]:
    """Return one environment-level recovery path instead of inviting source retries."""

    return {
        "sdk_root": str(sdk_root),
        "missing_paths": [str(path.relative_to(sdk_root)) for path in missing],
        "configuration": "HETERO_OPENHARMONY_SDK_ROOT",
        "remediation_kind": "provision_compact_openharmony_sdk_bundle",
        "provision_command_template": (
            "./deploy/linux/heteroctl install --source-root <OPENHARMONY_SOURCE_ROOT> "
            "--runtime-root <RUNTIME_ROOT>"
        ),
        "verify_command_template": (
            "./deploy/linux/heteroctl preflight --require-device --runtime-root <RUNTIME_ROOT>"
        ),
        "next_action_zh": (
            "这是部署资产缺失，不是实验源码错误。先由平台管理员从 OpenHarmony checkout "
            "生成紧凑 SDK；在预检通过前不要通过删除 stdio.h、stdlib.h、string.h、unistd.h "
            "或手写 libc 声明来重试。"
        ),
        "retry_policy": "stop_source_rewrites_until_environment_ready",
    }


EXPERIMENT_PROFILES: dict[str, dict[str, Any]] = {
    "native_c": {
        "unit": "native",
        "language": "c",
        "compiler": "clang",
        "target": "aarch64-linux-musl",
        "standard": "c17",
        "include_dirs": (),
        "libc_headers": False,
        "libraries": (),
        "link_mode": "static",
        "summary_zh": "最小 AArch64 C 实验。源码应自带 _start 与所需运行时，适合验证纯计算、输出与设备侧环境。",
        "runtime_contract_zh": "仅提供固定 Clang resource headers；无 libc 链接。源码必须提供 _start，输出与退出需使用设备侧系统调用。",
    },
    "native_cpp": {
        "unit": "native",
        "language": "cpp",
        "compiler": "clang++",
        "target": "aarch64-linux-gnu",
        "standard": "c++20",
        "include_dirs": (),
        "libc_headers": False,
        "libraries": (),
        "link_mode": "static",
        "summary_zh": "最小 AArch64 C++ 实验。源码应避免宿主 C++ 运行时依赖并自带 _start。",
        "runtime_contract_zh": "仅提供固定 Clang resource headers；不链接 libc++、异常或 RTTI。源码必须提供 extern \"C\" _start。",
    },
    "gpu_gles3": {
        "unit": "gpu",
        "language": "c",
        "compiler": "clang",
        "target": "aarch64-linux-musl",
        "standard": "c17",
        "include_dirs": (
            "interface/sdk_c/third_party/egl",
            "interface/sdk_c/third_party/openGLES",
        ),
        "libc_headers": True,
        "libraries": (
            ("/system/lib64/libEGL.so", "libEGL.so"),
            ("/system/lib64/libGLESv3.so", "libGLESv3.so"),
            ("/system/lib64/libc.so", "libc.so"),
        ),
        "link_mode": "dynamic_gpu",
        "summary_zh": "OpenHarmony EGL/GLES3 GPU 实验。平台固定 SDK 头文件与 EGL/GLES3/libc 链接面；模型可编写完整 shader/host 实验代码。",
        "source_entrypoint": "int main(void)",
        "runtime_contract_zh": "可引用固定 OpenHarmony musl C、EGL/OpenGLES SDK 根目录中实际存在的任意头文件；源码实现 int main(void)，Build Worker 提供固定 _start 启动胶水；程序自行建立 EGL context、检查 shader 日志和输出结果。",
    },
    "gpu_vulkan": {
        "unit": "gpu",
        "language": "c",
        "compiler": "clang",
        "target": "aarch64-linux-musl",
        "standard": "c17",
        "include_dirs": ("interface/sdk_c/third_party/vulkan-headers",),
        "libc_headers": True,
        "libraries": (
            ("/system/lib64/libvulkan.so", "libvulkan.so"),
            ("/system/lib64/libc.so", "libc.so"),
        ),
        "link_mode": "dynamic_gpu",
        "generated_headers": ("experiment_shader_spv.h",),
        "shader_contract": {
            "required": True,
            "language": "glsl_compute",
            "stage": "compute",
            "target_environment": "vulkan1.0",
            "generated_header": "experiment_shader_spv.h",
            "byte_array_symbol": "kExperimentShaderSpv",
            "byte_size_symbol": "kExperimentShaderSpvSize",
        },
        "summary_zh": "OpenHarmony Vulkan compute GPU 实验。Agent 同时提交 C host 与 GLSL compute shader；Build Worker 用固定 glslang 生成 SPIR-V，再链接 libvulkan/libc。",
        "source_entrypoint": "int main(void)",
        "runtime_contract_zh": "C 源码实现 int main(void)，并引用 <vulkan/vulkan.h> 与生成头 experiment_shader_spv.h；Build Worker 提供固定 _start 启动胶水，并以 Vulkan 1.0 目标编译完整 GLSL compute shader。程序输出 physical device、queue、pipeline、dispatch、比较与退出状态。",
        "runtime_loader_guidance_zh": "E0 独立进程优先使用 dlopen(\"libvulkan.so\", RTLD_NOW) 与 dlsym 获取 Vulkan 函数；既有真机实验观察到直接调用链接符号可能在首次 Vulkan 调用处静默退出。该指引只描述独立实验运行时合同，仍须以本次设备输出复核。",
    },
    "npu_nnrt": {
        "unit": "npu",
        "language": "c",
        "compiler": "clang",
        "target": "aarch64-linux-musl",
        "standard": "gnu2x",
        "include_dirs": ("interface/sdk_c/ai",),
        "libc_headers": True,
        "libraries": (
            ("/system/lib64/ndk/libneural_network_runtime.so", "libneural_network_runtime.so"),
            ("/system/lib64/ndk/libneural_network_core.so", "libneural_network_core.so"),
            ("/system/lib64/libc.so", "libc.so"),
        ),
        "link_mode": "dynamic_npu",
        "summary_zh": "OpenHarmony NNRT NPU 实验。平台固定 NNRT SDK 头文件与运行库，模型自行决定图、shape、dtype、设备选择、CPU 回退和比较逻辑。",
        "source_entrypoint": "int main(void)",
        "runtime_contract_zh": "可引用固定 OpenHarmony musl C 与 AI SDK 根目录中实际存在的任意头文件；源码实现 int main(void)，Build Worker 提供固定 _start 启动胶水；程序显式记录设备枚举、图完成、算子可用性、编译和执行结果。",
    },
    "media_avcodec": {
        "unit": "media",
        "language": "c",
        "compiler": "clang",
        "target": "aarch64-linux-musl",
        "standard": "c17",
        "include_dirs": (
            "interface/sdk_c/multimedia/av_codec",
            "interface/sdk_c/multimedia/media_foundation",
            "interface/sdk_c/graphic/graphic_2d",
        ),
        "libc_headers": True,
        "libraries": (
            ("/system/lib64/libnative_media_vdec.so", "libnative_media_vdec.so"),
            ("/system/lib64/libnative_media_venc.so", "libnative_media_venc.so"),
            ("/system/lib64/libnative_media_codecbase.so", "libnative_media_codecbase.so"),
            ("/system/lib64/libnative_media_core.so", "libnative_media_core.so"),
            ("/system/lib64/libc.so", "libc.so"),
        ),
        "link_mode": "dynamic_media",
        "summary_zh": "OpenHarmony AVCodec 媒体实验。它用于 capability、配置、buffer 生命周期和编解码路径验证；是否为硬件路径仍应由运行结果与能力查询决定。",
        "source_entrypoint": "int main(void)",
        "runtime_contract_zh": "可引用固定 OpenHarmony musl C、AVCodec/Media Foundation SDK 根目录中实际存在的任意头文件；源码实现 int main(void)，Build Worker 提供固定 _start 启动胶水；程序显式输出 capability、配置和 buffer 生命周期结果。",
    },
}


def _profile_id(profile: dict[str, Any]) -> str:
    return next(key for key, value in EXPERIMENT_PROFILES.items() if value is profile)


def _compiler_header_roots(workspace_root: Path) -> list[Path]:
    """Resolve Clang's fixed resource headers without consulting host SDK paths."""

    clang_root = llvm_bin_root().parent / "lib" / "clang"
    if not clang_root.is_dir():
        return []
    return sorted(
        (
            candidate.resolve()
            for candidate in clang_root.glob("*/include")
            if candidate.is_dir()
        ),
        key=lambda path: path.as_posix(),
    )


def _profile_sdk_header_roots(workspace_root: Path, profile: dict[str, Any]) -> list[Path]:
    sdk_root = openharmony_sdk_root()
    roots: list[Path] = []
    for relative in profile["include_dirs"]:
        candidate = (sdk_root / relative).resolve()
        try:
            candidate.relative_to(sdk_root)
        except ValueError:
            continue
        if candidate.is_dir():
            roots.append(candidate)
    return roots


def _generated_alltypes_header(source: str) -> str:
    """Apply musl's small mkalltypes transform without a host sed dependency."""

    generated: list[str] = []
    patterns = (
        ("TYPEDEF", re.compile(r"^TYPEDEF (.*) ([^ ]+);$"), "typedef {body} {name};", ""),
        ("STRUCT", re.compile(r"^STRUCT\s+([^ ]+)\s+(.*);$"), "struct {name} {body};", "struct_"),
        ("UNION", re.compile(r"^UNION\s+([^ ]+)\s+(.*);$"), "union {name} {body};", "union_"),
    )
    for line in source.splitlines():
        transformed = False
        for prefix, pattern, declaration, need_prefix in patterns:
            if not line.startswith(prefix):
                continue
            match = pattern.fullmatch(line)
            if match is None:
                break
            if prefix == "TYPEDEF":
                body, name = match.groups()
            else:
                name, body = match.groups()
            need_name = f"{need_prefix}{name}"
            generated.extend([
                f"#if defined(__NEED_{need_name}) && !defined(__DEFINED_{need_name})",
                declaration.format(body=body, name=name),
                f"#define __DEFINED_{need_name}",
                "#endif",
                "",
            ])
            transformed = True
            break
        if not transformed:
            generated.append(line)
    return "\n".join(generated) + "\n"


def _generated_syscall_header(source: str) -> str:
    """Mirror musl's generated __NR_* to SYS_* aliases."""

    aliases = [line.replace("__NR_", "SYS_") for line in source.splitlines() if "__NR_" in line]
    return source.rstrip() + "\n" + "\n".join(aliases) + "\n"


def _ensure_musl_header_overlay(workspace_root: Path, state_root: Path) -> tuple[Path, dict[str, Any]]:
    sdk_root = openharmony_sdk_root()
    musl_root = sdk_root / MUSL_SOURCE_RELATIVE_ROOT
    alltypes_source_paths = (
        musl_root / "arch" / "aarch64" / "bits" / "alltypes.h.in",
        musl_root / "include" / "alltypes.h.in",
    )
    syscall_source_path = musl_root / "arch" / "aarch64" / "bits" / "syscall.h.in"
    source_paths = (*alltypes_source_paths, syscall_source_path)
    missing = [path for path in source_paths if not path.is_file()]
    if missing:
        raise PlatformError(
            "DEVICE_EXPERIMENT_PROFILE_MISCONFIGURED",
            "OpenHarmony musl 头文件模板不完整，无法构造 AArch64 libc header surface。",
            status_code=503,
            details=_sdk_provisioning_details(sdk_root, missing),
        )
    alltypes_source = "".join(path.read_text(encoding="utf-8") for path in alltypes_source_paths)
    generated_headers = {
        "bits/alltypes.h": _generated_alltypes_header(alltypes_source).encode("utf-8"),
        "bits/syscall.h": _generated_syscall_header(
            syscall_source_path.read_text(encoding="utf-8")
        ).encode("utf-8"),
    }
    state = state_root.resolve()
    overlay_root = (state / MUSL_HEADER_OVERLAY_RELATIVE_ROOT).resolve()
    try:
        overlay_root.relative_to(state)
    except ValueError as exc:
        raise PlatformError(
            "DEVICE_EXPERIMENT_PROFILE_MISCONFIGURED",
            "生成的 libc header overlay 越过了平台代理工件目录。",
            status_code=500,
        ) from exc
    for relative, generated in generated_headers.items():
        output = overlay_root / relative
        if not output.is_file() or output.read_bytes() != generated:
            _atomic_write(output, generated)
    return overlay_root, {
        "headers": {
            relative: hashlib.sha256(generated).hexdigest()
            for relative, generated in generated_headers.items()
        },
        # Kept for compatibility with the first overlay revision.
        "output_sha256": hashlib.sha256(generated_headers["bits/alltypes.h"]).hexdigest(),
        "source_paths": [path.relative_to(sdk_root).as_posix() for path in source_paths],
        "generator": "ohos_musl_aarch64_headers_python/v2",
    }


def _profile_libc_header_roots(
    workspace_root: Path,
    state_root: Path,
    profile: dict[str, Any],
) -> tuple[list[Path], dict[str, Any] | None]:
    if not bool(profile.get("libc_headers")):
        return [], None
    sdk_root = openharmony_sdk_root()
    overlay_root, provenance = _ensure_musl_header_overlay(workspace_root, state_root)
    candidates = (
        sdk_root / MUSL_SOURCE_RELATIVE_ROOT / "arch" / "aarch64",
        sdk_root / MUSL_SOURCE_RELATIVE_ROOT / "arch" / "generic",
        sdk_root / MUSL_NDK_INCLUDE_RELATIVE_ROOT,
    )
    missing = [path for path in candidates if not path.is_dir()]
    if missing:
        raise PlatformError(
            "DEVICE_EXPERIMENT_PROFILE_MISCONFIGURED",
            "OpenHarmony musl libc header roots 不完整。",
            status_code=503,
            details=_sdk_provisioning_details(sdk_root, missing),
        )
    return [overlay_root, *(path.resolve() for path in candidates)], provenance


def _profile_header_roots(
    workspace_root: Path,
    state_root: Path,
    profile: dict[str, Any],
) -> list[Path]:
    libc_roots, _ = _profile_libc_header_roots(workspace_root, state_root, profile)
    return [
        *_compiler_header_roots(workspace_root),
        *libc_roots,
        *_profile_sdk_header_roots(workspace_root, profile),
    ]


def _available_profile_headers(workspace_root: Path, state_root: Path, profile: dict[str, Any]) -> list[str]:
    headers: set[str] = set()
    for root in _profile_header_roots(workspace_root, state_root, profile):
        for candidate in root.rglob("*"):
            if not candidate.is_file() or candidate.suffix.casefold() not in _HEADER_SUFFIXES:
                continue
            resolved = candidate.resolve()
            try:
                relative = resolved.relative_to(root)
            except ValueError:
                continue
            headers.add(relative.as_posix())
    return sorted(headers)


def _available_sdk_headers(workspace_root: Path, profile: dict[str, Any]) -> list[str]:
    headers: set[str] = set()
    for root in _profile_sdk_header_roots(workspace_root, profile):
        for candidate in root.rglob("*"):
            if not candidate.is_file() or candidate.suffix.casefold() not in _HEADER_SUFFIXES:
                continue
            resolved = candidate.resolve()
            try:
                headers.add(resolved.relative_to(root).as_posix())
            except ValueError:
                continue
    return sorted(headers)


def _repository_header_paths(
    workspace_root: Path,
    profile: dict[str, Any],
    headers: list[str],
) -> list[str]:
    """Return source-backed paths for the headers shown to the Agent."""

    sdk_root = openharmony_sdk_root()
    paths: set[str] = set()
    roots = [
        *_profile_sdk_header_roots(workspace_root, profile),
        sdk_root / MUSL_NDK_INCLUDE_RELATIVE_ROOT,
        sdk_root / MUSL_SOURCE_RELATIVE_ROOT / "arch" / "aarch64",
        sdk_root / MUSL_SOURCE_RELATIVE_ROOT / "arch" / "generic",
    ]
    for header in headers:
        for root in roots:
            candidate = (root / header).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            if candidate.is_file():
                paths.add(f"openharmony-sdk/{candidate.relative_to(sdk_root).as_posix()}")
                break
    return sorted(paths)


def experiment_profile_catalog(
    workspace_root: Path,
    *,
    state_root: Path | None = None,
    profile_id: str | None = None,
    header_query: str = "",
) -> list[dict[str, Any]]:
    """Return the exact fixed build surface the Page Agent may use."""

    normalized_query = header_query.strip().casefold()[:120]
    query_terms = [term for term in re.split(r"[\s,;]+", normalized_query) if term]
    selected_profiles = [
        (candidate_id, profile)
        for candidate_id, profile in EXPERIMENT_PROFILES.items()
        if profile_id is None or candidate_id == profile_id
    ]
    catalog: list[dict[str, Any]] = []
    resolved_state_root = state_root or platform_state_root()
    for candidate_id, profile in selected_profiles:
        available_headers = _available_profile_headers(workspace_root, resolved_state_root, profile)
        sdk_headers = _available_sdk_headers(workspace_root, profile)
        _, libc_provenance = _profile_libc_header_roots(workspace_root, resolved_state_root, profile)
        standard_headers = [header for header in _RECOMMENDED_LIBC_HEADERS if header in available_headers]
        libc_surface_available = bool(profile.get("libc_headers")) and len(standard_headers) == len(_RECOMMENDED_LIBC_HEADERS)
        if query_terms:
            matching_headers = [
                header
                for header in available_headers
                if (
                    not header.startswith(_INTERNAL_COMPILER_HEADER_PREFIXES)
                    and any(term in header.casefold() for term in query_terms)
                )
            ]
            shown_headers = matching_headers[:80]
            catalog_complete = len(shown_headers) == len(matching_headers)
        else:
            recommended = [
                header
                for header in (*_RECOMMENDED_COMPILER_HEADERS, *_RECOMMENDED_LIBC_HEADERS)
                if header in available_headers
            ]
            shown_headers = sorted(set([*recommended, *sdk_headers]))
            catalog_complete = len(shown_headers) == len(available_headers)
        shown_header_paths = _repository_header_paths(workspace_root, profile, shown_headers)
        catalog.append({
            "profile_id": candidate_id,
            "unit": str(profile["unit"]),
            "language": str(profile["language"]),
            "summary_zh": str(profile["summary_zh"]),
            "runtime_contract_zh": str(profile["runtime_contract_zh"]),
            "entrypoint": str(profile.get("source_entrypoint") or ('_start' if profile["language"] == "c" else 'extern "C" _start')),
            "binary_entrypoint": "_start",
            "runtime_harness": (
                "build_worker_startup_shim/v1"
                if profile.get("source_entrypoint") else
                "source_owned_freestanding_entrypoint"
            ),
            **(
                {"runtime_loader_guidance_zh": str(profile["runtime_loader_guidance_zh"])}
                if profile.get("runtime_loader_guidance_zh") else {}
            ),
            "sdk_include_roots": list(profile["include_dirs"]),
            "libc_header_surface": {
                "available": libc_surface_available,
                "family": "openharmony_musl_aarch64" if profile.get("libc_headers") else "none",
                "standard_c_headers": standard_headers,
                "linked_device_libc": any(local_name == "libc.so" for _, local_name in profile["libraries"]),
                "guidance_zh": (
                    "该 profile 已提供 OpenHarmony NDK musl AArch64 标准 C 头文件并链接设备 libc.so；"
                    "可直接使用 stdio.h、stdlib.h、string.h、unistd.h，不要删除后改写手工声明。"
                    if profile.get("libc_headers")
                    else "该 profile 是 freestanding 构建，不提供或链接 libc；不要引用 stdio.h、stdlib.h、string.h、unistd.h。"
                ),
                "generated_header_provenance": libc_provenance,
            },
            "repository_header_paths": shown_header_paths,
            "header_policy": {
                "mode": "fixed_root_resolution",
                "statement_zh": "源码可引用固定 LLVM/SDK 根目录中真实存在的任意头文件；链接 libc 的 profile 同时提供 OpenHarmony NDK musl 标准 C 头，不需要删除标准 include 或手写 libc 声明。",
                "header_query": header_query.strip()[:120],
                "header_query_terms": query_terms,
                "matched_headers": shown_headers,
                "matched_header_count": len(shown_headers),
                "total_header_count": len(available_headers),
                "catalog_complete": catalog_complete,
                "query_hint_zh": "需要查找其他头文件时，可在一次 header_query 中用空格、逗号或分号分隔多个名称。",
            },
            "linked_device_libraries": [remote for remote, _ in profile["libraries"]],
            "generated_headers": list(profile.get("generated_headers") or ()),
            "shader_contract": profile.get("shader_contract"),
        })
    return catalog


def _profile(profile_id: str) -> dict[str, Any]:
    profile = EXPERIMENT_PROFILES.get(profile_id)
    if profile is None:
        raise PlatformError(
            "DEVICE_EXPERIMENT_PROFILE_UNKNOWN",
            "实验构建 profile 不受支持。",
            status_code=422,
            details={"profile_id": profile_id, "supported_profiles": sorted(EXPERIMENT_PROFILES)},
        )
    return profile


def _text(value: Any, limit: int = MAX_EXPERIMENT_TITLE_LENGTH) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _safe_relative_path(value: Any) -> Path:
    raw = _text(value, 600).replace("\\", "/")
    relative = Path(raw)
    if not raw or relative.is_absolute() or ".." in relative.parts:
        raise PlatformError("DEVICE_EXPERIMENT_SOURCE_INVALID", "实验源码路径不在平台代理工件目录内。", status_code=422)
    return relative


def _resolve_profile_header(
    workspace_root: Path,
    state_root: Path,
    profile: dict[str, Any],
    header: str,
) -> Path | None:
    for root in _profile_header_roots(workspace_root, state_root, profile):
        candidate = (root / header).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            continue
        if candidate.is_file():
            return candidate
    return None


def _validate_source(
    workspace_root: Path,
    state_root: Path,
    profile: dict[str, Any],
    source: str,
    language: str,
) -> bytes:
    if language != profile["language"]:
        raise PlatformError(
            "DEVICE_EXPERIMENT_LANGUAGE_MISMATCH",
            "实验源码语言与所选构建 profile 不一致。",
            status_code=422,
        )
    if not source.strip():
        raise PlatformError("DEVICE_EXPERIMENT_SOURCE_EMPTY", "实验源码不能为空。", status_code=422)
    if "\x00" in source:
        raise PlatformError("DEVICE_EXPERIMENT_SOURCE_INVALID", "实验源码不能包含 NUL 字符。", status_code=422)
    try:
        encoded = source.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PlatformError("DEVICE_EXPERIMENT_SOURCE_INVALID", "实验源码必须是 UTF-8 文本。", status_code=422) from exc
    if len(encoded) > MAX_EXPERIMENT_SOURCE_BYTES:
        raise PlatformError(
            "DEVICE_EXPERIMENT_SOURCE_TOO_LARGE",
            f"实验源码超过 {MAX_EXPERIMENT_SOURCE_BYTES} 字节上限。",
            status_code=422,
        )
    if _FORBIDDEN_PREPROCESSOR.search(source) or _FORBIDDEN_INCLUDE_QUERY.search(source):
        raise PlatformError(
            "DEVICE_EXPERIMENT_PREPROCESSOR_DENIED",
            "实验源码不能使用 include_next、embed、line、pragma 或 __has_include。",
            status_code=422,
        )
    profile_id = _profile_id(profile)
    unavailable_headers: list[str] = []
    for include_argument in _INCLUDE_DIRECTIVE.findall(source):
        literal = _LITERAL_INCLUDE.fullmatch(include_argument)
        if literal is None:
            raise PlatformError(
                "DEVICE_EXPERIMENT_HEADER_DENIED",
                "实验源码只能使用字面量头文件路径，不能使用宏或动态 include。",
                status_code=422,
                details={"profile_id": profile_id, "header_policy": "fixed_root_resolution"},
            )
        normalized = str(literal.group(1) or literal.group(2) or "").strip()
        relative = Path(normalized)
        if (
            not normalized
            or normalized.startswith("/")
            or "\\" in normalized
            or relative.is_absolute()
            or ".." in relative.parts
        ):
            raise PlatformError(
                "DEVICE_EXPERIMENT_HEADER_DENIED",
                "实验源码引用的头文件路径越过了固定 SDK/include 根目录。",
                status_code=422,
                details={"header": normalized, "profile_id": profile_id, "header_policy": "fixed_root_resolution"},
            )
        generated_headers = {
            str(value)
            for value in profile.get("generated_headers", ())
            if isinstance(value, str) and value
        }
        if normalized not in generated_headers and _resolve_profile_header(workspace_root, state_root, profile, normalized) is None:
            unavailable_headers.append(normalized)
    if unavailable_headers:
        available_headers = _available_profile_headers(workspace_root, state_root, profile)
        suggestions_by_header: dict[str, list[str]] = {}
        for header in dict.fromkeys(unavailable_headers):
            suggested = difflib.get_close_matches(header, available_headers, n=12, cutoff=0.25)
            requested_parent = Path(header).parent.as_posix()
            if requested_parent not in {"", "."}:
                suggested = list(dict.fromkeys([
                    *suggested,
                    *[candidate for candidate in available_headers if Path(candidate).parent.as_posix() == requested_parent][:20],
                ]))[:24]
            suggestions_by_header[header] = suggested
        flattened_suggestions = list(dict.fromkeys(
            suggestion
            for header in dict.fromkeys(unavailable_headers)
            for suggestion in suggestions_by_header[header]
        ))[:24]
        raise PlatformError(
            "DEVICE_EXPERIMENT_HEADER_UNAVAILABLE",
            "所选实验 profile 的固定 SDK/include 根目录中不存在部分头文件。",
            status_code=422,
            details={
                "header": unavailable_headers[0],
                "missing_headers": list(dict.fromkeys(unavailable_headers)),
                "profile_id": profile_id,
                "header_policy": "fixed_root_resolution",
                "suggested_headers": flattened_suggestions,
                "suggestions_by_header": suggestions_by_header,
                "available_header_count": len(available_headers),
                "libc_headers_available": bool(profile.get("libc_headers")),
                "standard_c_headers": [
                    header for header in _RECOMMENDED_LIBC_HEADERS if header in available_headers
                ],
                "next_action_zh": (
                    "一次检查全部 missing_headers，并调用 device.describe_experiment_profiles 按需查询。"
                    "该 profile 已提供标准 C 头时，不要逐个删除 stdio.h、stdlib.h、string.h 或 unistd.h，"
                    "也不要手写 libc 声明绕过错误；若这些公共头仍缺失，应把它视为 profile 构建面缺陷。"
                    if profile.get("libc_headers")
                    else
                    "一次检查全部 missing_headers。该 profile 是 freestanding 构建面，不提供 libc 标准头；"
                    "请选择明确链接 libc 的硬件 profile，或编写真正不依赖 libc 的最小源码。"
                ),
            },
        )
    return encoded


def _validate_shader_source(profile: dict[str, Any], shader_source: str) -> bytes | None:
    contract = profile.get("shader_contract") if isinstance(profile.get("shader_contract"), dict) else None
    if contract is None:
        if shader_source.strip():
            raise PlatformError(
                "DEVICE_EXPERIMENT_SHADER_UNSUPPORTED",
                "所选实验 profile 不接受独立 shader_source。",
                status_code=422,
            )
        return None
    if not shader_source.strip():
        if bool(contract.get("required")):
            raise PlatformError(
                "DEVICE_EXPERIMENT_SHADER_REQUIRED",
                "Vulkan compute profile 需要完整 GLSL shader_source。",
                status_code=422,
            )
        return None
    if "\x00" in shader_source:
        raise PlatformError("DEVICE_EXPERIMENT_SHADER_INVALID", "GLSL shader 不能包含 NUL 字符。", status_code=422)
    if re.search(r"^\s*#\s*(?:include|include_next|line|pragma)\b", shader_source, re.MULTILINE):
        raise PlatformError(
            "DEVICE_EXPERIMENT_SHADER_PREPROCESSOR_DENIED",
            "GLSL shader 不能引用宿主文件或使用 include、line、pragma。",
            status_code=422,
        )
    try:
        encoded = shader_source.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PlatformError("DEVICE_EXPERIMENT_SHADER_INVALID", "GLSL shader 必须是 UTF-8 文本。", status_code=422) from exc
    if len(encoded) > MAX_EXPERIMENT_SHADER_BYTES:
        raise PlatformError(
            "DEVICE_EXPERIMENT_SHADER_TOO_LARGE",
            f"GLSL shader 超过 {MAX_EXPERIMENT_SHADER_BYTES} 字节上限。",
            status_code=422,
        )
    return encoded


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        temporary.write_bytes(content)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def stage_device_experiment_source(
    workspace_root: Path,
    state_root: Path,
    *,
    probe_artifact: dict[str, Any],
    profile_id: str,
    title: str,
    language: str,
    source: str,
    shader_source: str = "",
) -> dict[str, Any]:
    """Stage complete model-authored source below the artifact root.

    Staging does not compile or contact a device.  It produces an immutable
    hash reference that can be passed to the fixed builder in a later Agent
    turn.
    """

    profile = _profile(profile_id)
    encoded = _validate_source(workspace_root, state_root, profile, source, language)
    shader_encoded = _validate_shader_source(profile, shader_source)
    source_sha256 = hashlib.sha256(encoded).hexdigest()
    extension = ".cc" if language == "cpp" else ".c"
    relative = EXPERIMENT_ROOT / "sources" / f"{source_sha256[:32]}{extension}"
    target = (state_root / relative).resolve()
    root = state_root.resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise PlatformError("DEVICE_EXPERIMENT_SOURCE_INVALID", "实验源码路径不在平台代理工件目录内。", status_code=422) from exc
    if target.exists() and sha256_file(target) != source_sha256:
        raise PlatformError("DEVICE_EXPERIMENT_SOURCE_CONFLICT", "已有同名实验源码的哈希不一致。", status_code=409)
    if not target.exists():
        _atomic_write(target, encoded)
    if sha256_file(target) != source_sha256:
        raise PlatformError("DEVICE_EXPERIMENT_SOURCE_INVALID", "实验源码写入后哈希校验失败。", status_code=500)
    shader_record: dict[str, Any] | None = None
    if shader_encoded is not None:
        shader_sha256 = hashlib.sha256(shader_encoded).hexdigest()
        shader_relative = EXPERIMENT_ROOT / "sources" / f"{shader_sha256[:32]}.comp"
        shader_target = (state_root / shader_relative).resolve()
        try:
            shader_target.relative_to(root)
        except ValueError as exc:
            raise PlatformError("DEVICE_EXPERIMENT_SHADER_INVALID", "GLSL shader 路径不在平台代理工件目录内。", status_code=422) from exc
        if shader_target.exists() and sha256_file(shader_target) != shader_sha256:
            raise PlatformError("DEVICE_EXPERIMENT_SHADER_CONFLICT", "已有同名 GLSL shader 的哈希不一致。", status_code=409)
        if not shader_target.exists():
            _atomic_write(shader_target, shader_encoded)
        if sha256_file(shader_target) != shader_sha256:
            raise PlatformError("DEVICE_EXPERIMENT_SHADER_INVALID", "GLSL shader 写入后哈希校验失败。", status_code=500)
        shader_record = {
            "language": str((profile.get("shader_contract") or {}).get("language") or "glsl_compute"),
            "relative_path": shader_relative.as_posix(),
            "sha256": shader_sha256,
            "bytes": len(shader_encoded),
        }
    return {
        "schema_version": "agent_device_experiment_source/v1",
        "probe_artifact_id": probe_artifact.get("artifact_id"),
        "device": {"serial": _safe_serial(str((probe_artifact.get("device") or {}).get("serial") or ""))},
        "profile_id": profile_id,
        "requested_unit": profile["unit"],
        "language": language,
        "title": _text(title) or f"{profile_id} 实验",
        "source_relative_path": relative.as_posix(),
        "source_sha256": source_sha256,
        "source_bytes": len(encoded),
        "auxiliary_sources": ({"shader": shader_record} if shader_record else {}),
        "workstation_execution_boundary": {
            "host_shell": False,
            "source_root": "platform_artifact_root_only",
            "model_host_path_input": False,
            "compiler_flags": "not_accepted_at_staging",
            "header_policy": "fixed_root_resolution",
        },
        "summary_zh": "页面助手已将完整实验源码暂存到专用工件目录；下一步可使用固定 profile 交叉编译并通过 HDC 上传。",
    }


def _staged_source_path(state_root: Path, staged_source_artifact: dict[str, Any]) -> Path:
    relative = _safe_relative_path(staged_source_artifact.get("source_relative_path"))
    if relative.parts[:len(EXPERIMENT_ROOT.parts)] != EXPERIMENT_ROOT.parts:
        raise PlatformError("DEVICE_EXPERIMENT_SOURCE_INVALID", "实验源码不属于平台代理暂存目录。", status_code=422)
    root = state_root.resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise PlatformError("DEVICE_EXPERIMENT_SOURCE_INVALID", "实验源码路径不在平台代理工件目录内。", status_code=422) from exc
    expected = _text(staged_source_artifact.get("source_sha256"), 80)
    if not path.is_file() or not expected or sha256_file(path) != expected:
        raise PlatformError("DEVICE_EXPERIMENT_SOURCE_CHANGED", "实验源码不存在或哈希已变化，拒绝构建。", status_code=409)
    return path


def staged_shader_source_path(state_root: Path, staged_source_artifact: dict[str, Any]) -> Path | None:
    auxiliary = staged_source_artifact.get("auxiliary_sources") if isinstance(staged_source_artifact.get("auxiliary_sources"), dict) else {}
    shader = auxiliary.get("shader") if isinstance(auxiliary.get("shader"), dict) else None
    if shader is None:
        return None
    relative = _safe_relative_path(shader.get("relative_path"))
    if relative.parts[:len(EXPERIMENT_ROOT.parts)] != EXPERIMENT_ROOT.parts:
        raise PlatformError("DEVICE_EXPERIMENT_SHADER_INVALID", "GLSL shader 不属于平台代理暂存目录。", status_code=422)
    root = state_root.resolve()
    path = (root / relative).resolve()
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise PlatformError("DEVICE_EXPERIMENT_SHADER_INVALID", "GLSL shader 路径不在平台代理工件目录内。", status_code=422) from exc
    expected = _text(shader.get("sha256"), 80)
    if not path.is_file() or not expected or sha256_file(path) != expected:
        raise PlatformError("DEVICE_EXPERIMENT_SHADER_CHANGED", "GLSL shader 不存在或哈希已变化，拒绝构建。", status_code=409)
    return path


def prepare_profile_generated_sources(
    state_root: Path,
    run_root: Path,
    staged_source_artifact: dict[str, Any],
    profile: dict[str, Any],
    *,
    run_process: Any,
    timeout_seconds: int = DEVICE_VALIDATION_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Compile profile-owned auxiliary sources into fixed generated headers."""

    contract = profile.get("shader_contract") if isinstance(profile.get("shader_contract"), dict) else None
    if contract is None:
        return {}
    shader = staged_shader_source_path(state_root, staged_source_artifact)
    if shader is None:
        raise PlatformError("BUILD_SHADER_SOURCE_MISSING", "Vulkan compute 构建缺少已暂存的 GLSL shader。", status_code=422)
    glslang = glslang_binary()
    if not glslang.is_file() or not os.access(glslang, os.X_OK):
        raise PlatformError(
            "BUILD_SHADER_COMPILER_UNAVAILABLE",
            "Vulkan compute profile 缺少 glslang 编译器。请在 Linux Build Worker 上设置 "
            "HETERO_GLSLANG_BIN 为可执行文件的绝对路径，再执行 heteroctl restart；无需 refresh-env。",
            status_code=503,
            details={"configured_path": str(glslang), "configuration": "HETERO_GLSLANG_BIN"},
        )
    spirv = run_root / "experiment_shader.spv"
    command = [
        str(glslang),
        "-V",
        "--target-env",
        str(contract.get("target_environment") or "vulkan1.0"),
        "-S",
        "comp",
        "-o",
        str(spirv),
        str(shader),
    ]
    completed = run_process(command, timeout_seconds=timeout_seconds, cwd=run_root)
    detail = {"command": command, **_process_detail(completed)}
    if completed.returncode != 0 or not spirv.is_file():
        raise PlatformError(
            "BUILD_SHADER_COMPILE_FAILED",
            "GLSL compute shader 未能通过固定 glslang 编译。",
            status_code=422,
            details={"shader_compile": detail},
        )
    payload = spirv.read_bytes()
    if len(payload) < 20 or len(payload) % 4 != 0 or payload[:4] != b"\x03\x02\x23\x07":
        raise PlatformError(
            "BUILD_SHADER_OUTPUT_INVALID",
            "固定 glslang 没有生成结构完整的 SPIR-V 模块。",
            status_code=500,
            details={"shader_compile": detail, "output_bytes": len(payload)},
        )
    generated_header = str(contract.get("generated_header") or "experiment_shader_spv.h")
    byte_array_symbol = str(contract.get("byte_array_symbol") or "kExperimentShaderSpv")
    byte_size_symbol = str(contract.get("byte_size_symbol") or "kExperimentShaderSpvSize")
    rows = [
        "#ifndef HETERO_EXPERIMENT_SHADER_SPV_H_",
        "#define HETERO_EXPERIMENT_SHADER_SPV_H_",
        f"static const unsigned char {byte_array_symbol}[] __attribute__((aligned(4))) = {{",
    ]
    for offset in range(0, len(payload), 12):
        rows.append("    " + ", ".join(f"0x{value:02x}" for value in payload[offset:offset + 12]) + ",")
    rows.extend([
        "};",
        f"static const unsigned int {byte_size_symbol} = {len(payload)}u;",
        "#endif",
        "",
    ])
    _atomic_write(run_root / generated_header, "\n".join(rows).encode("ascii"))
    return {
        "shader_compile": detail,
        "shader": {
            "source_sha256": sha256_file(shader),
            "spirv_sha256": sha256_file(spirv),
            "spirv_bytes": len(payload),
            "target_environment": str(contract.get("target_environment") or "vulkan1.0"),
            "generated_header": generated_header,
            "compiler": {"name": glslang.name, "sha256": sha256_file(glslang)},
        },
    }


def _toolchain(workspace_root: Path) -> tuple[Path, Path, Path]:
    root = llvm_bin_root()
    clang = root / "clang"
    clangxx = root / "clang++"
    lld = root / "ld.lld"
    if not all(path.is_file() and os.access(path, os.X_OK) for path in (clang, clangxx, lld)):
        raise PlatformError(
            "DEVICE_EXPERIMENT_TOOLCHAIN_UNAVAILABLE",
            "未找到真机实验所需的 LLVM 工具链。请在 Linux Build Worker 上执行 "
            "./deploy/linux/heteroctl restart --llvm-bin-root <包含 clang、clang++、ld.lld 的目录>。",
            status_code=503,
            details={"llvm_bin_root": str(root)},
        )
    return clang, clangxx, lld


def _process_detail(completed: Any) -> dict[str, Any]:
    return {
        "return_code": completed.returncode,
        "stdout": _bounded(_clean_output(completed.stdout), 12 * 1024),
        "stderr": _bounded(_clean_output(completed.stderr), 12 * 1024),
    }


def _pull_profile_libraries(hdc_binary: Path, serial: str, profile: dict[str, Any], run_root: Path) -> tuple[bool, list[dict[str, Any]], list[Path]]:
    libraries_root = run_root / "device-libraries"
    libraries_root.mkdir(parents=True, exist_ok=True)
    details: list[dict[str, Any]] = []
    local_paths: list[Path] = []
    for remote, local_name in profile["libraries"]:
        local = libraries_root / local_name
        try:
            completed = _run_hdc(
                hdc_binary,
                ("file", "recv", remote, str(local)),
                serial=serial,
                timeout_seconds=90,
                require_success=False,
            )
            detail = {"remote_path": remote, "local_name": local_name, **_process_detail(completed)}
            detail["received"] = completed.returncode == 0 and local.is_file()
        except PlatformError as exc:
            detail = {"remote_path": remote, "local_name": local_name, "received": False, "error_code": exc.code, "stderr": exc.message}
        details.append(detail)
        if detail.get("received") is not True:
            return False, details, local_paths
        local_paths.append(local)
    return True, details, local_paths


def _compile_and_link(
    workspace_root: Path,
    state_root: Path,
    run_root: Path,
    source: Path,
    profile_id: str,
    profile: dict[str, Any],
    local_libraries: list[Path],
    *,
    run_process: Any | None = None,
) -> tuple[Path | None, dict[str, Any]]:
    """Compile with profile-owned flags.

    ``run_process`` lets the standalone Build Worker provide a stricter
    environment, rlimits and network policy without changing the existing
    local-device compatibility path.
    """

    clang, clangxx, lld = _toolchain(workspace_root)
    process_runner = run_process or _run_process
    compiler = clangxx if profile["compiler"] == "clang++" else clang
    object_path = run_root / "experiment.o"
    binary_path = run_root / "experiment.bin"
    compile_prefix = [
        str(compiler),
        f"--target={profile['target']}",
        "-O2",
        f"-std={profile['standard']}",
        "-nostdinc",
        "-fPIE",
        "-ffreestanding",
        "-fno-stack-protector",
        "-fno-builtin",
        "-fno-unwind-tables",
        "-fno-asynchronous-unwind-tables",
    ]
    if profile_id == "native_cpp":
        compile_prefix.extend(["-fno-exceptions", "-fno-rtti"])
    for include_root in _profile_header_roots(workspace_root, state_root, profile):
        compile_prefix.extend(["-isystem", str(include_root)])
    if profile.get("generated_headers"):
        compile_prefix.extend(["-I", str(run_root)])
    compile_command = [*compile_prefix, "-c", str(source), "-o", str(object_path)]
    compile_result = process_runner(compile_command, timeout_seconds=DEVICE_VALIDATION_TIMEOUT_SECONDS, cwd=run_root)
    detail: dict[str, Any] = {"compile": {"command": compile_command, **_process_detail(compile_result)}}
    if compile_result.returncode != 0:
        return None, detail

    mode = profile["link_mode"]
    link_objects = [object_path]
    if mode != "static":
        # Dynamic E0 experiments intentionally do not depend on a repository
        # CRT. Keep startup plumbing in the Build Worker so model-authored
        # sources can use a normal main() and focus on the hardware experiment.
        # The weak symbols preserve compatibility with older sources that
        # already provide their own _start implementation.
        startup_source = run_root / "build_worker_startup.c"
        startup_object = run_root / "build_worker_startup.o"
        startup_source.write_text(
            "extern int main(void) __attribute__((weak));\n"
            "extern void _Exit(int) __attribute__((noreturn));\n"
            "__attribute__((weak, noreturn)) void _start(void)\n"
            "{\n"
            "    _Exit(main ? main() : 127);\n"
            "}\n",
            encoding="utf-8",
        )
        startup_command = [*compile_prefix, "-c", str(startup_source), "-o", str(startup_object)]
        startup_result = process_runner(
            startup_command,
            timeout_seconds=DEVICE_VALIDATION_TIMEOUT_SECONDS,
            cwd=run_root,
        )
        detail["runtime_harness"] = {
            "id": "build_worker_startup_shim/v1",
            "source_entrypoint": str(profile.get("source_entrypoint") or "legacy _start"),
            "binary_entrypoint": "_start",
            "compile": {"command": startup_command, **_process_detail(startup_result)},
        }
        if startup_result.returncode != 0:
            return None, detail
        link_objects.insert(0, startup_object)

    if mode == "static":
        link_command = [
            str(lld), LLD_THREAD_ARGUMENT, "-m", "aarch64linux", "-static", "-e", "_start",
            "-o", str(binary_path), *[str(path) for path in link_objects],
        ]
    else:
        rpaths = ["/system/lib64", "/vendor/lib64"]
        if mode == "dynamic_npu":
            rpaths.insert(0, "/system/lib64/ndk")
        link_command = [
            str(lld), LLD_THREAD_ARGUMENT, "-m", "aarch64linux", "-pie", "-z", "max-page-size=4096", "-e", "_start",
            "-dynamic-linker", "/lib/ld-musl-aarch64.so.1",
            *[argument for rpath in rpaths for argument in ("-rpath", rpath)],
            "-o", str(binary_path), *[str(path) for path in link_objects], *[str(path) for path in local_libraries],
        ]
    link_result = process_runner(link_command, timeout_seconds=DEVICE_VALIDATION_TIMEOUT_SECONDS, cwd=run_root)
    detail["link"] = {"command": link_command, **_process_detail(link_result)}
    return (binary_path if link_result.returncode == 0 and binary_path.is_file() else None), detail


def _safe_slug(value: str) -> str:
    compact = re.sub(r"[^a-z0-9]", "", value.casefold())[:24]
    return compact or secrets.token_hex(10)


def build_and_upload_device_experiment(
    workspace_root: Path,
    state_root: Path,
    *,
    probe_artifact: dict[str, Any],
    staged_source_artifact: dict[str, Any],
    timeout_seconds: int = DEVICE_VALIDATION_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Compile a staged source with a fixed profile and upload its binary.

    Upload intentionally stops before arbitrary execution.  The following
    Agent turn can inspect the build result and use the probe-bound remote
    shell action to choose command-line arguments, diagnostics, repeats or
    cleanup on the phone.
    """

    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= MAX_EXPERIMENT_TIMEOUT_SECONDS:
        raise PlatformError("DEVICE_EXPERIMENT_TIMEOUT_INVALID", f"实验超时必须为 1 到 {MAX_EXPERIMENT_TIMEOUT_SECONDS} 秒。", status_code=422)
    profile_id = _text(staged_source_artifact.get("profile_id"), 80)
    profile = _profile(profile_id)
    if staged_source_artifact.get("probe_artifact_id") != probe_artifact.get("artifact_id"):
        raise PlatformError("DEVICE_EXPERIMENT_PROBE_MISMATCH", "实验源码不属于当前已探测设备。", status_code=409)
    source = _staged_source_path(state_root, staged_source_artifact)
    serial = _safe_serial(str((probe_artifact.get("device") or {}).get("serial") or ""))
    hdc_binary = resolve_hdc_binary(workspace_root)
    if serial not in _available_devices(hdc_binary):
        raise PlatformError("DEVICE_NOT_CONNECTED", "执行前设备已断开 HDC 连接。", status_code=409, details={"device_serial": serial})

    source_id = _safe_slug(str(staged_source_artifact.get("artifact_id") or staged_source_artifact.get("source_sha256") or "experiment"))
    run_root = state_root / EXPERIMENT_ROOT / "runs" / f"{source_id}-{secrets.token_hex(4)}"
    run_root.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    libraries_ok, library_details, local_libraries = _pull_profile_libraries(hdc_binary, serial, profile, run_root)
    common = {
        "schema_version": "agent_device_experiment_run/v1",
        "staged_source_artifact_id": staged_source_artifact.get("artifact_id"),
        "probe_artifact_id": probe_artifact.get("artifact_id"),
        "profile_id": profile_id,
        "requested_unit": profile["unit"],
        "source": {
            "relative_path": staged_source_artifact.get("source_relative_path"),
            "sha256": staged_source_artifact.get("source_sha256"),
            "bytes": staged_source_artifact.get("source_bytes"),
        },
        "device": {"serial": serial},
        "workstation_execution_boundary": {
            "host_shell": False,
            "compiler_source": "server_resolved_fixed_llvm_profile",
            "compiler_flags_source": "profile_owned_not_model_input",
            "hdc_binary_source": "server_resolved_fixed_path",
            "device_serial_source": "previously_probed_artifact",
            "host_path_input": "staged_artifact_only",
        },
    }
    if not libraries_ok:
        return {
            **common,
            "status": "dependency_pull_failed",
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "build": {"libraries": library_details},
            "summary_zh": "未能从已绑定设备取得该固定 profile 所需的运行库，因此没有编译或上传实验二进制。",
        }

    try:
        generated = prepare_profile_generated_sources(
            state_root,
            run_root,
            staged_source_artifact,
            profile,
            run_process=_run_process,
        )
    except PlatformError as exc:
        return {
            **common,
            "status": "compile_failed",
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "build": {"libraries": library_details, "error": {"code": exc.code, "message": exc.message, "details": exc.details}},
            "summary_zh": "实验的 GLSL/SPIR-V 生成阶段失败；页面助手可根据真实 shader 诊断修订后重试。",
        }
    binary, build = _compile_and_link(
        workspace_root,
        state_root,
        run_root,
        source,
        profile_id,
        profile,
        local_libraries,
    )
    build.update(generated)
    build["libraries"] = library_details
    if binary is None:
        return {
            **common,
            "status": "compile_failed",
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "build": build,
            "summary_zh": "实验源码未能通过固定 profile 的交叉编译或链接；页面助手可以根据真实错误修订源码后再次暂存。",
        }

    remote_directory = f"/data/local/tmp/hetero_agent_{source_id}"
    remote_binary = f"{remote_directory}/experiment.bin"
    mkdir = _run_hdc(
        hdc_binary,
        ("shell", f"mkdir -p {remote_directory}"),
        serial=serial,
        timeout_seconds=min(timeout_seconds, 90),
        require_success=False,
    )
    mkdir_detail = _process_detail(mkdir)
    if mkdir.returncode != 0:
        return {
            **common,
            "status": "deployment_failed",
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "build": build,
            "deployment": {"remote_directory": remote_directory, "mkdir": mkdir_detail},
            "summary_zh": "设备端实验目录创建失败，二进制未上传。",
        }
    upload = _run_hdc(
        hdc_binary,
        ("file", "send", str(binary), remote_binary),
        serial=serial,
        timeout_seconds=min(timeout_seconds, 180),
        require_success=False,
    )
    upload_detail = _process_detail(upload)
    if upload.returncode != 0:
        return {
            **common,
            "status": "deployment_failed",
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "build": build,
            "deployment": {"remote_directory": remote_directory, "remote_binary_path": remote_binary, "mkdir": mkdir_detail, "upload": upload_detail},
            "summary_zh": "交叉编译已完成，但 HDC 未能将实验二进制上传到已绑定设备。",
        }
    return {
        **common,
        "status": "uploaded",
        "duration_ms": round((time.monotonic() - started) * 1000, 3),
        "build": build,
        "deployment": {
            "remote_directory": remote_directory,
            "remote_binary_path": remote_binary,
            "binary_sha256": sha256_file(binary),
            "mkdir": mkdir_detail,
            "upload": upload_detail,
        },
        "next_action_zh": "二进制已上传。页面助手下一轮可用 device.execute_shell 自主决定 chmod、运行参数、环境诊断、重复执行、日志采集或清理命令。",
        "summary_zh": "实验二进制已通过固定交叉编译和 HDC 上传到已绑定手机；尚未把上传本身描述为功能正确、硬件卸载成功或性能收益。",
    }
