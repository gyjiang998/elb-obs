"""Hardware-neutral lexical facts for model-assisted semantic routing.

This module deliberately does *not* classify code as NPU/GPU/DSP/media and
does not emit accelerator API names.  It produces a bounded description of
visible source structure that a model can compare with source-backed hardware
capability cards.  The profile is lexical, not an AST proof: dependence,
aliasing and callee bodies stay explicitly unresolved.
"""

from __future__ import annotations

import re
from typing import Any


GENERIC_CODE_PROFILE_VERSION = "generic_static_code_profile/v1"
GENERIC_SCOPE_METHOD = "generic_lexical_scope_selection/v1"
MAX_SCOPES = 4
MAX_BLOCK_LINES = 96

_CONTROL_WORDS = frozenset(
    {
        "alignas",
        "alignof",
        "catch",
        "decltype",
        "do",
        "for",
        "if",
        "return",
        "sizeof",
        "static_assert",
        "switch",
        "while",
    }
)
_MATH_SYMBOLS = frozenset(
    {
        "abs",
        "acos",
        "asin",
        "atan",
        "atan2",
        "ceil",
        "cos",
        "cosh",
        "exp",
        "fabs",
        "floor",
        "fma",
        "log",
        "log10",
        "pow",
        "sin",
        "sinh",
        "sqrt",
        "tan",
        "tanh",
    }
)
_SYNC_SYMBOLS = frozenset(
    {
        "atomic",
        "condition_variable",
        "lock",
        "lock_guard",
        "mutex",
        "shared_lock",
        "shared_mutex",
        "unique_lock",
        "unlock",
        "wait",
    }
)
_IPC_SYMBOLS = frozenset(
    {
        "OnRemoteRequest",
        "SendRequest",
        "SendTransactCmd",
        "Transact",
        "WriteInterfaceToken",
    }
)
_IO_SYMBOLS = frozenset(
    {
        "close",
        "fclose",
        "fopen",
        "fread",
        "fwrite",
        "open",
        "read",
        "recv",
        "send",
        "write",
    }
)
_MEMORY_SYMBOLS = frozenset({"memcpy", "memmove", "memset"})


def _replace_non_code(code: str) -> str:
    """Blank comments and literals while retaining line/column structure."""

    output: list[str] = []
    index = 0
    state = "code"
    while index < len(code):
        current = code[index]
        following = code[index + 1] if index + 1 < len(code) else ""
        if state == "code":
            if current == "/" and following == "/":
                output.extend("  ")
                index += 2
                state = "line_comment"
                continue
            if current == "/" and following == "*":
                output.extend("  ")
                index += 2
                state = "block_comment"
                continue
            if current == '"':
                output.append(" ")
                index += 1
                state = "string"
                continue
            if current == "'":
                output.append(" ")
                index += 1
                state = "char"
                continue
            output.append(current)
            index += 1
            continue
        if state == "line_comment":
            output.append("\n" if current == "\n" else " ")
            index += 1
            if current == "\n":
                state = "code"
            continue
        if state == "block_comment":
            if current == "*" and following == "/":
                output.extend("  ")
                index += 2
                state = "code"
                continue
            output.append("\n" if current == "\n" else " ")
            index += 1
            continue
        if current == "\\" and following:
            output.extend("  ")
            index += 2
            continue
        output.append("\n" if current == "\n" else " ")
        index += 1
        if (state == "string" and current == '"') or (state == "char" and current == "'"):
            state = "code"
    return "".join(output)


def _count(pattern: str, code: str) -> int:
    return len(re.findall(pattern, code, flags=re.MULTILINE))


def _call_symbols(code: str) -> list[str]:
    found: list[str] = []
    for match in re.finditer(r"\b([A-Za-z_][A-Za-z0-9_:]*)\s*\(", code):
        symbol = match.group(1)
        if symbol in _CONTROL_WORDS or symbol in found:
            continue
        found.append(symbol)
    return found


def _present_symbols(code: str, candidates: frozenset[str]) -> list[str]:
    return [symbol for symbol in sorted(candidates) if re.search(rf"\b{re.escape(symbol)}\b", code)]


def _brace_delta(line: str) -> int:
    return line.count("{") - line.count("}")


def _block_end(lines: list[str], start: int) -> int:
    opening = next((index for index in range(start, min(len(lines), start + 3)) if "{" in lines[index]), -1)
    if opening < 0:
        return min(len(lines) - 1, start + 1)
    depth = 0
    for index in range(opening, min(len(lines), opening + MAX_BLOCK_LINES)):
        depth += _brace_delta(lines[index])
        if depth <= 0:
            return index
    return min(len(lines) - 1, opening + MAX_BLOCK_LINES - 1)


def _loop_regions(code: str) -> list[tuple[int, int]]:
    lines = code.splitlines()
    regions: list[tuple[int, int]] = []
    for index, line in enumerate(lines):
        if re.search(r"\b(?:for|while)\s*\(", line):
            regions.append((index, _block_end(lines, index)))
    return regions


def _max_loop_nesting(regions: list[tuple[int, int]]) -> int:
    maximum = 0
    for start, _ in regions:
        maximum = max(maximum, sum(1 for outer_start, outer_end in regions if outer_start <= start <= outer_end))
    return maximum


def _scope_signals(code: str) -> list[str]:
    signals: list[str] = []
    if re.search(r"\b(?:for|while)\s*\(", code):
        signals.append("循环")
    if re.search(r"\[[^\]]+\]", code) or re.search(r"\*\s*[A-Za-z_]", code):
        signals.append("下标或指针访问")
    if re.search(r"(?:\+\+|--|[+\-*/%]=|(?<![<>=!])[+\-*/%](?![=>]))", code):
        signals.append("算术表达式")
    if re.search(r"\b(?:float|double|int\d*_t|uint\d*_t|size_t|ptrdiff_t)\b", code):
        signals.append("数值或索引类型")
    if _call_symbols(code):
        signals.append("函数调用")
    if _present_symbols(code, _SYNC_SYMBOLS | _IPC_SYMBOLS | _IO_SYMBOLS):
        signals.append("同步或外部边界")
    return signals


def generic_candidate_scopes(code: str, limit: int = MAX_SCOPES) -> list[dict[str, Any]]:
    """Choose visible structural regions without relying on domain identifiers."""

    code_only = _replace_non_code(code)
    lines = code_only.splitlines()
    candidates: list[dict[str, Any]] = []
    for start, end in _loop_regions(code_only):
        fragment = "\n".join(lines[start : end + 1])
        signals = _scope_signals(fragment)
        compactness = max(0, 24 - min(24, end - start))
        candidates.append(
            {
                "scope_id": f"loop_region_{start + 1}_region",
                "level": "loop_region",
                "line_start": start + 1,
                "line_end": end + 1,
                "selection_priority": 100 + len(signals) * 6 + compactness,
                "signals": signals,
                "description_zh": f"第 {start + 1}-{end + 1} 行的可见循环片段。可见信号：{'、'.join(signals) if signals else '有限'}。",
                "resolution_status": "observed",
            }
        )
    for index, line in enumerate(lines):
        if re.search(r"\b(?:memcpy|memmove|memset)\s*\(", line):
            start = max(0, index - 1)
            end = min(len(lines) - 1, index + 1)
            signals = _scope_signals("\n".join(lines[start : end + 1]))
            candidates.append(
                {
                    "scope_id": f"data_pipeline_{start + 1}_region",
                    "level": "data_pipeline",
                    "line_start": start + 1,
                    "line_end": end + 1,
                    "selection_priority": 64 + len(signals) * 4,
                    "signals": signals,
                    "description_zh": f"第 {start + 1}-{end + 1} 行的可见数据处理片段。可见信号：{'、'.join(signals) if signals else '有限'}。",
                    "resolution_status": "observed",
                }
            )
    deduplicated: dict[tuple[str, int, int], dict[str, Any]] = {}
    for candidate in candidates:
        key = (str(candidate["level"]), int(candidate["line_start"]), int(candidate["line_end"]))
        if key not in deduplicated or int(candidate["selection_priority"]) > int(deduplicated[key]["selection_priority"]):
            deduplicated[key] = candidate
    selected: list[dict[str, Any]] = []
    for candidate in sorted(deduplicated.values(), key=lambda item: (-int(item["selection_priority"]), int(item["line_start"]))):
        if candidate["level"] == "loop_region" and any(
            current["level"] == "loop_region"
            and int(candidate["line_start"]) <= int(current["line_end"])
            and int(current["line_start"]) <= int(candidate["line_end"])
            for current in selected
        ):
            continue
        selected.append(candidate)
        if len(selected) >= max(0, limit):
            break
    return selected


def generic_static_code_profile(code: str) -> dict[str, Any]:
    """Return hardware-neutral, bounded lexical observations of visible code."""

    code_only = _replace_non_code(code)
    calls = _call_symbols(code_only)
    loop_regions = _loop_regions(code_only)
    numeric_types: list[str] = []
    if re.search(r"\b(?:float|double|long\s+double)\b", code_only):
        numeric_types.append("floating_point")
    if re.search(r"\b(?:bool|char|short|int|long|int\d*_t|uint\d*_t)\b", code_only):
        numeric_types.append("integer_or_boolean")
    if re.search(r"\b(?:size_t|ptrdiff_t|ssize_t)\b", code_only):
        numeric_types.append("size_or_index")
    math_symbols = [symbol for symbol in calls if symbol.split("::")[-1].casefold() in _MATH_SYMBOLS][:8]
    sync_symbols = _present_symbols(code_only, _SYNC_SYMBOLS)
    ipc_symbols = _present_symbols(code_only, _IPC_SYMBOLS)
    io_symbols = _present_symbols(code_only, _IO_SYMBOLS)
    memory_symbols = _present_symbols(code_only, _MEMORY_SYMBOLS)
    boundary_categories: list[str] = []
    if sync_symbols:
        boundary_categories.append("synchronization")
    if ipc_symbols:
        boundary_categories.append("ipc_or_remote_call")
    if io_symbols:
        boundary_categories.append("io")
    if memory_symbols:
        boundary_categories.append("bulk_memory")
    return {
        "schema_version": GENERIC_CODE_PROFILE_VERSION,
        "analysis_method": "lexical_static_scan/v2",
        "scope_method": GENERIC_SCOPE_METHOD,
        "limitations": [
            "这是硬件中立的词法画像，不是 AST、别名分析或循环依赖证明。",
            "未展开的被调函数、数据实际语义、输入规模和运行时数据路径需要额外证据。",
        ],
        "structure": {
            "line_count": len(code.splitlines()),
            "loop_construct_count": len(loop_regions),
            "max_loop_nesting_estimate": _max_loop_nesting(loop_regions),
            "branch_construct_count": _count(r"\b(?:if|switch)\s*\(", code_only),
            "function_call_count": len(calls),
        },
        "data_representation": {
            "numeric_type_classes": numeric_types,
            "subscript_access_count": _count(r"\[[^\]]+\]", code_only),
            "pointer_or_reference_expression_count": _count(r"(?:\*\s*[A-Za-z_]|&\s*[A-Za-z_]|->)", code_only),
            "read_write_access_relation": "not_proven_by_lexical_scan",
        },
        "computation": {
            "arithmetic_expression_count": _count(r"(?:\+\+|--|[+\-*/%]=|(?<![<>=!])[+\-*/%](?![=>]))", code_only),
            "math_function_symbols": math_symbols,
            "bulk_memory_symbols": memory_symbols,
        },
        "boundaries": {
            "observed_categories": boundary_categories,
            "synchronization_symbols": sync_symbols,
            "ipc_or_remote_symbols": ipc_symbols,
            "io_symbols": io_symbols,
        },
        "dependence": {
            "loop_carried_dependency": "not_proven_by_lexical_scan",
            "shared_state_or_aliasing": "not_proven_by_lexical_scan",
        },
    }


def model_safe_generic_code_profile(profile: dict[str, Any]) -> dict[str, Any]:
    """Project generic lexical facts without repeating symbol-name heuristics.

    The complete profile remains in the audit artifact.  The model gets only
    structural counts and broad boundary categories, so a router cannot gain
    hidden leverage from a second copy of a name-based IPC/I/O/synchronization
    matcher.  The original source is still supplied as code for genuine
    semantic interpretation.
    """

    structure = profile.get("structure") if isinstance(profile.get("structure"), dict) else {}
    data = profile.get("data_representation") if isinstance(profile.get("data_representation"), dict) else {}
    computation = profile.get("computation") if isinstance(profile.get("computation"), dict) else {}
    boundaries = profile.get("boundaries") if isinstance(profile.get("boundaries"), dict) else {}
    dependence = profile.get("dependence") if isinstance(profile.get("dependence"), dict) else {}
    return {
        "schema_version": GENERIC_CODE_PROFILE_VERSION,
        "projection": "model_safe_generic_code_profile/v1",
        "analysis_method": str(profile.get("analysis_method") or "lexical_static_scan/v2"),
        "scope_method": str(profile.get("scope_method") or GENERIC_SCOPE_METHOD),
        "limitations": list(profile.get("limitations") or []),
        "structure": {
            key: structure.get(key)
            for key in ("line_count", "loop_construct_count", "max_loop_nesting_estimate", "branch_construct_count", "function_call_count")
        },
        "data_representation": {
            key: data.get(key)
            for key in ("numeric_type_classes", "subscript_access_count", "pointer_or_reference_expression_count", "read_write_access_relation")
        },
        "computation": {
            "arithmetic_expression_count": computation.get("arithmetic_expression_count"),
            "math_function_call_count": len(computation.get("math_function_symbols") or []),
            "bulk_memory_operation_count": len(computation.get("bulk_memory_symbols") or []),
        },
        "boundaries": {
            "observed_categories": list(boundaries.get("observed_categories") or []),
            "observed_category_count": len(boundaries.get("observed_categories") or []),
        },
        "dependence": {
            key: dependence.get(key)
            for key in ("loop_carried_dependency", "shared_state_or_aliasing")
        },
    }
