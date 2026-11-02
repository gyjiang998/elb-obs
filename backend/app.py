"""Phase-A local API for the heterogeneous enablement workbench."""

from __future__ import annotations

import asyncio
import importlib.util
import base64
import binascii
import hashlib
import json
import logging
import os
import platform as runtime_platform
import queue
import re
import secrets
import socket
import ssl
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from copy import deepcopy
from collections import Counter
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterator, Literal
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import ProxyHandler, Request as UrlRequest, build_opener, getproxies, urlopen

from fastapi import Depends, FastAPI, File, Form, Header, Request, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from pydantic import AliasChoices, BaseModel, Field, model_validator
from starlette.requests import HTTPConnection

from .catalog import ensure_catalog
from .deployment_mode import (
    deployment_capabilities,
    request_needs_build_execution,
    request_needs_device_validation,
    require_build_execution,
    require_device_validation,
)
from .core import (
    ArtifactStore,
    JsonRegistry,
    PlatformError,
    SourceConnectorManager,
    atomic_write_json,
    sha256_file,
    sha256_json,
    utc_now,
)
from .custom_api_kb import CustomApiKnowledgeBaseManager, is_custom_routing_unit
from .application_flow import ApplicationFlowManager
from .application_flow_trace import ApplicationFlowRuntimeTraceManager
from .code_graph import CodeGraphManager
from .skills import action_catalog, registered_action, skill_catalog
from .skill_versions import (
    DEFAULT_SKILL_PROFILE_ID,
    activate_user_skill_version,
    resolve_skill_profile,
    save_user_skill_version,
    skill_management_catalog,
    skill_management_detail,
    skill_prompt,
    skill_version_library,
)
from .skill_bootstrap import (
    build_skill_bootstrap_requirement,
    merge_skill_bootstrap_requirement,
    public_skill_bootstrap,
)
from .htrace_analysis import (
    MAX_SELF_TIME_EVENT_CANDIDATES,
    analyze_htrace,
    list_htrace_cluster_events,
    list_htrace_self_time_events,
)
from .hitrace_instrumentation import describe_hitrace_event, load_hitrace_tag_catalog, locate_hitrace_instrumentation
from .trace_analysis import cluster_sys_trace, normalize_external_report, parse_json_trace
from .reasoning_lab import (
    MAX_CODE_BYTES,
    STRATEGIES,
    adapt_reasoning_result,
    build_model_input_context,
    canonical_sha256,
    compare_results,
    pasted_code_unit,
    prepare_context,
    validate_reasoning_result,
)
from .hardware_semantic_routing import (
    build_hardware_semantic_routing_context,
    validate_hardware_semantic_routing_result,
)
from .accelerator_api_retrieval import ACCELERATOR_UNITS, rank_accelerator_api_candidates
from .hardware_strategy_evaluation import (
    case_by_id,
    evaluation_code_unit,
    load_hardware_strategy_suite,
    public_evaluation_report,
    public_score_result,
    score_case_result,
    suite_public_catalog,
    summarize_case_scores,
)
from .hardware_semantic_routing_evaluation import (
    case_by_id as semantic_routing_case_by_id,
    load_semantic_routing_suite,
    prepare_case_context as prepare_semantic_routing_case_context,
    public_score as public_semantic_routing_score,
    public_suite_catalog as semantic_routing_suite_public_catalog,
    score_case_result as score_semantic_routing_case_result,
    summarize_case_scores as summarize_semantic_routing_case_scores,
)
from .npu_api_retrieval import rank_npu_operation_candidates
from .device_verification import (
    build_validation_plan,
    discover_devices,
    execute_remote_device_shell,
    execute_validation_plan,
    probe_device,
)
from .device_experiment import (
    EXPERIMENT_PROFILES,
    MAX_EXPERIMENT_SOURCE_BYTES,
    MAX_EXPERIMENT_TIMEOUT_SECONDS,
    build_and_upload_device_experiment as build_and_upload_device_experiment_payload,
    experiment_profile_catalog,
    stage_device_experiment_source as stage_device_experiment_source_payload,
)
from .device_dispatch import (
    build_device_dispatch_context,
    validate_device_dispatch_assessment,
)
from .dpvs_analysis import create_dpvs_router

from .device_adapter import (
    build_device_adapter_context,
    materialize_device_adapter,
    run_generated_device_adapter,
    validate_device_adapter_spec,
)
from .analysis_observability import AnalysisTraceStore
from .analysis_execution import AnalysisWorkspaceStore, build_analysis_execution_view
from .experience_evolution import ExperienceEvolutionManager
from .experimental_flow import ExperimentalFlowManager
from .parallel_workbench import ParallelWorkbench, create_parallel_router
from .trace_library import TraceLibrary, create_trace_library_router
from .parallel_modeling.calibration import create_calibration_router
from .parallel_modeling.workload_features import create_workload_features_router
from .agent_runtime import (
    AgentRunStore,
    AgentRuntime,
    CredentialLeaseStore,
    ModelGatewayError,
    ModelLease,
)
from .build_worker import BuildWorker
from .device_gateway import HTTP_POLL_PROTOCOL_VERSION, WEBSOCKET_PROTOCOL_VERSION, DeviceGateway
from .runtime_action_router import LEGACY_BROWSER_DEVICE_ACTIONS, RUNTIME_ACTIONS, PlatformRuntimeActionRouter
from .runtime_assets import OpenHarmonySdkAssetManager
from .agent_engine import InternalToolTokenManager
from .agent_cli_adapters import (
    AgentCliAdapter,
    AgentCliAdapterError,
    AgentCliAdapterRegistry,
    AgentCliLaunchSpec,
)
from .kimi_agent_runtime import KimiEngineAgentRuntime
from .kimi_server_engine import KimiServerAgentEngine, KimiServerError, KimiServerSettings
from .kimi_cli_terminal import (
    AgentCliTerminalError,
    AgentCliTerminalManager,
    AgentCliTerminalSession,
    EmptyCliUsageLedger,
    JsonlCliUsageLedger,
    KimiCliUsageLedger,
)
from .kimi_completion import KimiCompletionAdapter
from .analysis_agent_session import AnalysisAgentSessionRegistry, normalize_analysis_id
from .platform_paths import (
    ANALYSIS_TOOLS_ROOT,
    APP_ROOT,
    DATA_ROOT,
    HARDWARE_KB_ROOT,
    PATTERN_KB_ROOT,
    default_source_root,
    openharmony_sdk_root,
    optional_environment_path,
    state_root,
)
from .platform_version import platform_version as platform_version_payload

logger = logging.getLogger("heterogeneous-enable-web")

try:
    AGENT_CLI_RESUME_STARTUP_PROBE_SECONDS = max(
        0.25,
        min(float(os.environ.get("HETERO_AGENT_CLI_RESUME_PROBE_SECONDS", "1.25")), 5.0),
    )
except ValueError:
    AGENT_CLI_RESUME_STARTUP_PROBE_SECONDS = 1.25


# ``WORKSPACE_ROOT`` is retained as an internal compatibility name for the
# platform repository. Product source trees are Source Connectors and are not
# inferred from this repository's parent directory.
WORKSPACE_ROOT = APP_ROOT
STATE_ROOT = state_root()
RAW_INPUT_ROOT = STATE_ROOT / "raw-inputs"
CASE_CATALOG_PATH = DATA_ROOT / "knowledge_case_catalog.json"
LEGACY_PATTERN_KB_ROOT = PATTERN_KB_ROOT
OPTIMIZATION_KB_PATH = DATA_ROOT / "knowledge-bases" / "optimization" / "optimization_knowledge_base.json"
HARDWARE_KB_PATH = HARDWARE_KB_ROOT / "hardware_knowledge_base.json"
NPU_API_KB_PATH = HARDWARE_KB_ROOT / "npu_api_knowledge_base.json"
ACCELERATOR_API_KB_PATHS = {
    "gpu": HARDWARE_KB_ROOT / "gpu_api_knowledge_base.json",
    "dsp": HARDWARE_KB_ROOT / "dsp_api_knowledge_base.json",
    "media": HARDWARE_KB_ROOT / "media_accelerator_api_knowledge_base.json",
}
ACCELERATOR_API_MANIFEST_PATH = HARDWARE_KB_ROOT / "accelerator_api_knowledge_manifest.json"
TOOLS_ROOT = ANALYSIS_TOOLS_ROOT
PROJECT_ID = "local-default"


def _record(value: Any) -> dict[str, Any]:
    """Return an object-shaped value without trusting provider/registry input."""

    return value if isinstance(value, dict) else {}


def _text(value: Any, limit: int = 1024) -> str:
    """Normalize untrusted textual values before they enter a runtime record."""

    return value.strip()[:limit] if isinstance(value, str) else ""


def _positive_env_int(name: str, default: int) -> int:
    """Keep deployment size limits configurable without accepting invalid values."""

    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError:
        return default
    return value if value > 0 else default


# Browser clients now use cursor polling. Keep the legacy SSE endpoints
# finite so an older open tab can reconnect without holding API shutdown open.
COMPATIBILITY_SSE_MAX_SECONDS = min(
    _positive_env_int("HETERO_COMPATIBILITY_SSE_MAX_SECONDS", 5),
    60,
)
# Current browsers send non-blocking cursor polls. This compatibility cap is
# deliberately sub-second so an older tab that still asks for a 10-second poll
# cannot occupy all same-origin browser connections and starve a newly loaded
# page. The cursor contract makes these short responses cheap and lossless.
ASYNC_POLL_MAX_HOLD_SECONDS = min(
    _positive_env_int("HETERO_ASYNC_POLL_MAX_HOLD_MILLISECONDS", 250),
    1_000,
) / 1000.0


# Raw htrace captures are routinely larger than report JSON.  The upload remains
# streamed and conversion separately performs a derived-output disk preflight.
MAX_UPLOAD_BYTES = _positive_env_int("HETERO_MAX_TRACE_UPLOAD_BYTES", 2 * 1024 * 1024 * 1024)
WORKBENCH_URL = os.environ.get("HETERO_WORKBENCH_URL", "http://localhost:4032").strip().rstrip("/") or "http://localhost:4032"
# Structured opportunity reports can legitimately take longer than a short chat
# completion.  The browser waits a little longer than this value so it receives
# the relay's explicit timeout diagnostic rather than a generic fetch failure.
MODEL_RELAY_TIMEOUT_SECONDS = _positive_env_int("HETERO_MODEL_RELAY_TIMEOUT_SECONDS", 240)
# A streamed request should receive its first/next upstream bytes much sooner
# than a complete structured report.  This is an *idle* timeout, not a total
# generation timeout: each SSE chunk keeps the browser-side strategy alive.
MODEL_RELAY_STREAM_IDLE_TIMEOUT_SECONDS = _positive_env_int("HETERO_MODEL_RELAY_STREAM_IDLE_TIMEOUT_SECONDS", 90)
MODEL_RELAY_MIN_RESPONSE_BYTES = 2 * 1024 * 1024
MODEL_RELAY_MAX_RESPONSE_BYTES = max(
    MODEL_RELAY_MIN_RESPONSE_BYTES,
    _positive_env_int("HETERO_MODEL_RELAY_MAX_RESPONSE_BYTES", 32 * 1024 * 1024),
)
MODEL_RELAY_STREAM_BYTES_PER_OUTPUT_TOKEN = _positive_env_int(
    "HETERO_MODEL_RELAY_STREAM_BYTES_PER_OUTPUT_TOKEN",
    1024,
)
MODEL_RELAY_MAX_OUTPUT_TOKENS = min(_positive_env_int("HETERO_MODEL_RELAY_MAX_OUTPUT_TOKENS", 64 * 1024), 128 * 1024)
MODEL_RELAY_CONNECT_ATTEMPTS = min(_positive_env_int("HETERO_MODEL_RELAY_CONNECT_ATTEMPTS", 2), 4)
MAX_MODEL_TRANSCRIPT_TEXT_CHARS = 512 * 1024
MAX_AUTO_HOTSPOTS_PER_RUN = 12
MAX_CLUSTER_EVENT_CANDIDATES = 24
MAX_EVENT_SOURCE_CANDIDATES = 12
EVENT_SOURCE_SEARCH_TIMEOUT_SECONDS = 12
MAX_KB_CPU_SOURCE_LINES = 420
MAX_KB_CPU_SOURCE_BYTES = 96 * 1024
SOURCE_SEARCH_GLOBS = ("*.c", "*.cc", "*.cpp", "*.cxx", "*.h", "*.hh", "*.hpp", "*.hxx")
AST_ELIGIBLE_SOURCE_RESOLUTION_STATES = frozenset(
    {
        "declared_source",
        "instrumentation_point_exact",
        "instrumentation_point_function_derived",
    }
)
TRACE_ANALYSIS_ENVELOPE_FIELDS = frozenset(
    {
        "artifact_id",
        "artifact_type",
        "content_sha256",
        "created_at",
        "input_artifacts",
        "producer",
        "project_id",
        "schema_version",
        "source_binding",
        "source_connector_id",
    }
)

# The local relay exists because some coding-model endpoints intentionally do
# not grant browser CORS access.  Keep the endpoint set explicit: accepting an
# arbitrary URL here would turn a localhost API into an SSRF primitive.
MODEL_RELAY_ALLOWED_BASE_URLS = frozenset(
    {
        "https://api.kimi.com/coding/v1",
        "https://api.moonshot.cn/v1",
    }
)

# Trace discovery methods are trace-wide candidate generators.  Cluster event
# extraction remains a separate, time-window-scoped path.  New methods belong in
# this registry so their source-resolution and evidence behavior stays uniform.
HOTSPOT_DISCOVERY_PROFILES: dict[str, dict[str, Any]] = {
    "event_self_time": {
        "scope": "trace_wide",
        "input_kind": "raw_htrace",
        "profile_id": "htrace-event-self-time-v2",
        "max_events": MAX_SELF_TIME_EVENT_CANDIDATES,
    },
}

TRACE_FIXTURE_ROOT = optional_environment_path("HETERO_TRACE_FIXTURE_ROOT")
FIXTURES = {} if TRACE_FIXTURE_ROOT is None else {
    "captured-raw-htrace": {
        "path": TRACE_FIXTURE_ROOT / "captured" / "hitrace_capture_20260708.htrace",
        "kind": "raw_htrace",
        "label": "Captured raw .htrace",
        "description": "OHOSPROF raw trace analyzed through the local TraceStreamer adapter.",
    },
    "captured-report-json": {
        "path": TRACE_FIXTURE_ROOT / "captured" / "output_existing_db" / "report.json",
        "kind": "external_report_json",
        "label": "Captured trace analysis report",
        "description": "Existing report.json produced from the captured trace database.",
    },
    "direct-report-json": {
        "path": TRACE_FIXTURE_ROOT / "direct_analysis" / "report.json",
        "kind": "external_report_json",
        "label": "Direct analysis report",
        "description": "Existing direct-analysis report fixture.",
    },
    "hitrace-text-sys": {
        "path": TRACE_FIXTURE_ROOT / "captured" / "hitrace_text.sys",
        "kind": "sys_trace_experimental",
        "label": "HiTrace textual scheduler trace",
        "description": "Real .sys fixture for the bounded wakeup-chain Spike.",
    },
}

store = ArtifactStore(STATE_ROOT, project_id=PROJECT_ID)
analysis_trace_store = AnalysisTraceStore(STATE_ROOT / "analysis-traces")
analysis_workspace_store = AnalysisWorkspaceStore(STATE_ROOT / "analysis-workspaces")
connectors = SourceConnectorManager(STATE_ROOT, store, workspace_root=WORKSPACE_ROOT)
openharmony_sdk_assets = OpenHarmonySdkAssetManager(
    STATE_ROOT,
    connectors,
    sdk_root=openharmony_sdk_root(),
)
custom_api_kbs = CustomApiKnowledgeBaseManager(STATE_ROOT, connectors)
code_graphs = CodeGraphManager(STATE_ROOT, connectors, store)
application_flows = ApplicationFlowManager(
    connectors,
    store,
    scenario_schema_path=APP_ROOT / "agent-resources" / "skills" / "application-execution-flow-reconstruction" / "references" / "application-flow-scenario-v2.schema.json",
    discovery_schema_path=APP_ROOT / "agent-resources" / "skills" / "application-execution-flow-reconstruction" / "references" / "application-flow-discovery-v1.schema.json",
    stage_analysis_schema_path=APP_ROOT / "agent-resources" / "skills" / "application-execution-flow-reconstruction" / "references" / "application-flow-stage-analysis-v1.schema.json",
)
application_flow_runtime_traces = ApplicationFlowRuntimeTraceManager(STATE_ROOT, store, application_flows)
experimental_flows = ExperimentalFlowManager(STATE_ROOT, connectors, store)


class LocalConnectorRequest(BaseModel):
    source_root: str
    display_name: str = ""


class OpenHarmonySdkProvisionRequest(BaseModel):
    source_connector_id: str = Field(min_length=1, max_length=160)


class GitClonePlanRequest(BaseModel):
    url: str
    display_name: str = ""
    source_connector_id: str | None = None


class CloneRequest(BaseModel):
    plan_id: str


class CustomApiKnowledgeBaseBuildRequest(BaseModel):
    unit_name: str = Field(min_length=1, max_length=80)
    source_connector_id: str = Field(default="", max_length=160)
    source_root: str = Field(default="", max_length=4096)


class UserSkillVersionCreateRequest(BaseModel):
    skill_id: str = Field(pattern=r"^[a-z][a-z0-9-]{1,63}$")
    label_zh: str = Field(min_length=1, max_length=120)
    description_zh: str = Field(default="", max_length=800)
    content: str = Field(min_length=1, max_length=64 * 1024)
    prompt_slot: str = Field(default="", max_length=80)
    change_summary_zh: str = Field(default="", max_length=800)
    profile_id: str = Field(default=DEFAULT_SKILL_PROFILE_ID, min_length=1, max_length=120)


class UserSkillActivationRequest(BaseModel):
    version_id: str | None = Field(default=None, max_length=80)
    profile_id: str = Field(default=DEFAULT_SKILL_PROFILE_ID, min_length=1, max_length=120)


class ApplicationFlowRepositoryInspectRequest(BaseModel):
    source_connector_id: str = Field(min_length=1, max_length=160)
    subpath: str = Field(default="", max_length=2048)


class CodeGraphBuildRequest(BaseModel):
    source_connector_id: str = Field(min_length=1, max_length=160)
    subpath: str = Field(default="", max_length=2048)
    max_files: int = Field(default=50_000, ge=1, le=500_000)
    force_new: bool = False


class CodeKnowledgeAgentAnalysisRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)
    force: bool = False


class ApplicationFlowSourceSearchRequest(BaseModel):
    source_connector_id: str = Field(min_length=1, max_length=160)
    query: str = Field(min_length=1, max_length=240)
    subpath: str = Field(default="", max_length=2048)
    max_results: int = Field(default=30, ge=1, le=80)


class ApplicationFlowSourceReadRequest(BaseModel):
    source_connector_id: str = Field(min_length=1, max_length=160)
    path: str = Field(min_length=1, max_length=2048)
    line_start: int = Field(default=1, ge=1)
    line_end: int = Field(default=240, ge=1)


class ApplicationFlowDiscoverySaveRequest(BaseModel):
    source_connector_id: str = Field(min_length=1, max_length=160)
    subpath: str = Field(default="", max_length=2048)
    discovery: dict[str, Any]


class ApplicationFlowScenarioSaveRequest(BaseModel):
    source_connector_id: str = Field(min_length=1, max_length=160)
    discovery_artifact_id: str = Field(pattern=r"^art_[a-f0-9]{24}$")
    flow_id: str = Field(min_length=2, max_length=80)
    scenario_json: dict[str, Any]
    mermaid: str = Field(min_length=1, max_length=256 * 1024)
    stage_evidence: list[dict[str, Any]] = Field(min_length=1, max_length=128)
    unknowns: list[str] = Field(default_factory=list, max_length=64)
    parent_scenario_artifact_id: str | None = Field(default=None, pattern=r"^art_[a-f0-9]{24}$")
    revision_request: str = Field(default="", max_length=4000)


class ApplicationFlowMermaidCompileRequest(BaseModel):
    source_connector_id: str = Field(min_length=1, max_length=160)
    discovery_artifact_id: str = Field(pattern=r"^art_[a-f0-9]{24}$")
    flow_id: str = Field(min_length=2, max_length=80)
    mermaid: str = Field(min_length=1, max_length=256 * 1024)
    parent_scenario_artifact_id: str | None = Field(default=None, pattern=r"^art_[a-f0-9]{24}$")
    revision_request: str = Field(default="", max_length=4000)


class ApplicationFlowTraceBoundaryRequest(BaseModel):
    event_name: str = Field(min_length=1, max_length=1000)
    path: str = Field(min_length=1, max_length=2048)
    line: int = Field(ge=1)
    macro: str = Field(min_length=1, max_length=120)
    symbol: str = Field(min_length=1, max_length=1000)
    event_label_match: Literal["exact_literal", "wrapper_literal", "function_derived", "static_prefix"] = "exact_literal"
    build_condition: str = Field(default="not_observed_in_local_window", min_length=1, max_length=1000)
    kind: str = Field(default="hitrace_literal", min_length=1, max_length=1000)


class ApplicationFlowStageTraceResultRequest(BaseModel):
    stage_id: str = Field(min_length=2, max_length=80)
    start: ApplicationFlowTraceBoundaryRequest | None = None
    end: ApplicationFlowTraceBoundaryRequest | None = None
    notes: str = Field(default="", max_length=2000)


class ApplicationFlowTraceAnchorEnrichmentRequest(BaseModel):
    source_connector_id: str = Field(min_length=1, max_length=160)
    discovery_artifact_id: str = Field(pattern=r"^art_[a-f0-9]{24}$")
    scenario_artifact_id: str = Field(pattern=r"^art_[a-f0-9]{24}$")
    flow_id: str = Field(min_length=2, max_length=80)
    stage_results: list[ApplicationFlowStageTraceResultRequest] = Field(default_factory=list, max_length=128)


class ApplicationFlowRuntimeTraceBindingRequest(BaseModel):
    application_flow_analysis_id: str = Field(min_length=1, max_length=160)
    analysis_artifact_id: str | None = Field(default=None, pattern=r"^art_[a-f0-9]{24}$")


class ApplicationFlowPreferencesRequest(BaseModel):
    confirm_flow_before_trace: bool = True


class FixtureImportRequest(BaseModel):
    source_connector_id: str | None = None
    declared_revision: str | None = None


class JsonTraceImportRequest(BaseModel):
    trace_result: dict[str, Any]
    source_connector_id: str | None = None
    declared_revision: str | None = None
    trace_name: str = "uploaded-trace-result.json"


class TraceSourceSnapshotAttestationRequest(BaseModel):
    analysis_artifact_id: str
    source_connector_id: str
    source_snapshot_id: str = Field(min_length=1, max_length=256)
    attestation_actor: str = "user"
    attestation_note: str = Field(default="", max_length=1000)


class ClusterSelectionRequest(BaseModel):
    analysis_artifact_id: str
    cluster_id: str
    rationale: str = Field(min_length=3, max_length=1000)
    selection_actor: str = "user"
    action_id: str = "cluster.select"
    automation_policy_id: str | None = Field(default=None, max_length=160)
    automation_policy_version: str | None = Field(default=None, max_length=80)
    automation_input_hash: str | None = Field(default=None, max_length=128)
    exclusion_reasons: list[str] = Field(default_factory=list, max_length=64)


class ClusterEventListRequest(BaseModel):
    analysis_artifact_id: str
    cluster_id: str
    limit: int = Field(default=MAX_CLUSTER_EVENT_CANDIDATES, ge=1, le=MAX_CLUSTER_EVENT_CANDIDATES)


class TraceEventSourceRequest(BaseModel):
    analysis_artifact_id: str
    cluster_id: str
    event_id: str


class TraceHotspotDiscoveryRequest(BaseModel):
    analysis_artifact_id: str
    discovery_id: str = Field(default="event_self_time", min_length=1, max_length=120)
    limit: int = Field(default=MAX_SELF_TIME_EVENT_CANDIDATES, ge=1, le=MAX_SELF_TIME_EVENT_CANDIDATES)
    module_key: str | None = Field(default=None, max_length=80)


class TraceHotspotDiscoverySourceRequest(BaseModel):
    analysis_artifact_id: str
    discovery_id: str = Field(default="event_self_time", min_length=1, max_length=120)
    event_id: str
    module_key: str | None = Field(default=None, max_length=80)


class AnalysisRunRequest(BaseModel):
    analysis_artifact_id: str
    cluster_selection_artifact_id: str
    kb_version: str = "local-development"
    include_unverified: bool = True


class EventOpportunityAnalysisRequest(BaseModel):
    analysis_artifact_id: str
    source_resolution_artifact_id: str
    include_unverified: bool = True


class KBSearchRequest(BaseModel):
    query: str = ""
    include_unverified: bool = True
    include_cases: bool = True
    knowledge_base: str = Field(default="optimization", pattern="^(optimization|hardware)$")


class NpuApiSearchRequest(BaseModel):
    code: str = Field(min_length=1, max_length=16 * 1024)
    function_hint: str = Field(default="", max_length=300)
    event_name: str = Field(default="", max_length=300)
    limit: int = Field(default=5, ge=1, le=10)


class AcceleratorApiSearchRequest(BaseModel):
    code: str = Field(min_length=1, max_length=16 * 1024)
    function_hint: str = Field(default="", max_length=300)
    event_name: str = Field(default="", max_length=300)
    limit: int = Field(default=3, ge=1, le=5)


class SourceInspectionRequest(BaseModel):
    source_connector_id: str
    path: str
    line: int = Field(default=1, ge=1)


class WorkspaceFileReadRequest(BaseModel):
    source_connector_id: str = Field(min_length=1, max_length=160)
    path: str = Field(min_length=1, max_length=1024)
    max_bytes: int = Field(default=96 * 1024, ge=1, le=128 * 1024)


class WorkspaceFileWriteRequest(BaseModel):
    source_connector_id: str = Field(min_length=1, max_length=160)
    path: str = Field(min_length=1, max_length=1024)
    content: str = Field(max_length=256 * 1024)
    expected_sha256: str | None = Field(default=None, min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    execution_policy: Literal["full_access", "confirm_device_execution"] = "confirm_device_execution"


class AgentAuditRequest(BaseModel):
    action_id: str
    conversation_id: str = Field(min_length=1, max_length=200)
    source_connector_id: str
    input_sha256: str = Field(min_length=16, max_length=128)
    output_sha256: str = Field(min_length=16, max_length=128)
    result: str = Field(pattern="^(succeeded|failed|cancelled)$")


class HotspotCodePasteRequest(BaseModel):
    code: str = Field(min_length=1, max_length=16 * 1024)
    function_hint: str = Field(default="", max_length=300)
    event_name: str = Field(default="", max_length=300)
    source_connector_id: str | None = None


class ReasoningTraceHotspotRequest(BaseModel):
    analysis_artifact_id: str
    limit: int = Field(default=100, ge=1, le=MAX_SELF_TIME_EVENT_CANDIDATES)
    module_key: str | None = Field(default=None, max_length=80)
    group_by_module: bool = False
    per_module_limit: int = Field(default=6, ge=1, le=12)


class ReasoningTraceHotspotResolveRequest(BaseModel):
    analysis_artifact_id: str
    event_id: str = Field(min_length=1, max_length=160)
    module_key: str | None = Field(default=None, max_length=80)


class HotspotCodeFromResolutionRequest(BaseModel):
    source_resolution_artifact_id: str


class HotspotCallPathEvidence(BaseModel):
    caller_symbol: str = Field(min_length=1, max_length=300)
    callee_symbol: str = Field(min_length=1, max_length=300)
    path: str = Field(min_length=1, max_length=1000)
    line: int = Field(ge=1, le=10_000_000)
    evidence: str = Field(default="source_call_site", max_length=300)


class HotspotInvestigatedSourceRequest(BaseModel):
    source_resolution_artifact_id: str
    path: str = Field(min_length=1, max_length=1000)
    line_start: int = Field(ge=1, le=10_000_000)
    line_end: int = Field(ge=1, le=10_000_000)
    function_hint: str = Field(min_length=1, max_length=300)
    candidate_kind: str = Field(pattern="^(local_region|callee_function|transitive_callee|caller_region|function_pointer_target)$")
    relation_to_anchor: str = Field(min_length=3, max_length=1000)
    rationale: str = Field(min_length=3, max_length=2000)
    expected_source_sha256: str | None = Field(default=None, min_length=64, max_length=64)
    call_path: list[HotspotCallPathEvidence] = Field(default_factory=list, max_length=32)


def _default_target_platform_profile() -> dict[str, Any]:
    """Return the conservative default selected by the local workbench.

    This is an analysis scope supplied by the user, not a live capability
    probe.  It intentionally carries no claim about accelerator availability.
    """

    return {
        "schema_version": "target_platform_profile/v1",
        "profile_source": "user_selected",
        "capability_status": "not_device_probe",
        "chipset": {
            "family_id": "kirin",
            "family_label": "麒麟 SoC",
            "model_id": "kirin_unspecified",
            "model_label": "麒麟 SoC（型号待确认）",
        },
        "operating_system": {
            "family_id": "harmonyos",
            "family_label": "鸿蒙操作系统",
            "version_id": "harmonyos_unspecified",
            "version_label": "鸿蒙操作系统（版本待确认）",
        },
    }


class TargetPlatformComponent(BaseModel):
    family_id: str = Field(min_length=1, max_length=80, pattern=r"^[a-z0-9_]+$")
    family_label: str = Field(min_length=1, max_length=120)
    model_id: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"^[a-z0-9_]+$")
    model_label: str | None = Field(default=None, min_length=1, max_length=160)
    version_id: str | None = Field(default=None, min_length=1, max_length=80, pattern=r"^[a-z0-9_]+$")
    version_label: str | None = Field(default=None, min_length=1, max_length=160)


class TargetPlatformProfileRequest(BaseModel):
    schema_version: Literal["target_platform_profile/v1"] = "target_platform_profile/v1"
    profile_source: Literal["user_selected"] = "user_selected"
    capability_status: Literal["not_device_probe"] = "not_device_probe"
    chipset: TargetPlatformComponent
    operating_system: TargetPlatformComponent

    @model_validator(mode="after")
    def require_concrete_profile_selection(self) -> "TargetPlatformProfileRequest":
        if not self.chipset.model_id or not self.chipset.model_label:
            raise ValueError("target chipset must include a selected model identifier and label")
        if not self.operating_system.version_id or not self.operating_system.version_label:
            raise ValueError("target operating system must include a selected version identifier and label")
        return self


class ReasoningContextPrepareRequest(BaseModel):
    code_artifact_id: str
    strategy_id: str = Field(pattern="^(kb_rag|hardware_kb|direct_baseline)$")
    hardware_semantic_routing_result_artifact_id: str | None = Field(default=None, min_length=1, max_length=120)
    skill_profile_id: str = Field(default=DEFAULT_SKILL_PROFILE_ID, min_length=1, max_length=120)
    target_platform_profile: TargetPlatformProfileRequest = Field(default_factory=lambda: TargetPlatformProfileRequest.model_validate(_default_target_platform_profile()))


class HardwareSemanticRoutingPrepareRequest(BaseModel):
    code_artifact_id: str
    skill_profile_id: str = Field(default=DEFAULT_SKILL_PROFILE_ID, min_length=1, max_length=120)
    target_platform_profile: TargetPlatformProfileRequest = Field(default_factory=lambda: TargetPlatformProfileRequest.model_validate(_default_target_platform_profile()))


class HardwareSemanticRoutingResultRequest(BaseModel):
    context_artifact_id: str
    model_provider: str = Field(default="browser_configured_local_relay", max_length=120)
    model_name: str = Field(default="", max_length=240)
    raw_output: str = Field(default="", max_length=32 * 1024)
    result: dict[str, Any]


ROUTING_UNIT_ID_PATTERN = r"^(?:npu|gpu|dsp|media|custom:custom_api_kb_[a-f0-9]{24})$"


class HardwareSemanticRoutingManualSelectionRequest(BaseModel):
    result_artifact_id: str = Field(min_length=1, max_length=120)
    unit_id: str = Field(min_length=1, max_length=64, pattern=ROUTING_UNIT_ID_PATTERN)
    note: str = Field(default="", max_length=600)


class HardwareSemanticRoutingDirectSelectionRequest(BaseModel):
    code_artifact_id: str = Field(min_length=1, max_length=120)
    unit_id: str = Field(min_length=1, max_length=64, pattern=ROUTING_UNIT_ID_PATTERN)
    note: str = Field(default="", max_length=600)
    target_platform_profile: TargetPlatformProfileRequest = Field(default_factory=lambda: TargetPlatformProfileRequest.model_validate(_default_target_platform_profile()))


class ManualOpportunitySelectionRequest(BaseModel):
    code_artifact_id: str = Field(min_length=1, max_length=120)
    selection_kind: Literal["hardware_api", "parallel_cpu"]
    unit_id: str | None = Field(default=None, min_length=1, max_length=64, pattern=ROUTING_UNIT_ID_PATTERN)
    api_record_id: str | None = Field(default=None, min_length=1, max_length=300)
    hardware_semantic_routing_result_artifact_id: str | None = Field(default=None, min_length=1, max_length=120)
    note: str = Field(default="", max_length=600)
    target_platform_profile: TargetPlatformProfileRequest = Field(default_factory=lambda: TargetPlatformProfileRequest.model_validate(_default_target_platform_profile()))

    @model_validator(mode="after")
    def validate_selection_contract(self) -> "ManualOpportunitySelectionRequest":
        if self.selection_kind == "hardware_api":
            if not self.unit_id or not self.api_record_id or not self.hardware_semantic_routing_result_artifact_id:
                raise ValueError("hardware_api selection requires unit_id, api_record_id and hardware semantic route")
        elif self.unit_id or self.api_record_id or self.hardware_semantic_routing_result_artifact_id:
            raise ValueError("parallel_cpu selection must not include hardware route or API fields")
        return self


class HardwareStrategyEvaluationPrepareRequest(BaseModel):
    target_platform_profile: TargetPlatformProfileRequest = Field(default_factory=lambda: TargetPlatformProfileRequest.model_validate(_default_target_platform_profile()))


class HardwareStrategyEvaluationScoreRequest(BaseModel):
    context_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    result: dict[str, Any]
    target_platform_profile: TargetPlatformProfileRequest = Field(default_factory=lambda: TargetPlatformProfileRequest.model_validate(_default_target_platform_profile()))


class HardwareStrategyEvaluationReportCaseRequest(BaseModel):
    case_id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9_]+$")
    context_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    result: dict[str, Any]


class HardwareStrategyEvaluationReportRequest(BaseModel):
    results: list[HardwareStrategyEvaluationReportCaseRequest] = Field(min_length=1, max_length=72)
    model_name: str = Field(default="", max_length=240)
    target_platform_profile: TargetPlatformProfileRequest = Field(default_factory=lambda: TargetPlatformProfileRequest.model_validate(_default_target_platform_profile()))


class HardwareSemanticRoutingEvaluationPrepareRequest(BaseModel):
    target_platform_profile: TargetPlatformProfileRequest = Field(default_factory=lambda: TargetPlatformProfileRequest.model_validate(_default_target_platform_profile()))


class HardwareSemanticRoutingEvaluationScoreRequest(BaseModel):
    context_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    result: dict[str, Any]
    target_platform_profile: TargetPlatformProfileRequest = Field(default_factory=lambda: TargetPlatformProfileRequest.model_validate(_default_target_platform_profile()))


class HardwareSemanticRoutingEvaluationReportCaseRequest(BaseModel):
    case_id: str = Field(min_length=1, max_length=120, pattern=r"^[a-z0-9_]+$")
    context_sha256: str = Field(min_length=64, max_length=64, pattern=r"^[a-f0-9]{64}$")
    result: dict[str, Any]


class HardwareSemanticRoutingEvaluationReportRequest(BaseModel):
    results: list[HardwareSemanticRoutingEvaluationReportCaseRequest] = Field(min_length=1, max_length=32)
    model_name: str = Field(default="", max_length=240)
    target_platform_profile: TargetPlatformProfileRequest = Field(default_factory=lambda: TargetPlatformProfileRequest.model_validate(_default_target_platform_profile()))


class ReasoningResultRequest(BaseModel):
    context_artifact_id: str
    model_provider: str = Field(default="browser_direct", max_length=120)
    model_name: str = Field(default="", max_length=240)
    raw_output: str = Field(default="", max_length=MAX_MODEL_TRANSCRIPT_TEXT_CHARS)
    result: dict[str, Any]


class BlindReasoningPrepareRequest(BaseModel):
    analysis_id: str = Field(min_length=1, max_length=160)
    code_artifact_id: str = Field(min_length=1, max_length=120)
    user_question: str = Field(
        default="请判断这段热点代码如何通过异构硬件、CPU 并行、SIMD 或算法方式优化，并尽量给出可验证的硬件 API 方向。",
        min_length=1,
        max_length=2000,
    )
    skill_profile_id: str = Field(default=DEFAULT_SKILL_PROFILE_ID, min_length=1, max_length=120)
    target_platform_profile: TargetPlatformProfileRequest = Field(default_factory=lambda: TargetPlatformProfileRequest.model_validate(_default_target_platform_profile()))


class BlindReasoningResultRequest(BaseModel):
    context_artifact_id: str = Field(min_length=1, max_length=120)
    model_provider: str = Field(default="browser_configured_local_relay", max_length=120)
    model_name: str = Field(default="", max_length=240)
    raw_output: str = Field(default="", max_length=MAX_MODEL_TRANSCRIPT_TEXT_CHARS)
    result: dict[str, Any]


class BlindFlowComparisonPrepareRequest(BaseModel):
    blind_result_artifact_id: str = Field(min_length=1, max_length=120)
    flow_result_artifact_id: str = Field(min_length=1, max_length=120)
    device_run_artifact_id: str | None = Field(default=None, min_length=1, max_length=120)
    skill_profile_id: str = Field(default=DEFAULT_SKILL_PROFILE_ID, min_length=1, max_length=120)


class BlindFlowComparisonResultRequest(BaseModel):
    context_artifact_id: str = Field(min_length=1, max_length=120)
    model_provider: str = Field(default="browser_configured_local_relay", max_length=120)
    model_name: str = Field(default="", max_length=240)
    raw_output: str = Field(default="", max_length=MAX_MODEL_TRANSCRIPT_TEXT_CHARS)
    result: dict[str, Any]


class ModelTokenUsageRequest(BaseModel):
    inputTokens: int = Field(ge=0, le=MAX_CODE_BYTES * 64)
    outputTokens: int = Field(ge=0, le=MAX_CODE_BYTES * 64)
    totalTokens: int = Field(ge=0, le=MAX_CODE_BYTES * 128)
    inputSource: Literal["provider", "estimated"]
    outputSource: Literal["provider", "estimated"]
    cachedInputTokens: int | None = Field(default=None, ge=0, le=MAX_CODE_BYTES * 64)
    cacheHitRate: float | None = Field(default=None, ge=0, le=1)


class PromptTokenBudgetRequest(BaseModel):
    contextWindowTokens: int = Field(ge=8 * 1024, le=1024 * 1024)
    inputTokens: int = Field(ge=0, le=1024 * 1024)
    outputReserveTokens: int = Field(ge=1, le=MODEL_RELAY_MAX_OUTPUT_TOKENS)
    occupiedTokens: int = Field(ge=0, le=1024 * 1024 + MODEL_RELAY_MAX_OUTPUT_TOKENS)
    remainingTokens: int = Field(ge=0, le=1024 * 1024)
    utilization: float = Field(ge=0, le=1)
    nearLimit: bool
    exceedsWindow: bool
    compactionApplied: bool
    compactedItems: int = Field(ge=0, le=4096)


class ReasoningTranscriptRequest(BaseModel):
    analysis_id: str = Field(min_length=1, max_length=160)
    inference_id: str = Field(min_length=1, max_length=160)
    run_id: str = Field(min_length=1, max_length=160)
    strategy_id: str = Field(pattern="^(kb_rag|hardware_kb|direct_baseline|blind_baseline|blind_comparison)$")
    strategy_label: str = Field(min_length=1, max_length=160)
    context_artifact_id: str
    result_artifact_id: str | None = None
    model_provider: str = Field(default="browser_direct", max_length=120)
    model_name: str = Field(default="", max_length=240)
    system_prompt: str = Field(default="", max_length=72 * 1024)
    user_prompt: str = Field(default="", max_length=96 * 1024)
    raw_output: str = Field(default="", max_length=MAX_MODEL_TRANSCRIPT_TEXT_CHARS)
    model_usage: ModelTokenUsageRequest | None = None
    token_budget: PromptTokenBudgetRequest | None = None
    outcome: dict[str, Any] = Field(default_factory=dict)
    timeline: list[dict[str, Any]] = Field(default_factory=list, max_length=16)


class ReasoningComparisonRequest(BaseModel):
    result_artifact_ids: list[str] = Field(min_length=1, max_length=3)


class DeviceProbeRequest(BaseModel):
    device_serial: str = Field(min_length=1, max_length=160, pattern=r"^[A-Za-z0-9._:-]+$")
    source_connector_id: str | None = None


class DeviceValidationPlanRequest(BaseModel):
    device_probe_artifact_id: str = Field(min_length=1, max_length=120)
    hotspot_code_artifact_id: str = Field(min_length=1, max_length=120)
    reasoning_result_artifact_id: str = Field(min_length=1, max_length=120)
    requested_unit: Literal["npu", "gpu", "dsp", "media"]
    dispatch_assessment_artifact_id: str | None = Field(default=None, min_length=1, max_length=120)


class DeviceDispatchPrepareRequest(BaseModel):
    device_probe_artifact_id: str = Field(min_length=1, max_length=120)
    hotspot_code_artifact_id: str = Field(min_length=1, max_length=120)
    reasoning_result_artifact_id: str = Field(min_length=1, max_length=120)
    requested_unit: Literal["npu", "gpu", "dsp", "media"]


class DeviceDispatchAssessmentRequest(BaseModel):
    context_artifact_id: str = Field(min_length=1, max_length=120)
    model_provider: str = Field(default="browser_configured_local_relay", min_length=1, max_length=120)
    model_name: str = Field(default="", max_length=240)
    raw_output: str = Field(default="", max_length=64 * 1024)
    attempt_index: int = Field(default=1, ge=1, le=100)
    parent_assessment_artifact_id: str | None = Field(default=None, min_length=1, max_length=120)
    agent_note: str = Field(default="", max_length=1200)
    result: dict[str, Any]


class DeviceAdapterContextPrepareRequest(BaseModel):
    device_probe_artifact_id: str = Field(min_length=1, max_length=120)
    hotspot_code_artifact_id: str = Field(min_length=1, max_length=120)
    dispatch_assessment_artifact_id: str = Field(min_length=1, max_length=120)
    requested_unit: Literal["npu", "gpu", "dsp", "media"]


class DeviceAdapterSpecRequest(BaseModel):
    context_artifact_id: str = Field(min_length=1, max_length=120)
    model_provider: str = Field(default="browser_configured_local_relay", min_length=1, max_length=120)
    model_name: str = Field(default="", max_length=240)
    raw_output: str = Field(default="", max_length=64 * 1024)
    attempt_index: int = Field(default=1, ge=1, le=100)
    parent_adapter_artifact_id: str | None = Field(default=None, min_length=1, max_length=120)
    agent_note: str = Field(default="", max_length=1200)
    result: dict[str, Any]


class DeviceAdapterRunRequest(BaseModel):
    adapter_artifact_id: str = Field(min_length=1, max_length=120)
    execution_authorization: Literal["project_default_authorized", "agent_full_access"] = "project_default_authorized"


class DeviceRemoteShellRunRequest(BaseModel):
    device_probe_artifact_id: str = Field(min_length=1, max_length=120)
    command: str = Field(min_length=1, max_length=16 * 1024)
    timeout_seconds: int = Field(default=240, ge=1, le=900)
    execution_authorization: Literal["project_default_authorized", "agent_full_access"] = "project_default_authorized"


class DeviceExperimentSourceRequest(BaseModel):
    device_probe_artifact_id: str = Field(min_length=1, max_length=120)
    profile_id: Literal["native_c", "native_cpp", "gpu_gles3", "gpu_vulkan", "npu_nnrt", "media_avcodec"]
    language: Literal["c", "cpp"]
    title: str = Field(default="", max_length=160)
    source: str = Field(min_length=1, max_length=MAX_EXPERIMENT_SOURCE_BYTES)
    shader_source: str = Field(default="", max_length=128 * 1024)
    execution_authorization: Literal["agent_full_access"] = "agent_full_access"


class DeviceExperimentBuildRequest(BaseModel):
    device_probe_artifact_id: str = Field(min_length=1, max_length=120)
    staged_source_artifact_id: str = Field(min_length=1, max_length=120)
    timeout_seconds: int = Field(default=240, ge=1, le=MAX_EXPERIMENT_TIMEOUT_SECONDS)
    execution_authorization: Literal["agent_full_access"] = "agent_full_access"


class DeviceValidationRunRequest(BaseModel):
    plan_artifact_id: str = Field(min_length=1, max_length=120)
    user_confirmed: bool = False
    confirmation_nonce: str = Field(default="", max_length=160)
    execution_authorization: Literal["user_confirmation", "agent_full_access"] = "user_confirmation"


class AnalysisTraceEventRequest(BaseModel):
    event_id: str = Field(min_length=1, max_length=160)
    occurred_at: str = Field(min_length=1, max_length=80)
    category: str = Field(min_length=1, max_length=80)
    operation: str = Field(min_length=1, max_length=160)
    phase: str = Field(default="", max_length=80)
    status: str = Field(default="observed", max_length=80)
    request_id: str = Field(default="", max_length=160)
    run_id: str = Field(default="", max_length=160)
    turn: int | None = Field(default=None, ge=0, le=10_000)
    client_sequence: int | None = Field(default=None, ge=0)
    payload: dict[str, Any] = Field(default_factory=dict)


class AnalysisWorkspaceSnapshotRequest(BaseModel):
    snapshot: dict[str, Any] = Field(default_factory=dict)


class ExperienceCaseTriageRequest(BaseModel):
    status: Literal["observed", "triaged", "action_planned", "resolved", "verified", "dismissed"]
    selected_resolution_id: str = Field(default="", max_length=80)
    operator_note: str = Field(default="", max_length=4000)


class ModelRelayChatCompletionRequest(BaseModel):
    """Ephemeral browser-held credential request for an OpenAI-compatible API.

    The key is deliberately not part of a job, artifact, audit event, or
    server-side configuration.  This request is forwarded once and discarded.
    """

    base_url: str = Field(min_length=12, max_length=512)
    # Do not attach Field length validation to the secret: FastAPI's standard
    # validation payload can otherwise reflect the rejected input to clients.
    api_key: str
    model: str = Field(min_length=1, max_length=240)
    messages: list[dict[str, Any]] = Field(min_length=1, max_length=24)
    max_tokens: int | None = Field(default=None, ge=1, le=MODEL_RELAY_MAX_OUTPUT_TOKENS)
    analysis_id: str | None = Field(default=None, min_length=1, max_length=160)
    operation: str = Field(default="structured_inference", max_length=160)
    request_id: str = Field(default="", max_length=160)
    execution_mode: Literal["agent_engine", "direct_relay"] = "agent_engine"


class AgentRunCredentialRequest(BaseModel):
    """Browser BYOK material; never written to an artifact or run event."""

    # Current clients serialize the documented snake_case wire contract.
    # Accept the former browser-state spelling as a read-only compatibility
    # alias so an already-open page can recover after a backend upgrade.
    base_url: str = Field(min_length=12, max_length=512, validation_alias=AliasChoices("base_url", "baseUrl"))
    api_key: str = Field(validation_alias=AliasChoices("api_key", "apiKey"))
    model: str = Field(min_length=1, max_length=240)
    ttl_seconds: int | None = Field(default=None, ge=60, le=24 * 60 * 60)
    context_window_tokens: int = Field(
        default=256 * 1024,
        ge=8 * 1024,
        le=1024 * 1024,
        validation_alias=AliasChoices("context_window_tokens", "contextWindowTokens"),
    )


class AgentRunBudgetRequest(BaseModel):
    max_turns: int = Field(default=10, ge=1, le=100)
    max_actions_per_turn: int = Field(default=8, ge=1, le=12)
    max_output_tokens: int = Field(default=8192, ge=256, le=MODEL_RELAY_MAX_OUTPUT_TOKENS)
    context_window_tokens: int = Field(default=128 * 1024, ge=8 * 1024, le=1024 * 1024)


class AgentRunCreateRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=16 * 1024)
    initial_input: str = Field(min_length=1, max_length=96 * 1024)
    mode: Literal["page_agent", "device_validation"] = "page_agent"
    context: dict[str, Any] = Field(default_factory=dict)
    analysis_id: str | None = Field(default=None, min_length=1, max_length=160)
    budget: AgentRunBudgetRequest = Field(default_factory=AgentRunBudgetRequest)
    credential: AgentRunCredentialRequest | None = None


class AgentRunMessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=96 * 1024)
    mode: Literal["page_agent", "device_validation"] | None = None
    goal: str | None = Field(default=None, min_length=1, max_length=16 * 1024)
    context: dict[str, Any] | None = None
    credential: AgentRunCredentialRequest | None = None


class AgentRunAnalysisBindingRequest(BaseModel):
    analysis_id: str = Field(min_length=1, max_length=160)


class AgentRunResumeRequest(BaseModel):
    additional_turns: int = Field(default=0, ge=0, le=100)
    max_actions_per_turn: int | None = Field(default=None, ge=1, le=12)
    max_output_tokens: int | None = Field(default=None, ge=256, le=MODEL_RELAY_MAX_OUTPUT_TOKENS)
    context_window_tokens: int | None = Field(default=None, ge=8 * 1024, le=1024 * 1024)
    credential: AgentRunCredentialRequest | None = None


class AgentRunUiActionResultRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=160)
    result: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] | None = None
    credential: AgentRunCredentialRequest | None = None


class KimiCliSessionCreateRequest(BaseModel):
    adapter_id: str = Field(default="", max_length=64, pattern=r"^(?:|[a-z][a-z0-9-]{1,63})$")
    run_id: str | None = Field(default=None, min_length=1, max_length=160)
    adopt_page_session: bool = False
    page_session_id: str = Field(min_length=8, max_length=160, pattern=r"^page_[A-Za-z0-9-]+$")
    analysis_id: str | None = Field(default=None, min_length=1, max_length=160)
    goal: str = Field(default="持续协助用户完成鸿蒙异构优化分析。", min_length=1, max_length=16 * 1024)
    mode: Literal["page_agent", "device_validation"] = "page_agent"
    context: dict[str, Any] = Field(default_factory=dict)
    budget: AgentRunBudgetRequest = Field(default_factory=AgentRunBudgetRequest)
    credential: AgentRunCredentialRequest | None = None
    cols: int = Field(default=100, ge=40, le=320)
    rows: int = Field(default=32, ge=12, le=160)


class KimiCliInputRequest(BaseModel):
    data_base64: str = Field(min_length=1, max_length=400 * 1024)


class KimiCliCommandRequest(BaseModel):
    command: str = Field(min_length=1, max_length=256 * 1024)
    command_id: str = Field(default="", max_length=200)


class KimiCliResizeRequest(BaseModel):
    cols: int = Field(ge=40, le=320)
    rows: int = Field(ge=12, le=160)


class KimiCliContextRequest(BaseModel):
    goal: str = Field(min_length=1, max_length=16 * 1024)
    page_session_id: str = Field(min_length=8, max_length=160, pattern=r"^page_[A-Za-z0-9-]+$")
    mode: Literal["page_agent", "device_validation"] = "page_agent"
    context: dict[str, Any] = Field(default_factory=dict)
    analysis_id: str | None = Field(default=None, min_length=1, max_length=160)


# Wire-compatible alias for new integrations. Existing clients and tests can
# continue importing the former request name during the deprecation window.
AgentCliSessionCreateRequest = KimiCliSessionCreateRequest
AgentCliInputRequest = KimiCliInputRequest
AgentCliCommandRequest = KimiCliCommandRequest
AgentCliResizeRequest = KimiCliResizeRequest
AgentCliContextRequest = KimiCliContextRequest


class InternalAgentActionRequest(BaseModel):
    input: dict[str, Any] = Field(default_factory=dict)


class BuildRequestCreateRequest(BaseModel):
    agent_run_id: str = Field(min_length=1, max_length=120)
    source_artifact_id: str = Field(min_length=1, max_length=120)
    profile_id: Literal["native_c", "native_cpp", "gpu_gles3", "gpu_vulkan", "npu_nnrt", "media_avcodec"]
    target: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: int = Field(default=240, ge=1, le=900)


class DeviceLeaseRequest(BaseModel):
    bridge_id: str = Field(min_length=1, max_length=120)
    logical_device_id: str = Field(min_length=1, max_length=160)
    ttl_seconds: int = Field(default=2 * 60 * 60, ge=60, le=24 * 60 * 60)


class DeviceBridgePairingCodeRequest(BaseModel):
    ttl_seconds: int = Field(default=600, ge=60, le=3600)


class DeviceBridgeRegistrationRequest(BaseModel):
    pairing_code: str = Field(default="", max_length=256)
    display_name: str = Field(default="", max_length=160)
    capabilities: dict[str, Any] = Field(default_factory=dict)


class DeviceBridgePollRequest(BaseModel):
    connection_id: str = Field(min_length=35, max_length=75, pattern=r"^connection_[a-f0-9]{24,64}$")
    capabilities: dict[str, Any] = Field(default_factory=dict)
    reconcile_jobs: list[dict[str, Any]] | None = Field(default=None, max_length=256)
    max_messages: int = Field(default=32, ge=1, le=64)
    probe_only: bool = False


class DeviceBridgeEventRequest(BaseModel):
    connection_id: str = Field(default="", max_length=75)
    message: dict[str, Any] = Field(default_factory=dict)


async def enforce_deployment_capabilities(connection: HTTPConnection) -> None:
    if connection.scope["type"] != "http":
        return
    method = connection.scope["method"]
    if request_needs_build_execution(method, connection.url.path):
        require_build_execution()
    if request_needs_device_validation(method, connection.url.path):
        require_device_validation()


app = FastAPI(
    title="Heterogeneous Enablement Local API",
    version="0.2.0",
    description="Single-user Phase-A API with immutable artifacts and source-binding gates.",
    dependencies=[Depends(enforce_deployment_capabilities)],
)
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(?::\d+)?$",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/", include_in_schema=False)
def local_api_root() -> dict[str, str]:
    """Make a direct browser visit useful without coupling API routing to the UI."""

    return {
        "service": "Heterogeneous Enablement Local API",
        "message": f"这是本机 API 服务，不提供工作台页面。请打开 {WORKBENCH_URL}。",
        "workbench_url": WORKBENCH_URL,
        "api_docs": "/docs",
        "overview": "/v1/overview",
    }


@app.exception_handler(PlatformError)
async def platform_error_handler(_: Request, exc: PlatformError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status_code,
        content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}},
    )


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
    """Return useful field diagnostics without reflecting prompts or secrets."""

    validation_errors: list[dict[str, Any]] = []
    for item in exc.errors():
        location = [str(part) for part in item.get("loc", ()) if str(part) not in {"body"}]
        context = item.get("ctx") if isinstance(item.get("ctx"), dict) else {}
        safe_context = {
            str(key): value
            for key, value in context.items()
            if isinstance(value, (str, int, float, bool)) or value is None
        }
        validation_errors.append(
            {
                "field": ".".join(location) or "request",
                "type": str(item.get("type") or "validation_error"),
                "message": str(item.get("msg") or "请求字段无效。")[:300],
                "context": safe_context,
            }
        )
    first_field = validation_errors[0]["field"] if validation_errors else "request"
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "REQUEST_VALIDATION_FAILED",
                "message": f"请求字段校验失败：{first_field}。",
                "details": {"validation_errors": validation_errors},
            }
        },
    )


def _model_relay_base_url(value: str) -> str:
    """Accept only explicitly supported OpenAI-compatible provider roots."""

    normalized = value.strip().rstrip("/")
    if normalized not in MODEL_RELAY_ALLOWED_BASE_URLS:
        raise PlatformError(
            "MODEL_RELAY_PROVIDER_UNSUPPORTED",
            "The local model relay only supports the configured Kimi Coding and Kimi Open Platform endpoints.",
            status_code=422,
            details={"supported_base_urls": sorted(MODEL_RELAY_ALLOWED_BASE_URLS)},
        )
    return normalized


def _kimi_provider_base_url(value: str) -> str:
    """Validate a browser-selected provider root for the managed Kimi engine."""

    normalized = value.strip().rstrip("/")
    try:
        parsed = urlparse(normalized)
        # Accessing ``port`` also rejects malformed values such as ``:abc``.
        parsed.port
        invalid = (
            parsed.scheme.lower() not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or bool(parsed.params)
            or bool(parsed.query)
            or bool(parsed.fragment)
            or any(ord(character) < 32 or ord(character) == 127 for character in normalized)
        )
    except ValueError:
        invalid = True
        parsed = urlparse("")
    if invalid:
        raise PlatformError(
            "KIMI_PROVIDER_BASE_URL_INVALID",
            "模型服务地址必须是合法的 HTTP 或 HTTPS Base URL，且不能包含账号、查询参数或片段。",
            status_code=422,
            details={"examples": ["https://provider.example.com/v1", "http://127.0.0.1:8000/v1"]},
        )
    if parsed.path.rstrip("/").endswith("/chat/completions"):
        raise PlatformError(
            "KIMI_PROVIDER_BASE_URL_INVALID",
            "请填写模型服务 Base URL，不要包含 /chat/completions 接口路径。",
            status_code=422,
            details={"received_path": parsed.path[:240]},
        )
    return normalized


def _configure_kimi_browser_credential(credential: AgentRunCredentialRequest) -> dict[str, Any]:
    """Configure Kimi through its non-persistent ``KIMI_MODEL_*`` channel."""

    base_url = _kimi_provider_base_url(credential.base_url)

    def configure(provider_type: str | None = None) -> dict[str, Any]:
        return kimi_agent_engine.configure_ephemeral_model(
            base_url=base_url,
            api_key=credential.api_key,
            model=credential.model,
            context_window_tokens=credential.context_window_tokens,
            ttl_seconds=credential.ttl_seconds or 30 * 60,
            provider_type=provider_type,
        )

    def probe() -> None:
        _, result = kimi_completion_adapter.complete(
            messages=[{"role": "user", "content": "仅输出 OK。"}],
            max_tokens=8,
            thinking="low",
        )
        if not result.assistant_text.strip():
            raise KimiServerError("KIMI_MODEL_PROBE_EMPTY", "模型探针没有返回文本。")

    def protocol_retryable(error: KimiServerError) -> bool:
        if error.code != "KIMI_COMPLETION_FAILED":
            return False
        diagnostic = f"{error.message} {json.dumps(error.details, ensure_ascii=False, default=str)}".lower()
        return bool(re.search(r"\b(?:400|404|405|415|422)\b", diagnostic)) or any(
            marker in diagnostic
            for marker in ("invalid request", "unsupported parameter", "unknown field", "not found")
        )

    def reject_probe(error: KimiServerError, attempts: list[dict[str, Any]]) -> None:
        try:
            fallback = kimi_agent_engine.clear_ephemeral_model()
        except KimiServerError:
            fallback = {"status": "unavailable"}
        raise PlatformError(
            "KIMI_MODEL_PROBE_FAILED",
            f"浏览器模型配置未通过真实 Agent 调用：{error.message}。平台已撤销该临时配置。",
            status_code=422,
            details={
                "probe_error": error.code,
                "probe_details": error.details,
                "probe_attempts": attempts,
                "fallback_status": fallback.get("status"),
                "fallback_credential_mode": fallback.get("credential_mode"),
            },
        ) from error

    try:
        result = configure()
        if result.get("configuration_reloaded") is not True:
            return {**result, "model_probe": "retained"}
        probe_attempts: list[dict[str, Any]] = []
        try:
            probe()
        except KimiServerError as first_probe_error:
            initial_provider = str(result.get("provider_type") or "kimi")
            probe_attempts.append(
                {
                    "provider_type": initial_provider,
                    "error": first_probe_error.code,
                    "details": first_probe_error.details,
                }
            )
            if not protocol_retryable(first_probe_error):
                reject_probe(first_probe_error, probe_attempts)
            alternate_provider = "openai" if initial_provider == "kimi" else "kimi"
            try:
                alternate_result = configure(alternate_provider)
                probe()
                return {
                    **alternate_result,
                    "model_probe": "passed",
                    "provider_resolution": "protocol_fallback",
                    "initial_provider_type": initial_provider,
                }
            except KimiServerError as alternate_probe_error:
                probe_attempts.append(
                    {
                        "provider_type": alternate_provider,
                        "error": alternate_probe_error.code,
                        "details": alternate_probe_error.details,
                    }
                )
                reject_probe(alternate_probe_error, probe_attempts)
        return {**result, "model_probe": "passed", "provider_resolution": "inferred"}
    except KimiServerError as exc:
        status_code = 409 if exc.code in {
            "KIMI_MODEL_RECONFIGURE_BUSY",
            "KIMI_EXTERNAL_SERVER_RECONFIGURE_FORBIDDEN",
        } else 422 if exc.code == "KIMI_EPHEMERAL_CREDENTIAL_INVALID" else 503
        raise PlatformError(
            exc.code,
            exc.message,
            status_code=status_code,
            details=exc.details,
        ) from exc


def _model_relay_error_message(status_code: int, payload: bytes) -> str:
    """Return a bounded provider diagnostic without carrying request secrets."""

    try:
        decoded = json.loads(payload.decode("utf-8", errors="replace"))
    except json.JSONDecodeError:
        decoded = {}
    error = decoded.get("error") if isinstance(decoded, dict) else None
    detail = error.get("message") if isinstance(error, dict) else None
    if isinstance(detail, str) and detail.strip():
        return f"上游模型服务拒绝了请求（HTTP {status_code}）：{detail.strip()[:280]}"
    return f"上游模型服务拒绝了请求（HTTP {status_code}）。"


def _model_relay_http_error_details(exc: HTTPError) -> dict[str, Any]:
    details: dict[str, Any] = {"upstream_status": exc.code}
    retry_after = exc.headers.get("Retry-After") if exc.headers else None
    if retry_after:
        try:
            details["retry_after_seconds"] = max(0, min(30, int(float(retry_after))))
        except ValueError:
            details["retry_after"] = str(retry_after)[:80]
    return details


class _ModelRelayConnectionFailure(Exception):
    """Carry safe transport diagnostics across the sync and SSE relay paths."""

    def __init__(self, message: str, details: dict[str, Any]) -> None:
        super().__init__(message)
        self.message = message
        self.details = details


def _model_relay_proxy_endpoint() -> str | None:
    """Describe the active HTTPS proxy without exposing embedded credentials."""

    proxy = getproxies().get("https")
    if not proxy:
        return None
    parsed = urlparse(proxy)
    if not parsed.hostname:
        return "已配置（地址无法解析）"
    scheme = parsed.scheme or "http"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{scheme}://{parsed.hostname}{port}"


def _model_relay_failure_kind(reason: object) -> tuple[str, str, bool]:
    """Classify an urllib connection error and decide whether a quick retry helps."""

    if isinstance(reason, ssl.SSLCertVerificationError):
        return "tls_certificate", "TLS 证书校验失败", False
    if isinstance(reason, ssl.SSLError):
        return "tls", "TLS 握手失败", False
    if isinstance(reason, socket.gaierror):
        return "dns", "DNS 解析失败", True
    if isinstance(reason, ConnectionRefusedError):
        return "connection_refused", "连接被拒绝", True
    if isinstance(reason, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
        return "connection_reset", "连接被中断或重置", True
    if isinstance(reason, (TimeoutError, socket.timeout)):
        return "timeout", "建立连接或等待响应超时", False
    if isinstance(reason, OSError):
        if reason.errno in {54, 104}:
            return "connection_reset", "连接被上游重置", True
        if reason.errno in {60, 110}:
            return "timeout", "建立连接或等待响应超时", False
        if reason.errno in {61, 111}:
            return "connection_refused", "连接被拒绝", True
    return "network", "网络连接失败", True


def _model_relay_connection_failure(
    *,
    upstream: UrlRequest,
    failures: list[dict[str, Any]],
    direct_fallback_attempted: bool,
) -> _ModelRelayConnectionFailure:
    last = failures[-1] if failures else {"kind": "network", "summary": "网络连接失败", "reason": "unknown"}
    provider_host = urlparse(upstream.full_url).hostname or "上游模型服务"
    proxy_endpoint = _model_relay_proxy_endpoint()
    proxy_note = f"；当前系统 HTTPS 代理为 {proxy_endpoint}" if proxy_endpoint else ""
    direct_note = "，绕过本机代理直连也未成功" if direct_fallback_attempted else ""
    message = (
        f"本机中继无法连接上游模型服务 {provider_host}：{last['summary']}{proxy_note}{direct_note}。"
        "请确认网络或本机代理可用后重试；模型配置和 API 密钥仍保存在浏览器中。"
    )
    return _ModelRelayConnectionFailure(
        message,
        {
            "provider_host": provider_host,
            "failure_kind": last["kind"],
            "reason": last["reason"],
            "attempts": len(failures),
            "proxy_endpoint": proxy_endpoint,
            "direct_fallback_attempted": direct_fallback_attempted,
        },
    )


def _open_model_relay_upstream(upstream: UrlRequest, *, timeout: int):
    """Open an allowlisted provider with bounded retry and local-proxy fallback."""

    # urllib mutates a Request when routing it through an HTTP proxy.  Rebuild
    # it for every attempt so a failed proxy target cannot leak into the direct
    # fallback request.
    original_url = upstream.full_url
    original_data = upstream.data
    original_headers = dict(upstream.header_items())
    original_method = upstream.get_method()

    def fresh_request() -> UrlRequest:
        return UrlRequest(
            original_url,
            data=original_data,
            headers=original_headers,
            method=original_method,
        )

    failures: list[dict[str, Any]] = []
    for attempt in range(MODEL_RELAY_CONNECT_ATTEMPTS):
        try:
            return urlopen(fresh_request(), timeout=timeout)  # nosec B310: fixed allowlist in _model_relay_base_url
        except HTTPError:
            raise
        except URLError as exc:
            kind, summary, retryable = _model_relay_failure_kind(exc.reason)
            failures.append({"kind": kind, "summary": summary, "reason": str(exc.reason)[:240], "transport": "environment"})
            if not retryable or attempt + 1 >= MODEL_RELAY_CONNECT_ATTEMPTS:
                break
            time.sleep(0.25 * (attempt + 1))

    proxy_endpoint = _model_relay_proxy_endpoint()
    proxy_host = urlparse(proxy_endpoint).hostname if proxy_endpoint and "://" in proxy_endpoint else None
    direct_fallback_attempted = proxy_host in {"127.0.0.1", "::1", "localhost"}
    if direct_fallback_attempted:
        try:
            return build_opener(ProxyHandler({})).open(fresh_request(), timeout=timeout)
        except HTTPError:
            raise
        except URLError as exc:
            kind, summary, _ = _model_relay_failure_kind(exc.reason)
            failures.append({"kind": kind, "summary": summary, "reason": str(exc.reason)[:240], "transport": "direct"})

    raise _model_relay_connection_failure(
        upstream=upstream,
        failures=failures,
        direct_fallback_attempted=direct_fallback_attempted,
    )


def _model_relay_upstream_request(
    request: ModelRelayChatCompletionRequest,
    *,
    stream: bool,
    include_stream_usage: bool = True,
) -> UrlRequest:
    """Build a bounded OpenAI-compatible upstream request once per relay call."""

    base_url = _model_relay_base_url(request.base_url)
    request_body: dict[str, Any] = {
        "model": request.model,
        "messages": request.messages,
    }
    if request.max_tokens is not None:
        request_body["max_tokens"] = request.max_tokens
    if stream:
        request_body["stream"] = True
        # OpenAI-compatible streaming omits usage unless the client requests
        # the final usage-only chunk. Without it, cached input must remain
        # unknown and the UI can only show a local token estimate.
        if include_stream_usage:
            request_body["stream_options"] = {"include_usage": True}
    try:
        encoded = json.dumps(request_body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise PlatformError("MODEL_RELAY_REQUEST_INVALID", "Model messages must be JSON serializable.", status_code=422) from exc
    if len(encoded) > 512 * 1024:
        raise PlatformError("MODEL_RELAY_REQUEST_TOO_LARGE", "Model request body exceeds the 512 KiB local relay limit.", status_code=413)

    return UrlRequest(
        f"{base_url}/chat/completions",
        data=encoded,
        headers={
            "Accept": "text/event-stream" if stream else "application/json",
            "Authorization": f"Bearer {request.api_key}",
            "Content-Type": "application/json",
            # The workbench is Chinese-first. This cannot override a
            # provider's private reasoning policy, but it gives compatible
            # services the same locale signal as the visible UI and prompts.
            "Accept-Language": "zh-CN,zh;q=0.9",
            # This is the workbench's real client identity; it does not mimic
            # Kimi Code CLI or any other third-party client.
            "User-Agent": "Heterogeneous-Enablement-Workbench/0.2 (local model relay)",
        },
        method="POST",
    )


def _model_relay_rejected_stream_usage(exc: HTTPError, raw: bytes) -> bool:
    """Detect only an explicit rejection of the optional usage parameter."""

    if exc.code not in {400, 422}:
        return False
    message = raw.decode("utf-8", errors="replace").casefold()
    parameter_named = "stream_options" in message or "include_usage" in message
    rejection_named = any(
        marker in message
        for marker in ("unsupported", "unknown", "unrecognized", "not permitted", "extra inputs", "invalid parameter")
    )
    return parameter_named and rejection_named


def _model_relay_response_limit(request: ModelRelayChatCompletionRequest, *, stream: bool) -> int:
    """Size the transport guard from the requested semantic output budget.

    SSE carries JSON framing and metadata for every delta, so its wire size can
    be far larger than the final text.  The global ceiling still protects the
    workstation from an unbounded or malformed upstream stream.
    """

    requested_tokens = request.max_tokens or 8 * 1024
    bytes_per_token = MODEL_RELAY_STREAM_BYTES_PER_OUTPUT_TOKEN if stream else 32
    return min(
        MODEL_RELAY_MAX_RESPONSE_BYTES,
        max(MODEL_RELAY_MIN_RESPONSE_BYTES, requested_tokens * bytes_per_token),
    )


def _model_relay_size_label(value: int) -> str:
    return f"{value / (1024 * 1024):g} MiB"


def _model_relay_response_too_large(
    request: ModelRelayChatCompletionRequest,
    *,
    stream: bool,
    observed_bytes: int,
) -> tuple[str, dict[str, Any]]:
    limit = _model_relay_response_limit(request, stream=stream)
    kind = "流式传输体" if stream else "响应体"
    message = (
        f"模型{kind}超过本机中继 {_model_relay_size_label(limit)} 安全上限。"
        "该限制按本轮输出 Token 预算计算，用于保护本地工作站；"
        "请重试，若持续发生可降低单轮输出上限或拆分任务。"
    )
    return message, {
        "transport": "streaming" if stream else "non_streaming",
        "observed_bytes": observed_bytes,
        "limit_bytes": limit,
        "configured_global_limit_bytes": MODEL_RELAY_MAX_RESPONSE_BYTES,
        "requested_max_tokens": request.max_tokens,
    }


def _relay_chat_completion(request: ModelRelayChatCompletionRequest) -> dict[str, Any]:
    """Forward one completion without persisting credentials or model output."""

    upstream = _model_relay_upstream_request(request, stream=False)
    response_limit = _model_relay_response_limit(request, stream=False)
    try:
        with _open_model_relay_upstream(upstream, timeout=MODEL_RELAY_TIMEOUT_SECONDS) as response:
            raw = response.read(response_limit + 1)
    except HTTPError as exc:
        raw = exc.read(MODEL_RELAY_MIN_RESPONSE_BYTES + 1)
        raise PlatformError(
            "MODEL_RELAY_UPSTREAM_REJECTED",
            _model_relay_error_message(exc.code, raw),
            status_code=exc.code if 400 <= exc.code < 500 else 502,
            details=_model_relay_http_error_details(exc),
        ) from exc
    except TimeoutError as exc:
        raise PlatformError(
            "MODEL_RELAY_UPSTREAM_TIMEOUT",
            f"上游模型服务在本机中继等待 {MODEL_RELAY_TIMEOUT_SECONDS} 秒后仍未完成响应；请稍后重试，或缩小热点代码范围。",
            status_code=504,
            details={"timeout_seconds": MODEL_RELAY_TIMEOUT_SECONDS},
        ) from exc
    except _ModelRelayConnectionFailure as exc:
        raise PlatformError(
            "MODEL_RELAY_UPSTREAM_UNAVAILABLE",
            exc.message,
            status_code=502,
            details=exc.details,
        ) from exc
    if len(raw) > response_limit:
        message, details = _model_relay_response_too_large(
            request,
            stream=False,
            observed_bytes=len(raw),
        )
        raise PlatformError("MODEL_RELAY_RESPONSE_TOO_LARGE", message, status_code=502, details=details)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise PlatformError("MODEL_RELAY_RESPONSE_INVALID", "Upstream model service returned a non-JSON response.", status_code=502) from exc
    if not isinstance(payload, dict):
        raise PlatformError("MODEL_RELAY_RESPONSE_INVALID", "Upstream model service returned an invalid JSON object.", status_code=502)
    return payload


def _relay_stream_error_event(code: str, message: str, details: dict[str, Any] | None = None) -> bytes:
    """Emit a safe, structured error after an SSE response has started."""

    payload = {"error": {"code": code, "message": message, "details": details or {}}}
    return f"event: error\ndata: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8")


def _relay_chat_completion_stream(request: ModelRelayChatCompletionRequest) -> StreamingResponse:
    """Proxy OpenAI-compatible SSE without storing credentials or model text.

    Kimi Code's local-router guidance uses streaming Chat Completions.  Keeping
    the SSE connection intact lets the browser show a model-progress preview
    and prevents a long-thinking response from looking idle until its complete
    JSON answer is ready.
    """

    upstream = _model_relay_upstream_request(request, stream=True)
    response_limit = _model_relay_response_limit(request, stream=True)
    try:
        response = _open_model_relay_upstream(upstream, timeout=MODEL_RELAY_STREAM_IDLE_TIMEOUT_SECONDS)
    except HTTPError as exc:
        raw = exc.read(MODEL_RELAY_MIN_RESPONSE_BYTES + 1)
        if _model_relay_rejected_stream_usage(exc, raw):
            fallback = _model_relay_upstream_request(request, stream=True, include_stream_usage=False)
            try:
                response = _open_model_relay_upstream(fallback, timeout=MODEL_RELAY_STREAM_IDLE_TIMEOUT_SECONDS)
            except HTTPError as fallback_exc:
                fallback_raw = fallback_exc.read(MODEL_RELAY_MIN_RESPONSE_BYTES + 1)
                raise PlatformError(
                    "MODEL_RELAY_UPSTREAM_REJECTED",
                    _model_relay_error_message(fallback_exc.code, fallback_raw),
                    status_code=fallback_exc.code if 400 <= fallback_exc.code < 500 else 502,
                    details=_model_relay_http_error_details(fallback_exc),
                ) from fallback_exc
            except TimeoutError as fallback_exc:
                raise PlatformError(
                    "MODEL_RELAY_UPSTREAM_TIMEOUT",
                    f"上游模型服务在 {MODEL_RELAY_STREAM_IDLE_TIMEOUT_SECONDS} 秒内没有开始流式响应；请稍后重试。",
                    status_code=504,
                    details={"idle_timeout_seconds": MODEL_RELAY_STREAM_IDLE_TIMEOUT_SECONDS},
                ) from fallback_exc
            except _ModelRelayConnectionFailure as fallback_exc:
                raise PlatformError(
                    "MODEL_RELAY_UPSTREAM_UNAVAILABLE",
                    fallback_exc.message,
                    status_code=502,
                    details=fallback_exc.details,
                ) from fallback_exc
        else:
            raise PlatformError(
                "MODEL_RELAY_UPSTREAM_REJECTED",
                _model_relay_error_message(exc.code, raw),
                status_code=exc.code if 400 <= exc.code < 500 else 502,
                details=_model_relay_http_error_details(exc),
            ) from exc
    except TimeoutError as exc:
        raise PlatformError(
            "MODEL_RELAY_UPSTREAM_TIMEOUT",
            f"上游模型服务在 {MODEL_RELAY_STREAM_IDLE_TIMEOUT_SECONDS} 秒内没有开始流式响应；请稍后重试。",
            status_code=504,
            details={"idle_timeout_seconds": MODEL_RELAY_STREAM_IDLE_TIMEOUT_SECONDS},
        ) from exc
    except _ModelRelayConnectionFailure as exc:
        raise PlatformError(
            "MODEL_RELAY_UPSTREAM_UNAVAILABLE",
            exc.message,
            status_code=502,
            details=exc.details,
        ) from exc

    def stream_events() -> Iterator[bytes]:
        total_bytes = 0
        try:
            content_type = str(response.headers.get("Content-Type") or "").casefold()
            if "text/event-stream" not in content_type:
                # Some OpenAI-compatible proxies silently ignore `stream` and
                # return a normal completion. Convert it into one valid SSE
                # event so the browser can still complete this strategy.
                raw = response.read(response_limit + 1)
                if len(raw) > response_limit:
                    message, details = _model_relay_response_too_large(
                        request,
                        stream=True,
                        observed_bytes=len(raw),
                    )
                    yield _relay_stream_error_event("MODEL_RELAY_RESPONSE_TOO_LARGE", message, details)
                    return
                try:
                    payload = json.loads(raw.decode("utf-8"))
                except json.JSONDecodeError:
                    yield _relay_stream_error_event("MODEL_RELAY_RESPONSE_INVALID", "Upstream model service returned a non-JSON response.")
                    return
                yield f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8")
                yield b"data: [DONE]\n\n"
                return

            for chunk in response:
                total_bytes += len(chunk)
                if total_bytes > response_limit:
                    message, details = _model_relay_response_too_large(
                        request,
                        stream=True,
                        observed_bytes=total_bytes,
                    )
                    yield _relay_stream_error_event("MODEL_RELAY_RESPONSE_TOO_LARGE", message, details)
                    return
                yield chunk
        except TimeoutError:
            yield _relay_stream_error_event(
                "MODEL_RELAY_UPSTREAM_TIMEOUT",
                f"上游模型服务连续 {MODEL_RELAY_STREAM_IDLE_TIMEOUT_SECONDS} 秒没有产生流式输出；请稍后重试。",
                {"idle_timeout_seconds": MODEL_RELAY_STREAM_IDLE_TIMEOUT_SECONDS},
            )
        except URLError as exc:
            kind, summary, _ = _model_relay_failure_kind(exc.reason)
            yield _relay_stream_error_event(
                "MODEL_RELAY_UPSTREAM_UNAVAILABLE",
                f"模型流式连接已中断：{summary}。本轮未形成完整回答，请重试。",
                {"failure_kind": kind, "reason": str(exc.reason)[:240], "stage": "stream_read"},
            )
        finally:
            response.close()

    return StreamingResponse(
        stream_events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _require_project(project_id: str) -> None:
    if project_id != PROJECT_ID:
        raise PlatformError(
            "PROJECT_NOT_FOUND",
            "Phase A only exposes the local-default project.",
            status_code=404,
            details={"project_id": project_id},
        )


def _default_connector() -> dict[str, Any]:
    root = default_source_root()
    if not root.is_dir():
        raise PlatformError(
            "DEFAULT_SOURCE_ROOT_UNAVAILABLE",
            "默认源码目录不存在。请修正 HETERO_DEFAULT_SOURCE_ROOT，或取消该配置以使用平台仓库。",
            status_code=503,
            details={"source_root": str(root)},
        )
    display_name = "平台代码仓" if root == APP_ROOT else root.name
    return connectors.ensure_local_workspace(root, display_name=display_name)


def _optional_default_connector() -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Describe default-source readiness without making the workbench unavailable.

    A deployed application tree may come from a release archive and therefore
    contain no Git metadata. Source analysis still requires a real, snapshotable
    connector, but service liveness and repository registration do not.
    """

    root = default_source_root()
    try:
        connector = _default_connector()
    except PlatformError as exc:
        return None, {
            "status": "unavailable",
            "source_root": str(root),
            "error": {"code": exc.code, "message": exc.message, "details": exc.details},
        }
    return connector, {
        "status": "ready",
        "source_root": str(root),
        "source_connector_id": connector["connector_id"],
    }


def _runtime_system_prompt(run: dict[str, Any]) -> str:
    """Compatibility prompt for the legacy JSON-action runtime.

    KimiEngineAgentRuntime uses its workspace AGENTS.md plus native Kimi tools
    and does not consume this action-only prompt. Keeping the legacy contract
    explicit prevents these historical host restrictions from being mistaken
    for the current Kimi backend policy.
    """

    device_validation = str(run.get("mode") or "") == "device_validation"
    device_validation_ui_actions = {"workspace.read_file"}
    browser_actions = [
        {
            "action_id": item.get("action_id"),
            "owner": "ui",
            "risk_level": item.get("risk_level"),
            "input_schema": item.get("input_schema"),
        }
        for item in action_catalog()
        # A Runtime action is always backend-owned even if the former browser
        # workbench registered the same name.  Hide legacy device composition
        # actions as well, otherwise a model can accidentally stop a durable
        # run on a browser-only dependency chain.
        if str(item.get("action_id") or "") not in RUNTIME_ACTIONS
        and str(item.get("action_id") or "") not in LEGACY_BROWSER_DEVICE_ACTIONS
        and (not device_validation or str(item.get("action_id") or "") in device_validation_ui_actions)
    ]
    backend_actions = [
        {
            "action_id": action_id,
            "owner": definition["owner"],
            "required_input": (
                ["language", "profile_id", "source_ref"]
                if action_id == "build.stage_source"
                else sorted(definition["required"])
            ),
            **(
                {"payload_transport": "source_ref 对应响应中的 <agent-source> 原始源码块"}
                if action_id == "build.stage_source"
                else {}
            ),
        }
        for action_id, definition in sorted(RUNTIME_ACTIONS.items())
    ]
    action_text = json.dumps([*backend_actions, *browser_actions], ensure_ascii=False, separators=(",", ":"))
    mode_guidance = (
        "当前任务是真机验证阶段。起始任务包已经注入热点源码、策略摘要、目标单元和已有设备事实；"
        "使用这些已验证事实，不得机械地重新执行硬件路由、机会推理、Trace 导入、知识库检索或页面流程操作。只有任务包给出真实代码仓相对路径且必须展开被调函数时，"
        "才可调用 workspace.read_file；实验源码直接交给 build.stage_source。"
        if device_validation
        else
        "用户输入、附件和页面状态没有固定用途。首次输入的完整函数或循环在带有分析意图时可以作为热点候选，但也可能只是对照代码、日志、补充资料或实验草案。"
        "Skill 查看、编辑和创建属于独立 focused task：先调用 skills.list/read，保存时只创建新的用户版本，不能改写平台原始 Skill。"
        "从已登记代码仓构建异构单元 API 知识库属于独立的 focused task：先列举自建库与 Connector，再执行有界扫描；不得进入热点、硬件路由、机会推理或真机流程。"
        "只有在动态上下文提供的 input_id / attachment_id 与用户意图一致时，才调用 assistant.use_pending_text_as_hotspot 或 assistant.import_pending_trace；"
        "自然语言与裸代码混合时，热点 action 使用一基 line_range，只保存源码行；"
        "不要按文件后缀、关键词或正则强行改变页面状态。若用户明确要求对一段代码完成异构优化分析，默认自主执行完整链路：先保存热点代码，等待真实返回；"
        "再运行 reasoning.run_hardware_semantic_route，等待路由工件；随后运行 reasoning.run_strategy(strategy_id=hardware_kb)，等待机会结果；"
        "若结果存在可验证的硬件或并行方向，则继续 device.discover/probe、模板/API 选择、构建、部署、手机执行和结果比较。"
        "每个 owner=ui 操作都是同步边界，必须拿到真实返回后再决定下一步。只有用户明确只要求局部步骤、只问问题或拒绝真机执行时才缩短链路。"
    )
    analysis_rule = (
        "5. 当前目标单元和策略方向来自已注入的真机任务包；不得重新路由或补造其他硬件方向。先验证最小 API/运行时可达性与 CPU/reference 功能等价，"
        "再区分目标单元实际参与、热点集成和性能收益证据。"
        if device_validation
        else
        "5. 对热点机会识别，先从可见代码与已记录 AST/Trace 事实抽象计算形态、输入输出、访问模式、依赖和粒度；再选择少量可能的硬件单元，"
        "随后检索或比较该单元的 Top-N API。不能因为名称、循环、buffer、麒麟或鸿蒙字样就断言设备能力或卸载收益。"
    )
    return f"""# 平台 Agent Runtime Skill v1.8.0

你是鸿蒙异构优化工作台的后端页面助手。所有解释、计划和结论使用中文；源码、API 名称、设备原始输出保持原文。

你只能调用已注册 action。动作按所有权执行：`build.*` 在后端 Build Worker，`device.*` 在本机 HDC 适配器或已自动登记的 Windows Device Bridge，其他已注册页面动作由浏览器以 `ui.*` 方式执行。不得请求宿主机 Shell、编译器路径、HDC 路径、环境变量、Bridge 密钥或任意 DOM 操作。

真实 action 返回值是唯一新增事实。不得在同一轮使用前一 action 尚未返回的 artifact ID、remote path、device lease 或 serial。构建、部署、设备执行、功能等价、硬件参与、热点集成和性能收益是独立证据，不能互相自动升级。

{mode_guidance}

真机验证必须形成最小可证伪闭环：先选择可见计算核心和 CPU/reference，并按 API 暴露层选择测试承载。公开应用/NDK 生命周期优先 HAP + Hypium/OHJSUnitTest，原生 shell API 优先 Developer Test/gtest + xDevice CppTest，系统/HDI/provider 优先 HATS；一致性套件只能作为辅助。只有已注册 runner action 的真实返回才能证明官方框架已执行；没有 runner 时使用固定 profile + `device.execute_shell` 的 standalone fallback 并说明缺口，不能请求宿主 Shell。生产运行先调用 `platform.list_hardware_api_templates` 筛选，再用返回的精确 template_id 调用 `platform.read_hardware_api_template`；不得猜测模板文件路径或把 artifact ID 当路径。只选择 unit、API family、暴露层、profile 和数据语义相容且逐文件 SHA-256 匹配的模板；允许复用 loader/context、图或 codec 生命周期、错误处理和清理，已有相容模板时不得从零重写稳定脚手架，但必须替换当前热点的 kernel/算子、shape/format、输入、CPU oracle、比较与 benchmark，并报告 template_id、版本、文件 hash 及修改边界。模板历史 PASS 不是本轮证据；Demo、旧 Run、fixture、生成物或研究归档中的未登记实现不能直接复制。必要时调用 `build.describe_profile` 读取真实固定 SDK 头文件和链接面，模板也必须服从本轮构建面，再按 `build.stage_source`、`build.create`、`device.deploy_artifact`、`device.execute_shell` 的真实依赖推进。`build.stage_source` 只暂存源码，不依赖 `device.probe`；`device.probe` 只依赖 `device.discover` 返回的真实 serial。二者可以在已有构建面与 serial 时独立推进。没有真实设备执行结果或明确的环境阻塞结果时，不得宣称真机验证完成；遇到工具错误时读取 code/details 并做最小修订，不能说“等待页面返回”。

本机 HDC 模式直接从 `device.discover` 开始。Windows Bridge 主路径为 `device.list_bridges -> device.discover -> device.probe`；动态上下文已有用户选择的 `device_selector.bridge_id` 时可直接发现该 Bridge。依据用户选择或发现的唯一候选确认 `bridge_id`、`logical_device_id` 与 `device_serial`；多个候选先确认目标。远程 `device.probe/deploy_artifact/execute_shell/collect_artifact` 携带已确认的 `bridge_id` 与 `logical_device_id` 即可，`device_lease_id` 可省略，由平台自动创建或复用单写租约。`device.create_lease` 保留为可选兼容接口。远程操作失败时先读取 `failure_domain`、`stage`、`bridge_diagnostics` 与完整 HDC `return_code/stdout/stderr/invocation`，区分 Linux 到 Windows、Windows spool、HDC 进程、HDC 到设备、设备文件系统和设备端完整性。用户修复外部环境后，可为 `device.discover` 提供新的 `retry_nonce` 获取新鲜诊断。

设备租约是平台并发控制状态。平台确认可安全替代的过期租约后，通过 `lease_resolution` 返回有效租约，后续动作沿用真实返回与已成功的构建、probe。任务执行状态未知、设备忙或需要对账时，保留真实 job_id 和诊断，先完成任务对账再继续；租约维护交给平台，不循环创建租约或重跑已有结果。

后端真机路径禁止使用旧浏览器组合 action `device.describe_experiment_profiles`、`device.stage_experiment_source`、`device.build_upload_experiment`。它们不会出现在本 Run 的 action 清单中，并且即使被引用也会被 Router 拒绝；新 Run 必须使用 `build.*` 与 `device.*` 后端 action，以便浏览器关闭后仍可继续。`build.describe_profile` 的结果才是可编译头文件与链接面的事实，知识库或案例源码只能解释 API 语义，不能替代构建面事实。

已注册 Action（`owner=ui` 由当前浏览器执行；其他由后端执行）：
{action_text}

每轮响应先返回一个严格 JSON 控制对象：
{{"reply":"简短中文说明","disposition":"continue|complete|wait_for_user","actions":[{{"action_id":"...","input":{{}}}}]}}

当且仅当调用 `build.stage_source` 时，完整源码不得放进 JSON 的 `source` 字符串，而是让 action 的 input 使用 `source_ref`，并在 JSON 之后附加对应原始源码块：
<agent-source id="experiment_c" language="c">
#include <stdint.h>
void _start(void) {{}}
</agent-source>

原始源码块不是 Markdown fence；`id` 必须与 `source_ref` 完全一致。平台会在解析后把原始块无损注入 `build.stage_source.source`，不会把源码块重复放入后续模型上下文。

规则：
1. `continue` 必须带至少一个当前参数完整的 action；没有 action 时使用 `wait_for_user` 或 `complete`。
2. `complete` 仅在已有充分真实结果或无法由已注册 action 获得新事实时使用，并清楚写出证据边界。
3. C/C++ 源码必须通过 `agent-source` 原始块传输，不要塞入 JSON，不要用 Markdown fence。源码必须保持可编译词法形式，类型与标识符、关键字与表达式之间的必要空白不得为了缩短输出而删除，例如必须写 `int main`、`return 0`、`#define N`，不能写成 `intmain`、`return0`、`#defineN`。必须实现 profile 返回的精确源码 `entrypoint`；动态 E0 profile 返回 `int main(void)` 时，Build Worker 会注入固定 `_start` 启动胶水，源码不得自行调用 `__libc_start_main` 或编写启动汇编。
4. 后续用户消息或工具观察会以独立记录送入。优先从检查点继续，绝不机械重启硬件路由或已成功 action。
{analysis_rule}
6. `owner=ui` action 是浏览器同步边界。不要在同一个 `actions` 数组里把它与 `build.*` 或 `device.*` 混排；先等待该 ui action 的真实结果进入下一轮，再重新决定后端 action。纯后端 action 可以在彼此没有 artifact 依赖时同轮提交。
7. 完整代码分析流程不得把“已排队”当作完成。热点保存、硬件路由和机会推理 action 都会返回结构化结果；根据结果继续下一阶段。若某一步失败、缺少设备、缺少必要代码/输入合同，或证据明确不支持继续，使用 `wait_for_user` 或 `complete`，说明卡在哪一步、真实错误和用户可选择的处理方式，不得静默结束。
"""


def _runtime_model_gateway(
    lease: ModelLease,
    messages: list[dict[str, str]],
    *,
    max_tokens: int,
    on_stream: Any,
    cancel_event: threading.Event,
) -> dict[str, Any]:
    """Call an allowlisted provider for a durable Agent Run.

    This duplicates the relay's wire handling instead of routing through an
    HTTP endpoint so an API key is never serialized into a loopback request.
    It retains provider reasoning/output deltas for SSE and retries transient
    overload/network failures with bounded backoff.
    """

    request = ModelRelayChatCompletionRequest(
        base_url=lease.base_url,
        api_key=lease.api_key,
        model=lease.model,
        messages=messages,
        max_tokens=max_tokens,
    )
    last_error: ModelGatewayError | None = None
    include_stream_usage = True
    for attempt in range(1, 4):
        response = None
        cancel_watcher_stop: threading.Event | None = None
        try:
            if cancel_event.is_set():
                raise ModelGatewayError("MODEL_CANCELLED", "模型请求已由用户取消。", status_code=409)
            upstream = _model_relay_upstream_request(request, stream=True, include_stream_usage=include_stream_usage)
            response = _open_model_relay_upstream(upstream, timeout=MODEL_RELAY_STREAM_IDLE_TIMEOUT_SECONDS)
            cancel_watcher_stop = threading.Event()

            def close_response_on_cancel() -> None:
                while not cancel_watcher_stop.wait(0.1):
                    if cancel_event.is_set():
                        try:
                            response.close()
                        except Exception:
                            pass
                        return

            threading.Thread(
                target=close_response_on_cancel,
                name="agent-model-cancel-watcher",
                daemon=True,
            ).start()
            total_bytes = 0
            content_parts: list[str] = []
            reasoning_parts: list[str] = []
            usage: dict[str, Any] = {}
            finish_reason = "stop"
            content_type = str(response.headers.get("Content-Type") or "").casefold()
            if "text/event-stream" not in content_type:
                raw = response.read(_model_relay_response_limit(request, stream=True) + 1)
                if len(raw) > _model_relay_response_limit(request, stream=True):
                    raise ModelGatewayError("MODEL_RESPONSE_TOO_LARGE", "模型响应超过本机 Runtime 安全上限。", status_code=502)
                payload = json.loads(raw.decode("utf-8"))
                choice = _record((payload.get("choices") or [{}])[0] if isinstance(payload, dict) else {})
                message = _record(choice.get("message"))
                finish_reason = _text(choice.get("finish_reason"), 80) or finish_reason
                content_parts.append(_text(message.get("content"), MAX_MODEL_TRANSCRIPT_TEXT_CHARS))
                reasoning_parts.append(_text(message.get("reasoning_content") or message.get("reasoning"), MAX_MODEL_TRANSCRIPT_TEXT_CHARS))
                usage = _record(payload.get("usage")) if isinstance(payload, dict) else {}
            else:
                for raw_line in response:
                    if cancel_event.is_set():
                        raise ModelGatewayError("MODEL_CANCELLED", "模型请求已由用户取消。", status_code=409)
                    total_bytes += len(raw_line)
                    if total_bytes > _model_relay_response_limit(request, stream=True):
                        raise ModelGatewayError("MODEL_RESPONSE_TOO_LARGE", "模型流式响应超过本机 Runtime 安全上限。", status_code=502)
                    line = raw_line.decode("utf-8", errors="replace").strip()
                    if not line.startswith("data:"):
                        continue
                    raw_data = line[5:].strip()
                    if raw_data == "[DONE]":
                        break
                    try:
                        payload = json.loads(raw_data)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(payload, dict):
                        continue
                    upstream_error = _record(payload.get("error"))
                    if upstream_error:
                        message = _text(upstream_error.get("message"), 600) or "上游模型流返回错误。"
                        raise ModelGatewayError("MODEL_STREAM_ERROR", message, status_code=502, retryable=True)
                    if isinstance(payload.get("usage"), dict):
                        usage = _record(payload.get("usage"))
                    choices = payload.get("choices")
                    if not isinstance(choices, list) or not choices:
                        continue
                    delta = _record(_record(choices[0]).get("delta"))
                    observed_finish_reason = _text(_record(choices[0]).get("finish_reason"), 80)
                    if observed_finish_reason:
                        finish_reason = observed_finish_reason
                    reasoning = _text(delta.get("reasoning_content") or delta.get("reasoning"), MAX_MODEL_TRANSCRIPT_TEXT_CHARS)
                    content = _text(delta.get("content"), MAX_MODEL_TRANSCRIPT_TEXT_CHARS)
                    if reasoning:
                        reasoning_parts.append(reasoning)
                    if content:
                        content_parts.append(content)
                    if reasoning or content:
                        on_stream(
                            {
                                "reasoning_delta": reasoning,
                                "content_delta": content,
                                "reasoning_chars": sum(len(item) for item in reasoning_parts),
                                "content_chars": sum(len(item) for item in content_parts),
                            }
                        )
            content = "".join(content_parts)
            if finish_reason.casefold() in {"length", "max_tokens", "token_limit"}:
                raise ModelGatewayError(
                    "MODEL_OUTPUT_TRUNCATED",
                    "模型输出达到本轮 Token 上限，平台未执行可能被截断的 action；请提高单轮输出上限或让助手分轮继续。",
                    status_code=409,
                    details={
                        "finish_reason": finish_reason,
                        "received_content_chars": len(content),
                        "received_reasoning_chars": sum(len(item) for item in reasoning_parts),
                        "requested_max_tokens": max_tokens,
                    },
                )
            if not content.strip():
                raise ModelGatewayError("MODEL_EMPTY_RESPONSE", "上游模型没有返回正文。", status_code=502, retryable=True)
            return {"content": content, "reasoning_content": "".join(reasoning_parts), "usage": usage, "finish_reason": finish_reason}
        except HTTPError as exc:
            if cancel_event.is_set():
                raise ModelGatewayError("MODEL_CANCELLED", "模型请求已由用户取消。", status_code=409) from exc
            raw = exc.read(MODEL_RELAY_MIN_RESPONSE_BYTES + 1)
            if include_stream_usage and _model_relay_rejected_stream_usage(exc, raw):
                # Some OpenAI-compatible coding endpoints accept streaming but
                # reject the optional usage-only final chunk. Retry the exact
                # same model turn without that optional field; this does not
                # alter the task, action budget or BYOK lease.
                include_stream_usage = False
                on_stream({"reasoning_delta": "", "content_delta": "", "retry": {"attempt": attempt, "delay_seconds": 0, "reason": "stream_usage_unsupported"}})
                continue
            details = _model_relay_http_error_details(exc)
            retry_after = details.get("retry_after_seconds") if isinstance(details.get("retry_after_seconds"), int) else None
            last_error = ModelGatewayError(
                "MODEL_OVERLOADED" if exc.code == 429 else "MODEL_UPSTREAM_REJECTED",
                _model_relay_error_message(exc.code, raw),
                status_code=exc.code if 400 <= exc.code < 500 else 502,
                retryable=exc.code == 429 or exc.code >= 500,
                retry_after_seconds=retry_after,
                details=details,
            )
        except _ModelRelayConnectionFailure as exc:
            if cancel_event.is_set():
                raise ModelGatewayError("MODEL_CANCELLED", "模型请求已由用户取消。", status_code=409) from exc
            last_error = ModelGatewayError("MODEL_UPSTREAM_UNAVAILABLE", exc.message, status_code=502, retryable=True, details=exc.details)
        except TimeoutError:
            if cancel_event.is_set():
                raise ModelGatewayError("MODEL_CANCELLED", "模型请求已由用户取消。", status_code=409)
            last_error = ModelGatewayError("MODEL_UPSTREAM_TIMEOUT", "上游模型服务在规定时间内没有完成响应。", status_code=504, retryable=True)
        except (URLError, OSError) as exc:
            if cancel_event.is_set():
                raise ModelGatewayError("MODEL_CANCELLED", "模型请求已由用户取消。", status_code=409) from exc
            last_error = ModelGatewayError("MODEL_UPSTREAM_UNAVAILABLE", f"上游模型连接中断：{str(exc)[:300]}", status_code=502, retryable=True)
        except json.JSONDecodeError:
            last_error = ModelGatewayError("MODEL_RESPONSE_INVALID", "上游模型返回了无效 JSON。", status_code=502, retryable=False)
        finally:
            if cancel_watcher_stop is not None:
                cancel_watcher_stop.set()
            if response is not None:
                response.close()
        assert last_error is not None
        if not last_error.retryable or attempt >= 3:
            raise last_error
        delay = last_error.retry_after_seconds if last_error.retry_after_seconds is not None else min(12, 2 ** (attempt - 1))
        on_stream({"reasoning_delta": "", "content_delta": "", "retry": {"attempt": attempt + 1, "delay_seconds": delay, "reason": last_error.code}})
        if cancel_event.wait(delay):
            raise ModelGatewayError("MODEL_CANCELLED", "模型请求已由用户取消。", status_code=409)
    raise last_error or ModelGatewayError("MODEL_UPSTREAM_UNAVAILABLE", "模型服务不可用。")


build_worker = BuildWorker(workspace_root=WORKSPACE_ROOT, state_root=STATE_ROOT, artifact_store=store)
device_gateway = DeviceGateway(workspace_root=WORKSPACE_ROOT, state_root=STATE_ROOT)
parallel_workbench = ParallelWorkbench(STATE_ROOT, store, device_gateway)
app.include_router(create_parallel_router(parallel_workbench))
app.include_router(create_calibration_router(parallel_workbench.modeling.calibration))
app.include_router(create_workload_features_router(parallel_workbench.modeling.workload_features))
trace_library = TraceLibrary(STATE_ROOT, store, device_gateway, modeling=parallel_workbench.modeling)
app.include_router(create_trace_library_router(trace_library))
app.include_router(create_dpvs_router(STATE_ROOT))
agent_run_store = AgentRunStore(STATE_ROOT / "agent-runs")
agent_credential_leases = CredentialLeaseStore()
analysis_agent_sessions = AnalysisAgentSessionRegistry(STATE_ROOT / "agent-runs" / "analysis-sessions.json")
analysis_agent_session_lock = threading.RLock()
agent_cli_workspace_locks_guard = threading.RLock()
agent_cli_workspace_locks: dict[str, threading.RLock] = {}
experience_evolution = ExperienceEvolutionManager(
    STATE_ROOT / "experience-evolution",
    artifact_store=store,
    analysis_trace_store=analysis_trace_store,
    agent_run_store=agent_run_store,
)


def _agent_cli_workspace_lock(run_id: str) -> threading.RLock:
    """Serialize mutable workspace projections for one durable Agent Run."""

    normalized_run_id = str(run_id).strip()
    with agent_cli_workspace_locks_guard:
        lock = agent_cli_workspace_locks.get(normalized_run_id)
        if lock is None:
            lock = threading.RLock()
            agent_cli_workspace_locks[normalized_run_id] = lock
        return lock


def _bridge_artifact_url(job_id: str, token: str) -> str:
    # The Bridge resolves the relative URL against its administrator-configured
    # Platform origin.  The backend never needs to guess a public hostname.
    return f"/v1/device-bridges/jobs/{job_id}/artifact?token={token}"


agent_action_router = PlatformRuntimeActionRouter(
    artifact_store=store,
    build_worker=build_worker,
    device_gateway=device_gateway,
    default_source_connector=_default_connector,
    bridge_artifact_url=_bridge_artifact_url,
    curated_template_root=APP_ROOT / "agent-resources" / "hardware-api-templates",
    custom_api_kbs=custom_api_kbs,
    application_flows=application_flows,
    application_flow_runtime_traces=application_flow_runtime_traces,
    code_graphs=code_graphs,
    experience_evolution=experience_evolution,
    experimental_flows=experimental_flows,
    parallel_workbench=parallel_workbench,
)
legacy_agent_runtime = AgentRuntime(
    store=agent_run_store,
    leases=agent_credential_leases,
    model_gateway=_runtime_model_gateway,
    action_router=agent_action_router,
    system_prompt=_runtime_system_prompt,
)
AGENT_ENGINE_KIND = os.environ.get("HETERO_AGENT_ENGINE", "kimi_server").strip().lower()
kimi_internal_tokens = InternalToolTokenManager(STATE_ROOT)
kimi_agent_engine = KimiServerAgentEngine(
    KimiServerSettings.from_env(),
    log_root=STATE_ROOT / "kimi-engine" / "logs",
)
kimi_completion_adapter = KimiCompletionAdapter(
    kimi_agent_engine,
    root=STATE_ROOT / "kimi-engine" / "inference-workspaces",
)
agent_cli_adapters = AgentCliAdapterRegistry(
    kimi_binary=kimi_agent_engine.settings.binary,
    kimi_environment_factory=kimi_agent_engine.cli_process_environment,
    custom_manifest_path=optional_environment_path("HETERO_AGENT_CLI_CONFIG"),
    default_adapter_id=os.environ.get("HETERO_AGENT_CLI_ADAPTER", "kimi"),
    tolerate_manifest_errors=True,
)
if AGENT_ENGINE_KIND == "legacy":
    agent_runtime = legacy_agent_runtime
else:
    agent_runtime = KimiEngineAgentRuntime(
        store=agent_run_store,
        leases=agent_credential_leases,
        model_gateway=_runtime_model_gateway,
        action_router=agent_action_router,
        system_prompt=_runtime_system_prompt,
        engine=kimi_agent_engine,
        token_manager=kimi_internal_tokens,
        platform_api_base=os.environ.get("HETERO_PLATFORM_INTERNAL_URL", "http://127.0.0.1:8006"),
        app_root=APP_ROOT,
        source_connectors=connectors,
    )


def _record_agent_cli_exit(run_id: str, terminal_session_id: str, exit_code: int | None) -> None:
    """Leave a resumable Run when its browser terminal exits or API stops."""

    try:
        current = agent_run_store.read(run_id)
        if str(current.get("state") or "") == "cancelled":
            return
        current_transport = _record(_record(current.get("engine")).get("terminal_transport"))
        current_terminal_id = str(current_transport.get("terminal_session_id") or "")
        if terminal_session_id != "start_failed" and current_terminal_id != terminal_session_id:
            # An earlier PTY may finish after its replacement is already live.
            # It must never overwrite the replacement Run state.
            return

        def apply(value: dict[str, Any]) -> None:
            current_transport = _record(_record(value.get("engine")).get("terminal_transport"))
            active_terminal_id = str(current_transport.get("terminal_session_id") or "")
            if terminal_session_id != "start_failed" and active_terminal_id != terminal_session_id:
                return
            display_name = str(current_transport.get("agent_display_name") or "Agent CLI")
            value["state"] = "created"
            value["state_detail"] = f"{display_name} WebShell 已断开；同一页面 Run 可重新挂载。"
            engine = _record(value.get("engine"))
            engine["terminal_transport"] = {
                **current_transport,
                "kind": "pty",
                "terminal_session_id": terminal_session_id,
                "state": "exited",
                "exit_code": exit_code,
                "updated_at": utc_now(),
            }
            value["engine"] = engine

        agent_run_store.mutate(
            run_id,
            event_type="terminal.session_exited",
            event_state="created",
            payload={"terminal_session_id": terminal_session_id, "exit_code": exit_code},
            apply=apply,
        )
    except PlatformError:
        return


agent_cli_terminals = AgentCliTerminalManager(
    state_root=STATE_ROOT / "agent-cli-terminals",
    on_exit=_record_agent_cli_exit,
)
# Compatibility alias for extensions and tests written against v1 routes.
kimi_cli_terminals = agent_cli_terminals


def _bound_analysis_run(analysis_id: str) -> dict[str, Any] | None:
    """Resolve the durable Run, including a one-time pre-index migration."""

    normalized = normalize_analysis_id(analysis_id)
    run_id = analysis_agent_sessions.resolve(normalized)
    if run_id:
        try:
            return agent_run_store.read(run_id)
        except PlatformError:
            analysis_agent_sessions.forget(normalized, expected_run_id=run_id)
    migrated = agent_run_store.find_latest_by_analysis_id(normalized)
    if migrated is not None:
        analysis_agent_sessions.bind(normalized, str(migrated["run_id"]))
    return migrated


def _ensure_analysis_agent_session(analysis_id: str) -> tuple[dict[str, Any], Any]:
    """Return one Platform Run and Kimi binding for the whole analysis."""

    if not isinstance(agent_runtime, KimiEngineAgentRuntime):
        raise PlatformError("ANALYSIS_SESSION_ENGINE_UNAVAILABLE", "当前 Agent Engine 不支持分析级共享 Session。", status_code=409)
    normalized = normalize_analysis_id(analysis_id)
    with analysis_agent_session_lock:
        run = _bound_analysis_run(normalized)
        if run is None:
            public = agent_runtime.create(
                project_id=PROJECT_ID,
                goal="持续协助完成一条异构优化分析记录。",
                initial_input="分析会话已创建，等待当前阶段请求。",
                mode="page_agent",
                context={
                    "schema_version": "analysis_session_runtime_context/v1",
                    "analysis_record_id": normalized,
                },
                budget=AgentRunBudgetRequest().model_dump(),
                action_catalog_version=sha256_json(action_catalog()),
                skill_versions=[{"skill_id": str(item.get("skill_id")), "version": str(item.get("version"))} for item in skill_catalog()],
            )
            run = agent_run_store.read(str(public["run_id"]))
            analysis_agent_sessions.bind(normalized, str(run["run_id"]))
            agent_run_store.append_event(
                str(run["run_id"]),
                "analysis.session_bound",
                str(run.get("state") or "created"),
                {"analysis_id": normalized, "scope": "hardware_routing+opportunity_reasoning+device_validation+page_assistant"},
            )
            run = agent_run_store.read(str(run["run_id"]))
        binding = agent_runtime.ensure_session_binding(str(run["run_id"]))
        return agent_run_store.read(str(run["run_id"])), binding


def _structured_session_binding(request: ModelRelayChatCompletionRequest) -> tuple[str, Any | None]:
    if AGENT_ENGINE_KIND == "legacy" or not request.analysis_id:
        return "", None
    run, binding = _ensure_analysis_agent_session(request.analysis_id)
    # A page Agent can invoke routing or opportunity inference while its own
    # Kimi prompt is suspended on an owner=ui action. Re-entering that same
    # serialized Kimi Session would deadlock: the parent prompt waits for the UI
    # result while the child prompt waits for the parent Session lock. Keep the
    # owning Run for history and SSE observability, but execute this read-only
    # stage in an isolated completion Session. Its validated result is returned
    # to the one persistent page-Agent Session as the tool observation.
    if _record(run.get("active_engine_prompt")):
        return str(run["run_id"]), None
    return str(run["run_id"]), binding


def _structured_thinking_level(request: ModelRelayChatCompletionRequest) -> str | None:
    """Keep coarse directory routing fast without weakening later analysis."""

    if request.operation == "model_configuration_probe":
        return "low"
    return "low" if request.operation.startswith("hardware_semantic_routing") else None


def _append_structured_session_event(run_id: str, event_type: str, request: ModelRelayChatCompletionRequest, payload: dict[str, Any]) -> None:
    if not run_id:
        return
    run = agent_run_store.read(run_id)
    agent_run_store.append_event(
        run_id,
        event_type,
        str(run.get("state") or "created"),
        {
            "analysis_id": request.analysis_id,
            "operation": request.operation,
            "request_id": request.request_id,
            **payload,
        },
    )


def _set_structured_prompt_tool_policy(run_id: str, request: ModelRelayChatCompletionRequest, *, active: bool) -> None:
    if not run_id:
        return

    def apply(run: dict[str, Any]) -> None:
        current = _record(run.get("active_prompt_policy"))
        if active:
            run["active_prompt_policy"] = {
                "kind": "structured_read_only",
                "request_id": request.request_id,
                "operation": request.operation,
                "updated_at": utc_now(),
            }
        elif not request.request_id or current.get("request_id") == request.request_id:
            run["active_prompt_policy"] = {}

    run = agent_run_store.read(run_id)
    agent_run_store.mutate(
        run_id,
        event_type="analysis.tool_policy_activated" if active else "analysis.tool_policy_released",
        event_state=str(run.get("state") or "created"),
        payload={"request_id": request.request_id, "operation": request.operation, "policy": "structured_read_only"},
        apply=apply,
    )


def _mirror_bridge_progress_to_agent_run(event: dict[str, Any]) -> None:
    """Publish a transport progress frame on the owning Run's SSE stream.

    The Device Gateway retains the low-level audit log independently.  This
    mirror is intentionally best-effort so a stale/expired run cannot block an
    in-flight HDC job on a user's workstation.
    """

    run_id = str(event.get("agent_run_id") or "")
    if not run_id:
        return
    try:
        agent_run_store.append_event(
            run_id,
            "device.job_progress",
            "waiting_for_device",
            {
                "job_id": event.get("job_id"),
                "request_id": event.get("request_id"),
                "bridge_id": event.get("bridge_id"),
                "action_id": event.get("action_id"),
                "progress": event.get("progress") if isinstance(event.get("progress"), dict) else {},
            },
        )
    except PlatformError:
        return


device_gateway.set_run_event_sink(_mirror_bridge_progress_to_agent_run)
# Model credentials are intentionally process-local. Mark any orphaned work
# from a prior API process as resumable credential waits, then proactively
# start the backend-owned Kimi service. This must run on an actual ASGI service
# startup, not at module import: unit tests and offline tooling import
# ``backend.app`` and must never mutate a user's durable Runs or start services.
@app.on_event("startup")
def _recover_agent_runs_on_service_startup() -> None:
    parallel_workbench.recover_after_restart()
    agent_runtime.recover_after_restart()
    if isinstance(agent_runtime, KimiEngineAgentRuntime):
        try:
            kimi_agent_engine.start_service()
        except KimiServerError as exc:
            # Keep FastAPI available so the browser can show the precise Engine
            # diagnostic and deployment tooling can retry or repair it.
            logger.warning("Kimi Agent Engine startup failed: %s: %s", exc.code, exc.message)


@app.on_event("shutdown")
def _stop_agent_engine_on_service_shutdown() -> None:
    closers: list[tuple[str, Callable[[], None]]] = [("agent-cli-terminals", kimi_cli_terminals.close), ("parallel-workbench", parallel_workbench.shutdown)]
    if isinstance(agent_runtime, KimiEngineAgentRuntime):
        closers.append(("kimi-agent-engine", kimi_agent_engine.close))
    threads = [threading.Thread(target=closer, name=f"shutdown-{name}", daemon=True) for name, closer in closers]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + 6
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))


agent_run_idempotency = JsonRegistry(
    STATE_ROOT / "agent-runs" / "idempotency.json",
    {"schema_version": "agent_run_idempotency/v1", "entries": {}},
)
# Pairing codes and Bridge secrets must never be copied into the durable generic
# idempotency registry.  A short in-memory replay cache preserves ordinary HTTP
# retry behavior while deliberately disappearing on API restart; the private
# DeviceGateway registry remains the sole persistent holder of a bridge secret.
_secret_response_idempotency_lock = threading.RLock()
_secret_response_idempotency: dict[str, dict[str, Any]] = {}


def _require_idempotency_key(request: Request) -> str:
    key = str(request.headers.get("Idempotency-Key") or "").strip()
    if not re.fullmatch(r"[A-Za-z0-9._:-]{8,200}", key):
        raise PlatformError("IDEMPOTENCY_KEY_REQUIRED", "写操作需要格式正确的 Idempotency-Key。", status_code=422)
    return key


def _idempotency_lookup(scope: str, key: str, request_identity: dict[str, Any]) -> dict[str, Any] | None:
    """Return a previous public response for an idempotent write request."""

    identity = sha256_json(request_identity)
    registry = agent_run_idempotency.read()
    entry = _record(_record(registry.get("entries")).get(f"{scope}:{key}"))
    if not entry:
        return None
    if entry.get("request_sha256") != identity:
        raise PlatformError("IDEMPOTENCY_KEY_CONFLICT", "同一 Idempotency-Key 不能用于不同请求。", status_code=409)
    response = entry.get("response")
    return response if isinstance(response, dict) else None


def _idempotency_store(scope: str, key: str, request_identity: dict[str, Any], response: dict[str, Any]) -> None:
    registry = agent_run_idempotency.read()
    entries = _record(registry.get("entries"))
    entries[f"{scope}:{key}"] = {
        "request_sha256": sha256_json(request_identity),
        "response": response,
        "created_at": utc_now(),
    }
    # Keep the local registry bounded without changing live request semantics.
    if len(entries) > 5000:
        oldest = sorted(entries, key=lambda item: str(_record(entries[item]).get("created_at") or ""))[:1000]
        for item in oldest:
            entries.pop(item, None)
    registry["entries"] = entries
    agent_run_idempotency.write(registry)


def _secret_response_idempotency_lookup(scope: str, key: str, request_identity: dict[str, Any]) -> dict[str, Any] | None:
    """Replay a secret-bearing response without writing it to an audit file."""

    identity = sha256_json(request_identity)
    cache_key = f"{scope}:{key}"
    now = time.time()
    with _secret_response_idempotency_lock:
        expired = [entry_key for entry_key, entry in _secret_response_idempotency.items() if float(_record(entry).get("expires_at", 0)) <= now]
        for entry_key in expired:
            _secret_response_idempotency.pop(entry_key, None)
        entry = _record(_secret_response_idempotency.get(cache_key))
        if not entry:
            return None
        if entry.get("request_sha256") != identity:
            raise PlatformError("IDEMPOTENCY_KEY_CONFLICT", "同一 Idempotency-Key 不能用于不同请求。", status_code=409)
        response = entry.get("response")
        return dict(response) if isinstance(response, dict) else None


def _secret_response_idempotency_store(
    scope: str,
    key: str,
    request_identity: dict[str, Any],
    response: dict[str, Any],
    *,
    ttl_seconds: int,
) -> None:
    """Store one response only in process memory until its secret expires."""

    with _secret_response_idempotency_lock:
        _secret_response_idempotency[f"{scope}:{key}"] = {
            "request_sha256": sha256_json(request_identity),
            "response": dict(response),
            "expires_at": time.time() + max(60, min(ttl_seconds, 3600)),
        }


def _connector_id(requested: str | None) -> str:
    return requested or _default_connector()["connector_id"]


def _fixture_metadata(fixture_id: str) -> dict[str, Any]:
    fixture = FIXTURES.get(fixture_id)
    if fixture is None:
        raise PlatformError("FIXTURE_NOT_FOUND", "Fixture is not registered.", status_code=404, details={"fixture_id": fixture_id})
    path = fixture["path"]
    if not path.is_file():
        raise PlatformError("FIXTURE_NOT_FOUND", "Fixture file is unavailable in this checkout.", status_code=404, details={"fixture_id": fixture_id})
    return {
        "fixture_id": fixture_id,
        "kind": fixture["kind"],
        "label": fixture["label"],
        "description": fixture["description"],
        "path": str(path.relative_to(TRACE_FIXTURE_ROOT)) if TRACE_FIXTURE_ROOT is not None else path.name,
        "bytes": path.stat().st_size,
        "sha256": sha256_file(path),
    }


def _source_binding(connector_id: str, declared_revision: str | None) -> dict[str, Any]:
    return connectors.resolve_binding(connector_id, declared_revision)


_WORKSPACE_PROTECTED_PARTS = frozenset({".git", ".local-artifacts", "node_modules", "__pycache__"})
_WORKSPACE_PROTECTED_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
_WORKSPACE_NON_PATH_REFERENCE = re.compile(r"^(?:art_[a-f0-9]{24}|(?:artifact|trace|source):)", re.IGNORECASE)


def _workspace_file_target(source_connector_id: str, requested_path: str, *, must_exist: bool) -> tuple[dict[str, Any], Path, str]:
    """Resolve an Agent-visible text file beneath one editable local connector."""

    connector = connectors.get(source_connector_id)
    if connector.get("kind") != "local" or connector.get("mode") != "local_readonly":
        raise PlatformError(
            "WORKSPACE_FILE_ACCESS_UNSUPPORTED",
            "页面助手只能读写当前注册的本地代码仓，托管克隆保持只读。",
            status_code=409,
        )
    normalized = requested_path.replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    if _WORKSPACE_NON_PATH_REFERENCE.match(normalized):
        raise PlatformError(
            "WORKSPACE_FILE_IDENTIFIER_NOT_PATH",
            "该值是工件或证据标识，不是代码仓文件路径。请直接使用任务中已注入的内容，或提供明确的代码仓相对路径。",
            status_code=422,
            details={"identifier": normalized[:160]},
        )
    relative = Path(normalized)
    if not normalized or relative.is_absolute() or ".." in relative.parts:
        raise PlatformError("WORKSPACE_FILE_PATH_INVALID", "页面助手只能访问当前代码仓内的相对路径。", status_code=422)
    lower_name = relative.name.casefold()
    if (
        any(part.casefold() in _WORKSPACE_PROTECTED_PARTS for part in relative.parts)
        or lower_name == ".env"
        or lower_name.startswith(".env.")
        or lower_name in {"id_rsa", "credentials", "credentials.json"}
        or lower_name.endswith(_WORKSPACE_PROTECTED_SUFFIXES)
    ):
        raise PlatformError("WORKSPACE_FILE_PATH_PROTECTED", "该路径属于页面助手的受保护范围。", status_code=403)
    root = Path(str(connector["source_root"])).resolve()
    target = (root / relative).resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise PlatformError("WORKSPACE_FILE_PATH_INVALID", "页面助手只能访问当前代码仓内的相对路径。", status_code=422) from exc
    if must_exist and not target.is_file():
        raise PlatformError("WORKSPACE_FILE_NOT_FOUND", "当前代码仓中找不到该文本文件。", status_code=404, details={"path": normalized})
    if target.exists() and not target.is_file():
        raise PlatformError("WORKSPACE_FILE_INVALID", "该路径不是可读写的普通文件。", status_code=422, details={"path": normalized})
    return connector, target, target.relative_to(root).as_posix()


def _read_workspace_text(path: Path, max_bytes: int) -> tuple[str, bool, int]:
    size = path.stat().st_size
    with path.open("rb") as handle:
        data = handle.read(max_bytes + 1)
    if b"\x00" in data:
        raise PlatformError("WORKSPACE_FILE_BINARY", "页面助手只读取 UTF-8 文本源码。", status_code=422)
    try:
        content = data[:max_bytes].decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PlatformError("WORKSPACE_FILE_ENCODING", "页面助手只读取 UTF-8 文本源码。", status_code=422) from exc
    return content, len(data) > max_bytes, size


def _atomic_write_workspace_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".agent.tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _attested_local_snapshot_binding(
    connector: dict[str, Any],
    snapshot: dict[str, Any],
    worktree_fingerprint: dict[str, Any],
) -> dict[str, Any]:
    """Build an exact development binding for a user-attested local workspace.

    A raw trace does not carry a worktree-content identity.  The user/Agent
    attestation supplies the device-build assertion, while this binding records
    the selected local source snapshot and the content fingerprint of its
    uncommitted files.  A later AST run rechecks both values.
    """

    snapshot_id = str(snapshot.get("snapshot_id") or "")
    fingerprint_sha256 = str(worktree_fingerprint.get("sha256") or "")
    fingerprint_algorithm = str(worktree_fingerprint.get("algorithm") or "")
    if not snapshot_id or not fingerprint_sha256 or not fingerprint_algorithm:
        raise PlatformError(
            "SOURCE_WORKTREE_FINGERPRINT_INVALID",
            "The selected local source snapshot has no usable worktree content fingerprint.",
            status_code=422,
        )
    return {
        "declared_revision": snapshot_id,
        "resolved_snapshot_id": snapshot_id,
        "match_level": "exact",
        "resolver_version": "1.1",
        "snapshot_state": "clean" if bool(snapshot.get("clean")) else "dirty",
        "worktree_fingerprint_algorithm": fingerprint_algorithm,
        "worktree_fingerprint_sha256": fingerprint_sha256,
        "worktree_changed_path_count": int(worktree_fingerprint.get("changed_path_count") or 0),
        "worktree_dirty_repository_count": int(worktree_fingerprint.get("dirty_repository_count") or 0),
        "source_snapshot_artifact_id": connector.get("snapshot_artifact_id"),
    }


def _rebound_trace_analysis_payload(
    analysis: dict[str, Any],
    *,
    binding_artifact_id: str,
    attestation_actor: str,
) -> dict[str, Any]:
    """Copy trace facts into a new immutable analysis artifact with binding provenance."""

    payload = {
        key: value
        for key, value in analysis.items()
        if key not in TRACE_ANALYSIS_ENVELOPE_FIELDS
    }
    payload["source_binding_attestation"] = {
        "binding_artifact_id": binding_artifact_id,
        "bound_from_analysis_artifact_id": analysis["artifact_id"],
        "method": "explicit_current_snapshot_attestation",
        "attestation_actor": attestation_actor,
    }
    return payload


def _write_trace_result(
    normalized: dict[str, Any],
    *,
    connector_id: str,
    declared_revision: str | None,
    producer: dict[str, str],
    input_refs: list[str],
    import_instance_id: str = "",
) -> dict[str, Any]:
    payload = dict(normalized)
    if import_instance_id:
        payload["trace_import"] = {
            "import_instance_id": import_instance_id,
            "uploaded_at": utc_now(),
        }
    return store.write(
        "trace_analysis_result",
        payload,
        source_connector_id=connector_id,
        source_binding=_source_binding(connector_id, declared_revision),
        producer=producer,
        input_artifacts=input_refs,
    )


def _write_raw_trace_input(
    *,
    name: str,
    size: int,
    input_sha256: str,
    connector_id: str,
    binding: dict[str, Any],
    input_kind: str,
    managed_path: str | None = None,
    fixture_id: str | None = None,
    import_instance_id: str = "",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "file_name": name,
        "bytes": size,
        "input_sha256": input_sha256,
        "input_kind": input_kind,
    }
    if managed_path:
        payload["managed_path"] = managed_path
    if fixture_id:
        payload["fixture_id"] = fixture_id
        payload["input_origin"] = "registered_fixture"
    if import_instance_id:
        payload["trace_import"] = {
            "import_instance_id": import_instance_id,
            "uploaded_at": utc_now(),
        }
    return store.write(
        "raw_trace_input",
        payload,
        source_connector_id=connector_id,
        source_binding=binding,
        producer={"name": "trace-upload", "version": "1.1.0"},
    )


def _write_htrace_analysis_result(
    *,
    path: Path,
    name: str,
    input_sha256: str,
    connector_id: str,
    declared_revision: str | None,
    raw_input_artifact: dict[str, Any],
    import_instance_id: str = "",
) -> dict[str, Any]:
    """Run bounded raw-htrace extraction and link every derived artifact."""

    binding = _source_binding(connector_id, declared_revision)
    try:
        result = analyze_htrace(path, state_root=STATE_ROOT, workspace_root=WORKSPACE_ROOT)
    except PlatformError as exc:
        store.write(
            "htrace_analysis_failure",
            {
                "file_name": name,
                "input_sha256": input_sha256,
                "error_code": exc.code,
                "reason": exc.message,
                "details": exc.details,
            },
            source_connector_id=connector_id,
            source_binding=binding,
            producer={"name": "raw-htrace-analysis", "version": "1.1.0"},
            input_artifacts=[f"artifact:{raw_input_artifact['artifact_id']}", f"sha256:{input_sha256}"],
        )
        raise

    conversion_artifact = store.write(
        "htrace_conversion",
        {"input_sha256": input_sha256, **result["conversion"]},
        source_connector_id=connector_id,
        source_binding=binding,
        producer={"name": "raw-htrace-analysis", "version": "1.1.0"},
        input_artifacts=[f"artifact:{raw_input_artifact['artifact_id']}", f"sha256:{input_sha256}"],
    )
    normalized = dict(result["normalized"])
    normalized["htrace_conversion_artifact"] = f"artifact:{conversion_artifact['artifact_id']}"
    for hotspot in normalized.get("hotspot_candidates", []):
        if isinstance(hotspot, dict):
            refs = hotspot.setdefault("evidence_refs", [])
            if isinstance(refs, list):
                refs.append(f"artifact:{conversion_artifact['artifact_id']}")
    return _write_trace_result(
        normalized,
        connector_id=connector_id,
        declared_revision=declared_revision,
        producer={"name": "raw-htrace-analysis", "version": "1.1.0"},
        input_refs=[
            f"sha256:{input_sha256}",
            f"artifact:{raw_input_artifact['artifact_id']}",
            f"artifact:{conversion_artifact['artifact_id']}",
        ],
        import_instance_id=import_instance_id,
    )


def _load_json_records(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


TRUSTED_KB_CONFIDENCE = frozenset({"confirmed", "source_confirmed", "source_confirmed_integration"})


def _load_kb_artifact(path: Path, artifact_type: str, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise PlatformError("KB_VERSION_INCOMPATIBLE", f"{label} is unavailable.", status_code=503)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PlatformError("KB_VERSION_INCOMPATIBLE", f"{label} is not valid JSON.", status_code=503) from exc
    if not isinstance(payload, dict) or payload.get("artifact_type") != artifact_type or not isinstance(payload.get("records"), list):
        raise PlatformError("KB_VERSION_INCOMPATIBLE", f"{label} does not satisfy the required artifact contract.", status_code=503)
    return payload


def _optimization_kb() -> dict[str, Any]:
    return _load_kb_artifact(OPTIMIZATION_KB_PATH, "optimization_knowledge_base", "Optimization knowledge base")


def _hardware_kb() -> dict[str, Any]:
    return _load_kb_artifact(HARDWARE_KB_PATH, "hardware_knowledge_base", "Hardware knowledge base")


def _npu_api_kb() -> dict[str, Any]:
    return _load_kb_artifact(NPU_API_KB_PATH, "npu_api_knowledge_base", "NPU API knowledge base")


def _accelerator_api_kb(accelerator_unit: str) -> dict[str, Any]:
    if accelerator_unit not in ACCELERATOR_UNITS:
        raise PlatformError("ACCELERATOR_API_UNIT_NOT_FOUND", f"Unsupported accelerator API unit: {accelerator_unit}.", status_code=404)
    path = ACCELERATOR_API_KB_PATHS.get(accelerator_unit)
    if path is None:
        raise PlatformError("ACCELERATOR_API_UNIT_NOT_FOUND", f"Accelerator API catalog is unavailable: {accelerator_unit}.", status_code=404)
    payload = _load_kb_artifact(path, "accelerator_api_knowledge_base", f"{accelerator_unit.upper()} API knowledge base")
    if payload.get("accelerator_unit") != accelerator_unit:
        raise PlatformError("KB_VERSION_INCOMPATIBLE", f"{accelerator_unit.upper()} API knowledge base has an invalid unit contract.", status_code=503)
    manifest = _accelerator_api_manifest()
    entry = next(
        (
            item
            for item in manifest.get("entries", [])
            if isinstance(item, dict) and item.get("accelerator_unit") == accelerator_unit
        ),
        None,
    )
    summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
    if not isinstance(entry, dict) or any(
        entry.get(field) != actual
        for field, actual in (
            ("kb_id", payload.get("kb_id")),
            ("kb_version", payload.get("kb_version")),
            ("content_sha256", payload.get("content_sha256")),
            ("record_count", summary.get("record_count")),
        )
    ):
        raise PlatformError("KB_VERSION_INCOMPATIBLE", f"Accelerator API manifest disagrees with {accelerator_unit} catalog.", status_code=503)
    return payload


def _accelerator_api_manifest() -> dict[str, Any]:
    if not ACCELERATOR_API_MANIFEST_PATH.is_file():
        raise PlatformError("KB_VERSION_INCOMPATIBLE", "Accelerator API knowledge manifest is unavailable.", status_code=503)
    try:
        payload = json.loads(ACCELERATOR_API_MANIFEST_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise PlatformError("KB_VERSION_INCOMPATIBLE", "Accelerator API knowledge manifest is not valid JSON.", status_code=503) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("artifact_type") != "accelerator_api_knowledge_manifest"
        or not isinstance(payload.get("entries"), list)
    ):
        raise PlatformError("KB_VERSION_INCOMPATIBLE", "Accelerator API knowledge manifest does not satisfy the required contract.", status_code=503)
    return payload


def _accelerator_api_kbs() -> dict[str, dict[str, Any]]:
    payloads = {unit_id: _accelerator_api_kb(unit_id) for unit_id in sorted(ACCELERATOR_UNITS)}
    manifest = _accelerator_api_manifest()
    entries = {
        str(item.get("accelerator_unit")): item
        for item in manifest.get("entries", [])
        if isinstance(item, dict) and isinstance(item.get("accelerator_unit"), str)
    }
    if set(entries) != set(payloads):
        raise PlatformError("KB_VERSION_INCOMPATIBLE", "Accelerator API manifest does not cover the configured units.", status_code=503)
    for unit_id, payload in payloads.items():
        entry = entries[unit_id]
        mismatched = [
            field
            for field, actual in (
                ("kb_id", payload.get("kb_id")),
                ("kb_version", payload.get("kb_version")),
                ("content_sha256", payload.get("content_sha256")),
                ("record_count", (payload.get("summary") or {}).get("record_count") if isinstance(payload.get("summary"), dict) else None),
            )
            if entry.get(field) != actual
        ]
        if mismatched:
            raise PlatformError(
                "KB_VERSION_INCOMPATIBLE",
                f"Accelerator API manifest disagrees with {unit_id} catalog: {', '.join(mismatched)}.",
                status_code=503,
            )
    return payloads


def _record_provenance(item: dict[str, Any]) -> dict[str, Any]:
    value = item.get("provenance")
    return value if isinstance(value, dict) else {}


def _normalized_kb_records(payload: dict[str, Any], *, hardware: bool) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for item in payload["records"]:
        if not isinstance(item, dict):
            continue
        record_id = str(item.get("record_id") or item.get("case_id") or item.get("hardware_id") or "")
        if not record_id:
            continue
        provenance = _record_provenance(item)
        raw_record = item.get("record") if isinstance(item.get("record"), dict) else item
        records.append(
            {
                "record_type": str(item.get("record_type") or ("hardware_capability" if hardware else "optimization_record")),
                "record_id": record_id,
                "hardware_id": item.get("hardware_id") if hardware else None,
                "name": item.get("name") or item.get("algorithm") or item.get("hardware_id") or record_id,
                "algorithm": item.get("algorithm"),
                "target_unit": item.get("hardware_id") if hardware else item.get("target_unit"),
                "confidence": provenance.get("confidence", item.get("strict_status", "unverified")),
                "provenance": provenance,
                "record": raw_record,
            }
        )
    return records


def _optimization_kb_records() -> list[dict[str, Any]]:
    return _normalized_kb_records(_optimization_kb(), hardware=False)


def _hardware_kb_records() -> list[dict[str, Any]]:
    return _normalized_kb_records(_hardware_kb(), hardware=True)


def _npu_api_reasoning_records() -> list[dict[str, Any]]:
    """Return bounded NPU operation semantics for model-context retrieval.

    Full Doxygen text stays in the immutable API artifact and its detail
    endpoint.  The reasoning path only receives the semantic, source-contract,
    and runtime-gate fields it needs to assess a candidate.
    """

    compact: list[dict[str, Any]] = []
    for raw in _npu_api_kb().get("records", []):
        if not isinstance(raw, dict) or raw.get("record_type") != "npu_operation":
            continue
        source = raw.get("source_contract") if isinstance(raw.get("source_contract"), dict) else {}
        integration = raw.get("source_integration_evidence") if isinstance(raw.get("source_integration_evidence"), dict) else {}
        compact.append(
            {
                "record_id": raw.get("record_id"),
                "record_type": raw.get("record_type"),
                "name": raw.get("name_zh") or raw.get("symbol"),
                "symbol": raw.get("symbol"),
                "operation_id": raw.get("operation_id"),
                "api_layer": raw.get("api_layer"),
                "one_line_abstract_zh": raw.get("one_line_abstract_zh"),
                "detailed_description_zh": raw.get("detailed_description_zh"),
                "semantic_family": raw.get("semantic_family"),
                "code_feature_match": raw.get("code_feature_match"),
                "construction_path": raw.get("construction_path"),
                "capability_boundary": raw.get("capability_boundary"),
                "runtime_verification": raw.get("runtime_verification"),
                "source_integration_evidence": {
                    "strict_case_ids": _limited_strings(integration.get("strict_case_ids"), 8),
                    "statement_zh": integration.get("statement_zh"),
                },
                "source_contract": {
                    key: source.get(key)
                    for key in ("source_file", "source_line", "system_capability", "library", "since", "version", "source_doc_brief_en", "source_constraints_en")
                    if source.get(key) is not None
                },
                "confidence": "source_interface_confirmed",
            }
        )
    return sorted(compact, key=lambda item: int(item.get("operation_id") or 0))


def _npu_api_catalog_summary(raw: dict[str, Any]) -> dict[str, Any]:
    """Project a browseable NPU API record without shipping full header docs.

    The full source contract stays available through the detail endpoint.  This
    keeps the initial 146-record catalogue quick to render while making the
    browser, API search, and reasoning context all refer to one immutable KB.
    """

    family = raw.get("semantic_family") if isinstance(raw.get("semantic_family"), dict) else {}
    integration = raw.get("source_integration_evidence") if isinstance(raw.get("source_integration_evidence"), dict) else {}
    boundary = raw.get("capability_boundary") if isinstance(raw.get("capability_boundary"), dict) else {}
    source = raw.get("source_contract") if isinstance(raw.get("source_contract"), dict) else {}
    return {
        "record_id": raw.get("record_id"),
        "record_type": raw.get("record_type"),
        "symbol": raw.get("symbol"),
        "name": raw.get("name_zh") or raw.get("symbol"),
        "one_line_abstract_zh": raw.get("one_line_abstract_zh"),
        "detailed_description_zh": raw.get("detailed_description_zh"),
        "operation_id": raw.get("operation_id"),
        "semantic_family": {
            key: family.get(key)
            for key in ("id", "label_zh")
            if family.get(key) is not None
        },
        "api_layer": raw.get("api_layer"),
        "api_layer_label_zh": raw.get("api_layer_label_zh"),
        "workflow_role": raw.get("workflow_role"),
        "strict_case_ids": _limited_strings(integration.get("strict_case_ids"), 8),
        "device_availability": boundary.get("target_device_availability", "evidence_required"),
        "source_contract": {
            key: source.get(key)
            for key in ("source_file", "source_line", "system_capability", "library", "since", "version")
            if source.get(key) is not None
        },
    }


def _npu_api_catalog_records(query: str = "") -> list[dict[str, Any]]:
    """Return source-derived records with stable, user-facing ordering."""

    normalized = query.casefold().strip()
    scored_records: list[tuple[int, dict[str, Any]]] = []
    for raw in _npu_api_kb().get("records", []):
        if not isinstance(raw, dict):
            continue
        family = raw.get("semantic_family") if isinstance(raw.get("semantic_family"), dict) else {}
        code_features = raw.get("code_feature_match") if isinstance(raw.get("code_feature_match"), dict) else {}
        exact_fields = (raw.get("record_id"), raw.get("symbol"), raw.get("name_zh"))
        semantic_fields = (
            family.get("id"),
            family.get("label_zh"),
            raw.get("api_layer"),
            raw.get("api_layer_label_zh"),
            raw.get("workflow_role"),
            *(_limited_strings(code_features.get("operator_aliases"), 32)),
        )
        descriptive_fields = (raw.get("one_line_abstract_zh"), raw.get("detailed_description_zh"))
        exact_hit = any(normalized in str(value).casefold() for value in exact_fields if value is not None)
        semantic_hit = any(normalized in str(value).casefold() for value in semantic_fields if value is not None)
        descriptive_hit = any(normalized in str(value).casefold() for value in descriptive_fields if value is not None)
        if normalized and not (exact_hit or semantic_hit or descriptive_hit):
            continue
        score = 0
        if normalized:
            # Names/symbols denote the API's own semantics.  Family prose often
            # mentions neighbouring operators (for example activation fused
            # with MatMul), so it must never outrank a direct name match.
            score = 100 if exact_hit else 50 if semantic_hit else 10
        scored_records.append((score, _npu_api_catalog_summary(raw)))

    def order_key(record: dict[str, Any]) -> tuple[int, int, str]:
        is_operation = record.get("record_type") == "npu_operation"
        return (0 if is_operation else 1, int(record.get("operation_id") or 0), str(record.get("symbol") or ""))

    return [record for _, record in sorted(scored_records, key=lambda item: (-item[0], order_key(item[1])))]


def _accelerator_api_reasoning_records() -> dict[str, list[dict[str, Any]]]:
    """Return bounded GPU/DSP/media API semantics for strategy contexts.

    The complete source excerpts remain in the immutable catalog/detail API.
    Model contexts only need the candidate's data shape, source exposure, and
    runtime gate to decide which verification step is still missing.
    """

    result: dict[str, list[dict[str, Any]]] = {}
    for unit_id, payload in _accelerator_api_kbs().items():
        compact: list[dict[str, Any]] = []
        for raw in payload.get("records", []):
            if not isinstance(raw, dict) or raw.get("record_type") != "accelerator_api_entrypoint":
                continue
            source = raw.get("source_contract") if isinstance(raw.get("source_contract"), dict) else {}
            integration = raw.get("source_integration_evidence") if isinstance(raw.get("source_integration_evidence"), dict) else {}
            compact.append(
                {
                    "record_id": raw.get("record_id"),
                    "record_type": raw.get("record_type"),
                    "accelerator_unit": raw.get("accelerator_unit"),
                    "name": raw.get("name_zh") or raw.get("symbol"),
                    "symbol": raw.get("symbol"),
                    "one_line_abstract_zh": raw.get("one_line_abstract_zh"),
                    "detailed_description_zh": raw.get("detailed_description_zh"),
                    "semantic_family": raw.get("semantic_family"),
                    "code_feature_match": raw.get("code_feature_match"),
                    "capability_boundary": raw.get("capability_boundary"),
                    "runtime_verification": raw.get("runtime_verification"),
                    "source_integration_evidence": {
                        "strict_case_ids": _limited_strings(integration.get("strict_case_ids"), 8),
                        "statement_zh": integration.get("statement_zh"),
                    },
                    "source_contract": {
                        key: source.get(key)
                        for key in ("source_file", "source_line", "system_capability", "library", "exposure")
                        if source.get(key) is not None
                    },
                    "confidence": "source_interface_confirmed",
                }
            )
        result[unit_id] = sorted(compact, key=lambda item: str(item.get("symbol") or ""))
    return result


def _custom_api_reasoning_catalogs() -> dict[str, dict[str, Any]]:
    """Load isolated user catalogs as dynamic route/API directories."""

    return custom_api_kbs.reasoning_catalogs()


def _hardware_records_with_custom_catalogs(custom_catalogs: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    records = list(_hardware_reasoning_records())
    records.extend(
        catalog["routing_record"]
        for _unit_id, catalog in sorted(custom_catalogs.items())
        if isinstance(catalog.get("routing_record"), dict)
    )
    return records


def _accelerator_api_catalog_summary(raw: dict[str, Any]) -> dict[str, Any]:
    family = raw.get("semantic_family") if isinstance(raw.get("semantic_family"), dict) else {}
    integration = raw.get("source_integration_evidence") if isinstance(raw.get("source_integration_evidence"), dict) else {}
    boundary = raw.get("capability_boundary") if isinstance(raw.get("capability_boundary"), dict) else {}
    source = raw.get("source_contract") if isinstance(raw.get("source_contract"), dict) else {}
    return {
        "record_id": raw.get("record_id"),
        "record_type": raw.get("record_type"),
        "accelerator_unit": raw.get("accelerator_unit"),
        "symbol": raw.get("symbol"),
        "name": raw.get("name_zh") or raw.get("symbol"),
        "one_line_abstract_zh": raw.get("one_line_abstract_zh"),
        "detailed_description_zh": raw.get("detailed_description_zh"),
        "semantic_family": {
            key: family.get(key)
            for key in ("id", "label_zh")
            if family.get(key) is not None
        },
        "strict_case_ids": _limited_strings(integration.get("strict_case_ids"), 8),
        "device_availability": boundary.get("target_device_availability", "evidence_required"),
        "exposure": source.get("exposure"),
        "source_contract": {
            key: source.get(key)
            for key in ("source_file", "source_line", "system_capability", "library")
            if source.get(key) is not None
        },
    }


def _accelerator_api_catalog_records(accelerator_unit: str, query: str = "") -> list[dict[str, Any]]:
    """Browse a unit-specific source-derived API catalog with stable ordering."""

    normalized = query.casefold().strip()
    scored_records: list[tuple[int, dict[str, Any]]] = []
    for raw in _accelerator_api_kb(accelerator_unit).get("records", []):
        if not isinstance(raw, dict):
            continue
        family = raw.get("semantic_family") if isinstance(raw.get("semantic_family"), dict) else {}
        features = raw.get("code_feature_match") if isinstance(raw.get("code_feature_match"), dict) else {}
        source = raw.get("source_contract") if isinstance(raw.get("source_contract"), dict) else {}
        exact_fields = (raw.get("record_id"), raw.get("symbol"), raw.get("name_zh"))
        semantic_fields = (
            family.get("id"),
            family.get("label_zh"),
            source.get("exposure"),
            *(_limited_strings(features.get("operator_aliases"), 32)),
        )
        descriptive_fields = (raw.get("one_line_abstract_zh"), raw.get("detailed_description_zh"))
        exact_hit = any(normalized in str(value).casefold() for value in exact_fields if value is not None)
        semantic_hit = any(normalized in str(value).casefold() for value in semantic_fields if value is not None)
        descriptive_hit = any(normalized in str(value).casefold() for value in descriptive_fields if value is not None)
        if normalized and not (exact_hit or semantic_hit or descriptive_hit):
            continue
        score = 100 if normalized and exact_hit else 50 if normalized and semantic_hit else 10 if normalized else 0
        scored_records.append((score, _accelerator_api_catalog_summary(raw)))
    return [record for _, record in sorted(scored_records, key=lambda item: (-item[0], str(item[1].get("symbol") or "")))]


def _limited_strings(value: Any, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, (str, int, float))][:limit]


def _compact_provenance(value: Any, *, reference_limit: int = 6) -> dict[str, Any]:
    provenance = value if isinstance(value, dict) else {}
    return {
        "origin": provenance.get("origin"),
        "confidence": provenance.get("confidence"),
        "references": _limited_strings(provenance.get("references"), reference_limit),
        "device_availability": provenance.get("device_availability"),
        "performance_evidence": provenance.get("performance_evidence"),
    }


def _compact_cpu_ast_profile(value: Any) -> dict[str, Any]:
    profile = value if isinstance(value, dict) else {}
    features = profile.get("ast_features") if isinstance(profile.get("ast_features"), dict) else {}
    control_flow = features.get("control_flow") if isinstance(features.get("control_flow"), dict) else {}
    branches = control_flow.get("branches") if isinstance(control_flow.get("branches"), dict) else {}
    function = profile.get("function") if isinstance(profile.get("function"), dict) else {}
    source = function.get("source") if isinstance(function.get("source"), dict) else {}
    compute_core = profile.get("compute_core") if isinstance(profile.get("compute_core"), dict) else {}
    calls = features.get("calls") if isinstance(features.get("calls"), list) else []
    return {
        "binding_status": profile.get("binding_status"),
        "matchability": (profile.get("matchability") or {}).get("status") if isinstance(profile.get("matchability"), dict) else None,
        "compute_core": {
            "matching_status": compute_core.get("matching_status"),
            "status": compute_core.get("status"),
            "reason": compute_core.get("reason"),
        },
        "function": {
            "qualified_name": function.get("qualified_name"),
            "signature": function.get("signature"),
            "source_reference": source.get("reference"),
        },
        "ast_features": {
            "backend": features.get("ast_backend"),
            "parse_status": features.get("parse_status"),
            "algorithm_tags": _limited_strings(features.get("algorithm_tags"), 8),
            "types_used": _limited_strings(features.get("types_used"), 8),
            "operators": _limited_strings(features.get("operators"), 8),
            "memory_access": features.get("memory_access", {}),
            "code_metrics": features.get("code_metrics", {}),
            "loop_count": len(control_flow.get("loops", [])) if isinstance(control_flow.get("loops"), list) else 0,
            "branch_count": branches.get("if", 0) + branches.get("switch", 0) + branches.get("ternary", 0),
            "max_loop_nesting": control_flow.get("max_loop_nesting", 0),
            "callees": [str(item.get("simple_name") or item.get("callee")) for item in calls if isinstance(item, dict)][:8],
        },
    }


def _compact_cpu_code_feature_summary(value: Any) -> dict[str, Any]:
    """Preserve matchable CPU evidence without putting a full AST profile in RAG context."""

    summary = value if isinstance(value, dict) else {}
    structured = summary.get("structured_features") if isinstance(summary.get("structured_features"), dict) else {}
    memory = structured.get("memory_access") if isinstance(structured.get("memory_access"), dict) else {}
    control = structured.get("control_flow") if isinstance(structured.get("control_flow"), dict) else {}
    computation = structured.get("computation") if isinstance(structured.get("computation"), dict) else {}
    parallelism = structured.get("parallelism") if isinstance(structured.get("parallelism"), dict) else {}
    text = str(summary.get("summary") or "")
    return {
        "availability": summary.get("availability"),
        "summary": text[:1200] + ("…" if len(text) > 1200 else ""),
        "matching_guidance": summary.get("matching_guidance"),
        "structured_features": {
            "binding_status": structured.get("binding_status"),
            "compute_core_status": structured.get("compute_core_status"),
            "algorithm_tags": _limited_strings(structured.get("algorithm_tags"), 8),
            "data_type_evidence": _limited_strings(structured.get("data_type_evidence"), 8),
            "memory_access": {
                "classification": memory.get("classification"),
                "pointer_expression_count": memory.get("pointer_expression_count"),
                "array_subscript_count": memory.get("array_subscript_count"),
                "assignment_count": memory.get("assignment_count"),
                "continuity": memory.get("continuity"),
            },
            "control_flow": {
                "loop_count": control.get("loop_count"),
                "branch_count": control.get("branch_count"),
                "max_loop_nesting": control.get("max_loop_nesting"),
            },
            "computation": {
                "math_calls": _limited_strings(computation.get("math_calls"), 8),
                "notable_callees": _limited_strings(computation.get("notable_callees"), 8),
                "operators": _limited_strings(computation.get("operators"), 8),
            },
            "parallelism": {
                "openmp_pragma_count": parallelism.get("openmp_pragma_count"),
                "classification": parallelism.get("classification"),
            },
        },
    }


def _optimization_reasoning_records() -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for item in _optimization_kb_records():
        raw = item.get("record") if isinstance(item.get("record"), dict) else item
        record = {
            "record_id": item["record_id"],
            "record_type": item["record_type"],
            "name": item["name"],
            "algorithm": item.get("algorithm"),
            "target_unit": item.get("target_unit"),
            "confidence": item.get("confidence"),
            "provenance": _compact_provenance(item.get("provenance")),
        }
        if item["record_type"] == "strict_heterogeneous_case":
            fallback = raw.get("cpu_software_fallback") if isinstance(raw.get("cpu_software_fallback"), dict) else {}
            accelerator = raw.get("accelerator") if isinstance(raw.get("accelerator"), dict) else {}
            record.update(
                {
                    "source_group": raw.get("source_group"),
                    "module": raw.get("module"),
                    "pair_shape": raw.get("pair_shape"),
                    "accelerator": {
                        "kind": raw.get("accelerator_kind"),
                        "implementation": accelerator.get("implementation"),
                        "reference": accelerator.get("reference"),
                    },
                    "cpu_software_fallback": {
                        "implementation": fallback.get("implementation"),
                        "execution_domain": fallback.get("execution_domain"),
                        "reference": fallback.get("reference"),
                    },
                    "cpu_fallback_ast": _compact_cpu_ast_profile(raw.get("cpu_fallback_ast_profile")),
                    "cpu_code_features": _compact_cpu_code_feature_summary(raw.get("cpu_code_feature_summary")),
                }
            )
        else:
            record["optimization_details"] = {
                key: raw.get(key)
                for key in (
                    "algorithm",
                    "description",
                    "target_unit",
                    "constraints",
                    "preconditions",
                    "operations",
                    "data_types",
                    "memory_patterns",
                    "cost_factors",
                    "fallback",
                )
                if key in raw
            }
        compact.append(record)
    return compact


def _hardware_reasoning_records() -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for item in _hardware_kb_records():
        raw = item.get("record") if isinstance(item.get("record"), dict) else item
        source_capability = raw.get("source_capability_evidence") if isinstance(raw.get("source_capability_evidence"), dict) else {}
        compact.append(
            {
                "record_id": item["record_id"],
                "record_type": item["record_type"],
                "hardware_id": item.get("hardware_id") or raw.get("hardware_id") or item["record_id"],
                "name": item["name"],
                "unit_type": raw.get("unit_type"),
                "software_interface": raw.get("software_interface"),
                "capability_scope": raw.get("capability_scope"),
                "device_availability": raw.get("device_availability"),
                "performance_envelope": raw.get("performance_envelope"),
                "supported_algorithms": _limited_strings(raw.get("supported_algorithms_from_strict_cases"), 12),
                "strict_case_count": len(raw.get("strict_case_ids", [])) if isinstance(raw.get("strict_case_ids"), list) else 0,
                "strict_case_ids": _limited_strings(raw.get("strict_case_ids"), 8),
                "implementation_paths": _limited_strings(raw.get("implementation_paths"), 8),
                "constraints": _limited_strings(raw.get("constraints"), 6),
                "source_references": _limited_strings(raw.get("source_references"), 6),
                "source_capability_summary": {
                    "name": source_capability.get("name"),
                    "unit_type": source_capability.get("unit_type"),
                    "confidence": (source_capability.get("provenance") or {}).get("confidence") if isinstance(source_capability.get("provenance"), dict) else None,
                },
                "provenance": _compact_provenance(item.get("provenance")),
            }
        )
    return compact


def _is_trusted_kb_confidence(value: Any) -> bool:
    return str(value) in TRUSTED_KB_CONFIDENCE


def _kb_public_metadata(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "kb_id": payload.get("kb_id"),
        "version": payload.get("kb_version"),
        "source_revision": payload.get("source_revision"),
        "content_sha256": payload.get("content_sha256"),
        "summary": payload.get("summary", {}),
    }


def _strict_case_cpu_source(case: dict[str, Any]) -> dict[str, Any]:
    """Return CPU source only when it still matches the case's AST evidence."""

    fallback = case.get("cpu_software_fallback") if isinstance(case.get("cpu_software_fallback"), dict) else {}
    profile = case.get("cpu_fallback_ast_profile") if isinstance(case.get("cpu_fallback_ast_profile"), dict) else {}
    function = profile.get("function") if isinstance(profile.get("function"), dict) else {}
    source = function.get("source") if isinstance(function.get("source"), dict) else {}
    implementation = fallback.get("implementation") or "unknown"
    binding_status = profile.get("binding_status") or "missing"
    detail: dict[str, Any] = {
        "implementation": implementation,
        "execution_domain": fallback.get("execution_domain"),
        "reference": fallback.get("reference"),
        "binding_status": binding_status,
        "function": {
            "qualified_name": function.get("qualified_name"),
            "signature": function.get("signature"),
        },
    }
    relative_path = source.get("file")
    # The profile's function span is authoritative; do not use a generic
    # fallback reference to guess a function range.
    span = function.get("span") if isinstance(function.get("span"), dict) else {}
    start_line = span.get("start_line")
    end_line = span.get("end_line")
    if binding_status != "exact_definition" or not isinstance(relative_path, str) or not relative_path:
        return {
            **detail,
            "state": "not_available",
            "reason": "该 fallback 没有唯一、可验证的 CPU 函数定义；不能展示或猜测 CPU 源码。",
        }
    if not isinstance(start_line, int) or not isinstance(end_line, int) or start_line < 1 or end_line < start_line:
        return {
            **detail,
            "state": "not_available",
            "reason": "CPU AST profile 没有可验证的函数范围。",
        }
    source_root = default_source_root().resolve()
    relative_candidates = [Path(relative_path)]
    if source_root.name == "openharmony" and relative_candidates[0].parts[:1] == ("openharmony",):
        relative_candidates.append(Path(*relative_candidates[0].parts[1:]))
    source_file: Path | None = None
    for relative_candidate in relative_candidates:
        candidate = (source_root / relative_candidate).resolve()
        try:
            candidate.relative_to(source_root)
        except ValueError:
            continue
        if candidate.is_file():
            source_file = candidate
            break
    if source_file is None:
        return {
            **detail,
            "state": "not_available",
            "reason": "默认源码连接器中不存在该 CPU AST profile 绑定的源码文件。",
        }
    expected_sha = source.get("sha256")
    current_sha = sha256_file(source_file)
    if isinstance(expected_sha, str) and expected_sha and expected_sha != current_sha:
        return {
            **detail,
            "state": "source_changed",
            "reason": "当前源码与知识库 AST profile 的文件哈希不一致；为避免展示错误实现，不显示已变化的代码。",
            "source": {"path": relative_path, "expected_sha256": expected_sha, "current_sha256": current_sha},
        }
    lines = source_file.read_text(encoding="utf-8", errors="replace").splitlines()
    if end_line > len(lines):
        return {
            **detail,
            "state": "not_available",
            "reason": "CPU AST profile 的函数范围超出当前源码文件。",
        }
    if end_line - start_line + 1 > MAX_KB_CPU_SOURCE_LINES:
        return {
            **detail,
            "state": "too_large",
            "reason": f"CPU 函数范围超过 {MAX_KB_CPU_SOURCE_LINES} 行，保留 AST 和源码引用，不在页面内截断展示。",
            "source": {"path": relative_path, "line_start": start_line, "line_end": end_line, "sha256": current_sha},
        }
    excerpt = [
        {"line": index + 1, "text": lines[index]}
        for index in range(start_line - 1, end_line)
    ]
    if len("\n".join(item["text"] for item in excerpt).encode("utf-8")) > MAX_KB_CPU_SOURCE_BYTES:
        return {
            **detail,
            "state": "too_large",
            "reason": "CPU 函数源码超过页面安全展示上限，保留 AST 和源码引用。",
            "source": {"path": relative_path, "line_start": start_line, "line_end": end_line, "sha256": current_sha},
        }
    return {
        **detail,
        "state": "available",
        "source": {"path": relative_path, "line_start": start_line, "line_end": end_line, "sha256": current_sha},
        "excerpt": excerpt,
    }


def _search_kb(
    query: str,
    *,
    include_unverified: bool,
    include_cases: bool,
    knowledge_base: str = "optimization",
) -> dict[str, Any]:
    if knowledge_base not in {"optimization", "hardware"}:
        raise PlatformError("KB_QUERY_INVALID", "knowledge_base must be optimization or hardware.", status_code=422)
    normalized_query = query.casefold().strip()
    payload = _optimization_kb() if knowledge_base == "optimization" else _hardware_kb()
    records = _optimization_kb_records() if knowledge_base == "optimization" else _hardware_kb_records()
    results: list[dict[str, Any]] = []
    for item in records:
        if not include_unverified and not _is_trusted_kb_confidence(item["confidence"]):
            continue
        haystack = json.dumps(item, ensure_ascii=True, sort_keys=True).casefold()
        if not normalized_query or normalized_query in haystack:
            score = 1.0 if not normalized_query else min(0.98, 0.45 + 0.05 * haystack.count(normalized_query))
            results.append({**item, "score": round(score, 3), "matched_fields": ["structured_record"] if normalized_query else []})
    case_results: list[dict[str, Any]] = []
    if knowledge_base == "optimization" and include_cases:
        catalog = ensure_catalog(CASE_CATALOG_PATH, WORKSPACE_ROOT)
        for case in catalog["cases"]:
            haystack = json.dumps(case, ensure_ascii=True, sort_keys=True).casefold()
            if not normalized_query or normalized_query in haystack:
                case_results.append(
                    {
                        "record_type": "knowledge_case",
                        "record_id": case["case_id"],
                        "name": case["title"],
                        "algorithm": case["algorithm_hypothesis"],
                        "target_unit": case["target_hypothesis"],
                        "confidence": "candidate",
                        "score": 0.35,
                        "matched_fields": ["candidate_catalog"],
                        "provenance": case["provenance"],
                        "record": case,
                    }
                )
    results.sort(key=lambda item: (_is_trusted_kb_confidence(item["confidence"]), item["score"]), reverse=True)
    return {
        "knowledge_base": knowledge_base,
        "kb_id": payload.get("kb_id"),
        "kb_version": payload.get("kb_version"),
        "kb_content_sha256": payload.get("content_sha256"),
        # Compatibility alias for clients that predate the split KB contract.
        "kb_manifest_sha256": payload.get("content_sha256"),
        "summary": payload.get("summary", {}),
        "records": results[:100],
        "case_records": case_results[:100],
    }


def _safe_file_name(name: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(name).name).strip(".-")
    return safe[:120] or "trace-input"


async def _persist_upload(file: UploadFile) -> tuple[Path, str, int, str]:
    RAW_INPUT_ROOT.mkdir(parents=True, exist_ok=True)
    name = _safe_file_name(file.filename or "trace-input")
    descriptor, temp_name = tempfile.mkstemp(prefix=".upload-", dir=RAW_INPUT_ROOT)
    temporary = Path(temp_name)
    import hashlib

    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(descriptor, "wb") as handle:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise PlatformError(
                        "TRACE_UPLOAD_TOO_LARGE",
                        f"Trace upload exceeds the configured {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB Phase-A limit.",
                        status_code=413,
                        details={"max_bytes": MAX_UPLOAD_BYTES},
                    )
                digest.update(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        sha256 = digest.hexdigest()
        final_root = RAW_INPUT_ROOT / sha256
        final_root.mkdir(parents=True, exist_ok=True)
        final_path = final_root / name
        if final_path.exists():
            temporary.unlink(missing_ok=True)
        else:
            os.replace(temporary, final_path)
        return final_path, sha256, size, name
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)
        await file.close()


def _structural_precheck(
    text: str,
    line: int,
    *,
    target_function: dict[str, Any] | None = None,
) -> dict[str, Any]:
    lines = text.splitlines()
    start = max(0, line - 31)
    end = min(len(lines), line + 30)
    window = "\n".join(lines[start:end])
    payload = {
        "kind": "non_semantic_precheck",
        "line_window": {"start": start + 1, "end": end},
        "loop_tokens": len(re.findall(r"\b(for|while|do)\b", window)),
        "branch_tokens": len(re.findall(r"\b(if|switch|case)\b", window)),
        "array_subscript_tokens": window.count("["),
        "source_excerpt": [
            {"line": index + 1, "text": lines[index]}
            for index in range(start, end)
        ],
    }
    if target_function is not None:
        payload["target_function"] = target_function
    return payload


def _ast_include_dirs(source: Path, source_root: Path) -> list[Path]:
    """Build a bounded include search path from the selected source root.

    OpenHarmony frequently uses project-root-relative includes such as
    ``ecmascript/js_thread.h``.  Supplying only ``*/include`` directories
    leaves a source file's own project root invisible to libclang, even though
    the file and the requested instrumentation point are available locally.
    """

    directories: list[Path] = []
    root = source_root.resolve()
    current = source.parent.resolve()
    while True:
        for candidate in (current, current / "include"):
            if candidate.is_dir() and candidate not in directories:
                directories.append(candidate)
        if current == root or current.parent == current:
            break
        current = current.parent
    return directories


def _function_names_match(candidate: str, target: str) -> bool:
    """Compare extractor-qualified and source-text-qualified C++ names."""

    candidate = candidate.strip()
    target = target.strip()
    if not candidate or not target:
        return False
    return candidate == target or candidate.endswith(f"::{target}") or target.endswith(f"::{candidate}")


def _feature_contains_line(item: dict[str, Any], line: int) -> bool:
    extent = item.get("extent")
    if isinstance(extent, dict):
        try:
            return int(extent["start_line"]) <= line <= int(extent["end_line"])
        except (KeyError, TypeError, ValueError):
            pass
    location = item.get("location")
    try:
        return int(location.get("line")) == line if isinstance(location, dict) else int(item.get("line")) == line
    except (TypeError, ValueError):
        return False


def _precise_ast_features(
    source: Path,
    line: int,
    *,
    source_root: Path,
    target_function: dict[str, Any] | None = None,
) -> tuple[str, list[dict[str, Any]], list[str]]:
    """Use the existing libclang adapter when installed, otherwise report a hard diagnostic."""

    if importlib.util.find_spec("clang") is None:
        venv_python = APP_ROOT / ".venv" / "bin" / "python"
        restart_hint = f" Restart the backend with {venv_python}." if venv_python.is_file() else ""
        return "unavailable", [], [f"libclang Python bindings are not installed in the active backend interpreter.{restart_hint}"]
    descriptor, output_name = tempfile.mkstemp(prefix=".features-", suffix=".jsonl")
    os.close(descriptor)
    output = Path(output_name)
    try:
        include_dirs = _ast_include_dirs(source, source_root)
        command = [sys.executable, str(TOOLS_ROOT / "precise_feature_extractor.py"), "--src", str(source), "--out", str(output)]
        for include_dir in include_dirs[:16]:
            command.extend(["--include", str(include_dir)])
        completed = subprocess.run(command, text=True, capture_output=True, timeout=60, check=False)
        diagnostics = [line for line in (completed.stderr + "\n" + completed.stdout).splitlines() if line][:30]
        rows: list[dict[str, Any]] = []
        if output.is_file():
            for raw in output.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    row = json.loads(raw)
                    if isinstance(row, dict):
                        rows.append(row)
                except json.JSONDecodeError:
                    continue
        if not rows:
            return "failed", [], diagnostics or ["Precise extractor produced no functions for this compile context."]

        target_name = str(target_function.get("name") or "").strip() if isinstance(target_function, dict) else ""
        if target_name:
            target_rows = [
                item
                for item in rows
                if _function_names_match(str(item.get("function") or ""), target_name)
            ]
            if not target_rows:
                return (
                    "failed",
                    [],
                    [
                        f"Precise extractor did not emit the requested enclosing function {target_name}.",
                        *diagnostics,
                    ],
                )
            rows = target_rows

        def distance_to_requested_line(item: dict[str, Any]) -> int:
            location = item.get("location")
            candidate = location.get("line") if isinstance(location, dict) else item.get("line")
            try:
                return abs(int(candidate) - line)
            except (TypeError, ValueError):
                return 1_000_000

        rows.sort(
            key=lambda item: (
                0 if _feature_contains_line(item, line) else 1,
                distance_to_requested_line(item),
            )
        )
        return "available", rows[:3], diagnostics
    except subprocess.TimeoutExpired:
        return "failed", [], ["Precise AST extraction exceeded its 60 second bound."]
    except Exception as exc:
        return "failed", [], [str(exc)]
    finally:
        output.unlink(missing_ok=True)


def _match_features(features: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not features:
        return []
    if str(TOOLS_ROOT) not in sys.path:
        sys.path.insert(0, str(TOOLS_ROOT))
    try:
        from matching_engine import _load_kb, match_query

        # The deterministic matcher consumes the versioned source-pattern
        # layers.  They are a build input of the generated optimization KB;
        # the separate hardware KB is applied as a later capability gate.
        patterns = _load_kb(LEGACY_PATTERN_KB_ROOT)
        return [match_query(feature, patterns, top_k=3) for feature in features]
    except Exception as exc:
        return [{"status": "unavailable", "reason": str(exc)}]


def _build_source_inspection_payload(
    source_connector_id: str,
    path: str,
    line: int,
    *,
    target_function: dict[str, Any] | None = None,
    source_match: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    source = connectors.resolve_source_path(source_connector_id, path)
    if source.stat().st_size > 8 * 1024 * 1024:
        raise PlatformError("SOURCE_FILE_TOO_LARGE", "Source inspection has an 8 MiB Phase-A limit.", status_code=413)
    connector = connectors.get(source_connector_id)
    text = source.read_text(encoding="utf-8", errors="replace")
    normalized_target = target_function if isinstance(target_function, dict) and isinstance(target_function.get("name"), str) else None
    precheck = _structural_precheck(text, line, target_function=normalized_target)
    feature_status, features, diagnostics = _precise_ast_features(
        source,
        line,
        source_root=Path(str(connector["source_root"])),
        target_function=normalized_target,
    )
    binding = _source_binding(source_connector_id, connector["snapshot"]["snapshot_id"])
    ast_target = {
        "event_source_line": line,
        "selection": "enclosing_function" if normalized_target is not None else "nearest_function",
        "function": normalized_target,
    }
    return (
        {
            "source": {"path": path, "line": line, "sha256": sha256_file(source)},
            "feature_status": feature_status,
            "features": features,
            "ast_diagnostics": diagnostics,
            "ast_target": ast_target,
            "structural_precheck": precheck,
            **({"source_match": source_match} if source_match is not None else {}),
            "kb_matches": _match_features(features) if feature_status == "available" else [],
        },
        binding,
    )


def _hotspot_source_location(hotspot: dict[str, Any]) -> tuple[str, int] | None:
    function_ref = hotspot.get("function_ref")
    if not isinstance(function_ref, dict):
        return None
    path = function_ref.get("file")
    try:
        line = int(function_ref.get("line"))
    except (TypeError, ValueError):
        return None
    if not isinstance(path, str) or not path or line < 1:
        return None
    return path, line


def _cluster_from_analysis(analysis: dict[str, Any], cluster_id: str) -> dict[str, Any]:
    cluster = next(
        (
            item
            for item in analysis.get("clusters", [])
            if isinstance(item, dict) and item.get("cluster_id") == cluster_id
        ),
        None,
    )
    if cluster is None:
        raise PlatformError(
            "TRACE_CLUSTER_NOT_FOUND",
            "Requested trace cluster is not part of the analysis artifact.",
            status_code=422,
            details={"analysis_artifact_id": analysis.get("artifact_id"), "cluster_id": cluster_id},
        )
    return cluster


def _declared_event_source(function_ref: dict[str, Any]) -> dict[str, Any] | None:
    path = function_ref.get("file")
    try:
        line = int(function_ref.get("line"))
    except (TypeError, ValueError):
        return None
    if not isinstance(path, str) or not path or line < 1:
        return None
    return {
        "path": path,
        "line": line,
        "function": function_ref.get("symbol"),
        "resolution": "trace_declared_source_location",
    }


def _external_cluster_events(analysis: dict[str, Any], cluster_id: str, *, limit: int) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    for hotspot in analysis.get("hotspot_candidates", []):
        if not isinstance(hotspot, dict) or hotspot.get("cluster_id") != cluster_id:
            continue
        function_ref = hotspot.get("function_ref") if isinstance(hotspot.get("function_ref"), dict) else {}
        event_name = str(hotspot.get("trace_event_name") or function_ref.get("symbol") or hotspot.get("unresolved_reason") or hotspot.get("hotspot_id") or "unknown")
        events.append(
            {
                "event_id": str(hotspot.get("hotspot_id") or f"event_external_{len(events) + 1}"),
                "rank": len(events) + 1,
                "event_name": event_name,
                "event_kind": "callstack_span",
                "metrics": hotspot.get("active_time_metrics") if isinstance(hotspot.get("active_time_metrics"), dict) else {},
                "stage_context": hotspot.get("stage_context") if isinstance(hotspot.get("stage_context"), dict) else None,
                "measurement_semantics": hotspot.get("measurement_semantics") if isinstance(hotspot.get("measurement_semantics"), dict) else None,
                "trace_event_refs": [str(ref) for ref in hotspot.get("evidence_refs", []) if isinstance(ref, str)],
                "source_location": _declared_event_source(function_ref),
                "source_resolution": str(function_ref.get("resolution") or "external_symbol_only"),
            }
        )
        if len(events) >= limit:
            break
    return {
        "events": events,
        "quality": {
            "status": "available" if events else "unavailable",
            "profile_id": "external-cluster-hotspots-v1",
            "source": "normalized_external_report",
            "returned_events": len(events),
            "limit": limit,
            "source_location_available": any(event.get("source_location") for event in events),
        },
    }


def _scheduler_label_events(cluster: dict[str, Any], *, limit: int) -> dict[str, Any]:
    edge_summary = cluster.get("edge_summary") if isinstance(cluster.get("edge_summary"), dict) else {}
    top_edges = edge_summary.get("top_edges") if isinstance(edge_summary.get("top_edges"), list) else []
    events: list[dict[str, Any]] = []
    for item in top_edges:
        if not isinstance(item, dict) or not isinstance(item.get("edge"), str):
            continue
        name = item["edge"]
        events.append(
            {
                "event_id": f"event_scheduler_{sha256_json({'cluster': cluster.get('cluster_id'), 'edge': name})[:16]}",
                "rank": len(events) + 1,
                "event_name": name,
                "event_kind": "scheduler_edge_label",
                "metrics": {"wakeup_count": item.get("count", 0)},
                "trace_event_refs": [],
                "source_resolution": "scheduler_task_labels_are_not_source_symbols",
            }
        )
        if len(events) >= limit:
            break
    return {
        "events": events,
        "quality": {
            "status": "partial" if events else "unavailable",
            "profile_id": "scheduler-label-events-v1",
            "source": "scheduler_edge_labels",
            "returned_events": len(events),
            "limit": limit,
            "source_location_available": False,
        },
    }


def _cluster_event_catalog(analysis: dict[str, Any], cluster: dict[str, Any], *, limit: int) -> dict[str, Any]:
    input_kind = str(analysis.get("input_kind") or "")
    if input_kind == "raw_htrace":
        raw_htrace = analysis.get("raw_htrace") if isinstance(analysis.get("raw_htrace"), dict) else {}
        return list_htrace_cluster_events(raw_htrace, cluster, state_root=STATE_ROOT, limit=limit)
    if input_kind == "external_trace_analysis_report":
        return _external_cluster_events(analysis, str(cluster.get("cluster_id") or ""), limit=limit)
    return _scheduler_label_events(cluster, limit=limit)


def _hotspot_discovery_scope(discovery_id: str, *, module_key: str | None = None) -> dict[str, Any]:
    profile = HOTSPOT_DISCOVERY_PROFILES.get(discovery_id)
    if profile is None:
        raise PlatformError(
            "TRACE_HOTSPOT_DISCOVERY_UNKNOWN",
            "Requested hotspot discovery method is not registered.",
            status_code=422,
            details={"discovery_id": discovery_id, "available_discovery_ids": sorted(HOTSPOT_DISCOVERY_PROFILES)},
        )
    scope = {
        "kind": "trace_hotspot_discovery",
        "scope": profile["scope"],
        "discovery_id": discovery_id,
        "profile_id": profile["profile_id"],
    }
    if module_key:
        scope["module_key"] = module_key
    return scope


def _hitrace_tag_catalog_for_analysis(analysis: dict[str, Any]) -> dict[str, Any]:
    """Load versioned tag semantics when the selected source connector exists.

    Self-time discovery remains useful without a source tree, so an unavailable
    connector degrades tag classification to trace identity heuristics instead
    of blocking the raw trace catalog.
    """

    try:
        connector = connectors.get(str(analysis["source_connector_id"]))
        source_root = Path(str(connector["source_root"])).resolve()
    except (KeyError, PlatformError):
        return {
            "status": "unavailable",
            "tag_by_bit": {},
            "provenance": {"status": "unavailable", "reason": "Trace source connector is unavailable for HiTrace tag decoding."},
        }
    return load_hitrace_tag_catalog(source_root, WORKSPACE_ROOT)


def _hotspot_discovery_catalog(
    analysis: dict[str, Any],
    discovery_id: str,
    *,
    limit: int,
    module_key: str | None = None,
) -> dict[str, Any]:
    """Dispatch one registered trace-wide hotspot discovery method.

    A discovery result is intentionally an event catalog, not a cluster.  This
    keeps future CPU-sample, critical-path, or accelerator-timeline methods from
    inheriting wakeup-window semantics merely to reuse the code drill-down path.
    """

    profile = HOTSPOT_DISCOVERY_PROFILES.get(discovery_id)
    if profile is None:
        _hotspot_discovery_scope(discovery_id)
        raise AssertionError("Unknown discovery should have raised above.")
    input_kind = str(analysis.get("input_kind") or "")
    if input_kind != profile["input_kind"]:
        return {
            "events": [],
            "module_catalog": {"classification": "unavailable", "groups": []},
            "quality": {
                "status": "unavailable",
                "reason": f"Discovery {discovery_id!r} requires {profile['input_kind']!r}, not {input_kind or 'unknown'!r}.",
                "profile_id": profile["profile_id"],
                "discovery_id": discovery_id,
                "source_location_available": False,
            },
        }
    raw_htrace = analysis.get("raw_htrace") if isinstance(analysis.get("raw_htrace"), dict) else {}
    if discovery_id == "event_self_time":
        return list_htrace_self_time_events(
            raw_htrace,
            state_root=STATE_ROOT,
            limit=limit,
            module_key=module_key,
            tag_catalog=_hitrace_tag_catalog_for_analysis(analysis),
        )
    raise PlatformError(
        "TRACE_HOTSPOT_DISCOVERY_UNKNOWN",
        "Requested hotspot discovery method has no extractor implementation.",
        status_code=422,
        details={"discovery_id": discovery_id},
    )


def _external_reasoning_hotspot_catalog(
    analysis: dict[str, Any],
    *,
    limit: int,
    module_key: str | None = None,
    group_by_module: bool = False,
    per_module_limit: int = 6,
) -> dict[str, Any]:
    """Normalize trace-team hotspot records into the same event contract as .htrace.

    This does not manufacture source locations.  An upstream report can enter
    the reasoning workflow, but source resolution stays unavailable unless its
    typed function reference contains a real path and line.
    """

    def metric_number(metrics: dict[str, Any], *keys: str) -> float:
        for key in keys:
            try:
                value = float(metrics.get(key))
            except (TypeError, ValueError):
                continue
            if value >= 0:
                return value
        return 0.0

    candidates: list[dict[str, Any]] = []
    for index, hotspot in enumerate(analysis.get("hotspot_candidates", []), start=1):
        if not isinstance(hotspot, dict):
            continue
        function_ref = hotspot.get("function_ref") if isinstance(hotspot.get("function_ref"), dict) else {}
        event_module = str(function_ref.get("module") or "unknown").strip() or "unknown"
        if module_key and event_module.casefold() != module_key.casefold():
            continue
        metrics = hotspot.get("active_time_metrics") if isinstance(hotspot.get("active_time_metrics"), dict) else {}
        event_id = hotspot.get("hotspot_id")
        if not isinstance(event_id, str) or not event_id:
            event_id = f"external_hotspot_{index:04d}"
        event_name = hotspot.get("trace_event_name") or function_ref.get("symbol") or hotspot.get("event_name") or event_id
        candidate_role = str(hotspot.get("candidate_role") or "reported_hot_event")
        source_location: dict[str, Any] | None = None
        try:
            line = int(function_ref.get("line"))
            path = function_ref.get("file")
            if isinstance(path, str) and path and line > 0:
                source_location = {"path": path, "line": line}
        except (TypeError, ValueError):
            pass
        candidates.append(
            {
                "event_id": event_id,
                "event_name": str(event_name),
                "event_kind": "callstack_span",
                "rank": int(hotspot.get("external_rank") or index),
                "module": event_module,
                "metrics": metrics,
                "trace_symbol_hint": str(function_ref.get("symbol") or ""),
                "stage_context": hotspot.get("stage_context") if isinstance(hotspot.get("stage_context"), dict) else None,
                "measurement_semantics": hotspot.get("measurement_semantics") if isinstance(hotspot.get("measurement_semantics"), dict) else None,
                "observed_occurrences": hotspot.get("observed_occurrences") if isinstance(hotspot.get("observed_occurrences"), list) else [],
                "candidate_role": candidate_role,
                "source_location": source_location,
                "source_resolution": "trace_declared_source" if source_location else "external_symbol_only",
                "trace_event_refs": hotspot.get("evidence_refs") if isinstance(hotspot.get("evidence_refs"), list) else [],
                "summary": (
                    "该阶段没有提供代表热点；这里只展示 start/end Trace 锚点，供 Agent 继续检查源码和调用路径，不携带热点时长。"
                    if candidate_role == "trace_anchor_only"
                    else "阶段报告中的代表性 Trace span；桩点函数不是已证明的 CPU 热点，需继续检查局部代码、callee 或外部运行时边界。"
                    if hotspot.get("measurement_semantics")
                    else hotspot.get("unresolved_reason") or hotspot.get("temporal_pressure") or "External trace-team hotspot."
                ),
            }
        )
    if not group_by_module:
        events = candidates[:limit]
        for rank, event in enumerate(events, start=1):
            event["rank"] = int(event.get("rank") or rank)
        module_event_groups: list[dict[str, Any]] = []
    else:
        groups_by_module: dict[str, list[dict[str, Any]]] = {}
        for event in candidates:
            groups_by_module.setdefault(str(event["module"]), []).append(event)

        def event_self_time(event: dict[str, Any]) -> float:
            return metric_number(
                event.get("metrics") if isinstance(event.get("metrics"), dict) else {},
                "self_time_estimate_total_duration_ms",
                "exclusive_estimate_total_duration_ms",
                "total_duration_ms",
                "active_duration_ms",
            )

        def event_max_time(event: dict[str, Any]) -> float:
            return metric_number(
                event.get("metrics") if isinstance(event.get("metrics"), dict) else {},
                "max_self_time_estimate_duration_ms",
                "max_span_duration_ms",
                "max_duration_ms",
            )

        def event_calls(event: dict[str, Any]) -> int:
            return int(metric_number(event.get("metrics") if isinstance(event.get("metrics"), dict) else {}, "occurrence_count", "call_count"))

        ordered_modules = sorted(
            groups_by_module,
            key=lambda key: (-sum(event_self_time(event) for event in groups_by_module[key]), key.casefold()),
        )
        events = []
        module_event_groups = []
        global_rank = 1
        for key in ordered_modules:
            ordered_events = sorted(
                groups_by_module[key],
                key=lambda event: (-event_self_time(event), -event_max_time(event), -event_calls(event), str(event["event_name"])),
            )
            displayed = []
            for module_rank, event in enumerate(ordered_events[:per_module_limit], start=1):
                event = {**event, "rank": global_rank, "module_rank": module_rank}
                displayed.append(event)
                events.append(event)
                global_rank += 1
            module_event_groups.append(
                {
                    "module_key": key,
                    "module_name": key,
                    "system_collapsed": False,
                    "event_group_count": len(ordered_events),
                    "occurrence_count": sum(event_calls(event) for event in ordered_events),
                    "self_time_estimate_total_duration_ms": round(sum(event_self_time(event) for event in ordered_events), 6),
                    "max_self_time_estimate_duration_ms": round(max((event_max_time(event) for event in ordered_events), default=0.0), 6),
                    "returned_event_count": len(displayed),
                    "per_module_limit": per_module_limit,
                    "metric_semantics": "inclusive_trace_span" if any(event.get("measurement_semantics") for event in ordered_events) else "reported_active_time",
                    "events": displayed,
                }
            )

    catalog_groups: list[dict[str, Any]] = []
    grouped_candidates: dict[str, list[dict[str, Any]]] = {}
    for event in candidates:
        grouped_candidates.setdefault(str(event["module"]), []).append(event)
    for key, module_events in grouped_candidates.items():
        catalog_groups.append(
            {
                "module_key": key,
                "module_name": key,
                "system_collapsed": False,
                "event_group_count": len(module_events),
                "occurrence_count": sum(int(metric_number(record.get("metrics") if isinstance(record.get("metrics"), dict) else {}, "occurrence_count", "call_count")) for record in module_events),
                "self_time_estimate_total_duration_ms": round(sum(metric_number(record.get("metrics") if isinstance(record.get("metrics"), dict) else {}, "self_time_estimate_total_duration_ms", "exclusive_estimate_total_duration_ms", "total_duration_ms", "active_duration_ms") for record in module_events), 6),
                "metric_semantics": "inclusive_trace_span" if any(record.get("measurement_semantics") for record in module_events) else "reported_active_time",
            }
        )
    catalog_groups.sort(key=lambda group: (-float(group["self_time_estimate_total_duration_ms"]), str(group["module_name"]).casefold()))
    stage_events: dict[str, list[dict[str, Any]]] = {}
    for event in candidates:
        stage_context = event.get("stage_context") if isinstance(event.get("stage_context"), dict) else {}
        stage_id = str(stage_context.get("stage_id") or "").strip()
        if stage_id:
            stage_events.setdefault(stage_id, []).append(event)

    def stage_event_sort_key(event: dict[str, Any]) -> tuple[float, int, str]:
        occurrences = event.get("observed_occurrences") if isinstance(event.get("observed_occurrences"), list) else []
        first = occurrences[0] if occurrences and isinstance(occurrences[0], dict) else {}
        try:
            timestamp = float(first["ts_ms"])
        except (KeyError, TypeError, ValueError):
            timestamp = float("inf")
        return timestamp, int(event.get("rank") or 0), str(event.get("event_name") or "")

    stage_event_groups: list[dict[str, Any]] = []
    for stage_index, stage in enumerate(analysis.get("stage_catalog", []), start=1):
        if not isinstance(stage, dict):
            continue
        stage_id = str(stage.get("stage_id") or f"stage_{stage_index:04d}")
        grouped = sorted(stage_events.get(stage_id, []), key=stage_event_sort_key)
        stage_event_groups.append(
            {
                "stage_id": stage_id,
                "stage_name": str(stage.get("stage_name") or stage_id),
                "stage_order": int(stage.get("stage_order") or stage_index),
                "time_range_ns": stage.get("time_range_ns") if isinstance(stage.get("time_range_ns"), dict) else {},
                "duration_ms": stage.get("duration_ms"),
                "reported_event_count": int(stage.get("reported_event_count") or 0),
                "reported_total_event_duration_ms": stage.get("reported_total_event_duration_ms"),
                "listed_top_event_count": int(stage.get("listed_top_event_count") or 0),
                "coverage": str(stage.get("coverage") or "no_event_anchor"),
                "start_event": stage.get("start_event") if isinstance(stage.get("start_event"), dict) else None,
                "end_event": stage.get("end_event") if isinstance(stage.get("end_event"), dict) else None,
                "event_group_count": len(grouped),
                "returned_event_count": len(grouped),
                "events": grouped,
            }
        )
    stage_event_groups.sort(key=lambda group: (int(group["stage_order"]), str(group["stage_id"])))
    analysis_quality = analysis.get("quality") if isinstance(analysis.get("quality"), dict) else {}
    normalization = analysis_quality.get("normalization") if isinstance(analysis_quality.get("normalization"), dict) else {}
    all_analysis_candidates = [item for item in analysis.get("hotspot_candidates", []) if isinstance(item, dict)]
    has_reported_event = any(event.get("candidate_role") != "trace_anchor_only" for event in candidates)
    has_anchor_only_event = any(event.get("candidate_role") == "trace_anchor_only" for event in candidates)
    if events:
        reason = ""
    elif module_key and all_analysis_candidates:
        reason = f"当前报告已有可调查事件，但模块筛选“{module_key}”没有命中；请切换为全部模块。"
    elif int(normalization.get("input_stage_count") or 0) > 0:
        reason = (
            f"已识别 {int(normalization.get('input_stage_count') or 0)} 个阶段，但没有解析到代表热点事件或 start/end Trace 锚点。"
            "原始 JSON 已保留；请检查阶段中的 top_events/topEvents/hot_events 或 start_event/startEvent 字段。"
        )
    else:
        reason = (
            "JSON 已导入，但没有识别到热点或阶段事件容器。支持根节点或 result/data/analysis/report 包装下的 "
            "stages/stageList、hotspots/hotspotCandidates 和 modules/moduleList。"
        )
    if has_reported_event and has_anchor_only_event:
        duration_kind = "reported_trace_span_and_unmeasured_anchor"
    elif has_reported_event:
        duration_kind = "inclusive_trace_span" if any(item.get("measurement_semantics") for item in candidates) else "reported_active_time"
    elif has_anchor_only_event:
        duration_kind = "unmeasured_trace_anchor"
    else:
        duration_kind = "unavailable"
    return {
        "events": events,
        "module_event_groups": module_event_groups,
        "stage_event_groups": stage_event_groups,
        "module_catalog": {"classification": "trace_team_report", "groups": catalog_groups},
        "quality": {
            "status": "degraded" if events and not has_reported_event else "available" if events else "unavailable",
            "reason": reason,
            "note": "Trace 团队记录按原值保留；阶段跨度不是 CPU self-time。源码解析先定位 Trace 锚点，再由 Agent 深挖局部代码、callee 或外部运行时。",
            "profile_id": "external-report-hotspot-catalog/v2",
            "duration_kind": duration_kind,
            "cpu_self_time_available": False if any(item.get("measurement_semantics") for item in candidates) else None,
            "callee_attribution_available": False if any(item.get("measurement_semantics") for item in candidates) else None,
            "source_location_available": any(item["source_location"] is not None for item in candidates),
            "normalization": normalization,
        },
    }


def _reasoning_hotspot_catalog(
    analysis: dict[str, Any],
    *,
    limit: int,
    module_key: str | None = None,
    group_by_module: bool = False,
    per_module_limit: int = 6,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Return one real trace's candidates for the multi-strategy workflow."""

    input_kind = str(analysis.get("input_kind") or "")
    if input_kind == "raw_htrace":
        scope = _hotspot_discovery_scope("event_self_time", module_key=module_key)
        raw_htrace = analysis.get("raw_htrace") if isinstance(analysis.get("raw_htrace"), dict) else {}
        catalog = list_htrace_self_time_events(
            raw_htrace,
            state_root=STATE_ROOT,
            limit=limit,
            module_key=module_key,
            tag_catalog=_hitrace_tag_catalog_for_analysis(analysis),
            group_by_module=group_by_module,
            per_module_limit=per_module_limit,
        )
        return _enrich_hitrace_event_catalog(analysis, catalog), scope
    if input_kind == "external_trace_analysis_report":
        return _external_reasoning_hotspot_catalog(
            analysis,
            limit=limit,
            module_key=module_key,
            group_by_module=group_by_module,
            per_module_limit=per_module_limit,
        ), {
            "kind": "trace_team_hotspot_catalog",
            "scope": "trace_team_report",
            "profile_id": "external-report-hotspot-catalog/v1",
            **({"module_key": module_key} if module_key else {}),
        }
    return {
        "events": [],
        "module_event_groups": [],
        "stage_event_groups": [],
        "module_catalog": {"classification": "unavailable", "groups": []},
        "quality": {
            "status": "unavailable",
            "reason": "This trace input does not provide a supported hotspot catalog. Import a raw .htrace or a trace-team JSON report.",
            "profile_id": "reasoning-hotspot-catalog/v1",
            "source_location_available": False,
        },
    }, {"kind": "unsupported_trace_input", "scope": "none", "profile_id": "reasoning-hotspot-catalog/v1"}


def _catalog_event_by_id(catalog: dict[str, Any], event_id: str) -> dict[str, Any] | None:
    """Find an event in either the bounded flat list or complete stage groups."""

    for event in catalog.get("events", []):
        if isinstance(event, dict) and event.get("event_id") == event_id:
            return event
    for group in catalog.get("stage_event_groups", []):
        if not isinstance(group, dict):
            continue
        for event in group.get("events", []):
            if isinstance(event, dict) and event.get("event_id") == event_id:
                return event
    return None


def _enrich_hitrace_event_catalog(analysis: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    """Decode HiTrace level/tag fields before the user or Agent drills into source."""

    connector = connectors.get(str(analysis["source_connector_id"]))
    source_root = Path(str(connector["source_root"])).resolve()
    tag_catalog = load_hitrace_tag_catalog(source_root, WORKSPACE_ROOT)
    decoded_count = 0
    for event in catalog.get("events", []):
        if not isinstance(event, dict):
            continue
        marker = describe_hitrace_event(str(event.get("event_name") or ""), tag_catalog)
        if marker is None:
            continue
        event["hitrace_marker"] = marker
        decoded_count += 1
    quality = catalog.get("quality") if isinstance(catalog.get("quality"), dict) else {}
    catalog["quality"] = {
        **quality,
        "hitrace_marker_events": decoded_count,
        "hitrace_tag_catalog": dict(tag_catalog.get("provenance") or {}),
    }
    return catalog


def _hitrace_provenance_refs(payload: dict[str, Any]) -> list[str]:
    """Carry decoder and search-scope versions in the immutable evidence graph."""

    refs: list[str] = []
    marker = payload.get("marker") if isinstance(payload.get("marker"), dict) else {}
    catalog = marker.get("catalog") if isinstance(marker.get("catalog"), dict) else {}
    source_scope = payload.get("source_scope") if isinstance(payload.get("source_scope"), dict) else {}
    knowledge = source_scope.get("knowledge") if isinstance(source_scope.get("knowledge"), dict) else {}
    for provenance in (catalog, knowledge):
        for key in ("header_sha256", "mapping_sha256", "sha256"):
            value = provenance.get(key)
            if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
                refs.append(f"sha256:{value}")
    return list(dict.fromkeys(refs))


def _write_trace_event_catalog(
    analysis: dict[str, Any],
    cluster: dict[str, Any] | None,
    catalog: dict[str, Any],
    *,
    catalog_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist the deterministic event list so later steps share one evidence record."""

    quality = catalog.get("quality") if isinstance(catalog.get("quality"), dict) else {}
    tag_catalog = quality.get("hitrace_tag_catalog") if isinstance(quality.get("hitrace_tag_catalog"), dict) else {}
    provenance_refs = _hitrace_provenance_refs({"marker": {"catalog": tag_catalog}})
    scope = dict(catalog_scope or {})
    if not scope and cluster is not None:
        scope = {
            "kind": "cluster_overlap",
            "scope": "cluster_time_window",
            "cluster_id": cluster.get("cluster_id"),
            "time_range_ns": cluster.get("time_range_ns"),
        }
    return store.write(
        "trace_event_catalog",
        {
            "analysis_artifact_id": analysis["artifact_id"],
            "catalog_scope": scope,
            **({"cluster_id": cluster["cluster_id"], "time_range_ns": cluster.get("time_range_ns")} if cluster is not None else {}),
            "events": catalog.get("events", []),
            "module_event_groups": catalog.get("module_event_groups", []),
            "module_catalog": catalog.get("module_catalog", {}),
            "quality": catalog.get("quality", {}),
        },
        source_connector_id=analysis["source_connector_id"],
        source_binding=analysis["source_binding"],
        producer={"name": "trace-event-extraction", "version": "1.2.0"},
        input_artifacts=[f"artifact:{analysis['artifact_id']}", f"sha256:{analysis['content_sha256']}", *provenance_refs],
    )


def _event_source_search_terms(event_name: str) -> list[str]:
    """Extract conservative, source-searchable C/C++ identifiers from a trace label."""

    cleaned = re.sub(r"^H:\s*", "", event_name.strip())
    cleaned = cleaned.split("|", 1)[0].strip()
    qualified = re.findall(r"((?:[A-Za-z_]\w*::)+[A-Za-z_]\w*)\s*\(", cleaned)
    terms: list[str] = []
    if qualified:
        parts = qualified[-1].split("::")
        if len(parts) >= 2:
            terms.append("::".join(parts[-2:]))
        terms.append(parts[-1])
    else:
        called = re.findall(r"\b([A-Za-z_]\w*)\s*\(", cleaned)
        if called:
            terms.append(called[-1])
        elif re.fullmatch(r"[A-Za-z_]\w*", cleaned):
            terms.append(cleaned)
    return list(dict.fromkeys(term for term in terms if len(term) >= 8 and len(term) <= 160))


def _source_match_kind(line_text: str, term: str) -> str:
    index = line_text.find(term)
    prefix = line_text[:index] if index >= 0 else ""
    if re.search(r"(?:->|\.)\s*$", prefix):
        return "reference"
    if "::" in prefix or re.search(r"\b(?:void|bool|char|short|int|long|float|double|auto|static|virtual|inline)\b", prefix):
        return "definition_or_declaration"
    return "text_match"


def _search_source_for_event_symbol(source_connector_id: str, event_name: str) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    terms = _event_source_search_terms(event_name)
    if not terms:
        return [], {"status": "not_searchable", "reason": "Trace event name has no sufficiently specific C/C++ symbol.", "terms": []}
    if shutil.which("rg") is None:
        return [], {"status": "unavailable", "reason": "ripgrep is unavailable for bounded source lookup.", "terms": terms}
    connector = connectors.get(source_connector_id)
    root = Path(connector["source_root"]).resolve()
    candidates: dict[tuple[str, int], dict[str, Any]] = {}
    diagnostics: list[str] = []
    for term in terms:
        command = ["rg", "--json", "--no-messages", "--max-count", "2"]
        for glob in SOURCE_SEARCH_GLOBS:
            command.extend(["--glob", glob])
        command.extend(["-e", rf"\b{re.escape(term)}\s*\(", "."])
        try:
            completed = subprocess.run(
                command,
                cwd=root,
                text=True,
                capture_output=True,
                timeout=EVENT_SOURCE_SEARCH_TIMEOUT_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            diagnostics.append(f"Source lookup timed out for {term!r}.")
            continue
        if completed.returncode not in {0, 1}:
            diagnostics.append(f"Source lookup failed for {term!r} with exit code {completed.returncode}.")
            continue
        for raw in completed.stdout.splitlines():
            try:
                record = json.loads(raw)
            except json.JSONDecodeError:
                continue
            if record.get("type") != "match" or not isinstance(record.get("data"), dict):
                continue
            data = record["data"]
            path_data = data.get("path") if isinstance(data.get("path"), dict) else {}
            line_data = data.get("lines") if isinstance(data.get("lines"), dict) else {}
            path_text = path_data.get("text")
            line_text = line_data.get("text")
            line_number = data.get("line_number")
            if not isinstance(path_text, str) or not isinstance(line_text, str):
                continue
            try:
                line = int(line_number)
                source = (root / path_text).resolve()
                relative_path = str(source.relative_to(root))
            except (TypeError, ValueError):
                continue
            kind = _source_match_kind(line_text, term)
            key = (relative_path, line)
            previous = candidates.get(key)
            candidate = {
                "path": relative_path,
                "line": line,
                "search_term": term,
                "match_kind": kind,
                "line_text": line_text.rstrip(),
            }
            if previous is None or (previous["match_kind"] != "definition_or_declaration" and kind == "definition_or_declaration"):
                candidates[key] = candidate
            if len(candidates) >= MAX_EVENT_SOURCE_CANDIDATES:
                break
        if len(candidates) >= MAX_EVENT_SOURCE_CANDIDATES:
            break
        if any(item["match_kind"] == "definition_or_declaration" for item in candidates.values()):
            break
    ordered = sorted(
        candidates.values(),
        key=lambda item: (item["match_kind"] != "definition_or_declaration", item["match_kind"] == "text_match", item["path"], item["line"]),
    )[:MAX_EVENT_SOURCE_CANDIDATES]
    return ordered, {
        "status": "available" if ordered else "no_match",
        "terms": terms,
        "diagnostics": diagnostics,
        "bounded_candidate_limit": MAX_EVENT_SOURCE_CANDIDATES,
    }


def _function_target_from_scope(scope: dict[str, Any] | None) -> dict[str, Any] | None:
    if not isinstance(scope, dict) or not isinstance(scope.get("name"), str) or not scope["name"].strip():
        return None
    target: dict[str, Any] = {"name": scope["name"], "kind": str(scope.get("kind") or "textual_containing_function")}
    for key in ("line", "declaration_line", "body_start_line", "body_end_line"):
        try:
            target[key] = int(scope[key])
        except (KeyError, TypeError, ValueError):
            continue
    return target


def _hitrace_code_match_evidence(candidate: dict[str, Any]) -> dict[str, Any]:
    """Describe the exact source lines that justify browsing and AST targeting."""

    try:
        instrumentation_line = int(candidate["line"])
    except (KeyError, TypeError, ValueError):
        instrumentation_line = 0
    target_function = _function_target_from_scope(
        candidate.get("enclosing_function") if isinstance(candidate.get("enclosing_function"), dict) else None
    )
    matched_lines: list[dict[str, Any]] = []
    if instrumentation_line > 0:
        matched_lines.append(
            {
                "line": instrumentation_line,
                "role": "instrumentation_point",
                "label": "精确匹配的 HiTrace 插桩点",
            }
        )
    if target_function is not None:
        declaration_line = int(target_function.get("declaration_line") or target_function.get("line") or 0)
        if declaration_line > 0:
            matched_lines.append(
                {
                    "line": declaration_line,
                    "role": "enclosing_function",
                    "label": f"AST 目标函数 {target_function['name']}",
                }
            )
    return {
        "kind": "hitrace_instrumentation",
        "matched_lines": matched_lines,
        "ast_target": target_function,
    }


def _declared_source_match_evidence(line: int) -> dict[str, Any]:
    return {
        "kind": "trace_declared_source_location",
        "matched_lines": [
            {
                "line": line,
                "role": "trace_declared_source",
                "label": "上游声明的源码位置",
            }
        ],
        "ast_target": None,
    }


def _symbol_search_match_evidence(candidate: dict[str, Any]) -> dict[str, Any]:
    try:
        line = int(candidate["line"])
    except (KeyError, TypeError, ValueError):
        line = 0
    return {
        "kind": "bounded_symbol_text_search",
        "matched_lines": [
            {"line": line, "role": "symbol_text_match", "label": "受限符号文本匹配"}
        ] if line > 0 else [],
        "ast_target": None,
    }


def _build_trace_event_code_block(
    source_connector_id: str,
    path: str,
    line: int,
    *,
    match_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = connectors.resolve_source_path(source_connector_id, path)
    if source.stat().st_size > 8 * 1024 * 1024:
        raise PlatformError("SOURCE_FILE_TOO_LARGE", "Source code block has an 8 MiB Phase-A limit.", status_code=413)
    text = source.read_text(encoding="utf-8", errors="replace")
    precheck = _structural_precheck(text, line)
    return {
        "source": {"path": path, "line": line, "sha256": sha256_file(source)},
        "structural_precheck": precheck,
        **({"match_evidence": match_evidence} if match_evidence is not None else {}),
    }


def _hitrace_instrumentation_resolution(candidate: dict[str, Any]) -> dict[str, str]:
    instrumentation = candidate.get("instrumentation") if isinstance(candidate.get("instrumentation"), dict) else {}
    label_match = str(instrumentation.get("event_label_match") or "")
    tag_match = str(instrumentation.get("tag_match") or "")
    tag_verified = tag_match in {"direct", "wrapper_definition"}
    if label_match == "exact_literal" and tag_verified:
        state = "instrumentation_point_exact"
        detail = "The HiTrace label is an exact source literal and its serialized tag bits match the instrumentation macro. With an exact trace-to-snapshot binding, this is eligible source-location evidence for AST and KB matching."
    elif label_match == "function_derived" and tag_verified:
        state = "instrumentation_point_function_derived"
        detail = "The source macro derives its label from __func__ or __PRETTY_FUNCTION__; the trace label contains the same textual function and its serialized tag bits match. With an exact trace-to-snapshot binding, this is eligible source-location evidence for AST and KB matching."
    elif label_match in {"normalized_format", "static_prefix"} and tag_verified:
        state = "instrumentation_point_pattern"
        detail = "The HiTrace label matches a source format string or static prefix and its serialized tag bits match the instrumentation macro."
    else:
        state = "instrumentation_point_candidate"
        detail = "The HiTrace label matches a source instrumentation macro, but tag verification or literal equality is incomplete."
    return {
        "state": state,
        "confidence": "instrumentation_evidence",
        "method": "hitrace_tag_instrumentation_lookup",
        "reason": (
            f"{detail} This evidence never establishes the trace-to-source snapshot binding by itself."
        ),
    }


def _connector_scope_coverage(source_root: Path, required_relative_roots: list[str]) -> tuple[list[str], list[str]]:
    """Return curated source scopes available inside one registered connector.

    This is intentionally a directory-only preflight.  It may suggest a
    different registered connector, but it never searches that connector for
    the event or treats it as trace-to-source evidence.
    """

    root = source_root.resolve()
    covered: list[str] = []
    missing: list[str] = []
    for relative_root in required_relative_roots:
        candidate_relative = Path(relative_root)
        if candidate_relative.is_absolute() or ".." in candidate_relative.parts:
            continue
        candidates = [candidate_relative]
        if (root / "openharmony").is_dir() and candidate_relative.parts[:1] != ("openharmony",):
            candidates.append(Path("openharmony") / candidate_relative)
        found_path: Path | None = None
        for candidate_relative_path in candidates:
            candidate = (root / candidate_relative_path).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                continue
            if candidate.is_dir():
                found_path = candidate_relative_path
                break
        if found_path is None:
            missing.append(relative_root)
        else:
            covered.append(str(found_path))
    return covered, missing


def _registered_connector_scope_recommendations(
    source_connector_id: str,
    missing_relative_roots: list[str],
) -> list[dict[str, Any]]:
    """Find registered source roots that can cover a missing HiTrace scope.

    A recommendation is only an operator convenience.  The selected trace is
    still bound to its original connector until the user explicitly attests the
    selected development snapshot through the normal binding flow.
    """

    if not missing_relative_roots:
        return []
    recommendations: list[dict[str, Any]] = []
    for connector in connectors.list():
        connector_id = connector.get("connector_id")
        source_root = connector.get("source_root")
        if not isinstance(connector_id, str) or not isinstance(source_root, str) or connector_id == source_connector_id:
            continue
        root = Path(source_root).expanduser().resolve()
        if not root.is_dir():
            continue
        covered, still_missing = _connector_scope_coverage(root, missing_relative_roots)
        if still_missing:
            continue
        snapshot = connector.get("snapshot") if isinstance(connector.get("snapshot"), dict) else {}
        recommendations.append(
            {
                "connector_id": connector_id,
                "display_name": connector.get("display_name") if isinstance(connector.get("display_name"), str) else connector_id,
                "source_root": str(root),
                "snapshot_id": snapshot.get("snapshot_id") if isinstance(snapshot.get("snapshot_id"), str) else None,
                "snapshot_clean": bool(snapshot.get("clean")),
                "snapshot_artifact_id": connector.get("snapshot_artifact_id") if isinstance(connector.get("snapshot_artifact_id"), str) else None,
                "required_relative_roots": list(missing_relative_roots),
                "covered_relative_roots": covered,
                "recommendation_kind": "scope_coverage_only",
            }
        )
    return sorted(
        recommendations,
        key=lambda item: (
            not bool(item.get("snapshot_clean")),
            str(item.get("source_root") or ""),
            str(item.get("connector_id") or ""),
        ),
    )


def _resolve_trace_event_source(
    analysis: dict[str, Any],
    cluster: dict[str, Any] | None,
    event: dict[str, Any],
    event_catalog_artifact: dict[str, Any],
    *,
    catalog_scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    connector_id = str(analysis["source_connector_id"])
    declared_source = event.get("source_location") if isinstance(event.get("source_location"), dict) else None
    source_candidates: list[dict[str, Any]] = []
    code_block: dict[str, Any] | None = None
    search: dict[str, Any] | None = None
    hitrace_instrumentation: dict[str, Any] | None = None
    located_function: dict[str, Any] | None = None
    connector_recommendations: list[dict[str, Any]] = []
    resolution: dict[str, Any]

    if declared_source is not None:
        path = declared_source.get("path")
        line = declared_source.get("line")
        try:
            declared_line = int(line)
            code_block = _build_trace_event_code_block(
                connector_id,
                str(path),
                declared_line,
                match_evidence=_declared_source_match_evidence(declared_line),
            )
            resolution = {
                "state": "declared_source",
                "confidence": "trace_declared",
                "method": "external_trace_source_location",
                "reason": "The upstream trace report supplied this source path and line.",
            }
        except (PlatformError, TypeError, ValueError) as exc:
            reason = exc.message if isinstance(exc, PlatformError) else str(exc)
            resolution = {
                "state": "unresolved",
                "confidence": "not_scored",
                "method": "external_trace_source_location",
                "reason": f"Declared source location cannot be opened: {reason}",
            }
    elif event.get("event_kind") != "callstack_span":
        resolution = {
            "state": "unresolved",
            "confidence": "not_scored",
            "method": "not_applicable",
            "reason": "This scheduler label is not a source symbol and cannot be mapped to a code block.",
        }
    else:
        connector = connectors.get(connector_id)
        source_root = Path(str(connector["source_root"])).resolve()
        tag_catalog = load_hitrace_tag_catalog(source_root, WORKSPACE_ROOT)
        hitrace_instrumentation = locate_hitrace_instrumentation(
            source_root,
            WORKSPACE_ROOT,
            str(event.get("event_name") or ""),
            tag_catalog=tag_catalog,
            max_candidates=MAX_EVENT_SOURCE_CANDIDATES,
            timeout_seconds=EVENT_SOURCE_SEARCH_TIMEOUT_SECONDS,
        )
        instrumentation_candidates = hitrace_instrumentation.get("candidates")
        source_scope = hitrace_instrumentation.get("source_scope") if isinstance(hitrace_instrumentation.get("source_scope"), dict) else {}
        missing_scope_roots = source_scope.get("missing_relative_roots")
        if not instrumentation_candidates and isinstance(missing_scope_roots, list):
            connector_recommendations = _registered_connector_scope_recommendations(
                connector_id,
                [scope for scope in missing_scope_roots if isinstance(scope, str)],
            )
            if connector_recommendations:
                # These are scope-only preflight results, not cross-connector
                # source candidates.  Keep them beside the failed lookup so
                # their limited evidentiary meaning is explicit in the artifact.
                hitrace_instrumentation["source_connector_recommendations"] = connector_recommendations
        if isinstance(instrumentation_candidates, list) and instrumentation_candidates:
            source_candidates = [item for item in instrumentation_candidates if isinstance(item, dict)]
            candidate = source_candidates[0]
            located_function = candidate.get("enclosing_function") if isinstance(candidate.get("enclosing_function"), dict) else None
            try:
                code_block = _build_trace_event_code_block(
                    connector_id,
                    str(candidate["path"]),
                    int(candidate["line"]),
                    match_evidence=_hitrace_code_match_evidence(candidate),
                )
                resolution = _hitrace_instrumentation_resolution(candidate)
            except (PlatformError, TypeError, ValueError) as exc:
                reason = exc.message if isinstance(exc, PlatformError) else str(exc)
                resolution = {
                    "state": "unresolved",
                    "confidence": "not_scored",
                    "method": "hitrace_tag_instrumentation_lookup",
                    "reason": f"HiTrace instrumentation candidate cannot be opened: {reason}",
                }
            search = {"hitrace": hitrace_instrumentation}
        else:
            source_candidates, symbol_search = _search_source_for_event_symbol(connector_id, str(event.get("event_name") or ""))
            search = {"hitrace": hitrace_instrumentation, "symbol": symbol_search}
        if not source_candidates and (not isinstance(instrumentation_candidates, list) or not instrumentation_candidates):
            hitrace_reason = (
                hitrace_instrumentation.get("reason")
                if isinstance(hitrace_instrumentation, dict)
                and hitrace_instrumentation.get("status") in {"no_match", "unavailable"}
                and hitrace_instrumentation.get("reason")
                else None
            )
            symbol_reason = symbol_search.get("reason") if isinstance(symbol_search, dict) else None
            resolution = {
                "state": "unresolved",
                "confidence": "not_scored",
                "method": "hitrace_tag_instrumentation_lookup_and_bounded_symbol_text_search",
                "reason": str(hitrace_reason or symbol_reason or "No source candidate was found for this raw trace event."),
            }
        elif not (isinstance(instrumentation_candidates, list) and instrumentation_candidates):
            candidate = source_candidates[0]
            try:
                code_block = _build_trace_event_code_block(
                    connector_id,
                    candidate["path"],
                    int(candidate["line"]),
                    match_evidence=_symbol_search_match_evidence(candidate),
                )
                resolution = {
                    "state": "symbol_text_candidate" if candidate["match_kind"] == "definition_or_declaration" else "symbol_text_reference",
                    "confidence": "candidate",
                    "method": "bounded_symbol_text_search",
                    "reason": "Raw trace does not carry a source location; this code block is a text-search candidate, not a proven trace-to-source mapping.",
                }
            except PlatformError as exc:
                resolution = {
                    "state": "unresolved",
                    "confidence": "not_scored",
                    "method": "bounded_symbol_text_search",
                    "reason": f"Source candidate cannot be opened: {exc.message}",
                }

    scope = dict(catalog_scope or {})
    if not scope and cluster is not None:
        scope = {
            "kind": "cluster_overlap",
            "scope": "cluster_time_window",
            "cluster_id": cluster.get("cluster_id"),
            "time_range_ns": cluster.get("time_range_ns"),
        }
    payload = {
        "analysis_artifact_id": analysis["artifact_id"],
        "catalog_scope": scope,
        **({"cluster_id": cluster["cluster_id"]} if cluster is not None else {}),
        "event_catalog_artifact_id": event_catalog_artifact["artifact_id"],
        "event": event,
        "resolution": {
            **resolution,
            "trace_binding_match_level": analysis.get("source_binding", {}).get("match_level"),
        },
        "source_candidates": source_candidates,
        "source_search": search,
        "hitrace_instrumentation": hitrace_instrumentation,
        "located_function": located_function,
        "code_block": code_block,
    }
    provenance_refs = _hitrace_provenance_refs(hitrace_instrumentation or {})
    recommendation_refs = [
        f"artifact:{item['snapshot_artifact_id']}"
        for item in connector_recommendations
        if isinstance(item.get("snapshot_artifact_id"), str)
    ]
    return store.write(
        "trace_event_source_resolution",
        payload,
        source_connector_id=connector_id,
        source_binding=analysis["source_binding"],
        producer={"name": "trace-event-source-resolution", "version": "1.4.0"},
        input_artifacts=list(
            dict.fromkeys(
                [
                    f"artifact:{analysis['artifact_id']}",
                    f"sha256:{analysis['content_sha256']}",
                    f"artifact:{event_catalog_artifact['artifact_id']}",
                    f"sha256:{event_catalog_artifact['content_sha256']}",
                    *provenance_refs,
                    *recommendation_refs,
                ]
            )
        ),
    )


def _positive_line(value: Any) -> int | None:
    try:
        line = int(value)
    except (TypeError, ValueError):
        return None
    return line if line > 0 else None


def _source_language(path: str) -> str:
    suffix = Path(path).suffix.casefold()
    if suffix in {".c"}:
        return "c"
    if suffix in {".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"}:
        return "cpp"
    return "unknown"


def _hotspot_code_unit_from_resolution(source_resolution: dict[str, Any]) -> dict[str, Any]:
    """Extract the verified trace-anchor function for one resolved event.

    A local excerpt is deliberately insufficient: the model and later AST work
    receive a function range proven by the event resolver.  The resulting unit
    is still an anchor, not proof that this function owns the observed span or
    that one of its callees consumed CPU time.
    """

    resolution = source_resolution.get("resolution") if isinstance(source_resolution.get("resolution"), dict) else {}
    resolution_state = str(resolution.get("state") or "unresolved")
    if resolution_state not in AST_ELIGIBLE_SOURCE_RESOLUTION_STATES:
        raise PlatformError(
            "HOTSPOT_SOURCE_EVIDENCE_REQUIRED",
            "The selected event has no trace-proven source location. Text-search candidates cannot be converted into a hotspot function; provide an upstream file:line or paste the intended code explicitly.",
            status_code=422,
        )
    code_block = source_resolution.get("code_block") if isinstance(source_resolution.get("code_block"), dict) else {}
    source = code_block.get("source") if isinstance(code_block.get("source"), dict) else {}
    path = source.get("path")
    event_line = _positive_line(source.get("line"))
    if not isinstance(path, str) or not path or event_line is None:
        raise PlatformError(
            "HOTSPOT_SOURCE_UNAVAILABLE",
            "The selected real trace event has no readable source location.",
            status_code=422,
        )
    match_evidence = code_block.get("match_evidence") if isinstance(code_block.get("match_evidence"), dict) else {}
    target = match_evidence.get("ast_target") if isinstance(match_evidence.get("ast_target"), dict) else {}
    if not target:
        target = source_resolution.get("located_function") if isinstance(source_resolution.get("located_function"), dict) else {}
    start = _positive_line(target.get("declaration_line") or target.get("line"))
    end = _positive_line(target.get("body_end_line"))
    if start is None or end is None or end < start or not (start <= event_line <= end):
        raise PlatformError(
            "HOTSPOT_FUNCTION_RANGE_REQUIRED",
            "The event source is located, but no verified containing-function range is available. Re-resolve the event with AST-capable source scope, or paste the intended function explicitly.",
            status_code=422,
        )
    connector_id = str(source_resolution["source_connector_id"])
    source_file = connectors.resolve_source_path(connector_id, path)
    expected_sha = source.get("sha256")
    current_sha = sha256_file(source_file)
    if isinstance(expected_sha, str) and expected_sha and expected_sha != current_sha:
        raise PlatformError(
            "HOTSPOT_SOURCE_CHANGED",
            "Source content changed after event resolution. Re-resolve the trace event against the current source snapshot before extracting its function.",
            status_code=409,
            details={"expected_sha256": expected_sha, "current_sha256": current_sha},
        )
    lines = source_file.read_text(encoding="utf-8", errors="replace").splitlines()
    if end > len(lines):
        raise PlatformError("HOTSPOT_FUNCTION_RANGE_INVALID", "Resolved function range exceeds the current source file.", status_code=422)
    code = "\n".join(lines[start - 1 : end])
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise PlatformError(
            "HOTSPOT_FUNCTION_TOO_LARGE",
            f"Resolved function exceeds the {MAX_CODE_BYTES // 1024} KiB model-context limit; choose a narrower hot call site or paste the verified compute core.",
            status_code=413,
        )
    event = source_resolution.get("event") if isinstance(source_resolution.get("event"), dict) else {}
    marker = event.get("hitrace_marker") if isinstance(event.get("hitrace_marker"), dict) else {}
    module_tags = marker.get("module_tags") if isinstance(marker.get("module_tags"), list) else []
    module_names = [str(item.get("display_name")) for item in module_tags if isinstance(item, dict) and item.get("display_name")]
    binding = source_resolution.get("source_binding") if isinstance(source_resolution.get("source_binding"), dict) else {}
    function_name = str(target.get("name") or event.get("event_name") or "selected_function")
    return {
        "hotspot": {
            "event_id": str(event.get("event_id") or "selected_event"),
            "event_name": str(event.get("event_name") or function_name),
            "module": str(event.get("module") or " · ".join(module_names) or "unknown"),
            "trace_metrics": event.get("metrics") if isinstance(event.get("metrics"), dict) else {},
            "critical_path_status": "not_provided",
        },
        "source": {
            "origin": "resolved_real_trace_event",
            "path": path,
            "line_start": start,
            "line_end": end,
            "event_line": event_line,
            "sha256": current_sha,
            "binding_level": str(binding.get("match_level") or "unknown"),
            "resolution_state": str(resolution.get("state") or "unresolved"),
            "source_resolution_artifact_id": source_resolution["artifact_id"],
        },
        "code": code,
        "code_language": _source_language(path),
        "function_hint": function_name,
        "selection": {
            "kind": "verified_containing_function",
            "semantic_role": "trace_anchor_function",
            "function": target,
            "event_line": event_line,
            "source_resolution_artifact_id": source_resolution["artifact_id"],
        },
        "hotspot_attribution": {
            "status": "requires_code_path_investigation",
            "trace_anchor_observed": True,
            "cpu_self_time_available": False,
            "callee_attribution_available": False,
            "selected_compute_core": None,
            "reason": "The trace event proves an instrumentation anchor and inclusive span only. Inspect local regions, callers, callees, and external runtime boundaries before hardware routing.",
        },
    }


def _meets_temporal_evidence_gate(hotspot: dict[str, Any]) -> tuple[bool, str]:
    """Require local pressure, not a long-run aggregate, before candidate generation."""

    pressure = str(hotspot.get("temporal_pressure") or "low")
    critical_path = hotspot.get("critical_path_metrics")
    is_critical = isinstance(critical_path, dict) and bool(critical_path.get("available")) and bool(critical_path.get("is_critical"))
    if pressure == "high":
        return True, "high_per_invocation_pressure"
    if pressure == "medium" and is_critical:
        return True, "critical_path_with_material_per_invocation_pressure"
    if pressure == "medium":
        return False, "medium pressure requires an upstream typed critical-path assertion before automatic opportunity generation"
    return False, "low per-invocation pressure does not satisfy the automatic opportunity gate"


def _included_kb_matches(payload: dict[str, Any], *, include_unverified: bool) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    features = payload.get("features")
    match_results = payload.get("kb_matches")
    if not isinstance(features, list) or not isinstance(match_results, list):
        return []
    included: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for index, feature in enumerate(features):
        if not isinstance(feature, dict) or index >= len(match_results):
            continue
        match_result = match_results[index]
        if not isinstance(match_result, dict):
            continue
        matches = match_result.get("matches")
        if not isinstance(matches, list):
            continue
        for match in matches:
            if not isinstance(match, dict):
                continue
            if not include_unverified and match.get("knowledge_readiness") != "release":
                continue
            included.append((feature, match))
    return included


def _analysis_step(
    *,
    state: str,
    detail: str,
    analysis: dict[str, Any],
    selection: dict[str, Any],
    input_artifacts: list[str] | None = None,
) -> dict[str, Any]:
    return store.write(
        "analysis_run_step",
        {
            "state": state,
            "detail": detail,
            "analysis_artifact_id": analysis["artifact_id"],
            "selection_artifact_id": selection["artifact_id"],
        },
        source_connector_id=analysis["source_connector_id"],
        source_binding=analysis["source_binding"],
        producer={"name": "heterogeneous-opportunity-orchestrator", "version": "1.0.0"},
        input_artifacts=input_artifacts
        or [f"artifact:{analysis['artifact_id']}", f"artifact:{selection['artifact_id']}"],
    )


@app.get("/v1/live")
def liveness() -> dict[str, str]:
    """Return an O(1) process probe without touching runtime registries."""

    return {
        "schema_version": "service_liveness/v1",
        "status": "ok",
        "service": "heterogeneous-enablement-api",
        "project_id": PROJECT_ID,
    }


@app.get("/v1/health")
@app.get("/api/health")
def health() -> dict[str, Any]:
    catalog = ensure_catalog(CASE_CATALOG_PATH, WORKSPACE_ROOT)
    return {
        "status": "ok",
        "project_id": PROJECT_ID,
        "case_count": catalog["case_count"],
        "source_connector_count": len(connectors.list()),
        "default_source_root": str(default_source_root()),
        "artifact_store": store.summary(),
    }


@app.get("/v1/platform-version")
def platform_version() -> dict[str, Any]:
    """Return the build loaded by this API process and its MCP contract."""

    return platform_version_payload()


@app.get("/v1/deployment")
def deployment_configuration() -> dict[str, Any]:
    return deployment_capabilities()


@app.get("/v1/overview")
def overview() -> dict[str, Any]:
    catalog = ensure_catalog(CASE_CATALOG_PATH, WORKSPACE_ROOT)
    connector, connector_status = _optional_default_connector()
    optimization_kb = _optimization_kb()
    hardware_kb = _hardware_kb()
    optimization_metadata = _kb_public_metadata(optimization_kb)
    return {
        "project": {"project_id": PROJECT_ID, "mode": "single_user_single_machine"},
        "deployment": deployment_capabilities(),
        "default_connector": connector,
        "default_connector_status": connector_status,
        "connectors": connectors.list(),
        "optimization_kb": optimization_metadata,
        "hardware_kb": _kb_public_metadata(hardware_kb),
        "custom_api_knowledge_bases": custom_api_kbs.list(),
        "application_flow_assets": application_flows.list_assets(limit=20),
        # Compatibility alias for pre-split clients.  New clients must use
        # optimization_kb and hardware_kb explicitly.
        "kb": {
            "kb_id": optimization_metadata["kb_id"],
            "version": optimization_metadata["version"],
            "source_revision": optimization_metadata["source_revision"],
            "manifest_sha256": optimization_metadata["content_sha256"],
        },
        "case_catalog": {"catalog_id": catalog["catalog_id"], "case_count": catalog["case_count"], "content_sha256": catalog["content_sha256"]},
        "fixtures": [_fixture_metadata(fixture_id) for fixture_id in FIXTURES],
        "artifact_store": store.summary(),
        "agent_mode": {
            "runtime_location": "platform_backend",
            "selected_engine": "kimi_server" if isinstance(agent_runtime, KimiEngineAgentRuntime) else "legacy",
            "session_owner": "agent_engine",
            "task_and_evidence_owner": "platform_backend",
            "browser_role": "task_assembly_visualization_and_ui_actions",
            "model_key_storage": "agent_engine_managed" if isinstance(agent_runtime, KimiEngineAgentRuntime) else "browser_local_storage_only",
            "server_receives_model_key": False if isinstance(agent_runtime, KimiEngineAgentRuntime) else "ephemeral_relay_only",
            "legacy_model_relay_allowed_base_urls": sorted(MODEL_RELAY_ALLOWED_BASE_URLS),
        },
    }


@app.post("/v1/model-relay/chat-completions")
def relay_model_chat_completion(request: ModelRelayChatCompletionRequest) -> dict[str, Any]:
    """Compatibility completion endpoint backed by the selected AgentEngine."""

    if AGENT_ENGINE_KIND != "legacy" and request.execution_mode != "direct_relay":
        run_id, binding = _structured_session_binding(request)
        try:
            _append_structured_session_event(run_id, "analysis.model_request", request, {
                "message_count": len(request.messages),
                "messages_sha256": sha256_json({"messages": request.messages}),
            })
            payload, result = kimi_completion_adapter.complete(
                messages=request.messages,
                max_tokens=request.max_tokens,
                binding=binding,
                conversation_id=request.analysis_id or "",
                platform_run_id=run_id,
                thinking=_structured_thinking_level(request),
                before_prompt=(lambda: _set_structured_prompt_tool_policy(run_id, request, active=True)) if run_id and binding is not None else None,
                after_prompt=(lambda: _set_structured_prompt_tool_policy(run_id, request, active=False)) if run_id and binding is not None else None,
            )
            _append_structured_session_event(run_id, "analysis.model_response", request, {
                "prompt_id": result.prompt_id,
                "finish_reason": result.reason,
                "usage": payload.get("usage", {}),
                "content_sha256": sha256_json({"content": result.assistant_text}),
            })
            return payload
        except KimiServerError as exc:
            _append_structured_session_event(run_id, "analysis.model_error", request, {"code": exc.code, "message": exc.message})
            raise PlatformError(exc.code, exc.message, status_code=502, details=exc.details) from exc
    return _relay_chat_completion(request)


@app.post("/v1/model-relay/chat-completions/stream")
def relay_model_chat_completion_stream(request: ModelRelayChatCompletionRequest) -> StreamingResponse:
    """Stream the selected AgentEngine using the existing OpenAI-shaped SSE."""

    if AGENT_ENGINE_KIND != "legacy" and request.execution_mode != "direct_relay":
        run_id, binding = _structured_session_binding(request)
        output: queue.Queue[bytes | None] = queue.Queue(maxsize=2048)
        cancel_event = threading.Event()

        def emit(payload: dict[str, Any]) -> None:
            output.put(f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8"))

        def on_event(event: dict[str, Any]) -> None:
            event_type = str(event.get("type") or "")
            payload = _record(event.get("payload"))
            if event_type == "thinking.delta" and isinstance(payload.get("delta"), str):
                emit({"choices": [{"index": 0, "delta": {"reasoning_content": payload["delta"]}, "finish_reason": None}]})
            elif event_type == "assistant.delta" and isinstance(payload.get("delta"), str):
                emit({"choices": [{"index": 0, "delta": {"content": payload["delta"]}, "finish_reason": None}]})

        def worker() -> None:
            try:
                _append_structured_session_event(run_id, "analysis.model_request", request, {
                    "message_count": len(request.messages),
                    "messages_sha256": sha256_json({"messages": request.messages}),
                    "transport": "streaming",
                })
                payload, result = kimi_completion_adapter.complete(
                    messages=request.messages,
                    max_tokens=request.max_tokens,
                    on_event=on_event,
                    cancel_event=cancel_event,
                    binding=binding,
                    conversation_id=request.analysis_id or "",
                    platform_run_id=run_id,
                    thinking=_structured_thinking_level(request),
                    before_prompt=(lambda: _set_structured_prompt_tool_policy(run_id, request, active=True)) if run_id and binding is not None else None,
                    after_prompt=(lambda: _set_structured_prompt_tool_policy(run_id, request, active=False)) if run_id and binding is not None else None,
                )
                _append_structured_session_event(run_id, "analysis.model_response", request, {
                    "prompt_id": result.prompt_id,
                    "finish_reason": result.reason,
                    "usage": payload.get("usage", {}),
                    "content_sha256": sha256_json({"content": result.assistant_text}),
                })
                usage = _record(payload.get("usage"))
                emit({
                    "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                    "usage": usage,
                    "agent_engine": payload.get("agent_engine", {}),
                })
                output.put(b"data: [DONE]\n\n")
            except KimiServerError as exc:
                _append_structured_session_event(run_id, "analysis.model_error", request, {"code": exc.code, "message": exc.message})
                output.put(_relay_stream_error_event(exc.code, exc.message, exc.details))
            except Exception as exc:  # pragma: no cover - final streaming isolation boundary
                output.put(_relay_stream_error_event("KIMI_COMPLETION_FAILED", "Kimi Agent 结构化推理失败。", {"error": str(exc)[:1200]}))
            finally:
                output.put(None)

        threading.Thread(target=worker, name="kimi-structured-inference", daemon=True).start()

        def events() -> Iterator[bytes]:
            try:
                while True:
                    chunk = output.get()
                    if chunk is None:
                        return
                    yield chunk
            finally:
                cancel_event.set()

        return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})
    return _relay_chat_completion_stream(request)


def _agent_context_record(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _normalized_agent_run_context(mode: str, value: Any) -> dict[str, Any]:
    """Keep device-validation state independent from the full page snapshot.

    The initial validation task already contains the bounded hotspot source,
    strategy projection and target unit.  Persisting ``page_context`` beside
    it duplicated code previews and strategy summaries on every model turn.
    This server-side whitelist also protects upgraded backends from older
    browser clients that still submit the former broad context shape.
    """

    context = _agent_context_record(value)
    if mode != "device_validation":
        return context
    page_context = _agent_context_record(context.get("page_context"))
    target_profile = _record(context.get("target_platform_profile")) or _record(page_context.get("target_platform_profile"))
    execution_mode = str(context.get("device_execution_mode") or "local_hdc")
    if execution_mode not in {"local_hdc", "remote_bridge"}:
        execution_mode = "local_hdc"
    normalized = {
        "schema_version": "device_validation_runtime_context/v1",
        "page_session_id": str(context.get("page_session_id") or "")[:160],
        "analysis_record_id": str(context.get("analysis_record_id") or "")[:160],
        "target_platform_profile": target_profile,
        "device_validation_facts": _record(context.get("device_validation_facts")),
        "device_execution_mode": execution_mode,
    }
    skill_profile = _record(context.get("skill_profile"))
    if skill_profile:
        normalized["skill_profile"] = skill_profile
    runtime_environment = _record(context.get("runtime_environment"))
    if runtime_environment:
        normalized["runtime_environment"] = runtime_environment
    target = _record(context.get("device_validation_target"))
    requested_unit = str(target.get("requested_unit") or "").casefold()
    recommended_profile_id = str(target.get("recommended_profile_id") or "")
    compatible_profile_ids = [
        str(item)
        for item in target.get("compatible_profile_ids", [])
        if isinstance(item, str) and item in EXPERIMENT_PROFILES
    ][:8]
    if requested_unit in {"npu", "gpu", "dsp", "media", "parallel_cpu"}:
        normalized["device_validation_target"] = {
            "requested_unit": requested_unit,
            "recommended_profile_id": recommended_profile_id if recommended_profile_id in EXPERIMENT_PROFILES else None,
            "compatible_profile_ids": compatible_profile_ids,
        }
    selector = _record(context.get("device_selector"))
    bridge_id = str(selector.get("bridge_id") or "")[:160]
    if execution_mode == "remote_bridge" and bridge_id:
        normalized["device_selector"] = {"bridge_id": bridge_id}
    return normalized


def _agent_runtime_environment(
    adapter: AgentCliAdapter,
    credential: AgentRunCredentialRequest | None,
    *,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Capture non-secret identities needed for cross-environment diagnosis."""

    previous = _record(previous)
    model_name = credential.model.strip() if credential else _text(previous.get("model_name"), 240)
    provider_host = ""
    if credential:
        try:
            provider_host = str(urlparse(credential.base_url).hostname or "")[:240]
        except ValueError:
            provider_host = ""
    if model_name in {"", "unknown"} and adapter.adapter_id == "kimi":
        try:
            model_name = _text(_record(kimi_agent_engine.health()).get("model"), 240)
        except (KimiServerError, OSError):
            model_name = ""
    return {
        "schema_version": "agent_execution_environment/v1",
        "captured_at": previous.get("captured_at") or utc_now(),
        "updated_at": utc_now(),
        "host_os": runtime_platform.system() or sys.platform,
        "host_release": runtime_platform.release(),
        "host_architecture": runtime_platform.machine(),
        "python_version": runtime_platform.python_version(),
        "agent_adapter_id": adapter.adapter_id,
        "agent_display_name": adapter.display_name,
        "agent_adapter_source": adapter.source,
        "model_configuration_mode": str(adapter.model_config.get("mode") or "inherit"),
        "model_name": model_name or "unknown",
        "provider_host": provider_host or _text(previous.get("provider_host"), 240) or "unknown",
    }


def _agent_adapter_fingerprint(adapter: AgentCliAdapter) -> str:
    return sha256_json({
        "adapter_id": adapter.adapter_id,
        "source": adapter.source,
        "executable": adapter.executable,
        "args": list(adapter.args),
        "resume_args": list(adapter.resume_args),
        "session_strategy": adapter.session_strategy,
        "skill_roots": list(adapter.skill_roots),
        "skill_source_roots": list(adapter.skill_source_roots),
        "mcp_format": adapter.mcp_format,
        "mcp_path": adapter.mcp_path,
    })


def _synchronize_agent_skill_bootstrap(
    *,
    run_id: str,
    workspace: Path,
    adapter: AgentCliAdapter,
    agent_session_id: str,
    executable: Path | None,
) -> dict[str, Any]:
    """Project and persist the exact Skill contract owned by one CLI Session."""

    run = agent_run_store.read(run_id)
    requirement = build_skill_bootstrap_requirement(
        workspace=workspace,
        run=run,
        adapter_id=adapter.adapter_id,
        adapter_fingerprint=_agent_adapter_fingerprint(adapter),
        agent_session_id=agent_session_id,
        executable=executable,
    )
    state, changed = merge_skill_bootstrap_requirement(run.get("skill_bootstrap"), requirement)
    if changed:
        def apply(value: dict[str, Any]) -> None:
            merged, _ = merge_skill_bootstrap_requirement(value.get("skill_bootstrap"), requirement)
            value["skill_bootstrap"] = merged

        run = agent_run_store.mutate(
            run_id,
            event_type="skill.bootstrap_required",
            event_state=str(run.get("state") or "created"),
            payload={"skill_bootstrap": public_skill_bootstrap(state)},
            apply=apply,
        )
        state = _record(run.get("skill_bootstrap"))
    else:
        state = _record(run.get("skill_bootstrap"))

    platform_dir = workspace / ".agent-platform"
    platform_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(platform_dir / "skill-bootstrap.json", state)
    task_path = workspace / "task.json"
    if task_path.is_file():
        try:
            task = json.loads(task_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PlatformError(
                "AGENT_SKILL_BOOTSTRAP_TASK_INVALID",
                "Agent 工作区 task.json 无法写入 Skill Bootstrap 契约。",
                status_code=500,
                details={"run_id": run_id, "reason": str(exc)[:800]},
            ) from exc
        if isinstance(task, dict):
            task["skill_bootstrap"] = public_skill_bootstrap(state)
            atomic_write_json(task_path, task)
    return state


def _raise_agent_cli_error(exc: AgentCliTerminalError | AgentCliAdapterError) -> None:
    raise PlatformError(exc.code, exc.message, status_code=exc.status_code, details=exc.details) from exc


def _refresh_agent_cli_projected_workspace(terminal: Any) -> tuple[Path, AgentCliAdapter]:
    """Refresh neutral Run assets and the active Adapter's native projection."""

    if not isinstance(agent_runtime, KimiEngineAgentRuntime):
        raise PlatformError(
            "AGENT_CLI_RUNTIME_UNAVAILABLE",
            "当前后端运行时不能刷新 Agent 工作区。",
            status_code=409,
        )
    with _agent_cli_workspace_lock(terminal.run_id):
        workspace = agent_runtime.refresh_workspace(terminal.run_id)
        try:
            adapter = agent_cli_adapters.get(getattr(terminal, "adapter_id", "kimi"))
            adapter.materialize_workspace(workspace)
            _synchronize_agent_skill_bootstrap(
                run_id=terminal.run_id,
                workspace=workspace,
                adapter=adapter,
                agent_session_id=getattr(terminal, "agent_session_id", getattr(terminal, "kimi_session_id", "")),
                executable=adapter.resolve_executable(),
            )
        except AgentCliAdapterError as exc:
            _raise_agent_cli_error(exc)
        except OSError as exc:
            raise PlatformError(
                "AGENT_CLI_WORKSPACE_REFRESH_FAILED",
                "当前 Agent Adapter 的 Skill 或 MCP 工作区投影刷新失败。",
                status_code=500,
                details={"run_id": terminal.run_id, "reason": str(exc)[:1200]},
            ) from exc
    return workspace, adapter


def _activate_agent_cli_run(
    run_id: str,
    adapter: AgentCliAdapter,
    terminal_session_id: str = "starting",
    agent_session_id: str = "",
) -> dict[str, Any]:
    def apply(value: dict[str, Any]) -> None:
        value["cancel_requested"] = False
        value["state"] = "running"
        value["state_detail"] = f"真实 {adapter.display_name} 已通过后端 PTY 连接。"
        value["pending_ui_actions"] = []
        engine = _record(value.get("engine"))
        engine["terminal_transport"] = {
            "kind": "pty",
            "adapter_id": adapter.adapter_id,
            "agent_display_name": adapter.display_name,
            "agent_session_id": agent_session_id or None,
            "terminal_session_id": terminal_session_id,
            "state": "running",
            "updated_at": utc_now(),
        }
        value["engine"] = engine

    return agent_run_store.mutate(
        run_id,
        event_type="terminal.session_attached",
        event_state="running",
        payload={
            "terminal_session_id": terminal_session_id,
            "transport": "pty",
            "adapter_id": adapter.adapter_id,
            "agent_session_id": agent_session_id or None,
        },
        apply=apply,
    )


def _raise_kimi_cli_error(exc: AgentCliTerminalError | AgentCliAdapterError) -> None:
    _raise_agent_cli_error(exc)


def _run_page_session_id(run: dict[str, Any]) -> str:
    return str(_record(run.get("context")).get("page_session_id") or "")


def _require_matching_page_session(run: dict[str, Any], page_session_id: str | None) -> None:
    if not page_session_id:
        return
    current = _run_page_session_id(run)
    if current != page_session_id:
        raise PlatformError(
            "PAGE_AGENT_SESSION_MISMATCH",
            "该 Agent Run 不属于当前浏览器页面 Session。",
            status_code=409,
            details={"run_id": run.get("run_id"), "page_session_id": current or None},
        )


def _new_kimi_cli_backing_run(request: KimiCliSessionCreateRequest) -> dict[str, Any]:
    try:
        adapter = agent_cli_adapters.get(request.adapter_id or None)
    except AgentCliAdapterError as exc:
        _raise_agent_cli_error(exc)
    run_context = _normalized_agent_run_context(request.mode, request.context)
    page_session_id = request.page_session_id
    run_context = {
        **run_context,
        "page_session_id": page_session_id,
        "runtime_environment": _agent_runtime_environment(adapter, request.credential),
    }
    analysis_id = normalize_analysis_id(request.analysis_id) if request.analysis_id else ""
    if analysis_id:
        run_context = {**run_context, "analysis_record_id": analysis_id}
    selected_skill_profile = resolve_skill_profile(
        str(_record(run_context.get("skill_profile")).get("profile_id") or run_context.get("skill_profile_id") or "") or None
    )
    run_context = {**run_context, "skill_profile": _compact_skill_profile_ref(selected_skill_profile)}
    selected_versions = {str(item.get("skill_id")): str(item.get("version")) for item in skill_catalog()}
    for skill_id, item in _record(selected_skill_profile.get("skills")).items():
        if isinstance(item, dict):
            selected_versions[str(skill_id)] = str(item.get("version") or selected_versions.get(str(skill_id), ""))
    public = agent_runtime.create(
        project_id=PROJECT_ID,
        goal=request.goal,
        initial_input=f"真实 {adapter.display_name} 页面 Session 已创建，等待终端输入。",
        mode=request.mode,
        context=run_context,
        budget=request.budget.model_dump(),
        action_catalog_version=sha256_json(action_catalog()),
        skill_versions=[
            {"skill_id": skill_id, "version": version}
            for skill_id, version in sorted(selected_versions.items())
        ],
    )
    run_id = str(public["run_id"])
    agent_run_store.append_event(
        run_id,
        "page.session_bound",
        "created",
        {"page_session_id": page_session_id, "active_analysis_id": analysis_id or None},
    )
    return agent_run_store.read(run_id)


_new_agent_cli_backing_run = _new_kimi_cli_backing_run


def _agent_cli_resume_requested(run: dict[str, Any], adapter: AgentCliAdapter) -> bool:
    transport = _record(_record(run.get("engine")).get("terminal_transport"))
    return (
        adapter.session_strategy != "stateless"
        and transport.get("adapter_id") == adapter.adapter_id
        and bool(transport.get("terminal_session_id"))
    )


def _agent_cli_session_identity(run: dict[str, Any], adapter: AgentCliAdapter) -> tuple[str, str]:
    transport = _record(_record(run.get("engine")).get("terminal_transport"))
    if transport.get("adapter_id") == adapter.adapter_id and transport.get("agent_session_id"):
        session_id = str(transport["agent_session_id"])
    else:
        session_id = f"session_{secrets.token_hex(12)}"
    return session_id, str(run["run_id"])


def _create_agent_cli_terminal(
    *,
    run_id: str,
    adapter: AgentCliAdapter,
    launch: AgentCliLaunchSpec,
    agent_session_id: str,
    agent_workspace_id: str,
    cols: int,
    rows: int,
) -> AgentCliTerminalSession:
    if launch.usage_kind == "kimi_wire":
        kimi_home = Path(os.path.expanduser(launch.environment.get("KIMI_CODE_HOME", "~/.kimi-code")))
        usage_ledger = KimiCliUsageLedger(kimi_home / "sessions" / agent_workspace_id / agent_session_id)
    elif launch.usage_kind == "standard_jsonl":
        usage_ledger = JsonlCliUsageLedger(launch.workspace / ".agent-platform" / "token-usage.jsonl")
    else:
        usage_ledger = EmptyCliUsageLedger()
    return agent_cli_terminals.create(
        run_id=run_id,
        kimi_session_id=agent_session_id if adapter.adapter_id == "kimi" else "",
        kimi_workspace_id=agent_workspace_id if adapter.adapter_id == "kimi" else "",
        binary=Path(launch.command[0]),
        workspace=launch.workspace,
        environment=launch.environment,
        adapter_id=adapter.adapter_id,
        agent_display_name=adapter.display_name,
        agent_session_id=agent_session_id,
        agent_workspace_id=agent_workspace_id,
        workspace_contract_sha256=launch.workspace_contract_sha256,
        command=launch.command,
        submit_mode=launch.submit_mode,
        startup_input_delay_ms=launch.startup_input_delay_ms,
        submit_delay_ms=launch.submit_delay_ms,
        submit_fallback_ms=launch.submit_fallback_ms,
        usage_ledger=usage_ledger,
        cols=cols,
        rows=rows,
    )


@app.get("/v1/agent-cli/adapters")
def list_agent_cli_adapters() -> dict[str, Any]:
    try:
        return agent_cli_adapters.catalog()
    except AgentCliAdapterError as exc:
        _raise_agent_cli_error(exc)


@app.post("/v1/agent-cli/sessions")
@app.post("/v1/kimi-cli/sessions", include_in_schema=False)
def create_kimi_cli_session(request: AgentCliSessionCreateRequest) -> dict[str, Any]:
    """Create or reattach one adapter-selected Agent CLI for a browser page."""

    try:
        adapter = agent_cli_adapters.get(request.adapter_id or None)
    except AgentCliAdapterError as exc:
        _raise_agent_cli_error(exc)
    if not isinstance(agent_runtime, KimiEngineAgentRuntime):
        raise PlatformError(
            "AGENT_CLI_RUNTIME_UNAVAILABLE",
            "当前后端运行时不能生成 Agent CLI 独立工作区。",
            status_code=409,
        )
    if adapter.adapter_id == "kimi" and request.credential:
        _configure_kimi_browser_credential(request.credential)
    recovered_page_run = False
    if request.run_id:
        with _agent_cli_workspace_lock(request.run_id):
            run = agent_run_store.read(request.run_id)
            if request.adopt_page_session and _run_page_session_id(run) != request.page_session_id:
                requested_analysis_id = normalize_analysis_id(request.analysis_id) if request.analysis_id else ""
                bound_analysis_id = str(_record(run.get("context")).get("analysis_record_id") or "")
                if requested_analysis_id and bound_analysis_id and requested_analysis_id != bound_analysis_id:
                    raise PlatformError(
                        "ANALYSIS_SESSION_RUN_MISMATCH",
                        "目标 Agent Run 不属于要恢复的分析记录。",
                        status_code=409,
                        details={"run_id": request.run_id, "analysis_id": bound_analysis_id},
                    )

                def adopt_page(value: dict[str, Any]) -> None:
                    context = _record(value.get("context"))
                    context["page_session_id"] = request.page_session_id
                    if requested_analysis_id:
                        context["analysis_record_id"] = requested_analysis_id
                    value["context"] = context

                run = agent_run_store.mutate(
                    request.run_id,
                    event_type="page.session_adopted",
                    event_state=str(run.get("state") or "created"),
                    payload={
                        "page_session_id": request.page_session_id,
                        "analysis_id": requested_analysis_id or bound_analysis_id or None,
                        "reason": "explicit_analysis_restore",
                    },
                    apply=adopt_page,
                )
            else:
                _require_matching_page_session(run, request.page_session_id)
    else:
        run = agent_run_store.find_latest_by_page_session(
            request.page_session_id,
            project_id=PROJECT_ID,
            preferred_adapter_id=adapter.adapter_id,
        )
        if run is None:
            run = _new_agent_cli_backing_run(request)
        else:
            recovered_page_run = True
            agent_run_store.append_event(
                str(run["run_id"]),
                "page.session_recovered",
                str(run.get("state") or "created"),
                {
                    "page_session_id": request.page_session_id,
                    "adapter_id": adapter.adapter_id,
                    "recovery_reason": "client_run_binding_missing",
                },
            )
            run = agent_run_store.read(str(run["run_id"]))
    run_id = str(run["run_id"])
    active_prompt = _record(run.get("active_engine_prompt"))
    if active_prompt:
        raise PlatformError(
            "AGENT_CLI_SESSION_BUSY",
            "当前页面 Run 仍有旧式 Engine 请求在运行；请先停止该请求，再挂载 Agent CLI。",
            status_code=409,
            details={"run_id": run_id, "prompt_id": active_prompt.get("prompt_id")},
        )
    with _agent_cli_workspace_lock(run_id):
        requested_context = _normalized_agent_run_context(request.mode, request.context)
        try:
            if requested_context:
                requested_context = {**requested_context, "page_session_id": request.page_session_id}
                previous_environment = _record(_record(run.get("context")).get("runtime_environment"))
                requested_context["runtime_environment"] = _agent_runtime_environment(
                    adapter,
                    request.credential,
                    previous=previous_environment,
                )
                analysis_id = normalize_analysis_id(request.analysis_id) if request.analysis_id else ""
                if analysis_id:
                    requested_context = {**requested_context, "analysis_record_id": analysis_id}
                else:
                    requested_context.pop("analysis_record_id", None)
                agent_runtime.prepare_task(run_id, goal=request.goal, mode=request.mode, context=requested_context)
            if adapter.adapter_id == "kimi":
                binding = agent_runtime.ensure_session_binding(run_id)
                workspace = Path(binding.workspace_path)
                agent_session_id = binding.session_id
                agent_workspace_id = binding.workspace_id
            else:
                workspace = agent_runtime.refresh_workspace(run_id)
                agent_session_id, agent_workspace_id = _agent_cli_session_identity(run, adapter)
                binding = None
            adapter.materialize_workspace(workspace)
            resume = _agent_cli_resume_requested(run, adapter)
            launch = adapter.build_launch(
                workspace=workspace,
                run_id=run_id,
                agent_session_id=agent_session_id,
                agent_workspace_id=agent_workspace_id,
                resume=resume,
                credential=request.credential.model_dump() if request.credential else None,
            )
            _synchronize_agent_skill_bootstrap(
                run_id=run_id,
                workspace=workspace,
                adapter=adapter,
                agent_session_id=agent_session_id,
                executable=Path(launch.command[0]),
            )
        except PlatformError:
            raise
        except AgentCliAdapterError as exc:
            _raise_agent_cli_error(exc)
        except Exception as exc:
            logger.exception("Agent CLI Session preparation failed for run %s", run_id)
            raise PlatformError(
                "AGENT_CLI_SESSION_PREPARE_FAILED",
                f"{adapter.display_name} 工作区或 Session 准备失败。",
                status_code=500,
                details={"run_id": run_id, "error_type": type(exc).__name__, "reason": str(exc)[:1200]},
            ) from exc
    existing = agent_cli_terminals.for_run(run_id)
    if (
        existing is not None
        and existing.public()["state"] == "running"
        and existing.adapter_id == adapter.adapter_id
        and existing.agent_session_id == agent_session_id
        and existing.workspace_contract_sha256 == launch.workspace_contract_sha256
    ):
        existing.resize(request.cols, request.rows)
        _activate_agent_cli_run(run_id, adapter, existing.terminal_session_id, agent_session_id)
        return {
            "terminal": existing.public(),
            "run": agent_run_store.public(agent_run_store.read(run_id)),
            "recovery": {
                "status": "durable_run_recovered",
                "reason": "client_run_binding_missing",
            } if recovered_page_run else None,
        }
    if existing is not None and existing.public()["state"] == "running":
        if existing.workspace_contract_sha256 != launch.workspace_contract_sha256:
            agent_run_store.append_event(
                run_id,
                "terminal.workspace_contract_changed",
                "running",
                {
                    "terminal_session_id": existing.terminal_session_id,
                    "adapter_id": adapter.adapter_id,
                    "previous_contract_sha256": existing.workspace_contract_sha256 or None,
                    "next_contract_sha256": launch.workspace_contract_sha256,
                    "reason": "skill_or_mcp_projection_changed",
                },
            )
        agent_cli_terminals.stop(existing.terminal_session_id)
    _activate_agent_cli_run(run_id, adapter, agent_session_id=agent_session_id)
    try:
        terminal = _create_agent_cli_terminal(
            run_id=run_id,
            adapter=adapter,
            launch=launch,
            agent_session_id=agent_session_id,
            agent_workspace_id=agent_workspace_id,
            cols=request.cols,
            rows=request.rows,
        )
    except AgentCliTerminalError as exc:
        _record_agent_cli_exit(run_id, "start_failed", None)
        _raise_agent_cli_error(exc)
    _activate_agent_cli_run(run_id, adapter, terminal.terminal_session_id, agent_session_id)
    recovery: dict[str, Any] | None = {
        "status": "durable_run_recovered",
        "reason": "client_run_binding_missing",
    } if recovered_page_run else None
    if resume and terminal.wait_for_exit(AGENT_CLI_RESUME_STARTUP_PROBE_SECONDS):
        failed_terminal = terminal.public()
        previous_agent_session_id = agent_session_id
        try:
            if adapter.adapter_id == "kimi":
                binding = agent_runtime.replace_session_binding(
                    run_id,
                    reason="agent_cli_resume_startup_exit",
                )
                workspace = Path(binding.workspace_path)
                agent_session_id = binding.session_id
                agent_workspace_id = binding.workspace_id
            else:
                agent_session_id = f"session_{secrets.token_hex(12)}"
            fresh_launch = adapter.build_launch(
                workspace=workspace,
                run_id=run_id,
                agent_session_id=agent_session_id,
                agent_workspace_id=agent_workspace_id,
                resume=False,
                credential=request.credential.model_dump() if request.credential else None,
            )
            _synchronize_agent_skill_bootstrap(
                run_id=run_id,
                workspace=workspace,
                adapter=adapter,
                agent_session_id=agent_session_id,
                executable=Path(fresh_launch.command[0]),
            )
            agent_run_store.append_event(
                run_id,
                "terminal.session_resume_fallback",
                "running",
                {
                    "adapter_id": adapter.adapter_id,
                    "failed_terminal_session_id": failed_terminal.get("terminal_session_id"),
                    "failed_agent_session_id": previous_agent_session_id,
                    "failed_exit_code": failed_terminal.get("exit_code"),
                    "failed_log_path": failed_terminal.get("log_path"),
                    "replacement_agent_session_id": agent_session_id,
                    "probe_seconds": AGENT_CLI_RESUME_STARTUP_PROBE_SECONDS,
                },
            )
            _activate_agent_cli_run(run_id, adapter, agent_session_id=agent_session_id)
            terminal = _create_agent_cli_terminal(
                run_id=run_id,
                adapter=adapter,
                launch=fresh_launch,
                agent_session_id=agent_session_id,
                agent_workspace_id=agent_workspace_id,
                cols=request.cols,
                rows=request.rows,
            )
        except AgentCliTerminalError as exc:
            _record_agent_cli_exit(run_id, "start_failed", None)
            _raise_agent_cli_error(exc)
        except AgentCliAdapterError as exc:
            _record_agent_cli_exit(run_id, "start_failed", None)
            _raise_agent_cli_error(exc)
        except Exception as exc:
            _record_agent_cli_exit(run_id, "start_failed", None)
            logger.exception("Agent CLI fresh-session fallback failed for run %s", run_id)
            raise PlatformError(
                "AGENT_CLI_FRESH_SESSION_FAILED",
                f"{adapter.display_name} 历史会话无法恢复，且自动新建会话失败。",
                status_code=500,
                details={"run_id": run_id, "error_type": type(exc).__name__, "reason": str(exc)[:1200]},
            ) from exc
        _activate_agent_cli_run(run_id, adapter, terminal.terminal_session_id, agent_session_id)
        recovery = {
            "status": "fresh_session_started",
            "reason": "resume_startup_exit",
            "failed_terminal_session_id": failed_terminal.get("terminal_session_id"),
        }
    return {
        "terminal": terminal.public(),
        "run": agent_run_store.public(agent_run_store.read(run_id)),
        "recovery": recovery,
    }


@app.get("/v1/agent-cli/sessions/{terminal_session_id}")
@app.get("/v1/kimi-cli/sessions/{terminal_session_id}", include_in_schema=False)
def get_kimi_cli_session(terminal_session_id: str, page_session_id: str) -> dict[str, Any]:
    try:
        terminal = agent_cli_terminals.get(terminal_session_id)
    except AgentCliTerminalError as exc:
        _raise_agent_cli_error(exc)
    run = agent_run_store.read(terminal.run_id)
    _require_matching_page_session(run, page_session_id)
    return {"terminal": terminal.public(), "run": agent_run_store.public(run)}


@app.get("/v1/agent-cli/sessions/{terminal_session_id}/usage")
@app.get("/v1/kimi-cli/sessions/{terminal_session_id}/usage", include_in_schema=False)
def get_kimi_cli_session_usage(terminal_session_id: str, page_session_id: str) -> dict[str, Any]:
    """Return the small, durable token projection without serializing the Run."""

    try:
        terminal = agent_cli_terminals.get(terminal_session_id)
    except AgentCliTerminalError as exc:
        _raise_agent_cli_error(exc)
    run = agent_run_store.read(terminal.run_id)
    _require_matching_page_session(run, page_session_id)
    return {"token_usage": terminal.usage_ledger.public()}


@app.post("/v1/agent-cli/sessions/{terminal_session_id}/input")
@app.post("/v1/kimi-cli/sessions/{terminal_session_id}/input", include_in_schema=False)
def write_kimi_cli_input(terminal_session_id: str, request: KimiCliInputRequest) -> dict[str, Any]:
    try:
        data = base64.b64decode(request.data_base64, validate=True)
    except (binascii.Error, ValueError, TypeError) as exc:
        raise PlatformError("KIMI_CLI_INPUT_INVALID", "终端输入不是有效的 Base64 数据。", status_code=422) from exc
    try:
        terminal = agent_cli_terminals.get(terminal_session_id)
        if (b"\r" in data or b"\n" in data) and isinstance(agent_runtime, KimiEngineAgentRuntime):
            _refresh_agent_cli_projected_workspace(terminal)
        terminal.write(data)
    except AgentCliTerminalError as exc:
        _raise_agent_cli_error(exc)
    return {"accepted": True, "byte_count": len(data), "terminal": terminal.public()}


@app.post("/v1/agent-cli/sessions/{terminal_session_id}/commands")
@app.post("/v1/kimi-cli/sessions/{terminal_session_id}/commands", include_in_schema=False)
def send_kimi_cli_command(terminal_session_id: str, request: KimiCliCommandRequest) -> dict[str, Any]:
    try:
        terminal = agent_cli_terminals.get(terminal_session_id)
        if isinstance(agent_runtime, KimiEngineAgentRuntime):
            _refresh_agent_cli_projected_workspace(terminal)
        accepted = terminal.send_command(request.command, command_id=request.command_id)
    except AgentCliTerminalError as exc:
        _raise_agent_cli_error(exc)
    terminal_state = terminal.public()
    submission = terminal_state.get("last_submission")
    # A subsequent concurrent command may already own the live projection.
    # Only persist a transport receipt that identifies this exact command.
    submission_fields = {"submission_transport": submission} if (
        isinstance(submission, dict) and request.command_id.strip()
        and submission.get("command_id") == request.command_id.strip()[:200]
    ) else {}
    agent_run_store.append_event(
        terminal.run_id,
        "terminal.command_submitted" if accepted else "terminal.command_reused",
        "running",
        {
            "terminal_session_id": terminal_session_id,
            "command_id": request.command_id,
            "command_sha256": sha256_json({"command": request.command}),
            "character_count": len(request.command),
            **submission_fields,
        },
    )
    return {"accepted": accepted, "terminal": terminal_state}


@app.post("/v1/agent-cli/sessions/{terminal_session_id}/context")
@app.post("/v1/kimi-cli/sessions/{terminal_session_id}/context", include_in_schema=False)
def update_kimi_cli_context(terminal_session_id: str, request: KimiCliContextRequest) -> dict[str, Any]:
    if not isinstance(agent_runtime, KimiEngineAgentRuntime):
        raise PlatformError("AGENT_CLI_RUNTIME_UNAVAILABLE", "当前后端运行时不能刷新 Agent 工作区。", status_code=409)
    try:
        terminal = agent_cli_terminals.get(terminal_session_id)
    except AgentCliTerminalError as exc:
        _raise_agent_cli_error(exc)
    run = agent_run_store.read(terminal.run_id)
    context = _normalized_agent_run_context(request.mode, request.context)
    _require_matching_page_session(run, request.page_session_id)
    context = {**context, "page_session_id": request.page_session_id}
    analysis_id = normalize_analysis_id(request.analysis_id) if request.analysis_id else ""
    if analysis_id:
        context["analysis_record_id"] = analysis_id
    else:
        context.pop("analysis_record_id", None)
    agent_runtime.prepare_task(terminal.run_id, goal=request.goal, mode=request.mode, context=context)
    workspace, adapter = _refresh_agent_cli_projected_workspace(terminal)
    _activate_agent_cli_run(
        terminal.run_id,
        adapter,
        terminal_session_id,
        getattr(terminal, "agent_session_id", getattr(terminal, "kimi_session_id", "")),
    )
    return {
        "terminal": terminal.public(),
        "run": agent_run_store.public(agent_run_store.read(terminal.run_id)),
        "task": {
            "workspace": str(workspace),
            "context_sha256": sha256_json({"context": context}),
            "analysis_id": analysis_id or None,
            "page_session_id": request.page_session_id,
        },
    }


@app.post("/v1/agent-cli/sessions/{terminal_session_id}/resize")
@app.post("/v1/kimi-cli/sessions/{terminal_session_id}/resize", include_in_schema=False)
def resize_kimi_cli_session(terminal_session_id: str, request: KimiCliResizeRequest) -> dict[str, Any]:
    try:
        terminal = agent_cli_terminals.get(terminal_session_id)
        terminal.resize(request.cols, request.rows)
    except AgentCliTerminalError as exc:
        _raise_agent_cli_error(exc)
    return {"terminal": terminal.public()}


@app.delete("/v1/agent-cli/sessions/{terminal_session_id}")
@app.delete("/v1/kimi-cli/sessions/{terminal_session_id}", include_in_schema=False)
def stop_kimi_cli_session(terminal_session_id: str) -> dict[str, Any]:
    try:
        terminal = agent_cli_terminals.get(terminal_session_id)
        terminal.stop()
    except AgentCliTerminalError as exc:
        _raise_agent_cli_error(exc)
    return {"terminal": terminal.public()}


@app.get("/v1/agent-cli/sessions/{terminal_session_id}/output")
@app.get("/v1/kimi-cli/sessions/{terminal_session_id}/output", include_in_schema=False)
async def poll_kimi_cli_output(terminal_session_id: str, after: int = 0, wait_ms: int = 10_000) -> dict[str, Any]:
    """Return ordered PTY output through a reconnect-friendly JSON channel."""

    if after < 0:
        raise PlatformError("KIMI_CLI_EVENT_CURSOR_INVALID", "终端事件游标必须为非负整数。", status_code=422)
    if wait_ms < 0 or wait_ms > 15_000:
        raise PlatformError("KIMI_CLI_OUTPUT_WAIT_INVALID", "终端长轮询等待时间必须在 0 到 15000 毫秒之间。", status_code=422)
    try:
        terminal = agent_cli_terminals.get(terminal_session_id)
    except AgentCliTerminalError as exc:
        _raise_agent_cli_error(exc)

    wait_seconds = min(wait_ms / 1000.0, ASYNC_POLL_MAX_HOLD_SECONDS)
    chunks, truncated = await terminal.events_async(after, wait_seconds=wait_seconds)
    # Bound one JSON response while preserving chunk boundaries. If a browser
    # has fallen far behind it will immediately request the remaining pages.
    selected = []
    selected_bytes = 0
    for chunk in chunks:
        if selected and selected_bytes + len(chunk.data) > 512 * 1024:
            break
        selected.append(chunk)
        selected_bytes += len(chunk.data)
    state = terminal.public()
    return {
        "schema_version": "kimi_cli_output_poll/v1",
        "chunks": [chunk.public() for chunk in selected],
        "truncated": truncated,
        "more_available": len(selected) < len(chunks),
        "terminal": state,
    }


@app.get("/v1/agent-cli/sessions/{terminal_session_id}/events")
@app.get("/v1/kimi-cli/sessions/{terminal_session_id}/events", include_in_schema=False)
async def stream_kimi_cli_events(terminal_session_id: str, request: Request) -> StreamingResponse:
    try:
        terminal = agent_cli_terminals.get(terminal_session_id)
    except AgentCliTerminalError as exc:
        _raise_agent_cli_error(exc)
    raw_last = request.headers.get("Last-Event-ID") or request.query_params.get("after") or "0"
    try:
        last_sequence = max(0, int(raw_last))
    except ValueError as exc:
        raise PlatformError("KIMI_CLI_EVENT_CURSOR_INVALID", "终端事件游标必须为非负整数。", status_code=422) from exc

    async def events() -> AsyncIterator[bytes]:
        sequence = last_sequence
        terminal_state_sent = False
        stream_deadline = time.monotonic() + COMPATIBILITY_SSE_MAX_SECONDS
        while time.monotonic() < stream_deadline:
            if await request.is_disconnected():
                break
            remaining = max(0.0, stream_deadline - time.monotonic())
            chunks, truncated = await terminal.events_async(sequence, wait_seconds=min(10.0, remaining))
            if truncated:
                reset_payload = json.dumps({"reset": True, "earliest_sequence": terminal.public()["earliest_sequence"]}, separators=(",", ":"))
                yield f"event: reset\ndata: {reset_payload}\n\n".encode("utf-8")
            if chunks:
                for chunk in chunks:
                    sequence = chunk.sequence
                    payload = json.dumps(chunk.public(), separators=(",", ":"))
                    yield f"id: {sequence}\ndata: {payload}\n\n".encode("utf-8")
                continue
            state = terminal.public()
            if state["state"] == "exited":
                if not terminal_state_sent:
                    payload = json.dumps({"state": "exited", "exit_code": state.get("exit_code")}, separators=(",", ":"))
                    yield f"event: terminal_state\ndata: {payload}\n\n".encode("utf-8")
                    terminal_state_sent = True
                break
            yield b": keepalive\n\n"

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/v1/agent-runs")
def create_agent_run(request: AgentRunCreateRequest, http_request: Request) -> dict[str, Any]:
    """Create a durable Run and start it through the selected AgentEngine.

    ``credential`` remains a legacy-runtime compatibility field. The default
    Kimi engine owns model authentication and never persists browser secrets in
    the platform Run.
    """

    key = _require_idempotency_key(http_request)
    run_context = _normalized_agent_run_context(request.mode, request.context)
    requested_analysis_id = request.analysis_id or str(run_context.get("analysis_record_id") or "") or None
    analysis_id = normalize_analysis_id(requested_analysis_id) if requested_analysis_id else ""
    if analysis_id:
        run_context = {**run_context, "analysis_record_id": analysis_id}
    selected_skill_profile = resolve_skill_profile(
        str(_record(run_context.get("skill_profile")).get("profile_id") or run_context.get("skill_profile_id") or "") or None
    )
    run_context = {
        **run_context,
        "skill_profile": _compact_skill_profile_ref(selected_skill_profile),
    }
    identity = {
        "goal": request.goal,
        "initial_input": request.initial_input,
        "mode": request.mode,
        "context": run_context,
        "analysis_id": analysis_id or None,
        "budget": request.budget.model_dump(),
        "credential": {
            "base_url": request.credential.base_url,
            "model": request.credential.model,
            "ttl_seconds": request.credential.ttl_seconds,
        } if request.credential else None,
    }
    existing = _idempotency_lookup("create_agent_run", key, identity)
    if existing is not None:
        return existing
    if request.credential and isinstance(agent_runtime, KimiEngineAgentRuntime):
        _configure_kimi_browser_credential(request.credential)
    def create_new_run() -> dict[str, Any]:
        selected_versions = {
            str(item.get("skill_id")): str(item.get("version"))
            for item in skill_catalog()
        }
        for skill_id, item in _record(selected_skill_profile.get("skills")).items():
            if isinstance(item, dict):
                selected_versions[str(skill_id)] = str(item.get("version") or selected_versions.get(str(skill_id), ""))
        return agent_runtime.create(
            project_id=PROJECT_ID,
            goal=request.goal,
            initial_input=request.initial_input,
            mode=request.mode,
            context=run_context,
            budget=request.budget.model_dump(),
            action_catalog_version=sha256_json(action_catalog()),
            skill_versions=[
                {"skill_id": skill_id, "version": version}
                for skill_id, version in sorted(selected_versions.items())
            ],
        )

    if analysis_id and isinstance(agent_runtime, KimiEngineAgentRuntime):
        with analysis_agent_session_lock:
            bound = _bound_analysis_run(analysis_id)
            if bound is not None:
                bound_run_id = str(bound["run_id"])
                agent_runtime.prepare_task(
                    bound_run_id,
                    goal=request.goal,
                    mode=request.mode,
                    context=run_context,
                )
                response = {"run": agent_runtime.submit_message(bound_run_id, request.initial_input)}
                _idempotency_store("create_agent_run", key, identity, response)
                return response
            run = create_new_run()
            run_id = str(run["run_id"])
            analysis_agent_sessions.bind(analysis_id, run_id)
            agent_run_store.append_event(
                run_id,
                "analysis.session_bound",
                str(run.get("state") or "created"),
                {"analysis_id": analysis_id, "scope": "hardware_routing+opportunity_reasoning+device_validation+page_assistant"},
            )
    else:
        run = create_new_run()
        run_id = str(run["run_id"])
    if request.credential and AGENT_ENGINE_KIND == "legacy":
        _model_relay_base_url(request.credential.base_url)
        agent_runtime.attach_credential(
            run_id,
            base_url=request.credential.base_url,
            model=request.credential.model,
            api_key=request.credential.api_key,
            ttl_seconds=request.credential.ttl_seconds,
        )
    if (request.credential and AGENT_ENGINE_KIND == "legacy") or isinstance(agent_runtime, KimiEngineAgentRuntime):
        run = agent_runtime.start(run_id)
    response = {"run": run}
    _idempotency_store("create_agent_run", key, identity, response)
    return response


@app.get("/v1/agent-runs/{run_id}")
def get_agent_run(run_id: str) -> dict[str, Any]:
    return {"run": agent_run_store.public(agent_run_store.read(run_id))}


@app.get("/v1/analysis-agent-sessions/{analysis_id}")
def get_analysis_agent_session(analysis_id: str) -> dict[str, Any]:
    normalized = normalize_analysis_id(analysis_id)
    run = _bound_analysis_run(normalized)
    if run is None:
        raise PlatformError("ANALYSIS_SESSION_NOT_FOUND", "该分析记录尚未创建 Agent Session。", status_code=404)
    return {
        "analysis_id": normalized,
        "run": agent_run_store.public(run),
        "session_scope": "analysis",
    }


@app.post("/v1/agent-runs/{run_id}/analysis-binding")
def bind_agent_run_to_analysis(run_id: str, request: AgentRunAnalysisBindingRequest, http_request: Request) -> dict[str, Any]:
    """Attach an initially unscoped page-assistant Run to its new analysis.

    A pasted-code conversation can create the analysis record only after the
    model has classified the browser-held input.  Late binding keeps that same
    Kimi session for routing, opportunity reasoning and device validation.
    """

    key = _require_idempotency_key(http_request)
    analysis_id = normalize_analysis_id(request.analysis_id)
    identity = {"run_id": run_id, "analysis_id": analysis_id}
    existing = _idempotency_lookup("agent_run_analysis_binding", key, identity)
    if existing is not None:
        return existing
    run = agent_run_store.read(run_id)
    context = _record(run.get("context"))
    current_analysis_id = str(context.get("analysis_record_id") or "")
    if current_analysis_id and current_analysis_id != analysis_id:
        raise PlatformError(
            "AGENT_RUN_ANALYSIS_MISMATCH",
            "当前 Agent Run 已绑定到另一条优化分析记录。",
            status_code=409,
            details={"run_id": run_id, "analysis_id": current_analysis_id},
        )
    with analysis_agent_session_lock:
        bound_run = _bound_analysis_run(analysis_id)
        if bound_run is not None and str(bound_run.get("run_id") or "") != run_id:
            raise PlatformError(
                "ANALYSIS_SESSION_ALREADY_BOUND",
                "该优化分析记录已经绑定到另一个 Agent Run。",
                status_code=409,
                details={"analysis_id": analysis_id, "run_id": bound_run.get("run_id")},
            )
        prior_analysis_id = analysis_agent_sessions.analysis_id_for_run(run_id)
        if prior_analysis_id and prior_analysis_id != analysis_id:
            raise PlatformError(
                "AGENT_RUN_ALREADY_BOUND",
                "当前 Agent Run 已属于另一条优化分析记录。",
                status_code=409,
                details={"run_id": run_id, "analysis_id": prior_analysis_id},
            )
        analysis_agent_sessions.bind(analysis_id, run_id)

        def apply(value: dict[str, Any]) -> None:
            next_context = _record(value.get("context"))
            next_context["analysis_record_id"] = analysis_id
            value["context"] = next_context

        bound = agent_run_store.mutate(
            run_id,
            event_type="analysis.session_bound",
            event_state=str(run.get("state") or "created"),
            payload={"analysis_id": analysis_id, "scope": "hardware_routing+opportunity_reasoning+device_validation+page_assistant", "binding_mode": "late"},
            apply=apply,
        )
    response = {"run": agent_run_store.public(bound)}
    _idempotency_store("agent_run_analysis_binding", key, identity, response)
    return response


@app.post("/v1/agent-runs/{run_id}/messages")
def submit_agent_run_message(run_id: str, request: AgentRunMessageRequest, http_request: Request) -> dict[str, Any]:
    key = _require_idempotency_key(http_request)
    prepared_context = _normalized_agent_run_context(request.mode, request.context) if request.mode and request.context is not None else None
    identity = {
        "run_id": run_id,
        "message": request.message,
        "mode": request.mode,
        "goal": request.goal,
        "context": prepared_context,
        "credential": {"base_url": request.credential.base_url, "model": request.credential.model} if request.credential else None,
    }
    existing = _idempotency_lookup("agent_run_message", key, identity)
    if existing is not None:
        return existing
    if request.credential and isinstance(agent_runtime, KimiEngineAgentRuntime):
        _configure_kimi_browser_credential(request.credential)
    if request.credential and AGENT_ENGINE_KIND == "legacy":
        _model_relay_base_url(request.credential.base_url)
        agent_runtime.attach_credential(run_id, base_url=request.credential.base_url, model=request.credential.model, api_key=request.credential.api_key, ttl_seconds=request.credential.ttl_seconds)
    if request.mode and request.goal and prepared_context is not None:
        agent_runtime.prepare_task(run_id, goal=request.goal, mode=request.mode, context=prepared_context)
    response = {"run": agent_runtime.submit_message(run_id, request.message)}
    _idempotency_store("agent_run_message", key, identity, response)
    return response


@app.post("/v1/agent-runs/{run_id}/resume")
def resume_agent_run(run_id: str, request: AgentRunResumeRequest, http_request: Request) -> dict[str, Any]:
    key = _require_idempotency_key(http_request)
    identity = {
        "run_id": run_id,
        "additional_turns": request.additional_turns,
        "max_actions_per_turn": request.max_actions_per_turn,
        "max_output_tokens": request.max_output_tokens,
        "context_window_tokens": request.context_window_tokens,
        "credential": {"base_url": request.credential.base_url, "model": request.credential.model} if request.credential else None,
    }
    existing = _idempotency_lookup("agent_run_resume", key, identity)
    if existing is not None:
        return existing
    if request.credential and isinstance(agent_runtime, KimiEngineAgentRuntime):
        _configure_kimi_browser_credential(request.credential)
    if request.credential and AGENT_ENGINE_KIND == "legacy":
        _model_relay_base_url(request.credential.base_url)
        agent_runtime.attach_credential(run_id, base_url=request.credential.base_url, model=request.credential.model, api_key=request.credential.api_key, ttl_seconds=request.credential.ttl_seconds)
    budget_overrides = {
        key: value
        for key, value in {
            "max_actions_per_turn": request.max_actions_per_turn,
            "max_output_tokens": request.max_output_tokens,
            "context_window_tokens": request.context_window_tokens,
        }.items()
        if value is not None
    }
    response = {"run": agent_runtime.resume(run_id, extra_turns=request.additional_turns, budget_overrides=budget_overrides)}
    _idempotency_store("agent_run_resume", key, identity, response)
    return response


@app.post("/v1/agent-runs/{run_id}/cancel")
def cancel_agent_run(run_id: str, http_request: Request) -> dict[str, Any]:
    key = _require_idempotency_key(http_request)
    identity = {"run_id": run_id}
    existing = _idempotency_lookup("agent_run_cancel", key, identity)
    if existing is not None:
        return existing
    response = {"run": agent_runtime.cancel(run_id)}
    _idempotency_store("agent_run_cancel", key, identity, response)
    return response


@app.post("/v1/agent-runs/{run_id}/ui-action-results")
def submit_agent_ui_action_result(run_id: str, request: AgentRunUiActionResultRequest, http_request: Request) -> dict[str, Any]:
    key = _require_idempotency_key(http_request)
    identity = {
        "run_id": run_id,
        "request_id": request.request_id,
        "result": request.result,
        "error": request.error,
        "credential": {"base_url": request.credential.base_url, "model": request.credential.model} if request.credential else None,
    }
    existing = _idempotency_lookup("agent_ui_action_result", key, identity)
    if existing is not None:
        return existing
    # A UI result unblocks an already-running prompt and must never be gated
    # by model reconfiguration. The browser config may have changed while the
    # prompt was waiting; attempting to apply it here returns 409 and strands
    # the real action result. Kimi credentials are configured by create,
    # message, resume, or the dedicated credential endpoint instead.
    if request.credential and AGENT_ENGINE_KIND == "legacy":
        _model_relay_base_url(request.credential.base_url)
        agent_runtime.attach_credential(
            run_id,
            base_url=request.credential.base_url,
            model=request.credential.model,
            api_key=request.credential.api_key,
            ttl_seconds=request.credential.ttl_seconds,
        )
    response = {"run": agent_runtime.submit_ui_result(run_id, request.request_id, request.result, error=request.error)}
    _idempotency_store("agent_ui_action_result", key, identity, response)
    return response


@app.get("/v1/agent-runs/{run_id}/events")
async def stream_agent_run_events(run_id: str, request: Request) -> StreamingResponse:
    """Server-sent event stream with Last-Event-ID reconnect semantics."""

    raw_last = request.headers.get("Last-Event-ID") or request.query_params.get("after") or "0"
    try:
        last_sequence = max(0, int(raw_last))
    except ValueError:
        raise PlatformError("AGENT_EVENT_CURSOR_INVALID", "Last-Event-ID 必须为非负整数。", status_code=422)
    # Validate eagerly so an invalid run produces a normal API error instead of
    # a 200 response with an empty endless stream.
    agent_run_store.read(run_id)

    async def events() -> AsyncIterator[bytes]:
        sequence = last_sequence
        stream_deadline = time.monotonic() + COMPATIBILITY_SSE_MAX_SECONDS
        while time.monotonic() < stream_deadline:
            if await request.is_disconnected():
                break
            rows = agent_run_store.events(run_id, after_sequence=sequence, limit=500)
            if rows:
                for row in rows:
                    sequence = int(row["sequence"])
                    event_type = re.sub(r"[^A-Za-z0-9_.-]", "_", str(row.get("event_type") or "message"))
                    data = json.dumps(row, ensure_ascii=False, separators=(",", ":"))
                    yield f"id: {sequence}\nevent: {event_type}\ndata: {data}\n\n".encode("utf-8")
                continue
            yield b": keepalive\n\n"
            remaining = max(0.0, stream_deadline - time.monotonic())
            await agent_run_store.wait_for_events_async(run_id, sequence, min(10.0, remaining))

    return StreamingResponse(events(), media_type="text/event-stream", headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.get("/v1/agent-runs/{run_id}/events/poll")
async def poll_agent_run_events(run_id: str, after: int = 0, wait_ms: int = 10_000, limit: int = 500) -> dict[str, Any]:
    """Reconnectable Agent events without a reverse-proxied streaming body."""

    if after < 0:
        raise PlatformError("AGENT_EVENT_CURSOR_INVALID", "Agent 事件游标必须为非负整数。", status_code=422)
    if wait_ms < 0 or wait_ms > 15_000:
        raise PlatformError("AGENT_EVENT_WAIT_INVALID", "Agent 事件等待时间必须在 0 到 15000 毫秒之间。", status_code=422)
    bounded_limit = max(1, min(int(limit), 1_000))
    agent_run_store.read(run_id)
    rows = agent_run_store.events(run_id, after_sequence=after, limit=bounded_limit)
    if not rows and wait_ms:
        wait_seconds = min(wait_ms / 1000.0, ASYNC_POLL_MAX_HOLD_SECONDS)
        await agent_run_store.wait_for_events_async(run_id, after, wait_seconds)
        rows = agent_run_store.events(run_id, after_sequence=after, limit=bounded_limit)
    latest_sequence = int(rows[-1]["sequence"]) if rows else after
    return {
        "schema_version": "agent_run_event_poll/v1",
        "events": rows,
        "after": after,
        "latest_sequence": latest_sequence,
        "more_available": len(rows) >= bounded_limit,
    }


@app.get("/v1/agent-runs/{run_id}/execution-trace")
def download_agent_execution_trace(run_id: str) -> FileResponse:
    """Export the redacted append-only trace required for failure diagnosis."""

    path = agent_run_store.execution_trace_path(run_id)
    return FileResponse(path, media_type="application/x-ndjson", filename=f"{run_id}-analysis_execution_trace.jsonl")


@app.get("/v1/agent-engine/health")
def agent_engine_health() -> dict[str, Any]:
    """Return the selected engine and its non-secret connectivity state."""

    if isinstance(agent_runtime, KimiEngineAgentRuntime):
        return {"selected_engine": "kimi_server", "engine": kimi_agent_engine.health()}
    return {"selected_engine": "legacy", "engine": {"status": "ready", "engine_id": "legacy-platform-react"}}


@app.post("/v1/agent-engine/model-credential")
def configure_agent_engine_model(request: AgentRunCredentialRequest) -> dict[str, Any]:
    """Apply a browser-held model credential without persisting its secret."""

    if not isinstance(agent_runtime, KimiEngineAgentRuntime):
        return {
            "selected_engine": "legacy",
            "engine": {"status": "ready", "engine_id": "legacy-platform-react", "credential_mode": "browser_request"},
        }
    return {"selected_engine": "kimi_server", "engine": _configure_kimi_browser_credential(request)}


@app.delete("/v1/agent-engine/model-credential")
def clear_agent_engine_model() -> dict[str, Any]:
    """Revoke the process-local browser credential without touching Kimi config."""

    if not isinstance(agent_runtime, KimiEngineAgentRuntime):
        return {"selected_engine": "legacy", "engine": {"status": "ready", "credential_mode": "browser_request"}}
    try:
        result = kimi_agent_engine.clear_ephemeral_model()
    except KimiServerError as exc:
        raise PlatformError(exc.code, exc.message, status_code=409, details=exc.details) from exc
    return {"selected_engine": "kimi_server", "engine": result}


def _require_internal_agent_token(run_id: str, authorization: str | None) -> None:
    prefix = "Bearer "
    token = authorization[len(prefix):].strip() if isinstance(authorization, str) and authorization.startswith(prefix) else ""
    if not token or not kimi_internal_tokens.verify(run_id, token):
        raise PlatformError("AGENT_INTERNAL_AUTH_FAILED", "Agent Engine 工具凭证无效。", status_code=401)
    # Validate the Run independently from token derivation so a well-formed
    # token cannot create state for a deleted or unknown Run.
    agent_run_store.read(run_id)


@app.post("/v1/internal/agent-runs/{run_id}/actions/{action_id}")
def execute_internal_agent_action(
    run_id: str,
    action_id: str,
    request: InternalAgentActionRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    """Authenticated MCP-to-platform bridge for one typed action."""

    _require_internal_agent_token(run_id, authorization)
    run = agent_run_store.read(run_id)
    if _record(run.get("active_prompt_policy")).get("kind") == "structured_read_only":
        raise PlatformError(
            "AGENT_TOOL_DISABLED_FOR_STRUCTURED_STAGE",
            "当前硬件路由或机会推理阶段为只读结构化推理，不能调用平台工具。",
            status_code=409,
        )
    return agent_runtime.execute_external_action(run_id, action_id, request.input)


@app.get("/v1/internal/agent-runs/{run_id}/context")
def read_internal_agent_context(run_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _require_internal_agent_token(run_id, authorization)
    run = agent_run_store.read(run_id)
    public = agent_run_store.public(run)
    return {
        "schema_version": "agent_engine_task_context/v1",
        "run": {
            "run_id": run_id,
            "goal": run.get("goal"),
            "mode": run.get("mode"),
            "context": run.get("context"),
            "state": public.get("state"),
            "state_detail": public.get("state_detail"),
            "checkpoint": public.get("checkpoint"),
            "skill_bootstrap": public.get("skill_bootstrap"),
        },
    }


@app.get("/v1/internal/agent-runs/{run_id}/page-actions")
def list_internal_agent_page_actions(run_id: str, authorization: str | None = Header(default=None)) -> dict[str, Any]:
    _require_internal_agent_token(run_id, authorization)
    run = agent_run_store.read(run_id)
    device_validation = str(run.get("mode") or "") == "device_validation"
    allowed = []
    for item in action_catalog():
        action_id = str(item.get("action_id") or "")
        if action_id in RUNTIME_ACTIONS or action_id in LEGACY_BROWSER_DEVICE_ACTIONS:
            continue
        if device_validation and action_id != "workspace.read_file":
            continue
        allowed.append({
            "action_id": action_id,
            "risk_level": item.get("risk_level"),
            "input_schema": item.get("input_schema"),
            "output_schema": item.get("output_schema"),
        })
    return {"schema_version": "agent_engine_page_actions/v1", "run_id": run_id, "actions": allowed}


@app.get("/v1/internal/agent-runs/{run_id}/artifacts/{artifact_type}/{artifact_id}")
def read_internal_agent_artifact(
    run_id: str,
    artifact_type: str,
    artifact_id: str,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    _require_internal_agent_token(run_id, authorization)
    if not re.fullmatch(r"[a-z][a-z0-9_]{1,80}", artifact_type):
        raise PlatformError("ARTIFACT_NOT_FOUND", "工件类型无效。", status_code=404)
    run = agent_run_store.read(run_id)
    run_context = _record(run.get("context"))
    focused_task = _record(run_context.get("focused_task"))
    flow_analysis_id = str(
        focused_task.get("application_flow_analysis_id")
        or run_context.get("application_flow_analysis_id")
        or ""
    ).strip()
    if flow_analysis_id and artifact_type in {"application_flow_discovery", "application_flow_scenario"}:
        artifact = application_flows.require_analysis_scope(artifact_type, artifact_id, flow_analysis_id)
    else:
        artifact = store.read(artifact_type, artifact_id)
    agent_run_store.append_event(run_id, "artifact.read", str(run.get("state") or "running"), {"artifact_type": artifact_type, "artifact_id": artifact_id})
    return {"artifact": artifact}


@app.post("/v1/builds")
def create_build(request: BuildRequestCreateRequest, http_request: Request) -> dict[str, Any]:
    """Create an explicit E0 Build Worker request outside an Agent loop."""

    key = _require_idempotency_key(http_request)
    identity = request.model_dump()
    existing = _idempotency_lookup("create_build", key, identity)
    if existing is not None:
        return existing
    source = store.read("build_source", request.source_artifact_id)
    artifact = build_worker.build(
        project_id=PROJECT_ID,
        agent_run_id=request.agent_run_id,
        source_artifact=source,
        profile_id=request.profile_id,
        target=request.target,
        timeout_seconds=request.timeout_seconds,
    )
    response = {"artifact": artifact}
    _idempotency_store("create_build", key, identity, response)
    return response


@app.get("/v1/builds/{build_result_artifact_id}")
def get_build(build_result_artifact_id: str) -> dict[str, Any]:
    return {"artifact": store.read("build_result", build_result_artifact_id)}


@app.post("/v1/device-leases")
def create_device_lease(request: DeviceLeaseRequest, http_request: Request) -> dict[str, Any]:
    key = _require_idempotency_key(http_request)
    identity = request.model_dump()
    existing = _idempotency_lookup("create_device_lease", key, identity)
    if existing is not None:
        existing_lease = _record(existing.get("lease"))
        if device_gateway.lease_is_reusable(
            _text(existing_lease.get("lease_id"), 80),
            project_id=PROJECT_ID,
            bridge_id=request.bridge_id,
            logical_device_id=request.logical_device_id,
        ):
            return existing
    response = {"lease": device_gateway.create_lease(project_id=PROJECT_ID, bridge_id=request.bridge_id, logical_device_id=request.logical_device_id, ttl_seconds=request.ttl_seconds)}
    _idempotency_store("create_device_lease", key, identity, response)
    return response


@app.post("/v1/device-bridges/pairing-codes")
def create_device_bridge_pairing_code(request: DeviceBridgePairingCodeRequest, http_request: Request) -> dict[str, Any]:
    key = _require_idempotency_key(http_request)
    identity = request.model_dump()
    existing = _secret_response_idempotency_lookup("device_bridge_pairing_code", key, identity)
    if existing is not None:
        return existing
    response = device_gateway.create_pairing_code(project_id=PROJECT_ID, ttl_seconds=request.ttl_seconds)
    _secret_response_idempotency_store("device_bridge_pairing_code", key, identity, response, ttl_seconds=request.ttl_seconds)
    return response


@app.post("/v1/device-bridges/register")
def register_device_bridge(request: DeviceBridgeRegistrationRequest, http_request: Request) -> dict[str, Any]:
    """Register a trusted single-user Bridge; pairing remains legacy-compatible."""

    key = _require_idempotency_key(http_request)
    identity = {
        "registration_mode": "pairing_code" if request.pairing_code else "trusted_single_user",
        "pairing_code_sha256": hashlib.sha256(request.pairing_code.encode("utf-8")).hexdigest() if request.pairing_code else None,
        "display_name": request.display_name,
        "capabilities": request.capabilities,
    }
    existing = _secret_response_idempotency_lookup("device_bridge_register", key, identity)
    if existing is not None:
        return existing
    response = (
        device_gateway.register_bridge(
            pairing_code=request.pairing_code,
            display_name=request.display_name,
            capabilities=request.capabilities,
        )
        if request.pairing_code
        else device_gateway.register_trusted_bridge(
            project_id=PROJECT_ID,
            display_name=request.display_name,
            capabilities=request.capabilities,
        )
    )
    _secret_response_idempotency_store("device_bridge_register", key, identity, response, ttl_seconds=10 * 60)
    return response


@app.get("/v1/device-bridges")
def list_device_bridges() -> dict[str, Any]:
    return {"bridges": device_gateway.list_bridges(project_id=PROJECT_ID)}


@app.post("/v1/device-bridges/poll")
def poll_device_bridge(
    request: DeviceBridgePollRequest,
    x_bridge_id: str = Header(default="", alias="X-Bridge-Id"),
    x_bridge_secret: str = Header(default="", alias="X-Bridge-Secret"),
) -> dict[str, Any]:
    """Receive queued work without relying on WebSocket framing or Upgrade."""

    if request.probe_only:
        device_gateway.authenticate_bridge(x_bridge_id, x_bridge_secret)
        return {
            "schema_version": "device_bridge_http_poll/v1",
            "protocol_version": HTTP_POLL_PROTOCOL_VERSION,
            "bridge_id": x_bridge_id,
            "transport": "http_poll",
            "probe_verified": True,
            "messages": [],
            "poll_after_ms": 750,
        }
    response = device_gateway.poll_bridge_messages(
        x_bridge_id,
        x_bridge_secret,
        connection_id=request.connection_id,
        capabilities=request.capabilities,
        reconcile_jobs=request.reconcile_jobs,
        max_messages=request.max_messages,
    )
    return {"schema_version": "device_bridge_http_poll/v1", **response}


def _record_device_bridge_event(bridge_id: str, message: dict[str, Any]) -> None:
    kind = str(message.get("type") or "")
    if kind == "bridge.heartbeat":
        capabilities = message.get("capabilities") if isinstance(message.get("capabilities"), dict) else None
        device_gateway.heartbeat(bridge_id, capabilities=capabilities)
    elif kind == "job.result":
        result = message.get("result") if isinstance(message.get("result"), dict) else {}
        device_gateway.report_bridge_result(
            bridge_id,
            {
                "job_id": message.get("job_id"),
                "request_id": message.get("request_id"),
                "bridge_id": bridge_id,
                "started_at_epoch": message.get("started_at_epoch"),
                "finished_at_epoch": message.get("finished_at_epoch"),
                "result": result,
            },
        )
    elif kind == "job.accepted":
        device_gateway.mark_bridge_job_accepted(bridge_id, str(message.get("job_id") or ""))
    elif kind == "job.progress":
        progress = message.get("progress") if isinstance(message.get("progress"), dict) else {}
        device_gateway.report_bridge_progress(
            bridge_id,
            {
                "job_id": message.get("job_id"),
                "request_id": message.get("request_id"),
                "progress": progress,
            },
        )
    elif kind == "job.cancelled":
        device_gateway.report_bridge_result(
            bridge_id,
            {
                "job_id": message.get("job_id"),
                "request_id": message.get("request_id"),
                "bridge_id": bridge_id,
                "result": {"status": "cancelled", "detail": "Bridge 已终止对应 HDC 子进程。"},
            },
        )
    else:
        raise PlatformError(
            "BRIDGE_EVENT_UNSUPPORTED",
            "Device Bridge 提交了未注册的事件类型。",
            status_code=422,
            details={"type": kind},
        )


@app.post("/v1/device-bridges/events")
def report_device_bridge_event(
    request: DeviceBridgeEventRequest,
    x_bridge_id: str = Header(default="", alias="X-Bridge-Id"),
    x_bridge_secret: str = Header(default="", alias="X-Bridge-Secret"),
) -> dict[str, Any]:
    device_gateway.authenticate_bridge(x_bridge_id, x_bridge_secret)
    if len(json.dumps(request.message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) > 256 * 1024:
        raise PlatformError("BRIDGE_EVENT_TOO_LARGE", "Device Bridge 事件超过 256 KiB 上限。", status_code=413)
    disposition = "accepted"
    reason_code: str | None = None
    try:
        _record_device_bridge_event(x_bridge_id, request.message)
    except PlatformError as exc:
        if exc.code not in {
            "BRIDGE_JOB_RESULT_STALE",
            "BRIDGE_JOB_PROGRESS_STALE",
            "BRIDGE_JOB_ACCEPTED_STALE",
        }:
            raise
        # A completed event may outlive the backend's retained job state after
        # a restart or cleanup.  Treat this authenticated replay as an
        # idempotent no-op so old Bridge clients do not retry it forever.
        disposition = "stale_ignored"
        reason_code = exc.code
        device_gateway.record_ignored_bridge_event(
            x_bridge_id,
            event_type=str(request.message.get("type") or ""),
            job_id=str(request.message.get("job_id") or ""),
            reason_code=exc.code,
        )
    device_gateway.heartbeat(x_bridge_id)
    return {
        "schema_version": "device_bridge_http_event_ack/v1",
        "protocol_version": HTTP_POLL_PROTOCOL_VERSION,
        "bridge_id": x_bridge_id,
        "acknowledged": True,
        "event_type": str(request.message.get("type") or ""),
        "event_disposition": disposition,
        "reason_code": reason_code,
    }


@app.get("/v1/device-bridges/jobs/{job_id}/artifact")
def download_bridge_job_artifact(
    job_id: str,
    token: str,
    x_bridge_id: str = Header(default="", alias="X-Bridge-Id"),
    x_bridge_secret: str = Header(default="", alias="X-Bridge-Secret"),
) -> FileResponse:
    device_gateway.authenticate_bridge(x_bridge_id, x_bridge_secret)
    grant = device_gateway.resolve_download_grant(token=token, job_id=job_id, bridge_id=x_bridge_id)
    return FileResponse(grant["path"], media_type="application/octet-stream", filename="experiment.bin", headers={"X-Artifact-Sha256": grant["sha256"]})


@app.post("/v1/device-bridges/jobs/{job_id}/collected")
async def upload_bridge_collected_artifact(job_id: str, request: Request) -> dict[str, Any]:
    bridge_id = str(request.headers.get("X-Bridge-Id") or "")
    bridge_secret = str(request.headers.get("X-Bridge-Secret") or "")
    device_gateway.authenticate_bridge(bridge_id, bridge_secret)
    device_gateway.verify_job_owner(job_id=job_id, bridge_id=bridge_id)
    declared_hash = str(request.headers.get("X-Artifact-Sha256") or "")
    if not re.fullmatch(r"[a-f0-9]{64}", declared_hash):
        raise PlatformError("BRIDGE_COLLECT_HASH_INVALID", "Bridge 上传文件必须声明 SHA-256。", status_code=422)
    declared_length_text = request.headers.get("Content-Length")
    declared_length = None
    if declared_length_text is not None:
        if not re.fullmatch(r"[0-9]+", declared_length_text):
            raise PlatformError("BRIDGE_COLLECT_LENGTH_INVALID", "Bridge 上传文件长度无效。", status_code=400)
        declared_length = int(declared_length_text)
        if declared_length > MAX_UPLOAD_BYTES:
            raise PlatformError("BRIDGE_COLLECT_TOO_LARGE", "Bridge 上传的采集文件超过配置上限。", status_code=413, details={"max_bytes": MAX_UPLOAD_BYTES})
    root = STATE_ROOT / "device-gateway" / "bridge-collected"
    root.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{job_id}-", suffix=".upload", dir=root)
    temporary = Path(temporary_name)
    digest = hashlib.sha256()
    size = 0
    try:
        with os.fdopen(descriptor, "wb") as handle:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_UPLOAD_BYTES:
                    raise PlatformError("BRIDGE_COLLECT_TOO_LARGE", "Bridge 上传的采集文件超过配置上限。", status_code=413, details={"max_bytes": MAX_UPLOAD_BYTES})
                digest.update(chunk)
                handle.write(chunk)
            if declared_length is not None and size != declared_length:
                raise PlatformError("BRIDGE_COLLECT_LENGTH_MISMATCH", "Bridge 上传文件实际长度与声明不一致。", status_code=400)
            observed_hash = digest.hexdigest()
            if declared_hash != observed_hash:
                raise PlatformError("BRIDGE_COLLECT_HASH_MISMATCH", "Bridge 上传文件哈希不匹配。", status_code=409)
            handle.flush()
            os.fsync(handle.fileno())
        target = root / f"{job_id}-{observed_hash[:16]}.bin"
        os.replace(temporary, target)
        return {"status": "stored", "job_id": job_id, "sha256": observed_hash, "bytes": size}
    finally:
        temporary.unlink(missing_ok=True)


@app.websocket("/v1/device-bridges/ws")
async def device_bridge_websocket(websocket: WebSocket) -> None:
    """Outbound Bridge channel. Jobs are signed before being offered.

    The bridge secret is sent once in the authenticated ``bridge.hello`` frame,
    never as a query parameter.  Reverse proxies commonly log WebSocket URLs;
    putting an identity credential there would violate the run/trace contract.
    """

    try:
        require_device_validation()
    except PlatformError as exc:
        await websocket.close(code=4403, reason=exc.code)
        return
    await websocket.accept()
    loop = __import__("asyncio").get_running_loop()

    def send(message: dict[str, Any]) -> None:
        __import__("asyncio").run_coroutine_threadsafe(websocket.send_json(message), loop)

    connected = False
    bridge_id = ""
    bridge_secret = ""
    try:
        while True:
            message = await websocket.receive_json()
            if not isinstance(message, dict):
                continue
            kind = str(message.get("type") or "")
            if kind == "bridge.hello":
                if connected:
                    await websocket.close(code=4403)
                    return
                bridge_id = str(message.get("bridge_id") or "")
                bridge_secret = str(message.get("bridge_secret") or "")
                if str(message.get("protocol_version") or "") != WEBSOCKET_PROTOCOL_VERSION:
                    await websocket.close(code=4406)
                    return
                try:
                    device_gateway.authenticate_bridge(bridge_id, bridge_secret)
                except PlatformError:
                    await websocket.close(code=4401)
                    return
                capabilities = message.get("capabilities") if isinstance(message.get("capabilities"), dict) else {}
                device_gateway.connect_bridge(
                    bridge_id,
                    bridge_secret,
                    capabilities=capabilities,
                    send=send,
                    transport="websocket",
                )
                connected = True
                await websocket.send_json({"type": "bridge.welcome", "protocol_version": WEBSOCKET_PROTOCOL_VERSION, "bridge_id": bridge_id})
            elif not connected:
                await websocket.close(code=4401)
                return
            elif kind in {"bridge.heartbeat", "job.result", "job.accepted", "job.progress", "job.cancelled"}:
                _record_device_bridge_event(bridge_id, message)
            elif kind == "job.reconcile":
                known = message.get("jobs") if isinstance(message.get("jobs"), list) else []
                offers = device_gateway.reconcile_bridge_jobs(bridge_id, [item for item in known if isinstance(item, dict)])
                await websocket.send_json({"type": "job.reconcile", "jobs": [{key: value for key, value in job.items() if key != "signature"} for job in offers]})
                for job in offers:
                    await websocket.send_json({"type": "job.offer", "job": job})
    except WebSocketDisconnect:
        pass
    finally:
        if connected:
            device_gateway.disconnect_bridge(bridge_id, transport="websocket")


@app.get("/v1/page-actions")
def page_actions() -> dict[str, Any]:
    return {"actions": action_catalog(), "skills": skill_catalog()}


@app.get("/v1/skill-version-library")
def get_skill_version_library() -> dict[str, Any]:
    return skill_version_library()


@app.get("/v1/user-skill-library")
def get_user_skill_library(profile_id: str = DEFAULT_SKILL_PROFILE_ID) -> dict[str, Any]:
    return skill_management_catalog(profile_id)


@app.get("/v1/user-skill-library/skills/{skill_id}")
def get_user_skill_detail(
    skill_id: str,
    profile_id: str = DEFAULT_SKILL_PROFILE_ID,
    version_id: str = "",
) -> dict[str, Any]:
    return skill_management_detail(skill_id, profile_id=profile_id, version_id=version_id or None)


@app.post("/v1/user-skill-library/versions")
def create_user_skill_version(request: UserSkillVersionCreateRequest) -> dict[str, Any]:
    return save_user_skill_version(
        skill_id=request.skill_id,
        label_zh=request.label_zh,
        description_zh=request.description_zh,
        content=request.content,
        profile_id=request.profile_id,
        prompt_slot=request.prompt_slot,
        change_summary_zh=request.change_summary_zh,
        created_by="web_ui",
    )


@app.post("/v1/user-skill-library/skills/{skill_id}/activation")
def activate_user_skill(
    skill_id: str,
    request: UserSkillActivationRequest,
) -> dict[str, Any]:
    return activate_user_skill_version(skill_id, request.version_id, profile_id=request.profile_id)


@app.get("/v1/experience-evolution/sources")
def list_experience_sources(limit: int = 100) -> dict[str, Any]:
    return experience_evolution.source_catalog(limit=limit)


@app.get("/v1/experience-evolution/sources/{source_type}/{source_id}")
def read_experience_source(
    source_type: str,
    source_id: str,
    after: int = 0,
    limit: int = 500,
) -> dict[str, Any]:
    return experience_evolution.read_source(source_type, source_id, after=after, limit=limit)


@app.get("/v1/experience-evolution")
def get_experience_evolution_catalog() -> dict[str, Any]:
    return experience_evolution.catalog()


@app.get("/v1/experience-evolution/cases/{case_id}")
def get_experience_case(case_id: str) -> dict[str, Any]:
    return experience_evolution.read_case(case_id)


@app.post("/v1/experience-evolution/cases/{case_id}/triage")
def triage_experience_case(case_id: str, request: ExperienceCaseTriageRequest) -> dict[str, Any]:
    return experience_evolution.triage_case(
        case_id,
        status=request.status,
        selected_resolution_id=request.selected_resolution_id,
        operator_note=request.operator_note,
        actor="web_ui",
    )


@app.post("/v1/projects/{project_id}/analysis-traces/{analysis_id}/events")
def append_analysis_trace_event(
    project_id: str,
    analysis_id: str,
    request: AnalysisTraceEventRequest,
) -> dict[str, Any]:
    _require_project(project_id)
    return analysis_trace_store.append(analysis_id, request.model_dump())


@app.get("/v1/projects/{project_id}/analysis-traces/{analysis_id}")
def read_analysis_trace(project_id: str, analysis_id: str, limit: int = 5000) -> dict[str, Any]:
    _require_project(project_id)
    return analysis_trace_store.read(analysis_id, limit=limit)


@app.put("/v1/projects/{project_id}/analysis-workspaces/{analysis_id}")
def save_analysis_workspace(
    project_id: str,
    analysis_id: str,
    request: AnalysisWorkspaceSnapshotRequest,
) -> dict[str, Any]:
    """Persist the latest recoverable browser projection for one analysis."""

    _require_project(project_id)
    return analysis_workspace_store.write(analysis_id, request.snapshot)


@app.get("/v1/projects/{project_id}/analysis-workspaces")
def list_analysis_workspaces(project_id: str, limit: int = 40) -> dict[str, Any]:
    """List server-persisted analysis records for browser/session recovery."""

    _require_project(project_id)
    return {"records": analysis_workspace_store.catalog(limit=limit)}


@app.get("/v1/projects/{project_id}/analysis-workspaces/{analysis_id}")
def get_analysis_workspace(project_id: str, analysis_id: str) -> dict[str, Any]:
    _require_project(project_id)
    snapshot = analysis_workspace_store.read(analysis_id)
    if snapshot is None:
        raise PlatformError(
            "ANALYSIS_WORKSPACE_SNAPSHOT_NOT_FOUND",
            "此分析记录尚未保存完整工作台快照。",
            status_code=404,
            details={"analysis_id": analysis_id},
        )
    run = _bound_analysis_run(analysis_id)
    return {
        **snapshot,
        "agent_session": {
            "bound": run is not None,
            "run": agent_run_store.public(run) if run is not None else None,
        },
    }


@app.get("/v1/projects/{project_id}/analysis-executions/{analysis_id}")
def get_analysis_execution(project_id: str, analysis_id: str) -> dict[str, Any]:
    """Return the visual audit projection across page, Run, and native Agent logs."""

    _require_project(project_id)
    return build_analysis_execution_view(
        analysis_id,
        trace_store=analysis_trace_store,
        session_registry=analysis_agent_sessions,
        run_store=agent_run_store,
        workspace_store=analysis_workspace_store,
    )


@app.get("/v1/projects/{project_id}/analysis-traces/{analysis_id}/download")
def download_analysis_trace(project_id: str, analysis_id: str) -> FileResponse:
    _require_project(project_id)
    path = analysis_trace_store.download_path(analysis_id)
    return FileResponse(
        path,
        media_type="application/x-ndjson",
        filename=f"{analysis_id}.jsonl",
    )


@app.get("/v1/source-connectors")
def list_connectors() -> dict[str, Any]:
    return {"connectors": connectors.list()}


@app.get("/v1/runtime-assets/openharmony-sdk")
def openharmony_sdk_asset_status() -> dict[str, Any]:
    return openharmony_sdk_assets.status()


@app.post("/v1/runtime-assets/openharmony-sdk/provision")
def provision_openharmony_sdk_asset(request: OpenHarmonySdkProvisionRequest) -> dict[str, Any]:
    return openharmony_sdk_assets.provision_from_connector(request.source_connector_id)


@app.post("/v1/source-connectors/local")
def register_local_connector(request: LocalConnectorRequest) -> dict[str, Any]:
    connector = connectors.register_local(Path(request.source_root), display_name=request.display_name)
    return {"connector": connector}


@app.post("/v1/source-connectors/{connector_id}/refresh")
def refresh_source_connector_snapshot(connector_id: str) -> dict[str, Any]:
    """Refresh a local development snapshot; a Git commit is not required."""

    connector = connectors.refresh_snapshot(connector_id)
    return {"connector": connector}


@app.post("/v1/source-connectors/git/plan")
def create_git_clone_plan(request: GitClonePlanRequest) -> dict[str, Any]:
    source_connector_id = _connector_id(request.source_connector_id)
    return {
        "plan": connectors.create_clone_plan(
            request.url,
            display_name=request.display_name,
            source_connector_id=source_connector_id,
        )
    }


@app.post("/v1/source-connectors/git/clone")
def clone_git_repository(request: CloneRequest) -> dict[str, Any]:
    return connectors.clone_from_plan(request.plan_id)


@app.get("/v1/custom-api-kbs")
def list_custom_api_knowledge_bases() -> dict[str, Any]:
    """List user-built catalogs without mixing them into release KBs."""

    values = custom_api_kbs.list()
    return {
        "schema_version": "custom_accelerator_api_kb_catalog/v1",
        "knowledge_bases": values,
        "count": len(values),
        "platform_merge_policy": "developer_only",
    }


@app.post("/v1/custom-api-kbs/build")
def build_custom_api_knowledge_base(request: CustomApiKnowledgeBaseBuildRequest) -> dict[str, Any]:
    return custom_api_kbs.build(
        unit_name=request.unit_name,
        source_connector_id=request.source_connector_id,
        source_root=request.source_root,
    )


@app.get("/v1/custom-api-kbs/{kb_id}")
def read_custom_api_knowledge_base(kb_id: str, query: str = "") -> dict[str, Any]:
    return custom_api_kbs.read(kb_id, query=query)


@app.get("/v1/custom-api-kbs/{kb_id}/records/{record_id}")
def read_custom_api_knowledge_base_record(kb_id: str, record_id: str) -> dict[str, Any]:
    return custom_api_kbs.record(kb_id, record_id)


@app.delete("/v1/custom-api-kbs/{kb_id}")
def delete_custom_api_knowledge_base(kb_id: str) -> dict[str, Any]:
    return custom_api_kbs.delete(kb_id)


@app.get("/v1/code-graphs")
def list_code_graphs(source_connector_id: str = "", limit: int = 100) -> dict[str, Any]:
    values = code_graphs.list_graphs(
        source_connector_id=source_connector_id,
        limit=max(1, min(limit, 500)),
    )
    return {
        "schema_version": "module_code_graph_catalog/v1",
        "extractor_available": code_graphs.available(),
        "graphs": values,
        "count": len(values),
    }


@app.get("/v1/code-knowledge/integration")
def describe_code_knowledge_integration() -> dict[str, Any]:
    return {
        "schema_version": "code_knowledge_engine/v1",
        "engine_id": "platform-tree-sitter",
        "status": "ready" if code_graphs.available() else "unavailable",
        "capabilities": [
            "c_cpp_symbol_index",
            "bounded_call_candidate_query",
            "hitrace_anchor_index",
            "agent_native_source_analysis",
        ],
        "retrieval_policy": "bounded_static_graph_then_native_source",
    }


@app.post("/v1/code-graphs/builds")
def build_code_graph(request: CodeGraphBuildRequest) -> dict[str, Any]:
    return code_graphs.start_build(
        request.source_connector_id,
        subpath=request.subpath,
        max_files=request.max_files,
        force_new=request.force_new,
    )


@app.post("/v1/code-graphs/estimates")
def estimate_code_graph(request: CodeGraphBuildRequest) -> dict[str, Any]:
    return code_graphs.request_estimate(
        request.source_connector_id,
        subpath=request.subpath,
        max_files=request.max_files,
        force_new=request.force_new,
    )


@app.post("/v1/code-graphs/{graph_id}/confirm")
def confirm_code_graph(graph_id: str) -> dict[str, Any]:
    return code_graphs.confirm_build(graph_id)


@app.post("/v1/code-graphs/{graph_id}/cancel")
def cancel_code_graph(graph_id: str) -> dict[str, Any]:
    return code_graphs.cancel_build(graph_id)


@app.delete("/v1/code-graphs/{graph_id}")
def delete_code_graph(graph_id: str) -> dict[str, Any]:
    return code_graphs.delete_graph(graph_id)


@app.post("/v1/code-graphs/{graph_id}/semantic-analysis")
def claim_code_knowledge_semantic_analysis(graph_id: str, request: CodeKnowledgeAgentAnalysisRequest) -> dict[str, Any]:
    return code_graphs.claim_semantic_analysis(graph_id, request_id=request.request_id, force=request.force)


@app.get("/v1/code-graphs/{graph_id}")
def read_code_graph(graph_id: str) -> dict[str, Any]:
    return {"graph": code_graphs.get_graph(graph_id)}


@app.get("/v1/code-graphs/{graph_id}/search")
def search_code_graph(graph_id: str, query: str = "", limit: int = 30) -> dict[str, Any]:
    return code_graphs.search(graph_id, query=query, limit=max(1, min(limit, 100)))


@app.get("/v1/code-knowledge-reports")
def list_code_knowledge_reports(graph_id: str = "", source_connector_id: str = "", limit: int = 100) -> dict[str, Any]:
    reports = code_graphs.list_reports(
        graph_id=graph_id,
        source_connector_id=source_connector_id,
        limit=max(1, min(limit, 500)),
    )
    return {"schema_version": "code_knowledge_report_catalog/v1", "reports": reports, "count": len(reports)}


@app.get("/v1/code-knowledge-reports/{report_id}")
def read_code_knowledge_report(report_id: str) -> dict[str, Any]:
    return {"report": code_graphs.read_report(report_id)}


@app.get("/v1/code-knowledge-reports/{report_id}/markdown")
def download_code_knowledge_report(report_id: str) -> FileResponse:
    report = code_graphs.read_report(report_id)
    return FileResponse(
        code_graphs.report_markdown_path(report_id),
        media_type="text/markdown; charset=utf-8",
        filename=f"{str(report.get('title') or 'code-knowledge-report')[:80]}.md",
    )


@app.delete("/v1/code-knowledge-reports/{report_id}")
def delete_code_knowledge_report(report_id: str) -> dict[str, Any]:
    return code_graphs.delete_report(report_id)


@app.post("/v1/experimental-flows/evidence")
async def upload_experimental_flow_evidence(
    file: UploadFile = File(...),
    analysis_kind: str = Form(...),
    source_connector_id: str = Form(...),
) -> dict[str, Any]:
    media_type = file.content_type or "application/octet-stream"
    path, input_sha256, size, name = await _persist_upload(file)
    artifact = experimental_flows.register_evidence(
        analysis_kind=analysis_kind,
        source_connector_id=source_connector_id,
        managed_path=str(path.relative_to(STATE_ROOT)),
        file_name=name,
        content_sha256=input_sha256,
        size_bytes=size,
        media_type=media_type,
    )
    return {"evidence": artifact}


@app.get("/v1/experimental-flows/evidence")
def list_experimental_flow_evidence(
    analysis_kind: str,
    source_connector_id: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    values = experimental_flows.list_evidence(
        analysis_kind=analysis_kind,
        source_connector_id=source_connector_id,
        limit=max(1, min(limit, 500)),
    )
    return {
        "schema_version": "experimental_flow_evidence_catalog/v1",
        "analysis_kind": analysis_kind,
        "evidence": values,
        "count": len(values),
    }


@app.get("/v1/experimental-flows/evidence/{artifact_id}")
def read_experimental_flow_evidence(artifact_id: str) -> dict[str, Any]:
    return experimental_flows.read_evidence(artifact_id)


@app.get("/v1/experimental-flows/results")
def list_experimental_flow_results(
    analysis_kind: str,
    source_connector_id: str = "",
    limit: int = 100,
) -> dict[str, Any]:
    values = experimental_flows.list_results(
        analysis_kind=analysis_kind,
        source_connector_id=source_connector_id,
        limit=max(1, min(limit, 500)),
    )
    return {
        "schema_version": "experimental_flow_result_catalog/v1",
        "analysis_kind": analysis_kind,
        "results": values,
        "count": len(values),
    }


@app.get("/v1/experimental-flows/results/{artifact_id}")
def read_experimental_flow_result(artifact_id: str) -> dict[str, Any]:
    return {"result": experimental_flows.read_result(artifact_id)}


@app.get("/v1/experimental-flows/results/{artifact_id}/mermaid")
def download_experimental_flow_mermaid(artifact_id: str) -> Response:
    result = experimental_flows.read_result(artifact_id)
    stem = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(result.get("title") or "experimental-flow"))[:100]
    return Response(
        content=str(result.get("mermaid") or "") + "\n",
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{stem}.mermaid"'},
    )


@app.delete("/v1/experimental-flows/results/{artifact_id}")
def delete_experimental_flow_result(artifact_id: str) -> dict[str, Any]:
    return experimental_flows.delete_result(artifact_id)


@app.get("/v1/application-flows")
def list_application_flow_assets(source_connector_id: str = "", limit: int = 100) -> dict[str, Any]:
    return application_flows.list_assets(source_connector_id=source_connector_id, limit=max(1, min(limit, 200)))


@app.get("/v1/application-flows/preferences")
def read_application_flow_preferences() -> dict[str, Any]:
    return application_flows.get_preferences()


@app.put("/v1/application-flows/preferences")
def update_application_flow_preferences(request: ApplicationFlowPreferencesRequest) -> dict[str, Any]:
    return application_flows.update_preferences(
        confirm_flow_before_trace=request.confirm_flow_before_trace,
    )


@app.get("/v1/application-flows/runtime-traces")
def list_application_flow_runtime_traces(source_connector_ids: str = "", limit: int = 100) -> dict[str, Any]:
    connector_ids = [item.strip() for item in source_connector_ids.split(",") if item.strip()]
    return application_flow_runtime_traces.list_traces(
        source_connector_ids=connector_ids,
        limit=max(1, min(limit, 200)),
    )


@app.post("/v1/application-flows/runtime-traces/bind")
def bind_application_flow_runtime_trace(request: ApplicationFlowRuntimeTraceBindingRequest) -> dict[str, Any]:
    return application_flow_runtime_traces.bind_trace(
        request.application_flow_analysis_id,
        request.analysis_artifact_id or "",
    )


@app.get("/v1/application-flows/runtime-traces/{artifact_id}")
def read_application_flow_runtime_trace(artifact_id: str) -> dict[str, Any]:
    return application_flow_runtime_traces.get_trace(artifact_id)


@app.delete("/v1/application-flows/runtime-traces/{artifact_id}")
def delete_application_flow_runtime_trace(artifact_id: str) -> dict[str, Any]:
    return application_flow_runtime_traces.delete_trace(artifact_id)


@app.post("/v1/application-flows/repositories/inspect")
def inspect_application_flow_repository(request: ApplicationFlowRepositoryInspectRequest) -> dict[str, Any]:
    return application_flows.inspect_repository(request.source_connector_id, subpath=request.subpath)


@app.post("/v1/application-flows/repositories/search")
def search_application_flow_source(request: ApplicationFlowSourceSearchRequest) -> dict[str, Any]:
    return application_flows.search_source(
        request.source_connector_id,
        query=request.query,
        subpath=request.subpath,
        max_results=request.max_results,
    )


@app.post("/v1/application-flows/repositories/read")
def read_application_flow_source(request: ApplicationFlowSourceReadRequest) -> dict[str, Any]:
    return application_flows.read_source(
        request.source_connector_id,
        path=request.path,
        line_start=request.line_start,
        line_end=request.line_end,
    )


@app.post("/v1/application-flows/discoveries")
def save_application_flow_discovery(request: ApplicationFlowDiscoverySaveRequest) -> dict[str, Any]:
    return application_flows.save_discovery(
        request.source_connector_id,
        request.discovery,
        subpath=request.subpath,
    )


@app.get("/v1/application-flows/discoveries/{artifact_id}")
def read_application_flow_discovery(artifact_id: str) -> dict[str, Any]:
    return application_flows.read_discovery(artifact_id)


@app.post("/v1/application-flows/scenarios")
def save_application_flow_scenario(request: ApplicationFlowScenarioSaveRequest) -> dict[str, Any]:
    return application_flows.save_scenario(
        request.source_connector_id,
        discovery_artifact_id=request.discovery_artifact_id,
        flow_id=request.flow_id,
        scenario_json=request.scenario_json,
        mermaid=request.mermaid,
        stage_evidence=request.stage_evidence,
        unknowns=request.unknowns,
        parent_scenario_artifact_id=request.parent_scenario_artifact_id or "",
        revision_request=request.revision_request,
    )


@app.post("/v1/application-flows/scenarios/compile-mermaid")
def compile_application_flow_mermaid(request: ApplicationFlowMermaidCompileRequest) -> dict[str, Any]:
    return application_flows.compile_mermaid(
        request.source_connector_id,
        discovery_artifact_id=request.discovery_artifact_id,
        flow_id=request.flow_id,
        mermaid=request.mermaid,
        parent_scenario_artifact_id=request.parent_scenario_artifact_id or "",
        revision_request=request.revision_request,
    )


@app.post("/v1/application-flows/scenarios/enrich-trace-anchors")
def enrich_application_flow_trace_anchors(request: ApplicationFlowTraceAnchorEnrichmentRequest) -> dict[str, Any]:
    return application_flows.enrich_trace_anchors(
        request.source_connector_id,
        discovery_artifact_id=request.discovery_artifact_id,
        scenario_artifact_id=request.scenario_artifact_id,
        flow_id=request.flow_id,
        stage_results=[item.model_dump(exclude_none=True) for item in request.stage_results],
    )


@app.get("/v1/application-flows/scenarios/{artifact_id}")
def read_application_flow_scenario(artifact_id: str) -> dict[str, Any]:
    return application_flows.read_scenario(artifact_id)


@app.delete("/v1/application-flows/{artifact_type}/{artifact_id}")
def delete_application_flow_asset(artifact_type: str, artifact_id: str, cascade: bool = False) -> dict[str, Any]:
    return application_flows.delete_asset(artifact_type, artifact_id, cascade=cascade)


@app.get("/v1/application-flows/scenarios/{artifact_id}/json")
def download_application_flow_scenario_json(artifact_id: str) -> Response:
    artifact = application_flows.read_scenario(artifact_id)
    scenario = _record(artifact.get("scenario_json"))
    filename = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(scenario.get("scenario") or "application-flow")) + ".json"
    return Response(
        content=json.dumps(scenario, ensure_ascii=False, indent=2) + "\n",
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/v1/application-flows/scenarios/{artifact_id}/mermaid")
def download_application_flow_scenario_mermaid(artifact_id: str) -> Response:
    artifact = application_flows.read_scenario(artifact_id)
    scenario = _record(artifact.get("scenario_json"))
    filename = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(scenario.get("scenario") or "application-flow")) + ".mermaid"
    return Response(
        content=str(artifact.get("mermaid") or "") + "\n",
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.get("/v1/application-flows/scenarios/{artifact_id}/stage-analysis")
def download_application_flow_stage_analysis(artifact_id: str) -> Response:
    artifact = application_flows.read_scenario(artifact_id)
    stage_analysis = _record(artifact.get("stage_analysis"))
    if not stage_analysis:
        raise PlatformError(
            "APPLICATION_FLOW_STAGE_ANALYSIS_NOT_FOUND",
            "该流程版本尚未生成真实 Trace 阶段分析。",
            status_code=404,
        )
    scenario = _record(artifact.get("scenario_json"))
    stem = re.sub(r"[^a-zA-Z0-9._-]+", "-", str(scenario.get("scenario") or "application-flow"))
    return Response(
        content=json.dumps(stage_analysis, ensure_ascii=False, indent=2) + "\n",
        media_type="application/json",
        headers={"Content-Disposition": f'attachment; filename="{stem}-stage-analysis.json"'},
    )


def _kb_version_response(knowledge_base: str) -> dict[str, Any]:
    payload = _optimization_kb() if knowledge_base == "optimization" else _hardware_kb()
    records = _optimization_kb_records() if knowledge_base == "optimization" else _hardware_kb_records()
    return {
        "knowledge_base": knowledge_base,
        "kb_id": payload.get("kb_id"),
        "kb_version": payload.get("kb_version"),
        "source_revision": payload.get("source_revision"),
        "schema_version": payload.get("schema_version"),
        "content_sha256": payload.get("content_sha256"),
        "record_count": len(records),
        "trusted_count": sum(_is_trusted_kb_confidence(item["confidence"]) for item in records),
        "nontrusted_count": sum(not _is_trusted_kb_confidence(item["confidence"]) for item in records),
        "summary": payload.get("summary", {}),
        "artifact": payload,
    }


@app.get("/v1/optimization-kb/versions/local-development")
@app.get("/v1/kb/versions/local-development")
def optimization_kb_version() -> dict[str, Any]:
    return _kb_version_response("optimization")


@app.get("/v1/hardware-kb/versions/local-development")
def hardware_kb_version() -> dict[str, Any]:
    return _kb_version_response("hardware")


@app.get("/v1/hardware-kb/npu-api-version")
def npu_api_kb_version() -> dict[str, Any]:
    return {"npu_api_kb": _kb_public_metadata(_npu_api_kb())}


@app.get("/v1/hardware-kb/npu-api/records")
def list_npu_api_kb_records(query: str = "") -> dict[str, Any]:
    """List the source-derived NNRT/MindSpore API catalogue for inspection."""

    records = _npu_api_catalog_records(query)
    operation_count = sum(item.get("record_type") == "npu_operation" for item in records)
    entrypoint_count = sum(item.get("record_type") == "npu_api_entrypoint" for item in records)
    family_counts: Counter[str] = Counter()
    for item in records:
        family = item.get("semantic_family") if isinstance(item.get("semantic_family"), dict) else {}
        family_id = family.get("id")
        if isinstance(family_id, str) and family_id:
            family_counts[family_id] += 1
    return {
        "npu_api_kb": _kb_public_metadata(_npu_api_kb()),
        "query": query,
        "record_count": len(records),
        "summary": {
            "npu_operation_count": operation_count,
            "npu_api_entrypoint_count": entrypoint_count,
            "semantic_family_counts": dict(sorted(family_counts.items())),
        },
        "records": records,
    }


@app.get("/v1/hardware-kb/npu-api/records/{record_id}")
def npu_api_kb_record_detail(record_id: str) -> dict[str, Any]:
    """Return the complete, source-provenance record for one public NPU API."""

    payload = _npu_api_kb()
    record = next(
        (item for item in payload.get("records", []) if isinstance(item, dict) and item.get("record_id") == record_id),
        None,
    )
    if record is None:
        raise PlatformError("KB_RECORD_NOT_FOUND", "NPU API record does not exist.", status_code=404)
    return {"npu_api_kb": _kb_public_metadata(payload), "record": record}


@app.get("/v1/hardware-kb/accelerator-api/units")
def list_accelerator_api_units() -> dict[str, Any]:
    """List GPU/DSP/media source-derived API catalogs and their boundaries."""

    payloads = _accelerator_api_kbs()
    manifest = _accelerator_api_manifest()
    return {
        "accelerator_api_manifest": {
            key: manifest.get(key)
            for key in ("manifest_id", "manifest_version", "source_revision", "content_sha256")
        },
        "units": [
            {
                "accelerator_unit": unit_id,
                "api_kb": _kb_public_metadata(payload),
                "scope": payload.get("scope", {}),
                "evidence_boundary": payload.get("evidence_boundary", {}),
            }
            for unit_id, payload in sorted(payloads.items())
        ],
    }


@app.get("/v1/hardware-kb/accelerator-api/{accelerator_unit}/records")
def list_accelerator_api_kb_records(accelerator_unit: str, query: str = "") -> dict[str, Any]:
    """Browse one GPU/DSP/media API catalog without asserting device support."""

    payload = _accelerator_api_kb(accelerator_unit)
    records = _accelerator_api_catalog_records(accelerator_unit, query)
    family_counts: Counter[str] = Counter()
    exposure_counts: Counter[str] = Counter()
    for item in records:
        family = item.get("semantic_family") if isinstance(item.get("semantic_family"), dict) else {}
        family_id = family.get("id")
        if isinstance(family_id, str) and family_id:
            family_counts[family_id] += 1
        exposure = item.get("exposure")
        if isinstance(exposure, str) and exposure:
            exposure_counts[exposure] += 1
    return {
        "accelerator_api_kb": _kb_public_metadata(payload),
        "accelerator_unit": accelerator_unit,
        "query": query,
        "record_count": len(records),
        "summary": {
            "semantic_family_counts": dict(sorted(family_counts.items())),
            "exposure_counts": dict(sorted(exposure_counts.items())),
            "target_device_availability": "evidence_required",
        },
        "records": records,
    }


@app.get("/v1/hardware-kb/accelerator-api/{accelerator_unit}/records/{record_id}")
def accelerator_api_kb_record_detail(accelerator_unit: str, record_id: str) -> dict[str, Any]:
    """Return one complete source-provenance GPU/DSP/media API record."""

    payload = _accelerator_api_kb(accelerator_unit)
    record = next(
        (item for item in payload.get("records", []) if isinstance(item, dict) and item.get("record_id") == record_id),
        None,
    )
    if record is None:
        raise PlatformError("KB_RECORD_NOT_FOUND", "Accelerator API record does not exist.", status_code=404)
    return {"accelerator_api_kb": _kb_public_metadata(payload), "accelerator_unit": accelerator_unit, "record": record}


@app.get("/v1/kb/cases")
def kb_cases(query: str = "") -> dict[str, Any]:
    catalog = ensure_catalog(CASE_CATALOG_PATH, WORKSPACE_ROOT)
    normalized = query.casefold().strip()
    cases = [
        case
        for case in catalog["cases"]
        if not normalized or normalized in json.dumps(case, ensure_ascii=True).casefold()
    ]
    return {"catalog": {key: catalog[key] for key in ("catalog_id", "catalog_version", "content_sha256")}, "cases": cases}


@app.get("/v1/optimization-kb/strict-cases")
def optimization_kb_strict_cases(query: str = "") -> dict[str, Any]:
    payload = _optimization_kb()
    normalized = query.casefold().strip()
    cases = [
        case
        for case in payload.get("strict_heterogeneous_cases", [])
        if isinstance(case, dict) and (not normalized or normalized in json.dumps(case, ensure_ascii=True, sort_keys=True).casefold())
    ]
    return {
        "kb": _kb_public_metadata(payload),
        "cases": cases,
        "case_count": len(cases),
    }


@app.get("/v1/optimization-kb/strict-cases/{case_id}")
def optimization_kb_strict_case_detail(case_id: str) -> dict[str, Any]:
    payload = _optimization_kb()
    case = next(
        (
            item for item in payload.get("strict_heterogeneous_cases", [])
            if isinstance(item, dict) and item.get("case_id") == case_id
        ),
        None,
    )
    if case is None:
        raise PlatformError("KB_RECORD_NOT_FOUND", "Strict heterogeneous case does not exist.", status_code=404)
    return {
        "kb": _kb_public_metadata(payload),
        "case": case,
        "cpu_source": _strict_case_cpu_source(case),
    }


@app.post("/v1/optimization-kb/search")
def search_optimization_kb(request: KBSearchRequest) -> dict[str, Any]:
    return _search_kb(
        request.query,
        include_unverified=request.include_unverified,
        include_cases=request.include_cases,
        knowledge_base="optimization",
    )


@app.post("/v1/hardware-kb/search")
def search_hardware_kb(request: KBSearchRequest) -> dict[str, Any]:
    return _search_kb(
        request.query,
        include_unverified=request.include_unverified,
        include_cases=False,
        knowledge_base="hardware",
    )


@app.post("/v1/hardware-kb/npu-api/search")
def search_npu_api_kb(request: NpuApiSearchRequest) -> dict[str, Any]:
    """Return deterministic Top-N NNRT operation candidates for supplied code.

    This endpoint is a candidate generator. It returns the same static-semantic
    retrieval used by the hardware reasoning strategy; runtime support remains
    an explicit gate on every returned record.
    """

    code_unit = pasted_code_unit(request.code, request.function_hint, request.event_name)
    candidates, retrieval = rank_npu_operation_candidates(
        code_unit,
        _npu_api_reasoning_records(),
        limit=request.limit,
    )
    return {
        "npu_api_kb": _kb_public_metadata(_npu_api_kb()),
        "retrieval": retrieval,
        "records": candidates,
    }


@app.post("/v1/hardware-kb/accelerator-api/{accelerator_unit}/search")
def search_accelerator_api_kb(accelerator_unit: str, request: AcceleratorApiSearchRequest) -> dict[str, Any]:
    """Generate conservative Top-N API candidates for one accelerator unit."""

    payload = _accelerator_api_kb(accelerator_unit)
    code_unit = pasted_code_unit(request.code, request.function_hint, request.event_name)
    candidates, retrieval = rank_accelerator_api_candidates(
        code_unit,
        {accelerator_unit: _accelerator_api_reasoning_records().get(accelerator_unit, [])},
        per_unit_limit=request.limit,
    )
    return {
        "accelerator_api_kb": _kb_public_metadata(payload),
        "accelerator_unit": accelerator_unit,
        "retrieval": retrieval,
        "records": candidates,
    }


@app.post("/v1/kb/search")
def search_kb(request: KBSearchRequest) -> dict[str, Any]:
    return _search_kb(
        request.query,
        include_unverified=request.include_unverified,
        include_cases=request.include_cases,
        knowledge_base=request.knowledge_base,
    )


@app.post("/v1/kb/validate-query")
def validate_kb_query(request: KBSearchRequest) -> dict[str, Any]:
    result = _search_kb(
        request.query,
        include_unverified=request.include_unverified,
        include_cases=request.include_cases,
        knowledge_base=request.knowledge_base,
    )
    return {
        "valid": True,
        "knowledge_base": result["knowledge_base"],
        "kb_version": result["kb_version"],
        "record_count": len(result["records"]),
        "case_count": len(result["case_records"]),
    }


def _lookup_kb_record(knowledge_base: str, record_type: str, record_id: str) -> dict[str, Any]:
    records = _optimization_kb_records() if knowledge_base == "optimization" else _hardware_kb_records()
    for record in records:
        if record["record_type"] == record_type and record["record_id"] == record_id:
            return record
    if knowledge_base == "optimization" and record_type == "knowledge_case":
        catalog = ensure_catalog(CASE_CATALOG_PATH, WORKSPACE_ROOT)
        for case in catalog["cases"]:
            if case["case_id"] == record_id:
                return case
    raise PlatformError("KB_RECORD_NOT_FOUND", "KB record does not exist.", status_code=404)


@app.get("/v1/optimization-kb/records/{record_type}/{record_id}")
@app.get("/v1/kb/records/{record_type}/{record_id}")
def optimization_kb_record(record_type: str, record_id: str) -> dict[str, Any]:
    return _lookup_kb_record("optimization", record_type, record_id)


@app.get("/v1/hardware-kb/records/{record_type}/{record_id}")
def hardware_kb_record(record_type: str, record_id: str) -> dict[str, Any]:
    return _lookup_kb_record("hardware", record_type, record_id)


@app.get("/v1/fixtures")
def fixtures() -> dict[str, Any]:
    return {"fixtures": [_fixture_metadata(fixture_id) for fixture_id in FIXTURES]}


def _trace_analysis_summary(artifact: dict[str, Any]) -> dict[str, Any]:
    """Return a light list item; raw trace payloads never go to the selector."""

    quality = artifact.get("quality") if isinstance(artifact.get("quality"), dict) else {}
    binding = artifact.get("source_binding") if isinstance(artifact.get("source_binding"), dict) else {}
    return {
        "artifact_id": artifact["artifact_id"],
        "created_at": artifact.get("created_at"),
        "input_kind": artifact.get("input_kind"),
        "analysis_profile": artifact.get("analysis_profile"),
        "source_connector_id": artifact.get("source_connector_id"),
        "source_binding": {key: binding.get(key) for key in ("match_level", "resolved_snapshot_id", "snapshot_state")},
        "hotspot_count": len(artifact.get("hotspot_candidates", [])),
        "cluster_count": len(artifact.get("clusters", [])),
        "quality_status": quality.get("status"),
        "quality_reason": quality.get("reason"),
    }


@app.get("/v1/projects/{project_id}/trace-analysis-results")
def list_reasoning_trace_analysis_results(
    project_id: str,
    source_connector_id: str | None = None,
    limit: int = 40,
) -> dict[str, Any]:
    _require_project(project_id)
    connector_id = _connector_id(source_connector_id)
    artifacts = store.list_artifacts("trace_analysis_result", limit=min(max(limit, 1), 100), source_connector_id=connector_id)
    return {
        "traces": [
            _trace_analysis_summary(artifact)
            for artifact in artifacts
            if artifact.get("input_kind") in {"raw_htrace", "external_trace_analysis_report", "sys_trace_experimental"}
        ]
    }


@app.get("/v1/projects/{project_id}/trace-analysis-results/{analysis_artifact_id}")
def get_reasoning_trace_analysis_result(project_id: str, analysis_artifact_id: str) -> dict[str, Any]:
    """Read one immutable trace summary for historical workbench restoration."""

    _require_project(project_id)
    artifact = store.read("trace_analysis_result", analysis_artifact_id)
    if artifact.get("input_kind") not in {"raw_htrace", "external_trace_analysis_report", "sys_trace_experimental"}:
        raise PlatformError(
            "TRACE_ANALYSIS_NOT_RESTORABLE",
            "The requested artifact is not a restorable trace analysis input.",
            status_code=422,
        )
    return {"trace": _trace_analysis_summary(artifact)}


@app.post("/v1/projects/{project_id}/reasoning-trace-hotspots")
def list_reasoning_trace_hotspots(project_id: str, request: ReasoningTraceHotspotRequest) -> dict[str, Any]:
    _require_project(project_id)
    analysis = store.read("trace_analysis_result", request.analysis_artifact_id)
    catalog, scope = _reasoning_hotspot_catalog(
        analysis,
        limit=request.limit,
        module_key=request.module_key,
        group_by_module=request.group_by_module,
        per_module_limit=request.per_module_limit,
    )
    artifact = _write_trace_event_catalog(analysis, None, catalog, catalog_scope=scope)
    return {"analysis_artifact_id": analysis["artifact_id"], "artifact": artifact, "catalog_scope": scope, **catalog}


@app.post("/v1/projects/{project_id}/reasoning-trace-hotspots/resolve-source")
def resolve_reasoning_trace_hotspot(project_id: str, request: ReasoningTraceHotspotResolveRequest) -> dict[str, Any]:
    _require_project(project_id)
    analysis = store.read("trace_analysis_result", request.analysis_artifact_id)
    catalog, scope = _reasoning_hotspot_catalog(analysis, limit=MAX_SELF_TIME_EVENT_CANDIDATES, module_key=request.module_key)
    event_catalog_artifact = _write_trace_event_catalog(analysis, None, catalog, catalog_scope=scope)
    event = _catalog_event_by_id(catalog, request.event_id)
    if event is None:
        raise PlatformError(
            "TRACE_HOTSPOT_EVENT_NOT_FOUND",
            "Selected event is not available in the current real trace hotspot catalog.",
            status_code=422,
            details={"analysis_artifact_id": request.analysis_artifact_id, "event_id": request.event_id},
        )
    return {"artifact": _resolve_trace_event_source(analysis, None, event, event_catalog_artifact, catalog_scope=scope)}


@app.post("/v1/projects/{project_id}/hotspot-code/from-source-resolution")
def create_hotspot_code_from_source_resolution(project_id: str, request: HotspotCodeFromResolutionRequest) -> dict[str, Any]:
    _require_project(project_id)
    source_resolution = store.read("trace_event_source_resolution", request.source_resolution_artifact_id)
    payload = _hotspot_code_unit_from_resolution(source_resolution)
    artifact = store.write(
        "hotspot_code_unit",
        payload,
        source_connector_id=source_resolution["source_connector_id"],
        source_binding=source_resolution["source_binding"],
        producer={"name": "real-trace-hotspot-function-extractor", "version": "1.0.0"},
        input_artifacts=[
            f"artifact:{source_resolution['artifact_id']}",
            f"artifact:{source_resolution['analysis_artifact_id']}",
        ],
    )
    return {"artifact": artifact}


@app.post("/v1/projects/{project_id}/hotspot-code/from-investigated-source")
def create_hotspot_code_from_investigated_source(
    project_id: str,
    request: HotspotInvestigatedSourceRequest,
) -> dict[str, Any]:
    """Persist a source-observed candidate selected after trace-anchor investigation.

    The source range is verified against the current connector.  Its relation
    to the trace anchor and its hotness remain explicit model hypotheses until
    sampling, a nested span, or another runtime measurement attributes cost.
    """

    _require_project(project_id)
    source_resolution = store.read("trace_event_source_resolution", request.source_resolution_artifact_id)
    connector_id = str(source_resolution["source_connector_id"])
    connector = connectors.get(connector_id)
    source_root = Path(str(connector["source_root"])).resolve()
    source_file = connectors.resolve_source_path(connector_id, request.path).resolve()
    relative_path = str(source_file.relative_to(source_root))
    current_sha = sha256_file(source_file)
    if request.expected_source_sha256 and request.expected_source_sha256 != current_sha:
        raise PlatformError(
            "HOTSPOT_INVESTIGATED_SOURCE_CHANGED",
            "候选源码在 Agent 读取后发生变化；请基于当前文件重新选择范围。",
            status_code=409,
            details={"expected_sha256": request.expected_source_sha256, "current_sha256": current_sha},
        )
    lines = source_file.read_text(encoding="utf-8", errors="replace").splitlines()
    if request.line_end < request.line_start or request.line_end > len(lines):
        raise PlatformError(
            "HOTSPOT_INVESTIGATED_RANGE_INVALID",
            "候选热点的源码行范围无效。",
            status_code=422,
            details={"line_count": len(lines), "line_start": request.line_start, "line_end": request.line_end},
        )
    code = "\n".join(lines[request.line_start - 1 : request.line_end])
    if not code.strip():
        raise PlatformError("HOTSPOT_INVESTIGATED_RANGE_EMPTY", "候选热点源码范围为空。", status_code=422)
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise PlatformError(
            "HOTSPOT_INVESTIGATED_SOURCE_TOO_LARGE",
            f"候选源码超过 {MAX_CODE_BYTES // 1024} KiB；请选择能够独立解释负载的最小函数或局部区域。",
            status_code=413,
        )
    event = source_resolution.get("event") if isinstance(source_resolution.get("event"), dict) else {}
    anchor_source = source_resolution.get("code_block") if isinstance(source_resolution.get("code_block"), dict) else {}
    anchor_location = anchor_source.get("source") if isinstance(anchor_source.get("source"), dict) else {}
    current_binding = _source_binding(connector_id, None)
    call_path: list[dict[str, Any]] = []
    for item in request.call_path:
        call_site = connectors.resolve_source_path(connector_id, item.path).resolve()
        call_site_lines = call_site.read_text(encoding="utf-8", errors="replace").splitlines()
        if item.line > len(call_site_lines):
            raise PlatformError(
                "HOTSPOT_CALL_PATH_LOCATION_INVALID",
                "调用路径证据行超出当前源码文件。",
                status_code=422,
                details={"path": item.path, "line": item.line, "line_count": len(call_site_lines)},
            )
        call_path.append(
            {
                **item.model_dump(exclude_none=True),
                "path": str(call_site.relative_to(source_root)),
                "line_text": call_site_lines[item.line - 1].strip()[:1000],
                "source_sha256": sha256_file(call_site),
                "relation_evidence_level": "source_location_observed_relation_model_hypothesis",
            }
        )
    payload = {
        "hotspot": {
            "event_id": str(event.get("event_id") or "selected_event"),
            "event_name": str(event.get("event_name") or request.function_hint),
            "module": str(event.get("module") or "unknown"),
            "trace_metrics": event.get("metrics") if isinstance(event.get("metrics"), dict) else {},
            "critical_path_status": "not_provided",
        },
        "source": {
            "origin": "trace_investigated_source_candidate",
            "path": relative_path,
            "line_start": request.line_start,
            "line_end": request.line_end,
            "sha256": current_sha,
            "binding_level": str(current_binding.get("match_level") or "unknown"),
            "trace_anchor_source_resolution_artifact_id": source_resolution["artifact_id"],
        },
        "code": code,
        "code_language": _source_language(relative_path),
        "function_hint": request.function_hint,
        "selection": {
            "kind": "investigated_source_candidate",
            "candidate_kind": request.candidate_kind,
            "semantic_role": "candidate_compute_or_wait_core",
            "relation_to_anchor": request.relation_to_anchor,
            "rationale": request.rationale,
            "call_path": call_path,
        },
        "trace_anchor": {
            "event_name": str(event.get("event_name") or ""),
            "path": anchor_location.get("path"),
            "line": anchor_location.get("line"),
            "source_resolution_artifact_id": source_resolution["artifact_id"],
        },
        "hotspot_attribution": {
            "status": "model_selected_without_cpu_sampling",
            "trace_anchor_observed": True,
            "candidate_source_observed": True,
            "call_path_evidence_level": "source_observed" if call_path else "not_provided",
            "hotness_evidence_level": "model_hypothesis_without_cpu_sampling",
            "cpu_self_time_available": False,
            "callee_attribution_available": False,
            "reason": "The selected source exists and is related to the trace anchor by the submitted code-path evidence, but no CPU sampler attributes elapsed time to it.",
        },
    }
    artifact = store.write(
        "hotspot_code_unit",
        payload,
        source_connector_id=connector_id,
        source_binding=current_binding,
        producer={"name": "trace-hotspot-code-investigation", "version": "1.0.0"},
        input_artifacts=[
            f"artifact:{source_resolution['artifact_id']}",
            f"artifact:{source_resolution['analysis_artifact_id']}",
        ],
    )
    return {"artifact": artifact}


@app.post("/v1/projects/{project_id}/hotspot-code/paste")
def paste_hotspot_code(project_id: str, request: HotspotCodePasteRequest) -> dict[str, Any]:
    _require_project(project_id)
    connector_id = _connector_id(request.source_connector_id)
    try:
        payload = pasted_code_unit(request.code, request.function_hint, request.event_name)
    except ValueError as exc:
        raise PlatformError("HOTSPOT_CODE_INVALID", str(exc), status_code=422) from exc
    artifact = store.write(
        "hotspot_code_unit",
        payload,
        source_connector_id=connector_id,
        source_binding=_source_binding(connector_id, None),
        producer={"name": "hotspot-code-paste", "version": "1.0.0"},
    )
    return {"artifact": artifact}


@app.get("/v1/projects/{project_id}/hotspot-code/{hotspot_code_artifact_id}")
def get_hotspot_code_unit(project_id: str, hotspot_code_artifact_id: str) -> dict[str, Any]:
    """Read the immutable code unit selected by a historical analysis record."""

    _require_project(project_id)
    return {"artifact": store.read("hotspot_code_unit", hotspot_code_artifact_id)}


def _hardware_strategy_evaluation_context(case: dict[str, Any], target_platform_profile: dict[str, Any]) -> dict[str, Any]:
    """Build a fresh, oracle-free Hardware-KB context for one skill fixture."""

    optimization_kb = _optimization_kb()
    hardware_kb = _hardware_kb()
    npu_api_kb = _npu_api_kb()
    accelerator_api_kbs = _accelerator_api_kbs()
    accelerator_api_manifest = _accelerator_api_manifest()
    return prepare_context(
        "hardware_kb",
        evaluation_code_unit(case),
        [],
        [],
        _hardware_reasoning_records(),
        optimization_kb_metadata={
            "kb_id": str(optimization_kb.get("kb_id") or "unknown"),
            "version": str(optimization_kb.get("kb_version") or "unknown"),
            "content_sha256": str(optimization_kb.get("content_sha256") or ""),
        },
        hardware_kb_metadata={
            "kb_id": str(hardware_kb.get("kb_id") or "unknown"),
            "version": str(hardware_kb.get("kb_version") or "unknown"),
            "content_sha256": str(hardware_kb.get("content_sha256") or ""),
        },
        npu_api_records=_npu_api_reasoning_records(),
        npu_api_kb_metadata={
            "kb_id": str(npu_api_kb.get("kb_id") or "unknown"),
            "version": str(npu_api_kb.get("kb_version") or "unknown"),
            "content_sha256": str(npu_api_kb.get("content_sha256") or ""),
        },
        accelerator_api_records=_accelerator_api_reasoning_records(),
        accelerator_api_kb_metadata={
            unit_id: {
                "kb_id": str(payload.get("kb_id") or "unknown"),
                "version": str(payload.get("kb_version") or "unknown"),
                "content_sha256": str(payload.get("content_sha256") or ""),
            }
            for unit_id, payload in sorted(accelerator_api_kbs.items())
        },
        accelerator_api_manifest_metadata={
            "manifest_id": str(accelerator_api_manifest.get("manifest_id") or "unknown"),
            "version": str(accelerator_api_manifest.get("manifest_version") or "unknown"),
            "content_sha256": str(accelerator_api_manifest.get("content_sha256") or ""),
        },
        target_platform_profile=target_platform_profile,
        # This is the named historical static baseline evaluator.  It must not
        # be confused with the model-first production Hardware-KB chain.
        allow_legacy_static_baseline=True,
    )


def _hardware_strategy_evaluation_suite() -> dict[str, Any]:
    try:
        return load_hardware_strategy_suite()
    except ValueError as exc:
        raise PlatformError("SKILL_EVALUATION_SUITE_INVALID", str(exc), status_code=503) from exc


@app.get("/v1/skill-evaluations/hardware-kb")
def hardware_strategy_evaluation_catalog() -> dict[str, Any]:
    """List the browser-safe metadata for the Hardware-KB strategy suite."""

    return {"suite": suite_public_catalog(_hardware_strategy_evaluation_suite())}


@app.post("/v1/skill-evaluations/hardware-kb/cases/{case_id}/prepare")
def prepare_hardware_strategy_evaluation_case(case_id: str, request: HardwareStrategyEvaluationPrepareRequest) -> dict[str, Any]:
    """Build one fresh model context without revealing the evaluation oracle."""

    suite = _hardware_strategy_evaluation_suite()
    try:
        case = case_by_id(suite, case_id)
        context = _hardware_strategy_evaluation_context(case, request.target_platform_profile.model_dump(exclude_none=True))
    except ValueError as exc:
        raise PlatformError("SKILL_EVALUATION_CASE_INVALID", str(exc), status_code=422) from exc
    public_case = next(item for item in suite_public_catalog(suite)["cases"] if item["case_id"] == case_id)
    return {
        "suite_id": suite["suite_id"],
        "suite_version": suite.get("suite_version", "unknown"),
        "case": public_case,
        "context": context,
    }


@app.post("/v1/skill-evaluations/hardware-kb/cases/{case_id}/score")
def score_hardware_strategy_evaluation_case(case_id: str, request: HardwareStrategyEvaluationScoreRequest) -> dict[str, Any]:
    """Rebuild and score a browser model response against the hidden oracle."""

    suite = _hardware_strategy_evaluation_suite()
    try:
        case = case_by_id(suite, case_id)
        context = _hardware_strategy_evaluation_context(case, request.target_platform_profile.model_dump(exclude_none=True))
    except ValueError as exc:
        raise PlatformError("SKILL_EVALUATION_CASE_INVALID", str(exc), status_code=422) from exc
    if context.get("context_sha256") != request.context_sha256:
        raise PlatformError(
            "SKILL_EVALUATION_CONTEXT_STALE",
            "Evaluation context changed after preparation; rerun this case with the current KB and target profile.",
            status_code=409,
        )
    return {"score": public_score_result(score_case_result(case, context, request.result))}


@app.post("/v1/skill-evaluations/hardware-kb/report")
def save_hardware_strategy_evaluation_report(request: HardwareStrategyEvaluationReportRequest) -> dict[str, Any]:
    """Persist an auditable aggregate without model keys or raw chat prompts."""

    suite = _hardware_strategy_evaluation_suite()
    seen_case_ids: set[str] = set()
    scores: list[dict[str, Any]] = []
    profile = request.target_platform_profile.model_dump(exclude_none=True)
    for item in request.results:
        if item.case_id in seen_case_ids:
            raise PlatformError("SKILL_EVALUATION_DUPLICATE_CASE", f"Duplicate evaluation result: {item.case_id}", status_code=422)
        seen_case_ids.add(item.case_id)
        try:
            case = case_by_id(suite, item.case_id)
            context = _hardware_strategy_evaluation_context(case, profile)
        except ValueError as exc:
            raise PlatformError("SKILL_EVALUATION_CASE_INVALID", str(exc), status_code=422) from exc
        if context.get("context_sha256") != item.context_sha256:
            raise PlatformError(
                "SKILL_EVALUATION_CONTEXT_STALE",
                f"Evaluation context is stale for {item.case_id}; do not mix KB/profile versions in one report.",
                status_code=409,
            )
        scores.append(score_case_result(case, context, item.result))
    report = summarize_case_scores(suite, scores)
    report["target_platform_profile"] = profile
    report["model"] = {"provider": "browser_configured_local_relay", "name": request.model_name or "unknown"}
    public_report = public_evaluation_report(report)
    connector = _default_connector()
    artifact = store.write(
        "hardware_kb_strategy_skill_evaluation_report",
        public_report,
        source_connector_id=connector["connector_id"],
        source_binding=_source_binding(connector["connector_id"], None),
        producer={"name": "hardware-kb-strategy-evaluator", "version": "1.0.0"},
    )
    return {"artifact": artifact, "report": public_report}


def _hardware_semantic_routing_evaluation_suite() -> dict[str, Any]:
    try:
        return load_semantic_routing_suite()
    except ValueError as exc:
        raise PlatformError("HARDWARE_SEMANTIC_ROUTING_EVALUATION_SUITE_INVALID", str(exc), status_code=503) from exc


@app.get("/v1/skill-evaluations/hardware-semantic-routing")
def hardware_semantic_routing_evaluation_catalog() -> dict[str, Any]:
    """List model-routing generalization cases without their hidden oracle."""

    return {"suite": semantic_routing_suite_public_catalog(_hardware_semantic_routing_evaluation_suite())}


@app.post("/v1/skill-evaluations/hardware-semantic-routing/cases/{case_id}/prepare")
def prepare_hardware_semantic_routing_evaluation_case(
    case_id: str,
    request: HardwareSemanticRoutingEvaluationPrepareRequest,
) -> dict[str, Any]:
    suite = _hardware_semantic_routing_evaluation_suite()
    try:
        case = semantic_routing_case_by_id(suite, case_id)
        context = prepare_semantic_routing_case_context(
            case,
            _hardware_reasoning_records(),
            request.target_platform_profile.model_dump(exclude_none=True),
        )
    except ValueError as exc:
        raise PlatformError("HARDWARE_SEMANTIC_ROUTING_EVALUATION_CASE_INVALID", str(exc), status_code=422) from exc
    public_case = next(item for item in semantic_routing_suite_public_catalog(suite)["cases"] if item["case_id"] == case_id)
    return {
        "suite_id": suite["suite_id"],
        "suite_version": suite.get("suite_version", "unknown"),
        "case": public_case,
        "context": context,
    }


@app.post("/v1/skill-evaluations/hardware-semantic-routing/cases/{case_id}/score")
def score_hardware_semantic_routing_evaluation_case(
    case_id: str,
    request: HardwareSemanticRoutingEvaluationScoreRequest,
) -> dict[str, Any]:
    suite = _hardware_semantic_routing_evaluation_suite()
    try:
        case = semantic_routing_case_by_id(suite, case_id)
        context = prepare_semantic_routing_case_context(
            case,
            _hardware_reasoning_records(),
            request.target_platform_profile.model_dump(exclude_none=True),
        )
    except ValueError as exc:
        raise PlatformError("HARDWARE_SEMANTIC_ROUTING_EVALUATION_CASE_INVALID", str(exc), status_code=422) from exc
    if context.get("context_sha256") != request.context_sha256:
        raise PlatformError(
            "HARDWARE_SEMANTIC_ROUTING_EVALUATION_CONTEXT_STALE",
            "评测上下文在准备后已变化；请使用当前知识库和目标画像重新运行。",
            status_code=409,
        )
    return {"score": public_semantic_routing_score(score_semantic_routing_case_result(case, context, request.result))}


@app.post("/v1/skill-evaluations/hardware-semantic-routing/report")
def save_hardware_semantic_routing_evaluation_report(
    request: HardwareSemanticRoutingEvaluationReportRequest,
) -> dict[str, Any]:
    suite = _hardware_semantic_routing_evaluation_suite()
    profile = request.target_platform_profile.model_dump(exclude_none=True)
    seen_case_ids: set[str] = set()
    scores: list[dict[str, Any]] = []
    for item in request.results:
        if item.case_id in seen_case_ids:
            raise PlatformError("HARDWARE_SEMANTIC_ROUTING_EVALUATION_DUPLICATE_CASE", f"Duplicate evaluation result: {item.case_id}", status_code=422)
        seen_case_ids.add(item.case_id)
        try:
            case = semantic_routing_case_by_id(suite, item.case_id)
            context = prepare_semantic_routing_case_context(case, _hardware_reasoning_records(), profile)
        except ValueError as exc:
            raise PlatformError("HARDWARE_SEMANTIC_ROUTING_EVALUATION_CASE_INVALID", str(exc), status_code=422) from exc
        if context.get("context_sha256") != item.context_sha256:
            raise PlatformError(
                "HARDWARE_SEMANTIC_ROUTING_EVALUATION_CONTEXT_STALE",
                f"Evaluation context is stale for {item.case_id}; do not mix route contexts in one report.",
                status_code=409,
            )
        scores.append(score_semantic_routing_case_result(case, context, item.result))
    report = summarize_semantic_routing_case_scores(suite, scores)
    report["target_platform_profile"] = profile
    report["model"] = {"provider": "browser_configured_local_relay", "name": request.model_name or "unknown"}
    connector = _default_connector()
    artifact = store.write(
        "hardware_semantic_routing_generalization_report",
        report,
        source_connector_id=connector["connector_id"],
        source_binding=_source_binding(connector["connector_id"], None),
        producer={"name": "hardware-semantic-routing-generalization-evaluator", "version": "1.0.0"},
    )
    return {"artifact": artifact, "report": report}


@app.post("/v1/projects/{project_id}/reasoning-contexts/prepare")
def prepare_reasoning_context(project_id: str, request: ReasoningContextPrepareRequest) -> dict[str, Any]:
    _require_project(project_id)
    code_unit = store.read("hotspot_code_unit", request.code_artifact_id)
    hardware_semantic_route: dict[str, Any] | None = None
    route_context: dict[str, Any] = {}
    route_input_artifacts: list[str] = []
    if request.strategy_id == "hardware_kb":
        route_artifact_id = request.hardware_semantic_routing_result_artifact_id
        if not route_artifact_id:
            raise PlatformError(
                "HARDWARE_SEMANTIC_ROUTE_REQUIRED",
                "硬件知识库上下文需要先完成模型语义硬件路由；静态规则路由不再作为生产入口。",
                status_code=422,
            )
        route_artifact = store.read("hardware_semantic_routing_result", route_artifact_id)
        route_context_id = str(route_artifact.get("context_artifact_id") or "")
        route_context = store.read("hardware_semantic_routing_context", route_context_id)
        if str(route_context.get("code_artifact_id") or "") != request.code_artifact_id:
            raise PlatformError("HARDWARE_SEMANTIC_ROUTE_MISMATCH", "语义路由与当前热点代码不一致。", status_code=409)
        if str(route_context.get("source_connector_id") or "") != str(code_unit.get("source_connector_id") or ""):
            raise PlatformError("HARDWARE_SEMANTIC_ROUTE_MISMATCH", "语义路由与当前代码仓不一致。", status_code=409)
        route_result = route_artifact.get("result") if isinstance(route_artifact.get("result"), dict) else {}
        validation = route_result.get("validation") if isinstance(route_result.get("validation"), dict) else {}
        if str(validation.get("status") or "") != "valid":
            raise PlatformError("HARDWARE_SEMANTIC_ROUTE_INVALID", "模型语义路由未通过结构与证据校验，不能用于 API 检索。", status_code=422)
        hardware_semantic_route = route_result
        route_input_artifacts.append(f"artifact:{route_artifact_id}")
    elif request.hardware_semantic_routing_result_artifact_id:
        raise PlatformError("HARDWARE_SEMANTIC_ROUTE_UNUSED", "仅硬件知识库上下文可使用语义路由工件。", status_code=422)
    catalog = ensure_catalog(CASE_CATALOG_PATH, WORKSPACE_ROOT)
    optimization_kb = _optimization_kb()
    hardware_kb = _hardware_kb()
    npu_api_kb = _npu_api_kb()
    accelerator_api_kbs = _accelerator_api_kbs()
    accelerator_api_manifest = _accelerator_api_manifest()
    custom_catalogs = _custom_api_reasoning_catalogs() if request.strategy_id == "hardware_kb" else {}
    selected_custom_units: list[str] = []
    if hardware_semantic_route is not None:
        route_candidates = hardware_semantic_route.get("candidates") if isinstance(hardware_semantic_route.get("candidates"), list) else []
        selected_custom_units = [
            str(candidate.get("unit_id") or "").casefold()
            for candidate in route_candidates[:1]
            if isinstance(candidate, dict) and is_custom_routing_unit(str(candidate.get("unit_id") or "").casefold())
        ]
        route_catalogs = {
            str(item.get("unit_id") or ""): item
            for item in (
                route_context.get("routing_catalog_summary", {}).get("custom_catalogs", [])
                if isinstance(route_context.get("routing_catalog_summary"), dict)
                else []
            )
            if isinstance(item, dict)
        }
        for unit_id in selected_custom_units:
            current = custom_catalogs.get(unit_id)
            if current is None:
                raise PlatformError(
                    "CUSTOM_API_ROUTE_CATALOG_UNAVAILABLE",
                    "路由选中的自建 API 知识库已被删除或当前无法读取；请重新执行硬件路由。",
                    status_code=409,
                    details={"unit_id": unit_id},
                )
            expected_sha = str(route_catalogs.get(unit_id, {}).get("content_sha256") or "")
            current_sha = str(current.get("metadata", {}).get("content_sha256") or "")
            if expected_sha and current_sha != expected_sha:
                raise PlatformError(
                    "CUSTOM_API_ROUTE_CATALOG_CHANGED",
                    "路由选中的自建 API 知识库已发生变化；请重新执行硬件路由以生成新的证据快照。",
                    status_code=409,
                    details={"unit_id": unit_id},
                )
    selected_skill_profile = resolve_skill_profile(request.skill_profile_id)
    try:
        context = prepare_context(
            request.strategy_id,
            code_unit,
            _optimization_reasoning_records(),
            catalog["cases"],
            _hardware_records_with_custom_catalogs(custom_catalogs),
            optimization_kb_metadata={
                "kb_id": str(optimization_kb.get("kb_id") or "unknown"),
                "version": str(optimization_kb.get("kb_version") or "unknown"),
                "content_sha256": str(optimization_kb.get("content_sha256") or ""),
            },
            hardware_kb_metadata={
                "kb_id": str(hardware_kb.get("kb_id") or "unknown"),
                "version": str(hardware_kb.get("kb_version") or "unknown"),
                "content_sha256": str(hardware_kb.get("content_sha256") or ""),
            },
            npu_api_records=_npu_api_reasoning_records(),
            npu_api_kb_metadata={
                "kb_id": str(npu_api_kb.get("kb_id") or "unknown"),
                "version": str(npu_api_kb.get("kb_version") or "unknown"),
                "content_sha256": str(npu_api_kb.get("content_sha256") or ""),
            },
            accelerator_api_records=_accelerator_api_reasoning_records(),
            accelerator_api_kb_metadata={
                unit_id: {
                    "kb_id": str(payload.get("kb_id") or "unknown"),
                    "version": str(payload.get("kb_version") or "unknown"),
                    "content_sha256": str(payload.get("content_sha256") or ""),
                }
                for unit_id, payload in sorted(accelerator_api_kbs.items())
            },
            accelerator_api_manifest_metadata={
                "manifest_id": str(accelerator_api_manifest.get("manifest_id") or "unknown"),
                "version": str(accelerator_api_manifest.get("manifest_version") or "unknown"),
                "content_sha256": str(accelerator_api_manifest.get("content_sha256") or ""),
            },
            custom_api_records={
                unit_id: list(catalog.get("records", []))
                for unit_id, catalog in custom_catalogs.items()
                if unit_id in selected_custom_units
            },
            custom_api_kb_metadata={
                unit_id: dict(catalog.get("metadata", {}))
                for unit_id, catalog in custom_catalogs.items()
                if unit_id in selected_custom_units
            },
            target_platform_profile=request.target_platform_profile.model_dump(exclude_none=True),
            hardware_semantic_route=hardware_semantic_route,
            skill_profile=_compact_skill_profile_ref(selected_skill_profile),
        )
    except ValueError as exc:
        raise PlatformError("REASONING_CONTEXT_INVALID", str(exc), status_code=422) from exc
    artifact = store.write(
        "reasoning_context_pack",
        context,
        source_connector_id=code_unit["source_connector_id"],
        source_binding=code_unit["source_binding"],
        producer={"name": "multi-strategy-context-builder", "version": "2.1.0"},
        input_artifacts=[f"artifact:{code_unit['artifact_id']}", *route_input_artifacts],
    )
    return {"artifact": artifact, "context": context}


@app.post("/v1/projects/{project_id}/hardware-semantic-routing/prepare")
def prepare_hardware_semantic_routing(project_id: str, request: HardwareSemanticRoutingPrepareRequest) -> dict[str, Any]:
    """Prepare the bounded model input for coarse accelerator-unit routing.

    This endpoint excludes full API catalogs.  The model sees four platform
    cards plus concise cards for currently registered user-built catalogs, and
    cannot turn a broad semantic match into device or API availability.
    """

    _require_project(project_id)
    code_unit = store.read("hotspot_code_unit", request.code_artifact_id)
    selected_skill_profile = resolve_skill_profile(request.skill_profile_id)
    custom_catalogs = _custom_api_reasoning_catalogs()
    try:
        context = build_hardware_semantic_routing_context(
            code_unit,
            _hardware_records_with_custom_catalogs(custom_catalogs),
            target_platform_profile=request.target_platform_profile.model_dump(exclude_none=True),
            skill_profile=_compact_skill_profile_ref(selected_skill_profile),
        )
    except ValueError as exc:
        raise PlatformError("HARDWARE_SEMANTIC_ROUTING_CONTEXT_INVALID", str(exc), status_code=422) from exc
    artifact = store.write(
        "hardware_semantic_routing_context",
        context,
        source_connector_id=code_unit["source_connector_id"],
        source_binding=code_unit["source_binding"],
        producer={"name": "hardware-semantic-routing-context-builder", "version": "3.0.0"},
        input_artifacts=[f"artifact:{code_unit['artifact_id']}"],
    )
    return {"artifact": artifact, "context": context}


@app.get("/v1/projects/{project_id}/hardware-semantic-routing/{context_artifact_id}")
def get_hardware_semantic_routing_context(project_id: str, context_artifact_id: str) -> dict[str, Any]:
    _require_project(project_id)
    return {"artifact": store.read("hardware_semantic_routing_context", context_artifact_id)}


@app.post("/v1/projects/{project_id}/hardware-semantic-routing/results")
def save_hardware_semantic_routing_result(project_id: str, request: HardwareSemanticRoutingResultRequest) -> dict[str, Any]:
    """Validate and persist a model route without treating it as a final decision."""

    _require_project(project_id)
    context = store.read("hardware_semantic_routing_context", request.context_artifact_id)
    normalized, errors = validate_hardware_semantic_routing_result(request.result, context)
    artifact = store.write(
        "hardware_semantic_routing_result",
        {
            "context_artifact_id": context["artifact_id"],
            "context_sha256": context.get("context_sha256", ""),
            "model": {"provider": request.model_provider, "name": request.model_name},
            "raw_output_sha256": sha256_json({"raw_output": request.raw_output}),
            "raw_output": request.raw_output,
            "result": normalized,
            "validation_errors": errors,
        },
        source_connector_id=context["source_connector_id"],
        source_binding=context["source_binding"],
        producer={"name": "browser-hardware-semantic-router", "version": "1.0.0"},
        input_artifacts=[f"artifact:{context['artifact_id']}"],
    )
    return {"artifact": artifact, "validation_errors": errors}


@app.get("/v1/projects/{project_id}/hardware-semantic-routing/results/{result_artifact_id}")
def get_hardware_semantic_routing_result(project_id: str, result_artifact_id: str) -> dict[str, Any]:
    """Read an immutable routing result when restoring a historical workbench."""

    _require_project(project_id)
    return {"artifact": store.read("hardware_semantic_routing_result", result_artifact_id)}


def _manual_route_semantic_profile(model_input: dict[str, Any]) -> dict[str, list[str]]:
    """Project the neutral static profile without claiming model inference."""

    generic = _record(model_input.get("generic_code_profile"))
    structure = _record(generic.get("structure"))
    data = _record(generic.get("data_representation"))
    computation = _record(generic.get("computation"))
    dependence = _record(generic.get("dependence"))
    boundaries = _record(generic.get("boundaries"))
    numeric_types = [str(value) for value in data.get("numeric_type_classes", []) if isinstance(value, str)]
    observed_boundaries = [str(value) for value in boundaries.get("observed_categories", []) if isinstance(value, str)]
    limitations = [str(value) for value in generic.get("limitations", []) if isinstance(value, str)]
    return {
        "data_representation": [
            f"词法扫描观察到数值类型类别：{', '.join(numeric_types)}" if numeric_types else "词法扫描未确认具体数值类型",
            f"下标访问 {int(data.get('subscript_access_count') or 0)} 次，指针或引用表达式 {int(data.get('pointer_or_reference_expression_count') or 0)} 次",
        ],
        "operation_pattern": [
            f"可见循环 {int(structure.get('loop_construct_count') or 0)} 个，估计最大嵌套 {int(structure.get('max_loop_nesting_estimate') or 0)} 层",
            f"算术表达式 {int(computation.get('arithmetic_expression_count') or 0)} 个，数学函数调用 {int(computation.get('math_function_call_count') or 0)} 个",
        ],
        "dependency_and_state": [
            f"循环携带依赖：{str(dependence.get('loop_carried_dependency') or 'not_proven')}；共享状态或别名：{str(dependence.get('shared_state_or_aliasing') or 'not_proven')}"
        ],
        "boundary_conditions": [
            f"可见边界类别：{', '.join(observed_boundaries)}" if observed_boundaries else "词法扫描未观察到明确外部运行边界"
        ],
        "uncertainty": limitations[:4] or ["人工目录选择没有增加新的代码语义或设备证据"],
    }


def _save_manual_hardware_route(
    *,
    context: dict[str, Any],
    unit_id: str,
    note: str,
    parent: dict[str, Any] | None,
) -> dict[str, Any]:
    model_input = _record(context.get("model_input_context"))
    cards = model_input.get("hardware_capability_cards") if isinstance(model_input.get("hardware_capability_cards"), list) else []
    card = next(
        (
            item
            for item in cards
            if isinstance(item, dict) and str(item.get("unit_id") or "").casefold() == unit_id
        ),
        None,
    )
    if not isinstance(card, dict):
        raise PlatformError("HARDWARE_MANUAL_SELECTION_UNIT_UNKNOWN", "当前路由上下文不包含所选硬件目录。", status_code=422)
    evidence_ids = [
        value
        for value in card.get("allowed_evidence_ids", [])
        if isinstance(value, str) and value.strip()
    ]
    if not evidence_ids:
        raise PlatformError("HARDWARE_MANUAL_SELECTION_EVIDENCE_MISSING", "所选硬件目录没有可审计的能力卡证据。", status_code=422)

    parent_result = _record(parent.get("result")) if parent else {}
    parent_candidates = parent_result.get("candidates") if isinstance(parent_result.get("candidates"), list) else []
    prior_candidate = next(
        (
            item
            for item in parent_candidates
            if isinstance(item, dict) and str(item.get("unit_id") or "").casefold() == unit_id
        ),
        {},
    )
    scope_candidates = model_input.get("candidate_scopes") if isinstance(model_input.get("candidate_scopes"), list) else []
    observed_scope = next(
        (
            str(item.get("description_zh") or "").strip()
            for item in scope_candidates
            if isinstance(item, dict) and str(item.get("description_zh") or "").strip()
        ),
        "可见源码未提供可自动归纳的候选作用域；本选择只打开硬件目录供后续复核。",
    )
    observed_code_evidence = [
        value
        for value in (_record(prior_candidate).get("observed_code_evidence") or [])
        if isinstance(value, str) and value.strip()
    ][:5] or [observed_scope]
    semantic_profile = _record(parent_result.get("semantic_profile")) or _manual_route_semantic_profile(model_input)
    normalized_note = note.strip()
    label = str(card.get("label_zh") or unit_id)
    manual_candidate = {
        "unit_id": unit_id,
        "confidence": "low",
        "analysis_scope": str(_record(prior_candidate).get("analysis_scope") or observed_scope)[:420],
        "observed_code_evidence": observed_code_evidence,
        "rationale": (
            f"用户指定将当前热点送入 {label} 目录做 API 复核；"
            "该目录选择不表示模型已证明代码匹配，也不表示设备、驱动或接口可用。"
        ),
        "missing_evidence": [
            "人工目录选择不是代码语义或设备支持证据",
            "需要后续检查 API 输入输出、运行时能力、数据路径与端到端基准",
        ],
        "evidence_ids": [evidence_ids[0]],
    }
    normalized, errors = validate_hardware_semantic_routing_result(
        {
            "schema_version": "hardware_semantic_routing/v2",
            "decision": "select_units",
            "summary": f"用户选择 {label} 作为 API 目录复核入口；该选择只限定可核查范围，不构成异构适配或设备可用性结论。",
            "semantic_profile": semantic_profile,
            "candidates": [manual_candidate],
            "negative_evidence": ["人工选择不替代模型语义判断、真实设备能力或端到端测量。"],
        },
        context,
    )
    if errors:
        raise PlatformError("HARDWARE_MANUAL_SELECTION_INVALID", "人工硬件目录选择未通过内部结构校验。", status_code=422)
    normalized["selection_origin"] = "user_manual"
    normalized["selection_note"] = normalized_note
    parent_id = str(parent.get("artifact_id") or "") if parent else ""
    input_artifacts = [f"artifact:{context['artifact_id']}"]
    if parent_id:
        input_artifacts.insert(0, f"artifact:{parent_id}")
    artifact = store.write(
        "hardware_semantic_routing_result",
        {
            "context_artifact_id": context["artifact_id"],
            "context_sha256": context.get("context_sha256", ""),
            "model": _record(parent.get("model")) if parent else {},
            "raw_output_sha256": sha256_json({"manual_selection": {"unit_id": unit_id, "note": normalized_note, "parent": parent_id or None}}),
            "raw_output": "",
            "result": normalized,
            "validation_errors": [],
            "manual_selection": {
                "origin": "user_manual",
                "unit_id": unit_id,
                "note": normalized_note,
                "parent_result_artifact_id": parent_id or None,
            },
        },
        source_connector_id=context["source_connector_id"],
        source_binding=context["source_binding"],
        producer={"name": "hardware-semantic-routing-manual-directory-selector", "version": "1.1.0"},
        input_artifacts=input_artifacts,
    )
    return artifact


@app.post("/v1/projects/{project_id}/hardware-semantic-routing/direct-selection")
def save_hardware_semantic_routing_direct_selection(
    project_id: str,
    request: HardwareSemanticRoutingDirectSelectionRequest,
) -> dict[str, Any]:
    """Create an auditable user route without paying for a model route first."""

    prepared = prepare_hardware_semantic_routing(
        project_id,
        HardwareSemanticRoutingPrepareRequest(
            code_artifact_id=request.code_artifact_id,
            target_platform_profile=request.target_platform_profile,
        ),
    )
    context = prepared["artifact"]
    artifact = _save_manual_hardware_route(
        context=context,
        unit_id=request.unit_id,
        note=request.note,
        parent=None,
    )
    return {"artifact": artifact, "context_artifact": context, "validation_errors": []}


@app.post("/v1/projects/{project_id}/hardware-semantic-routing/manual-selection")
def save_hardware_semantic_routing_manual_selection(
    project_id: str,
    request: HardwareSemanticRoutingManualSelectionRequest,
) -> dict[str, Any]:
    """Persist a user-selected API directory without upgrading it to evidence.

    A manual selection is deliberately modelled as a new immutable route result.
    It reuses the original route context and its source-backed capability card,
    but carries ``selection_origin=user_manual`` so later prompt construction
    cannot describe the directory as model support or device capability.
    """

    _require_project(project_id)
    parent = store.read("hardware_semantic_routing_result", request.result_artifact_id)
    context_id = str(parent.get("context_artifact_id") or "")
    context = store.read("hardware_semantic_routing_context", context_id)
    parent_result = parent.get("result") if isinstance(parent.get("result"), dict) else {}
    parent_validation = parent_result.get("validation") if isinstance(parent_result.get("validation"), dict) else {}
    if str(parent_validation.get("status") or "") != "valid":
        raise PlatformError(
            "HARDWARE_SEMANTIC_ROUTE_INVALID",
            "需要先得到一条通过结构与证据校验的模型语义路由，才能人工选择复核目录。",
            status_code=422,
        )

    artifact = _save_manual_hardware_route(
        context=context,
        unit_id=request.unit_id,
        note=request.note,
        parent=parent,
    )
    return {"artifact": artifact, "validation_errors": []}


@app.get("/v1/projects/{project_id}/reasoning-contexts/{context_artifact_id}")
def get_reasoning_context(project_id: str, context_artifact_id: str) -> dict[str, Any]:
    """Read an immutable strategy context without rebuilding retrieval output."""

    _require_project(project_id)
    return {"artifact": store.read("reasoning_context_pack", context_artifact_id)}


def _redact_secret_fields(value: Any) -> Any:
    """Defend immutable audit artifacts from accidental credential fields."""

    if isinstance(value, dict):
        return {
            str(key): "[redacted]" if re.search(r"(?:api[_-]?key|authorization|access[_-]?token|secret|pairing[_-]?code)", str(key), re.IGNORECASE)
            else _redact_secret_fields(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_redact_secret_fields(item) for item in value]
    return value


@app.post("/v1/projects/{project_id}/reasoning-transcripts")
def save_reasoning_transcript(project_id: str, request: ReasoningTranscriptRequest) -> dict[str, Any]:
    """Persist the complete, secret-free audit trail for one model round."""

    _require_project(project_id)
    context_type = {
        "blind_baseline": "blind_reasoning_context",
        "blind_comparison": "blind_flow_comparison_context",
    }.get(request.strategy_id, "reasoning_context_pack")
    result_type = {
        "blind_baseline": "blind_opportunity_reasoning_result",
        "blind_comparison": "blind_flow_reasoning_comparison",
    }.get(request.strategy_id, "opportunity_reasoning_result")
    context = store.read(context_type, request.context_artifact_id)
    declared_strategy_id = str(context.get("strategy_id") or request.strategy_id)
    if declared_strategy_id != request.strategy_id:
        raise PlatformError(
            "REASONING_TRANSCRIPT_STRATEGY_MISMATCH",
            "Transcript strategy must match its immutable reasoning context.",
            status_code=422,
        )

    input_artifacts = [f"artifact:{context['artifact_id']}"]
    if request.result_artifact_id:
        result = store.read(result_type, request.result_artifact_id)
        if result.get("context_artifact_id") != context["artifact_id"]:
            raise PlatformError(
                "REASONING_TRANSCRIPT_RESULT_MISMATCH",
                "Transcript result must belong to the same immutable reasoning context.",
                status_code=422,
            )
        input_artifacts.append(f"artifact:{result['artifact_id']}")

    payload = _redact_secret_fields({
        "analysis_id": request.analysis_id,
        "inference_id": request.inference_id,
        "run_id": request.run_id,
        "strategy_id": request.strategy_id,
        "strategy_label": request.strategy_label,
        "model": {"provider": request.model_provider, "name": request.model_name},
        "model_usage": request.model_usage.model_dump() if request.model_usage else None,
        "token_budget": request.token_budget.model_dump() if request.token_budget else None,
        "context_artifact_id": context["artifact_id"],
        "context_sha256": context.get("context_sha256", ""),
        "target_platform_profile": context.get("target_platform_profile", _default_target_platform_profile()),
        # A self-contained copy makes a historical review robust even when the
        # viewer only knows the transcript artifact ID.
        "context_pack": context,
        "prompt": {"system": request.system_prompt, "user": request.user_prompt},
        "model_response": {
            "raw_output": request.raw_output,
            "raw_output_sha256": sha256_json({"raw_output": request.raw_output}),
        },
        "result_artifact_id": request.result_artifact_id,
        "outcome": request.outcome,
        "timeline": request.timeline,
    })
    artifact = store.write(
        "reasoning_inference_transcript",
        payload,
        source_connector_id=context["source_connector_id"],
        source_binding=context["source_binding"],
        producer={"name": "browser-reasoning-transcript-ingestion", "version": "1.0.0"},
        input_artifacts=input_artifacts,
    )
    return {"artifact": artifact}


@app.get("/v1/projects/{project_id}/reasoning-transcripts/{transcript_artifact_id}")
def get_reasoning_transcript(project_id: str, transcript_artifact_id: str) -> dict[str, Any]:
    _require_project(project_id)
    return {"artifact": store.read("reasoning_inference_transcript", transcript_artifact_id)}


@app.post("/v1/projects/{project_id}/reasoning-results")
def save_reasoning_result(project_id: str, request: ReasoningResultRequest) -> dict[str, Any]:
    _require_project(project_id)
    context = store.read("reasoning_context_pack", request.context_artifact_id)
    adapted_result, format_adaptation = adapt_reasoning_result(request.result)
    validated, errors = validate_reasoning_result(adapted_result, context)
    if format_adaptation is not None:
        validated["format_adaptation"] = format_adaptation
    artifact = store.write(
        "opportunity_reasoning_result",
        {
            "strategy_id": context["strategy_id"],
            "context_artifact_id": context["artifact_id"],
            "context_sha256": context["context_sha256"],
            "model": {"provider": request.model_provider, "name": request.model_name},
            "raw_output_sha256": sha256_json({"raw_output": request.raw_output}),
            "raw_output": request.raw_output,
            "result": validated,
            "validation_errors": errors,
        },
        source_connector_id=context["source_connector_id"],
        source_binding=context["source_binding"],
        producer={"name": "browser-model-result-ingestion", "version": "1.0.0"},
        input_artifacts=[f"artifact:{context['artifact_id']}"],
    )
    return {"artifact": artifact, "validation_errors": errors, "format_adaptation": format_adaptation}


def _compact_skill_profile_ref(profile: dict[str, Any]) -> dict[str, Any]:
    skills = profile.get("skills") if isinstance(profile.get("skills"), dict) else {}
    return {
        "profile_id": str(profile.get("profile_id") or ""),
        "profile_sha256": str(profile.get("profile_sha256") or ""),
        "user_skill_revision": int(profile.get("user_skill_revision") or 0),
        "resolution_order": profile.get("skill_resolution_order") if isinstance(profile.get("skill_resolution_order"), list) else [],
        "skills": {
            str(skill_id): {
                "version": str(item.get("version") or ""),
                "content_sha256": str(item.get("content_sha256") or ""),
                "origin": str(item.get("origin") or "server"),
                "user_version_id": str(item.get("user_version_id") or "") or None,
            }
            for skill_id, item in sorted(skills.items())
            if isinstance(item, dict)
        },
    }


def _blind_session_analysis_id(project_id: str, analysis_id: str, code_sha256: str) -> str:
    digest = hashlib.sha256(
        f"blind-session/v1:{project_id}:{analysis_id}:{code_sha256}".encode("utf-8")
    ).hexdigest()[:32]
    return f"blind-{digest}"


def _string_list(value: Any, *, limit: int, item_limit: int = 800) -> list[str]:
    if not isinstance(value, list):
        return []
    return [
        item.strip()[:item_limit]
        for item in value
        if isinstance(item, str) and item.strip()
    ][:limit]


def _normalize_blind_reasoning_result(value: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    workload = value.get("workload_characterization") if isinstance(value.get("workload_characterization"), dict) else {}
    candidates_input = value.get("candidates") if isinstance(value.get("candidates"), list) else []
    candidates: list[dict[str, Any]] = []
    allowed_kinds = {"heterogeneous", "parallel_cpu", "simd", "algorithmic"}
    allowed_units = {"npu", "gpu", "dsp", "media", "cpu", "none"}
    for index, raw in enumerate(candidates_input[:5]):
        if not isinstance(raw, dict):
            errors.append(f"candidates[{index}] 必须为对象")
            continue
        kind = str(raw.get("optimization_kind") or "").strip()
        unit = str(raw.get("hardware_unit") or "none").strip().lower()
        if kind not in allowed_kinds:
            errors.append(f"candidates[{index}].optimization_kind 无效")
            kind = "algorithmic"
        if unit not in allowed_units:
            errors.append(f"candidates[{index}].hardware_unit 无效")
            unit = "none"
        target = str(raw.get("target") or "").strip()[:500]
        rationale = str(raw.get("rationale") or "").strip()[:2400]
        if not target or not rationale:
            errors.append(f"candidates[{index}] 缺少 target 或 rationale")
        candidates.append({
            "optimization_kind": kind,
            "hardware_unit": unit,
            "target": target or "待进一步明确",
            "api_candidates": _string_list(raw.get("api_candidates"), limit=6, item_limit=300),
            "rationale": rationale or "模型没有给出可复核理由。",
            "required_conditions": _string_list(raw.get("required_conditions"), limit=10),
            "risks": _string_list(raw.get("risks"), limit=10),
            "verification_plan": _string_list(raw.get("verification_plan"), limit=10),
        })
    summary = str(value.get("summary") or "").strip()[:3000]
    preferred_path = str(value.get("preferred_path") or "").strip()[:1600]
    if not summary:
        errors.append("summary 不能为空")
    if not preferred_path:
        errors.append("preferred_path 不能为空")
    confidence = str(value.get("confidence") or "low").strip().lower()
    if confidence not in {"low", "medium", "high"}:
        errors.append("confidence 必须为 low、medium 或 high")
        confidence = "low"
    normalized = {
        "schema_version": "blind_opportunity_reasoning/v1",
        "summary": summary or "盲推理没有形成完整摘要。",
        "confidence": confidence,
        "workload_characterization": {
            "core_scope": str(workload.get("core_scope") or "").strip()[:1200],
            "data_and_memory": str(workload.get("data_and_memory") or "").strip()[:1600],
            "computation": str(workload.get("computation") or "").strip()[:1600],
            "dependency": str(workload.get("dependency") or "").strip()[:1600],
            "runtime_boundary": str(workload.get("runtime_boundary") or "").strip()[:1600],
        },
        "candidates": candidates,
        "preferred_path": preferred_path or "需要补充代码或运行时事实后再选择。",
        "unknowns": _string_list(value.get("unknowns"), limit=12),
        "claim_boundary_zh": "该结果只使用代码、用户问题与目标平台画像；API 和设备方向均为待验证的模型假设。",
        "validation": {"status": "valid" if not errors else "needs_review", "errors": errors},
    }
    return normalized, errors


@app.post("/v1/projects/{project_id}/blind-reasoning/prepare")
def prepare_blind_reasoning(project_id: str, request: BlindReasoningPrepareRequest) -> dict[str, Any]:
    """Build a minimal prompt pack with no route, KB, Trace or evidence IDs."""

    _require_project(project_id)
    code_unit = store.read("hotspot_code_unit", request.code_artifact_id)
    code = str(code_unit.get("code") or "")
    if not code.strip():
        raise PlatformError("BLIND_REASONING_CONTEXT_INVALID", "热点代码为空，无法准备盲推理。", status_code=422)
    profile = resolve_skill_profile(request.skill_profile_id)
    code_sha256 = hashlib.sha256(code.encode("utf-8")).hexdigest()
    blind_analysis_id = _blind_session_analysis_id(project_id, request.analysis_id, code_sha256)
    model_input = {
        "user_question": request.user_question,
        "target_platform_profile": request.target_platform_profile.model_dump(exclude_none=True),
        "code_language": str(code_unit.get("code_language") or "cpp"),
        "code": code,
    }
    system_prompt = skill_prompt(profile["profile_id"], "blind_reasoning")
    user_prompt = json.dumps({
        "locale": "zh-CN",
        "task": request.user_question,
        "required_schema": "blind_opportunity_reasoning/v1",
        "output_contract": {
            "schema_version": "blind_opportunity_reasoning/v1",
            "summary": "中文总体判断",
            "confidence": "low | medium | high",
            "workload_characterization": {
                "core_scope": "最小代码核心",
                "data_and_memory": "数据类型、形态和访存",
                "computation": "计算特征",
                "dependency": "依赖与可并行性",
                "runtime_boundary": "可见外部边界与未知项",
            },
            "candidates": [{
                "optimization_kind": "heterogeneous | parallel_cpu | simd | algorithmic",
                "hardware_unit": "npu | gpu | dsp | media | cpu | none",
                "target": "候选方向",
                "api_candidates": ["待验证的具体 API 或 API family"],
                "rationale": "代码到候选的因果理由",
                "required_conditions": ["成立条件"],
                "risks": ["风险"],
                "verification_plan": ["最小验证步骤"],
            }],
            "preferred_path": "首选路径与选择原因",
            "unknowns": ["缺失信息"],
        },
        "input": model_input,
    }, ensure_ascii=False, separators=(",", ":"))
    payload = {
        "schema_version": "blind_reasoning_context/v1",
        "strategy_id": "blind_baseline",
        "prompt_contract_version": "blind-opportunity-analysis/v1",
        "analysis_id": request.analysis_id,
        "blind_session_analysis_id": blind_analysis_id,
        "code_artifact_id": request.code_artifact_id,
        "code_sha256": code_sha256,
        "target_platform_profile": request.target_platform_profile.model_dump(exclude_none=True),
        "user_question": request.user_question,
        "skill_profile": _compact_skill_profile_ref(profile),
        "model_input": model_input,
        "prompt": {"system": system_prompt, "user": user_prompt},
        "context_exclusions": [
            "optimization_kb", "hardware_kb", "hardware_route", "api_retrieval",
            "trace_metrics", "source_path", "evidence_ids", "flow_reasoning_result",
        ],
    }
    payload["context_sha256"] = sha256_json({
        "strategy_id": payload["strategy_id"],
        "prompt_contract_version": payload["prompt_contract_version"],
        "skill_profile": payload["skill_profile"],
        "model_input": payload["model_input"],
        "prompt": payload["prompt"],
    })
    artifact = store.write(
        "blind_reasoning_context",
        payload,
        source_connector_id=code_unit["source_connector_id"],
        source_binding=code_unit["source_binding"],
        producer={"name": "blind-reasoning-context-builder", "version": "1.0.0"},
        input_artifacts=[f"artifact:{code_unit['artifact_id']}"],
    )
    return {"artifact": artifact, "context": payload}


@app.post("/v1/projects/{project_id}/blind-reasoning/results")
def save_blind_reasoning_result(project_id: str, request: BlindReasoningResultRequest) -> dict[str, Any]:
    _require_project(project_id)
    context = store.read("blind_reasoning_context", request.context_artifact_id)
    normalized, errors = _normalize_blind_reasoning_result(request.result)
    artifact = store.write(
        "blind_opportunity_reasoning_result",
        {
            "context_artifact_id": context["artifact_id"],
            "code_artifact_id": context["code_artifact_id"],
            "code_sha256": context["code_sha256"],
            "blind_session_analysis_id": context["blind_session_analysis_id"],
            "skill_profile": context["skill_profile"],
            "model": {"provider": request.model_provider, "name": request.model_name},
            "raw_output": request.raw_output,
            "raw_output_sha256": sha256_json({"raw_output": request.raw_output}),
            "result": normalized,
            "validation_errors": errors,
        },
        source_connector_id=context["source_connector_id"],
        source_binding=context["source_binding"],
        producer={"name": "blind-reasoning-result-ingestion", "version": "1.0.0"},
        input_artifacts=[f"artifact:{context['artifact_id']}"],
    )
    return {"artifact": artifact, "validation_errors": errors}


@app.get("/v1/projects/{project_id}/blind-reasoning/results/{result_artifact_id}")
def get_blind_reasoning_result(project_id: str, result_artifact_id: str) -> dict[str, Any]:
    _require_project(project_id)
    return {"artifact": store.read("blind_opportunity_reasoning_result", result_artifact_id)}


def _normalize_blind_flow_comparison(value: dict[str, Any]) -> tuple[dict[str, Any], list[str]]:
    errors: list[str] = []
    verdict = str(value.get("verdict") or "inconclusive").strip().lower()
    if verdict not in {"blind_better", "flow_better", "complementary", "inconclusive"}:
        errors.append("verdict 枚举无效")
        verdict = "inconclusive"
    summary = str(value.get("summary") or "").strip()[:3000]
    merged = str(value.get("merged_recommendation") or "").strip()[:3000]
    if not summary:
        errors.append("summary 不能为空")
    if not merged:
        errors.append("merged_recommendation 不能为空")
    normalized = {
        "schema_version": "blind_flow_reasoning_comparison/v1",
        "verdict": verdict,
        "summary": summary or "对照没有形成完整摘要。",
        "agreements": _string_list(value.get("agreements"), limit=12),
        "disagreements": _string_list(value.get("disagreements"), limit=12),
        "blind_strengths": _string_list(value.get("blind_strengths"), limit=10),
        "blind_weaknesses": _string_list(value.get("blind_weaknesses"), limit=10),
        "flow_strengths": _string_list(value.get("flow_strengths"), limit=10),
        "flow_weaknesses": _string_list(value.get("flow_weaknesses"), limit=10),
        "unsupported_claims": _string_list(value.get("unsupported_claims"), limit=12),
        "merged_recommendation": merged or "需要人工复核双方证据后决定。",
        "evaluation_boundary_zh": "这是同一模型对冻结盲推结果与正式流程结果的自评，只用于诊断 Skill 与知识库增益，不是真值或准确率。",
        "validation": {"status": "valid" if not errors else "needs_review", "errors": errors},
    }
    return normalized, errors


@app.post("/v1/projects/{project_id}/blind-reasoning/comparisons/prepare")
def prepare_blind_flow_comparison(project_id: str, request: BlindFlowComparisonPrepareRequest) -> dict[str, Any]:
    _require_project(project_id)
    blind = store.read("blind_opportunity_reasoning_result", request.blind_result_artifact_id)
    blind_context = store.read("blind_reasoning_context", str(blind.get("context_artifact_id") or ""))
    flow = store.read("opportunity_reasoning_result", request.flow_result_artifact_id)
    flow_context = store.read("reasoning_context_pack", str(flow.get("context_artifact_id") or ""))
    if hashlib.sha256(str(flow_context.get("code") or "").encode("utf-8")).hexdigest() != str(blind.get("code_sha256") or ""):
        raise PlatformError("BLIND_COMPARISON_INPUT_MISMATCH", "盲推理与流程推理不是同一段热点代码。", status_code=409)
    profile = resolve_skill_profile(request.skill_profile_id)
    blind_result = blind.get("result") if isinstance(blind.get("result"), dict) else {}
    flow_result = flow.get("result") if isinstance(flow.get("result"), dict) else {}
    device_run = store.read("device_remote_shell_run", request.device_run_artifact_id) if request.device_run_artifact_id else None
    device_execution = _record(device_run.get("execution")) if device_run else {}
    model_input = {
        "frozen_blind_result": blind_result,
        "flow_reasoning_result": flow_result,
        "flow_strategy_id": str(flow.get("strategy_id") or ""),
        "device_validation_result": ({
            "artifact_id": device_run.get("artifact_id"),
            "status": device_run.get("status"),
            "return_code": device_execution.get("return_code"),
            "duration_ms": device_execution.get("duration_ms"),
            "stdout": str(device_execution.get("stdout") or "")[-4000:],
            "stderr": str(device_execution.get("stderr") or "")[-2000:],
            "evidence_boundary_zh": "这是真实设备 Shell 工件的紧凑投影；零退出码不自动证明功能等价、硬件参与或性能收益。",
        } if device_run else None),
        "instruction": "保持盲推理历史判断冻结，比较流程结果与可用设备证据增加了哪些事实、约束、遗漏或偏差。",
    }
    system_prompt = skill_prompt(profile["profile_id"], "blind_comparison")
    user_prompt = json.dumps({
        "locale": "zh-CN",
        "required_schema": "blind_flow_reasoning_comparison/v1",
        "output_contract": {
            "schema_version": "blind_flow_reasoning_comparison/v1",
            "verdict": "blind_better | flow_better | complementary | inconclusive",
            "summary": "中文对照摘要",
            "agreements": ["共识"],
            "disagreements": ["关键分歧"],
            "blind_strengths": ["盲推理优势"],
            "blind_weaknesses": ["盲推理不足"],
            "flow_strengths": ["流程推理优势"],
            "flow_weaknesses": ["流程推理不足或过度约束"],
            "unsupported_claims": ["任一侧无依据或过度推断"],
            "merged_recommendation": "合并后的下一步建议",
        },
        "input": model_input,
    }, ensure_ascii=False, separators=(",", ":"))
    payload = {
        "schema_version": "blind_flow_comparison_context/v1",
        "strategy_id": "blind_comparison",
        "prompt_contract_version": "blind-flow-comparison/v1",
        "blind_result_artifact_id": blind["artifact_id"],
        "flow_result_artifact_id": flow["artifact_id"],
        "device_run_artifact_id": device_run.get("artifact_id") if device_run else None,
        "code_artifact_id": blind["code_artifact_id"],
        "code_sha256": blind["code_sha256"],
        "blind_session_analysis_id": blind["blind_session_analysis_id"],
        "target_platform_profile": blind_context.get("target_platform_profile", _default_target_platform_profile()),
        "skill_profile": _compact_skill_profile_ref(profile),
        "model_input": model_input,
        "prompt": {"system": system_prompt, "user": user_prompt},
    }
    payload["context_sha256"] = sha256_json({
        "strategy_id": payload["strategy_id"],
        "prompt_contract_version": payload["prompt_contract_version"],
        "skill_profile": payload["skill_profile"],
        "model_input": payload["model_input"],
        "prompt": payload["prompt"],
    })
    artifact = store.write(
        "blind_flow_comparison_context",
        payload,
        source_connector_id=blind["source_connector_id"],
        source_binding=blind["source_binding"],
        producer={"name": "blind-flow-comparison-context-builder", "version": "1.0.0"},
        input_artifacts=[
            f"artifact:{blind['artifact_id']}",
            f"artifact:{flow['artifact_id']}",
            *([f"artifact:{device_run['artifact_id']}"] if device_run else []),
        ],
    )
    return {"artifact": artifact, "context": payload}


@app.post("/v1/projects/{project_id}/blind-reasoning/comparisons/results")
def save_blind_flow_comparison_result(project_id: str, request: BlindFlowComparisonResultRequest) -> dict[str, Any]:
    _require_project(project_id)
    context = store.read("blind_flow_comparison_context", request.context_artifact_id)
    normalized, errors = _normalize_blind_flow_comparison(request.result)
    artifact = store.write(
        "blind_flow_reasoning_comparison",
        {
            "context_artifact_id": context["artifact_id"],
            "blind_result_artifact_id": context["blind_result_artifact_id"],
            "flow_result_artifact_id": context["flow_result_artifact_id"],
            "device_run_artifact_id": context.get("device_run_artifact_id"),
            "blind_session_analysis_id": context["blind_session_analysis_id"],
            "skill_profile": context["skill_profile"],
            "model": {"provider": request.model_provider, "name": request.model_name},
            "raw_output": request.raw_output,
            "raw_output_sha256": sha256_json({"raw_output": request.raw_output}),
            "result": normalized,
            "validation_errors": errors,
        },
        source_connector_id=context["source_connector_id"],
        source_binding=context["source_binding"],
        producer={"name": "blind-flow-comparison-result-ingestion", "version": "1.0.0"},
        input_artifacts=[f"artifact:{context['artifact_id']}"],
    )
    return {"artifact": artifact, "validation_errors": errors}


@app.get("/v1/projects/{project_id}/blind-reasoning/comparisons/{comparison_artifact_id}")
def get_blind_flow_comparison(project_id: str, comparison_artifact_id: str) -> dict[str, Any]:
    _require_project(project_id)
    return {"artifact": store.read("blind_flow_reasoning_comparison", comparison_artifact_id)}


def _manual_api_record(unit_id: str, record_id: str) -> tuple[dict[str, Any], str]:
    if is_custom_routing_unit(unit_id):
        kb_id = unit_id.removeprefix("custom:")
        detail = custom_api_kbs.record(kb_id, record_id)
        record = detail.get("record") if isinstance(detail.get("record"), dict) else None
        if not isinstance(record, dict):
            raise PlatformError("MANUAL_API_SELECTION_NOT_FOUND", "所选 API 不存在于当前自建知识库。", status_code=404)
        return record, f"custom_api_record:{record_id}"
    if unit_id == "npu":
        records = _npu_api_kb().get("records", [])
        evidence_prefix = "npu_api"
    else:
        records = _accelerator_api_kb(unit_id).get("records", [])
        evidence_prefix = "accelerator_api"
    record = next(
        (item for item in records if isinstance(item, dict) and str(item.get("record_id") or "") == record_id),
        None,
    )
    if not isinstance(record, dict):
        raise PlatformError("MANUAL_API_SELECTION_NOT_FOUND", "所选 API 不存在于当前硬件 API 知识库。", status_code=404)
    return record, f"{evidence_prefix}:{record_id}"


def _derived_reasoning_context_with_manual_api(
    base_context: dict[str, Any],
    *,
    api_record: dict[str, Any],
    evidence_id: str,
    route_artifact_id: str,
) -> dict[str, Any]:
    storage_keys = {
        "artifact_id",
        "artifact_type",
        "content_sha256",
        "created_at",
        "input_artifacts",
        "producer",
        "project_id",
        "schema_version",
        "source_binding",
        "source_connector_id",
    }
    payload = {
        key: deepcopy(value)
        for key, value in base_context.items()
        if key not in storage_keys and key != "context_sha256"
    }
    records = payload.get("context_records") if isinstance(payload.get("context_records"), list) else []
    records = [item for item in records if not (isinstance(item, dict) and item.get("evidence_id") == evidence_id)]
    records.append(
        {
            "evidence_id": evidence_id,
            "score": 0,
            "match_terms": [],
            "retrieval_rationale": {
                "method": "user_manual_api_selection/v1",
                "statement_zh": "该 API 由用户从已选硬件目录中直接指定；它不是模型匹配、设备支持或性能收益结论。",
            },
            "record": deepcopy(api_record),
        }
    )
    payload["context_records"] = records
    retrieval = _record(payload.get("retrieval"))
    payload["retrieval"] = {
        **retrieval,
        "manual_api_selection": {
            "method": "user_manual_api_selection/v1",
            "evidence_id": evidence_id,
            "route_artifact_id": route_artifact_id,
            "statement_zh": "用户直接指定 API，后续真机验证仍需证明构建、运行、功能等价和目标单元实际参与。",
        },
    }
    payload["model_input_context"] = build_model_input_context(payload)
    payload["context_sha256"] = canonical_sha256(payload)
    return payload


def _manual_selection_scope(context: dict[str, Any]) -> dict[str, str]:
    model_input = _record(context.get("model_input_context"))
    scopes = model_input.get("candidate_scopes") if isinstance(model_input.get("candidate_scopes"), list) else []
    first = next((item for item in scopes if isinstance(item, dict)), {})
    level = str(first.get("level") or "unknown")
    if level not in {"function", "loop_region", "data_pipeline", "callee", "unknown"}:
        level = "unknown"
    resolution = str(first.get("resolution_status") or first.get("status") or "")
    status = "observed" if resolution == "observed" and level != "unknown" else "not_declared"
    description = str(first.get("description_zh") or first.get("description") or "用户尚未为该优化方向补充更小的源码作用域。").strip()
    return {
        "level": level,
        "status": status,
        "description": description[:600],
        "source_evidence": f"人工选择沿用热点代码上下文中的可见范围：{description[:420]}",
    }


@app.post("/v1/projects/{project_id}/manual-opportunity-selections")
def save_manual_opportunity_selection(project_id: str, request: ManualOpportunitySelectionRequest) -> dict[str, Any]:
    """Persist a user-selected API or CPU-parallel direction for device validation."""

    strategy_id = "hardware_kb" if request.selection_kind == "hardware_api" else "direct_baseline"
    prepared = prepare_reasoning_context(
        project_id,
        ReasoningContextPrepareRequest(
            code_artifact_id=request.code_artifact_id,
            strategy_id=strategy_id,
            hardware_semantic_routing_result_artifact_id=request.hardware_semantic_routing_result_artifact_id,
            target_platform_profile=request.target_platform_profile,
        ),
    )
    base_context = prepared["artifact"]
    context = base_context
    api_record: dict[str, Any] | None = None
    api_evidence_id = ""
    if request.selection_kind == "hardware_api":
        assert request.unit_id is not None
        assert request.api_record_id is not None
        assert request.hardware_semantic_routing_result_artifact_id is not None
        api_record, api_evidence_id = _manual_api_record(request.unit_id, request.api_record_id)
        context_payload = _derived_reasoning_context_with_manual_api(
            base_context,
            api_record=api_record,
            evidence_id=api_evidence_id,
            route_artifact_id=request.hardware_semantic_routing_result_artifact_id,
        )
        context = store.write(
            "reasoning_context_pack",
            context_payload,
            source_connector_id=base_context["source_connector_id"],
            source_binding=base_context["source_binding"],
            producer={"name": "manual-api-selection-context-builder", "version": "1.0.0"},
            input_artifacts=[
                f"artifact:{base_context['artifact_id']}",
                f"artifact:{request.hardware_semantic_routing_result_artifact_id}",
            ],
        )

    source_sha = str(_record(context.get("source")).get("sha256") or "")
    event_id = str(_record(context.get("hotspot")).get("event_id") or "")
    evidence_ids = [value for value in (api_evidence_id, f"trace:{event_id}", f"source:{source_sha}") if value]
    scope = _manual_selection_scope(context)
    note = request.note.strip()
    if request.selection_kind == "parallel_cpu":
        target = "CPU 并行优化"
        summary = "用户选择直接验证 CPU 并行方向；该选择跳过硬件机会推理，不代表迭代独立性、正确性或性能收益已经成立。"
        rationale = "用户希望以当前可见热点为对象设计 CPU 并行对照实验；需要在真机上验证任务切分、数据依赖、同步成本、结果一致性与实际粒度。"
        required_conditions = ["确认迭代或任务之间不存在未处理的数据依赖", "定义串行 reference 与确定性结果比较器", "调度和同步成本低于可并行工作量"]
        risks = ["共享状态、别名或归约可能破坏结果正确性", "小任务的线程调度与同步开销可能抵消收益"]
        verification_plan = ["建立串行 reference 与 CPU 并行实现", "在目标手机比较结果一致性、延迟和重复运行稳定性", "使用代表性输入评估线程数与任务粒度"]
        selected_unit = "parallel_cpu"
    else:
        assert request.unit_id is not None and api_record is not None
        target = str(api_record.get("symbol") or api_record.get("name_zh") or api_record.get("name") or api_record.get("record_id") or request.api_record_id)
        abstract = str(api_record.get("one_line_abstract_zh") or "该 API 的适配性仍需通过真实输入输出合同验证。")
        summary = f"用户直接选择 {target} 作为 {request.unit_id.upper()} 优化方向；该选择跳过模型机会推理，不构成 API 适配或设备可用性结论。"
        rationale = f"用户从已选 {request.unit_id.upper()} 目录指定此 API：{abstract}"
        required_conditions = ["核对热点输入输出、数据格式和所选 API 合同", "确认目标系统可构建并在连接设备上发现所需运行时能力", "保留 CPU/software fallback 与结果比较器"]
        risks = ["用户选择的 API 可能与实际代码语义或暴露层级不相容", "数据转换、搬运、同步或小批量成本可能抵消收益"]
        runtime_checks = [value for value in api_record.get("runtime_verification", []) if isinstance(value, str) and value.strip()]
        verification_plan = runtime_checks[:4] or ["在目标设备上完成构建、运行时能力查询、功能等价与端到端基准"]
        selected_unit = request.unit_id

    manual_result = {
        "schema_version": "opportunity_reasoning/v1",
        "recommendation": "evidence_required",
        "confidence": "low",
        "evidence_status": "insufficient_evidence",
        "summary": summary,
        "opportunities": [{
            "opportunity_kind": "parallel_cpu" if request.selection_kind == "parallel_cpu" else "heterogeneous",
            "candidate_target": target,
            "analysis_scope": scope,
            "rationale": rationale,
            "required_conditions": required_conditions,
            "risks": risks,
            "verification_plan": verification_plan,
            "implementation_sketch": note,
            "evidence_ids": evidence_ids,
        }],
        "missing_evidence": required_conditions,
        "negative_evidence": ["该方向由用户直接选择，尚未经过模型机会推理或真机实测。"],
        "selection_origin": "user_manual",
    }
    validated, errors = validate_reasoning_result(manual_result, context)
    if errors:
        raise PlatformError("MANUAL_OPPORTUNITY_SELECTION_INVALID", "直接优化方向未通过内部结构校验。", status_code=422, details={"errors": errors})
    manual_selection = {
        "origin": "user_manual",
        "selection_kind": request.selection_kind,
        "selected_unit": selected_unit,
        "label": "并行优化" if request.selection_kind == "parallel_cpu" else target,
        "note": note,
        "route_artifact_id": request.hardware_semantic_routing_result_artifact_id,
        "api_record_id": request.api_record_id,
        "api_evidence_id": api_evidence_id or None,
        "api_record_snapshot": {
            key: deepcopy(api_record[key])
            for key in ("record_id", "record_type", "symbol", "name", "name_zh", "one_line_abstract_zh", "detailed_description_zh", "semantic_family", "capability_boundary", "runtime_verification", "source_contract")
            if api_record is not None and key in api_record
        },
    }
    artifact = store.write(
        "opportunity_reasoning_result",
        {
            "strategy_id": strategy_id,
            "context_artifact_id": context["artifact_id"],
            "context_sha256": context["context_sha256"],
            "model": {"provider": "user_manual", "name": "direct_selection"},
            "raw_output_sha256": sha256_json({"manual_selection": manual_selection}),
            "raw_output": "",
            "result": validated,
            "validation_errors": [],
            "manual_selection": manual_selection,
        },
        source_connector_id=context["source_connector_id"],
        source_binding=context["source_binding"],
        producer={"name": "manual-opportunity-selector", "version": "1.0.0"},
        input_artifacts=[f"artifact:{context['artifact_id']}", f"artifact:{request.code_artifact_id}"],
    )
    return {"artifact": artifact, "context_artifact": context, "validation_errors": []}


@app.get("/v1/projects/{project_id}/reasoning-results/{result_artifact_id}")
def get_reasoning_result(project_id: str, result_artifact_id: str) -> dict[str, Any]:
    """Read a validated immutable reasoning result for historical restoration."""

    _require_project(project_id)
    return {"artifact": store.read("opportunity_reasoning_result", result_artifact_id)}


@app.get("/v1/device-verification/devices")
def list_device_verification_devices() -> dict[str, Any]:
    """List local HDC targets without changing a connected device."""

    return discover_devices(WORKSPACE_ROOT)


@app.post("/v1/projects/{project_id}/device-probes")
def create_device_probe(project_id: str, request: DeviceProbeRequest) -> dict[str, Any]:
    """Persist a fixed, read-only HDC capability observation."""

    _require_project(project_id)
    connector_id = _connector_id(request.source_connector_id)
    payload = probe_device(WORKSPACE_ROOT, request.device_serial)
    artifact = store.write(
        "device_probe",
        payload,
        source_connector_id=connector_id,
        source_binding=_source_binding(connector_id, None),
        producer={"name": "hdc-readonly-capability-probe", "version": "1.0.0"},
    )
    return {"artifact": artifact}


@app.get("/v1/projects/{project_id}/device-probes/{probe_artifact_id}")
def get_device_probe(project_id: str, probe_artifact_id: str) -> dict[str, Any]:
    _require_project(project_id)
    return {"artifact": store.read("device_probe", probe_artifact_id)}


def _device_validation_reasoning_context(code_unit: dict[str, Any], reasoning_result: dict[str, Any]) -> dict[str, Any]:
    """Load the immutable strategy context and enforce the code binding once.

    This guards artifact mix-ups only.  It deliberately does not compare code
    bytes against a reference sample; semantic dispatch eligibility is decided
    by the model contract created from these artifacts.
    """

    context_id = str(reasoning_result.get("context_artifact_id") or "")
    if not context_id:
        raise PlatformError(
            "DEVICE_VALIDATION_REASONING_CONTEXT_MISSING",
            "所选策略结果缺少可追溯的推理上下文，不能生成真机验证方案。",
            status_code=409,
        )
    context = store.read("reasoning_context_pack", context_id)
    code_reference = f"artifact:{code_unit['artifact_id']}"
    if code_reference not in context.get("input_artifacts", []):
        raise PlatformError(
            "DEVICE_VALIDATION_ANALYSIS_MISMATCH",
            "所选策略结果并非针对当前热点代码工件，不能生成真机验证方案。",
            status_code=409,
        )
    if reasoning_result.get("source_connector_id") != code_unit.get("source_connector_id"):
        raise PlatformError(
            "DEVICE_VALIDATION_CONNECTOR_MISMATCH",
            "策略结果与热点代码来自不同代码仓，不能生成真机验证方案。",
            status_code=409,
        )
    return context


@app.post("/v1/projects/{project_id}/device-dispatch-contexts/prepare")
def prepare_device_dispatch_context(project_id: str, request: DeviceDispatchPrepareRequest) -> dict[str, Any]:
    """Prepare the model-owned semantic dispatch assessment context.

    The endpoint is side-effect free with respect to the device.  It stores a
    compact evidence pack; the browser retains model credentials and performs
    the model call.
    """

    _require_project(project_id)
    probe = store.read("device_probe", request.device_probe_artifact_id)
    code_unit = store.read("hotspot_code_unit", request.hotspot_code_artifact_id)
    reasoning_result = store.read("opportunity_reasoning_result", request.reasoning_result_artifact_id)
    reasoning_context = _device_validation_reasoning_context(code_unit, reasoning_result)
    try:
        payload = build_device_dispatch_context(
            code_artifact=code_unit,
            reasoning_result_artifact=reasoning_result,
            reasoning_context_artifact=reasoning_context,
            probe_artifact=probe,
            requested_unit=request.requested_unit,
        )
    except ValueError as exc:
        raise PlatformError("DEVICE_DISPATCH_CONTEXT_INVALID", str(exc), status_code=422) from exc
    artifact = store.write(
        "device_dispatch_context",
        payload,
        source_connector_id=str(code_unit["source_connector_id"]),
        source_binding=code_unit["source_binding"],
        producer={"name": "model-first-device-dispatch-context-builder", "version": "1.0.0"},
        input_artifacts=[
            f"artifact:{probe['artifact_id']}",
            f"artifact:{code_unit['artifact_id']}",
            f"artifact:{reasoning_result['artifact_id']}",
            f"artifact:{reasoning_context['artifact_id']}",
        ],
    )
    return {"artifact": artifact, "context": payload}


@app.get("/v1/projects/{project_id}/device-dispatch-contexts/{context_artifact_id}")
def get_device_dispatch_context(project_id: str, context_artifact_id: str) -> dict[str, Any]:
    _require_project(project_id)
    return {"artifact": store.read("device_dispatch_context", context_artifact_id)}


@app.post("/v1/projects/{project_id}/device-dispatch-assessments")
def save_device_dispatch_assessment(project_id: str, request: DeviceDispatchAssessmentRequest) -> dict[str, Any]:
    """Validate and persist the model's semantic dispatch contract."""

    _require_project(project_id)
    context = store.read("device_dispatch_context", request.context_artifact_id)
    if request.parent_assessment_artifact_id:
        parent = store.read("device_dispatch_assessment", request.parent_assessment_artifact_id)
        if str(parent.get("context_artifact_id") or "") != str(context["artifact_id"]):
            raise PlatformError("DEVICE_DISPATCH_PARENT_CONTEXT_MISMATCH", "复核判断必须引用同一份下发判断上下文。", status_code=409)
    normalized, errors = validate_device_dispatch_assessment(request.result, context)
    artifact = store.write(
        "device_dispatch_assessment",
        {
            "context_artifact_id": context["artifact_id"],
            "context_sha256": context.get("context_sha256", ""),
            "code_artifact_id": context.get("code_artifact_id"),
            "reasoning_result_artifact_id": context.get("reasoning_result_artifact_id"),
            "probe_artifact_id": context.get("probe_artifact_id"),
            "requested_unit": context.get("requested_unit"),
            "model": {"provider": request.model_provider, "name": request.model_name},
            "raw_output_sha256": sha256_json({"raw_output": request.raw_output}),
            "raw_output": request.raw_output,
            "iteration": {
                "attempt_index": request.attempt_index,
                "parent_assessment_artifact_id": request.parent_assessment_artifact_id,
                "agent_note": request.agent_note,
                "agent_note_sha256": sha256_json({"agent_note": request.agent_note}),
            },
            "result": normalized,
            "validation_errors": errors,
        },
        source_connector_id=str(context["source_connector_id"]),
        source_binding=context["source_binding"],
        producer={"name": "browser-model-device-dispatch-assessor", "version": "1.2.0"},
        input_artifacts=[f"artifact:{context['artifact_id']}"],
    )
    return {"artifact": artifact, "validation_errors": errors}


@app.get("/v1/projects/{project_id}/device-dispatch-assessments/{assessment_artifact_id}")
def get_device_dispatch_assessment(project_id: str, assessment_artifact_id: str) -> dict[str, Any]:
    _require_project(project_id)
    return {"artifact": store.read("device_dispatch_assessment", assessment_artifact_id)}


@app.post("/v1/projects/{project_id}/device-adapter-contexts/prepare")
def prepare_device_adapter_context(project_id: str, request: DeviceAdapterContextPrepareRequest) -> dict[str, Any]:
    """Prepare a model context for one template-materialized Adapter attempt.

    This endpoint only creates an immutable context artifact.  It never writes
    source code, compiles, or touches the connected device.
    """

    _require_project(project_id)
    probe = store.read("device_probe", request.device_probe_artifact_id)
    code_unit = store.read("hotspot_code_unit", request.hotspot_code_artifact_id)
    assessment = store.read("device_dispatch_assessment", request.dispatch_assessment_artifact_id)
    if str(assessment.get("code_artifact_id") or "") != str(code_unit["artifact_id"]):
        raise PlatformError("DEVICE_ADAPTER_CODE_MISMATCH", "模型下发判断不属于当前热点代码，不能生成 Adapter。", status_code=409)
    if str(assessment.get("probe_artifact_id") or "") != str(probe["artifact_id"]):
        raise PlatformError("DEVICE_ADAPTER_PROBE_MISMATCH", "模型下发判断不属于当前设备探测，不能生成 Adapter。", status_code=409)
    if str(assessment.get("requested_unit") or "") != request.requested_unit:
        raise PlatformError("DEVICE_ADAPTER_UNIT_MISMATCH", "模型下发判断对应的硬件单元与 Adapter 目标不一致。", status_code=409)
    try:
        payload = build_device_adapter_context(
            code_artifact=code_unit,
            dispatch_assessment_artifact=assessment,
            probe_artifact=probe,
            requested_unit=request.requested_unit,
        )
    except ValueError as exc:
        raise PlatformError("DEVICE_ADAPTER_CONTEXT_INVALID", str(exc), status_code=422) from exc
    artifact = store.write(
        "device_adapter_context",
        payload,
        source_connector_id=str(code_unit["source_connector_id"]),
        source_binding=code_unit["source_binding"],
        producer={"name": "template-device-adapter-context-builder", "version": "1.0.0"},
        input_artifacts=[
            f"artifact:{probe['artifact_id']}",
            f"artifact:{code_unit['artifact_id']}",
            f"artifact:{assessment['artifact_id']}",
        ],
    )
    return {"artifact": artifact, "context": payload}


@app.get("/v1/projects/{project_id}/device-adapter-contexts/{context_artifact_id}")
def get_device_adapter_context(project_id: str, context_artifact_id: str) -> dict[str, Any]:
    _require_project(project_id)
    return {"artifact": store.read("device_adapter_context", context_artifact_id)}


@app.post("/v1/projects/{project_id}/device-adapters")
def save_device_adapter(project_id: str, request: DeviceAdapterSpecRequest) -> dict[str, Any]:
    """Validate a model specification and materialize one bounded source file."""

    _require_project(project_id)
    context = store.read("device_adapter_context", request.context_artifact_id)
    if request.parent_adapter_artifact_id:
        parent = store.read("generated_device_adapter", request.parent_adapter_artifact_id)
        if str(parent.get("context_artifact_id") or "") != str(context["artifact_id"]):
            raise PlatformError("DEVICE_ADAPTER_PARENT_CONTEXT_MISMATCH", "后续 Adapter 尝试必须引用同一份 Adapter 上下文。", status_code=409)
    normalized, errors = validate_device_adapter_spec(request.result, context)
    materialization = materialize_device_adapter(
        WORKSPACE_ROOT,
        STATE_ROOT,
        context=context,
        spec=normalized,
    )
    artifact = store.write(
        "generated_device_adapter",
        {
            "context_artifact_id": context["artifact_id"],
            "context_sha256": context.get("context_sha256", ""),
            "code_artifact_id": context.get("code_artifact_id"),
            "dispatch_assessment_artifact_id": context.get("dispatch_assessment_artifact_id"),
            "probe_artifact_id": context.get("probe_artifact_id"),
            "requested_unit": context.get("requested_unit"),
            "model": {"provider": request.model_provider, "name": request.model_name},
            "raw_output_sha256": sha256_json({"raw_output": request.raw_output}),
            "raw_output": request.raw_output,
            "iteration": {
                "attempt_index": request.attempt_index,
                "parent_adapter_artifact_id": request.parent_adapter_artifact_id,
                "agent_note": request.agent_note,
                "agent_note_sha256": sha256_json({"agent_note": request.agent_note}),
            },
            "result": normalized,
            "validation_errors": errors,
            "materialization": materialization,
        },
        source_connector_id=str(context["source_connector_id"]),
        source_binding=context["source_binding"],
        producer={"name": "browser-model-template-device-adapter", "version": "1.0.0"},
        input_artifacts=[f"artifact:{context['artifact_id']}"],
    )
    return {"artifact": artifact, "validation_errors": errors}


@app.get("/v1/projects/{project_id}/device-adapters/{adapter_artifact_id}")
def get_device_adapter(project_id: str, adapter_artifact_id: str) -> dict[str, Any]:
    _require_project(project_id)
    return {"artifact": store.read("generated_device_adapter", adapter_artifact_id)}


@app.post("/v1/projects/{project_id}/device-adapter-runs")
def run_device_adapter(project_id: str, request: DeviceAdapterRunRequest) -> dict[str, Any]:
    """Run one immutable template-generated Adapter without an extra click.

    The project default is explicit pre-authorization from this local single
    user workbench.  The runner still has no browser-provided command, path or
    compiler arguments: it reads only an immutable Adapter artifact.
    """

    _require_project(project_id)
    adapter = store.read("generated_device_adapter", request.adapter_artifact_id)
    probe_artifact_id = str(adapter.get("probe_artifact_id") or "")
    if not probe_artifact_id:
        raise PlatformError("DEVICE_ADAPTER_PROBE_MISSING", "生成 Adapter 缺少关联设备探测工件。", status_code=409)
    probe = store.read("device_probe", probe_artifact_id)
    payload = run_generated_device_adapter(
        WORKSPACE_ROOT,
        STATE_ROOT,
        adapter_artifact=adapter,
        probe_artifact=probe,
    )
    payload["execution_authorization"] = request.execution_authorization
    artifact = store.write(
        "generated_device_adapter_run",
        payload,
        source_connector_id=str(adapter["source_connector_id"]),
        source_binding=adapter["source_binding"],
        producer={"name": "template-device-adapter-runner", "version": "1.0.0"},
        input_artifacts=[f"artifact:{adapter['artifact_id']}", f"artifact:{probe['artifact_id']}"],
    )
    return {"artifact": artifact}


@app.get("/v1/projects/{project_id}/device-adapter-runs/{run_artifact_id}")
def get_device_adapter_run(project_id: str, run_artifact_id: str) -> dict[str, Any]:
    _require_project(project_id)
    return {"artifact": store.read("generated_device_adapter_run", run_artifact_id)}


@app.post("/v1/projects/{project_id}/device-shell-runs")
def run_device_shell(project_id: str, request: DeviceRemoteShellRunRequest) -> dict[str, Any]:
    """Run one Agent-selected command on the already probed remote device.

    The command is intentionally device-side only.  The endpoint resolves HDC
    and the serial from server-owned state and never passes the command through
    a workstation shell or accepts a workstation executable/path from the
    browser.
    """

    _require_project(project_id)
    probe = store.read("device_probe", request.device_probe_artifact_id)
    payload = execute_remote_device_shell(
        WORKSPACE_ROOT,
        probe_artifact=probe,
        command=request.command,
        timeout_seconds=request.timeout_seconds,
    )
    payload["execution_authorization"] = request.execution_authorization
    artifact = store.write(
        "device_remote_shell_run",
        payload,
        source_connector_id=str(probe["source_connector_id"]),
        source_binding=probe["source_binding"],
        producer={"name": "page-agent-remote-device-shell", "version": "1.0.0"},
        input_artifacts=[f"artifact:{probe['artifact_id']}"],
    )
    return {"artifact": artifact}


@app.get("/v1/projects/{project_id}/device-shell-runs/{run_artifact_id}")
def get_device_shell_run(project_id: str, run_artifact_id: str) -> dict[str, Any]:
    _require_project(project_id)
    return {"artifact": store.read("device_remote_shell_run", run_artifact_id)}


@app.get("/v1/device-experiment-profiles")
def get_device_experiment_profiles(
    profile_id: Literal["native_c", "native_cpp", "gpu_gles3", "gpu_vulkan", "npu_nnrt", "media_avcodec"] | None = None,
    header_query: str = "",
) -> dict[str, Any]:
    """Return the real fixed-root header and runtime surface available to the Agent."""

    return {
        "profiles": experiment_profile_catalog(
            WORKSPACE_ROOT,
            state_root=STATE_ROOT,
            profile_id=profile_id,
            header_query=header_query,
        ),
    }


@app.post("/v1/projects/{project_id}/device-experiment-sources")
def stage_agent_device_experiment_source(project_id: str, request: DeviceExperimentSourceRequest) -> dict[str, Any]:
    """Stage complete Agent-authored source below the platform artifact root."""

    _require_project(project_id)
    probe = store.read("device_probe", request.device_probe_artifact_id)
    payload = stage_device_experiment_source_payload(
        WORKSPACE_ROOT,
        STATE_ROOT,
        probe_artifact=probe,
        profile_id=request.profile_id,
        title=request.title,
        language=request.language,
        source=request.source,
        shader_source=request.shader_source,
    )
    payload["execution_authorization"] = request.execution_authorization
    artifact = store.write(
        "agent_device_experiment_source",
        payload,
        source_connector_id=str(probe["source_connector_id"]),
        source_binding=probe["source_binding"],
        producer={"name": "page-agent-device-experiment-stager", "version": "1.0.0"},
        input_artifacts=[f"artifact:{probe['artifact_id']}"],
    )
    return {"artifact": artifact}


@app.get("/v1/projects/{project_id}/device-experiment-sources/{source_artifact_id}")
def get_device_experiment_source(project_id: str, source_artifact_id: str) -> dict[str, Any]:
    _require_project(project_id)
    return {"artifact": store.read("agent_device_experiment_source", source_artifact_id)}


@app.post("/v1/projects/{project_id}/device-experiment-runs")
def build_and_upload_agent_device_experiment(project_id: str, request: DeviceExperimentBuildRequest) -> dict[str, Any]:
    """Build a staged source with a fixed profile and upload its binary.

    The endpoint deliberately stops after HDC upload.  The Page Agent uses the
    separate probe-bound remote-shell action in the next turn to choose the
    actual device command, arguments, diagnostics and cleanup.
    """

    _require_project(project_id)
    probe = store.read("device_probe", request.device_probe_artifact_id)
    staged_source = store.read("agent_device_experiment_source", request.staged_source_artifact_id)
    payload = build_and_upload_device_experiment_payload(
        WORKSPACE_ROOT,
        STATE_ROOT,
        probe_artifact=probe,
        staged_source_artifact=staged_source,
        timeout_seconds=request.timeout_seconds,
    )
    payload["execution_authorization"] = request.execution_authorization
    artifact = store.write(
        "agent_device_experiment_run",
        payload,
        source_connector_id=str(staged_source["source_connector_id"]),
        source_binding=staged_source["source_binding"],
        producer={"name": "page-agent-fixed-profile-device-builder", "version": "1.0.0"},
        input_artifacts=[f"artifact:{probe['artifact_id']}", f"artifact:{staged_source['artifact_id']}"],
    )
    return {"artifact": artifact}


@app.get("/v1/projects/{project_id}/device-experiment-runs/{run_artifact_id}")
def get_device_experiment_run(project_id: str, run_artifact_id: str) -> dict[str, Any]:
    _require_project(project_id)
    return {"artifact": store.read("agent_device_experiment_run", run_artifact_id)}


@app.post("/v1/projects/{project_id}/device-validation-plans")
def create_device_validation_plan(project_id: str, request: DeviceValidationPlanRequest) -> dict[str, Any]:
    """Create an immutable plan. Planning never sends a command to the device."""

    _require_project(project_id)
    probe = store.read("device_probe", request.device_probe_artifact_id)
    code_unit = store.read("hotspot_code_unit", request.hotspot_code_artifact_id)
    reasoning_result = store.read("opportunity_reasoning_result", request.reasoning_result_artifact_id)
    _device_validation_reasoning_context(code_unit, reasoning_result)
    dispatch_assessment: dict[str, Any] | None = None
    if request.dispatch_assessment_artifact_id:
        dispatch_assessment = store.read("device_dispatch_assessment", request.dispatch_assessment_artifact_id)
        if str(dispatch_assessment.get("code_artifact_id") or "") != str(code_unit["artifact_id"]):
            raise PlatformError("DEVICE_DISPATCH_CODE_MISMATCH", "模型下发判断不属于当前热点代码，不能用于生成方案。", status_code=409)
        if str(dispatch_assessment.get("reasoning_result_artifact_id") or "") != str(reasoning_result["artifact_id"]):
            raise PlatformError("DEVICE_DISPATCH_REASONING_MISMATCH", "模型下发判断不属于所选策略结果，不能用于生成方案。", status_code=409)
        if str(dispatch_assessment.get("probe_artifact_id") or "") != str(probe["artifact_id"]):
            raise PlatformError("DEVICE_DISPATCH_PROBE_MISMATCH", "模型下发判断不属于当前设备探测，不能用于生成方案。", status_code=409)
        if str(dispatch_assessment.get("requested_unit") or "") != request.requested_unit:
            raise PlatformError("DEVICE_DISPATCH_UNIT_MISMATCH", "模型下发判断对应的硬件单元与当前选择不一致。", status_code=409)
    payload = build_validation_plan(
        WORKSPACE_ROOT,
        probe_artifact=probe,
        code_artifact=code_unit,
        reasoning_result_artifact=reasoning_result,
        requested_unit=request.requested_unit,
        dispatch_assessment_artifact=dispatch_assessment,
    )
    artifact = store.write(
        "device_validation_plan",
        payload,
        source_connector_id=str(code_unit["source_connector_id"]),
        source_binding=code_unit["source_binding"],
        producer={"name": "model-first-device-validation-planner", "version": "2.0.0"},
        input_artifacts=[
            f"artifact:{probe['artifact_id']}",
            f"artifact:{code_unit['artifact_id']}",
            f"artifact:{reasoning_result['artifact_id']}",
            *([f"artifact:{dispatch_assessment['artifact_id']}"] if dispatch_assessment else []),
        ],
    )
    return {"artifact": artifact}


@app.get("/v1/projects/{project_id}/device-validation-plans/{plan_artifact_id}")
def get_device_validation_plan(project_id: str, plan_artifact_id: str) -> dict[str, Any]:
    _require_project(project_id)
    return {"artifact": store.read("device_validation_plan", plan_artifact_id)}


@app.post("/v1/projects/{project_id}/device-validation-runs")
def run_device_validation(project_id: str, request: DeviceValidationRunRequest) -> dict[str, Any]:
    """Run one allow-listed recipe with either UI or full-access Agent authority."""

    _require_project(project_id)
    plan = store.read("device_validation_plan", request.plan_artifact_id)
    agent_authorized = request.execution_authorization == "agent_full_access"
    confirmation = plan.get("confirmation") if isinstance(plan.get("confirmation"), dict) else {}
    user_confirmed = request.user_confirmed or agent_authorized
    confirmation_nonce = request.confirmation_nonce or (str(confirmation.get("nonce") or "") if agent_authorized else "")
    payload = execute_validation_plan(
        WORKSPACE_ROOT,
        STATE_ROOT,
        plan_artifact=plan,
        user_confirmed=user_confirmed,
        confirmation_nonce=confirmation_nonce,
    )
    payload["execution_authorization"] = request.execution_authorization
    artifact = store.write(
        "device_validation_run",
        payload,
        source_connector_id=str(plan["source_connector_id"]),
        source_binding=plan["source_binding"],
        producer={"name": "registered-device-validation-runner", "version": "1.1.0"},
        input_artifacts=[f"artifact:{plan['artifact_id']}"],
    )
    return {"artifact": artifact}


@app.get("/v1/projects/{project_id}/device-validation-runs/{run_artifact_id}")
def get_device_validation_run(project_id: str, run_artifact_id: str) -> dict[str, Any]:
    _require_project(project_id)
    return {"artifact": store.read("device_validation_run", run_artifact_id)}


@app.post("/v1/projects/{project_id}/reasoning-comparisons")
def compare_reasoning_results(project_id: str, request: ReasoningComparisonRequest) -> dict[str, Any]:
    _require_project(project_id)
    artifacts = [store.read("opportunity_reasoning_result", artifact_id) for artifact_id in request.result_artifact_ids]
    strategies = [str(artifact.get("strategy_id")) for artifact in artifacts]
    if len(strategies) != len(set(strategies)) or any(strategy not in STRATEGIES for strategy in strategies):
        raise PlatformError("REASONING_COMPARISON_INVALID", "Provide at most one result for each supported strategy.", status_code=422)
    comparison = compare_results(artifacts)
    anchor = artifacts[0]
    artifact = store.write(
        "opportunity_reasoning_comparison",
        comparison,
        source_connector_id=anchor["source_connector_id"],
        source_binding=anchor["source_binding"],
        producer={"name": "multi-strategy-comparison", "version": "1.0.0"},
        input_artifacts=[f"artifact:{item['artifact_id']}" for item in artifacts],
    )
    return {"artifact": artifact}


@app.post("/v1/projects/{project_id}/fixtures/{fixture_id}/import")
def import_fixture(project_id: str, fixture_id: str, request: FixtureImportRequest) -> dict[str, Any]:
    _require_project(project_id)
    metadata = _fixture_metadata(fixture_id)
    path = FIXTURES[fixture_id]["path"]
    connector_id = _connector_id(request.source_connector_id)
    if metadata["kind"] == "raw_htrace":
        binding = _source_binding(connector_id, request.declared_revision)
        raw_input = _write_raw_trace_input(
            name=path.name,
            size=int(metadata["bytes"]),
            input_sha256=str(metadata["sha256"]),
            connector_id=connector_id,
            binding=binding,
            input_kind="raw_htrace",
            fixture_id=fixture_id,
        )
        artifact = _write_htrace_analysis_result(
            path=path,
            name=path.name,
            input_sha256=str(metadata["sha256"]),
            connector_id=connector_id,
            declared_revision=request.declared_revision,
            raw_input_artifact=raw_input,
        )
        return {"artifact": artifact}
    if metadata["kind"] != "external_report_json":
        raise PlatformError("TRACE_EVENT_UNSUPPORTED", "Use the experimental cluster endpoint for textual scheduler fixtures.", status_code=422)
    normalized = parse_json_trace(path)
    artifact = _write_trace_result(
        normalized,
        connector_id=connector_id,
        declared_revision=request.declared_revision,
        producer={"name": "trace-result-ingestion", "version": "1.0.0"},
        input_refs=[f"sha256:{metadata['sha256']}"],
    )
    return {"artifact": artifact}


@app.post("/v1/projects/{project_id}/fixtures/{fixture_id}/cluster")
def cluster_fixture(project_id: str, fixture_id: str, request: FixtureImportRequest) -> dict[str, Any]:
    _require_project(project_id)
    metadata = _fixture_metadata(fixture_id)
    if metadata["kind"] != "sys_trace_experimental":
        raise PlatformError("TRACE_EVENT_UNSUPPORTED", "This fixture is not a supported .sys trace.", status_code=422)
    normalized = cluster_sys_trace(FIXTURES[fixture_id]["path"])
    connector_id = _connector_id(request.source_connector_id)
    artifact = _write_trace_result(
        normalized,
        connector_id=connector_id,
        declared_revision=request.declared_revision,
        producer={"name": "trace-clustering", "version": "0.2.0-spike"},
        input_refs=[f"sha256:{metadata['sha256']}"],
    )
    return {"artifact": artifact}


@app.post("/v1/projects/{project_id}/trace-imports")
def import_json_trace(project_id: str, request: JsonTraceImportRequest) -> dict[str, Any]:
    _require_project(project_id)
    import_instance_id = f"trace_import_{secrets.token_hex(12)}"
    input_sha256 = sha256_json(request.trace_result)
    normalized = normalize_external_report(request.trace_result, trace_name=request.trace_name, trace_sha256=input_sha256)
    declared_revision = request.declared_revision or request.trace_result.get("source_revision")
    if not isinstance(declared_revision, str):
        declared_revision = None
    requested_connector = request.source_connector_id or request.trace_result.get("source_connector_id")
    connector_id = _connector_id(requested_connector if isinstance(requested_connector, str) else None)
    raw_input = _write_raw_trace_input(
        name=_safe_file_name(request.trace_name),
        size=len(json.dumps(request.trace_result).encode("utf-8")),
        input_sha256=input_sha256,
        connector_id=connector_id,
        binding=_source_binding(connector_id, declared_revision),
        input_kind="external_trace_analysis_report",
        import_instance_id=import_instance_id,
    )
    artifact = _write_trace_result(
        normalized,
        connector_id=connector_id,
        declared_revision=declared_revision,
        producer={"name": "trace-result-ingestion", "version": "1.0.0"},
        input_refs=[f"sha256:{input_sha256}", f"artifact:{raw_input['artifact_id']}"],
        import_instance_id=import_instance_id,
    )
    return {"artifact": artifact}


@app.post("/v1/projects/{project_id}/trace-imports/upload")
async def upload_trace(
    project_id: str,
    file: UploadFile = File(...),
    source_connector_id: str | None = Form(default=None),
    declared_revision: str | None = Form(default=None),
) -> dict[str, Any]:
    _require_project(project_id)
    import_instance_id = f"trace_import_{secrets.token_hex(12)}"
    path, input_sha256, size, name = await _persist_upload(file)
    suffix = path.suffix.casefold()
    effective_revision = declared_revision
    requested_connector = source_connector_id

    report: dict[str, Any] | None = None
    if suffix == ".json":
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(report, dict):
                raise ValueError("trace result root must be a JSON object")
            embedded_revision = report.get("source_revision")
            if not effective_revision and isinstance(embedded_revision, str):
                effective_revision = embedded_revision
            embedded_connector = report.get("source_connector_id")
            if not requested_connector and isinstance(embedded_connector, str):
                requested_connector = embedded_connector
        except ValueError as exc:
            fallback_connector_id = _connector_id(source_connector_id)
            fallback_binding = _source_binding(fallback_connector_id, declared_revision)
            _write_raw_trace_input(
                name=name,
                size=size,
                input_sha256=input_sha256,
                connector_id=fallback_connector_id,
                binding=fallback_binding,
                input_kind="external_trace_analysis_report",
                managed_path=str(path.relative_to(STATE_ROOT)),
                import_instance_id=import_instance_id,
            )
            raise PlatformError("TRACE_EVENT_UNSUPPORTED", str(exc), status_code=422) from exc

    connector_id = _connector_id(requested_connector)
    binding = _source_binding(connector_id, effective_revision)
    input_kind = {
        ".json": "external_trace_analysis_report",
        ".htrace": "raw_htrace",
        ".sys": "sys_trace_experimental",
    }.get(suffix, "unsupported_trace_input")
    raw_input = _write_raw_trace_input(
        name=name,
        size=size,
        input_sha256=input_sha256,
        connector_id=connector_id,
        binding=binding,
        input_kind=input_kind,
        managed_path=str(path.relative_to(STATE_ROOT)),
        import_instance_id=import_instance_id,
    )

    try:
        if suffix == ".htrace":
            artifact = await asyncio.to_thread(
                _write_htrace_analysis_result,
                path=path,
                name=name,
                input_sha256=input_sha256,
                connector_id=connector_id,
                declared_revision=effective_revision,
                raw_input_artifact=raw_input,
                import_instance_id=import_instance_id,
            )
            return {"artifact": artifact}
        if suffix == ".json" and report is not None:
            normalized = normalize_external_report(report, trace_name=name, trace_sha256=input_sha256)
            producer = {"name": "trace-result-ingestion", "version": "1.0.0"}
        elif suffix == ".sys":
            normalized = cluster_sys_trace(path)
            producer = {"name": "trace-clustering", "version": "0.2.0-spike"}
        else:
            raise PlatformError(
                "TRACE_EVENT_UNSUPPORTED",
                "Phase A accepts trace-team JSON reports, raw .htrace, and experimental .sys text traces.",
                status_code=422,
            )
    except ValueError as exc:
        raise PlatformError("TRACE_EVENT_UNSUPPORTED", str(exc), status_code=422) from exc
    artifact = _write_trace_result(
        normalized,
        connector_id=connector_id,
        declared_revision=effective_revision,
        producer=producer,
        input_refs=[f"sha256:{input_sha256}", f"artifact:{raw_input['artifact_id']}"],
        import_instance_id=import_instance_id,
    )
    return {"artifact": artifact}


@app.post("/v1/projects/{project_id}/trace-source-bindings")
def attest_trace_source_snapshot(
    project_id: str,
    request: TraceSourceSnapshotAttestationRequest,
) -> dict[str, Any]:
    """Bind an unversioned trace to a current local development snapshot.

    A raw trace normally does not carry a source-tree revision.  This endpoint
    never mutates its analysis artifact or upgrades an existing mismatch: it
    records the user's (or browser agent's) explicit assertion and derives a
    new analysis artifact whose snapshot equality can be checked.
    """

    _require_project(project_id)
    registered_action("trace.bind_source_snapshot")
    if request.attestation_actor not in {"user", "agent"}:
        raise PlatformError(
            "TRACE_SOURCE_ATTESTATION_ACTOR_INVALID",
            "Trace source attestation actor must be user or agent.",
            status_code=422,
        )
    if request.attestation_actor == "agent" and not request.attestation_note.strip():
        raise PlatformError(
            "TRACE_SOURCE_ATTESTATION_EVIDENCE_REQUIRED",
            "An agent must record the scenario evidence used to attest a trace-source snapshot.",
            status_code=422,
        )
    analysis = store.read("trace_analysis_result", request.analysis_artifact_id)
    previous_binding = analysis.get("source_binding") if isinstance(analysis.get("source_binding"), dict) else {}
    previous_match_level = str(previous_binding.get("match_level") or "unknown")
    if previous_match_level == "mismatch":
        raise PlatformError(
            "SOURCE_BINDING_MISMATCH",
            "This trace already declares a source revision that does not match its selected source connector; it cannot be overridden by attestation.",
            status_code=422,
            details={"source_binding": previous_binding},
        )
    if previous_match_level != "unknown":
        raise PlatformError(
            "TRACE_SOURCE_BINDING_ALREADY_DECLARED",
            "This trace already has a source revision declaration. Choose a matching connector or import the trace again with its original provenance.",
            status_code=422,
            details={"source_binding": previous_binding},
        )

    connector = connectors.get(request.source_connector_id)
    snapshot = connector.get("snapshot") if isinstance(connector.get("snapshot"), dict) else {}
    snapshot_id = str(snapshot.get("snapshot_id") or "")
    # Connectors created before v1.1 did not persist a dirty-worktree
    # fingerprint. Refresh them in place once so legacy local workspaces can
    # continue without forcing a commit or re-registration.
    worktree_fingerprint = connector.get("worktree_fingerprint") if isinstance(connector.get("worktree_fingerprint"), dict) else None
    legacy_snapshot_id = snapshot_id
    if worktree_fingerprint is None:
        if connector.get("kind") == "local":
            connector = connectors.refresh_snapshot(request.source_connector_id)
            snapshot = connector.get("snapshot") if isinstance(connector.get("snapshot"), dict) else {}
            snapshot_id = str(snapshot.get("snapshot_id") or "")
            worktree_fingerprint = connector.get("worktree_fingerprint") if isinstance(connector.get("worktree_fingerprint"), dict) else None
        else:
            worktree_fingerprint = connectors.capture_worktree_fingerprint(request.source_connector_id)
    if request.source_snapshot_id not in {snapshot_id, legacy_snapshot_id}:
        raise PlatformError(
            "SOURCE_SNAPSHOT_ID_INVALID",
            "The supplied source snapshot is not the current snapshot of the selected source connector.",
            status_code=422,
            details={
                "requested_snapshot_id": request.source_snapshot_id,
                "current_snapshot_id": snapshot_id,
                "legacy_snapshot_id": legacy_snapshot_id,
            },
        )
    if not isinstance(worktree_fingerprint, dict):
        raise PlatformError(
            "SOURCE_WORKTREE_FINGERPRINT_INVALID",
            "The selected source connector has no usable worktree content fingerprint.",
            status_code=422,
        )
    binding = _attested_local_snapshot_binding(connector, snapshot, worktree_fingerprint)
    live_snapshot = connectors.verify_current_snapshot(
        request.source_connector_id,
        snapshot_id,
        expected_worktree_fingerprint_sha256=str(worktree_fingerprint.get("sha256") or ""),
    )
    if not live_snapshot.get("matches_expected"):
        raise PlatformError(
            "SOURCE_BINDING_SNAPSHOT_STALE",
            "The selected source root changed while creating the trace-source binding. Refresh its snapshot and try again.",
            status_code=409,
            details=live_snapshot,
        )

    attestation = store.write(
        "trace_source_binding_attestation",
        {
            "analysis_artifact_id": analysis["artifact_id"],
            "previous_source_binding": previous_binding,
            "declared_source_snapshot_id": snapshot_id,
            "attestation_actor": request.attestation_actor,
            "attestation_note": request.attestation_note,
            "assertion": "The trace was captured from a build produced from the declared source snapshot.",
            "verification": "The platform verified that the declared source snapshot and local worktree content fingerprint equal the current connector state; it cannot independently prove the device build origin.",
        },
        source_connector_id=request.source_connector_id,
        source_binding=binding,
        producer={"name": "trace-source-binding", "version": "1.1.0"},
        input_artifacts=list(
            dict.fromkeys(
                [
                    f"artifact:{analysis['artifact_id']}",
                    f"sha256:{analysis['content_sha256']}",
                    *([f"artifact:{connector['snapshot_artifact_id']}"] if isinstance(connector.get("snapshot_artifact_id"), str) else []),
                ]
            )
        ),
    )
    rebound_analysis = store.write(
        "trace_analysis_result",
        _rebound_trace_analysis_payload(
            analysis,
            binding_artifact_id=attestation["artifact_id"],
            attestation_actor=request.attestation_actor,
        ),
        source_connector_id=request.source_connector_id,
        source_binding=binding,
        producer={"name": "trace-source-binding", "version": "1.1.0"},
        input_artifacts=[f"artifact:{analysis['artifact_id']}", f"artifact:{attestation['artifact_id']}"],
    )
    return {"artifact": rebound_analysis, "binding_artifact": attestation}


@app.post("/v1/projects/{project_id}/cluster-selections")
def select_cluster(project_id: str, request: ClusterSelectionRequest) -> dict[str, Any]:
    _require_project(project_id)
    registered_action(request.action_id)
    if request.selection_actor not in {"user", "agent", "system"}:
        raise PlatformError(
            "CLUSTER_SELECTION_ACTOR_INVALID",
            "Cluster selection actor must be user, agent, or system.",
            status_code=422,
        )
    automation: dict[str, Any] | None = None
    if request.selection_actor == "system":
        required = {
            "policy_id": request.automation_policy_id,
            "policy_version": request.automation_policy_version,
            "input_hash": request.automation_input_hash,
        }
        missing = sorted(name for name, value in required.items() if not isinstance(value, str) or not value)
        if missing:
            raise PlatformError(
                "AUTO_TRIAGE_POLICY_REQUIRED",
                "System cluster selection requires policy ID, version, and deterministic input hash.",
                status_code=422,
                details={"missing": missing},
            )
        if not re.fullmatch(r"[0-9a-f]{64}", str(request.automation_input_hash)):
            raise PlatformError(
                "AUTO_TRIAGE_INPUT_HASH_INVALID",
                "System cluster selection requires a lowercase SHA-256 input hash.",
                status_code=422,
            )
        automation = {
            "policy_id": request.automation_policy_id,
            "policy_version": request.automation_policy_version,
            "input_hash": request.automation_input_hash,
            "exclusion_reasons": request.exclusion_reasons,
        }
    analysis = store.read("trace_analysis_result", request.analysis_artifact_id)
    cluster = next((item for item in analysis.get("clusters", []) if item.get("cluster_id") == request.cluster_id), None)
    if cluster is None:
        raise PlatformError("CLUSTER_NOT_SELECTED", "Selected cluster is not part of the trace analysis artifact.", status_code=422)
    selected_cluster = dict(cluster)
    selected_cluster.update(
        {
            "selection_state": "selected",
            "selection_actor": request.selection_actor,
            "selection_rationale": request.rationale,
            "action_id": request.action_id,
            "selected_at": utc_now(),
            **({"automation": automation} if automation is not None else {}),
        }
    )
    artifact = store.write(
        "trace_cluster_selection",
        {
            "analysis_artifact_id": analysis["artifact_id"],
            "selected_cluster": selected_cluster,
            "state": "cluster_selected",
        },
        source_connector_id=analysis["source_connector_id"],
        source_binding=analysis["source_binding"],
        producer={"name": "cluster-selection", "version": "1.0.0"},
        input_artifacts=[f"artifact:{analysis['artifact_id']}", f"sha256:{analysis['content_sha256']}"],
    )
    return {"artifact": artifact}


@app.post("/v1/projects/{project_id}/trace-events")
def list_cluster_events(project_id: str, request: ClusterEventListRequest) -> dict[str, Any]:
    _require_project(project_id)
    registered_action("trace.list_cluster_events")
    analysis = store.read("trace_analysis_result", request.analysis_artifact_id)
    cluster = _cluster_from_analysis(analysis, request.cluster_id)
    catalog = _enrich_hitrace_event_catalog(analysis, _cluster_event_catalog(analysis, cluster, limit=request.limit))
    artifact = _write_trace_event_catalog(analysis, cluster, catalog)
    return {
        "analysis_artifact_id": analysis["artifact_id"],
        "cluster_id": cluster["cluster_id"],
        "artifact": artifact,
        **catalog,
    }


@app.post("/v1/projects/{project_id}/trace-events/resolve-source")
def resolve_cluster_event_source(project_id: str, request: TraceEventSourceRequest) -> dict[str, Any]:
    _require_project(project_id)
    registered_action("trace.resolve_event_source")
    analysis = store.read("trace_analysis_result", request.analysis_artifact_id)
    cluster = _cluster_from_analysis(analysis, request.cluster_id)
    catalog = _enrich_hitrace_event_catalog(analysis, _cluster_event_catalog(analysis, cluster, limit=MAX_CLUSTER_EVENT_CANDIDATES))
    event_catalog_artifact = _write_trace_event_catalog(analysis, cluster, catalog)
    event = next((item for item in catalog.get("events", []) if isinstance(item, dict) and item.get("event_id") == request.event_id), None)
    if event is None:
        raise PlatformError(
            "TRACE_EVENT_NOT_FOUND",
            "Selected event is not available for the requested trace cluster.",
            status_code=422,
            details={"cluster_id": request.cluster_id, "event_id": request.event_id},
        )
    artifact = _resolve_trace_event_source(analysis, cluster, event, event_catalog_artifact)
    return {"artifact": artifact}


@app.post("/v1/projects/{project_id}/trace-hotspot-discoveries")
def list_trace_hotspot_discovery(project_id: str, request: TraceHotspotDiscoveryRequest) -> dict[str, Any]:
    _require_project(project_id)
    registered_action("trace.list_hotspot_discovery")
    analysis = store.read("trace_analysis_result", request.analysis_artifact_id)
    scope = _hotspot_discovery_scope(request.discovery_id, module_key=request.module_key)
    catalog = _enrich_hitrace_event_catalog(
        analysis,
        _hotspot_discovery_catalog(
            analysis,
            request.discovery_id,
            limit=request.limit,
            module_key=request.module_key,
        ),
    )
    artifact = _write_trace_event_catalog(analysis, None, catalog, catalog_scope=scope)
    return {
        "analysis_artifact_id": analysis["artifact_id"],
        "discovery": scope,
        "artifact": artifact,
        **catalog,
    }


@app.post("/v1/projects/{project_id}/trace-hotspot-discoveries/resolve-source")
def resolve_trace_hotspot_discovery_event(project_id: str, request: TraceHotspotDiscoverySourceRequest) -> dict[str, Any]:
    _require_project(project_id)
    registered_action("trace.resolve_hotspot_discovery_event")
    analysis = store.read("trace_analysis_result", request.analysis_artifact_id)
    scope = _hotspot_discovery_scope(request.discovery_id, module_key=request.module_key)
    catalog = _enrich_hitrace_event_catalog(
        analysis,
        _hotspot_discovery_catalog(
            analysis,
            request.discovery_id,
            limit=int(HOTSPOT_DISCOVERY_PROFILES[request.discovery_id]["max_events"]),
            module_key=request.module_key,
        ),
    )
    event_catalog_artifact = _write_trace_event_catalog(analysis, None, catalog, catalog_scope=scope)
    event = next(
        (
            item
            for item in catalog.get("events", [])
            if isinstance(item, dict) and item.get("event_id") == request.event_id
        ),
        None,
    )
    if event is None:
        raise PlatformError(
            "TRACE_HOTSPOT_EVENT_NOT_FOUND",
            "Selected event is not available for the requested hotspot discovery method.",
            status_code=422,
            details={"discovery_id": request.discovery_id, "event_id": request.event_id},
        )
    artifact = _resolve_trace_event_source(
        analysis,
        None,
        event,
        event_catalog_artifact,
        catalog_scope=scope,
    )
    return {"artifact": artifact}


@app.post("/v1/projects/{project_id}/analysis-runs")
def start_analysis_run(project_id: str, request: AnalysisRunRequest) -> dict[str, Any]:
    _require_project(project_id)
    analysis = store.read("trace_analysis_result", request.analysis_artifact_id)
    selection = store.read("trace_cluster_selection", request.cluster_selection_artifact_id)
    if selection.get("analysis_artifact_id") != analysis.get("artifact_id"):
        raise PlatformError("CLUSTER_NOT_SELECTED", "Cluster selection does not belong to the analysis artifact.", status_code=422)
    selected_id = selection.get("selected_cluster", {}).get("cluster_id")
    selected_hotspots = [item for item in analysis.get("hotspot_candidates", []) if item.get("cluster_id") == selected_id]
    binding = analysis["source_binding"]
    match_level = binding.get("match_level")
    if match_level == "mismatch":
        raise PlatformError(
            "SOURCE_BINDING_MISMATCH",
            "Trace source revision does not match the selected source connector snapshot.",
            status_code=422,
            details={"source_binding": binding},
        )
    if match_level == "exact":
        live_snapshot = connectors.verify_current_snapshot(
            analysis["source_connector_id"],
            str(binding.get("resolved_snapshot_id") or ""),
            expected_worktree_fingerprint_sha256=(
                str(binding["worktree_fingerprint_sha256"])
                if isinstance(binding.get("worktree_fingerprint_sha256"), str)
                else None
            ),
        )
        if not live_snapshot["matches_expected"]:
            raise PlatformError(
                "SOURCE_BINDING_MISMATCH",
                "The selected source root changed after the trace artifact was bound; create a new snapshot and trace binding before analysis.",
                status_code=409,
                details=live_snapshot,
            )

    steps: list[dict[str, Any]] = []
    for state, detail in [
        ("uploaded", "Trace input retained as immutable artifact."),
        ("normalized", "Trace result normalized into clusters and hotspot candidates."),
        ("cluster_selected", "Selected cluster is explicit and auditable."),
        ("source_resolved", "Source binding inspected before AST/KB work."),
    ]:
        steps.append(_analysis_step(state=state, detail=detail, analysis=analysis, selection=selection))

    blocked_reasons: list[str] = []
    opportunities: list[dict[str, Any]] = []
    unresolved_hotspots = list(analysis.get("unresolved_hotspots", []))
    feature_artifact_ids: list[str] = []
    source_failure_artifact_ids: list[str] = []
    match_artifact_ids: list[str] = []
    ast_available_count = 0
    source_location_count = 0
    temporal_eligible_count = 0
    kb_match_count = 0

    if match_level != "exact":
        blocked_reasons.append("SOURCE_BINDING_UNKNOWN: trace result cannot be proven against the selected source snapshot.")
        for hotspot in selected_hotspots[:20]:
            opportunities.append(
                {
                    "opportunity_id": f"opp_{hotspot['hotspot_id']}",
                    "hotspot_id": hotspot["hotspot_id"],
                    "state": "source_binding_required",
                    "confidence": "not_scored",
                    "reason": "Automatic AST extraction and KB matching are blocked until exact source binding is supplied.",
                    "evidence_refs": hotspot.get("evidence_refs", []),
                }
            )
        final_state = "partial"
    else:
        if not selected_hotspots:
            blocked_reasons.append("SELECTED_CLUSTER_EMPTY: selected trace cluster has no hotspot candidates.")

        for hotspot in selected_hotspots[:MAX_AUTO_HOTSPOTS_PER_RUN]:
            hotspot_id = str(hotspot.get("hotspot_id") or "unknown_hotspot")
            source_location = _hotspot_source_location(hotspot)
            if source_location is None:
                unresolved_hotspots.append(hotspot_id)
                opportunities.append(
                    {
                        "opportunity_id": f"opp_{hotspot_id}",
                        "hotspot_id": hotspot_id,
                        "cluster_id": selected_id,
                        "state": "source_location_required",
                        "confidence": "not_scored",
                        "reason": "Exact trace-to-snapshot binding is present, but this hotspot has no declared source file and line.",
                        "evidence_refs": hotspot.get("evidence_refs", []),
                    }
                )
                continue

            source_location_count += 1
            temporal_ok, temporal_reason = _meets_temporal_evidence_gate(hotspot)
            if not temporal_ok:
                opportunities.append(
                    {
                        "opportunity_id": f"opp_{hotspot_id}",
                        "hotspot_id": hotspot_id,
                        "cluster_id": selected_id,
                        "state": "temporal_evidence_required",
                        "confidence": "not_scored",
                        "reason": temporal_reason,
                        "temporal_pressure": hotspot.get("temporal_pressure"),
                        "critical_path_metrics": hotspot.get("critical_path_metrics"),
                        "evidence_refs": hotspot.get("evidence_refs", []),
                    }
                )
                continue

            temporal_eligible_count += 1
            path, line = source_location
            try:
                feature_payload, feature_binding = _build_source_inspection_payload(analysis["source_connector_id"], path, line)
                feature_artifact = store.write(
                    "code_feature_inspection",
                    {
                        **feature_payload,
                        "hotspot_id": hotspot_id,
                        "cluster_id": selected_id,
                        "trace_evidence_refs": hotspot.get("evidence_refs", []),
                    },
                    source_connector_id=analysis["source_connector_id"],
                    source_binding=feature_binding,
                    producer={"name": "ast-feature-extraction", "version": "1.0.0"},
                    input_artifacts=[
                        f"artifact:{analysis['artifact_id']}",
                        f"artifact:{selection['artifact_id']}",
                    ],
                )
                feature_artifact_ids.append(feature_artifact["artifact_id"])
            except PlatformError as exc:
                failure_artifact = store.write(
                    "source_inspection_failure",
                    {
                        "hotspot_id": hotspot_id,
                        "cluster_id": selected_id,
                        "source_location": {"path": path, "line": line},
                        "error_code": exc.code,
                        "reason": exc.message,
                    },
                    source_connector_id=analysis["source_connector_id"],
                    source_binding=binding,
                    producer={"name": "ast-feature-extraction", "version": "1.0.0"},
                    input_artifacts=[f"artifact:{analysis['artifact_id']}", f"artifact:{selection['artifact_id']}"],
                )
                source_failure_artifact_ids.append(failure_artifact["artifact_id"])
                unresolved_hotspots.append(hotspot_id)
                opportunities.append(
                    {
                        "opportunity_id": f"opp_{hotspot_id}",
                        "hotspot_id": hotspot_id,
                        "cluster_id": selected_id,
                        "state": "source_inspection_failed",
                        "confidence": "not_scored",
                        "reason": exc.message,
                        "error_code": exc.code,
                        "evidence_refs": [*hotspot.get("evidence_refs", []), f"artifact:{failure_artifact['artifact_id']}"],
                    }
                )
                continue
            except Exception as exc:
                failure_artifact = store.write(
                    "source_inspection_failure",
                    {
                        "hotspot_id": hotspot_id,
                        "cluster_id": selected_id,
                        "source_location": {"path": path, "line": line},
                        "error_code": "AST_PARSE_UNAVAILABLE",
                        "reason": "Unexpected source inspection failure.",
                        "diagnostic": str(exc),
                    },
                    source_connector_id=analysis["source_connector_id"],
                    source_binding=binding,
                    producer={"name": "ast-feature-extraction", "version": "1.0.0"},
                    input_artifacts=[f"artifact:{analysis['artifact_id']}", f"artifact:{selection['artifact_id']}"],
                )
                source_failure_artifact_ids.append(failure_artifact["artifact_id"])
                unresolved_hotspots.append(hotspot_id)
                opportunities.append(
                    {
                        "opportunity_id": f"opp_{hotspot_id}",
                        "hotspot_id": hotspot_id,
                        "cluster_id": selected_id,
                        "state": "source_inspection_failed",
                        "confidence": "not_scored",
                        "reason": "Unexpected source inspection failure; the hotspot remains available for diagnosis.",
                        "error_code": "AST_PARSE_UNAVAILABLE",
                        "diagnostic": str(exc),
                        "evidence_refs": [*hotspot.get("evidence_refs", []), f"artifact:{failure_artifact['artifact_id']}"],
                    }
                )
                continue

            if feature_payload.get("feature_status") != "available":
                unresolved_hotspots.append(hotspot_id)
                opportunities.append(
                    {
                        "opportunity_id": f"opp_{hotspot_id}",
                        "hotspot_id": hotspot_id,
                        "cluster_id": selected_id,
                        "state": "ast_features_required",
                        "confidence": "not_scored",
                        "reason": "Precise AST extraction did not produce usable function features for this source location.",
                        "ast_diagnostics": feature_payload.get("ast_diagnostics", []),
                        "evidence_refs": [*hotspot.get("evidence_refs", []), f"artifact:{feature_artifact['artifact_id']}"],
                    }
                )
                continue

            ast_available_count += 1
            included_matches = _included_kb_matches(feature_payload, include_unverified=request.include_unverified)
            kb_match_artifact = store.write(
                "kb_match_result",
                {
                    "hotspot_id": hotspot_id,
                    "cluster_id": selected_id,
                    "source_feature_artifact_id": feature_artifact["artifact_id"],
                    "include_unverified": request.include_unverified,
                    "match_results": feature_payload.get("kb_matches", []),
                    "included_match_count": len(included_matches),
                },
                source_connector_id=analysis["source_connector_id"],
                source_binding=binding,
                producer={"name": "kb-retrieval", "version": "1.0.0"},
                input_artifacts=[f"artifact:{feature_artifact['artifact_id']}", f"artifact:{analysis['artifact_id']}"],
            )
            match_artifact_ids.append(kb_match_artifact["artifact_id"])

            if not included_matches:
                opportunities.append(
                    {
                        "opportunity_id": f"opp_{hotspot_id}",
                        "hotspot_id": hotspot_id,
                        "cluster_id": selected_id,
                        "state": "no_kb_match",
                        "confidence": "not_scored",
                        "reason": "AST features were extracted, but no KB pattern passed its deterministic threshold and readiness filter.",
                        "source_location": {"path": path, "line": line},
                        "evidence_refs": [
                            *hotspot.get("evidence_refs", []),
                            f"artifact:{feature_artifact['artifact_id']}",
                            f"artifact:{kb_match_artifact['artifact_id']}",
                        ],
                    }
                )
                continue

            for feature_index, (feature, match) in enumerate(included_matches, start=1):
                pattern_id = str(match.get("knowledge_base_pattern_id") or match.get("pattern_id") or "pattern")
                kb_match_count += 1
                opportunities.append(
                    {
                        "opportunity_id": f"opp_{hotspot_id}_{pattern_id}_{feature_index}",
                        "hotspot_id": hotspot_id,
                        "cluster_id": selected_id,
                        "state": "development_candidate",
                        "confidence": match.get("confidence", "not_scored"),
                        "knowledge_readiness": match.get("knowledge_readiness", "development_only"),
                        "knowledge_base_pattern_id": pattern_id,
                        "target_unit": match.get("target_unit", "UNKNOWN"),
                        "alternative_units": match.get("alternative_units", []),
                        "source_layers": match.get("source_layers", {}),
                        "source_location": {
                            "path": path,
                            "line": line,
                            "function": feature.get("function"),
                            "signature": feature.get("signature"),
                        },
                        "temporal_evidence": {
                            "criterion": temporal_reason,
                            "pressure": hotspot.get("temporal_pressure"),
                            "metrics": hotspot.get("active_time_metrics", {}),
                            "critical_path": hotspot.get("critical_path_metrics", {}),
                        },
                        "matched_features": match.get("matched_features", []),
                        "missing_features": match.get("missing_features", []),
                        "capability_constraints": match.get("capability_constraints", {}),
                        "cost_model": match.get("cost_model", {}),
                        "fallback_pairs": match.get("fallback_pairs", []),
                        "recommendation": match.get("recommendation", ""),
                        "reason": "Deterministic AST-to-KB match; development-only until capability, correctness, and benchmark evidence are completed.",
                        "evidence_refs": [
                            *hotspot.get("evidence_refs", []),
                            f"artifact:{feature_artifact['artifact_id']}",
                            f"artifact:{kb_match_artifact['artifact_id']}",
                        ],
                    }
                )

        deferred_hotspots = selected_hotspots[MAX_AUTO_HOTSPOTS_PER_RUN:]
        if deferred_hotspots:
            for hotspot in deferred_hotspots:
                hotspot_id = str(hotspot.get("hotspot_id") or "unknown_hotspot")
                opportunities.append(
                    {
                        "opportunity_id": f"opp_{hotspot_id}",
                        "hotspot_id": hotspot_id,
                        "cluster_id": selected_id,
                        "state": "deferred_by_run_limit",
                        "confidence": "not_scored",
                        "reason": f"Phase-A automatic run limit is {MAX_AUTO_HOTSPOTS_PER_RUN} hotspots per selected cluster.",
                        "evidence_refs": hotspot.get("evidence_refs", []),
                    }
                )

        steps.append(
            _analysis_step(
                state="feature_extracted",
                detail=(
                    f"AST extraction attempted for {temporal_eligible_count} temporally eligible hotspot(s); "
                    f"{ast_available_count} produced usable features."
                ),
                analysis=analysis,
                selection=selection,
                input_artifacts=[
                    f"artifact:{analysis['artifact_id']}",
                    f"artifact:{selection['artifact_id']}",
                    *[f"artifact:{artifact_id}" for artifact_id in feature_artifact_ids],
                    *[f"artifact:{artifact_id}" for artifact_id in source_failure_artifact_ids],
                ],
            )
        )
        steps.append(
            _analysis_step(
                state="kb_matched",
                detail=(
                    f"KB retrieval completed for {len(match_artifact_ids)} feature artifact(s); "
                    f"{kb_match_count} development candidate match(es) passed the configured readiness filter."
                ),
                analysis=analysis,
                selection=selection,
                input_artifacts=[
                    f"artifact:{analysis['artifact_id']}",
                    f"artifact:{selection['artifact_id']}",
                    *[f"artifact:{artifact_id}" for artifact_id in match_artifact_ids],
                ],
            )
        )

        final_state = "development_candidate" if kb_match_count else "partial"
        if not kb_match_count:
            blocked_reasons.append("No selected hotspot passed all temporal-evidence, source-location, AST, KB-threshold, and readiness gates.")

    steps.append(
        _analysis_step(
            state=final_state,
            detail=(
                f"{kb_match_count} development candidate match(es) generated."
                if final_state == "development_candidate"
                else "; ".join(blocked_reasons)
            ),
            analysis=analysis,
            selection=selection,
            input_artifacts=[
                f"artifact:{analysis['artifact_id']}",
                f"artifact:{selection['artifact_id']}",
                *[f"artifact:{artifact_id}" for artifact_id in feature_artifact_ids],
                *[f"artifact:{artifact_id}" for artifact_id in source_failure_artifact_ids],
                *[f"artifact:{artifact_id}" for artifact_id in match_artifact_ids],
            ],
        )
    )
    report = store.write(
        "opportunity_report",
        {
            "analysis_artifact_id": analysis["artifact_id"],
            "cluster_selection_artifact_id": selection["artifact_id"],
            "state": final_state,
            "knowledge_readiness": "development_only",
            "opportunities": opportunities,
            "unresolved_hotspots": list(dict.fromkeys(unresolved_hotspots)),
            "blocking_conditions": blocked_reasons,
            "summary": {
                "selected_cluster_id": selected_id,
                "selected_hotspot_count": len(selected_hotspots),
                "source_location_count": source_location_count,
                "temporal_eligible_count": temporal_eligible_count,
                "ast_available_count": ast_available_count,
                "kb_match_count": kb_match_count,
                "automatic_hotspot_limit": MAX_AUTO_HOTSPOTS_PER_RUN,
            },
        },
        source_connector_id=analysis["source_connector_id"],
        source_binding=binding,
        producer={"name": "heterogeneous-opportunity-orchestrator", "version": "1.0.0"},
        input_artifacts=[f"artifact:{analysis['artifact_id']}", f"artifact:{selection['artifact_id']}"] + [f"artifact:{step['artifact_id']}" for step in steps],
    )
    run = store.write(
        "analysis_run",
        {
            "state": final_state,
            "analysis_artifact_id": analysis["artifact_id"],
            "cluster_selection_artifact_id": selection["artifact_id"],
            "opportunity_report_artifact_id": report["artifact_id"],
            "step_artifact_ids": [step["artifact_id"] for step in steps],
            "kb_version": request.kb_version,
            "include_unverified": request.include_unverified,
        },
        source_connector_id=analysis["source_connector_id"],
        source_binding=binding,
        producer={"name": "heterogeneous-opportunity-orchestrator", "version": "1.0.0"},
        input_artifacts=[f"artifact:{report['artifact_id']}"] + [f"artifact:{step['artifact_id']}" for step in steps],
    )
    return {"run": run, "report": report}


@app.post("/v1/projects/{project_id}/event-opportunity-runs")
def identify_selected_event_opportunity(project_id: str, request: EventOpportunityAnalysisRequest) -> dict[str, Any]:
    """Run the AST-to-KB path for one explicitly selected trace event.

    Cluster-wide automation remains a separate policy-driven path.  This route
    preserves a user/agent's selected event as its own evidence object and does
    not upgrade source-location evidence into trace-to-snapshot binding.
    """

    _require_project(project_id)
    registered_action("event.identify_opportunity")
    analysis = store.read("trace_analysis_result", request.analysis_artifact_id)
    source_resolution = store.read("trace_event_source_resolution", request.source_resolution_artifact_id)
    if source_resolution.get("analysis_artifact_id") != analysis.get("artifact_id"):
        raise PlatformError(
            "EVENT_SOURCE_RESOLUTION_MISMATCH",
            "Selected source-resolution evidence does not belong to this trace analysis artifact.",
            status_code=422,
        )
    if source_resolution.get("source_connector_id") != analysis.get("source_connector_id"):
        raise PlatformError(
            "EVENT_SOURCE_CONNECTOR_MISMATCH",
            "Selected source-resolution evidence was produced against another source connector.",
            status_code=422,
        )

    event = source_resolution.get("event") if isinstance(source_resolution.get("event"), dict) else {}
    resolution = source_resolution.get("resolution") if isinstance(source_resolution.get("resolution"), dict) else {}
    code_block = source_resolution.get("code_block") if isinstance(source_resolution.get("code_block"), dict) else {}
    code_source = code_block.get("source") if isinstance(code_block.get("source"), dict) else {}
    code_match = code_block.get("match_evidence") if isinstance(code_block.get("match_evidence"), dict) else {}
    ast_target = code_match.get("ast_target") if isinstance(code_match.get("ast_target"), dict) else None
    event_id = str(event.get("event_id") or "selected_event")
    event_name = str(event.get("event_name") or event_id)
    catalog_scope = source_resolution.get("catalog_scope") if isinstance(source_resolution.get("catalog_scope"), dict) else {}
    cluster_id = catalog_scope.get("cluster_id") if isinstance(catalog_scope.get("cluster_id"), str) else None
    binding = analysis["source_binding"]
    evidence_refs = [f"artifact:{source_resolution['artifact_id']}"]
    raw_event_refs = event.get("trace_event_refs")
    if isinstance(raw_event_refs, list):
        evidence_refs.extend(
            str(item.get("event_id"))
            for item in raw_event_refs
            if isinstance(item, dict) and isinstance(item.get("event_id"), str)
        )

    def write_result(
        state: str,
        opportunities: list[dict[str, Any]],
        *,
        blocking_conditions: list[str],
        feature_artifact_id: str | None = None,
        kb_match_artifact_id: str | None = None,
        source_location_available: bool = False,
        ast_available: bool = False,
    ) -> dict[str, Any]:
        report = store.write(
            "opportunity_report",
            {
                "analysis_artifact_id": analysis["artifact_id"],
                "source_resolution_artifact_id": source_resolution["artifact_id"],
                "selection_mode": "manual_trace_event",
                "state": state,
                "knowledge_readiness": "development_only",
                "opportunities": opportunities,
                "unresolved_hotspots": [event_id] if state != "development_candidate" else [],
                "blocking_conditions": blocking_conditions,
                "summary": {
                    "selected_event_id": event_id,
                    "selected_event_name": event_name,
                    "selected_hotspot_count": 1,
                    "source_location_count": 1 if source_location_available else 0,
                    "temporal_eligible_count": 0,
                    "ast_available_count": 1 if ast_available else 0,
                    "kb_match_count": sum(item.get("state") == "development_candidate" for item in opportunities),
                    "selection_mode": "manual_trace_event",
                },
            },
            source_connector_id=analysis["source_connector_id"],
            source_binding=binding,
            producer={"name": "selected-event-opportunity-identification", "version": "1.0.0"},
            input_artifacts=[
                f"artifact:{analysis['artifact_id']}",
                f"artifact:{source_resolution['artifact_id']}",
                *([f"artifact:{feature_artifact_id}"] if feature_artifact_id else []),
                *([f"artifact:{kb_match_artifact_id}"] if kb_match_artifact_id else []),
            ],
        )
        run = store.write(
            "analysis_run",
            {
                "state": state,
                "run_kind": "manual_trace_event",
                "analysis_artifact_id": analysis["artifact_id"],
                "source_resolution_artifact_id": source_resolution["artifact_id"],
                "opportunity_report_artifact_id": report["artifact_id"],
                "step_artifact_ids": [artifact_id for artifact_id in (feature_artifact_id, kb_match_artifact_id) if artifact_id],
                "include_unverified": request.include_unverified,
            },
            source_connector_id=analysis["source_connector_id"],
            source_binding=binding,
            producer={"name": "selected-event-opportunity-identification", "version": "1.0.0"},
            input_artifacts=[f"artifact:{report['artifact_id']}"] + [
                f"artifact:{artifact_id}" for artifact_id in (feature_artifact_id, kb_match_artifact_id) if artifact_id
            ],
        )
        return {"run": run, "report": report}

    match_level = str(binding.get("match_level") or "unknown")
    base_opportunity = {
        "opportunity_id": f"opp_{event_id}",
        "hotspot_id": event_id,
        "event_id": event_id,
        "event_name": event_name,
        **({"cluster_id": cluster_id} if cluster_id else {}),
        "trace_scope": catalog_scope,
        "trace_metrics": event.get("metrics") if isinstance(event.get("metrics"), dict) else {},
        "evidence_refs": evidence_refs,
    }
    if match_level == "mismatch":
        raise PlatformError(
            "SOURCE_BINDING_MISMATCH",
            "Trace source revision does not match the selected source connector snapshot.",
            status_code=422,
            details={"source_binding": binding},
        )
    if match_level != "exact":
        return write_result(
            "partial",
            [
                {
                    **base_opportunity,
                    "state": "source_binding_required",
                    "confidence": "not_scored",
                    "reason": "AST extraction and KB matching require an exact trace-to-source snapshot binding.",
                }
            ],
            blocking_conditions=["SOURCE_BINDING_UNKNOWN: selected event cannot enter AST/KB matching until its source snapshot is exact."],
        )

    live_snapshot = connectors.verify_current_snapshot(
        analysis["source_connector_id"],
        str(binding.get("resolved_snapshot_id") or ""),
        expected_worktree_fingerprint_sha256=(
            str(binding["worktree_fingerprint_sha256"])
            if isinstance(binding.get("worktree_fingerprint_sha256"), str)
            else None
        ),
    )
    if not live_snapshot["matches_expected"]:
        raise PlatformError(
            "SOURCE_BINDING_MISMATCH",
            "The selected source root changed after the trace artifact was bound; create a new snapshot and trace binding before analysis.",
            status_code=409,
            details=live_snapshot,
        )

    resolution_state = str(resolution.get("state") or "unresolved")
    path = code_source.get("path")
    line = code_source.get("line")
    if resolution_state not in AST_ELIGIBLE_SOURCE_RESOLUTION_STATES or not isinstance(path, str) or not path:
        return write_result(
            "partial",
            [
                {
                    **base_opportunity,
                    "state": "source_location_required",
                    "confidence": "not_scored",
                    "reason": (
                        "Selected event has browsing-only source evidence "
                        f"({resolution_state}); AST/KB matching requires an upstream declared source location "
                        "or a tag-verified exact HiTrace instrumentation point."
                    ),
                }
            ],
            blocking_conditions=["SOURCE_LOCATION_REQUIRED: only declared source or tag-verified exact HiTrace instrumentation evidence may enter AST/KB matching."],
        )
    try:
        line_number = int(line)
    except (TypeError, ValueError):
        return write_result(
            "partial",
            [
                {
                    **base_opportunity,
                    "state": "source_location_required",
                    "confidence": "not_scored",
                    "reason": "Selected event has no valid declared source line for AST extraction.",
                }
            ],
            blocking_conditions=["SOURCE_LOCATION_REQUIRED: declared source line is unavailable."],
        )

    try:
        feature_payload, _feature_binding = _build_source_inspection_payload(
            analysis["source_connector_id"],
            path,
            line_number,
            target_function=ast_target,
            source_match=code_match or None,
        )
    except PlatformError as exc:
        return write_result(
            "partial",
            [
                {
                    **base_opportunity,
                    "state": "source_inspection_failed",
                    "confidence": "not_scored",
                    "reason": exc.message,
                    "error_code": exc.code,
                }
            ],
            blocking_conditions=[f"SOURCE_INSPECTION_FAILED: {exc.code}"],
            source_location_available=True,
        )

    feature_artifact = store.write(
        "code_feature_inspection",
        {
            **feature_payload,
            "event_id": event_id,
            "event_name": event_name,
            "source_resolution_artifact_id": source_resolution["artifact_id"],
            "trace_scope": catalog_scope,
            "trace_evidence_refs": evidence_refs,
        },
        source_connector_id=analysis["source_connector_id"],
        # The trace binding was freshly verified above.  Preserve it on the
        # AST artifact instead of re-resolving a dirty local connector from a
        # bare snapshot ID, which intentionally lacks a worktree attestation.
        source_binding=binding,
        producer={"name": "ast-feature-extraction", "version": "1.0.0"},
        input_artifacts=[f"artifact:{analysis['artifact_id']}", f"artifact:{source_resolution['artifact_id']}"],
    )
    if feature_payload.get("feature_status") != "available":
        return write_result(
            "partial",
            [
                {
                    **base_opportunity,
                    "state": "ast_features_required",
                    "confidence": "not_scored",
                    "reason": "Precise AST extraction did not produce usable function features for this selected event.",
                    "ast_diagnostics": feature_payload.get("ast_diagnostics", []),
                    "evidence_refs": [*evidence_refs, f"artifact:{feature_artifact['artifact_id']}"],
                }
            ],
            blocking_conditions=["AST_FEATURES_REQUIRED: no usable precise AST feature was produced."],
            feature_artifact_id=feature_artifact["artifact_id"],
            source_location_available=True,
        )

    included_matches = _included_kb_matches(feature_payload, include_unverified=request.include_unverified)
    kb_match_artifact = store.write(
        "kb_match_result",
        {
            "event_id": event_id,
            "event_name": event_name,
            "source_feature_artifact_id": feature_artifact["artifact_id"],
            "source_resolution_artifact_id": source_resolution["artifact_id"],
            "include_unverified": request.include_unverified,
            "match_results": feature_payload.get("kb_matches", []),
            "included_match_count": len(included_matches),
        },
        source_connector_id=analysis["source_connector_id"],
        source_binding=binding,
        producer={"name": "kb-retrieval", "version": "1.0.0"},
        input_artifacts=[f"artifact:{feature_artifact['artifact_id']}", f"artifact:{source_resolution['artifact_id']}"],
    )
    if not included_matches:
        return write_result(
            "partial",
            [
                {
                    **base_opportunity,
                    "state": "no_kb_match",
                    "confidence": "not_scored",
                    "reason": "AST features were extracted, but no KB pattern passed its deterministic threshold and readiness filter.",
                    "source_location": {"path": path, "line": line_number},
                    "evidence_refs": [*evidence_refs, f"artifact:{feature_artifact['artifact_id']}", f"artifact:{kb_match_artifact['artifact_id']}"],
                }
            ],
            blocking_conditions=["NO_KB_MATCH: extracted AST features did not match an eligible KB record."],
            feature_artifact_id=feature_artifact["artifact_id"],
            kb_match_artifact_id=kb_match_artifact["artifact_id"],
            source_location_available=True,
            ast_available=True,
        )

    opportunities: list[dict[str, Any]] = []
    for feature_index, (feature, match) in enumerate(included_matches, start=1):
        pattern_id = str(match.get("knowledge_base_pattern_id") or match.get("pattern_id") or "pattern")
        opportunities.append(
            {
                **base_opportunity,
                "opportunity_id": f"opp_{event_id}_{pattern_id}_{feature_index}",
                "state": "development_candidate",
                "confidence": match.get("confidence", "not_scored"),
                "knowledge_readiness": match.get("knowledge_readiness", "development_only"),
                "knowledge_base_pattern_id": pattern_id,
                "target_unit": match.get("target_unit", "UNKNOWN"),
                "alternative_units": match.get("alternative_units", []),
                "source_layers": match.get("source_layers", {}),
                "source_location": {
                    "path": path,
                    "line": line_number,
                    "function": feature.get("function"),
                    "signature": feature.get("signature"),
                    "resolution_state": resolution_state,
                    "resolution_method": resolution.get("method"),
                },
                "matched_features": match.get("matched_features", []),
                "missing_features": match.get("missing_features", []),
                "capability_constraints": match.get("capability_constraints", {}),
                "cost_model": match.get("cost_model", {}),
                "fallback_pairs": match.get("fallback_pairs", []),
                "recommendation": match.get("recommendation", ""),
                "reason": "Selected event passed exact-source, AST feature, and deterministic KB matching gates; capability, correctness, and benchmark evidence remain required.",
                "evidence_refs": [*evidence_refs, f"artifact:{feature_artifact['artifact_id']}", f"artifact:{kb_match_artifact['artifact_id']}"],
            }
        )
    return write_result(
        "development_candidate",
        opportunities,
        blocking_conditions=[],
        feature_artifact_id=feature_artifact["artifact_id"],
        kb_match_artifact_id=kb_match_artifact["artifact_id"],
        source_location_available=True,
        ast_available=True,
    )


@app.post("/v1/source-inspections")
def inspect_source_location(request: SourceInspectionRequest) -> dict[str, Any]:
    payload, binding = _build_source_inspection_payload(request.source_connector_id, request.path, request.line)
    artifact = store.write(
        "code_feature_inspection",
        payload,
        source_connector_id=request.source_connector_id,
        source_binding=binding,
        producer={"name": "ast-feature-extraction", "version": "1.0.0"},
    )
    return {"artifact": artifact}


@app.post("/v1/workspace-files/read")
def read_workspace_file(request: WorkspaceFileReadRequest) -> dict[str, Any]:
    connector, target, relative_path = _workspace_file_target(request.source_connector_id, request.path, must_exist=True)
    content, truncated, size = _read_workspace_text(target, request.max_bytes)
    artifact = store.write(
        "agent_workspace_file_read",
        {
            "path": relative_path,
            "sha256": sha256_file(target),
            "bytes": size,
            "returned_bytes": len(content.encode("utf-8")),
            "truncated": truncated,
        },
        source_connector_id=request.source_connector_id,
        source_binding=_source_binding(request.source_connector_id, str(connector["snapshot"]["snapshot_id"])),
        producer={"name": "page-agent-workspace-reader", "version": "1.0.0"},
    )
    return {
        "artifact": artifact,
        "path": relative_path,
        "sha256": sha256_file(target),
        "bytes": size,
        "truncated": truncated,
        "content": content,
    }


@app.post("/v1/workspace-files/write")
def write_workspace_file(request: WorkspaceFileWriteRequest) -> dict[str, Any]:
    if request.execution_policy != "full_access":
        raise PlatformError(
            "WORKSPACE_WRITE_REQUIRES_FULL_ACCESS",
            "当前执行策略不允许页面助手写入代码仓。",
            status_code=403,
        )
    connector, target, relative_path = _workspace_file_target(request.source_connector_id, request.path, must_exist=False)
    before_sha256 = sha256_file(target) if target.exists() else None
    if target.exists() and request.expected_sha256 != before_sha256:
        raise PlatformError(
            "WORKSPACE_FILE_CONFLICT",
            "文件内容已变化；请先重新读取后再写入，避免覆盖用户的新修改。",
            status_code=409,
            details={"path": relative_path, "current_sha256": before_sha256},
        )
    if not target.exists() and request.expected_sha256:
        raise PlatformError(
            "WORKSPACE_FILE_CONFLICT",
            "目标文件尚不存在，不能携带已有文件的内容哈希写入。",
            status_code=409,
        )
    _atomic_write_workspace_text(target, request.content)
    after_sha256 = sha256_file(target)
    artifact = store.write(
        "agent_workspace_file_write",
        {
            "path": relative_path,
            "before_sha256": before_sha256,
            "after_sha256": after_sha256,
            "bytes": target.stat().st_size,
            "execution_policy": request.execution_policy,
            "written_content_sha256": sha256_json({"content": request.content}),
        },
        source_connector_id=request.source_connector_id,
        source_binding=_source_binding(request.source_connector_id, str(connector["snapshot"]["snapshot_id"])),
        producer={"name": "page-agent-workspace-writer", "version": "1.0.0"},
    )
    return {
        "artifact": artifact,
        "path": relative_path,
        "sha256": after_sha256,
        "bytes": target.stat().st_size,
        "previous_sha256": before_sha256,
    }


@app.get("/v1/reports/{analysis_run_artifact_id}")
def export_report(analysis_run_artifact_id: str) -> dict[str, Any]:
    run = store.read("analysis_run", analysis_run_artifact_id)
    report = store.read("opportunity_report", run["opportunity_report_artifact_id"])
    return {"run": run, "report": report}


@app.post("/v1/agent-action-audits")
def agent_action_audit(request: AgentAuditRequest) -> dict[str, Any]:
    action = registered_action(request.action_id)
    connector = connectors.get(request.source_connector_id)
    binding = _source_binding(request.source_connector_id, connector["snapshot"]["snapshot_id"])
    artifact = store.write(
        "agent_action_audit",
        {
            "conversation_id": request.conversation_id,
            "action_id": action["action_id"],
            "risk_level": action["risk_level"],
            "input_sha256": request.input_sha256,
            "output_sha256": request.output_sha256,
            "result": request.result,
            "model_key_present": False,
        },
        source_connector_id=request.source_connector_id,
        source_binding=binding,
        producer={"name": "browser-agent-audit", "version": "1.0.0"},
    )
    return {"artifact": artifact}
