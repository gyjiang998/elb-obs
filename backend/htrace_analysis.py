"""Bounded, evidence-preserving analysis for raw OpenHarmony trace files.

The authoritative parser for both OHOSPROF protobuf containers and textual
ftrace captures is TraceStreamer.  This adapter deliberately does not reverse
engineer either format itself: it identifies the input from a bounded prefix,
runs a configured/local TraceStreamer without a shell, then performs read-only,
schema-tolerant SQL extraction on the derived SQLite database.

Phase A emits basic span and scheduler candidates only.  It does not infer a
critical path, resolve trace symbols to source locations, or claim GPU work when
the converted database has no corresponding evidence.
"""

from __future__ import annotations

import hashlib
import os
import platform
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from .core import PlatformError, file_lock, sha256_file
from .hitrace_instrumentation import describe_hitrace_event
from .platform_paths import runtime_asset_root
from .trace_modules import classify_trace_event, known_trace_module_keys, summarize_trace_modules


HTRACE_MAGIC = b"OHOSPROF"
FTRACE_HEADER_MARKERS = (b"# tracer:", b"# entries-in-buffer/entries-written:")
FRAME_WINDOW_NS = 20_000_000
MAX_SPAN_GROUPS = 24
MAX_SCHEDULER_CATALOG_CLUSTERS = 2_000
MAX_SCHEDULER_WAKEUP_ROWS = 500_000
MAX_CLUSTER_ROOT_EVENT_IDS = 32
MAX_CLUSTER_EVENT_CANDIDATES = 24
MAX_SELF_TIME_EVENT_CANDIDATES = 100
MAX_SELF_TIME_EVENT_GROUP_SCAN = 50_000
MAX_EVENT_OCCURRENCE_REFS = 8
MAX_RUNTIME_EVENT_NAME_CANDIDATES = 2_000
TRACE_STREAMER_TIMEOUT_SECONDS = 300
TRACE_STREAMER_ENV = "HETERO_TRACE_STREAMER"
TRACE_STREAMER_METADATA_ALLOWLIST = {
    "datasize",
    "parse_tool",
    "tool_version",
    "tool_publish_time",
    "trace_duration",
    "source_type",
}


def _compact_text(value: Any, *, limit: int = 512) -> str:
    text = str(value or "unknown").strip() or "unknown"
    return text if len(text) <= limit else f"{text[: limit - 3]}..."


def _number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _temporal_pressure(avg_ms: float, max_ms: float, call_count: int) -> tuple[str, list[str]]:
    notes = ["Pressure is derived from converted callstack span durations, not aggregate trace time alone."]
    if max_ms >= 16.6 or avg_ms >= 8.0:
        notes.append("At least one observed invocation approaches or exceeds a frame-sized budget.")
        return "high", notes
    if max_ms >= 4.0 or avg_ms >= 2.0:
        notes.append("Per-invocation latency is material, but frame alignment and criticality are not proven.")
        return "medium", notes
    if call_count:
        notes.append("Aggregate duration is retained as evidence; low per-invocation pressure is not a hotspot verdict.")
    return "low", notes


def _dominance(process_name: str, function_name: str) -> str:
    text = f"{process_name} {function_name}".casefold()
    if any(token in text for token in ("binder", "fence", "wait", "bufferqueue", "queue", "reply", "ipc")):
        return "blocking_dominant"
    if any(token in text for token in ("driver", "gpu", "hwc", "composer", "present")):
        return "driver_wait_dominant"
    if any(token in text for token in ("render", "paint", "decode", "filter", "blur", "compute", "encode")):
        return "compute_dominant"
    return "unknown"


def _binary_name() -> str:
    system = platform.system().casefold()
    if system == "darwin":
        return "trace_streamer_mac"
    if system == "linux":
        return "trace_streamer_linux"
    if system == "windows":
        return "trace_streamer_windows.exe"
    raise PlatformError(
        "TRACE_STREAMER_UNAVAILABLE",
        f"No TraceStreamer platform mapping exists for {platform.system()}.",
        status_code=503,
    )


def _ensure_executable(path: Path) -> Path:
    if not path.is_file():
        raise PlatformError(
            "TRACE_STREAMER_UNAVAILABLE",
            "Configured TraceStreamer executable does not exist.",
            status_code=503,
            details={"path": str(path)},
        )
    if platform.system().casefold() != "windows":
        mode = path.stat().st_mode
        if not mode & stat.S_IXUSR:
            path.chmod(mode | stat.S_IXUSR)
    if not os.access(path, os.X_OK):
        raise PlatformError(
            "TRACE_STREAMER_UNAVAILABLE",
            "Configured TraceStreamer is not executable.",
            status_code=503,
            details={"path": str(path)},
        )
    return path.resolve()


def resolve_trace_streamer(*, state_root: Path, workspace_root: Path) -> dict[str, Any]:
    """Resolve a TraceStreamer executable with immutable provenance metadata.

    ``HETERO_TRACE_STREAMER`` is the preferred deployment path. A runtime asset
    provisioned below ``HETERO_RUNTIME_ASSET_ROOT`` is the only fallback; this
    repository never reaches into a sibling checkout or downloads a binary.
    """

    configured = os.environ.get(TRACE_STREAMER_ENV)
    if configured:
        executable = _ensure_executable(Path(configured).expanduser())
        return {
            "path": executable,
            "binary_name": executable.name,
            "binary_sha256": sha256_file(executable),
            "resolution": "environment",
        }

    executable = runtime_asset_root() / "trace-streamer" / _binary_name()
    if not executable.is_file():
        raise PlatformError(
            "TRACE_STREAMER_UNAVAILABLE",
            f"Set {TRACE_STREAMER_ENV} or provision a platform TraceStreamer binary.",
            status_code=503,
            details={
                "environment_variable": TRACE_STREAMER_ENV,
                "runtime_asset_path": str(executable),
            },
        )
    executable = _ensure_executable(executable)
    return {
        "path": executable,
        "binary_name": executable.name,
        "binary_sha256": sha256_file(executable),
        "resolution": "runtime_asset",
    }


def detect_htrace_format(path: Path) -> str:
    try:
        with path.open("rb") as handle:
            prefix = handle.read(4096)
    except OSError as exc:
        raise PlatformError(
            "HTRACE_READ_FAILED",
            "Raw .htrace file cannot be read.",
            status_code=422,
            details={"path": str(path), "reason": str(exc)},
        ) from exc
    if prefix.startswith(HTRACE_MAGIC):
        return "ohosprof_protobuf"
    stripped = prefix.lstrip(b"\xef\xbb\xbf\r\n\t ")
    if any(stripped.startswith(marker) for marker in FTRACE_HEADER_MARKERS):
        return "ftrace_text"
    if b"tracing_mark_write:" in prefix or b"sched_switch:" in prefix or b"sched_wakeup:" in prefix:
        return "ftrace_text"
    raise PlatformError(
        "HTRACE_HEADER_INVALID",
        "Raw trace is neither an OHOSPROF container nor a recognizable textual ftrace capture.",
        status_code=422,
        details={
            "accepted_formats": ["ohosprof_protobuf", "ftrace_text"],
            "expected_binary_magic": HTRACE_MAGIC.decode("ascii"),
        },
    )


def validate_htrace_header(path: Path) -> str:
    """Validate and return the TraceStreamer input format.

    The historical function name is retained because callers and tests use it.
    """

    return detect_htrace_format(path)


def _sqlite_is_readable(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        try:
            connection.execute("SELECT name FROM sqlite_master LIMIT 1").fetchone()
        finally:
            connection.close()
        return True
    except sqlite3.Error:
        return False


def _conversion_paths(state_root: Path, trace_sha256: str, tool_sha256: str) -> dict[str, Path]:
    root = state_root / "derived-traces" / trace_sha256 / f"trace_streamer_{tool_sha256[:16]}"
    return {
        "root": root,
        "database": root / "trace_streamer.db",
        "stdout": root / "trace_streamer.stdout.log",
        "stderr": root / "trace_streamer.stderr.log",
    }


def _relative_to_state(path: Path, state_root: Path) -> str:
    try:
        return str(path.resolve().relative_to(state_root.resolve()))
    except ValueError:
        return str(path)


def convert_htrace(
    path: Path,
    *,
    trace_sha256: str,
    state_root: Path,
    workspace_root: Path,
) -> dict[str, Any]:
    """Convert an OHOSPROF or textual ftrace input into a cached SQLite DB."""

    path = path.resolve()
    input_format = validate_htrace_header(path)
    tool = resolve_trace_streamer(state_root=state_root, workspace_root=workspace_root)
    paths = _conversion_paths(state_root, trace_sha256, str(tool["binary_sha256"]))
    root = paths["root"]
    root.mkdir(parents=True, exist_ok=True)
    database = paths["database"]
    # The cache identity includes both input and converter binary.  Serializing one
    # entry prevents competing imports from interleaving logs or replacing each
    # other's derived DB while retaining cross-input parallelism.
    with file_lock(root / ".conversion.lock"):
        if not _sqlite_is_readable(database):
            required_bytes = max(512 * 1024 * 1024, path.stat().st_size * 3)
            available_bytes = shutil.disk_usage(root).free
            if available_bytes < required_bytes:
                raise PlatformError(
                    "TRACE_DISK_SPACE_INSUFFICIENT",
                    "Insufficient local disk capacity for raw .htrace conversion output.",
                    status_code=413,
                    details={"available_bytes": available_bytes, "required_bytes": required_bytes},
                )
            descriptor, temporary_name = tempfile.mkstemp(prefix=".trace-streamer-", suffix=".db", dir=root)
            os.close(descriptor)
            temporary_database = Path(temporary_name)
            temporary_database.unlink(missing_ok=True)
            temporary_sidecar = Path(f"{temporary_database}.ohos.ts")
            command = [str(tool["path"]), str(path), "-e", str(temporary_database)]
            try:
                with paths["stdout"].open("wb") as stdout, paths["stderr"].open("wb") as stderr:
                    completed = subprocess.run(
                        command,
                        cwd=root,
                        stdin=subprocess.DEVNULL,
                        stdout=stdout,
                        stderr=stderr,
                        check=False,
                        timeout=TRACE_STREAMER_TIMEOUT_SECONDS,
                    )
            except subprocess.TimeoutExpired as exc:
                raise PlatformError(
                    "TRACE_STREAMER_TIMEOUT",
                    "TraceStreamer exceeded the raw .htrace conversion time limit.",
                    status_code=504,
                    details={
                        "timeout_seconds": TRACE_STREAMER_TIMEOUT_SECONDS,
                        "stdout_log": _relative_to_state(paths["stdout"], state_root),
                        "stderr_log": _relative_to_state(paths["stderr"], state_root),
                    },
                ) from exc
            except OSError as exc:
                raise PlatformError(
                    "TRACE_STREAMER_FAILED",
                    "TraceStreamer could not be started for this raw .htrace.",
                    status_code=502,
                    details={"reason": str(exc)},
                ) from exc
            finally:
                if not _sqlite_is_readable(temporary_database):
                    temporary_database.unlink(missing_ok=True)
                    temporary_sidecar.unlink(missing_ok=True)

            if completed.returncode != 0:
                temporary_database.unlink(missing_ok=True)
                temporary_sidecar.unlink(missing_ok=True)
                raise PlatformError(
                    "TRACE_STREAMER_FAILED",
                    "TraceStreamer failed to convert the raw .htrace.",
                    status_code=422,
                    details={
                        "exit_code": completed.returncode,
                        "stdout_log": _relative_to_state(paths["stdout"], state_root),
                        "stderr_log": _relative_to_state(paths["stderr"], state_root),
                    },
                )
            if not _sqlite_is_readable(temporary_database):
                raise PlatformError(
                    "TRACE_DB_INVALID",
                    "TraceStreamer completed without producing a readable SQLite database.",
                    status_code=422,
                    details={
                        "stdout_log": _relative_to_state(paths["stdout"], state_root),
                        "stderr_log": _relative_to_state(paths["stderr"], state_root),
                    },
                )
            os.replace(temporary_database, database)
            temporary_sidecar.unlink(missing_ok=True)

    return {
        "input_format": input_format,
        "converter": {
            "name": "TraceStreamer",
            "binary_name": tool["binary_name"],
            "binary_sha256": tool["binary_sha256"],
            "resolution": tool["resolution"],
            **({"bootstrap_archive_sha256": tool["bootstrap_archive_sha256"]} if tool.get("bootstrap_archive_sha256") else {}),
        },
        "cache_key": f"{trace_sha256}:{tool['binary_sha256']}",
        "database": {
            "managed_path": _relative_to_state(database, state_root),
            "sha256": sha256_file(database),
            "bytes": database.stat().st_size,
            "stdout_log": _relative_to_state(paths["stdout"], state_root),
            "stderr_log": _relative_to_state(paths["stderr"], state_root),
        },
    }


def _open_readonly_database(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _quoted_identifier(value: str) -> str:
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def _schema(connection: sqlite3.Connection) -> dict[str, list[str]]:
    table_names = [str(row["name"]) for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    return {
        table_name: [str(row["name"]) for row in connection.execute(f"PRAGMA table_info({_quoted_identifier(table_name)})")]
        for table_name in table_names
    }


def _count_rows(connection: sqlite3.Connection, table_name: str) -> int:
    try:
        return int(connection.execute(f"SELECT COUNT(*) AS count FROM {_quoted_identifier(table_name)}").fetchone()["count"])
    except sqlite3.Error:
        return 0


def _trace_range(connection: sqlite3.Connection, schema: dict[str, list[str]]) -> tuple[int | None, int | None, str]:
    if {"start_ts", "end_ts"}.issubset(schema.get("trace_range", [])):
        row = connection.execute("SELECT MIN(start_ts) AS start_ns, MAX(end_ts) AS end_ns FROM trace_range").fetchone()
        if row and row["start_ns"] is not None and row["end_ns"] is not None and int(row["end_ns"]) >= int(row["start_ns"]):
            return int(row["start_ns"]), int(row["end_ns"]), "trace_range"
    if {"ts", "dur"}.issubset(schema.get("callstack", [])):
        row = connection.execute("SELECT MIN(ts) AS start_ns, MAX(ts + dur) AS end_ns FROM callstack WHERE dur >= 0").fetchone()
        if row and row["start_ns"] is not None and row["end_ns"] is not None:
            return int(row["start_ns"]), int(row["end_ns"]), "callstack_inferred"
    return None, None, "unavailable"


def _capabilities(schema: dict[str, list[str]]) -> dict[str, dict[str, Any]]:
    def present(table_name: str, required: set[str]) -> dict[str, Any]:
        columns = set(schema.get(table_name, []))
        missing = sorted(required - columns)
        return {
            "available": not missing,
            "table": table_name,
            "missing_columns": missing,
        }

    return {
        "callstack_spans": present("callstack", {"id", "ts", "dur", "name", "callid"}),
        "callstack_parent_links": present("callstack", {"id", "parent_id"}),
        "process_thread_map": {
            "available": {"itid", "name", "ipid"}.issubset(schema.get("thread", [])) and {"ipid", "name"}.issubset(schema.get("process", [])),
            "tables": ["thread", "process"],
        },
        "sched_slices": present("sched_slice", {"ts", "dur", "cpu", "itid"}),
        "thread_states": present("thread_state", {"ts", "dur", "state", "itid"}),
        "scheduler_wakeups": present("raw", {"ts", "name"}),
        "gpu_slices": present("gpu_slice", {"dur"}),
        "dma_fence": present("dma_fence", {"ts", "dur"}),
        "frame_slices": present("frame_slice", {"ts", "dur"}),
    }


def _event_counts(connection: sqlite3.Connection, schema: dict[str, list[str]]) -> dict[str, int]:
    if not {"name"}.issubset(schema.get("raw", [])):
        return {}
    rows = connection.execute("SELECT name, COUNT(*) AS count FROM raw GROUP BY name ORDER BY count DESC LIMIT 80").fetchall()
    return {_compact_text(row["name"], limit=160): int(row["count"]) for row in rows}


def _database_meta(connection: sqlite3.Connection, schema: dict[str, list[str]]) -> dict[str, str]:
    if not {"name", "value"}.issubset(schema.get("meta", [])):
        return {}
    rows = connection.execute("SELECT name, value FROM meta").fetchall()
    return {
        key: _compact_text(row["value"], limit=512)
        for row in rows
        if (key := _compact_text(row["name"], limit=100)) in TRACE_STREAMER_METADATA_ALLOWLIST
    }


def _span_groups(connection: sqlite3.Connection, schema: dict[str, list[str]]) -> list[dict[str, Any]]:
    """Group spans without assuming every TraceStreamer schema has every join field."""

    callstack_columns = set(schema.get("callstack", []))
    thread_columns = set(schema.get("thread", []))
    process_columns = set(schema.get("process", []))
    has_thread_map = {"itid", "name"}.issubset(thread_columns)
    has_process_map = has_thread_map and {"ipid", "name"}.issubset(process_columns) and "ipid" in thread_columns
    has_parent_links = {"id", "parent_id"}.issubset(callstack_columns)

    if has_thread_map:
        thread_join = "LEFT JOIN thread t ON c.callid = t.itid"
        thread_name = "COALESCE(t.name, 'unknown')"
        thread_tid = "COALESCE(t.tid, -1)" if "tid" in thread_columns else "CAST(-1 AS INTEGER)"
        thread_itid = "COALESCE(t.itid, c.callid, -1)"
    else:
        thread_join = ""
        thread_name = "'unknown'"
        thread_tid = "CAST(-1 AS INTEGER)"
        thread_itid = "COALESCE(c.callid, -1)"

    if has_process_map:
        process_join = "LEFT JOIN process p ON t.ipid = p.ipid"
        process_name = "COALESCE(p.name, 'unknown')"
        process_pid = "COALESCE(p.pid, -1)" if "pid" in process_columns else "CAST(-1 AS INTEGER)"
    else:
        process_join = ""
        process_name = "'unknown'"
        process_pid = "CAST(-1 AS INTEGER)"

    if has_parent_links:
        child_cte = """
            WITH child_duration AS (
                SELECT parent_id, SUM(dur) AS total_child_duration
                FROM callstack
                WHERE dur > 0
                GROUP BY parent_id
            )
        """
        child_join = "LEFT JOIN child_duration ON c.id = child_duration.parent_id"
        exclusive_estimate = """
            SUM(CASE WHEN c.dur > COALESCE(child_duration.total_child_duration, 0)
                THEN c.dur - COALESCE(child_duration.total_child_duration, 0) ELSE 0 END)
        """
    else:
        child_cte = ""
        child_join = ""
        exclusive_estimate = "NULL"

    rows = connection.execute(
        f"""
        {child_cte}
        SELECT
            {process_name} AS process_name,
            {process_pid} AS process_pid,
            {thread_name} AS thread_name,
            {thread_tid} AS thread_tid,
            {thread_itid} AS thread_itid,
            COALESCE(c.callid, -1) AS callid,
            c.name AS function_name,
            COUNT(*) AS call_count,
            SUM(c.dur) AS inclusive_total_ns,
            {exclusive_estimate} AS exclusive_estimate_total_ns,
            AVG(c.dur) AS avg_ns,
            MAX(c.dur) AS max_ns
        FROM callstack c
        {thread_join}
        {process_join}
        {child_join}
        WHERE c.dur > 0 AND TRIM(COALESCE(c.name, '')) != ''
        GROUP BY c.name, COALESCE(c.callid, -1), {process_name}, {process_pid},
                 {thread_name}, {thread_tid}, {thread_itid}
        ORDER BY MAX(c.dur) DESC, exclusive_estimate_total_ns DESC, inclusive_total_ns DESC
        LIMIT ?
        """,
        (MAX_SPAN_GROUPS,),
    ).fetchall()
    groups: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        parent_id = "c.parent_id" if has_parent_links else "NULL"
        peak = connection.execute(
            f"""
            SELECT c.id, c.ts, c.dur, {parent_id} AS parent_id
            FROM callstack c
            {thread_join}
            {process_join}
            WHERE COALESCE(c.callid, -1) = ? AND c.name = ?
              AND {process_name} = ?
              AND {thread_name} = ?
            ORDER BY c.dur DESC, c.ts ASC, c.id ASC
            LIMIT 1
            """,
            (item["callid"], item["function_name"], item["process_name"], item["thread_name"]),
        ).fetchone()
        if peak is None:
            continue
        item["peak_event_id"] = int(peak["id"])
        item["peak_ts_ns"] = int(peak["ts"])
        item["peak_duration_ns"] = int(peak["dur"])
        parent_event_id = int(peak["parent_id"]) if peak["parent_id"] is not None else None
        # TraceStreamer represents a root span with an unsigned -1 sentinel.
        item["parent_event_id"] = None if parent_event_id == 4_294_967_295 else parent_event_id
        item["exclusive_estimate_available"] = has_parent_links
        groups.append(item)
    return groups


def _span_candidates(
    groups: list[dict[str, Any]],
    *,
    trace_sha256: str,
    trace_start_ns: int | None,
    trace_end_ns: int | None,
    clock_quality: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    clusters: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    trace_window_ms = None
    if trace_start_ns is not None and trace_end_ns is not None:
        trace_window_ms = max(0.0, (trace_end_ns - trace_start_ns) / 1_000_000.0)

    for rank, group in enumerate(groups, start=1):
        peak_id = int(group["peak_event_id"])
        cluster_id = f"cluster_htrace_{trace_sha256[:10]}_{peak_id}"
        peak_start_ns = int(group["peak_ts_ns"])
        peak_end_ns = peak_start_ns + max(1, int(group["peak_duration_ns"]))
        function_name = _compact_text(group["function_name"])
        process_name = _compact_text(group["process_name"], limit=160)
        thread_name = _compact_text(group["thread_name"], limit=160)
        module_summary = summarize_trace_modules(
            (function_name, process_name, thread_name),
            fallback_top_function=function_name,
        )
        avg_ms = _number(group["avg_ns"]) / 1_000_000.0
        max_ms = _number(group["max_ns"]) / 1_000_000.0
        inclusive_total_ms = _number(group["inclusive_total_ns"]) / 1_000_000.0
        exclusive_estimate_ms = (
            _number(group["exclusive_estimate_total_ns"]) / 1_000_000.0
            if group.get("exclusive_estimate_total_ns") is not None
            else None
        )
        call_count = int(group["call_count"])
        pressure, pressure_notes = _temporal_pressure(avg_ms, max_ms, call_count)
        thread_ref = {
            "itid": int(group["thread_itid"]),
            "tid": int(group["thread_tid"]),
            "task": thread_name,
            "process": process_name,
            "pid": int(group["process_pid"]),
        }
        clusters.append(
            {
                "cluster_id": cluster_id,
                "time_range_ns": {"start_ns": peak_start_ns, "end_ns": peak_end_ns},
                "root_event_ids": [f"callstack:{peak_id}"],
                "thread_refs": [thread_ref],
                "edge_summary": {
                    "kind": "callstack_peak_span_candidate",
                    "edge_count": 1 if group.get("parent_event_id") not in (None, 4294967295) else 0,
                    "peak_event_id": peak_id,
                    "parent_event_id": group.get("parent_event_id"),
                    "span_count_for_group": call_count,
                    "peak_span_ms": round(max_ms, 6),
                    "inclusive_total_duration_ms": round(inclusive_total_ms, 6),
                    "exclusive_estimate_total_duration_ms": round(exclusive_estimate_ms, 6) if exclusive_estimate_ms is not None else None,
                    "exclusive_estimate_method": "direct_child_duration_subtraction" if group.get("exclusive_estimate_available") else "unavailable_without_parent_links",
                },
                "module_summary": module_summary,
                "clock_quality": clock_quality,
                "profile_id": "htrace-basic-span-v1",
                "selection_state": "pending",
                "artifact_ref": f"sha256:{trace_sha256}",
                "analysis_scope": "basic_span_candidate_not_critical_path",
            }
        )
        candidates.append(
            {
                "hotspot_id": f"hs_htrace_{rank:03d}_{peak_id}",
                "cluster_id": cluster_id,
                "function_ref": {
                    "symbol": function_name,
                    "module": process_name,
                    "process": process_name,
                    "thread": thread_name,
                    "trace_event_id": f"callstack:{peak_id}",
                    "resolution": "raw_htrace_symbol_only",
                },
                "unresolved_reason": "Raw .htrace spans do not carry a source file and line mapping; source resolution remains blocked until a trace-to-source binding artifact is supplied.",
                "active_time_metrics": {
                    "inclusive_total_duration_ms": round(inclusive_total_ms, 6),
                    "exclusive_estimate_total_duration_ms": round(exclusive_estimate_ms, 6) if exclusive_estimate_ms is not None else None,
                    "exclusive_estimate_method": "direct_child_duration_subtraction" if group.get("exclusive_estimate_available") else "unavailable_without_parent_links",
                    "avg_duration_ms": round(avg_ms, 6),
                    "max_duration_ms": round(max_ms, 6),
                    "call_count": call_count,
                    "aggregation_scope": "trace_span_group",
                    "trace_window_ms": trace_window_ms,
                    "peak_occurrence": {
                        "event_id": f"callstack:{peak_id}",
                        "start_ns": peak_start_ns,
                        "end_ns": peak_end_ns,
                    },
                },
                "temporal_pressure": pressure,
                "temporal_pressure_notes": pressure_notes,
                "critical_path_metrics": {
                    "available": False,
                    "reason": "Raw .htrace basic extraction does not reconstruct a critical path.",
                },
                "dominance_label": _dominance(process_name, function_name),
                "evidence_refs": [
                    f"sha256:{trace_sha256}",
                    f"trace_event:callstack:{peak_id}",
                    f"cluster:{cluster_id}",
                ],
                "external_rank": rank,
            }
        )
    return clusters, candidates


def _scheduler_wakeup_candidates(
    connection: sqlite3.Connection,
    schema: dict[str, list[str]],
    *,
    trace_sha256: str,
    clock_quality: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Build a bounded wakeup-window catalog with endpoint-derived modules.

    TraceStreamer exposes sched_wakeup in ``raw`` and, on current OHOS traces,
    exposes the source thread through the matching ``instant`` record.  The
    joins below are optional by design: absent fields degrade to a task/itid
    label rather than inventing a source-function or critical-path claim.
    """

    raw_columns = set(schema.get("raw", []))
    instant_columns = set(schema.get("instant", []))
    thread_columns = set(schema.get("thread", []))
    process_columns = set(schema.get("process", []))
    if not {"ts", "name"}.issubset(raw_columns):
        return [], [], {
            "available": False,
            "reason": "raw.ts and raw.name are required for scheduler wakeup extraction.",
        }

    has_raw_itid = "itid" in raw_columns
    has_thread_map = has_raw_itid and {"itid", "name"}.issubset(thread_columns)
    has_process_map = has_thread_map and "ipid" in thread_columns and {"ipid", "name"}.issubset(process_columns)
    has_instant_edge_map = has_raw_itid and {"ts", "name", "ref", "wakeup_from"}.issubset(instant_columns)
    raw_id = "r.id" if "id" in raw_columns else "r.rowid"
    target_itid = "COALESCE(r.itid, -1)" if has_raw_itid else "CAST(-1 AS INTEGER)"

    target_thread_join = "LEFT JOIN thread target_thread ON target_thread.itid = r.itid" if has_thread_map else ""
    target_thread_name = "COALESCE(target_thread.name, 'unknown')" if has_thread_map else "'unknown'"
    target_thread_tid = "COALESCE(target_thread.tid, -1)" if has_thread_map and "tid" in thread_columns else "CAST(-1 AS INTEGER)"
    target_process_join = "LEFT JOIN process target_process ON target_process.ipid = target_thread.ipid" if has_process_map else ""
    target_process_name = "COALESCE(target_process.name, 'unknown')" if has_process_map else "'unknown'"
    target_process_pid = "COALESCE(target_process.pid, -1)" if has_process_map and "pid" in process_columns else "CAST(-1 AS INTEGER)"

    if has_instant_edge_map:
        instant_join = """
        LEFT JOIN (
            SELECT ts, ref, MAX(wakeup_from) AS wakeup_from
            FROM instant
            WHERE name IN ('sched_wakeup', 'sched_wakeup_new')
            GROUP BY ts, ref
        ) wakeup_instant ON wakeup_instant.ts = r.ts AND wakeup_instant.ref = r.itid
        """
        source_itid = "COALESCE(wakeup_instant.wakeup_from, -1)"
        source_thread_join = "LEFT JOIN thread source_thread ON source_thread.itid = wakeup_instant.wakeup_from" if has_thread_map else ""
        source_thread_name = "COALESCE(source_thread.name, 'unknown')" if has_thread_map else "'unknown'"
        source_thread_tid = "COALESCE(source_thread.tid, -1)" if has_thread_map and "tid" in thread_columns else "CAST(-1 AS INTEGER)"
        source_process_join = "LEFT JOIN process source_process ON source_process.ipid = source_thread.ipid" if has_process_map else ""
        source_process_name = "COALESCE(source_process.name, 'unknown')" if has_process_map else "'unknown'"
        source_process_pid = "COALESCE(source_process.pid, -1)" if has_process_map and "pid" in process_columns else "CAST(-1 AS INTEGER)"
    else:
        instant_join = ""
        source_itid = "CAST(-1 AS INTEGER)"
        source_thread_join = ""
        source_thread_name = "'unknown'"
        source_thread_tid = "CAST(-1 AS INTEGER)"
        source_process_join = ""
        source_process_name = "'unknown'"
        source_process_pid = "CAST(-1 AS INTEGER)"

    total_wakeups = int(
        connection.execute(
            "SELECT COUNT(*) AS count FROM raw WHERE name IN ('sched_wakeup', 'sched_wakeup_new')"
        ).fetchone()["count"]
    )
    rows = connection.execute(
        f"""
        SELECT
            r.ts AS ts,
            {raw_id} AS event_id,
            {target_itid} AS target_itid,
            {target_thread_tid} AS target_tid,
            {target_thread_name} AS target_thread_name,
            {target_process_pid} AS target_pid,
            {target_process_name} AS target_process_name,
            {source_itid} AS source_itid,
            {source_thread_tid} AS source_tid,
            {source_thread_name} AS source_thread_name,
            {source_process_pid} AS source_pid,
            {source_process_name} AS source_process_name
        FROM raw r
        {instant_join}
        {target_thread_join}
        {target_process_join}
        {source_thread_join}
        {source_process_join}
        WHERE r.name IN ('sched_wakeup', 'sched_wakeup_new')
        ORDER BY r.ts ASC, {raw_id} ASC
        LIMIT ?
        """,
        (MAX_SCHEDULER_WAKEUP_ROWS,),
    )

    def compact_identity(process_name: Any, thread_name: Any, itid: Any) -> str:
        process = _compact_text(process_name, limit=160)
        thread = _compact_text(thread_name, limit=160)
        if process != "unknown" and thread != "unknown" and process != thread:
            return f"{process}/{thread}"
        if thread != "unknown":
            return thread
        if process != "unknown":
            return process
        return f"itid:{int(itid)}" if _number(itid) >= 0 else "unknown"

    def endpoint_ref(row: sqlite3.Row, prefix: str) -> dict[str, Any] | None:
        itid = int(_number(row[f"{prefix}_itid"]))
        task = _compact_text(row[f"{prefix}_thread_name"], limit=160)
        process = _compact_text(row[f"{prefix}_process_name"], limit=160)
        if itid < 0 and task == "unknown" and process == "unknown":
            return None
        return {
            "itid": itid,
            "tid": int(_number(row[f"{prefix}_tid"])),
            "task": task,
            "process": process,
            "pid": int(_number(row[f"{prefix}_pid"])),
        }

    windows: dict[int, dict[str, Any]] = {}
    processed_wakeups = 0
    target_thread_mapped_wakeups = 0
    source_edge_mapped_wakeups = 0
    process_mapped_wakeups = 0
    for row in rows:
        processed_wakeups += 1
        timestamp_ns = int(row["ts"])
        bucket = timestamp_ns // FRAME_WINDOW_NS
        window = windows.setdefault(
            bucket,
            {
                "start_ns": timestamp_ns,
                "end_ns": timestamp_ns,
                "edges": Counter(),
                "root_event_ids": [],
                "threads": {},
                "module_labels": Counter(),
                "top_labels": Counter(),
                "wakeup_count": 0,
            },
        )
        source_ref = endpoint_ref(row, "source")
        target_ref = endpoint_ref(row, "target")
        if target_ref is not None:
            target_thread_mapped_wakeups += 1
        if source_ref is not None:
            source_edge_mapped_wakeups += 1
        if any(_compact_text(row[column]) != "unknown" for column in ("source_process_name", "target_process_name")):
            process_mapped_wakeups += 1
        source_label = compact_identity(row["source_process_name"], row["source_thread_name"], row["source_itid"])
        target_label = compact_identity(row["target_process_name"], row["target_thread_name"], row["target_itid"])
        edge_key = f"{source_label}->{target_label}"
        window["start_ns"] = min(window["start_ns"], timestamp_ns)
        window["end_ns"] = max(window["end_ns"], timestamp_ns)
        window["edges"][edge_key] += 1
        window["wakeup_count"] += 1
        for label in (
            row["source_process_name"],
            row["source_thread_name"],
            row["target_process_name"],
            row["target_thread_name"],
        ):
            if _compact_text(label) != "unknown":
                window["module_labels"].update((str(label),))
        top_label = _compact_text(row["target_thread_name"], limit=160)
        window["top_labels"][target_label if top_label == "unknown" else top_label] += 1
        if len(window["root_event_ids"]) < MAX_CLUSTER_ROOT_EVENT_IDS:
            window["root_event_ids"].append(f"raw:{int(row['event_id'])}")
        for role, reference in (("source", source_ref), ("target", target_ref)):
            if reference is not None and len(window["threads"]) < 40:
                window["threads"][f"{role}:{reference['itid']}"] = reference

    window_items = []
    for bucket, window in windows.items():
        top_label = window["top_labels"].most_common(1)[0][0] if window["top_labels"] else None
        module_summary = summarize_trace_modules(
            window["module_labels"].elements(),
            fallback_top_function=top_label,
        )
        window_items.append((bucket, window, module_summary))
    ordered_windows = sorted(
        window_items,
        key=lambda item: (
            item[2]["system_module_only"],
            -item[1]["wakeup_count"],
            -len(item[1]["edges"]),
            item[0],
        ),
    )[:MAX_SCHEDULER_CATALOG_CLUSTERS]

    clusters: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for ordinal, (bucket, window, module_summary) in enumerate(ordered_windows, start=1):
        count = int(window["wakeup_count"])
        cluster_id = f"cluster_htrace_wakeup_{trace_sha256[:10]}_{bucket:x}"
        pressure = "high" if count >= 5 else "medium" if count >= 2 else "low"
        edge_items = window["edges"].most_common(8)
        clusters.append(
            {
                "cluster_id": cluster_id,
                "time_range_ns": {"start_ns": int(window["start_ns"]), "end_ns": int(window["end_ns"])},
                "root_event_ids": window["root_event_ids"],
                "thread_refs": list(window["threads"].values()),
                "edge_summary": {
                    "kind": "scheduler_wakeup_window_candidate",
                    "edge_count": count,
                    "wakeup_count": count,
                    "unique_edge_count": len(window["edges"]),
                    "top_edges": [{"edge": edge, "count": edge_count} for edge, edge_count in edge_items],
                },
                "module_summary": module_summary,
                "clock_quality": clock_quality,
                "profile_id": "htrace-basic-scheduler-module-catalog-v2",
                "selection_state": "pending",
                "artifact_ref": f"sha256:{trace_sha256}",
                "analysis_scope": "scheduler_wakeup_window_not_critical_path",
            }
        )
        candidates.append(
            {
                "hotspot_id": f"hs_htrace_wakeup_{ordinal:04d}",
                "cluster_id": cluster_id,
                "function_ref": {
                    "symbol": module_summary["top_function_name"],
                    "resolution": "raw_htrace_scheduler_task_only",
                },
                "unresolved_reason": "Scheduler task identities are not resolved source symbols; wakeup density cannot establish a critical path.",
                "active_time_metrics": {
                    "measurement": "wakeup_count_in_20ms_window",
                    "wakeup_count": count,
                    "window_ns": FRAME_WINDOW_NS,
                },
                "temporal_pressure": pressure,
                "temporal_pressure_notes": ["Scheduler wakeup density is not CPU execution duration."],
                "critical_path_metrics": {"available": False, "reason": "Basic raw .htrace extraction does not infer a critical path."},
                "dominance_label": "unknown",
                "evidence_refs": [f"sha256:{trace_sha256}", f"cluster:{cluster_id}"],
            }
        )

    return clusters, candidates, {
        "available": True,
        "input_wakeup_events": total_wakeups,
        "processed_wakeup_events": processed_wakeups,
        "available_windows": len(window_items),
        "emitted_clusters": len(clusters),
        "cluster_limit": MAX_SCHEDULER_CATALOG_CLUSTERS,
        "input_truncated": total_wakeups > processed_wakeups,
        "catalog_truncated": len(window_items) > MAX_SCHEDULER_CATALOG_CLUSTERS,
        "endpoint_mapping": {
            "target_thread_map_schema_available": has_thread_map,
            "target_thread_map": target_thread_mapped_wakeups > 0,
            "target_thread_mapped_wakeups": target_thread_mapped_wakeups,
            "process_map_schema_available": has_process_map,
            "process_map": process_mapped_wakeups > 0,
            "process_mapped_wakeups": process_mapped_wakeups,
            "source_wakeup_edge_map_schema_available": has_instant_edge_map,
            "source_wakeup_edge_map": source_edge_mapped_wakeups > 0,
            "source_wakeup_edge_mapped_wakeups": source_edge_mapped_wakeups,
        },
    }


def _managed_conversion_database(raw_htrace: dict[str, Any], *, state_root: Path) -> Path:
    """Resolve the server-managed conversion DB recorded in a raw-trace artifact."""

    conversion = raw_htrace.get("conversion") if isinstance(raw_htrace.get("conversion"), dict) else {}
    database = conversion.get("database") if isinstance(conversion.get("database"), dict) else {}
    managed_path = database.get("managed_path")
    if not isinstance(managed_path, str) or not managed_path or Path(managed_path).is_absolute():
        raise PlatformError(
            "TRACE_DB_INVALID",
            "The raw trace artifact does not contain a safe managed conversion database path.",
            status_code=422,
        )
    candidate = (state_root / managed_path).resolve()
    try:
        candidate.relative_to(state_root.resolve())
    except ValueError as exc:
        raise PlatformError(
            "TRACE_DB_INVALID",
            "The raw trace conversion database path escapes managed storage.",
            status_code=422,
        ) from exc
    if not candidate.is_file():
        raise PlatformError(
            "TRACE_DB_INVALID",
            "The raw trace conversion database is no longer available.",
            status_code=404,
        )
    return candidate


def _cluster_time_range(cluster: dict[str, Any]) -> tuple[int, int] | None:
    time_range = cluster.get("time_range_ns") if isinstance(cluster.get("time_range_ns"), dict) else {}
    try:
        start_ns = int(time_range.get("start_ns"))
        end_ns = int(time_range.get("end_ns"))
    except (TypeError, ValueError):
        return None
    return (start_ns, end_ns) if end_ns >= start_ns else None


def _raw_htrace_database_identity(raw_htrace: dict[str, Any]) -> str:
    """Return an immutable identity for event IDs derived from a conversion DB."""

    conversion = raw_htrace.get("conversion") if isinstance(raw_htrace.get("conversion"), dict) else {}
    database = conversion.get("database") if isinstance(conversion.get("database"), dict) else {}
    identity = database.get("sha256")
    return str(identity) if isinstance(identity, str) and identity else "unknown-conversion-db"


def _self_time_context_sql(schema: dict[str, list[str]]) -> dict[str, Any]:
    """Build optional execution-context joins for module-scoped event ranking."""

    callstack_columns = set(schema.get("callstack", []))
    thread_columns = set(schema.get("thread", []))
    process_columns = set(schema.get("process", []))
    has_callid = "callid" in callstack_columns
    has_thread_map = has_callid and {"itid", "name"}.issubset(thread_columns)
    has_process_map = has_thread_map and "ipid" in thread_columns and {"ipid", "name"}.issubset(process_columns)
    callid = "COALESCE(c.callid, -1)" if has_callid else "CAST(-1 AS INTEGER)"
    thread_join = "LEFT JOIN thread t ON c.callid = t.itid" if has_thread_map else ""
    thread_name = "COALESCE(t.name, 'unknown')" if has_thread_map else "'unknown'"
    thread_tid = "t.tid" if has_thread_map and "tid" in thread_columns else "NULL"
    thread_itid = "t.itid" if has_thread_map else "NULL"
    process_join = "LEFT JOIN process p ON t.ipid = p.ipid" if has_process_map else ""
    process_name = "COALESCE(p.name, 'unknown')" if has_process_map else "'unknown'"
    process_pid = "p.pid" if has_process_map and "pid" in process_columns else "NULL"
    return {
        "callid": callid,
        "thread_join": thread_join,
        "thread_name": thread_name,
        "thread_tid": thread_tid,
        "thread_itid": thread_itid,
        "process_join": process_join,
        "process_name": process_name,
        "process_pid": process_pid,
        "has_callid": has_callid,
        "context_mapping_available": has_thread_map,
        "process_mapping_available": has_process_map,
    }


def _self_time_occurrences(
    connection: sqlite3.Connection,
    event_name: str,
    *,
    callid: int,
    has_callid: bool,
    limit: int,
) -> list[sqlite3.Row]:
    """Return costly positive exclusive-estimate occurrences for one event context."""

    callid_filter = "AND COALESCE(c.callid, -1) = ?" if has_callid else ""
    parameters: tuple[Any, ...] = (event_name, callid, limit) if has_callid else (event_name, limit)
    return connection.execute(
        f"""
        WITH child_duration AS (
            SELECT parent_id, SUM(dur) AS total_direct_child_duration
            FROM callstack
            WHERE dur > 0 AND parent_id IS NOT NULL
            GROUP BY parent_id
        ), span_self AS (
            SELECT
                c.id,
                c.ts,
                c.dur,
                CASE WHEN c.dur > COALESCE(child_duration.total_direct_child_duration, 0)
                    THEN c.dur - COALESCE(child_duration.total_direct_child_duration, 0)
                    ELSE 0 END AS self_time_estimate_ns
            FROM callstack c
            LEFT JOIN child_duration ON c.id = child_duration.parent_id
            WHERE c.dur > 0 AND c.name = ? {callid_filter}
        )
        SELECT id, ts, dur, self_time_estimate_ns
        FROM span_self
        WHERE self_time_estimate_ns > 0
        ORDER BY self_time_estimate_ns DESC, dur DESC, ts ASC, id ASC
        LIMIT ?
        """,
        parameters,
    ).fetchall()


def list_htrace_runtime_event_names(
    raw_htrace: dict[str, Any],
    *,
    state_root: Path,
    search_terms: list[str] | None = None,
    limit: int = 600,
) -> dict[str, Any]:
    """Return exact event names observed in one converted ``.htrace``.

    This catalogue is intentionally independent from hotspot ranking.  A useful
    stage boundary can have little exclusive time, so runtime-anchor discovery
    needs the real callstack labels even when they are not top self-time events.
    Search terms only improve bounded retrieval; they never establish a stage
    match or source relationship.
    """

    bounded_limit = min(max(int(limit), 1), MAX_RUNTIME_EVENT_NAME_CANDIDATES)
    normalized_terms: list[str] = []
    for raw_term in search_terms or []:
        for token in re.findall(r"[A-Za-z0-9_]{3,}|[\u4e00-\u9fff]{2,}", str(raw_term)):
            normalized = token.casefold()
            if normalized not in normalized_terms:
                normalized_terms.append(normalized)
            if len(normalized_terms) >= 48:
                break
        if len(normalized_terms) >= 48:
            break

    database_path = _managed_conversion_database(raw_htrace, state_root=state_root)
    try:
        connection = _open_readonly_database(database_path)
    except sqlite3.Error as exc:
        raise PlatformError(
            "TRACE_DB_INVALID",
            "The raw trace conversion database cannot be opened for runtime event extraction.",
            status_code=422,
            details={"reason": str(exc)},
        ) from exc

    try:
        schema = _schema(connection)
        required_columns = {"ts", "dur", "name"}
        missing_columns = sorted(required_columns - set(schema.get("callstack", [])))
        if missing_columns:
            return {
                "events": [],
                "quality": {
                    "status": "unavailable",
                    "reason": "Converted trace has no usable callstack labels for runtime-anchor discovery.",
                    "missing_columns": missing_columns,
                    "profile_id": "htrace-runtime-event-name-catalog/v1",
                },
            }

        context = _self_time_context_sql(schema)
        select_sql = f"""
            SELECT
                c.name AS event_name,
                {context['callid']} AS execution_context_id,
                COUNT(*) AS occurrence_count,
                SUM(CASE WHEN c.dur > 0 THEN c.dur ELSE 0 END) AS inclusive_total_ns,
                MAX(CASE WHEN c.dur > 0 THEN c.dur ELSE 0 END) AS max_duration_ns,
                MIN(c.ts) AS first_start_ns,
                MAX(c.ts + MAX(c.dur, 0)) AS last_end_ns,
                MIN({context['process_name']}) AS representative_process_name,
                MIN({context['thread_name']}) AS representative_thread_name
            FROM callstack c
            {context['thread_join']}
            {context['process_join']}
            WHERE TRIM(COALESCE(c.name, '')) != ''
        """
        group_order_sql = f"""
            GROUP BY c.name, {context['callid']}
            ORDER BY inclusive_total_ns DESC, occurrence_count DESC, event_name ASC
            LIMIT ?
        """
        global_rows = connection.execute(
            select_sql + group_order_sql,
            (bounded_limit,),
        ).fetchall()
        matched_rows: list[sqlite3.Row] = []
        if normalized_terms:
            predicates = " OR ".join("LOWER(c.name) LIKE ?" for _ in normalized_terms)
            matched_rows = connection.execute(
                select_sql + f" AND ({predicates}) " + group_order_sql,
                (*[f"%{term}%" for term in normalized_terms], bounded_limit),
            ).fetchall()
        total_row = connection.execute(
            "SELECT COUNT(DISTINCT name) AS count FROM callstack WHERE TRIM(COALESCE(name, '')) != ''"
        ).fetchone()
        total_distinct_names = int(total_row["count"] or 0) if total_row else 0
        total_context_row = connection.execute(
            f"""
            SELECT COUNT(*) AS count
            FROM (
                SELECT c.name, {context['callid']} AS execution_context_id
                FROM callstack c
                WHERE TRIM(COALESCE(c.name, '')) != ''
                GROUP BY c.name, {context['callid']}
            )
            """
        ).fetchone()
        total_distinct_contexts = int(total_context_row["count"] or 0) if total_context_row else 0

        rows_by_context: dict[tuple[str, int], sqlite3.Row] = {}
        for row in [*matched_rows, *global_rows]:
            key = (str(row["event_name"]), int(row["execution_context_id"] or -1))
            rows_by_context.setdefault(key, row)
        database_identity = _raw_htrace_database_identity(raw_htrace)
        events: list[dict[str, Any]] = []
        for row in rows_by_context.values():
            event_name = _compact_text(row["event_name"], limit=1000)
            execution_context_id = int(row["execution_context_id"] or -1)
            matched_terms = [term for term in normalized_terms if term in event_name.casefold()]
            event_identity = hashlib.sha256(
                f"runtime_event_name\0{database_identity}\0{event_name}\0{execution_context_id}".encode("utf-8")
            ).hexdigest()[:16]
            events.append(
                {
                    "event_id": f"event_htrace_runtime_{event_identity}",
                    "event_name": event_name,
                    "event_kind": "callstack_label",
                    "trace_context": {
                        "execution_context_id": execution_context_id,
                        "process_name": _compact_text(row["representative_process_name"], limit=160),
                        "thread_name": _compact_text(row["representative_thread_name"], limit=160),
                    },
                    "metrics": {
                        "occurrence_count": int(row["occurrence_count"] or 0),
                        "inclusive_total_duration_ms": round(_number(row["inclusive_total_ns"]) / 1_000_000.0, 6),
                        "max_span_duration_ms": round(_number(row["max_duration_ns"]) / 1_000_000.0, 6),
                        "first_start_ns": int(row["first_start_ns"] or 0),
                        "last_end_ns": int(row["last_end_ns"] or 0),
                    },
                    "retrieval": {
                        "matched_search_terms": matched_terms,
                        "term_match": bool(matched_terms),
                    },
                    "source_resolution": "observed_runtime_trace_name",
                }
            )
        events.sort(
            key=lambda item: (
                not bool((item.get("retrieval") if isinstance(item.get("retrieval"), dict) else {}).get("term_match")),
                -_number((item.get("metrics") if isinstance(item.get("metrics"), dict) else {}).get("inclusive_total_duration_ms")),
                -int((item.get("metrics") if isinstance(item.get("metrics"), dict) else {}).get("occurrence_count") or 0),
                str(item.get("event_name") or ""),
            )
        )
        events = events[:bounded_limit]
        for rank, event in enumerate(events, start=1):
            event["rank"] = rank
    except sqlite3.Error as exc:
        raise PlatformError(
            "TRACE_RUNTIME_EVENT_EXTRACTION_FAILED",
            "Runtime event names could not be extracted from the converted trace database.",
            status_code=422,
            details={"reason": str(exc)},
        ) from exc
    finally:
        connection.close()

    return {
        "events": events,
        "quality": {
            "status": "available" if events else "unavailable",
            "reason": None if events else "The converted trace contains no non-empty callstack labels.",
            "profile_id": "htrace-runtime-event-name-catalog/v1",
            "source": "converted_callstack_distinct_names",
            "total_distinct_event_names": total_distinct_names,
            "total_distinct_event_contexts": total_distinct_contexts,
            "returned_event_names": len({str(item.get("event_name") or "") for item in events}),
            "returned_event_contexts": len(events),
            "truncated": total_distinct_contexts > len(events),
            "search_terms": normalized_terms,
            "measurement_semantics": "inclusive_trace_span",
            "limitations": [
                "An observed event name proves runtime presence, not semantic ownership of an application stage.",
                "Inclusive spans can overlap and are not CPU self time.",
                "Representative process and thread names are retrieval context rather than a complete occurrence list.",
            ],
        },
    }


def analyze_htrace_stage_windows(
    raw_htrace: dict[str, Any],
    *,
    state_root: Path,
    stages: list[dict[str, Any]],
    top_event_limit: int = 30,
    timeline_occurrence_limit: int = 320,
    timeline_bucket_count: int = 96,
) -> dict[str, Any]:
    """Extract trace-span metrics inside already established stage windows.

    The caller owns stage semantics and boundaries. This function only applies
    those windows to the converted TraceStreamer database and ranks event
    groups with the same parent-minus-direct-children estimate used elsewhere
    in the platform.
    """

    bounded_limit = max(1, min(int(top_event_limit), 50))
    bounded_occurrence_limit = max(1, min(int(timeline_occurrence_limit), 1000))
    bounded_bucket_count = max(16, min(int(timeline_bucket_count), 128))
    database_path = _managed_conversion_database(raw_htrace, state_root=state_root)
    try:
        connection = _open_readonly_database(database_path)
    except sqlite3.Error as exc:
        raise PlatformError(
            "TRACE_DB_INVALID",
            "The raw trace conversion database cannot be opened for stage analysis.",
            status_code=422,
            details={"reason": str(exc)},
        ) from exc

    output: list[dict[str, Any]] = []
    try:
        schema = _schema(connection)
        required_columns = {"id", "ts", "dur", "name"}
        missing_columns = sorted(required_columns - set(schema.get("callstack", [])))
        if missing_columns:
            return {
                "stages": [],
                "quality": {
                    "status": "unavailable",
                    "profile_id": "htrace-stage-window-analysis/v1",
                    "missing_columns": missing_columns,
                },
            }
        context = _self_time_context_sql(schema)
        has_parent_links = "parent_id" in set(schema.get("callstack", []))

        for raw_stage in stages[:128]:
            stage_id = _compact_text(raw_stage.get("id"), limit=80)
            try:
                start_ns = int(raw_stage.get("start_ns"))
                end_ns = int(raw_stage.get("end_ns"))
            except (TypeError, ValueError):
                continue
            if not stage_id or start_ns < 0 or end_ns < start_ns:
                continue
            process_names = sorted({
                _compact_text(value, limit=160)
                for value in raw_stage.get("process_names", [])
                if _compact_text(value, limit=160) not in {"", "unknown"}
            })
            excluded_names = sorted({
                _compact_text(value, limit=1000)
                for value in raw_stage.get("excluded_event_names", [])
                if _compact_text(value, limit=1000)
            })
            filters = ["c.dur > 0", "c.ts >= ?", "c.ts < ?", "TRIM(COALESCE(c.name, '')) != ''"]
            parameters: list[Any] = [start_ns, end_ns]
            if process_names and context["process_mapping_available"]:
                filters.append(f"{context['process_name']} IN ({','.join('?' for _ in process_names)})")
                parameters.extend(process_names)
            if excluded_names:
                filters.append(f"c.name NOT IN ({','.join('?' for _ in excluded_names)})")
                parameters.extend(excluded_names)
            filter_sql = " AND ".join(filters)
            child_cte = (
                """
                , child_duration AS (
                    SELECT child.parent_id, SUM(child.dur) AS direct_child_duration
                    FROM callstack child
                    JOIN stage_spans parent ON parent.id = child.parent_id
                    WHERE child.dur > 0
                    GROUP BY child.parent_id
                )
                """
                if has_parent_links
                else ""
            )
            child_join = "LEFT JOIN child_duration cd ON cd.parent_id = s.id" if has_parent_links else ""
            self_expression = (
                "CASE WHEN s.dur > COALESCE(cd.direct_child_duration, 0) "
                "THEN s.dur - COALESCE(cd.direct_child_duration, 0) ELSE 0 END"
                if has_parent_links
                else "s.dur"
            )
            common_cte = f"""
                WITH stage_spans AS (
                    SELECT c.id, c.name, c.ts, c.dur,
                           {context['process_name']} AS process_name,
                           {context['process_pid']} AS pid,
                           {context['thread_name']} AS thread_name,
                           {context['thread_tid']} AS tid,
                           {context['thread_itid']} AS itid
                    FROM callstack c
                    {context['thread_join']}
                    {context['process_join']}
                    WHERE {filter_sql}
                )
                {child_cte}
                , span_self AS (
                    SELECT s.*, {self_expression} AS self_ns
                    FROM stage_spans s
                    {child_join}
                )
            """
            totals = connection.execute(
                common_cte + "SELECT COUNT(*) AS event_count, COALESCE(SUM(dur), 0) AS inclusive_total_ns FROM span_self",
                parameters,
            ).fetchone()
            rows = connection.execute(
                common_cte
                + """
                , group_metrics AS (
                    SELECT name, process_name, thread_name,
                           COUNT(*) AS occurrence_count,
                           SUM(dur) AS inclusive_total_ns,
                           SUM(self_ns) AS self_total_ns,
                           MAX(dur) AS max_duration_ns,
                           MAX(self_ns) AS max_self_ns
                    FROM span_self
                    GROUP BY name, process_name, thread_name
                    HAVING SUM(self_ns) > 0
                ), representatives AS (
                    SELECT *, ROW_NUMBER() OVER (
                        PARTITION BY name, process_name, thread_name
                        ORDER BY self_ns DESC, dur DESC, ts ASC, id ASC
                    ) AS occurrence_rank
                    FROM span_self
                )
                SELECT r.id, r.name, r.ts, r.dur, r.process_name, r.pid,
                       r.thread_name, r.tid, r.itid,
                       g.occurrence_count, g.inclusive_total_ns, g.self_total_ns,
                       g.max_duration_ns, g.max_self_ns
                FROM group_metrics g
                JOIN representatives r
                  ON r.name = g.name
                 AND r.process_name = g.process_name
                 AND r.thread_name = g.thread_name
                 AND r.occurrence_rank = 1
                ORDER BY g.self_total_ns DESC, g.max_self_ns DESC, g.inclusive_total_ns DESC, g.name ASC
                LIMIT ?
                """,
                [*parameters, bounded_limit],
            ).fetchall()
            top_events: list[dict[str, Any]] = []
            timeline_groups: dict[tuple[str, str, str], tuple[str, int]] = {}
            for rank, row in enumerate(rows, start=1):
                event_identity = hashlib.sha256(
                    f"stage_event\0{stage_id}\0{row['id']}\0{row['name']}\0{row['process_name']}\0{row['thread_name']}".encode("utf-8")
                ).hexdigest()[:16]
                event_id = f"event_htrace_stage_{event_identity}"
                timeline_groups[(
                    str(row["name"] or ""),
                    str(row["process_name"] or ""),
                    str(row["thread_name"] or ""),
                )] = (event_id, rank)
                top_events.append({
                    "event_id": event_id,
                    "name": _compact_text(row["name"], limit=1000),
                    "ts_ms": round(int(row["ts"]) / 1_000_000.0, 6),
                    "dur_ms": round(int(row["dur"]) / 1_000_000.0, 6),
                    "process_name": _compact_text(row["process_name"], limit=160) or "unknown",
                    "pid": int(row["pid"]) if row["pid"] is not None else None,
                    "thread_name": _compact_text(row["thread_name"], limit=160) or "unknown",
                    "tid": int(row["tid"]) if row["tid"] is not None else None,
                    "itid": int(row["itid"]) if row["itid"] is not None else None,
                    "metrics": {
                        "rank": rank,
                        "occurrence_count": int(row["occurrence_count"] or 0),
                        "self_time_estimate_total_duration_ms": round(_number(row["self_total_ns"]) / 1_000_000.0, 6),
                        "inclusive_total_duration_ms": round(_number(row["inclusive_total_ns"]) / 1_000_000.0, 6),
                        "max_self_time_estimate_duration_ms": round(_number(row["max_self_ns"]) / 1_000_000.0, 6),
                        "max_span_duration_ms": round(_number(row["max_duration_ns"]) / 1_000_000.0, 6),
                    },
                    "measurement_semantics": {
                        "duration_kind": "representative_inclusive_trace_span",
                        "ranking_kind": "parent_span_minus_direct_children_estimate" if has_parent_links else "inclusive_span_fallback",
                        "cpu_self_time_proven": False,
                    },
                })
            source_occurrence_count = sum(int(row["occurrence_count"] or 0) for row in rows)
            event_timeline: dict[str, Any] = {
                "representation": "unavailable",
                "source_occurrence_count": source_occurrence_count,
                "returned_occurrence_count": 0,
                "truncated": False,
                "occurrences": [],
                "density_buckets": [],
                "semantics": "当前阶段没有可绘制的热点调用区间。",
            }
            if timeline_groups:
                group_filters: list[str] = []
                group_parameters: list[Any] = []
                for name, process_name, thread_name in timeline_groups:
                    group_filters.append(
                        "(name = ? AND COALESCE(process_name, '') = ? AND COALESCE(thread_name, '') = ?)"
                    )
                    group_parameters.extend([name, process_name, thread_name])
                group_filter_sql = " OR ".join(group_filters)
                if source_occurrence_count <= bounded_occurrence_limit:
                    occurrence_rows = connection.execute(
                        common_cte
                        + f"""
                        SELECT id, name, ts, dur, self_ns, process_name, thread_name
                        FROM span_self
                        WHERE {group_filter_sql}
                        ORDER BY ts ASC, dur DESC, id ASC
                        """,
                        [*parameters, *group_parameters],
                    ).fetchall()
                    timeline_occurrences: list[dict[str, Any]] = []
                    for occurrence in occurrence_rows:
                        group_key = (
                            str(occurrence["name"] or ""),
                            str(occurrence["process_name"] or ""),
                            str(occurrence["thread_name"] or ""),
                        )
                        event_id, event_rank = timeline_groups[group_key]
                        timeline_occurrences.append({
                            "event_id": event_id,
                            "name": _compact_text(occurrence["name"], limit=1000),
                            "rank": event_rank,
                            "ts_ms": round(int(occurrence["ts"]) / 1_000_000.0, 6),
                            "dur_ms": round(int(occurrence["dur"]) / 1_000_000.0, 6),
                            "self_time_estimate_duration_ms": round(_number(occurrence["self_ns"]) / 1_000_000.0, 6),
                            "process_name": _compact_text(occurrence["process_name"], limit=160) or "unknown",
                            "thread_name": _compact_text(occurrence["thread_name"], limit=160) or "unknown",
                        })
                    event_timeline = {
                        "representation": "observed_occurrences",
                        "source_occurrence_count": source_occurrence_count,
                        "returned_occurrence_count": len(timeline_occurrences),
                        "truncated": len(timeline_occurrences) < source_occurrence_count,
                        "occurrences": timeline_occurrences,
                        "density_buckets": [],
                        "semantics": "橙色区间来自原始 Trace 中 Top 事件组的真实调用实例；Self 为父跨度减直接子跨度的估算。",
                    }
                else:
                    window_ns = max(1, end_ns - start_ns)
                    bucket_rows = connection.execute(
                        common_cte
                        + f"""
                        , selected_timeline_spans AS (
                            SELECT *, CAST(((ts - ?) * ?) / ? AS INTEGER) AS bucket_index
                            FROM span_self
                            WHERE {group_filter_sql}
                        )
                        SELECT bucket_index, COUNT(*) AS occurrence_count,
                               COALESCE(SUM(dur), 0) AS inclusive_total_ns,
                               COALESCE(SUM(self_ns), 0) AS self_total_ns
                        FROM selected_timeline_spans
                        GROUP BY bucket_index
                        ORDER BY bucket_index ASC
                        """,
                        [
                            *parameters,
                            start_ns,
                            bounded_bucket_count,
                            window_ns,
                            *group_parameters,
                        ],
                    ).fetchall()
                    bucket_width_ns = window_ns / bounded_bucket_count
                    density_buckets = []
                    for bucket in bucket_rows:
                        bucket_index = max(0, min(bounded_bucket_count - 1, int(bucket["bucket_index"] or 0)))
                        density_buckets.append({
                            "index": bucket_index,
                            "start_ms": round((start_ns + bucket_index * bucket_width_ns) / 1_000_000.0, 6),
                            "end_ms": round((start_ns + (bucket_index + 1) * bucket_width_ns) / 1_000_000.0, 6),
                            "occurrence_count": int(bucket["occurrence_count"] or 0),
                            "inclusive_total_duration_ms": round(_number(bucket["inclusive_total_ns"]) / 1_000_000.0, 6),
                            "self_time_estimate_total_duration_ms": round(_number(bucket["self_total_ns"]) / 1_000_000.0, 6),
                        })
                    event_timeline = {
                        "representation": "binned_density",
                        "source_occurrence_count": source_occurrence_count,
                        "returned_occurrence_count": 0,
                        "truncated": True,
                        "occurrences": [],
                        "density_buckets": density_buckets,
                        "semantics": f"Top 事件组共有 {source_occurrence_count} 次调用，按 {bounded_bucket_count} 个时间桶聚合显示。",
                    }
            output.append({
                "stage_id": stage_id,
                "event_count": int(totals["event_count"] or 0) if totals else 0,
                "total_event_duration_ms": round(_number(totals["inclusive_total_ns"] if totals else 0) / 1_000_000.0, 6),
                "top_events": top_events,
                "event_timeline": event_timeline,
            })
    except sqlite3.Error as exc:
        raise PlatformError(
            "TRACE_STAGE_ANALYSIS_FAILED",
            "Stage windows could not be analyzed in the converted trace database.",
            status_code=422,
            details={"reason": str(exc)},
        ) from exc
    finally:
        connection.close()

    return {
        "stages": output,
        "quality": {
            "status": "available" if output else "unavailable",
            "profile_id": "htrace-stage-window-analysis/v1",
            "stage_count": len(output),
            "top_event_limit_per_stage": bounded_limit,
            "timeline_occurrence_limit": bounded_occurrence_limit,
            "timeline_bucket_count": bounded_bucket_count,
            "parent_link_estimate_available": has_parent_links,
            "process_mapping_available": context["process_mapping_available"],
        },
    }


def list_htrace_self_time_events(
    raw_htrace: dict[str, Any],
    *,
    state_root: Path,
    limit: int = MAX_SELF_TIME_EVENT_CANDIDATES,
    module_key: str | None = None,
    tag_catalog: dict[str, Any] | None = None,
    group_by_module: bool = False,
    per_module_limit: int = 6,
) -> dict[str, Any]:
    """Rank trace-wide callstack contexts by a bounded self-time estimate.

    The extraction groups a label with its execution context before module
    classification.  This avoids a generic event label hiding an ArkUI/ACE,
    rendering, or media occurrence that ran on a different process or thread.
    A decoded HiTrace tag takes precedence over label heuristics; neither is a
    source-module ownership claim.
    """

    bounded_limit = min(max(int(limit), 1), MAX_SELF_TIME_EVENT_CANDIDATES)
    bounded_per_module_limit = min(max(int(per_module_limit), 1), MAX_SELF_TIME_EVENT_CANDIDATES)
    normalized_module_key = str(module_key or "").strip() or None
    if normalized_module_key is not None and normalized_module_key not in known_trace_module_keys():
        raise PlatformError(
            "TRACE_MODULE_UNKNOWN",
            "Requested trace module is not registered for self-time discovery.",
            status_code=422,
            details={"module_key": normalized_module_key, "available_module_keys": list(known_trace_module_keys())},
        )
    database_path = _managed_conversion_database(raw_htrace, state_root=state_root)
    try:
        connection = _open_readonly_database(database_path)
    except sqlite3.Error as exc:
        raise PlatformError(
            "TRACE_DB_INVALID",
            "The raw trace conversion database cannot be opened for self-time extraction.",
            status_code=422,
            details={"reason": str(exc)},
        ) from exc

    try:
        schema = _schema(connection)
        required_columns = {"id", "ts", "dur", "name", "parent_id"}
        missing_columns = sorted(required_columns - set(schema.get("callstack", [])))
        if missing_columns:
            return {
                "events": [],
                "module_catalog": {"classification": "hitrace_tag_then_trace_identity_v1", "groups": []},
                "quality": {
                    "status": "unavailable",
                    "reason": "Converted trace has no parent-linked callstack table for self-time estimation.",
                    "missing_columns": missing_columns,
                    "profile_id": "htrace-event-self-time-v2",
                    "source": "converted_callstack_direct_child_subtraction",
                },
            }

        context = _self_time_context_sql(schema)
        rows = connection.execute(
            f"""
            WITH child_duration AS (
                SELECT parent_id, SUM(dur) AS total_direct_child_duration
                FROM callstack
                WHERE dur > 0 AND parent_id IS NOT NULL
                GROUP BY parent_id
            ), span_self AS (
                SELECT
                    c.id,
                    c.ts,
                    c.dur,
                    c.name,
                    {context['callid']} AS callid,
                    {context['process_name']} AS process_name,
                    {context['thread_name']} AS thread_name,
                    CASE WHEN c.dur > COALESCE(child_duration.total_direct_child_duration, 0)
                        THEN c.dur - COALESCE(child_duration.total_direct_child_duration, 0)
                        ELSE 0 END AS self_time_estimate_ns
                FROM callstack c
                {context['thread_join']}
                {context['process_join']}
                LEFT JOIN child_duration ON c.id = child_duration.parent_id
                WHERE c.dur > 0 AND TRIM(COALESCE(c.name, '')) != ''
            )
            SELECT
                name AS event_name,
                callid,
                process_name,
                thread_name,
                COUNT(*) AS occurrence_count,
                SUM(self_time_estimate_ns) AS self_time_estimate_total_ns,
                SUM(dur) AS inclusive_total_ns,
                MAX(self_time_estimate_ns) AS max_self_time_estimate_ns,
                MAX(dur) AS max_duration_ns,
                MIN(ts) AS first_start_ns,
                MAX(ts + dur) AS last_end_ns
            FROM span_self
            GROUP BY name, callid, process_name, thread_name
            HAVING SUM(self_time_estimate_ns) > 0
            ORDER BY self_time_estimate_total_ns DESC, max_self_time_estimate_ns DESC,
                     inclusive_total_ns DESC, occurrence_count DESC, event_name ASC
            LIMIT ?
            """,
            (MAX_SELF_TIME_EVENT_GROUP_SCAN,),
        ).fetchall()

        classified_rows: list[dict[str, Any]] = []
        module_rollups: dict[str, dict[str, Any]] = {}
        for row in rows:
            raw_event_name = str(row["event_name"])
            marker = describe_hitrace_event(raw_event_name, tag_catalog or {})
            trace_module = classify_trace_event(
                raw_event_name,
                process_name=row["process_name"],
                thread_name=row["thread_name"],
                hitrace_marker=marker,
            )
            module_key_for_row = str(trace_module["module_key"])
            row_data = {"row": row, "event_name": raw_event_name, "marker": marker, "trace_module": trace_module}
            classified_rows.append(row_data)

            rollup = module_rollups.setdefault(
                module_key_for_row,
                {
                    "module_key": module_key_for_row,
                    "module_name": trace_module["module_name"],
                    "system_collapsed": trace_module["system_collapsed"],
                    "event_group_count": 0,
                    "occurrence_count": 0,
                    "self_time_estimate_total_ns": 0.0,
                    "inclusive_total_ns": 0.0,
                    "max_self_time_estimate_ns": 0.0,
                    "classification_counts": Counter(),
                },
            )
            rollup["event_group_count"] += 1
            rollup["occurrence_count"] += int(row["occurrence_count"])
            rollup["self_time_estimate_total_ns"] += _number(row["self_time_estimate_total_ns"])
            rollup["inclusive_total_ns"] += _number(row["inclusive_total_ns"])
            rollup["max_self_time_estimate_ns"] = max(
                float(rollup["max_self_time_estimate_ns"]),
                _number(row["max_self_time_estimate_ns"]),
            )
            rollup["classification_counts"][str(trace_module["classification"])] += 1

        module_groups = []
        for rollup in module_rollups.values():
            classification_counts = rollup["classification_counts"]
            module_groups.append(
                {
                    "module_key": rollup["module_key"],
                    "module_name": rollup["module_name"],
                    "system_collapsed": bool(rollup["system_collapsed"]),
                    "event_group_count": int(rollup["event_group_count"]),
                    "occurrence_count": int(rollup["occurrence_count"]),
                    "self_time_estimate_total_duration_ms": round(float(rollup["self_time_estimate_total_ns"]) / 1_000_000.0, 6),
                    "inclusive_total_duration_ms": round(float(rollup["inclusive_total_ns"]) / 1_000_000.0, 6),
                    "max_self_time_estimate_duration_ms": round(float(rollup["max_self_time_estimate_ns"]) / 1_000_000.0, 6),
                    "classification_counts": dict(sorted(classification_counts.items())),
                }
            )
        module_groups.sort(
            key=lambda item: (
                -float(item["self_time_estimate_total_duration_ms"]),
                bool(item["system_collapsed"]),
                str(item["module_name"]),
            )
        )

        candidate_rows = [
            item
            for item in classified_rows
            if normalized_module_key is None or item["trace_module"]["module_key"] == normalized_module_key
        ]
        candidate_rows.sort(
            key=lambda item: (
                -_number(item["row"]["self_time_estimate_total_ns"]),
                -_number(item["row"]["max_self_time_estimate_ns"]),
                -_number(item["row"]["inclusive_total_ns"]),
                -int(item["row"]["occurrence_count"]),
                item["event_name"],
                int(item["row"]["callid"]),
            )
        )

        database_identity = _raw_htrace_database_identity(raw_htrace)

        def build_event(item: dict[str, Any], *, rank: int, module_rank: int | None = None) -> dict[str, Any]:
            row = item["row"]
            raw_event_name = item["event_name"]
            trace_module = item["trace_module"]
            callid = int(row["callid"])
            process_name = _compact_text(row["process_name"], limit=160)
            thread_name = _compact_text(row["thread_name"], limit=160)
            event_identity = hashlib.sha256(
                (
                    f"event_self_time\0{database_identity}\0{raw_event_name}\0{callid}\0"
                    f"{process_name}\0{thread_name}\0{trace_module['module_key']}"
                ).encode("utf-8")
            ).hexdigest()[:16]
            occurrence_rows = _self_time_occurrences(
                connection,
                raw_event_name,
                callid=callid,
                has_callid=bool(context["has_callid"]),
                limit=MAX_EVENT_OCCURRENCE_REFS,
            )
            event = {
                "event_id": f"event_htrace_self_{event_identity}",
                "rank": rank,
                "event_name": _compact_text(raw_event_name),
                "event_kind": "callstack_span",
                "discovery_id": "event_self_time",
                "trace_module": trace_module,
                "trace_context": {
                    "callid": callid,
                    "process_name": process_name,
                    "thread_name": thread_name,
                },
                "metrics": {
                    "self_time_estimate_total_duration_ms": round(
                        _number(row["self_time_estimate_total_ns"]) / 1_000_000.0,
                        6,
                    ),
                    "inclusive_total_duration_ms": round(_number(row["inclusive_total_ns"]) / 1_000_000.0, 6),
                    "max_self_time_estimate_duration_ms": round(
                        _number(row["max_self_time_estimate_ns"]) / 1_000_000.0,
                        6,
                    ),
                    "max_span_duration_ms": round(_number(row["max_duration_ns"]) / 1_000_000.0, 6),
                    "occurrence_count": int(row["occurrence_count"]),
                    "first_start_ns": int(row["first_start_ns"]),
                    "last_end_ns": int(row["last_end_ns"]),
                },
                "trace_event_refs": [
                    {
                        "event_id": f"callstack:{int(occurrence['id'])}",
                        "start_ns": int(occurrence["ts"]),
                        "duration_ns": int(occurrence["dur"]),
                        "self_time_estimate_duration_ns": int(occurrence["self_time_estimate_ns"]),
                    }
                    for occurrence in occurrence_rows
                ],
                "source_resolution": "raw_htrace_symbol_only",
            }
            if module_rank is not None:
                event["module_rank"] = module_rank
            if item["marker"] is not None:
                event["hitrace_marker"] = item["marker"]
            return event

        module_event_groups: list[dict[str, Any]] = []
        if group_by_module:
            candidates_by_module: dict[str, list[dict[str, Any]]] = {}
            for item in candidate_rows:
                candidates_by_module.setdefault(str(item["trace_module"]["module_key"]), []).append(item)

            events = []
            global_rank = 1
            for rollup in module_groups:
                group_key = str(rollup["module_key"])
                module_candidates = candidates_by_module.get(group_key, [])
                if not module_candidates:
                    continue
                group_events = []
                for module_rank, item in enumerate(module_candidates[:bounded_per_module_limit], start=1):
                    group_events.append(build_event(item, rank=global_rank, module_rank=module_rank))
                    global_rank += 1
                module_event_groups.append(
                    {
                        **rollup,
                        "returned_event_count": len(group_events),
                        "per_module_limit": bounded_per_module_limit,
                        "events": group_events,
                    }
                )
                events.extend(group_events)
        else:
            events = [build_event(item, rank=rank) for rank, item in enumerate(candidate_rows[:bounded_limit], start=1)]
            module_event_groups = []
    except sqlite3.Error as exc:
        raise PlatformError(
            "TRACE_SELF_TIME_EXTRACTION_FAILED",
            "Self-time events could not be extracted from the converted trace database.",
            status_code=422,
            details={"reason": str(exc)},
        ) from exc
    finally:
        connection.close()

    return {
        "events": events,
        "module_event_groups": module_event_groups,
        "module_catalog": {
            "classification": "hitrace_tag_then_trace_identity_v1",
            "grouping_unit": "event_name_and_execution_context",
            "module_filter": normalized_module_key,
            "groups": module_groups,
        },
        "quality": {
            "status": "partial" if events else "unavailable",
            "reason": None if events else "No callstack event has a positive self-time estimate in this trace for the selected module.",
            "profile_id": "htrace-event-self-time-v2",
            "source": "converted_callstack_direct_child_subtraction",
            "ranking": "self_time_estimate_total_duration_desc_top_n_per_module" if group_by_module else "self_time_estimate_total_duration_desc_within_module" if normalized_module_key else "self_time_estimate_total_duration_desc",
            "scope": "trace_wide",
            "self_time_estimate_method": "direct_child_duration_subtraction",
            "module_classification": "decoded_hitrace_tag_then_trace_identity_heuristic",
            "limitations": [
                "This is an instrumentation-span exclusive estimate, not sampled CPU self time.",
                "Overlapping or incomplete direct child spans can under-estimate the parent exclusive duration.",
                "Trace module classification is tag/context evidence for triage, not source-module ownership proof.",
            ],
            "returned_events": len(events),
            "limit": bounded_limit,
            "module_filter": normalized_module_key,
            "scanned_event_groups": len(classified_rows),
            "event_group_scan_limit": MAX_SELF_TIME_EVENT_GROUP_SCAN,
            "event_group_scan_may_be_truncated": len(classified_rows) == MAX_SELF_TIME_EVENT_GROUP_SCAN,
            "execution_context_mapping_available": bool(context["context_mapping_available"]),
            "process_mapping_available": bool(context["process_mapping_available"]),
            "source_location_available": False,
        },
    }


def list_htrace_cluster_events(
    raw_htrace: dict[str, Any],
    cluster: dict[str, Any],
    *,
    state_root: Path,
    limit: int = MAX_CLUSTER_EVENT_CANDIDATES,
) -> dict[str, Any]:
    """List top callstack event names overlapping one selected trace cluster.

    Scheduler wakeup records themselves usually all have the same event name.
    For a wakeup window, the useful next manual triage step is therefore the
    callstack spans that overlap its time range, ranked by their overlap rather
    than by the trace-wide aggregate.  This function deliberately returns no
    source location because raw trace symbols cannot establish one.
    """

    time_range = _cluster_time_range(cluster)
    if time_range is None:
        return {
            "events": [],
            "quality": {
                "status": "unavailable",
                "reason": "Selected cluster has no valid nanosecond time range.",
            },
        }
    start_ns, end_ns = time_range
    bounded_limit = min(max(int(limit), 1), MAX_CLUSTER_EVENT_CANDIDATES)
    database_path = _managed_conversion_database(raw_htrace, state_root=state_root)
    try:
        connection = _open_readonly_database(database_path)
    except sqlite3.Error as exc:
        raise PlatformError(
            "TRACE_DB_INVALID",
            "The raw trace conversion database cannot be opened for event extraction.",
            status_code=422,
            details={"reason": str(exc)},
        ) from exc

    try:
        schema = _schema(connection)
        required_columns = {"id", "ts", "dur", "name"}
        missing_columns = sorted(required_columns - set(schema.get("callstack", [])))
        if missing_columns:
            return {
                "events": [],
                "quality": {
                    "status": "unavailable",
                    "reason": "Converted trace has no usable callstack event table for this cluster.",
                    "missing_columns": missing_columns,
                },
            }
        rows = connection.execute(
            """
            SELECT
                c.name AS event_name,
                COUNT(*) AS occurrence_count,
                SUM(MAX(0, MIN(c.ts + c.dur, ?) - MAX(c.ts, ?))) AS overlap_total_ns,
                MAX(c.dur) AS max_duration_ns,
                MIN(c.ts) AS first_start_ns,
                MAX(c.ts + c.dur) AS last_end_ns
            FROM callstack c
            WHERE c.dur > 0
              AND TRIM(COALESCE(c.name, '')) != ''
              AND c.ts < ?
              AND c.ts + c.dur > ?
            GROUP BY c.name
            ORDER BY overlap_total_ns DESC, max_duration_ns DESC, occurrence_count DESC, event_name ASC
            LIMIT ?
            """,
            (end_ns, start_ns, end_ns, start_ns, bounded_limit),
        ).fetchall()
        events: list[dict[str, Any]] = []
        for rank, row in enumerate(rows, start=1):
            event_name = _compact_text(row["event_name"])
            occurrence_rows = connection.execute(
                """
                SELECT c.id, c.ts, c.dur
                FROM callstack c
                WHERE c.name = ?
                  AND c.dur > 0
                  AND c.ts < ?
                  AND c.ts + c.dur > ?
                ORDER BY c.dur DESC, c.ts ASC, c.id ASC
                LIMIT ?
                """,
                (row["event_name"], end_ns, start_ns, MAX_EVENT_OCCURRENCE_REFS),
            ).fetchall()
            event_identity = hashlib.sha256(
                f"{cluster.get('cluster_id', 'unknown')}\0{event_name}".encode("utf-8")
            ).hexdigest()[:16]
            events.append(
                {
                    "event_id": f"event_htrace_{event_identity}",
                    "rank": rank,
                    "event_name": event_name,
                    "event_kind": "callstack_span",
                    "metrics": {
                        "overlap_total_duration_ms": round(_number(row["overlap_total_ns"]) / 1_000_000.0, 6),
                        "max_span_duration_ms": round(_number(row["max_duration_ns"]) / 1_000_000.0, 6),
                        "occurrence_count": int(row["occurrence_count"]),
                        "first_start_ns": int(row["first_start_ns"]),
                        "last_end_ns": int(row["last_end_ns"]),
                    },
                    "trace_event_refs": [
                        {
                            "event_id": f"callstack:{int(occurrence['id'])}",
                            "start_ns": int(occurrence["ts"]),
                            "duration_ns": int(occurrence["dur"]),
                        }
                        for occurrence in occurrence_rows
                    ],
                    "source_resolution": "raw_htrace_symbol_only",
                }
            )
    except sqlite3.Error as exc:
        raise PlatformError(
            "TRACE_EVENT_EXTRACTION_FAILED",
            "Callstack events could not be extracted from the converted trace database.",
            status_code=422,
            details={"reason": str(exc)},
        ) from exc
    finally:
        connection.close()

    return {
        "events": events,
        "quality": {
            "status": "partial",
            "reason": (
                None
                if events
                else "No positive-duration named callstack span overlaps this cluster time window."
            ),
            "profile_id": "htrace-cluster-callstack-events-v1",
            "source": "converted_callstack_overlap",
            "time_range_ns": {"start_ns": start_ns, "end_ns": end_ns},
            "returned_events": len(events),
            "limit": bounded_limit,
            "source_location_available": False,
        },
    }


def analyze_htrace(path: Path, *, state_root: Path, workspace_root: Path) -> dict[str, Any]:
    """Produce a normalized Phase-A result plus conversion provenance for one .htrace."""

    trace_sha256 = sha256_file(path)
    conversion = convert_htrace(path, trace_sha256=trace_sha256, state_root=state_root, workspace_root=workspace_root)
    database_path = state_root / str(conversion["database"]["managed_path"])
    try:
        connection = _open_readonly_database(database_path)
    except sqlite3.Error as exc:
        raise PlatformError(
            "TRACE_DB_INVALID",
            "The converted trace database cannot be opened read-only.",
            status_code=422,
            details={"reason": str(exc)},
        ) from exc

    try:
        schema = _schema(connection)
        capabilities = _capabilities(schema)
        metadata = _database_meta(connection, schema)
        start_ns, end_ns, time_source = _trace_range(connection, schema)
        event_counts = _event_counts(connection, schema)
        table_counts = {
            table_name: _count_rows(connection, table_name)
            for table_name in ("process", "thread", "callstack", "sched_slice", "thread_state", "raw", "instant", "frame_slice", "gpu_slice", "dma_fence")
            if table_name in schema
        }
        span_clusters: list[dict[str, Any]] = []
        span_candidates: list[dict[str, Any]] = []
        scheduler_clusters: list[dict[str, Any]] = []
        scheduler_candidates: list[dict[str, Any]] = []
        scheduler_catalog: dict[str, Any] = {
            "available": False,
            "reason": "No raw scheduler wakeup records are available.",
        }
        if capabilities["callstack_spans"]["available"] and table_counts.get("callstack", 0) > 0:
            span_clusters, span_candidates = _span_candidates(
                _span_groups(connection, schema),
                trace_sha256=trace_sha256,
                trace_start_ns=start_ns,
                trace_end_ns=end_ns,
                clock_quality=f"{time_source}_from_trace_streamer",
            )
        if capabilities["scheduler_wakeups"]["available"]:
            scheduler_clusters, scheduler_candidates, scheduler_catalog = _scheduler_wakeup_candidates(
                connection,
                schema,
                trace_sha256=trace_sha256,
                clock_quality=f"{time_source}_from_trace_streamer",
            )
        clusters = [*scheduler_clusters, *span_clusters]
        candidates = [*scheduler_candidates, *span_candidates]
        if span_candidates and scheduler_candidates:
            analysis_profile = "htrace-basic-span-scheduler-module-catalog-v2"
        elif span_candidates:
            analysis_profile = "htrace-basic-span-v1"
        elif scheduler_candidates:
            analysis_profile = "htrace-basic-scheduler-module-catalog-v2"
        else:
            analysis_profile = "htrace-basic-empty-v1"
    finally:
        connection.close()

    warnings = [
        "Raw .htrace basic analysis extracts converted span and scheduler facts; it does not infer a critical path.",
        "Trace symbols are retained as trace evidence only. No source file or line is invented from raw .htrace input.",
    ]
    if not capabilities["callstack_spans"]["available"]:
        warnings.append("Callstack spans are unavailable; the result can contain scheduler wakeup-window candidates only.")
    elif not capabilities["callstack_parent_links"]["available"]:
        warnings.append("Callstack parent links are unavailable; no exclusive-duration estimate is emitted.")
    else:
        warnings.append("Any exclusive-duration value is a direct-child subtraction estimate, not a CPU self-time measurement.")
    if scheduler_catalog.get("available"):
        warnings.append("Scheduler wakeups are grouped into 20 ms windows. Module labels are trace-name heuristics, not source ownership or critical-path proof.")
        if scheduler_catalog.get("input_truncated"):
            warnings.append(f"Scheduler extraction stopped after {MAX_SCHEDULER_WAKEUP_ROWS} wakeups; the wakeup catalog is partial.")
        if scheduler_catalog.get("catalog_truncated"):
            warnings.append(f"Scheduler catalog is capped at {MAX_SCHEDULER_CATALOG_CLUSTERS} windows; use a narrower trace for exhaustive exploration.")
    if not table_counts.get("gpu_slice", 0):
        warnings.append("No GPU slice records are present in the converted database; this input cannot support a GPU-time conclusion.")
    if not table_counts.get("dma_fence", 0):
        warnings.append("No DMA fence records are present in the converted database; fence causality is not available from this input.")
    if not candidates:
        warnings.append("No supported span or scheduler candidates were found in the converted trace database.")

    trace_window_ms = None
    if start_ns is not None and end_ns is not None:
        trace_window_ms = round(max(0, end_ns - start_ns) / 1_000_000.0, 6)
    conversion["database"]["trace_streamer_metadata"] = metadata
    conversion["database"]["schema_sha256"] = hashlib.sha256(
        repr(sorted((name, tuple(columns)) for name, columns in schema.items())).encode("utf-8")
    ).hexdigest()
    conversion["database"]["table_count"] = len(schema)

    return {
        "normalized": {
            "trace_id": f"htrace_{trace_sha256[:16]}",
            "trace_name": path.name,
            "input_kind": "raw_htrace",
            "input_sha256": trace_sha256,
            "analysis_profile": analysis_profile,
            "quality": {
                "status": "partial",
                "trace_window_ns": {"start_ns": start_ns, "end_ns": end_ns, "source": time_source},
                "trace_window_ms": trace_window_ms,
                "table_counts": table_counts,
                "capabilities": capabilities,
                "supported_event_counts": event_counts,
                "scheduler_catalog": scheduler_catalog,
                "warnings": warnings,
            },
            "raw_htrace": {
                "format": conversion["input_format"],
                **({"header_magic": HTRACE_MAGIC.decode("ascii")} if conversion["input_format"] == "ohosprof_protobuf" else {}),
                "conversion": conversion,
            },
            "clusters": clusters,
            "hotspot_candidates": candidates,
            "unresolved_hotspots": [item["hotspot_id"] for item in candidates],
        },
        "conversion": conversion,
    }
