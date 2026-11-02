"""Per-Run MCP facade for the currently selected page Agent.

The process receives an HMAC-derived token and Run identity through its
environment.  It cannot select another Run and exposes no workstation shell.
"""

from __future__ import annotations

from copy import deepcopy
import os
from typing import Any, Literal

import httpx
from mcp.server.fastmcp import FastMCP
from pydantic import BaseModel, Field


API_BASE = os.environ.get("HETERO_PLATFORM_API_BASE", "http://127.0.0.1:8006").rstrip("/")
RUN_ID = os.environ.get("HETERO_AGENT_RUN_ID", "").strip()
TOKEN = os.environ.get("HETERO_AGENT_TOOL_TOKEN", "").strip()

if not RUN_ID or not TOKEN:
    raise RuntimeError("HETERO_AGENT_RUN_ID and HETERO_AGENT_TOOL_TOKEN are required")

mcp = FastMCP(
    "heterogeneous_platform",
    instructions="异构优化平台的白名单构建、设备和工件工具。工具返回值是唯一执行事实。",
    log_level="WARNING",
)


def _portable_mcp_tool_schema(schema: dict[str, Any]) -> dict[str, Any]:
    """Inline Pydantic references for Agent runtimes with basic JSON Schema support.

    FastMCP keeps the original Pydantic argument model separately and uses it for
    execution-time validation. This projection only changes the schema advertised
    through ``tools/list``. Optional nullable fields are advertised as omittable
    non-null values because several Agent runtimes reject ``anyOf(..., null)``.
    """

    root = deepcopy(schema)
    definitions = root.get("$defs")
    if not isinstance(definitions, dict):
        definitions = {}

    def project(
        value: Any,
        *,
        resolving: tuple[str, ...] = (),
        nullable_can_be_omitted: bool = False,
    ) -> Any:
        if isinstance(value, list):
            return [project(item, resolving=resolving) for item in value]
        if not isinstance(value, dict):
            return value

        reference = value.get("$ref")
        if reference is not None:
            prefix = "#/$defs/"
            if not isinstance(reference, str) or not reference.startswith(prefix):
                raise ValueError(f"MCP tool schema contains unsupported reference: {reference!r}")
            definition_name = reference[len(prefix) :].replace("~1", "/").replace("~0", "~")
            definition = definitions.get(definition_name)
            if not isinstance(definition, dict):
                raise ValueError(f"MCP tool schema references missing definition: {definition_name}")
            if definition_name in resolving:
                chain = " -> ".join((*resolving, definition_name))
                raise ValueError(f"MCP tool schema contains a recursive reference: {chain}")
            resolved = project(
                definition,
                resolving=(*resolving, definition_name),
                nullable_can_be_omitted=nullable_can_be_omitted,
            )
            siblings = {
                key: project(item, resolving=resolving)
                for key, item in value.items()
                if key != "$ref"
            }
            return {**resolved, **siblings}

        required = {
            str(item)
            for item in value.get("required", [])
            if isinstance(item, str)
        }
        projected: dict[str, Any] = {}
        for key, item in value.items():
            if key == "$defs":
                continue
            if key == "properties" and isinstance(item, dict):
                projected[key] = {
                    property_name: project(
                        property_schema,
                        resolving=resolving,
                        nullable_can_be_omitted=property_name not in required,
                    )
                    for property_name, property_schema in item.items()
                }
            else:
                projected[key] = project(item, resolving=resolving)

        alternatives = projected.get("anyOf")
        if nullable_can_be_omitted and isinstance(alternatives, list):
            non_null = [
                item
                for item in alternatives
                if not (isinstance(item, dict) and item.get("type") == "null")
            ]
            has_null = len(non_null) + 1 == len(alternatives)
            if has_null and len(non_null) == 1 and isinstance(non_null[0], dict):
                siblings = {
                    key: item
                    for key, item in projected.items()
                    if key not in {"anyOf", "default"}
                }
                return {**non_null[0], **siblings}

        return projected

    projected_root = project(root)
    if not isinstance(projected_root, dict):
        raise ValueError("MCP tool schema root must remain an object")
    return projected_root


def _publish_portable_mcp_tool_schemas() -> None:
    """Project every registered tool at one deterministic facade boundary."""

    tool_manager = getattr(mcp, "_tool_manager", None)
    tools = getattr(tool_manager, "_tools", None)
    if not isinstance(tools, dict):
        raise RuntimeError("FastMCP tool registry is unavailable")
    for tool in tools.values():
        tool.parameters = _portable_mcp_tool_schema(tool.parameters)


def _request(method: str, path: str, body: dict[str, Any] | None = None, *, timeout: float = 920.0) -> dict[str, Any]:
    headers = {"Authorization": f"Bearer {TOKEN}"}
    response = httpx.request(method, f"{API_BASE}{path}", headers=headers, json=body, timeout=timeout, trust_env=False)
    try:
        value = response.json()
    except ValueError as exc:
        raise RuntimeError(f"平台返回非 JSON 响应（HTTP {response.status_code}）") from exc
    if response.status_code >= 400:
        detail = value.get("detail") if isinstance(value, dict) else value
        raise RuntimeError(f"平台工具请求失败（HTTP {response.status_code}）：{detail}")
    return value if isinstance(value, dict) else {"value": value}


def _action(action_id: str, value: dict[str, Any]) -> dict[str, Any]:
    return _request("POST", f"/v1/internal/agent-runs/{RUN_ID}/actions/{action_id}", {"input": value})


@mcp.tool(
    description=(
        "Agent Adapter 未直接展示某个专用 MCP 工具时，通过本入口调用同一个已注册工具。"
        "tool_name 必须是精确 MCP 工具名，input 使用该工具原有参数；目标工具仍执行原参数校验、权限边界和审计。"
    )
)
async def platform_call_tool(tool_name: str, input: dict[str, Any]) -> dict[str, Any]:
    normalized = tool_name.strip()
    if not normalized or normalized == "platform_call_tool":
        raise ValueError("tool_name 必须指向另一个已注册 MCP 工具")
    tool_manager = getattr(mcp, "_tool_manager", None)
    if tool_manager is None or tool_manager.get_tool(normalized) is None:
        raise ValueError(f"未注册 MCP 工具：{normalized}")
    result = await tool_manager.call_tool(
        normalized,
        input,
        context=mcp.get_context(),
        convert_result=False,
    )
    if not isinstance(result, dict):
        raise RuntimeError(f"MCP 工具 {normalized} 返回了非对象结果")
    return result


ExperimentEvidenceArtifactType = Literal[
    "build_profile_surface",
    "build_source",
    "build_result",
    "device_probe",
    "device_deployment",
    "device_remote_shell_run",
    "device_collected_artifact",
]


class ExperimentEvidenceArtifact(BaseModel):
    artifact_type: ExperimentEvidenceArtifactType
    artifact_id: str = Field(pattern=r"^art_[a-f0-9]{24}$")


class SkillBootstrapDiscovery(BaseModel):
    skill_id: str = Field(min_length=1, max_length=120)
    status: Literal["loaded", "available", "not_found", "load_failed"]
    source: str = Field(default="", max_length=1024)
    detail: str = Field(default="", max_length=1200)


class ExperimentFinding(BaseModel):
    title: str = Field(min_length=1, max_length=240)
    category: Literal[
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
    ]
    observed_behavior: str = Field(min_length=1, max_length=2400)
    why_it_matters: str = Field(min_length=1, max_length=1600)
    trigger_conditions: list[str] = Field(default_factory=list, max_length=8)
    successful_revision: str = Field(default="", max_length=2400)
    applicability_scope: str = Field(min_length=1, max_length=1600)
    limitations: list[str] = Field(default_factory=list, max_length=8)
    reproducibility: Literal[
        "first_observation",
        "reproduced_same_device",
        "reproduced_cross_device",
        "contradicted",
        "not_tested",
    ]
    proposed_destinations: list[
        Literal[
            "build_profile",
            "hardware_api_kb",
            "device_validation_skill",
            "regression_fixture",
            "product_diagnostic",
            "observation_only",
        ]
    ] = Field(default_factory=list, max_length=6)
    evidence_artifact_ids: list[str] = Field(default_factory=list, max_length=12)


class ExperienceSourceRef(BaseModel):
    source_type: Literal["agent_run", "analysis_trace", "experiment_learning_report"]
    source_id: str = Field(min_length=1, max_length=160)


class ExperienceEnvironmentComparison(BaseModel):
    dimension: str = Field(min_length=1, max_length=120)
    baseline: str = Field(default="未知", max_length=800)
    observed: str = Field(default="未知", max_length=800)
    impact: str = Field(default="尚未确定", max_length=1200)


class ExperienceResolutionOption(BaseModel):
    resolution_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,79}$")
    kind: Literal[
        "environment_config",
        "repository_change",
        "sdk_or_library_change",
        "device_change",
        "runtime_adapter_change",
        "workflow_change",
        "skill_revision",
        "code_fix",
        "additional_evidence",
        "no_action",
    ]
    title: str = Field(min_length=1, max_length=240)
    rationale: str = Field(min_length=1, max_length=2000)
    operator_steps: list[str] = Field(default_factory=list, max_length=12)
    agent_steps: list[str] = Field(default_factory=list, max_length=12)
    prerequisites: list[str] = Field(default_factory=list, max_length=10)
    risks: list[str] = Field(default_factory=list, max_length=10)
    verification: str = Field(min_length=1, max_length=2000)
    target_skill_ids: list[str] = Field(default_factory=list, max_length=12)
    recommended: bool = False


class ExperiencePitfall(BaseModel):
    recurrence_key: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]{2,119}$")
    title: str = Field(min_length=1, max_length=240)
    category: Literal[
        "model_behavior",
        "agent_framework",
        "workflow_orchestration",
        "skill_design",
        "host_environment",
        "sdk_or_toolchain",
        "runtime_library",
        "source_repository",
        "device_or_os",
        "network_or_provider",
        "evidence_contract",
        "other",
    ]
    symptom: str = Field(min_length=1, max_length=2400)
    failure_stage: str = Field(default="", max_length=160)
    root_cause_hypothesis: str = Field(min_length=1, max_length=2400)
    confidence: Literal["low", "medium", "high"]
    trigger_conditions: list[str] = Field(min_length=1, max_length=12)
    evidence_source_ids: list[str] = Field(min_length=1, max_length=20)
    applicability_scope: str = Field(min_length=1, max_length=1800)
    generalization_boundary: str = Field(min_length=1, max_length=1800)
    affected_skill_ids: list[str] = Field(default_factory=list, max_length=16)
    resolution_options: list[ExperienceResolutionOption] = Field(min_length=1, max_length=8)


class ApplicationFlowCandidateBoundary(BaseModel):
    source_connector_id: str = Field(default="", max_length=160, description="边界所在代码仓；为空时使用主代码仓")
    path: str = Field(min_length=1, max_length=2048, description="代码仓内相对路径")
    line: int = Field(ge=1, description="已用源码小窗口核对的声明行号")
    symbol: str = Field(min_length=1, max_length=1000)
    name: str = Field(default="", max_length=1000)
    kind: str = Field(default="", max_length=1000)


class ApplicationFlowUserAction(BaseModel):
    order: int = Field(ge=1)
    name: str = Field(min_length=1, max_length=1000)
    description: str = Field(min_length=1, max_length=2000)
    observable_outcome: str = Field(min_length=1, max_length=2000)


class ApplicationFlowActionCoverage(BaseModel):
    action_order: int = Field(ge=1)
    mapping_status: Literal["mapped", "partial", "unmapped"]
    source_connector_ids: list[str] = Field(default_factory=list, max_length=12)
    evidence_summary: str = Field(default="", max_length=2000)
    missing_reason: str = Field(default="", max_length=2000)
    suspected_modules: list[str] = Field(default_factory=list, max_length=12)


class ApplicationFlowSourceScope(BaseModel):
    source_connector_id: str = Field(min_length=1, max_length=160)
    source_snapshot_id: str = Field(default="", max_length=256)
    subpath: str = Field(default="", max_length=2048)
    code_graph_id: str = Field(default="", max_length=80)


class ExperimentalFlowSourceScope(BaseModel):
    source_connector_id: str = Field(min_length=1, max_length=160)
    source_snapshot_id: str = Field(default="", max_length=256)
    subpath: str = Field(default="", max_length=2048)


class ApplicationFlowCandidate(BaseModel):
    name: str = Field(min_length=1, max_length=1000)
    description: str = Field(min_length=1, max_length=3000)
    trigger: str = Field(min_length=1, max_length=3000)
    completion: str = Field(min_length=1, max_length=3000)
    operation_scope: Literal["complete_user_operation"]
    boundary_rationale: str = Field(min_length=1, max_length=3000)
    user_actions: list[ApplicationFlowUserAction] = Field(min_length=1, max_length=16)
    action_coverage: list[ApplicationFlowActionCoverage] = Field(min_length=1, max_length=16)
    semantic_outline: list[str] = Field(min_length=1, max_length=24)
    participants: list[str] = Field(default_factory=list, max_length=16)
    entry: ApplicationFlowCandidateBoundary
    exit: ApplicationFlowCandidateBoundary
    confidence: Literal["high", "medium", "low"] = "medium"
    open_questions: list[str] = Field(default_factory=list, max_length=16)


class ApplicationFlowTraceBoundary(BaseModel):
    source_connector_id: str = Field(default="", max_length=160, description="锚点所在代码仓；为空时使用主代码仓")
    event_name: str = Field(min_length=1, max_length=1000, description="源码字面量 HiTrace label、静态前缀，或函数派生事件的稳定 selector")
    path: str = Field(min_length=1, max_length=2048, description="代码仓内相对路径")
    line: int = Field(ge=1, description="HiTrace 宏或 event 字面量所在源码行")
    macro: str = Field(min_length=1, max_length=120)
    symbol: str = Field(min_length=1, max_length=1000, description="包含该插桩的函数或作用域符号")
    event_label_match: Literal["exact_literal", "wrapper_literal", "function_derived", "static_prefix"] = "exact_literal"
    build_condition: str = Field(default="not_observed_in_local_window", min_length=1, max_length=1000)
    kind: str = Field(default="hitrace_literal", min_length=1, max_length=1000)


class ApplicationFlowStageTraceResult(BaseModel):
    stage_id: str = Field(min_length=2, max_length=80)
    start: ApplicationFlowTraceBoundary | None = None
    end: ApplicationFlowTraceBoundary | None = None
    notes: str = Field(default="", max_length=2000)


class ApplicationFlowRuntimeStageMatch(BaseModel):
    stage_id: str = Field(min_length=2, max_length=80)
    match_status: Literal["matched", "partial", "unmatched"]
    confidence: Literal["high", "medium", "low"]
    start_event_id: str = Field(default="", max_length=80)
    end_event_id: str = Field(default="", max_length=80)
    supporting_event_ids: list[str] = Field(default_factory=list, max_length=16)
    rationale: str = Field(min_length=1, max_length=2000)


class ApplicationFlowRuntimeNodeMatch(BaseModel):
    node_id: str = Field(min_length=2, max_length=80)
    match_status: Literal["matched", "partial", "unmatched"]
    confidence: Literal["high", "medium", "low"]
    start_event_id: str = Field(default="", max_length=80)
    end_event_id: str = Field(default="", max_length=80)
    supporting_event_ids: list[str] = Field(default_factory=list, max_length=16)
    rationale: str = Field(min_length=1, max_length=2000)


@mcp.tool(
    description=(
        "列出当前运行 profile 下的 Skill。解析顺序固定为：激活的用户自编辑版本优先，找不到时回退平台只读版本。"
        "用户要求查看、修改或新建 Skill 时先调用；不要通过文件搜索猜测运行时生效版本。"
    )
)
def skills_list() -> dict[str, Any]:
    return _action("skills.list", {})


@mcp.tool(
    description=(
        "读取一个 Skill 的完整内容。version_id 为空时读取当前热加载版本，server 读取平台只读基线，usr_* 读取指定用户版本。"
        "修改已有 Skill 前必须先读当前版本，避免丢失仍然有效的契约。"
    )
)
def skills_read(skill_id: str, version_id: str = "") -> dict[str, Any]:
    return _action("skills.read", {"skill_id": skill_id, "version_id": version_id})


@mcp.tool(
    description=(
        "创建并立即激活一个新的用户自编辑 Skill 版本；绝不覆盖平台 Skill 或旧用户版本。"
        "已有 Skill 应保留目标、关键契约和停止条件，删除泛化建议与单一失败样本堆积；新 Skill 使用小写连字符 ID。"
        "content 是完整、可独立使用的中文 Skill 正文。保存成功后下一轮对话和后续页面操作会热加载该版本。"
    )
)
def skills_save_user_version(
    skill_id: str,
    label_zh: str,
    content: str,
    description_zh: str = "",
    prompt_slot: str = "",
    change_summary_zh: str = "",
) -> dict[str, Any]:
    return _action(
        "skills.save_user_version",
        {
            "skill_id": skill_id,
            "label_zh": label_zh,
            "description_zh": description_zh,
            "content": content,
            "prompt_slot": prompt_slot,
            "change_summary_zh": change_summary_zh,
        },
    )


@mcp.tool(
    description=(
        "切换某个 Skill 的用户版本。version_id 使用 skills_list 返回的 usr_*；传空字符串会取消用户覆盖并回退平台版本。"
        "切换只影响后续推理，不改写历史 Run 工件。"
    )
)
def skills_activate_version(skill_id: str, version_id: str = "") -> dict[str, Any]:
    return _action("skills.activate_version", {"skill_id": skill_id, "version_id": version_id})


@mcp.tool(
    description=(
        "列出可用于经验提炼的历史 Agent Run、页面分析 Trace 和真机实验学习记录。"
        "先按失败阶段、时间和环境身份选择少量相关源，不要无差别读取全部历史。"
    )
)
def experience_list_sources(limit: int = 100) -> dict[str, Any]:
    return _action("experience.list_sources", {"limit": limit})


@mcp.tool(
    description=(
        "分批读取一个历史执行源的脱敏内容。agent_run 使用 after 游标继续读取；"
        "返回中的真实工具结果、错误码和环境身份是证据，模型解释仍是待验证假设。"
    )
)
def experience_read_source(
    source_type: Literal["agent_run", "analysis_trace", "experiment_learning_report"],
    source_id: str,
    after: int = 0,
    limit: int = 500,
) -> dict[str, Any]:
    return _action(
        "experience.read_source",
        {"source_type": source_type, "source_id": source_id, "after": after, "limit": limit},
    )


@mcp.tool(description="列出已提炼的踩坑案例、候选处置、用户选择和验证状态。")
def experience_list_cases() -> dict[str, Any]:
    return _action("experience.list_cases", {})


@mcp.tool(
    description=(
        "把多个历史执行源提炼为可审阅的踩坑案例。必须区分事实、根因假设和环境差异；"
        "recurrence_key 描述可复用问题类，不能塞入某台设备、某次 Run 或单个报错文本。"
        "每个案例给出可选择处置及验证方法。该操作不会自动修改环境、代码或 Skill。"
    )
)
def experience_save_review(
    title: str,
    summary_zh: str,
    source_refs: list[ExperienceSourceRef],
    environment_comparison: list[ExperienceEnvironmentComparison],
    pitfalls: list[ExperiencePitfall],
) -> dict[str, Any]:
    return _action(
        "experience.save_review",
        {
            "title": title,
            "summary_zh": summary_zh,
            "source_refs": [item.model_dump() for item in source_refs],
            "environment_comparison": [item.model_dump() for item in environment_comparison],
            "pitfalls": [item.model_dump() for item in pitfalls],
        },
    )


@mcp.tool(
    description=(
        "记录一个踩坑案例的处置选择或验证状态。只有用户已明确选择，或用户要求 Agent 执行该处置时调用；"
        "若处置涉及 Skill，仍需另行创建用户 Skill 版本并做回归，不能把 triage 当作已修复。"
    )
)
def experience_triage_case(
    case_id: str,
    status: Literal["observed", "triaged", "action_planned", "resolved", "verified", "dismissed"],
    selected_resolution_id: str = "",
    operator_note: str = "",
) -> dict[str, Any]:
    return _action(
        "experience.triage_case",
        {
            "case_id": case_id,
            "status": status,
            "selected_resolution_id": selected_resolution_id,
            "operator_note": operator_note,
        },
    )


@mcp.tool(
    description=(
        "列出当前平台的自建 API 知识库和服务端已登记代码仓。"
        "当用户给出代码仓路径但没有 connector ID 时，先用本工具取得精确映射；不得把任意宿主路径直接当作已授权代码仓。"
    )
)
def knowledge_list_custom_api_kbs() -> dict[str, Any]:
    return _action("knowledge.list_custom_api_kbs", {})


@mcp.tool(
    description=(
        "对一个已登记代码仓执行有界、只读的 API 合同扫描，并为指定异构单元创建新的独立自建 API 知识库。"
        "扫描器优先公开头文件、算子/能力枚举和 HDI/VDI/IDL 合同，内部 Service/Manager 实现仅作为低优先级证据。"
        "同名单元也会新建一份，不覆盖平台库或其他自建库。source_connector_id 与 source_root 至少提供一个；"
        "source_root 只接受 knowledge_list_custom_api_kbs 返回的精确服务端路径。输出是静态候选，不是精准 AST、公开 SDK 审核或设备能力证明。"
    )
)
def knowledge_build_custom_api_kb(
    unit_name: str,
    source_connector_id: str = "",
    source_root: str = "",
) -> dict[str, Any]:
    return _action(
        "knowledge.build_custom_api_kb",
        {
            "unit_name": unit_name,
            "source_connector_id": source_connector_id,
            "source_root": source_root,
        },
    )


@mcp.tool(
    description=(
        "列出模块代码知识底图及构建状态。底图绑定代码仓、源码快照和相对目录；ready 状态才可用于流程分析。"
        "底图是 Tree-sitter 静态语法导航事实，不证明编译期绑定或运行时路径。"
    )
)
def code_graph_list(source_connector_id: str = "", limit: int = 100) -> dict[str, Any]:
    return _action("code_graph.list", {"source_connector_id": source_connector_id, "limit": limit})


@mcp.tool(
    description=(
        "为已登记代码仓的相对目录启动代码知识底图构建。构建在后台执行；返回 graph_id 后使用 code_graph_get 查询进度。"
        "不要对 30G 全仓默认建图，应优先使用用户选择的业务模块目录。"
    )
)
def code_graph_build(source_connector_id: str, subpath: str = "", max_files: int = 50_000) -> dict[str, Any]:
    return _action(
        "code_graph.build",
        {"source_connector_id": source_connector_id, "subpath": subpath, "max_files": max_files},
    )


@mcp.tool(description="读取一个代码知识底图的构建状态、源码绑定、统计摘要和血缘工件。")
def code_graph_get(graph_id: str) -> dict[str, Any]:
    return _action("code_graph.get", {"graph_id": graph_id})


@mcp.tool(
    description=(
        "按用户流程描述、业务动作或符号检索代码知识底图，返回相关函数、文件、一跳调用候选和 HiTrace 插桩候选。"
        "先用宽语义词定位，再用源码读取工具核对关键边界；名称解析边只可作为导航候选。"
    )
)
def code_graph_search(graph_id: str, query: str = "", limit: int = 30) -> dict[str, Any]:
    return _action("code_graph.search", {"graph_id": graph_id, "query": query, "limit": limit})


@mcp.tool(
    description=(
        "查询代码知识库中的 Tree-sitter/SQLite 静态底图，返回限幅的相关符号、调用候选和 HiTrace 插桩候选。"
        "使用这些候选定位真实源码，再由 Agent 复核接口契约、控制流、数据流和调用关系。"
    )
)
def code_graph_query_knowledge(graph_id: str, query: str = "", limit: int = 15) -> dict[str, Any]:
    return _action("code_graph.query_knowledge", {"graph_id": graph_id, "query": query, "limit": limit})


@mcp.tool(
    description=(
        "把当前代码知识分析发布为版本化 Markdown 报告。报告绑定 ready 代码底图和源码快照；"
        "可以包含 Mermaid fenced code block。首次分析不传 parent_report_id，后续问答基于当前报告生成新版本。"
        "报告必须区分静态底图事实、源码复核事实、Agent 语义推断和仍需确认事项。"
    )
)
def code_graph_publish_report(
    graph_id: str,
    title: str,
    markdown: str,
    question: str = "",
    parent_report_id: str = "",
) -> dict[str, Any]:
    return _action(
        "code_graph.publish_report",
        {
            "graph_id": graph_id,
            "title": title,
            "markdown": markdown,
            "question": question,
            "parent_report_id": parent_report_id,
        },
    )


@mcp.tool(
    description=(
        "读取 Playground 上传的 HiPerf 或 Trace 运行数据，返回服务端文件路径、元数据与可用的文本预览。"
        "该工件只服务于实验流程分析，不是 application-flow 场景工件。"
    )
)
def experimental_flow_read_evidence(evidence_artifact_id: str) -> dict[str, Any]:
    return _action("experimental_flow.read_evidence", {"evidence_artifact_id": evidence_artifact_id})


@mcp.tool(description="读取一个 Playground 历史结果的完整自由 Mermaid 与分析说明，用于继续修订。")
def experimental_flow_read_result(result_artifact_id: str) -> dict[str, Any]:
    return _action("experimental_flow.read_result", {"result_artifact_id": result_artifact_id})


@mcp.tool(
    description=(
        "发布 Playground 的自由 Mermaid 流程图与分析说明。analysis_kind 为 hiperf 或 trace；"
        "source_scopes 携带本轮使用的全部代码仓范围。平台仅校验来源绑定和浏览器渲染安全，"
        "不套用正式应用流程 Schema。"
    )
)
def experimental_flow_publish_result(
    analysis_kind: Literal["hiperf", "trace"],
    source_connector_id: str,
    flow_goal: str,
    title: str,
    mermaid: str,
    summary_markdown: str,
    evidence_artifact_id: str,
    subpath: str = "",
    parent_result_artifact_id: str = "",
    source_scopes: list[ExperimentalFlowSourceScope] | None = None,
) -> dict[str, Any]:
    return _action(
        "experimental_flow.publish_result",
        {
            "analysis_kind": analysis_kind,
            "source_connector_id": source_connector_id,
            "flow_goal": flow_goal,
            "title": title,
            "mermaid": mermaid,
            "summary_markdown": summary_markdown,
            "evidence_artifact_id": evidence_artifact_id,
            "subpath": subpath,
            "parent_result_artifact_id": parent_result_artifact_id,
            "source_scopes": [item.model_dump() for item in (source_scopes or [])],
        },
    )


@mcp.tool(
    description=(
        "列出应用执行流程发现工件、应用流程场景工件、两份标准 Schema 摘要和已登记代码仓。"
        "开始流程发现、读取用户选中的历史候选或检查生成结果时先调用。"
    )
)
def application_flow_list_assets(source_connector_id: str = "", limit: int = 100) -> dict[str, Any]:
    return _action("application_flow.list_assets", {"source_connector_id": source_connector_id, "limit": limit})


@mcp.tool(
    description=(
        "对已登记代码仓或相对目录执行一次快速、只读的流程侦察，返回高优先级公共入口文件与带行号的"
        "action_symbol_candidates；每项均直接来自当前源码声明行。它适合在入口不明确时提供导航候选，"
        "Agent 也可以依据已有符号、Trace 或调用线索直接使用原生源码工具。结果只表示静态候选；大仓传入"
        "用户选择的 subpath 可缩小扫描范围。"
    )
)
def application_flow_inspect_repository(source_connector_id: str, subpath: str = "") -> dict[str, Any]:
    return _action("application_flow.inspect_repository", {"source_connector_id": source_connector_id, "subpath": subpath})


@mcp.tool(
    description=(
        "在已登记代码仓的目录或单个源码文件内按固定文本执行有界搜索，返回相对路径、行号和命中行。"
        "query 不是正则表达式；需要正则、批量导航或复杂调用链分析时优先使用原生 Grep/Glob/Read。"
    )
)
def application_flow_search_source(
    source_connector_id: str,
    query: str,
    subpath: str = "",
    max_results: int = 30,
) -> dict[str, Any]:
    return _action(
        "application_flow.search_source",
        {"source_connector_id": source_connector_id, "query": query, "subpath": subpath, "max_results": max_results},
    )


@mcp.tool(
    description=(
        "按代码仓相对路径读取最多 400 行源码，用于闭合应用流程的入口、转发、状态机、回调和出口证据。"
        "artifact ID 不是 path；不得请求绝对路径或越过 Source Connector。"
    )
)
def application_flow_read_source(
    source_connector_id: str,
    path: str,
    line_start: int = 1,
    line_end: int = 240,
) -> dict[str, Any]:
    return _action(
        "application_flow.read_source",
        {"source_connector_id": source_connector_id, "path": path, "line_start": line_start, "line_end": line_end},
    )


@mcp.tool(
    description=(
        "围绕一个已核对的实现位置反向发现源码 HiTrace 插桩。工具同时识别直接 HiTrace API、"
        "C/C++ 模块包装宏、RAII scope 包装和 ArkTS Trace/hiTraceMeter 调用，并返回事件字面量、"
        "start/end/scope 角色及可解析的包装定义链。结果是源码候选，仍需结合阶段边界确认配对。"
    )
)
def application_flow_inspect_trace_instrumentation(
    source_connector_id: str,
    path: str,
    line: int = 1,
    symbol: str = "",
    radius: int = 320,
    max_results: int = 40,
) -> dict[str, Any]:
    return _action(
        "application_flow.inspect_trace_instrumentation",
        {
            "source_connector_id": source_connector_id,
            "path": path,
            "line": line,
            "symbol": symbol,
            "radius": radius,
            "max_results": max_results,
        },
    )


@mcp.tool(
    description=(
        "从用户选定的真实 .htrace 或阶段分析 JSON 中提取实际出现的 Trace event name，并围绕现有流程阶段返回有界候选。"
        "页面发起的分析已在后端绑定 Trace，analysis_artifact_id 可省略并由平台按本次分析解析。"
        "stage_candidate_hints 只是词法检索提示；应结合阶段语义、源码实现、进程线程和时间范围判断。"
        "需要保存 event_catalog_artifact_id，后续只能引用该目录中真实存在的 event_id。"
    )
)
def application_flow_inspect_runtime_trace(
    source_connector_id: str,
    scenario_artifact_id: str,
    query: str = "",
    global_limit: int = 120,
    per_stage_limit: int = 12,
    analysis_artifact_id: str = "",
) -> dict[str, Any]:
    return _action(
        "application_flow.inspect_runtime_trace",
        {
            "source_connector_id": source_connector_id,
            "analysis_artifact_id": analysis_artifact_id,
            "scenario_artifact_id": scenario_artifact_id,
            "query": query,
            "global_limit": global_limit,
            "per_stage_limit": per_stage_limit,
        },
    )


@mcp.tool(description="读取一份精确的应用流程发现或场景工件；artifact_type 只能使用工具目录返回的两个规范值。")
def application_flow_read_asset(artifact_type: Literal["application_flow_discovery", "application_flow_scenario"], artifact_id: str) -> dict[str, Any]:
    return _action("application_flow.read_asset", {"artifact_type": artifact_type, "artifact_id": artifact_id})


@mcp.tool(
    description=(
        "按 application-flow-discovery-v1 Schema 保存候选业务流程和行级源码证据。"
        "调用前必须读取 .agents/skills/application-execution-flow-reconstruction/references/"
        "application-flow-discovery-v1.schema.json，不得凭记忆猜字段。顶层只能是 schema_version、"
        "source_connector_id、source_snapshot_id、scope、analysis_summary、flows、unknowns；"
        "每个 flow 的 id、name、order 等属性名不可改。轻量发现允许 trace_anchor_candidates 为空；"
        "observed_in_source 锚点必须提供可逐行校验的 source_location 与 instrumentation。"
    )
)
def application_flow_save_discovery(
    source_connector_id: str,
    discovery: dict[str, Any],
    subpath: str = "",
) -> dict[str, Any]:
    return _action(
        "application_flow.save_discovery",
        {"source_connector_id": source_connector_id, "subpath": subpath, "discovery": discovery},
    )


@mcp.tool(
    description=(
        "登记 Agent 已理解的完整用户操作，为场景工件建立代码仓、源码快照和分析目标血缘。首次直接生成阶段图时通常只提交"
        "一个最符合用户目标的操作；确有不同业务边界时也可登记多个。每个操作包含有序 user_actions、逐动作 action_coverage、"
        "semantic_outline，以及已核对的 entry/exit。partial/unmapped coverage 用于保存缺失源码和疑似模块。"
        "source_scopes 原样携带页面给出的全部仓、目录、快照和可选 code_graph_id；平台生成稳定 ID、证据引用与 discovery 工件。"
    )
)
def application_flow_publish_candidates(
    source_connector_id: str,
    summary_zh: str,
    candidates: list[ApplicationFlowCandidate],
    subpath: str = "",
    unknowns: list[str] | None = None,
    analysis_goal: str = "",
    code_graph_id: str = "",
    source_scopes: list[ApplicationFlowSourceScope] | None = None,
    parent_discovery_artifact_id: str = "",
    refinement_request: str = "",
) -> dict[str, Any]:
    return _action(
        "application_flow.publish_candidates",
        {
            "source_connector_id": source_connector_id,
            "subpath": subpath,
            "summary_zh": summary_zh,
            "candidates": [candidate.model_dump() for candidate in candidates],
            "unknowns": unknowns or [],
            "analysis_goal": analysis_goal,
            "code_graph_id": code_graph_id,
            "source_scopes": [item.model_dump() for item in (source_scopes or [])],
            "parent_discovery_artifact_id": parent_discovery_artifact_id,
            "refinement_request": refinement_request,
        },
    )


@mcp.tool(
    description=(
        "基于用户已选的 discovery_artifact_id 与 flow_id 保存字段名稳定的应用流程场景 v2 JSON 和 Mermaid。"
        "每个源码闭合 stage 必须用 stage_evidence.boundary_anchors 将 start/end 绑定到 discovery anchor；缺少源码锚点时保存为 draft_needs_trace_evidence。"
    )
)
def application_flow_save_scenario(
    source_connector_id: str,
    discovery_artifact_id: str,
    flow_id: str,
    scenario_json: dict[str, Any],
    mermaid: str,
    stage_evidence: list[dict[str, Any]],
    unknowns: list[str],
    parent_scenario_artifact_id: str = "",
    revision_request: str = "",
) -> dict[str, Any]:
    return _action(
        "application_flow.save_scenario",
        {
            "source_connector_id": source_connector_id,
            "discovery_artifact_id": discovery_artifact_id,
            "flow_id": flow_id,
            "scenario_json": scenario_json,
            "mermaid": mermaid,
            "stage_evidence": stage_evidence,
            "unknowns": unknowns,
            "parent_scenario_artifact_id": parent_scenario_artifact_id,
            "revision_request": revision_request,
        },
    )


@mcp.tool(
    description=(
        "把一份带 %% @scenario、%% @action、%% @phase、%% @stage、%% @stage_hierarchy、"
        "%% @implementation 以及 stage start/end 或 matcher 元数据的 Mermaid 编译成标准应用流程场景 v2 JSON。"
        "flow_id 是场景身份的权威值；工具会归一化 Markdown 代码围栏、scenario 头、重复进程组，"
        "并接受参考 JSON 或等价 key=value 形式的分层元数据。"
        "工具保存分层作者结构，把可定位子阶段投影到既有 stage，并从 discovery 自动绑定源码证据与 HiTrace anchor。模型只需生成 Mermaid，"
        "不要再手写 scenario_json、stage_evidence 或 unknowns；这是新流程的首选保存工具。"
    )
)
def application_flow_compile_mermaid(
    source_connector_id: str,
    discovery_artifact_id: str,
    flow_id: str,
    mermaid: str,
    parent_scenario_artifact_id: str = "",
    revision_request: str = "",
) -> dict[str, Any]:
    return _action(
        "application_flow.compile_mermaid",
        {
            "source_connector_id": source_connector_id,
            "discovery_artifact_id": discovery_artifact_id,
            "flow_id": flow_id,
            "mermaid": mermaid,
            "parent_scenario_artifact_id": parent_scenario_artifact_id,
            "revision_request": revision_request,
        },
    )


@mcp.tool(
    description=(
        "提交当前流程阶段图中逐阶段的源码 HiTrace start/end 查找结果。"
        "每个已找到边界必须给出真实 event、代码仓相对路径、行号、宏和所在符号；未找到的一侧留空并在 notes 说明。"
        "平台会逐行校验源码、生成稳定证据 ID、保存增强 discovery，并确定性重编译原 Mermaid/JSON。"
        "不要调用 application_flow_save_discovery 手写完整 discovery，也不要用普通函数名或推断名称冒充 event。"
    )
)
def application_flow_enrich_trace_anchors(
    source_connector_id: str,
    discovery_artifact_id: str,
    scenario_artifact_id: str,
    flow_id: str,
    stage_results: list[ApplicationFlowStageTraceResult],
) -> dict[str, Any]:
    return _action(
        "application_flow.enrich_trace_anchors",
        {
            "source_connector_id": source_connector_id,
            "discovery_artifact_id": discovery_artifact_id,
            "scenario_artifact_id": scenario_artifact_id,
            "flow_id": flow_id,
            "stage_results": [item.model_dump(exclude_none=True) for item in stage_results],
        },
    )


@mcp.tool(
    description=(
        "把真实 Trace 事件与阶段的语义匹配保存为侧车证据，并用完整 Mermaid 生成一个新场景版本。"
        "页面发起的分析已在后端绑定 Trace，analysis_artifact_id 可省略并由平台按本次分析解析。"
        "每个修订后 stage 提交一条 stage_matches；matched 必须给出 start/end event_id，partial 至少给出一个真实事件，"
        "unmatched 不引用事件。运行时确认的 Mermaid start/end 必须使用事件目录中的完整 event_name 和 exact 匹配；"
        "可观察的 action、phase 或 activity 通过 node_matches 保存细粒度 start/end，平台按 parent_id 聚合父级时间包络；"
        "只有候选而未闭合边界的阶段使用 matcher unavailable，并把候选作为 supporting_event_ids 保存。"
        "允许依据运行时顺序拆分、合并、重命名或重排阶段，但 Mermaid 元数据和最终 application-flow-scenario/v2 Schema 不变。"
    )
)
def application_flow_apply_runtime_trace_alignment(
    source_connector_id: str,
    event_catalog_artifact_id: str,
    scenario_artifact_id: str,
    flow_id: str,
    mermaid: str,
    stage_matches: list[ApplicationFlowRuntimeStageMatch],
    change_summary: str,
    node_matches: list[ApplicationFlowRuntimeNodeMatch] | None = None,
    limitations: list[str] | None = None,
    analysis_artifact_id: str = "",
) -> dict[str, Any]:
    return _action(
        "application_flow.apply_runtime_trace_alignment",
        {
            "source_connector_id": source_connector_id,
            "analysis_artifact_id": analysis_artifact_id,
            "event_catalog_artifact_id": event_catalog_artifact_id,
            "scenario_artifact_id": scenario_artifact_id,
            "flow_id": flow_id,
            "mermaid": mermaid,
            "stage_matches": [item.model_dump() for item in stage_matches],
            "node_matches": [item.model_dump() for item in (node_matches or [])],
            "change_summary": change_summary,
            "limitations": limitations or [],
        },
    )


@mcp.tool(
    description=(
        "在真机任务结束前，把关键发现与限制记录为未经审阅的实验学习候选。"
        "只引用当前 Run 已观察到的真实工件；单次现象必须缩小 applicability_scope，不能写成全局规则。"
        "该工具不会自动修改 Skill、构建 profile 或知识库。没有新发现时 findings 可为空，但仍应记录限制。"
    )
)
def platform_record_experiment_findings(
    summary_zh: str,
    findings: list[ExperimentFinding],
    limitations: list[str],
    evidence_artifacts: list[ExperimentEvidenceArtifact],
) -> dict[str, Any]:
    return _action(
        "platform.record_experiment_findings",
        {
            "summary_zh": summary_zh,
            "findings": [item.model_dump() for item in findings],
            "limitations": limitations,
            "evidence_artifacts": [item.model_dump() for item in evidence_artifacts],
        },
    )


@mcp.tool(
    description=(
        "列出经过版本和 SHA-256 校验的硬件 API 实验模板元数据，不返回源码。"
        "unit、profile_id 和 api_family 是目录中的规范值，不确定时应省略该字段并使用 query 描述计算语义，或先不带筛选查看 available_facets。"
        "精确筛选零命中时必须检查 filter_diagnostics 与 suggested_templates，而不是立即从零重写脚手架。"
        "候选排名只用于发现，仍需比较 semantic_match 后再选择 template_id；"
        "同时检查 coverage_level：executable_harness 可适配构建，lifecycle_with_adapter 还需补算子/格式适配，integration_recipe 只能指导系统集成；"
        "不要猜测模板路径，也不要一次读取整个模板库。"
    )
)
def platform_list_hardware_api_templates(
    unit: str = "",
    profile_id: str = "",
    api_family: str = "",
    query: str = "",
) -> dict[str, Any]:
    return _action(
        "platform.list_hardware_api_templates",
        {"unit": unit, "profile_id": profile_id, "api_family": api_family, "query": query},
    )


@mcp.tool(
    description=(
        "按 platform_list_hardware_api_templates 返回的精确 template_id 读取一个受审查模板及其已校验源码。"
        "复用 loader、context、资源生命周期、错误处理和清理脚手架；必须针对当前热点替换计算核心、输入、CPU oracle、比较与 benchmark。"
        "若 coverage_level=integration_recipe，只能按 recipe 定位真实系统构建面，不能把文档当作可直接编译源码。"
        "模板历史结果不是本轮设备或性能证据。"
    )
)
def platform_read_hardware_api_template(template_id: str) -> dict[str, Any]:
    return _action("platform.read_hardware_api_template", {"template_id": template_id})


@mcp.tool(description="查询固定 Build Worker profile 的真实 SDK 头文件、库、编译器与限制。写实验源码前应先调用。")
def build_describe_profile(profile_id: str = "", header_query: str = "") -> dict[str, Any]:
    return _action("build.describe_profile", {"profile_id": profile_id, "header_query": header_query})


@mcp.tool(description="把完整 C/C++ 实验源码暂存为不可变 build_source 工件。gpu_vulkan 还必须传完整 GLSL compute shader_source；平台用固定 glslang 生成 SPIR-V，禁止手写 SPIR-V。")
def build_stage_source(profile_id: str, language: str, source: str, title: str = "", shader_source: str = "", retry_nonce: str = "") -> dict[str, Any]:
    return _action("build.stage_source", {"profile_id": profile_id, "language": language, "source": source, "shader_source": shader_source, "title": title, "retry_nonce": retry_nonce})


@mcp.tool(description="使用固定工具链构建已暂存源码，返回 build_result 工件和结构化诊断。")
def build_create(source_artifact_id: str, profile_id: str, timeout_seconds: int = 240, retry_nonce: str = "") -> dict[str, Any]:
    return _action("build.create", {"source_artifact_id": source_artifact_id, "profile_id": profile_id, "timeout_seconds": timeout_seconds, "retry_nonce": retry_nonce})


@mcp.tool(description="列出已自动登记的远程 Device Bridge；本机直连 HDC 场景通常不需要。")
def device_list_bridges() -> dict[str, Any]:
    return _action("device.list_bridges", {})


@mcp.tool(description="可选兼容接口：为已确认的 Bridge 与逻辑设备显式创建单写租约。通常直接调用设备动作并省略 device_lease_id，由平台自动创建或复用；任务状态未知时先按真实任务诊断对账。")
def device_create_lease(bridge_id: str, logical_device_id: str, ttl_seconds: int = 7200) -> dict[str, Any]:
    return _action("device.create_lease", {"bridge_id": bridge_id, "logical_device_id": logical_device_id, "ttl_seconds": ttl_seconds})


@mcp.tool(description="发现本机 HDC 设备，或在提供已确认的 bridge_id 时发现远程 Bridge 设备。Windows 主路径为 list_bridges -> discover -> probe；从返回结果确认 logical_device_id 与 serial 后即可探测。")
def device_discover(bridge_id: str = "", retry_nonce: str = "") -> dict[str, Any]:
    value: dict[str, Any] = {"retry_nonce": retry_nonce}
    if bridge_id:
        value["bridge_id"] = bridge_id
    return _action("device.discover", value)


@mcp.tool(description="探测 device_discover 返回的真实设备序列号并形成 device_probe 工件。远程模式传入已确认的 bridge_id 与 logical_device_id；device_lease_id 可省略，由平台自动创建或复用，结果中的 lease_resolution 描述租约处理。")
def device_probe(device_serial: str, bridge_id: str = "", device_lease_id: str = "", logical_device_id: str = "", retry_nonce: str = "") -> dict[str, Any]:
    value = {"device_serial": device_serial, "retry_nonce": retry_nonce}
    value.update({key: item for key, item in {"bridge_id": bridge_id, "device_lease_id": device_lease_id, "logical_device_id": logical_device_id}.items() if item})
    return _action("device.probe", value)


@mcp.tool(description="把成功构建的二进制部署到已 probe 的设备。只有手机端文件存在且 SHA-256 与构建产物一致时，才返回 deployment_verified=true 和可执行 remote_path；远程 Bridge 模式会自动完成 Linux 下载、Windows spool 校验、HDC 下发与手机端校验。已确认 bridge_id 与 logical_device_id 后，device_lease_id 可省略，由平台自动管理。失败时 requested_remote_path 仅表示目标位置，不能执行；修订后用新的 retry_nonce 触发真实重传。")
def device_deploy_artifact(build_result_artifact_id: str, device_probe_artifact_id: str, timeout_seconds: int = 240, bridge_id: str = "", device_lease_id: str = "", logical_device_id: str = "", retry_nonce: str = "") -> dict[str, Any]:
    value: dict[str, Any] = {"build_result_artifact_id": build_result_artifact_id, "device_probe_artifact_id": device_probe_artifact_id, "timeout_seconds": timeout_seconds, "retry_nonce": retry_nonce}
    value.update({key: item for key, item in {"bridge_id": bridge_id, "device_lease_id": device_lease_id, "logical_device_id": logical_device_id}.items() if item})
    return _action("device.deploy_artifact", value)


@mcp.tool(description="在已 probe 的手机上执行受审计的设备侧 shell 命令；用于运行已部署实验并读取退出码/stdout/stderr。已确认 bridge_id 与 logical_device_id 后，device_lease_id 可省略，由平台自动管理；执行状态未知时先按任务诊断对账。")
def device_execute_shell(device_probe_artifact_id: str, command: str, timeout_seconds: int = 240, bridge_id: str = "", device_lease_id: str = "", logical_device_id: str = "", retry_nonce: str = "") -> dict[str, Any]:
    value: dict[str, Any] = {"device_probe_artifact_id": device_probe_artifact_id, "command": command, "timeout_seconds": timeout_seconds, "retry_nonce": retry_nonce}
    value.update({key: item for key, item in {"bridge_id": bridge_id, "device_lease_id": device_lease_id, "logical_device_id": logical_device_id}.items() if item})
    return _action("device.execute_shell", value)


@mcp.tool(description="从已 probe 的设备收集指定远端文件为平台工件。已确认 bridge_id 与 logical_device_id 后，device_lease_id 可省略，由平台自动管理。")
def device_collect_artifact(device_probe_artifact_id: str, remote_path: str, timeout_seconds: int = 240, bridge_id: str = "", device_lease_id: str = "", logical_device_id: str = "") -> dict[str, Any]:
    value: dict[str, Any] = {"device_probe_artifact_id": device_probe_artifact_id, "remote_path": remote_path, "timeout_seconds": timeout_seconds}
    value.update({key: item for key, item in {"bridge_id": bridge_id, "device_lease_id": device_lease_id, "logical_device_id": logical_device_id}.items() if item})
    return _action("device.collect_artifact", value)


@mcp.tool(
    description=(
        "记录当前 Agent 已加载 skill_bootstrap 指定的主规则和任务 Skill。"
        "skill_discovery 填写本轮实际使用的 Skill 及来源。"
    )
)
def platform_ack_skill_bootstrap(
    bootstrap_id: str,
    orchestrator_version: str,
    skill_discovery: list[SkillBootstrapDiscovery],
    orchestrator_status: Literal["loaded"] = "loaded",
    agent_note: str = "",
) -> dict[str, Any]:
    return _action("platform.ack_skill_bootstrap", {
        "bootstrap_id": bootstrap_id,
        "orchestrator_version": orchestrator_version,
        "orchestrator_status": orchestrator_status,
        "skill_discovery": [item.model_dump() for item in skill_discovery],
        "agent_note": agent_note,
    })


@mcp.tool(description="读取当前 Agent Run 的目标、模式、精简上下文和公开检查点。")
def platform_get_task_context() -> dict[str, Any]:
    return _request("GET", f"/v1/internal/agent-runs/{RUN_ID}/context")


@mcp.tool(
    description=(
        "直接准备当前页面热点的硬件语义路由上下文，返回精确代码、中立画像、平台与自建能力卡以及 "
        "hardware_semantic_routing/v2 输出契约。该工具不调用模型；无需读取平台源码或查询页面 Action 目录。"
    )
)
def reasoning_prepare_hardware_semantic_route() -> dict[str, Any]:
    return _action("reasoning.prepare_hardware_semantic_route", {})


@mcp.tool(
    description=(
        "校验、保存并回填硬件语义路由结果。context_artifact_id 必须来自本轮 "
        "reasoning_prepare_hardware_semantic_route，agent_result 必须符合其返回的契约。"
    )
)
def reasoning_run_hardware_semantic_route(
    context_artifact_id: str,
    agent_result: dict[str, Any],
    reason: str = "",
    retry_nonce: str = "",
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "context_artifact_id": context_artifact_id,
        "agent_result": agent_result,
    }
    if reason:
        value["reason"] = reason
    if retry_nonce:
        value["retry_nonce"] = retry_nonce
    return _action("reasoning.run_hardware_semantic_route", value)


@mcp.tool(
    description=(
        "直接准备当前页面热点的一种机会识别策略上下文。strategy_id 为 kb_rag、hardware_kb 或 "
        "direct_baseline；返回精确 context_artifact_id、模型输入和 opportunity_reasoning/v1 契约。"
    )
)
def reasoning_prepare_context(
    strategy_id: Literal["kb_rag", "hardware_kb", "direct_baseline"],
) -> dict[str, Any]:
    return _action("reasoning.prepare_context", {"strategy_id": strategy_id})


@mcp.tool(
    description=(
        "校验、保存并回填机会识别结果。strategy_id 与 context_artifact_id 必须来自本轮 "
        "reasoning_prepare_context，agent_result 必须符合其返回的契约。"
    )
)
def reasoning_run_strategy(
    strategy_id: Literal["kb_rag", "hardware_kb", "direct_baseline"],
    context_artifact_id: str,
    agent_result: dict[str, Any],
    reason: str = "",
    retry_nonce: str = "",
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "strategy_id": strategy_id,
        "context_artifact_id": context_artifact_id,
        "agent_result": agent_result,
    }
    if reason:
        value["reason"] = reason
    if retry_nonce:
        value["retry_nonce"] = retry_nonce
    return _action("reasoning.run_strategy", value)


@mcp.tool(description="按需查询注册页面动作。已知 action_id 和参数契约时无需调用；未知时优先传 action_ids 精确查询，避免加载整份目录。")
def platform_list_page_actions(action_ids: list[str] | None = None, include_schemas: bool = False) -> dict[str, Any]:
    payload = _request("GET", f"/v1/internal/agent-runs/{RUN_ID}/page-actions")
    actions = payload.get("actions") if isinstance(payload.get("actions"), list) else []
    requested = {str(item) for item in (action_ids or []) if str(item).strip()}
    if requested:
        actions = [item for item in actions if isinstance(item, dict) and str(item.get("action_id") or "") in requested]
    if not include_schemas:
        actions = [
            {
                "action_id": str(item.get("action_id") or ""),
                "risk_level": item.get("risk_level"),
            }
            for item in actions
            if isinstance(item, dict) and str(item.get("action_id") or "")
        ]
    return {
        "schema_version": payload.get("schema_version"),
        "run_id": payload.get("run_id"),
        "actions": actions,
        "schemas_included": include_schemas,
        "filtered": bool(requested),
    }


@mcp.tool(description="请求当前活动浏览器执行一个已注册页面动作，并等待真实结果。Skill 或任务已给出精确 action_id 与参数时直接调用；只有契约未知时才查询动作目录。")
def ui_execute_registered_action(action_id: str, input: dict[str, Any]) -> dict[str, Any]:
    return _action(action_id, input)


@mcp.tool(description="按明确 artifact_type 与 artifact_id 读取平台工件；工件 ID 绝不是文件路径。")
def platform_read_artifact(artifact_type: str, artifact_id: str) -> dict[str, Any]:
    return _request("GET", f"/v1/internal/agent-runs/{RUN_ID}/artifacts/{artifact_type}/{artifact_id}")


@mcp.tool(description="读取场景模板、连接方式、本机 HDC 状态、Windows Bridge 目录与步骤操作合同。设备目标由页面选择的 connection_mode、bridge_id、device_serial 完整路径确定。")
def scenario_capture_catalog() -> dict[str, Any]:
    return _action("scenario_capture.catalog", {})


@mcp.tool(description="根据用户步骤建立版本化采集计划。plan含title、template_id、natural_language_steps、steps、profile和capture。capture.selected_step_ids决定采集窗口，preparation_mode为auto或already_prepared。")
def scenario_capture_create_plan(plan: dict[str, Any]) -> dict[str, Any]:
    return _action("scenario_capture.create_plan", plan)


@mcp.tool(description="读取完整步骤、界面定位、选中采集窗口和计划就绪状态。")
def scenario_capture_read_plan(plan_id: str) -> dict[str, Any]:
    return _action("scenario_capture.read_plan", {"plan_id": plan_id})


@mcp.tool(description="生成包含完整采集计划的 Python 自动化脚本，支持平台所在主机 HDC 直连及 Windows Bridge。")
def scenario_capture_generate_script(plan_id: str) -> dict[str, Any]:
    return _action("scenario_capture.generate_script", {"plan_id": plan_id})


@mcp.tool(description="发现采集设备：未选目标时 auto 汇总本机及在线 Windows Bridge，返回候选、各路径 diagnostics 和 discovery_complete。完整发现仅有一台时可自动选择；多台或局部探测失败时依据用户明确选择。已有目标时用其 connection_mode 与 bridge_id 定向轻量发现；auto 携带 bridge_id 时仅发现该 Bridge。")
def scenario_capture_discover_devices(bridge_id: str = "", connection_mode: str = "auto") -> dict[str, Any]:
    return _action("scenario_capture.discover_devices", {"bridge_id": bridge_id, "connection_mode": connection_mode})


@mcp.tool(description="执行步骤采集，传入 plan_id 及页面任务快照中的 connection_mode、bridge_id、device_serial 完整目标路径，本机直连 bridge_id 为空。准备、执行、回收与故障诊断均沿用选定目标；未选目标先发现并选择。返回异步 job_id/run_id，parallel_job_read 读取进度。自动修复任务携带页面提供的 repair_request_id，平台绑定原设备、保留采集点并执行一次重试；相同请求复用原任务，关闭后停止新重试。新的 retry_nonce 标识普通手动重采批次。")
def scenario_capture_execute(plan_id: str, bridge_id: str = "", device_serial: str = "", retry_nonce: str = "", connection_mode: str = "auto", repair_request_id: str = "") -> dict[str, Any]:
    return _action("scenario_capture.execute", {"plan_id": plan_id, "bridge_id": bridge_id, "device_serial": device_serial, "retry_nonce": retry_nonce, "connection_mode": connection_mode,
        **({"repair_request_id": repair_request_id} if repair_request_id else {})})


@mcp.tool(description="读取采集批次、步骤数据文件、设备信息和数据质量。")
def scenario_capture_read_run(run_id: str) -> dict[str, Any]:
    return _action("scenario_capture.read_run", {"run_id": run_id})


@mcp.tool(description="读取内置图库数据集、硬件画像、当前硬件可用的已发布校准模型、离线原生代理回退模型与适用域，以及条件并行搜索空间。省略模型时优先选择硬件兼容的已发布校准版本。")
def parallel_modeling_catalog() -> dict[str, Any]:
    return _action("parallel_modeling.catalog", {})


@mcp.tool(description="导入建模数据。dataset支持builtin_id，或capture_run_id与step_id、target_tid、target_vsync_id，或trace_text，或parsed解析JSON。可选PMU需同时提供pmu_text与真实pmu_metadata；已核验图库样本按指纹自动匹配硬件；其他新数据可在导入时选择hardware_profile_id。")
def parallel_modeling_import_dataset(dataset: dict[str, Any]) -> dict[str, Any]:
    return _action("parallel_modeling.import_dataset", dataset)


@mcp.tool(description="执行BuildItem关键路径拆解、运行/等待账本和负载特征提取，返回异步job_id。")
def parallel_modeling_analyze(dataset_id: str, model_package_id: str = "", feature_profile_id: str = "") -> dict[str, Any]:
    return _action("parallel_modeling.analyze", {"dataset_id": dataset_id,
        **({"model_package_id": model_package_id} if model_package_id else {}),
        **({"feature_profile_id": feature_profile_id} if feature_profile_id else {})})


@mcp.tool(description="读取关键路径、BuildItem与服务依赖、负载特征和建模质量。")
def parallel_modeling_read_analysis(analysis_id: str) -> dict[str, Any]:
    return _action("parallel_modeling.read_analysis", {"analysis_id": analysis_id})


@mcp.tool(description="执行条件离散事件仿真与并行核频寻优，保留observed依赖。search含analysis_id、model_package_id、feature_profile_id、candidate_ids、objective、budget_ms、robustness；space含available_core_group_ids/main_core_group_id/worker_counts/main_participation/frequency_policy（all_opps或max_opps）/frequencies_khz。0 worker需显式选择才包含串行。返回异步job_id，预测不是应用改造实测。")
def parallel_modeling_search(search: dict[str, Any]) -> dict[str, Any]:
    return _action("parallel_modeling.search", search)


@mcp.tool(description="读取推荐并行配置、预测耗时/收益、资源代价、候选排序和模型适用条件。")
def parallel_modeling_read_search(search_id: str) -> dict[str, Any]:
    return _action("parallel_modeling.read_search", {"search_id": search_id})


@mcp.tool(description="读取负载特征分类方法、指标定义、采样质量条件与阶段级测量数据格式。")
def workload_features_catalog() -> dict[str, Any]:
    return _action("workload_features.catalog", {})


@mcp.tool(description="列出已保存的阶段级负载特征模型及其数据集关联，供页面和并行寻优选择。")
def workload_features_list_profiles() -> dict[str, Any]:
    return _action("workload_features.list_profiles", {})


@mcp.tool(description="针对已导入dataset_id构建BuildItem阶段级负载特征模型，提取计算、访存、调度及服务特征。可选measurements需符合catalog给出的同阶段同窗口测量格式。返回持久化profile，可用其id作为并行建模和搜索的feature_profile_id。")
def workload_features_analyze(dataset_id: str, measurements: dict[str, Any] | None = None) -> dict[str, Any]:
    return _action("workload_features.analyze", {"dataset_id": dataset_id,
        **({"measurements": measurements} if measurements is not None else {})})


@mcp.tool(description="读取负载特征模型完整阶段指标、分类依据、缺失项与来源。")
def workload_features_read_profile(profile_id: str) -> dict[str, Any]:
    return _action("workload_features.read_profile", {"profile_id": profile_id})


@mcp.tool(description="读取采集、建模或寻优任务的进度、结果与错误信息。")
def parallel_job_read(job_id: str) -> dict[str, Any]:
    return _action("parallel_job.read", {"job_id": job_id})


@mcp.tool(description="停止后台采集或建模任务，保留已有运行记录。")
def parallel_job_cancel(job_id: str) -> dict[str, Any]:
    return _action("parallel_job.cancel", {"job_id": job_id})


_publish_portable_mcp_tool_schemas()


if __name__ == "__main__":
    mcp.run(transport="stdio")
