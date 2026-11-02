"""Agent-engine boundary shared by the platform and concrete runtimes.

The platform owns durable Runs, artifacts and typed executors.  An engine owns
the conversational session and decides which registered tool to call next.
Keeping this protocol small lets a future engine replace Kimi Server without
changing the browser or the build/device evidence contracts.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol

from .core import atomic_write_json


class AgentEngineError(RuntimeError):
    """Provider-neutral engine failure with explicit recovery semantics."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
        recoverable: bool = False,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}
        self.recoverable = recoverable


@dataclass(frozen=True)
class AgentEngineBinding:
    engine_id: str
    engine_version: str
    workspace_id: str
    session_id: str
    workspace_path: str

    def public(self) -> dict[str, Any]:
        return {
            "engine_id": self.engine_id,
            "engine_version": self.engine_version,
            "workspace_id": self.workspace_id,
            "session_id": self.session_id,
            "workspace_path": self.workspace_path,
        }


@dataclass(frozen=True)
class AgentEngineResult:
    prompt_id: str
    reason: str
    assistant_text: str
    thinking_text: str
    usage: dict[str, Any]
    last_sequence: int


EngineEventSink = Callable[[dict[str, Any]], None]


def normalized_token_usage(raw: dict[str, Any]) -> dict[str, Any]:
    """Project an engine's cumulative usage onto the browser token contract."""

    if any(key in raw for key in ("prompt_tokens", "completion_tokens", "total_tokens")):
        prompt = int(raw.get("prompt_tokens", 0) or 0)
        output = int(raw.get("completion_tokens", 0) or 0)
        details = raw.get("prompt_tokens_details") if isinstance(raw.get("prompt_tokens_details"), dict) else {}
        return {
            "prompt_tokens": prompt,
            "completion_tokens": output,
            "total_tokens": int(raw.get("total_tokens", prompt + output) or prompt + output),
            "prompt_tokens_details": {"cached_tokens": int(details.get("cached_tokens", 0) or 0)},
            "cache_creation_input_tokens": int(raw.get("cache_creation_input_tokens", 0) or 0),
            "context_tokens": int(raw.get("context_tokens", 0) or 0),
            "context_limit": int(raw.get("context_limit", 0) or 0),
        }
    cache_read = int(raw.get("inputCacheRead", 0) or 0)
    cache_creation = int(raw.get("inputCacheCreation", 0) or 0)
    input_other = int(raw.get("inputOther", 0) or 0)
    output = int(raw.get("output", 0) or 0)
    prompt = cache_read + cache_creation + input_other
    return {
        "prompt_tokens": prompt,
        "completion_tokens": output,
        "total_tokens": prompt + output,
        "prompt_tokens_details": {"cached_tokens": cache_read},
        "cache_creation_input_tokens": cache_creation,
        "context_tokens": int(raw.get("contextTokens", 0) or 0),
        "context_limit": int(raw.get("maxContextTokens", 0) or 0),
    }


class AgentEngine(Protocol):
    """Minimal stateful engine contract used by a durable platform Run."""

    engine_id: str

    def health(self) -> dict[str, Any]: ...

    def ensure_session(
        self,
        *,
        run_id: str,
        workspace_path: Path,
        title: str,
        metadata: dict[str, Any],
        existing: dict[str, Any] | None = None,
    ) -> AgentEngineBinding: ...

    def run_prompt(
        self,
        binding: AgentEngineBinding,
        prompt: str,
        *,
        on_event: EngineEventSink,
        cancel_requested: Callable[[], bool],
    ) -> AgentEngineResult: ...

    def cancel(self, binding: AgentEngineBinding, prompt_id: str | None = None) -> None: ...


class InternalToolTokenManager:
    """Derive revocable per-Run bearer tokens without persisting model keys."""

    def __init__(self, state_root: Path) -> None:
        self.path = state_root.resolve() / "kimi-engine" / "internal-tool.key"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if not self.path.is_file():
            # atomic_write_json also gives us same-filesystem replacement.  The
            # value is opaque and never included in an Agent event.
            atomic_write_json(self.path, {"key": secrets.token_urlsafe(48)})
            try:
                os.chmod(self.path, 0o600)
            except OSError:
                pass
        raw = self.path.read_text(encoding="utf-8")
        import json

        value = json.loads(raw)
        key = value.get("key") if isinstance(value, dict) else None
        if not isinstance(key, str) or len(key) < 32:
            raise RuntimeError("Kimi internal tool key is invalid")
        self._key = key.encode("utf-8")

    def issue(self, run_id: str) -> str:
        digest = hmac.new(self._key, run_id.encode("utf-8"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    def verify(self, run_id: str, token: str) -> bool:
        return hmac.compare_digest(self.issue(run_id), token.strip())
