"""Deterministic trace normalization and the intentionally limited .sys Spike."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict, deque
from pathlib import Path
from typing import Any

from .core import sha256_file
from .trace_modules import summarize_trace_modules


FRAME_WINDOW_NS = 20_000_000
MAX_SYS_LINES = 500_000
# The Explorer needs enough rows to make module filtering useful, while the
# textual Spike remains bounded for a browser-rendered catalog.
MAX_CLUSTER_CATALOG = 2_000
_TRACE_LINE = re.compile(
    r"^\s*(?P<task>.+?)-(?P<pid>\d+)\s+\(\s*\d+\)\s+\[\d+\].*?\s(?P<timestamp>\d+\.\d+):\s+(?P<event>[A-Za-z0-9_]+):\s*(?P<body>.*)$"
)
_WAKEUP = re.compile(r"comm=(?P<task>.+?)\s+pid=(?P<pid>\d+)")
_HITRACE_EVENT_PREFIX = re.compile(r"^[A-Z]:\s*")
_HITRACE_EVENT_TAG_SUFFIX = re.compile(r"\|[A-Z]\d+.*$")
_REPORT_WRAPPER_KEYS = (
    "result",
    "data",
    "payload",
    "analysis",
    "trace_analysis",
    "traceAnalysis",
    "trace_result",
    "traceResult",
    "report",
    "flow",
    "workflow",
    "stage_analysis",
    "stageAnalysis",
    "flow_analysis",
    "flowAnalysis",
)
_STAGE_COLLECTION_KEYS = (
    "stages",
    "stage_list",
    "stageList",
    "stage_results",
    "stageResults",
    "stage_statistics",
    "stageStatistics",
    "phases",
)
_TOP_EVENT_KEYS = (
    "top_events",
    "topEvents",
    "hot_events",
    "hotEvents",
    "hotspot_events",
    "hotspotEvents",
    "representative_events",
    "representativeEvents",
    "top_trace_events",
    "topTraceEvents",
    "hot_traces",
    "hotTraces",
    "trace_hotspots",
    "traceHotspots",
    "hotspots",
)
_ROOT_HOTSPOT_KEYS = (
    "hotspots",
    "hotspot_candidates",
    "hotspotCandidates",
    "hot_events",
    "hotEvents",
    "hot_traces",
    "hotTraces",
    "trace_hotspots",
    "traceHotspots",
)
_MODULE_COLLECTION_KEYS = ("modules", "module_list", "moduleList", "module_results", "moduleResults")
_MODULE_FUNCTION_KEYS = ("top_functions", "topFunctions", "hot_functions", "hotFunctions", "functions")
_EVENT_NAME_KEYS = (
    "name",
    "event_name",
    "eventName",
    "trace_name",
    "traceName",
    "label",
    "symbol",
    "function",
    "function_name",
    "functionName",
)
_STAGE_IDENTITY_KEYS = ("id", "stage_id", "stageId", "key", "name", "stage_name", "stageName", "label", "title")
_STAGE_TEMPORAL_KEYS = (
    "start_ns",
    "startNs",
    "begin_ns",
    "beginNs",
    "end_ns",
    "endNs",
    "finish_ns",
    "finishNs",
    "start_ms",
    "startMs",
    "begin_ms",
    "beginMs",
    "end_ms",
    "endMs",
    "finish_ms",
    "finishMs",
    "duration_ms",
    "durationMs",
    "elapsed_ms",
    "elapsedMs",
)
_STAGE_ANCHOR_KEYS = (
    "start_event",
    "startEvent",
    "begin_event",
    "beginEvent",
    "end_event",
    "endEvent",
    "finish_event",
    "finishEvent",
    "anchors",
)
_STAGE_METRIC_KEYS = (
    "event_count",
    "eventCount",
    "count",
    "trace_count",
    "traceCount",
    "total_event_duration_ms",
    "totalEventDurationMs",
    "event_duration_ms",
    "eventDurationMs",
)
_EVENT_TIMING_KEYS = (
    "ts_ms",
    "tsMs",
    "start_ms",
    "startMs",
    "timestamp_ms",
    "timestampMs",
    "ts_ns",
    "tsNs",
    "start_ns",
    "startNs",
    "timestamp_ns",
    "timestampNs",
    "dur_ms",
    "durMs",
    "duration_ms",
    "durationMs",
    "elapsed_ms",
    "elapsedMs",
    "cost_ms",
    "costMs",
    "dur_ns",
    "durNs",
    "duration_ns",
    "durationNs",
    "elapsed_ns",
    "elapsedNs",
)
_EVENT_CONTEXT_KEYS = (
    "process_name",
    "processName",
    "process",
    "module",
    "module_name",
    "moduleName",
    "pid",
    "process_id",
    "processId",
    "thread_name",
    "threadName",
    "thread",
    "tid",
    "thread_id",
    "threadId",
    "itid",
    "internal_tid",
    "internalTid",
)
_MAX_SCHEMA_SEARCH_DEPTH = 12
_MAX_SCHEMA_SEARCH_OBJECTS = 20_000


def _first_value(record: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key not in record or record[key] is None:
            continue
        value = record[key]
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _report_views(report: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Expose common trace-team envelopes without recursively guessing arbitrary JSON."""

    views = [("$", report)]
    seen = {id(report)}
    cursor = 0
    while cursor < len(views):
        path, current = views[cursor]
        cursor += 1
        if path.count(".") >= 3:
            continue
        for key in _REPORT_WRAPPER_KEYS:
            nested = current.get(key)
            if isinstance(nested, dict) and id(nested) not in seen:
                seen.add(id(nested))
                views.append((f"{path}.{key}", nested))
    return views


def _schema_views(report: dict[str, Any]) -> list[tuple[str, dict[str, Any], int]]:
    """Traverse JSON containers for schema fields, independent of field values.

    Trace-team reports are frequently wrapped by transport- or product-specific
    objects.  Collection discovery therefore follows object structure rather
    than a fixed wrapper-name allowlist.  Depth and object-count bounds keep a
    malformed report from turning normalization into an unbounded walk.
    """

    views: list[tuple[str, dict[str, Any], int]] = []
    pending: deque[tuple[str, Any, int]] = deque([("$", report, 0)])
    seen: set[int] = set()
    while pending and len(views) < _MAX_SCHEMA_SEARCH_OBJECTS:
        path, current, depth = pending.popleft()
        if depth > _MAX_SCHEMA_SEARCH_DEPTH:
            continue
        if isinstance(current, dict):
            if id(current) in seen:
                continue
            seen.add(id(current))
            views.append((path, current, depth))
            for key, nested in current.items():
                if isinstance(nested, dict):
                    pending.append((f"{path}.{key}", nested, depth + 1))
                elif isinstance(nested, list):
                    pending.append((f"{path}.{key}", nested, depth + 1))
        elif isinstance(current, list):
            for index, nested in enumerate(current):
                if isinstance(nested, (dict, list)):
                    pending.append((f"{path}[{index}]", nested, depth + 1))
    return views


def _collection_records(value: Any, *, kind: str) -> list[dict[str, Any]]:
    if isinstance(value, list):
        records: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict):
                records.append(dict(item))
            elif kind == "event" and isinstance(item, str) and item.strip():
                records.append({"name": item.strip()})
        return records
    if not isinstance(value, dict):
        return []

    identity_keys = (
        ("id", "stage_id", "stageId", "name", "stage_name", "stageName")
        if kind == "stage"
        else _EVENT_NAME_KEYS
    )
    if any(key in value for key in identity_keys):
        return [dict(value)]

    nested_keys = (
        ("items", "records", "list", "stages", "stage_list", "stageList")
        if kind == "stage"
        else ("items", "records", "list", "events", "top_events", "topEvents", "hot_events", "hotEvents")
    )
    for key in nested_keys:
        if key in value:
            nested = _collection_records(value[key], kind=kind)
            if nested or isinstance(value[key], (list, dict)):
                return nested

    records = []
    for key, item in value.items():
        if isinstance(item, dict):
            record = dict(item)
            record.setdefault("id" if kind == "stage" else "name", str(key))
            records.append(record)
        elif kind == "event" and isinstance(item, (int, float)) and not isinstance(item, bool):
            records.append({"name": str(key), "dur_ms": item})
        elif isinstance(item, list):
            records.extend(_collection_records(item, kind=kind))
    return records


def _record_schema_score(record: dict[str, Any], *, kind: str) -> tuple[int, list[str]]:
    """Score only field presence, never names, labels, IDs, or metric values."""

    present = set(record)
    matched: set[str] = set()
    score = 0
    if kind == "stage":
        groups = (
            (_STAGE_IDENTITY_KEYS, 3),
            (_STAGE_TEMPORAL_KEYS, 3),
            (_TOP_EVENT_KEYS, 5),
            (_STAGE_ANCHOR_KEYS, 4),
            (_STAGE_METRIC_KEYS, 2),
        )
    else:
        groups = (
            (_EVENT_NAME_KEYS, 5),
            (_EVENT_TIMING_KEYS, 3),
            (_EVENT_CONTEXT_KEYS, 2),
            (("metrics", "critical_path", "criticalPath"), 1),
        )
    for field_names, weight in groups:
        group_matches = present.intersection(field_names)
        if group_matches:
            score += weight
            matched.update(group_matches)
    return score, sorted(matched)


def _find_report_collection_match(
    report: dict[str, Any],
    keys: tuple[str, ...],
    *,
    kind: str,
) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    for path, view, depth in _schema_views(report):
        for key in keys:
            if key not in view:
                continue
            value = view[key]
            if not isinstance(value, (list, dict)):
                continue
            records = _collection_records(value, kind=kind)
            scored_records = [_record_schema_score(record, kind=kind) for record in records]
            schema_score = max((score for score, _fields in scored_records), default=0)
            matched_fields = sorted({field for _score, fields in scored_records for field in fields})
            matches.append(
                {
                    "records": records,
                    "path": f"{path}.{key}",
                    "key": key,
                    "depth": depth + 1,
                    "schema_score": schema_score,
                    "matched_fields": matched_fields,
                }
            )
    if not matches:
        return None
    matches.sort(
        key=lambda match: (
            int(match["schema_score"]),
            bool(match["records"]),
            len(match["records"]),
            -int(match["depth"]),
        ),
        reverse=True,
    )
    return matches[0]


def _milliseconds(record: dict[str, Any], millisecond_keys: tuple[str, ...], nanosecond_keys: tuple[str, ...]) -> float | None:
    value = _first_value(record, *millisecond_keys)
    if value is not None:
        return _number(value)
    value = _first_value(record, *nanosecond_keys)
    if value is not None:
        return _number(value) / 1_000_000.0
    return None


def _normalize_stage_event(value: Any) -> dict[str, Any] | None:
    if isinstance(value, str):
        return {"name": value.strip()} if value.strip() else None
    if not isinstance(value, dict):
        return None
    metrics = value.get("metrics") if isinstance(value.get("metrics"), dict) else {}
    name = _first_value(value, *_EVENT_NAME_KEYS)
    if name is None:
        name = _first_value(metrics, *_EVENT_NAME_KEYS)
    if not isinstance(name, (str, int, float)) or not str(name).strip():
        return None
    duration_ms = _milliseconds(
        value,
        ("dur_ms", "durMs", "duration_ms", "durationMs", "elapsed_ms", "elapsedMs", "cost_ms", "costMs"),
        ("dur_ns", "durNs", "duration_ns", "durationNs", "elapsed_ns", "elapsedNs"),
    )
    if duration_ms is None:
        duration_ms = _milliseconds(
            metrics,
            ("dur_ms", "durMs", "duration_ms", "durationMs", "wall_time_ms", "wallTimeMs"),
            ("dur_ns", "durNs", "duration_ns", "durationNs"),
        )
    timestamp_ms = _milliseconds(
        value,
        ("ts_ms", "tsMs", "start_ms", "startMs", "timestamp_ms", "timestampMs"),
        ("ts_ns", "tsNs", "start_ns", "startNs", "timestamp_ns", "timestampNs"),
    )
    normalized = {
        "name": str(name).strip(),
        "ts_ms": timestamp_ms,
        "dur_ms": duration_ms,
        "process_name": _first_value(value, "process_name", "processName", "process", "module", "module_name", "moduleName"),
        "pid": _first_value(value, "pid", "process_id", "processId"),
        "thread_name": _first_value(value, "thread_name", "threadName", "thread"),
        "tid": _first_value(value, "tid", "thread_id", "threadId"),
        "itid": _first_value(value, "itid", "internal_tid", "internalTid"),
    }
    if metrics:
        normalized["metrics"] = dict(metrics)
    measurement_semantics = value.get("measurement_semantics")
    if isinstance(measurement_semantics, dict):
        normalized["measurement_semantics"] = dict(measurement_semantics)
    summary = value.get("summary")
    if isinstance(summary, str) and summary.strip():
        normalized["summary"] = summary.strip()
    return {key: item for key, item in normalized.items() if item is not None}


def _stage_event_collection(stage: dict[str, Any]) -> tuple[list[dict[str, Any]], list[str]]:
    events: list[dict[str, Any]] = []
    event_keys: list[str] = []
    seen: set[tuple[Any, ...]] = set()
    for key in _TOP_EVENT_KEYS:
        if key not in stage:
            continue
        event_keys.append(key)
        records = _collection_records(stage[key], kind="event")
        for item in records:
            normalized = _normalize_stage_event(item)
            if normalized is None:
                continue
            occurrence_key = _stage_occurrence_key(normalized)
            if occurrence_key in seen:
                continue
            seen.add(occurrence_key)
            events.append(normalized)
    return events, event_keys


def _report_metadata(report: dict[str, Any], *keys: str) -> Any:
    for _path, view in _report_views(report):
        value = _first_value(view, *keys)
        if value is not None:
            return value
    return None


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _pressure(avg_ms: float, max_ms: float, call_count: int) -> tuple[str, list[str]]:
    """Never let long-run accumulation alone create a high-pressure label."""

    notes = ["Trace window duration is not present in this imported report."]
    if max_ms >= 16.6 or avg_ms >= 8.0:
        notes.append("A single observed invocation approaches or exceeds a frame-sized budget.")
        return "high", notes
    if max_ms >= 4.0 or avg_ms >= 2.0:
        notes.append("Observed per-invocation latency is material, but frame alignment is not proven.")
        return "medium", notes
    if call_count > 0:
        notes.append("Aggregate time is retained as evidence, but low per-invocation pressure is not a hotspot verdict.")
    return "low", notes


def _dominance(module: str, function: str) -> str:
    text = f"{module} {function}".lower()
    if any(token in text for token in ("fence", "wait", "bufferqueue", "queue", "reply", "ipc")):
        return "blocking_dominant"
    if any(token in text for token in ("driver", "gpu", "hwc", "composer", "present")):
        return "driver_wait_dominant"
    if any(token in text for token in ("render", "paint", "decode", "filter", "blur", "compute")):
        return "compute_dominant"
    return "unknown"


def _trace_event_symbol(event_name: str) -> str:
    """Extract a searchable symbol without pretending the label is a function."""

    cleaned = _HITRACE_EVENT_PREFIX.sub("", event_name.strip())
    cleaned = _HITRACE_EVENT_TAG_SUFFIX.sub("", cleaned).strip()
    # Some instrumented C++ methods append runtime arguments to the label.
    # Keep only the method-shaped prefix; arbitrary labels containing '-' are
    # left untouched because they are not proven symbols.
    if "::" in cleaned and "-" in cleaned:
        cleaned = cleaned.split("-", 1)[0].strip()
    return cleaned or event_name.strip() or "unknown"


def _stage_cluster_id(stage_id: str, trace_sha256: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9_]+", "_", stage_id).strip("_")[:48] or "unknown"
    digest = hashlib.sha256(f"{trace_sha256}:{stage_id}".encode("utf-8")).hexdigest()[:10]
    return f"cluster_stage_{slug}_{digest}"


def _stage_occurrence_key(event: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(event.get("name") or ""),
        event.get("ts_ms"),
        event.get("dur_ms"),
        event.get("pid"),
        event.get("tid"),
    )


def _stage_event_aggregate(occurrences: list[dict[str, Any]]) -> dict[str, Any]:
    """Prefer supplied aggregate metrics while retaining occurrence-shaped reports."""

    count = 0
    inclusive_ms = 0.0
    self_ms = 0.0
    max_span_ms = 0.0
    max_self_ms = 0.0
    supplied = False
    for occurrence in occurrences:
        metrics = occurrence.get("metrics") if isinstance(occurrence.get("metrics"), dict) else {}
        count_value = _first_value(metrics, "occurrence_count", "occurrenceCount", "call_count", "callCount")
        inclusive_value = _first_value(
            metrics,
            "inclusive_total_duration_ms",
            "inclusiveTotalDurationMs",
            "total_duration_ms",
            "totalDurationMs",
        )
        self_value = _first_value(
            metrics,
            "self_time_estimate_total_duration_ms",
            "selfTimeEstimateTotalDurationMs",
            "exclusive_estimate_total_duration_ms",
            "exclusiveEstimateTotalDurationMs",
        )
        max_span_value = _first_value(
            metrics,
            "max_span_duration_ms",
            "maxSpanDurationMs",
            "max_duration_ms",
            "maxDurationMs",
        )
        max_self_value = _first_value(
            metrics,
            "max_self_time_estimate_duration_ms",
            "maxSelfTimeEstimateDurationMs",
        )
        aggregate_values = (count_value, inclusive_value, self_value, max_span_value, max_self_value)
        supplied = supplied or any(value is not None for value in aggregate_values)
        occurrence_count = max(1, _int(count_value)) if count_value is not None else 1
        representative_ms = _number(occurrence.get("dur_ms"))
        count += occurrence_count
        inclusive_ms += _number(inclusive_value) if inclusive_value is not None else representative_ms
        self_ms += _number(self_value) if self_value is not None else representative_ms
        max_span_ms = max(max_span_ms, _number(max_span_value) if max_span_value is not None else representative_ms)
        max_self_ms = max(max_self_ms, _number(max_self_value) if max_self_value is not None else representative_ms)
    return {
        "occurrence_count": count,
        "inclusive_total_duration_ms": inclusive_ms,
        "self_time_estimate_total_duration_ms": self_ms,
        "max_span_duration_ms": max_span_ms,
        "max_self_time_estimate_duration_ms": max_self_ms,
        "aggregate_metrics_supplied": supplied,
    }


def _stage_flow_report(
    report: dict[str, Any],
    *,
    trace_name: str,
    trace_sha256: str,
    stage_match: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize a stage-oriented trace report without inventing CPU samples.

    `top_events` contains representative inclusive spans.  Stage duration and
    total event duration are stage-level envelopes and must never be assigned
    to one event, its instrumentation function, or one of its callees.
    """

    stage_match = stage_match or _find_report_collection_match(report, _STAGE_COLLECTION_KEYS, kind="stage")
    raw_stages = list(stage_match["records"]) if stage_match is not None else []
    stage_container_path = str(stage_match["path"]) if stage_match is not None else None
    stage_container_key = str(stage_match["key"]) if stage_match is not None else None
    stage_catalog: list[dict[str, Any]] = []
    cluster_records: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    reported_hot_event_count = 0
    boundary_anchor_count = 0
    event_container_aliases: set[str] = set()
    rank = 1

    for stage_index, raw_stage in enumerate(raw_stages, start=1):
        if not isinstance(raw_stage, dict):
            continue
        stage_id = str(_first_value(raw_stage, "id", "stage_id", "stageId", "key") or f"stage_{stage_index:04d}")
        stage_name = str(_first_value(raw_stage, "name", "stage_name", "stageName", "label", "title") or stage_id)
        stage_order = _int(_first_value(raw_stage, "order", "index", "sequence", "seq", "rank")) or stage_index
        cluster_id = _stage_cluster_id(stage_id, trace_sha256)
        top_events, event_container_keys = _stage_event_collection(raw_stage)
        event_container_aliases.update(event_container_keys)
        reported_hot_event_count += len(top_events)

        anchors = raw_stage.get("anchors") if isinstance(raw_stage.get("anchors"), dict) else {}
        start_event = _normalize_stage_event(
            _first_value(raw_stage, "start_event", "startEvent", "begin_event", "beginEvent")
            or _first_value(anchors, "start", "start_event", "startEvent", "begin")
        )
        end_event = _normalize_stage_event(
            _first_value(raw_stage, "end_event", "endEvent", "finish_event", "finishEvent")
            or _first_value(anchors, "end", "end_event", "endEvent", "finish")
        )
        start_ns = _int(_first_value(raw_stage, "start_ns", "startNs", "begin_ns", "beginNs"))
        end_ns = _int(_first_value(raw_stage, "end_ns", "endNs", "finish_ns", "finishNs"))
        if not start_ns:
            start_ms = _first_value(raw_stage, "start_ms", "startMs", "begin_ms", "beginMs")
            start_ns = int(_number(start_ms) * 1_000_000) if start_ms is not None else 0
        if not end_ns:
            end_ms = _first_value(raw_stage, "end_ms", "endMs", "finish_ms", "finishMs")
            end_ns = int(_number(end_ms) * 1_000_000) if end_ms is not None else 0
        duration_ms_value = _first_value(raw_stage, "duration_ms", "durationMs", "elapsed_ms", "elapsedMs", "cost_ms", "costMs")
        duration_ms = _number(duration_ms_value) if duration_ms_value is not None else None
        if duration_ms is None and start_ns and end_ns and end_ns >= start_ns:
            duration_ms = (end_ns - start_ns) / 1_000_000.0
        reported_event_count = _int(_first_value(raw_stage, "event_count", "eventCount", "count", "trace_count", "traceCount"))
        total_event_duration_value = _first_value(
            raw_stage,
            "total_event_duration_ms",
            "totalEventDurationMs",
            "event_duration_ms",
            "eventDurationMs",
        )
        reported_total_event_duration_ms = _number(total_event_duration_value) if total_event_duration_value is not None else None

        investigation_events = list(top_events)
        candidate_role = "reported_hot_event"
        if not investigation_events:
            candidate_role = "trace_anchor_only"
            seen_anchors: set[tuple[Any, ...]] = set()
            for anchor in (start_event, end_event):
                if anchor is None:
                    continue
                occurrence_key = _stage_occurrence_key(anchor)
                if occurrence_key in seen_anchors:
                    continue
                seen_anchors.add(occurrence_key)
                investigation_events.append(anchor)
            boundary_anchor_count += len(investigation_events)
        stage_context = {
            "stage_id": stage_id,
            "stage_name": stage_name,
            "stage_order": stage_order,
            "time_range_ns": {
                "start_ns": start_ns or None,
                "end_ns": end_ns or None,
            },
            "duration_ms": duration_ms,
            "reported_event_count": reported_event_count,
            "reported_total_event_duration_ms": reported_total_event_duration_ms,
            "listed_top_event_count": len(top_events),
            "investigation_anchor_count": len(investigation_events) if candidate_role == "trace_anchor_only" else 0,
            "coverage": "representative_top_events_only" if top_events else "stage_boundary_anchors_only" if investigation_events else "no_event_anchor",
        }
        stage_catalog.append(
            {
                **stage_context,
                "cluster_id": cluster_id,
                "start_event": start_event,
                "end_event": end_event,
                "top_events": top_events,
                "investigation_anchors": investigation_events if candidate_role == "trace_anchor_only" else [],
            }
        )

        grouped_events: dict[tuple[str, str], list[dict[str, Any]]] = {}
        for event in investigation_events:
            raw_name = str(event.get("name") or "unknown")
            bucket = grouped_events.setdefault((raw_name, candidate_role), [])
            if _stage_occurrence_key(event) not in {_stage_occurrence_key(item) for item in bucket}:
                bucket.append(event)

        stage_candidate_ids: list[str] = []
        for (raw_name, event_role), occurrences in grouped_events.items():
            aggregate = _stage_event_aggregate(occurrences) if event_role == "reported_hot_event" else {
                "occurrence_count": 0,
                "inclusive_total_duration_ms": 0.0,
                "self_time_estimate_total_duration_ms": 0.0,
                "max_span_duration_ms": 0.0,
                "max_self_time_estimate_duration_ms": 0.0,
                "aggregate_metrics_supplied": False,
            }
            module = next(
                (
                    str(item.get("process_name") or item.get("thread_name"))
                    for item in occurrences
                    if item.get("process_name") or item.get("thread_name")
                ),
                str(_first_value(raw_stage, "module", "module_name", "moduleName", "process_name", "processName") or "unknown"),
            )
            hotspot_digest = hashlib.sha256(
                f"{trace_sha256}:{stage_id}:{event_role}:{raw_name}".encode("utf-8")
            ).hexdigest()[:16]
            hotspot_id = f"hs_stage_{hotspot_digest}"
            duration_kind = "inclusive_trace_span" if event_role == "reported_hot_event" else "unmeasured_trace_anchor"
            normalized_item = {
                "hotspot_id": hotspot_id,
                "rank": rank,
                "cluster_id": cluster_id,
                "module": module,
                "symbol": _trace_event_symbol(raw_name),
                "trace_event_name": raw_name,
                "total_duration_ms": aggregate["inclusive_total_duration_ms"],
                "avg_duration_ms": (
                    aggregate["inclusive_total_duration_ms"] / aggregate["occurrence_count"]
                    if aggregate["occurrence_count"]
                    else 0.0
                ),
                "max_duration_ms": aggregate["max_span_duration_ms"],
                "call_count": aggregate["occurrence_count"] if event_role == "reported_hot_event" else len(occurrences),
                "candidate_role": event_role,
                "aggregation_scope": "stage_report_listed_top_events" if event_role == "reported_hot_event" else "stage_boundary_anchor_fallback",
                "metrics": {
                    "window_duration_ms": stage_context["duration_ms"],
                    "listed_occurrence_count": aggregate["occurrence_count"] if event_role == "reported_hot_event" else 0,
                    "anchor_occurrence_count": len(occurrences) if event_role == "trace_anchor_only" else 0,
                    "reported_stage_event_count": stage_context["reported_event_count"],
                    "reported_stage_total_event_duration_ms": stage_context["reported_total_event_duration_ms"],
                    **(
                        {
                            "self_time_estimate_total_duration_ms": aggregate["self_time_estimate_total_duration_ms"],
                            "inclusive_total_duration_ms": aggregate["inclusive_total_duration_ms"],
                            "max_self_time_estimate_duration_ms": aggregate["max_self_time_estimate_duration_ms"],
                            "max_span_duration_ms": aggregate["max_span_duration_ms"],
                            "aggregate_metrics_supplied": True,
                        }
                        if aggregate["aggregate_metrics_supplied"]
                        else {}
                    ),
                },
                "stage_context": stage_context,
                "measurement_semantics": {
                    "duration_kind": duration_kind,
                    "representative_only": event_role == "reported_hot_event",
                    "cpu_self_time_available": False,
                    "callee_attribution_available": False,
                    "note": (
                        "Durations cover only listed top-event occurrences and do not attribute CPU time to the trace label, its instrumentation function, or callees."
                        if event_role == "reported_hot_event"
                        else "This stage boundary is exposed only as a source-investigation anchor. It has no reported hotspot duration or CPU attribution."
                    ),
                },
                "observed_occurrences": [
                    {
                        key: occurrence.get(key)
                        for key in ("name", "ts_ms", "dur_ms", "process_name", "pid", "thread_name", "tid", "itid")
                        if occurrence.get(key) is not None
                    }
                    for occurrence in occurrences
                ],
            }
            candidate = _report_hotspot_candidate(normalized_item, trace_sha256)
            candidate["temporal_pressure_notes"] = (
                [
                    "Per-occurrence duration is an inclusive trace span, not CPU self time.",
                    "Only representative events are available; stage event_count is not this label's call count.",
                    "The trace label identifies an execution boundary. Source and callee investigation are required before selecting hotspot code.",
                ]
                if event_role == "reported_hot_event"
                else [
                    "The report supplied no representative hot event for this stage.",
                    "This start/end label is selectable only for source and call-path investigation; no hotspot duration is assigned to it.",
                ]
            )
            candidates.append(candidate)
            stage_candidate_ids.append(hotspot_id)
            rank += 1

        module_values = [
            value
            for item in candidates
            if item.get("cluster_id") == cluster_id
            for value in (
                item.get("function_ref", {}).get("module"),
                item.get("function_ref", {}).get("symbol"),
            )
            if value
        ]
        cluster_records.append(
            {
                "cluster_id": cluster_id,
                "stage_id": stage_id,
                "stage_name": stage_name,
                "stage_order": stage_order,
                "time_range_ns": stage_context["time_range_ns"],
                "root_event_ids": stage_candidate_ids,
                "thread_refs": [],
                "edge_summary": {
                    "kind": "external_stage_envelope",
                    "edge_count": 0,
                    "reported_event_count": stage_context["reported_event_count"],
                },
                "module_summary": summarize_trace_modules(module_values, fallback_top_function=stage_name),
                "clock_quality": "reported_stage_timestamps",
                "profile_id": "external-stage-flow-v1",
                "selection_state": "pending" if stage_candidate_ids else "no_listed_hotspot_event",
                "artifact_ref": f"sha256:{trace_sha256}",
            }
        )

    candidates.sort(
        key=lambda item: (
            item.get("candidate_role") != "trace_anchor_only",
            item["temporal_pressure"] == "high",
            item["active_time_metrics"]["max_duration_ms"],
            -item["external_rank"],
        ),
        reverse=True,
    )
    warnings = [
        "Stage duration is an execution envelope and is not attributed to any one top event.",
        "Listed event durations are inclusive spans that may overlap; CPU self time and callee attribution are unavailable.",
        "top_events is representative rather than exhaustive; stage event_count is preserved separately and is not used as a label call count.",
        "Trace labels are source anchors or runtime boundaries, not automatically selected hotspot functions.",
    ]
    if boundary_anchor_count:
        warnings.append(
            f"{boundary_anchor_count} stage start/end label(s) were exposed as investigation anchors because those stages supplied no representative hotspot event; no duration was assigned to them."
        )
    source_revision = _report_metadata(report, "source_revision", "sourceRevision")
    if source_revision:
        warnings.append("A source revision declaration was supplied; the platform must still resolve it against the selected connector.")
    else:
        warnings.append("The imported report has no source revision binding.")
    return {
        "trace_id": str(_report_metadata(report, "trace_id", "traceId", "id") or f"trace_{trace_sha256[:16]}"),
        "trace_name": trace_name,
        "input_kind": "external_trace_analysis_report",
        "analysis_profile": "external-stage-flow-v1",
        "input_sha256": trace_sha256,
        "quality": {
            "status": "partial",
            "warnings": warnings,
            "normalization": {
                "contract": "stage-flow-compatible/v2",
                "recognition_method": "json-field-structure/v1",
                "value_independent_schema_detection": True,
                "stage_container_path": stage_container_path,
                "stage_container_key": stage_container_key,
                "stage_schema_score": int(stage_match["schema_score"]) if stage_match is not None else 0,
                "matched_stage_fields": list(stage_match["matched_fields"]) if stage_match is not None else [],
                "input_stage_count": len(raw_stages),
                "reported_hot_event_count": reported_hot_event_count,
                "boundary_anchor_fallback_count": boundary_anchor_count,
                "event_container_aliases": sorted(event_container_aliases),
            },
        },
        "stage_catalog": stage_catalog,
        "clusters": cluster_records,
        "hotspot_candidates": candidates,
        "unresolved_hotspots": [item["hotspot_id"] for item in candidates if item.get("unresolved_reason")],
    }


def _report_hotspot_candidate(item: dict[str, Any], trace_sha256: str) -> dict[str, Any]:
    rank = _int(_first_value(item, "rank", "order", "index"))
    module = str(_first_value(item, "module", "module_name", "moduleName", "process_name", "processName") or "unknown")
    function = str(_first_value(item, "symbol", "function", "function_name", "functionName", "event_name", "eventName", "name") or "unknown")
    metrics = item.get("metrics") if isinstance(item.get("metrics"), dict) else {}

    def item_or_metric(item_keys: tuple[str, ...], metric_keys: tuple[str, ...]) -> Any:
        value = _first_value(item, *item_keys)
        return value if value is not None else _first_value(metrics, *metric_keys)

    total_ms = _number(item_or_metric(
        ("total_duration_ms", "totalDurationMs", "duration_ms", "durationMs"),
        ("total_duration_ms", "totalDurationMs", "wall_time_ms", "wallTimeMs"),
    ))
    avg_ms = _number(item_or_metric(
        ("avg_duration_ms", "avgDurationMs", "average_duration_ms", "averageDurationMs"),
        ("avg_duration_ms", "avgDurationMs", "wall_time_ms", "wallTimeMs"),
    ))
    max_ms = _number(item_or_metric(
        ("max_duration_ms", "maxDurationMs", "peak_duration_ms", "peakDurationMs"),
        ("max_duration_ms", "maxDurationMs", "wall_time_ms", "wallTimeMs"),
    ))
    call_count = _int(item_or_metric(
        ("call_count", "callCount", "occurrence_count", "occurrenceCount"),
        ("call_count", "callCount", "occurrence_count", "occurrenceCount"),
    ))
    temporal_pressure, pressure_notes = _pressure(avg_ms, max_ms, call_count)
    source_path_value = _first_value(item, "file", "path", "source_path", "sourcePath")
    source_path = source_path_value if isinstance(source_path_value, str) else None
    source_line = _int(_first_value(item, "line", "line_number", "lineNumber", "source_line", "sourceLine"))
    function_ref: dict[str, Any] = {"symbol": function, "module": module, "resolution": "external_symbol_only"}
    unresolved_reason: str | None = "External report has no source file, line, or proven source revision."
    if source_path and source_line > 0:
        function_ref = {
            "symbol": function,
            "module": module,
            "file": source_path,
            "line": source_line,
            "resolution": "source_location_declared",
        }
        unresolved_reason = None
    critical_path_value = _first_value(item, "critical_path", "criticalPath")
    critical_path = critical_path_value if isinstance(critical_path_value, dict) else None
    active_time_metrics = {
        **metrics,
        "total_duration_ms": total_ms,
        "avg_duration_ms": avg_ms,
        "max_duration_ms": max_ms,
        "call_count": call_count,
        "aggregation_scope": str(metrics.get("aggregation_scope") or item.get("aggregation_scope") or "whole_trace"),
        "window_duration_ms": metrics.get("window_duration_ms"),
    }
    candidate = {
        "hotspot_id": str(item.get("hotspot_id") or f"hs_report_{rank:04d}"),
        "cluster_id": str(item.get("cluster_id") or f"cluster_report_{trace_sha256[:12]}"),
        "function_ref": function_ref,
        "active_time_metrics": active_time_metrics,
        "temporal_pressure": temporal_pressure,
        "temporal_pressure_notes": pressure_notes,
        "critical_path_metrics": {"available": True, **critical_path} if critical_path else {"available": False, "reason": "No typed critical-path segment was supplied."},
        "dominance_label": _dominance(module, function),
        "evidence_refs": [f"sha256:{trace_sha256}"],
        "external_rank": rank,
        "pct": _number(item.get("pct")),
    }
    for key in ("trace_event_name", "stage_context", "measurement_semantics", "observed_occurrences", "candidate_role"):
        if item.get(key) is not None:
            candidate[key] = item[key]
    if unresolved_reason:
        candidate["unresolved_reason"] = unresolved_reason
    return candidate


def normalize_external_report(report: dict[str, Any], *, trace_name: str, trace_sha256: str) -> dict[str, Any]:
    """Normalize the existing trace team's report without inventing source linkage."""

    stage_match = _find_report_collection_match(report, _STAGE_COLLECTION_KEYS, kind="stage")
    hotspot_match = _find_report_collection_match(report, _ROOT_HOTSPOT_KEYS, kind="event")
    module_match = _find_report_collection_match(report, _MODULE_COLLECTION_KEYS, kind="event")
    explicit_hotspots = list(hotspot_match["records"]) if hotspot_match is not None else []
    hotspot_container_path = str(hotspot_match["path"]) if hotspot_match is not None else None
    report_modules = list(module_match["records"]) if module_match is not None else []
    module_container_path = str(module_match["path"]) if module_match is not None else None
    has_module_hotspots = any(
        isinstance(module, dict)
        and any(bool(_collection_records(module.get(key), kind="event")) for key in _MODULE_FUNCTION_KEYS if key in module)
        for module in report_modules
    )
    stage_records = list(stage_match["records"]) if stage_match is not None else []
    stage_schema_is_present = stage_match is not None and (
        bool(stage_records) or (not explicit_hotspots and not has_module_hotspots)
    )
    if stage_schema_is_present:
        return _stage_flow_report(
            report,
            trace_name=trace_name,
            trace_sha256=trace_sha256,
            stage_match=stage_match,
        )

    raw_hotspots = list(explicit_hotspots)
    if not raw_hotspots:
        for module in report_modules:
            module_name = _first_value(module, "name", "module", "module_name", "moduleName") or "unknown"
            module_functions: list[dict[str, Any]] = []
            for key in _MODULE_FUNCTION_KEYS:
                if key in module:
                    module_functions = _collection_records(module[key], kind="event")
                    break
            for item in module_functions:
                raw_hotspots.append(
                    {
                        "rank": len(raw_hotspots) + 1,
                        "module": module_name,
                        **item,
                    }
                )
    candidates = []
    for index, item in enumerate(raw_hotspots[:80], start=1):
        if not isinstance(item, dict):
            continue
        normalized_item = dict(item)
        normalized_item.setdefault("rank", index)
        candidates.append(_report_hotspot_candidate(normalized_item, trace_sha256))
    candidates.sort(key=lambda item: (item["temporal_pressure"] == "high", item["active_time_metrics"]["max_duration_ms"]), reverse=True)
    cluster_ids = list(dict.fromkeys(item["cluster_id"] for item in candidates))
    warnings = ["Aggregate duration is shown separately from per-invocation temporal pressure."]
    source_revision = _report_metadata(report, "source_revision", "sourceRevision")
    if source_revision:
        warnings.append("A source revision declaration was supplied; the platform must still resolve it against the selected connector.")
    else:
        warnings.append("The imported report has no source revision binding.")
    if any(item.get("critical_path_metrics", {}).get("available") for item in candidates):
        warnings.append("Typed critical-path fields are preserved as upstream evidence; this adapter does not infer additional causality.")
    else:
        warnings.append("No typed critical path is inferred from this report.")
    return {
        "trace_id": str(_report_metadata(report, "trace_id", "traceId", "id") or f"trace_{trace_sha256[:16]}"),
        "trace_name": trace_name,
        "input_kind": "external_trace_analysis_report",
        "input_sha256": trace_sha256,
        "quality": {
            "status": "partial",
            "warnings": warnings,
            "normalization": {
                "contract": "external-report-compatible/v2",
                "recognition_method": "json-field-structure/v1",
                "value_independent_schema_detection": True,
                "hotspot_container_path": hotspot_container_path,
                "module_container_path": module_container_path,
                "hotspot_schema_score": int(hotspot_match["schema_score"]) if hotspot_match is not None else 0,
                "module_schema_score": int(module_match["schema_score"]) if module_match is not None else 0,
                "normalized_hotspot_count": len(candidates),
            },
        },
        "clusters": [
            {
                "cluster_id": cluster_id,
                "time_range_ns": {"start_ns": None, "end_ns": None},
                "root_event_ids": [item["hotspot_id"] for item in candidates if item["cluster_id"] == cluster_id][:20],
                "thread_refs": [],
                "edge_summary": {"kind": "external_aggregate", "edge_count": 0},
                "module_summary": summarize_trace_modules(
                    [
                        value
                        for item in candidates
                        if item["cluster_id"] == cluster_id
                        for value in (
                            item.get("function_ref", {}).get("module"),
                            item.get("function_ref", {}).get("symbol"),
                        )
                    ],
                    fallback_top_function=next(
                        (
                            str(item.get("function_ref", {}).get("symbol"))
                            for item in candidates
                            if item["cluster_id"] == cluster_id and item.get("function_ref", {}).get("symbol")
                        ),
                        None,
                    ),
                ),
                "clock_quality": "not_provided",
                "profile_id": "external-report-v1",
                "selection_state": "pending",
                "artifact_ref": f"sha256:{trace_sha256}",
            }
            for cluster_id in cluster_ids
        ],
        "hotspot_candidates": candidates,
        "unresolved_hotspots": [
            item["hotspot_id"] for item in candidates if item.get("unresolved_reason")
        ],
    }


def _sys_timestamp_ns(text: str) -> int:
    seconds, fraction = text.split(".", 1)
    return int(seconds) * 1_000_000_000 + int((fraction + "000000000")[:9])


def _thread_ref(task: str, pid: str) -> dict[str, Any]:
    return {"task": task.strip(), "pid": int(pid)}


def cluster_sys_trace(path: Path) -> dict[str, Any]:
    """Create wakeup-window candidates, never a claimed critical path.

    This is a Spike adapter.  It needs the input's textual scheduler records and
    therefore deliberately exposes its limits in the resulting artifact.
    """

    trace_sha256 = sha256_file(path)
    windows: dict[int, dict[str, Any]] = {}
    event_counts: Counter[str] = Counter()
    parsed_lines = 0
    total_lines = 0
    last_timestamp_ns: int | None = None
    first_timestamp_ns: int | None = None
    truncated = False

    with path.open("r", encoding="utf-8", errors="replace") as handle:
        for line_number, line in enumerate(handle, start=1):
            total_lines = line_number
            if line_number > MAX_SYS_LINES:
                truncated = True
                break
            match = _TRACE_LINE.match(line)
            if not match:
                continue
            parsed_lines += 1
            event = match.group("event")
            event_counts[event] += 1
            try:
                timestamp_ns = _sys_timestamp_ns(match.group("timestamp"))
            except (ValueError, OverflowError):
                continue
            first_timestamp_ns = timestamp_ns if first_timestamp_ns is None else min(first_timestamp_ns, timestamp_ns)
            last_timestamp_ns = timestamp_ns if last_timestamp_ns is None else max(last_timestamp_ns, timestamp_ns)
            if event != "sched_wakeup":
                continue
            wakeup = _WAKEUP.search(match.group("body"))
            if not wakeup:
                continue
            bucket = timestamp_ns // FRAME_WINDOW_NS
            window = windows.setdefault(
                bucket,
                {
                    "start_ns": timestamp_ns,
                    "end_ns": timestamp_ns,
                    "edges": Counter(),
                    "root_event_ids": [],
                    "threads": {},
                    "module_labels": Counter(),
                    "top_labels": Counter(),
                    "wakeup_count": 0,
                },
            )
            source = _thread_ref(match.group("task"), match.group("pid"))
            target = _thread_ref(wakeup.group("task"), wakeup.group("pid"))
            edge_key = f"{source['task']}:{source['pid']}->{target['task']}:{target['pid']}"
            window["start_ns"] = min(window["start_ns"], timestamp_ns)
            window["end_ns"] = max(window["end_ns"], timestamp_ns)
            window["edges"][edge_key] += 1
            window["wakeup_count"] += 1
            window["module_labels"].update((source["task"], target["task"]))
            window["top_labels"][target["task"]] += 1
            if len(window["root_event_ids"]) < 32:
                window["root_event_ids"].append(f"sys_{line_number}")
            for thread in (source, target):
                if len(window["threads"]) < 40:
                    window["threads"][f"{thread['task']}:{thread['pid']}"] = thread

    window_items = []
    for bucket, window in windows.items():
        top_label = window["top_labels"].most_common(1)[0][0] if window["top_labels"] else None
        module_summary = summarize_trace_modules(
            window["module_labels"].elements(),
            fallback_top_function=top_label,
        )
        window_items.append((bucket, window, module_summary))
    ordered_windows = sorted(
        window_items,
        key=lambda item: (
            item[2]["system_module_only"],
            -item[1]["wakeup_count"],
            -len(item[1]["edges"]),
            item[0],
        ),
    )[:MAX_CLUSTER_CATALOG]
    clusters: list[dict[str, Any]] = []
    hotspot_candidates: list[dict[str, Any]] = []
    for ordinal, (bucket, window, module_summary) in enumerate(ordered_windows, start=1):
        cluster_id = f"cluster_sys_{trace_sha256[:10]}_{bucket:x}"
        edge_items = window["edges"].most_common(8)
        pressure = "high" if window["wakeup_count"] >= 5 else "medium" if window["wakeup_count"] >= 2 else "low"
        clusters.append(
            {
                "cluster_id": cluster_id,
                "time_range_ns": {"start_ns": window["start_ns"], "end_ns": window["end_ns"]},
                "root_event_ids": window["root_event_ids"],
                "thread_refs": list(window["threads"].values()),
                "edge_summary": {
                    "kind": "wakeup_window_candidate",
                    "edge_count": window["wakeup_count"],
                    "unique_edge_count": len(window["edges"]),
                    "top_edges": [{"edge": edge, "count": count} for edge, count in edge_items],
                },
                "module_summary": module_summary,
                "clock_quality": "text_trace_monotonicity_unverified",
                "profile_id": "sys-wakeup-module-catalog-v1",
                "selection_state": "pending",
                "artifact_ref": f"sha256:{trace_sha256}",
                "experimental": True,
            }
        )
        hotspot_candidates.append(
            {
                "hotspot_id": f"hs_sys_{ordinal:03d}",
                "cluster_id": cluster_id,
                "unresolved_reason": "Scheduler wakeup identifiers are not source symbols; source resolution is intentionally blocked.",
                "active_time_metrics": {
                    "measurement": "wakeup_count_in_20ms_window",
                    "wakeup_count": window["wakeup_count"],
                    "window_ns": FRAME_WINDOW_NS,
                },
                "temporal_pressure": pressure,
                "temporal_pressure_notes": ["Experimental wakeup-window density; not a CPU execution duration."],
                "critical_path_metrics": {"available": False, "reason": "Spike does not claim a critical path."},
                "dominance_label": "unknown",
                "evidence_refs": [f"sha256:{trace_sha256}", f"cluster:{cluster_id}"],
            }
        )

    warnings = [
        "Experimental .sys parser only consumes sched_wakeup windows; it does not establish a critical path.",
        "Scheduler task names are not resolved to source functions.",
    ]
    if truncated:
        warnings.append(f"Parsing stopped after {MAX_SYS_LINES} lines; result is partial.")
    if len(window_items) > MAX_CLUSTER_CATALOG:
        warnings.append(f"Cluster catalog is capped at {MAX_CLUSTER_CATALOG} wakeup windows; use a narrower trace for exhaustive text-trace exploration.")
    if event_counts["sched_wakeup"] == 0:
        warnings.append("No sched_wakeup events were found; no wakeup clusters were emitted.")
    return {
        "trace_id": f"sys_{trace_sha256[:16]}",
        "trace_name": path.name,
        "input_kind": "sys_trace_experimental",
        "input_sha256": trace_sha256,
        "experimental": True,
        "quality": {
            "status": "partial" if truncated else "experimental",
            "parsed_lines": parsed_lines,
            "observed_lines": total_lines,
            "supported_event_counts": dict(event_counts),
            "time_range_ns": {"start_ns": first_timestamp_ns, "end_ns": last_timestamp_ns},
            "cluster_catalog": {
                "available_windows": len(window_items),
                "emitted_clusters": len(clusters),
                "limit": MAX_CLUSTER_CATALOG,
                "truncated": len(window_items) > MAX_CLUSTER_CATALOG,
            },
            "warnings": warnings,
        },
        "clusters": clusters,
        "hotspot_candidates": hotspot_candidates,
        "unresolved_hotspots": [item["hotspot_id"] for item in hotspot_candidates],
    }


def parse_json_trace(path: Path) -> dict[str, Any]:
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"trace result is not valid JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ValueError("trace result root must be a JSON object")
    return normalize_external_report(parsed, trace_name=path.name, trace_sha256=sha256_file(path))
