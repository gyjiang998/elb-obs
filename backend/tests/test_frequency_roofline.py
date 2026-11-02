from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from backend.core import PlatformError
from backend.parallel_modeling import ParallelModelingManager
from backend.parallel_modeling import service_dag
from backend.parallel_modeling.frequency_roofline import factor, fit, profile_factor
from backend.parallel_modeling.manager import HARDWARE_ID, SEARCH_ENGINE


class FrequencyRooflineTests(unittest.TestCase):
    def test_piecewise_curve_is_continuous_and_distinguishes_compute_from_memory(self):
        maximum, knee = 2_000_000, 1_000_000
        at_knee = factor(maximum, knee, knee, .05, .8)
        below = factor(maximum, knee - 1, knee, .05, .8)
        above = factor(maximum, knee + 1, knee, .05, .8)
        self.assertAlmostEqual(at_knee, below, places=5)
        self.assertAlmostEqual(at_knee, above, places=5)
        compute = factor(maximum, knee, maximum, 1.0, 1.0)
        memory = factor(maximum, knee, knee, .05, .8)
        self.assertAlmostEqual(compute, 2.0)
        self.assertLess(memory, 1.04)

    def test_five_opp_fit_recovers_knee_and_both_exponents(self):
        maximum, knee, above, below = 2_000_000, 1_000_000, .1, .8
        frequencies = [500_000, 750_000, 1_000_000, 1_500_000, maximum]
        result = fit([(frequency, factor(maximum, frequency, knee, above, below))
                      for frequency in frequencies], maximum)
        self.assertEqual(result["knee_khz"], knee)
        self.assertAlmostEqual(result["above_knee_exponent"], above, places=10)
        self.assertAlmostEqual(result["below_knee_exponent"], below, places=10)
        self.assertLess(result["training_mape"], 1e-10)

    def test_plan_rejects_hardware_without_five_train_and_one_holdout_opp(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = ParallelModelingManager(Path(temporary))
            profile = manager.calibration.base_model["profiles"]["middle"]
            profile["opps_khz"] = profile["opps_khz"][-5:]
            with self.assertRaises(PlatformError) as raised:
                manager.calibration.create_plan({"parameter_groups": ["frequency_roofline"],
                                                 "core_kinds": ["middle"], "repeats": 3})
            self.assertIn("至少提供6个不同OPP", str(raised.exception))
            self.assertEqual(raised.exception.details["available_opp_count"], 5)

    def test_simulator_applies_class_curve_only_to_on_cpu_and_preserves_off_cpu(self):
        def graph(workload_class):
            nodes = [
                {"id": "begin_main", "kind": "begin", "group": "main", "duration_ms": 0.0},
                {"id": "cpu", "kind": "cpu", "group": "main", "component": "build_00",
                 "state": "on_cpu", "duration_ms": 10.0, "observed_duration_ms": 10.0},
                {"id": "blocked", "kind": "delay", "group": "main", "component": "build_00",
                 "state": "blocked", "duration_ms": 4.0, "observed_duration_ms": 4.0},
                {"id": "end_main", "kind": "end", "group": "main", "duration_ms": 0.0},
            ]
            return {"nodes": nodes,
                    "edges": [{"source": left["id"], "target": right["id"], "kind": "within_task_order"}
                              for left, right in zip(nodes, nodes[1:])],
                    "groups": {"main": {"branch": False, "cpu_ms": 10.0}}, "terminal": "end_main",
                    "component_classes": {"build_00": workload_class}}

        profile = {"max_khz": 2_000_000, "build_ratio": 1.0, "residual_ratio": 1.0,
                   "alpha": 0.0, "frequency_elasticity": .75,
                   "frequency_roofline": {"schema_version": "frequency_roofline/v1", "classes": {
                       "compute": {"knee_khz": 2_000_000, "above_knee_exponent": 1.0, "below_knee_exponent": 1.0,
                                   "frequency_domain_khz": [1_000_000, 2_000_000]},
                       "memory": {"knee_khz": 1_000_000, "above_knee_exponent": .05, "below_knee_exponent": .8,
                                  "frequency_domain_khz": [1_000_000, 2_000_000]},
                   }}}
        configuration = {"main": {"kind": "middle", "khz": 1_000_000}, "workers": [],
                         "main_participates": False, "dispatch": "main_first"}
        compute = service_dag.simulate(graph("compute"), configuration, {"middle": profile},
                                       contention=False, overhead_ms=0)
        memory = service_dag.simulate(graph("memory"), configuration, {"middle": profile},
                                      elasticity=None, contention=False, overhead_ms=0)
        self.assertAlmostEqual(compute["finish_ms"], 24.0)
        self.assertAlmostEqual(memory["finish_ms"], 4 + 10 * (2 ** .05))
        self.assertEqual(compute["app_off_cpu_observed_ms"], 4.0)
        self.assertEqual(memory["time_accounting"]["observed_reference"]["off_cpu_states_ms"]["blocked"], 4.0)
        self.assertEqual(compute["frequency_model_usage"]["matched_models"][0]["workload_class"], "compute")
        override = service_dag.simulate(graph("compute"), configuration, {"middle": profile},
                                        elasticity=.75, contention=False, overhead_ms=0)
        self.assertEqual(override["frequency_model_usage"]["matched_models"], [])
        self.assertGreater(override["frequency_model_usage"]["fallback_reasons"]["explicit_sensitivity_override"], 0)

        extrapolated, detail = profile_factor(profile, 500_000, "memory", .75)
        self.assertAlmostEqual(extrapolated, .25 + .75 * 4)
        self.assertEqual(detail["mode"], "legacy_elasticity_fallback")
        self.assertEqual(detail["reason"], "outside_calibrated_frequency_domain")
        low_configuration = {**configuration, "main": {"kind": "middle", "khz": 500_000}}
        low = service_dag.simulate(graph("memory"), low_configuration, {"middle": profile},
                                   elasticity=None, contention=False, overhead_ms=0)
        self.assertEqual(low["frequency_model_usage"]["matched_models"], [])
        self.assertGreater(low["frequency_model_usage"]["fallback_reasons"]["outside_calibrated_frequency_domain"], 0)

    def test_absolute_class_curve_replaces_legacy_core_ratio_without_double_counting(self):
        nodes = [
            {"id": "begin", "kind": "begin", "group": "main", "duration_ms": 0.0},
            {"id": "cpu", "kind": "cpu", "group": "main", "component": "build_00",
             "state": "on_cpu", "duration_ms": 10.0, "observed_duration_ms": 10.0},
            {"id": "end", "kind": "end", "group": "main", "duration_ms": 0.0},
        ]
        graph = {"nodes": nodes,
                 "edges": [{"source": left["id"], "target": right["id"]}
                           for left, right in zip(nodes, nodes[1:])],
                 "groups": {"main": {"branch": False, "cpu_ms": 10.0}}, "terminal": "end",
                 "component_classes": {"build_00": "compute"}}
        parameters = {"knee_khz": 2_000_000, "above_knee_exponent": 1.0,
                      "below_knee_exponent": 1.0,
                      "frequency_domain_khz": [1_000_000, 2_000_000],
                      "cross_core_time_ratio": 2.0,
                      "transfer_reference": {"core_kind": "big", "frequency_khz": 2_500_000,
                                             "cpu_ms_per_work_unit": .01,
                                             "metric": "target_thread_cpu_ms_per_work_unit"}}
        profile = {"max_khz": 2_000_000, "build_ratio": 100.0, "residual_ratio": 1.0,
                   "alpha": 0.0, "frequency_elasticity": .75,
                   "frequency_roofline": {"schema_version": "frequency_roofline/v1",
                                          "classes": {"compute": parameters}}}
        configuration = {"main": {"kind": "middle", "khz": 2_000_000}, "workers": [],
                         "main_participates": False, "dispatch": "main_first"}
        measured = service_dag.simulate(graph, configuration, {"middle": profile},
                                        contention=False, overhead_ms=0)
        self.assertAlmostEqual(measured["finish_ms"], 20.0)
        self.assertEqual(measured["frequency_model_usage"]["matched_models"][0]["core_transfer_mode"],
                         "absolute_curve_ratio")
        self.assertAlmostEqual(measured["frequency_model_usage"]["absolute_curve_core_ms"], 20.0)

        # Removing only the additive v1 anchor fields must preserve the old
        # normalized-curve schema and its legacy build_ratio behavior.
        old_profile = {**profile, "frequency_roofline": {"schema_version": "frequency_roofline/v1",
            "classes": {"compute": {key: value for key, value in parameters.items()
                                     if key not in ("cross_core_time_ratio", "transfer_reference")}}}}
        legacy = service_dag.simulate(graph, configuration, {"middle": old_profile},
                                      contention=False, overhead_ms=0)
        self.assertAlmostEqual(legacy["finish_ms"], 1000.0)
        self.assertEqual(legacy["frequency_model_usage"]["matched_models"][0]["core_transfer_mode"],
                         "legacy_profile_ratio")

        # An explicit sensitivity request intentionally exercises the old
        # ratio/elasticity path rather than silently consuming calibration.
        override = service_dag.simulate(graph, configuration, {"middle": profile}, elasticity=0,
                                        contention=False, overhead_ms=0)
        self.assertAlmostEqual(override["finish_ms"], 1000.0)

    def test_calibration_fits_per_core_per_class_curve_and_publishes_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            manager = ParallelModelingManager(Path(temporary))
            plan = manager.calibration.create_plan({"name": "ROOFLINE TEST", "parameter_groups": ["frequency_roofline"],
                                                     "core_kinds": ["middle"], "repeats": 3,
                                                     "roofline_work_units": 100000})
            frequencies = plan["frequencies_khz"]["middle"]
            split = plan["frequency_roofline_split"]["middle"]
            train_frequencies = split["train_khz"]
            holdout_frequencies = split["validation_khz"]
            self.assertGreaterEqual(len(frequencies), 6)
            self.assertGreaterEqual(len(train_frequencies), 5)
            self.assertEqual(len(holdout_frequencies), 1)
            self.assertTrue(set(train_frequencies).isdisjoint(holdout_frequencies))
            self.assertEqual((frequencies[0], frequencies[-1]),
                             (manager.calibration.base_model["profiles"]["middle"]["opps_khz"][0],
                              manager.calibration.base_model["profiles"]["middle"]["max_khz"]))
            parameters = {
                "compute": (frequencies[-1], 1.0, 1.0),
                "memory": (train_frequencies[1], .05, .8),
                "mixed": (train_frequencies[3], .25, .9),
            }
            samples = []
            for condition in plan["conditions"]:
                knee, above, below = parameters[condition["workload_class"]]
                cpu_ms = (.00002 * condition["work_units"]
                          * factor(frequencies[-1], condition["frequency_khz"], knee, above, below))
                for repeat in range(condition["repeats"]):
                    identity = f"ROOFLINE_TEST_{condition['id']}_{repeat}"
                    samples.append({**condition, "sample_id": identity, "run_id": identity, "repeat": repeat,
                                    "workload_version": "parallel-calibration-proxy-v6",
                                    "cpu_ms": cpu_ms, "wall_ms": cpu_ms * 1.05,
                                    "cpu": 4, "helpers": [], "observer_cpu": 0,
                                    "observer_independent_core": True, "affinity_verified": True,
                                    "target_sibling_cpus": [], "target_sibling_activity": [],
                                    "target_sibling_activity_verified": True,
                                    "target_sibling_busy_fraction": 0,
                                    "target_sibling_busy_threshold": .05,
                                    "target_sibling_quietness_verified": True,
                                    "frequency_verified": True, "thermal_throttled": False,
                                    "thermal_snapshot_before": [{"zone": "thermal_zone0", "type": "cpu",
                                                                 "temperature_raw": 41000}],
                                    "thermal_snapshot_after": [{"zone": "thermal_zone0", "type": "cpu",
                                                                "temperature_raw": 41100}],
                                    "observed_frequency_khz": condition["frequency_khz"],
                                    "frequency_mean_khz": condition["frequency_khz"],
                                    "frequency_min_khz": condition["frequency_khz"],
                                    "frequency_max_khz": condition["frequency_khz"],
                                    "frequency_sample_count": 20,
                                    "frequency_read_failures": 0,
                                    "frequency_source": "cpuinfo_cur_freq",
                                    "frequency_source_cpu": 4,
                                    "frequency_source_path":
                                        "/sys/devices/system/cpu/cpu4/cpufreq/cpuinfo_cur_freq",
                                    "warmup_frequency_sample_count": 3,
                                    "warmup_frequency_in_range_consecutive": 3,
                                    "warmup_frequency_required_consecutive": 3,
                                    "warmup_frequency_max_samples": 12,
                                    "warmup_frequency_min_khz": condition["frequency_khz"],
                                    "warmup_frequency_max_khz": condition["frequency_khz"],
                                    "warmup_frequency_last_khz": condition["frequency_khz"],
                                    "warmup_frequency_verified": True,
                                    "warmup_work_units": 3 * min(condition["work_units"], 32768),
                                    "binary_sha256": "a" * 64})
            measurements = {"schema_version": "parallel_calibration_measurements/v1",
                            "plan_id": plan["id"], "hardware_profile_id": plan["hardware_profile_id"],
                            "provenance": {"source_kind": "measured", "device_id": "ISOLATED_QA_DEVICE",
                                           "os_build": "TEST_ONLY", "collector": "unit-test",
                                           "measured_at": "2026-10-29T00:00:00Z", "cpu_map": plan["cpu_map"]},
                            "samples": samples}
            fitted = manager.calibration.fit({"plan_id": plan["id"], "measurements": measurements})
            self.assertTrue(fitted["quality"]["passed"], fitted["quality"])
            classes = fitted["model_preview"]["profiles"]["middle"]["frequency_roofline"]["classes"]
            for workload_class, (knee, above, below) in parameters.items():
                self.assertEqual(classes[workload_class]["knee_khz"], knee)
                self.assertAlmostEqual(classes[workload_class]["above_knee_exponent"], above, places=8)
                self.assertAlmostEqual(classes[workload_class]["below_knee_exponent"], below, places=8)
                self.assertEqual(classes[workload_class]["frequency_domain_khz"], train_frequencies)
            self.assertLess(fitted["quality"]["roofline_training_mape_max"], 1e-8)
            version = manager.calibration.publish({"fit_id": fitted["id"]})
            self.assertEqual(version["version"], "1.3.0")
            self.assertEqual(version["model"]["version"], "1.3.0")
            resolved = manager.calibration.resolve_model(version["id"])
            self.assertEqual(resolved["profiles"]["middle"]["frequency_roofline"]["schema_version"],
                             "frequency_roofline/v1")

            # Exercise the complete Trace + PMU classification -> graph class
            # annotation -> search consumption path at a low OPP.  The same
            # trace must select different calibrated curves solely from its
            # feature profile, rather than from a UI hint or model default.
            from backend.tests.test_workload_modeling_integration import two_stage_trace

            dataset = manager.import_dataset({"trace_text": two_stage_trace(),
                                              "hardware_profile_id": HARDWARE_ID})
            source = manager.workload_features.analyze({"dataset_id": dataset["id"]})

            def feature_profile(workload_class):
                if workload_class == "compute":
                    counters = {"instructions": 2000, "cycles": 1000, "llc_misses": 2}
                else:
                    counters = {"instructions": 1000, "cycles": 2000, "llc_misses": 40}
                measurements = {
                    "schema_version": "workload_stage_measurements/v1",
                    "dataset_id": dataset["id"], "hardware_profile_id": HARDWARE_ID,
                    "clock_domain": "trace_monotonic",
                    "clock_alignment_evidence": "Isolated synthetic fixture uses trace clock",
                    "provenance": {"source": "unit-test", "run_id": workload_class,
                                   "collector": "test", "data_kind": "synthetic_fixture"},
                    "stages": [{
                        "stage_id": stage["id"], "start_ns": stage["start_ns"],
                        "end_ns": stage["end_ns"], "tid": 10,
                        "measurement_scope": "thread_user",
                        "counters": {key: {"value": value, "unit": "count", "event": key}
                                     for key, value in counters.items()},
                        "event_mapping": {"llc_misses": {"verified": True,
                                                          "cache_level": "LLC",
                                                          "event": "llc_misses"}},
                    } for stage in source["stages"]],
                }
                return manager.workload_features.analyze({"dataset_id": dataset["id"],
                                                          "measurements": measurements})

            searches = {}
            for workload_class in ("compute", "memory"):
                profile = feature_profile(workload_class)
                self.assertEqual(set(profile["component_classes"].values()), {workload_class})
                analysis = manager.analyze({"dataset_id": dataset["id"],
                                            "model_package_id": version["id"],
                                            "feature_profile_id": profile["id"]})
                searches[workload_class] = manager.search({
                    "analysis_id": analysis["id"], "feature_profile_id": profile["id"],
                    "space": {"available_core_group_ids": ["middle"],
                              "main_core_group_id": "middle", "worker_counts": [1],
                              "main_participation": [True], "frequency_policy": "all_opps",
                              "frequencies_khz": {"middle": [frequencies[0]]}},
                })
            self.assertGreater(searches["compute"]["best"]["metrics"]["finish_ms"],
                               searches["memory"]["best"]["metrics"]["finish_ms"])
            self.assertEqual(searches["compute"]["best"]["graph"]["component_classes"],
                             searches["compute"]["workload_profile"]["component_classes"])
            memory_usage = searches["memory"]["best"]["frequency_model_usage"]
            self.assertGreater(memory_usage["class_roofline_core_ms"], 0)
            self.assertEqual({item["workload_class"] for item in memory_usage["matched_models"]},
                             {"memory"})
            self.assertEqual(memory_usage["fallback_reasons"], {"non_component_cpu": memory_usage["legacy_fallback_core_ms"]})
            self.assertEqual(searches["compute"]["search_provenance"]["engine"],
                             SEARCH_ENGINE)
            self.assertNotEqual(searches["compute"]["id"], searches["memory"]["id"])


if __name__ == "__main__":
    unittest.main()
