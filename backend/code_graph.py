"""Deterministic, reusable module code-knowledge graphs.

The graph is deliberately separate from application-flow scenario JSON.  It is
an immutable, snapshot-bound navigation index: Tree-sitter provides broad C/C++
structure, SQLite provides bounded retrieval, and the Agent remains responsible
for business semantics.  No graph edge produced here claims runtime execution.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
import math
import os
import re
import shutil
import sqlite3
import statistics
import threading
import time
import tempfile
from pathlib import Path
from typing import Any, Callable

from .core import (
    ArtifactStore,
    JsonRegistry,
    PlatformError,
    SourceConnectorManager,
    sha256_file,
    sha256_json,
    utc_now,
)

try:  # Deployment reports a precise diagnostic when the optional parser is absent.
    from tree_sitter import Language, Parser
    import tree_sitter_cpp
except ImportError:  # pragma: no cover - exercised through the unavailable diagnostic.
    Language = None  # type: ignore[assignment,misc]
    Parser = None  # type: ignore[assignment,misc]
    tree_sitter_cpp = None  # type: ignore[assignment]

try:
    import tree_sitter_c
except ImportError:  # C is a subset of the C++ grammar; this is an explicit fallback.
    tree_sitter_c = None  # type: ignore[assignment]


GRAPH_CONTRACT = "module_code_graph/v1"
REGISTRY_CONTRACT = "module_code_graph_registry/v1"
BUILDER_VERSION = "1.1.0"
ESTIMATE_CONTRACT = "module_code_graph_estimate/v1"
REPORT_CONTRACT = "code_knowledge_report/v1"
GRAPH_ID_PATTERN = re.compile(r"^mcg_[a-f0-9]{24}$")
ARTIFACT_ID_PATTERN = re.compile(r"^art_[a-f0-9]{24}$")
SOURCE_SUFFIXES = frozenset({".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"})
C_SUFFIXES = frozenset({".c"})
SKIPPED_DIRECTORIES = frozenset(
    {
        ".git", ".hg", ".svn", ".idea", ".vscode", ".venv", "__pycache__",
        "node_modules", ".next", ".local-artifacts", "build", "build-out", "dist",
        "out", "output", "coverage", "target", "generated", "doxygen_output",
        "heterogeneous-enable-web-runtime",
    }
)
DEFAULT_MAX_FILES = 50_000
MAX_MAX_FILES = 500_000
MAX_FILE_BYTES = 8 * 1024 * 1024
PROGRESS_INTERVAL_SECONDS = 0.75
FALLBACK_FILES_PER_SECOND = 120.0
FALLBACK_BYTES_PER_SECOND = 8 * 1024 * 1024.0
MIN_FREE_BYTES = 512 * 1024 * 1024
ACTION_WORDS = frozenset(
    {
        "begin", "capture", "close", "commit", "configure", "connect", "create",
        "destroy", "disable", "disconnect", "enable", "end", "init", "initialize",
        "launch", "load", "open", "prepare", "process", "register", "release", "save",
        "start", "stop", "submit", "unregister", "update",
    }
)
TRACE_CALL_RE = re.compile(
    rb"\b(?P<macro>(?=[A-Za-z0-9_:.]*(?:hitrace|trace|bytrace))"
    rb"[A-Za-z_]\w*(?:(?:::|\.)[A-Za-z_]\w*)*)\s*\((?P<args>[^\n;]{0,1600})",
    re.IGNORECASE,
)
STRING_LITERAL_RES = (
    re.compile(rb'(?:(?:u8|u|U|L))?"((?:\\.|[^"\\])*)"'),
    re.compile(rb"'((?:\\.|[^'\\])*)'"),
    re.compile(rb"`((?:\\.|[^`\\])*)`"),
)


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any, limit: int = 4096) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _normalize_subpath(value: str) -> str:
    normalized = str(value or "").strip().replace("\\", "/")
    if not normalized or normalized == ".":
        return ""
    if normalized.startswith("/") or re.match(r"^[A-Za-z]:", normalized):
        raise PlatformError("CODE_GRAPH_SCOPE_INVALID", "代码知识底图目录必须是代码仓内相对路径。", status_code=422)
    parts = [part for part in normalized.split("/") if part not in {"", "."}]
    if any(part == ".." for part in parts):
        raise PlatformError("CODE_GRAPH_SCOPE_INVALID", "代码知识底图目录不能越过代码仓根目录。", status_code=422)
    return "/".join(parts)


def _node_text(source: bytes, node: Any, limit: int = 2000) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace").strip()[:limit]


def _stable_id(prefix: str, *parts: object) -> str:
    digest = sha256("\0".join(str(part) for part in parts).encode("utf-8")).hexdigest()
    return f"{prefix}_{digest[:24]}"


def _atomic_write_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(value)
            if not value.endswith("\n"):
                handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _simple_name(value: str) -> str:
    compact = value.strip().split("<", 1)[0]
    return compact.rsplit("::", 1)[-1].lstrip("~")


class _CodeGraphBuildCancelled(Exception):
    """Internal cooperative cancellation signal."""


def _action_score(symbol: str, path: str, kind: str) -> int:
    expanded = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", symbol)
    tokens = {item.casefold() for item in re.findall(r"[A-Za-z0-9]+", expanded)}
    score = sum(20 for item in ACTION_WORDS if item in tokens)
    lowered_path = f"/{path.casefold()}"
    if any(token in lowered_path for token in ("/interfaces/", "/include/", "/public/", "/kits/")):
        score += 18
    if any(token in lowered_path for token in ("/test/", "/tests/", "/mock/", "/fuzz/", "/demo/")):
        score -= 30
    if kind == "function_definition":
        score += 6
    return score


def _function_name(declarator: Any, source: bytes) -> str:
    current = declarator
    for _ in range(12):
        if current is None:
            break
        if current.type in {
            "identifier", "field_identifier", "qualified_identifier", "operator_name",
            "destructor_name", "namespace_identifier", "type_identifier",
        }:
            return _node_text(source, current, 1000)
        child = current.child_by_field_name("declarator")
        if child is None and current.type == "qualified_identifier":
            child = current.child_by_field_name("name")
        if child is not None:
            current = child
            continue
        named = [item for item in current.named_children if item.type not in {"parameter_list", "template_argument_list"}]
        current = named[0] if named else None
    text = _node_text(source, declarator, 1200)
    match = re.search(r"((?:[A-Za-z_]\w*::)*~?[A-Za-z_]\w*)\s*\(", text)
    return match.group(1) if match else text.split("(", 1)[0].strip()[-500:]


def _contains_function_declarator(node: Any) -> Any | None:
    stack = [node]
    while stack:
        current = stack.pop()
        if current.type == "function_declarator":
            return current
        stack.extend(reversed(current.named_children))
    return None


class CodeGraphManager:
    """Build and query snapshot-bound module code graphs."""

    def __init__(
        self,
        state_root: Path,
        connectors: SourceConnectorManager,
        artifact_store: ArtifactStore,
        *,
        recover_interrupted: bool = True,
    ) -> None:
        self.state_root = state_root.resolve()
        self.connectors = connectors
        self.artifact_store = artifact_store
        self.graph_root = self.state_root / "code-graphs"
        self.database_root = self.graph_root / "databases"
        self.report_root = self.graph_root / "reports"
        self.database_root.mkdir(parents=True, exist_ok=True)
        self.report_root.mkdir(parents=True, exist_ok=True)
        self.registry = JsonRegistry(
            self.graph_root / "registry.json",
            {"schema_version": REGISTRY_CONTRACT, "graphs": {}},
        )
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="code-graph")
        self._estimate_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="code-graph-estimate")
        self._cancel_events: dict[str, threading.Event] = {}
        # The Web server owns one long-lived manager and can safely interpret
        # an active state at construction as a process restart. Desktop RPC is
        # one Python process per request, so read-only polling must not mark a
        # job owned by another request as interrupted.
        if recover_interrupted:
            self._mark_interrupted_builds()

    @staticmethod
    def available() -> bool:
        return Language is not None and Parser is not None and tree_sitter_cpp is not None

    def _mark_interrupted_builds(self) -> None:
        with self._lock:
            registry = self.registry.read()
            changed = False
            for item in _record(registry.get("graphs")).values():
                if isinstance(item, dict) and item.get("status") in {"estimating", "queued", "running", "cancelling"}:
                    item["status"] = "interrupted"
                    item["finished_at"] = utc_now()
                    item["error"] = {
                        "code": "CODE_GRAPH_BUILD_INTERRUPTED",
                        "message": "后端重启中断了代码知识底图构建，请重新执行。",
                    }
                    changed = True
            if changed:
                self.registry.write(registry)

    def _scope(self, source_connector_id: str, subpath: str) -> tuple[dict[str, Any], Path, Path, str]:
        connector = self.connectors.get(source_connector_id)
        root = Path(str(connector["source_root"])).resolve()
        relative = _normalize_subpath(subpath)
        scope = (root / relative).resolve() if relative else root
        if not scope.is_dir() or not _is_relative_to(scope, root):
            raise PlatformError(
                "CODE_GRAPH_SCOPE_NOT_FOUND",
                "代码仓内不存在所选代码知识底图目录。",
                status_code=404,
                details={"source_connector_id": source_connector_id, "subpath": relative},
            )
        return connector, root, scope, relative

    def _mutate(self, graph_id: str, update: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            registry = self.registry.read()
            graphs = _record(registry.setdefault("graphs", {}))
            current = _record(graphs.get(graph_id))
            current.update(update)
            graphs[graph_id] = current
            registry["graphs"] = graphs
            self.registry.write(registry)
            return json.loads(json.dumps(current))

    def _build_identity(
        self,
        source_connector_id: str,
        subpath: str,
        max_files: int,
    ) -> tuple[dict[str, Any], str, int, str]:
        connector, _, _, relative = self._scope(source_connector_id, subpath)
        bounded_max_files = max(1, min(int(max_files), MAX_MAX_FILES))
        snapshot = _record(connector.get("snapshot"))
        build_key = {
            "source_connector_id": source_connector_id,
            "source_snapshot_id": str(snapshot.get("snapshot_id") or ""),
            "scope": relative,
            "builder_version": BUILDER_VERSION,
            "max_files": bounded_max_files,
        }
        return connector, relative, bounded_max_files, f"mcg_{sha256_json(build_key)[:24]}"

    def _new_graph(
        self,
        *,
        connector: dict[str, Any],
        relative: str,
        max_files: int,
        graph_id: str,
        status: str,
        build_series_id: str = "",
        parent_graph_id: str = "",
    ) -> dict[str, Any]:
        now = utc_now()
        snapshot = _record(connector.get("snapshot"))
        return {
            "graph_id": graph_id,
            "graph_contract": GRAPH_CONTRACT,
            "status": status,
            "source_connector_id": str(connector["connector_id"]),
            "source_snapshot_id": str(snapshot.get("snapshot_id") or ""),
            "scope": relative,
            "builder_version": BUILDER_VERSION,
            "max_files": max_files,
            "build_series_id": build_series_id or graph_id,
            "parent_graph_id": parent_graph_id or None,
            "created_at": now,
            "updated_at": now,
            "progress": {
                "phase": status,
                "files_discovered": 0,
                "files_parsed": 0,
                "files_failed": 0,
            },
        }

    def _new_build_identity(self, build_series_id: str) -> str:
        return f"mcg_{sha256(os.urandom(32) + build_series_id.encode('utf-8')).hexdigest()[:24]}"

    def _latest_series_graph(self, source_connector_id: str, build_series_id: str) -> dict[str, Any] | None:
        return next(
            (
                item
                for item in self.list_graphs(source_connector_id=source_connector_id, limit=500)
                if item.get("graph_id") == build_series_id or item.get("build_series_id") == build_series_id
            ),
            None,
        )

    def _cancel_event(self, graph_id: str, *, reset: bool = False) -> threading.Event:
        with self._lock:
            if reset or graph_id not in self._cancel_events:
                self._cancel_events[graph_id] = threading.Event()
            return self._cancel_events[graph_id]

    def request_estimate(
        self,
        source_connector_id: str,
        *,
        subpath: str = "",
        max_files: int = DEFAULT_MAX_FILES,
        force_new: bool = False,
    ) -> dict[str, Any]:
        """Create an observable estimate that requires explicit confirmation."""

        if not self.available():
            raise PlatformError(
                "CODE_GRAPH_EXTRACTOR_UNAVAILABLE",
                "Tree-sitter C/C++ 解析器未安装；请重新执行部署依赖安装。",
                status_code=503,
                details={"required_packages": ["tree-sitter", "tree-sitter-cpp", "tree-sitter-c"]},
            )
        connector, relative, bounded_max_files, build_series_id = self._build_identity(
            source_connector_id, subpath, max_files,
        )
        parent_graph = self._latest_series_graph(source_connector_id, build_series_id)
        graph_id = self._new_build_identity(build_series_id) if force_new else build_series_id
        existing = next(
            (item for item in self.list_graphs(source_connector_id=source_connector_id, limit=500) if item["graph_id"] == graph_id),
            None,
        )
        if existing and existing.get("status") in {
            "estimating", "awaiting_confirmation", "queued", "running", "cancelling", "ready",
        }:
            return {
                "status": "reused" if existing.get("status") == "ready" else "accepted",
                "graph": existing,
            }
        graph = self._new_graph(
            connector=connector,
            relative=relative,
            max_files=bounded_max_files,
            graph_id=graph_id,
            status="estimating",
            build_series_id=build_series_id,
            parent_graph_id=str(parent_graph.get("graph_id") or "") if force_new and parent_graph else "",
        )
        self._mutate(graph_id, {**graph, "estimate_requested_at": utc_now(), "error": None})
        self._cancel_event(graph_id, reset=True)
        self._estimate_executor.submit(self._run_estimate, graph_id)
        return {"status": "accepted", "graph": graph}

    def _historical_throughput(self) -> tuple[float, float, int]:
        file_rates: list[float] = []
        byte_rates: list[float] = []
        for graph in self.list_graphs(limit=500):
            if graph.get("status") != "ready":
                continue
            duration_seconds = float(graph.get("duration_ms") or 0) / 1000.0
            summary = _record(graph.get("summary"))
            files = int(summary.get("files_parsed") or 0)
            source_bytes = int(summary.get("bytes_parsed") or 0)
            if duration_seconds <= 0 or files <= 0:
                continue
            file_rates.append(files / duration_seconds)
            if source_bytes > 0:
                byte_rates.append(source_bytes / duration_seconds)
        return (
            statistics.median(file_rates) if file_rates else FALLBACK_FILES_PER_SECOND,
            statistics.median(byte_rates) if byte_rates else FALLBACK_BYTES_PER_SECOND,
            len(file_rates),
        )

    def _run_estimate(self, graph_id: str) -> None:
        started = time.monotonic()
        event = self._cancel_event(graph_id)
        try:
            graph = self.get_graph(graph_id)
            _, _, scope, _ = self._scope(str(graph["source_connector_id"]), str(graph.get("scope") or ""))

            def progress(files: int, source_bytes: int, current_path: str) -> None:
                self._mutate(graph_id, {
                    "updated_at": utc_now(),
                    "progress": {
                        "phase": "estimating",
                        "files_discovered": files,
                        "source_bytes_discovered": source_bytes,
                        "current_path": current_path,
                    },
                })

            files, truncated = self._discover_files(
                scope,
                int(graph.get("max_files") or DEFAULT_MAX_FILES),
                cancel_event=event,
                progress_callback=progress,
            )
            if event.is_set():
                raise _CodeGraphBuildCancelled()
            source_bytes = 0
            for path in files:
                try:
                    source_bytes += path.stat().st_size
                except OSError:
                    # The inventory is advisory; the build records any later read failure precisely.
                    continue
            files_per_second, bytes_per_second, historical_samples = self._historical_throughput()
            estimate_seconds = max(
                len(files) / max(files_per_second, 1.0),
                source_bytes / max(bytes_per_second, 1.0),
                1.0,
            )
            estimated_database_bytes = max(1024 * 1024, int(source_bytes * 0.45))
            free_bytes = shutil.disk_usage(self.graph_root).free
            estimate = {
                "schema_version": ESTIMATE_CONTRACT,
                "estimated_at": utc_now(),
                "inventory_duration_ms": round((time.monotonic() - started) * 1000, 2),
                "source_file_count": len(files),
                "source_bytes": source_bytes,
                "truncated_by_max_files": truncated,
                "max_files": int(graph.get("max_files") or DEFAULT_MAX_FILES),
                "estimated_duration_seconds": int(math.ceil(estimate_seconds)),
                "estimated_duration_range_seconds": {
                    "lower": max(1, int(math.floor(estimate_seconds * 0.65))),
                    "upper": max(2, int(math.ceil(estimate_seconds * 1.8))),
                },
                "estimated_database_bytes": estimated_database_bytes,
                "filesystem_free_bytes": free_bytes,
                "can_start": free_bytes >= max(MIN_FREE_BYTES, estimated_database_bytes * 2),
                "confidence": "high" if historical_samples >= 5 else "medium" if historical_samples >= 2 else "low",
                "basis": "historical_local_builds" if historical_samples else "conservative_default_throughput",
                "historical_sample_count": historical_samples,
                "files_per_second": round(files_per_second, 2),
                "bytes_per_second": round(bytes_per_second, 2),
            }
            current = self.get_graph(graph_id)
            if current.get("auto_start_after_estimate") is True:
                self._mutate(graph_id, {
                    "status": "queued",
                    "estimate": estimate,
                    "updated_at": utc_now(),
                    "progress": {"phase": "queued", "files_discovered": len(files), "files_parsed": 0, "files_failed": 0},
                })
                self._executor.submit(self._run_build, graph_id)
            else:
                self._mutate(graph_id, {
                    "status": "awaiting_confirmation",
                    "estimate": estimate,
                    "updated_at": utc_now(),
                    "progress": {"phase": "awaiting_confirmation", "files_discovered": len(files), "files_parsed": 0, "files_failed": 0},
                })
        except _CodeGraphBuildCancelled:
            self._mutate(graph_id, {
                "status": "cancelled",
                "finished_at": utc_now(),
                "updated_at": utc_now(),
                "progress": {**_record(self.get_graph(graph_id).get("progress")), "phase": "cancelled"},
            })
        except Exception as exc:
            code = exc.code if isinstance(exc, PlatformError) else "CODE_GRAPH_ESTIMATE_FAILED"
            message = exc.message if isinstance(exc, PlatformError) else str(exc)
            self._mutate(graph_id, {
                "status": "failed",
                "finished_at": utc_now(),
                "updated_at": utc_now(),
                "error": {"code": code, "message": message[:2000]},
                "progress": {**_record(self.get_graph(graph_id).get("progress")), "phase": "failed"},
            })

    def confirm_build(self, graph_id: str) -> dict[str, Any]:
        graph = self.get_graph(graph_id)
        if graph.get("status") == "ready":
            return {"status": "reused", "graph": graph}
        if graph.get("status") in {"queued", "running", "cancelling"}:
            return {"status": "accepted", "graph": graph}
        if graph.get("status") != "awaiting_confirmation":
            raise PlatformError(
                "CODE_GRAPH_CONFIRMATION_INVALID",
                "代码知识底图尚未完成估时，或当前状态不能确认启动。",
                status_code=409,
                details={"status": graph.get("status")},
            )
        estimate = _record(graph.get("estimate"))
        if estimate.get("can_start") is not True:
            raise PlatformError(
                "CODE_GRAPH_DISK_INSUFFICIENT",
                "当前可用空间不足以启动代码知识底图构建。",
                status_code=507,
                details={"estimate": estimate},
            )
        self._cancel_event(graph_id, reset=True)
        queued = self._mutate(graph_id, {
            "status": "queued",
            "confirmed_at": utc_now(),
            "updated_at": utc_now(),
            "error": None,
            "progress": {"phase": "queued", "files_discovered": int(estimate.get("source_file_count") or 0), "files_parsed": 0, "files_failed": 0},
        })
        self._executor.submit(self._run_build, graph_id)
        return {"status": "accepted", "graph": queued}

    def cancel_build(self, graph_id: str) -> dict[str, Any]:
        graph = self.get_graph(graph_id)
        status = str(graph.get("status") or "")
        if status in {"cancelled", "failed", "interrupted", "ready"}:
            return {"status": "unchanged", "graph": graph}
        if status == "awaiting_confirmation":
            cancelled = self._mutate(graph_id, {
                "status": "cancelled",
                "cancel_requested_at": utc_now(),
                "finished_at": utc_now(),
                "updated_at": utc_now(),
                "progress": {**_record(graph.get("progress")), "phase": "cancelled"},
            })
            return {"status": "cancelled", "graph": cancelled}
        if status not in {"estimating", "queued", "running", "cancelling"}:
            raise PlatformError("CODE_GRAPH_CANCEL_INVALID", "当前代码知识底图状态不能取消。", status_code=409, details={"status": status})
        self._cancel_event(graph_id).set()
        cancelling = self._mutate(graph_id, {
            "status": "cancelling",
            "cancel_requested_at": utc_now(),
            "updated_at": utc_now(),
            "progress": {**_record(graph.get("progress")), "phase": "cancelling"},
        })
        return {"status": "accepted", "graph": cancelling}

    def claim_semantic_analysis(self, graph_id: str, *, request_id: str, force: bool = False) -> dict[str, Any]:
        graph = self.validate_ready_graph(graph_id)
        normalized_request_id = _text(request_id, 120)
        if not normalized_request_id:
            raise PlatformError("CODE_KNOWLEDGE_REQUEST_ID_REQUIRED", "代码语义分析需要 request_id。", status_code=422)
        current = _record(graph.get("semantic_analysis"))
        if not force and current.get("status") == "completed":
            return {"status": "reused", "dispatch_required": False, "graph": graph}
        if not force and current.get("status") == "requested":
            requested_at = _text(current.get("requested_at"), 80)
            try:
                age = datetime.now(timezone.utc) - datetime.fromisoformat(requested_at.replace("Z", "+00:00"))
            except (TypeError, ValueError):
                age = timedelta.max
            if age < timedelta(minutes=30):
                return {"status": "in_progress", "dispatch_required": False, "graph": graph}
        updated = self._mutate(graph_id, {
            "semantic_analysis": {
                "status": "requested",
                "request_id": normalized_request_id,
                "requested_at": utc_now(),
                "attempt": int(current.get("attempt") or 0) + 1,
            },
            "updated_at": utc_now(),
        })
        return {"status": "accepted", "dispatch_required": True, "graph": updated}

    def start_build(
        self,
        source_connector_id: str,
        *,
        subpath: str = "",
        max_files: int = DEFAULT_MAX_FILES,
        force_new: bool = False,
    ) -> dict[str, Any]:
        if not self.available():
            raise PlatformError(
                "CODE_GRAPH_EXTRACTOR_UNAVAILABLE",
                "Tree-sitter C/C++ 解析器未安装；请重新执行部署依赖安装。",
                status_code=503,
                details={"required_packages": ["tree-sitter", "tree-sitter-cpp", "tree-sitter-c"]},
            )
        connector, relative, bounded_max_files, build_series_id = self._build_identity(source_connector_id, subpath, max_files)
        parent_graph = self._latest_series_graph(source_connector_id, build_series_id)
        graph_id = self._new_build_identity(build_series_id) if force_new else build_series_id
        existing = next((item for item in self.list_graphs(source_connector_id=source_connector_id, limit=500) if item["graph_id"] == graph_id), None)
        if existing and existing.get("status") in {"queued", "running", "ready"}:
            return {"status": "reused" if existing.get("status") == "ready" else "accepted", "graph": existing}
        if existing and existing.get("status") == "awaiting_confirmation":
            return self.confirm_build(graph_id)
        if existing and existing.get("status") == "estimating":
            graph = self._mutate(graph_id, {"auto_start_after_estimate": True, "updated_at": utc_now()})
            return {"status": "accepted", "graph": graph}

        if shutil.disk_usage(self.graph_root).free < MIN_FREE_BYTES:
            raise PlatformError("CODE_GRAPH_DISK_INSUFFICIENT", "代码知识底图构建至少需要 512 MiB 可用空间。", status_code=507)

        graph = self._new_graph(
            connector=connector,
            relative=relative,
            max_files=bounded_max_files,
            graph_id=graph_id,
            status="queued",
            build_series_id=build_series_id,
            parent_graph_id=str(parent_graph.get("graph_id") or "") if force_new and parent_graph else "",
        )
        self._mutate(graph_id, graph)
        self._cancel_event(graph_id, reset=True)
        self._executor.submit(self._run_build, graph_id)
        return {"status": "accepted", "graph": graph}

    def _run_build(self, graph_id: str) -> None:
        graph = self.get_graph(graph_id)
        started = time.monotonic()
        cancel_event = self._cancel_event(graph_id)
        if cancel_event.is_set():
            self._mutate(graph_id, {
                "status": "cancelled",
                "finished_at": utc_now(),
                "updated_at": utc_now(),
                "progress": {**_record(graph.get("progress")), "phase": "cancelled"},
            })
            return
        self._mutate(graph_id, {"status": "running", "started_at": utc_now(), "updated_at": utc_now(), "error": None})
        try:
            connector, repository_root, scope, relative = self._scope(
                str(graph["source_connector_id"]),
                str(graph.get("scope") or ""),
            )
            summary = self._build_database(
                graph_id,
                repository_root=repository_root,
                scope=scope,
                max_files=int(graph.get("max_files") or DEFAULT_MAX_FILES),
                cancel_event=cancel_event,
            )
            database_path = self.database_root / f"{graph_id}.sqlite3"
            if cancel_event.is_set():
                database_path.unlink(missing_ok=True)
                raise _CodeGraphBuildCancelled()
            snapshot = _record(connector.get("snapshot"))
            source_binding = {
                "declared_revision": snapshot.get("snapshot_id"),
                "resolved_snapshot_id": snapshot.get("snapshot_id"),
                "match_level": "exact" if snapshot.get("clean") else "compatible",
                "resolver_version": "module-code-graph/1.0",
                "snapshot_state": "clean" if snapshot.get("clean") else "dirty",
            }
            input_artifacts = [str(connector["snapshot_artifact_id"])] if connector.get("snapshot_artifact_id") else []
            artifact = self.artifact_store.write(
                "module_code_graph",
                {
                    "graph_id": graph_id,
                    "graph_contract": GRAPH_CONTRACT,
                    "scope": relative,
                    "source_snapshot_id": snapshot.get("snapshot_id"),
                    "summary": summary,
                    "database": {
                        "format": "sqlite3",
                        "file_name": database_path.name,
                        "content_sha256": sha256_file(database_path),
                        "size_bytes": database_path.stat().st_size,
                    },
                },
                source_connector_id=str(graph["source_connector_id"]),
                source_binding=source_binding,
                producer={"name": "module-code-graph-builder", "version": BUILDER_VERSION},
                input_artifacts=input_artifacts,
                schema_version="1.0",
            )
            self._mutate(
                graph_id,
                {
                    "status": "ready",
                    "updated_at": utc_now(),
                    "finished_at": utc_now(),
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                    "artifact_id": artifact["artifact_id"],
                    "summary": summary,
                    "database_size_bytes": database_path.stat().st_size,
                    "progress": {
                        "phase": "completed",
                        "files_discovered": summary["files_discovered"],
                        "files_parsed": summary["files_parsed"],
                        "files_failed": summary["files_failed"],
                        "symbol_count": summary["symbol_count"],
                        "call_candidate_count": summary["call_candidate_count"],
                        "tracepoint_count": summary["tracepoint_count"],
                    },
                },
            )
        except _CodeGraphBuildCancelled:
            (self.database_root / f"{graph_id}.sqlite3").unlink(missing_ok=True)
            self._mutate(
                graph_id,
                {
                    "status": "cancelled",
                    "updated_at": utc_now(),
                    "finished_at": utc_now(),
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                    "error": None,
                    "progress": {**_record(self.get_graph(graph_id).get("progress")), "phase": "cancelled"},
                },
            )
        except Exception as exc:  # Background jobs must persist a diagnosable terminal state.
            code = exc.code if isinstance(exc, PlatformError) else "CODE_GRAPH_BUILD_FAILED"
            message = exc.message if isinstance(exc, PlatformError) else str(exc)
            self._mutate(
                graph_id,
                {
                    "status": "failed",
                    "updated_at": utc_now(),
                    "finished_at": utc_now(),
                    "duration_ms": round((time.monotonic() - started) * 1000, 2),
                    "error": {"code": code, "message": message[:2000]},
                    "progress": {**_record(self.get_graph(graph_id).get("progress")), "phase": "failed"},
                },
            )

    @staticmethod
    def _discover_files(
        scope: Path,
        max_files: int,
        *,
        cancel_event: threading.Event | None = None,
        progress_callback: Callable[[int, int, str], None] | None = None,
    ) -> tuple[list[Path], bool]:
        files: list[Path] = []
        truncated = False
        source_bytes = 0
        last_progress = 0.0
        for current_root, directories, names in os.walk(scope):
            if cancel_event is not None and cancel_event.is_set():
                raise _CodeGraphBuildCancelled()
            directories[:] = sorted(
                item for item in directories
                if item not in SKIPPED_DIRECTORIES and not item.startswith(".")
            )
            for name in sorted(names):
                path = Path(current_root) / name
                if path.suffix.casefold() not in SOURCE_SUFFIXES:
                    continue
                files.append(path)
                try:
                    source_bytes += path.stat().st_size
                except OSError:
                    pass
                now = time.monotonic()
                if progress_callback is not None and (now - last_progress >= PROGRESS_INTERVAL_SECONDS):
                    last_progress = now
                    progress_callback(len(files), source_bytes, path.relative_to(scope).as_posix())
                if len(files) >= max_files:
                    truncated = True
                    if progress_callback is not None:
                        progress_callback(len(files), source_bytes, path.relative_to(scope).as_posix())
                    return files, truncated
        if progress_callback is not None:
            progress_callback(len(files), source_bytes, "")
        return files, truncated

    @staticmethod
    def _schema(connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            PRAGMA journal_mode = DELETE;
            PRAGMA synchronous = NORMAL;
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value_json TEXT NOT NULL);
            CREATE TABLE files (
                file_id TEXT PRIMARY KEY, path TEXT NOT NULL UNIQUE, language TEXT NOT NULL,
                size_bytes INTEGER NOT NULL, line_count INTEGER NOT NULL, content_sha256 TEXT NOT NULL,
                parse_status TEXT NOT NULL, parse_error TEXT
            );
            CREATE TABLE symbols (
                symbol_id TEXT PRIMARY KEY, file_id TEXT NOT NULL, kind TEXT NOT NULL,
                name TEXT NOT NULL, qualified_name TEXT NOT NULL, signature TEXT NOT NULL,
                line_start INTEGER NOT NULL, line_end INTEGER NOT NULL, action_score INTEGER NOT NULL,
                FOREIGN KEY(file_id) REFERENCES files(file_id)
            );
            CREATE TABLE calls (
                call_id TEXT PRIMARY KEY, file_id TEXT NOT NULL, caller_symbol_id TEXT,
                callee_text TEXT NOT NULL, line INTEGER NOT NULL, resolved_symbol_id TEXT,
                resolution_status TEXT NOT NULL,
                FOREIGN KEY(file_id) REFERENCES files(file_id)
            );
            CREATE TABLE includes (
                include_id TEXT PRIMARY KEY, file_id TEXT NOT NULL, target_text TEXT NOT NULL,
                line INTEGER NOT NULL, resolved_file_id TEXT, resolution_status TEXT NOT NULL,
                FOREIGN KEY(file_id) REFERENCES files(file_id)
            );
            CREATE TABLE tracepoints (
                tracepoint_id TEXT PRIMARY KEY, file_id TEXT NOT NULL, owner_symbol_id TEXT,
                macro TEXT NOT NULL, label TEXT, label_kind TEXT NOT NULL, line INTEGER NOT NULL,
                FOREIGN KEY(file_id) REFERENCES files(file_id)
            );
            CREATE INDEX symbols_name_idx ON symbols(name);
            CREATE INDEX symbols_qualified_name_idx ON symbols(qualified_name);
            CREATE INDEX symbols_file_idx ON symbols(file_id);
            CREATE INDEX calls_caller_idx ON calls(caller_symbol_id);
            CREATE INDEX calls_resolved_idx ON calls(resolved_symbol_id);
            CREATE INDEX calls_callee_idx ON calls(callee_text);
            CREATE INDEX includes_file_idx ON includes(file_id);
            CREATE INDEX tracepoints_label_idx ON tracepoints(label);
            """
        )

    @staticmethod
    def _parsers() -> dict[str, Any]:
        if not CodeGraphManager.available():
            raise PlatformError("CODE_GRAPH_EXTRACTOR_UNAVAILABLE", "Tree-sitter C/C++ 解析器不可用。", status_code=503)
        cpp_parser = Parser(Language(tree_sitter_cpp.language()))
        c_parser = cpp_parser
        if tree_sitter_c is not None:
            c_parser = Parser(Language(tree_sitter_c.language()))
        return {"c": c_parser, "cpp": cpp_parser}

    def _extract_file(
        self,
        *,
        repository_root: Path,
        path: Path,
        source: bytes,
        parser: Any,
    ) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        relative = path.relative_to(repository_root).as_posix()
        language = "c" if path.suffix.casefold() in C_SUFFIXES else "cpp"
        file_id = _stable_id("file", relative)
        tree = parser.parse(source)
        file_row = {
            "file_id": file_id,
            "path": relative,
            "language": language,
            "size_bytes": len(source),
            "line_count": source.count(b"\n"),
            "content_sha256": sha256(source).hexdigest(),
            "parse_status": "partial" if tree.root_node.has_error else "complete",
            "parse_error": "tree_contains_error_nodes" if tree.root_node.has_error else None,
        }
        symbols: list[dict[str, Any]] = []
        calls: list[dict[str, Any]] = []
        call_ids: set[str] = set()
        includes: list[dict[str, Any]] = []
        traces: list[dict[str, Any]] = []
        symbol_ranges: list[tuple[int, int, str]] = []

        stack: list[tuple[Any, tuple[str, ...], str | None]] = [(tree.root_node, (), None)]
        while stack:
            node, context, owner_symbol_id = stack.pop()
            child_context = context
            child_owner = owner_symbol_id

            if node.type in {"namespace_definition", "class_specifier", "struct_specifier", "union_specifier", "enum_specifier"}:
                name_node = node.child_by_field_name("name")
                name = _node_text(source, name_node, 1000) if name_node is not None else ""
                if name:
                    kind = node.type.removesuffix("_specifier").removesuffix("_definition")
                    qualified = name if "::" in name else "::".join((*context, name))
                    symbol_id = _stable_id("sym", relative, node.start_byte, node.end_byte)
                    symbols.append(
                        {
                            "symbol_id": symbol_id,
                            "file_id": file_id,
                            "kind": kind,
                            "name": _simple_name(name),
                            "qualified_name": qualified,
                            "signature": _node_text(source, node, 1600).split("{", 1)[0].strip(),
                            "line_start": node.start_point.row + 1,
                            "line_end": node.end_point.row + 1,
                            "action_score": 0,
                        }
                    )
                    child_context = (*context, _simple_name(name))

            if node.type == "function_definition":
                declarator = node.child_by_field_name("declarator")
                name = _function_name(declarator, source) if declarator is not None else ""
                body = node.child_by_field_name("body")
                if name:
                    qualified = name if "::" in name else "::".join((*context, name))
                    signature_end = body.start_byte if body is not None else min(node.end_byte, node.start_byte + 2000)
                    signature = source[node.start_byte:signature_end].decode("utf-8", errors="replace").strip()[:2000]
                    symbol_id = _stable_id("sym", relative, node.start_byte, node.end_byte)
                    symbols.append(
                        {
                            "symbol_id": symbol_id,
                            "file_id": file_id,
                            "kind": "function_definition",
                            "name": _simple_name(name),
                            "qualified_name": qualified,
                            "signature": signature,
                            "line_start": node.start_point.row + 1,
                            "line_end": node.end_point.row + 1,
                            "action_score": _action_score(qualified, relative, "function_definition"),
                        }
                    )
                    child_owner = symbol_id
                    symbol_ranges.append((node.start_point.row + 1, node.end_point.row + 1, symbol_id))

            if node.type == "declaration" and owner_symbol_id is None:
                function_declarator = _contains_function_declarator(node)
                if function_declarator is not None:
                    name = _function_name(function_declarator, source)
                    if name:
                        qualified = name if "::" in name else "::".join((*context, name))
                        symbol_id = _stable_id("sym", relative, node.start_byte, node.end_byte)
                        symbols.append(
                            {
                                "symbol_id": symbol_id,
                                "file_id": file_id,
                                "kind": "function_declaration",
                                "name": _simple_name(name),
                                "qualified_name": qualified,
                                "signature": _node_text(source, node, 2000),
                                "line_start": node.start_point.row + 1,
                                "line_end": node.end_point.row + 1,
                                "action_score": _action_score(qualified, relative, "function_declaration"),
                            }
                        )

            if node.type == "call_expression":
                function = node.child_by_field_name("function")
                callee = _node_text(source, function, 500) if function is not None else ""
                if callee:
                    # Nested fluent calls share a source start and their display text may
                    # truncate identically. The complete AST byte range is the stable
                    # identity of a call within an immutable source snapshot.
                    call_id = _stable_id("call", relative, node.start_byte, node.end_byte)
                    if call_id not in call_ids:
                        call_ids.add(call_id)
                        calls.append({
                            "call_id": call_id,
                            "file_id": file_id,
                            "caller_symbol_id": owner_symbol_id,
                            "callee_text": callee,
                            "line": node.start_point.row + 1,
                            "resolved_symbol_id": None,
                            "resolution_status": "unresolved_syntax_candidate",
                        })

            if node.type == "preproc_include":
                path_node = node.child_by_field_name("path")
                target = _node_text(source, path_node, 1000) if path_node is not None else _node_text(source, node, 1000)
                target = target.strip('<>"')
                if target:
                    includes.append(
                        {
                            "include_id": _stable_id("inc", relative, node.start_byte, node.end_byte),
                            "file_id": file_id,
                            "target_text": target,
                            "line": node.start_point.row + 1,
                            "resolved_file_id": None,
                            "resolution_status": "unresolved",
                        }
                    )

            for child in reversed(node.named_children):
                stack.append((child, child_context, child_owner))

        symbol_ranges.sort(key=lambda item: (item[1] - item[0], item[0]))
        for matched in TRACE_CALL_RE.finditer(source):
            line = source.count(b"\n", 0, matched.start()) + 1
            owner = next((item[2] for item in symbol_ranges if item[0] <= line <= item[1]), None)
            literal_matches = [pattern.search(matched.group("args")) for pattern in STRING_LITERAL_RES]
            literal_match = min(
                (item for item in literal_matches if item is not None),
                key=lambda item: item.start(),
                default=None,
            )
            label = None
            if literal_match:
                label = literal_match.group(1).decode("utf-8", errors="replace")[:1000]
            macro = matched.group("macro").decode("ascii", errors="replace")[:160]
            traces.append(
                {
                    "tracepoint_id": _stable_id("trace", relative, matched.start(), matched.end()),
                    "file_id": file_id,
                    "owner_symbol_id": owner,
                    "macro": macro,
                    "label": label,
                    "label_kind": "literal_candidate" if label else "dynamic_or_wrapper",
                    "line": line,
                }
            )
        return file_row, symbols, calls, includes, traces

    def _build_database(
        self,
        graph_id: str,
        *,
        repository_root: Path,
        scope: Path,
        max_files: int,
        cancel_event: threading.Event,
    ) -> dict[str, Any]:
        build_started = time.monotonic()

        def discovery_progress(files_discovered: int, source_bytes: int, current_path: str) -> None:
            self._mutate(
                graph_id,
                {
                    "updated_at": utc_now(),
                    "progress": {
                        "phase": "discovering",
                        "files_discovered": files_discovered,
                        "source_bytes_discovered": source_bytes,
                        "files_parsed": 0,
                        "files_failed": 0,
                        "symbol_count": 0,
                        "call_candidate_count": 0,
                        "tracepoint_count": 0,
                        "current_path": current_path,
                    },
                },
            )

        files, truncated = self._discover_files(
            scope,
            max_files,
            cancel_event=cancel_event,
            progress_callback=discovery_progress,
        )
        if cancel_event.is_set():
            raise _CodeGraphBuildCancelled()
        self._mutate(
            graph_id,
            {
                "updated_at": utc_now(),
                "progress": {
                    "phase": "parsing",
                    "files_discovered": len(files),
                    "files_parsed": 0,
                    "files_failed": 0,
                    "symbol_count": 0,
                    "call_candidate_count": 0,
                    "tracepoint_count": 0,
                },
            },
        )
        parsers = self._parsers()
        final_path = self.database_root / f"{graph_id}.sqlite3"
        temporary_path = self.database_root / f".{graph_id}.{os.getpid()}.tmp.sqlite3"
        temporary_path.unlink(missing_ok=True)
        counters: Counter[str] = Counter()
        parsed_source_manifest = sha256()
        parse_failures: list[dict[str, Any]] = []
        last_progress = 0.0
        connection = sqlite3.connect(temporary_path)
        try:
            self._schema(connection)
            for index, path in enumerate(files, 1):
                if cancel_event.is_set():
                    raise _CodeGraphBuildCancelled()
                relative = path.relative_to(repository_root).as_posix()
                try:
                    stat = path.stat()
                    if stat.st_size > MAX_FILE_BYTES:
                        raise ValueError(f"file exceeds {MAX_FILE_BYTES} bytes")
                    source = path.read_bytes()
                    language = "c" if path.suffix.casefold() in C_SUFFIXES else "cpp"
                    file_row, symbols, calls, includes, traces = self._extract_file(
                        repository_root=repository_root,
                        path=path,
                        source=source,
                        parser=parsers[language],
                    )
                    parsed_source_manifest.update(relative.encode("utf-8"))
                    parsed_source_manifest.update(b"\0")
                    parsed_source_manifest.update(str(file_row["content_sha256"]).encode("ascii"))
                    parsed_source_manifest.update(b"\n")
                    connection.execute(
                        "INSERT INTO files VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        tuple(file_row[key] for key in ("file_id", "path", "language", "size_bytes", "line_count", "content_sha256", "parse_status", "parse_error")),
                    )
                    connection.executemany(
                        "INSERT INTO symbols VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        [tuple(item[key] for key in ("symbol_id", "file_id", "kind", "name", "qualified_name", "signature", "line_start", "line_end", "action_score")) for item in symbols],
                    )
                    connection.executemany(
                        "INSERT INTO calls VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [tuple(item[key] for key in ("call_id", "file_id", "caller_symbol_id", "callee_text", "line", "resolved_symbol_id", "resolution_status")) for item in calls],
                    )
                    connection.executemany(
                        "INSERT INTO includes VALUES (?, ?, ?, ?, ?, ?)",
                        [tuple(item[key] for key in ("include_id", "file_id", "target_text", "line", "resolved_file_id", "resolution_status")) for item in includes],
                    )
                    connection.executemany(
                        "INSERT INTO tracepoints VALUES (?, ?, ?, ?, ?, ?, ?)",
                        [tuple(item[key] for key in ("tracepoint_id", "file_id", "owner_symbol_id", "macro", "label", "label_kind", "line")) for item in traces],
                    )
                    counters["files_parsed"] += 1
                    counters["bytes_parsed"] += len(source)
                    counters["lines"] += file_row["line_count"]
                    counters["symbols"] += len(symbols)
                    counters["calls"] += len(calls)
                    counters["includes"] += len(includes)
                    counters["tracepoints"] += len(traces)
                    if file_row["parse_status"] == "partial":
                        counters["files_partial"] += 1
                except (OSError, ValueError, RuntimeError) as exc:
                    counters["files_failed"] += 1
                    if len(parse_failures) < 32:
                        parse_failures.append({"path": relative, "reason": str(exc)[:1000]})
                if index % 200 == 0:
                    connection.commit()
                now = time.monotonic()
                if now - last_progress >= PROGRESS_INTERVAL_SECONDS or index == len(files):
                    last_progress = now
                    elapsed_seconds = max(0.001, now - build_started)
                    observed_rate = index / elapsed_seconds
                    remaining_seconds = max(0, int(math.ceil((len(files) - index) / max(observed_rate, 0.01))))
                    estimated_completion_at = (
                        datetime.now(timezone.utc) + timedelta(seconds=remaining_seconds)
                    ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
                    self._mutate(
                        graph_id,
                        {
                            "updated_at": utc_now(),
                            "progress": {
                                "phase": "parsing",
                                "files_discovered": len(files),
                                "files_processed": index,
                                "files_parsed": counters["files_parsed"],
                                "files_failed": counters["files_failed"],
                                "symbol_count": counters["symbols"],
                                "call_candidate_count": counters["calls"],
                                "tracepoint_count": counters["tracepoints"],
                                "current_path": relative,
                                "observed_files_per_second": round(observed_rate, 2),
                                "estimated_remaining_seconds": remaining_seconds,
                                "estimated_completion_at": estimated_completion_at,
                                "eta_updated_at": utc_now(),
                            },
                        },
                    )

            if cancel_event.is_set():
                raise _CodeGraphBuildCancelled()
            connection.commit()
            self._mutate(
                graph_id,
                {
                    "updated_at": utc_now(),
                    "progress": {
                        **_record(self.get_graph(graph_id).get("progress")),
                        "phase": "resolving_edges",
                        "current_path": "",
                    },
                },
            )
            self._resolve_edges(connection)
            if cancel_event.is_set():
                raise _CodeGraphBuildCancelled()
            summary = {
                "files_discovered": len(files),
                "files_parsed": counters["files_parsed"],
                "files_partial": counters["files_partial"],
                "files_failed": counters["files_failed"],
                "truncated": truncated,
                "bytes_parsed": counters["bytes_parsed"],
                "line_count": counters["lines"],
                "symbol_count": counters["symbols"],
                "call_candidate_count": counters["calls"],
                "include_count": counters["includes"],
                "tracepoint_count": counters["tracepoints"],
                "parsed_source_manifest_sha256": parsed_source_manifest.hexdigest(),
                "parse_failure_samples": parse_failures,
                "analysis_method": "tree_sitter_c_cpp_structure/v1",
                "evidence_scope": "static_syntax_candidate",
            }
            for key, value in {
                "graph_contract": GRAPH_CONTRACT,
                "builder_version": BUILDER_VERSION,
                "summary": summary,
            }.items():
                connection.execute("INSERT INTO metadata(key, value_json) VALUES (?, ?)", (key, json.dumps(value, ensure_ascii=True, sort_keys=True)))
            connection.commit()
            connection.execute("PRAGMA optimize")
            connection.commit()
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
        finally:
            connection.close()
        try:
            os.replace(temporary_path, final_path)
        except Exception:
            temporary_path.unlink(missing_ok=True)
            raise
        return summary

    @staticmethod
    def _resolve_edges(connection: sqlite3.Connection) -> None:
        rows = connection.execute("SELECT symbol_id, name, qualified_name FROM symbols").fetchall()
        exact: dict[str, list[str]] = defaultdict(list)
        simple: dict[str, list[str]] = defaultdict(list)
        for symbol_id, name, qualified in rows:
            exact[str(qualified).casefold()].append(str(symbol_id))
            simple[str(name).casefold()].append(str(symbol_id))
        updates: list[tuple[str | None, str, str]] = []
        for call_id, callee_text in connection.execute("SELECT call_id, callee_text FROM calls"):
            normalized = re.sub(r"\s*<.*>$", "", str(callee_text).strip()).casefold()
            candidates = exact.get(normalized, [])
            status = "unique_qualified_name_candidate"
            if len(candidates) != 1:
                candidates = simple.get(_simple_name(normalized).casefold(), [])
                status = "unique_simple_name_candidate"
            if len(candidates) == 1:
                updates.append((candidates[0], status, str(call_id)))
            else:
                updates.append((None, "ambiguous_or_external", str(call_id)))
        connection.executemany("UPDATE calls SET resolved_symbol_id = ?, resolution_status = ? WHERE call_id = ?", updates)

        files = connection.execute("SELECT file_id, path FROM files").fetchall()
        by_path = {str(path): str(file_id) for file_id, path in files}
        by_basename: dict[str, list[str]] = defaultdict(list)
        for file_id, path in files:
            by_basename[Path(str(path)).name].append(str(file_id))
        include_updates: list[tuple[str | None, str, str]] = []
        for include_id, target in connection.execute("SELECT include_id, target_text FROM includes"):
            target_text = str(target).replace("\\", "/")
            resolved = by_path.get(target_text)
            status = "exact_relative_path_candidate"
            if resolved is None:
                matches = by_basename.get(Path(target_text).name, [])
                resolved = matches[0] if len(matches) == 1 else None
                status = "unique_basename_candidate" if resolved else "ambiguous_or_external"
            include_updates.append((resolved, status, str(include_id)))
        connection.executemany("UPDATE includes SET resolved_file_id = ?, resolution_status = ? WHERE include_id = ?", include_updates)
        connection.commit()

    def list_graphs(self, *, source_connector_id: str = "", limit: int = 100) -> list[dict[str, Any]]:
        graphs = [item for item in _record(self.registry.read().get("graphs")).values() if isinstance(item, dict)]
        if source_connector_id:
            graphs = [item for item in graphs if item.get("source_connector_id") == source_connector_id]
        graphs.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return json.loads(json.dumps(graphs[: max(1, min(limit, 500))]))

    def delete_graph(self, graph_id: str) -> dict[str, Any]:
        """Logically delete one completed build and its reports.

        Immutable evidence artifacts remain available for existing lineage. The
        operational registry and SQLite index are removed so the build no longer
        appears in user catalogs or consumes queryable index storage.
        """

        graph = self.get_graph(graph_id)
        if graph.get("status") in {"estimating", "queued", "running", "cancelling"}:
            raise PlatformError(
                "CODE_GRAPH_DELETE_ACTIVE",
                "正在执行的代码知识底图不能删除，请先中止并等待任务结束。",
                status_code=409,
                details={"status": graph.get("status")},
            )
        reports = self.list_reports(graph_id=graph_id, limit=500)
        deleted_at = utc_now()
        with self._lock:
            registry = self.registry.read()
            graphs = _record(registry.setdefault("graphs", {}))
            archived = _record(registry.setdefault("archived_graphs", {}))
            deleted_reports = _record(registry.setdefault("deleted_report_ids", {}))
            stored = _record(graphs.pop(graph_id, None))
            if not stored:
                raise PlatformError("CODE_GRAPH_NOT_FOUND", "代码知识底图不存在。", status_code=404)
            archived[graph_id] = {**stored, "deleted_at": deleted_at}
            for report in reports:
                report_id = str(report.get("artifact_id") or "")
                if report_id:
                    deleted_reports[report_id] = {"deleted_at": deleted_at, "graph_id": graph_id, "cascade": True}
            registry["graphs"] = graphs
            registry["archived_graphs"] = archived
            registry["deleted_report_ids"] = deleted_reports
            self.registry.write(registry)
        (self.database_root / f"{graph_id}.sqlite3").unlink(missing_ok=True)
        (self.database_root / f".{graph_id}.tmp.sqlite3").unlink(missing_ok=True)
        self.artifact_store.append_event({
            "event_type": "code_graph_deleted",
            "graph_id": graph_id,
            "deleted_report_count": len(reports),
            "created_at": deleted_at,
        })
        return {
            "status": "deleted",
            "graph_id": graph_id,
            "deleted_report_count": len(reports),
            "deleted_at": deleted_at,
            "retention": "immutable_lineage_artifacts_preserved",
        }

    def get_graph(self, graph_id: str) -> dict[str, Any]:
        if not GRAPH_ID_PATTERN.fullmatch(graph_id):
            raise PlatformError("CODE_GRAPH_NOT_FOUND", "代码知识底图 ID 非法。", status_code=404)
        value = _record(_record(self.registry.read().get("graphs")).get(graph_id))
        if not value:
            raise PlatformError("CODE_GRAPH_NOT_FOUND", "代码知识底图不存在。", status_code=404)
        return json.loads(json.dumps(value))

    def validate_ready_graph(
        self,
        graph_id: str,
        *,
        source_connector_id: str = "",
        source_snapshot_id: str = "",
        scope: str | None = None,
    ) -> dict[str, Any]:
        graph = self.get_graph(graph_id)
        if graph.get("status") != "ready":
            raise PlatformError("CODE_GRAPH_NOT_READY", "代码知识底图尚未构建完成。", status_code=409, details={"status": graph.get("status")})
        if source_connector_id and graph.get("source_connector_id") != source_connector_id:
            raise PlatformError("CODE_GRAPH_SOURCE_MISMATCH", "代码知识底图不属于当前代码仓。", status_code=409)
        if source_snapshot_id and graph.get("source_snapshot_id") != source_snapshot_id:
            raise PlatformError("CODE_GRAPH_SNAPSHOT_MISMATCH", "代码知识底图与当前源码快照不一致。", status_code=409)
        if scope is not None and _normalize_subpath(str(graph.get("scope") or "")) != _normalize_subpath(scope):
            raise PlatformError("CODE_GRAPH_SCOPE_MISMATCH", "代码知识底图与当前分析目录不一致。", status_code=409)
        database = self.database_root / f"{graph_id}.sqlite3"
        if not database.is_file():
            raise PlatformError("CODE_GRAPH_DATABASE_MISSING", "代码知识底图数据库缺失，请重新构建。", status_code=500)
        return graph

    def search(self, graph_id: str, *, query: str = "", limit: int = 30) -> dict[str, Any]:
        graph = self.validate_ready_graph(graph_id)
        bounded_limit = max(1, min(int(limit), 100))
        terms = [item.casefold() for item in re.findall(r"[A-Za-z0-9_:\u4e00-\u9fff]+", query) if item]
        database = self.database_root / f"{graph_id}.sqlite3"
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            where = ""
            parameters: list[Any] = []
            if terms:
                clauses = []
                for term in terms[:8]:
                    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    pattern = f"%{escaped}%"
                    clauses.append("(lower(s.qualified_name) LIKE ? ESCAPE '\\' OR lower(s.signature) LIKE ? ESCAPE '\\' OR lower(f.path) LIKE ? ESCAPE '\\')")
                    parameters.extend([pattern, pattern, pattern])
                # A user goal often mixes business and implementation terms.
                # Broad OR retrieval gives the Agent a bounded candidate set;
                # source reads remain the precision step.
                where = "WHERE " + " OR ".join(clauses)
            rows = connection.execute(
                f"""
                SELECT s.symbol_id, s.kind, s.name, s.qualified_name, s.signature,
                       s.line_start, s.line_end, s.action_score, f.path
                FROM symbols s JOIN files f ON f.file_id = s.file_id
                {where}
                ORDER BY s.action_score DESC, s.qualified_name, f.path, s.line_start
                LIMIT ?
                """,
                [*parameters, bounded_limit],
            ).fetchall()
            results: list[dict[str, Any]] = []
            for row in rows:
                symbol_id = str(row["symbol_id"])
                outgoing = [
                    dict(item)
                    for item in connection.execute(
                        "SELECT callee_text, line, resolved_symbol_id, resolution_status FROM calls WHERE caller_symbol_id = ? LIMIT 16",
                        (symbol_id,),
                    ).fetchall()
                ]
                incoming = [
                    dict(item)
                    for item in connection.execute(
                        """
                        SELECT c.line, c.resolution_status, caller.qualified_name AS caller, f.path
                        FROM calls c
                        LEFT JOIN symbols caller ON caller.symbol_id = c.caller_symbol_id
                        LEFT JOIN files f ON f.file_id = c.file_id
                        WHERE c.resolved_symbol_id = ? LIMIT 16
                        """,
                        (symbol_id,),
                    ).fetchall()
                ]
                results.append({**dict(row), "outgoing_calls": outgoing, "incoming_calls": incoming})
            trace_rows: list[dict[str, Any]] = []
            if terms:
                trace_clauses: list[str] = []
                trace_parameters: list[Any] = []
                for term in terms[:8]:
                    escaped = term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                    pattern = f"%{escaped}%"
                    trace_clauses.append("(lower(coalesce(t.label, '')) LIKE ? ESCAPE '\\' OR lower(t.macro) LIKE ? ESCAPE '\\')")
                    trace_parameters.extend([pattern, pattern])
                trace_rows = [
                    dict(item)
                    for item in connection.execute(
                        f"""
                        SELECT t.tracepoint_id, t.macro, t.label, t.label_kind, t.line, f.path,
                               s.qualified_name AS owner_symbol
                        FROM tracepoints t JOIN files f ON f.file_id = t.file_id
                        LEFT JOIN symbols s ON s.symbol_id = t.owner_symbol_id
                        WHERE {' OR '.join(trace_clauses)}
                        ORDER BY f.path, t.line LIMIT ?
                        """,
                        (*trace_parameters, bounded_limit),
                    ).fetchall()
                ]
            return {
                "schema_version": "module_code_graph_search/v1",
                "graph_id": graph_id,
                "query": query,
                "evidence_scope": "static_syntax_candidate",
                "symbols": results,
                "tracepoints": trace_rows,
                "result_count": len(results) + len(trace_rows),
                "warning": "调用边来自 Tree-sitter 语法和唯一名称候选解析，不证明编译期目标或运行时发生。",
            }
        finally:
            connection.close()

    def query_knowledge(self, graph_id: str, *, query: str = "", limit: int = 15) -> dict[str, Any]:
        """Return a bounded static navigation slice for Agent source analysis."""

        graph = self.validate_ready_graph(graph_id)
        bounded_limit = max(1, min(int(limit), 30))
        static_result = self.search(
            graph_id,
            query=query,
            limit=min(30, max(bounded_limit, 12)),
        )
        return {
            "schema_version": "code_knowledge_query/v1",
            "graph_id": graph_id,
            "source_connector_id": graph.get("source_connector_id"),
            "source_snapshot_id": graph.get("source_snapshot_id"),
            "scope": graph.get("scope") or "",
            "query": query,
            "provider": "platform-tree-sitter",
            "retrieval_status": "ready",
            "static_graph": static_result,
            "result_count": int(static_result.get("result_count") or 0),
            "retrieval": {
                "method": "bounded_static_symbol_call_trace_query/v1",
                "limit": bounded_limit,
            },
            "evidence_scope": "static_syntax_candidate",
            "guidance_zh": (
                "使用返回的符号、调用候选与 HiTrace 插桩候选定位相关源码，"
                "再通过原生源码读取工具确认接口契约、控制流、数据流和调用关系。"
            ),
        }

    def publish_report(
        self,
        graph_id: str,
        *,
        title: str,
        markdown: str,
        question: str = "",
        parent_report_id: str = "",
        agent_run_id: str = "",
    ) -> dict[str, Any]:
        graph = self.validate_ready_graph(graph_id)
        normalized_title = _text(title, 240)
        normalized_markdown = str(markdown or "").strip()
        if not normalized_title or not normalized_markdown:
            raise PlatformError(
                "CODE_KNOWLEDGE_REPORT_INVALID",
                "代码知识报告需要非空标题与 Markdown 内容。",
                status_code=422,
            )
        if len(normalized_markdown.encode("utf-8")) > 512 * 1024 or "\0" in normalized_markdown:
            raise PlatformError(
                "CODE_KNOWLEDGE_REPORT_TOO_LARGE",
                "代码知识报告必须是小于 512 KiB 的 UTF-8 Markdown。",
                status_code=413,
            )
        parent: dict[str, Any] | None = None
        normalized_parent_id = _text(parent_report_id, 80)
        if normalized_parent_id:
            parent = self.read_report(normalized_parent_id)
            if parent.get("graph_id") != graph_id:
                raise PlatformError(
                    "CODE_KNOWLEDGE_REPORT_PARENT_MISMATCH",
                    "父报告不属于当前代码知识底图。",
                    status_code=409,
                )
        connector = self.connectors.get(str(graph["source_connector_id"]))
        snapshot = _record(connector.get("snapshot"))
        source_binding = {
            "declared_revision": snapshot.get("snapshot_id"),
            "resolved_snapshot_id": snapshot.get("snapshot_id"),
            "match_level": "exact" if snapshot.get("clean") else "compatible",
            "resolver_version": "code-knowledge-report/1.0",
            "snapshot_state": "clean" if snapshot.get("clean") else "dirty",
        }
        graph_artifact_id = _text(graph.get("artifact_id"), 80)
        input_artifacts = [item for item in [graph_artifact_id, normalized_parent_id] if item]
        # Include logically deleted immutable reports so a later child never
        # reuses a historical revision number.
        existing_revisions = [
            int(item.get("revision") or 0)
            for item in self.artifact_store.list_artifacts(
                "code_knowledge_report",
                limit=500,
                source_connector_id=str(graph["source_connector_id"]),
            )
            if item.get("graph_id") == graph_id
        ]
        next_revision = max(existing_revisions, default=0) + 1
        artifact = self.artifact_store.write(
            "code_knowledge_report",
            {
                "report_contract": REPORT_CONTRACT,
                "graph_id": graph_id,
                "scope": str(graph.get("scope") or ""),
                "source_snapshot_id": str(graph.get("source_snapshot_id") or ""),
                "title": normalized_title,
                "markdown": normalized_markdown,
                "question": _text(question, 8000),
                "parent_report_id": normalized_parent_id or None,
                "revision": next_revision,
                "agent_run_id": _text(agent_run_id, 120) or None,
                "knowledge_engine": {
                    "engine_id": "platform-tree-sitter",
                    "graph_version": BUILDER_VERSION,
                },
                "evidence_scope": "agent_semantic_analysis_over_static_code_graph",
                "claim_boundary_zh": (
                    "Tree-sitter 与 SQLite 提供静态符号、调用和插桩导航候选；"
                    "Agent 通过有界查询与引用源码形成架构意图、调用语义和 Mermaid 解释。"
                ),
            },
            source_connector_id=str(graph["source_connector_id"]),
            source_binding=source_binding,
            producer={"name": "code-knowledge-agent", "version": "1.0.0"},
            input_artifacts=input_artifacts,
            schema_version="1.0",
        )
        markdown_path = self.report_root / f"{artifact['artifact_id']}.md"
        if not markdown_path.exists():
            _atomic_write_text(markdown_path, normalized_markdown)
        self._mutate(graph_id, {
            "semantic_analysis": {
                "status": "completed",
                "request_id": _text(_record(graph.get("semantic_analysis")).get("request_id"), 120) or None,
                "completed_at": utc_now(),
                "report_id": artifact["artifact_id"],
                "agent_run_id": _text(agent_run_id, 120) or None,
                "knowledge_engine_id": "platform-tree-sitter",
            },
            "updated_at": utc_now(),
        })
        return {**artifact, "markdown_file": markdown_path.name}

    def list_reports(
        self,
        *,
        graph_id: str = "",
        source_connector_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        reports = self.artifact_store.list_artifacts(
            "code_knowledge_report",
            limit=500,
            source_connector_id=source_connector_id or None,
        )
        deleted_report_ids = set(_record(self.registry.read().get("deleted_report_ids")))
        reports = [item for item in reports if item.get("artifact_id") not in deleted_report_ids]
        if graph_id:
            reports = [item for item in reports if item.get("graph_id") == graph_id]
        reports.sort(
            key=lambda item: (
                str(item.get("created_at") or ""),
                int(item.get("revision") or 0),
                str(item.get("artifact_id") or ""),
            ),
            reverse=True,
        )
        compact: list[dict[str, Any]] = []
        for report in reports[: max(1, min(limit, 500))]:
            compact.append({key: value for key, value in report.items() if key != "markdown"})
        return compact

    def read_report(self, report_id: str) -> dict[str, Any]:
        if not ARTIFACT_ID_PATTERN.fullmatch(report_id):
            raise PlatformError("CODE_KNOWLEDGE_REPORT_NOT_FOUND", "代码知识报告 ID 非法。", status_code=404)
        if report_id in _record(self.registry.read().get("deleted_report_ids")):
            raise PlatformError("CODE_KNOWLEDGE_REPORT_NOT_FOUND", "代码知识报告不存在。", status_code=404)
        try:
            report = self.artifact_store.read("code_knowledge_report", report_id)
        except PlatformError as exc:
            raise PlatformError("CODE_KNOWLEDGE_REPORT_NOT_FOUND", "代码知识报告不存在。", status_code=404) from exc
        markdown_path = self.report_root / f"{report_id}.md"
        if not markdown_path.is_file():
            _atomic_write_text(markdown_path, str(report.get("markdown") or ""))
        return {**report, "markdown_file": markdown_path.name}

    def delete_report(self, report_id: str) -> dict[str, Any]:
        report = self.read_report(report_id)
        deleted_at = utc_now()
        with self._lock:
            registry = self.registry.read()
            deleted_reports = _record(registry.setdefault("deleted_report_ids", {}))
            deleted_reports[report_id] = {
                "deleted_at": deleted_at,
                "graph_id": report.get("graph_id"),
                "cascade": False,
            }
            registry["deleted_report_ids"] = deleted_reports
            self.registry.write(registry)
        graph_id = str(report.get("graph_id") or "")
        try:
            graph = self.get_graph(graph_id)
        except PlatformError:
            graph = {}
        if _record(graph.get("semantic_analysis")).get("report_id") == report_id:
            remaining = self.list_reports(graph_id=graph_id, limit=1)
            self._mutate(graph_id, {
                "semantic_analysis": {
                    "status": "completed" if remaining else "not_started",
                    "report_id": remaining[0].get("artifact_id") if remaining else None,
                    "updated_at": deleted_at,
                },
                "updated_at": deleted_at,
            })
        self.artifact_store.append_event({
            "event_type": "code_knowledge_report_deleted",
            "artifact_id": report_id,
            "graph_id": report.get("graph_id"),
            "created_at": deleted_at,
        })
        return {
            "status": "deleted",
            "report_id": report_id,
            "graph_id": report.get("graph_id"),
            "deleted_at": deleted_at,
            "retention": "immutable_artifact_preserved",
        }

    def report_markdown_path(self, report_id: str) -> Path:
        self.read_report(report_id)
        path = self.report_root / f"{report_id}.md"
        if not path.is_file():
            raise PlatformError("CODE_KNOWLEDGE_REPORT_FILE_MISSING", "代码知识报告 Markdown 文件缺失。", status_code=500)
        return path
