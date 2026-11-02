"""Server-authoritative progress for page-based optimization analysis."""

from __future__ import annotations

import json
from typing import Any

from .core import PlatformError


FULL_ANALYSIS_SCOPE = "full_optimization_analysis"
FOCUSED_EXECUTION_SCOPE = "focused_execution"
NEW_INPUT_TARGET = "new_input"
CURRENT_PAGE_TARGET = "current_page"

HOTSPOT_ACTION = "assistant.use_pending_text_as_hotspot"
BLIND_ACTION = "reasoning.run_blind_analysis"
ROUTE_ACTION = "reasoning.run_hardware_semantic_route"
STRATEGY_ACTION = "reasoning.run_strategy"
BLIND_COMPARE_ACTION = "reasoning.compare_blind_flow"

DOWNSTREAM_RUNTIME_ACTION_PREFIXES = ("platform.", "build.", "device.")


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any, limit: int = 1024) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def page_analysis_contract(run: dict[str, Any]) -> dict[str, Any]:
    if str(run.get("mode") or "") != "page_agent":
        return {}
    entry = _record(_record(run.get("checkpoint")).get("entry_triage"))
    if entry.get("execution_scope") != FULL_ANALYSIS_SCOPE:
        return {}
    return entry


def _page_context(run: dict[str, Any]) -> dict[str, Any]:
    value = _record(run.get("context")).get("page_context")
    if isinstance(value, str):
        try:
            return _record(json.loads(value))
        except json.JSONDecodeError:
            return {}
    return _record(value)


def _request_observations(run: dict[str, Any], contract: dict[str, Any]) -> list[dict[str, Any]]:
    checkpoint = _record(run.get("checkpoint"))
    completed = _record(checkpoint.get("completed_action_keys"))
    baseline = {
        item for item in contract.get("baseline_action_keys", [])
        if isinstance(item, str)
    }
    return [
        value for key, value in completed.items()
        if key not in baseline
        and isinstance(value, dict)
    ]


def _latest_action(observations: list[dict[str, Any]], action_id: str) -> dict[str, Any]:
    for observation in reversed(observations):
        if observation.get("action_id") == action_id:
            return observation
    return {}


def _action_failure_is_retryable(observation: dict[str, Any]) -> bool:
    if observation.get("status") != "failed":
        return False
    code = _text(_record(observation.get("error")).get("code"), 120)
    return code in {"UI_ACTION_TIMEOUT"}


def _initial_page_facts(run: dict[str, Any]) -> dict[str, Any]:
    reasoning = _record(_page_context(run).get("reasoning_lab"))
    route = _record(reasoning.get("hardware_routing"))
    blind = _record(reasoning.get("blind_baseline"))
    strategies = reasoning.get("strategy_results") if isinstance(reasoning.get("strategy_results"), list) else []
    return {
        "has_code": bool(_record(reasoning.get("code_unit"))),
        "route": route if route.get("status") == "succeeded" or route.get("result_artifact_id") else {},
        "blind": blind if blind.get("result_artifact_id") else {},
        "blind_comparison": blind if blind.get("comparison_artifact_id") else {},
        "strategies": [
            item for item in strategies
            if isinstance(item, dict) and item.get("status") in {"succeeded", "completed"}
        ],
    }


def _required_hotspot_input(contract: dict[str, Any]) -> dict[str, Any]:
    required: dict[str, Any] = {
        "input_id": _text(contract.get("target_input_id"), 120),
    }
    selection = _record(contract.get("source_selection"))
    content_mode = _text(selection.get("content_mode"), 40)
    if content_mode in {"entire_input", "first_code_fence", "line_range"}:
        required["content_mode"] = content_mode
    if content_mode == "line_range":
        required["line_start"] = selection.get("line_start")
        required["line_end"] = selection.get("line_end")
    return required


def page_analysis_progress(run: dict[str, Any]) -> dict[str, Any]:
    """Return the next required persisted workbench artifact for this request."""

    contract = page_analysis_contract(run)
    if not contract:
        return {"active": False, "stage": "not_applicable", "ready": True}

    all_observations = _request_observations(run, contract)
    observations = [item for item in all_observations if item.get("status") == "succeeded"]
    initial = _initial_page_facts(run)
    target = _text(contract.get("analysis_target"), 40) or NEW_INPUT_TARGET

    hotspot = _latest_action(observations, HOTSPOT_ACTION)
    source_ready = bool(hotspot) or (target == CURRENT_PAGE_TARGET and initial["has_code"])
    if not source_ready:
        failed = _latest_action(all_observations, HOTSPOT_ACTION)
        if failed.get("status") == "failed" and not _action_failure_is_retryable(failed):
            return {
                "active": True,
                "ready": False,
                "blocked": True,
                "stage": "hotspot_binding_failed",
                "required_action": None,
                "failed_action": HOTSPOT_ACTION,
                "error": _record(failed.get("error")),
            }
        return {
            "active": True,
            "ready": False,
            "stage": "hotspot_binding",
            "required_action": HOTSPOT_ACTION,
            "target_input_id": _text(contract.get("target_input_id"), 120) or None,
            "required_action_input": _required_hotspot_input(contract),
        }

    route_observation = _latest_action(observations, ROUTE_ACTION)
    route_result = _record(route_observation.get("result")) or (initial["route"] if target == CURRENT_PAGE_TARGET else {})
    if not route_result:
        failed = _latest_action(all_observations, ROUTE_ACTION)
        if failed.get("status") == "failed" and not _action_failure_is_retryable(failed):
            return {
                "active": True,
                "ready": False,
                "blocked": True,
                "stage": "hardware_routing_failed",
                "required_action": None,
                "failed_action": ROUTE_ACTION,
                "error": _record(failed.get("error")),
            }
        return {
            "active": True,
            "ready": False,
            "stage": "hardware_routing",
            "required_action": ROUTE_ACTION,
        }

    selected_units = route_result.get("selected_units") if isinstance(route_result.get("selected_units"), list) else []
    required_strategy = "hardware_kb" if selected_units else "direct_baseline"
    # A new route is a new hypothesis.  Only strategy observations produced
    # after the latest route can satisfy the workflow; prior results remain
    # immutable history and are available for comparison.
    route_index = next(
        (index for index in range(len(observations) - 1, -1, -1) if observations[index] is route_observation),
        -1,
    )
    strategy_observations = [
        item for item in observations[route_index + 1:]
        if item.get("action_id") == STRATEGY_ACTION
        and _record(item.get("input")).get("strategy_id") == required_strategy
    ]
    initial_strategy_ready = target == CURRENT_PAGE_TARGET and any(
        item.get("strategy_id") == required_strategy for item in initial["strategies"]
    )
    if not strategy_observations and not initial_strategy_ready:
        failed = next((
            item for item in reversed(all_observations)
            if item.get("action_id") == STRATEGY_ACTION
            and _record(item.get("input")).get("strategy_id") == required_strategy
            and item.get("status") == "failed"
        ), {})
        if failed and not _action_failure_is_retryable(failed):
            return {
                "active": True,
                "ready": False,
                "blocked": True,
                "stage": "opportunity_reasoning_failed",
                "required_action": None,
                "required_strategy_id": required_strategy,
                "failed_action": STRATEGY_ACTION,
                "error": _record(failed.get("error")),
            }
        return {
            "active": True,
            "ready": False,
            "stage": "opportunity_reasoning",
            "required_action": STRATEGY_ACTION,
            "required_strategy_id": required_strategy,
            "required_action_input": {"strategy_id": required_strategy},
            "selected_unit_count": len(selected_units),
        }

    device_execution_observation = _latest_action(observations, "device.execute_shell")
    device_execution_observed = bool(device_execution_observation)
    blind_observation = _latest_action(observations, BLIND_ACTION)
    blind_ready = bool(blind_observation) or (target == CURRENT_PAGE_TARGET and bool(initial["blind"]))
    comparison_observation = _latest_action(observations, BLIND_COMPARE_ACTION)
    comparison_ready = bool(comparison_observation) or (target == CURRENT_PAGE_TARGET and bool(initial["blind_comparison"]))
    recommended_action = None
    recommended_action_reason = None
    if device_execution_observed and not blind_ready:
        recommended_action = BLIND_ACTION
        recommended_action_reason = (
            "流程推理与真机执行均已形成记录；现在启动隔离的盲推理基线，再诊断流程资产的增益或干扰。"
        )
    elif device_execution_observed and blind_ready and not comparison_ready:
        recommended_action = BLIND_COMPARE_ACTION
        recommended_action_reason = (
            "后置盲推理已完成；建议对照冻结的盲推结论、流程结论与真机证据。"
        )
    return {
        "active": True,
        "ready": True,
        "stage": "analysis_ready",
        "required_action": None,
        "required_strategy_id": required_strategy,
        "selected_unit_count": len(selected_units),
        "device_execution_observed": device_execution_observed,
        "blind_baseline_ready": blind_ready,
        "blind_comparison_ready": comparison_ready,
        "recommended_action": recommended_action,
        "recommended_action_reason_zh": recommended_action_reason,
    }


def validate_page_analysis_action(action_id: str, input_value: dict[str, Any], run: dict[str, Any]) -> None:
    """Prevent backend/device work from bypassing visible analysis artifacts."""

    progress = page_analysis_progress(run)
    if not progress.get("active") or progress.get("ready"):
        return
    if progress.get("blocked") and action_id.startswith(DOWNSTREAM_RUNTIME_ACTION_PREFIXES):
        raise PlatformError(
            "PAGE_ANALYSIS_BLOCKED",
            "前序页面分析 action 已失败，不能继续模板、构建或设备阶段。",
            status_code=409,
            details={
                "attempted_action": action_id,
                "workflow_stage": progress.get("stage"),
                "failed_action": progress.get("failed_action"),
                "error": progress.get("error"),
                "recovery_zh": "保留失败记录并向用户说明；修正输入或环境后由新请求在同一分析记录中重试。",
            },
        )
    required_action = str(progress.get("required_action") or "")
    if action_id == required_action:
        required_input = _record(progress.get("required_action_input"))
        mismatches = {
            key: {"expected": expected, "received": input_value.get(key)}
            for key, expected in required_input.items()
            if expected not in {None, ""} and input_value.get(key) != expected
        }
        if mismatches:
            raise PlatformError(
                "PAGE_ANALYSIS_ACTION_INPUT_MISMATCH",
                "页面分析 action 的输入与入口阶段冻结的目标不一致。",
                status_code=409,
                details={
                    "action_id": action_id,
                    "workflow_stage": progress.get("stage"),
                    "required_action_input": required_input,
                    "mismatches": mismatches,
                    "recovery_zh": "使用 required_action_input 原样调用当前 action，不要重新猜测源码范围或策略。",
                },
            )
        return
    blocked = False
    if action_id.startswith(DOWNSTREAM_RUNTIME_ACTION_PREFIXES):
        blocked = True
    elif action_id == ROUTE_ACTION and required_action == HOTSPOT_ACTION:
        blocked = True
    elif action_id == STRATEGY_ACTION and required_action in {HOTSPOT_ACTION, ROUTE_ACTION}:
        blocked = True
    if not blocked:
        return
    raise PlatformError(
        "PAGE_ANALYSIS_STAGE_REQUIRED",
        "完整优化分析必须先生成并回填当前阶段的工作台工件。",
        status_code=409,
        details={
            "attempted_action": action_id,
            "workflow_stage": progress.get("stage"),
            "required_action": required_action,
            "required_action_input": progress.get("required_action_input"),
            "required_strategy_id": progress.get("required_strategy_id"),
            "target_input_id": progress.get("target_input_id"),
            "recovery_zh": "调用 required_action，等待浏览器返回真实结果，再继续处理缺失阶段。",
        },
    )


def page_analysis_completion_correction(run: dict[str, Any]) -> str | None:
    progress = page_analysis_progress(run)
    if not progress.get("active") or progress.get("ready") or progress.get("blocked"):
        return None
    required_action = str(progress.get("required_action") or "")
    required_input = json.dumps(
        _record(progress.get("required_action_input")),
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return (
        "RUNTIME_PAGE_ANALYSIS_INCOMPLETE：本次完整优化分析尚未生成可回填左侧工作台的必要工件。"
        f" current_stage={progress.get('stage')}；required_action={required_action}；"
        f"required_action_input={required_input}；"
        "请先调用该页面 action 并等待真实结构化结果，不要直接跳到模板、编译、设备工具或只在聊天中给出结论。"
        "结果返回后继续处理缺失阶段；工作流不限制模型推理轮次。"
    )
