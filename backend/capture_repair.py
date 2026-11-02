"""One cancellable Agent repair and recapture for a user-started capture job."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
from typing import Any

from .core import PlatformError, atomic_write_json, utc_now


def repairable_capture(job: dict[str, Any]) -> bool:
    if job.get("kind") != "scenario_capture" or job.get("cancel_requested"):
        return False
    if job.get("status") == "failed":
        return True
    if job.get("status") != "partial":
        return False
    run = job.get("result") or {}
    manifest = run.get("manifest") or {}
    preparation = manifest.get("pmu_preparation") or run.get("pmu_preparation") or {}
    if (preparation.get("allow_execution") is False
            or preparation.get("status") in {"probe_failed", "probe_timed_out", "deployment_failed", "bundle_missing", "bundle_invalid", "incompatible", "not_executable", "custom_path_missing"}):
        return True
    for step in manifest.get("steps", []):
        if step.get("captured") is False:
            continue
        if (step.get("collector_status") or {}).get("pmu") in {"failed", "unavailable", "invalid", "missing"}:
            return True
        if ((step.get("pmu_validation") or {}).get("sampler_preparation") or {}).get("allow_execution") is False:
            return True
    return False


class CaptureRepairRequests:
    def __init__(self, service):
        self.service = service
        self.root = service.root.parent / "capture-repair-requests"
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, request_id):
        if not isinstance(request_id, str) or not re.fullmatch(r"capture_repair_[a-f0-9]{24}", request_id):
            raise PlatformError("CAPTURE_REPAIR_ID_INVALID", "无效的采集修复请求。", status_code=422)
        return self.root / (request_id + ".json")

    def _read(self, request_id):
        path = self._path(request_id)
        if not path.is_file():
            raise PlatformError("CAPTURE_REPAIR_NOT_FOUND", "采集修复请求不存在。", status_code=404)
        value = json.loads(path.read_text(encoding="utf-8"))
        if value.get("project_id") != self.service.project_id:
            raise PlatformError("CAPTURE_REPAIR_NOT_FOUND", "采集修复请求不存在。", status_code=404)
        return value

    def _write(self, value):
        value["updated_at"] = utc_now()
        atomic_write_json(self._path(value["repair_request_id"]), value)
        return value

    def _recover_child(self, value):
        """Recover the durable job if persisting its receipt was interrupted."""
        if value.get("retry_job_id") or value["status"] == "pending":
            return False
        matches = []
        for path in self.service.root.glob("parallel_job_*.json"):
            try:
                job = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(job, dict) or job.get("job_id") != path.stem:
                continue
            inputs = job.get("inputs")
            if not isinstance(inputs, dict) or not inputs.get("plan_id"):
                continue
            if (job.get("kind") == "scenario_capture"
                    and inputs.get("repair_request_id") == value["repair_request_id"]
                    and inputs.get("retry_of_run_id") == value["source_run_id"]
                    and all(inputs.get(key, "") == value.get(key, "")
                            for key in ("device_serial", "connection_mode", "bridge_id"))):
                matches.append(job)
        if len(matches) > 1:
            raise PlatformError("CAPTURE_REPAIR_CHILD_AMBIGUOUS", "修复请求关联了多个重试任务，请核对任务记录。", status_code=409)
        if not matches:
            return False
        job = matches[0]
        value.update(retry_job_id=job["job_id"], retry_run_id=job.get("run_id"),
                     retry_plan_id=job["inputs"]["plan_id"], child_receipt_recovered=True)
        if value["status"] in {"pending", "submitting"}:
            value["status"] = "submitted"
        return True

    def create(self, run_id, job_id):
        with self.service._lock:
            job = self.service.get_job(job_id)
            if job.get("run_id") != run_id or not repairable_capture(job):
                raise PlatformError("CAPTURE_REPAIR_NOT_APPLICABLE", "本次任务没有可自动修复的采集失败。", status_code=409)
            if (job.get("inputs") or {}).get("repair_request_id"):
                raise PlatformError("CAPTURE_REPAIR_LIMIT_REACHED", "本次采集已完成一轮自动修复，可查看结果后手动重新采集。", status_code=409)
            request_id = "capture_repair_" + hashlib.sha256((self.service.project_id + ":" + job_id).encode()).hexdigest()[:24]
            if self._path(request_id).exists():
                return self.get(request_id)
            inputs = job["inputs"]
            plan = self.service.capture.get_plan(inputs["plan_id"])
            now = datetime.now(timezone.utc)
            return self._write({"schema_version": "capture_repair_request/v1", "repair_request_id": request_id,
                "project_id": self.service.project_id, "source_job_id": job_id, "source_run_id": run_id,
                "plan_id": inputs["plan_id"], "device_serial": inputs["device_serial"],
                "bridge_id": inputs.get("bridge_id", ""), "connection_mode": inputs["connection_mode"],
                "intent": {"bundle": plan["profile"]["bundle"], "selected_step_ids": plan["capture"]["selected_step_ids"],
                           "collectors": plan["capture"]["collectors"]},
                "status": "pending", "retry_job_id": None, "retry_run_id": None,
                "created_at": utc_now(), "expires_at": (now + timedelta(minutes=30)).isoformat().replace("+00:00", "Z")})

    def get(self, request_id):
        with self.service._lock:
            value = self._read(request_id)
            if self._recover_child(value):
                if value["status"] in {"cancelled", "expired"}:
                    self.service.cancel_job(value["retry_job_id"])
                self._write(value)
            if value["status"] == "pending" and datetime.now(timezone.utc) >= datetime.fromisoformat(value["expires_at"].replace("Z", "+00:00")):
                value.update(status="expired", message="自动修复请求已过期，可手动重新采集。")
                self._write(value)
            if value.get("retry_job_id"):
                job = self.service.get_job(value["retry_job_id"])
                value["retry_status"] = job["status"]
                if value["status"] != "cancelled" and job["status"] in {"succeeded", "partial", "failed", "cancelled"}:
                    value["status"] = job["status"]
                    self._write(value)
            return value

    def cancel(self, request_id):
        with self.service._lock:
            # Resolve without writing first: cancellation must reach the child
            # even when the original receipt write failed because storage is full.
            value = self._read(request_id)
            recovered = self._recover_child(value)
            child = self.service.get_job(value["retry_job_id"]) if value.get("retry_job_id") else None
            child_active = child is not None and child["status"] not in {"succeeded", "partial", "failed", "cancelled"}
            if child and not child_active and value["status"] not in {"cancelled", "expired"}:
                value.update(status=child["status"], retry_status=child["status"])
                recovered = True
            if value["status"] in {"pending", "submitting", "submitted"} or child_active:
                value.update(status="cancelled", message="自动修复与重试已关闭。")
                if value.get("retry_job_id"):
                    self.service.cancel_job(value["retry_job_id"])
                self._write(value)
            elif recovered:
                self._write(value)
            return value

    def execute(self, request_id, plan_id, payload, *, agent_run_id=""):
        with self.service._lock:
            value = self.get(request_id)
            if value["status"] in {"cancelled", "expired"}:
                raise PlatformError("CAPTURE_REPAIR_DISABLED", "自动修复请求已关闭或过期，未启动新的采集。", status_code=409)
            if value.get("retry_job_id"):
                if value.get("retry_plan_id") != plan_id:
                    raise PlatformError("CAPTURE_REPAIR_ALREADY_USED", "该修复请求已提交一次重试。", status_code=409)
                return self.service.get_job(value["retry_job_id"])
            if value["status"] != "pending":
                raise PlatformError("CAPTURE_REPAIR_ALREADY_USED", "该修复请求已处理，可手动发起新的采集。", status_code=409)
            for key in ("device_serial", "bridge_id"):
                if payload.get(key) and payload[key] != value[key]:
                    raise PlatformError("CAPTURE_REPAIR_DEVICE_CHANGED", "自动重试沿用本次采集设备。", status_code=409)
            if payload.get("connection_mode", "auto") not in {"auto", value["connection_mode"]}:
                raise PlatformError("CAPTURE_REPAIR_DEVICE_CHANGED", "自动重试沿用本次采集连接方式。", status_code=409)
            plan = self.service.capture.get_plan(plan_id)
            intent = value["intent"]
            if (plan["profile"]["bundle"] != intent["bundle"]
                    or set(plan["capture"]["selected_step_ids"]) != set(intent["selected_step_ids"])
                    or set(plan["capture"]["collectors"]) != set(intent["collectors"])):
                raise PlatformError("CAPTURE_REPAIR_SCOPE_CHANGED", "自动修复可调整准备参数与控件定位，重试保留原应用、采集点和采集器。", status_code=409)
            # The same lock covers consumption and job submission. Queued work
            # takes that lock before executing, so cancellation has one owner.
            value.update(status="submitting", retry_plan_id=plan_id, agent_run_id=agent_run_id)
            self._write(value)
            try:
                receipt = self.service._execute_capture(plan_id, {**payload,
                    "connection_mode": value["connection_mode"], "device_serial": value["device_serial"],
                    "bridge_id": value["bridge_id"], "repair_request_id": request_id,
                    "retry_of_run_id": value["source_run_id"]}, agent_run_id=agent_run_id)
            except Exception as exc:
                value.update(status="failed", message=str(exc)[:2000], error_code=getattr(exc, "code", "CAPTURE_REPAIR_SUBMIT_FAILED"))
                self._write(value)
                raise
            value.update(status="submitted", retry_job_id=receipt["job_id"], retry_run_id=receipt["run_id"])
            self._write(value)
            return receipt

    def recover_after_restart(self):
        with self.service._lock:
            for path in self.root.glob("capture_repair_*.json"):
                try:
                    value = self._read(path.stem)
                except (OSError, ValueError, PlatformError):
                    continue
                interrupted = value["status"] in {"pending", "submitting"}
                recovered = self._recover_child(value)
                if interrupted:
                    value.update(status="cancelled", message="服务重启已撤销未执行的自动修复请求。")
                    if value.get("retry_job_id"):
                        self.service.cancel_job(value["retry_job_id"])
                    self._write(value)
                elif recovered:
                    if value["status"] in {"cancelled", "expired"}:
                        self.service.cancel_job(value["retry_job_id"])
                    self._write(value)
