"""Build a portable Windows-local scenario capture package.

The package executes the fixed capture runner beside the phone.  Device work
therefore does not traverse the Device Bridge job queue; the resulting archive
is imported separately for server-side analysis.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
import secrets
from typing import Any
import zipfile

from .core import PlatformError


ROOT = Path(__file__).resolve().parent.parent
SERIAL = re.compile(r"[A-Za-z0-9_.:-]{1,160}")
RUNTIME_FILES = (
    "backend/__init__.py",
    "backend/core.py",
    "backend/platform_paths.py",
    "backend/pmu_sampler.py",
    "tools/scenario_capture/__init__.py",
    "tools/scenario_capture/collector_cleanup.py",
    "tools/scenario_capture/gallery.py",
    "tools/scenario_capture/gallery_reset.py",
    "tools/scenario_capture/pmu_bundle.py",
    "tools/scenario_capture/pmu_validation.py",
    "tools/scenario_capture/power_query.py",
    "tools/scenario_capture/runner.py",
    "tools/scenario_capture/screen_awake.py",
    "tools/scenario_capture/screen_preparation.py",
    "tools/scenario_capture/thread_pmu_sampler.c",
    "tools/scenario_capture/run_local_package.py",
)


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _platform_build() -> dict[str, Any]:
    path = ROOT / ".platform-build.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return {key: value[key] for key in ("version", "build_id", "source_revision", "source_dirty") if key in value}


def _read_runtime(relative: str) -> bytes:
    path = ROOT / relative
    if not path.is_file() or path.is_symlink():
        raise PlatformError("LOCAL_CAPTURE_RUNTIME_MISSING", f"本地采集运行时缺少文件：{relative}", status_code=503)
    return path.read_bytes()


def _read_tree(relative_root: str) -> dict[str, bytes]:
    root = ROOT / relative_root
    if not root.is_dir() or root.is_symlink():
        raise PlatformError("LOCAL_CAPTURE_PMU_BUNDLE_MISSING", "Windows 本地采集包缺少 PMU 采样器资产。", status_code=503)
    result: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise PlatformError("LOCAL_CAPTURE_PMU_BUNDLE_INVALID", "PMU 采样器资产包含不允许的链接。", status_code=503)
        if path.is_file():
            result[(Path("pmu-sampler") / path.relative_to(root)).as_posix()] = path.read_bytes()
    return result


def _readme(device_serial: str) -> bytes:
    target_description = (f"包内默认设备序列号：{device_serial}；严格使用该设备，不自动切换到其他手机。"
                          if device_serial else
                          "包内未绑定设备。执行时自动使用 Windows 本机唯一在线 HDC 设备；有多台时需显式指定。")
    example_serial = device_serial or "YOUR_DEVICE_SERIAL"
    return f"""Windows 本地场景采集包
========================

这个包在连接手机的 Windows 电脑上直接调用本机 HDC。采集阶段不经过平台
Device Bridge 作业队列，因此不会累加远端轮询和 bridge_job_deadline 延迟。

执行步骤
1. 解压整个 ZIP，并保持目录结构不变。
2. 将手机连接到这台 Windows，完成 USB 调试授权。
3. 若 run-device-bridge.ps1 正在同一台 Windows 运行，请在其窗口按 Ctrl+C
   暂停；本地采集结束后再按原命令启动，避免两个进程同时调用 HDC。
4. 在解压目录打开 PowerShell：

   Set-ExecutionPolicy -Scope Process Bypass
   .\\run-capture.ps1

{target_description}
可先运行 hdc list targets 查看 Windows 本机连接的设备。

常用参数
  .\\run-capture.ps1 -DeviceSerial "{example_serial}"
  .\\run-capture.ps1 -HdcPath "C:\\path\\to\\hdc.exe"
  .\\run-capture.ps1 -OutputRoot "D:\\AgenticCapture"

脚本优先读取 %LOCALAPPDATA%\\HeterogeneousDeviceBridge\\bridge.json 中的
hdc_path 与 Bridge Python；也可以用上述参数显式指定。完成后输出目录包含
标准 capture_<id> 目录和 capture_<id>.capture.zip。

结果登记
打开平台“Trace 管理”，点击“导入本地采集包”，选择 .capture.zip。
平台会验证清单、路径、大小和 SHA-256 后原子导入。Trace、HiLog、PMU 与
校验元数据会一起进入平台受管目录，可继续做负载特征和并行建模。

Windows 原目录会保留，便于使用本机工具分析或自行清理；平台导入的是一份
独立、可复现的分析副本。
""".encode("utf-8")


def build_local_capture_package(*, plan: dict[str, Any], project_id: str, plan_id: str,
                                device_serial: str = "", bridge_id: str = "",
                                connection_mode: str = "bridge") -> dict[str, Any]:
    if not isinstance(device_serial, str) or (device_serial and not SERIAL.fullmatch(device_serial)):
        raise PlatformError("LOCAL_CAPTURE_DEVICE_INVALID", "设备序列号无效；也可留空，在 Windows 本地识别设备。", status_code=422)
    if connection_mode not in {"local", "bridge", "auto"}:
        raise PlatformError("LOCAL_CAPTURE_CONNECTION_INVALID", "设备连接方式无效。", status_code=422)
    plan_bytes = _json_bytes(plan)
    package_id = "localcap_" + secrets.token_hex(12)
    payloads: dict[str, bytes] = {
        "plan.json": plan_bytes,
        "run-capture.ps1": _read_runtime("deploy/windows/run-local-scenario-capture.ps1"),
        "README.txt": _readme(device_serial),
        "runtime/tools/__init__.py": b"\n",
    }
    for relative in RUNTIME_FILES:
        payloads[(Path("runtime") / relative).as_posix()] = _read_runtime(relative)
    payloads.update(_read_tree("tools/scenario_capture/prebuilt/aarch64-linux-ohos"))
    files = [{"path": path, "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}
             for path, content in sorted(payloads.items())]
    metadata = {
        "schema_version": "scenario_capture_local_package/v1",
        "package_id": package_id,
        "project_id": project_id,
        "plan_id": plan_id,
        "plan_sha256": hashlib.sha256(plan_bytes).hexdigest(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "selected_target": {"connection_mode": connection_mode, "bridge_id": bridge_id, "device_serial": device_serial},
        "pmu_bundle_path": "pmu-sampler",
        "runtime_entry": "runtime/tools/scenario_capture/run_local_package.py",
        "platform_build": _platform_build(),
        "files": files,
    }
    payloads["package.json"] = _json_bytes(metadata)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path, content in sorted(payloads.items()):
            info = zipfile.ZipInfo(path, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, content)
    filename = f"{plan_id}-windows-local-{package_id[-8:]}.zip"
    return {"filename": filename, "content": stream.getvalue(), "package": metadata,
            "sha256": hashlib.sha256(stream.getvalue()).hexdigest()}
