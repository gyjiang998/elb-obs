"""Leak-resistant evaluation helpers for the Hardware-KB strategy skill.

The browser may run a configured model against these cases, but the model only
receives ``public_input`` plus a freshly built Hardware-KB context.  Oracles
stay in this local evaluator module and are never included in the prompt.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from copy import deepcopy
from typing import Any

from .reasoning_lab import adapt_reasoning_result, pasted_code_unit, validate_reasoning_result
from .platform_paths import EVALUATION_ROOT


SUITE_PATH = EVALUATION_ROOT / "hardware_kb_strategy_skill_cases.json"
SUITE_SCHEMA_VERSION = "hardware_kb_strategy_skill_evaluation/v1"
EVALUATION_PARTITIONS = frozenset({"smoke", "development", "validation", "holdout"})


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string_list(value: Any) -> list[str]:
    return [item.strip() for item in value if isinstance(item, str) and item.strip()] if isinstance(value, list) else []


def _check_by_id(score: dict[str, Any], check_id: str) -> dict[str, Any]:
    for check in score.get("checks", []):
        item = _record(check)
        if item.get("check_id") == check_id:
            return item
    return {}


def _safe_rate(numerator: int | float, denominator: int | float) -> float | None:
    return round(float(numerator) / float(denominator), 4) if denominator else None


def _quality_band(value: float | None) -> str:
    if value is None:
        return "not_evaluable"
    if value >= 0.9:
        return "healthy"
    if value >= 0.75:
        return "review"
    return "regression"


def load_hardware_strategy_suite(path: Path = SUITE_PATH) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"unable to load hardware strategy suite: {error}") from error
    errors = validate_hardware_strategy_suite(payload)
    if errors:
        raise ValueError("; ".join(errors))
    return payload


def validate_hardware_strategy_suite(suite: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    if suite.get("artifact_type") != "hardware_kb_strategy_skill_evaluation_suite":
        errors.append("suite artifact_type is invalid")
    if suite.get("schema_version") != SUITE_SCHEMA_VERSION:
        errors.append("suite schema_version is invalid")
    if not isinstance(suite.get("suite_id"), str) or not suite["suite_id"]:
        errors.append("suite_id is required")
    cases = suite.get("cases")
    if not isinstance(cases, list) or not cases:
        return [*errors, "cases must be a nonempty list"]
    ids: set[str] = set()
    for index, case in enumerate(cases):
        prefix = f"case[{index}]"
        if not isinstance(case, dict):
            errors.append(f"{prefix} must be an object")
            continue
        case_id = case.get("case_id")
        if not isinstance(case_id, str) or not case_id:
            errors.append(f"{prefix}.case_id is required")
        elif case_id in ids:
            errors.append(f"duplicate case_id: {case_id}")
        else:
            ids.add(case_id)
        if case.get("partition") not in EVALUATION_PARTITIONS:
            errors.append(f"{prefix}.partition is invalid")
        if not isinstance(case.get("family_id"), str) or not case["family_id"].strip():
            errors.append(f"{prefix}.family_id is required")
        if not isinstance(case.get("variation_axis"), str) or not case["variation_axis"].strip():
            errors.append(f"{prefix}.variation_axis is required")
        public_input = _record(case.get("public_input"))
        if not isinstance(public_input.get("code"), str) or not public_input["code"].strip():
            errors.append(f"{prefix}.public_input.code is required")
        if not isinstance(public_input.get("function_hint"), str) or not public_input["function_hint"].strip():
            errors.append(f"{prefix}.public_input.function_hint is required")
        if not isinstance(public_input.get("trace_metrics"), dict):
            errors.append(f"{prefix}.public_input.trace_metrics is required")
        oracle = _record(case.get("oracle"))
        model = _record(oracle.get("model"))
        if model.get("hardware_mode") not in {"required", "forbidden"}:
            errors.append(f"{prefix}.oracle.model.hardware_mode is invalid")
        if model.get("parallel_expectation") not in {"required", "forbidden", "optional"}:
            errors.append(f"{prefix}.oracle.model.parallel_expectation is invalid")
        if not _string_list(model.get("allowed_recommendations")):
            errors.append(f"{prefix}.oracle.model.allowed_recommendations is required")
    return errors


def suite_public_catalog(suite: dict[str, Any]) -> dict[str, Any]:
    """Return an oracle-free catalog safe to expose to the browser."""

    return {
        "suite_id": suite["suite_id"],
        "suite_version": suite.get("suite_version", "unknown"),
        "strategy_contract": deepcopy(_record(suite.get("strategy_contract"))),
        "purpose_zh": str(suite.get("purpose_zh") or ""),
        "case_count": len(suite["cases"]),
        "categories": dict(sorted(Counter(str(case.get("category") or "uncategorized") for case in suite["cases"]).items())),
        "partitions": dict(sorted(Counter(str(case.get("partition") or "unknown") for case in suite["cases"]).items())),
        "families": dict(sorted(Counter(str(case.get("family_id") or "uncategorized") for case in suite["cases"]).items())),
        "evaluation_policy": deepcopy(_record(suite.get("evaluation_policy"))),
        "cases": [
            {
                "case_id": case["case_id"],
                "title_zh": str(case.get("title_zh") or case["case_id"]),
                "category": str(case.get("category") or "uncategorized"),
                "partition": str(case.get("partition") or "unknown"),
                "family_id": str(case.get("family_id") or "uncategorized"),
                "variation_axis": str(case.get("variation_axis") or "unknown"),
                "public_input": deepcopy(_record(case.get("public_input"))),
            }
            for case in suite["cases"]
        ],
    }


def case_by_id(suite: dict[str, Any], case_id: str) -> dict[str, Any]:
    for case in suite["cases"]:
        if case.get("case_id") == case_id:
            return case
    raise ValueError(f"hardware strategy evaluation case does not exist: {case_id}")


def evaluation_code_unit(case: dict[str, Any]) -> dict[str, Any]:
    """Materialize the public synthetic code and timing facts as a code unit."""

    public_input = _record(case.get("public_input"))
    code_unit = pasted_code_unit(
        str(public_input.get("code") or ""),
        str(public_input.get("function_hint") or ""),
        str(public_input.get("event_name") or public_input.get("function_hint") or ""),
    )
    hotspot = _record(code_unit.get("hotspot"))
    hotspot["module"] = str(public_input.get("module") or "skill_evaluation")
    hotspot["trace_metrics"] = deepcopy(_record(public_input.get("trace_metrics")))
    hotspot["critical_path_status"] = "not_provided"
    source = _record(code_unit.get("source"))
    source["origin"] = "skill_evaluation_fixture"
    source["binding_level"] = "unbound"
    return code_unit


def _model_context_evidence_ids(context: dict[str, Any]) -> list[str]:
    model_context = _record(context.get("model_input_context"))
    return [
        str(_record(item).get("evidence_id") or "")
        for item in model_context.get("context_records", [])
        if isinstance(item, dict) and str(_record(item).get("evidence_id") or "")
    ]


def _context_preflight(case: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    oracle = _record(case.get("oracle"))
    expectations = _record(oracle.get("retrieval"))
    evidence_ids = _model_context_evidence_ids(context)
    required = _string_list(expectations.get("required_evidence_ids"))
    forbidden_prefixes = _string_list(expectations.get("forbidden_evidence_prefixes"))
    require_empty = expectations.get("require_empty_accelerator_context") is True
    checks: list[dict[str, Any]] = []
    for evidence_id in required:
        checks.append(
            {
                "check_id": f"retrieval:{evidence_id}",
                "passed": evidence_id in evidence_ids,
                "detail": "所需 API 证据已进入模型上下文。" if evidence_id in evidence_ids else "所需 API 证据未进入模型上下文。",
            }
        )
    for prefix in forbidden_prefixes:
        matched = [item for item in evidence_ids if item.startswith(prefix)]
        checks.append(
            {
                "check_id": f"retrieval_no_prefix:{prefix}",
                "passed": not matched,
                "detail": "未出现禁止的跨单元候选。" if not matched else f"出现禁止候选：{', '.join(matched)}",
            }
        )
    if require_empty:
        accelerator_records = [
            item
            for item in evidence_ids
            if item.startswith(("npu_api:", "accelerator_api:", "hardware:"))
        ]
        checks.append(
            {
                "check_id": "retrieval_empty_accelerator_context",
                "passed": not accelerator_records,
                "detail": "未给模型提供无关的硬件候选。" if not accelerator_records else f"出现无关硬件候选：{', '.join(accelerator_records)}",
            }
        )
    if not checks:
        checks.append({"check_id": "retrieval_no_explicit_oracle", "passed": True, "detail": "该样本只检查模型行为。"})
    return {
        "evidence_ids": evidence_ids,
        "checks": checks,
        "passed": all(check["passed"] for check in checks),
    }


def preflight_case(case: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    return {
        "case_id": str(case.get("case_id") or ""),
        "title_zh": str(case.get("title_zh") or ""),
        "category": str(case.get("category") or ""),
        "partition": str(case.get("partition") or ""),
        "family_id": str(case.get("family_id") or ""),
        "context_sha256": str(context.get("context_sha256") or ""),
        **_context_preflight(case, context),
    }


def _all_result_text(result: dict[str, Any]) -> str:
    values = [
        str(result.get("summary") or ""),
        *(_string_list(result.get("missing_evidence"))),
        *(_string_list(result.get("negative_evidence"))),
    ]
    for opportunity in result.get("opportunities", []):
        item = _record(opportunity)
        values.extend(
            [
                str(item.get("candidate_target") or ""),
                str(item.get("rationale") or ""),
                str(item.get("implementation_sketch") or ""),
                *_string_list(item.get("required_conditions")),
                *_string_list(item.get("risks")),
                *_string_list(item.get("verification_plan")),
            ]
        )
    return "\n".join(values).casefold()


def _opportunities(result: dict[str, Any]) -> list[dict[str, Any]]:
    return [_record(item) for item in result.get("opportunities", []) if isinstance(item, dict)]


def _opportunity_evidence_ids(opportunities: list[dict[str, Any]]) -> set[str]:
    return {item for opportunity in opportunities for item in _string_list(opportunity.get("evidence_ids"))}


def _inferred_units(opportunities: list[dict[str, Any]], context: dict[str, Any]) -> set[str]:
    """Infer a named unit from cited evidence first, then a target label."""

    records_by_evidence = {
        str(_record(item).get("evidence_id") or ""): _record(_record(item).get("record"))
        for item in _record(context.get("model_input_context")).get("context_records", [])
        if isinstance(item, dict)
    }
    inferred: set[str] = set()
    for evidence_id in _opportunity_evidence_ids(opportunities):
        if evidence_id.startswith("npu_api:"):
            inferred.add("npu")
        record = records_by_evidence.get(evidence_id, {})
        unit = str(record.get("accelerator_unit") or "").casefold()
        if unit:
            inferred.add(unit)
        unit_type = str(record.get("unit_type") or "").casefold()
        if "npu" in unit_type or "neural" in unit_type:
            inferred.add("npu")
        if "gpu" in unit_type:
            inferred.add("gpu")
        if "dsp" in unit_type or "audio" in unit_type:
            inferred.add("dsp")
        if "video" in unit_type or "codec" in unit_type:
            inferred.add("media")
    target_text = " ".join(str(item.get("candidate_target") or "") for item in opportunities).casefold()
    if re.search(r"\b(?:npu|nnrt|hiai)\b", target_text):
        inferred.add("npu")
    if re.search(r"\b(?:gpu|vulkan|opengl|opencl)\b", target_text):
        inferred.add("gpu")
    if re.search(r"\b(?:dsp|pcm|audio effect)\b", target_text):
        inferred.add("dsp")
    if re.search(r"\b(?:media|avcodec|hcodec|jpeg|heif|codec component)\b", target_text):
        inferred.add("media")
    return inferred


def _keyword_groups_check(text: str, groups: list[list[str]]) -> tuple[bool, list[dict[str, Any]]]:
    details: list[dict[str, Any]] = []
    for group in groups:
        lowered = [item.casefold() for item in group]
        matched = next((item for item in lowered if item in text), None)
        details.append({"keywords": group, "matched": matched})
    return all(item["matched"] for item in details), details


def _score_check(check_id: str, points: float, passed: bool, detail: str, **extra: Any) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "max_points": points,
        "earned_points": points if passed else 0.0,
        "passed": passed,
        "detail": detail,
        **extra,
    }


def score_case_result(case: dict[str, Any], context: dict[str, Any], model_result: dict[str, Any]) -> dict[str, Any]:
    """Score a browser-produced model result without exposing the oracle.

    Scores are diagnostic, not a declaration that a code path is deployable.
    A passing positive case means the strategy selected a source-supported API
    direction and preserved the device/cost gates.
    """

    normalized, adaptation = adapt_reasoning_result(model_result)
    validated, validation_errors = validate_reasoning_result(normalized, context)
    oracle = _record(case.get("oracle"))
    model_oracle = _record(oracle.get("model"))
    preflight = _context_preflight(case, context)
    opportunities = _opportunities(validated)
    opportunity_kinds = {str(item.get("opportunity_kind") or "") for item in opportunities}
    cited_evidence = _opportunity_evidence_ids(opportunities)
    inferred_units = _inferred_units(opportunities, context)
    expected_api_evidence = set(_string_list(model_oracle.get("required_api_evidence_ids")))
    expected_units = set(_string_list(model_oracle.get("required_units")))
    all_text = _all_result_text(validated)
    checks: list[dict[str, Any]] = []

    # Context correctness is deliberately scored separately: it diagnoses a KB
    # or retriever failure before attributing the miss to the prompt/model.
    checks.append(_score_check(
        "context_preflight",
        25.0,
        preflight["passed"],
        "模型可见的 Hardware-KB 上下文符合该样本的 API/零候选边界。" if preflight["passed"] else "模型上下文未通过 API/零候选预检。",
        preflight=preflight,
    ))

    contract_ok = not validation_errors and _record(validated.get("validation")).get("status") == "valid"
    checks.append(_score_check(
        "result_contract",
        10.0,
        contract_ok,
        "模型结果符合结构化契约且引用未越界。" if contract_ok else f"模型结果存在契约问题：{'；'.join(validation_errors) or 'unknown'}",
        validation_errors=validation_errors,
        format_adaptation=adaptation,
    ))

    scope_declarations = [_record(item.get("analysis_scope")) for item in opportunities]
    scope_disclosure_ok = all(
        str(scope.get("status") or "") != "not_declared"
        and bool(str(scope.get("description") or "").strip())
        and bool(str(scope.get("source_evidence") or "").strip())
        for scope in scope_declarations
    )
    checks.append(_score_check(
        "scope_disclosure",
        5.0,
        scope_disclosure_ok,
        "每个机会都声明了可见源码作用域及其依据。" if scope_disclosure_ok else "至少一个机会未声明实际作用域、源码依据，或把范围标为未声明。",
        opportunity_count=len(opportunities),
    ))

    allowed_recommendations = set(_string_list(model_oracle.get("allowed_recommendations")))
    recommendation_ok = str(validated.get("recommendation") or "") in allowed_recommendations
    checks.append(_score_check(
        "recommendation_calibration",
        10.0,
        recommendation_ok,
        "顶层结论保持了无 device probe 时的证据边界。" if recommendation_ok else f"顶层 recommendation={validated.get('recommendation')}，不在允许集合内。",
        allowed=sorted(allowed_recommendations),
        actual=validated.get("recommendation"),
    ))

    hardware_mode = str(model_oracle.get("hardware_mode") or "")
    hetero_opportunities = [item for item in opportunities if item.get("opportunity_kind") == "heterogeneous"]
    if hardware_mode == "required":
        has_heterogeneous = bool(hetero_opportunities)
        expected_api_selected = bool(expected_api_evidence.intersection(cited_evidence))
        expected_unit_selected = bool(expected_units.intersection(inferred_units))
        checks.append(_score_check(
            "hardware_direction",
            15.0,
            has_heterogeneous and expected_unit_selected,
            "已选择正确的硬件单元方向。" if has_heterogeneous and expected_unit_selected else f"缺少异构方向或单元不匹配；实际单元={sorted(inferred_units)}。",
            expected_units=sorted(expected_units),
            actual_units=sorted(inferred_units),
        ))
        checks.append(_score_check(
            "hardware_api_selection",
            15.0,
            expected_api_selected,
            "异构方向引用了期望 API 证据。" if expected_api_selected else f"未引用期望 API 证据；实际引用={sorted(cited_evidence)}。",
            expected_evidence_ids=sorted(expected_api_evidence),
            actual_evidence_ids=sorted(cited_evidence),
        ))
    else:
        no_heterogeneous_claim = not hetero_opportunities
        checks.append(_score_check(
            "hardware_rejection",
            30.0,
            no_heterogeneous_claim,
            "未把非计算/受约束代码误报为异构卸载候选。" if no_heterogeneous_claim else "结果包含不应出现的 heterogeneous 候选。",
            actual_opportunity_kinds=sorted(opportunity_kinds),
        ))

    parallel_expectation = str(model_oracle.get("parallel_expectation") or "optional")
    has_parallel = "parallel_cpu" in opportunity_kinds
    if parallel_expectation == "required":
        parallel_ok = has_parallel
        parallel_detail = "识别出需要条件验证的 CPU 并行方向。" if parallel_ok else "未识别出金标要求的 CPU 并行方向。"
    elif parallel_expectation == "forbidden":
        parallel_ok = not has_parallel
        parallel_detail = "未对依赖、I/O、锁或单次极小任务给出不当并行建议。" if parallel_ok else "出现了不应出现的 CPU 并行候选。"
    else:
        parallel_ok = True
        parallel_detail = "该样本不以 CPU 并行方向计分。"
    checks.append(_score_check("parallel_judgment", 10.0, parallel_ok, parallel_detail, expectation=parallel_expectation))

    keyword_groups = [list(group) for group in model_oracle.get("verification_keyword_groups", []) if isinstance(group, list)]
    keywords_ok, keyword_details = _keyword_groups_check(all_text, keyword_groups)
    verification_actions = sum(
        len(_string_list(opportunity.get("verification_plan")))
        for opportunity in opportunities
    )
    missing_evidence_count = len(_string_list(validated.get("missing_evidence")))
    # Exact wording differs substantially across providers.  The scoring gate
    # therefore checks that the model proposes a concrete verification path;
    # oracle keywords remain a diagnostic signal, not a brittle pass/fail rule.
    verification_required = bool(keyword_groups)
    verification_ok = not verification_required or verification_actions + missing_evidence_count > 0
    checks.append(_score_check(
        "verification_and_cost_gates",
        10.0,
        verification_ok,
        (
            "该样本不强制额外验证动作。"
            if not verification_required
            else "结果给出了后续验证或缺失证据。"
        ) if verification_ok else "结果没有给出可执行的验证或缺失证据。",
        verification_required=verification_required,
        verification_action_count=verification_actions,
        missing_evidence_count=missing_evidence_count,
        semantic_keyword_coverage=_safe_rate(sum(1 for item in keyword_details if item["matched"]), len(keyword_details)),
        keyword_matches=keyword_details,
        semantic_keyword_diagnostic_passed=keywords_ok,
    ))

    score = round(sum(float(check["earned_points"]) for check in checks), 2)
    hardware_selected = bool(hetero_opportunities)
    if hardware_mode == "required":
        hardware_correct = bool(expected_api_evidence.intersection(cited_evidence)) and bool(expected_units.intersection(inferred_units))
    else:
        hardware_correct = not hardware_selected
    assessment = {
        "hardware_expected": "select" if hardware_mode == "required" else "reject",
        "hardware_selected": hardware_selected,
        "hardware_correct": hardware_correct,
        "parallel_expected": parallel_expectation,
        "parallel_selected": has_parallel,
        "parallel_correct": parallel_ok,
        "semantic_decision_correct": hardware_correct and parallel_ok,
        "context_preflight_passed": bool(preflight["passed"]),
        "contract_valid": contract_ok,
        "scope_disclosed": scope_disclosure_ok,
        "recommendation_calibrated": recommendation_ok,
        "verification_action_present": verification_ok,
        "verification_semantic_keyword_coverage": _safe_rate(sum(1 for item in keyword_details if item["matched"]), len(keyword_details)),
    }
    return {
        "case_id": str(case.get("case_id") or ""),
        "title_zh": str(case.get("title_zh") or ""),
        "category": str(case.get("category") or ""),
        "partition": str(case.get("partition") or ""),
        "family_id": str(case.get("family_id") or ""),
        "context_sha256": str(context.get("context_sha256") or ""),
        "score": score,
        "passed": all(bool(check["passed"]) for check in checks),
        "assessment": assessment,
        "checks": checks,
        "validated_result": validated,
        "validation_errors": validation_errors,
        "format_adaptation": adaptation,
    }


def public_score_result(score: dict[str, Any]) -> dict[str, Any]:
    """Remove evaluator-side expectation fields before returning a browser score.

    A model may see whether its answer passed a check, but it must not receive
    required evidence IDs, required units, context-preflight oracle details or
    the expected select/reject label as a tuning oracle.
    """

    checks = []
    for value in score.get("checks", []):
        check = _record(value)
        checks.append({
            "check_id": str(check.get("check_id") or "unknown"),
            "max_points": check.get("max_points"),
            "earned_points": check.get("earned_points"),
            "passed": bool(check.get("passed")),
            "detail": str(check.get("detail") or ""),
        })
    assessment = _record(score.get("assessment"))
    return {
        "case_id": str(score.get("case_id") or ""),
        "title_zh": str(score.get("title_zh") or ""),
        "category": str(score.get("category") or ""),
        "partition": str(score.get("partition") or ""),
        "family_id": str(score.get("family_id") or ""),
        "context_sha256": str(score.get("context_sha256") or ""),
        "score": score.get("score"),
        "passed": bool(score.get("passed")),
        "assessment": {
            "hardware_direction_correct": bool(assessment.get("hardware_correct")),
            "parallel_judgment_correct": bool(assessment.get("parallel_correct")),
            "semantic_decision_correct": bool(assessment.get("semantic_decision_correct")),
            "context_preflight_passed": bool(assessment.get("context_preflight_passed")),
            "contract_valid": bool(assessment.get("contract_valid")),
            "scope_disclosed": bool(assessment.get("scope_disclosed")),
            "recommendation_calibrated": bool(assessment.get("recommendation_calibrated")),
            "verification_action_present": bool(assessment.get("verification_action_present")),
            "verification_semantic_keyword_coverage": assessment.get("verification_semantic_keyword_coverage"),
        },
        "checks": checks,
        "validated_result": deepcopy(_record(score.get("validated_result"))),
        "validation_errors": _string_list(score.get("validation_errors")),
        "format_adaptation": deepcopy(_record(score.get("format_adaptation"))),
    }


def public_evaluation_report(report: dict[str, Any]) -> dict[str, Any]:
    """Return a report whose per-case diagnostics cannot reveal oracle data."""

    sanitized = deepcopy(report)
    sanitized["cases"] = [
        public_score_result(_record(score))
        for score in report.get("cases", [])
        if isinstance(score, dict)
    ]
    return sanitized


def _subset_metrics(items: list[tuple[dict[str, Any], dict[str, Any]]]) -> dict[str, Any]:
    """Summarize semantic behavior without treating an aggregate score as truth."""

    if not items:
        return {
            "case_count": 0,
            "diagnostic_score_average": None,
            "semantic_decision_rate": None,
            "quality_dimensions": {},
            "hardware": {"case_count": 0},
            "cpu_parallel": {"case_count": 0},
        }

    def assessment(score: dict[str, Any]) -> dict[str, Any]:
        supplied = _record(score.get("assessment"))
        if supplied:
            return supplied
        return {
            "hardware_expected": "select" if _check_by_id(score, "hardware_direction") else "reject",
            "hardware_selected": not bool(_check_by_id(score, "hardware_rejection")),
            "hardware_correct": bool(_check_by_id(score, "hardware_direction").get("passed", _check_by_id(score, "hardware_rejection").get("passed"))),
            "parallel_expected": str(_check_by_id(score, "parallel_judgment").get("expectation") or "optional"),
            "parallel_selected": False,
            "parallel_correct": bool(_check_by_id(score, "parallel_judgment").get("passed")),
            "semantic_decision_correct": False,
            "context_preflight_passed": bool(_check_by_id(score, "context_preflight").get("passed")),
            "contract_valid": bool(_check_by_id(score, "result_contract").get("passed")),
            "scope_disclosed": bool(_check_by_id(score, "scope_disclosure").get("passed")),
            "recommendation_calibrated": bool(_check_by_id(score, "recommendation_calibration").get("passed")),
            "verification_action_present": bool(_check_by_id(score, "verification_and_cost_gates").get("passed")),
        }

    assessments = [assessment(score) for _, score in items]

    def binary_metrics(expected_key: str, actual_key: str, *, exclude_optional: bool = False) -> dict[str, Any]:
        observations = [
            item for item in assessments
            if not (exclude_optional and item.get(expected_key) == "optional")
        ]
        positive_expected = [item.get(expected_key) in {"select", "required"} for item in observations]
        positive_actual = [bool(item.get(actual_key)) for item in observations]
        true_positive = sum(expected and actual for expected, actual in zip(positive_expected, positive_actual))
        false_negative = sum(expected and not actual for expected, actual in zip(positive_expected, positive_actual))
        false_positive = sum(not expected and actual for expected, actual in zip(positive_expected, positive_actual))
        true_negative = sum(not expected and not actual for expected, actual in zip(positive_expected, positive_actual))
        recall = _safe_rate(true_positive, true_positive + false_negative)
        specificity = _safe_rate(true_negative, true_negative + false_positive)
        precision = _safe_rate(true_positive, true_positive + false_positive)
        balanced_accuracy = (
            round((recall + specificity) / 2, 4)
            if recall is not None and specificity is not None
            else recall if specificity is None else specificity
        )
        return {
            "case_count": len(observations),
            "true_positive": true_positive,
            "false_negative": false_negative,
            "false_positive": false_positive,
            "true_negative": true_negative,
            "precision": precision,
            "recall": recall,
            "specificity": specificity,
            "balanced_accuracy": balanced_accuracy,
        }

    def rate(field: str) -> float | None:
        return _safe_rate(sum(bool(item.get(field)) for item in assessments), len(assessments))

    hint_values = [
        value for item in assessments
        if isinstance((value := item.get("verification_semantic_keyword_coverage")), (int, float))
    ]
    quality_dimensions = {
        "context_preflight_rate": rate("context_preflight_passed"),
        "result_contract_rate": rate("contract_valid"),
        "scope_disclosure_rate": rate("scope_disclosed"),
        "recommendation_calibration_rate": rate("recommendation_calibrated"),
        "verification_action_rate": rate("verification_action_present"),
        "semantic_hint_coverage": round(sum(hint_values) / len(hint_values), 4) if hint_values else None,
        "semantic_decision_rate": rate("semantic_decision_correct"),
    }
    hardware = binary_metrics("hardware_expected", "hardware_selected")
    parallel = binary_metrics("parallel_expected", "parallel_selected", exclude_optional=True)
    return {
        "case_count": len(items),
        "diagnostic_score_average": round(sum(float(score.get("score") or 0.0) for _, score in items) / len(items), 2),
        "diagnostic_all_checks_passed_count": sum(bool(score.get("passed")) for _, score in items),
        "quality_dimensions": quality_dimensions,
        "hardware": hardware,
        "cpu_parallel": parallel,
        "quality_bands": {
            "safety_calibration": _quality_band(quality_dimensions["recommendation_calibration_rate"]),
            "hardware_semantics": _quality_band(hardware.get("balanced_accuracy")),
            "parallel_semantics": _quality_band(parallel.get("balanced_accuracy")),
            "overall_semantic": _quality_band(quality_dimensions["semantic_decision_rate"]),
        },
    }


def summarize_case_scores(suite: dict[str, Any], scores: list[dict[str, Any]]) -> dict[str, Any]:
    expected_ids = {str(case.get("case_id") or "") for case in suite["cases"]}
    score_by_id = {str(score.get("case_id") or ""): score for score in scores}
    unknown = sorted(set(score_by_id) - expected_ids)
    missing = sorted(expected_ids - set(score_by_id))
    cases_by_id = {str(case.get("case_id") or ""): case for case in suite["cases"]}
    completed = [score for score in scores if str(score.get("case_id") or "") in expected_ids]
    completed_items = [(cases_by_id[str(score.get("case_id") or "")], score) for score in completed]
    by_category: dict[str, list[dict[str, Any]]] = {}
    for score in completed:
        by_category.setdefault(str(score.get("category") or "uncategorized"), []).append(score)
    check_stats: dict[str, list[bool]] = {}
    for score in completed:
        for check in score.get("checks", []):
            item = _record(check)
            check_stats.setdefault(str(item.get("check_id") or "unknown"), []).append(bool(item.get("passed")))
    average = round(sum(float(score.get("score") or 0.0) for score in completed) / len(completed), 2) if completed else 0.0
    expected_partitions = Counter(str(case.get("partition") or "unknown") for case in suite["cases"])
    completed_by_partition: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    completed_by_family: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for case, score in completed_items:
        completed_by_partition.setdefault(str(case.get("partition") or "unknown"), []).append((case, score))
        completed_by_family.setdefault(str(case.get("family_id") or "uncategorized"), []).append((case, score))
    return {
        "artifact_type": "hardware_kb_strategy_skill_evaluation_report",
        "schema_version": SUITE_SCHEMA_VERSION,
        "suite_id": suite["suite_id"],
        "suite_version": suite.get("suite_version", "unknown"),
        "suite_content_sha256": _sha256(suite),
        "coverage": {
            "expected_case_count": len(expected_ids),
            "completed_case_count": len(completed),
            "missing_case_ids": missing,
            "unknown_case_ids": unknown,
            "partitions": {
                partition: {
                    "expected_case_count": expected_count,
                    "completed_case_count": len(completed_by_partition.get(partition, [])),
                }
                for partition, expected_count in sorted(expected_partitions.items())
            },
        },
        "score": {
            "average": average,
            "passed_case_count": sum(bool(score.get("passed")) for score in completed),
            "failed_case_count": sum(not bool(score.get("passed")) for score in completed),
            "interpretation_zh": "逐例分数用于诊断约束失效；不作为泛化或发布的单一结论。",
        },
        "evaluation_policy": deepcopy(_record(suite.get("evaluation_policy"))),
        "quality_summary": _subset_metrics(completed_items),
        "partition_metrics": {
            partition: _subset_metrics(items)
            for partition, items in sorted(completed_by_partition.items())
        },
        "family_metrics": {
            family: _subset_metrics(items)
            for family, items in sorted(completed_by_family.items())
        },
        "category_metrics": {
            category: {
                "case_count": len(items),
                "average_score": round(sum(float(item.get("score") or 0.0) for item in items) / len(items), 2),
                "passed_case_count": sum(bool(item.get("passed")) for item in items),
            }
            for category, items in sorted(by_category.items())
        },
        "check_pass_rates": {
            check_id: round(sum(values) / len(values), 4)
            for check_id, values in sorted(check_stats.items())
            if values
        },
        "cases": completed,
    }


def build_reference_result(case: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Create an evaluator self-test response; never expose this to a model."""

    model_oracle = _record(_record(case.get("oracle")).get("model"))
    expected_api = _string_list(model_oracle.get("required_api_evidence_ids"))
    expected_units = _string_list(model_oracle.get("required_units"))
    source_sha = str(_record(context.get("source")).get("sha256") or "")
    opportunities: list[dict[str, Any]] = []
    if model_oracle.get("hardware_mode") == "required":
        opportunities.append(
            {
                "opportunity_kind": "heterogeneous",
                "candidate_target": expected_units[0] if expected_units else "model_hypothesis",
                "analysis_scope": {
                    "level": "function",
                    "status": "observed",
                    "description": "公开 fixture 中可见的函数级计算或数据处理范围；模型仍需定位其中的最小核心片段。",
                    "source_evidence": "本 case 的 public_input.code 与当前 source evidence。",
                },
                "rationale": "静态代码形态与当前 Hardware-KB API 记录相符，但设备可用性和收益尚未被探测。",
                "required_conditions": ["必须完成目标设备能力、数据格式和端到端成本验证。"],
                "risks": ["搬运、同步、格式转换或回退成本可能抵消收益。"],
                "evidence_ids": [*expected_api, f"source:{source_sha}"],
                "verification_plan": [
                    f"验证 {keyword_group[0]}。"
                    for keyword_group in model_oracle.get("verification_keyword_groups", [])
                    if isinstance(keyword_group, list) and keyword_group
                ],
                "implementation_sketch": "先完成设备侧 gate，再比较 CPU/software fallback 与目标路径。",
            }
        )
    if model_oracle.get("parallel_expectation") == "required":
        opportunities.append(
            {
                "opportunity_kind": "parallel_cpu",
                "candidate_target": "CPU 分块任务（待验证）",
                "analysis_scope": {
                    "level": "function",
                    "status": "observed",
                    "description": "公开 fixture 中可见的函数级工作范围；实际切分点需要根据循环或数据边界复核。",
                    "source_evidence": "本 case 的 public_input.code 与当前 source evidence。",
                },
                "rationale": "需要先验证任务之间的独立性、同步和粒度。",
                "required_conditions": ["线程任务之间没有未处理的数据竞争或服务端串行瓶颈。"],
                "risks": ["线程切换、锁和归并成本可能抵消收益。"],
                "evidence_ids": [f"source:{source_sha}"],
                "verification_plan": [
                    f"检查 {keyword_group[0]}。"
                    for keyword_group in model_oracle.get("verification_keyword_groups", [])
                    if isinstance(keyword_group, list) and keyword_group
                ],
                "implementation_sketch": "仅在规模阈值和同步边界满足后采用分块任务。",
            }
        )
    return {
        "schema_version": "opportunity_reasoning/v1",
        "recommendation": _string_list(model_oracle.get("allowed_recommendations"))[0],
        "confidence": "low",
        "evidence_status": "insufficient_evidence",
        "summary": "当前结论保留为待验证；已区分源码静态语义、运行时设备门禁和端到端成本。",
        "opportunities": opportunities,
        "missing_evidence": [
            f"需要验证 {keyword_group[0]}。"
            for keyword_group in model_oracle.get("verification_keyword_groups", [])
            if isinstance(keyword_group, list) and keyword_group
        ],
        "negative_evidence": ["没有 device probe 或端到端 benchmark，不能将静态 API 候选升级为设备事实。"],
    }
