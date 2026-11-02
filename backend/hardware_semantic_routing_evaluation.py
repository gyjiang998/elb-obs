"""Oracle-isolated evaluation helpers for model-first hardware routing."""

from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from typing import Any

from .hardware_semantic_routing import build_hardware_semantic_routing_context, validate_hardware_semantic_routing_result
from .platform_paths import EVALUATION_ROOT
from .reasoning_lab import canonical_sha256, pasted_code_unit


SUITE_PATH = EVALUATION_ROOT / "hardware_semantic_routing_generalization_cases.json"


def load_semantic_routing_suite(path: Path = SUITE_PATH) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("cases"), list):
        raise ValueError("invalid hardware semantic routing evaluation suite")
    return payload


def case_by_id(suite: dict[str, Any], case_id: str) -> dict[str, Any]:
    for case in suite.get("cases", []):
        if isinstance(case, dict) and str(case.get("case_id") or "") == case_id:
            return case
    raise ValueError(f"unknown hardware semantic routing case: {case_id}")


def public_suite_catalog(suite: dict[str, Any]) -> dict[str, Any]:
    cases = [case for case in suite.get("cases", []) if isinstance(case, dict)]
    return {
        "suite_id": str(suite.get("suite_id") or "unknown"),
        "suite_version": str(suite.get("suite_version") or "unknown"),
        "purpose_zh": str(suite.get("purpose_zh") or ""),
        "case_count": len(cases),
        "categories": dict(sorted(Counter(str(case.get("category") or "unknown") for case in cases).items())),
        "partitions": dict(sorted(Counter(str(case.get("partition") or "unknown") for case in cases).items())),
        "families": dict(sorted(Counter(str(case.get("family_id") or "unknown") for case in cases).items())),
        "cases": [
            {
                "case_id": str(case.get("case_id") or ""),
                "title_zh": str(case.get("title_zh") or ""),
                "category": str(case.get("category") or "unknown"),
                "partition": str(case.get("partition") or "unknown"),
                "family_id": str(case.get("family_id") or "unknown"),
                "variation_axis": str(case.get("variation_axis") or "unknown"),
            }
            for case in cases
        ],
    }


def prepare_case_context(
    case: dict[str, Any],
    hardware_records: list[dict[str, Any]],
    target_platform_profile: dict[str, Any],
) -> dict[str, Any]:
    public_input = case.get("public_input") if isinstance(case.get("public_input"), dict) else {}
    code = str(public_input.get("code") or "")
    hint = str(public_input.get("function_hint") or "")
    unit = pasted_code_unit(code, hint, hint)
    context = build_hardware_semantic_routing_context(
        unit,
        hardware_records,
        target_platform_profile=target_platform_profile,
    )
    # Bind the otherwise ephemeral pasted-code unit to this public fixture.
    context["evaluation_case_id"] = str(case.get("case_id") or "")
    context["context_sha256"] = canonical_sha256({key: value for key, value in context.items() if key != "context_sha256"})
    return context


def score_case_result(case: dict[str, Any], context: dict[str, Any], raw_result: dict[str, Any]) -> dict[str, Any]:
    normalized, validation_errors = validate_hardware_semantic_routing_result(raw_result, context)
    oracle = case.get("oracle") if isinstance(case.get("oracle"), dict) else {}
    allowed_units = {str(value) for value in oracle.get("allowed_units", []) if isinstance(value, str)}
    selected_units = {
        str(item.get("unit_id") or "")
        for item in normalized.get("candidates", [])
        if isinstance(item, dict)
    }
    expected_decision = str(oracle.get("decision") or "conservative")
    if expected_decision == "select_units":
        correct = normalized.get("decision") == "select_units" and bool(selected_units.intersection(allowed_units))
        assessment = "selected_expected_unit" if correct else "missed_or_wrong_unit"
    else:
        correct = normalized.get("decision") != "select_units" and not selected_units
        assessment = "conservative" if correct else "overrouted"
    return {
        "case_id": str(case.get("case_id") or ""),
        "category": str(case.get("category") or "unknown"),
        "partition": str(case.get("partition") or "unknown"),
        "family_id": str(case.get("family_id") or "unknown"),
        "variation_axis": str(case.get("variation_axis") or "unknown"),
        "score": 1.0 if correct and not validation_errors else 0.0,
        "correct": bool(correct and not validation_errors),
        "assessment": assessment if not validation_errors else "contract_invalid",
        "model_decision": normalized.get("decision"),
        "model_selected_units": sorted(selected_units),
        "validation_errors": validation_errors,
    }


def summarize_case_scores(suite: dict[str, Any], scores: list[dict[str, Any]]) -> dict[str, Any]:
    score_by_case = {str(score.get("case_id") or ""): score for score in scores}
    cases = [case for case in suite.get("cases", []) if isinstance(case, dict)]
    positive_cases = [case for case in cases if str((case.get("oracle") or {}).get("decision") or "") == "select_units"]
    negative_cases = [case for case in cases if case not in positive_cases]
    completed_positive = [score_by_case.get(str(case.get("case_id") or "")) for case in positive_cases]
    completed_negative = [score_by_case.get(str(case.get("case_id") or "")) for case in negative_cases]

    def ratio(items: list[dict[str, Any] | None]) -> float | None:
        available = [item for item in items if item is not None]
        return None if not available else sum(1 for item in available if item.get("correct")) / len(available)

    families: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        score = score_by_case.get(str(case.get("case_id") or ""))
        if score is not None:
            families[str(case.get("family_id") or "unknown")].append(score)
    family_invariance = {
        family_id: all(bool(item.get("correct")) for item in family_scores)
        for family_id, family_scores in families.items()
        if len(family_scores) >= 2
    }
    transformed_families = [value for value in family_invariance.values()]
    by_partition: dict[str, dict[str, Any]] = {}
    for partition in sorted({str(case.get("partition") or "unknown") for case in cases}):
        partition_scores = [score for score in scores if str(score.get("partition") or "") == partition]
        by_partition[partition] = {
            "case_count": len(partition_scores),
            "accuracy": None if not partition_scores else sum(1 for score in partition_scores if score.get("correct")) / len(partition_scores),
        }
    return {
        "suite_id": str(suite.get("suite_id") or "unknown"),
        "suite_version": str(suite.get("suite_version") or "unknown"),
        "completed_case_count": len(scores),
        "selection_recall": ratio(completed_positive),
        "rejection_specificity": ratio(completed_negative),
        "balanced_accuracy": (
            None
            if ratio(completed_positive) is None or ratio(completed_negative) is None
            else (ratio(completed_positive) + ratio(completed_negative)) / 2
        ),
        "transformation_family_invariance": None if not transformed_families else sum(transformed_families) / len(transformed_families),
        "family_invariance": family_invariance,
        "by_partition": by_partition,
        "scores": scores,
        "statement_zh": "重命名和等价改写要求在同一语义族内保持正确；领域语义被完全删去的样本以保守不路由为正确，不要求模型凭纯循环结构猜硬件。",
    }


def public_score(score: dict[str, Any]) -> dict[str, Any]:
    return dict(score)


def report_fingerprint(report: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(report, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
