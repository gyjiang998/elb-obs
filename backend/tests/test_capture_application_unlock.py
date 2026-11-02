"""Foreground-Ability unlock compatibility with synthetic UI, never a phone."""
from copy import deepcopy
import shlex
import unittest

from backend.scenario_capture import CaptureCancelled
from backend.tests.test_capture_back_usb import BACK, UiDevice, ui_page
from tools.scenario_capture.gallery import CapturePreparationError
from tools.scenario_capture.screen_preparation import prepare_capture_device, prepare_unlocked_page


class ApplicationUnlockTests(unittest.TestCase):
    def setUp(self):
        self.profile = {"bundle": "com.example.reader", "ability": "ReaderAbility",
                        "screen_size": [1280, 2832], "targets": {}}
        self.record = {}
        self.command = "aa start -a ReaderAbility -b com.example.reader"

    def run_gate(self, client):
        return prepare_capture_device(self.profile, client, self.record, client.read)

    def test_only_ability_start_succeeds_after_power_command_rejection(self):
        def respond(client, command):
            if command == "power-shell dump -s":
                return "Current State: AWAKE" if self.command in client.commands else "Current State: INACTIVE"
            if command == "uitest uiInput keyEvent Power":
                return "Error: unsupported command"
            if command == self.command:
                client.tree = ui_page("desktop")
                return "start ability successfully."
        client = UiDevice(ui_page("locked"), respond)
        self.run_gate(client)
        self.assertEqual(client.commands.count("power-shell dump -s"), 2)
        self.assertEqual(client.commands.count(self.command), 1)
        self.assertFalse(any("swipe" in command or "keyEvent Back" in command for command in client.commands))
        status = self.record["screen_preparation"]
        self.assertEqual(status["status"], "verified")
        self.assertTrue(status["application_foreground_verified"])
        self.assertTrue(status["application_launch_completed"])
        launch = next(item for item in self.record["outside_capture_preparation"]
                      if item["action"] == "foreground_target_application")
        self.assertTrue(launch["before_capture"])
        self.assertEqual(launch["bundle"], self.profile["bundle"])
        self.assertIn("started_or_resumed", launch["launch_effect"])

    def test_previous_inactive_power_observation_does_not_override_new_unlocked_ui(self):
        self.record["initial_display_wakeup"] = {"status": "failed", "power_awake": False}
        def respond(client, command):
            if command == self.command:
                client.tree = ui_page("desktop")
        client = UiDevice(ui_page("locked"), respond)
        prepare_unlocked_page(deepcopy(client.tree), self.profile, client, self.record, client.read,
                              settle_seconds=0)
        self.assertEqual(self.record["screen_preparation"]["final_state"], "desktop")

    def test_app_layout_while_display_remains_inactive_is_not_success(self):
        def respond(client, command):
            if command == "power-shell dump -s":
                return "Current State: INACTIVE"
            if command == self.command:
                client.tree = ui_page("desktop")
        client = UiDevice(ui_page("locked"), respond)
        with self.assertRaises(CapturePreparationError) as raised:
            self.run_gate(client)
        self.assertEqual(raised.exception.code, "SCENARIO_DISPLAY_NOT_AWAKE")
        self.assertEqual(client.commands.count(self.command), 1)
        self.assertIn("power-shell wakeup", client.commands)
        self.assertIn("uinput -K -d 2802 -u 2802", client.commands)

    def test_successful_exit_with_still_locked_ui_never_starts_capture(self):
        client = UiDevice(ui_page("locked"))
        with self.assertRaises(CapturePreparationError) as raised:
            self.run_gate(client)
        self.assertEqual(raised.exception.code, "SCENARIO_SCREEN_LOCKED")
        self.assertFalse(self.record["screen_preparation"]["application_foreground_verified"])
        self.assertEqual(client.commands.count(self.command), 1)
        self.assertEqual(sum("uiInput swipe" in command for command in client.commands), 3)

    def test_ability_failure_keeps_other_unlock_methods_available(self):
        def respond(client, command):
            if command == self.command:
                return "error: start ability failed"
            if command.startswith("uitest uiInput swipe"):
                client.tree = ui_page("desktop")
        client = UiDevice(ui_page("locked"), respond)
        self.run_gate(client)
        self.assertEqual(self.record["screen_preparation"]["status"], "verified")
        self.assertFalse(self.record["screen_preparation"]["application_launch_completed"])
        self.assertIn("failed", self.record["screen_preparation"]["attempt_failures"][0]["reason"])

    def test_ability_start_then_usb_dialog_is_dismissed_and_verified(self):
        def respond(client, command):
            if command == self.command:
                client.tree = ui_page("usb")
            if command == BACK:
                client.tree = ui_page("desktop")
        client = UiDevice(ui_page("locked"), respond)
        self.run_gate(client)
        self.assertEqual(self.record["screen_preparation"]["status"], "verified")
        self.assertEqual(client.commands.count(BACK), 1)
        self.assertFalse(any("swipe" in command for command in client.commands))

    def test_existing_usable_page_is_not_relaunched(self):
        client = UiDevice(ui_page("gallery_browser"))
        self.run_gate(client)
        self.assertNotIn(self.command, client.commands)
        self.assertTrue(self.record["screen_preparation"]["already_ready"])

    def test_credentials_are_not_touched_before_or_after_ability_launch(self):
        for initial in ("credentials", "locked"):
            with self.subTest(initial=initial):
                self.record = {}
                def respond(client, command):
                    if command == self.command:
                        client.tree = ui_page("credentials")
                client = UiDevice(ui_page(initial), respond)
                with self.assertRaises(CapturePreparationError) as raised:
                    self.run_gate(client)
                self.assertEqual(raised.exception.code, "SCENARIO_UNLOCK_AUTH_REQUIRED")
                self.assertEqual(client.commands.count(self.command), 0 if initial == "credentials" else 1)
                self.assertFalse(any("swipe" in command or "keyEvent Back" in command for command in client.commands))

    def test_cancellation_during_ability_start_stops_without_more_input(self):
        def respond(_client, command):
            if command == self.command:
                raise CaptureCancelled("cancelled while starting Ability")
        client = UiDevice(ui_page("locked"), respond)
        with self.assertRaises(CaptureCancelled):
            self.run_gate(client)
        self.assertEqual(client.commands[-1], self.command)
        self.assertEqual(self.record["screen_preparation"]["status"], "cancelled")

    def test_profile_launch_arguments_are_shell_quoted(self):
        self.profile.update(bundle="com.example.reader; echo bad", ability="Reader Ability")
        def respond(client, command):
            if command.startswith("aa start "):
                self.assertEqual(shlex.split(command), ["aa", "start", "-a", "Reader Ability", "-b",
                                                       "com.example.reader; echo bad"])
                client.tree = ui_page("desktop")
        self.run_gate(UiDevice(ui_page("locked"), respond))

    def test_gallery_missing_ability_uses_confirmed_alias_in_run_local_profile(self):
        self.profile.update(bundle="com.huawei.hmos.photos", ability="com.huawei.hmos.photos.MainAbility")
        original = deepcopy(self.profile)
        alias = "aa start -a com.huawei.photos.MainAbility -b com.huawei.hmos.photos"
        def respond(client, command):
            if command.startswith("aa start "):
                if command == alias:
                    client.tree = ui_page("desktop")
                    return "start ability successfully."
                return "error: resolve ability err."
        client = UiDevice(ui_page("locked"), respond)
        self.run_gate(client)
        starts = [command for command in client.commands if command.startswith("aa start ")]
        self.assertEqual(starts, ["aa start -a com.huawei.hmos.photos.MainAbility -b com.huawei.hmos.photos", alias])
        self.assertEqual(self.profile["ability"], "com.huawei.photos.MainAbility")
        self.assertEqual(original["ability"], "com.huawei.hmos.photos.MainAbility")
        self.assertEqual(self.record["screen_preparation"]["configured_launch_ability"], original["ability"])
        self.assertEqual(self.record["screen_preparation"]["resolved_launch_ability"], "com.huawei.photos.MainAbility")
        attempts = [item for item in self.record["outside_capture_preparation"]
                    if item["action"] == "foreground_target_application"]
        self.assertFalse(attempts[0]["compatibility_alias"])
        self.assertTrue(attempts[1]["compatibility_alias"])

    def test_gallery_alias_is_not_used_for_successful_command_or_non_resolution_errors(self):
        self.profile.update(bundle="com.huawei.hmos.photos", ability="com.huawei.hmos.photos.MainAbility")
        for reply in ("start ability successfully.", "error: permission denied", "error: operation timed out"):
            with self.subTest(reply=reply):
                self.record = {}
                def respond(client, command):
                    if command.startswith("aa start "):
                        return reply
                    if command.startswith("uitest uiInput swipe"):
                        client.tree = ui_page("desktop")
                client = UiDevice(ui_page("locked"), respond)
                self.run_gate(client)
                self.assertEqual(sum(command.startswith("aa start ") for command in client.commands), 1)

    def test_non_gallery_resolution_failure_does_not_launch_gallery_alias(self):
        def respond(client, command):
            if command == self.command:
                return "error: ability does not exist"
            if command.startswith("uitest uiInput swipe"):
                client.tree = ui_page("desktop")
        client = UiDevice(ui_page("locked"), respond)
        self.run_gate(client)
        self.assertEqual([command for command in client.commands if command.startswith("aa start ")], [self.command])


if __name__ == "__main__":
    unittest.main()
