"""Validated JSON / raw ftrace adapters. Step envelopes never stand in for UI frames."""

import hashlib
import json
from collections import Counter
from pathlib import Path
import re

from backend.core import PlatformError
from .trace_parser import LINE_RE, FRAME_RE, parse_trace
from .extract import extract_frame
from .evidence import events_in, binder_chains, wait_ledger, Pmu


def error(message, **details):
    raise PlatformError("parallel_modeling_invalid_input", message, details=details)


def capture_step_metadata(manifest, step):
    """Return only naming metadata carried by the authenticated manifest.

    Older capture packages did not persist a plan title or an explicit ordinal.
    Keep those fields absent instead of inventing a plan name; the step number
    may still be recovered from the stable ``step_07`` / ``step-07`` identity.
    """
    plan_title = manifest.get("title")
    plan_title = plan_title.strip()[:300] if isinstance(plan_title, str) and plan_title.strip() else ""
    step_title = step.get("title")
    step_title = step_title.strip()[:300] if isinstance(step_title, str) and step_title.strip() else ""
    ordinal = step.get("ordinal")
    if not isinstance(ordinal, int) or isinstance(ordinal, bool) or ordinal < 1:
        match = re.fullmatch(r"step[_-]0*([1-9][0-9]*)", str(step.get("step_id") or ""), re.IGNORECASE)
        ordinal = int(match.group(1)) if match else None
    return {"plan_title": plan_title, "step_title": step_title, "step_ordinal": ordinal}


def capture_dataset_name(manifest, step):
    metadata = capture_step_metadata(manifest, step)
    step_title = metadata["step_title"] or str(step.get("step_id") or "采集负载")
    step_label = (f"Step {metadata['step_ordinal']}: {step_title}"
                  if metadata["step_ordinal"] is not None else step_title)
    return (f"[{metadata['plan_title']}] {step_label}"
            if metadata["plan_title"] else step_label)[:200]


def capture_inputs(payload, state_root):
    manifest = payload["capture_manifest"]
    if manifest.get("schema") != "scenario_capture_manifest/v1" and manifest.get("schema_version") != "scenario_capture_manifest/v1":
        error("Unsupported capture manifest schema")
    steps = [step for step in manifest.get("steps", []) if step.get("captured") and step.get("step_id") == payload.get("step_id")]
    if not steps and not payload.get("step_id"):
        steps = [step for step in manifest.get("steps", []) if step.get("captured")]
    if len(steps) != 1:
        error("Choose exactly one captured step", available_step_ids=[step.get("step_id") for step in manifest.get("steps", []) if step.get("captured")])
    step = steps[0]
    if step.get("trace_validation", {}).get("valid") is not True or step.get("collector_status", {}).get("trace") == "invalid":
        error("The selected capture step did not pass trace quality validation", step_id=step["step_id"])
    files = [entry for entry in manifest.get("files", []) if entry.get("step_id") == step["step_id"]]
    trace_files = [entry for entry in files if entry.get("kind") == "trace"]
    if len(trace_files) != 1:
        error("The selected step must contain exactly one managed ftrace file")

    def read(entry):
        relative = Path(entry.get("managed_path", ""))
        if relative.is_absolute() or ".." in relative.parts or not relative.parts or relative.parts[0] != "scenario-capture":
            error("Capture artifacts must use managed paths beneath scenario-capture")
        path = Path(state_root) / relative
        if not path.resolve().is_relative_to((Path(state_root) / "scenario-capture").resolve()) or not path.is_file() or path.is_symlink():
            error("Capture artifact is missing or not a regular managed file", name=entry.get("name"))
        if path.stat().st_size > 256 * 1024 * 1024:
            error("Capture artifact exceeds 256 MiB import limit")
        raw = path.read_bytes()
        if not entry.get("sha256") or hashlib.sha256(raw).hexdigest() != entry["sha256"]:
            error("Capture artifact hash verification failed", name=entry.get("name"))
        return raw.decode("utf-8", errors="replace")

    naming = capture_step_metadata(manifest, step)
    result = {**payload, "trace_text": read(trace_files[0]), "target_tid": payload.get("target_tid"),
              "capture_provenance": {"run_id": manifest.get("run_id"), "plan_id": manifest.get("plan_id"),
                                     "step_id": step["step_id"], **naming,
                                     "trace_sha256": trace_files[0]["sha256"],
                                     "quality": manifest.get("quality"), "step_validation": step.get("trace_validation")}}
    # A PID is useful as a preference but marker shell TIDs must never become the analysis target.
    result["preferred_tid"] = step.get("target_pid")
    for entry in files:
        if entry.get("kind") == "pmu":
            result["pmu_text"] = read(entry)
        elif entry.get("kind") == "pmu_metadata":
            result["pmu_metadata"] = json.loads(read(entry))
    return result


def raw_to_parsed(payload, directory):
    text = payload.get("trace_text")
    if not isinstance(text, str) or not text.strip():
        error("trace_text must contain ftrace records")
    if len(text.encode()) > 256 * 1024 * 1024:
        error("Trace exceeds 256 MiB import limit")
    trace = directory / "trace.ftrace"
    trace.write_text(text)
    timestamps, targets = [], Counter()
    for line in text.splitlines():
        match = LINE_RE.match(line)
        if not match:
            continue
        timestamps.append(float(match["ts"]))
        if match["event"] == "tracing_mark_write" and FRAME_RE.search(match["body"]):
            targets[int(match["pid"])] += 1
    if len(timestamps) < 2 or min(timestamps) >= max(timestamps):
        error("No usable timestamped ftrace records were found")
    target = payload.get("target_tid")
    if target is None:
        preferred = payload.get("preferred_tid")
        if preferred in targets:
            target = preferred
        elif len(targets) == 1:
            target = next(iter(targets))
        else:
            error("Choose the application UI thread TID", candidate_tids=sorted(targets))
    if not isinstance(target, int) or isinstance(target, bool) or target <= 0:
        error("target_tid must be a positive integer")
    digest = hashlib.sha256(trace.read_bytes()).hexdigest()
    (directory / "trace_validation.json").write_text(json.dumps({"first_event_s": min(timestamps), "last_event_s": max(timestamps), "sha256": digest}))
    parsed = parse_trace(trace, target)
    frames = [frame for frame in parsed["frames"] if any("CustomNode:BuildItem" in span["name"]
              and frame["start_s"] <= span["start_s"] < span["end_s"] <= frame["end_s"] for span in parsed["spans"])]
    vsync = payload.get("target_vsync_id")
    if vsync is None:
        if not frames:
            error("No complete UIVsyncTask containing BuildItem scopes was found; a capture step marker is not a UI callback")
        # Deterministic selection, exposed in provenance; explicit ID overrides it.
        chosen = max(frames, key=lambda frame: (frame["duration_ms"], -frame["line"]))
        vsync = chosen["vsync_id"]
    if not isinstance(vsync, int) or isinstance(vsync, bool):
        error("target_vsync_id must be an integer")
    frame = extract_frame(trace, target, vsync, {"preferred_max_ms": 5, "min_child_ms": .5})
    events = events_in(trace, round(min(timestamps) * 1e9), round(max(timestamps) * 1e9))
    chains, anomalies = binder_chains(events, target, frame["start_ns"], frame["end_ns"], parsed["states"])
    for chain in chains:
        scopes = [span for span in frame["spans"] if span["start_ns"] <= chain["send"]["ns"] < span["end_ns"]]
        chain["caller_scopes"] = sorted(scopes, key=lambda span: span["depth"]) or [{"name": "unknown"}]
    waits = wait_ledger(parsed, frame, events, chains)
    frame["trace"] = "dataset://trace.ftrace"
    supplied = []
    pmu_quality = {"status": "not_collected"}
    if payload.get("pmu_text") or payload.get("pmu_metadata"):
        if not payload.get("pmu_text") or not isinstance(payload.get("pmu_metadata"), dict):
            pmu_quality = {"status": "unusable", "reason": "PMU CSV and metadata are both required"}
        elif payload["pmu_metadata"].get("target_tid") != target:
            pmu_quality = {"status": "unusable", "reason": "PMU target TID differs from frame TID"}
        else:
            (directory / "pmu.csv").write_text(payload["pmu_text"])
            (directory / "pmu.json").write_text(json.dumps(payload["pmu_metadata"]))
            try:
                pmu = Pmu(directory, events, window=(frame["start_ns"], frame["end_ns"]))
                pmu_quality = pmu.quality
                builds = sorted((span for span in frame["spans"] if "CustomNode:BuildItem" in span["name"]), key=lambda span: span["start_ns"])
                supplied = [{"id": f"build_{index:02d}", "pmu": pmu.stage(span["start_ns"], span["end_ns"])} for index, span in enumerate(builds)]
            except (ValueError, KeyError, TypeError, ZeroDivisionError) as exc:
                pmu_quality = {"status": "unusable", "reason": str(exc)}
    return {"frame": frame, "binder_chains": {"chains": chains, "anomalies": anomalies},
            "wait_ledger": waits, "component_features": supplied, "pmu_quality": pmu_quality,
            "selection": {"target_tid": target, "target_vsync_id": vsync,
                          "rule": "explicit_vsync" if payload.get("target_vsync_id") is not None else "longest_complete_callback_with_BuildItem",
                          "candidate_vsync_ids": [row["vsync_id"] for row in frames], "trace_sha256": digest}}
