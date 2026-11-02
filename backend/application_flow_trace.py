"""Runtime Trace evidence for application-flow reconstruction.

The exported scenario JSON remains owned by :mod:`application_flow`.  This
module builds an immutable sidecar from event names that were actually observed
in a trace, gives the Agent a bounded retrieval view, and validates the Agent's
stage alignment before asking the existing Mermaid compiler for a new version.
"""

from __future__ import annotations

from hashlib import sha256
import re
from pathlib import Path
from typing import Any

from .application_flow import ApplicationFlowManager
from .core import ArtifactStore, JsonRegistry, PlatformError, utc_now
from .htrace_analysis import analyze_htrace_stage_windows, list_htrace_runtime_event_names


TRACE_ANALYSIS_ARTIFACT_TYPE = "trace_analysis_result"
RUNTIME_TRACE_CATALOG_ARTIFACT_TYPE = "application_flow_runtime_trace_catalog"
SUPPORTED_TRACE_KINDS = {"raw_htrace", "external_trace_analysis_report"}
_MATCH_STATUSES = {"matched", "partial", "unmatched"}
_CONFIDENCE_LEVELS = {"high", "medium", "low"}
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]{3,}|[\u4e00-\u9fff]{2,}")


def _representative_event_timeline(events: list[dict[str, Any]]) -> dict[str, Any]:
    occurrences: list[dict[str, Any]] = []
    source_occurrence_count = 0
    for event in events:
        metrics = _record(event.get("metrics"))
        source_occurrence_count += max(0, int(_number(metrics.get("occurrence_count"))))
        if event.get("ts_ms") is None or event.get("dur_ms") is None:
            continue
        occurrences.append({
            "event_id": _text(event.get("event_id"), maximum=160),
            "name": _text(event.get("name") or event.get("event_name"), maximum=1000),
            "rank": int(metrics.get("rank")) if isinstance(metrics.get("rank"), int) else None,
            "ts_ms": round(_number(event.get("ts_ms")), 6),
            "dur_ms": round(max(0.0, _number(event.get("dur_ms"))), 6),
            "self_time_estimate_duration_ms": (
                round(max(0.0, _number(metrics.get("max_self_time_estimate_duration_ms"))), 6)
                if metrics.get("max_self_time_estimate_duration_ms") is not None
                else None
            ),
            "process_name": _text(event.get("process_name"), maximum=160) or "unknown",
            "thread_name": _text(event.get("thread_name"), maximum=160) or "unknown",
        })
    return {
        "representation": "representative_occurrences" if occurrences else "unavailable",
        "source_occurrence_count": source_occurrence_count,
        "returned_occurrence_count": len(occurrences),
        "truncated": source_occurrence_count > len(occurrences),
        "occurrences": occurrences,
        "density_buckets": [],
        "semantics": "仅包含输入报告为每个热点事件提供的代表性调用区间。",
    }


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any, *, maximum: int = 4096) -> str:
    return value.strip()[:maximum] if isinstance(value, str) else ""


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _tokens(*values: object) -> list[str]:
    result: list[str] = []
    for value in values:
        expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", str(value or ""))
        for token in _TOKEN_RE.findall(expanded):
            normalized = token.casefold()
            if normalized not in result:
                result.append(normalized)
    return result


def _event_name(value: Any) -> str:
    item = _record(value)
    return _text(item.get("name") or item.get("event_name") or item.get("eventName"), maximum=1000)


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _runtime_event_bounds(value: Any) -> tuple[int | None, int | None]:
    metrics = _record(_record(value).get("metrics"))
    return _positive_int(metrics.get("first_start_ns")), _positive_int(metrics.get("last_end_ns"))


def _stage_analysis_event(
    value: Any,
    *,
    rank: int | None = None,
    duration_kind: str = "aggregated_runtime_event_context",
    ranking_kind: str = "aligned_boundary_or_supporting_evidence",
) -> dict[str, Any] | None:
    event = _record(value)
    name = _text(event.get("event_name") or event.get("name"), maximum=1000)
    if not name:
        return None
    metrics = _record(event.get("metrics"))
    context = _record(event.get("trace_context"))
    first_start_ns, _ = _runtime_event_bounds(event)
    direct_ts_ms = _number(event.get("ts_ms")) if event.get("ts_ms") is not None else None
    direct_duration_ms = max(0.0, _number(event.get("dur_ms"))) if event.get("dur_ms") is not None else None
    inclusive_ms = max(0.0, _number(metrics.get("inclusive_total_duration_ms")))
    if inclusive_ms == 0.0 and direct_duration_ms is not None:
        inclusive_ms = direct_duration_ms
    max_span_ms = max(0.0, _number(metrics.get("max_span_duration_ms")) or inclusive_ms)
    event_identity = "\0".join([
        name,
        str(event.get("ts_ms") or ""),
        str(context.get("process_name") or event.get("process_name") or ""),
        str(context.get("thread_name") or event.get("thread_name") or ""),
    ])
    return {
        "event_id": _text(event.get("event_id"), maximum=160) or f"event_runtime_{sha256(event_identity.encode('utf-8')).hexdigest()[:16]}",
        "name": name,
        "ts_ms": round(first_start_ns / 1_000_000.0, 6) if first_start_ns is not None else round(direct_ts_ms, 6) if direct_ts_ms is not None else None,
        "dur_ms": round(max_span_ms, 6) if max_span_ms > 0 else None,
        "process_name": _text(context.get("process_name") or event.get("process_name"), maximum=160) or "unknown",
        "pid": _positive_int(context.get("pid") or event.get("pid")),
        "thread_name": _text(context.get("thread_name") or event.get("thread_name"), maximum=160) or "unknown",
        "tid": _positive_int(context.get("tid") or event.get("tid")),
        "itid": _positive_int(context.get("execution_context_id") or event.get("itid")),
        "metrics": {
            "rank": rank,
            "occurrence_count": max(0, int(_number(metrics.get("occurrence_count")) or (1 if direct_duration_ms is not None else 0))),
            "self_time_estimate_total_duration_ms": None,
            "inclusive_total_duration_ms": round(inclusive_ms, 6),
            "max_self_time_estimate_duration_ms": None,
            "max_span_duration_ms": round(max_span_ms, 6),
        },
        "measurement_semantics": {
            "duration_kind": duration_kind,
            "ranking_kind": ranking_kind,
            "cpu_self_time_proven": False,
        },
    }


class ApplicationFlowRuntimeTraceManager:
    """Connect observed trace labels to immutable application-flow revisions."""

    def __init__(
        self,
        state_root: Path,
        artifact_store: ArtifactStore,
        application_flows: ApplicationFlowManager,
    ) -> None:
        self.state_root = state_root.resolve()
        self.artifact_store = artifact_store
        self.application_flows = application_flows
        self.registry = JsonRegistry(
            self.artifact_store.root / "application-flows" / "runtime-traces.json",
            {
                "schema_version": "application_flow_runtime_trace_registry/v1",
                "deleted_traces": {},
                "analysis_bindings": {},
            },
        )

    @staticmethod
    def _trace_summary(artifact: dict[str, Any]) -> dict[str, Any]:
        quality = _record(artifact.get("quality"))
        trace_import = _record(artifact.get("trace_import"))
        return {
            "artifact_id": artifact.get("artifact_id"),
            "created_at": artifact.get("created_at"),
            "uploaded_at": trace_import.get("uploaded_at") or artifact.get("created_at"),
            "import_instance_id": trace_import.get("import_instance_id"),
            "trace_name": artifact.get("trace_name"),
            "input_kind": artifact.get("input_kind"),
            "analysis_profile": artifact.get("analysis_profile"),
            "source_connector_id": artifact.get("source_connector_id"),
            "stage_count": len(artifact.get("stage_catalog", [])) if isinstance(artifact.get("stage_catalog"), list) else 0,
            "hotspot_count": len(artifact.get("hotspot_candidates", [])) if isinstance(artifact.get("hotspot_candidates"), list) else 0,
            "quality_status": quality.get("status"),
        }

    def _deleted_trace_ids(self) -> set[str]:
        return set(_record(self.registry.read().get("deleted_traces")))

    def read_trace(self, artifact_id: str) -> dict[str, Any]:
        deleted = _record(self.registry.read().get("deleted_traces"))
        if artifact_id in deleted:
            raise PlatformError(
                "APPLICATION_FLOW_TRACE_NOT_FOUND",
                "该真实 Trace 已从应用流程 Trace 列表删除。",
                status_code=404,
                details={"analysis_artifact_id": artifact_id},
            )
        artifact = self.artifact_store.read(TRACE_ANALYSIS_ARTIFACT_TYPE, artifact_id)
        if artifact.get("input_kind") not in SUPPORTED_TRACE_KINDS:
            raise PlatformError(
                "APPLICATION_FLOW_TRACE_KIND_UNSUPPORTED",
                "该工件不是应用流程分析支持的真实 Trace。",
                status_code=422,
                details={"analysis_artifact_id": artifact_id},
            )
        return artifact

    def get_trace(self, artifact_id: str) -> dict[str, Any]:
        return {"trace": self._trace_summary(self.read_trace(artifact_id))}

    def bind_trace(self, application_flow_analysis_id: str, artifact_id: str = "") -> dict[str, Any]:
        analysis_id = application_flow_analysis_id.strip()[:160]
        if not analysis_id:
            raise PlatformError(
                "APPLICATION_FLOW_ANALYSIS_ID_REQUIRED",
                "绑定真实 Trace 需要应用流程分析 ID。",
                status_code=422,
            )
        trace = self.read_trace(artifact_id) if artifact_id else None
        registry = self.registry.read()
        bindings = _record(registry.get("analysis_bindings"))
        if trace is None:
            bindings.pop(analysis_id, None)
        else:
            bindings[analysis_id] = {
                "analysis_artifact_id": artifact_id,
                "bound_at": utc_now(),
            }
        registry["analysis_bindings"] = bindings
        self.registry.write(registry)
        return {
            "status": "bound" if trace is not None else "cleared",
            "application_flow_analysis_id": analysis_id,
            "trace": self._trace_summary(trace) if trace is not None else None,
        }

    def resolve_bound_trace(self, application_flow_analysis_id: str) -> str:
        analysis_id = application_flow_analysis_id.strip()[:160]
        if not analysis_id:
            return ""
        binding = _record(_record(self.registry.read().get("analysis_bindings")).get(analysis_id))
        artifact_id = _text(binding.get("analysis_artifact_id"), maximum=80)
        if not artifact_id:
            return ""
        try:
            self.read_trace(artifact_id)
        except PlatformError as exc:
            if exc.code in {"ARTIFACT_NOT_FOUND", "APPLICATION_FLOW_TRACE_NOT_FOUND"}:
                return ""
            raise
        return artifact_id

    def delete_trace(self, artifact_id: str) -> dict[str, Any]:
        artifact = self.read_trace(artifact_id)
        deleted_at = utc_now()
        registry = self.registry.read()
        deleted = _record(registry.get("deleted_traces"))
        deleted[artifact_id] = {
            "deleted_at": deleted_at,
            "trace_name": artifact.get("trace_name"),
        }
        registry["deleted_traces"] = deleted
        bindings = _record(registry.get("analysis_bindings"))
        cleared_analysis_ids = [
            analysis_id
            for analysis_id, binding in bindings.items()
            if _record(binding).get("analysis_artifact_id") == artifact_id
        ]
        for analysis_id in cleared_analysis_ids:
            bindings.pop(analysis_id, None)
        registry["analysis_bindings"] = bindings
        self.registry.write(registry)
        self.artifact_store.append_event({
            "event_type": "application_flow_runtime_trace_deleted",
            "artifact_id": artifact_id,
            "trace_name": artifact.get("trace_name"),
            "cleared_analysis_binding_count": len(cleared_analysis_ids),
            "created_at": deleted_at,
        })
        return {
            "status": "deleted",
            "analysis_artifact_id": artifact_id,
            "trace_name": artifact.get("trace_name"),
            "deleted_at": deleted_at,
            "cleared_analysis_binding_count": len(cleared_analysis_ids),
            "retention": "immutable_artifacts_preserved",
        }

    def list_traces(self, *, source_connector_ids: list[str] | None = None, limit: int = 100) -> dict[str, Any]:
        connector_filter = {item for item in (source_connector_ids or []) if item}
        deleted_trace_ids = self._deleted_trace_ids()
        if connector_filter:
            by_id: dict[str, dict[str, Any]] = {}
            for connector_id in connector_filter:
                for artifact in self.artifact_store.list_artifacts(
                    TRACE_ANALYSIS_ARTIFACT_TYPE,
                    limit=500,
                    source_connector_id=connector_id,
                ):
                    by_id[str(artifact.get("artifact_id") or "")] = artifact
            available = sorted(by_id.values(), key=lambda item: str(item.get("created_at") or ""), reverse=True)
        else:
            available = self.artifact_store.list_artifacts(TRACE_ANALYSIS_ARTIFACT_TYPE, limit=500)
        traces = [
            artifact
            for artifact in available
            if artifact.get("input_kind") in SUPPORTED_TRACE_KINDS
            and artifact.get("artifact_id") not in deleted_trace_ids
        ]
        traces.sort(
            key=lambda item: (
                str(_record(item.get("trace_import")).get("uploaded_at") or item.get("created_at") or ""),
                str(item.get("artifact_id") or ""),
            ),
            reverse=True,
        )
        traces = traces[:max(1, min(int(limit), 200))]
        return {
            "schema_version": "application_flow_runtime_trace_catalog/v1",
            "traces": [self._trace_summary(item) for item in traces],
            "count": len(traces),
        }

    @staticmethod
    def _merge_external_event(
        events: dict[tuple[str, str, str], dict[str, Any]],
        *,
        analysis_id: str,
        name: str,
        process_name: str = "",
        thread_name: str = "",
        occurrence_count: int = 1,
        inclusive_ms: float = 0.0,
        max_ms: float = 0.0,
        first_start_ns: int = 0,
        last_end_ns: int = 0,
        stage_ids: list[str] | None = None,
    ) -> None:
        normalized_name = name.strip()[:1000]
        if not normalized_name:
            return
        key = (normalized_name, process_name[:160], thread_name[:160])
        current = events.get(key)
        if current is None:
            digest = sha256(f"{analysis_id}\0{normalized_name}\0{process_name}\0{thread_name}".encode("utf-8")).hexdigest()[:16]
            current = {
                "event_id": f"event_report_runtime_{digest}",
                "event_name": normalized_name,
                "event_kind": "reported_trace_label",
                "trace_context": {"process_name": process_name[:160] or "unknown", "thread_name": thread_name[:160] or "unknown"},
                "metrics": {
                    "occurrence_count": 0,
                    "inclusive_total_duration_ms": 0.0,
                    "max_span_duration_ms": 0.0,
                    "first_start_ns": 0,
                    "last_end_ns": 0,
                },
                "reported_stage_ids": [],
                "source_resolution": "observed_runtime_trace_name",
            }
            events[key] = current
        metrics = _record(current.get("metrics"))
        metrics["occurrence_count"] = int(metrics.get("occurrence_count") or 0) + max(0, occurrence_count)
        metrics["inclusive_total_duration_ms"] = round(_number(metrics.get("inclusive_total_duration_ms")) + max(0.0, inclusive_ms), 6)
        metrics["max_span_duration_ms"] = max(_number(metrics.get("max_span_duration_ms")), max(0.0, max_ms))
        previous_first = int(metrics.get("first_start_ns") or 0)
        metrics["first_start_ns"] = min(value for value in (previous_first, first_start_ns) if value > 0) if previous_first > 0 and first_start_ns > 0 else max(previous_first, first_start_ns)
        metrics["last_end_ns"] = max(int(metrics.get("last_end_ns") or 0), last_end_ns)
        current["metrics"] = metrics
        linked_stages = current.setdefault("reported_stage_ids", [])
        for stage_id in stage_ids or []:
            if stage_id and stage_id not in linked_stages:
                linked_stages.append(stage_id)

    def _external_events(self, analysis: dict[str, Any]) -> dict[str, Any]:
        analysis_id = str(analysis.get("artifact_id") or "unknown")
        collected: dict[tuple[str, str, str], dict[str, Any]] = {}
        stage_records = [item for item in analysis.get("stage_catalog", []) if isinstance(item, dict)] if isinstance(analysis.get("stage_catalog"), list) else []
        if not stage_records:
            for candidate in analysis.get("hotspot_candidates", []):
                item = _record(candidate)
                name = _text(item.get("trace_event_name"), maximum=1000)
                if not name:
                    continue
                metrics = _record(item.get("metrics"))
                active = _record(item.get("active_time_metrics"))
                stage = _record(item.get("stage_context"))
                occurrences = [entry for entry in item.get("observed_occurrences", []) if isinstance(entry, dict)] if isinstance(item.get("observed_occurrences"), list) else []
                first_occurrence = _record(occurrences[0]) if occurrences else {}
                last_occurrence = _record(occurrences[-1]) if occurrences else {}
                first_ns = int(_number(first_occurrence.get("ts_ms")) * 1_000_000)
                last_ns = int((_number(last_occurrence.get("ts_ms")) + _number(last_occurrence.get("dur_ms"))) * 1_000_000)
                self._merge_external_event(
                    collected,
                    analysis_id=analysis_id,
                    name=name,
                    process_name=_text(first_occurrence.get("process_name") or item.get("module"), maximum=160),
                    thread_name=_text(first_occurrence.get("thread_name"), maximum=160),
                    occurrence_count=int(metrics.get("occurrence_count") or active.get("call_count") or len(occurrences) or 1),
                    inclusive_ms=_number(metrics.get("inclusive_total_duration_ms") or active.get("total_duration_ms")),
                    max_ms=_number(metrics.get("max_span_duration_ms") or active.get("max_duration_ms")),
                    first_start_ns=first_ns,
                    last_end_ns=last_ns,
                    stage_ids=[_text(stage.get("stage_id"), maximum=80)],
                )

        seen_occurrences: set[tuple[str, str, str, float, float]] = set()

        def merge_stage_event(event: dict[str, Any], stage_id: str, time_range: dict[str, Any]) -> None:
            name = _event_name(event)
            if not name:
                return
            process_name = _text(event.get("process_name"), maximum=160)
            thread_name = _text(event.get("thread_name"), maximum=160)
            ts_ms = _number(event.get("ts_ms"))
            dur_ms = _number(event.get("dur_ms"))
            occurrence_key = (name, process_name, thread_name, ts_ms, dur_ms)
            duplicate = occurrence_key in seen_occurrences
            seen_occurrences.add(occurrence_key)
            metrics = _record(event.get("metrics"))
            occurrence_count = int(metrics.get("occurrence_count") or metrics.get("call_count") or 1)
            inclusive_ms = _number(metrics.get("inclusive_total_duration_ms") or metrics.get("total_duration_ms") or dur_ms)
            max_ms = _number(metrics.get("max_span_duration_ms") or metrics.get("max_duration_ms") or dur_ms)
            self._merge_external_event(
                collected,
                analysis_id=analysis_id,
                name=name,
                process_name=process_name,
                thread_name=thread_name,
                occurrence_count=0 if duplicate else occurrence_count,
                inclusive_ms=0.0 if duplicate else inclusive_ms,
                max_ms=0.0 if duplicate else max_ms,
                first_start_ns=(int(ts_ms * 1_000_000) or int(time_range.get("start_ns") or 0)) if not duplicate else 0,
                last_end_ns=(int((ts_ms + dur_ms) * 1_000_000) or int(time_range.get("end_ns") or 0)) if not duplicate else 0,
                stage_ids=[stage_id],
            )

        for raw_stage in stage_records:
            stage = _record(raw_stage)
            stage_id = _text(stage.get("stage_id"), maximum=80)
            time_range = _record(stage.get("time_range_ns"))
            for key in ("start_event", "end_event"):
                event = _record(stage.get(key))
                merge_stage_event(event, stage_id, time_range)
            for key in ("top_events", "investigation_anchors"):
                for raw_event in stage.get(key, []) if isinstance(stage.get(key), list) else []:
                    event = _record(raw_event)
                    merge_stage_event(event, stage_id, time_range)
        events = list(collected.values())
        events.sort(key=lambda item: (-_number(_record(item.get("metrics")).get("inclusive_total_duration_ms")), str(item.get("event_name") or "")))
        for rank, event in enumerate(events, start=1):
            event["rank"] = rank
        return {
            "events": events,
            "quality": {
                "status": "available" if events else "unavailable",
                "profile_id": "external-report-runtime-event-name-catalog/v1",
                "source": "normalized_report_trace_labels",
                "total_distinct_event_names": len({str(item.get("event_name")) for item in events}),
                "returned_event_names": len(events),
                "truncated": False,
                "measurement_semantics": "report_supplied",
                "limitations": [
                    "The catalogue contains only labels present in the supplied report; it is not necessarily an exhaustive raw trace.",
                    "Stage envelopes and listed event durations do not establish CPU self time.",
                ],
            },
        }

    @staticmethod
    def _scenario_terms(
        scenario_artifact: dict[str, Any],
        query: str,
    ) -> tuple[list[str], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        scenario = _record(scenario_artifact.get("scenario_json"))
        hierarchy = _record(scenario_artifact.get("stage_hierarchy"))
        implementations = [item for item in hierarchy.get("implementations", []) if isinstance(item, dict)] if isinstance(hierarchy.get("implementations"), list) else []
        stage_profiles: dict[str, dict[str, Any]] = {}
        node_profiles: dict[str, dict[str, Any]] = {}
        all_terms: list[str] = _tokens(query, scenario_artifact.get("flow_name"))
        for raw_stage in scenario.get("stages", []):
            stage = _record(raw_stage)
            stage_id = _text(stage.get("id"), maximum=80)
            implementation_symbols = [
                _text(item.get("symbol"), maximum=1000)
                for item in implementations
                if _text(item.get("stage_id"), maximum=80) == stage_id
            ]
            locator_names = [
                _text(_record(stage.get(boundary)).get("name"), maximum=1000)
                for boundary in ("start", "end")
                if _record(stage.get(boundary)).get("type") == "trace_point"
            ]
            terms = _tokens(stage.get("name"), stage.get("process_name"), *implementation_symbols, *locator_names)
            stage_profiles[stage_id] = {
                "stage_id": stage_id,
                "stage_name": stage.get("name"),
                "process_name": stage.get("process_name"),
                "implementation_symbols": implementation_symbols,
                "source_locator_names": locator_names,
                "terms": terms,
            }
            for term in terms:
                if term not in all_terms:
                    all_terms.append(term)
        for raw_node in scenario.get("nodes", []):
            node = _record(raw_node)
            node_id = _text(node.get("id"), maximum=80)
            node_type = _text(node.get("type"), maximum=20)
            if not node_id or node_type == "stage":
                continue
            implementation_symbols = [
                _text(item.get("symbol"), maximum=1000)
                for item in implementations
                if (
                    node_type == "activity"
                    and _text(item.get("activity_id"), maximum=80) == node_id
                )
            ]
            locator_names = [
                _text(_record(node.get(boundary)).get("name"), maximum=1000)
                for boundary in ("start", "end")
                if _record(node.get(boundary)).get("type") == "trace_point"
            ]
            terms = _tokens(
                node.get("name"),
                node.get("description"),
                node.get("process_name"),
                *implementation_symbols,
                *locator_names,
            )
            node_profiles[node_id] = {
                "stage_id": node_id,
                "stage_name": node.get("name"),
                "node_type": node_type,
                "parent_id": node.get("parent_id"),
                "process_name": node.get("process_name"),
                "implementation_symbols": implementation_symbols,
                "source_locator_names": locator_names,
                "terms": terms,
            }
            for term in terms:
                if term not in all_terms:
                    all_terms.append(term)
        return all_terms[:96], stage_profiles, node_profiles

    @classmethod
    def _node_hints(
        cls,
        events: list[dict[str, Any]],
        node_profiles: dict[str, dict[str, Any]],
        *,
        per_node_limit: int,
    ) -> list[dict[str, Any]]:
        hints = cls._stage_hints(events, node_profiles, per_stage_limit=per_node_limit)
        output: list[dict[str, Any]] = []
        for hint in hints:
            node_id = str(hint.get("stage_id") or "")
            profile = _record(node_profiles.get(node_id))
            output.append({
                "node_id": node_id,
                "node_name": hint.get("stage_name"),
                "node_type": profile.get("node_type"),
                "parent_id": profile.get("parent_id"),
                "candidates": hint.get("candidates", []),
                "candidate_count": hint.get("candidate_count", 0),
                "retrieval_note": hint.get("retrieval_note"),
            })
        return output

    @staticmethod
    def _stage_hints(events: list[dict[str, Any]], stage_profiles: dict[str, dict[str, Any]], *, per_stage_limit: int) -> list[dict[str, Any]]:
        output: list[dict[str, Any]] = []
        for stage_id, profile in stage_profiles.items():
            locators = [value.casefold() for value in profile["source_locator_names"] if value]
            symbols = [value.rsplit("::", 1)[-1].split("(", 1)[0].casefold() for value in profile["implementation_symbols"] if value]
            terms = list(profile["terms"])
            candidates: list[dict[str, Any]] = []
            for event in events:
                name = str(event.get("event_name") or "")
                normalized_name = name.casefold()
                event_tokens = set(_tokens(name))
                reasons: list[str] = []
                score = 0
                if normalized_name in locators:
                    score += 120
                    reasons.append("与源码候选事件名完全相同")
                elif any(locator and (locator in normalized_name or normalized_name in locator) for locator in locators):
                    score += 85
                    reasons.append("与源码候选事件名互为子串")
                symbol_matches = [symbol for symbol in symbols if symbol and symbol in normalized_name]
                if symbol_matches:
                    score += 65
                    reasons.append("包含实现函数标识")
                overlap = sorted(event_tokens.intersection(terms))
                if overlap:
                    score += min(40, len(overlap) * 8)
                    reasons.append(f"共享检索词：{', '.join(overlap[:5])}")
                process_name = _text(_record(event.get("trace_context")).get("process_name"), maximum=160).casefold()
                expected_process = _text(profile.get("process_name"), maximum=160).casefold()
                if expected_process and expected_process != "unknown" and expected_process in process_name:
                    score += 15
                    reasons.append("进程上下文相容")
                if score <= 0:
                    continue
                candidates.append({
                    "event_id": event.get("event_id"),
                    "event_name": event.get("event_name"),
                    "retrieval_score": score,
                    "retrieval_reasons": reasons,
                    "trace_context": event.get("trace_context"),
                    "metrics": event.get("metrics"),
                })
            candidates.sort(key=lambda item: (-int(item["retrieval_score"]), -_number(_record(item.get("metrics")).get("inclusive_total_duration_ms")), str(item.get("event_name") or "")))
            selected_candidates: list[dict[str, Any]] = []
            contexts_per_name: dict[str, int] = {}
            for candidate in candidates:
                event_name = str(candidate.get("event_name") or "")
                if contexts_per_name.get(event_name, 0) >= 3:
                    continue
                selected_candidates.append(candidate)
                contexts_per_name[event_name] = contexts_per_name.get(event_name, 0) + 1
                if len(selected_candidates) >= per_stage_limit:
                    break
            output.append({
                "stage_id": stage_id,
                "stage_name": profile.get("stage_name"),
                "candidates": selected_candidates,
                "candidate_count": len(candidates),
                "retrieval_note": "排序只用于缩小候选，并限制同名事件占满 Top-N；阶段语义、边界角色和时序仍由 Agent 判断。",
            })
        return output

    def inspect_trace(
        self,
        *,
        analysis_artifact_id: str,
        scenario_artifact_id: str,
        query: str = "",
        global_limit: int = 120,
        per_stage_limit: int = 12,
    ) -> dict[str, Any]:
        analysis = self.read_trace(analysis_artifact_id)
        if analysis.get("input_kind") not in SUPPORTED_TRACE_KINDS:
            raise PlatformError("APPLICATION_FLOW_TRACE_UNSUPPORTED", "请选择原始 .htrace 或阶段分析 JSON。", status_code=422)
        scenario_artifact = self.application_flows.read_scenario(scenario_artifact_id)
        source_scopes = [item for item in scenario_artifact.get("source_scopes", []) if isinstance(item, dict)] if isinstance(scenario_artifact.get("source_scopes"), list) else []
        allowed_connectors = {str(item.get("source_connector_id") or "") for item in source_scopes}
        allowed_connectors.add(str(scenario_artifact.get("source_connector_id") or ""))
        if analysis.get("source_connector_id") not in allowed_connectors:
            raise PlatformError(
                "APPLICATION_FLOW_TRACE_SOURCE_MISMATCH",
                "真实 Trace 未绑定到当前流程所选代码仓范围。",
                status_code=409,
                details={"trace_source_connector_id": analysis.get("source_connector_id"), "allowed_source_connector_ids": sorted(allowed_connectors)},
            )
        terms, stage_profiles, node_profiles = self._scenario_terms(scenario_artifact, query)
        if analysis.get("input_kind") == "raw_htrace":
            raw_htrace = _record(analysis.get("raw_htrace"))
            extracted = list_htrace_runtime_event_names(
                raw_htrace,
                state_root=self.state_root,
                search_terms=terms,
                limit=max(240, min(2_000, int(global_limit) * 6)),
            )
        else:
            extracted = self._external_events(analysis)
        events = [item for item in extracted.get("events", []) if isinstance(item, dict)]
        if not events:
            raise PlatformError(
                "APPLICATION_FLOW_TRACE_EVENTS_UNAVAILABLE",
                "所选 Trace 没有可用于阶段匹配的真实事件名。",
                status_code=422,
                details={"quality": extracted.get("quality")},
            )
        hints = self._stage_hints(events, stage_profiles, per_stage_limit=max(1, min(int(per_stage_limit), 30)))
        node_hints = self._node_hints(
            events,
            node_profiles,
            per_node_limit=max(1, min(int(per_stage_limit), 30)),
        )
        catalog_artifact = self.artifact_store.write(
            RUNTIME_TRACE_CATALOG_ARTIFACT_TYPE,
            {
                "analysis_artifact_id": analysis_artifact_id,
                "scenario_artifact_id": scenario_artifact_id,
                "flow_id": scenario_artifact.get("flow_id"),
                "trace": self._trace_summary(analysis),
                "retrieval_terms": terms,
                "events": events,
                "stage_candidate_hints": hints,
                "node_candidate_hints": node_hints,
                "quality": extracted.get("quality"),
                "contract_note": "候选排序不是阶段匹配；只有 apply_runtime_trace_alignment 接受的 event_id 才形成已校验侧车证据。",
            },
            source_connector_id=str(scenario_artifact.get("source_connector_id") or ""),
            source_binding=_record(scenario_artifact.get("source_binding")),
            producer={"name": "application-flow-runtime-trace-retrieval", "version": "1.0.0"},
            input_artifacts=[analysis_artifact_id, scenario_artifact_id],
        )
        bounded_global = max(1, min(int(global_limit), 300))
        return {
            "status": "succeeded",
            "schema_version": "application_flow_runtime_trace_inspection/v1",
            "event_catalog_artifact_id": catalog_artifact["artifact_id"],
            "trace": self._trace_summary(analysis),
            "scenario": {
                "artifact_id": scenario_artifact_id,
                "flow_id": scenario_artifact.get("flow_id"),
                "flow_name": scenario_artifact.get("flow_name"),
                "stage_count": len(stage_profiles),
            },
            "catalog_quality": extracted.get("quality"),
            "runtime_events": events[:bounded_global],
            "stage_candidate_hints": hints,
            "node_candidate_hints": node_hints,
            "guidance": [
                "先依据阶段语义、源码实现、真实事件名和时序判断，不把 retrieval_score 当作结论。",
                "一个 scope 事件可以同时作为同一阶段的 start 与 end；异步 begin/end 必须分别选择对应事件。",
                "Trace 暴露出缺失、合并、拆分或顺序差异时，可以修订 Mermaid，但仍须遵循当前 @ 元数据和场景 v2 Schema。",
            ],
        }

    def _build_stage_analysis(
        self,
        *,
        analysis: dict[str, Any],
        scenario: dict[str, Any],
        stage_matches: list[dict[str, Any]],
        node_matches: list[dict[str, Any]] | None = None,
        top_event_limit: int = 30,
    ) -> dict[str, Any]:
        """Project validated runtime alignment into a workload-analysis sidecar."""

        bounded_limit = max(1, min(int(top_event_limit), 50))
        scenario_stages = [item for item in scenario.get("stages", []) if isinstance(item, dict)]
        scenario_nodes = [item for item in scenario.get("nodes", []) if isinstance(item, dict)]
        if not scenario_nodes:
            scenario_nodes = [
                {
                    "id": item.get("id"),
                    "type": "stage",
                    "parent_id": None,
                    "name": item.get("name"),
                    "order": item.get("order"),
                    "process_name": item.get("process_name"),
                }
                for item in scenario_stages
            ]
        node_metadata_by_id = {
            str(item.get("id") or ""): item
            for item in scenario_nodes
            if str(item.get("id") or "")
        }
        match_by_stage = {
            str(item.get("stage_id") or ""): item
            for item in stage_matches
            if isinstance(item, dict)
        }
        match_by_node = {
            str(item.get("node_id") or ""): item
            for item in node_matches or []
            if isinstance(item, dict)
        }
        report_stage_by_id = {
            str(item.get("stage_id") or item.get("id") or ""): item
            for item in analysis.get("stage_catalog", [])
            if isinstance(item, dict) and str(item.get("stage_id") or item.get("id") or "")
        } if isinstance(analysis.get("stage_catalog"), list) else {}
        external_report = analysis.get("input_kind") == "external_trace_analysis_report"
        report_stage_metrics_available = False
        stage_rows: list[dict[str, Any]] = []
        window_specs: list[dict[str, Any]] = []
        process_names_by_id: dict[str, list[str]] = {}

        for raw_stage in scenario_stages:
            stage_id = str(raw_stage.get("id") or "")
            match = _record(match_by_stage.get(stage_id))
            start_event = _record(match.get("start_event"))
            end_event = _record(match.get("end_event"))
            supporting_events = [
                item for item in match.get("supporting_events", []) if isinstance(item, dict)
            ] if isinstance(match.get("supporting_events"), list) else []
            referenced_events = [
                item for item in [start_event, end_event, *supporting_events] if item
            ]
            reported_stage_ids: list[str] = []
            for event in referenced_events:
                raw_ids = event.get("reported_stage_ids")
                if not isinstance(raw_ids, list):
                    continue
                for raw_id in raw_ids:
                    stage_ref = _text(raw_id, maximum=80)
                    if stage_ref and stage_ref not in reported_stage_ids:
                        reported_stage_ids.append(stage_ref)
            linked_report_stages = [
                report_stage_by_id[stage_ref]
                for stage_ref in reported_stage_ids
                if stage_ref in report_stage_by_id
            ]
            starts: list[int] = []
            ends: list[int] = []
            for event in referenced_events:
                first_start_ns, last_end_ns = _runtime_event_bounds(event)
                if first_start_ns is not None:
                    starts.append(first_start_ns)
                if last_end_ns is not None:
                    ends.append(last_end_ns)
            start_boundary, _ = _runtime_event_bounds(start_event)
            _, end_boundary = _runtime_event_bounds(end_event)
            start_ns = start_boundary if start_boundary is not None else (min(starts) if starts else None)
            end_ns = end_boundary if end_boundary is not None else (max(ends) if ends else None)
            report_starts = [
                value
                for report_stage in linked_report_stages
                if (value := _positive_int(_record(report_stage.get("time_range_ns")).get("start_ns"))) is not None
            ]
            report_ends = [
                value
                for report_stage in linked_report_stages
                if (value := _positive_int(_record(report_stage.get("time_range_ns")).get("end_ns"))) is not None
            ]
            if external_report and report_starts and report_ends:
                start_ns = min(report_starts)
                end_ns = max(report_ends)
                report_stage_metrics_available = True
            if start_ns is not None and end_ns is not None and end_ns < start_ns:
                start_ns = min(starts) if starts else start_ns
                end_ns = max(ends) if ends else end_ns
            if start_ns is not None and end_ns is not None and end_ns < start_ns:
                start_ns = None
                end_ns = None

            unique_events: dict[str, dict[str, Any]] = {}
            for event in referenced_events:
                event_id = _text(event.get("event_id"), maximum=160)
                identity = event_id or f"{_event_name(event)}\0{_record(event.get('trace_context')).get('process_name')}"
                unique_events.setdefault(identity, event)
            ranked_events = sorted(
                unique_events.values(),
                key=lambda item: (
                    -_number(_record(item.get("metrics")).get("inclusive_total_duration_ms")),
                    _event_name(item),
                ),
            )[:bounded_limit]
            fallback_top_events = [
                normalized
                for rank, event in enumerate(ranked_events, start=1)
                if (normalized := _stage_analysis_event(event, rank=rank)) is not None
            ]
            fallback_event_count = sum(
                max(0, int(_number(_record(event.get("metrics")).get("occurrence_count")) or 0))
                for event in unique_events.values()
            )
            fallback_total_duration_ms = round(sum(
                max(0.0, _number(_record(event.get("metrics")).get("inclusive_total_duration_ms")))
                for event in unique_events.values()
            ), 6)
            report_top_events: dict[tuple[str, float, str, str], dict[str, Any]] = {}
            for report_stage in linked_report_stages:
                for raw_event in report_stage.get("top_events", []) if isinstance(report_stage.get("top_events"), list) else []:
                    event = _record(raw_event)
                    name = _event_name(event)
                    if not name:
                        continue
                    identity = (
                        name,
                        _number(event.get("ts_ms")),
                        _text(event.get("process_name"), maximum=160),
                        _text(event.get("thread_name"), maximum=160),
                    )
                    report_top_events.setdefault(identity, event)
            ranked_report_events = sorted(
                report_top_events.values(),
                key=lambda item: (-_number(item.get("dur_ms")), _event_name(item)),
            )[:bounded_limit]
            normalized_report_top_events = [
                normalized
                for rank, event in enumerate(ranked_report_events, start=1)
                if (normalized := _stage_analysis_event(
                    event,
                    rank=rank,
                    duration_kind="report_supplied_event_span",
                    ranking_kind="report_supplied_stage_top_events",
                )) is not None
            ]
            reported_counts = [
                count
                for report_stage in linked_report_stages
                if (count := _positive_int(report_stage.get("reported_event_count"))) is not None
            ]
            reported_durations = [
                max(0.0, _number(report_stage.get("reported_total_event_duration_ms")))
                for report_stage in linked_report_stages
                if report_stage.get("reported_total_event_duration_ms") is not None
            ]
            if external_report and linked_report_stages:
                if reported_counts:
                    fallback_event_count = sum(reported_counts)
                if reported_durations:
                    fallback_total_duration_ms = round(sum(reported_durations), 6)
                if normalized_report_top_events:
                    fallback_top_events = normalized_report_top_events
            process_names = sorted({
                _text(_record(event.get("trace_context")).get("process_name"), maximum=160)
                for event in referenced_events
                if _text(_record(event.get("trace_context")).get("process_name"), maximum=160) not in {"", "unknown"}
            })
            row = {
                "id": stage_id,
                "name": _text(raw_stage.get("name"), maximum=512) or stage_id,
                "order": int(raw_stage.get("order") or len(stage_rows) + 1),
                "type": "stage",
                "parent_id": _record(node_metadata_by_id.get(stage_id)).get("parent_id"),
                "start_ns": start_ns,
                "end_ns": end_ns,
                "start_ms": round(start_ns / 1_000_000.0, 6) if start_ns is not None else None,
                "end_ms": round(end_ns / 1_000_000.0, 6) if end_ns is not None else None,
                "duration_ms": round((end_ns - start_ns) / 1_000_000.0, 6) if start_ns is not None and end_ns is not None else None,
                "event_count": fallback_event_count,
                "total_event_duration_ms": fallback_total_duration_ms,
                "start_event": _stage_analysis_event(start_event),
                "end_event": _stage_analysis_event(end_event),
                "top_events": fallback_top_events,
                "event_timeline": _representative_event_timeline(fallback_top_events),
                "match_status": str(match.get("match_status") or "unmatched"),
                "confidence": str(match.get("confidence") or "low"),
                "coverage": "aligned_event_evidence" if referenced_events else "no_runtime_boundary",
                "boundary_source": "direct_trace" if referenced_events else "unresolved",
                "boundary_coverage": (
                    "full" if start_ns is not None and end_ns is not None
                    else "partial" if start_ns is not None or end_ns is not None
                    else "unresolved"
                ),
                "start_edge": "start",
                "end_edge": "end",
            }
            stage_rows.append(row)
            process_names_by_id[stage_id] = process_names
            if start_ns is not None and end_ns is not None:
                window_specs.append({
                    "id": stage_id,
                    "start_ns": start_ns,
                    "end_ns": end_ns,
                    "process_names": process_names,
                })

        node_rows_by_id = {str(item["id"]): item for item in stage_rows}
        for node in scenario_nodes:
            node_id = str(node.get("id") or "")
            if node.get("type") == "stage":
                continue
            match = _record(match_by_node.get(node_id))
            start_event = _record(match.get("start_event"))
            end_event = _record(match.get("end_event"))
            supporting_events = [
                item for item in match.get("supporting_events", []) if isinstance(item, dict)
            ] if isinstance(match.get("supporting_events"), list) else []
            referenced_events = [item for item in [start_event, end_event, *supporting_events] if item]
            starts: list[int] = []
            ends: list[int] = []
            for event in referenced_events:
                first_start_ns, last_end_ns = _runtime_event_bounds(event)
                if first_start_ns is not None:
                    starts.append(first_start_ns)
                if last_end_ns is not None:
                    ends.append(last_end_ns)
            start_boundary, _ = _runtime_event_bounds(start_event)
            _, end_boundary = _runtime_event_bounds(end_event)
            start_ns = start_boundary if start_boundary is not None else (min(starts) if starts else None)
            end_ns = end_boundary if end_boundary is not None else (max(ends) if ends else None)
            if start_ns is not None and end_ns is not None and end_ns < start_ns:
                start_ns = min(starts) if starts else None
                end_ns = max(ends) if ends else None
            unique_events: dict[str, dict[str, Any]] = {}
            for event in referenced_events:
                event_id = _text(event.get("event_id"), maximum=160)
                identity = event_id or f"{_event_name(event)}\0{_record(event.get('trace_context')).get('process_name')}"
                unique_events.setdefault(identity, event)
            ranked_events = sorted(
                unique_events.values(),
                key=lambda item: (
                    -_number(_record(item.get("metrics")).get("inclusive_total_duration_ms")),
                    _event_name(item),
                ),
            )[:bounded_limit]
            top_events = [
                normalized
                for rank, event in enumerate(ranked_events, start=1)
                if (normalized := _stage_analysis_event(event, rank=rank)) is not None
            ]
            process_names = sorted({
                _text(_record(event.get("trace_context")).get("process_name"), maximum=160)
                for event in referenced_events
                if _text(_record(event.get("trace_context")).get("process_name"), maximum=160) not in {"", "unknown"}
            })
            row = {
                "id": node_id,
                "name": _text(node.get("name"), maximum=512) or node_id,
                "order": int(node.get("order") or 1),
                "type": str(node.get("type") or "activity"),
                "parent_id": node.get("parent_id"),
                "start_ns": start_ns,
                "end_ns": end_ns,
                "start_ms": round(start_ns / 1_000_000.0, 6) if start_ns is not None else None,
                "end_ms": round(end_ns / 1_000_000.0, 6) if end_ns is not None else None,
                "duration_ms": round((end_ns - start_ns) / 1_000_000.0, 6) if start_ns is not None and end_ns is not None else None,
                "event_count": sum(
                    max(0, int(_number(_record(event.get("metrics")).get("occurrence_count")) or 0))
                    for event in unique_events.values()
                ),
                "total_event_duration_ms": round(sum(
                    max(0.0, _number(_record(event.get("metrics")).get("inclusive_total_duration_ms")))
                    for event in unique_events.values()
                ), 6),
                "start_event": _stage_analysis_event(start_event),
                "end_event": _stage_analysis_event(end_event),
                "top_events": top_events,
                "event_timeline": _representative_event_timeline(top_events),
                "match_status": str(match.get("match_status") or "unmatched"),
                "confidence": str(match.get("confidence") or "low"),
                "coverage": "aligned_event_evidence" if referenced_events else "no_runtime_boundary",
                "boundary_source": "direct_trace" if referenced_events else "unresolved",
                "boundary_coverage": (
                    "full" if start_ns is not None and end_ns is not None
                    else "partial" if start_ns is not None or end_ns is not None
                    else "unresolved"
                ),
                "start_edge": "start",
                "end_edge": "end",
            }
            node_rows_by_id[node_id] = row
            process_names_by_id[node_id] = process_names

        children_by_parent: dict[str, list[str]] = {}
        for node in scenario_nodes:
            parent_id = str(node.get("parent_id") or "")
            node_id = str(node.get("id") or "")
            if parent_id and node_id:
                children_by_parent.setdefault(parent_id, []).append(node_id)
        for node_type in ("stage", "phase", "action"):
            for node in scenario_nodes:
                if node.get("type") != node_type:
                    continue
                node_id = str(node.get("id") or "")
                row = node_rows_by_id.get(node_id)
                if row is None:
                    continue
                children = [
                    node_rows_by_id[child_id]
                    for child_id in children_by_parent.get(node_id, [])
                    if child_id in node_rows_by_id
                ]
                timed_children = [
                    child for child in children
                    if isinstance(child.get("start_ns"), int) and isinstance(child.get("end_ns"), int)
                ]
                if not timed_children or (
                    isinstance(row.get("start_ns"), int) and isinstance(row.get("end_ns"), int)
                ):
                    continue
                start_child = min(timed_children, key=lambda item: int(item["start_ns"]))
                end_child = max(timed_children, key=lambda item: int(item["end_ns"]))
                start_ns = row.get("start_ns") if isinstance(row.get("start_ns"), int) else int(start_child["start_ns"])
                end_ns = row.get("end_ns") if isinstance(row.get("end_ns"), int) else int(end_child["end_ns"])
                if end_ns < start_ns:
                    continue
                child_events: dict[str, dict[str, Any]] = {}
                for child in timed_children:
                    for event in child.get("top_events", []):
                        if not isinstance(event, dict):
                            continue
                        identity = _text(event.get("event_id"), maximum=160) or (
                            f"{_event_name(event)}\0{event.get('ts_ms')}"
                        )
                        child_events.setdefault(identity, event)
                top_events = sorted(
                    child_events.values(),
                    key=lambda item: (
                        -_number(_record(item.get("metrics")).get("inclusive_total_duration_ms")),
                        _event_name(item),
                    ),
                )[:bounded_limit]
                row.update({
                    "start_ns": start_ns,
                    "end_ns": end_ns,
                    "start_ms": round(start_ns / 1_000_000.0, 6),
                    "end_ms": round(end_ns / 1_000_000.0, 6),
                    "duration_ms": round((end_ns - start_ns) / 1_000_000.0, 6),
                    "event_count": sum(int(child.get("event_count") or 0) for child in timed_children),
                    "total_event_duration_ms": round(sum(
                        _number(child.get("total_event_duration_ms")) for child in timed_children
                    ), 6),
                    "start_event": row.get("start_event") or start_child.get("start_event"),
                    "end_event": row.get("end_event") or end_child.get("end_event"),
                    "top_events": top_events,
                    "event_timeline": _representative_event_timeline(top_events),
                    "match_status": "matched" if all(
                        child.get("match_status") == "matched" for child in timed_children
                    ) else "partial",
                    "confidence": "high" if all(
                        child.get("confidence") == "high" for child in timed_children
                    ) else "medium",
                    "coverage": "derived_child_envelope",
                    "boundary_source": "derived_from_children",
                    "boundary_coverage": "full",
                })
                process_names_by_id[node_id] = sorted({
                    process_name
                    for child in timed_children
                    for process_name in process_names_by_id.get(str(child.get("id") or ""), [])
                })

        node_rows = [
            node_rows_by_id[str(node.get("id") or "")]
            for node in scenario_nodes
            if str(node.get("id") or "") in node_rows_by_id
        ]
        window_specs = [
            {
                "id": str(row["id"]),
                "start_ns": int(row["start_ns"]),
                "end_ns": int(row["end_ns"]),
                "process_names": process_names_by_id.get(str(row["id"]), []),
            }
            for row in node_rows
            if isinstance(row.get("start_ns"), int) and isinstance(row.get("end_ns"), int)
        ]
        raw_stage_metrics: dict[str, dict[str, Any]] = {}
        raw_analysis_available = False
        if analysis.get("input_kind") == "raw_htrace" and window_specs:
            try:
                extracted = analyze_htrace_stage_windows(
                    _record(analysis.get("raw_htrace")),
                    state_root=self.state_root,
                    stages=window_specs,
                    top_event_limit=bounded_limit,
                )
                raw_stage_metrics = {
                    str(item.get("stage_id") or ""): item
                    for item in extracted.get("stages", [])
                    if isinstance(item, dict)
                }
                raw_analysis_available = _record(extracted.get("quality")).get("status") == "available"
            except PlatformError:
                # Runtime alignment remains useful even when the converted DB
                # cannot provide a detailed stage-window breakdown.
                raw_stage_metrics = {}

        for row in node_rows:
            raw_metrics = raw_stage_metrics.get(str(row["id"]))
            if raw_metrics is None:
                continue
            row["event_count"] = max(0, int(raw_metrics.get("event_count") or 0))
            row["total_event_duration_ms"] = round(max(0.0, _number(raw_metrics.get("total_event_duration_ms"))), 6)
            row["top_events"] = [
                item for item in raw_metrics.get("top_events", []) if isinstance(item, dict)
            ][:bounded_limit]
            row["event_timeline"] = _record(raw_metrics.get("event_timeline"))
            row["coverage"] = (
                "raw_trace_stage_window" if row.get("type") == "stage"
                else "raw_trace_node_window"
            )

        timed_stages = [
            row for row in stage_rows
            if isinstance(row.get("start_ns"), int) and isinstance(row.get("end_ns"), int)
        ]
        trace_start_ns = min((int(row["start_ns"]) for row in timed_stages), default=None)
        trace_end_ns = max((int(row["end_ns"]) for row in timed_stages), default=None)
        timed_nodes = [
            row for row in node_rows
            if isinstance(row.get("start_ns"), int) and isinstance(row.get("end_ns"), int)
        ]
        node_type_counts = {
            node_type: sum(row.get("type") == node_type for row in node_rows)
            for node_type in ("action", "phase", "stage", "activity")
        }
        return {
            "schema_version": "application_flow_stage_analysis/v1",
            "summary": {
                "total_stages": len(stage_rows),
                "timed_stages": len(timed_stages),
                "matched_stages": sum(row["match_status"] == "matched" for row in stage_rows),
                "partial_stages": sum(row["match_status"] == "partial" for row in stage_rows),
                "unmatched_stages": sum(row["match_status"] == "unmatched" for row in stage_rows),
                "total_nodes": len(node_rows),
                "timed_nodes": len(timed_nodes),
                "node_type_counts": node_type_counts,
                "scenario_id": _text(scenario.get("scenario"), maximum=80) or "unknown",
                "scenario_name": _text(scenario.get("description") or scenario.get("scenario"), maximum=512) or "未命名流程",
                "capture_kind": str(analysis.get("input_kind") or "external_trace_analysis_report"),
                "trace_analysis_artifact_id": str(analysis.get("artifact_id") or ""),
                "trace_name": _text(analysis.get("trace_name") or analysis.get("artifact_id"), maximum=512) or "runtime-trace",
                "trace_window_start_ns": trace_start_ns,
                "trace_window_end_ns": trace_end_ns,
                "trace_window_duration_ms": round((trace_end_ns - trace_start_ns) / 1_000_000.0, 6) if trace_start_ns is not None and trace_end_ns is not None else None,
                "total_event_count": sum(int(row["event_count"]) for row in stage_rows),
                "total_event_duration_ms": round(sum(_number(row["total_event_duration_ms"]) for row in stage_rows), 6),
                "top_event_limit_per_stage": bounded_limit,
                "ranking_method": (
                    "parent_span_minus_direct_children_estimate"
                    if raw_analysis_available
                    else "report_supplied_stage_top_events"
                    if report_stage_metrics_available
                    else "aligned_boundary_or_supporting_evidence"
                ),
                "stage_semantics": "阶段时长为原始 Trace 的已校准 start/end 边界，或外部报告明确给出的 stage time_range 形成的墙钟时间包络；不等同于 CPU self-time。",
                "event_semantics": "事件累计时长可能重叠；原始 Trace 热点按父跨度减直接子跨度估算排序，该估算不等同于采样证明的 CPU self-time。",
            },
            "stages": stage_rows,
            "nodes": node_rows,
        }

    def apply_alignment(
        self,
        *,
        source_connector_id: str,
        analysis_artifact_id: str,
        event_catalog_artifact_id: str,
        scenario_artifact_id: str,
        flow_id: str,
        mermaid: str,
        stage_matches: list[dict[str, Any]],
        change_summary: str,
        limitations: list[str],
        node_matches: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        analysis = self.read_trace(analysis_artifact_id)
        catalog = self.artifact_store.read(RUNTIME_TRACE_CATALOG_ARTIFACT_TYPE, event_catalog_artifact_id)
        parent = self.application_flows.read_scenario(scenario_artifact_id)
        if (
            parent.get("source_connector_id") != source_connector_id
            or parent.get("flow_id") != flow_id
            or catalog.get("analysis_artifact_id") != analysis_artifact_id
            or catalog.get("scenario_artifact_id") != scenario_artifact_id
        ):
            raise PlatformError("APPLICATION_FLOW_RUNTIME_ALIGNMENT_MISMATCH", "Trace、事件目录与流程场景的血缘不一致。", status_code=409)
        normalized_mermaid, _ = self.application_flows.normalize_mermaid_authoring(
            mermaid,
            scenario_id=flow_id,
        )
        proposed = self.application_flows.scenario_from_mermaid(
            normalized_mermaid,
            default_description=str(parent.get("flow_name") or flow_id),
        )
        discovery_artifact = self.application_flows.read_discovery(
            str(parent.get("discovery_artifact_id") or "")
        )
        discovery = _record(discovery_artifact.get("discovery"))
        selected_flow = next(
            (
                item for item in discovery.get("flows", [])
                if isinstance(item, dict) and item.get("id") == flow_id
            ),
            None,
        )
        if selected_flow is None:
            raise PlatformError(
                "APPLICATION_FLOW_CANDIDATE_NOT_FOUND",
                "运行时校准所引用的流程已从 discovery 中移除。",
                status_code=404,
            )
        hierarchy = self.application_flows.stage_hierarchy_from_mermaid(
            normalized_mermaid,
            scenario=proposed,
            source_connector_id=source_connector_id,
            scope=str(discovery.get("scope") or ""),
            selected_flow=selected_flow,
            source_scopes=[
                item for item in parent.get("source_scopes", []) if isinstance(item, dict)
            ],
        )
        proposed = self.application_flows.project_hierarchy_nodes(proposed, hierarchy)
        proposed = self.application_flows.validate_scenario(proposed, normalized_mermaid)
        stages = [item for item in proposed.get("stages", []) if isinstance(item, dict)]
        stage_by_id = {str(item.get("id") or ""): item for item in stages}
        flow_nodes = [item for item in proposed.get("nodes", []) if isinstance(item, dict)]
        node_by_id = {str(item.get("id") or ""): item for item in flow_nodes}
        if not isinstance(stage_matches, list) or len(stage_matches) != len(stages):
            raise PlatformError("APPLICATION_FLOW_RUNTIME_ALIGNMENT_INVALID", "每个修订后 stage 必须提交一条运行时匹配记录。", status_code=422)
        events = [item for item in catalog.get("events", []) if isinstance(item, dict)]
        event_by_id = {str(item.get("event_id") or ""): item for item in events}
        observed_event_names = {
            str(item.get("event_name") or "")
            for item in events
            if str(item.get("event_name") or "")
        }
        for stage in stages:
            stage_id = str(stage.get("id") or "")
            for boundary in ("start", "end"):
                locator = _record(stage.get(boundary))
                if locator.get("type") != "trace_point":
                    continue
                event_name = str(locator.get("name") or "")
                if event_name not in observed_event_names or locator.get("name_match") != "exact":
                    raise PlatformError(
                        "APPLICATION_FLOW_RUNTIME_ALIGNMENT_INVALID",
                        "运行时校准后的 Mermaid 只能以 exact 方式引用事件目录中的真实 Trace event name。",
                        status_code=422,
                        details={
                            "stage_id": stage_id,
                            "boundary": boundary,
                            "event_name": event_name,
                            "name_match": locator.get("name_match"),
                        },
                    )
        normalized_matches: list[dict[str, Any]] = []
        seen_stages: set[str] = set()
        matched_count = 0
        for index, raw_match in enumerate(stage_matches):
            match = _record(raw_match)
            stage_id = _text(match.get("stage_id"), maximum=80)
            if stage_id not in stage_by_id or stage_id in seen_stages:
                raise PlatformError("APPLICATION_FLOW_RUNTIME_ALIGNMENT_INVALID", "运行时匹配引用了未知或重复 stage。", status_code=422, details={"stage_id": stage_id})
            seen_stages.add(stage_id)
            status = _text(match.get("match_status"), maximum=20)
            confidence = _text(match.get("confidence"), maximum=20)
            if status not in _MATCH_STATUSES or confidence not in _CONFIDENCE_LEVELS:
                raise PlatformError("APPLICATION_FLOW_RUNTIME_ALIGNMENT_INVALID", "match_status 或 confidence 非法。", status_code=422, details={"stage_id": stage_id})
            start_id = _text(match.get("start_event_id"), maximum=80)
            end_id = _text(match.get("end_event_id"), maximum=80)
            supporting_ids = [str(item) for item in match.get("supporting_event_ids", []) if isinstance(item, str)] if isinstance(match.get("supporting_event_ids"), list) else []
            referenced_ids = [item for item in [start_id, end_id, *supporting_ids] if item]
            if len(referenced_ids) > 18 or any(item not in event_by_id for item in referenced_ids):
                raise PlatformError("APPLICATION_FLOW_RUNTIME_ALIGNMENT_INVALID", "运行时匹配引用了事件目录之外的 event_id。", status_code=422, details={"stage_id": stage_id})
            if status == "matched" and (not start_id or not end_id):
                raise PlatformError("APPLICATION_FLOW_RUNTIME_ALIGNMENT_INVALID", "matched stage 必须同时提供 start_event_id 与 end_event_id。", status_code=422, details={"stage_id": stage_id})
            if status == "partial" and not referenced_ids:
                raise PlatformError("APPLICATION_FLOW_RUNTIME_ALIGNMENT_INVALID", "partial stage 至少引用一个真实 Trace event。", status_code=422, details={"stage_id": stage_id})
            if status == "unmatched" and referenced_ids:
                raise PlatformError("APPLICATION_FLOW_RUNTIME_ALIGNMENT_INVALID", "unmatched stage 不能引用 Trace event。", status_code=422, details={"stage_id": stage_id})
            stage = stage_by_id[stage_id]
            for boundary, event_id in (("start", start_id), ("end", end_id)):
                if not event_id:
                    continue
                locator = _record(stage.get(boundary))
                expected_name = str(event_by_id[event_id].get("event_name") or "")
                if (
                    locator.get("type") != "trace_point"
                    or locator.get("name") != expected_name
                    or locator.get("name_match") != "exact"
                ):
                    raise PlatformError(
                        "APPLICATION_FLOW_RUNTIME_ALIGNMENT_INVALID",
                        "Mermaid stage 边界必须以 exact 方式使用所选真实 Trace event 的精确名称。",
                        status_code=422,
                        details={"stage_id": stage_id, "boundary": boundary, "expected_event_name": expected_name},
                    )
            if status == "matched":
                matched_count += 1
            normalized_matches.append({
                "stage_id": stage_id,
                "stage_name": stage.get("name"),
                "match_status": status,
                "confidence": confidence,
                "start_event": event_by_id.get(start_id) if start_id else None,
                "end_event": event_by_id.get(end_id) if end_id else None,
                "supporting_events": [event_by_id[item] for item in supporting_ids],
                "rationale": _text(match.get("rationale"), maximum=2000),
            })
        if seen_stages != set(stage_by_id):
            raise PlatformError("APPLICATION_FLOW_RUNTIME_ALIGNMENT_INVALID", "运行时匹配没有覆盖全部修订后 stage。", status_code=422)
        normalized_node_matches: list[dict[str, Any]] = []
        seen_nodes: set[str] = set()
        for raw_match in node_matches or []:
            match = _record(raw_match)
            node_id = _text(match.get("node_id"), maximum=80)
            node = _record(node_by_id.get(node_id))
            if not node or node.get("type") == "stage" or node_id in seen_nodes:
                raise PlatformError(
                    "APPLICATION_FLOW_RUNTIME_ALIGNMENT_INVALID",
                    "细粒度运行时匹配引用唯一的 action、phase 或 activity 节点。",
                    status_code=422,
                    details={"node_id": node_id},
                )
            seen_nodes.add(node_id)
            status = _text(match.get("match_status"), maximum=20)
            confidence = _text(match.get("confidence"), maximum=20)
            if status not in _MATCH_STATUSES or confidence not in _CONFIDENCE_LEVELS:
                raise PlatformError(
                    "APPLICATION_FLOW_RUNTIME_ALIGNMENT_INVALID",
                    "节点 match_status 与 confidence 使用标准枚举值。",
                    status_code=422,
                    details={"node_id": node_id},
                )
            start_id = _text(match.get("start_event_id"), maximum=80)
            end_id = _text(match.get("end_event_id"), maximum=80)
            supporting_ids = [
                str(item)
                for item in match.get("supporting_event_ids", [])
                if isinstance(item, str)
            ] if isinstance(match.get("supporting_event_ids"), list) else []
            referenced_ids = [item for item in [start_id, end_id, *supporting_ids] if item]
            if len(referenced_ids) > 18 or any(item not in event_by_id for item in referenced_ids):
                raise PlatformError(
                    "APPLICATION_FLOW_RUNTIME_ALIGNMENT_INVALID",
                    "节点运行时匹配引用当前事件目录中的 event_id。",
                    status_code=422,
                    details={"node_id": node_id},
                )
            if status == "matched" and (not start_id or not end_id):
                raise PlatformError(
                    "APPLICATION_FLOW_RUNTIME_ALIGNMENT_INVALID",
                    "matched 节点同时提供 start_event_id 与 end_event_id。",
                    status_code=422,
                    details={"node_id": node_id},
                )
            if status == "partial" and not referenced_ids:
                raise PlatformError(
                    "APPLICATION_FLOW_RUNTIME_ALIGNMENT_INVALID",
                    "partial 节点引用至少一个真实 Trace event。",
                    status_code=422,
                    details={"node_id": node_id},
                )
            if status == "unmatched" and referenced_ids:
                raise PlatformError(
                    "APPLICATION_FLOW_RUNTIME_ALIGNMENT_INVALID",
                    "unmatched 节点以空事件引用表达待校准状态。",
                    status_code=422,
                    details={"node_id": node_id},
                )
            normalized_node_matches.append({
                "node_id": node_id,
                "node_name": node.get("name"),
                "node_type": node.get("type"),
                "parent_id": node.get("parent_id"),
                "match_status": status,
                "confidence": confidence,
                "start_event": event_by_id.get(start_id) if start_id else None,
                "end_event": event_by_id.get(end_id) if end_id else None,
                "supporting_events": [event_by_id[item] for item in supporting_ids],
                "rationale": _text(match.get("rationale"), maximum=2000),
            })
        old_stages = [item for item in _record(parent.get("scenario_json")).get("stages", []) if isinstance(item, dict)]
        old_ids = [str(item.get("id") or "") for item in old_stages]
        new_ids = [str(item.get("id") or "") for item in stages]
        partial_count = sum(item["match_status"] == "partial" for item in normalized_matches)
        unmatched_count = sum(item["match_status"] == "unmatched" for item in normalized_matches)
        alignment = {
            "schema_version": "application_flow_runtime_trace_alignment/v1",
            "trace_analysis_artifact_id": analysis_artifact_id,
            "event_catalog_artifact_id": event_catalog_artifact_id,
            "source_scenario_artifact_id": scenario_artifact_id,
            "trace": self._trace_summary(analysis),
            "stage_matches": normalized_matches,
            "node_matches": normalized_node_matches,
            "graph_revision": {
                "topology_changed": old_ids != new_ids,
                "previous_stage_ids": old_ids,
                "revised_stage_ids": new_ids,
                "change_summary": _text(change_summary, maximum=4000),
            },
            "quality": {
                "status": "complete" if matched_count == len(stages) else "partial" if matched_count or partial_count else "unmatched",
                "stage_count": len(stages),
                "matched_stage_count": matched_count,
                "partial_stage_count": partial_count,
                "unmatched_stage_count": unmatched_count,
                "matched_node_count": sum(
                    item["match_status"] == "matched" for item in normalized_node_matches
                ),
                "partial_node_count": sum(
                    item["match_status"] == "partial" for item in normalized_node_matches
                ),
                "unmatched_node_count": sum(
                    item["match_status"] == "unmatched" for item in normalized_node_matches
                ),
                "evidence_scope": "observed_runtime_trace_names",
            },
            "limitations": [_text(item, maximum=2000) for item in limitations if _text(item, maximum=2000)][:64],
        }
        proposed = self.application_flows.project_hierarchy_nodes(
            proposed,
            hierarchy,
            alignment,
        )
        stage_analysis = self._build_stage_analysis(
            analysis=analysis,
            scenario=proposed,
            stage_matches=normalized_matches,
            node_matches=normalized_node_matches,
        )
        result = self.application_flows.compile_mermaid(
            source_connector_id,
            discovery_artifact_id=str(parent.get("discovery_artifact_id") or ""),
            flow_id=flow_id,
            mermaid=normalized_mermaid,
            additional_input_artifacts=[scenario_artifact_id, analysis_artifact_id, event_catalog_artifact_id],
            generation_method="runtime_trace_calibrated_mermaid/v1",
            parent_scenario_artifact_id=scenario_artifact_id,
            revision_request=_text(change_summary, maximum=4000) or "使用真实 Trace 事件校准阶段边界与阶段图。",
            runtime_trace_alignment=alignment,
            stage_analysis=stage_analysis,
        )
        result["runtime_trace_alignment"] = alignment
        result["stage_analysis"] = stage_analysis
        result["summary_zh"] = (
            f"已用真实 Trace 校准 {len(stages)} 个阶段：{matched_count} 个完整匹配、"
            f"{partial_count} 个部分匹配、{unmatched_count} 个未匹配；已生成阶段负载分析侧车，"
            "正式流程 JSON 仍使用 application-flow-scenario/v2。"
        )
        return result
