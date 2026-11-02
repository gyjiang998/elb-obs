"""Small, allow-listed RPC surface used by the Desktop SSH transport.

The Desktop application owns interaction and device capture.  This module is
intentionally limited to durable analysis data kept below HETERO_STATE_ROOT;
it does not start an HTTP server or expose device operations.
"""

from __future__ import annotations

import argparse
import base64
from collections import OrderedDict
import copy
import gzip
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import stat
import sys
import time
from typing import Any

from backend.core import PlatformError, utc_now
from backend.capture_runtime_trace import materialize_capture_runtime_trace
from backend.desktop_workspace_rpc import dispatch_workspace
from backend.local_capture_package import LocalCapturePackageImporter, LocalCapturePackageLimits
from backend.parallel_modeling.manager import BUNDLE_ROOT, ParallelModelingManager
from backend.parallel_modeling.workload_features import export_profile_csv
from backend.trace_library import import_capture_datasets


SCHEMA_VERSION = "desktop_platform_rpc/v2"
MAX_REQUEST_BYTES = 12 * 1024 * 1024
RPC_MARKER = "HETERO_DESKTOP_RPC\t"
RPC_GZIP_MARKER = "HETERO_DESKTOP_RPC_GZIP\t"
RPC_STREAM_READY_MARKER = "HETERO_DESKTOP_RPC_STREAM_READY\t"
RPC_STREAM_RESPONSE_MARKER = "HETERO_DESKTOP_RPC_STREAM_RESPONSE\t"
RPC_STREAM_PROTOCOL = "desktop_platform_rpc_stream/v1"
# Preserve the complete evidence model without making ssh2 buffer an
# unbounded stdout stream. Repeated per-stage PMU schemas compress well, and
# this wire budget leaves headroom below DesktopRuntime's 1 MiB output guard.
RESPONSE_COMPRESSION_THRESHOLD = 512 * 1024
MAX_RESPONSE_JSON_BYTES = 8 * 1024 * 1024
MAX_RESPONSE_WIRE_BYTES = 768 * 1024
PROFILE_PATH = re.compile(r"^/v1/workload-features/profiles/(wf_[0-9a-f]{24})$")
EXPORT_PATH = re.compile(
    r"^/v1/workload-features/profiles/(wf_[0-9a-f]{24})/export\.csv\?view=(stages|pmu)$"
)
DATASET_PATH = re.compile(r"^/v1/parallel-modeling/datasets/(ds_[0-9a-f]{24})$")
DATASET_HARDWARE_BINDING_PATH = re.compile(r"^/v1/parallel-modeling/datasets/(ds_[0-9a-f]{24})/hardware-binding$")
HARDWARE_PROFILE_PATH = re.compile(r"^/v1/parallel-modeling/hardware-profiles/(hp_[0-9a-f]{24}|kirin-gallery-reference-v1)$")
HARDWARE_PROFILE_ARCHIVE_PATH = re.compile(r"^/v1/parallel-modeling/hardware-profiles/(hp_[0-9a-f]{24}|kirin-gallery-reference-v1)/archive$")
HARDWARE_PROFILE_RESTORE_PATH = re.compile(r"^/v1/parallel-modeling/hardware-profiles/(hp_[0-9a-f]{24}|kirin-gallery-reference-v1)/restore$")
ANALYSIS_PATH = re.compile(r"^/v1/parallel-modeling/analyses/(an_[0-9a-f]{24})$")
SEARCH_PATH = re.compile(r"^/v1/parallel-modeling/searches/(sr_[0-9a-f]{24})$")
CALIBRATION_PLAN_PATH = re.compile(r"^/v1/parallel-calibration/plans/(cp_[0-9a-f]{24})$")
CALIBRATION_BUNDLE_PATH = re.compile(r"^/v1/parallel-calibration/plans/(cp_[0-9a-f]{24})/bundle$")
CALIBRATION_FIT_PATH = re.compile(r"^/v1/parallel-calibration/fits/(cf_[0-9a-f]{24})$")
DESKTOP_CAPTURE_TOKEN = re.compile(r"[0-9a-f]{32}")
DESKTOP_CAPTURE_SHA256 = re.compile(r"[0-9a-f]{64}")
DESKTOP_CAPTURE_UPLOAD_DIRECTORY = "desktop-capture-uploads"
DESKTOP_CAPTURE_STAGING_TTL_SECONDS = 24 * 60 * 60
CALIBRATION_MEASUREMENT_UPLOAD_DIRECTORY = "parallel-calibration-measurement-uploads"
CALIBRATION_MEASUREMENT_MAX_BYTES = 20 * 1024 * 1024
CALIBRATION_MEASUREMENT_STAGING_TTL_SECONDS = 24 * 60 * 60
PERSISTENT_MANAGER_CACHE_ENTRIES = 4
PORTABLE_CALIBRATION_PACKAGE_ROOT = BUNDLE_ROOT / "calibration-packages"


class _ParallelModelingManagerCache:
    """Bound expensive modeling initialization to a long-lived RPC worker.

    ParallelModelingManager verifies the immutable bundled model when it is
    constructed, but its durable datasets, profiles, analyses and calibration
    records are read from disk by each operation. Reusing the manager therefore
    avoids repeated bundle verification without turning durable records into an
    in-memory snapshot.
    """

    def __init__(self, max_entries: int = PERSISTENT_MANAGER_CACHE_ENTRIES):
        if isinstance(max_entries, bool) or not isinstance(max_entries, int) or max_entries < 1:
            raise ValueError("max_entries must be a positive integer")
        self.max_entries = max_entries
        self._entries: OrderedDict[Path, ParallelModelingManager] = OrderedDict()

    def get(self, state_root: Path) -> ParallelModelingManager:
        key = Path(state_root).expanduser().resolve()
        manager = self._entries.pop(key, None)
        if manager is None:
            manager = ParallelModelingManager(
                key,
                artifact_store=None,
                portable_calibration_package_root=PORTABLE_CALIBRATION_PACKAGE_ROOT,
            )
        self._entries[key] = manager
        while len(self._entries) > self.max_entries:
            self._entries.popitem(last=False)
        return manager


def _analysis_view(value: dict[str, Any]) -> dict[str, Any]:
    """Return the fields consumed by the shared modeling page.

    The observed and candidate-parallel graphs are part of that page's primary
    result, so they remain available. Raw scheduling timelines and request
    ledgers stay in the persisted result and are omitted from this transport.
    """

    result = copy.deepcopy(value)
    baseline = result.get("baseline") if isinstance(result.get("baseline"), dict) else {}
    result["baseline"] = {
        key: baseline[key]
        for key in (
            "duration_ms", "app_cpu_ms", "app_off_cpu_ms", "service_cpu_ms", "service_call_count",
            "builditem_count", "replay_ms", "endpoint", "data_kind",
            "time_accounting",
        )
        if key in baseline
    }
    result.pop("requests", None)
    result.pop("model_reference_costs", None)
    result["transport_projection"] = {
        "schema_version": "desktop_analysis_projection/v1",
        "full_result_persisted": True,
        "omitted": ["baseline.timeline", "baseline.critical_chain", "baseline.critical_edges", "requests", "model_reference_costs"],
    }
    return result


def _candidate_view(value: Any, *, include_execution: bool = False) -> Any:
    if not isinstance(value, dict):
        return value
    result = copy.deepcopy(value)
    omitted = ["node_start_ms", "node_end_ms", "critical_edges"]
    if not include_execution:
        omitted.extend(["timeline", "graph", "critical_chain", "component_costs", "resource_costs"])
    for key in omitted:
        result.pop(key, None)
    return result


def _search_view(value: dict[str, Any], *, candidate_limit: int = 100) -> dict[str, Any]:
    """Project a persisted search to a bounded, reviewable Desktop result."""

    result = copy.deepcopy(value)
    candidates = result.get("candidates") if isinstance(result.get("candidates"), list) else []
    # The shared page renders the winner's predicted DAG, timeline, critical
    # chain and cost detail. Keep those fields only for the winner; projected
    # comparison rows remain bounded.
    result["best"] = _candidate_view(result.get("best"), include_execution=True)
    result["candidates"] = [_candidate_view(item) for item in candidates[:candidate_limit]]
    result["core_combination_recommendations"] = [
        _candidate_view(item) for item in result.get("core_combination_recommendations", [])
    ]
    result["pareto"] = [_candidate_view(item) for item in result.get("pareto", [])[:candidate_limit]]
    result["transport_projection"] = {
        "schema_version": "desktop_search_projection/v1",
        "full_result_persisted": True,
        "total_candidates": len(candidates),
        "returned_candidates": min(len(candidates), candidate_limit),
        "omitted": ["non_winner.graph", "non_winner.timeline", "candidate.node_times", "non_winner.component_costs", "non_winner.resource_costs"],
    }
    return result


def _analysis_summary(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value[key])
        for key in ("id", "analysis_id", "dataset_id", "name", "status", "created_at", "feature_profile_id")
        if key in value
    }


def _search_summary(value: dict[str, Any]) -> dict[str, Any]:
    return {
        key: copy.deepcopy(value[key])
        for key in ("id", "search_id", "analysis_id", "dataset_id", "status", "created_at", "objective", "feature_profile_id")
        if key in value
    }


def _plan_summary(value: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: copy.deepcopy(value[key])
        for key in ("id", "name", "created_at", "hardware_profile_id", "parameter_groups", "repeats")
        if key in value
    }
    result["condition_count"] = len(value.get("conditions", [])) if isinstance(value.get("conditions"), list) else 0
    return result


def _fit_summary(value: dict[str, Any]) -> dict[str, Any]:
    result = {
        key: copy.deepcopy(value[key])
        for key in ("id", "plan_id", "name", "status", "created_at", "hardware_profile_id", "quality")
        if key in value
    }
    result["frequency_curve_observations"] = _frequency_curve_observations(value)
    return result


def _frequency_curve_observations(value: dict[str, Any]) -> dict[str, Any]:
    """Project raw calibration samples into a small curve-display payload.

    A portable fit can retain several MiB of audit evidence.  The Desktop list
    response only needs the accepted frequency-proxy medians, so aggregate by
    core group, workload class and OPP instead of transferring the full fit.
    """
    quality = value.get("quality") if isinstance(value.get("quality"), dict) else {}
    rejected = {
        str(item.get("sample_id"))
        for item in quality.get("rejected_sample_details", [])
        if isinstance(item, dict) and item.get("sample_id")
    }
    rejected_indices = {
        item.get("index")
        for item in quality.get("rejected_sample_details", [])
        if isinstance(item, dict) and type(item.get("index")) is int and item["index"] >= 0
    }
    measurements = value.get("measurements") if isinstance(value.get("measurements"), dict) else {}
    grouped: dict[tuple[str, str, int], dict[str, Any]] = {}
    for sample_index, sample in enumerate(measurements.get("samples", [])):
        if not isinstance(sample, dict):
            continue
        sample_id = str(sample.get("sample_id") or "")
        workload_id = str(sample.get("workload_id") or "")
        core_kind = str(sample.get("core_kind") or "")
        workload_class = str(sample.get("workload_class") or "")
        frequency = sample.get("frequency_khz")
        cpu_ms = sample.get("cpu_ms")
        work_units = sample.get("work_units")
        numeric = (frequency, cpu_ms, work_units)
        if (sample.get("benchmark") != "pair_work" or sample.get("active_tasks") != 1
                or "frequency_proxy" not in workload_id or not core_kind or not workload_class
                or any(isinstance(item, bool) or not isinstance(item, (int, float)) or item <= 0
                       for item in numeric)
                or sample_index in rejected_indices or sample_id in rejected
                or sample.get("affinity_verified") is False
                or sample.get("frequency_verified") is False or sample.get("thermal_throttled") is True):
            continue
        key = (core_kind, workload_class, int(frequency))
        entry = grouped.setdefault(key, {
            "values": [], "splits": set(), "workload_ids": set(),
            "workload_versions": set(), "working_set_bytes": set(),
            "instruction_intensities": [],
            "pmu_ipc": [], "pmu_cache_misses": [], "pmu_cache_references": [],
            "pmu_cache_misses_per_work_unit": [], "pmu_cache_mpki": [],
            "pmu_statuses": set(), "pmu_complete_sample_count": 0,
        })
        entry["values"].append(float(cpu_ms) / float(work_units))
        if isinstance(sample.get("split"), str) and sample["split"]:
            entry["splits"].add(sample["split"])
        entry["workload_ids"].add(workload_id)
        if isinstance(sample.get("workload_version"), str) and sample["workload_version"]:
            entry["workload_versions"].add(sample["workload_version"])
        working_set = sample.get("working_set_bytes")
        if isinstance(working_set, (int, float)) and not isinstance(working_set, bool) and working_set > 0:
            entry["working_set_bytes"].add(int(working_set))
        intensity = sample.get("instruction_intensity_instructions_per_dram_byte")
        if not isinstance(intensity, (int, float)) or isinstance(intensity, bool) or intensity <= 0:
            instructions, dram_bytes = sample.get("pmu_retired_instructions"), sample.get("pmu_dram_bytes")
            if (sample.get("pmu_window_aligned") is True
                    and isinstance(instructions, (int, float)) and not isinstance(instructions, bool)
                    and isinstance(dram_bytes, (int, float)) and not isinstance(dram_bytes, bool)
                    and instructions > 0 and dram_bytes > 0):
                intensity = float(instructions) / float(dram_bytes)
            else:
                intensity = None
        if intensity is not None:
            entry["instruction_intensities"].append(float(intensity))
        pmu_status = sample.get("pmu_collection_status")
        if isinstance(pmu_status, str) and pmu_status in {"collected", "partial", "unavailable"}:
            entry["pmu_statuses"].add(pmu_status)
        complete_pmu = True
        for field in ("pmu_ipc", "pmu_cache_misses", "pmu_cache_references", "pmu_cache_mpki"):
            metric = sample.get(field)
            if (isinstance(metric, (int, float)) and not isinstance(metric, bool)
                    and math.isfinite(metric) and metric >= 0):
                entry[field].append(float(metric))
                if field == "pmu_cache_misses":
                    # Cache misses are counts for the whole timed invocation.
                    # Keep that raw median for audit, but normalize before
                    # comparing samples whose work_units differ.
                    entry["pmu_cache_misses_per_work_unit"].append(
                        float(metric) / float(work_units))
            else:
                complete_pmu = False
        if complete_pmu:
            entry["pmu_complete_sample_count"] += 1
    points = []
    for (core_kind, workload_class, frequency), entry in sorted(grouped.items()):
        values = sorted(entry["values"])
        middle = len(values) // 2
        median = values[middle] if len(values) % 2 else (values[middle - 1] + values[middle]) / 2
        intensity_values = sorted(entry["instruction_intensities"])
        intensity_middle = len(intensity_values) // 2
        intensity_median = (intensity_values[intensity_middle] if len(intensity_values) % 2
                            else (intensity_values[intensity_middle - 1] + intensity_values[intensity_middle]) / 2
                            if intensity_values else None)
        def metric_median(field: str) -> float | None:
            metric_values = sorted(entry[field])
            if not metric_values:
                return None
            metric_middle = len(metric_values) // 2
            return (metric_values[metric_middle] if len(metric_values) % 2
                    else (metric_values[metric_middle - 1] + metric_values[metric_middle]) / 2)

        pmu_metric_counts = {field: len(entry[field]) for field in
                             ("pmu_ipc", "pmu_cache_misses", "pmu_cache_references", "pmu_cache_mpki")}
        points.append({
            "core_kind": core_kind,
            "workload_class": workload_class,
            "frequency_khz": frequency,
            "unit_cpu_ms": median,
            "repeat_count": len(values),
            "splits": sorted(entry["splits"]),
            "workload_ids": sorted(entry["workload_ids"]),
            "workload_versions": sorted(entry["workload_versions"]),
            "working_set_bytes": sorted(entry["working_set_bytes"]),
            "instruction_intensity_instructions_per_dram_byte": intensity_median,
            "instruction_intensity_sample_count": len(intensity_values),
            "pmu_status": ("collected" if entry["pmu_statuses"] == {"collected"}
                           else "partial" if any(pmu_metric_counts.values()) else "unavailable"),
            "pmu_ipc": metric_median("pmu_ipc"),
            "pmu_cache_misses": metric_median("pmu_cache_misses"),
            "pmu_cache_misses_per_work_unit": metric_median(
                "pmu_cache_misses_per_work_unit"),
            "pmu_cache_references": metric_median("pmu_cache_references"),
            "pmu_cache_mpki": metric_median("pmu_cache_mpki"),
            "pmu_metric_sample_counts": pmu_metric_counts,
            "pmu_sample_count": entry["pmu_complete_sample_count"],
        })
    return {
        "schema_version": "calibration_frequency_curve_observations/v5",
        "points": points,
        "accepted_sample_count": sum(point["repeat_count"] for point in points),
    }


def _fit_view(value: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(value)
    result["frequency_curve_observations"] = _frequency_curve_observations(value)
    result.pop("measurements", None)
    result.pop("validation_predictions", None)
    result["transport_projection"] = {
        "schema_version": "desktop_calibration_fit_projection/v1",
        "full_result_persisted": True,
        "omitted": ["measurements", "validation_predictions"],
    }
    return result


def _state_root() -> Path:
    raw = os.environ.get("HETERO_STATE_ROOT", "").strip()
    if not raw:
        raise PlatformError(
            "DESKTOP_STATE_ROOT_MISSING",
            "服务器环境尚未初始化 HETERO_STATE_ROOT。请先在 Desktop 的“服务器环境”中初始化托管目录。",
            status_code=409,
        )
    return Path(raw).expanduser().resolve()


def _record(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise PlatformError("DESKTOP_RPC_BODY_INVALID", "请求 body 必须是 JSON 对象。", status_code=422)
    return value


def _capture_upload_contract(body: dict[str, Any], *, token_required: bool) -> tuple[str, int, str]:
    token = body.get("upload_token", "")
    digest = body.get("archive_sha256", "")
    size = body.get("archive_size_bytes")
    if token_required and (not isinstance(token, str) or not DESKTOP_CAPTURE_TOKEN.fullmatch(token)):
        raise PlatformError("DESKTOP_CAPTURE_TOKEN_INVALID", "采集包上传标识无效。", status_code=422)
    if not isinstance(digest, str) or not DESKTOP_CAPTURE_SHA256.fullmatch(digest):
        raise PlatformError("DESKTOP_CAPTURE_HASH_INVALID", "采集包 SHA-256 无效。", status_code=422)
    limit = LocalCapturePackageLimits().max_archive_bytes
    if isinstance(size, bool) or not isinstance(size, int) or size < 1 or size > limit:
        raise PlatformError("DESKTOP_CAPTURE_SIZE_INVALID", "采集包大小无效或超过限制。", status_code=422,
                            details={"max_bytes": limit})
    return str(token), size, digest


def _capture_upload_root(state_root: Path) -> Path:
    root = state_root / DESKTOP_CAPTURE_UPLOAD_DIRECTORY
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    current = root.lstat()
    if root.is_symlink() or not stat.S_ISDIR(current.st_mode) or not root.resolve().is_relative_to(state_root):
        raise PlatformError("DESKTOP_CAPTURE_STAGING_UNSAFE", "采集包暂存目录不安全。", status_code=409)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def _capture_upload_paths(state_root: Path, token: str) -> tuple[Path, Path]:
    root = _capture_upload_root(state_root)
    return root / f"{token}.capture.zip", root / f"{token}.reservation.json"


def _expire_staged_captures(state_root: Path, *, now: float | None = None) -> int:
    """Remove only stale token-shaped files from the private staging root."""
    root = _capture_upload_root(state_root)
    cutoff = (time.time() if now is None else now) - DESKTOP_CAPTURE_STAGING_TTL_SECONDS
    expired: set[str] = set()
    for path in root.iterdir():
        match = re.fullmatch(r"([0-9a-f]{32})\.(?:capture\.zip|reservation\.json)", path.name)
        if not match:
            continue
        try:
            current = path.lstat()
        except FileNotFoundError:
            continue
        if current.st_mtime < cutoff:
            expired.add(match.group(1))
    for token in expired:
        archive, reservation = _capture_upload_paths(state_root, token)
        _unlink_staging_file(archive)
        _unlink_staging_file(reservation)
    return len(expired)


def _stage_capture_upload(state_root: Path, body: dict[str, Any]) -> dict[str, Any]:
    _, size, digest = _capture_upload_contract(body, token_required=False)
    _expire_staged_captures(state_root)
    for _ in range(8):
        token = secrets.token_hex(16)
        archive, reservation = _capture_upload_paths(state_root, token)
        try:
            archive.lstat()
            continue
        except FileNotFoundError:
            pass
        try:
            descriptor = os.open(reservation, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        value = {"schema_version": "desktop_capture_upload_reservation/v1", "upload_token": token,
                 "archive_sha256": digest, "archive_size_bytes": size, "created_at": utc_now()}
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(value, output, ensure_ascii=False, sort_keys=True)
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
        except Exception:
            reservation.unlink(missing_ok=True)
            raise
        return {"schema_version": "desktop_capture_upload_staging/v1", "upload_token": token,
                "remote_path": str(archive), "archive_sha256": digest, "archive_size_bytes": size}
    raise PlatformError("DESKTOP_CAPTURE_STAGING_BUSY", "无法分配采集包暂存位置，请重试。", status_code=503)


def _read_capture_reservation(path: Path, *, token: str, digest: str, size: int) -> None:
    try:
        current = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(current.st_mode):
            raise OSError("reservation is not a regular file")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlatformError("DESKTOP_CAPTURE_RESERVATION_MISSING", "采集包上传预约不存在或已失效。",
                            status_code=409, details={"reason": str(exc)}) from exc
    expected = (value.get("upload_token"), value.get("archive_sha256"), value.get("archive_size_bytes"))
    if expected != (token, digest, size):
        raise PlatformError("DESKTOP_CAPTURE_RESERVATION_MISMATCH", "采集包上传参数与预约不一致。", status_code=409)


def _unlink_staging_file(path: Path) -> None:
    try:
        current = path.lstat()
        if path.is_symlink() or stat.S_ISREG(current.st_mode):
            path.unlink()
    except FileNotFoundError:
        pass


def _import_staged_capture(state_root: Path, manager: ParallelModelingManager, body: dict[str, Any]) -> dict[str, Any]:
    token, size, digest = _capture_upload_contract(body, token_required=True)
    archive, reservation = _capture_upload_paths(state_root, token)
    try:
        _read_capture_reservation(reservation, token=token, digest=digest, size=size)
        try:
            current = archive.lstat()
        except OSError as exc:
            raise PlatformError("DESKTOP_CAPTURE_UPLOAD_MISSING", "尚未找到已上传的采集包。", status_code=409) from exc
        if archive.is_symlink() or not stat.S_ISREG(current.st_mode):
            raise PlatformError("DESKTOP_CAPTURE_UPLOAD_UNSAFE", "上传目标不是普通采集包文件。", status_code=409)
        imported = LocalCapturePackageImporter(state_root).import_zip(
            archive, project_id="local-default", expected_sha256=digest, expected_size_bytes=size)
        dataset_imports = import_capture_datasets(manager, imported["run"])
        datasets = [item["dataset"] for item in dataset_imports if item.get("status") == "ready"]
        return {"schema_version": "desktop_capture_import/v1", "status": imported["status"],
                "run_id": imported["run"]["run_id"], "dataset_imports": dataset_imports,
                "datasets": datasets, "analysis_ready_count": len(datasets)}
    finally:
        # Both names are derived exclusively from the validated token.  Unlink
        # removes an attacker-created symlink itself and never follows it.
        _unlink_staging_file(archive)
        _unlink_staging_file(reservation)


def _import_staged_capture_runtime_trace(state_root: Path, body: dict[str, Any]) -> dict[str, Any]:
    """Import one Desktop capture step into the application-flow Trace catalog."""

    token, size, digest = _capture_upload_contract(body, token_required=True)
    archive, reservation = _capture_upload_paths(state_root, token)
    try:
        _read_capture_reservation(reservation, token=token, digest=digest, size=size)
        try:
            current = archive.lstat()
        except OSError as exc:
            raise PlatformError("DESKTOP_CAPTURE_UPLOAD_MISSING", "尚未找到已上传的采集包。", status_code=409) from exc
        if archive.is_symlink() or not stat.S_ISREG(current.st_mode):
            raise PlatformError("DESKTOP_CAPTURE_UPLOAD_UNSAFE", "上传目标不是普通采集包文件。", status_code=409)
        imported = LocalCapturePackageImporter(state_root).import_zip(
            archive, project_id="local-default", expected_sha256=digest, expected_size_bytes=size)
        result = materialize_capture_runtime_trace(
            state_root,
            imported["run"],
            step_id=str(body.get("step_id") or ""),
            source_connector_id=str(body.get("source_connector_id") or ""),
        )
        artifact = result["artifact"]
        quality = artifact.get("quality") if isinstance(artifact.get("quality"), dict) else {}
        trace_import = artifact.get("trace_import") if isinstance(artifact.get("trace_import"), dict) else {}
        return {
            "schema_version": "desktop_capture_runtime_trace_import/v1",
            "status": "ready",
            "run_id": imported["run"]["run_id"],
            "step_id": str(body.get("step_id") or ""),
            "trace": {
                "artifact_id": artifact["artifact_id"], "created_at": artifact.get("created_at"),
                "uploaded_at": trace_import.get("uploaded_at") or artifact.get("created_at"),
                "import_instance_id": trace_import.get("import_instance_id"),
                "trace_name": artifact.get("trace_name"), "input_kind": artifact.get("input_kind"),
                "analysis_profile": artifact.get("analysis_profile"),
                "source_connector_id": artifact.get("source_connector_id"),
                "stage_count": len(artifact.get("stage_catalog", [])),
                "hotspot_count": len(artifact.get("hotspot_candidates", [])),
                "quality_status": quality.get("status"),
            },
            "raw_input_artifact_id": result["raw_input_artifact"]["artifact_id"],
            "extraction": result["extraction"],
        }
    finally:
        _unlink_staging_file(archive)
        _unlink_staging_file(reservation)


def _discard_staged_capture(state_root: Path, body: dict[str, Any]) -> dict[str, Any]:
    """Remove one token-bound upload reservation without importing it.

    The operation is idempotent after a completed import.  If either staged
    file still exists, a valid reservation must bind the token, digest and
    size before anything is removed.
    """

    token, size, digest = _capture_upload_contract(body, token_required=True)
    archive, reservation = _capture_upload_paths(state_root, token)
    try:
        reservation.lstat()
    except FileNotFoundError:
        try:
            archive.lstat()
        except FileNotFoundError:
            return {"schema_version": "desktop_capture_discard/v1", "status": "absent", "discarded": False}
        raise PlatformError(
            "DESKTOP_CAPTURE_RESERVATION_MISSING",
            "采集包上传预约不存在，未删除孤立文件。",
            status_code=409,
        )
    _read_capture_reservation(reservation, token=token, digest=digest, size=size)
    _unlink_staging_file(archive)
    _unlink_staging_file(reservation)
    return {"schema_version": "desktop_capture_discard/v1", "status": "discarded", "discarded": True}


def _measurement_contract(body: dict[str, Any], *, token_required: bool,
                          plan_required: bool = True) -> tuple[str, str, int, str, str]:
    plan_id = body.get("plan_id", "")
    token = body.get("upload_token", "")
    digest = body.get("measurement_sha256", "")
    size = body.get("measurement_size_bytes")
    filename = str(body.get("filename") or "measurements.json")
    if plan_required and (not isinstance(plan_id, str) or not re.fullmatch(r"cp_[0-9a-f]{24}", plan_id)):
        raise PlatformError("DESKTOP_CALIBRATION_PLAN_INVALID", "微基准实测文件缺少有效校准方案。", status_code=422)
    if not plan_required and plan_id and (not isinstance(plan_id, str) or not re.fullmatch(r"cp_[0-9a-f]{24}", plan_id)):
        raise PlatformError("DESKTOP_CALIBRATION_PLAN_INVALID", "微基准实测文件校准方案无效。", status_code=422)
    if token_required and (not isinstance(token, str) or not DESKTOP_CAPTURE_TOKEN.fullmatch(token)):
        raise PlatformError("DESKTOP_CALIBRATION_TOKEN_INVALID", "微基准实测文件上传标识无效。", status_code=422)
    if not isinstance(digest, str) or not DESKTOP_CAPTURE_SHA256.fullmatch(digest):
        raise PlatformError("DESKTOP_CALIBRATION_HASH_INVALID", "微基准实测文件 SHA-256 无效。", status_code=422)
    if isinstance(size, bool) or not isinstance(size, int) or not 1 <= size <= CALIBRATION_MEASUREMENT_MAX_BYTES:
        raise PlatformError("DESKTOP_CALIBRATION_SIZE_INVALID", "微基准实测文件为空或超过20 MiB。", status_code=422,
                            details={"max_bytes": CALIBRATION_MEASUREMENT_MAX_BYTES})
    filename = Path(filename).name[:255] or "measurements.json"
    return str(token), plan_id, size, digest, filename


def _measurement_upload_root(state_root: Path) -> Path:
    root = state_root / CALIBRATION_MEASUREMENT_UPLOAD_DIRECTORY
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    current = root.lstat()
    if root.is_symlink() or not stat.S_ISDIR(current.st_mode) or not root.resolve().is_relative_to(state_root):
        raise PlatformError("DESKTOP_CALIBRATION_STAGING_UNSAFE", "微基准实测暂存目录不安全。", status_code=409)
    try:
        root.chmod(0o700)
    except OSError:
        pass
    return root


def _measurement_upload_paths(state_root: Path, token: str) -> tuple[Path, Path]:
    root = _measurement_upload_root(state_root)
    return root / f"{token}.measurements.json", root / f"{token}.reservation.json"


def _expire_measurement_uploads(state_root: Path, *, now: float | None = None) -> int:
    root = _measurement_upload_root(state_root)
    cutoff = (time.time() if now is None else now) - CALIBRATION_MEASUREMENT_STAGING_TTL_SECONDS
    expired: set[str] = set()
    for path in root.iterdir():
        match = re.fullmatch(r"([0-9a-f]{32})\.(?:measurements|reservation)\.json", path.name)
        if not match:
            continue
        try:
            current = path.lstat()
        except FileNotFoundError:
            continue
        if current.st_mtime < cutoff:
            expired.add(match.group(1))
    for token in expired:
        measurement, reservation = _measurement_upload_paths(state_root, token)
        _unlink_staging_file(measurement)
        _unlink_staging_file(reservation)
    return len(expired)


def _stage_measurements(state_root: Path, calibration, body: dict[str, Any]) -> dict[str, Any]:
    _, plan_id, size, digest, filename = _measurement_contract(body, token_required=False)
    calibration.get_plan(plan_id)
    _expire_measurement_uploads(state_root)
    for _ in range(8):
        token = secrets.token_hex(16)
        measurement, reservation = _measurement_upload_paths(state_root, token)
        try:
            measurement.lstat()
            continue
        except FileNotFoundError:
            pass
        try:
            descriptor = os.open(reservation, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        value = {"schema_version": "desktop_calibration_measurement_reservation/v1",
                 "upload_token": token, "plan_id": plan_id,
                 "measurement_sha256": digest, "measurement_size_bytes": size,
                 "filename": filename, "created_at": utc_now()}
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(value, output, ensure_ascii=False, sort_keys=True)
                output.write("\n"); output.flush(); os.fsync(output.fileno())
        except Exception:
            reservation.unlink(missing_ok=True)
            raise
        return {"schema_version": "desktop_calibration_measurement_staging/v1",
                "upload_token": token, "remote_path": str(measurement),
                "plan_id": plan_id, "measurement_sha256": digest,
                "measurement_size_bytes": size}
    raise PlatformError("DESKTOP_CALIBRATION_STAGING_BUSY", "无法分配微基准实测暂存位置，请重试。", status_code=503)


def _read_measurement_reservation(path: Path, *, token: str, plan_id: str, digest: str, size: int) -> None:
    try:
        current = path.lstat()
        if path.is_symlink() or not stat.S_ISREG(current.st_mode):
            raise OSError("reservation is not a regular file")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlatformError("DESKTOP_CALIBRATION_RESERVATION_MISSING", "微基准实测上传预约不存在或已失效。",
                            status_code=409, details={"reason": str(exc)}) from exc
    expected = (value.get("upload_token"), value.get("measurement_sha256"), value.get("measurement_size_bytes"))
    if expected != (token, digest, size) or plan_id and value.get("plan_id") != plan_id:
        raise PlatformError("DESKTOP_CALIBRATION_RESERVATION_MISMATCH", "微基准实测上传参数与预约不一致。", status_code=409)


def _fit_staged_measurements(state_root: Path, calibration, body: dict[str, Any]) -> dict[str, Any]:
    token, plan_id, size, digest, _ = _measurement_contract(body, token_required=True)
    measurement, reservation = _measurement_upload_paths(state_root, token)
    try:
        _read_measurement_reservation(reservation, token=token, plan_id=plan_id, digest=digest, size=size)
        descriptor = -1
        no_follow = getattr(os, "O_NOFOLLOW", None)
        if no_follow is None:
            raise PlatformError("DESKTOP_CALIBRATION_UPLOAD_UNSAFE", "当前服务器不支持安全打开微基准实测文件。", status_code=503)
        try:
            descriptor = os.open(
                measurement,
                os.O_RDONLY | no_follow | getattr(os, "O_CLOEXEC", 0),
            )
        except FileNotFoundError as exc:
            raise PlatformError("DESKTOP_CALIBRATION_UPLOAD_MISSING", "尚未找到已上传的微基准实测文件。", status_code=409) from exc
        except OSError as exc:
            raise PlatformError("DESKTOP_CALIBRATION_UPLOAD_UNSAFE", "无法安全打开已上传的微基准实测文件。", status_code=409,
                                details={"reason": str(exc)}) from exc
        try:
            current = os.fstat(descriptor)
            if not stat.S_ISREG(current.st_mode):
                raise PlatformError("DESKTOP_CALIBRATION_UPLOAD_UNSAFE", "上传目标不是普通微基准实测文件。", status_code=409)
            if current.st_size != size:
                raise PlatformError("DESKTOP_CALIBRATION_SIZE_MISMATCH", "微基准实测文件大小与预约不一致。", status_code=409)
            stream = os.fdopen(descriptor, "rb")
            descriptor = -1
            with stream:
                contents = stream.read(size + 1)
        except PlatformError:
            raise
        except OSError as exc:
            raise PlatformError("DESKTOP_CALIBRATION_UPLOAD_UNSAFE", "无法安全读取已上传的微基准实测文件。", status_code=409,
                                details={"reason": str(exc)}) from exc
        finally:
            if descriptor >= 0:
                os.close(descriptor)
        if len(contents) != size:
            raise PlatformError("DESKTOP_CALIBRATION_SIZE_MISMATCH", "微基准实测文件大小与预约不一致。", status_code=409)
        measured_digest = hashlib.sha256(contents).hexdigest()
        if measured_digest != digest:
            raise PlatformError("DESKTOP_CALIBRATION_HASH_MISMATCH", "微基准实测文件完整性校验失败。", status_code=409)
        try:
            measurements = json.loads(contents.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PlatformError("DESKTOP_CALIBRATION_JSON_INVALID", "微基准实测文件不是有效 UTF-8 JSON。", status_code=422) from exc
        return calibration.fit({"plan_id": plan_id, "measurements": measurements})
    finally:
        _unlink_staging_file(measurement)
        _unlink_staging_file(reservation)


def _discard_staged_measurements(state_root: Path, body: dict[str, Any]) -> dict[str, Any]:
    token, plan_id, size, digest, _ = _measurement_contract(body, token_required=True, plan_required=False)
    measurement, reservation = _measurement_upload_paths(state_root, token)
    try:
        reservation.lstat()
    except FileNotFoundError:
        if not measurement.exists():
            return {"schema_version": "desktop_calibration_measurement_discard/v1", "status": "absent", "discarded": False}
        raise PlatformError("DESKTOP_CALIBRATION_RESERVATION_MISSING", "微基准实测上传预约不存在，未删除孤立文件。", status_code=409)
    _read_measurement_reservation(reservation, token=token, plan_id=plan_id, digest=digest, size=size)
    _unlink_staging_file(measurement)
    _unlink_staging_file(reservation)
    return {"schema_version": "desktop_calibration_measurement_discard/v1", "status": "discarded", "discarded": True}


def dispatch(
    request: dict[str, Any],
    *,
    manager_cache: _ParallelModelingManagerCache | None = None,
) -> dict[str, Any]:
    """Dispatch one explicitly supported Desktop analysis request."""

    method = str(request.get("method") or "GET").upper()
    path = str(request.get("path") or "")
    body = _record(request.get("body"))
    state_root = _state_root()
    workspace_result = dispatch_workspace(request, state_root)
    if workspace_result is not None:
        if workspace_result.get("__desktop_response_kind__") == "text":
            return {
                "kind": "text",
                "content": str(workspace_result.get("content") or ""),
                "content_type": str(workspace_result.get("content_type") or "text/plain; charset=utf-8"),
                "filename": str(workspace_result.get("filename") or "download.txt"),
            }
        return {"kind": "json", "value": workspace_result}
    manager = (
        manager_cache.get(state_root)
        if manager_cache is not None
        else ParallelModelingManager(
            state_root,
            artifact_store=None,
            portable_calibration_package_root=PORTABLE_CALIBRATION_PACKAGE_ROOT,
        )
    )

    if method == "POST" and path == "/v1/desktop-capture/staging":
        return {"kind": "json", "value": _stage_capture_upload(state_root, body)}
    if method == "POST" and path == "/v1/desktop-capture/import-package":
        return {"kind": "json", "value": _import_staged_capture(state_root, manager, body)}
    if method == "POST" and path == "/v1/desktop-capture/import-runtime-trace":
        return {"kind": "json", "value": _import_staged_capture_runtime_trace(state_root, body)}
    if method == "POST" and path == "/v1/desktop-capture/discard-staging":
        return {"kind": "json", "value": _discard_staged_capture(state_root, body)}

    if method == "GET" and path == "/v1/parallel-modeling/datasets":
        return {"kind": "json", "value": {"datasets": manager.list_datasets()}}
    if method == "GET" and path == "/v1/parallel-modeling/catalog":
        return {"kind": "json", "value": manager.catalog()}
    if method == "GET" and path == "/v1/parallel-modeling/hardware-profiles":
        return {"kind": "json", "value": {"profiles": manager.list_hardware_profiles()}}
    if method == "POST" and path == "/v1/parallel-modeling/hardware-profiles":
        return {"kind": "json", "value": manager.create_hardware_profile(body)}
    match = HARDWARE_PROFILE_ARCHIVE_PATH.fullmatch(path)
    if method == "POST" and match:
        return {"kind": "json", "value": manager.archive_hardware_profile(match.group(1))}
    match = HARDWARE_PROFILE_RESTORE_PATH.fullmatch(path)
    if method == "POST" and match:
        return {"kind": "json", "value": manager.restore_hardware_profile(match.group(1))}
    match = HARDWARE_PROFILE_PATH.fullmatch(path)
    if method == "GET" and match:
        return {"kind": "json", "value": manager.get_hardware_profile(match.group(1))}
    if method == "POST" and path == "/v1/parallel-modeling/datasets":
        return {"kind": "json", "value": manager.import_dataset(body)}
    match = DATASET_HARDWARE_BINDING_PATH.fullmatch(path)
    if method == "POST" and match:
        return {"kind": "json", "value": manager.bind_dataset_hardware(match.group(1), body)}
    match = DATASET_PATH.fullmatch(path)
    if method == "GET" and match:
        return {"kind": "json", "value": manager.get_dataset(match.group(1))}
    if method == "GET" and path == "/v1/parallel-modeling/analyses":
        return {"kind": "json", "value": {"analyses": [_analysis_summary(item) for item in manager.list_analyses()]}}
    if method == "POST" and path == "/v1/parallel-modeling/analyses":
        return {"kind": "json", "value": _analysis_view(manager.analyze(body))}
    match = ANALYSIS_PATH.fullmatch(path)
    if method == "GET" and match:
        return {"kind": "json", "value": _analysis_view(manager.get_analysis(match.group(1)))}
    if method == "GET" and path == "/v1/parallel-modeling/searches":
        return {"kind": "json", "value": {"searches": [_search_summary(item) for item in manager.list_searches()]}}
    if method == "POST" and path == "/v1/parallel-modeling/searches":
        return {"kind": "json", "value": _search_view(manager.search(body))}
    match = SEARCH_PATH.fullmatch(path)
    if method == "GET" and match:
        return {"kind": "json", "value": _search_view(manager.get_search(match.group(1)))}
    if method == "GET" and path == "/v1/workload-features/catalog":
        return {"kind": "json", "value": manager.workload_features.catalog()}
    if method == "GET" and path == "/v1/workload-features/profiles":
        return {"kind": "json", "value": {"profiles": manager.workload_features.list_profiles()}}

    match = PROFILE_PATH.fullmatch(path)
    if method == "GET" and match:
        return {"kind": "json", "value": manager.workload_features.get_profile(match.group(1))}

    if method == "POST" and path == "/v1/workload-features/analyze":
        return {"kind": "json", "value": manager.workload_features.analyze(body)}

    calibration = manager.calibration
    if method == "POST" and path == "/v1/parallel-calibration/measurement-staging":
        return {"kind": "json", "value": _stage_measurements(state_root, calibration, body)}
    if method == "POST" and path == "/v1/parallel-calibration/fits/from-staging":
        return {"kind": "json", "value": _fit_view(_fit_staged_measurements(state_root, calibration, body))}
    if method == "POST" and path == "/v1/parallel-calibration/measurement-staging/discard":
        return {"kind": "json", "value": _discard_staged_measurements(state_root, body)}
    if method == "GET" and path == "/v1/parallel-calibration/catalog":
        return {"kind": "json", "value": calibration.catalog()}
    if method == "GET" and path == "/v1/parallel-calibration/plans":
        return {"kind": "json", "value": {"plans": [_plan_summary(item) for item in calibration.list_plans()]}}
    if method == "POST" and path == "/v1/parallel-calibration/plans":
        return {"kind": "json", "value": calibration.create_plan(body)}
    match = CALIBRATION_PLAN_PATH.fullmatch(path)
    if method == "GET" and match:
        return {"kind": "json", "value": calibration.get_plan(match.group(1))}
    match = CALIBRATION_BUNDLE_PATH.fullmatch(path)
    if method == "GET" and match:
        return {"kind": "json", "value": calibration.bundle(match.group(1))}
    if method == "GET" and path == "/v1/parallel-calibration/fits":
        return {"kind": "json", "value": {"fits": [_fit_summary(item) for item in calibration.list_fits()]}}
    match = CALIBRATION_FIT_PATH.fullmatch(path)
    if method == "GET" and match:
        return {"kind": "json", "value": _fit_view(calibration.get_fit(match.group(1)))}
    if method == "GET" and path == "/v1/parallel-calibration/versions":
        return {"kind": "json", "value": {"versions": calibration.list_versions()}}
    if method == "POST" and path == "/v1/parallel-calibration/versions":
        return {"kind": "json", "value": calibration.publish(body)}

    match = EXPORT_PATH.fullmatch(path)
    if method == "GET" and match:
        profile = manager.workload_features.get_profile(match.group(1))
        view = match.group(2)
        content = export_profile_csv(profile, view)
        if isinstance(content, bytes):
            content = content.decode("utf-8")
        return {
            "kind": "text",
            "content": content,
            "content_type": "text/csv; charset=utf-8",
            "filename": f'{profile["id"]}-{view}.csv',
        }

    raise PlatformError(
        "DESKTOP_RPC_ROUTE_NOT_ALLOWED",
        "Desktop 分析传输未开放该操作。",
        status_code=404,
        details={"method": method, "path": path},
    )


def _decode_request(encoded: str) -> dict[str, Any]:
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError) as exc:
        raise PlatformError("DESKTOP_RPC_REQUEST_INVALID", "Desktop RPC 请求编码无效。", status_code=422) from exc
    if len(raw) > MAX_REQUEST_BYTES:
        raise PlatformError("DESKTOP_RPC_REQUEST_TOO_LARGE", "Desktop RPC 请求超过 12 MiB。", status_code=413)
    try:
        value = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise PlatformError("DESKTOP_RPC_REQUEST_INVALID", "Desktop RPC 请求不是有效 JSON。", status_code=422) from exc
    if not isinstance(value, dict) or value.get("schema_version") != SCHEMA_VERSION:
        raise PlatformError("DESKTOP_RPC_REQUEST_INVALID", "Desktop RPC 请求契约版本无效。", status_code=422)
    return value


def response_envelope(
    request: dict[str, Any],
    *,
    manager_cache: _ParallelModelingManagerCache | None = None,
) -> dict[str, Any]:
    try:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": True,
            "result": dispatch(request, manager_cache=manager_cache),
        }
    except PlatformError as exc:
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "error": {
                "code": exc.code,
                "message": str(exc),
                "details": getattr(exc, "details", {}) or {},
                "status_code": getattr(exc, "status_code", 500),
            },
        }
    except Exception as exc:  # Keep the CLI boundary structured for Desktop.
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "error": {"code": "DESKTOP_RPC_FAILED", "message": str(exc), "details": {}, "status_code": 500},
        }


def encode_response_line(response: dict[str, Any]) -> str:
    """Encode one bounded response while preserving the full result payload."""

    raw = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode()
    if len(raw) > MAX_RESPONSE_JSON_BYTES:
        response = {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "error": {
                "code": "DESKTOP_RPC_RESPONSE_TOO_LARGE",
                "message": "Desktop RPC 结果超过 8 MiB，请缩小分析范围或通过 Artifact / SFTP 读取。",
                "details": {"actual_bytes": len(raw), "max_bytes": MAX_RESPONSE_JSON_BYTES},
                "status_code": 413,
            },
        }
        raw = json.dumps(response, ensure_ascii=False, separators=(",", ":")).encode()
    if len(raw) >= RESPONSE_COMPRESSION_THRESHOLD:
        encoded = base64.b64encode(gzip.compress(raw, compresslevel=6, mtime=0)).decode()
        marker = RPC_GZIP_MARKER
    else:
        encoded = base64.b64encode(raw).decode()
        marker = RPC_MARKER
    if len(encoded.encode("ascii")) > MAX_RESPONSE_WIRE_BYTES:
        fallback = {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "error": {
                "code": "DESKTOP_RPC_RESPONSE_TOO_LARGE",
                "message": "Desktop RPC 压缩结果仍超过安全传输上限，请通过 Artifact / SFTP 读取。",
                "details": {"max_wire_bytes": MAX_RESPONSE_WIRE_BYTES},
                "status_code": 413,
            },
        }
        return RPC_MARKER + base64.b64encode(
            json.dumps(fallback, ensure_ascii=False, separators=(",", ":")).encode()
        ).decode()
    return marker + encoded


def _request_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, PlatformError):
        return {
            "schema_version": SCHEMA_VERSION,
            "ok": False,
            "error": {
                "code": exc.code,
                "message": str(exc),
                "details": getattr(exc, "details", {}) or {},
                "status_code": getattr(exc, "status_code", 500),
            },
        }
    return {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "error": {"code": "DESKTOP_RPC_FAILED", "message": str(exc), "details": {}, "status_code": 500},
    }


def _serve_stdio() -> int:
    """Serve framed requests on one long-lived SSH exec channel."""

    manager_cache = _ParallelModelingManagerCache()
    sys.stdout.write(f"{RPC_STREAM_READY_MARKER}{RPC_STREAM_PROTOCOL}\n")
    sys.stdout.flush()
    request_id_pattern = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
    for line in sys.stdin:
        request_id = "invalid"
        try:
            if len(line) > MAX_REQUEST_BYTES * 2:
                raise PlatformError(
                    "DESKTOP_RPC_REQUEST_TOO_LARGE",
                    "Desktop RPC 请求超过安全传输上限。",
                    status_code=413,
                )
            frame = json.loads(line)
            if not isinstance(frame, dict):
                raise PlatformError("DESKTOP_RPC_FRAME_INVALID", "Desktop RPC 流式请求帧无效。", status_code=422)
            request_id = str(frame.get("request_id", ""))
            if not request_id_pattern.fullmatch(request_id):
                request_id = "invalid"
                raise PlatformError("DESKTOP_RPC_FRAME_INVALID", "Desktop RPC 流式请求 ID 无效。", status_code=422)
            encoded = frame.get("request_base64")
            if not isinstance(encoded, str):
                raise PlatformError("DESKTOP_RPC_FRAME_INVALID", "Desktop RPC 流式请求缺少请求内容。", status_code=422)
            response = response_envelope(
                _decode_request(encoded),
                manager_cache=manager_cache,
            )
        except (PlatformError, json.JSONDecodeError, UnicodeError, TypeError, ValueError) as exc:
            response = _request_error(exc)
        sys.stdout.write(
            f"{RPC_STREAM_RESPONSE_MARKER}{request_id}\t{encode_response_line(response)}\n"
        )
        sys.stdout.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Execute allow-listed Desktop analysis requests")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--request-base64")
    mode.add_argument("--stdio-server", action="store_true")
    args = parser.parse_args(argv)
    if args.stdio_server:
        return _serve_stdio()
    try:
        request = _decode_request(args.request_base64)
        response = response_envelope(request)
    except PlatformError as exc:
        response = _request_error(exc)
    sys.stdout.write(encode_response_line(response) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
