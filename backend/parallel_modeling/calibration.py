"""Measurement-backed calibration for the conditional transfer cost model.

The backend validates frozen plans, fits true-device measurements and preserves
both statistical quality and source-integrity evidence. Statistical or
coverage gates may produce an explicitly risk-labelled model; source,
structure and measurement-integrity failures remain fail-closed. Device
control stays in the Desktop runner; this module never fabricates missing
samples. Observed UI/Binder accounting remains immutable: published parameters
affect only conditional alternative-core/OPP predictions.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import re
import statistics
from collections import defaultdict
from pathlib import Path

from backend.core import PlatformError, atomic_write_json, utc_now
from .interference import SCHEMA as INTERFERENCE_SCHEMA, CLASSES, PAIR_FIELDS, CELL_FIELDS, cell_key
from .frequency_roofline import SCHEMA as ROOFLINE_SCHEMA, WORKLOAD_CLASSES, factor as roofline_factor, fit as fit_roofline
from . import classic_roofline

KINDS = ("big", "middle", "small")
# Kept as a public compatibility set for old plans and callers which explicitly
# request the original scalar model. New one-click plans measure typed
# frequency curves and a directional interference matrix instead.
GROUPS = ("core_ratios", "frequency_response", "contention", "dispatch_overhead")
DEFAULT_GROUPS = ("frequency_roofline", "pairwise_interference", "dispatch_overhead")
ALL_GROUPS = (*GROUPS, "frequency_roofline", "classic_roofline", "pairwise_interference")
ASSET_ROOT = Path(__file__).resolve().parents[2] / "tools" / "parallel_calibration"
MEASUREMENT_SCHEMA = "parallel_calibration_measurements/v1"
PORTABLE_PACKAGE_SCHEMA = "parallel_calibration_portable_package/v1"
PORTABLE_PACKAGE_ID = re.compile(r"pcp_[a-f0-9]{24}")
SUPERSESSION_SCHEMA = "parallel_calibration_supersession/v1"
CALIBRATION_ENGINE_VERSION = "1.3.0"
PAIRED_DISPATCH_WORKLOAD_VERSION = "parallel-calibration-proxy-v6"
PAIRED_DISPATCH_WORKLOAD_VERSIONS = frozenset((
    "parallel-calibration-proxy-v4",
    "parallel-calibration-proxy-v5",
    PAIRED_DISPATCH_WORKLOAD_VERSION,
))
PAIRED_DISPATCH_SCHEMA = "paired_dispatch_delta/v1"
PAIRED_DISPATCH_TOTAL_FIELDS = (
    "direct_batch_worker_cpu_ms", "direct_batch_worker_wall_ms",
    "direct_batch_producer_cpu_ms", "direct_batch_roundtrip_wall_ms",
    "queue_dispatch_worker_cpu_ms", "queue_dispatch_worker_wall_ms",
    "queue_dispatch_producer_cpu_ms", "queue_dispatch_roundtrip_wall_ms",
)
PAIRED_DISPATCH_DELTA_FIELDS = (
    "dispatch_wall_delta_ms_per_task", "dispatch_worker_cpu_delta_ms_per_task",
    "dispatch_producer_cpu_delta_ms_per_task",
)
THERMAL_EVIDENCE_BENCHMARKS = frozenset(("cpu_work", "pair_work", "roofline"))
MAX_SAMPLES = 20000
REFERENCE = {
    "core_kind": "big", "frequency_khz": 2500000,
    "work_unit": "recorded_ui_callback_on_cpu_ms",
    "ratio_reference": "同一代理负载、相同输入规模在各核最高 OPP 的线程 CPU 时间 / 大核最高 OPP 的线程 CPU 时间",
    "trace_frequency_normalization": "分析阶段仅在源硬件身份、95%–105% 驻留覆盖、核组/频率及绝对曲线全部核验后，按完整源驻留调和归一到统一参考工作量；任一证据缺失时显式回退旧参考锚",
    "application_scope": "代理微基准参数校准；应用迁移与并行正确性仍需独立验证",
}
THRESHOLDS = {"minimum_train_repeats": 3, "minimum_validation_repeats": 2,
              "maximum_repeat_cv": .15, "maximum_validation_median_ape": .10,
              "maximum_validation_p90_ape": .20, "maximum_roofline_training_mape": .10,
              "maximum_roofline_training_p90_ape": .20, "minimum_cpu_ms": 1.0,
              "maximum_frequency_relative_error": .03,
              "maximum_target_sibling_busy_fraction": .05}

PAIR_WORK_INTENSITY_SCHEMA = "pair_work_source_intensity/v1"

# These are source-kernel payload counts for parallel-calibration-proxy-v6.
# They deliberately exclude loop/index/address-control work and say nothing
# about the compiler's retired instruction count or cache/DRAM traffic.
_PAIR_WORK_SOURCE_COUNTS = {
    "compute": {
        "algorithmic_integer_operations_per_work_unit": 640,
        "mix64_calls_per_work_unit": 64,
        "minimum_source_bytes_per_work_unit": 0,
        "streamed_footprint_bytes_per_work_unit": 0,
        "working_set_access_pattern": "register_integer_mix_dependency_chain",
    },
    "memory": {
        "algorithmic_integer_operations_per_work_unit": 2052,
        "mix64_calls_per_work_unit": 0,
        "minimum_source_bytes_per_work_unit": 16384,
        "streamed_footprint_bytes_per_work_unit": 8192,
        "working_set_access_pattern": "sequential_read_modify_write_8kib_blocks_with_wrap",
    },
    "mixed": {
        "algorithmic_integer_operations_per_work_unit": 2692,
        "mix64_calls_per_work_unit": 64,
        "minimum_source_bytes_per_work_unit": 16384,
        "streamed_footprint_bytes_per_work_unit": 8192,
        "working_set_access_pattern": "integer_mix_then_sequential_read_modify_write_8kib_blocks_with_wrap",
    },
}


def pair_work_source_intensity(workload_class, *, work_units=None,
                               working_set_bytes=None):
    """Describe the frozen v6 proxy's auditable source-level intensity.

    This contract is intentionally separate from classic floating-point
    Roofline.  ``algorithmic_integer_operations`` counts payload expressions
    in the source kernel; it is not FLOP and is not a retired-instruction
    estimate.  ``minimum_source_bytes`` counts explicit volatile buffer reads
    and writes; it is not cache-line, write-allocate, prefetch, or DRAM traffic.
    """
    if workload_class not in _PAIR_WORK_SOURCE_COUNTS:
        raise ValueError(f"unsupported pair_work class: {workload_class}")
    counts = copy.deepcopy(_PAIR_WORK_SOURCE_COUNTS[workload_class])
    operations = counts["algorithmic_integer_operations_per_work_unit"]
    source_bytes = counts["minimum_source_bytes_per_work_unit"]
    result = {
        "schema_version": PAIR_WORK_INTENSITY_SCHEMA,
        "workload_version": PAIRED_DISPATCH_WORKLOAD_VERSION,
        "workload_class": workload_class,
        "metric_basis": "source_integer_payload_operations_per_minimum_source_byte",
        **counts,
        "operational_intensity_integer_ops_per_byte": (
            operations / source_bytes if source_bytes else None),
        "count_semantics": (
            "Integer operations are source-level payload arithmetic/bitwise "
            "expressions, excluding loop, index, address-generation and control "
            "work; they are neither FLOPs nor retired instructions. Minimum "
            "source bytes count explicit volatile buffer reads and writes and "
            "exclude cache-line, write-allocate, prefetch and DRAM traffic."
        ),
        "hardware_counter_evidence": {
            "retired_instructions": None,
            "dram_bytes": None,
            "instruction_intensity_instructions_per_dram_byte": None,
            "status": "optional_direct_perf_event_in_managed_benchmark",
            "available_metrics": ["IPC", "generic cache misses", "generic Cache MPKI"],
            "availability_note": "Each event remains unavailable when perf_event_open is unsupported or denied; workload labels never synthesize PMU values.",
        },
    }
    if working_set_bytes is not None:
        if type(working_set_bytes) is not int or working_set_bytes <= 0:
            raise ValueError("working_set_bytes must be a positive integer")
        result["working_set_bytes"] = working_set_bytes
        result["working_set_reuse_distance_bytes"] = (
            working_set_bytes if counts["streamed_footprint_bytes_per_work_unit"] else None)
        result["cache_residency_verified"] = False
    if work_units is not None:
        if type(work_units) is not int or work_units <= 0:
            raise ValueError("work_units must be a positive integer")
        result["work_units"] = work_units
        result["algorithmic_integer_operations"] = operations * work_units
        result["minimum_source_bytes"] = source_bytes * work_units
        result["streamed_footprint_bytes"] = (
            counts["streamed_footprint_bytes_per_work_unit"] * work_units)
        if working_set_bytes is not None:
            result["full_working_set_passes"] = (
                result["streamed_footprint_bytes"] / working_set_bytes)
    return result


def _error(message, code="parallel_calibration_invalid_input", **details):
    raise PlatformError(code, message, status_code=422, details=details)


def _fit_is_publishable(fit):
    """Support current risk-aware fits and older already-passed fit records."""
    if not isinstance(fit, dict):
        return False
    quality = fit.get("quality") if isinstance(fit.get("quality"), dict) else {}
    if "publishable" in quality:
        return quality.get("publishable") is True
    return bool(quality.get("passed") is True
                and quality.get("accepted_samples")
                and fit.get("parameter_estimates"))


def _encoded(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode()


def _hash(value):
    return hashlib.sha256(_encoded(value)).hexdigest()


def _number(value, label, minimum=0, maximum=float("inf")):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not minimum <= value <= maximum:
        _error(f"{label} 应为 {minimum} 至 {maximum} 范围内的有限数字。")
    return value


def _integer(value, label, minimum, maximum):
    _number(value, label, minimum, maximum)
    if not isinstance(value, int):
        _error(f"{label} 应为整数。")
    return value


def _string(value, label, maximum=200):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        _error(f"{label} 应为 1 至 {maximum} 字符的字符串。")
    return value.strip()


def _validate_thermal_snapshot(value, label):
    """Require auditable device thermal-zone evidence around frequency runs."""
    if not isinstance(value, list) or not value:
        _error(f"{label} 应包含至少一条有效 thermal zone 读数。")
    for index, row in enumerate(value):
        if not isinstance(row, dict):
            _error(f"{label}[{index}] 应为 thermal zone 对象。")
        zone = row.get("zone")
        zone_type = row.get("type")
        temperature = row.get("temperature_raw")
        if (not isinstance(zone, str) or not re.fullmatch(r"thermal_zone\d+", zone.strip())
                or not isinstance(zone_type, str) or not zone_type.strip() or len(zone_type.strip()) > 120
                or type(temperature) is not int or not -10_000_000 <= temperature <= 10_000_000):
            _error(f"{label}[{index}] 缺少有效 zone/type/整数 temperature_raw。")


def _validate_optional_benchmark_pmu(sample):
    """Validate optional target-thread counters without requiring device support.

    Older v6 measurements did not contain direct counters and remain valid. A
    managed binary that does emit them must preserve the per-event availability
    and enabled/running scaling evidence so IPC or MPKI cannot be injected as an
    unaudited scalar.
    """
    if "pmu_collection_status" not in sample:
        return
    status = sample.get("pmu_collection_status")
    if status not in ("collected", "partial", "unavailable"):
        _error("pmu_collection_status 应为 collected、partial 或 unavailable。")
    if sample.get("pmu_scope") != "target_thread_user_only_timed_window":
        _error("微基准 PMU 必须声明目标工作线程用户态计时窗作用域。")
    if sample.get("pmu_counting_semantics") != (
            "perf_event_open per-thread user-only group; one PERF_IOC_FLAG_GROUP "
            "reset/enable/disable window; each count scaled by time_enabled/time_running"):
        _error("微基准 PMU 必须由 perf 事件组在同一外层计时窗启停。")
    if sample.get("pmu_window_relation") != (
            "perf_group_encloses_timed_workload_window; timed wall/cpu exclude "
            "perf control/read overhead"):
        _error("微基准 PMU 必须证明事件组包络正式计时窗，且时延排除 perf 控制与读取开销。")
    if not isinstance(sample.get("pmu_window_aligned"), bool):
        _error("pmu_window_aligned 应为布尔值。")
    counters = sample.get("pmu_counters")
    expected_names = ("cycles", "instructions", "generic_cache_references", "generic_cache_misses")
    if not isinstance(counters, list) or len(counters) != len(expected_names):
        _error("pmu_counters 应包含固定的 cycles/instructions/cache reference/cache miss 证据。")
    observed: dict[str, float | None] = {}
    available_count = 0
    for index, (counter, name) in enumerate(zip(counters, expected_names)):
        if not isinstance(counter, dict) or counter.get("event") != name:
            _error(f"pmu_counters[{index}] 事件身份与固定顺序不一致。")
        counter_status = counter.get("status")
        if counter_status not in ("collected", "unavailable"):
            _error(f"pmu_counters[{index}].status 无效。")
        if counter_status == "unavailable":
            if any(counter.get(field) is not None for field in
                   ("raw_value", "scaled_value", "enabled_ns", "running_ns")):
                _error(f"不可用 PMU 事件 {name} 不能携带部分计数。")
            error = counter.get("error")
            if error is not None:
                _integer(error, f"pmu_counters[{index}].error", 1, 65535)
            observed[name] = None
            continue
        available_count += 1
        raw = _number(counter.get("raw_value"), f"pmu_counters[{index}].raw_value", 0, 1e30)
        scaled = _number(counter.get("scaled_value"), f"pmu_counters[{index}].scaled_value", 0, 1e30)
        enabled = _integer(counter.get("enabled_ns"), f"pmu_counters[{index}].enabled_ns", 1, 10**18)
        running = _integer(counter.get("running_ns"), f"pmu_counters[{index}].running_ns", 1, enabled)
        expected = raw * enabled / running
        if abs(scaled - expected) > max(abs(expected), 1.0) * 1e-6:
            _error(f"PMU 事件 {name} 的缩放计数与 enabled/running 证据不一致。")
        if counter.get("error") is not None:
            _error(f"已采集 PMU 事件 {name} 不能同时声明错误。")
        observed[name] = float(scaled)
    expected_status = "collected" if available_count == 4 else "partial" if available_count else "unavailable"
    if status != expected_status:
        _error("PMU 汇总状态与逐事件可用性不一致。")
    if available_count and sample.get("pmu_window_aligned") is not True:
        _error("已采集的 PMU 事件必须来自同一事件组计时窗。")
    flat_fields = {
        "pmu_cycles": "cycles",
        "pmu_retired_instructions": "instructions",
        "pmu_cache_references": "generic_cache_references",
        "pmu_cache_misses": "generic_cache_misses",
    }
    for field, name in flat_fields.items():
        value = sample.get(field)
        expected = observed[name]
        if expected is None:
            if value is not None:
                _error(f"{field} 对应事件不可用时必须为 null。")
        else:
            _number(value, field, 0, 1e30)
            if abs(float(value) - expected) > max(abs(expected), 1.0) * 1e-6:
                _error(f"{field} 与逐事件缩放计数不一致。")
    ipc = sample.get("pmu_ipc")
    expected_ipc = (observed["instructions"] / observed["cycles"]
                    if sample.get("pmu_window_aligned") is True
                    and observed["instructions"] is not None and observed["cycles"] else None)
    cache_mpki = sample.get("pmu_cache_mpki")
    expected_mpki = (observed["generic_cache_misses"] * 1000 / observed["instructions"]
                     if sample.get("pmu_window_aligned") is True
                     and observed["generic_cache_misses"] is not None and observed["instructions"] else None)
    for field, value, expected in (("pmu_ipc", ipc, expected_ipc),
                                   ("pmu_cache_mpki", cache_mpki, expected_mpki)):
        if expected is None:
            if value is not None:
                _error(f"{field} 缺少同窗分子/分母时必须为 null。")
        else:
            _number(value, field, 0, 1e12)
            if abs(float(value) - expected) > max(abs(expected), 1.0) * 1e-6:
                _error(f"{field} 与同窗计数器计算结果不一致。")


def _validate_target_sibling_quietness(sample):
    """Recompute and fail closed on target SMT-sibling activity evidence."""
    target_cpu = _integer(sample.get("cpu"), "cpu", 0, 4095)
    siblings = sample.get("target_sibling_cpus")
    if not isinstance(siblings, list):
        _error("target_sibling_cpus 应为目标物理核的同胞逻辑CPU数组。")
    if any(type(cpu) is not int or not 0 <= cpu <= 4095 for cpu in siblings):
        _error("target_sibling_cpus 只能包含有效逻辑CPU编号。")
    if len(siblings) != len(set(siblings)):
        _error("target_sibling_cpus 不能包含重复逻辑CPU。")
    if target_cpu in siblings:
        _error("target_sibling_cpus 不能包含目标CPU自身。")

    activity = sample.get("target_sibling_activity")
    if not isinstance(activity, list):
        _error("target_sibling_activity 应为逐同胞逻辑CPU活动数组。")
    if len(activity) != len(siblings):
        _error("target_sibling_activity 必须逐项覆盖 target_sibling_cpus。")

    detail_cpus = []
    verified_fractions = []
    detail_verified = []
    for index, row in enumerate(activity):
        if not isinstance(row, dict):
            _error(f"target_sibling_activity[{index}] 应为对象。")
        cpu = _integer(row.get("cpu"), f"target_sibling_activity[{index}].cpu", 0, 4095)
        detail_cpus.append(cpu)
        busy_ticks = _integer(row.get("busy_ticks"),
                              f"target_sibling_activity[{index}].busy_ticks", 0, 2 ** 63 - 1)
        total_ticks = _integer(row.get("total_ticks"),
                               f"target_sibling_activity[{index}].total_ticks", 0, 2 ** 63 - 1)
        if busy_ticks > total_ticks:
            _error(f"target_sibling_activity[{index}] 的 busy_ticks 不能超过 total_ticks。")
        verified = row.get("verified")
        if type(verified) is not bool:
            _error(f"target_sibling_activity[{index}].verified 应为布尔值。")
        detail_verified.append(verified)
        fraction = row.get("busy_fraction")
        if verified:
            if total_ticks <= 0:
                _error(f"target_sibling_activity[{index}] 已核验时 total_ticks 必须大于0。")
            fraction = _number(fraction,
                               f"target_sibling_activity[{index}].busy_fraction", 0, 1)
            expected_fraction = busy_ticks / total_ticks
            if abs(fraction - expected_fraction) > 1e-9:
                _error(f"target_sibling_activity[{index}].busy_fraction 与 tick 计数不一致。")
            verified_fractions.append(fraction)
        elif fraction is not None:
            _error(f"target_sibling_activity[{index}] 未核验时 busy_fraction 必须为 null。")

    if len(detail_cpus) != len(set(detail_cpus)) or set(detail_cpus) != set(siblings):
        _error("target_sibling_activity 的CPU明细必须与 target_sibling_cpus 一一匹配。")

    reported_activity_verified = sample.get("target_sibling_activity_verified")
    if type(reported_activity_verified) is not bool:
        _error("target_sibling_activity_verified 应为布尔值。")
    expected_activity_verified = not siblings or all(detail_verified)
    if reported_activity_verified is not expected_activity_verified:
        _error("target_sibling_activity_verified 与逐CPU核验状态不一致。")

    aggregate = sample.get("target_sibling_busy_fraction")
    if expected_activity_verified:
        aggregate = _number(aggregate, "target_sibling_busy_fraction", 0, 1)
        expected_aggregate = max(verified_fractions, default=0)
        if abs(aggregate - expected_aggregate) > 1e-9:
            _error("target_sibling_busy_fraction 必须等于逐同胞逻辑CPU busy_fraction 的最大值。")
    elif aggregate is not None:
        _error("同胞逻辑CPU活动未完整核验时 target_sibling_busy_fraction 必须为 null。")

    threshold = _number(sample.get("target_sibling_busy_threshold"),
                        "target_sibling_busy_threshold", 0, 1)
    expected_threshold = THRESHOLDS["maximum_target_sibling_busy_fraction"]
    if abs(threshold - expected_threshold) > 1e-12:
        _error("target_sibling_busy_threshold 必须使用冻结的5%质量门。")
    reported_quietness = sample.get("target_sibling_quietness_verified")
    if type(reported_quietness) is not bool:
        _error("target_sibling_quietness_verified 应为布尔值。")
    expected_quietness = (expected_activity_verified
                          and aggregate is not None and aggregate <= expected_threshold)
    if reported_quietness is not expected_quietness:
        _error("target_sibling_quietness_verified 与实测同胞CPU活动不一致。")
    if not expected_activity_verified:
        _error("目标物理核的同胞逻辑CPU活动未完整核验。")
    if not expected_quietness:
        _error("目标物理核的同胞逻辑CPU忙占比超过5%，样本受到SMT同核干扰。")


def _validate_per_cpu_frequency_source(sample, *, prefix, expected_cpu, label):
    """Admit only a single per-logical-CPU cpuinfo source for a fit window."""
    source_field = prefix + "frequency_source"
    cpu_field = prefix + "frequency_source_cpu"
    path_field = prefix + "frequency_source_path"
    source = sample.get(source_field)
    if source != "cpuinfo_cur_freq":
        _error(f"{label}频率必须全窗口使用目标逻辑CPU的 cpuinfo_cur_freq；fallback仅供诊断。")
    source_cpu = _integer(sample.get(cpu_field), cpu_field, 0, 4095)
    if source_cpu != expected_cpu:
        _error(f"{label}频率采样CPU与实际工作线程CPU不一致。")
    expected_path = f"/sys/devices/system/cpu/cpu{expected_cpu}/cpufreq/cpuinfo_cur_freq"
    if sample.get(path_field) != expected_path:
        _error(f"{label}频率采样路径未严格绑定实际工作线程的 cpuinfo_cur_freq。")


def _validate_frequency_window(sample, *, prefix, requested_khz, label):
    """Recompute the v6 measured-window frequency verdict from raw evidence."""
    count_field = prefix + "frequency_sample_count"
    failures_field = prefix + "frequency_read_failures"
    mean_field = "peer_observed_frequency_khz" if prefix else "frequency_mean_khz"
    minimum_field = prefix + "frequency_min_khz"
    maximum_field = prefix + "frequency_max_khz"
    verified_field = prefix + "frequency_verified"

    count = _integer(sample.get(count_field), count_field, 0, 2 ** 63 - 1)
    failures = _integer(sample.get(failures_field), failures_field, 0, 2 ** 63 - 1)
    mean = _number(sample.get(mean_field), mean_field, 1, 10_000_000)
    minimum = _number(sample.get(minimum_field), minimum_field, 1, 10_000_000)
    maximum = _number(sample.get(maximum_field), maximum_field, 1, 10_000_000)
    interval_ordered = minimum <= mean <= maximum
    tolerance = THRESHOLDS["maximum_frequency_relative_error"]
    low = requested_khz * (1 - tolerance)
    high = requested_khz * (1 + tolerance)
    window_in_range = interval_ordered and minimum >= low and maximum <= high
    expected_verified = count >= 5 and failures == 0 and window_in_range

    reported_verified = sample.get(verified_field)
    if type(reported_verified) is not bool:
        _error(f"{verified_field} 应为布尔值。")
    if reported_verified is not expected_verified:
        _error(f"{label} frequency_verified 与正式采样窗口证据不一致。")
    if not interval_ordered:
        _error(f"{label}频率窗口必须满足 min <= mean <= max。")
    if count < 5:
        _error(f"{label}频率窗口至少需要5个有效样本。")
    if failures != 0:
        _error(f"{label}频率窗口存在读取失败。")
    if not window_in_range:
        _error(f"{label}频率窗口未完整落在目标OPP±3%范围内。")
    if reported_verified is not True:
        _error(f"{label}频率窗口没有通过核验。")

    # Desktop and offline collectors retain the raw C mean while exposing a
    # canonical observed alias.  Reject diverging aliases so downstream audit
    # and fitting cannot see two different frequencies for the same window.
    if not prefix:
        observed = _number(sample.get("observed_frequency_khz"),
                           "observed_frequency_khz", 1, 10_000_000)
        if abs(observed - mean) > max(1e-9, abs(mean) * 1e-12):
            _error("observed_frequency_khz 必须等于 frequency_mean_khz。")
        for alias, canonical, value in (
                ("observed_frequency_min_khz", minimum_field, minimum),
                ("observed_frequency_max_khz", maximum_field, maximum)):
            if alias in sample:
                alias_value = _number(sample.get(alias), alias, 1, 10_000_000)
                if alias_value != value:
                    _error(f"{alias} 必须等于 {canonical}。")


def _validate_adaptive_frequency_residency(sample):
    """Validate the frozen v6 adaptive warm-up and terminal OPP residency gate."""
    count = _integer(sample.get("warmup_frequency_sample_count"),
                     "warmup_frequency_sample_count", 3, 12)
    consecutive = _integer(sample.get("warmup_frequency_in_range_consecutive"),
                           "warmup_frequency_in_range_consecutive", 0, 12)
    required = _integer(sample.get("warmup_frequency_required_consecutive"),
                        "warmup_frequency_required_consecutive", 1, 12)
    maximum = _integer(sample.get("warmup_frequency_max_samples"),
                       "warmup_frequency_max_samples", 1, 12)
    if required != 3 or maximum != 12:
        _error("自适应频率驻留必须使用冻结的连续3次、最多12次采样合同。")
    if count > maximum or consecutive != required or consecutive > count:
        _error("自适应频率驻留成功样本必须在连续3次命中后立即停止。")

    minimum = _integer(sample.get("warmup_frequency_min_khz"),
                       "warmup_frequency_min_khz", 1, 10_000_000)
    maximum_observed = _integer(sample.get("warmup_frequency_max_khz"),
                                "warmup_frequency_max_khz", 1, 10_000_000)
    last = _integer(sample.get("warmup_frequency_last_khz"),
                    "warmup_frequency_last_khz", 1, 10_000_000)
    if not minimum <= last <= maximum_observed:
        _error("自适应频率驻留的 min/last/max 顺序不一致。")
    target = sample["frequency_khz"]
    if abs(last / target - 1) > .03:
        _error("自适应预热末次频率未进入目标OPP±3%范围。")
    if sample.get("warmup_frequency_verified") is not True:
        _error("自适应频率驻留未达到末尾连续3次命中。")

    warmup_units = _integer(sample.get("warmup_work_units"),
                            "warmup_work_units", 1, 2_000_000_000)
    chunk = 32 if sample["benchmark"] == "empty_dispatch" else min(sample["work_units"], 32768)
    if warmup_units != count * chunk:
        _error("warmup_work_units 与自适应采样轮数及冻结预热块大小不一致。")


def _quantile(values, q):
    values = sorted(values)
    if not values:
        return None
    position = (len(values) - 1) * q
    lower = int(position)
    return values[lower] + (values[min(lower + 1, len(values) - 1)] - values[lower]) * (position - lower)


def _condition(sample):
    base = tuple(sample[key] for key in ("benchmark", "family", "workload_id", "core_kind", "frequency_khz", "active_tasks", "work_units"))
    if sample["benchmark"] == "pair_work":
        return base + tuple(sample.get(key) for key in PAIR_FIELDS)
    if sample["benchmark"] == "roofline":
        return base + tuple(sample.get(key) for key in
                            ("workload_class", "working_set_bytes", "roofline_operations_per_element"))
    return base


def _is_paired_dispatch(sample):
    return (sample.get("benchmark") == "empty_dispatch"
            and sample.get("workload_version") in PAIRED_DISPATCH_WORKLOAD_VERSIONS)


def _validate_paired_dispatch(sample):
    """Validate the v4-v6 same-payload direct/queue paired-delta contract."""
    if not _is_paired_dispatch(sample):
        return
    if (sample.get("dispatch_measurement_schema") != PAIRED_DISPATCH_SCHEMA
            or sample.get("dispatch_payload") != "mix64_v1"
            or sample.get("dispatch_payload_match") is not True
            or sample.get("cold_start_included") is not False):
        _error("v4-v6 Worker 投递样本缺少同负载配对差分合同，或 direct/queue 结果不一致。")
    for field in PAIRED_DISPATCH_TOTAL_FIELDS:
        _number(sample.get(field), field, 0, 3600000)
    for field in PAIRED_DISPATCH_DELTA_FIELDS:
        _number(sample.get(field), field, -3600000, 3600000)
    count = sample["work_units"]
    expected = {
        "dispatch_wall_delta_ms_per_task": (
            sample["queue_dispatch_roundtrip_wall_ms"]
            - sample["direct_batch_roundtrip_wall_ms"]) / count,
        "dispatch_worker_cpu_delta_ms_per_task": (
            sample["queue_dispatch_worker_cpu_ms"]
            - sample["direct_batch_worker_cpu_ms"]) / count,
        "dispatch_producer_cpu_delta_ms_per_task": (
            sample["queue_dispatch_producer_cpu_ms"]
            - sample["direct_batch_producer_cpu_ms"]) / count,
    }
    for field, value in expected.items():
        tolerance = max(1e-9, abs(value) * 1e-6)
        if abs(sample[field] - value) > tolerance:
            _error(f"v4-v6 Worker 投递样本 {field} 与配对总量不一致。")


def _dispatch_unit_cost(sample):
    """Return the modeled per-task critical-path cost for a dispatch sample."""
    if _is_paired_dispatch(sample):
        return sample["dispatch_wall_delta_ms_per_task"]
    return sample["cpu_ms"] / sample["work_units"]


def _repeat_unit_metric(sample):
    """Use the published wall-time metric for each repeat family.

    Classic Roofline is a throughput experiment.  Applying the generic
    worker-CPU-ms/work-unit CV to it can hide wall-time jitter and contradict
    the quantity that is actually fitted.
    """
    if sample.get("benchmark") == "empty_dispatch":
        return _dispatch_unit_cost(sample)
    if sample.get("benchmark") == "roofline":
        if sample.get("workload_class") in ("compute_ceiling", "intensity_ladder"):
            return sample["attainable_gflops"]
        return sample["source_level_gbytes_per_s"]
    return sample["cpu_ms"] / sample["work_units"]


def measurement_contract():
    return {"schema_version": MEASUREMENT_SCHEMA,
            "root_fields": {"hardware_profile_id": "与方案一致的参考硬件 ID", "plan_id": "校准方案 ID（建议保留）",
                            "provenance": {"device_id": "匿名设备标识", "os_build": "OS/固件版本", "collector": "采集程序及版本",
                                           "measured_at": "实测时间 ISO-8601", "source_kind": "measured"}, "samples": "以下字段的实际样本数组"},
            "sample_fields": {"sample_id": "全局唯一", "run_id": "独立完整运行 ID，训练与验证不能共用",
                              "benchmark": "cpu_work | empty_dispatch | pair_work | roofline", "family": "build | residual | roofline",
                              "workload_version": "采集器代理负载版本；empty_dispatch v4-v6 必须满足 paired_dispatch_delta/v1",
                              "workload_id": "冻结方案中的代理负载 ID；包含 object/integer/dispatch、频响代理与 classic Roofline 内核",
                              "core_kind": "硬件画像中的实际核组 ID", "frequency_khz": "目标 OPP，kHz",
                              "observed_frequency_khz": "采样观测的实际频率，kHz", "active_tasks": "目标加同时运行的干扰线程数",
                              "frequency_source": "生产拟合必须为cpuinfo_cur_freq；scaling_cur_freq_fallback仅供诊断",
                              "frequency_source_cpu": "频率采样绑定的目标逻辑CPU，必须等于cpu",
                              "frequency_source_path": "目标CPU的/sys/devices/system/cpu/cpuN/cpufreq/cpuinfo_cur_freq完整路径",
                              "frequency_sample_count": "正式测量窗口有效频率样本数，至少5",
                              "frequency_read_failures": "正式测量窗口读取失败数，生产拟合必须为0",
                              "frequency_mean_khz": "正式测量窗口目标CPU均频，必须位于min/max之间",
                              "frequency_min_khz": "正式测量窗口目标CPU最低频率，必须位于目标OPP±3%",
                              "frequency_max_khz": "正式测量窗口目标CPU最高频率，必须位于目标OPP±3%",
                              "warmup_frequency_sample_count": "自适应预热实际频率采样轮数，3至12",
                              "warmup_frequency_in_range_consecutive": "成功样本在连续3次命中目标OPP±3%后立即停止，冻结为3",
                              "warmup_frequency_required_consecutive": "冻结为3",
                              "warmup_frequency_max_samples": "冻结为12",
                              "warmup_frequency_min_khz": "整个预热收敛过程的最低频率，可位于目标范围外",
                              "warmup_frequency_max_khz": "整个预热收敛过程的最高频率，可位于目标范围外",
                              "warmup_frequency_last_khz": "预热末次频率，必须位于目标OPP±3%",
                              "warmup_frequency_verified": "末尾连续3次命中目标OPP±3%",
                              "warmup_work_units": "累计预热工作量，等于采样轮数乘冻结预热块大小",
                              "work_units": "迭代数或空任务投递数，正整数", "cpu_ms": "目标线程 CPU 时间，ms",
                              "wall_ms": "对应区间单调时钟耗时，ms（不作为核速度拟合值）", "repeat": "独立重复序号",
                              "split": "train | validation", "affinity_verified": "实际落核核验布尔值",
                              "frequency_verified": "频率稳定性核验布尔值", "thermal_throttled": "是否观测热降频",
                              "thermal_snapshot_before": "cpu_work/pair_work/roofline 必需；实测窗口前非空 thermal zone 原始读数",
                              "thermal_snapshot_after": "cpu_work/pair_work/roofline 必需；实测窗口后非空 thermal zone 原始读数",
                              "binary_sha256": "实际运行二进制SHA-256；所有训练/验证样本使用同一构建",
                              "target_sibling_activity_source": "proc_stat_cpu_ticks/v1；目标窗口前后读取/proc/stat逐逻辑CPU tick差分",
                              "target_sibling_cpus": "目标物理核除目标CPU外的全部同胞逻辑CPU；无SMT时为空数组",
                              "target_sibling_activity": "逐同胞CPU的busy_ticks/total_ticks/busy_fraction/verified原始证据",
                              "target_sibling_activity_verified": "明细完整且全部核验；无同胞线程时为true",
                              "target_sibling_busy_fraction": "逐同胞CPU忙占比最大值；未核验时为null，无同胞线程时为0",
                              "target_sibling_busy_threshold": "冻结质量门0.05",
                              "target_sibling_quietness_verified": "重算最大忙占比不超过5%；所有拟合样本必须为true",
                              "physical_core_placement_verified": "共跑时必需true；目标与干扰线程占用不同物理核心，跨全设备核组放置",
                              "concurrency_verified": "共跑时必需true；干扰线程覆盖目标区间，CPU/wall至少90%"},
            "optional_fields": ["observed_frequency_min_khz", "observed_frequency_max_khz", "temperature_c", "raw_sha256"],
            "optional_direct_pmu": {
                "scope": "target_thread_user_only_timed_window",
                "events": ["cycles", "instructions", "generic_cache_references",
                           "generic_cache_misses"],
                "window": "perf group enable precedes both timed start clocks; both timed end clocks are captured before perf disable/read/close",
                "window_relation": "perf_group_encloses_timed_workload_window; timed wall/cpu exclude perf control/read overhead",
                "scaling": "scaled_value = raw_value * enabled_ns / running_ns",
                "derived": {"pmu_ipc": "instructions / cycles",
                            "pmu_cache_mpki": "generic_cache_misses * 1000 / instructions"},
                "availability": "collected | partial | unavailable; missing events and derived values remain null and do not invalidate otherwise valid latency evidence",
            },
            "pairwise_interference": {"benchmark": "pair_work", "family": "build", "workload_class": "compute | memory | mixed",
                "peer_workload_class": "compute | memory | mixed，共跑时填写；单跑为none", "peer_core_kind": "硬件画像中的实际核组 ID；单跑为none",
                "peer_frequency_khz": "干扰者实际配置频率；单跑为0", "working_set_bytes": "每线程固定工作集，方案冻结",
                "peer_frequency_verified": "共跑时必需true，干扰者频率也需稳定",
                "peer_observed_frequency_khz": "共跑干扰者实际均频，kHz", "peer_frequency_min_khz": "共跑频率区间最小值", "peer_frequency_max_khz": "共跑频率区间最大值",
                "peer_frequency_source": "共跑时生产拟合必须为cpuinfo_cur_freq",
                "peer_frequency_source_cpu": "频率采样绑定的实际helper逻辑CPU",
                "peer_frequency_source_path": "实际helper CPU的cpuinfo_cur_freq完整路径",
                "peer_frequency_sample_count": "共跑正式窗口干扰线程有效频率样本数，至少5",
                "peer_frequency_read_failures": "共跑正式窗口干扰线程频率读取失败数，必须为0",
                "cpu": "目标逻辑CPU，且核验独立物理核", "observer_cpu": "单跑与共跑固定相同的独立采样CPU", "helpers": "共跑辅助线程，必须含相等的cpu/actual_cpu及affinity_verified=true，并对应冻结核组放置",
                "fit": "有方向的beta=max(0,共跑CPU/同条件单跑CPU−1)，只在两个活动任务、测量核组与频点匹配时消费",
                "validation": "同固定工作集、双倍工作量留出；训练和验证独立运行；保留每个方向的CPU而非只用整段wall"},
            "frequency_roofline": {"benchmark": "pair_work", "active_tasks": 1,
                "workload_class": "compute | memory | mixed（由PMU特征映射到阶段类型）",
                "fit": "按核组和负载类型拟合连续分段频响：高于knee使用e_hi，低于knee使用e_lo，0<=e_hi<=e_lo<=1",
                "validation": "每类每核至少五个训练OPP拟合，另用至少一个未参与拟合的OPP及独立run_id/工作量留出；训练与留出固定同一目标CPU、观测CPU和工作集；方案总计至少六个OPP并覆盖硬件最低/最高频，不用PMU分类直接臆造拐点"},
            "dispatch_overhead": {
                "benchmark": "empty_dispatch",
                "v4_identity": {"workload_version": "parallel-calibration-proxy-v4",
                                "dispatch_measurement_schema": PAIRED_DISPATCH_SCHEMA,
                                "dispatch_payload": "mix64_v1", "dispatch_payload_match": True,
                                "cold_start_included": False},
                "v5_identity": {"workload_version": "parallel-calibration-proxy-v5",
                                "dispatch_measurement_schema": PAIRED_DISPATCH_SCHEMA,
                                "dispatch_payload": "mix64_v1", "dispatch_payload_match": True,
                                "cold_start_included": False},
                "v6_identity": {"workload_version": PAIRED_DISPATCH_WORKLOAD_VERSION,
                                "dispatch_measurement_schema": PAIRED_DISPATCH_SCHEMA,
                                "dispatch_payload": "mix64_v1", "dispatch_payload_match": True,
                                "cold_start_included": False},
                "paired_paths": "同一已预热持久 Worker、目标CPU和OPP；direct_batch 连续执行 N 次同一 payload，queue_dispatch 逐项投递并逐项确认",
                "totals": list(PAIRED_DISPATCH_TOTAL_FIELDS),
                "deltas": list(PAIRED_DISPATCH_DELTA_FIELDS),
                "recompute": "每个 delta = (queue_dispatch 对应总量 - direct_batch 对应总量) / work_units；容差 max(1e-9, |expected|*1e-6)",
                "fit": "branch_overhead_ms 使用 dispatch_wall_delta_ms_per_task；worker/producer CPU delta 仅单独归因，三者不相加",
                "validation": "留出集比较 branch_overhead_ms*task_count 与 dispatch_wall_delta_ms_per_task*task_count",
                "legacy": "parallel-calibration-proxy-v1-v3 继续使用兼容字段 cpu_ms/work_units（worker CPU）"},
            "classic_roofline": classic_roofline.measurement_contract(),
            "units": {"cpu_ms": "ms", "wall_ms": "ms", "dispatch_delta_ms_per_task": "ms/task",
                      "frequency_khz": "kHz", "ape": "ratio, 0.10 = 10%"},
            "quality_thresholds": THRESHOLDS,
            "validation": "按完整运行和条件留出。模板将中间 OPP、不同工作规模和不同共跑数留出；避免相同条件的重复行充当验证。",
            "evidence_boundary": "导入者声明实测来源；保存输入哈希、运行标识和设备信息。服务检查数值与实验一致性，不独立证明设备身份或应用有效性。"}


class CalibrationManager:
    def __init__(self, state_root, base_model, hardware_profile, artifact_store=None, hardware_profile_store=None):
        self.root = Path(state_root).resolve() / "parallel-modeling" / "calibration"
        self.base_model = copy.deepcopy(base_model)
        self.hardware = copy.deepcopy(hardware_profile)
        self.artifact_store = artifact_store
        self.hardware_profile_store = hardware_profile_store
        for name in ("plans", "fits", "versions", "supersessions"):
            (self.root / name).mkdir(parents=True, exist_ok=True)

    def _context(self, hardware_profile_id):
        """Return an immutable real-topology model for one active profile.

        ``proxy_kind`` selects only the inherited starting coefficients.  The
        plan, CPU map, OPP domain, fit paths and published model all retain the
        actual hardware group IDs.
        """
        if hardware_profile_id == self.hardware["id"]:
            hardware = copy.deepcopy(self.hardware)
            model = copy.deepcopy(self.base_model)
        else:
            if self.hardware_profile_store is None:
                _error("所选硬件画像不在当前校准目录中。", code="parallel_hardware_profile_not_found",
                       hardware_profile_id=hardware_profile_id)
            hardware = self.hardware_profile_store.get(hardware_profile_id, require_active=True)
            model = copy.deepcopy(self.base_model)
            profiles = {}
            for group in hardware["core_groups"]:
                proxy_kind = group.get("proxy_kind", group.get("kind"))
                if proxy_kind not in self.base_model["profiles"]:
                    _error("硬件画像核组缺少有效的代理核型映射。", hardware_profile_id=hardware_profile_id,
                           core_group_id=group.get("id"), proxy_kind=proxy_kind)
                profile = copy.deepcopy(self.base_model["profiles"][proxy_kind])
                profile.update(opps_khz=copy.deepcopy(group["opps_khz"]), max_khz=group["max_khz"],
                               proxy_kind=proxy_kind,
                               provenance="OPP/topology from hardware profile; coefficients inherited before device calibration")
                profiles[group["id"]] = profile
            model["profiles"] = profiles
            model.update(id="tm_" + _hash({"base_model_id": self.base_model["id"],
                                           "base_model_version": self.base_model.get("version"),
                                           "hardware_profile_id": hardware["id"],
                                           "profile_sha256": hardware.get("profile_sha256")})[:24],
                         hardware_profile_id=hardware["id"], status="topology_proxy_uncalibrated")
            model["hardware_profile"] = {key: copy.deepcopy(hardware[key]) for key in
                                         ("id", "name", "source", "core_groups", "frequency_domains", "profile_sha256")
                                         if key in hardware}
        groups = hardware.get("core_groups", hardware.get("cpu_groups", []))
        if not groups:
            _error("硬件画像缺少 CPU 核组。", hardware_profile_id=hardware_profile_id)
        group_map = {group["id"]: copy.deepcopy(group) for group in groups}
        if set(group_map) != set(model["profiles"]):
            _error("硬件画像核组与校准模型核组不一致。", hardware_profile_id=hardware_profile_id)
        big = [group for group in groups if group.get("proxy_kind", group.get("kind")) == "big"]
        reference = max(big or groups, key=lambda group: (group["max_khz"], group["id"]))["id"]
        anchor = copy.deepcopy(REFERENCE)
        anchor.update(core_kind=reference, frequency_khz=model["profiles"][reference]["max_khz"])
        return {"hardware": hardware, "model": model, "groups": group_map,
                "group_ids": list(group_map), "reference_group": reference, "reference_anchor": anchor}

    def _record(self, category, value):
        value = copy.deepcopy(value)
        if self.artifact_store:
            kind = "parallel_calibration_" + {"plans": "plan", "fits": "fit", "versions": "version"}[category]
            inputs = []
            if category == "fits":
                inputs = self.get_plan(value["plan_id"]).get("artifacts", [])
            elif category == "versions":
                inputs = self.get_fit(value["fit_id"]).get("artifacts", [])
            artifact = self.artifact_store.write(kind, value, source_connector_id="parallel-calibration",
                source_binding={"record_id": value["id"]},
                producer={"component": "parallel_calibration", "version": CALIBRATION_ENGINE_VERSION},
                input_artifacts=["artifact:" + item["artifact_id"] for item in inputs])
            value["artifacts"] = [{"artifact_id": artifact["artifact_id"], "artifact_type": kind}]
        else:
            value["artifacts"] = []
        atomic_write_json(self.root / category / (value["id"] + ".json"), value)
        return copy.deepcopy(value)

    def _get(self, category, identity):
        prefix = {"plans": "cp", "fits": "cf", "versions": "cm"}[category]
        if not isinstance(identity, str) or not re.fullmatch(prefix + r"_[a-f0-9]{24}", identity):
            _error("无效的校准记录 ID。", code="parallel_calibration_not_found")
        path = self.root / category / (identity + ".json")
        if not path.is_file():
            raise PlatformError("parallel_calibration_not_found", "校准记录不存在。", status_code=404)
        return json.loads(path.read_text(encoding="utf-8"))

    def _list(self, category):
        values = [json.loads(path.read_text(encoding="utf-8")) for path in (self.root / category).glob("*.json")]
        return sorted(values, key=lambda value: (value.get("created_at", ""), value["id"]), reverse=True)

    def get_plan(self, identity): return self._get("plans", identity)
    def get_fit(self, identity): return self._get("fits", identity)
    def get_version(self, identity): return self._get("versions", identity)
    def list_plans(self, *, include_superseded=False):
        plans = self._list("plans")
        if not include_superseded:
            plans = [plan for plan in plans
                     if not self.is_hardware_profile_superseded(plan.get("hardware_profile_id"))]
        return plans
    def list_fits(self, *, include_superseded=False):
        fits = self._list("fits")
        if not include_superseded:
            fits = [fit for fit in fits
                    if not self.is_hardware_profile_superseded(fit.get("hardware_profile_id"))]
        return fits
    def _display_provenance(self, version):
        distribution = version.get("distribution")
        if not isinstance(distribution, dict) or distribution.get("kind") != "bundled_offline_calibration":
            return None
        return {
            "kind": "bundled_offline_calibration",
            "label": "平台离线实测",
            "calibration_label": "随包校准",
            "package_id": distribution.get("package_id"),
            "package_source": distribution.get("source"),
            "calibration_version_id": version.get("id"),
        }

    def _decorate_version_for_display(self, version):
        version = copy.deepcopy(version)
        provenance = self._display_provenance(version)
        if provenance:
            version.update(display_source=provenance["kind"],
                           display_source_label=provenance["calibration_label"],
                           display_provenance=provenance)
        return version

    def decorate_hardware_profile_for_display(self, profile):
        profile = copy.deepcopy(profile)
        matching = [version for version in self._list("versions")
                    if version.get("hardware_profile_id") == profile.get("id")
                    and version.get("status") in ("proxy_calibrated", "partially_calibrated", "risk_calibrated")
                    and self._display_provenance(version)]
        if matching:
            version = matching[0]
            provenance = self._display_provenance(version)
            profile.update(display_source=provenance["kind"],
                           display_source_label=provenance["label"],
                           display_provenance=provenance,
                           bundled_calibration_version_id=version["id"],
                           bundled_calibration_status=version.get("status"))
        return profile

    def list_versions(self, *, include_superseded=False):
        versions = self._list("versions")
        if not include_superseded:
            versions = [version for version in versions
                        if (version.get("status") != "superseded"
                            and not self.is_calibration_version_superseded(version.get("id")))]
        return [self._decorate_version_for_display(version) for version in versions]

    def list_supersessions(self):
        values = []
        for path in (self.root / "supersessions").glob("ps_*.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                _error("校准包退役回执无法读取。",
                       code="parallel_calibration_supersession_corrupt",
                       receipt_id=path.stem, reason=str(exc))
            payload = {key: copy.deepcopy(item) for key, item in value.items()
                       if key != "receipt_sha256"}
            if (value.get("schema_version") != SUPERSESSION_SCHEMA
                    or not re.fullmatch(r"ps_[a-f0-9]{24}", str(value.get("id", "")))
                    or value.get("id") != path.stem
                    or value.get("receipt_sha256") != _hash(payload)):
                _error("校准包退役回执内容校验失败。",
                       code="parallel_calibration_supersession_corrupt",
                       receipt_id=path.stem)
            values.append(value)
        return sorted(values, key=lambda value: (value.get("applied_at", ""), value["id"]), reverse=True)

    @staticmethod
    def _normalize_supersedes(value, *, current_package_id=None,
                              current_hardware_profile_id=None,
                              current_calibration_version_id=None):
        if value is None:
            return []
        if not isinstance(value, list) or len(value) > 64:
            _error("离线校准包 supersedes 应为不超过 64 项的数组。",
                   code="parallel_calibration_package_corrupt")
        normalized = []
        seen = set()
        allowed = {"package_id", "hardware_profile_id", "calibration_version_id", "reason"}
        for index, raw in enumerate(value):
            if not isinstance(raw, dict) or set(raw) - allowed:
                _error(f"离线校准包 supersedes[{index}] 字段无效。",
                       code="parallel_calibration_package_corrupt")
            package_id = raw.get("package_id")
            hardware_id = raw.get("hardware_profile_id")
            version_id = raw.get("calibration_version_id")
            reason = raw.get("reason")
            if (not isinstance(package_id, str) or not PORTABLE_PACKAGE_ID.fullmatch(package_id)
                    or not isinstance(hardware_id, str)
                    or not re.fullmatch(r"hp_[a-f0-9]{24}", hardware_id)
                    or not isinstance(version_id, str)
                    or not re.fullmatch(r"cm_[a-f0-9]{24}", version_id)
                    or not isinstance(reason, str) or not reason.strip()
                    or len(reason.strip()) > 500):
                _error(f"离线校准包 supersedes[{index}] 身份或原因无效。",
                       code="parallel_calibration_package_corrupt")
            if (package_id == current_package_id
                    or hardware_id == current_hardware_profile_id
                    or version_id == current_calibration_version_id):
                _error("离线校准包不能退役自身的包、硬件画像或校准版本。",
                       code="parallel_calibration_package_corrupt")
            identity = (package_id, hardware_id, version_id)
            if identity in seen:
                _error("离线校准包 supersedes 包含重复目标。",
                       code="parallel_calibration_package_corrupt")
            seen.add(identity)
            normalized.append({
                "package_id": package_id,
                "hardware_profile_id": hardware_id,
                "calibration_version_id": version_id,
                "reason": reason.strip(),
            })
        return normalized

    @staticmethod
    def _portable_record(value, label):
        if not isinstance(value, dict):
            _error(f"离线校准包缺少有效的 {label}。", code="parallel_calibration_package_corrupt")
        result = copy.deepcopy(value)
        # Artifact IDs belong to the state store that produced the package and
        # are not portable.  The evidence/model hashes below remain portable.
        result["artifacts"] = []
        result.pop("distribution", None)
        return result

    @staticmethod
    def _portable_payload(package):
        return {key: copy.deepcopy(value) for key, value in package.items()
                if key not in {"package_id", "package_sha256"}}

    def export_portable_package(self, version_id, *, supersedes=None):
        """Export one auditable, self-contained offline calibration package.

        Keeping the fit (including measurements) is intentional: the Desktop
        curve gallery can show observed OPP medians instead of only a fitted
        line, and an engineer can audit which holdout gates admitted the model.
        """
        version = self._portable_record(self.get_version(version_id), "calibration_version")
        if (version.get("status") == "superseded"
                or self.is_calibration_version_superseded(version_id)):
            _error("已退役的校准版本不能重新导出为可安装离线包。",
                   code="parallel_calibration_model_superseded",
                   calibration_version_id=version_id)
        fit = self._portable_record(self.get_fit(version["fit_id"]), "fit")
        plan = self._portable_record(self.get_plan(version["plan_id"]), "plan")
        if self.hardware_profile_store is None:
            hardware = copy.deepcopy(self.hardware)
        else:
            hardware = self.hardware_profile_store.get(version["hardware_profile_id"])
        hardware.pop("cpu_groups", None)
        package = {
            "schema_version": PORTABLE_PACKAGE_SCHEMA,
            "created_at": version.get("created_at") or fit.get("created_at") or plan.get("created_at"),
            "name": version["name"],
            "hardware_profile": hardware,
            "plan": plan,
            "fit": fit,
            "calibration_version": version,
        }
        normalized_supersedes = self._normalize_supersedes(
            supersedes,
            current_hardware_profile_id=hardware.get("id"),
            current_calibration_version_id=version.get("id"),
        )
        if normalized_supersedes:
            package["supersedes"] = normalized_supersedes
        digest = _hash(self._portable_payload(package))
        package.update(package_id="pcp_" + digest[:24], package_sha256=digest)
        return package

    def _validate_portable_package(self, package):
        if not isinstance(package, dict) or package.get("schema_version") != PORTABLE_PACKAGE_SCHEMA:
            _error("离线校准包格式无效。", code="parallel_calibration_package_corrupt")
        digest = _hash(self._portable_payload(package))
        if (package.get("package_sha256") != digest
                or not isinstance(package.get("package_id"), str)
                or not PORTABLE_PACKAGE_ID.fullmatch(package["package_id"])
                or package["package_id"] != "pcp_" + digest[:24]):
            _error("离线校准包内容哈希校验失败。", code="parallel_calibration_package_corrupt")
        hardware_value = package.get("hardware_profile")
        if not isinstance(hardware_value, dict):
            _error("离线校准包缺少有效的 hardware_profile。",
                   code="parallel_calibration_package_corrupt")
        hardware = copy.deepcopy(hardware_value)
        hardware.pop("cpu_groups", None)
        plan = self._portable_record(package.get("plan"), "plan")
        fit = self._portable_record(package.get("fit"), "fit")
        version = self._portable_record(package.get("calibration_version"), "calibration_version")
        identities = ((plan, "cp"), (fit, "cf"), (version, "cm"))
        if any(not isinstance(record.get("id"), str)
               or not re.fullmatch(prefix + r"_[a-f0-9]{24}", record["id"])
               for record, prefix in identities):
            _error("离线校准包记录 ID 无效。", code="parallel_calibration_package_corrupt")
        hardware_id = hardware.get("id")
        if (not isinstance(hardware_id, str)
                or any(record.get("hardware_profile_id") != hardware_id for record in (plan, fit, version))
                or fit.get("plan_id") != plan["id"]
                or version.get("plan_id") != plan["id"]
                or version.get("fit_id") != fit["id"]):
            _error("离线校准包的硬件、方案、拟合与版本引用不一致。",
                   code="parallel_calibration_package_corrupt")
        if (not _fit_is_publishable(fit)
                or not fit.get("quality", {}).get("accepted_samples")):
            _error("离线校准包未包含可发布且来源完整的真机拟合。",
                   code="parallel_calibration_package_corrupt")
        measurements = fit.get("measurements")
        if not isinstance(measurements, dict) or fit.get("measurements_sha256") != _hash(measurements):
            _error("离线校准包的实测数据哈希校验失败。", code="parallel_calibration_package_corrupt")
        model = version.get("model")
        if (version.get("status") not in ("proxy_calibrated", "partially_calibrated", "risk_calibrated")
                or not isinstance(model, dict)
                or model.get("id") != version["id"]
                or model.get("hardware_profile_id") != hardware_id
                or version.get("model_sha256") != _hash(model)
                or version.get("measurements_sha256") != fit.get("measurements_sha256")):
            _error("离线校准包的已发布模型校验失败。", code="parallel_calibration_package_corrupt")
        supersedes = self._normalize_supersedes(
            package.get("supersedes"),
            current_package_id=package["package_id"],
            current_hardware_profile_id=hardware_id,
            current_calibration_version_id=version["id"],
        )
        return hardware, plan, fit, version, supersedes

    def _apply_supersessions(self, package, installed_version):
        targets = self._normalize_supersedes(
            package.get("supersedes"),
            current_package_id=package["package_id"],
            current_hardware_profile_id=installed_version["hardware_profile_id"],
            current_calibration_version_id=installed_version["id"],
        )
        receipts = []
        previous_receipts = {receipt["id"]: receipt for receipt in self.list_supersessions()}
        for target in targets:
            for previous in previous_receipts.values():
                previous_target = previous.get("target", {})
                same_target = all(previous_target.get(key) == target.get(key) for key in (
                    "package_id", "hardware_profile_id", "calibration_version_id"))
                if (same_target
                        and previous.get("superseding_package_id") != package["package_id"]):
                    _error("同一旧校准包已由其他后继包退役，不能分叉迁移链。",
                           code="parallel_calibration_package_conflict",
                           target_package_id=target["package_id"],
                           existing_superseding_package_id=(
                               previous.get("superseding_package_id")),
                           requested_superseding_package_id=package["package_id"])
            target_version_path = self.root / "versions" / (target["calibration_version_id"] + ".json")
            target_version = None
            target_package_verified = False
            version_result = "not_installed"
            if target_version_path.is_file():
                target_version = self.get_version(target["calibration_version_id"])
                distribution = target_version.get("distribution")
                distributed_by = distribution.get("package_id") if isinstance(distribution, dict) else None
                if distributed_by != target["package_id"]:
                    _error("supersedes 目标不是由声明的旧离线包安装，拒绝退役本地校准证据。",
                           code="parallel_calibration_package_conflict",
                           calibration_version_id=target_version["id"],
                           expected_package_id=target["package_id"])
                if target_version.get("hardware_profile_id") != target["hardware_profile_id"]:
                    _error("supersedes 目标版本与硬件画像引用不一致。",
                           code="parallel_calibration_package_conflict",
                           calibration_version_id=target_version["id"],
                           hardware_profile_id=target["hardware_profile_id"])
                target_package_verified = True
                existing_supersession = target_version.get("supersession")
                if target_version.get("status") == "superseded":
                    if (not isinstance(existing_supersession, dict)
                            or existing_supersession.get("by_package_id") != package["package_id"]):
                        _error("校准版本已由其他离线包退役，不能改写迁移链。",
                               code="parallel_calibration_package_conflict",
                               calibration_version_id=target_version["id"])
                    version_result = "superseded"
                else:
                    target_version["status"] = "superseded"
                    target_version["superseded_at"] = utc_now()
                    target_version["supersession"] = {
                        "by_package_id": package["package_id"],
                        "by_calibration_version_id": installed_version["id"],
                        "reason": target["reason"],
                    }
                    atomic_write_json(target_version_path, target_version)
                    version_result = "superseded"

            hardware_result = "not_installed"
            hardware_path = self.hardware_profile_store.root / (target["hardware_profile_id"] + ".json")
            if hardware_path.is_file():
                # Hardware profiles are content-addressed but do not carry a
                # package-install provenance marker.  Only archive one after
                # the matching calibration version proves that this exact
                # profile was referenced by the declared predecessor package.
                # Otherwise a successor-only install could retire unrelated
                # local evidence which happens to have the same profile ID.
                if not target_package_verified:
                    hardware_result = "preserved_unverified_origin"
                else:
                    profile = self.hardware_profile_store.get(target["hardware_profile_id"])
                    if profile.get("status") == "archived":
                        hardware_result = "archived"
                    else:
                        self.hardware_profile_store.archive(target["hardware_profile_id"])
                        hardware_result = "archived"

            receipt_id = "ps_" + _hash({
                "by_package_id": package["package_id"],
                "target_package_id": target["package_id"],
                "target_calibration_version_id": target["calibration_version_id"],
            })[:24]
            receipt_path = self.root / "supersessions" / (receipt_id + ".json")
            previous = previous_receipts.get(receipt_id)
            receipt = {
                "schema_version": SUPERSESSION_SCHEMA,
                "id": receipt_id,
                "applied_at": (previous or {}).get("applied_at") or utc_now(),
                "superseding_package_id": package["package_id"],
                "superseding_calibration_version_id": installed_version["id"],
                "target": copy.deepcopy(target),
                "result": {
                    "calibration_version": version_result,
                    "hardware_profile": hardware_result,
                },
            }
            stable_keys = ("schema_version", "id", "superseding_package_id",
                           "superseding_calibration_version_id", "target", "result")
            if previous and all(previous.get(key) == receipt.get(key) for key in stable_keys):
                receipt = previous
            else:
                if previous:
                    receipt["updated_at"] = utc_now()
                receipt["receipt_sha256"] = _hash(receipt)
                atomic_write_json(receipt_path, receipt)
            receipts.append(receipt)
        return receipts

    def _preflight_supersessions(self, package, targets):
        """Reject attempts to retire local evidence before installing a successor."""
        for target in targets:
            path = self.root / "versions" / (target["calibration_version_id"] + ".json")
            if not path.is_file():
                continue
            version = self.get_version(target["calibration_version_id"])
            if version.get("hardware_profile_id") != target["hardware_profile_id"]:
                _error("supersedes 目标版本与硬件画像引用不一致。",
                       code="parallel_calibration_package_conflict",
                       calibration_version_id=version["id"],
                       hardware_profile_id=target["hardware_profile_id"])
            if version.get("status") == "superseded":
                existing = version.get("supersession")
                if (not isinstance(existing, dict)
                        or existing.get("by_package_id") != package["package_id"]):
                    _error("校准版本已由其他离线包退役，不能改写迁移链。",
                           code="parallel_calibration_package_conflict",
                           calibration_version_id=version["id"])
                continue
            distribution = version.get("distribution")
            distributed_by = distribution.get("package_id") if isinstance(distribution, dict) else None
            if distributed_by != target["package_id"]:
                _error("supersedes 目标不是由声明的旧离线包安装，拒绝退役本地校准证据。",
                       code="parallel_calibration_package_conflict",
                       calibration_version_id=version["id"],
                       expected_package_id=target["package_id"])

    def _persisted_retirement(self, package, *, hardware_profile_id, calibration_version_id):
        """Return the verified receipt that permanently retires this package identity.

        A successor-only installation can record that its predecessor was not
        present.  The receipt must still prevent a later bundle downgrade from
        materializing that predecessor as an active model when the successor
        JSON is no longer in the scanned directory.
        """
        matches = []
        for receipt in self.list_supersessions():
            target = receipt.get("target", {})
            if (target.get("package_id") == package.get("package_id")
                    and target.get("hardware_profile_id") == hardware_profile_id
                    and target.get("calibration_version_id") == calibration_version_id):
                matches.append(receipt)
        if len(matches) > 1:
            _error("同一离线校准包存在多条持久退役链。",
                   code="parallel_calibration_supersession_corrupt",
                   package_id=package.get("package_id"),
                   calibration_version_id=calibration_version_id)
        return matches[0] if matches else None

    def is_hardware_profile_superseded(self, identity):
        return any(receipt.get("target", {}).get("hardware_profile_id") == identity
                   and receipt.get("result", {}).get("hardware_profile") == "archived"
                   for receipt in self.list_supersessions())

    def is_calibration_version_superseded(self, identity):
        # Version IDs are content-bound to the predecessor package.  A durable
        # receipt retires that exact version even when it was absent at the
        # moment of migration; unlike a matching local hardware profile, it
        # cannot represent unrelated local evidence.
        return any(receipt.get("target", {}).get("calibration_version_id") == identity
                   for receipt in self.list_supersessions())

    def install_portable_package(self, package, *, source=None, apply_supersessions=True):
        """Idempotently materialize a repository-bundled model into state."""
        if self.hardware_profile_store is None:
            _error("当前校准目录不能安装离线硬件画像。", code="parallel_calibration_package_corrupt")
        hardware, plan, fit, version, supersedes = self._validate_portable_package(package)
        self._preflight_supersessions(package, supersedes)
        persisted_retirement = self._persisted_retirement(
            package, hardware_profile_id=hardware["id"],
            calibration_version_id=version["id"])
        if persisted_retirement:
            _error("该离线校准包已被持久迁移回执退役，不能重新安装为活动模型。",
                   code="parallel_calibration_package_superseded",
                   package_id=package["package_id"],
                   calibration_version_id=version["id"],
                   superseding_package_id=persisted_retirement.get("superseding_package_id"),
                   superseding_calibration_version_id=(
                       persisted_retirement.get("superseding_calibration_version_id")))
        if hardware["id"] == self.hardware_profile_store.builtin["id"]:
            installed_hardware = self.hardware_profile_store.get(hardware["id"])
            if installed_hardware.get("profile_sha256") != hardware.get("profile_sha256"):
                _error("离线校准包的内置硬件画像不匹配。", code="parallel_calibration_package_corrupt")
        else:
            installed_hardware = self.hardware_profile_store.install_portable(hardware)
            if (installed_hardware["id"] != hardware["id"]
                    or installed_hardware.get("profile_sha256") != hardware.get("profile_sha256")):
                _error("离线校准包的硬件画像身份校验失败。", code="parallel_calibration_package_corrupt")
        distribution = {"kind": "bundled_offline_calibration", "package_id": package["package_id"]}
        if source is not None:
            distribution["source"] = str(source)
        installed = {}
        for category, record in (("plans", plan), ("fits", fit), ("versions", version)):
            path = self.root / category / (record["id"] + ".json")
            if path.is_file():
                existing = self._get(category, record["id"])
                # Existing local evidence wins, but a deterministic record ID
                # may never point to a different plan/model/measurement set.
                comparable = {
                    "plans": _hash({key: value for key, value in existing.items()
                                    if key not in {"id", "created_at", "artifacts", "distribution"}})
                             == _hash({key: value for key, value in record.items()
                                      if key not in {"id", "created_at", "artifacts", "distribution"}}),
                    "fits": (existing.get("measurements_sha256") == record.get("measurements_sha256")
                             and _hash(existing.get("model_preview")) == _hash(record.get("model_preview"))),
                    "versions": (existing.get("model_sha256") == record.get("model_sha256")
                                 and existing.get("measurements_sha256") == record.get("measurements_sha256")),
                }[category]
                if not comparable:
                    _error("离线校准包与本地同 ID 记录内容冲突。",
                           code="parallel_calibration_package_conflict", category=category,
                           record_id=record["id"])
                installed[category] = existing
                continue
            materialized = copy.deepcopy(record)
            materialized["distribution"] = distribution
            materialized["artifacts"] = []
            atomic_write_json(path, materialized)
            installed[category] = self._get(category, record["id"])
        result = {"package_id": package["package_id"], "hardware_profile": installed_hardware,
                  "plan": installed["plans"], "fit": installed["fits"],
                  "calibration_version": installed["versions"],
                  "supersedes": supersedes, "supersession_receipts": []}
        if apply_supersessions:
            result["supersession_receipts"] = self._apply_supersessions(
                package, installed["versions"])
        return result

    def install_portable_packages(self, directory):
        root = Path(directory).resolve()
        if not root.is_dir():
            return []
        packages = []
        for path in sorted(root.glob("*.json")):
            try:
                package = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                _error("离线校准包无法读取。", code="parallel_calibration_package_corrupt",
                       source=str(path), reason=str(exc))
            packages.append((path.name, package))
        installable = []
        declared_targets = {}
        for source, package in packages:
            hardware, _, _, version, supersedes = self._validate_portable_package(package)
            self._preflight_supersessions(package, supersedes)
            for target in supersedes:
                identity = (target["package_id"], target["hardware_profile_id"],
                            target["calibration_version_id"])
                owner = declared_targets.get(identity)
                if owner is not None and owner != package["package_id"]:
                    _error("同一启动目录中的后继包对同一旧包宣告了分叉迁移链。",
                           code="parallel_calibration_package_conflict",
                           target_package_id=target["package_id"],
                           existing_superseding_package_id=owner,
                           requested_superseding_package_id=package["package_id"])
                declared_targets[identity] = package["package_id"]
            retirement = self._persisted_retirement(
                package, hardware_profile_id=hardware["id"],
                calibration_version_id=version["id"])
            # A verified receipt is durable migration state.  If the retired
            # package was absent when the successor was first installed, a
            # later directory downgrade must not resurrect it.  Existing audit
            # records remain in state and never need the retired JSON reinstalled.
            if retirement:
                continue
            installable.append((source, package))
        installed = [self.install_portable_package(package, source=source,
                                                   apply_supersessions=False)
                     for source, package in installable]
        # Apply after every package is materialized.  This makes startup safe
        # even when filenames sort the successor before the retired package.
        for (_, package), result in zip(installable, installed):
            result["supersession_receipts"] = self._apply_supersessions(
                package, result["calibration_version"])
        return installed

    def catalog(self):
        profiles = self.base_model["profiles"]
        hardware_profiles = ([self.hardware] if self.hardware_profile_store is None else
                             self.hardware_profile_store.list(include_archived=False))
        hardware_profiles = [self.decorate_hardware_profile_for_display(profile)
                             for profile in hardware_profiles]
        return {"schema_version": "parallel_calibration_catalog/v1",
            "model_structure": {"name": "保留 Off-CPU 与同步服务依赖的离散事件模型", "status": self.base_model.get("status", "legacy_compat"),
                "formula": "观测账目满足 T_wall(observed) = T_on_cpu(observed) + T_off_cpu(observed)；预测总时延由依赖图和资源约束下的 DAG makespan 给出。命中任务类型频点性能曲线时，T_on_cpu 按目标核组曲线成本 / 模型参考核组曲线成本迁移，不再叠乘旧核型倍率或线性频率弹性；曲线缺失、类别未知或频点越界时才回退旧 ratio × elasticity。非 Binder delay 保留 Trace 时长，同步 blocked 通过请求、服务与回复依赖链表达。",
                "reference_anchor": REFERENCE,
                "stages": [{"id": "observed", "name": "实测账目", "description": "Trace 分离应用 CPU、Runnable、阻塞与服务 CPU，保留请求/返回依赖；校准不覆盖原始观测。"},
                           {"id": "transfer", "name": "On-CPU 条件代价", "description": "按核型、PMU负载分类、类型拐点/分段频响和实时并发数转换应用 CPU 服务率；非 Binder delay 保留观测时长，同步 blocked 由服务依赖链重放。"},
                           {"id": "schedule", "name": "资源调度", "description": "应用物理核心与有限服务通道分别占用；任务完成后更新争用系数和可执行节点。"},
                           {"id": "search", "name": "配置寻优", "description": "枚举 Worker 数、主线程参与、核组及共享 OPP，按最低回调耗时或满足预算的最低频点排序，保留 CPU 成本与敏感性结果。"}],
                "limitations": ["当前为代理负载迁移模型，微基准校准不等同于 ArkUI 或应用并行实测验证。",
                                "原 Trace 只有在源硬件身份、完整 CPU/频率驻留与同一绝对曲线参考系均核验后才反推统一参考工作量；旧数据、部分驻留或曲线缺失时整体回退旧参考锚并报告风险。Off-CPU 保持原始调度与服务依赖，不由微基准拟合。",
                                "服务停留时间来自原始记录；服务容量当前仍为假设，未由 CPU 微基准拟合。",
                                "线程创建、绑核、首次唤醒、join、数据复制、GC 与冷缓存成本尚未独立标定；配对派发基准只覆盖已预热持久 Worker 的逐项投递临界路径增量。",
                                "阶段负载类别可选择实测双任务干扰矩阵；通用 cache miss 不换算 DRAM 字节，类型本身不决定并行收益。",
                                "双任务干扰模型只覆盖实测核组、频点和代理工作集；未知类别、未测组合或三个以上活动CPU任务回退alpha并单独报告。",
                                "按核组迁移不代表不同物理核心共享缓存、内存通道或NUMA位置等价；具体测量放置保留在版本中。",
                                "一个核组共享 OPP，Worker 使用同一核组；应用独立性和线程安全需要额外证据。"]},
            "parameter_catalog": [
                {"id": "core_ratios", "name": "核型 CPU 倍率（兼容回退）", "description": "仅在任务类型频点曲线缺失时使用旧 build_ratio / residual_ratio；不与已校准曲线叠乘。", "current_value": {kind: {key: profiles[kind][key] for key in ("build_ratio", "residual_ratio")} for kind in KINDS}, "method": "旧模型兼容：相同代理和输入规模在各核组最高 OPP 的单位工作量线程 CPU 时间配对归一。", "status": "legacy_proxy_transfer", "calibratable": True},
                {"id": "frequency_response", "name": "线性频率弹性（兼容回退）", "description": "仅在任务类型频点曲线缺失、类别未知或请求频点超出实测域时使用；只作用于On-CPU。", "current_value": {kind: .75 for kind in KINDS}, "method": "旧模型兼容：至少最大/低频两档训练，中间 OPP 留出，对相对 CPU 时间与 max/f−1 做过原点最小二乘拟合。", "status": "sensitivity_assumption", "calibratable": True},
                {"id": "frequency_roofline", "name": "任务类型频点性能曲线", "description": "按核组实测计算、访存、混合任务的 OPP—单位工作量时延曲线；跨核迁移优先使用目标/参考绝对曲线成本比值。", "current_value": "尚未校准；未命中时回退旧核型倍率与线性频率弹性", "method": "每类每核组至少使用五个训练OPP并另留一个未见OPP；保存最高OPP绝对单位成本，拟合连续分段曲线并检查训练残差、跨核锚点和留出误差。", "status": "requires_measurements", "calibratable": True},
                {"id": "classic_roofline", "name": "经典硬件 Roofline", "description": "按实际核组与 OPP 实测计算吞吐屋顶、持续源级带宽斜线和计算强度阶梯；与任务核频时延响应分开落盘。", "current_value": "尚未校准", "method": "固定源级 FLOP/最小字节计数，执行 compute ceiling、bandwidth ceiling 与 intensity ladder；Instruction Roofline 只有在 PMU retired instructions 与 DRAM bytes 同窗对齐后才可发布。", "status": "requires_measurements", "calibratable": True},
                {"id": "contention", "name": "默认并发争用 alpha（兼容回退）", "description": "负载组合干扰矩阵没有匹配类别、核组、频点或并发数时使用的旧膨胀系数。", "current_value": {kind: profiles[kind]["alpha"] for kind in KINDS}, "method": "旧模型兼容：同核组/频率/工作量下，以目标线程共跑 CPU 时间相对单跑膨胀拟合 alpha。", "status": "legacy_proxy_transfer", "calibratable": True},
                {"id": "pairwise_interference", "name": "负载组合干扰矩阵", "description": "计算/访存/混合代理两两共跑，分别测量目标受到干扰者的CPU膨胀；保持方向性。", "current_value": "尚未校准；缺匹配数据时回退alpha", "method": "固定核绑定、双方频点和工作集，测量单跑/共跑CPU；按不同工作量留出验证9种有向负载组合，保存适用域。", "status": "requires_measurements", "calibratable": True},
                {"id": "dispatch_overhead", "name": "Worker 任务派发开销", "description": "持久 Worker 同 payload 的逐项队列路径相对批内直跑路径的每任务临界路径增量，参考核组最高OPP。", "current_value": .05, "method": "v4 使用 direct-batch/queue-dispatch 配对 wall 差分拟合 branch_overhead_ms，并单独保存 worker/producer CPU 差分；不相加且不含冷启动。v1-v3 继续使用 worker CPU/任务兼容拟合。", "status": "assumed", "calibratable": True},
                {"id": "service_capacity", "name": "服务执行通道", "description": "默认1；影响同步服务排队，历史服务CPU与内部等待原样保留。", "current_value": 1, "method": "需要具体系统服务的真实并发请求/返回证据。CPU微基准不提供此参数。", "status": "retained_assumption", "calibratable": False}],
            "benchmark_catalog": [
                {"id": "object_tree_v1", "name": "对象树代理工作", "description": "固定内存中的对象/树访问与整数更新，代表BuildItem类工作特征；以线程CPU时间记录。", "parameters": ["core_ratios", "frequency_response", "contention"]},
                {"id": "integer_mix_v1", "name": "其他 CPU 工作代理", "description": "固定规模整数依赖链，仅用于兼容回退参数的独立标定。", "parameters": ["core_ratios", "frequency_response"]},
                {"id": "class_frequency_sweep_v1", "name": "计算 / 访存 / 混合频点性能扫描", "description": "三个固定整数代理在每个核组至少使用5个训练OPP拟合频点性能曲线，并用第6个未见OPP独立验证；静态源码强度用于解释内核差异，不能当作FLOP、退役指令或DRAM流量。", "parameters": ["frequency_roofline"],
                 "workload_intensity_contracts": {workload_class: pair_work_source_intensity(workload_class)
                                                    for workload_class in WORKLOAD_CLASSES}},
                {"id": "classic_roofline_v1", "name": "计算 / 带宽屋顶与强度阶梯", "description": "浮点计算上限、流式源级带宽和 2/8/32/128/512 次操作强度阶梯，按实际核组与 OPP 实测并跨越 Roofline 拐点。", "parameters": ["classic_roofline"]},
                {"id": "pairwise_proxy_v1", "name": "计算 × 访存 × 混合共跑代理", "description": "整数混合、固定工作集顺序读写、计算访存交错三个内核；目标与持续干扰者放置于独立物理核，双方采样频率。", "parameters": ["pairwise_interference"]},
                {"id": "empty_dispatch_v1", "name": "同负载配对派发", "description": "同一持久worker执行 direct-batch 与逐项队列两条相同payload路径；用往返wall差分刻画分支临界路径，worker/producer CPU差分单独归因。", "parameters": ["dispatch_overhead"]}],
            "hardware_profiles": hardware_profiles, "measurement_contract": measurement_contract(),
            "defaults": {"hardware_profile_id": self.hardware["id"], "parameter_groups": list(DEFAULT_GROUPS), "repeats": 5},
            "publication_policy": "结构、来源、冻结计划、硬件与受管二进制身份完整性属于发布硬门；已有可用实测和估计参数时，统计质量或覆盖门失败仍保存 risk_calibrated 风险版本，并逐项标明失败门禁、证据等级及参数来源。"}

    def create_plan(self, payload):
        if not isinstance(payload, dict): _error("方案应为 JSON 对象。")
        hardware = payload.get("hardware_profile_id", self.hardware["id"])
        context = self._context(hardware)
        model, hardware_record = context["model"], context["hardware"]
        available, reference = context["group_ids"], context["reference_group"]
        groups = payload.get("parameter_groups", list(DEFAULT_GROUPS))
        if not isinstance(groups, list) or not groups or any(value not in ALL_GROUPS for value in groups):
            _error("请选择可校准的参数组。", allowed=list(ALL_GROUPS))
        groups = list(dict.fromkeys(groups))
        kinds = payload.get("core_kinds", available)
        if not isinstance(kinds, list) or not kinds or any(value not in available for value in kinds):
            _error("核组应来自所选硬件画像。", allowed=available)
        selected = set(kinds)
        if any(group in groups for group in ("core_ratios", "dispatch_overhead")):
            selected.add(reference)
        kinds = [kind for kind in available if kind in selected]
        roofline_kinds = []
        if "frequency_roofline" in groups:
            eligible_roofline_kinds = [
                kind for kind in kinds
                if len(set(model["profiles"][kind]["opps_khz"])) >= 6
            ]
            requested_roofline_kinds = payload.get("frequency_roofline_core_kinds")
            if requested_roofline_kinds is None:
                roofline_kinds = eligible_roofline_kinds
            else:
                if (not isinstance(requested_roofline_kinds, list)
                        or any(kind not in kinds for kind in requested_roofline_kinds)):
                    _error("frequency_roofline_core_kinds 应为 core_kinds 的子集。",
                           allowed=eligible_roofline_kinds)
                roofline_kinds = [kind for kind in kinds
                                  if kind in set(requested_roofline_kinds)]
                ineligible = [kind for kind in roofline_kinds
                              if kind not in eligible_roofline_kinds]
                if ineligible:
                    _error("任务类型频点性能曲线仅支持至少6个不同OPP的核组。",
                           ineligible_core_kinds=ineligible,
                           minimum_opp_count=6)
            if not roofline_kinds:
                available_opp_counts = {
                    kind: len(set(model["profiles"][kind]["opps_khz"]))
                    for kind in kinds
                }
                _error("任务类型频点性能曲线要求至少提供6个不同OPP；所选核组均不满足。",
                       core_kinds=kinds, minimum_opp_count=6,
                       available_opp_counts=available_opp_counts,
                       available_opp_count=(available_opp_counts[kinds[0]]
                                            if len(kinds) == 1 else None))
        elif payload.get("frequency_roofline_core_kinds") not in (None, []):
            _error("未选择任务类型频点性能曲线时，不应指定 frequency_roofline_core_kinds。")
        roofline_kind_set = set(roofline_kinds)
        physical_cpu_count = sum(context["groups"][kind]["physical_core_count"] for kind in available)
        if physical_cpu_count < 2:
            _error("真机校准需要至少两个独立物理核，以保留目标核与观测核。",
                   hardware_profile_id=hardware, physical_core_count=physical_cpu_count)
        if "contention" in groups and physical_cpu_count < 4:
            _error("并发争用校准需要至少四个独立物理核，以执行三任务留出并保留观测核。",
                   hardware_profile_id=hardware, physical_core_count=physical_cpu_count)
        repeats = _integer(payload.get("repeats", 5), "repeats", 3, 30)
        units = _integer(payload.get("work_units", 2000000), "work_units", 10000, 1000000000)
        requested = payload.get("frequencies_khz", {})
        if not isinstance(requested, dict) or any(key not in available for key in requested):
            _error("frequencies_khz 应为硬件画像核组到 OPP 数组的映射。", allowed=available)
        frequencies, conditions, roofline_frequency_split = {}, [], {}

        def add(benchmark, family, kind, frequency, tasks, count, split):
            condition = {"benchmark": benchmark, "family": family, "workload_id": "empty_dispatch_v1" if benchmark == "empty_dispatch" else "object_tree_v1" if family == "build" else "integer_mix_v1", "core_kind": kind, "frequency_khz": frequency, "active_tasks": tasks, "work_units": count, "split": split, "repeats": repeats if split == "train" else 2}
            condition["id"] = "condition_" + _hash(condition)[:12]
            conditions.append(condition)

        for kind in kinds:
            profile = model["profiles"][kind]
            opps = profile["opps_khz"]
            roofline_enabled = kind in roofline_kind_set
            frequency_sweep = "frequency_response" in groups or roofline_enabled
            if roofline_enabled:
                default = [opps[round(index * (len(opps) - 1) / 5)] for index in range(6)]
            elif "classic_roofline" in groups:
                # A classic Roofline is independently measurable at one OPP.
                # Prefer low/mid/high for the frequency explorer, but do not
                # invent a three-OPP requirement on hardware exposing fewer
                # operating points.
                default = [opps[0], opps[len(opps) // 2], profile["max_khz"]]
            else:
                default = [opps[0], opps[len(opps) // 2], profile["max_khz"]] if frequency_sweep else [profile["max_khz"]]
            values = requested.get(kind, default)
            if not isinstance(values, list) or not values or any(type(value) is not int or value not in opps for value in values): _error("方案频点应在硬件 OPP 目录内。", core_kind=kind)
            values = sorted(set([opps[0], *values, profile["max_khz"]])) if roofline_enabled else sorted(set([*values, profile["max_khz"]]))
            if frequency_sweep and len(values) < 3: _error("频率响应/拐点方案需至少3个实测频点。", core_kind=kind)
            if roofline_enabled and len(values) < 6:
                _error("任务类型拐点拟合需至少6个实测OPP（5个训练、1个独立留出），并覆盖硬件最低/最高频。", core_kind=kind)
            frequencies[kind] = values
            if roofline_enabled:
                holdout_frequency = values[len(values) // 2]
                roofline_frequency_split[kind] = {
                    "train_khz": [frequency for frequency in values if frequency != holdout_frequency],
                    "validation_khz": [holdout_frequency],
                }
            if any(group in groups for group in ("core_ratios", "frequency_response", "contention")):
                families = ("build", "residual") if any(group in groups for group in ("core_ratios", "frequency_response")) else ("build",)
                for family in families:
                    for frequency in values:
                        add("cpu_work", family, kind, frequency, 1, units, "validation" if "frequency_response" in groups and frequency not in (values[0], values[-1]) else "train")
                    add("cpu_work", family, kind, profile["max_khz"], 1, units * 2, "validation")
                if "contention" in groups:
                    add("cpu_work", "build", kind, profile["max_khz"], 2, units, "train")
                    add("cpu_work", "build", kind, profile["max_khz"], 3, units, "validation")
            if roofline_enabled:
                roofline_units = _integer(payload.get("roofline_work_units", payload.get("pair_work_units", 100000)),
                                          "roofline_work_units", 1000, 100000000)
                roofline_working_set = _integer(payload.get("roofline_working_set_bytes", payload.get("working_set_bytes", 64 * 1024 * 1024)),
                                                "roofline_working_set_bytes", 1024 * 1024, 512 * 1024 * 1024)
                if roofline_working_set % 8192: _error("Roofline工作集应按8 KiB完整访存块对齐。")
                train_frequencies = roofline_frequency_split[kind]["train_khz"]
                validation_frequency = roofline_frequency_split[kind]["validation_khz"][0]
                for workload_class in WORKLOAD_CLASSES:
                    for frequency in train_frequencies:
                        row = {"benchmark": "pair_work", "family": "build",
                               "workload_id": workload_class + "_frequency_proxy_v1",
                               "workload_class": workload_class, "core_kind": kind,
                               "frequency_khz": frequency, "active_tasks": 1,
                               "peer_workload_class": "none", "peer_core_kind": "none",
                               "peer_frequency_khz": 0, "working_set_bytes": roofline_working_set,
                               "work_units": roofline_units, "split": "train", "repeats": repeats}
                        row["workload_intensity"] = pair_work_source_intensity(
                            workload_class, work_units=roofline_units,
                            working_set_bytes=roofline_working_set)
                        row["id"] = "condition_" + _hash(row)[:12]
                        conditions.append(row)
                    row = {"benchmark": "pair_work", "family": "build",
                           "workload_id": workload_class + "_frequency_proxy_v1",
                           "workload_class": workload_class, "core_kind": kind,
                           "frequency_khz": validation_frequency, "active_tasks": 1,
                           "peer_workload_class": "none", "peer_core_kind": "none",
                           "peer_frequency_khz": 0, "working_set_bytes": roofline_working_set,
                           "work_units": roofline_units * 2, "split": "validation", "repeats": 2}
                    row["workload_intensity"] = pair_work_source_intensity(
                        workload_class, work_units=roofline_units * 2,
                        working_set_bytes=roofline_working_set)
                    row["id"] = "condition_" + _hash(row)[:12]
                    conditions.append(row)
            if "classic_roofline" in groups:
                working_set = _integer(payload.get("classic_roofline_working_set_bytes", 64 * 1024 * 1024),
                                       "classic_roofline_working_set_bytes", 1024 * 1024, 512 * 1024 * 1024)
                if working_set % 24:
                    aligned_up = working_set + (24 - working_set % 24)
                    working_set = aligned_up if aligned_up <= 512 * 1024 * 1024 else working_set - working_set % 24
                intensity_operations = payload.get(
                    "classic_roofline_operations_per_element", [2, 8, 32, 128, 512])
                if (not isinstance(intensity_operations, list) or not intensity_operations
                        or any(type(value) is not int or value < 2 or value > 4096 or value % 2 for value in intensity_operations)):
                    _error("经典 Roofline 计算强度阶梯应为 2 至 4096 范围内的偶数数组。")
                intensity_operations = list(dict.fromkeys(intensity_operations))
                for frequency in values:
                    memory_work_units = max(1, working_set // 24) * 4
                    memory_work_units = max(
                        memory_work_units,
                        (classic_roofline.MIN_STREAMED_BYTES + 23) // 24,
                    )
                    rows = [("compute_ceiling", classic_roofline.COMPUTE_CEILING_WORK_UNITS, 32),
                            ("bandwidth_ceiling", memory_work_units, 2),
                            ("instruction_proxy", memory_work_units, 8)]
                    rows.extend(("intensity_ladder", memory_work_units, operations)
                                for operations in intensity_operations)
                    for role, work_units, operations in rows:
                        # Compute and bandwidth ceilings directly estimate the
                        # two roofs.  The intensity ladder is a different
                        # kernel family, so keep it out of fitting and use it
                        # as an independent cross-kernel ceiling check.
                        split = "validation" if role == "intensity_ladder" else "train"
                        row = {"benchmark": "roofline", "family": "roofline",
                               "workload_id": f"classic_{role}_{operations}_v1",
                               "workload_class": role, "core_kind": kind,
                               "frequency_khz": frequency, "active_tasks": 1,
                               "working_set_bytes": working_set,
                               "roofline_kernel_version": classic_roofline.KERNEL_VERSION,
                               "roofline_operations_per_element": operations,
                               "work_units": work_units, "split": split,
                               "repeats": 2 if split == "validation" else repeats}
                        row["id"] = "condition_" + _hash(row)[:12]
                        conditions.append(row)
        if "dispatch_overhead" in groups:
            add("empty_dispatch", "residual", reference, model["profiles"][reference]["max_khz"], 1, 10000, "train")
            add("empty_dispatch", "residual", reference, model["profiles"][reference]["max_khz"], 1, 20000, "validation")
        placements = []
        if "pairwise_interference" in groups:
            if physical_cpu_count < 3:
                _error("双任务干扰校准需要至少三个独立物理核，以保留目标、干扰与观测核。",
                       hardware_profile_id=hardware, physical_core_count=physical_cpu_count)
            same_group = [kind for kind in kinds if context["groups"][kind]["physical_core_count"] >= 2]
            preferred = [kind for kind in same_group if context["groups"][kind].get("proxy_kind") == "middle"]
            if preferred or same_group:
                default_victim = (preferred or same_group)[0]
                default_placements = [{"victim_core_kind": default_victim, "peer_core_kind": default_victim}]
            elif len(kinds) >= 2:
                default_placements = [{"victim_core_kind": kinds[0], "peer_core_kind": kinds[1]}]
            else:
                _error("所选核组无法提供两个独立物理核用于双任务干扰校准。")
            placements = payload.get("interference_placements", default_placements)
            if not isinstance(placements, list) or not 1 <= len(placements) <= 20: _error("共跑放置应为1至20个核组/频点组合。")
            pair_units = _integer(payload.get("pair_work_units", 100000), "pair_work_units", 1000, 100000000)
            working_set = _integer(payload.get("working_set_bytes", 64 * 1024 * 1024), "working_set_bytes", 1024 * 1024, 512 * 1024 * 1024)
            if working_set % 8192: _error("工作集应按8 KiB完整访存块对齐。")
            normalized, pair_conditions = [], {}
            for placement in placements:
                if not isinstance(placement, dict): _error("共跑放置应为对象。")
                victim, peer = placement.get("victim_core_kind"), placement.get("peer_core_kind")
                if victim not in kinds or peer not in kinds: _error("共跑核组需要在方案core_kinds内。")
                if victim == peer and context["groups"][victim]["physical_core_count"] < 2:
                    _error("同核组双任务校准需要至少两个独立物理核。", core_kind=victim)
                vf = placement.get("victim_frequency_khz", model["profiles"][victim]["max_khz"])
                pf = placement.get("peer_frequency_khz", model["profiles"][peer]["max_khz"])
                for kind, frequency in ((victim, vf), (peer, pf)):
                    if type(frequency) is not int or frequency not in model["profiles"][kind]["opps_khz"]: _error("共跑频率不在核组OPP目录内。")
                    frequencies[kind] = sorted(set(frequencies[kind] + [frequency]))
                if victim == peer and vf != pf: _error("同核组共享OPP，共跑双方频率需要相同。")
                normalized.append(dict(victim_core_kind=victim, peer_core_kind=peer, victim_frequency_khz=vf, peer_frequency_khz=pf))
                for workload in CLASSES:
                    for peer_class in ("none", *CLASSES):
                        for split, count in (("train", pair_units), ("validation", pair_units * 2)):
                            row = {"benchmark": "pair_work", "family": "build", "workload_id": workload + "_proxy_v1", "workload_class": workload,
                                   "core_kind": victim, "frequency_khz": vf, "active_tasks": 1 if peer_class == "none" else 2,
                                   "peer_workload_class": peer_class, "peer_core_kind": "none" if peer_class == "none" else peer,
                                   "peer_frequency_khz": 0 if peer_class == "none" else pf, "working_set_bytes": working_set,
                                   "work_units": count, "split": split, "repeats": repeats if split == "train" else 2}
                            row["workload_intensity"] = pair_work_source_intensity(
                                workload, work_units=count,
                                working_set_bytes=working_set)
                            if peer_class != "none":
                                # The helper runs until the target completes, so
                                # only its per-unit static contract is frozen;
                                # total helper units remain measured evidence.
                                row["peer_workload_intensity"] = pair_work_source_intensity(
                                    peer_class, working_set_bytes=working_set)
                            row["id"] = "condition_" + _hash(row)[:12]
                            pair_conditions[row["id"]] = row
            placements = normalized
            conditions.extend(pair_conditions.values())
        cpu_map = {kind: copy.deepcopy(context["groups"][kind].get("physical_core_representative_cpu_ids")
                                       or context["groups"][kind]["logical_cpu_ids"][:context["groups"][kind]["physical_core_count"]])
                   for kind in available}
        frequency_policy_ids = {}
        for kind in available:
            policy_id = context["groups"][kind].get("source_policy_id") or f"cpu{cpu_map[kind][0]}"
            if not isinstance(policy_id, str) or not re.fullmatch(r"(?:policy|cpu)\d{1,4}", policy_id):
                _error("硬件画像包含无效的 CPU 频率域标识，请重新从当前设备提取画像。",
                       core_group_id=kind, source_policy_id=policy_id)
            frequency_policy_ids[kind] = policy_id
        record = {"schema_version": "parallel_calibration_plan/v1",
                  "name": str(payload.get("name") or hardware_record.get("name", "真机") + " · 性能模型校准")[:200],
                  "status": "planned", "hardware_profile_id": hardware, "hardware_profile_sha256": hardware_record.get("profile_sha256"),
                  "parameter_groups": groups, "core_kinds": kinds,
                  "frequency_roofline_core_kinds": roofline_kinds,
                  "reference_core_kind": reference,
                  "repeats": repeats, "frequencies_khz": frequencies, "conditions": conditions,
                  "expected_sample_count": sum(item["repeats"] for item in conditions),
                  "base_model_id": model["id"], "base_model_sha256": _hash(model),
                  "reference_anchor": context["reference_anchor"], "measurement_contract": measurement_contract(),
                  "quality_thresholds": THRESHOLDS, "cpu_map": cpu_map,
                  "frequency_policy_ids": frequency_policy_ids,
                  "cpu_map_note": "由所选硬件画像冻结；每个物理核心仅保留一个逻辑 CPU 代表，Desktop 执行前再次核验设备画像。"}
        if roofline_frequency_split:
            record["frequency_roofline_split"] = roofline_frequency_split
        if placements: record.update(interference_placements=placements, interference_model_schema=INTERFERENCE_SCHEMA)
        record.update(id="cp_" + _hash(record)[:24], created_at=utc_now())
        return self._record("plans", record)

    def bundle(self, plan_id):
        plan = self.get_plan(plan_id)
        empty = {"schema_version": MEASUREMENT_SCHEMA, "plan_id": plan_id, "hardware_profile_id": plan["hardware_profile_id"], "provenance": {"device_id": "", "os_build": "", "collector": "parallel_calibration_collect/1.0.0", "measured_at": "", "source_kind": "measured"}, "samples": []}
        files = [{"filename": "plan.json", "media_type": "application/json", "content": json.dumps(plan, ensure_ascii=False, indent=2)},
                 {"filename": "measurements-template.json", "media_type": "application/json", "content": json.dumps(empty, ensure_ascii=False, indent=2)},
                 {"filename": "measurement-contract.json", "media_type": "application/json", "content": json.dumps(measurement_contract(), ensure_ascii=False, indent=2)}]
        for name, mime in (("benchmark.c", "text/x-c"), ("collect.py", "text/x-python"), ("README.md", "text/markdown")):
            path = ASSET_ROOT / name
            if not path.is_file():
                raise PlatformError("parallel_calibration_asset_missing", "校准基准资源尚未部署。", status_code=503, details={"filename": name})
            files.append({"filename": name, "media_type": mime, "content": path.read_text(encoding="utf-8")})
        for file in files: file["sha256"] = hashlib.sha256(file["content"].encode()).hexdigest()
        return {"schema_version": "parallel_calibration_bundle/v1", "plan_id": plan_id, "files": files}

    def fit(self, payload):
        if not isinstance(payload, dict): _error("拟合输入应为 JSON 对象。")
        plan = self.get_plan(payload.get("plan_id"))
        context = self._context(plan["hardware_profile_id"])
        base_model = context["model"]
        reference = plan.get("reference_core_kind", context["reference_group"])
        reference_required = any(group in plan.get("parameter_groups", []) for group in ("core_ratios", "dispatch_overhead"))
        if ((reference_required and reference not in plan.get("core_kinds", []))
                or any(kind not in base_model["profiles"] for kind in plan.get("core_kinds", []))):
            _error("校准方案核组与当前硬件画像不一致，请重新生成方案。")
        if plan.get("hardware_profile_sha256") not in (None, context["hardware"].get("profile_sha256")):
            _error("校准方案绑定的硬件画像内容已变化，请重新生成方案。")
        if plan.get("base_model_sha256") != _hash(base_model):
            _error("校准方案的初始模型与当前硬件画像不一致，请重新生成方案。")
        roofline_kinds = []
        if "frequency_roofline" in plan.get("parameter_groups", []):
            # v1 plans created before per-core capability selection implicitly
            # applied the curve sweep to every selected core kind.  New plans
            # freeze the eligible subset explicitly so that a five-OPP cluster
            # cannot cancel a valid sweep on an eight-OPP cluster.
            roofline_kinds = plan.get("frequency_roofline_core_kinds",
                                      plan.get("core_kinds", []))
            if (not isinstance(roofline_kinds, list) or not roofline_kinds
                    or len(set(roofline_kinds)) != len(roofline_kinds)
                    or any(kind not in plan.get("core_kinds", [])
                           for kind in roofline_kinds)):
                _error("拐点校准方案的 frequency_roofline_core_kinds 必须是非空 core_kinds 子集。")
            for kind in roofline_kinds:
                if len(set(base_model["profiles"][kind]["opps_khz"])) < 6:
                    _error("拐点校准核组至少需要6个不同OPP。", core_kind=kind)
            split = plan.get("frequency_roofline_split")
            if not isinstance(split, dict):
                _error("拐点校准方案缺少独立训练/留出频点合同，请重新生成方案。")
            if set(split) != set(roofline_kinds):
                _error("拐点训练/留出频点合同必须与 frequency_roofline_core_kinds 完全一致。")
            for kind in roofline_kinds:
                partition = split.get(kind) if isinstance(split.get(kind), dict) else {}
                train_frequencies = partition.get("train_khz")
                validation_frequencies = partition.get("validation_khz")
                if (not isinstance(train_frequencies, list) or len(set(train_frequencies)) < 5
                        or not isinstance(validation_frequencies, list) or not validation_frequencies
                        or set(train_frequencies) & set(validation_frequencies)):
                    _error("拐点校准方案要求至少5个训练OPP和不重叠的独立留出OPP。", core_kind=kind)
                profile = base_model["profiles"][kind]
                if profile["opps_khz"][0] not in train_frequencies or profile["max_khz"] not in train_frequencies:
                    _error("拐点训练频点必须覆盖硬件最低和最高OPP。", core_kind=kind)
                selected = set(plan["frequencies_khz"].get(kind, []))
                if not (set(train_frequencies) | set(validation_frequencies)) <= selected:
                    _error("拐点训练/留出频点与冻结方案频点不一致。", core_kind=kind)
                for workload_class in WORKLOAD_CLASSES:
                    class_conditions = [row for row in plan["conditions"]
                                        if row.get("benchmark") == "pair_work"
                                        and row.get("workload_id") == workload_class + "_frequency_proxy_v1"
                                        and row.get("core_kind") == kind and row.get("active_tasks") == 1]
                    observed_train = {row["frequency_khz"] for row in class_conditions if row.get("split") == "train"}
                    observed_validation = {row["frequency_khz"] for row in class_conditions if row.get("split") == "validation"}
                    if observed_train != set(train_frequencies) or observed_validation != set(validation_frequencies):
                        _error("拐点条件未严格实现冻结的训练/留出频点划分。",
                               core_kind=kind, workload_class=workload_class)
        # New plans freeze an auditable source-kernel intensity contract.  Old
        # signed v6 packages remain readable; when the field exists it must be
        # exactly reproducible from the frozen workload class/size/count.
        for condition in plan.get("conditions", []):
            if condition.get("benchmark") != "pair_work":
                continue
            expected_intensity = pair_work_source_intensity(
                condition.get("workload_class"),
                work_units=condition.get("work_units"),
                working_set_bytes=condition.get("working_set_bytes"),
            )
            if ("workload_intensity" in condition
                    and condition.get("workload_intensity") != expected_intensity):
                _error("pair_work 冻结条件的源码强度合同与工作负载定义不一致。",
                       condition_id=condition.get("id"))
            peer_class = condition.get("peer_workload_class")
            if peer_class not in (None, "none"):
                expected_peer = pair_work_source_intensity(
                    peer_class, working_set_bytes=condition.get("working_set_bytes"))
                if ("peer_workload_intensity" in condition
                        and condition.get("peer_workload_intensity") != expected_peer):
                    _error("pair_work 干扰负载的源码强度合同与工作负载定义不一致。",
                           condition_id=condition.get("id"))
        measurements = payload.get("measurements")
        if not isinstance(measurements, dict) or measurements.get("schema_version") != MEASUREMENT_SCHEMA: _error("测量文件版本应为 " + MEASUREMENT_SCHEMA)
        if measurements.get("hardware_profile_id") != plan["hardware_profile_id"]: _error("测量硬件配置与方案不一致。")
        if measurements.get("plan_id") not in (None, plan["id"]): _error("测量文件属于另一个校准方案。")
        provenance = measurements.get("provenance")
        if not isinstance(provenance, dict): _error("请提供实际测量来源信息。")
        if provenance.get("source_kind") != "measured": _error("拟合只接受实测来源；示例或合成数据不发布校准版本。")
        if any(container.get(field) is True for container in (measurements, provenance)
               for field in ("fixture_only", "synthetic", "is_synthetic", "test_fixture")):
            _error("明确标记为示例或合成的数据不能用于校准发布。")
        for field in ("device_id", "os_build", "collector", "measured_at"): _string(provenance.get(field), "provenance." + field, 500)
        placement_benchmarks = {condition.get("benchmark") for condition in plan["conditions"]}
        if placement_benchmarks.intersection({"pair_work", "roofline"}):
            cpu_map = provenance.get("cpu_map")
            required_groups = plan["core_kinds"]
            if not isinstance(cpu_map, dict) or any(not isinstance(cpu_map.get(kind), list) or any(type(cpu) is not int or not 0 <= cpu < 4096 for cpu in cpu_map[kind]) for kind in required_groups):
                _error("频响/共跑/Roofline校准需在provenance.cpu_map记录每核组的物理核代表CPU数组。")
            if cpu_map != plan.get("cpu_map"):
                _error("实测CPU映射与冻结校准方案不一致，请重新生成方案或在原设备拓扑上采集。")
            mapped_cpus = [cpu for values in cpu_map.values() for cpu in values]
            if len(mapped_cpus) != len(set(mapped_cpus)): _error("物理核代表CPU不能重复映射到不同核组。")
        else:
            cpu_map, mapped_cpus = {}, []
        samples = measurements.get("samples")
        if not isinstance(samples, list) or len(samples) > MAX_SAMPLES: _error("samples 应为最多20000条样本的数组。")
        # Canonical encoding rejects NaN/Infinity even in uninterpreted provenance.
        try: digest = _hash(measurements)
        except (ValueError, TypeError): _error("测量文件包含无效的 JSON 数字或类型。")
        expected = {_condition(row): row for row in plan["conditions"]}
        accepted, rejected, seen, run_splits = [], [], set(), defaultdict(set)
        for index, sample in enumerate(samples):
            try:
                if not isinstance(sample, dict): _error("样本应为对象。")
                identity = _string(sample.get("sample_id"), "sample_id")
                if identity in seen: _error("sample_id 重复。")
                seen.add(identity)
                _string(sample.get("run_id"), "run_id")
                if sample.get("split") not in ("train", "validation"): _error("split 应为 train 或 validation。")
                for field in ("benchmark", "family", "workload_id", "core_kind"): _string(sample.get(field), field)
                _integer(sample.get("frequency_khz"), "frequency_khz", 1, 10000000)
                _integer(sample.get("active_tasks"), "active_tasks", 1, 8)
                _integer(sample.get("work_units"), "work_units", 1, 2000000000)
                _integer(sample.get("repeat"), "repeat", 0, 10000)
                _validate_paired_dispatch(sample)
                _validate_optional_benchmark_pmu(sample)
                if sample.get("workload_version") != PAIRED_DISPATCH_WORKLOAD_VERSION:
                    _error("生产拟合只接受 parallel-calibration-proxy-v6 实测样本。")
                condition = expected.get(_condition(sample))
                if condition is None or sample["split"] != condition["split"]: _error("样本条件或训练/验证划分不在冻结方案中。")
                _number(sample.get("cpu_ms"), "cpu_ms", THRESHOLDS["minimum_cpu_ms"], 3600000)
                _number(sample.get("wall_ms"), "wall_ms", .001, 3600000)
                if sample["cpu_ms"] > sample["wall_ms"] * 1.02: _error("目标线程CPU时间超过对应wall时间。")
                if sample.get("affinity_verified") is not True: _error("样本未核验落核。")
                _validate_target_sibling_quietness(sample)
                _validate_per_cpu_frequency_source(
                    sample, prefix="", expected_cpu=sample["cpu"], label="目标线程")
                _validate_adaptive_frequency_residency(sample)
                _validate_frequency_window(
                    sample, prefix="", requested_khz=sample["frequency_khz"], label="目标线程")
                if sample["active_tasks"] > 1 and sample.get("physical_core_placement_verified") is not True:
                    _error("共跑样本未核验目标与干扰者位于不同物理核心。")
                if sample["active_tasks"] > 1 and sample.get("concurrency_verified") is not True:
                    _error("共跑样本未核验干扰者全程覆盖目标且持续占用CPU。")
                if sample["benchmark"] == "pair_work":
                    cpu_map = provenance.get("cpu_map", {})
                    cpu = sample.get("cpu")
                    if type(cpu) is not int or cpu not in cpu_map.get(sample["core_kind"], []): _error("共跑代理缺少目标CPU与核组的实测放置映射。")
                    if sample.get("observer_independent_core") is not True: _error("共跑代理采样线程需位于独立物理核。")
                    observer = _integer(sample.get("observer_cpu"), "observer_cpu", 0, 4095)
                    if observer == cpu or observer not in mapped_cpus:
                        _error("共跑代理需使用冻结CPU映射中的独立采样CPU编号。")
                    if sample["active_tasks"] == 2:
                        helpers = sample.get("helpers")
                        if not isinstance(helpers, list) or len(helpers) != 1 or not isinstance(helpers[0], dict): _error("双任务代理应包含一个干扰线程的实际放置。")
                        peer_cpu_requested = _integer(
                            helpers[0].get("cpu"), "helpers[0].cpu", 0, 4095)
                        peer_cpu = _integer(
                            helpers[0].get("actual_cpu"), "helpers[0].actual_cpu", 0, 4095)
                        if helpers[0].get("affinity_verified") is not True:
                            _error("干扰线程缺少逐线程实际落核核验。")
                        if peer_cpu != peer_cpu_requested:
                            _error("干扰线程实际逻辑CPU与请求逻辑CPU不一致。")
                        if peer_cpu == cpu or peer_cpu not in cpu_map.get(sample["peer_core_kind"], []): _error("干扰线程CPU与方案核组不匹配。")
                        if peer_cpu == sample["observer_cpu"]: _error("干扰者与采样线程不能共用CPU。")
                        _validate_per_cpu_frequency_source(
                            sample, prefix="peer_", expected_cpu=peer_cpu, label="干扰线程")
                        _validate_frequency_window(
                            sample, prefix="peer_",
                            requested_khz=sample["peer_frequency_khz"], label="干扰线程")
                if sample["benchmark"] == "roofline":
                    if sample.get("workload_class") not in classic_roofline.ROLES:
                        _error("经典 Roofline 样本包含未知内核角色。")
                    _integer(sample.get("working_set_bytes"), "working_set_bytes", 1024 * 1024, 512 * 1024 * 1024)
                    _integer(sample.get("roofline_operations_per_element"), "roofline_operations_per_element", 2, 4096)
                    if sample.get("working_set_coverage_verified") is not True:
                        _error("经典 Roofline 样本没有覆盖冻结工作集。")
                    cpu = sample.get("cpu")
                    if type(cpu) is not int or cpu not in cpu_map.get(sample["core_kind"], []):
                        _error("经典 Roofline 样本缺少目标CPU与核组的实测放置映射。")
                    observer = _integer(sample.get("observer_cpu"), "observer_cpu", 0, 4095)
                    if observer not in mapped_cpus or observer == cpu:
                        _error("经典 Roofline 观测线程必须使用冻结CPU映射中的另一个物理核。")
                    if sample.get("observer_independent_core") is not True:
                        _error("经典 Roofline 观测线程需位于独立物理核。")
                binary = sample.get("binary_sha256", provenance.get("binary_sha256"))
                if not isinstance(binary, str) or not re.fullmatch(r"[a-fA-F0-9]{64}", binary):
                    _error("样本缺少实际二进制的 SHA-256。")
                if sample.get("thermal_throttled") is not False: _error("样本存在热降频或温度状态未记录。")
                if sample["benchmark"] in THERMAL_EVIDENCE_BENCHMARKS:
                    _validate_thermal_snapshot(sample.get("thermal_snapshot_before"),
                                               "thermal_snapshot_before")
                    _validate_thermal_snapshot(sample.get("thermal_snapshot_after"),
                                               "thermal_snapshot_after")
                if sample.get("data_kind", "measured") != "measured" or any(sample.get(field) is True for field in ("fixture_only", "synthetic", "is_synthetic", "test_fixture")):
                    _error("样本不是实测数据。")
                run_splits[sample["run_id"]].add(sample["split"])
                normalized = {**copy.deepcopy(sample), "binary_sha256": binary.lower()}
                # Static workload semantics come from the hash-frozen plan,
                # not from an untrusted measurement producer.  Preserve them
                # beside each accepted sample for audit and visualization.
                for field in ("workload_intensity", "peer_workload_intensity"):
                    if field in condition:
                        normalized[field] = copy.deepcopy(condition[field])
                accepted.append(normalized)
            except (PlatformError, KeyError) as exc:
                rejected.append({"index": index, "sample_id": sample.get("sample_id") if isinstance(sample, dict) else None, "reason": str(exc)})
        leakage = [identity for identity, splits in run_splits.items() if len(splits) > 1]
        errors, warnings, missing, integrity_errors = [], [], [], []
        if len({row["binary_sha256"] for row in accepted}) > 1:
            message = "训练与验证混用了不同二进制构建；请按相同构建分别校准。"
            errors.append(message); integrity_errors.append(message)
        dispatch_rows = [row for row in accepted if row["benchmark"] == "empty_dispatch"]
        if dispatch_rows and {bool(_is_paired_dispatch(row)) for row in dispatch_rows} == {False, True}:
            message = "Worker 投递训练与留出不能混用 v4 配对 wall 差分和 v1-v3 worker CPU 口径。"
            errors.append(message); integrity_errors.append(message)
        if leakage:
            message = "训练与验证共用完整运行 ID：" + ", ".join(leakage[:5])
            errors.append(message); integrity_errors.append(message)
        if rejected: warnings.append(f"{len(rejected)} 条样本未通过来源/数值/实验条件检查，已排除。")
        by_condition = defaultdict(list)
        for row in accepted: by_condition[_condition(row)].append(row)
        repeat_cvs = []
        for condition, definition in expected.items():
            rows = by_condition[condition]
            required = plan["repeats"] if definition["split"] == "train" else 2
            if len({row["run_id"] for row in rows}) < required:
                missing.append(f"{definition['id']} 需要{required}次独立{definition['split']}运行，现有{len({row['run_id'] for row in rows})}次。")
            values = [_repeat_unit_metric(row) for row in rows]
            if len(values) > 1:
                mean = statistics.mean(values)
                if mean <= 0:
                    errors.append(f"{definition['id']} 的单位工作量实测平均成本必须为正数。")
                else:
                    repeat_cvs.append(statistics.stdev(values) / mean)
        train = [row for row in accepted if row["split"] == "train"]
        validation = [row for row in accepted if row["split"] == "validation"]
        model = copy.deepcopy(base_model)
        model["hardware_profile_sha256"] = plan.get("hardware_profile_sha256")
        if "frequency_roofline" in plan["parameter_groups"]:
            # A proxy profile can inherit coefficients from another hardware
            # model.  Never carry a frequency curve into a core group whose
            # own OPP catalog cannot satisfy this experiment's 5+1 contract.
            for kind in plan["core_kinds"]:
                if (kind not in roofline_kinds
                        and len(set(model["profiles"][kind]["opps_khz"])) < 6):
                    model["profiles"][kind].pop("frequency_roofline", None)
        for profile in model["profiles"].values(): profile["frequency_elasticity"] = .75
        model["simulation_parameters"] = {"frequency_elasticity": None, "contention": True, "service_capacity": 1, "branch_overhead_ms": .05}
        estimates, calibrated = [], []
        roofline_training_mapes, roofline_training_p90s = [], []
        anchors = {}
        for kind in plan["core_kinds"]:
            for family in ("build", "residual"):
                rows = [row for row in train if row["benchmark"] == "cpu_work" and row["core_kind"] == kind and row["family"] == family and row["active_tasks"] == 1 and row["frequency_khz"] == model["profiles"][kind]["max_khz"]]
                if rows: anchors[(kind, family)] = statistics.median(row["cpu_ms"] / row["work_units"] for row in rows)

        def estimate(group, path, value, rows, method, fit_residual=None):
            item = {"parameter": group, "path": path, "value": value, "method": method, "sample_count": len(rows), "status": "estimated_from_measurements"}
            if fit_residual is not None: item["training_mape"] = fit_residual
            estimates.append(item); calibrated.append(path)

        def is_roofline_row(row):
            return (row.get("benchmark") == "pair_work"
                    and str(row.get("workload_id", "")).endswith("_frequency_proxy_v1"))

        # A frequency curve is a within-placement comparison.  Changing the
        # target or observer CPU between the fitted and held-out OPPs aliases
        # core placement noise into the frequency response, so keep the full
        # train/validation domain fixed for every core-kind/class curve.
        roofline_placement_domains = []
        if "frequency_roofline" in plan["parameter_groups"]:
            for kind in roofline_kinds:
                for workload_class in WORKLOAD_CLASSES:
                    rows = [row for row in accepted if is_roofline_row(row)
                            and row["core_kind"] == kind
                            and row["workload_class"] == workload_class
                            and row["active_tasks"] == 1]
                    targets = sorted({row["cpu"] for row in rows})
                    observers = sorted({row["observer_cpu"] for row in rows})
                    working_sets = sorted({row["working_set_bytes"] for row in rows})
                    consistent = len(targets) == len(observers) == len(working_sets) == 1
                    roofline_placement_domains.append({
                        "core_kind": kind, "workload_class": workload_class,
                        "target_cpus": targets, "observer_cpus": observers,
                        "working_set_bytes": working_sets, "consistent": consistent,
                    })
                    if rows and len(targets) != 1:
                        message = f"{kind}/{workload_class} 拐点训练与留出必须固定同一目标CPU。"
                        errors.append(message); integrity_errors.append(message)
                    if rows and len(observers) != 1:
                        message = f"{kind}/{workload_class} 拐点训练与留出必须固定同一观测CPU。"
                        errors.append(message); integrity_errors.append(message)
                    if rows and len(working_sets) != 1:
                        message = f"{kind}/{workload_class} 拐点训练与留出必须固定同一工作集。"
                        errors.append(message); integrity_errors.append(message)

        classic_placement_domains = []
        if "classic_roofline" in plan["parameter_groups"]:
            for kind in plan["core_kinds"]:
                rows = [row for row in accepted
                        if row["benchmark"] == "roofline" and row["core_kind"] == kind]
                targets = sorted({row["cpu"] for row in rows})
                observers = sorted({row["observer_cpu"] for row in rows})
                working_sets = sorted({row["working_set_bytes"] for row in rows})
                consistent = len(targets) == len(observers) == len(working_sets) == 1
                classic_placement_domains.append({
                    "core_kind": kind, "target_cpus": targets,
                    "observer_cpus": observers, "working_set_bytes": working_sets,
                    "consistent": consistent,
                })
                if rows and len(targets) != 1:
                    message = f"{kind} 经典 Roofline 跨OPP测量必须固定同一目标CPU。"
                    errors.append(message); integrity_errors.append(message)
                if rows and len(observers) != 1:
                    message = f"{kind} 经典 Roofline 跨OPP测量必须固定同一观测CPU。"
                    errors.append(message); integrity_errors.append(message)
                if rows and len(working_sets) != 1:
                    message = f"{kind} 经典 Roofline 跨OPP测量必须固定同一工作集。"
                    errors.append(message); integrity_errors.append(message)

        def solo_key(row):
            return row["workload_class"], row["core_kind"], row["frequency_khz"], row["working_set_bytes"], row["cpu"], row["observer_cpu"]

        for kind in plan["core_kinds"]:
            profile = model["profiles"][kind]
            if "core_ratios" in plan["parameter_groups"]:
                for family in ("build", "residual"):
                    if (kind, family) in anchors and (reference, family) in anchors:
                        value = anchors[(kind, family)] / anchors[(reference, family)]
                        profile[family + "_ratio"] = value
                        rows = [row for row in train if row["core_kind"] in (kind, reference) and row["family"] == family and row["active_tasks"] == 1 and row["frequency_khz"] == model["profiles"][row["core_kind"]]["max_khz"]]
                        estimate("core_ratios", f"profiles.{kind}.{family}_ratio", value, rows, "paired_max_opp_cpu_per_work_unit_median")
                    else: missing.append(f"{kind}/{family} 或参考核组 {reference} 单跑 CPU 样本不足，核型倍率保留原值。")
            if "frequency_response" in plan["parameter_groups"]:
                points = []
                rows = [row for row in train if row["benchmark"] == "cpu_work" and row["core_kind"] == kind and row["active_tasks"] == 1]
                for row in rows:
                    anchor = anchors.get((kind, row["family"]))
                    if anchor: points.append((profile["max_khz"] / row["frequency_khz"] - 1, row["cpu_ms"] / row["work_units"] / anchor - 1))
                if len({row["frequency_khz"] for row in rows}) < 2 or not any(x > 0 for x, _ in points):
                    missing.append(f"{kind} 频率响应缺少至少两个训练 OPP，保留敏感性默认值。")
                else:
                    raw = sum(x * y for x, y in points) / sum(x * x for x, _ in points)
                    if not -.02 <= raw <= 1.02: errors.append(f"{kind} 频率弹性拟合值{raw:.4f}超出当前模型0至1适用域。")
                    value = min(1.0, max(0.0, raw))
                    profile["frequency_elasticity"] = value
                    estimate("frequency_response", f"profiles.{kind}.frequency_elasticity", value, rows, "least_squares_relative_cpu_on_max_over_f_minus_one")
            if "frequency_roofline" in plan["parameter_groups"] and kind in roofline_kinds:
                fitted_classes = {}
                for workload_class in WORKLOAD_CLASSES:
                    rows = [row for row in train if is_roofline_row(row)
                            and row["core_kind"] == kind and row["workload_class"] == workload_class
                            and row["active_tasks"] == 1]
                    by_frequency = defaultdict(list)
                    for row in rows:
                        by_frequency[row["frequency_khz"]].append(row["cpu_ms"] / row["work_units"])
                    if len(by_frequency) < 5 or profile["max_khz"] not in by_frequency:
                        missing.append(f"{kind}/{workload_class} 拐点拟合需至少5个训练OPP并包含最大OPP，当前{len(by_frequency)}个。")
                        continue
                    anchor = statistics.median(by_frequency[profile["max_khz"]])
                    try:
                        fitted = fit_roofline([(frequency, statistics.median(values) / anchor)
                                               for frequency, values in sorted(by_frequency.items())], profile["max_khz"])
                    except ValueError as exc:
                        errors.append(f"{kind}/{workload_class} 拐点拟合失败：{exc}")
                        continue
                    fitted.update(workload_class=workload_class,
                                  reference_frequency_khz=profile["max_khz"],
                                  reference_cpu_ms_per_work_unit=anchor,
                                  formula={
                                      "symbolic": "T(f)=Tmax*(fmax/f)^e_hi for f>=fk; Tmax*(fmax/fk)^e_hi*(fk/f)^e_lo for f<fk",
                                      "parameters": {
                                          "Tmax_cpu_ms_per_work_unit": anchor,
                                          "fmax_khz": profile["max_khz"],
                                          "fk_khz": fitted["knee_khz"],
                                          "e_hi": fitted["above_knee_exponent"],
                                          "e_lo": fitted["below_knee_exponent"],
                                      },
                                      "scope": "target-thread On-CPU cost only",
                                  },
                                  source="measured_class_proxy_frequency_sweep",
                                  workload_intensity=pair_work_source_intensity(
                                      workload_class,
                                      working_set_bytes=min(
                                          row["working_set_bytes"] for row in rows)))
                    roofline_training_mapes.append(fitted["training_mape"])
                    roofline_training_p90s.append(fitted["training_p90_ape"])
                    if (fitted["training_mape"] > THRESHOLDS["maximum_roofline_training_mape"]
                            or fitted["training_p90_ape"] > THRESHOLDS["maximum_roofline_training_p90_ape"]):
                        errors.append(
                            f"{kind}/{workload_class} 拐点训练残差 MAPE {fitted['training_mape']:.1%}/"
                            f"P90 {fitted['training_p90_ape']:.1%} 超出质量门槛。"
                        )
                    fitted_classes[workload_class] = fitted
                    estimate("frequency_roofline",
                             f"profiles.{kind}.frequency_roofline.classes.{workload_class}",
                             fitted, rows, "continuous_piecewise_power_law_opp_grid_fit",
                             fitted["training_mape"])
                if fitted_classes:
                    profile["frequency_roofline"] = {
                        "schema_version": ROOFLINE_SCHEMA,
                        "classes": fitted_classes,
                        "selection": "PMU-derived stage workload_class",
                        "fallback": "legacy profile ratio and frequency_elasticity for unknown/unmeasured class or frequency",
                    }
            if "contention" in plan["parameter_groups"]:
                anchor = anchors.get((kind, "build"))
                rows = [row for row in train if row["benchmark"] == "cpu_work" and row["core_kind"] == kind and row["family"] == "build" and row["active_tasks"] > 1 and row["frequency_khz"] == profile["max_khz"]]
                if not rows or not anchor: missing.append(f"{kind} 缺少匹配单跑/共跑 CPU 测量，争用系数保留原值。")
                else:
                    points = [(row["active_tasks"] - 1, row["cpu_ms"] / row["work_units"] / anchor - 1) for row in rows]
                    raw = sum(x * y for x, y in points) / sum(x * x for x, _ in points)
                    if raw < -.02 or raw > 5: errors.append(f"{kind} 争用系数{raw:.4f}超出非负线性膨胀模型适用域。")
                    value = max(0.0, raw)
                    profile["alpha"] = value
                    estimate("contention", f"profiles.{kind}.alpha", value, rows, "least_squares_cpu_inflation_on_active_tasks_minus_one")
        # The class curves already contain an absolute maximum-OPP unit cost.
        # Once the model reference group has the same class measurement, bind
        # every target curve to that measured source anchor. Consumers can then
        # transfer reference CPU work with target/source curve costs directly,
        # without multiplying the old build_ratio a second time. Plans which
        # measured only one non-reference group retain the v1 normalized-curve
        # behavior and explicitly fall back to the legacy core ratio.
        if "frequency_roofline" in plan["parameter_groups"]:
            reference_profile = model["profiles"].get(reference, {})
            reference_classes = reference_profile.get("frequency_roofline", {}).get("classes", {})
            for kind in roofline_kinds:
                profile = model["profiles"][kind]
                classes = profile.get("frequency_roofline", {}).get("classes", {})
                absolute_transfer_bound = False
                for workload_class, parameters in classes.items():
                    source = reference_classes.get(workload_class)
                    target_cost = parameters.get("reference_cpu_ms_per_work_unit")
                    source_cost = source.get("reference_cpu_ms_per_work_unit") if isinstance(source, dict) else None
                    if not (isinstance(target_cost, (int, float)) and math.isfinite(target_cost) and target_cost > 0
                            and isinstance(source_cost, (int, float)) and math.isfinite(source_cost) and source_cost > 0):
                        continue
                    parameters.update(
                        max_opp_cpu_ms_per_work_unit=target_cost,
                        cross_core_time_ratio=target_cost / source_cost,
                        transfer_reference={
                            "core_kind": reference,
                            "frequency_khz": reference_profile["max_khz"],
                            "cpu_ms_per_work_unit": source_cost,
                            "metric": "target_thread_cpu_ms_per_work_unit",
                        },
                    )
                    absolute_transfer_bound = True
                if classes:
                    profile["frequency_roofline"]["core_transfer"] = (
                        "target_absolute_unit_cost_over_model_reference_absolute_unit_cost"
                        if absolute_transfer_bound else
                        "legacy_profile_ratio_fallback_missing_reference_curve")
        if "dispatch_overhead" in plan["parameter_groups"]:
            rows = [row for row in train if row["benchmark"] == "empty_dispatch"]
            if not rows: missing.append("缺少参考核组的 Worker 派发开销实测。")
            else:
                paired_rows = [row for row in rows if _is_paired_dispatch(row)]
                if paired_rows:
                    wall_delta = statistics.median(
                        row["dispatch_wall_delta_ms_per_task"] for row in paired_rows)
                    worker_delta = statistics.median(
                        row["dispatch_worker_cpu_delta_ms_per_task"] for row in paired_rows)
                    producer_delta = statistics.median(
                        row["dispatch_producer_cpu_delta_ms_per_task"] for row in paired_rows)
                    if wall_delta <= 0:
                        missing.append("v4 配对派发 wall 差分中位数必须为正，无法拟合分支临界路径开销。")
                    else:
                        model["simulation_parameters"]["branch_overhead_ms"] = wall_delta
                        model["simulation_parameters"]["dispatch_overhead"] = {
                            "schema_version": PAIRED_DISPATCH_SCHEMA,
                            "dispatch_wall_delta_ms_per_task": wall_delta,
                            "dispatch_worker_cpu_delta_ms_per_task": worker_delta,
                            "dispatch_producer_cpu_delta_ms_per_task": producer_delta,
                            "branch_metric": "dispatch_wall_delta_ms_per_task",
                            "cpu_deltas_are_attribution_only": True,
                            "cold_start_included": False,
                        }
                        estimate("dispatch_overhead", "simulation_parameters.branch_overhead_ms",
                                 wall_delta, paired_rows,
                                 "median_paired_queue_minus_direct_roundtrip_wall_ms_per_task")
                        estimate("dispatch_overhead",
                                 "simulation_parameters.dispatch_overhead.dispatch_worker_cpu_delta_ms_per_task",
                                 worker_delta, paired_rows,
                                 "median_paired_queue_minus_direct_worker_cpu_ms_per_task")
                        estimate("dispatch_overhead",
                                 "simulation_parameters.dispatch_overhead.dispatch_producer_cpu_delta_ms_per_task",
                                 producer_delta, paired_rows,
                                 "median_paired_queue_minus_direct_producer_cpu_ms_per_task")
                else:
                    value = statistics.median(row["cpu_ms"] / row["work_units"] for row in rows)
                    model["simulation_parameters"]["branch_overhead_ms"] = value
                    estimate("dispatch_overhead", "simulation_parameters.branch_overhead_ms", value, rows,
                             "median_worker_thread_cpu_ms_per_empty_dispatch_legacy_v1_v3")
        if "classic_roofline" in plan["parameter_groups"]:
            rows = [row for row in accepted if row["benchmark"] == "roofline"]
            try:
                fitted_classic_roofline = classic_roofline.fit(rows)
            except ValueError as exc:
                missing.append("经典 Roofline 实测不足或无效：" + str(exc))
            else:
                model["classic_roofline"] = fitted_classic_roofline
                classic_failed_gates = fitted_classic_roofline.get("quality", {}).get("failed_gates", [])
                for gate in classic_failed_gates if isinstance(classic_failed_gates, list) else []:
                    if isinstance(gate, dict) and isinstance(gate.get("message"), str):
                        errors.append("经典 Roofline：" + gate["message"])
                estimate("classic_roofline", "classic_roofline", fitted_classic_roofline,
                         [row for row in rows if row["split"] == "train"],
                         "median_compute_ceiling_and_source_bandwidth_per_real_core_group_opp")
        pair_anchors, pair_cells = {}, {}
        if "pairwise_interference" in plan["parameter_groups"]:
            solo_rows = defaultdict(list)
            for row in train:
                if row["benchmark"] == "pair_work" and row["active_tasks"] == 1 and not is_roofline_row(row):
                    solo_rows[solo_key(row)].append(row)
            pair_anchors = {key: statistics.median(row["cpu_ms"] / row["work_units"] for row in rows) for key, rows in solo_rows.items()}
            paired = defaultdict(list)
            for row in train:
                if row["benchmark"] == "pair_work" and row["active_tasks"] == 2: paired[cell_key(row)].append(row)
            for key, rows in paired.items():
                points = [row["cpu_ms"] / row["work_units"] / pair_anchors[solo_key(row)] - 1 for row in rows if solo_key(row) in pair_anchors]
                if len(points) != len(rows):
                    missing.append("共跑方向缺少同工作集、核放置与频点的单跑锚点：" + str(key)); continue
                placements_seen = {(row["cpu"], row["helpers"][0]["cpu"], row["observer_cpu"], row["working_set_bytes"]) for row in rows}
                if len(placements_seen) != 1:
                    message = "同一干扰单元混用了不同物理放置或工作集：" + str(key)
                    errors.append(message); integrity_errors.append(message); continue
                raw = statistics.median(points)
                if not -.02 <= raw <= 5: errors.append("双任务膨胀超出非负干扰模型适用域：" + str(key))
                beta = max(0.0, raw)
                cell = dict(zip(CELL_FIELDS, key))
                cell.update(id="pair_" + _hash(cell)[:12], beta=beta, sample_count=len(rows),
                            victim_cpu=rows[0]["cpu"], peer_cpu=rows[0]["helpers"][0]["cpu"], observer_cpu=rows[0]["observer_cpu"], working_set_bytes=rows[0]["working_set_bytes"],
                            active_tasks=2, work_units_observed=sorted({row["work_units"] for row in accepted if row["benchmark"] == "pair_work" and row["active_tasks"] == 2 and cell_key(row) == key}))
                pair_cells[key] = cell
                estimate("pairwise_interference", "interference_model.cells." + cell["id"] + ".beta", beta, rows, "median_directional_corun_cpu_over_matched_solo_minus_one")
            model["interference_model"] = {"schema_version": INTERFERENCE_SCHEMA, "version": "1.0.0", "status": "provisional",
                "cells": list(pair_cells.values()), "active_tasks_domain": [2], "hardware_profile_id": plan["hardware_profile_id"],
                "measurement_placement": copy.deepcopy(provenance.get("cpu_map", {})),
                "fallback": "legacy_alpha_for_unknown_class_unmeasured_pair_or_concurrency_above_two",
                "evidence_boundary": "按负载类别、核组与频点进行代理迁移；应用阶段工作集、共享缓存拓扑和具体物理放置等价性未验证。", "source_provenance": copy.deepcopy(provenance)}
        predictions, absolute_errors = [], []
        classic_roofs = {
            (row["core_kind"], row["frequency_khz"]): row
            for row in model.get("classic_roofline", {}).get("roofs", [])
        }
        for row in validation:
            if row["benchmark"] == "roofline":
                roof = classic_roofs.get((row["core_kind"], row["frequency_khz"]))
                if roof is None or row["workload_class"] != "intensity_ladder":
                    continue
                observed = row["attainable_gflops"]
                predicted = classic_roofline.attainable_performance(
                    row["operational_intensity_flop_per_byte"],
                    roof["maximum_attainable_performance_gflops"],
                    roof["sustainable_source_bandwidth_gbytes_s"],
                )
                # Roofline is a ceiling rather than a point predictor.  A
                # portable intensity kernel may sit below the roof; only a
                # statistically meaningful excursion above the measured roof
                # is an internal inconsistency.
                ape = max(0.0, (observed - predicted) / predicted)
                predictions.append({
                    "sample_id": row["sample_id"], "split": "validation",
                    "validation_metric": "roof_ceiling_violation_ratio",
                    "observed_attainable_gflops": observed,
                    "predicted_roof_gflops": predicted,
                    "absolute_percentage_error": ape,
                })
                absolute_errors.append(ape)
                continue
            if is_roofline_row(row):
                profile = model["profiles"][row["core_kind"]]
                parameters = profile.get("frequency_roofline", {}).get("classes", {}).get(row["workload_class"])
                if not parameters: continue
                predicted = (parameters["reference_cpu_ms_per_work_unit"] * row["work_units"]
                             * roofline_factor(profile["max_khz"], row["frequency_khz"],
                                               parameters["knee_khz"], parameters["above_knee_exponent"],
                                               parameters["below_knee_exponent"]))
            elif row["benchmark"] == "pair_work":
                anchor = pair_anchors.get(solo_key(row))
                cell = pair_cells.get(cell_key(row)) if row["active_tasks"] == 2 else None
                if anchor is None or row["active_tasks"] == 2 and cell is None: continue
                if cell and (row["cpu"] != cell["victim_cpu"] or row["helpers"][0]["cpu"] != cell["peer_cpu"]):
                    message = "留出样本核放置与训练不同，不能视为同域验证。"
                    errors.append(message); integrity_errors.append(message); continue
                predicted = anchor * row["work_units"] * (1 + cell["beta"] if cell else 1)
            elif row["benchmark"] == "empty_dispatch":
                predicted = model["simulation_parameters"]["branch_overhead_ms"] * row["work_units"]
                if _is_paired_dispatch(row):
                    observed = row["dispatch_wall_delta_ms_per_task"] * row["work_units"]
                    if observed <= 0:
                        errors.append("v4 配对派发留出集的 wall 差分总量必须为正。")
                        continue
                    ape = abs(predicted - observed) / observed
                    predictions.append({
                        "sample_id": row["sample_id"], "split": "validation",
                        "validation_metric": "dispatch_wall_delta_absolute_percentage_error",
                        "dispatch_task_count": row["work_units"],
                        "observed_dispatch_wall_delta_ms": observed,
                        "predicted_dispatch_wall_delta_ms": predicted,
                        "absolute_percentage_error": ape,
                    })
                    absolute_errors.append(ape)
                    continue
            else:
                profile = model["profiles"][row["core_kind"]]
                anchor = anchors.get((reference if "core_ratios" in plan["parameter_groups"] else row["core_kind"], row["family"]))
                if anchor is None: continue
                ratio = profile[row["family"] + "_ratio"] if "core_ratios" in plan["parameter_groups"] else 1
                frequency = (1 - profile["frequency_elasticity"]) + profile["frequency_elasticity"] * profile["max_khz"] / row["frequency_khz"]
                inflation = 1 + profile["alpha"] * (row["active_tasks"] - 1) if row["family"] == "build" else 1
                predicted = anchor * row["work_units"] * ratio * frequency * inflation
            ape = abs(predicted - row["cpu_ms"]) / row["cpu_ms"]
            predictions.append({"sample_id": row["sample_id"], "split": "validation", "validation_metric": "cpu_time_absolute_percentage_error", "observed_cpu_ms": row["cpu_ms"], "predicted_cpu_ms": predicted, "absolute_percentage_error": ape})
            absolute_errors.append(ape)
        median, p90 = _quantile(absolute_errors, .5), _quantile(absolute_errors, .9)
        heldout_errors = defaultdict(list)
        predicted_by_id = {row["sample_id"]: row for row in predictions}
        for row in validation:
            if row["sample_id"] in predicted_by_id:
                heldout_errors[_condition(row)].append(predicted_by_id[row["sample_id"]]["absolute_percentage_error"])
        validation_by_condition = []
        for condition, values in heldout_errors.items():
            condition_median, condition_p90 = _quantile(values, .5), _quantile(values, .9)
            passed_condition = condition_median <= THRESHOLDS["maximum_validation_median_ape"] and condition_p90 <= THRESHOLDS["maximum_validation_p90_ape"]
            validation_by_condition.append({"condition_id": expected[condition]["id"], "sample_count": len(values),
                                            "median_ape": condition_median, "p90_ape": condition_p90, "passed": passed_condition})
            if not passed_condition:
                errors.append(f"留出条件 {expected[condition]['id']} 校验误差中位数{condition_median:.1%}/P90 {condition_p90:.1%}超出门槛。")
        cv = max(repeat_cvs, default=None)
        if not validation or len(predictions) != len(validation): missing.append("独立留出样本不足或缺少预测锚点。")
        if cv is not None and cv > THRESHOLDS["maximum_repeat_cv"]: errors.append(f"重复运行变异系数{cv:.1%}超过15%。")
        if median is not None and median > THRESHOLDS["maximum_validation_median_ape"]: errors.append(f"留出样本校验误差中位数{median:.1%}超过10%。")
        if p90 is not None and p90 > THRESHOLDS["maximum_validation_p90_ape"]: errors.append(f"留出样本校验误差P90 {p90:.1%}超过20%。")
        all_paths = [f"profiles.{kind}.{field}" for kind in model["profiles"] for field in ("build_ratio", "residual_ratio", "frequency_elasticity", "alpha")] + ["simulation_parameters.branch_overhead_ms"]
        if "classic_roofline" in plan["parameter_groups"]:
            all_paths.append("classic_roofline")
        if "frequency_roofline" in plan["parameter_groups"]:
            all_paths.extend(f"profiles.{kind}.frequency_roofline.classes.{workload_class}"
                             for kind in roofline_kinds for workload_class in WORKLOAD_CLASSES)
        inherited = [path for path in all_paths if path not in calibrated]
        passed = bool(accepted and estimates and predictions and not missing and not errors)
        publishable = bool(accepted and estimates and not integrity_errors)
        failed_gates = ([{"category": "quality", "message": message} for message in errors]
                        + [{"category": "coverage", "message": message} for message in missing])
        if passed:
            risk_level, evidence_grade = "low", "A"
            risk_summary = "实测覆盖、重复稳定性和独立留出质量门均通过。"
        elif publishable:
            risk_level = "high" if missing else "medium"
            evidence_grade = "C" if missing else "B"
            risk_summary = (f"模型基于 {len(accepted)} 条可用实测生成并允许发布，但有 "
                            f"{len(failed_gates)} 项质量或覆盖门未通过；寻优结果应标注风险并优先补测。")
        else:
            risk_level, evidence_grade = "high", "insufficient"
            risk_summary = ("没有同时满足来源完整性、可用实测和至少一个估计参数，"
                            "不能生成可用校准版本。")
        if "interference_model" in model: model["interference_model"]["status"] = "proxy_holdout_validated" if passed else "provisional"
        status = ("passed" if passed else "publishable_with_risk" if publishable
                  else "failed_integrity" if integrity_errors else "needs_data" if missing else "failed_quality")
        for estimate_row in estimates: estimate_row["status"] = "validated_proxy_parameter" if passed else "provisional_estimate"
        domain = {"core_kinds": plan["core_kinds"], "frequencies_khz": plan["frequencies_khz"],
                  "frequency_roofline_core_kinds": roofline_kinds,
                  "active_tasks_observed": sorted({row["active_tasks"] for row in accepted if row["benchmark"] in ("cpu_work", "pair_work")}),
                  "work_units_observed": sorted({row["work_units"] for row in accepted}),
                  "workload_ids": sorted({row["workload_id"] for row in accepted}),
                  "frequency_roofline": {kind: copy.deepcopy(model["profiles"][kind].get("frequency_roofline"))
                                         for kind in roofline_kinds if model["profiles"][kind].get("frequency_roofline")},
                  "concurrency_transfer": "双任务矩阵只匹配已测有向类别/核组/频点；缺少匹配或并发数超过2回退alpha。所有代理向应用负载的迁移均属条件预测。",
                  "pairwise_domain": model.get("interference_model", {}).get("cells", [])}
        validation_info = {"status": "proxy_holdout_validated" if passed else status,
                           "publishable": publishable, "risk_level": risk_level,
                           "risk_summary": risk_summary, "evidence_grade": evidence_grade,
                           "failed_gates": failed_gates,
                           "application_validation": "not_application_validated", "median_ape": median, "p90_ape": p90,
                           "roofline_training_mape_max": max(roofline_training_mapes, default=None),
                           "roofline_training_p90_ape_max": max(roofline_training_p90s, default=None),
                           "coverage": {"calibrated_parameters": calibrated, "inherited_parameters": inherited}, "source_provenance": copy.deepcopy(provenance), "held_out_conditions": True, "reference_anchor": plan.get("reference_anchor", context["reference_anchor"]), "calibration_domain": domain}
        # Legacy source sample arrays describe the parent model, not this fit.
        for key in ("single_task_samples", "audit_sources", "big_contention_same_quarter_cpu_ms"):
            model.pop(key, None)
        for kind, profile in model["profiles"].items():
            profile.pop("serial_cpu_ms", None); profile.pop("serial_residual_ms", None)
            profile["provenance"] = {"calibrated_paths": [path for path in calibrated if path.startswith(f"profiles.{kind}.")],
                                     "inherited_paths": [path for path in inherited if path.startswith(f"profiles.{kind}.")],
                                     "source": "imported_proxy_measurements_with_holdout"}
        model["frequency_elasticity"] = {"status": "per_core_fitted_or_explicitly_inherited", "values": {kind: model["profiles"][kind]["frequency_elasticity"] for kind in model["profiles"]}}
        model.update(validation=validation_info, reference_anchor=plan.get("reference_anchor", context["reference_anchor"]), calibrated_parameters=calibrated, inherited_parameters=inherited,
                     inherited_base_model={"id": base_model["id"], "sha256": _hash(base_model)})
        quality = {"passed": passed, "publishable": publishable,
                   "risk_level": risk_level, "risk_summary": risk_summary,
                   "evidence_grade": evidence_grade, "failed_gates": failed_gates,
                   "publication_blockers": integrity_errors,
                   "errors": errors + missing, "warnings": warnings, "accepted_samples": len(accepted), "rejected_samples": len(rejected), "train_samples": len(train), "validation_samples": len(validation), "validation_median_ape": median, "validation_p90_ape": p90,
                   "roofline_training_mape_max": max(roofline_training_mapes, default=None),
                   "roofline_training_p90_ape_max": max(roofline_training_p90s, default=None),
                   "roofline_placement_domains": roofline_placement_domains,
                   "classic_roofline_placement_domains": classic_placement_domains,
                   "validation_by_condition": validation_by_condition, "repeat_cv_max": cv, "thresholds": THRESHOLDS, "parameter_coverage": {"calibrated": calibrated, "inherited": inherited}, "rejected_sample_details": rejected, "independent_device_verification": False, "application_validation": "not_application_validated"}
        record = {"schema_version": "parallel_calibration_fit/v1", "id": "cf_" + _hash({"plan": plan["id"], "measurements": digest, "engine": CALIBRATION_ENGINE_VERSION})[:24], "created_at": utc_now(), "plan_id": plan["id"], "name": plan["name"], "status": status, "hardware_profile_id": plan["hardware_profile_id"], "parameter_estimates": estimates, "quality": quality, "model_preview": model, "measurements_sha256": digest, "measurements": copy.deepcopy(measurements), "validation_predictions": predictions, "calibrated_parameters": calibrated, "inherited_parameters": inherited, "provenance": copy.deepcopy(provenance)}
        return self._record("fits", record)

    def publish(self, payload):
        if not isinstance(payload, dict): _error("版本请求应为 JSON 对象。")
        fit = self.get_fit(payload.get("fit_id"))
        if self.is_hardware_profile_superseded(fit.get("hardware_profile_id")):
            _error("该拟合所属硬件画像已被后续离线校准包退役，不能再次发布模型。",
                   code="parallel_calibration_hardware_superseded",
                   hardware_profile_id=fit.get("hardware_profile_id"))
        if not _fit_is_publishable(fit):
            _error("当前拟合缺少可用实测、估计参数或未通过来源完整性检查，不能发布。",
                   code="parallel_calibration_not_publishable", quality=fit.get("quality", {}))
        name = str(payload.get("name") or fit["name"] + " · 校准版本")[:200]
        identity = "cm_" + _hash({"fit_id": fit["id"], "name": name})[:24]
        if self.is_calibration_version_superseded(identity):
            _error("该校准版本身份已被后续离线包持久退役，不能重新发布。",
                   code="parallel_calibration_model_superseded",
                   calibration_version_id=identity)
        path = self.root / "versions" / (identity + ".json")
        if path.is_file(): return self.get_version(identity)
        quality_passed = fit["quality"].get("passed") is True
        status = ("partially_calibrated" if fit["inherited_parameters"] else "proxy_calibrated") \
            if quality_passed else "risk_calibrated"
        model = copy.deepcopy(fit["model_preview"])
        plan = self.get_plan(fit["plan_id"])
        model["hardware_profile_sha256"] = plan.get("hardware_profile_sha256")
        risk = {key: copy.deepcopy(fit["quality"].get(key)) for key in
                ("risk_level", "risk_summary", "evidence_grade", "failed_gates", "publication_blockers")}
        model.update(id=identity, version=CALIBRATION_ENGINE_VERSION, status=status, name=name,
                     hardware_profile_id=fit["hardware_profile_id"], calibration_risk=risk)
        description = ("已通过独立微基准留出检查的代理模型；应用迁移效果另行验证。"
                       if quality_passed else
                       "基于可用实测生成的风险校准模型；质量门失败项、证据等级与适用风险随版本保存。")
        record = {"schema_version": "parallel_calibration_version/v1", "id": identity, "created_at": utc_now(), "name": name, "version": CALIBRATION_ENGINE_VERSION, "status": status, "fit_id": fit["id"], "plan_id": fit["plan_id"], "hardware_profile_id": fit["hardware_profile_id"], "hardware_profile_sha256": plan.get("hardware_profile_sha256"), "calibrated_parameters": fit["calibrated_parameters"], "inherited_parameters": fit["inherited_parameters"], "validation": model["validation"], "risk_level": risk["risk_level"], "risk_summary": risk["risk_summary"], "evidence_grade": risk["evidence_grade"], "failed_gates": risk["failed_gates"], "model": model, "model_sha256": _hash(model), "measurements_sha256": fit["measurements_sha256"], "description": description}
        return self._record("versions", record)

    def resolve_model(self, identity):
        if identity == self.base_model["id"]: return copy.deepcopy(self.base_model)
        version = self.get_version(identity)
        if (version.get("status") == "superseded"
                or self.is_calibration_version_superseded(identity)):
            supersession = version.get("supersession") if isinstance(version.get("supersession"), dict) else {}
            _error("该校准模型已被后续离线校准包退役，不能用于新的分析或寻优。",
                   code="parallel_calibration_model_superseded",
                   calibration_version_id=identity,
                   superseded_by_calibration_version_id=supersession.get("by_calibration_version_id"))
        if version["status"] not in ("proxy_calibrated", "partially_calibrated", "risk_calibrated") or _hash(version["model"]) != version["model_sha256"]:
            _error("已保存模型版本内容校验失败。", code="parallel_calibration_model_corrupt")
        model = copy.deepcopy(version["model"])
        model["calibration_provenance"] = {"version_id": identity, "fit_id": version["fit_id"], "plan_id": version["plan_id"], "measurements_sha256": version["measurements_sha256"], "model_sha256": version["model_sha256"], "artifacts": version.get("artifacts", [])}
        return model


def create_calibration_router(manager: CalibrationManager):
    # Calibration itself is also used by the stdlib-only Desktop SSH worker;
    # FastAPI is required only when the Web router is instantiated.
    from fastapi import APIRouter, Body

    router = APIRouter(prefix="/v1/parallel-calibration", tags=["parallel-calibration"])

    @router.get("/catalog")
    def catalog(): return manager.catalog()
    @router.get("/plans")
    def plans(): return {"plans": manager.list_plans()}
    @router.post("/plans")
    def create_plan(payload: dict = Body(...)): return manager.create_plan(payload)
    @router.get("/plans/{identity}")
    def get_plan(identity: str): return manager.get_plan(identity)
    @router.get("/plans/{identity}/bundle")
    def bundle(identity: str): return manager.bundle(identity)
    @router.get("/fits")
    def fits(): return {"fits": manager.list_fits()}
    @router.post("/fits")
    def fit(payload: dict = Body(...)): return manager.fit(payload)
    @router.get("/fits/{identity}")
    def get_fit(identity: str): return manager.get_fit(identity)
    @router.get("/versions")
    def versions(): return {"versions": manager.list_versions()}
    @router.post("/versions")
    def publish(payload: dict = Body(...)): return manager.publish(payload)
    @router.get("/versions/{identity}")
    def get_version(identity: str): return manager.get_version(identity)
    return router
