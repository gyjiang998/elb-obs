"""Generic API shortlist retrieval from a model-produced semantic profile.

This is intentionally different from the legacy unit/API keyword routers.  A
model first chooses a bounded hardware unit from capability cards and describes
the visible computation in its own semantic profile.  This module only compares
that profile with API-record descriptions inside the selected unit directory.
It never infers a unit from source names and never treats a lexical overlap as
API support.
"""

from __future__ import annotations

import re
from typing import Any


PLATFORM_ROUTABLE_UNITS = frozenset({"npu", "gpu", "dsp", "media"})


def _flatten_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return " ".join(_flatten_text(item) for item in value)
    if isinstance(value, dict):
        return " ".join(_flatten_text(item) for item in value.values())
    return ""


def _tokens(value: str) -> set[str]:
    normalized = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", value).casefold()
    latin = {token for token in re.findall(r"[a-z][a-z0-9_]{1,}", normalized) if len(token) >= 2}
    cjk_runs = re.findall(r"[\u4e00-\u9fff]{2,}", normalized)
    cjk_bigrams = {run[index : index + 2] for run in cjk_runs for index in range(len(run) - 1)}
    return latin | cjk_bigrams


def _route_query(route_result: dict[str, Any]) -> tuple[set[str], list[str]]:
    profile = route_result.get("semantic_profile") if isinstance(route_result.get("semantic_profile"), dict) else {}
    candidates = route_result.get("candidates") if isinstance(route_result.get("candidates"), list) else []
    values: list[str] = [_flatten_text(profile)]
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        values.extend(
            [
                str(candidate.get("analysis_scope") or ""),
                _flatten_text(candidate.get("observed_code_evidence")),
            ]
        )
    tokens = _tokens(" ".join(values))
    return tokens, sorted(tokens)[:32]


def _record_text(record: dict[str, Any]) -> str:
    fields = (
        "record_id",
        "symbol",
        "name",
        "one_line_abstract_zh",
        "detailed_description_zh",
        "semantic_family",
        "workflow_role",
        "code_feature_match",
        "construction_path",
        "runtime_verification",
    )
    return " ".join(_flatten_text(record.get(field)) for field in fields)


def _candidate(
    unit_id: str,
    record: dict[str, Any],
    query_tokens: set[str],
    query_preview: list[str],
) -> dict[str, Any] | None:
    record_tokens = _tokens(_record_text(record))
    overlap = sorted(query_tokens.intersection(record_tokens))
    if not overlap:
        return None
    record_id = str(record.get("record_id") or "")
    if not record_id:
        return None
    if unit_id == "npu":
        prefix = "npu_api"
    elif unit_id in {"gpu", "dsp", "media"}:
        prefix = "accelerator_api"
    else:
        prefix = "custom_api_record"
    return {
        "evidence_id": f"{prefix}:{record_id}",
        "score": len(overlap),
        "match_terms": overlap[:8],
        "retrieval_rationale": {
            "method": "model_semantic_profile_text_retrieval/v1",
            "selected_unit": unit_id,
            "query_terms": query_preview,
            "reasons": ["模型语义画像与 API 记录的公开语义字段存在文本重合"],
            "statement_zh": "这是以模型语义画像为查询的 API 简介候选，不是由源码关键词规则选出的 API，也不是接口或设备支持结论。",
        },
        "record": record,
    }


def _eligible_records(
    unit_id: str,
    npu_records: list[dict[str, Any]],
    accelerator_records: dict[str, list[dict[str, Any]]],
    custom_records: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    if unit_id == "npu":
        return [
            record
            for record in npu_records
            if isinstance(record, dict) and str(record.get("record_type") or "").startswith("npu_")
        ]
    if unit_id in PLATFORM_ROUTABLE_UNITS:
        return [
            record
            for record in accelerator_records.get(unit_id, [])
            if isinstance(record, dict) and str(record.get("record_type") or "") == "accelerator_api_entrypoint"
        ]
    return [
        record
        for record in custom_records.get(unit_id, [])
        if isinstance(record, dict) and str(record.get("record_type") or "").startswith("custom_accelerator_api_")
    ]


def rank_api_candidates_from_model_route(
    route_result: dict[str, Any],
    *,
    npu_records: list[dict[str, Any]],
    accelerator_records: dict[str, list[dict[str, Any]]],
    custom_records: dict[str, list[dict[str, Any]]] | None = None,
    per_unit_limit: int,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Shortlist APIs only inside model-selected unit directories.

    The model route itself is a hypothesis.  Empty overlap stays empty rather
    than falling back to a hand-written DSP/GPU/media or operator rule.
    """

    available_custom_records = custom_records or {}
    available_units = {*PLATFORM_ROUTABLE_UNITS, *available_custom_records}
    candidate_units = route_result.get("candidates") if isinstance(route_result.get("candidates"), list) else []
    routed_units = [
        str(item.get("unit_id") or "").casefold()
        for item in candidate_units
        if isinstance(item, dict) and str(item.get("unit_id") or "").casefold() in available_units
    ]
    routed_units = list(dict.fromkeys(routed_units))
    # The routing contract orders candidates by preference.  Opportunity
    # reasoning is a single-directory stage: mixing an alternative unit's API
    # cards into the primary unit prompt lets a high lexical score silently
    # change GPU into NPU (or vice versa).  Alternatives stay in the immutable
    # route artifact and can be selected explicitly in a later run.
    selected_units = routed_units[:1]
    query_tokens, query_preview = _route_query(route_result)
    ranked_all: list[dict[str, Any]] = []
    unit_results: dict[str, dict[str, Any]] = {}
    for unit_id in selected_units:
        eligible = _eligible_records(unit_id, npu_records, accelerator_records, available_custom_records)
        candidates = [item for record in eligible if (item := _candidate(unit_id, record, query_tokens, query_preview)) is not None]
        candidates.sort(
            key=lambda item: (
                -int(item["score"]),
                str((item["record"].get("semantic_family") or {}).get("id") if isinstance(item["record"], dict) else ""),
                str(item["record"].get("symbol") if isinstance(item["record"], dict) else ""),
            )
        )
        limited = candidates[:max(0, per_unit_limit)]
        ranked_all.extend(limited)
        unit_results[unit_id] = {
            "catalog_record_count": len(eligible),
            "candidate_count": len(candidates),
            "returned_count": len(limited),
            "status": "matched" if limited else "no_semantic_profile_text_overlap",
        }
    ranked_all.sort(
        key=lambda item: (
            -int(item["score"]),
            str((item["retrieval_rationale"] or {}).get("selected_unit") or ""),
            str((item["record"] or {}).get("symbol") or ""),
        )
    )
    manual_directory = str(route_result.get("selection_origin") or "") == "user_manual"
    return ranked_all, {
        "method": "model_semantic_profile_text_retrieval/v1",
        "primary_unit_id": selected_units[0] if selected_units else None,
        "selected_units": selected_units,
        "alternative_units_not_searched": routed_units[1:],
        "query_term_count": len(query_tokens),
        "query_terms": query_preview,
        "candidate_count": sum(item["candidate_count"] for item in unit_results.values()),
        "returned_count": len(ranked_all),
        "per_unit_limit": per_unit_limit,
        "unit_results": unit_results,
        "statement_zh": "API 目录由用户人工指定，API 简介仍只按保留的语义画像在该目录内检索；这不是模型或设备能力结论。没有文本相容候选时保持为空，并要求补充计算核心或进一步 API 复核。"
        if manual_directory
        else "API 目录仅在模型已选择的单元内按模型语义画像检索；没有文本相容候选时保持为空，并要求补充计算核心或人工/API 目录复核。",
    }
