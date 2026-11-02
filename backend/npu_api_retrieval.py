"""Deterministic Top-N retrieval over the source-derived NPU API catalog.

The retriever is deliberately conservative.  It ranks *NNRT graph operation
candidates* from observable source text and function/event identifiers; it does
not claim that the code is a neural-network graph or that a target device can
execute the candidate.  Those questions stay behind NNRT's runtime support and
build gates.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any


# These names occur frequently in ordinary C/C++ identifiers.  They only
# become an NNRT signal when the source also establishes a tensor/image data
# domain.  Specific aliases such as ``matmul`` or ``depthwise_conv2d`` remain
# meaningful on their own and continue to rank before broad family matches.
AMBIGUOUS_OPERATOR_ALIASES = frozenset({
    "abs", "add", "all", "ceil", "cos", "div", "exp", "floor", "log",
    "max", "min", "mod", "mul", "neg", "pow", "round", "sin", "sqrt",
    "square", "sub", "tile",
})


def _text_for_unit(code_unit: dict[str, Any]) -> str:
    hotspot = code_unit.get("hotspot") if isinstance(code_unit.get("hotspot"), dict) else {}
    raw = " ".join(
        (
            str(code_unit.get("function_hint") or ""),
            str(hotspot.get("event_name") or ""),
            str(code_unit.get("code") or ""),
        )
    )
    # Expose Stable components of C++ CamelCase names without losing source
    # operators and punctuation used below.
    return re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", raw).casefold()


def _tokens(text: str) -> set[str]:
    raw_tokens = re.findall(r"[a-z][a-z0-9_]{1,}", text)
    parts = [piece for token in raw_tokens for piece in token.split("_") if len(piece) >= 2]
    # CamelCase is normalized before tokenization, so ``MatMul`` becomes
    # ``mat_mul``.  Keep its compact form as well: NNRT's public operator is
    # spelled ``MATMUL`` and this is a lexical identifier normalization, not a
    # semantic-family expansion.
    compact = [token.replace("_", "") for token in raw_tokens if "_" in token]
    adjacent_parts: list[str] = []
    for token in raw_tokens:
        token_parts = [part for part in token.split("_") if len(part) >= 2]
        adjacent_parts.extend(f"{token_parts[index]}{token_parts[index + 1]}" for index in range(len(token_parts) - 1))
    return set(raw_tokens) | set(parts) | set(compact) | set(adjacent_parts)


def static_semantic_observations(code_unit: dict[str, Any]) -> dict[str, Any]:
    """Return bounded, lexical source observations used by this retriever.

    This is intentionally not called an AST result: a real AST/core extractor
    can replace or enrich it later without changing the retrieval contract.
    """

    code = str(code_unit.get("code") or "")
    text = _text_for_unit(code_unit)
    tokens = _tokens(text)
    loop_count = len(re.findall(r"\b(?:for|while|do)\b", code))
    array_access_count = len(re.findall(r"\[[^\]\n]{1,80}\]", code))
    has_multiply_accumulate = bool(
        re.search(r"(?:\+=\s*[^;\n]*\*|\*[^;\n]*\+=|\bfma\s*\()", code)
    )
    has_elementwise_add = bool(re.search(r"\[[^\]]+\]\s*(?:\+=|=\s*[^;\n]*\+)", code))
    has_elementwise_sub = bool(re.search(r"\[[^\]]+\]\s*(?:-=|=\s*[^;\n]*-)", code))
    has_elementwise_mul = bool(re.search(r"\[[^\]]+\]\s*(?:\*=|=\s*[^;\n]*\*)", code))
    has_elementwise_div = bool(re.search(r"\[[^\]]+\]\s*(?:/=|=\s*[^;\n]*/)", code))
    # A recurrence such as ``output[i] = output[i - 1] + input[i]`` is not a
    # regular independent elementwise node.  It needs a scan/rewrite analysis,
    # not a generic ADD/SUB candidate.
    has_loop_carried_array_dependency = bool(
        re.search(
            r"\b([a-zA-Z_]\w*)\s*\[\s*\w+\s*\]\s*=\s*\1\s*\[\s*\w+\s*[-+]\s*\d+\s*\]",
            code,
        )
    )
    feature_flags = {
        "tensor_or_image_terms": any(token in tokens for token in {"tensor", "feature", "featuremap", "image", "pixel", "pixels", "rgba", "rgb", "nhwc", "nchw"}),
        "matrix_terms": any(token in tokens for token in {"matrix", "matmul", "gemm", "weight", "kernel"}),
        "convolution_terms": any(token in tokens for token in {"conv", "convolution", "filter", "kernel", "stride", "padding", "dilation"}),
        "normalization_terms": any(token in tokens for token in {"norm", "normalization", "variance", "epsilon", "mean"}),
        "activation_terms": any(token in tokens for token in {"relu", "sigmoid", "softmax", "tanh", "gelu", "swish"}),
        "reduction_terms": any(token in tokens for token in {"reduce", "sum", "mean", "argmax", "top", "axis"}),
        "shape_layout_terms": any(token in tokens for token in {"reshape", "transpose", "nchw", "nhwc", "split", "concat", "layout"}),
        "indexing_terms": any(token in tokens for token in {"gather", "scatter", "slice", "index", "padding", "sparse"}),
    }
    return {
        "analysis_method": "lexical_semantic_signal/v1",
        "loop_count": loop_count,
        "array_access_count": array_access_count,
        "has_multiply_accumulate": has_multiply_accumulate,
        "has_elementwise_add": has_elementwise_add,
        "has_elementwise_sub": has_elementwise_sub,
        "has_elementwise_mul": has_elementwise_mul,
        "has_elementwise_div": has_elementwise_div,
        "has_loop_carried_array_dependency": has_loop_carried_array_dependency,
        "feature_flags": feature_flags,
        # The caller needs tokens for scoring but model contexts do not need an
        # unconstrained token dump.
        "_tokens": sorted(tokens),
    }


def _operation_score(record: dict[str, Any], observations: dict[str, Any]) -> tuple[int, list[str], list[str]]:
    match = record.get("code_feature_match") if isinstance(record.get("code_feature_match"), dict) else {}
    # Exact aliases are intentionally separate from broad family vocabulary.
    # Older artifacts may expose only `semantic_aliases`; retain that fallback
    # for compatibility but new source-built records always use the precise
    # `operator_aliases` field.
    aliases = [
        str(value).casefold()
        for value in (match.get("operator_aliases") or match.get("semantic_aliases") or [])
        if isinstance(value, str)
    ]
    tokens = set(observations.get("_tokens") or [])
    direct_aliases = [alias for alias in aliases if alias in tokens and len(alias) >= 3]
    flags = observations.get("feature_flags") if isinstance(observations.get("feature_flags"), dict) else {}
    if not flags.get("tensor_or_image_terms"):
        direct_aliases = [alias for alias in direct_aliases if alias not in AMBIGUOUS_OPERATOR_ALIASES]
    # Prefer a more specific exact alias (for example depthwise_conv2d) over
    # its broader sibling (conv2d) when both can explain the same source.
    alias_specificity = sum(min(alias.count("_") + 1, 3) for alias in direct_aliases)
    score = min(len(direct_aliases), 4) * 5 + min(alias_specificity, 4)
    reasons = [f"标识符/语义别名命中：{', '.join(direct_aliases[:4])}"] if direct_aliases else []

    family = record.get("semantic_family") if isinstance(record.get("semantic_family"), dict) else {}
    family_id = str(family.get("id") or "")
    loops = int(observations.get("loop_count") or 0)
    arrays = int(observations.get("array_access_count") or 0)

    # A family receives evidence only from a source pattern that is meaningful
    # for that family. Generic C++ identifiers and a lone arithmetic token do
    # not create a NPU candidate.
    if family_id == "matrix" and loops >= 2 and arrays >= 2 and observations.get("has_multiply_accumulate"):
        score += 9
        reasons.append("观察到嵌套循环、数组访问与乘加累积")
        symbol = str(record.get("symbol") or "")
        # A bare three-level output-row/output-column/reduction multiply-
        # accumulate loop is the most general visible form of MatMul. A full
        # connection can lower to the same primitive, but should only outrank
        # it when the source names a dense/weight/bias semantic explicitly.
        # This keeps an unnamed C/C++ kernel from being needlessly described as
        # a neural-network layer merely because both records share a matrix
        # family.
        if symbol.endswith("_MATMUL") and loops >= 3 and arrays >= 3:
            score += 5
            reasons.append("三层输出行/列/归约乘加循环更接近通用 MatMul 形态")
    elif family_id == "convolution" and loops >= 2 and arrays >= 2 and flags.get("convolution_terms"):
        score += 8
        reasons.append("观察到空间/卷积术语、嵌套循环与规则数组访问")
    elif family_id == "pooling" and direct_aliases and loops >= 2 and arrays >= 2 and flags.get("tensor_or_image_terms"):
        score += 5
        reasons.append("观察到图像/张量循环与窗口类数据访问")
    elif (
        family_id == "elementwise_arithmetic"
        and loops >= 1
        and arrays >= 1
        and flags.get("tensor_or_image_terms")
        and not observations.get("has_loop_carried_array_dependency")
    ):
        symbol = str(record.get("symbol") or "")
        elementwise_name_signals = {
            "_ELTWISE": "eltwise" in tokens or "elementwise" in tokens,
            "_MAXIMUM": "maximum" in tokens or "max" in tokens,
            "_MINIMUM": "minimum" in tokens or "min" in tokens,
            "_POW": "pow" in tokens,
            "_SCALE": "scale" in tokens,
            "_SQUARED_DIFFERENCE": "squared_difference" in tokens or "difference" in tokens,
            "_ABS": "abs" in tokens,
            "_SQUARE": "square" in tokens,
            "_MOD": "mod" in tokens,
            "_NEG": "neg" in tokens or "negative" in tokens,
            "_CLIP": "clip" in tokens,
            "_BIAS_ADD": "bias" in tokens and observations.get("has_elementwise_add"),
        }
        operator_signal = (
            (symbol.endswith("_ADD") and not symbol.endswith("_BIAS_ADD") and observations.get("has_elementwise_add"))
            or (symbol.endswith("_SUB") and observations.get("has_elementwise_sub"))
            or (symbol.endswith("_MUL") and observations.get("has_elementwise_mul"))
            or (symbol.endswith("_DIV") and observations.get("has_elementwise_div"))
            or any(symbol.endswith(suffix) and present for suffix, present in elementwise_name_signals.items())
        )
        if operator_signal:
            score += 5
            reasons.append("观察到张量/图像语义下的循环内规则逐元素数组读写")
    elif family_id == "activation" and direct_aliases and loops >= 1 and arrays >= 1 and flags.get("activation_terms"):
        score += 5
        reasons.append("观察到激活函数术语与逐元素数组访问")
    elif family_id == "normalization" and direct_aliases and loops >= 1 and arrays >= 1 and flags.get("normalization_terms"):
        score += 5
        reasons.append("观察到归一化统计术语与数组遍历")
    elif family_id == "reduction" and direct_aliases and loops >= 1 and arrays >= 1 and flags.get("reduction_terms"):
        score += 5
        reasons.append("观察到归约术语与循环数组访问")
    elif family_id == "shape_layout" and direct_aliases and arrays >= 1 and flags.get("shape_layout_terms"):
        score += 4
        reasons.append("观察到 shape/layout 术语与规则数据访问")
    elif family_id == "indexing_data" and direct_aliases and arrays >= 1 and flags.get("indexing_terms"):
        score += 4
        reasons.append("观察到索引/切片术语与数组访问")
    elif family_id == "vision_postprocess" and direct_aliases and arrays >= 1 and flags.get("tensor_or_image_terms"):
        score += 3
        reasons.append("观察到图像/张量数据形态")

    return score, reasons, direct_aliases[:8]


def rank_npu_operation_candidates(
    code_unit: dict[str, Any],
    records: list[dict[str, Any]],
    *,
    limit: int = 5,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Rank source-confirmed operation records without turning them into proof.

    Only positive semantic evidence makes a record eligible.  This means a
    proxy/IPC function with no tensor computation returns no NPU operator
    candidates instead of a generic accelerator recommendation.
    """

    observations = static_semantic_observations(code_unit)
    candidates: list[dict[str, Any]] = []
    for record in records:
        if record.get("record_type") != "npu_operation":
            continue
        score, reasons, matched_aliases = _operation_score(record, observations)
        if score <= 0:
            continue
        candidates.append(
            {
                "evidence_id": f"npu_api:{record.get('record_id')}",
                "score": score,
                "match_terms": matched_aliases,
                "retrieval_rationale": {
                    "method": "npu_api_static_semantic_retrieval/v1",
                    "reasons": reasons,
                    "source_observations": {
                        key: value for key, value in observations.items() if key != "_tokens"
                    },
                    "statement_zh": "这是基于可见源码的 Top-N 图算子候选排序，不是 AST 完整语义证明，也不是目标设备支持结论。",
                },
                "record": record,
            }
        )
    candidates.sort(
        key=lambda item: (
            -int(item["score"]),
            str((item["record"].get("semantic_family") or {}).get("id") if isinstance(item["record"], dict) else ""),
            str(item["record"].get("symbol") if isinstance(item["record"], dict) else ""),
        )
    )
    summary = {
        "method": "npu_api_static_semantic_retrieval/v1",
        "candidate_count": len(candidates),
        "returned_count": min(len(candidates), limit),
        "source_observations": {key: value for key, value in observations.items() if key != "_tokens"},
        "statement_zh": "候选仅表示与公开 NNRT 图算子语义接近；后续必须验证完整图、deviceID、GetAvailableOperations、编译和端到端基准。",
    }
    return candidates[:limit], summary
