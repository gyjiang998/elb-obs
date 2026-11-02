"""Materialize a captured ftrace step as auditable application-flow evidence.

Desktop capture packages contain the real trace, while application-flow
reconstruction consumes immutable ``trace_analysis_result`` artifacts.  This
adapter keeps those two inventories connected without pretending that ftrace
is a raw ``.htrace`` database: it extracts only observed HiTrace B/E spans and
publishes them as an ``external_trace_analysis_report``.
"""

from __future__ import annotations

from collections import defaultdict
import hashlib
from pathlib import Path
import re
from typing import Any

from backend.core import ArtifactStore, JsonRegistry, PlatformError
from backend.parallel_modeling.trace_parser import LINE_RE


_SAFE_CONNECTOR_ID = re.compile(r"[A-Za-z0-9_.:-]{1,160}")
_TRACE_SUFFIX = re.compile(r"\|[IM]\d+$")
_MAX_TRACE_BYTES = 256 * 1024 * 1024
_MAX_MARKER_LINES = 2_000_000
_MAX_EVENTS = 500
_MAX_OCCURRENCES_PER_EVENT = 8


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _trace_file(state_root: Path, manifest: dict[str, Any], step_id: str) -> tuple[dict[str, Any], Path]:
    steps = [item for item in manifest.get("steps", []) if isinstance(item, dict) and item.get("step_id") == step_id]
    if len(steps) != 1 or steps[0].get("captured") is not True:
        raise PlatformError("CAPTURE_RUNTIME_TRACE_STEP_INVALID", "所选采集步骤不存在或没有完成采集。", status_code=422)
    step = steps[0]
    if _record(step.get("trace_validation")).get("valid") is not True or _record(step.get("collector_status")).get("trace") == "invalid":
        raise PlatformError("CAPTURE_RUNTIME_TRACE_INVALID", "所选步骤的 Trace 未通过采集质量校验。", status_code=422)
    files = [item for item in manifest.get("files", []) if isinstance(item, dict)
             and item.get("step_id") == step_id and item.get("kind") == "trace"]
    if len(files) != 1:
        raise PlatformError("CAPTURE_RUNTIME_TRACE_FILE_INVALID", "所选步骤需要且只能包含一份 Trace。", status_code=422)
    entry = files[0]
    relative = Path(str(entry.get("managed_path") or ""))
    if relative.is_absolute() or ".." in relative.parts or relative.parts[:2] != ("scenario-capture", "runs"):
        raise PlatformError("CAPTURE_RUNTIME_TRACE_PATH_INVALID", "采集 Trace 不属于平台托管目录。", status_code=409)
    trace_path = state_root / relative
    try:
        info = trace_path.lstat()
    except OSError as exc:
        raise PlatformError("CAPTURE_RUNTIME_TRACE_MISSING", "采集 Trace 已移动或删除。", status_code=404) from exc
    if trace_path.is_symlink() or not trace_path.is_file() or info.st_size > _MAX_TRACE_BYTES:
        raise PlatformError("CAPTURE_RUNTIME_TRACE_PATH_INVALID", "采集 Trace 不是可分析的普通文件。", status_code=409)
    digest = hashlib.sha256(trace_path.read_bytes()).hexdigest()
    if digest != entry.get("sha256"):
        raise PlatformError("CAPTURE_RUNTIME_TRACE_HASH_MISMATCH", "采集 Trace 完整性校验失败。", status_code=409)
    return step, trace_path


def _marker_name(body: str) -> str:
    parts = body.split("|", 2)
    if len(parts) != 3:
        return ""
    value = _TRACE_SUFFIX.sub("", parts[2]).strip()
    return value[2:].strip() if value.startswith("H:") else value


def _observed_spans(trace_path: Path) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    stacks: dict[int, list[dict[str, Any]]] = defaultdict(list)
    aggregates: dict[tuple[str, str, int], dict[str, Any]] = {}
    marker_lines = matched_spans = unmatched_ends = 0
    truncated = False

    with trace_path.open("r", encoding="utf-8", errors="replace") as source:
        for raw_line in source:
            match = LINE_RE.match(raw_line)
            if not match or match["event"] != "tracing_mark_write":
                continue
            marker_lines += 1
            if marker_lines > _MAX_MARKER_LINES:
                truncated = True
                break
            body = match["body"]
            kind = body.split("|", 1)[0]
            tid = int(match["pid"])
            timestamp_ms = float(match["ts"]) * 1000.0
            if kind == "B":
                name = _marker_name(body)
                if name:
                    stacks[tid].append({
                        "name": name[:1000], "ts_ms": timestamp_ms,
                        "process_name": str(match["comm"]).strip()[:160] or "unknown",
                        "thread_name": str(match["comm"]).strip()[:160] or "unknown",
                        "pid": int(match["tgid"]) if str(match["tgid"] or "").isdigit() else tid,
                        "tid": tid,
                    })
                continue
            if kind != "E":
                continue
            if not stacks[tid]:
                unmatched_ends += 1
                continue
            occurrence = stacks[tid].pop()
            duration_ms = max(0.0, timestamp_ms - occurrence["ts_ms"])
            key = (occurrence["name"], occurrence["thread_name"], occurrence["tid"])
            current = aggregates.setdefault(key, {
                **occurrence, "occurrence_count": 0, "inclusive_total_duration_ms": 0.0,
                "max_span_duration_ms": 0.0, "first_start_ns": 0, "last_end_ns": 0,
                "observed_occurrences": [],
            })
            start_ns = int(occurrence["ts_ms"] * 1_000_000)
            end_ns = int(timestamp_ms * 1_000_000)
            current["occurrence_count"] += 1
            current["inclusive_total_duration_ms"] += duration_ms
            current["max_span_duration_ms"] = max(current["max_span_duration_ms"], duration_ms)
            current["first_start_ns"] = min(current["first_start_ns"] or start_ns, start_ns)
            current["last_end_ns"] = max(current["last_end_ns"], end_ns)
            if len(current["observed_occurrences"]) < _MAX_OCCURRENCES_PER_EVENT:
                current["observed_occurrences"].append({
                    "ts_ms": round(occurrence["ts_ms"], 6), "dur_ms": round(duration_ms, 6),
                    "process_name": occurrence["process_name"], "thread_name": occurrence["thread_name"],
                    "pid": occurrence["pid"], "tid": occurrence["tid"],
                })
            matched_spans += 1

    rows = sorted(aggregates.values(), key=lambda item: (
        -float(item["inclusive_total_duration_ms"]), -int(item["occurrence_count"]), item["name"], item["tid"]
    ))[:_MAX_EVENTS]
    events: list[dict[str, Any]] = []
    for rank, item in enumerate(rows, 1):
        digest = hashlib.sha256(f"{item['name']}\0{item['thread_name']}\0{item['tid']}".encode()).hexdigest()[:16]
        events.append({
            "event_id": f"event_capture_{digest}", "name": item["name"],
            "process_name": item["process_name"], "thread_name": item["thread_name"],
            "pid": item["pid"], "tid": item["tid"],
            "ts_ms": round(item["first_start_ns"] / 1_000_000.0, 6),
            "dur_ms": round(item["max_span_duration_ms"], 6),
            "metrics": {
                "rank": rank, "occurrence_count": item["occurrence_count"],
                "inclusive_total_duration_ms": round(item["inclusive_total_duration_ms"], 6),
                "max_span_duration_ms": round(item["max_span_duration_ms"], 6),
                "first_start_ns": item["first_start_ns"], "last_end_ns": item["last_end_ns"],
            },
            "observed_occurrences": item["observed_occurrences"],
        })
    return events, {
        "marker_line_count": min(marker_lines, _MAX_MARKER_LINES), "matched_span_count": matched_spans,
        "distinct_event_count": len(aggregates), "returned_event_count": len(events),
        "unmatched_end_count": unmatched_ends, "open_span_count": sum(len(value) for value in stacks.values()),
        "truncated": truncated or len(aggregates) > _MAX_EVENTS,
    }


def materialize_capture_runtime_trace(
    state_root: Path,
    run: dict[str, Any],
    *,
    step_id: str,
    source_connector_id: str,
) -> dict[str, Any]:
    """Create (or reuse) the immutable runtime Trace artifact for one capture step."""

    connector_id = str(source_connector_id or "").strip()
    if not _SAFE_CONNECTOR_ID.fullmatch(connector_id):
        raise PlatformError("SOURCE_CONNECTOR_ID_INVALID", "请选择有效的代码仓后再导入真实 Trace。", status_code=422)
    registered = JsonRegistry(Path(state_root) / "connectors.json", {"connectors": []}).read().get("connectors", [])
    if not any(isinstance(item, dict) and item.get("connector_id") == connector_id for item in registered):
        raise PlatformError("SOURCE_CONNECTOR_NOT_FOUND", "所选代码仓已不存在，请刷新代码仓目录。", status_code=404)
    manifest = _record(run.get("manifest"))
    step, trace_path = _trace_file(Path(state_root).resolve(), manifest, str(step_id or ""))
    events, extraction = _observed_spans(trace_path)
    if not events:
        raise PlatformError(
            "CAPTURE_RUNTIME_TRACE_EVENTS_UNAVAILABLE",
            "所选 Trace 中没有可配对的 HiTrace B/E 事件，无法用于应用流程校准。",
            status_code=422,
            details=extraction,
        )
    files = [item for item in manifest.get("files", []) if isinstance(item, dict)
             and item.get("step_id") == step_id and item.get("kind") == "trace"]
    entry = files[0]
    store = ArtifactStore(Path(state_root), project_id="local-default")
    raw_device_serial = str(manifest.get("device_serial") or "")
    capture_provenance = {
        "run_id": manifest.get("run_id"), "plan_id": manifest.get("plan_id"), "step_id": step_id,
        "step_title": step.get("title"), "step_ordinal": step.get("ordinal"),
        # Keep device correlation auditable without persisting the workstation's
        # raw HDC serial in server-side immutable artifacts.
        "device_serial_sha256": hashlib.sha256(raw_device_serial.encode("utf-8")).hexdigest()
        if raw_device_serial else None,
        "trace_sha256": entry.get("sha256"),
    }
    binding = {"match_level": "unknown", "capture_provenance": capture_provenance}
    raw = store.write(
        "raw_trace_input",
        {
            "file_name": entry.get("name") or trace_path.name, "bytes": trace_path.stat().st_size,
            "input_sha256": entry.get("sha256"), "input_kind": "capture_ftrace",
            "managed_path": str(entry.get("managed_path") or ""), "capture_provenance": capture_provenance,
        },
        source_connector_id=connector_id, source_binding=binding,
        producer={"name": "desktop-capture-runtime-trace", "version": "1.0.0"},
    )
    title = str(manifest.get("title") or "").strip()
    step_title = str(step.get("title") or step_id).strip()
    ordinal = step.get("ordinal")
    trace_name = f"[{title}] " if title else ""
    trace_name += f"Step {ordinal}: {step_title}" if isinstance(ordinal, int) and ordinal > 0 else step_title
    first = min(events, key=lambda item: float(item.get("ts_ms") or 0))
    last = max(events, key=lambda item: float(item.get("ts_ms") or 0) + float(item.get("dur_ms") or 0))
    start_ns = min(int(_record(item.get("metrics")).get("first_start_ns") or 0) for item in events)
    end_ns = max(int(_record(item.get("metrics")).get("last_end_ns") or 0) for item in events)
    hotspots = []
    for item in events:
        metrics = item["metrics"]
        hotspots.append({
            # The opportunity APIs address a hotspot by this stable identifier.
            # It is derived only from observed event identity and is therefore
            # repeatable when the same capture package is imported again.
            "hotspot_id": f"hotspot_{item['event_id'].removeprefix('event_')}",
            "trace_event_name": item["name"],
            "module": item["process_name"],
            "function_ref": {
                "symbol": item["name"],
                "module": item["process_name"],
                "resolution": "observed_runtime_trace_name",
            },
            "metrics": metrics,
            "active_time_metrics": {
                "total_duration_ms": metrics["inclusive_total_duration_ms"],
                "max_duration_ms": metrics["max_span_duration_ms"],
                "call_count": metrics["occurrence_count"],
                "aggregation_scope": "observed_hitrace_spans",
            },
            "observed_occurrences": item["observed_occurrences"],
            "stage_context": {"stage_id": f"capture_{step_id}"},
        })
    payload = {
        "input_kind": "external_trace_analysis_report", "trace_name": trace_name[:300],
        "analysis_profile": "desktop-capture-ftrace-runtime-events/v1",
        "trace_import": {
            "import_instance_id": f"capture_trace_{str(entry.get('sha256') or '')[:24]}",
            "uploaded_at": manifest.get("created_at"),
        },
        "capture_provenance": capture_provenance,
        "stage_catalog": [{
            "stage_id": f"capture_{step_id}", "stage_name": step_title,
            "time_range_ns": {"start_ns": start_ns, "end_ns": end_ns},
            "reported_event_count": extraction["matched_span_count"],
            "start_event": first, "end_event": last, "top_events": events,
        }],
        "hotspot_candidates": hotspots,
        "quality": {
            "status": "partial" if extraction["truncated"] or extraction["open_span_count"] else "complete",
            "source": "desktop_capture_ftrace_observed_hitrace_spans", **extraction,
            "limitations": ["仅物化 Trace 中实际配对的 HiTrace B/E 区间；未把调度事件推断为业务语义。"],
        },
    }
    artifact = store.write(
        "trace_analysis_result", payload,
        source_connector_id=connector_id, source_binding=binding,
        producer={"name": "desktop-capture-runtime-trace", "version": "1.0.0"},
        input_artifacts=[f"artifact:{raw['artifact_id']}", f"sha256:{entry.get('sha256')}"],
    )
    return {"artifact": artifact, "raw_input_artifact": raw, "extraction": extraction}
