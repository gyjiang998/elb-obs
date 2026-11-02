from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from backend.core import ArtifactStore
from backend.parallel_modeling import ParallelModelingManager
from backend.parallel_modeling.manager import HARDWARE_ID
from backend.parallel_modeling import service_dag


def calibrated_profile(max_khz: int, minimum_khz: int, ratio: float) -> dict:
    parameters = {
        "knee_khz": max_khz,
        "above_knee_exponent": 1.0,
        "below_knee_exponent": 1.0,
        "frequency_domain_khz": [minimum_khz, max_khz],
        "cross_core_time_ratio": ratio,
        "transfer_reference": {
            "core_kind": "big",
            "frequency_khz": 2_000,
            "cpu_ms_per_work_unit": 1.0,
        },
    }
    return {
        "max_khz": max_khz,
        "opps_khz": [minimum_khz, max_khz],
        "build_ratio": 99.0,
        "residual_ratio": 1.0,
        "frequency_elasticity": .75,
        "alpha": 0.0,
        "frequency_roofline": {
            "schema_version": "frequency_roofline/v1",
            "classes": {kind: dict(parameters) for kind in ("compute", "memory", "mixed")},
        },
    }


PROFILES = {
    "big": calibrated_profile(2_000, 1_000, 1.0),
    "middle": calibrated_profile(1_500, 750, 2.0),
}
HARDWARE_SHA256 = "a" * 64


def feature_profile(residency: list[dict], *, verified: bool = True,
                    evidence: dict | None = None) -> dict:
    placement = {"source": "trace_sched_switch_cpu_residency"}
    if evidence is not None:
        placement["residency_evidence"] = evidence
    return {
        "id": "wf_test_source_placement",
        "hardware_profile_id": "hardware_test",
        "hardware_profile_sha256": HARDWARE_SHA256,
        "quality": {"hardware_identity_verified": verified},
        "component_classes": {"build_00": "compute"},
        "stages": [{
            "id": "build_00", "workload_class": "compute", "cpu_ms": 12.0,
            "classification": {"id": "compute", "confidence": {"level": "high", "score": .95},
                               "evidence_quality": "complete", "available_metrics": ["ipc", "llc_mpki"],
                               "missing_evidence": []},
            "scheduling": {"cpu_placement": placement, "cpu_residency": residency},
        }],
    }


def graph(transfers: dict) -> dict:
    return {
        "variant": "observed_serial",
        "nodes": [
            {"id": "begin_main_serial", "kind": "begin", "group": "main_serial",
             "duration_ms": 0.0},
            {"id": "cpu", "kind": "cpu", "group": "main_serial", "component": "build_00",
             "state": "on_cpu", "duration_ms": 12.0, "observed_duration_ms": 12.0},
            {"id": "blocked", "kind": "delay", "group": "main_serial", "component": "build_00",
             "state": "blocked", "duration_ms": 5.0, "observed_duration_ms": 5.0},
            {"id": "end_main_serial", "kind": "end", "group": "main_serial",
             "duration_ms": 0.0},
        ],
        "edges": [
            {"source": "begin_main_serial", "target": "cpu", "kind": "within_task_order"},
            {"source": "cpu", "target": "blocked", "kind": "within_task_order"},
            {"source": "blocked", "target": "end_main_serial", "kind": "within_task_order"},
        ],
        "groups": {"main_serial": {"branch": False, "cpu_ms": 12.0, "label": "test"}},
        "terminal": "end_main_serial",
        "component_classes": {"build_00": "compute"},
        "component_source_cost_transfers": transfers,
    }


def simulate(transfers: dict, target_kind: str, target_khz: int) -> dict:
    return service_dag.simulate(
        graph(transfers),
        {"main": {"kind": target_kind, "khz": target_khz}, "workers": [],
         "main_participates": False, "dispatch": "main_first"},
        PROFILES, contention=False, overhead_ms=0,
    )


class SourcePlacementTransferTests(unittest.TestCase):
    def resolve(self, profile: dict):
        return service_dag.resolve_source_cost_transfers(
            profile, PROFILES, "hardware_test", HARDWARE_SHA256, HARDWARE_SHA256)

    def test_middle_non_max_opp_source_is_normalized_before_target_transfer(self):
        transfers, summary = self.resolve(feature_profile([
            {"cpu": 4, "core_group_id": "middle", "frequency_khz": 750, "cpu_ms": 12.0},
        ]))
        transfer = transfers["build_00"]
        # Middle@750 costs 2(core ratio) * 2(frequency) = 4 reference ms.
        # 12 measured ms therefore represents 3 reference ms of work.
        self.assertEqual(transfer["status"], "verified_source_normalization")
        self.assertAlmostEqual(transfer["reference_cpu_ms"], 3.0)
        self.assertAlmostEqual(transfer["reference_work_per_observed_cpu_ms"], .25)
        self.assertEqual(summary["status"], "verified")

        target_big = simulate(transfers, "big", 2_000)
        target_middle = simulate(transfers, "middle", 1_500)
        self.assertAlmostEqual(target_big["app_core_ms"], 3.0)
        self.assertAlmostEqual(target_big["finish_ms"], 8.0)  # 3 ms CPU + unchanged 5 ms off-CPU.
        self.assertAlmostEqual(target_middle["app_core_ms"], 6.0)
        self.assertEqual(target_big["source_placement_cost_transfer"]["status"], "verified")
        self.assertAlmostEqual(
            target_big["time_accounting"]["observed_reference"]["app_off_cpu_ms"], 5.0)

    def test_mixed_source_residency_uses_time_weighted_harmonic_factor(self):
        transfers, _ = self.resolve(feature_profile([
            {"cpu": 4, "core_group_id": "middle", "frequency_khz": 750, "cpu_ms": 8.0},
            {"cpu": 10, "core_group_id": "big", "frequency_khz": 1_000, "cpu_ms": 4.0},
        ]))
        transfer = transfers["build_00"]
        # W_ref = 8/4 + 4/2 = 4 ms, not 12 / arithmetic_mean(4, 2).
        self.assertAlmostEqual(transfer["reference_cpu_ms"], 4.0)
        self.assertAlmostEqual(transfer["reference_work_per_observed_cpu_ms"], 1 / 3)
        self.assertEqual([row["observed_time_weight"] for row in transfer["residency"]],
                         [2 / 3, 1 / 3])
        result = simulate(transfers, "big", 2_000)
        self.assertAlmostEqual(result["app_core_ms"], 4.0)
        self.assertAlmostEqual(result["finish_ms"], 9.0)

    def test_unverified_source_falls_back_without_fabricating_placement(self):
        transfers, summary = self.resolve(feature_profile([
            {"cpu": 4, "core_group_id": "middle", "frequency_khz": 750, "cpu_ms": 12.0},
        ], verified=False))
        transfer = transfers["build_00"]
        self.assertEqual(transfer["status"], "fallback_reference_anchor")
        self.assertEqual(transfer["fallback_reason"], "source_hardware_identity_unverified")
        self.assertEqual(summary["fallback_reasons"], {"source_hardware_identity_unverified": 1})
        result = simulate(transfers, "big", 2_000)
        self.assertAlmostEqual(result["app_core_ms"], 12.0)
        self.assertAlmostEqual(result["finish_ms"], 17.0)
        self.assertEqual(result["source_placement_cost_transfer"]["status"],
                         "fallback_reference_anchor")
        self.assertIn("source_hardware_identity_unverified",
                      result["source_placement_cost_transfer"]["fallback_reasons"])

    def test_new_coverage_contract_is_fail_closed(self):
        evidence = {
            "schema_version": "cpu_residency_evidence/v1", "status": "partial",
            "observed_on_cpu_ms": 10.0, "expected_on_cpu_ms": 12.0,
            "coverage_ratio": 10 / 12, "source_anchor_eligible": False,
        }
        transfers, _ = self.resolve(feature_profile([
            {"cpu": 4, "core_group_id": "middle", "frequency_khz": 750, "cpu_ms": 10.0},
        ], evidence=evidence))
        self.assertEqual(transfers["build_00"]["status"], "fallback_reference_anchor")
        self.assertEqual(transfers["build_00"]["fallback_reason"],
                         "source_residency_evidence_incomplete")

    def test_complete_coverage_contract_accepts_only_declared_95_to_105_percent(self):
        for coverage in (.95, .98, 1.02, 1.05):
            with self.subTest(coverage=coverage):
                observed = 12.0 * coverage
                evidence = {
                    "schema_version": "cpu_residency_evidence/v1", "status": "complete",
                    "observed_on_cpu_ms": observed, "expected_on_cpu_ms": 12.0,
                    "coverage_ratio": coverage, "source_anchor_eligible": True,
                }
                transfers, summary = self.resolve(feature_profile([
                    {"cpu": 4, "core_group_id": "middle", "frequency_khz": 750,
                     "cpu_ms": observed},
                ], evidence=evidence))
                transfer = transfers["build_00"]
                self.assertEqual(transfer["status"], "verified_source_normalization")
                # The complete contract treats the observed residency mix as
                # representative of the full 12 ms stage. Middle@750 has F=4.
                self.assertAlmostEqual(transfer["reference_cpu_ms"], 3.0)
                self.assertAlmostEqual(transfer["reference_work_per_observed_cpu_ms"], .25)
                self.assertEqual(transfer["risk_level"], "medium")
                self.assertIn("source_residency_complete_tolerance_extrapolation",
                              summary["evidence_risk_reasons"])
                self.assertNotEqual(summary["risk_level"], "none")

        coverage = .94
        observed = 12.0 * coverage
        evidence = {
            "schema_version": "cpu_residency_evidence/v1", "status": "complete",
            "observed_on_cpu_ms": observed, "expected_on_cpu_ms": 12.0,
            "coverage_ratio": coverage, "source_anchor_eligible": True,
        }
        transfers, _ = self.resolve(feature_profile([
            {"cpu": 4, "core_group_id": "middle", "frequency_khz": 750,
             "cpu_ms": observed},
        ], evidence=evidence))
        self.assertEqual(transfers["build_00"]["status"], "fallback_reference_anchor")
        self.assertEqual(transfers["build_00"]["fallback_reason"],
                         "source_residency_coverage_invalid")

    def test_unknown_or_out_of_domain_source_evidence_falls_back_with_reason(self):
        cases = (
            ("unknown core group",
             [{"cpu": 99, "core_group_id": None, "frequency_khz": 750, "cpu_ms": 12.0}],
             PROFILES, "source_core_group_unknown"),
            ("unknown frequency",
             [{"cpu": 4, "core_group_id": "middle", "frequency_khz": None, "cpu_ms": 12.0}],
             PROFILES, "source_frequency_unknown"),
            ("outside calibrated domain",
             [{"cpu": 4, "core_group_id": "middle", "frequency_khz": 700, "cpu_ms": 12.0}],
             PROFILES, "source_frequency_outside_calibrated_domain"),
        )
        for name, residency, profiles, reason in cases:
            with self.subTest(name=name):
                transfers, _ = service_dag.resolve_source_cost_transfers(
                    feature_profile(residency), profiles, "hardware_test",
                    HARDWARE_SHA256, HARDWARE_SHA256)
                self.assertEqual(transfers["build_00"]["status"],
                                 "fallback_reference_anchor")
                self.assertEqual(transfers["build_00"]["fallback_reason"], reason)

        profiles = copy.deepcopy(PROFILES)
        profiles["middle"].pop("frequency_roofline")
        transfers, _ = service_dag.resolve_source_cost_transfers(feature_profile([
            {"cpu": 4, "core_group_id": "middle", "frequency_khz": 750, "cpu_ms": 12.0},
        ]), profiles, "hardware_test", HARDWARE_SHA256, HARDWARE_SHA256)
        self.assertEqual(transfers["build_00"]["fallback_reason"], "source_curve_missing")

    def test_source_normalization_requires_same_target_reference_and_class(self):
        transfers, _ = self.resolve(feature_profile([
            {"cpu": 4, "core_group_id": "middle", "frequency_khz": 750, "cpu_ms": 12.0},
        ]))

        mismatched_profiles = copy.deepcopy(PROFILES)
        mismatched_profiles["big"]["frequency_roofline"]["classes"]["compute"][
            "transfer_reference"] = {
                "core_kind": "big", "frequency_khz": 2_000,
                "cpu_ms_per_work_unit": 1.0, "metric": "different_work_unit",
            }
        result = service_dag.simulate(
            graph(transfers),
            {"main": {"kind": "big", "khz": 2_000}, "workers": [],
             "main_participates": False, "dispatch": "main_first"},
            mismatched_profiles, contention=False, overhead_ms=0)
        self.assertAlmostEqual(result["app_core_ms"], 12.0)
        self.assertEqual(result["source_placement_cost_transfer"]["status"],
                         "fallback_reference_anchor")
        self.assertIn("source_target_transfer_reference_mismatch",
                      result["source_placement_cost_transfer"]["fallback_reasons"])
        predicted, detail = service_dag.predict_isolated_component_cpu_ms(
            12.0, transfers["build_00"], mismatched_profiles["big"], 2_000, "compute")
        self.assertAlmostEqual(predicted, 12.0)
        self.assertEqual(detail["source_fallback_reason"],
                         "source_target_transfer_reference_mismatch")

        class_graph = graph(transfers)
        class_graph["component_classes"]["build_00"] = "memory"
        result = service_dag.simulate(
            class_graph,
            {"main": {"kind": "big", "khz": 2_000}, "workers": [],
             "main_participates": False, "dispatch": "main_first"},
            PROFILES, contention=False, overhead_ms=0)
        self.assertAlmostEqual(result["app_core_ms"], 12.0)
        self.assertIn("source_target_workload_class_mismatch",
                      result["source_placement_cost_transfer"]["fallback_reasons"])

    def test_explicit_elasticity_does_not_mix_absolute_source_with_legacy_target(self):
        transfers, _ = self.resolve(feature_profile([
            {"cpu": 4, "core_group_id": "middle", "frequency_khz": 750, "cpu_ms": 12.0},
        ]))
        result = service_dag.simulate(
            graph(transfers),
            {"main": {"kind": "big", "khz": 2_000}, "workers": [],
             "main_participates": False, "dispatch": "main_first"},
            PROFILES, elasticity=.75, contention=False, overhead_ms=0)
        # Explicit sensitivity uses the legacy build ratio (99) from the
        # observed-time anchor; applying the absolute source factor (.25) too
        # would incorrectly mix two reference conventions.
        self.assertAlmostEqual(result["app_core_ms"], 12.0 * 99.0)
        self.assertIn("source_target_absolute_curve_unavailable_explicit_sensitivity",
                      result["source_placement_cost_transfer"]["fallback_reasons"])

    def test_hardware_profile_sha_and_classification_evidence_are_provenance_gates(self):
        profile = feature_profile([
            {"cpu": 4, "core_group_id": "middle", "frequency_khz": 750, "cpu_ms": 12.0},
        ])
        profile["hardware_profile_sha256"] = "b" * 64
        transfers, _ = self.resolve(profile)
        self.assertEqual(transfers["build_00"]["fallback_reason"],
                         "source_hardware_profile_sha256_mismatch")

        profile["hardware_profile_sha256"] = HARDWARE_SHA256
        transfers, _ = service_dag.resolve_source_cost_transfers(
            profile, PROFILES, "hardware_test", "b" * 64, HARDWARE_SHA256)
        self.assertEqual(transfers["build_00"]["fallback_reason"],
                         "model_hardware_profile_sha256_mismatch")

        profile["stages"][0]["classification"].update(
            confidence={"level": "low", "score": .2}, evidence_quality="minimal",
            available_metrics=["ipc"], missing_evidence=["cache_mpki"])
        transfers, summary = self.resolve(profile)
        self.assertEqual(transfers["build_00"]["status"], "verified_source_normalization")
        self.assertEqual(summary["status"], "verified_with_evidence_risk")
        self.assertEqual(summary["risk_level"], "medium")
        self.assertIn("source_workload_class_confidence_below_high",
                      summary["evidence_risk_reasons"])
        self.assertIn("source_workload_class_evidence_incomplete",
                      summary["evidence_risk_reasons"])

        profile["stages"][0]["classification"]["id"] = "memory"
        transfers, _ = self.resolve(profile)
        self.assertEqual(transfers["build_00"]["status"], "fallback_reference_anchor")
        self.assertEqual(transfers["build_00"]["fallback_reason"],
                         "source_workload_class_evidence_inconsistent")

    def test_selected_feature_profile_reaches_analysis_and_search(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            manager = ParallelModelingManager(root, ArtifactStore(root / "artifacts"))
            dataset = manager.import_dataset({"builtin_id": "gallery-edit-pmu-20261014"})
            generated = manager.workload_features.analyze({"dataset_id": dataset["id"]})

            model = copy.deepcopy(manager.model)
            model.update(id="test-source-placement-model", version="test", status="proxy_calibrated")
            model["hardware_profile_sha256"] = manager.hardware_profiles.get(
                HARDWARE_ID)["profile_sha256"]
            reference = {"core_kind": "big", "frequency_khz": 2_500_000,
                         "cpu_ms_per_work_unit": 1.0}
            for kind, profile in model["profiles"].items():
                ratio = {"big": 1.0, "middle": 2.0, "small": 3.0}[kind]
                minimum = min(profile["opps_khz"])
                parameters = {
                    "knee_khz": profile["max_khz"], "above_knee_exponent": 1.0,
                    "below_knee_exponent": 1.0,
                    "frequency_domain_khz": [minimum, profile["max_khz"]],
                    "cross_core_time_ratio": ratio, "transfer_reference": reference,
                }
                profile["frequency_roofline"] = {
                    "schema_version": "frequency_roofline/v1",
                    "classes": {name: copy.deepcopy(parameters)
                                for name in ("compute", "memory", "mixed")},
                }

            def stored_profile(*, verified: bool) -> dict:
                value = copy.deepcopy(generated)
                value.pop("profile_sha256", None)
                value["id"] = "wf_" + ("1" if verified else "2") * 24
                value["quality"]["hardware_identity_verified"] = verified
                value["component_classes"] = {stage["id"]: "unknown" for stage in value["stages"]}
                for item in value["stages"]:
                    item["workload_class"] = "unknown"
                stage = value["stages"][0]
                stage["workload_class"] = "compute"
                value["component_classes"][stage["id"]] = "compute"
                running_classification = stage["classification"].get(
                    "cpu_candidate", stage["classification"])
                running_classification["id"] = "compute"
                source_frequency = min(model["profiles"]["middle"]["opps_khz"])
                stage["scheduling"]["cpu_residency"] = [{
                    "cpu": 4, "core_group_id": "middle", "core_group_name": "中核组",
                    "frequency_khz": source_frequency, "cpu_ms": stage["cpu_ms"],
                }]
                stage["scheduling"]["cpu_placement"] = {
                    "source": "trace_sched_switch_cpu_residency", "primary_cpu": 4,
                    "primary_core_group_id": "middle",
                }
                encoded = json.dumps(value, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":"), allow_nan=False).encode()
                value["profile_sha256"] = hashlib.sha256(encoded).hexdigest()
                manager._write_json(manager.workload_features.state_root / f"{value['id']}.json", value)
                return value

            verified_profile = stored_profile(verified=True)
            fallback_profile = stored_profile(verified=False)
            search_space = {"available_core_group_ids": ["big"], "main_core_group_id": "big",
                            "worker_counts": [0], "frequency_policy": "max_opps"}
            with patch.object(manager, "_resolve_model", return_value=model):
                verified_analysis = manager.analyze({
                    "dataset_id": dataset["id"], "feature_profile_id": verified_profile["id"],
                    "model_package_id": model["id"],
                })
                fallback_analysis = manager.analyze({
                    "dataset_id": dataset["id"], "feature_profile_id": fallback_profile["id"],
                    "model_package_id": model["id"],
                })
                verified_search = manager.search({
                    "analysis_id": verified_analysis["id"], "space": search_space,
                    "model_package_id": model["id"],
                })
                fallback_search = manager.search({
                    "analysis_id": fallback_analysis["id"], "space": search_space,
                    "model_package_id": model["id"],
                })

            first = verified_analysis["components"][0]
            self.assertEqual(first["source_placement_cost_transfer"]["status"],
                             "verified_source_normalization")
            self.assertEqual(
                verified_analysis["analysis_provenance"]["source_placement_cost_transfer"]["status"],
                "mixed_verified_and_fallback")
            self.assertLess(verified_search["best"]["metrics"]["finish_ms"],
                            fallback_search["best"]["metrics"]["finish_ms"])
            self.assertEqual(
                verified_search["search_provenance"]["source_placement_cost_transfer"]["verified_component_count"],
                1)
            self.assertIn(first["id"],
                          verified_search["best"]["source_placement_cost_transfer"]["verified_components"])
            self.assertEqual(
                fallback_search["search_provenance"]["source_placement_cost_transfer"]["fallback_reasons"].get(
                    "source_hardware_identity_unverified"),
                len(fallback_profile["stages"]))


if __name__ == "__main__":
    unittest.main()
