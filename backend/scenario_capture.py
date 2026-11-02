"""Versioned scenario plans and fixed local/Bridge selected-step capture."""
from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
import json
import math
import re
import secrets
import shutil
import socket
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from .core import ArtifactStore, PlatformError, atomic_write_json, sha256_file, utc_now
from .device_gateway import DeviceGateway, normalize_deployment_result
from .device_verification import resolve_hdc_binary
from .pmu_sampler import DEFAULT_DEVICE_PATH, LEGACY_DEVICE_PATH, platform_bundle_status, prepare_pmu_sampler
from .trace_library import trace_identity, bridge_copy_identity, server_copy_identity
from tools.scenario_capture.plan_contract import OPERATIONS, _error, gallery_steps, create_plan_payload
from tools.scenario_capture.runner import PREPARATION_SCREENSHOT_KIND, PREPARATION_SCREENSHOT_MAX_BYTES, RUNNER_VERSION, execute_plan, pmu_validation_schema

ASSET_ROOT = Path(__file__).resolve().parent.parent / "tools" / "scenario_capture"
PLAN_TYPE = "scenario_capture_plan"
MANIFEST_TYPE = "scenario_capture_manifest"
RUN_PATTERN = re.compile(r"capture_[a-f0-9]{24}")
CONNECTION_MODES = frozenset({"local", "bridge"})
SCENARIO_MAX_COLLECT_BYTES = 2 * 1024 * 1024 * 1024
AUTO_LOCAL_DISCOVERY_SECONDS = 5
AUTO_BRIDGE_DISCOVERY_SECONDS = 10
AUTO_BRIDGE_DISCOVERY_CONCURRENCY = 4
SCENARIO_BRIDGE_RESULT_GRACE_SECONDS = 3


class CaptureCancelled(RuntimeError):
    pass


class GatewayDeviceClient:
    """Registered DeviceClient that translates operations to signed Bridge jobs."""
    operation_timeout_grace_seconds = SCENARIO_BRIDGE_RESULT_GRACE_SECONDS

    def __init__(self, gateway: DeviceGateway, *, state_root: Path, project_id: str, bridge_id: str,
                 device_serial: str, run_id: str, run_dir: Path, lease_id: str = "", agent_run_id: str = "",
                 cancel_event: threading.Event | None = None) -> None:
        self.gateway, self.state_root = gateway, state_root
        self.project_id, self.bridge_id, self.serial = project_id, bridge_id, device_serial
        self.run_id, self.run_dir = run_id, run_dir
        self.agent_run_id = agent_run_id or "run_" + secrets.token_hex(12)
        self.logical_device_id = gateway.logical_device_id(device_serial)
        self.cancel_event = cancel_event
        self.lease_id = lease_id or str(gateway.create_lease(project_id=project_id, bridge_id=bridge_id,
            logical_device_id=self.logical_device_id, ttl_seconds=3600)["lease_id"])
        self.jobs: list[dict[str, Any]] = []

    def check_cancelled(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise CaptureCancelled("Scenario capture cancelled")

    def rpc(self, action: str, payload: dict[str, Any], *, timeout: int = 240, cleanup: bool = False,
            _configure_job: Callable[[dict[str, Any]], None] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
        if not cleanup:
            self.check_cancelled()
        resolution = self.gateway.ensure_active_lease(self.lease_id, project_id=self.project_id,
            bridge_id=self.bridge_id, logical_device_id=self.logical_device_id)
        self.lease_id = str(resolution["effective_lease_id"])
        job = self.gateway._bridge_job(project_id=self.project_id, agent_run_id=self.agent_run_id,
            bridge_id=self.bridge_id, lease_id=self.lease_id, logical_device_id=self.logical_device_id,
            action=action, payload={**payload, "device_serial": self.serial, "timeout_seconds": timeout},
            idempotency_key=f"{self.run_id}:{len(self.jobs)}:{action}", timeout_seconds=timeout)
        started = time.monotonic()
        record = {"job_id": job["job_id"], "action": action, "started_at": utc_now(), "status": "running",
            "timeout_seconds": timeout, "cleanup": cleanup}
        self.jobs.append(record)
        completed = threading.Event()

        def watch_cancellation() -> None:
            while not completed.wait(0.2):
                if self.cancel_event is not None and self.cancel_event.is_set():
                    try:
                        self.gateway.cancel_bridge_job(bridge_id=self.bridge_id, job_id=str(job["job_id"]))
                    except Exception:
                        # The runner's finally block also cancels its owned
                        # device shell; unknown transport state stays explicit.
                        pass
                    return

        watcher = None
        if self.cancel_event is not None and not cleanup:
            watcher = threading.Thread(target=watch_cancellation, name="scenario-device-cancel", daemon=True)
            watcher.start()
        try:
            if _configure_job is not None:
                _configure_job(job)
                self.check_cancelled()
            # The device deadline stays unchanged. Allow its timeout/exit
            # evidence a short, separate trip back through the Bridge poll.
            envelope = self.gateway.dispatch_bridge_job(job, wait_seconds=timeout + SCENARIO_BRIDGE_RESULT_GRACE_SECONDS)
            result = envelope.get("result") if isinstance(envelope.get("result"), dict) else envelope
            record.update(status=result.get("status", "unknown"), finished_at=utc_now())
            if isinstance(result.get("retained_trace"), dict):
                record["retained_trace"] = result["retained_trace"]
            if not cleanup:
                self.check_cancelled()
            if result.get("status") == "cancelled":
                raise CaptureCancelled("Scenario device operation cancelled")
            if result.get("status") != "succeeded":
                raise RuntimeError(f"Bridge {action} failed: {str(result)[:3000]}")
            return result, job
        except Exception as exc:
            if isinstance(exc, CaptureCancelled):
                status = "cancelled"
            elif isinstance(exc, PlatformError) and exc.code == "BRIDGE_JOB_TIMEOUT":
                status = "state_unknown"
            elif record["status"] == "running" and not isinstance(exc, PlatformError):
                # A broken send/response transport is not evidence that the
                # offered device command stopped or never started.
                status = "state_unknown"
            else:
                status = record["status"] if record["status"] in {"timed_out", "cancelled", "state_unknown", "failed"} else "failed"
            record.update(status=status, error=str(exc)[:2000], finished_at=utc_now())
            if isinstance(exc, PlatformError):
                record["error_code"] = exc.code
            raise
        finally:
            record["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
            completed.set()
            if watcher is not None:
                watcher.join(timeout=1)
            atomic_write_json(self.run_dir / "device_jobs.json", {"jobs": self.jobs})

    def deploy(self, local_file: Path, remote_path: str, sha256: str, *, timeout: int = 180) -> dict[str, Any]:
        def authorize(job: dict[str, Any]) -> None:
            token = self.gateway.issue_download_grant(job_id=str(job["job_id"]), bridge_id=self.bridge_id,
                path=local_file, expected_sha256=sha256)
            job["payload"]["download_url"] = f"/v1/device-bridges/jobs/{job['job_id']}/artifact?token={token}"
            bridge = self.gateway._read_registry().get("bridges", {}).get(self.bridge_id, {})
            job["signature"] = self.gateway._sign_job(str(bridge.get("bridge_secret", "")), job)

        result, job = self.rpc("device.deploy_artifact", {"artifact_sha256": sha256, "remote_path": remote_path},
            timeout=timeout, _configure_job=authorize)
        checked = normalize_deployment_result(result, expected_sha256=sha256,
            expected_remote_path=remote_path, expected_serial=self.serial)
        if checked.get("status") != "succeeded" or checked.get("deployment_verified") is not True:
            raise RuntimeError("PMU device deployment could not be verified: " + str(checked)[:2000])
        return {**checked, "job_id": job["job_id"], "executor": "registered_windows_bridge"}

    def shell(self, command: str, timeout: int = 40, *, cleanup: bool = False) -> str:
        token = "__SCENARIO_RC_" + secrets.token_hex(12)
        wrapped = f"( {command}\n); scenario_rc=$?; printf '\\n{token}%s\\n' \"$scenario_rc\""
        if len(wrapped.encode("utf-8")) > 15 * 1024:
            raise ValueError("Device command exceeds the bounded Bridge payload")
        result, _ = self.rpc("device.execute_shell", {"command": wrapped}, timeout=timeout, cleanup=cleanup)
        execution = result.get("execution", {})
        output = str(execution.get("stdout", ""))
        match = re.search(rf"(?m)^{token}(\d+)\s*$", output)
        if not match:
            raise RuntimeError("Remote shell did not return its completion marker: " + output[-1500:])
        if int(match[1]):
            raise RuntimeError(f"Remote shell exit {match[1]}: " + output[:match.start()][-2000:])
        return output[:match.start()].strip()

    def collect(self, remote: str, destination: Path, *, timeout: int = 600, cleanup: bool = True) -> dict[str, Any]:
        payload: dict[str, Any] = {"remote_path": remote}
        if destination.name == "trace.ftrace":
            relative = destination.relative_to(self.run_dir).as_posix()
            trace_id = trace_identity(self.project_id, self.run_id, relative)
            run = json.loads((self.run_dir / "run.json").read_text()) if (self.run_dir / "run.json").is_file() else {}
            ordinal = destination.parent.name.split("_", 1)[0]
            step_id = "step_" + ordinal if ordinal.isdigit() else ""
            step = next((item for item in (run.get("manifest") or {}).get("steps", [])
                         if ordinal.isdigit() and item.get("ordinal") == int(ordinal)), {})
            step_id = step.get("step_id", step_id)
            payload["retain_trace"] = {"trace_id": trace_id, "copy_id": bridge_copy_identity(self.project_id, self.bridge_id, trace_id),
                "run_id": self.run_id, "plan_id": run.get("plan_id", ""), "step_id": step_id, "step_title": step.get("title", ""),
                "name": "trace.ftrace", "relative_path": relative}
        result, job = self.rpc("device.collect_artifact", payload, timeout=timeout, cleanup=cleanup)
        digest = result.get("sha256", "")
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise RuntimeError("Bridge collection has no verified content hash")
        source = self.state_root / "device-gateway" / "bridge-collected" / f"{job['job_id']}-{digest[:16]}.bin"
        if not source.is_file() or sha256_file(source) != digest:
            raise RuntimeError("Collected file was not retained with the reported hash")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        retained = result.get("retained_trace")
        return {"job_id": job["job_id"], "sha256": digest, "size_bytes": destination.stat().st_size,
            "storage_locations": [retained["location"]] if isinstance(retained, dict) and isinstance(retained.get("location"), dict) else []}


class LocalDeviceClient:
    """Fixed runner adapter using the gateway's cancellable local HDC executor."""
    # Existing tracked HDC teardown permits three seconds to terminate and
    # three seconds each to drain stdout/stderr after the operation deadline.
    operation_timeout_grace_seconds = 9

    def __init__(self, gateway: DeviceGateway, *, device_serial: str, run_id: str,
                 run_dir: Path, agent_run_id: str = "", cancel_event: threading.Event | None = None) -> None:
        self.gateway, self.serial = gateway, device_serial
        self.run_id, self.run_dir, self.agent_run_id = run_id, run_dir, agent_run_id
        self.cancel_event = cancel_event
        # A capture can share its owning Agent Run with other work. Its HDC
        # cancellation must affect only this capture, and must not poison the
        # cleanup commands that stop collectors and release the device lock.
        self.executor_run_id = run_id + ":capture"
        self.cleanup_run_id = run_id + ":cleanup"
        self.jobs: list[dict[str, Any]] = []

    def check_cancelled(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise CaptureCancelled("Scenario capture cancelled")

    def rpc(self, action: str, operation: Callable[[str], dict[str, Any]], *, cleanup: bool = False) -> tuple[dict[str, Any], dict[str, Any]]:
        if not cleanup:
            self.check_cancelled()
        token = self.cleanup_run_id if cleanup else self.executor_run_id
        started = time.monotonic()
        record = {"job_id": "localjob_" + secrets.token_hex(12), "action": action,
            "connection_mode": "local", "executor_run_id": token, "cleanup": cleanup,
            "started_at": utc_now(), "status": "running"}
        self.jobs.append(record)
        completed = threading.Event()

        def watch_cancellation() -> None:
            while not completed.wait(0.1):
                if self.cancel_event is not None and self.cancel_event.is_set():
                    try:
                        self.gateway.local_executor.cancel_run(self.executor_run_id)
                    except Exception as exc:
                        record["cancellation_error"] = str(exc)[:1000]
                    return

        watcher = None
        if self.cancel_event is not None and not cleanup:
            watcher = threading.Thread(target=watch_cancellation, name="scenario-local-cancel", daemon=True)
            watcher.start()
        try:
            result = operation(token)
            record.update(status=result.get("status", "unknown"), finished_at=utc_now())
            if not cleanup:
                self.check_cancelled()
            if result.get("status") == "cancelled":
                raise CaptureCancelled("Scenario capture cancelled")
            if result.get("status") != "succeeded":
                raise RuntimeError(f"Local HDC {action} failed: {str(result)[:3000]}")
            return result, record
        except Exception as exc:
            cancelled = isinstance(exc, CaptureCancelled) or (isinstance(exc, PlatformError) and exc.code == "DEVICE_COMMAND_CANCELLED")
            if cancelled:
                status = "cancelled"
            elif isinstance(exc, PlatformError) and exc.code == "DEVICE_COMMAND_TIMEOUT":
                status = "timed_out"
            else:
                status = record["status"] if record["status"] in {"timed_out", "cancelled", "failed"} else "failed"
            record.update(status=status,
                error=str(exc)[:2000], finished_at=utc_now())
            if isinstance(exc, PlatformError):
                record["error_code"] = exc.code
            if cancelled and not isinstance(exc, CaptureCancelled):
                raise CaptureCancelled("Scenario capture cancelled") from exc
            raise
        finally:
            record["duration_ms"] = round((time.monotonic() - started) * 1000, 3)
            completed.set()
            if watcher is not None:
                watcher.join(timeout=1)
            atomic_write_json(self.run_dir / "device_jobs.json", {"jobs": self.jobs})

    def deploy(self, local_file: Path, remote_path: str, sha256: str, *, timeout: int = 180) -> dict[str, Any]:
        result, record = self.rpc("device.deploy_artifact", lambda token: self.gateway.local_deploy(
            serial=self.serial, local_file=local_file, artifact_sha256=sha256, remote_path=remote_path,
            timeout_seconds=timeout, agent_run_id=token))
        checked = normalize_deployment_result(result, expected_sha256=sha256,
            expected_remote_path=remote_path, expected_serial=self.serial)
        if checked.get("status") != "succeeded" or checked.get("deployment_verified") is not True:
            raise RuntimeError("PMU device deployment could not be verified: " + str(checked)[:2000])
        return {**checked, "job_id": record["job_id"], "executor": "local_hdc"}

    def shell(self, command: str, timeout: int = 40, *, cleanup: bool = False) -> str:
        marker = "__SCENARIO_RC_" + secrets.token_hex(12)
        wrapped = f"( {command}\n); scenario_rc=$?; printf '\\n{marker}%s\\n' \"$scenario_rc\""
        if len(wrapped.encode("utf-8")) > 15 * 1024:
            raise ValueError("Device command exceeds the bounded HDC payload")
        result, _ = self.rpc("device.execute_shell", lambda token: self.gateway.local_execute_shell(
            serial=self.serial, command=wrapped, timeout_seconds=timeout, agent_run_id=token), cleanup=cleanup)
        output = str(result.get("execution", {}).get("stdout", ""))
        match = re.search(rf"(?m)^{marker}(\d+)\s*$", output)
        if not match:
            raise RuntimeError("Remote shell did not return its completion marker: " + output[-1500:])
        if int(match[1]):
            raise RuntimeError(f"Remote shell exit {match[1]}: " + output[:match.start()][-2000:])
        return output[:match.start()].strip()

    def collect(self, remote: str, destination: Path, *, timeout: int = 600, cleanup: bool = True) -> dict[str, Any]:
        # Trace/layout recovery defaults to cleanup; optional failure screenshots
        # use a short timeout and the cancellable capture executor instead.
        result, record = self.rpc("device.collect_artifact", lambda token: self.gateway.local_collect(
            serial=self.serial, remote_path=remote, destination=destination, timeout_seconds=timeout,
            agent_run_id=token, max_bytes=SCENARIO_MAX_COLLECT_BYTES), cleanup=cleanup)
        digest = result.get("sha256", "")
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise RuntimeError("Local collection has no verified content hash")
        if not destination.is_file() or sha256_file(destination) != digest:
            raise RuntimeError("Collected file was not retained with the reported hash")
        return {"job_id": record["job_id"], "sha256": digest, "size_bytes": destination.stat().st_size}


def capture_failure_details(manifest: dict[str, Any]) -> dict[str, Any] | None:
    """Promote retained runner diagnostics to the run and background job."""
    errors = manifest.get("quality", {}).get("errors", [])
    error = next((item for item in errors if isinstance(item, dict) and (item.get("reason") or item.get("message"))), None)
    if error is not None:
        details = dict(error)
        details["code"] = str(error.get("code") or "SCENARIO_STEP_FAILED")
        details["message"] = str(error.get("message") or error["reason"])
        step = next((item for item in reversed(manifest.get("steps", [])) if item.get("status") in {"failed", "cancelled"}), None)
        if step:
            details.setdefault("step_id", step["step_id"])
            details.setdefault("step_title", step.get("title", step["step_id"]))
        return details
    cleanup = manifest.get("cleanup", {})
    if cleanup.get("status") == "pending":
        return {"code": "SCENARIO_CLEANUP_FAILED", "message": str(cleanup.get("reason") or "采集结束清理未完成。"),
                "phase": "cleanup", "suggestion": "查看保留的采集器状态，完成本次清理后再采集。"}
    return None


class ScenarioCaptureManager:
    def __init__(self, state_root: Path, artifact_store: ArtifactStore, device_gateway: DeviceGateway) -> None:
        self.state_root = Path(state_root).resolve()
        self.artifact_store, self.device_gateway = artifact_store, device_gateway
        self.root = self.state_root / "scenario-capture"
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    @staticmethod
    def discovered_devices(result: dict[str, Any], *, connection_mode: str, bridge_id: str = "",
                           bridge_name: str = "") -> list[dict[str, Any]]:
        """Normalize only fresh discovery output, never registry capabilities."""
        if result.get("status") not in (None, "", "succeeded", "ready", "completed"):
            return []
        devices = result.get("devices")
        if not isinstance(devices, list):
            return []
        label = "本机 HDC 直连" if connection_mode == "local" else "Windows Bridge" + (" · " + bridge_name if bridge_name else "")
        found: dict[str, dict[str, Any]] = {}
        for device in devices:
            if not isinstance(device, dict) or device.get("connection_state") in {"offline", "disconnected", "unauthorized"}:
                continue
            serial = device.get("device_serial") or device.get("serial")
            if not isinstance(serial, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", serial):
                continue
            found[serial] = {"device_serial": serial, "serial": serial, "connection_mode": connection_mode,
                "bridge_id": bridge_id if connection_mode == "bridge" else "",
                "display_name": str(device.get("display_name") or device.get("model") or serial)[:160],
                "connection_label": label}
        return list(found.values())

    @staticmethod
    def _discovery_response(devices: list[dict[str, Any]], diagnostics: list[dict[str, Any]], *,
                            connection_mode: str, bridge_id: str = "", raw: dict[str, Any] | None = None) -> dict[str, Any]:
        complete = all(item["status"] in {"ready", "empty", "offline", "unavailable"} for item in diagnostics)
        status = ("selection_required" if devices and (len(devices) > 1 or not complete) else
                  "ready" if devices else "unavailable" if complete else "discovery_incomplete")
        messages = {"ready": "已确认设备及连接方式。", "selection_required": "发现多个可用设备连接，请选择本次采集设备。",
            "unavailable": "当前连接未发现在线设备。", "discovery_incomplete": "当前连接尚未确认，请重试；未将探测失败判为设备离线。"}
        if devices and not complete:
            messages["selection_required"] = "已发现在线设备，其他连接尚未确认；可明确选择已确认的设备及连接方式。"
        return {**(raw or {}), "schema_version": "scenario_capture_devices/v1", "connection_mode": connection_mode,
            "bridge_id": bridge_id, "devices": devices, "selected_device": devices[0] if status == "ready" else None,
            "status": status, "message": messages[status], "discovery_complete": complete,
            "diagnostics": diagnostics, "observed_at": utc_now()}

    def _discover_route(self, mode: str, bridge_id: str = "", bridge_name: str = "", *, timeout: int) -> dict[str, Any]:
        """One fresh target-list request; properties and other routes are independent."""
        diagnostic: dict[str, Any] = {"connection_mode": mode, "bridge_id": bridge_id, "device_count": 0}
        result: dict[str, Any] = {}
        devices: list[dict[str, Any]] = []
        try:
            if mode == "local":
                result = self.device_gateway.local_discover(timeout_seconds=timeout)
            else:
                job = self.device_gateway._bridge_job(project_id=self.artifact_store.project_id,
                    agent_run_id="run_" + secrets.token_hex(12), bridge_id=bridge_id, lease_id="",
                    logical_device_id="device_discovery", action="device.discover", payload={"targets_only": True},
                    idempotency_key="scenario-discover:" + secrets.token_hex(12), timeout_seconds=timeout)
                diagnostic["device_job_id"] = job["job_id"]
                envelope = self.device_gateway.dispatch_bridge_job(job, wait_seconds=timeout)
                result = envelope.get("result") if isinstance(envelope.get("result"), dict) else envelope
                result = {**result, "device_job_id": job["job_id"]}
            hdc = result.get("hdc") if isinstance(result.get("hdc"), dict) else {}
            success = result.get("status") in (None, "", "succeeded", "ready", "completed")
            success = (success and isinstance(result.get("devices"), list) and hdc.get("available", True)
                       and hdc.get("return_code") in (None, 0) and not hdc.get("timed_out") and not hdc.get("cancelled"))
            devices = self.discovered_devices(result, connection_mode=mode, bridge_id=bridge_id, bridge_name=bridge_name) if success else []
            timed_out = result.get("status") == "timed_out" or hdc.get("timed_out") is True
            diagnostic.update(status="ready" if devices else "empty" if success else "timed_out" if timed_out else "failed",
                device_count=len(devices), message="已发现在线设备。" if devices else "当前未发现在线设备。" if success else "设备发现未成功完成。")
            if not success:
                error = result.get("error") if isinstance(result.get("error"), dict) else {}
                launch_error = hdc.get("launch_error") if isinstance(hdc.get("launch_error"), dict) else {}
                diagnostic["code"] = error.get("code") or hdc.get("error_code") or launch_error.get("code") or "SCENARIO_DISCOVERY_FAILED"
                detail = error.get("message") or launch_error.get("message") or hdc.get("stderr")
                if timed_out:
                    diagnostic["message"] = "设备发现超时，尚未确认手机是否在线；请确认 Bridge 正在运行后重试。" if mode == "bridge" else "本机 HDC 设备发现超时，尚未确认手机是否在线。"
                if detail:
                    diagnostic["message"] = (diagnostic["message"] + " " + str(detail))[:500]
                # A Bridge command result keeps its failure below `hdc`, while
                # dispatch/transport errors keep it in the top-level `error`.
                # Preserve both shapes so a failed target list is diagnosable.
                diagnostic["hdc"] = {key: hdc[key] for key in ("return_code", "timed_out", "cancelled", "duration_ms") if key in hdc}
                if hdc.get("available") is False:
                    diagnostic.update(status="unavailable", code=hdc.get("error_code") or "DEVICE_HDC_UNAVAILABLE",
                                      message=str(hdc.get("diagnostic") or "此连接未配置可用 HDC。")[:500])
        except Exception as exc:
            code = getattr(exc, "code", "SCENARIO_DISCOVERY_FAILED")
            normalized_code = str(code).upper()
            status = ("offline" if code in {"BRIDGE_OFFLINE", "SCENARIO_BRIDGE_OFFLINE"} else
                      "unavailable" if code in {"HDC_NOT_FOUND", "DEVICE_HDC_UNAVAILABLE", "SCENARIO_LOCAL_HDC_UNAVAILABLE"} else
                      "timed_out" if "TIMEOUT" in normalized_code or "DEADLINE" in normalized_code else "failed")
            diagnostic.update(status=status, code=code, message=str(exc)[:500])
        return self._discovery_response(devices, [diagnostic], connection_mode=mode, bridge_id=bridge_id,
                                        raw=result if isinstance(result, dict) else {})

    def discover_auto(self) -> dict[str, Any]:
        diagnostics: list[dict[str, Any]] = []
        devices: list[dict[str, Any]] = []

        if self.catalog().get("local_hdc", {}).get("available"):
            local = self._discover_route("local", timeout=AUTO_LOCAL_DISCOVERY_SECONDS)
            diagnostics.extend(local["diagnostics"])
            devices.extend(local["devices"])
        else:
            diagnostics.append({"connection_mode": "local", "bridge_id": "", "status": "unavailable", "device_count": 0,
                "code": "SCENARIO_LOCAL_HDC_UNAVAILABLE", "message": "本机未找到 HDC，继续探测在线 Windows Bridge。"})
        try:
            registered = self.device_gateway.list_bridges(project_id=self.artifact_store.project_id)
            bridges = [entry for entry in registered if entry.get("connection_state") == "connected" and entry.get("bridge_id")]
            diagnostics.extend({"connection_mode": "bridge", "bridge_id": entry["bridge_id"], "status": "offline", "device_count": 0,
                                "message": "此 Windows Bridge 当前离线。"}
                               for entry in registered if entry.get("bridge_id") and entry.get("connection_state") == "offline")
        except Exception as exc:
            diagnostics.append({"connection_mode": "bridge", "bridge_id": "", "status": "failed", "device_count": 0,
                "code": getattr(exc, "code", "SCENARIO_BRIDGE_REGISTRY_UNAVAILABLE"), "message": str(exc)[:500]})
            return self._discovery_response(devices, diagnostics, connection_mode="auto")
        if not bridges:
            diagnostics.append({"connection_mode": "bridge", "bridge_id": "", "status": "unavailable", "device_count": 0,
                "message": "当前项目没有在线 Windows Bridge。"})
            return self._discovery_response(devices, diagnostics, connection_mode="auto")
        deadline = time.monotonic() + AUTO_BRIDGE_DISCOVERY_SECONDS

        def bridge_probe(bridge: dict[str, Any]) -> dict[str, Any]:
            remaining = deadline - time.monotonic()
            if remaining < 1:
                diagnostic = {"connection_mode": "bridge", "bridge_id": bridge["bridge_id"], "status": "not_probed", "device_count": 0,
                    "code": "SCENARIO_DISCOVERY_BUDGET_EXCEEDED", "message": "本轮设备发现时限已到，请重试或选择具体 Bridge。"}
                return self._discovery_response([], [diagnostic], connection_mode="bridge", bridge_id=bridge["bridge_id"])
            return self._discover_route("bridge", bridge["bridge_id"], str(bridge.get("display_name") or ""), timeout=math.ceil(remaining))

        # Each RPC has the remaining batch budget. Context-managed workers all
        # finish before returning; queued work skips when the budget is spent.
        with ThreadPoolExecutor(max_workers=AUTO_BRIDGE_DISCOVERY_CONCURRENCY, thread_name_prefix="scenario-discovery") as pool:
            results = list(pool.map(bridge_probe, bridges))
        for result in results:
            devices.extend(result["devices"])
            diagnostics.extend(result["diagnostics"])
        return self._discovery_response(devices, diagnostics, connection_mode="auto")

    def discover_devices(self, bridge_id: str = "", *, connection_mode: str = "bridge", timeout_seconds: int = 10,
                         targets_only: bool = True) -> dict[str, Any]:
        """Discover targets through the explicitly selected connection."""
        if connection_mode == "auto" and not bridge_id:
            return self.discover_auto()
        if connection_mode == "auto":
            connection_mode = "bridge"
        if not isinstance(connection_mode, str) or connection_mode not in CONNECTION_MODES:
            _error("connection_mode must be local or bridge")
        if connection_mode == "local":
            return self._discover_route("local", timeout=min(timeout_seconds, AUTO_LOCAL_DISCOVERY_SECONDS))
        if not bridge_id:
            _error("A registered Bridge is required for bridge mode")
        return self._discover_route("bridge", bridge_id, timeout=min(timeout_seconds, AUTO_BRIDGE_DISCOVERY_SECONDS))

    def catalog(self) -> dict[str, Any]:
        profile = json.loads((ASSET_ROOT / "gallery_17_profile.json").read_text(encoding="utf-8"))
        try:
            binary = resolve_hdc_binary(getattr(self.device_gateway, "workspace_root", ASSET_ROOT.parent.parent))
            local_hdc = {"available": True, "binary_path": str(binary)}
        except PlatformError as exc:
            local_hdc = {"available": False, "diagnostic": exc.message, "error_code": exc.code}
        return {"schema_version": "scenario_capture_catalog/v1", "templates": [{"template_id": "gallery_17_steps_v1", "name": "图库 17 步", "steps": gallery_steps()}],
            "connection_modes": [{"id": "auto", "name": "自动识别", "label": "自动识别", "requires_bridge": False},
                {"id": "local", "name": "本机 HDC 直连", "label": "本机 HDC 直连", "requires_bridge": False},
                {"id": "bridge", "name": "Windows Bridge", "label": "Windows Bridge", "requires_bridge": True}],
            "default_connection_mode": "auto",
            "host_platform": sys.platform, "local_hdc": local_hdc,
            "default_template_id": "gallery_17_steps_v1", "default_profile": profile,
            "default_capture": {"selected_step_ids": ["step_07"], "collectors": ["trace", "hilog", "pmu"], "preparation_mode": "auto",
                "pmu": {"binary_path": DEFAULT_DEVICE_PATH, "period_us": 100, "duration_ms": 10000, "group": "auto", "reader_cpu": 0}},
            "operations": sorted(OPERATIONS), "execution_policy": "through_last_selected",
            "capabilities": {"executor": "local_hdc_or_registered_windows_bridge", "executors": ["local_hdc", "registered_windows_bridge"], "trace": "runtime_probe", "hilog": "runtime_probe", "pmu": "verified_bundle_or_existing_sampler", "arbitrary_host_python": False},
            "pmu_sampler": {"display_name": "通用线程 PMU 采样器", "source_asset": "tools/scenario_capture/thread_pmu_sampler.c", "source_sha256": sha256_file(ASSET_ROOT / "thread_pmu_sampler.c"),
                "platform_bundle": platform_bundle_status(),
                "default_device_path": DEFAULT_DEVICE_PATH, "legacy_device_path": LEGACY_DEVICE_PATH,
                "scope": "target_main_thread", "user_only": True, "duration_ms_range": [100, 10000],
                "period_us_range": [10, 10000], "high_resolution_sampler_version": "1.1.0", "period_semantics": "requested_interval_actual_cadence_measured",
                "supported_modes": ["auto", "cache", "branch", "stall", "l1d", "llc", "dtlb"],
                "auto_event_groups": ["base", "cache", "branch", "stall", "l1d", "llc", "dtlb"], "auto_event_count": 14,
                "auto_sampling": "one_operation_independent_perf_groups_with_per_group_coverage",
                "multi_group_sampler_version": "1.2.0", "multi_group_buffer_limit_bytes": 128 * 1024 * 1024,
                "missing_behavior": "partial_result_with_unavailable_status", "binary_source_binding": "runtime_binary_hash_retained_no_build_equivalence_assumed"},
            "provenance": json.loads((ASSET_ROOT / "provenance.json").read_text(encoding="utf-8"))}

    def create_plan(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            _error("Plan must be an object")
        plan = create_plan_payload(payload, self.catalog())
        artifact = self.artifact_store.write(PLAN_TYPE, plan, source_connector_id=plan["source_connector_id"],
            source_binding={"match_level": "unbound", "declared_revision": plan["source_snapshot_id"] or None},
            producer={"name": "scenario-capture", "version": "1.1.0"}, schema_version="scenario_capture_plan/v1")
        return {**artifact, "plan_id": artifact["artifact_id"]}

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        value = self.artifact_store.read(PLAN_TYPE, plan_id)
        return {**value, "plan_id": value["artifact_id"]}

    def list_plans(self, limit: int = 100) -> list[dict[str, Any]]:
        return [{**value, "plan_id": value["artifact_id"]} for value in self.artifact_store.list_artifacts(PLAN_TYPE, limit=max(1, min(int(limit), 500)))]

    def generate_script(self, plan_id: str) -> dict[str, Any]:
        plan = self.get_plan(plan_id)
        embedded = {key: plan[key] for key in ("title", "template_id", "natural_language_steps", "steps", "profile", "capture", "execution_policy", "source_connector_id", "source_snapshot_id")}
        content = '''#!/usr/bin/env python3
"""Generated selected-step automation. Execution uses the platform's selected
local HDC or Windows Bridge DeviceClient; this script calls the platform API.
Edit PLAN selectors/steps, then submit the plan to the platform and observe its
background job. The server fixed runner performs and records the actual work.
"""
import argparse
import json
import os
import time
from urllib.request import Request, urlopen

PLAN = json.loads(''' + repr(json.dumps(embedded, ensure_ascii=False, indent=2)) + ''')

def rpc(base_url, path, payload=None):
    headers = {"Content-Type": "application/json"}
    token = os.environ.get("HETERO_PLATFORM_TOKEN", "")
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    with urlopen(Request(base_url.rstrip("/") + path, data=data, headers=headers), timeout=60) as response:
        return json.load(response)

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", required=True)
    parser.add_argument("--connection-mode", choices=("auto", "local", "bridge"), default="auto")
    parser.add_argument("--bridge-id", default="")
    parser.add_argument("--device-serial", default="")
    args = parser.parse_args()
    if args.connection_mode == "bridge" and not args.bridge_id:
        parser.error("--bridge-id is required for bridge mode")
    if args.connection_mode in ("local", "bridge") and not args.device_serial:
        parser.error("--device-serial is required for an explicit connection mode")
    print(json.dumps({"steps": PLAN["steps"], "capture": PLAN["capture"]}, ensure_ascii=False, indent=2))
    plan_response = rpc(args.platform, "/v1/scenario-capture/plans", PLAN)
    plan = plan_response.get("plan", plan_response)
    plan_id = plan.get("plan_id") or plan["artifact_id"]
    job = rpc(args.platform, "/v1/scenario-capture/plans/" + plan_id + "/execute",
              {"connection_mode": args.connection_mode, "bridge_id": args.bridge_id, "device_serial": args.device_serial})
    job_id = job.get("job_id") or job["job"]["job_id"]
    while True:
        response = rpc(args.platform, "/v1/parallel-jobs/" + job_id)
        state = response.get("job", response)
        print(json.dumps(state, ensure_ascii=False), flush=True)
        if state.get("status") in {"succeeded", "completed", "failed", "cancelled", "partial"}:
            result = state.get("result") or {}
            if state.get("status") in {"failed", "cancelled"} or result.get("status") in {"failed", "cancelled"}:
                raise SystemExit(1)
            return
        time.sleep(2)

if __name__ == "__main__":
    main()
'''
        return {"filename": "capture_scenario.py", "media_type": "text/x-python", "content": content,
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(), "plan_id": plan_id}

    def generate_local_package(self, plan_id: str, *, device_serial: str = "",
                               bridge_id: str = "", connection_mode: str = "bridge") -> dict[str, Any]:
        """Freeze a saved plan with the fixed runner for direct Windows HDC execution."""
        from .scenario_capture_local_package import build_local_capture_package
        plan = self.get_plan(plan_id)
        if plan["readiness"]["blockers"]:
            raise PlatformError("SCENARIO_PLAN_NOT_READY", "请先补齐当前计划的操作与控件信息。", status_code=422,
                                details=plan["readiness"])
        return build_local_capture_package(plan=plan, project_id=self.artifact_store.project_id,
            plan_id=plan_id, device_serial=device_serial, bridge_id=bridge_id, connection_mode=connection_mode)

    @staticmethod
    def new_run_id() -> str:
        return "capture_" + secrets.token_hex(12)

    def _run_dir(self, run_id: str) -> Path:
        if not isinstance(run_id, str) or not RUN_PATTERN.fullmatch(run_id):
            raise PlatformError("SCENARIO_RUN_INVALID", "Invalid scenario run identifier", status_code=422)
        return self.root / "runs" / run_id

    def _terminal_device_run_ids(self, *, connection_mode: str, bridge_id: str, device_serial: str) -> set[str]:
        """Local terminal records are evidence for recovering pre-lease device locks."""
        result: set[str] = set()
        for path in (self.root / "runs").glob("capture_*/run.json"):
            if path.is_symlink() or path.parent.is_symlink() or not RUN_PATTERN.fullmatch(path.parent.name):
                continue
            try:
                previous = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(previous, dict):
                continue
            if (previous.get("status") in {"failed", "cancelled", "succeeded", "partial"}
                    and previous.get("project_id") == self.artifact_store.project_id
                    and previous.get("connection_mode", "bridge") == connection_mode
                    and previous.get("bridge_id", "") == bridge_id
                    and previous.get("device_serial") == device_serial
                    and previous.get("run_id") == path.parent.name):
                result.add(path.parent.name)
        return result

    def execute(self, plan_id: str, *, bridge_id: str = "", device_serial: str, connection_mode: str = "bridge", lease_id: str = "", agent_run_id: str = "",
                run_id: str | None = None, progress: Callable[[dict[str, Any]], None] | None = None,
                cancel_event: threading.Event | None = None, device_client: Any = None,
                requested_connection_mode: str | None = None) -> dict[str, Any]:
        plan = self.get_plan(plan_id)
        if plan["readiness"]["blockers"]:
            raise PlatformError("SCENARIO_PLAN_NOT_READY", "Some operations or selectors need completion", status_code=422, details=plan["readiness"])
        if not isinstance(connection_mode, str) or connection_mode not in CONNECTION_MODES:
            _error("connection_mode must be local or bridge")
        if not isinstance(device_serial, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", device_serial):
            _error("A valid device serial is required")
        if connection_mode == "bridge" and not bridge_id:
            _error("A registered Bridge is required for bridge mode")
        if connection_mode == "local":
            bridge_id = ""
        execution_provenance = {"connection_mode": connection_mode,
            "runner_version": RUNNER_VERSION, "pmu_validation_schema": pmu_validation_schema(plan["capture"]["pmu"]["group"]),
            "requested_connection_mode": requested_connection_mode or connection_mode,
            "executor": "local_hdc" if connection_mode == "local" else "registered_windows_bridge",
            "host_platform": sys.platform, "host_shell": False}
        device = {"connection_mode": connection_mode, "executor": execution_provenance["executor"],
            "bridge_id": bridge_id, "serial": device_serial}
        run_id = run_id or self.new_run_id()
        directory = self._run_dir(run_id)
        with self._lock:
            if (directory / "run.json").exists():
                raise PlatformError("SCENARIO_RUN_EXISTS", "Scenario Run already exists; use its retained result", status_code=409)
            directory.mkdir(parents=True, exist_ok=True)
            run = {"schema_version": "scenario_capture_run/v1", "run_id": run_id, "plan_id": plan_id,
                "project_id": self.artifact_store.project_id,
                "status": "running", "created_at": utc_now(), "connection_mode": connection_mode,
                "requested_connection_mode": requested_connection_mode or connection_mode,
                "bridge_id": bridge_id, "device_serial": device_serial, "device": device, "execution_provenance": execution_provenance,
                "source_connector_id": plan["source_connector_id"], "source_snapshot_id": plan["source_snapshot_id"], "manifest": None}
            atomic_write_json(directory / "run.json", run)

        def update(manifest: dict[str, Any]) -> None:
            preparation = run.get("pmu_preparation")
            if isinstance(preparation, dict):
                manifest["pmu_preparation"] = preparation
                if preparation.get("code"):
                    warnings = manifest.setdefault("quality", {}).setdefault("warnings", [])
                    if not any(item.get("source") == "pmu_preparation" for item in warnings):
                        warnings.append({"source": "pmu_preparation", "collector": "pmu",
                            "code": preparation["code"], "reason": preparation.get("message", "PMU preparation unavailable")})
                    if manifest.get("status") == "succeeded":
                        manifest["status"] = "partial"
            for entry in manifest["files"]:
                entry["managed_path"] = (directory / entry["relative_path"]).relative_to(self.state_root).as_posix()
                if entry.get("kind") == "trace":
                    location = {"copy_id": server_copy_identity(self.artifact_store.project_id, entry["managed_path"]),
                        "kind": "server", "bridge_id": "", "local_path": str(directory / entry["relative_path"]),
                        "managed_path": entry["managed_path"], "storage_host": socket.gethostname(), "connection_mode": connection_mode,
                        "label": "本机原始文件" if connection_mode == "local" else "平台建模副本", "status": "available",
                        "can_download": True, "can_delete": False, "observed_at": utc_now()}
                    entry["storage_locations"] = [item for item in entry.get("storage_locations", []) if item.get("kind") != "server"] + [location]
            manifest.update(connection_mode=connection_mode, device=device, execution_provenance=execution_provenance,
                source_connector_id=plan["source_connector_id"], source_snapshot_id=plan["source_snapshot_id"])
            atomic_write_json(directory / "manifest.json", manifest)
            run.update(status=manifest["status"], manifest=manifest, updated_at=utc_now())
            if manifest["status"] in {"failed", "cancelled"}:
                run["error"] = capture_failure_details(manifest)
            atomic_write_json(directory / "run.json", run)
            if progress:
                value = dict(run)
                latest = manifest.get("steps", [])[-1] if manifest.get("steps") else {}
                timing = latest.get("screen_preparation_timing", {})
                if latest.get("screen_handoff_timing"):
                    timing = latest["screen_handoff_timing"]
                gallery_timing = latest.get("gallery_preparation_timing", {})
                if isinstance(gallery_timing, dict) and gallery_timing.get("status") == "running":
                    timing = gallery_timing
                evidence = latest.get("screen_evidence_timing", {})
                if isinstance(evidence, dict) and evidence.get("status") == "running":
                    timing = evidence
                if latest.get("status") == "preparing" and isinstance(timing, dict):
                    for field in ("message", "phase"):
                        if isinstance(timing.get(field), str) and timing[field]:
                            value[field] = timing[field]
                collector_preparation = manifest.get("collector_preparation", {})
                if not latest and isinstance(collector_preparation, dict) and manifest.get("status") == "running":
                    value["phase"] = "collector_preparation"
                    value["message"] = str(collector_preparation.get("message") or "正在核验采集会话并清理上次采集残留")
                progress(value)

        try:
            if device_client is not None:
                client = device_client
            elif connection_mode == "local":
                client = LocalDeviceClient(self.device_gateway, device_serial=device_serial, run_id=run_id,
                    run_dir=directory, agent_run_id=agent_run_id, cancel_event=cancel_event)
            else:
                client = GatewayDeviceClient(self.device_gateway, state_root=self.state_root,
                    project_id=self.artifact_store.project_id, bridge_id=bridge_id, device_serial=device_serial,
                    run_id=run_id, run_dir=directory, lease_id=lease_id, agent_run_id=agent_run_id, cancel_event=cancel_event)

            def preparing_pmu(preparation: dict[str, Any]) -> None:
                run.update(pmu_preparation=preparation, updated_at=utc_now())
                atomic_write_json(directory / "run.json", run)
                atomic_write_json(directory / "pmu_preparation.json", preparation)
                if progress:
                    progress({**run, "phase": "pmu_preparation", "message": preparation.get("message", "")})

            execution_plan, preparation = prepare_pmu_sampler(plan, client, progress=preparing_pmu)
            run["pmu_preparation"] = preparation
            manifest = execute_plan(execution_plan, client, directory, run_id, update,
                terminal_run_ids=self._terminal_device_run_ids(connection_mode=connection_mode,
                    bridge_id=bridge_id, device_serial=device_serial))
            update(manifest)
            artifact = self.artifact_store.write(MANIFEST_TYPE, manifest, source_connector_id=plan["source_connector_id"],
                source_binding={"match_level": "unbound", "declared_revision": plan["source_snapshot_id"] or None},
                producer={"name": "scenario-capture-runner", "version": RUNNER_VERSION}, input_artifacts=["artifact:" + plan_id], schema_version="scenario_capture_manifest/v1")
            run.update(manifest_artifact_id=artifact["artifact_id"], finished_at=utc_now())
        except Exception as exc:
            run.update(status="cancelled" if isinstance(exc, CaptureCancelled) else "failed", error={"type": type(exc).__name__, "message": str(exc)[:3000]}, finished_at=utc_now())
            preparation = run.get("pmu_preparation")
            if isinstance(preparation, dict) and preparation.get("status") in {"checking", "verifying_bundle", "checking_managed", "deploying", "probing_capabilities"}:
                preparation.update(status=run["status"], message="PMU 准备已取消。" if run["status"] == "cancelled" else "PMU 准备未完成。", finished_at=utc_now())
                atomic_write_json(directory / "pmu_preparation.json", preparation)
        atomic_write_json(directory / "run.json", run)
        return run

    def get_run(self, run_id: str) -> dict[str, Any]:
        path = self._run_dir(run_id) / "run.json"
        if not path.is_file():
            raise PlatformError("SCENARIO_RUN_NOT_FOUND", "Scenario capture run not found", status_code=404)
        return json.loads(path.read_text(encoding="utf-8"))

    def preparation_screenshot(self, run_id: str, step_id: str) -> tuple[Path, str]:
        """Serve only an unchanged PNG registered for this run's failed step."""
        directory = self._run_dir(run_id)
        if not re.fullmatch(r"step_\d{2,4}", step_id):
            raise PlatformError("SCENARIO_STEP_INVALID", "无效的采集步骤。", status_code=422)

        def checked_path(path: Path) -> Path:
            current = self.state_root
            for part in path.relative_to(self.state_root).parts:
                current = current / part
                if current.is_symlink():
                    raise PlatformError("SCENARIO_SCREENSHOT_PATH_INVALID", "截图路径包含符号链接。", status_code=409)
            if not path.resolve().is_relative_to(directory.resolve()):
                raise PlatformError("SCENARIO_SCREENSHOT_PATH_INVALID", "截图不属于该采集批次。", status_code=422)
            return path

        checked_path(directory / "run.json")
        run = self.get_run(run_id)
        if run.get("project_id") != self.artifact_store.project_id:
            raise PlatformError("SCENARIO_SCREENSHOT_NOT_FOUND", "未找到该步骤的失败截图。", status_code=404)
        manifest = run.get("manifest") or {}
        entry = next((item for item in manifest.get("files", [])
                      if item.get("kind") == PREPARATION_SCREENSHOT_KIND and item.get("step_id") == step_id), None)
        if not entry:
            raise PlatformError("SCENARIO_SCREENSHOT_NOT_FOUND", "未找到该步骤的失败截图。", status_code=404)
        relative = Path(str(entry.get("relative_path", "")))
        if (relative.is_absolute() or ".." in relative.parts or len(relative.parts) != 2
                or relative.name != "screen_preparation_failed.png"):
            raise PlatformError("SCENARIO_SCREENSHOT_PATH_INVALID", "截图路径不属于受管理的失败现场。", status_code=422)
        path = checked_path(directory / relative)
        if not path.is_file():
            raise PlatformError("SCENARIO_SCREENSHOT_NOT_FOUND", "失败截图文件已缺失。", status_code=404)
        if (not 8 <= path.stat().st_size <= PREPARATION_SCREENSHOT_MAX_BYTES
                or path.stat().st_size != entry.get("size_bytes") or sha256_file(path) != entry.get("sha256")):
            raise PlatformError("SCENARIO_SCREENSHOT_CHANGED", "失败截图内容已发生变化。", status_code=409)
        with path.open("rb") as handle:
            if handle.read(8) != b"\x89PNG\r\n\x1a\n":
                raise PlatformError("SCENARIO_SCREENSHOT_INVALID", "失败截图不是 PNG 文件。", status_code=409)
        return path, str(entry["sha256"])

    def list_runs(self, limit: int = 100) -> list[dict[str, Any]]:
        paths = sorted((self.root / "runs").glob("capture_*/run.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        return [json.loads(path.read_text(encoding="utf-8")) for path in paths[:max(1, min(int(limit), 500))]]
