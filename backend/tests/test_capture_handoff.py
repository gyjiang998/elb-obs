"""Replay relock during upload and timeout restoration without device access."""
import copy
import json
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

from backend.core import ArtifactStore
from backend.scenario_capture import CaptureCancelled, ScenarioCaptureManager
from backend.tests.test_gallery_capture_preparation import page
from backend.tests.test_scenario_capture import SimulatedDeviceClient
from backend.tests.test_screen_preparation_budget import FakeClock
from tools.scenario_capture.runner import execute_plan


class HandoffDevice(SimulatedDeviceClient):
    def __init__(self, *, relock=False, stuck=False, cancel=False, locked=False, fail_restore=False):
        super().__init__()
        self.state = "locked" if locked else "desktop"
        self.override = None
        self.relock, self.stuck, self.cancel_on_upload = relock, stuck, cancel
        self.fail_restore = fail_restore
        self.uploaded = self.cancelled = False
        self.pid = "123"
        self.calls = []

    def check_cancelled(self):
        if self.cancelled:
            raise CaptureCancelled("cancelled during upload")

    def shell(self, command, timeout=40, *, cleanup=False):
        self.calls.append((command, cleanup))
        if not cleanup:
            self.check_cancelled()
        output = super().shell(command, timeout, cleanup=cleanup)
        if command == "power-shell dump -s":
            extra = "" if self.override is None else f" OverrideTimeout={self.override}ms"
            return "Current State: AWAKE\nScreenOffTime: Timeout=30000ms" + extra
        if command.startswith("power-shell timeout -o "):
            self.override = int(command.split()[-1])
        if command == "power-shell timeout -r":
            if self.fail_restore:
                return "failed to restore screen off time"
            self.override = None
        if command.startswith("aa force-stop "):
            self.state, self.pid = "desktop", ""
            return "force stop process successfully."
        if command.startswith("aa start "):
            if not self.stuck:
                self.state = "gallery_home"
                self.pid = "456"
            return "start ability successfully"
        if command == "uitest uiInput keyEvent Home":
            self.state = "desktop"
        if command.startswith("uitest dumpLayout"):
            self.files[shlex.split(command)[-1]] = json.dumps(page(self.state)).encode()
        if command.startswith("pidof "):
            return self.pid
        if command.startswith("printf %s ") and command.endswith("/capture.sh") and not self.uploaded:
            self.uploaded = True
            if self.relock:
                self.state = "locked"
            if self.cancel_on_upload:
                self.cancelled = True
        if command.startswith("sh "):
            assert self.override and self.override > 45000, "idle protection must span execution"
            assert self.state != "locked", "collectors must not start while locked"
            root = str(Path(shlex.split(command)[1]).parent)
            source = self.files[root + "/capture.sh"].decode()
            self.files[root + "/hilog_status.txt"] = b"stopped"
            self.files[root + "/hilog.log"] = b"test log\n"
            if "command_action LAUNCH aa start" in source:
                self.state = "gallery_home"
                self.pid = "789"
        return output


class MovingControlDevice(HandoffDevice):
    """Change a control after upload while retaining the same screen geometry."""

    def __init__(self, *, continuously=False):
        super().__init__()
        self.state = "gallery_home"
        self.continuously = continuously
        self.upload_count = 0

    def shell(self, command, timeout=40, *, cleanup=False):
        output = super().shell(command, timeout, cleanup=cleanup)
        if command.startswith("printf %s ") and command.endswith("/capture.sh") and shlex.split(command)[-2] == ">":
            self.upload_count += 1
        if command.startswith("uitest dumpLayout"):
            path = shlex.split(command)[-1]
            tree = json.loads(self.files[path])
            delta = 100 * (self.upload_count if self.continuously else min(self.upload_count, 1))
            for node in tree["children"]:
                if node["attributes"]["id"] == "TabPhotos":
                    node["attributes"]["bounds"] = f"[{40 + delta},200][{240 + delta},400]"
            self.files[path] = json.dumps(tree).encode()
        return output


class CaptureHandoffTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.manager = ScenarioCaptureManager(self.root, ArtifactStore(self.root), object())

    def run_capture(self, device, operation="wait", profile=None):
        plan = self.manager.create_plan({"template_id": "custom", "steps": [
            {"title": "capture", "operation": operation, "seconds": 0}],
            "profile": profile or {},
            "capture": {"selected_step_ids": ["step_01"], "collectors": ["hilog"]}})
        original = copy.deepcopy(plan)
        result = execute_plan(plan, device, self.root / "run", "capture_handoff", page_ready_timeout_seconds=0)
        self.assertEqual(plan, original)
        return result

    def test_valid_integral_float_step_budget_is_supported(self):
        result = self.run_capture(HandoffDevice(), profile={"step_timeout_seconds": 120.0})
        self.assertEqual(result["status"], "succeeded", result["quality"])
        self.assertIsInstance(result["screen_awake_guard"]["timeout_ms"], int)

    def test_ready_seven_steps_do_not_repeat_wakeup_or_idle_override(self):
        device = HandoffDevice()
        plan = self.manager.create_plan({"template_id": "custom", "steps": [
            {"title": f"step {index}", "operation": "wait", "seconds": 0} for index in range(7)],
            "capture": {"selected_step_ids": ["step_07"], "collectors": ["hilog"]}})
        result = execute_plan(plan, device, self.root / "ready_run", "capture_ready",
                              page_ready_timeout_seconds=0)
        self.assertEqual(result["status"], "succeeded", result["quality"])
        self.assertEqual(len(device.executed), 7)
        self.assertNotIn("sleep 1", device.commands)
        self.assertEqual(sum(command.startswith("power-shell timeout -o ") for command in device.commands), 1)
        self.assertEqual(result["screen_awake_guard"]["refresh_count"], 0)
        self.assertLessEqual(result["collector_preparation"]["lease_remote_refreshes"], 2)
        for step in result["steps"]:
            handoff = step["capture_handoffs"][0]
            self.assertEqual(handoff["screen_preparation"]["path"], "fresh_layout")
            self.assertNotIn("initial_display_wakeup", handoff)
            self.assertTrue(handoff["screen_preparation"]["already_ready"])
        self.assertIsNone(device.override)

    def test_slow_initial_preparation_and_handoff_each_exceed_45_seconds_and_complete(self):
        clock = FakeClock()

        class SlowUnlockDevice(HandoffDevice):
            def __init__(self):
                super().__init__(locked=True, relock=True, stuck=True)
                self.swipes = 0

            def shell(self, command, timeout=40, *, cleanup=False):
                # Every RPC completes inside its individual timeout. Several
                # layout/transfer rounds may nevertheless exceed 45 seconds.
                clock.advance(max(0, min(timeout, 8) - .1))
                was_uploaded = self.uploaded
                output = super().shell(command, timeout, cleanup=cleanup)
                if self.uploaded and not was_uploaded:
                    self.swipes = 0
                if command.startswith("uitest uiInput swipe "):
                    self.swipes += 1
                    if self.swipes == 3:
                        self.state = "gallery_home"
                return output

            def collect(self, remote, destination, *, timeout=600, cleanup=True):
                clock.advance(max(0, min(timeout, 8) - .1))
                return super().collect(remote, destination, timeout=timeout, cleanup=cleanup)

        device = SlowUnlockDevice()
        with patch("tools.scenario_capture.runner.time.monotonic", side_effect=clock.monotonic):
            result = self.run_capture(device)
        self.assertEqual(result["status"], "succeeded", result["quality"])
        step = result["steps"][0]
        for key in ("screen_preparation_timing", "screen_handoff_timing"):
            self.assertGreater(step[key]["elapsed_seconds"], 45)
            self.assertIsNone(step[key]["budget_seconds"])
            self.assertIsNone(step[key]["remaining_seconds"])
            self.assertEqual(step[key]["status"], "completed")
            self.assertTrue(all(1 <= operation["timeout_seconds"] <= 8 for operation in step[key]["operations"]))
        self.assertEqual(len(device.executed), 1)
        self.assertIsNone(device.override)

    def test_relock_during_upload_recovers_and_restages_new_pid_before_capture(self):
        device = HandoffDevice(relock=True)
        result = self.run_capture(device)
        self.assertEqual(result["status"], "succeeded", result["quality"])
        step = result["steps"][0]
        self.assertEqual(step["target_pid"], 456)
        self.assertEqual(len(step["capture_handoffs"]), 2)
        self.assertTrue(step["capture_handoffs"][0]["script_changed"])
        self.assertFalse(step["capture_handoffs"][1]["script_changed"])
        self.assertTrue(step["capture_handoffs"][0]["screen_preparation"]["application_launch_completed"])
        self.assertEqual(len(device.executed), 1)
        script = device.files[device.executed[0]].decode()
        self.assertIn("hilog -P 456", script)
        self.assertNotIn("timeout -o", script)
        self.assertNotIn("keyEvent Power", script)
        aa_index = next(i for i, cmd in enumerate(device.commands) if cmd.startswith("aa start "))
        self.assertLess(aa_index, device.commands.index("sh " + device.executed[0]))
        self.assertIsNone(device.override)
        self.assertEqual(result["screen_awake_guard"]["restore_status"], "completed")

    def run_control_capture(self, device):
        plan = self.manager.create_plan({"template_id": "custom", "steps": [
            {"title": "click", "operation": "click", "selector": {"id": "TabPhotos"}}],
            "capture": {"selected_step_ids": ["step_01"], "collectors": ["hilog"]}})
        return execute_plan(plan, device, self.root / "run", "capture_moving_control", page_ready_timeout_seconds=0)

    def test_control_movement_after_upload_restages_and_executes_only_new_coordinates(self):
        device = MovingControlDevice()
        result = self.run_control_capture(device)
        self.assertEqual(result["status"], "succeeded", result["quality"])
        self.assertEqual(device.upload_count, 2)
        self.assertEqual(len(device.executed), 1)
        step = result["steps"][0]
        self.assertEqual([item["script_changed"] for item in step["capture_handoffs"]], [True, False])
        script = device.files[device.executed[0]].decode()
        self.assertIn("command_action CLICK_TARGET_STEP_01 uitest uiInput click 240 300", script)
        self.assertNotIn("command_action CLICK_TARGET_STEP_01 uitest uiInput click 140 300", script)
        actions = json.loads((self.root / "run/01_action_01/resolved_actions.json").read_text())
        self.assertEqual(actions[0]["target"]["point"], [240, 300])
        self.assertIsNone(device.override)

    def test_continuously_moving_control_stops_before_collectors(self):
        device = MovingControlDevice(continuously=True)
        result = self.run_control_capture(device)
        self.assertEqual(result["status"], "failed", result["quality"])
        self.assertEqual(result["quality"]["errors"][0]["code"], "SCENARIO_CAPTURE_HANDOFF_UNSTABLE")
        self.assertEqual(device.upload_count, 2)
        self.assertEqual(device.executed, [])
        self.assertEqual([item["script_changed"] for item in result["steps"][0]["capture_handoffs"]], [True, True])
        self.assertIsNone(device.override)
        self.assertEqual(result["screen_awake_guard"]["restore_status"], "completed")

    def test_persistent_relock_stops_before_collectors_and_restores_timeout(self):
        device = HandoffDevice(relock=True, stuck=True)
        result = self.run_capture(device)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["quality"]["errors"][0]["code"], "SCENARIO_SCREEN_LOCKED")
        self.assertEqual(device.executed, [])
        self.assertIsNone(device.override)

    def test_cancelled_upload_restores_with_cleanup_path(self):
        device = HandoffDevice(cancel=True)
        result = self.run_capture(device)
        self.assertEqual(result["status"], "cancelled", result["quality"])
        self.assertEqual(device.executed, [])
        self.assertIsNone(device.override)
        self.assertIn(("power-shell timeout -r", True), device.calls)

    def test_initial_aa_then_gallery_reset_records_stopped_process_before_launch(self):
        device = HandoffDevice(locked=True)
        result = self.run_capture(device, "launch")
        self.assertEqual(result["status"], "succeeded", result["quality"])
        step = result["steps"][0]
        self.assertTrue(step["screen_preparation"]["application_launch_attempted"])
        self.assertTrue(step["gallery_reset"]["process_stop_verified"])
        self.assertEqual(step["launch_process_state"], "process_stopped_before_launch")
        self.assertEqual(step["launch_condition"], "process_stopped_before_launch")
        self.assertEqual(step["launch_temperature"], "process_restart")
        self.assertIsNone(step["target_pid"])
        self.assertEqual(step["hilog_scope"], "system_no_unique_preaction_pid")
        self.assertFalse(any(w.get("code") == "SCREEN_PREPARATION_PRELAUNCHED_APP" for w in result["quality"]["warnings"]))
        launches = [i for i, command in enumerate(device.commands) if command.startswith("aa start ")]
        stop = device.commands.index("aa force-stop com.huawei.hmos.photos")
        self.assertEqual(len(launches), 1)
        self.assertLess(launches[0], stop)
        self.assertLess(stop, device.commands.index("sh " + device.executed[0]))
        self.assertEqual(len(device.executed), 1)
        source = device.files[device.executed[0]].decode()
        self.assertIn("command_action LAUNCH aa start", source)
        self.assertNotIn("hilog -P 456", source)
        self.assertEqual(device.pid, "789")

    def test_handoff_aa_after_gallery_reset_keeps_prelaunch_warning_and_new_pid(self):
        device = HandoffDevice(locked=True, relock=True)
        result = self.run_capture(device, "launch")
        self.assertEqual(result["status"], "partial", result["quality"])
        step = result["steps"][0]
        self.assertTrue(step["gallery_reset"]["process_stop_verified"])
        self.assertEqual(step["launch_temperature"], "prelaunched_during_screen_preparation")
        self.assertEqual(step["launch_process_state"], "already_running")
        self.assertEqual(step["target_pid"], 456)
        self.assertTrue(step["capture_handoffs"][0]["screen_preparation"]["application_launch_completed"])
        self.assertEqual([entry["script_changed"] for entry in step["capture_handoffs"]], [True, False])
        self.assertTrue(any(w.get("code") == "SCREEN_PREPARATION_PRELAUNCHED_APP" for w in result["quality"]["warnings"]))
        launches = [i for i, command in enumerate(device.commands) if command.startswith("aa start ")]
        stop = device.commands.index("aa force-stop com.huawei.hmos.photos")
        self.assertEqual(len(launches), 2)
        self.assertLess(launches[0], stop)
        self.assertLess(stop, launches[1])
        self.assertEqual(device.commands.count("aa force-stop com.huawei.hmos.photos"), 1)
        self.assertEqual(len(device.executed), 1)
        source = device.files[device.executed[0]].decode()
        self.assertIn("command_action LAUNCH aa start", source)
        self.assertIn("hilog -P 456", source)

    def test_restore_failure_is_visible_and_does_not_skip_device_lock_cleanup(self):
        device = HandoffDevice(fail_restore=True)
        result = self.run_capture(device)
        self.assertEqual(result["status"], "partial", result["quality"])
        self.assertTrue(result["screen_awake_guard"]["restoration_needed"])
        self.assertTrue(any(w.get("code") == "SCREEN_TIMEOUT_RESTORE_FAILED" for w in result["quality"]["warnings"]))
        self.assertTrue(any("rmdir" in cmd and cleanup for cmd, cleanup in device.calls))


if __name__ == "__main__":
    unittest.main()
