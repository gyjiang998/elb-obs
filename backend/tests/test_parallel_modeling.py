from __future__ import annotations

import copy
import hashlib
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.core import ArtifactStore, PlatformError
from backend.parallel_modeling import ParallelModelingManager
from backend.parallel_modeling.manager import (
    ANALYSIS_ENGINE, ARTIFACT_PRODUCER, BUNDLE_ROOT, HARDWARE_ID, MODEL_ID,
    SEARCH_ENGINE,
)
from backend.parallel_modeling.service_dag import validate_graph


def trace_fixture(*, binder=False):
    records = [
        (1.000000, 10, "sched_switch", "prev_comm=idle prev_pid=0 prev_prio=120 prev_state=R ==> next_comm=app next_pid=10 next_prio=120"),
        (1.001000, 10, "tracing_mark_write", "B|10|H:UIVsyncTask[timestamp:1000000000][vsyncID:1][instanceID:1]"),
        (1.002000, 10, "tracing_mark_write", "B|10|H:CustomNode:BuildItem [Widget]"),
    ]
    if binder:
        records.extend([
            (1.003000, 10, "binder_transaction", "transaction=100 dest_proc=20 dest_thread=20 reply=0 flags=0x0"),
            (1.003100, 10, "sched_switch", "prev_comm=app prev_pid=10 prev_prio=120 prev_state=S ==> next_comm=server next_pid=20 next_prio=120"),
            (1.003200, 20, "binder_transaction_received", "transaction=100"),
            (1.003400, 20, "binder_transaction", "transaction=101 dest_proc=10 dest_thread=10 reply=1 flags=0x0"),
            (1.003500, 20, "sched_wakeup", "comm=app pid=10 prio=120 target_cpu=0"),
            (1.003600, 20, "sched_switch", "prev_comm=server prev_pid=20 prev_prio=120 prev_state=S ==> next_comm=app next_pid=10 next_prio=120"),
            (1.003700, 10, "binder_transaction_received", "transaction=101"),
        ])
    records.extend([
        (1.004000, 10, "tracing_mark_write", "E|10"),
        (1.006000, 10, "tracing_mark_write", "E|10"),
        (1.007000, 10, "sched_switch", "prev_comm=app prev_pid=10 prev_prio=120 prev_state=S ==> next_comm=idle next_pid=0 next_prio=120"),
    ])
    return "".join(f"app-{tid} ( {tid:4d}) [000] .... {timestamp:.6f}: {event}: {body}\n" for timestamp, tid, event, body in records)


class ParallelModelingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manager = ParallelModelingManager(self.root, ArtifactStore(self.root / "artifacts"))

    def tearDown(self):
        self.temporary.cleanup()

    def builtin_analysis(self):
        dataset = self.manager.import_dataset({"builtin_id": "gallery-edit-pmu-20261014"})
        return self.manager.analyze({"dataset_id": dataset["id"]})

    def raw_analysis(self, binder=False, hardware=HARDWARE_ID):
        dataset = self.manager.import_dataset({"trace_text": trace_fixture(binder=binder), "hardware_profile_id": hardware})
        return self.manager.analyze({"dataset_id": dataset["id"]})

    def test_archived_replay_and_real_component_features(self):
        analysis = self.builtin_analysis()
        self.assertAlmostEqual(analysis["baseline"]["duration_ms"], 86.940, places=8)
        self.assertAlmostEqual(analysis["baseline"]["replay_ms"], 86.940, places=8)
        self.assertAlmostEqual(analysis["baseline"]["app_cpu_ms"], 76.094, places=8)
        self.assertEqual(analysis["baseline"]["service_call_count"], 65)
        self.assertEqual(len(analysis["components"]), 26)
        self.assertTrue(analysis["replay_gate"]["passed"])
        video = next(item for item in analysis["components"] if item["name"] == "VideoPlay")
        self.assertEqual(len(video["request_ids"]), 21)
        self.assertEqual(video["instruction_count_bounds"], [15876248, 16519967])
        self.assertTrue(video["pmu"]["bounds_valid"])
        self.assertIsNone(video["dram_bytes"])
        self.assertEqual(analysis["statistics"]["pmu_qualified_components"], 9)
        self.assertEqual(analysis["cost_model"]["status"], "legacy_compat")
        self.assertEqual(analysis["analysis_provenance"]["engine"], ANALYSIS_ENGINE)
        self.assertGreater(analysis["baseline"]["app_off_cpu_ms"], 0)
        accounting = analysis["baseline"]["time_accounting"]
        self.assertEqual(accounting["observed_reference"]["app_off_cpu_ms"],
                         analysis["baseline"]["app_off_cpu_ms"])
        self.assertAlmostEqual(accounting["observed_reference"]["app_wall_ms"],
                               accounting["observed_reference"]["app_on_cpu_ms"]
                               + accounting["observed_reference"]["app_off_cpu_ms"])
        self.assertEqual(accounting["prediction"]["dag_makespan_ms"], analysis["baseline"]["replay_ms"])
        self.assertIn("90.409", analysis["provenance"]["distinct_historical_sample"])
        self.assertTrue(analysis["artifacts"])
        self.assertEqual(analysis["artifacts"][0]["artifact_type"], "parallel_modeling_analysis")
        artifact = self.manager.artifact_store.read(
            "parallel_modeling_analysis", analysis["artifacts"][0]["artifact_id"])
        self.assertEqual(artifact["producer"], ARTIFACT_PRODUCER)

    def test_synchronous_blocked_time_is_accounted_but_replayed_by_dependency(self):
        analysis = self.raw_analysis(binder=True)
        accounting = analysis["baseline"]["time_accounting"]
        observed = accounting["observed_reference"]
        self.assertGreater(observed["synchronous_blocked_ms"], 0)
        self.assertAlmostEqual(observed["app_wall_ms"], observed["app_on_cpu_ms"] + observed["app_off_cpu_ms"])
        self.assertIn("no directly added duration", accounting["off_cpu_policy"]["synchronous_blocked"])
        self.assertIn("DAG makespan", accounting["prediction"]["meaning"])
        awaits = [node for node in analysis["graphs"]["observed"]["nodes"] if node["kind"] == "await"]
        self.assertTrue(awaits)
        self.assertTrue(all(node["duration_ms"] == 0 for node in awaits))
        self.assertAlmostEqual(sum(node["observed_duration_ms"] for node in awaits),
                               observed["synchronous_blocked_ms"])

    def test_fastest_archive_configuration_and_dependency_legality(self):
        analysis = self.builtin_analysis()
        result = self.manager.search({"analysis_id": analysis["id"], "space": {
            "available_core_group_ids": ["big", "middle"], "worker_counts": [3], "frequency_policy": "max_opps"}})
        best = result["best"]
        self.assertAlmostEqual(best["metrics"]["finish_ms"], 53.87303045981391, places=8)
        self.assertAlmostEqual(best["metrics"]["app_core_ms"], 109.83574815232885, places=8)
        self.assertTrue(best["configuration"]["main_participates"])
        self.assertEqual(best["configuration"]["dispatch"], "main_first")
        self.assertEqual(result["candidates"][0]["configuration"], best["configuration"])
        self.assertTrue(result["candidates"][0]["feasible"])
        self.assertTrue(all("comparison" in row for row in result["candidates"]))
        self.assertTrue(validate_graph(best["graph"]))
        for edge in best["graph"]["edges"]:
            self.assertLessEqual(best["node_end_ms"][edge["source"]], best["node_start_ms"][edge["target"]] + 1e-7)
        observed_service_edges = {tuple(edge[key] for key in ("source", "target", "kind")) for edge in analysis["graphs"]["observed"]["edges"] if edge["kind"] != "within_task_order"}
        parallel_edges = {tuple(edge[key] for key in ("source", "target", "kind")) for edge in best["graph"]["edges"]}
        self.assertTrue(observed_service_edges <= parallel_edges)
        self.assertGreater(best["comparison"]["app_cpu_delta_ms"], 0)
        self.assertGreater(best["comparison"]["latency_gain_pct"], 37)
        self.assertAlmostEqual(sum(row["app_core_ms"] for row in best["resource_costs"]["application_lanes"]), best["metrics"]["app_core_ms"])
        self.assertAlmostEqual(sum(row["service_core_ms"] for row in best["resource_costs"]["services"]), best["metrics"]["service_core_ms"])
        self.assertEqual(len(best["component_costs"]), 26)
        self.assertEqual(result["search_provenance"]["engine"], SEARCH_ENGINE)
        self.assertEqual(result["search_configuration"], {
            "schema_version": "parallel_search_configuration/v1",
            "objective": "minimum_latency",
            "budget_ms": analysis["baseline"]["duration_ms"],
            "candidate_ids": sorted(item["id"] for item in analysis["components"]),
            "model_package_id": result["model_package"]["id"],
            "feature_profile_id": None,
            "space": {
                "available_core_group_ids": ["big", "middle"],
                "main_core_group_id": "big",
                "worker_counts": [3],
                "main_participation": [False, True],
                "frequency_policy": "max_opps",
            },
            "robustness": False,
            "dependency_policy": "preserve_observed",
        })
        self.assertFalse(result["search_provenance"]["robustness_requested"])
        self.assertIsNone(result["search_provenance"]["nominal_parameters"]["frequency_elasticity"])
        self.assertNotIn("explicit_sensitivity_override",
                         best["frequency_model_usage"]["fallback_reasons"])
        artifact = self.manager.artifact_store.read(
            "parallel_modeling_search", result["artifacts"][0]["artifact_id"])
        self.assertEqual(artifact["producer"], ARTIFACT_PRODUCER)

    def test_frequency_budget_selection_and_reproducibility(self):
        analysis = self.builtin_analysis()
        payload = {"analysis_id": analysis["id"], "objective": "minimum_frequency_with_budget", "budget_ms": 86.94,
                   "space": {"available_core_group_ids": ["big", "middle"], "worker_counts": [3], "main_participation": True,
                             "frequencies_khz": {"big": [1325000, 1440000], "middle": [1076000, 1325000]}}}
        first = self.manager.search(payload)
        second = self.manager.search(payload)
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["best"], second["best"])
        self.assertEqual(first["best"]["frequency_vector"], {"big": 1325000, "middle": 1076000, "small": None})
        self.assertLessEqual(first["best"]["metrics"]["finish_ms"], 86.94)
        self.assertGreater(first["best"]["metrics"]["finish_ms"], 86.4)
        robust = self.manager.search({**payload, "robustness": True})
        self.assertEqual(robust["search_configuration"]["objective"], "minimum_frequency_with_budget")
        self.assertEqual(robust["search_configuration"]["budget_ms"], 86.94)
        self.assertTrue(robust["search_configuration"]["robustness"])
        self.assertEqual(robust["search_configuration"]["space"]["frequency_policy"], "all_opps")
        self.assertEqual(robust["search_configuration"]["space"]["main_core_group_id"], "big")
        self.assertEqual(robust["search_configuration"]["space"]["main_participation"], [True])
        self.assertTrue(robust["search_provenance"]["robustness_requested"])
        self.assertEqual(robust["best"]["frequency_vector"], {"big": 1440000, "middle": 1325000, "small": None})
        self.assertLessEqual(robust["best"]["metrics"]["scenario_worst_ms"], 86.94)
        self.assertEqual(robust["best"]["metrics"]["scenario_count"], 18)
        self.assertEqual(robust["candidates"][0]["configuration_index"], robust["best"]["configuration_index"])
        self.assertTrue(robust["candidates"][0]["feasible"])

    def test_process_parallel_search_matches_serial_result(self):
        analysis = self.builtin_analysis()
        payload = {
            "analysis_id": analysis["id"],
            "objective": "minimum_frequency_with_budget",
            "budget_ms": 86.94,
            "robustness": True,
            "space": {
                "available_core_group_ids": ["big", "middle"],
                "worker_counts": [1, 2],
                "main_participation": [False, True],
                "frequencies_khz": {
                    "big": [1325000, 2500000],
                    "middle": [1076000, 2151000],
                },
            },
        }
        with patch.dict(os.environ, {"HETERO_PARALLEL_SEARCH_WORKERS": "1"}):
            serial = self.manager.search(payload)
        with patch.dict(os.environ, {"HETERO_PARALLEL_SEARCH_WORKERS": "2"}), patch(
                "backend.parallel_modeling.search_executor.PARALLEL_MIN_CONFIGURATIONS", 1):
            parallel = self.manager.search(payload)

        self.assertEqual(serial["id"], parallel["id"])
        for field in (
            "best", "candidates", "pareto", "core_combination_recommendations",
            "search_provenance", "assumptions",
        ):
            self.assertEqual(serial[field], parallel[field], field)

    def test_raw_trace_autodetection_and_exact_binder_pairing(self):
        analysis = self.raw_analysis(binder=True)
        self.assertEqual(analysis["baseline"]["duration_ms"], 5.0)
        self.assertAlmostEqual(analysis["baseline"]["app_cpu_ms"], 4.5)
        self.assertEqual(analysis["baseline"]["service_call_count"], 1)
        self.assertAlmostEqual(analysis["baseline"]["service_cpu_ms"], .2)
        self.assertEqual(analysis["components"][0]["request_ids"], [100])
        self.assertTrue(analysis["replay_gate"]["passed"])
        self.assertEqual(analysis["provenance"]["selection"]["target_tid"], 10)
        self.assertFalse(analysis["components"][0]["pmu"]["bounds_valid"])

    def test_shared_frequency_domain_and_physical_cores(self):
        analysis = self.raw_analysis()
        result = self.manager.search({"analysis_id": analysis["id"], "space": {"main_core_group_id": "middle",
            "available_core_group_ids": ["middle"], "worker_counts": [1, 2, 3, 4], "main_participation": True}})
        for row in result["candidates"]:
            config = row["configuration"]
            self.assertLessEqual(1 + len(config["workers"]), 3)
            self.assertEqual({lane["khz"] for lane in [config["main"], *config["workers"]]}, {config["main"]["khz"]})
        with self.assertRaises(PlatformError):
            self.manager.search({"analysis_id": analysis["id"], "space": {"frequencies_khz": {"big": [999999]}}})
        with self.assertRaises(PlatformError):
            self.manager.search({"analysis_id": analysis["id"], "space": {"available_core_group_ids": ["small"], "main_core_group_id": "big"}})

    def test_candidate_selection_is_applied(self):
        analysis = self.builtin_analysis()
        component = analysis["components"][1]
        result = self.manager.search({"analysis_id": analysis["id"], "candidate_ids": [component["id"]],
            "space": {"frequency_policy": "max_opps", "worker_counts": [1], "available_core_group_ids": ["big", "middle"]}})
        branches = [key for key, group in result["best"]["graph"]["groups"].items() if group["branch"]]
        self.assertEqual(branches, [component["id"]])
        with self.assertRaises(PlatformError):
            self.manager.search({"analysis_id": analysis["id"], "candidate_ids": ["missing"]})

    def test_max_opps_search_includes_requested_serial_and_selected_parallel_work(self):
        analysis = self.builtin_analysis()
        result = self.manager.search({"analysis_id": analysis["id"], "candidate_ids": ["build_01"], "space": {
            "available_core_group_ids": ["big", "middle"], "worker_counts": [0, 3], "frequency_policy": "max_opps"}})
        serial = [row for row in result["candidates"] if not row["configuration"]["workers"]]
        parallel = [row for row in result["candidates"] if row["configuration"]["workers"]]
        self.assertEqual(len(serial), 1)
        self.assertAlmostEqual(serial[0]["metrics"]["finish_ms"], 86.940)
        self.assertEqual(len(parallel), 3)
        self.assertEqual(result["candidate_ids"], ["build_01"])
        self.assertIn("IPC 21 次", result["best"]["graph"]["mermaid"])

    def test_unknown_hardware_bad_input_and_unattainable_budget(self):
        with self.assertRaises(PlatformError):
            self.manager.import_dataset({"trace_text": trace_fixture(), "hardware_profile_id": "another-soc"})
        analysis = self.raw_analysis(hardware=None)
        with self.assertRaises(PlatformError) as caught:
            self.manager.search({"analysis_id": analysis["id"]})
        self.assertEqual(caught.exception.code, "parallel_modeling_unknown_hardware")
        with self.assertRaises(PlatformError):
            self.manager.import_dataset({"trace_text": "B|10|GALLERY_STEP_07_edit\nE|10"})
        with self.assertRaises(PlatformError):
            self.manager.import_dataset({"parsed": {"frame": {}}})
        parsed = json.loads((BUNDLE_ROOT / "gallery-edit-pmu-20261014.json").read_text())
        parsed["frame"]["units"][0]["phases"][0]["end_ns"] += 1
        with self.assertRaises(PlatformError):
            self.manager.import_dataset({"parsed": parsed})
        analysis = self.raw_analysis()
        with self.assertRaises(PlatformError) as caught:
            self.manager.search({"analysis_id": analysis["id"], "objective": "minimum_frequency_with_budget", "budget_ms": .01,
                                 "space": {"frequency_policy": "max_opps", "worker_counts": [1]}})
        self.assertEqual(caught.exception.code, "parallel_modeling_no_feasible_configuration")
        with self.assertRaises(PlatformError):
            self.manager.search({"analysis_id": analysis["id"], "model_package_id": "unknown"})

    def test_capture_managed_paths_hashes_and_quality(self):
        relative = Path("scenario-capture/runs/capture_test/step-07/trace.ftrace")
        destination = self.root / relative
        destination.parent.mkdir(parents=True)
        destination.write_text(trace_fixture())
        manifest = {"schema_version": "scenario_capture_manifest/v1", "run_id": "capture_test", "plan_id": "plan_test", "title": "图库17步", "status": "completed",
                    "steps": [{"step_id": "step-07", "ordinal": 7, "title": "点击编辑", "captured": True, "target_pid": 10,
                               "collector_status": {"trace": "collected"}, "trace_validation": {"valid": True}}],
                    "files": [{"step_id": "step-07", "kind": "trace", "managed_path": str(relative), "sha256": hashlib.sha256(destination.read_bytes()).hexdigest()}]}
        dataset = self.manager.import_dataset({"capture_manifest": manifest, "step_id": "step-07"})
        self.assertEqual(dataset["summary"]["duration_ms"], 5.0)
        self.assertEqual(dataset["provenance"]["capture"]["run_id"], "capture_test")
        self.assertEqual(dataset["provenance"]["capture"]["plan_title"], "图库17步")
        self.assertEqual(dataset["provenance"]["capture"]["step_title"], "点击编辑")
        self.assertEqual(dataset["provenance"]["capture"]["step_ordinal"], 7)
        bad = copy.deepcopy(manifest)
        bad["steps"][0]["trace_validation"]["valid"] = False
        with self.assertRaises(PlatformError):
            self.manager.import_dataset({"capture_manifest": bad})
        bad = copy.deepcopy(manifest)
        bad["files"][0]["sha256"] = "0" * 64
        with self.assertRaises(PlatformError):
            self.manager.import_dataset({"capture_manifest": bad})
        bad["files"][0]["managed_path"] = "/etc/passwd"
        with self.assertRaises(PlatformError):
            self.manager.import_dataset({"capture_manifest": bad})

    def test_cancellation_between_configurations_and_persistence(self):
        analysis = self.raw_analysis()
        event = threading.Event()
        updates = []

        def progress(value):
            updates.append(value)
            if value.get("completed", 0) >= 1:
                event.set()

        with patch.dict(os.environ, {"HETERO_PARALLEL_SEARCH_WORKERS": "2"}), patch(
                "backend.parallel_modeling.search_executor.PARALLEL_MIN_CONFIGURATIONS", 1):
            with self.assertRaises(PlatformError) as caught:
                self.manager.search({"analysis_id": analysis["id"]}, cancel_event=event, progress=progress)
        self.assertEqual(caught.exception.code, "job_cancelled")
        self.assertTrue(updates)
        self.assertEqual(self.manager.list_searches(), [])
        reopened = ParallelModelingManager(self.root)
        self.assertEqual(reopened.get_analysis(analysis["id"])["baseline"], analysis["baseline"])
        self.assertEqual(len(reopened.list_datasets()), 1)


if __name__ == "__main__":
    unittest.main()
