#!/usr/bin/env python3
"""Audit explicit jank reports and main-thread frame work in Gallery case traces."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections import Counter
from pathlib import Path

LINE_RE = re.compile(r"^\s*(?P<comm>.+)-(?P<pid>\d+)\s+(?:\(\s*(?P<tgid>\d+|-----)\)\s+)?\[(?P<cpu>\d+)\].*?\s(?P<ts>\d+\.\d+):\s(?P<event>[^:]+):\s(?P<body>.*)$")


SWITCH_RE = re.compile(r"prev_pid=(\d+).*?prev_state=(\S+).*?next_pid=(\d+)")
WAKE_RE = re.compile(r"\bpid=(\d+)\b")
VSYNC_RE = re.compile(r"now:(\d+) expectedEnd:\s*(\d+) vsyncId:(\d+)")
FRAME_RE = re.compile(r"UIVsyncTask\[timestamp:(\d+)\]\[vsyncID:(\d+)\]\[instanceID:")
JANK_RE = re.compile(r"JANK_FRAME_(UNFILTERED|FILTERED): skipppedFrameTime=(\d+)\(ms\)")
RS_RE = re.compile(
    r"ReportEventJankFrame maxFrameTime is (\d+)ms, maxHitchTime is (\d+)ms, "
    r"maxTechFrameTime is (\d+)ms, maxRealFrameTime is (\d+)ms,"
    r"maxFrameRefreshRate is (\d+)fps: (.*)"
)
RATIO_RE = re.compile(r"ReportEventHitchTimeRatio hitch time ratio is ([\d.]+) ms/s: (.*)")




def overlap_ms(start, end, other_start, other_end):
    return max(0.0, min(end, other_end) - max(start, other_start)) * 1000

def state_totals(start, end, states):
    totals = {"on_cpu_ms": 0.0, "runnable_ms": 0.0, "blocked_ms": 0.0, "unknown_ms": 0.0}
    for segment in states:
        totals[segment["state"] + "_ms"] += overlap_ms(start, end, segment["start_s"], segment["end_s"])
    assert abs(sum(totals.values()) - (end - start) * 1000) < 0.01
    return {key: round(value, 6) for key, value in totals.items()}

def parse_trace(trace, app_tid):
    validation = json.loads((trace.parent / "trace_validation.json").read_text())
    origin = validation["first_event_s"]
    last_event = validation["last_event_s"]
    stack = []
    spans = []
    frames = []
    reports = []
    rs_reports = []
    ratios = []
    states = []
    state = "unknown"
    state_start = origin
    state_evidence = {"reason": "unobserved", "source_event": None, "source_line": None}
    warnings = Counter()
    digest = hashlib.sha256()

    def transition(timestamp, next_state, **evidence):
        nonlocal state, state_start, state_evidence
        if next_state == state and evidence.get("reason") == state_evidence.get("reason"):
            return
        assert timestamp >= state_start
        if timestamp > state_start:
            states.append({"start_s": state_start, "end_s": timestamp, "state": state, **state_evidence})
        state, state_start = next_state, timestamp
        state_evidence = evidence

    with trace.open("rb") as source:
        for line_number, raw_line in enumerate(source, 1):
            digest.update(raw_line)
            line = raw_line.decode("utf-8", errors="replace")
            if not any(event in line for event in ("tracing_mark_write:", "sched_switch:", "sched_wakeup:", "sched_wakeup_new:")):
                continue
            match = LINE_RE.match(line)
            if not match:
                warnings["unparsed_relevant_lines"] += 1
                continue
            event = match["event"]
            body = match["body"]
            timestamp = float(match["ts"])
            tid = int(match["pid"])
            if event == "sched_switch":
                switch = SWITCH_RE.search(body)
                if not switch:
                    warnings["unparsed_switches"] += 1
                    continue
                previous_tid, previous_state, next_tid = switch.groups()
                if int(previous_tid) == app_tid:
                    runnable = previous_state.startswith("R")
                    # Linux/Harmony sched_switch marks explicit preemption with
                    # R+. Plain R also occurs for voluntary runnable switch-out.
                    transition(timestamp, "runnable" if runnable else "blocked",
                               reason="preempted" if runnable and "+" in previous_state else
                                      "runnable_switch_out" if runnable else "sleeping_switch_out",
                               source_event=event, source_line=line_number, prev_state=previous_state,
                               cpu=int(match["cpu"]))
                if int(next_tid) == app_tid:
                    transition(timestamp, "on_cpu", reason="scheduled_in", source_event=event,
                               source_line=line_number, cpu=int(match["cpu"]))
                continue
            if event in ("sched_wakeup", "sched_wakeup_new"):
                wake = WAKE_RE.search(body)
                if wake and int(wake[1]) == app_tid and state not in ("on_cpu", "runnable"):
                    transition(timestamp, "runnable", reason="wakeup", source_event=event,
                               source_line=line_number, cpu=int(match["cpu"]))
                continue
            parts = body.split("|", 2)
            if parts[0] == "B" and len(parts) == 3:
                name = re.sub(r"\|[IM]\d+$", "", parts[2])
                details = {"line": line_number, "timestamp_s": timestamp, "relative_s": timestamp - origin, "tid": tid, "name": name}
                jank = JANK_RE.search(name)
                if jank:
                    report = {**details, "kind": jank[1], "reported_ms": int(jank[2])}
                    for field in ("filterType", "vsyncTime"):
                        value = re.search(rf"{field}=(\d+)", name)
                        if value:
                            report[field] = int(value[1])
                    reports.append(report)
                rs_match = RS_RE.search(name)
                if rs_match:
                    values = rs_match.groups()
                    rs_reports.append({**details, **dict(zip(
                        ("max_frame_ms", "max_hitch_ms", "max_tech_ms", "max_real_ms", "refresh_hz"),
                        map(int, values[:5])
                    )), "scene": values[5]})
                ratio = RATIO_RE.search(name)
                if ratio:
                    ratios.append({**details, "hitch_ratio_ms_per_s": float(ratio[1]), "scene": ratio[2]})
                if tid == app_tid:
                    stack.append({"name": name, "line": line_number, "start_s": timestamp, "depth": len(stack), "parent_line": stack[-1]["line"] if stack else None})
            elif parts[0] == "E" and tid == app_tid:
                if not stack:
                    warnings["unmatched_main_end"] += 1
                    continue
                span = stack.pop()
                span.update(end_s=timestamp, end_line=line_number, duration_ms=(timestamp - span["start_s"]) * 1000)
                assert span["duration_ms"] >= 0
                frame_match = FRAME_RE.search(span["name"])
                if frame_match:
                    receiver = next((parent for parent in reversed(stack) if "ReceiveVsync " in parent["name"]), None)
                    vsync = VSYNC_RE.search(receiver["name"]) if receiver else None
                    frame = {**span, "vsync_ns": int(frame_match[1]), "vsync_id": int(frame_match[2]), "relative_s": span["start_s"] - origin}
                    if vsync:
                        frame.update(expected_end_s=int(vsync[2]) / 1e9, receiver_line=receiver["line"])
                        frame["budget_ms"] = (int(vsync[2]) - int(vsync[1])) / 1e6
                        frame["vsync_to_callback_ms"] = (span["start_s"] - int(vsync[1]) / 1e9) * 1000
                        frame["callback_end_after_deadline_ms"] = (timestamp - frame["expected_end_s"]) * 1000
                    frames.append(frame)
                spans.append(span)
    states.append({"start_s": state_start, "end_s": last_event, "state": state, **state_evidence})
    warnings["unclosed_main_begin"] = len(stack)
    if digest.hexdigest() != validation["sha256"]:
        raise ValueError(f"Trace hash mismatch: {trace}")
    for frame in frames:
        frame.update(state_totals(frame["start_s"], frame["end_s"], states))
    return {
        "trace": str(trace), "sha256": digest.hexdigest(), "origin_s": origin,
        "duration_s": last_event - origin, "warnings": dict(warnings), "frames": frames,
        "reports": reports, "rs_reports": rs_reports, "hitch_ratios": ratios,
        "states": states, "spans": spans, "scheduler_evidence_version": "sched_switch_reason/v1",
    }
