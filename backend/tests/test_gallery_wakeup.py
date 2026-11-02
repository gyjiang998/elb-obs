"""Wakeup/state-query regressions with scripted RPCs and fresh UI fixtures."""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from backend.scenario_capture import CaptureCancelled
from backend.tests.test_gallery_capture_preparation import page
from tools.scenario_capture.gallery import (
    CapturePreparationError, parse_power_state_dump, prepare_unlocked_page,
    screen_state, wake_display_before_capture,
)


WAKE = "power-shell wakeup"
QUERY = "power-shell dump -s"
SERVICE_QUERY = "hidumper -s 3301 -a -a"
FALLBACK = "uinput -K -d 2802 -u 2802"
POWER = "uitest uiInput keyEvent Power"


class WakeClient:
    def __init__(self, responses=()):
        self.responses = iter(responses)
        self.commands = []

    def check_cancelled(self):
        pass

    def shell(self, command, timeout=40, *, cleanup=False):
        self.commands.append((command, timeout))
        # These fixtures describe preferred-query and action responses. Their
        # older service CLI is unavailable; do not consume an action response.
        if command == SERVICE_QUERY:
            return "unknown command"
        result = next(self.responses, "")
        if isinstance(result, Exception):
            raise result
        return result


class GalleryWakeupTests(unittest.TestCase):
    def setUp(self):
        self.profile = json.loads((Path(__file__).parents[2] / "tools/scenario_capture/gallery_17_profile.json").read_text())

    def wake(self, responses):
        client, record = WakeClient(responses), {}
        result = wake_display_before_capture(client, record)
        self.assertIs(result, record["initial_display_wakeup"])
        self.assertIs(result, record["outside_capture_preparation"][0])
        return client, record, result

    def test_only_current_power_state_counts_not_history_or_success_text(self):
        for current, awake in (("AWAKE", True), ("DIM", True), ("SLEEP", False),
                               ("INACTIVE", False), ("DOZE", False), ("FREEZE", None)):
            with self.subTest(current=current):
                result = parse_power_state_dump(f"Power Dump result:\nCurrent State: {current}  Reason: 1\n"
                                               "DUMP EACH STATES:\nState: AWAKE\nState: SLEEP\n")
                self.assertEqual(result, {"power_state": current, "power_awake": awake})
        for output in ("", "WakeupDevice is called", "State: AWAKE", "Current State: AWAKE\nCurrent State: SLEEP"):
            self.assertEqual(parse_power_state_dump(output), {"power_state": "UNKNOWN", "power_awake": None})

    def test_standard_wake_uses_short_readonly_query_and_preserves_failure_history(self):
        client, _, result = self.wake(["", "WakeupDevice is called", "Current State: AWAKE\nFailure: display error\n"])
        self.assertEqual(client.commands, [(QUERY, 3), (SERVICE_QUERY, 3), (WAKE, 8), (QUERY, 3)])
        self.assertEqual(result["status"], "completed")
        self.assertIs(result["power_awake"], True)
        self.assertFalse(result["fallback_attempted"])

    def test_unknown_query_never_claims_awake_or_injects_another_key(self):
        for response in ("", "power-shell: unknown command", RuntimeError("query permission denied")):
            with self.subTest(response=str(response)):
                client, _, result = self.wake([response, "", response])
                self.assertEqual([c for c, _ in client.commands], [QUERY, SERVICE_QUERY, WAKE, QUERY, SERVICE_QUERY])
                self.assertEqual(result["status"], "completed")
                self.assertIsNone(result["power_awake"])
                self.assertFalse(result["fallback_attempted"])

    def test_explicit_wake_failure_uses_paired_wakeup_key_once(self):
        for failure in ("power-shell: permission denied", RuntimeError("Remote shell exit 127: power-shell: not found")):
            with self.subTest(failure=str(failure)):
                client, _, result = self.wake(["", failure, "", "", "Current State: AWAKE"])
                self.assertEqual([c for c, _ in client.commands], [QUERY, SERVICE_QUERY, WAKE, QUERY, SERVICE_QUERY, FALLBACK, QUERY])
                self.assertTrue(result["fallback_attempted"])
                self.assertEqual(result["attempts"][0]["status"], "failed")
                self.assertEqual(result["attempts"][1]["status"], "completed")
                self.assertEqual(result["status"], "completed")
                self.assertIs(result["power_awake"], True)

    def test_successful_standard_command_with_inactive_power_also_uses_fallback(self):
        client, _, result = self.wake(["", "WakeupDevice is called", "Current State: INACTIVE", "", "Current State: DIM"])
        self.assertEqual([c for c, _ in client.commands], [QUERY, SERVICE_QUERY, WAKE, QUERY, FALLBACK, QUERY])
        self.assertEqual(result["fallback_reason"], "power_still_inactive")
        self.assertIs(result["power_awake"], True)

    def test_unknown_after_fallback_does_not_reuse_old_inactive_state(self):
        _, _, result = self.wake(["", "", "Current State: INACTIVE", "", ""])
        self.assertIsNone(result["power_awake"])
        self.assertEqual(result["power_state"], "UNKNOWN")
        self.assertFalse(any("keyEvent Power" in " ".join(a["command"]) for a in result["attempts"]))

    def test_old_inactive_observation_does_not_prevent_application_and_gesture_fallbacks(self):
        client, record, result = self.wake(["", "", "Current State: INACTIVE", "", "Current State: SLEEP"])
        self.assertEqual(result["status"], "failed")
        with self.assertRaises(CapturePreparationError) as caught:
            prepare_unlocked_page(page("locked"), self.profile, client, record,
                                  lambda _: page("locked"), settle_seconds=0)
        self.assertEqual(caught.exception.code, "SCENARIO_SCREEN_LOCKED")
        self.assertEqual([c for c, _ in client.commands[:6]], [QUERY, SERVICE_QUERY, WAKE, QUERY, FALLBACK, QUERY])
        self.assertTrue(any(command.startswith("aa start ") for command, _ in client.commands[6:]))
        self.assertEqual(sum(command == WAKE for command, _ in client.commands), 1)
        self.assertEqual(record["screen_preparation"]["status"], "failed")

    def test_fresh_known_usable_ui_can_continue_but_unknown_ui_cannot(self):
        for state in ("gallery_home", "gallery_browser", "desktop", "other"):
            with self.subTest(state=state):
                client, record, result = self.wake(["", "permission denied", "", "permission denied", ""])
                if state == "other":
                    with self.assertRaises(CapturePreparationError):
                        prepare_unlocked_page(page(state), self.profile, client, record,
                                              lambda _: page(state), settle_seconds=0)
                else:
                    tree = prepare_unlocked_page(page(state), self.profile, client, record, lambda _: self.fail("Unexpected read"))
                    self.assertEqual(screen_state(tree, self.profile), state)
                    self.assertEqual(result["continued_on_verified_page"], state)
                self.assertEqual(len(client.commands), 9 if state == "other" else 8)

    def test_fresh_usable_page_after_waking_does_not_replay_unlock(self):
        client, record, result = self.wake(["", "permission denied", "", "permission denied", ""])
        tree = prepare_unlocked_page(page("locked"), self.profile, client, record,
                                     lambda _: page("gallery_browser"), settle_seconds=0)
        self.assertEqual(screen_state(tree, self.profile), "gallery_browser")
        self.assertEqual(result["continued_on_verified_page"], "gallery_browser")
        self.assertEqual(len(client.commands), 8)

    def test_power_awake_does_not_bypass_credential_ui(self):
        client, record, _ = self.wake(["Current State: AWAKE"])
        with self.assertRaises(CapturePreparationError) as caught:
            prepare_unlocked_page(page("locked_credentials"), self.profile, client, record,
                                  lambda _: self.fail("Credentials require no navigation"))
        self.assertEqual(caught.exception.code, "SCENARIO_UNLOCK_AUTH_REQUIRED")
        self.assertEqual(len(client.commands), 1)

    def test_confirmed_inactive_display_gets_power_once_then_settle_and_requery(self):
        client, _, result = self.wake(["Current State: INACTIVE", "No Error", "", "Current State: AWAKE"])
        self.assertEqual([c for c, _ in client.commands], [QUERY, POWER, "sleep 1", QUERY])
        self.assertTrue(result["power_key_attempted"])
        self.assertEqual(result["settled_seconds"], 1)
        self.assertIs(result["power_awake"], True)
        self.assertFalse(result["fallback_attempted"])

    def test_awake_display_requires_no_wakeup_or_toggle(self):
        for state in ("AWAKE", "DIM"):
            with self.subTest(state=state):
                client, _, result = self.wake([f"Current State: {state}"])
                self.assertEqual([c for c, _ in client.commands], [QUERY])
                self.assertEqual(result["status"], "completed")
                self.assertFalse(result["power_key_attempted"])

    def test_power_failure_uses_non_toggle_fallback_and_does_not_repeat_power(self):
        client, _, result = self.wake([
            "Current State: INACTIVE", "Error: unknown command", "", "Current State: INACTIVE",
            "", "Current State: INACTIVE", "", "Current State: AWAKE",
        ])
        self.assertEqual([c for c, _ in client.commands], [QUERY, POWER, "sleep 1", QUERY, WAKE, QUERY, FALLBACK, QUERY])
        self.assertEqual(result["attempts"][0]["status"], "failed")
        self.assertIs(result["power_awake"], True)
        self.assertEqual(result["status"], "completed")

    def test_deadline_and_cancellation_interrupt_every_wake_phase_without_fallback_or_query(self):
        phases = ([], [""], ["", ""], ["", "", "Current State: INACTIVE"],
                  ["", "", "Current State: INACTIVE", ""], ["Current State: INACTIVE"],
                  ["Current State: INACTIVE", ""], ["Current State: INACTIVE", "", ""])
        for before in phases:
            for error in (CaptureCancelled("cancelled"), CapturePreparationError(
                    "SCENARIO_SCREEN_PREPARATION_TIMEOUT", "deadline", "retry", phase="preparation")):
                with self.subTest(phase=len(before), error=type(error).__name__):
                    client, record = WakeClient([*before, error]), {}
                    with self.assertRaises(type(error)) as caught:
                        wake_display_before_capture(client, record)
                    self.assertIs(caught.exception, error)
                    action_and_primary = [command for command, _ in client.commands if command != SERVICE_QUERY]
                    self.assertEqual(len(action_and_primary), len(before) + 1)
                    self.assertEqual(record["initial_display_wakeup"]["status"], "interrupted")

    def test_deadline_is_preserved_during_unlock_command_and_layout_wait(self):
        for during in ("back", "layout", "sleep"):
            with self.subTest(during=during):
                error = CapturePreparationError("SCENARIO_SCREEN_PREPARATION_TIMEOUT", "deadline", "retry")
                client = WakeClient([error])
                record = {"initial_display_wakeup": {"status": "completed"}}
                def read(_):
                    if during == "layout":
                        raise error
                    return page("locked")
                with self.assertRaises(CapturePreparationError) as caught:
                    prepare_unlocked_page(page("locked"), self.profile, client, record, read,
                                          settle_seconds=0.35 if during == "sleep" else 0)
                self.assertIs(caught.exception, error)
                self.assertEqual(len(client.commands), 0 if during == "layout" else 1)


if __name__ == "__main__":
    unittest.main()
