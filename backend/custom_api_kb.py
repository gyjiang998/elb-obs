"""User-built accelerator API knowledge bases.

The platform-owned API catalogs are release assets.  This module deliberately
stores user scans under the mutable state root and never mutates those release
files.  Discovery is a bounded, read-only source scan; its output is labelled
as lexical source evidence rather than precise AST or device capability proof.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any, Iterable

from .core import JsonRegistry, PlatformError, SourceConnectorManager, atomic_write_json, sha256_file, sha256_json, utc_now


SCHEMA_VERSION = "custom_accelerator_api_knowledge_base/v1"
REGISTRY_SCHEMA_VERSION = "custom_accelerator_api_kb_registry/v1"
BUILDER_VERSION = "1.1.0"
ANALYSIS_METHOD = "bounded_api_contract_discovery/v2"
CUSTOM_ROUTING_UNIT_PREFIX = "custom:"
CUSTOM_KB_ID_PATTERN = re.compile(r"^custom_api_kb_[a-f0-9]{24}$")

MAX_FILES_CONSIDERED = 50_000
MAX_FILES_READ = 4_000
MAX_TOTAL_BYTES_READ = 64 * 1024 * 1024
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_RECORDS = 300
MAX_SCAN_SECONDS = 75.0
MAX_DECLARATION_CHARS = 1_600

SOURCE_SUFFIXES = frozenset({".h", ".hh", ".hpp", ".hxx", ".c", ".cc", ".cpp", ".cxx"})
HEADER_SUFFIXES = frozenset({".h", ".hh", ".hpp", ".hxx"})
SKIPPED_DIRECTORIES = frozenset(
    {
        ".git",
        ".hg",
        ".svn",
        ".idea",
        ".vscode",
        ".venv",
        "__pycache__",
        "node_modules",
        "build",
        "dist",
        "out",
        "target",
        "coverage",
        ".local-artifacts",
        ".next",
        "heterogeneous-enable-web-runtime",
    }
)

UNIT_ALIASES: dict[str, tuple[str, ...]] = {
    "npu": ("npu", "nn", "nnrt", "oh_nn", "neural", "tensor", "mindspore", "model", "ai_core"),
    "gpu": ("gpu", "vk", "gl", "egl", "cl", "vulkan", "opencl", "opengl", "gles", "shader", "graphics"),
    "dsp": ("dsp", "audio_effect", "pcm", "hpae", "aae", "effect_hdi", "effect_vdi"),
    "aae": ("aae", "audio_effect", "effect", "pcm", "dsp"),
    "hpae": ("hpae", "audio_effect", "effect", "pcm", "dsp"),
    "media": ("media", "codec", "avcodec", "hcodec", "image_codec", "encode", "decode"),
    "codec": ("codec", "avcodec", "hcodec", "encode", "decode", "media"),
}

ROLE_RULES: tuple[tuple[str, tuple[str, ...], str], ...] = (
    ("capability_query", ("capability", "support", "available", "enumerate", "deviceinfo"), "能力查询"),
    ("resource_create", ("create", "alloc", "acquire", "open", "construct"), "资源创建"),
    ("resource_destroy", ("destroy", "free", "release", "close", "deinit"), "资源释放"),
    ("configuration", ("configure", "config", "setparam", "setattr", "setoption"), "配置"),
    ("initialization", ("initialize", "initialise", "init"), "初始化"),
    ("preparation", ("prepare", "compile", "build", "finalize", "finish"), "准备或编译"),
    ("execution", ("execute", "process", "dispatch", "submit", "enqueue", "run", "invoke"), "执行"),
    ("encoding", ("encode", "compress"), "编码"),
    ("decoding", ("decode", "decompress"), "解码"),
    ("lifecycle_start", ("start", "resume", "enable"), "启动"),
    ("lifecycle_stop", ("stop", "pause", "disable"), "停止"),
    ("data_transfer", ("copy", "upload", "download", "map", "unmap", "transfer", "read", "write"), "数据传输"),
    ("state_query", ("query", "get", "status", "result"), "状态或结果查询"),
)

CONTROL_SYMBOLS = frozenset({"if", "for", "while", "switch", "catch", "sizeof", "alignof", "decltype", "return"})
GENERIC_UNIT_TOKENS = frozenset({"api", "accelerator", "acceleration", "hardware", "unit", "engine", "runtime"})

SURFACE_BONUSES = {
    "public_api": 240,
    "interface_contract": 150,
    "exported_header": 95,
    "implementation_header": 35,
    "implementation": 0,
    "example_or_test": -180,
}


def _text(value: Any, limit: int = 1024) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def custom_routing_unit_id(kb_id: str) -> str:
    """Return the stable route-directory ID for one isolated custom catalog."""

    if not CUSTOM_KB_ID_PATTERN.fullmatch(kb_id):
        raise ValueError("invalid custom API knowledge-base ID")
    return f"{CUSTOM_ROUTING_UNIT_PREFIX}{kb_id}"


def is_custom_routing_unit(unit_id: str) -> bool:
    if not unit_id.startswith(CUSTOM_ROUTING_UNIT_PREFIX):
        return False
    return CUSTOM_KB_ID_PATTERN.fullmatch(unit_id.removeprefix(CUSTOM_ROUTING_UNIT_PREFIX)) is not None


def _tokenize(value: str) -> list[str]:
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", value)
    return [token.casefold() for token in re.findall(r"[A-Za-z0-9]+", expanded) if len(token) >= 2]


def _unit_terms(unit_name: str) -> list[str]:
    tokens = _tokenize(unit_name)
    terms = set(tokens)
    for token in tokens:
        terms.update(UNIT_ALIASES.get(token, ()))
    return sorted(terms)


def _primary_unit_terms(unit_name: str) -> list[str]:
    return sorted(token for token in set(_tokenize(unit_name)) if token not in GENERIC_UNIT_TOKENS)


def _symbol_matches_terms(symbol: str, terms: Iterable[str]) -> bool:
    lowered = symbol.casefold()
    compact = re.sub(r"[^a-z0-9]", "", lowered)
    tokens = set(_tokenize(symbol))
    for term in terms:
        normalized = term.casefold()
        compact_term = re.sub(r"[^a-z0-9]", "", normalized)
        if not compact_term:
            continue
        if len(compact_term) <= 2:
            if compact.startswith(compact_term) or compact_term in tokens:
                return True
        elif compact_term in compact:
            return True
    return False


def _source_surface(relative_path: str, *, is_header: bool) -> str:
    lowered = f"/{relative_path.casefold().lstrip('/')}"
    if any(marker in lowered for marker in ("/test/", "/tests/", "/mock/", "/fuzz/", "/demo/", "/example/")):
        return "example_or_test"
    if any(
        marker in lowered
        for marker in ("/interfaces/kits/", "/interfaces/ndk/", "/ndk/", "/sdk/", "/public/", "/public_api/")
    ):
        return "public_api"
    if "/interfaces/" in lowered and "/include/" in lowered:
        return "public_api"
    if any(
        marker in lowered
        for marker in (
            "/interfaces/innerkits/",
            "/interfaces/inner_api/",
            "/drivers/interface/",
            "/interface/",
            "/hdi/",
            "/vdi/",
            "/idl/",
        )
    ):
        return "interface_contract"
    if is_header and (lowered.startswith("/include/") or "/include/" in lowered or "/inc/" in lowered):
        return "exported_header"
    if is_header:
        return "implementation_header"
    return "implementation"


def _path_score(relative_path: str, terms: Iterable[str], *, is_header: bool) -> int:
    lowered = relative_path.casefold()
    surface = _source_surface(relative_path, is_header=is_header)
    score = SURFACE_BONUSES[surface] + (18 if is_header else 0)
    for term in terms:
        normalized = term.replace("_", "/")
        if len(re.sub(r"[^a-z0-9]", "", term)) > 2 and (term in lowered or normalized in lowered):
            score += 6
    return score


def _semantic_role(symbol: str, *, element_kind: str = "", container_name: str = "") -> tuple[str, str]:
    normalized = "".join(_tokenize(symbol))
    operator_text = f"{normalized} {''.join(_tokenize(container_name))}"
    if element_kind == "enum_value" and any(term in operator_text for term in ("operator", "operation", "ops", "kernel")):
        return "operator", "算子或操作类型"
    if element_kind in {"enum_value", "macro_constant"}:
        return "configuration_constant", "能力、格式或配置常量"
    for role_id, terms, label in ROLE_RULES:
        if any(term in normalized for term in terms):
            return role_id, label
    return "api_entrypoint", "接口调用"


def _data_forms(text: str) -> list[str]:
    lowered = text.casefold()
    forms: list[str] = []
    rules = (
        ("tensor / model / graph", ("tensor", "model", "graph", "shape", "dimension")),
        ("image / pixel / texture", ("image", "pixel", "rgba", "yuv", "texture", "surface")),
        ("audio / PCM / frame", ("audio", "pcm", "sample", "framecount", "channel")),
        ("video / codec / bitstream", ("video", "codec", "bitstream", "nal", "mime")),
        ("buffer / memory", ("buffer", "memory", "mem", "data", "address", "ashmem", "nativebuffer")),
        ("device / context / queue", ("device", "context", "queue", "stream", "command")),
    )
    for label, terms in rules:
        if any(term in lowered for term in terms):
            forms.append(label)
    return forms[:5]


def _exposure(relative_path: str) -> str:
    surface = _source_surface(relative_path, is_header=Path(relative_path).suffix.casefold() in HEADER_SUFFIXES)
    if surface == "public_api":
        return "public_sdk_or_ndk_candidate"
    if surface == "interface_contract":
        return "system_hdi_or_vdi_candidate"
    if surface == "exported_header":
        return "exported_header_candidate"
    if surface == "example_or_test":
        return "example_or_test_candidate"
    lowered = f"/{relative_path.casefold()}"
    if any(marker in lowered for marker in ("/frameworks/", "/services/", "/src/")):
        return "system_internal_candidate"
    if "/third_party/" in lowered:
        return "third_party_api_candidate"
    return "source_declared_candidate"


def _clean_comment(value: str) -> str:
    lines = []
    for raw in value.splitlines():
        line = re.sub(r"^\s*(?://[/!]?|/\*+|\*+/?|\*/)", "", raw).strip()
        line = re.sub(r"\s+", " ", line)
        if line and not line.startswith(("@", "\\")):
            lines.append(line)
    return " ".join(lines)[:1200]


def _leading_comment(lines: list[str], start_index: int) -> str:
    collected: list[str] = []
    index = start_index - 1
    in_block = False
    while index >= 0 and len(collected) < 14:
        stripped = lines[index].strip()
        if not stripped:
            if collected:
                break
            index -= 1
            continue
        if stripped.startswith("//"):
            collected.append(lines[index])
            index -= 1
            continue
        if stripped.endswith("*/") or in_block:
            in_block = True
            collected.append(lines[index])
            if "/*" in stripped:
                break
            index -= 1
            continue
        break
    return _clean_comment("\n".join(reversed(collected)))


def _statement_symbol(statement: str) -> tuple[str, str] | None:
    compact = re.sub(r"\s+", " ", statement).strip()
    compact = compact.split("{", 1)[0].strip()
    compact = compact[:-1].strip() if compact.endswith(";") else compact
    if "(" not in compact or ")" not in compact or len(compact) > MAX_DECLARATION_CHARS:
        return None
    if re.match(r"^(?:typedef|using)\b", compact):
        return None
    prefix = compact.split("(", 1)[0].strip()
    match = re.search(r"(?P<symbol>[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_~][A-Za-z0-9_]*)*)\s*$", prefix)
    if match is None:
        return None
    symbol = match.group("symbol")
    leaf = symbol.rsplit("::", 1)[-1]
    if leaf.casefold() in CONTROL_SYMBOLS or leaf.startswith("~") or leaf.startswith("operator"):
        return None
    declaration_prefix = prefix[: match.start()].strip()
    if not declaration_prefix or "=" in declaration_prefix:
        return None
    if declaration_prefix.endswith((".", "->")):
        return None
    return symbol, compact


def _candidate_statements(text: str, *, is_header: bool) -> Iterable[dict[str, Any]]:
    lines = text.splitlines()
    buffer: list[str] = []
    start_index = 0
    paren_depth = 0
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if not buffer:
            if not stripped or stripped.startswith(("#", "//", "/*", "*")) or "(" not in stripped:
                continue
            if re.match(r"^(?:if|for|while|switch|catch|return|static_assert)\b", stripped):
                continue
            if not is_header and len(raw) - len(raw.lstrip()) > 4:
                continue
            buffer = [stripped]
            start_index = index
            paren_depth = stripped.count("(") - stripped.count(")")
        else:
            buffer.append(stripped)
            paren_depth += stripped.count("(") - stripped.count(")")
        joined = " ".join(buffer)
        terminates = paren_depth <= 0 and (";" in stripped or "{" in stripped)
        if not terminates and len(joined) <= MAX_DECLARATION_CHARS and len(buffer) <= 20:
            continue
        if terminates and (is_header or "{" in stripped):
            parsed = _statement_symbol(joined)
            if parsed is not None:
                symbol, declaration = parsed
                yield {
                    "symbol": symbol,
                    "declaration": declaration,
                    "line_start": start_index + 1,
                    "line_end": index + 1,
                    "comment": _leading_comment(lines, start_index),
                    "api_element_kind": "function_declaration" if is_header else "function_definition",
                }
        buffer = []
        paren_depth = 0


def _candidate_enum_values(text: str) -> Iterable[dict[str, Any]]:
    """Extract named enum members as API contract elements.

    Accelerator APIs commonly expose their supported operators, formats and
    modes through enum values rather than callable functions.  They are kept
    as first-class records instead of being inferred from implementation
    switch statements.
    """

    lines = text.splitlines()
    in_enum = False
    start_name = ""
    enum_comment = ""
    entries: list[dict[str, Any]] = []
    enum_start = re.compile(
        r"\b(?:typedef\s+)?enum(?:\s+(?:class|struct))?\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)?\s*(?::[^\{]+)?\s*\{"
    )
    enum_value = re.compile(r"^(?P<symbol>[A-Za-z_][A-Za-z0-9_]*)\s*(?:=\s*(?P<value>.+?))?\s*,?$")

    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if not in_enum:
            match = enum_start.search(stripped)
            if match is None:
                continue
            in_enum = True
            start_name = match.group("name") or ""
            enum_comment = _leading_comment(lines, index)
            entries = []
            continue

        closing = re.search(r"}\s*(?P<name>[A-Za-z_][A-Za-z0-9_]*)?\s*;", stripped)
        if closing is not None:
            container_name = closing.group("name") or start_name
            for entry in entries:
                entry["container_name"] = container_name
                if not entry.get("comment"):
                    entry["comment"] = enum_comment
                yield entry
            in_enum = False
            start_name = ""
            enum_comment = ""
            entries = []
            continue

        if not stripped or stripped.startswith(("//", "/*", "*", "#")):
            continue
        code = re.sub(r"/\*.*?\*/", "", stripped).split("//", 1)[0].strip()
        match = enum_value.fullmatch(code)
        if match is None:
            continue
        symbol = match.group("symbol")
        if symbol.casefold() in CONTROL_SYMBOLS:
            continue
        entries.append(
            {
                "symbol": symbol,
                "declaration": code[:MAX_DECLARATION_CHARS],
                "line_start": index + 1,
                "line_end": index + 1,
                "comment": _leading_comment(lines, index),
                "api_element_kind": "enum_value",
            }
        )


def _candidate_macros(text: str) -> Iterable[dict[str, Any]]:
    lines = text.splitlines()
    macro_pattern = re.compile(
        r"^\s*#\s*define\s+(?P<symbol>[A-Za-z_][A-Za-z0-9_]*)(?P<args>\([^)]*\))?\s+(?P<value>\S.*)$"
    )
    for index, raw in enumerate(lines):
        match = macro_pattern.match(raw)
        if match is None:
            continue
        value = match.group("value").strip()
        if not value or value.endswith("_H"):
            continue
        yield {
            "symbol": match.group("symbol"),
            "declaration": raw.strip()[:MAX_DECLARATION_CHARS],
            "line_start": index + 1,
            "line_end": index + 1,
            "comment": _leading_comment(lines, index),
            "api_element_kind": "macro_function" if match.group("args") else "macro_constant",
        }


def _candidate_calls(
    text: str,
    *,
    terms: Iterable[str],
    declaration_locations: set[tuple[int, str]],
) -> Iterable[dict[str, Any]]:
    lines = text.splitlines()
    call_pattern = re.compile(r"(?<![A-Za-z0-9_])(?P<symbol>[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_~][A-Za-z0-9_]*)*)\s*\(")
    literal_pattern = re.compile(r'"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\'')
    for index, raw in enumerate(lines):
        stripped = raw.strip()
        if not stripped or stripped.startswith(("#", "//", "/*", "*", "typedef", "using")):
            continue
        searchable = literal_pattern.sub("", raw)
        for match in call_pattern.finditer(searchable):
            symbol = match.group("symbol")
            leaf = symbol.rsplit("::", 1)[-1]
            if leaf.casefold() in CONTROL_SYMBOLS or leaf.startswith(("~", "operator")):
                continue
            if (index + 1, symbol) in declaration_locations or not _symbol_matches_terms(symbol, terms):
                continue
            yield {
                "symbol": symbol,
                "declaration": stripped[:MAX_DECLARATION_CHARS],
                "line_start": index + 1,
                "line_end": index + 1,
                "comment": _leading_comment(lines, index),
                "evidence_kind": "callsite",
                "api_element_kind": "callsite",
            }


def _has_export_marker(declaration: str) -> bool:
    prefix = declaration.split("(", 1)[0]
    return bool(
        re.search(
            r"\b(?:[A-Z][A-Z0-9_]*(?:API|EXPORT|PUBLIC)|__attribute__|__declspec|extern\s+\"C\")\b",
            prefix,
        )
    )


def _candidate_score(
    candidate: dict[str, Any],
    *,
    path_score: int,
    unit_terms: list[str],
    primary_terms: list[str],
) -> int:
    symbol = str(candidate.get("symbol") or "")
    declaration = str(candidate.get("declaration") or "")
    comment = str(candidate.get("comment") or "")
    container_name = str(candidate.get("container_name") or "")
    element_kind = str(candidate.get("api_element_kind") or "function_declaration")
    surface = str(candidate.get("source_surface") or "implementation")
    evidence = f"{symbol} {container_name} {declaration} {comment}"

    score = path_score
    if _symbol_matches_terms(symbol, primary_terms):
        score += 90
    elif _symbol_matches_terms(symbol, unit_terms):
        score += 32
    if _symbol_matches_terms(container_name, unit_terms):
        score += 24
    if _has_export_marker(declaration):
        score += 55
    if comment:
        score += 12
    if element_kind == "enum_value":
        score += 28
        operator_text = " ".join(_tokenize(f"{symbol} {container_name}"))
        if any(term in operator_text for term in ("operator", "operation", "ops", "kernel")):
            score += 80
    elif element_kind == "macro_function":
        score += 24
    elif element_kind == "function_definition":
        score -= 28
    elif element_kind == "callsite":
        score -= 70
    if "::" in symbol and surface in {"implementation", "implementation_header", "example_or_test"}:
        score -= 22
    if surface == "example_or_test" and not _symbol_matches_terms(symbol, primary_terms):
        score -= 80
    # A comment or signature can establish relevance for contract headers even
    # when the leaf symbol itself is generic (for example Configure or Run).
    if _symbol_matches_terms(evidence, primary_terms):
        score += 30
    return score


def _candidate_is_in_scope(
    candidate: dict[str, Any],
    *,
    unit_terms: list[str],
    primary_terms: list[str],
    small_repository_scope: bool,
) -> bool:
    surface = str(candidate.get("source_surface") or "implementation")
    symbol = str(candidate.get("symbol") or "")
    evidence = f"{symbol} {candidate.get('container_name', '')} {candidate.get('declaration', '')} {candidate.get('comment', '')}"
    direct_unit_match = _symbol_matches_terms(symbol, primary_terms)
    alias_match = _symbol_matches_terms(evidence, unit_terms)
    exported = _has_export_marker(str(candidate.get("declaration") or ""))

    if surface == "public_api":
        return True
    if surface in {"interface_contract", "exported_header"}:
        return alias_match or exported or small_repository_scope
    if surface == "implementation_header":
        return direct_unit_match or exported
    if surface == "implementation":
        return direct_unit_match or exported
    return direct_unit_match and str(candidate.get("api_element_kind")) == "callsite"


def _candidate_bucket(candidate: dict[str, Any]) -> str:
    element_kind = str(candidate.get("api_element_kind") or "function_declaration")
    surface = str(candidate.get("source_surface") or "implementation")
    contract_surface = surface in {"public_api", "interface_contract", "exported_header"}
    role_id, _ = _semantic_role(
        str(candidate.get("symbol") or ""),
        element_kind=element_kind,
        container_name=str(candidate.get("container_name") or ""),
    )
    if contract_surface and element_kind in {"function_declaration", "macro_function"}:
        return "callable_contract"
    if contract_surface and role_id == "operator":
        return "operator_contract"
    if contract_surface:
        return "data_or_capability_contract"
    return "supporting_implementation"


def _select_balanced_candidates(candidates: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Keep callable APIs and operators visible when one category is large."""

    if len(candidates) <= limit:
        selected = list(candidates)
    else:
        callable_reserve = max(1, int(limit * 0.40))
        operator_reserve = max(1, int(limit * 0.40))
        selected_ids: set[int] = set()
        for bucket, reserve in (
            ("callable_contract", callable_reserve),
            ("operator_contract", operator_reserve),
        ):
            bucket_values = [item for item in candidates if _candidate_bucket(item) == bucket]
            selected_ids.update(id(item) for item in bucket_values[:reserve])
        for item in candidates:
            if len(selected_ids) >= limit:
                break
            selected_ids.add(id(item))
        selected = [item for item in candidates if id(item) in selected_ids]

    bucket_order = {
        "callable_contract": 0,
        "operator_contract": 1,
        "data_or_capability_contract": 2,
        "supporting_implementation": 3,
    }
    selected.sort(
        key=lambda item: (
            bucket_order[_candidate_bucket(item)],
            -int(item.get("relevance_score") or 0),
            str(item.get("source_file") or ""),
            int(item.get("line_start") or 0),
            str(item.get("symbol") or ""),
        )
    )
    return selected[:limit]


class CustomApiKnowledgeBaseManager:
    """Build and manage isolated user-owned API catalogues."""

    def __init__(self, state_root: Path, connectors: SourceConnectorManager) -> None:
        self.state_root = state_root.resolve()
        self.root = self.state_root / "custom-api-kbs"
        self.root.mkdir(parents=True, exist_ok=True)
        self.connectors = connectors
        self.registry = JsonRegistry(
            self.root / "registry.json",
            {"schema_version": REGISTRY_SCHEMA_VERSION, "knowledge_bases": []},
        )
        self._lock = threading.RLock()

    def list(self) -> list[dict[str, Any]]:
        values = self.registry.read().get("knowledge_bases", [])
        rows = [dict(item) for item in values if isinstance(item, dict)]
        rows.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return rows

    def reasoning_catalogs(self) -> dict[str, dict[str, Any]]:
        """Project every user catalog into a dynamic route/API directory.

        The route projection is intentionally concise.  Full declarations stay
        out of coarse routing and enter model context only after this exact
        directory is selected.
        """

        catalogs: dict[str, dict[str, Any]] = {}
        for metadata in self.list():
            kb_id = str(metadata.get("kb_id") or "")
            if not CUSTOM_KB_ID_PATTERN.fullmatch(kb_id):
                continue
            path = self.root / f"{kb_id}.json"
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            records = [item for item in payload.get("records", []) if isinstance(item, dict)]
            if not records:
                continue

            def representative_order(record: dict[str, Any]) -> tuple[int, int, str]:
                family = record.get("semantic_family") if isinstance(record.get("semantic_family"), dict) else {}
                family_id = str(family.get("id") or "")
                if "operator" in family_id:
                    family_rank = 0
                elif any(token in family_id for token in ("execution", "encoding", "decoding", "capability")):
                    family_rank = 1
                else:
                    family_rank = 2
                exposure_rank = 0 if str(record.get("exposure") or "") == "public_sdk_or_ndk_candidate" else 1
                return family_rank, exposure_rank, str(record.get("symbol") or "")

            representatives = sorted(records, key=representative_order)[:8]
            semantic_families = sorted(
                {
                    str(family.get("label_zh") or family.get("id") or "")
                    for record in records
                    for family in [record.get("semantic_family") if isinstance(record.get("semantic_family"), dict) else {}]
                    if str(family.get("label_zh") or family.get("id") or "")
                }
            )
            data_forms = sorted(
                {
                    str(item)
                    for record in records
                    for match in [record.get("code_feature_match") if isinstance(record.get("code_feature_match"), dict) else {}]
                    for item in match.get("expected_data_forms", [])
                    if isinstance(item, str) and item.strip()
                }
            )
            route_unit_id = custom_routing_unit_id(kb_id)
            route_evidence_id = f"custom_api_kb:{kb_id}"
            display_name = str(payload.get("display_name") or metadata.get("display_name") or kb_id)
            accelerator_unit = str(payload.get("accelerator_unit") or metadata.get("accelerator_unit") or display_name)
            quality = payload.get("quality") if isinstance(payload.get("quality"), dict) else {}
            warnings = [str(item) for item in quality.get("warnings", []) if isinstance(item, str) and item.strip()]
            routing_record = {
                "record_id": kb_id,
                "record_type": "custom_hardware_api_directory",
                "hardware_id": kb_id,
                "routing_unit_id": route_unit_id,
                "routing_evidence_id": route_evidence_id,
                "routing_projection_version": "custom_api_routing_projection/v1",
                "custom_api_kb_id": kb_id,
                "content_sha256": str(payload.get("content_sha256") or ""),
                "accelerator_unit": accelerator_unit,
                "name": display_name,
                "software_interface": f"{len(records)} 条源码可追溯 API 合同候选",
                "supported_algorithms": [*semantic_families[:8], *data_forms[:6]],
                "implementation_paths": [],
                "constraints": [
                    "自建 API 目录来自有界源码扫描，只证明静态候选存在。",
                    "公开性、目标设备支持、调用权限、功能等价和性能收益均需后续验证。",
                    *warnings[:3],
                ],
                "representative_api_summaries": [
                    {
                        "record_id": str(record.get("record_id") or ""),
                        "symbol": str(record.get("symbol") or ""),
                        "semantic_family": record.get("semantic_family") if isinstance(record.get("semantic_family"), dict) else {},
                        "one_line_abstract_zh": _text(record.get("one_line_abstract_zh"), 260),
                        "expected_data_forms": [
                            str(item)
                            for item in (
                                record.get("code_feature_match", {}).get("expected_data_forms", [])
                                if isinstance(record.get("code_feature_match"), dict)
                                else []
                            )
                            if isinstance(item, str)
                        ][:4],
                    }
                    for record in representatives
                ],
                "quality": quality,
            }
            catalogs[route_unit_id] = {
                "unit_id": route_unit_id,
                "kb_id": kb_id,
                "metadata": self._metadata(payload),
                "routing_record": routing_record,
                "records": records,
            }
        return catalogs

    def _resolve_connector(self, *, source_connector_id: str = "", source_root: str = "") -> dict[str, Any]:
        connector_id = _text(source_connector_id, 160)
        requested_root = _text(source_root, 4096)
        if connector_id:
            connector = self.connectors.get(connector_id)
            if requested_root and Path(str(connector.get("source_root"))).resolve() != Path(requested_root).expanduser().resolve():
                raise PlatformError(
                    "CUSTOM_API_KB_SOURCE_MISMATCH",
                    "代码仓 ID 与路径没有指向同一个已登记代码仓。",
                    status_code=409,
                )
            return connector
        if requested_root:
            resolved = Path(requested_root).expanduser().resolve()
            for connector in self.connectors.list():
                if Path(str(connector.get("source_root") or "")).resolve() == resolved:
                    return connector
            raise PlatformError(
                "CUSTOM_API_KB_SOURCE_NOT_REGISTERED",
                "该服务端路径尚未登记为代码仓；请先在代码仓页面登记。",
                status_code=422,
                details={"source_root": str(resolved)},
            )
        raise PlatformError(
            "CUSTOM_API_KB_SOURCE_REQUIRED",
            "构建自建 API 知识库需要已登记的代码仓 ID 或服务端路径。",
            status_code=422,
        )

    def _enumerate_files(self, source_root: Path, terms: list[str], deadline: float) -> tuple[list[tuple[int, Path, str]], dict[str, Any]]:
        candidates: list[tuple[int, Path, str]] = []
        considered = 0
        truncated = False
        for current_root, directories, files in os.walk(source_root, followlinks=False):
            directories[:] = sorted(
                directory for directory in directories
                if not directory.startswith(".")
                and directory not in SKIPPED_DIRECTORIES
                and not (Path(current_root) / directory).is_symlink()
                and self.state_root not in (Path(current_root) / directory).resolve().parents
                and (Path(current_root) / directory).resolve() != self.state_root
            )
            for name in sorted(files):
                if time.monotonic() >= deadline or considered >= MAX_FILES_CONSIDERED:
                    truncated = True
                    break
                path = Path(current_root) / name
                suffix = path.suffix.casefold()
                if suffix not in SOURCE_SUFFIXES or path.is_symlink():
                    continue
                considered += 1
                try:
                    relative = path.relative_to(source_root).as_posix()
                except ValueError:
                    continue
                candidates.append((_path_score(relative, terms, is_header=suffix in HEADER_SUFFIXES), path, relative))
            if truncated:
                break
        candidates.sort(key=lambda item: (-item[0], item[2]))
        return candidates[:MAX_FILES_READ], {
            "source_files_considered": considered,
            "source_files_selected": min(len(candidates), MAX_FILES_READ),
            "enumeration_truncated": truncated or len(candidates) > MAX_FILES_READ,
        }

    def _record(self, candidate: dict[str, Any], *, unit_name: str, kb_id: str) -> dict[str, Any]:
        symbol = str(candidate["symbol"])
        declaration = str(candidate["declaration"])
        comment = str(candidate.get("comment") or "")
        element_kind = str(candidate.get("api_element_kind") or "function_declaration")
        container_name = str(candidate.get("container_name") or "")
        role_id, role_label = _semantic_role(symbol, element_kind=element_kind, container_name=container_name)
        forms = _data_forms(f"{symbol} {declaration} {comment}")
        aliases = []
        for token in _tokenize(symbol.rsplit("::", 1)[-1]):
            if token not in aliases:
                aliases.append(token)
        source_file = str(candidate["source_file"])
        evidence_kind = str(candidate.get("evidence_kind") or "declaration")
        is_callsite = evidence_kind == "callsite"
        first_comment_sentence = re.split(r"(?<=[。！？.!?])\s+", comment, maxsplit=1)[0][:280] if comment else ""
        abstract = (
            f"源码注释表明该接口用于：{first_comment_sentence}"
            if first_comment_sentence
            else (
                f"源码调用点出现 `{symbol}`；其接口语义、参数合同和设备能力仍需结合声明与实现验证。"
                if is_callsite
                else (
                    f"源码声明的“{container_name}”枚举成员，表示{role_label}。"
                    if element_kind == "enum_value" and container_name
                    else f"源码声明的{role_label}接口；具体语义、调用顺序和设备能力仍需结合实现与运行时验证。"
                )
            )
        )
        identity = sha256_json(
            {
                "kb_id": kb_id,
                "source_file": source_file,
                "line_start": candidate["line_start"],
                "symbol": symbol,
                "declaration": declaration,
            }
        )
        return {
            "record_id": f"custom_api:{identity[:24]}",
            "record_type": (
                "custom_accelerator_api_callsite_candidate"
                if is_callsite
                else "custom_accelerator_api_candidate"
            ),
            "accelerator_unit": unit_name,
            "symbol": symbol,
            "name_zh": symbol,
            "api_element_kind": element_kind,
            "container_name": container_name,
            "one_line_abstract_zh": abstract,
            "detailed_description_zh": (
                f"该记录由服务端对已登记代码仓执行有界静态 API 面扫描得到。源码中可见 `{symbol}` "
                f"{'调用点' if is_callsite else '声明'}，当前仅能确认其位置和词法角色“{role_label}”；"
                "不能据此断言接口为公开 SDK、目标设备支持该接口或使用后存在性能收益。"
            ),
            "semantic_family": {"id": f"custom_{role_id}", "label_zh": role_label},
            "exposure": _exposure(source_file),
            "confidence": "source_usage_candidate" if is_callsite else "source_declared_candidate",
            "evidence_status": (
                "source_observed_callsite_candidate"
                if is_callsite
                else "source_observed_static_candidate"
            ),
            "code_feature_match": {
                "analysis_method": ANALYSIS_METHOD,
                "operator_aliases": aliases[:10],
                "expected_data_forms": forms,
                "ast_signals": [],
                "standalone_offload_note": (
                    "该记录描述源码 API 调用点，不是接口定义、设备支持或热点代码到硬件单元的 AST 适配证明。"
                    if is_callsite
                    else "该记录描述源码 API 面，不是热点代码到硬件单元的 AST 适配证明。"
                ),
            },
            "source_contract": {
                "evidence_kind": evidence_kind,
                "api_element_kind": element_kind,
                "container_name": container_name,
                "source_surface": candidate.get("source_surface"),
                "source_file": source_file,
                "source_line": candidate["line_start"],
                "line_end": candidate["line_end"],
                "declaration": declaration,
                "declaration_or_usage": declaration,
                "leading_comment": comment,
                "source_file_sha256": candidate["source_file_sha256"],
                "exposure": _exposure(source_file),
            },
            "capability_boundary": {
                "target_device_availability": "evidence_required",
                "statement_zh": "源码声明只能证明接口候选存在；公开性、链接面、驱动/provider、设备支持和收益都需要独立验证。",
            },
            "runtime_verification": [
                "确认调用方可访问的真实头文件、库和权限层级。",
                "在目标设备上查询或初始化真实 capability/provider/device，并保留原始返回。",
                "以确定性输入对比 CPU/software fallback 的功能等价性。",
                "功能闭合后再测量数据搬运、同步和端到端收益。",
            ],
        }

    def build(
        self,
        *,
        unit_name: str,
        source_connector_id: str = "",
        source_root: str = "",
    ) -> dict[str, Any]:
        normalized_unit = _text(unit_name, 80)
        if not normalized_unit or any(ord(character) < 32 for character in normalized_unit):
            raise PlatformError("CUSTOM_API_KB_UNIT_INVALID", "异构单元名称不能为空或包含控制字符。", status_code=422)
        connector = self._resolve_connector(source_connector_id=source_connector_id, source_root=source_root)
        root = Path(str(connector["source_root"])).resolve()
        if not root.is_dir():
            raise PlatformError("CUSTOM_API_KB_SOURCE_UNAVAILABLE", "已登记代码仓当前不可访问。", status_code=409)

        started = time.monotonic()
        deadline = started + MAX_SCAN_SECONDS
        terms = _unit_terms(normalized_unit)
        primary_terms = _primary_unit_terms(normalized_unit)
        files, scan = self._enumerate_files(root, terms, deadline)
        bytes_read = 0
        files_read = 0
        raw_candidates: list[dict[str, Any]] = []
        declared_symbols: set[str] = set()
        scanned_file_hashes: list[dict[str, str]] = []
        term_hits = 0
        timed_out = False
        small_repository_scope = scan["source_files_considered"] <= 800

        for path_score, path, relative in files:
            if time.monotonic() >= deadline:
                timed_out = True
                break
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size <= 0 or stat.st_size > MAX_FILE_BYTES or bytes_read + stat.st_size > MAX_TOTAL_BYTES_READ:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            files_read += 1
            bytes_read += stat.st_size
            lowered = text.casefold()
            content_hits = sum(lowered.count(term) for term in terms)
            term_hits += content_hits
            file_sha256 = sha256_file(path)
            scanned_file_hashes.append({"path": relative, "sha256": file_sha256})
            is_header = path.suffix.casefold() in HEADER_SUFFIXES
            source_surface = _source_surface(relative, is_header=is_header)
            functions = list(_candidate_statements(text, is_header=is_header))
            contract_elements = list(functions)
            if is_header:
                contract_elements.extend(_candidate_enum_values(text))
                contract_elements.extend(_candidate_macros(text))
            declaration_locations = {(int(item["line_start"]), str(item["symbol"])) for item in functions}
            for item in contract_elements:
                item["source_surface"] = source_surface
                if not _candidate_is_in_scope(
                    item,
                    unit_terms=terms,
                    primary_terms=primary_terms,
                    small_repository_scope=small_repository_scope,
                ):
                    continue
                relevance = _candidate_score(
                    item,
                    path_score=path_score,
                    unit_terms=terms,
                    primary_terms=primary_terms,
                )
                declared_symbols.add(str(item["symbol"]).casefold())
                raw_candidates.append(
                    {
                        **item,
                        "evidence_kind": "declaration",
                        "source_file": relative,
                        "source_file_sha256": file_sha256,
                        "relevance_score": relevance,
                    }
                )
            for item in _candidate_calls(text, terms=terms, declaration_locations=declaration_locations):
                item["source_surface"] = source_surface
                if not _candidate_is_in_scope(
                    item,
                    unit_terms=terms,
                    primary_terms=primary_terms,
                    small_repository_scope=small_repository_scope,
                ):
                    continue
                raw_candidates.append(
                    {
                        **item,
                        "source_file": relative,
                        "source_file_sha256": file_sha256,
                        "relevance_score": _candidate_score(
                            item,
                            path_score=path_score,
                            unit_terms=terms,
                            primary_terms=primary_terms,
                        ),
                    }
                )

        raw_candidates = [
            item
            for item in raw_candidates
            if item.get("evidence_kind") != "callsite"
            or str(item["symbol"]).casefold() not in declared_symbols
        ]

        raw_candidates.sort(
            key=lambda item: (-int(item["relevance_score"]), str(item["source_file"]), int(item["line_start"]), str(item["symbol"]))
        )
        deduplicated: list[dict[str, Any]] = []
        seen: set[tuple[str, ...]] = set()
        for item in raw_candidates:
            symbol_key = str(item["symbol"]).casefold()
            element_kind = str(item.get("api_element_kind") or "function_declaration")
            if element_kind in {"function_declaration", "function_definition"} and "::" not in str(item["symbol"]):
                # C APIs cannot overload. Keep the highest-ranked public
                # declaration and use lower-ranked definitions as supporting
                # source evidence rather than duplicate API records.
                key = ("c_api_symbol", symbol_key)
            else:
                key = (str(item.get("evidence_kind") or "declaration"), element_kind, symbol_key)
            if item.get("evidence_kind") != "callsite" and "::" in str(item["symbol"]):
                key += (re.sub(r"\s+", " ", str(item["declaration"])).casefold(),)
            if key in seen:
                continue
            seen.add(key)
            deduplicated.append(item)

        candidate_count_before_limit = len(deduplicated)
        deduplicated = _select_balanced_candidates(deduplicated, MAX_RECORDS)

        if not deduplicated:
            raise PlatformError(
                "CUSTOM_API_KB_NO_API_CANDIDATES",
                "在当前有界扫描范围内没有发现可用 API 声明；请登记更聚焦的模块目录，或确认异构单元名称与源码术语一致。",
                status_code=422,
                details={
                    **scan,
                    "files_read": files_read,
                    "bytes_read": bytes_read,
                    "unit_terms": terms,
                    "timed_out": timed_out,
                },
            )

        kb_id = f"custom_api_kb_{secrets.token_hex(12)}"
        display_name = f"{normalized_unit.upper()} API（自建）"
        records = [self._record(item, unit_name=normalized_unit, kb_id=kb_id) for item in deduplicated]
        role_counts = Counter(str(record["semantic_family"]["label_zh"]) for record in records)
        exposure_counts = Counter(str(record["exposure"]) for record in records)
        evidence_kind_counts = Counter(str(record["source_contract"]["evidence_kind"]) for record in records)
        element_kind_counts = Counter(str(record["api_element_kind"]) for record in records)
        source_surface_counts = Counter(str(record["source_contract"].get("source_surface") or "unknown") for record in records)
        public_contract_count = sum(
            count
            for surface, count in source_surface_counts.items()
            if surface in {"public_api", "interface_contract", "exported_header"}
        )
        operator_count = role_counts.get("算子或操作类型", 0)
        internal_count = sum(
            count
            for surface, count in source_surface_counts.items()
            if surface in {"implementation", "implementation_header", "example_or_test"}
        )
        quality_warnings: list[str] = []
        if public_contract_count == 0:
            quality_warnings.append("未发现公开头文件、接口合同或导出头文件候选；结果可能只覆盖内部实现。")
        if internal_count > public_contract_count and public_contract_count > 0:
            quality_warnings.append("内部实现候选多于 API 合同候选；建议登记更聚焦的接口目录或复核暴露层级。")
        quality_status = "pass" if not quality_warnings else "warning"
        snapshot = connector.get("snapshot") if isinstance(connector.get("snapshot"), dict) else {}
        created_at = utc_now()
        payload: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "kb_id": kb_id,
            "kb_version": "1.1.0",
            "display_name": display_name,
            "origin": "user_built",
            "deletable": True,
            "merge_policy": "developer_only",
            "accelerator_unit": normalized_unit,
            "created_at": created_at,
            "producer": {"name": "custom-api-kb-builder", "version": BUILDER_VERSION},
            "source": {
                "source_connector_id": connector["connector_id"],
                "display_name": connector.get("display_name"),
                "source_root": str(root),
                "source_snapshot_id": snapshot.get("snapshot_id"),
                "snapshot_clean": snapshot.get("clean"),
            },
            "scan": {
                "analysis_method": ANALYSIS_METHOD,
                "unit_terms": terms,
                "primary_unit_terms": primary_terms,
                "limits": {
                    "max_files_considered": MAX_FILES_CONSIDERED,
                    "max_files_read": MAX_FILES_READ,
                    "max_total_bytes_read": MAX_TOTAL_BYTES_READ,
                    "max_file_bytes": MAX_FILE_BYTES,
                    "max_records": MAX_RECORDS,
                    "max_scan_seconds": MAX_SCAN_SECONDS,
                },
                **scan,
                "files_read": files_read,
                "bytes_read": bytes_read,
                "scanned_source_sha256": sha256_json(scanned_file_hashes),
                "unit_term_occurrences": term_hits,
                "timed_out": timed_out,
                "result_truncated": candidate_count_before_limit > len(records),
            },
            "summary": {
                "record_count": len(records),
                "semantic_role_counts": dict(sorted(role_counts.items())),
                "exposure_counts": dict(sorted(exposure_counts.items())),
                "evidence_kind_counts": dict(sorted(evidence_kind_counts.items())),
                "api_element_kind_counts": dict(sorted(element_kind_counts.items())),
                "source_surface_counts": dict(sorted(source_surface_counts.items())),
                "public_contract_record_count": public_contract_count,
                "operator_record_count": operator_count,
                "internal_implementation_record_count": internal_count,
                "evidence_status": "source_observed_static_candidates",
                "device_availability": "evidence_required",
            },
            "quality": {
                "status": quality_status,
                "public_contract_primary": public_contract_count >= internal_count and public_contract_count > 0,
                "warnings": quality_warnings,
                "acceptance_note_zh": "知识库主体应为消费方可见 API、接口合同和算子/能力常量；内部服务方法只能作为补充候选。",
            },
            "evidence_boundary": {
                "statement_zh": "自建库与平台内置库物理分离。记录来自有界词法扫描，不等价于精准 AST、公开 API 审核、设备支持或性能收益。",
                "automatic_merge_to_platform": False,
            },
            "records": records,
        }
        payload["content_sha256"] = sha256_json(payload)
        metadata = self._metadata(payload)
        with self._lock:
            atomic_write_json(self.root / f"{kb_id}.json", payload)
            registry = self.registry.read()
            current = [item for item in registry.get("knowledge_bases", []) if isinstance(item, dict)]
            current.append(metadata)
            self.registry.write({"schema_version": REGISTRY_SCHEMA_VERSION, "knowledge_bases": current})
        return {"knowledge_base": metadata, "records": records[:12], "scan": payload["scan"]}

    @staticmethod
    def _metadata(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            key: payload.get(key)
            for key in (
                "kb_id",
                "kb_version",
                "display_name",
                "origin",
                "deletable",
                "merge_policy",
                "accelerator_unit",
                "created_at",
                "content_sha256",
                "source",
                "summary",
                "scan",
                "quality",
                "evidence_boundary",
            )
        }

    def read(self, kb_id: str, *, query: str = "") -> dict[str, Any]:
        metadata = next((item for item in self.list() if item.get("kb_id") == kb_id), None)
        if metadata is None:
            raise PlatformError("CUSTOM_API_KB_NOT_FOUND", "自建 API 知识库不存在。", status_code=404)
        path = self.root / f"{kb_id}.json"
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise PlatformError("CUSTOM_API_KB_CORRUPT", "自建 API 知识库无法读取。", status_code=500) from exc
        records = [item for item in payload.get("records", []) if isinstance(item, dict)]
        normalized_query = _text(query, 500).casefold()
        if normalized_query:
            records = [
                item
                for item in records
                if normalized_query in json.dumps(item, ensure_ascii=False, sort_keys=True).casefold()
            ]
        return {
            "knowledge_base": self._metadata(payload),
            "query": query,
            "record_count": len(records),
            "records": records,
        }

    def record(self, kb_id: str, record_id: str) -> dict[str, Any]:
        payload = self.read(kb_id)
        record = next((item for item in payload["records"] if item.get("record_id") == record_id), None)
        if record is None:
            raise PlatformError("KB_RECORD_NOT_FOUND", "自建 API 记录不存在。", status_code=404)
        return {"knowledge_base": payload["knowledge_base"], "record": record}

    def delete(self, kb_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"custom_api_kb_[a-f0-9]{24}", kb_id):
            raise PlatformError(
                "CUSTOM_API_KB_DELETE_FORBIDDEN",
                "只能删除平台登记的自建 API 知识库；内置知识库不提供用户删除接口。",
                status_code=403,
            )
        with self._lock:
            registry = self.registry.read()
            current = [item for item in registry.get("knowledge_bases", []) if isinstance(item, dict)]
            target = next((item for item in current if item.get("kb_id") == kb_id), None)
            if target is None:
                raise PlatformError("CUSTOM_API_KB_NOT_FOUND", "自建 API 知识库不存在。", status_code=404)
            self.registry.write(
                {
                    "schema_version": REGISTRY_SCHEMA_VERSION,
                    "knowledge_bases": [item for item in current if item.get("kb_id") != kb_id],
                }
            )
            (self.root / f"{kb_id}.json").unlink(missing_ok=True)
        return {"status": "deleted", "kb_id": kb_id, "display_name": target.get("display_name")}
