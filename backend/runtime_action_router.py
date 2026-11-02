"""Backend ActionRouter used by :mod:`backend.agent_runtime`.

It owns only platform/build/device actions.  Existing rich workbench actions
remain ``ui.*`` work for the browser and are returned through the Agent Run
event stream.  This lets the product migrate without granting a model direct
DOM or host-shell access.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Callable

from .agent_runtime import ActionRouter
from .build_worker import BuildWorker
from .core import ArtifactStore, PlatformError, sha256_json
from .deployment_mode import require_action_capability
from .custom_api_kb import CustomApiKnowledgeBaseManager
from .application_flow import ApplicationFlowManager
from .application_flow_trace import ApplicationFlowRuntimeTraceManager, TRACE_ANALYSIS_ARTIFACT_TYPE
from .code_graph import CodeGraphManager
from .curated_templates import (
    CuratedTemplateError,
    CuratedTemplateNotFoundError,
    list_curated_templates,
    read_curated_template,
)
from .device_gateway import (
    DEFAULT_LEASE_SECONDS,
    LEASE_REUSE_MIN_REMAINING_SECONDS,
    DeviceGateway,
    normalize_deployment_result,
)
from .experience_evolution import ExperienceEvolutionManager
from .experimental_flow import ExperimentalFlowManager
from .page_analysis_workflow import validate_page_analysis_action
from .scenario_capture_target import bind_capture_target, capture_target
from .skill_versions import (
    activate_user_skill_version,
    save_user_skill_version,
    skill_management_catalog,
    skill_management_detail,
)
from .skills import registered_action
from .skill_bootstrap import (
    SKILL_BOOTSTRAP_ACK_ACTION,
    observe_skill_bootstrap,
    validate_skill_bootstrap_ack,
)


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any, limit: int = 1024) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _application_flow_analysis_scope(action_id: str, run: dict[str, Any]) -> str:
    if not action_id.startswith("application_flow."):
        return ""
    context = _record(run.get("context"))
    focused_task = _record(context.get("focused_task"))
    return (
        _text(focused_task.get("application_flow_analysis_id"), 160)
        or _text(context.get("application_flow_analysis_id"), 160)
    )


def _device_deployment_idempotency_key(
    *,
    run_id: str,
    build_artifact_id: str,
    probe_artifact_id: str,
    retry_nonce: str,
) -> str:
    return sha256_json({
        "run": run_id,
        "action": "device.deploy_artifact",
        "build": build_artifact_id,
        "probe": probe_artifact_id,
        "retry_nonce": _text(retry_nonce, 160) or None,
    })


RUNTIME_ACTIONS: dict[str, dict[str, Any]] = {
    "workload_features.catalog": {"owner": "platform", "required": set()},
    "workload_features.list_profiles": {"owner": "platform", "required": set()},
    "workload_features.analyze": {"owner": "platform", "required": {"dataset_id"}},
    "workload_features.read_profile": {"owner": "platform", "required": {"profile_id"}},
    "scenario_capture.catalog": {"owner": "platform", "required": set()},
    "scenario_capture.create_plan": {"owner": "platform", "required": set()},
    "scenario_capture.read_plan": {"owner": "platform", "required": {"plan_id"}},
    "scenario_capture.generate_script": {"owner": "platform", "required": {"plan_id"}},
    "scenario_capture.execute": {"owner": "platform", "required": {"plan_id"}},
    "scenario_capture.read_run": {"owner": "platform", "required": {"run_id"}},
    "scenario_capture.discover_devices": {"owner": "platform", "required": set()},
    "parallel_modeling.catalog": {"owner": "platform", "required": set()},
    "parallel_modeling.import_dataset": {"owner": "platform", "required": set()},
    "parallel_modeling.analyze": {"owner": "platform", "required": {"dataset_id"}},
    "parallel_modeling.read_analysis": {"owner": "platform", "required": {"analysis_id"}},
    "parallel_modeling.search": {"owner": "platform", "required": {"analysis_id"}},
    "parallel_modeling.read_search": {"owner": "platform", "required": {"search_id"}},
    "parallel_job.read": {"owner": "platform", "required": {"job_id"}},
    "parallel_job.cancel": {"owner": "platform", "required": {"job_id"}},
    SKILL_BOOTSTRAP_ACK_ACTION: {
        "owner": "platform",
        "required": {"bootstrap_id", "orchestrator_version", "orchestrator_status", "skill_discovery"},
    },
    "skills.list": {
        "owner": "platform",
        "required": set(),
    },
    "skills.read": {
        "owner": "platform",
        "required": {"skill_id"},
    },
    "skills.save_user_version": {
        "owner": "platform",
        "required": {"skill_id", "label_zh", "content"},
    },
    "skills.activate_version": {
        "owner": "platform",
        "required": {"skill_id"},
    },
    "experience.list_sources": {
        "owner": "platform",
        "required": set(),
    },
    "experience.read_source": {
        "owner": "platform",
        "required": {"source_type", "source_id"},
    },
    "experience.list_cases": {
        "owner": "platform",
        "required": set(),
    },
    "experience.save_review": {
        "owner": "platform",
        "required": {"title", "summary_zh", "source_refs", "environment_comparison", "pitfalls"},
    },
    "experience.triage_case": {
        "owner": "platform",
        "required": {"case_id", "status"},
    },
    "application_flow.list_assets": {
        "owner": "platform",
        "required": set(),
    },
    "application_flow.inspect_repository": {
        "owner": "platform",
        "required": {"source_connector_id"},
    },
    "application_flow.search_source": {
        "owner": "platform",
        "required": {"source_connector_id", "query"},
    },
    "application_flow.read_source": {
        "owner": "platform",
        "required": {"source_connector_id", "path"},
    },
    "application_flow.inspect_trace_instrumentation": {
        "owner": "platform",
        "required": {"source_connector_id", "path"},
    },
    "application_flow.inspect_runtime_trace": {
        "owner": "platform",
        "required": {"source_connector_id", "analysis_artifact_id", "scenario_artifact_id"},
    },
    "application_flow.apply_runtime_trace_alignment": {
        "owner": "platform",
        "required": {
            "source_connector_id", "analysis_artifact_id", "event_catalog_artifact_id",
            "scenario_artifact_id", "flow_id", "mermaid", "stage_matches", "change_summary",
        },
    },
    "application_flow.read_asset": {
        "owner": "platform",
        "required": {"artifact_type", "artifact_id"},
    },
    "application_flow.save_discovery": {
        "owner": "platform",
        "required": {"source_connector_id", "discovery"},
    },
    "application_flow.publish_candidates": {
        "owner": "platform",
        "required": {"source_connector_id", "summary_zh", "candidates"},
    },
    "application_flow.save_scenario": {
        "owner": "platform",
        "required": {"source_connector_id", "discovery_artifact_id", "flow_id", "scenario_json", "mermaid", "stage_evidence", "unknowns"},
    },
    "application_flow.compile_mermaid": {
        "owner": "platform",
        "required": {"source_connector_id", "discovery_artifact_id", "flow_id", "mermaid"},
    },
    "application_flow.enrich_trace_anchors": {
        "owner": "platform",
        "required": {"source_connector_id", "discovery_artifact_id", "scenario_artifact_id", "flow_id", "stage_results"},
    },
    "code_graph.list": {
        "owner": "platform",
        "required": set(),
    },
    "code_graph.build": {
        "owner": "platform",
        "required": {"source_connector_id"},
    },
    "code_graph.get": {
        "owner": "platform",
        "required": {"graph_id"},
    },
    "code_graph.search": {
        "owner": "platform",
        "required": {"graph_id"},
    },
    "code_graph.query_knowledge": {
        "owner": "platform",
        "required": {"graph_id"},
    },
    "code_graph.publish_report": {
        "owner": "platform",
        "required": {"graph_id", "title", "markdown"},
    },
    "experimental_flow.read_evidence": {
        "owner": "platform",
        "required": {"evidence_artifact_id"},
    },
    "experimental_flow.read_result": {
        "owner": "platform",
        "required": {"result_artifact_id"},
    },
    "experimental_flow.publish_result": {
        "owner": "platform",
        "required": {
            "analysis_kind", "source_connector_id", "flow_goal", "title",
            "mermaid", "summary_markdown", "evidence_artifact_id",
        },
    },
    "knowledge.list_custom_api_kbs": {
        "owner": "platform",
        "required": set(),
    },
    "knowledge.build_custom_api_kb": {
        "owner": "platform",
        "required": {"unit_name"},
    },
    "platform.list_hardware_api_templates": {
        "owner": "platform",
        "required": set(),
    },
    "platform.read_hardware_api_template": {
        "owner": "platform",
        "required": {"template_id"},
    },
    "platform.record_experiment_findings": {
        "owner": "platform",
        "required": {"summary_zh", "findings", "limitations", "evidence_artifacts"},
    },
    "build.describe_profile": {
        "owner": "platform",
        "required": set(),
    },
    "build.stage_source": {
        "owner": "platform",
        "required": {"profile_id", "language", "source"},
    },
    "build.create": {
        "owner": "platform",
        "required": {"source_artifact_id", "profile_id"},
    },
    "device.list_bridges": {
        "owner": "platform",
        "required": set(),
    },
    "device.create_lease": {
        "owner": "platform",
        "required": {"bridge_id", "logical_device_id"},
    },
    "device.discover": {
        "owner": "device",
        "required": set(),
    },
    "device.probe": {
        "owner": "device",
        "required": {"device_serial"},
    },
    "device.deploy_artifact": {
        "owner": "device",
        "required": {"build_result_artifact_id", "device_probe_artifact_id"},
    },
    "device.execute_shell": {
        "owner": "device",
        "required": {"device_probe_artifact_id", "command"},
    },
    "device.collect_artifact": {
        "owner": "device",
        "required": {"device_probe_artifact_id", "remote_path"},
    },
    "device.cancel_job": {
        "owner": "device",
        "required": {"job_id"},
    },
}

# These actions belong to the pre-Runtime browser workflow.  They are kept in
# ``skills.action_catalog`` so an already-open legacy workbench can finish its
# own flow, but a durable Agent Run must never dispatch them.  In particular,
# the legacy source staging endpoint couples staging to a browser-held probe;
# the backend Build Worker deliberately does not have that dependency.
LEGACY_BROWSER_DEVICE_ACTIONS = frozenset(
    {
        "device.describe_experiment_profiles",
        "device.stage_experiment_source",
        "device.build_upload_experiment",
    }
)
BUILD_PROFILE_IDS = frozenset({"native_c", "native_cpp", "gpu_gles3", "gpu_vulkan", "npu_nnrt", "media_avcodec"})
EXPERIMENT_FINDING_CATEGORIES = frozenset(
    {
        "build_contract",
        "deployment_runtime_contract",
        "api_runtime_behavior",
        "device_capability",
        "correctness",
        "performance",
        "tooling",
        "workflow",
        "evidence_boundary",
        "other",
    }
)
EXPERIMENT_FINDING_REPRODUCIBILITY = frozenset(
    {"first_observation", "reproduced_same_device", "reproduced_cross_device", "contradicted", "not_tested"}
)
EXPERIMENT_FINDING_DESTINATIONS = frozenset(
    {
        "build_profile",
        "hardware_api_kb",
        "device_validation_skill",
        "regression_fixture",
        "product_diagnostic",
        "observation_only",
    }
)
ARTIFACT_ID_PATTERN = re.compile(r"art_[a-f0-9]{24}")
ARTIFACT_TYPE_PATTERN = re.compile(r"[a-z][a-z0-9_]{1,80}")


class PlatformRuntimeActionRouter(ActionRouter):
    """Route registered actions without letting the model select an executor."""

    def __init__(
        self,
        *,
        artifact_store: ArtifactStore,
        build_worker: BuildWorker,
        device_gateway: DeviceGateway,
        default_source_connector: Callable[[], dict[str, Any]],
        bridge_artifact_url: Callable[[str, str], str],
        curated_template_root: Path | None = None,
        custom_api_kbs: CustomApiKnowledgeBaseManager | None = None,
        application_flows: ApplicationFlowManager | None = None,
        application_flow_runtime_traces: ApplicationFlowRuntimeTraceManager | None = None,
        code_graphs: CodeGraphManager | None = None,
        experience_evolution: ExperienceEvolutionManager | None = None,
        experimental_flows: ExperimentalFlowManager | None = None,
        parallel_workbench: Any = None,
    ) -> None:
        self.artifact_store = artifact_store
        self.build_worker = build_worker
        self.device_gateway = device_gateway
        self.default_source_connector = default_source_connector
        self.bridge_artifact_url = bridge_artifact_url
        self.custom_api_kbs = custom_api_kbs
        self.application_flows = application_flows
        self.application_flow_runtime_traces = application_flow_runtime_traces
        self.code_graphs = code_graphs
        self.experience_evolution = experience_evolution
        self.experimental_flows = experimental_flows
        self.parallel_workbench = parallel_workbench
        self.curated_template_root = (
            curated_template_root
            if curated_template_root is not None
            else Path(__file__).resolve().parents[1] / "agent-resources" / "hardware-api-templates"
        ).resolve()

    def owner(self, action_id: str) -> str:
        if action_id in RUNTIME_ACTIONS:
            return str(RUNTIME_ACTIONS[action_id]["owner"])
        # Legacy page actions deliberately remain browser-owned while the UI
        # migrates.  Unknown actions are caught by validate before this owner
        # decision can cause a UI event.
        return "ui"

    def can_reuse(
        self,
        action_id: str,
        input_value: dict[str, Any],
        prior_result: dict[str, Any],
        run: dict[str, Any],
    ) -> bool:
        """Keep durable action reuse while rejecting expired control facts."""

        try:
            bind_capture_target(action_id, input_value, run, logical_device_id=DeviceGateway.logical_device_id)
        except PlatformError:
            return False
        if action_id == "scenario_capture.execute":
            request = _record(_record(_record(run.get("context")).get("focused_task")).get("request_context"))
            if input_value.get("repair_request_id") or request.get("repair_request_id"):
                # The ticket owns idempotency and revocation, including an Agent
                # that omitted the optional parameter in its tool arguments.
                return False
            target, required = capture_target(_record(run.get("context")))
            if required:
                return False
            if target:
                prior = _record(prior_result.get("result")) or prior_result
                inputs = _record(prior.get("inputs"))
                return all(inputs.get(key, "") == value for key, value in target.items())

        if action_id in {
            "scenario_capture.catalog", "scenario_capture.read_run", "scenario_capture.discover_devices",
            "parallel_modeling.catalog", "parallel_job.read", "parallel_job.cancel",
            "workload_features.catalog", "workload_features.list_profiles",
        }:
            # Live Bridge presence, task progress and cancellation acknowledgements
            # must be observed again when the Agent polls with the same input.
            return False
        if action_id != "device.create_lease":
            return True
        result = _record(prior_result.get("result")) or prior_result
        lease = _record(result.get("device_lease"))
        lease_id = _text(lease.get("lease_id"), 80)
        return self.device_gateway.lease_is_reusable(
            lease_id,
            project_id=str(run.get("project_id") or ""),
            bridge_id=_text(lease.get("bridge_id"), 80) or _text(input_value.get("bridge_id"), 80),
            logical_device_id=_text(lease.get("logical_device_id"), 160) or _text(input_value.get("logical_device_id"), 160),
            minimum_remaining_seconds=LEASE_REUSE_MIN_REMAINING_SECONDS,
        )

    def validate(self, action_id: str, input_value: dict[str, Any], run: dict[str, Any]) -> None:
        require_action_capability(action_id)
        input_value, run = bind_capture_target(action_id, input_value, run,
            logical_device_id=self.device_gateway.logical_device_id if hasattr(self.device_gateway, "logical_device_id") else DeviceGateway.logical_device_id)
        observe_skill_bootstrap(run, action_id)
        validate_page_analysis_action(action_id, input_value, run)
        action = RUNTIME_ACTIONS.get(action_id)
        if action is None:
            if action_id in LEGACY_BROWSER_DEVICE_ACTIONS:
                raise PlatformError(
                    "ACTION_LEGACY_BROWSER_ONLY",
                    "该真机 action 仅属于历史浏览器流程；当前后端 Agent Run 必须使用 build.describe_profile、build.stage_source、build.create 与 device.*。",
                    status_code=422,
                    details={
                        "action_id": action_id,
                        "replacement_flow": [
                            "build.describe_profile",
                            "build.stage_source",
                            "build.create",
                            "device.deploy_artifact",
                            "device.execute_shell",
                        ],
                    },
                )
            # Registered legacy actions are represented as browser ui.* work;
            # reject unknown strings before exposing them to a page.
            try:
                registered_action(action_id)
            except PlatformError as exc:
                raise PlatformError("ACTION_NOT_REGISTERED", "Agent 请求了未注册 action。", status_code=422, details={"action_id": action_id}) from exc
            return
        missing = sorted(name for name in action["required"] if name not in input_value)
        if missing:
            raise PlatformError("ACTION_INPUT_INVALID", "Action 缺少必填字段。", status_code=422, details={"action_id": action_id, "missing": missing})
        if action_id.startswith(("scenario_capture.", "parallel_modeling.", "workload_features.", "parallel_job.")) and self.parallel_workbench is None:
            raise PlatformError("PARALLEL_WORKBENCH_UNAVAILABLE", "场景采集与并行建模服务未配置。", status_code=503)
        if action_id == SKILL_BOOTSTRAP_ACK_ACTION:
            validate_skill_bootstrap_ack(run, input_value)
        if action_id == "platform.record_experiment_findings":
            self._normalize_experiment_findings(input_value, run)
        if action_id == "platform.list_hardware_api_templates":
            for field in ("unit", "profile_id", "api_family", "query"):
                value = input_value.get(field, "")
                if not isinstance(value, str) or len(value.strip()) > (500 if field == "query" else 120):
                    raise PlatformError(
                        "CURATED_TEMPLATE_FILTER_INVALID",
                        "模板筛选条件不是有效的短文本。",
                        status_code=422,
                        details={"field": field},
                    )
        if action_id == "platform.read_hardware_api_template":
            if not _text(input_value.get("template_id"), 160):
                raise PlatformError(
                    "CURATED_TEMPLATE_ID_INVALID",
                    "读取模板需要 catalog 返回的精确 template_id。",
                    status_code=422,
                )
        if action_id == "build.stage_source":
            if _text(input_value.get("profile_id"), 80) not in BUILD_PROFILE_IDS:
                raise PlatformError("BUILD_PROFILE_INVALID", "Build Worker profile 不受支持。", status_code=422)
            if _text(input_value.get("language"), 16) not in {"c", "cpp"}:
                raise PlatformError("BUILD_LANGUAGE_INVALID", "Build Worker language 只能为 c 或 cpp。", status_code=422)
            if not _text(input_value.get("source"), 192 * 1024):
                raise PlatformError("BUILD_SOURCE_INVALID", "实验源码不能为空。", status_code=422)
            if _text(input_value.get("profile_id"), 80) == "gpu_vulkan" and not _text(input_value.get("shader_source"), 128 * 1024):
                raise PlatformError("BUILD_SHADER_SOURCE_INVALID", "gpu_vulkan profile 需要完整 shader_source。", status_code=422)
        if action_id == "knowledge.build_custom_api_kb":
            if not _text(input_value.get("unit_name"), 80):
                raise PlatformError("CUSTOM_API_KB_UNIT_INVALID", "异构单元名称不能为空。", status_code=422)
            if not _text(input_value.get("source_connector_id"), 160) and not _text(input_value.get("source_root"), 4096):
                raise PlatformError(
                    "CUSTOM_API_KB_SOURCE_REQUIRED",
                    "构建自建 API 知识库需要已登记的代码仓 ID 或服务端路径。",
                    status_code=422,
                )
        if action_id.startswith("application_flow."):
            if self.application_flows is None:
                raise PlatformError("APPLICATION_FLOW_UNAVAILABLE", "应用执行流程重建服务未配置。", status_code=503)
            connector_id = _text(input_value.get("source_connector_id"), 160)
            if action_id not in {"application_flow.list_assets", "application_flow.read_asset"} and not connector_id:
                raise PlatformError("APPLICATION_FLOW_SOURCE_REQUIRED", "应用流程操作需要精确 source_connector_id。", status_code=422)
            if action_id == "application_flow.search_source" and not _text(input_value.get("query"), 240):
                raise PlatformError("APPLICATION_FLOW_QUERY_INVALID", "流程源码搜索需要非空短文本 query。", status_code=422)
            if action_id in {"application_flow.read_source", "application_flow.inspect_trace_instrumentation"} and not _text(input_value.get("path"), 2048):
                raise PlatformError("APPLICATION_FLOW_PATH_INVALID", "流程源码读取需要代码仓内相对路径。", status_code=422)
            if action_id == "application_flow.read_asset":
                if _text(input_value.get("artifact_type"), 80) not in {"application_flow_discovery", "application_flow_scenario"}:
                    raise PlatformError("APPLICATION_FLOW_ARTIFACT_TYPE_INVALID", "只允许读取流程发现或流程场景工件。", status_code=422)
                if not re.fullmatch(r"art_[a-f0-9]{24}", _text(input_value.get("artifact_id"), 80)):
                    raise PlatformError("APPLICATION_FLOW_ARTIFACT_ID_INVALID", "流程工件 ID 非法。", status_code=422)
            if action_id == "application_flow.save_discovery" and not isinstance(input_value.get("discovery"), dict):
                raise PlatformError("APPLICATION_FLOW_DISCOVERY_INVALID", "discovery 必须是结构化对象。", status_code=422)
            if action_id == "application_flow.publish_candidates":
                if not _text(input_value.get("summary_zh"), 4000):
                    raise PlatformError("APPLICATION_FLOW_CANDIDATE_CATALOG_INVALID", "轻量候选目录需要 summary_zh。", status_code=422)
                candidates = input_value.get("candidates")
                if not isinstance(candidates, list) or not candidates:
                    raise PlatformError("APPLICATION_FLOW_CANDIDATE_CATALOG_INVALID", "轻量候选目录需要非空 candidates。", status_code=422)
                graph_id = _text(input_value.get("code_graph_id"), 80)
                if graph_id and self.code_graphs is None:
                    raise PlatformError("CODE_GRAPH_UNAVAILABLE", "代码知识底图服务未配置。", status_code=503)
            if action_id == "application_flow.save_scenario":
                if not isinstance(input_value.get("scenario_json"), dict) or not isinstance(input_value.get("stage_evidence"), list) or not isinstance(input_value.get("unknowns"), list):
                    raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "scenario_json、stage_evidence 或 unknowns 类型非法。", status_code=422)
            if action_id == "application_flow.compile_mermaid":
                if not _text(input_value.get("discovery_artifact_id"), 80) or not _text(input_value.get("flow_id"), 80) or not _text(input_value.get("mermaid"), 256 * 1024):
                    raise PlatformError("APPLICATION_FLOW_MERMAID_METADATA_INVALID", "Mermaid 编译需要 discovery_artifact_id、flow_id 和 mermaid。", status_code=422)
            if action_id == "application_flow.enrich_trace_anchors":
                if (
                    not _text(input_value.get("discovery_artifact_id"), 80)
                    or not _text(input_value.get("scenario_artifact_id"), 80)
                    or not _text(input_value.get("flow_id"), 80)
                    or not isinstance(input_value.get("stage_results"), list)
                ):
                    raise PlatformError(
                        "APPLICATION_FLOW_TRACE_ANCHOR_INVALID",
                        "阶段三需要 discovery、scenario、flow 与逐阶段锚点结果。",
                        status_code=422,
                    )
            if action_id in {"application_flow.inspect_runtime_trace", "application_flow.apply_runtime_trace_alignment"}:
                if self.application_flow_runtime_traces is None:
                    raise PlatformError("APPLICATION_FLOW_RUNTIME_TRACE_UNAVAILABLE", "应用流程真实 Trace 校准服务未配置。", status_code=503)
                input_value["analysis_artifact_id"] = self._resolve_runtime_trace_artifact_id(
                    _text(input_value.get("analysis_artifact_id"), 80),
                    run,
                    required=True,
                )
                required_artifacts = ["analysis_artifact_id", "scenario_artifact_id"]
                if action_id == "application_flow.apply_runtime_trace_alignment":
                    required_artifacts.append("event_catalog_artifact_id")
                if any(not re.fullmatch(r"art_[a-f0-9]{24}", _text(input_value.get(field), 80)) for field in required_artifacts):
                    raise PlatformError("APPLICATION_FLOW_RUNTIME_TRACE_INVALID", "真实 Trace 校准需要合法的工件 ID。", status_code=422)
            if action_id == "application_flow.apply_runtime_trace_alignment":
                if (
                    not _text(input_value.get("flow_id"), 80)
                    or not _text(input_value.get("mermaid"), 256 * 1024)
                    or not isinstance(input_value.get("stage_matches"), list)
                    or not _text(input_value.get("change_summary"), 4000)
                ):
                    raise PlatformError(
                        "APPLICATION_FLOW_RUNTIME_ALIGNMENT_INVALID",
                        "真实 Trace 校准需要 flow、完整 Mermaid、逐阶段匹配和变更摘要。",
                        status_code=422,
                    )
        if action_id.startswith("code_graph."):
            if self.code_graphs is None:
                raise PlatformError("CODE_GRAPH_UNAVAILABLE", "代码知识底图服务未配置。", status_code=503)
            if action_id == "code_graph.build" and not _text(input_value.get("source_connector_id"), 160):
                raise PlatformError("CODE_GRAPH_SOURCE_REQUIRED", "构建代码知识底图需要 source_connector_id。", status_code=422)
            if action_id in {
                "code_graph.get",
                "code_graph.search",
                "code_graph.query_knowledge",
                "code_graph.publish_report",
            } and not _text(input_value.get("graph_id"), 80):
                raise PlatformError("CODE_GRAPH_ID_REQUIRED", "代码知识底图查询需要 graph_id。", status_code=422)
            if action_id == "code_graph.publish_report":
                if not _text(input_value.get("title"), 240) or not _text(input_value.get("markdown"), 512 * 1024):
                    raise PlatformError("CODE_KNOWLEDGE_REPORT_INVALID", "发布代码知识报告需要标题与 Markdown。", status_code=422)
        if action_id.startswith("experimental_flow."):
            if self.experimental_flows is None:
                raise PlatformError("EXPERIMENTAL_FLOW_UNAVAILABLE", "Playground 流程分析服务未配置。", status_code=503)
            artifact_field = (
                "evidence_artifact_id"
                if action_id == "experimental_flow.read_evidence"
                else "result_artifact_id"
                if action_id == "experimental_flow.read_result"
                else ""
            )
            if artifact_field and not ARTIFACT_ID_PATTERN.fullmatch(_text(input_value.get(artifact_field), 80)):
                raise PlatformError("EXPERIMENTAL_FLOW_ARTIFACT_ID_INVALID", "Playground 工件 ID 非法。", status_code=422)
            if action_id == "experimental_flow.publish_result":
                if _text(input_value.get("analysis_kind"), 20) not in {"hiperf", "trace"}:
                    raise PlatformError("EXPERIMENTAL_FLOW_KIND_INVALID", "Playground 分析类型只能为 hiperf 或 trace。", status_code=422)
                if not ARTIFACT_ID_PATTERN.fullmatch(_text(input_value.get("evidence_artifact_id"), 80)):
                    raise PlatformError("EXPERIMENTAL_FLOW_ARTIFACT_ID_INVALID", "Playground 运行数据工件 ID 非法。", status_code=422)
        if action_id == "build.describe_profile":
            profile_id = _text(input_value.get("profile_id"), 80)
            if profile_id and profile_id not in BUILD_PROFILE_IDS:
                raise PlatformError("BUILD_PROFILE_INVALID", "Build Worker profile 不受支持。", status_code=422)
        if action_id == "device.probe":
            if not _text(input_value.get("device_serial"), 160):
                raise PlatformError("DEVICE_PROBE_INPUT_INVALID", "设备 probe 需要上一轮真实 discover 返回的 device_serial。", status_code=422)
        if action_id == "device.execute_shell":
            if not _text(input_value.get("command"), 16 * 1024):
                raise PlatformError("DEVICE_REMOTE_SHELL_INVALID", "设备侧命令不能为空。", status_code=422)

    def _connector(self, run: dict[str, Any]) -> dict[str, Any]:
        context = _record(run.get("context"))
        connector_id = _text(context.get("source_connector_id"), 160)
        default = self.default_source_connector()
        if connector_id and connector_id != default.get("connector_id"):
            # The current single-user action router only has the default
            # connector in process.  Browser-owned repository selection can
            # set a different one before starting a Run.
            return {**default, "connector_id": connector_id}
        return default

    def _resolve_runtime_trace_artifact_id(
        self,
        requested_artifact_id: str,
        run: dict[str, Any],
        *,
        required: bool,
    ) -> str:
        run_context = _record(run.get("context"))
        focused_task = _record(run_context.get("focused_task"))
        application_flow_analysis_id = _text(
            focused_task.get("application_flow_analysis_id")
            or run_context.get("application_flow_analysis_id"),
            160,
        )
        bound_artifact_id = ""
        resolve_bound_trace = getattr(self.application_flow_runtime_traces, "resolve_bound_trace", None)
        if application_flow_analysis_id and callable(resolve_bound_trace):
            bound_artifact_id = _text(resolve_bound_trace(application_flow_analysis_id), 80)
        selected_artifact_id = _text(focused_task.get("trace_analysis_artifact_id"), 80)
        requested = _text(requested_artifact_id, 80)
        candidates: list[str] = []
        for candidate in (bound_artifact_id, selected_artifact_id, requested):
            if candidate and candidate not in candidates:
                candidates.append(candidate)
        for candidate in candidates:
            if not ARTIFACT_ID_PATTERN.fullmatch(candidate):
                continue
            try:
                read_trace = getattr(self.application_flow_runtime_traces, "read_trace", None)
                if callable(read_trace):
                    read_trace(candidate)
                else:
                    self.artifact_store.read(TRACE_ANALYSIS_ARTIFACT_TYPE, candidate)
            except PlatformError as exc:
                if exc.code in {"ARTIFACT_NOT_FOUND", "APPLICATION_FLOW_TRACE_NOT_FOUND"}:
                    continue
                raise
            return candidate
        if not required:
            return ""
        list_traces = getattr(self.application_flow_runtime_traces, "list_traces", None)
        if callable(list_traces):
            available = list_traces(limit=20).get("traces", [])
        else:
            available = self.artifact_store.list_artifacts(TRACE_ANALYSIS_ARTIFACT_TYPE, limit=20)
        raise PlatformError(
            "APPLICATION_FLOW_TRACE_ARTIFACT_UNAVAILABLE",
            "当前页面绑定的真实 Trace 工件在后端不存在；部署环境可能更换了 runtime，请在本页重新导入或重新选择 Trace。",
            status_code=404,
            details={
                "application_flow_analysis_id": application_flow_analysis_id or None,
                "bound_analysis_artifact_id": bound_artifact_id or None,
                "selected_analysis_artifact_id": selected_artifact_id or None,
                "requested_analysis_artifact_id": requested or None,
                "available_analysis_artifact_ids": [item.get("artifact_id") for item in available],
            },
        )

    def _write_artifact(self, artifact_type: str, payload: dict[str, Any], run: dict[str, Any], *, input_artifacts: list[str]) -> dict[str, Any]:
        connector = self._connector(run)
        return self.artifact_store.write(
            artifact_type,
            payload,
            source_connector_id=str(connector["connector_id"]),
            source_binding=_record(connector.get("snapshot")) or {"match_level": "unknown"},
            producer={"name": "platform-agent-runtime", "version": "1.0.0"},
            input_artifacts=input_artifacts,
        )

    @staticmethod
    def _observed_artifact_ids(run: dict[str, Any]) -> set[str]:
        """Return artifact IDs already visible in this Run's typed facts."""

        observed: set[str] = set()

        def visit(value: Any) -> None:
            if isinstance(value, str):
                observed.update(ARTIFACT_ID_PATTERN.findall(value))
                return
            if isinstance(value, dict):
                for item in value.values():
                    visit(item)
                return
            if isinstance(value, list):
                for item in value:
                    visit(item)

        visit(run.get("context"))
        visit(_record(run.get("checkpoint")).get("completed_action_keys"))
        return observed

    @staticmethod
    def _bounded_string_list(value: Any, *, field: str, maximum: int, item_limit: int = 1200) -> list[str]:
        if not isinstance(value, list) or len(value) > maximum:
            raise PlatformError(
                "EXPERIMENT_FINDINGS_INVALID",
                f"{field} 必须为不超过 {maximum} 项的字符串数组。",
                status_code=422,
                details={"field": field, "maximum": maximum},
            )
        result: list[str] = []
        for index, item in enumerate(value):
            if not isinstance(item, str) or not item.strip() or len(item.strip()) > item_limit:
                raise PlatformError(
                    "EXPERIMENT_FINDINGS_INVALID",
                    f"{field}[{index}] 必须为非空且长度受限的字符串。",
                    status_code=422,
                    details={"field": field, "index": index, "max_chars": item_limit},
                )
            result.append(item.strip())
        return result

    def _normalize_experiment_findings(self, input_value: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
        if str(run.get("mode") or "") != "device_validation":
            raise PlatformError(
                "EXPERIMENT_FINDINGS_MODE_INVALID",
                "实验发现只能由真机验证 Run 记录。",
                status_code=409,
            )
        summary_zh = input_value.get("summary_zh")
        if not isinstance(summary_zh, str) or not summary_zh.strip() or len(summary_zh.strip()) > 4000:
            raise PlatformError(
                "EXPERIMENT_FINDINGS_INVALID",
                "summary_zh 必须为 1 到 4000 字符的非空文本。",
                status_code=422,
                details={"field": "summary_zh", "max_chars": 4000},
            )
        limitations = self._bounded_string_list(input_value.get("limitations"), field="limitations", maximum=12)

        raw_evidence = input_value.get("evidence_artifacts")
        if not isinstance(raw_evidence, list) or len(raw_evidence) > 24:
            raise PlatformError(
                "EXPERIMENT_FINDINGS_INVALID",
                "evidence_artifacts 必须为不超过 24 项的工件引用数组。",
                status_code=422,
                details={"field": "evidence_artifacts", "maximum": 24},
            )
        visible_artifacts = self._observed_artifact_ids(run)
        evidence_artifacts: list[dict[str, str]] = []
        evidence_ids: set[str] = set()
        for index, item in enumerate(raw_evidence):
            artifact_type = _text(_record(item).get("artifact_type"), 81)
            artifact_id = _text(_record(item).get("artifact_id"), 40)
            if not ARTIFACT_TYPE_PATTERN.fullmatch(artifact_type) or not ARTIFACT_ID_PATTERN.fullmatch(artifact_id):
                raise PlatformError(
                    "EXPERIMENT_FINDINGS_EVIDENCE_INVALID",
                    "实验发现引用了格式无效的工件。",
                    status_code=422,
                    details={"index": index, "artifact_type": artifact_type, "artifact_id": artifact_id},
                )
            if artifact_id not in visible_artifacts:
                raise PlatformError(
                    "EXPERIMENT_FINDINGS_EVIDENCE_NOT_OBSERVED",
                    "实验发现只能引用当前 Run 已观察到的工件。",
                    status_code=409,
                    details={"index": index, "artifact_type": artifact_type, "artifact_id": artifact_id},
                )
            artifact = self.artifact_store.read(artifact_type, artifact_id)
            evidence_artifacts.append(
                {
                    "artifact_type": artifact_type,
                    "artifact_id": artifact_id,
                    "content_sha256": _text(artifact.get("content_sha256"), 80),
                }
            )
            evidence_ids.add(artifact_id)

        raw_findings = input_value.get("findings")
        if not isinstance(raw_findings, list) or len(raw_findings) > 8:
            raise PlatformError(
                "EXPERIMENT_FINDINGS_INVALID",
                "findings 必须为不超过 8 项的数组；没有新发现时可传空数组。",
                status_code=422,
                details={"field": "findings", "maximum": 8},
            )
        findings: list[dict[str, Any]] = []
        for index, raw in enumerate(raw_findings):
            item = _record(raw)

            def required_text(field: str, limit: int) -> str:
                value = item.get(field)
                if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
                    raise PlatformError(
                        "EXPERIMENT_FINDINGS_INVALID",
                        f"findings[{index}].{field} 必须为非空且长度受限的文本。",
                        status_code=422,
                        details={"field": f"findings[{index}].{field}", "max_chars": limit},
                    )
                return value.strip()

            category = required_text("category", 80)
            reproducibility = required_text("reproducibility", 80)
            if category not in EXPERIMENT_FINDING_CATEGORIES:
                raise PlatformError(
                    "EXPERIMENT_FINDINGS_INVALID",
                    "实验发现 category 不受支持。",
                    status_code=422,
                    details={"index": index, "category": category, "allowed": sorted(EXPERIMENT_FINDING_CATEGORIES)},
                )
            if reproducibility not in EXPERIMENT_FINDING_REPRODUCIBILITY:
                raise PlatformError(
                    "EXPERIMENT_FINDINGS_INVALID",
                    "实验发现 reproducibility 不受支持。",
                    status_code=422,
                    details={"index": index, "reproducibility": reproducibility, "allowed": sorted(EXPERIMENT_FINDING_REPRODUCIBILITY)},
                )
            destinations = self._bounded_string_list(
                item.get("proposed_destinations"),
                field=f"findings[{index}].proposed_destinations",
                maximum=6,
                item_limit=80,
            )
            unknown_destinations = sorted(set(destinations) - EXPERIMENT_FINDING_DESTINATIONS)
            if unknown_destinations:
                raise PlatformError(
                    "EXPERIMENT_FINDINGS_INVALID",
                    "实验发现包含不受支持的 proposed_destinations。",
                    status_code=422,
                    details={"index": index, "unknown": unknown_destinations, "allowed": sorted(EXPERIMENT_FINDING_DESTINATIONS)},
                )
            finding_evidence = self._bounded_string_list(
                item.get("evidence_artifact_ids"),
                field=f"findings[{index}].evidence_artifact_ids",
                maximum=12,
                item_limit=40,
            )
            unknown_evidence = sorted(set(finding_evidence) - evidence_ids)
            if unknown_evidence:
                raise PlatformError(
                    "EXPERIMENT_FINDINGS_EVIDENCE_INVALID",
                    "finding 的 evidence_artifact_ids 必须来自顶层 evidence_artifacts。",
                    status_code=422,
                    details={"index": index, "unknown_artifact_ids": unknown_evidence},
                )
            raw_successful_revision = item.get("successful_revision", "")
            if not isinstance(raw_successful_revision, str) or len(raw_successful_revision.strip()) > 2400:
                raise PlatformError(
                    "EXPERIMENT_FINDINGS_INVALID",
                    f"findings[{index}].successful_revision 必须为不超过 2400 字符的文本。",
                    status_code=422,
                    details={"field": f"findings[{index}].successful_revision", "max_chars": 2400},
                )
            normalized = {
                "title": required_text("title", 240),
                "category": category,
                "observed_behavior": required_text("observed_behavior", 2400),
                "why_it_matters": required_text("why_it_matters", 1600),
                "trigger_conditions": self._bounded_string_list(
                    item.get("trigger_conditions"), field=f"findings[{index}].trigger_conditions", maximum=8
                ),
                "successful_revision": raw_successful_revision.strip(),
                "applicability_scope": required_text("applicability_scope", 1600),
                "limitations": self._bounded_string_list(
                    item.get("limitations"), field=f"findings[{index}].limitations", maximum=8
                ),
                "reproducibility": reproducibility,
                "proposed_destinations": destinations,
                "evidence_artifact_ids": finding_evidence,
            }
            normalized["finding_id"] = f"finding_{sha256_json(normalized)[:16]}"
            findings.append(normalized)

        return {
            "summary_zh": summary_zh.strip(),
            "findings": findings,
            "limitations": limitations,
            "evidence_artifacts": evidence_artifacts,
        }

    @staticmethod
    def _probe_serial(probe: dict[str, Any]) -> str:
        device = _record(probe.get("device"))
        serial = _text(device.get("serial"), 160)
        if not serial:
            raise PlatformError("DEVICE_PROBE_INVALID", "设备 probe 未包含可用 serial。", status_code=409)
        return serial

    def _remote_selector(self, input_value: dict[str, Any], run: dict[str, Any]) -> tuple[str, str, str] | None:
        context_selector = _record(_record(run.get("context")).get("device_selector"))
        bridge_id = _text(input_value.get("bridge_id"), 80) or _text(context_selector.get("bridge_id"), 80)
        lease_id = _text(input_value.get("device_lease_id"), 80) or _text(context_selector.get("device_lease_id"), 80)
        logical_device_id = _text(input_value.get("logical_device_id"), 160) or _text(context_selector.get("logical_device_id"), 160)
        supplied = [bridge_id, lease_id, logical_device_id]
        if not any(supplied):
            return None
        if not bridge_id or not logical_device_id:
            raise PlatformError("DEVICE_BRIDGE_SELECTOR_INVALID", "远程 Device Bridge 需要 bridge_id 和发现结果中的 logical_device_id；设备租约由平台自动管理。", status_code=422)
        return bridge_id, lease_id, logical_device_id

    def _resolve_remote_lease(
        self,
        *,
        selector: tuple[str, str, str],
        run: dict[str, Any],
        device_serial: str,
        input_value: dict[str, Any],
    ) -> dict[str, Any]:
        """Bind a real device identity before acquiring or renewing its lease."""

        bridge_id, requested_lease_id, logical_device_id = selector
        supplied_serial = _text(input_value.get("device_serial"), 160)
        if (
            not device_serial
            or (supplied_serial and supplied_serial != device_serial)
            or self.device_gateway.logical_device_id(device_serial) != logical_device_id
        ):
            raise PlatformError(
                "DEVICE_TARGET_MISMATCH",
                "设备序列号或 probe 与所选逻辑设备不一致，请使用同一设备的发现与探测结果。",
                status_code=409,
                details={"logical_device_id": logical_device_id, "device_serial": device_serial},
            )
        lease_id = requested_lease_id
        if not lease_id:
            lease = self.device_gateway.create_lease(
                project_id=str(run["project_id"]),
                bridge_id=bridge_id,
                logical_device_id=logical_device_id,
            )
            lease_id = _text(lease.get("lease_id"), 80)
        resolution = self.device_gateway.ensure_active_lease(
            lease_id,
            project_id=str(run["project_id"]),
            bridge_id=bridge_id,
            logical_device_id=logical_device_id,
        )
        return {
            **resolution,
            "requested_lease_id": requested_lease_id,
            "automatic": not bool(requested_lease_id),
        }

    def _remote_bridge_id(self, input_value: dict[str, Any], run: dict[str, Any]) -> str:
        context_selector = _record(_record(run.get("context")).get("device_selector"))
        return _text(input_value.get("bridge_id"), 80) or _text(context_selector.get("bridge_id"), 80)

    def _remote_job(
        self,
        *,
        run: dict[str, Any],
        input_value: dict[str, Any],
        action: str,
        payload: dict[str, Any],
        timeout_seconds: int,
    ) -> dict[str, Any]:
        selector = self._remote_selector(input_value, run)
        if selector is None:
            raise PlatformError("DEVICE_BRIDGE_SELECTOR_INVALID", "此操作未选择远程 Device Bridge。", status_code=422)
        bridge_id, _, logical_device_id = selector
        lease_resolution = self._resolve_remote_lease(
            selector=selector,
            run=run,
            device_serial=_text(payload.get("device_serial"), 160),
            input_value=input_value,
        )
        effective_lease_id = _text(lease_resolution.get("effective_lease_id"), 80)
        job = self.device_gateway._bridge_job(
            project_id=str(run["project_id"]),
            agent_run_id=str(run["run_id"]),
            bridge_id=bridge_id,
            lease_id=effective_lease_id,
            logical_device_id=logical_device_id,
            action=action,
            payload=payload,
            idempotency_key=sha256_json({"run": run["run_id"], "action": action, "input": input_value}),
            timeout_seconds=timeout_seconds,
        )
        envelope = self.device_gateway.dispatch_bridge_job(job, wait_seconds=timeout_seconds)
        observed = _record(envelope.get("result")) if isinstance(envelope.get("result"), dict) else envelope
        return {
            "status": _text(observed.get("status"), 40) or "unknown",
            "job": {key: value for key, value in job.items() if key != "signature"},
            "bridge_result": observed,
            "bridge_envelope": {key: value for key, value in envelope.items() if key != "result"},
            "lease_resolution": lease_resolution,
        }

    def _remote_discover(self, *, run: dict[str, Any], input_value: dict[str, Any]) -> dict[str, Any]:
        bridge_id = self._remote_bridge_id(input_value, run)
        if not bridge_id:
            raise PlatformError("DEVICE_BRIDGE_SELECTOR_INVALID", "远程设备发现需要 bridge_id，或在 Run context 中提供 device_selector.bridge_id。", status_code=422)
        job = self.device_gateway._bridge_job(
            project_id=str(run["project_id"]),
            agent_run_id=str(run["run_id"]),
            bridge_id=bridge_id,
            lease_id="",
            logical_device_id="",
            action="device.discover",
            payload={},
            idempotency_key=sha256_json({
                "run": run["run_id"],
                "action": "device.discover",
                "bridge_id": bridge_id,
                "retry_nonce": _text(input_value.get("retry_nonce"), 160),
            }),
            timeout_seconds=60,
        )
        envelope = self.device_gateway.dispatch_bridge_job(job, wait_seconds=60)
        observed = _record(envelope.get("result")) if isinstance(envelope.get("result"), dict) else envelope
        return {
            "status": _text(observed.get("status"), 40) or "unknown",
            "executor": "bridge",
            "job": {key: value for key, value in job.items() if key != "signature"},
            "devices": observed.get("devices") if isinstance(observed.get("devices"), list) else [],
            "bridge_result": observed,
        }

    def execute(self, action_id: str, input_value: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
        require_action_capability(action_id)
        input_value, run = bind_capture_target(action_id, input_value, run,
            logical_device_id=self.device_gateway.logical_device_id if hasattr(self.device_gateway, "logical_device_id") else DeviceGateway.logical_device_id)
        if action_id in {"device.execute_shell", "device.collect_artifact", "device.deploy_artifact"}:
            target, _ = capture_target(_record(run.get("context")))
            if target:
                probe = self.artifact_store.read("device_probe", _text(input_value.get("device_probe_artifact_id"), 120))
                if self._probe_serial(probe) != target["device_serial"]:
                    raise PlatformError("SCENARIO_DEVICE_TARGET_MISMATCH", "设备探测记录属于另一台手机，请沿用本次选定设备。", status_code=409,
                                        details={"selected_device": target})
        project_id = str(run["project_id"])
        run_context = _record(run.get("context"))
        focused_task_context = _record(run_context.get("focused_task"))
        application_flow_analysis_id = _application_flow_analysis_scope(action_id, run)
        requested_profile = _record(run_context.get("skill_profile"))
        profile_id = (
            _text(input_value.get("profile_id"), 120)
            or _text(requested_profile.get("profile_id"), 120)
            or _text(run_context.get("skill_profile_id"), 120)
            or None
        )
        if action_id == SKILL_BOOTSTRAP_ACK_ACTION:
            return {
                "status": "succeeded",
                "acknowledgement": validate_skill_bootstrap_ack(run, input_value),
                "summary_zh": "当前 Agent Session 已回传主规则与代码理解 Skill 的发现状态。",
            }
        if action_id == "skills.list":
            return {"status": "succeeded", **skill_management_catalog(profile_id)}
        if action_id == "skills.read":
            return {
                "status": "succeeded",
                **skill_management_detail(
                    _text(input_value.get("skill_id"), 64),
                    profile_id=profile_id,
                    version_id=_text(input_value.get("version_id"), 80) or None,
                ),
            }
        if action_id == "skills.save_user_version":
            return save_user_skill_version(
                skill_id=_text(input_value.get("skill_id"), 64),
                label_zh=_text(input_value.get("label_zh"), 120),
                description_zh=_text(input_value.get("description_zh"), 800),
                content=_text(input_value.get("content"), 64 * 1024),
                profile_id=profile_id,
                prompt_slot=_text(input_value.get("prompt_slot"), 80),
                change_summary_zh=_text(input_value.get("change_summary_zh"), 800),
                created_by="page_agent",
            )
        if action_id == "skills.activate_version":
            return activate_user_skill_version(
                _text(input_value.get("skill_id"), 64),
                _text(input_value.get("version_id"), 80) or None,
                profile_id=profile_id,
            )
        if action_id.startswith("experience."):
            if self.experience_evolution is None:
                raise PlatformError("EXPERIENCE_EVOLUTION_UNAVAILABLE", "经验演进服务未配置。", status_code=503)
            if action_id == "experience.list_sources":
                return {"status": "succeeded", **self.experience_evolution.source_catalog(limit=int(input_value.get("limit") or 100))}
            if action_id == "experience.read_source":
                return {
                    "status": "succeeded",
                    **self.experience_evolution.read_source(
                        _text(input_value.get("source_type"), 80),
                        _text(input_value.get("source_id"), 160),
                        after=max(0, int(input_value.get("after") or 0)),
                        limit=max(1, int(input_value.get("limit") or 500)),
                    ),
                }
            if action_id == "experience.list_cases":
                return {"status": "succeeded", **self.experience_evolution.catalog()}
            if action_id == "experience.save_review":
                return self.experience_evolution.save_review(input_value, agent_run_id=str(run["run_id"]))
            if action_id == "experience.triage_case":
                return self.experience_evolution.triage_case(
                    _text(input_value.get("case_id"), 80),
                    status=_text(input_value.get("status"), 40),
                    selected_resolution_id=_text(input_value.get("selected_resolution_id"), 80),
                    operator_note=_text(input_value.get("operator_note"), 4000),
                    actor="page_agent",
                )
        if action_id == "code_graph.list":
            assert self.code_graphs is not None
            graphs = self.code_graphs.list_graphs(
                source_connector_id=_text(input_value.get("source_connector_id"), 160),
                limit=max(1, min(int(input_value.get("limit") or 100), 500)),
            )
            return {
                "status": "succeeded",
                "schema_version": "module_code_graph_catalog/v1",
                "extractor_available": self.code_graphs.available(),
                "graphs": graphs,
                "count": len(graphs),
            }
        if action_id == "code_graph.build":
            assert self.code_graphs is not None
            return self.code_graphs.start_build(
                _text(input_value.get("source_connector_id"), 160),
                subpath=_text(input_value.get("subpath"), 2048),
                max_files=max(1, min(int(input_value.get("max_files") or 50_000), 500_000)),
            )
        if action_id == "code_graph.get":
            assert self.code_graphs is not None
            return {"status": "succeeded", "graph": self.code_graphs.get_graph(_text(input_value.get("graph_id"), 80))}
        if action_id == "code_graph.search":
            assert self.code_graphs is not None
            return {
                "status": "succeeded",
                **self.code_graphs.search(
                    _text(input_value.get("graph_id"), 80),
                    query=_text(input_value.get("query"), 1000),
                    limit=max(1, min(int(input_value.get("limit") or 30), 100)),
                ),
            }
        if action_id == "code_graph.query_knowledge":
            assert self.code_graphs is not None
            return {
                "status": "succeeded",
                **self.code_graphs.query_knowledge(
                    _text(input_value.get("graph_id"), 80),
                    query=_text(input_value.get("query"), 4000),
                    limit=max(1, min(int(input_value.get("limit") or 15), 30)),
                ),
            }
        if action_id == "code_graph.publish_report":
            assert self.code_graphs is not None
            report = self.code_graphs.publish_report(
                _text(input_value.get("graph_id"), 80),
                title=_text(input_value.get("title"), 240),
                markdown=_text(input_value.get("markdown"), 512 * 1024),
                question=_text(input_value.get("question"), 8000),
                parent_report_id=_text(input_value.get("parent_report_id"), 80),
                agent_run_id=str(run["run_id"]),
            )
            return {
                "status": "succeeded",
                "report": {key: value for key, value in report.items() if key != "markdown"},
                "summary_zh": f"代码知识报告已发布：{report['title']}（第 {report['revision']} 版）。",
            }
        if action_id == "experimental_flow.read_evidence":
            assert self.experimental_flows is not None
            return {
                "status": "succeeded",
                **self.experimental_flows.read_evidence(_text(input_value.get("evidence_artifact_id"), 80)),
            }
        if action_id == "experimental_flow.read_result":
            assert self.experimental_flows is not None
            return {
                "status": "succeeded",
                "result": self.experimental_flows.read_result(_text(input_value.get("result_artifact_id"), 80)),
            }
        if action_id == "experimental_flow.publish_result":
            assert self.experimental_flows is not None
            source_scopes = input_value.get("source_scopes")
            if not isinstance(source_scopes, list) or not source_scopes:
                source_scopes = _record(_record(run.get("context")).get("focused_task")).get("source_scopes")
            result = self.experimental_flows.publish_result(
                analysis_kind=_text(input_value.get("analysis_kind"), 20),
                source_connector_id=_text(input_value.get("source_connector_id"), 160),
                flow_goal=_text(input_value.get("flow_goal"), 12_000),
                title=_text(input_value.get("title"), 240),
                mermaid=_text(input_value.get("mermaid"), 512 * 1024),
                summary_markdown=_text(input_value.get("summary_markdown"), 512 * 1024),
                evidence_artifact_id=_text(input_value.get("evidence_artifact_id"), 80),
                subpath=_text(input_value.get("subpath"), 2048),
                source_scopes=[
                    dict(item)
                    for item in source_scopes
                    if isinstance(item, dict)
                ] if isinstance(source_scopes, list) else [],
                parent_result_artifact_id=_text(input_value.get("parent_result_artifact_id"), 80),
                agent_run_id=str(run["run_id"]),
            )
            return {
                "status": "succeeded",
                "result": {key: value for key, value in result.items() if key not in {"mermaid", "summary_markdown"}},
                "summary_zh": f"Playground 流程图已发布：{result['title']}（第 {result['revision']} 版）。",
            }
        if action_id.startswith(("scenario_capture.", "parallel_modeling.", "workload_features.", "parallel_job.")):
            if action_id == "scenario_capture.execute":
                repair_id = _text(_record(focused_task_context.get("request_context")).get("repair_request_id"), 80)
                if repair_id:
                    if input_value.get("repair_request_id") and input_value["repair_request_id"] != repair_id:
                        raise PlatformError("CAPTURE_REPAIR_CONTEXT_MISMATCH", "自动重试与当前采集修复请求不一致。", status_code=409)
                    input_value = {**input_value, "repair_request_id": repair_id}
            return self.parallel_workbench.action(action_id, input_value, agent_run_id=str(run["run_id"]))
        connector = self._connector(run)
        if action_id == "application_flow.list_assets":
            if self.application_flows is None:
                raise PlatformError("APPLICATION_FLOW_UNAVAILABLE", "应用执行流程重建服务未配置。", status_code=503)
            return {
                "status": "succeeded",
                **self.application_flows.list_assets(
                    source_connector_id=_text(input_value.get("source_connector_id"), 160),
                    application_flow_analysis_id=application_flow_analysis_id,
                    limit=max(1, min(int(input_value.get("limit") or 100), 200)),
                ),
                "registered_source_connectors": [
                    {
                        "source_connector_id": item.get("connector_id"),
                        "display_name": item.get("display_name"),
                        "source_root": item.get("source_root"),
                        "source_snapshot_id": _record(item.get("snapshot")).get("snapshot_id"),
                    }
                    for item in self.application_flows.connectors.list()
                ],
            }
        if action_id == "application_flow.inspect_repository":
            assert self.application_flows is not None
            return {
                "status": "succeeded",
                **self.application_flows.inspect_repository(
                    _text(input_value.get("source_connector_id"), 160),
                    subpath=_text(input_value.get("subpath"), 2048),
                ),
            }
        if action_id == "application_flow.search_source":
            assert self.application_flows is not None
            return {
                "status": "succeeded",
                **self.application_flows.search_source(
                    _text(input_value.get("source_connector_id"), 160),
                    query=_text(input_value.get("query"), 240),
                    subpath=_text(input_value.get("subpath"), 2048),
                    max_results=max(1, min(int(input_value.get("max_results") or 30), 80)),
                ),
            }
        if action_id == "application_flow.read_source":
            assert self.application_flows is not None
            return {
                "status": "succeeded",
                **self.application_flows.read_source(
                    _text(input_value.get("source_connector_id"), 160),
                    path=_text(input_value.get("path"), 2048),
                    line_start=max(1, int(input_value.get("line_start") or 1)),
                    line_end=max(1, int(input_value.get("line_end") or 240)),
                ),
            }
        if action_id == "application_flow.inspect_trace_instrumentation":
            assert self.application_flows is not None
            return {
                "status": "succeeded",
                **self.application_flows.inspect_trace_instrumentation(
                    _text(input_value.get("source_connector_id"), 160),
                    path=_text(input_value.get("path"), 2048),
                    line=max(1, int(input_value.get("line") or 1)),
                    symbol=_text(input_value.get("symbol"), 1000),
                    radius=max(40, min(int(input_value.get("radius") or 320), 1200)),
                    max_results=max(1, min(int(input_value.get("max_results") or 40), 100)),
                ),
            }
        if action_id == "application_flow.inspect_runtime_trace":
            assert self.application_flow_runtime_traces is not None
            if application_flow_analysis_id:
                assert self.application_flows is not None
                self.application_flows.require_analysis_scope(
                    "application_flow_scenario",
                    _text(input_value.get("scenario_artifact_id"), 80),
                    application_flow_analysis_id,
                )
            return self.application_flow_runtime_traces.inspect_trace(
                analysis_artifact_id=_text(input_value.get("analysis_artifact_id"), 80),
                scenario_artifact_id=_text(input_value.get("scenario_artifact_id"), 80),
                query=_text(input_value.get("query"), 1000),
                global_limit=max(20, min(int(input_value.get("global_limit") or 120), 300)),
                per_stage_limit=max(1, min(int(input_value.get("per_stage_limit") or 12), 30)),
            )
        if action_id == "application_flow.read_asset":
            assert self.application_flows is not None
            artifact_type = _text(input_value.get("artifact_type"), 80)
            artifact_id = _text(input_value.get("artifact_id"), 80)
            artifact = self.application_flows.require_analysis_scope(
                artifact_type,
                artifact_id,
                application_flow_analysis_id,
            ) if application_flow_analysis_id else (
                self.application_flows.read_discovery(artifact_id)
                if artifact_type == "application_flow_discovery"
                else self.application_flows.read_scenario(artifact_id)
            )
            return {"status": "succeeded", "artifact": artifact}
        if action_id == "application_flow.save_discovery":
            assert self.application_flows is not None
            return self.application_flows.save_discovery(
                _text(input_value.get("source_connector_id"), 160),
                _record(input_value.get("discovery")),
                subpath=_text(input_value.get("subpath"), 2048),
                analysis_context={
                    "trace_analysis_artifact_id": _text(
                        focused_task_context.get("trace_analysis_artifact_id"),
                        80,
                    ) or None,
                    "source_scopes": focused_task_context.get("source_scopes")
                    if isinstance(focused_task_context.get("source_scopes"), list)
                    else [],
                },
                application_flow_analysis_id=application_flow_analysis_id,
            )
        if action_id == "application_flow.publish_candidates":
            assert self.application_flows is not None
            graph_artifact_id = ""
            graph_id = _text(input_value.get("code_graph_id"), 80)
            if graph_id:
                assert self.code_graphs is not None
                graph = self.code_graphs.validate_ready_graph(
                    graph_id,
                    source_connector_id=_text(input_value.get("source_connector_id"), 160),
                    scope=_text(input_value.get("subpath"), 2048),
                )
                graph_artifact_id = _text(graph.get("artifact_id"), 80)
            normalized_source_scopes: list[dict[str, Any]] = []
            for raw_scope in input_value.get("source_scopes", []) if isinstance(input_value.get("source_scopes"), list) else []:
                scope = _record(raw_scope)
                scope_connector_id = _text(scope.get("source_connector_id"), 160)
                if not scope_connector_id:
                    continue
                scope_subpath = _text(scope.get("subpath"), 2048)
                scope_graph_id = _text(scope.get("code_graph_id"), 80)
                scope_graph_artifact_id = ""
                if scope_graph_id:
                    assert self.code_graphs is not None
                    scope_graph = self.code_graphs.validate_ready_graph(
                        scope_graph_id,
                        source_connector_id=scope_connector_id,
                        source_snapshot_id=_text(scope.get("source_snapshot_id"), 256) or None,
                        scope=scope_subpath,
                    )
                    scope_graph_artifact_id = _text(scope_graph.get("artifact_id"), 80)
                normalized_source_scopes.append({
                    "source_connector_id": scope_connector_id,
                    "source_snapshot_id": _text(scope.get("source_snapshot_id"), 256),
                    "subpath": scope_subpath,
                    "code_graph_artifact_id": scope_graph_artifact_id or None,
                })
            return self.application_flows.save_candidate_catalog(
                _text(input_value.get("source_connector_id"), 160),
                summary_zh=_text(input_value.get("summary_zh"), 4000),
                candidates=[_record(item) for item in input_value.get("candidates", []) if isinstance(item, dict)],
                subpath=_text(input_value.get("subpath"), 2048),
                unknowns=[str(item) for item in input_value.get("unknowns", []) if isinstance(item, str)],
                analysis_goal=_text(input_value.get("analysis_goal"), 4000),
                code_graph_artifact_id=graph_artifact_id,
                source_scopes=normalized_source_scopes or None,
                parent_discovery_artifact_id=_text(input_value.get("parent_discovery_artifact_id"), 80),
                refinement_request=_text(input_value.get("refinement_request"), 4000),
                application_flow_analysis_id=application_flow_analysis_id,
                trace_analysis_artifact_id=_text(
                    focused_task_context.get("trace_analysis_artifact_id"),
                    80,
                ),
            )
        if action_id == "application_flow.save_scenario":
            assert self.application_flows is not None
            if application_flow_analysis_id:
                self.application_flows.require_analysis_scope(
                    "application_flow_discovery",
                    _text(input_value.get("discovery_artifact_id"), 80),
                    application_flow_analysis_id,
                )
            return self.application_flows.save_scenario(
                _text(input_value.get("source_connector_id"), 160),
                discovery_artifact_id=_text(input_value.get("discovery_artifact_id"), 80),
                flow_id=_text(input_value.get("flow_id"), 80),
                scenario_json=_record(input_value.get("scenario_json")),
                mermaid=_text(input_value.get("mermaid"), 256 * 1024),
                stage_evidence=input_value.get("stage_evidence") if isinstance(input_value.get("stage_evidence"), list) else [],
                unknowns=input_value.get("unknowns") if isinstance(input_value.get("unknowns"), list) else [],
                parent_scenario_artifact_id=_text(input_value.get("parent_scenario_artifact_id"), 80),
                revision_request=_text(input_value.get("revision_request"), 4000),
            )
        if action_id == "application_flow.compile_mermaid":
            assert self.application_flows is not None
            if application_flow_analysis_id:
                self.application_flows.require_analysis_scope(
                    "application_flow_discovery",
                    _text(input_value.get("discovery_artifact_id"), 80),
                    application_flow_analysis_id,
                )
            result = self.application_flows.compile_mermaid(
                _text(input_value.get("source_connector_id"), 160),
                discovery_artifact_id=_text(input_value.get("discovery_artifact_id"), 80),
                flow_id=_text(input_value.get("flow_id"), 80),
                mermaid=_text(input_value.get("mermaid"), 256 * 1024),
                parent_scenario_artifact_id=_text(input_value.get("parent_scenario_artifact_id"), 80),
                revision_request=_text(input_value.get("revision_request"), 4000),
            )
            trace_artifact_id = self._resolve_runtime_trace_artifact_id("", run, required=False)
            scenario_artifact_id = _text(_record(result.get("artifact")).get("artifact_id"), 80)
            if trace_artifact_id and scenario_artifact_id:
                result["runtime_trace_next_step"] = {
                    "action_id": "application_flow.inspect_runtime_trace",
                    "input": {
                        "source_connector_id": _text(input_value.get("source_connector_id"), 160),
                        "analysis_artifact_id": trace_artifact_id,
                        "scenario_artifact_id": scenario_artifact_id,
                    },
                    "note": "当前页面已绑定一份可读取的真实 Trace；使用这些已校验 ID 获取事件目录。",
                }
            return result
        if action_id == "application_flow.enrich_trace_anchors":
            assert self.application_flows is not None
            if application_flow_analysis_id:
                self.application_flows.require_analysis_scope(
                    "application_flow_discovery",
                    _text(input_value.get("discovery_artifact_id"), 80),
                    application_flow_analysis_id,
                )
                self.application_flows.require_analysis_scope(
                    "application_flow_scenario",
                    _text(input_value.get("scenario_artifact_id"), 80),
                    application_flow_analysis_id,
                )
            return self.application_flows.enrich_trace_anchors(
                _text(input_value.get("source_connector_id"), 160),
                discovery_artifact_id=_text(input_value.get("discovery_artifact_id"), 80),
                scenario_artifact_id=_text(input_value.get("scenario_artifact_id"), 80),
                flow_id=_text(input_value.get("flow_id"), 80),
                stage_results=[
                    _record(item)
                    for item in input_value.get("stage_results", [])
                    if isinstance(item, dict)
                ],
            )
        if action_id == "application_flow.apply_runtime_trace_alignment":
            assert self.application_flow_runtime_traces is not None
            if application_flow_analysis_id:
                assert self.application_flows is not None
                self.application_flows.require_analysis_scope(
                    "application_flow_scenario",
                    _text(input_value.get("scenario_artifact_id"), 80),
                    application_flow_analysis_id,
                )
            return self.application_flow_runtime_traces.apply_alignment(
                source_connector_id=_text(input_value.get("source_connector_id"), 160),
                analysis_artifact_id=_text(input_value.get("analysis_artifact_id"), 80),
                event_catalog_artifact_id=_text(input_value.get("event_catalog_artifact_id"), 80),
                scenario_artifact_id=_text(input_value.get("scenario_artifact_id"), 80),
                flow_id=_text(input_value.get("flow_id"), 80),
                mermaid=_text(input_value.get("mermaid"), 256 * 1024),
                stage_matches=[_record(item) for item in input_value.get("stage_matches", []) if isinstance(item, dict)],
                node_matches=[_record(item) for item in input_value.get("node_matches", []) if isinstance(item, dict)],
                change_summary=_text(input_value.get("change_summary"), 4000),
                limitations=[str(item) for item in input_value.get("limitations", []) if isinstance(item, str)],
            )
        if action_id == "knowledge.list_custom_api_kbs":
            if self.custom_api_kbs is None:
                raise PlatformError("CUSTOM_API_KB_UNAVAILABLE", "自建 API 知识库服务未配置。", status_code=503)
            return {
                "status": "succeeded",
                "knowledge_bases": self.custom_api_kbs.list(),
                "registered_source_connectors": [
                    {
                        "source_connector_id": item.get("connector_id"),
                        "display_name": item.get("display_name"),
                        "source_root": item.get("source_root"),
                        "source_snapshot_id": _record(item.get("snapshot")).get("snapshot_id"),
                    }
                    for item in self.custom_api_kbs.connectors.list()
                ],
                "platform_merge_policy": "developer_only",
            }
        if action_id == "knowledge.build_custom_api_kb":
            if self.custom_api_kbs is None:
                raise PlatformError("CUSTOM_API_KB_UNAVAILABLE", "自建 API 知识库服务未配置。", status_code=503)
            result = self.custom_api_kbs.build(
                unit_name=_text(input_value.get("unit_name"), 80),
                source_connector_id=_text(input_value.get("source_connector_id"), 160),
                source_root=_text(input_value.get("source_root"), 4096),
            )
            metadata = _record(result.get("knowledge_base"))
            summary = _record(metadata.get("summary"))
            quality = _record(metadata.get("quality"))
            quality_passed = quality.get("status") == "pass"
            return {
                "status": "succeeded",
                **result,
                "summary_zh": (
                    f"已生成 {metadata.get('display_name')}，共 {summary.get('record_count', 0)} 条 API 合同候选；"
                    f"公开/契约 {summary.get('public_contract_record_count', 0)} 条，"
                    f"算子 {summary.get('operator_record_count', 0)} 条，"
                    f"内部实现 {summary.get('internal_implementation_record_count', 0)} 条。"
                    f"质量检查{'通过' if quality_passed else '需要复核'}。"
                    "该自建库与平台内置库分离，只有开发者可以执行发布合并。"
                ),
            }
        if action_id == "platform.list_hardware_api_templates":
            try:
                listing = list_curated_templates(
                    self.curated_template_root,
                    unit=_text(input_value.get("unit"), 120),
                    profile_id=_text(input_value.get("profile_id"), 120),
                    api_family=_text(input_value.get("api_family"), 120),
                    query=_text(input_value.get("query"), 500),
                )
            except CuratedTemplateError as exc:
                raise PlatformError(
                    "CURATED_TEMPLATE_CATALOG_INVALID",
                    "受审查硬件 API 模板目录未通过完整性校验。",
                    status_code=500,
                    details={"reason": str(exc)},
                ) from exc
            return {"status": "succeeded", **listing}
        if action_id == "platform.read_hardware_api_template":
            try:
                bundle = read_curated_template(
                    self.curated_template_root,
                    _text(input_value.get("template_id"), 160),
                )
            except CuratedTemplateNotFoundError as exc:
                raise PlatformError(
                    "CURATED_TEMPLATE_NOT_FOUND",
                    "请求的受审查硬件 API 模板未登记。",
                    status_code=404,
                    details={"template_id": _text(input_value.get("template_id"), 160)},
                ) from exc
            except CuratedTemplateError as exc:
                raise PlatformError(
                    "CURATED_TEMPLATE_CATALOG_INVALID",
                    "受审查硬件 API 模板目录未通过完整性校验。",
                    status_code=500,
                    details={"reason": str(exc)},
                ) from exc
            return {"status": "succeeded", **bundle}
        if action_id == "platform.record_experiment_findings":
            normalized = self._normalize_experiment_findings(input_value, run)
            task_context_sha256 = sha256_json({"mode": run.get("mode"), "goal": run.get("goal"), "context": run.get("context")})
            artifact = self._write_artifact(
                "experiment_learning_report",
                {
                    "schema_version": "experiment_learning_report/v1",
                    "agent_run_id": str(run["run_id"]),
                    "analysis_record_id": _text(_record(run.get("context")).get("analysis_record_id"), 160) or None,
                    "task_context_sha256": task_context_sha256,
                    "capture_status": "captured_unreviewed",
                    **normalized,
                    "run_evidence": {
                        "agent_run_id": str(run["run_id"]),
                        "execution_trace_file": "analysis_execution_trace.jsonl",
                    },
                    "promotion_contract": {
                        "automatic_skill_or_kb_update": False,
                        "required_before_release": [
                            "triage_destination",
                            "reproduce_or_explicitly_scope_single_observation",
                            "add_failure_and_success_regression",
                            "versioned_change",
                            "review_and_rollback_target",
                        ],
                        "claim_boundary_zh": "该工件只记录本次实验观察与候选改进方向；未经复现、回归和审阅，不得成为全局运行时事实、硬件能力结论或生产 Skill。",
                    },
                },
                run,
                input_artifacts=[f"artifact:{item['artifact_id']}" for item in normalized["evidence_artifacts"]],
            )
            return {
                "status": "succeeded",
                "experiment_learning_report_artifact_id": artifact["artifact_id"],
                "capture_status": artifact["capture_status"],
                "finding_count": len(artifact["findings"]),
                "task_context_sha256": task_context_sha256,
                "summary_zh": "已记录实验发现候选；该记录尚未复现或发布，不会自动修改 Skill、构建 profile 或知识库。",
            }
        if action_id == "build.describe_profile":
            profile_id = _text(input_value.get("profile_id"), 80) or None
            header_query = _text(input_value.get("header_query"), 120)
            profiles = self.build_worker.describe_profile(
                profile_id=profile_id or "",
                header_query=header_query,
            )
            artifact = self._write_artifact(
                "build_profile_surface",
                {
                    "schema_version": "build_profile_surface/v1",
                    "profile_id": profile_id,
                    "header_query": header_query,
                    "profiles": profiles,
                    "summary_zh": "Build Worker 返回固定 SDK/header 与链接面的真实构建事实；它不证明设备支持或实验可运行。",
                },
                run,
                input_artifacts=[],
            )
            return {
                "status": "succeeded",
                "build_profile_artifact_id": artifact["artifact_id"],
                "profiles": profiles,
            }
        if action_id == "build.stage_source":
            artifact = self.build_worker.stage_source(
                project_id=project_id,
                source_connector_id=str(connector["connector_id"]),
                source_binding=_record(connector.get("snapshot")) or {"match_level": "unknown"},
                profile_id=_text(input_value.get("profile_id"), 80),
                language=_text(input_value.get("language"), 16),
                title=_text(input_value.get("title"), 160),
                source=_text(input_value.get("source"), 192 * 1024),
                agent_run_id=str(run["run_id"]),
                shader_source=_text(input_value.get("shader_source"), 128 * 1024),
            )
            return {"status": "succeeded", "source_artifact_id": artifact["artifact_id"], "profile_id": artifact["profile_id"], "source_sha256": artifact["source_sha256"]}
        if action_id == "build.create":
            source = self.artifact_store.read("build_source", _text(input_value.get("source_artifact_id"), 120))
            target = _record(input_value.get("target"))
            artifact = self.build_worker.build(
                project_id=project_id,
                agent_run_id=str(run["run_id"]),
                source_artifact=source,
                profile_id=_text(input_value.get("profile_id"), 80),
                target=target,
                timeout_seconds=int(input_value.get("timeout_seconds", 240)),
            )
            diagnostic = _record(artifact.get("diagnostic"))
            result = {
                "status": artifact["status"],
                "build_result_artifact_id": artifact["artifact_id"],
                "build_identity": artifact["build_identity"],
                "summary_zh": artifact["summary_zh"],
                "diagnostic": diagnostic,
            }
            if artifact["status"] != "succeeded":
                manifest_error = _record(_record(artifact.get("manifest")).get("error"))
                diagnostic_error = _record(diagnostic.get("error"))
                result["error"] = diagnostic_error or manifest_error or {
                    "code": "BUILD_FAILED",
                    "message": artifact["summary_zh"],
                    "details": {"diagnostic_stage": diagnostic.get("stage", "unknown")},
                }
            return result
        if action_id == "device.list_bridges":
            bridges = self.device_gateway.list_bridges(project_id=project_id)
            return {
                "status": "succeeded",
                "bridges": bridges,
                "summary_zh": "仅返回当前项目中已登记 Bridge 的连接状态与 capability；选择 Bridge 本身不等于获得设备租约。",
            }
        if action_id == "device.create_lease":
            lease = self.device_gateway.create_lease(
                project_id=project_id,
                bridge_id=_text(input_value.get("bridge_id"), 80),
                logical_device_id=_text(input_value.get("logical_device_id"), 160),
                ttl_seconds=int(input_value.get("ttl_seconds", DEFAULT_LEASE_SECONDS)),
            )
            return {
                "status": "succeeded",
                "device_lease": lease,
                "summary_zh": "已取得设备租约；后续远程 device.* action 指定同一 Bridge 和逻辑设备即可，平台自动管理租约并保留审计链。",
            }
        if action_id == "device.discover":
            if self._remote_bridge_id(input_value, run):
                return self._remote_discover(run=run, input_value=input_value)
            result = self.device_gateway.local_discover()
            hdc = _record(result.get("hdc"))
            if hdc.get("available") is not True:
                return {
                    "status": "failed",
                    "executor": "local_hdc",
                    **result,
                    "error": {
                        "code": _text(hdc.get("error_code"), 120) or "DEVICE_HDC_UNAVAILABLE",
                        "message": _text(hdc.get("diagnostic"), 1200) or "本机 HDC 不可用。",
                    },
                }
            return {"status": "succeeded", "executor": "local_hdc", **result}
        if action_id == "device.probe":
            serial = _text(input_value.get("device_serial"), 160)
            lease_resolution: dict[str, Any] = {}
            if self._remote_selector(input_value, run):
                result = self._remote_job(run=run, input_value=input_value, action=action_id, payload={"device_serial": serial}, timeout_seconds=120)
                payload = _record(result.get("bridge_result"))
                result_status = _text(result.get("status"), 40) or "unknown"
                lease_resolution = _record(result.get("lease_resolution"))
            else:
                payload = self.device_gateway.local_probe(serial)
                result_status = "succeeded"
            artifact = self._write_artifact("device_probe", payload, run, input_artifacts=[])
            return {
                "status": result_status,
                "probe_artifact_id": artifact["artifact_id"],
                "device_serial": serial,
                "probe": payload,
                **({"lease_resolution": lease_resolution} if lease_resolution else {}),
            }
        if action_id == "device.deploy_artifact":
            build = self.artifact_store.read("build_result", _text(input_value.get("build_result_artifact_id"), 120))
            probe = self.artifact_store.read("device_probe", _text(input_value.get("device_probe_artifact_id"), 120))
            serial = self._probe_serial(probe)
            binary = self.build_worker.binary_path(build)
            manifest = _record(build.get("manifest"))
            binary_info = _record(manifest.get("binary"))
            binary_sha = _text(binary_info.get("sha256"), 80)
            remote_path = f"/data/local/tmp/hetero_runs/{str(build['build_identity'])[:24]}/experiment.bin"
            timeout = int(input_value.get("timeout_seconds", 240))
            if self._remote_selector(input_value, run):
                selector = self._remote_selector(input_value, run)
                assert selector is not None
                bridge_id, _, logical_device_id = selector
                lease_resolution = self._resolve_remote_lease(
                    selector=selector,
                    run=run,
                    device_serial=serial,
                    input_value=input_value,
                )
                effective_lease_id = _text(lease_resolution.get("effective_lease_id"), 80)
                # The bridge gets only a short-lived opaque URL and hash.
                job = self.device_gateway._bridge_job(
                    project_id=project_id,
                    agent_run_id=str(run["run_id"]),
                    bridge_id=bridge_id,
                    lease_id=effective_lease_id,
                    logical_device_id=logical_device_id,
                    action="device.deploy_artifact",
                    payload={"artifact_sha256": binary_sha, "remote_path": remote_path, "device_serial": serial},
                    idempotency_key=_device_deployment_idempotency_key(
                        run_id=str(run["run_id"]),
                        build_artifact_id=str(build["artifact_id"]),
                        probe_artifact_id=str(probe["artifact_id"]),
                        retry_nonce=_text(input_value.get("retry_nonce"), 160),
                    ),
                    timeout_seconds=timeout,
                )
                token = self.device_gateway.issue_download_grant(job_id=str(job["job_id"]), bridge_id=bridge_id, path=binary, expected_sha256=binary_sha)
                job["payload"]["download_url"] = self.bridge_artifact_url(str(job["job_id"]), token)
                job["signature"] = self.device_gateway._sign_job(_text(_record(_record(self.device_gateway._read_registry().get("bridges")).get(bridge_id)).get("bridge_secret"), 200), job)
                envelope = self.device_gateway.dispatch_bridge_job(job, wait_seconds=timeout)
                deployment = {
                    "executor": "bridge",
                    "job_id": job["job_id"],
                    **(_record(envelope.get("result")) if isinstance(envelope.get("result"), dict) else {"status": "unknown"}),
                    "bridge_envelope": {key: value for key, value in envelope.items() if key != "result"},
                    "lease_resolution": lease_resolution,
                }
            else:
                deployment = {
                    "executor": "local_hdc",
                    **self.device_gateway.local_deploy(
                        serial=serial,
                        local_file=binary,
                        artifact_sha256=binary_sha,
                        remote_path=remote_path,
                        timeout_seconds=timeout,
                        agent_run_id=str(run["run_id"]),
                    ),
                }
            deployment = normalize_deployment_result(
                deployment,
                expected_sha256=binary_sha,
                expected_remote_path=remote_path,
                expected_serial=serial,
            )
            artifact = self._write_artifact("device_deployment", deployment, run, input_artifacts=[f"artifact:{build['artifact_id']}", f"artifact:{probe['artifact_id']}"])
            transfer = _record(deployment.get("artifact_transfer"))
            device_transfer = _record(deployment.get("device_transfer"))
            deployment_verified = deployment.get("deployment_verified") is True
            bridge_envelope = _record(deployment.get("bridge_envelope"))
            return {
                "status": deployment.get("status", _record(deployment.get("result")).get("status", "unknown")),
                "deployment_artifact_id": artifact["artifact_id"],
                "executor": deployment.get("executor", "unknown"),
                "deployment_verified": deployment_verified,
                "remote_path": remote_path if deployment_verified else None,
                "requested_remote_path": remote_path,
                "artifact_sha256": binary_sha,
                "remote_sha256": deployment.get("remote_sha256"),
                "stage": deployment.get("stage"),
                "failure_domain": deployment.get("failure_domain"),
                **({"error": deployment["error"]} if isinstance(deployment.get("error"), dict) else {}),
                **({"hdc": deployment["hdc"]} if isinstance(deployment.get("hdc"), dict) else {}),
                **({"artifact_transfer": transfer} if transfer else {}),
                **({"device_transfer": device_transfer} if device_transfer else {}),
                **({"bridge_diagnostics": deployment["bridge_diagnostics"]} if isinstance(deployment.get("bridge_diagnostics"), dict) else {}),
                **({"idempotency_reused": True} if bridge_envelope.get("idempotency_reused") is True else {}),
                **({"lease_resolution": deployment["lease_resolution"]} if isinstance(deployment.get("lease_resolution"), dict) else {}),
                "summary_zh": (
                    "构建产物已由 Linux 平台自动中转到 Windows Bridge，并通过本机 HDC 下发到手机；传输与手机端哈希均已校验。"
                    if deployment.get("executor") == "bridge" and deployment_verified
                    else "构建产物已通过本机 HDC 下发到手机，且手机端 SHA-256 校验通过。"
                    if deployment.get("executor") == "local_hdc" and deployment_verified
                    else "设备部署未形成手机端文件校验证据；不得执行 requested_remote_path，请依据 stage、error 与 HDC 输出修订或使用新的 retry_nonce 重试。"
                ),
            }
        if action_id == "device.execute_shell":
            probe = self.artifact_store.read("device_probe", _text(input_value.get("device_probe_artifact_id"), 120))
            serial = self._probe_serial(probe)
            timeout = int(input_value.get("timeout_seconds", 240))
            command = _text(input_value.get("command"), 16 * 1024)
            if self._remote_selector(input_value, run):
                execution = self._remote_job(run=run, input_value=input_value, action=action_id, payload={"command": command, "device_serial": serial}, timeout_seconds=timeout)
            else:
                execution = {
                    "executor": "local_hdc",
                    **self.device_gateway.local_execute_shell(
                        serial=serial,
                        command=command,
                        timeout_seconds=timeout,
                        agent_run_id=str(run["run_id"]),
                    ),
                }
            artifact = self._write_artifact("device_remote_shell_run", execution, run, input_artifacts=[f"artifact:{probe['artifact_id']}"])
            return {
                "status": execution.get("status", _record(execution.get("bridge_result")).get("status", "unknown")),
                "remote_shell_run_artifact_id": artifact["artifact_id"],
                "device_serial": serial,
                "execution": execution,
                **({"lease_resolution": execution["lease_resolution"]} if isinstance(execution.get("lease_resolution"), dict) else {}),
            }
        if action_id == "device.collect_artifact":
            probe = self.artifact_store.read("device_probe", _text(input_value.get("device_probe_artifact_id"), 120))
            serial = self._probe_serial(probe)
            remote_path = _text(input_value.get("remote_path"), 512)
            timeout = int(input_value.get("timeout_seconds", 240))
            if self._remote_selector(input_value, run):
                collection = self._remote_job(run=run, input_value=input_value, action=action_id, payload={"remote_path": remote_path, "device_serial": serial}, timeout_seconds=timeout)
            else:
                destination = self.device_gateway.spool_root / str(run["run_id"]) / f"collect-{hashlib.sha256(remote_path.encode('utf-8')).hexdigest()[:16]}"
                collection = {
                    "executor": "local_hdc",
                    **self.device_gateway.local_collect(
                        serial=serial,
                        remote_path=remote_path,
                        destination=destination,
                        timeout_seconds=timeout,
                        agent_run_id=str(run["run_id"]),
                    ),
                }
            artifact = self._write_artifact("device_collected_artifact", collection, run, input_artifacts=[f"artifact:{probe['artifact_id']}"])
            return {
                "status": collection.get("status", _record(collection.get("bridge_result")).get("status", "unknown")),
                "collection_artifact_id": artifact["artifact_id"],
                "collection": collection,
                **({"lease_resolution": collection["lease_resolution"]} if isinstance(collection.get("lease_resolution"), dict) else {}),
            }
        if action_id == "device.cancel_job":
            job_id = _text(input_value.get("job_id"), 80)
            selector = self._remote_selector(input_value, run)
            target, _ = capture_target(_record(run.get("context")))
            if target:
                # A Bridge may host several phones. Binding the cancellation
                # route alone does not bind the historical job being cancelled.
                # Local HDC currently has no durable per-job handle; an absent
                # ownership record must not become an acknowledged cancellation.
                try:
                    entry = self.device_gateway.verify_job_owner(job_id=job_id, bridge_id=target["bridge_id"])
                except PlatformError as exc:
                    if exc.code not in {"BRIDGE_JOB_UNAUTHORIZED", "DEVICE_JOB_NOT_FOUND"}:
                        raise
                    raise PlatformError(
                        "SCENARIO_DEVICE_JOB_OWNER_UNVERIFIED", "无法确认该任务属于本次选定设备，未发送取消请求。",
                        status_code=409, details={"job_id": job_id, "selected_device": target},
                    ) from exc
                job = _record(entry.get("job"))
                job_project = _text(job.get("project_id"), 160)
                job_logical = _text(job.get("logical_device_id"), 160)
                job_serials = [value for value in (
                    _text(_record(job.get("payload")).get("device_serial"), 160),
                    _text(job.get("device_serial"), 160),
                ) if value]
                if not job_project or (selector is not None and not job_logical) or (selector is None and not job_serials):
                    raise PlatformError(
                        "SCENARIO_DEVICE_JOB_OWNER_UNVERIFIED", "任务记录缺少设备归属信息，未发送取消请求。",
                        status_code=409, details={"job_id": job_id, "selected_device": target},
                    )
                if (job_project != project_id
                        or (selector is not None and job_logical != selector[2])
                        or any(serial != target["device_serial"] for serial in job_serials)):
                    raise PlatformError(
                        "SCENARIO_DEVICE_TARGET_MISMATCH", "该任务属于其他项目或设备，未发送取消请求。",
                        status_code=409, details={"job_id": job_id, "selected_device": target},
                    )
            if selector is not None:
                return self.device_gateway.cancel_bridge_job(bridge_id=selector[0], job_id=job_id)
            return self.device_gateway.local_executor.cancel(job_id)
        raise PlatformError("ACTION_NOT_IMPLEMENTED", "后端 Action Router 尚未实现该 action。", status_code=422, details={"action_id": action_id})

    def cancel(self, run: dict[str, Any]) -> None:
        # Cancellation crosses only typed executor boundaries. The Build Worker
        # terminates tracked fixed-toolchain children; the Device Gateway sends
        # `job.cancel` to any live remote Bridge jobs and records deferred
        # cancellation if the bridge is offline. Neither path grants a host
        # shell to the Agent.
        run_id = str(run.get("run_id") or "")
        self.build_worker.cancel_run(run_id)
        self.device_gateway.cancel_agent_run(run_id)
        return None
