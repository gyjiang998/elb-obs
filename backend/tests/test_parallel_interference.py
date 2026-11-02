"""Numerical contracts in temporary state; these fixtures are not device evidence."""
import copy
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from backend.core import PlatformError
from backend.parallel_modeling import ParallelModelingManager
from backend.parallel_modeling import service_dag
from backend.parallel_modeling.interference import SCHEMA, calibrated_factor

BETAS = {("compute", "compute"): .2, ("compute", "memory"): .35, ("compute", "mixed"): .25,
         ("memory", "compute"): .3, ("memory", "memory"): .5, ("memory", "mixed"): .4,
         ("mixed", "compute"): .25, ("mixed", "memory"): .45, ("mixed", "mixed"): .3}


def pair_measurements(plan):
    rows = []
    for condition in plan["conditions"]:
        assert condition["benchmark"] == "pair_work"
        target = plan["cpu_map"][condition["core_kind"]][0]
        peers = [cpu for cpu in plan["cpu_map"].get(condition["peer_core_kind"], []) if cpu != target]
        beta = BETAS[(condition["workload_class"], condition["peer_workload_class"])] if peers else 0
        cpu = condition["work_units"] * .0001 * (1 + beta)
        for repeat in range(condition["repeats"]):
            identity = "ISOLATED_SYNTHETIC_" + condition["id"] + "_" + str(repeat)
            rows.append({**condition, "sample_id": identity, "run_id": identity, "repeat": repeat,
                         "workload_version": "parallel-calibration-proxy-v6",
                         "cpu_ms": cpu, "wall_ms": cpu * 1.01, "cpu": target,
                         "helpers": ([{"cpu": peers[0], "actual_cpu": peers[0],
                                      "affinity_verified": True}] if peers else []),
                         "observer_independent_core": True, "observer_cpu": 3,
                         "observed_frequency_khz": condition["frequency_khz"],
                         "frequency_mean_khz": condition["frequency_khz"],
                         "frequency_min_khz": condition["frequency_khz"],
                         "frequency_max_khz": condition["frequency_khz"],
                         "frequency_sample_count": 20, "frequency_read_failures": 0,
                         "frequency_verified": True,
                         "frequency_source": "cpuinfo_cur_freq", "frequency_source_cpu": target,
                         "frequency_source_path": f"/sys/devices/system/cpu/cpu{target}/cpufreq/cpuinfo_cur_freq",
                         "warmup_frequency_sample_count": 3,
                         "warmup_frequency_in_range_consecutive": 3,
                         "warmup_frequency_required_consecutive": 3,
                         "warmup_frequency_max_samples": 12,
                         "warmup_frequency_min_khz": condition["frequency_khz"],
                         "warmup_frequency_max_khz": condition["frequency_khz"],
                         "warmup_frequency_last_khz": condition["frequency_khz"],
                         "warmup_frequency_verified": True,
                         "warmup_work_units": 3 * min(condition["work_units"], 32768),
                         "peer_frequency_verified": True,
                         "peer_frequency_sample_count": 20 if peers else 0,
                         "peer_frequency_read_failures": 0,
                         "peer_observed_frequency_khz": condition["peer_frequency_khz"],
                         "peer_frequency_min_khz": condition["peer_frequency_khz"], "peer_frequency_max_khz": condition["peer_frequency_khz"],
                         **({"peer_frequency_source": "cpuinfo_cur_freq",
                             "peer_frequency_source_cpu": peers[0],
                             "peer_frequency_source_path": f"/sys/devices/system/cpu/cpu{peers[0]}/cpufreq/cpuinfo_cur_freq"}
                            if peers else {}),
                         "affinity_verified": True, "physical_core_placement_verified": True, "concurrency_verified": True,
                         "target_sibling_cpus": [], "target_sibling_activity": [],
                         "target_sibling_activity_verified": True,
                         "target_sibling_busy_fraction": 0,
                         "target_sibling_busy_threshold": .05,
                         "target_sibling_quietness_verified": True,
                         "thermal_throttled": False,
                         "thermal_snapshot_before": [{"zone": "thermal_zone0", "type": "cpu",
                                                       "temperature_raw": 41000}],
                         "thermal_snapshot_after": [{"zone": "thermal_zone0", "type": "cpu",
                                                      "temperature_raw": 41100}],
                         "binary_sha256": "a" * 64})
    return {"schema_version": "parallel_calibration_measurements/v1", "plan_id": plan["id"],
            "hardware_profile_id": plan["hardware_profile_id"], "samples": rows,
            "provenance": {"source_kind": "measured", "device_id": "ISOLATED_NUMERICAL_TEST_NOT_REAL", "os_build": "TEST_ONLY",
                           "collector": "synthetic-test-harness", "measured_at": "2026-10-21T00:00:00Z", "cpu_map": plan["cpu_map"]}}


def concurrent_graph(classes, durations=None):
    nodes, edges, groups = [], [], {}
    for index, workload in enumerate(classes):
        group = "build_" + str(index)
        groups[group] = {"branch": True, "cpu_ms": 1}
        for kind in ("begin", "cpu", "end"):
            nodes.append({"id": f"{kind}_{index}", "group": group, "kind": kind, "duration_ms": (durations or [1] * len(classes))[index] if kind == "cpu" else 0,
                          **({"component": group, "workload_class": workload} if kind == "cpu" else {})})
        for source, target in ((f"begin_{index}", f"cpu_{index}"), (f"cpu_{index}", f"end_{index}"), (f"end_{index}", "terminal")):
            edges.append({"source": source, "target": target})
    nodes.append({"id": "terminal", "kind": "marker", "duration_ms": 0})
    return {"nodes": nodes, "edges": edges, "groups": groups, "terminal": "terminal"}


class PairwiseInterferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.manager = ParallelModelingManager(Path(self.tmp.name))
        self.calibration = self.manager.calibration
        self.plan = self.calibration.create_plan({"parameter_groups": ["pairwise_interference"], "repeats": 3})
        self.measurements = pair_measurements(self.plan)

    def tearDown(self): self.tmp.cleanup()

    def fit(self, data=None):
        return self.calibration.fit({"plan_id": self.plan["id"], "measurements": data or self.measurements})

    def test_directional_matrix_publish_preserves_domain_and_missing_parameters(self):
        fit = self.fit()
        self.assertTrue(fit["quality"]["passed"], fit["quality"])
        version = self.calibration.publish({"fit_id": fit["id"]})
        model = self.calibration.resolve_model(version["id"])
        matrix = model["interference_model"]
        self.assertEqual(matrix["status"], "proxy_holdout_validated")
        self.assertEqual(matrix["active_tasks_domain"], [2])
        self.assertEqual(len(matrix["cells"]), 9)
        for cell in matrix["cells"]:
            self.assertAlmostEqual(cell["beta"], BETAS[cell["victim_class"], cell["peer_class"]])
            self.assertEqual((cell["victim_cpu"], cell["peer_cpu"]), (4, 6))
            self.assertEqual(cell["working_set_bytes"], 64 * 1024 * 1024)
        self.assertIn("profiles.middle.alpha", version["inherited_parameters"])
        self.assertEqual(version["validation"]["application_validation"], "not_application_validated")

    def test_missing_peer_frequency_or_solo_evidence_cannot_publish(self):
        bad = copy.deepcopy(self.measurements)
        next(row for row in bad["samples"] if row["active_tasks"] == 2)["peer_frequency_verified"] = False
        self.assertFalse(self.fit(bad)["quality"]["passed"])
        bad = copy.deepcopy(self.measurements)
        bad["samples"] = [row for row in bad["samples"] if row["active_tasks"] == 2]
        result = self.fit(bad)
        self.assertFalse(result["quality"]["passed"])
        with self.assertRaises(PlatformError): self.calibration.publish({"fit_id": result["id"]})

    def test_heldout_outlier_or_changed_physical_placement_rejected(self):
        bad = copy.deepcopy(self.measurements)
        for row in bad["samples"]:
            if row["split"] == "validation" and row["workload_class"] == "memory":
                row["cpu_ms"] *= 1.5; row["wall_ms"] *= 1.5
        self.assertFalse(self.fit(bad)["quality"]["passed"])
        bad = copy.deepcopy(self.measurements)
        next(row for row in bad["samples"] if row["split"] == "validation" and row["active_tasks"] == 2)["helpers"][0]["cpu"] = 8
        self.assertFalse(self.fit(bad)["quality"]["passed"])

    def test_input_permutation_preserves_fit(self):
        other = copy.deepcopy(self.measurements)
        other["samples"].reverse()
        first, second = self.fit()["model_preview"]["interference_model"], self.fit(other)["model_preview"]["interference_model"]
        self.assertEqual({cell["id"]: cell["beta"] for cell in first["cells"]}, {cell["id"]: cell["beta"] for cell in second["cells"]})

    def test_combined_single_and_pair_calibration_keep_independent_measurements(self):
        from backend.tests.test_parallel_calibration import build_measurements, FIXTURE_PARAMETERS
        from backend.parallel_modeling.calibration import ALL_GROUPS
        plan = self.calibration.create_plan({"parameter_groups": list(ALL_GROUPS), "repeats": 3})
        legacy = {**plan, "conditions": [row for row in plan["conditions"] if row["benchmark"] != "pair_work"]}
        pair = {**plan, "conditions": [row for row in plan["conditions"] if row["benchmark"] == "pair_work"]}
        data = pair_measurements(pair)
        data["samples"].extend(build_measurements(legacy)["samples"])
        result = self.calibration.fit({"plan_id": plan["id"], "measurements": data})
        self.assertTrue(result["quality"]["passed"], result["quality"])
        self.assertEqual(len(result["model_preview"]["interference_model"]["cells"]), 9)
        for kind in ("big", "middle", "small"):
            self.assertAlmostEqual(result["model_preview"]["profiles"][kind]["frequency_elasticity"], FIXTURE_PARAMETERS["frequency_elasticity"][kind])

    def simulate(self, classes, durations=None, matrix=None):
        graph = concurrent_graph(classes, durations)
        frequency = self.manager.model["profiles"]["middle"]["max_khz"]
        lane = {"kind": "middle", "khz": frequency}
        profile = {"max_khz": frequency, "build_ratio": 1, "residual_ratio": 1, "alpha": .9}
        return service_dag.simulate(graph, {"main": lane, "workers": [lane] * len(classes)}, {"middle": profile},
                                    overhead_ms=0, interference_model=matrix or self.fit()["model_preview"]["interference_model"])

    def test_classes_change_prediction_and_unknown_or_three_way_falls_back(self):
        compute = self.simulate(["compute", "compute"])
        memory = self.simulate(["memory", "memory"])
        self.assertAlmostEqual(compute["finish_ms"], 1.2)
        self.assertAlmostEqual(memory["finish_ms"], 1.5)
        self.assertEqual(compute["interference"]["status"], "calibrated_pair_proxy")
        self.assertAlmostEqual(compute["interference"]["calibrated_cpu_ms"], 2.4)
        unknown = self.simulate(["unknown", "compute"])
        self.assertAlmostEqual(unknown["finish_ms"], 1.9)
        self.assertIn("missing_workload_class", unknown["interference"]["fallback_reasons"])
        three = self.simulate(["compute"] * 3)
        self.assertAlmostEqual(three["finish_ms"], 2.8)
        self.assertIn("outside_two_task_domain", three["interference"]["fallback_reasons"])

    def test_interference_stops_when_peer_finishes(self):
        value = self.simulate(["compute", "memory"], [1, 2])
        expected = 1.35 + (2 - 1.35 / 1.3)
        self.assertAlmostEqual(value["finish_ms"], expected)
        self.assertLess(value["finish_ms"], 2 * 1.3)

    def test_unmeasured_frequency_and_unvalidated_matrix_cannot_match(self):
        matrix = self.fit()["model_preview"]["interference_model"]
        lane = {"kind": "middle", "khz": 1}
        factor, reason, _ = calibrated_factor(matrix, "compute", "memory", lane, lane, 2)
        self.assertIsNone(factor)
        self.assertEqual(reason, "unmeasured_class_core_frequency_pair")
        matrix["status"] = "provisional"
        self.assertEqual(calibrated_factor(matrix, "compute", "memory", lane, lane, 2)[1], "no_validated_pair_model")

    def test_collector_dry_run_plans_distinct_kernels_and_peer_frequency(self):
        source = Path(__file__).resolve().parents[2] / "tools/parallel_calibration/collect.py"
        spec = importlib.util.spec_from_file_location("pair_collect", source)
        collector = importlib.util.module_from_spec(spec); spec.loader.exec_module(collector)
        condition = next(row for row in self.plan["conditions"] if row["workload_class"] == "compute" and row["peer_workload_class"] == "memory")
        command = collector.benchmark_command(Path("/never-run"), condition, self.plan["cpu_map"], SimpleNamespace(observer_cpu=None, sample_period_us=1000, frequency_tolerance=.03))
        self.assertEqual(command[command.index("--workload-class") + 1], "compute")
        self.assertEqual(command[command.index("--peer-workload-class") + 1], "memory")
        self.assertEqual(command[command.index("--helper-cpus") + 1], "6")
        self.assertEqual(command[command.index("--peer-frequency-khz") + 1], str(condition["peer_frequency_khz"]))
        observer = collector.fixed_observer_cpu(self.plan, self.plan["cpu_map"])
        for row in self.plan["conditions"]:
            target, helpers, actual = collector.placements(row, self.plan["cpu_map"], observer)
            self.assertEqual(actual, observer)
            self.assertNotIn(observer, [target, *helpers])
        with self.assertRaises(ValueError): collector.fixed_observer_cpu(self.plan, self.plan["cpu_map"], 6)

    def test_changed_observer_between_solo_and_corun_cannot_fit(self):
        data = copy.deepcopy(self.measurements)
        for row in data["samples"]:
            if row["active_tasks"] == 2: row["observer_cpu"] = 2
        result = self.fit(data)
        self.assertFalse(result["quality"]["passed"])
        self.assertTrue(any("单跑锚点" in error for error in result["quality"]["errors"]))


if __name__ == "__main__": unittest.main()
