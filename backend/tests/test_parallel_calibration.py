"""Isolated numerical fixtures. No fixture is a real device calibration.

build_measurements is also useful for browser QA against a fresh temporary state
directory. Its provenance explicitly identifies it as a synthetic test fixture.
Production workflows receive measurements only through the import endpoint.
"""
from __future__ import annotations

import copy
import json
import importlib.util
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from backend.core import ArtifactStore, PlatformError
from backend.parallel_modeling import ParallelModelingManager
from backend.parallel_modeling.calibration import (
    DEFAULT_GROUPS, GROUPS, _hash, create_calibration_router,
    pair_work_source_intensity,
)
from backend.parallel_modeling.frequency_roofline import factor as roofline_factor
from backend.parallel_modeling.manager import BUILTIN_ID, HARDWARE_ID, MODEL_ID
from backend.tests.test_parallel_modeling import trace_fixture

FIXTURE_PARAMETERS = {
    "build_ratio": {"big": 1.0, "middle": 1.6, "small": 3.4},
    "residual_ratio": {"big": 1.0, "middle": 1.3, "small": 4.2},
    "frequency_elasticity": {"big": .60, "middle": .80, "small": .45},
    "alpha": {"big": .08, "middle": .12, "small": .05},
    "branch_overhead_ms": .002,
}


def build_measurements(plan, *, parameters=None):
    """Generate clearly labeled numerical QA inputs; use only temporary state."""
    parameters = parameters or FIXTURE_PARAMETERS
    samples = []
    for condition in plan["conditions"]:
        kind = condition["core_kind"]
        maximum = max(plan["frequencies_khz"][kind])
        extra = {}
        if condition["benchmark"] == "empty_dispatch":
            cpu = parameters["branch_overhead_ms"] * condition["work_units"]
        elif condition["benchmark"] == "roofline":
            role = condition["workload_class"]
            operations = condition["roofline_operations_per_element"]
            peak = 12.0 * condition["frequency_khz"] / maximum
            bandwidth = 6.0
            source_flops = condition["work_units"] * operations if role != "instruction_proxy" else None
            source_bytes = condition["work_units"] * 24 if role != "compute_ceiling" else None
            working_set_elements = (0 if role == "compute_ceiling"
                                    else condition["working_set_bytes"] // 24)
            if role == "compute_ceiling":
                wall = source_flops / (peak * 1e6)
            elif role == "bandwidth_ceiling":
                wall = source_bytes / (bandwidth * 1e6)
            elif role == "intensity_ladder":
                intensity = source_flops / source_bytes
                wall = source_flops / (min(peak, bandwidth * intensity) * 1e6)
            else:
                wall = source_bytes / (bandwidth * 1e6)
            cpu = wall * .98
            extra = {"schema_version": "parallel_calibration_measurement/v1",
                     "metric_basis": {"compute_ceiling": "algorithmic_flop",
                                      "bandwidth_ceiling": "minimum_source_bytes",
                                      "intensity_ladder": "algorithmic_flop_per_minimum_source_byte",
                                      "instruction_proxy": "integer_mix_round_proxy"}[role],
                     "algorithmic_flops": source_flops, "algorithmic_bytes_minimum": source_bytes,
                     "integer_mix_rounds": condition["work_units"] * operations if role == "instruction_proxy" else None,
                     "operational_intensity_flop_per_byte": source_flops / source_bytes if source_flops and source_bytes else None,
                     "attainable_gflops": source_flops / (wall * 1e6) if source_flops else None,
                     "source_level_gbytes_per_s": source_bytes / (wall * 1e6) if source_bytes else None,
                     "working_set_coverage_verified": True,
                     "working_set_element_count": working_set_elements,
                     "working_set_bytes_effective": working_set_elements * 24,
                     "full_working_set_passes": (None if role == "compute_ceiling"
                                                 else condition["work_units"] / working_set_elements),
                     "roofline_kernel_version": "classic-roofline-blocked-v2",
                     "roofline_kernel_execution": (
                         "fp64_16_independent_recurrences" if role == "compute_ceiling"
                         else "integer_mix_proxy" if role == "instruction_proxy"
                         else "blocked_fp64_16way_ilp"),
                     "roofline_ilp_lanes": (16 if role == "compute_ceiling"
                                             else 1 if role == "instruction_proxy" else 16),
                     "roofline_generic_even_operations_supported": True,
                     "roofline_quality_gate_required": True,
                     "target_cpu_busy_fraction": .99}
        elif (condition["benchmark"] == "pair_work"
              and str(condition.get("workload_id", "")).endswith("_frequency_proxy_v1")):
            train_frequencies = plan["frequency_roofline_split"][kind]["train_khz"]
            curve = {
                "compute": (maximum, 1.0, 1.0),
                "memory": (train_frequencies[1], .05, .8),
                "mixed": (train_frequencies[-2], .25, .9),
            }[condition["workload_class"]]
            cpu = (.00002 * condition["work_units"]
                   * roofline_factor(maximum, condition["frequency_khz"], *curve))
        elif condition["benchmark"] == "pair_work":
            classes = {"compute": 0, "memory": 1, "mixed": 2}
            beta = (0 if condition["active_tasks"] == 1 else
                    .10 + .05 * classes[condition["workload_class"]]
                    + .03 * classes[condition["peer_workload_class"]])
            cpu = .0001 * condition["work_units"] * (1 + beta)
        else:
            density = .000005 if condition["family"] == "build" else .000004
            ratio = parameters[condition["family"] + "_ratio"][kind]
            elasticity = parameters["frequency_elasticity"][kind]
            frequency = 1 - elasticity + elasticity * maximum / condition["frequency_khz"]
            inflation = 1 + parameters["alpha"][kind] * (condition["active_tasks"] - 1) if condition["family"] == "build" else 1
            cpu = density * condition["work_units"] * ratio * frequency * inflation
        for repeat in range(condition["repeats"]):
            # Symmetric independent-repeat variation keeps fitted medians exact.
            noise = 1 + .002 * (repeat - (condition["repeats"] - 1) / 2)
            identity = f"TEST_ONLY_{condition['id']}_{repeat}"
            target_cpu = plan["cpu_map"][kind][0]
            warmup_chunk = (32 if condition["benchmark"] == "empty_dispatch"
                            else min(condition["work_units"], 32768))
            sample = {**{key: value for key, value in condition.items() if key not in ("id", "repeats")},
                "sample_id": identity, "run_id": identity, "repeat": repeat,
                "workload_version": "parallel-calibration-proxy-v6",
                "cpu": target_cpu,
                "cpu_ms": cpu * noise, "wall_ms": cpu * noise * 1.05,
                "observed_frequency_khz": condition["frequency_khz"],
                "frequency_mean_khz": condition["frequency_khz"],
                "frequency_min_khz": condition["frequency_khz"],
                "frequency_max_khz": condition["frequency_khz"],
                "frequency_read_failures": 0,
                "frequency_source": "cpuinfo_cur_freq",
                "frequency_source_cpu": target_cpu,
                "frequency_source_path":
                    f"/sys/devices/system/cpu/cpu{target_cpu}/cpufreq/cpuinfo_cur_freq",
                "warmup_frequency_sample_count": 3,
                "warmup_frequency_in_range_consecutive": 3,
                "warmup_frequency_required_consecutive": 3,
                "warmup_frequency_max_samples": 12,
                "warmup_frequency_min_khz": condition["frequency_khz"],
                "warmup_frequency_max_khz": condition["frequency_khz"],
                "warmup_frequency_last_khz": condition["frequency_khz"],
                "warmup_frequency_verified": True,
                "warmup_work_units": 3 * warmup_chunk,
                "observed_frequency_min_khz": condition["frequency_khz"],
                "observed_frequency_max_khz": condition["frequency_khz"],
                "frequency_sample_count": 20, "affinity_verified": True, "frequency_verified": True,
                "thermal_throttled": False, "binary_sha256": "a" * 64,
                "thermal_snapshot_before": [{"zone": "thermal_zone0", "type": "cpu",
                                               "temperature_raw": 41000}],
                "thermal_snapshot_after": [{"zone": "thermal_zone0", "type": "cpu",
                                              "temperature_raw": 41100}],
                "physical_core_placement_verified": True, "concurrency_verified": True, "measurement_scope": "target_thread_cpu",
                "target_sibling_cpus": [], "target_sibling_activity": [],
                "target_sibling_activity_verified": True,
                "target_sibling_busy_fraction": 0,
                "target_sibling_busy_threshold": .05,
                "target_sibling_quietness_verified": True,
                **extra}
            if condition["benchmark"] == "roofline":
                sample["wall_ms"] = wall * noise
                sample["cpu_ms"] = cpu * noise
                if source_flops:
                    sample["attainable_gflops"] = source_flops / (sample["wall_ms"] * 1e6)
                if source_bytes:
                    sample["source_level_gbytes_per_s"] = source_bytes / (sample["wall_ms"] * 1e6)
            if condition["benchmark"] in ("pair_work", "roofline"):
                all_cpus = [cpu for cpus in plan["cpu_map"].values() for cpu in cpus]
                observer = all_cpus[-1]
                target = next(cpu for cpu in plan["cpu_map"][kind] if cpu != observer)
                peer_cpu = None
                if condition["benchmark"] == "pair_work" and condition["active_tasks"] == 2:
                    peer_cpu = next(cpu for cpu in plan["cpu_map"][condition["peer_core_kind"]]
                                    if cpu not in (target, observer))
                sample.update(cpu=target, observer_cpu=observer,
                              observer_independent_core=True,
                              frequency_source_cpu=target,
                              frequency_source_path=
                                  f"/sys/devices/system/cpu/cpu{target}/cpufreq/cpuinfo_cur_freq")
                if condition["benchmark"] == "pair_work":
                    sample["helpers"] = ([] if peer_cpu is None else [{
                        "cpu": peer_cpu, "actual_cpu": peer_cpu, "affinity_verified": True,
                    }])
                    if peer_cpu is not None:
                        sample.update(peer_frequency_verified=True,
                                      peer_frequency_sample_count=20,
                                      peer_frequency_read_failures=0,
                                      peer_frequency_source="cpuinfo_cur_freq",
                                      peer_frequency_source_cpu=peer_cpu,
                                      peer_frequency_source_path=
                                          f"/sys/devices/system/cpu/cpu{peer_cpu}/cpufreq/cpuinfo_cur_freq",
                                      peer_observed_frequency_khz=condition["peer_frequency_khz"],
                                      peer_frequency_min_khz=condition["peer_frequency_khz"],
                                      peer_frequency_max_khz=condition["peer_frequency_khz"])
            samples.append(sample)
    measurements = {"schema_version": "parallel_calibration_measurements/v1", "plan_id": plan["id"],
        "hardware_profile_id": plan["hardware_profile_id"],
        "provenance": {"source_kind": "measured", "device_id": "ISOLATED_QA_SYNTHETIC_DEVICE",
                       "os_build": "TEST_FIXTURE_NOT_REAL", "collector": "isolated-unit-test-harness",
                       "measured_at": "2026-10-16T00:00:00Z", "cpu_map": plan["cpu_map"]},
        "samples": samples}
    return paired_dispatch_v4(
        measurements, wall_delta=parameters["branch_overhead_ms"])


def paired_dispatch_v4(measurements, *, wall_delta=.0017, worker_delta=.0006,
                       producer_delta=.0004):
    """Upgrade only dispatch rows to the v4 paired same-payload fixture."""
    measurements = copy.deepcopy(measurements)
    for sample in measurements["samples"]:
        if sample["benchmark"] != "empty_dispatch":
            continue
        count = sample["work_units"]
        direct_worker_cpu = .0020 * count
        direct_worker_wall = .0027 * count
        direct_producer_cpu = .0002 * count
        direct_roundtrip_wall = .0030 * count
        queue_worker_cpu = direct_worker_cpu + worker_delta * count
        queue_worker_wall = direct_worker_wall + .0004 * count
        queue_producer_cpu = direct_producer_cpu + producer_delta * count
        queue_roundtrip_wall = direct_roundtrip_wall + wall_delta * count
        sample.update(
            schema_version="parallel_calibration_measurement/v1",
            workload_version="parallel-calibration-proxy-v6",
            dispatch_measurement_schema="paired_dispatch_delta/v1",
            dispatch_payload="mix64_v1",
            dispatch_payload_match=True,
            cold_start_included=False,
            task_count=count,
            direct_batch_worker_cpu_ms=direct_worker_cpu,
            direct_batch_worker_wall_ms=direct_worker_wall,
            direct_batch_producer_cpu_ms=direct_producer_cpu,
            direct_batch_roundtrip_wall_ms=direct_roundtrip_wall,
            queue_dispatch_worker_cpu_ms=queue_worker_cpu,
            queue_dispatch_worker_wall_ms=queue_worker_wall,
            queue_dispatch_producer_cpu_ms=queue_producer_cpu,
            queue_dispatch_roundtrip_wall_ms=queue_roundtrip_wall,
            dispatch_wall_delta_ms_per_task=wall_delta,
            dispatch_worker_cpu_delta_ms_per_task=worker_delta,
            dispatch_producer_cpu_delta_ms_per_task=producer_delta,
            cpu_ms=queue_worker_cpu,
            wall_ms=queue_worker_wall,
            measurement_scope="paired direct-batch versus per-item queue dispatch",
        )
    return measurements


def with_optional_pmu(sample, collected=("cycles", "instructions",
                                         "generic_cache_references",
                                         "generic_cache_misses")):
    """Attach exact same-window PMU evidence, including tiny scaled counts."""
    raw_values = {
        "cycles": 3, "instructions": 6,
        "generic_cache_references": 2, "generic_cache_misses": 1,
    }
    counters = []
    observed = {}
    for name in ("cycles", "instructions", "generic_cache_references",
                 "generic_cache_misses"):
        if name in collected:
            scaled = raw_values[name] * 10 / 6
            observed[name] = scaled
            counters.append({
                "event": name, "status": "collected", "raw_value": raw_values[name],
                "scaled_value": scaled, "enabled_ns": 10, "running_ns": 6,
                "error": None,
            })
        else:
            observed[name] = None
            counters.append({
                "event": name, "status": "unavailable", "raw_value": None,
                "scaled_value": None, "enabled_ns": None, "running_ns": None,
                "error": 13,
            })
    available = len(collected)
    sample.update({
        "pmu_collection_status": ("collected" if available == 4 else
                                  "partial" if available else "unavailable"),
        "pmu_scope": "target_thread_user_only_timed_window",
        "pmu_window_aligned": bool(available),
        "pmu_window_relation": (
            "perf_group_encloses_timed_workload_window; timed wall/cpu exclude "
            "perf control/read overhead"),
        "pmu_counting_semantics": (
            "perf_event_open per-thread user-only group; one PERF_IOC_FLAG_GROUP "
            "reset/enable/disable window; each count scaled by time_enabled/time_running"),
        "pmu_cycles": observed["cycles"],
        "pmu_retired_instructions": observed["instructions"],
        "pmu_cache_references": observed["generic_cache_references"],
        "pmu_cache_misses": observed["generic_cache_misses"],
        "pmu_ipc": (observed["instructions"] / observed["cycles"]
                    if observed["instructions"] is not None and observed["cycles"] else None),
        "pmu_cache_mpki": (observed["generic_cache_misses"] * 1000 / observed["instructions"]
                           if observed["generic_cache_misses"] is not None
                           and observed["instructions"] else None),
        "pmu_counters": counters,
    })
    return sample


class ParallelCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.manager = ParallelModelingManager(self.root, ArtifactStore(self.root / "artifacts"))
        self.calibration = self.manager.calibration

    def tearDown(self): self.temporary.cleanup()

    def plan(self, **options):
        return self.calibration.create_plan({"name": "ISOLATED NUMERICAL TEST", "hardware_profile_id": HARDWARE_ID,
                                             "repeats": 3, "parameter_groups": list(GROUPS), **options})

    def fit(self, plan=None, data=None):
        plan = plan or self.plan()
        return self.calibration.fit({"plan_id": plan["id"], "measurements": data or build_measurements(plan)})

    def portable_profile_package(self, name, *, supersedes=None):
        payload = self.manager._builtin_hardware_profile()
        payload.update(
            name=name,
            source={
                "kind": "local_hdc",
                "transport": "usb",
                "collector": {"name": "test-hardware-extractor", "version": "1.0.0"},
            },
            evidence={"device_serial_sha256": ("a" if "OLD" in name else "b") * 64},
        )
        hardware = self.manager.create_hardware_profile(payload)
        plan = self.calibration.create_plan({
            "name": name,
            "hardware_profile_id": hardware["id"],
            "repeats": 3,
            "parameter_groups": ["dispatch_overhead"],
        })
        fit = self.fit(plan, build_measurements(plan))
        self.assertEqual(fit["status"], "passed", fit["quality"])
        version = self.calibration.publish({"fit_id": fit["id"], "name": name})
        package = self.calibration.export_portable_package(version["id"], supersedes=supersedes)
        return hardware, version, package

    def test_fit_recovers_real_equation_and_published_version_is_selectable(self):
        plan = self.plan()
        fit = self.fit(plan)
        self.assertEqual(fit["status"], "passed", fit["quality"])
        self.assertTrue(fit["quality"]["passed"])
        self.assertLess(fit["quality"]["validation_p90_ape"], .005)
        model = fit["model_preview"]
        for kind in ("big", "middle", "small"):
            for field in ("build_ratio", "residual_ratio", "frequency_elasticity", "alpha"):
                self.assertAlmostEqual(model["profiles"][kind][field], FIXTURE_PARAMETERS[field][kind], places=10)
        self.assertAlmostEqual(model["simulation_parameters"]["branch_overhead_ms"], .002)
        self.assertEqual(fit["inherited_parameters"], [])
        version = self.calibration.publish({"fit_id": fit["id"], "name": "TEST ONLY"})
        self.assertEqual(version["status"], "proxy_calibrated")
        self.assertEqual(version["hardware_profile_sha256"], plan["hardware_profile_sha256"])
        self.assertEqual(version["model"]["hardware_profile_sha256"],
                         plan["hardware_profile_sha256"])
        self.assertEqual(version["validation"]["application_validation"], "not_application_validated")
        self.assertIn(version["id"], [item["id"] for item in self.manager.catalog()["model_packages"]])
        self.assertEqual(self.calibration.publish({"fit_id": fit["id"], "name": "TEST ONLY"}), version)
        reopened = ParallelModelingManager(self.root)
        self.assertEqual(reopened.calibration.resolve_model(version["id"])["profiles"], model["profiles"])

    def test_default_model_prefers_gate_passed_version_before_newer_risk_version(self):
        dataset = self.manager.import_dataset({"builtin_id": BUILTIN_ID})
        versions = [
            {"id": "newer-risk", "hardware_profile_id": HARDWARE_ID,
             "status": "risk_calibrated"},
            {"id": "older-passed", "hardware_profile_id": HARDWARE_ID,
             "status": "partially_calibrated"},
        ]
        original = self.calibration.list_versions
        try:
            self.calibration.list_versions = lambda **_kwargs: versions
            self.assertEqual(self.manager._default_model_id(dataset), "older-passed")
            self.calibration.list_versions = lambda **_kwargs: versions[:1]
            self.assertEqual(self.manager._default_model_id(dataset), "newer-risk")
        finally:
            self.calibration.list_versions = original

    def test_legacy_passed_fit_without_publishable_field_remains_publishable(self):
        fit = self.fit()
        path = self.calibration.root / "fits" / (fit["id"] + ".json")
        legacy = json.loads(path.read_text(encoding="utf-8"))
        legacy["quality"].pop("publishable")
        path.write_text(json.dumps(legacy), encoding="utf-8")
        version = self.calibration.publish({"fit_id": fit["id"], "name": "LEGACY PASSED FIT"})
        self.assertEqual(version["status"], "proxy_calibrated")

    def test_portable_package_installs_version_fit_plan_and_hardware_for_desktop(self):
        plan = self.plan()
        fit = self.fit(plan)
        version = self.calibration.publish({"fit_id": fit["id"], "name": "OFFLINE DEVICE CURVES"})
        package = self.calibration.export_portable_package(version["id"])
        self.assertEqual(package["schema_version"], "parallel_calibration_portable_package/v1")
        self.assertRegex(package["package_id"], r"^pcp_[0-9a-f]{24}$")
        self.assertEqual(package["fit"]["measurements"]["samples"],
                         self.calibration.get_fit(fit["id"])["measurements"]["samples"])

        package_root = self.root / "portable-packages"
        package_root.mkdir()
        (package_root / "offline-device.json").write_text(
            json.dumps(package, ensure_ascii=False), encoding="utf-8")
        offline_root = self.root / "offline-state"
        offline = ParallelModelingManager(
            offline_root, portable_calibration_package_root=package_root)
        self.assertEqual([item["id"] for item in offline.calibration.list_versions()], [version["id"]])
        self.assertEqual(offline.calibration.get_fit(fit["id"])["measurements_sha256"],
                         fit["measurements_sha256"])
        self.assertEqual(offline.calibration.get_plan(plan["id"])["hardware_profile_id"], HARDWARE_ID)
        self.assertEqual(offline.calibration.resolve_model(version["id"])["id"], version["id"])
        self.assertIn(version["id"], [item["id"] for item in offline.catalog()["model_packages"]])
        dataset = offline.import_dataset({"builtin_id": BUILTIN_ID})
        analysis = offline.analyze({"dataset_id": dataset["id"]})
        self.assertEqual(
            analysis["cost_model"]["id"], version["id"],
            "omitting model_package_id must prefer the installed compatible calibration",
        )
        # A new persistent worker can install the same package again without
        # replacing the already materialized evidence records.
        restarted = ParallelModelingManager(
            offline_root, portable_calibration_package_root=package_root)
        self.assertEqual(restarted.calibration.get_version(version["id"])["model_sha256"],
                         version["model_sha256"])

        legacy_package = copy.deepcopy(package)
        legacy_package["fit"]["quality"].pop("publishable")
        legacy_digest = _hash(self.calibration._portable_payload(legacy_package))
        legacy_package.update(package_id="pcp_" + legacy_digest[:24],
                              package_sha256=legacy_digest)
        legacy_root = self.root / "legacy-packages"
        legacy_root.mkdir()
        (legacy_root / "legacy-passed.json").write_text(
            json.dumps(legacy_package, ensure_ascii=False), encoding="utf-8")
        legacy_state = ParallelModelingManager(
            self.root / "legacy-state", portable_calibration_package_root=legacy_root)
        self.assertEqual(legacy_state.calibration.resolve_model(version["id"])["id"],
                         version["id"])

        corrupt = copy.deepcopy(package)
        corrupt["calibration_version"]["name"] = "tampered"
        (package_root / "corrupt.json").write_text(json.dumps(corrupt), encoding="utf-8")
        with self.assertRaises(PlatformError) as caught:
            ParallelModelingManager(
                self.root / "corrupt-state", portable_calibration_package_root=package_root)
        self.assertEqual(caught.exception.code, "parallel_calibration_package_corrupt")

    def test_portable_package_preserves_extended_hardware_source_metadata(self):
        payload = self.manager._builtin_hardware_profile()
        payload.update(
            name="EXTENDED SOURCE METADATA DEVICE",
            source={
                "kind": "local_hdc",
                "transport": "usb",
                "collector": {"name": "desktop-hardware-extractor", "version": "1.2.0"},
            },
            evidence={"device_serial_sha256": "b" * 64, "extracted_at": "2026-10-30T00:00:00Z"},
        )
        hardware = self.manager.create_hardware_profile(payload)
        self.assertEqual(hardware["source_metadata"]["transport"], "usb")
        plan = self.calibration.create_plan({
            "name": "EXTENDED SOURCE PORTABLE TEST",
            "hardware_profile_id": hardware["id"],
            "repeats": 3,
            "parameter_groups": ["dispatch_overhead"],
        })
        fit = self.fit(plan, build_measurements(plan))
        self.assertEqual(fit["status"], "passed", fit["quality"])
        version = self.calibration.publish({"fit_id": fit["id"]})
        package = self.calibration.export_portable_package(version["id"])

        package_root = self.root / "extended-source-packages"
        package_root.mkdir()
        (package_root / "extended-source.json").write_text(
            json.dumps(package, ensure_ascii=False), encoding="utf-8")
        installed = ParallelModelingManager(
            self.root / "extended-source-state",
            portable_calibration_package_root=package_root,
        )
        restored = installed.hardware_profiles.get(hardware["id"])
        self.assertEqual(restored["id"], hardware["id"])
        self.assertEqual(restored["profile_sha256"], hardware["profile_sha256"])
        self.assertEqual(restored["source"], "local_hdc")
        self.assertEqual(restored["source_metadata"], hardware["source_metadata"])
        self.assertEqual(restored["source_metadata"]["collector"]["version"], "1.2.0")
        self.assertEqual(installed.calibration.get_version(version["id"])["hardware_profile_id"],
                         hardware["id"])
        displayed = next(item for item in installed.list_hardware_profiles()
                         if item["id"] == hardware["id"])
        self.assertEqual(displayed["source"], "local_hdc",
                         "display provenance must not rewrite hashed hardware evidence")
        self.assertEqual(displayed["display_source"], "bundled_offline_calibration")
        self.assertEqual(displayed["display_source_label"], "平台离线实测")
        self.assertEqual(displayed["compatible_model_package_id"], version["id"])
        displayed_version = installed.calibration.list_versions()[0]
        self.assertEqual(displayed_version["display_source_label"], "随包校准")
        self.assertEqual(displayed_version["display_provenance"]["package_id"], package["package_id"])
        model_entry = next(item for item in installed.catalog()["model_packages"]
                           if item["id"] == version["id"])
        self.assertEqual(model_entry["display_source"], "bundled_offline_calibration")
        source_dataset = installed.import_dataset({"builtin_id": BUILTIN_ID})
        bound_dataset = installed.bind_dataset_hardware(
            source_dataset["id"], {"hardware_profile_id": hardware["id"]},
        )
        default_analysis = installed.analyze({"dataset_id": bound_dataset["id"]})
        self.assertEqual(
            default_analysis["cost_model"]["id"], version["id"],
            "a custom portable hardware profile must not default back to its topology proxy",
        )
        calibration_hardware = next(item for item in installed.calibration.catalog()["hardware_profiles"]
                                    if item["id"] == hardware["id"])
        self.assertEqual(calibration_hardware["display_source_label"], "平台离线实测")

        local = self.manager.get_hardware_profile(hardware["id"])
        self.assertNotIn("display_source", local)
        self.assertEqual(local["source"], "local_hdc")

    def test_successor_package_retires_installed_package_profile_and_version(self):
        old_hardware, old_version, old_package = self.portable_profile_package("OLD INVALID TOPOLOGY")
        target = {
            "package_id": old_package["package_id"],
            "hardware_profile_id": old_hardware["id"],
            "calibration_version_id": old_version["id"],
            "reason": "The predecessor used an invalid physical-core topology and must not remain selectable.",
        }
        new_hardware, new_version, new_package = self.portable_profile_package(
            "NEW VERIFIED TOPOLOGY", supersedes=[target])
        self.assertEqual(new_package["supersedes"], [target])

        package_root = self.root / "upgrade-packages"
        package_root.mkdir()
        old_path = package_root / "retired.json"
        old_path.write_text(json.dumps(old_package, ensure_ascii=False), encoding="utf-8")
        state_root = self.root / "upgrade-state"
        before = ParallelModelingManager(state_root, portable_calibration_package_root=package_root)
        self.assertEqual(before.calibration.list_versions()[0]["id"], old_version["id"])
        self.assertEqual(before.hardware_profiles.get(old_hardware["id"])["status"], "active")

        old_path.unlink()
        (package_root / "successor.json").write_text(
            json.dumps(new_package, ensure_ascii=False), encoding="utf-8")
        upgraded = ParallelModelingManager(state_root, portable_calibration_package_root=package_root)
        self.assertEqual([item["id"] for item in upgraded.calibration.list_versions()],
                         [new_version["id"]])
        retired = upgraded.calibration.get_version(old_version["id"])
        self.assertEqual(retired["status"], "superseded")
        self.assertEqual(retired["supersession"]["by_package_id"], new_package["package_id"])
        self.assertEqual(retired["supersession"]["by_calibration_version_id"], new_version["id"])
        self.assertEqual(upgraded.hardware_profiles.get(old_hardware["id"])["status"], "archived")
        self.assertEqual(upgraded.hardware_profiles.get(new_hardware["id"])["status"], "active")
        self.assertNotIn(old_package["plan"]["id"],
                         [item["id"] for item in upgraded.calibration.list_plans()])
        self.assertNotIn(old_package["fit"]["id"],
                         [item["id"] for item in upgraded.calibration.list_fits()])
        self.assertEqual(upgraded.calibration.get_plan(old_package["plan"]["id"])["id"],
                         old_package["plan"]["id"])
        self.assertEqual(upgraded.calibration.get_fit(old_package["fit"]["id"])["id"],
                         old_package["fit"]["id"])
        self.assertNotIn(old_hardware["id"],
                         [item["id"] for item in upgraded.list_hardware_profiles(include_archived=False)])
        self.assertNotIn(old_version["id"],
                         [item["id"] for item in upgraded.catalog()["model_packages"]])
        self.assertIn(new_version["id"],
                      [item["id"] for item in upgraded.catalog()["model_packages"]])
        source_dataset = upgraded.import_dataset({"builtin_id": BUILTIN_ID})
        bound_dataset = upgraded.bind_dataset_hardware(
            source_dataset["id"], {"hardware_profile_id": new_hardware["id"]})
        default_analysis = upgraded.analyze({"dataset_id": bound_dataset["id"]})
        self.assertEqual(default_analysis["cost_model"]["id"], new_version["id"])
        with self.assertRaises(PlatformError) as caught:
            upgraded.calibration.resolve_model(old_version["id"])
        self.assertEqual(caught.exception.code, "parallel_calibration_model_superseded")
        with self.assertRaises(PlatformError) as caught:
            upgraded.restore_hardware_profile(old_hardware["id"])
        self.assertEqual(caught.exception.code, "parallel_hardware_profile_superseded")
        with self.assertRaises(PlatformError) as caught:
            upgraded.calibration.publish({
                "fit_id": old_package["fit"]["id"],
                "name": "MUST NOT REPUBLISH RETIRED EVIDENCE",
            })
        self.assertEqual(caught.exception.code, "parallel_calibration_hardware_superseded")

        receipts = upgraded.calibration.list_supersessions()
        self.assertEqual(len(receipts), 1)
        self.assertEqual(receipts[0]["schema_version"], "parallel_calibration_supersession/v1")
        self.assertEqual(receipts[0]["target"], target)
        self.assertEqual(receipts[0]["result"], {
            "calibration_version": "superseded",
            "hardware_profile": "archived",
        })

        restarted = ParallelModelingManager(state_root, portable_calibration_package_root=package_root)
        self.assertEqual([item["id"] for item in restarted.calibration.list_versions()],
                         [new_version["id"]])
        self.assertEqual(len(restarted.calibration.list_supersessions()), 1)
        self.assertEqual(restarted.calibration.list_supersessions()[0]["applied_at"],
                         receipts[0]["applied_at"])

    def test_directory_install_applies_supersession_after_all_files_regardless_of_name_order(self):
        old_hardware, old_version, old_package = self.portable_profile_package("OLD ORDER TEST")
        target = {
            "package_id": old_package["package_id"],
            "hardware_profile_id": old_hardware["id"],
            "calibration_version_id": old_version["id"],
            "reason": "Replacement package must win independently of filename order.",
        }
        _, new_version, new_package = self.portable_profile_package(
            "NEW ORDER TEST", supersedes=[target])
        package_root = self.root / "order-packages"
        package_root.mkdir()
        (package_root / "00-successor.json").write_text(
            json.dumps(new_package, ensure_ascii=False), encoding="utf-8")
        (package_root / "99-retired.json").write_text(
            json.dumps(old_package, ensure_ascii=False), encoding="utf-8")

        installed = ParallelModelingManager(
            self.root / "order-state", portable_calibration_package_root=package_root)
        self.assertEqual([item["id"] for item in installed.calibration.list_versions()],
                         [new_version["id"]])
        self.assertEqual(installed.calibration.get_version(old_version["id"])["status"],
                         "superseded")

    def test_successor_package_cannot_retire_matching_local_evidence(self):
        old_hardware, old_version, old_package = self.portable_profile_package("OLD LOCAL EVIDENCE")
        target = {
            "package_id": old_package["package_id"],
            "hardware_profile_id": old_hardware["id"],
            "calibration_version_id": old_version["id"],
            "reason": "This target deliberately collides with a local record.",
        }
        _, _, new_package = self.portable_profile_package(
            "NEW LOCAL EVIDENCE", supersedes=[target])
        package_root = self.root / "local-evidence-packages"
        package_root.mkdir()
        (package_root / "successor.json").write_text(
            json.dumps(new_package, ensure_ascii=False), encoding="utf-8")

        with self.assertRaises(PlatformError) as caught:
            ParallelModelingManager(self.root, portable_calibration_package_root=package_root)
        self.assertEqual(caught.exception.code, "parallel_calibration_package_conflict")
        self.assertEqual(self.calibration.get_version(old_version["id"])["status"],
                         old_version["status"])
        self.assertEqual(self.manager.hardware_profiles.get(old_hardware["id"])["status"], "active")

    def test_successor_package_preserves_unproven_matching_hardware_profile(self):
        old_hardware, old_version, old_package = self.portable_profile_package(
            "OLD PROFILE WITHOUT INSTALLED VERSION")
        target = {
            "package_id": old_package["package_id"],
            "hardware_profile_id": old_hardware["id"],
            "calibration_version_id": old_version["id"],
            "reason": "A declaration alone must not archive matching local hardware evidence.",
        }
        _, _, new_package = self.portable_profile_package(
            "NEW PROFILE WITH UNPROVEN TARGET", supersedes=[target])

        state_root = self.root / "unproven-hardware-state"
        local = ParallelModelingManager(state_root)
        local.hardware_profiles.install_portable(old_package["hardware_profile"])
        self.assertEqual(local.hardware_profiles.get(old_hardware["id"])["status"], "active")

        installed = local.calibration.install_portable_package(new_package)
        self.assertEqual(local.hardware_profiles.get(old_hardware["id"])["status"], "active")
        self.assertFalse(local.calibration.is_hardware_profile_superseded(old_hardware["id"]))
        self.assertEqual(installed["supersession_receipts"][0]["result"], {
            "calibration_version": "not_installed",
            "hardware_profile": "preserved_unverified_origin",
        })

    def test_successor_only_fresh_install_and_restart_are_idempotent(self):
        old_hardware, old_version, old_package = self.portable_profile_package(
            "OLD PACKAGE ABSENT FROM FRESH STATE")
        target = {
            "package_id": old_package["package_id"],
            "hardware_profile_id": old_hardware["id"],
            "calibration_version_id": old_version["id"],
            "reason": "Fresh installs must not require the retired package to remain bundled.",
        }
        new_hardware, new_version, new_package = self.portable_profile_package(
            "NEW FRESH INSTALL PACKAGE", supersedes=[target])
        package_root = self.root / "successor-only-packages"
        package_root.mkdir()
        (package_root / "successor.json").write_text(
            json.dumps(new_package, ensure_ascii=False), encoding="utf-8")
        state_root = self.root / "successor-only-state"

        first = ParallelModelingManager(
            state_root, portable_calibration_package_root=package_root)
        self.assertEqual([row["id"] for row in first.calibration.list_versions()],
                         [new_version["id"]])
        self.assertEqual(first.hardware_profiles.get(new_hardware["id"])["status"], "active")
        self.assertFalse((first.calibration.root / "versions" /
                          f"{old_version['id']}.json").exists())
        receipt = first.calibration.list_supersessions()[0]
        self.assertEqual(receipt["result"], {
            "calibration_version": "not_installed",
            "hardware_profile": "not_installed",
        })

        restarted = ParallelModelingManager(
            state_root, portable_calibration_package_root=package_root)
        self.assertEqual([row["id"] for row in restarted.calibration.list_versions()],
                         [new_version["id"]])
        self.assertEqual(len(restarted.calibration.list_supersessions()), 1)
        self.assertEqual(restarted.calibration.list_supersessions()[0]["applied_at"],
                         receipt["applied_at"])

        # A later bundle downgrade must not resurrect the retired package when
        # the successor JSON is temporarily absent from the scanned directory.
        (package_root / "successor.json").unlink()
        (package_root / "retired-reintroduced.json").write_text(
            json.dumps(old_package, ensure_ascii=False), encoding="utf-8")
        downgraded_bundle = ParallelModelingManager(
            state_root, portable_calibration_package_root=package_root)
        self.assertEqual(downgraded_bundle.installed_calibration_packages, [])
        self.assertEqual([row["id"] for row in downgraded_bundle.calibration.list_versions()],
                         [new_version["id"]])
        self.assertFalse((downgraded_bundle.calibration.root / "versions" /
                          f"{old_version['id']}.json").exists())
        with self.assertRaises(PlatformError) as caught:
            downgraded_bundle.calibration.install_portable_package(old_package)
        self.assertEqual(caught.exception.code, "parallel_calibration_package_superseded")

    def test_directory_rejects_forked_successors_before_materializing_any_package(self):
        old_hardware, old_version, old_package = self.portable_profile_package(
            "OLD FORK TARGET")
        target = {
            "package_id": old_package["package_id"],
            "hardware_profile_id": old_hardware["id"],
            "calibration_version_id": old_version["id"],
            "reason": "Only one successor may own a retired package identity.",
        }
        _, first_version, first_package = self.portable_profile_package(
            "FIRST FORKED SUCCESSOR", supersedes=[target])
        _, second_version, second_package = self.portable_profile_package(
            "SECOND FORKED SUCCESSOR", supersedes=[target])
        package_root = self.root / "forked-successor-packages"
        package_root.mkdir()
        (package_root / "first.json").write_text(
            json.dumps(first_package, ensure_ascii=False), encoding="utf-8")
        (package_root / "second.json").write_text(
            json.dumps(second_package, ensure_ascii=False), encoding="utf-8")
        state_root = self.root / "forked-successor-state"

        with self.assertRaises(PlatformError) as caught:
            ParallelModelingManager(state_root, portable_calibration_package_root=package_root)
        self.assertEqual(caught.exception.code, "parallel_calibration_package_conflict")
        version_root = state_root / "parallel-modeling" / "calibration" / "versions"
        self.assertFalse((version_root / f"{first_version['id']}.json").exists())
        self.assertFalse((version_root / f"{second_version['id']}.json").exists())

    def test_default_groups_collect_fit_and_publish_with_independent_roofline_holdout(self):
        plan = self.calibration.create_plan({"name": "DEFAULT GROUPS CLOSED LOOP", "repeats": 3})
        self.assertEqual(DEFAULT_GROUPS,
                         ("frequency_roofline", "pairwise_interference", "dispatch_overhead"))
        self.assertEqual(plan["parameter_groups"], list(DEFAULT_GROUPS))
        self.assertTrue(set(("core_ratios", "frequency_response", "contention")).isdisjoint(
            plan["parameter_groups"]))
        catalog = self.calibration.catalog()
        self.assertEqual(catalog["defaults"]["parameter_groups"], list(DEFAULT_GROUPS))
        self.assertIn("兼容回退", next(item for item in catalog["parameter_catalog"]
                                      if item["id"] == "core_ratios")["name"])
        self.assertEqual(catalog["measurement_contract"]["dispatch_overhead"]["v4_identity"]
                         ["dispatch_measurement_schema"], "paired_dispatch_delta/v1")
        self.assertEqual(catalog["measurement_contract"]["dispatch_overhead"]["v5_identity"]
                         ["workload_version"], "parallel-calibration-proxy-v5")
        self.assertEqual(catalog["measurement_contract"]["dispatch_overhead"]["v6_identity"]
                         ["workload_version"], "parallel-calibration-proxy-v6")
        dispatch_parameter = next(item for item in catalog["parameter_catalog"]
                                  if item["id"] == "dispatch_overhead")
        self.assertIn("wall 差分", dispatch_parameter["method"])
        for kind in plan["core_kinds"]:
            split = plan["frequency_roofline_split"][kind]
            self.assertGreaterEqual(len(split["train_khz"]), 5)
            self.assertEqual(len(split["validation_khz"]), 1)
            self.assertTrue(set(split["train_khz"]).isdisjoint(split["validation_khz"]))
            roofline_conditions = [row for row in plan["conditions"]
                                   if row["benchmark"] == "pair_work" and row["core_kind"] == kind
                                   and str(row.get("workload_id", "")).endswith("_frequency_proxy_v1")]
            train_opps = {row["frequency_khz"] for row in roofline_conditions if row["split"] == "train"}
            validation_opps = {row["frequency_khz"] for row in roofline_conditions if row["split"] == "validation"}
            self.assertEqual(train_opps, set(split["train_khz"]))
            self.assertEqual(validation_opps, set(split["validation_khz"]))
            for row in roofline_conditions:
                self.assertEqual(
                    row["workload_intensity"],
                    pair_work_source_intensity(
                        row["workload_class"], work_units=row["work_units"],
                        working_set_bytes=row["working_set_bytes"]),
                )
        collector_source = Path(__file__).resolve().parents[2] / "tools/parallel_calibration/collect.py"
        collector_spec = importlib.util.spec_from_file_location("default_groups_collect", collector_source)
        collector = importlib.util.module_from_spec(collector_spec); collector_spec.loader.exec_module(collector)
        plan_file, command_file = self.root / "default-plan.json", self.root / "default-commands.json"
        plan_file.write_text(json.dumps(plan), encoding="utf-8")
        self.assertEqual(collector.main(["--plan", str(plan_file), "--output", str(command_file), "--dry-run"]), 0)
        commands = json.loads(command_file.read_text(encoding="utf-8"))
        self.assertEqual(commands["schema_version"], "parallel_calibration_commands/v1")
        self.assertEqual(len(commands["commands"]), len(plan["conditions"]))
        self.assertEqual(commands["frequency_policy_ids"], plan["frequency_policy_ids"])
        self.assertTrue(all("--frequency-policy-id" in row["command"] for row in commands["commands"]))
        fitted = self.fit(plan, build_measurements(plan))
        self.assertTrue(fitted["quality"]["passed"], fitted["quality"])
        self.assertLess(fitted["quality"]["roofline_training_mape_max"], 1e-8)
        self.assertIsNone(fitted["model_preview"]["simulation_parameters"]["frequency_elasticity"])
        version = self.calibration.publish({"fit_id": fitted["id"], "name": "DEFAULT CLOSED LOOP"})
        self.assertEqual(version["version"], "1.3.0")
        self.assertIn("profiles.middle.build_ratio", version["inherited_parameters"])
        self.assertEqual(len(version["model"]["interference_model"]["cells"]), 9)
        reference = plan["reference_core_kind"]
        for kind, profile in version["model"]["profiles"].items():
            self.assertEqual(set(profile["frequency_roofline"]["classes"]), {"compute", "memory", "mixed"})
            for parameters in profile["frequency_roofline"]["classes"].values():
                self.assertGreater(parameters["max_opp_cpu_ms_per_work_unit"], 0)
                self.assertGreater(parameters["cross_core_time_ratio"], 0)
                self.assertEqual(parameters["transfer_reference"]["core_kind"], reference)
                self.assertEqual(parameters["transfer_reference"]["frequency_khz"],
                                 version["model"]["profiles"][reference]["max_khz"])
                self.assertEqual(parameters["formula"]["scope"],
                                 "target-thread On-CPU cost only")
                self.assertGreater(parameters["formula"]["parameters"]
                                   ["Tmax_cpu_ms_per_work_unit"], 0)
                self.assertIn("Tmax", parameters["formula"]["symbolic"])
                intensity = parameters["workload_intensity"]
                self.assertEqual(intensity["schema_version"],
                                 "pair_work_source_intensity/v1")
                self.assertIsNone(intensity["hardware_counter_evidence"]
                                  ["instruction_intensity_instructions_per_dram_byte"])

    def test_frequency_roofline_skips_only_ineligible_core_kind_in_mixed_opp_profile(self):
        profile = self.manager.create_hardware_profile({
            "name": "TEST ONLY MIXED 8 AND 5 OPP PHONE", "source": "test_fixture",
            "core_groups": [
                {"id": "cluster_8opp", "physical_core_count": 2,
                 "logical_cpu_ids": [0, 1],
                 "opps_khz": [400000, 600000, 800000, 1000000,
                              1200000, 1400000, 1600000, 1800000]},
                {"id": "cluster_5opp", "physical_core_count": 2,
                 "logical_cpu_ids": [2, 3],
                 "opps_khz": [500000, 900000, 1300000, 1700000, 2100000]},
            ],
        })
        # A topology proxy may start from a previously calibrated profile.
        # The ineligible group must not retain that unrelated curve.
        for base_profile in self.calibration.base_model["profiles"].values():
            base_profile["frequency_roofline"] = {"schema_version": "foreign_test_curve"}
        plan = self.calibration.create_plan({
            "name": "MIXED OPP CURVE SUBSET",
            "hardware_profile_id": profile["id"],
            "repeats": 3,
            "parameter_groups": ["frequency_roofline", "frequency_response"],
        })

        self.assertEqual(plan["core_kinds"], ["cluster_8opp", "cluster_5opp"])
        self.assertEqual(plan["frequency_roofline_core_kinds"], ["cluster_8opp"])
        self.assertEqual(set(plan["frequency_roofline_split"]), {"cluster_8opp"})
        curve_conditions = [row for row in plan["conditions"]
                            if str(row.get("workload_id", "")).endswith(
                                "_frequency_proxy_v1")]
        self.assertEqual({row["core_kind"] for row in curve_conditions},
                         {"cluster_8opp"})
        response_conditions = [row for row in plan["conditions"]
                               if row["benchmark"] == "cpu_work"]
        self.assertEqual({row["core_kind"] for row in response_conditions},
                         {"cluster_8opp", "cluster_5opp"},
                         "the curve subset must not narrow other parameter groups")

        curve_only = self.calibration.create_plan({
            "name": "MIXED OPP CURVE FIT",
            "hardware_profile_id": profile["id"],
            "repeats": 3,
            "parameter_groups": ["frequency_roofline"],
        })
        fitted = self.fit(curve_only, build_measurements(curve_only))
        self.assertEqual(fitted["status"], "passed", fitted["quality"])
        preview = fitted["model_preview"]["profiles"]
        self.assertIn("frequency_roofline", preview["cluster_8opp"])
        self.assertNotIn("frequency_roofline", preview["cluster_5opp"])
        self.assertEqual(curve_only["reference_core_kind"], "cluster_5opp")
        fitted_curve = preview["cluster_8opp"]["frequency_roofline"]
        self.assertEqual(fitted_curve["core_transfer"],
                         "legacy_profile_ratio_fallback_missing_reference_curve")
        for parameters in fitted_curve["classes"].values():
            self.assertNotIn("transfer_reference", parameters)
            self.assertNotIn("cross_core_time_ratio", parameters)
        self.assertEqual(
            fitted["model_preview"]["validation"]["calibration_domain"]
            ["frequency_roofline_core_kinds"],
            ["cluster_8opp"],
        )

    def test_pair_work_source_intensity_keeps_integer_and_flop_semantics_separate(self):
        compute = pair_work_source_intensity(
            "compute", work_units=100, working_set_bytes=64 * 1024 * 1024)
        memory = pair_work_source_intensity(
            "memory", work_units=8192, working_set_bytes=64 * 1024 * 1024)
        mixed = pair_work_source_intensity(
            "mixed", work_units=8192, working_set_bytes=64 * 1024 * 1024)

        self.assertEqual(compute["algorithmic_integer_operations_per_work_unit"], 640)
        self.assertEqual(compute["minimum_source_bytes_per_work_unit"], 0)
        self.assertIsNone(compute["operational_intensity_integer_ops_per_byte"])
        self.assertEqual(memory["algorithmic_integer_operations_per_work_unit"], 2052)
        self.assertEqual(memory["minimum_source_bytes_per_work_unit"], 16384)
        self.assertAlmostEqual(memory["operational_intensity_integer_ops_per_byte"],
                               2052 / 16384)
        self.assertEqual(memory["full_working_set_passes"], 1)
        self.assertEqual(mixed["algorithmic_integer_operations_per_work_unit"], 2692)
        self.assertAlmostEqual(mixed["operational_intensity_integer_ops_per_byte"],
                               2692 / 16384)
        self.assertNotIn("flop", " ".join(compute.keys()).lower())
        self.assertIn("neither FLOPs nor retired instructions",
                      compute["count_semantics"])

    def test_optional_direct_pmu_collected_partial_unavailable_and_scaling_contract(self):
        plan = self.plan(parameter_groups=["core_ratios"])
        cases = (
            ("collected", ("cycles", "instructions", "generic_cache_references",
                           "generic_cache_misses")),
            ("partial", ("cycles", "instructions")),
            ("unavailable", ()),
        )
        for expected_status, events in cases:
            with self.subTest(expected_status=expected_status):
                data = build_measurements(plan)
                with_optional_pmu(data["samples"][0], events)
                fitted = self.fit(plan, data)
                self.assertEqual(fitted["quality"]["rejected_samples"], 0,
                                 fitted["quality"]["rejected_sample_details"])
                retained = fitted["measurements"]["samples"][0]
                self.assertEqual(retained["pmu_collection_status"], expected_status)
                if expected_status == "collected":
                    self.assertAlmostEqual(retained["pmu_cache_misses"], 10 / 6)
                    self.assertAlmostEqual(retained["pmu_ipc"], 2)
                    self.assertAlmostEqual(retained["pmu_cache_mpki"], 1000 / 6)

        data = build_measurements(plan)
        with_optional_pmu(data["samples"][0])
        data["samples"][0]["pmu_counters"][3]["scaled_value"] += .01
        fitted = self.fit(plan, data)
        self.assertEqual(fitted["quality"]["rejected_samples"], 1)
        self.assertIn("缩放计数", fitted["quality"]["rejected_sample_details"][0]["reason"])

        data = build_measurements(plan)
        with_optional_pmu(data["samples"][0], ("cycles", "instructions"))
        data["samples"][0]["pmu_window_aligned"] = False
        fitted = self.fit(plan, data)
        self.assertEqual(fitted["quality"]["rejected_samples"], 1)
        self.assertIn("同一事件组计时窗",
                      fitted["quality"]["rejected_sample_details"][0]["reason"])

        data = build_measurements(plan)
        with_optional_pmu(data["samples"][0])
        data["samples"][0]["pmu_window_relation"] = "timed_window_includes_perf_read"
        fitted = self.fit(plan, data)
        self.assertEqual(fitted["quality"]["rejected_samples"], 1)
        self.assertIn("排除 perf 控制与读取开销",
                      fitted["quality"]["rejected_sample_details"][0]["reason"])

    def test_classic_roofline_only_uses_independent_intensity_ladder_validation(self):
        plan = self.calibration.create_plan({
            "name": "CLASSIC ROOFLINE ONLY", "repeats": 3,
            "parameter_groups": ["classic_roofline"], "core_kinds": ["middle"],
        })
        self.assertTrue(all(row["split"] == "validation"
                            for row in plan["conditions"]
                            if row["workload_class"] == "intensity_ladder"))
        self.assertTrue(all(row["split"] == "train"
                            for row in plan["conditions"]
                            if row["workload_class"] in ("compute_ceiling", "bandwidth_ceiling")))
        fitted = self.fit(plan, build_measurements(plan))
        self.assertEqual(fitted["status"], "passed", fitted["quality"])
        self.assertTrue(fitted["validation_predictions"])
        self.assertEqual({row["validation_metric"] for row in fitted["validation_predictions"]},
                         {"roof_ceiling_violation_ratio"})

        changed_placement = build_measurements(plan)
        for row in changed_placement["samples"]:
            if row["split"] == "validation":
                row["cpu"] = 6
                row["frequency_source_cpu"] = 6
                row["frequency_source_path"] = (
                    "/sys/devices/system/cpu/cpu6/cpufreq/cpuinfo_cur_freq")
        fitted = self.fit(plan, changed_placement)
        self.assertEqual(fitted["status"], "failed_integrity")
        self.assertFalse(fitted["quality"]["publishable"])
        self.assertTrue(any("目标CPU" in error for error in fitted["quality"]["errors"]))

    def test_classic_roofline_accepts_hardware_with_two_opps(self):
        profile = self.manager.create_hardware_profile({
            "name": "TEST ONLY TWO OPP DOMAIN", "source": "test_fixture",
            "core_groups": [{"id": "cluster", "physical_core_count": 2,
                             "logical_cpu_ids": [0, 1],
                             "opps_khz": [600000, 1200000]}],
        })
        plan = self.calibration.create_plan({
            "name": "TWO OPP CLASSIC ROOFLINE", "hardware_profile_id": profile["id"],
            "repeats": 3, "parameter_groups": ["classic_roofline"],
            "classic_roofline_working_set_bytes": 1024 * 1024,
        })
        self.assertEqual(plan["frequencies_khz"]["cluster"], [600000, 1200000])
        self.assertTrue(all(row["working_set_bytes"] >= 1024 * 1024
                            and row["working_set_bytes"] % 24 == 0
                            for row in plan["conditions"]))
        fitted = self.fit(plan, build_measurements(plan))
        self.assertEqual(fitted["status"], "passed", fitted["quality"])
        self.assertEqual({row["frequency_khz"] for row in fitted["model_preview"]["classic_roofline"]["roofs"]},
                         {600000, 1200000})

    def test_roofline_train_and_holdout_require_fixed_target_and_observer_cpu(self):
        plan = self.calibration.create_plan({"name": "FIXED ROOFLINE PLACEMENT", "repeats": 3,
                                             "parameter_groups": ["frequency_roofline"]})
        cases = (("cpu", 6, "目标CPU"), ("observer_cpu", 0, "观测CPU"))
        for field, replacement, message in cases:
            with self.subTest(field=field):
                data = build_measurements(plan)
                for row in data["samples"]:
                    if (row["core_kind"] == "middle" and row["workload_class"] == "compute"
                            and row["split"] == "validation"):
                        row[field] = replacement
                        if field == "cpu":
                            row["frequency_source_cpu"] = replacement
                            row["frequency_source_path"] = (
                                f"/sys/devices/system/cpu/cpu{replacement}/cpufreq/cpuinfo_cur_freq")
                fitted = self.fit(plan, data)
                self.assertEqual(fitted["status"], "failed_integrity")
                self.assertFalse(fitted["quality"]["passed"])
                self.assertFalse(fitted["quality"]["publishable"])
                self.assertTrue(any(message in error for error in fitted["quality"]["errors"]),
                                fitted["quality"]["errors"])
                domain = next(item for item in fitted["quality"]["roofline_placement_domains"]
                              if item["core_kind"] == "middle" and item["workload_class"] == "compute")
                self.assertFalse(domain["consistent"])

    def test_empty_cannot_publish_but_partial_frequency_fit_can_publish_with_risk(self):
        plan = self.plan()
        data = build_measurements(plan)
        data["samples"] = []
        fit = self.calibration.fit({"plan_id": plan["id"], "measurements": data})
        self.assertEqual(fit["status"], "needs_data")
        with self.assertRaises(PlatformError): self.calibration.publish({"fit_id": fit["id"]})
        data = build_measurements(plan)
        data["samples"] = [row for row in data["samples"] if row["frequency_khz"] == max(plan["frequencies_khz"][row["core_kind"]])]
        fit = self.fit(plan, data)
        self.assertFalse(fit["quality"]["passed"])
        self.assertTrue(any("频率响应" in error for error in fit["quality"]["errors"]))
        self.assertEqual(fit["status"], "publishable_with_risk")
        self.assertTrue(fit["quality"]["publishable"])
        version = self.calibration.publish({"fit_id": fit["id"]})
        self.assertEqual(version["status"], "risk_calibrated")
        self.assertEqual(self.calibration.resolve_model(version["id"])["id"], version["id"])

    def test_missing_validation_and_leakage_are_rejected(self):
        plan = self.plan()
        data = build_measurements(plan)
        data["samples"] = [row for row in data["samples"] if row["split"] == "train"]
        missing_validation = self.fit(plan, data)
        self.assertEqual(missing_validation["status"], "publishable_with_risk")
        self.assertTrue(missing_validation["quality"]["publishable"])
        self.assertTrue(any(gate["category"] == "coverage"
                            for gate in missing_validation["quality"]["failed_gates"]))
        data = build_measurements(plan)
        train_id = next(row["run_id"] for row in data["samples"] if row["split"] == "train")
        next(row for row in data["samples"] if row["split"] == "validation")["run_id"] = train_id
        fit = self.fit(plan, data)
        self.assertFalse(fit["quality"]["passed"])
        self.assertEqual(fit["status"], "failed_integrity")
        self.assertFalse(fit["quality"]["publishable"])
        self.assertTrue(any("共用" in error for error in fit["quality"]["errors"]))
        with self.assertRaises(PlatformError):
            self.calibration.publish({"fit_id": fit["id"]})

    def test_bad_frequency_cpu_or_thermal_data_are_not_silent_calibration(self):
        plan = self.plan()
        data = build_measurements(plan)
        data["samples"][0]["frequency_verified"] = False
        data["samples"][1]["cpu_ms"] = data["samples"][1]["wall_ms"] * 2
        data["samples"][2]["thermal_throttled"] = True
        fit = self.fit(plan, data)
        self.assertEqual(fit["quality"]["rejected_samples"], 3)
        self.assertFalse(fit["quality"]["passed"])
        data = build_measurements(plan)
        data["provenance"]["source_kind"] = "synthetic"
        with self.assertRaises(PlatformError): self.fit(plan, data)
        data = build_measurements(plan)
        data["provenance"]["fixture_only"] = True
        with self.assertRaises(PlatformError): self.fit(plan, data)
        data = build_measurements(plan)
        data["samples"][0]["cpu_ms"] = float("nan")
        with self.assertRaises(PlatformError): self.fit(plan, data)

    def test_every_fit_sample_requires_target_sibling_quietness_evidence(self):
        plan = self.plan(parameter_groups=["dispatch_overhead"])
        fields = (
            "target_sibling_cpus",
            "target_sibling_activity",
            "target_sibling_activity_verified",
            "target_sibling_busy_fraction",
            "target_sibling_busy_threshold",
            "target_sibling_quietness_verified",
        )
        for field in fields:
            with self.subTest(field=field):
                data = build_measurements(plan)
                data["samples"][0].pop(field)
                fitted = self.fit(plan, data)
                self.assertEqual(fitted["quality"]["rejected_samples"], 1)
                self.assertIn("target_sibling", fitted["quality"]["rejected_sample_details"][0]["reason"])

    def test_busy_target_sibling_and_forged_verification_are_rejected(self):
        plan = self.plan(parameter_groups=["dispatch_overhead"])

        busy = build_measurements(plan)
        row = busy["samples"][0]
        sibling = 91 if row["cpu"] != 91 else 92
        row.update(
            target_sibling_cpus=[sibling],
            target_sibling_activity=[{
                "cpu": sibling, "busy_ticks": 6, "total_ticks": 100,
                "busy_fraction": .06, "verified": True,
            }],
            target_sibling_activity_verified=True,
            target_sibling_busy_fraction=.06,
            target_sibling_busy_threshold=.05,
            target_sibling_quietness_verified=False,
        )
        fitted = self.fit(plan, busy)
        self.assertEqual(fitted["quality"]["rejected_samples"], 1)
        self.assertIn("超过5%", fitted["quality"]["rejected_sample_details"][0]["reason"])

        forged = build_measurements(plan)
        row = forged["samples"][0]
        sibling = 91 if row["cpu"] != 91 else 92
        row.update(
            target_sibling_cpus=[sibling],
            target_sibling_activity=[{
                "cpu": sibling, "busy_ticks": 0, "total_ticks": 0,
                "busy_fraction": None, "verified": False,
            }],
            target_sibling_activity_verified=True,
            target_sibling_busy_fraction=0,
            target_sibling_busy_threshold=.05,
            target_sibling_quietness_verified=True,
        )
        fitted = self.fit(plan, forged)
        self.assertEqual(fitted["quality"]["rejected_samples"], 1)
        self.assertIn("逐CPU核验状态不一致",
                      fitted["quality"]["rejected_sample_details"][0]["reason"])

    def test_target_sibling_lists_ticks_fraction_and_aggregate_are_recomputed(self):
        plan = self.plan(parameter_groups=["dispatch_overhead"])

        def smt_row():
            data = build_measurements(plan)
            row = data["samples"][0]
            siblings = [91, 92] if row["cpu"] not in (91, 92) else [93, 94]
            row.update(
                target_sibling_cpus=siblings,
                target_sibling_activity=[
                    {"cpu": siblings[0], "busy_ticks": 1, "total_ticks": 100,
                     "busy_fraction": .01, "verified": True},
                    {"cpu": siblings[1], "busy_ticks": 2, "total_ticks": 100,
                     "busy_fraction": .02, "verified": True},
                ],
                target_sibling_activity_verified=True,
                target_sibling_busy_fraction=.02,
                target_sibling_busy_threshold=.05,
                target_sibling_quietness_verified=True,
            )
            return data, row

        valid, _ = smt_row()
        self.assertEqual(self.fit(plan, valid)["quality"]["rejected_samples"], 0)

        cases = {
            "duplicate sibling": lambda row: row.update(
                target_sibling_cpus=[row["target_sibling_cpus"][0]] * 2),
            "contains target": lambda row: row.update(
                target_sibling_cpus=[row["cpu"], row["target_sibling_cpus"][1]]),
            "detail mismatch": lambda row: row["target_sibling_activity"][0].update(cpu=95),
            "fraction mismatch": lambda row: row["target_sibling_activity"][0].update(
                busy_fraction=.03),
            "aggregate mismatch": lambda row: row.update(target_sibling_busy_fraction=.01),
            "threshold mismatch": lambda row: row.update(target_sibling_busy_threshold=.10),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                data, row = smt_row()
                mutate(row)
                self.assertEqual(self.fit(plan, data)["quality"]["rejected_samples"], 1)

    def test_non_smt_target_has_explicit_empty_verified_evidence(self):
        plan = self.plan(parameter_groups=["dispatch_overhead"])
        data = build_measurements(plan)
        self.assertTrue(all(row["target_sibling_cpus"] == []
                            and row["target_sibling_activity"] == []
                            and row["target_sibling_busy_fraction"] == 0
                            and row["target_sibling_activity_verified"] is True
                            and row["target_sibling_quietness_verified"] is True
                            for row in data["samples"]))
        fitted = self.fit(plan, data)
        self.assertEqual(fitted["status"], "passed", fitted["quality"])

    def test_fit_requires_target_per_cpu_cpuinfo_frequency_evidence(self):
        plan = self.plan(parameter_groups=["dispatch_overhead"])
        for field in ("frequency_source", "frequency_source_cpu", "frequency_source_path"):
            with self.subTest(missing=field):
                data = build_measurements(plan)
                data["samples"][0].pop(field)
                fitted = self.fit(plan, data)
                self.assertEqual(fitted["quality"]["rejected_samples"], 1)

        cases = {
            "fallback": lambda row: row.update(
                frequency_source="scaling_cur_freq_fallback",
                frequency_source_path="/sys/devices/system/cpu/cpufreq/policy0/scaling_cur_freq"),
            "wrong cpu": lambda row: row.update(frequency_source_cpu=row["cpu"] + 1),
            "wrong path": lambda row: row.update(
                frequency_source_path="/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_cur_freq"),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                data = build_measurements(plan)
                mutate(data["samples"][0])
                fitted = self.fit(plan, data)
                self.assertEqual(fitted["quality"]["rejected_samples"], 1)

    def test_target_frequency_window_evidence_is_recomputed_fail_closed(self):
        plan = self.plan(parameter_groups=["dispatch_overhead"])
        fields = (
            "frequency_sample_count", "frequency_read_failures",
            "frequency_mean_khz", "frequency_min_khz", "frequency_max_khz",
            "frequency_verified",
        )
        for field in fields:
            with self.subTest(missing=field):
                data = build_measurements(plan)
                data["samples"][0].pop(field)
                self.assertEqual(self.fit(plan, data)["quality"]["rejected_samples"], 1)

        cases = {
            "too few samples with forged verified": lambda row: row.update(
                frequency_sample_count=4),
            "read failure with forged verified": lambda row: row.update(
                frequency_read_failures=1),
            "mean below minimum": lambda row: row.update(
                frequency_mean_khz=row["frequency_min_khz"] - 1),
            "minimum 3.1 percent outside tolerance": lambda row: row.update(
                frequency_min_khz=row["frequency_khz"] * 969 // 1000,
                observed_frequency_min_khz=row["frequency_khz"] * 969 // 1000),
            "maximum 3.1 percent outside tolerance": lambda row: row.update(
                frequency_max_khz=row["frequency_khz"] * 1031 // 1000,
                observed_frequency_max_khz=row["frequency_khz"] * 1031 // 1000),
            "forged false verdict": lambda row: row.update(frequency_verified=False),
            "mean alias mismatch": lambda row: row.update(
                observed_frequency_khz=row["frequency_mean_khz"] + 1),
            "minimum alias mismatch": lambda row: row.update(
                observed_frequency_min_khz=row["frequency_min_khz"] + 1),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                data = build_measurements(plan)
                mutate(data["samples"][0])
                self.assertEqual(self.fit(plan, data)["quality"]["rejected_samples"], 1)

        boundary = build_measurements(plan)
        row = boundary["samples"][0]
        row.update(
            frequency_min_khz=row["frequency_khz"] * 97 // 100,
            observed_frequency_min_khz=row["frequency_khz"] * 97 // 100,
            frequency_max_khz=row["frequency_khz"] * 103 // 100,
            observed_frequency_max_khz=row["frequency_khz"] * 103 // 100,
        )
        fitted = self.fit(plan, boundary)
        self.assertEqual(fitted["quality"]["rejected_samples"], 0)
        self.assertEqual(fitted["status"], "passed", fitted["quality"])

    def test_pair_fit_requires_peer_cpuinfo_source_bound_to_actual_helper(self):
        plan = self.plan(parameter_groups=["pairwise_interference"])
        for mutation in ("missing", "fallback", "wrong_cpu", "wrong_path"):
            with self.subTest(mutation=mutation):
                data = build_measurements(plan)
                row = next(item for item in data["samples"] if item["active_tasks"] == 2)
                if mutation == "missing":
                    row.pop("peer_frequency_source")
                elif mutation == "fallback":
                    row.update(
                        peer_frequency_source="scaling_cur_freq_fallback",
                        peer_frequency_source_path=
                            "/sys/devices/system/cpu/cpufreq/policy1/scaling_cur_freq")
                elif mutation == "wrong_cpu":
                    row["peer_frequency_source_cpu"] = row["helpers"][0]["cpu"] + 1
                else:
                    row["peer_frequency_source_path"] = (
                        "/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_cur_freq")
                fitted = self.fit(plan, data)
                self.assertEqual(fitted["quality"]["rejected_samples"], 1)

    def test_pair_fit_recomputes_helper_actual_cpu_and_affinity_binding(self):
        plan = self.plan(parameter_groups=["pairwise_interference"])
        for mutation in ("missing_actual_cpu", "actual_cpu_mismatch", "affinity_false"):
            with self.subTest(mutation=mutation):
                data = build_measurements(plan)
                row = next(item for item in data["samples"] if item["active_tasks"] == 2)
                helper = row["helpers"][0]
                if mutation == "missing_actual_cpu":
                    helper.pop("actual_cpu")
                elif mutation == "actual_cpu_mismatch":
                    helper["actual_cpu"] = helper["cpu"] + 1
                else:
                    helper["affinity_verified"] = False
                fitted = self.fit(plan, data)
                self.assertEqual(fitted["quality"]["rejected_samples"], 1)

    def test_peer_frequency_window_evidence_is_recomputed_fail_closed(self):
        plan = self.plan(parameter_groups=["pairwise_interference"])

        def paired_row(data):
            return next(item for item in data["samples"] if item["active_tasks"] == 2)

        fields = (
            "peer_frequency_sample_count", "peer_frequency_read_failures",
            "peer_observed_frequency_khz", "peer_frequency_min_khz",
            "peer_frequency_max_khz", "peer_frequency_verified",
        )
        for field in fields:
            with self.subTest(missing=field):
                data = build_measurements(plan)
                paired_row(data).pop(field)
                self.assertEqual(self.fit(plan, data)["quality"]["rejected_samples"], 1)

        cases = {
            "too few samples with forged verified": lambda row: row.update(
                peer_frequency_sample_count=4),
            "read failure with forged verified": lambda row: row.update(
                peer_frequency_read_failures=1),
            "mean above maximum": lambda row: row.update(
                peer_observed_frequency_khz=row["peer_frequency_max_khz"] + 1),
            "minimum outside tolerance": lambda row: row.update(
                peer_frequency_min_khz=int(row["peer_frequency_khz"] * .96)),
            "maximum outside tolerance": lambda row: row.update(
                peer_frequency_max_khz=int(row["peer_frequency_khz"] * 1.04)),
            "forged false verdict": lambda row: row.update(peer_frequency_verified=False),
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                data = build_measurements(plan)
                mutate(paired_row(data))
                self.assertEqual(self.fit(plan, data)["quality"]["rejected_samples"], 1)

    def test_production_fit_rejects_pre_v6_workload_samples(self):
        plan = self.plan(parameter_groups=["core_ratios"])
        for version in (None, "parallel-calibration-proxy-v4",
                        "parallel-calibration-proxy-v5"):
            with self.subTest(version=version):
                data = build_measurements(plan)
                if version is None:
                    data["samples"][0].pop("workload_version")
                else:
                    data["samples"][0]["workload_version"] = version
                fitted = self.fit(plan, data)
                self.assertEqual(fitted["quality"]["rejected_samples"], 1)
                self.assertIn("只接受 parallel-calibration-proxy-v6",
                              fitted["quality"]["rejected_sample_details"][0]["reason"])

    def test_adaptive_frequency_residency_evidence_is_fail_closed(self):
        plan = self.plan(parameter_groups=["core_ratios"])
        fields = (
            "warmup_frequency_sample_count",
            "warmup_frequency_in_range_consecutive",
            "warmup_frequency_required_consecutive",
            "warmup_frequency_max_samples",
            "warmup_frequency_min_khz",
            "warmup_frequency_max_khz",
            "warmup_frequency_last_khz",
            "warmup_frequency_verified",
            "warmup_work_units",
        )
        for field in fields:
            with self.subTest(missing=field):
                data = build_measurements(plan)
                data["samples"][0].pop(field)
                self.assertEqual(self.fit(plan, data)["quality"]["rejected_samples"], 1)

        def wrong_units(row):
            row["warmup_work_units"] += 1

        cases = {
            "count below minimum": lambda row: row.update(
                warmup_frequency_sample_count=2, warmup_work_units=2 * min(row["work_units"], 32768)),
            "required changed": lambda row: row.update(
                warmup_frequency_required_consecutive=2),
            "maximum changed": lambda row: row.update(warmup_frequency_max_samples=11),
            "consecutive short": lambda row: row.update(
                warmup_frequency_in_range_consecutive=2),
            "forged fourth consecutive hit": lambda row: row.update(
                warmup_frequency_in_range_consecutive=4),
            "range order": lambda row: row.update(
                warmup_frequency_min_khz=row["frequency_khz"] + 1),
            "last outside tolerance": lambda row: row.update(
                warmup_frequency_last_khz=int(row["frequency_khz"] * 1.04),
                warmup_frequency_max_khz=int(row["frequency_khz"] * 1.04)),
            "forged verified": lambda row: row.update(warmup_frequency_verified=False),
            "work-unit mismatch": wrong_units,
        }
        for name, mutate in cases.items():
            with self.subTest(name=name):
                data = build_measurements(plan)
                mutate(data["samples"][0])
                self.assertEqual(self.fit(plan, data)["quality"]["rejected_samples"], 1)

        converged = build_measurements(plan)
        row = converged["samples"][0]
        row.update(
            warmup_frequency_sample_count=4,
            warmup_frequency_in_range_consecutive=3,
            warmup_frequency_min_khz=max(1, row["frequency_khz"] // 2),
            warmup_frequency_max_khz=int(row["frequency_khz"] * 1.5),
            warmup_work_units=4 * min(row["work_units"], 32768),
        )
        fitted = self.fit(plan, converged)
        self.assertEqual(fitted["status"], "passed", fitted["quality"])

    def test_bounded_retry_keeps_contaminated_attempt_auditable_without_poisoning_replacement(self):
        plan = self.plan(parameter_groups=["dispatch_overhead"])
        data = build_measurements(plan)
        contaminated = copy.deepcopy(data["samples"][0])
        contaminated.update(
            sample_id="TEST_ONLY_CONTAMINATED_ATTEMPT",
            run_id="TEST_ONLY_CONTAMINATED_RUN",
            collection_attempt=1,
            max_collection_attempts=3,
            target_sibling_cpus=[91],
            target_sibling_activity=[{
                "cpu": 91, "busy_ticks": 20, "total_ticks": 100,
                "busy_fraction": .20, "verified": True,
            }],
            target_sibling_activity_verified=True,
            target_sibling_busy_fraction=.20,
            target_sibling_busy_threshold=.05,
            target_sibling_quietness_verified=False,
        )
        data["samples"].insert(0, contaminated)
        fitted = self.fit(plan, data)
        self.assertEqual(fitted["status"], "passed", fitted["quality"])
        self.assertEqual(fitted["quality"]["rejected_samples"], 1)
        self.assertIn("超过5%", fitted["quality"]["rejected_sample_details"][0]["reason"])

    def test_frequency_and_roofline_samples_require_valid_thermal_snapshots(self):
        plan = self.calibration.create_plan({
            "name": "THERMAL EVIDENCE GATE",
            "repeats": 3,
            "parameter_groups": ["frequency_roofline"],
        })
        cases = (
            ("missing before", lambda row: row.pop("thermal_snapshot_before")),
            ("empty after", lambda row: row.update(thermal_snapshot_after=[])),
            ("invalid zone", lambda row: row.update(
                thermal_snapshot_before=[{"zone": "cpu0", "type": "cpu", "temperature_raw": 41000}])),
            ("non-integer temperature", lambda row: row.update(
                thermal_snapshot_after=[{"zone": "thermal_zone0", "type": "cpu",
                                         "temperature_raw": 41100.0}])),
        )
        for name, mutate in cases:
            with self.subTest(name=name):
                data = build_measurements(plan)
                mutate(data["samples"][0])
                fitted = self.fit(plan, data)
                self.assertEqual(fitted["quality"]["rejected_samples"], 1)
                self.assertFalse(fitted["quality"]["passed"])
                self.assertIn("thermal", fitted["quality"]["rejected_sample_details"][0]["reason"])

    def test_dispatch_only_measurements_keep_legacy_thermal_snapshot_compatibility(self):
        plan = self.plan(parameter_groups=["dispatch_overhead"])
        data = build_measurements(plan)
        for row in data["samples"]:
            row.pop("thermal_snapshot_before")
            row.pop("thermal_snapshot_after")
        fitted = self.fit(plan, data)
        self.assertEqual(fitted["status"], "passed", fitted["quality"])

    def test_holdout_error_and_repeat_variation_fail_quality(self):
        plan = self.plan()
        data = build_measurements(plan)
        for row in data["samples"]:
            if row["split"] == "validation": row.update(cpu_ms=row["cpu_ms"] * 1.5, wall_ms=row["wall_ms"] * 1.5)
        fit = self.fit(plan, data)
        self.assertEqual(fit["status"], "publishable_with_risk")
        self.assertTrue(fit["quality"]["publishable"])
        self.assertGreater(fit["quality"]["validation_median_ape"], .20)
        version = self.calibration.publish({"fit_id": fit["id"]})
        self.assertEqual(version["status"], "risk_calibrated")
        self.assertTrue(any("留出" in gate["message"] for gate in version["failed_gates"]))
        self.assertTrue(version["failed_gates"])
        self.assertEqual(self.calibration.resolve_model(version["id"])["id"], version["id"])
        catalog_model = next(item for item in self.manager.catalog()["model_packages"]
                             if item["id"] == version["id"])
        self.assertEqual(catalog_model["risk_level"], version["risk_level"])
        self.assertEqual(catalog_model["evidence_grade"], version["evidence_grade"])
        dataset = self.manager.import_dataset({
            "trace_text": trace_fixture(), "hardware_profile_id": HARDWARE_ID})
        analysis = self.manager.analyze({
            "dataset_id": dataset["id"], "model_package_id": version["id"]})
        space = {"available_core_group_ids": ["big", "middle"],
                 "main_core_group_id": "big", "worker_counts": [1],
                 "main_participation": [False], "frequency_policy": "all_opps",
                 "frequencies_khz": {"big": [2500000], "middle": [1210000]}}
        search = self.manager.search({"analysis_id": analysis["id"], "space": space,
                                      "model_package_id": version["id"]})
        self.assertEqual(search["model_package"]["id"], version["id"])
        self.assertEqual(search["model_package"]["calibration_risk"]["risk_level"],
                         version["risk_level"])
        data = build_measurements(plan)
        for row in data["samples"]:
            if row["repeat"] == 0: row.update(cpu_ms=row["cpu_ms"] * 1.5, wall_ms=row["wall_ms"] * 1.5)
        fit = self.fit(plan, data)
        self.assertFalse(fit["quality"]["passed"])
        self.assertTrue(fit["quality"]["publishable"])
        self.assertGreater(fit["quality"]["repeat_cv_max"], .15)

    def test_partial_parameter_fit_keeps_inherited_parameters_explicit(self):
        plan = self.plan(parameter_groups=["dispatch_overhead"])
        fit = self.fit(plan)
        self.assertEqual(fit["status"], "passed", fit["quality"])
        version = self.calibration.publish({"fit_id": fit["id"]})
        self.assertEqual(version["status"], "partially_calibrated")
        self.assertEqual(version["calibrated_parameters"], [
            "simulation_parameters.branch_overhead_ms",
            "simulation_parameters.dispatch_overhead.dispatch_worker_cpu_delta_ms_per_task",
            "simulation_parameters.dispatch_overhead.dispatch_producer_cpu_delta_ms_per_task",
        ])
        self.assertIn("profiles.middle.build_ratio", version["inherited_parameters"])
        self.assertEqual(version["model"]["simulation_parameters"]["service_capacity"], 1)

    def test_v6_paired_dispatch_uses_wall_delta_and_keeps_cpu_attribution(self):
        plan = self.plan(parameter_groups=["dispatch_overhead"])
        default_fit = self.fit(plan)
        self.assertAlmostEqual(default_fit["model_preview"]["simulation_parameters"]["branch_overhead_ms"],
                               FIXTURE_PARAMETERS["branch_overhead_ms"])
        self.assertEqual(
            next(row for row in default_fit["parameter_estimates"]
                 if row["path"] == "simulation_parameters.branch_overhead_ms")["method"],
            "median_paired_queue_minus_direct_roundtrip_wall_ms_per_task")

        fitted = self.fit(plan, paired_dispatch_v4(build_measurements(plan)))
        self.assertEqual(fitted["status"], "passed", fitted["quality"])
        simulation = fitted["model_preview"]["simulation_parameters"]
        self.assertAlmostEqual(simulation["branch_overhead_ms"], .0017)
        dispatch = simulation["dispatch_overhead"]
        self.assertEqual(dispatch["schema_version"], "paired_dispatch_delta/v1")
        self.assertAlmostEqual(dispatch["dispatch_wall_delta_ms_per_task"], .0017)
        self.assertAlmostEqual(dispatch["dispatch_worker_cpu_delta_ms_per_task"], .0006)
        self.assertAlmostEqual(dispatch["dispatch_producer_cpu_delta_ms_per_task"], .0004)
        self.assertTrue(dispatch["cpu_deltas_are_attribution_only"])
        self.assertTrue(all(row["validation_metric"]
                            == "dispatch_wall_delta_absolute_percentage_error"
                            for row in fitted["validation_predictions"]))
        self.assertTrue(all(abs(row["observed_dispatch_wall_delta_ms"]
                                - .0017 * row["dispatch_task_count"]) < 1e-12
                            for row in fitted["validation_predictions"]))

    def test_v4_dispatch_holdout_compares_wall_delta_times_task_count(self):
        plan = self.plan(parameter_groups=["dispatch_overhead"])
        data = paired_dispatch_v4(build_measurements(plan))
        # Compatibility cpu_ms is the queue worker CPU total. It is not the
        # v4 branch critical-path target and must not drive holdout error.
        for row in data["samples"]:
            if row["benchmark"] == "empty_dispatch" and row["split"] == "validation":
                row["cpu_ms"] *= .5
        fitted = self.fit(plan, data)
        self.assertEqual(fitted["status"], "passed", fitted["quality"])
        self.assertLess(fitted["quality"]["validation_p90_ape"], 1e-12)

        changed = paired_dispatch_v4(build_measurements(plan))
        for row in changed["samples"]:
            if row["benchmark"] == "empty_dispatch" and row["split"] == "validation":
                row["dispatch_wall_delta_ms_per_task"] *= 1.5
                row["queue_dispatch_roundtrip_wall_ms"] = (
                    row["direct_batch_roundtrip_wall_ms"]
                    + row["dispatch_wall_delta_ms_per_task"] * row["work_units"])
        failed = self.fit(plan, changed)
        self.assertEqual(failed["status"], "publishable_with_risk")
        self.assertTrue(failed["quality"]["publishable"])
        self.assertGreater(failed["quality"]["validation_median_ape"], .30)

    def test_v4_dispatch_rejects_broken_pair_contract(self):
        plan = self.plan(parameter_groups=["dispatch_overhead"])
        data = paired_dispatch_v4(build_measurements(plan))
        data["samples"][0]["dispatch_payload_match"] = False
        fitted = self.fit(plan, data)
        self.assertEqual(fitted["quality"]["rejected_samples"], 1)
        self.assertIn("同负载配对差分合同",
                      fitted["quality"]["rejected_sample_details"][0]["reason"])

        totals = paired_dispatch_v4(build_measurements(plan))
        totals["samples"][0]["queue_dispatch_roundtrip_wall_ms"] += 1
        fitted = self.fit(plan, totals)
        self.assertEqual(fitted["quality"]["rejected_samples"], 1)
        self.assertIn("与配对总量不一致",
                      fitted["quality"]["rejected_sample_details"][0]["reason"])

    def test_v4_v5_and_v6_dispatch_all_enforce_the_paired_contract(self):
        plan = self.plan(parameter_groups=["dispatch_overhead"])
        for version in ("parallel-calibration-proxy-v4", "parallel-calibration-proxy-v5",
                        "parallel-calibration-proxy-v6"):
            with self.subTest(version=version):
                data = paired_dispatch_v4(build_measurements(plan))
                for row in data["samples"]:
                    row["workload_version"] = version
                data["samples"][0]["dispatch_payload_match"] = False
                fitted = self.fit(plan, data)
                self.assertEqual(fitted["quality"]["rejected_samples"],
                                 1 if version.endswith("v6") else len(data["samples"]))
                self.assertIn("同负载配对差分合同",
                              fitted["quality"]["rejected_sample_details"][0]["reason"])

    def test_selected_calibration_changes_actual_search_and_analysis_predictions(self):
        fit = self.fit()
        version = self.calibration.publish({"fit_id": fit["id"]})
        dataset = self.manager.import_dataset({"trace_text": trace_fixture(), "hardware_profile_id": HARDWARE_ID})
        original = self.manager.analyze({"dataset_id": dataset["id"], "model_package_id": MODEL_ID})
        calibrated = self.manager.analyze({"dataset_id": dataset["id"], "model_package_id": version["id"]})
        self.assertEqual(original["baseline"], calibrated["baseline"])
        self.assertNotEqual(original["model_reference_costs"], calibrated["model_reference_costs"])
        space = {"available_core_group_ids": ["big", "middle"], "main_core_group_id": "big", "worker_counts": [1], "main_participation": [False], "frequency_policy": "all_opps", "frequencies_khz": {"big": [2500000], "middle": [1210000]}}
        first = self.manager.search({"analysis_id": original["id"], "space": space, "model_package_id": MODEL_ID})
        second = self.manager.search({"analysis_id": original["id"], "space": space, "model_package_id": version["id"]})
        self.assertNotEqual(first["best"]["metrics"]["finish_ms"], second["best"]["metrics"]["finish_ms"])
        self.assertEqual(second["model_package"]["id"], version["id"])
        self.assertEqual(second["search_provenance"]["nominal_parameters"]["branch_overhead_ms"], .002)
        self.assertAlmostEqual(second["search_provenance"]["nominal_parameters"]["per_core_frequency_elasticity"]["middle"], .8)
        self.assertEqual(second["search_provenance"]["model_sha256"], version["model_sha256"])
        inherited_selection = self.manager.search({"analysis_id": calibrated["id"], "space": space})
        self.assertEqual(inherited_selection["model_package"]["id"], version["id"])
        self.assertEqual(inherited_selection["best"]["metrics"], second["best"]["metrics"])
        robust = self.manager.search({"analysis_id": original["id"], "space": space, "model_package_id": version["id"], "robustness": True})
        self.assertEqual(robust["best"]["metrics"]["scenario_count"], 6)
        self.assertEqual(robust["best"]["metrics"]["finish_ms"], second["best"]["metrics"]["finish_ms"])

    def test_api_plan_fit_publish_and_version_tamper_detection(self):
        app = FastAPI()
        app.include_router(create_calibration_router(self.calibration))
        client = TestClient(app)
        catalog = client.get("/v1/parallel-calibration/catalog")
        self.assertEqual(catalog.status_code, 200)
        plan = client.post("/v1/parallel-calibration/plans", json={"name": "ISOLATED API TEST", "repeats": 3,
                                                                    "parameter_groups": list(GROUPS)}).json()
        fit = client.post("/v1/parallel-calibration/fits", json={"plan_id": plan["id"], "measurements": build_measurements(plan)}).json()
        self.assertEqual(fit["status"], "passed", fit)
        version = client.post("/v1/parallel-calibration/versions", json={"fit_id": fit["id"]}).json()
        self.assertEqual(client.get("/v1/parallel-calibration/versions").json()["versions"][0]["id"], version["id"])
        path = self.calibration.root / "versions" / (version["id"] + ".json")
        modified = json.loads(path.read_text())
        modified["model"]["profiles"]["middle"]["build_ratio"] = 100
        path.write_text(json.dumps(modified))
        with self.assertRaises(PlatformError) as caught: self.calibration.resolve_model(version["id"])
        self.assertEqual(caught.exception.code, "parallel_calibration_model_corrupt")

    def test_binary_consistency_physical_concurrency_and_per_condition_validation(self):
        plan = self.plan()
        data = build_measurements(plan)
        data["samples"][0]["binary_sha256"] = "b" * 64
        self.assertFalse(self.fit(plan, data)["quality"]["passed"])
        data = build_measurements(plan)
        next(row for row in data["samples"] if row["active_tasks"] > 1)["physical_core_placement_verified"] = False
        self.assertEqual(self.fit(plan, data)["quality"]["rejected_samples"], 1)
        data = build_measurements(plan)
        next(row for row in data["samples"] if row["active_tasks"] > 1)["concurrency_verified"] = False
        self.assertEqual(self.fit(plan, data)["quality"]["rejected_samples"], 1)
        data = build_measurements(plan)
        heldout = next(row for row in plan["conditions"] if row["split"] == "validation")
        from backend.parallel_modeling.calibration import _condition
        for row in data["samples"]:
            if _condition(row) == _condition(heldout): row.update(cpu_ms=row["cpu_ms"] * 2, wall_ms=row["wall_ms"] * 2)
        fit = self.fit(plan, data)
        self.assertFalse(fit["quality"]["passed"])
        self.assertTrue(any(not row["passed"] for row in fit["quality"]["validation_by_condition"]))

    def test_bundle_is_complete_and_has_no_fabricated_measurements(self):
        plan = self.plan()
        files = {item["filename"]: item for item in self.calibration.bundle(plan["id"])["files"]}
        self.assertEqual(set(files), {"plan.json", "measurement-contract.json", "measurements-template.json", "benchmark.c", "collect.py", "README.md"})
        self.assertEqual(json.loads(files["measurements-template.json"]["content"])["samples"], [])
        self.assertTrue(all(len(item["sha256"]) == 64 for item in files.values()))

    def test_custom_hardware_profile_freezes_real_groups_opps_and_cpu_map_through_publish(self):
        profile = self.manager.create_hardware_profile({
            "name": "TEST ONLY FOUR DOMAIN PHONE", "source": "test_fixture",
            "core_groups": [
                {"id": "cluster_0", "physical_core_count": 2, "logical_cpu_ids": [0, 1],
                 "opps_khz": [400000, 900000, 1400000]},
                {"id": "cluster_1", "physical_core_count": 2, "logical_cpu_ids": [2, 3],
                 "opps_khz": [500000, 1100000, 1800000]},
                {"id": "cluster_2", "physical_core_count": 2, "logical_cpu_ids": [4, 5],
                 "opps_khz": [700000, 1500000, 2400000]},
                {"id": "cluster_3", "physical_core_count": 1, "logical_cpu_ids": [6],
                 "opps_khz": [900000, 2100000, 3000000]},
            ],
        })
        plan = self.calibration.create_plan({
            "name": "CUSTOM PROFILE CALIBRATION", "hardware_profile_id": profile["id"],
            "repeats": 3, "parameter_groups": ["dispatch_overhead"],
            "core_kinds": ["cluster_1"],
        })
        self.assertEqual(plan["core_kinds"], ["cluster_1", "cluster_3"],
                         "the actual reference group is added without substituting legacy 'big'")
        self.assertEqual(plan["reference_core_kind"], "cluster_3")
        self.assertEqual(plan["cpu_map"], {"cluster_0": [0, 1], "cluster_1": [2, 3],
                                           "cluster_2": [4, 5], "cluster_3": [6]})
        self.assertEqual(plan["frequency_policy_ids"], {"cluster_0": "cpu0", "cluster_1": "cpu2",
                                                        "cluster_2": "cpu4", "cluster_3": "cpu6"})
        self.assertEqual({row["core_kind"] for row in plan["conditions"]}, {"cluster_3"})
        self.assertEqual(plan["frequencies_khz"]["cluster_3"], [3000000])
        fitted = self.fit(plan, build_measurements(plan))
        self.assertEqual(fitted["status"], "passed", fitted["quality"])
        self.assertEqual(set(fitted["model_preview"]["profiles"]),
                         {"cluster_0", "cluster_1", "cluster_2", "cluster_3"})
        version = self.calibration.publish({"fit_id": fitted["id"]})
        self.assertEqual(version["hardware_profile_id"], profile["id"])
        self.assertEqual(version["hardware_profile_sha256"], profile["profile_sha256"])
        self.assertEqual(version["model"]["hardware_profile_sha256"], profile["profile_sha256"])
        self.assertEqual(version["model"]["profiles"]["cluster_3"]["opps_khz"], [900000, 2100000, 3000000])


if __name__ == "__main__": unittest.main()
