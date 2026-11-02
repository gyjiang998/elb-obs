"""Selected-page routes survive old assistant state without physical device I/O."""
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import Mock

from backend.agent_runtime import AgentRunStore, AgentRuntime, CredentialLeaseStore
from backend.core import PlatformError
from backend.device_gateway import DeviceGateway
from backend.runtime_action_router import PlatformRuntimeActionRouter
from backend.scenario_capture_target import bind_capture_target, capture_target


LOCAL = {"connection_mode": "local", "bridge_id": "", "device_serial": "phone-same"}
A = {"connection_mode": "bridge", "bridge_id": "bridge_A", "device_serial": "phone-same"}
B = {"connection_mode": "bridge", "bridge_id": "bridge_B", "device_serial": "phone-same"}


def run_for(target):
    return {"run_id": "run_test", "project_id": "fixture", "context": {
        "device_execution_mode": "remote_bridge", "device_selector": {"bridge_id": "bridge_A"},
        "page_context": {"active_view": "scenario_capture", "scenario_capture": {"selected_device": target}},
    }}


class CaptureTargetTests(unittest.TestCase):
    def setUp(self):
        self.workbench = Mock()
        self.gateway = Mock(spec=DeviceGateway)
        self.gateway.logical_device_id = DeviceGateway.logical_device_id
        self.store = Mock()
        self.router = PlatformRuntimeActionRouter(
            artifact_store=self.store, build_worker=Mock(), device_gateway=self.gateway,
            default_source_connector=lambda: {"connector_id": "fixture", "snapshot": {}},
            bridge_artifact_url=lambda *_: "unused", parallel_workbench=self.workbench,
        )

    def assert_code(self, code, call):
        with self.assertRaises(PlatformError) as caught:
            call()
        self.assertEqual(caught.exception.code, code)

    def test_submitted_snapshot_wins_over_later_page_and_old_assistant_bridge(self):
        run = run_for(A)
        run["context"]["focused_task"] = {"kind": "scenario_capture", "request_context": {"selected_device": B}}
        self.router.execute("scenario_capture.execute", {"plan_id": "plan_fixture"}, run)
        self.workbench.action.assert_called_once_with("scenario_capture.execute", {"plan_id": "plan_fixture", **B}, agent_run_id="run_test")
        self.gateway.local_discover.assert_not_called()

    def test_manual_chat_json_page_context_uses_page_selection(self):
        run = run_for(B)
        run["context"]["page_context"] = json.dumps(run["context"]["page_context"])
        self.router.execute("scenario_capture.discover_devices", {"connection_mode": "auto"}, run)
        self.workbench.action.assert_called_once_with("scenario_capture.discover_devices", B, agent_run_id="run_test")

    def test_snapshot_supports_frontend_target_and_repair_direct_route(self):
        for request in ({"scenario_capture_target": B}, {**B, "repair_request_id": "repair_fixture"}):
            with self.subTest(request=request):
                run = run_for(A)
                run["context"]["focused_task"] = {"kind": "scenario_capture", "request_context": request}
                self.assertEqual(capture_target(run["context"]), (B, False))

    def test_local_discovery_clears_old_bridge_selector(self):
        self.gateway.local_discover.return_value = {"hdc": {"available": True}, "devices": [{"serial": LOCAL["device_serial"]}]}
        result = self.router.execute("device.discover", {}, run_for(LOCAL))
        self.assertEqual(result["executor"], "local_hdc")
        self.gateway.local_discover.assert_called_once()
        self.gateway._bridge_job.assert_not_called()

    def test_bridge_probe_fills_logical_identity_and_serial_before_execution(self):
        run = run_for(B)
        payload, effective = bind_capture_target("device.probe", {}, run, logical_device_id=DeviceGateway.logical_device_id)
        self.assertEqual(payload, {**B, "logical_device_id": DeviceGateway.logical_device_id(B["device_serial"])})
        self.assertEqual(effective["context"]["device_selector"]["bridge_id"], "bridge_B")
        self.router.validate("device.probe", {}, run)
        self.assertEqual(run["context"]["device_selector"]["bridge_id"], "bridge_A")

    def test_conflicting_explicit_route_fails_before_any_device_or_capture_call(self):
        for mismatch in ({"bridge_id": "bridge_A"}, {"connection_mode": "local"}, {"device_serial": "other-phone"}):
            for method in (self.router.validate, self.router.execute):
                with self.subTest(mismatch=mismatch, method=method.__name__):
                    self.assert_code("SCENARIO_DEVICE_TARGET_MISMATCH", lambda: method(
                        "scenario_capture.execute", {"plan_id": "plan_fixture", **mismatch}, run_for(B)))
        self.workbench.action.assert_not_called()
        self.assertEqual(self.gateway.mock_calls, [])

    def test_unselected_task_never_inherits_old_bridge_even_with_explicit_tool_arguments(self):
        run = run_for(None)
        for action, inputs in (("scenario_capture.execute", {"plan_id": "fixture", **A}), ("device.probe", A)):
            self.assert_code("SCENARIO_DEVICE_SELECTION_REQUIRED", lambda: self.router.execute(action, inputs, run))
        self.router.execute("scenario_capture.discover_devices", {"connection_mode": "auto"}, run)
        self.workbench.action.assert_called_once()

    def test_explicit_empty_snapshot_stays_unselected_despite_new_page_selection(self):
        run = run_for(B)
        run["context"]["scenario_capture_target"] = B
        run["context"]["focused_task"] = {"kind": "scenario_capture", "request_context": {"selected_device": None}}
        self.assertEqual(capture_target(run["context"]), (None, True))

    def test_request_without_device_stays_unselected_despite_later_page_selection(self):
        run = run_for(B)
        run["context"]["focused_task"] = {"kind": "scenario_capture", "request_context": {"plan_id": "plan_fixture"}}
        self.assertEqual(capture_target(run["context"]), (None, True))
        run["context"].pop("focused_task")
        run["context"]["scenario_capture_selection_required"] = True
        self.assertEqual(capture_target(run["context"]), (None, True))

    def test_local_route_rejects_stale_remote_lease(self):
        self.assert_code("SCENARIO_DEVICE_TARGET_MISMATCH", lambda: self.router.execute(
            "device.probe", {"device_lease_id": "lease_old"}, run_for(LOCAL)))

    def test_wrong_probe_phone_is_rejected_before_shell_or_deployment(self):
        self.store.read.return_value = {"device": {"serial": "another-phone"}}
        for target in (LOCAL, B):
            for action in ("device.execute_shell", "device.deploy_artifact", "device.collect_artifact"):
                with self.subTest(target=target, action=action):
                    self.assert_code("SCENARIO_DEVICE_TARGET_MISMATCH", lambda: self.router.execute(
                        action, {"device_probe_artifact_id": "art_old", "command": "pwd"}, run_for(target)))
        self.assertEqual(self.gateway.mock_calls, [])

    def test_general_agent_without_capture_context_keeps_original_behavior(self):
        run = {"context": {"device_selector": {"bridge_id": "bridge_A"}}}
        inputs = {"device_serial": "other-phone"}
        self.assertEqual(bind_capture_target("device.probe", inputs, run,
            logical_device_id=DeviceGateway.logical_device_id), (inputs, run))

    def test_capture_receipt_reuse_requires_same_route_and_matching_arguments(self):
        receipt = {"status": "succeeded", "result": {"inputs": A}}
        self.assertTrue(self.router.can_reuse("scenario_capture.execute", {"plan_id": "fixture"}, receipt, run_for(A)))
        self.assertFalse(self.router.can_reuse("scenario_capture.execute", {"plan_id": "fixture"}, receipt, run_for(B)))
        self.assertFalse(self.router.can_reuse("scenario_capture.execute", {"plan_id": "fixture", **B}, receipt, run_for(A)))

    def test_route_scopes_idempotency_in_both_external_and_legacy_agent_paths(self):
        for action in ("scenario_capture.execute", "device.execute_shell", "device.probe", "device.create_lease"):
            with self.subTest(action=action):
                inputs = {"command": "fixture-command"}
                keys = [AgentRuntime._run_action_key(run_for(target), action, inputs) for target in (A, B, LOCAL, None)]
                self.assertEqual(len(set(keys)), 4)
                self.assertEqual(keys[0], AgentRuntime._capture_action_key(run_for(A), action, inputs))
                self.assertEqual(keys[0], AgentRuntime._run_action_key(run_for(A), action, dict(inputs)))
        self.assertIsNone(AgentRuntime._capture_action_key({"context": {}}, "device.probe", {}))

    def test_reused_cli_session_runs_same_plan_on_new_selection_once(self):
        self.workbench.action.side_effect = lambda action, payload, **_: {"status": "succeeded", "inputs": payload}
        with tempfile.TemporaryDirectory() as directory:
            runtime = AgentRuntime(store=AgentRunStore(Path(directory)), leases=CredentialLeaseStore(),
                model_gateway=Mock(), action_router=self.router, system_prompt=lambda _: "fixture")
            run = runtime.create(project_id="fixture", goal="capture", initial_input="fixture", mode="page_agent",
                context=run_for(A)["context"], budget={}, action_catalog_version="fixture", skill_versions=[])
            inputs = {"plan_id": "plan_fixture"}
            first = runtime.execute_external_action(run["run_id"], "scenario_capture.execute", inputs)
            again = runtime.execute_external_action(run["run_id"], "scenario_capture.execute", inputs)
            self.assertEqual(first["result"]["inputs"]["bridge_id"], "bridge_A")
            self.assertTrue(again["reused_from_checkpoint"])
            runtime.prepare_task(run["run_id"], goal="capture selected B", mode="page_agent", context=run_for(B)["context"])
            second = runtime.execute_external_action(run["run_id"], "scenario_capture.execute", inputs)
            self.assertEqual(second["result"]["inputs"]["bridge_id"], "bridge_B")
            self.assertEqual(self.workbench.action.call_count, 2)

    def test_context_change_while_waiting_for_action_lock_cannot_retarget_or_pollute_cache(self):
        self.workbench.action.side_effect = lambda action, payload, **_: {"status": "succeeded", "inputs": payload}
        with tempfile.TemporaryDirectory() as directory:
            runtime = AgentRuntime(store=AgentRunStore(Path(directory)), leases=CredentialLeaseStore(),
                model_gateway=Mock(), action_router=self.router, system_prompt=lambda _: "fixture")
            run = runtime.create(project_id="fixture", goal="capture A", initial_input="fixture", mode="page_agent",
                context=run_for(A)["context"], budget={}, action_catalog_version="fixture", skill_versions=[])
            inputs = {"plan_id": "plan_fixture"}
            key_a = runtime._run_action_key(runtime.store.read(run["run_id"]), "scenario_capture.execute", inputs)

            class ContextSwitchLock:
                switched = False

                def __enter__(self):
                    if not self.switched:
                        self.switched = True
                        runtime.prepare_task(run["run_id"], goal="capture B", mode="page_agent", context=run_for(B)["context"])

                def __exit__(self, *_):
                    return False

            runtime._external_action_locks[key_a] = ContextSwitchLock()
            first = runtime.execute_external_action(run["run_id"], "scenario_capture.execute", inputs)
            self.assertEqual(first["action_key"], key_a)
            self.assertEqual(first["result"]["inputs"]["bridge_id"], "bridge_A")
            self.assertEqual(capture_target(runtime.store.read(run["run_id"])["context"])[0], B)
            second = runtime.execute_external_action(run["run_id"], "scenario_capture.execute", inputs)
            self.assertEqual(second["result"]["inputs"]["bridge_id"], "bridge_B")
            runtime.prepare_task(run["run_id"], goal="capture A", mode="page_agent", context=run_for(A)["context"])
            again = runtime.execute_external_action(run["run_id"], "scenario_capture.execute", inputs)
            self.assertTrue(again["reused_from_checkpoint"])
            self.assertEqual(again["result"]["inputs"]["bridge_id"], "bridge_A")
            self.assertEqual(self.workbench.action.call_count, 2)

    def test_legacy_runtime_keeps_validated_device_snapshot_until_execution(self):
        from backend.tests.test_agent_runtime import ScriptedGateway, decision
        self.workbench.action.side_effect = lambda action, payload, **_: {"status": "succeeded", "inputs": payload}
        with tempfile.TemporaryDirectory() as directory:
            gateway = ScriptedGateway([
                decision("capture", disposition="continue", actions=[{"action_id": "scenario_capture.execute", "input": {"plan_id": "plan_fixture"}}]),
                decision("done"),
            ])
            runtime = AgentRuntime(store=AgentRunStore(Path(directory)), leases=CredentialLeaseStore(),
                model_gateway=gateway, action_router=self.router, system_prompt=lambda _: "fixture")
            run = runtime.create(project_id="fixture", goal="capture A", initial_input="fixture", mode="page_agent",
                context=run_for(A)["context"], budget={}, action_catalog_version="fixture", skill_versions=[])
            validate = self.router.validate

            def switch_after_validation(action, payload, snapshot):
                validate(action, payload, snapshot)
                runtime.prepare_task(run["run_id"], goal="capture B", mode="page_agent", context=run_for(B)["context"])

            self.router.validate = switch_after_validation
            runtime.attach_credential(run["run_id"], base_url="https://example.invalid/v1", model="fake", api_key="fixture")
            runtime.start(run["run_id"])
            deadline = time.monotonic() + 3
            while runtime.store.read(run["run_id"])["state"] not in {"completed", "failed"} and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertEqual(runtime.store.read(run["run_id"])["state"], "completed")
            self.assertEqual(self.workbench.action.call_args.args[1]["bridge_id"], "bridge_A")


if __name__ == "__main__":
    unittest.main()
