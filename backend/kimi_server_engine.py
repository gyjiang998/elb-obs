"""Kimi Server implementation of the platform :mod:`agent_engine` contract."""

from __future__ import annotations

import atexit
import hashlib
import json
import os
import signal
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import httpx
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import connect

from .agent_engine import (
    AgentEngineBinding,
    AgentEngineError,
    AgentEngineResult,
    EngineEventSink,
    normalized_token_usage,
)


class KimiServerError(AgentEngineError):
    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(
            code,
            message,
            details=details,
            recoverable=code in {"KIMI_SERVER_UNAVAILABLE", "KIMI_EVENT_STREAM_CLOSED", "KIMI_PROMPT_IDLE_TIMEOUT"},
        )


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any, limit: int = 4096) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def normalized_stream_delta(value: Any, limit: int = 256 * 1024) -> str:
    """Normalize Kimi text fragments without losing provider whitespace."""

    if limit <= 0:
        return ""
    if isinstance(value, str):
        return value[:limit]
    if isinstance(value, list):
        parts: list[str] = []
        remaining = limit
        for item in value:
            fragment = normalized_stream_delta(item, remaining)
            if fragment:
                parts.append(fragment)
                remaining -= len(fragment)
            if remaining <= 0:
                break
        return "".join(parts)
    if isinstance(value, dict):
        for key in ("text", "content", "value", "delta"):
            fragment = normalized_stream_delta(value.get(key), limit)
            if fragment:
                return fragment
    return ""


def openai_token_usage(raw: dict[str, Any]) -> dict[str, Any]:
    """Project cumulative Kimi step usage onto the browser's token contract."""

    return normalized_token_usage(raw)


@dataclass(frozen=True)
class KimiServerSettings:
    base_url: str
    token_file: Path
    binary: Path
    auto_start: bool
    startup_timeout_seconds: int
    request_timeout_seconds: int
    model: str
    thinking: str

    @classmethod
    def from_env(cls) -> "KimiServerSettings":
        home = Path(os.path.expanduser(os.environ.get("KIMI_CODE_HOME", "~/.kimi-code")))
        return cls(
            base_url=os.environ.get("HETERO_KIMI_SERVER_URL", "http://127.0.0.1:58627").rstrip("/"),
            token_file=Path(os.path.expanduser(os.environ.get("HETERO_KIMI_SERVER_TOKEN_FILE", str(home / "server.token")))),
            binary=Path(os.path.expanduser(os.environ.get("HETERO_KIMI_BIN", str(home / "bin" / "kimi")))),
            auto_start=os.environ.get("HETERO_KIMI_AUTO_START", "1").strip().lower() not in {"0", "false", "no"},
            startup_timeout_seconds=max(2, int(os.environ.get("HETERO_KIMI_STARTUP_TIMEOUT_SECONDS", "30"))),
            request_timeout_seconds=max(30, int(os.environ.get("HETERO_KIMI_REQUEST_TIMEOUT_SECONDS", "1800"))),
            model=(os.environ.get("HETERO_KIMI_MODEL") or os.environ.get("KIMI_MODEL_NAME") or "").strip(),
            thinking=os.environ.get("HETERO_KIMI_THINKING", "medium").strip() or "medium",
        )


@dataclass(frozen=True)
class EphemeralKimiModelCredential:
    base_url: str
    api_key: str
    model: str
    provider_type: str
    context_window_tokens: int
    expires_at: float
    fingerprint: str


KIMI_NATIVE_PROVIDER_HOSTS = frozenset({"api.kimi.com", "api.moonshot.cn", "api.moonshot.ai"})
KIMI_NATIVE_MODEL_IDS = frozenset({"kimi-for-coding", "kimi-for-coding-highspeed", "k3", "k3-256k"})
SUPPORTED_EPHEMERAL_PROVIDER_TYPES = frozenset({"kimi", "openai"})
KIMI_DISABLED_NETWORK_TOOLS = ("FetchURL", "WebSearch")
KIMI_NATIVE_BACKEND_TOOLS = (
    "Bash", "Read", "Write", "Edit", "Glob", "Grep", "ReadMediaFile", "TodoList",
)


def model_provider_type(base_url: str, model: str = "") -> str:
    """Choose the wire adapter from both endpoint identity and model identity.

    Private relays often preserve Kimi model IDs while replacing the public
    hostname.  Treating every such relay as generic OpenAI changes Kimi's
    thinking payload and can make an otherwise valid Coding API return 400.
    """

    try:
        hostname = (urlparse(base_url).hostname or "").lower()
    except ValueError:
        hostname = ""
    model_id = model.strip().lower().rsplit("/", 1)[-1]
    kimi_model = (
        model_id in KIMI_NATIVE_MODEL_IDS
        or model_id.startswith("kimi-")
        or model_id.startswith("moonshot-")
        or model_id.startswith("k3-")
    )
    return "kimi" if hostname in KIMI_NATIVE_PROVIDER_HOSTS or kimi_model else "openai"


def model_capabilities(provider_type: str) -> str:
    """Declare only capabilities that are valid for the selected wire."""

    return "thinking,tool_use" if provider_type == "kimi" else "tool_use"


class KimiServerAgentEngine:
    """Stateful Kimi session adapter with REST control and WebSocket events."""

    engine_id = "kimi-server"

    def __init__(self, settings: KimiServerSettings, *, log_root: Path) -> None:
        self.settings = settings
        self.log_root = log_root.resolve()
        self.log_root.mkdir(parents=True, exist_ok=True)
        self._process: subprocess.Popen[bytes] | None = None
        self._process_log: Any | None = None
        self._process_credential_fingerprint: str | None = None
        self._start_lock = threading.RLock()
        self._resolved_model: str | None = None
        self._ephemeral_credential: EphemeralKimiModelCredential | None = None
        self._active_prompts = 0
        self._session_locks: dict[str, threading.Lock] = {}
        self._session_locks_guard = threading.RLock()
        atexit.register(self.close)

    def _token(self) -> str:
        try:
            token = self.settings.token_file.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise KimiServerError("KIMI_TOKEN_MISSING", "Kimi Server token 文件不可读取。") from exc
        if not token:
            raise KimiServerError("KIMI_TOKEN_MISSING", "Kimi Server token 为空。")
        return token

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}", "Content-Type": "application/json"}

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
        try:
            response = httpx.request(
                method,
                f"{self.settings.base_url}/api/v1{path}",
                headers=self._headers(),
                json=body,
                timeout=timeout or 30.0,
                trust_env=False,
            )
        except (httpx.HTTPError, OSError) as exc:
            raise KimiServerError("KIMI_SERVER_UNAVAILABLE", f"无法连接 Kimi Server：{str(exc)[:400]}") from exc
        try:
            envelope = response.json()
        except ValueError as exc:
            raise KimiServerError("KIMI_RESPONSE_INVALID", "Kimi Server 返回了非 JSON 响应。", details={"status": response.status_code}) from exc
        record = _record(envelope)
        if response.status_code >= 400 or int(record.get("code", -1)) != 0:
            raise KimiServerError(
                "KIMI_REQUEST_REJECTED",
                _text(record.get("msg"), 1200) or f"Kimi Server 请求失败（HTTP {response.status_code}）。",
                details={"status": response.status_code, "code": record.get("code"), "path": path},
            )
        return record.get("data")

    def _meta_once(self) -> dict[str, Any]:
        value = self._request("GET", "/meta")
        return _record(value)

    def _valid_ephemeral_credential(self) -> EphemeralKimiModelCredential | None:
        credential = self._ephemeral_credential
        if credential is None:
            return None
        if credential.expires_at <= time.time():
            self._ephemeral_credential = None
            return None
        return credential

    def _process_environment(self) -> dict[str, str]:
        environment = os.environ.copy()
        environment.setdefault("KIMI_DISABLE_TELEMETRY", "1")
        credential = self._valid_ephemeral_credential()
        if credential is not None:
            environment.update(
                {
                    "KIMI_MODEL_NAME": credential.model,
                    "KIMI_MODEL_API_KEY": credential.api_key,
                    "KIMI_MODEL_BASE_URL": credential.base_url,
                    "KIMI_MODEL_PROVIDER_TYPE": credential.provider_type,
                    "KIMI_MODEL_MAX_CONTEXT_SIZE": str(credential.context_window_tokens),
                    "KIMI_MODEL_CAPABILITIES": model_capabilities(credential.provider_type),
                }
            )
            if credential.provider_type == "kimi":
                environment["KIMI_MODEL_THINKING_EFFORT"] = self.settings.thinking
            else:
                # A generic OpenAI-compatible relay may reject the optional
                # reasoning_effort field.  Capability discovery can be added
                # later without claiming support during initial connection.
                environment.pop("KIMI_MODEL_THINKING_EFFORT", None)
        return environment

    def cli_process_environment(self) -> dict[str, str]:
        """Return the effective model environment for an owned Kimi CLI PTY.

        The returned mapping is passed directly to the child process and is
        never serialized into a Run, terminal event, or browser response.
        """

        return self._process_environment()

    def _stop_owned_process(self) -> None:
        process = self._process
        self._process = None
        self._process_credential_fingerprint = None
        if process is not None and process.poll() is None:
            if os.name == "posix":
                process_groups = self._owned_process_groups(process.pid)
                self._signal_process_groups(process_groups, signal.SIGTERM)
                deadline = time.monotonic() + 2.5
                while process.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.02)
                self._signal_process_groups(process_groups, signal.SIGKILL)
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
            else:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=1)
        if self._process_log is not None:
            try:
                self._process_log.close()
            except OSError:
                pass
            self._process_log = None

    @staticmethod
    def _owned_process_groups(root_pid: int) -> set[int]:
        """Snapshot the server and descendant process groups before shutdown."""

        descendants: dict[int, list[int]] = {}
        try:
            result = subprocess.run(
                ["ps", "-Ao", "pid=,ppid="],
                check=False,
                capture_output=True,
                text=True,
                timeout=2,
            )
            for line in result.stdout.splitlines():
                fields = line.split()
                if len(fields) != 2:
                    continue
                pid, parent = (int(fields[0]), int(fields[1]))
                descendants.setdefault(parent, []).append(pid)
        except (OSError, ValueError, subprocess.TimeoutExpired):
            descendants = {}
        pending = [root_pid]
        owned_pids: set[int] = set()
        while pending:
            pid = pending.pop()
            if pid in owned_pids:
                continue
            owned_pids.add(pid)
            pending.extend(descendants.get(pid, ()))
        process_groups: set[int] = set()
        for pid in owned_pids:
            try:
                process_groups.add(os.getpgid(pid))
            except ProcessLookupError:
                continue
        return process_groups

    @staticmethod
    def _signal_process_groups(process_groups: set[int], signal_number: signal.Signals) -> None:
        for process_group in process_groups:
            try:
                os.killpg(process_group, signal_number)
            except ProcessLookupError:
                pass

    def _start_owned_process(self) -> None:
        if not self.settings.binary.is_file():
            raise KimiServerError("KIMI_BINARY_MISSING", f"未找到 Kimi Code 可执行文件：{self.settings.binary}")
        parsed = urlparse(self.settings.base_url)
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            raise KimiServerError("KIMI_AUTOSTART_REMOTE_FORBIDDEN", "只能自动启动本机 Kimi Server。")
        port = parsed.port or 58627
        log_path = self.log_root / "kimi-server.log"
        self._process_log = log_path.open("ab")
        credential = self._valid_ephemeral_credential()
        self._process = subprocess.Popen(
            [str(self.settings.binary), "web", "--no-open", "--port", str(port), "--log-level", "info"],
            stdin=subprocess.DEVNULL,
            stdout=self._process_log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            env=self._process_environment(),
        )
        self._process_credential_fingerprint = credential.fingerprint if credential is not None else None

    def _wait_until_available(self) -> dict[str, Any]:
        deadline = time.monotonic() + self.settings.startup_timeout_seconds
        last_error: Exception | None = None
        while time.monotonic() < deadline:
            if self._process is not None and self._process.poll() is not None:
                break
            try:
                return self._meta_once()
            except KimiServerError as exc:
                last_error = exc
                time.sleep(0.25)
        raise KimiServerError(
            "KIMI_SERVER_START_FAILED",
            "Kimi Server 未能在规定时间内启动。",
            details={"log_path": str(self.log_root / "kimi-server.log"), "error": str(last_error or "process exited")[:500]},
        )

    def _shutdown_reconfigurable_local_server(self) -> None:
        """Stop an authenticated loopback server before applying a new BYOK env."""

        parsed = urlparse(self.settings.base_url)
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or not self.settings.auto_start:
            raise KimiServerError(
                "KIMI_EXTERNAL_SERVER_RECONFIGURE_FORBIDDEN",
                "当前 Kimi Server 不属于可由平台重载的本机服务。",
            )
        try:
            self._request("POST", "/shutdown", {}, timeout=5.0)
        except KimiServerError as exc:
            # The connection can close before the shutdown response is fully
            # delivered. Only transport closure is acceptable here.
            if exc.code != "KIMI_SERVER_UNAVAILABLE":
                raise
        deadline = time.monotonic() + 8.0
        while time.monotonic() < deadline:
            try:
                self._meta_once()
            except KimiServerError:
                return
            time.sleep(0.1)
        raise KimiServerError(
            "KIMI_EXTERNAL_SERVER_RECONFIGURE_FORBIDDEN",
            "本机 Kimi Server 未能停止，无法安全刷新模型配置。",
        )

    def configure_ephemeral_model(
        self,
        *,
        base_url: str,
        api_key: str,
        model: str,
        context_window_tokens: int,
        ttl_seconds: int = 30 * 60,
        provider_type: str | None = None,
    ) -> dict[str, Any]:
        """Apply browser-held BYOK material without writing Kimi config files."""

        normalized_url = base_url.strip().rstrip("/")
        normalized_model = model.strip()
        normalized_api_key = api_key.strip() if isinstance(api_key, str) else ""
        normalized_provider_type = (provider_type or model_provider_type(normalized_url, normalized_model)).strip().lower()
        try:
            parsed_url = urlparse(normalized_url)
            parsed_url.port
            valid_url = (
                parsed_url.scheme.lower() in {"http", "https"}
                and bool(parsed_url.hostname)
                and parsed_url.username is None
                and parsed_url.password is None
                and not parsed_url.params
                and not parsed_url.query
                and not parsed_url.fragment
                and not any(ord(character) < 32 or ord(character) == 127 for character in normalized_url)
            )
        except ValueError:
            valid_url = False
        if (
            not valid_url
            or not normalized_model
            or not normalized_api_key
            or normalized_provider_type not in SUPPORTED_EPHEMERAL_PROVIDER_TYPES
        ):
            raise KimiServerError("KIMI_EPHEMERAL_CREDENTIAL_INVALID", "模型服务地址、模型名称或 API 密钥不完整。")
        context_window = min(1024 * 1024, max(8 * 1024, int(context_window_tokens)))
        ttl = min(24 * 60 * 60, max(60, int(ttl_seconds)))
        fingerprint = hashlib.sha256(
            f"{normalized_url}\0{normalized_model}\0{normalized_provider_type}\0{context_window}\0{normalized_api_key}".encode("utf-8")
        ).hexdigest()
        with self._start_lock:
            current = self._valid_ephemeral_credential()
            if current is not None and current.fingerprint == fingerprint:
                self._ephemeral_credential = EphemeralKimiModelCredential(
                    base_url=current.base_url,
                    api_key=current.api_key,
                    model=current.model,
                    provider_type=current.provider_type,
                    context_window_tokens=current.context_window_tokens,
                    expires_at=time.time() + ttl,
                    fingerprint=current.fingerprint,
                )
                meta = self.ensure_available()
                return {
                    "status": "ready",
                    "service_status": "ready",
                    "model_status": "ready",
                    "credential_mode": "browser_ephemeral",
                    "model": self._model(),
                    "base_url": normalized_url,
                    "provider_type": normalized_provider_type,
                    "server_version": _text(meta.get("server_version") or meta.get("version"), 120),
                    "expires_in_seconds": ttl,
                    "configuration_reloaded": False,
                }
            if self._active_prompts > 0:
                raise KimiServerError(
                    "KIMI_MODEL_RECONFIGURE_BUSY",
                    "模型正在推理；请停止或等待本轮完成后再切换模型配置。",
                )
            try:
                self._meta_once()
                server_running = True
            except KimiServerError:
                server_running = False
            if server_running and (self._process is None or self._process.poll() is not None):
                # A browser save is an explicit model override. The official
                # loopback server exposes an authenticated shutdown endpoint;
                # use it and restart under FastAPI with the new KIMI_MODEL_*
                # environment instead of silently keeping an older key.
                self._shutdown_reconfigurable_local_server()
            self._ephemeral_credential = EphemeralKimiModelCredential(
                base_url=normalized_url,
                api_key=normalized_api_key,
                model=normalized_model,
                provider_type=normalized_provider_type,
                context_window_tokens=context_window,
                expires_at=time.time() + ttl,
                fingerprint=fingerprint,
            )
            self._resolved_model = None
            self._stop_owned_process()
            self._start_owned_process()
            meta = self._wait_until_available()
            return {
                "status": "ready",
                "service_status": "ready",
                "model_status": "ready",
                "credential_mode": "browser_ephemeral",
                "model": self._model(),
                "base_url": normalized_url,
                "provider_type": normalized_provider_type,
                "server_version": _text(meta.get("server_version") or meta.get("version"), 120),
                "expires_in_seconds": ttl,
                "configuration_reloaded": True,
            }

    def clear_ephemeral_model(self) -> dict[str, Any]:
        """Revoke browser BYOK material and stop the process that inherited it."""

        with self._start_lock:
            if self._active_prompts > 0:
                raise KimiServerError(
                    "KIMI_MODEL_RECONFIGURE_BUSY",
                    "模型正在推理；请停止或等待本轮完成后再清除模型配置。",
                )
            cleared = self._ephemeral_credential is not None
            self._ephemeral_credential = None
            self._resolved_model = None
            self._stop_owned_process()
            # Clearing a browser override restores the backend/Kimi-managed
            # configuration.  The Agent Engine service remains a backend-owned
            # platform component even when no model has been configured yet.
            result = self.health()
            result["credential_cleared"] = cleared
            result["fallback_configuration"] = "backend_or_kimi_managed"
            return result

    def ensure_available(self) -> dict[str, Any]:
        with self._start_lock:
            if self._process_credential_fingerprint and self._valid_ephemeral_credential() is None:
                self._stop_owned_process()
            try:
                return self._meta_once()
            except KimiServerError:
                if not self.settings.auto_start:
                    raise
            self._stop_owned_process()
            self._start_owned_process()
            return self._wait_until_available()

    def start_service(self) -> dict[str, Any]:
        """Start the backend-owned Kimi service without requiring a model."""

        meta = self.ensure_available()
        return {
            "status": "ready",
            "service_status": "ready",
            "engine_id": self.engine_id,
            "base_url": self.settings.base_url,
            "version": _text(meta.get("server_version") or meta.get("version"), 120),
            "tool_policy": self.tool_policy(),
        }

    @staticmethod
    def tool_policy() -> dict[str, Any]:
        return {
            "backend_control": "kimi_native_tools",
            "native_tool_mode": "all_available_except_disabled",
            "native_backend_tools": list(KIMI_NATIVE_BACKEND_TOOLS),
            "disabled_tools": list(KIMI_DISABLED_NETWORK_TOOLS),
            "web_search_enabled": False,
            "web_fetch_enabled": False,
            "cross_boundary_tools": ["page_mcp", "windows_hdc_bridge"],
        }

    def health(self) -> dict[str, Any]:
        try:
            meta = self.ensure_available()
        except KimiServerError as exc:
            return {
                "status": "unavailable",
                "service_status": "unavailable",
                "model_status": "unknown",
                "engine_id": self.engine_id,
                "error": {"code": exc.code, "message": exc.message, "details": exc.details},
                "tool_policy": self.tool_policy(),
            }
        credential = self._valid_ephemeral_credential()
        credential_mode = "browser_ephemeral" if credential is not None else "kimi_managed"
        try:
            model = self._model()
        except KimiServerError as exc:
            if exc.code == "KIMI_MODEL_NOT_CONFIGURED":
                return {
                    "status": "model_configuration_required",
                    "service_status": "ready",
                    "model_status": "unconfigured",
                    "engine_id": self.engine_id,
                    "base_url": self.settings.base_url,
                    "version": _text(meta.get("server_version") or meta.get("version"), 120),
                    "credential_mode": "none",
                    "capabilities": _record(meta.get("capabilities")),
                    "tool_policy": self.tool_policy(),
                }
            return {
                "status": "unavailable",
                "service_status": "ready",
                "model_status": "unavailable",
                "engine_id": self.engine_id,
                "base_url": self.settings.base_url,
                "version": _text(meta.get("server_version") or meta.get("version"), 120),
                "credential_mode": credential_mode,
                "error": {"code": exc.code, "message": exc.message, "details": exc.details},
                "tool_policy": self.tool_policy(),
            }
        expires_in_seconds = max(0, int(credential.expires_at - time.time())) if credential is not None else None
        return {
            "status": "ready",
            "service_status": "ready",
            "model_status": "ready",
            "engine_id": self.engine_id,
            "base_url": self.settings.base_url,
            "version": _text(meta.get("server_version") or meta.get("version"), 120),
            "model": model,
            "credential_mode": credential_mode,
            **({"provider_type": credential.provider_type} if credential is not None else {}),
            **({"expires_in_seconds": expires_in_seconds} if expires_in_seconds is not None else {}),
            "capabilities": _record(meta.get("capabilities")),
            "tool_policy": self.tool_policy(),
        }

    def close(self) -> None:
        with self._start_lock:
            self._stop_owned_process()

    def _model(self) -> str:
        if self.settings.model:
            return self.settings.model
        if self._resolved_model:
            return self._resolved_model
        config = _record(self._request("GET", "/config"))
        model = _text(config.get("default_model"), 240)
        if not model:
            items = _record(self._request("GET", "/models")).get("items")
            if isinstance(items, list) and items:
                model = _text(_record(items[0]).get("model"), 240)
        if not model:
            raise KimiServerError("KIMI_MODEL_NOT_CONFIGURED", "Kimi Server 没有可用的默认模型。")
        self._resolved_model = model
        return model

    @staticmethod
    def _project_skill_name(skill_path: Path) -> str:
        """Return the Skill identity advertised by Kimi's native loader."""

        try:
            with skill_path.open("r", encoding="utf-8-sig") as stream:
                lines = [stream.readline() for _ in range(80)]
        except OSError:
            return skill_path.parent.name
        if not lines or lines[0].strip() != "---":
            return skill_path.parent.name
        for line in lines[1:]:
            stripped = line.strip()
            if stripped == "---":
                break
            key, separator, value = stripped.partition(":")
            if separator and key.strip() == "name":
                declared = value.strip()
                if len(declared) >= 2 and declared[0] == declared[-1] and declared[0] in {"'", '"'}:
                    declared = declared[1:-1].strip()
                if declared and len(declared) <= 240 and all(
                    character.isalnum() or character in {"-", "_", "."}
                    for character in declared
                ):
                    return declared
                break
        return skill_path.parent.name

    @staticmethod
    def _expected_project_skill_names(workspace_path: Path) -> set[str]:
        """Return the project Skill identities Kimi should advertise."""

        names: set[str] = set()
        for root in (workspace_path / ".kimi-code" / "skills", workspace_path / ".agents" / "skills"):
            if not root.is_dir():
                continue
            for candidate in root.iterdir():
                skill_path = candidate / "SKILL.md"
                if candidate.is_dir() and skill_path.is_file():
                    names.add(KimiServerAgentEngine._project_skill_name(skill_path))
        return names

    def _missing_session_skills(self, session_id: str, expected: set[str]) -> list[str]:
        if not expected:
            return []
        payload = _record(self._request("GET", f"/sessions/{session_id}/skills"))
        records = payload.get("skills")
        available = {
            _text(_record(item).get("name"), 240)
            for item in records
            if isinstance(item, dict)
        } if isinstance(records, list) else set()
        return sorted(expected - available)

    def _wait_for_session_skills(
        self,
        session_id: str,
        expected: set[str],
        *,
        timeout_seconds: float = 3.0,
    ) -> list[str]:
        """Wait for the Agent server's asynchronous project-Skill scan."""

        deadline = time.monotonic() + max(0.0, timeout_seconds)
        missing = self._missing_session_skills(session_id, expected)
        while missing and time.monotonic() < deadline:
            time.sleep(0.1)
            missing = self._missing_session_skills(session_id, expected)
        return missing

    def ensure_session(
        self,
        *,
        run_id: str,
        workspace_path: Path,
        title: str,
        metadata: dict[str, Any],
        existing: dict[str, Any] | None = None,
    ) -> AgentEngineBinding:
        meta = self.ensure_available()
        resolved_workspace = workspace_path.resolve()
        expected_skills = self._expected_project_skill_names(resolved_workspace)
        prior = _record(existing)
        if prior.get("engine_id") == self.engine_id and _text(prior.get("session_id")):
            try:
                session = _record(self._request("GET", f"/sessions/{prior['session_id']}"))
                prior_session_id = _text(session.get("id"), 200) or str(prior["session_id"])
                if not self._missing_session_skills(prior_session_id, expected_skills):
                    return AgentEngineBinding(
                        engine_id=self.engine_id,
                        engine_version=_text(meta.get("server_version") or meta.get("version"), 120),
                        workspace_id=_text(session.get("workspace_id"), 200) or _text(prior.get("workspace_id"), 200),
                        session_id=prior_session_id,
                        workspace_path=str(resolved_workspace),
                    )
            except KimiServerError:
                # A stale server-local session is recoverable: create a new
                # session while the platform Run and evidence remain intact.
                pass
        workspace = _record(self._request("POST", "/workspaces", {"root": str(resolved_workspace)}))
        workspace_id = _text(workspace.get("id") or workspace.get("workspace_id"), 200)
        if not workspace_id:
            raise KimiServerError("KIMI_WORKSPACE_INVALID", "Kimi Server 未返回 workspace_id。")
        self._request("POST", f"/workspaces/{workspace_id}/trust", {})
        agent_config: dict[str, Any] = {
            "model": self._model(),
            "permission_mode": "yolo",
            "plan_mode": False,
            "thinking": self.settings.thinking,
        }
        session = _record(
            self._request(
                "POST",
                "/sessions",
                {
                    "workspace_id": workspace_id,
                    "title": title[:200] or run_id,
                    "metadata": {"cwd": str(resolved_workspace), "platform_run_id": run_id, **metadata},
                    "agent_config": agent_config,
                },
            )
        )
        session_id = _text(session.get("id") or session.get("session_id"), 200)
        if not session_id:
            raise KimiServerError("KIMI_SESSION_INVALID", "Kimi Server 未返回 session_id。")
        missing_skills = self._wait_for_session_skills(session_id, expected_skills)
        if missing_skills:
            raise KimiServerError(
                "KIMI_SESSION_SKILLS_MISSING",
                "Kimi Session 未装载 Run 工作区中的项目 Skill。",
                details={
                    "workspace_path": str(resolved_workspace),
                    "session_id": session_id,
                    "missing_skills": missing_skills,
                },
            )
        return AgentEngineBinding(
            engine_id=self.engine_id,
            engine_version=_text(meta.get("server_version") or meta.get("version"), 120),
            workspace_id=workspace_id,
            session_id=session_id,
            workspace_path=str(resolved_workspace),
        )

    def _ws_url(self) -> str:
        parsed = urlparse(self.settings.base_url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return f"{scheme}://{parsed.netloc}/api/v1/ws"

    def _wait_for_platform_mcp(self, binding: AgentEngineBinding) -> None:
        """Give the workspace-scoped MCP process time to publish its tools."""

        config_path = Path(binding.workspace_path) / ".kimi-code" / "mcp.json"
        try:
            config = json.loads(config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        configured_servers = _record(config).get("mcpServers")
        if not isinstance(configured_servers, dict) or not configured_servers:
            return
        deadline = time.monotonic() + 15.0
        expected_ids = set(configured_servers)
        while time.monotonic() < deadline:
            try:
                servers = _record(self._request("GET", "/mcp/servers")).get("servers")
            except KimiServerError:
                servers = []
            if isinstance(servers, list) and any(
                _text(_record(item).get("id"), 160) in expected_ids
                and _text(_record(item).get("status"), 40) == "connected"
                for item in servers
            ):
                return
            time.sleep(0.2)

    @staticmethod
    def _payload(frame: dict[str, Any]) -> dict[str, Any]:
        return _record(frame.get("payload"))

    def run_prompt(
        self,
        binding: AgentEngineBinding,
        prompt: str,
        *,
        on_event: EngineEventSink,
        cancel_requested: Callable[[], bool],
        before_prompt: Callable[[], None] | None = None,
        after_prompt: Callable[[], None] | None = None,
        thinking: str | None = None,
    ) -> AgentEngineResult:
        """Serialize prompts per Kimi Session while allowing independent analyses."""

        with self._session_locks_guard:
            session_lock = self._session_locks.setdefault(binding.session_id, threading.Lock())
        with session_lock:
            if cancel_requested():
                return AgentEngineResult("", "cancelled", "", "", {}, 0)
            with self._start_lock:
                self._active_prompts += 1
            try:
                if before_prompt is not None:
                    before_prompt()
                return self._run_prompt_unlocked(
                    binding,
                    prompt,
                    on_event=on_event,
                    cancel_requested=cancel_requested,
                    thinking=thinking,
                )
            finally:
                if after_prompt is not None:
                    after_prompt()
                with self._start_lock:
                    self._active_prompts = max(0, self._active_prompts - 1)

    @staticmethod
    def _disabled_tools(binding: AgentEngineBinding) -> list[str]:
        """Disable external search/fetch while preserving Kimi's coding tools."""

        del binding
        return list(KIMI_DISABLED_NETWORK_TOOLS)

    def _run_prompt_unlocked(
        self,
        binding: AgentEngineBinding,
        prompt: str,
        *,
        on_event: EngineEventSink,
        cancel_requested: Callable[[], bool],
        thinking: str | None = None,
    ) -> AgentEngineResult:
        token = self._token()
        assistant_parts: list[str] = []
        thinking_parts: list[str] = []
        latest_assistant_step: list[str] = []
        latest_thinking_step: list[str] = []
        usage: dict[str, Any] = {}
        last_sequence = 0
        prompt_id = ""
        final_reason = "failed"
        hello_id = f"hello-{uuid.uuid4()}"
        deadline = time.monotonic() + self.settings.request_timeout_seconds
        self._wait_for_platform_mcp(binding)
        try:
            with connect(
                self._ws_url(),
                subprotocols=[f"kimi-code.bearer.{token}"],
                open_timeout=15,
                close_timeout=5,
                max_size=8 * 1024 * 1024,
            ) as ws:
                while True:
                    frame = json.loads(ws.recv(timeout=15))
                    if isinstance(frame, dict) and frame.get("type") == "server_hello":
                        break
                ws.send(
                    json.dumps(
                        {
                            "type": "client_hello",
                            "id": hello_id,
                            "payload": {"client_id": f"heterogeneous-platform-{binding.session_id[-8:]}", "subscriptions": [binding.session_id]},
                        },
                        ensure_ascii=False,
                    )
                )
                while True:
                    frame = json.loads(ws.recv(timeout=15))
                    if isinstance(frame, dict) and frame.get("type") == "ack" and frame.get("id") == hello_id:
                        if int(frame.get("code", -1)) != 0:
                            raise KimiServerError("KIMI_WS_HANDSHAKE_REJECTED", _text(frame.get("msg"), 1000) or "Kimi WebSocket 握手被拒绝。")
                        break
                submit_body: dict[str, Any] = {
                    "content": [{"type": "text", "text": prompt}],
                    "thinking": _text(thinking, 40) or self.settings.thinking,
                    "permission_mode": "yolo",
                    "plan_mode": False,
                    "model": self._model(),
                    "disabled_tools": self._disabled_tools(binding),
                }
                submitted = _record(self._request("POST", f"/sessions/{binding.session_id}/prompts", submit_body))
                prompt_id = _text(submitted.get("prompt_id"), 200)
                if not prompt_id:
                    raise KimiServerError("KIMI_PROMPT_INVALID", "Kimi Server 未返回 prompt_id。")
                while time.monotonic() < deadline:
                    if cancel_requested():
                        self.cancel(binding, prompt_id)
                        return AgentEngineResult(prompt_id, "cancelled", "".join(latest_assistant_step), "".join(latest_thinking_step), usage, last_sequence)
                    try:
                        raw = ws.recv(timeout=1.0)
                    except TimeoutError:
                        continue
                    frame = json.loads(raw)
                    if not isinstance(frame, dict):
                        continue
                    if frame.get("type") == "ping":
                        ws.send(json.dumps({"type": "pong", "payload": {"nonce": self._payload(frame).get("nonce")}}))
                        continue
                    session_id = frame.get("session_id")
                    if session_id and session_id != binding.session_id:
                        continue
                    sequence = frame.get("seq")
                    if isinstance(sequence, int):
                        last_sequence = max(last_sequence, sequence)
                    payload = self._payload(frame)
                    event_type = _text(frame.get("type"), 160)
                    event_payload = payload
                    if event_type == "turn.step.started":
                        latest_assistant_step = []
                        latest_thinking_step = []
                    elif event_type == "assistant.delta":
                        delta = normalized_stream_delta(payload.get("delta"))
                        latest_assistant_step.append(delta)
                        assistant_parts.append(delta)
                        event_payload = {**payload, "delta": delta}
                    elif event_type == "thinking.delta":
                        delta = normalized_stream_delta(payload.get("delta"))
                        latest_thinking_step.append(delta)
                        thinking_parts.append(delta)
                        event_payload = {**payload, "delta": delta}
                    elif event_type == "turn.step.completed" and isinstance(payload.get("usage"), dict):
                        step_usage = _record(payload.get("usage"))
                        for key in ("inputOther", "output", "inputCacheRead", "inputCacheCreation"):
                            usage[key] = int(usage.get(key, 0) or 0) + int(step_usage.get(key, 0) or 0)
                    elif event_type == "agent.status.updated":
                        if isinstance(payload.get("contextTokens"), (int, float)):
                            usage["contextTokens"] = int(payload["contextTokens"])
                        if isinstance(payload.get("maxContextTokens"), (int, float)):
                            usage["maxContextTokens"] = int(payload["maxContextTokens"])
                    on_event({"type": event_type, "sequence": sequence, "timestamp": frame.get("timestamp"), "volatile": bool(frame.get("volatile")), "payload": event_payload})
                    if event_type == "prompt.completed" and _text(payload.get("promptId") or payload.get("prompt_id"), 200) == prompt_id:
                        final_reason = _text(payload.get("reason"), 80) or "completed"
                        break
                    if event_type == "prompt.aborted" and _text(payload.get("promptId") or payload.get("prompt_id"), 200) == prompt_id:
                        final_reason = "cancelled"
                        break
                else:
                    self.cancel(binding, prompt_id)
                    raise KimiServerError("KIMI_PROMPT_TIMEOUT", "Kimi Agent 在运行时限内没有结束。", details={"prompt_id": prompt_id})
        except ConnectionClosed as exc:
            raise KimiServerError("KIMI_EVENT_STREAM_CLOSED", f"Kimi 事件流意外断开：{exc}") from exc
        except (OSError, ValueError) as exc:
            if isinstance(exc, KimiServerError):
                raise
            raise KimiServerError("KIMI_EVENT_STREAM_FAILED", f"Kimi 事件流失败：{str(exc)[:500]}") from exc
        return AgentEngineResult(
            prompt_id=prompt_id,
            reason=final_reason,
            assistant_text="".join(latest_assistant_step) or "".join(assistant_parts),
            thinking_text="".join(latest_thinking_step) or "".join(thinking_parts),
            usage=usage,
            last_sequence=last_sequence,
        )

    def cancel(self, binding: AgentEngineBinding, prompt_id: str | None = None) -> None:
        try:
            path = f"/sessions/{binding.session_id}/prompts/{prompt_id}:abort" if prompt_id else f"/sessions/{binding.session_id}:abort"
            self._request("POST", path, {})
        except KimiServerError:
            # Platform cancellation remains authoritative.  A failed engine
            # abort is surfaced by the Run trace but must not resurrect work.
            return
