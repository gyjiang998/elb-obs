"""HiTrace marker decoding and conservative instrumentation-point lookup.

Raw HiTrace labels are often authored at an instrumentation site instead of
being generated from a C++ function name.  A label such as
``H:JsDisplayListener::OnChange|I48`` therefore carries two useful, distinct
pieces of evidence:

* ``I`` is the trace output level and ``48`` is a serialized HiTrace tag bit;
* the event label can be reverse-matched to the macro that emitted it.

This module keeps those facts separate from a trace-to-source binding.  Its
results are useful for source browsing and triage, but they do not claim that
the selected source snapshot is the one that produced the trace.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .core import sha256_file


SOURCE_SEARCH_GLOBS = ("*.c", "*.cc", "*.cpp", "*.cxx", "*.h", "*.hh", "*.hpp", "*.hxx")
TAG_HEADER_RELATIVE_PATH = Path(
    "base/hiviewdfx/hitrace/interfaces/native/innerkits/include/hitrace_meter/hitrace_meter.h"
)
SOURCE_SCOPE_HINTS_PATH = Path(__file__).resolve().parents[1] / "data" / "hitrace_tag_source_scope_hints.json"
OPTION_TAG_MACROS = {
    "HITRACE_TAG_ALWAYS",
    "HITRACE_TAG_COMMERCIAL",
    "HITRACE_TAG_NEVER",
    "HITRACE_TAG_NOT_READY",
}
TRACE_LEVEL_NAMES = {
    "D": "debug",
    "I": "info",
    "C": "critical",
    "M": "commercial",
}
TAG_DEFINITION_RE = re.compile(
    r"^\s*constexpr\s+uint64_t\s+(HITRACE_TAG_[A-Za-z0-9_]+)\s*=\s*"
    r"\(\s*1(?:ULL|UL|LL|U)?\s*<<\s*(\d+)\s*\)\s*;\s*(?://\s*(.*))?$",
    re.MULTILINE,
)
MARKER_TOKEN_RE = re.compile(r"^([DICM])((?:\d{2})*)$")
# Most sites use an uppercase HiTrace macro, while a small set of OpenHarmony
# subsystems use the StartTrace/FinishTrace API pair directly.  Both remain
# subject to literal-label and tag verification before becoming a candidate.
MACRO_CALL_RE = re.compile(r"\b((?:[A-Z][A-Z0-9_]*(?:HITRACE|TRACE|BYTRACE)[A-Z0-9_]*)|StartTrace|FinishTrace)\s*\(")
STRING_LITERAL_RE = re.compile(r'"((?:\\.|[^"\\])*)"')
FUNCTION_HEADER_RE = re.compile(
    r"(?P<name>(?:[A-Za-z_]\w*::)*~?[A-Za-z_]\w*)\s*\([^;{}]*\)\s*"
    r"(?:const\s*)?(?:noexcept\s*)?\{"
)
CONTROL_FLOW_NAMES = {"if", "for", "while", "switch", "catch"}
MAX_SOURCE_FILE_BYTES = 8 * 1024 * 1024
MAX_LABEL_MATCHES = 160


def _canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _load_source_scope_hints() -> tuple[dict[str, list[str]], dict[str, Any]]:
    """Read the small, versioned tag-to-source-scope knowledge record."""

    try:
        payload = json.loads(SOURCE_SCOPE_HINTS_PATH.read_text(encoding="utf-8"))
        records = payload.get("records") if isinstance(payload, dict) else None
        if not isinstance(records, list):
            raise ValueError("records is not a list")
        hints: dict[str, list[str]] = {}
        for record in records:
            if not isinstance(record, dict):
                continue
            macro = record.get("tag_macro")
            roots = record.get("relative_roots")
            if not isinstance(macro, str) or not isinstance(roots, list):
                continue
            clean_roots = [root for root in roots if isinstance(root, str) and root and not Path(root).is_absolute()]
            if clean_roots:
                hints[macro] = clean_roots
        return hints, {
            "status": "available",
            "knowledge_id": payload.get("knowledge_id"),
            "schema_version": payload.get("schema_version"),
            "sha256": sha256_file(SOURCE_SCOPE_HINTS_PATH),
        }
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return {}, {"status": "unavailable", "reason": f"Cannot load tag source-scope hints: {exc}"}


def _source_search_roots(connector_root: Path, expected_tag_macros: set[str]) -> tuple[list[Path], dict[str, Any]]:
    """Prefer curated module subtrees and only fall back for unknown connector layouts."""

    hints, provenance = _load_source_scope_hints()
    resolved_root = connector_root.resolve()
    search_roots: list[Path] = []
    applied_macros: list[str] = []
    requested_relative_roots: list[str] = []
    missing_relative_roots: list[str] = []
    for macro in sorted(expected_tag_macros):
        relative_roots = hints.get(macro, [])
        found_for_macro = False
        for relative_root in relative_roots:
            candidate_roots = [relative_root]
            # A connector may point at a workspace wrapper whose checked-out
            # OpenHarmony tree lives in ``openharmony/``.  This remains inside
            # the selected connector and preserves the same logical scope.
            if (resolved_root / "openharmony").is_dir():
                candidate_roots.append(str(Path("openharmony") / relative_root))
            found_for_relative_root = False
            for candidate_relative_root in candidate_roots:
                if candidate_relative_root not in requested_relative_roots:
                    requested_relative_roots.append(candidate_relative_root)
                candidate = (resolved_root / candidate_relative_root).resolve()
                try:
                    candidate.relative_to(resolved_root)
                except ValueError:
                    continue
                if candidate.is_dir() and candidate not in search_roots:
                    search_roots.append(candidate)
                    found_for_macro = True
                    found_for_relative_root = True
            if not found_for_relative_root and relative_root not in missing_relative_roots:
                missing_relative_roots.append(relative_root)
        if found_for_macro:
            applied_macros.append(macro)
    if not search_roots:
        return [resolved_root], {
            "strategy": "connector_root_fallback",
            "reason": "No curated tag source-scope path exists in this connector; bounded lookup uses the connector root.",
            "tag_macros": sorted(expected_tag_macros),
            "applied_tag_macros": [],
            "requested_relative_roots": requested_relative_roots,
            "missing_relative_roots": missing_relative_roots,
            "roots": ["."],
            "knowledge": provenance,
        }
    return search_roots, {
        "strategy": "curated_tag_source_scopes",
        "tag_macros": sorted(expected_tag_macros),
        "applied_tag_macros": applied_macros,
        "requested_relative_roots": requested_relative_roots,
        "missing_relative_roots": missing_relative_roots,
        "roots": [_relative_path(path, resolved_root) for path in search_roots],
        "knowledge": provenance,
    }


def _relative_path(path: Path, root: Path) -> str:
    try:
        return str(path.resolve().relative_to(root.resolve()))
    except ValueError:
        return path.name


def _parse_marker(event_name: str) -> dict[str, Any] | None:
    """Decode a textual HiTrace marker without assuming the label is a symbol."""

    raw = event_name.strip()
    if not raw.startswith("H:"):
        return None
    fields = raw[2:].split("|")
    if not fields or not fields[0].strip():
        return None
    for index, field in enumerate(fields[1:], start=1):
        matched = MARKER_TOKEN_RE.fullmatch(field.strip())
        if matched is None:
            continue
        bit_text = matched.group(2)
        return {
            "instrumentation_name": fields[0].strip(),
            "output_level": {"code": matched.group(1), "name": TRACE_LEVEL_NAMES[matched.group(1)]},
            "serialized_tag_bits": bit_text,
            "tag_bits": [int(bit_text[offset : offset + 2]) for offset in range(0, len(bit_text), 2)],
            "marker_field_index": index,
            "custom_args_present": index + 1 < len(fields),
        }
    return None


def _find_tag_header(source_root: Path, workspace_root: Path) -> tuple[Path | None, str | None]:
    source_root = source_root.resolve()
    workspace_root = workspace_root.resolve()
    candidates = [
        (source_root / TAG_HEADER_RELATIVE_PATH, "selected_connector"),
        (source_root / "openharmony" / TAG_HEADER_RELATIVE_PATH, "selected_connector"),
    ]
    seen: set[Path] = set()
    for candidate, origin in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.is_file():
            return resolved, origin
    return None, None


def load_hitrace_tag_catalog(source_root: Path, workspace_root: Path) -> dict[str, Any]:
    """Build a tag catalog from a versioned source header, never a hard-coded map."""

    header, origin = _find_tag_header(source_root, workspace_root)
    if header is None:
        return {
            "status": "unavailable",
            "tag_by_bit": {},
            "provenance": {
                "status": "unavailable",
                "reason": "hitrace_meter.h is unavailable from the selected connector and workspace reference path.",
            },
        }
    try:
        text = header.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        return {
            "status": "unavailable",
            "tag_by_bit": {},
            "provenance": {"status": "unavailable", "reason": f"Cannot read hitrace_meter.h: {exc}"},
        }

    tag_by_bit: dict[int, dict[str, Any]] = {}
    for matched in TAG_DEFINITION_RE.finditer(text):
        macro = matched.group(1)
        bit = int(matched.group(2))
        if not 0 <= bit < 64:
            continue
        tag_by_bit[bit] = {
            "bit": bit,
            "macro": macro,
            "display_name": macro.removeprefix("HITRACE_TAG_"),
            "category": "option" if macro in OPTION_TAG_MACROS else "module",
            "comment": (matched.group(3) or "").strip() or None,
        }
    if not tag_by_bit:
        return {
            "status": "unavailable",
            "tag_by_bit": {},
            "provenance": {
                "status": "unavailable",
                "header_path": _relative_path(header, workspace_root),
                "header_sha256": sha256_file(header),
                "origin": origin,
                "reason": "No direct HITRACE_TAG bit definitions were found in hitrace_meter.h.",
            },
        }
    compact_mapping = [tag_by_bit[bit] for bit in sorted(tag_by_bit)]
    return {
        "status": "available",
        "tag_by_bit": tag_by_bit,
        "provenance": {
            "status": "available",
            "header_path": _relative_path(header, workspace_root),
            "header_sha256": sha256_file(header),
            "mapping_sha256": _canonical_hash(compact_mapping),
            "tag_count": len(tag_by_bit),
            "origin": origin,
        },
    }


def describe_hitrace_event(event_name: str, tag_catalog: dict[str, Any]) -> dict[str, Any] | None:
    """Attach decoded tag meaning to one trace event, if it uses HiTrace syntax."""

    marker = _parse_marker(event_name)
    if marker is None:
        return None
    tag_by_bit = tag_catalog.get("tag_by_bit") if isinstance(tag_catalog.get("tag_by_bit"), dict) else {}
    tags: list[dict[str, Any]] = []
    for bit in marker["tag_bits"]:
        known = tag_by_bit.get(bit)
        if isinstance(known, dict):
            tags.append(dict(known))
        else:
            tags.append({"bit": bit, "macro": None, "display_name": f"bit_{bit:02d}", "category": "unknown", "comment": None})
    module_tags = [tag for tag in tags if tag["category"] == "module"]
    option_tags = [tag for tag in tags if tag["category"] == "option"]
    return {
        "format": "hitrace_marker_v1",
        **marker,
        "tags": tags,
        "module_tags": module_tags,
        "option_tags": option_tags,
        "catalog": dict(tag_catalog.get("provenance") or {}),
    }


def _event_search_terms(label: str) -> list[str]:
    """Return a bounded set of literal anchors for static and formatted labels."""

    terms = [label]
    for separator in ("[", "(", "{"):
        prefix = label.split(separator, 1)[0].strip()
        if prefix:
            terms.append(prefix)
    dynamic_value = re.search(r"(?:0x[0-9a-fA-F]+|(?<![A-Za-z_])[-+]?\d+(?:\.\d+)?)", label)
    if dynamic_value is not None:
        prefix = label[: dynamic_value.start()].rstrip(" :,[({")
        if prefix:
            terms.append(prefix)
    # HiTrace labels often append dynamic work metadata, for example
    # ``Native async work complete callback, name:fs.access#123``.  The
    # source keeps only the stable prefix before the field, so retain that
    # anchor for a bounded literal instrumentation search.
    for field_name in ("name", "traceid", "taskid", "handleid", "id"):
        marker = f"{field_name}:"
        if marker not in label:
            continue
        prefix = label.split(marker, 1)[0].rstrip()
        if prefix:
            terms.append(prefix)
    return list(dict.fromkeys(term for term in terms if 8 <= len(term) <= 240))


def _rg_fixed_matches(root: Path, term: str, *, timeout_seconds: int) -> tuple[list[dict[str, Any]], list[str]]:
    if shutil.which("rg") is None:
        return [], ["ripgrep is unavailable for instrumentation lookup."]
    command = ["rg", "--json", "--no-messages", "--fixed-strings", "--max-count", "3"]
    for glob in SOURCE_SEARCH_GLOBS:
        command.extend(["--glob", glob])
    command.extend([term, "."])
    try:
        completed = subprocess.run(command, cwd=root, text=True, capture_output=True, timeout=timeout_seconds, check=False)
    except subprocess.TimeoutExpired:
        return [], [f"Instrumentation label lookup timed out for {term!r}."]
    if completed.returncode not in {0, 1}:
        return [], [f"Instrumentation label lookup failed for {term!r} with exit code {completed.returncode}."]
    matches: list[dict[str, Any]] = []
    for raw in completed.stdout.splitlines():
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if record.get("type") != "match" or not isinstance(record.get("data"), dict):
            continue
        data = record["data"]
        path_data = data.get("path") if isinstance(data.get("path"), dict) else {}
        lines_data = data.get("lines") if isinstance(data.get("lines"), dict) else {}
        path_text = path_data.get("text")
        line_text = lines_data.get("text")
        line_number = data.get("line_number")
        if not isinstance(path_text, str) or not isinstance(line_text, str):
            continue
        try:
            source = (root / path_text).resolve()
            relative_path = str(source.relative_to(root))
            line = int(line_number)
        except (TypeError, ValueError):
            continue
        matches.append({"path": relative_path, "line": line, "line_text": line_text.rstrip(), "search_term": term})
        if len(matches) >= MAX_LABEL_MATCHES:
            break
    return matches, []


def _rg_symbol_matches(root: Path, symbol: str, *, timeout_seconds: int) -> tuple[list[dict[str, Any]], list[str]]:
    """Find C/C++ function-like spellings for `__func__`/`__PRETTY_FUNCTION__` sites."""

    if shutil.which("rg") is None:
        return [], ["ripgrep is unavailable for function-derived instrumentation lookup."]
    command = ["rg", "--json", "--no-messages", "--max-count", "4"]
    for glob in SOURCE_SEARCH_GLOBS:
        command.extend(["--glob", glob])
    command.extend(["-e", rf"\b{re.escape(symbol)}\s*\(", "."])
    try:
        completed = subprocess.run(command, cwd=root, text=True, capture_output=True, timeout=timeout_seconds, check=False)
    except subprocess.TimeoutExpired:
        return [], [f"Function-derived instrumentation lookup timed out for {symbol!r}."]
    if completed.returncode not in {0, 1}:
        return [], [f"Function-derived instrumentation lookup failed for {symbol!r} with exit code {completed.returncode}."]
    matches: list[dict[str, Any]] = []
    for raw in completed.stdout.splitlines():
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if record.get("type") != "match" or not isinstance(record.get("data"), dict):
            continue
        data = record["data"]
        path_data = data.get("path") if isinstance(data.get("path"), dict) else {}
        lines_data = data.get("lines") if isinstance(data.get("lines"), dict) else {}
        path_text = path_data.get("text")
        line_text = lines_data.get("text")
        if not isinstance(path_text, str) or not isinstance(line_text, str):
            continue
        try:
            line = int(data.get("line_number"))
            source = (root / path_text).resolve()
            relative_path = str(source.relative_to(root))
        except (TypeError, ValueError):
            continue
        matches.append({"path": relative_path, "line": line, "line_text": line_text.rstrip(), "search_term": symbol})
        if len(matches) >= MAX_LABEL_MATCHES:
            break
    return matches, []


def _read_source_lines(root: Path, relative_path: str) -> list[str] | None:
    source = (root / relative_path).resolve()
    try:
        source.relative_to(root)
    except ValueError:
        return None
    try:
        if not source.is_file() or source.stat().st_size > MAX_SOURCE_FILE_BYTES:
            return None
        return source.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None


def _rebase_candidate(candidate: dict[str, Any], search_root: Path, connector_root: Path) -> dict[str, Any]:
    """Convert a scope-relative candidate back to the selected connector root."""

    rebased = dict(candidate)
    relative_prefix = search_root.resolve().relative_to(connector_root.resolve())
    candidate_path = Path(str(candidate.get("path") or ""))
    rebased["path"] = str(relative_prefix / candidate_path) if str(relative_prefix) != "." else str(candidate_path)
    instrumentation = candidate.get("instrumentation")
    if not isinstance(instrumentation, dict):
        return rebased
    rebased_instrumentation = dict(instrumentation)
    wrapper_evidence = instrumentation.get("wrapper_tag_evidence")
    if isinstance(wrapper_evidence, list):
        rebased_wrapper_evidence: list[dict[str, Any]] = []
        for item in wrapper_evidence:
            if not isinstance(item, dict):
                continue
            rebased_item = dict(item)
            item_path = item.get("path")
            if isinstance(item_path, str):
                rebased_item["path"] = str(relative_prefix / item_path) if str(relative_prefix) != "." else item_path
            rebased_wrapper_evidence.append(rebased_item)
        rebased_instrumentation["wrapper_tag_evidence"] = rebased_wrapper_evidence
    rebased["instrumentation"] = rebased_instrumentation
    return rebased


def _unescape_cpp_string(value: str) -> str:
    return value.replace(r"\"", '"').replace(r"\\", "\\")


def _normalize_trace_text(value: str) -> str:
    """Normalize common formatted values without treating the result as source proof."""

    value = value.replace(r"\"", '"')
    value = re.sub(r"%\{[^}]+\}(?:[A-Za-z]|ll|llu|zu)", "<value>", value)
    value = re.sub(r"%(?:[-+ #0]*\d*(?:\.\d+)?(?:hh|h|ll|l|z|t|j|L)?)?[diuoxXfFeEgGaAcsp]", "<value>", value)
    value = re.sub(r"0x[0-9a-fA-F]+", "<value>", value)
    value = re.sub(r"(?<![A-Za-z_])[-+]?\d+(?:\.\d+)?", "<value>", value)
    return re.sub(r"\s+", " ", value).strip()


def _label_match_kind(label: str, literals: list[str]) -> tuple[str | None, str | None]:
    decoded = [_unescape_cpp_string(value) for value in literals]
    for literal in decoded:
        if literal == label:
            return "exact_literal", literal
    normalized_label = _normalize_trace_text(label)
    for literal in decoded:
        if literal and _normalize_trace_text(literal) == normalized_label:
            return "normalized_format", literal
    for literal in decoded:
        static_prefix = literal.split("%", 1)[0]
        if len(static_prefix) >= 8 and label.startswith(static_prefix):
            return "static_prefix", literal
    return None, None


def _matching_brace_line(lines: list[str], opening_line: int, opening_column: int) -> int:
    """Find a C/C++ body end with enough lexical handling for bounded triage."""

    depth = 0
    quote: str | None = None
    escaped = False
    block_comment = False
    for line_index in range(opening_line, min(len(lines), opening_line + 4096)):
        line = lines[line_index]
        column = opening_column if line_index == opening_line else 0
        while column < len(line):
            char = line[column]
            next_char = line[column + 1] if column + 1 < len(line) else ""
            if block_comment:
                if char == "*" and next_char == "/":
                    block_comment = False
                    column += 2
                    continue
                column += 1
                continue
            if quote is not None:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == quote:
                    quote = None
                column += 1
                continue
            if char == "/" and next_char == "/":
                break
            if char == "/" and next_char == "*":
                block_comment = True
                column += 2
                continue
            if char in {'"', "'"}:
                quote = char
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return line_index
            column += 1
    return min(len(lines) - 1, opening_line + 4095)


def _function_scope_from_opening_brace(
    lines: list[str],
    brace_line: int,
    *,
    lower_bound: int = 0,
) -> dict[str, Any] | None:
    """Build a bounded textual function scope for one candidate opening brace.

    This is deliberately not a C++ parser.  It is only used to explain which
    function owns a verified HiTrace macro.  The closing-brace check below is
    essential: a preceding function definition is not evidence that it owns a
    later macro after its body has already ended.
    """

    opening_column = lines[brace_line].find("{")
    if opening_column < 0:
        return None
    header_start = max(lower_bound, brace_line - 24)
    header = " ".join(part.strip() for part in lines[header_start : brace_line + 1])
    # The lookback may contain completed control-flow blocks.  Select the
    # declaration that ends at this exact opening brace rather than the first
    # ``if (...) {`` in the lookback window.
    function_match = next(
        (match for match in reversed(list(FUNCTION_HEADER_RE.finditer(header))) if match.end() == len(header)),
        None,
    )
    if function_match is None:
        return None
    name = function_match.group("name")
    if name in CONTROL_FLOW_NAMES or (function_match.start() > 0 and header[function_match.start() - 1] in ".>"):
        return None

    short_name = name.rsplit("::", 1)[-1].lstrip("~")
    declaration_line = brace_line + 1
    for index in range(brace_line, header_start - 1, -1):
        if name in lines[index] or short_name in lines[index]:
            declaration_line = index + 1
            break
    return {
        "name": name,
        # Keep ``line`` for existing consumers, but make it the declaration
        # line rather than the opening-brace line so the UI can highlight it.
        "line": declaration_line,
        "declaration_line": declaration_line,
        "body_start_line": brace_line + 1,
        "body_end_line": _matching_brace_line(lines, brace_line, opening_column) + 1,
        "kind": "textual_containing_function",
    }


def _find_containing_function(lines: list[str], macro_line: int) -> dict[str, Any] | None:
    """Return the textual function scope that actually contains ``macro_line``."""

    lower_bound = max(0, macro_line - 1 - 360)
    for brace_line in range(macro_line - 1, lower_bound - 1, -1):
        if "{" not in lines[brace_line]:
            continue
        scope = _function_scope_from_opening_brace(lines, brace_line, lower_bound=lower_bound)
        if scope is None:
            continue
        if int(scope["body_start_line"]) <= macro_line <= int(scope["body_end_line"]):
            return scope
    return None


def _function_scope_from_definition(lines: list[str], matched_line: int) -> dict[str, Any] | None:
    """Recognize a nearby C++ definition before inspecting its instrumentation macros."""

    match_index = matched_line - 1
    lower_bound = max(0, match_index - 8)
    upper_bound = min(len(lines), match_index + 17)
    for brace_line in range(lower_bound, upper_bound):
        if "{" not in lines[brace_line]:
            continue
        scope = _function_scope_from_opening_brace(lines, brace_line, lower_bound=lower_bound)
        if scope is not None:
            return scope
    return None


def _macro_invocation_text(lines: list[str], line_index: int, opening_parenthesis: int) -> str:
    """Return one bounded macro invocation, keeping adjacent macros independent."""

    source = "\n".join(lines[line_index : min(len(lines), line_index + 32)])
    depth = 0
    quote: str | None = None
    escaped = False
    for index in range(opening_parenthesis, len(source)):
        char = source[index]
        if quote is not None:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == quote:
                quote = None
            continue
        if char in {'"', "'"}:
            quote = char
            continue
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return source[: index + 1]
    return source


def _wrapper_tag_evidence(
    root: Path,
    macro_name: str,
    expected_tag_macros: set[str],
    *,
    timeout_seconds: int,
    cache: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Resolve simple wrapper macros such as ``RS_TRACE_NAME`` within this connector."""

    if macro_name in cache:
        return cache[macro_name]
    if macro_name.startswith("HITRACE_") or shutil.which("rg") is None:
        cache[macro_name] = []
        return []
    command = ["rg", "--json", "--no-messages", "--max-count", "4"]
    for glob in ("*.h", "*.hh", "*.hpp", "*.hxx"):
        command.extend(["--glob", glob])
    command.extend(["-e", rf"^\s*#\s*define\s+{re.escape(macro_name)}\b", "."])
    try:
        completed = subprocess.run(command, cwd=root, text=True, capture_output=True, timeout=timeout_seconds, check=False)
    except subprocess.TimeoutExpired:
        cache[macro_name] = []
        return []
    if completed.returncode not in {0, 1}:
        cache[macro_name] = []
        return []
    evidence: list[dict[str, Any]] = []
    for raw in completed.stdout.splitlines():
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if record.get("type") != "match" or not isinstance(record.get("data"), dict):
            continue
        data = record["data"]
        path_data = data.get("path") if isinstance(data.get("path"), dict) else {}
        path_text = path_data.get("text")
        try:
            line = int(data.get("line_number"))
        except (TypeError, ValueError):
            continue
        if not isinstance(path_text, str):
            continue
        lines = _read_source_lines(root, path_text)
        if lines is None:
            continue
        macro_body = "\n".join(lines[line - 1 : min(len(lines), line + 7)])
        observed = set(re.findall(r"\bHITRACE_TAG_[A-Z0-9_]+\b", macro_body))
        if expected_tag_macros and not expected_tag_macros.issubset(observed):
            continue
        evidence.append({"path": path_text, "line": line, "tag_macros": sorted(observed)})
    cache[macro_name] = evidence
    return evidence


def _tag_match_evidence(
    root: Path,
    macro_name: str,
    macro_text: str,
    expected_tag_macros: set[str],
    *,
    timeout_seconds: int,
    wrapper_cache: dict[str, list[dict[str, Any]]],
) -> tuple[str, set[str], list[dict[str, Any]]]:
    observed_tag_macros = set(re.findall(r"\bHITRACE_TAG_[A-Z0-9_]+\b", macro_text))
    if expected_tag_macros and expected_tag_macros.issubset(observed_tag_macros):
        return "direct", observed_tag_macros, []
    wrapper_evidence = _wrapper_tag_evidence(
        root,
        macro_name,
        expected_tag_macros,
        timeout_seconds=timeout_seconds,
        cache=wrapper_cache,
    )
    if wrapper_evidence:
        return "wrapper_definition", observed_tag_macros, wrapper_evidence
    return ("not_verified" if expected_tag_macros else "catalog_unavailable"), observed_tag_macros, []


def _function_label_terms(label: str) -> list[str]:
    qualified = re.findall(r"((?:[A-Za-z_]\w*::)+~?[A-Za-z_]\w*)\s*\(", label)
    terms: list[str] = []
    for value in qualified:
        parts = value.split("::")
        terms.append(value)
        if len(parts) >= 2:
            terms.append("::".join(parts[-2:]))
        terms.append(parts[-1])
    return list(dict.fromkeys(term for term in terms if 4 <= len(term) <= 200))


def _label_matches_function(label: str, function_name: str) -> bool:
    if function_name in label:
        return True
    bare_name = function_name.split("::")[-1]
    compact_label = re.sub(r"\s+", " ", label).strip()
    return compact_label == bare_name or compact_label.endswith(f"::{bare_name}")


def _function_label_derivation(macro_name: str, macro_text: str) -> str | None:
    if "__PRETTY_FUNCTION__" in macro_text:
        return "__PRETTY_FUNCTION__"
    if "__func__" in macro_text or "__FUNCTION__" in macro_text:
        return "__func__"
    if macro_name in {"HITRACE_METER", "HITRACE_METER_EX"}:
        return "implicit___func__"
    return None


def _instrumentation_candidate(
    root: Path,
    marker: dict[str, Any],
    match: dict[str, Any],
    expected_tag_macros: set[str],
    *,
    timeout_seconds: int,
    wrapper_cache: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    lines = _read_source_lines(root, str(match["path"]))
    if lines is None:
        return []
    matched_line_index = int(match["line"]) - 1
    start = max(0, matched_line_index - 8)
    end = min(len(lines), matched_line_index + 10)
    candidates: list[dict[str, Any]] = []
    for macro_line_index in range(start, end):
        macro_matches = list(MACRO_CALL_RE.finditer(lines[macro_line_index]))
        for macro_match in macro_matches:
            macro_name = macro_match.group(1)
            macro_text = _macro_invocation_text(lines, macro_line_index, macro_match.end() - 1)
            literals = STRING_LITERAL_RE.findall(macro_text)
            label_match, source_label = _label_match_kind(str(marker["instrumentation_name"]), literals)
            if label_match is None:
                continue
            tag_match, observed_tag_macros, wrapper_evidence = _tag_match_evidence(
                root,
                macro_name,
                macro_text,
                expected_tag_macros,
                timeout_seconds=timeout_seconds,
                wrapper_cache=wrapper_cache,
            )
            candidates.append(
                {
                    "path": str(match["path"]),
                    "line": macro_line_index + 1,
                    "line_text": lines[macro_line_index].rstrip(),
                    "search_term": match["search_term"],
                    "match_kind": "instrumentation_point",
                    "enclosing_function": _find_containing_function(lines, macro_line_index + 1),
                    "instrumentation": {
                        "macro": macro_name,
                        "source_label": source_label,
                        "event_label_match": label_match,
                        "tag_match": tag_match,
                        "expected_tag_macros": sorted(expected_tag_macros),
                        "observed_tag_macros": sorted(observed_tag_macros),
                        "wrapper_tag_evidence": wrapper_evidence,
                    },
                }
            )
    return candidates


def _function_derived_instrumentation_candidates(
    root: Path,
    marker: dict[str, Any],
    expected_tag_macros: set[str],
    *,
    timeout_seconds: int,
    wrapper_cache: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Locate macros that derive their label from ``__func__`` or ``__PRETTY_FUNCTION__``."""

    label = str(marker["instrumentation_name"])
    diagnostics: list[str] = []
    candidates: list[dict[str, Any]] = []
    for term in _function_label_terms(label)[:2]:
        matches, match_diagnostics = _rg_symbol_matches(root, term, timeout_seconds=min(timeout_seconds, 6))
        diagnostics.extend(match_diagnostics)
        for match in matches:
            lines = _read_source_lines(root, str(match["path"]))
            if lines is None:
                continue
            scope = _function_scope_from_definition(lines, int(match["line"]))
            if scope is None or not _label_matches_function(label, str(scope["name"])):
                continue
            body_start = int(scope["body_start_line"]) - 1
            body_end = min(int(scope["body_end_line"]), body_start + 1024)
            for macro_line_index in range(body_start, body_end):
                for macro_match in MACRO_CALL_RE.finditer(lines[macro_line_index]):
                    macro_name = macro_match.group(1)
                    macro_text = _macro_invocation_text(lines, macro_line_index, macro_match.end() - 1)
                    derivation = _function_label_derivation(macro_name, macro_text)
                    if derivation is None:
                        continue
                    enclosing_function = _find_containing_function(lines, macro_line_index + 1)
                    if enclosing_function is not None and enclosing_function.get("name") != scope["name"]:
                        continue
                    tag_match, observed_tag_macros, wrapper_evidence = _tag_match_evidence(
                        root,
                        macro_name,
                        macro_text,
                        expected_tag_macros,
                        timeout_seconds=timeout_seconds,
                        wrapper_cache=wrapper_cache,
                    )
                    candidates.append(
                        {
                            "path": str(match["path"]),
                            "line": macro_line_index + 1,
                            "line_text": lines[macro_line_index].rstrip(),
                            "search_term": match["search_term"],
                            "match_kind": "instrumentation_point",
                            "enclosing_function": enclosing_function or scope,
                            "instrumentation": {
                                "macro": macro_name,
                                "source_label": str(scope["name"]),
                                "event_label_match": "function_derived",
                                "label_derivation": derivation,
                                "tag_match": tag_match,
                                "expected_tag_macros": sorted(expected_tag_macros),
                                "observed_tag_macros": sorted(observed_tag_macros),
                                "wrapper_tag_evidence": wrapper_evidence,
                            },
                        }
                    )
        if candidates:
            break
    return candidates, diagnostics


def _candidate_sort_key(candidate: dict[str, Any]) -> tuple[int, int, str, int]:
    instrumentation = candidate.get("instrumentation") if isinstance(candidate.get("instrumentation"), dict) else {}
    label_rank = {"exact_literal": 0, "normalized_format": 1, "function_derived": 2, "static_prefix": 3}.get(
        instrumentation.get("event_label_match"), 9
    )
    tag_rank = {"direct": 0, "wrapper_definition": 1, "not_verified": 2, "catalog_unavailable": 3}.get(
        instrumentation.get("tag_match"), 9
    )
    return (label_rank, tag_rank, str(candidate.get("path") or ""), int(candidate.get("line") or 0))


def locate_hitrace_instrumentation(
    source_root: Path,
    workspace_root: Path,
    event_name: str,
    *,
    tag_catalog: dict[str, Any] | None = None,
    max_candidates: int = 12,
    timeout_seconds: int = 12,
) -> dict[str, Any]:
    """Find source insertion points for a decoded HiTrace marker.

    The search stays inside ``source_root``.  A workspace-level header may only
    explain tag bits when a partial connector does not contain HiTrace itself;
    its provenance is carried in the returned marker metadata.
    """

    root = source_root.resolve()
    catalog = tag_catalog or load_hitrace_tag_catalog(root, workspace_root)
    marker = describe_hitrace_event(event_name, catalog)
    if marker is None:
        return {
            "status": "not_applicable",
            "reason": "Trace event does not use the textual HiTrace H:<name>|<level><tag-bits> encoding.",
            "marker": None,
            "candidates": [],
            "diagnostics": [],
        }
    if not root.is_dir():
        return {
            "status": "unavailable",
            "reason": "Selected source connector root is unavailable.",
            "marker": marker,
            "candidates": [],
            "diagnostics": [],
        }
    expected_tag_macros = {
        str(tag["macro"])
        for tag in marker.get("tags", [])
        if isinstance(tag, dict) and isinstance(tag.get("macro"), str)
    }
    search_roots, source_scope = _source_search_roots(root, expected_tag_macros)
    diagnostics: list[str] = []
    raw_candidates: dict[tuple[str, int], dict[str, Any]] = {}
    function_terms = _function_label_terms(str(marker["instrumentation_name"]))
    if function_terms:
        for search_root in search_roots:
            wrapper_cache: dict[str, list[dict[str, Any]]] = {}
            derived_candidates, derived_diagnostics = _function_derived_instrumentation_candidates(
                search_root,
                marker,
                expected_tag_macros,
                timeout_seconds=timeout_seconds,
                wrapper_cache=wrapper_cache,
            )
            diagnostics.extend(derived_diagnostics)
            for raw_candidate in derived_candidates:
                candidate = _rebase_candidate(raw_candidate, search_root, root)
                key = (str(candidate["path"]), int(candidate["line"]))
                previous = raw_candidates.get(key)
                if previous is None or _candidate_sort_key(candidate) < _candidate_sort_key(previous):
                    raw_candidates[key] = candidate
            if raw_candidates:
                break
    if not raw_candidates:
        for search_root in search_roots:
            wrapper_cache = {}
            for term in _event_search_terms(str(marker["instrumentation_name"])):
                matches, match_diagnostics = _rg_fixed_matches(search_root, term, timeout_seconds=timeout_seconds)
                diagnostics.extend(match_diagnostics)
                for match in matches:
                    for raw_candidate in _instrumentation_candidate(
                        search_root,
                        marker,
                        match,
                        expected_tag_macros,
                        timeout_seconds=timeout_seconds,
                        wrapper_cache=wrapper_cache,
                    ):
                        candidate = _rebase_candidate(raw_candidate, search_root, root)
                        key = (str(candidate["path"]), int(candidate["line"]))
                        previous = raw_candidates.get(key)
                        if previous is None or _candidate_sort_key(candidate) < _candidate_sort_key(previous):
                            raw_candidates[key] = candidate
                if len(raw_candidates) >= max_candidates * 3:
                    break
            if raw_candidates:
                break
    candidates = sorted(raw_candidates.values(), key=_candidate_sort_key)[:max_candidates]
    missing_scopes = source_scope.get("missing_relative_roots")
    missing_scope_note = ""
    if isinstance(missing_scopes, list) and missing_scopes:
        missing_scope_note = " Curated candidate source scope(s) absent from the selected connector: " + ", ".join(
            str(scope) for scope in missing_scopes
        ) + "."
    return {
        "status": "available" if candidates else "no_match",
        "reason": None if candidates else "No matching HiTrace instrumentation point was found in the selected source connector." + missing_scope_note,
        "marker": marker,
        "candidates": candidates,
        "diagnostics": diagnostics,
        "search_terms": _event_search_terms(str(marker["instrumentation_name"])),
        "source_scope": source_scope,
        "bounded_candidate_limit": max_candidates,
    }
