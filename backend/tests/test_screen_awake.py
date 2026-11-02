"""Idle timeout protection, uncertain writes, and cancellation-safe restoration."""
from __future__ import annotations

from collections import deque
import unittest
from unittest.mock import patch

from backend.scenario_capture import CaptureCancelled
from tools.scenario_capture.power_query import query_power_compat
from tools.scenario_capture.screen_awake import ScreenAwakeGuard, parse_screen_off_timeout


QUERY = "power-shell dump -s"
SERVICE_QUERY = "hidumper -s 3301 -a -a"
SET = "power-shell timeout -o 180000"
RESTORE = "power-shell timeout -r"


class PowerClient:
    def __init__(self, *, timeout=30000, override=None):
        self.timeout, self.override = timeout, override
        self.responses = {}
        self.calls = []
        self.cancelled = False

    def reply(self, command, *responses):
        self.responses[command] = deque(responses)

    def check_cancelled(self):
        if self.cancelled:
            raise CaptureCancelled("fixture cancelled")

    def default(self, command):
        if command == QUERY:
            override = f"  OverrideTimeout={self.override}ms" if self.override is not None else ""
            return f"Current State: AWAKE\nScreenOffTime: Timeout={self.timeout}ms{override}\n"
        if command == SERVICE_QUERY:
            return "unknown command"
        if command == RESTORE:
            self.override = None
            return "Restore screen off time\n"
        if command.startswith("power-shell timeout -o "):
            self.override = int(command.rsplit(" ", 1)[1])
            return f"Override screen off time to {self.override}\n"
        raise AssertionError(command)

    def shell(self, command, timeout=40, *, cleanup=False):
        self.calls.append((command, timeout, cleanup))
        if not cleanup:
            self.check_cancelled()
        responses = self.responses.get(command)
        if responses:
            result = responses.popleft()
            if callable(result):
                return result(command)
            if isinstance(result, Exception):
                raise result
            return result
        return self.default(command)


class ScreenAwakeTests(unittest.TestCase):
    def create(self, **kwargs):
        client, record = PowerClient(**kwargs), {}
        return client, record, ScreenAwakeGuard(client, record, 180000)

    def test_parse_unique_current_timeout_and_override(self):
        self.assertEqual(parse_screen_off_timeout("ScreenOffTime: Timeout=30000ms"),
                         {"timeout_ms": 30000, "override_timeout_ms": None})
        self.assertEqual(parse_screen_off_timeout("ScreenOffTime: Timeout=30000ms  OverrideTimeout=180000ms\nHistory: failed"),
                         {"timeout_ms": 30000, "override_timeout_ms": 180000})
        for output in ("", "Current State: AWAKE", "Timeout=30000ms",
                       "ScreenOffTime: Timeout=30000ms OverrideTimeout=oops",
                       "ScreenOffTime: Timeout=30000ms\nScreenOffTime: Timeout=60000ms",
                       "ScreenOffTime: Timeout=2147483648ms"):
            with self.subTest(output=output):
                self.assertIsNone(parse_screen_off_timeout(output))

    def test_acquire_refresh_restore_and_short_command_evidence(self):
        client, record, guard = self.create()
        self.assertIs(guard.acquire(), record)
        self.assertEqual(record["status"], "active")
        guard.refresh()
        self.assertEqual(record["refresh_status"], "completed")
        guard.restore()
        self.assertIsNone(client.override)
        self.assertEqual(record["restore_status"], "completed")
        self.assertFalse(record["restoration_needed"])
        self.assertFalse(record["protection_degraded"])
        self.assertEqual([command for command, _, _ in client.calls],
                         [QUERY, SET, QUERY, QUERY, SET, QUERY, QUERY, RESTORE, QUERY])
        self.assertTrue(all(timeout <= 3 for _, timeout, _ in client.calls))
        self.assertTrue(all(cleanup for _, _, cleanup in client.calls[-3:]))
        self.assertTrue(all("output" in entry and "duration_seconds" in entry for entry in record["operations"]))

    def test_existing_override_is_restored_without_clearing_it(self):
        client, record, guard = self.create(override=60000)
        guard.acquire()
        guard.restore()
        self.assertEqual(client.override, 60000)
        self.assertNotIn(RESTORE, [command for command, _, _ in client.calls])
        self.assertEqual(record["restore_status"], "completed")

    def test_existing_longer_delay_is_not_shortened(self):
        client, record, guard = self.create(timeout=600000)
        guard.acquire()
        self.assertEqual(client.override, 600000)
        self.assertEqual(record["timeout_ms"], 600000)
        guard.restore()
        self.assertIsNone(client.override)

    def test_original_matching_override_needs_no_restore_write(self):
        client, record, guard = self.create(override=180000)
        guard.acquire()
        guard.restore()
        self.assertEqual(record["restore_status"], "already_original")
        self.assertEqual([command for command, _, _ in client.calls].count(SET), 1)

    def test_unknown_or_indefinite_original_settings_are_not_changed(self):
        for response in ("unknown command", RuntimeError("transport error"), "ScreenOffTime: Timeout=-1ms",
                         "ScreenOffTime: Timeout=30000ms  OverrideTimeout=-1ms"):
            with self.subTest(response=str(response)):
                client, record, guard = self.create()
                client.reply(QUERY, response)
                guard.acquire()
                guard.refresh()
                guard.restore()
                self.assertEqual(record["status"], "degraded")
                self.assertFalse(record["restoration_needed"])
                expected = [QUERY] if isinstance(response, str) and "Timeout=" in response else [QUERY, SERVICE_QUERY]
                self.assertEqual([command for command, _, _ in client.calls], expected)

    def test_zero_exit_service_failure_does_not_claim_active(self):
        client, record, guard = self.create()
        client.reply(SET, "Override screen off time to 180000 failed\n")
        guard.acquire()
        self.assertEqual(record["status"], "degraded")
        self.assertFalse(record["owned"])
        self.assertFalse(record["restoration_needed"])
        self.assertEqual(record["operations"][1]["status"], "failed")

    def test_timeout_after_write_is_owned_if_readback_verifies(self):
        client, record, guard = self.create()
        def applied_then_timeout(command):
            client.default(command)
            raise TimeoutError("response lost")
        client.reply(SET, applied_then_timeout)
        guard.acquire()
        self.assertEqual(record["status"], "active")
        guard.restore()
        self.assertIsNone(client.override)

    def test_uncertain_write_and_readback_failure_still_gets_restored(self):
        client, record, guard = self.create()
        def applied_then_timeout(command):
            client.default(command)
            raise TimeoutError("response lost")
        client.reply(SET, applied_then_timeout)
        client.reply(QUERY, client.default(QUERY), TimeoutError("query timed out"))
        guard.acquire()
        self.assertEqual(record["status"], "degraded")
        self.assertTrue(record["restoration_needed"])
        guard.refresh()
        self.assertEqual(record["refresh_status"], "skipped_not_owned")
        guard.restore()
        self.assertIsNone(client.override)
        self.assertEqual(record["restore_status"], "completed")

    def test_cancellation_after_write_propagates_and_cleanup_restores(self):
        client, record, guard = self.create()
        def applied_then_cancel(command):
            client.default(command)
            client.cancelled = True
            raise CaptureCancelled("cancelled after write")
        client.reply(SET, applied_then_cancel)
        with self.assertRaises(CaptureCancelled):
            guard.acquire()
        self.assertTrue(record["restoration_needed"])
        guard.restore()
        self.assertIsNone(client.override)
        self.assertEqual(record["restore_status"], "completed")

    def test_cancellation_before_write_propagates_without_mutation(self):
        client, record, guard = self.create()
        client.cancelled = True
        with self.assertRaises(CaptureCancelled):
            guard.acquire()
        self.assertFalse(record["restoration_needed"])
        guard.restore()
        self.assertEqual(client.calls, [])

    def test_external_override_changes_are_preserved_on_refresh_and_restore(self):
        for method in ("refresh", "restore"):
            with self.subTest(method=method):
                client, record, guard = self.create()
                guard.acquire()
                client.override = 90000
                getattr(guard, method)()
                guard.restore()
                self.assertEqual(client.override, 90000)
                self.assertEqual(record["restore_status"], "skipped_changed_externally")
                self.assertEqual([command for command, _, _ in client.calls].count(SET), 1)
                self.assertNotIn(RESTORE, [command for command, _, _ in client.calls])

    def test_new_system_default_during_capture_is_preserved(self):
        client, record, guard = self.create()
        guard.acquire()
        client.timeout = 60000
        guard.restore()
        self.assertEqual(client.timeout, 60000)
        self.assertIsNone(client.override)
        self.assertEqual(record["restore_status"], "completed")

    def test_refresh_query_failure_disables_further_refresh_but_retains_cleanup(self):
        client, record, guard = self.create()
        guard.acquire()
        client.reply(QUERY, TimeoutError("query unavailable"))
        guard.refresh()
        self.assertEqual(record["status"], "degraded")
        self.assertFalse(record["owned"])
        self.assertTrue(record["restoration_needed"])
        count = len(client.calls)
        guard.refresh()
        self.assertEqual(len(client.calls), count)
        guard.restore()
        self.assertIsNone(client.override)

    def test_refresh_command_error_cannot_prove_idle_timer_reset(self):
        client, record, guard = self.create()
        guard.acquire()
        client.reply(SET, "Override screen off time to 180000 failed")
        guard.refresh()
        self.assertEqual(record["status"], "degraded")
        self.assertTrue(record["restoration_needed"])
        guard.restore()
        self.assertIsNone(client.override)

    def test_refresh_cancellation_propagates_and_restores(self):
        client, record, guard = self.create()
        guard.acquire()
        client.cancelled = True
        with self.assertRaises(CaptureCancelled):
            guard.refresh()
        guard.restore()
        self.assertIsNone(client.override)

    def test_restore_failures_are_visible_non_throwing_and_retryable(self):
        for failure in (RuntimeError("transport unavailable"), CaptureCancelled("cleanup failed"), "unknown command"):
            with self.subTest(failure=str(failure)):
                client, record, guard = self.create()
                guard.acquire()
                client.reply(QUERY, failure)
                guard.restore()
                self.assertEqual(record["status"], "restore_failed")
                self.assertTrue(record["restoration_needed"])
                self.assertEqual(client.override, 180000)
                guard.restore()
                self.assertEqual(record["restore_status"], "completed")
                self.assertIsNone(client.override)

    def test_restore_command_failure_cannot_claim_restored(self):
        client, record, guard = self.create()
        guard.acquire()
        client.reply(RESTORE, "Restore screen off time failed")
        guard.restore()
        self.assertEqual(record["status"], "restore_failed")
        self.assertTrue(record["restoration_needed"])

    def test_acquire_is_idempotent_and_nonpositive_or_overflow_timeouts_rejected(self):
        client, record, guard = self.create()
        guard.acquire()
        count = len(client.calls)
        guard.acquire()
        self.assertEqual(len(client.calls), count)
        for invalid in (0, -1, 2147483648, float("inf"), True):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                ScreenAwakeGuard(client, {}, invalid)

    def test_shared_interface_cache_skips_unsupported_query_and_restores(self):
        client, record, cache = PowerClient(), {}, {}
        client.reply(QUERY, "usage: power-shell\ncommand list:\nwakeup\ntimeout\n")
        client.reply(SERVICE_QUERY, *[lambda _: client.default(QUERY) for _ in range(6)])
        guard = ScreenAwakeGuard(client, record, 180000, capability_cache=cache)
        guard.acquire()
        guard.refresh()
        guard.restore()
        commands = [command for command, _, _ in client.calls]
        self.assertEqual(commands.count(QUERY), 1)
        self.assertEqual(commands.count(SERVICE_QUERY), 6)
        self.assertEqual(cache["power_query_preferred_command"], SERVICE_QUERY)
        self.assertEqual(cache["power_query_unsupported_commands"], [QUERY])
        self.assertEqual(record["restore_status"], "completed")
        self.assertIsNone(client.override)

    def test_query_cache_keeps_transient_and_ambiguous_responses_retryable(self):
        for transient in ({"status": "failed", "reason": "query timed out", "output": ""},
                          {"status": "completed", "output": "temporarily unavailable"}):
            with self.subTest(transient=transient):
                cache, calls = {}, []
                responses = {
                    QUERY: deque([transient, {"status": "completed", "output": "ScreenOffTime: Timeout=30000ms"}]),
                    SERVICE_QUERY: deque([{"status": "completed", "output": "unknown command"}]),
                }
                def run(command):
                    calls.append(command)
                    return dict(responses[command].popleft())
                for _ in range(2):
                    _, state = query_power_compat(run, parse_screen_off_timeout, lambda value: value is not None,
                                                 unavailable_reason="no current timeout", capability_cache=cache)
                self.assertEqual(calls, [QUERY, SERVICE_QUERY, QUERY])
                self.assertEqual(state, {"timeout_ms": 30000, "override_timeout_ms": None})
                self.assertNotIn(QUERY, cache.get("power_query_unsupported_commands", []))

    def test_cached_preferred_query_failure_falls_back_to_other_supported_interface(self):
        cache = {"power_query_preferred_command": SERVICE_QUERY}
        calls = []
        def run(command):
            calls.append(command)
            return ({"status": "failed", "output": "", "reason": "timeout"} if command == SERVICE_QUERY else
                    {"status": "completed", "output": "ScreenOffTime: Timeout=30000ms"})
        _, state = query_power_compat(run, parse_screen_off_timeout, lambda value: value is not None,
                                     unavailable_reason="no current timeout", capability_cache=cache)
        self.assertEqual(calls, [SERVICE_QUERY, QUERY])
        self.assertEqual(state["timeout_ms"], 30000)
        self.assertEqual(cache["power_query_preferred_command"], QUERY)
        self.assertEqual(cache.get("power_query_unsupported_commands", []), [])

    def test_all_unsupported_queries_are_not_repeated_within_run(self):
        cache, calls = {}, []
        def run(command):
            calls.append(command)
            return {"status": "completed", "output": "unknown command"}
        for _ in range(2):
            observation, state = query_power_compat(run, parse_screen_off_timeout, lambda value: value is not None,
                                                   unavailable_reason="no current timeout", capability_cache=cache)
        self.assertEqual(calls, [QUERY, SERVICE_QUERY])
        self.assertIsNone(state)
        self.assertTrue(observation["cached_unsupported"])

    def test_needed_refresh_skips_queries_then_refreshes_before_budget_exhaustion(self):
        client, record, guard = self.create()
        clock = [1000.0]
        with patch("tools.scenario_capture.screen_awake.time.monotonic", side_effect=lambda: clock[0]):
            guard.acquire()
            initial_calls = len(client.calls)
            clock[0] += 20
            guard.refresh_if_needed(required_seconds=90)
            self.assertEqual(len(client.calls), initial_calls)
            self.assertEqual(record["refresh_status"], "skipped_sufficient_timeout")
            self.assertEqual(record["protected_remaining_seconds"], 160)
            clock[0] += 80
            guard.refresh_if_needed(required_seconds=90)
            self.assertEqual(record["refresh_status"], "completed")
            self.assertEqual(record["refresh_count"], 1)
            guard.restore()
        self.assertIsNone(client.override)
        self.assertEqual(record["restore_status"], "completed")

    def test_needed_refresh_counts_transport_elapsed_from_write_start(self):
        client, record, guard = self.create()
        clock = [1000.0]
        def delayed_write(command):
            clock[0] += 100
            return client.default(command)
        client.reply(SET, delayed_write)
        with patch("tools.scenario_capture.screen_awake.time.monotonic", side_effect=lambda: clock[0]):
            guard.acquire()
            guard.refresh_if_needed(required_seconds=90)
        self.assertEqual(record["protected_remaining_seconds"], 80)
        self.assertEqual(record["refresh_status"], "completed")
        self.assertEqual(record["refresh_count"], 1)

    def test_needed_refresh_preserves_external_override_when_refresh_is_due(self):
        client, record, guard = self.create()
        with patch("tools.scenario_capture.screen_awake.time.monotonic", return_value=1000):
            guard.acquire()
        client.override = 90000
        with patch("tools.scenario_capture.screen_awake.time.monotonic", return_value=1200):
            guard.refresh_if_needed(required_seconds=90)
        guard.restore()
        self.assertEqual(client.override, 90000)
        self.assertEqual(record["restore_status"], "skipped_changed_externally")

    def test_needed_refresh_cancellation_and_invalid_budget(self):
        client, record, guard = self.create()
        guard.acquire()
        for invalid in (True, -1, float("nan"), float("inf"), "90"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                guard.refresh_if_needed(required_seconds=invalid)
        client.cancelled = True
        with self.assertRaises(CaptureCancelled):
            guard.refresh_if_needed(required_seconds=90)
        guard.restore()
        self.assertIsNone(client.override)


if __name__ == "__main__":
    unittest.main()
