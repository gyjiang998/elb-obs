"""Deployment capabilities shared by HTTP, Agent tools and preflight."""

from __future__ import annotations

import os
from typing import Any

from .core import PlatformError


BUILD_MODE_MESSAGE = (
    "当前部署为分析模式，服务器构建能力尚未启用。请在服务器执行 "
    "./deploy/linux/heteroctl restart --worker-only，或使用 "
    "--with-device-validation 启用完整真机部署。"
)


def _device_mode_message(mode: str) -> str:
    if mode == "worker":
        return (
            "当前部署为 Worker 模式，设备发现、租约、Bridge 与真机执行由 Desktop 本地负责。"
            "如需由服务器直接操作设备，请执行 "
            "./deploy/linux/heteroctl restart --with-device-validation。"
        )
    return (
        "当前部署为分析模式，真机验证尚未启用。请在服务器执行 "
        "./deploy/linux/heteroctl restart --with-device-validation，"
        "并根据预检提示配置工具链、SDK、运行库和设备连接。"
    )


def deployment_mode() -> str:
    mode = os.environ.get("HETERO_DEPLOYMENT_MODE", "device").strip() or "device"
    if mode not in {"device", "worker", "analysis"}:
        raise PlatformError(
            "DEPLOYMENT_MODE_INVALID",
            "HETERO_DEPLOYMENT_MODE 应为 device、worker 或 analysis。",
            status_code=503,
        )
    return mode


def deployment_capabilities() -> dict[str, Any]:
    mode = deployment_mode()
    return {
        "mode": mode,
        "build_execution_enabled": mode in {"worker", "device"},
        "build_execution_message": BUILD_MODE_MESSAGE if mode == "analysis" else "",
        "device_validation_enabled": mode == "device",
        "device_validation_message": _device_mode_message(mode) if mode != "device" else "",
    }


def require_build_execution() -> None:
    mode = deployment_mode()
    if mode == "analysis":
        raise PlatformError(
            "DEPLOYMENT_BUILD_EXECUTION_DISABLED",
            BUILD_MODE_MESSAGE,
            status_code=409,
            details={"deployment_mode": mode, "required_modes": ["worker", "device"]},
        )


def require_device_validation() -> None:
    mode = deployment_mode()
    if mode != "device":
        raise PlatformError(
            "DEPLOYMENT_DEVICE_VALIDATION_DISABLED",
            _device_mode_message(mode),
            status_code=409,
            details={"deployment_mode": mode, "required_mode": "device"},
        )


def require_action_capability(action_id: str) -> None:
    if action_id.startswith("build."):
        require_build_execution()
    elif action_id.startswith("device.") or action_id in {
        "scenario_capture.execute",
        "scenario_capture.discover_devices",
    }:
        require_device_validation()


def request_needs_build_execution(method: str, path: str) -> bool:
    if method in {"HEAD", "OPTIONS"}:
        return False
    path = path.rstrip("/")
    return path == "/v1/device-experiment-profiles" or (
        method == "POST"
        and path in {"/v1/builds", "/v1/runtime-assets/openharmony-sdk/provision"}
    )


def request_needs_device_validation(method: str, path: str) -> bool:
    if method in {"HEAD", "OPTIONS"}:
        return False
    path = path.rstrip("/")
    if (
        path == "/v1/device-verification/devices"
        or path == "/v1/device-bridges"
        or path.startswith("/v1/device-bridges/")
    ):
        return True
    if method != "POST":
        return False
    if path == "/v1/scenario-capture/devices/discover":
        return True
    if path.startswith("/v1/scenario-capture/plans/") and path.endswith("/execute"):
        return True
    if path == "/v1/device-leases":
        return True
    parts = path.split("/")
    return len(parts) >= 5 and parts[1:3] == ["v1", "projects"] and parts[4].startswith("device-")
