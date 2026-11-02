"""Convert a trace frame into non-overlapping semantic work and evidence."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path

from .trace_parser import parse_trace, LINE_RE


def nanoseconds(seconds):
    return round(seconds * 1_000_000_000)


def semantic_type(name):
    for pattern, label in (
        ("OverlayManagerProxy", "resource_ipc"),
        ("CreateModuleContext", "resource_context"),
        ("ResourceManager", "resource_initialization"),
        ("BuildItem", "component_build"),
        ("OnAppear", "lifecycle"),
        ("aboutToAppear", "lifecycle"),
        ("CustomNodeUpdate", "state_update"),
        ("ExecuteRerender", "state_update"),
        ("FlushDirtyNodeUpdate", "state_update"),
        ("CreateTaskMeasure", "measure"),
        ("FlushLayout", "layout"),
        ("RenderTask", "render_preparation"),
        ("Canvas", "canvas"),
        ("FlushMessages", "submit"),
        ("SendCommands", "submit"),
        ("OnAreaChange", "area_callback"),
        ("ExecuteJS", "javascript"),
    ):
        if pattern in name:
            return label
    return "unclassified"


def state_phases(start_ns, end_ns, states):
    phases = []
    for state in states:
        lower = max(start_ns, nanoseconds(state["start_s"]))
        upper = min(end_ns, nanoseconds(state["end_s"]))
        if upper > lower:
            phases.append({"kind": state["state"], "start_ns": lower,
                           "end_ns": upper, "duration_us": (upper - lower) / 1000, "source_timestamp_ns": nanoseconds(state["start_s"]),
                           **{key: state[key] for key in ("reason", "source_event", "source_line", "prev_state", "cpu") if key in state}})
    if sum(round(phase["duration_us"] * 1000) for phase in phases) != end_ns - start_ns:
        raise ValueError("State coverage does not close")
    return phases


def extract_frame(trace, tid, vsync_id, partition):
    parsed = parse_trace(trace, tid)
    matches = [frame for frame in parsed["frames"] if frame["vsync_id"] == vsync_id]
    if len(matches) != 1:
        raise ValueError(f"Expected one frame {vsync_id}, found {len(matches)}")
    frame = matches[0]
    start_ns, end_ns = nanoseconds(frame["start_s"]), nanoseconds(frame["end_s"])
    spans = [span for span in parsed["spans"]
             if span["start_s"] >= frame["start_s"] and span["end_s"] <= frame["end_s"]]
    by_line = {span["line"]: span for span in spans}
    children = defaultdict(list)
    for span in spans:
        children[span["parent_line"]].append(span)
    for descendants in children.values():
        descendants.sort(key=lambda span: span["line"])
    units = []

    def emit(span, lower, upper, residual):
        if upper <= lower:
            return
        ancestors = []
        current = span
        while current:
            ancestors.append(current)
            current = by_line.get(current["parent_line"])
        build = next((scope for scope in ancestors if "CustomNode:BuildItem" in scope["name"]), None)
        component = re.search(r"BuildItem \[([^]]+)\]", build["name"]) if build else None
        phases = state_phases(lower, upper, parsed["states"])
        totals = Counter()
        for phase in phases:
            totals[phase["kind"]] += phase["duration_us"]
        units.append({
            "id": f"u{len(units):04d}", "name": span["name"] + (" :: self/residual" if residual else ""),
            "semantic_type": semantic_type(span["name"]), "source_line": span["line"],
            "source_end_line": span["end_line"], "ancestor_lines": [scope["line"] for scope in ancestors],
            "start_ns": lower, "end_ns": upper, "relative_start_us": (lower - start_ns) / 1000,
            "duration_us": (upper - lower) / 1000, "state_us": dict(totals), "phases": phases,
            "residual": residual, "build_scope_line": build["line"] if build else None,
            "component": component[1] if component else None,
            "thread_affinity": "main", "evidence": "target_measured",
            "features": {"instructions": None, "cycles": None, "cache_misses": None,
                         "memory_bytes": None, "provenance": "unavailable_in_historical_frame"},
        })

    def visit(span):
        lower, upper = nanoseconds(span["start_s"]), nanoseconds(span["end_s"])
        if span["duration_ms"] <= partition["preferred_max_ms"]:
            emit(span, lower, upper, False)
            return
        selected = [child for child in children[span["line"]]
                    if child["duration_ms"] >= partition["min_child_ms"]]
        cursor = lower
        for child in selected:
            child_start, child_end = nanoseconds(child["start_s"]), nanoseconds(child["end_s"])
            if child_start < cursor or child_end > upper:
                raise ValueError("Overlapping or invalid synchronous children")
            emit(span, cursor, child_start, True)
            visit(child)
            cursor = child_end
        emit(span, cursor, upper, True)

    visit(by_line[frame["line"]])
    for previous, following in zip(units, units[1:]):
        if previous["end_ns"] != following["start_ns"]:
            raise ValueError("Work units contain a gap or overlap")
    if sum(round(unit["duration_us"] * 1000) for unit in units) != end_ns - start_ns:
        raise ValueError("Work unit accounting does not close")
    evidence = trace_evidence(trace, tid, start_ns, end_ns)
    evidence["scheduler_evidence_version"] = parsed.get("scheduler_evidence_version")
    evidence["scheduler_state_segments"] = state_phases(start_ns, end_ns, parsed["states"])
    for unit in units:
        histogram = Counter()
        for segment in evidence["cpu_frequency_slices"]:
            overlap = max(0, min(unit["end_ns"], segment["end_ns"]) -
                          max(unit["start_ns"], segment["start_ns"]))
            if overlap:
                histogram[(segment["cpu"], segment["frequency_khz"])] += overlap / 1000
        unit["cpu_frequency_us"] = [{"cpu": cpu, "frequency_khz": frequency, "duration_us": duration}
                                    for (cpu, frequency), duration in histogram.items()]
    span_rows = []
    for span in spans:
        span_start, span_end = nanoseconds(span["start_s"]), nanoseconds(span["end_s"])
        direct = children[span["line"]]
        own_ns = span_end - span_start - sum(nanoseconds(child["end_s"]) - nanoseconds(child["start_s"])
                                           for child in direct)
        if own_ns < 0:
            raise ValueError("Negative exclusive scope time")
        span_rows.append({**span, "start_ns": span_start, "end_ns": span_end,
                          "exclusive_wall_us": own_ns / 1000})
    return {
        "schema_version": "1.0", "trace": str(trace.resolve()), "sha256": parsed["sha256"],
        "tid": tid, "vsync_id": vsync_id, "start_ns": start_ns, "end_ns": end_ns,
        "deadline_ns": nanoseconds(frame["expected_end_s"]) if "expected_end_s" in frame else None,
        "origin_ns": nanoseconds(parsed["origin_s"]), "duration_us": (end_ns - start_ns) / 1000,
        "state_us": {key: round(frame[key + "_ms"] * 1000, 3)
                     for key in ("on_cpu", "runnable", "blocked", "unknown")},
        "units": units, "spans": sorted(span_rows, key=lambda span: span["line"]),
        "parse_warnings": parsed["warnings"], "evidence": evidence,
        "notes": ["on_cpu includes interrupt residence; not pure application compute",
                  "Small critical events remain in spans and IPC evidence even if not separate work units",
                  "Residual scopes are localized but not semantically explained",
                  "Frequency values are trace-reported kHz, not independently verified silicon frequency"],
    }


def trace_evidence(trace, tid, start_ns, end_ns):
    frequencies = {}
    active_start = None
    active_cpu = None
    slices = []
    wakes = []
    transactions = {}
    received = {}
    telemetry = []
    interrupt_stacks = {"irq": defaultdict(list), "softirq": defaultdict(list)}
    interrupt_intervals = {"irq": [], "softirq": []}
    interrupt_events = Counter()
    interrupt_unmatched = Counter()

    def flush(timestamp):
        nonlocal active_start
        if active_start is not None:
            lower, upper = max(active_start, start_ns), min(timestamp, end_ns)
            if upper > lower:
                slices.append({"start_ns": lower, "end_ns": upper, "cpu": active_cpu,
                               "frequency_khz": frequencies.get(active_cpu)})
            active_start = timestamp

    with trace.open() as source:
        for line_number, line in enumerate(source, 1):
            match = LINE_RE.match(line)
            if not match:
                continue
            seconds, fraction = match["ts"].split(".")
            timestamp = int(seconds) * 1_000_000_000 + int(fraction.ljust(9, "0")[:9])
            if timestamp > end_ns:
                break
            event, body = match["event"], match["body"]
            for kind, prefix in (("irq", "irq_handler"), ("softirq", "softirq")):
                if event not in (prefix + "_entry", prefix + "_exit"):
                    continue
                cpu = int(match["cpu"])
                interrupt_events[kind] += 1
                if event.endswith("_entry"):
                    interrupt_stacks[kind][cpu].append((timestamp, line_number))
                elif interrupt_stacks[kind][cpu]:
                    begin, begin_line = interrupt_stacks[kind][cpu].pop()
                    if timestamp > start_ns and begin < end_ns:
                        interrupt_intervals[kind].append({"start_ns": max(begin, start_ns), "end_ns": min(timestamp, end_ns),
                            "cpu": cpu, "source_line": begin_line, "end_line": line_number})
                else:
                    interrupt_unmatched[kind] += 1
            if event == "cpu_frequency":
                frequency = re.search(r"state=(\d+) cpu_id=(\d+)", body)
                if frequency:
                    cpu = int(frequency[2])
                    if active_start is not None and active_cpu == cpu:
                        flush(timestamp)
                    frequencies[cpu] = int(frequency[1])
            if event == "sched_switch":
                switch = re.search(r"prev_pid=(\d+).*?next_pid=(\d+)", body)
                if switch:
                    if int(switch[1]) == tid:
                        flush(timestamp)
                        active_start = None
                    if int(switch[2]) == tid:
                        active_start, active_cpu = timestamp, int(match["cpu"])
            if timestamp < start_ns:
                continue
            record = {"line": line_number, "timestamp_ns": timestamp,
                      "source_tid": int(match["pid"]), "source_cpu": int(match["cpu"]), "body": body}
            if event in ("sched_wakeup", "sched_wakeup_new"):
                target = re.search(r"\bpid=(\d+)\b", body)
                if target and (int(target[1]) == tid or int(match["pid"]) == tid):
                    wakes.append({**record, "target_tid": int(target[1]), "relationship": "observed_wakeup_not_data_dependency"})
            if event == "binder_transaction":
                transaction = re.search(r"transaction=(\d+).*dest_proc=(\d+) dest_thread=(\d+) reply=(\d+)", body)
                if transaction:
                    transactions[int(transaction[1])] = {**record, "transaction_id": int(transaction[1]),
                                                        "dest_pid": int(transaction[2]), "dest_tid": int(transaction[3]),
                                                        "reply": bool(int(transaction[4]))}
            if event == "binder_transaction_received":
                transaction = re.search(r"transaction=(\d+)", body)
                if transaction:
                    received[int(transaction[1])] = record
            if (event == "clock_set_rate" and "ddr" in body.lower()) or "throughput" in event:
                telemetry.append({**record, "event": event, "semantics": "raw_unverified_not_per_task_bandwidth"})
    flush(end_ns)
    ipc = []
    for identifier, sent in transactions.items():
        receive = received.get(identifier)
        if sent["source_tid"] == tid or sent["dest_tid"] == tid or (receive and receive["source_tid"] == tid):
            ipc.append({**sent, "received": receive,
                        "delivery_us": (receive["timestamp_ns"] - sent["timestamp_ns"]) / 1000 if receive else None})
    return {"cpu_frequency_slices": slices, "wakeup_events": wakes,
            "ipc_transactions": ipc, "ddr_raw_events": telemetry,
            "interrupt_evidence": {kind: {"event_count": interrupt_events[kind], "intervals": interrupt_intervals[kind],
                "unmatched_events": interrupt_unmatched[kind] + sum(len(stack) for stack in interrupt_stacks[kind].values())}
                for kind in ("irq", "softirq")}}
