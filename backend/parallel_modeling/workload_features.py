"""Versioned, evidence-preserving workload profiles at measured BuildItem boundaries.

PMU sample deltas may be apportioned by wall-time overlap as labeled estimates.
Exact stage measurements and conservative bounds retain their distinct semantics.
"""
from __future__ import annotations

import copy
import csv
import hashlib
import json
import math
import re
from collections import Counter
from typing import Literal

from backend.core import PlatformError, utc_now
from .features import component_features
from .evidence import Pmu, timestamp_ns
from .trace_parser import LINE_RE
from .feature_export import (SOURCE_ANCHOR_ENGINE_VERSION, SOURCE_ANCHOR_PROFILE_SCHEMA,
                             export_profile_csv)

VERSION = SOURCE_ANCHOR_ENGINE_VERSION
PROFILE_SCHEMA = SOURCE_ANCHOR_PROFILE_SCHEMA
CPU_RESIDENCY_EVIDENCE_SCHEMA = "cpu_residency_evidence/v1"
CPU_RESIDENCY_COVERAGE = {
    "complete_min_ratio": .95,
    "complete_max_ratio": 1.05,
    "partial_min_ratio": .70,
}
CLASSES = {"compute": "计算主导候选", "memory": "访存压力候选", "mixed": "混合特征",
           "service": "系统服务等待", "scheduler": "调度等待", "unknown": "证据不足"}
RULES = {"version": "evidence-tiered-stage-rules/v2", "minimum_cpu_share": .7,
         "compute_min_ipc": 1.0, "compute_max_llc_mpki": 5.0,
         "memory_max_ipc": 1.0, "memory_min_llc_mpki": 20.0,
         "fallback_compute_min_ipc": 1.2, "fallback_memory_max_ipc": .8,
         "fallback_low_cache_mpki": 5.0, "fallback_high_cache_mpki": 20.0,
         "dominant_wait_share": .3,
         "meaning": "完整证据优先使用阶段计数边界与已核验 LLC；证据不完整时以现有 IPC、Cache/LLC MPKI 或基础计数生成带置信度的候选类型。不是芯片通用瓶颈阈值，也不是并行收益模型。"}
COUNTERS = {"instructions": "count", "cycles": "count", "cache_references": "count",
            "cache_misses": "count", "llc_misses": "count", "loads": "count", "stores": "count",
            "branch_instructions": "count", "branch_misses": "count", "memory_stall_cycles": "count",
            "dram_bytes": "bytes", "io_read_bytes": "bytes", "io_write_bytes": "bytes",
            "l1d_accesses": "count", "l1d_misses": "count", "llc_accesses": "count",
            "dtlb_accesses": "count", "dtlb_misses": "count", "stalled_frontend": "count", "stalled_backend": "count"}


def _error(message, **details):
    raise PlatformError("workload_features_invalid_input", message, details=details)


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(",", ":"), allow_nan=False).encode()).hexdigest()


def _file_hash(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _rebuild_sample_estimates(directory, frame, rows, trace_sha256=None):
    """Upgrade derived evidence from retained samples without changing old inputs.

    A range alone is insufficient to reconstruct a weighted count. Re-reading raw
    snapshots also verifies target identity and the original clock sync marker.
    """
    if not rows or all(row["pmu"].get("evidence_version") == "grouped_stage_pmu/v3" for row in rows):
        return {"status": "stored_evidence"}
    names = ("pmu.csv", "pmu.json", "trace.ftrace")
    if not all((directory / name).is_file() for name in names):
        return {"status": "raw_samples_unavailable", "reason": "保留原有区间；缺少原始采样窗口时不以区间中点生成估值。"}
    record = {"raw_sha256": {name: _file_hash(directory / name) for name in names}}
    try:
        if trace_sha256 and record["raw_sha256"]["trace.ftrace"] != trace_sha256:
            raise ValueError("Retained Trace differs from the originally imported Trace")
        metadata = json.loads((directory / "pmu.json").read_text())
        if type(metadata.get("target_tid")) is not int or metadata["target_tid"] != frame.get("tid"):
            raise ValueError("PMU target TID differs from frame TID")
        begin, end = metadata.get("sync_begin_ns"), metadata.get("sync_end_ns")
        if type(begin) is not int or type(end) is not int or end < begin:
            raise ValueError("PMU clock sync bracket is missing or invalid")
        events = []
        with (directory / "trace.ftrace").open() as source:
            for line in source:
                if "PMU_CLOCK_SYNC|" not in line: continue
                match = LINE_RE.match(line)
                if match and match["event"] == "tracing_mark_write":
                    events.append({"event": match["event"], "ns": timestamp_ns(match["ts"]), "body": match["body"]})
        pmu = Pmu(directory, events, window=(frame["start_ns"], frame["end_ns"]))
        if not pmu.clock_verified:
            raise ValueError("PMU clock sync marker cannot be verified")
        rebuilt = [pmu.stage(row["start_ns"], row["end_ns"]) for row in rows]
        for row, evidence in zip(rows, rebuilt):
            row["pmu"] = evidence
        return {**record, "status": "rebuilt_from_raw_samples", "evidence_version": "grouped_stage_pmu/v3",
                "quality": pmu.quality}
    except (OSError, ValueError, KeyError, TypeError, ZeroDivisionError, csv.Error) as exc:
        return {**record, "status": "raw_samples_unusable", "reason": str(exc)}


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) and value >= 0


def _cpu_residency_evidence(residency_rows, expected_on_cpu_ms, fallback_on_cpu_ms=None,
                            *, hardware_identity_verified=False):
    """Grade sched-switch residency without promoting a visible fragment.

    ``stage.cpu_ms`` is the preferred expected on-CPU duration because it is
    the accounting value later transferred by the simulator.  The state
    partition is only a compatibility fallback.  Coverage deliberately keeps
    the raw observed/expected ratio: a value materially above one is conflicting
    evidence, not 100% coverage.
    """
    expected = expected_on_cpu_ms if _number(expected_on_cpu_ms) else None
    expected_source = "stage.cpu_ms" if expected is not None else None
    if expected is None and _number(fallback_on_cpu_ms):
        expected, expected_source = fallback_on_cpu_ms, "stage.states_ms.on_cpu"
    observed = sum(
        item.get("cpu_ms", 0) for item in residency_rows
        if isinstance(item, dict) and _number(item.get("cpu_ms"))
    )
    ratio = observed / expected if expected and expected > 0 else None
    reasons = []
    if expected is None or expected <= 0:
        status = "unavailable"
        reasons.append("expected_on_cpu_duration_unavailable")
    elif observed <= 0:
        status = "unavailable"
        ratio = 0.0
        reasons.append("sched_switch_cpu_residency_unobserved")
    elif ratio > CPU_RESIDENCY_COVERAGE["complete_max_ratio"]:
        status = "insufficient"
        reasons.append("observed_residency_exceeds_expected_on_cpu_duration")
    elif ratio >= CPU_RESIDENCY_COVERAGE["complete_min_ratio"]:
        status = "complete"
    elif ratio >= CPU_RESIDENCY_COVERAGE["partial_min_ratio"]:
        status = "partial"
        reasons.append("sched_switch_cpu_residency_partially_observed")
    else:
        status = "insufficient"
        reasons.append("sched_switch_cpu_residency_coverage_insufficient")
    temporal_complete = status == "complete"
    mapped_core_groups = bool(residency_rows) and all(
        isinstance(item, dict) and isinstance(item.get("core_group_id"), str)
        and bool(item["core_group_id"]) for item in residency_rows)
    valid_frequencies = bool(residency_rows) and all(
        isinstance(item, dict) and not isinstance(item.get("frequency_khz"), bool)
        and isinstance(item.get("frequency_khz"), int) and item["frequency_khz"] > 0
        for item in residency_rows)
    if not hardware_identity_verified:
        reasons.append("hardware_identity_unverified")
    if residency_rows and not mapped_core_groups:
        reasons.append("source_core_group_unresolved")
    if residency_rows and not valid_frequencies:
        reasons.append("source_frequency_unresolved")
    source_preconditions = bool(hardware_identity_verified and mapped_core_groups and valid_frequencies)
    return {
        "schema_version": CPU_RESIDENCY_EVIDENCE_SCHEMA,
        "source": "trace_sched_switch_cpu_residency",
        "observed_on_cpu_ms": observed,
        "expected_on_cpu_ms": expected,
        "expected_source": expected_source,
        "coverage": ratio,
        "coverage_ratio": ratio,
        "status": status,
        "temporal_coverage_complete": temporal_complete,
        "source_cost_anchor_preconditions_met": source_preconditions,
        "source_anchor_eligible": temporal_complete and source_preconditions,
        "thresholds": copy.deepcopy(CPU_RESIDENCY_COVERAGE),
        "reason_codes": reasons,
    }


def _bounds(lower, upper):
    return [lower, upper] if _number(lower) and _number(upper) and lower <= upper else None


def _ratio(numerator, denominator, scale=1):
    if not numerator or not denominator or denominator[0] <= 0:
        return None
    return [scale * numerator[0] / denominator[1], scale * numerator[1] / denominator[0]]


def _exact(bounds):
    return bounds[0] if bounds and bounds[0] == bounds[1] else None


EVENT_ALIASES = {"generic_cache_references": "cache_references", "generic_cache_misses": "cache_misses",
    "llc_read_access": "llc_accesses", "llc_read_misses": "llc_misses", "l1d_read_access": "l1d_accesses",
    "l1d_read_misses": "l1d_misses", "dtlb_read_access": "dtlb_accesses", "dtlb_read_misses": "dtlb_misses"}
RATIOS = {"ipc": ("instructions", "cycles", 1), "cache_mpki": ("cache_misses", "instructions", 1000),
    "llc_mpki": ("llc_misses", "instructions", 1000), "branch_mpki": ("branch_misses", "instructions", 1000),
    "l1d_mpki": ("l1d_misses", "instructions", 1000), "dtlb_mpki": ("dtlb_misses", "instructions", 1000)}


def _metric_evidence(pmu, measurement):
    """Use same-group overlap estimates without promoting them to measured counts."""
    result = {}
    groups = pmu.get("groups") or {pmu.get("group_id", "legacy"): pmu}
    for identity, group in groups.items():
        lower = {EVENT_ALIASES.get(key, key): value for key, value in (group.get("lower") or {}).items()}
        upper = {EVENT_ALIASES.get(key, key): value for key, value in (group.get("upper") or {}).items()}
        observed_lower = {EVENT_ALIASES.get(key, key): value for key, value in (group.get("observed_counter_lower") or {}).items()}
        observed_upper = {EVENT_ALIASES.get(key, key): value for key, value in (group.get("observed_counter_upper") or {}).items()}
        interior_estimates = {EVENT_ALIASES.get(key, key): value for key, value in (group.get("interior_estimates") or {}).items()}
        weighted = {EVENT_ALIASES.get(key, key): value for key, value in (group.get("time_weighted_estimates") or {}).items()}
        estimates = weighted or interior_estimates
        names = set(lower) | set(upper) | set(estimates) | {EVENT_ALIASES.get(key, key) for key in group.get("event_names", [])}
        reasons = list(group.get("reason_codes", []))
        interior = group.get("interior_bins", 0)
        if not interior and "short_stage_no_interior_samples" not in reasons:
            reasons.append("short_stage_no_interior_samples")
        if group.get("bounds_valid") and not group.get("feature_usable_under_25pct_instruction_boundary_rule"):
            reasons.append("boundary_dominated")
        estimate_scope = "covered_stage_window" if weighted else "interior_only" if estimates else None
        template = {"group_id": identity,
            "time_coverage": group.get("weighted_time_coverage", 0) if weighted else group.get("estimate_time_fraction", group.get("interior_time_fraction", 0)),
            "runtime_coverage": group.get("weighted_runtime_coverage") if weighted else group.get("runtime_coverage"),
            "interior_time_coverage": group.get("interior_time_fraction", 0), "interior_bins": interior,
            "boundary_bins": group.get("boundary_bins", max(0, group.get("intersecting_bins", 0) - interior)),
            "max_boundary_span_us": group.get("max_intersecting_snapshot_span_us"),
            "reason_codes": list(dict.fromkeys(reasons)), "scope": group.get("scope", pmu.get("scope")),
            "estimate_scope": estimate_scope,
            "estimate_method": group.get("weighted_estimate_method") if weighted else "interior_sum" if estimates else None,
            "sample_window_convention": group.get("sample_window_convention") if weighted else None,
            "weighted_bins": group.get("weighted_bins") if weighted else None}
        candidates = {}
        for name in names & COUNTERS.keys():
            bounds = _bounds(lower.get(name), upper.get(name)) if group.get("bounds_valid") else None
            observed = _bounds(observed_lower.get(name), observed_upper.get(name))
            estimate = estimates.get(name)
            candidates[name] = {**template, "status": "exact" if _exact(bounds) is not None else "estimated" if estimate is not None else "bounded" if bounds else "unavailable",
                "exact": _exact(bounds), "bounds": bounds, "estimate": estimate,
                "interior_estimate": interior_estimates.get(name),
                "observed_counter_bounds": observed, "unit": COUNTERS[name]}
        for name, (num, den, scale) in RATIOS.items():
            if num not in names or den not in names: continue
            bounds = _ratio(candidates[num]["bounds"], candidates[den]["bounds"], scale)
            estimate = scale * estimates[num] / estimates[den] if estimates.get(den) and num in estimates else None
            # Legacy archives carry an interior ratio but not scaled counts.
            if name == "ipc" and not weighted and estimate is None and _number(group.get("interior_ipc")) and interior:
                estimate = group["interior_ipc"]
            interior_ratio = scale * interior_estimates[num] / interior_estimates[den] if interior_estimates.get(den) and num in interior_estimates else None
            if name == "ipc" and interior_ratio is None and _number(group.get("interior_ipc")) and interior:
                interior_ratio = group["interior_ipc"]
            candidates[name] = {**template, "status": "exact" if _exact(bounds) is not None else "estimated" if estimate is not None else "bounded" if bounds else "unavailable",
                "exact": _exact(bounds), "bounds": bounds, "estimate": estimate,
                "interior_estimate": interior_ratio,
                "estimate_scope": (estimate_scope or "interior_only") if estimate is not None else None,
                "observed_counter_bounds": None, "unit": "ratio" if name == "ipc" else "per_kinst"}
        for name, value in candidates.items():
            # Repeated denominators belong to their own groups; prefer base for standalone counts.
            if (name not in result or (result[name]["status"] == "unavailable" and value["status"] != "unavailable")
                    or (identity == "base" and value["status"] != "unavailable")):
                result[name] = value
    if measurement:
        result = {}
        for key, counter in measurement["counters"].items():
            value = counter["value"]
            result[key] = {"status": "exact", "exact": value, "bounds": [value, value], "estimate": None,
                "estimate_scope": None, "observed_counter_bounds": None, "group_id": "stage_measurement",
                "time_coverage": 1, "runtime_coverage": 1, "interior_bins": 0, "boundary_bins": 0,
                "max_boundary_span_us": 0, "reason_codes": [], "unit": COUNTERS[key], "scope": measurement["measurement_scope"]}
        for key, (num, den, scale) in RATIOS.items():
            if num in result and den in result and result[den]["exact"]:
                value = scale * result[num]["exact"] / result[den]["exact"]
                result[key] = {**result[num], "exact": value, "bounds": [value, value], "unit": "ratio" if key == "ipc" else "per_kinst"}
    mapping = measurement.get("event_mapping", {}).get("llc_misses", {}) if measurement else {}
    verified = mapping.get("verified") is True and mapping.get("cache_level") == "LLC"
    for key in ("llc_mpki", "llc_misses", "llc_accesses"):
        if key in result:
            result[key]["hardware_mapping_verified"] = verified
            if not verified: result[key]["reason_codes"] = [*result[key]["reason_codes"], "hardware_llc_mapping_unverified"]
    for key in (*COUNTERS, *RATIOS):
        result.setdefault(key, {"status": "unavailable", "exact": None, "bounds": None, "estimate": None,
            "estimate_scope": None, "observed_counter_bounds": None, "group_id": None,
            "time_coverage": 0, "runtime_coverage": None, "interior_bins": 0, "boundary_bins": 0,
            "max_boundary_span_us": None, "reason_codes": ["event_not_collected_or_unavailable"], "unit": COUNTERS.get(key, "ratio" if key == "ipc" else "per_kinst"), "scope": None})
    return result


def _counter_features(pmu, measurement):
    metrics = _metric_evidence(pmu, measurement)
    bounds = {key: metrics[key]["bounds"] for key in COUNTERS}
    usable = pmu.get("feature_usable_under_25pct_instruction_boundary_rule") is True
    weighted = any(value.get("estimate_method") == "wall_time_overlap" for value in metrics.values())
    source = "trace_aligned_time_weighted_estimates" if weighted else "trace_aligned_interval_bounds" if pmu.get("bounds_valid") else "trace_aligned_interior_estimates" if any(value["estimate"] is not None for value in metrics.values()) else "trace_pmu_unresolved" if pmu.get("groups") or pmu.get("intersecting_bins") else "not_collected"
    scope, reasons = pmu.get("scope"), []
    llc_verified = metrics["llc_misses"].get("hardware_mapping_verified", False)
    if measurement:
        usable = bool(bounds["instructions"] and bounds["instructions"][0] > 0 and bounds["cycles"] and bounds["cycles"][0] > 0)
        source, scope = "stage_exact_measurement", measurement["measurement_scope"]
    if not measurement and any("short_stage_no_interior_samples" in value["reason_codes"] and value["bounds"] for value in metrics.values()):
        reasons.append("阶段短于采样包络且没有完整内部采样区间；0–上界表示边界不确定，不能作为阶段总计数。")
    if not measurement and any("counter_multiplexed_full_stage_upper_unknown" in value["reason_codes"] for value in metrics.values()):
        reasons.append("PMU 事件组存在复用；先按同组 enabled/running 校正，再分配已覆盖窗口内的计数，不能视为精确实测值或保守上界。")
    if not measurement and weighted:
        reasons.append("单值按采样窗口与阶段的墙钟时间重叠比例估算，假设计数在窗口内均匀分布；未覆盖时间不外推，100% 时间覆盖不等于精确归因。")
    if not usable:
        reasons.append("阶段边界或 PMU 运行覆盖不足，不能形成完整证据分型；现有指标仅用于降级候选。")
    elif not llc_verified:
        reasons.append("缺少已核验的 LLC 事件语义；通用 cache miss 不直接推断 DRAM 带宽压力。")
    return {"values": {key: metrics[key]["exact"] for key in COUNTERS}, "bounds": bounds,
            "ipc": metrics["ipc"]["exact"], "ipc_bounds": metrics["ipc"]["bounds"],
            "llc_mpki": metrics["llc_mpki"]["exact"], "llc_mpki_bounds": metrics["llc_mpki"]["bounds"],
            "cache_mpki": metrics["cache_mpki"]["exact"], "cache_mpki_bounds": metrics["cache_mpki"]["bounds"],
            "interior_ipc": metrics["ipc"].get("interior_estimate"), "metric_evidence": metrics,
            "usable": usable, "llc_mapping_verified": llc_verified, "source": source, "scope": scope, "reasons": reasons}


def _union_duration(intervals):
    cursor, total = None, 0
    for start, end in sorted(intervals):
        if end <= start: continue
        total += max(0, end - max(start, cursor if cursor is not None else start))
        cursor = max(end, cursor if cursor is not None else end)
    return total


def _time_breakdown(row, states, frame):
    evidence = frame.get("evidence", {})
    available = evidence.get("scheduler_evidence_version") == "sched_switch_reason/v1"
    segments = evidence.get("scheduler_state_segments", [])
    preempted = [(max(row["start_ns"], item["start_ns"]), min(row["end_ns"], item["end_ns"]))
                 for item in segments if item.get("kind") == "runnable" and item.get("reason") == "preempted"]
    wait = min(states["runnable"], _union_duration(preempted) / 1e6) if available else 0
    partition = {"running": states["on_cpu"], "preempted_wait": wait,
                 "runnable_other": max(0, states["runnable"] - wait), "blocked": states["blocked"], "unknown": states["unknown"]}
    result = {"partition_ms": partition, "runnable_total_ms": states["runnable"],
        "preemption_evidence_available": available,
        "preemption_event_count": sum(item.get("kind") == "runnable" and item.get("reason") == "preempted"
            and row["start_ns"] <= item.get("source_timestamp_ns", item["start_ns"]) < row["end_ns"] for item in segments) if available else None,
        "notes": ["Interrupted 为 R+ 证据确认的抢占后等核，是 Runnable 子集；普通 R 不证明抢占。",
                  "Running 是调度驻留时间，可包含内核及中断；PMU 仅统计目标线程用户态。",
                  "Blocked 包含睡眠和同步服务等待；S / D 状态本身不能证明 I/O 原因。"]}
    for kind in ("irq", "softirq"):
        source = evidence.get("interrupt_evidence", {}).get(kind, {})
        overlaps = []
        for irq in source.get("intervals", []):
            for cpu in evidence.get("cpu_frequency_slices", []):
                if irq["cpu"] == cpu["cpu"]:
                    overlaps.append((max(row["start_ns"], irq["start_ns"], cpu["start_ns"]),
                                     min(row["end_ns"], irq["end_ns"], cpu["end_ns"])))
        result[kind + "_overlap_ms"] = _union_duration(overlaps) / 1e6 if source.get("event_count") else None
        result[kind + "_evidence_complete"] = bool(source.get("event_count") and not source.get("unmatched_events"))
    return result

def _metric_point(metric):
    """Return a displayed point without manufacturing a midpoint from bounds."""
    for field, source in (("exact", "exact"), ("estimate", "estimated"),
                          ("interior_estimate", "interior_estimate")):
        value = metric.get(field)
        if _number(value):
            return value, source
    return None, None


def _metric_band(metric, low, high):
    """Classify a point or a conclusive interval; broad intervals stay ambiguous."""
    point, source = _metric_point(metric)
    if point is not None:
        return ("low" if point <= low else "high" if point >= high else "middle"), point, source
    bounds = metric.get("bounds")
    if not bounds:
        return None, None, None
    lower, upper = bounds
    if upper <= low:
        return "low", bounds, "bounded"
    if lower >= high:
        return "high", bounds, "bounded"
    if lower > low and upper < high:
        return "middle", bounds, "bounded"
    return "ambiguous", bounds, "bounded"


def _meaningful_metric(metric):
    """A zero-to-upper boundary envelope with no covered sample is not evidence."""
    point, _ = _metric_point(metric)
    if point is not None:
        return True
    bounds = metric.get("bounds")
    return bool(bounds and (bounds[0] > 0 or bounds[0] == bounds[1]))


def _positive_count_metric(metric):
    """Bare count fallback needs observed work; a recorded zero is not activity."""
    point, _ = _metric_point(metric)
    if point is not None:
        return point > 0
    bounds = metric.get("bounds")
    return bool(bounds and bounds[0] > 0 and bounds[1] > 0)


def _format_metric_evidence(label, value, source):
    if isinstance(value, (list, tuple)):
        rendered = f"{value[0]:.3f}–{value[1]:.3f}"
    else:
        rendered = f"{value:.3f}"
    source_label = {"exact": "实测值", "estimated": "覆盖窗口估算值",
                    "interior_estimate": "内部窗口估算值", "bounded": "范围"}.get(source, "值")
    return f"{label} {source_label} {rendered}。"


def _classification_result(kind, evidence, *, confidence, quality, available, missing):
    return {"id": kind, "name": CLASSES[kind], "method": RULES["version"], "status": "candidate",
            "confidence": {"level": confidence[0], "score": confidence[1]},
            "evidence_quality": quality, "available_metrics": list(dict.fromkeys(available)),
            "missing_evidence": list(dict.fromkeys(missing)), "evidence": evidence,
            "parallel_implication": "需结合核组、频点与共跑组合实测校准；分型本身不保证并行收益。"}


def _classify_cpu(row, features):
    wall = row["wall_ms"]
    evidence = []
    if (features["usable"] and features["llc_mapping_verified"] and features["ipc_bounds"]
            and features["llc_mpki_bounds"] and wall and row["cpu_ms"] / wall >= RULES["minimum_cpu_share"]):
        ipc, mpki = features["ipc_bounds"], features["llc_mpki_bounds"]
        if ipc[0] >= RULES["compute_min_ipc"] and mpki[1] <= RULES["compute_max_llc_mpki"]:
            kind = "compute"
        elif ipc[1] <= RULES["memory_max_ipc"] and mpki[0] >= RULES["memory_min_llc_mpki"]:
            kind = "memory"
        else:
            kind = "mixed"
        evidence.extend([f"IPC 范围 {ipc[0]:.3f}–{ipc[1]:.3f}。", f"已核验 LLC MPKI 范围 {mpki[0]:.3f}–{mpki[1]:.3f}。"])
        return _classification_result(kind, evidence, confidence=("high", .95), quality="complete",
                                      available=["ipc", "verified_llc_mpki", "cpu_share"], missing=[])

    # Partial-evidence path: choose a usable curve instead of blocking all downstream
    # work, while preserving that the result is a low/medium-confidence candidate.
    metrics = features["metric_evidence"]
    votes = {"compute": 0.0, "memory": 0.0, "mixed": 0.0}
    available, domains = [], set()
    ipc_band, ipc_value, ipc_source = (None, None, None)
    if _meaningful_metric(metrics["ipc"]):
        ipc_band, ipc_value, ipc_source = _metric_band(
            metrics["ipc"], RULES["fallback_memory_max_ipc"], RULES["fallback_compute_min_ipc"])
    if ipc_band:
        available.append("ipc"); domains.add("pipeline")
        evidence.append(_format_metric_evidence("IPC", ipc_value, ipc_source))
        if ipc_band == "high":
            votes["compute"] += 2.0
        elif ipc_band == "low":
            votes["memory"] += 1.25
            evidence.append("低 IPC 只作为访存压力弱证据，也可能来自前端、分支或依赖停顿。")
        else:
            votes["mixed"] += 1.25 if ipc_band == "middle" else .5

    cache_key = None
    if features["llc_mapping_verified"] and _meaningful_metric(metrics["llc_mpki"]):
        cache_key = "llc_mpki"
    elif _meaningful_metric(metrics["cache_mpki"]):
        cache_key = "cache_mpki"
    elif _meaningful_metric(metrics["llc_mpki"]):
        cache_key = "llc_mpki"
    if cache_key:
        cache_band, cache_value, cache_source = _metric_band(
            metrics[cache_key], RULES["fallback_low_cache_mpki"], RULES["fallback_high_cache_mpki"])
        available.append(cache_key); domains.add("cache")
        label = "已核验 LLC MPKI" if cache_key == "llc_mpki" and features["llc_mapping_verified"] else (
            "通用 Cache MPKI" if cache_key == "cache_mpki" else "未核验 LLC MPKI")
        evidence.append(_format_metric_evidence(label, cache_value, cache_source))
        verified = cache_key == "llc_mpki" and features["llc_mapping_verified"]
        weight = 2.5 if verified else 1.5
        if cache_band == "high":
            votes["memory"] += weight
        elif cache_band == "low":
            votes["compute"] += weight * .75
        else:
            votes["mixed"] += weight * (1 if cache_band == "middle" else .4)
        if not verified:
            evidence.append("该缓存指标仅参与候选分型，不等同已核验 LLC 或 DRAM 带宽瓶颈。")

    raw_keys = ("instructions", "cycles", "cache_misses", "cache_references", "llc_misses",
                "l1d_misses", "dtlb_misses", "memory_stall_cycles", "loads", "stores")
    raw_available = [key for key in raw_keys if _positive_count_metric(metrics[key])]
    if raw_available:
        available.extend(raw_available)
        if not domains:
            domains.add("raw_counts")
            votes["mixed"] += .5
            evidence.append("仅有基础计数，能确认阶段存在 CPU / Cache 活动，但不足以判定单一主导因素。")

    missing = []
    if "ipc" not in available:
        missing.append("IPC（同组 instructions / cycles）")
    if not any(key in available for key in ("cache_mpki", "llc_mpki")):
        missing.append("Cache / LLC MPKI")
    if not features["llc_mapping_verified"]:
        missing.append("已核验 LLC 事件语义")
    if not features["usable"]:
        missing.append("充分的阶段边界与 PMU 运行覆盖")
    cpu_share = row["cpu_ms"] / wall if wall else 0
    if cpu_share < RULES["minimum_cpu_share"]:
        missing.append(f"CPU 运行占比达到 {RULES['minimum_cpu_share']:.0%}")

    if not available:
        evidence = features["reasons"] or ["没有可用 IPC、Cache/LLC MPKI 或基础计数，暂时无法形成候选分型。"]
        return _classification_result("unknown", evidence, confidence=("none", 0), quality="none",
                                      available=[], missing=missing)

    directional = "compute" if votes["compute"] > votes["memory"] else "memory"
    direction_score = votes[directional]
    opposite_score = votes["memory" if directional == "compute" else "compute"]
    conflict = opposite_score >= max(.75, direction_score * .5)
    if not direction_score or conflict or votes["mixed"] >= direction_score * .75:
        kind = "mixed"
    else:
        kind = directional
    if domains == {"raw_counts"}:
        confidence = ("low", .2)
        quality = "minimal"
    elif len(domains) >= 2 and features["usable"] and not conflict:
        confidence = ("medium", .68)
        quality = "partial"
    elif len(domains) >= 2:
        confidence = ("medium", .55)
        quality = "partial"
    else:
        confidence = ("low", .35)
        quality = "minimal"
    if conflict:
        confidence = ("low", min(confidence[1], .45))
        evidence.append("现有计算与缓存信号方向不一致，保守归为混合候选。")
    return _classification_result(kind, evidence, confidence=confidence, quality=quality,
                                  available=available, missing=missing)


def _classify(row, features):
    """Keep wall-time dominance separate from the Running workload candidate."""
    wall = row["wall_ms"]
    cpu_candidate = _classify_cpu(row, features)
    if wall and row["binder_blocked_ms"] / wall >= RULES["dominant_wait_share"]:
        primary = _classification_result(
            "service", [f"同步 Binder 阻塞占阶段 {row['binder_blocked_ms'] / wall:.1%}。"],
            confidence=("high", .9), quality="complete", available=["binder_blocked_share"], missing=[])
    elif wall and row["runnable_ms"] / wall >= RULES["dominant_wait_share"]:
        primary = _classification_result(
            "scheduler", [f"Runnable 等核占阶段 {row['runnable_ms'] / wall:.1%}。"],
            confidence=("high", .9), quality="complete", available=["runnable_share"], missing=[])
    else:
        return cpu_candidate
    primary["cpu_candidate"] = cpu_candidate
    return primary


class WorkloadFeaturesManager:
    def __init__(self, parallel_manager):
        self.parallel = parallel_manager
        self.state_root = parallel_manager.state_root / "workload-profiles"
        self.state_root.mkdir(parents=True, exist_ok=True)

    def catalog(self):
        return {"schema_version": "workload_features_catalog/v1", "engine_version": VERSION,
                "classes": [{"id": key, "name": value} for key, value in CLASSES.items()],
                "classification_rules": copy.deepcopy(RULES),
                "cpu_residency_contract": {"schema_version": CPU_RESIDENCY_EVIDENCE_SCHEMA,
                    "coverage_thresholds": copy.deepcopy(CPU_RESIDENCY_COVERAGE),
                    "statuses": ["complete", "partial", "insufficient", "unavailable"],
                    "source_anchor_rule": "Eligibility requires complete temporal coverage, verified hardware identity, and a known core group and positive frequency for every residency slice; absolute curve compatibility is checked by the consumer."},
                "feature_groups": [
                    {"id": "compute", "name": "计算特征", "metrics": ["instructions", "cycles", "IPC", "branch misses"]},
                    {"id": "memory", "name": "访存特征", "metrics": ["cache misses", "LLC MPKI", "loads / stores", "DRAM bytes"]},
                    {"id": "scheduling", "name": "调度与服务", "metrics": ["CPU / runnable / blocked", "Binder", "实际逻辑 CPU / 核组 / 频率", "核心驻留 / 迁移", "I/O bytes"]}],
                "measurement_contract": {"schema_version": "workload_stage_measurements/v1",
                    "dataset_id": "当前数据集 ID", "hardware_profile_id": "与数据集一致（未绑定硬件时为 null）",
                    "clock_domain": "trace_monotonic", "clock_alignment_evidence": "同轮时钟同步标记或逐阶段探针记录",
                    "provenance": {"source": "原始文件 / 采集记录", "run_id": "同轮采集 ID", "collector": "采集工具与版本"},
                    "stages": [{"stage_id": "build_00", "start_ns": "精确阶段起点", "end_ns": "精确阶段终点",
                        "tid": "目标线程", "measurement_scope": "thread_user", "counters": {
                            "instructions": {"value": "实测计数", "unit": "count", "event": "硬件事件名 / 编码"},
                            "llc_misses": {"value": "实测计数", "unit": "count", "event": "已核验 LLC 事件名 / 编码"}},
                        "event_mapping": {"llc_misses": {"verified": True, "cache_level": "LLC", "event": "与 counters.llc_misses.event 完全相同"}}}],
                    "supported_counters": COUNTERS},
                "notes": ["阶段顺序与并行建模共享 BuildItem 边界和 ID。", "主数值按同线程采样窗口的墙钟重叠比例估算，原始边界与覆盖单独保留。",
                          "估算假设窗口内计数均匀分布；未覆盖时间不外推，缺失指标保留 null。", "IPC / MPKI 使用同组同窗口计数；不完整证据只生成标注置信度的降级候选，不提升为硬件瓶颈事实。",
                          "整体阶段保留服务 / 调度主特征；Running 负载候选独立保存在 cpu_candidate，下游 workload_class 优先使用它。",
                          "阶段实际逻辑 CPU 来自 Trace sched_switch 驻留；核组只按数据集绑定硬件画像的 logical_cpu_ids 映射，未绑定时不按编号猜测核型。",
                          "CPU 驻留覆盖以阶段 cpu_ms 为期望值；时间覆盖 complete 之外，还需硬件身份已核验且每段核组/频率已知，才可作为执行时间迁移的源锚；绝对曲线兼容性由消费端继续门禁。",
                          "分母有效时的零 IPC / MPKI 是低值证据；单独的零指令、零周期或零 miss 计数不代表已观测到 CPU 活动。",
                          "I/O 阻塞不由普通 blocked 状态推断；未采集时保留缺失。"]}

    def list_profiles(self):
        values = [self.get_profile(path.stem) for path in self.state_root.glob("wf_*.json")]
        return [{key: row[key] for key in ("id", "name", "created_at", "dataset_id", "hardware_profile_id", "summary", "quality")}
                for row in sorted(values, key=lambda row: row["created_at"], reverse=True)]

    def get_profile(self, identity):
        if not isinstance(identity, str) or not re.fullmatch(r"wf_[0-9a-f]{24}", identity):
            _error("负载特征版本 ID 无效。")
        path = self.state_root / (identity + ".json")
        if not path.is_file():
            raise PlatformError("workload_features_not_found", "负载特征版本不存在。", status_code=404)
        value = json.loads(path.read_text())
        digest = value.get("profile_sha256")
        if digest != _hash({key: item for key, item in value.items() if key != "profile_sha256"}):
            _error("负载特征版本内容校验失败。")
        return value

    def dataset_fingerprint(self, dataset_id):
        dataset = self.parallel.get_dataset(dataset_id)
        directory = self.parallel.state_root / "inputs" / dataset["id"]
        parsed = json.loads((directory / "parsed.json").read_text())
        extracted = json.loads((directory / "extracted.json").read_text())
        hardware_id = dataset.get("hardware_profile_id")
        hardware_sha256 = (self.parallel.hardware_profiles.get(hardware_id).get("profile_sha256")
                           if hardware_id else None)
        return _hash({"parsed": parsed, "extracted": extracted,
                      "hardware_profile_id": hardware_id,
                      "hardware_profile_sha256": hardware_sha256})

    def _legacy_dataset_fingerprint(self, dataset_id):
        """Recognize pre-v1.5 profiles without promoting them to source anchors."""
        dataset = self.parallel.get_dataset(dataset_id)
        directory = self.parallel.state_root / "inputs" / dataset["id"]
        parsed = json.loads((directory / "parsed.json").read_text())
        extracted = json.loads((directory / "extracted.json").read_text())
        return _hash({"parsed": parsed, "extracted": extracted,
                      "hardware_profile_id": dataset.get("hardware_profile_id")})

    def resolve_for_dataset(self, profile_id, dataset):
        if profile_id is None or profile_id == "":
            return None
        profile = self.get_profile(profile_id)
        hardware_id = dataset.get("hardware_profile_id")
        current_hardware_sha256 = (self.parallel.hardware_profiles.get(hardware_id).get("profile_sha256")
                                   if hardware_id else None)
        profile_hardware_sha256 = profile.get("hardware_profile_sha256")
        current_fingerprint = self.dataset_fingerprint(dataset["id"])
        legacy_without_hardware_sha = (
            profile_hardware_sha256 is None
            and profile.get("dataset_fingerprint") == self._legacy_dataset_fingerprint(dataset["id"]))
        if (profile["dataset_id"] != dataset["id"] or profile.get("hardware_profile_id") != dataset.get("hardware_profile_id")
                or (not legacy_without_hardware_sha
                    and (profile_hardware_sha256 != current_hardware_sha256
                         or profile["dataset_fingerprint"] != current_fingerprint))):
            _error("负载特征版本与当前数据集、原始内容或硬件配置不一致。")
        for name, digest in profile.get("provenance", {}).get("pmu_estimate_rebuild", {}).get("raw_sha256", {}).items():
            if name not in ("pmu.csv", "pmu.json", "trace.ftrace"):
                _error("负载特征的原始 PMU 来源记录无效。")
            source = self.parallel.state_root / "inputs" / dataset["id"] / name
            if not source.is_file() or _file_hash(source) != digest:
                _error("重建负载特征的原始 PMU / Trace 数据已改变，请重新分析。")
        return profile

    def _measurements(self, payload, dataset, rows, frame):
        measurements = payload.get("measurements")
        if measurements is None:
            return {}, None
        if not isinstance(measurements, dict) or measurements.get("schema_version") != "workload_stage_measurements/v1":
            _error("阶段实测数据需要 workload_stage_measurements/v1 schema。")
        if measurements.get("dataset_id") != dataset["id"] or measurements.get("hardware_profile_id") != dataset.get("hardware_profile_id"):
            _error("阶段实测数据的数据集或硬件身份不匹配。")
        provenance = measurements.get("provenance")
        if not isinstance(provenance, dict) or any(not isinstance(provenance.get(key), str) or not provenance[key].strip() for key in ("source", "run_id", "collector")):
            _error("阶段实测数据需要 source、run_id 和 collector 来源信息。")
        if measurements.get("clock_domain") != "trace_monotonic" or not isinstance(measurements.get("clock_alignment_evidence"), str) or not measurements["clock_alignment_evidence"].strip():
            _error("阶段实测数据需说明与 Trace 单调时钟的对齐证据。")
        supplied = measurements.get("stages")
        if not isinstance(supplied, list) or not supplied or len(supplied) > len(rows):
            _error("stages 必须包含当前数据集内的非重复阶段。")
        by_id, result = {row["id"]: row for row in rows}, {}
        for entry in supplied:
            if (not isinstance(entry, dict) or not isinstance(entry.get("stage_id"), str)
                    or entry["stage_id"] not in by_id or entry["stage_id"] in result):
                _error("阶段 ID 不存在或重复。")
            row = by_id[entry["stage_id"]]
            if any(type(entry.get(key)) is not int or entry[key] != row[key] for key in ("start_ns", "end_ns")):
                _error("PMU 窗口必须精确对应阶段边界；整帧计数不能映射成阶段计数。", stage_id=row["id"])
            if type(entry.get("tid")) is not int or entry["tid"] != frame.get("tid"):
                _error("PMU 线程与阶段目标线程不一致。", stage_id=row["id"])
            if entry.get("measurement_scope") not in ("thread_user", "thread_user_kernel"):
                _error("PMU 作用域需为目标线程 user 或 user+kernel；进程/系统级统计不能用于阶段分型。")
            counters = entry.get("counters")
            if not isinstance(counters, dict) or not counters or any(key not in COUNTERS for key in counters):
                _error("阶段计数器包含未支持的指标。")
            for key, counter in counters.items():
                if (not isinstance(counter, dict) or not _number(counter.get("value")) or counter.get("unit") != COUNTERS[key]
                        or not isinstance(counter.get("event"), str) or not counter["event"].strip()):
                    _error("计数器需有非负有限值、正确单位与事件语义。", stage_id=row["id"], counter=key)
            mapping = entry.get("event_mapping", {})
            if not isinstance(mapping, dict) or not isinstance(mapping.get("llc_misses", {}), dict):
                _error("event_mapping 需包含事件语义对象。")
            llc_mapping = mapping.get("llc_misses", {})
            if llc_mapping.get("verified") is True and llc_mapping.get("cache_level") == "LLC":
                event = counters.get("llc_misses", {}).get("event")
                if not event or llc_mapping.get("event") != event:
                    _error("已核验 LLC 映射的 event 必须与 llc_misses 计数器事件一致。", stage_id=row["id"])
            result[row["id"]] = copy.deepcopy(entry)
        return result, copy.deepcopy(measurements)

    def analyze(self, payload):
        if not isinstance(payload, dict):
            _error("分析参数必须为 JSON 对象。")
        dataset = self.parallel.get_dataset(payload.get("dataset_id"))
        directory = self.parallel.state_root / "inputs" / dataset["id"]
        extracted = json.loads((directory / "extracted.json").read_text())
        parsed = json.loads((directory / "parsed.json").read_text())
        rows = component_features(extracted, parsed.get("component_features"))
        measurements, imported = self._measurements(payload, dataset, rows, parsed["frame"])
        frame, stages = parsed["frame"], []
        rebuild = _rebuild_sample_estimates(directory, frame, rows, parsed.get("selection", {}).get("trace_sha256"))
        frequency_slices = frame.get("evidence", {}).get("cpu_frequency_slices", [])
        wakes = frame.get("evidence", {}).get("wakeup_events", [])
        cpu_groups = {}
        hardware = None
        if dataset.get("hardware_profile_id"):
            hardware = self.parallel.hardware_profiles.get(dataset["hardware_profile_id"])
            for group in hardware.get("core_groups", hardware.get("cpu_groups", [])):
                identity = {"core_group_id": group.get("id"), "core_group_name": group.get("name"),
                            "proxy_kind": group.get("proxy_kind", group.get("kind"))}
                for cpu in group.get("logical_cpu_ids", []):
                    cpu_groups[cpu] = identity
        for row in rows:
            feature = _counter_features(row["pmu"], measurements.get(row["id"]))
            values, bounds = feature["values"], feature["bounds"]
            calls = [call for call in extracted["requests"] if call.get("component") == row["id"]]
            nodes = [node for node in extracted["nodes"] if node.get("component") == row["id"] and node["id"].startswith("app_")]
            states = {kind: sum(node["observed_duration_ms"] for node in nodes if node.get("state") == kind)
                      for kind in ("on_cpu", "runnable", "blocked", "unknown")}
            residency, sequence = Counter(), []
            for item in frequency_slices:
                duration = max(0, min(row["end_ns"], item["end_ns"]) - max(row["start_ns"], item["start_ns"])) / 1e6
                if duration:
                    residency[(item["cpu"], item.get("frequency_khz"))] += duration
                    if not sequence or sequence[-1] != item["cpu"]:
                        sequence.append(item["cpu"])
            residency_rows = [{"cpu": cpu, "frequency_khz": frequency, "cpu_ms": duration,
                **cpu_groups.get(cpu, {"core_group_id": None, "core_group_name": None, "proxy_kind": None})}
                for (cpu, frequency), duration in residency.items()]
            cpu_totals = Counter()
            for item in residency_rows:
                cpu_totals[item["cpu"]] += item["cpu_ms"]
            primary_cpu = min(cpu_totals, key=lambda cpu: (-cpu_totals[cpu], cpu)) if cpu_totals else None
            primary_group = cpu_groups.get(primary_cpu, {}) if primary_cpu is not None else {}
            observed_group_ids = sorted({item["core_group_id"] for item in residency_rows if item["core_group_id"]})
            residency_evidence = _cpu_residency_evidence(
                residency_rows, row.get("cpu_ms"), states.get("on_cpu"),
                hardware_identity_verified=dataset.get("quality", {}).get(
                    "hardware_identity_verified", False))
            residency_quality_note = None
            if not residency_evidence["source_anchor_eligible"]:
                ratio = residency_evidence["coverage_ratio"]
                coverage = f"{ratio:.1%}" if ratio is not None else "未知"
                residency_quality_note = (
                    f"CPU 驻留证据为 {residency_evidence['status']}，观测覆盖 {coverage}；"
                    f"源成本锚前提未满足（{', '.join(residency_evidence['reason_codes']) or 'unknown'}），"
                    "最长可见 CPU 不作为整阶段确定源锚。")
            placement = {"source": "trace_sched_switch_cpu_residency", "primary_cpu": primary_cpu,
                "primary_cpu_ms": cpu_totals.get(primary_cpu) if primary_cpu is not None else None,
                "primary_core_group_id": primary_group.get("core_group_id"),
                "primary_core_group_name": primary_group.get("core_group_name"),
                "primary_proxy_kind": primary_group.get("proxy_kind"),
                "observed_cpu_ids": sorted(cpu_totals), "observed_core_group_ids": observed_group_ids,
                "mixed_core_group_residency": len(observed_group_ids) > 1,
                "cpu_migrations": max(0, len(sequence) - 1) if sequence else None,
                "observed_on_cpu_ms": residency_evidence["observed_on_cpu_ms"],
                "expected_on_cpu_ms": residency_evidence["expected_on_cpu_ms"],
                "coverage_ratio": residency_evidence["coverage_ratio"],
                "coverage_status": residency_evidence["status"],
                "temporal_coverage_complete": residency_evidence["temporal_coverage_complete"],
                "source_cost_anchor_preconditions_met": residency_evidence["source_cost_anchor_preconditions_met"],
                "source_anchor_eligible": residency_evidence["source_anchor_eligible"],
                "residency_evidence": residency_evidence}
            classification = _classify(row, feature)
            running_classification = classification.get("cpu_candidate", classification)
            stages.append({"id": row["id"], "name": row["name"], "display_id": row["display_id"],
                "start_ns": row["start_ns"], "end_ns": row["end_ns"], "source_line": row["line"],
                "relative_start_ms": (row["start_ns"] - frame["start_ns"]) / 1e6,
                "wall_ms": row["wall_ms"], "cpu_ms": row["cpu_ms"], "states_ms": states,
                "time_breakdown": _time_breakdown(row, states, frame), "metric_evidence": feature["metric_evidence"],
                "compute": {**{key: values[key] for key in ("instructions", "cycles", "branch_instructions", "branch_misses")},
                    "ipc": feature["ipc"], "ipc_bounds": feature["ipc_bounds"], "interior_ipc": feature["interior_ipc"],
                    "instruction_bounds": bounds["instructions"], "cycle_bounds": bounds["cycles"]},
                "memory": {**{key: values[key] for key in ("cache_misses", "cache_references", "llc_misses", "loads", "stores", "dram_bytes", "memory_stall_cycles")},
                    "cache_miss_bounds": bounds["cache_misses"], "llc_mpki": feature["llc_mpki"],
                    "llc_mpki_bounds": feature["llc_mpki_bounds"], "cache_mpki_bounds": feature["cache_mpki_bounds"],
                    "llc_mapping_verified": feature["llc_mapping_verified"]},
                "scheduling": {"runnable_ms": states["runnable"], "blocked_ms": states["blocked"],
                    "binder_blocked_ms": row["binder_blocked_ms"], "other_wait_ms": row["other_wait_ms"],
                    "service_cpu_ms": row["service_cpu_ms"], "service_residence_ms": row["service_residence_ms"],
                    "service_calls": len(calls), "services": sorted(set(call["service"] for call in calls)),
                    "io_read_bytes": values["io_read_bytes"], "io_write_bytes": values["io_write_bytes"],
                    "io_wait_ms": None,
                    "wakeup_count": sum(row["start_ns"] <= item["timestamp_ns"] < row["end_ns"] and item.get("target_tid") == frame.get("tid") for item in wakes)
                        if "wakeup_events" in frame.get("evidence", {}) else None,
                    "cpu_migrations": placement["cpu_migrations"], "cpu_placement": placement,
                    "cpu_residency": residency_rows,
                    "cpu_residency_evidence": residency_evidence},
                "classification": classification,
                "workload_class": running_classification["id"] if running_classification["id"] in ("compute", "memory", "mixed") else "unknown",
                "quality": {"pmu_usable": feature["usable"], "pmu_source": feature["source"], "pmu_scope": feature["scope"],
                    "missing_counters": [key for key, value in bounds.items() if value is None],
                    "reasons": [*feature["reasons"], *([residency_quality_note] if residency_quality_note else [])],
                    "cpu_residency_status": residency_evidence["status"],
                    "source_placement_anchor_eligible": residency_evidence["source_anchor_eligible"],
                    "classification_quality": classification["evidence_quality"],
                    "classification_confidence": classification["confidence"],
                    "classification_missing_evidence": classification["missing_evidence"],
                    "running_classification_quality": running_classification["evidence_quality"],
                    "running_classification_confidence": running_classification["confidence"],
                    "running_classification_missing_evidence": running_classification["missing_evidence"],
                    "status": "partial" if feature["reasons"] or residency_quality_note
                        or any(value is None for value in bounds.values()) else "available"},
                "evidence": {"request_ids": row["request_ids"], "measurement": measurements.get(row["id"]), "pmu": row["pmu"]}})
        fingerprint = self.dataset_fingerprint(dataset["id"])
        identity = "wf_" + _hash({"dataset_id": dataset["id"], "dataset_fingerprint": fingerprint,
                                  "measurements": imported, "rules": RULES, "engine": VERSION,
                                  "profile_schema": PROFILE_SCHEMA,
                                  "hardware_profile_sha256": (hardware or {}).get("profile_sha256"),
                                  "pmu_estimate_rebuild": rebuild})[:24]
        path = self.state_root / (identity + ".json")
        if path.exists():
            return self.get_profile(identity)
        counts = Counter(stage["classification"]["id"] for stage in stages)
        workload_counts = Counter(stage["workload_class"] for stage in stages)
        residency_counts = Counter(stage["scheduling"]["cpu_residency_evidence"]["status"] for stage in stages)
        profile = {"schema_version": PROFILE_SCHEMA, "id": identity, "name": dataset["name"] + " · 负载特征",
            "created_at": utc_now(), "engine_version": VERSION, "dataset_id": dataset["id"], "dataset_fingerprint": fingerprint,
            "hardware_profile_id": dataset.get("hardware_profile_id"),
            "hardware_profile_sha256": (hardware or {}).get("profile_sha256"),
            "endpoint": "ui_callback", "status": "completed",
            "stages": stages, "component_classes": {stage["id"]: stage["workload_class"] for stage in stages},
            "summary": {"stage_count": len(stages), "duration_ms": frame["duration_us"] / 1000,
                "stage_wall_ms": sum(stage["wall_ms"] for stage in stages), "stage_cpu_ms": sum(stage["cpu_ms"] for stage in stages),
                "non_builditem_ms": max(0, frame["duration_us"] / 1000 - sum(stage["wall_ms"] for stage in stages)),
                "pmu_usable_stages": sum(stage["quality"]["pmu_usable"] for stage in stages),
                "cpu_residency_status_counts": dict(residency_counts),
                "cpu_residency_anchor_stages": sum(
                    stage["scheduling"]["cpu_residency_evidence"]["source_anchor_eligible"]
                    for stage in stages),
                "classified_cpu_stages": sum(stage["workload_class"] != "unknown" for stage in stages),
                "class_counts": dict(counts), "workload_class_counts": dict(workload_counts)},
            "quality": {"status": "partial" if any(stage["quality"]["status"] == "partial" for stage in stages) else "available",
                "hardware_identity_verified": dataset.get("quality", {}).get("hardware_identity_verified", False),
                "classification_is_candidate": True, "notes": self.catalog()["notes"]},
            "provenance": {"dataset": dataset["provenance"], "dataset_fingerprint": fingerprint,
                "classification_rules": copy.deepcopy(RULES), "measurements": imported,
                "pmu_estimate_rebuild": rebuild,
                "measurement_sha256": _hash(imported) if imported else None,
                "measurement_verification": "用户提供的逐阶段计数声明；校验身份、边界与单位，不等同独立硬件事件认证。" if imported else "同线程同钟采样窗口墙钟重叠估算；保留 PMU 保守区间，不将估值视为精确测量。"}}
        profile["profile_sha256"] = _hash(profile)
        self.parallel._write_json(path, profile)
        return profile


def create_workload_features_router(manager: WorkloadFeaturesManager):
    # Keep the analysis model importable by the Desktop SSH worker, whose
    # execution contract is intentionally independent from the Web API stack.
    from fastapi import APIRouter, Body
    from fastapi.responses import Response

    router = APIRouter(prefix="/v1/workload-features", tags=["workload-features"])

    @router.get("/catalog")
    def catalog(): return manager.catalog()
    @router.get("/profiles")
    def profiles(): return {"profiles": manager.list_profiles()}
    @router.get("/profiles/{identity}")
    def profile(identity: str): return manager.get_profile(identity)
    @router.get("/profiles/{identity}/export.csv")
    def export_csv(identity: str, view: Literal["stages", "pmu"] = "stages"):
        profile = manager.get_profile(identity)
        return Response(export_profile_csv(profile, view), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{profile["id"]}-{view}.csv"',
                                 "Cache-Control": "no-store"})
    @router.post("/analyze")
    def analyze(payload: dict = Body(...)): return manager.analyze(payload)
    return router
