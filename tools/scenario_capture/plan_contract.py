"""Shared, stdlib-only scenario plan validation for Web and desktop runners.

This module builds the plan payload only. Each host owns persistence, device
selection and execution, while both consume the same selectors/readiness rules.
"""
from __future__ import annotations

import json
import re
from typing import Any

from backend.core import PlatformError
from tools.scenario_capture.gallery import STEPS, point_from_ratio

OPERATIONS = {"launch", "click", "date_mode", "zoom", "albums", "scroll", "home", "key", "wait", "swipe", "unresolved"}


def _error(message: str) -> None:
    raise PlatformError("SCENARIO_PLAN_INVALID", message, status_code=422)


def _merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(base))
    for key, value in override.items():
        result[key] = _merge(result[key], value) if isinstance(result.get(key), dict) and isinstance(value, dict) else value
    return result


def _number(value: Any, name: str, low: float, high: float, *, integer: bool = False) -> int | float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not low <= value <= high or (integer and int(value) != value):
        _error(f"{name} must be {'an integer' if integer else 'a number'} between {low} and {high}")
    return int(value) if integer else value


def _selector(value: Any, name: str) -> None:
    if value is None:
        return
    if not isinstance(value, dict) or not value:
        _error(f"{name} must be a nonempty selector or null")
    if "point" in value:
        if set(value) != {"point"}:
            _error(f"{name}.point must be a standalone calibrated coordinate")
        point = value["point"]
        if not isinstance(point, list) or len(point) != 2:
            _error(f"{name}.point must contain x/y ratios")
        for coordinate in point:
            _number(coordinate, name + ".point", .00001, .99999)
        return
    if "alternatives" in value:
        if set(value) != {"alternatives"} or not isinstance(value["alternatives"], list) or not 1 <= len(value["alternatives"]) <= 12:
            _error(f"{name}.alternatives must contain 1–12 selectors")
        for item in value["alternatives"]:
            _selector(item, name)
        return
    allowed = {"text", "id", "description", "type", "text_regex", "id_regex", "description_regex", "type_regex", "index", "region", "clickable", "enabled"}
    if set(value) - allowed or not (set(value) - {"index", "region"}):
        _error(f"{name} needs text/id/description/type or a matching attribute")
    for key, item in value.items():
        if key == "index":
            _number(item, name + ".index", 0, 10000, integer=True)
        elif key == "region":
            if not isinstance(item, list) or len(item) != 4:
                _error(f"{name}.region must contain four ratios")
            for coordinate in item:
                _number(coordinate, name + ".region", 0, 1)
            if item[0] >= item[2] or item[1] >= item[3]:
                _error(f"{name}.region must have positive width and height")
        elif not isinstance(item, (str, bool)) or len(str(item)) > 256:
            _error(f"{name}.{key} is invalid")
        elif key.endswith("_regex"):
            try:
                re.compile(str(item))
            except re.error:
                _error(f"{name}.{key} is not a valid regex")


def _validate_profile(profile: dict[str, Any]) -> None:
    for name in ("bundle", "ability"):
        if not isinstance(profile.get(name), str) or not re.fullmatch(r"[A-Za-z0-9_.]{1,200}", profile[name]):
            _error(f"Invalid profile.{name}")
    if profile.get("screen_size_mode", "dynamic") not in ("dynamic", "fixed"):
        _error("profile.screen_size_mode must be dynamic or fixed")
    size = profile.get("screen_size")
    if not isinstance(size, list) or len(size) != 2:
        _error("profile.screen_size must contain width and height")
    for value in size:
        _number(value, "screen_size", 100, 16000, integer=True)
    for name in ("hold_seconds", "guard_seconds", "prepare_settle_seconds"):
        _number(profile.get(name), name, 0, 10)
    _number(profile.get("trace_buffer_kb"), "trace_buffer_kb", 1024, 1048576, integer=True)
    _number(profile.get("step_timeout_seconds"), "step_timeout_seconds", 10, 300, integer=True)
    categories = profile.get("trace_categories")
    if not isinstance(categories, list) or not categories or len(categories) > 64 or any(not isinstance(c, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", c) for c in categories):
        _error("trace_categories must be category names")
    targets = profile.get("targets")
    if not isinstance(targets, dict) or len(targets) > 150:
        _error("profile.targets must be a selector mapping")
    for name, selector in targets.items():
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,63}", name):
            _error("Invalid selector name")
        _selector(selector, "targets." + name)
    zoom, scroll, nav = profile.get("zoom"), profile.get("scroll"), profile.get("navigation")
    if not all(isinstance(item, dict) for item in (zoom, scroll, nav)):
        _error("zoom, scroll and navigation profiles are required")
    for value in (zoom.get("center"), scroll.get("upper"), scroll.get("lower")):
        try:
            point_from_ratio(value, size)
        except (TypeError, ValueError):
            _error("Invalid gesture coordinates")
    inner = _number(zoom.get("inner_half_width"), "inner_half_width", .001, .49)
    outer = _number(zoom.get("outer_half_width"), "outer_half_width", .001, .49)
    if not inner < outer or not outer < zoom["center"][0] < 1 - outer:
        _error("Pinch coordinates exceed the screen or have reversed widths")
    _number(zoom.get("rounds"), "zoom.rounds", 1, 5, integer=True)
    _number(zoom.get("sweeps_per_extreme"), "zoom.sweeps_per_extreme", 1, 10, integer=True)
    _number(zoom.get("duration_ms"), "zoom.duration_ms", 100, 3000, integer=True)
    for obj, name in ((zoom, "gesture_gap_seconds"), (scroll, "gesture_gap_seconds"), (nav, "back_gap_seconds")):
        _number(obj.get(name), name, 0, 5)
    _number(scroll.get("velocity"), "scroll.velocity", 200, 40000, integer=True)
    if scroll.get("step15_mode") not in {"up3_down3", "up3_then_roundtrip3"}:
        _error("Invalid scroll mode")
    if sorted(nav.get("date_mode_cycle", [])) != sorted(["日", "月", "年"]):
        _error("date_mode_cycle must include 日/月/年 exactly once")
    for name in ("step11_back_count", "step16_back_count"):
        _number(nav.get(name), name, 0, 4, integer=True)


def gallery_steps() -> list[dict[str, Any]]:
    return [{"step_id": f"step_{number:02d}", "ordinal": number, "slug": slug, "title": title,
             "operation": operation, "target": target, "expected_targets": expected, "template_step": number}
            for number, slug, title, operation, target, expected in STEPS]


def create_plan_payload(payload: dict[str, Any], catalog: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        _error("Plan must be an object")
    template = payload.get("template_id", "gallery_17_steps_v1")
    if template not in {"gallery_17_steps_v1", "custom"}:
        _error("Unknown scenario template")
    raw_steps = payload.get("steps")
    if raw_steps is None:
        raw_steps = gallery_steps() if template != "custom" else []
    if not isinstance(raw_steps, list) or not 1 <= len(raw_steps) <= 100:
        _error("Plan needs 1–100 structured steps")
    override = payload.get("profile", {})
    if not isinstance(override, dict):
        _error("profile must be an object")
    profile = _merge(catalog["default_profile"], override)
    steps: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_steps, 1):
        if isinstance(item, str):
            item = {"title": item, "operation": "unresolved"}
        if not isinstance(item, dict):
            _error("Each step must be an object or natural-language instruction")
        step_id = str(item.get("step_id") or f"step_{index:02d}")
        slug = str(item.get("slug") or f"action_{index:02d}")
        title = str(item.get("title") or item.get("instruction") or "")[:300]
        operation = item.get("operation", "unresolved")
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_-]{0,63}", step_id) or step_id in seen:
            _error("Step IDs must be unique identifiers")
        if not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,63}", slug) or not title or operation not in OPERATIONS:
            _error("Invalid step slug, title or operation")
        if any(key in item for key in ("command", "shell", "python", "code", "script")):
            _error("Use structured operations and selectors, not executable command text")
        seen.add(step_id)
        target = item.get("target")
        if target is not None and (not isinstance(target, str) or not re.fullmatch(r"[a-zA-Z][a-zA-Z0-9_]{0,63}", target)):
            _error("target must be a selector name")
        if "selector" in item:
            target = "target_" + step_id.replace("-", "_")
            profile["targets"][target] = item["selector"]
        expected = item.get("expected_targets", [])
        if not isinstance(expected, list) or len(expected) > 20 or any(not isinstance(t, str) for t in expected):
            _error("expected_targets must contain selector names")
        step = {"step_id": step_id, "ordinal": index, "slug": slug, "title": title, "operation": operation,
                "target": target, "expected_targets": expected}
        if template == "gallery_17_steps_v1" and item.get("template_step") in range(1, 18):
            step["template_step"] = item["template_step"]
        if operation == "click" and not isinstance(target, str):
            _error("Click needs a target name or selector")
        if operation == "date_mode" and target not in {"year", "day"}:
            _error("date_mode target must be year or day")
        if operation == "albums" and index not in {11, 16}:
            _error("Gallery albums operation requires template position 11 or 16; use key + click for a custom workflow")
        if operation == "key":
            if item.get("key") not in {"Home", "Back", "Enter", "Power"}:
                _error("Unsupported key")
            step["key"] = item["key"]
        if operation == "wait":
            step["seconds"] = _number(item.get("seconds", 1), "wait.seconds", 0, 10)
        if operation == "swipe":
            points = item.get("points")
            if not isinstance(points, list) or len(points) != 4:
                _error("Swipe points must contain start/end x/y ratios")
            for point in points:
                _number(point, "swipe.points", .00001, .99999)
            step.update(points=points, velocity=_number(item.get("velocity", 1000), "swipe.velocity", 200, 40000, integer=True))
        steps.append(step)
    _validate_profile(profile)
    capture_override = payload.get("capture", {})
    if not isinstance(capture_override, dict):
        _error("capture must be an object")
    capture = _merge(catalog["default_capture"], capture_override)
    if "selected_step_ids" in payload:
        capture["selected_step_ids"] = payload["selected_step_ids"]
    if template == "custom" and "selected_step_ids" not in capture_override and "selected_step_ids" not in payload:
        capture["selected_step_ids"] = [steps[-1]["step_id"]]
    selected = capture.get("selected_step_ids")
    if not isinstance(selected, list) or not selected or any(not isinstance(value, str) or value not in seen for value in selected):
        _error("Select one or more existing step IDs for capture")
    capture["selected_step_ids"] = list(dict.fromkeys(selected))
    if capture.get("preparation_mode") not in {"auto", "already_prepared"}:
        _error("preparation_mode must be auto or already_prepared")
    collectors = capture.get("collectors")
    if not isinstance(collectors, list) or not collectors or any(not isinstance(c, str) or c not in {"trace", "hilog", "pmu"} for c in collectors):
        _error("collectors must contain trace, hilog and/or pmu")
    capture["collectors"] = list(dict.fromkeys(collectors))
    pmu = capture.get("pmu")
    if not isinstance(pmu, dict) or not isinstance(pmu.get("binary_path"), str) or not re.fullmatch(r"/data/local/tmp/[A-Za-z0-9_./-]{1,180}", pmu["binary_path"]) or ".." in pmu["binary_path"].split("/"):
        _error("PMU binary must be a device /data/local/tmp path")
    if pmu.get("group") not in {"auto", "all", "cache", "branch", "stall", "l1d", "llc", "dtlb"}:
        _error("Unsupported PMU event group")
    _number(pmu.get("period_us"), "pmu.period_us", 10, 10000, integer=True)
    _number(pmu.get("duration_ms"), "pmu.duration_ms", 100, 10000, integer=True)
    _number(pmu.get("reader_cpu"), "pmu.reader_cpu", 0, 255, integer=True)
    if payload.get("execution_policy", "through_last_selected") != "through_last_selected":
        _error("First version executes through the last selected step")
    last = max(i for i, step in enumerate(steps) if step["step_id"] in capture["selected_step_ids"])
    first = min(i for i, step in enumerate(steps) if step["step_id"] in capture["selected_step_ids"]) if capture["preparation_mode"] == "already_prepared" else 0
    execution_steps = steps[first:last + 1]
    blockers = []
    for step in execution_steps:
        if step["operation"] == "unresolved":
            blockers.append({"step_id": step["step_id"], "reason": "instruction_needs_structured_operation"})
        required = set(step["expected_targets"])
        if step.get("target"):
            required.add(step["target"])
        if step["operation"] == "date_mode":
            required.add("date_mode_button")
        if step.get("template_step") == 10:
            required.add("save_menu")
        for name in required:
            if not profile["targets"].get(name):
                blockers.append({"step_id": step["step_id"], "reason": "selector_missing", "target": name})
    plan = {"title": str(payload.get("title") or "图库选定步骤采集")[:300], "template_id": template,
        "natural_language_steps": str(payload.get("natural_language_steps") or "")[:30000], "steps": steps,
        "profile": profile, "capture": capture, "execution_policy": "through_last_selected",
        "execution_step_ids": [step["step_id"] for step in execution_steps], "preparation_step_ids": [step["step_id"] for step in execution_steps if step["step_id"] not in capture["selected_step_ids"]],
        "readiness": {"status": "needs_input" if blockers else "ready_for_device_preflight", "blockers": blockers},
        "source_connector_id": str(payload.get("source_connector_id") or "unbound"),
        "source_snapshot_id": str(payload.get("source_snapshot_id") or ""), "provenance": catalog["provenance"]}
    return plan
