from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

from backend.agent_engine import AgentEngineBinding, AgentEngineError, AgentEngineResult, InternalToolTokenManager
from backend.agent_runtime import AgentRunStore, CredentialLeaseStore
from backend.core import ArtifactStore, SourceConnectorManager, sha256_file
from backend.kimi_agent_runtime import KimiEngineAgentRuntime, _entry_user_payload, _normalize_entry_decision
from backend.kimi_completion import KimiCompletionAdapter
from backend.skill_versions import (
    DEFAULT_SKILL_PROFILE_ID,
    activate_user_skill_version,
    save_user_skill_version,
    set_user_skill_store_for_testing,
)
from backend.user_skills import UserSkillStore
from backend.kimi_server_engine import (
    EphemeralKimiModelCredential,
    KimiServerAgentEngine,
    KimiServerError,
    KimiServerSettings,
    model_provider_type,
    normalized_stream_delta,
    openai_token_usage,
)


class _FakeEngine:
    engine_id = "fake-session-engine"

    def __init__(self) -> None:
        self.prompts: list[str] = []
        self.bindings: list[AgentEngineBinding] = []

    def health(self) -> dict[str, Any]:
        return {"status": "ready", "engine_id": self.engine_id}

    def _model(self) -> str:
        return "fake-model"

    def ensure_session(
        self,
        *,
        run_id: str,
        workspace_path: Path,
        title: str,
        metadata: dict[str, Any],
        existing: dict[str, Any] | None = None,
    ) -> AgentEngineBinding:
        del title, metadata
        existing = existing or {}
        binding = AgentEngineBinding(
            engine_id=self.engine_id,
            engine_version="test-1",
            workspace_id=str(existing.get("workspace_id") or f"workspace-{run_id[-8:]}"),
            session_id=str(existing.get("session_id") or f"session-{run_id[-8:]}"),
            workspace_path=str(workspace_path),
        )
        self.bindings.append(binding)
        return binding

    def run_prompt(
        self,
        binding: AgentEngineBinding,
        prompt: str,
        *,
        on_event: Any,
        cancel_requested: Any,
        before_prompt: Any = None,
        after_prompt: Any = None,
    ) -> AgentEngineResult:
        del binding
        if before_prompt is not None:
            before_prompt()
        self.prompts.append(prompt)
        if cancel_requested():
            return AgentEngineResult("prompt-cancelled", "cancelled", "", "", {}, 0)
        on_event({"type": "turn.step.started", "sequence": 1, "payload": {}})
        on_event({"type": "thinking.delta", "sequence": 2, "payload": {"delta": "正在检查任务。"}})
        on_event({"type": "assistant.delta", "sequence": 3, "payload": {"delta": "测试任务已完成。"}})
        on_event({
            "type": "turn.step.completed",
            "sequence": 4,
            "payload": {
                "usage": {"inputOther": 20, "inputCacheRead": 80, "inputCacheCreation": 0, "output": 10},
                "finishReason": "stop",
            },
        })
        on_event({"type": "prompt.completed", "sequence": 5, "payload": {"promptId": "prompt-1", "reason": "completed"}})
        result = AgentEngineResult(
            prompt_id="prompt-1",
            reason="completed",
            assistant_text="测试任务已完成。",
            thinking_text="正在检查任务。",
            usage={"inputOther": 20, "inputCacheRead": 80, "inputCacheCreation": 0, "output": 10, "contextTokens": 100, "maxContextTokens": 1024},
            last_sequence=5,
        )
        if after_prompt is not None:
            after_prompt()
        return result

    def cancel(self, binding: AgentEngineBinding, prompt_id: str | None = None) -> None:
        del binding, prompt_id


class _EntryDecisionEngine(_FakeEngine):
    def __init__(self, decision: str, *, execution_scope: str = "focused_execution", analysis_target: str = "none") -> None:
        super().__init__()
        self.decision = decision
        self.execution_scope = execution_scope
        self.analysis_target = analysis_target
        self.thinking_modes: list[str | None] = []

    def run_prompt(
        self,
        binding: AgentEngineBinding,
        prompt: str,
        *,
        on_event: Any,
        cancel_requested: Any,
        before_prompt: Any = None,
        after_prompt: Any = None,
        thinking: str | None = None,
    ) -> AgentEngineResult:
        self.thinking_modes.append(thinking)
        if "page_agent_entry_decision/v1" not in prompt:
            return super().run_prompt(
                binding,
                prompt,
                on_event=on_event,
                cancel_requested=cancel_requested,
                before_prompt=before_prompt,
                after_prompt=after_prompt,
            )
        if before_prompt is not None:
            before_prompt()
        self.prompts.append(prompt)
        if cancel_requested():
            return AgentEngineResult("prompt-entry-cancelled", "cancelled", "", "", {}, 0)
        content = json.dumps({
            "schema_version": "page_agent_entry_decision/v1",
            "decision": self.decision,
            "execution_scope": "conversation" if self.decision == "reply" else self.execution_scope,
            "analysis_target": "none" if self.decision == "reply" else self.analysis_target,
            "source_selection": {"content_mode": "none"},
            "reply": "你好，我在。" if self.decision == "reply" else "",
            "reason": "当前请求无需工具。" if self.decision == "reply" else "需要读取真实上下文。",
        }, ensure_ascii=False)
        on_event({"type": "thinking.delta", "sequence": 1, "payload": {"delta": "正在判断是否需要工具。"}})
        on_event({"type": "assistant.delta", "sequence": 2, "payload": {"delta": content}})
        usage = {"inputOther": 30, "inputCacheRead": 10, "inputCacheCreation": 0, "output": 20, "contextTokens": 60, "maxContextTokens": 1024}
        on_event({"type": "turn.step.completed", "sequence": 3, "payload": {"usage": usage, "finishReason": "stop"}})
        if after_prompt is not None:
            after_prompt()
        return AgentEngineResult(
            prompt_id="prompt-entry",
            reason="completed",
            assistant_text=content,
            thinking_text="正在判断是否需要工具。",
            usage=usage,
            last_sequence=3,
        )


class KimiStreamDeltaTests(unittest.TestCase):
    def test_backend_agent_keeps_coding_tools_and_disables_external_search(self) -> None:
        binding = AgentEngineBinding("kimi-server", "test", "workspace", "session", "/tmp/workspace")

        disabled = KimiServerAgentEngine._disabled_tools(binding)
        policy = KimiServerAgentEngine.tool_policy()

        self.assertEqual(disabled, ["FetchURL", "WebSearch"])
        self.assertNotIn("Bash", disabled)
        self.assertNotIn("Read", disabled)
        self.assertNotIn("Edit", disabled)
        self.assertEqual(policy["native_tool_mode"], "all_available_except_disabled")
        self.assertTrue({"Bash", "Read", "Edit", "Glob", "Grep"}.issubset(policy["native_backend_tools"]))
        self.assertFalse(policy["web_search_enabled"])
        self.assertFalse(policy["web_fetch_enabled"])

    def test_normalizes_string_and_structured_text_fragments(self) -> None:
        self.assertEqual(normalized_stream_delta("  保留空白  "), "  保留空白  ")
        self.assertEqual(normalized_stream_delta({"text": "第一段"}), "第一段")
        self.assertEqual(normalized_stream_delta([{"text": "第一段"}, {"content": "第二段"}]), "第一段第二段")

    def test_bounds_normalized_fragment(self) -> None:
        self.assertEqual(normalized_stream_delta([{"text": "123"}, {"text": "456"}], 4), "1234")

    def test_entry_payload_unwraps_backend_start_and_keeps_browser_input_id(self) -> None:
        envelope = json.dumps({
            "schema_version": "page_agent_user_input/v1",
            "input_id": "input_12345678",
            "user_content": "帮我优化下面代码",
            "pending_attachments": [],
        }, ensure_ascii=False)
        wrapper = json.dumps({"schema_version": "page_agent_backend_start/v1", "task": envelope}, ensure_ascii=False)

        payload = _entry_user_payload(wrapper)

        self.assertEqual(payload["input_id"], "input_12345678")
        self.assertEqual(payload["user_content"], "帮我优化下面代码")
        self.assertEqual(payload["source_lines"], [{"line": 1, "text": "帮我优化下面代码"}])
        self.assertEqual(payload["source_line_count"], 1)

    def test_invalid_execute_contract_conservatively_requires_full_analysis_for_new_input(self) -> None:
        decision = _normalize_entry_decision(
            '{"decision":"execute","reason":"需要分析代码"}',
            target_input_id="input_12345678",
        )

        self.assertEqual(decision["execution_scope"], "full_optimization_analysis")
        self.assertEqual(decision["analysis_target"], "new_input")
        self.assertFalse(decision["contract_valid"])

    def test_entry_contract_preserves_model_selected_raw_code_line_range(self) -> None:
        decision = _normalize_entry_decision(
            json.dumps({
                "decision": "execute",
                "execution_scope": "full_optimization_analysis",
                "analysis_target": "new_input",
                "source_selection": {
                    "content_mode": "line_range",
                    "line_start": 2,
                    "line_end": 9,
                },
                "reason": "第一行是意图，后续行是源码。",
            }, ensure_ascii=False),
            target_input_id="input_12345678",
            input_line_count=9,
        )

        self.assertTrue(decision["contract_valid"])
        self.assertEqual(decision["source_selection"], {
            "content_mode": "line_range",
            "line_start": 2,
            "line_end": 9,
        })


class KimiEphemeralCredentialTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)
        binary = root / "kimi"
        binary.write_text("test", encoding="utf-8")
        self.engine = KimiServerAgentEngine(
            KimiServerSettings(
                base_url="http://127.0.0.1:58627",
                token_file=root / "server.token",
                binary=binary,
                auto_start=True,
                startup_timeout_seconds=2,
                request_timeout_seconds=30,
                model="",
                thinking="medium",
            ),
            log_root=root / "logs",
        )
        self.addCleanup(self.engine.close)

    def test_provider_type_uses_native_adapter_only_for_official_kimi_hosts(self) -> None:
        self.assertEqual(model_provider_type("https://api.kimi.com/coding/v1"), "kimi")
        self.assertEqual(model_provider_type("https://api.moonshot.cn/v1"), "kimi")
        self.assertEqual(model_provider_type("http://models.example.internal/v1"), "openai")
        self.assertEqual(model_provider_type("https://gateway.example.com/openai/v1"), "openai")
        self.assertEqual(model_provider_type("http://models.example.internal/v1", "kimi-for-coding"), "kimi")
        self.assertEqual(model_provider_type("http://models.example.internal/v1", "k3-256k"), "kimi")
        self.assertEqual(model_provider_type("http://models.example.internal/v1", "moonshot-v1-128k"), "kimi")

    def test_browser_credential_only_enters_child_environment(self) -> None:
        secret = "sk-browser-only-value"
        captured_environment: dict[str, str] = {}

        def capture_start() -> None:
            captured_environment.update(self.engine._process_environment())

        with (
            patch.object(self.engine, "_meta_once", side_effect=KimiServerError("KIMI_SERVER_UNAVAILABLE", "offline")),
            patch.object(self.engine, "_start_owned_process", side_effect=capture_start),
            patch.object(self.engine, "_wait_until_available", return_value={"server_version": "test"}),
            patch.object(self.engine, "_model", return_value="env/generic-tool-model"),
        ):
            result = self.engine.configure_ephemeral_model(
                base_url="http://models.example.internal/openai/v1",
                api_key=f"  {secret}\n",
                model="generic-tool-model",
                context_window_tokens=262144,
                ttl_seconds=600,
            )

        self.assertEqual(captured_environment["KIMI_MODEL_API_KEY"], secret)
        self.assertEqual(captured_environment["KIMI_MODEL_BASE_URL"], "http://models.example.internal/openai/v1")
        self.assertEqual(captured_environment["KIMI_MODEL_PROVIDER_TYPE"], "openai")
        self.assertEqual(captured_environment["KIMI_MODEL_MAX_CONTEXT_SIZE"], "262144")
        self.assertEqual(captured_environment["KIMI_MODEL_CAPABILITIES"], "tool_use")
        self.assertNotIn("KIMI_MODEL_THINKING_EFFORT", captured_environment)
        self.assertEqual(result["provider_type"], "openai")
        self.assertTrue(result["configuration_reloaded"])
        self.assertNotIn(secret, json.dumps(result, ensure_ascii=False))
        self.assertNotIn(secret, self.engine._ephemeral_credential.fingerprint if self.engine._ephemeral_credential else "")

    def test_service_can_be_ready_before_a_model_is_configured(self) -> None:
        with (
            patch.object(self.engine, "ensure_available", return_value={"server_version": "test", "capabilities": {"mcp": True}}),
            patch.object(
                self.engine,
                "_model",
                side_effect=KimiServerError("KIMI_MODEL_NOT_CONFIGURED", "missing"),
            ),
        ):
            result = self.engine.health()

        self.assertEqual(result["status"], "model_configuration_required")
        self.assertEqual(result["service_status"], "ready")
        self.assertEqual(result["model_status"], "unconfigured")
        self.assertEqual(result["credential_mode"], "none")

    def test_start_service_does_not_require_a_model(self) -> None:
        with patch.object(
            self.engine,
            "ensure_available",
            return_value={"server_version": "test", "capabilities": {"mcp": True}},
        ):
            result = self.engine.start_service()

        self.assertEqual(result["status"], "ready")
        self.assertEqual(result["service_status"], "ready")
        self.assertNotIn("model", result)

    def test_owned_process_groups_include_nested_mcp_sessions(self) -> None:
        process_table = "100 1\n101 100\n102 101\n200 1\n"
        with (
            patch("backend.kimi_server_engine.subprocess.run", return_value=SimpleNamespace(stdout=process_table)),
            patch("backend.kimi_server_engine.os.getpgid", side_effect=lambda pid: {100: 100, 101: 101, 102: 101}[pid]),
        ):
            groups = self.engine._owned_process_groups(100)

        self.assertEqual(groups, {100, 101})

    def test_clear_browser_override_falls_back_without_disabling_service(self) -> None:
        self.engine._ephemeral_credential = EphemeralKimiModelCredential(
            base_url="https://api.kimi.com/coding/v1",
            api_key="sk-browser-only",
            model="kimi-for-coding",
            provider_type="kimi",
            context_window_tokens=262144,
            expires_at=time.time() + 600,
            fingerprint="fingerprint",
        )
        with (
            patch.object(self.engine, "_stop_owned_process") as stop,
            patch.object(
                self.engine,
                "health",
                return_value={"status": "ready", "service_status": "ready", "model_status": "ready"},
            ),
        ):
            result = self.engine.clear_ephemeral_model()

        stop.assert_called_once()
        self.assertIsNone(self.engine._ephemeral_credential)
        self.assertTrue(result["credential_cleared"])
        self.assertEqual(result["fallback_configuration"], "backend_or_kimi_managed")

    def test_browser_override_reloads_an_authenticated_loopback_server(self) -> None:
        with (
            patch.object(self.engine, "_meta_once", return_value={"server_version": "test"}),
            patch.object(self.engine, "_model", return_value="kimi-code/kimi-for-coding"),
            patch.object(self.engine, "_shutdown_reconfigurable_local_server") as shutdown,
            patch.object(self.engine, "_start_owned_process") as start,
        ):
            result = self.engine.configure_ephemeral_model(
                base_url="https://api.kimi.com/coding/v1",
                api_key="sk-not-applied",
                model="kimi-for-coding",
                context_window_tokens=262144,
            )

        self.assertEqual(result["credential_mode"], "browser_ephemeral")
        self.assertTrue(result["configuration_reloaded"])
        self.assertIsNotNone(self.engine._ephemeral_credential)
        shutdown.assert_called_once()
        start.assert_called_once()

    def test_expired_lease_stops_process_that_inherited_secret(self) -> None:
        with (
            patch.object(self.engine, "_meta_once", side_effect=KimiServerError("KIMI_SERVER_UNAVAILABLE", "offline")),
            patch.object(self.engine, "_start_owned_process"),
            patch.object(self.engine, "_wait_until_available", return_value={"server_version": "test"}),
            patch.object(self.engine, "_model", return_value="__kimi_env_model__"),
        ):
            self.engine.configure_ephemeral_model(
                base_url="https://api.kimi.com/coding/v1",
                api_key="sk-expiring",
                model="kimi-for-coding",
                context_window_tokens=262144,
                ttl_seconds=60,
            )
        credential = self.engine._ephemeral_credential
        self.assertIsNotNone(credential)
        assert credential is not None
        self.engine._ephemeral_credential = replace(credential, expires_at=time.time() - 1)
        self.engine._process_credential_fingerprint = credential.fingerprint

        with (
            patch.object(self.engine, "_stop_owned_process") as stop,
            patch.object(self.engine, "_meta_once", return_value={"server_version": "test"}),
        ):
            self.engine.ensure_available()

        stop.assert_called_once()

    def test_existing_session_without_project_skills_is_rebound(self) -> None:
        workspace = Path(self.tempdir.name) / "run-workspace"
        skill = workspace / ".kimi-code" / "skills" / "application-execution-flow-reconstruction" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text(
            "---\nname: application-execution-flow-reconstruction\ndescription: test\n---\n",
            encoding="utf-8",
        )
        calls: list[tuple[str, str]] = []

        def request(method: str, path: str, body: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
            del body, timeout
            calls.append((method, path))
            if (method, path) == ("GET", "/sessions/session-old"):
                return {"id": "session-old", "workspace_id": "workspace-old"}
            if (method, path) == ("GET", "/sessions/session-old/skills"):
                return {"skills": [{"name": "check-kimi-code-docs", "source": "builtin"}]}
            if (method, path) == ("POST", "/workspaces"):
                return {"id": "workspace-new"}
            if (method, path) == ("POST", "/workspaces/workspace-new/trust"):
                return {}
            if (method, path) == ("POST", "/sessions"):
                return {"id": "session-new"}
            if (method, path) == ("GET", "/sessions/session-new/skills"):
                return {"skills": [{"name": "application-execution-flow-reconstruction", "source": "project"}]}
            raise AssertionError(f"unexpected request: {method} {path}")

        with (
            patch.object(self.engine, "ensure_available", return_value={"server_version": "test"}),
            patch.object(self.engine, "_model", return_value="test-model"),
            patch.object(self.engine, "_request", side_effect=request),
        ):
            binding = self.engine.ensure_session(
                run_id="run-test",
                workspace_path=workspace,
                title="test",
                metadata={},
                existing={"engine_id": "kimi-server", "workspace_id": "workspace-old", "session_id": "session-old"},
            )

        self.assertEqual(binding.session_id, "session-new")
        self.assertIn(("GET", "/sessions/session-old/skills"), calls)
        self.assertIn(("GET", "/sessions/session-new/skills"), calls)

    def test_project_skill_preflight_uses_declared_frontmatter_identity(self) -> None:
        workspace = Path(self.tempdir.name) / "aliased-skill-workspace"
        aliased = workspace / ".kimi-code" / "skills" / "page-analysis-artifact-orchestrator" / "SKILL.md"
        fallback = workspace / ".agents" / "skills" / "directory-fallback" / "SKILL.md"
        aliased.parent.mkdir(parents=True)
        fallback.parent.mkdir(parents=True)
        aliased.write_text(
            "---\nname: heterogeneous-page-analysis\ndescription: test\n---\n",
            encoding="utf-8",
        )
        fallback.write_text("# Skill without frontmatter\n", encoding="utf-8")

        names = self.engine._expected_project_skill_names(workspace)

        self.assertEqual(names, {"heterogeneous-page-analysis", "directory-fallback"})
        self.assertNotIn("page-analysis-artifact-orchestrator", names)

    def test_new_session_fails_preflight_when_project_skill_is_missing(self) -> None:
        workspace = Path(self.tempdir.name) / "missing-skill-workspace"
        skill = workspace / ".kimi-code" / "skills" / "required-skill" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: required-skill\ndescription: test\n---\n", encoding="utf-8")

        def request(method: str, path: str, body: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
            del body, timeout
            if (method, path) == ("POST", "/workspaces"):
                return {"id": "workspace-new"}
            if (method, path) == ("POST", "/workspaces/workspace-new/trust"):
                return {}
            if (method, path) == ("POST", "/sessions"):
                return {"id": "session-new"}
            if (method, path) == ("GET", "/sessions/session-new/skills"):
                return {"skills": []}
            raise AssertionError(f"unexpected request: {method} {path}")

        with (
            patch.object(self.engine, "ensure_available", return_value={"server_version": "test"}),
            patch.object(self.engine, "_model", return_value="test-model"),
            patch.object(self.engine, "_request", side_effect=request),
            patch.object(self.engine, "_wait_for_session_skills", return_value=["required-skill"]),
            self.assertRaises(KimiServerError) as captured,
        ):
            self.engine.ensure_session(
                run_id="run-test",
                workspace_path=workspace,
                title="test",
                metadata={},
            )

        self.assertEqual(captured.exception.code, "KIMI_SESSION_SKILLS_MISSING")
        self.assertEqual(captured.exception.details["missing_skills"], ["required-skill"])

    def test_new_session_waits_for_asynchronous_project_skill_scan(self) -> None:
        workspace = Path(self.tempdir.name) / "delayed-skill-workspace"
        skill = workspace / ".kimi-code" / "skills" / "delayed-skill" / "SKILL.md"
        skill.parent.mkdir(parents=True)
        skill.write_text("---\nname: delayed-skill\ndescription: test\n---\n", encoding="utf-8")
        skill_reads = 0

        def request(method: str, path: str, body: dict[str, Any] | None = None, *, timeout: float | None = None) -> Any:
            nonlocal skill_reads
            del body, timeout
            if (method, path) == ("POST", "/workspaces"):
                return {"id": "workspace-new"}
            if (method, path) == ("POST", "/workspaces/workspace-new/trust"):
                return {}
            if (method, path) == ("POST", "/sessions"):
                return {"id": "session-new"}
            if (method, path) == ("GET", "/sessions/session-new/skills"):
                skill_reads += 1
                return {"skills": [] if skill_reads == 1 else [{"name": "delayed-skill", "source": "project"}]}
            raise AssertionError(f"unexpected request: {method} {path}")

        with (
            patch.object(self.engine, "ensure_available", return_value={"server_version": "test"}),
            patch.object(self.engine, "_model", return_value="test-model"),
            patch.object(self.engine, "_request", side_effect=request),
            patch("backend.kimi_server_engine.time.sleep"),
        ):
            binding = self.engine.ensure_session(
                run_id="run-delayed-skill",
                workspace_path=workspace,
                title="test",
                metadata={},
            )

        self.assertEqual(binding.session_id, "session-new")
        self.assertEqual(skill_reads, 2)


class _EchoRouter:
    def __init__(self) -> None:
        self.executions = 0
        self.reuse_allowed = True

    def owner(self, action_id: str) -> str:
        if action_id.startswith("ui."):
            return "ui"
        return "device" if action_id.startswith("device.") else "platform"

    def validate(self, action_id: str, input_value: dict[str, Any], run: dict[str, Any]) -> None:
        del action_id, input_value, run

    def execute(self, action_id: str, input_value: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
        del run
        self.executions += 1
        return {"status": "succeeded", "action_id": action_id, "echo": input_value}

    def can_reuse(self, action_id: str, input_value: dict[str, Any], prior_result: dict[str, Any], run: dict[str, Any]) -> bool:
        del action_id, input_value, prior_result, run
        return self.reuse_allowed

    def cancel(self, run: dict[str, Any]) -> None:
        del run


class _IdleEngine(_FakeEngine):
    def run_prompt(
        self,
        binding: AgentEngineBinding,
        prompt: str,
        *,
        on_event: Any,
        cancel_requested: Any,
        before_prompt: Any = None,
        after_prompt: Any = None,
    ) -> AgentEngineResult:
        del binding, on_event
        if before_prompt is not None:
            before_prompt()
        self.prompts.append(prompt)
        while not cancel_requested():
            time.sleep(0.002)
        if after_prompt is not None:
            after_prompt()
        return AgentEngineResult("prompt-idle", "cancelled", "", "", {}, 0)


class _FailedCompletionEngine(_FakeEngine):
    def run_prompt(
        self,
        binding: AgentEngineBinding,
        prompt: str,
        *,
        on_event: Any,
        cancel_requested: Any,
        before_prompt: Any = None,
        after_prompt: Any = None,
    ) -> AgentEngineResult:
        del binding, prompt, cancel_requested
        if before_prompt is not None:
            before_prompt()
        on_event({
            "type": "turn.failed",
            "sequence": 1,
            "payload": {"error": {"code": "authentication_failed", "message": "upstream rejected the API key"}},
        })
        if after_prompt is not None:
            after_prompt()
        return AgentEngineResult("prompt-failed", "failed", "", "", {}, 1)


class AgentEngineBoundaryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        previous_user_skills = set_user_skill_store_for_testing(UserSkillStore(self.root / "user-skills"))
        self.addCleanup(set_user_skill_store_for_testing, previous_user_skills)
        self.store = AgentRunStore(self.root / "agent-runs")
        self.engine = _FakeEngine()
        self.router = _EchoRouter()
        self.runtime = KimiEngineAgentRuntime(
            store=self.store,
            leases=CredentialLeaseStore(default_ttl_seconds=60),
            model_gateway=lambda *_args, **_kwargs: {},
            action_router=self.router,
            system_prompt=lambda _run: "unused",
            engine=self.engine,
            token_manager=InternalToolTokenManager(self.root),
            platform_api_base="http://127.0.0.1:8006",
            app_root=Path(__file__).resolve().parents[2],
        )

    def _create(self) -> str:
        run = self.runtime.create(
            project_id="local-default",
            goal="验证可替换 Agent Engine 边界",
            initial_input="完成最小测试。",
            mode="page_agent",
            context={"fact": "value"},
            budget={"max_turns": 3, "max_actions_per_turn": 2, "max_output_tokens": 4096, "context_window_tokens": 8192},
            action_catalog_version="test",
            skill_versions=[],
        )
        return str(run["run_id"])

    def test_focused_flow_task_binds_the_canonical_backend_workspace(self) -> None:
        source_root = self.root / "camera-module"
        source_root.mkdir()
        (source_root / "camera.cpp").write_text("void StartCamera() {}\n", encoding="utf-8")
        gallery_root = self.root / "gallery-module"
        gallery_root.mkdir()
        (gallery_root / "gallery.cpp").write_text("void SavePhoto() {}\n", encoding="utf-8")
        artifact_store = ArtifactStore(self.root / "source-state", project_id="local-default")
        connectors = SourceConnectorManager(self.root / "source-state", artifact_store, workspace_root=self.root)
        connectors.registry.write({"connectors": [
            {
                "connector_id": "local_camera",
                "source_root": str(source_root),
                "snapshot": {"snapshot_id": "source_snapshot_camera", "clean": True},
            },
            {
                "connector_id": "local_gallery",
                "source_root": str(gallery_root),
                "snapshot": {"snapshot_id": "source_snapshot_gallery", "clean": True},
            },
        ]})
        runtime = KimiEngineAgentRuntime(
            store=self.store,
            leases=CredentialLeaseStore(default_ttl_seconds=60),
            model_gateway=lambda *_args, **_kwargs: {},
            action_router=self.router,
            system_prompt=lambda _run: "unused",
            engine=self.engine,
            token_manager=InternalToolTokenManager(self.root),
            platform_api_base="http://127.0.0.1:8006",
            app_root=Path(__file__).resolve().parents[2],
            source_connectors=connectors,
        )
        workspace = self.root / "flow-workspace"
        workspace.mkdir()

        source_context = runtime._prepare_backend_source_context(workspace, {
            "focused_task": {
                "kind": "application_flow_discovery",
                "source_connector_id": "local_camera",
                "source_snapshot_id": "source_snapshot_camera",
                "subpath": "",
                "source_scopes": [
                    {
                        "source_connector_id": "local_camera",
                        "source_snapshot_id": "source_snapshot_camera",
                        "subpath": "",
                        "graph_id": "mcg_camera",
                    },
                    {
                        "source_connector_id": "local_gallery",
                        "source_snapshot_id": "source_snapshot_gallery",
                        "subpath": "",
                        "graph_id": "mcg_gallery",
                    },
                ],
            }
        })

        self.assertEqual(source_context["profile"], "agent_native_backend_workspace")
        self.assertEqual(source_context["source_scope"], str(source_root.resolve()))
        self.assertEqual(source_context["workspace_path"], str(source_root.resolve()))
        self.assertIn("source_scopes", source_context["native_tool_guidance_zh"])
        self.assertEqual(len(source_context["source_scopes"]), 2)
        self.assertEqual(source_context["source_scopes"][1]["graph_id"], "mcg_gallery")
        self.assertEqual((workspace / "repository").resolve(), source_root.resolve())
        self.assertEqual((workspace / "repositories" / "local_gallery").resolve(), gallery_root.resolve())

    def test_focused_flow_task_projects_static_graph_retrieval_context(self) -> None:
        source_root = self.root / "shared-code-context"
        source_root.mkdir()
        (source_root / "camera.cpp").write_text("void StartCamera() {}\n", encoding="utf-8")
        artifact_store = ArtifactStore(self.root / "shared-source-state", project_id="local-default")
        connectors = SourceConnectorManager(self.root / "shared-source-state", artifact_store, workspace_root=self.root)
        connectors.registry.write({"connectors": [{
            "connector_id": "local_shared_context",
            "source_root": str(source_root),
            "snapshot": {"snapshot_id": "source_snapshot_shared", "clean": True},
        }]})
        runtime = KimiEngineAgentRuntime(
            store=self.store,
            leases=CredentialLeaseStore(default_ttl_seconds=60),
            model_gateway=lambda *_args, **_kwargs: {},
            action_router=self.router,
            system_prompt=lambda _run: "unused",
            engine=self.engine,
            token_manager=InternalToolTokenManager(self.root),
            platform_api_base="http://127.0.0.1:8006",
            app_root=Path(__file__).resolve().parents[2],
            source_connectors=connectors,
        )
        run = runtime.create(
            project_id="local-default",
            goal="发现相机启动流程",
            initial_input="发现流程。",
            mode="page_agent",
            context={
                "focused_task": {
                    "kind": "application_flow_discovery",
                    "source_connector_id": "local_shared_context",
                    "source_snapshot_id": "source_snapshot_shared",
                    "subpath": "",
                    "graph_id": "mcg_1234567890abcdef12345678",
                }
            },
            budget={"max_turns": 3, "max_actions_per_turn": 2, "max_output_tokens": 4096, "context_window_tokens": 8192},
            action_catalog_version="test",
            skill_versions=[],
        )

        task = json.loads((runtime.refresh_workspace(str(run["run_id"])) / "task.json").read_text(encoding="utf-8"))
        source_analysis = task["source_analysis"]
        self.assertEqual(source_analysis["focused_task_kind"], "application_flow_discovery")
        self.assertEqual(source_analysis["code_graph_id"], "mcg_1234567890abcdef12345678")
        self.assertEqual(source_analysis["retrieval_policy"], "bounded_static_graph_then_native_source")
        self.assertEqual(source_analysis["knowledge_engine"]["status"], "ready")
        self.assertEqual(source_analysis["knowledge_engine"]["engine_id"], "platform-tree-sitter")
        self.assertIn("bounded_call_candidate_query", source_analysis["knowledge_engine"]["capabilities"])
        self.assertIn("agent_native_source_analysis", source_analysis["knowledge_engine"]["capabilities"])

    def test_workspace_refresh_hot_loads_active_user_skill_and_restores_server_fallback(self) -> None:
        run_id = self._create()
        workspace = self.runtime.refresh_workspace(run_id)
        skill_path = workspace / ".kimi-code" / "skills" / "strategy-hardware-kb" / "SKILL.md"
        server_content = skill_path.read_text(encoding="utf-8")
        self.assertIn("硬件与 API 知识策略", server_content)

        saved = save_user_skill_version(
            skill_id="strategy-hardware-kb",
            label_zh="硬件机会推理",
            description_zh="测试热加载",
            content="# 热加载测试\n\n只保留当前测试契约。",
            profile_id=DEFAULT_SKILL_PROFILE_ID,
        )
        self.runtime.refresh_workspace(run_id)
        user_content = skill_path.read_text(encoding="utf-8")
        self.assertIn("# 热加载测试", user_content)
        self.assertNotEqual(user_content, server_content)
        task = json.loads((workspace / "task.json").read_text(encoding="utf-8"))
        self.assertEqual(task["skill_profile"]["skills"]["strategy-hardware-kb"]["origin"], "user")
        self.assertEqual(
            task["skill_profile"]["skills"]["strategy-hardware-kb"]["user_version_id"],
            saved["version"]["version_id"],
        )

        activate_user_skill_version("strategy-hardware-kb", None, profile_id=DEFAULT_SKILL_PROFILE_ID)
        self.runtime.refresh_workspace(run_id)
        self.assertEqual(skill_path.read_text(encoding="utf-8"), server_content)

    def test_workspace_refresh_reuses_unchanged_projection(self) -> None:
        run_id = self._create()
        workspace = self.runtime.refresh_workspace(run_id)
        task_path = workspace / "task.json"
        first_mtime = task_path.stat().st_mtime_ns

        with patch.object(self.runtime, "_prepare_backend_source_context") as source_projection:
            self.assertEqual(self.runtime.refresh_workspace(run_id), workspace)

        source_projection.assert_not_called()
        self.assertEqual(task_path.stat().st_mtime_ns, first_mtime)

    def _wait(self, run_id: str, state: str = "completed") -> dict[str, Any]:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            run = self.store.read(run_id)
            if run.get("state") == state:
                return run
            time.sleep(0.01)
        self.fail(f"run did not reach {state}: {self.store.read(run_id).get('state')}")

    def _runtime_for_engine(self, engine: _FakeEngine) -> KimiEngineAgentRuntime:
        return KimiEngineAgentRuntime(
            store=self.store,
            leases=CredentialLeaseStore(default_ttl_seconds=60),
            model_gateway=lambda *_args, **_kwargs: {},
            action_router=self.router,
            system_prompt=lambda _run: "unused",
            engine=engine,
            token_manager=InternalToolTokenManager(self.root),
            platform_api_base="http://127.0.0.1:8006",
            app_root=Path(__file__).resolve().parents[2],
        )

    def _create_with_entry_model(self, runtime: KimiEngineAgentRuntime, message: str) -> str:
        run = runtime.create(
            project_id="local-default",
            goal=message,
            initial_input=json.dumps({
                "schema_version": "page_agent_user_input/v1",
                "input_id": "input-test",
                "user_content": message,
                "pending_attachments": [],
            }, ensure_ascii=False),
            mode="page_agent",
            context={
                "agent_entry_policy": "model_decides",
                "page_context": json.dumps({
                    "workflow": {"stage": "idle", "detail": "尚未开始分析。"},
                    "reasoning_lab": {"hotspots": [], "code_unit": {}, "strategy_results": []},
                }, ensure_ascii=False),
            },
            budget={"max_turns": 3, "max_actions_per_turn": 2, "max_output_tokens": 4096, "context_window_tokens": 8192},
            action_catalog_version="test",
            skill_versions=[],
        )
        return str(run["run_id"])

    def test_entry_model_can_reply_without_loading_full_agent_workspace(self) -> None:
        engine = _EntryDecisionEngine("reply")
        runtime = self._runtime_for_engine(engine)
        run_id = self._create_with_entry_model(runtime, "hi")

        runtime.start(run_id)
        run = self._wait(run_id)

        self.assertNotIn("engine", run)
        self.assertEqual(run["checkpoint"]["entry_triage"]["decision"], "reply")
        self.assertEqual(engine.thinking_modes, ["low"])
        self.assertEqual(len(engine.prompts), 1)
        self.assertIn("page_agent_entry_decision/v1", engine.prompts[0])
        self.assertFalse((runtime._workspace(run) / ".kimi-code" / "mcp.json").exists())
        events = self.store.events(run_id, after_sequence=0, limit=200)
        response = next(event for event in events if event["event_type"] == "model.response")
        self.assertEqual(response["payload"]["phase"], "entry_triage")
        self.assertEqual(response["payload"]["content"], "你好，我在。")
        decision = next(event for event in events if event["event_type"] == "agent.entry_decision")
        self.assertEqual(decision["payload"]["decision"], "reply")

    def test_entry_model_can_escalate_to_full_agent_in_same_run(self) -> None:
        engine = _EntryDecisionEngine("execute")
        runtime = self._runtime_for_engine(engine)
        run_id = self._create_with_entry_model(runtime, "分析当前热点代码")

        runtime.start(run_id)
        run = self._wait(run_id)

        self.assertEqual(run["checkpoint"]["entry_triage"]["decision"], "execute")
        self.assertEqual(engine.thinking_modes, ["low", None])
        self.assertEqual(len(engine.prompts), 2)
        self.assertIn("page_agent_entry_decision/v1", engine.prompts[0])
        self.assertIn("# 平台任务", engine.prompts[1])
        self.assertIn("engine", run)
        self.assertTrue((runtime._workspace(run) / ".kimi-code" / "mcp.json").is_file())
        events = self.store.events(run_id, after_sequence=0, limit=200)
        responses = [event for event in events if event["event_type"] == "model.response"]
        self.assertEqual(len(responses), 2)
        self.assertEqual(responses[0]["payload"]["phase"], "entry_triage")
        self.assertEqual(responses[0]["payload"]["content"], "")
        self.assertEqual(responses[1]["payload"]["content"], "测试任务已完成。")

    def test_agent_direct_enters_the_persistent_kimi_session_without_entry_triage(self) -> None:
        engine = _EntryDecisionEngine("reply")
        runtime = self._runtime_for_engine(engine)
        message = "hi"
        run = runtime.create(
            project_id="local-default",
            goal=message,
            initial_input=json.dumps({
                "schema_version": "page_agent_user_input/v1",
                "input_id": "input-direct",
                "user_content": message,
                "pending_attachments": [],
            }, ensure_ascii=False),
            mode="page_agent",
            context={"agent_entry_policy": "agent_direct", "page_context": "{}"},
            budget={"max_turns": 3, "max_actions_per_turn": 2, "max_output_tokens": 4096, "context_window_tokens": 8192},
            action_catalog_version="test",
            skill_versions=[],
        )
        run_id = str(run["run_id"])

        runtime.start(run_id)
        completed = self._wait(run_id)

        self.assertNotIn("entry_triage", completed["checkpoint"])
        self.assertEqual(engine.thinking_modes, [None])
        self.assertEqual(len(engine.prompts), 1)
        self.assertIn("# 平台任务", engine.prompts[0])
        events = self.store.events(run_id, after_sequence=0, limit=200)
        self.assertFalse(any(event["event_type"] == "agent.entry_decision" for event in events))
        self.assertTrue((runtime._workspace(completed) / ".kimi-code" / "mcp.json").is_file())

    def test_runtime_observation_does_not_reenter_user_entry_triage(self) -> None:
        run = {
            "mode": "page_agent",
            "context": {"agent_entry_policy": "model_decides"},
            "checkpoint": {},
        }
        user_turn = json.dumps({
            "schema_version": "page_agent_user_input/v1",
            "input_id": "input-test",
            "user_content": "继续分析",
            "pending_attachments": [],
        }, ensure_ascii=False)
        observation = "上一轮页面 action 已返回真实结果，请从当前检查点继续。"

        self.assertTrue(KimiEngineAgentRuntime._entry_triage_pending(run, user_turn))
        self.assertFalse(KimiEngineAgentRuntime._entry_triage_pending(run, observation))

    def test_engine_session_drives_run_and_persists_stream_usage(self) -> None:
        run_id = self._create()

        self.runtime.start(run_id)
        run = self._wait(run_id)

        self.assertEqual(run["engine"]["engine_id"], self.engine.engine_id)
        self.assertEqual(run["engine"]["session_id"], f"session-{run_id[-8:]}")
        self.assertIn("完成最小测试", self.engine.prompts[0])
        workspace = Path(run["engine"]["workspace_path"])
        self.assertTrue((workspace / "AGENTS.md").is_file())
        self.assertEqual((workspace / ".git" / "HEAD").read_text(encoding="ascii"), "ref: refs/heads/main\n")
        self.assertIn("repositoryformatversion = 0", (workspace / ".git" / "config").read_text(encoding="ascii"))
        device_skill = workspace / ".kimi-code" / "skills" / "heterogeneous-device-validation" / "SKILL.md"
        flow_skill = workspace / ".kimi-code" / "skills" / "application-execution-flow-reconstruction" / "SKILL.md"
        framework_reference = device_skill.parent / "references" / "official-test-frameworks.md"
        template_catalog_path = device_skill.parent / "assets" / "hardware-api-templates" / "catalog.json"
        self.assertTrue(device_skill.is_file())
        self.assertTrue(flow_skill.is_file())
        self.assertIn("name: application-execution-flow-reconstruction", flow_skill.read_text(encoding="utf-8"))
        self.assertTrue(framework_reference.is_file())
        self.assertTrue(template_catalog_path.is_file())
        device_skill_text = device_skill.read_text(encoding="utf-8")
        agents_text = (workspace / "AGENTS.md").read_text(encoding="utf-8")
        portable_runtime_skill = workspace / ".agents" / "skills" / "agent-runtime-orchestrator" / "SKILL.md"
        portable_runtime_text = portable_runtime_skill.read_text(encoding="utf-8")
        framework_reference_text = framework_reference.read_text(encoding="utf-8")
        template_catalog = json.loads(template_catalog_path.read_text(encoding="utf-8"))
        template_ids = {item["template_id"] for item in template_catalog["templates"]}
        self.assertIn("standalone_fallback", device_skill_text)
        self.assertIn("reasoning_prepare_hardware_semantic_route", agents_text)
        self.assertIn("$HETERO_AGENT_SKILL_ROOT/<skill-id>/SKILL.md", agents_text)
        self.assertIn(".agents/skills/<skill-id>/SKILL.md", agents_text)
        self.assertIn("Agent 框架自身的 Skill 发现约定", agents_text)
        self.assertIn(".agent-platform/skill-bootstrap.json", agents_text)
        self.assertIn("platform_ack_skill_bootstrap", agents_text)
        self.assertIn("任务涉及模块职责", agents_text)
        self.assertIn("code-context-retrieval", agents_text)
        self.assertTrue(portable_runtime_skill.is_file())
        self.assertIn("真实结果", portable_runtime_text)
        self.assertIn("$HETERO_AGENT_SKILL_ROOT/<skill-id>/SKILL.md", portable_runtime_text)
        self.assertIn("页面命令已点名 MCP 工具时立即调用", portable_runtime_text)
        self.assertNotIn("不要先搜索平台源码、Action ID 或全量 Skill", portable_runtime_text)
        self.assertNotIn("清单与哈希", portable_runtime_text)
        self.assertIn("后续阶段沿用已经加载的内容", portable_runtime_text)
        self.assertNotIn("platform_ack_skill_bootstrap", portable_runtime_text)
        self.assertIn("code-context-retrieval", portable_runtime_text)
        for phrase in ("Agent Session", "Kimi Session", "当前 Session", "同一 Session", "本 Session", "另起模型会话", "代为触发"):
            self.assertNotIn(phrase, agents_text)
            self.assertNotIn(phrase, portable_runtime_text)
        self.assertIn("platform_list_hardware_api_templates", device_skill_text)
        self.assertIn("精确 ID", device_skill_text)
        self.assertNotIn("模板历史不是本轮证据", device_skill_text)
        self.assertLess(len(agents_text), 6000)
        self.assertLess(len(device_skill_text), 6000)
        self.assertLess(len(flow_skill.read_text(encoding="utf-8")), 6000)
        self.assertIn("xDevice `CppTest`", framework_reference_text)
        self.assertIn("Hypium/OHJSUnitTest", framework_reference_text)
        self.assertIn("GPU Vulkan compute", framework_reference_text)
        self.assertIn("NPU NNRT", framework_reference_text)
        self.assertIn("媒体 AVCodec", framework_reference_text)
        self.assertIn("DSP/Audio Effect", framework_reference_text)
        self.assertIn("CPU 并行/NEON", framework_reference_text)
        self.assertIn("gpu.vulkan.compute-buffer.v1", template_ids)
        self.assertIn("gpu.gles3.fragment-image.v1", template_ids)
        self.assertIn("npu.nnrt.graph-lifecycle.v1", template_ids)
        self.assertIn("media.avcodec.hardware-capability.v1", template_ids)
        self.assertIn("dsp.audio-effect-provider.recipe.v1", template_ids)
        for template in template_catalog["templates"]:
            self.assertEqual(template["asset_class"], "curated_template")
            self.assertTrue(template["version"])
            for relative in template["files"]:
                materialized = template_catalog_path.parent / relative
                self.assertTrue(materialized.is_file())
                self.assertEqual(sha256_file(materialized), template["file_sha256"][relative])
        npu_template = next(item for item in template_catalog["templates"] if item["unit"] == "npu")
        self.assertEqual(npu_template["verification"]["level"], "device_negative_observation")
        self.assertNotIn("gpu_rgba_brightness", device_skill_text + framework_reference_text)
        self.assertNotIn("Maleoon 920", device_skill_text + framework_reference_text)
        mcp_config = json.loads((workspace / ".kimi-code" / "mcp.json").read_text(encoding="utf-8"))
        self.assertEqual(len(mcp_config["mcpServers"]), 1)
        mcp_server_id = next(iter(mcp_config["mcpServers"]))
        self.assertTrue(mcp_server_id.startswith("heterogeneous_platform_"))
        self.assertNotIn("_v2_", mcp_server_id)
        mcp_tools = json.loads((workspace / ".agent-platform" / "mcp-tools.json").read_text(encoding="utf-8"))
        self.assertEqual(mcp_tools["server_id"], mcp_server_id)
        self.assertEqual(mcp_tools["tool_count"], len(mcp_tools["tool_names"]))
        self.assertIn("application_flow_compile_mermaid", mcp_tools["tool_names"])
        events = self.store.events(run_id, after_sequence=0, limit=200)
        response = next(event for event in events if event["event_type"] == "model.response")
        self.assertEqual(response["payload"]["content"], "测试任务已完成。")
        self.assertEqual(response["payload"]["usage"]["prompt_tokens"], 100)
        self.assertEqual(response["payload"]["usage"]["prompt_tokens_details"]["cached_tokens"], 80)

    def test_structured_completion_stops_after_model_event_idle_timeout(self) -> None:
        adapter = KimiCompletionAdapter(_IdleEngine(), root=self.root / "idle-inference")
        adapter.structured_idle_timeout_seconds = 0.02

        with self.assertRaises(KimiServerError) as captured:
            adapter.complete(messages=[{"role": "user", "content": "返回结构化结果。"}], max_tokens=256)

        self.assertEqual(captured.exception.code, "KIMI_PROMPT_IDLE_TIMEOUT")
        self.assertTrue(captured.exception.recoverable)

    def test_structured_completion_surfaces_provider_failure_event(self) -> None:
        adapter = KimiCompletionAdapter(_FailedCompletionEngine(), root=self.root / "failed-inference")

        with self.assertRaises(KimiServerError) as captured:
            adapter.complete(messages=[{"role": "user", "content": "返回结构化结果。"}], max_tokens=256)

        self.assertEqual(captured.exception.code, "KIMI_COMPLETION_FAILED")
        self.assertIn("upstream rejected the API key", captured.exception.message)
        self.assertEqual(captured.exception.details["provider_event_type"], "turn.failed")
        self.assertEqual(captured.exception.details["provider_message"], "upstream rejected the API key")

    def test_structured_stages_and_runtime_task_reuse_one_engine_session(self) -> None:
        run_id = self._create()
        binding = self.runtime.ensure_session_binding(run_id)
        adapter = KimiCompletionAdapter(self.engine, root=self.root / "inference")

        route, _ = adapter.complete(
            messages=[{"role": "user", "content": "选择主硬件单元。"}],
            max_tokens=1024,
            binding=binding,
            conversation_id="analysis-1",
            platform_run_id=run_id,
        )
        opportunity, _ = adapter.complete(
            messages=[{"role": "user", "content": "根据 GPU API 判断机会。"}],
            max_tokens=1024,
            binding=binding,
            conversation_id="analysis-1",
            platform_run_id=run_id,
        )
        self.runtime.prepare_task(
            run_id,
            goal="执行 GPU 真机验证",
            mode="device_validation",
            context={"analysis_record_id": "analysis-1", "device_validation_target": {}},
        )
        self.runtime.submit_message(run_id, "继续完成真机验证。")
        run = self._wait(run_id)

        self.assertEqual(route["agent_engine"]["session_id"], binding.session_id)
        self.assertEqual(opportunity["agent_engine"]["session_id"], binding.session_id)
        self.assertEqual(run["engine"]["session_id"], binding.session_id)
        self.assertEqual({item.session_id for item in self.engine.bindings}, {binding.session_id})
        self.assertEqual(len(self.engine.prompts), 3)
        self.assertIn("选择主硬件单元", self.engine.prompts[0])
        self.assertIn("根据 GPU API 判断机会", self.engine.prompts[1])
        self.assertIn("执行 GPU 真机验证", self.engine.prompts[2])

    def test_kimi_run_ignores_legacy_max_turns_field(self) -> None:
        run_id = self._create()
        self.store.mutate(
            run_id,
            event_type="test.advance_sequence",
            event_state="created",
            payload={},
            apply=lambda run: run.update({"current_turn": 3}),
        )

        self.runtime.start(run_id)
        run = self._wait(run_id)

        self.assertEqual(run["current_turn"], 4)
        self.assertEqual(run["state"], "completed")

    def test_timeout_resume_uses_short_session_continuation_instead_of_replaying_task_pack(self) -> None:
        run_id = self._create()
        normal_run_prompt = self.engine.run_prompt
        attempts = 0

        def flaky_run_prompt(
            binding: AgentEngineBinding,
            prompt: str,
            *,
            on_event: Any,
            cancel_requested: Any,
            before_prompt: Any = None,
            after_prompt: Any = None,
        ) -> AgentEngineResult:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                self.engine.prompts.append(prompt)
                on_event({"type": "turn.started", "sequence": 1, "payload": {}})
                raise AgentEngineError("KIMI_PROMPT_TIMEOUT", "模型调用超时。")
            return normal_run_prompt(
                binding,
                prompt,
                on_event=on_event,
                cancel_requested=cancel_requested,
                before_prompt=before_prompt,
                after_prompt=after_prompt,
            )

        self.engine.run_prompt = flaky_run_prompt  # type: ignore[method-assign]
        self.runtime.start(run_id)
        self._wait(run_id, "failed")
        self.runtime.resume(run_id)
        self._wait(run_id, "completed")

        self.assertEqual(len(self.engine.prompts), 2)
        self.assertIn("# 平台任务", self.engine.prompts[0])
        self.assertNotIn("# 平台任务", self.engine.prompts[1])
        self.assertLess(len(self.engine.prompts[1]), 200)
        self.assertIn("继续工作", self.engine.prompts[1])

    def test_external_engine_action_is_durable_and_idempotent(self) -> None:
        run_id = self._create()

        first = self.runtime.execute_external_action(run_id, "platform.echo", {"value": 7})
        second = self.runtime.execute_external_action(run_id, "platform.echo", {"value": 7})

        self.assertEqual(first["status"], "succeeded")
        self.assertEqual(first["input"], {"value": 7})
        self.assertTrue(second["reused_from_checkpoint"])
        self.assertEqual(second["input"], {"value": 7})
        self.assertEqual(self.router.executions, 1)
        events = self.store.events(run_id, after_sequence=0, limit=100)
        self.assertTrue(any(event["event_type"] == "tool.result_reused" for event in events))

    def test_application_flow_checkpoint_reuse_stays_inside_one_root_analysis(self) -> None:
        run_id = self._create()

        def select_analysis(value: dict[str, Any], analysis_id: str) -> None:
            context = value["context"]
            context["focused_task"] = {
                "kind": "application_flow_generation",
                "application_flow_analysis_id": analysis_id,
            }

        self.store.mutate(
            run_id,
            event_type="test.flow_analysis_selected",
            event_state="created",
            payload={"analysis_id": "application-flow:root-a"},
            apply=lambda value: select_analysis(value, "application-flow:root-a"),
        )
        first = self.runtime.execute_external_action(run_id, "application_flow.list_assets", {"limit": 100})
        self.store.mutate(
            run_id,
            event_type="test.flow_analysis_selected",
            event_state="running",
            payload={"analysis_id": "application-flow:root-b"},
            apply=lambda value: select_analysis(value, "application-flow:root-b"),
        )
        second = self.runtime.execute_external_action(run_id, "application_flow.list_assets", {"limit": 100})
        repeated_second = self.runtime.execute_external_action(run_id, "application_flow.list_assets", {"limit": 100})

        self.assertEqual(first["status"], "succeeded")
        self.assertEqual(second["status"], "succeeded")
        self.assertNotIn("reused_from_checkpoint", second)
        self.assertTrue(repeated_second["reused_from_checkpoint"])
        self.assertEqual(self.router.executions, 2)

    def test_time_bounded_checkpoint_result_is_reexecuted_when_router_marks_it_stale(self) -> None:
        run_id = self._create()

        first = self.runtime.execute_external_action(run_id, "device.create_lease", {"bridge_id": "bridge-a"})
        self.router.reuse_allowed = False
        second = self.runtime.execute_external_action(run_id, "device.create_lease", {"bridge_id": "bridge-a"})

        self.assertEqual(first["status"], "succeeded")
        self.assertEqual(second["status"], "succeeded")
        self.assertNotIn("reused_from_checkpoint", second)
        self.assertEqual(self.router.executions, 2)
        events = self.store.events(run_id, after_sequence=0, limit=100)
        self.assertTrue(any(event["event_type"] == "tool.result_stale" for event in events))

    def test_failed_external_action_is_retryable_instead_of_reused(self) -> None:
        run_id = self._create()
        action_input = {"value": 7}
        action_key = self.runtime._run_action_key(self.store.read(run_id), "platform.echo", action_input)

        def record_failure(run: dict[str, Any]) -> None:
            checkpoint = run["checkpoint"]
            checkpoint["completed_action_keys"][action_key] = {
                "action_id": "platform.echo",
                "action_key": action_key,
                "input": action_input,
                "status": "failed",
                "error": {"code": "UI_ACTION_TIMEOUT", "message": "timed out"},
            }

        self.store.mutate(
            run_id,
            event_type="test.action_failed",
            event_state="created",
            payload={},
            apply=record_failure,
        )

        retried = self.runtime.execute_external_action(run_id, "platform.echo", action_input)

        self.assertEqual(retried["status"], "succeeded")
        self.assertNotIn("reused_from_checkpoint", retried)
        self.assertEqual(self.router.executions, 1)

    def test_device_validation_gate_requires_execution_from_recommended_profile_chain(self) -> None:
        def observation(action_id: str, input_value: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
            return {
                "action_id": action_id,
                "status": "succeeded",
                "input": input_value,
                "result": result,
            }

        context = {
            "device_execution_mode": "remote_bridge",
            "device_validation_target": {
                "requested_unit": "gpu",
                "recommended_profile_id": "gpu_vulkan",
                "compatible_profile_ids": ["gpu_vulkan", "gpu_gles3"],
            }
        }
        gles_only = {
            "mode": "device_validation",
            "context": context,
            "checkpoint": {"completed_action_keys": {
                "stage": observation("build.stage_source", {"profile_id": "gpu_gles3"}, {"source_artifact_id": "art_gles_source", "profile_id": "gpu_gles3"}),
                "build": observation("build.create", {"profile_id": "gpu_gles3", "source_artifact_id": "art_gles_source"}, {"status": "succeeded", "build_result_artifact_id": "art_gles_build"}),
                "deploy": observation("device.deploy_artifact", {"build_result_artifact_id": "art_gles_build"}, {"status": "succeeded", "remote_path": "/data/local/tmp/gles.bin"}),
                "execute": observation("device.execute_shell", {"command": "/data/local/tmp/gles.bin"}, {"status": "succeeded", "remote_shell_run_artifact_id": "art_gles_run"}),
            }},
        }
        correction = self.runtime._device_validation_completion_correction(gles_only)

        self.assertIsNotNone(correction)
        self.assertIn("required_profile=gpu_vulkan", correction)
        self.assertIn("gpu_gles3", correction)

        vulkan = json.loads(json.dumps(gles_only))
        vulkan["checkpoint"]["completed_action_keys"].update({
            "vk-stage": observation("build.stage_source", {"profile_id": "gpu_vulkan"}, {"source_artifact_id": "art_vk_source", "profile_id": "gpu_vulkan"}),
            "vk-build": observation("build.create", {"profile_id": "gpu_vulkan", "source_artifact_id": "art_vk_source"}, {"status": "succeeded", "build_result_artifact_id": "art_vk_build"}),
            "vk-deploy": observation("device.deploy_artifact", {"build_result_artifact_id": "art_vk_build"}, {"status": "succeeded", "remote_path": "/data/local/tmp/vulkan.bin"}),
            "vk-execute": observation("device.execute_shell", {"command": "/data/local/tmp/vulkan.bin"}, {"status": "succeeded", "remote_shell_run_artifact_id": "art_vk_run"}),
        })

        self.assertIsNone(self.runtime._device_validation_completion_correction(vulkan))

    def test_device_validation_gate_does_not_force_native_backend_tools_through_mcp(self) -> None:
        run = {
            "mode": "device_validation",
            "context": {
                "device_execution_mode": "local_hdc",
                "device_validation_target": {
                    "requested_unit": "gpu",
                    "recommended_profile_id": "gpu_vulkan",
                    "compatible_profile_ids": ["gpu_vulkan"],
                },
            },
            "checkpoint": {"completed_action_keys": {}},
        }

        self.assertIsNone(self.runtime._device_validation_completion_correction(run))

    def test_external_engine_actions_are_not_cut_off_by_legacy_budget_fields(self) -> None:
        run_id = self._create()

        for value in range(20):
            result = self.runtime.execute_external_action(run_id, "platform.echo", {"value": value})
            self.assertEqual(result["status"], "succeeded")

        self.assertEqual(self.router.executions, 20)
        events = self.store.events(run_id, after_sequence=0, limit=200)
        self.assertFalse(any(event["event_type"] == "tool.action_budget_reserved" for event in events))

    def test_browser_cancel_is_the_authoritative_execution_boundary(self) -> None:
        run_id = self._create()

        cancelled = self.runtime.cancel(run_id)

        self.assertEqual(cancelled["state"], "cancelled")
        with self.assertRaisesRegex(Exception, "已结束或已取消"):
            self.runtime.execute_external_action(run_id, "platform.echo", {"value": 1})
        self.assertEqual(self.router.executions, 0)

    def test_ui_tool_waits_for_real_browser_result_in_same_engine_call(self) -> None:
        run_id = self._create()
        holder: dict[str, Any] = {}

        thread = threading.Thread(
            target=lambda: holder.update(self.runtime.execute_external_action(run_id, "ui.select", {"value": "hotspot-1"})),
            daemon=True,
        )
        thread.start()
        deadline = time.monotonic() + 2
        pending: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            values = self.store.read(run_id).get("pending_ui_actions") or []
            if values:
                pending = values[0]
                break
            time.sleep(0.01)
        self.assertIsNotNone(pending)

        self.runtime.submit_ui_result(run_id, str(pending["request_id"]), {"selected": "hotspot-1"})
        thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(holder["status"], "succeeded")
        self.assertEqual(holder["result"]["selected"], "hotspot-1")
        self.assertEqual(self.store.read(run_id)["pending_ui_actions"], [])
        events = self.store.events(run_id, after_sequence=0, limit=100)
        self.assertTrue(any(event["event_type"] == "ui.action_requested" for event in events))
        self.assertTrue(any(event["event_type"] == "ui.action_result" for event in events))

    def test_internal_tool_token_is_run_scoped_and_restart_stable(self) -> None:
        manager = InternalToolTokenManager(self.root)
        token = manager.issue("run-a")

        self.assertTrue(manager.verify("run-a", token))
        self.assertFalse(manager.verify("run-b", token))
        self.assertEqual(InternalToolTokenManager(self.root).issue("run-a"), token)

    def test_kimi_usage_projection_reports_cache_and_context(self) -> None:
        usage = openai_token_usage({
            "inputOther": 25,
            "inputCacheRead": 75,
            "inputCacheCreation": 10,
            "output": 12,
            "contextTokens": 110,
            "maxContextTokens": 4096,
        })

        self.assertEqual(usage["prompt_tokens"], 110)
        self.assertEqual(usage["completion_tokens"], 12)
        self.assertEqual(usage["prompt_tokens_details"]["cached_tokens"], 75)
        self.assertEqual(usage["context_limit"], 4096)


if __name__ == "__main__":
    unittest.main()
