"""Validate independent PMU streams without treating multiplexed estimates as exact."""
from __future__ import annotations

from array import array
import csv
import statistics
from pathlib import Path


class _GroupCounters:
    """Bound memory by intervals, not millions of raw CSV dictionaries."""
    def __init__(self, definition):
        self.definition = definition
        self.rows = self.good = self.baseline = self.read_errors = self.invalid = self.unscheduled = 0
        self.first_ns = self.last_ns = self.first_read_end = self.last_read_begin = self.previous_begin = self.previous_end = self.previous_tick = None
        self.first_counter = self.last_counter = self.previous_counter = None
        self.ordered = self.monotonic = True
        self.active_seen = False
        self.intervals = array("d")

    def consume(self, row):
        self.rows += 1
        events = self.definition["event_names"]
        begin, end, tick = int(row["read_begin_ns"]), int(row["read_end_ns"]), int(row["sample_index"])
        self.first_ns = begin if self.first_ns is None else self.first_ns
        self.last_ns = end
        self.first_read_end = end if self.first_read_end is None else self.first_read_end
        self.last_read_begin = begin
        self.ordered &= (0 <= begin <= end and tick >= 0
                         and (self.previous_end is None or begin >= self.previous_end)
                         and (self.previous_tick is None or tick > self.previous_tick))
        if self.previous_begin is not None and begin >= self.previous_begin:
            self.intervals.append((begin - self.previous_begin) / 1000)
        self.previous_begin, self.previous_end, self.previous_tick = begin, end, tick
        if int(row["read_error"]):
            self.read_errors += 1
            return
        values = [int(row[f"event{index}"]) for index in range(len(events))]
        enabled, running = int(row["enabled_ns"]), int(row["running_ns"])
        counter = [enabled, running, *values]
        regressed = self.previous_counter is not None and any(now < before for now, before in zip(counter, self.previous_counter))
        invalid_delta = self.previous_counter is not None and (
            running - self.previous_counter[1] > enabled - self.previous_counter[0]
            or (running == self.previous_counter[1] and any(now > before for now, before in zip(values, self.previous_counter[2:]))))
        self.monotonic &= not regressed and not invalid_delta
        self.previous_counter = counter
        if (int(row["event_count"]) != len(events) or not events or min(counter) < 0
                or running > enabled or regressed or invalid_delta):
            self.invalid += 1
            return
        self.first_counter = counter if self.first_counter is None else self.first_counter
        self.last_counter = counter
        if enabled == running == 0 and not any(values) and not self.active_seen:
            self.baseline += 1
        elif running == 0:
            self.unscheduled += 1
        else:
            self.active_seen = True
            self.good += 1


def validate_multi_pmu(directory: Path, metadata: dict, *, target_tid=None, step_window=None):
    diagnostics = []

    def note(code, message, suggestion="", severity="warning"):
        diagnostics.append({"code": code, "message": message, "suggestion": suggestion, "severity": severity})

    definitions = metadata.get("groups")
    if not isinstance(definitions, list) or not definitions:
        raise ValueError("Multi-group PMU metadata must declare its independent event groups")
    groups = {}
    for definition in definitions:
        if not isinstance(definition, dict):
            raise ValueError("Each PMU group definition must be an object")
        identity, events = definition.get("id"), definition.get("event_names")
        if (not isinstance(identity, str) or identity in groups or not isinstance(events, list)
                or not 0 <= len(events) <= 4 or any(not isinstance(name, str) or not name for name in events)
                or len(set(events)) != len(events)):
            raise ValueError("Invalid or duplicate multi-group PMU event definition")
        groups[identity] = _GroupCounters(definition)
    total = 0
    with (directory / "pmu.csv").open(encoding="utf-8") as stream:
        reader = csv.DictReader(stream)
        required = {"sample_index", "group_id", "read_begin_ns", "read_end_ns", "event_count", "enabled_ns", "running_ns", "read_error", "event0", "event1", "event2", "event3"}
        if not required <= set(reader.fieldnames or []):
            raise ValueError("Multi-group PMU CSV is missing required columns")
        for row in reader:
            if row["group_id"] not in groups:
                raise ValueError("PMU CSV contains an undeclared group")
            groups[row["group_id"]].consume(row)
            total += 1
    metadata_valid = (metadata.get("clock") == "CLOCK_MONOTONIC" and metadata.get("sample_count") == total
                      and (target_tid is None or metadata.get("target_tid") == target_tid))
    if not metadata_valid:
        note("PMU_METADATA_MISMATCH", "PMU 总样本数、时钟或目标线程与采集记录不一致。", severity="error")
    results, all_intervals, available, requested = [], array("d"), set(), set()
    for identity, group in groups.items():
        definition, row_count = group.definition, group.rows
        events = definition["event_names"]
        requested.update(definition.get("requested_event_names", events))
        unavailable = definition.get("unavailable_events", [])
        first_ns, last_ns = group.first_ns, group.last_ns
        first_counter, last_counter = group.first_counter, group.last_counter
        good, baseline, read_errors, invalid, unscheduled = group.good, group.baseline, group.read_errors, group.invalid, group.unscheduled
        intervals = group.intervals
        ordered, monotonic, active_seen = group.ordered, group.monotonic, group.active_seen
        enabled_delta = last_counter[0] - first_counter[0] if first_counter and last_counter else 0
        running_delta = last_counter[1] - first_counter[1] if first_counter and last_counter else 0
        ratio = running_delta / enabled_delta if enabled_delta > 0 else None
        multiplexed = ratio is not None and ratio < 1 - 1e-9
        valid = bool(metadata_valid and good >= 2 and running_delta > 0 and enabled_delta > 0 and ordered and monotonic
                     and definition.get("sample_count") == row_count)
        coverage = {"status": "not_measured", "first_sample_ns": first_ns, "last_sample_ns": last_ns,
                    "first_read_end_ns": group.first_read_end, "last_read_begin_ns": group.last_read_begin}
        if step_window is not None:
            start, end = round(step_window["start_s"] * 1e9), round(step_window["end_s"] * 1e9)
            full = group.first_read_end is not None and group.last_read_begin is not None and group.first_read_end <= start and group.last_read_begin >= end
            coverage.update(status="full" if full else "partial", step_start_ns=start, step_end_ns=end)
        complete = valid and not (read_errors or invalid or unscheduled or unavailable or definition.get("enable_error")) and coverage["status"] != "partial"
        status = ("estimated" if multiplexed else "collected") if valid else "not_running" if events and not active_seen else "unavailable" if not row_count else "invalid"
        if valid:
            available.update(events)
        if unavailable:
            note("PMU_EVENTS_UNAVAILABLE", f"{identity} 组部分事件无法打开：" + ", ".join(str(item.get("name")) for item in unavailable),
                 "其他事件继续采集；具体 errno 和失败阶段见各组记录。")
        if not valid and events:
            note("PMU_GROUP_NOT_RUNNING" if not active_seen else "PMU_GROUP_INVALID",
                 f"{identity} 组未获得足够的有效运行计数；已打开事件不等于已采到数据。",
                 "查看该组运行时间、事件错误与计数器资源；可选择该单组再次采集。")
        if valid and multiplexed:
            note("PMU_GROUP_LOW_COVERAGE" if ratio < .5 else "PMU_GROUP_MULTIPLEXED",
                 f"{identity} 组计数器运行覆盖为 {ratio:.1%}，只能在标明覆盖率后给出复用估计。",
                 "同组分子分母配对；缩放估计不是精确计数或严格上下界。必要时切换为单组。",
                 "warning" if ratio < .5 else "info")
        if valid and not complete:
            note("PMU_GROUP_PARTIAL", f"{identity} 组存在缺失事件、读取异常或采样窗口未完整覆盖。",
                 "使用保留下来的有效区间，并查看该组完整性记录。")
        all_intervals.extend(intervals)
        results.append({"id": identity, "status": status, "valid": valid, "quality_complete": complete,
                        "event_names": events, "requested_event_names": definition.get("requested_event_names", events),
                        "unavailable_events": unavailable, "enable_error": definition.get("enable_error", 0),
                        "rows": row_count, "usable_counter_rows": good, "inactive_baseline_rows": baseline,
                        "read_error_rows": read_errors, "invalid_counter_rows": invalid, "unscheduled_counter_rows": unscheduled,
                        "ordered_samples": ordered, "counters_monotonic": monotonic, "coverage": coverage,
                        "enabled_delta_ns": enabled_delta, "running_delta_ns": running_delta,
                        "running_ratio": ratio, "multiplexed": multiplexed,
                        "observed_median_interval_us": statistics.median(intervals) if intervals else None,
                        "maximum_interval_us": max(intervals, default=None)})
    valid = any(result["valid"] for result in results)
    if not valid:
        note("PMU_NO_USABLE_GROUP", "所有 PMU 组均未获得可用计数；查看事件权限、运行覆盖和组内错误。", severity="error")
    period_ns = metadata.get("period_ns_requested", 0)
    period = period_ns / 1000 if isinstance(period_ns, (int, float)) and period_ns > 0 else None
    effective_ns = metadata.get("period_ns_effective", period_ns)
    effective = effective_ns / 1000 if isinstance(effective_ns, (int, float)) and effective_ns > 0 else None
    skipped = int(metadata.get("skipped_deadlines", 0))
    maximum = max(all_intervals, default=None)
    degraded = bool(metadata.get("buffer_limited") or skipped or period and maximum is not None and maximum > 2 * period)
    if metadata.get("buffer_limited"):
        note("PMU_MULTI_BUFFER_LIMIT", f"多组采样缓冲限制为 128 MiB，请求 {period} μs，执行间隔调整为 {effective} μs。",
             "缩短采样窗口、调大目标间隔或切换单组以提高时间分辨率。")
    if degraded:
        note("PMU_SAMPLING_CADENCE_DEGRADED", "多组采样的实际间隔未达到请求值；分辨率按各组真实时间戳判断。",
             "查看实际中位间隔、最大间隔及跳过的采样时点。")
    if metadata.get("affinity_error", 0):
        note("PMU_READER_AFFINITY_FAILED", "采样器绑定指定 CPU 失败。")
    exit_path = directory / "pmu_exit_code.txt"
    exit_code = int(exit_path.read_text().strip()) if exit_path.is_file() else None
    signal = metadata.get("stopped_signal", 0)
    expected_stop = exit_code in (None, 0) and not signal or exit_code == 5 and signal == 15
    if not expected_stop:
        note("PMU_UNEXPECTED_STOP", f"多组采样器非预期退出：退出码 {exit_code}，信号 {signal}。")
    ordered = sorted(all_intervals)
    p95 = ordered[min(len(ordered) - 1, int((len(ordered) - 1) * .95))] if ordered else None
    return {"schema_version": "pmu_capture_validation/v3", "validator_version": "3.0.0", "format": "grouped_long_csv",
            "valid": valid, "quality_complete": valid and all(result["quality_complete"] for result in results) and expected_stop,
            "rows": total, "usable_counter_rows": sum(result["usable_counter_rows"] for result in results),
            "metadata_valid": metadata_valid, "metadata": metadata, "groups": results,
            "requested_event_names": sorted(requested), "available_event_names": sorted(available),
            "missing_event_names": sorted(requested - available), "diagnostics": diagnostics,
            "sampling_quality": {"status": "degraded" if degraded else "observed" if all_intervals else "not_measured",
                "requested_period_us": period, "effective_period_us": effective,
                "observed_median_interval_us": statistics.median(all_intervals) if all_intervals else None,
                "observed_p95_interval_us": p95, "maximum_interval_us": maximum, "skipped_deadlines": skipped,
                "running_ratio_min": min((result["running_ratio"] for result in results if result["running_ratio"] is not None), default=None)},
            "stop": {"exit_code": exit_code, "signal": signal, "expected_stop": expected_stop},
            "scope": "target_main_thread", "limitation": "Each group has independent scheduling and timestamp brackets. Multiplex-scaled values are estimates, not measured interval bounds; use group-local denominators."}
