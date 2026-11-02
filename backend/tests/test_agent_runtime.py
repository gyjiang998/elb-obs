from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any

from backend.agent_runtime import (
    AgentRunStore,
    AgentRuntime,
    CredentialLeaseStore,
    ModelGatewayError,
    _safe_json,
    parse_agent_decision,
)
from backend.core import PlatformError


def decision(reply: str, *, disposition: str = "complete", actions: list[dict[str, Any]] | None = None) -> str:
    return json.dumps(
        {"reply": reply, "disposition": disposition, "actions": actions or []},
        ensure_ascii=False,
        separators=(",", ":"),
    )


class ScriptedGateway:
    def __init__(self, responses: list[str | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[list[dict[str, str]]] = []

    def __call__(self, lease: Any, messages: list[dict[str, str]], *, max_tokens: int, on_stream: Any, cancel_event: threading.Event) -> dict[str, Any]:
        del lease, max_tokens, cancel_event
        self.calls.append(messages)
        if not self.responses:
            raise AssertionError("model was called after the scripted response sequence ended")
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        on_stream({"reasoning_delta": "正在检查真实观察。", "content_delta": "", "reasoning_chars": 10, "content_chars": 0})
        return {"content": response, "reasoning_content": "正在检查真实观察。", "usage": {"prompt_tokens": 12, "completion_tokens": 8}}


class FakeRouter:
    def __init__(self, *, owners: dict[str, str] | None = None, invalid_actions: set[str] | None = None) -> None:
        self.owners = owners or {}
        self.invalid_actions = invalid_actions or set()
        self.executed: list[tuple[str, dict[str, Any]]] = []
        self.cancelled = False

    def owner(self, action_id: str) -> str:
        return self.owners.get(action_id, "platform")

    def validate(self, action_id: str, input_value: dict[str, Any], run: dict[str, Any]) -> None:
        del input_value, run
        if action_id in self.invalid_actions:
            raise PlatformError("ACTION_INPUT_INVALID", "测试用 action 参数无效。", status_code=422)

    def execute(self, action_id: str, input_value: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
        del run
        self.executed.append((action_id, dict(input_value)))
        return {"status": "succeeded", "echo": input_value}

    def cancel(self, run: dict[str, Any]) -> None:
        del run
        self.cancelled = True


class AgentRuntimeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.store = AgentRunStore(Path(self.tempdir.name) / "runs")

    def _runtime(self, gateway: ScriptedGateway, router: FakeRouter) -> AgentRuntime:
        return AgentRuntime(
            store=self.store,
            leases=CredentialLeaseStore(default_ttl_seconds=60),
            model_gateway=gateway,
            action_router=router,
            system_prompt=lambda _: "只返回严格 JSON。",
        )

    def _start(self, runtime: AgentRuntime, *, mode: str = "page_agent", max_turns: int = 8) -> str:
        run = runtime.create(
            project_id="local-default",
            goal="测试持久化 Agent Run",
            initial_input="请继续真实任务。",
            mode=mode,
            context={"hotspot": "unit-test"},
            budget={"max_turns": max_turns, "max_actions_per_turn": 3, "max_output_tokens": 4096, "context_window_tokens": 8192},
            action_catalog_version="test",
            skill_versions=[],
        )
        run_id = str(run["run_id"])
        runtime.attach_credential(run_id, base_url="https://example.invalid/v1", model="fake", api_key="not-persisted")
        runtime.start(run_id)
        return run_id

    def _wait_for(self, run_id: str, states: set[str], timeout_seconds: float = 3.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            run = self.store.read(run_id)
            if str(run.get("state")) in states:
                return run
            time.sleep(0.01)
        current = self.store.read(run_id)
        self.fail(f"run {run_id} did not reach {states}; current={current.get('state')} detail={current.get('state_detail')}")

    def test_page_session_recovers_latest_durable_run_without_terminal_id(self) -> None:
        def create(page_session_id: str, *, project_id: str = "local-default") -> str:
            public = self.store.create(
                project_id=project_id,
                goal="页面持久会话",
                initial_input="等待输入",
                mode="page_agent",
                context={"page_session_id": page_session_id},
                budget={"max_turns": 1, "max_actions_per_turn": 1, "max_output_tokens": 1024, "context_window_tokens": 8192},
                action_catalog_version="test",
                skill_versions=[],
            )
            return str(public["run_id"])

        older_run_id = create("page_recovery-owner")
        newer_run_id = create("page_recovery-owner")
        create("page_other-owner")
        create("page_recovery-owner", project_id="other-project")

        self.store.mutate(
            older_run_id,
            event_type="terminal.session_attached",
            event_state="running",
            payload={},
            apply=lambda run: run.update({
                "engine": {
                    "terminal_transport": {
                        "adapter_id": "kimi",
                        "updated_at": "2026-08-20T00:00:00Z",
                    },
                },
            }),
        )
        self.store.mutate(
            newer_run_id,
            event_type="terminal.session_attached",
            event_state="running",
            payload={},
            apply=lambda run: run.update({
                "engine": {
                    "terminal_transport": {
                        "adapter_id": "team-agent",
                        "updated_at": "2026-08-21T00:00:00Z",
                    },
                },
            }),
        )
        self.store.append_event(
            older_run_id,
            "terminal.command_submitted",
            "running",
            {"command_sha256": "test"},
        )

        recovered = self.store.find_latest_by_page_session(
            "page_recovery-owner",
            project_id="local-default",
            preferred_adapter_id="kimi",
        )

        self.assertIsNotNone(recovered)
        self.assertEqual(recovered["run_id"], older_run_id)
        self.assertIsNone(self.store.find_latest_by_page_session("page_missing-owner"))

    def test_invalid_c_escape_is_repaired_without_silent_action_loss(self) -> None:
        raw = '{"reply":"暂存源码","disposition":"continue","actions":[{"action_id":"build.stage_source","input":{"source":"char terminator = \\0;"}}]}'

        parsed = parse_agent_decision(raw)

        self.assertEqual(parsed.actions[0]["action_id"], "build.stage_source")
        self.assertEqual(parsed.actions[0]["input"]["source"], "char terminator = \\0;")

    def test_raw_source_block_preserves_c_lexical_whitespace(self) -> None:
        control = decision(
            "暂存独立源码块。",
            disposition="continue",
            actions=[{
                "action_id": "build.stage_source",
                "input": {"profile_id": "gpu_gles3", "language": "c", "source_ref": "experiment_c"},
            }],
        )
        source = "#define MAX_PIXELS 4096\nstatic uint32_t g_src[MAX_PIXELS];\nvoid _start(void) {}\n"

        parsed = parse_agent_decision(
            f'{control}\n<agent-source id="experiment_c" language="c">\n{source}</agent-source>'
        )

        self.assertNotIn("source_ref", parsed.actions[0]["input"])
        self.assertEqual(parsed.actions[0]["input"]["source"], source)

    def test_unreferenced_source_block_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "未被 action 引用"):
            parse_agent_decision(
                decision("没有暂存动作。")
                + '\n<agent-source id="orphan" language="c">\nvoid _start(void) {}\n</agent-source>'
            )

    def test_staged_source_is_not_replayed_into_next_model_context(self) -> None:
        source = "static uint32_t g_src[4096];\nvoid _start(void) {}\n"
        control = decision(
            "暂存源码。",
            disposition="continue",
            actions=[{
                "action_id": "build.stage_source",
                "input": {"profile_id": "gpu_gles3", "language": "c", "source_ref": "experiment_c"},
            }],
        )

        class StageRouter(FakeRouter):
            def execute(self, action_id: str, input_value: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
                del run
                self.executed.append((action_id, dict(input_value)))
                return {"status": "succeeded", "source_artifact_id": "art_source", "source_sha256": "a" * 64}

        gateway = ScriptedGateway([
            f'{control}\n<agent-source id="experiment_c" language="c">\n{source}</agent-source>',
            decision("源码 artifact 已建立。"),
        ])
        router = StageRouter()
        runtime = self._runtime(gateway, router)

        run_id = self._start(runtime)
        self._wait_for(run_id, {"completed"})

        self.assertEqual(router.executed[0][1]["source"], source)
        second_turn_context = "\n".join(message["content"] for message in gateway.calls[1])
        self.assertNotIn("static uint32_t g_src", second_turn_context)
        self.assertIn("source_payload", second_turn_context)

    def test_execution_trace_redacts_pairing_credentials_by_field_and_inline_text(self) -> None:
        redacted = _safe_json(
            {
                "pairing_code": "one-time-bridge-code",
                "user_note": "pairing code=one-time-bridge-code",
                "nested": {"bridge_secret": "private-bridge-secret"},
            }
        )

        self.assertEqual(redacted["pairing_code"], "[redacted]")
        self.assertIn("[redacted]", redacted["user_note"])
        self.assertEqual(redacted["nested"]["bridge_secret"], "[redacted]")

    def test_malformed_model_output_is_corrected_instead_of_completing(self) -> None:
        gateway = ScriptedGateway(["{this is not JSON", decision("已形成当前结论。")])
        router = FakeRouter()
        runtime = self._runtime(gateway, router)

        run_id = self._start(runtime)
        run = self._wait_for(run_id, {"completed"})

        self.assertEqual(run["state"], "completed")
        self.assertEqual(len(gateway.calls), 2)
        self.assertEqual(router.executed, [])
        events = self.store.events(run_id, after_sequence=0, limit=100)
        self.assertTrue(any(event["event_type"] == "model.contract_invalid" for event in events))
        self.assertFalse(any(event["event_type"] == "run.state_changed" and event["state"] == "completed" and event["sequence"] < 5 for event in events))

    def test_repeated_action_is_reused_not_reexecuted(self) -> None:
        action = {"action_id": "platform.echo", "input": {"value": 7}}
        gateway = ScriptedGateway([
            decision("先读取一次。", disposition="continue", actions=[action]),
            decision("不得重复执行，复用已有观察。", disposition="continue", actions=[action]),
            decision("已有真实结果，完成。"),
        ])
        router = FakeRouter()
        runtime = self._runtime(gateway, router)

        run_id = self._start(runtime)
        self._wait_for(run_id, {"completed"})

        self.assertEqual(router.executed, [("platform.echo", {"value": 7})])
        events = self.store.events(run_id, after_sequence=0, limit=100)
        self.assertTrue(any(event["event_type"] == "tool.result_reused" for event in events))

    def test_reported_build_failure_is_a_real_next_turn_observation(self) -> None:
        class CompileFailingRouter(FakeRouter):
            def execute(self, action_id: str, input_value: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
                self.executed.append((action_id, dict(input_value)))
                return {
                    "status": "compile_failed",
                    "build_result_artifact_id": "art_build_failed",
                    "summary_zh": "固定 profile 编译失败，诊断已保存。",
                }

        action = {"action_id": "build.create", "input": {"source_artifact_id": "art_source", "profile_id": "gpu_gles3"}}
        gateway = ScriptedGateway([
            decision("先构建最小实验。", disposition="continue", actions=[action]),
            decision("已读取编译失败事实，停止并报告诊断。"),
        ])
        router = CompileFailingRouter()
        runtime = self._runtime(gateway, router)

        run_id = self._start(runtime)
        self._wait_for(run_id, {"completed"})

        events = self.store.events(run_id, after_sequence=0, limit=100)
        failure = next(event for event in events if event["event_type"] == "tool.result" and event["payload"].get("action_id") == "build.create")
        self.assertEqual(failure["payload"]["status"], "failed")
        self.assertEqual(failure["payload"]["result"]["build_result_artifact_id"], "art_build_failed")
        follow_up = gateway.calls[1][-1]["content"]
        self.assertIn("compile_failed", follow_up)
        self.assertIn("art_build_failed", follow_up)

    def test_ui_result_resumes_same_run_without_backend_execution(self) -> None:
        action = {"action_id": "ui.select_hotspot", "input": {"event_id": "event_1"}}
        gateway = ScriptedGateway([
            decision("请在页面选择热点。", disposition="continue", actions=[action]),
            decision("页面结果已收到，形成结论。"),
        ])
        router = FakeRouter(owners={"ui.select_hotspot": "ui"})
        runtime = self._runtime(gateway, router)

        run_id = self._start(runtime)
        waiting = self._wait_for(run_id, {"waiting_for_ui"})
        pending = waiting["pending_ui_actions"]
        self.assertEqual(len(pending), 1)
        runtime.submit_ui_result(run_id, str(pending[0]["request_id"]), {"selected": "event_1"})
        self._wait_for(run_id, {"completed"})

        self.assertEqual(router.executed, [])
        events = self.store.events(run_id, after_sequence=0, limit=100)
        self.assertTrue(any(event["event_type"] == "ui.action_result" for event in events))

    def test_new_user_message_keeps_prior_checkpoint_observation(self) -> None:
        gateway = ScriptedGateway([
            decision("需要用户补充真实设备序列号。", disposition="wait_for_user"),
            decision("已合并补充输入与上轮事实。"),
        ])
        router = FakeRouter()
        runtime = self._runtime(gateway, router)

        run_id = self._start(runtime)
        self._wait_for(run_id, {"waiting_for_ui"})
        runtime.submit_message(run_id, "设备序列号是 026MLM25CK129272，请继续。")
        self._wait_for(run_id, {"completed"})

        follow_up = gateway.calls[1][-1]["content"]
        self.assertIn("上一检查点的未完成任务与真实观察", follow_up)
        self.assertIn("需要用户补充真实设备序列号", follow_up)
        self.assertIn("026MLM25CK129272", follow_up)

    def test_device_validation_cannot_complete_before_device_execution_observation(self) -> None:
        device_action = {"action_id": "device.execute_shell", "input": {"device_probe_artifact_id": "art_probe", "command": "./experiment.bin"}}
        gateway = ScriptedGateway([
            decision("尚未执行设备命令，但先结束。"),
            decision("现在执行设备侧实验。", disposition="continue", actions=[device_action]),
            decision("已取得设备命令返回，形成当前结论。"),
        ])
        router = FakeRouter(owners={"device.execute_shell": "device"})
        runtime = self._runtime(gateway, router)

        run_id = self._start(runtime, mode="device_validation")
        self._wait_for(run_id, {"completed"})

        self.assertEqual(router.executed, [("device.execute_shell", {"device_probe_artifact_id": "art_probe", "command": "./experiment.bin"})])
        events = self.store.events(run_id, after_sequence=0, limit=100)
        self.assertTrue(any(event["event_type"] == "run.incomplete_device_validation" for event in events))

    def test_device_validation_does_not_treat_compile_error_as_environment_blocker(self) -> None:
        build_action = {"action_id": "build.create", "input": {"source_artifact_id": "art_source", "profile_id": "gpu_gles3"}}
        device_action = {"action_id": "device.execute_shell", "input": {"device_probe_artifact_id": "art_probe", "command": "./experiment.bin"}}

        class CompileThenDeviceRouter(FakeRouter):
            def execute(self, action_id: str, input_value: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
                del run
                self.executed.append((action_id, dict(input_value)))
                if action_id == "build.create":
                    return {
                        "status": "compile_failed",
                        "error": {"code": "BUILD_COMPILE_FAILED", "message": "source.c:3: expected identifier"},
                    }
                return {"status": "succeeded", "execution": {"return_code": 0}}

        gateway = ScriptedGateway([
            decision("先编译实验。", disposition="continue", actions=[build_action]),
            decision("源码编译失败，先把它当作环境阻断结束。"),
            decision("根据运行时门禁继续执行修订后的设备实验。", disposition="continue", actions=[device_action]),
            decision("已有真实设备执行结果，完成。"),
        ])
        router = CompileThenDeviceRouter(owners={"device.execute_shell": "device"})
        runtime = self._runtime(gateway, router)

        run_id = self._start(runtime, mode="device_validation")
        self._wait_for(run_id, {"completed"})

        self.assertEqual(len(gateway.calls), 4)
        self.assertEqual([action_id for action_id, _ in router.executed], ["build.create", "device.execute_shell"])
        correction = gateway.calls[2][-1]["content"]
        self.assertIn("普通源码编译或链接错误属于可修订实验", correction)

    def test_cancel_drops_late_model_stream_and_decision(self) -> None:
        started = threading.Event()
        release = threading.Event()

        class BlockingGateway:
            def __call__(self, lease: Any, messages: list[dict[str, str]], *, max_tokens: int, on_stream: Any, cancel_event: threading.Event) -> dict[str, Any]:
                del lease, messages, max_tokens, cancel_event
                started.set()
                if not release.wait(timeout=2):
                    raise AssertionError("test did not release the blocked model call")
                on_stream({"reasoning_delta": "这是取消后才到达的迟到流。", "reasoning_chars": 14})
                return {
                    "content": decision(
                        "取消后不得执行这个动作。",
                        disposition="continue",
                        actions=[{"action_id": "platform.echo", "input": {"value": 9}}],
                    ),
                    "reasoning_content": "迟到推理",
                    "usage": {},
                }

        router = FakeRouter()
        runtime = self._runtime(BlockingGateway(), router)  # type: ignore[arg-type]
        run_id = self._start(runtime)
        self.assertTrue(started.wait(timeout=1))

        cancelled = runtime.cancel(run_id)
        release.set()
        self.assertEqual(cancelled["state"], "cancelled")
        self._wait_for(run_id, {"cancelled"})
        time.sleep(0.05)

        events = self.store.events(run_id, after_sequence=0, limit=100)
        cancel_sequence = next(event["sequence"] for event in events if event["event_type"] == "run.cancel_requested")
        late_types = {
            event["event_type"]
            for event in events
            if event["sequence"] > cancel_sequence
        }
        self.assertFalse({"model.stream", "model.response", "model.decision"} & late_types)
        self.assertEqual(router.executed, [])
        self.assertTrue(router.cancelled)

    def test_output_limit_and_restart_create_recoverable_checkpoints(self) -> None:
        gateway = ScriptedGateway([ModelGatewayError("MODEL_OUTPUT_TRUNCATED", "输出被截断。", status_code=409)])
        router = FakeRouter()
        runtime = self._runtime(gateway, router)

        run_id = self._start(runtime)
        self._wait_for(run_id, {"paused_output_limit"})

        restarted = self._runtime(ScriptedGateway([]), FakeRouter())
        self.assertEqual(restarted.recover_after_restart(), 1)
        run = self._wait_for(run_id, {"waiting_for_model_credential"})
        self.assertIn("浏览器自动重新连接模型", str(run["state_detail"]))

    def test_restart_credential_reattach_resumes_checkpoint_without_expanding_budget(self) -> None:
        runtime = self._runtime(ScriptedGateway([]), FakeRouter())
        created = runtime.create(
            project_id="local-default",
            goal="验证零增量恢复",
            initial_input="从这个原始检查点继续。",
            mode="page_agent",
            context={"hotspot": "unit-test"},
            budget={"max_turns": 3, "max_actions_per_turn": 2, "max_output_tokens": 1024, "context_window_tokens": 8192},
            action_catalog_version="test",
            skill_versions=[],
        )
        run_id = str(created["run_id"])
        self.assertEqual(runtime.recover_after_restart(), 1)

        restarted_gateway = ScriptedGateway([decision("已从原检查点恢复。")])
        restarted = self._runtime(restarted_gateway, FakeRouter())
        restarted.attach_credential(run_id, base_url="https://example.invalid/v1", model="fake", api_key="not-persisted")
        restarted.resume(run_id, extra_turns=0)
        completed = self._wait_for(run_id, {"completed"})

        self.assertEqual(completed["budget"]["max_turns"], 3)
        self.assertIn("从这个原始检查点继续", restarted_gateway.calls[0][-1]["content"])


if __name__ == "__main__":
    unittest.main()
