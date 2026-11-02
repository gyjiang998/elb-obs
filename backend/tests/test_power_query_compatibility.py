"""Service-dump compatibility for real devices without power-shell dump."""
import json
from pathlib import Path
import unittest

from backend.scenario_capture import CaptureCancelled
from backend.tests.test_gallery_capture_preparation import page
from backend.tests.test_screen_awake import PowerClient
from tools.scenario_capture.gallery import CapturePreparationError
from tools.scenario_capture.screen_awake import ScreenAwakeGuard
from tools.scenario_capture.screen_preparation import prepare_unlocked_page, wake_display_before_capture


PRIMARY = "power-shell dump -s"
SERVICE = "hidumper -s 3301 -a -a"
POWER = "uitest uiInput keyEvent Power"
HELP = "usage: power-shell\ncommand list:\nwakeup : Wakeup system and turn screen on.\n"
LONG_HEADER = "other service diagnostic\n" * 250


class ServicePowerClient(PowerClient):
    def default(self, command):
        if command == PRIMARY:
            return HELP
        if command == SERVICE:
            return LONG_HEADER + super().default(PRIMARY)
        if command in (POWER, "sleep 1"):
            return "No Error"
        return super().default(command)


class PowerQueryCompatibilityTests(unittest.TestCase):
    def test_full_service_dump_establishes_awake_before_diagnostic_truncation(self):
        client, record = ServicePowerClient(), {}
        client.reply(SERVICE, LONG_HEADER + "Current State: DIM\nScreenOffTime: Timeout=600000ms OverrideTimeout=10000ms")
        result = wake_display_before_capture(client, record)
        self.assertEqual([item[0] for item in client.calls], [PRIMARY, SERVICE])
        self.assertEqual(result["power_state"], "DIM")
        self.assertTrue(result["power_awake"])
        self.assertFalse(result["power_key_attempted"])
        self.assertEqual(result["power_observations"][0]["status"], "unavailable")
        observation = result["power_observations"][1]
        self.assertEqual(observation["command"], ["hidumper", "-s", "3301", "-a", "-a"])
        self.assertEqual(len(observation["output"]), 2000)

    def test_confirmed_inactive_service_state_allows_one_power_key_then_fresh_query(self):
        client, record = ServicePowerClient(), {}
        client.reply(SERVICE, "Current State: INACTIVE", "Current State: AWAKE")
        result = wake_display_before_capture(client, record)
        self.assertEqual([item[0] for item in client.calls], [PRIMARY, SERVICE, POWER, "sleep 1", PRIMARY, SERVICE])
        self.assertTrue(result["power_key_attempted"])
        self.assertTrue(result["power_awake"])

    def test_usable_layout_still_checks_current_service_power_state(self):
        client = ServicePowerClient()
        client.reply(SERVICE, LONG_HEADER + "Current State: AWAKE")
        record = {"initial_display_wakeup": {"status": "completed", "state_before_wakeup": "INACTIVE"}}
        profile = json.loads((Path(__file__).parents[2] / "tools/scenario_capture/gallery_17_profile.json").read_text())
        prepare_unlocked_page(page("gallery_home"), profile, client, record,
                              lambda _: self.fail("Usable layout needs no gesture"), initial_page_fresh=True)
        self.assertEqual([item[0] for item in client.calls], [PRIMARY, SERVICE])
        self.assertTrue(record["screen_preparation"]["final_power_awake"])

    def test_service_timeout_readback_refresh_and_original_override_restoration(self):
        client, record = ServicePowerClient(timeout=600000, override=10000), {}
        guard = ScreenAwakeGuard(client, record, 300000)
        guard.acquire()
        self.assertTrue(record["owned"])
        self.assertEqual(record["original"], {"timeout_ms": 600000, "override_timeout_ms": 10000})
        self.assertEqual(client.override, 300000)
        guard.refresh()
        self.assertEqual(record["refresh_status"], "completed")
        guard.restore()
        self.assertEqual(client.override, 10000)
        self.assertEqual(record["restore_status"], "completed")
        queries = [(command, timeout, cleanup) for command, timeout, cleanup in client.calls if command in (PRIMARY, SERVICE)]
        self.assertEqual(len(queries), 12)
        self.assertEqual([command for command, _, _ in queries], [PRIMARY, SERVICE] * 6)
        self.assertTrue(all(timeout == 3 for _, timeout, _ in queries))
        self.assertTrue(all(cleanup for _, _, cleanup in queries[-4:]))
        self.assertTrue(all(len(item["output"]) <= 4000 for item in record["operations"]))

    def test_service_query_cancellation_and_deadline_stop_wake_immediately(self):
        for error in (CaptureCancelled("cancelled"), CapturePreparationError(
                "SCENARIO_SCREEN_PREPARATION_TIMEOUT", "deadline", "retry")):
            with self.subTest(error=type(error).__name__):
                client, record = ServicePowerClient(), {}
                client.reply(SERVICE, error)
                with self.assertRaises(type(error)) as caught:
                    wake_display_before_capture(client, record)
                self.assertIs(caught.exception, error)
                self.assertEqual([item[0] for item in client.calls], [PRIMARY, SERVICE])
                self.assertEqual(record["initial_display_wakeup"]["status"], "interrupted")

    def test_service_query_cancellation_stops_timeout_acquisition_before_write(self):
        client, record = ServicePowerClient(), {}
        client.reply(SERVICE, CaptureCancelled("cancelled"))
        guard = ScreenAwakeGuard(client, record, 300000)
        with self.assertRaises(CaptureCancelled):
            guard.acquire()
        self.assertEqual([item[0] for item in client.calls], [PRIMARY, SERVICE])
        self.assertFalse(record["restoration_needed"])


if __name__ == "__main__":
    unittest.main()
