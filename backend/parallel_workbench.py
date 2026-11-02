"""Durable jobs joining selected-step capture and numerical parallel modeling."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from pathlib import Path
import re
import secrets
import threading
from typing import Any, Callable

from fastapi import APIRouter, Body
from fastapi.responses import FileResponse, Response

from .core import PlatformError, atomic_write_json, utc_now
from .deployment_mode import require_device_validation
from .scenario_capture import ScenarioCaptureManager, capture_failure_details
from .parallel_modeling import ParallelModelingManager
from .capture_repair import CaptureRepairRequests

TERMINAL = {"succeeded", "partial", "failed", "cancelled"}


class ParallelWorkbench:
    def __init__(self, state_root: Path, artifact_store: Any, device_gateway: Any,
                 *, capture: Any = None, modeling: Any = None) -> None:
        state_root = Path(state_root).resolve()
        self.root = state_root / "parallel-jobs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.artifact_store = artifact_store
        self.project_id = artifact_store.project_id
        self.gateway = device_gateway
        self.capture = capture or ScenarioCaptureManager(state_root, artifact_store, device_gateway)
        self.modeling = modeling or ParallelModelingManager(state_root, artifact_store)
        self._lock = threading.RLock()
        self._cancel: dict[str, threading.Event] = {}
        self._device_jobs: dict[tuple[str, str], str] = {}
        self._job_devices: dict[str, tuple[str, str]] = {}
        self._closed = False
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="parallel-workbench")
        self.repairs = CaptureRepairRequests(self)

    def recover_after_restart(self) -> None:
        """Called once from application startup, never as an import side effect."""
        self.repairs.recover_after_restart()
        for path in self.root.glob("parallel_job_*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue
            with self._lock:
                if isinstance(value, dict) and value.get("status") not in TERMINAL and value.get("job_id") not in self._cancel:
                    value.update(status="failed", updated_at=utc_now(), finished_at=utc_now(), error={"code": "JOB_INTERRUPTED", "message": "服务重启中断了任务，可基于已归档输入重新运行。", "details": {}})
                    atomic_write_json(path, value)

    def shutdown(self, *, wait: bool = True) -> None:
        with self._lock:
            self._closed = True
            for event in self._cancel.values():
                event.set()
        self._pool.shutdown(wait=wait)

    def _path(self, job_id: str) -> Path:
        if not re.fullmatch(r"parallel_job_[a-f0-9]{24}", job_id):
            raise PlatformError("PARALLEL_JOB_INVALID", "无效的任务标识。", status_code=422)
        return self.root / (job_id + ".json")

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            path = self._path(job_id)
            if not path.is_file():
                raise PlatformError("PARALLEL_JOB_NOT_FOUND", "任务不存在。", status_code=404)
            return json.loads(path.read_text(encoding="utf-8"))

    def _update(self, job_id: str, **values: Any) -> dict[str, Any]:
        with self._lock:
            job = self.get_job(job_id)
            job.update(**deepcopy(values), updated_at=utc_now())
            atomic_write_json(self._path(job_id), job)
            return job

    def _release_device(self, job_id: str) -> None:
        device = self._job_devices.pop(job_id, None)
        if device is not None and self._device_jobs.get(device) == job_id:
            self._device_jobs.pop(device)

    def submit(self, kind: str, operation: Callable[..., dict[str, Any]], *, inputs: dict[str, Any], run_id: str = "",
               device: tuple[str, str] | None = None) -> dict[str, Any]:
        job_id = "parallel_job_" + secrets.token_hex(12)
        event = threading.Event()
        job = {"schema_version": "parallel_job/v1", "job_id": job_id, "kind": kind,
               "status": "queued", "progress": {}, "result": None, "error": None,
               "inputs": deepcopy(inputs), "created_at": utc_now(), "updated_at": utc_now()}
        if run_id:
            job["run_id"] = run_id
        with self._lock:
            if self._closed:
                raise PlatformError("PARALLEL_WORKBENCH_STOPPING", "后台服务正在停止。", status_code=503)
            if device is not None and device in self._device_jobs:
                raise PlatformError("SCENARIO_DEVICE_BUSY", "该设备已有排队或执行中的采集任务。", status_code=409,
                                    details={"job_id": self._device_jobs[device], "bridge_id": device[0], "device_serial": device[1]})
            atomic_write_json(self._path(job_id), job)
            self._cancel[job_id] = event
            if device is not None:
                self._device_jobs[device] = job_id
                self._job_devices[job_id] = device

        def progress(value: dict[str, Any]) -> None:
            with self._lock:
                if self.get_job(job_id)["status"] not in TERMINAL:
                    self._update(job_id, progress=value)

        def work() -> None:
            try:
                with self._lock:
                    if event.is_set():
                        self._update(job_id, status="cancelled", finished_at=utc_now())
                        return
                    self._update(job_id, status="running", started_at=utc_now())
                result = operation(cancel_event=event, progress=progress)
                if not isinstance(result, dict):
                    raise TypeError("Background operation must return a JSON object")
                with self._lock:
                    status = str(result.get("status", ""))
                    if status == "cancelled" or event.is_set():
                        self._update(job_id, status="cancelled", result=result, finished_at=utc_now())
                    elif status == "failed":
                        failure = result.get("error") or (capture_failure_details(result.get("manifest") or {}) if kind == "scenario_capture" else None) or {}
                        if not isinstance(failure, dict):
                            failure = {"message": str(failure)}
                        self._update(job_id, status="failed", result=result, finished_at=utc_now(), error={
                            "code": failure.get("code") or ("CAPTURE_FAILED" if kind == "scenario_capture" else "PARALLEL_JOB_FAILED"),
                            "message": failure.get("message") or failure.get("reason") or "任务未完成，已保留运行记录和可用文件。", "details": failure})
                    else:
                        self._update(job_id, status="partial" if status == "partial" else "succeeded", result=result, finished_at=utc_now())
            except Exception as exc:
                error = {"code": getattr(exc, "code", "PARALLEL_JOB_FAILED"), "message": str(exc), "details": getattr(exc, "details", {})}
                self._update(job_id, status="cancelled" if event.is_set() else "failed", error=error, finished_at=utc_now())
            finally:
                with self._lock:
                    self._cancel.pop(job_id, None)
                    self._release_device(job_id)

        with self._lock:
            try:
                self._pool.submit(work)
            except RuntimeError:
                self._cancel.pop(job_id, None)
                self._release_device(job_id)
                self._update(job_id, status="failed", finished_at=utc_now(), error={"code": "PARALLEL_WORKBENCH_STOPPING", "message": "后台服务正在停止。", "details": {}})
                raise
        return job

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        with self._lock:
            job = self.get_job(job_id)
            if job["status"] not in TERMINAL:
                event = self._cancel.get(job_id)
                if event:
                    event.set()
                if job["status"] == "queued":
                    job = self._update(job_id, cancel_requested=True, status="cancelled", finished_at=utc_now())
                    self._release_device(job_id)
                else:
                    job = self._update(job_id, cancel_requested=True)
            return job

    def capture_catalog(self) -> dict[str, Any]:
        return {**self.capture.catalog(), "bridges": self.gateway.list_bridges(project_id=self.project_id)}

    def _require_bridge(self, bridge_id: str) -> None:
        bridge = next((entry for entry in self.gateway.list_bridges(project_id=self.project_id)
                       if entry.get("bridge_id") == bridge_id), None)
        if bridge is None:
            raise PlatformError("SCENARIO_BRIDGE_NOT_FOUND", "请选择当前项目已注册的 Windows Bridge。", status_code=422)
        if bridge.get("connection_state") != "connected":
            raise PlatformError("SCENARIO_BRIDGE_OFFLINE", "所选 Windows Bridge 当前未连接。", status_code=409)

    @staticmethod
    def _connection_mode(value: Any) -> str:
        if not isinstance(value, str) or value not in {"auto", "bridge", "local"}:
            raise PlatformError("SCENARIO_CONNECTION_MODE_INVALID", "请选择自动识别、本机 HDC 直连或 Windows Bridge。", status_code=422)
        return str(value)

    def _require_local_hdc(self) -> None:
        local = self.capture.catalog().get("local_hdc", {})
        if not local.get("available"):
            raise PlatformError("SCENARIO_LOCAL_HDC_UNAVAILABLE", "本机尚未找到 HDC，可通过 HETERO_HDC_BIN 指定工具路径。", status_code=503, details=local)

    def discover_devices(self, bridge_id: str = "", *, connection_mode: str = "bridge") -> dict[str, Any]:
        require_device_validation()
        mode = self._connection_mode(connection_mode)
        if mode == "local":
            bridge_id = ""
        return self.capture.discover_devices(bridge_id, connection_mode=mode)

    def _resolve_auto_device(self, serial: str, bridge_id: str) -> dict[str, Any]:
        if bridge_id:
            # A previously selected remote target remains pinned even if a
            # different local phone has since appeared.
            discovery = self.capture.discover_devices(bridge_id, connection_mode="bridge", timeout_seconds=10, targets_only=True)
        else:
            discovery = self.capture.discover_devices(connection_mode="auto")
        devices = discovery.get("devices", [])
        if discovery.get("discovery_complete") is False or discovery.get("status") == "discovery_incomplete":
            raise PlatformError("SCENARIO_DEVICE_DISCOVERY_INCOMPLETE", "设备发现尚未完整完成，请重试或选择具体设备连接。",
                                status_code=503, details=discovery)
        if serial:
            selected = [device for device in devices if device.get("device_serial") == serial]
            if not selected:
                raise PlatformError("SCENARIO_DEVICE_NOT_CONNECTED", "先前选择的设备当前不在线，尚未切换到其他设备。",
                                    status_code=409, details={"device_serial": serial, "bridge_id": bridge_id, "discovery": discovery})
        else:
            selected = devices
        if len(selected) > 1:
            raise PlatformError("SCENARIO_DEVICE_SELECTION_REQUIRED", "发现多台可用设备，请选择本次采集设备及连接方式。",
                                status_code=422, details=discovery)
        if not selected:
            raise PlatformError("SCENARIO_DEVICE_UNAVAILABLE", "尚未发现可用设备，请连接手机或启动 Windows Bridge 后重试。",
                                status_code=503, details=discovery)
        return selected[0]

    def execute_capture(self, plan_id: str, payload: dict[str, Any], *, agent_run_id: str = "") -> dict[str, Any]:
        if payload.get("repair_request_id"):
            return self.repairs.execute(payload["repair_request_id"], plan_id, payload, agent_run_id=agent_run_id)
        return self._execute_capture(plan_id, payload, agent_run_id=agent_run_id)

    def _execute_capture(self, plan_id: str, payload: dict[str, Any], *, agent_run_id: str = "") -> dict[str, Any]:
        require_device_validation()
        plan = self.capture.get_plan(plan_id)
        if plan["readiness"]["blockers"]:
            raise PlatformError("SCENARIO_PLAN_NOT_READY", "请完善步骤操作和界面定位信息。", status_code=422, details=plan["readiness"])
        requested_mode = self._connection_mode(payload.get("connection_mode", "bridge"))
        mode = requested_mode
        bridge_id = str(payload.get("bridge_id") or "") if mode in {"auto", "bridge"} else ""
        serial = str(payload.get("device_serial") or "")
        if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", serial) and (serial or mode != "auto"):
            raise PlatformError("SCENARIO_DEVICE_REQUIRED", "请选择设备或填写设备序列号。", status_code=422)
        if mode == "auto":
            device = self._resolve_auto_device(serial, bridge_id)
            mode, bridge_id, serial = device["connection_mode"], device["bridge_id"], device["device_serial"]
        if mode == "bridge":
            self._require_bridge(bridge_id)
        else:
            self._require_local_hdc()
        lease_id = str(payload.get("lease_id") or "")
        run_id = self.capture.new_run_id()
        return self.submit("scenario_capture", lambda **callbacks: self.capture.execute(
            plan_id, connection_mode=mode, bridge_id=bridge_id, device_serial=serial, lease_id=lease_id if mode == "bridge" else "",
            agent_run_id=agent_run_id, run_id=run_id, requested_connection_mode=requested_mode, **callbacks),
            inputs={"plan_id": plan_id, "connection_mode": mode, "requested_connection_mode": requested_mode,
                    "bridge_id": bridge_id, "device_serial": serial,
                    **({"repair_request_id": payload["repair_request_id"], "retry_of_run_id": payload["retry_of_run_id"]}
                       if payload.get("repair_request_id") else {})}, run_id=run_id,
            device=("local" if mode == "local" else "bridge:" + bridge_id, serial))

    def get_capture_run(self, run_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"capture_[a-f0-9]{24}", run_id):
            raise PlatformError("SCENARIO_RUN_INVALID", "无效的采集批次标识。", status_code=422)
        job = None
        with self._lock:
            for path in self.root.glob("parallel_job_*.json"):
                try:
                    candidate = json.loads(path.read_text(encoding="utf-8"))
                except (ValueError, OSError):
                    continue
                if isinstance(candidate, dict) and candidate.get("run_id") == run_id:
                    job = candidate
                    break
        try:
            run = self.capture.get_run(run_id)
        except PlatformError as exc:
            if exc.code != "SCENARIO_RUN_NOT_FOUND" or job is None:
                raise
            run = {"schema_version": "scenario_capture_run/v1", "run_id": run_id,
                   **job["inputs"], "created_at": job["created_at"], "status": job["status"], "manifest": None}
        if job:
            run["job_id"] = job["job_id"]
            if job["status"] in TERMINAL and run["status"] in {"queued", "running"}:
                run.update(status=job["status"], error=job.get("error"), finished_at=job.get("finished_at"))
        return run

    def import_dataset(self, payload: dict[str, Any]) -> dict[str, Any]:
        value = deepcopy(payload)
        if value.get("capture_run_id"):
            run = self.get_capture_run(str(value.pop("capture_run_id")))
            if run.get("status") in {"queued", "running"}:
                raise PlatformError("CAPTURE_NOT_FINISHED", "采集仍在执行，请等待文件回收完成。", status_code=409)
            manifest = run.get("manifest")
            if run.get("manifest_artifact_id"):
                manifest = self.artifact_store.read("scenario_capture_manifest", run["manifest_artifact_id"])
            if not isinstance(manifest, dict):
                raise PlatformError("CAPTURE_MANIFEST_UNAVAILABLE", "所选采集批次尚未生成数据清单。", status_code=422)
            value["capture_manifest"] = manifest
        return self.modeling.import_dataset(value)

    def analyze(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.modeling.get_dataset(str(payload.get("dataset_id") or ""))
        frozen = deepcopy(payload)
        return self.submit("parallel_analysis", lambda **callbacks: self.modeling.analyze(frozen, **callbacks), inputs=frozen)

    def search(self, payload: dict[str, Any]) -> dict[str, Any]:
        self.modeling.get_analysis(str(payload.get("analysis_id") or ""))
        frozen = deepcopy(payload)
        return self.submit("parallel_search", lambda **callbacks: self.modeling.search(frozen, **callbacks), inputs=frozen)

    def action(self, action_id: str, payload: dict[str, Any], *, agent_run_id: str = "") -> dict[str, Any]:
        def required(key: str) -> str:
            value = payload.get(key)
            if not isinstance(value, str) or not value.strip():
                raise PlatformError("PARALLEL_ACTION_INPUT_REQUIRED", f"缺少 {key}。", status_code=422)
            return value

        handlers: dict[str, Callable[[], Any]] = {
            "scenario_capture.catalog": self.capture_catalog,
            "scenario_capture.create_plan": lambda: self.capture.create_plan(payload),
            "scenario_capture.read_plan": lambda: self.capture.get_plan(required("plan_id")),
            "scenario_capture.generate_script": lambda: self.capture.generate_script(required("plan_id")),
            "scenario_capture.execute": lambda: self.execute_capture(required("plan_id"), payload, agent_run_id=agent_run_id),
            "scenario_capture.read_run": lambda: self.get_capture_run(required("run_id")),
            "scenario_capture.discover_devices": lambda: self.discover_devices(str(payload.get("bridge_id") or ""), connection_mode=payload.get("connection_mode", "bridge")),
            "parallel_modeling.catalog": self.modeling.catalog,
            "parallel_modeling.list_hardware_profiles": lambda: {"profiles": self.modeling.list_hardware_profiles()},
            "parallel_modeling.create_hardware_profile": lambda: self.modeling.create_hardware_profile(payload),
            "parallel_modeling.archive_hardware_profile": lambda: self.modeling.archive_hardware_profile(required("hardware_profile_id")),
            "parallel_modeling.restore_hardware_profile": lambda: self.modeling.restore_hardware_profile(required("hardware_profile_id")),
            "parallel_modeling.bind_dataset_hardware": lambda: self.modeling.bind_dataset_hardware(required("dataset_id"), payload),
            "workload_features.catalog": lambda: self.modeling.workload_features.catalog(),
            "workload_features.list_profiles": lambda: {"profiles": self.modeling.workload_features.list_profiles()},
            "workload_features.analyze": lambda: self.modeling.workload_features.analyze(payload),
            "workload_features.read_profile": lambda: self.modeling.workload_features.get_profile(required("profile_id")),
            "parallel_modeling.import_dataset": lambda: self.import_dataset(payload),
            "parallel_modeling.analyze": lambda: self.analyze(payload),
            "parallel_modeling.read_analysis": lambda: self.modeling.get_analysis(required("analysis_id")),
            "parallel_modeling.search": lambda: self.search(payload),
            "parallel_modeling.read_search": lambda: self.modeling.get_search(required("search_id")),
            "parallel_job.read": lambda: self.get_job(required("job_id")),
            "parallel_job.cancel": lambda: self.cancel_job(required("job_id")),
        }
        if action_id not in handlers:
            raise PlatformError("PARALLEL_ACTION_UNKNOWN", "未知的并行工作台操作。", status_code=422)
        return handlers[action_id]()


def create_parallel_router(service: ParallelWorkbench) -> APIRouter:
    router = APIRouter(prefix="/v1", tags=["scenario-capture", "parallel-modeling"])

    @router.get("/scenario-capture/catalog")
    def capture_catalog():
        return service.capture_catalog()

    @router.post("/scenario-capture/devices/discover")
    def discover(payload: dict[str, Any] = Body(...)):
        return service.discover_devices(str(payload.get("bridge_id") or ""), connection_mode=payload.get("connection_mode", "bridge"))

    @router.get("/scenario-capture/plans")
    def plans():
        return {"plans": service.capture.list_plans()}

    @router.post("/scenario-capture/plans")
    def create_plan(payload: dict[str, Any] = Body(...)):
        return service.capture.create_plan(payload)

    @router.get("/scenario-capture/plans/{plan_id}")
    def get_plan(plan_id: str):
        return service.capture.get_plan(plan_id)

    @router.get("/scenario-capture/plans/{plan_id}/script")
    def script(plan_id: str):
        result = service.capture.generate_script(plan_id)
        return Response(result["content"], media_type="text/x-python", headers={"Content-Disposition": f'attachment; filename="{result["filename"]}"'})

    @router.get("/scenario-capture/plans/{plan_id}/local-package")
    def local_package(plan_id: str, device_serial: str = "", bridge_id: str = "", connection_mode: str = "bridge"):
        result = service.capture.generate_local_package(plan_id, device_serial=device_serial,
            bridge_id=bridge_id, connection_mode=connection_mode)
        return Response(result["content"], media_type="application/zip", headers={
            "Content-Disposition": f'attachment; filename="{result["filename"]}"',
            "Cache-Control": "no-store", "X-Content-Sha256": result["sha256"],
        })

    @router.post("/scenario-capture/plans/{plan_id}/execute")
    def execute(plan_id: str, payload: dict[str, Any] = Body(...)):
        return service.execute_capture(plan_id, payload)

    @router.get("/scenario-capture/runs")
    def runs():
        return {"runs": service.capture.list_runs()}

    @router.get("/scenario-capture/runs/{run_id}")
    def get_run(run_id: str):
        return service.get_capture_run(run_id)

    @router.post("/scenario-capture/runs/{run_id}/repair-request")
    def create_repair_request(run_id: str, payload: dict[str, Any] = Body(...)):
        return service.repairs.create(run_id, str(payload.get("job_id") or ""))

    @router.get("/scenario-capture/repair-requests/{request_id}")
    def get_repair_request(request_id: str):
        return service.repairs.get(request_id)

    @router.post("/scenario-capture/repair-requests/{request_id}/cancel")
    def cancel_repair_request(request_id: str):
        return service.repairs.cancel(request_id)

    @router.get("/scenario-capture/runs/{run_id}/steps/{step_id}/preparation-screenshot")
    def preparation_screenshot(run_id: str, step_id: str, download: bool = False):
        path, digest = service.capture.preparation_screenshot(run_id, step_id)
        return FileResponse(path, media_type="image/png", filename=f"{run_id}-{step_id}-screen.png",
                            content_disposition_type="attachment" if download else "inline",
                            headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "X-Artifact-Sha256": digest})

    @router.get("/parallel-modeling/catalog")
    def model_catalog():
        return service.modeling.catalog()

    @router.get("/parallel-modeling/hardware-profiles")
    def hardware_profiles():
        return {"profiles": service.modeling.list_hardware_profiles()}

    @router.post("/parallel-modeling/hardware-profiles")
    def create_hardware_profile(payload: dict[str, Any] = Body(...)):
        return service.modeling.create_hardware_profile(payload)

    @router.get("/parallel-modeling/hardware-profiles/{hardware_profile_id}")
    def get_hardware_profile(hardware_profile_id: str):
        return service.modeling.get_hardware_profile(hardware_profile_id)

    @router.post("/parallel-modeling/hardware-profiles/{hardware_profile_id}/archive")
    def archive_hardware_profile(hardware_profile_id: str):
        return service.modeling.archive_hardware_profile(hardware_profile_id)

    @router.post("/parallel-modeling/hardware-profiles/{hardware_profile_id}/restore")
    def restore_hardware_profile(hardware_profile_id: str):
        return service.modeling.restore_hardware_profile(hardware_profile_id)

    @router.get("/parallel-modeling/datasets")
    def datasets():
        return {"datasets": service.modeling.list_datasets()}

    @router.post("/parallel-modeling/datasets")
    @router.post("/parallel-modeling/datasets/import")
    def import_dataset(payload: dict[str, Any] = Body(...)):
        return service.import_dataset(payload)

    @router.get("/parallel-modeling/datasets/{dataset_id}")
    def get_dataset(dataset_id: str):
        return service.modeling.get_dataset(dataset_id)

    @router.post("/parallel-modeling/datasets/{dataset_id}/hardware-binding")
    def bind_dataset_hardware(dataset_id: str, payload: dict[str, Any] = Body(...)):
        return service.modeling.bind_dataset_hardware(dataset_id, payload)

    @router.post("/parallel-modeling/analyses")
    def analyze(payload: dict[str, Any] = Body(...)):
        return service.analyze(payload)

    @router.get("/parallel-modeling/analyses")
    def analyses():
        return {"analyses": service.modeling.list_analyses()}

    @router.get("/parallel-modeling/analyses/{analysis_id}")
    def get_analysis(analysis_id: str):
        return service.modeling.get_analysis(analysis_id)

    @router.post("/parallel-modeling/searches")
    def search(payload: dict[str, Any] = Body(...)):
        return service.search(payload)

    @router.get("/parallel-modeling/searches")
    def searches():
        return {"searches": service.modeling.list_searches()}

    @router.get("/parallel-modeling/searches/{search_id}")
    def get_search(search_id: str):
        return service.modeling.get_search(search_id)

    @router.get("/parallel-jobs/{job_id}")
    def job(job_id: str):
        return service.get_job(job_id)

    @router.post("/parallel-jobs/{job_id}/cancel")
    def cancel(job_id: str):
        return service.cancel_job(job_id)

    return router
