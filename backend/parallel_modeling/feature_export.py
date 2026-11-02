"""Tabular views of an immutable workload profile; no new measurement inference."""
from __future__ import annotations

import csv
import io
import math

# A saved stage flag is only meaningful under the profile contract that binds
# measured placement to an immutable hardware image. Keep these constants here
# so the producer and CSV consumer cannot silently drift apart.
SOURCE_ANCHOR_PROFILE_SCHEMA = "workload_feature_profile/v2"
SOURCE_ANCHOR_ENGINE_VERSION = "1.6.0"


def source_anchor_profile_contract_eligible(profile):
    """Whether stage-level source-anchor flags may be consumed as authoritative."""
    return bool(
        profile.get("schema_version") == SOURCE_ANCHOR_PROFILE_SCHEMA
        and profile.get("engine_version") == SOURCE_ANCHOR_ENGINE_VERSION
        and isinstance(profile.get("hardware_profile_sha256"), str)
        and profile["hardware_profile_sha256"].strip()
    )

# Keep stable event IDs and explicit units beside the Chinese spreadsheet labels.
# The legacy columns match workload-feature-display.ts; bounds are never averaged.
METRICS = (
    ("instructions", "指令数", "count", "compute", "instruction_bounds"),
    ("cycles", "周期数", "count", "compute", "cycle_bounds"),
    ("ipc", "IPC", "ratio", "compute", "ipc_bounds"),
    ("branch_instructions", "分支指令", "count", "compute", None),
    ("branch_misses", "分支预测失败", "count", "compute", None),
    ("branch_mpki", "Branch MPKI", "per_kinst", "compute", None),
    ("cache_references", "通用Cache访问", "count", "memory", None),
    ("cache_misses", "通用Cache miss", "count", "memory", "cache_miss_bounds"),
    ("cache_mpki", "Cache MPKI", "per_kinst", "memory", "cache_mpki_bounds"),
    ("llc_accesses", "LLC读访问", "count", "memory", None),
    ("llc_misses", "LLC miss", "count", "memory", None),
    ("llc_mpki", "LLC MPKI", "per_kinst", "memory", "llc_mpki_bounds"),
    ("l1d_accesses", "L1D读访问", "count", "memory", None),
    ("l1d_misses", "L1D miss", "count", "memory", None),
    ("l1d_mpki", "L1D MPKI", "per_kinst", "memory", None),
    ("dtlb_accesses", "DTLB读访问", "count", "memory", None),
    ("dtlb_misses", "DTLB miss", "count", "memory", None),
    ("dtlb_mpki", "DTLB MPKI", "per_kinst", "memory", None),
    ("stalled_frontend", "前端停顿周期", "count", "compute", None),
    ("stalled_backend", "后端停顿周期", "count", "compute", None),
    ("memory_stall_cycles", "访存停顿周期", "count", "memory", None),
    ("loads", "Load", "count", "memory", None),
    ("stores", "Store", "count", "memory", None),
    ("dram_bytes", "DRAM字节", "bytes", "memory", None),
    ("io_read_bytes", "I/O读取字节", "bytes", "scheduling", None),
    ("io_write_bytes", "I/O写入字节", "bytes", "scheduling", None),
)


def _number(value):
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value) else None


def _first(*values):
    return next((value for value in values if _number(value) is not None), None)


def _interval(value):
    return value if (isinstance(value, (list, tuple)) and len(value) == 2
                     and all(_number(item) is not None for item in value) and value[0] <= value[1]) else (None, None)


def _percent(value):
    return value * 100 if _number(value) is not None else None


def _joined(value):
    return " | ".join(str(item) for item in value) if isinstance(value, list) else value


def _cell(value):
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return value if _number(value) is not None else ""
    value = str(value)
    # A component/service name is untrusted text, never a spreadsheet formula.
    if value.startswith(("\t", "\r", "\n")) or value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def _metric(stage, definition):
    key, _, unit, area, legacy_bounds = definition
    declared = stage.get("metric_evidence", {}).get(key) or {}
    if declared:
        return {**declared, "unit": declared.get("unit") or unit,
                "exact": _number(declared.get("exact")), "estimate": _number(declared.get("estimate"))}
    legacy = stage.get(area, {})
    exact = _number(legacy.get(key))
    bounds = _interval(legacy.get(legacy_bounds)) if legacy_bounds else (None, None)
    pmu = stage.get("evidence", {}).get("pmu", {})
    return {"exact": exact, "estimate": None, "bounds": bounds,
            "status": "exact" if exact is not None else "bounded" if bounds[0] is not None else "unavailable",
            "unit": unit, "scope": pmu.get("scope"),
            "time_coverage": pmu.get("interior_time_fraction"), "interior_bins": pmu.get("interior_bins"),
            "max_boundary_span_us": pmu.get("max_intersecting_snapshot_span_us"),
            "hardware_mapping_verified": stage.get("memory", {}).get("llc_mapping_verified") if key.startswith("llc_") else None,
            "reason_codes": ["legacy_profile_without_metric_evidence"]}


def _value(metric):
    if metric.get("status") == "exact":
        return _number(metric.get("exact"))
    if metric.get("status") == "estimated":
        return _number(metric.get("estimate"))
    return None


def _identity(profile, stage):
    return {"特征版本ID": profile["id"], "数据集ID": profile["dataset_id"],
            "阶段ID": stage["id"], "阶段编号": stage.get("display_id"), "阶段名称": stage["name"]}


def _placement(schedule):
    declared = schedule.get("cpu_placement") or {}
    if declared:
        return declared
    residency = schedule.get("cpu_residency", [])
    totals = {}
    for item in residency:
        cpu = item.get("cpu")
        duration = _number(item.get("cpu_ms"))
        if isinstance(cpu, int) and duration is not None:
            totals[cpu] = totals.get(cpu, 0) + duration
    primary = min(totals, key=lambda cpu: (-totals[cpu], cpu)) if totals else None
    source = next((item for item in residency if item.get("cpu") == primary), {})
    groups = sorted({item.get("core_group_id") for item in residency if item.get("core_group_id")})
    return {"primary_cpu": primary, "primary_cpu_ms": totals.get(primary),
            "primary_core_group_id": source.get("core_group_id"),
            "primary_core_group_name": source.get("core_group_name"),
            "primary_proxy_kind": source.get("proxy_kind"),
            "observed_cpu_ids": sorted(totals), "observed_core_group_ids": groups,
            "mixed_core_group_residency": len(groups) > 1}


def _residency_evidence(stage, schedule, placement):
    declared = placement.get("residency_evidence") or schedule.get("cpu_residency_evidence") or {}
    observed = _first(declared.get("observed_on_cpu_ms"), placement.get("observed_on_cpu_ms"),
                      sum(_number(item.get("cpu_ms")) or 0 for item in schedule.get("cpu_residency", [])))
    expected = _first(declared.get("expected_on_cpu_ms"), placement.get("expected_on_cpu_ms"),
                      stage.get("cpu_ms"), stage.get("states_ms", {}).get("on_cpu"))
    ratio = _first(declared.get("coverage_ratio"), declared.get("coverage"),
                   placement.get("coverage_ratio"))
    if ratio is None and expected and observed is not None:
        ratio = observed / expected
    status = declared.get("status") or placement.get("coverage_status")
    if status not in ("complete", "partial", "insufficient", "unavailable"):
        conserved = bool(expected and observed is not None
                         and abs(observed - expected) <= max(.001, expected * .001))
        status = ("unavailable" if not expected or not observed else
                  "complete" if conserved else
                  "insufficient" if ratio is None or ratio > 1.05 or ratio < .70 else
                  "partial")
    eligible = declared.get("source_anchor_eligible", placement.get("source_anchor_eligible"))
    if not isinstance(eligible, bool):
        eligible = status == "complete"
    return {"observed_on_cpu_ms": observed, "expected_on_cpu_ms": expected,
            "coverage_ratio": ratio, "status": status, "source_anchor_eligible": eligible}


def _stage_row(profile, stage):
    timing, schedule, states = stage.get("time_breakdown", {}), stage.get("scheduling", {}), stage.get("states_ms", {})
    partition = timing.get("partition_ms", {})
    known = timing.get("preemption_evidence_available") is True
    running = _first(partition.get("running"), stage.get("cpu_ms"), states.get("on_cpu"))
    wall, start = _number(stage.get("wall_ms")), _number(stage.get("relative_start_ms"))
    quality = stage.get("quality", {})
    classification = stage.get("classification", {})
    confidence = classification.get("confidence", {})
    running_classification = classification.get("cpu_candidate") or classification
    running_confidence = running_classification.get("confidence", {})
    placement = _placement(schedule)
    residency_evidence = _residency_evidence(stage, schedule, placement)
    source_anchor_eligible = (source_anchor_profile_contract_eligible(profile)
                              and residency_evidence["source_anchor_eligible"])
    row = {**_identity(profile, stage), "特征版本名称": profile.get("name"), "版本创建时间": profile.get("created_at"),
           "版本SHA256": profile.get("profile_sha256"), "硬件画像ID": profile.get("hardware_profile_id"),
           "硬件身份已核验": profile.get("quality", {}).get("hardware_identity_verified"),
           "相对回调起点(ms)": start, "相对回调终点(ms)": start + wall if start is not None and wall is not None else None,
           "墙钟耗时(ms)": wall, "Running(ms)": running,
           "Runnable总计(ms)": _first(timing.get("runnable_total_ms"), states.get("runnable"), schedule.get("runnable_ms")),
           "Interrupted抢占等待_Runnable子集(ms)": _number(partition.get("preempted_wait")) if known else None,
           "其余Runnable(ms)": _number(partition.get("runnable_other")) if known else None,
           "抢占来源可区分": known,
           "Blocked(ms)": _first(partition.get("blocked"), states.get("blocked"), schedule.get("blocked_ms")),
           "状态未知(ms)": _first(partition.get("unknown"), states.get("unknown")),
           "Running占比(%)": 100 * running / wall if wall and running is not None else None,
           "Binder同步阻塞_Blocked子集(ms)": schedule.get("binder_blocked_ms"),
           "服务端CPU耗时_非本线程分区(ms)": schedule.get("service_cpu_ms"),
           "服务停留耗时_非额外墙钟分区(ms)": schedule.get("service_residence_ms"),
           "服务调用次数": schedule.get("service_calls"), "服务名称": _joined(schedule.get("services")),
           "唤醒次数": schedule.get("wakeup_count"), "CPU迁核次数": schedule.get("cpu_migrations"),
           "主要运行逻辑CPU": placement.get("primary_cpu"),
           "主要CPU驻留(ms)": placement.get("primary_cpu_ms"),
           "主要核组ID": placement.get("primary_core_group_id"),
           "主要核组名称": placement.get("primary_core_group_name"),
           "主要核组代理类型": placement.get("primary_proxy_kind"),
           "观测逻辑CPU": _joined(placement.get("observed_cpu_ids")),
           "观测核组": _joined(placement.get("observed_core_group_ids")),
           "跨核组驻留": placement.get("mixed_core_group_residency"),
           "CPU驻留观测(ms)": residency_evidence["observed_on_cpu_ms"],
           "CPU驻留期望(ms)": residency_evidence["expected_on_cpu_ms"],
           "CPU驻留覆盖(%)": _percent(residency_evidence["coverage_ratio"]),
           "CPU驻留覆盖状态": residency_evidence["status"],
           "CPU驻留可作为源锚": source_anchor_eligible,
           "抢占事件次数": timing.get("preemption_event_count") if known else None,
           "I/O等待(ms)": schedule.get("io_wait_ms"),
           "IRQ重叠_不另计耗时(ms)": timing.get("irq_overlap_ms"),
           "SoftIRQ重叠_不另计耗时(ms)": timing.get("softirq_overlap_ms"),
           "IRQ证据完整": timing.get("irq_evidence_complete"), "SoftIRQ证据完整": timing.get("softirq_evidence_complete"),
           "CPU驻留_CPU@核组@kHz:ms": " | ".join(f"CPU{item.get('cpu')}@{item.get('core_group_name') or '核型未知'}@{item.get('frequency_khz') if item.get('frequency_khz') is not None else '未知'}:{item.get('cpu_ms')}"
                                            for item in schedule.get("cpu_residency", [])),
           "负载分类候选": classification.get("name"), "负载分类ID": classification.get("id"),
           "分型置信度": confidence.get("level"), "分型置信分数": confidence.get("score"),
           "分型证据质量": classification.get("evidence_quality"),
           "分型已用指标": _joined(classification.get("available_metrics")),
           "分型缺失证据": _joined(classification.get("missing_evidence")),
           "分型依据": _joined(classification.get("evidence")),
           "Running负载候选": running_classification.get("name"), "Running负载分类ID": running_classification.get("id"),
           "Running分型置信度": running_confidence.get("level"), "Running分型置信分数": running_confidence.get("score"),
           "Running分型证据质量": running_classification.get("evidence_quality"),
           "Running分型已用指标": _joined(running_classification.get("available_metrics")),
           "Running分型缺失证据": _joined(running_classification.get("missing_evidence")),
           "Running分型依据": _joined(running_classification.get("evidence")),
           "PMU来源": quality.get("pmu_source"), "PMU作用域": quality.get("pmu_scope"),
           "LLC硬件映射已核验": stage.get("memory", {}).get("llc_mapping_verified"),
           "质量说明": _joined(quality.get("reasons")), "调度说明": _joined(timing.get("notes"))}
    for definition in METRICS:
        key, label, unit, *_ = definition
        metric = _metric(stage, definition)
        row[f"{key}_{label}({unit})"] = _value(metric)
        row[f"{key}_状态"] = metric.get("status", "unavailable")
    return row


def _pmu_rows(profile, stage):
    schedule = stage.get("scheduling", {})
    placement = _placement(schedule)
    residency_evidence = _residency_evidence(stage, schedule, placement)
    source_anchor_eligible = (source_anchor_profile_contract_eligible(profile)
                              and residency_evidence["source_anchor_eligible"])
    for definition in METRICS:
        key, label, *_ = definition
        metric = _metric(stage, definition)
        lower, upper = _interval(metric.get("bounds"))
        observed_lower, observed_upper = _interval(metric.get("observed_counter_bounds"))
        yield {**_identity(profile, stage), "相对回调起点(ms)": stage.get("relative_start_ms"),
               "阶段墙钟耗时(ms)": stage.get("wall_ms"), "指标ID": key, "指标名称": label,
               "主要运行逻辑CPU": placement.get("primary_cpu"),
               "主要核组ID": placement.get("primary_core_group_id"),
               "主要核组名称": placement.get("primary_core_group_name"),
               "主要核组代理类型": placement.get("primary_proxy_kind"),
               "CPU驻留观测(ms)": residency_evidence["observed_on_cpu_ms"],
               "CPU驻留期望(ms)": residency_evidence["expected_on_cpu_ms"],
               "CPU驻留覆盖(%)": _percent(residency_evidence["coverage_ratio"]),
               "CPU驻留覆盖状态": residency_evidence["status"],
               "CPU驻留可作为源锚": source_anchor_eligible,
               "单位": metric["unit"], "数值": _value(metric), "状态": metric.get("status", "unavailable"),
               "阶段对齐计数值": metric.get("exact"), "估算值": metric.get("estimate"),
               "阶段保守下界": lower, "阶段保守上界": upper,
               "已观测贡献下界_非全阶段界": observed_lower, "已观测贡献上界_非全阶段界": observed_upper,
               "采集组": metric.get("group_id"), "作用域": metric.get("scope"),
               "估算方法": metric.get("estimate_method"), "估算覆盖范围": metric.get("estimate_scope"),
               "有效时间覆盖(%)": _percent(metric.get("time_coverage")),
               "计数器运行覆盖(%)": _percent(metric.get("runtime_coverage")),
               "完整内部窗口覆盖(%)": _percent(metric.get("interior_time_coverage")),
               "内部采样区间数": metric.get("interior_bins"), "跨边界区间数": metric.get("boundary_bins"),
               "加权窗口数": metric.get("weighted_bins"), "最大跨边界读取跨度(us)": metric.get("max_boundary_span_us"),
               "硬件映射已核验": metric.get("hardware_mapping_verified"),
               "采样窗口定义": metric.get("sample_window_convention"),
               "原因代码": _joined(metric.get("reason_codes")),
               "数据来源": stage.get("quality", {}).get("pmu_source")}


def export_profile_csv(profile, view="stages"):
    """Return Excel-readable UTF-8 CSV with numeric cells and explicit missingness."""
    if view not in ("stages", "pmu"):
        raise ValueError("Unknown workload CSV view")
    rows = ([_stage_row(profile, stage) for stage in profile["stages"]] if view == "stages"
            else [row for stage in profile["stages"] for row in _pmu_rows(profile, stage)])
    stream = io.StringIO(newline="")
    if rows:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]), lineterminator="\r\n")
        writer.writeheader()
        writer.writerows({key: _cell(value) for key, value in row.items()} for row in rows)
    return stream.getvalue().encode("utf-8-sig")
