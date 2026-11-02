"""PMU completion and temporal-resolution contracts, without a device."""
import csv
import json
import tempfile
import unittest
from pathlib import Path

from tools.scenario_capture.runner import validate_pmu


class PmuValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self): self.tmp.cleanup()

    def samples(self, counts, *, metadata=None, errors=None):
        header = ["read_begin_ns", "read_end_ns", "event_count", "enabled_ns", "running_ns", "cycles", "instructions", "event2", "event3", "read_error"]
        with (self.root / "pmu.csv").open("w") as stream:
            writer = csv.DictWriter(stream, fieldnames=header)
            writer.writeheader()
            for index, (enabled, running) in enumerate(counts):
                writer.writerow({"read_begin_ns": 1_000_000 + index * 100_000, "read_end_ns": 1_000_010 + index * 100_000,
                                 "event_count": 4, "enabled_ns": enabled, "running_ns": running,
                                 "cycles": running * 2, "instructions": running * 3, "event2": running, "event3": running // 10,
                                 "read_error": (errors or {}).get(index, 0)})
        (self.root / "pmu.json").write_text(json.dumps({"sample_count": len(counts), "target_tid": 42, "clock": "CLOCK_MONOTONIC",
            "event_names": ["cycles", "instructions", "generic_cache_references", "generic_cache_misses"],
            "period_ns_requested": 100000, **(metadata or {})}))

    def test_initial_zero_cumulative_baseline_is_not_read_failure(self):
        self.samples([(0, 0), (0, 0), (100, 100), (200, 200), (200, 200)])
        result = validate_pmu(self.root, target_tid=42, step_window={"start_s": .001, "end_s": .0014})
        self.assertTrue(result["quality_complete"], result)
        self.assertEqual(result["inactive_baseline_rows"], 2)
        self.assertEqual(result["usable_counter_rows"], 3)
        self.assertEqual(result["read_error_rows"], 0)
        self.assertEqual(result["sampling_quality"]["status"], "observed")

    def test_never_running_or_enabled_but_unscheduled_is_not_complete(self):
        self.samples([(0, 0)] * 3)
        result = validate_pmu(self.root)
        self.assertFalse(result["valid"])
        self.assertEqual(result["inactive_baseline_rows"], 3)
        self.samples([(0, 0), (100, 0), (200, 100), (300, 200)])
        result = validate_pmu(self.root)
        self.assertTrue(result["valid"])
        self.assertFalse(result["quality_complete"])
        self.assertEqual(result["unscheduled_counter_rows"], 1)
        self.assertEqual(result["read_error_rows"], 0)

    def test_actual_read_error_and_counter_reset_are_not_hidden(self):
        self.samples([(100, 100), (200, 200), (300, 300)], errors={1: 5})
        result = validate_pmu(self.root)
        self.assertTrue(result["valid"])
        self.assertFalse(result["quality_complete"])
        self.assertEqual(result["read_error_rows"], 1)
        self.samples([(100, 100), (0, 0), (300, 300)])
        result = validate_pmu(self.root)
        self.assertFalse(result["valid"])
        self.assertEqual(result["inactive_baseline_rows"], 0)
        self.assertEqual(result["invalid_counter_rows"], 1)

    def test_cadence_degradation_is_separate_from_capture_completeness(self):
        self.samples([(0, 0), (100, 100), (200, 200)], metadata={"skipped_deadlines": 10, "stopped_signal": 15})
        (self.root / "pmu_exit_code.txt").write_text("5\n")
        result = validate_pmu(self.root)
        self.assertTrue(result["quality_complete"])
        self.assertTrue(result["stop"]["expected_stop"])
        self.assertEqual(result["sampling_quality"]["status"], "degraded")
        self.assertEqual(result["sampling_quality"]["observed_median_interval_us"], 100)
        self.assertAlmostEqual(result["sampling_quality"]["skipped_deadline_fraction"], 10 / 13)
        self.assertIn("PMU_SAMPLING_CADENCE_DEGRADED", [row["code"] for row in result["diagnostics"]])
        (self.root / "pmu_exit_code.txt").write_text("4\n")
        self.assertFalse(validate_pmu(self.root)["quality_complete"])

    def test_partial_window_quantifies_missing_boundaries(self):
        self.samples([(100, 100), (200, 200)])
        result = validate_pmu(self.root, step_window={"start_s": .0005, "end_s": .002})
        self.assertFalse(result["quality_complete"])
        self.assertAlmostEqual(result["coverage"]["missing_start_ms"], .5)
        self.assertAlmostEqual(result["coverage"]["missing_end_ms"], .89999)

    def test_unavailable_pid_permission_and_unsupported_events_have_specific_reasons(self):
        (self.root / "pmu.log").write_text("No unique pre-action target PID; PMU not started")
        self.assertEqual(validate_pmu(self.root)["diagnostics"][0]["code"], "PMU_TARGET_NOT_RESOLVED")
        (self.root / "pmu.log").write_text("Preinstalled PMU sampler unavailable")
        self.assertEqual(validate_pmu(self.root)["diagnostics"][0]["code"], "PMU_SAMPLER_UNAVAILABLE")
        (self.root / "pmu.json").write_text('{"open_error":13}')
        self.assertEqual(validate_pmu(self.root)["diagnostics"][0]["code"], "PMU_PERMISSION_DENIED")
        (self.root / "pmu.json").write_text('{"open_error":22}')
        self.assertEqual(validate_pmu(self.root)["diagnostics"][0]["code"], "PMU_EVENT_UNSUPPORTED")

    def test_verified_sampler_not_started_is_not_misdiagnosed_as_missing(self):
        (self.root / "pmu_binary_identity.txt").write_text(
            "a" * 64 + "  /data/local/tmp/hetero_tools/pmu/" + "a" * 64 + "/thread_pmu_sampler\n")
        (self.root / "pmu_compatibility.json").write_text(json.dumps({
            "schema_version": "pmu_sampler_compatibility/v1",
            "requested_period_us": 100,
            "status": "supported",
            "reported_min_period_us": 10,
            "reported_max_period_us": 10000,
        }))

        result = validate_pmu(self.root, collector_state="unavailable")

        self.assertEqual(result["diagnostics"][0]["code"], "PMU_NOT_STARTED")
        self.assertIn("已经核验", result["reason"])
        self.assertNotIn("采样器路径", result["reason"])

    def test_unavailable_without_verified_identity_remains_sampler_missing(self):
        (self.root / "pmu_compatibility.json").write_text(json.dumps({
            "schema_version": "pmu_sampler_compatibility/v1",
            "requested_period_us": 100,
            "status": "supported",
        }))

        result = validate_pmu(self.root, collector_state="unavailable")

        self.assertEqual(result["diagnostics"][0]["code"], "PMU_SAMPLER_UNAVAILABLE")

    def test_split_identical_zero_baselines_preserves_count_quality(self):
        for zeros in (1, 10, 100):
            self.samples([(0, 0)] * zeros + [(100, 100), (200, 200)])
            result = validate_pmu(self.root)
            self.assertTrue(result["quality_complete"])
            self.assertEqual(result["inactive_baseline_rows"], zeros)
            self.assertEqual(result["read_error_rows"], 0)


if __name__ == "__main__": unittest.main()
