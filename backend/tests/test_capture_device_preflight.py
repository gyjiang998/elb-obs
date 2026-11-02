"""Cross-scenario screen preparation replays; no host HDC or phone is used."""
from __future__ import annotations

import json
from pathlib import Path
import shlex
import tempfile
import unittest

from backend.core import ArtifactStore
from backend.scenario_capture import CaptureCancelled, ScenarioCaptureManager
from backend.tests.test_gallery_capture_preparation import node, page
from backend.tests.test_scenario_capture import SimulatedDeviceClient
from tools.scenario_capture.runner import execute_plan


TARGET_BUNDLE = "com.example.reader"
POWER = "uitest uiInput keyEvent Power"
QUERY = "power-shell dump -s"


class ScreenReplayDevice(SimulatedDeviceClient):
    """Replay power/UI state independently of command success responses."""

    def __init__(self, *, power="INACTIVE", state="locked", size=(1080, 2400),
                 power_works=True, swipe_works=True, relock_before_step=None,
                 cancel_after_power=False, query_output=None):
        super().__init__()
        self.power, self.state, self.size = power, state, size
        self.power_works, self.swipe_works = power_works, swipe_works
        self.relock_before_step = relock_before_step
        self.cancel_after_power, self.cancelled = cancel_after_power, False
        self.query_output = query_output
        self.script_entry_states = []

    def check_cancelled(self):
        if self.cancelled:
            raise CaptureCancelled("fixture cancelled after Power")

    def tree(self):
        tree = page(self.state if self.state != "target_app" else "other")
        if self.state == "target_app":
            tree["children"] = [node("ReaderContent", bundle=TARGET_BUNDLE)]
        tree["attributes"]["bounds"] = f"[0,0][{self.size[0]},{self.size[1]}]"
        return tree

    def shell(self, command, timeout=40, *, cleanup=False):
        result = super().shell(command, timeout, cleanup=cleanup)
        if self.relock_before_step and command.startswith("mkdir -p ") and \
                f"/{self.relock_before_step:02d}_action_" in command:
            self.state, self.power = "locked", "INACTIVE"
            self.relock_before_step = None
        if command == QUERY:
            return self.query_output if self.query_output is not None else f"Current State: {self.power}"
        if command == POWER:
            if self.power_works:
                self.power = "AWAKE" if self.power != "AWAKE" else "INACTIVE"
            if self.cancel_after_power:
                self.cancelled = True
        if command.startswith("uitest uiInput swipe ") and self.swipe_works and self.power == "AWAKE":
            self.state = "target_app"
        if command == "uitest uiInput keyEvent Home":
            self.state = "desktop"
        if command.startswith("uitest dumpLayout"):
            self.files[shlex.split(command)[-1]] = json.dumps(self.tree()).encode()
        if command.startswith("sh "):
            self.script_entry_states.append((self.power, self.state))
        return result


class CaptureDevicePreflightTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.manager = ScenarioCaptureManager(self.root, ArtifactStore(self.root), object())
        self.run_count = 0

    def plan(self, *, steps=1, mode="auto", collectors=None):
        return self.manager.create_plan({
            "template_id": "custom",
            "profile": {"bundle": TARGET_BUNDLE, "ability": "ReaderAbility"},
            "steps": [{"title": f"阅读步骤 {index + 1}", "operation": "wait", "seconds": 0}
                      for index in range(steps)],
            "capture": {"selected_step_ids": [f"step_{steps:02d}"], "preparation_mode": mode,
                        "collectors": collectors or ["trace", "pmu"]},
        })

    def run_plan(self, device, **kwargs):
        self.run_count += 1
        return execute_plan(self.plan(**kwargs), device, self.root / f"run_{self.run_count}",
                            f"capture_replay_{self.run_count}", page_ready_timeout_seconds=0)

    def test_power_wait_and_slow_swipe_prepare_non_gallery_before_any_capture_script(self):
        device = ScreenReplayDevice()
        manifest = self.run_plan(device)
        self.assertIn(manifest["status"], {"succeeded", "partial"}, manifest["quality"])
        self.assertEqual(device.script_entry_states, [("AWAKE", "target_app")])
        commands = device.commands
        power_index = commands.index(POWER)
        wait_index = commands.index("sleep 1", power_index)
        swipe_index = next(i for i, command in enumerate(commands) if command.startswith("uitest uiInput swipe "))
        script_index = commands.index("sh " + device.executed[0])
        self.assertLess(commands.index(QUERY), power_index)
        self.assertLess(power_index, wait_index)
        self.assertLess(wait_index, swipe_index)
        self.assertLess(swipe_index, script_index)
        self.assertEqual(commands.count(POWER), 1)
        self.assertNotIn("uitest uiInput keyEvent Home", commands)
        source = device.files[device.executed[0]].decode()
        self.assertIn("hitrace --trace_begin", source)
        self.assertIn("pmu_state=unavailable", source)
        self.assertIn("begin GALLERY_ACTION_01", source)
        self.assertNotIn("keyEvent Power", source)
        self.assertNotIn("uiInput swipe", source)
        self.assertNotIn("aa start", source)
        self.assertEqual(manifest["steps"][0]["screen_preparation_timing"]["status"], "completed")

    def test_slow_gesture_tracks_actual_device_size_including_smaller_screens(self):
        for size in ((720, 1600), (1080, 2400), (1440, 3120)):
            with self.subTest(size=size):
                device = ScreenReplayDevice(size=size)
                manifest = self.run_plan(device)
                self.assertIn(manifest["status"], {"succeeded", "partial"}, manifest["quality"])
                swipes = [shlex.split(command) for command in device.commands
                          if command.startswith("uitest uiInput swipe ")]
                self.assertEqual(len(swipes), 1)
                x1, y1, x2, y2, velocity = map(int, swipes[0][3:])
                self.assertEqual(x1, x2)
                self.assertAlmostEqual(x1 / size[0], .5, delta=.003)
                self.assertTrue(0 <= x1 < size[0] and 0 <= y2 < y1 < size[1])
                self.assertAlmostEqual((y1 - y2) / velocity, 1.9, delta=.01)
                self.assertEqual(manifest["screen_geometry"]["actual_size"], list(size))

    def test_already_ready_app_is_verified_without_power_or_navigation(self):
        device = ScreenReplayDevice(power="AWAKE", state="target_app")
        manifest = self.run_plan(device)
        self.assertIn(manifest["status"], {"succeeded", "partial"}, manifest["quality"])
        self.assertEqual(device.script_entry_states, [("AWAKE", "target_app")])
        self.assertTrue(any(command.startswith("uitest dumpLayout") for command in device.commands))
        self.assertFalse(any(command.startswith("uitest uiInput") for command in device.commands))

    def test_already_prepared_mode_still_unlocks_without_replaying_previous_steps(self):
        device = ScreenReplayDevice()
        manifest = self.run_plan(device, steps=3, mode="already_prepared")
        self.assertIn(manifest["status"], {"succeeded", "partial"}, manifest["quality"])
        self.assertEqual([step["step_id"] for step in manifest["steps"]], ["step_03"])
        self.assertEqual(len(device.executed), 1)
        self.assertEqual(device.script_entry_states, [("AWAKE", "target_app")])
        self.assertEqual(device.commands.count(POWER), 1)
        self.assertNotIn("uitest uiInput keyEvent Home", device.commands)

    def test_each_step_is_prepared_when_screen_relocks_between_scripts(self):
        device = ScreenReplayDevice(power="AWAKE", state="target_app", relock_before_step=2)
        manifest = self.run_plan(device, steps=2)
        self.assertIn(manifest["status"], {"succeeded", "partial"}, manifest["quality"])
        self.assertEqual(device.script_entry_states, [("AWAKE", "target_app"), ("AWAKE", "target_app")])
        first = device.commands.index("sh " + device.executed[0])
        second = device.commands.index("sh " + device.executed[1])
        self.assertNotIn(POWER, device.commands[:first])
        self.assertIn(POWER, device.commands[first:second])
        self.assertEqual([step["screen_preparation_timing"]["status"] for step in manifest["steps"]],
                         ["completed", "completed"])

    def test_ready_custom_steps_each_run_the_shared_gate(self):
        device = ScreenReplayDevice(power="AWAKE", state="target_app")
        manifest = self.run_plan(device, steps=2)
        self.assertIn(manifest["status"], {"succeeded", "partial"}, manifest["quality"])
        first = device.commands.index("sh " + device.executed[0])
        second = device.commands.index("sh " + device.executed[1])
        self.assertIn(QUERY, device.commands[:first])
        self.assertIn(QUERY, device.commands[first:second])
        self.assertTrue(all(step["screen_preparation"]["already_ready"] for step in manifest["steps"]))
        self.assertTrue(all(step["initial_display_wakeup"]["power_awake"] for step in manifest["steps"]))
        self.assertNotIn(POWER, device.commands)

    def test_default_gallery_template_uses_same_gate_and_keeps_measured_launch(self):
        class GalleryTemplateDevice(ScreenReplayDevice):
            def shell(self, command, timeout=40, *, cleanup=False):
                result = super().shell(command, timeout, cleanup=cleanup)
                if command.startswith("sh "):
                    self.state = "gallery_home"
                return result

        device = GalleryTemplateDevice()
        plan = self.manager.create_plan({"capture": {
            "selected_step_ids": ["step_01"], "collectors": ["trace"]}})
        manifest = execute_plan(plan, device, self.root / "gallery_template", "capture_template",
                                page_ready_timeout_seconds=0)
        self.assertIn(manifest["status"], {"succeeded", "partial"}, manifest["quality"])
        script_index = device.commands.index("sh " + device.executed[0])
        self.assertLess(device.commands.index(POWER), script_index)
        self.assertTrue(any(command.startswith("uitest uiInput swipe ")
                            for command in device.commands[:script_index]))
        self.assertIn("command_action LAUNCH aa start", device.files[device.executed[0]].decode())
        self.assertEqual(manifest["steps"][0]["screen_preparation"]["status"], "verified")

    def test_custom_launch_from_foreground_target_app_does_not_require_gallery_controls(self):
        device = ScreenReplayDevice(power="AWAKE", state="target_app")
        plan = self.manager.create_plan({
            "template_id": "custom",
            "profile": {"bundle": TARGET_BUNDLE, "ability": "ReaderAbility"},
            "steps": [{"title": "拉起阅读器", "operation": "launch"}],
            "capture": {"selected_step_ids": ["step_01"], "collectors": ["trace"]},
        })
        manifest = execute_plan(plan, device, self.root / "reader_launch", "capture_reader_launch",
                                page_ready_timeout_seconds=0)
        self.assertIn(manifest["status"], {"succeeded", "partial"}, manifest["quality"])
        source = device.files[device.executed[0]].decode()
        self.assertIn("command_action LAUNCH aa start -a ReaderAbility -b " + TARGET_BUNDLE, source)
        self.assertIn("hitrace --trace_begin", source)
        self.assertEqual(manifest["steps"][0]["launch_temperature"], "warm_or_resume")
        self.assertNotIn("uitest uiInput keyEvent Home", device.commands)
        self.assertFalse(any(command.startswith("uitest uiInput click") for command in device.commands))

    def test_power_is_not_repeated_when_device_remains_off_and_script_never_starts(self):
        device = ScreenReplayDevice(power_works=False)
        manifest = self.run_plan(device)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(device.commands.count(POWER), 1)
        self.assertEqual(device.executed, [])
        self.assertEqual(manifest["steps"][0]["error"]["code"], "SCENARIO_DISPLAY_NOT_AWAKE")

    def test_unknown_power_state_never_authorizes_toggle(self):
        device = ScreenReplayDevice(query_output="Previous State: AWAKE\nPrevious State: SLEEP")
        manifest = self.run_plan(device)
        self.assertEqual(manifest["status"], "failed")
        self.assertNotIn(POWER, device.commands)
        self.assertEqual(device.executed, [])

    def test_cancel_after_power_prevents_swipe_and_capture(self):
        device = ScreenReplayDevice(cancel_after_power=True)
        manifest = self.run_plan(device)
        self.assertEqual(manifest["status"], "cancelled")
        self.assertEqual(device.commands.count(POWER), 1)
        self.assertFalse(any(command.startswith("uitest uiInput swipe ") for command in device.commands))
        self.assertEqual(device.executed, [])

    def test_visible_credential_prompt_stops_before_any_capture_script(self):
        device = ScreenReplayDevice(power="AWAKE", state="locked_credentials")
        manifest = self.run_plan(device)
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(manifest["steps"][0]["error"]["code"], "SCENARIO_UNLOCK_AUTH_REQUIRED")
        self.assertFalse(any(command.startswith("uitest uiInput") for command in device.commands))
        self.assertEqual(device.executed, [])


if __name__ == "__main__":
    unittest.main()
