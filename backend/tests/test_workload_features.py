from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.core import PlatformError
from backend.parallel_modeling.manager import ParallelModelingManager, HARDWARE_ID
from backend.parallel_modeling.workload_features import (
    CPU_RESIDENCY_EVIDENCE_SCHEMA,
    COUNTERS,
    RATIOS,
    WorkloadFeaturesManager,
    _classify_cpu,
    _cpu_residency_evidence,
    create_workload_features_router,
)
from backend.parallel_modeling.hardware_profiles import _hash as hardware_profile_hash
from backend.tests.test_parallel_modeling import trace_fixture


class WorkloadFeaturesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.manager = ParallelModelingManager(Path(self.temporary.name))
        self.features = WorkloadFeaturesManager(self.manager)

    def tearDown(self):
        self.temporary.cleanup()

    def dataset(self, binder=False, name="Widget"):
        return self.manager.import_dataset({"trace_text": trace_fixture(binder=binder).replace("Widget", name), "hardware_profile_id": HARDWARE_ID})

    def measurement(self, dataset, *, instructions=2000, cycles=1000, misses=2):
        stage = self.features.analyze({"dataset_id": dataset["id"]})["stages"][0]
        return {"schema_version": "workload_stage_measurements/v1", "dataset_id": dataset["id"],
                "hardware_profile_id": dataset["hardware_profile_id"], "clock_domain": "trace_monotonic",
                "clock_alignment_evidence": "synthetic fixture uses trace timestamps", "provenance": {
                    "source": "unit-test-fixture", "run_id": "test-run", "collector": "unit-test", "data_kind": "synthetic_fixture"},
                "stages": [{"stage_id": stage["id"], "start_ns": stage["start_ns"], "end_ns": stage["end_ns"],
                    "tid": dataset["summary"]["tid"], "measurement_scope": "thread_user",
                    "counters": {key: {"value": value, "unit": "count", "event": "fixture-llc-refills" if key == "llc_misses" else key}
                        for key, value in {"instructions": instructions, "cycles": cycles, "llc_misses": misses}.items()},
                    "event_mapping": {"llc_misses": {"verified": True, "cache_level": "LLC", "event": "fixture-llc-refills"}}}]}

    def replace_cpu_residency_slices(self, dataset, slices):
        path = self.manager.state_root / "inputs" / dataset["id"] / "parsed.json"
        parsed = json.loads(path.read_text())
        parsed["frame"]["evidence"]["cpu_frequency_slices"] = slices
        path.write_text(json.dumps(parsed))

    def test_trace_without_pmu_keeps_missing_counters_and_measured_schedule(self):
        dataset = self.dataset(binder=True)
        profile = self.features.analyze({"dataset_id": dataset["id"]})
        stage = profile["stages"][0]
        self.assertEqual(stage["id"], "build_00")
        self.assertEqual(stage["wall_ms"], 2)
        self.assertAlmostEqual(stage["cpu_ms"], 1.5)
        self.assertEqual(stage["scheduling"]["service_calls"], 1)
        self.assertGreater(stage["scheduling"]["binder_blocked_ms"], 0)
        self.assertIsNone(stage["compute"]["instructions"])
        self.assertIsNone(stage["compute"]["ipc"])
        self.assertIsNone(stage["memory"]["loads"])
        self.assertIsNone(stage["scheduling"]["io_wait_ms"])
        self.assertIsNone(stage["scheduling"]["io_read_bytes"])
        self.assertEqual(profile["component_classes"], {"build_00": "unknown"})
        self.assertEqual(stage["classification"]["id"], "unknown")
        self.assertEqual(stage["classification"]["confidence"], {"level": "none", "score": 0})
        self.assertEqual(stage["classification"]["evidence_quality"], "none")
        self.assertEqual(profile["quality"]["status"], "partial")
        self.assertAlmostEqual(sum(stage["states_ms"].values()), stage["wall_ms"])
        placement = stage["scheduling"]["cpu_placement"]
        self.assertEqual(placement["primary_cpu"], 0)
        self.assertEqual(placement["primary_core_group_id"], "small")
        self.assertEqual(placement["primary_core_group_name"], "小核组")
        self.assertEqual(placement["primary_proxy_kind"], "small")
        self.assertEqual(placement["observed_cpu_ids"], [0])
        self.assertFalse(placement["mixed_core_group_residency"])
        self.assertEqual(stage["scheduling"]["cpu_residency"][0]["core_group_id"], "small")
        residency_evidence = stage["scheduling"]["cpu_residency_evidence"]
        self.assertEqual(residency_evidence["schema_version"], CPU_RESIDENCY_EVIDENCE_SCHEMA)
        self.assertEqual(residency_evidence["status"], "complete")
        self.assertTrue(residency_evidence["temporal_coverage_complete"])
        self.assertFalse(residency_evidence["source_anchor_eligible"])
        self.assertIn("hardware_identity_unverified", residency_evidence["reason_codes"])
        self.assertAlmostEqual(residency_evidence["observed_on_cpu_ms"], stage["cpu_ms"])
        self.assertAlmostEqual(residency_evidence["expected_on_cpu_ms"], stage["cpu_ms"])
        self.assertAlmostEqual(residency_evidence["coverage_ratio"], 1)
        self.assertEqual(placement["coverage_status"], "complete")
        self.assertEqual(stage["quality"]["cpu_residency_status"], "complete")
        self.assertEqual(profile["summary"]["cpu_residency_anchor_stages"], 0)
        self.assertEqual(profile["summary"]["cpu_residency_status_counts"], {"complete": 1})

    def test_residency_boundary_gaps_are_partial_or_insufficient_not_complete(self):
        # Missing a short leading sched slice still preserves the visible CPU,
        # but it cannot describe the whole stage as a deterministic placement.
        missing_first = _cpu_residency_evidence(
            [{"cpu": 4, "frequency_khz": 1800000, "cpu_ms": 8}], 10)
        self.assertEqual(missing_first["status"], "partial")
        self.assertAlmostEqual(missing_first["coverage_ratio"], .8)
        self.assertFalse(missing_first["source_anchor_eligible"])

        # A larger missing tail is evidence-insufficient rather than a
        # misleading "primary CPU" observation for the complete stage.
        missing_tail = _cpu_residency_evidence(
            [{"cpu": 4, "frequency_khz": 1800000, "cpu_ms": 6}], 10)
        self.assertEqual(missing_tail["status"], "insufficient")
        self.assertAlmostEqual(missing_tail["coverage_ratio"], .6)
        self.assertFalse(missing_tail["source_anchor_eligible"])

        unavailable = _cpu_residency_evidence([], 10)
        self.assertEqual(unavailable["status"], "unavailable")
        self.assertEqual(unavailable["coverage_ratio"], 0)

    def test_missing_leading_sched_slice_is_visible_as_partial_observation(self):
        dataset = self.dataset()
        self.replace_cpu_residency_slices(dataset, [{
            "start_ns": 1_002_400_000, "end_ns": 1_004_000_000,
            "cpu": 0, "frequency_khz": 1_800_000,
        }])
        stage = self.features.analyze({"dataset_id": dataset["id"]})["stages"][0]
        evidence = stage["scheduling"]["cpu_residency_evidence"]
        self.assertEqual(evidence["status"], "partial")
        self.assertAlmostEqual(evidence["observed_on_cpu_ms"], 1.6)
        self.assertAlmostEqual(evidence["expected_on_cpu_ms"], 2)
        self.assertFalse(evidence["source_anchor_eligible"])
        self.assertEqual(stage["scheduling"]["cpu_placement"]["coverage_status"], "partial")
        self.assertEqual(stage["quality"]["cpu_residency_status"], "partial")
        self.assertIn("最长可见 CPU 不作为整阶段确定源锚", stage["quality"]["reasons"][-1])

    def test_missing_trailing_sched_slice_is_evidence_insufficient(self):
        dataset = self.dataset()
        self.replace_cpu_residency_slices(dataset, [{
            "start_ns": 1_002_000_000, "end_ns": 1_003_000_000,
            "cpu": 0, "frequency_khz": 1_800_000,
        }])
        stage = self.features.analyze({"dataset_id": dataset["id"]})["stages"][0]
        evidence = stage["scheduling"]["cpu_residency_evidence"]
        self.assertEqual(evidence["status"], "insufficient")
        self.assertAlmostEqual(evidence["coverage_ratio"], .5)
        self.assertFalse(evidence["source_anchor_eligible"])

    def test_mixed_cpu_residency_closes_coverage_across_all_slices(self):
        evidence = _cpu_residency_evidence([
            {"cpu": 2, "core_group_id": "small", "frequency_khz": 1400000, "cpu_ms": 4},
            {"cpu": 6, "core_group_id": "middle", "frequency_khz": 2300000, "cpu_ms": 3},
            {"cpu": 6, "core_group_id": "middle", "frequency_khz": 2600000, "cpu_ms": 3},
        ], 10, hardware_identity_verified=True)
        self.assertEqual(evidence["status"], "complete")
        self.assertAlmostEqual(evidence["observed_on_cpu_ms"], 10)
        self.assertAlmostEqual(evidence["coverage_ratio"], 1)
        self.assertTrue(evidence["source_anchor_eligible"])
        self.assertTrue(evidence["temporal_coverage_complete"])
        self.assertTrue(evidence["source_cost_anchor_preconditions_met"])

        dataset = self.dataset()
        self.replace_cpu_residency_slices(dataset, [
            {"start_ns": 1_002_000_000, "end_ns": 1_003_000_000,
             "cpu": 0, "frequency_khz": 1_400_000},
            {"start_ns": 1_003_000_000, "end_ns": 1_004_000_000,
             "cpu": 4, "frequency_khz": 2_300_000},
        ])
        stage = self.features.analyze({"dataset_id": dataset["id"]})["stages"][0]
        placement = stage["scheduling"]["cpu_placement"]
        self.assertEqual(placement["coverage_status"], "complete")
        self.assertTrue(placement["mixed_core_group_residency"])
        self.assertEqual(placement["observed_cpu_ids"], [0, 4])
        self.assertTrue(placement["temporal_coverage_complete"])
        self.assertFalse(placement["source_anchor_eligible"])

    def test_residency_overcoverage_is_conflicting_evidence(self):
        evidence = _cpu_residency_evidence(
            [{"cpu": 2, "frequency_khz": 1400000, "cpu_ms": 11}], 10)
        self.assertEqual(evidence["status"], "insufficient")
        self.assertGreater(evidence["coverage_ratio"], 1)
        self.assertIn("observed_residency_exceeds_expected_on_cpu_duration", evidence["reason_codes"])

    def test_cpu_identity_is_preserved_without_guessing_a_core_group(self):
        dataset = self.manager.import_dataset({"trace_text": trace_fixture()})
        stage = self.features.analyze({"dataset_id": dataset["id"]})["stages"][0]
        placement = stage["scheduling"]["cpu_placement"]
        self.assertEqual(placement["primary_cpu"], 0)
        self.assertIsNone(placement["primary_core_group_id"])
        self.assertIsNone(placement["primary_core_group_name"])
        self.assertIsNone(placement["primary_proxy_kind"])
        self.assertEqual(stage["scheduling"]["cpu_residency"][0]["cpu"], 0)
        evidence = stage["scheduling"]["cpu_residency_evidence"]
        self.assertTrue(evidence["temporal_coverage_complete"])
        self.assertFalse(evidence["source_cost_anchor_preconditions_met"])
        self.assertFalse(evidence["source_anchor_eligible"])
        self.assertIn("hardware_identity_unverified", evidence["reason_codes"])
        self.assertIn("source_core_group_unresolved", evidence["reason_codes"])

    def test_source_anchor_requires_verified_hardware_mapped_group_and_frequency(self):
        base = [{"cpu": 4, "core_group_id": "middle", "frequency_khz": 1800000,
                 "cpu_ms": 10}]
        unverified = _cpu_residency_evidence(base, 10, hardware_identity_verified=False)
        self.assertEqual(unverified["status"], "complete")
        self.assertTrue(unverified["temporal_coverage_complete"])
        self.assertFalse(unverified["source_anchor_eligible"])
        self.assertIn("hardware_identity_unverified", unverified["reason_codes"])

        unknown_group = copy.deepcopy(base)
        unknown_group[0]["core_group_id"] = None
        evidence = _cpu_residency_evidence(
            unknown_group, 10, hardware_identity_verified=True)
        self.assertFalse(evidence["source_anchor_eligible"])
        self.assertIn("source_core_group_unresolved", evidence["reason_codes"])

        partial_frequency = [*copy.deepcopy(base),
            {"cpu": 5, "core_group_id": "middle", "frequency_khz": None, "cpu_ms": 1}]
        partial_frequency[0]["cpu_ms"] = 9
        evidence = _cpu_residency_evidence(
            partial_frequency, 10, hardware_identity_verified=True)
        self.assertEqual(evidence["status"], "complete")
        self.assertFalse(evidence["source_anchor_eligible"])
        self.assertIn("source_frequency_unresolved", evidence["reason_codes"])

    def test_profile_identity_tracks_hardware_content_and_legacy_profile_stays_readable(self):
        dataset = self.dataset()
        first = self.features.analyze({"dataset_id": dataset["id"]})
        self.assertEqual(first["schema_version"], "workload_feature_profile/v2")
        self.assertEqual(first["engine_version"], "1.6.0")
        self.assertEqual(first["hardware_profile_sha256"],
                         self.manager.hardware_profiles.get(HARDWARE_ID)["profile_sha256"])

        # Pre-v2 profiles lack the frozen hardware SHA. They remain readable
        # for historical display/analysis; source transfer itself fails closed.
        legacy = copy.deepcopy(first)
        legacy["id"] = "wf_" + "e" * 24
        legacy["schema_version"] = "workload_feature_profile/v1"
        legacy.pop("hardware_profile_sha256")
        legacy["dataset_fingerprint"] = self.features._legacy_dataset_fingerprint(dataset["id"])
        legacy.pop("profile_sha256", None)
        encoded = json.dumps(legacy, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":"), allow_nan=False).encode()
        legacy["profile_sha256"] = hashlib.sha256(encoded).hexdigest()
        self.manager._write_json(self.features.state_root / f"{legacy['id']}.json", legacy)
        self.assertEqual(self.features.resolve_for_dataset(legacy["id"], dataset)["id"], legacy["id"])

        # The built-in profile ID is fixed, so content SHA must participate in
        # the cache identity when a platform upgrade changes its definition.
        builtin = self.manager.hardware_profiles.builtin
        builtin["name"] += " · upgraded"
        builtin["profile_sha256"] = hardware_profile_hash(
            self.manager.hardware_profiles._integrity_content(builtin))
        second = self.features.analyze({"dataset_id": dataset["id"]})
        self.assertNotEqual(second["id"], first["id"])
        self.assertNotEqual(second["hardware_profile_sha256"], first["hardware_profile_sha256"])

    def test_wait_dominance_and_running_candidate_are_preserved_independently(self):
        trace = trace_fixture(binder=True)
        for old, new in (("1.003000", "1.002100"), ("1.003100", "1.002200"),
                         ("1.003200", "1.002300"), ("1.003400", "1.003500"),
                         ("1.003500", "1.003600"), ("1.003600", "1.003700"),
                         ("1.003700", "1.003800")):
            trace = trace.replace(old, new)
        dataset = self.manager.import_dataset({"trace_text": trace, "hardware_profile_id": HARDWARE_ID})
        profile = self.features.analyze({"dataset_id": dataset["id"], "measurements": self.measurement(dataset)})
        stage = profile["stages"][0]
        self.assertEqual(stage["classification"]["id"], "service")
        self.assertEqual(stage["classification"]["confidence"]["level"], "high")
        self.assertEqual(stage["classification"]["cpu_candidate"]["id"], "compute")
        self.assertEqual(stage["classification"]["cpu_candidate"]["confidence"]["level"], "medium")
        self.assertIn("CPU 运行占比达到 70%", stage["classification"]["cpu_candidate"]["missing_evidence"])
        self.assertEqual(stage["workload_class"], "compute")
        self.assertEqual(profile["component_classes"], {"build_00": "compute"})

    def test_archived_real_data_preserves_bounds_and_component_order(self):
        dataset = self.manager.import_dataset({"builtin_id": "gallery-edit-pmu-20261014"})
        profile = self.features.analyze({"dataset_id": dataset["id"]})
        self.assertEqual(len(profile["stages"]), 26)
        self.assertGreater(profile["summary"]["pmu_usable_stages"], 0)
        first = profile["stages"][0]
        self.assertEqual(first["compute"]["instruction_bounds"], [85446, 608162])
        self.assertIsNone(first["compute"]["instructions"])
        self.assertIsNone(first["compute"]["ipc"])
        self.assertEqual(first["quality"]["pmu_source"], "trace_aligned_interval_bounds")
        classified = [stage for stage in profile["stages"] if stage["workload_class"] != "unknown"]
        self.assertGreater(len(classified), 0)
        self.assertTrue(all(stage["classification"]["confidence"]["level"] in ("low", "medium") for stage in classified))
        self.assertTrue(all("已核验 LLC 事件语义" in stage["classification"]["missing_evidence"] for stage in classified))
        self.assertTrue(all(stage["workload_class"] == "unknown" for stage in profile["stages"]
                            if not stage["classification"]["available_metrics"]))
        self.assertTrue(all(stage["memory"]["dram_bytes"] is None for stage in profile["stages"]))
        self.assertAlmostEqual(profile["summary"]["stage_wall_ms"] + profile["summary"]["non_builditem_ms"], 86.94)
        self.assertEqual([stage["start_ns"] for stage in profile["stages"]], sorted(stage["start_ns"] for stage in profile["stages"]))

    def test_same_window_exact_counters_enable_explainable_classes(self):
        dataset = self.dataset()
        for instructions, cycles, misses, expected in [(2000, 1000, 2, "compute"), (1000, 2000, 40, "memory"), (1000, 1000, 10, "mixed")]:
            with self.subTest(expected=expected):
                measurements = self.measurement(dataset, instructions=instructions, cycles=cycles, misses=misses)
                profile = self.features.analyze({"dataset_id": dataset["id"], "measurements": measurements})
                stage = profile["stages"][0]
                self.assertEqual(stage["workload_class"], expected)
                self.assertEqual(stage["compute"]["ipc"], instructions / cycles)
                self.assertEqual(stage["memory"]["llc_mpki"], misses / instructions * 1000)
                self.assertEqual(profile["provenance"]["measurements"]["provenance"]["data_kind"], "synthetic_fixture")
                self.assertEqual(profile["component_classes"], {"build_00": expected})
                self.assertTrue(stage["classification"]["evidence"])
                self.assertEqual(stage["classification"]["confidence"]["level"], "high")
                self.assertEqual(stage["classification"]["evidence_quality"], "complete")

    def test_unverified_llc_still_yields_labeled_memory_candidate(self):
        dataset = self.dataset()
        measurements = self.measurement(dataset, instructions=1000, cycles=2000, misses=40)
        measurements["stages"][0]["event_mapping"]["llc_misses"]["verified"] = False
        stage = self.features.analyze({"dataset_id": dataset["id"], "measurements": measurements})["stages"][0]
        self.assertEqual(stage["workload_class"], "memory")
        self.assertIsNotNone(stage["memory"]["llc_mpki"])
        self.assertEqual(stage["classification"]["confidence"]["level"], "medium")
        self.assertEqual(stage["classification"]["evidence_quality"], "partial")
        self.assertIn("已核验 LLC 事件语义", stage["classification"]["missing_evidence"])
        self.assertTrue(any("不等同已核验 LLC" in item for item in stage["classification"]["evidence"]))

    def test_ipc_only_and_single_count_form_non_blocking_fallback_candidates(self):
        dataset = self.dataset()
        ipc_only = self.measurement(dataset, instructions=2000, cycles=1000)
        ipc_only["stages"][0]["counters"].pop("llc_misses")
        ipc_only["stages"][0]["event_mapping"] = {}
        stage = self.features.analyze({"dataset_id": dataset["id"], "measurements": ipc_only})["stages"][0]
        self.assertEqual(stage["workload_class"], "compute")
        self.assertEqual(stage["classification"]["confidence"]["level"], "low")
        self.assertEqual(stage["classification"]["available_metrics"][:1], ["ipc"])
        self.assertIn("Cache / LLC MPKI", stage["classification"]["missing_evidence"])

        count_only = copy.deepcopy(ipc_only)
        count_only["stages"][0]["counters"].pop("cycles")
        stage = self.features.analyze({"dataset_id": dataset["id"], "measurements": count_only})["stages"][0]
        self.assertEqual(stage["workload_class"], "mixed")
        self.assertEqual(stage["classification"]["confidence"], {"level": "low", "score": .2})
        self.assertEqual(stage["classification"]["evidence_quality"], "minimal")

    def test_zero_bare_counts_are_not_activity_but_zero_ratio_remains_evidence(self):
        dataset = self.dataset()
        prototype = self.measurement(dataset)
        stage_measurement = prototype["stages"][0]
        stage_measurement["event_mapping"] = {}
        stage_measurement["counters"] = {
            "instructions": {"value": 0, "unit": "count", "event": "instructions"},
            "cycles": {"value": 0, "unit": "count", "event": "cycles"},
            "cache_misses": {"value": 0, "unit": "count", "event": "cache-misses"},
        }
        stage = self.features.analyze({"dataset_id": dataset["id"], "measurements": prototype})["stages"][0]
        self.assertEqual(stage["workload_class"], "unknown")
        self.assertEqual(stage["classification"]["available_metrics"], [])
        self.assertEqual(stage["classification"]["confidence"], {"level": "none", "score": 0})

        stage_measurement["counters"]["instructions"]["value"] = 1000
        stage = self.features.analyze({"dataset_id": dataset["id"], "measurements": prototype})["stages"][0]
        self.assertEqual(stage["workload_class"], "compute")
        self.assertIn("cache_mpki", stage["classification"]["available_metrics"])
        self.assertEqual(stage["metric_evidence"]["cache_mpki"]["exact"], 0)
        self.assertTrue(any("通用 Cache MPKI 实测值 0.000" in item for item in stage["classification"]["evidence"]))

    def test_zero_to_upper_ipc_envelope_is_not_classification_evidence(self):
        unavailable = {
            "exact": None,
            "estimate": None,
            "interior_estimate": None,
            "bounds": None,
        }
        metrics = {key: dict(unavailable) for key in (*COUNTERS, *RATIOS)}
        metrics["ipc"]["bounds"] = [0, 4]
        result = _classify_cpu(
            {"wall_ms": 1, "cpu_ms": .1},
            {
                "usable": False,
                "llc_mapping_verified": False,
                "ipc_bounds": [0, 4],
                "llc_mpki_bounds": None,
                "metric_evidence": metrics,
                "reasons": ["0–上界仅表示边界不确定。"],
            },
        )
        self.assertEqual(result["id"], "unknown")
        self.assertNotIn("ipc", result["available_metrics"])
        self.assertEqual(result["confidence"], {"level": "none", "score": 0})

    def test_verified_llc_mapping_cannot_relabel_a_different_event(self):
        dataset = self.dataset()
        measurements = self.measurement(dataset)
        measurements["stages"][0]["counters"]["llc_misses"]["event"] = "branch-misses"
        with self.assertRaises(PlatformError):
            self.features.analyze({"dataset_id": dataset["id"], "measurements": measurements})

    def test_invalid_stage_identifier_returns_contract_error(self):
        dataset = self.dataset()
        measurements = self.measurement(dataset)
        measurements["stages"][0]["stage_id"] = []
        with self.assertRaises(PlatformError):
            self.features.analyze({"dataset_id": dataset["id"], "measurements": measurements})

    def test_whole_frame_process_or_wrong_window_cannot_be_spread_to_stage(self):
        dataset = self.dataset()
        original = self.measurement(dataset)
        edits = [lambda m: m["stages"][0].update(start_ns=m["stages"][0]["start_ns"] - 1),
                 lambda m: m["stages"][0].update(end_ns=m["stages"][0]["end_ns"] + 1),
                 lambda m: m["stages"][0].update(tid=999),
                 lambda m: m["stages"][0].update(measurement_scope="whole_process"),
                 lambda m: m.update(clock_alignment_evidence=""),
                 lambda m: m.update(clock_domain="wall_clock"),
                 lambda m: m.update(hardware_profile_id="other"),
                 lambda m: m["stages"][0]["counters"]["cycles"].update(unit="ms"),
                 lambda m: m["stages"][0]["counters"]["cycles"].update(value=float("nan")),
                 lambda m: m["stages"][0]["counters"]["cycles"].update(value=-1)]
        for edit in edits:
            with self.subTest(edit=edit):
                invalid = copy.deepcopy(original)
                edit(invalid)
                with self.assertRaises(PlatformError):
                    self.features.analyze({"dataset_id": dataset["id"], "measurements": invalid})

    def test_names_do_not_determine_classification(self):
        results = []
        for name in ("MemoryBandwidthHeavy", "ComputeFriendly"):
            dataset = self.dataset(name=name)
            profile = self.features.analyze({"dataset_id": dataset["id"], "measurements": self.measurement(dataset)})
            results.append(profile["stages"][0]["classification"])
        self.assertEqual(results[0], results[1])

    def test_profile_versions_reproducible_and_bound_to_dataset_content(self):
        dataset = self.dataset()
        profile = self.features.analyze({"dataset_id": dataset["id"]})
        self.assertEqual(self.features.analyze({"dataset_id": dataset["id"]}), profile)
        self.assertEqual(self.features.resolve_for_dataset(profile["id"], dataset), profile)
        self.assertEqual(self.features.list_profiles()[0]["id"], profile["id"])
        wrong = self.dataset(name="another")
        with self.assertRaises(PlatformError):
            self.features.resolve_for_dataset(profile["id"], wrong)
        path = self.manager.state_root / "inputs" / dataset["id"] / "parsed.json"
        value = json.loads(path.read_text())
        value["untrusted_change"] = True
        path.write_text(json.dumps(value))
        with self.assertRaises(PlatformError):
            self.features.resolve_for_dataset(profile["id"], dataset)

    def test_profile_tampering_and_path_traversal_rejected(self):
        dataset = self.dataset()
        profile = self.features.analyze({"dataset_id": dataset["id"]})
        path = self.features.state_root / (profile["id"] + ".json")
        value = json.loads(path.read_text()); value["component_classes"]["build_00"] = "compute"
        path.write_text(json.dumps(value))
        with self.assertRaises(PlatformError):
            self.features.get_profile(profile["id"])
        with self.assertRaises(PlatformError):
            self.features.get_profile("../../datasets/example")

    def test_extracted_evidence_is_included_in_content_binding(self):
        dataset = self.dataset()
        profile = self.features.analyze({"dataset_id": dataset["id"]})
        path = self.manager.state_root / "inputs" / dataset["id"] / "extracted.json"
        value = json.loads(path.read_text())
        value["components"][0]["cpu_ms"] += .1
        path.write_text(json.dumps(value))
        self.assertNotEqual(profile["dataset_fingerprint"], self.features.dataset_fingerprint(dataset["id"]))
        with self.assertRaises(PlatformError):
            self.features.resolve_for_dataset(profile["id"], dataset)

    def test_identical_evidence_with_different_dataset_identity_gets_distinct_profile(self):
        import shutil
        dataset = self.dataset()
        original = self.features.analyze({"dataset_id": dataset["id"]})
        duplicate = {**dataset, "id": "ds_" + "a" * 24, "name": "separately registered copy"}
        self.manager._write_json(self.manager.state_root / "datasets" / (duplicate["id"] + ".json"), duplicate)
        shutil.copytree(self.manager.state_root / "inputs" / dataset["id"], self.manager.state_root / "inputs" / duplicate["id"])
        copied = self.features.analyze({"dataset_id": duplicate["id"]})
        self.assertEqual(original["dataset_fingerprint"], copied["dataset_fingerprint"])
        self.assertNotEqual(original["id"], copied["id"])
        self.assertEqual(copied["dataset_id"], duplicate["id"])
        self.assertEqual(self.features.resolve_for_dataset(copied["id"], duplicate), copied)

    def test_io_only_overlay_does_not_claim_usable_pmu(self):
        dataset = self.dataset()
        measurements = self.measurement(dataset)
        measurements["stages"][0]["counters"] = {"io_read_bytes": {"value": 4096, "unit": "bytes", "event": "per-thread read completion"}}
        measurements["stages"][0]["event_mapping"] = {}
        stage = self.features.analyze({"dataset_id": dataset["id"], "measurements": measurements})["stages"][0]
        self.assertFalse(stage["quality"]["pmu_usable"])
        self.assertEqual(stage["scheduling"]["io_read_bytes"], 4096)
        self.assertIsNone(stage["compute"]["instructions"])

    def test_feature_profile_drives_pairwise_interference_in_parallel_search(self):
        from backend.tests.test_parallel_interference import pair_measurements
        lines = trace_fixture().splitlines(keepends=True)
        second = [f"app-10 (   10) [000] .... {stamp}: tracing_mark_write: {body}\n" for stamp, body in
                  [("1.004100", "B|10|H:CustomNode:BuildItem [Other]"), ("1.005500", "E|10")]]
        trace = "".join(lines[:-2] + second + lines[-2:])
        dataset = self.manager.import_dataset({"trace_text": trace, "hardware_profile_id": HARDWARE_ID})
        baseline = self.features.analyze({"dataset_id": dataset["id"]})
        self.assertEqual(len(baseline["stages"]), 2)
        plan = self.manager.calibration.create_plan({"parameter_groups": ["pairwise_interference"], "repeats": 3})
        fit = self.manager.calibration.fit({"plan_id": plan["id"], "measurements": pair_measurements(plan)})
        version = self.manager.calibration.publish({"fit_id": fit["id"]})
        outputs = {}
        for kind, instructions, cycles, misses in (("compute", 2000, 1000, 2), ("memory", 1000, 2000, 40)):
            measured = self.measurement(dataset, instructions=instructions, cycles=cycles, misses=misses)
            prototype = measured["stages"][0]
            measured["stages"] = [{**copy.deepcopy(prototype), "stage_id": stage["id"], "start_ns": stage["start_ns"], "end_ns": stage["end_ns"]}
                                  for stage in baseline["stages"]]
            profile = self.features.analyze({"dataset_id": dataset["id"], "measurements": measured})
            analysis = self.manager.analyze({"dataset_id": dataset["id"], "feature_profile_id": profile["id"], "model_package_id": version["id"]})
            result = self.manager.search({"analysis_id": analysis["id"], "model_package_id": version["id"],
                "candidate_ids": [stage["id"] for stage in baseline["stages"]], "robustness": False,
                "space": {"available_core_group_ids": ["big", "middle"], "main_core_group_id": "big",
                          "worker_counts": [2], "main_participation": [False], "frequency_policy": "max_opps"}})
            self.assertEqual(result["feature_profile_id"], profile["id"])
            self.assertTrue(result["best"]["interference"]["matched_cells"])
            self.assertGreater(result["best"]["interference"]["calibrated_cpu_ms"], 0)
            outputs[kind] = result["best"]["metrics"]["finish_ms"]
        self.assertGreater(outputs["memory"], outputs["compute"])

    def test_router_roundtrip(self):
        app = FastAPI(); app.include_router(create_workload_features_router(self.features))
        client = TestClient(app)
        dataset = self.dataset()
        self.assertEqual(client.get("/v1/workload-features/catalog").status_code, 200)
        response = client.post("/v1/workload-features/analyze", json={"dataset_id": dataset["id"]})
        self.assertEqual(response.status_code, 200)
        profile = response.json()
        self.assertEqual(client.get("/v1/workload-features/profiles/" + profile["id"]).json(), profile)
        self.assertEqual(client.get("/v1/workload-features/profiles").json()["profiles"][0]["id"], profile["id"])


if __name__ == "__main__":
    unittest.main()
