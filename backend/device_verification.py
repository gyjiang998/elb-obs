"""Real-device probe, verification, and remote-shell execution primitives.

The workstation boundary is strict: this module owns HDC resolution and all
host process creation, never sends a model string through a host shell, and
records each device operation.  In the local full-access mode, a model may
choose a remote device-shell command through the dedicated runner.  A recipe
or remote command can prove only the scope stated in its artifact; it never
proves that an arbitrary hotspot was ported or is faster.
"""

from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import subprocess
import threading
import time
from pathlib import Path
from typing import Any, Sequence

from .core import PlatformError, sha256_file
from .platform_paths import runtime_asset_root


DEVICE_UNITS = frozenset({"npu", "gpu", "dsp", "media"})
SERIAL_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
ANSI_ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

HDC_COMMAND_TIMEOUT_SECONDS = 20
DEVICE_VALIDATION_TIMEOUT_SECONDS = 240
MAX_COMMAND_OUTPUT_BYTES = 64 * 1024
MAX_REMOTE_SHELL_COMMAND_BYTES = 16 * 1024
REMOTE_SHELL_DEFAULT_TIMEOUT_SECONDS = 240
REMOTE_SHELL_MAX_TIMEOUT_SECONDS = 900
OUTPUT_TRUNCATION_MARKER = "\n[输出已截断]"

PROPERTY_KEYS: tuple[tuple[str, str], ...] = (
    ("model", "const.product.model"),
    ("hardware", "ohos.boot.hardware"),
    ("chipset", "ohos.boot.chiptype"),
    ("gpu_vendor", "const.gpu.vendor"),
    ("os_full_name", "const.ohos.fullname"),
    ("api_version", "const.ohos.apiversion"),
    ("gles3_capability", "const.SystemCapability.Graphic.Graphic2D.GLES3"),
    ("vulkan_capability", "const.SystemCapability.Graphic.Graphic2D.Vulkan"),
)

UNIT_RUNTIME_PATHS: dict[str, tuple[str, ...]] = {
    "gpu": (
        "/system/lib64/libEGL.so",
        "/system/lib64/libGLESv3.so",
        "/system/lib64/libvulkan.so",
    ),
    "dsp": (
        "/vendor/lib64/libspeaker_processing_dsp.z.so",
        "/vendor/lib64/libspatialization_processing_dsp.z.so",
        "/vendor/lib64/libdsp_ctl.so",
    ),
    # These file observations are deliberately only a weak hint.  NNRT device
    # enumeration, graph Finish/GetAvailableOperations and compilation remain
    # necessary before an NPU claim can be made.
    "npu": (
        "/system/lib64/ndk/libneural_network_core.so",
        "/system/lib64/ndk/libneural_network_runtime.so",
        "/system/lib64/libneural_network_runtime.so",
        "/system/lib64/libneural_network_runtime_ndk.z.so",
        "/system/lib64/libhiai.so",
    ),
    "media": (
        "/system/lib64/libnative_media_codecbase.so",
        "/system/lib64/libnative_media_core.so",
        "/vendor/lib64/libcodec_component.z.so",
    ),
}

GPU_BRIGHTNESS_SAMPLE = Path("demo/hardware-api-examples/03_gpu_rgba_brightness.cpp")
GPU_BRIGHTNESS_RUNNER = Path("demo/hardware-api-examples/device-run/run_gpu_rgba_brightness_gles3_smoke.sh")
GPU_BRIGHTNESS_REMOTE_BINARY = "/data/local/tmp/gpu_rgba_brightness_gles3_smoke"
NPU_MATMUL_SAMPLE = Path("demo/hardware-api-examples/02_npu_matmul.cpp")
NPU_MATMUL_RUNNER = Path("demo/hardware-api-examples/device-run/run_npu_nnrt_matmul_smoke.sh")
NPU_MATMUL_REMOTE_BINARY = "/data/local/tmp/npu_nnrt_matmul_smoke"


def _bounded(value: str, limit: int = MAX_COMMAND_OUTPUT_BYTES) -> str:
    encoded = value.encode("utf-8", errors="replace")
    if len(encoded) <= limit:
        return value
    marker = OUTPUT_TRUNCATION_MARKER.encode("utf-8")
    if limit <= len(marker):
        return marker[:limit].decode("utf-8", errors="ignore")
    # `ignore` avoids turning a partial UTF-8 code point into a larger U+FFFD
    # sequence and therefore keeps the resulting byte length within `limit`.
    prefix = encoded[:limit - len(marker)].decode("utf-8", errors="ignore")
    return f"{prefix}{OUTPUT_TRUNCATION_MARKER}"


class _CappedPipeDrain:
    """Drain one child pipe without allowing remote output to fill host memory."""

    def __init__(self, stream: Any, *, limit: int = MAX_COMMAND_OUTPUT_BYTES) -> None:
        self._stream = stream
        self._limit = limit
        self._captured = bytearray()
        self.truncated = False

    def drain(self) -> None:
        try:
            while True:
                chunk = self._stream.read(8192)
                if not chunk:
                    return
                remaining = self._limit - len(self._captured)
                if remaining > 0:
                    self._captured.extend(chunk[:remaining])
                if len(chunk) > remaining:
                    self.truncated = True
        finally:
            self._stream.close()

    def text(self) -> str:
        value = bytes(self._captured).decode("utf-8", errors="replace")
        if self.truncated:
            value = f"{value}{OUTPUT_TRUNCATION_MARKER}"
        return _bounded(value, self._limit)


def _clean_output(value: str) -> str:
    """Remove HDC's terminal warning noise without changing device evidence."""

    text = ANSI_ESCAPE.sub("", value.replace("\r", ""))
    lines = [
        line.strip()
        for line in text.splitlines()
        if "FreeChannelContinue" not in line and "data is nullptr" not in line
    ]
    return "\n".join(line for line in lines if line).strip()


def _safe_serial(serial: str) -> str:
    value = serial.strip()
    if not SERIAL_PATTERN.fullmatch(value):
        raise PlatformError(
            "DEVICE_SERIAL_INVALID",
            "设备序列号格式无效。",
            status_code=422,
        )
    return value


def resolve_hdc_binary(workspace_root: Path) -> Path:
    """Resolve one configured HDC binary without accepting browser paths."""

    configured = os.environ.get("HETERO_HDC_BIN", "").strip()
    candidates: list[Path] = []
    if configured:
        candidates.append(Path(configured).expanduser())
    else:
        candidates.append(runtime_asset_root() / "hdc" / ("hdc.exe" if os.name == "nt" else "hdc"))
        discovered = shutil.which("hdc")
        if discovered:
            candidates.append(Path(discovered))

    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved.is_file() and os.access(resolved, os.X_OK):
            return resolved

    details: dict[str, Any] = {
        "configuration": "HETERO_HDC_BIN" if configured else "runtime asset or PATH",
    }
    if configured:
        details["configured_path"] = configured
    raise PlatformError(
        "DEVICE_HDC_UNAVAILABLE",
        "未找到可执行的 HDC。请在本机设置 HETERO_HDC_BIN，或安装 OpenHarmony SDK 的 hdc。",
        status_code=503,
        details=details,
    )


def _run_process(command: Sequence[str], *, timeout_seconds: int, cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    try:
        process = subprocess.Popen(
            list(command),
            cwd=str(cwd) if cwd else None,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=False,
            shell=False,
        )
    except FileNotFoundError as exc:
        raise PlatformError("DEVICE_TOOL_UNAVAILABLE", "真机验证所需的本机工具不可用。", status_code=503) from exc
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_drain = _CappedPipeDrain(process.stdout)
    stderr_drain = _CappedPipeDrain(process.stderr)
    stdout_thread = threading.Thread(target=stdout_drain.drain, name="device-command-stdout", daemon=True)
    stderr_thread = threading.Thread(target=stderr_drain.drain, name="device-command-stderr", daemon=True)
    stdout_thread.start()
    stderr_thread.start()
    try:
        return_code = process.wait(timeout=timeout_seconds)
    except subprocess.TimeoutExpired as exc:
        process.kill()
        process.wait()
        stdout_thread.join()
        stderr_thread.join()
        raise PlatformError(
            "DEVICE_COMMAND_TIMEOUT",
            f"真机命令在 {timeout_seconds} 秒内没有完成。",
            status_code=504,
            details={
                "timeout_seconds": timeout_seconds,
                "stdout": _bounded(stdout_drain.text(), 4096),
                "stderr": _bounded(stderr_drain.text(), 4096),
            },
        ) from exc
    stdout_thread.join()
    stderr_thread.join()
    return subprocess.CompletedProcess(
        list(command),
        return_code,
        stdout_drain.text(),
        stderr_drain.text(),
    )


def _as_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    return value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value


def _run_hdc(
    hdc_binary: Path,
    command: Sequence[str],
    *,
    serial: str | None = None,
    timeout_seconds: int = HDC_COMMAND_TIMEOUT_SECONDS,
    require_success: bool = True,
) -> subprocess.CompletedProcess[str]:
    args = [str(hdc_binary)]
    if serial is not None:
        args.extend(["-t", _safe_serial(serial)])
    args.extend(command)
    completed = _run_process(args, timeout_seconds=timeout_seconds)
    if require_success and completed.returncode != 0:
        raise PlatformError(
            "DEVICE_HDC_COMMAND_FAILED",
            "HDC 未能完成受控的真机操作。",
            status_code=502,
            details={
                "operation": " ".join(command[:3]),
                "return_code": completed.returncode,
                "stdout": _bounded(_clean_output(completed.stdout), 4096),
                "stderr": _bounded(_clean_output(completed.stderr), 4096),
            },
        )
    return completed


def _available_devices(hdc_binary: Path, *, timeout_seconds: int = HDC_COMMAND_TIMEOUT_SECONDS) -> list[str]:
    completed = _run_hdc(hdc_binary, ("list", "targets"), require_success=False, timeout_seconds=timeout_seconds)
    if completed.returncode != 0:
        return []
    devices: list[str] = []
    for line in _clean_output(completed.stdout).splitlines():
        candidate = line.strip().split()[0] if line.strip() else ""
        if SERIAL_PATTERN.fullmatch(candidate):
            devices.append(candidate)
    return list(dict.fromkeys(devices))


def execute_remote_device_shell(
    workspace_root: Path,
    *,
    probe_artifact: dict[str, Any],
    command: str,
    timeout_seconds: int = REMOTE_SHELL_DEFAULT_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Execute a model-selected shell command on one previously probed device.

    This deliberately gives the remote device command freedom in full-access
    mode.  The workstation boundary remains strict: the fixed HDC executable is
    invoked with ``shell=False`` through ``subprocess.run``; the remote command
    is one argument to HDC, never interpolated into a host command line.
    """

    if not isinstance(command, str) or not command.strip():
        raise PlatformError("DEVICE_REMOTE_SHELL_EMPTY", "远端 Shell 命令不能为空。", status_code=422)
    if "\x00" in command:
        raise PlatformError("DEVICE_REMOTE_SHELL_INVALID", "远端 Shell 命令不能包含 NUL 字符。", status_code=422)
    try:
        encoded = command.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PlatformError("DEVICE_REMOTE_SHELL_INVALID", "远端 Shell 命令包含无法 UTF-8 编码的字符。", status_code=422) from exc
    if len(encoded) > MAX_REMOTE_SHELL_COMMAND_BYTES:
        raise PlatformError(
            "DEVICE_REMOTE_SHELL_TOO_LARGE",
            f"远端 Shell 命令超过 {MAX_REMOTE_SHELL_COMMAND_BYTES} 字节上限。",
            status_code=422,
        )
    if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= REMOTE_SHELL_MAX_TIMEOUT_SECONDS:
        raise PlatformError(
            "DEVICE_REMOTE_SHELL_TIMEOUT_INVALID",
            f"远端 Shell 超时必须为 1 到 {REMOTE_SHELL_MAX_TIMEOUT_SECONDS} 秒。",
            status_code=422,
        )

    probe_device_data = probe_artifact.get("device") if isinstance(probe_artifact.get("device"), dict) else {}
    serial = _safe_serial(str(probe_device_data.get("serial") or ""))
    hdc_binary = resolve_hdc_binary(workspace_root)
    if serial not in _available_devices(hdc_binary):
        raise PlatformError(
            "DEVICE_NOT_CONNECTED",
            "执行前设备已断开 HDC 连接。",
            status_code=409,
            details={"device_serial": serial},
        )

    started = time.monotonic()
    stdout = ""
    stderr = ""
    return_code: int | None = None
    timed_out = False
    try:
        completed = _run_hdc(
            hdc_binary,
            # `command` stays a single argv item. HDC relays it to the remote
            # device shell; no workstation shell parses it.
            ("shell", command),
            serial=serial,
            timeout_seconds=timeout_seconds,
            require_success=False,
        )
        return_code = completed.returncode
        stdout = _bounded(_clean_output(completed.stdout))
        stderr = _bounded(_clean_output(completed.stderr))
    except PlatformError as exc:
        if exc.code != "DEVICE_COMMAND_TIMEOUT":
            raise
        timed_out = True
        stdout = _bounded(_as_text(exc.details.get("stdout")), MAX_COMMAND_OUTPUT_BYTES)
        stderr = _bounded(exc.message, 4096)

    status = "timed_out" if timed_out else "succeeded" if return_code == 0 else "failed"
    summary = (
        "页面助手发起的远端 Shell 命令已在已绑定设备上完成。"
        if status == "succeeded"
        else "页面助手发起的远端 Shell 命令在设备侧超时；命令可能仍在设备上运行。"
        if status == "timed_out"
        else "页面助手发起的远端 Shell 命令已在设备侧返回非零状态。"
    )
    return {
        "schema_version": "device_remote_shell_run/v1",
        "probe_artifact_id": probe_artifact.get("artifact_id"),
        "device": {"serial": serial},
        "command": command,
        "command_sha256": hashlib.sha256(encoded).hexdigest(),
        "status": status,
        "execution": {
            "duration_ms": round((time.monotonic() - started) * 1000, 3),
            "return_code": return_code,
            "timed_out": timed_out,
            "timeout_seconds": timeout_seconds,
            "stdout": stdout,
            "stderr": stderr,
        },
        "workstation_execution_boundary": {
            "host_shell": False,
            "hdc_binary_source": "server_resolved_fixed_path",
            "device_serial_source": "previously_probed_artifact",
            "host_command_input": "fixed_hdc_argv_plus_one_remote_shell_argument",
            "host_working_directory": None,
        },
        "device_execution_boundary_zh": "全授权页面助手可选择设备侧 Shell 命令；平台只约束工作站侧进程创建、设备绑定、超时、输出大小和审计，不把该命令解释为设备安全策略。",
        "summary_zh": summary,
    }


def discover_devices(workspace_root: Path, *, timeout_seconds: int | None = None) -> dict[str, Any]:
    """Discover attached devices. This makes no device-side state change."""

    try:
        hdc_binary = resolve_hdc_binary(workspace_root)
    except PlatformError as exc:
        return {
            "schema_version": "device_discovery/v1",
            "hdc": {
                "available": False,
                "diagnostic": exc.message,
                "error_code": exc.code,
            },
            "devices": [],
        }

    if timeout_seconds is not None and (isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, int) or not 1 <= timeout_seconds <= HDC_COMMAND_TIMEOUT_SECONDS):
        raise PlatformError("DEVICE_DISCOVERY_TIMEOUT_INVALID", "设备发现时限超出允许范围。", status_code=422)
    # Automatic discovery only needs the current targets. Avoid spending a
    # second command timeout querying the HDC binary version on each refresh.
    version = _run_hdc(hdc_binary, ("version",), require_success=False) if timeout_seconds is None else None
    return {
        "schema_version": "device_discovery/v1",
        "hdc": {
            "available": True,
            "version": _clean_output(version.stdout) if version is not None and version.returncode == 0 else "unknown",
            "configuration": "HETERO_HDC_BIN" if os.environ.get("HETERO_HDC_BIN") else "workspace default or PATH",
        },
        "devices": [{"serial": serial, "connection_state": "connected"} for serial in
                    (_available_devices(hdc_binary) if timeout_seconds is None else _available_devices(hdc_binary, timeout_seconds=timeout_seconds))],
    }


def _property(hdc_binary: Path, serial: str, property_key: str) -> str | None:
    completed = _run_hdc(
        hdc_binary,
        ("shell", "param", "get", property_key),
        serial=serial,
        require_success=False,
    )
    if completed.returncode != 0:
        return None
    value = _clean_output(completed.stdout)
    return value or None


def _runtime_path_exists(hdc_binary: Path, serial: str, path: str) -> bool:
    completed = _run_hdc(
        hdc_binary,
        ("shell", "ls", path),
        serial=serial,
        require_success=False,
    )
    if completed.returncode != 0:
        return False
    return path in _clean_output(completed.stdout).splitlines()


def _unit_observation(unit_id: str, properties: dict[str, str | None], files: list[str]) -> dict[str, Any]:
    if unit_id == "gpu":
        gles = str(properties.get("gles3_capability") or "").casefold() == "true" or "/system/lib64/libGLESv3.so" in files
        vulkan = str(properties.get("vulkan_capability") or "").casefold() == "true" or "/system/lib64/libvulkan.so" in files
        return {
            "unit_id": "gpu",
            "observation_status": "observed" if gles or vulkan else "not_observed",
            "observed_capabilities": [name for name, present in (("GLES3", gles), ("Vulkan runtime", vulkan)) if present],
            "observed_runtime_paths": files,
            "claim_boundary_zh": "仅观测到系统属性或运行库；仍需针对目标格式、context、shader、搬运和同步做实测。",
            "recommended_next_step_zh": "按数据语义、现有管线和验证成本选择实验：图像/纹理逐像素变换优先比较 GLES fragment；规则通用 buffer 可用 GLES 3.1 compute 做低脚手架成本证伪；需要显式内存、同步、通用 compute 或生产级 Vulkan 集成时使用 gpu_vulkan。三条路径都必须验证 context/device、格式、搬运、同步、readback 与 CPU 对照；单一 API family 失败不能代表整个 GPU 不可用。",
        }
    if unit_id == "dsp":
        return {
            "unit_id": "dsp",
            "observation_status": "observed" if files else "not_observed",
            "observed_capabilities": ["Audio Effect DSP provider"] if files else [],
            "observed_runtime_paths": files,
            "claim_boundary_zh": "vendor DSP provider 文件只说明系统路径可能存在，不证明任意 PCM 算法可直接调用或已经卸载。",
            "recommended_next_step_zh": "需要匹配 Audio Effect HDI/provider、PCM 格式、控制命令、实时约束和 AP fallback。",
        }
    if unit_id == "npu":
        return {
            "unit_id": "npu",
            "observation_status": "observed" if files else "not_observed",
            "observed_capabilities": ["NNRT/HIAI runtime file"] if files else [],
            "observed_runtime_paths": files,
            "claim_boundary_zh": "运行库文件不能证明模型图、shape/dtype/layout 或指定设备编译可用。",
            "recommended_next_step_zh": "需要设备枚举、真实子图 Finish、GetAvailableOperations、指定设备编译和端到端基准。",
        }
    return {
        "unit_id": "media",
        "observation_status": "observed" if files else "not_observed",
        "observed_capabilities": ["media runtime file"] if files else [],
        "observed_runtime_paths": files,
        "claim_boundary_zh": "媒体运行库文件不能证明目标编码格式、component、buffer 生命周期或硬件路径可用。",
        "recommended_next_step_zh": "需要 capability query、格式/色彩空间、buffer 生命周期、回调与 software fallback 验证。",
    }


def probe_device(workspace_root: Path, serial: str) -> dict[str, Any]:
    """Run a fixed, read-only probe against one connected HDC target."""

    serial = _safe_serial(serial)
    hdc_binary = resolve_hdc_binary(workspace_root)
    if serial not in _available_devices(hdc_binary):
        raise PlatformError(
            "DEVICE_NOT_CONNECTED",
            "所选真机当前不在 HDC 已连接设备列表中。",
            status_code=404,
            details={"device_serial": serial},
        )

    version = _run_hdc(hdc_binary, ("version",), require_success=False)
    properties = {name: _property(hdc_binary, serial, key) for name, key in PROPERTY_KEYS}
    units: dict[str, dict[str, Any]] = {}
    for unit_id in sorted(DEVICE_UNITS):
        observed_paths = [
            path for path in UNIT_RUNTIME_PATHS[unit_id]
            if _runtime_path_exists(hdc_binary, serial, path)
        ]
        units[unit_id] = _unit_observation(unit_id, properties, observed_paths)

    return {
        "schema_version": "device_probe/v1",
        "probe_profile": "hdc-readonly-capability-probe/v1",
        "device": {
            "serial": serial,
            "model": properties.get("model"),
            "hardware": properties.get("hardware"),
            "chipset": properties.get("chipset"),
            "os_full_name": properties.get("os_full_name"),
            "api_version": properties.get("api_version"),
            "gpu_vendor": properties.get("gpu_vendor"),
        },
        "hdc": {
            "version": _clean_output(version.stdout) if version.returncode == 0 else "unknown",
            "probe_mode": "read_only_allowlist",
        },
        "observed_units": units,
        "probe_evidence": {
            "property_allowlist": [key for _, key in PROPERTY_KEYS],
            "runtime_path_allowlist": UNIT_RUNTIME_PATHS,
            "command_policy": "只调用 list targets、version、shell param get 和 shell ls；不写入设备。",
        },
        "claim_boundary_zh": "本工件是一次设备时刻的只读观察，不等价于 API 调用权限、目标代码卸载成功、端到端收益或功耗结论。",
    }


def _reference_source_match_level(reference_path: Path, code_unit: dict[str, Any]) -> str | None:
    """Compare a pasted reference without mistaking an arbitrary loop for it.

    Browser textareas and fenced-code extraction commonly drop the final newline.
    A terminal CR/LF sequence has no C/C++ token-level meaning, so it is safe to
    canonicalize *only* that boundary.  Every other byte remains part of the
    registered source contract; this must never become a broad whitespace or
    AST-similarity match because the runner executes a fixed reviewed binary.
    """

    code = code_unit.get("code")
    if not isinstance(code, str) or not code.strip():
        raise PlatformError("DEVICE_VALIDATION_CODE_INVALID", "真机验证需要可读取的热点代码工件。", status_code=422)
    reference = reference_path.read_text(encoding="utf-8")
    if code == reference:
        return "byte_exact"
    normalized_reference = reference.replace("\r\n", "\n").rstrip("\n")
    normalized_code = code.replace("\r\n", "\n").rstrip("\n")
    return "terminal_newline_normalized" if normalized_code == normalized_reference else None


def _reference_match_description(match_level: str) -> str:
    return "字节严格匹配" if match_level == "byte_exact" else "仅末尾换行规范化后匹配"


def _gpu_baseline_recipe_contract(workspace_root: Path, probe: dict[str, Any]) -> dict[str, Any]:
    """Return the reviewed GPU baseline without relating it to a hotspot.

    A baseline proves that one fixed, reviewed GLES3 pipeline can run on this
    device.  It is useful evidence after a model has selected GPU semantically,
    but it must never be presented as execution of arbitrary user code.
    """

    app_root = workspace_root
    sample_path = app_root / GPU_BRIGHTNESS_SAMPLE
    runner_path = app_root / GPU_BRIGHTNESS_RUNNER
    if not sample_path.is_file() or not runner_path.is_file():
        return {
            "status": "unavailable",
            "recipe": None,
            "reason_zh": "已登记的 GPU 平台基线配方文件在当前工作区不可用。",
        }
    observed_units = probe.get("observed_units") if isinstance(probe.get("observed_units"), dict) else {}
    gpu = observed_units.get("gpu") if isinstance(observed_units.get("gpu"), dict) else {}
    observed = gpu.get("observed_capabilities", []) if isinstance(gpu.get("observed_capabilities"), list) else []
    if "GLES3" not in observed:
        return {
            "status": "evidence_required",
            "recipe": None,
            "reason_zh": "设备探测未观测到 GLES3 runtime；不能执行 GPU GLES3 平台基线。",
        }
    return {
        "status": "ready",
        "recipe": {
            "recipe_id": "gpu_gles3_rgba_brightness_smoke/v1",
            "recipe_scope": "platform_baseline_preflight",
            "registered_runner": str(GPU_BRIGHTNESS_RUNNER),
            "runner_sha256": sha256_file(runner_path),
            "baseline_reference_source": str(GPU_BRIGHTNESS_SAMPLE),
            "validation_kind": "functional_gpu_pipeline_baseline",
            "execution_scope_zh": "在设备上执行固定、已审查的 RGBA 亮度 GLES3 基线，验证 EGL/GLES3 context、shader 与固定像素比较；不编译或替换当前热点代码。",
            "does_not_prove_zh": [
                "不会自动验证当前热点已经移植、编译或下发到 GPU",
                "不测量当前热点的端到端加速比、功耗或生产数据搬运成本",
                "固定 readback 只用于平台基线的结果校验，不代表生产数据路径",
            ],
            "remote_cleanup_path": GPU_BRIGHTNESS_REMOTE_BINARY,
        },
        "reason_zh": "设备已观测到 GLES3 runtime，可运行固定 GPU 平台基线以补充设备侧证据。",
    }


def _npu_baseline_recipe_contract(workspace_root: Path, probe: dict[str, Any]) -> dict[str, Any]:
    """Return the reviewed NNRT MatMul baseline without matching hotspot text."""

    app_root = workspace_root
    sample_path = app_root / NPU_MATMUL_SAMPLE
    runner_path = app_root / NPU_MATMUL_RUNNER
    if not sample_path.is_file() or not runner_path.is_file():
        return {
            "status": "unavailable",
            "recipe": None,
            "reason_zh": "已登记的 NNRT MatMul 平台基线配方文件在当前工作区不可用。",
        }
    observed_units = probe.get("observed_units") if isinstance(probe.get("observed_units"), dict) else {}
    npu = observed_units.get("npu") if isinstance(observed_units.get("npu"), dict) else {}
    if npu.get("observation_status") != "observed":
        return {
            "status": "evidence_required",
            "recipe": None,
            "reason_zh": "设备探测未观测到公开 NNRT runtime；不能执行 NNRT MatMul 平台基线。",
        }
    return {
        "status": "ready",
        "recipe": {
            "recipe_id": "npu_nnrt_matmul_capability_preflight/v1",
            "recipe_scope": "platform_baseline_preflight",
            "registered_runner": str(NPU_MATMUL_RUNNER),
            "runner_sha256": sha256_file(runner_path),
            "baseline_reference_source": str(NPU_MATMUL_SAMPLE),
            "validation_kind": "npu_nnrt_accelerator_capability_baseline",
            "execution_scope_zh": "通过公开 NNRT API 枚举 OH_NN_ACCELERATOR，并运行固定 2x3 x 3x2 FLOAT32 MatMul 基线的 Finish、可用性、编译、执行与固定 CPU 结果比较；不编译或替换当前热点代码。",
            "does_not_prove_zh": [
                "不会把当前热点自动改写、编译或下发到 NPU",
                "若基线算子不可用，只形成该固定图的不支持证据，不会伪称 NPU 卸载成功",
                "即使通过，也不测量当前热点的端到端加速比、功耗、批量阈值或生产数据搬运成本",
            ],
            "remote_cleanup_path": NPU_MATMUL_REMOTE_BINARY,
        },
        "reason_zh": "设备已观测到公开 NNRT runtime，可运行固定 NNRT MatMul 平台基线以补充设备侧证据。",
    }


def _gpu_recipe_contract(workspace_root: Path, code_unit: dict[str, Any], probe: dict[str, Any]) -> dict[str, Any]:
    app_root = workspace_root
    sample_path = app_root / GPU_BRIGHTNESS_SAMPLE
    runner_path = app_root / GPU_BRIGHTNESS_RUNNER
    if not sample_path.is_file() or not runner_path.is_file():
        return {
            "status": "unavailable",
            "recipe": None,
            "reason_zh": "已登记的 GPU 验证配方文件在当前工作区不可用。",
        }
    baseline = _gpu_baseline_recipe_contract(workspace_root, probe)
    if baseline["status"] != "ready":
        return baseline
    sample_sha256 = sha256_file(sample_path)
    match_level = _reference_source_match_level(sample_path, code_unit)
    if match_level is None:
        return {
            "status": "evidence_required",
            "recipe": None,
            "reason_zh": "当前热点不是已登记 GPU RGBA 亮度参考实现的精确源码内容。V1 不会把任意热点代码自动改写、编译或下发到设备。",
            "required_next_steps": [
                "为该热点建立经过代码审查的 GPU adapter、输入生成器与 CPU/GPU 结果比较器",
                "固定图像格式、尺寸、数据所有权、同步与回退合同",
                "在 adapter 通过功能等价性后，再增加端到端性能与功耗基准",
            ],
        }
    recipe = dict(baseline["recipe"])
    recipe.update(
        {
            "recipe_scope": "target_reference_validation",
            "reference_source": str(GPU_BRIGHTNESS_SAMPLE),
            "reference_source_sha256": sample_sha256,
            "reference_match_level": match_level,
            "validation_kind": "functional_gpu_pipeline_reference_validation",
            "execution_scope_zh": "当前热点与已登记 RGBA 亮度参考实现匹配；在设备上创建离屏 EGL/GLES3 context，执行固定 fragment shader 并比较固定像素。",
        }
    )
    return {
        "status": "ready",
        "recipe": recipe,
        "reason_zh": f"当前代码与已登记 GPU 参考实现{_reference_match_description(match_level)}，可执行该参考实现的受控功能验证。",
    }


def _npu_recipe_contract(workspace_root: Path, code_unit: dict[str, Any], probe: dict[str, Any]) -> dict[str, Any]:
    """Return a bounded NNRT capability preflight for the exact MatMul sample.

    This is intentionally a *capability preflight*, not a claim that NPU
    offload is already available. The runner itself supplies the stronger
    evidence: selected OH_NN_ACCELERATOR, graph finalization, per-model
    operation availability, compilation, execution, and result comparison.
    """

    app_root = workspace_root
    sample_path = app_root / NPU_MATMUL_SAMPLE
    runner_path = app_root / NPU_MATMUL_RUNNER
    if not sample_path.is_file() or not runner_path.is_file():
        return {
            "status": "unavailable",
            "recipe": None,
            "reason_zh": "已登记的 NNRT MatMul 能力预检配方文件在当前工作区不可用。",
        }
    baseline = _npu_baseline_recipe_contract(workspace_root, probe)
    if baseline["status"] != "ready":
        return baseline
    sample_sha256 = sha256_file(sample_path)
    match_level = _reference_source_match_level(sample_path, code_unit)
    if match_level is None:
        return {
            "status": "evidence_required",
            "recipe": None,
            "reason_zh": "当前热点不是已登记 NNRT MatMul 参考实现的精确源码内容。平台不会把任意循环自动改写、编译或下发到 NPU。",
            "required_next_steps": [
                "先把目标子图约束为确定的 shape、dtype、layout、输入所有权和数值语义。",
                "为目标热点建立经审查的 NNRT adapter、CPU fallback 与结果比较器。",
                "adapter 通过功能等价性后，再为该精确子图登记真机预检和性能基准。",
            ],
        }
    recipe = dict(baseline["recipe"])
    recipe.update(
        {
            "recipe_scope": "target_reference_validation",
            "reference_source": str(NPU_MATMUL_SAMPLE),
            "reference_source_sha256": sample_sha256,
            "reference_match_level": match_level,
            "validation_kind": "npu_nnrt_accelerator_reference_validation",
            "execution_scope_zh": "当前热点与已登记 MatMul 参考实现匹配；通过公开 NNRT API 枚举 OH_NN_ACCELERATOR，构建 2x3 x 3x2 FLOAT32 MatMul 图，查询可用性并在可用时编译、运行及比较固定 CPU 结果。",
        }
    )
    return {
        "status": "ready",
        "recipe": recipe,
        "reason_zh": f"当前代码与已登记 NNRT MatMul 参考实现{_reference_match_description(match_level)}；可执行该参考图的受控能力验证。",
    }


def _baseline_recipe_for_unit(workspace_root: Path, unit_id: str, probe: dict[str, Any]) -> dict[str, Any]:
    if unit_id == "gpu":
        return _gpu_baseline_recipe_contract(workspace_root, probe)
    if unit_id == "npu":
        return _npu_baseline_recipe_contract(workspace_root, probe)
    observation = (
        probe.get("observed_units", {}).get(unit_id, {})
        if isinstance(probe.get("observed_units"), dict)
        else {}
    )
    return {
        "status": "evidence_required",
        "recipe": None,
        "reason_zh": f"{unit_id.upper()} 尚未登记可运行的平台基线配方。",
        "required_next_steps": [
            str(observation.get("recommended_next_step_zh") or "补足该单元的公开运行时合同与受控基线配方。"),
            "模型语义判断可以保留为目标适配方向，但不能自动生成或下发任意设备程序。",
        ],
    }


def _dispatch_assessment_summary(assessment_artifact: dict[str, Any]) -> dict[str, Any]:
    result = assessment_artifact.get("result") if isinstance(assessment_artifact.get("result"), dict) else {}
    return {
        "artifact_id": assessment_artifact.get("artifact_id"),
        "decision": result.get("decision"),
        "target_unit": result.get("target_unit"),
        "execution_mode": result.get("execution_mode"),
        "summary": result.get("summary"),
        "validation_status": (
            result.get("validation", {}).get("status")
            if isinstance(result.get("validation"), dict)
            else "unknown"
        ),
    }


def _model_dispatch_recipe_contract(
    workspace_root: Path,
    *,
    probe: dict[str, Any],
    requested_unit: str,
    assessment_artifact: dict[str, Any],
) -> dict[str, Any]:
    """Turn a model semantic judgement into a bounded validation plan.

    This is intentionally not a source matcher.  A valid model contract can
    produce a *platform baseline* preflight for GPU/NPU even when the hotspot
    source is entirely new.  The baseline is evidence about the device path;
    it never claims to have executed the hotspot until a reviewed adapter is
    registered separately.
    """

    result = assessment_artifact.get("result") if isinstance(assessment_artifact.get("result"), dict) else {}
    validation = result.get("validation") if isinstance(result.get("validation"), dict) else {}
    summary = _dispatch_assessment_summary(assessment_artifact)
    if validation.get("status") != "valid":
        return {
            "status": "evidence_required",
            "recipe": None,
            "dispatch_assessment": summary,
            "reason_zh": "模型已返回下发判断，但其结构或证据引用未通过校验；请重新生成合同后再建立真机方案。",
            "required_next_steps": [
                *[str(item) for item in validation.get("errors", []) if isinstance(item, str)],
                "重新让模型根据当前热点、策略证据和设备观察形成完整下发合同。",
            ],
        }
    decision = str(result.get("decision") or "")
    target_unit = str(result.get("target_unit") or "")
    if decision != "semantic_candidate" or target_unit != requested_unit:
        meaning = {
            "not_suitable": "模型认为当前可见核心不适合下发到所选单元。",
            "needs_contract": "模型认为可见代码仍不足以形成完整下发合同。",
        }.get(decision, "模型没有形成可用于下发的语义候选。")
        incomplete_contract = result.get("dispatch_contract") if isinstance(result.get("dispatch_contract"), dict) else {}
        return {
            "status": "evidence_required",
            "recipe": None,
            "dispatch_assessment": summary,
            "reason_zh": meaning,
            "required_next_steps": [
                *[str(item) for item in result.get("required_device_checks", []) if isinstance(item, str)],
                *[str(item) for item in incomplete_contract.get("required_adapter_work", []) if isinstance(item, str)],
                "可切换硬件单元重新请求模型判断，或补充热点的最小计算核心与输入输出语义。",
            ],
        }

    baseline = _baseline_recipe_for_unit(workspace_root, requested_unit, probe)
    if baseline["status"] != "ready":
        return {
            "status": "evidence_required",
            "recipe": None,
            "dispatch_assessment": summary,
            "reason_zh": f"模型已把当前热点判断为 {requested_unit.upper()} 的语义下发候选，但当前设备/平台缺少可执行的受控基线。{baseline['reason_zh']}",
            "required_next_steps": [
                *[str(item) for item in result.get("required_device_checks", []) if isinstance(item, str)],
                *[str(item) for item in baseline.get("required_next_steps", []) if isinstance(item, str)],
                "为目标代码建立审查后的 adapter、CPU fallback 与结果比较器后，再登记目标实测配方。",
            ],
        }

    recipe = dict(baseline["recipe"])
    recipe["model_dispatch_assessment_artifact_id"] = assessment_artifact.get("artifact_id")
    recipe["model_execution_mode"] = result.get("execution_mode")
    recipe["target_dispatch_status"] = "semantic_candidate_needs_adapter"
    contract = result.get("dispatch_contract") if isinstance(result.get("dispatch_contract"), dict) else {}
    return {
        "status": "ready",
        "recipe": recipe,
        "dispatch_assessment": summary,
        "reason_zh": (
            f"模型已将当前热点判断为 {requested_unit.upper()} 的语义下发候选（{str(result.get('execution_mode') or '未声明路径')}）。"
            "当前可运行的是平台基线预检，用于验证设备侧固定路径；目标热点仍需基于下发合同实现和审查 adapter，绝不自动改写或下发。"
        ),
        "required_next_steps": [
            *[str(item) for item in contract.get("required_adapter_work", []) if isinstance(item, str)],
            *[str(item) for item in result.get("required_device_checks", []) if isinstance(item, str)],
            "平台基线通过后，仍需实现目标 adapter、CPU fallback 与结果比较器，才能登记目标实测。",
        ],
    }


def _model_dispatch_required_contract(requested_unit: str) -> dict[str, Any]:
    return {
        "status": "evidence_required",
        "recipe": None,
        "reason_zh": (
            f"尚未生成 {requested_unit.upper()} 的模型下发判断。参考样例只用于平台基线，不再用源码匹配决定当前热点能否下发。"
        ),
        "required_next_steps": [
            "先点击“模型判断下发可行性”，让模型基于当前代码、策略证据、硬件/API 上下文与设备观察形成下发合同。",
            "若模型形成语义候选，平台会把固定 GPU/NNRT 样例作为设备基线预检，而不是把它伪装为当前热点的执行。",
        ],
    }


def build_validation_plan(
    workspace_root: Path,
    *,
    probe_artifact: dict[str, Any],
    code_artifact: dict[str, Any],
    reasoning_result_artifact: dict[str, Any],
    requested_unit: str,
    dispatch_assessment_artifact: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an immutable plan; this function never touches the device."""

    unit_id = requested_unit.casefold().strip()
    if unit_id not in DEVICE_UNITS:
        raise PlatformError("DEVICE_VALIDATION_UNIT_INVALID", "不支持的真机验证硬件单元。", status_code=422)
    probe_device_info = probe_artifact.get("device") if isinstance(probe_artifact.get("device"), dict) else {}
    device_serial = _safe_serial(str(probe_device_info.get("serial") or ""))
    result = reasoning_result_artifact.get("result") if isinstance(reasoning_result_artifact.get("result"), dict) else {}
    if dispatch_assessment_artifact is not None:
        contract = _model_dispatch_recipe_contract(
            workspace_root,
            probe=probe_artifact,
            requested_unit=unit_id,
            assessment_artifact=dispatch_assessment_artifact,
        )
    elif unit_id == "gpu":
        reference_contract = _gpu_recipe_contract(workspace_root, code_artifact, probe_artifact)
        contract = reference_contract if reference_contract["status"] == "ready" else _model_dispatch_required_contract(unit_id)
    elif unit_id == "npu":
        reference_contract = _npu_recipe_contract(workspace_root, code_artifact, probe_artifact)
        contract = reference_contract if reference_contract["status"] == "ready" else _model_dispatch_required_contract(unit_id)
    else:
        observation = (
            probe_artifact.get("observed_units", {}).get(unit_id, {})
            if isinstance(probe_artifact.get("observed_units"), dict)
            else {}
        )
        contract = {
            "status": "evidence_required",
            "recipe": None,
            "reason_zh": f"{unit_id.upper()} 当前只有设备探测与方案生成能力；尚未登记可安全执行的端到端实测配方。",
            "required_next_steps": [
                str(observation.get("recommended_next_step_zh") or "补足该单元的运行时合同和可重复功能比较器。"),
                "为目标热点建立经审查的 adapter、输入/输出合同、软件 fallback 和结果比较器。",
                "通过功能等价性后，再单独设计性能与功耗 benchmark。",
            ],
        }

    ready = contract["status"] == "ready"
    reasoning_summary = {
        "strategy_id": reasoning_result_artifact.get("strategy_id"),
        "recommendation": result.get("recommendation"),
        "evidence_status": result.get("evidence_status"),
        "confidence": result.get("confidence"),
    }
    return {
        "schema_version": "device_validation_plan/v1",
        "plan_status": "ready_for_user_confirmation" if ready else "evidence_required",
        "requested_unit": unit_id,
        "device": {
            "serial": device_serial,
            "model": probe_device_info.get("model"),
            "chipset": probe_device_info.get("chipset"),
            "os_full_name": probe_device_info.get("os_full_name"),
        },
        "analysis_link": {
            "hotspot_code_artifact_id": code_artifact.get("artifact_id"),
            "reasoning_result_artifact_id": reasoning_result_artifact.get("artifact_id"),
            "reasoning_summary": reasoning_summary,
        },
        "probe_artifact_id": probe_artifact.get("artifact_id"),
        "recipe_contract": contract.get("recipe"),
        "dispatch_assessment": contract.get("dispatch_assessment"),
        "planning_reason_zh": contract["reason_zh"],
        "required_next_steps": contract.get("required_next_steps", []),
        "confirmation": {
            "required": ready,
            "mechanism": "页面中的显式用户勾选与一次性确认 nonce",
            # The nonce is intentionally never sent to the model or copied to
            # browser history. It binds a plan response to one UI confirmation.
            "nonce": secrets.token_urlsafe(24) if ready else None,
        },
        "claim_boundary_zh": "生成实测方案不改变策略推理的 recommendation。即使后续配方通过，也只能增加该配方范围内的设备与功能证据，不能自动证明热点移植成功或性能收益。",
    }


def _run_registered_gpu_recipe(
    workspace_root: Path,
    state_root: Path,
    hdc_binary: Path,
    serial: str,
    plan_artifact_id: str,
) -> subprocess.CompletedProcess[str]:
    app_root = workspace_root
    runner = app_root / GPU_BRIGHTNESS_RUNNER
    if not runner.is_file():
        raise PlatformError("DEVICE_VALIDATION_RECIPE_UNAVAILABLE", "GPU 验证配方的执行脚本不可用。", status_code=503)
    safe_plan_id = re.sub(r"[^a-z0-9_]", "_", plan_artifact_id.casefold())
    run_root = state_root / "device-validation" / safe_plan_id
    environment = dict(os.environ)
    environment.update(
        {
            "HDC_BIN": str(hdc_binary),
            "HDC_TARGET": _safe_serial(serial),
            "DEVICE_LIB_DIR": str(run_root / "device-gpu-libs"),
            "OUT_DIR": str(run_root / "out"),
        }
    )
    return _run_process(
        ("bash", str(runner)),
        timeout_seconds=DEVICE_VALIDATION_TIMEOUT_SECONDS,
        cwd=app_root,
        env=environment,
    )


def _run_registered_npu_recipe(
    workspace_root: Path,
    state_root: Path,
    hdc_binary: Path,
    serial: str,
    plan_artifact_id: str,
) -> subprocess.CompletedProcess[str]:
    app_root = workspace_root
    runner = app_root / NPU_MATMUL_RUNNER
    if not runner.is_file():
        raise PlatformError("DEVICE_VALIDATION_RECIPE_UNAVAILABLE", "NNRT MatMul 预检配方的执行脚本不可用。", status_code=503)
    safe_plan_id = re.sub(r"[^a-z0-9_]", "_", plan_artifact_id.casefold())
    run_root = state_root / "device-validation" / safe_plan_id
    environment = dict(os.environ)
    environment.update(
        {
            "HDC_BIN": str(hdc_binary),
            "HDC_TARGET": _safe_serial(serial),
            "DEVICE_LIB_DIR": str(run_root / "device-nnrt-libs"),
            "OUT_DIR": str(run_root / "out"),
        }
    )
    return _run_process(
        ("bash", str(runner)),
        timeout_seconds=DEVICE_VALIDATION_TIMEOUT_SECONDS,
        cwd=app_root,
        env=environment,
    )


def _cleanup_gpu_recipe(hdc_binary: Path, serial: str) -> dict[str, Any]:
    completed = _run_hdc(
        hdc_binary,
        ("shell", "rm", "-f", GPU_BRIGHTNESS_REMOTE_BINARY),
        serial=serial,
        require_success=False,
    )
    return {
        "attempted": True,
        "succeeded": completed.returncode == 0,
        "path": GPU_BRIGHTNESS_REMOTE_BINARY,
        "stdout": _bounded(_clean_output(completed.stdout), 2048),
        "stderr": _bounded(_clean_output(completed.stderr), 2048),
    }


def _cleanup_npu_recipe(hdc_binary: Path, serial: str) -> dict[str, Any]:
    completed = _run_hdc(
        hdc_binary,
        ("shell", "rm", "-f", NPU_MATMUL_REMOTE_BINARY),
        serial=serial,
        require_success=False,
    )
    return {
        "attempted": True,
        "succeeded": completed.returncode == 0,
        "path": NPU_MATMUL_REMOTE_BINARY,
        "stdout": _bounded(_clean_output(completed.stdout), 2048),
        "stderr": _bounded(_clean_output(completed.stderr), 2048),
    }


def _gpu_run_observations(stdout: str) -> dict[str, Any]:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    renderer = ""
    version = ""
    for index, line in enumerate(lines):
        if line == "GPU offload renderer:" and index + 1 < len(lines):
            renderer = lines[index + 1]
        if line == "GPU API version:" and index + 1 < len(lines):
            version = lines[index + 1]
    return {
        "pass_marker_observed": "GPU RGBA brightness offload: PASS" in stdout,
        "renderer": renderer or None,
        "api_version": version or None,
    }


def _npu_run_observations(stdout: str) -> dict[str, Any]:
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]
    accelerator_name = ""
    for index, line in enumerate(lines):
        if line == "NPU NNRT selected accelerator:" and index + 1 < len(lines):
            accelerator_name = lines[index + 1]
            break
    availability = "unknown"
    if "NPU NNRT MatMul availability: PASS" in stdout:
        availability = "supported"
    elif "NPU NNRT MatMul availability: NOT_SUPPORTED" in stdout:
        availability = "not_supported"
    return {
        "pass_marker_observed": "NPU NNRT MatMul accelerator offload: PASS" in stdout,
        "device_enumeration_passed": "NPU NNRT device enumeration: PASS" in stdout,
        "graph_finish_passed": "NPU NNRT graph: FINISH PASS" in stdout,
        "operation_availability": availability,
        "compilation_passed": "NPU NNRT compilation: PASS" in stdout,
        "execution_passed": "NPU NNRT execution: PASS" in stdout,
        "result_comparison_passed": "NPU NNRT result comparison: PASS" in stdout,
        "accelerator_name": accelerator_name or None,
    }


def execute_validation_plan(
    workspace_root: Path,
    state_root: Path,
    *,
    plan_artifact: dict[str, Any],
    user_confirmed: bool,
    confirmation_nonce: str,
) -> dict[str, Any]:
    """Execute one registered recipe after the UI's explicit confirmation."""

    if plan_artifact.get("plan_status") != "ready_for_user_confirmation":
        raise PlatformError(
            "DEVICE_VALIDATION_PLAN_NOT_EXECUTABLE",
            "当前真机验证方案尚未具备可执行配方；请先补足方案列出的证据。",
            status_code=422,
        )
    confirmation = plan_artifact.get("confirmation") if isinstance(plan_artifact.get("confirmation"), dict) else {}
    expected_nonce = confirmation.get("nonce") if isinstance(confirmation.get("nonce"), str) else ""
    if not user_confirmed or not expected_nonce or not secrets.compare_digest(expected_nonce, confirmation_nonce):
        raise PlatformError(
            "DEVICE_VALIDATION_CONFIRMATION_REQUIRED",
            "执行真机验证前必须在页面中明确确认本次受控配方。",
            status_code=422,
        )
    recipe = plan_artifact.get("recipe_contract") if isinstance(plan_artifact.get("recipe_contract"), dict) else {}
    recipe_id = str(recipe.get("recipe_id") or "")
    serial = _safe_serial(str((plan_artifact.get("device") or {}).get("serial") or ""))
    hdc_binary = resolve_hdc_binary(workspace_root)
    if serial not in _available_devices(hdc_binary):
        raise PlatformError("DEVICE_NOT_CONNECTED", "执行前设备已断开 HDC 连接。", status_code=409, details={"device_serial": serial})

    started = time.monotonic()
    timeout = False
    try:
        if recipe_id == "gpu_gles3_rgba_brightness_smoke/v1":
            completed = _run_registered_gpu_recipe(workspace_root, state_root, hdc_binary, serial, str(plan_artifact.get("artifact_id") or "plan"))
        elif recipe_id == "npu_nnrt_matmul_capability_preflight/v1":
            completed = _run_registered_npu_recipe(workspace_root, state_root, hdc_binary, serial, str(plan_artifact.get("artifact_id") or "plan"))
        else:
            raise PlatformError("DEVICE_VALIDATION_RECIPE_UNAVAILABLE", "该真机方案没有已登记的可执行配方。", status_code=422)
        stdout = _bounded(completed.stdout)
        stderr = _bounded(completed.stderr)
        return_code = completed.returncode
    except PlatformError as exc:
        if exc.code != "DEVICE_COMMAND_TIMEOUT":
            raise
        timeout = True
        details = exc.details if isinstance(exc.details, dict) else {}
        stdout = _bounded(str(details.get("stdout") or ""))
        stderr = _bounded(str(details.get("stderr") or ""))
        return_code = None
    except subprocess.TimeoutExpired as exc:
        timeout = True
        stdout = _bounded(_as_text(exc.stdout))
        stderr = _bounded(_as_text(exc.stderr))
        return_code = None
    finally:
        if recipe_id == "gpu_gles3_rgba_brightness_smoke/v1":
            cleanup = _cleanup_gpu_recipe(hdc_binary, serial)
        elif recipe_id == "npu_nnrt_matmul_capability_preflight/v1":
            cleanup = _cleanup_npu_recipe(hdc_binary, serial)
        else:
            cleanup = {"attempted": False}

    duration_ms = round((time.monotonic() - started) * 1000, 3)
    gpu_observations = _gpu_run_observations(stdout) if recipe_id == "gpu_gles3_rgba_brightness_smoke/v1" else {}
    npu_observations = _npu_run_observations(stdout) if recipe_id == "npu_nnrt_matmul_capability_preflight/v1" else {}
    if recipe_id == "gpu_gles3_rgba_brightness_smoke/v1":
        passed = not timeout and return_code == 0 and gpu_observations.get("pass_marker_observed") is True
        status = "passed" if passed else "failed"
        observations = gpu_observations
        summary = (
            "已在连接设备上完成已登记 GPU GLES3 RGBA 亮度配方的功能验证。"
            if passed
            else "已执行受控真机配方，但未通过功能验证；请查看运行输出与清理状态。"
        )
        calibration = {
            "adds": [
                "此设备在本次运行中能够创建该配方所需的 EGL/GLES3 context",
                "该参考配方的固定 RGBA 输入输出比较通过" if passed else "未形成通过的参考配方功能证据",
            ],
            "does_not_add": [
                "当前业务热点已被自动移植或卸载到 GPU",
                "端到端性能、功耗或生产数据搬运收益",
                "其他 GPU API、DSP、NPU 或媒体单元的可用性",
            ],
            "recommendation_effect": "不会自动提升原策略推理的 recommendation、confidence 或 evidence_status。",
        }
    else:
        passed = not timeout and return_code == 0 and npu_observations.get("pass_marker_observed") is True
        unsupported = not timeout and npu_observations.get("operation_availability") == "not_supported"
        status = "passed" if passed else "unsupported" if unsupported else "failed"
        observations = npu_observations
        summary = (
            "已在连接设备上完成已登记 NNRT MatMul 预检，并在选定加速器上通过编译、执行与固定 CPU 结果比较。"
            if passed
            else "已在连接设备上确认：公开 NNRT 可以枚举专用加速器并完成该图构建，但选定加速器明确拒绝该精确 FLOAT32 MatMul 图。"
            if unsupported
            else "已执行受控 NNRT MatMul 预检，但未形成可用性结论；请查看运行输出与清理状态。"
        )
        calibration = {
            "adds": [
                "本次运行中公开 NNRT 设备枚举已成功" if npu_observations.get("device_enumeration_passed") else "未形成 NNRT 设备枚举成功证据",
                "本次 MatMul 图已完成 Finish" if npu_observations.get("graph_finish_passed") else "未形成 MatMul 图 Finish 成功证据",
                "该精确 MatMul 图被选定加速器明确拒绝" if unsupported else "该精确 MatMul 图在选定加速器上的可用性未确认",
                "该精确图在选定加速器上执行且固定结果比较通过" if passed else "未形成该精确图在选定加速器上的执行通过证据",
            ],
            "does_not_add": [
                "任意 MatMul、任意 NPU API 或任意业务热点都可在该设备上卸载",
                "当前业务热点已被自动移植到 NPU",
                "端到端性能、功耗、批量阈值或生产数据搬运收益",
            ],
            "recommendation_effect": "不会自动提升原策略推理的 recommendation、confidence 或 evidence_status。",
        }
    return {
        "schema_version": "device_validation_run/v1",
        "plan_artifact_id": plan_artifact.get("artifact_id"),
        "probe_artifact_id": plan_artifact.get("probe_artifact_id"),
        "recipe_id": recipe_id,
        "requested_unit": plan_artifact.get("requested_unit"),
        "device": {"serial": serial},
        "status": status,
        "execution": {
            "duration_ms": duration_ms,
            "return_code": return_code,
            "timed_out": timeout,
            "stdout": stdout,
            "stderr": stderr,
        },
        "cleanup": cleanup,
        "observed_evidence": observations,
        "summary_zh": summary,
        "evidence_calibration": calibration,
    }
