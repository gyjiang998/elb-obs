"""Durable, backend-owned Agent Runtime for the workbench.

The original Phase-A page assistant ran its ReAct loop inside a browser tab.
This module is the durable control plane described by
``platform_agent_runtime_build_device_bridge_spec.md``: a run has an
append-only event log, a recoverable checkpoint, an ephemeral BYOK credential
lease, and typed action ownership.  It intentionally contains no FastAPI or
HDC code so it can be exercised with deterministic model/action fakes.

Only a small amount of state is mutable.  Evidence, build outputs and device
results remain immutable artifacts owned by their corresponding executors.
"""

from __future__ import annotations

import asyncio
import json
import re
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .core import PlatformError, atomic_write_json, file_lock, sha256_json, utc_now
from .page_analysis_workflow import page_analysis_progress
from .scenario_capture_target import capture_target
from .skill_bootstrap import (
    SKILL_BOOTSTRAP_ACK_ACTION,
    acknowledge_skill_bootstrap,
    public_skill_bootstrap,
)


RUN_ID_PATTERN = re.compile(r"run_[a-f0-9]{24}")
EVENT_ID_PATTERN = re.compile(r"evt_[a-f0-9]{24}")
MAX_RUN_EVENT_BYTES = 2 * 1024 * 1024
MAX_RUN_EVENTS = 20_000
# The OpenAI-compatible relay accepts at most 24 messages.  Two stable system
# messages and the current observation leave room for 21 history items; retain
# a little headroom for an explicit compacted-facts system message.
MAX_MEMORY_MESSAGES = 18
MAX_MESSAGE_CHARS = 96 * 1024
MAX_STREAM_EVENT_CHARS = 24 * 1024
DEFAULT_LEASE_TTL_SECONDS = 30 * 60
EXTERNAL_UI_ACTION_TIMEOUT_SECONDS = 15 * 60

RUN_STATES = frozenset(
    {
        "created",
        "waiting_for_model_credential",
        "running",
        "waiting_for_tool",
        "waiting_for_ui",
        "waiting_for_device",
        "compacting",
        "paused_model_overloaded",
        "paused_output_limit",
        "paused_turn_budget",
        "paused_bridge_offline",
        "completed",
        "failed",
        "cancelled",
    }
)

PAUSED_STATES = frozenset({"paused_model_overloaded", "paused_output_limit", "paused_turn_budget", "paused_bridge_offline"})
TERMINAL_STATES = frozenset({"completed", "failed", "cancelled"})
ACTION_RESULT_FAILURE_STATUSES = frozenset(
    {
        "failed",
        "compile_failed",
        "deployment_failed",
        "dependency_pull_failed",
        "timed_out",
        "cancelled",
        "state_unknown",
        "unsupported",
    }
)
DEVICE_VALIDATION_TERMINAL_BLOCKER_CODES = frozenset(
    {
        "BUILD_NETWORK_SANDBOX_UNAVAILABLE",
        "BUILD_PROFILE_RUNTIME_UNAVAILABLE",
        "BUILD_TOOLCHAIN_UNAVAILABLE",
        "BRIDGE_OFFLINE",
        "DEVICE_HDC_UNAVAILABLE",
        "DEVICE_NOT_CONNECTED",
        "DEVICE_TOOL_UNAVAILABLE",
    }
)

_SECRET_FIELD = re.compile(
    r"(?:^|_)(?:api_?key|authorization|secret|credential|token|password|pairing_?code)(?:$|_)",
    re.IGNORECASE,
)
_INLINE_SECRET = re.compile(
    r"(?i)(?:\b(?:bearer|api[_ -]?key|token|secret|pairing[_ -]?code)\s*[:=]\s*|\bsk-)[A-Za-z0-9_./+=:-]{12,}"
)


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any, limit: int = MAX_MESSAGE_CHARS) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _reported_action_failure(result: Any) -> dict[str, Any] | None:
    """Translate a typed executor's terminal status into an Agent fact.

    Executors deliberately return structured artifacts even when a build or a
    device command fails.  Treating every non-exception return as success made
    the next model turn believe that a failed build/deploy had completed.  The
    full result remains attached to the action observation; this helper only
    gives the ReAct loop an unambiguous status and concise error identity.
    """

    value = _record(result)
    status = _text(value.get("status"), 80).casefold()
    if status not in ACTION_RESULT_FAILURE_STATUSES:
        return None
    error = _record(value.get("error"))
    message = (
        _text(error.get("message"), 1200)
        or _text(value.get("summary_zh"), 1200)
        or _text(value.get("detail_zh"), 1200)
        or _text(value.get("detail"), 1200)
        or f"Action 返回终态 {status}。"
    )
    reported_details = _record(error.get("details"))
    return {
        "code": _text(error.get("code"), 120) or f"ACTION_REPORTED_{status.upper()}",
        "message": message,
        "details": {"reported_status": status, **reported_details},
    }


def _action_error_code(observation: dict[str, Any]) -> str:
    direct = _record(observation.get("error"))
    result = _record(observation.get("result"))
    nested = _record(result.get("error"))
    return _text(direct.get("code"), 120) or _text(nested.get("code"), 120)


def _safe_json(value: Any, *, depth: int = 0) -> Any:
    """Bound and redact data before it is persisted to a run event.

    Model keys only ever live in :class:`CredentialLeaseStore`; this defence in
    depth guard also prevents a future action implementation from leaking one
    into an append-only execution trace.
    """

    if depth > 20:
        return "[payload nesting limit]"
    if isinstance(value, dict):
        return {
            str(key): "[redacted]" if _SECRET_FIELD.search(str(key)) else _safe_json(item, depth=depth + 1)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_safe_json(item, depth=depth + 1) for item in value[:256]]
    if isinstance(value, tuple):
        return [_safe_json(item, depth=depth + 1) for item in value[:256]]
    if isinstance(value, str):
        # A model is never deliberately given the BYOK value, but a user can
        # paste credentials into a chat turn.  Run traces are durable, so make
        # accidental inline secrets non-recoverable before they reach disk.
        return _INLINE_SECRET.sub("[redacted]", value[:MAX_MESSAGE_CHARS])
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)[:MAX_MESSAGE_CHARS]


def _action_input_fact(action_id: str, value: dict[str, Any]) -> dict[str, Any]:
    """Persist action identity fields without replaying authored source text."""

    projected = _record(_safe_json(value))
    if action_id != "build.stage_source":
        return projected
    projected = dict(projected)
    for field in ("source", "shader_source"):
        raw = value.get(field)
        projected.pop(field, None)
        if isinstance(raw, str) and raw:
            projected[f"{field}_payload"] = {
                "characters": len(raw),
                "content_sha256": sha256_json({field: raw}),
                "retention": "immutable build_source artifact",
            }
    return projected


def _bounded_json_line(value: dict[str, Any]) -> bytes:
    encoded = (json.dumps(_safe_json(value), ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
    if len(encoded) > MAX_RUN_EVENT_BYTES:
        raise PlatformError(
            "AGENT_RUN_EVENT_TOO_LARGE",
            "Agent Run 的单条事件超过 2 MiB 上限。",
            status_code=413,
            details={"event_bytes": len(encoded), "max_event_bytes": MAX_RUN_EVENT_BYTES},
        )
    return encoded


def _run_id(value: str) -> str:
    normalized = value.strip()
    if not RUN_ID_PATTERN.fullmatch(normalized):
        raise PlatformError("AGENT_RUN_NOT_FOUND", "Agent Run 标识无效。", status_code=404)
    return normalized


def _event_id(value: str) -> str:
    normalized = value.strip()
    if not EVENT_ID_PATTERN.fullmatch(normalized):
        raise PlatformError("AGENT_EVENT_NOT_FOUND", "Agent Run 事件标识无效。", status_code=404)
    return normalized


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


class AgentRuntimeError(PlatformError):
    """A safe, stable runtime error used by the API adapter."""


class ModelGatewayError(Exception):
    """A model transport error with pause semantics.

    ``retryable`` is intentionally narrower than generic network errors.  The
    runtime only auto-resumes errors for which repeating the same request is a
    meaningful operation; all others retain an exact checkpoint for the user.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 502,
        retryable: bool = False,
        retry_after_seconds: int | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.retryable = retryable
        self.retry_after_seconds = retry_after_seconds
        self.details = details or {}


@dataclass(frozen=True)
class ModelLease:
    """An in-memory BYOK lease.  ``api_key`` is never serialized."""

    lease_id: str
    run_id: str
    base_url: str
    model: str
    api_key: str
    issued_at: str
    expires_at_epoch: float

    def public(self) -> dict[str, Any]:
        return {
            "lease_id": self.lease_id,
            "run_id": self.run_id,
            "base_url": self.base_url,
            "model": self.model,
            "expires_at_epoch": self.expires_at_epoch,
        }


class CredentialLeaseStore:
    """Thread-safe, process-local storage for browser supplied model keys."""

    def __init__(self, *, default_ttl_seconds: int = DEFAULT_LEASE_TTL_SECONDS) -> None:
        self.default_ttl_seconds = max(60, int(default_ttl_seconds))
        self._leases: dict[str, ModelLease] = {}
        self._run_index: dict[str, str] = {}
        self._lock = threading.RLock()

    def issue(
        self,
        run_id: str,
        *,
        base_url: str,
        model: str,
        api_key: str,
        ttl_seconds: int | None = None,
    ) -> ModelLease:
        _run_id(run_id)
        if not _text(base_url, 512) or not _text(model, 240) or not isinstance(api_key, str) or not api_key.strip():
            raise AgentRuntimeError("MODEL_CREDENTIAL_INVALID", "模型服务地址、模型名或 API 密钥为空。", status_code=422)
        lifetime = max(60, min(int(ttl_seconds or self.default_ttl_seconds), 24 * 60 * 60))
        lease = ModelLease(
            lease_id=_new_id("lease"),
            run_id=run_id,
            base_url=base_url.strip().rstrip("/"),
            model=model.strip(),
            api_key=api_key,
            issued_at=utc_now(),
            expires_at_epoch=time.time() + lifetime,
        )
        with self._lock:
            previous = self._run_index.get(run_id)
            if previous:
                self._leases.pop(previous, None)
            self._leases[lease.lease_id] = lease
            self._run_index[run_id] = lease.lease_id
        return lease

    def get_for_run(self, run_id: str) -> ModelLease | None:
        _run_id(run_id)
        with self._lock:
            lease_id = self._run_index.get(run_id)
            lease = self._leases.get(lease_id or "")
            if lease is None:
                return None
            if lease.expires_at_epoch <= time.time():
                self._leases.pop(lease.lease_id, None)
                self._run_index.pop(run_id, None)
                return None
            return lease

    def revoke(self, run_id: str) -> None:
        with self._lock:
            lease_id = self._run_index.pop(run_id, None)
            if lease_id:
                self._leases.pop(lease_id, None)


@dataclass(frozen=True)
class AgentDecision:
    reply: str
    actions: list[dict[str, Any]]
    disposition: str
    recovered: bool = False


class DecisionParseError(ValueError):
    pass


_AGENT_SOURCE_BLOCK = re.compile(
    r'<agent-source\s+id="(?P<source_id>[A-Za-z][A-Za-z0-9_.-]{0,79})"'
    r'(?:\s+language="(?P<language>c|cpp)")?\s*>(?P<source>[\s\S]*?)</agent-source>',
    flags=re.IGNORECASE,
)


def _extract_agent_source_blocks(text: str) -> tuple[str, dict[str, str]]:
    """Detach raw source payloads from the JSON control envelope.

    Long C/C++ programs are materially more reliable when the model writes
    normal source text instead of a heavily escaped JSON string.  The control
    object references these blocks by ID; this parser preserves every byte
    between the tags except the optional first formatting newline.
    """

    blocks: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        source_id = match.group("source_id")
        if source_id in blocks:
            raise DecisionParseError(f"模型返回了重复的 agent-source id：{source_id}。")
        source = match.group("source")
        if source.startswith("\r\n"):
            source = source[2:]
        elif source.startswith("\n"):
            source = source[1:]
        if not source.strip():
            raise DecisionParseError(f"agent-source {source_id} 为空。")
        if len(source) > 192 * 1024:
            raise DecisionParseError(f"agent-source {source_id} 超过 192 KiB 上限。")
        blocks[source_id] = source
        return ""

    control_text = _AGENT_SOURCE_BLOCK.sub(replace, text)
    return control_text, blocks


def _resolve_agent_source_refs(actions: list[dict[str, Any]], source_blocks: dict[str, str]) -> list[dict[str, Any]]:
    resolved: list[dict[str, Any]] = []
    referenced: set[str] = set()
    for action in actions:
        action_id = _text(action.get("action_id"), 160)
        action_input = dict(_record(action.get("input")))
        source_ref = _text(action_input.get("source_ref"), 80)
        if source_ref:
            if action_id != "build.stage_source":
                raise DecisionParseError("source_ref 只能用于 build.stage_source。")
            if "source" in action_input:
                raise DecisionParseError("build.stage_source 不能同时提供 source 与 source_ref。")
            source = source_blocks.get(source_ref)
            if source is None:
                raise DecisionParseError(f"build.stage_source 引用了不存在的 agent-source：{source_ref}。")
            action_input.pop("source_ref", None)
            action_input["source"] = source
            referenced.add(source_ref)
        resolved.append({"action_id": action_id, "input": action_input})
    unreferenced = sorted(set(source_blocks) - referenced)
    if unreferenced:
        raise DecisionParseError(f"存在未被 action 引用的 agent-source：{', '.join(unreferenced)}。")
    return resolved


def _decision_memory_content(decision: AgentDecision) -> str:
    """Retain control intent in memory without replaying complete source."""

    actions: list[dict[str, Any]] = []
    for action in decision.actions:
        action_input = dict(_record(action.get("input")))
        source = action_input.pop("source", None)
        if isinstance(source, str):
            action_input["source_payload"] = {
                "transport": "agent-source",
                "characters": len(source),
                "content_sha256": sha256_json({"source": source}),
                "retention": "model.response 与后续 build_source artifact",
            }
        actions.append({"action_id": action.get("action_id"), "input": action_input})
    return json.dumps(
        {
            "reply": decision.reply,
            "disposition": decision.disposition,
            "actions": actions,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _extract_json_objects(text: str) -> list[str]:
    """Extract balanced JSON objects without trusting Markdown fences.

    Coding models often wrap tool payloads in `````json`` fences and generated
    C literals may contain braces.  A normal regex would stop at the first
    literal brace; this small scanner tracks JSON strings and escapes.
    """

    candidates: list[str] = []
    starts = [match.start() for match in re.finditer(r"\{", text)]
    for start in starts:
        depth = 0
        in_string = False
        escaped = False
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : index + 1])
                    break
    return candidates


def _repair_json_string_escapes(value: str) -> str:
    """Repair only invalid JSON escapes inside strings.

    The repair is intentionally conservative.  It makes a generated C literal
    such as ``'\\0'`` valid JSON by escaping the slash, but does not invent
    fields, strip arbitrary syntax or execute any embedded code.
    """

    output: list[str] = []
    index = 0
    in_string = False
    while index < len(value):
        char = value[index]
        if not in_string:
            output.append(char)
            if char == '"':
                in_string = True
            index += 1
            continue
        if char == '"':
            output.append(char)
            in_string = False
            index += 1
            continue
        if char == "\\":
            next_char = value[index + 1] if index + 1 < len(value) else ""
            if next_char in {'"', "\\", "/", "b", "f", "n", "r", "t"}:
                output.extend((char, next_char))
                index += 2
                continue
            if next_char == "u" and index + 5 < len(value) and re.fullmatch(r"[0-9a-fA-F]{4}", value[index + 2 : index + 6]):
                output.append(value[index : index + 6])
                index += 6
                continue
            # Retain the source character, but make it a literal backslash.
            output.append("\\\\")
            index += 1
            continue
        code = ord(char)
        if code < 0x20:
            escapes = {"\n": "\\n", "\r": "\\r", "\t": "\\t"}
            output.append(escapes.get(char, f"\\u{code:04x}"))
        else:
            output.append(char)
        index += 1
    return "".join(output)


def _recover_json_string_field(content: str, key: str) -> str:
    match = re.search(rf'"{re.escape(key)}"\s*:\s*("(?:\\.|[^"\\])*")', content, flags=re.DOTALL)
    if not match:
        return ""
    try:
        value = json.loads(match.group(1))
    except json.JSONDecodeError:
        return ""
    return _text(value, 12 * 1024) if isinstance(value, str) else ""


def _recover_complete_action_objects(content: str) -> list[dict[str, Any]]:
    """Recover only complete first-level actions from a broken outer array.

    This accepts the common ``missing closing ]`` failure while deliberately
    refusing partial JSON objects.  It mirrors the conservative page-runtime
    recovery rule and never invents an action that the provider did not emit.
    """

    marker = re.search(r'"actions"\s*:\s*\[', content)
    if not marker:
        return []
    cursor = marker.end()
    recovered: list[dict[str, Any]] = []
    while cursor < len(content):
        while cursor < len(content) and (content[cursor].isspace() or content[cursor] == ","):
            cursor += 1
        if cursor >= len(content) or content[cursor] in "]}":
            break
        if content[cursor] != "{":
            break
        depth = 0
        in_string = False
        escaped = False
        end: int | None = None
        for index in range(cursor, len(content)):
            char = content[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    end = index + 1
                    break
        if end is None:
            break
        raw = content[cursor:end]
        try:
            value = json.loads(raw)
        except json.JSONDecodeError:
            try:
                value = json.loads(_repair_json_string_escapes(raw))
            except json.JSONDecodeError:
                value = None
        if isinstance(value, dict):
            action_id = _text(value.get("action_id"), 160)
            action_input = value.get("input")
            if action_id and isinstance(action_input, dict):
                recovered.append({"action_id": action_id, "input": action_input})
        cursor = end
    return recovered


def parse_agent_decision(content: str) -> AgentDecision:
    """Parse one strict Agent response and reject silent text fallbacks.

    Unlike the legacy browser parser, a malformed tool envelope is never
    interpreted as a completed task.  A repaired form is accepted only when it
    parses into the registered outer JSON structure; otherwise callers receive
    :class:`DecisionParseError` and can request a correction in the next turn.
    """

    text = _text(content, MAX_MESSAGE_CHARS * 2)
    if not text:
        raise DecisionParseError("模型没有返回可解析的 JSON 决策。")
    control_text, source_blocks = _extract_agent_source_blocks(text)
    fences = re.findall(r"```(?:json)?\s*([\s\S]*?)```", control_text, flags=re.IGNORECASE)
    candidates = [*fences, control_text, *_extract_json_objects(control_text)]
    parsed_value: Any = None
    for candidate in candidates:
        candidate = candidate.strip()
        if not candidate:
            continue
        for variant in (candidate, _repair_json_string_escapes(candidate)):
            try:
                parsed_value = json.loads(variant)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed_value, str):
                try:
                    parsed_value = json.loads(parsed_value)
                except json.JSONDecodeError:
                    pass
            if isinstance(parsed_value, dict):
                break
        if isinstance(parsed_value, dict):
            break
    if not isinstance(parsed_value, dict):
        reply = _recover_json_string_field(control_text, "reply")
        actions = _recover_complete_action_objects(control_text)
        if reply and actions:
            disposition = _recover_json_string_field(text, "disposition") or "continue"
            if disposition not in {"continue", "complete", "wait_for_user"}:
                disposition = "continue"
            return AgentDecision(
                reply=reply,
                actions=_resolve_agent_source_refs(actions, source_blocks),
                disposition=disposition,
                recovered=True,
            )
        raise DecisionParseError("模型输出不是可解析的 JSON 对象。")
    reply = _text(parsed_value.get("reply"), 12 * 1024)
    if not reply:
        raise DecisionParseError("模型 JSON 决策缺少 reply。")
    raw_actions = parsed_value.get("actions")
    if not isinstance(raw_actions, list):
        raise DecisionParseError("模型 JSON 决策缺少 actions 数组。")
    actions: list[dict[str, Any]] = []
    for item in raw_actions:
        action = _record(item)
        action_id = _text(action.get("action_id"), 160)
        action_input = action.get("input")
        if not action_id or not isinstance(action_input, dict):
            raise DecisionParseError("模型 action 必须包含 action_id 与 object 类型的 input。")
        actions.append({"action_id": action_id, "input": action_input})
    disposition = _text(parsed_value.get("disposition"), 40) or ("continue" if actions else "complete")
    if disposition not in {"continue", "complete", "wait_for_user"}:
        raise DecisionParseError("模型 disposition 只能是 continue、complete 或 wait_for_user。")
    return AgentDecision(
        reply=reply,
        actions=_resolve_agent_source_refs(actions, source_blocks),
        disposition=disposition,
    )


class ModelGateway(Protocol):
    def __call__(
        self,
        lease: ModelLease,
        messages: list[dict[str, str]],
        *,
        max_tokens: int,
        on_stream: Callable[[dict[str, Any]], None],
        cancel_event: threading.Event,
    ) -> dict[str, Any]: ...


class ActionRouter(Protocol):
    def owner(self, action_id: str) -> str: ...

    def validate(self, action_id: str, input_value: dict[str, Any], run: dict[str, Any]) -> None: ...

    def execute(self, action_id: str, input_value: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]: ...

    def can_reuse(self, action_id: str, input_value: dict[str, Any], prior_result: dict[str, Any], run: dict[str, Any]) -> bool: ...

    def cancel(self, run: dict[str, Any]) -> None: ...


class AgentRunStore:
    """Append-only event storage plus compact mutable run/checkpoint state."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conditions: dict[str, threading.Condition] = {}
        self._async_waiters: dict[str, set[tuple[asyncio.AbstractEventLoop, asyncio.Event]]] = {}

    def _run_dir(self, run_id: str) -> Path:
        return self.root / _run_id(run_id)

    def _run_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "run.json"

    def _events_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / "events.jsonl"

    def _execution_trace_path(self, run_id: str) -> Path:
        """Return the spec-visible immutable execution-trace export path.

        ``events.jsonl`` remains for the lightweight event reader used by the
        runtime.  The second name is intentionally explicit: it is the file a
        user can archive with an analysis record without needing to know an
        internal implementation detail.
        """

        return self._run_dir(run_id) / "analysis_execution_trace.jsonl"

    def _lock_path(self, run_id: str) -> Path:
        return self._run_dir(run_id) / ".run.lock"

    def _condition(self, run_id: str) -> threading.Condition:
        with self._lock:
            return self._conditions.setdefault(run_id, threading.Condition(self._lock))

    def _notify_async_waiters_locked(self, run_id: str) -> None:
        stale: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []
        for loop, event in tuple(self._async_waiters.get(run_id, ())):
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                stale.append((loop, event))
        for waiter in stale:
            self._async_waiters.get(run_id, set()).discard(waiter)

    def create(
        self,
        *,
        project_id: str,
        goal: str,
        initial_input: str,
        mode: str,
        context: dict[str, Any],
        budget: dict[str, int],
        action_catalog_version: str,
        skill_versions: list[dict[str, str]],
    ) -> dict[str, Any]:
        run_id = _new_id("run")
        now = utc_now()
        normalized_budget = {
            "max_turns": max(1, min(int(budget.get("max_turns", 10)), 100)),
            "max_actions_per_turn": max(1, min(int(budget.get("max_actions_per_turn", 8)), 12)),
            "max_output_tokens": max(256, min(int(budget.get("max_output_tokens", 8192)), 64 * 1024)),
            "context_window_tokens": max(8 * 1024, min(int(budget.get("context_window_tokens", 128 * 1024)), 1024 * 1024)),
        }
        run = {
            "schema_version": "agent_run/v1",
            "run_id": run_id,
            "project_id": project_id,
            "mode": _text(mode, 80) or "page_agent",
            "goal": _text(goal, 16 * 1024),
            "state": "created",
            "state_detail": "任务已创建，等待模型凭证或启动。",
            "created_at": now,
            "updated_at": now,
            "revision": 1,
            "current_turn": 0,
            "budget": normalized_budget,
            "action_catalog_version": _text(action_catalog_version, 120),
            "skill_versions": [_safe_json(item) for item in skill_versions[:32]],
            "context": _safe_json(context),
            "checkpoint": {
                "schema_version": "agent_checkpoint/v1",
                "next_input": _text(initial_input, MAX_MESSAGE_CHARS),
                "memory": [],
                "unresolved_goal": _text(goal, 16 * 1024),
                "completed_action_keys": {},
                "last_action_results": [],
                "invalid_model_output_count": 0,
                "saved_at": now,
            },
            "pending_ui_actions": [],
            "cancel_requested": False,
            "model_lease": {"status": "missing"},
            "next_event_sequence": 1,
            "execution_trace_file": "analysis_execution_trace.jsonl",
        }
        run_dir = self._run_dir(run_id)
        # ``file_lock`` creates its parent directory.  Taking a lock *inside*
        # a not-yet-created Run directory used to create ``run_dir`` before
        # this mkdir and made every new Agent Run fail with FileExistsError.
        # The random run ID plus mkdir is the cross-process creation gate; the
        # per-run file lock starts only after that directory exists.
        run_dir.mkdir(parents=True, exist_ok=False)
        with self._lock, file_lock(self._lock_path(run_id)):
            atomic_write_json(self._run_path(run_id), run)
            self._append_event_locked(run, "run.created", "created", {"goal": run["goal"], "mode": run["mode"], "budget": normalized_budget})
            # _append_event_locked advances the durable SSE cursor.  Persist it
            # before another worker can mutate the Run, otherwise run.created
            # and the first state transition both receive sequence 1.
            atomic_write_json(self._run_path(run_id), run)
        return self.public(run)

    def _read_locked(self, run_id: str) -> dict[str, Any]:
        path = self._run_path(run_id)
        if not path.is_file():
            raise AgentRuntimeError("AGENT_RUN_NOT_FOUND", "未找到指定的 Agent Run。", status_code=404)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentRuntimeError("AGENT_RUN_CORRUPT", "Agent Run 状态文件不可读取。", status_code=500) from exc
        if not isinstance(value, dict):
            raise AgentRuntimeError("AGENT_RUN_CORRUPT", "Agent Run 状态文件格式无效。", status_code=500)
        return value

    def read(self, run_id: str) -> dict[str, Any]:
        normalized = _run_id(run_id)
        with self._lock, file_lock(self._lock_path(normalized)):
            return self._read_locked(normalized)

    def list_unfinished(self) -> list[dict[str, Any]]:
        """Read recoverable runs without trusting directory names blindly."""

        runs: list[dict[str, Any]] = []
        with self._lock:
            for candidate in sorted(self.root.glob("run_*"), key=lambda item: item.name):
                if not candidate.is_dir() or not RUN_ID_PATTERN.fullmatch(candidate.name):
                    continue
                try:
                    with file_lock(self._lock_path(candidate.name)):
                        run = self._read_locked(candidate.name)
                except AgentRuntimeError:
                    continue
                if run.get("state") not in TERMINAL_STATES:
                    runs.append(run)
        return runs

    def list_recent(self, *, limit: int = 100, order_by: str = "updated_at") -> list[dict[str, Any]]:
        """Return bounded public Run summaries ordered by creation or update time."""

        bounded = max(1, min(int(limit), 500))
        runs: list[dict[str, Any]] = []
        with self._lock:
            for candidate in self.root.glob("run_*"):
                if not candidate.is_dir() or not RUN_ID_PATTERN.fullmatch(candidate.name):
                    continue
                try:
                    with file_lock(self._lock_path(candidate.name)):
                        run = self._read_locked(candidate.name)
                except AgentRuntimeError:
                    continue
                public = self.public(run)
                # Environment comparison needs the non-secret runtime identity,
                # target profile and selected Skill profile. Source text and
                # browser credentials remain excluded.
                public["context"] = _safe_json({
                    key: value
                    for key, value in _record(run.get("context")).items()
                    if key in {
                        "analysis_record_id",
                        "device_execution_mode",
                        "target_platform_profile",
                        "skill_profile",
                        "source_connector_id",
                        "source_snapshot_id",
                        "runtime_environment",
                    }
                })
                runs.append(public)
        sort_field = "created_at" if order_by == "created_at" else "updated_at"
        runs.sort(
            key=lambda item: str(item.get(sort_field) or item.get("created_at") or ""),
            reverse=True,
        )
        return runs[:bounded]

    def find_latest_by_analysis_id(self, analysis_id: str) -> dict[str, Any] | None:
        """Find a pre-index Run when upgrading to analysis-scoped sessions."""

        normalized = _text(analysis_id, 160)
        if not normalized:
            return None
        matches: list[dict[str, Any]] = []
        with self._lock:
            for candidate in self.root.glob("run_*"):
                if not candidate.is_dir() or not RUN_ID_PATTERN.fullmatch(candidate.name):
                    continue
                try:
                    with file_lock(self._lock_path(candidate.name)):
                        run = self._read_locked(candidate.name)
                except AgentRuntimeError:
                    continue
                context = _record(run.get("context"))
                if _text(context.get("analysis_record_id"), 160) == normalized:
                    matches.append(run)
        if not matches:
            return None
        matches.sort(key=lambda item: str(item.get("updated_at") or item.get("created_at") or ""), reverse=True)
        return matches[0]

    def find_latest_by_page_session(
        self,
        page_session_id: str,
        *,
        project_id: str = "",
        preferred_adapter_id: str = "",
    ) -> dict[str, Any] | None:
        """Recover the durable Run owned by a browser page.

        PTY identifiers are process-local and necessarily become stale after a
        backend restart.  ``page_session_id`` is the durable ownership key; the
        adapter is only a preference because switching Agent frameworks must
        retain the same Platform Run and its artifacts.
        """

        normalized_page_session_id = _text(page_session_id, 160)
        normalized_project_id = _text(project_id, 160)
        normalized_adapter_id = _text(preferred_adapter_id, 80)
        if not normalized_page_session_id:
            return None
        matches: list[dict[str, Any]] = []
        with self._lock:
            for candidate in self.root.glob("run_*"):
                if not candidate.is_dir() or not RUN_ID_PATTERN.fullmatch(candidate.name):
                    continue
                try:
                    with file_lock(self._lock_path(candidate.name)):
                        run = self._read_locked(candidate.name)
                except AgentRuntimeError:
                    continue
                context = _record(run.get("context"))
                if _text(context.get("page_session_id"), 160) != normalized_page_session_id:
                    continue
                if normalized_project_id and _text(run.get("project_id"), 160) != normalized_project_id:
                    continue
                matches.append(run)
        if not matches:
            return None

        substantive_event_types = {
            "agent.entry_decision",
            "engine.prompt_accepted",
            "engine.prompt_completed",
            "run.user_message",
            "terminal.command_submitted",
            "terminal.command_reused",
        }

        def has_substantive_activity(run: dict[str, Any]) -> int:
            try:
                with self._events_path(str(run["run_id"])).open("r", encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            event_type = str(_record(json.loads(line)).get("event_type") or "")
                        except (json.JSONDecodeError, TypeError):
                            continue
                        if (
                            event_type in substantive_event_types
                            or event_type.startswith("model.")
                            or event_type.startswith("tool.")
                            or event_type.startswith("ui.action_")
                        ):
                            return 1
            except OSError:
                return 0
            return 0

        def recovery_rank(run: dict[str, Any]) -> tuple[int, str, str, int, str, int]:
            transport = _record(_record(run.get("engine")).get("terminal_transport"))
            adapter_match = int(
                bool(normalized_adapter_id)
                and _text(transport.get("adapter_id"), 80) == normalized_adapter_id
            )
            return (
                has_substantive_activity(run),
                _text(transport.get("updated_at"), 80),
                _text(run.get("updated_at"), 80),
                int(run.get("next_event_sequence") or 0),
                _text(run.get("created_at"), 80),
                adapter_match,
            )

        matches.sort(key=recovery_rank, reverse=True)
        return matches[0]

    def public(self, run: dict[str, Any]) -> dict[str, Any]:
        checkpoint = _record(run.get("checkpoint"))
        return {
            "schema_version": "agent_run/v1",
            "run_id": run.get("run_id"),
            "project_id": run.get("project_id"),
            "mode": run.get("mode"),
            "goal": run.get("goal"),
            "state": run.get("state"),
            "state_detail": run.get("state_detail"),
            "created_at": run.get("created_at"),
            "updated_at": run.get("updated_at"),
            "current_turn": run.get("current_turn"),
            "event_cursor": max(0, int(run.get("next_event_sequence", 1)) - 1),
            "budget": _safe_json(_record(run.get("budget"))),
            "action_catalog_version": run.get("action_catalog_version"),
            "skill_versions": _safe_json(run.get("skill_versions") if isinstance(run.get("skill_versions"), list) else []),
            "pending_ui_actions": _safe_json(run.get("pending_ui_actions") if isinstance(run.get("pending_ui_actions"), list) else []),
            "checkpoint": {
                "saved_at": checkpoint.get("saved_at"),
                "unresolved_goal": checkpoint.get("unresolved_goal"),
                "last_action_results": _safe_json(checkpoint.get("last_action_results") if isinstance(checkpoint.get("last_action_results"), list) else []),
                "invalid_model_output_count": checkpoint.get("invalid_model_output_count", 0),
                "completed_action_count": len(_record(checkpoint.get("completed_action_keys"))),
            },
            "analysis_workflow": _safe_json(page_analysis_progress(run)),
            "skill_bootstrap": _safe_json(public_skill_bootstrap(run.get("skill_bootstrap"))),
            "model_lease": _safe_json(_record(run.get("model_lease"))),
            "engine": _safe_json(_record(run.get("engine"))),
            "cancel_requested": bool(run.get("cancel_requested")),
            "execution_trace": {
                "file_name": "analysis_execution_trace.jsonl",
                "available": self._execution_trace_path(str(run.get("run_id"))).is_file(),
            },
        }

    def _write_locked(self, run: dict[str, Any]) -> None:
        run["updated_at"] = utc_now()
        run["revision"] = int(run.get("revision", 0)) + 1
        atomic_write_json(self._run_path(str(run["run_id"])), run)

    def _append_event_locked(self, run: dict[str, Any], event_type: str, state: str, payload: dict[str, Any]) -> dict[str, Any]:
        sequence = int(run.get("next_event_sequence", 1))
        if sequence > MAX_RUN_EVENTS:
            raise AgentRuntimeError(
                "AGENT_RUN_EVENT_LIMIT",
                "此 Agent Run 的可追溯事件已达到上限；请从当前检查点创建后续 Run。",
                status_code=409,
                details={"max_events": MAX_RUN_EVENTS},
            )
        event = {
            "schema_version": "agent_run_event/v1",
            "event_id": _new_id("evt"),
            "sequence": sequence,
            "run_id": run["run_id"],
            "occurred_at": utc_now(),
            "event_type": event_type,
            "state": state,
            "payload": _safe_json(payload),
        }
        encoded = _bounded_json_line(event)
        paths = (self._events_path(str(run["run_id"])), self._execution_trace_path(str(run["run_id"])))
        for path in paths:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("ab") as handle:
                handle.write(encoded)
                handle.flush()
        run["next_event_sequence"] = sequence + 1
        self._condition(str(run["run_id"])).notify_all()
        self._notify_async_waiters_locked(str(run["run_id"]))
        return event

    def mutate(
        self,
        run_id: str,
        *,
        event_type: str,
        event_state: str,
        payload: dict[str, Any],
        apply: Callable[[dict[str, Any]], None],
    ) -> dict[str, Any]:
        normalized = _run_id(run_id)
        with self._lock, file_lock(self._lock_path(normalized)):
            run = self._read_locked(normalized)
            apply(run)
            self._append_event_locked(run, event_type, event_state, payload)
            self._write_locked(run)
            return run

    def transition(self, run_id: str, state: str, detail: str, *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if state not in RUN_STATES:
            raise AgentRuntimeError("AGENT_RUN_STATE_INVALID", "Agent Run 状态不受支持。", status_code=422)
        return self.mutate(
            run_id,
            event_type="run.state_changed",
            event_state=state,
            payload={"detail": detail, **(payload or {})},
            apply=lambda run: run.update({"state": state, "state_detail": _text(detail, 1200)}),
        )

    def append_event(self, run_id: str, event_type: str, state: str, payload: dict[str, Any]) -> dict[str, Any]:
        return self.mutate(
            run_id,
            event_type=event_type,
            event_state=state,
            payload=payload,
            apply=lambda _: None,
        )

    def events(self, run_id: str, *, after_sequence: int = 0, limit: int = 500) -> list[dict[str, Any]]:
        normalized = _run_id(run_id)
        after = max(0, int(after_sequence))
        bounded_limit = max(1, min(int(limit), 5_000))
        with self._lock, file_lock(self._lock_path(normalized)):
            path = self._events_path(normalized)
            if not path.is_file():
                return []
            rows: list[dict[str, Any]] = []
            for line in path.read_text(encoding="utf-8").splitlines():
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict) and int(item.get("sequence", 0)) > after:
                    rows.append(item)
                    if len(rows) >= bounded_limit:
                        break
            return rows

    def wait_for_events(self, run_id: str, after_sequence: int, timeout_seconds: float) -> None:
        condition = self._condition(_run_id(run_id))
        with condition:
            condition.wait(timeout=max(0.0, min(timeout_seconds, 20.0)))

    async def wait_for_events_async(self, run_id: str, after_sequence: int, timeout_seconds: float) -> None:
        """Wait for a Run event without occupying FastAPI's worker thread pool."""

        normalized = _run_id(run_id)
        timeout = max(0.0, min(float(timeout_seconds), 20.0))
        if timeout <= 0 or self.events(normalized, after_sequence=after_sequence, limit=1):
            return
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        waiter = (loop, event)
        with self._lock:
            self._async_waiters.setdefault(normalized, set()).add(waiter)
            # Register before rechecking so an append cannot slip between the
            # first read and the waiter becoming visible to the writer thread.
            if self.events(normalized, after_sequence=after_sequence, limit=1):
                event.set()
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            pass
        finally:
            with self._lock:
                waiters = self._async_waiters.get(normalized)
                if waiters is not None:
                    waiters.discard(waiter)
                    if not waiters:
                        self._async_waiters.pop(normalized, None)

    def execution_trace_path(self, run_id: str) -> Path:
        """Return a verified on-disk execution trace for download/export."""

        normalized = _run_id(run_id)
        with self._lock, file_lock(self._lock_path(normalized)):
            self._read_locked(normalized)
            path = self._execution_trace_path(normalized)
            if not path.is_file():
                raise AgentRuntimeError("AGENT_EXECUTION_TRACE_NOT_FOUND", "该 Agent Run 尚未产生可导出的执行记录。", status_code=404)
            return path


class AgentRuntime:
    """A persistent ReAct runner whose browser is only a UI action executor."""

    def __init__(
        self,
        *,
        store: AgentRunStore,
        leases: CredentialLeaseStore,
        model_gateway: ModelGateway,
        action_router: ActionRouter,
        system_prompt: Callable[[dict[str, Any]], str],
    ) -> None:
        self.store = store
        self.leases = leases
        self.model_gateway = model_gateway
        self.action_router = action_router
        self.system_prompt = system_prompt
        self._threads: dict[str, threading.Thread] = {}
        self._cancel_events: dict[str, threading.Event] = {}
        self._external_action_locks: dict[str, threading.Lock] = {}
        self._external_ui_waiters: dict[tuple[str, str], threading.Event] = {}
        self._external_ui_results: dict[tuple[str, str], dict[str, Any]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _action_key(action_id: str, action_input: dict[str, Any]) -> str:
        """Stable idempotency identity for one typed action.

        A restarted Runtime must never run a previously successful deploy or
        remote command merely because the model repeated the same JSON.  A
        model that intentionally needs a fresh attempt can provide a meaningful
        ``retry_nonce`` in the registered action input, which naturally makes a
        new key and remains auditable.
        """

        return sha256_json({"action_id": action_id, "input": _safe_json(action_input)})

    @staticmethod
    def _capture_action_key(run: dict[str, Any], action_id: str, action_input: dict[str, Any]) -> str | None:
        if action_id.startswith("device.") or action_id == "scenario_capture.execute":
            target, required = capture_target(_record(run.get("context")))
            if target or required:
                # Tool arguments may omit the route supplied by the page. Keep
                # idempotency within that route instead of reusing another phone.
                return sha256_json({"action_id": action_id, "input": _safe_json(action_input),
                                    "capture_target": target, "selection_required": required})
        return None

    @classmethod
    def _run_action_key(cls, run: dict[str, Any], action_id: str, action_input: dict[str, Any]) -> str:
        capture_key = cls._capture_action_key(run, action_id, action_input)
        if capture_key is not None:
            return capture_key
        if action_id.startswith("application_flow."):
            context = _record(run.get("context"))
            focused_task = _record(context.get("focused_task"))
            flow_analysis_id = _text(
                focused_task.get("application_flow_analysis_id")
                or context.get("application_flow_analysis_id"),
                160,
            )
            if flow_analysis_id:
                return sha256_json({
                    "action_id": action_id,
                    "input": _safe_json(action_input),
                    "application_flow_analysis_id": flow_analysis_id,
                })
        if action_id not in {"reasoning.run_hardware_semantic_route", "reasoning.run_strategy"}:
            return cls._action_key(action_id, action_input)
        context = _record(run.get("context"))
        entry = _record(_record(run.get("checkpoint")).get("entry_triage"))
        route_artifact_id = ""
        if action_id == "reasoning.run_strategy":
            completed = _record(_record(run.get("checkpoint")).get("completed_action_keys"))
            for observation in reversed(list(completed.values())):
                item = _record(observation)
                if item.get("action_id") != "reasoning.run_hardware_semantic_route" or item.get("status") != "succeeded":
                    continue
                route_artifact_id = _text(_record(item.get("result")).get("routing_result_artifact_id"), 160)
                if route_artifact_id:
                    break
        return sha256_json({
            "action_id": action_id,
            "input": _safe_json(action_input),
            "analysis_record_id": _text(context.get("analysis_record_id"), 160),
            "request_input_sha256": _text(entry.get("input_sha256"), 80),
            "route_artifact_id": route_artifact_id or None,
        })

    @staticmethod
    def _completed_action(run: dict[str, Any], action_key: str) -> dict[str, Any] | None:
        checkpoint = _record(run.get("checkpoint"))
        completed = _record(checkpoint.get("completed_action_keys"))
        value = completed.get(action_key)
        return _record(value) if isinstance(value, dict) else None

    def _can_reuse_completed_action(
        self,
        run: dict[str, Any],
        action_id: str,
        action_input: dict[str, Any],
        prior_result: dict[str, Any],
    ) -> bool:
        checker = getattr(self.action_router, "can_reuse", None)
        if checker is None:
            return True
        try:
            return bool(checker(action_id, action_input, prior_result, run))
        except PlatformError:
            return False

    def _mark_action_completed(
        self,
        run_id: str,
        *,
        action_key: str,
        action_result: dict[str, Any],
        event_state: str = "running",
    ) -> None:
        """Persist one observed action result before the next model turn.

        Failed calls are facts too.  Recording them prevents a resumed model
        from replaying the exact same non-idempotent device command forever;
        it must deliberately alter a parameter (for example ``retry_nonce``)
        or explain why a new attempt is justified.
        """

        def apply(run: dict[str, Any]) -> None:
            checkpoint = _record(run.get("checkpoint"))
            completed = _record(checkpoint.get("completed_action_keys"))
            completed[action_key] = _safe_json(action_result)
            # Retain the most recent successful facts while keeping checkpoint
            # files bounded.  The append-only trace remains the full record.
            if len(completed) > 256:
                for key in list(completed)[: len(completed) - 256]:
                    completed.pop(key, None)
            checkpoint["completed_action_keys"] = completed
            checkpoint["saved_at"] = utc_now()
            run["checkpoint"] = checkpoint

        self.store.mutate(
            run_id,
            event_type="tool.idempotency_recorded",
            event_state=event_state,
            payload={"action_key": action_key, "action_id": action_result.get("action_id"), "status": action_result.get("status")},
            apply=apply,
        )

    def recover_after_restart(self) -> int:
        """Convert orphaned in-flight runs into explicit credential waits.

        BYOK leases are intentionally memory-only, therefore a process restart
        cannot safely resume provider work on its own.  Pending browser actions
        remain pending; all other non-terminal work receives a precise event so
        the browser can attach a fresh lease and continue from its checkpoint.
        """

        recovered = 0
        for directory in self.store.root.glob("run_*"):
            if not directory.is_dir():
                continue
            run_id = directory.name
            try:
                run = self.store.read(run_id)
            except AgentRuntimeError:
                continue
            state = str(run.get("state") or "")
            if state in TERMINAL_STATES or state == "waiting_for_ui":
                continue
            self.store.transition(
                run_id,
                "waiting_for_model_credential",
                "后端运行时已恢复，正在等待浏览器自动重新连接模型并从检查点继续。",
                payload={"recovery_reason": "runtime_process_restart", "previous_state": state},
            )
            recovered += 1
        return recovered

    def create(
        self,
        *,
        project_id: str,
        goal: str,
        initial_input: str,
        mode: str,
        context: dict[str, Any],
        budget: dict[str, int],
        action_catalog_version: str,
        skill_versions: list[dict[str, str]],
    ) -> dict[str, Any]:
        return self.store.create(
            project_id=project_id,
            goal=goal,
            initial_input=initial_input,
            mode=mode,
            context=context,
            budget=budget,
            action_catalog_version=action_catalog_version,
            skill_versions=skill_versions,
        )

    def prepare_task(
        self,
        run_id: str,
        *,
        goal: str,
        mode: str,
        context: dict[str, Any],
    ) -> dict[str, Any]:
        """Switch the active task contract without replacing conversation history."""

        normalized_mode = _text(mode, 80) or "page_agent"
        normalized_goal = _text(goal, 16 * 1024)
        normalized_context = _safe_json(context)

        def apply(run: dict[str, Any]) -> None:
            checkpoint = _record(run.get("checkpoint"))
            run["mode"] = normalized_mode
            run["goal"] = normalized_goal
            run["context"] = normalized_context
            checkpoint["unresolved_goal"] = normalized_goal
            checkpoint["task_context_pending"] = True
            checkpoint["saved_at"] = utc_now()
            run["checkpoint"] = checkpoint

        run = self.store.mutate(
            run_id,
            event_type="run.task_context_updated",
            event_state=str(self.store.read(run_id).get("state") or "created"),
            payload={
                "mode": normalized_mode,
                "goal_sha256": sha256_json({"goal": normalized_goal}),
                "context_sha256": sha256_json({"context": normalized_context}),
            },
            apply=apply,
        )
        return self.store.public(run)

    def attach_credential(self, run_id: str, *, base_url: str, model: str, api_key: str, ttl_seconds: int | None = None) -> dict[str, Any]:
        lease = self.leases.issue(run_id, base_url=base_url, model=model, api_key=api_key, ttl_seconds=ttl_seconds)

        def apply(run: dict[str, Any]) -> None:
            run["model_lease"] = {"status": "active", **lease.public()}
            if run.get("state") == "waiting_for_model_credential":
                run["state"] = "created"
                run["state_detail"] = "已收到新的内存模型凭证，可从检查点启动。"

        run = self.store.mutate(
            run_id,
            event_type="model.credential_attached",
            event_state="created",
            payload={"lease": lease.public()},
            apply=apply,
        )
        return self.store.public(run)

    def start(self, run_id: str) -> dict[str, Any]:
        run = self.store.read(run_id)
        if run.get("state") in TERMINAL_STATES:
            raise AgentRuntimeError("AGENT_RUN_TERMINAL", "此 Agent Run 已结束，不能直接启动。", status_code=409)
        if self.leases.get_for_run(run_id) is None:
            run = self.store.transition(run_id, "waiting_for_model_credential", "等待浏览器重新提供模型凭证。")
            return self.store.public(run)
        self._start_thread(run_id)
        return self.store.public(self.store.read(run_id))

    def resume(self, run_id: str, *, extra_turns: int = 0, budget_overrides: dict[str, int] | None = None) -> dict[str, Any]:
        def apply(run: dict[str, Any]) -> None:
            budget = _record(run.get("budget"))
            if extra_turns:
                budget["max_turns"] = min(100, max(1, int(budget.get("max_turns", 10)) + int(extra_turns)))
            overrides = _record(budget_overrides)
            if "max_actions_per_turn" in overrides:
                budget["max_actions_per_turn"] = max(1, min(int(overrides["max_actions_per_turn"]), 12))
            if "max_output_tokens" in overrides:
                budget["max_output_tokens"] = max(256, min(int(overrides["max_output_tokens"]), 64 * 1024))
            if "context_window_tokens" in overrides:
                budget["context_window_tokens"] = max(8 * 1024, min(int(overrides["context_window_tokens"]), 1024 * 1024))
            run["budget"] = budget
            run["cancel_requested"] = False

        run = self.store.mutate(
            run_id,
            event_type="run.resume_requested",
            event_state="created",
            payload={"extra_turns": extra_turns, "budget_overrides": _safe_json(_record(budget_overrides))},
            apply=apply,
        )
        if self.leases.get_for_run(run_id) is None:
            run = self.store.transition(run_id, "waiting_for_model_credential", "检查点已保留，等待浏览器补充模型凭证。")
            return self.store.public(run)
        self._start_thread(run_id)
        return self.store.public(self.store.read(run_id))

    def submit_message(self, run_id: str, message: str) -> dict[str, Any]:
        message = _text(message, MAX_MESSAGE_CHARS)
        if not message:
            raise AgentRuntimeError("AGENT_MESSAGE_INVALID", "用户消息不能为空。", status_code=422)

        def apply(run: dict[str, Any]) -> None:
            checkpoint = _record(run.get("checkpoint"))
            # A new chat message must not erase the observation that was about
            # to drive the next turn.  This is especially important after a
            # 429, a page refresh, or a user typing "继续工作": replacing
            # ``next_input`` here used to make the Runtime start over from a
            # vague user request instead of the persisted checkpoint.
            prior_observation = _text(checkpoint.get("next_input"), 64 * 1024)
            checkpoint["next_input"] = (
                "上一检查点的未完成任务与真实观察如下（不能忽略、不能重复已成功 action）：\n"
                f"{prior_observation}\n\n"
                f"最新用户输入：{message}\n\n"
                "请结合最新意图决定继续、修订或停止；已成功 action 不得机械重放。"
            )[:MAX_MESSAGE_CHARS]
            checkpoint["saved_at"] = utc_now()
            run["checkpoint"] = checkpoint
            run["pending_ui_actions"] = []
            if run.get("state") not in TERMINAL_STATES:
                run["state"] = "created"
                run["state_detail"] = "已收到用户消息，等待从检查点继续。"

        run = self.store.mutate(
            run_id,
            event_type="run.user_message",
            event_state="created",
            payload={"message": message},
            apply=apply,
        )
        if self.leases.get_for_run(run_id) is not None and run.get("state") not in TERMINAL_STATES:
            self._start_thread(run_id)
        return self.store.public(self.store.read(run_id))

    def submit_ui_result(self, run_id: str, request_id: str, result: dict[str, Any], *, error: dict[str, Any] | None = None) -> dict[str, Any]:
        request_id = _text(request_id, 160)
        if not request_id:
            raise AgentRuntimeError("AGENT_UI_RESULT_INVALID", "ui action request_id 不能为空。", status_code=422)

        waiter_key = (run_id, request_id)
        with self._lock:
            external_waiter = self._external_ui_waiters.get(waiter_key)
        if external_waiter is not None:
            observation_holder: dict[str, Any] = {}

            def apply_external(run: dict[str, Any]) -> None:
                pending = run.get("pending_ui_actions") if isinstance(run.get("pending_ui_actions"), list) else []
                matching = [item for item in pending if isinstance(item, dict) and item.get("request_id") == request_id]
                if not matching:
                    raise AgentRuntimeError("AGENT_UI_ACTION_NOT_PENDING", "此 ui action 已完成、过期或不属于当前 Run。", status_code=409)
                pending_action = matching[0]
                action_id = _text(pending_action.get("action_id"), 160)
                action_input = _record(pending_action.get("input"))
                action_key = _text(pending_action.get("action_key"), 128) or self._action_key(action_id, action_input)
                observation = {
                    "action_id": action_id,
                    "request_id": request_id,
                    "action_key": action_key,
                    "input": _action_input_fact(action_id, action_input),
                    "result": _safe_json(result),
                    "error": _safe_json(error or {}),
                    "status": "failed" if error else "succeeded",
                }
                checkpoint = _record(run.get("checkpoint"))
                completed = _record(checkpoint.get("completed_action_keys"))
                completed[action_key] = _safe_json(observation)
                if len(completed) > 256:
                    for key in list(completed)[: len(completed) - 256]:
                        completed.pop(key, None)
                checkpoint["completed_action_keys"] = completed
                checkpoint["last_action_results"] = [observation]
                checkpoint["saved_at"] = utc_now()
                run["checkpoint"] = checkpoint
                run["pending_ui_actions"] = [item for item in pending if not (isinstance(item, dict) and item.get("request_id") == request_id)]
                run["state"] = "running"
                run["state_detail"] = f"浏览器已返回 {action_id} 的真实结果。"
                observation_holder.update(observation)

            run = self.store.mutate(
                run_id,
                event_type="ui.action_result",
                event_state="running",
                payload={"request_id": request_id, "result": result, "error": error or {}, "engine_waiting": True},
                apply=apply_external,
            )
            with self._lock:
                self._external_ui_results[waiter_key] = observation_holder
                external_waiter.set()
            return self.store.public(run)

        def apply(run: dict[str, Any]) -> None:
            pending = run.get("pending_ui_actions") if isinstance(run.get("pending_ui_actions"), list) else []
            matching = [item for item in pending if isinstance(item, dict) and item.get("request_id") == request_id]
            if not matching:
                raise AgentRuntimeError("AGENT_UI_ACTION_NOT_PENDING", "此 ui action 已完成、过期或不属于当前 Run。", status_code=409)
            checkpoint = _record(run.get("checkpoint"))
            observation = {
                "action_id": matching[0].get("action_id"),
                "request_id": request_id,
                "action_key": matching[0].get("action_key"),
                "input": _action_input_fact(_text(matching[0].get("action_id"), 160), _record(matching[0].get("input"))),
                "result": _safe_json(result),
                "error": _safe_json(error or {}),
                "status": "failed" if error else "succeeded",
            }
            checkpoint["next_input"] = self._observation_message([observation], int(run.get("current_turn", 0)))
            checkpoint["last_action_results"] = [observation]
            checkpoint["saved_at"] = utc_now()
            run["checkpoint"] = checkpoint
            run["pending_ui_actions"] = [item for item in pending if not (isinstance(item, dict) and item.get("request_id") == request_id)]
            completed = _record(checkpoint.get("completed_action_keys"))
            action_id = _text(matching[0].get("action_id"), 160)
            action_input = _record(matching[0].get("input"))
            action_key = _text(matching[0].get("action_key"), 128) or self._action_key(action_id, action_input)
            if action_id:
                completed[action_key] = _safe_json(observation)
                if len(completed) > 256:
                    for key in list(completed)[: len(completed) - 256]:
                        completed.pop(key, None)
                checkpoint["completed_action_keys"] = completed
            run["state"] = "created"
            run["state_detail"] = "已收到浏览器 ui action 的真实结果，准备继续。"

        run = self.store.mutate(
            run_id,
            event_type="ui.action_result",
            event_state="created",
            payload={"request_id": request_id, "result": result, "error": error or {}},
            apply=apply,
        )
        if self.leases.get_for_run(run_id) is not None:
            self._start_thread(run_id)
        return self.store.public(self.store.read(run_id))

    def cancel(self, run_id: str) -> dict[str, Any]:
        with self._lock:
            cancel_event = self._cancel_events.setdefault(run_id, threading.Event())
            cancel_event.set()
        run = self.store.mutate(
            run_id,
            event_type="run.cancel_requested",
            event_state="cancelled",
            payload={},
            apply=lambda value: value.update({"cancel_requested": True, "state": "cancelled", "state_detail": "用户已请求取消 Agent Run。", "pending_ui_actions": []}),
        )
        try:
            self.action_router.cancel(run)
        finally:
            self.leases.revoke(run_id)
        return self.store.public(run)

    def execute_external_action(self, run_id: str, action_id: str, action_input: dict[str, Any]) -> dict[str, Any]:
        """Execute one engine-requested typed action with durable idempotency.

        Kimi reaches this method through the per-Run MCP bridge.  The engine
        never receives a host shell: validation and executor ownership remain
        exactly the same as in the legacy Runtime.
        """

        action_id = _text(action_id, 160)
        action_input = _record(action_input)
        if not action_id:
            raise AgentRuntimeError("ACTION_INPUT_INVALID", "Action ID 不能为空。", status_code=422)
        run = self.store.read(run_id)
        action_key = self._run_action_key(run, action_id, action_input)
        capture_context = _safe_json(run.get("context")) if self._capture_action_key(run, action_id, action_input) else None
        with self._lock:
            action_lock = self._external_action_locks.setdefault(action_key, threading.Lock())
        with action_lock:
            run = self.store.read(run_id)
            if run.get("state") in TERMINAL_STATES or run.get("cancel_requested"):
                raise AgentRuntimeError("AGENT_RUN_TERMINAL", "Agent Run 已结束或已取消，不能继续执行工具。", status_code=409)
            if capture_context is not None:
                run = {**run, "context": capture_context}
            owner = self.action_router.owner(action_id)
            if owner == "ui" and str(run.get("mode") or "") == "device_validation" and action_id != "workspace.read_file":
                raise AgentRuntimeError(
                    "ACTION_NOT_ALLOWED_IN_RUN_MODE",
                    "真机验证 Run 只允许通过浏览器读取明确授权的代码文件，不执行其他页面工作流动作。",
                    status_code=422,
                    details={"action_id": action_id, "mode": run.get("mode")},
                )
            prior = self._completed_action(run, action_key)
            # Successful actions are durable and idempotent. A failed action
            # is an observation, not a completed result: transient failures
            # such as a lost browser callback must remain retryable in the
            # same Agent Session.
            if (
                prior is not None
                and prior.get("status") != "failed"
                and self._can_reuse_completed_action(run, action_id, action_input, prior)
            ):
                reused = {
                    "action_id": action_id,
                    "status": str(prior.get("status") or "succeeded"),
                    "input": prior.get("input", _action_input_fact(action_id, action_input)),
                    "result": prior.get("result", prior),
                    "action_key": action_key,
                    "reused_from_checkpoint": True,
                }
                self.store.append_event(run_id, "tool.result_reused", "running", reused)
                return reused
            if prior is not None and prior.get("status") != "failed":
                self.store.append_event(
                    run_id,
                    "tool.result_stale",
                    "running",
                    {"action_id": action_id, "action_key": action_key, "reason": "time_bounded_result_expired"},
                )
            request_id = _new_id("act")
            if owner == "ui":
                self.action_router.validate(action_id, action_input, run)
                pending = {
                    "request_id": request_id,
                    "action_id": action_id,
                    "input": action_input,
                    "action_key": action_key,
                    "requested_at": utc_now(),
                    "turn": int(run.get("current_turn", 0)) or 1,
                    "engine_waiting": True,
                }
                waiter_key = (run_id, request_id)
                waiter = threading.Event()
                with self._lock:
                    self._external_ui_waiters[waiter_key] = waiter

                def apply_waiting(value: dict[str, Any]) -> None:
                    existing = value.get("pending_ui_actions") if isinstance(value.get("pending_ui_actions"), list) else []
                    value["pending_ui_actions"] = [*existing, pending]
                    value["state"] = "waiting_for_ui"
                    value["state_detail"] = f"等待浏览器执行 {action_id}。"

                self.store.mutate(
                    run_id,
                    event_type="ui.action_requested",
                    event_state="waiting_for_ui",
                    payload=pending,
                    apply=apply_waiting,
                )
                deadline = time.monotonic() + EXTERNAL_UI_ACTION_TIMEOUT_SECONDS
                observation: dict[str, Any] | None = None
                try:
                    while time.monotonic() < deadline:
                        if waiter.wait(0.5):
                            with self._lock:
                                observation = self._external_ui_results.pop(waiter_key, None)
                            break
                        current = self.store.read(run_id)
                        if current.get("cancel_requested") or current.get("state") in TERMINAL_STATES:
                            raise AgentRuntimeError("AGENT_RUN_TERMINAL", "Agent Run 在等待页面操作时已取消或结束。", status_code=409)
                    if observation is None:
                        observation = {
                            "action_id": action_id,
                            "request_id": request_id,
                            "status": "failed",
                            "action_key": action_key,
                            "input": _action_input_fact(action_id, action_input),
                            "error": {"code": "UI_ACTION_TIMEOUT", "message": "浏览器未在 15 分钟内返回页面操作结果。"},
                        }

                        def apply_timeout(value: dict[str, Any]) -> None:
                            existing = value.get("pending_ui_actions") if isinstance(value.get("pending_ui_actions"), list) else []
                            value["pending_ui_actions"] = [item for item in existing if not (isinstance(item, dict) and item.get("request_id") == request_id)]
                            value["state"] = "running"
                            value["state_detail"] = f"{action_id} 等待页面结果超时。"

                        self.store.mutate(run_id, event_type="ui.action_timeout", event_state="running", payload=observation, apply=apply_timeout)
                        self._mark_action_completed(run_id, action_key=action_key, action_result=observation)
                finally:
                    with self._lock:
                        self._external_ui_waiters.pop(waiter_key, None)
                        self._external_ui_results.pop(waiter_key, None)
                self.store.append_event(run_id, "tool.result", "running", observation)
                return observation
            waiting_state = "waiting_for_device" if owner == "device" else "waiting_for_tool"
            self.store.transition(
                run_id,
                waiting_state,
                f"Kimi Engine 正在执行 {action_id}。",
                payload={"action_id": action_id, "request_id": request_id, "action_key": action_key},
            )
            self.store.append_event(
                run_id,
                "tool.call",
                waiting_state,
                {"action_id": action_id, "request_id": request_id, "input": action_input, "action_key": action_key, "owner": owner, "engine": "kimi-server"},
            )
            try:
                current_run = self.store.read(run_id)
                if capture_context is not None:
                    if current_run.get("state") in TERMINAL_STATES or current_run.get("cancel_requested"):
                        raise AgentRuntimeError("AGENT_RUN_TERMINAL", "Agent Run 已结束或已取消，不能继续执行工具。", status_code=409)
                    # Waiting for another action must not retarget this request
                    # while retaining its original device-scoped cache key.
                    current_run = {**current_run, "context": capture_context}
                self.action_router.validate(action_id, action_input, current_run)
                result = self.action_router.execute(action_id, action_input, current_run)
                reported = _reported_action_failure(result)
                if action_id == SKILL_BOOTSTRAP_ACK_ACTION and not reported:
                    acknowledgement = _record(result.get("acknowledgement"))
                    acknowledged_state = acknowledge_skill_bootstrap(
                        current_run.get("skill_bootstrap"),
                        acknowledgement,
                    )

                    def apply_ack(value: dict[str, Any]) -> None:
                        current = _record(value.get("skill_bootstrap"))
                        if current.get("bootstrap_id") != acknowledged_state.get("bootstrap_id"):
                            raise PlatformError("SKILL_BOOTSTRAP_STALE", "Skill Bootstrap 清单已经变化。", status_code=409)
                        value["skill_bootstrap"] = acknowledged_state

                    acknowledged_run = self.store.mutate(
                        run_id,
                        event_type="skill.bootstrap_acknowledged",
                        event_state=waiting_state,
                        payload={"skill_bootstrap": public_skill_bootstrap(acknowledged_state)},
                        apply=apply_ack,
                    )
                    result = {
                        **result,
                        "skill_bootstrap": public_skill_bootstrap(acknowledged_run.get("skill_bootstrap")),
                    }
                observation = {
                    "action_id": action_id,
                    "request_id": request_id,
                    "status": "failed" if reported else "succeeded",
                    "input": _action_input_fact(action_id, action_input),
                    "result": _safe_json(result),
                    "action_key": action_key,
                }
                if reported:
                    observation["error"] = reported
            except PlatformError as exc:
                observation = {
                    "action_id": action_id,
                    "request_id": request_id,
                    "status": "failed",
                    "action_key": action_key,
                    "input": _action_input_fact(action_id, action_input),
                    "error": {"code": exc.code, "message": exc.message, "details": exc.details},
                }
            except Exception as exc:  # pragma: no cover - executor isolation boundary
                observation = {
                    "action_id": action_id,
                    "request_id": request_id,
                    "status": "failed",
                    "action_key": action_key,
                    "input": _action_input_fact(action_id, action_input),
                    "error": {"code": "ACTION_EXECUTOR_FAILED", "message": str(exc)[:1200]},
                }
            self.store.append_event(run_id, "tool.result", waiting_state, observation)
            self._mark_action_completed(run_id, action_key=action_key, action_result=observation, event_state=waiting_state)
            latest = self.store.read(run_id)
            if latest.get("state") not in TERMINAL_STATES and not latest.get("cancel_requested"):
                self.store.transition(run_id, "running", f"{action_id} 已返回真实执行结果。", payload={"action_id": action_id, "request_id": request_id, "status": observation["status"]})
            return observation

    def _start_thread(self, run_id: str) -> None:
        with self._lock:
            existing = self._threads.get(run_id)
            if existing and existing.is_alive():
                return
            cancel_event = self._cancel_events.setdefault(run_id, threading.Event())
            cancel_event.clear()
            thread = threading.Thread(target=self._run_loop, args=(run_id, cancel_event), name=f"agent-run-{run_id[-8:]}", daemon=True)
            self._threads[run_id] = thread
            thread.start()

    def _system_messages(self, run: dict[str, Any]) -> list[dict[str, str]]:
        system = self.system_prompt(run)
        context = _record(run.get("context"))
        context_text = json.dumps(_safe_json(context), ensure_ascii=False, separators=(",", ":"))
        return [
            {"role": "system", "content": system},
            {"role": "system", "content": f"当前任务不可变上下文：{context_text[:MAX_MESSAGE_CHARS]}"},
        ]

    def _compact_memory(self, run_id: str, run: dict[str, Any], memory: list[dict[str, str]]) -> list[dict[str, str]]:
        budget = _record(run.get("budget"))
        context_limit = max(8 * 1024, int(budget.get("context_window_tokens", 128 * 1024)))
        # Conservative Chinese/code approximation: it is only a trigger.  The
        # actual provider remains the authority for exact token accounting.
        estimated_tokens = sum(max(1, len(_text(item.get("content"))) // 3) for item in memory)
        if len(memory) <= MAX_MEMORY_MESSAGES and estimated_tokens < int(context_limit * 0.70):
            return memory
        self.store.transition(run_id, "compacting", "上下文接近消息上限，正在生成保留事实的摘要。")
        retained_count = 12 if len(memory) > 12 else min(6, len(memory))
        compacted = memory[:-retained_count] if retained_count else memory
        # A single pasted source file can be larger than a whole small model
        # window.  Keep its tail as a reference-sized excerpt; the canonical
        # source remains in an artifact/context reference and can be requested
        # through a registered action instead of silently overflowing again.
        retained = [
            {"role": _text(item.get("role"), 20), "content": _text(item.get("content"), 12_000)}
            for item in (memory[-retained_count:] if retained_count else [])
        ]
        digest_parts: list[str] = []
        for item in compacted:
            content = _text(item.get("content"), 1200)
            if content:
                digest_parts.append(f"{item.get('role', 'unknown')}：{content}")
        summary = "\n".join(digest_parts)[-16_000:]
        output = [{"role": "system", "content": f"已压缩的历史事实（不可替代真实 action 结果）：\n{summary}"}, *retained]
        self.store.append_event(
            run_id,
            "run.compacted",
            "compacting",
            {
                "compacted_messages": len(compacted),
                "retained_messages": len(retained),
                "estimated_tokens_before": estimated_tokens,
                "context_window_tokens": context_limit,
            },
        )
        self.store.transition(run_id, "running", "上下文压缩完成，继续当前任务。")
        return output

    @staticmethod
    def _observation_message(results: list[dict[str, Any]], turn: int) -> str:
        compact = json.dumps(_safe_json(results), ensure_ascii=False, separators=(",", ":"))
        return (
            f"第 {turn} 轮 action 已返回以下真实观察：{compact[:MAX_MESSAGE_CHARS]}\n"
            "这些结果是当前唯一新增事实。请依据结果继续，不得声称尚未返回的 action 已执行；"
            "若下一步依赖新 artifact、remote path 或设备 lease，必须等到本轮观察中真实出现后再调用。"
        )

    def _set_checkpoint(
        self,
        run_id: str,
        *,
        memory: list[dict[str, str]],
        next_input: str,
        action_results: list[dict[str, Any]],
        invalid_model_output_count: int | None = None,
        current_turn: int | None = None,
    ) -> dict[str, Any]:
        def apply(run: dict[str, Any]) -> None:
            checkpoint = _record(run.get("checkpoint"))
            checkpoint["memory"] = memory[-MAX_MEMORY_MESSAGES:]
            checkpoint["next_input"] = _text(next_input, MAX_MESSAGE_CHARS)
            checkpoint["last_action_results"] = _safe_json(action_results)
            checkpoint["saved_at"] = utc_now()
            if invalid_model_output_count is not None:
                checkpoint["invalid_model_output_count"] = invalid_model_output_count
            run["checkpoint"] = checkpoint
            if current_turn is not None:
                run["current_turn"] = current_turn

        return self.store.mutate(
            run_id,
            event_type="run.checkpoint_saved",
            event_state="running",
            payload={"memory_message_count": len(memory), "action_result_count": len(action_results), "current_turn": current_turn},
            apply=apply,
        )

    def _run_loop(self, run_id: str, cancel_event: threading.Event) -> None:
        try:
            while not cancel_event.is_set():
                run = self.store.read(run_id)
                if run.get("state") in TERMINAL_STATES or run.get("cancel_requested"):
                    return
                lease = self.leases.get_for_run(run_id)
                if lease is None:
                    self.store.transition(run_id, "waiting_for_model_credential", "模型密钥租约不存在或已过期，等待浏览器重新提供。")
                    return
                if run.get("pending_ui_actions"):
                    self.store.transition(run_id, "waiting_for_ui", "等待浏览器执行已注册的 ui.* 动作。")
                    return
                budget = _record(run.get("budget"))
                max_turns = int(budget.get("max_turns", 10))
                turn = int(run.get("current_turn", 0)) + 1
                if turn > max_turns:
                    self.store.transition(run_id, "paused_turn_budget", "已达到用户设置的自主轮次预算；检查点与真实观察已保留。", payload={"max_turns": max_turns})
                    return
                self.store.transition(run_id, "running", f"第 {turn} 轮：模型正在决定下一步。", payload={"turn": turn})
                if cancel_event.is_set():
                    self.store.transition(run_id, "cancelled", "用户已请求取消 Agent Run。")
                    return
                checkpoint = _record(run.get("checkpoint"))
                memory = [
                    {"role": _text(item.get("role"), 20), "content": _text(item.get("content"), MAX_MESSAGE_CHARS)}
                    for item in checkpoint.get("memory", [])
                    if isinstance(item, dict) and _text(item.get("role"), 20) in {"system", "user", "assistant"}
                ]
                memory = self._compact_memory(run_id, run, memory)
                next_input = _text(checkpoint.get("next_input"), MAX_MESSAGE_CHARS)
                messages = [*self._system_messages(run), *memory, {"role": "user", "content": next_input}]
                self.store.append_event(
                    run_id,
                    "model.request",
                    "running",
                    {
                        "turn": turn,
                        "message_count": len(messages),
                        "message_sha256": sha256_json(messages),
                        "message_chars": sum(len(item.get("content", "")) for item in messages),
                        "max_output_tokens": int(budget.get("max_output_tokens", 8192)),
                        "model": lease.model,
                        "base_url": lease.base_url,
                    },
                )

                # Providers may emit one SSE delta per token.  Persisting every
                # fragment would exhaust the append-only event budget during a
                # perfectly healthy long coding turn.  Coalesce small deltas
                # while retaining retry notices immediately and retaining the
                # complete provider output in ``model.response``.
                pending_reasoning: list[str] = []
                pending_content: list[str] = []
                last_reasoning_chars: Any = 0
                last_content_chars: Any = 0
                last_stream_flush = 0.0

                def flush_stream(*, retry: dict[str, Any] | None = None) -> None:
                    nonlocal last_stream_flush
                    if cancel_event.is_set():
                        pending_reasoning.clear()
                        pending_content.clear()
                        return
                    if not pending_reasoning and not pending_content and not retry:
                        return
                    self.store.append_event(
                        run_id,
                        "model.stream",
                        "running",
                        {
                            "turn": turn,
                            "reasoning_delta": "".join(pending_reasoning)[-MAX_STREAM_EVENT_CHARS:],
                            "content_delta": "".join(pending_content)[-MAX_STREAM_EVENT_CHARS:],
                            "reasoning_chars": last_reasoning_chars,
                            "content_chars": last_content_chars,
                            "retry": _safe_json(retry or {}),
                        },
                    )
                    pending_reasoning.clear()
                    pending_content.clear()
                    last_stream_flush = time.monotonic()

                def on_stream(event: dict[str, Any]) -> None:
                    nonlocal last_reasoning_chars, last_content_chars
                    if cancel_event.is_set():
                        return
                    reasoning_delta = _text(event.get("reasoning_delta"), MAX_STREAM_EVENT_CHARS)
                    content_delta = _text(event.get("content_delta"), MAX_STREAM_EVENT_CHARS)
                    if reasoning_delta:
                        pending_reasoning.append(reasoning_delta)
                    if content_delta:
                        pending_content.append(content_delta)
                    last_reasoning_chars = event.get("reasoning_chars", last_reasoning_chars)
                    last_content_chars = event.get("content_chars", last_content_chars)
                    retry = _record(event.get("retry"))
                    buffered_chars = sum(len(item) for item in pending_reasoning) + sum(len(item) for item in pending_content)
                    if retry or buffered_chars >= 768 or time.monotonic() - last_stream_flush >= 0.18:
                        flush_stream(retry=retry or None)

                try:
                    response = self.model_gateway(
                        lease,
                        messages,
                        max_tokens=int(budget.get("max_output_tokens", 8192)),
                        on_stream=on_stream,
                        cancel_event=cancel_event,
                    )
                except ModelGatewayError as exc:
                    if cancel_event.is_set():
                        return
                    flush_stream()
                    state = (
                        "paused_output_limit"
                        if exc.code in {"MODEL_OUTPUT_TRUNCATED", "MODEL_RESPONSE_TOO_LARGE"}
                        else "paused_model_overloaded"
                        if exc.retryable or exc.status_code == 429 or exc.code == "MODEL_OVERLOADED"
                        else "failed"
                    )
                    self.store.transition(
                        run_id,
                        state,
                        exc.message,
                        payload={"code": exc.code, "retryable": exc.retryable, "retry_after_seconds": exc.retry_after_seconds, "details": exc.details},
                    )
                    return
                except Exception as exc:  # pragma: no cover - defensive boundary around provider adapters
                    if cancel_event.is_set():
                        return
                    flush_stream()
                    self.store.transition(run_id, "failed", "模型调用发生未分类错误。", payload={"error": str(exc)[:1200]})
                    return

                if cancel_event.is_set():
                    return
                flush_stream()
                content = _text(response.get("content"), MAX_MESSAGE_CHARS * 2)
                self.store.append_event(
                    run_id,
                    "model.response",
                    "running",
                    {
                        "turn": turn,
                        "content": content,
                        "reasoning_content": _text(response.get("reasoning_content"), MAX_MESSAGE_CHARS),
                        "usage": _safe_json(_record(response.get("usage"))),
                        "finish_reason": response.get("finish_reason"),
                    },
                )
                try:
                    decision = parse_agent_decision(content)
                except DecisionParseError as exc:
                    invalid_count = int(checkpoint.get("invalid_model_output_count", 0)) + 1
                    correction = (
                        f"RUNTIME_MODEL_OUTPUT_INVALID：{exc}。上一轮模型输出未形成合法 action JSON，因此没有执行任何 action，"
                        "也不能视作任务结束。请只返回严格 JSON 对象，包含 reply、actions 和 disposition；"
                        "若需要暂存 C/C++ 源码，JSON action 使用 source_ref，并在 JSON 后提供同 ID 的 <agent-source> 原始源码块。"
                    )
                    updated_memory = [*memory, {"role": "assistant", "content": content}]
                    self._set_checkpoint(
                        run_id,
                        memory=updated_memory,
                        next_input=correction,
                        action_results=[],
                        invalid_model_output_count=invalid_count,
                        current_turn=turn,
                    )
                    self.store.append_event(run_id, "model.contract_invalid", "running", {"turn": turn, "reason": str(exc), "invalid_count": invalid_count})
                    if invalid_count >= 3:
                        self.store.transition(run_id, "failed", "模型连续三次未返回有效 Action JSON；已保留原始输出与恢复检查点。")
                        return
                    continue

                self.store.append_event(
                    run_id,
                    "model.decision",
                    "running",
                    {
                        "turn": turn,
                        "reply": decision.reply,
                        "actions": decision.actions,
                        "disposition": decision.disposition,
                        "parse_status": "recovered" if decision.recovered else "strict",
                    },
                )
                updated_memory = [
                    *memory,
                    {"role": "user", "content": next_input},
                    {"role": "assistant", "content": _decision_memory_content(decision)},
                ]
                requested = decision.actions[: int(budget.get("max_actions_per_turn", 3))]
                if len(decision.actions) > len(requested):
                    self.store.append_event(run_id, "action.budget_limited", "running", {"turn": turn, "requested": len(decision.actions), "allowed": len(requested)})

                action_results: list[dict[str, Any]] = []
                for action in requested:
                    if cancel_event.is_set():
                        return
                    action_id = _text(action.get("action_id"), 160)
                    action_input = _record(action.get("input"))
                    action_key = self._capture_action_key(run, action_id, action_input) or self._action_key(action_id, action_input)
                    prior_result = self._completed_action(run, action_key)
                    if prior_result is not None and (
                        prior_result.get("status") == "failed"
                        or self._can_reuse_completed_action(run, action_id, action_input, prior_result)
                    ):
                        reused = {
                            "action_id": action_id,
                            "request_id": _new_id("act"),
                            "status": str(prior_result.get("status") or "succeeded"),
                            "result": prior_result.get("result", prior_result),
                            "reused_from_checkpoint": True,
                            "action_key": action_key,
                        }
                        action_results.append(reused)
                        self.store.append_event(
                            run_id,
                            "tool.result_reused",
                            "running",
                            {"turn": turn, **reused},
                        )
                        continue
                    if prior_result is not None:
                        self.store.append_event(
                            run_id,
                            "tool.result_stale",
                            "running",
                            {"turn": turn, "action_id": action_id, "action_key": action_key, "reason": "time_bounded_result_expired"},
                        )
                    try:
                        self.action_router.validate(action_id, action_input, run)
                    except PlatformError as exc:
                        action_results.append({"action_id": action_id, "status": "failed", "action_key": action_key, "error": {"code": exc.code, "message": exc.message, "details": exc.details}})
                        self.store.append_event(run_id, "tool.result", "running", action_results[-1])
                        # Validation failures are real observations too.  Do
                        # not let a resumed model repeat the identical invalid
                        # action forever; it must amend its input or choose a
                        # different registered operation.
                        self._mark_action_completed(run_id, action_key=action_key, action_result=action_results[-1])
                        continue
                    owner = self.action_router.owner(action_id)
                    request_id = _new_id("act")
                    if owner == "ui":
                        pending = {
                            "request_id": request_id,
                            "action_id": action_id,
                            "input": action_input,
                            "action_key": action_key,
                            "requested_at": utc_now(),
                            "turn": turn,
                        }

                        def apply_waiting(value: dict[str, Any]) -> None:
                            value["pending_ui_actions"] = [pending]
                            value["state"] = "waiting_for_ui"
                            value["state_detail"] = f"等待浏览器执行 {action_id}。"
                            checkpoint_value = _record(value.get("checkpoint"))
                            checkpoint_value["memory"] = updated_memory[-MAX_MEMORY_MESSAGES:]
                            checkpoint_value["next_input"] = next_input
                            checkpoint_value["saved_at"] = utc_now()
                            value["checkpoint"] = checkpoint_value
                            value["current_turn"] = turn

                        self.store.mutate(
                            run_id,
                            event_type="ui.action_requested",
                            event_state="waiting_for_ui",
                            payload=pending,
                            apply=apply_waiting,
                        )
                        return
                    if owner == "device":
                        self.store.transition(run_id, "waiting_for_device", f"第 {turn} 轮：正在执行 {action_id}。", payload={"action_id": action_id, "request_id": request_id})
                    else:
                        self.store.transition(run_id, "waiting_for_tool", f"第 {turn} 轮：正在执行 {action_id}。", payload={"action_id": action_id, "request_id": request_id})
                    if cancel_event.is_set():
                        # Cancellation may race with the transition above.
                        # Restore the terminal state before any new action is
                        # dispatched so a stopped Run cannot be resurrected.
                        self.store.transition(run_id, "cancelled", "用户已请求取消 Agent Run。")
                        return
                    self.store.append_event(run_id, "tool.call", "running", {"turn": turn, "request_id": request_id, "action_id": action_id, "input": action_input, "owner": owner})
                    try:
                        action_run = self.store.read(run_id)
                        if self._capture_action_key(run, action_id, action_input):
                            action_run = {**action_run, "context": run.get("context")}
                        result = self.action_router.execute(action_id, action_input, action_run)
                        reported_failure = _reported_action_failure(result)
                        action_result = {
                            "action_id": action_id,
                            "request_id": request_id,
                            "status": "failed" if reported_failure else "succeeded",
                            "input": _action_input_fact(action_id, action_input),
                            "result": result,
                            "action_key": action_key,
                            **({"error": reported_failure} if reported_failure else {}),
                        }
                    except PlatformError as exc:
                        action_result = {"action_id": action_id, "request_id": request_id, "status": "failed", "input": _action_input_fact(action_id, action_input), "action_key": action_key, "error": {"code": exc.code, "message": exc.message, "details": exc.details}}
                    except Exception as exc:  # pragma: no cover - executor boundary
                        action_result = {"action_id": action_id, "request_id": request_id, "status": "failed", "input": _action_input_fact(action_id, action_input), "action_key": action_key, "error": {"code": "ACTION_EXECUTION_FAILED", "message": str(exc)[:1200]}}
                    action_results.append(action_result)
                    result_state = "cancelled" if cancel_event.is_set() else "running"
                    self.store.append_event(run_id, "tool.result", result_state, {"turn": turn, **action_result})
                    self._mark_action_completed(run_id, action_key=action_key, action_result=action_result, event_state=result_state)
                    if cancel_event.is_set():
                        return
                    if action_result["status"] != "succeeded" and owner == "device" and _record(action_result.get("error")).get("code") in {"BRIDGE_OFFLINE", "BRIDGE_JOB_TIMEOUT", "BRIDGE_RECONCILIATION_REQUIRED"}:
                        observation = self._observation_message(action_results, turn)
                        self._set_checkpoint(run_id, memory=updated_memory, next_input=observation, action_results=action_results, current_turn=turn)
                        self.store.transition(
                            run_id,
                            "paused_bridge_offline",
                            "Device Bridge 暂时离线或设备 job 状态未知；真实观察已保存，重连对账后可继续。",
                            payload={"turn": turn, "action_id": action_id, "error": action_result["error"]},
                        )
                        return
                    run = self.store.read(run_id)

                if action_results:
                    observation = self._observation_message(action_results, turn)
                    self._set_checkpoint(run_id, memory=updated_memory, next_input=observation, action_results=action_results, current_turn=turn)
                    continue

                if decision.disposition == "continue":
                    correction = (
                        "RUNTIME_CONTINUE_WITHOUT_ACTION：你声明任务需要继续，但本轮没有可执行 action。"
                        "请调用当前已具备完整参数的下一项 action；若缺少事实，说明需要的具体用户输入并使用 wait_for_user。"
                    )
                    self._set_checkpoint(run_id, memory=updated_memory, next_input=correction, action_results=[], current_turn=turn)
                    self.store.append_event(run_id, "run.continuation_without_action", "running", {"turn": turn})
                    continue
                if decision.disposition == "wait_for_user":
                    waiting_input = (
                        f"第 {turn} 轮模型请求用户补充：{decision.reply}\n\n"
                        f"进入等待前的任务与真实观察：{next_input}\n\n"
                        "收到用户消息后，请先回答或利用补充信息继续；不要遗失这项待补充问题，也不要重放已成功 action。"
                    )
                    self._set_checkpoint(run_id, memory=updated_memory, next_input=waiting_input, action_results=[], current_turn=turn)
                    self.store.transition(run_id, "waiting_for_ui", decision.reply, payload={"waiting_reason": "model_requested_user_input"})
                    return
                # A device validation cannot be declared complete before the
                # runtime has at least one real device execution observation.
                if run.get("mode") == "device_validation":
                    current_checkpoint = _record(self.store.read(run_id).get("checkpoint"))
                    observations = [item for item in _record(current_checkpoint.get("completed_action_keys")).values() if isinstance(item, dict)]
                    ran_device = any(str(item.get("action_id")) == "device.execute_shell" for item in observations)
                    terminal_environment_blocker = any(
                        str(item.get("status")) == "failed"
                        and _action_error_code(item) in DEVICE_VALIDATION_TERMINAL_BLOCKER_CODES
                        for item in observations
                    )
                    if not ran_device and not terminal_environment_blocker:
                        correction = (
                            "RUNTIME_DEVICE_VALIDATION_INCOMPLETE：当前没有真实的 device.execute_shell 结果，"
                            "不能把真机验证标记为完成。请依据现有 artifact 继续构建、部署或执行；"
                            "普通源码编译或链接错误属于可修订实验，不是环境终态；请读取诊断后修改源码。"
                            "只有固定工具链、固定运行时、HDC、设备连接或 Bridge 的明确基础设施错误才可作为阻断结论。"
                        )
                        self._set_checkpoint(run_id, memory=updated_memory, next_input=correction, action_results=[], current_turn=turn)
                        self.store.append_event(run_id, "run.incomplete_device_validation", "running", {"turn": turn})
                        continue
                self._set_checkpoint(run_id, memory=updated_memory, next_input=next_input, action_results=[], current_turn=turn)
                self.store.transition(run_id, "completed", decision.reply, payload={"turn": turn, "reply": decision.reply})
                return
        finally:
            with self._lock:
                self._threads.pop(run_id, None)
