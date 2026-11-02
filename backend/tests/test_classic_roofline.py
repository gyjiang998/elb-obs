from __future__ import annotations

import unittest
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

from backend.parallel_modeling.classic_roofline import (
    KERNEL_VERSION, attainable_performance, build_plan, fit,
    measurement_contract,
)


def sample(role: str, *, frequency: int = 2_000_000, repeat: int = 0, **extra):
    basis = {
        "compute_ceiling": "algorithmic_flop",
        "bandwidth_ceiling": "minimum_source_bytes",
        "intensity_ladder": "algorithmic_flop_per_minimum_source_byte",
        "instruction_proxy": "integer_mix_round_proxy",
    }[role]
    operations = {"compute_ceiling": 32, "bandwidth_ceiling": 2,
                  "intensity_ladder": 32, "instruction_proxy": 8}[role]
    work_units = 100_000 if role != "compute_ceiling" else 31_250
    supplied_bytes = extra.get("algorithmic_bytes_minimum")
    if isinstance(supplied_bytes, int) and supplied_bytes > 0 and supplied_bytes % 24 == 0:
        work_units = supplied_bytes // 24
    supplied_flops = extra.get("algorithmic_flops")
    if role == "compute_ceiling" and isinstance(supplied_flops, int) and supplied_flops % 32 == 0:
        work_units = supplied_flops // 32
    elif role == "intensity_ladder" and isinstance(supplied_flops, int):
        inferred = supplied_flops // work_units
        if supplied_flops == inferred * work_units and 2 <= inferred <= 4096 and inferred % 2 == 0:
            operations = inferred
    supplied_rounds = extra.get("integer_mix_rounds")
    if role == "instruction_proxy" and isinstance(supplied_rounds, int):
        inferred = supplied_rounds // work_units
        if supplied_rounds == inferred * work_units and 2 <= inferred <= 4096 and inferred % 2 == 0:
            operations = inferred
    working_set_bytes = 1_048_576
    elements = 0 if role == "compute_ceiling" else working_set_bytes // 24
    flops = (work_units * (32 if role == "compute_ceiling" else operations)
             if role != "instruction_proxy" else None)
    source_bytes = work_units * 24 if role != "compute_ceiling" else None
    base = {
        "schema_version": "parallel_calibration_measurement/v1",
        "benchmark": "roofline", "workload_class": role, "core_kind": "middle",
        "frequency_khz": frequency, "wall_ms": 10.0 + repeat,
        "affinity_verified": True, "frequency_verified": True,
        "thermal_throttled": False, "metric_basis": basis,
        "working_set_coverage_verified": True,
        "roofline_kernel_version": KERNEL_VERSION,
        "roofline_kernel_execution": {
            "compute_ceiling": "fp64_16_independent_recurrences",
            "bandwidth_ceiling": "blocked_fp64_16way_ilp",
            "intensity_ladder": "blocked_fp64_16way_ilp",
            "instruction_proxy": "integer_mix_proxy",
        }[role],
        "roofline_ilp_lanes": 16 if role != "instruction_proxy" else 1,
        "roofline_generic_even_operations_supported": True,
        "roofline_quality_gate_required": True,
        "target_cpu_busy_fraction": .99,
        "work_units": work_units,
        "roofline_operations_per_element": operations,
        "working_set_bytes": working_set_bytes,
        "working_set_element_count": elements,
        "working_set_bytes_effective": elements * 24,
        "working_set_coverage_verified": role == "compute_ceiling" or work_units >= elements,
        "full_working_set_passes": None if role == "compute_ceiling" else work_units / elements,
        "algorithmic_flops": flops,
        "algorithmic_bytes_minimum": source_bytes,
        "integer_mix_rounds": work_units * operations if role == "instruction_proxy" else None,
        "operational_intensity_flop_per_byte": (
            flops / source_bytes if flops is not None and source_bytes else None),
    }
    base.update(extra)
    if base.get("algorithmic_flops") and base.get("attainable_gflops"):
        base["wall_ms"] = base["algorithmic_flops"] / (base["attainable_gflops"] * 1e6)
    elif base.get("algorithmic_bytes_minimum") and base.get("source_level_gbytes_per_s"):
        base["wall_ms"] = base["algorithmic_bytes_minimum"] / (base["source_level_gbytes_per_s"] * 1e6)
    if base.get("algorithmic_flops"):
        base["attainable_gflops"] = base["algorithmic_flops"] / (base["wall_ms"] * 1e6)
    else:
        base["attainable_gflops"] = None
    if base.get("algorithmic_bytes_minimum"):
        base["source_level_gbytes_per_s"] = (
            base["algorithmic_bytes_minimum"] / (base["wall_ms"] * 1e6))
    else:
        base["source_level_gbytes_per_s"] = None
    return base


def valid_roof_rows(*, frequency: int = 2_000_000, peak: float = 100.0,
                    bandwidth: float = 20.0, include_proxy: bool = False):
    rows = []
    work_units = 100_000
    source_bytes = work_units * 24
    operations_values = (2, 8, 32, 128, 512)
    for repeat in range(3):
        rows.extend((
            sample("compute_ceiling", frequency=frequency, repeat=repeat,
                   algorithmic_flops=1_000_000, attainable_gflops=peak),
            sample("bandwidth_ceiling", frequency=frequency, repeat=repeat,
                   algorithmic_bytes_minimum=source_bytes,
                   source_level_gbytes_per_s=bandwidth),
        ))
        if include_proxy:
            rows.append(sample(
                "instruction_proxy", frequency=frequency, repeat=repeat,
                algorithmic_bytes_minimum=source_bytes,
                integer_mix_rounds=work_units * 8,
            ))
    for repeat in range(2):
        for operations in operations_values:
            intensity = operations / 24
            rows.append(sample(
                "intensity_ladder", frequency=frequency, repeat=repeat,
                algorithmic_flops=work_units * operations,
                algorithmic_bytes_minimum=source_bytes,
                operational_intensity_flop_per_byte=intensity,
                attainable_gflops=min(peak, bandwidth * intensity),
            ))
    return rows


class ClassicRooflineTests(unittest.TestCase):
    def test_portable_binary_reports_auditable_counts_and_warmup(self):
        compiler = shutil.which("cc")
        if not compiler:
            self.skipTest("C compiler is unavailable")
        source = Path(__file__).resolve().parents[2] / "tools/parallel_calibration/benchmark.c"
        with tempfile.TemporaryDirectory() as temporary:
            binary = Path(temporary) / "benchmark"
            subprocess.run([compiler, "-O2", "-std=c11", "-pthread",
                            "-DPARALLEL_CALIBRATION_PORTABLE_SMOKE_TEST",
                            str(source), "-o", str(binary)],
                           check=True, capture_output=True, text=True)
            common = [str(binary), "--benchmark", "roofline", "--family", "roofline",
                      "--cpu", "0", "--observer-cpu", "1", "--frequency-khz", "1000000",
                      "--work-units", "100000", "--active-tasks", "1", "--sample-period-us", "1000",
                      "--frequency-tolerance", "0.2", "--working-set-bytes", "1048576"]
            compute = json.loads(subprocess.run(
                [*common, "--workload-class", "compute_ceiling",
                 "--roofline-operations-per-element", "32"],
                check=True, capture_output=True, text=True).stdout)
            self.assertEqual(compute["algorithmic_flops"], 3_200_000)
            self.assertEqual(compute["roofline_kernel_version"], KERNEL_VERSION)
            self.assertEqual(compute["metric_basis"], "algorithmic_flop")
            self.assertEqual(compute["warmup_frequency_sample_count"], 12)
            self.assertEqual(compute["warmup_work_units"], 12 * 32_768)
            ladder = json.loads(subprocess.run(
                [*common, "--workload-class", "intensity_ladder",
                 "--roofline-operations-per-element", "8"],
                check=True, capture_output=True, text=True).stdout)
            self.assertEqual(ladder["algorithmic_flops"], 800_000)
            self.assertEqual(ladder["algorithmic_bytes_minimum"], 2_400_000)
            self.assertAlmostEqual(ladder["operational_intensity_flop_per_byte"], 1 / 3, places=10)
            self.assertEqual(ladder["roofline_kernel_execution"],
                             "blocked_fp64_16way_ilp")
            self.assertEqual(ladder["roofline_ilp_lanes"], 16)
            self.assertEqual(ladder["warmup_frequency_sample_count"], 12)
            self.assertEqual(ladder["warmup_work_units"], 12 * 32_768)
            proxy = json.loads(subprocess.run(
                [*common, "--workload-class", "instruction_proxy",
                 "--roofline-operations-per-element", "8"],
                check=True, capture_output=True, text=True).stdout)
            self.assertIsNone(proxy["algorithmic_flops"])
            self.assertEqual(proxy["integer_mix_rounds"], 800_000)
            self.assertIn("not retired instructions", proxy["count_semantics"])

            def pair_measurement(work_units):
                return json.loads(subprocess.run([
                    str(binary), "--benchmark", "pair_work", "--family", "build",
                    "--cpu", "0", "--observer-cpu", "1", "--frequency-khz", "1000000",
                    "--work-units", str(work_units), "--active-tasks", "1",
                    "--sample-period-us", "1000", "--frequency-tolerance", "0.2",
                    "--working-set-bytes", "1048576", "--workload-class", "compute",
                    "--peer-workload-class", "none",
                ], check=True, capture_output=True, text=True).stdout)

            self.assertEqual(pair_measurement(1_000)["warmup_work_units"], 12 * 1_000)
            portable_pmu = pair_measurement(100_000)
            self.assertEqual(portable_pmu["warmup_work_units"], 12 * 32_768)
            self.assertEqual(portable_pmu["pmu_collection_status"], "unavailable")
            self.assertFalse(portable_pmu["pmu_window_aligned"])
            self.assertEqual(
                portable_pmu["pmu_window_relation"],
                "perf_group_encloses_timed_workload_window; timed wall/cpu exclude "
                "perf control/read overhead",
            )
            self.assertEqual(len(portable_pmu["pmu_counters"]), 4)
            self.assertIn("PERF_IOC_FLAG_GROUP", portable_pmu["pmu_counting_semantics"])
            source_text = source.read_text(encoding="utf-8")
            self.assertIn('printf("%.17g", counter->scaled_value)', source_text)
            begin = source_text.index("pmu_begin(&worker->pmu);")
            start_wall = source_text.index("worker->start_ns = now_ns(CLOCK_MONOTONIC);", begin)
            end_wall = source_text.index("worker->end_ns = now_ns(CLOCK_MONOTONIC);", start_wall)
            end_pmu = source_text.index("pmu_end(&worker->pmu);", end_wall)
            self.assertLess(begin, start_wall)
            self.assertLess(end_wall, end_pmu)
            self.assertIn("worker->cpu_ms = (end_cpu - start_cpu) / 1e6;", source_text)

            proxy_common = [str(binary), "--cpu", "0", "--observer-cpu", "1",
                            "--frequency-khz", "1000000", "--active-tasks", "1",
                            "--sample-period-us", "1000", "--frequency-tolerance", "0.2"]
            warmup_cases = (
                ("cpu_work", 7, 12 * 7,
                 ["--family", "residual"]),
                ("cpu_work", 32769, 12 * 32768,
                 ["--family", "residual"]),
                ("pair_work", 7, 12 * 7,
                 ["--family", "build", "--workload-class", "compute",
                  "--peer-workload-class", "none", "--working-set-bytes", "1048576"]),
                ("pair_work", 32769, 12 * 32768,
                 ["--family", "build", "--workload-class", "memory",
                  "--peer-workload-class", "none", "--working-set-bytes", "1048576"]),
            )
            for benchmark, work_units, expected_warmup, extra in warmup_cases:
                with self.subTest(benchmark=benchmark, work_units=work_units):
                    measured = json.loads(subprocess.run(
                        [*proxy_common, "--benchmark", benchmark,
                         "--work-units", str(work_units), *extra],
                        check=True, capture_output=True, text=True).stdout)
                    self.assertEqual(measured["workload_version"],
                                     "parallel-calibration-proxy-v6")
                    self.assertEqual(measured["work_units"], work_units)
                    self.assertEqual(measured["warmup_work_units"], expected_warmup)

    def test_plan_covers_every_core_group_and_opp_with_intensity_ladder(self):
        plan = build_plan({"id": "phone", "core_groups": [
            {"id": "middle", "cpu": 4, "observer_cpu": 0,
             "opps_khz": [1_000_000, 2_000_000], "working_set_bytes": 24_000_000},
        ]}, intensity_operations=(2, 16, 64))
        self.assertEqual(plan["schema_version"], "classic_roofline_plan/v1")
        self.assertEqual(len(plan["conditions"]), 12)
        self.assertEqual({row["frequency_khz"] for row in plan["conditions"]}, {1_000_000, 2_000_000})
        self.assertEqual({row["workload_class"] for row in plan["conditions"]},
                         {"compute_ceiling", "bandwidth_ceiling", "intensity_ladder", "instruction_proxy"})

        default_plan = build_plan({"id": "phone", "core_groups": [
            {"id": "middle", "cpu": 4, "observer_cpu": 0,
             "opps_khz": [2_000_000], "working_set_bytes": 24_000_000},
        ]})
        self.assertEqual(
            {row["roofline_operations_per_element"] for row in default_plan["conditions"]
             if row["workload_class"] == "intensity_ladder"},
            {2, 8, 32, 128, 512},
        )

    def test_classic_equation_and_frequency_specific_knee(self):
        self.assertEqual(attainable_performance(2, 100, 20), 40)
        self.assertEqual(attainable_performance(20, 100, 20), 100)
        rows = []
        for frequency, peak in ((1_000_000, 50.0), (2_000_000, 100.0)):
            rows.extend(valid_roof_rows(frequency=frequency, peak=peak, bandwidth=20.0))
            for repeat in range(2):
                rows.append(sample(
                    "intensity_ladder", frequency=frequency, repeat=repeat,
                    algorithmic_flops=9_600_000, algorithmic_bytes_minimum=2_400_000,
                    operational_intensity_flop_per_byte=4.0,
                    attainable_gflops=min(peak, 80.0),
                ))
        result = fit(rows)
        self.assertEqual([roof["knee_flop_per_byte"] for roof in result["roofs"]], [2.5, 5.0])
        generic = [[point for point in roof["intensity_points"]
                    if point["operational_intensity_flop_per_byte"] == 4.0][0]
                   for roof in result["roofs"]]
        self.assertEqual([point["roof_gflops"] for point in generic], [50.0, 80.0])

    def test_integer_proxy_is_not_mislabeled_as_instruction_roofline(self):
        rows = valid_roof_rows(include_proxy=True)
        roof = fit(rows)["roofs"][0]
        self.assertEqual(roof["instruction_roofline"]["status"], "needs_aligned_pmu_counters")
        contract = measurement_contract()
        self.assertIn("pmu_retired_instructions",
                      contract["roles"]["instruction_proxy"]["publishable_instruction_roofline_requires"])
        for row in rows:
            if row["workload_class"] == "instruction_proxy":
                row.update(pmu_window_aligned=True, pmu_retired_instructions=5_000_000,
                           pmu_dram_bytes=2_000_000)
        roof = fit(rows)["roofs"][0]
        self.assertEqual(roof["instruction_roofline"]["status"], "publishable")

    def test_intensity_summary_keeps_per_metric_pmu_coverage(self):
        rows = valid_roof_rows()
        target = [row for row in rows if row["workload_class"] == "intensity_ladder"
                  and row["roofline_operations_per_element"] == 32]
        target[0].update(pmu_ipc=1.5, pmu_cache_misses=10,
                         pmu_cache_references=40, pmu_cache_mpki=2.0)
        target[1].update(pmu_ipc=1.7)
        summary = next(point for point in fit(rows)["roofs"][0]["intensity_summary"]
                       if abs(point["operational_intensity_flop_per_byte"] - 32 / 24) < 1e-12)
        self.assertEqual(summary["pmu"]["status"], "partial")
        self.assertEqual(summary["pmu"]["ipc_sample_count"], 2)
        self.assertEqual(summary["pmu"]["cache_miss_sample_count"], 1)
        self.assertEqual(summary["pmu"]["cache_reference_sample_count"], 1)
        self.assertEqual(summary["pmu"]["cache_mpki_sample_count"], 1)
        self.assertEqual(summary["pmu"]["complete_sample_count"], 1)

    def test_rejects_unverified_or_single_repeat_roofs(self):
        rows = valid_roof_rows()
        first_compute = next(row for row in rows if row["workload_class"] == "compute_ceiling")
        first_bandwidth = next(row for row in rows if row["workload_class"] == "bandwidth_ceiling")
        rows = [row for row in rows if row["workload_class"] == "intensity_ladder"]
        rows.extend((first_compute, first_bandwidth))
        fitted = fit(rows)
        gates = fitted["quality"]["failed_gates"]
        self.assertTrue(any(gate["id"] == "minimum_repeat_count" for gate in gates))
        self.assertTrue(fitted["quality"]["publishable_with_risk"])
        rows[0]["frequency_verified"] = False
        with self.assertRaisesRegex(ValueError, "affinity and frequency"):
            fit(rows)

    def test_rejects_performance_fields_inconsistent_with_counts_and_time(self):
        row = sample("compute_ceiling", algorithmic_flops=1_000_000,
                     attainable_gflops=100.0)
        row["attainable_gflops"] *= 2
        with self.assertRaisesRegex(ValueError, "inconsistent"):
            fit([row])

    def test_rejects_collapsing_or_above_ceiling_intensity_ladder(self):
        rows = valid_roof_rows()
        for row in rows:
            if (row["workload_class"] == "intensity_ladder"
                    and row["roofline_operations_per_element"] == 512):
                row["attainable_gflops"] = 30.0
                row["wall_ms"] = row["algorithmic_flops"] / (30.0 * 1e6)
                row["source_level_gbytes_per_s"] = (
                    row["algorithmic_bytes_minimum"] / (row["wall_ms"] * 1e6))
        fitted = fit(rows)
        self.assertIn("maximum_intensity_throughput_drop",
                      {gate["id"] for gate in fitted["quality"]["failed_gates"]})

        rows = valid_roof_rows()
        for row in rows:
            if (row["workload_class"] == "intensity_ladder"
                    and row["roofline_operations_per_element"] == 32):
                row["attainable_gflops"] = 40.0
                row["wall_ms"] = row["algorithmic_flops"] / (40.0 * 1e6)
                row["source_level_gbytes_per_s"] = (
                    row["algorithmic_bytes_minimum"] / (row["wall_ms"] * 1e6))
        fitted = fit(rows)
        self.assertIn("maximum_ceiling_violation",
                      {gate["id"] for gate in fitted["quality"]["failed_gates"]})

    def test_rejects_missing_knee_span_low_utilization_and_repeat_dispersion(self):
        rows = [row for row in valid_roof_rows()
                if row.get("roofline_operations_per_element") != 512]
        fitted = fit(rows)
        self.assertIn("minimum_intensity_point_count",
                      {gate["id"] for gate in fitted["quality"]["failed_gates"]})

        rows = valid_roof_rows(peak=100.0, bandwidth=1.0)
        fitted = fit(rows)
        self.assertIn("knee_span", {gate["id"] for gate in fitted["quality"]["failed_gates"]})

        rows = valid_roof_rows()
        for row in rows:
            if row["workload_class"] == "intensity_ladder":
                value = min(row["attainable_gflops"], 20.0)
                row["attainable_gflops"] = value
                row["wall_ms"] = row["algorithmic_flops"] / (value * 1e6)
                row["source_level_gbytes_per_s"] = (
                    row["algorithmic_bytes_minimum"] / (row["wall_ms"] * 1e6))
        fitted = fit(rows)
        self.assertIn("minimum_high_intensity_compute_utilization",
                      {gate["id"] for gate in fitted["quality"]["failed_gates"]})

        rows = valid_roof_rows()
        compute = [row for row in rows if row["workload_class"] == "compute_ceiling"][-1]
        compute["attainable_gflops"] = 120.0
        compute["wall_ms"] = compute["algorithmic_flops"] / (120.0 * 1e6)
        fitted = fit(rows)
        self.assertIn("maximum_repeat_cv",
                      {gate["id"] for gate in fitted["quality"]["failed_gates"]})

    def test_recomputes_source_counts_and_coverage_from_work_units(self):
        row = sample("intensity_ladder", algorithmic_flops=3_200_000,
                     algorithmic_bytes_minimum=2_400_000,
                     attainable_gflops=20.0)
        row["algorithmic_flops"] += 2
        with self.assertRaisesRegex(ValueError, "frozen source count"):
            fit([row])

        row = sample("bandwidth_ceiling", algorithmic_bytes_minimum=2_400_000,
                     source_level_gbytes_per_s=20.0)
        row["working_set_coverage_verified"] = False
        with self.assertRaisesRegex(ValueError, "work_units/effective"):
            fit([row])


if __name__ == "__main__":
    unittest.main()
