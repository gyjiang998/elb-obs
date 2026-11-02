from __future__ import annotations

import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from fastapi.testclient import TestClient

from backend import app as platform_app
from backend.agent_cli_adapters import AgentCliAdapter
from backend.agent_engine import AgentEngineBinding, AgentEngineResult, InternalToolTokenManager
from backend.analysis_agent_session import AnalysisAgentSessionRegistry
from backend.agent_runtime import AgentRunStore, AgentRuntime, CredentialLeaseStore
from backend.core import JsonRegistry
from backend.device_gateway import DeviceGateway
from backend.kimi_agent_runtime import KimiEngineAgentRuntime
from backend.kimi_cli_terminal import AgentCliTerminalManager, TerminalChunk
from backend.kimi_server_engine import KimiServerError


class _NoopRouter:
    """The no-credential API path must not invoke a tool executor."""

    def owner(self, action_id: str) -> str:
        del action_id
        return "platform"

    def validate(self, action_id: str, input_value: dict[str, object], run: dict[str, object]) -> None:
        del action_id, input_value, run

    def execute(self, action_id: str, input_value: dict[str, object], run: dict[str, object]) -> dict[str, object]:
        del action_id, input_value, run
        raise AssertionError("A Run without a model credential must not execute an action.")

    def cancel(self, run: dict[str, object]) -> None:
        del run


class _ApiSessionEngine:
    engine_id = "api-session-engine"

    def __init__(self) -> None:
        self.session_ids: list[str] = []

    def ensure_session(self, *, run_id: str, workspace_path: Path, title: str, metadata: dict[str, Any], existing: dict[str, Any] | None = None) -> AgentEngineBinding:
        del title, metadata
        prior = existing or {}
        session_id = str(prior.get("session_id") or f"session-{run_id}")
        self.session_ids.append(session_id)
        return AgentEngineBinding(self.engine_id, "test", str(prior.get("workspace_id") or f"workspace-{run_id}"), session_id, str(workspace_path))

    def run_prompt(self, binding: AgentEngineBinding, prompt: str, *, on_event: Any, cancel_requested: Any) -> AgentEngineResult:
        del binding, prompt, on_event
        if cancel_requested():
            return AgentEngineResult("", "cancelled", "", "", {}, 0)
        return AgentEngineResult("prompt-api", "completed", "完成", "", {}, 1)

    def cancel(self, binding: AgentEngineBinding, prompt_id: str | None = None) -> None:
        del binding, prompt_id


class _FakeCliTerminal:
    def __init__(self, run_id: str, terminal_session_id: str = "terminal-page-session") -> None:
        self.run_id = run_id
        self.terminal_session_id = terminal_session_id
        self.adapter_id = "kimi"
        self.output_chunks: list[TerminalChunk] = []
        self.commands: list[tuple[str, str]] = []
        self.last_wait_seconds: float | None = None

    def events(self, after_sequence: int, *, wait_seconds: float = 0.0) -> tuple[list[TerminalChunk], bool]:
        self.last_wait_seconds = wait_seconds
        return [chunk for chunk in self.output_chunks if chunk.sequence > after_sequence], False

    async def events_async(self, after_sequence: int, *, wait_seconds: float = 0.0) -> tuple[list[TerminalChunk], bool]:
        return self.events(after_sequence, wait_seconds=wait_seconds)

    def public(self) -> dict[str, object]:
        latest_sequence = self.output_chunks[-1].sequence if self.output_chunks else 0
        return {
            "schema_version": "kimi_cli_terminal/v1",
            "terminal_session_id": self.terminal_session_id,
            "run_id": self.run_id,
            "kimi_session_id": f"session-{self.run_id}",
            "state": "running",
            "pid": 123,
            "exit_code": None,
            "cols": 100,
            "rows": 32,
            "created_at": "2026-08-20T00:00:00Z",
            "last_activity_at": "2026-08-20T00:00:00Z",
            "earliest_sequence": 0,
            "latest_sequence": latest_sequence,
            "transport": "pty",
        }

    def send_command(self, command: str, *, command_id: str = "") -> bool:
        self.commands.append((command, command_id))
        return True


class AgentRuntimeApiTests(unittest.TestCase):
    """Exercise the public durable-Run API without mutating workspace state."""

    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        root = Path(self.tempdir.name)

        self.previous_runtime = platform_app.agent_runtime
        self.previous_store = platform_app.agent_run_store
        self.previous_leases = platform_app.agent_credential_leases
        self.previous_idempotency = platform_app.agent_run_idempotency
        self.previous_device_gateway = platform_app.device_gateway
        self.previous_secret_response_idempotency = platform_app._secret_response_idempotency
        self.previous_analysis_agent_sessions = platform_app.analysis_agent_sessions

        store = AgentRunStore(root / "runs")
        leases = CredentialLeaseStore(default_ttl_seconds=60)
        platform_app.agent_run_store = store
        platform_app.agent_credential_leases = leases
        platform_app.agent_runtime = AgentRuntime(
            store=store,
            leases=leases,
            model_gateway=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("model must not be called")),
            action_router=_NoopRouter(),
            system_prompt=lambda _run: "只返回严格 JSON。",
        )
        platform_app.agent_run_idempotency = JsonRegistry(
            root / "idempotency.json",
            {"schema_version": "agent_run_idempotency/v1", "entries": {}},
        )
        platform_app.device_gateway = DeviceGateway(
            workspace_root=root,
            state_root=root / "device-gateway-state",
        )
        platform_app._secret_response_idempotency = {}
        platform_app.analysis_agent_sessions = AnalysisAgentSessionRegistry(root / "analysis-sessions.json")
        self.addCleanup(self._restore_globals)
        self.client = TestClient(platform_app.app)

    def _restore_globals(self) -> None:
        platform_app.agent_runtime = self.previous_runtime
        platform_app.agent_run_store = self.previous_store
        platform_app.agent_credential_leases = self.previous_leases
        platform_app.agent_run_idempotency = self.previous_idempotency
        platform_app.device_gateway = self.previous_device_gateway
        platform_app._secret_response_idempotency = self.previous_secret_response_idempotency
        platform_app.analysis_agent_sessions = self.previous_analysis_agent_sessions

    @staticmethod
    def _payload() -> dict[str, object]:
        return {
            "goal": "验证后端 Agent Run API",
            "initial_input": "创建一条无需模型凭证的可恢复任务。",
            "mode": "page_agent",
            "context": {"test": True},
            "budget": {
                "max_turns": 1,
                "max_actions_per_turn": 1,
                "max_output_tokens": 1024,
                "context_window_tokens": 8192,
            },
        }

    def test_create_replay_read_and_export_agent_run(self) -> None:
        headers = {"Idempotency-Key": "agent-runtime-api-test-0001"}
        created = self.client.post("/v1/agent-runs", headers=headers, json=self._payload())

        self.assertEqual(created.status_code, 200, created.text)
        run_id = created.json()["run"]["run_id"]
        self.assertEqual(created.json()["run"]["state"], "created")
        self.assertGreaterEqual(created.json()["run"]["event_cursor"], 1)

        replayed = self.client.post("/v1/agent-runs", headers=headers, json=self._payload())
        self.assertEqual(replayed.status_code, 200, replayed.text)
        self.assertEqual(replayed.json()["run"]["run_id"], run_id)

        fetched = self.client.get(f"/v1/agent-runs/{run_id}")
        self.assertEqual(fetched.status_code, 200, fetched.text)
        self.assertEqual(fetched.json()["run"]["run_id"], run_id)

        trace = self.client.get(f"/v1/agent-runs/{run_id}/execution-trace")
        self.assertEqual(trace.status_code, 200, trace.text)
        self.assertIn('"event_type":"run.created"', trace.text)

    def test_same_analysis_reuses_run_and_engine_session_across_task_modes(self) -> None:
        engine = _ApiSessionEngine()
        platform_app.agent_runtime = KimiEngineAgentRuntime(
            store=platform_app.agent_run_store,
            leases=platform_app.agent_credential_leases,
            model_gateway=lambda *_args, **_kwargs: {},
            action_router=_NoopRouter(),
            system_prompt=lambda _run: "unused",
            engine=engine,
            token_manager=InternalToolTokenManager(Path(self.tempdir.name)),
            platform_api_base="http://127.0.0.1:8006",
            app_root=Path(__file__).resolve().parents[2],
        )
        analysis_id = "analysis-shared-session"
        first_payload = self._payload()
        first_payload["analysis_id"] = analysis_id
        first_payload["context"] = {"analysis_record_id": analysis_id}

        first = self.client.post(
            "/v1/agent-runs",
            headers={"Idempotency-Key": "analysis-session-first"},
            json=first_payload,
        )
        self.assertEqual(first.status_code, 200, first.text)
        run_id = first.json()["run"]["run_id"]
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and platform_app.agent_run_store.read(run_id)["state"] != "completed":
            time.sleep(0.01)

        second_payload = self._payload()
        second_payload.update({
            "analysis_id": analysis_id,
            "mode": "device_validation",
            "goal": "继续真机验证",
            "initial_input": "进入设备阶段",
            "context": {"analysis_record_id": analysis_id, "device_validation_facts": {}},
        })
        second = self.client.post(
            "/v1/agent-runs",
            headers={"Idempotency-Key": "analysis-session-second"},
            json=second_payload,
        )

        self.assertEqual(second.status_code, 200, second.text)
        self.assertEqual(second.json()["run"]["run_id"], run_id)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and platform_app.agent_run_store.read(run_id)["state"] != "completed":
            time.sleep(0.01)
        run = platform_app.agent_run_store.read(run_id)
        self.assertEqual(run["mode"], "device_validation")
        self.assertEqual(len(set(engine.session_ids)), 1)

    def test_page_cli_output_poll_returns_ordered_recoverable_chunks(self) -> None:
        terminal = _FakeCliTerminal("run-output")
        terminal.output_chunks = [
            TerminalChunk(1, b"first", "2026-08-20T00:00:01Z"),
            TerminalChunk(2, b"second", "2026-08-20T00:00:02Z"),
        ]

        with patch.object(platform_app.kimi_cli_terminals, "get", return_value=terminal):
            response = self.client.get(
                f"/v1/kimi-cli/sessions/{terminal.terminal_session_id}/output",
                params={"after": 1, "wait_ms": 0},
            )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["schema_version"], "kimi_cli_output_poll/v1")
        self.assertEqual([chunk["sequence"] for chunk in payload["chunks"]], [2])
        self.assertEqual(payload["chunks"][0]["data_base64"], "c2Vjb25k")
        self.assertFalse(payload["truncated"])
        self.assertFalse(payload["more_available"])
        self.assertEqual(payload["terminal"]["latest_sequence"], 2)

    def test_page_cli_output_poll_rejects_invalid_wait(self) -> None:
        response = self.client.get(
            "/v1/kimi-cli/sessions/terminal-page-session/output",
            params={"after": 0, "wait_ms": 15_001},
        )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["error"]["code"], "KIMI_CLI_OUTPUT_WAIT_INVALID")

    def test_page_cli_output_poll_caps_legacy_long_wait(self) -> None:
        terminal = _FakeCliTerminal("run-legacy-long-poll")

        with (
            patch.object(platform_app.kimi_cli_terminals, "get", return_value=terminal),
            patch.object(platform_app, "ASYNC_POLL_MAX_HOLD_SECONDS", 0.25),
        ):
            response = self.client.get(
                f"/v1/kimi-cli/sessions/{terminal.terminal_session_id}/output",
                params={"after": 0, "wait_ms": 10_000},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(terminal.last_wait_seconds, 0.25)

    def test_agent_run_event_poll_returns_cursor_ordered_events(self) -> None:
        run = platform_app.agent_run_store.create(
            project_id="local-default",
            goal="事件轮询",
            initial_input="等待",
            mode="page_agent",
            context={},
            budget={"max_turns": 1, "max_actions_per_turn": 1, "max_output_tokens": 1024, "context_window_tokens": 8192},
            action_catalog_version="test",
            skill_versions=[],
        )
        run_id = str(run["run_id"])
        platform_app.agent_run_store.append_event(run_id, "test.event", "running", {"value": 7})

        response = self.client.get(
            f"/v1/agent-runs/{run_id}/events/poll",
            params={"after": 1, "wait_ms": 0},
        )

        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["schema_version"], "agent_run_event_poll/v1")
        self.assertEqual([event["event_type"] for event in payload["events"]], ["test.event"])
        self.assertEqual(payload["latest_sequence"], payload["events"][-1]["sequence"])

    def test_legacy_agent_event_stream_has_a_bounded_lifetime(self) -> None:
        run = platform_app.agent_run_store.create(
            project_id="local-default",
            goal="兼容事件流",
            initial_input="等待旧页面重连",
            mode="page_agent",
            context={},
            budget={"max_turns": 1, "max_actions_per_turn": 1, "max_output_tokens": 1024, "context_window_tokens": 8192},
            action_catalog_version="test",
            skill_versions=[],
        )
        run_id = str(run["run_id"])

        started = time.monotonic()
        with patch.object(platform_app, "COMPATIBILITY_SSE_MAX_SECONDS", 0.03):
            response = self.client.get(f"/v1/agent-runs/{run_id}/events")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertIn("event: run.created", response.text)

    def test_page_cli_command_refreshes_the_active_adapter_projection(self) -> None:
        engine = _ApiSessionEngine()
        platform_app.agent_runtime = KimiEngineAgentRuntime(
            store=platform_app.agent_run_store,
            leases=platform_app.agent_credential_leases,
            model_gateway=lambda *_args, **_kwargs: {},
            action_router=_NoopRouter(),
            system_prompt=lambda _run: "unused",
            engine=engine,
            token_manager=InternalToolTokenManager(Path(self.tempdir.name)),
            platform_api_base="http://127.0.0.1:8006",
            app_root=Path(__file__).resolve().parents[2],
        )
        created = platform_app._new_kimi_cli_backing_run(platform_app.KimiCliSessionCreateRequest(
            page_session_id="page_12345678-refresh",
            goal="刷新 Adapter Skill 投影",
            context={"page_context": "{}"},
        ))
        terminal = _FakeCliTerminal(str(created["run_id"]))
        terminal.adapter_id = "team-agent"

        with (
            patch.object(platform_app.kimi_cli_terminals, "get", return_value=terminal),
            patch.object(platform_app, "_refresh_agent_cli_projected_workspace") as refresh,
        ):
            response = self.client.post(
                f"/v1/agent-cli/sessions/{terminal.terminal_session_id}/commands",
                json={"command": "运行语义路由", "command_id": "command-refresh-1"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        refresh.assert_called_once_with(terminal)
        self.assertEqual(terminal.commands, [("运行语义路由", "command-refresh-1")])

    def test_page_cli_persists_only_matching_transport_receipts(self) -> None:
        created = platform_app.agent_run_store.create(
            project_id="local-default", goal="提交诊断", initial_input="计划", mode="page_agent",
            context={}, budget={"max_turns": 1, "max_actions_per_turn": 1, "max_output_tokens": 1024, "context_window_tokens": 8192},
            action_catalog_version="test", skill_versions=[],
        )
        terminal = _FakeCliTerminal(str(created["run_id"]))
        state = terminal.public()
        for receipt_id in ("command-own", "command-concurrent"):
            receipt = {"schema_version": "agent_cli_submission_transport/v1", "command_id": receipt_id,
                       "strategy_version": "pty_output_settle/v1", "delivery_state": "delivered",
                       "actual_wait_ms": 451.0, "wait_reason": "output_settled", "output_observed": True,
                       "submit_key": "cr", "submit_key_count": 1}
            with self.subTest(receipt_id=receipt_id), \
                 patch.object(platform_app.kimi_cli_terminals, "get", return_value=terminal), \
                 patch.object(terminal, "public", return_value={**state, "last_submission": receipt}), \
                 patch.object(platform_app, "_refresh_agent_cli_projected_workspace"):
                response = self.client.post(
                    f"/v1/agent-cli/sessions/{terminal.terminal_session_id}/commands",
                    json={"command": "运行语义路由", "command_id": "command-own"},
                )
                self.assertEqual(response.status_code, 200, response.text)
                events = platform_app.agent_run_store.events(terminal.run_id)
                submitted = [event for event in events if event["event_type"] == "terminal.command_submitted"][-1]
                if receipt_id == "command-own":
                    self.assertEqual(submitted["payload"]["submission_transport"], receipt)
                else:
                    self.assertNotIn("submission_transport", submitted["payload"])

    def test_page_cli_stale_native_resume_falls_back_to_fresh_session(self) -> None:
        root = Path(self.tempdir.name)
        binary = root / "resume-sensitive-agent"
        binary.write_text(
            "#!/bin/sh\n"
            "if [ \"${1:-}\" = \"--continue\" ]; then\n"
            "  printf 'NO_CONVERSATION_TO_RESUME\\r\\n'\n"
            "  exit 23\n"
            "fi\n"
            "printf 'FRESH_SESSION_READY\\r\\n'\n"
            "while :; do sleep 1; done\n",
            encoding="utf-8",
        )
        binary.chmod(0o755)
        adapter = AgentCliAdapter(
            adapter_id="test-agent",
            display_name="Test Agent",
            description="Resume fallback test adapter.",
            executable=str(binary),
            resume_args=("--continue",),
            session_strategy="workspace_continue",
            skill_roots=(".agents/skills",),
        )
        engine = _ApiSessionEngine()
        platform_app.agent_runtime = KimiEngineAgentRuntime(
            store=platform_app.agent_run_store,
            leases=platform_app.agent_credential_leases,
            model_gateway=lambda *_args, **_kwargs: {},
            action_router=_NoopRouter(),
            system_prompt=lambda _run: "unused",
            engine=engine,
            token_manager=InternalToolTokenManager(root),
            platform_api_base="http://127.0.0.1:8006",
            app_root=Path(__file__).resolve().parents[2],
        )
        terminals = AgentCliTerminalManager(
            state_root=root / "resume-terminal-state",
            on_exit=platform_app._record_agent_cli_exit,
        )
        page_session_id = "page_resume-fallback"
        request_body = {
            "adapter_id": "test-agent",
            "page_session_id": page_session_id,
            "goal": "验证历史会话自动降级",
            "context": {"page_context": "{}"},
            "cols": 80,
            "rows": 24,
        }

        try:
            with (
                patch.object(platform_app.agent_cli_adapters, "get", return_value=adapter),
                patch.object(platform_app, "agent_cli_terminals", terminals),
                patch.object(platform_app, "kimi_cli_terminals", terminals),
            ):
                first = self.client.post("/v1/agent-cli/sessions", json=request_body)
                self.assertEqual(first.status_code, 200, first.text)
                run_id = first.json()["run"]["run_id"]
                first_terminal_id = first.json()["terminal"]["terminal_session_id"]
                terminals.stop(first_terminal_id)
                deadline = time.monotonic() + 2
                while terminals.get(first_terminal_id).public()["state"] == "running" and time.monotonic() < deadline:
                    time.sleep(0.01)

                resumed = self.client.post(
                    "/v1/agent-cli/sessions",
                    json={**request_body, "run_id": run_id},
                )

                self.assertEqual(resumed.status_code, 200, resumed.text)
                resumed_payload = resumed.json()
                self.assertIsNotNone(resumed_payload["recovery"], resumed_payload)
                self.assertEqual(resumed_payload["recovery"]["status"], "fresh_session_started")
                replacement = resumed_payload["terminal"]
                self.assertEqual(replacement["state"], "running")
                self.assertNotEqual(replacement["terminal_session_id"], first_terminal_id)
                events = platform_app.agent_run_store.events(run_id, limit=500)
                self.assertIn("terminal.session_resume_fallback", {event["event_type"] for event in events})
                current = platform_app.agent_run_store.read(run_id)
                self.assertEqual(current["state"], "running")
                self.assertEqual(
                    current["engine"]["terminal_transport"]["terminal_session_id"],
                    replacement["terminal_session_id"],
                )
        finally:
            terminals.close()

    def test_page_cli_backend_restart_recovers_same_run_without_client_run_id(self) -> None:
        root = Path(self.tempdir.name)
        binary = root / "restart-recoverable-agent"
        binary.write_text("#!/bin/sh\nprintf 'READY\\r\\n'\nwhile :; do sleep 1; done\n", encoding="utf-8")
        binary.chmod(0o755)
        adapter = AgentCliAdapter(
            adapter_id="test-agent",
            display_name="Test Agent",
            description="Backend restart recovery adapter.",
            executable=str(binary),
            session_strategy="stateless",
            skill_roots=(".agents/skills",),
        )
        platform_app.agent_runtime = KimiEngineAgentRuntime(
            store=platform_app.agent_run_store,
            leases=platform_app.agent_credential_leases,
            model_gateway=lambda *_args, **_kwargs: {},
            action_router=_NoopRouter(),
            system_prompt=lambda _run: "unused",
            engine=_ApiSessionEngine(),
            token_manager=InternalToolTokenManager(root),
            platform_api_base="http://127.0.0.1:8006",
            app_root=Path(__file__).resolve().parents[2],
        )
        page_session_id = "page_restart-recovery"
        request_body = {
            "adapter_id": "test-agent",
            "page_session_id": page_session_id,
            "goal": "升级后继续页面会话",
            "context": {"page_context": "{}"},
            "cols": 80,
            "rows": 24,
        }
        first_terminals = AgentCliTerminalManager(
            state_root=root / "first-terminal-process",
            on_exit=platform_app._record_agent_cli_exit,
        )
        second_terminals = AgentCliTerminalManager(
            state_root=root / "second-terminal-process",
            on_exit=platform_app._record_agent_cli_exit,
        )

        try:
            with (
                patch.object(platform_app.agent_cli_adapters, "get", return_value=adapter),
                patch.object(platform_app, "agent_cli_terminals", first_terminals),
                patch.object(platform_app, "kimi_cli_terminals", first_terminals),
            ):
                first = self.client.post("/v1/agent-cli/sessions", json=request_body)
            self.assertEqual(first.status_code, 200, first.text)
            first_payload = first.json()
            run_id = first_payload["run"]["run_id"]
            first_terminal_id = first_payload["terminal"]["terminal_session_id"]

            first_terminals.close()

            with (
                patch.object(platform_app.agent_cli_adapters, "get", return_value=adapter),
                patch.object(platform_app, "agent_cli_terminals", second_terminals),
                patch.object(platform_app, "kimi_cli_terminals", second_terminals),
            ):
                recovered = self.client.post("/v1/agent-cli/sessions", json=request_body)

            self.assertEqual(recovered.status_code, 200, recovered.text)
            recovered_payload = recovered.json()
            self.assertEqual(recovered_payload["run"]["run_id"], run_id)
            self.assertNotEqual(recovered_payload["terminal"]["terminal_session_id"], first_terminal_id)
            self.assertEqual(recovered_payload["recovery"]["status"], "durable_run_recovered")
            events = platform_app.agent_run_store.events(run_id, limit=500)
            self.assertIn("page.session_recovered", {event["event_type"] for event in events})
        finally:
            first_terminals.close()
            second_terminals.close()

    def test_page_cli_session_switches_analysis_context_without_switching_run(self) -> None:
        engine = _ApiSessionEngine()
        platform_app.agent_runtime = KimiEngineAgentRuntime(
            store=platform_app.agent_run_store,
            leases=platform_app.agent_credential_leases,
            model_gateway=lambda *_args, **_kwargs: {},
            action_router=_NoopRouter(),
            system_prompt=lambda _run: "unused",
            engine=engine,
            token_manager=InternalToolTokenManager(Path(self.tempdir.name)),
            platform_api_base="http://127.0.0.1:8006",
            app_root=Path(__file__).resolve().parents[2],
        )
        page_session_id = "page_12345678-abcd"
        created = platform_app._new_kimi_cli_backing_run(platform_app.KimiCliSessionCreateRequest(
            page_session_id=page_session_id,
            analysis_id="analysis-page-a",
            goal="页面内持续分析",
            context={"page_context": "{}"},
        ))
        run_id = str(created["run_id"])
        terminal = _FakeCliTerminal(run_id)

        with patch.object(platform_app.kimi_cli_terminals, "get", return_value=terminal):
            matched_owner = self.client.get(
                f"/v1/kimi-cli/sessions/{terminal.terminal_session_id}",
                params={"page_session_id": page_session_id},
            )
            rejected_owner = self.client.get(
                f"/v1/kimi-cli/sessions/{terminal.terminal_session_id}",
                params={"page_session_id": "page_87654321-other"},
            )
            switched = self.client.post(
                f"/v1/kimi-cli/sessions/{terminal.terminal_session_id}/context",
                json={
                    "page_session_id": page_session_id,
                    "analysis_id": "analysis-page-b",
                    "goal": "切换到页面内另一条分析记录",
                    "mode": "page_agent",
                    "context": {"page_context": "{}"},
                },
            )

        self.assertEqual(matched_owner.status_code, 200, matched_owner.text)
        self.assertEqual(rejected_owner.status_code, 409, rejected_owner.text)
        self.assertEqual(rejected_owner.json()["error"]["code"], "PAGE_AGENT_SESSION_MISMATCH")
        self.assertEqual(switched.status_code, 200, switched.text)
        self.assertEqual(switched.json()["run"]["run_id"], run_id)
        self.assertEqual(switched.json()["task"]["page_session_id"], page_session_id)
        run = platform_app.agent_run_store.read(run_id)
        self.assertEqual(run["context"]["page_session_id"], page_session_id)
        self.assertEqual(run["context"]["analysis_record_id"], "analysis-page-b")
        self.assertIsNone(platform_app.analysis_agent_sessions.resolve("analysis-page-a"))
        self.assertIsNone(platform_app.analysis_agent_sessions.resolve("analysis-page-b"))

    def test_page_cli_session_rejects_another_page_owner(self) -> None:
        run = platform_app.agent_runtime.create(
            project_id="local-default",
            goal="页面 A",
            initial_input="等待输入",
            mode="page_agent",
            context={"page_session_id": "page_owner-a"},
            budget={"max_turns": 1, "max_actions_per_turn": 1, "max_output_tokens": 1024, "context_window_tokens": 8192},
            action_catalog_version="test",
            skill_versions=[],
        )

        stored_run = platform_app.agent_run_store.read(str(run["run_id"]))
        with self.assertRaises(platform_app.PlatformError) as raised:
            platform_app._require_matching_page_session(stored_run, "page_owner-b")
        self.assertEqual(raised.exception.code, "PAGE_AGENT_SESSION_MISMATCH")

    def test_unscoped_page_run_can_bind_to_analysis_created_by_paste_action(self) -> None:
        created = self.client.post(
            "/v1/agent-runs",
            headers={"Idempotency-Key": "late-bind-create"},
            json=self._payload(),
        )
        self.assertEqual(created.status_code, 200, created.text)
        run_id = created.json()["run"]["run_id"]

        bound = self.client.post(
            f"/v1/agent-runs/{run_id}/analysis-binding",
            headers={"Idempotency-Key": "late-bind-analysis"},
            json={"analysis_id": "analysis-created-after-paste"},
        )

        self.assertEqual(bound.status_code, 200, bound.text)
        self.assertEqual(bound.json()["run"]["run_id"], run_id)
        self.assertEqual(
            platform_app.agent_run_store.read(run_id)["context"]["analysis_record_id"],
            "analysis-created-after-paste",
        )
        resolved = self.client.get("/v1/analysis-agent-sessions/analysis-created-after-paste")
        self.assertEqual(resolved.status_code, 200, resolved.text)
        self.assertEqual(resolved.json()["run"]["run_id"], run_id)

    def test_structured_stage_uses_isolated_session_while_page_agent_waits_for_ui(self) -> None:
        engine = _ApiSessionEngine()
        platform_app.agent_runtime = KimiEngineAgentRuntime(
            store=platform_app.agent_run_store,
            leases=platform_app.agent_credential_leases,
            model_gateway=lambda *_args, **_kwargs: {},
            action_router=_NoopRouter(),
            system_prompt=lambda _run: "unused",
            engine=engine,
            token_manager=InternalToolTokenManager(Path(self.tempdir.name)),
            platform_api_base="http://127.0.0.1:8006",
            app_root=Path(__file__).resolve().parents[2],
        )
        analysis_id = "analysis-active-page-prompt"
        payload = self._payload()
        payload["analysis_id"] = analysis_id
        payload["context"] = {"analysis_record_id": analysis_id}
        with patch.object(
            platform_app.agent_runtime,
            "start",
            side_effect=lambda run_id: platform_app.agent_run_store.public(platform_app.agent_run_store.read(run_id)),
        ):
            created = self.client.post(
                "/v1/agent-runs",
                headers={"Idempotency-Key": "structured-stage-active-parent"},
                json=payload,
            )
        self.assertEqual(created.status_code, 200, created.text)
        run_id = created.json()["run"]["run_id"]
        platform_app.agent_run_store.mutate(
            run_id,
            event_type="test.parent_prompt_active",
            event_state="waiting_for_ui",
            payload={},
            apply=lambda run: run.update({
                "state": "waiting_for_ui",
                "active_engine_prompt": {"turn": 1, "phase": "full_execution"},
            }),
        )
        request = platform_app.ModelRelayChatCompletionRequest(
            base_url="https://api.kimi.com/coding/v1",
            api_key="ephemeral-test-key",
            model="kimi-for-coding",
            messages=[{"role": "user", "content": "执行只读路由。"}],
            max_tokens=1024,
            analysis_id=analysis_id,
            operation="hardware_semantic_routing.model",
            request_id="route-request",
        )

        owner_run_id, binding = platform_app._structured_session_binding(request)

        self.assertEqual(owner_run_id, run_id)
        self.assertIsNone(binding)
        self.assertEqual(platform_app.analysis_agent_sessions.resolve(analysis_id), run_id)

    def test_structured_stage_hard_disables_platform_tools(self) -> None:
        created = self.client.post(
            "/v1/agent-runs",
            headers={"Idempotency-Key": "structured-read-only-run"},
            json=self._payload(),
        )
        self.assertEqual(created.status_code, 200, created.text)
        run_id = created.json()["run"]["run_id"]
        platform_app.agent_run_store.mutate(
            run_id,
            event_type="test.structured_policy",
            event_state="created",
            payload={},
            apply=lambda run: run.update({"active_prompt_policy": {"kind": "structured_read_only", "request_id": "request-1"}}),
        )
        token = platform_app.kimi_internal_tokens.issue(run_id)

        response = self.client.post(
            f"/v1/internal/agent-runs/{run_id}/actions/platform.echo",
            headers={"Authorization": f"Bearer {token}"},
            json={"input": {"value": 1}},
        )

        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "AGENT_TOOL_DISABLED_FOR_STRUCTURED_STAGE")

    def test_credential_contract_accepts_current_and_pre_migration_field_names(self) -> None:
        current = platform_app.AgentRunCredentialRequest.model_validate({
            "base_url": "https://api.kimi.com/coding/v1",
            "api_key": "not-persisted",
            "model": "kimi-for-coding",
        })
        previous = platform_app.AgentRunCredentialRequest.model_validate({
            "baseUrl": "https://api.kimi.com/coding/v1",
            "apiKey": "not-persisted",
            "model": "kimi-for-coding",
            "contextWindowTokens": 262144,
        })

        self.assertEqual(current.base_url, previous.base_url)
        self.assertEqual(current.api_key, previous.api_key)
        self.assertEqual(previous.context_window_tokens, 262144)

        payload = self._payload()
        payload["credential"] = {
            "baseUrl": "https://api.kimi.com/coding/v1",
            "apiKey": "not-persisted",
            "model": "kimi-for-coding",
        }
        with patch.object(
            platform_app.agent_runtime,
            "start",
            side_effect=lambda run_id: platform_app.agent_run_store.public(platform_app.agent_run_store.read(run_id)),
        ):
            response = self.client.post(
                "/v1/agent-runs",
                headers={"Idempotency-Key": "agent-runtime-camel-credential-0001"},
                json=payload,
            )
        self.assertEqual(response.status_code, 200, response.text)

    def test_kimi_browser_credential_endpoint_never_echoes_secret(self) -> None:
        secret = "sk-browser-lease-only"
        with (
            patch.object(platform_app, "KimiEngineAgentRuntime", type(platform_app.agent_runtime)),
            patch.object(
                platform_app.kimi_agent_engine,
                "configure_ephemeral_model",
                return_value={
                    "status": "ready",
                    "credential_mode": "browser_ephemeral",
                    "model": "env/kimi-for-coding",
                    "expires_in_seconds": 1800,
                },
            ) as configure,
        ):
            response = self.client.post(
                "/v1/agent-engine/model-credential",
                json={
                    "base_url": "https://models.example.internal/openai/v1",
                    "api_key": secret,
                    "model": "kimi-for-coding",
                    "context_window_tokens": 262144,
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(secret, response.text)
        self.assertEqual(configure.call_args.kwargs["api_key"], secret)
        self.assertEqual(configure.call_args.kwargs["base_url"], "https://models.example.internal/openai/v1")
        self.assertEqual(configure.call_args.kwargs["context_window_tokens"], 262144)

    def test_kimi_browser_credential_is_committed_after_real_agent_probe(self) -> None:
        probe = AgentEngineResult("prompt-probe", "completed", "OK", "", {}, 1)
        with (
            patch.object(platform_app, "KimiEngineAgentRuntime", type(platform_app.agent_runtime)),
            patch.object(
                platform_app.kimi_agent_engine,
                "configure_ephemeral_model",
                return_value={
                    "status": "ready",
                    "credential_mode": "browser_ephemeral",
                    "configuration_reloaded": True,
                    "provider_type": "openai",
                },
            ),
            patch.object(platform_app.kimi_completion_adapter, "complete", return_value=({}, probe)) as complete,
        ):
            response = self.client.post(
                "/v1/agent-engine/model-credential",
                json={
                    "base_url": "http://models.example.internal/v1",
                    "api_key": "sk-probe",
                    "model": "test-model",
                    "context_window_tokens": 131072,
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["engine"]["model_probe"], "passed")
        self.assertEqual(complete.call_args.kwargs["thinking"], "low")

    def test_browser_model_probe_retries_with_alternate_wire_protocol(self) -> None:
        probe = AgentEngineResult("prompt-probe", "completed", "OK", "", {}, 1)
        with (
            patch.object(platform_app, "KimiEngineAgentRuntime", type(platform_app.agent_runtime)),
            patch.object(
                platform_app.kimi_agent_engine,
                "configure_ephemeral_model",
                side_effect=[
                    {
                        "status": "ready",
                        "credential_mode": "browser_ephemeral",
                        "configuration_reloaded": True,
                        "provider_type": "openai",
                    },
                    {
                        "status": "ready",
                        "credential_mode": "browser_ephemeral",
                        "configuration_reloaded": True,
                        "provider_type": "kimi",
                    },
                ],
            ) as configure,
            patch.object(
                platform_app.kimi_completion_adapter,
                "complete",
                side_effect=[
                    KimiServerError("KIMI_COMPLETION_FAILED", "上游返回 400。"),
                    ({}, probe),
                ],
            ),
        ):
            response = self.client.post(
                "/v1/agent-engine/model-credential",
                json={
                    "base_url": "http://models.example.internal/v1",
                    "api_key": "sk-protocol-fallback",
                    "model": "test-model",
                    "context_window_tokens": 131072,
                },
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["engine"]["provider_type"], "kimi")
        self.assertEqual(response.json()["engine"]["provider_resolution"], "protocol_fallback")
        self.assertIsNone(configure.call_args_list[0].kwargs["provider_type"])
        self.assertEqual(configure.call_args_list[1].kwargs["provider_type"], "kimi")

    def test_failed_browser_model_probe_rolls_back_ephemeral_credential(self) -> None:
        with (
            patch.object(platform_app, "KimiEngineAgentRuntime", type(platform_app.agent_runtime)),
            patch.object(
                platform_app.kimi_agent_engine,
                "configure_ephemeral_model",
                return_value={
                    "status": "ready",
                    "credential_mode": "browser_ephemeral",
                    "configuration_reloaded": True,
                },
            ),
            patch.object(
                platform_app.kimi_completion_adapter,
                "complete",
                side_effect=KimiServerError("KIMI_COMPLETION_FAILED", "上游返回 400。"),
            ),
            patch.object(
                platform_app.kimi_agent_engine,
                "clear_ephemeral_model",
                return_value={"status": "ready", "credential_mode": "kimi_managed"},
            ) as clear,
        ):
            response = self.client.post(
                "/v1/agent-engine/model-credential",
                json={
                    "base_url": "http://models.example.internal/v1",
                    "api_key": "sk-invalid",
                    "model": "test-model",
                    "context_window_tokens": 131072,
                },
            )

        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(response.json()["error"]["code"], "KIMI_MODEL_PROBE_FAILED")
        self.assertNotIn("sk-invalid", response.text)
        clear.assert_called_once()

    def test_device_validation_context_is_server_side_whitelisted(self) -> None:
        payload = self._payload()
        payload["mode"] = "device_validation"
        payload["context"] = {
            "schema_version": "page_agent_runtime_context/v1",
            "analysis_record_id": "analysis-1",
            "page_context": {
                "target_platform_profile": {
                    "schema_version": "target_platform_profile/v1",
                    "chipset": {"model_id": "kirin_9020"},
                    "operating_system": {"version_id": "openharmony_5"},
                },
                "reasoning_lab": {
                    "code_unit": {"code_preview": "duplicate hotspot source"},
                    "strategy_results": [{"summary": "duplicate strategy summary"}],
                },
            },
            "page_context_compacted": False,
            "device_validation_facts": {"schemaVersion": "device_validation_facts/v1", "actionFacts": []},
            "device_validation_target": {
                "requested_unit": "gpu",
                "recommended_profile_id": "gpu_vulkan",
                "compatible_profile_ids": ["gpu_vulkan", "gpu_gles3", "unknown_profile"],
            },
            "device_execution_mode": "remote_bridge",
            "device_selector": {"bridge_id": "bridge-test"},
            "selected_device_bridge": {"display_name": "must-not-reach-model"},
            "unrelated": "must-not-reach-model",
        }
        response = self.client.post(
            "/v1/agent-runs",
            headers={"Idempotency-Key": "agent-runtime-device-context-0001"},
            json=payload,
        )

        self.assertEqual(response.status_code, 200, response.text)
        run_id = response.json()["run"]["run_id"]
        context = platform_app.agent_run_store.read(run_id)["context"]
        self.assertEqual(context["schema_version"], "device_validation_runtime_context/v1")
        self.assertEqual(context["analysis_record_id"], "analysis-1")
        self.assertEqual(context["target_platform_profile"]["chipset"]["model_id"], "kirin_9020")
        self.assertEqual(context["device_selector"], {"bridge_id": "bridge-test"})
        self.assertEqual(context["device_validation_target"], {
            "requested_unit": "gpu",
            "recommended_profile_id": "gpu_vulkan",
            "compatible_profile_ids": ["gpu_vulkan", "gpu_gles3"],
        })
        self.assertNotIn("page_context", context)
        self.assertNotIn("selected_device_bridge", context)
        self.assertNotIn("unrelated", context)
        self.assertNotIn("duplicate hotspot source", str(context))

    def test_device_validation_system_prompt_excludes_unrelated_page_actions(self) -> None:
        device_prompt = platform_app._runtime_system_prompt({"mode": "device_validation"})
        page_prompt = platform_app._runtime_system_prompt({"mode": "page_agent"})

        self.assertIn("build.stage_source", device_prompt)
        self.assertIn('"source_ref"', device_prompt)
        self.assertIn('<agent-source id="experiment_c"', device_prompt)
        self.assertIn("device.execute_shell", device_prompt)
        self.assertIn("workspace.read_file", device_prompt)
        self.assertNotIn("reasoning.run_hardware_semantic_route", device_prompt)
        self.assertNotIn("trace.upload_raw_htrace", device_prompt)
        self.assertNotIn("workspace.write_file", device_prompt)
        self.assertIn("reasoning.run_hardware_semantic_route", page_prompt)
        self.assertIn("默认自主执行完整链路", page_prompt)
        self.assertIn("reasoning.run_strategy(strategy_id=hardware_kb)", page_prompt)
        self.assertIn("则继续 device.discover/probe", page_prompt)
        self.assertIn("每个 owner=ui 操作都是同步边界", page_prompt)
        for phrase in (
            "Agent Session", "Kimi Session", "当前 Session", "同一 Session", "本 Session",
            "另起模型会话", "新模型会话", "另一模型会话", "代为触发", "主编排器",
        ):
            self.assertNotIn(phrase, page_prompt)
            self.assertNotIn(phrase, device_prompt)

    def test_pairing_response_is_ephemeral_and_bridge_list_is_public_only(self) -> None:
        pair_headers = {"Idempotency-Key": "agent-runtime-pairing-test-0001"}
        paired = self.client.post("/v1/device-bridges/pairing-codes", headers=pair_headers, json={"ttl_seconds": 600})

        self.assertEqual(paired.status_code, 200, paired.text)
        pairing_code = paired.json()["pairing_code"]
        replayed = self.client.post("/v1/device-bridges/pairing-codes", headers=pair_headers, json={"ttl_seconds": 600})
        self.assertEqual(replayed.status_code, 200, replayed.text)
        self.assertEqual(replayed.json()["pairing_code"], pairing_code)

        registered = self.client.post(
            "/v1/device-bridges/register",
            headers={"Idempotency-Key": "agent-runtime-register-test-0001"},
            json={"pairing_code": pairing_code, "display_name": "isolated-bridge", "capabilities": {"hdc": True}},
        )
        self.assertEqual(registered.status_code, 200, registered.text)
        bridge_id = registered.json()["bridge_id"]
        bridge_secret = registered.json()["bridge_secret"]
        self.assertTrue(bridge_secret)

        with self.client.websocket_connect("/v1/device-bridges/ws") as websocket:
            websocket.send_json({
                "type": "bridge.hello",
                "bridge_id": bridge_id,
                "bridge_secret": bridge_secret,
                "protocol_version": "device-bridge-ws/v1",
                "capabilities": {"actions": ["device.discover"]},
            })
            welcome = websocket.receive_json()
            self.assertEqual(welcome["type"], "bridge.welcome")
            self.assertEqual(welcome["bridge_id"], bridge_id)

        bridges = self.client.get("/v1/device-bridges")
        self.assertEqual(bridges.status_code, 200, bridges.text)
        listed = next(item for item in bridges.json()["bridges"] if item["bridge_id"] == bridge_id)
        self.assertNotIn("bridge_secret", listed)

    def test_device_bridge_poll_requires_authentication_for_probe_and_full_connection(self) -> None:
        registered = self.client.post(
            "/v1/device-bridges/register",
            headers={"Idempotency-Key": "agent-runtime-poll-authentication-0001"},
            json={"display_name": "authenticated-poll-bridge", "capabilities": {}},
        )
        self.assertEqual(registered.status_code, 200, registered.text)
        identity = registered.json()
        for probe_only in (True, False):
            payload = {"connection_id": "connection_" + "a" * 32, "capabilities": {}, "probe_only": probe_only}
            rejected = self.client.post("/v1/device-bridges/poll", json=payload,
                headers={"X-Bridge-Id": identity["bridge_id"], "X-Bridge-Secret": "wrong-secret"})
            self.assertEqual(rejected.status_code, 401, rejected.text)
            self.assertEqual(platform_app.device_gateway.list_bridges(project_id=platform_app.PROJECT_ID)[0]["connection_state"], "offline")
        verified = self.client.post("/v1/device-bridges/poll",
            headers={"X-Bridge-Id": identity["bridge_id"], "X-Bridge-Secret": identity["bridge_secret"]},
            json={"connection_id": "connection_" + "a" * 32, "capabilities": {}, "probe_only": True})
        self.assertEqual(verified.status_code, 200, verified.text)
        self.assertTrue(verified.json()["probe_verified"])
        self.assertEqual(platform_app.device_gateway.list_bridges(project_id=platform_app.PROJECT_ID)[0]["connection_state"], "offline")

    def test_device_bridge_registers_without_pairing_code(self) -> None:
        registered = self.client.post(
            "/v1/device-bridges/register",
            headers={"Idempotency-Key": "agent-runtime-zero-touch-register-0001"},
            json={"display_name": "zero-touch-bridge", "capabilities": {"hdc": True}},
        )

        self.assertEqual(registered.status_code, 200, registered.text)
        payload = registered.json()
        self.assertEqual(payload["registration_mode"], "trusted_single_user")
        self.assertTrue(payload["bridge_secret"])

        polled = self.client.post(
            "/v1/device-bridges/poll",
            headers={
                "X-Bridge-Id": payload["bridge_id"],
                "X-Bridge-Secret": payload["bridge_secret"],
            },
            json={
                "connection_id": "connection_" + "d" * 32,
                "capabilities": {
                    "protocol_version": "device-bridge-http-poll/v1",
                    "transport": "http_poll",
                    "actions": ["device.discover"],
                },
                "reconcile_jobs": [],
                "max_messages": 16,
            },
        )
        self.assertEqual(polled.status_code, 200, polled.text)
        self.assertEqual(polled.json()["protocol_version"], "device-bridge-http-poll/v1")
        self.assertEqual(polled.json()["transport"], "http_poll")
        self.assertEqual(polled.json()["messages"], [])

        heartbeat = self.client.post(
            "/v1/device-bridges/events",
            headers={
                "X-Bridge-Id": payload["bridge_id"],
                "X-Bridge-Secret": payload["bridge_secret"],
            },
            json={
                "connection_id": "connection_" + "d" * 32,
                "message": {
                    "type": "bridge.heartbeat",
                    "capabilities": {
                        "protocol_version": "device-bridge-http-poll/v1",
                        "transport": "http_poll",
                        "actions": ["device.discover"],
                    },
                },
            },
        )
        self.assertEqual(heartbeat.status_code, 200, heartbeat.text)
        self.assertTrue(heartbeat.json()["acknowledged"])
        self.assertEqual(heartbeat.json()["event_type"], "bridge.heartbeat")

        stale_result = self.client.post(
            "/v1/device-bridges/events",
            headers={
                "X-Bridge-Id": payload["bridge_id"],
                "X-Bridge-Secret": payload["bridge_secret"],
            },
            json={
                "connection_id": "connection_" + "d" * 32,
                "message": {
                    "type": "job.result",
                    "job_id": "devjob_" + "f" * 24,
                    "request_id": "req_stale_replay",
                    "result": {"status": "succeeded"},
                },
            },
        )
        self.assertEqual(stale_result.status_code, 200, stale_result.text)
        self.assertTrue(stale_result.json()["acknowledged"])
        self.assertEqual(stale_result.json()["event_disposition"], "stale_ignored")
        self.assertEqual(stale_result.json()["reason_code"], "BRIDGE_JOB_RESULT_STALE")

        replayed = self.client.post(
            "/v1/device-bridges/register",
            headers={"Idempotency-Key": "agent-runtime-zero-touch-register-0001"},
            json={"display_name": "zero-touch-bridge", "capabilities": {"hdc": True}},
        )
        self.assertEqual(replayed.status_code, 200, replayed.text)
        self.assertEqual(replayed.json()["bridge_id"], payload["bridge_id"])
        self.assertEqual(replayed.json()["bridge_secret"], payload["bridge_secret"])

        bridges = self.client.get("/v1/device-bridges")
        self.assertEqual(bridges.status_code, 200, bridges.text)
        listed = next(item for item in bridges.json()["bridges"] if item["bridge_id"] == payload["bridge_id"])
        self.assertEqual(listed["registration_mode"], "trusted_single_user")
        self.assertNotIn("bridge_secret", listed)
