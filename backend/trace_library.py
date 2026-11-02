"""Explicit Trace copies: persistent metadata, bounded inventories and deletion."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import ntpath
import os
from pathlib import Path
import re
import secrets
import socket
import stat
import tempfile
import threading
from typing import Any

from .core import PlatformError, atomic_write_json, sha256_file, utc_now
from .deployment_mode import require_device_validation
from .local_capture_package import LocalCapturePackageImporter, LocalCapturePackageLimits
from .parallel_modeling.importer import capture_dataset_name

TRACE_ID = re.compile(r"trace_[a-f0-9]{24}")
COPY_ID = re.compile(r"copy_[a-f0-9]{24}")
RUN_ID = re.compile(r"capture_[a-f0-9]{24}")


def trace_identity(project_id: str, run_id: str, relative_path: str) -> str:
    return "trace_" + hashlib.sha256(f"{project_id}\n{run_id}\n{relative_path}".encode()).hexdigest()[:24]


def bridge_copy_identity(project_id: str, bridge_id: str, trace_id: str) -> str:
    return "copy_" + hashlib.sha256(f"{project_id}\nbridge\n{bridge_id}\n{trace_id}".encode()).hexdigest()[:24]


def server_copy_identity(project_id: str, managed_path: str) -> str:
    return "copy_" + hashlib.sha256(f"{project_id}\nserver\n{managed_path}".encode()).hexdigest()[:24]


def windows_local_copy_identity(project_id: str, run_id: str, relative_path: str) -> str:
    return "copy_" + hashlib.sha256(f"{project_id}\nwindows-local\n{run_id}\n{relative_path}".encode()).hexdigest()[:24]


def import_capture_datasets(modeling: Any, run: dict[str, Any]) -> list[dict[str, Any]]:
    """Create one analysis dataset for every modelable captured step.

    ``run`` must come from :class:`LocalCapturePackageImporter` (or the normal
    capture manager), so all managed paths already point below the trusted
    scenario-capture root.  The existing modeling capture adapter remains the
    only code that interprets Trace/PMU evidence.
    """
    if modeling is None:
        return []
    manifest = run.get("manifest") if isinstance(run.get("manifest"), dict) else {}
    files = manifest.get("files") if isinstance(manifest.get("files"), list) else []
    results: list[dict[str, Any]] = []
    for step in manifest.get("steps", []):
        if not isinstance(step, dict) or step.get("captured") is not True:
            continue
        step_id = step.get("step_id")
        if not isinstance(step_id, str):
            continue
        step_files = [entry for entry in files if isinstance(entry, dict) and entry.get("step_id") == step_id]
        kinds = sorted({str(entry.get("kind")) for entry in step_files if entry.get("kind")})
        trace_count = sum(entry.get("kind") == "trace" for entry in step_files)
        if trace_count != 1:
            results.append({"step_id": step_id, "step_title": str(step.get("title") or step_id),
                            "status": "skipped", "file_kinds": kinds,
                            "reason": "该步骤需要且只能包含一个 Trace 文件。"})
            continue
        payload = {"capture_manifest": manifest, "step_id": step_id,
                   "name": capture_dataset_name(manifest, step)}
        try:
            dataset = modeling.import_dataset(payload)
            resolution = dataset.get("provenance", {}).get("hardware_resolution")
            results.append({"step_id": step_id, "step_title": str(step.get("title") or step_id),
                            "status": "ready", "file_kinds": kinds,
                            "dataset_id": dataset.get("id"),
                            "hardware_profile_id": dataset.get("hardware_profile_id"),
                            "hardware_resolution": deepcopy(resolution) if isinstance(resolution, dict) else None,
                            "dataset": dataset})
        except PlatformError as exc:
            # A valid capture may not contain a complete BuildItem callback.
            # Preserve all evidence and report that step independently.
            results.append({"step_id": step_id, "step_title": str(step.get("title") or step_id),
                            "status": "not_analyzable", "file_kinds": kinds,
                            "error": {"code": exc.code, "message": exc.message,
                                      "details": deepcopy(exc.details)}})
    return results


class TraceLibrary:
    def __init__(self, state_root: Path, artifact_store: Any, device_gateway: Any, *, modeling: Any = None):
        self.state_root = Path(state_root).resolve()
        self.store, self.gateway = artifact_store, device_gateway
        self.project_id = artifact_store.project_id
        self.root = artifact_store.root / "trace-library" / ("project_" + hashlib.sha256(self.project_id.encode()).hexdigest()[:16])
        self.root.mkdir(parents=True, exist_ok=True)
        self.upload_root = self.root / "capture-package-uploads"
        self.upload_root.mkdir(parents=True, exist_ok=True)
        self.capture_package_limits = LocalCapturePackageLimits()
        self.capture_package_importer = LocalCapturePackageImporter(self.state_root, limits=self.capture_package_limits)
        # The Trace library owns package validation/materialization.  A modeling
        # manager is optional so inventory-only deployments and focused tests do
        # not need to initialize the analysis stack.
        self.modeling = modeling
        self.index_path = self.root / "index.json"
        self._lock = threading.RLock()

    def _read(self) -> dict:
        if not self.index_path.exists():
            return {"schema_version": "trace_library_registry/v1", "items": {}}
        return json.loads(self.index_path.read_text(encoding="utf-8"))

    def _safe_path(self, managed: str) -> Path:
        value = Path(managed)
        if value.is_absolute() or ".." in value.parts or not value.parts:
            raise PlatformError("TRACE_PATH_INVALID", "Trace 路径不属于受管理目录。", status_code=422)
        allowed = (value.parts[0] == "raw-inputs" or value.parts[:2] == ("scenario-capture", "runs")
                   or value.parts[:2] == ("device-gateway", "bridge-collected")
                   or (len(value.parts) == 4 and value.parts[:2] == ("parallel-modeling", "inputs")
                       and re.fullmatch(r"ds_[a-f0-9]{24}", value.parts[2]) and value.parts[3] == "trace.ftrace"))
        if not allowed:
            raise PlatformError("TRACE_PATH_INVALID", "Trace 路径不属于受管理目录。", status_code=422)
        path = self.state_root / value
        current = self.state_root
        for part in value.parts:
            current = current / part
            if current.is_symlink():
                raise PlatformError("TRACE_PATH_INVALID", "Trace 路径包含符号链接，文件未被访问。", status_code=409)
        if not path.resolve().is_relative_to(self.state_root):
            raise PlatformError("TRACE_PATH_INVALID", "Trace 路径越过管理目录。", status_code=422)
        return path

    def _server_location(self, trace_id: str, managed: str, mode: str, *, label: str = "") -> dict:
        path = self._safe_path(managed)
        copy_id = server_copy_identity(self.project_id, managed)
        return {"copy_id": copy_id, "kind": "server", "bridge_id": "", "managed_path": managed,
            "local_path": str(path), "label": label or ("本机原始文件" if mode == "local" else "平台建模副本"),
            "storage_host": socket.gethostname(), "connection_mode": mode, "status": "available" if path.is_file() else "missing",
            "can_download": path.is_file(), "can_delete": path.is_file(), "observed_at": utc_now()}

    @staticmethod
    def _merge(index: dict, item: dict) -> None:
        previous = index["items"].get(item["trace_id"], {})
        locations = {value["copy_id"]: value for value in previous.get("locations", [])}
        for value in item.get("locations", []):
            old = locations.get(value["copy_id"], {})
            keep_verified = old.get("verified_by_library") and not value.get("verified_by_library")
            locations[value["copy_id"]] = old if old.get("status") == "deleted" or keep_verified else {**old, **value}
        index["items"][item["trace_id"]] = {**previous, **item, "locations": list(locations.values())}

    def _bridge_item(self, item: dict, bridge_id: str, *, verified: bool = False) -> dict | None:
        if not isinstance(item, dict) or not isinstance(item.get("location"), dict):
            return None
        trace_id = item.get("trace_id", "")
        location = item.get("location", {})
        if (not isinstance(trace_id, str) or not TRACE_ID.fullmatch(trace_id)
                or item.get("project_id") != self.project_id or location.get("bridge_id") != bridge_id
                or (item.get("run_id") and not RUN_ID.fullmatch(str(item["run_id"])))
                or location.get("copy_id") != bridge_copy_identity(self.project_id, bridge_id, trace_id)):
            return None
        return {key: deepcopy(value) for key, value in item.items() if key != "location"} | {"locations": [{
            **location, "kind": "windows_bridge", "connection_mode": "bridge", "can_download": False,
            "can_delete": location.get("status") == "available", "verified_by_library": verified,
            "label": "Windows Bridge 原始文件"}]}

    def _inventory(self, index: dict) -> None:
        # Only known capture manifests and typed raw-input artifacts are read.
        # No recursive scan or migration of users' arbitrary filesystem files.
        runs = self.state_root / "scenario-capture" / "runs"
        paths = sorted(runs.glob("capture_*/run.json"), key=lambda path: path.stat().st_mtime, reverse=True)[:500] if runs.is_dir() and not runs.is_symlink() and not runs.parent.is_symlink() else []
        for path in paths:
            if path.is_symlink() or path.parent.is_symlink() or not RUN_ID.fullmatch(path.parent.name):
                continue
            try:
                run = json.loads(path.read_text(encoding="utf-8"))
                project_id = run.get("project_id")
                if not project_id and run.get("plan_id"):
                    try:
                        project_id = self.store.read("scenario_capture_plan", run["plan_id"]).get("project_id")
                    except PlatformError:
                        project_id = "local-default"
                if (project_id or "local-default") != self.project_id:
                    continue
                manifest = run.get("manifest") or {}
                titles = {step["step_id"]: step.get("title", "") for step in manifest.get("steps", [])}
                for entry in manifest.get("files", []):
                    if entry.get("kind") != "trace" or not entry.get("managed_path"):
                        continue
                    relative = entry.get("relative_path") or str(Path(entry["managed_path"]).relative_to(path.parent.relative_to(self.state_root)))
                    if str(path.parent.relative_to(self.state_root) / relative) != entry["managed_path"]:
                        continue
                    trace_id = trace_identity(self.project_id, run["run_id"], relative)
                    locations = [self._server_location(trace_id, entry["managed_path"], run.get("connection_mode", "bridge"))]
                    local_execution = manifest.get("local_execution") if isinstance(manifest.get("local_execution"), dict) else {}
                    windows_root = local_execution.get("run_directory")
                    windows_host = local_execution.get("host")
                    if (isinstance(windows_root, str) and 1 <= len(windows_root) <= 2048
                            and not any(ord(char) < 32 for char in windows_root)
                            and (re.match(r"^[A-Za-z]:[\\/]", windows_root) or windows_root.startswith("\\\\"))
                            and (not isinstance(windows_host, str) or len(windows_host) <= 255)):
                        locations.append({
                            "copy_id": windows_local_copy_identity(self.project_id, run["run_id"], relative),
                            "kind": "windows_local", "bridge_id": "",
                            "local_path": ntpath.normpath(ntpath.join(windows_root, relative.replace("/", "\\"))),
                            "storage_host": windows_host if isinstance(windows_host, str) else "",
                            "connection_mode": "local", "label": "Windows 本地原始文件（采集时记录）",
                            "status": "external", "can_download": False, "can_delete": False,
                            "observed_at": run.get("created_at", ""),
                        })
                    for location in entry.get("storage_locations", []):
                        if location.get("kind") == "windows_bridge" and location.get("bridge_id") == run.get("bridge_id"):
                            expected = bridge_copy_identity(self.project_id, run["bridge_id"], trace_id)
                            if location.get("copy_id") == expected:
                                locations.append(deepcopy(location))
                    job_id, digest = entry.get("device_job_id", ""), entry.get("sha256", "")
                    if re.fullmatch(r"devjob_[a-f0-9]{24}", job_id) and re.fullmatch(r"[a-f0-9]{64}", digest):
                        managed = f"device-gateway/bridge-collected/{job_id}-{digest[:16]}.bin"
                        if (self.state_root / managed).is_file():
                            locations.append(self._server_location(trace_id, managed, "bridge", label="平台回传缓存"))
                    self._merge(index, {"trace_id": trace_id, "name": entry.get("name", "trace.ftrace"),
                        "project_id": self.project_id, "run_id": run["run_id"], "plan_id": run.get("plan_id", ""),
                        "step_id": entry.get("step_id", ""), "step_title": titles.get(entry.get("step_id"), ""),
                        "device_serial": run.get("device_serial", ""), "created_at": run.get("created_at", ""),
                        "size_bytes": entry.get("size_bytes", 0), "sha256": digest, "source": "scenario_capture",
                        "locations": locations, "capture_status": run.get("status", "unknown")})
                jobs_path = path.parent / "device_jobs.json"
                if jobs_path.is_file() and not jobs_path.is_symlink():
                    for job in json.loads(jobs_path.read_text(encoding="utf-8")).get("jobs", []):
                        retained = job.get("retained_trace")
                        if isinstance(retained, dict):
                            item = self._bridge_item(retained, run.get("bridge_id", ""))
                            if item:
                                self._merge(index, item)
            except (OSError, ValueError, KeyError, TypeError, PlatformError):
                continue
        for artifact in self.store.list_artifacts("raw_trace_input", limit=500):
            managed = artifact.get("managed_path")
            if (artifact.get("project_id") != self.project_id or not isinstance(managed, str)
                    or not managed.startswith("raw-inputs/")
                    or artifact.get("input_kind") not in {"raw_htrace", "sys_trace_experimental", "raw_ftrace"}):
                continue
            try:
                trace_id = trace_identity(self.project_id, "raw_input", managed)
                self._merge(index, {"trace_id": trace_id, "name": artifact.get("file_name", Path(managed).name),
                    "project_id": self.project_id, "run_id": "", "plan_id": "", "step_id": "", "step_title": "上传的原始 Trace",
                    "device_serial": "", "created_at": artifact.get("created_at", ""), "size_bytes": artifact.get("bytes", 0),
                    "sha256": artifact.get("input_sha256", ""), "source": "raw_trace_input", "artifact_id": artifact["artifact_id"],
                    "locations": [self._server_location(trace_id, managed, "local", label="上传的原始文件")]})
            except PlatformError:
                continue
        for artifact in self.store.list_artifacts("parallel_modeling_dataset", limit=500):
            dataset_id = artifact.get("id", "")
            if artifact.get("project_id") != self.project_id or not re.fullmatch(r"ds_[a-f0-9]{24}", dataset_id):
                continue
            managed = f"parallel-modeling/inputs/{dataset_id}/trace.ftrace"
            try:
                path = self._safe_path(managed)
                if not path.is_file():
                    continue
                current = path.stat()
                fingerprints = index.setdefault("file_fingerprints", {})
                previous = fingerprints.get(managed, {})
                fingerprint = {"size_bytes": current.st_size, "mtime_ns": current.st_mtime_ns, "inode": current.st_ino}
                digest = previous.get("sha256") if all(previous.get(key) == value for key, value in fingerprint.items()) else None
                digest = digest or sha256_file(path)
                fingerprints[managed] = {**fingerprint, "sha256": digest}
                capture = artifact.get("provenance", {}).get("capture", {})
                parent = next((value for value in index["items"].values()
                               if capture.get("run_id") and value.get("run_id") == capture["run_id"]
                               and value.get("step_id") == capture.get("step_id") and value.get("sha256") == digest), None)
                trace_id = parent["trace_id"] if parent else trace_identity(self.project_id, "modeling_input", managed)
                location = self._server_location(trace_id, managed, "local", label="建模数据集原始文件")
                location["dataset_id"] = dataset_id
                item = ({**parent, "locations": [location]} if parent else {
                    "trace_id": trace_id, "name": str(artifact.get("name") or "trace") + ".ftrace", "project_id": self.project_id,
                    "run_id": "", "plan_id": "", "step_id": "", "step_title": "并行建模原始 Trace", "device_serial": "",
                    "created_at": artifact.get("created_at", ""), "size_bytes": current.st_size, "sha256": digest,
                    "source": "parallel_modeling", "artifact_id": artifact["artifact_id"], "locations": [location]})
                item["dataset_ids"] = sorted(set(item.get("dataset_ids", [])) | {dataset_id})
                # Re-importing a bundled example explicitly restores its runtime
                # copy. Keep bridge/deleted-file tombstones unchanged otherwise.
                sample = artifact.get("provenance", {}).get("sample_archive", {})
                if (artifact.get("source_kind") == "builtin_id"
                        and sample.get("raw_source_files", {}).get("trace.ftrace", {}).get("sha256") == digest):
                    for old in index["items"].get(trace_id, {}).get("locations", []):
                        if old["copy_id"] == location["copy_id"] and old.get("status") == "deleted":
                            old.update(location)
                self._merge(index, item)
            except (PlatformError, OSError):
                continue

    def _bridges(self) -> list[dict]:
        return [{key: value for key, value in bridge.items() if key in {"bridge_id", "display_name", "connection_state", "capabilities"}}
                for bridge in self.gateway.list_bridges(project_id=self.project_id)]

    def _present(self, index: dict, bridges: list[dict]) -> dict:
        live = {bridge["bridge_id"]: bridge for bridge in bridges}
        items = deepcopy(list(index["items"].values()))
        for item in items:
            active = item.get("capture_status") in {"running", "queued"}
            for location in item["locations"]:
                if location.get("status") == "deleted":
                    location.update(can_download=False, can_delete=False)
                    continue
                if location["kind"] == "server":
                    try:
                        path = self._safe_path(location["managed_path"])
                        available = path.is_file() and path.stat().st_size == item["size_bytes"]
                        location["status"] = "available" if available else "missing"
                    except (PlatformError, OSError):
                        location["status"] = "unknown"
                    location.update(can_download=location["status"] == "available", can_delete=location["status"] == "available" and not active)
                elif location["kind"] == "windows_local":
                    location.update(status="external", can_download=False, can_delete=False)
                else:
                    bridge = live.get(location.get("bridge_id"), {})
                    online = bridge.get("connection_state") == "connected"
                    location["status"] = location.get("status", "unknown") if online else "offline"
                    location.update(can_download=False, can_delete=online and location["status"] == "available" and not active)
            preferred = next((loc for loc in item["locations"] if loc["kind"] == "windows_bridge" and loc["status"] == "available"), None)
            preferred = preferred or next((loc for loc in item["locations"] if loc["status"] == "available"), None)
            item["preferred_copy_id"] = preferred["copy_id"] if preferred else None
        items.sort(key=lambda item: item.get("created_at", ""), reverse=True)
        return {"schema_version": "trace_library/v1", "items": items, "bridges": bridges, "observed_at": utc_now(),
            "summary": {"trace_count": len(items), "available_copy_count": sum(loc["status"] == "available" for item in items for loc in item["locations"])}}

    def list(self) -> dict:
        with self._lock:
            index = self._read()
            self._inventory(index)
            atomic_write_json(self.index_path, index)
            return self._present(index, self._bridges())

    def _import_capture_datasets(self, run: dict[str, Any]) -> list[dict[str, Any]]:
        """Create one analysis dataset for every captured step in a package.

        The package importer has already authenticated every manifest member,
        verified its size/hash and materialized it below ``scenario-capture``.
        Passing the normalized manifest to the existing capture adapter keeps a
        single interpretation of Trace/PMU metadata and does not unzip or trust
        a second set of paths here.
        """
        return import_capture_datasets(self.modeling, run)

    def import_capture_package(self, archive: Path, *, expected_size_bytes: int | None = None) -> dict[str, Any]:
        result = self.capture_package_importer.import_zip(archive, project_id=self.project_id,
            expected_size_bytes=expected_size_bytes)
        dataset_imports = self._import_capture_datasets(result["run"])
        datasets = [item["dataset"] for item in dataset_imports if item.get("status") == "ready"]
        return {**result, "dataset_imports": dataset_imports, "datasets": datasets,
                "analysis_ready_count": len(datasets), "catalog": self.list()}

    def _bridge(self, bridge_id: str, action: str, payload: dict) -> dict:
        require_device_validation()
        bridge = next((item for item in self._bridges() if item["bridge_id"] == bridge_id), None)
        if not bridge:
            raise PlatformError("TRACE_BRIDGE_UNAUTHORIZED", "该 Bridge 不属于当前项目。", status_code=403)
        if bridge.get("connection_state") != "connected":
            raise PlatformError("TRACE_BRIDGE_OFFLINE", "Bridge 当前离线，未操作其文件。", status_code=409)
        timeout = 10 if action == "trace.library.list" else 30
        job = self.gateway._bridge_job(project_id=self.project_id, agent_run_id="run_" + secrets.token_hex(12), bridge_id=bridge_id,
            lease_id="", logical_device_id="trace_library", action=action, payload=payload,
            idempotency_key="trace-library:" + secrets.token_hex(12), timeout_seconds=timeout)
        envelope = self.gateway.dispatch_bridge_job(job, wait_seconds=timeout)
        result = envelope.get("result", envelope)
        if result.get("status") != "succeeded":
            raise PlatformError("TRACE_BRIDGE_OPERATION_FAILED", "Bridge 文件操作未成功，原始返回已保留。", status_code=409, details=result)
        return result

    def refresh(self, bridge_id: str = "") -> dict:
        bridges = self._bridges()
        if bridge_id and not any(item["bridge_id"] == bridge_id for item in bridges):
            raise PlatformError("TRACE_BRIDGE_UNAUTHORIZED", "该 Bridge 不属于当前项目。", status_code=403)
        diagnostics = []
        for bridge in bridges:
            if (bridge_id and bridge["bridge_id"] != bridge_id) or bridge.get("connection_state") != "connected":
                continue
            try:
                values, cursor = [], ""
                for _ in range(10):
                    result = self._bridge(bridge["bridge_id"], "trace.library.list", {"cursor": cursor, "limit": 50})
                    values.extend(result.get("items", []))
                    next_cursor = result.get("next_cursor")
                    if not next_cursor or next_cursor == cursor:
                        break
                    cursor = str(next_cursor)
                with self._lock:
                    index = self._read()
                    for existing in index["items"].values():
                        for location in existing["locations"]:
                            if location.get("bridge_id") == bridge["bridge_id"] and location.get("status") != "deleted":
                                location.update(status="unknown", verified_by_library=True)
                    for value in values:
                        item = self._bridge_item(value, bridge["bridge_id"], verified=True)
                        if item:
                            self._merge(index, item)
                    atomic_write_json(self.index_path, index)
                diagnostics.append({"bridge_id": bridge["bridge_id"], "status": "succeeded", "message": "已刷新 Bridge 原始 Trace 文件。"})
            except PlatformError as exc:
                with self._lock:
                    index = self._read()
                    for existing in index["items"].values():
                        for location in existing["locations"]:
                            if location.get("bridge_id") == bridge["bridge_id"] and location.get("status") != "deleted":
                                location.update(status="unknown", verified_by_library=True)
                    atomic_write_json(self.index_path, index)
                diagnostics.append({"bridge_id": bridge["bridge_id"], "status": "failed", "code": exc.code, "message": exc.message})
        return {**self.list(), "diagnostics": diagnostics}

    def _selected(self, trace_id: str, copy_id: str) -> tuple[dict, dict]:
        if not TRACE_ID.fullmatch(trace_id) or not COPY_ID.fullmatch(copy_id):
            raise PlatformError("TRACE_ID_INVALID", "Trace 或副本标识无效。", status_code=422)
        items = self.list()["items"]
        item = next((item for item in items if item["trace_id"] == trace_id), None)
        location = next((loc for loc in item["locations"] if loc["copy_id"] == copy_id), None) if item else None
        if not item or not location:
            raise PlatformError("TRACE_COPY_NOT_FOUND", "未找到该 Trace 的指定副本。", status_code=404)
        return item, location

    def download(self, trace_id: str, copy_id: str) -> tuple[Path, str]:
        item, location = self._selected(trace_id, copy_id)
        if location["kind"] != "server":
            raise PlatformError("TRACE_COPY_REMOTE", "该文件保存在 Bridge，请使用显示的本地路径打开。", status_code=409)
        if not location["can_download"]:
            raise PlatformError("TRACE_FILE_MISSING", "该 Trace 副本已删除、缺失或发生变化。", status_code=404)
        path = self._safe_path(location["managed_path"])
        if sha256_file(path) != item["sha256"]:
            raise PlatformError("TRACE_FILE_CHANGED", "该 Trace 内容已发生变化。", status_code=409)
        return path, item["name"]

    def delete_copy(self, trace_id: str, copy_id: str) -> dict:
        with self._lock:
            item, location = self._selected(trace_id, copy_id)
            if item.get("run_id"):
                run_path = self.state_root / "scenario-capture" / "runs" / item["run_id"] / "run.json"
                if run_path.is_file() and json.loads(run_path.read_text()).get("status") in {"running", "queued"}:
                    raise PlatformError("TRACE_CAPTURE_ACTIVE", "该批次仍在采集，文件尚未删除。", status_code=409)
            if location.get("status") == "deleted":
                return {"status": "deleted", "trace_id": trace_id, "copy_id": copy_id, "item": item}
            if location["kind"] == "windows_bridge":
                self._bridge(location["bridge_id"], "trace.library.delete", {"trace_id": trace_id, "copy_id": copy_id})
            else:
                path = self._safe_path(location["managed_path"])
                if path.exists():
                    before = path.lstat()
                    if not stat.S_ISREG(before.st_mode) or before.st_size != item["size_bytes"] or sha256_file(path) != item["sha256"]:
                        raise PlatformError("TRACE_FILE_CHANGED", "该路径文件已变化，未删除。", status_code=409)
                    after = path.lstat()
                    if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                        raise PlatformError("TRACE_FILE_CHANGED", "删除前文件发生变化，未删除。", status_code=409)
                    path.unlink()
            index = self._read()
            # Physical copies may be referenced by several immutable artifacts.
            for value in index["items"].values():
                for loc in value["locations"]:
                    if loc["copy_id"] == copy_id:
                        loc.update(status="deleted", deleted_at=utc_now(), can_download=False, can_delete=False)
            atomic_write_json(self.index_path, index)
            updated = next(value for value in self.list()["items"] if value["trace_id"] == trace_id)
            return {"status": "deleted", "trace_id": trace_id, "copy_id": copy_id, "item": updated}


def create_trace_library_router(service: TraceLibrary):
    # Trace metadata and capture import helpers are part of the Desktop RPC
    # closure. Load HTTP-only types lazily so that worker stays stdlib-only.
    from fastapi import APIRouter, Body, Request
    from fastapi.responses import FileResponse

    router = APIRouter(prefix="/v1/trace-library", tags=["trace-library"])

    @router.get("")
    def list_traces():
        return service.list()

    @router.post("/refresh")
    def refresh(payload: dict[str, Any] = Body(default={})):
        return service.refresh(str(payload.get("bridge_id") or ""))

    async def import_capture_package(request):
        content_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type not in {"application/zip", "application/octet-stream"}:
            raise PlatformError("LOCAL_CAPTURE_CONTENT_TYPE_INVALID", "请选择 Windows 本地采集生成的 .capture.zip 文件。", status_code=415)
        declared = request.headers.get("content-length", "").strip()
        if declared:
            try:
                declared_size = int(declared)
            except ValueError as exc:
                raise PlatformError("LOCAL_CAPTURE_ARCHIVE_SIZE_INVALID", "采集包上传大小无效。", status_code=422) from exc
            if declared_size < 1 or declared_size > service.capture_package_limits.max_archive_bytes:
                raise PlatformError("LOCAL_CAPTURE_ARCHIVE_TOO_LARGE", "本地采集包超过上传大小限制。", status_code=413,
                                    details={"max_bytes": service.capture_package_limits.max_archive_bytes})
        else:
            declared_size = None
        descriptor, temporary_name = tempfile.mkstemp(prefix=".capture-package-", suffix=".zip", dir=service.upload_root)
        temporary = Path(temporary_name)
        received = 0
        try:
            with os.fdopen(descriptor, "wb") as output:
                async for chunk in request.stream():
                    if not chunk:
                        continue
                    received += len(chunk)
                    if received > service.capture_package_limits.max_archive_bytes:
                        raise PlatformError("LOCAL_CAPTURE_ARCHIVE_TOO_LARGE", "本地采集包超过上传大小限制。", status_code=413,
                                            details={"max_bytes": service.capture_package_limits.max_archive_bytes})
                    output.write(chunk)
                output.flush()
                os.fsync(output.fileno())
            if received == 0:
                raise PlatformError("LOCAL_CAPTURE_ARCHIVE_MISSING", "上传的本地采集包为空。", status_code=422)
            return service.import_capture_package(temporary, expected_size_bytes=declared_size)
        finally:
            temporary.unlink(missing_ok=True)

    # With postponed annotations, a locally imported Request would otherwise
    # be resolved as a query parameter. Attach the concrete class before the
    # route is registered without adding FastAPI to the worker import path.
    import_capture_package.__annotations__["request"] = Request
    router.post("/import-capture-package")(import_capture_package)

    @router.get("/{trace_id}/download")
    def download(trace_id: str, copy_id: str):
        path, name = service.download(trace_id, copy_id)
        return FileResponse(path, filename=name, media_type="application/octet-stream")

    @router.delete("/{trace_id}/copies/{copy_id}")
    def delete(trace_id: str, copy_id: str):
        return service.delete_copy(trace_id, copy_id)

    return router
