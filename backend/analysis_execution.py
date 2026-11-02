"""Durable workbench snapshots and merged execution views.

An optimization analysis spans three independently durable surfaces:

* browser orchestration events (``AnalysisTraceStore``),
* the backend Agent Run event journal, and
* the native coding-agent transcript when the adapter exposes one.

This module keeps those surfaces separate at rest and joins them only for the
audit UI.  A missing native transcript is reported as a coverage gap instead
of being inferred from terminal paint or page state.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import PlatformError, atomic_write_json, file_lock, utc_now


ANALYSIS_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")
MAX_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_TEXT_CHARS = 256 * 1024
MAX_WIRE_ROWS = 50_000
SECRET_FIELD_PATTERN = re.compile(
    r"(?:^|_)(?:api_?key|authorization|password|secret|credential|access_?key|pairing_?code)(?:$|_)",
    re.IGNORECASE,
)


def _analysis_id(value: Any) -> str:
    normalized = str(value or "").strip()
    if not ANALYSIS_ID_PATTERN.fullmatch(normalized):
        raise PlatformError("ANALYSIS_WORKSPACE_ID_INVALID", "分析记录标识格式无效。", status_code=422)
    return normalized


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any, limit: int = MAX_TEXT_CHARS) -> str:
    if not isinstance(value, str):
        return ""
    return value[:limit]


def _redact(value: Any, *, depth: int = 0) -> Any:
    if depth > 28:
        return "[超过可观测载荷嵌套深度]"
    if isinstance(value, dict):
        return {
            str(key): "[已脱敏]" if SECRET_FIELD_PATTERN.search(str(key)) else _redact(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item, depth=depth + 1) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def _iso_time(value: Any) -> str:
    if isinstance(value, (int, float)):
        try:
            return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        except (OverflowError, OSError, ValueError):
            return ""
    return _text(value, 80)


def _time_millis(value: str) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000.0
    except ValueError:
        return None


def _turn_step_number(value: Any) -> int:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        candidate = value.rsplit(".", 1)[-1]
        try:
            return int(candidate)
        except ValueError:
            return 0
    return 0


class AnalysisWorkspaceStore:
    """Latest complete browser workbench projection for one analysis."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def _path(self, analysis_id: str) -> Path:
        return self.root / f"{_analysis_id(analysis_id)}.json"

    def write(self, analysis_id: str, snapshot: dict[str, Any]) -> dict[str, Any]:
        normalized = _analysis_id(analysis_id)
        safe_snapshot = _redact(snapshot)
        encoded_snapshot = json.dumps(safe_snapshot, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded_snapshot) > MAX_SNAPSHOT_BYTES:
            raise PlatformError(
                "ANALYSIS_WORKSPACE_SNAPSHOT_TOO_LARGE",
                "分析工作台快照超过 8 MiB 上限。",
                status_code=413,
                details={"snapshot_bytes": len(encoded_snapshot), "max_snapshot_bytes": MAX_SNAPSHOT_BYTES},
            )
        path = self._path(normalized)
        lock_path = path.with_suffix(".json.lock")
        with self._lock, file_lock(lock_path):
            existing: dict[str, Any] = {}
            if path.is_file():
                try:
                    existing = _record(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, json.JSONDecodeError):
                    existing = {}
            now = utc_now()
            value = {
                "schema_version": "analysis_workspace_snapshot/v1",
                "analysis_id": normalized,
                "revision": max(0, int(existing.get("revision", 0))) + 1,
                "created_at": str(existing.get("created_at") or now),
                "updated_at": now,
                "snapshot": safe_snapshot,
            }
            atomic_write_json(path, value)
            return value

    def read(self, analysis_id: str) -> dict[str, Any] | None:
        normalized = _analysis_id(analysis_id)
        path = self._path(normalized)
        if not path.is_file():
            return None
        with self._lock, file_lock(path.with_suffix(".json.lock")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise PlatformError(
                    "ANALYSIS_WORKSPACE_SNAPSHOT_CORRUPT",
                    "分析工作台快照不可读取。",
                    status_code=500,
                    details={"analysis_id": normalized, "reason": str(exc)},
                ) from exc
        if not isinstance(value, dict) or value.get("analysis_id") != normalized:
            raise PlatformError("ANALYSIS_WORKSPACE_SNAPSHOT_CORRUPT", "分析工作台快照格式无效。", status_code=500)
        return value

    def catalog(self, *, limit: int = 40) -> list[dict[str, Any]]:
        """Return lightweight history projections without loading UI artifacts."""

        records: list[dict[str, Any]] = []
        try:
            paths = list(self.root.glob("*.json"))
        except OSError:
            return []
        for path in paths:
            try:
                value = self.read(path.stem)
            except PlatformError:
                continue
            if value is None:
                continue
            snapshot = _record(value.get("snapshot"))
            analysis_record = _record(snapshot.get("analysis_record"))
            if str(analysis_record.get("analysisId") or "") != str(value.get("analysis_id") or ""):
                continue
            records.append({
                "analysis_id": value["analysis_id"],
                "revision": value.get("revision", 0),
                "created_at": value.get("created_at", ""),
                "updated_at": value.get("updated_at", ""),
                "analysis_record": analysis_record,
                "workflow_stage": _record(snapshot.get("workflow")).get("stage"),
            })
        records.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        return records[: max(1, min(limit, 200))]


def _wire_paths(run: dict[str, Any]) -> list[Path]:
    engine = _record(run.get("engine"))
    workspace_id = _text(engine.get("workspace_id"), 240)
    if not workspace_id:
        return []
    kimi_home = Path(os.path.expanduser(os.environ.get("KIMI_CODE_HOME", "~/.kimi-code"))).resolve()
    workspace_sessions = kimi_home / "sessions" / workspace_id
    try:
        # One durable Platform Run can outlive multiple native CLI process
        # sessions after backend restarts.  The workspace is unique to that
        # Run, so all child session journals belong to the same audit trail.
        return sorted(
            (path for path in workspace_sessions.glob("*/agents/*/wire.jsonl") if path.is_file()),
            key=lambda path: (path.stat().st_mtime, str(path)),
        )
    except OSError:
        return []


def _tool_status(result: Any) -> str:
    record = _record(result)
    if record.get("isError") is True or record.get("error"):
        return "failed"
    output = record.get("output")
    if isinstance(output, str):
        try:
            parsed = json.loads(output.split("<mcp-structured-result>", 1)[0].strip())
        except (json.JSONDecodeError, TypeError):
            parsed = None
        if isinstance(parsed, dict):
            status = _record(parsed.get("result")).get("status") or parsed.get("status")
            if status in {"failed", "error"}:
                return "failed"
    return "succeeded"


def _parse_native_wire(run: dict[str, Any]) -> dict[str, Any]:
    paths = _wire_paths(run)
    if not paths:
        return {
            "available": False,
            "source": "unavailable",
            "system_prompt": "",
            "turn_prompt": "",
            "steps": [],
            "wire_files": [],
        }

    system_prompt = ""
    turn_prompt = ""
    steps: list[dict[str, Any]] = []
    current_by_agent: dict[str, dict[str, Any]] = {}
    pending_inputs: dict[str, list[str]] = {}
    llm_requests: dict[tuple[str, int], dict[str, Any]] = {}
    row_count = 0

    for path in paths:
        try:
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for line in lines:
            row_count += 1
            if row_count > MAX_WIRE_ROWS:
                break
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(row, dict):
                continue
            kind = str(row.get("type") or "")
            agent_id = str(row.get("agentId") or path.parent.name or "main")
            if kind == "profile.bind" and not system_prompt:
                system_prompt = _text(row.get("systemPrompt"))
            elif kind == "turn.prompt":
                raw_input = row.get("input")
                if isinstance(raw_input, str):
                    turn_prompt = _text(raw_input)
                    pending_inputs.setdefault(agent_id, []).append(turn_prompt)
                elif raw_input is not None:
                    pending_inputs.setdefault(agent_id, []).append(_text(json.dumps(_redact(raw_input), ensure_ascii=False)))
            elif kind == "llm.request":
                step_number = _turn_step_number(row.get("turnStep"))
                request = _redact(row)
                llm_requests[(agent_id, step_number)] = request
                current = current_by_agent.get(agent_id)
                if current is not None and (step_number == 0 or int(current.get("step", 0)) == step_number):
                    current["model_request"] = request
            elif kind != "context.append_loop_event":
                continue

            if kind != "context.append_loop_event":
                continue
            event = _record(row.get("event"))
            event_type = str(event.get("type") or "")
            event_time = _iso_time(row.get("time"))
            if event_type == "step.begin":
                step_number = int(event.get("step", len(steps) + 1))
                current_by_agent[agent_id] = {
                    "step_id": str(event.get("uuid") or f"{agent_id}:{step_number}"),
                    "agent_id": agent_id,
                    "turn_id": str(event.get("turnId") or ""),
                    "step": step_number,
                    "started_at": event_time,
                    "ended_at": "",
                    "status": "running",
                    "finish_reason": "",
                    "input": "\n\n".join(pending_inputs.pop(agent_id, []))[:MAX_TEXT_CHARS],
                    "reasoning": "",
                    "output": "",
                    "model_request": llm_requests.pop((agent_id, step_number), {}),
                    "usage": {},
                    "latency": {},
                    "tools": [],
                }
                steps.append(current_by_agent[agent_id])
                continue
            current = current_by_agent.get(agent_id)
            if current is None:
                continue
            if event_type == "content.part":
                part = _record(event.get("part"))
                if part.get("type") == "think":
                    current["reasoning"] = f"{current['reasoning']}{_text(part.get('think'))}"[-MAX_TEXT_CHARS:]
                elif part.get("type") == "text":
                    current["output"] = f"{current['output']}{_text(part.get('text'))}"[-MAX_TEXT_CHARS:]
            elif event_type == "tool.call":
                current["tools"].append({
                    "tool_call_id": str(event.get("toolCallId") or event.get("uuid") or ""),
                    "name": _text(event.get("name"), 500),
                    "status": "running",
                    "started_at": event_time,
                    "ended_at": "",
                    "arguments": _redact(_record(event.get("args"))),
                    "result": None,
                })
            elif event_type == "tool.result":
                tool_call_id = str(event.get("toolCallId") or "")
                tool = next((item for item in reversed(current["tools"]) if item.get("tool_call_id") == tool_call_id), None)
                if tool is None:
                    tool = {
                        "tool_call_id": tool_call_id,
                        "name": "未记录工具名",
                        "status": "running",
                        "started_at": "",
                        "arguments": {},
                    }
                    current["tools"].append(tool)
                result = _redact(event.get("result"))
                tool.update({"status": _tool_status(result), "ended_at": event_time, "result": result})
                pending_inputs.setdefault(agent_id, []).append(_text(json.dumps(result, ensure_ascii=False)))
            elif event_type == "step.end":
                usage = _record(event.get("usage"))
                current.update({
                    "ended_at": event_time,
                    "status": "failed" if event.get("finishReason") in {"error", "failed"} else "succeeded",
                    "finish_reason": str(event.get("finishReason") or ""),
                    "usage": {
                        "input_tokens": int(usage.get("inputOther", 0) or 0) + int(usage.get("inputCacheRead", 0) or 0) + int(usage.get("inputCacheCreation", 0) or 0),
                        "output_tokens": int(usage.get("output", 0) or 0),
                        "cached_input_tokens": int(usage.get("inputCacheRead", 0) or 0),
                        "cache_creation_input_tokens": int(usage.get("inputCacheCreation", 0) or 0),
                    },
                    "latency": {
                        "first_token_ms": event.get("llmFirstTokenLatencyMs"),
                        "stream_ms": event.get("llmStreamDurationMs"),
                        "server_first_token_ms": event.get("llmServerFirstTokenMs"),
                        "server_decode_ms": event.get("llmServerDecodeMs"),
                    },
                })
                request = llm_requests.pop((agent_id, int(current.get("step", 0))), None)
                if request:
                    current["model_request"] = request
                current_by_agent.pop(agent_id, None)
        if row_count > MAX_WIRE_ROWS:
            break

    for current in current_by_agent.values():
        current["status"] = "running"
    return {
        "available": bool(steps),
        "source": "kimi_wire_jsonl" if steps else "wire_without_model_steps",
        "system_prompt": system_prompt,
        "turn_prompt": turn_prompt,
        "steps": steps[-200:],
        "wire_files": [str(path) for path in paths],
        "row_count": row_count,
        "truncated": row_count > MAX_WIRE_ROWS,
    }


def _event_time(event: dict[str, Any]) -> str:
    return _text(event.get("occurred_at") or event.get("received_at"), 80)


def build_analysis_execution_view(
    analysis_id: str,
    *,
    trace_store: Any,
    session_registry: Any,
    run_store: Any,
    workspace_store: AnalysisWorkspaceStore,
) -> dict[str, Any]:
    """Join persisted sources into one UI-oriented, still-auditable view."""

    normalized = _analysis_id(analysis_id)
    browser_trace = trace_store.read(normalized, limit=5000)
    workspace = workspace_store.read(normalized)
    run: dict[str, Any] | None = None
    run_events: list[dict[str, Any]] = []
    run_id = session_registry.resolve(normalized)
    if run_id:
        try:
            run = run_store.read(run_id)
            run_events = run_store.events(run_id, after_sequence=0, limit=5000)
        except Exception:
            run = None
            run_events = []
    native = _parse_native_wire(run or {})
    page_events = browser_trace.get("events") if isinstance(browser_trace.get("events"), list) else []

    normalized_events: list[dict[str, Any]] = []
    for event in page_events:
        if not isinstance(event, dict):
            continue
        normalized_events.append({
            "event_id": str(event.get("event_id") or ""),
            "occurred_at": _event_time(event),
            "source": "browser",
            "kind": str(event.get("category") or "workflow"),
            "operation": str(event.get("operation") or ""),
            "phase": str(event.get("phase") or ""),
            "status": str(event.get("status") or "observed"),
            "request_id": str(event.get("request_id") or ""),
            "run_id": str(event.get("run_id") or ""),
            "payload": _redact(_record(event.get("payload"))),
        })
    for event in run_events:
        if not isinstance(event, dict):
            continue
        event_type = str(event.get("event_type") or "")
        normalized_events.append({
            "event_id": str(event.get("event_id") or ""),
            "occurred_at": _event_time(event),
            "source": "agent_run",
            "kind": "tool" if event_type.startswith(("tool.", "ui.action")) else "agent",
            "operation": event_type,
            "phase": str(_record(event.get("payload")).get("phase") or ""),
            "status": str(event.get("state") or "observed"),
            "request_id": str(_record(event.get("payload")).get("request_id") or ""),
            "run_id": str(event.get("run_id") or run_id or ""),
            "payload": _redact(_record(event.get("payload"))),
        })
    normalized_events.sort(key=lambda event: (event.get("occurred_at") or "", event.get("event_id") or ""))

    steps = native.get("steps") if isinstance(native.get("steps"), list) else []
    tools = [tool for step in steps if isinstance(step, dict) for tool in step.get("tools", []) if isinstance(tool, dict)]
    input_tokens = sum(int(_record(step.get("usage")).get("input_tokens", 0) or 0) for step in steps if isinstance(step, dict))
    output_tokens = sum(int(_record(step.get("usage")).get("output_tokens", 0) or 0) for step in steps if isinstance(step, dict))
    cached_tokens = sum(int(_record(step.get("usage")).get("cached_input_tokens", 0) or 0) for step in steps if isinstance(step, dict))
    all_times = [
        time_value
        for time_value in [
            *[str(event.get("occurred_at") or "") for event in normalized_events],
            *[str(step.get("started_at") or "") for step in steps if isinstance(step, dict)],
            *[str(step.get("ended_at") or "") for step in steps if isinstance(step, dict)],
        ]
        if _time_millis(time_value) is not None
    ]
    start_time = min(all_times, default="")
    end_time = max(all_times, default="")
    start_ms = _time_millis(start_time)
    end_ms = _time_millis(end_time)
    record_span_ms = max(0.0, end_ms - start_ms) if start_ms is not None and end_ms is not None else None
    active_duration_ms = sum(
        max(0.0, ended - started)
        for step in steps
        if isinstance(step, dict)
        for started, ended in [(_time_millis(str(step.get("started_at") or "")), _time_millis(str(step.get("ended_at") or "")))]
        if started is not None and ended is not None
    )

    public_run = run_store.public(run) if run is not None else None
    run_context = _record(_record(run or {}).get("context"))
    runtime_environment = _record(run_context.get("runtime_environment"))
    run_engine = _record(_record(run or {}).get("engine"))
    return {
        "schema_version": "analysis_execution_view/v1",
        "analysis_id": normalized,
        "generated_at": utc_now(),
        "coverage": {
            "workspace_snapshot": workspace is not None,
            "browser_events": len(page_events) > 0,
            "agent_run": run is not None,
            "native_model_transcript": bool(native.get("available")),
            "native_model_source": native.get("source"),
        },
        "workspace": workspace,
        "agent_session": {
            "bound": run is not None,
            "run": public_run,
            "run_id": run_id,
            "adapter_id": runtime_environment.get("agent_adapter_id") if run else None,
            "native_session_id": run_engine.get("session_id") if run else None,
        },
        "summary": {
            "started_at": start_time,
            "ended_at": end_time,
            "duration_ms": active_duration_ms if steps else record_span_ms,
            "record_span_ms": record_span_ms,
            "model_step_count": len(steps),
            "tool_call_count": len(tools),
            "tool_success_count": sum(1 for tool in tools if tool.get("status") == "succeeded"),
            "tool_failure_count": sum(1 for tool in tools if tool.get("status") == "failed"),
            "browser_event_count": len(page_events),
            "agent_event_count": len(run_events),
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_input_tokens": cached_tokens,
            "cache_hit_rate": cached_tokens / input_tokens if input_tokens > 0 else None,
        },
        "model_transcript": native,
        "events": normalized_events[-5000:],
        "raw_sources": {
            "browser_trace_id": browser_trace.get("trace_id"),
            "browser_trace_relative_path": browser_trace.get("relative_path"),
            "agent_execution_trace": f"/v1/agent-runs/{run_id}/execution-trace" if run_id else None,
        },
    }
