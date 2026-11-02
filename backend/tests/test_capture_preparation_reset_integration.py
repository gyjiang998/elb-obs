"""Preparation/reset boundaries through the complete runner; no real device access."""
from __future__ import annotations

import json
import re
import shlex
import tempfile
import unittest
from pathlib import Path

from backend.core import ArtifactStore
from backend.scenario_capture import CaptureCancelled, ScenarioCaptureManager
from backend.tests.test_scenario_capture import SimulatedDeviceClient, trace_fixture
from tools.scenario_capture.runner import execute_plan


GALLERY = "com.huawei.hmos.photos"


def ui_page(state: str, bundle: str = GALLERY) -> dict:
    identifiers = {
        "desktop": [("SCBDesktop_Flex_Desktop", "com.ohos.sceneboard", "Column")],
        "home": [("TabPhotos", bundle, "Column"), ("TabAlbums", bundle, "Column")],
        "browser": [("PhotoBrowserBg", bundle, "Column"), ("AccessibilityComponent_BrowserBack", bundle, "Button")],
        "editor": [("petalClip_bottomToolBar_4", bundle, "Column")],
        "dialog": [("PhotoBrowserBg", bundle, "Column"), ("confirm", bundle, "AlertDialog")],
        "custom": [("CustomWorkspace", bundle, "Column")],
    }[state]
    return {"attributes": {"bounds": "[0,0][1280,2832]"}, "children": [
        {"attributes": {"id": name, "bundleName": target, "type": kind,
                        "bounds": "[40,200][240,400]", "visible": "true", "enabled": "true"}, "children": []}
        for name, target, kind in identifiers]}


class ResetScenarioClient(SimulatedDeviceClient):
    """Model app lifecycle while the shared double models remote collector files."""

    def __init__(self, state="editor", *, bundle=GALLERY, cancel_on_stop=False, fail_stop=False):
        super().__init__()
        self.state, self.bundle = state, bundle
        self.app_running = True
        self.cancelled = False
        self.cancel_on_stop, self.fail_stop = cancel_on_stop, fail_stop
        self.calls = []

    def check_cancelled(self):
        if self.cancelled:
            raise CaptureCancelled("QA cancelled during launch preparation")

    def shell(self, command, timeout=40, *, cleanup=False):
        self.calls.append((command, timeout, cleanup))
        if not cleanup:
            self.check_cancelled()
        result = super().shell(command, timeout, cleanup=cleanup)
        if command.startswith("aa force-stop "):
            if self.fail_stop:
                return "error: force-stop failed"
            self.app_running, self.state = False, "desktop"
            self.cancelled = self.cancel_on_stop
        elif command.startswith("aa start "):
            self.app_running, self.state = True, "home"
            result = "start ability successfully."
        elif command == "uitest uiInput keyEvent Home":
            self.state = "desktop"
        elif command.startswith("pidof "):
            result = "123" if self.app_running else ""
        elif command.startswith("sh "):
            script = self.files[shlex.split(command)[1]].decode()
            if "command_action LAUNCH aa start" in script:
                self.app_running = True
                self.state = "home" if self.bundle == GALLERY else "custom"
            marker = re.search(r"begin GALLERY_STEP_(\d+)_(\S+)", script)
            if marker and "hitrace --trace_begin" in script:
                destination = str(Path(shlex.split(command)[1]).parent / "trace.ftrace")
                self.files[destination] = trace_fixture(int(marker[1]), marker[2]).encode()
        elif command.startswith("uitest dumpLayout"):
            self.files[shlex.split(command)[-1]] = json.dumps(ui_page(self.state, self.bundle)).encode()
        return result


class SessionResponseClient(ResetScenarioClient):
    """Inject device session replies without pretending remote cleanup succeeded."""

    def __init__(self, *, replies=None, trace_active=False, **kwargs):
        super().__init__(**kwargs)
        self.replies = replies or {}
        self.trace_active = trace_active

    def shell(self, command, timeout=40, *, cleanup=False):
        before = self.collector_session
        result = super().shell(command, timeout, cleanup=cleanup)
        if command.startswith("# HETERO_COLLECTOR_SESSION "):
            operation = command.splitlines()[0].split()[-1]
            if operation in self.replies:
                self.collector_session = before
                reply = self.replies[operation]
                if isinstance(reply, Exception):
                    raise reply
                return reply
        if self.trace_active and command.startswith("for root in"):
            return "/sys/kernel/tracing=1"
        return result


class CapturePreparationResetIntegrationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manager = ScenarioCaptureManager(self.root, ArtifactStore(self.root), object())

    def plan(self, *, selected="step_02", mode="auto", bundle=GALLERY):
        targets = {"photos": {"id": "TabPhotos"}, "albums": {"id": "TabAlbums"}}
        return self.manager.create_plan({"template_id": "custom", "profile": {
            "bundle": bundle, "ability": "com.huawei.photos.MainAbility", "targets": targets},
            "steps": [{"title": "启动应用", "operation": "launch", "expected_targets": ["photos", "albums"] if bundle == GALLERY else []},
                      {"title": "被测步骤", "operation": "wait", "seconds": 0}],
            "capture": {"selected_step_ids": [selected], "preparation_mode": mode, "collectors": ["trace"]}})

    def execute(self, plan, client, suffix="test"):
        return execute_plan(plan, client, self.root / suffix, "capture_" + suffix,
                            page_ready_timeout_seconds=0)

    def test_auto_reset_from_editor_or_dialog_is_outside_all_capture_windows(self):
        for state in ("editor", "dialog"):
            with self.subTest(state=state):
                client = ResetScenarioClient(state)
                manifest = self.execute(self.plan(), client, state)
                self.assertEqual(manifest["status"], "succeeded", manifest["quality"])
                self.assertEqual([step["captured"] for step in manifest["steps"]], [False, True])
                stop = next(i for i, command in enumerate(client.commands) if command.startswith("aa force-stop "))
                relaunch = next(i for i, command in enumerate(client.commands) if command.startswith("aa start "))
                first_script = client.commands.index("sh " + client.executed[0])
                measured_script = client.commands.index("sh " + client.executed[1])
                self.assertLess(stop, relaunch)
                self.assertLess(relaunch, first_script)
                self.assertLess(first_script, measured_script)
                acquisition = next(i for i, command in enumerate(client.commands)
                                   if command.startswith("# HETERO_COLLECTOR_SESSION acquire\n"))
                self.assertLess(acquisition, stop)
                self.assertEqual(sum(command.startswith("aa force-stop ") for command in client.commands), 1)
                for filename in client.executed:
                    self.assertNotIn("aa force-stop", client.files[filename].decode())
                    self.assertIn("register_collector owner", client.files[filename].decode())
                self.assertNotIn("hitrace --trace_begin", client.files[client.executed[0]].decode())
                self.assertIn("hitrace --trace_begin", client.files[client.executed[1]].decode())
                for command, timeout, cleanup in client.calls:
                    if command.startswith(("aa force-stop ", "aa start ")):
                        self.assertLessEqual(timeout, 8)
                        self.assertFalse(cleanup)

    def test_captured_launch_resets_process_but_keeps_launch_inside_measurement(self):
        client = ResetScenarioClient("editor")
        manifest = self.execute(self.plan(selected="step_01"), client)
        self.assertEqual(manifest["status"], "succeeded", manifest["quality"])
        self.assertEqual(len(client.executed), 1)
        self.assertTrue(any(command.startswith("aa force-stop ") for command in client.commands))
        self.assertFalse(any(command.startswith("aa start ") for command in client.commands))
        source = client.files[client.executed[0]].decode()
        self.assertIn("command_action LAUNCH aa start", source)
        self.assertLess(source.index("hitrace --trace_begin"), source.index("command_action LAUNCH aa start"))
        self.assertLess(source.index("begin GALLERY_ACTION_01"), source.index("command_action LAUNCH aa start"))
        self.assertNotIn("aa force-stop", source)
        self.assertNotEqual(manifest["steps"][0]["launch_condition"], "already_ready")
        self.assertEqual(manifest["steps"][0]["launch_condition"], "process_stopped_before_launch")
        self.assertEqual(manifest["steps"][0]["launch_process_state"], "process_stopped_before_launch")
        self.assertEqual(manifest["steps"][0]["launch_temperature"], "process_restart")
        self.assertFalse(any(warning.get("code") == "SCREEN_PREPARATION_PRELAUNCHED_APP"
                             for warning in manifest["quality"]["warnings"]))

    def test_already_prepared_preserves_editor_and_skips_only_prior_launch(self):
        client = ResetScenarioClient("editor")
        manifest = self.execute(self.plan(mode="already_prepared"), client)
        self.assertEqual(manifest["status"], "succeeded", manifest["quality"])
        self.assertEqual([step["step_id"] for step in manifest["steps"]], ["step_02"])
        self.assertEqual(client.state, "editor")
        self.assertFalse(any(command.startswith(("aa force-stop ", "aa start ")) for command in client.commands))
        self.assertNotIn("uitest uiInput keyEvent Home", client.commands)

    def test_custom_app_keeps_its_measured_launch_without_gallery_reset(self):
        bundle = "org.example.reader"
        client = ResetScenarioClient("custom", bundle=bundle)
        manifest = self.execute(self.plan(selected="step_01", bundle=bundle), client)
        self.assertEqual(manifest["status"], "succeeded", manifest["quality"])
        self.assertFalse(any(command.startswith("aa force-stop ") for command in client.commands))
        self.assertNotIn("uitest uiInput keyEvent Home", client.commands)
        self.assertEqual(len(client.executed), 1)
        source = client.files[client.executed[0]].decode()
        self.assertIn("command_action LAUNCH aa start", source)
        self.assertIn(bundle, source)

    def test_cancel_during_reset_uses_cleanup_channel_and_never_starts_script(self):
        client = ResetScenarioClient("editor", cancel_on_stop=True)
        manifest = self.execute(self.plan(), client)
        self.assertEqual(manifest["status"], "cancelled", manifest["quality"])
        self.assertEqual(client.executed, [])
        self.assertFalse(any(command.startswith("aa start ") for command in client.commands))
        self.assertEqual(manifest["cleanup"]["status"], "completed")
        self.assertTrue(any(cleanup for _, _, cleanup in client.calls))
        release = [event for event in client.collector_session_events if event["action"] == "release"]
        self.assertEqual(len(release), 1)
        self.assertTrue(release[0]["cleanup"])
        self.assertIsNone(client.collector_session)

    def test_failed_reset_does_not_enter_captured_or_preparation_script(self):
        client = ResetScenarioClient("editor", fail_stop=True)
        manifest = self.execute(self.plan(), client)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(client.executed, [])
        self.assertFalse(any(command.startswith("aa start ") for command in client.commands))
        self.assertTrue(manifest["quality"]["errors"])

    def test_active_foreign_session_blocks_gallery_reset_and_retains_owner(self):
        client = ResetScenarioClient()
        owner = {"session_run": "capture_other", "session_token": "foreign-owner-token"}
        client.collector_session = owner
        manifest = self.execute(self.plan(), client)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["quality"]["errors"][0]["code"], "SCENARIO_COLLECTOR_SESSION_BUSY")
        self.assertEqual(manifest["cleanup"]["status"], "not_owned")
        self.assertEqual(client.collector_session, owner)
        self.assertEqual(client.executed, [])
        self.assertFalse(any(command.startswith(("aa force-stop ", "aa start ", "uitest "))
                             for command in client.commands))

    def test_unconfirmed_acquisition_never_restarts_app_or_runs_script(self):
        for response in ("HETERO_COLLECTOR_SESSION|pending|collector_recovery_incomplete", ""):
            with self.subTest(response=response):
                client = SessionResponseClient(replies={"acquire": response})
                manifest = self.execute(self.plan(), client)
                self.assertEqual(manifest["status"], "failed")
                self.assertEqual(client.executed, [])
                self.assertFalse(any(command.startswith(("aa force-stop ", "aa start "))
                                     for command in client.commands))
                self.assertNotEqual(manifest["cleanup"]["status"], "completed")

    def test_foreign_trace_is_not_stopped_and_blocks_reset_before_measurement(self):
        client = SessionResponseClient(trace_active=True)
        manifest = self.execute(self.plan(), client)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["quality"]["errors"][0]["code"], "SCENARIO_TRACE_ALREADY_ACTIVE")
        self.assertEqual(client.executed, [])
        self.assertFalse(any(command.startswith(("aa force-stop ", "aa start ", "hitrace --trace_finish"))
                             for command in client.commands))
        self.assertEqual(manifest["cleanup"]["status"], "completed")

    def test_failed_script_recovers_owned_collectors_before_output_recovery(self):
        client = ResetScenarioClient()
        client.fail_step = True
        manifest = self.execute(self.plan(), client)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(len(client.executed), 1)
        recover = next(event for event in client.collector_session_events if event["action"] == "recover")
        self.assertTrue(recover["cleanup"])
        operations = [event["action"] for event in client.collector_session_events]
        self.assertLess(operations.index("recover"), operations.index("release"))
        self.assertTrue(any(file["kind"] == "execution_status" for file in manifest["files"]))
        self.assertEqual(manifest["cleanup"]["status"], "completed")
        self.assertIsNone(client.collector_session)

    def test_pending_recovery_retains_session_and_does_not_claim_cleanup_completed(self):
        client = SessionResponseClient(replies={
            "recover": "HETERO_COLLECTOR_SESSION|pending|collector_recovery_incomplete",
            "release": "HETERO_COLLECTOR_SESSION|pending|collector_recovery_incomplete"})
        client.fail_step = True
        manifest = self.execute(self.plan(), client)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(len(client.executed), 1)
        self.assertEqual(manifest["cleanup"]["status"], "pending")
        self.assertTrue(manifest["cleanup"]["remote_lock_retained"])
        self.assertIsNotNone(client.collector_session)
        self.assertTrue(any(file["kind"] == "execution_status" for file in manifest["files"]))

    def test_unconfirmed_release_marks_successful_capture_failed_and_retains_lock(self):
        for index, reply in enumerate(("HETERO_COLLECTOR_SESSION|pending|foreign_trace", RuntimeError("bridge offline"))):
            with self.subTest(reply=str(reply)):
                client = SessionResponseClient(replies={"release": reply})
                manifest = self.execute(self.plan(), client, "release_" + str(index))
                self.assertEqual(len(client.executed), 2)
                self.assertEqual(manifest["steps"][-1]["status"], "succeeded")
                self.assertEqual(manifest["status"], "failed")
                self.assertEqual(manifest["cleanup"]["status"], "pending")
                self.assertTrue(manifest["cleanup"]["remote_lock_retained"])
                self.assertIsNotNone(client.collector_session)

    def test_legacy_lock_recovery_uses_only_matching_terminal_run_records(self):
        runs = self.manager.root / "runs"
        expected_bridge = set()
        expected_local = set()

        def write(index, **overrides):
            identity = "capture_" + f"{index:024x}"
            folder = runs / identity
            folder.mkdir(parents=True, exist_ok=True)
            value = {"run_id": identity, "project_id": self.manager.artifact_store.project_id,
                     "status": "failed", "connection_mode": "bridge", "bridge_id": "bridge_qa",
                     "device_serial": "qa_phone", **overrides}
            (folder / "run.json").write_text(json.dumps(value), encoding="utf-8")
            return identity, folder, value

        for index, status in enumerate(("failed", "cancelled", "succeeded", "partial"), 1):
            identity, _, _ = write(index, status=status)
            expected_bridge.add(identity)
        for index, status in enumerate(("running", "queued", "preparing", "state_unknown"), 10):
            write(index, status=status)
        write(20, project_id="another-project")
        write(21, bridge_id="bridge_another")
        write(22, device_serial="another_phone")
        write(23, connection_mode="local")  # Contradictory local + bridge identity is not reused.
        write(24, run_id="capture_" + "f" * 24)
        local_id, _, _ = write(25, connection_mode="local", bridge_id="")
        expected_local.add(local_id)
        legacy_id, folder, value = write(26)
        del value["connection_mode"]
        (folder / "run.json").write_text(json.dumps(value), encoding="utf-8")
        expected_bridge.add(legacy_id)
        _, folder, _ = write(27)
        (folder / "run.json").write_text("{unfinished", encoding="utf-8")
        _, folder, value = write(28)
        other = self.root / "other-record.json"
        other.write_text(json.dumps(value), encoding="utf-8")
        (folder / "run.json").unlink()
        (folder / "run.json").symlink_to(other)

        self.assertEqual(self.manager._terminal_device_run_ids(
            connection_mode="bridge", bridge_id="bridge_qa", device_serial="qa_phone"), expected_bridge)
        self.assertEqual(self.manager._terminal_device_run_ids(
            connection_mode="local", bridge_id="", device_serial="qa_phone"), expected_local)


if __name__ == "__main__":
    unittest.main()
