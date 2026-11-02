"""Deployment preflight for analysis, remote-worker and real-device capabilities."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import tempfile
from pathlib import Path
from typing import Any

from tools.scenario_capture.pmu_bundle import PmuBundleError, verify_bundle

from ..core import PlatformError
from ..deployment_mode import deployment_mode
from ..device_verification import resolve_hdc_binary
from ..curated_templates import CuratedTemplateError, validate_curated_template_catalog
from ..kimi_server_engine import (
    KIMI_DISABLED_NETWORK_TOOLS,
    KIMI_NATIVE_BACKEND_TOOLS,
    KimiServerAgentEngine,
)
from ..platform_paths import (
    AGENT_RESOURCES_ROOT,
    ANALYSIS_TOOLS_ROOT,
    APP_ROOT,
    EVALUATION_ROOT,
    HARDWARE_KB_ROOT,
    OPTIMIZATION_KB_ROOT,
    PATTERN_KB_ROOT,
    default_source_root,
    glslang_binary,
    llvm_bin_root,
    openharmony_sdk_root,
    runtime_asset_root,
    state_root,
)
from .provision_openharmony_sdk_bundle import inspect_sdk_bundle
from .validate_next_build import NextBuildValidationError, validate_next_build
from ..runtime_library_source import RUNTIME_LIBRARY_SOURCE_RELATIVE_PATH, runtime_library_requirements


_ENVIRONMENT_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _check(check_id: str, available: bool, path: Path, capability: str, detail: str = "") -> dict[str, Any]:
    return {
        "check_id": check_id,
        "status": "ready" if available else "unavailable",
        "capability": capability,
        "path": str(path),
        "detail": detail,
    }


def _executable(path: Path) -> bool:
    return path.is_file() and os.access(path, os.X_OK)


def _clang_resource_headers_check(llvm_bin: Path) -> tuple[bool, Path, str]:
    """Verify the Clang built-in headers promised by every fixed build profile."""

    clang_root = llvm_bin.parent / "lib" / "clang"
    include_roots = sorted(
        candidate
        for candidate in clang_root.glob("*/include")
        if candidate.is_dir()
    ) if clang_root.is_dir() else []
    for include_root in include_roots:
        if (include_root / "stddef.h").is_file() and (include_root / "stdint.h").is_file():
            return True, include_root, "Clang resource headers include stddef.h and stdint.h."
    inspected = include_roots[0] if include_roots else clang_root
    return False, inspected, (
        "HETERO_LLVM_BIN_ROOT 的相邻 lib/clang/<version>/include 必须包含 "
        "stddef.h 与 stdint.h；仅复制 clang/ld.lld 可执行文件不足以构建固定 profile。"
    )


def _load_environment_file(path: Path) -> None:
    if not path.is_file():
        raise ValueError(f"environment file not found: {path}")
    for line_number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise ValueError(f"invalid environment entry at {path}:{line_number}")
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not _ENVIRONMENT_KEY.fullmatch(key):
            raise ValueError(f"invalid environment key at {path}:{line_number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
            value = value[1:-1]
        os.environ[key] = value


def _enabled(name: str) -> bool:
    return os.environ.get(name, "").strip().casefold() in {"1", "true", "yes", "on"}


def _sandbox_check() -> tuple[bool, Path, str]:
    raw_prefix = os.environ.get("HETERO_BUILD_SANDBOX_PREFIX", "").strip()
    required = _enabled("HETERO_BUILD_REQUIRE_NETWORK_SANDBOX")
    attested = _enabled("HETERO_BUILD_SANDBOX_NETWORK_ISOLATED")
    if not required:
        return True, Path(raw_prefix or "not-required"), "Network sandbox is optional in this environment."
    try:
        prefix = shlex.split(raw_prefix)
    except ValueError:
        prefix = []
    executable = ""
    if prefix:
        candidate = Path(prefix[0]).expanduser()
        executable = str(candidate if candidate.is_absolute() else (shutil.which(prefix[0]) or ""))
    path = Path(executable or raw_prefix or "missing")
    return bool(executable and _executable(Path(executable)) and attested), path, (
        "Configure an executable HETERO_BUILD_SANDBOX_PREFIX and attest network isolation."
    )


def _sdk_header_check(sdk_root: Path) -> tuple[bool, str]:
    inspection = inspect_sdk_bundle(sdk_root)
    missing = list(inspection["missing_paths"])
    if not missing:
        return True, "Compact OpenHarmony SDK includes the complete musl and accelerator header surface."
    preview = ", ".join(missing[:6])
    if len(missing) > 6:
        preview += f", ... ({len(missing)} missing paths)"
    return False, (
        f"Missing: {preview}. This is a deployment asset issue, not an experiment source issue. "
        "Run `./deploy/linux/heteroctl install --source-root <OPENHARMONY_SOURCE_ROOT> "
        "--runtime-root <RUNTIME_ROOT>`, then rerun `heteroctl preflight --require-build`."
    )


def collect_resource_checks() -> list[dict[str, Any]]:
    mode = deployment_mode()
    if mode == "analysis":
        return []
    suffix = ".exe" if os.name == "nt" else ""
    llvm = llvm_bin_root()
    sdk = openharmony_sdk_root()
    sdk_headers_ready, sdk_headers_detail = _sdk_header_check(sdk)
    clang_headers_ready, clang_headers_path, clang_headers_detail = _clang_resource_headers_check(llvm)
    sandbox_ready, sandbox_path, sandbox_detail = _sandbox_check()
    trace_path = Path(os.environ.get("HETERO_TRACE_STREAMER") or runtime_asset_root() / "trace-streamer" / ("trace_streamer_windows.exe" if os.name == "nt" else "trace_streamer_linux")).expanduser()
    checks = [
        _check("trace.streamer", _executable(trace_path), trace_path, "trace", "设置 HETERO_TRACE_STREAMER 为本机可执行的 TraceStreamer 路径，用于解析原始 HiTrace。"),
        *[
            _check(f"toolchain.{check_id}", _executable(llvm / f"{tool}{suffix}"), llvm / f"{tool}{suffix}", "build", "设置 HETERO_LLVM_BIN_ROOT 为宿主机 LLVM bin 目录。")
            for check_id, tool in (("clang", "clang"), ("clangxx", "clang++"), ("lld", "ld.lld"))
        ],
        _check("toolchain.clang-headers", clang_headers_ready, clang_headers_path, "build", clang_headers_detail),
        _check("toolchain.glslang", _executable(glslang_binary()), glslang_binary(), "build", "设置 HETERO_GLSLANG_BIN；Vulkan shader 构建使用此工具。"),
        _check("sdk.headers", sdk_headers_ready, sdk, "build", "设置 HETERO_OPENHARMONY_SDK_ROOT 为完整 SDK 目录，或从代码仓生成紧凑 SDK。 " + sdk_headers_detail),
        _check("build.network-sandbox", sandbox_ready, sandbox_path, "build", sandbox_detail),
    ]
    library_root = os.environ.get("HETERO_RUNTIME_LIBRARY_ROOT") or os.environ.get("HETERO_BUILD_RUNTIME_LIBRARY_ROOT")
    registry_path = state_root() / RUNTIME_LIBRARY_SOURCE_RELATIVE_PATH
    registry_detail = ""
    if not library_root and registry_path.is_file():
        try:
            registry = json.loads(registry_path.read_text(encoding="utf-8"))
            library_root = registry.get("library_root") if isinstance(registry, dict) else None
        except (OSError, ValueError) as exc:
            registry_detail = f"共享运行库配置读取失败：{exc}。"
    for profile, names in runtime_library_requirements().items():
        legacy_root = Path(os.environ.get("HETERO_BUILD_PROFILE_LIBRARY_ROOT") or state_root() / "build-worker" / "profile-runtime-libraries").expanduser()
        root = Path(library_root).expanduser() if isinstance(library_root, str) and library_root else legacy_root / profile
        missing = [name for name in names if not (root / name).is_file()]
        checks.append(_check(f"runtime.{profile}", not missing, root, "build", registry_detail + "设置 HETERO_RUNTIME_LIBRARY_ROOT 为目标 AArch64 运行库目录；缺少：" + ", ".join(missing)))
    if mode == "device":
        pmu_root = Path(os.environ.get("HETERO_PMU_SAMPLER_BUNDLE_ROOT") or runtime_asset_root() / "pmu-sampler").expanduser()
        try:
            sampler = verify_bundle(pmu_root)
            checks.append(_check("pmu.bundle", True, pmu_root, "pmu", f"Verified {sampler['target']} sampler {sampler['sampler_version']}; sha256={sampler['sha256']}. Device PMU permissions/events are checked during capture."))
        except PmuBundleError as exc:
            checks.append(_check("pmu.bundle", False, pmu_root, "pmu", f"{exc.code}: {exc.message} 正常 install/start/restart 会自动准备仓库随附采样器；可运行 `heteroctl pmu` 重试。检查仓库预置包完整性与目标目录权限，常规部署无需 SDK 或手动传包。"))
        try:
            hdc = resolve_hdc_binary(APP_ROOT)
            checks.append(_check("device.hdc", True, hdc, "connection"))
        except PlatformError:
            checks.append(_check("device.hdc", False, Path(os.environ.get("HETERO_HDC_BIN") or "hdc"), "connection", "本机直连时设置 HETERO_HDC_BIN；手机连接 Windows 时，在 Windows 运行 run-device-bridge.ps1 并连接本平台，Linux 无需安装 HDC。"))
    return checks


def collect_checks() -> list[dict[str, Any]]:
    mode = deployment_mode()
    state = state_root()
    writable = False
    try:
        state.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=state, prefix=".preflight-", delete=True):
            writable = True
    except OSError:
        pass
    if mode == "worker":
        return [
            _check("state.writable", writable, state, "core", "Set HETERO_STATE_ROOT to a writable persistent directory."),
            _check("source.default", default_source_root().is_dir(), default_source_root(), "core", "Set HETERO_DEFAULT_SOURCE_ROOT to the product source used by remote code analysis."),
            _check("analysis.tools", (ANALYSIS_TOOLS_ROOT / "precise_feature_extractor.py").is_file(), ANALYSIS_TOOLS_ROOT, "core"),
            *collect_resource_checks(),
        ]
    template_root = AGENT_RESOURCES_ROOT / "hardware-api-templates"
    web_build_root = APP_ROOT / ".next"
    web_build_ready = False
    web_build_detail = ""
    try:
        web_build = validate_next_build(web_build_root)
        web_build_ready = True
        web_build_detail = (
            f"build_id={web_build['build_id']}; "
            f"static_assets={web_build['static_asset_count']}; "
            f"referenced_assets={web_build['referenced_asset_count']}"
        )
    except (OSError, UnicodeError, NextBuildValidationError) as exc:
        web_build_detail = f"Run a clean production build: {exc}"
    template_ready = False
    template_detail = ""
    try:
        catalog, registered = validate_curated_template_catalog(template_root)
        template_ready = True
        template_detail = (
            f"{len(catalog['templates'])} templates and {len(registered)} hash-pinned files validated."
        )
    except CuratedTemplateError as exc:
        template_detail = str(exc)
    effective_disabled_tools = set(KimiServerAgentEngine._disabled_tools(None))
    declared_tool_policy = KimiServerAgentEngine.tool_policy()
    agent_tool_policy_ready = (
        effective_disabled_tools == {"FetchURL", "WebSearch"}
        and set(KIMI_DISABLED_NETWORK_TOOLS) == effective_disabled_tools
        and not effective_disabled_tools.intersection(KIMI_NATIVE_BACKEND_TOOLS)
        and declared_tool_policy.get("backend_control") == "kimi_native_tools"
        and declared_tool_policy.get("native_tool_mode") == "all_available_except_disabled"
        and declared_tool_policy.get("web_search_enabled") is False
        and declared_tool_policy.get("web_fetch_enabled") is False
    )
    checks = [
        _check("app.build", web_build_ready, web_build_root, "core", web_build_detail),
        _check("state.writable", writable, state, "core", "Set HETERO_STATE_ROOT to a writable persistent directory."),
        _check("source.default", default_source_root().is_dir(), default_source_root(), "core", "Optional product repositories can be added later in the UI."),
        _check("kb.optimization", (OPTIMIZATION_KB_ROOT / "optimization_knowledge_base.json").is_file(), OPTIMIZATION_KB_ROOT, "core"),
        _check("kb.hardware", (HARDWARE_KB_ROOT / "hardware_knowledge_base.json").is_file(), HARDWARE_KB_ROOT, "core"),
        _check("kb.patterns", (PATTERN_KB_ROOT / "kb_manifest.json").is_file(), PATTERN_KB_ROOT, "core"),
        _check("evaluation", (EVALUATION_ROOT / "hardware_kb_strategy_skill_cases.json").is_file(), EVALUATION_ROOT, "core"),
        _check("analysis.tools", (ANALYSIS_TOOLS_ROOT / "precise_feature_extractor.py").is_file(), ANALYSIS_TOOLS_ROOT, "core"),
        _check(
            "agent.templates",
            template_ready,
            template_root,
            "core",
            template_detail or "Deploy the versioned agent-resources directory with the application repository.",
        ),
        _check(
            "agent.external-search-disabled",
            agent_tool_policy_ready,
            APP_ROOT / "backend" / "kimi_server_engine.py",
            "core",
            "Effective Kimi prompt policy must disable only WebSearch/FetchURL and preserve native backend coding tools.",
        ),
        *collect_resource_checks(),
    ]
    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description="Check deployable platform resources")
    parser.add_argument("--require-trace", action="store_true")
    parser.add_argument("--require-build", action="store_true")
    parser.add_argument("--require-device", action="store_true")
    parser.add_argument("--require-pmu", action="store_true")
    parser.add_argument("--env-file", help="Load deployment environment values without executing a shell")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--resources-only", action="store_true", help="Only check optional trace/device resources")
    args = parser.parse_args()
    if args.env_file:
        try:
            _load_environment_file(Path(args.env_file).expanduser().resolve())
        except (OSError, UnicodeError, ValueError) as exc:
            parser.error(str(exc))
    mode = deployment_mode()
    if mode == "analysis" and (args.require_build or args.require_device or args.require_trace or args.require_pmu):
        parser.error("分析部署跳过 Trace、构建和真机资源预检；使用 --worker-only 或 --with-device-validation 切换后重试。")
    if mode == "worker" and (args.require_device or args.require_pmu):
        parser.error("Worker 部署不提供 HDC、PMU 或真机能力；设备采集由 Desktop 本地负责。")
    checks = collect_resource_checks() if args.resources_only else collect_checks()
    required = {"core"}
    if args.require_trace:
        required.add("trace")
    if args.require_build or args.require_device:
        required.add("build")
    if args.require_pmu:
        required.add("pmu")
    failures = [item for item in checks if item["capability"] in required and item["status"] != "ready"]
    if args.json:
        print(json.dumps({"status": "ready" if not failures else "blocked", "deployment_mode": mode, "checks": checks}, ensure_ascii=False, indent=2))
    else:
        print(f"Deployment mode: {mode}")
        if mode == "analysis":
            print("分析模式：已跳过 TraceStreamer、工具链、SDK、运行库和设备连接预检；真机验证入口已关闭。")
        elif mode == "worker":
            print("Worker 模式：检查 TraceStreamer 与构建资源；HDC、PMU、Bridge 和真机入口由 Desktop 本地负责。")
        for item in checks:
            marker = "OK" if item["status"] == "ready" else "--"
            print(f"[{marker}] {item['capability']:6s} {item['check_id']}: {item['path']}")
            if item["status"] != "ready" and item["detail"]:
                print(f"             {item['detail']}")
        print("Deployment preflight ready." if not failures else f"Deployment preflight blocked by {len(failures)} required check(s).")
        unavailable_resources = [
            item for item in checks
            if item["status"] != "ready" and item["capability"] in {"build", "trace", "connection", "pmu"}
        ]
        if unavailable_resources and failures:
            print("必需资源缺项已列出；补齐后才能启动或使用本次要求的能力。")
        elif unavailable_resources:
            print("可选资源缺项已列出；平台仍可启动并进行分析，使用对应能力前请补齐资源。")
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
