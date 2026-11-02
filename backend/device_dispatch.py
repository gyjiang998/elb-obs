"""Model-first semantic dispatch contracts for connected-device validation.

The model, not a source-text fingerprint, decides whether a visible code scope
is a plausible offload candidate for the unit selected by the user.  This
module only builds a bounded, auditable context and validates the returned
contract's structure.  It intentionally does *not* compile or execute model
generated code; device execution remains limited to separately reviewed
recipes.
"""

from __future__ import annotations

from typing import Any

from .core import sha256_json


DEVICE_UNITS = frozenset({"npu", "gpu", "dsp", "media"})
DECISIONS = frozenset({"semantic_candidate", "needs_contract", "not_suitable"})
MAX_TEXT_LENGTH = 1200
MAX_LIST_ITEMS = 8


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any, limit: int = MAX_TEXT_LENGTH) -> str:
    if not isinstance(value, str):
        return ""
    normalized = value.strip()
    return normalized[:limit]


def _strings(value: Any, *, limit: int = MAX_LIST_ITEMS, item_limit: int = 600) -> list[str]:
    if not isinstance(value, list):
        return []
    return [_text(item, item_limit) for item in value if _text(item, item_limit)][:limit]


def _source_reference(code_artifact: dict[str, Any]) -> dict[str, Any]:
    source = _record(code_artifact.get("source"))
    return {
        "origin": _text(source.get("origin")),
        "binding_level": _text(source.get("binding_level")),
        "line_start": source.get("line_start") if isinstance(source.get("line_start"), int) else None,
        "line_end": source.get("line_end") if isinstance(source.get("line_end"), int) else None,
    }


def _model_context(reasoning_context: dict[str, Any], code_artifact: dict[str, Any]) -> dict[str, Any]:
    context = _record(reasoning_context.get("model_input_context"))
    code = _text(context.get("code"), 24 * 1024) or _text(code_artifact.get("code"), 24 * 1024)
    return {
        "code": code,
        "code_language": _text(context.get("code_language")) or "cpp",
        "code_observations": _record(context.get("code_observations")),
        "candidate_scope_method": _text(context.get("candidate_scope_method")),
        "candidate_scopes": [
            item for item in context.get("candidate_scopes", [])
            if isinstance(item, dict)
        ][:4],
        "hardware_routing": _record(context.get("hardware_routing")),
        "context_records": [
            item for item in context.get("context_records", [])
            if isinstance(item, dict)
        ][:8],
        "allowed_evidence_ids": _strings(context.get("allowed_evidence_ids"), limit=32, item_limit=160),
    }


def _record_unit(item: dict[str, Any]) -> str:
    evidence_id = _text(item.get("evidence_id"), 240)
    record = _record(item.get("record"))
    unit = _text(record.get("accelerator_unit")) or _text(record.get("unit_type"))
    if unit in DEVICE_UNITS:
        return unit
    if evidence_id.startswith("npu_api:"):
        return "npu"
    if evidence_id.startswith("accelerator_api:"):
        parts = evidence_id.split(":", 2)
        if len(parts) >= 2 and parts[1] in DEVICE_UNITS:
            return parts[1]
    return ""


def _project_evidence(item: dict[str, Any]) -> dict[str, Any]:
    raw = _record(item.get("record"))
    return {
        "evidence_id": _text(item.get("evidence_id"), 240),
        "record": {
            key: raw[key]
            for key in (
                "record_id",
                "name",
                "symbol",
                "accelerator_unit",
                "unit_type",
                "one_line_abstract_zh",
                "semantic_family",
                "code_feature_match",
                "construction_path",
                "capability_boundary",
                "runtime_verification",
                "source_contract",
            )
            if key in raw
        },
    }


def _strategy_summary(reasoning_result: dict[str, Any]) -> dict[str, Any]:
    result = _record(reasoning_result.get("result"))
    opportunities: list[dict[str, Any]] = []
    for item in result.get("opportunities", []):
        if not isinstance(item, dict):
            continue
        opportunities.append(
            {
                "opportunity_kind": _text(item.get("opportunity_kind"), 80),
                "candidate_target": _text(item.get("candidate_target"), 240),
                "analysis_scope": _record(item.get("analysis_scope")),
                "rationale": _text(item.get("rationale")),
                "required_conditions": _strings(item.get("required_conditions"), limit=5),
                "risks": _strings(item.get("risks"), limit=5),
                "verification_plan": _strings(item.get("verification_plan"), limit=5),
                "evidence_ids": _strings(item.get("evidence_ids"), limit=8, item_limit=160),
            }
        )
    return {
        "strategy_id": _text(reasoning_result.get("strategy_id"), 80),
        "recommendation": _text(result.get("recommendation"), 80),
        "confidence": _text(result.get("confidence"), 80),
        "evidence_status": _text(result.get("evidence_status"), 80),
        "summary": _text(result.get("summary")),
        "opportunities": opportunities[:3],
        "missing_evidence": _strings(result.get("missing_evidence"), limit=8),
        "negative_evidence": _strings(result.get("negative_evidence"), limit=8),
    }


def build_device_dispatch_context(
    *,
    code_artifact: dict[str, Any],
    reasoning_result_artifact: dict[str, Any],
    reasoning_context_artifact: dict[str, Any],
    probe_artifact: dict[str, Any],
    requested_unit: str,
) -> dict[str, Any]:
    """Build one bounded model context without using reference-source matching.

    ``requested_unit`` is a user selection for this assessment.  The model may
    reject it and list alternatives, but a model answer cannot turn that choice
    into device support or executable code.
    """

    unit = requested_unit.strip().casefold()
    if unit not in DEVICE_UNITS:
        raise ValueError("unsupported requested device unit")

    model_context = _model_context(reasoning_context_artifact, code_artifact)
    observed_units = _record(probe_artifact.get("observed_units"))
    unit_observation = _record(observed_units.get(unit))
    records = model_context["context_records"]
    relevant_records = [item for item in records if _record_unit(item) == unit]
    route = _record(model_context.get("hardware_routing"))
    route_units = [
        _text(_record(item).get("unit_id"), 40)
        for item in route.get("selected_units", [])
        if isinstance(item, dict)
    ][:2]
    evidence_ids = {
        *model_context["allowed_evidence_ids"],
        *[_text(item.get("evidence_id"), 240) for item in relevant_records],
        f"device_probe:{_text(probe_artifact.get('artifact_id'), 160)}",
    }
    evidence_ids.discard("")
    model_input_context = {
        "schema_version": "device_dispatch_model_input/v1",
        "task": "针对用户选定硬件单元，判断可见热点核心是否可形成下发合同；判断由模型语义分析完成，而不是按参考源码或函数名匹配。",
        "requested_unit": unit,
        "source": _source_reference(code_artifact),
        "code_language": model_context["code_language"],
        "code": model_context["code"],
        "code_observations": model_context["code_observations"],
        "candidate_scope_method": model_context["candidate_scope_method"],
        "candidate_scopes": model_context["candidate_scopes"],
        "prior_strategy": _strategy_summary(reasoning_result_artifact),
        "hardware_routing": {
            "selection_origin": _text(route.get("selection_origin"), 80) or "not_available",
            "selected_units": route_units,
            "statement_zh": _text(route.get("statement_zh")),
        },
        "selected_unit_evidence": [_project_evidence(item) for item in relevant_records[:5]],
        "device_observation": {
            "unit_id": unit,
            "observation_status": _text(unit_observation.get("observation_status"), 80) or "not_observed",
            "observed_capabilities": _strings(unit_observation.get("observed_capabilities"), limit=8),
            "claim_boundary_zh": _text(unit_observation.get("claim_boundary_zh")),
            "recommended_next_step_zh": _text(unit_observation.get("recommended_next_step_zh")),
        },
        "constraints": [
            "模型负责语义适配判断；参考源码的字节匹配不是本轮判断条件。",
            "设备观察只能说明本次探测到的运行时线索，不能证明目标代码已经下发。",
            "模型不得生成 HDC、shell、驱动代码或任意可执行命令。后续实测只能使用平台注册的受控配方。",
            "若输入、输出、状态、副作用或数值语义不足以形成合同，应返回 needs_contract，而不是把任意循环断言为可下发。",
        ],
        "allowed_evidence_ids": sorted(evidence_ids),
    }
    return {
        "schema_version": "device_dispatch_context/v1",
        "code_artifact_id": _text(code_artifact.get("artifact_id"), 160),
        "reasoning_result_artifact_id": _text(reasoning_result_artifact.get("artifact_id"), 160),
        "reasoning_context_artifact_id": _text(reasoning_context_artifact.get("artifact_id"), 160),
        "probe_artifact_id": _text(probe_artifact.get("artifact_id"), 160),
        "requested_unit": unit,
        "model_input_context": model_input_context,
        "context_sha256": sha256_json(model_input_context),
    }


def _validate_scope(value: Any, errors: list[str]) -> dict[str, Any]:
    scope = _record(value)
    level = _text(scope.get("level"), 80)
    status = _text(scope.get("status"), 80)
    description = _text(scope.get("description"))
    source_evidence = _text(scope.get("source_evidence"))
    if level not in {"function", "loop_region", "data_pipeline", "callee", "unknown"}:
        errors.append("candidate_scope.level 不受支持")
    if status not in {"observed", "callee_unresolved", "not_declared"}:
        errors.append("candidate_scope.status 不受支持")
    if not description:
        errors.append("candidate_scope.description 不能为空")
    if not source_evidence:
        errors.append("candidate_scope.source_evidence 不能为空")
    return {
        "level": level,
        "status": status,
        "description": description,
        "source_evidence": source_evidence,
    }


def _validate_contract(value: Any, errors: list[str]) -> dict[str, Any]:
    contract = _record(value)
    normalized = {
        "semantic_operations": _strings(contract.get("semantic_operations")),
        "input_assumptions": _strings(contract.get("input_assumptions")),
        "output_semantics": _strings(contract.get("output_semantics")),
        "state_and_side_effects": _strings(contract.get("state_and_side_effects")),
        "required_adapter_work": _strings(contract.get("required_adapter_work")),
        "cpu_fallback": _strings(contract.get("cpu_fallback")),
        "result_comparison": _strings(contract.get("result_comparison")),
    }
    if not normalized["semantic_operations"]:
        errors.append("dispatch_contract.semantic_operations 不能为空")
    if not normalized["input_assumptions"]:
        errors.append("dispatch_contract.input_assumptions 不能为空")
    if not normalized["required_adapter_work"]:
        errors.append("dispatch_contract.required_adapter_work 不能为空")
    if not normalized["cpu_fallback"]:
        errors.append("dispatch_contract.cpu_fallback 不能为空")
    if not normalized["result_comparison"]:
        errors.append("dispatch_contract.result_comparison 不能为空")
    return normalized


def validate_device_dispatch_assessment(
    result: dict[str, Any],
    context: dict[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Validate structure and provenance, never a hard-coded code pattern.

    The only automatic downgrade here is for an incomplete or untraceable
    contract.  It is deliberately independent of source paths, names and
    bytes, so cosmetic source edits cannot erase a semantic candidate.
    """

    errors: list[str] = []
    requested_unit = _text(context.get("requested_unit"), 40)
    model_context = _record(context.get("model_input_context"))
    allowed_evidence = set(_strings(model_context.get("allowed_evidence_ids"), limit=64, item_limit=240))
    normalized = dict(result)
    if _text(normalized.get("schema_version"), 80) != "device_dispatch_assessment/v1":
        errors.append("schema_version 必须为 device_dispatch_assessment/v1")
    decision = _text(normalized.get("decision"), 80)
    if decision not in DECISIONS:
        errors.append("decision 必须为 semantic_candidate、needs_contract 或 not_suitable")
    target_unit = _text(normalized.get("target_unit"), 40)
    if target_unit not in {*DEVICE_UNITS, "none"}:
        errors.append("target_unit 必须为 npu、gpu、dsp、media 或 none")
    if decision == "semantic_candidate" and target_unit != requested_unit:
        errors.append("semantic_candidate 必须针对本轮用户选择的硬件单元")
    summary = _text(normalized.get("summary"))
    reasoning = _text(normalized.get("reasoning"))
    if not summary:
        errors.append("summary 不能为空")
    if not reasoning:
        errors.append("reasoning 不能为空")
    execution_mode = _text(normalized.get("execution_mode"), 120)
    if not execution_mode:
        errors.append("execution_mode 不能为空")
    scope = _validate_scope(normalized.get("candidate_scope"), errors)
    evidence_ids = _strings(normalized.get("evidence_ids"), limit=12, item_limit=240)
    if any(item not in allowed_evidence for item in evidence_ids):
        errors.append("evidence_ids 包含本轮上下文以外的证据")
    code_evidence = _strings(normalized.get("observed_code_evidence"), limit=8)
    if decision == "semantic_candidate" and not code_evidence:
        errors.append("semantic_candidate 必须列出可见源码依据")
    dispatch_contract = _validate_contract(normalized.get("dispatch_contract"), errors) if decision == "semantic_candidate" else {}
    alternatives = _strings(normalized.get("alternative_units"), limit=3, item_limit=40)
    if any(item not in DEVICE_UNITS for item in alternatives):
        errors.append("alternative_units 只能包含已知硬件单元")
    validated: dict[str, Any] = {
        "schema_version": "device_dispatch_assessment/v1",
        "decision": decision,
        "target_unit": target_unit,
        "execution_mode": execution_mode,
        "summary": summary,
        "reasoning": reasoning,
        "candidate_scope": scope,
        "observed_code_evidence": code_evidence,
        "dispatch_contract": dispatch_contract,
        "required_device_checks": _strings(normalized.get("required_device_checks")),
        "risks": _strings(normalized.get("risks")),
        "alternative_units": alternatives,
        "evidence_ids": evidence_ids,
    }
    calibration_actions: list[str] = []
    if errors and decision == "semantic_candidate":
        validated["decision"] = "needs_contract"
        validated["dispatch_contract"] = {}
        calibration_actions.append("模型给出的下发合同缺少可审计字段，已降为 needs_contract；不是由源码文本匹配导致。")
    validated["validation"] = {
        "status": "valid" if not errors else "invalid",
        "errors": errors,
        "calibration_actions": calibration_actions,
    }
    return validated, errors
