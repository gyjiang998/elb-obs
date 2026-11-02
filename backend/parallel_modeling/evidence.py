"""Trace, synchronous Binder and interval-bounded PMU extraction; no plotting dependencies."""
import csv
import json
import re
import statistics
from collections import Counter, defaultdict
from .trace_parser import LINE_RE, state_totals
from .extract import nanoseconds

def timestamp_ns(text):
    seconds, fraction = text.split(".")
    return int(seconds) * 10**9 + int(fraction.ljust(9, "0"))

def events_in(trace, lower, upper):
    events = []
    interrupt_depth = Counter()
    with trace.open() as source:
        for number, line in enumerate(source, 1):
            match = LINE_RE.match(line)
            if not match:
                continue
            timestamp = timestamp_ns(match["ts"])
            cpu = int(match["cpu"])
            if match["event"] == "irq_handler_entry":
                interrupt_depth[cpu] += 1
            if timestamp > upper:
                break
            if timestamp >= lower:
                events.append({"line": number, "ns": timestamp, "tid": int(match["pid"]),
                               "cpu": cpu, "event": match["event"],
                               "interrupt_context": interrupt_depth[cpu] > 0,
                               "body": match["body"], "comm": match["comm"]})
            if match["event"] == "irq_handler_exit":
                interrupt_depth[cpu] = max(0, interrupt_depth[cpu] - 1)
    return events

def thread_states(events, tid, lower, upper):
    segments = []
    state, start = "unknown", lower
    for record in events:
        next_state = state
        if record["event"] == "sched_switch":
            switch = re.search(r"prev_pid=(\d+).*prev_state=(\S+).*next_pid=(\d+)", record["body"])
            if switch:
                if int(switch[1]) == tid:
                    next_state = "runnable" if switch[2].startswith("R") else "blocked"
                if int(switch[3]) == tid:
                    next_state = "on_cpu"
        elif record["event"] in ("sched_wakeup", "sched_wakeup_new"):
            target = re.search(r"\bpid=(\d+)\b", record["body"])
            if target and int(target[1]) == tid and state not in ("runnable", "on_cpu"):
                next_state = "runnable"
        if next_state != state:
            segments.append({"start_s": start / 1e9, "end_s": record["ns"] / 1e9, "state": state})
            state, start = next_state, record["ns"]
    segments.append({"start_s": start / 1e9, "end_s": upper / 1e9, "state": state})
    return segments

def binder_chains(events, main_tid, frame_start, frame_end, main_states):
    transactions = {}
    stacks = defaultdict(list)
    scopes = defaultdict(list)
    spans = []
    pairs = []
    anomalies = Counter()
    for record in events:
        event, body, tid = record["event"], record["body"], record["tid"]
        if event == "tracing_mark_write":
            parts = body.split("|", 2)
            if len(parts) == 3 and parts[0] == "B":
                scopes[tid].append({**record, "name": parts[2]})
            elif parts[0] == "E" and scopes[tid]:
                scope = scopes[tid].pop()
                spans.append({**scope, "end_ns": record["ns"], "end_line": record["line"]})
        if event == "binder_transaction":
            values = re.search(r"transaction=(\d+).*dest_proc=(\d+) dest_thread=(\d+) reply=(\d+) flags=(\S+)", body)
            if not values:
                anomalies["unparsed_send"] += 1
                continue
            transaction = {"id": int(values[1]), "send": record, "dest_pid": int(values[2]),
                           "dest_tid": int(values[3]), "reply": bool(int(values[4])), "flags": int(values[5], 16)}
            transactions[transaction["id"]] = transaction
            if transaction["reply"]:
                active = stacks[tid]
                if active and active[-1]["send"]["tid"] == transaction["dest_tid"]:
                    request = active.pop()
                    request["response"] = transaction
                else:
                    anomalies["reply_without_matching_active_request"] += 1
        elif event == "binder_transaction_received":
            identifier = re.search(r"transaction=(\d+)", body)
            transaction = transactions.get(int(identifier[1])) if identifier else None
            if transaction:
                transaction["receive"] = record
                if not transaction["reply"] and not transaction["flags"] & 1:
                    stacks[tid].append(transaction)
    for transaction in transactions.values():
        if transaction["reply"] or transaction["send"]["tid"] != main_tid:
            continue
        if not frame_start <= transaction["send"]["ns"] <= frame_end:
            continue
        row = {**transaction, "kind": "oneway_no_wait_inferred" if transaction["flags"] & 1 else "synchronous_unresolved"}
        receive = transaction.get("receive")
        response = transaction.get("response")
        if receive and response and response.get("receive"):
            start, end = transaction["send"]["ns"], response["receive"]["ns"]
            if response["receive"]["tid"] != main_tid or end < start:
                raise ValueError("Invalid Binder caller/ordering")
            service_tid = receive["tid"]
            service_states = thread_states(events, service_tid, events[0]["ns"], events[-1]["ns"])
            row.update(kind="synchronous_stack_matched", service_tid=service_tid,
                       duration_us=(end - start) / 1000,
                       caller_states_ms=state_totals(start / 1e9, end / 1e9, main_states),
                       service_states_ms=state_totals(receive["ns"] / 1e9, response["send"]["ns"] / 1e9, service_states),
                       service_scopes=[scope for scope in spans if scope["tid"] == service_tid and
                                       receive["ns"] <= scope["ns"] <= scope["end_ns"] <= response["send"]["ns"]],
                       evidence="Each send/receive uses exact transaction ID; request/reply pairing uses per-server-thread synchronous call stack and caller TID, not adjacent IDs")
            row["caller_wakeups"] = [record for record in events if record["event"] in ("sched_wakeup", "sched_wakeup_new")
                                      and start <= record["ns"] <= end and re.search(rf"\bpid={main_tid}\b", record["body"])]
            row["caller_resumes"] = [record for record in events if record["event"] == "sched_switch"
                                      and start <= record["ns"] <= end and re.search(rf"\bnext_pid={main_tid}\b", record["body"])]
            row["service_wakeups"] = [record for record in events if record["event"] in ("sched_wakeup", "sched_wakeup_new")
                                       and receive["ns"] <= record["ns"] <= response["send"]["ns"]
                                       and re.search(rf"\bpid={service_tid}\b", record["body"])]
            row["service_state_segments"] = [state for state in service_states if
                                               state["end_s"] > receive["ns"] / 1e9 and state["start_s"] < response["send"]["ns"] / 1e9]
        pairs.append(row)
    return pairs, dict(anomalies)

class Pmu:
    """Keep conservative bounds and wall-overlap estimates as distinct evidence.

    Snapshot midpoint windows partition time without overlapping adjacent reads.
    Apportioning their scaled deltas assumes uniform work within each window;
    neither that assumption nor perf multiplex scaling creates exact counts.
    """
    def __init__(self, directory, events, window=None):
        self.metadata = json.loads((directory / "pmu.json").read_text())
        grouped = self.metadata.get("format") == "grouped_long_csv"
        self.groups = {}
        metadata_groups = self.metadata.get("groups", []) if grouped else [{
            "id": self.metadata.get("group", "legacy"), "event_names": self.metadata.get("event_names", []), "status": "active"}]
        for meta in metadata_groups:
            self.groups[meta["id"]] = {"metadata": meta, "rows": [], "bins": [], "total_rows": 0, "window_started": False, "window_closed": False}
        with (directory / "pmu.csv").open() as source:
            for raw in csv.DictReader(source):
                group_id = raw.get("group_id") if grouped else metadata_groups[0]["id"]
                if group_id not in self.groups:
                    raise ValueError("PMU row references an unknown group")
                group = self.groups[group_id]
                group["total_rows"] += 1
                # Retain only the selected callback plus both boundary snapshots.
                # Parse row-by-row: full captures may contain millions of group reads.
                if window and group["window_closed"]:
                    continue
                row = {key: (int(value) if value not in (None, "") else None)
                       for key, value in raw.items() if key != "group_id"}
                if window and row["read_end_ns"] <= window[0] and not group["window_started"]:
                    group["rows"] = [row]
                else:
                    group["rows"].append(row)
                    group["window_started"] = True
                    if window and row["read_begin_ns"] >= window[1]:
                        group["window_closed"] = True
        self.rows = [row for group in self.groups.values() for row in group["rows"]]
        if len(self.rows) < 2:
            raise ValueError("At least two PMU snapshots are required")
        sync = [event for event in events if event["event"] == "tracing_mark_write" and
                re.search(r"(?:^|\|)PMU_CLOCK_SYNC\|" + re.escape(str(self.metadata.get("sync_begin_ns"))) + r"(?:\||$)", event["body"])]
        begin, end = self.metadata.get("sync_begin_ns"), self.metadata.get("sync_end_ns")
        self.clock_verified = (len(sync) == 1 and isinstance(begin, int) and isinstance(end, int)
                               and begin - 1000 <= sync[0]["ns"] <= end + 1000)
        rejected, gaps, reads = Counter(), [], []
        quality_groups = {}
        for identity, group in self.groups.items():
            rows, names = group["rows"], group["metadata"].get("event_names", [])
            if len(names) > 4 or len(set(names)) != len(names):
                raise ValueError("Invalid PMU group events")
            columns = [f"event{i}" for i in range(len(names))] if grouped else ["cycles", "instructions", "event2", "event3"][:len(names)]
            group_rejected = Counter()
            for previous, current in zip(rows, rows[1:]):
                missing = any(row.get(column) is None for row in (previous, current) for column in columns)
                values = {name: current[column] - previous[column] for name, column in zip(names, columns)} if not missing else {}
                enabled = current["enabled_ns"] - previous["enabled_ns"]
                running = current["running_ns"] - previous["running_ns"]
                reason = None
                if (missing or not names or previous["read_error"] or current["read_error"]
                        or previous["event_count"] != len(names) or current["event_count"] != len(names)):
                    reason = "invalid_read"
                elif min(values.values()) < 0 or enabled < 0 or running < 0:
                    reason = "counter_not_monotonic"
                elif (previous["read_end_ns"] < previous["read_begin_ns"] or current["read_begin_ns"] < previous["read_end_ns"]
                      or current["read_end_ns"] < current["read_begin_ns"]):
                    reason = "nonmonotonic_read_brackets"
                elif running > enabled:
                    reason = "counter_runtime_exceeds_enabled"
                elif running == 0 and any(values.values()):
                    reason = "counts_without_counter_runtime"
                if reason:
                    rejected[reason] += 1
                    group_rejected[reason] += 1
                group["bins"].append({"start_ns": previous["read_begin_ns"], "end_ns": current["read_end_ns"],
                    "inner_start_ns": previous["read_end_ns"], "inner_end_ns": current["read_begin_ns"],
                    "sample_start_ns": (previous["read_begin_ns"] + previous["read_end_ns"]) // 2,
                    "sample_end_ns": (current["read_begin_ns"] + current["read_end_ns"]) // 2,
                    "values": values, "enabled_ns": enabled, "running_ns": running,
                    "multiplexed": running < enabled, "valid": reason is None, "reason": reason})
            group_gaps = [(following["read_begin_ns"] - previous["read_begin_ns"]) / 1000 for previous, following in zip(rows, rows[1:])]
            gaps.extend(group_gaps)
            reads.extend((row["read_end_ns"] - row["read_begin_ns"]) / 1000 for row in rows)
            quality_groups[identity] = {**group["metadata"], "sample_count": group["total_rows"], "retained_sample_count": len(rows), "rejected_bins": dict(group_rejected),
                "median_period_us": statistics.median(group_gaps) if group_gaps else None,
                "multiplexed_bins": sum(entry["multiplexed"] for entry in group["bins"]),
                "zero_increment_events": [name for name, column in zip(names, columns) if len(rows) > 1
                    and rows[-1].get(column) is not None and rows[-1][column] == rows[0].get(column)]}
        self.bins = [entry for group in self.groups.values() for entry in group["bins"]]
        self.quality = {"sample_count": sum(group["total_rows"] for group in self.groups.values()), "retained_sample_count": len(self.rows),
            "analysis_window_ns": list(window) if window else None,
            "statistics_scope": "selected_callback_with_boundary_snapshots" if window else "full_capture",
            "median_period_us": statistics.median(gaps) if gaps else None,
            "max_period_us": max(gaps) if gaps else None, "median_read_us": statistics.median(reads) if reads else None,
            "max_read_us": max(reads) if reads else None, "rejected_bins": dict(rejected),
            "clock_verified_with_marker": self.clock_verified, "clock_markers": sync, "groups": quality_groups,
            "schema_version": self.metadata.get("schema_version", "thread_pmu_capture/v1"),
            "scope": self.metadata.get("scope", "target_thread_user_only_no_inherit")}
        self.quality["zero_increment_events"] = sorted(set(name for group in quality_groups.values() for name in group["zero_increment_events"]))
        self.quality["branch_semantics_warning"] = any("branch_instructions" in group["zero_increment_events"] for group in quality_groups.values())

    def _stage_group(self, group, start, end):
        names, rows = group["metadata"].get("event_names", []), group["rows"]
        intersect = [entry for entry in group["bins"] if entry["start_ns"] < end and entry["end_ns"] > start]
        contained = [entry for entry in intersect if entry["start_ns"] >= start and entry["end_ns"] <= end and entry["valid"]]
        covered = bool(rows) and rows[0]["read_end_ns"] <= start and rows[-1]["read_begin_ns"] >= end
        observed_valid = self.clock_verified and bool(intersect) and all(entry["valid"] for entry in intersect) and covered
        multiplexed = any(entry["multiplexed"] for entry in intersect)
        runtime_observed = any(entry["valid"] and entry["running_ns"] > 0 for entry in intersect)
        valid = observed_valid and not multiplexed and runtime_observed
        lower = {name: sum(entry["values"].get(name, 0) for entry in contained) for name in names}
        upper = {name: sum(entry["values"].get(name, 0) for entry in intersect if entry["valid"]) for name in names}
        usable_interior = [entry for entry in contained if entry["running_ns"] > 0]
        estimates = {name: sum(entry["values"][name] * entry["enabled_ns"] / entry["running_ns"]
                              for entry in usable_interior) for name in names} if self.clock_verified and usable_interior else None
        enabled = sum(entry["enabled_ns"] for entry in contained)
        runtime = sum(entry["running_ns"] for entry in contained)
        # All counters in one group use exactly the same bins and weights. The
        # denominator is the entire sampled wall interval, not stage duration;
        # missing/invalid intervals are omitted, never filled or extrapolated.
        weighted = []
        if self.clock_verified:
            for entry in intersect:
                overlap = max(0, min(end, entry["sample_end_ns"]) - max(start, entry["sample_start_ns"]))
                span = entry["sample_end_ns"] - entry["sample_start_ns"]
                if entry["valid"] and entry["running_ns"] > 0 and overlap > 0 and span > 0:
                    weighted.append((entry, overlap, overlap / span))
        weighted_estimates = {name: sum(entry["values"][name] * entry["enabled_ns"] / entry["running_ns"] * weight
                                       for entry, _, weight in weighted) for name in names} if weighted else None
        weighted_enabled = sum(entry["enabled_ns"] * weight for entry, _, weight in weighted)
        weighted_runtime = sum(entry["running_ns"] * weight for entry, _, weight in weighted)
        weighted_coverage = sum(overlap for _, overlap, _ in weighted) / (end - start)
        reasons = []
        if not self.clock_verified: reasons.append("clock_alignment_unverified")
        if not covered: reasons.append("stage_outside_sample_coverage")
        if not intersect: reasons.append("no_intersecting_samples")
        if any(not entry["valid"] for entry in intersect): reasons.append("invalid_counter_intervals")
        if multiplexed: reasons.append("counter_multiplexed_full_stage_upper_unknown")
        if not runtime_observed: reasons.append("no_counter_runtime_in_stage")
        if not contained: reasons.append("short_stage_no_interior_samples")
        if contained and not usable_interior: reasons.append("no_counter_runtime_in_interior")
        qualified = bool(valid and len(contained) >= 3 and lower.get("instructions", 0) > 0
                         and (upper["instructions"] - lower["instructions"]) / lower["instructions"] <= .25)
        if intersect and not qualified: reasons.append("boundary_or_runtime_uncertainty")
        if weighted:
            reasons.append("uniform_wall_time_attribution_assumption")
            if weighted_coverage < 1: reasons.append("weighted_estimate_partial_stage_coverage")
        result = {"group_id": group["metadata"]["id"], "event_names": names, "bounds_valid": valid,
            "observed_counter_bounds_valid": observed_valid,
            "observed_counter_lower": lower if observed_valid else None, "observed_counter_upper": upper if observed_valid else None,
            "lower": lower if valid else None, "upper": upper if valid else None,
            "interior_estimates": estimates, "estimate_scope": "interior_only" if estimates else None,
            "time_weighted_estimates": weighted_estimates,
            "weighted_estimate_scope": "covered_stage_window" if weighted else None,
            "weighted_estimate_method": "wall_time_overlap" if weighted else None,
            "sample_window_convention": "adjacent_read_bracket_midpoints",
            "weighted_time_coverage": weighted_coverage,
            "weighted_runtime_coverage": weighted_runtime / weighted_enabled if weighted_enabled else None,
            "weighted_bins": len(weighted),
            "estimate_time_fraction": sum(max(0, entry["inner_end_ns"] - entry["inner_start_ns"]) for entry in usable_interior) / (end - start) if estimates else 0,
            "runtime_coverage": runtime / enabled if enabled else None,
            "interior_bins": len(contained), "intersecting_bins": len(intersect),
            "boundary_bins": len(intersect) - len(contained),
            "interior_time_fraction": sum(max(0, entry["inner_end_ns"] - entry["inner_start_ns"]) for entry in contained) / (end - start),
            "max_intersecting_snapshot_span_us": max((entry["end_ns"]-entry["start_ns"])/1000 for entry in intersect) if intersect else None,
            "interior_ipc": estimates.get("instructions", 0) / estimates["cycles"] if estimates and estimates.get("cycles") else None,
            "scope": "target-thread user-mode only; wall-overlap estimates assume uniform work within sampled windows; no extrapolation across uncovered time; nested stages not additive",
            "reason_codes": reasons, "feature_usable_under_25pct_instruction_boundary_rule": qualified,
            "unavailable_events": group["metadata"].get("unavailable_events", [])}
        if valid and lower.get("cycles") and lower.get("instructions"):
            result["ipc_bounds"] = [lower["instructions"] / upper["cycles"], upper["instructions"] / lower["cycles"]]
            extras = [name for name in names if name not in ("instructions", "cycles")]
            result["event_per_cycle_bounds"] = {name: [lower[name] / upper["cycles"], upper[name] / lower["cycles"]] for name in extras}
            result["event_per_kinst_bounds"] = {name: [1000 * lower[name] / upper["instructions"], 1000 * upper[name] / lower["instructions"]] for name in extras}
            misses = next((name for name in reversed(extras) if "miss" in name), None)
            if misses:
                result["interior_misses_per_kinst"] = 1000 * lower[misses] / lower["instructions"]
                result["misses_per_kinst_bounds"] = result["event_per_kinst_bounds"][misses]
        return result

    def stage(self, start, end):
        if end <= start:
            raise ValueError("PMU stage window must be positive")
        groups = {identity: self._stage_group(group, start, end) for identity, group in self.groups.items()}
        primary = max(groups.values(), key=lambda group: (bool(group["bounds_valid"] or group["time_weighted_estimates"] or group["interior_estimates"]),
            group["group_id"] == "base", bool(group["bounds_valid"])))
        return {**primary, "groups": groups, "evidence_version": "grouped_stage_pmu/v3"}

def wait_ledger(parsed, frame, events, chains):
    rows = []
    for state in parsed["states"]:
        if state["state"] != "blocked":
            continue
        start, end = max(nanoseconds(state["start_s"]), frame["start_ns"]), min(nanoseconds(state["end_s"]), frame["end_ns"])
        if end <= start:
            continue
        wakes = [event for event in events if event["event"] in ("sched_wakeup", "sched_wakeup_new") and
                 abs(event["ns"] - end) <= 1000 and re.search(rf"\bpid={frame['tid']}\b", event["body"])]
        active = [span for span in frame["spans"] if span["start_ns"] <= start < span["end_ns"]]
        active.sort(key=lambda span: span["depth"])
        matched = [chain for chain in chains if chain["kind"] == "synchronous_stack_matched" and
                   chain["send"]["ns"] <= start and chain["response"]["receive"]["ns"] >= end]
        resumes = [event for event in events if event["event"] == "sched_switch" and event["ns"] >= end and
                   re.search(rf"\bnext_pid={frame['tid']}\b", event["body"])]
        rows.append({"start_ns": start, "end_ns": end, "blocked_us": (end-start)/1000,
                     "scope": active[-1]["name"] if active else None,
                     "build_scope": next((span["name"] for span in reversed(active) if "CustomNode:BuildItem" in span["name"]), None),
                     "scope_line": active[-1]["line"] if active else None,
                     "wakeup": wakes[0] if len(wakes) == 1 else None,
                     "resume": resumes[0] if resumes else None,
                     "wake_to_run_us": (resumes[0]["ns"]-end)/1000 if resumes else None,
                     "binder_request_ids": [chain["id"] for chain in matched],
                     "dependency": "synchronous_Binder_wait" if matched else "unknown_wait; wakeup alone not data dependency"})
    return rows
