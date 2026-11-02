#!/usr/bin/env python3
"""
matching_engine.py

把热点函数的精准特征与异构优化知识库做自动化匹配，输出带置信度的异构优化机会。

核心设计：
  - 不靠大模型/向量数据库，只用结构化规则 + 加权相似度。
  - 知识库由代码签名、算法到硬件映射、硬件能力、代价模型、fallback 对共同构建。
  - 匹配维度：领域关键词、运算类型、访存模式、数据类型、循环结构、调用图。
  - 输出：独立的 opportunity JSONL；不要写回知识库文件。

输入：
  - 查询：precise_feature_extractor.py 输出的 JSONL（每个函数一条）
  - 知识库：旧版单文件 JSON/JSONL，或分层知识库目录

输出：异构优化机会 JSONL，每条记录形如：
  {
    "artifact_type": "heterogeneous_opportunity",
    "query_file": "...",
    "query_function": "...",
    "matches": [
      {
        "knowledge_base_pattern_id": "gaussian_blur_2d",
        "source_layers": {...},
        "name": "2D Gaussian blur",
        "target_unit": "GPU_SHADER",
        "confidence": 0.82,
        "matched_features": [...],
        "missing_features": [...],
        "recommendation": "..."
      }
    ]
  }

用法：
    # 1. 生成热点函数特征
    python precise_feature_extractor.py --src hot_func.cpp --out query.jsonl

    # 2. 匹配知识库
    python matching_engine.py \
        --query query.jsonl \
        --kb data/knowledge-bases/patterns \
        --out opportunities.jsonl \
        --top-k 3

    # 3. 只显示高置信度结果
    python matching_engine.py \
        --query query.jsonl \
        --kb heterogeneous_kb/ \
        --threshold 0.6 \
        --out opportunities.jsonl

作者：OpenHarmony 异构优化机会识别工具链。
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from validate_heterogeneous_kb import validate as validate_layered_kb


# ---------------------------------------------------------------------------
# Knowledge base schema
# ---------------------------------------------------------------------------

@dataclass
class HeterogeneousPattern:
    pattern_id: str
    name: str
    cpu_signature: Dict[str, Any]
    heterogeneous: Dict[str, Any]
    weights: Dict[str, float] = field(default_factory=dict)
    threshold: float = 0.0
    required_features: List[str] = field(default_factory=list)
    hard_constraints: Dict[str, Any] = field(default_factory=dict)
    source_layers: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "HeterogeneousPattern":
        return cls(
            pattern_id=d.get("pattern_id", ""),
            name=d.get("name", ""),
            cpu_signature=d.get("cpu_signature", {}),
            heterogeneous=d.get("heterogeneous", {}),
            weights=d.get("weights", {}),
            threshold=float(d.get("threshold", 0.0)),
            required_features=d.get("required_features", []),
            hard_constraints=d.get("hard_constraints", {}),
            source_layers=d.get("source_layers", {}),
        )


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _index_by(items: Iterable[Dict[str, Any]], key: str) -> Dict[str, Dict[str, Any]]:
    return {str(item.get(key, "")): item for item in items if item.get(key)}


def _compose_layered_pattern(
    signature: Dict[str, Any],
    mapping: Dict[str, Any],
    hardware: Dict[str, Any],
    cost_model: Dict[str, Any],
    fallback_pairs: List[Dict[str, Any]],
    implementation_cases: List[Dict[str, Any]],
) -> HeterogeneousPattern:
    """Compose one matching pattern from layered KB records."""
    algorithm = signature.get("algorithm", "")
    target_unit = mapping.get("target_unit") or (mapping.get("preferred_units") or ["UNKNOWN"])[0]
    alt_units = mapping.get("alternative_units", [])
    constraints: Dict[str, Any] = {}
    constraints.update(hardware.get("constraints", {}))
    constraints.update(mapping.get("capability_constraints", {}))

    hard_constraints = dict(signature.get("hard_constraints", {}))
    hard_constraints.update(mapping.get("hard_constraints", {}))

    related_pairs = [
        pair for pair in fallback_pairs
        if pair.get("algorithm") == algorithm or pair.get("target_unit") == target_unit
    ]
    related_implementations = [
        case for case in implementation_cases
        if case.get("algorithm") == algorithm or case.get("target_unit") == target_unit
    ]
    knowledge_provenance = {
        "code_signature": signature.get("provenance", {}),
        "algorithm_hardware_mapping": mapping.get("provenance", {}),
        "hardware_capability": hardware.get("provenance", {}),
        "cost_model": cost_model.get("provenance", {}),
        "fallback_pairs": [
            {"pair_id": pair.get("pair_id", ""), "provenance": pair.get("provenance", {})}
            for pair in related_pairs
        ],
        "existing_implementation_cases": [
            {"case_id": case.get("case_id", ""), "provenance": case.get("provenance", {})}
            for case in related_implementations
        ],
    }
    required_provenance = [
        knowledge_provenance["code_signature"],
        knowledge_provenance["algorithm_hardware_mapping"],
        knowledge_provenance["hardware_capability"],
        knowledge_provenance["cost_model"],
    ]
    is_release_ready = all(item.get("confidence") == "confirmed" for item in required_provenance)

    return HeterogeneousPattern(
        pattern_id=mapping.get("mapping_id") or signature.get("pattern_id", ""),
        name=mapping.get("name") or signature.get("name") or algorithm,
        cpu_signature=signature.get("cpu_signature", signature.get("features", {})),
        heterogeneous={
            "target_unit": target_unit,
            "alternative_units": alt_units,
            "capability_constraints": constraints,
            "hardware": hardware,
            "cost_model": cost_model,
            "fallback_pairs": related_pairs,
            "existing_implementation_cases": related_implementations,
            "knowledge_provenance": knowledge_provenance,
            "knowledge_readiness": "release" if is_release_ready else "development_only",
        },
        weights=mapping.get("weights", signature.get("weights", {})),
        threshold=float(mapping.get("threshold", signature.get("threshold", 0.0))),
        hard_constraints=hard_constraints,
        source_layers={
            "code_signature_pattern_id": signature.get("pattern_id", ""),
            "algorithm": algorithm,
            "mapping_id": mapping.get("mapping_id", ""),
            "hardware_id": hardware.get("hardware_id", target_unit),
            "cost_model_id": cost_model.get("cost_model_id", ""),
            "fallback_pair_ids": [p.get("pair_id") for p in related_pairs if p.get("pair_id")],
            "existing_implementation_case_ids": [
                case.get("case_id") for case in related_implementations if case.get("case_id")
            ],
        },
    )


# ---------------------------------------------------------------------------
# Feature normalization from precise_feature_extractor output
# ---------------------------------------------------------------------------

DOMAIN_STOPWORDS = {
    "the", "and", "or", "not", "if", "else", "for", "while", "do", "return",
    "void", "int", "float", "double", "bool", "char", "unsigned", "signed",
    "const", "static", "inline", "class", "struct", "namespace", "public",
    "private", "protected", "virtual", "override", "final", "template",
    "typename", "using", "typedef", "sizeof", "new", "delete", "this",
    "std", "vector", "array", "map", "set", "string", "unique_ptr", "shared_ptr",
    "ohos", "rosen", "namespace", "anonymous", "operator",
}


def _tokenize(text: str) -> List[str]:
    """Split camelCase/PascalCase/snake_case into lowercase tokens."""
    if not text:
        return []
    # Insert space before capital letters
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", text)
    # Replace underscores/non-alnum with space
    s = re.sub(r"[^A-Za-z0-9]+", " ", s)
    tokens = [t.lower() for t in s.split() if len(t) > 1]
    return [t for t in tokens if t not in DOMAIN_STOPWORDS]


def _extract_keywords(text: str) -> Set[str]:
    tokens = _tokenize(text)
    keywords: Set[str] = set(tokens)
    # Stem common float-precision math function suffixes
    math_stems = {
        "expf": "exp", "exp2f": "exp2", "logf": "log", "log2f": "log2",
        "sqrtf": "sqrt", "cbrtf": "cbrt", "powf": "pow", "fabsf": "abs",
        "sinf": "sin", "cosf": "cos", "tanf": "tan",
        "sinhf": "sinh", "coshf": "cosh", "tanhf": "tanh",
    }
    for tok in tokens:
        if tok in math_stems:
            keywords.add(math_stems[tok])
        # Also drop trailing 'f' from lowercase alphanumeric tokens
        if tok.endswith("f") and len(tok) > 1 and tok[:-1].isalpha():
            keywords.add(tok[:-1])
    return keywords


def _extract_domain_keywords(query: Dict[str, Any]) -> Set[str]:
    """Extract domain keywords from function name, variables, callees, and branch conditions."""
    parts: List[str] = []
    parts.append(query.get("function", ""))
    parts.append(query.get("display_name", ""))
    for p in query.get("parameters", []):
        parts.append(p.get("name", ""))
        parts.append(p.get("type", ""))
    for v in query.get("variables", []):
        parts.append(v.get("name", ""))
        parts.append(v.get("type", ""))
    for c in query.get("calls", []):
        parts.append(c.get("callee", ""))
    for m in query.get("memory_accesses", []):
        parts.append(m.get("base", ""))
    for cond in query.get("branches", {}).get("conditions", []):
        parts.append(cond.get("text", ""))
    for loop in query.get("loops", []):
        if isinstance(loop, dict):
            parts.append(loop.get("condition", ""))
    for kw in query.get("keywords", []):
        parts.append(kw)
    return _extract_keywords(" ".join(parts))


def _extract_operations(query: Dict[str, Any]) -> Set[str]:
    """Derive operation keywords from calls, operators, and control-flow text."""
    ops: Set[str] = set()
    math_ops = {
        "sqrt": "sqrt", "sqrtf": "sqrt", "cbrt": "cbrt",
        "exp": "exp", "expf": "exp", "exp2": "exp", "log": "log", "logf": "log",
        "pow": "pow", "powf": "pow", "fma": "fma",
        "sin": "sin", "cos": "cos", "tan": "tan",
        "sinh": "sinh", "cosh": "cosh", "tanh": "tanh",
        "fabs": "abs", "floor": "floor", "ceil": "ceil", "round": "round",
        "min": "min", "max": "max", "clamp": "clamp",
    }

    # Extract math ops and keywords from callee names
    for c in query.get("calls", []):
        callee = c.get("callee", "")
        base = callee.split("(")[0].split("::")[-1].split(".")[-1]
        if base in math_ops:
            ops.add(math_ops[base])
        else:
            ops.update(_extract_keywords(base))

    for api in query.get("math_apis", []):
        if api in math_ops:
            ops.add(math_ops[api])
        else:
            ops.update(_extract_keywords(api))

    # Operator-based operation inference from explicit AST operators
    op_set = set(query.get("operators", []))
    if "+=" in op_set:
        ops.add("accumulate")
    if "/" in op_set or "/=" in op_set:
        ops.add("divide")
    if "+" in op_set or "+=" in op_set:
        ops.add("add")
    if "*" in op_set or "*=" in op_set:
        ops.add("multiply")
    if any(o in op_set for o in (">", "<", ">=", "<=", "==", "!=")):
        ops.add("compare")
        if any(o in op_set for o in (">", ">=")):
            ops.add("max")
        if any(o in op_set for o in ("<", "<=")):
            ops.add("min")

    # Variable-name hints for accumulator/reduction patterns
    var_hint_ops = {
        "sum": ["sum", "accumulate"],
        "total": ["sum", "accumulate"],
        "acc": ["accumulate"],
        "max_val": ["max"],
        "min_val": ["min"],
        "max_value": ["max"],
        "min_value": ["min"],
    }
    for v in query.get("variables", []):
        name = v.get("name", "")
        if name in var_hint_ops:
            ops.update(var_hint_ops[name])

    # From memory accesses
    for m in query.get("memory_accesses", []):
        if m.get("kind") == "array_subscript":
            ops.add("array_access")
        if m.get("kind") == "pointer_deref":
            ops.add("pointer_deref")

    return ops


def _extract_memory_patterns(query: Dict[str, Any]) -> Set[str]:
    """Derive memory access patterns from explicit accesses and container operator[] calls."""
    patterns: Set[str] = set(query.get("memory_patterns", []))
    for m in query.get("memory_accesses", []):
        kind = m.get("kind")
        stride = m.get("stride_hint", "")
        if kind:
            patterns.add(kind)
        if stride and stride != "unknown":
            patterns.add(f"stride_{stride}")

    # std::vector / array operator[] shows up as a call in libclang AST
    for c in query.get("calls", []):
        callee = c.get("callee", "")
        if "operator[]" in callee:
            patterns.add("container_index")
            patterns.add("array_subscript")

    return patterns


def _extract_data_types(query: Dict[str, Any]) -> Set[str]:
    types: Set[str] = set()
    for t in query.get("types_used", []) + query.get("types", []):
        # normalize
        t = t.replace("const ", "").replace("&", "").replace("*", "").strip()
        if not t:
            continue
        # keep base type name
        base = t.split("<")[0].split("::")[-1]
        if base in ("float", "double", "int", "unsigned int", "uint32_t", "uint16_t",
                    "uint8_t", "int32_t", "int16_t", "int8_t", "size_t", "bool",
                    "half", "float16_t", "bfloat16_t", "char", "short", "long"):
            types.add(base)
    return types


def _max_loop_nesting(query: Dict[str, Any]) -> int:
    loops = query.get("loops", [])
    if isinstance(loops, dict):
        return int(loops.get("max_nesting", 0) or 0)
    if not loops:
        return 0
    depths = [loop.get("depth", 0) for loop in loops if isinstance(loop, dict)]
    return max(depths) + 1 if depths else 0


def _extract_call_set(query: Dict[str, Any]) -> Set[str]:
    calls: Set[str] = set()
    for c in query.get("calls", []):
        callee = c.get("callee", "")
        # strip arguments
        base = callee.split("(")[0]
        calls.add(base)
    return calls


def _infer_high_level_patterns(norm: Dict[str, Any]) -> Dict[str, Any]:
    """Derive higher-level algorithm patterns from low-level features."""
    ops = norm["operations"]
    mem = norm["memory_patterns"]
    loops = norm["loop_count"]
    nesting = norm["loop_nesting"]

    # Convolution / weighted-sum: loop nest + accumulate + multiply + array access
    if nesting >= 2 and "accumulate" in ops and "multiply" in ops and "array_subscript" in mem:
        ops.add("convolution")
        ops.add("weighted_sum")

    # Neighbor access: 2D strided/linear index around a center pixel
    if any(p.startswith("stride_") and p not in ("stride_unit_stride",) for p in mem):
        mem.add("neighbor_access")

    # Multi-pass: multiple loop nests that look like 2D processing
    if loops >= 2 and nesting >= 2:
        ops.add("multi_pass")

    # Matrix multiply: triple nest + accumulate + multiply
    if nesting >= 3 and "accumulate" in ops and "multiply" in ops:
        ops.add("mac")

    return norm


def _normalize_query(query: Dict[str, Any]) -> Dict[str, Any]:
    loops = query.get("loops", [])
    if isinstance(loops, dict):
        loop_count = sum(int(loops.get(k, 0) or 0) for k in ("for", "while", "do_while"))
    else:
        loop_count = len(loops)

    norm = {
        "domain_keywords": _extract_domain_keywords(query),
        "operations": _extract_operations(query),
        "memory_patterns": _extract_memory_patterns(query),
        "data_types": _extract_data_types(query),
        "loop_nesting": _max_loop_nesting(query),
        "branch_count": (
            query.get("branches", {}).get("if", 0)
            + query.get("branches", {}).get("switch", 0)
            + query.get("branches", {}).get("ternary", 0)
        ),
        "calls": _extract_call_set(query),
        "loop_count": loop_count,
    }
    return _infer_high_level_patterns(norm)


# ---------------------------------------------------------------------------
# Similarity functions
# ---------------------------------------------------------------------------

def _jaccard(a: Set[str], b: Set[str]) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / len(a | b)


def _weighted_overlap(a: Set[str], b: Set[str], weight_map: Optional[Dict[str, float]] = None) -> float:
    """
    Pattern-recall oriented overlap: how much of pattern set `b` is covered by query set `a`.
    This is appropriate when `b` is a curated pattern signature and `a` is the observed query.
    """
    if not a or not b:
        return 0.0
    intersection = a & b
    if not intersection:
        return 0.0
    if weight_map:
        score = sum(weight_map.get(tok, 1.0) for tok in intersection)
        denom = sum(weight_map.get(tok, 1.0) for tok in b)
        return score / denom if denom else 0.0
    return len(intersection) / len(b)


def _range_similarity(value: int, min_val: Optional[int], max_val: Optional[int]) -> float:
    """Gaussian-like similarity for numeric ranges."""
    if min_val is not None and max_val is not None and min_val > max_val:
        min_val, max_val = max_val, min_val
    if min_val is not None and value < min_val:
        diff = min_val - value
        return max(0.0, 1.0 - diff * 0.3)
    if max_val is not None and value > max_val:
        diff = value - max_val
        return max(0.0, 1.0 - diff * 0.2)
    return 1.0


def _call_similarity(query_calls: Set[str], pattern_calls: Set[str]) -> float:
    """Compare call sets, with alias tolerance."""
    if not pattern_calls:
        return 0.0
    if not query_calls:
        return 0.0
    # direct match
    direct = query_calls & pattern_calls
    if direct:
        return len(direct) / max(len(query_calls), len(pattern_calls))
    # keyword-level overlap on callee base names
    query_kw: Set[str] = set()
    pattern_kw: Set[str] = set()
    for c in query_calls:
        query_kw.update(_extract_keywords(c))
    for c in pattern_calls:
        pattern_kw.update(_extract_keywords(c))
    return _jaccard(query_kw, pattern_kw)


# ---------------------------------------------------------------------------
# Constraint checking
# ---------------------------------------------------------------------------

def _check_hard_constraints(query_norm: Dict[str, Any], pattern: HeterogeneousPattern) -> Tuple[bool, List[str]]:
    """Return (pass, reasons)."""
    reasons: List[str] = []
    hc = pattern.hard_constraints
    if not hc:
        return True, reasons

    # Required keyword presence
    required_keywords = set(hc.get("required_keywords", []))
    missing = required_keywords - query_norm["domain_keywords"]
    if missing:
        reasons.append(f"missing required keywords: {sorted(missing)}")

    # Loop nesting range
    nesting = query_norm["loop_nesting"]
    min_n = hc.get("loop_nesting_min")
    max_n = hc.get("loop_nesting_max")
    if min_n is not None and nesting < min_n:
        reasons.append(f"loop nesting {nesting} < min {min_n}")
    if max_n is not None and nesting > max_n:
        reasons.append(f"loop nesting {nesting} > max {max_n}")

    # Data type whitelist
    allowed_types = set(hc.get("allowed_data_types", []))
    if allowed_types and not (query_norm["data_types"] & allowed_types):
        reasons.append(f"no allowed data types from {sorted(allowed_types)}")

    # Required operations
    required_ops = set(hc.get("required_operations", []))
    missing_ops = required_ops - query_norm["operations"]
    if missing_ops:
        reasons.append(f"missing required operations: {sorted(missing_ops)}")

    # Required memory patterns
    required_mem = set(hc.get("required_memory_patterns", []))
    missing_mem = required_mem - query_norm["memory_patterns"]
    if missing_mem:
        reasons.append(f"missing required memory patterns: {sorted(missing_mem)}")

    # At least one domain keyword from a curated set.
    any_keywords = set(hc.get("any_keywords", []))
    if any_keywords and not (query_norm["domain_keywords"] & any_keywords):
        reasons.append(f"missing any domain keyword from: {sorted(any_keywords)}")

    # Negative filters for known false positives.
    forbidden_keywords = set(hc.get("forbidden_keywords", []))
    hit_forbidden_keywords = forbidden_keywords & query_norm["domain_keywords"]
    if hit_forbidden_keywords:
        reasons.append(f"hit forbidden keywords: {sorted(hit_forbidden_keywords)}")

    forbidden_ops = set(hc.get("forbidden_operations", []))
    hit_forbidden_ops = forbidden_ops & query_norm["operations"]
    if hit_forbidden_ops:
        reasons.append(f"hit forbidden operations: {sorted(hit_forbidden_ops)}")

    return len(reasons) == 0, reasons


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

def _match_pattern(query_norm: Dict[str, Any], pattern: HeterogeneousPattern) -> Optional[Dict[str, Any]]:
    passed, reasons = _check_hard_constraints(query_norm, pattern)
    if not passed:
        return None

    cpu = pattern.cpu_signature
    weights = pattern.weights or {
        "domain_keywords": 3.0,
        "operations": 2.5,
        "memory_patterns": 2.0,
        "data_types": 1.5,
        "loop_nesting": 1.0,
        "calls": 1.0,
    }

    scores: Dict[str, float] = {}

    # Set-based features
    if "domain_keywords" in weights:
        scores["domain_keywords"] = _weighted_overlap(
            query_norm["domain_keywords"], set(cpu.get("domain_keywords", [])), None
        )
    if "operations" in weights:
        scores["operations"] = _weighted_overlap(
            query_norm["operations"], set(cpu.get("operations", [])), None
        )
    if "memory_patterns" in weights:
        scores["memory_patterns"] = _weighted_overlap(
            query_norm["memory_patterns"], set(cpu.get("memory_patterns", [])), None
        )
    if "data_types" in weights:
        scores["data_types"] = _weighted_overlap(
            query_norm["data_types"], set(cpu.get("data_types", [])), None
        )
    if "calls" in weights:
        scores["calls"] = _call_similarity(
            query_norm["calls"], set(cpu.get("calls", []))
        )

    # Numeric features
    if "loop_nesting" in weights:
        scores["loop_nesting"] = _range_similarity(
            query_norm["loop_nesting"],
            cpu.get("loop_nesting_min"),
            cpu.get("loop_nesting_max"),
        )

    if not scores:
        return None

    total_weight = sum(weights.get(k, 1.0) for k in scores)
    weighted_sum = sum(scores[k] * weights.get(k, 1.0) for k in scores)
    confidence = weighted_sum / total_weight if total_weight > 0 else 0.0

    if confidence < pattern.threshold:
        return None

    # Build matched/missing feature lists for explanation
    matched: List[str] = []
    missing: List[str] = []
    for k, score in scores.items():
        if score >= 0.5:
            matched.append(f"{k}({score:.2f})")
        else:
            missing.append(f"{k}({score:.2f})")

    target_unit = pattern.heterogeneous.get("target_unit", "UNKNOWN")
    alt_units = pattern.heterogeneous.get("alternative_units", [])
    cost_model = pattern.heterogeneous.get("cost_model", {})
    fallback_pairs = pattern.heterogeneous.get("fallback_pairs", [])
    implementation_cases = pattern.heterogeneous.get("existing_implementation_cases", [])
    recommendation = (
        f"Candidate for {target_unit}. "
        f"Strongest match on: {', '.join(matched[:3]) if matched else 'none'}."
    )
    if alt_units:
        recommendation += f" Alternative units: {', '.join(alt_units)}."

    return {
        "pattern_id": pattern.pattern_id,
        "knowledge_base_pattern_id": pattern.pattern_id,
        "source_layers": pattern.source_layers,
        "name": pattern.name,
        "target_unit": target_unit,
        "alternative_units": alt_units,
        "confidence": round(confidence, 4),
        "scores": {k: round(v, 4) for k, v in scores.items()},
        "matched_features": matched,
        "missing_features": missing,
        "capability_constraints": pattern.heterogeneous.get("capability_constraints", {}),
        "cost_model": {
            "cost_model_id": cost_model.get("cost_model_id", ""),
            "positive_factors": cost_model.get("positive_factors", []),
            "negative_factors": cost_model.get("negative_factors", []),
            "decision_rules": cost_model.get("decision_rules", []),
        } if cost_model else {},
        "knowledge_provenance": pattern.heterogeneous.get("knowledge_provenance", {}),
        "knowledge_readiness": pattern.heterogeneous.get("knowledge_readiness", "development_only"),
        "fallback_pairs": [
            {
                "pair_id": pair.get("pair_id", ""),
                "cpu_impl": pair.get("cpu_impl", ""),
                "fallback_impl": pair.get("fallback_impl", pair.get("cpu_impl", "")),
                "fallback_execution_domain": pair.get("fallback_execution_domain", "unknown"),
                "hw_impl": pair.get("hw_impl", ""),
                "dispatcher": pair.get("dispatcher", ""),
                "dispatcher_type": pair.get("dispatcher_type", ""),
                "dispatch_conditions": pair.get("dispatch_conditions", []),
                "fallback_conditions": pair.get("fallback_conditions", []),
                "failure_reasons": pair.get("failure_reasons", []),
                "fallback_guarantee": pair.get("fallback_guarantee", ""),
                "evidence": pair.get("evidence", {}),
            }
            for pair in fallback_pairs
        ],
        "existing_implementation_cases": [
            {
                "case_id": case.get("case_id", ""),
                "name": case.get("name", ""),
                "module": case.get("module", ""),
                "algorithm": case.get("algorithm", ""),
                "target_unit": case.get("target_unit", ""),
                "case_scope": case.get("case_scope", ""),
                "implementation_kind": case.get("implementation_kind", ""),
                "implementation_family": case.get("implementation_family", ""),
                "execution_roles": case.get("execution_roles", []),
                "source_anchors": case.get("source_anchors", []),
                "discovery": case.get("discovery", {}),
            }
            for case in implementation_cases
        ],
        "recommendation": recommendation,
    }


def match_query(query: Dict[str, Any], kb: List[HeterogeneousPattern], top_k: int = 3) -> Dict[str, Any]:
    query_norm = _normalize_query(query)
    matches: List[Dict[str, Any]] = []
    for pattern in kb:
        result = _match_pattern(query_norm, pattern)
        if result:
            matches.append(result)
    matches.sort(key=lambda x: x["confidence"], reverse=True)
    return {
        "artifact_type": "heterogeneous_opportunity",
        "schema_version": "1.0",
        "query_file": query.get("file", ""),
        "query_function": query.get("function", ""),
        "query_signature": query.get("signature", ""),
        "normalized_features": {k: sorted(v) if isinstance(v, set) else v for k, v in query_norm.items()},
        "matches": matches[:top_k],
    }


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------

def _load_jsonl(path: Path) -> Iterable[Dict[str, Any]]:
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            yield json.loads(line)


def _load_json_or_jsonl(path: Path) -> List[Dict[str, Any]]:
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return []
    if text.startswith("["):
        return list(json.loads(text))
    return list(_load_jsonl(path))


def _load_optional_layer(root: Path, filename: str) -> List[Dict[str, Any]]:
    path = root / filename
    if not path.exists():
        return []
    return _load_json_or_jsonl(path)


def _load_layered_kb(root: Path) -> List[HeterogeneousPattern]:
    signatures = _load_optional_layer(root, "code_signature_patterns.json")
    mappings = _load_optional_layer(root, "algorithm_hardware_mapping.json")
    hardware_items = _load_optional_layer(root, "hardware_capabilities.json")
    cost_models = _load_optional_layer(root, "cost_models.json")
    fallback_pairs = _load_optional_layer(root, "fallback_pairs.json")
    implementation_cases = _load_optional_layer(root, "existing_heterogeneous_implementation_cases.json")

    hardware_by_id = _index_by(hardware_items, "hardware_id")
    cost_by_id = _index_by(cost_models, "cost_model_id")
    signatures_by_algorithm: Dict[str, List[Dict[str, Any]]] = {}
    for sig in signatures:
        signatures_by_algorithm.setdefault(sig.get("algorithm", ""), []).append(sig)

    patterns: List[HeterogeneousPattern] = []
    for mapping in mappings:
        algorithm = mapping.get("algorithm", "")
        target_unit = mapping.get("target_unit") or (_as_list(mapping.get("preferred_units")) or [""])[0]
        hardware_id = mapping.get("hardware_id") or target_unit
        cost_model_id = mapping.get("cost_model_id", "")
        hardware = hardware_by_id.get(hardware_id, {})
        cost_model = cost_by_id.get(cost_model_id, {})
        for signature in signatures_by_algorithm.get(algorithm, []):
            patterns.append(
                _compose_layered_pattern(
                    signature,
                    mapping,
                    hardware,
                    cost_model,
                    fallback_pairs,
                    implementation_cases,
                )
            )

    if not patterns:
        print(
            f"WARNING: layered KB at {root} produced no patterns. "
            "Expected code_signature_patterns.json and algorithm_hardware_mapping.json.",
            file=sys.stderr,
        )
    return patterns


def _load_kb(path: Path) -> List[HeterogeneousPattern]:
    if path.is_dir():
        return _load_layered_kb(path)

    patterns: List[HeterogeneousPattern] = []
    for item in _load_json_or_jsonl(path):
        patterns.append(HeterogeneousPattern.from_dict(item))
    return patterns


def release_kb_errors(path: Path, *, source_root: Optional[Path] = None) -> List[str]:
    """Return release-gate errors before a production opportunity run."""
    if not path.is_dir():
        return ["release mode requires a layered KB directory with manifest"]
    errors, warnings, _ = validate_layered_kb(path, strict=True, source_root=source_root)
    # validate_heterogeneous_kb.py exposes warnings separately so development
    # callers can inspect them; production matching treats every warning as a
    # hard release failure, matching the validator CLI's --strict behavior.
    return errors + [f"strict mode: {warning}" for warning in warnings]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Match hotspot function features against heterogeneous optimization knowledge base."
    )
    ap.add_argument("--query", required=True, help="Query JSONL from precise_feature_extractor.py")
    ap.add_argument("--kb", required=True, help="Knowledge base JSON/JSONL file or layered KB directory")
    ap.add_argument("--out", required=True, help="Output JSONL file")
    ap.add_argument("--top-k", type=int, default=3, help="Number of top matches per function")
    ap.add_argument("--threshold", type=float, default=0.0, help="Global confidence threshold")
    ap.add_argument("--require-release-kb", action="store_true", help="Refuse non-release KBs before writing opportunities")
    ap.add_argument("--source-root", help="Workspace root for release KB source-provenance validation")
    ap.add_argument("--pretty", action="store_true", help="Pretty-print JSON output")
    args = ap.parse_args()

    query_path = Path(args.query)
    kb_path = Path(args.kb)
    out_path = Path(args.out)

    if not query_path.exists():
        print(f"ERROR: query file not found: {query_path}", file=sys.stderr)
        return 1
    if not kb_path.exists():
        print(f"ERROR: knowledge base not found: {kb_path}", file=sys.stderr)
        return 1
    if args.require_release_kb:
        release_errors = release_kb_errors(kb_path, source_root=Path(args.source_root) if args.source_root else None)
        if release_errors:
            for error in release_errors:
                print(f"ERROR: release KB gate: {error}", file=sys.stderr)
            return 1

    kb = _load_kb(kb_path)
    if not kb:
        print(f"WARNING: empty knowledge base: {kb_path}", file=sys.stderr)

    out_path.parent.mkdir(parents=True, exist_ok=True)

    all_results: List[Dict[str, Any]] = []
    total_matches = 0

    with out_path.open("w", encoding="utf-8") as fh:
        for query in _load_jsonl(query_path):
            result = match_query(query, kb, top_k=args.top_k)
            if args.threshold > 0:
                result["matches"] = [m for m in result["matches"] if m["confidence"] >= args.threshold]
            total_matches += len(result["matches"])
            all_results.append(result)
            if not args.pretty:
                fh.write(json.dumps(result, ensure_ascii=False) + "\n")

        if args.pretty:
            json.dump(all_results, fh, ensure_ascii=False, indent=2)

    print(f"Matched {len(all_results)} queries -> {total_matches} suggestions -> {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
