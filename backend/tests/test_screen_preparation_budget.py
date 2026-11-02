"""Per-operation limits and optional evidence deadlines using fake time."""
from __future__ import annotations

import base64
import json
from pathlib import Path
import shlex
import tempfile
import unittest
from unittest.mock import patch

from backend.core import ArtifactStore
from backend.scenario_capture import CaptureCancelled, ScenarioCaptureManager
from backend.tests.test_scenario_capture import SimulatedDeviceClient
from tools.scenario_capture.gallery import CapturePreparationError
from tools.scenario_capture.runner import PreparationClient, execute_plan


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jXGkAAAAASUVORK5CYII=")


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def monotonic(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class BudgetDevice(SimulatedDeviceClient):
    operation_timeout_grace_seconds = 3

    def __init__(self, root, *, clock=None, failed_dump=False, image=PNG):
        super().__init__()
        self.root, self.clock, self.failed_dump, self.image = root, clock, failed_dump, image
        self.cancelled = False
        self.calls, self.persisted = [], []
        self.durations = {}

    def check_cancelled(self):
        if self.cancelled:
            raise CaptureCancelled("fixture cancelled")

    def observe(self, kind, target, timeout, cleanup):
        self.calls.append({"kind": kind, "target": target, "timeout": timeout, "cleanup": cleanup})
        manifest = self.root / "manifest.json"
        if manifest.exists():
            value = json.loads(manifest.read_text())
            if value["steps"]:
                record = value["steps"][-1]
                timing = record.get("screen_evidence_timing") or record.get("screen_preparation_timing")
                if timing:
                    self.persisted.append({"target": target, "timing": timing})
        if self.clock:
            self.clock.advance(self.durations.get(target, 0))

    def shell(self, command, timeout=40, *, cleanup=False):
        self.observe("shell", command, timeout, cleanup)
        if command.startswith("uitest dumpLayout") and self.failed_dump:
            raise RuntimeError("fixture UI dump unavailable")
        output = super().shell(command, timeout, cleanup=cleanup)
        if command.startswith("uitest screenCap -p "):
            self.files[shlex.split(command)[-1]] = self.image
        if command.startswith("wc -c < "):
            return str(len(self.files[shlex.split(command)[-1]]))
        if command == "power-shell dump -s":
            return "Current State: AWAKE"
        return output

    def collect(self, remote, destination, *, timeout=600, cleanup=True):
        self.observe("collect", remote, timeout, cleanup)
        return super().collect(remote, destination, timeout=timeout, cleanup=cleanup)


class PreparationBudgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        manager = ScenarioCaptureManager(self.root, ArtifactStore(self.root), object())
        self.plan = manager.create_plan({"template_id": "custom", "steps": [
            {"title": "capture", "operation": "wait", "seconds": 0}],
            "capture": {"selected_step_ids": ["step_01"], "collectors": ["trace"]}})

    def test_default_preparation_can_exceed_45_seconds_with_bounded_individual_operations(self):
        clock, record, timeouts = FakeClock(), {}, []

        class Client:
            def check_cancelled(self):
                pass
            def shell(self, command, timeout, cleanup):
                timeouts.append(timeout)
                clock.advance(7)
                return "completed"

        with patch("tools.scenario_capture.runner.time.monotonic", side_effect=clock.monotonic):
            wrapped = PreparationClient(Client(), record, lambda: None)
            for index in range(10):
                self.assertEqual(wrapped.shell(f"fixture-operation-{index}", timeout=40), "completed")
            wrapped.finish("completed")
        timing = record["screen_preparation_timing"]
        self.assertEqual(timeouts, [8] * 10)
        self.assertIsNone(wrapped.deadline)
        self.assertIsNone(timing["budget_seconds"])
        self.assertIsNone(timing["remaining_seconds"])
        self.assertEqual(timing["elapsed_seconds"], 70)
        self.assertEqual(timing["status"], "completed")

    def test_cancellation_remains_immediate_after_a_long_preparation(self):
        clock, record, calls = FakeClock(), {}, []

        class Client:
            cancelled = False
            def check_cancelled(self):
                if self.cancelled:
                    raise CaptureCancelled("cancelled after slow bridge operations")
            def shell(self, command, timeout, cleanup):
                calls.append(command)
                clock.advance(7)
                self.cancelled = len(calls) == 10

        with patch("tools.scenario_capture.runner.time.monotonic", side_effect=clock.monotonic):
            wrapped = PreparationClient(Client(), record, lambda: None)
            for index in range(9):
                wrapped.shell(f"fixture-operation-{index}")
            with self.assertRaises(CaptureCancelled):
                wrapped.shell("last-operation")
            with self.assertRaises(CaptureCancelled):
                wrapped.shell("must-not-run")
        timing = record["screen_preparation_timing"]
        self.assertEqual(len(calls), 10)
        self.assertEqual(timing["elapsed_seconds"], 70)
        self.assertEqual(timing["status"], "cancelled")
        self.assertEqual(timing["operations"][-1]["status"], "cancelled")
        self.assertIsNone(timing["budget_seconds"])

    def test_individual_rpc_timeout_is_preserved_without_an_overall_deadline(self):
        clock, record = FakeClock(), {}
        expected = TimeoutError("device operation timed out")

        class Client:
            def check_cancelled(self):
                pass
            def shell(self, command, timeout, cleanup):
                self.timeout = timeout
                clock.advance(timeout)
                raise expected

        device = Client()
        with patch("tools.scenario_capture.runner.time.monotonic", side_effect=clock.monotonic):
            wrapped = PreparationClient(device, record, lambda: None)
            clock.advance(90)
            with self.assertRaises(TimeoutError) as raised:
                wrapped.shell("fixture-hung-operation", timeout=600)
        self.assertIs(raised.exception, expected)
        self.assertEqual(device.timeout, 8)
        self.assertEqual(record["screen_preparation_timing"]["status"], "failed")

    def test_wake_dump_and_transfer_debit_same_budget_and_reserve_return_grace(self):
        clock, record, calls = FakeClock(), {}, []

        class Client:
            operation_timeout_grace_seconds = 3
            def check_cancelled(self):
                pass
            def shell(self, command, timeout, cleanup):
                calls.append((command, timeout, cleanup))
                clock.advance(9 if len(calls) == 1 else 8)
            def collect(self, remote, destination, timeout, cleanup):
                calls.append((remote, timeout, cleanup))
                clock.advance(4)

        with patch("tools.scenario_capture.runner.time.monotonic", side_effect=clock.monotonic):
            wrapped = PreparationClient(Client(), record, lambda: None, seconds=25)
            wrapped.shell("power-shell wakeup", timeout=40)
            wrapped.shell("uitest dumpLayout -p /layout.json", timeout=40)
            wrapped.collect("/layout.json", self.root / "layout.json", timeout=600, cleanup=True)
            self.assertEqual([call[1] for call in calls], [8, 8, 5])
            self.assertTrue(all(call[2] is False for call in calls))
            clock.advance(1)
            with self.assertRaises(CapturePreparationError) as error:
                wrapped.shell("must_not_execute")
        self.assertEqual(error.exception.code, "SCENARIO_SCREEN_PREPARATION_TIMEOUT")
        self.assertEqual(len(calls), 3)
        self.assertEqual(record["screen_preparation_timing"]["status"], "timed_out")
        self.assertEqual(record["screen_preparation_timing"]["operations"][-1]["duration_seconds"], 4)

    def test_local_teardown_reservation_reduces_next_command(self):
        clock, record, calls = FakeClock(), {}, []

        class Client:
            operation_timeout_grace_seconds = 9
            def check_cancelled(self):
                pass
            def shell(self, command, timeout, cleanup):
                calls.append(timeout)
                clock.advance(9 if len(calls) == 1 else 0)

        with patch("tools.scenario_capture.runner.time.monotonic", side_effect=clock.monotonic):
            wrapped = PreparationClient(Client(), record, lambda: None, seconds=20)
            wrapped.shell("power-shell wakeup")
            wrapped.shell("uitest dumpLayout -p /layout.json")
        self.assertEqual(calls, [8, 2])

    def test_cancellation_after_rpc_is_not_relabelled_as_timeout(self):
        clock, record = FakeClock(), {}

        class Client:
            cancelled = False
            def check_cancelled(self):
                if self.cancelled:
                    raise CaptureCancelled("fixture cancelled")
            def shell(self, command, timeout, cleanup):
                clock.advance(45)
                self.cancelled = True

        with patch("tools.scenario_capture.runner.time.monotonic", side_effect=clock.monotonic):
            wrapped = PreparationClient(Client(), record, lambda: None)
            with self.assertRaises(CaptureCancelled):
                wrapped.shell("power-shell wakeup")
        self.assertEqual(record["screen_preparation_timing"]["status"], "cancelled")
        self.assertEqual(record["screen_preparation_timing"]["operations"][0]["status"], "cancelled")

    def test_layout_transport_is_short_cancellable_and_progress_precedes_rpc(self):
        directory = self.root / "run"
        device = BudgetDevice(directory)
        manifest = execute_plan(self.plan, device, directory, "capture_fixture")
        self.assertIn(manifest["status"], {"succeeded", "partial"})
        layouts = [call for call in device.calls if call["target"].endswith(".json") and
                   (call["kind"] == "collect" or call["target"].startswith("uitest dumpLayout"))]
        self.assertTrue(layouts)
        self.assertTrue(all(1 <= call["timeout"] <= 8 and call["cleanup"] is False for call in layouts))
        wake = next(item for item in device.persisted if item["target"] == "power-shell dump -s")
        self.assertEqual(wake["timing"]["status"], "running")
        self.assertEqual(wake["timing"]["operation"], "读取电源状态")
        self.assertEqual(wake["timing"]["operations"][-1]["status"], "running")
        dump = next(item for item in device.persisted if item["target"].startswith("uitest dumpLayout"))
        self.assertEqual(dump["timing"]["operation"], "读取 UI 布局")
        received = next(item for item in device.persisted if item["target"].endswith("before.json") and
                        not item["target"].startswith("uitest dumpLayout"))
        self.assertEqual(received["timing"]["operation"], "回收界面布局")
        self.assertEqual(manifest["steps"][0]["screen_preparation_timing"]["status"], "completed")

    def test_layout_error_closes_preparation_and_keeps_original_diagnostic(self):
        directory = self.root / "failed"
        device = BudgetDevice(directory, failed_dump=True)
        manifest = execute_plan(self.plan, device, directory, "capture_fixture")
        step = manifest["steps"][0]
        self.assertEqual(manifest["status"], "failed")
        self.assertEqual(step["screen_preparation_timing"]["status"], "failed")
        self.assertEqual(step["error"]["code"], "SCENARIO_SCREEN_LAYOUT_UNAVAILABLE")
        self.assertIn("fixture UI dump unavailable", step["error"]["reason"])
        self.assertEqual(device.executed, [])

    def test_evidence_has_fresh_budget_and_exhaustion_preserves_locked_error(self):
        clock, directory = FakeClock(), self.root / "evidence_timeout"
        device = BudgetDevice(directory, clock=clock)

        def locked(*args, **kwargs):
            clock.advance(40)
            device.durations = {"power-shell dump -s": 11, "hidumper -s 3704 -a '-all'": 8}
            raise CapturePreparationError("SCENARIO_SCREEN_LOCKED", "original locked failure", "fixture")

        with patch("tools.scenario_capture.runner.time.monotonic", side_effect=clock.monotonic), \
                patch("tools.scenario_capture.screen_preparation.prepare_unlocked_page", side_effect=locked):
            manifest = execute_plan(self.plan, device, directory, "capture_fixture")
        step = manifest["steps"][0]
        self.assertEqual(step["error"]["code"], "SCENARIO_SCREEN_LOCKED")
        self.assertEqual(step["error"]["reason"], "original locked failure")
        self.assertEqual(step["screen_evidence_timing"]["budget_seconds"], 20)
        self.assertEqual(step["screen_evidence_timing"]["status"], "timed_out")
        self.assertGreaterEqual(step["screen_evidence_timing"]["elapsed_seconds"], 19)
        self.assertLessEqual(step["screen_evidence_timing"]["elapsed_seconds"], 20)
        self.assertFalse(any(command.startswith("uitest screenCap") for command in device.commands))

    def test_invalid_png_closes_evidence_timing_without_changing_primary_error(self):
        directory = self.root / "invalid_png"
        device = BudgetDevice(directory, image=b"invalid image payload")
        error = CapturePreparationError("SCENARIO_SCREEN_LOCKED", "original locked failure", "fixture")
        with patch("tools.scenario_capture.screen_preparation.prepare_unlocked_page", side_effect=error):
            manifest = execute_plan(self.plan, device, directory, "capture_fixture")
        step = manifest["steps"][0]
        self.assertEqual(step["error"]["code"], "SCENARIO_SCREEN_LOCKED")
        self.assertEqual(step["screen_evidence_timing"]["status"], "failed")
        self.assertEqual(step["screen_preparation_screenshot"]["status"], "unavailable")


if __name__ == "__main__":
    unittest.main()
