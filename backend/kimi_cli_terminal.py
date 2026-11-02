"""Persistent PTY sessions for browser-hosted coding-agent CLIs.

The legacy module name remains for API compatibility.  A session receives one
validated argv launch specification from an ``AgentCliAdapter``; the browser
never receives an endpoint that can spawn an arbitrary login shell.
"""

from __future__ import annotations

import asyncio
import base64
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timezone
import errno
import fcntl
import json
import os
from pathlib import Path
import secrets
import signal
import struct
import subprocess
import termios
import threading
import time
from typing import Any, Callable, Protocol


MAX_TERMINAL_BUFFER_BYTES = 8 * 1024 * 1024
MAX_TERMINAL_CHUNKS = 20_000
MAX_INPUT_BYTES = 256 * 1024
# PTYs are byte streams: adjacent writes can become one TUI input batch. Give
# the editor time to consume the final paste chunk and finish painting it.
# These bounds are a transport heuristic, not an Agent execution acknowledgement.
BRACKETED_PASTE_MIN_SUBMIT_DELAY_MS = 1000
BRACKETED_PASTE_OUTPUT_QUIET_MS = 150
BRACKETED_PASTE_NO_OUTPUT_WAIT_MS = 1000
BRACKETED_PASTE_MAX_WAIT_MS = 3000


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


class KimiCliTerminalError(RuntimeError):
    def __init__(self, code: str, message: str, *, status_code: int = 409, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


# New integrations should use the vendor-neutral name.  The former export is
# retained because deployment scripts and older API tests import it directly.
AgentCliTerminalError = KimiCliTerminalError


class UsageLedger(Protocol):
    def public(self) -> dict[str, Any]: ...


class EmptyCliUsageLedger:
    """Stable zero projection when a CLI has no provider usage adapter yet."""

    def __init__(self, source: str = "unavailable") -> None:
        self.source = source

    def public(self) -> dict[str, Any]:
        return {
            "schema_version": "agent_cli_token_usage/v1",
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cached_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "uncached_input_tokens": 0,
            "cache_hit_rate": None,
            "model_calls": 0,
            "source": self.source,
            "updated_at": _utc_now(),
        }


@dataclass
class _UsageFileCursor:
    offset: int = 0
    input_other_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0
    output_tokens: int = 0
    model_calls: int = 0


class KimiCliUsageLedger:
    """Incrementally aggregate provider-reported usage for one Kimi Session.

    Kimi's interactive TUI persists an exact ``usage.record`` after each model
    step, including sub-agent and compaction steps.  The REST session summary
    is not updated by every CLI version, so the wire journal is the durable
    source of truth for a browser-hosted PTY session.
    """

    def __init__(self, session_path: Path) -> None:
        self.session_path = session_path.resolve()
        self._lock = threading.RLock()
        self._files: dict[Path, _UsageFileCursor] = {}

    @staticmethod
    def _integer(value: Any) -> int:
        if isinstance(value, bool):
            return 0
        if isinstance(value, (int, float)):
            return max(0, int(value))
        return 0

    @classmethod
    def _record_usage(cls, cursor: _UsageFileCursor, record: dict[str, Any]) -> None:
        if record.get("type") != "usage.record" or not isinstance(record.get("usage"), dict):
            return
        usage = record["usage"]
        cursor.input_other_tokens += cls._integer(usage.get("inputOther"))
        cursor.cache_read_tokens += cls._integer(usage.get("inputCacheRead"))
        cursor.cache_creation_tokens += cls._integer(usage.get("inputCacheCreation"))
        cursor.output_tokens += cls._integer(usage.get("output"))
        cursor.model_calls += 1

    def _refresh_file(self, path: Path, cursor: _UsageFileCursor) -> None:
        try:
            size = path.stat().st_size
        except OSError:
            return
        if size < cursor.offset:
            cursor.offset = 0
            cursor.input_other_tokens = 0
            cursor.cache_read_tokens = 0
            cursor.cache_creation_tokens = 0
            cursor.output_tokens = 0
            cursor.model_calls = 0
        if size == cursor.offset:
            return
        try:
            with path.open("rb") as stream:
                stream.seek(cursor.offset)
                content = stream.read()
        except OSError:
            return
        complete_end = content.rfind(b"\n")
        if complete_end < 0:
            return
        for raw_line in content[:complete_end].splitlines():
            try:
                record = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(record, dict):
                self._record_usage(cursor, record)
        cursor.offset += complete_end + 1

    def public(self) -> dict[str, Any]:
        with self._lock:
            agents_root = self.session_path / "agents"
            try:
                paths = sorted(agents_root.glob("*/wire.jsonl"))
            except OSError:
                paths = []
            current_paths = set(paths)
            for stale_path in set(self._files) - current_paths:
                self._files.pop(stale_path, None)
            for path in paths:
                cursor = self._files.setdefault(path, _UsageFileCursor())
                self._refresh_file(path, cursor)

            input_other = sum(item.input_other_tokens for item in self._files.values())
            cache_read = sum(item.cache_read_tokens for item in self._files.values())
            cache_creation = sum(item.cache_creation_tokens for item in self._files.values())
            output = sum(item.output_tokens for item in self._files.values())
            calls = sum(item.model_calls for item in self._files.values())
            input_tokens = input_other + cache_read + cache_creation
            return {
                "schema_version": "kimi_cli_token_usage/v1",
                "input_tokens": input_tokens,
                "output_tokens": output,
                "total_tokens": input_tokens + output,
                "cached_input_tokens": cache_read,
                "cache_creation_input_tokens": cache_creation,
                "uncached_input_tokens": input_other + cache_creation,
                "cache_hit_rate": cache_read / input_tokens if input_tokens > 0 else None,
                "model_calls": calls,
                "source": "kimi_wire_usage_records",
                "updated_at": _utc_now(),
            }


@dataclass
class _StandardUsageCursor:
    offset: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    cache_creation_input_tokens: int = 0
    model_calls: int = 0


class JsonlCliUsageLedger:
    """Aggregate the vendor-neutral usage journal exposed to custom CLIs."""

    def __init__(self, journal_path: Path) -> None:
        self.journal_path = journal_path.resolve()
        self._lock = threading.RLock()
        self._cursor = _StandardUsageCursor()

    @staticmethod
    def _integer(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return max(0, int(value))
        return None

    @classmethod
    def _first_integer(cls, value: dict[str, Any], *keys: str) -> int | None:
        for key in keys:
            parsed = cls._integer(value.get(key))
            if parsed is not None:
                return parsed
        return None

    @classmethod
    def _record_usage(cls, cursor: _StandardUsageCursor, record: dict[str, Any]) -> None:
        usage = record.get("usage") if isinstance(record.get("usage"), dict) else record
        input_tokens = cls._first_integer(usage, "input_tokens", "prompt_tokens", "inputTokens", "promptTokens")
        output_tokens = cls._first_integer(usage, "output_tokens", "completion_tokens", "outputTokens", "completionTokens")
        input_details = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
        prompt_details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
        cached_tokens = cls._first_integer(
            usage,
            "cached_input_tokens",
            "cache_read_input_tokens",
            "cachedInputTokens",
            "cacheReadInputTokens",
        )
        if cached_tokens is None:
            cached_tokens = cls._first_integer(input_details, "cached_tokens", "cachedTokens")
        if cached_tokens is None:
            cached_tokens = cls._first_integer(prompt_details, "cached_tokens", "cachedTokens")
        cache_creation_tokens = cls._first_integer(
            usage,
            "cache_creation_input_tokens",
            "cacheCreationInputTokens",
        )
        if input_tokens is None or output_tokens is None:
            return
        cursor.input_tokens += input_tokens
        cursor.output_tokens += output_tokens
        cursor.cached_input_tokens += cached_tokens or 0
        cursor.cache_creation_input_tokens += cache_creation_tokens or 0
        cursor.model_calls += 1

    def _refresh(self) -> None:
        cursor = self._cursor
        try:
            size = self.journal_path.stat().st_size
        except OSError:
            return
        if size < cursor.offset:
            self._cursor = cursor = _StandardUsageCursor()
        if size == cursor.offset:
            return
        try:
            with self.journal_path.open("rb") as stream:
                stream.seek(cursor.offset)
                content = stream.read()
        except OSError:
            return
        complete_end = content.rfind(b"\n")
        if complete_end < 0:
            return
        for raw_line in content[:complete_end].splitlines():
            try:
                record = json.loads(raw_line)
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if isinstance(record, dict):
                self._record_usage(cursor, record)
        cursor.offset += complete_end + 1

    def public(self) -> dict[str, Any]:
        with self._lock:
            self._refresh()
            cursor = self._cursor
            return {
                "schema_version": "agent_cli_token_usage/v1",
                "input_tokens": cursor.input_tokens,
                "output_tokens": cursor.output_tokens,
                "total_tokens": cursor.input_tokens + cursor.output_tokens,
                "cached_input_tokens": cursor.cached_input_tokens,
                "cache_creation_input_tokens": cursor.cache_creation_input_tokens,
                "uncached_input_tokens": max(0, cursor.input_tokens - cursor.cached_input_tokens),
                "cache_hit_rate": cursor.cached_input_tokens / cursor.input_tokens if cursor.input_tokens > 0 else None,
                "model_calls": cursor.model_calls,
                "source": "standard_jsonl_usage_events",
                "updated_at": _utc_now(),
            }


@dataclass(frozen=True)
class TerminalChunk:
    sequence: int
    data: bytes
    occurred_at: str

    def public(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "data_base64": base64.b64encode(self.data).decode("ascii"),
            "byte_count": len(self.data),
            "occurred_at": self.occurred_at,
        }


class KimiCliTerminalSession:
    def __init__(
        self,
        *,
        terminal_session_id: str,
        run_id: str,
        kimi_session_id: str = "",
        kimi_workspace_id: str = "",
        binary: Path | None = None,
        workspace: Path,
        environment: dict[str, str],
        log_path: Path,
        cols: int,
        rows: int,
        adapter_id: str = "kimi",
        agent_display_name: str = "Kimi CLI",
        agent_session_id: str = "",
        agent_workspace_id: str = "",
        workspace_contract_sha256: str = "",
        command: tuple[str, ...] | list[str] | None = None,
        submit_mode: str = "bracketed_paste_cr",
        startup_input_delay_ms: int = 0,
        submit_delay_ms: int = 0,
        submit_fallback_ms: int = 0,
        usage_ledger: UsageLedger | None = None,
        on_exit: Callable[[str, int | None], None] | None = None,
    ) -> None:
        self.terminal_session_id = terminal_session_id
        self.run_id = run_id
        self.adapter_id = adapter_id
        self.agent_display_name = agent_display_name
        self.agent_session_id = agent_session_id or kimi_session_id
        self.agent_workspace_id = agent_workspace_id or kimi_workspace_id
        self.workspace_contract_sha256 = workspace_contract_sha256
        self.kimi_session_id = kimi_session_id or self.agent_session_id
        self.kimi_workspace_id = kimi_workspace_id or self.agent_workspace_id
        self.binary = binary.resolve() if binary is not None else Path(str(command[0] if command else "")).resolve()
        self.command = tuple(command) if command else (
            str(self.binary),
            "--auto",
            "--session",
            self.agent_session_id,
        )
        self.submit_mode = submit_mode
        self.startup_input_delay_ms = max(0, min(5000, int(startup_input_delay_ms)))
        self.submit_delay_ms = max(0, min(5000, int(submit_delay_ms)))
        self.effective_submit_delay_ms = max(
            BRACKETED_PASTE_MIN_SUBMIT_DELAY_MS if submit_mode.startswith("bracketed_paste_") else 0,
            self.submit_delay_ms,
        )
        self.submit_fallback_ms = max(0, min(1000, int(submit_fallback_ms)))
        self.workspace = workspace.resolve()
        self.environment = dict(environment)
        self.log_path = log_path.resolve()
        self.cols = max(40, min(320, int(cols)))
        self.rows = max(12, min(160, int(rows)))
        self.on_exit = on_exit
        self.created_at = _utc_now()
        self.last_activity_at = self.created_at
        self.exit_code: int | None = None
        self._master_fd = -1
        self._process: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._condition = threading.Condition(threading.RLock())
        self._async_waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()
        self._write_lock = threading.RLock()
        self._chunks: deque[TerminalChunk] = deque()
        self._command_ids: deque[str] = deque()
        self._command_id_set: set[str] = set()
        self._buffer_bytes = 0
        self._sequence = 0
        self._last_output_monotonic = 0.0
        self._last_submission: dict[str, Any] | None = None
        self._closed = False
        self._started_monotonic = time.monotonic()
        if usage_ledger is not None:
            self.usage_ledger = usage_ledger
        elif self.adapter_id == "kimi":
            kimi_home = Path(os.path.expanduser(self.environment.get("KIMI_CODE_HOME", "~/.kimi-code")))
            self.usage_ledger = KimiCliUsageLedger(
                kimi_home / "sessions" / self.kimi_workspace_id / self.kimi_session_id
            )
        else:
            self.usage_ledger = EmptyCliUsageLedger()
        self._start()

    def _start(self) -> None:
        if os.name != "posix":
            raise KimiCliTerminalError(
                "AGENT_CLI_PTY_UNSUPPORTED",
                "真实 Agent CLI WebShell 当前需要 Linux 或 macOS 后端 PTY。Windows 浏览器不受影响。",
                status_code=501,
            )
        if not self.binary.is_file():
            raise KimiCliTerminalError("AGENT_CLI_BINARY_MISSING", f"未找到 {self.agent_display_name}：{self.binary}")
        if not self.workspace.is_dir():
            raise KimiCliTerminalError("AGENT_CLI_WORKSPACE_MISSING", f"Agent CLI 工作区不存在：{self.workspace}")
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        master_fd, slave_fd = os.openpty()
        self._set_window_size(slave_fd, self.cols, self.rows)
        environment = dict(self.environment)
        environment.update({
            "TERM": "xterm-256color",
            "COLORTERM": "truecolor",
            "COLUMNS": str(self.cols),
            "LINES": str(self.rows),
        })
        try:
            self._process = subprocess.Popen(
                list(self.command),
                stdin=slave_fd,
                stdout=slave_fd,
                stderr=slave_fd,
                cwd=self.workspace,
                env=environment,
                close_fds=True,
                start_new_session=True,
            )
        except OSError as exc:
            os.close(master_fd)
            raise KimiCliTerminalError(
                "AGENT_CLI_START_FAILED",
                f"无法启动 {self.agent_display_name}：{str(exc)[:400]}",
                details={"adapter_id": self.adapter_id, "binary": str(self.binary), "workspace": str(self.workspace)},
            ) from exc
        finally:
            os.close(slave_fd)
        self._master_fd = master_fd
        self._reader = threading.Thread(
            target=self._read_loop,
            name=f"agent-cli-pty-{self.terminal_session_id[-8:]}",
            daemon=True,
        )
        self._reader.start()
        # Browser model credentials are needed only by the spawned process.
        # Do not retain a second long-lived copy in the Python session object.
        self.environment = {}

    @staticmethod
    def _set_window_size(fd: int, cols: int, rows: int) -> None:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", rows, cols, 0, 0))

    def _append(self, data: bytes) -> None:
        if not data:
            return
        occurred_at = _utc_now()
        try:
            with self.log_path.open("ab") as stream:
                stream.write(data)
        except OSError:
            pass
        with self._condition:
            self._sequence += 1
            self._last_output_monotonic = time.monotonic()
            chunk = TerminalChunk(self._sequence, data, occurred_at)
            self._chunks.append(chunk)
            self._buffer_bytes += len(data)
            while self._chunks and (
                len(self._chunks) > MAX_TERMINAL_CHUNKS
                or self._buffer_bytes > MAX_TERMINAL_BUFFER_BYTES
            ):
                removed = self._chunks.popleft()
                self._buffer_bytes -= len(removed.data)
            self.last_activity_at = occurred_at
            self._condition.notify_all()
            self._notify_async_waiters_locked()

    def _notify_async_waiters_locked(self) -> None:
        stale: list[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = []
        for loop, event in tuple(self._async_waiters):
            try:
                loop.call_soon_threadsafe(event.set)
            except RuntimeError:
                stale.append((loop, event))
        for waiter in stale:
            self._async_waiters.discard(waiter)

    def _read_loop(self) -> None:
        try:
            while True:
                try:
                    data = os.read(self._master_fd, 64 * 1024)
                except OSError as exc:
                    if exc.errno in {errno.EIO, errno.EBADF}:
                        break
                    if exc.errno == errno.EINTR:
                        continue
                    raise
                if not data:
                    break
                self._append(data)
        finally:
            process = self._process
            if process is not None:
                try:
                    self.exit_code = process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.exit_code = process.poll()
            with self._condition:
                self._closed = True
                self.last_activity_at = _utc_now()
                self._condition.notify_all()
                self._notify_async_waiters_locked()
            try:
                os.close(self._master_fd)
            except OSError:
                pass
            self._master_fd = -1
            if self.on_exit is not None:
                self.on_exit(self.terminal_session_id, self.exit_code)

    def write(self, data: bytes) -> None:
        if not data:
            return
        if len(data) > MAX_INPUT_BYTES:
            raise KimiCliTerminalError(
                "AGENT_CLI_INPUT_TOO_LARGE",
                f"单次终端输入不能超过 {MAX_INPUT_BYTES // 1024} KiB。",
                status_code=422,
            )
        with self._write_lock:
            if self._closed or self._master_fd < 0 or self._process is None or self._process.poll() is not None:
                raise KimiCliTerminalError("AGENT_CLI_SESSION_EXITED", f"{self.agent_display_name} 进程已经退出，请重新连接 Session。")
            view = memoryview(data)
            while view:
                try:
                    written = os.write(self._master_fd, view)
                except OSError as exc:
                    raise KimiCliTerminalError("AGENT_CLI_INPUT_FAILED", f"写入 {self.agent_display_name} 失败：{str(exc)[:300]}") from exc
                view = view[written:]
            self.last_activity_at = _utc_now()

    def send_command(self, command: str, *, command_id: str = "") -> bool:
        # A retry must not acknowledge an ID whose original paste/submit is
        # still in progress (or about to fail). Serialize delivery and dedupe.
        with self._write_lock:
            return self._send_command_locked(command, command_id=command_id)

    def _wait_for_paste_settle(self, output_sequence: int) -> None:
        """Wait for bounded output settling before emitting the submit key.

        Output activity can include an immediate paste echo or progress marker
        before the editor has committed its input state. It can extend the
        conservative minimum, never shorten it. A quiet window reduces
        paste/Enter coalescing but is not an input-ready acknowledgement.
        """
        started = time.monotonic()
        minimum = self.effective_submit_delay_ms / 1000
        no_output_wait = max(minimum, BRACKETED_PASTE_NO_OUTPUT_WAIT_MS / 1000)
        maximum = max(minimum, BRACKETED_PASTE_MAX_WAIT_MS / 1000)
        quiet = BRACKETED_PASTE_OUTPUT_QUIET_MS / 1000
        with self._condition:
            while True:
                now = time.monotonic()
                elapsed = now - started
                observed = self._sequence > output_sequence
                exited = (
                    self._closed or self._master_fd < 0 or self._process is None
                    or self._process.poll() is not None
                )
                reason = None
                if exited:
                    reason = "session_exited"
                elif elapsed >= maximum:
                    reason = "maximum_wait_reached"
                elif observed and elapsed >= minimum and now - self._last_output_monotonic >= quiet:
                    reason = "output_settled"
                elif not observed and elapsed >= no_output_wait:
                    reason = "no_output_wait_elapsed"
                if self._last_submission is not None:
                    self._last_submission.update({
                        "actual_wait_ms": round(elapsed * 1000, 1),
                        "wait_reason": reason or "waiting",
                        "output_observed": observed,
                    })
                if exited:
                    raise KimiCliTerminalError(
                        "AGENT_CLI_SESSION_EXITED",
                        f"{self.agent_display_name} 在等待粘贴处理时退出，请重新连接 Session。",
                    )
                if reason is not None:
                    return
                next_boundary = (
                    max(started + minimum, self._last_output_monotonic + quiet)
                    if observed else started + no_output_wait
                )
                # Poll process state as well as output: inherited PTY handles
                # can keep the reader open after the actual CLI has exited.
                self._condition.wait(max(0.001, min(0.05, started + maximum - now, next_boundary - now)))

    def _send_command_locked(self, command: str, *, command_id: str = "") -> bool:
        normalized = command.replace("\x00", "").replace("\r\n", "\n").strip()
        if not normalized:
            raise KimiCliTerminalError("AGENT_CLI_COMMAND_EMPTY", f"发送给 {self.agent_display_name} 的命令不能为空。", status_code=422)
        normalized_id = command_id.strip()[:200]
        with self._condition:
            if normalized_id and normalized_id in self._command_id_set:
                return False
            if normalized_id:
                self._command_ids.append(normalized_id)
                self._command_id_set.add(normalized_id)
                while len(self._command_ids) > 2048:
                    removed = self._command_ids.popleft()
                    self._command_id_set.discard(removed)
        bracketed = self.submit_mode.startswith("bracketed_paste_")
        content = f"\x1b[200~{normalized}\x1b[201~".encode("utf-8") if bracketed else normalized.encode("utf-8")
        submit = b"\n" if self.submit_mode.endswith("_lf") else b"\r"
        with self._condition:
            self._last_submission = {
                "schema_version": "agent_cli_submission_transport/v1",
                "command_id": normalized_id,
                "strategy_version": "pty_output_settle/v1" if bracketed else "plain_delay/v1",
                "delivery_state": "writing_content",
                "actual_wait_ms": 0.0,
                "wait_reason": "not_started",
                "output_observed": False,
                "submit_key": "lf" if submit == b"\n" else "cr",
                "submit_key_count": 0,
            }
        try:
            # Treat paste and submit as distinct terminal input events. Several
            # TUIs consume an entire bracketed-paste frame as editor content and
            # ignore a CR/LF appended to the same os.write call.
            with self._write_lock:
                startup_remaining = (
                    self.startup_input_delay_ms / 1000
                    - (time.monotonic() - self._started_monotonic)
                )
                if startup_remaining > 0:
                    time.sleep(startup_remaining)
                with self._condition:
                    output_sequence = self._sequence
                self.write(content)
                with self._condition:
                    self._last_submission["delivery_state"] = "waiting_before_submit"
                if bracketed:
                    self._wait_for_paste_settle(output_sequence)
                else:
                    started = time.monotonic()
                    if self.effective_submit_delay_ms:
                        time.sleep(self.effective_submit_delay_ms / 1000)
                    with self._condition:
                        self._last_submission.update({
                            "actual_wait_ms": round((time.monotonic() - started) * 1000, 1),
                            "wait_reason": "configured_delay_elapsed",
                            "output_observed": self._sequence > output_sequence,
                        })
                self.write(submit)
                with self._condition:
                    self._last_submission["submit_key_count"] += 1
                if self.submit_fallback_ms:
                    # Retain explicitly configured legacy retry behavior. The
                    # settling strategy does not depend on an extra Enter.
                    time.sleep(self.submit_fallback_ms / 1000)
                    self.write(submit)
                    with self._condition:
                        self._last_submission["submit_key_count"] += 1
                with self._condition:
                    self._last_submission["delivery_state"] = "delivered"
        except Exception:
            with self._condition:
                self._last_submission["delivery_state"] = "failed"
                submit_written = self._last_submission["submit_key_count"] > 0
            # An explicit legacy retry can fail after the primary Enter was
            # delivered. Replaying the entire prompt could execute it twice.
            if normalized_id and not submit_written:
                with self._condition:
                    self._command_id_set.discard(normalized_id)
                    try:
                        self._command_ids.remove(normalized_id)
                    except ValueError:
                        pass
            raise
        return True

    def resize(self, cols: int, rows: int) -> None:
        next_cols = max(40, min(320, int(cols)))
        next_rows = max(12, min(160, int(rows)))
        if self._master_fd < 0 or self._closed:
            return
        self._set_window_size(self._master_fd, next_cols, next_rows)
        self.cols = next_cols
        self.rows = next_rows
        try:
            os.killpg(self._process.pid, signal.SIGWINCH) if self._process is not None else None
        except (OSError, ProcessLookupError):
            pass

    def events(self, after_sequence: int, *, wait_seconds: float = 0.0) -> tuple[list[TerminalChunk], bool]:
        deadline = time.monotonic() + max(0.0, wait_seconds)
        with self._condition:
            while self._sequence <= after_sequence and not self._closed and time.monotonic() < deadline:
                self._condition.wait(max(0.0, deadline - time.monotonic()))
            earliest = self._chunks[0].sequence if self._chunks else self._sequence + 1
            truncated = after_sequence < earliest - 1
            return [chunk for chunk in self._chunks if chunk.sequence > after_sequence], truncated

    async def events_async(self, after_sequence: int, *, wait_seconds: float = 0.0) -> tuple[list[TerminalChunk], bool]:
        """Wait for PTY output without pinning one AnyIO worker per browser tab."""

        timeout = max(0.0, float(wait_seconds))
        chunks, truncated = self.events(after_sequence)
        if chunks or self._closed or timeout <= 0:
            return chunks, truncated
        loop = asyncio.get_running_loop()
        event = asyncio.Event()
        waiter = (loop, event)
        with self._condition:
            self._async_waiters.add(waiter)
            if self._sequence > after_sequence or self._closed:
                event.set()
        try:
            await asyncio.wait_for(event.wait(), timeout=timeout)
        except TimeoutError:
            pass
        finally:
            with self._condition:
                self._async_waiters.discard(waiter)
        return self.events(after_sequence)

    def wait_for_exit(self, timeout_seconds: float) -> bool:
        """Wait briefly for startup failure without consuming terminal output."""

        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        with self._condition:
            while not self._closed:
                process = self._process
                if process is not None and process.poll() is not None:
                    return True
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                # A process can exit before the PTY reader observes EOF. Poll
                # periodically so startup fallback never depends on reader
                # thread scheduling.
                self._condition.wait(min(0.05, remaining))
            return self._closed or (self._process is not None and self._process.poll() is not None)

    def stop(self) -> None:
        self.request_stop()
        if not self.wait_for_exit(5):
            self.request_stop(force=True)
            self.wait_for_exit(3)

    def request_stop(self, *, force: bool = False) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        except ProcessLookupError:
            pass

    def public(self) -> dict[str, Any]:
        process = self._process
        running = not self._closed and process is not None and process.poll() is None
        with self._condition:
            earliest = self._chunks[0].sequence if self._chunks else self._sequence + 1
            latest = self._sequence
            last_submission = dict(self._last_submission) if self._last_submission is not None else None
        return {
            "schema_version": "agent_cli_terminal/v1",
            "terminal_session_id": self.terminal_session_id,
            "run_id": self.run_id,
            "adapter_id": self.adapter_id,
            "agent_display_name": self.agent_display_name,
            "agent_session_id": self.agent_session_id,
            "agent_workspace_id": self.agent_workspace_id,
            "workspace_contract_sha256": self.workspace_contract_sha256 or None,
            # Compatibility field for browser tabs created before the generic
            # Agent CLI protocol was introduced.
            "kimi_session_id": self.kimi_session_id,
            "state": "running" if running else "exited",
            "pid": process.pid if running and process is not None else None,
            "exit_code": self.exit_code,
            "cols": self.cols,
            "rows": self.rows,
            "created_at": self.created_at,
            "last_activity_at": self.last_activity_at,
            "earliest_sequence": earliest,
            "latest_sequence": latest,
            "log_path": str(self.log_path),
            "transport": "pty",
            # A browser poll ending is only a transport boundary. The native
            # Agent process stays alive until explicit stop, process exit, or
            # backend shutdown; inactivity never expires the PTY.
            "idle_timeout_seconds": None,
            "lifetime_policy": "until_explicit_stop_or_backend_shutdown",
            "submit_mode": self.submit_mode,
            "startup_input_delay_ms": self.startup_input_delay_ms,
            "submit_delay_ms": self.submit_delay_ms,
            "effective_submit_delay_ms": self.effective_submit_delay_ms,
            "submit_fallback_ms": self.submit_fallback_ms,
            "last_submission": last_submission,
            "token_usage": self.usage_ledger.public(),
        }


class KimiCliTerminalManager:
    def __init__(self, *, state_root: Path, on_exit: Callable[[str, str, int | None], None] | None = None) -> None:
        self.state_root = state_root.resolve()
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.on_exit = on_exit
        self._sessions: dict[str, KimiCliTerminalSession] = {}
        self._run_sessions: dict[str, str] = {}
        self._lock = threading.RLock()

    def create(
        self,
        *,
        run_id: str,
        kimi_session_id: str = "",
        kimi_workspace_id: str = "",
        binary: Path | None = None,
        workspace: Path,
        environment: dict[str, str],
        cols: int,
        rows: int,
        adapter_id: str = "kimi",
        agent_display_name: str = "Kimi CLI",
        agent_session_id: str = "",
        agent_workspace_id: str = "",
        workspace_contract_sha256: str = "",
        command: tuple[str, ...] | list[str] | None = None,
        submit_mode: str = "bracketed_paste_cr",
        startup_input_delay_ms: int = 0,
        submit_delay_ms: int = 0,
        submit_fallback_ms: int = 0,
        usage_ledger: UsageLedger | None = None,
    ) -> KimiCliTerminalSession:
        with self._lock:
            existing_id = self._run_sessions.get(run_id)
            existing = self._sessions.get(existing_id or "")
            if existing is not None and existing.public()["state"] == "running":
                existing.resize(cols, rows)
                return existing
            terminal_session_id = f"terminal_{secrets.token_hex(12)}"
            session = KimiCliTerminalSession(
                terminal_session_id=terminal_session_id,
                run_id=run_id,
                kimi_session_id=kimi_session_id,
                kimi_workspace_id=kimi_workspace_id,
                binary=binary,
                workspace=workspace,
                environment=environment,
                adapter_id=adapter_id,
                agent_display_name=agent_display_name,
                agent_session_id=agent_session_id,
                agent_workspace_id=agent_workspace_id,
                workspace_contract_sha256=workspace_contract_sha256,
                command=command,
                submit_mode=submit_mode,
                startup_input_delay_ms=startup_input_delay_ms,
                submit_delay_ms=submit_delay_ms,
                submit_fallback_ms=submit_fallback_ms,
                usage_ledger=usage_ledger,
                log_path=self.state_root / terminal_session_id / "terminal.ansi",
                cols=cols,
                rows=rows,
                on_exit=self._handle_exit,
            )
            self._sessions[terminal_session_id] = session
            self._run_sessions[run_id] = terminal_session_id
            return session

    def _handle_exit(self, terminal_session_id: str, exit_code: int | None) -> None:
        with self._lock:
            session = self._sessions.get(terminal_session_id)
        if session is not None and self.on_exit is not None:
            self.on_exit(session.run_id, terminal_session_id, exit_code)

    def get(self, terminal_session_id: str) -> KimiCliTerminalSession:
        with self._lock:
            session = self._sessions.get(terminal_session_id)
        if session is None:
            raise KimiCliTerminalError("AGENT_CLI_SESSION_NOT_FOUND", "Agent CLI WebShell Session 不存在或后端已重启。", status_code=404)
        return session

    def for_run(self, run_id: str) -> KimiCliTerminalSession | None:
        with self._lock:
            session_id = self._run_sessions.get(run_id)
            return self._sessions.get(session_id or "")

    def stop(self, terminal_session_id: str) -> None:
        self.get(terminal_session_id).stop()

    def close(self) -> None:
        with self._lock:
            sessions = list(self._sessions.values())
        # Signal every process group before waiting for any one of them. A
        # deployment may own many page sessions; sequential five-second waits
        # used to exceed heteroctl's shutdown grace and orphan the remainder.
        for session in sessions:
            session.request_stop()
        deadline = time.monotonic() + 2.5
        while any(session.public()["state"] == "running" for session in sessions) and time.monotonic() < deadline:
            time.sleep(0.02)
        remaining = [session for session in sessions if session.public()["state"] == "running"]
        for session in remaining:
            session.request_stop(force=True)
        deadline = time.monotonic() + 0.75
        while any(session.public()["state"] == "running" for session in remaining) and time.monotonic() < deadline:
            time.sleep(0.02)


AgentCliTerminalSession = KimiCliTerminalSession
AgentCliTerminalManager = KimiCliTerminalManager
