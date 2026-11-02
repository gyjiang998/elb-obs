"""Application execution-flow discovery and generic scenario assets.

The model performs architectural reasoning, while this module owns the
read-only repository boundary, immutable evidence artifacts, and deterministic
contract validation. Code-discovery metadata deliberately lives outside the
scenario document so the reference JSON field names and downstream consumers
remain unchanged without importing the reference workload's semantics.
"""

from __future__ import annotations

from copy import deepcopy
from hashlib import sha256
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Iterable

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError

from .core import ArtifactStore, JsonRegistry, PlatformError, SourceConnectorManager, sha256_file, utc_now


DISCOVERY_ARTIFACT_TYPE = "application_flow_discovery"
SCENARIO_ARTIFACT_TYPE = "application_flow_scenario"
DISCOVERY_SCHEMA_VERSION = "application_flow_discovery/v1"
SCENARIO_VERSION = "2.0"
PREFERENCES_SCHEMA_VERSION = "application_flow_preferences/v1"

_TRACE_ANCHOR_BASE_FIELDS = {
    "id", "name", "kind", "process_name", "source_evidence_ids", "verification_status",
}
_TRACE_ANCHOR_OPTIONAL_FIELDS = {"source_location", "instrumentation"}
_TRACE_EVENT_LABEL_MATCHES = {"exact_literal", "wrapper_literal", "function_derived", "static_prefix"}
_SOURCE_REFERENCE_PREFIX = "@source/"
_SOURCE_CONNECTOR_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,160}$")
_FLOW_ANALYSIS_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_C_STRING_LITERAL_RE = re.compile(r'(?:(?:u8|u|U|L))?"((?:\\.|[^"\\])*)"')
_SINGLE_STRING_LITERAL_RE = re.compile(r"'((?:\\.|[^'\\])*)'")
_TEMPLATE_STRING_LITERAL_RE = re.compile(r"`((?:\\.|[^`\\])*)`")
_QUALIFIED_FUNCTION_RE = re.compile(
    r"((?:[A-Za-z_]\w*::)+(?:~?[A-Za-z_]\w*|operator(?:\[\]|\(\)|[^\s(]+)))\s*(?=\()"
)
_PLAIN_FUNCTION_RE = re.compile(r"\b(~?[A-Za-z_]\w*)\s*(?=\()")

_SOURCE_SUFFIXES = {
    ".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx",
    ".ets", ".ts", ".tsx", ".js", ".jsx", ".java", ".kt", ".rs", ".go", ".py",
    ".idl", ".proto", ".json", ".json5", ".yaml", ".yml", ".toml", ".xml", ".md", ".gn", ".gni", ".cmake",
}
_SOURCE_FILENAMES = {
    "CMakeLists.txt", "BUILD.gn", "BUILD", "Makefile", "README", "README.md",
    "bundle.json", "module.json", "module.json5", "package.json",
}
_SKIP_DIRECTORIES = {
    ".git", ".hg", ".svn", ".next", ".local-artifacts", ".idea", ".vscode",
    "node_modules", "third_party", "vendor", "build", "build-out", "dist",
    "out", "output", "coverage", "__pycache__", "target", "generated", "doxygen_output",
}
_MANIFEST_NAMES = {
    "CMakeLists.txt", "BUILD.gn", "BUILD", "Makefile", "bundle.json",
    "module.json", "module.json5", "package.json", "README.md",
}
_ENTRY_FILE_TOKENS = (
    "main", "ability", "application", "service", "session", "manager", "controller",
    "pipeline", "workflow", "capture", "startup", "entry", "dispatcher",
)
_ACTION_SYMBOL = re.compile(r"\b(?P<symbol>(?:OH_[A-Za-z0-9_]+|[A-Za-z_][A-Za-z0-9_]*(?:::[A-Za-z_][A-Za-z0-9_]*)?))\s*\(")
_ACTION_WORDS = {
    "begin", "close", "commit", "configure", "connect", "create", "destroy", "disable", "disconnect",
    "enable", "end", "init", "initialize", "launch", "open", "prepare", "register", "release", "start",
    "stop", "submit", "capture", "unregister",
}
_ACTION_WORD_PRIORITY = {
    "begin": 35, "close": 40, "commit": 45, "create": 45, "end": 35, "open": 50, "release": 35,
    "start": 50, "stop": 45, "capture": 50, "configure": 30, "prepare": 30, "submit": 30,
    "connect": 25, "disconnect": 25, "init": 25, "initialize": 25, "launch": 30,
    "enable": 15, "disable": 15, "register": 8, "unregister": 8, "destroy": 25,
}
_TRACE_CALL_RE = re.compile(
    r"\b(?P<name>(?=[A-Za-z0-9_:.]*(?:hitrace|trace|bytrace))"
    r"[A-Za-z_]\w*(?:(?:::|\.)[A-Za-z_]\w*)*)\s*\(",
    re.IGNORECASE,
)
_TRACE_BARE_MACRO_RE = re.compile(r"\b(?P<name>[A-Z][A-Z0-9_]*(?:HITRACE|TRACE|BYTRACE|METER)[A-Z0-9_]*)\b(?!\s*\()")
_TRACE_SYMBOL_RE = re.compile(
    r"\b(?P<name>(?=[A-Za-z0-9_:]*(?:hitrace|trace|bytrace))"
    r"[A-Za-z_]\w*(?:::[A-Za-z_]\w*)*)\b",
    re.IGNORECASE,
)
_TRACE_BASE_CALL_TOKENS = (
    "HITRACE_METER", "StartTrace", "FinishTrace", "OH_HiTrace_StartTrace", "OH_HiTrace_FinishTrace",
    "hiTraceMeter.startAsyncTrace", "hiTraceMeter.finishAsyncTrace", "hiTraceMeter.startSyncTrace",
    "hiTraceMeter.finishSyncTrace",
)
_LOW_VALUE_PATH_PARTS = {"test", "tests", "unittest", "moduletest", "fuzztest", "mock", "demo", "sample", "samples"}
_SAFE_ID = re.compile(r"^[a-z][a-z0-9_]{1,79}$")
_STAGE_COMMENT = re.compile(
    r'^%%\s+@stage\s+(?P<id>\S+)\s+order=(?P<order>\d+)\s+'
    r'process=(?P<process>\S+)\s+(?P<requirement>optional|required)'
    r'(?:\s+name="(?P<name>[^"]+)")?\s*$'
)
_SCENARIO_COMMENT = re.compile(r"^%%\s+@scenario\s+(?P<scenario>\S+)\s+version=(?P<version>\S+)\s*$")
_SCENARIO_COMMENT_PREFIX = re.compile(r"^%%\s+@scenario\b.*$")
_DESCRIPTION_COMMENT = re.compile(r"^%%\s+@description\s+(?P<description>.+?)\s*$")
_CONSTANT_COMMENT = re.compile(
    r"^%%\s+@constant\s+(?P<name>[A-Z][A-Z0-9_]{1,79})=(?P<value>.+?)\s*$"
)
_PROCESS_GROUP_COMMENT_PREFIX = re.compile(r"^%%\s+@process_group\s+(?P<body>.+?)\s*$")
_STAGE_DETAIL_COMMENT = re.compile(
    r"^%%\s+(?P<kind>start|end|matcher):\s+(?P<body>.+?)\s*$"
)
_RELATION_COMMENT = re.compile(r"^%%\s+@relation\s+(?P<body>\{.+\})\s*$")
_STRUCTURED_METADATA_COMMENT = re.compile(
    r"^%%\s+@(?P<kind>action|phase|stage_hierarchy|activity|activity_relation|implementation)\s+(?P<body>.+?)\s*$"
)
_UNSAFE_MERMAID = re.compile(r"(?i)(?:<script|javascript:|\bclick\s+\S+|\bhref\s+|%%\{init)")

_ACTIVITY_RELATION_TYPES = {
    "sequence",
    "data_dependency",
    "control_dependency",
    "async_dispatch",
    "parallel",
    "parallel_candidate",
    "join_all",
    "join_any",
    "fallback",
}


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _string(value: Any, *, field: str, maximum: int = 4096) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise PlatformError(
            "APPLICATION_FLOW_CONTRACT_INVALID",
            f"{field} 必须为非空且长度不超过 {maximum} 的字符串。",
            status_code=422,
            details={"field": field},
        )
    return value.strip()


def _string_list(value: Any, *, field: str, maximum: int, item_limit: int = 512) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise PlatformError(
            "APPLICATION_FLOW_CONTRACT_INVALID",
            f"{field} 必须为不超过 {maximum} 项的字符串数组。",
            status_code=422,
            details={"field": field},
        )
    result: list[str] = []
    for index, item in enumerate(value):
        result.append(_string(item, field=f"{field}[{index}]", maximum=item_limit))
    return result


def _flow_analysis_id(value: Any, *, allow_empty: bool = False) -> str:
    analysis_id = str(value or "").strip()
    if not analysis_id and allow_empty:
        return ""
    if not _FLOW_ANALYSIS_ID_RE.fullmatch(analysis_id):
        raise PlatformError(
            "APPLICATION_FLOW_ANALYSIS_ID_INVALID",
            "应用流程分析 ID 格式无效。",
            status_code=422,
        )
    return analysis_id


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _normalized_relative_path(value: str) -> str:
    text = value.strip().replace("\\", "/")
    if not text or text == ".":
        return ""
    if text.startswith("/") or re.match(r"^[A-Za-z]:", text):
        raise PlatformError(
            "APPLICATION_FLOW_SCOPE_INVALID",
            "流程分析范围必须是代码仓内的相对目录。",
            status_code=422,
            details={"subpath": value},
        )
    parts = [part for part in text.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise PlatformError(
            "APPLICATION_FLOW_SCOPE_INVALID",
            "流程分析范围不能越过代码仓根目录。",
            status_code=422,
            details={"subpath": value},
        )
    return "/".join(parts)


def _split_source_reference(default_connector_id: str, value: str) -> tuple[str, str]:
    """Decode a cross-connector source path without changing the stable path field."""

    normalized = value.strip().replace("\\", "/")
    if not normalized.startswith(_SOURCE_REFERENCE_PREFIX):
        return default_connector_id, _normalized_relative_path(normalized)
    payload = normalized[len(_SOURCE_REFERENCE_PREFIX):]
    connector_id, separator, relative = payload.partition("/")
    if not separator or not _SOURCE_CONNECTOR_ID_RE.fullmatch(connector_id):
        raise PlatformError(
            "APPLICATION_FLOW_SOURCE_REFERENCE_INVALID",
            "跨代码仓源码路径必须使用 @source/<connector-id>/<relative-path>。",
            status_code=422,
            details={"path": value},
        )
    return connector_id, _normalized_relative_path(relative)


def _source_reference(default_connector_id: str, connector_id: str, relative_path: str) -> str:
    normalized = _normalized_relative_path(relative_path)
    return normalized if connector_id == default_connector_id else f"{_SOURCE_REFERENCE_PREFIX}{connector_id}/{normalized}"


def _is_source_file(path: Path) -> bool:
    return path.name in _SOURCE_FILENAMES or path.suffix.lower() in _SOURCE_SUFFIXES


def _source_string_literals(value: str) -> list[str]:
    """Return literal payloads without treating arbitrary source text as an event."""

    values: list[str] = []
    for pattern in (_C_STRING_LITERAL_RE, _SINGLE_STRING_LITERAL_RE, _TEMPLATE_STRING_LITERAL_RE):
        for matched in pattern.finditer(value):
            raw = matched.group(1)
            decoded = raw
            if "\\" in raw:
                try:
                    decoded = bytes(raw, "utf-8").decode("unicode_escape")
                except UnicodeDecodeError:
                    decoded = raw
            if "${" in decoded:
                decoded = decoded.split("${", 1)[0]
            if decoded and decoded not in values:
                values.append(decoded)
    return values


def _stable_function_trace_selector(value: str) -> str:
    """Derive a compiler-stable substring for __func__/__PRETTY_FUNCTION__ labels."""

    compact = " ".join(str(value or "").strip().split())
    qualified = _QUALIFIED_FUNCTION_RE.findall(compact)
    if not qualified and re.fullmatch(
        r"(?:[A-Za-z_]\w*::)+(?:~?[A-Za-z_]\w*|operator(?:\[\]|\(\)|[^\s(]+))",
        compact,
    ):
        qualified = [compact]
    if qualified:
        parts = qualified[-1].split("::")
        return "::".join(parts[-2:]) if len(parts) >= 2 else parts[-1]
    plain = _PLAIN_FUNCTION_RE.findall(compact)
    if plain:
        return plain[-1]
    if re.fullmatch(r"~?[A-Za-z_]\w*", compact):
        return compact
    raise PlatformError(
        "APPLICATION_FLOW_TRACE_ANCHOR_INVALID",
        "function_derived 锚点无法从 symbol 提取稳定函数匹配名。",
        status_code=422,
        details={"symbol": compact},
    )


class ApplicationFlowManager:
    """Own bounded source inspection and immutable flow assets."""

    def __init__(
        self,
        connectors: SourceConnectorManager,
        artifact_store: ArtifactStore,
        *,
        scenario_schema_path: Path,
        discovery_schema_path: Path,
        stage_analysis_schema_path: Path | None = None,
    ) -> None:
        self.connectors = connectors
        self.artifact_store = artifact_store
        self.scenario_schema_path = scenario_schema_path.resolve()
        self.discovery_schema_path = discovery_schema_path.resolve()
        self.stage_analysis_schema_path = (
            stage_analysis_schema_path.resolve()
            if stage_analysis_schema_path is not None
            else self.scenario_schema_path.parent / "application-flow-stage-analysis-v1.schema.json"
        )
        self._schemas = {
            "scenario": self._load_schema(self.scenario_schema_path),
            "discovery": self._load_schema(self.discovery_schema_path),
            "stage_analysis": self._load_schema(self.stage_analysis_schema_path),
        }
        try:
            for schema in self._schemas.values():
                Draft202012Validator.check_schema(schema)
        except SchemaError as exc:
            raise RuntimeError(f"application flow schema is invalid: {exc.message}") from exc
        self._validators = {
            name: Draft202012Validator(schema)
            for name, schema in self._schemas.items()
        }
        self._macro_definition_cache: dict[tuple[str, str, str], tuple[str, ...]] = {}
        self._trace_wrapper_definition_cache: dict[tuple[str, str, str], tuple[dict[str, Any], ...]] = {}
        self.asset_registry = JsonRegistry(
            self.artifact_store.root / "application-flows" / "registry.json",
            {"schema_version": "application_flow_asset_registry/v1", "deleted_assets": {}},
        )
        self.preferences_registry = JsonRegistry(
            self.artifact_store.root / "application-flows" / "preferences.json",
            {
                "schema_version": PREFERENCES_SCHEMA_VERSION,
                "confirm_flow_before_trace": True,
            },
        )
        self.schema_metadata = {
            "scenario": {
                "schema_id": self._schemas["scenario"].get("$id"),
                "content_sha256": sha256_file(self.scenario_schema_path),
            },
            "discovery": {
                "schema_id": self._schemas["discovery"].get("$id"),
                "content_sha256": sha256_file(self.discovery_schema_path),
            },
            "stage_analysis": {
                "schema_id": self._schemas["stage_analysis"].get("$id"),
                "content_sha256": sha256_file(self.stage_analysis_schema_path),
            },
        }

    @staticmethod
    def _new_flow_analysis_id() -> str:
        return f"application-flow:{secrets.token_hex(12)}"

    @staticmethod
    def _artifact_flow_analysis_id(artifact: dict[str, Any], artifact_type: str) -> str:
        explicit = str(artifact.get("application_flow_analysis_id") or "").strip()
        if explicit:
            return explicit
        if artifact_type == DISCOVERY_ARTIFACT_TYPE:
            contextual = str(_record(artifact.get("analysis_context")).get("application_flow_analysis_id") or "").strip()
            return contextual or f"legacy-application-flow:{artifact.get('artifact_id')}"
        discovery_id = str(artifact.get("discovery_artifact_id") or artifact.get("artifact_id") or "")
        return f"legacy-application-flow:{discovery_id}"

    def require_analysis_scope(
        self,
        artifact_type: str,
        artifact_id: str,
        application_flow_analysis_id: str,
    ) -> dict[str, Any]:
        """Read an artifact only when it belongs to the active flow analysis."""

        expected = _flow_analysis_id(application_flow_analysis_id)
        if artifact_type == DISCOVERY_ARTIFACT_TYPE:
            artifact = self.read_discovery(artifact_id)
        elif artifact_type == SCENARIO_ARTIFACT_TYPE:
            artifact = self.read_scenario(artifact_id)
        else:
            raise PlatformError(
                "APPLICATION_FLOW_ASSET_TYPE_INVALID",
                "应用流程工件类型非法。",
                status_code=422,
            )
        actual = self._artifact_flow_analysis_id(artifact, artifact_type)
        if actual != expected:
            raise PlatformError(
                "APPLICATION_FLOW_ANALYSIS_SCOPE_MISMATCH",
                "该应用流程工件属于另一条独立分析。",
                status_code=409,
                details={
                    "artifact_id": artifact_id,
                    "expected_application_flow_analysis_id": expected,
                    "actual_application_flow_analysis_id": actual,
                },
            )
        return artifact

    def _macro_definition_snippets(
        self,
        source_connector_id: str,
        macro: str,
        evidence_by_id: dict[str, dict[str, Any]],
    ) -> list[str]:
        """Resolve a wrapper macro once per source snapshot for label classification."""

        connector = self.connectors.get(source_connector_id)
        repository_root = Path(str(connector["source_root"])).resolve()
        snapshot_id = str(_record(connector.get("snapshot")).get("snapshot_id") or "")
        cache_key = (str(repository_root), snapshot_id, macro)
        cached = self._macro_definition_cache.get(cache_key)
        if cached is not None:
            return list(cached)

        snippets: list[str] = []
        definition_re = re.compile(rf"^\s*#\s*define\s+{re.escape(macro)}\b")

        def append_definition(path: Path, line_number: int) -> None:
            if not path.is_file() or path.stat().st_size > 4 * 1024 * 1024:
                return
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            if line_number < 1 or line_number > len(lines) or not definition_re.search(lines[line_number - 1]):
                return
            end = line_number
            while end < len(lines) and lines[end - 1].rstrip().endswith("\\") and end - line_number < 12:
                end += 1
            snippet = "\n".join(lines[line_number - 1:end])
            if snippet not in snippets:
                snippets.append(snippet)

        for evidence in evidence_by_id.values():
            path_text = str(evidence.get("path") or "")
            if not path_text:
                continue
            try:
                evidence_connector_id, evidence_path = _split_source_reference(source_connector_id, path_text)
                if evidence_connector_id != source_connector_id:
                    continue
                path = self.connectors.resolve_source_path(evidence_connector_id, evidence_path)
                line_start = max(1, int(evidence.get("line_start") or 1))
                line_end = max(line_start, int(evidence.get("line_end") or line_start))
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except (OSError, ValueError, PlatformError):
                continue
            for line_number in range(line_start, min(line_end, len(lines)) + 1):
                if definition_re.search(lines[line_number - 1]):
                    append_definition(path, line_number)

        if not snippets:
            try:
                matches = self.search_source(
                    source_connector_id,
                    query=f"#define {macro}",
                    max_results=8,
                ).get("matches", [])
            except PlatformError:
                matches = []
            for match in matches:
                if not isinstance(match, dict):
                    continue
                try:
                    path = self.connectors.resolve_source_path(source_connector_id, str(match.get("path") or ""))
                    append_definition(path, int(match.get("line") or 0))
                except (OSError, ValueError, PlatformError):
                    continue

        self._macro_definition_cache[cache_key] = tuple(snippets)
        return snippets

    def _trace_label_source_context(
        self,
        source_connector_id: str,
        *,
        source: Path,
        line: int,
        macro: str,
        evidence_by_id: dict[str, dict[str, Any]],
    ) -> tuple[str, str]:
        lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
        window = "\n".join(lines[max(0, line - 5):min(len(lines), line + 4)])
        definitions = self._macro_definition_snippets(source_connector_id, macro, evidence_by_id)
        if not definitions and "." not in macro and not any(token in macro for token in _TRACE_BASE_CALL_TOKENS):
            definitions = [
                str(item.get("definition") or "")
                for item in self._trace_wrapper_definition_candidates(source_connector_id, macro)
            ]
        return window, "\n".join([window, *definitions])

    def _trace_wrapper_definition_candidates(
        self,
        source_connector_id: str,
        symbol: str,
    ) -> list[dict[str, Any]]:
        """Find bounded function/class wrapper bodies that call another Trace symbol."""

        connector = self.connectors.get(source_connector_id)
        repository_root = Path(str(connector["source_root"])).resolve()
        snapshot_id = str(_record(connector.get("snapshot")).get("snapshot_id") or "")
        simple_name = re.split(r"::|\.", symbol)[-1]
        cache_key = (str(repository_root), snapshot_id, f"function:{simple_name}")
        cached = self._trace_wrapper_definition_cache.get(cache_key)
        if cached is not None:
            return [dict(item) for item in cached]
        try:
            matches = self.search_source(
                source_connector_id,
                query=f"{simple_name}(",
                max_results=16,
            ).get("matches", [])
        except PlatformError:
            matches = []
        candidates: list[dict[str, Any]] = []
        for match in matches:
            if not isinstance(match, dict):
                continue
            try:
                relative_path = str(match.get("path") or "")
                path = self.connectors.resolve_source_path(source_connector_id, relative_path)
                if path.stat().st_size > 4 * 1024 * 1024:
                    continue
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
                line = max(1, int(match.get("line") or 1))
            except (OSError, ValueError, PlatformError):
                continue
            window_start = max(0, line - 4)
            window_end = min(len(lines), line + 36)
            snippet = "\n".join(lines[window_start:window_end])
            nested_calls = [matched.group("name") for matched in _TRACE_CALL_RE.finditer(snippet)]
            nested_other = [name for name in nested_calls if re.split(r"::|\.", name)[-1] != simple_name]
            if not nested_other:
                continue
            if any(
                item.get("path") == relative_path and abs(int(item.get("line") or 0) - line) <= 40
                for item in candidates
            ):
                continue
            candidates.append({
                "kind": "wrapper_definition_candidate",
                "symbol": simple_name,
                "path": relative_path,
                "line": line,
                "definition": snippet[:5000],
                "nested_trace_calls": nested_calls[:12],
                "base_hitrace_calls": [
                    name for name in nested_calls
                    if any(token in name for token in _TRACE_BASE_CALL_TOKENS)
                ][:8],
            })
            if len(candidates) >= 4:
                break
        self._trace_wrapper_definition_cache[cache_key] = tuple(candidates)
        return [dict(item) for item in candidates]

    @staticmethod
    def _load_schema(path: Path) -> dict[str, Any]:
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"application flow schema cannot be loaded: {path}: {exc}") from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"application flow schema root is not an object: {path}")
        return value

    def _validate_schema(self, name: str, value: dict[str, Any], *, code: str) -> None:
        errors = sorted(
            self._validators[name].iter_errors(value),
            key=lambda error: tuple(str(part) for part in error.absolute_path),
        )
        if not errors:
            return
        error = errors[0]
        path = ".".join(str(part) for part in error.absolute_path) or "$"
        raise PlatformError(
            code,
            f"结构不符合标准 Schema：{path}: {error.message}",
            status_code=422,
            details={
                "schema_id": self._schemas[name].get("$id"),
                "path": list(error.absolute_path),
                "validator": error.validator,
            },
        )

    def _scope(self, source_connector_id: str, subpath: str = "") -> tuple[dict[str, Any], Path, str]:
        connector = self.connectors.get(source_connector_id)
        root = Path(str(connector["source_root"])).resolve()
        relative = _normalized_relative_path(subpath)
        scope = (root / relative).resolve() if relative else root
        if not _is_relative_to(scope, root) or not scope.is_dir():
            raise PlatformError(
                "APPLICATION_FLOW_SCOPE_NOT_FOUND",
                "代码仓内不存在所选流程分析目录。",
                status_code=404,
                details={"source_connector_id": source_connector_id, "subpath": relative},
            )
        return connector, scope, relative

    def _resolve_source_reference(
        self,
        default_connector_id: str,
        path: str,
    ) -> tuple[str, dict[str, Any], Path, str]:
        connector_id, relative_path = _split_source_reference(default_connector_id, path)
        connector = self.connectors.get(connector_id)
        source = self.connectors.resolve_source_path(connector_id, relative_path)
        return connector_id, connector, source, relative_path

    def _normalize_source_scopes(
        self,
        source_connector_id: str,
        *,
        subpath: str,
        source_scopes: list[dict[str, Any]] | None,
        code_graph_artifact_id: str = "",
    ) -> list[dict[str, Any]]:
        raw_scopes = source_scopes or [{
            "source_connector_id": source_connector_id,
            "subpath": subpath,
            "code_graph_artifact_id": code_graph_artifact_id,
        }]
        if not 1 <= len(raw_scopes) <= 12:
            raise PlatformError(
                "APPLICATION_FLOW_SOURCE_SCOPE_INVALID",
                "一次应用流程分析必须包含 1 到 12 个代码仓范围。",
                status_code=422,
            )
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for index, raw in enumerate(raw_scopes):
            item = _record(raw)
            connector_id = _string(
                item.get("source_connector_id"),
                field=f"source_scopes[{index}].source_connector_id",
                maximum=160,
            )
            if connector_id in seen:
                raise PlatformError(
                    "APPLICATION_FLOW_SOURCE_SCOPE_INVALID",
                    "同一个代码仓不能在一次流程分析中重复选择。",
                    status_code=422,
                    details={"source_connector_id": connector_id},
                )
            seen.add(connector_id)
            connector, _, relative_scope = self._scope(connector_id, str(item.get("subpath") or ""))
            snapshot_id = str(_record(connector.get("snapshot")).get("snapshot_id") or "")
            declared_snapshot_id = str(item.get("source_snapshot_id") or "").strip()
            if declared_snapshot_id and declared_snapshot_id != snapshot_id:
                raise PlatformError(
                    "APPLICATION_FLOW_SOURCE_BINDING_MISMATCH",
                    "流程分析范围引用的代码仓快照已经变化。",
                    status_code=409,
                    details={
                        "source_connector_id": connector_id,
                        "expected_snapshot_id": declared_snapshot_id,
                        "actual_snapshot_id": snapshot_id,
                    },
                )
            graph_artifact_id = str(item.get("code_graph_artifact_id") or "").strip()
            normalized.append({
                "source_connector_id": connector_id,
                "source_snapshot_id": snapshot_id,
                "subpath": relative_scope,
                "code_graph_artifact_id": graph_artifact_id or None,
                "role": "primary" if connector_id == source_connector_id else "related",
            })
        if source_connector_id not in seen:
            raise PlatformError(
                "APPLICATION_FLOW_SOURCE_SCOPE_INVALID",
                "多代码仓范围必须包含负责归档的主代码仓。",
                status_code=422,
                details={"source_connector_id": source_connector_id},
            )
        normalized.sort(key=lambda item: (item["source_connector_id"] != source_connector_id, item["source_connector_id"]))
        return normalized

    def _validate_bound_source_scopes(self, artifact: dict[str, Any]) -> list[dict[str, Any]]:
        context = _record(artifact.get("analysis_context"))
        raw_scopes = context.get("source_scopes")
        primary_connector_id = str(artifact.get("source_connector_id") or "")
        discovery = _record(artifact.get("discovery"))
        if not isinstance(raw_scopes, list) or not raw_scopes:
            raw_scopes = [{
                "source_connector_id": primary_connector_id,
                "source_snapshot_id": discovery.get("source_snapshot_id"),
                "subpath": discovery.get("scope") or "",
                "code_graph_artifact_id": context.get("code_graph_artifact_id"),
            }]
        return self._normalize_source_scopes(
            primary_connector_id,
            subpath=str(discovery.get("scope") or ""),
            source_scopes=[_record(item) for item in raw_scopes if isinstance(item, dict)],
            code_graph_artifact_id=str(context.get("code_graph_artifact_id") or ""),
        )

    def _search_scope(self, source_connector_id: str, subpath: str = "") -> tuple[dict[str, Any], Path, str]:
        """Resolve a search scope that may be either a directory or one file."""

        connector = self.connectors.get(source_connector_id)
        root = Path(str(connector["source_root"])).resolve()
        relative = _normalized_relative_path(subpath)
        scope = (root / relative).resolve() if relative else root
        valid_file = scope.is_file() and _is_source_file(scope)
        if not _is_relative_to(scope, root) or not (scope.is_dir() or valid_file):
            raise PlatformError(
                "APPLICATION_FLOW_SCOPE_NOT_FOUND",
                "代码仓内不存在所选流程分析目录或源码文件。",
                status_code=404,
                details={"source_connector_id": source_connector_id, "subpath": relative},
            )
        return connector, scope, relative

    @staticmethod
    def _binding(connector: dict[str, Any]) -> dict[str, Any]:
        snapshot = _record(connector.get("snapshot"))
        snapshot_id = str(snapshot.get("snapshot_id") or "")
        fingerprint = _record(connector.get("worktree_fingerprint"))
        return {
            "declared_revision": snapshot_id or None,
            "resolved_snapshot_id": snapshot_id or None,
            "match_level": "exact" if snapshot.get("clean") else "compatible",
            "resolver_version": "application-flow/1.0",
            "snapshot_state": "clean" if snapshot.get("clean") else "dirty",
            "worktree_fingerprint_sha256": fingerprint.get("sha256"),
        }

    @staticmethod
    def _relative(root: Path, path: Path) -> str:
        return path.resolve().relative_to(root.resolve()).as_posix()

    def _iter_source_files(self, scope: Path, *, max_files: int, max_seconds: float) -> tuple[list[Path], bool, float]:
        started = time.monotonic()
        files: list[Path] = []
        truncated = False
        for current_root, directories, names in os.walk(scope):
            directories[:] = sorted(
                item for item in directories
                if item not in _SKIP_DIRECTORIES and not item.startswith(".")
            )
            for name in sorted(names):
                path = Path(current_root) / name
                if not _is_source_file(path):
                    continue
                files.append(path)
                if len(files) >= max_files or time.monotonic() - started >= max_seconds:
                    truncated = True
                    return files, truncated, time.monotonic() - started
        return files, truncated, time.monotonic() - started

    @staticmethod
    def _entry_path_score(relative: str) -> int:
        """Rank likely public workflow surfaces without claiming flow semantics."""

        lowered = relative.casefold()
        parts = set(lowered.split("/"))
        score = 0
        if parts & _LOW_VALUE_PATH_PARTS or any(token in lowered for token in ("_test.", "_mock.", "fuzzer")):
            score -= 240
        if "/interfaces/" in f"/{lowered}" or "/include/" in f"/{lowered}":
            score += 90
        if "/kits/" in f"/{lowered}" or "/ndk/" in f"/{lowered}":
            score += 55
        if "/frameworks/" in f"/{lowered}" or "/services/" in f"/{lowered}":
            score += 25
        if Path(relative).suffix.lower() in {".h", ".hh", ".hpp", ".hxx", ".idl", ".ets", ".ts"}:
            score += 30
        stem = Path(relative).stem.casefold()
        score += 12 * sum(token in stem for token in _ENTRY_FILE_TOKENS)
        return score

    @staticmethod
    def _action_symbol_priority(symbol: str) -> tuple[int, str] | None:
        if symbol.startswith("OH_"):
            segments = symbol.split("_")
            method_name = "_".join(segments[2:]) if len(segments) > 2 else ""
        elif "::" in symbol:
            method_name = symbol.rsplit("::", 1)[-1]
        else:
            method_name = symbol
        words = [
            item.casefold()
            for item in re.findall(r"[A-Z]+(?=[A-Z][a-z]|$)|[A-Z]?[a-z]+|[0-9]+", method_name)
        ]
        if not words:
            return None
        if words[0] == "on":
            return 38, "callback"
        action = words[0]
        if action not in _ACTION_WORDS:
            return None
        return _ACTION_WORD_PRIORITY[action], action

    def _find_action_symbols(
        self,
        files: list[Path],
        repository_root: Path,
        *,
        output_limit: int,
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """Return bounded public action declarations as navigation hints.

        This is intentionally lexical.  It gives the coding agent useful paths,
        symbols, and line numbers in one cheap pass, but never groups declarations
        into a business flow or asserts runtime reachability.
        """

        ranked_files = sorted(
            files,
            key=lambda path: (
                -self._entry_path_score(self._relative(repository_root, path)),
                self._relative(repository_root, path),
            ),
        )
        started = time.monotonic()
        scanned_bytes = 0
        total = 0
        candidates: list[tuple[int, dict[str, Any]]] = []
        seen: set[tuple[str, str, int]] = set()
        truncated = False
        for path in ranked_files[:600]:
            if time.monotonic() - started > 3.0 or scanned_bytes >= 24 * 1024 * 1024:
                truncated = True
                break
            try:
                size = path.stat().st_size
                if size > 1024 * 1024:
                    continue
                scanned_bytes += size
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            relative = self._relative(repository_root, path)
            path_score = self._entry_path_score(relative)
            for line_number, raw_line in enumerate(lines, 1):
                line = raw_line.strip()
                if not line or line.startswith(("//", "/*", "*", "#")):
                    continue
                for match in _ACTION_SYMBOL.finditer(line):
                    symbol = match.group("symbol")
                    action = self._action_symbol_priority(symbol)
                    if action is None:
                        continue
                    action_priority, action_kind = action
                    key = (relative, symbol, line_number)
                    if key in seen:
                        continue
                    seen.add(key)
                    total += 1
                    symbol_score = path_score + action_priority
                    if symbol.startswith("OH_"):
                        symbol_score += 45
                    if re.search(r"(?:Complete|Completed|Ready|Available|Finished|Error|Result)$", symbol):
                        symbol_score += 18
                    candidates.append((symbol_score, {
                        "path": relative,
                        "line": line_number,
                        "symbol": symbol,
                        "kind": "callback_candidate" if action_kind == "callback" else "action_candidate",
                        "text": line[:600],
                    }))
        candidates.sort(key=lambda item: (-item[0], item[1]["path"], item[1]["line"], item[1]["symbol"]))
        selected: list[dict[str, Any]] = []
        per_file: dict[str, int] = {}
        for _, item in candidates:
            path = str(item["path"])
            if per_file.get(path, 0) >= 4:
                continue
            selected.append(item)
            per_file[path] = per_file.get(path, 0) + 1
            if len(selected) >= output_limit:
                break
        return selected, total, truncated or total > len(selected)

    def inspect_repository(self, source_connector_id: str, *, subpath: str = "") -> dict[str, Any]:
        connector, scope, relative_scope = self._scope(source_connector_id, subpath)
        repository_root = Path(str(connector["source_root"])).resolve()
        files, truncated, elapsed = self._iter_source_files(scope, max_files=20_000, max_seconds=12.0)
        suffix_counts: dict[str, int] = {}
        manifests: list[str] = []
        all_entry_candidates: list[str] = []
        manifest_count = 0
        entry_candidate_count = 0
        manifest_output_limit = 16
        entry_output_limit = 24
        for path in files:
            suffix = path.suffix.lower() or path.name
            suffix_counts[suffix] = suffix_counts.get(suffix, 0) + 1
            relative = self._relative(repository_root, path)
            if path.name in _MANIFEST_NAMES:
                manifest_count += 1
                if len(manifests) < manifest_output_limit:
                    manifests.append(relative)
            stem = path.stem.lower()
            if any(token in stem for token in _ENTRY_FILE_TOKENS):
                entry_candidate_count += 1
                all_entry_candidates.append(relative)
        entry_candidates = sorted(
            all_entry_candidates,
            key=lambda relative: (-self._entry_path_score(relative), relative),
        )[:entry_output_limit]
        action_output_limit = 24
        action_symbols, action_symbol_count, action_symbols_truncated = self._find_action_symbols(
            files,
            repository_root,
            output_limit=action_output_limit,
        )
        top_languages = sorted(suffix_counts.items(), key=lambda item: (-item[1], item[0]))[:12]
        return {
            "schema_version": "application_flow_repository_inspection/v1",
            "source_connector_id": source_connector_id,
            "source_snapshot_id": _record(connector.get("snapshot")).get("snapshot_id"),
            "scope": relative_scope,
            "scan": {
                "files_considered": len(files),
                "truncated": truncated,
                "elapsed_ms": round(elapsed * 1000, 2),
                "limits": {"max_files": 20_000, "max_seconds": 12},
            },
            "language_file_counts": [{"suffix": key, "count": value} for key, value in top_languages],
            "manifest_candidates": manifests,
            "entry_file_candidates": entry_candidates,
            "action_symbol_candidates": action_symbols,
            "candidate_counts": {
                "manifest_total": manifest_count,
                "entry_file_total": entry_candidate_count,
                "action_symbol_total": action_symbol_count,
            },
            "output_limits": {
                "manifest_candidates": manifest_output_limit,
                "entry_file_candidates": entry_output_limit,
                "action_symbol_candidates": action_output_limit,
            },
            "output_truncated": {
                "manifest_candidates": manifest_count > len(manifests),
                "entry_file_candidates": entry_candidate_count > len(entry_candidates),
                "action_symbol_candidates": action_symbols_truncated,
            },
            "next_step_zh": (
                "action_symbol_candidates 是词法导航提示，不是业务流程结论。请用原生 rg/Read 对候选入口和完成边界"
                "各复核一个小窗口；形成 1 到 5 个候选后立即发布，不要在阶段一展开完整调用链。"
            ),
        }

    def _python_search(
        self,
        files: Iterable[Path],
        repository_root: Path,
        query: str,
        *,
        max_results: int,
    ) -> list[dict[str, Any]]:
        needle = query.casefold()
        results: list[dict[str, Any]] = []
        scanned_bytes = 0
        started = time.monotonic()
        for path in files:
            if len(results) >= max_results or scanned_bytes >= 64 * 1024 * 1024 or time.monotonic() - started > 12:
                break
            try:
                size = path.stat().st_size
                if size > 2 * 1024 * 1024:
                    continue
                scanned_bytes += size
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            for line_number, line in enumerate(lines, 1):
                if needle not in line.casefold():
                    continue
                results.append({
                    "path": self._relative(repository_root, path),
                    "line": line_number,
                    "text": line.strip()[:1200],
                })
                if len(results) >= max_results:
                    break
        return results

    def _rg_search(
        self,
        scope: Path,
        repository_root: Path,
        query: str,
        *,
        max_results: int,
    ) -> list[dict[str, Any]]:
        command = [
            str(shutil.which("rg")), "--json", "--ignore-case", "--fixed-strings",
            "--max-count", "3", "--max-filesize", "2M", "--hidden",
        ]
        for directory in sorted(_SKIP_DIRECTORIES):
            command.extend(["--glob", f"!**/{directory}/**"])
        command.extend([query, str(scope)])
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        results: list[dict[str, Any]] = []
        started = time.monotonic()
        assert process.stdout is not None
        try:
            for raw_line in process.stdout:
                if time.monotonic() - started > 15 or len(results) >= max_results:
                    process.terminate()
                    break
                try:
                    event = json.loads(raw_line)
                except json.JSONDecodeError:
                    continue
                if event.get("type") != "match":
                    continue
                data = _record(event.get("data"))
                path_text = str(_record(data.get("path")).get("text") or "")
                path = Path(path_text).resolve()
                if not _is_relative_to(path, repository_root):
                    continue
                results.append({
                    "path": self._relative(repository_root, path),
                    "line": int(data.get("line_number") or 0),
                    "text": str(_record(data.get("lines")).get("text") or "").strip()[:1200],
                })
        finally:
            if process.poll() is None:
                process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=2)
            process.stdout.close()
        return results

    def search_source(
        self,
        source_connector_id: str,
        *,
        query: str,
        subpath: str = "",
        max_results: int = 30,
    ) -> dict[str, Any]:
        connector, scope, relative_scope = self._search_scope(source_connector_id, subpath)
        query = _string(query, field="query", maximum=240)
        max_results = max(1, min(int(max_results), 80))
        repository_root = Path(str(connector["source_root"])).resolve()
        engine = "ripgrep_json"
        if shutil.which("rg"):
            matches = self._rg_search(scope, repository_root, query, max_results=max_results)
        else:
            engine = "bounded_python_fallback"
            files = [scope] if scope.is_file() else self._iter_source_files(scope, max_files=12_000, max_seconds=8.0)[0]
            matches = self._python_search(files, repository_root, query, max_results=max_results)
        return {
            "schema_version": "application_flow_source_search/v1",
            "source_connector_id": source_connector_id,
            "source_snapshot_id": _record(connector.get("snapshot")).get("snapshot_id"),
            "scope": relative_scope,
            "query": query,
            "engine": engine,
            "match_count": len(matches),
            "matches": matches,
            "truncated": len(matches) >= max_results,
        }

    def read_source(
        self,
        source_connector_id: str,
        *,
        path: str,
        line_start: int = 1,
        line_end: int = 240,
    ) -> dict[str, Any]:
        path = _string(path, field="path", maximum=2048)
        source = self.connectors.resolve_source_path(source_connector_id, path)
        if source.stat().st_size > 4 * 1024 * 1024:
            raise PlatformError("APPLICATION_FLOW_SOURCE_TOO_LARGE", "单个源码文件超过流程分析读取上限。", status_code=422)
        line_start = max(1, int(line_start))
        line_end = max(line_start, min(int(line_end), line_start + 399))
        lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
        excerpt = [
            {"line": number, "text": lines[number - 1]}
            for number in range(line_start, min(line_end, len(lines)) + 1)
        ]
        return {
            "schema_version": "application_flow_source_excerpt/v1",
            "source_connector_id": source_connector_id,
            "path": path.replace("\\", "/"),
            "line_start": line_start,
            "line_end": excerpt[-1]["line"] if excerpt else line_start,
            "total_lines": len(lines),
            "excerpt": excerpt,
        }

    @staticmethod
    def _trace_boundary_role(call_name: str) -> str:
        lowered = call_name.casefold()
        if any(token in lowered for token in ("finish", ".end", "_end")):
            return "end"
        if any(token in lowered for token in ("start", ".begin", "_begin")):
            return "start"
        return "scope"

    @staticmethod
    def _trace_invocation_source(lines: list[str], line_index: int, call_name: str) -> str:
        """Extract one bounded call expression so adjacent code cannot pollute labels."""

        first_line = lines[line_index]
        start_column = first_line.find(call_name)
        fragments = [first_line[max(0, start_column):], *lines[line_index + 1:line_index + 10]]
        source = "\n".join(fragments)
        opened = False
        depth = 0
        quote = ""
        escaped = False
        for index, character in enumerate(source):
            if quote:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == quote:
                    quote = ""
                continue
            if character in {'"', "'", "`"}:
                quote = character
                continue
            if character == "(":
                opened = True
                depth += 1
            elif character == ")" and opened:
                depth -= 1
                if depth <= 0:
                    return source[:index + 1]
        return first_line[max(0, start_column):]

    def inspect_trace_instrumentation(
        self,
        source_connector_id: str,
        *,
        path: str,
        line: int = 1,
        symbol: str = "",
        radius: int = 320,
        max_results: int = 40,
    ) -> dict[str, Any]:
        """Reverse-discover direct and module-wrapped HiTrace calls near one implementation."""

        path = _string(path, field="path", maximum=2048).replace("\\", "/")
        source = self.connectors.resolve_source_path(source_connector_id, path)
        if source.stat().st_size > 4 * 1024 * 1024:
            raise PlatformError("APPLICATION_FLOW_SOURCE_TOO_LARGE", "HiTrace 反向检索文件超过校验上限。", status_code=422)
        lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
        bounded_line = max(1, min(int(line or 1), max(1, len(lines))))
        bounded_radius = max(40, min(int(radius), 1200))
        bounded_limit = max(1, min(int(max_results), 100))
        start_index = max(0, bounded_line - 1 - bounded_radius)
        end_index = min(len(lines), bounded_line - 1 + bounded_radius + 1)
        candidates: list[dict[str, Any]] = []
        seen: set[tuple[int, str]] = set()

        def wrapper_chain(call_name: str) -> list[dict[str, Any]]:
            simple_name = re.split(r"::|\.", call_name)[-1]
            snippets = self._macro_definition_snippets(source_connector_id, simple_name, {})
            chain: list[dict[str, Any]] = []
            nested_wrapper_names: list[str] = []
            for snippet in snippets[:4]:
                nested_calls = [matched.group("name") for matched in _TRACE_CALL_RE.finditer(snippet)]
                base_calls = [name for name in nested_calls if any(token in name for token in _TRACE_BASE_CALL_TOKENS)]
                nested_wrapper_names.extend(
                    name for name in nested_calls
                    if name != call_name and name not in base_calls
                )
                nested_wrapper_names.extend(
                    name for name in (matched.group("name") for matched in _TRACE_SYMBOL_RE.finditer(snippet))
                    if re.split(r"::", name)[-1] != simple_name
                    and not any(token in name for token in _TRACE_BASE_CALL_TOKENS)
                )
                chain.append({
                    "kind": "macro_definition",
                    "symbol": simple_name,
                    "definition": snippet[:2400],
                    "nested_trace_calls": nested_calls[:8],
                    "base_hitrace_calls": base_calls[:8],
                })
            function_symbols = list(dict.fromkeys(nested_wrapper_names)) if snippets else [call_name]
            for wrapper_symbol in function_symbols[:5]:
                if any(token in wrapper_symbol for token in _TRACE_BASE_CALL_TOKENS):
                    continue
                for candidate in self._trace_wrapper_definition_candidates(source_connector_id, wrapper_symbol):
                    identity = (candidate.get("path"), candidate.get("line"), candidate.get("symbol"))
                    if any((item.get("path"), item.get("line"), item.get("symbol")) == identity for item in chain):
                        continue
                    chain.append(candidate)
                    if len(chain) >= 8:
                        return chain
            return chain

        for line_index in range(start_index, end_index):
            raw_line = lines[line_index]
            matches: list[tuple[str, str]] = [
                (matched.group("name"), "call") for matched in _TRACE_CALL_RE.finditer(raw_line)
            ]
            matches.extend((matched.group("name"), "bare_macro") for matched in _TRACE_BARE_MACRO_RE.finditer(raw_line))
            for call_name, syntax_kind in matches:
                lowered_call_name = call_name.casefold()
                if (
                    lowered_call_name in {"begin", "end"}
                    or lowered_call_name.endswith((".begin", ".end"))
                ) and "trace" not in lowered_call_name:
                    continue
                key = (line_index + 1, call_name)
                if key in seen:
                    continue
                seen.add(key)
                direct = any(token in call_name for token in _TRACE_BASE_CALL_TOKENS)
                arkts_wrapper = "." in call_name
                chain = [] if direct or arkts_wrapper else wrapper_chain(call_name)
                invocation_source = self._trace_invocation_source(lines, line_index, call_name)
                wrapper_source = "\n".join(str(item.get("definition") or "") for item in chain)
                invocation_literals = _source_string_literals(invocation_source)
                wrapper_literals = _source_string_literals(wrapper_source)
                literals = invocation_literals or wrapper_literals
                label_kind = "literal" if literals else "dynamic"
                function_source = "\n".join((invocation_source, wrapper_source))
                if not invocation_literals and any(
                    token in function_source
                    for token in ("__func__", "__FUNCTION__", "__PRETTY_FUNCTION__")
                ):
                    label_kind = "function_derived"
                elif "${" in invocation_source:
                    label_kind = "static_prefix"
                family = "direct_hitrace_api" if direct else "arkts_trace_wrapper" if arkts_wrapper else "module_trace_wrapper"
                build_condition = "not_observed_in_local_window"
                for condition_line in reversed(lines[max(0, line_index - 24):line_index + 1]):
                    stripped = condition_line.strip()
                    if stripped.startswith(("#if", "#ifdef", "#ifndef")):
                        build_condition = stripped[:500]
                        break
                candidates.append({
                    "path": path,
                    "line": line_index + 1,
                    "symbol": symbol or None,
                    "call": call_name,
                    "syntax_kind": syntax_kind,
                    "boundary_role": self._trace_boundary_role(call_name),
                    "instrumentation_family": family,
                    "event_label_candidates": literals[:12],
                    "event_label_kind": label_kind,
                    "build_condition": build_condition,
                    "wrapper_chain": chain,
                    "wrapper_resolution_status": (
                        "direct_hitrace_api" if direct
                        else "known_arkts_trace_api" if arkts_wrapper
                        else
                        "resolved_to_hitrace_base"
                        if any(item.get("base_hitrace_calls") for item in chain)
                        else "resolved_wrapper_candidate" if chain
                        else "direct_or_unresolved_wrapper"
                    ),
                    "line_text": raw_line.strip()[:1200],
                    "distance_from_implementation_line": abs(line_index + 1 - bounded_line),
                })
        candidates.sort(key=lambda item: (
            int(item["distance_from_implementation_line"]),
            0 if item["instrumentation_family"] == "direct_hitrace_api" else 1,
            int(item["line"]),
        ))
        selected = candidates[:bounded_limit]
        families = sorted({str(item["instrumentation_family"]) for item in selected})
        return {
            "schema_version": "application_flow_trace_instrumentation_inspection/v1",
            "source_connector_id": source_connector_id,
            "path": path,
            "implementation": {"line": bounded_line, "symbol": symbol or None},
            "scan_window": {"line_start": start_index + 1, "line_end": end_index, "radius": bounded_radius},
            "candidate_count": len(selected),
            "candidates": selected,
            "instrumentation_families": families,
            "diagnostics": [] if selected else [
                "当前实现窗口未发现直接 HiTrace、ArkTS Trace 调用或名称含 Trace/HiTrace/Meter 的模块包装。",
            ],
            "guidance_zh": (
                "候选仅证明当前源码窗口存在插桩调用。模块包装需要沿 wrapper_chain 或定义继续确认底层 HiTrace、"
                "事件名传递方式和 start/end 配对；同一 RAII scope 包装可同时作为阶段开始与结束边界。"
            ),
        }

    def _validate_evidence(self, source_connector_id: str, evidence: Any, *, field: str) -> dict[str, dict[str, Any]]:
        if not isinstance(evidence, list) or not evidence or len(evidence) > 512:
            raise PlatformError(
                "APPLICATION_FLOW_DISCOVERY_INVALID",
                f"{field} 必须包含 1 到 512 条源码证据。",
                status_code=422,
            )
        by_id: dict[str, dict[str, Any]] = {}
        for index, raw in enumerate(evidence):
            item = _record(raw)
            evidence_id = _string(item.get("id"), field=f"{field}[{index}].id", maximum=80)
            if not _SAFE_ID.fullmatch(evidence_id) or evidence_id in by_id:
                raise PlatformError("APPLICATION_FLOW_DISCOVERY_INVALID", "源码证据 id 非法或重复。", status_code=422, details={"id": evidence_id})
            path = _string(item.get("path"), field=f"{field}[{index}].path", maximum=2048)
            _, _, source, _ = self._resolve_source_reference(source_connector_id, path)
            if source.stat().st_size > 4 * 1024 * 1024:
                raise PlatformError(
                    "APPLICATION_FLOW_SOURCE_TOO_LARGE",
                    "单个源码证据文件超过流程分析校验上限。",
                    status_code=422,
                    details={"id": evidence_id, "path": path},
                )
            total_lines = len(source.read_text(encoding="utf-8", errors="replace").splitlines())
            line_start = item.get("line_start")
            line_end = item.get("line_end")
            if not isinstance(line_start, int) or not isinstance(line_end, int) or line_start < 1 or line_end < line_start or line_end > max(total_lines, 1):
                raise PlatformError(
                    "APPLICATION_FLOW_DISCOVERY_INVALID",
                    "源码证据行号超出当前代码仓文件范围。",
                    status_code=422,
                    details={"id": evidence_id, "path": path, "line_start": line_start, "line_end": line_end, "total_lines": total_lines},
                )
            for required in ("symbol", "evidence_kind", "claim"):
                _string(item.get(required), field=f"{field}[{index}].{required}", maximum=1000)
            by_id[evidence_id] = item
        return by_id

    def _validate_source_trace_anchor(
        self,
        source_connector_id: str,
        anchor: dict[str, Any],
        evidence_by_id: dict[str, dict[str, Any]],
        *,
        field: str,
    ) -> None:
        location = _record(anchor.get("source_location"))
        instrumentation = _record(anchor.get("instrumentation"))
        has_source_details = bool(location) or bool(instrumentation)
        if anchor.get("verification_status") == "observed_in_source" and not has_source_details:
            raise PlatformError(
                "APPLICATION_FLOW_TRACE_ANCHOR_INVALID",
                "observed_in_source 锚点必须提供源码位置和插桩信息。",
                status_code=422,
                details={"field": field, "anchor_id": anchor.get("id")},
            )
        if not has_source_details:
            return
        if set(location) != {"path", "line"} or set(instrumentation) != {"macro", "event_label_match", "build_condition"}:
            raise PlatformError(
                "APPLICATION_FLOW_TRACE_ANCHOR_INVALID",
                "源码 Trace 锚点的 source_location 或 instrumentation 字段不完整。",
                status_code=422,
                details={"field": field, "anchor_id": anchor.get("id")},
            )
        path = _string(location.get("path"), field=f"{field}.source_location.path", maximum=2048).replace("\\", "/")
        line = location.get("line")
        if not isinstance(line, int) or line < 1:
            raise PlatformError("APPLICATION_FLOW_TRACE_ANCHOR_INVALID", "源码 Trace 锚点行号非法。", status_code=422)
        evidence_refs = [str(value) for value in anchor.get("source_evidence_ids", []) if isinstance(value, str)]
        covering_evidence = [
            evidence_by_id[ref]
            for ref in evidence_refs
            if ref in evidence_by_id
            and str(evidence_by_id[ref].get("path") or "").replace("\\", "/") == path
            and int(evidence_by_id[ref].get("line_start") or 0) <= line <= int(evidence_by_id[ref].get("line_end") or 0)
        ]
        if not covering_evidence:
            raise PlatformError(
                "APPLICATION_FLOW_TRACE_ANCHOR_INVALID",
                "源码 Trace 锚点必须位于其引用的源码证据范围内。",
                status_code=422,
                details={"anchor_id": anchor.get("id"), "path": path, "line": line},
            )
        macro = _string(instrumentation.get("macro"), field=f"{field}.instrumentation.macro", maximum=120)
        if not re.fullmatch(r"[A-Za-z_]\w*(?:(?:::|\.)[A-Za-z_]\w*)*", macro):
            raise PlatformError("APPLICATION_FLOW_TRACE_ANCHOR_INVALID", "源码 Trace 锚点调用名称非法。", status_code=422)
        label_match = instrumentation.get("event_label_match")
        if label_match not in _TRACE_EVENT_LABEL_MATCHES:
            raise PlatformError("APPLICATION_FLOW_TRACE_ANCHOR_INVALID", "源码 Trace 锚点匹配级别非法。", status_code=422)
        _string(instrumentation.get("build_condition"), field=f"{field}.instrumentation.build_condition", maximum=1000)

        anchor_connector_id, _, source, _ = self._resolve_source_reference(source_connector_id, path)
        lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
        if line > len(lines):
            raise PlatformError("APPLICATION_FLOW_TRACE_ANCHOR_INVALID", "源码 Trace 锚点行号超出文件范围。", status_code=422)
        evidence_start = min(int(item["line_start"]) for item in covering_evidence)
        evidence_end = max(int(item["line_end"]) for item in covering_evidence)
        window_start = max(evidence_start, line - 4)
        window_end = min(evidence_end, line + 4)
        window = "\n".join(lines[window_start - 1:window_end])
        if not re.search(rf"(?<![A-Za-z0-9_]){re.escape(macro)}(?![A-Za-z0-9_])", window):
            raise PlatformError(
                "APPLICATION_FLOW_TRACE_ANCHOR_INVALID",
                "声明的 HiTrace 宏未出现在锚点源码窗口中。",
                status_code=422,
                details={"anchor_id": anchor.get("id"), "macro": macro, "path": path, "line": line},
            )
        _, label_source_context = self._trace_label_source_context(
            anchor_connector_id,
            source=source,
            line=line,
            macro=macro,
            evidence_by_id=evidence_by_id,
        )
        event_name = _string(anchor.get("name"), field=f"{field}.name", maximum=1000)
        if label_match == "function_derived":
            if not any(
                token in label_source_context
                for token in ("__func__", "__FUNCTION__", "__PRETTY_FUNCTION__")
            ):
                raise PlatformError(
                    "APPLICATION_FLOW_TRACE_ANCHOR_INVALID",
                    "function_derived 锚点的插桩调用或包装宏中没有函数名派生表达式。",
                    status_code=422,
                )
            evidence_symbols = [str(item.get("symbol") or "") for item in covering_evidence]
            selectors: set[str] = set()
            for symbol in evidence_symbols:
                if not symbol:
                    continue
                try:
                    selectors.add(_stable_function_trace_selector(symbol))
                except PlatformError:
                    continue
            if event_name not in selectors:
                raise PlatformError(
                    "APPLICATION_FLOW_TRACE_ANCHOR_INVALID",
                    "function_derived 的 name 必须是稳定函数匹配名，不能复制源码声明或猜测完整签名。",
                    status_code=422,
                    details={"event_name": event_name, "allowed_selectors": sorted(selectors)},
                )
        else:
            literals = _source_string_literals(label_source_context)
            if label_match in {"exact_literal", "wrapper_literal"} and event_name not in literals:
                raise PlatformError(
                    "APPLICATION_FLOW_TRACE_ANCHOR_INVALID",
                    "字面量 HiTrace name 必须来自插桩参数或包装宏中的字符串字面量，不能使用函数声明。",
                    status_code=422,
                    details={
                        "anchor_id": anchor.get("id"),
                        "event_name": event_name,
                        "literal_candidates": literals[:16],
                        "path": path,
                        "line": line,
                    },
                )
            if label_match == "static_prefix" and not any(
                literal and (event_name.startswith(literal) or literal.startswith(event_name))
                for literal in literals
            ):
                raise PlatformError(
                    "APPLICATION_FLOW_TRACE_ANCHOR_INVALID",
                    "static_prefix 必须能追溯到插桩格式串中的静态字符串片段。",
                    status_code=422,
                    details={"event_name": event_name, "literal_candidates": literals[:16]},
                )

    def validate_discovery(self, source_connector_id: str, discovery: dict[str, Any], *, expected_scope: str | None = None) -> dict[str, Any]:
        self._validate_schema("discovery", discovery, code="APPLICATION_FLOW_DISCOVERY_INVALID")
        required_root = {
            "schema_version", "source_connector_id", "source_snapshot_id", "scope",
            "analysis_summary", "flows", "unknowns",
        }
        if set(discovery) != required_root:
            raise PlatformError(
                "APPLICATION_FLOW_DISCOVERY_INVALID",
                "流程发现工件的顶层字段与标准 Schema 不一致。",
                status_code=422,
                details={"required": sorted(required_root), "received": sorted(discovery)},
            )
        connector = self.connectors.get(source_connector_id)
        snapshot_id = str(_record(connector.get("snapshot")).get("snapshot_id") or "")
        if discovery.get("schema_version") != DISCOVERY_SCHEMA_VERSION:
            raise PlatformError("APPLICATION_FLOW_DISCOVERY_INVALID", "流程发现 schema_version 不受支持。", status_code=422)
        if discovery.get("source_connector_id") != source_connector_id or discovery.get("source_snapshot_id") != snapshot_id:
            raise PlatformError("APPLICATION_FLOW_SOURCE_BINDING_MISMATCH", "流程发现结果与当前代码仓快照不一致。", status_code=409)
        scope = _normalized_relative_path(str(discovery.get("scope") or ""))
        if expected_scope is not None and scope != _normalized_relative_path(expected_scope):
            raise PlatformError("APPLICATION_FLOW_SCOPE_MISMATCH", "流程发现结果的 scope 与任务范围不一致。", status_code=409)
        summary = _record(discovery.get("analysis_summary"))
        if set(summary) != {"what", "scope_rationale", "methods"}:
            raise PlatformError("APPLICATION_FLOW_DISCOVERY_INVALID", "analysis_summary 字段不完整。", status_code=422)
        _string(summary.get("what"), field="analysis_summary.what", maximum=4000)
        _string(summary.get("scope_rationale"), field="analysis_summary.scope_rationale", maximum=2000)
        _string_list(summary.get("methods"), field="analysis_summary.methods", maximum=16)
        _string_list(discovery.get("unknowns"), field="unknowns", maximum=64, item_limit=2000)
        flows = discovery.get("flows")
        if not isinstance(flows, list) or not flows or len(flows) > 64:
            raise PlatformError("APPLICATION_FLOW_DISCOVERY_INVALID", "flows 必须包含 1 到 64 个候选流程。", status_code=422)
        flow_ids: set[str] = set()
        orders: list[int] = []
        for index, raw_flow in enumerate(flows):
            flow = _record(raw_flow)
            required_flow = {
                "id", "name", "order", "description", "trigger", "completion", "participants",
                "entry_points", "exit_points", "source_evidence", "trace_anchor_candidates",
                "confidence", "open_questions",
            }
            if set(flow) != required_flow:
                raise PlatformError("APPLICATION_FLOW_DISCOVERY_INVALID", "候选流程字段与标准 Schema 不一致。", status_code=422, details={"flow_index": index})
            flow_id = _string(flow.get("id"), field=f"flows[{index}].id", maximum=80)
            if not _SAFE_ID.fullmatch(flow_id) or flow_id in flow_ids:
                raise PlatformError("APPLICATION_FLOW_DISCOVERY_INVALID", "候选流程 id 非法或重复。", status_code=422, details={"id": flow_id})
            flow_ids.add(flow_id)
            order = flow.get("order")
            if not isinstance(order, int) or order < 1:
                raise PlatformError("APPLICATION_FLOW_DISCOVERY_INVALID", "候选流程 order 必须是从 1 开始的整数。", status_code=422)
            orders.append(order)
            for field in ("name", "description", "trigger", "completion"):
                _string(flow.get(field), field=f"flows[{index}].{field}", maximum=3000)
            participants = _string_list(flow.get("participants"), field=f"flows[{index}].participants", maximum=64)
            if not participants:
                raise PlatformError("APPLICATION_FLOW_DISCOVERY_INVALID", "候选流程至少需要一个参与者。", status_code=422)
            evidence_by_id = self._validate_evidence(source_connector_id, flow.get("source_evidence"), field=f"flows[{index}].source_evidence")
            for point_field in ("entry_points", "exit_points"):
                points = flow.get(point_field)
                if not isinstance(points, list) or not points or len(points) > 64:
                    raise PlatformError("APPLICATION_FLOW_DISCOVERY_INVALID", f"{point_field} 必须包含 1 到 64 项。", status_code=422)
                point_ids: set[str] = set()
                for point_index, raw_point in enumerate(points):
                    point = _record(raw_point)
                    if set(point) != {"id", "name", "symbol", "kind", "source_evidence_ids"}:
                        raise PlatformError("APPLICATION_FLOW_DISCOVERY_INVALID", f"{point_field} 条目字段不完整。", status_code=422)
                    point_id = _string(point.get("id"), field=f"{point_field}[{point_index}].id", maximum=80)
                    if not _SAFE_ID.fullmatch(point_id) or point_id in point_ids:
                        raise PlatformError("APPLICATION_FLOW_DISCOVERY_INVALID", f"{point_field} id 非法或重复。", status_code=422)
                    point_ids.add(point_id)
                    for field in ("name", "symbol", "kind"):
                        _string(point.get(field), field=f"{point_field}[{point_index}].{field}", maximum=1000)
                    refs = _string_list(point.get("source_evidence_ids"), field=f"{point_field}[{point_index}].source_evidence_ids", maximum=24)
                    if not refs or any(ref not in evidence_by_id for ref in refs):
                        raise PlatformError("APPLICATION_FLOW_DISCOVERY_INVALID", f"{point_field} 引用了未知源码证据。", status_code=422)
            anchors = flow.get("trace_anchor_candidates")
            if not isinstance(anchors, list) or len(anchors) > 256:
                raise PlatformError("APPLICATION_FLOW_DISCOVERY_INVALID", "trace_anchor_candidates 必须是不超过 256 项的数组。", status_code=422)
            anchor_ids: set[str] = set()
            for anchor_index, raw_anchor in enumerate(anchors):
                anchor = _record(raw_anchor)
                if not _TRACE_ANCHOR_BASE_FIELDS.issubset(anchor) or not set(anchor).issubset(_TRACE_ANCHOR_BASE_FIELDS | _TRACE_ANCHOR_OPTIONAL_FIELDS):
                    raise PlatformError("APPLICATION_FLOW_DISCOVERY_INVALID", "trace_anchor_candidates 条目字段不完整。", status_code=422)
                anchor_id = _string(anchor.get("id"), field=f"trace_anchor_candidates[{anchor_index}].id", maximum=80)
                if not _SAFE_ID.fullmatch(anchor_id) or anchor_id in anchor_ids:
                    raise PlatformError("APPLICATION_FLOW_DISCOVERY_INVALID", "trace anchor id 非法或重复。", status_code=422)
                anchor_ids.add(anchor_id)
                for field in ("name", "kind", "process_name"):
                    _string(anchor.get(field), field=f"trace_anchor_candidates[{anchor_index}].{field}", maximum=1000)
                refs = _string_list(anchor.get("source_evidence_ids"), field=f"trace_anchor_candidates[{anchor_index}].source_evidence_ids", maximum=24)
                if not refs or any(ref not in evidence_by_id for ref in refs):
                    raise PlatformError("APPLICATION_FLOW_DISCOVERY_INVALID", "trace anchor 引用了未知源码证据。", status_code=422)
                if anchor.get("verification_status") not in {"observed_in_source", "inferred_needs_trace", "user_provided", "unavailable"}:
                    raise PlatformError("APPLICATION_FLOW_DISCOVERY_INVALID", "trace anchor verification_status 非法。", status_code=422)
                self._validate_source_trace_anchor(
                    source_connector_id,
                    anchor,
                    evidence_by_id,
                    field=f"trace_anchor_candidates[{anchor_index}]",
                )
            if flow.get("confidence") not in {"high", "medium", "low"}:
                raise PlatformError("APPLICATION_FLOW_DISCOVERY_INVALID", "候选流程 confidence 非法。", status_code=422)
            _string_list(flow.get("open_questions"), field=f"flows[{index}].open_questions", maximum=32, item_limit=1600)
        if sorted(orders) != list(range(1, len(flows) + 1)):
            raise PlatformError("APPLICATION_FLOW_DISCOVERY_INVALID", "候选流程 order 必须从 1 连续递增。", status_code=422)
        return discovery

    def save_discovery(
        self,
        source_connector_id: str,
        discovery: dict[str, Any],
        *,
        subpath: str = "",
        additional_input_artifacts: list[str] | None = None,
        analysis_context: dict[str, Any] | None = None,
        application_flow_analysis_id: str = "",
    ) -> dict[str, Any]:
        connector, _, relative_scope = self._scope(source_connector_id, subpath)
        validated = self.validate_discovery(source_connector_id, discovery, expected_scope=relative_scope)
        normalized_context = deepcopy(analysis_context) if isinstance(analysis_context, dict) else {}
        contextual_analysis_id = _flow_analysis_id(
            normalized_context.get("application_flow_analysis_id"),
            allow_empty=True,
        )
        requested_analysis_id = _flow_analysis_id(application_flow_analysis_id, allow_empty=True)
        if requested_analysis_id and contextual_analysis_id and requested_analysis_id != contextual_analysis_id:
            raise PlatformError(
                "APPLICATION_FLOW_ANALYSIS_SCOPE_MISMATCH",
                "应用流程上下文与当前独立分析不一致。",
                status_code=409,
            )
        active_analysis_id = requested_analysis_id or contextual_analysis_id or self._new_flow_analysis_id()
        normalized_context["application_flow_analysis_id"] = active_analysis_id
        flow_count = len(validated["flows"])
        evidence_count = sum(len(item["source_evidence"]) for item in validated["flows"])
        input_artifacts = [str(connector["snapshot_artifact_id"])] if connector.get("snapshot_artifact_id") else []
        for artifact_id in additional_input_artifacts or []:
            if artifact_id and artifact_id not in input_artifacts:
                input_artifacts.append(artifact_id)
        artifact = self.artifact_store.write(
            DISCOVERY_ARTIFACT_TYPE,
            {
                "application_flow_analysis_id": active_analysis_id,
                "discovery": validated,
                "quality": {
                    "status": "pass",
                    "flow_count": flow_count,
                    "source_evidence_count": evidence_count,
                    "contract_validation": "passed",
                },
                "schema_ref": self.schema_metadata["discovery"],
                "analysis_context": normalized_context,
            },
            source_connector_id=source_connector_id,
            source_binding=self._binding(connector),
            producer={"name": "application-execution-flow-reconstruction", "version": "1.0.0"},
            input_artifacts=input_artifacts,
            schema_version="1.0",
        )
        return {"status": "succeeded", "artifact": artifact, "summary_zh": f"已保存 {flow_count} 个候选应用流程和 {evidence_count} 条源码证据。"}

    def save_candidate_catalog(
        self,
        source_connector_id: str,
        *,
        summary_zh: str,
        candidates: list[dict[str, Any]],
        subpath: str = "",
        unknowns: list[str] | None = None,
        analysis_goal: str = "",
        code_graph_artifact_id: str = "",
        source_scopes: list[dict[str, Any]] | None = None,
        parent_discovery_artifact_id: str = "",
        refinement_request: str = "",
        application_flow_analysis_id: str = "",
        trace_analysis_artifact_id: str = "",
    ) -> dict[str, Any]:
        """Expand a small phase-one catalog into the strict discovery contract.

        Flow discovery should feel like a coding-agent conversation, not a JSON
        authoring exercise.  The agent supplies only semantic labels and one
        verified entry/exit location per candidate; the platform owns IDs,
        evidence wiring, snapshot binding, and the verbose storage schema.
        """

        connector, _, relative_scope = self._scope(source_connector_id, subpath)
        normalized_source_scopes = self._normalize_source_scopes(
            source_connector_id,
            subpath=relative_scope,
            source_scopes=source_scopes,
            code_graph_artifact_id=code_graph_artifact_id,
        )
        scope_bindings: dict[str, tuple[dict[str, Any], Path, str]] = {}
        for source_scope in normalized_source_scopes:
            connector_id = str(source_scope["source_connector_id"])
            scope_bindings[connector_id] = self._scope(connector_id, str(source_scope.get("subpath") or ""))
        summary = _string(summary_zh, field="summary_zh", maximum=4000)
        goal = str(analysis_goal or "").strip()[:4000]
        additional_input_artifacts: list[str] = []
        interaction_iteration = 1
        parent_artifact_id = str(parent_discovery_artifact_id or "").strip()
        refinement = str(refinement_request or "").strip()[:4000]
        active_analysis_id = _flow_analysis_id(application_flow_analysis_id, allow_empty=True)
        if parent_artifact_id:
            parent_artifact = self.read_discovery(parent_artifact_id)
            if parent_artifact.get("source_connector_id") != source_connector_id:
                raise PlatformError(
                    "APPLICATION_FLOW_SOURCE_BINDING_MISMATCH",
                    "候选修订引用的发现工件不属于当前代码仓。",
                    status_code=409,
                )
            parent_analysis_id = self._artifact_flow_analysis_id(parent_artifact, DISCOVERY_ARTIFACT_TYPE)
            if active_analysis_id and active_analysis_id != parent_analysis_id:
                raise PlatformError(
                    "APPLICATION_FLOW_ANALYSIS_SCOPE_MISMATCH",
                    "候选修订不能引用另一条独立分析。",
                    status_code=409,
                )
            active_analysis_id = parent_analysis_id
            parent_discovery = _record(parent_artifact.get("discovery"))
            current_snapshot = str(_record(connector.get("snapshot")).get("snapshot_id") or "")
            if (
                parent_discovery.get("source_snapshot_id") != current_snapshot
                or _normalized_relative_path(str(parent_discovery.get("scope") or "")) != relative_scope
            ):
                raise PlatformError(
                    "APPLICATION_FLOW_SOURCE_BINDING_MISMATCH",
                    "候选修订必须基于同一源码快照和分析目录。",
                    status_code=409,
                )
            parent_context = _record(parent_artifact.get("analysis_context"))
            parent_scopes = self._validate_bound_source_scopes(parent_artifact)
            scope_identity = lambda values: [
                (item.get("source_connector_id"), item.get("source_snapshot_id"), item.get("subpath") or "")
                for item in values
            ]
            if scope_identity(parent_scopes) != scope_identity(normalized_source_scopes):
                raise PlatformError(
                    "APPLICATION_FLOW_SOURCE_BINDING_MISMATCH",
                    "候选修订必须沿用父工件的全部代码仓范围与源码快照。",
                    status_code=409,
                )
            interaction_iteration = max(1, int(parent_context.get("interaction_iteration") or 1)) + 1
            additional_input_artifacts.append(parent_artifact_id)
        for source_scope in normalized_source_scopes:
            graph_id = str(source_scope.get("code_graph_artifact_id") or "")
            if not graph_id:
                continue
            graph_artifact = self.artifact_store.read("module_code_graph", graph_id)
            scope_connector_id = str(source_scope["source_connector_id"])
            if graph_artifact.get("source_connector_id") != scope_connector_id:
                raise PlatformError(
                    "APPLICATION_FLOW_CODE_GRAPH_SOURCE_MISMATCH",
                    "候选流程引用的代码知识底图不属于声明的代码仓。",
                    status_code=409,
                )
            graph_snapshot = str(graph_artifact.get("source_snapshot_id") or "")
            current_snapshot = str(source_scope.get("source_snapshot_id") or "")
            if graph_snapshot and graph_snapshot != current_snapshot:
                raise PlatformError(
                    "APPLICATION_FLOW_CODE_GRAPH_SNAPSHOT_MISMATCH",
                    "候选流程引用的代码知识底图与当前源码快照不一致。",
                    status_code=409,
                )
            graph_scope = str(graph_artifact.get("scope") or "")
            if graph_scope != str(source_scope.get("subpath") or ""):
                raise PlatformError(
                    "APPLICATION_FLOW_CODE_GRAPH_SCOPE_MISMATCH",
                    "候选流程引用的代码知识底图与当前分析目录不一致。",
                    status_code=409,
                )
            additional_input_artifacts.append(graph_id)
        for source_scope in normalized_source_scopes:
            scope_connector = self.connectors.get(str(source_scope["source_connector_id"]))
            snapshot_artifact_id = str(scope_connector.get("snapshot_artifact_id") or "")
            if snapshot_artifact_id and snapshot_artifact_id not in additional_input_artifacts:
                additional_input_artifacts.append(snapshot_artifact_id)
        if not isinstance(candidates, list) or not 1 <= len(candidates) <= 32:
            raise PlatformError(
                "APPLICATION_FLOW_CANDIDATE_CATALOG_INVALID",
                "轻量候选目录必须包含 1 到 32 个流程；更大范围应按业务子模块拆分。",
                status_code=422,
            )

        flows: list[dict[str, Any]] = []
        candidate_blueprints: list[dict[str, Any]] = []
        coverage_unknowns: list[str] = []
        source_line_cache: dict[Path, list[str]] = {}
        for index, raw_candidate in enumerate(candidates, 1):
            candidate = _record(raw_candidate)
            name = _string(candidate.get("name"), field=f"candidates[{index - 1}].name", maximum=1000)
            description = _string(candidate.get("description"), field=f"candidates[{index - 1}].description", maximum=3000)
            trigger = _string(candidate.get("trigger"), field=f"candidates[{index - 1}].trigger", maximum=3000)
            completion = _string(candidate.get("completion"), field=f"candidates[{index - 1}].completion", maximum=3000)
            operation_scope = str(candidate.get("operation_scope") or "").strip()
            if operation_scope != "complete_user_operation":
                raise PlatformError(
                    "APPLICATION_FLOW_CANDIDATE_CATALOG_INVALID",
                    "每个候选必须覆盖完整用户操作，而不是单个内部步骤。",
                    status_code=422,
                    details={"candidate_index": index - 1, "operation_scope": operation_scope},
                )
            boundary_rationale = _string(
                candidate.get("boundary_rationale"),
                field=f"candidates[{index - 1}].boundary_rationale",
                maximum=3000,
            )
            raw_user_actions = candidate.get("user_actions")
            if not isinstance(raw_user_actions, list) or not 1 <= len(raw_user_actions) <= 16:
                raise PlatformError(
                    "APPLICATION_FLOW_CANDIDATE_CATALOG_INVALID",
                    "完整流程候选必须包含 1 到 16 个有序用户动作。",
                    status_code=422,
                    details={"candidate_index": index - 1},
                )
            user_actions: list[dict[str, Any]] = []
            action_orders: list[int] = []
            for action_index, raw_action in enumerate(raw_user_actions):
                action = _record(raw_action)
                order = action.get("order")
                if not isinstance(order, int) or order < 1:
                    raise PlatformError(
                        "APPLICATION_FLOW_CANDIDATE_CATALOG_INVALID",
                        "user_actions.order 必须是从 1 开始的整数。",
                        status_code=422,
                        details={"candidate_index": index - 1, "action_index": action_index},
                    )
                action_orders.append(order)
                user_actions.append({
                    "order": order,
                    "name": _string(action.get("name"), field=f"candidates[{index - 1}].user_actions[{action_index}].name", maximum=1000),
                    "description": _string(action.get("description"), field=f"candidates[{index - 1}].user_actions[{action_index}].description", maximum=2000),
                    "observable_outcome": _string(
                        action.get("observable_outcome"),
                        field=f"candidates[{index - 1}].user_actions[{action_index}].observable_outcome",
                        maximum=2000,
                    ),
                })
            if sorted(action_orders) != list(range(1, len(user_actions) + 1)):
                raise PlatformError(
                    "APPLICATION_FLOW_CANDIDATE_CATALOG_INVALID",
                    "user_actions.order 必须从 1 连续递增。",
                    status_code=422,
                    details={"candidate_index": index - 1},
                )
            semantic_outline = _string_list(
                candidate.get("semantic_outline"),
                field=f"candidates[{index - 1}].semantic_outline",
                maximum=24,
                item_limit=1600,
            )
            if not semantic_outline:
                raise PlatformError(
                    "APPLICATION_FLOW_CANDIDATE_CATALOG_INVALID",
                    "完整流程候选至少需要一个高层语义阶段提示。",
                    status_code=422,
                    details={"candidate_index": index - 1},
                )
            participants = _string_list(
                candidate.get("participants") or ["待深入闭合的参与模块"],
                field=f"candidates[{index - 1}].participants",
                maximum=16,
                item_limit=1000,
            )
            confidence = str(candidate.get("confidence") or "medium")
            if confidence not in {"high", "medium", "low"}:
                raise PlatformError(
                    "APPLICATION_FLOW_CANDIDATE_CATALOG_INVALID",
                    "候选流程 confidence 只能为 high、medium 或 low。",
                    status_code=422,
                    details={"candidate_index": index - 1},
                )
            open_questions = _string_list(
                candidate.get("open_questions") or [],
                field=f"candidates[{index - 1}].open_questions",
                maximum=16,
                item_limit=1600,
            )
            raw_coverage = candidate.get("action_coverage")
            coverage_by_order: dict[int, dict[str, Any]] = {}
            if raw_coverage is not None and not isinstance(raw_coverage, list):
                raise PlatformError(
                    "APPLICATION_FLOW_CANDIDATE_CATALOG_INVALID",
                    "action_coverage 必须是逐用户动作的数组。",
                    status_code=422,
                )
            for coverage_index, raw_item in enumerate(raw_coverage or []):
                coverage = _record(raw_item)
                action_order = coverage.get("action_order")
                if not isinstance(action_order, int) or action_order not in action_orders or action_order in coverage_by_order:
                    raise PlatformError(
                        "APPLICATION_FLOW_CANDIDATE_CATALOG_INVALID",
                        "action_coverage 必须逐一引用已存在且不重复的 user_actions.order。",
                        status_code=422,
                        details={"candidate_index": index - 1, "coverage_index": coverage_index},
                    )
                mapping_status = str(coverage.get("mapping_status") or "").strip()
                if mapping_status not in {"mapped", "partial", "unmapped"}:
                    raise PlatformError(
                        "APPLICATION_FLOW_CANDIDATE_CATALOG_INVALID",
                        "action_coverage.mapping_status 只能为 mapped、partial 或 unmapped。",
                        status_code=422,
                    )
                connector_ids = _string_list(
                    coverage.get("source_connector_ids") or [],
                    field=f"candidates[{index - 1}].action_coverage[{coverage_index}].source_connector_ids",
                    maximum=12,
                    item_limit=160,
                )
                unknown_connectors = sorted(set(connector_ids) - set(scope_bindings))
                if unknown_connectors:
                    raise PlatformError(
                        "APPLICATION_FLOW_SOURCE_SCOPE_INVALID",
                        "action_coverage 引用了本次分析范围之外的代码仓。",
                        status_code=422,
                        details={"source_connector_ids": unknown_connectors},
                    )
                evidence_summary = str(coverage.get("evidence_summary") or "").strip()[:2000]
                missing_reason = str(coverage.get("missing_reason") or "").strip()[:2000]
                suspected_modules = _string_list(
                    coverage.get("suspected_modules") or [],
                    field=f"candidates[{index - 1}].action_coverage[{coverage_index}].suspected_modules",
                    maximum=12,
                    item_limit=500,
                )
                if mapping_status == "mapped" and (not connector_ids or not evidence_summary):
                    raise PlatformError(
                        "APPLICATION_FLOW_CANDIDATE_CATALOG_INVALID",
                        "mapped 动作必须给出已关联代码仓和源码证据摘要。",
                        status_code=422,
                    )
                if mapping_status in {"partial", "unmapped"} and not missing_reason:
                    raise PlatformError(
                        "APPLICATION_FLOW_CANDIDATE_CATALOG_INVALID",
                        "未完全关联的动作必须说明缺失原因。",
                        status_code=422,
                    )
                coverage_by_order[action_order] = {
                    "action_order": action_order,
                    "mapping_status": mapping_status,
                    "source_connector_ids": connector_ids,
                    "evidence_summary": evidence_summary,
                    "missing_reason": missing_reason,
                    "suspected_modules": suspected_modules,
                }
            action_coverage = [
                coverage_by_order.get(action["order"], {
                    "action_order": action["order"],
                    "mapping_status": "unassessed",
                    "source_connector_ids": [],
                    "evidence_summary": "",
                    "missing_reason": "本轮候选发现未提交该动作的源码关联判断。",
                    "suspected_modules": [],
                })
                for action in user_actions
            ]

            boundaries: dict[str, dict[str, Any]] = {}
            for boundary_name in ("entry", "exit"):
                boundary = _record(candidate.get(boundary_name))
                boundary_connector_id = str(boundary.get("source_connector_id") or source_connector_id).strip()
                if boundary_connector_id not in scope_bindings:
                    raise PlatformError(
                        "APPLICATION_FLOW_SOURCE_SCOPE_INVALID",
                        "候选流程边界引用了本次分析范围之外的代码仓。",
                        status_code=422,
                        details={"source_connector_id": boundary_connector_id, "boundary": boundary_name},
                    )
                path = _string(
                    boundary.get("path"),
                    field=f"candidates[{index - 1}].{boundary_name}.path",
                    maximum=2048,
                ).replace("\\", "/")
                boundary_connector, boundary_scope_path, boundary_relative_scope = scope_bindings[boundary_connector_id]
                boundary_repository_root = Path(str(boundary_connector["source_root"])).resolve()
                source = self.connectors.resolve_source_path(boundary_connector_id, path)
                if not _is_relative_to(source, boundary_scope_path):
                    raise PlatformError(
                        "APPLICATION_FLOW_SCOPE_MISMATCH",
                        "候选流程边界不在本次分析目录中。",
                        status_code=409,
                        details={"path": path, "scope": boundary_relative_scope, "source_connector_id": boundary_connector_id},
                    )
                if source.stat().st_size > 4 * 1024 * 1024:
                    raise PlatformError(
                        "APPLICATION_FLOW_SOURCE_TOO_LARGE",
                        "候选流程边界文件超过源码校验上限。",
                        status_code=422,
                        details={"path": path},
                    )
                line = boundary.get("line")
                if not isinstance(line, int) or line < 1:
                    raise PlatformError(
                        "APPLICATION_FLOW_CANDIDATE_CATALOG_INVALID",
                        "候选流程入口和出口必须提供有效源码行号。",
                        status_code=422,
                        details={"candidate_index": index - 1, "boundary": boundary_name},
                    )
                symbol = _string(
                    boundary.get("symbol"),
                    field=f"candidates[{index - 1}].{boundary_name}.symbol",
                    maximum=1000,
                )
                source_lines = source_line_cache.get(source)
                if source_lines is None:
                    source_lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
                    source_line_cache[source] = source_lines
                if line > len(source_lines):
                    raise PlatformError(
                        "APPLICATION_FLOW_CANDIDATE_CATALOG_INVALID",
                        "候选流程边界行号超出当前源码文件。",
                        status_code=422,
                        details={"path": path, "line": line, "total_lines": len(source_lines)},
                    )
                symbol_window = "\n".join(source_lines[max(0, line - 2):min(len(source_lines), line + 2)])
                if symbol not in symbol_window:
                    raise PlatformError(
                        "APPLICATION_FLOW_CANDIDATE_CATALOG_INVALID",
                        "候选流程边界符号未出现在声明行附近。",
                        status_code=422,
                        details={"path": path, "line": line, "symbol": symbol},
                    )
                boundaries[boundary_name] = {
                    "path": _source_reference(
                        source_connector_id,
                        boundary_connector_id,
                        self._relative(boundary_repository_root, source),
                    ),
                    "source_connector_id": boundary_connector_id,
                    "line": line,
                    "symbol": symbol,
                    "name": str(boundary.get("name") or symbol).strip()[:1000] or symbol,
                    "kind": str(boundary.get("kind") or ("public_entry" if boundary_name == "entry" else "completion_boundary")).strip()[:1000],
                }

            flow_id = f"flow_{index:02d}"
            entry_evidence_id = f"ev_{flow_id}_entry"
            exit_evidence_id = f"ev_{flow_id}_exit"
            entry = boundaries["entry"]
            exit_boundary = boundaries["exit"]
            flows.append({
                "id": flow_id,
                "name": name,
                "order": index,
                "description": description,
                "trigger": trigger,
                "completion": completion,
                "participants": participants,
                "entry_points": [{
                    "id": f"entry_{flow_id}",
                    "name": entry["name"],
                    "symbol": entry["symbol"],
                    "kind": entry["kind"],
                    "source_evidence_ids": [entry_evidence_id],
                }],
                "exit_points": [{
                    "id": f"exit_{flow_id}",
                    "name": exit_boundary["name"],
                    "symbol": exit_boundary["symbol"],
                    "kind": exit_boundary["kind"],
                    "source_evidence_ids": [exit_evidence_id],
                }],
                "source_evidence": [
                    {
                        "id": entry_evidence_id,
                        "path": entry["path"],
                        "line_start": entry["line"],
                        "line_end": entry["line"],
                        "symbol": entry["symbol"],
                        "evidence_kind": "entry",
                        "claim": f"候选流程入口声明：{entry['symbol']}。",
                    },
                    {
                        "id": exit_evidence_id,
                        "path": exit_boundary["path"],
                        "line_start": exit_boundary["line"],
                        "line_end": exit_boundary["line"],
                        "symbol": exit_boundary["symbol"],
                        "evidence_kind": "exit",
                        "claim": f"候选流程完成边界声明：{exit_boundary['symbol']}。",
                    },
                ],
                "trace_anchor_candidates": [],
                "confidence": confidence,
                "open_questions": open_questions,
            })
            candidate_blueprints.append({
                "flow_id": flow_id,
                "operation_scope": operation_scope,
                "boundary_rationale": boundary_rationale,
                "user_actions": user_actions,
                "action_coverage": action_coverage,
                "coverage_summary": {
                    "action_count": len(action_coverage),
                    "mapped_count": sum(item["mapping_status"] == "mapped" for item in action_coverage),
                    "partial_count": sum(item["mapping_status"] == "partial" for item in action_coverage),
                    "unmapped_count": sum(item["mapping_status"] in {"unmapped", "unassessed"} for item in action_coverage),
                },
                "semantic_outline": semantic_outline,
            })
            action_name_by_order = {item["order"]: item["name"] for item in user_actions}
            for coverage in action_coverage:
                if coverage["mapping_status"] == "mapped":
                    continue
                suspected = "、".join(coverage["suspected_modules"])
                suffix = f"；疑似位于：{suspected}" if suspected else ""
                coverage_unknowns.append(
                    f"候选“{name}”的动作“{action_name_by_order[coverage['action_order']]}”"
                    f"源码关联状态为 {coverage['mapping_status']}：{coverage['missing_reason']}{suffix}"
                )

        normalized_unknowns = _string_list(
            [*(unknowns or []), *coverage_unknowns][:64],
            field="unknowns",
            maximum=64,
            item_limit=2000,
        )
        scope_summary = "；".join(
            f"{item['source_connector_id']}:{item.get('subpath') or '.'}"
            for item in normalized_source_scopes
        )
        discovery = {
            "schema_version": DISCOVERY_SCHEMA_VERSION,
            "source_connector_id": source_connector_id,
            "source_snapshot_id": str(_record(connector.get("snapshot")).get("snapshot_id") or ""),
            "scope": relative_scope,
            "analysis_summary": {
                "what": summary,
                "scope_rationale": f"阶段一围绕用户目标“{goal or '尚未补充'}”，在已登记多代码仓范围 {scope_summary} 内发现并校准候选。",
                "methods": ["多代码仓知识底图检索", "用户目标约束", "逐动作源码覆盖校准", "入口与完成边界小窗口复核", "平台确定性契约展开"],
            },
            "flows": flows,
            "unknowns": normalized_unknowns,
        }
        result = self.save_discovery(
            source_connector_id,
            discovery,
            subpath=relative_scope,
            additional_input_artifacts=additional_input_artifacts,
            analysis_context={
                "application_flow_analysis_id": active_analysis_id or None,
                "analysis_goal": goal,
                "trace_analysis_artifact_id": str(trace_analysis_artifact_id or "").strip() or None,
                "code_graph_artifact_id": code_graph_artifact_id or None,
                "source_scope_contract": "application_flow_source_scope_set/v1",
                "source_scopes": normalized_source_scopes,
                "interaction_state": "candidate_confirmation_required",
                "interaction_iteration": interaction_iteration,
                "parent_discovery_artifact_id": parent_artifact_id or None,
                "refinement_request": refinement or None,
                "candidate_blueprints": candidate_blueprints,
            },
            application_flow_analysis_id=active_analysis_id,
        )
        artifact = _record(result.get("artifact"))
        return {
            "status": "succeeded",
            "artifact": {
                "artifact_id": artifact.get("artifact_id"),
                "artifact_type": artifact.get("artifact_type"),
                "created_at": artifact.get("created_at"),
                "application_flow_analysis_id": artifact.get("application_flow_analysis_id"),
                "quality": artifact.get("quality"),
            },
            "catalog_contract": "application_flow_candidate_catalog/v2",
            "summary_zh": (
                f"已发布 {len(flows)} 个完整用户操作候选，覆盖 {len(normalized_source_scopes)} 个代码仓范围；"
                f"其中 {len(coverage_unknowns)} 个动作仍缺少完整源码关联。分层调用链与 HiTrace 留待选中后闭合。"
            ),
        }

    @staticmethod
    def _constant_refs(value: Any) -> set[str]:
        refs: set[str] = set()
        if isinstance(value, dict):
            if value.get("source") == "constant" and isinstance(value.get("ref"), str):
                refs.add(value["ref"])
            for item in value.values():
                refs.update(ApplicationFlowManager._constant_refs(item))
        elif isinstance(value, list):
            for item in value:
                refs.update(ApplicationFlowManager._constant_refs(item))
        return refs

    @staticmethod
    def normalize_mermaid_authoring(mermaid: str, *, scenario_id: str) -> tuple[str, list[str]]:
        """Remove transport wrappers and make duplicated scenario metadata canonical.

        ``scenario_id`` already arrives as a typed tool argument, so requiring an
        Agent to reproduce it in a second, free-form representation adds no
        information.  The visual and stage metadata remain model-authored.
        """

        value = _string(mermaid, field="mermaid", maximum=256 * 1024)
        source_lines = value.splitlines()
        fence_start = next((
            index
            for index, line in enumerate(source_lines)
            if re.fullmatch(r"```(?:mermaid)?", line.strip(), flags=re.IGNORECASE)
        ), None)
        changes: list[str] = []
        if fence_start is not None:
            fence_end = next((
                index
                for index in range(fence_start + 1, len(source_lines))
                if source_lines[index].strip() == "```"
            ), len(source_lines))
            source_lines = source_lines[fence_start + 1:fence_end]
            changes.append("markdown_code_fence_removed")
        normalized: list[str] = []
        scenario_written = False
        for raw_line in source_lines:
            line = raw_line.strip()
            if _SCENARIO_COMMENT_PREFIX.match(line):
                if not scenario_written:
                    canonical = f"%% @scenario {scenario_id} version={SCENARIO_VERSION}"
                    normalized.append(canonical)
                    scenario_written = True
                    if line != canonical:
                        changes.append("scenario_metadata_canonicalized")
                elif "duplicate_scenario_metadata_removed" not in changes:
                    changes.append("duplicate_scenario_metadata_removed")
                continue
            normalized.append(raw_line)
        if not scenario_written:
            normalized.insert(0, f"%% @scenario {scenario_id} version={SCENARIO_VERSION}")
            changes.append("scenario_metadata_added")
        return "\n".join(normalized).strip() + "\n", changes

    @staticmethod
    def _metadata_values(values: Iterable[Any]) -> list[str]:
        result: list[str] = []
        for value in values:
            if isinstance(value, list):
                candidates = value
            else:
                raw = str(value or "").strip()
                if raw.startswith("[") and raw.endswith("]"):
                    try:
                        parsed = json.loads(raw)
                    except json.JSONDecodeError:
                        parsed = None
                    candidates = parsed if isinstance(parsed, list) else re.split(r"[,\s]+", raw[1:-1])
                else:
                    candidates = re.split(r"[,\s]+", raw)
            for candidate in candidates:
                item = str(candidate or "").strip().strip("\"'")
                if item and item not in result:
                    result.append(item)
        return result

    @classmethod
    def _parse_process_group_metadata(cls, body: str, *, line_number: int) -> tuple[str, list[str]]:
        raw = body.strip()
        if raw.startswith("{"):
            parsed = cls._parse_mermaid_json(raw, field=f"process_group@{line_number}")
            group = str(
                parsed.get("id")
                or parsed.get("group")
                or parsed.get("name")
                or parsed.get("process_group")
                or ""
            ).strip()
            processes = cls._metadata_values([
                parsed.get("processes")
                or parsed.get("process_names")
                or parsed.get("process")
                or []
            ])
        else:
            try:
                tokens = shlex.split(raw)
            except ValueError as exc:
                raise PlatformError(
                    "APPLICATION_FLOW_MERMAID_METADATA_INVALID",
                    f"Mermaid 第 {line_number} 行 process_group 引号未闭合。",
                    status_code=422,
                ) from exc
            assignments: dict[str, list[str]] = {}
            positional: list[str] = []
            for token in tokens:
                key, separator, value = token.partition("=")
                if separator and key:
                    assignments.setdefault(key, []).append(value)
                else:
                    positional.append(token)

            process_fields = [
                *assignments.get("processes", []),
                *assignments.get("process_names", []),
                *assignments.get("process", []),
                *assignments.get("process_name", []),
            ]
            explicit_group = next((
                assignments[key][0]
                for key in ("group", "name", "process_group")
                if assignments.get(key)
            ), "")
            if explicit_group:
                group = explicit_group
                process_fields.extend(positional)
            elif process_fields or positional:
                if assignments.get("application"):
                    group = assignments["application"][0]
                    process_fields.extend(positional)
                elif positional:
                    group = positional.pop(0)
                    process_fields.extend(positional)
                elif len(assignments) == 1:
                    group = next(iter(assignments))
                else:
                    group = ""
            elif len(assignments) == 1:
                group, process_fields = next(iter(assignments.items()))
            else:
                group = ""
            processes = cls._metadata_values(process_fields)

        if not _SAFE_ID.fullmatch(group) or not processes:
            raise PlatformError(
                "APPLICATION_FLOW_MERMAID_METADATA_INVALID",
                f"Mermaid 第 {line_number} 行 process_group 无法解析。",
                status_code=422,
                details={
                    "line": f"%% @process_group {body}",
                    "accepted_shape": "%% @process_group <group>=<process_a>,<process_b>",
                },
            )
        return group, processes

    @classmethod
    def _parse_structured_mermaid_metadata(
        cls,
        kind: str,
        body: str,
        *,
        line_number: int,
    ) -> dict[str, Any]:
        if body.lstrip().startswith("{"):
            return cls._parse_mermaid_json(body, field=f"{kind}@{line_number}")
        try:
            tokens = shlex.split(body)
        except ValueError as exc:
            raise PlatformError(
                "APPLICATION_FLOW_MERMAID_METADATA_INVALID",
                f"Mermaid 第 {line_number} 行 {kind} 引号未闭合。",
                status_code=422,
            ) from exc
        assignments: dict[str, str] = {}
        positional: list[str] = []
        for token in tokens:
            key, separator, value = token.partition("=")
            if separator and key:
                assignments[key] = value
            else:
                positional.append(token)

        aliases: dict[str, tuple[str, ...]] = {
            "action": ("id", "action_id", "order", "name", "description"),
            "phase": ("id", "phase_id", "action", "action_id", "order", "name", "description"),
            "stage_hierarchy": ("stage", "stage_id", "action", "action_id", "phase", "phase_id"),
            "activity": ("id", "activity_id", "stage", "stage_id", "order", "name", "description"),
            "activity_relation": ("from", "to", "type", "description"),
            "implementation": (
                "stage", "stage_id", "activity", "activity_id", "order", "symbol", "path", "line",
                "relation", "source_connector_id",
            ),
        }
        unknown = sorted(set(assignments) - set(aliases[kind]))
        if unknown:
            raise PlatformError(
                "APPLICATION_FLOW_MERMAID_METADATA_INVALID",
                f"Mermaid 第 {line_number} 行 {kind} 包含未知字段。",
                status_code=422,
                details={"unknown_fields": unknown, "line": f"%% @{kind} {body}"},
            )

        def first(*names: str) -> str:
            return next((assignments[name] for name in names if assignments.get(name)), "")

        def identifier(*names: str) -> str:
            value = first(*names)
            if not value and positional:
                value = positional.pop(0)
            return value

        if kind == "action":
            value = {
                "id": identifier("id", "action_id"),
                "order": first("order"),
                "name": first("name"),
                "description": first("description"),
            }
        elif kind == "phase":
            value = {
                "id": identifier("id", "phase_id"),
                "action_id": first("action_id", "action"),
                "order": first("order"),
                "name": first("name"),
                "description": first("description"),
            }
        elif kind == "stage_hierarchy":
            value = {
                "stage_id": first("stage_id", "stage"),
                "action_id": first("action_id", "action"),
                "phase_id": first("phase_id", "phase"),
            }
        elif kind == "activity":
            value = {
                "id": identifier("id", "activity_id"),
                "stage_id": first("stage_id", "stage"),
                "order": first("order"),
                "name": first("name"),
                "description": first("description"),
            }
        elif kind == "activity_relation":
            value = {
                "from": first("from"),
                "to": first("to"),
                "type": first("type"),
                **({"description": first("description")} if first("description") else {}),
            }
        else:
            value = {
                "stage_id": first("stage_id", "stage"),
                **({"activity_id": first("activity_id", "activity")} if first("activity_id", "activity") else {}),
                **({"order": first("order")} if first("order") else {}),
                "symbol": first("symbol"),
                "path": first("path"),
                "line": first("line"),
                "relation": first("relation") or "call",
                **({"source_connector_id": first("source_connector_id")} if first("source_connector_id") else {}),
            }
        for field in ("order", "line"):
            if field in value and isinstance(value[field], str) and value[field].isdigit():
                value[field] = int(value[field])
        if kind in {"action", "phase", "activity"}:
            value["name"] = value.get("name") or value.get("id")
            value["description"] = value.get("description") or value.get("name")
        return value

    @staticmethod
    def _parse_mermaid_json(value: str, *, field: str) -> dict[str, Any]:
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise PlatformError(
                "APPLICATION_FLOW_MERMAID_METADATA_INVALID",
                f"Mermaid {field} 元数据不是合法 JSON。",
                status_code=422,
                details={"field": field, "line": exc.lineno, "column": exc.colno},
            ) from exc
        if not isinstance(parsed, dict):
            raise PlatformError(
                "APPLICATION_FLOW_MERMAID_METADATA_INVALID",
                f"Mermaid {field} 元数据必须是对象。",
                status_code=422,
                details={"field": field},
            )
        return parsed

    @staticmethod
    def _parse_mermaid_stage_detail(kind: str, body: str, *, stage_id: str) -> dict[str, Any]:
        if body.lstrip().startswith("{"):
            return ApplicationFlowManager._parse_mermaid_json(body, field=f"{stage_id}.{kind}")
        try:
            tokens = shlex.split(body)
        except ValueError as exc:
            raise PlatformError(
                "APPLICATION_FLOW_MERMAID_METADATA_INVALID",
                f"Mermaid stage {stage_id} 的 {kind} 元数据引号未闭合。",
                status_code=422,
            ) from exc
        if not tokens:
            raise PlatformError(
                "APPLICATION_FLOW_MERMAID_METADATA_INVALID",
                f"Mermaid stage {stage_id} 的 {kind} 元数据不能为空。",
                status_code=422,
            )
        if kind == "matcher":
            if tokens[0] in {"unavailable", "source_inferred"} and len(tokens) == 1:
                return {"type": "composition", "steps": [{"op": "trace_boundary_unavailable"}]}
            if tokens[0] == "composition" and len(tokens) == 2:
                operations = [item.strip() for item in tokens[1].split(",") if item.strip()]
                if operations:
                    return {"type": "composition", "steps": [{"op": item} for item in operations]}
            if tokens[0] == "detailed_rule" and len(tokens) == 2:
                return {"type": "detailed_rule", "rule": tokens[1]}
            raise PlatformError(
                "APPLICATION_FLOW_MERMAID_METADATA_INVALID",
                f"Mermaid stage {stage_id} 的 matcher 应使用 unavailable、composition 或 JSON 对象。",
                status_code=422,
            )
        if tokens[0] == "trace_point":
            if len(tokens) < 4:
                raise PlatformError(
                    "APPLICATION_FLOW_MERMAID_METADATA_INVALID",
                    f"Mermaid stage {stage_id} 的 {kind} trace_point 信息不完整。",
                    status_code=422,
                )
            event_name = tokens[1]
            cursor = 2
            name_match = "exact"
            if cursor < len(tokens) and tokens[cursor] in {"exact", "substring_case_insensitive", "regex"}:
                name_match = tokens[cursor]
                cursor += 1
            if cursor < len(tokens) and tokens[cursor] in {"start", "end"}:
                if tokens[cursor] != kind:
                    raise PlatformError(
                        "APPLICATION_FLOW_MERMAID_METADATA_INVALID",
                        f"Mermaid stage {stage_id} 的 {kind} 元数据声明了错误边界。",
                        status_code=422,
                    )
                cursor += 1
            if tokens[cursor:] and len(tokens[cursor:]) == 2 and tokens[cursor] == "in":
                process = {"process_group": tokens[cursor + 1]}
            else:
                raise PlatformError(
                    "APPLICATION_FLOW_MERMAID_METADATA_INVALID",
                    f"Mermaid stage {stage_id} 的 {kind} trace_point 必须以 'in <process_group>' 结束。",
                    status_code=422,
                )
            return {
                "type": "trace_point",
                "name": event_name,
                "name_match": name_match,
                "which": kind,
                "process": process,
            }
        if tokens[0] == "relation" and len(tokens) >= 5:
            relation, direction, endpoint, process_name = tokens[1:5]
            if endpoint not in {"from", "to"}:
                raise PlatformError(
                    "APPLICATION_FLOW_MERMAID_METADATA_INVALID",
                    f"Mermaid stage {stage_id} 的 relation 必须声明 from 或 to。",
                    status_code=422,
                )
            selection = "first"
            for token in tokens[5:]:
                if token.startswith("selection="):
                    selection = token.split("=", 1)[1]
                else:
                    raise PlatformError(
                        "APPLICATION_FLOW_MERMAID_METADATA_INVALID",
                        f"Mermaid stage {stage_id} 的 relation 包含未知参数。",
                        status_code=422,
                    )
            return {
                "type": "relation",
                "relation": relation,
                "direction": direction,
                f"{endpoint}_process": process_name,
                "selection": selection,
            }
        raise PlatformError(
            "APPLICATION_FLOW_MERMAID_METADATA_INVALID",
            f"Mermaid stage {stage_id} 的 {kind} 必须是 trace_point、relation 或 JSON 对象。",
            status_code=422,
        )

    def scenario_from_mermaid(self, mermaid: str, *, default_description: str = "") -> dict[str, Any]:
        """Compile the Mermaid authoring contract into scenario v2 JSON.

        Mermaid is the sole model-authored representation. Fixed boilerplate,
        defaults, and the duplicate scenario document are owned by this parser.
        """

        mermaid = _string(mermaid, field="mermaid", maximum=256 * 1024)
        if _UNSAFE_MERMAID.search(mermaid):
            raise PlatformError("APPLICATION_FLOW_MERMAID_UNSAFE", "Mermaid 包含不允许的初始化、链接或脚本语法。", status_code=422)
        scenario_match: re.Match[str] | None = None
        description = ""
        constants: dict[str, Any] = {}
        process_groups: dict[str, list[str]] = {}
        stages: list[dict[str, Any]] = []
        stage_by_id: dict[str, dict[str, Any]] = {}
        relations: list[dict[str, Any]] = []
        current_stage_id = ""
        for line_number, raw_line in enumerate(mermaid.splitlines(), start=1):
            line = raw_line.strip()
            match = _SCENARIO_COMMENT.match(line)
            if match:
                if scenario_match is not None:
                    raise PlatformError("APPLICATION_FLOW_MERMAID_METADATA_INVALID", "Mermaid 只能声明一个 @scenario。", status_code=422)
                scenario_match = match
                current_stage_id = ""
                continue
            match = _DESCRIPTION_COMMENT.match(line)
            if match:
                description = match.group("description").strip()
                current_stage_id = ""
                continue
            match = _CONSTANT_COMMENT.match(line)
            if match:
                name = match.group("name")
                raw_value = match.group("value").strip()
                try:
                    value = json.loads(raw_value)
                except json.JSONDecodeError:
                    value = raw_value
                constants[name] = value
                current_stage_id = ""
                continue
            match = _PROCESS_GROUP_COMMENT_PREFIX.match(line)
            if match:
                group, processes = self._parse_process_group_metadata(
                    match.group("body"),
                    line_number=line_number,
                )
                destination = process_groups.setdefault(group, [])
                destination.extend(item for item in processes if item not in destination)
                current_stage_id = ""
                continue
            structured_match = _STRUCTURED_METADATA_COMMENT.match(line)
            if structured_match:
                self._parse_structured_mermaid_metadata(
                    structured_match.group("kind"),
                    structured_match.group("body"),
                    line_number=line_number,
                )
                current_stage_id = ""
                continue
            match = _STAGE_COMMENT.match(line)
            if match:
                stage_id = match.group("id")
                if stage_id in stage_by_id:
                    raise PlatformError("APPLICATION_FLOW_MERMAID_METADATA_INVALID", f"Mermaid stage {stage_id} 重复。", status_code=422)
                name = (match.group("name") or "").strip()
                if not name:
                    raise PlatformError("APPLICATION_FLOW_MERMAID_METADATA_INVALID", f"Mermaid stage {stage_id} 缺少 name。", status_code=422)
                stage = {
                    "id": stage_id,
                    "name": name,
                    "process_name": match.group("process"),
                    "order": int(match.group("order")),
                    "optional": match.group("requirement") == "optional",
                }
                stages.append(stage)
                stage_by_id[stage_id] = stage
                current_stage_id = stage_id
                continue
            match = _STAGE_DETAIL_COMMENT.match(line)
            if match:
                if not current_stage_id:
                    raise PlatformError(
                        "APPLICATION_FLOW_MERMAID_METADATA_INVALID",
                        f"Mermaid 第 {line_number} 行 stage 细节没有对应的 @stage。",
                        status_code=422,
                    )
                kind = match.group("kind")
                stage = stage_by_id[current_stage_id]
                if kind in stage:
                    raise PlatformError(
                        "APPLICATION_FLOW_MERMAID_METADATA_INVALID",
                        f"Mermaid stage {current_stage_id} 重复声明 {kind}。",
                        status_code=422,
                    )
                stage[kind] = self._parse_mermaid_stage_detail(kind, match.group("body"), stage_id=current_stage_id)
                continue
            match = _RELATION_COMMENT.match(line)
            if match:
                relations.append(self._parse_mermaid_json(match.group("body"), field="relation"))
                current_stage_id = ""
                continue
            if line.startswith("%% @") or re.match(r"^%%\s+(?:start|end|matcher):", line):
                raise PlatformError(
                    "APPLICATION_FLOW_MERMAID_METADATA_INVALID",
                    f"Mermaid 第 {line_number} 行包含未知或格式错误的流程元数据。",
                    status_code=422,
                    details={"line": raw_line},
                )
        if scenario_match is None:
            raise PlatformError("APPLICATION_FLOW_MERMAID_METADATA_INVALID", "Mermaid 缺少 @scenario 元数据。", status_code=422)
        if scenario_match.group("version") != SCENARIO_VERSION:
            raise PlatformError("APPLICATION_FLOW_MERMAID_METADATA_INVALID", "Mermaid @scenario version 必须为 2.0。", status_code=422)
        if not stages:
            raise PlatformError("APPLICATION_FLOW_MERMAID_METADATA_INVALID", "Mermaid 至少需要一个 @stage。", status_code=422)
        for stage in stages:
            has_pair = "start" in stage or "end" in stage
            has_matcher = "matcher" in stage
            if has_matcher == has_pair or (has_pair and not {"start", "end"}.issubset(stage)):
                raise PlatformError(
                    "APPLICATION_FLOW_MERMAID_METADATA_INVALID",
                    f"Mermaid stage {stage['id']} 必须且只能声明 start/end 或 matcher。",
                    status_code=422,
                )
        if not process_groups:
            stage_processes = sorted({str(item["process_name"]) for item in stages})
            process_groups = {"application": stage_processes}
        else:
            declared_processes = {
                process
                for processes in process_groups.values()
                for process in processes
            }
            missing_processes = [
                str(stage["process_name"])
                for stage in stages
                if str(stage["process_name"]) not in declared_processes
            ]
            if missing_processes:
                fallback_group = "application" if "application" in process_groups else next(iter(process_groups))
                destination = process_groups[fallback_group]
                destination.extend(item for item in missing_processes if item not in destination)
        scenario_id = scenario_match.group("scenario")
        compiled = {
            "version": SCENARIO_VERSION,
            "scenario": scenario_id,
            "description": description or default_description,
            "source_mermaid": f"{scenario_id}.mermaid",
            "constants": constants,
            "process_groups": process_groups,
            "stage_search_window": {"source": "trace"},
            "stages": sorted(stages, key=lambda item: int(item["order"])),
            "relations": relations,
            "relation_defaults": {},
        }
        return self.validate_scenario(compiled, mermaid)

    def stage_hierarchy_from_mermaid(
        self,
        mermaid: str,
        *,
        scenario: dict[str, Any],
        source_connector_id: str,
        scope: str,
        selected_flow: dict[str, Any],
        source_scopes: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Parse hierarchical authoring metadata for the scenario projection."""

        raw_actions: list[dict[str, Any]] = []
        raw_phases: list[dict[str, Any]] = []
        raw_bindings: list[dict[str, Any]] = []
        raw_activities: list[dict[str, Any]] = []
        raw_activity_relations: list[dict[str, Any]] = []
        raw_implementations: list[dict[str, Any]] = []
        destinations = {
            "action": raw_actions,
            "phase": raw_phases,
            "stage_hierarchy": raw_bindings,
            "activity": raw_activities,
            "activity_relation": raw_activity_relations,
            "implementation": raw_implementations,
        }
        for line_number, raw_line in enumerate(mermaid.splitlines(), start=1):
            line = raw_line.strip()
            matched = _STRUCTURED_METADATA_COMMENT.match(line)
            if not matched:
                continue
            field = matched.group("kind")
            value = self._parse_structured_mermaid_metadata(
                field,
                matched.group("body"),
                line_number=line_number,
            )
            value["_line_number"] = line_number
            destinations[field].append(value)

        stage_ids = [str(item.get("id") or "") for item in scenario.get("stages", []) if isinstance(item, dict)]
        if not any((raw_actions, raw_phases, raw_bindings, raw_activities, raw_activity_relations, raw_implementations)):
            occupied_ids = set(stage_ids)
            action_id = "action_01"
            suffix = 1
            while action_id in occupied_ids:
                suffix += 1
                action_id = f"action_{suffix:02d}"
            occupied_ids.add(action_id)
            phases: list[dict[str, Any]] = []
            for index, stage in enumerate(scenario.get("stages", []), start=1):
                if not isinstance(stage, dict):
                    continue
                phase_id = f"phase_{index:02d}"
                suffix = index
                while phase_id in occupied_ids:
                    suffix += 1
                    phase_id = f"phase_{suffix:02d}"
                occupied_ids.add(phase_id)
                phases.append({
                    "id": phase_id,
                    "action_id": action_id,
                    "order": index,
                    "name": str(stage.get("name") or stage.get("id") or f"阶段 {index}"),
                    "description": "旧版流程工件未保存分层作者元数据。",
                    "stage_ids": [str(stage.get("id") or "")],
                })
            return {
                "schema_version": "application_flow_stage_hierarchy/v1",
                "representation": "legacy_flat_projection",
                "actions": [{
                    "id": action_id,
                    "order": 1,
                    "name": str(selected_flow.get("name") or scenario.get("scenario") or "既有流程"),
                    "description": str(selected_flow.get("description") or "由既有有序 Stage 兼容投影。"),
                    "phase_ids": [item["id"] for item in phases],
                }],
                "phases": phases,
                "stage_bindings": [
                    {"stage_id": item["stage_ids"][0], "action_id": action_id, "phase_id": item["id"]}
                    for item in phases
                ],
                "activities": [],
                "activity_relations": [],
                "implementations": [],
                "projection": {
                    "target_contract": "application-flow-scenario/v2",
                    "strategy": "legacy_stage_order",
                    "stage_ids": stage_ids,
                },
                "quality": {
                    "status": "legacy_flat",
                    "action_count": 1,
                    "phase_count": len(phases),
                    "anchorable_stage_count": len(stage_ids),
                    "activity_count": 0,
                    "activity_relation_count": 0,
                    "implementation_count": 0,
                },
            }

        if not raw_actions or not raw_phases or not raw_bindings:
            raise PlatformError(
                "APPLICATION_FLOW_HIERARCHY_INVALID",
                "分层 Mermaid 必须同时声明 @action、@phase 和每个 stage 的 @stage_hierarchy。",
                status_code=422,
            )

        def checked_id(value: Any, *, field: str) -> str:
            identifier = _string(value, field=field, maximum=80)
            if not _SAFE_ID.fullmatch(identifier):
                raise PlatformError(
                    "APPLICATION_FLOW_HIERARCHY_INVALID",
                    f"{field} 必须是小写字母开头的稳定 snake_case ID。",
                    status_code=422,
                    details={"field": field, "value": identifier},
                )
            return identifier

        actions: list[dict[str, Any]] = []
        action_by_id: dict[str, dict[str, Any]] = {}
        for index, raw in enumerate(raw_actions):
            received = set(raw) - {"_line_number"}
            required = {"id", "order", "name", "description"}
            if received != required:
                raise PlatformError(
                    "APPLICATION_FLOW_HIERARCHY_INVALID",
                    "@action 字段必须为 id、order、name、description。",
                    status_code=422,
                    details={"received": sorted(received), "line": raw.get("_line_number")},
                )
            action_id = checked_id(raw.get("id"), field=f"actions[{index}].id")
            if action_id in action_by_id:
                raise PlatformError("APPLICATION_FLOW_HIERARCHY_INVALID", f"action {action_id} 重复。", status_code=422)
            order = raw.get("order")
            if not isinstance(order, int) or order < 1:
                raise PlatformError("APPLICATION_FLOW_HIERARCHY_INVALID", "action.order 必须是正整数。", status_code=422)
            action = {
                "id": action_id,
                "order": order,
                "name": _string(raw.get("name"), field=f"actions[{index}].name", maximum=1000),
                "description": _string(raw.get("description"), field=f"actions[{index}].description", maximum=2000),
                "phase_ids": [],
            }
            actions.append(action)
            action_by_id[action_id] = action
        if sorted(item["order"] for item in actions) != list(range(1, len(actions) + 1)):
            raise PlatformError("APPLICATION_FLOW_HIERARCHY_INVALID", "action.order 必须从 1 连续递增。", status_code=422)

        phases: list[dict[str, Any]] = []
        phase_by_id: dict[str, dict[str, Any]] = {}
        for index, raw in enumerate(raw_phases):
            received = set(raw) - {"_line_number"}
            required = {"id", "action_id", "order", "name", "description"}
            if received != required:
                raise PlatformError(
                    "APPLICATION_FLOW_HIERARCHY_INVALID",
                    "@phase 字段必须为 id、action_id、order、name、description。",
                    status_code=422,
                    details={"received": sorted(received), "line": raw.get("_line_number")},
                )
            phase_id = checked_id(raw.get("id"), field=f"phases[{index}].id")
            action_id = checked_id(raw.get("action_id"), field=f"phases[{index}].action_id")
            if phase_id in phase_by_id or action_id not in action_by_id:
                raise PlatformError(
                    "APPLICATION_FLOW_HIERARCHY_INVALID",
                    "phase ID 重复或引用了未知 action。",
                    status_code=422,
                    details={"phase_id": phase_id, "action_id": action_id},
                )
            order = raw.get("order")
            if not isinstance(order, int) or order < 1:
                raise PlatformError("APPLICATION_FLOW_HIERARCHY_INVALID", "phase.order 必须是正整数。", status_code=422)
            phase = {
                "id": phase_id,
                "action_id": action_id,
                "order": order,
                "name": _string(raw.get("name"), field=f"phases[{index}].name", maximum=1000),
                "description": _string(raw.get("description"), field=f"phases[{index}].description", maximum=2000),
                "stage_ids": [],
            }
            phases.append(phase)
            phase_by_id[phase_id] = phase
            action_by_id[action_id]["phase_ids"].append(phase_id)
        global_phase_orders = sorted(int(item["order"]) for item in phases)
        global_phase_sequence = global_phase_orders == list(range(1, len(phases) + 1))
        phase_orders_by_action = {
            str(action["id"]): sorted(
                int(phase_by_id[phase_id]["order"])
                for phase_id in action["phase_ids"]
            )
            for action in actions
        }
        per_action_phase_sequence = all(
            orders == list(range(1, len(orders) + 1))
            for orders in phase_orders_by_action.values()
        )
        if not (global_phase_sequence or per_action_phase_sequence):
            raise PlatformError(
                "APPLICATION_FLOW_HIERARCHY_INVALID",
                "phase.order 必须全局连续，或在各 action 内分别连续。",
                status_code=422,
                details={"orders": global_phase_orders, "orders_by_action": phase_orders_by_action},
            )
        if any(not item["phase_ids"] for item in actions):
            raise PlatformError("APPLICATION_FLOW_HIERARCHY_INVALID", "每个 action 至少需要一个 phase。", status_code=422)
        for action in actions:
            action["phase_ids"] = sorted(action["phase_ids"], key=lambda phase_id: int(phase_by_id[phase_id]["order"]))

        stage_bindings: list[dict[str, str]] = []
        seen_stage_ids: set[str] = set()
        for index, raw in enumerate(raw_bindings):
            received = set(raw) - {"_line_number"}
            required = {"stage_id", "action_id", "phase_id"}
            if received != required:
                raise PlatformError(
                    "APPLICATION_FLOW_HIERARCHY_INVALID",
                    "@stage_hierarchy 字段必须为 stage_id、action_id、phase_id。",
                    status_code=422,
                    details={"received": sorted(received), "line": raw.get("_line_number")},
                )
            stage_id = checked_id(raw.get("stage_id"), field=f"stage_bindings[{index}].stage_id")
            action_id = checked_id(raw.get("action_id"), field=f"stage_bindings[{index}].action_id")
            phase_id = checked_id(raw.get("phase_id"), field=f"stage_bindings[{index}].phase_id")
            phase = phase_by_id.get(phase_id)
            if stage_id not in stage_ids or stage_id in seen_stage_ids or not phase or phase.get("action_id") != action_id:
                raise PlatformError(
                    "APPLICATION_FLOW_HIERARCHY_INVALID",
                    "stage hierarchy 引用了未知、重复或不一致的 action/phase/stage。",
                    status_code=422,
                    details={"stage_id": stage_id, "action_id": action_id, "phase_id": phase_id},
                )
            seen_stage_ids.add(stage_id)
            phase["stage_ids"].append(stage_id)
            stage_bindings.append({"stage_id": stage_id, "action_id": action_id, "phase_id": phase_id})
        if seen_stage_ids != set(stage_ids) or any(not item["stage_ids"] for item in phases):
            raise PlatformError(
                "APPLICATION_FLOW_HIERARCHY_INVALID",
                "每个 scenario stage 必须绑定且仅绑定一个非空 phase。",
                status_code=422,
                details={"expected_stage_ids": stage_ids, "received_stage_ids": sorted(seen_stage_ids)},
            )
        for phase in phases:
            phase["stage_ids"] = sorted(phase["stage_ids"], key=stage_ids.index)
        stage_bindings.sort(key=lambda item: stage_ids.index(item["stage_id"]))

        activities: list[dict[str, Any]] = []
        activity_by_id: dict[str, dict[str, Any]] = {}
        activity_orders: dict[str, set[int]] = {stage_id: set() for stage_id in stage_ids}
        for index, raw in enumerate(raw_activities):
            received = set(raw) - {"_line_number"}
            required = {"id", "stage_id", "order", "name", "description"}
            optional = {"start", "end"}
            if not required.issubset(received) or not received.issubset(required | optional):
                raise PlatformError(
                    "APPLICATION_FLOW_HIERARCHY_INVALID",
                    "@activity 字段包含 id、stage_id、order、name、description，并可携带 start/end。",
                    status_code=422,
                    details={"received": sorted(received), "line": raw.get("_line_number")},
                )
            activity_id = checked_id(raw.get("id"), field=f"activities[{index}].id")
            stage_id = checked_id(raw.get("stage_id"), field=f"activities[{index}].stage_id")
            order = raw.get("order")
            if activity_id in activity_by_id or stage_id not in activity_orders:
                raise PlatformError(
                    "APPLICATION_FLOW_HIERARCHY_INVALID",
                    "activity ID 重复或引用了未知 stage。",
                    status_code=422,
                    details={"activity_id": activity_id, "stage_id": stage_id},
                )
            if not isinstance(order, int) or order < 1 or order in activity_orders[stage_id]:
                raise PlatformError(
                    "APPLICATION_FLOW_HIERARCHY_INVALID",
                    "同一 stage 内的 activity.order 必须是唯一正整数。",
                    status_code=422,
                    details={"stage_id": stage_id, "order": order},
                )
            activity = {
                "id": activity_id,
                "stage_id": stage_id,
                "order": order,
                "name": _string(raw.get("name"), field=f"activities[{index}].name", maximum=1000),
                "description": _string(raw.get("description"), field=f"activities[{index}].description", maximum=2000),
                **({"start": deepcopy(raw["start"]), "end": deepcopy(raw["end"])} if {"start", "end"}.issubset(raw) else {}),
            }
            if ("start" in raw) != ("end" in raw):
                raise PlatformError(
                    "APPLICATION_FLOW_HIERARCHY_INVALID",
                    "@activity 的直接运行时边界由 start/end 成对表达。",
                    status_code=422,
                    details={"activity_id": activity_id},
                )
            activities.append(activity)
            activity_by_id[activity_id] = activity
            activity_orders[stage_id].add(order)

        activity_relations: list[dict[str, Any]] = []
        seen_activity_relations: set[tuple[str, str, str]] = set()
        for index, raw in enumerate(raw_activity_relations):
            received = set(raw) - {"_line_number"}
            required = {"from", "to", "type"}
            if not required.issubset(received) or not received.issubset(required | {"description"}):
                raise PlatformError(
                    "APPLICATION_FLOW_HIERARCHY_INVALID",
                    "@activity_relation 必须包含 from、to、type，可额外声明 description。",
                    status_code=422,
                    details={"received": sorted(received), "line": raw.get("_line_number")},
                )
            source_id = checked_id(raw.get("from"), field=f"activity_relations[{index}].from")
            target_id = checked_id(raw.get("to"), field=f"activity_relations[{index}].to")
            relation_type = _string(raw.get("type"), field=f"activity_relations[{index}].type", maximum=80)
            relation_key = (source_id, target_id, relation_type)
            if source_id not in activity_by_id or target_id not in activity_by_id or source_id == target_id:
                raise PlatformError(
                    "APPLICATION_FLOW_HIERARCHY_INVALID",
                    "activity relation 引用了未知节点或形成自连接。",
                    status_code=422,
                    details={"from": source_id, "to": target_id},
                )
            if relation_type not in _ACTIVITY_RELATION_TYPES:
                raise PlatformError(
                    "APPLICATION_FLOW_HIERARCHY_INVALID",
                    "activity relation.type 不在支持范围内。",
                    status_code=422,
                    details={"type": relation_type, "allowed": sorted(_ACTIVITY_RELATION_TYPES)},
                )
            if relation_key in seen_activity_relations:
                raise PlatformError(
                    "APPLICATION_FLOW_HIERARCHY_INVALID",
                    "activity relation 重复。",
                    status_code=422,
                    details={"from": source_id, "to": target_id, "type": relation_type},
                )
            seen_activity_relations.add(relation_key)
            activity_relations.append({
                "from": source_id,
                "to": target_id,
                "type": relation_type,
                **(
                    {"description": _string(raw.get("description"), field=f"activity_relations[{index}].description", maximum=1000)}
                    if raw.get("description")
                    else {}
                ),
            })

        normalized_scopes = self._normalize_source_scopes(
            source_connector_id,
            subpath=scope,
            source_scopes=source_scopes,
        )
        scope_bindings = {
            str(item["source_connector_id"]): self._scope(
                str(item["source_connector_id"]),
                str(item.get("subpath") or ""),
            )
            for item in normalized_scopes
        }
        source_line_cache: dict[Path, list[str]] = {}
        implementations: list[dict[str, Any]] = []
        implementation_orders: dict[tuple[str, str], list[int]] = {}
        stage_implementation_counts: dict[str, int] = {stage_id: 0 for stage_id in stage_ids}
        for index, raw in enumerate(raw_implementations):
            received = set(raw) - {"_line_number"}
            required = {"stage_id", "symbol", "path", "line", "relation"}
            if not required.issubset(received) or not received.issubset(required | {"order", "source_connector_id", "activity_id"}):
                raise PlatformError(
                    "APPLICATION_FLOW_HIERARCHY_INVALID",
                    "@implementation 字段必须包含 stage_id、symbol、path、line、relation，可额外声明 order、source_connector_id、activity_id。",
                    status_code=422,
                    details={"received": sorted(received), "line": raw.get("_line_number")},
                )
            stage_id = checked_id(raw.get("stage_id"), field=f"implementations[{index}].stage_id")
            if stage_id not in stage_implementation_counts:
                raise PlatformError("APPLICATION_FLOW_HIERARCHY_INVALID", "implementation 引用了未知 stage。", status_code=422)
            activity_id = ""
            if raw.get("activity_id"):
                activity_id = checked_id(raw.get("activity_id"), field=f"implementations[{index}].activity_id")
                activity = activity_by_id.get(activity_id)
                if not activity or activity.get("stage_id") != stage_id:
                    raise PlatformError(
                        "APPLICATION_FLOW_HIERARCHY_INVALID",
                        "implementation.activity_id 必须引用同一 stage 内的 activity。",
                        status_code=422,
                        details={"stage_id": stage_id, "activity_id": activity_id},
                    )
            order_group = (stage_id, activity_id)
            group_orders = implementation_orders.setdefault(order_group, [])
            order = raw.get("order", len(group_orders) + 1)
            line_number = raw.get("line")
            if not isinstance(order, int) or order < 1 or not isinstance(line_number, int) or line_number < 1:
                raise PlatformError("APPLICATION_FLOW_HIERARCHY_INVALID", "implementation.order/line 必须是正整数。", status_code=422)
            path = _string(raw.get("path"), field=f"implementations[{index}].path", maximum=2048).replace("\\", "/")
            symbol = _string(raw.get("symbol"), field=f"implementations[{index}].symbol", maximum=1000)
            referenced_connector_id, referenced_path = _split_source_reference(source_connector_id, path)
            explicit_connector_id = str(raw.get("source_connector_id") or "").strip()
            implementation_connector_id = explicit_connector_id or referenced_connector_id
            if explicit_connector_id and referenced_connector_id != source_connector_id and explicit_connector_id != referenced_connector_id:
                raise PlatformError(
                    "APPLICATION_FLOW_HIERARCHY_INVALID",
                    "implementation 的 source_connector_id 与 path 命名空间不一致。",
                    status_code=422,
                )
            source: Path | None = None
            if not explicit_connector_id and not path.startswith(_SOURCE_REFERENCE_PREFIX):
                candidates: list[tuple[str, tuple[dict[str, Any], Path, str], Path]] = []
                for candidate_connector_id, candidate_binding in scope_bindings.items():
                    try:
                        candidate_source = self.connectors.resolve_source_path(candidate_connector_id, referenced_path)
                    except PlatformError:
                        continue
                    if _is_relative_to(candidate_source, candidate_binding[1]):
                        candidates.append((candidate_connector_id, candidate_binding, candidate_source))
                if len(candidates) == 1:
                    implementation_connector_id, _, source = candidates[0]
                elif len(candidates) > 1:
                    raise PlatformError(
                        "APPLICATION_FLOW_HIERARCHY_INVALID",
                        "implementation 路径在多个已选代码仓中同时存在，请声明 source_connector_id。",
                        status_code=422,
                        details={
                            "path": path,
                            "candidate_source_connector_ids": [item[0] for item in candidates],
                        },
                    )
            binding = scope_bindings.get(implementation_connector_id)
            if binding is None:
                raise PlatformError(
                    "APPLICATION_FLOW_HIERARCHY_INVALID",
                    "implementation 引用了流程分析范围之外的代码仓。",
                    status_code=422,
                    details={"source_connector_id": implementation_connector_id, "path": path},
                )
            implementation_connector, implementation_scope_path, implementation_scope = binding
            repository_root = Path(str(implementation_connector["source_root"])).resolve()
            if source is None:
                source = self.connectors.resolve_source_path(implementation_connector_id, referenced_path)
            if not _is_relative_to(source, implementation_scope_path) or source.stat().st_size > 4 * 1024 * 1024:
                raise PlatformError(
                    "APPLICATION_FLOW_HIERARCHY_INVALID",
                    "implementation 源码位置不在分析范围内或文件过大。",
                    status_code=422,
                    details={"path": path, "scope": implementation_scope, "source_connector_id": implementation_connector_id},
                )
            source_lines = source_line_cache.get(source)
            if source_lines is None:
                source_lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
                source_line_cache[source] = source_lines
            if line_number > len(source_lines):
                raise PlatformError(
                    "APPLICATION_FLOW_HIERARCHY_INVALID",
                    "implementation 行号超出源码文件。",
                    status_code=422,
                    details={"path": path, "line": line_number, "total_lines": len(source_lines)},
                )
            symbol_window = "\n".join(source_lines[max(0, line_number - 5):min(len(source_lines), line_number + 4)])
            short_symbol = re.split(r"::|\.", symbol)[-1].split("(", 1)[0].strip()
            if symbol not in symbol_window and (not short_symbol or short_symbol not in symbol_window):
                raise PlatformError(
                    "APPLICATION_FLOW_HIERARCHY_INVALID",
                    "implementation 符号未出现在声明行附近。",
                    status_code=422,
                    details={"path": path, "line": line_number, "symbol": symbol},
                )
            group_orders.append(order)
            stage_implementation_counts[stage_id] += 1
            identity_scope = activity_id or "stage"
            implementations.append({
                "id": f"impl_{stage_id}_{identity_scope}_{order:02d}",
                "stage_id": stage_id,
                **({"activity_id": activity_id} if activity_id else {}),
                "order": order,
                "symbol": symbol,
                "path": _source_reference(
                    source_connector_id,
                    implementation_connector_id,
                    self._relative(repository_root, source),
                ),
                "source_connector_id": implementation_connector_id,
                "line": line_number,
                "relation": _string(raw.get("relation"), field=f"implementations[{index}].relation", maximum=200),
                "resolution_status": "observed_in_source",
            })
        for stage_id in stage_ids:
            stage_groups = {
                activity_id: orders
                for (candidate_stage_id, activity_id), orders in implementation_orders.items()
                if candidate_stage_id == stage_id
            }
            stage_orders = [order for orders in stage_groups.values() for order in orders]
            stage_sequence = sorted(stage_orders) == list(range(1, len(stage_orders) + 1))
            activity_sequences = all(
                sorted(orders) == list(range(1, len(orders) + 1))
                for orders in stage_groups.values()
            )
            if stage_orders and not (stage_sequence or activity_sequences):
                raise PlatformError(
                    "APPLICATION_FLOW_HIERARCHY_INVALID",
                    "implementation.order 必须在 stage 内连续，或在各 activity 内分别连续。",
                    status_code=422,
                    details={"stage_id": stage_id, "orders_by_activity": stage_groups},
                )
        unresolved_stage_ids = [stage_id for stage_id, count in stage_implementation_counts.items() if not count]

        stage_activity_counts = {
            stage_id: sum(str(item["stage_id"]) == stage_id for item in activities)
            for stage_id in stage_ids
        }
        activity_implementation_counts = {
            activity_id: sum(str(item.get("activity_id") or "") == activity_id for item in implementations)
            for activity_id in activity_by_id
        }
        stages_without_activities = [
            stage_id for stage_id, count in stage_activity_counts.items() if count == 0
        ]
        activities_without_implementations = [
            activity_id for activity_id, count in activity_implementation_counts.items() if count == 0
        ]
        activities_with_multiple_implementations = [
            {
                "activity_id": activity_id,
                "stage_id": str(activity_by_id[activity_id]["stage_id"]),
                "implementation_count": count,
            }
            for activity_id, count in activity_implementation_counts.items()
            if count > 1
        ]
        if not activities:
            granularity_status = "stage_only"
        elif stages_without_activities or activities_without_implementations:
            granularity_status = "activity_graph_with_source_gaps"
        elif activities_with_multiple_implementations:
            granularity_status = "activity_graph_with_aggregates"
        else:
            granularity_status = "activity_source_resolved"
        granularity_observation = {
            "schema_version": "application_flow_granularity_observation/v1",
            "structural_status": granularity_status,
            "stage_activity_counts": [
                {"stage_id": stage_id, "activity_count": stage_activity_counts[stage_id]}
                for stage_id in stage_ids
            ],
            "stages_without_activities": stages_without_activities,
            "activities_without_implementations": activities_without_implementations,
            "activities_with_multiple_implementations": activities_with_multiple_implementations,
            "review_candidate_count": (
                len(stages_without_activities)
                + len(activities_without_implementations)
                + len(activities_with_multiple_implementations)
            ),
            "interpretation_zh": (
                "这些指标只描述 Mermaid 的结构分辨率。聚合项是语义复核候选，"
                "是否拆分仍由源码职责、异步边界和用户操作边界决定。"
            ),
        }

        return {
            "schema_version": "application_flow_stage_hierarchy/v1",
            "representation": "hierarchical_authoring_projection",
            "actions": sorted(actions, key=lambda item: int(item["order"])),
            "phases": sorted(
                phases,
                key=lambda item: (int(action_by_id[str(item["action_id"])]["order"]), int(item["order"])),
            ),
            "stage_bindings": stage_bindings,
            "activities": sorted(
                activities,
                key=lambda item: (stage_ids.index(str(item["stage_id"])), int(item["order"])),
            ),
            "activity_relations": activity_relations,
            "implementations": sorted(
                implementations,
                key=lambda item: (
                    stage_ids.index(str(item["stage_id"])),
                    int(activity_by_id.get(str(item.get("activity_id") or ""), {}).get("order") or 0),
                    int(item["order"]),
                ),
            ),
            "unresolved_stage_ids": unresolved_stage_ids,
            "granularity_observation": granularity_observation,
            "projection": {
                "target_contract": "application-flow-scenario/v2",
                "strategy": "anchorable_substage_order",
                "stage_ids": stage_ids,
            },
            "quality": {
                "status": "source_bound_hierarchy" if not unresolved_stage_ids else "partial_source_binding",
                "action_count": len(actions),
                "phase_count": len(phases),
                "anchorable_stage_count": len(stage_ids),
                "activity_count": len(activities),
                "activity_relation_count": len(activity_relations),
                "activity_graph_stage_count": len({str(item["stage_id"]) for item in activities}),
                "activity_bound_implementation_count": sum(bool(item.get("activity_id")) for item in implementations),
                "implementation_count": len(implementations),
                "implementation_bound_stage_count": len(stage_ids) - len(unresolved_stage_ids),
                "unresolved_stage_count": len(unresolved_stage_ids),
                "granularity_status": granularity_status,
                "stage_without_activity_count": len(stages_without_activities),
                "activity_without_implementation_count": len(activities_without_implementations),
                "multi_implementation_activity_count": len(activities_with_multiple_implementations),
            },
        }

    @staticmethod
    def project_hierarchy_nodes(
        scenario: dict[str, Any],
        hierarchy: dict[str, Any],
        runtime_trace_alignment: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Add a hierarchical node view while retaining the stage projection."""

        projected = deepcopy(scenario)
        stages = [item for item in projected.get("stages", []) if isinstance(item, dict)]
        stage_by_id = {str(item.get("id") or ""): item for item in stages}
        bindings = {
            str(item.get("stage_id") or ""): item
            for item in hierarchy.get("stage_bindings", [])
            if isinstance(item, dict)
        }
        runtime_matches = {
            str(item.get("node_id") or ""): item
            for item in _record(runtime_trace_alignment).get("node_matches", [])
            if isinstance(item, dict) and str(item.get("node_id") or "")
        }

        def observed_locator(event: Any, edge: str, process_name: str | None) -> dict[str, Any] | None:
            value = _record(event)
            name = str(value.get("event_name") or value.get("name") or "").strip()
            if not name:
                return None
            context = _record(value.get("trace_context"))
            observed_process = str(context.get("process_name") or value.get("process_name") or process_name or "").strip()
            return {
                "type": "trace_point",
                "name": name[:512],
                "name_match": "exact",
                "which": edge,
                **({"process": {"process_name": observed_process[:256]}} if observed_process else {}),
            }

        def runtime_boundaries(
            node_id: str,
            start: Any,
            end: Any,
            process_name: str | None,
            source: str,
        ) -> tuple[Any, Any, str]:
            match = _record(runtime_matches.get(node_id))
            runtime_start = observed_locator(match.get("start_event"), "start", process_name)
            runtime_end = observed_locator(match.get("end_event"), "end", process_name)
            if runtime_start or runtime_end:
                return runtime_start or start, runtime_end or end, "runtime_observed"
            return start, end, source

        actions = sorted(
            [item for item in hierarchy.get("actions", []) if isinstance(item, dict)],
            key=lambda item: int(item.get("order") or 0),
        )
        action_order = {
            str(item.get("id") or ""): int(item.get("order") or 0)
            for item in actions
        }
        phases = sorted(
            [item for item in hierarchy.get("phases", []) if isinstance(item, dict)],
            key=lambda item: (
                action_order.get(str(item.get("action_id") or ""), 0),
                int(item.get("order") or 0),
            ),
        )
        phase_by_id = {
            str(item.get("id") or ""): item
            for item in phases
        }

        def stage_boundary(stage_id: str, edge: str) -> Any:
            stage = _record(stage_by_id.get(stage_id))
            return deepcopy(stage.get(edge)) if edge in stage else None

        activities = sorted(
            [item for item in hierarchy.get("activities", []) if isinstance(item, dict)],
            key=lambda item: (str(item.get("stage_id") or ""), int(item.get("order") or 0)),
        )
        nodes: list[dict[str, Any]] = []
        for action in actions:
            phase_ids = [str(item) for item in action.get("phase_ids", []) if str(item)]
            first_phase = _record(phase_by_id.get(phase_ids[0])) if phase_ids else {}
            last_phase = _record(phase_by_id.get(phase_ids[-1])) if phase_ids else {}
            first_stage_ids = [str(item) for item in first_phase.get("stage_ids", []) if str(item)]
            last_stage_ids = [str(item) for item in last_phase.get("stage_ids", []) if str(item)]
            start = stage_boundary(first_stage_ids[0], "start") if first_stage_ids else None
            end = stage_boundary(last_stage_ids[-1], "end") if last_stage_ids else None
            start, end, source = runtime_boundaries(str(action.get("id") or ""), start, end, None, "derived_from_children")
            nodes.append({
                "id": str(action.get("id") or ""),
                "type": "action",
                "parent_id": None,
                "name": str(action.get("name") or action.get("id") or "用户动作")[:512],
                "description": str(action.get("description") or action.get("name") or "用户动作")[:2000],
                "order": int(action.get("order") or 1),
                "process_name": None,
                "optional": False,
                "boundary_source": source,
                "start": start,
                "end": end,
            })
        for phase in phases:
            stage_ids = [str(item) for item in phase.get("stage_ids", []) if str(item)]
            start = stage_boundary(stage_ids[0], "start") if stage_ids else None
            end = stage_boundary(stage_ids[-1], "end") if stage_ids else None
            start, end, source = runtime_boundaries(str(phase.get("id") or ""), start, end, None, "derived_from_children")
            nodes.append({
                "id": str(phase.get("id") or ""),
                "type": "phase",
                "parent_id": str(phase.get("action_id") or ""),
                "name": str(phase.get("name") or phase.get("id") or "业务阶段")[:512],
                "description": str(phase.get("description") or phase.get("name") or "业务阶段")[:2000],
                "order": int(phase.get("order") or 1),
                "process_name": None,
                "optional": False,
                "boundary_source": source,
                "start": start,
                "end": end,
            })
        for stage in stages:
            stage_id = str(stage.get("id") or "")
            binding = _record(bindings.get(stage_id))
            start = deepcopy(stage.get("start")) if "start" in stage else None
            end = deepcopy(stage.get("end")) if "end" in stage else None
            nodes.append({
                "id": stage_id,
                "type": "stage",
                "parent_id": str(binding.get("phase_id") or "") or None,
                "name": str(stage.get("name") or stage_id)[:512],
                "description": str(stage.get("name") or stage_id)[:2000],
                "order": int(stage.get("order") or 1),
                "process_name": str(stage.get("process_name") or "") or None,
                "optional": bool(stage.get("optional")),
                "boundary_source": "declared" if start or end else "unresolved",
                "start": start,
                "end": end,
                **({"matcher": deepcopy(stage.get("matcher"))} if "matcher" in stage else {}),
            })
        for activity in activities:
            activity_id = str(activity.get("id") or "")
            stage_id = str(activity.get("stage_id") or "")
            stage = _record(stage_by_id.get(stage_id))
            start = deepcopy(activity.get("start")) if "start" in activity else None
            end = deepcopy(activity.get("end")) if "end" in activity else None
            source = "declared" if start or end else "unresolved"
            start, end, source = runtime_boundaries(
                activity_id,
                start,
                end,
                str(stage.get("process_name") or "") or None,
                source,
            )
            nodes.append({
                "id": activity_id,
                "type": "activity",
                "parent_id": stage_id,
                "name": str(activity.get("name") or activity_id)[:512],
                "description": str(activity.get("description") or activity.get("name") or activity_id)[:2000],
                "order": int(activity.get("order") or 1),
                "process_name": str(stage.get("process_name") or "") or None,
                "optional": bool(stage.get("optional")),
                "boundary_source": source,
                "start": start,
                "end": end,
            })
        projected["nodes"] = nodes
        projected["node_relations"] = [
            {
                "from": str(item.get("from") or ""),
                "to": str(item.get("to") or ""),
                "type": str(item.get("type") or ""),
                **({"description": str(item.get("description"))[:1000]} if item.get("description") else {}),
            }
            for item in hierarchy.get("activity_relations", [])
            if isinstance(item, dict)
        ]
        return projected

    @staticmethod
    def _derive_stage_evidence(
        scenario: dict[str, Any],
        selected_flow: dict[str, Any],
        preferred_anchor_ids: dict[str, dict[str, str]] | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        source_evidence_ids = [
            str(item.get("id"))
            for item in selected_flow.get("source_evidence", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        ]
        anchors = [item for item in selected_flow.get("trace_anchor_candidates", []) if isinstance(item, dict)]
        anchors_by_id = {
            str(item.get("id")): item
            for item in anchors
            if isinstance(item.get("id"), str)
        }
        stage_evidence: list[dict[str, Any]] = []
        unknowns: list[str] = []
        for stage in scenario.get("stages", []):
            linked: list[dict[str, str]] = []
            refs: list[str] = []
            stage_id = str(stage.get("id") or "")
            preferred = (preferred_anchor_ids or {}).get(stage_id, {})
            for boundary in ("start", "end"):
                locator = _record(stage.get(boundary))
                if locator.get("type") != "trace_point":
                    continue
                preferred_id = str(preferred.get(boundary) or "")
                if preferred_id:
                    candidate = anchors_by_id.get(preferred_id)
                    candidates = [candidate] if (
                        candidate
                        and candidate.get("name") == locator.get("name")
                        and candidate.get("verification_status") == "observed_in_source"
                        and candidate.get("source_location")
                        and candidate.get("instrumentation")
                        and (not candidate.get("process_name") or candidate.get("process_name") == stage.get("process_name"))
                    ) else []
                else:
                    candidates = [
                        anchor for anchor in anchors
                        if anchor.get("name") == locator.get("name")
                        and anchor.get("verification_status") == "observed_in_source"
                        and anchor.get("source_location")
                        and anchor.get("instrumentation")
                        and (not anchor.get("process_name") or anchor.get("process_name") == stage.get("process_name"))
                    ]
                if len(candidates) != 1:
                    continue
                anchor = candidates[0]
                linked.append({"boundary": boundary, "anchor_id": str(anchor.get("id") or "")})
                for ref in anchor.get("source_evidence_ids", []):
                    if isinstance(ref, str) and ref not in refs:
                        refs.append(ref)
            observed = {item["boundary"] for item in linked} == {"start", "end"}
            if not refs:
                refs = list(source_evidence_ids)
            if observed:
                notes = "Mermaid 边界名称已由工具唯一匹配到聚焦 discovery 中的源码 HiTrace 锚点。"
            else:
                notes = "流程阶段具有源码证据，但 start/end 尚未同时唯一匹配到源码 HiTrace 锚点。"
                unknowns.append(f"stage {stage_id} 的 start/end 仍需源码插桩或真实 Trace 证据闭合。")
            stage_evidence.append({
                "stage_id": stage_id,
                "source_evidence_ids": refs,
                "trace_anchor_status": "observed_in_source" if observed else "inferred_needs_trace",
                "notes": notes,
                "boundary_anchors": linked,
            })
        return stage_evidence, unknowns

    def compile_mermaid(
        self,
        source_connector_id: str,
        *,
        discovery_artifact_id: str,
        flow_id: str,
        mermaid: str,
        preferred_anchor_ids: dict[str, dict[str, str]] | None = None,
        boundary_analysis: list[dict[str, Any]] | None = None,
        additional_input_artifacts: list[str] | None = None,
        generation_method: str = "deterministic_mermaid_compiler/v1",
        parent_scenario_artifact_id: str = "",
        revision_request: str = "",
        runtime_trace_alignment: dict[str, Any] | None = None,
        stage_analysis: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        discovery_artifact = self.read_discovery(discovery_artifact_id)
        if discovery_artifact.get("source_connector_id") != source_connector_id:
            raise PlatformError("APPLICATION_FLOW_SOURCE_BINDING_MISMATCH", "流程发现工件不属于当前代码仓。", status_code=409)
        discovery = _record(discovery_artifact.get("discovery"))
        selected_flow = next(
            (item for item in discovery.get("flows", []) if isinstance(item, dict) and item.get("id") == flow_id),
            None,
        )
        if selected_flow is None:
            raise PlatformError("APPLICATION_FLOW_CANDIDATE_NOT_FOUND", "所选流程不在发现工件中。", status_code=404)
        normalized_mermaid, normalizations = self.normalize_mermaid_authoring(
            mermaid,
            scenario_id=flow_id,
        )
        scenario = self.scenario_from_mermaid(
            normalized_mermaid,
            default_description=str(selected_flow.get("description") or selected_flow.get("name") or flow_id),
        )
        stage_evidence, generated_unknowns = self._derive_stage_evidence(
            scenario,
            selected_flow,
            preferred_anchor_ids,
        )
        runtime_matched_stage_ids = {
            str(item.get("stage_id") or "")
            for item in _record(runtime_trace_alignment).get("stage_matches", [])
            if isinstance(item, dict) and item.get("match_status") == "matched"
        }
        if runtime_matched_stage_ids:
            generated_unknowns = [
                item
                for item in generated_unknowns
                if not any(item.startswith(f"stage {stage_id} ") for stage_id in runtime_matched_stage_ids)
            ]
        unknowns: list[str] = []
        for value in [
            *selected_flow.get("open_questions", []),
            *discovery.get("unknowns", []),
            *generated_unknowns,
        ]:
            if isinstance(value, str) and value.strip() and value.strip() not in unknowns:
                unknowns.append(value.strip())
        result = self.save_scenario(
            source_connector_id,
            discovery_artifact_id=discovery_artifact_id,
            flow_id=flow_id,
            scenario_json=scenario,
            mermaid=normalized_mermaid,
            stage_evidence=stage_evidence,
            unknowns=unknowns[:64],
            generation_method=generation_method,
            boundary_analysis=boundary_analysis,
            additional_input_artifacts=additional_input_artifacts,
            parent_scenario_artifact_id=parent_scenario_artifact_id,
            revision_request=revision_request,
            runtime_trace_alignment=runtime_trace_alignment,
            stage_analysis=stage_analysis,
        )
        result["compiler"] = {
            "schema_version": "application_flow_mermaid_compiler/v2",
            "input_representation": "mermaid_only",
            "generated_representation": "application_flow_scenario/v2",
            "hierarchy_representation": "application_flow_stage_hierarchy/v1",
            "granularity_observation": _record(
                _record(result.get("artifact")).get("stage_hierarchy")
            ).get("granularity_observation", {}),
            "normalizations": normalizations,
        }
        return result

    @staticmethod
    def _trace_asset_id(prefix: str, *parts: object) -> str:
        digest = sha256("\0".join(str(part) for part in parts).encode("utf-8")).hexdigest()[:14]
        return f"{prefix}_trace_{digest}"

    @staticmethod
    def _mermaid_with_trace_bindings(
        mermaid: str,
        scenario: dict[str, Any],
        anchor_by_id: dict[str, dict[str, Any]],
        bindings: dict[str, dict[str, str]],
    ) -> str:
        process_groups = _record(scenario.get("process_groups"))
        stage_by_id = {
            str(item.get("id") or ""): item
            for item in scenario.get("stages", [])
            if isinstance(item, dict)
        }
        complete: dict[str, tuple[str, str, str, str, str]] = {}
        for stage_id, boundary_ids in bindings.items():
            start_anchor = anchor_by_id.get(str(boundary_ids.get("start") or ""))
            end_anchor = anchor_by_id.get(str(boundary_ids.get("end") or ""))
            stage = stage_by_id.get(stage_id)
            if not start_anchor or not end_anchor or not stage:
                continue
            process_name = str(stage.get("process_name") or "")
            groups = [
                str(group)
                for group, processes in process_groups.items()
                if isinstance(processes, list) and process_name in processes
            ]
            if not groups:
                raise PlatformError(
                    "APPLICATION_FLOW_TRACE_ANCHOR_INVALID",
                    "HiTrace 锚点阶段没有可用的 process_group。",
                    status_code=422,
                    details={"stage_id": stage_id, "process_name": process_name},
                )
            start_match = (
                "substring_case_insensitive"
                if _record(start_anchor.get("instrumentation")).get("event_label_match") in {"function_derived", "static_prefix"}
                else "exact"
            )
            end_match = (
                "substring_case_insensitive"
                if _record(end_anchor.get("instrumentation")).get("event_label_match") in {"function_derived", "static_prefix"}
                else "exact"
            )
            complete[stage_id] = (
                str(start_anchor.get("name") or ""),
                start_match,
                str(end_anchor.get("name") or ""),
                end_match,
                groups[0],
            )

        output: list[str] = []
        current_stage = ""
        for raw_line in mermaid.splitlines():
            stage_match = _STAGE_COMMENT.match(raw_line.strip())
            if stage_match:
                current_stage = stage_match.group("id")
                output.append(raw_line)
                replacement = complete.get(current_stage)
                if replacement:
                    start_name, start_match, end_name, end_match, process_group = replacement
                    output.append(
                        f"%%   start: trace_point {json.dumps(start_name, ensure_ascii=False)} {start_match} start in {process_group}"
                    )
                    output.append(
                        f"%%   end: trace_point {json.dumps(end_name, ensure_ascii=False)} {end_match} end in {process_group}"
                    )
                continue
            if current_stage and _STAGE_DETAIL_COMMENT.match(raw_line.strip()):
                if current_stage not in complete:
                    output.append(raw_line)
                continue
            current_stage = ""
            output.append(raw_line)
        trailing_newline = "\n" if mermaid.endswith("\n") else ""
        return "\n".join(output) + trailing_newline

    def enrich_trace_anchors(
        self,
        source_connector_id: str,
        *,
        discovery_artifact_id: str,
        scenario_artifact_id: str,
        flow_id: str,
        stage_results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """Validate compact source-anchor findings and rebuild immutable flow assets.

        The coding agent owns source exploration. This boundary owns IDs, source
        validation, lineage, Mermaid rewriting, and scenario compilation so a
        failed or partial anchor search cannot corrupt the topology artifact.
        """

        if not isinstance(stage_results, list) or len(stage_results) > 128:
            raise PlatformError(
                "APPLICATION_FLOW_TRACE_ANCHOR_INVALID",
                "stage_results 必须是不超过 128 项的数组。",
                status_code=422,
            )
        discovery_artifact = self.read_discovery(discovery_artifact_id)
        scenario_artifact = self.read_scenario(scenario_artifact_id)
        if (
            discovery_artifact.get("source_connector_id") != source_connector_id
            or scenario_artifact.get("source_connector_id") != source_connector_id
        ):
            raise PlatformError("APPLICATION_FLOW_SOURCE_BINDING_MISMATCH", "流程工件不属于当前代码仓。", status_code=409)
        if (
            scenario_artifact.get("discovery_artifact_id") != discovery_artifact_id
            or scenario_artifact.get("flow_id") != flow_id
        ):
            raise PlatformError(
                "APPLICATION_FLOW_SOURCE_BINDING_MISMATCH",
                "源码锚点增强必须基于当前场景所绑定的 discovery 与 flow。",
                status_code=409,
            )

        discovery = deepcopy(_record(discovery_artifact.get("discovery")))
        bound_source_scopes = self._validate_bound_source_scopes(discovery_artifact)
        source_scope_ids = {str(item["source_connector_id"]) for item in bound_source_scopes}
        scenario = _record(scenario_artifact.get("scenario_json"))
        selected_flow = next(
            (item for item in discovery.get("flows", []) if isinstance(item, dict) and item.get("id") == flow_id),
            None,
        )
        if selected_flow is None:
            raise PlatformError("APPLICATION_FLOW_CANDIDATE_NOT_FOUND", "所选流程不在发现工件中。", status_code=404)
        stages = [item for item in scenario.get("stages", []) if isinstance(item, dict)]
        stage_by_id = {str(item.get("id") or ""): item for item in stages}
        if not stage_by_id:
            raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "当前场景没有可增强的 stage。", status_code=422)

        source_evidence = selected_flow.get("source_evidence")
        anchors = selected_flow.get("trace_anchor_candidates")
        if not isinstance(source_evidence, list) or not isinstance(anchors, list):
            raise PlatformError("APPLICATION_FLOW_DISCOVERY_INVALID", "当前 discovery 缺少源码证据容器。", status_code=422)
        evidence_by_id = {
            str(item.get("id")): item
            for item in source_evidence
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        anchor_by_id = {
            str(item.get("id")): item
            for item in anchors
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        bindings: dict[str, dict[str, str]] = {}
        for raw_evidence in scenario_artifact.get("stage_evidence", []):
            evidence = _record(raw_evidence)
            stage_id = str(evidence.get("stage_id") or "")
            for raw_link in evidence.get("boundary_anchors", []):
                link = _record(raw_link)
                boundary = str(link.get("boundary") or "")
                anchor_id = str(link.get("anchor_id") or "")
                if stage_id in stage_by_id and boundary in {"start", "end"} and anchor_id in anchor_by_id:
                    bindings.setdefault(stage_id, {})[boundary] = anchor_id

        submitted: dict[str, dict[str, Any]] = {}
        for index, raw_result in enumerate(stage_results):
            result = _record(raw_result)
            if not set(result).issubset({"stage_id", "start", "end", "notes"}):
                raise PlatformError(
                    "APPLICATION_FLOW_TRACE_ANCHOR_INVALID",
                    "stage_results 条目包含未知字段。",
                    status_code=422,
                    details={"index": index},
                )
            stage_id = _string(result.get("stage_id"), field=f"stage_results[{index}].stage_id", maximum=80)
            if stage_id not in stage_by_id or stage_id in submitted:
                raise PlatformError(
                    "APPLICATION_FLOW_TRACE_ANCHOR_INVALID",
                    "stage_results 引用了未知或重复 stage。",
                    status_code=422,
                    details={"stage_id": stage_id},
                )
            submitted[stage_id] = result

        for stage_id, result in submitted.items():
            stage = stage_by_id[stage_id]
            for boundary in ("start", "end"):
                raw_boundary = result.get(boundary)
                if raw_boundary is None:
                    continue
                item = _record(raw_boundary)
                allowed = {
                    "event_name", "path", "line", "macro", "symbol", "event_label_match",
                    "build_condition", "kind", "source_connector_id",
                }
                required = {"event_name", "path", "line", "macro", "symbol"}
                if not required.issubset(item) or not set(item).issubset(allowed):
                    raise PlatformError(
                        "APPLICATION_FLOW_TRACE_ANCHOR_INVALID",
                        "阶段边界必须提供 event_name、path、line、macro 和 symbol。",
                        status_code=422,
                        details={"stage_id": stage_id, "boundary": boundary},
                    )
                event_name = _string(item.get("event_name"), field=f"{stage_id}.{boundary}.event_name", maximum=1000)
                path = _string(item.get("path"), field=f"{stage_id}.{boundary}.path", maximum=2048).replace("\\", "/")
                referenced_connector_id, referenced_path = _split_source_reference(source_connector_id, path)
                explicit_connector_id = str(item.get("source_connector_id") or "").strip()
                anchor_connector_id = explicit_connector_id or referenced_connector_id
                if explicit_connector_id and referenced_connector_id != source_connector_id and explicit_connector_id != referenced_connector_id:
                    raise PlatformError(
                        "APPLICATION_FLOW_TRACE_ANCHOR_INVALID",
                        "HiTrace 边界的 source_connector_id 与 path 命名空间不一致。",
                        status_code=422,
                    )
                if anchor_connector_id not in source_scope_ids:
                    raise PlatformError(
                        "APPLICATION_FLOW_TRACE_ANCHOR_INVALID",
                        "HiTrace 边界引用了流程分析范围之外的代码仓。",
                        status_code=422,
                        details={"source_connector_id": anchor_connector_id, "path": path},
                    )
                anchor_connector = self.connectors.get(anchor_connector_id)
                repository_root = Path(str(anchor_connector["source_root"])).resolve()
                source = self.connectors.resolve_source_path(anchor_connector_id, referenced_path)
                if source.stat().st_size > 4 * 1024 * 1024:
                    raise PlatformError("APPLICATION_FLOW_SOURCE_TOO_LARGE", "HiTrace 锚点源码文件超过校验上限。", status_code=422)
                lines = source.read_text(encoding="utf-8", errors="replace").splitlines()
                line = item.get("line")
                if not isinstance(line, int) or line < 1 or line > len(lines):
                    raise PlatformError(
                        "APPLICATION_FLOW_TRACE_ANCHOR_INVALID",
                        "HiTrace 锚点行号超出当前源码文件。",
                        status_code=422,
                        details={"stage_id": stage_id, "boundary": boundary, "path": path, "line": line},
                    )
                macro = _string(item.get("macro"), field=f"{stage_id}.{boundary}.macro", maximum=120)
                symbol = _string(item.get("symbol"), field=f"{stage_id}.{boundary}.symbol", maximum=1000)
                label_match = str(item.get("event_label_match") or "exact_literal")
                if label_match not in _TRACE_EVENT_LABEL_MATCHES:
                    raise PlatformError("APPLICATION_FLOW_TRACE_ANCHOR_INVALID", "event_label_match 非法。", status_code=422)
                _, label_source_context = self._trace_label_source_context(
                    anchor_connector_id,
                    source=source,
                    line=line,
                    macro=macro,
                    evidence_by_id=evidence_by_id,
                )
                if any(
                    token in label_source_context
                    for token in ("__func__", "__FUNCTION__", "__PRETTY_FUNCTION__")
                ):
                    # The compiler owns the complete runtime label.  Store only
                    # a stable qualified function substring for Trace matching.
                    label_match = "function_derived"
                    event_name = _stable_function_trace_selector(symbol)
                build_condition = str(item.get("build_condition") or "not_observed_in_local_window").strip()
                if not build_condition:
                    build_condition = "not_observed_in_local_window"
                relative_path = _source_reference(
                    source_connector_id,
                    anchor_connector_id,
                    self._relative(repository_root, source),
                )
                fingerprint = (anchor_connector_id, relative_path, line, event_name, macro, str(stage.get("process_name") or ""))
                evidence_id = self._trace_asset_id("ev", *fingerprint)
                anchor_id = self._trace_asset_id("anchor", *fingerprint)
                evidence = {
                    "id": evidence_id,
                    "path": relative_path,
                    "line_start": max(1, line - 4),
                    "line_end": min(len(lines), line + 4),
                    "symbol": symbol,
                    "evidence_kind": "trace_instrumentation",
                    "claim": (
                        f"stage {stage_id} 的 {boundary} 边界在当前源码快照声明函数派生 HiTrace；"
                        f"场景使用稳定匹配名 {event_name}。"
                        if label_match == "function_derived"
                        else f"stage {stage_id} 的 {boundary} 边界在当前源码快照声明 HiTrace event {event_name}。"
                    ),
                }
                anchor = {
                    "id": anchor_id,
                    "name": event_name,
                    "kind": str(item.get("kind") or "hitrace_literal")[:1000],
                    "process_name": str(stage.get("process_name") or "")[:1000],
                    "source_evidence_ids": [evidence_id],
                    "verification_status": "observed_in_source",
                    "source_location": {"path": relative_path, "line": line},
                    "instrumentation": {
                        "macro": macro,
                        "event_label_match": label_match,
                        "build_condition": build_condition[:1000],
                    },
                }
                evidence_by_id[evidence_id] = evidence
                anchor_by_id[anchor_id] = anchor
                bindings.setdefault(stage_id, {})[boundary] = anchor_id

        selected_flow["source_evidence"] = list(evidence_by_id.values())
        selected_flow["trace_anchor_candidates"] = list(anchor_by_id.values())
        methods = _record(discovery.get("analysis_summary")).get("methods")
        if isinstance(methods, list) and "源码 HiTrace 锚点校验" not in methods and len(methods) < 16:
            methods.append("源码 HiTrace 锚点校验")
        discovery_result = self.save_discovery(
            source_connector_id,
            discovery,
            subpath=str(discovery.get("scope") or ""),
            additional_input_artifacts=[discovery_artifact_id, scenario_artifact_id],
            analysis_context=deepcopy(_record(discovery_artifact.get("analysis_context"))),
            application_flow_analysis_id=self._artifact_flow_analysis_id(
                discovery_artifact,
                DISCOVERY_ARTIFACT_TYPE,
            ),
        )
        enriched_discovery = _record(discovery_result.get("artifact"))

        boundary_analysis: list[dict[str, Any]] = []
        for stage in stages:
            stage_id = str(stage.get("id") or "")
            stage_bindings = bindings.get(stage_id, {})
            analysis: dict[str, Any] = {"stage_id": stage_id}
            found = 0
            for boundary in ("start", "end"):
                anchor = anchor_by_id.get(str(stage_bindings.get(boundary) or ""))
                if anchor:
                    found += 1
                    analysis[boundary] = {
                        "status": "observed_in_source",
                        "anchor_id": anchor.get("id"),
                        "event_name": anchor.get("name"),
                        "source_location": anchor.get("source_location"),
                        "instrumentation": anchor.get("instrumentation"),
                    }
                else:
                    analysis[boundary] = {"status": "unavailable"}
            analysis["status"] = "complete" if found == 2 else "partial" if found else "unavailable"
            submitted_notes = str(_record(submitted.get(stage_id)).get("notes") or "").strip()
            analysis["notes"] = submitted_notes[:2000] or (
                "start/end 均已通过当前源码快照逐行校验。" if found == 2
                else "只找到一个可逐行校验的边界锚点。" if found
                else "当前有界源码搜索未找到可逐行校验的 HiTrace start/end。"
            )
            boundary_analysis.append(analysis)

        enriched_mermaid = self._mermaid_with_trace_bindings(
            str(scenario_artifact.get("mermaid") or ""),
            scenario,
            anchor_by_id,
            bindings,
        )
        result = self.compile_mermaid(
            source_connector_id,
            discovery_artifact_id=str(enriched_discovery.get("artifact_id") or ""),
            flow_id=flow_id,
            mermaid=enriched_mermaid,
            preferred_anchor_ids=bindings,
            boundary_analysis=boundary_analysis,
            additional_input_artifacts=[scenario_artifact_id],
            generation_method="deterministic_mermaid_anchor_enrichment/v1",
            parent_scenario_artifact_id=scenario_artifact_id,
            revision_request="补充并校验 HiTrace start/end 源码锚点。",
        )
        result["discovery_artifact"] = enriched_discovery
        result["enrichment"] = {
            "schema_version": "application_flow_trace_anchor_enrichment/v1",
            "stage_count": len(stages),
            "complete_stage_count": sum(item.get("status") == "complete" for item in boundary_analysis),
            "found_boundary_count": sum(
                _record(item.get(boundary)).get("status") == "observed_in_source"
                for item in boundary_analysis
                for boundary in ("start", "end")
            ),
        }
        result["summary_zh"] = (
            f"已检查 {len(stages)} 个流程阶段的源码插桩，"
            f"{result['enrichment']['complete_stage_count']} 个阶段的 start/end 已由源码闭合，"
            f"共确认 {result['enrichment']['found_boundary_count']} 个 HiTrace 边界。"
        )
        return result

    def validate_scenario(self, scenario: dict[str, Any], mermaid: str) -> dict[str, Any]:
        self._validate_schema("scenario", scenario, code="APPLICATION_FLOW_SCENARIO_INVALID")
        required_root_fields = {
            "version", "scenario", "description", "source_mermaid", "constants",
            "process_groups", "stage_search_window", "stages", "relations", "relation_defaults",
        }
        optional_root_fields = {"nodes", "node_relations"}
        if not required_root_fields.issubset(scenario) or not set(scenario).issubset(required_root_fields | optional_root_fields):
            raise PlatformError(
                "APPLICATION_FLOW_SCENARIO_INVALID",
                "应用流程场景 JSON 使用 v2 标准字段，并可携带 nodes 与 node_relations 层级投影。",
                status_code=422,
                details={"required": sorted(required_root_fields), "received": sorted(scenario)},
            )
        if scenario.get("version") != SCENARIO_VERSION:
            raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "应用流程场景 version 必须为 2.0。", status_code=422)
        scenario_id = _string(scenario.get("scenario"), field="scenario", maximum=120)
        if not _SAFE_ID.fullmatch(scenario_id):
            raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "scenario 必须是稳定的小写语义标识。", status_code=422)
        _string(scenario.get("description"), field="description", maximum=4000)
        source_mermaid = _string(scenario.get("source_mermaid"), field="source_mermaid", maximum=512)
        if Path(source_mermaid).is_absolute() or ".." in Path(source_mermaid).parts or not source_mermaid.endswith((".mermaid", ".mmd")):
            raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "source_mermaid 必须是安全的相对 Mermaid 文件名。", status_code=422)
        constants = _record(scenario.get("constants"))
        process_groups = _record(scenario.get("process_groups"))
        if not process_groups:
            raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "process_groups 不能为空。", status_code=422)
        known_processes: set[str] = set()
        process_group_ids: set[str] = set()
        for group, raw_processes in process_groups.items():
            if not _SAFE_ID.fullmatch(str(group)):
                raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "process_groups 包含非法分组名。", status_code=422)
            process_group_ids.add(str(group))
            processes = _string_list(raw_processes, field=f"process_groups.{group}", maximum=128)
            known_processes.update(processes)
        stages = scenario.get("stages")
        if not isinstance(stages, list) or not stages or len(stages) > 128:
            raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "stages 必须包含 1 到 128 个阶段。", status_code=422)
        stage_ids: set[str] = set()
        stage_by_id: dict[str, dict[str, Any]] = {}
        orders: list[int] = []
        stage_fields = {"id", "name", "process_name", "order", "optional", "time_window", "start", "end", "matcher"}
        for index, raw_stage in enumerate(stages):
            stage = _record(raw_stage)
            if not set(stage).issubset(stage_fields) or not {"id", "name", "process_name", "order", "optional"}.issubset(stage):
                raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "stage 字段与应用流程场景 v2 契约不一致。", status_code=422, details={"stage_index": index})
            stage_id = _string(stage.get("id"), field=f"stages[{index}].id", maximum=80)
            if not _SAFE_ID.fullmatch(stage_id) or stage_id in stage_ids:
                raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "stage id 非法或重复。", status_code=422, details={"id": stage_id})
            stage_ids.add(stage_id)
            stage_by_id[stage_id] = stage
            _string(stage.get("name"), field=f"stages[{index}].name", maximum=512)
            process_name = _string(stage.get("process_name"), field=f"stages[{index}].process_name", maximum=256)
            if process_name not in known_processes:
                raise PlatformError(
                    "APPLICATION_FLOW_SCENARIO_INVALID",
                    "stage process_name 必须在 process_groups 中声明。",
                    status_code=422,
                    details={"stage_id": stage_id, "process_name": process_name},
                )
            order = stage.get("order")
            if not isinstance(order, int) or order < 1:
                raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "stage order 必须是正整数。", status_code=422)
            orders.append(order)
            if not isinstance(stage.get("optional"), bool):
                raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "stage optional 必须是布尔值。", status_code=422)
            has_pair = "start" in stage or "end" in stage
            has_matcher = "matcher" in stage
            if has_matcher == has_pair or (has_pair and not {"start", "end"}.issubset(stage)):
                raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "每个 stage 必须且只能使用 start/end 或 matcher。", status_code=422, details={"id": stage_id})
            if has_pair:
                for boundary in ("start", "end"):
                    locator = _record(stage.get(boundary))
                    locator_type = locator.get("type")
                    allowed = {"type", "name", "name_match", "which", "process"} if locator_type == "trace_point" else {
                        "type", "relation", "direction", "reference", "from_process", "to_process", "pass_through", "selection",
                    }
                    if locator_type not in {"trace_point", "relation"} or not set(locator).issubset(allowed):
                        raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "stage locator 类型或字段非法。", status_code=422, details={"id": stage_id, "boundary": boundary})
                    if locator_type == "trace_point":
                        _string(locator.get("name"), field=f"{stage_id}.{boundary}.name", maximum=512)
                        if locator.get("name_match", "substring_case_insensitive") not in {"substring_case_insensitive", "exact", "regex"}:
                            raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "trace_point name_match 非法。", status_code=422)
                        if locator.get("which", boundary) != boundary:
                            raise PlatformError(
                                "APPLICATION_FLOW_SCENARIO_INVALID",
                                "trace_point which 必须与所在 start/end 边界一致。",
                                status_code=422,
                                details={"stage_id": stage_id, "boundary": boundary},
                            )
                        process_filter = _record(locator.get("process"))
                        if process_filter.get("process_group") and process_filter.get("process_group") not in process_group_ids:
                            raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "trace_point 引用了未知 process_group。", status_code=422)
                    else:
                        if locator.get("relation") not in {"wakeup", "binder_sync", "binder_async"}:
                            raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "relation locator 的 relation 非法。", status_code=422)
                        if locator.get("direction") not in {"incoming", "outgoing"}:
                            raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "relation locator 的 direction 非法。", status_code=422)
                        if not locator.get("from_process") and not locator.get("to_process"):
                            raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "relation locator 缺少对端进程。", status_code=422)
                        if locator.get("selection", "first") not in {"first", "last", "nearest"}:
                            raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "relation locator 的 selection 非法。", status_code=422)
            else:
                matcher = _record(stage.get("matcher"))
                if matcher.get("type") == "detailed_rule":
                    if not set(matcher).issubset({"type", "rule", "params"}):
                        raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "detailed_rule matcher 字段非法。", status_code=422)
                    _string(matcher.get("rule"), field=f"{stage_id}.matcher.rule", maximum=160)
                elif matcher.get("type") == "composition":
                    if set(matcher) != {"type", "steps"} or not isinstance(matcher.get("steps"), list) or not matcher["steps"]:
                        raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "composition matcher 必须包含 steps。", status_code=422)
                    for step in matcher["steps"]:
                        if not isinstance(step, dict) or not isinstance(step.get("op"), str):
                            raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "composition step 必须包含 op。", status_code=422)
                else:
                    raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "matcher type 必须为 detailed_rule 或 composition。", status_code=422)
        if sorted(orders) != list(range(1, len(stages) + 1)):
            raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "stage order 必须从 1 连续递增。", status_code=422)
        nodes = scenario.get("nodes")
        if nodes is not None:
            node_by_id: dict[str, dict[str, Any]] = {}
            for index, raw_node in enumerate(nodes if isinstance(nodes, list) else []):
                node = _record(raw_node)
                node_id = _string(node.get("id"), field=f"nodes[{index}].id", maximum=80)
                if node_id in node_by_id:
                    raise PlatformError(
                        "APPLICATION_FLOW_SCENARIO_INVALID",
                        "层级节点 ID 在当前场景中保持唯一。",
                        status_code=422,
                        details={"node_id": node_id},
                    )
                node_by_id[node_id] = node
            node_type_by_id = {node_id: str(node.get("type") or "") for node_id, node in node_by_id.items()}
            expected_parent_type = {"phase": "action", "stage": "phase", "activity": "stage"}
            for node_id, node in node_by_id.items():
                node_type = node_type_by_id[node_id]
                parent_id = str(node.get("parent_id") or "")
                if node_type == "action":
                    if parent_id:
                        raise PlatformError(
                            "APPLICATION_FLOW_SCENARIO_INVALID",
                            "action 节点位于层级根部。",
                            status_code=422,
                            details={"node_id": node_id},
                        )
                elif node_type in expected_parent_type:
                    if node_type_by_id.get(parent_id) != expected_parent_type[node_type]:
                        raise PlatformError(
                            "APPLICATION_FLOW_SCENARIO_INVALID",
                            "层级节点的 parent_id 与 type 形成 action → phase → stage → activity 关系。",
                            status_code=422,
                            details={"node_id": node_id, "parent_id": parent_id, "type": node_type},
                        )
                for edge in ("start", "end"):
                    boundary = _record(node.get(edge))
                    if boundary.get("type") == "trace_point" and boundary.get("which") != edge:
                        raise PlatformError(
                            "APPLICATION_FLOW_SCENARIO_INVALID",
                            "层级节点的 trace_point which 与 start/end 边缘一致。",
                            status_code=422,
                            details={"node_id": node_id, "edge": edge},
                        )
            projected_stage_ids = {
                node_id for node_id, node_type in node_type_by_id.items() if node_type == "stage"
            }
            if projected_stage_ids != stage_ids:
                raise PlatformError(
                    "APPLICATION_FLOW_SCENARIO_INVALID",
                    "nodes 中的 stage 节点与兼容 stages 投影一一对应。",
                    status_code=422,
                    details={"node_stage_ids": sorted(projected_stage_ids), "stage_ids": sorted(stage_ids)},
                )
            for relation in scenario.get("node_relations", []):
                item = _record(relation)
                if str(item.get("from") or "") not in node_by_id or str(item.get("to") or "") not in node_by_id:
                    raise PlatformError(
                        "APPLICATION_FLOW_SCENARIO_INVALID",
                        "node_relations 的端点引用当前场景中的节点。",
                        status_code=422,
                    )
        for stage in stages:
            for candidate in (stage.get("time_window"), stage.get("start"), stage.get("end")):
                reference_stage = str(_record(_record(candidate).get("reference")).get("stage") or "")
                if reference_stage and reference_stage not in stage_ids:
                    raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "stage 定位表达式引用了未知 stage。", status_code=422, details={"stage_id": stage.get("id"), "reference_stage": reference_stage})
        missing_constants = sorted(self._constant_refs(scenario) - set(constants))
        if missing_constants:
            raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "应用流程场景 JSON 引用了未定义常量。", status_code=422, details={"missing_constants": missing_constants})
        search_window = _record(scenario.get("stage_search_window"))
        if search_window and search_window.get("source") != "trace":
            reference_stage = str(_record(search_window.get("reference")).get("stage") or "")
            if reference_stage and reference_stage not in stage_ids:
                raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "stage_search_window 引用了未知 stage。", status_code=422)
        relations = scenario.get("relations")
        if not isinstance(relations, list) or len(relations) > 256:
            raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "relations 必须是受限数组。", status_code=422)
        for relation in relations:
            item = _record(relation)
            if not {"from", "to", "type"}.issubset(item) or not set(item).issubset({"from", "to", "type", "direction", "binder_type"}):
                raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "relation 字段与应用流程场景 v2 契约不一致。", status_code=422)
            if item.get("type") not in {"wakeup", "binder", "callstack"}:
                raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "relation type 非法。", status_code=422)
            for endpoint in ("from", "to"):
                _string(item.get(endpoint), field=f"relations.{endpoint}", maximum=256)
        if not isinstance(scenario.get("relation_defaults"), dict):
            raise PlatformError("APPLICATION_FLOW_SCENARIO_INVALID", "relation_defaults 必须是对象。", status_code=422)
        mermaid = _string(mermaid, field="mermaid", maximum=256 * 1024)
        if _UNSAFE_MERMAID.search(mermaid):
            raise PlatformError("APPLICATION_FLOW_MERMAID_UNSAFE", "Mermaid 包含不允许的初始化、链接或脚本语法。", status_code=422)
        lines = mermaid.splitlines()
        scenario_comment = next((_SCENARIO_COMMENT.match(line.strip()) for line in lines if _SCENARIO_COMMENT.match(line.strip())), None)
        if scenario_comment is None or scenario_comment.group("scenario") != scenario_id or scenario_comment.group("version") != SCENARIO_VERSION:
            raise PlatformError("APPLICATION_FLOW_MERMAID_INVALID", "Mermaid @scenario 元数据与 JSON 不一致。", status_code=422)
        comments: dict[str, re.Match[str]] = {}
        for line in lines:
            match = _STAGE_COMMENT.match(line.strip())
            if match:
                comments[match.group("id")] = match
        if set(comments) != stage_ids:
            raise PlatformError("APPLICATION_FLOW_MERMAID_INVALID", "Mermaid @stage 与 JSON stages 必须一一对应。", status_code=422, details={"json_stage_ids": sorted(stage_ids), "mermaid_stage_ids": sorted(comments)})
        for stage_id, stage in stage_by_id.items():
            match = comments[stage_id]
            expected_requirement = "optional" if stage["optional"] else "required"
            if int(match.group("order")) != stage["order"] or match.group("process") != stage["process_name"] or match.group("requirement") != expected_requirement:
                raise PlatformError("APPLICATION_FLOW_MERMAID_INVALID", "Mermaid stage 元数据与 JSON 阶段属性不一致。", status_code=422, details={"stage_id": stage_id})
            if match.group("name") and match.group("name") != stage["name"]:
                raise PlatformError("APPLICATION_FLOW_MERMAID_INVALID", "Mermaid stage name 与 JSON 不一致。", status_code=422, details={"stage_id": stage_id})
        if not any(re.match(r"^\s*(?:graph|flowchart)\s+(?:LR|RL|TB|TD|BT)\b", line) for line in lines):
            raise PlatformError("APPLICATION_FLOW_MERMAID_INVALID", "可执行流程 Mermaid 必须包含 graph/flowchart 方向声明。", status_code=422)
        return scenario

    def save_scenario(
        self,
        source_connector_id: str,
        *,
        discovery_artifact_id: str,
        flow_id: str,
        scenario_json: dict[str, Any],
        mermaid: str,
        stage_evidence: list[dict[str, Any]],
        unknowns: list[str],
        generation_method: str = "model_supplied_json_mermaid/v1",
        boundary_analysis: list[dict[str, Any]] | None = None,
        additional_input_artifacts: list[str] | None = None,
        parent_scenario_artifact_id: str = "",
        revision_request: str = "",
        runtime_trace_alignment: dict[str, Any] | None = None,
        stage_analysis: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        connector = self.connectors.get(source_connector_id)
        discovery_artifact = self.read_discovery(discovery_artifact_id)
        if discovery_artifact.get("source_connector_id") != source_connector_id:
            raise PlatformError("APPLICATION_FLOW_SOURCE_BINDING_MISMATCH", "流程发现工件不属于当前代码仓。", status_code=409)
        discovery = _record(discovery_artifact.get("discovery"))
        application_flow_analysis_id = self._artifact_flow_analysis_id(
            discovery_artifact,
            DISCOVERY_ARTIFACT_TYPE,
        )
        bound_source_scopes = self._validate_bound_source_scopes(discovery_artifact)
        current_snapshot = str(_record(connector.get("snapshot")).get("snapshot_id") or "")
        if discovery.get("source_snapshot_id") != current_snapshot:
            raise PlatformError("APPLICATION_FLOW_SOURCE_BINDING_MISMATCH", "代码仓快照已变化，请重新发现流程后再生成。", status_code=409)
        selected_flow = next((item for item in discovery.get("flows", []) if isinstance(item, dict) and item.get("id") == flow_id), None)
        if selected_flow is None:
            raise PlatformError("APPLICATION_FLOW_CANDIDATE_NOT_FOUND", "所选流程不在发现工件中。", status_code=404)
        normalized_parent_id = str(parent_scenario_artifact_id or "").strip()
        parent_scenario: dict[str, Any] | None = None
        if normalized_parent_id:
            parent_scenario = self.read_scenario(normalized_parent_id)
            if (
                parent_scenario.get("source_connector_id") != source_connector_id
                or parent_scenario.get("flow_id") != flow_id
            ):
                raise PlatformError(
                    "APPLICATION_FLOW_PARENT_MISMATCH",
                    "父流程场景与当前代码仓或流程不一致。",
                    status_code=409,
                )
            if self._artifact_flow_analysis_id(parent_scenario, SCENARIO_ARTIFACT_TYPE) != application_flow_analysis_id:
                raise PlatformError(
                    "APPLICATION_FLOW_ANALYSIS_SCOPE_MISMATCH",
                    "父流程场景属于另一条独立分析。",
                    status_code=409,
                )
        existing_revisions = [
            int(item.get("revision") or 1)
            for item in self.artifact_store.list_artifacts(
                SCENARIO_ARTIFACT_TYPE,
                limit=500,
                source_connector_id=source_connector_id,
            )
            if item.get("flow_id") == flow_id
            and self._artifact_flow_analysis_id(item, SCENARIO_ARTIFACT_TYPE) == application_flow_analysis_id
        ]
        # A new root analysis starts from v1. Only artifacts produced inside
        # that root analysis share a monotonic version sequence.
        revision = max(existing_revisions, default=0) + 1
        validated_scenario = self.validate_scenario(scenario_json, mermaid)
        stage_hierarchy = self.stage_hierarchy_from_mermaid(
            mermaid,
            scenario=validated_scenario,
            source_connector_id=source_connector_id,
            scope=str(discovery.get("scope") or ""),
            selected_flow=selected_flow,
            source_scopes=bound_source_scopes,
        )
        validated_scenario = self.project_hierarchy_nodes(
            validated_scenario,
            stage_hierarchy,
            runtime_trace_alignment,
        )
        validated_scenario = self.validate_scenario(validated_scenario, mermaid)
        evidence_ids = {item["id"] for item in selected_flow.get("source_evidence", []) if isinstance(item, dict) and isinstance(item.get("id"), str)}
        anchor_by_id = {
            item["id"]: item
            for item in selected_flow.get("trace_anchor_candidates", [])
            if isinstance(item, dict) and isinstance(item.get("id"), str)
        }
        stage_by_id = {item["id"]: item for item in validated_scenario["stages"]}
        stage_ids = set(stage_by_id)
        if not isinstance(stage_evidence, list) or len(stage_evidence) != len(stage_ids):
            raise PlatformError("APPLICATION_FLOW_STAGE_EVIDENCE_INVALID", "每个 stage 必须有且仅有一条证据校准记录。", status_code=422)
        seen_stages: set[str] = set()
        verified_count = 0
        source_declared_stage_count = 0
        source_declared_anchor_count = 0
        normalized_stage_evidence: list[dict[str, Any]] = []
        for index, raw in enumerate(stage_evidence):
            item = _record(raw)
            required_fields = {"stage_id", "source_evidence_ids", "trace_anchor_status", "notes"}
            if not required_fields.issubset(item) or not set(item).issubset(required_fields | {"boundary_anchors"}):
                raise PlatformError("APPLICATION_FLOW_STAGE_EVIDENCE_INVALID", "stage_evidence 字段不完整。", status_code=422)
            stage_id = _string(item.get("stage_id"), field=f"stage_evidence[{index}].stage_id", maximum=80)
            if stage_id not in stage_ids or stage_id in seen_stages:
                raise PlatformError("APPLICATION_FLOW_STAGE_EVIDENCE_INVALID", "stage_evidence 引用了未知或重复 stage。", status_code=422)
            seen_stages.add(stage_id)
            refs = _string_list(item.get("source_evidence_ids"), field=f"stage_evidence[{index}].source_evidence_ids", maximum=24)
            if not refs or any(ref not in evidence_ids for ref in refs):
                raise PlatformError("APPLICATION_FLOW_STAGE_EVIDENCE_INVALID", "stage_evidence 引用了未知源码证据。", status_code=422)
            status = item.get("trace_anchor_status")
            if status not in {"observed_in_source", "inferred_needs_trace", "user_provided", "unavailable"}:
                raise PlatformError("APPLICATION_FLOW_STAGE_EVIDENCE_INVALID", "trace_anchor_status 非法。", status_code=422)
            raw_links = item.get("boundary_anchors", [])
            if not isinstance(raw_links, list) or len(raw_links) > 2:
                raise PlatformError("APPLICATION_FLOW_STAGE_EVIDENCE_INVALID", "boundary_anchors 必须是不超过两项的数组。", status_code=422)
            linked_boundaries: set[str] = set()
            normalized_links: list[dict[str, str]] = []
            stage = stage_by_id[stage_id]
            for link_index, raw_link in enumerate(raw_links):
                link = _record(raw_link)
                link_fields = {"boundary", "anchor_id", "event_name", "source_location", "instrumentation"}
                if not {"boundary", "anchor_id"}.issubset(link) or not set(link).issubset(link_fields):
                    raise PlatformError("APPLICATION_FLOW_STAGE_EVIDENCE_INVALID", "boundary_anchors 条目字段不完整。", status_code=422)
                boundary = link.get("boundary")
                if boundary not in {"start", "end"} or boundary in linked_boundaries:
                    raise PlatformError("APPLICATION_FLOW_STAGE_EVIDENCE_INVALID", "boundary_anchors 边界非法或重复。", status_code=422)
                anchor_id = _string(link.get("anchor_id"), field=f"stage_evidence[{index}].boundary_anchors[{link_index}].anchor_id", maximum=80)
                anchor = anchor_by_id.get(anchor_id)
                if anchor is None:
                    raise PlatformError("APPLICATION_FLOW_STAGE_EVIDENCE_INVALID", "boundary_anchors 引用了未知源码 Trace 锚点。", status_code=422)
                if anchor.get("verification_status") != "observed_in_source" or not anchor.get("source_location") or not anchor.get("instrumentation"):
                    raise PlatformError("APPLICATION_FLOW_STAGE_EVIDENCE_INVALID", "边界锚点必须具有已校验的源码插桩证据。", status_code=422)
                anchor_refs = {value for value in anchor.get("source_evidence_ids", []) if isinstance(value, str)}
                if not anchor_refs or not anchor_refs.issubset(set(refs)):
                    raise PlatformError("APPLICATION_FLOW_STAGE_EVIDENCE_INVALID", "stage_evidence 必须包含边界锚点引用的源码证据。", status_code=422)
                locator = _record(stage.get(str(boundary)))
                if locator.get("type") != "trace_point" or locator.get("name") != anchor.get("name"):
                    raise PlatformError(
                        "APPLICATION_FLOW_STAGE_EVIDENCE_INVALID",
                        "源码边界锚点名称必须与场景 JSON 的 trace_point 完全一致。",
                        status_code=422,
                        details={"stage_id": stage_id, "boundary": boundary, "anchor_id": anchor_id},
                    )
                linked_boundaries.add(str(boundary))
                normalized_links.append(
                    {
                        "boundary": str(boundary),
                        "anchor_id": anchor_id,
                        "event_name": str(anchor.get("name") or ""),
                        "source_location": dict(_record(anchor.get("source_location"))),
                        "instrumentation": dict(_record(anchor.get("instrumentation"))),
                    }
                )
            expected_source_boundaries = {
                boundary
                for boundary in ("start", "end")
                if _record(stage.get(boundary)).get("type") == "trace_point"
            }
            if status == "observed_in_source":
                if expected_source_boundaries != {"start", "end"} or linked_boundaries != expected_source_boundaries:
                    raise PlatformError(
                        "APPLICATION_FLOW_STAGE_EVIDENCE_INVALID",
                        "observed_in_source stage 必须为 start/end 各绑定一个已校验源码 HiTrace 锚点。",
                        status_code=422,
                        details={"stage_id": stage_id, "expected_boundaries": sorted(expected_source_boundaries), "linked_boundaries": sorted(linked_boundaries)},
                    )
                source_declared_stage_count += 1
            if status in {"observed_in_source", "user_provided"}:
                verified_count += 1
            source_declared_anchor_count += len(normalized_links)
            notes = _string(item.get("notes"), field=f"stage_evidence[{index}].notes", maximum=2000)
            normalized_stage_evidence.append({**item, "notes": notes, "boundary_anchors": normalized_links})
        unknowns = _string_list(unknowns, field="unknowns", maximum=64, item_limit=2000)
        for stage_id in stage_hierarchy.get("unresolved_stage_ids", []):
            message = f"stage {stage_id} 尚未关联到当前代码仓范围内已核对的源码实现。"
            if message not in unknowns:
                unknowns.append(message)
        unknowns = unknowns[:64]
        normalized_runtime_alignment = deepcopy(runtime_trace_alignment) if isinstance(runtime_trace_alignment, dict) else None
        normalized_stage_analysis = deepcopy(stage_analysis) if isinstance(stage_analysis, dict) else None
        if normalized_stage_analysis is not None:
            self._validate_schema(
                "stage_analysis",
                normalized_stage_analysis,
                code="APPLICATION_FLOW_STAGE_ANALYSIS_INVALID",
            )
        runtime_verified_stages = {
            str(item.get("stage_id") or "")
            for item in _record(normalized_runtime_alignment).get("stage_matches", [])
            if isinstance(item, dict) and item.get("match_status") == "matched"
        }
        required_stages = [item for item in validated_scenario["stages"] if not item["optional"]]
        source_verified_stages = {
            item["stage_id"] for item in normalized_stage_evidence
            if item["trace_anchor_status"] in {"observed_in_source", "user_provided"}
        }
        verified_required = source_verified_stages | runtime_verified_stages
        executable_status = "ready" if all(item["id"] in verified_required for item in required_stages) else "draft_needs_trace_evidence"
        if runtime_verified_stages and source_verified_stages:
            anchor_evidence_scope = "mixed_source_runtime"
        elif runtime_verified_stages:
            anchor_evidence_scope = "runtime_observed"
        else:
            anchor_evidence_scope = "source_declared_only"
        quality = {
            "status": "pass" if executable_status == "ready" else "partial",
            "contract_validation": "passed",
            "executable_status": executable_status,
            "stage_count": len(stage_ids),
            "trace_anchor_verified_stage_count": verified_count,
            "source_declared_anchor_count": source_declared_anchor_count,
            "source_declared_stage_count": source_declared_stage_count,
            "anchor_evidence_scope": anchor_evidence_scope,
            "required_stage_count": len(required_stages),
            "required_stage_verified_count": sum(item["id"] in verified_required for item in required_stages),
            "required_stage_source_verified_count": sum(item["id"] in source_verified_stages for item in required_stages),
            "required_stage_runtime_verified_count": sum(item["id"] in runtime_verified_stages for item in required_stages),
            "hierarchy_status": _record(stage_hierarchy.get("quality")).get("status"),
            "user_action_count": _record(stage_hierarchy.get("quality")).get("action_count", 0),
            "semantic_phase_count": _record(stage_hierarchy.get("quality")).get("phase_count", 0),
            "implementation_symbol_count": _record(stage_hierarchy.get("quality")).get("implementation_count", 0),
        }
        normalized_boundary_analysis = boundary_analysis if isinstance(boundary_analysis, list) else []
        analyzed_stages = {
            str(item.get("stage_id") or "")
            for item in normalized_boundary_analysis
            if isinstance(item, dict) and str(item.get("stage_id") or "") in stage_ids
        }
        found_boundaries = sum(
            1
            for item in normalized_boundary_analysis
            if isinstance(item, dict)
            for boundary in ("start", "end")
            if _record(item.get(boundary)).get("status") == "observed_in_source"
        )
        if normalized_boundary_analysis:
            quality["anchor_search_status"] = (
                "complete" if found_boundaries == len(stage_ids) * 2
                else "partial" if found_boundaries
                else "no_match"
            )
            quality["anchor_search_stage_count"] = len(analyzed_stages)
            quality["anchor_search_found_boundary_count"] = found_boundaries
        if normalized_runtime_alignment:
            runtime_quality = _record(normalized_runtime_alignment.get("quality"))
            quality["runtime_trace_alignment_status"] = runtime_quality.get("status")
            quality["runtime_trace_observed_stage_count"] = int(runtime_quality.get("matched_stage_count") or 0)
            quality["runtime_trace_partial_stage_count"] = int(runtime_quality.get("partial_stage_count") or 0)
            quality["runtime_trace_unmatched_stage_count"] = int(runtime_quality.get("unmatched_stage_count") or 0)
        input_artifacts = [discovery_artifact_id]
        if normalized_parent_id and normalized_parent_id not in input_artifacts:
            input_artifacts.append(normalized_parent_id)
        for artifact_id in additional_input_artifacts or []:
            if artifact_id and artifact_id not in input_artifacts:
                input_artifacts.append(artifact_id)
        artifact = self.artifact_store.write(
            SCENARIO_ARTIFACT_TYPE,
            {
                "application_flow_analysis_id": application_flow_analysis_id,
                "discovery_artifact_id": discovery_artifact_id,
                "flow_id": flow_id,
                "flow_name": selected_flow.get("name"),
                "revision": revision,
                "parent_scenario_artifact_id": normalized_parent_id or None,
                "revision_request": str(revision_request or "").strip()[:4000],
                "scenario_json": validated_scenario,
                "mermaid": mermaid,
                "stage_hierarchy": stage_hierarchy,
                "stage_evidence": normalized_stage_evidence,
                "boundary_analysis": normalized_boundary_analysis,
                **({"runtime_trace_alignment": normalized_runtime_alignment} if normalized_runtime_alignment else {}),
                **({"stage_analysis": normalized_stage_analysis} if normalized_stage_analysis else {}),
                "quality": quality,
                "unknowns": unknowns,
                "generation_method": generation_method,
                "source_scopes": bound_source_scopes,
                "schema_ref": self.schema_metadata["scenario"],
            },
            source_connector_id=source_connector_id,
            source_binding=self._binding(connector),
            producer={"name": "application-execution-flow-reconstruction", "version": "1.3.0"},
            input_artifacts=input_artifacts,
            schema_version="1.0",
        )
        return {
            "status": "succeeded",
            "artifact": artifact,
            "summary_zh": (
                (
                    f"已从 Mermaid 编译 {len(stage_ids)} 个阶段的标准场景 JSON；"
                    if generation_method == "deterministic_mermaid_compiler/v1"
                    else f"已生成 {len(stage_ids)} 个阶段的应用流程场景 JSON 与 Mermaid；"
                ) +
                f"定位状态为 {executable_status}，{source_declared_stage_count} 个阶段的 start/end 已由源码 HiTrace 插桩闭合。"
            ),
        }

    def list_assets(
        self,
        *,
        source_connector_id: str = "",
        application_flow_analysis_id: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        # Tombstones are applied after reading immutable artifacts. Scan the
        # complete catalog bound so many deleted records cannot starve the
        # requested active list.
        scan_limit = 500
        discoveries = self.artifact_store.list_artifacts(DISCOVERY_ARTIFACT_TYPE, limit=scan_limit)
        scenarios = self.artifact_store.list_artifacts(SCENARIO_ARTIFACT_TYPE, limit=scan_limit)
        deleted_ids = self._deleted_asset_ids()
        active_analysis_id = _flow_analysis_id(application_flow_analysis_id, allow_empty=True)

        def includes_connector(item: dict[str, Any]) -> bool:
            if not source_connector_id or item.get("source_connector_id") == source_connector_id:
                return True
            context_scopes = _record(item.get("analysis_context")).get("source_scopes")
            scenario_scopes = item.get("source_scopes")
            scopes = context_scopes if isinstance(context_scopes, list) else scenario_scopes
            return isinstance(scopes, list) and any(
                _record(scope).get("source_connector_id") == source_connector_id
                for scope in scopes
            )

        discoveries = [
            item for item in discoveries
            if item.get("artifact_id") not in deleted_ids
            and includes_connector(item)
            and (
                not active_analysis_id
                or self._artifact_flow_analysis_id(item, DISCOVERY_ARTIFACT_TYPE) == active_analysis_id
            )
        ][:limit]
        scenarios = [
            item for item in scenarios
            if item.get("artifact_id") not in deleted_ids
            and includes_connector(item)
            and (
                not active_analysis_id
                or self._artifact_flow_analysis_id(item, SCENARIO_ARTIFACT_TYPE) == active_analysis_id
            )
        ][:limit]
        return {
            "schema_version": "application_flow_asset_catalog/v1",
            "schemas": self.schema_metadata,
            "preferences": self.get_preferences(),
            "discoveries": [
                {
                    "artifact_id": item.get("artifact_id"),
                    "application_flow_analysis_id": self._artifact_flow_analysis_id(item, DISCOVERY_ARTIFACT_TYPE),
                    "created_at": item.get("created_at"),
                    "source_connector_id": item.get("source_connector_id"),
                    "source_snapshot_id": _record(item.get("discovery")).get("source_snapshot_id"),
                    "scope": _record(item.get("discovery")).get("scope"),
                    "source_scopes": _record(item.get("analysis_context")).get("source_scopes") or [{
                        "source_connector_id": item.get("source_connector_id"),
                        "source_snapshot_id": _record(item.get("discovery")).get("source_snapshot_id"),
                        "subpath": _record(item.get("discovery")).get("scope") or "",
                        "role": "primary",
                    }],
                    "analysis_goal": _record(item.get("analysis_context")).get("analysis_goal"),
                    "code_graph_artifact_id": _record(item.get("analysis_context")).get("code_graph_artifact_id"),
                    "parent_discovery_artifact_id": _record(item.get("analysis_context")).get("parent_discovery_artifact_id"),
                    "refinement_request": _record(item.get("analysis_context")).get("refinement_request"),
                    "interaction_iteration": _record(item.get("analysis_context")).get("interaction_iteration"),
                    "trace_analysis_artifact_id": _record(item.get("analysis_context")).get(
                        "trace_analysis_artifact_id"
                    ),
                    "flow_count": len(_record(item.get("discovery")).get("flows", [])),
                    "flows": [
                        {key: flow.get(key) for key in ("id", "name", "order", "description", "confidence")}
                        for flow in _record(item.get("discovery")).get("flows", [])
                        if isinstance(flow, dict)
                    ],
                    "quality": item.get("quality"),
                }
                for item in discoveries
            ],
            "scenarios": [
                {
                    "artifact_id": item.get("artifact_id"),
                    "application_flow_analysis_id": self._artifact_flow_analysis_id(item, SCENARIO_ARTIFACT_TYPE),
                    "created_at": item.get("created_at"),
                    "source_connector_id": item.get("source_connector_id"),
                    "source_scopes": item.get("source_scopes") or [{
                        "source_connector_id": item.get("source_connector_id"),
                        "role": "primary",
                    }],
                    "discovery_artifact_id": item.get("discovery_artifact_id"),
                    "flow_id": item.get("flow_id"),
                    "flow_name": item.get("flow_name"),
                    "revision": item.get("revision") or 1,
                    "parent_scenario_artifact_id": item.get("parent_scenario_artifact_id"),
                    "revision_request": item.get("revision_request"),
                    "scenario": _record(item.get("scenario_json")).get("scenario"),
                    "stage_count": len(_record(item.get("scenario_json")).get("stages", [])),
                    "has_runtime_trace_alignment": bool(_record(item.get("runtime_trace_alignment"))),
                    "has_stage_analysis": bool(_record(item.get("stage_analysis"))),
                    "runtime_trace_analysis_artifact_id": _record(item.get("runtime_trace_alignment")).get(
                        "trace_analysis_artifact_id"
                    ),
                    "quality": item.get("quality"),
                }
                for item in scenarios
            ],
        }

    def get_preferences(self) -> dict[str, Any]:
        stored = self.preferences_registry.read()
        return {
            "schema_version": PREFERENCES_SCHEMA_VERSION,
            "confirm_flow_before_trace": (
                stored.get("confirm_flow_before_trace")
                if isinstance(stored.get("confirm_flow_before_trace"), bool)
                else True
            ),
            "updated_at": str(stored.get("updated_at") or "") or None,
        }

    def update_preferences(self, *, confirm_flow_before_trace: bool) -> dict[str, Any]:
        preferences = {
            "schema_version": PREFERENCES_SCHEMA_VERSION,
            "confirm_flow_before_trace": confirm_flow_before_trace,
            "updated_at": utc_now(),
        }
        self.preferences_registry.write(preferences)
        return preferences

    def read_discovery(self, artifact_id: str) -> dict[str, Any]:
        self._ensure_asset_active(DISCOVERY_ARTIFACT_TYPE, artifact_id)
        artifact = self.artifact_store.read(DISCOVERY_ARTIFACT_TYPE, artifact_id)
        return {
            **artifact,
            "application_flow_analysis_id": self._artifact_flow_analysis_id(artifact, DISCOVERY_ARTIFACT_TYPE),
        }

    def read_scenario(self, artifact_id: str) -> dict[str, Any]:
        self._ensure_asset_active(SCENARIO_ARTIFACT_TYPE, artifact_id)
        artifact = self.artifact_store.read(SCENARIO_ARTIFACT_TYPE, artifact_id)
        return {
            **artifact,
            "application_flow_analysis_id": self._artifact_flow_analysis_id(artifact, SCENARIO_ARTIFACT_TYPE),
        }

    def _deleted_asset_ids(self) -> set[str]:
        return set(_record(self.asset_registry.read().get("deleted_assets")))

    def _ensure_asset_active(self, artifact_type: str, artifact_id: str) -> None:
        deleted = _record(self.asset_registry.read().get("deleted_assets"))
        record = _record(deleted.get(artifact_id))
        if record and record.get("artifact_type") == artifact_type:
            raise PlatformError("APPLICATION_FLOW_ASSET_NOT_FOUND", "应用流程工件不存在。", status_code=404)

    def delete_asset(self, artifact_type: str, artifact_id: str, *, cascade: bool = False) -> dict[str, Any]:
        if artifact_type not in {DISCOVERY_ARTIFACT_TYPE, SCENARIO_ARTIFACT_TYPE}:
            raise PlatformError("APPLICATION_FLOW_ASSET_TYPE_INVALID", "应用流程工件类型非法。", status_code=422)
        artifact = self.read_discovery(artifact_id) if artifact_type == DISCOVERY_ARTIFACT_TYPE else self.read_scenario(artifact_id)
        child_scenarios: list[dict[str, Any]] = []
        if artifact_type == DISCOVERY_ARTIFACT_TYPE:
            child_scenarios = [
                item
                for item in self.artifact_store.list_artifacts(
                    SCENARIO_ARTIFACT_TYPE,
                    limit=500,
                    source_connector_id=str(artifact.get("source_connector_id") or "") or None,
                )
                if item.get("discovery_artifact_id") == artifact_id
                and item.get("artifact_id") not in self._deleted_asset_ids()
            ]
            if child_scenarios and not cascade:
                raise PlatformError(
                    "APPLICATION_FLOW_ASSET_HAS_CHILDREN",
                    "该流程发现仍有场景版本，请确认级联删除。",
                    status_code=409,
                    details={"child_scenario_count": len(child_scenarios)},
                )
        deleted_at = utc_now()
        registry = self.asset_registry.read()
        deleted = _record(registry.setdefault("deleted_assets", {}))
        targets = [(artifact_type, artifact_id), *[
            (SCENARIO_ARTIFACT_TYPE, str(item.get("artifact_id") or ""))
            for item in child_scenarios
        ]]
        for target_type, target_id in targets:
            if target_id:
                deleted[target_id] = {
                    "artifact_type": target_type,
                    "deleted_at": deleted_at,
                    "cascade_root_id": artifact_id if target_id != artifact_id else None,
                }
        registry["deleted_assets"] = deleted
        self.asset_registry.write(registry)
        self.artifact_store.append_event({
            "event_type": "application_flow_asset_deleted",
            "artifact_type": artifact_type,
            "artifact_id": artifact_id,
            "cascade_count": len(child_scenarios),
            "created_at": deleted_at,
        })
        return {
            "status": "deleted",
            "artifact_type": artifact_type,
            "artifact_id": artifact_id,
            "cascade_count": len(child_scenarios),
            "deleted_at": deleted_at,
            "retention": "immutable_artifacts_preserved",
        }
