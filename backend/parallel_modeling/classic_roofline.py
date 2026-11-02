"""Auditable classic Roofline calibration helpers.

This module deliberately stays separate from ``frequency_roofline``.  Classic
Roofline uses operational intensity on the x axis and attainable throughput on
the y axis.  The frequency response model uses frequency on the x axis and
latency inflation on the y axis; they may share measurements, but they are not
the same model.

The portable benchmark reports *algorithmic* FLOPs and minimum source-level
bytes.  Those quantities are reproducible from its source, but are not hardware
retired instructions or memory-controller DRAM traffic.  An Instruction
Roofline is therefore publishable only when an external PMU sampler supplies
both retired-instruction and DRAM-byte evidence for the same timed window.
"""
from __future__ import annotations

import math
import statistics
from collections import defaultdict
from typing import Any, Iterable


SCHEMA = "classic_roofline/v1"
BENCHMARK_SCHEMA = "parallel_calibration_measurement/v1"
KERNEL_VERSION = "classic-roofline-blocked-v2"
ROLES = ("compute_ceiling", "bandwidth_ceiling", "intensity_ladder", "instruction_proxy")
MIN_STREAMED_BYTES = 1024 * 1024 * 1024
MINIMUM_TARGET_CPU_BUSY_FRACTION = .95
MAXIMUM_CEILING_VIOLATION = .10
MAXIMUM_INTENSITY_THROUGHPUT_DROP = .10
MAXIMUM_BANDWIDTH_CROSSCHECK_ERROR = .10
MAXIMUM_CEILING_REPEAT_CV = .05
MAXIMUM_LADDER_REPEAT_CV = .08
MINIMUM_INTENSITY_POINT_COUNT = 5
MINIMUM_KNEE_SPAN_FACTOR = 2.0
MINIMUM_HIGH_INTENSITY_COMPUTE_UTILIZATION = .75
COMPUTE_CEILING_WORK_UNITS = 64_000_000


def measurement_contract() -> dict[str, Any]:
    return {
        "schema_version": "classic_roofline_measurement_contract/v1",
        "benchmark": "roofline",
        "required_common": [
            "schema_version", "benchmark", "workload_class", "core_kind",
            "frequency_khz", "wall_ms", "affinity_verified",
            "frequency_verified", "thermal_throttled",
            "roofline_kernel_version", "roofline_kernel_execution",
            "roofline_ilp_lanes", "roofline_generic_even_operations_supported",
            "roofline_quality_gate_required", "work_units",
            "roofline_operations_per_element", "working_set_bytes",
            "working_set_bytes_effective", "working_set_element_count",
            "full_working_set_passes", "working_set_coverage_verified",
            "target_cpu_busy_fraction",
        ],
        "roles": {
            "compute_ceiling": {
                "metric_basis": "algorithmic_flop",
                "required": ["algorithmic_flops", "attainable_gflops"],
            },
            "bandwidth_ceiling": {
                "metric_basis": "minimum_source_bytes",
                "required": ["algorithmic_flops", "attainable_gflops",
                             "algorithmic_bytes_minimum", "source_level_gbytes_per_s",
                             "working_set_coverage_verified"],
            },
            "intensity_ladder": {
                "metric_basis": "algorithmic_flop_per_minimum_source_byte",
                "required": [
                    "algorithmic_flops", "algorithmic_bytes_minimum",
                    "operational_intensity_flop_per_byte", "attainable_gflops",
                    "source_level_gbytes_per_s", "working_set_coverage_verified",
                ],
            },
            "instruction_proxy": {
                "metric_basis": "integer_mix_round_proxy",
                "required": ["algorithmic_bytes_minimum", "source_level_gbytes_per_s",
                             "integer_mix_rounds",
                             "working_set_coverage_verified"],
                "publishable_instruction_roofline_requires": [
                    "pmu_retired_instructions", "pmu_dram_bytes",
                    "pmu_window_aligned",
                ],
            },
        },
        "semantics": {
            "algorithmic_flops": "source-level floating operations; not retired FP instructions",
            "algorithmic_bytes_minimum": "two source reads plus one source write; cache/write-allocate traffic excluded",
            "pmu_retired_instructions": "hardware counter over the benchmark timed window",
            "pmu_dram_bytes": "memory-controller or verified uncore traffic over the same timed window",
            "roofline_kernel_version": KERNEL_VERSION,
            "quality_gate": {
                "maximum_ceiling_violation": MAXIMUM_CEILING_VIOLATION,
                "maximum_intensity_throughput_drop": MAXIMUM_INTENSITY_THROUGHPUT_DROP,
                "maximum_bandwidth_crosscheck_error": MAXIMUM_BANDWIDTH_CROSSCHECK_ERROR,
                "maximum_ceiling_repeat_cv": MAXIMUM_CEILING_REPEAT_CV,
                "maximum_ladder_repeat_cv": MAXIMUM_LADDER_REPEAT_CV,
                "minimum_intensity_point_count": MINIMUM_INTENSITY_POINT_COUNT,
                "minimum_knee_span_factor": MINIMUM_KNEE_SPAN_FACTOR,
                "minimum_high_intensity_compute_utilization": (
                    MINIMUM_HIGH_INTENSITY_COMPUTE_UTILIZATION),
                "minimum_target_cpu_busy_fraction": MINIMUM_TARGET_CPU_BUSY_FRACTION,
            },
        },
    }


def build_plan(hardware_profile: dict[str, Any], *, repeats: int = 3,
               intensity_operations: Iterable[int] = (2, 8, 32, 128, 512)) -> dict[str, Any]:
    """Build a transport-neutral per-core-group, per-OPP Roofline plan.

    ``hardware_profile`` intentionally uses a small contract so Desktop device
    discovery can produce it without importing the full modeling manager:
    ``{"id": ..., "core_groups": [{"id", "cpu", "observer_cpu",
    "opps_khz", "working_set_bytes"}]}``.
    """
    if not isinstance(repeats, int) or repeats < 3:
        raise ValueError("Roofline calibration requires at least three independent ceiling repeats")
    operations = tuple(dict.fromkeys(int(value) for value in intensity_operations))
    if not operations or any(value < 2 or value > 4096 or value % 2 for value in operations):
        raise ValueError("intensity operations must be unique even integers in [2, 4096]")
    groups = hardware_profile.get("core_groups")
    if not isinstance(groups, list) or not groups:
        raise ValueError("hardware profile must contain core_groups")
    conditions: list[dict[str, Any]] = []
    for group in groups:
        group_id = str(group.get("id") or "").strip()
        cpu, observer = group.get("cpu"), group.get("observer_cpu")
        opps = sorted({int(value) for value in group.get("opps_khz", []) if int(value) > 0})
        working_set = int(group.get("working_set_bytes") or 64 * 1024 * 1024)
        if not group_id or not isinstance(cpu, int) or not isinstance(observer, int) or cpu == observer or not opps:
            raise ValueError(f"invalid Roofline core group: {group!r}")
        for frequency in opps:
            memory_work_units = max(1, working_set // 24) * 4
            memory_work_units = max(memory_work_units, (MIN_STREAMED_BYTES + 23) // 24)
            base = {
                "benchmark": "roofline", "family": "roofline", "active_tasks": 1,
                "core_kind": group_id, "cpu": cpu,
                "observer_cpu": observer, "frequency_khz": frequency,
                "working_set_bytes": working_set, "repeats": repeats,
                "roofline_kernel_version": KERNEL_VERSION,
            }
            conditions.extend((
                {**base, "workload_class": "compute_ceiling",
                 "work_units": COMPUTE_CEILING_WORK_UNITS,
                 "roofline_operations_per_element": 32},
                {**base, "workload_class": "bandwidth_ceiling",
                 "work_units": memory_work_units,
                 "roofline_operations_per_element": 2},
                {**base, "workload_class": "instruction_proxy",
                 "work_units": memory_work_units,
                 "roofline_operations_per_element": 8,
                 "requires_aligned_pmu": ["retired_instructions", "dram_bytes"]},
            ))
            for value in operations:
                conditions.append({**base, "workload_class": "intensity_ladder",
                                   "work_units": memory_work_units,
                                   "roofline_operations_per_element": value})
    return {
        "schema_version": "classic_roofline_plan/v1",
        "hardware_profile_id": hardware_profile.get("id"),
        "conditions": conditions,
        "measurement_contract": measurement_contract(),
    }


def _finite_positive(row: dict[str, Any], key: str) -> float:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{key} must be numeric")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{key} must be finite and positive")
    return value


def _positive_integer(row: dict[str, Any], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{key} must be a positive integer")
    return value


def _nonnegative_integer(row: dict[str, Any], key: str) -> int:
    value = row.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{key} must be a non-negative integer")
    return value


def _require_integer_count(row: dict[str, Any], key: str, expected: int) -> int:
    value = _nonnegative_integer(row, key)
    if value != expected:
        raise ValueError(f"{key} does not match the frozen source count")
    return value


def _repeat_cv(values: Iterable[float], label: str, *, minimum_repeats: int,
               maximum_cv: float, failed_gates: list[dict[str, Any]]) -> float:
    values = [float(value) for value in values]
    if not values:
        raise ValueError(f"{label} has no usable measurement")
    if len(values) < minimum_repeats:
        failed_gates.append({
            "id": "minimum_repeat_count", "scope": label,
            "observed": len(values), "threshold": minimum_repeats,
            "message": f"{label} 只有 {len(values)} 次有效重复，要求至少 {minimum_repeats} 次。",
        })
    mean = statistics.mean(values)
    if not math.isfinite(mean) or mean <= 0:
        raise ValueError(f"{label} repeat mean must be finite and positive")
    result = statistics.stdev(values) / mean if len(values) > 1 else 0.0
    if result > maximum_cv:
        failed_gates.append({
            "id": "maximum_repeat_cv", "scope": label,
            "observed": result, "threshold": maximum_cv,
            "message": f"{label} 重复 CV {result:.1%} 超过 {maximum_cv:.0%}。",
        })
    return result


def _require_close(observed: float, expected: float, label: str, *, relative_tolerance: float = 1e-6) -> None:
    scale = max(abs(observed), abs(expected), 1e-12)
    if abs(observed - expected) / scale > relative_tolerance:
        raise ValueError(f"{label} is inconsistent with source counts and wall_ms")


def validate_measurement(row: dict[str, Any]) -> None:
    if row.get("schema_version") != BENCHMARK_SCHEMA or row.get("benchmark") != "roofline":
        raise ValueError("not a portable Roofline benchmark measurement")
    role = row.get("workload_class")
    if role not in ROLES:
        raise ValueError(f"unsupported Roofline role: {role!r}")
    if not str(row.get("core_kind") or "").strip():
        raise ValueError("core_kind is required")
    if row.get("roofline_kernel_version") != KERNEL_VERSION:
        raise ValueError(f"Roofline measurements require {KERNEL_VERSION}")
    expected_execution = {
        "compute_ceiling": ("fp64_16_independent_recurrences", 16),
        "bandwidth_ceiling": ("blocked_fp64_16way_ilp", 16),
        "intensity_ladder": ("blocked_fp64_16way_ilp", 16),
        "instruction_proxy": ("integer_mix_proxy", 1),
    }[role]
    if (row.get("roofline_kernel_execution"), row.get("roofline_ilp_lanes")) != expected_execution:
        raise ValueError("Roofline kernel execution/ILP evidence does not match its role")
    if (row.get("roofline_generic_even_operations_supported") is not True
            or row.get("roofline_quality_gate_required") is not True):
        raise ValueError("Roofline kernel lacks the v2 generic-intensity quality contract")
    work_units = _positive_integer(row, "work_units")
    operations = _positive_integer(row, "roofline_operations_per_element")
    if operations < 2 or operations > 4096 or operations % 2:
        raise ValueError("roofline_operations_per_element must be an even integer in [2, 4096]")
    if role == "compute_ceiling" and operations != 32:
        raise ValueError("compute_ceiling must use 32 algorithmic FLOPs per work unit")
    if role == "bandwidth_ceiling" and operations != 2:
        raise ValueError("bandwidth_ceiling must use 2 algorithmic FLOPs per element")
    working_set_bytes = _positive_integer(row, "working_set_bytes")
    expected_elements = 0 if role == "compute_ceiling" else working_set_bytes // 24
    if role != "compute_ceiling" and expected_elements <= 0:
        raise ValueError("memory Roofline working set must contain at least one element")
    elements = _nonnegative_integer(row, "working_set_element_count")
    if elements != expected_elements:
        raise ValueError("working_set_element_count does not match working_set_bytes")
    _require_integer_count(row, "working_set_bytes_effective", expected_elements * 24)
    expected_coverage = role == "compute_ceiling" or work_units >= expected_elements
    if row.get("working_set_coverage_verified") is not expected_coverage:
        raise ValueError("working_set_coverage_verified does not match work_units/effective working set")
    passes = row.get("full_working_set_passes")
    if role == "compute_ceiling":
        if passes is not None:
            raise ValueError("compute_ceiling cannot claim memory working-set passes")
    else:
        passes = _finite_positive(row, "full_working_set_passes")
        _require_close(passes, work_units / expected_elements, "full_working_set_passes")
    _finite_positive(row, "frequency_khz")
    _finite_positive(row, "wall_ms")
    target_busy = _finite_positive(row, "target_cpu_busy_fraction")
    if target_busy > 1 or target_busy < MINIMUM_TARGET_CPU_BUSY_FRACTION:
        raise ValueError(
            f"target_cpu_busy_fraction must be in "
            f"[{MINIMUM_TARGET_CPU_BUSY_FRACTION:.2f}, 1]")
    if row.get("affinity_verified") is not True or row.get("frequency_verified") is not True:
        raise ValueError("affinity and frequency must be verified")
    if row.get("thermal_throttled") is not False:
        raise ValueError("thermal state must be measured and non-throttled")
    expected_basis = {
        "compute_ceiling": "algorithmic_flop",
        "bandwidth_ceiling": "minimum_source_bytes",
        "intensity_ladder": "algorithmic_flop_per_minimum_source_byte",
        "instruction_proxy": "integer_mix_round_proxy",
    }[role]
    if row.get("metric_basis") != expected_basis:
        raise ValueError(f"{role} must declare metric_basis={expected_basis}")
    if role != "compute_ceiling" and row.get("working_set_coverage_verified") is not True:
        raise ValueError("memory Roofline samples must cover the complete working set")
    if role in ("compute_ceiling", "bandwidth_ceiling", "intensity_ladder"):
        expected_flops = work_units * (32 if role == "compute_ceiling" else operations)
        flops = _require_integer_count(row, "algorithmic_flops", expected_flops)
        gflops = _finite_positive(row, "attainable_gflops")
        _require_close(gflops, flops / (_finite_positive(row, "wall_ms") * 1e6),
                       "attainable_gflops")
    if role in ("bandwidth_ceiling", "intensity_ladder", "instruction_proxy"):
        source_bytes = _require_integer_count(
            row, "algorithmic_bytes_minimum", work_units * 24)
        source_bandwidth = _finite_positive(row, "source_level_gbytes_per_s")
        _require_close(source_bandwidth,
                       source_bytes / (_finite_positive(row, "wall_ms") * 1e6),
                       "source_level_gbytes_per_s")
    if role == "intensity_ladder":
        intensity = _finite_positive(row, "operational_intensity_flop_per_byte")
        _require_close(intensity, flops / source_bytes, "operational_intensity_flop_per_byte")
    if role == "instruction_proxy":
        _require_integer_count(row, "integer_mix_rounds", work_units * operations)
        if row.get("algorithmic_flops") is not None:
            raise ValueError("instruction_proxy integer rounds cannot be reported as FLOPs")


def attainable_performance(operational_intensity: float, peak_gflops: float,
                           bandwidth_gbytes_s: float) -> float:
    """Return ``min(P_peak, BW * I)`` in GFLOP/s."""
    if min(operational_intensity, peak_gflops, bandwidth_gbytes_s) < 0:
        raise ValueError("Roofline inputs cannot be negative")
    return min(peak_gflops, bandwidth_gbytes_s * operational_intensity)


def fit(measurements: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Fit classic roofs while retaining failed statistical gates as risk.

    Measurement identity, source counts and numeric integrity remain strict in
    :func:`validate_measurement`. Repeat dispersion, ladder coverage and roof
    consistency are evidence-quality gates: they lower the evidence grade but
    do not discard an otherwise auditable hour-long experiment.
    """
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    for row in measurements:
        validate_measurement(row)
        grouped[(str(row["core_kind"]), int(row["frequency_khz"]))].append(row)
    if not grouped:
        raise ValueError("no Roofline measurements")
    roofs: list[dict[str, Any]] = []
    for (core_kind, frequency), rows in sorted(grouped.items()):
        failed_gates: list[dict[str, Any]] = []
        compute = [float(row["attainable_gflops"]) for row in rows
                   if row["workload_class"] == "compute_ceiling"]
        bandwidth = [float(row["source_level_gbytes_per_s"]) for row in rows
                     if row["workload_class"] == "bandwidth_ceiling"]
        compute_cv = _repeat_cv(
            compute, f"{core_kind}@{frequency} compute ceiling",
            minimum_repeats=3, maximum_cv=MAXIMUM_CEILING_REPEAT_CV,
            failed_gates=failed_gates)
        bandwidth_cv = _repeat_cv(
            bandwidth, f"{core_kind}@{frequency} bandwidth ceiling",
            minimum_repeats=3, maximum_cv=MAXIMUM_CEILING_REPEAT_CV,
            failed_gates=failed_gates)
        peak, sustainable = statistics.median(compute), statistics.median(bandwidth)
        points = []
        for row in rows:
            if row["workload_class"] == "intensity_ladder":
                intensity = float(row["operational_intensity_flop_per_byte"])
                points.append({
                    "operational_intensity_flop_per_byte": intensity,
                    "measured_gflops": float(row["attainable_gflops"]),
                    "roof_gflops": attainable_performance(intensity, peak, sustainable),
                })
        intensity_summary = []
        by_intensity: dict[float, list[dict[str, float]]] = defaultdict(list)
        for point in points:
            by_intensity[point["operational_intensity_flop_per_byte"]].append(point)
        if not by_intensity:
            raise ValueError(f"{core_kind}@{frequency} has no usable intensity-ladder measurement")
        if len(by_intensity) < MINIMUM_INTENSITY_POINT_COUNT:
            failed_gates.append({
                "id": "minimum_intensity_point_count", "scope": f"{core_kind}@{frequency}",
                "observed": len(by_intensity), "threshold": MINIMUM_INTENSITY_POINT_COUNT,
                "message": (f"{core_kind}@{frequency} 只有 {len(by_intensity)} 个计算强度点，"
                            f"要求至少 {MINIMUM_INTENSITY_POINT_COUNT} 个。"),
            })
        for intensity, values in sorted(by_intensity.items()):
            measured_values = [value["measured_gflops"] for value in values]
            repeat_cv = _repeat_cv(
                measured_values, f"{core_kind}@{frequency} intensity {intensity:g}",
                minimum_repeats=2, maximum_cv=MAXIMUM_LADDER_REPEAT_CV,
                failed_gates=failed_gates)
            measured = statistics.median(measured_values)
            roof = attainable_performance(intensity, peak, sustainable)
            violation = max(0.0, measured / roof - 1)
            if violation > MAXIMUM_CEILING_VIOLATION:
                failed_gates.append({
                    "id": "maximum_ceiling_violation", "scope": f"{core_kind}@{frequency}/{intensity:g}",
                    "observed": violation, "threshold": MAXIMUM_CEILING_VIOLATION,
                    "message": (f"{core_kind}@{frequency} 强度 {intensity:g} 的实测性能"
                                f"超出屋顶 {violation:.1%}。"),
                })
            source_rows = [row for row in rows
                           if row["workload_class"] == "intensity_ladder"
                           and abs(float(row["operational_intensity_flop_per_byte"]) - intensity) < 1e-12]

            def median_metric(field: str) -> tuple[float | None, int]:
                metric_values = [float(row[field]) for row in source_rows
                                 if isinstance(row.get(field), (int, float))
                                 and not isinstance(row.get(field), bool)
                                 and math.isfinite(float(row[field])) and float(row[field]) >= 0]
                return ((statistics.median(metric_values), len(metric_values))
                        if metric_values else (None, 0))

            ipc, ipc_count = median_metric("pmu_ipc")
            cache_misses, cache_miss_count = median_metric("pmu_cache_misses")
            cache_references, cache_reference_count = median_metric("pmu_cache_references")
            cache_mpki, cache_mpki_count = median_metric("pmu_cache_mpki")
            complete_count = sum(
                all(isinstance(row.get(field), (int, float))
                    and not isinstance(row.get(field), bool)
                    and math.isfinite(float(row[field])) and float(row[field]) >= 0
                    for field in ("pmu_ipc", "pmu_cache_misses",
                                  "pmu_cache_references", "pmu_cache_mpki"))
                for row in source_rows)
            intensity_summary.append({
                "operational_intensity_flop_per_byte": intensity,
                "measured_gflops_median": measured,
                "roof_gflops": roof,
                "ceiling_violation_ratio": violation,
                "repeat_count": len(values),
                "repeat_cv": repeat_cv,
                "pmu": {
                    "status": ("collected" if complete_count == len(source_rows)
                               and source_rows else "partial" if any((ipc_count, cache_miss_count,
                                                                      cache_reference_count, cache_mpki_count))
                               else "unavailable"),
                    "scope": "target_thread_user_only_timed_window",
                    "ipc_median": ipc, "ipc_sample_count": ipc_count,
                    "cache_misses_median": cache_misses,
                    "cache_miss_sample_count": cache_miss_count,
                    "cache_references_median": cache_references,
                    "cache_reference_sample_count": cache_reference_count,
                    "cache_mpki_median": cache_mpki,
                    "cache_mpki_sample_count": cache_mpki_count,
                    "complete_sample_count": complete_count,
                    "unavailable_note": (None if any((ipc_count, cache_miss_count, cache_mpki_count))
                                         else "perf_event_open unsupported, denied, or event unavailable"),
                },
            })
        knee = peak / sustainable
        minimum_intensity = intensity_summary[0]["operational_intensity_flop_per_byte"]
        maximum_intensity = intensity_summary[-1]["operational_intensity_flop_per_byte"]
        if (minimum_intensity > knee / MINIMUM_KNEE_SPAN_FACTOR
                or maximum_intensity < knee * MINIMUM_KNEE_SPAN_FACTOR):
            failed_gates.append({
                "id": "knee_span", "scope": f"{core_kind}@{frequency}",
                "observed": {"minimum": minimum_intensity, "knee": knee,
                             "maximum": maximum_intensity},
                "threshold": MINIMUM_KNEE_SPAN_FACTOR,
                "message": (f"{core_kind}@{frequency} 的强度阶梯未在拐点两侧各覆盖 "
                            f"{MINIMUM_KNEE_SPAN_FACTOR:g} 倍范围。"),
            })
        maximum_drop = 0.0
        for previous, current in zip(intensity_summary, intensity_summary[1:]):
            drop = max(0.0, 1 - current["measured_gflops_median"] /
                       previous["measured_gflops_median"])
            maximum_drop = max(maximum_drop, drop)
            if drop > MAXIMUM_INTENSITY_THROUGHPUT_DROP:
                failed_gates.append({
                    "id": "maximum_intensity_throughput_drop", "scope": f"{core_kind}@{frequency}",
                    "observed": drop, "threshold": MAXIMUM_INTENSITY_THROUGHPUT_DROP,
                    "message": f"{core_kind}@{frequency} 的性能随计算强度增加反而下降 {drop:.1%}。",
                })
        bandwidth_crosscheck = next((
            point for point in intensity_summary
            if abs(point["operational_intensity_flop_per_byte"] - 2 / 24) < 1e-12
        ), None)
        bandwidth_crosscheck_error = None
        if bandwidth_crosscheck is None:
            failed_gates.append({
                "id": "bandwidth_crosscheck_present", "scope": f"{core_kind}@{frequency}",
                "observed": False, "threshold": True,
                "message": f"{core_kind}@{frequency} 缺少 2-FLOP 带宽交叉核验点。",
            })
        else:
            ladder_bandwidth = (bandwidth_crosscheck["measured_gflops_median"] /
                                bandwidth_crosscheck["operational_intensity_flop_per_byte"])
            bandwidth_crosscheck_error = abs(ladder_bandwidth / sustainable - 1)
            if bandwidth_crosscheck_error > MAXIMUM_BANDWIDTH_CROSSCHECK_ERROR:
                failed_gates.append({
                    "id": "maximum_bandwidth_crosscheck_error", "scope": f"{core_kind}@{frequency}",
                    "observed": bandwidth_crosscheck_error,
                    "threshold": MAXIMUM_BANDWIDTH_CROSSCHECK_ERROR,
                    "message": (f"{core_kind}@{frequency} 的 2-FLOP 内核与带宽屋顶相差 "
                                f"{bandwidth_crosscheck_error:.1%}。"),
                })
        high_intensity_compute_utilization = (
            intensity_summary[-1]["measured_gflops_median"] / peak)
        if high_intensity_compute_utilization < MINIMUM_HIGH_INTENSITY_COMPUTE_UTILIZATION:
            failed_gates.append({
                "id": "minimum_high_intensity_compute_utilization", "scope": f"{core_kind}@{frequency}",
                "observed": high_intensity_compute_utilization,
                "threshold": MINIMUM_HIGH_INTENSITY_COMPUTE_UTILIZATION,
                "message": (f"{core_kind}@{frequency} 的最高强度点只达到计算屋顶的 "
                            f"{high_intensity_compute_utilization:.1%}。"),
            })
        instruction_rows = [row for row in rows if row["workload_class"] == "instruction_proxy"]
        aligned = [row for row in instruction_rows
                   if row.get("pmu_window_aligned") is True
                   and isinstance(row.get("pmu_retired_instructions"), (int, float))
                   and isinstance(row.get("pmu_dram_bytes"), (int, float))
                   and row.get("pmu_retired_instructions", 0) > 0 and row.get("pmu_dram_bytes", 0) > 0]
        instruction = {
            "status": "publishable" if len(aligned) >= 2 else "needs_aligned_pmu_counters",
            "required_evidence": ["pmu_retired_instructions", "pmu_dram_bytes", "pmu_window_aligned"],
            "proxy_rows": len(instruction_rows), "aligned_pmu_rows": len(aligned),
        }
        if aligned:
            instruction.update({
                "median_retired_instructions_per_second": statistics.median(
                    float(row["pmu_retired_instructions"]) / (float(row["wall_ms"]) / 1000) for row in aligned),
                "median_instruction_intensity_per_dram_byte": statistics.median(
                    float(row["pmu_retired_instructions"]) / float(row["pmu_dram_bytes"]) for row in aligned),
            })
        severe_gate_ids = {"minimum_repeat_count", "minimum_intensity_point_count", "knee_span"}
        risk_level = ("low" if not failed_gates else "high"
                      if any(gate["id"] in severe_gate_ids for gate in failed_gates) else "medium")
        evidence_grade = "A" if not failed_gates else "C" if risk_level == "high" else "B"
        roofs.append({
            "core_kind": core_kind, "frequency_khz": frequency,
            "maximum_attainable_performance_gflops": peak,
            "sustainable_source_bandwidth_gbytes_s": sustainable,
            "knee_flop_per_byte": knee,
            "formula": {
                "symbolic": "P(I) = min(P_peak, B_source × I)",
                "substituted": f"P(I) = min({peak:.6g}, {sustainable:.6g} × I) GFLOP/s",
                "knee": "I_knee = P_peak / B_source",
            },
            "intensity_points": sorted(points, key=lambda item: item["operational_intensity_flop_per_byte"]),
            "intensity_summary": intensity_summary,
            "quality": {
                "passed": not failed_gates,
                "publishable_with_risk": True,
                "risk_level": risk_level,
                "risk_summary": ("全部统计质量门通过。" if not failed_gates else
                                 f"模型保留并可发布，但有 {len(failed_gates)} 项质量门未通过；"
                                 "请按失败项判断适用域并优先补测。"),
                "evidence_grade": evidence_grade,
                "failed_gates": failed_gates,
                "maximum_ceiling_violation": max(
                    (point["ceiling_violation_ratio"] for point in intensity_summary),
                    default=0.0),
                "maximum_intensity_throughput_drop": maximum_drop,
                "bandwidth_crosscheck_error": bandwidth_crosscheck_error,
                "compute_ceiling_repeat_cv": compute_cv,
                "bandwidth_ceiling_repeat_cv": bandwidth_cv,
                "maximum_ladder_repeat_cv": max(
                    point["repeat_cv"] for point in intensity_summary),
                "minimum_intensity_to_knee_ratio": minimum_intensity / knee,
                "maximum_intensity_to_knee_ratio": maximum_intensity / knee,
                "high_intensity_compute_utilization": high_intensity_compute_utilization,
            },
            "instruction_roofline": instruction,
        })
    return {
        "schema_version": SCHEMA,
        "roofline_kernel_version": KERNEL_VERSION,
        "roofs": roofs,
        "formula": {
            "symbolic": "P(I) = min(P_peak, B_source × I)",
            "knee": "I_knee = P_peak / B_source",
        },
        "quality": {
            "passed": all(roof["quality"]["passed"] for roof in roofs),
            "publishable_with_risk": True,
            "risk_level": ("high" if any(roof["quality"]["risk_level"] == "high" for roof in roofs)
                           else "medium" if any(roof["quality"]["risk_level"] == "medium" for roof in roofs)
                           else "low"),
            "failed_gates": [gate for roof in roofs for gate in roof["quality"]["failed_gates"]],
            "evidence_grade": ("C" if any(roof["quality"]["evidence_grade"] == "C" for roof in roofs)
                               else "B" if any(roof["quality"]["evidence_grade"] == "B" for roof in roofs)
                               else "A"),
        },
        "count_semantics": {
            "compute": "algorithmic FLOPs from fixed portable source",
            "bandwidth": "minimum source-level bytes, not measured DRAM traffic",
            "instruction": "never inferred from integer source operations; aligned PMU counters required",
        },
    }
