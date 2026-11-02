"""Desktop-only workspace APIs that do not require FastAPI.

The Electron client keeps one framed Python RPC worker alive for the selected
SSH target and server profile, then submits allow-listed logical requests over
that channel.  This module deliberately reuses the platform's durable managers
and JSON stores while keeping HTTP, device access and model credentials outside
the worker.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import select
import subprocess
import time
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

from backend.core import ArtifactStore, JsonRegistry, PlatformError, SourceConnectorManager, utc_now
from backend.dpvs_analysis import dispatch_dpvs_analysis


_GRAPH_ACTION = re.compile(r"^/v1/code-graphs/([^/]+)/(confirm|cancel|semantic-analysis)$")
_GRAPH_ITEM = re.compile(r"^/v1/code-graphs/([^/]+)$")
_REPORT_ITEM = re.compile(r"^/v1/code-knowledge-reports/([^/]+)$")
_REPORT_MARKDOWN = re.compile(r"^/v1/code-knowledge-reports/([^/]+)/markdown$")
_CONNECTOR_REFRESH = re.compile(r"^/v1/source-connectors/([^/]+)/refresh$")
_FLOW_DISCOVERY = re.compile(r"^/v1/application-flows/discoveries/([^/]+)$")
_FLOW_SCENARIO = re.compile(r"^/v1/application-flows/scenarios/([^/]+)$")
_FLOW_TRACE = re.compile(r"^/v1/application-flows/runtime-traces/([^/]+)$")
_FLOW_DELETE = re.compile(
    r"^/v1/application-flows/(application_flow_discovery|application_flow_scenario|discoveries|scenarios)/([^/]+)$"
)
_FLOW_SCENARIO_EXPORT = re.compile(r"^/v1/application-flows/scenarios/([^/]+)/(json|mermaid|stage-analysis)$")
_SKILL_DETAIL = re.compile(r"^/v1/user-skill-library/skills/([^/]+)$")
_SKILL_ACTIVATE = re.compile(r"^/v1/user-skill-library/skills/([^/]+)/activation$")
_EXPERIENCE_CASE = re.compile(r"^/v1/experience-evolution/cases/([^/]+)$")
_KB_RECORD = re.compile(r"^/v1/(optimization|hardware)-kb/records/([^/]+)/([^/]+)$")
_ACCELERATOR_API_RECORDS = re.compile(r"^/v1/hardware-kb/accelerator-api/([^/]+)/records$")
_ACCELERATOR_API_RECORD = re.compile(r"^/v1/hardware-kb/accelerator-api/([^/]+)/records/([^/]+)$")
_CUSTOM_API_KB = re.compile(r"^/v1/custom-api-kbs/([^/]+)$")
_CUSTOM_API_RECORD = re.compile(r"^/v1/custom-api-kbs/([^/]+)/records/([^/]+)$")
_OPPORTUNITY_TRACE = re.compile(r"^/v1/desktop-opportunity/traces/([^/]+)$")

_ACCELERATOR_API_FILES = {
    "gpu": "gpu_api_knowledge_base.json",
    "dsp": "dsp_api_knowledge_base.json",
    "media": "media_accelerator_api_knowledge_base.json",
}


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _bounded_int(value: Any, default: int, maximum: int = 500) -> int:
    try:
        return max(1, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def _query_value(query: dict[str, list[str]], key: str, default: str = "") -> str:
    values = query.get(key)
    return str(values[-1]) if values else default


def _project_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _workspace_root() -> Path:
    for name in ("HETERO_REPO_ROOT", "HETERO_DEFAULT_SOURCE_ROOT"):
        value = os.environ.get(name, "").strip()
        if value:
            return Path(value).expanduser().resolve()
    return _project_root()


def _services(state_root: Path) -> tuple[ArtifactStore, SourceConnectorManager]:
    store = ArtifactStore(state_root, project_id="local-default")
    connectors = SourceConnectorManager(state_root, store, workspace_root=_workspace_root())
    return store, connectors


def _code_graphs(state_root: Path, store: ArtifactStore, connectors: SourceConnectorManager):
    from backend.code_graph import CodeGraphManager
    return CodeGraphManager(state_root, connectors, store, recover_interrupted=False)


def _application_flow_services(
    state_root: Path,
    store: ArtifactStore,
    connectors: SourceConnectorManager,
):
    """Build the transport-neutral flow services used by Desktop Agent tools.

    The Web Agent used to reach these services through a live FastAPI process.
    Desktop owns a persistent stdio worker instead, so the same managers can be
    invoked without requiring an HTTP server to be running on the SSH target.
    """

    try:
        from backend.application_flow import ApplicationFlowManager
        from backend.application_flow_trace import ApplicationFlowRuntimeTraceManager
    except ModuleNotFoundError as exc:
        if exc.name == "jsonschema" or str(exc.name or "").startswith("jsonschema."):
            raise PlatformError(
                "DESKTOP_ANALYSIS_RUNTIME_UNAVAILABLE",
                "应用流程分析环境缺少受管 JSON Schema 校验器。",
                status_code=503,
                details={"missing_module": "jsonschema"},
            ) from exc
        raise

    references = (
        _project_root()
        / "agent-resources"
        / "skills"
        / "application-execution-flow-reconstruction"
        / "references"
    )
    flows = ApplicationFlowManager(
        connectors,
        store,
        scenario_schema_path=references / "application-flow-scenario-v2.schema.json",
        discovery_schema_path=references / "application-flow-discovery-v1.schema.json",
        stage_analysis_schema_path=references / "application-flow-stage-analysis-v1.schema.json",
    )
    return flows, ApplicationFlowRuntimeTraceManager(state_root, store, flows)


def _short_text(value: Any, maximum: int = 4096) -> str:
    return str(value).strip()[:maximum] if isinstance(value, str) else ""


def _desktop_application_flow_action(
    state_root: Path,
    store: ArtifactStore,
    connectors: SourceConnectorManager,
    body: dict[str, Any],
) -> dict[str, Any]:
    """Execute one named, allow-listed application-flow domain operation."""

    action_id = _short_text(body.get("action_id"), 120)
    input_value = _record(body.get("input"))
    context = _record(body.get("context"))
    allowed = {
        "application_flow.list_assets",
        "application_flow.inspect_repository",
        "application_flow.search_source",
        "application_flow.read_source",
        "application_flow.inspect_trace_instrumentation",
        "application_flow.inspect_runtime_trace",
        "application_flow.read_asset",
        "application_flow.publish_candidates",
        "application_flow.compile_mermaid",
        "application_flow.enrich_trace_anchors",
        "application_flow.apply_runtime_trace_alignment",
    }
    if action_id not in allowed:
        raise PlatformError(
            "DESKTOP_FLOW_ACTION_NOT_ALLOWED",
            "Desktop 未开放该应用流程操作。",
            status_code=404,
            details={"action_id": action_id},
        )

    flows, runtime_traces = _application_flow_services(state_root, store, connectors)
    connector_id = _short_text(
        context.get("source_connector_id") or input_value.get("source_connector_id"),
        160,
    )
    analysis_id = _short_text(
        context.get("application_flow_analysis_id")
        or input_value.get("application_flow_analysis_id"),
        160,
    )
    if not analysis_id:
        raise PlatformError(
            "DESKTOP_FLOW_ANALYSIS_CONTEXT_REQUIRED",
            "Desktop Code Agent 操作缺少受信的应用流程分析上下文。",
            status_code=409,
        )

    def require_scope(artifact_type: str, artifact_id: str) -> None:
        if analysis_id:
            flows.require_analysis_scope(artifact_type, artifact_id, analysis_id)

    if action_id == "application_flow.list_assets":
        return flows.list_assets(
            source_connector_id=connector_id,
            application_flow_analysis_id=analysis_id,
            limit=_bounded_int(input_value.get("limit"), 100),
        )
    if action_id == "application_flow.inspect_repository":
        return flows.inspect_repository(connector_id, subpath=_short_text(input_value.get("subpath"), 2048))
    if action_id == "application_flow.search_source":
        return flows.search_source(
            connector_id,
            query=_short_text(input_value.get("query"), 240),
            subpath=_short_text(input_value.get("subpath"), 2048),
            max_results=_bounded_int(input_value.get("max_results"), 30, 100),
        )
    if action_id == "application_flow.read_source":
        return flows.read_source(
            connector_id,
            path=_short_text(input_value.get("path"), 2048),
            line_start=max(1, int(input_value.get("line_start") or 1)),
            line_end=max(1, int(input_value.get("line_end") or 240)),
        )
    if action_id == "application_flow.inspect_trace_instrumentation":
        return flows.inspect_trace_instrumentation(
            connector_id,
            path=_short_text(input_value.get("path"), 2048),
            line=max(1, int(input_value.get("line") or 1)),
            symbol=_short_text(input_value.get("symbol"), 1000),
            radius=max(40, min(int(input_value.get("radius") or 320), 1200)),
            max_results=_bounded_int(input_value.get("max_results"), 40, 100),
        )
    if action_id == "application_flow.read_asset":
        artifact_type = _short_text(input_value.get("artifact_type"), 80)
        artifact_id = _short_text(input_value.get("artifact_id"), 80)
        if artifact_type not in {"application_flow_discovery", "application_flow_scenario"}:
            raise PlatformError("APPLICATION_FLOW_ASSET_TYPE_INVALID", "流程工件类型非法。", status_code=422)
        require_scope(artifact_type, artifact_id)
        artifact = (
            flows.read_discovery(artifact_id)
            if artifact_type == "application_flow_discovery"
            else flows.read_scenario(artifact_id)
        )
        return {"status": "succeeded", "artifact": artifact}
    if action_id == "application_flow.publish_candidates":
        graph_artifact_id = ""
        graph_id = _short_text(input_value.get("code_graph_id"), 80)
        if graph_id:
            graph = _code_graphs(state_root, store, connectors).validate_ready_graph(
                graph_id,
                source_connector_id=connector_id,
                scope=_short_text(input_value.get("subpath"), 2048),
            )
            graph_artifact_id = _short_text(graph.get("artifact_id"), 80)
        normalized_scopes: list[dict[str, Any]] = []
        for raw_scope in input_value.get("source_scopes", []) if isinstance(input_value.get("source_scopes"), list) else []:
            scope = _record(raw_scope)
            scope_connector_id = _short_text(scope.get("source_connector_id"), 160)
            if not scope_connector_id:
                continue
            scope_graph_artifact_id = ""
            scope_graph_id = _short_text(scope.get("code_graph_id"), 80)
            scope_subpath = _short_text(scope.get("subpath"), 2048)
            if scope_graph_id:
                graph = _code_graphs(state_root, store, connectors).validate_ready_graph(
                    scope_graph_id,
                    source_connector_id=scope_connector_id,
                    source_snapshot_id=_short_text(scope.get("source_snapshot_id"), 256) or None,
                    scope=scope_subpath,
                )
                scope_graph_artifact_id = _short_text(graph.get("artifact_id"), 80)
            normalized_scopes.append({
                "source_connector_id": scope_connector_id,
                "source_snapshot_id": _short_text(scope.get("source_snapshot_id"), 256),
                "subpath": scope_subpath,
                "code_graph_artifact_id": scope_graph_artifact_id or None,
            })
        return flows.save_candidate_catalog(
            connector_id,
            summary_zh=_short_text(input_value.get("summary_zh"), 4000),
            candidates=[_record(item) for item in input_value.get("candidates", []) if isinstance(item, dict)],
            subpath=_short_text(input_value.get("subpath"), 2048),
            unknowns=[str(item) for item in input_value.get("unknowns", []) if isinstance(item, str)],
            analysis_goal=_short_text(input_value.get("analysis_goal"), 4000),
            code_graph_artifact_id=graph_artifact_id,
            source_scopes=normalized_scopes or None,
            parent_discovery_artifact_id=_short_text(input_value.get("parent_discovery_artifact_id"), 80),
            refinement_request=_short_text(input_value.get("refinement_request"), 4000),
            application_flow_analysis_id=analysis_id,
            trace_analysis_artifact_id=_short_text(
                context.get("trace_analysis_artifact_id") or input_value.get("trace_analysis_artifact_id"),
                80,
            ),
        )
    if action_id == "application_flow.compile_mermaid":
        discovery_id = _short_text(input_value.get("discovery_artifact_id"), 80)
        require_scope("application_flow_discovery", discovery_id)
        return flows.compile_mermaid(
            connector_id,
            discovery_artifact_id=discovery_id,
            flow_id=_short_text(input_value.get("flow_id"), 80),
            mermaid=_short_text(input_value.get("mermaid"), 256 * 1024),
            parent_scenario_artifact_id=_short_text(input_value.get("parent_scenario_artifact_id"), 80),
            revision_request=_short_text(input_value.get("revision_request"), 4000),
        )
    if action_id == "application_flow.enrich_trace_anchors":
        discovery_id = _short_text(input_value.get("discovery_artifact_id"), 80)
        scenario_id = _short_text(input_value.get("scenario_artifact_id"), 80)
        require_scope("application_flow_discovery", discovery_id)
        require_scope("application_flow_scenario", scenario_id)
        return flows.enrich_trace_anchors(
            connector_id,
            discovery_artifact_id=discovery_id,
            scenario_artifact_id=scenario_id,
            flow_id=_short_text(input_value.get("flow_id"), 80),
            stage_results=[_record(item) for item in input_value.get("stage_results", []) if isinstance(item, dict)],
        )
    if action_id == "application_flow.inspect_runtime_trace":
        scenario_id = _short_text(input_value.get("scenario_artifact_id"), 80)
        require_scope("application_flow_scenario", scenario_id)
        return runtime_traces.inspect_trace(
            analysis_artifact_id=_short_text(input_value.get("analysis_artifact_id"), 80),
            scenario_artifact_id=scenario_id,
            query=_short_text(input_value.get("query"), 1000),
            global_limit=max(20, min(int(input_value.get("global_limit") or 120), 300)),
            per_stage_limit=max(1, min(int(input_value.get("per_stage_limit") or 12), 30)),
        )

    scenario_id = _short_text(input_value.get("scenario_artifact_id"), 80)
    require_scope("application_flow_scenario", scenario_id)
    return runtime_traces.apply_alignment(
        source_connector_id=connector_id,
        analysis_artifact_id=_short_text(input_value.get("analysis_artifact_id"), 80),
        event_catalog_artifact_id=_short_text(input_value.get("event_catalog_artifact_id"), 80),
        scenario_artifact_id=scenario_id,
        flow_id=_short_text(input_value.get("flow_id"), 80),
        mermaid=_short_text(input_value.get("mermaid"), 256 * 1024),
        stage_matches=[_record(item) for item in input_value.get("stage_matches", []) if isinstance(item, dict)],
        node_matches=[_record(item) for item in input_value.get("node_matches", []) if isinstance(item, dict)],
        change_summary=_short_text(input_value.get("change_summary"), 4000),
        limitations=[str(item) for item in input_value.get("limitations", []) if isinstance(item, str)],
    )


def _wait_for_code_graph_job(
    manager: Any,
    result: dict[str, Any],
    *,
    estimate: bool = False,
    build: bool = False,
) -> dict[str, Any]:
    """Join the request-scoped graph worker and return its persisted state.

    A Web process keeps the manager's ThreadPool alive after returning HTTP
    202.  The persistent Desktop RPC process creates a fresh manager for each
    logical request; returning the initial ``queued`` object would leave no
    request-owned job handle for later polling.  Joining here makes that wait
    explicit and returns the terminal/confirmation state that was persisted.
    """

    if estimate:
        manager._estimate_executor.shutdown(wait=True)  # noqa: SLF001 - Desktop adapter owns manager lifetime.
        # An estimate may have been upgraded to auto-start by a concurrent
        # build request before inventory completed.
        manager._executor.shutdown(wait=True)  # noqa: SLF001 - Desktop adapter owns manager lifetime.
    elif build:
        manager._executor.shutdown(wait=True)  # noqa: SLF001 - Desktop adapter owns manager lifetime.
    graph = _record(result.get("graph"))
    graph_id = str(graph.get("graph_id") or "")
    return {**result, **({"graph": manager.get_graph(graph_id)} if graph_id else {})}


def _knowledge_payload(kind: str) -> dict[str, Any]:
    path = _project_root() / "data" / "knowledge-bases" / kind / f"{kind}_knowledge_base.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlatformError("KB_VERSION_INCOMPATIBLE", f"{kind} knowledge base is unavailable.", status_code=503) from exc
    expected = f"{kind}_knowledge_base"
    if not isinstance(value, dict) or value.get("artifact_type") != expected or not isinstance(value.get("records"), list):
        raise PlatformError("KB_VERSION_INCOMPATIBLE", f"{kind} knowledge base contract is invalid.", status_code=503)
    return value


def _knowledge_records(kind: str) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    payload = _knowledge_payload(kind)
    hardware = kind == "hardware"
    records: list[dict[str, Any]] = []
    for raw in payload["records"]:
        if not isinstance(raw, dict):
            continue
        record_id = str(raw.get("record_id") or raw.get("case_id") or raw.get("hardware_id") or "")
        if not record_id:
            continue
        provenance = _record(raw.get("provenance"))
        records.append({
            "record_type": str(raw.get("record_type") or ("hardware_capability" if hardware else "optimization_record")),
            "record_id": record_id,
            "hardware_id": raw.get("hardware_id") if hardware else None,
            "name": raw.get("name") or raw.get("algorithm") or raw.get("hardware_id") or record_id,
            "algorithm": raw.get("algorithm"),
            "target_unit": raw.get("hardware_id") if hardware else raw.get("target_unit"),
            "confidence": provenance.get("confidence", raw.get("strict_status", "unverified")),
            "provenance": provenance,
            "record": raw.get("record") if isinstance(raw.get("record"), dict) else raw,
        })
    return payload, records


def _hardware_api_payload(unit: str) -> dict[str, Any]:
    """Read the same immutable API catalogs exposed by the Web backend."""

    filename = "npu_api_knowledge_base.json" if unit == "npu" else _ACCELERATOR_API_FILES.get(unit)
    if filename is None:
        raise PlatformError("ACCELERATOR_API_UNIT_NOT_FOUND", f"Unsupported accelerator API unit: {unit}.", status_code=404)
    path = _project_root() / "data" / "knowledge-bases" / "hardware" / filename
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PlatformError("KB_VERSION_INCOMPATIBLE", f"{unit.upper()} API knowledge base is unavailable.", status_code=503) from exc
    expected = "npu_api_knowledge_base" if unit == "npu" else "accelerator_api_knowledge_base"
    if not isinstance(payload, dict) or payload.get("artifact_type") != expected or not isinstance(payload.get("records"), list):
        raise PlatformError("KB_VERSION_INCOMPATIBLE", f"{unit.upper()} API knowledge base contract is invalid.", status_code=503)
    if unit != "npu" and payload.get("accelerator_unit") != unit:
        raise PlatformError("KB_VERSION_INCOMPATIBLE", f"{unit.upper()} API knowledge base unit is invalid.", status_code=503)
    return payload


def _hardware_api_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "kb_id": payload.get("kb_id"),
        "version": payload.get("kb_version"),
        "source_revision": payload.get("source_revision"),
        "content_sha256": payload.get("content_sha256"),
        "summary": payload.get("summary", {}),
    }


def _api_catalog_record(raw: dict[str, Any], unit: str) -> dict[str, Any]:
    family = _record(raw.get("semantic_family"))
    integration = _record(raw.get("source_integration_evidence"))
    boundary = _record(raw.get("capability_boundary"))
    source = _record(raw.get("source_contract"))
    return {
        "record_id": raw.get("record_id"),
        "record_type": raw.get("record_type"),
        "accelerator_unit": None if unit == "npu" else unit,
        "symbol": raw.get("symbol"),
        "name": raw.get("name_zh") or raw.get("symbol"),
        "one_line_abstract_zh": raw.get("one_line_abstract_zh"),
        "detailed_description_zh": raw.get("detailed_description_zh"),
        "operation_id": raw.get("operation_id"),
        "semantic_family": {key: family.get(key) for key in ("id", "label_zh") if family.get(key) is not None},
        "api_layer": raw.get("api_layer"),
        "api_layer_label_zh": raw.get("api_layer_label_zh"),
        "workflow_role": raw.get("workflow_role"),
        "strict_case_ids": [str(value) for value in integration.get("strict_case_ids", []) if isinstance(value, (str, int))][:8],
        "device_availability": boundary.get("target_device_availability", "evidence_required"),
        "exposure": source.get("exposure"),
        "source_contract": {
            key: source.get(key)
            for key in ("source_file", "source_line", "system_capability", "library", "since", "version")
            if source.get(key) is not None
        },
    }


def _api_catalog(unit: str, query: str = "") -> dict[str, Any]:
    payload = _hardware_api_payload(unit)
    normalized = query.casefold().strip()
    records: list[dict[str, Any]] = []
    for raw in payload["records"]:
        if not isinstance(raw, dict):
            continue
        haystack = json.dumps(raw, ensure_ascii=False, sort_keys=True).casefold()
        if normalized and normalized not in haystack:
            continue
        records.append(_api_catalog_record(raw, unit))
    records.sort(key=lambda item: (
        0 if item.get("record_type") == "npu_operation" else 1,
        int(item.get("operation_id") or 0),
        str(item.get("symbol") or ""),
    ))
    metadata_key = "npu_api_kb" if unit == "npu" else "accelerator_api_kb"
    return {
        metadata_key: _hardware_api_metadata(payload),
        **({"accelerator_unit": unit} if unit != "npu" else {}),
        "query": query,
        "record_count": len(records),
        "summary": payload.get("summary", {}),
        "records": records,
    }


def _api_record(unit: str, record_id: str) -> dict[str, Any]:
    payload = _hardware_api_payload(unit)
    item = next((raw for raw in payload["records"] if isinstance(raw, dict) and raw.get("record_id") == record_id), None)
    if item is None:
        raise PlatformError("KB_RECORD_NOT_FOUND", "Hardware API record does not exist.", status_code=404)
    metadata_key = "npu_api_kb" if unit == "npu" else "accelerator_api_kb"
    return {
        metadata_key: _hardware_api_metadata(payload),
        **({"accelerator_unit": unit} if unit != "npu" else {}),
        "record": item,
    }


def _custom_api_manager(state_root: Path, connectors: SourceConnectorManager):
    from backend.custom_api_kb import CustomApiKnowledgeBaseManager
    return CustomApiKnowledgeBaseManager(state_root, connectors)


def _search_knowledge(kind: str, body: dict[str, Any]) -> dict[str, Any]:
    payload, records = _knowledge_records(kind)
    query = str(body.get("query") or "").casefold().strip()
    include_unverified = bool(body.get("include_unverified"))
    trusted = {"confirmed", "source_confirmed", "source_confirmed_integration"}
    results: list[dict[str, Any]] = []
    for item in records:
        if not include_unverified and str(item.get("confidence")) not in trusted:
            continue
        haystack = json.dumps(item, ensure_ascii=False, sort_keys=True).casefold()
        if query and query not in haystack:
            # Multiple goal words should still retrieve bounded candidate evidence.
            terms = [term for term in re.findall(r"[\w\u4e00-\u9fff]{2,}", query) if len(term) > 1]
            hits = sum(term in haystack for term in terms[:12])
            if not hits:
                continue
            score = min(0.9, 0.3 + 0.08 * hits)
        else:
            score = 1.0 if not query else min(0.98, 0.45 + 0.05 * haystack.count(query))
        results.append({**item, "score": round(score, 3), "matched_fields": ["structured_record"] if query else []})
    results.sort(key=lambda item: (str(item.get("confidence")) in trusted, float(item.get("score") or 0)), reverse=True)
    return {
        "knowledge_base": kind,
        "kb_id": payload.get("kb_id"),
        "kb_version": payload.get("kb_version"),
        "kb_content_sha256": payload.get("content_sha256"),
        "kb_manifest_sha256": payload.get("content_sha256"),
        "summary": payload.get("summary", {}),
        "records": results[:100],
        "case_records": [],
    }


def _flow_preferences(store: ArtifactStore) -> JsonRegistry:
    return JsonRegistry(store.root / "application-flows" / "preferences.json", {
        "schema_version": "application_flow_preferences/v1",
        "confirm_flow_before_trace": True,
    })


def _flow_asset_registry(store: ArtifactStore) -> JsonRegistry:
    return JsonRegistry(
        store.root / "application-flows" / "registry.json",
        {"schema_version": "application_flow_asset_registry/v1", "deleted_assets": {}},
    )


def _flow_trace_registry(store: ArtifactStore) -> JsonRegistry:
    return JsonRegistry(
        store.root / "application-flows" / "runtime-traces.json",
        {
            "schema_version": "application_flow_runtime_trace_registry/v1",
            "deleted_traces": {},
            "analysis_bindings": {},
        },
    )


def _flow_analysis_id(item: dict[str, Any], artifact_type: str) -> str:
    explicit = str(item.get("application_flow_analysis_id") or "").strip()
    if explicit:
        return explicit
    if artifact_type == "application_flow_discovery":
        contextual = str(_record(item.get("analysis_context")).get("application_flow_analysis_id") or "").strip()
        return contextual or f"legacy-application-flow:{item.get('artifact_id')}"
    discovery_id = str(item.get("discovery_artifact_id") or item.get("artifact_id") or "")
    return f"legacy-application-flow:{discovery_id}"


def _deleted_flow_assets(store: ArtifactStore) -> dict[str, Any]:
    return _record(_flow_asset_registry(store).read().get("deleted_assets"))


def _ensure_flow_asset_active(store: ArtifactStore, artifact_type: str, artifact_id: str) -> dict[str, Any]:
    record = _record(_deleted_flow_assets(store).get(artifact_id))
    if record and record.get("artifact_type") == artifact_type:
        raise PlatformError("APPLICATION_FLOW_ASSET_NOT_FOUND", "应用流程工件不存在。", status_code=404)
    return store.read(artifact_type, artifact_id)


def _flow_catalog(store: ArtifactStore, query: dict[str, list[str]]) -> dict[str, Any]:
    discoveries = store.list_artifacts("application_flow_discovery", limit=500)
    scenarios = store.list_artifacts("application_flow_scenario", limit=500)
    tombstones = _deleted_flow_assets(store)
    connector_id = _query_value(query, "source_connector_id").strip()
    analysis_id = _query_value(query, "application_flow_analysis_id").strip()
    limit = _bounded_int(_query_value(query, "limit", "100"), 100, 200)

    def includes_connector(item: dict[str, Any]) -> bool:
        if not connector_id or item.get("source_connector_id") == connector_id:
            return True
        context_scopes = _record(item.get("analysis_context")).get("source_scopes")
        scenario_scopes = item.get("source_scopes")
        scopes = context_scopes if isinstance(context_scopes, list) else scenario_scopes
        return isinstance(scopes, list) and any(
            _record(scope).get("source_connector_id") == connector_id for scope in scopes
        )

    discoveries = [
        item for item in discoveries
        if item.get("artifact_id") not in tombstones
        and includes_connector(item)
        and (not analysis_id or _flow_analysis_id(item, "application_flow_discovery") == analysis_id)
    ][:limit]
    scenarios = [
        item for item in scenarios
        if item.get("artifact_id") not in tombstones
        and includes_connector(item)
        and (not analysis_id or _flow_analysis_id(item, "application_flow_scenario") == analysis_id)
    ][:limit]
    discovery_rows = []
    for item in discoveries:
        discovery = _record(item.get("discovery"))
        context = _record(item.get("analysis_context"))
        flows = discovery.get("flows") if isinstance(discovery.get("flows"), list) else []
        discovery_rows.append({
            "artifact_id": item.get("artifact_id"), "application_flow_analysis_id": _flow_analysis_id(item, "application_flow_discovery"),
            "created_at": item.get("created_at"), "source_connector_id": item.get("source_connector_id"),
            "source_snapshot_id": discovery.get("source_snapshot_id"), "scope": discovery.get("scope"),
            "source_scopes": context.get("source_scopes") or [], "analysis_goal": context.get("analysis_goal"),
            "code_graph_artifact_id": context.get("code_graph_artifact_id"), "trace_analysis_artifact_id": context.get("trace_analysis_artifact_id"),
            "parent_discovery_artifact_id": context.get("parent_discovery_artifact_id"),
            "refinement_request": context.get("refinement_request"),
            "interaction_iteration": context.get("interaction_iteration"),
            "flow_count": len(flows),
            "flows": [
                {key: flow.get(key) for key in ("id", "name", "order", "description", "confidence")}
                for flow in flows if isinstance(flow, dict)
            ],
            "quality": item.get("quality") or {},
        })
    scenario_rows = []
    for item in scenarios:
        scenario = _record(item.get("scenario_json"))
        stages = scenario.get("stages") if isinstance(scenario.get("stages"), list) else []
        alignment = _record(item.get("runtime_trace_alignment"))
        scenario_rows.append({
            "artifact_id": item.get("artifact_id"), "application_flow_analysis_id": _flow_analysis_id(item, "application_flow_scenario"),
            "created_at": item.get("created_at"), "source_connector_id": item.get("source_connector_id"),
            "source_scopes": item.get("source_scopes") or [], "discovery_artifact_id": item.get("discovery_artifact_id"),
            "flow_id": item.get("flow_id"), "flow_name": item.get("flow_name"), "scenario": scenario.get("scenario"),
            "stage_count": len(stages), "revision": item.get("revision", 1),
            "parent_scenario_artifact_id": item.get("parent_scenario_artifact_id"), "revision_request": item.get("revision_request"),
            "has_runtime_trace_alignment": bool(alignment), "has_stage_analysis": bool(item.get("stage_analysis")),
            "runtime_trace_analysis_artifact_id": alignment.get("trace_analysis_artifact_id"), "quality": item.get("quality") or {},
        })
    return {
        "schema_version": "application_flow_asset_catalog/v1", "schemas": {},
        "preferences": _flow_preferences_value(store),
        "discoveries": discovery_rows, "scenarios": scenario_rows,
    }


def _flow_preferences_value(store: ArtifactStore) -> dict[str, Any]:
    value = _flow_preferences(store).read()
    return {
        "schema_version": "application_flow_preferences/v1",
        "confirm_flow_before_trace": value.get("confirm_flow_before_trace")
        if isinstance(value.get("confirm_flow_before_trace"), bool) else True,
        "updated_at": str(value.get("updated_at") or "") or None,
    }


def _runtime_trace_catalog(store: ArtifactStore, query: dict[str, list[str]]) -> dict[str, Any]:
    connector_ids = {item for item in _query_value(query, "source_connector_ids").split(",") if item}
    limit = _bounded_int(_query_value(query, "limit", "100"), 100, 500)
    deleted = set(_record(_flow_trace_registry(store).read().get("deleted_traces")))
    rows = []
    for item in store.list_artifacts("trace_analysis_result", limit=500):
        if item.get("artifact_id") in deleted:
            continue
        if connector_ids and item.get("source_connector_id") not in connector_ids:
            continue
        if item.get("input_kind") not in {"raw_htrace", "external_trace_analysis_report"}:
            continue
        trace_import = _record(item.get("trace_import")); quality = _record(item.get("quality"))
        rows.append({
            "artifact_id": item.get("artifact_id"), "created_at": item.get("created_at"),
            "uploaded_at": trace_import.get("uploaded_at") or item.get("created_at"),
            "import_instance_id": trace_import.get("import_instance_id"), "trace_name": item.get("trace_name"),
            "input_kind": item.get("input_kind"), "analysis_profile": item.get("analysis_profile"),
            "source_connector_id": item.get("source_connector_id"),
            "stage_count": len(item.get("stage_catalog", [])) if isinstance(item.get("stage_catalog"), list) else 0,
            "hotspot_count": len(item.get("hotspot_candidates", [])) if isinstance(item.get("hotspot_candidates"), list) else 0,
            "quality_status": quality.get("status"),
        })
    rows.sort(
        key=lambda item: (str(item.get("uploaded_at") or item.get("created_at") or ""), str(item.get("artifact_id") or "")),
        reverse=True,
    )
    visible_rows = rows[:limit]
    return {
        "schema_version": "application_flow_runtime_trace_catalog/v1",
        "traces": visible_rows,
        "count": len(visible_rows),
        "total_count": len(rows),
    }


def _read_runtime_trace(store: ArtifactStore, artifact_id: str) -> dict[str, Any]:
    if artifact_id in _record(_flow_trace_registry(store).read().get("deleted_traces")):
        raise PlatformError(
            "APPLICATION_FLOW_TRACE_NOT_FOUND",
            "该真实 Trace 已从应用流程 Trace 列表删除。",
            status_code=404,
            details={"analysis_artifact_id": artifact_id},
        )
    artifact = store.read("trace_analysis_result", artifact_id)
    if artifact.get("input_kind") not in {"raw_htrace", "external_trace_analysis_report"}:
        raise PlatformError(
            "APPLICATION_FLOW_TRACE_KIND_UNSUPPORTED",
            "该工件不是应用流程分析支持的真实 Trace。",
            status_code=422,
            details={"analysis_artifact_id": artifact_id},
        )
    return artifact


def _runtime_trace_summary(artifact: dict[str, Any]) -> dict[str, Any]:
    quality = _record(artifact.get("quality")); trace_import = _record(artifact.get("trace_import"))
    return {
        "artifact_id": artifact.get("artifact_id"), "created_at": artifact.get("created_at"),
        "uploaded_at": trace_import.get("uploaded_at") or artifact.get("created_at"),
        "import_instance_id": trace_import.get("import_instance_id"), "trace_name": artifact.get("trace_name"),
        "input_kind": artifact.get("input_kind"), "analysis_profile": artifact.get("analysis_profile"),
        "source_connector_id": artifact.get("source_connector_id"),
        "stage_count": len(artifact.get("stage_catalog", [])) if isinstance(artifact.get("stage_catalog"), list) else 0,
        "hotspot_count": len(artifact.get("hotspot_candidates", [])) if isinstance(artifact.get("hotspot_candidates"), list) else 0,
        "quality_status": quality.get("status"),
    }


def _opportunity_trace_summary(artifact: dict[str, Any]) -> dict[str, Any]:
    """Return the Web reasoning selector contract without copying trace payloads."""

    binding = _record(artifact.get("source_binding"))
    quality = _record(artifact.get("quality"))
    return {
        **_runtime_trace_summary(artifact),
        "source_binding": {
            key: binding.get(key)
            for key in ("match_level", "resolved_snapshot_id", "snapshot_state")
            if binding.get(key) is not None
        },
        "cluster_count": len(artifact.get("clusters", [])) if isinstance(artifact.get("clusters"), list) else 0,
        "quality_reason": quality.get("reason"),
    }


def _opportunity_hotspot_id(candidate: dict[str, Any], rank: int) -> str:
    explicit = str(candidate.get("hotspot_id") or "").strip()
    if explicit:
        return explicit
    function = _record(candidate.get("function_ref"))
    stable = json.dumps({
        "symbol": function.get("symbol"),
        "module": function.get("module") or candidate.get("module"),
        "event": candidate.get("trace_event_name"),
        "stage": candidate.get("stage_context"),
    }, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    # Artifact IDs already bind the complete trace content.  This short digest
    # only gives legacy candidates without hotspot_id a stable address.
    return f"hotspot_{hashlib.sha256(stable.encode('utf-8')).hexdigest()[:20]}"


def _metric_number(*values: Any) -> float | int | None:
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value == value and value not in (float("inf"), float("-inf")):
            return value
    return None


def _opportunity_hotspot(candidate: dict[str, Any], rank: int) -> dict[str, Any]:
    function = _record(candidate.get("function_ref"))
    active = _record(candidate.get("active_time_metrics"))
    metrics = _record(candidate.get("metrics"))
    occurrences = candidate.get("observed_occurrences") if isinstance(candidate.get("observed_occurrences"), list) else []
    event_name = str(
        function.get("symbol")
        or candidate.get("trace_event_name")
        or candidate.get("hotspot_id")
        or f"热点 {rank}"
    )
    return {
        "hotspot_id": _opportunity_hotspot_id(candidate, rank),
        "rank": rank,
        "event_name": event_name,
        "module": str(function.get("module") or candidate.get("module") or "unknown"),
        "source_path": function.get("file"),
        "source_line": function.get("line"),
        "source_resolution": function.get("resolution") or "unresolved",
        "temporal_pressure": candidate.get("temporal_pressure"),
        "candidate_role": candidate.get("candidate_role"),
        "stage_context": candidate.get("stage_context"),
        "total_duration_ms": _metric_number(
            active.get("total_duration_ms"), active.get("inclusive_total_duration_ms"),
            metrics.get("total_duration_ms"), metrics.get("inclusive_total_duration_ms"),
        ),
        "max_duration_ms": _metric_number(
            active.get("max_duration_ms"), active.get("max_span_duration_ms"),
            metrics.get("max_duration_ms"), metrics.get("max_span_duration_ms"),
        ),
        "call_count": _metric_number(
            active.get("call_count"), active.get("occurrence_count"),
            metrics.get("call_count"), metrics.get("occurrence_count"), len(occurrences) or None,
        ),
        "unresolved_reason": candidate.get("unresolved_reason"),
    }


def _opportunity_htrace_tag_catalog(
    artifact: dict[str, Any],
    connectors: SourceConnectorManager,
) -> dict[str, Any]:
    """Load the same source-versioned HiTrace tag catalog as the Web flow.

    A raw trace remains inspectable when its connector has been removed.  In
    that case the shared extractor still classifies labels and execution
    contexts, but records that source-backed tag decoding was unavailable.
    """

    try:
        connector = connectors.get(str(artifact["source_connector_id"]))
        source_root = Path(str(connector["source_root"])).resolve()
    except (KeyError, PlatformError):
        return {
            "status": "unavailable",
            "tag_by_bit": {},
            "provenance": {
                "status": "unavailable",
                "reason": "Trace source connector is unavailable for HiTrace tag decoding.",
            },
        }
    from backend.hitrace_instrumentation import load_hitrace_tag_catalog

    return load_hitrace_tag_catalog(source_root, _workspace_root())


def _opportunity_self_time_legacy_ids(
    artifact: dict[str, Any],
    events: list[dict[str, Any]],
) -> dict[str, str]:
    """Preserve an immutable Phase-A hotspot id only for an exact context match.

    The Web self-time extractor owns the event semantics.  This compatibility
    map merely lets existing source-match artifacts continue to address a
    candidate when its symbol and any recorded process/thread context agree.
    """

    candidates = [item for item in artifact.get("hotspot_candidates", []) if isinstance(item, dict)]
    claimed: set[int] = set()
    result: dict[str, str] = {}
    for event in events:
        event_name = str(event.get("event_name") or "")
        context = _record(event.get("trace_context"))
        for index, candidate in enumerate(candidates):
            if index in claimed:
                continue
            function = _record(candidate.get("function_ref"))
            candidate_name = str(
                function.get("symbol") or candidate.get("trace_event_name") or candidate.get("event_name") or ""
            )
            if candidate_name != event_name:
                continue
            candidate_process = str(
                function.get("process") or candidate.get("process_name") or candidate.get("process") or ""
            ).strip()
            candidate_thread = str(
                function.get("thread") or candidate.get("thread_name") or candidate.get("thread") or ""
            ).strip()
            if candidate_process and candidate_process != str(context.get("process_name") or ""):
                continue
            if candidate_thread and candidate_thread != str(context.get("thread_name") or ""):
                continue
            event_id = str(event.get("event_id") or "")
            if event_id:
                result[event_id] = _opportunity_hotspot_id(candidate, index + 1)
                claimed.add(index)
            break
    return result


def _opportunity_self_time_hotspot(
    event: dict[str, Any],
    rank: int,
    *,
    legacy_hotspot_id: str | None = None,
) -> dict[str, Any]:
    """Project the shared Web self-time event onto the Desktop hotspot card."""

    candidate = _opportunity_self_time_candidate(
        event,
        rank,
        legacy_hotspot_id=legacy_hotspot_id,
    )
    event_id = str(candidate["event_id"])
    metrics = _record(candidate.get("metrics"))
    trace_module = _record(event.get("trace_module"))
    return {
        "hotspot_id": candidate["hotspot_id"],
        "event_id": event_id,
        "legacy_hotspot_id": legacy_hotspot_id,
        "rank": rank,
        "module_rank": event.get("module_rank"),
        "event_name": candidate["trace_event_name"],
        "event_kind": event.get("event_kind"),
        "discovery_id": event.get("discovery_id"),
        "module": str(trace_module.get("module_name") or "Unclassified"),
        "module_key": str(trace_module.get("module_key") or "unclassified"),
        "module_classification": trace_module.get("classification"),
        "module_evidence": trace_module.get("evidence"),
        "system_collapsed": bool(trace_module.get("system_collapsed")),
        "source_path": None,
        "source_line": None,
        "source_resolution": event.get("source_resolution") or "raw_htrace_symbol_only",
        "total_duration_ms": _metric_number(metrics.get("self_time_estimate_total_duration_ms")),
        "inclusive_total_duration_ms": _metric_number(metrics.get("inclusive_total_duration_ms")),
        "max_duration_ms": _metric_number(metrics.get("max_self_time_estimate_duration_ms")),
        "max_span_duration_ms": _metric_number(metrics.get("max_span_duration_ms")),
        "call_count": _metric_number(metrics.get("occurrence_count")),
        "metric_semantics": "instrumentation_span_direct_child_self_time_estimate",
        "trace_context": candidate["trace_context"],
        "trace_event_refs": candidate["trace_event_refs"],
        "hitrace_marker": candidate["hitrace_marker"],
        "unresolved_reason": (
            "Raw HiTrace event names are trace evidence; source ownership still requires a separate source match."
        ),
    }


def _opportunity_self_time_candidate(
    event: dict[str, Any],
    rank: int,
    *,
    legacy_hotspot_id: str | None = None,
) -> dict[str, Any]:
    """Represent a Web self-time event as a source/route hotspot candidate.

    Detail cards and the later source-match/route calls must address the same
    immutable event.  Keeping the complete self-time metrics and trace context
    here prevents those later stages from silently falling back to the older
    Phase-A span candidate.
    """

    event_id = str(event.get("event_id") or f"event_htrace_self_{rank}")
    event_name = str(event.get("event_name") or f"热点 {rank}")
    metrics = dict(_record(event.get("metrics")))
    context = dict(_record(event.get("trace_context")))
    trace_module = _record(event.get("trace_module"))
    active_time_metrics = {
        "total_duration_ms": _metric_number(metrics.get("self_time_estimate_total_duration_ms")),
        "max_duration_ms": _metric_number(metrics.get("max_self_time_estimate_duration_ms")),
        "call_count": _metric_number(metrics.get("occurrence_count")),
    }
    active_time_metrics = {key: value for key, value in active_time_metrics.items() if value is not None}
    return {
        "hotspot_id": legacy_hotspot_id or event_id,
        "event_id": event_id,
        "legacy_hotspot_id": legacy_hotspot_id,
        "trace_event_name": event_name,
        "event_name": event_name,
        "event_kind": event.get("event_kind"),
        "function_ref": {
            "symbol": event_name,
            "module": str(trace_module.get("module_name") or "Unclassified"),
            "process": context.get("process_name"),
            "thread": context.get("thread_name"),
            "resolution": event.get("source_resolution") or "raw_htrace_symbol_only",
        },
        "active_time_metrics": active_time_metrics,
        "metrics": metrics,
        "trace_context": context,
        "trace_event_refs": event.get("trace_event_refs") if isinstance(event.get("trace_event_refs"), list) else [],
        "hitrace_marker": event.get("hitrace_marker") if isinstance(event.get("hitrace_marker"), dict) else None,
        "metric_semantics": "instrumentation_span_direct_child_self_time_estimate",
    }


def _opportunity_raw_htrace_catalog(
    artifact: dict[str, Any],
    *,
    state_root: Path,
    connectors: SourceConnectorManager,
) -> dict[str, Any]:
    """Reuse the Web raw-HiTrace self-time/module catalog with a hard fallback."""

    from backend.htrace_analysis import list_htrace_self_time_events

    tag_catalog = _opportunity_htrace_tag_catalog(artifact, connectors)
    raw_htrace = _record(artifact.get("raw_htrace"))
    try:
        catalog = list_htrace_self_time_events(
            raw_htrace,
            state_root=state_root,
            limit=100,
            tag_catalog=tag_catalog,
            group_by_module=True,
            per_module_limit=6,
        )
    except PlatformError as exc:
        return {
            "events": [],
            "module_event_groups": [],
            "module_catalog": {"classification": "hitrace_tag_then_trace_identity_v1", "groups": []},
            "quality": {
                "status": "unavailable",
                "reason": exc.message,
                "error_code": exc.code,
                "profile_id": "htrace-event-self-time-v2",
                "source": "converted_callstack_direct_child_subtraction",
                "fallback": "normalized_hotspot_candidates",
                "hitrace_tag_catalog": dict(tag_catalog.get("provenance") or {}),
                "limitations": [
                    "The converted parent-linked callstack database is unavailable, so raw-HiTrace self-time cannot be computed.",
                    "Fallback candidates retain their reported trace-span metrics and must not be interpreted as CPU self time.",
                ],
            },
        }

    events = [item for item in catalog.get("events", []) if isinstance(item, dict)]
    quality = _record(catalog.get("quality"))
    raw_limitations = quality.get("limitations") if isinstance(quality.get("limitations"), list) else []
    limitations = [str(item) for item in raw_limitations if isinstance(item, str)]
    if not events:
        limitations.append(
            "No shared self-time event was available; fallback candidates retain trace-span semantics, not CPU self time."
        )
    catalog["quality"] = {
        **quality,
        "hitrace_marker_events": sum(1 for event in events if isinstance(event.get("hitrace_marker"), dict)),
        "hitrace_tag_catalog": dict(tag_catalog.get("provenance") or {}),
        **({"fallback": "normalized_hotspot_candidates"} if not events else {}),
        "limitations": list(dict.fromkeys(limitations)),
    }
    catalog.setdefault("module_event_groups", [])
    catalog.setdefault("module_catalog", {"classification": "hitrace_tag_then_trace_identity_v1", "groups": []})
    return catalog


def _read_opportunity_trace(store: ArtifactStore, artifact_id: str) -> dict[str, Any]:
    if artifact_id in set(_record(_flow_trace_registry(store).read().get("deleted_traces"))):
        raise PlatformError("TRACE_ANALYSIS_NOT_RESTORABLE", "该真实 Trace 已从平台 Trace 列表删除。", status_code=404)
    artifact = store.read("trace_analysis_result", artifact_id)
    if artifact.get("input_kind") not in {"raw_htrace", "external_trace_analysis_report", "sys_trace_experimental"}:
        raise PlatformError("TRACE_ANALYSIS_NOT_RESTORABLE", "该工件不是机会推理支持的真实 Trace。", status_code=422)
    return artifact


def _find_opportunity_hotspot(analysis: dict[str, Any], hotspot_id: str) -> dict[str, Any] | None:
    candidates = [item for item in analysis.get("hotspot_candidates", []) if isinstance(item, dict)]
    return next((item for rank, item in enumerate(candidates, start=1)
                 if _opportunity_hotspot_id(item, rank) == hotspot_id), None)


def _resolve_opportunity_hotspot_candidate(
    analysis: dict[str, Any],
    hotspot_id: str,
    *,
    state_root: Path,
    connectors: SourceConnectorManager,
) -> dict[str, Any] | None:
    """Resolve exactly the hotspot identifier exposed by trace detail.

    Raw HiTrace detail is sourced from the shared Web self-time catalog rather
    than the older normalized candidates.  Accept both its immutable event ID
    and an exact legacy candidate ID; when self-time extraction is unavailable,
    retain the normalized-candidate fallback used by the detail response.
    """

    if analysis.get("input_kind") == "raw_htrace":
        catalog = _opportunity_raw_htrace_catalog(
            analysis,
            state_root=state_root,
            connectors=connectors,
        )
        events = [item for item in catalog.get("events", []) if isinstance(item, dict)]
        if events:
            legacy_ids = _opportunity_self_time_legacy_ids(analysis, events)
            for rank, event in enumerate(events, start=1):
                event_id = str(event.get("event_id") or f"event_htrace_self_{rank}")
                legacy_hotspot_id = legacy_ids.get(event_id)
                if hotspot_id not in {event_id, legacy_hotspot_id}:
                    continue
                return _opportunity_self_time_candidate(
                    event,
                    rank,
                    legacy_hotspot_id=legacy_hotspot_id,
                )
            return None
    return _find_opportunity_hotspot(analysis, hotspot_id)


def _opportunity_trace_catalog(store: ArtifactStore, query: dict[str, list[str]]) -> dict[str, Any]:
    """List every real trace, independent of the currently selected code repo.

    A trace can legitimately belong to another registered connector.  Filtering
    it out in the selector made Desktop appear to have no traces and prevented
    the user from switching the code repository deliberately.
    """

    limit = _bounded_int(_query_value(query, "limit", "100"), 100, 500)
    deleted = set(_record(_flow_trace_registry(store).read().get("deleted_traces")))
    rows = [
        _opportunity_trace_summary(item)
        for item in store.list_artifacts("trace_analysis_result", limit=500)
        if item.get("artifact_id") not in deleted
        and item.get("input_kind") in {"raw_htrace", "external_trace_analysis_report", "sys_trace_experimental"}
    ]
    rows.sort(
        key=lambda item: (str(item.get("uploaded_at") or item.get("created_at") or ""), str(item.get("artifact_id") or "")),
        reverse=True,
    )
    visible_rows = rows[:limit]
    return {
        "schema_version": "desktop_opportunity_trace_catalog/v1",
        "traces": visible_rows,
        "count": len(visible_rows),
        "total_count": len(rows),
    }


def _opportunity_trace_detail(
    store: ArtifactStore,
    artifact_id: str,
    *,
    state_root: Path,
    connectors: SourceConnectorManager,
) -> dict[str, Any]:
    artifact = _read_opportunity_trace(store, artifact_id)
    candidates = [item for item in artifact.get("hotspot_candidates", []) if isinstance(item, dict)]
    module_catalog: dict[str, Any] = {"classification": "unavailable", "groups": []}
    module_event_groups: list[dict[str, Any]] = []
    hotspot_quality: dict[str, Any] = {
        "status": "available" if candidates else "unavailable",
        "profile_id": "normalized-trace-hotspot-candidates/v1",
        "source": "normalized_hotspot_candidates",
    }
    if artifact.get("input_kind") == "raw_htrace":
        catalog = _opportunity_raw_htrace_catalog(artifact, state_root=state_root, connectors=connectors)
        events = [item for item in catalog.get("events", []) if isinstance(item, dict)]
        module_catalog = _record(catalog.get("module_catalog"))
        module_event_groups = [item for item in catalog.get("module_event_groups", []) if isinstance(item, dict)]
        hotspot_quality = _record(catalog.get("quality"))
        if events:
            legacy_ids = _opportunity_self_time_legacy_ids(artifact, events)
            hotspots = [
                _opportunity_self_time_hotspot(
                    event,
                    rank,
                    legacy_hotspot_id=legacy_ids.get(str(event.get("event_id") or "")),
                )
                for rank, event in enumerate(events, start=1)
            ]
        else:
            hotspots = [_opportunity_hotspot(item, rank) for rank, item in enumerate(candidates, start=1)]
    else:
        hotspots = [_opportunity_hotspot(item, rank) for rank, item in enumerate(candidates, start=1)]
        hotspots.sort(
            key=lambda item: (
                float(item.get("total_duration_ms") or item.get("max_duration_ms") or 0),
                -int(item.get("rank") or 0),
            ),
            reverse=True,
        )
        for rank, hotspot in enumerate(hotspots, start=1):
            hotspot["rank"] = rank
    return {
        "schema_version": "desktop_opportunity_trace_detail/v1",
        "trace": _opportunity_trace_summary(artifact),
        "quality": artifact.get("quality") or {},
        "hotspot_quality": hotspot_quality,
        "module_catalog": module_catalog,
        "module_event_groups": module_event_groups,
        "hotspots": hotspots[:80],
    }


def _source_excerpt(path: Path, line_number: int, *, radius: int = 6) -> dict[str, Any]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return {"line_start": 0, "line_end": 0, "code": ""}
    line = max(1, min(int(line_number or 1), max(1, len(lines))))
    start = max(1, line - radius); end = min(len(lines), line + radius)
    code = "\n".join(f"{index:>6}  {lines[index - 1][:500]}" for index in range(start, end + 1))
    return {"line_start": start, "line_end": end, "code": code[:16_000]}


def _rg_source_candidates(source_root: Path, symbols: list[str], *, limit: int = 12) -> list[dict[str, Any]]:
    """Run a time/output bounded literal search over source-like files."""

    results: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    deadline = time.monotonic() + 6.0
    output_budget = 256 * 1024
    for symbol in symbols:
        if time.monotonic() >= deadline or output_budget <= 0:
            break
        if not symbol or len(symbol) > 240 or any(char in symbol for char in ("\n", "\r", "\0")):
            continue
        command = [
            "rg", "--line-number", "--column", "--fixed-strings", "--no-heading", "--color", "never",
            "--max-filesize", "8M", "--glob", "!.git/**", "--glob", "!node_modules/**",
            "--glob", "!out/**", "--glob", "!build/**",
            "--glob", "*.{c,cc,cpp,cxx,h,hh,hpp,hxx,arkts,ets,ts,js}", "--", symbol, str(source_root),
        ]
        try:
            process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
        except OSError:
            break
        pending = b""
        try:
            assert process.stdout is not None
            descriptor = process.stdout.fileno()
            while len(results) < limit and output_budget > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                ready, _, _ = select.select([descriptor], [], [], min(0.2, remaining))
                if not ready:
                    if process.poll() is not None:
                        break
                    continue
                chunk = os.read(descriptor, min(64 * 1024, output_budget))
                if not chunk:
                    break
                output_budget -= len(chunk)
                pending += chunk
                lines = pending.split(b"\n"); pending = lines.pop()
                for raw_bytes in lines:
                    raw = raw_bytes.decode("utf-8", errors="replace")
                    match = re.match(r"^(.*?):(\d+):(\d+):(.*)$", raw)
                    if not match:
                        continue
                    absolute = Path(match.group(1)).resolve()
                    try:
                        relative = str(absolute.relative_to(source_root)).replace("\\", "/")
                    except ValueError:
                        continue
                    line = int(match.group(2)); key = (relative, line)
                    if key in seen:
                        continue
                    seen.add(key)
                    results.append({
                        "path": relative, "line": line, "column": int(match.group(3)),
                        "match_text": match.group(4).strip()[:500], "matched_symbol": symbol,
                    })
                    if len(results) >= limit:
                        break
        finally:
            if process.poll() is None:
                process.kill()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill(); process.wait()
            if process.stdout is not None:
                process.stdout.close()
        if len(results) >= limit:
            break
    return results


def _resolve_opportunity_source(
    store: ArtifactStore,
    connectors: SourceConnectorManager,
    state_root: Path,
    body: dict[str, Any],
) -> dict[str, Any]:
    analysis_id = str(body.get("analysis_artifact_id") or "").strip()
    hotspot_id = str(body.get("hotspot_id") or "").strip()
    connector_id = str(body.get("source_connector_id") or "").strip()
    analysis = _read_opportunity_trace(store, analysis_id)
    candidate = _resolve_opportunity_hotspot_candidate(
        analysis,
        hotspot_id,
        state_root=state_root,
        connectors=connectors,
    )
    if candidate is None:
        raise PlatformError("TRACE_HOTSPOT_EVENT_NOT_FOUND", "所选热点不属于当前真实 Trace。", status_code=422)
    connector_id = connector_id or str(analysis.get("source_connector_id") or "")
    if not connector_id:
        raise PlatformError("SOURCE_CONNECTOR_REQUIRED", "热点代码匹配需要选择代码仓。", status_code=422)
    connector = connectors.get(connector_id); source_root = Path(str(connector["source_root"])).resolve()
    function = _record(candidate.get("function_ref"))
    symbol = str(function.get("symbol") or candidate.get("trace_event_name") or "").strip()
    declared_path = str(function.get("file") or "").strip()
    try:
        declared_line = max(0, int(function.get("line") or 0))
    except (TypeError, ValueError):
        declared_line = 0
    matches: list[dict[str, Any]] = []
    if declared_path:
        try:
            exact = connectors.resolve_source_path(connector_id, declared_path)
            relative = str(exact.relative_to(source_root)).replace("\\", "/")
            line = declared_line or 1
            matches.append({
                "path": relative, "line": line, "column": 1, "match_text": symbol,
                "matched_symbol": symbol, "match_level": "declared_source_location",
                **_source_excerpt(exact, line),
            })
        except PlatformError:
            pass
    if not matches and symbol:
        symbol_variants = list(dict.fromkeys([symbol, symbol.split("::")[-1], symbol.split("(", 1)[0].split("::")[-1]]))
        for item in _rg_source_candidates(source_root, symbol_variants):
            path = source_root / item["path"]
            matches.append({**item, "match_level": "bounded_literal_search", **_source_excerpt(path, int(item["line"]))})
    source_binding = _record(analysis.get("source_binding")) if connector_id == analysis.get("source_connector_id") else {
        "match_level": "unknown", "reason": "selected_connector_differs_from_trace_connector",
    }
    result = {
        "schema_version": "desktop_hotspot_source_match/v1",
        "analysis_artifact_id": analysis_id,
        "hotspot_id": hotspot_id,
        "event_name": symbol or hotspot_id,
        "source_connector_id": connector_id,
        "source_snapshot_id": _record(connector.get("snapshot")).get("snapshot_id"),
        "source_binding": source_binding,
        "matches": matches,
        "selected_match": matches[0] if matches else None,
        "quality": {
            "status": "available" if matches else "unavailable",
            "reason": "" if matches else "Trace 热点只有运行时标签，当前代码仓未找到可核验的源码位置。",
        },
    }
    artifact = store.write(
        "desktop_hotspot_source_match", result,
        source_connector_id=connector_id,
        source_binding=source_binding,
        producer={"name": "desktop-real-trace-source-matcher", "version": "1.0.0"},
        input_artifacts=[f"artifact:{analysis_id}"],
    )
    return {**result, "artifact": artifact}


def _bounded_strings(value: Any, limit: int = 8) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item)[:320] for item in value if isinstance(item, (str, int, float))][:limit]


def _desktop_hardware_routing_records() -> list[dict[str, Any]]:
    """Project source-backed hardware records to the Web model-router contract.

    This projection deliberately carries no hotspot-dependent score.  It gives
    the model the same bounded capability facts used by the Web route builder;
    selecting a unit remains a model decision.
    """

    records: list[dict[str, Any]] = []
    for raw in _knowledge_payload("hardware").get("records", []):
        if not isinstance(raw, dict):
            continue
        record_id = str(raw.get("record_id") or raw.get("hardware_id") or "").strip()
        if not record_id:
            continue
        capability = _record(raw.get("source_capability_evidence"))
        provenance = _record(raw.get("provenance"))
        records.append({
            "record_id": record_id,
            "record_type": str(raw.get("record_type") or "hardware_capability"),
            "hardware_id": str(raw.get("hardware_id") or record_id),
            "name": str(raw.get("name") or raw.get("hardware_id") or record_id),
            "unit_type": raw.get("unit_type") or capability.get("unit_type"),
            "software_interface": raw.get("software_interface"),
            "capability_scope": raw.get("capability_scope"),
            "device_availability": raw.get("device_availability"),
            "performance_envelope": raw.get("performance_envelope"),
            "supported_algorithms": _bounded_strings(raw.get("supported_algorithms_from_strict_cases"), 12),
            "implementation_paths": _bounded_strings(raw.get("implementation_paths"), 8),
            "constraints": _bounded_strings(raw.get("constraints"), 6),
            "provenance": {
                "confidence": provenance.get("confidence"),
                "origin": provenance.get("origin"),
            },
        })
    return records


def _semantic_family_directory(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Describe every API family in stable catalog order, without code scoring."""

    families: dict[str, dict[str, Any]] = {}
    for raw in sorted(records, key=lambda item: str(item.get("record_id") or item.get("symbol") or "")):
        family = _record(raw.get("semantic_family"))
        family_id = str(family.get("id") or "uncategorized").strip()
        item = families.setdefault(family_id, {
            "family_id": family_id,
            "label_zh": str(family.get("label_zh") or family_id),
            "record_count": 0,
            "example_symbols": [],
        })
        item["record_count"] += 1
        symbol = str(raw.get("symbol") or raw.get("name_zh") or "").strip()
        if symbol and symbol not in item["example_symbols"] and len(item["example_symbols"]) < 3:
            item["example_symbols"].append(symbol)
    return list(families.values())


def _platform_api_directory(unit: str) -> dict[str, Any]:
    payload = _hardware_api_payload(unit)
    records = [item for item in payload.get("records", []) if isinstance(item, dict)]
    metadata = _hardware_api_metadata(payload)
    return {
        "unit_id": unit,
        "catalog_origin": "platform",
        "kb_id": metadata.get("kb_id"),
        "version": metadata.get("version"),
        "content_sha256": metadata.get("content_sha256"),
        "record_count": len(records),
        "semantic_families": _semantic_family_directory(records),
        "browse_contract": (
            "/v1/hardware-kb/npu-api/records" if unit == "npu"
            else f"/v1/hardware-kb/accelerator-api/{unit}/records"
        ),
        "evidence_boundary": "目录描述源码可见接口合同，不证明目标设备支持、正确性或性能收益。",
    }


def _custom_api_directory(unit_id: str, catalog: dict[str, Any]) -> dict[str, Any]:
    metadata = _record(catalog.get("metadata"))
    records = [item for item in catalog.get("records", []) if isinstance(item, dict)]
    return {
        "unit_id": unit_id,
        "catalog_origin": "user_built",
        "kb_id": catalog.get("kb_id"),
        "version": metadata.get("kb_version"),
        "content_sha256": metadata.get("content_sha256"),
        "record_count": len(records),
        "semantic_families": _semantic_family_directory(records),
        "browse_contract": f"/v1/custom-api-kbs/{catalog.get('kb_id')}",
        "evidence_boundary": "自建目录来自有界源码扫描；接口公开性、设备能力和收益仍需验证。",
    }


def _software_opportunity_cards() -> list[dict[str, Any]]:
    """Unranked non-accelerator directions for the one-shot Desktop agent."""

    return [
        {
            "direction_id": "cpu_parallel",
            "label_zh": "CPU 并行",
            "fit_questions_zh": ["工作单元是否相互独立", "调度、同步和合并成本是否小于并行收益"],
            "required_evidence_zh": ["依赖关系", "任务粒度", "共跑争用", "串行基线"],
        },
        {
            "direction_id": "cpu_simd",
            "label_zh": "CPU SIMD",
            "fit_questions_zh": ["核心循环是否存在规则数据并行", "别名、对齐和尾部处理能否明确"],
            "required_evidence_zh": ["循环依赖", "数据类型与布局", "向量化报告", "功能对照"],
        },
        {
            "direction_id": "algorithm_or_dataflow",
            "label_zh": "算法与数据流",
            "fit_questions_zh": ["是否可减少重复计算、拷贝或中间状态", "复杂度变化是否保持语义等价"],
            "required_evidence_zh": ["调用与数据依赖", "复杂度", "内存流量", "正确性 oracle"],
        },
    ]


def _source_code_for_model(source_match: dict[str, Any]) -> str:
    """Remove display-only line gutters from the persisted source excerpt."""

    code = str(_record(source_match.get("selected_match")).get("code") or "")
    return "\n".join(re.sub(r"^\s*\d+\s{2}", "", line) for line in code.splitlines())[:16_000]


def _model_route_trace_metrics(hotspot: dict[str, Any]) -> dict[str, float | int]:
    """Keep only bounded timing facts in the isolated Web routing context."""

    active = _record(hotspot.get("active_time_metrics"))
    metrics = _record(hotspot.get("metrics"))
    projected: dict[str, float | int] = {}
    sources = (active, metrics)
    for key in (
        "self_time_estimate_total_duration_ms",
        "max_self_time_estimate_duration_ms",
        "inclusive_total_duration_ms",
        "max_span_duration_ms",
        "occurrence_count",
        "first_start_ns",
        "last_end_ns",
    ):
        value = _metric_number(*(source.get(key) for source in sources))
        if value is not None:
            projected[key] = value
    fallbacks = {
        "inclusive_total_duration_ms": (active.get("total_duration_ms"), metrics.get("total_duration_ms")),
        "max_span_duration_ms": (active.get("max_duration_ms"), metrics.get("max_duration_ms")),
        "occurrence_count": (
            active.get("call_count"), active.get("occurrence_count"),
            metrics.get("call_count"), metrics.get("occurrence_count"),
        ),
    }
    for key, values in fallbacks.items():
        if key in projected:
            continue
        value = _metric_number(*values)
        if value is not None:
            projected[key] = value
    return projected


def _route_opportunity_hardware(
    store: ArtifactStore,
    connectors: SourceConnectorManager,
    state_root: Path,
    body: dict[str, Any],
) -> dict[str, Any]:
    """Prepare model-owned route evidence; never select or score a unit here."""

    manual_code_artifact_id = str(body.get("manual_code_artifact_id") or "").strip()
    device_profile = _record(body.get("device_profile"))
    if manual_code_artifact_id:
        source_match = store.read("desktop_manual_hotspot_code", manual_code_artifact_id)
        source_match_artifact_id = manual_code_artifact_id
        analysis_id = ""
        hotspot_id = f"manual:{manual_code_artifact_id}"
        analysis = {"trace_name": "用户粘贴热点代码", "quality": {"status": "code_only"}}
        hotspot = {
            "function_ref": {"symbol": source_match.get("function_hint") or "pasted_hotspot", "module": "manual"},
            "trace_event_name": source_match.get("event_name") or source_match.get("function_hint") or "pasted_hotspot",
            "active_time_metrics": {},
            "trace_context": {"source": "user_paste"},
            "candidate_role": "user_selected_code",
        }
    else:
        analysis_id = str(body.get("analysis_artifact_id") or "").strip()
        hotspot_id = str(body.get("hotspot_id") or "").strip()
        analysis = _read_opportunity_trace(store, analysis_id)
        hotspot = _resolve_opportunity_hotspot_candidate(
            analysis,
            hotspot_id,
            state_root=state_root,
            connectors=connectors,
        )
        if hotspot is None:
            raise PlatformError("TRACE_HOTSPOT_EVENT_NOT_FOUND", "所选热点不属于当前真实 Trace。", status_code=422)
        source_match = _record(body.get("source_match"))
        source_match_artifact_id = str(_record(source_match.get("artifact")).get("artifact_id") or "").strip()
        if not source_match_artifact_id:
            raise PlatformError(
                "OPPORTUNITY_SOURCE_MATCH_ARTIFACT_REQUIRED",
                "模型路由需要平台生成并持久化的热点代码匹配工件。",
                status_code=422,
            )
        persisted_match = store.read("desktop_hotspot_source_match", source_match_artifact_id)
        if (persisted_match.get("analysis_artifact_id") != analysis_id
                or persisted_match.get("hotspot_id") != hotspot_id):
            raise PlatformError(
                "OPPORTUNITY_SOURCE_MATCH_MISMATCH",
                "热点代码匹配工件不属于当前 Trace 热点。",
                status_code=409,
            )
        source_match = persisted_match
    if not str(_record(source_match.get("selected_match")).get("code") or "").strip():
        raise PlatformError(
            "OPPORTUNITY_SOURCE_CODE_REQUIRED",
            "模型路由需要已持久化且非空的热点源码；只有 Trace 标签时不能构建代码驱动路由。",
            status_code=422,
        )
    custom_catalogs = _custom_api_manager(state_root, connectors).reasoning_catalogs()
    hardware_records = _desktop_hardware_routing_records()
    hardware_records.extend(
        catalog["routing_record"]
        for _unit_id, catalog in sorted(custom_catalogs.items())
        if isinstance(catalog.get("routing_record"), dict)
    )
    function = _record(hotspot.get("function_ref"))
    source = _record(source_match.get("selected_match"))
    active_metrics = _record(hotspot.get("active_time_metrics")) or _record(hotspot.get("metrics"))
    route_metrics = _model_route_trace_metrics(hotspot)
    code_unit = {
        "artifact_id": source_match_artifact_id,
        "function_hint": str(function.get("symbol") or hotspot.get("trace_event_name") or ""),
        "code_language": "cpp",
        "code": _source_code_for_model(source_match),
        "hotspot": {
            "event_name": str(function.get("symbol") or hotspot.get("trace_event_name") or hotspot_id),
            "trace_metrics": route_metrics,
        },
        "source": {
            "origin": "user_paste" if manual_code_artifact_id else "desktop_real_trace_source_match",
            "path": source.get("path"),
            "line_start": source.get("line_start") or source.get("line"),
            "line_end": source.get("line_end") or source.get("line"),
            "binding_level": _record(source_match.get("source_binding")).get("match_level"),
        },
    }
    target_platform = {
        "schema_version": "desktop_target_platform_profile/v1",
        "profile_source": "local_hdc" if device_profile else "unavailable",
        "vendor": device_profile.get("vendor"),
        "model": device_profile.get("model"),
        "chipset": device_profile.get("chipset"),
        "os_build": device_profile.get("os_build"),
        "cpu_groups": [
            {
                key: group.get(key)
                for key in ("id", "name", "cpus", "core_count", "max_frequency_khz", "min_frequency_khz")
                if group.get(key) is not None
            }
            for group in device_profile.get("cpu_groups", [])[:16]
            if isinstance(group, dict)
        ] if isinstance(device_profile.get("cpu_groups"), list) else [],
    }
    from backend.hardware_semantic_routing import build_hardware_semantic_routing_context

    route_context = build_hardware_semantic_routing_context(
        code_unit,
        hardware_records,
        target_platform_profile=target_platform,
    )
    api_directories = [_platform_api_directory(unit) for unit in ("npu", "gpu", "dsp", "media")]
    api_directories.extend(
        _custom_api_directory(unit_id, catalog)
        for unit_id, catalog in sorted(custom_catalogs.items())
    )
    model_input_context = _record(route_context.get("model_input_context"))
    downstream_context = {
        "trace_evidence": {
            "analysis_artifact_id": analysis_id,
            "hotspot_id": hotspot_id,
            "trace_name": analysis.get("trace_name"),
            "event_name": code_unit["hotspot"]["event_name"],
            "trace_context": _record(hotspot.get("trace_context")),
            "trace_event_refs": (
                hotspot.get("trace_event_refs")
                if isinstance(hotspot.get("trace_event_refs"), list) else []
            ),
            "metrics": {**active_metrics, **route_metrics},
            "temporal_pressure": hotspot.get("temporal_pressure"),
            "candidate_role": hotspot.get("candidate_role"),
            "stage_context": hotspot.get("stage_context"),
            "quality": analysis.get("quality") or {},
            "evidence_kind": "manual_code" if manual_code_artifact_id else "real_trace",
        },
        "source_evidence": {
            "source_match_artifact_id": source_match_artifact_id,
            "source_connector_id": source_match.get("source_connector_id") or analysis.get("source_connector_id"),
            "source_binding": source_match.get("source_binding") or analysis.get("source_binding") or {},
            "source": code_unit["source"],
        },
    }
    model_context_sha256 = hashlib.sha256(json.dumps(
        model_input_context, ensure_ascii=True, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    payload = {
        "schema_version": "desktop_hardware_model_context/v1",
        "analysis_artifact_id": analysis_id, "hotspot_id": hotspot_id,
        "source_match_artifact_id": source_match_artifact_id or None,
        "target_platform": target_platform,
        "selection_owner": "right_code_agent_model",
        "selection_status": "model_required",
        "model_context_sha256": model_context_sha256,
        "model_input_context": model_input_context,
        "downstream_context": downstream_context,
        "api_directories": api_directories,
        "software_opportunity_cards": _software_opportunity_cards(),
        "agent_output_contract": {
            "route": {
                "decision": "select_units | needs_compute_core | no_accelerator_candidate",
                "candidates": "0..2 ordered capability-card unit_id values with code evidence and card evidence_ids",
            },
            "opportunities": "ranked heterogeneous, CPU parallel, SIMD or algorithm candidates with prerequisites and validation plan",
            "evidence_boundary": "API family and device availability are hypotheses until exact catalog records and real-device evidence are checked.",
        },
        "catalog_summary": {
            "capability_card_count": len(model_input_context.get("hardware_capability_cards", [])),
            "api_directory_count": len(api_directories),
            "api_record_count": sum(int(item.get("record_count") or 0) for item in api_directories),
        },
        "boundary": "平台只准备硬件中立画像、能力卡和 API 目录证据；单元路由与机会判断由右侧 Code Agent 完成，并需后续实测。",
    }
    artifact = store.write(
        "desktop_hardware_routing_context", payload,
        source_connector_id=str(source_match.get("source_connector_id") or analysis.get("source_connector_id") or ""),
        source_binding=_record(source_match.get("source_binding") or analysis.get("source_binding")),
        producer={"name": "desktop-hardware-model-context-builder", "version": "1.0.0"},
        input_artifacts=([f"artifact:{analysis_id}"] if analysis_id else [])
        + ([f"artifact:{payload['source_match_artifact_id']}"] if payload["source_match_artifact_id"] else []),
    )
    return {
        **payload,
        "artifact": {
            key: artifact.get(key)
            for key in ("artifact_id", "artifact_type", "created_at", "content_sha256")
            if artifact.get(key) is not None
        },
    }


def _save_manual_opportunity_code(store: ArtifactStore, body: dict[str, Any]) -> dict[str, Any]:
    code = str(body.get("code") or "")
    if not code.strip():
        raise PlatformError("OPPORTUNITY_MANUAL_CODE_REQUIRED", "请粘贴需要分析的热点代码。", status_code=422)
    if len(code.encode("utf-8")) > 16 * 1024:
        raise PlatformError("OPPORTUNITY_MANUAL_CODE_TOO_LARGE", "热点代码不能超过 16 KiB。", status_code=413)
    function_hint = str(body.get("function_hint") or "pasted_hotspot").strip()[:240]
    event_name = str(body.get("event_name") or function_hint or "pasted_hotspot").strip()[:240]
    connector_id = str(body.get("source_connector_id") or "").strip()[:160]
    payload = {
        "schema_version": "desktop_manual_hotspot_code/v1",
        "function_hint": function_hint,
        "event_name": event_name,
        "source_connector_id": connector_id or None,
        "selected_match": {
            "path": None,
            "line": 1,
            "line_start": 1,
            "line_end": max(1, code.count("\n") + 1),
            "code": code,
            "match_level": "user_paste",
        },
        "source_binding": {"match_level": "unbound", "source": "user_paste"},
        "quality": {"status": "available", "reason": "用户直接提供热点代码；未声明源码快照绑定。"},
    }
    artifact = store.write(
        "desktop_manual_hotspot_code",
        payload,
        source_connector_id=connector_id or None,
        source_binding=payload["source_binding"],
        producer={"name": "desktop-opportunity-manual-code", "version": "1.0"},
    )
    return {
        **payload,
        "artifact": {
            key: artifact.get(key)
            for key in ("artifact_id", "artifact_type", "created_at", "content_sha256")
            if artifact.get(key) is not None
        },
    }


def _text_response(content: str, *, content_type: str, filename: str) -> dict[str, Any]:
    return {
        "__desktop_response_kind__": "text",
        "content": content,
        "content_type": content_type,
        "filename": filename,
    }


def _safe_download_stem(value: Any, fallback: str) -> str:
    stem = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(value or fallback)).strip(".-")
    return stem[:100] or fallback


def _delete_flow_asset(store: ArtifactStore, artifact_type: str, artifact_id: str, *, cascade: bool) -> dict[str, Any]:
    if artifact_type not in {"application_flow_discovery", "application_flow_scenario"}:
        raise PlatformError("APPLICATION_FLOW_ASSET_TYPE_INVALID", "应用流程工件类型非法。", status_code=422)
    artifact = _ensure_flow_asset_active(store, artifact_type, artifact_id)
    child_scenarios: list[dict[str, Any]] = []
    deleted_ids = set(_deleted_flow_assets(store))
    if artifact_type == "application_flow_discovery":
        child_scenarios = [
            item for item in store.list_artifacts(
                "application_flow_scenario",
                limit=500,
                source_connector_id=str(artifact.get("source_connector_id") or "") or None,
            )
            if item.get("discovery_artifact_id") == artifact_id
            and item.get("artifact_id") not in deleted_ids
        ]
        if child_scenarios and not cascade:
            raise PlatformError(
                "APPLICATION_FLOW_ASSET_HAS_CHILDREN",
                "该流程发现仍有场景版本，请确认级联删除。",
                status_code=409,
                details={"child_scenario_count": len(child_scenarios)},
            )
    deleted_at = utc_now()
    registry = _flow_asset_registry(store)
    value = registry.read()
    deleted = _record(value.get("deleted_assets"))
    targets = [(artifact_type, artifact_id), *[
        ("application_flow_scenario", str(item.get("artifact_id") or "")) for item in child_scenarios
    ]]
    for target_type, target_id in targets:
        if target_id:
            deleted[target_id] = {
                "artifact_type": target_type,
                "deleted_at": deleted_at,
                "cascade_root_id": artifact_id if target_id != artifact_id else None,
            }
    value["deleted_assets"] = deleted
    registry.write(value)
    store.append_event({
        "event_type": "application_flow_asset_deleted",
        "artifact_type": artifact_type,
        "artifact_id": artifact_id,
        "cascade_count": len(child_scenarios),
        "created_at": deleted_at,
    })
    return {
        "status": "deleted", "artifact_type": artifact_type, "artifact_id": artifact_id,
        "cascade_count": len(child_scenarios), "deleted_at": deleted_at,
        "retention": "immutable_artifacts_preserved",
    }


class _AgentRunReader:
    """Read the existing durable Agent history without importing model runtime code."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @staticmethod
    def public(run: dict[str, Any]) -> dict[str, Any]:
        result = {key: run.get(key) for key in (
            "run_id", "project_id", "mode", "goal", "state", "state_detail", "created_at", "updated_at",
            "current_turn", "budget", "action_catalog_version", "skill_versions", "engine",
        )} | {"event_cursor": max(0, int(run.get("next_event_sequence") or 1) - 1)}
        context = _record(run.get("context"))
        result["context"] = {
            key: value for key, value in context.items()
            if key in {
                "analysis_record_id", "device_execution_mode", "target_platform_profile", "skill_profile",
                "source_connector_id", "source_snapshot_id", "runtime_environment",
            }
        }
        return result

    def _read_path(self, path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

    def list_recent(self, *, limit: int = 100, order_by: str = "updated_at") -> list[dict[str, Any]]:
        rows = [self.public(run) for path in self.root.glob("run_*/run.json") if (run := self._read_path(path))]
        rows.sort(key=lambda item: str(item.get(order_by) or item.get("created_at") or ""), reverse=True)
        return rows[:_bounded_int(limit, 100)]

    def read(self, run_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"run_[a-f0-9]{24}", run_id):
            raise PlatformError("AGENT_RUN_NOT_FOUND", "未找到指定的 Agent Run。", status_code=404)
        value = self._read_path(self.root / run_id / "run.json")
        if not value:
            raise PlatformError("AGENT_RUN_NOT_FOUND", "未找到指定的 Agent Run。", status_code=404)
        return value

    def events(self, run_id: str, *, after_sequence: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        self.read(run_id)
        path = self.root / run_id / "events.jsonl"
        rows: list[dict[str, Any]] = []
        if path.is_file():
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try: item = json.loads(line)
                except json.JSONDecodeError: continue
                if isinstance(item, dict) and int(item.get("sequence") or 0) > after_sequence: rows.append(item)
        return rows[:_bounded_int(limit, 500, 1000)]


def _experience(state_root: Path, store: ArtifactStore):
    from backend.analysis_observability import AnalysisTraceStore
    from backend.experience_evolution import ExperienceEvolutionManager
    return ExperienceEvolutionManager(
        state_root / "experience-evolution", artifact_store=store,
        analysis_trace_store=AnalysisTraceStore(state_root / "analysis-traces"),
        agent_run_store=_AgentRunReader(state_root / "agent-runs"),
    )


def dispatch_workspace(request: dict[str, Any], state_root: Path) -> dict[str, Any] | None:
    """Return an RPC result for migrated workspace pages, or ``None``."""

    dpvs_result = dispatch_dpvs_analysis(request, state_root)
    if dpvs_result is not None:
        return dpvs_result

    method = str(request.get("method") or "GET").upper()
    parsed = urlsplit(str(request.get("path") or ""))
    path, query = parsed.path, parse_qs(parsed.query, keep_blank_values=True)
    body = _record(request.get("body"))
    store, connectors = _services(state_root)

    if method == "GET" and path == "/v1/overview":
        values = connectors.list()
        optimization = _knowledge_payload("optimization"); hardware = _knowledge_payload("hardware")
        meta = lambda item: {"kb_id": item.get("kb_id"), "version": item.get("kb_version"), "source_revision": item.get("source_revision"), "content_sha256": item.get("content_sha256"), "summary": item.get("summary", {})}
        return {"project": {"project_id": "local-default", "mode": "desktop_ssh_worker"}, "connectors": values,
                "default_connector": values[0] if values else None, "default_connector_status": "ready" if values else "missing",
                "optimization_kb": meta(optimization), "hardware_kb": meta(hardware), "artifact_store": store.summary()}
    if method == "GET" and path == "/v1/source-connectors":
        return {"connectors": connectors.list()}
    if method == "POST" and path == "/v1/source-connectors/local":
        source_root = str(body.get("source_root") or "").strip()
        if not source_root:
            raise PlatformError("SOURCE_PATH_INVALID", "代码仓绝对路径不能为空。", status_code=422)
        return {"connector": connectors.register_local(Path(source_root), display_name=str(body.get("display_name") or ""))}
    match = _CONNECTOR_REFRESH.fullmatch(path)
    if method == "POST" and match:
        return {"connector": connectors.refresh_snapshot(unquote(match.group(1)))}

    if method == "GET" and path == "/v1/hardware-kb/npu-api/records":
        return _api_catalog("npu", _query_value(query, "query"))
    if method == "GET" and path.startswith("/v1/hardware-kb/npu-api/records/"):
        return _api_record("npu", unquote(path.removeprefix("/v1/hardware-kb/npu-api/records/")))
    if method == "GET" and path == "/v1/hardware-kb/accelerator-api/units":
        return {
            "units": [
                {
                    "accelerator_unit": unit,
                    "api_kb": _hardware_api_metadata(_hardware_api_payload(unit)),
                    "scope": _hardware_api_payload(unit).get("scope", {}),
                    "evidence_boundary": _hardware_api_payload(unit).get("evidence_boundary", {}),
                }
                for unit in _ACCELERATOR_API_FILES
            ],
        }
    match = _ACCELERATOR_API_RECORD.fullmatch(path)
    if method == "GET" and match:
        return _api_record(unquote(match.group(1)), unquote(match.group(2)))
    match = _ACCELERATOR_API_RECORDS.fullmatch(path)
    if method == "GET" and match:
        return _api_catalog(unquote(match.group(1)), _query_value(query, "query"))

    custom_api_manager = None
    if path == "/v1/custom-api-kbs" or path.startswith("/v1/custom-api-kbs/"):
        custom_api_manager = _custom_api_manager(state_root, connectors)
    if method == "GET" and path == "/v1/custom-api-kbs":
        values = custom_api_manager.list()
        return {
            "schema_version": "custom_accelerator_api_kb_catalog/v1",
            "knowledge_bases": values,
            "count": len(values),
            "platform_merge_policy": "developer_only",
        }
    if method == "POST" and path == "/v1/custom-api-kbs/build":
        return custom_api_manager.build(
            unit_name=str(body.get("unit_name") or ""),
            source_connector_id=str(body.get("source_connector_id") or ""),
            source_root=str(body.get("source_root") or ""),
        )
    match = _CUSTOM_API_RECORD.fullmatch(path)
    if match and method == "GET":
        return custom_api_manager.record(unquote(match.group(1)), unquote(match.group(2)))
    match = _CUSTOM_API_KB.fullmatch(path)
    if match:
        kb_id = unquote(match.group(1))
        if method == "GET":
            return custom_api_manager.read(kb_id, query=_query_value(query, "query"))
        if method == "DELETE":
            return custom_api_manager.delete(kb_id)

    if path in {"/v1/optimization-kb/search", "/v1/hardware-kb/search"} and method == "POST":
        return _search_knowledge("optimization" if "optimization" in path else "hardware", body)
    match = _KB_RECORD.fullmatch(path)
    if method == "GET" and match:
        kind, record_type, record_id = (unquote(item) for item in match.groups())
        _payload, rows = _knowledge_records(kind)
        for item in rows:
            if item["record_type"] == record_type and item["record_id"] == record_id:
                return item
        raise PlatformError("KB_RECORD_NOT_FOUND", "KB record does not exist.", status_code=404)

    if method == "GET" and path == "/v1/desktop-opportunity/traces":
        return _opportunity_trace_catalog(store, query)
    match = _OPPORTUNITY_TRACE.fullmatch(path)
    if method == "GET" and match:
        return _opportunity_trace_detail(
            store,
            unquote(match.group(1)),
            state_root=state_root,
            connectors=connectors,
        )
    if method == "POST" and path == "/v1/desktop-opportunity/source-match":
        return _resolve_opportunity_source(store, connectors, state_root, body)
    if method == "POST" and path == "/v1/desktop-opportunity/manual-code":
        return _save_manual_opportunity_code(store, body)
    if method == "POST" and path == "/v1/desktop-opportunity/hardware-route":
        return _route_opportunity_hardware(store, connectors, state_root, body)

    if method == "GET" and path == "/v1/code-graphs":
        manager = _code_graphs(state_root, store, connectors)
        values = manager.list_graphs(source_connector_id=_query_value(query, "source_connector_id"), limit=_bounded_int(_query_value(query, "limit", "100"), 100))
        return {"schema_version": "module_code_graph_catalog/v1", "extractor_available": manager.available(), "graphs": values, "count": len(values)}
    if method in {"POST"} and path in {"/v1/code-graphs/estimates", "/v1/code-graphs/builds"}:
        manager = _code_graphs(state_root, store, connectors)
        function = manager.request_estimate if path.endswith("estimates") else manager.start_build
        result = function(str(body.get("source_connector_id") or ""), subpath=str(body.get("subpath") or ""),
                          max_files=_bounded_int(body.get("max_files"), 50_000, 500_000), force_new=bool(body.get("force_new")))
        return _wait_for_code_graph_job(
            manager, result,
            estimate=path.endswith("estimates"), build=path.endswith("builds"),
        )
    match = _GRAPH_ACTION.fullmatch(path)
    if method == "POST" and match:
        manager = _code_graphs(state_root, store, connectors); graph_id, action = (unquote(item) for item in match.groups())
        if action == "confirm":
            return _wait_for_code_graph_job(manager, manager.confirm_build(graph_id), build=True)
        if action == "cancel": return manager.cancel_build(graph_id)
        return manager.claim_semantic_analysis(graph_id, request_id=str(body.get("request_id") or ""), force=bool(body.get("force")))
    match = _GRAPH_ITEM.fullmatch(path)
    if match:
        manager = _code_graphs(state_root, store, connectors); graph_id = unquote(match.group(1))
        if method == "GET": return {"graph": manager.get_graph(graph_id)}
        if method == "DELETE": return manager.delete_graph(graph_id)
    if method == "GET" and path == "/v1/code-knowledge-reports":
        manager = _code_graphs(state_root, store, connectors)
        rows = manager.list_reports(graph_id=_query_value(query, "graph_id"), source_connector_id=_query_value(query, "source_connector_id"), limit=_bounded_int(_query_value(query, "limit", "100"), 100))
        return {"schema_version": "code_knowledge_report_catalog/v1", "reports": rows, "count": len(rows)}
    match = _REPORT_MARKDOWN.fullmatch(path)
    if method == "GET" and match:
        manager = _code_graphs(state_root, store, connectors); report_id = unquote(match.group(1))
        report = manager.read_report(report_id)
        try:
            content = manager.report_markdown_path(report_id).read_text(encoding="utf-8")
        except OSError as exc:
            raise PlatformError("CODE_KNOWLEDGE_REPORT_NOT_FOUND", "代码知识报告不存在。", status_code=404) from exc
        return _text_response(
            content, content_type="text/markdown; charset=utf-8",
            filename=f"{_safe_download_stem(report.get('title'), 'code-knowledge-report')}.md",
        )
    match = _REPORT_ITEM.fullmatch(path)
    if match:
        manager = _code_graphs(state_root, store, connectors); report_id = unquote(match.group(1))
        if method == "GET": return {"report": manager.read_report(report_id)}
        if method == "DELETE": return manager.delete_report(report_id)

    if method == "GET" and path == "/v1/application-flows": return _flow_catalog(store, query)
    if method == "GET" and path == "/v1/application-flows/preferences": return _flow_preferences_value(store)
    if method == "PUT" and path == "/v1/application-flows/preferences":
        value = {"schema_version": "application_flow_preferences/v1", "confirm_flow_before_trace": bool(body.get("confirm_flow_before_trace")), "updated_at": utc_now()}
        _flow_preferences(store).write(value); return value
    if method == "GET" and path == "/v1/application-flows/runtime-traces": return _runtime_trace_catalog(store, query)
    if method == "POST" and path == "/v1/application-flows/runtime-traces/bind":
        registry = _flow_trace_registry(store); value = registry.read(); bindings = _record(value.get("analysis_bindings"))
        analysis_id = str(body.get("application_flow_analysis_id") or "").strip()[:160]
        if not analysis_id:
            raise PlatformError("APPLICATION_FLOW_ANALYSIS_ID_REQUIRED", "绑定真实 Trace 需要应用流程分析 ID。", status_code=422)
        artifact_id = str(body.get("analysis_artifact_id") or "").strip()
        trace = _read_runtime_trace(store, artifact_id) if artifact_id else None
        if trace is not None:
            bindings[analysis_id] = {"analysis_artifact_id": artifact_id, "bound_at": utc_now()}
        else:
            bindings.pop(analysis_id, None)
        value["analysis_bindings"] = bindings; registry.write(value)
        return {
            "status": "bound" if trace is not None else "cleared",
            "application_flow_analysis_id": analysis_id,
            "trace": _runtime_trace_summary(trace) if trace is not None else None,
        }
    if method == "POST" and path == "/v1/application-flows/agent-action":
        return _desktop_application_flow_action(state_root, store, connectors, body)
    match = _FLOW_DISCOVERY.fullmatch(path)
    if method == "GET" and match:
        artifact = _ensure_flow_asset_active(store, "application_flow_discovery", unquote(match.group(1)))
        return {**artifact, "application_flow_analysis_id": _flow_analysis_id(artifact, "application_flow_discovery")}
    match = _FLOW_SCENARIO.fullmatch(path)
    if method == "GET" and match:
        artifact = _ensure_flow_asset_active(store, "application_flow_scenario", unquote(match.group(1)))
        return {**artifact, "application_flow_analysis_id": _flow_analysis_id(artifact, "application_flow_scenario")}
    match = _FLOW_SCENARIO_EXPORT.fullmatch(path)
    if method == "GET" and match:
        artifact_id, export_kind = unquote(match.group(1)), match.group(2)
        artifact = _ensure_flow_asset_active(store, "application_flow_scenario", artifact_id)
        scenario = _record(artifact.get("scenario_json")); stem = _safe_download_stem(scenario.get("scenario"), "application-flow")
        if export_kind == "json":
            return _text_response(
                json.dumps(scenario, ensure_ascii=False, indent=2) + "\n",
                content_type="application/json", filename=f"{stem}.json",
            )
        if export_kind == "mermaid":
            return _text_response(
                str(artifact.get("mermaid") or "") + "\n",
                content_type="text/plain; charset=utf-8", filename=f"{stem}.mermaid",
            )
        stage_analysis = _record(artifact.get("stage_analysis"))
        if not stage_analysis:
            raise PlatformError(
                "APPLICATION_FLOW_STAGE_ANALYSIS_NOT_FOUND",
                "该流程版本尚未生成真实 Trace 阶段分析。",
                status_code=404,
            )
        return _text_response(
            json.dumps(stage_analysis, ensure_ascii=False, indent=2) + "\n",
            content_type="application/json", filename=f"{stem}-stage-analysis.json",
        )
    match = _FLOW_TRACE.fullmatch(path)
    if method == "GET" and match:
        return {"trace": _runtime_trace_summary(_read_runtime_trace(store, unquote(match.group(1))))}
    if method == "DELETE" and match:
        artifact_id = unquote(match.group(1)); trace = _read_runtime_trace(store, artifact_id); deleted_at = utc_now()
        registry = _flow_trace_registry(store); value = registry.read(); deleted = _record(value.get("deleted_traces"))
        deleted[artifact_id] = {"deleted_at": deleted_at, "trace_name": trace.get("trace_name")}; value["deleted_traces"] = deleted
        bindings = _record(value.get("analysis_bindings"))
        cleared = [analysis_id for analysis_id, binding in bindings.items() if _record(binding).get("analysis_artifact_id") == artifact_id]
        for analysis_id in cleared: bindings.pop(analysis_id, None)
        value["analysis_bindings"] = bindings; registry.write(value)
        store.append_event({
            "event_type": "application_flow_runtime_trace_deleted", "artifact_id": artifact_id,
            "trace_name": trace.get("trace_name"), "cleared_analysis_binding_count": len(cleared), "created_at": deleted_at,
        })
        return {
            "status": "deleted", "analysis_artifact_id": artifact_id, "trace_name": trace.get("trace_name"),
            "deleted_at": deleted_at, "cleared_analysis_binding_count": len(cleared),
            "retention": "immutable_artifacts_preserved",
        }
    match = _FLOW_DELETE.fullmatch(path)
    if method == "DELETE" and match:
        artifact_type, artifact_id = match.groups()
        normalized_type = {
            "discoveries": "application_flow_discovery", "scenarios": "application_flow_scenario",
        }.get(artifact_type, artifact_type)
        cascade = _query_value(query, "cascade").casefold() in {"1", "true", "yes", "on"}
        return _delete_flow_asset(store, normalized_type, unquote(artifact_id), cascade=cascade)

    if method == "GET" and path == "/v1/page-actions":
        from backend.skills import action_catalog, skill_catalog
        return {"actions": action_catalog(), "skills": skill_catalog()}
    if method == "GET" and path == "/v1/skill-version-library":
        from backend.skill_versions import skill_version_library
        return skill_version_library()
    if method == "GET" and path == "/v1/user-skill-library":
        from backend.skill_versions import skill_management_catalog
        return skill_management_catalog(_query_value(query, "profile_id") or None)
    match = _SKILL_DETAIL.fullmatch(path)
    if method == "GET" and match:
        from backend.skill_versions import skill_management_detail
        return skill_management_detail(unquote(match.group(1)), profile_id=_query_value(query, "profile_id") or None, version_id=_query_value(query, "version_id") or None)
    if method == "POST" and path == "/v1/user-skill-library/versions":
        from backend.skill_versions import save_user_skill_version
        return save_user_skill_version(skill_id=str(body.get("skill_id") or ""), label_zh=str(body.get("label_zh") or ""),
            description_zh=str(body.get("description_zh") or ""), content=str(body.get("content") or ""), profile_id=str(body.get("profile_id") or "") or None,
            prompt_slot=str(body.get("prompt_slot") or ""), change_summary_zh=str(body.get("change_summary_zh") or ""), created_by="desktop_ui")
    match = _SKILL_ACTIVATE.fullmatch(path)
    if method == "POST" and match:
        from backend.skill_versions import activate_user_skill_version
        return activate_user_skill_version(unquote(match.group(1)), str(body.get("version_id") or "") or None, profile_id=str(body.get("profile_id") or "") or None)

    if path.startswith("/v1/experience-evolution"):
        manager = _experience(state_root, store)
        if method == "GET" and path == "/v1/experience-evolution/sources": return manager.source_catalog(limit=_bounded_int(_query_value(query, "limit", "100"), 100, 300))
        if method == "GET" and path == "/v1/experience-evolution": return manager.catalog()
        match = _EXPERIENCE_CASE.fullmatch(path)
        if match and method == "GET": return manager.read_case(unquote(match.group(1)))
        if match and method == "POST": return manager.triage_case(unquote(match.group(1)), status=str(body.get("status") or ""), selected_resolution_id=str(body.get("selected_resolution_id") or ""), operator_note=str(body.get("operator_note") or ""), actor="desktop_ui")

    return None
