"""Resolve the device snapshot selected for a page-assistant capture task."""
from __future__ import annotations

import json
import re
from typing import Any

from .core import PlatformError


def _record(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except ValueError:
            pass
    return {}


def capture_target(context: dict[str, Any]) -> tuple[dict[str, str] | None, bool]:
    """Task snapshots precede current page state; a missing choice stays missing."""
    if context.get("scenario_capture_selection_required") is True:
        return None, True
    focused = _record(context.get("focused_task"))
    request = _record(focused.get("request_context"))
    page = _record(context.get("page_context"))
    candidates = []
    if focused.get("kind") == "scenario_capture":
        if request and not any(key in request for key in ("scenario_capture_target", "selected_device", "device_serial")):
            return None, True
        candidates.append(request)
    if isinstance(context.get("scenario_capture_target"), dict):
        candidates.append({"selected_device": context["scenario_capture_target"]})
    if page.get("active_view") == "scenario_capture" or focused.get("kind") == "scenario_capture":
        candidates.append(_record(page.get("scenario_capture")))
    for candidate in candidates:
        if "scenario_capture_target" in candidate:
            target = _record(candidate["scenario_capture_target"])
        elif "selected_device" in candidate:
            target = _record(candidate["selected_device"])
        elif candidate.get("device_serial"):
            target = candidate
        else:
            continue
        mode = target.get("connection_mode")
        bridge = target.get("bridge_id") or ""
        serial = target.get("device_serial") or target.get("serial")
        if (mode in {"local", "bridge"} and isinstance(serial, str)
                and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", serial)
                and isinstance(bridge, str) and (mode == "local" or re.fullmatch(r"[A-Za-z0-9_-]{1,160}", bridge))):
            return {"connection_mode": mode, "bridge_id": bridge if mode == "bridge" else "",
                    "device_serial": serial}, False
        return None, True
    return None, context.get("scenario_capture_selection_required") is True


def bind_capture_target(action_id: str, payload: dict[str, Any], run: dict[str, Any], *, logical_device_id) -> tuple[dict[str, Any], dict[str, Any]]:
    """Bind omitted fields and reject conflicting routes before device I/O."""
    actions = {"scenario_capture.execute", "scenario_capture.discover_devices", "device.discover", "device.probe",
               "device.execute_shell", "device.collect_artifact", "device.deploy_artifact", "device.create_lease", "device.cancel_job"}
    if action_id not in actions:
        return payload, run
    context = _record(run.get("context"))
    target, required = capture_target(context)
    if target is None:
        if required and action_id not in {"scenario_capture.discover_devices", "device.discover"}:
            raise PlatformError("SCENARIO_DEVICE_SELECTION_REQUIRED", "请在场景采集页选择本次设备，助手会沿用该设备执行。", status_code=422)
        if required and action_id == "device.discover":
            # Capture discovery is the multi-route entry point; a generic
            # discover must not silently inherit a stale assistant Bridge.
            raise PlatformError("SCENARIO_DEVICE_SELECTION_REQUIRED", "请使用 scenario_capture_discover_devices 查看采集设备并在页面选择目标。", status_code=422)
        return payload, run
    for key in ("connection_mode", "bridge_id", "device_serial"):
        supplied = payload.get(key)
        if supplied and not (key == "connection_mode" and supplied == "auto") and supplied != target[key]:
            raise PlatformError("SCENARIO_DEVICE_TARGET_MISMATCH", "助手请求的设备与本次页面选择不一致，请沿用当前选定设备重新执行。",
                                status_code=409, details={"selected_device": target, "conflicting_field": key})
    bound = {**payload, **target}
    selector = dict(target)
    if target["connection_mode"] == "bridge" and action_id.startswith("device."):
        logical = logical_device_id(target["device_serial"])
        if payload.get("logical_device_id") and payload["logical_device_id"] != logical:
            raise PlatformError("SCENARIO_DEVICE_TARGET_MISMATCH", "逻辑设备与页面选择不一致。", status_code=409,
                                details={"selected_device": target})
        bound["logical_device_id"] = selector["logical_device_id"] = logical
    elif target["connection_mode"] == "local":
        if payload.get("logical_device_id") or payload.get("device_lease_id") or payload.get("lease_id"):
            raise PlatformError("SCENARIO_DEVICE_TARGET_MISMATCH", "本次选定本机直连设备，请使用其连接路径执行。", status_code=409,
                                details={"selected_device": target})
        selector = {}
    return bound, {**run, "context": {**context, "device_selector": selector,
                                     "device_execution_mode": "remote_bridge" if target["connection_mode"] == "bridge" else "local_hdc"}}
