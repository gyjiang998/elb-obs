from __future__ import annotations

import shlex
import tempfile
import unittest
from pathlib import Path

from backend.core import ArtifactStore, PlatformError
from backend.scenario_capture import ScenarioCaptureManager
from backend.tests.test_scenario_capture import SimulatedDeviceClient
from tools.scenario_capture.runner import build_step_script, hitrace_begin_command


class ScenarioCapturePortabilityTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.manager = ScenarioCaptureManager(self.root, ArtifactStore(self.root), object())

    def plan(self, **changes: object) -> dict:
        payload = {"template_id": "custom", "steps": [
            {"title": "采集窗口", "operation": "wait", "seconds": 0}],
            "capture": {"selected_step_ids": ["step_01"], "collectors": ["trace"]}}
        payload.update(changes)
        return self.manager.create_plan(payload)

    def test_hitrace_categories_are_distinct_shell_arguments(self) -> None:
        for categories in (["sched", "ace", "app"], ["app", "sched", "ace"]):
            with self.subTest(categories=categories):
                plan = self.plan(profile={"trace_categories": categories})
                script = build_step_script(plan["steps"][0], [], plan["profile"], plan["capture"],
                                           "/data/local/tmp/run/step", "/sys/kernel/tracing", "123")
                line = next(line for line in script.splitlines() if line.startswith("hitrace --trace_begin "))
                argv = shlex.split(line.split(" > ", 1)[0])
                self.assertEqual(argv, ["hitrace", "--trace_begin", "--trace_clock", "mono", "-b",
                                        str(plan["profile"]["trace_buffer_kb"]), *categories])
                self.assertEqual(argv, hitrace_begin_command(plan["profile"]))

    def test_csv_and_embedded_shell_categories_are_rejected(self) -> None:
        for categories in (["sched,ace,app"], ["sched ace"], ["sched;echo x"], "sched,app", []):
            with self.subTest(categories=categories):
                with self.assertRaises(PlatformError):
                    self.plan(profile={"trace_categories": categories})
                with self.assertRaises(ValueError):
                    hitrace_begin_command({"trace_categories": categories, "trace_buffer_kb": 1024})

    def test_manifest_records_effective_hitrace_arguments_after_device_filtering(self) -> None:
        plan = self.plan(profile={"trace_categories": ["sched", "ace", "app", "unsupported_tag"]})
        run = self.manager.execute(plan["plan_id"], bridge_id="bridge_test", device_serial="phone",
                                   device_client=SimulatedDeviceClient())
        categories = run["manifest"]["trace_categories"]
        self.assertEqual(categories["argument_format"], "space_separated")
        self.assertEqual(categories["selected"], ["sched", "ace", "app"])
        self.assertEqual(categories["unavailable"], ["unsupported_tag"])
        self.assertEqual(categories["begin_argv"][-3:], ["sched", "ace", "app"])

    def test_catalog_and_plan_accept_10us_without_rewriting_default(self) -> None:
        catalog = self.manager.catalog()
        self.assertEqual(catalog["pmu_sampler"]["period_us_range"], [10, 10000])
        self.assertEqual(catalog["default_capture"]["pmu"]["period_us"], 100)
        for interval in (10, 11, 20, 100, 10000):
            with self.subTest(interval=interval):
                plan = self.plan(capture={"selected_step_ids": ["step_01"], "collectors": ["pmu"],
                                          "pmu": {"period_us": interval}})
                self.assertEqual(plan["capture"]["pmu"]["period_us"], interval)
                self.assertEqual(self.manager.get_plan(plan["plan_id"])["capture"]["pmu"]["period_us"], interval)

    def test_invalid_sampling_intervals_and_screen_modes_are_validation_errors(self) -> None:
        for interval in (9, 10001, 10.5, True, "10", None):
            with self.subTest(interval=interval), self.assertRaises(PlatformError):
                self.plan(capture={"pmu": {"period_us": interval}})
        for mode in ("unknown", None, [], {}, True):
            with self.subTest(mode=mode), self.assertRaises(PlatformError):
                self.plan(profile={"screen_size_mode": mode})
        for mode in ("dynamic", "fixed"):
            with self.subTest(mode=mode):
                self.assertEqual(self.plan(profile={"screen_size_mode": mode})["profile"]["screen_size_mode"], mode)


if __name__ == "__main__":
    unittest.main()
