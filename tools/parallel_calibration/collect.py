#!/usr/bin/env python3
"""Run an offline calibration-plan batch on the machine being measured.

No HDC/ADB, frequency writes, device preparation, synthetic rows or automatic
model publication. Each sample is a fresh benchmark process / independent run.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = "parallel_calibration_measurements/v1"
DEFAULT_CPUS = {"big": [10], "middle": [4, 6, 8], "small": [0, 1, 2, 3]}
KINDS = tuple(DEFAULT_CPUS)
FREQUENCY_POLICY_ID = re.compile(r"(?:policy|cpu)\d{1,4}\Z")
SUPPORTED_WORKLOAD_VERSIONS = ("parallel-calibration-proxy-v6",)
PAIRED_DISPATCH_WORKLOAD_VERSIONS = frozenset((
    "parallel-calibration-proxy-v4", "parallel-calibration-proxy-v5", "parallel-calibration-proxy-v6",
))
TARGET_SIBLING_BUSY_THRESHOLD = .05
FREQUENCY_TOLERANCE = .03
WARMUP_FREQUENCY_REQUIRED_CONSECUTIVE = 3
WARMUP_FREQUENCY_MAX_SAMPLES = 12
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


def utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def read_json(path):
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"Expected a JSON object in {path}")
    return value


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _integer(value, label, minimum=0, maximum=2 ** 63 - 1):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"Benchmark emitted invalid {label}")
    return value


def _number(value, label, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
        raise ValueError(f"Benchmark emitted invalid {label}")
    return value


def _frequency_source_path(source, cpu, policy_id):
    if source == "cpuinfo_cur_freq":
        return f"/sys/devices/system/cpu/cpu{cpu}/cpufreq/cpuinfo_cur_freq"
    if source != "scaling_cur_freq_fallback" or not isinstance(policy_id, str) or not FREQUENCY_POLICY_ID.fullmatch(policy_id):
        return None
    return (f"/sys/devices/system/cpu/cpufreq/{policy_id}/scaling_cur_freq"
            if policy_id.startswith("policy") else
            f"/sys/devices/system/cpu/{policy_id}/cpufreq/scaling_cur_freq")


def _validate_frequency_role(measurement, *, prefix, cpu, policy_id, requested_khz, require_warmup=False):
    """Validate one immutable per-CPU source; fallback remains diagnostic only."""
    source = measurement.get(prefix + "frequency_source")
    source_cpu = _integer(measurement.get(prefix + "frequency_source_cpu"),
                          prefix + "frequency_source_cpu", 0, 4095)
    expected_path = _frequency_source_path(source, cpu, policy_id)
    if source_cpu != cpu or expected_path is None or measurement.get(prefix + "frequency_source_path") != expected_path:
        raise ValueError("Benchmark frequency source is not frozen to the measured logical CPU")
    if source != "cpuinfo_cur_freq":
        raise ValueError("Benchmark only has scaling_cur_freq fallback evidence; production collection requires per-CPU cpuinfo_cur_freq")
    samples = _integer(measurement.get(prefix + "frequency_sample_count"),
                       prefix + "frequency_sample_count", 0)
    failures = _integer(measurement.get(prefix + "frequency_read_failures"),
                        prefix + "frequency_read_failures", 0)
    mean_field = "peer_observed_frequency_khz" if prefix else "frequency_mean_khz"
    mean = _number(measurement.get(mean_field), mean_field, 1)
    minimum = _number(measurement.get(prefix + "frequency_min_khz"),
                      prefix + "frequency_min_khz", 1)
    maximum = _number(measurement.get(prefix + "frequency_max_khz"),
                      prefix + "frequency_max_khz", 1)
    if minimum > mean or mean > maximum:
        raise ValueError("Benchmark frequency mean is outside its sampled interval")
    low, high = requested_khz * (1 - FREQUENCY_TOLERANCE), requested_khz * (1 + FREQUENCY_TOLERANCE)
    if samples < 5 or failures or minimum < low or maximum > high:
        raise ValueError("Benchmark actual per-CPU frequency window is not verified within +/-3%")
    if require_warmup:
        count = _integer(measurement.get("warmup_frequency_sample_count"),
                         "warmup_frequency_sample_count", 1, WARMUP_FREQUENCY_MAX_SAMPLES)
        consecutive = _integer(measurement.get("warmup_frequency_in_range_consecutive"),
                               "warmup_frequency_in_range_consecutive", 0,
                               WARMUP_FREQUENCY_REQUIRED_CONSECUTIVE)
        if (measurement.get("warmup_frequency_required_consecutive") != WARMUP_FREQUENCY_REQUIRED_CONSECUTIVE
                or measurement.get("warmup_frequency_max_samples") != WARMUP_FREQUENCY_MAX_SAMPLES
                or consecutive > count):
            raise ValueError("Benchmark warmup residency contract is inconsistent")
        warmup_minimum = _number(measurement.get("warmup_frequency_min_khz"),
                                 "warmup_frequency_min_khz", 1)
        warmup_maximum = _number(measurement.get("warmup_frequency_max_khz"),
                                 "warmup_frequency_max_khz", 1)
        warmup_last = _number(measurement.get("warmup_frequency_last_khz"),
                              "warmup_frequency_last_khz", 1)
        if not warmup_minimum <= warmup_last <= warmup_maximum:
            raise ValueError("Benchmark warmup frequency interval is inconsistent")
        if (consecutive < WARMUP_FREQUENCY_REQUIRED_CONSECUTIVE
                or not low <= warmup_last <= high
                or measurement.get("warmup_frequency_verified") is not True):
            raise ValueError("Benchmark target CPU did not reach bounded warmup residency")
        chunk = 32 if measurement.get("benchmark") == "empty_dispatch" else min(measurement.get("work_units", 0), 32768)
        if _integer(measurement.get("warmup_work_units"), "warmup_work_units", 1) != count * chunk:
            raise ValueError("Benchmark warmup work does not match its frequency samples")
    if measurement.get(prefix + "frequency_verified") is not True:
        raise ValueError("Benchmark reported unverified per-CPU frequency evidence")


def validate_measurement_contract(measurement):
    if (measurement.get("schema_version") != "parallel_calibration_measurement/v1"
            or measurement.get("workload_version") not in SUPPORTED_WORKLOAD_VERSIONS):
        raise ValueError("Benchmark emitted an unsupported measurement version")
    target = measurement.get("cpu")
    siblings = measurement.get("target_sibling_cpus")
    activity = measurement.get("target_sibling_activity")
    if (type(target) is not int or not 0 <= target < 4096
            or not isinstance(siblings, list) or len(siblings) > 64
            or any(type(cpu) is not int or not 0 <= cpu < 4096 or cpu == target for cpu in siblings)
            or len(siblings) != len(set(siblings))
            or not isinstance(activity, list) or len(activity) != len(siblings)):
        raise ValueError("Benchmark emitted invalid target sibling topology evidence")
    fractions = []
    for cpu, row in zip(siblings, activity):
        if (not isinstance(row, dict) or row.get("cpu") != cpu or row.get("verified") is not True
                or type(row.get("busy_ticks")) is not int or row["busy_ticks"] < 0
                or type(row.get("total_ticks")) is not int or row["total_ticks"] <= 0
                or row["busy_ticks"] > row["total_ticks"]):
            raise ValueError("Benchmark did not verify every target sibling activity counter")
        fraction = row.get("busy_fraction")
        expected = row["busy_ticks"] / row["total_ticks"]
        if (isinstance(fraction, bool) or not isinstance(fraction, (int, float))
                or not math.isfinite(fraction) or abs(fraction - expected) > 1e-8):
            raise ValueError("Benchmark target sibling fraction disagrees with counter deltas")
        fractions.append(fraction)
    maximum = max(fractions, default=0)
    if (measurement.get("target_sibling_activity_verified") is not True
            or measurement.get("target_sibling_quietness_verified") is not True
            or measurement.get("target_sibling_busy_threshold") != TARGET_SIBLING_BUSY_THRESHOLD
            or isinstance(measurement.get("target_sibling_busy_fraction"), bool)
            or not isinstance(measurement.get("target_sibling_busy_fraction"), (int, float))
            or not math.isfinite(measurement["target_sibling_busy_fraction"])
            or abs(measurement["target_sibling_busy_fraction"] - maximum) > 1e-8
            or maximum > TARGET_SIBLING_BUSY_THRESHOLD):
        raise ValueError("Benchmark target physical-core sibling activity is not quiet and verified")
    _validate_frequency_role(
        measurement, prefix="", cpu=target,
        policy_id=measurement.get("frequency_policy_id"),
        requested_khz=_integer(measurement.get("frequency_khz"), "frequency_khz", 1, 10_000_000),
        require_warmup=True,
    )
    if measurement.get("benchmark") == "pair_work" and measurement.get("active_tasks") == 2:
        helpers = measurement.get("helpers")
        if (not isinstance(helpers, list) or len(helpers) != 1 or not isinstance(helpers[0], dict)
                or helpers[0].get("affinity_verified") is not True
                or helpers[0].get("actual_cpu") != helpers[0].get("cpu")):
            raise ValueError("Benchmark did not verify the actual helper logical CPU")
        _validate_frequency_role(
            measurement, prefix="peer_",
            cpu=_integer(helpers[0].get("cpu"), "helpers[0].cpu", 0, 4095),
            policy_id=measurement.get("peer_frequency_policy_id"),
            requested_khz=_integer(measurement.get("peer_frequency_khz"),
                                   "peer_frequency_khz", 1, 10_000_000),
        )
    if (measurement.get("benchmark") != "empty_dispatch"
            or measurement.get("workload_version") not in PAIRED_DISPATCH_WORKLOAD_VERSIONS):
        return
    if (measurement.get("dispatch_measurement_schema") != "paired_dispatch_delta/v1"
            or measurement.get("dispatch_payload") != "mix64_v1"
            or measurement.get("dispatch_payload_match") is not True
            or measurement.get("cold_start_included") is not False):
        raise ValueError("Worker dispatch measurement lacks the paired same-payload contract")
    for field in PAIRED_DISPATCH_TOTAL_FIELDS:
        value = measurement.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0:
            raise ValueError(f"Worker dispatch measurement has invalid {field}")
    for field in PAIRED_DISPATCH_DELTA_FIELDS:
        value = measurement.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError(f"Worker dispatch measurement has invalid {field}")
    count = measurement.get("work_units")
    if isinstance(count, bool) or not isinstance(count, int) or count < 1:
        raise ValueError("Worker dispatch measurement has invalid task count")
    expected = {
        "dispatch_wall_delta_ms_per_task": (
            measurement["queue_dispatch_roundtrip_wall_ms"] - measurement["direct_batch_roundtrip_wall_ms"]) / count,
        "dispatch_worker_cpu_delta_ms_per_task": (
            measurement["queue_dispatch_worker_cpu_ms"] - measurement["direct_batch_worker_cpu_ms"]) / count,
        "dispatch_producer_cpu_delta_ms_per_task": (
            measurement["queue_dispatch_producer_cpu_ms"] - measurement["direct_batch_producer_cpu_ms"]) / count,
    }
    for field, value in expected.items():
        if abs(measurement[field] - value) > max(1e-9, abs(value) * 1e-6):
            raise ValueError(f"Worker dispatch measurement {field} disagrees with paired totals")


def cpu_map_value(value, plan):
    mapping = read_json(value) if value and Path(value).is_file() else json.loads(value) if value else plan.get("cpu_map", DEFAULT_CPUS)
    required = tuple(plan.get("core_kinds") or KINDS)
    if not isinstance(mapping, dict) or any(kind not in mapping for kind in required):
        raise ValueError("CPU map must contain every core group frozen by the plan")
    kinds = tuple(mapping)
    cpus = []
    for kind in kinds:
        if not isinstance(mapping[kind], list) or not mapping[kind] or any(type(cpu) is not int or not 0 <= cpu < 4096 for cpu in mapping[kind]):
            raise ValueError(f"Invalid CPU list for {kind}")
        cpus.extend(mapping[kind])
    if len(cpus) != len(set(cpus)):
        raise ValueError("CPU map must choose one distinct logical representative per physical core")
    return mapping


def frequency_policy_map(plan, cpu_map):
    """Freeze frequency sampling to the same hardware domain as the plan.

    Older plans remain usable through the target CPU's per-CPU cpufreq view.
    New plans carry source_policy_id from hardware extraction so policy-only
    devices do not silently sample a different or nonexistent domain.
    """
    frozen = plan.get("frequency_policy_ids", {})
    if frozen is None:
        frozen = {}
    if not isinstance(frozen, dict):
        raise ValueError("frequency_policy_ids must be an object")
    result = {}
    for kind, cpus in cpu_map.items():
        value = frozen.get(kind, f"cpu{cpus[0]}")
        if not isinstance(value, str) or not FREQUENCY_POLICY_ID.fullmatch(value):
            raise ValueError(f"Invalid frequency policy id for {kind}")
        result[kind] = value
    if any(kind not in cpu_map for kind in frozen):
        raise ValueError("frequency_policy_ids contains a core group outside cpu_map")
    return result


def placements(condition, cpu_map, observer_cpu=None):
    kind = condition["core_kind"]
    target = cpu_map[kind][0]
    others = [cpu for cpu in cpu_map[kind][1:]]
    others.extend(cpu for group in cpu_map if group != kind for cpu in cpu_map[group])
    count = condition["active_tasks"] - 1
    if count < 0 or count > len(others):
        raise ValueError("Not enough independent cores for target/helpers")
    helpers = others[:count]
    if condition["benchmark"] == "pair_work" and count:
        candidates = [cpu for cpu in cpu_map[condition["peer_core_kind"]] if cpu != target]
        if not candidates: raise ValueError("Pair condition requires an independent physical representative in the requested peer core group")
        helpers = [candidates[0]]
    observer = observer_cpu if observer_cpu is not None else next((cpu for cpu in others if cpu not in helpers), None)
    if observer is None: raise ValueError("No independent observer remains; provide --observer-cpu for an additional physical core")
    if observer in [target, *helpers]:
        raise ValueError("Observer CPU must differ from target and helper CPUs")
    return target, helpers, observer


def benchmark_command(binary, condition, cpu_map, options):
    target, helpers, observer = placements(condition, cpu_map, options.observer_cpu)
    policies = getattr(options, "frequency_policy_ids", {}) or {}
    target_policy = policies.get(condition["core_kind"], f"cpu{target}")
    if not FREQUENCY_POLICY_ID.fullmatch(target_policy):
        raise ValueError("Invalid target frequency policy id")
    command = [str(binary), "--benchmark", condition["benchmark"], "--family", condition["family"],
               "--cpu", str(target), "--observer-cpu", str(observer), "--frequency-khz", str(condition["frequency_khz"]),
               "--frequency-policy-id", target_policy,
               "--work-units", str(condition["work_units"]), "--active-tasks", str(condition["active_tasks"]),
               "--sample-period-us", str(options.sample_period_us), "--frequency-tolerance", str(options.frequency_tolerance)]
    if helpers:
        command.extend(["--helper-cpus", ",".join(str(cpu) for cpu in helpers)])
    if condition["benchmark"] == "pair_work":
        for field in ("workload_class", "peer_workload_class", "peer_frequency_khz", "working_set_bytes"):
            command.extend(["--" + field.replace("_", "-"), str(condition[field])])
        if condition["active_tasks"] == 2:
            peer_policy = policies.get(condition["peer_core_kind"], f"cpu{helpers[0]}")
            if not FREQUENCY_POLICY_ID.fullmatch(peer_policy):
                raise ValueError("Invalid peer frequency policy id")
            command.extend(["--peer-frequency-policy-id", peer_policy])
    if condition["benchmark"] == "roofline":
        for field in ("workload_class", "working_set_bytes", "roofline_operations_per_element"):
            command.extend(["--" + field.replace("_", "-"), str(condition[field])])
    return command


def fixed_observer_cpu(plan, cpu_map, requested=None):
    """Keep observation placement identical for solo/co-run and every batch."""
    used = set()
    for condition in plan["conditions"]:
        target, helpers, _ = placements(condition, cpu_map, requested)
        used.update([target, *helpers])
    candidates = [cpu for kind in cpu_map for cpu in cpu_map[kind] if cpu not in used]
    if requested is not None:
        if requested in used: raise ValueError("Fixed observer CPU overlaps a target/helper in the frozen plan")
        return requested
    if not candidates:
        raise ValueError("No independent observer remains across this plan; provide --observer-cpu for an additional physical core")
    return candidates[0]


def thermal_zones():
    result = []
    for directory in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        try:
            result.append({"zone": directory.name, "type": (directory / "type").read_text().strip(),
                           "temperature_raw": int((directory / "temp").read_text().strip())})
        except (OSError, ValueError):
            continue
    return result


def quality_reasons(sample):
    reasons = []
    for field in ("affinity_verified", "physical_cores_verified", "observer_independent_core", "frequency_verified"):
        if sample.get(field) is not True:
            reasons.append(field + " is not verified")
    if sample.get("active_tasks", 1) > 1 and sample.get("concurrency_verified") is not True:
        reasons.append("target/helpers did not verify >=90% CPU occupancy throughout the target interval")
    if sample.get("benchmark") == "pair_work" and sample.get("active_tasks") == 2 and sample.get("peer_frequency_verified") is not True:
        reasons.append("peer frequency is not verified throughout the co-run interval")
    if sample.get("thermal_throttled") is not False:
        reasons.append("thermal status is unknown or throttled")
    cpu, wall = sample.get("cpu_ms"), sample.get("wall_ms")
    if not isinstance(cpu, (int, float)) or not math.isfinite(cpu) or cpu < 1:
        reasons.append("target thread CPU time is below the 1 ms quality threshold")
    if not isinstance(wall, (int, float)) or not math.isfinite(wall) or wall <= 0 or isinstance(cpu, (int, float)) and cpu > wall * 1.02:
        reasons.append("invalid thread CPU / wall interval")
    if sample.get("frequency_sample_count", 0) < 5:
        reasons.append("fewer than 5 valid frequency samples")
    return reasons


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", required=True, type=Path)
    parser.add_argument("--binary", type=Path, default=Path("./benchmark"))
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--merge", action="append", default=[], type=Path, help="Merge compatible prior batches; repeat for multiple files")
    parser.add_argument("--merge-only", action="store_true", help="Combine prior measured batches without executing a benchmark")
    parser.add_argument("--core-kind", action="append")
    parser.add_argument("--frequency-khz", type=int, action="append")
    parser.add_argument("--benchmark", choices=("cpu_work", "empty_dispatch", "pair_work", "roofline"))
    parser.add_argument("--family", choices=("build", "residual", "roofline"))
    parser.add_argument("--split", choices=("train", "validation"))
    parser.add_argument("--condition-id", action="append")
    parser.add_argument("--peer-core-kind")
    parser.add_argument("--peer-frequency-khz", type=int)
    parser.add_argument("--cpu-map", help="JSON text or JSON file; each entry is one representative per physical core")
    parser.add_argument("--observer-cpu", type=int)
    parser.add_argument("--sample-period-us", type=int, default=1000)
    parser.add_argument("--frequency-tolerance", type=float, default=.03)
    parser.add_argument("--timeout-seconds", type=float, default=300)
    parser.add_argument("--cooldown-seconds", type=float, default=1)
    parser.add_argument("--device-id", help="Measured device identifier, or inherit from --merge")
    parser.add_argument("--os-build", help="Actual OS / firmware build, or inherit from --merge")
    parser.add_argument("--build-info", default="not_recorded", help="Compiler, flags and target ABI used for this binary")
    parser.add_argument("--thermal-state", choices=("normal", "throttled", "unknown"), default="unknown",
                        help="Operator-observed state for this batch. Unknown cannot pass the fit quality gate.")
    parser.add_argument("--thermal-note", default="", help="How thermal state was monitored; temperature snapshots are also retained")
    parser.add_argument("--dry-run", action="store_true", help="Write commands only, never a measured sample")
    return parser.parse_args(argv)


def main(argv=None):
    options = parse_args(argv)
    try:
        plan = read_json(options.plan)
        if plan.get("schema_version") != "parallel_calibration_plan/v1" or not plan.get("id") or not isinstance(plan.get("conditions"), list):
            raise ValueError("Expected a frozen parallel_calibration_plan/v1 plan with id and conditions")
        if not 50 <= options.sample_period_us <= 1000000 or not 0 <= options.frequency_tolerance <= .05:
            raise ValueError("Sample period must be 50–1000000 us; frequency tolerance must be 0–0.05")
        if not math.isfinite(options.timeout_seconds) or options.timeout_seconds <= 0 or not math.isfinite(options.cooldown_seconds) or options.cooldown_seconds < 0:
            raise ValueError("Invalid timeout or cooldown")
        if options.thermal_state == "normal" and not options.thermal_note.strip():
            raise ValueError("--thermal-state normal requires --thermal-note describing the actual observation")
        cpu_map = cpu_map_value(options.cpu_map, plan)
        options.frequency_policy_ids = frequency_policy_map(plan, cpu_map)
        if options.core_kind and any(kind not in cpu_map for kind in options.core_kind):
            raise ValueError("--core-kind must be an actual core group in the frozen plan")
        if options.peer_core_kind and options.peer_core_kind not in cpu_map:
            raise ValueError("--peer-core-kind must be an actual core group in the frozen plan")
        options.observer_cpu = fixed_observer_cpu(plan, cpu_map, options.observer_cpu)
        conditions = [condition for condition in plan["conditions"]
                      if (not options.core_kind or condition["core_kind"] in options.core_kind)
                      and (not options.frequency_khz or condition["frequency_khz"] in options.frequency_khz)
                      and (not options.benchmark or condition["benchmark"] == options.benchmark)
                      and (not options.family or condition["family"] == options.family)
                      and (not options.split or condition["split"] == options.split)
                      and (not options.peer_core_kind or condition.get("peer_core_kind") in ("none", options.peer_core_kind))
                      and (not options.peer_frequency_khz or condition.get("peer_frequency_khz") in (0, options.peer_frequency_khz))
                      and (not options.condition_id or condition["id"] in options.condition_id)]
        if not options.merge_only and not conditions:
            raise ValueError("No plan conditions match the selected filters")
        if not options.merge_only and not options.dry_run and len({row["frequency_khz"] for row in conditions}) > 1:
            raise ValueError("Choose one externally controlled frequency batch with --core-kind and --frequency-khz; the collector never changes OPP")
        if not options.merge_only and not options.dry_run and len({row.get("peer_frequency_khz") for row in conditions if row.get("peer_frequency_khz")}) > 1:
            raise ValueError("Choose one peer frequency batch with --peer-core-kind and --peer-frequency-khz")
        binary = options.binary.resolve()
        commands = [{"condition_id": row["id"], "split": row["split"], "repeats": row["repeats"],
                     "command": benchmark_command(binary, row, cpu_map, options)} for row in conditions]
        if options.dry_run:
            atomic_json(options.output, {"schema_version": "parallel_calibration_commands/v1", "plan_id": plan["id"],
                                        "source_kind": "planned_commands_not_measurements", "cpu_map": cpu_map,
                                        "frequency_policy_ids": options.frequency_policy_ids, "commands": commands})
            print(f"Wrote {len(commands)} condition commands to {options.output}; no benchmark was executed")
            return 0
        batches = [read_json(path) for path in options.merge]
        prior_provenance = batches[0].get("provenance", {}) if batches else {}
        device_id = options.device_id or prior_provenance.get("device_id")
        os_build = options.os_build or prior_provenance.get("os_build")
        if not device_id or not os_build:
            raise ValueError("Actual --device-id and --os-build are required")
        binary_hash = prior_provenance.get("binary_sha256") if options.merge_only else sha256(binary)
        if not binary_hash:
            raise ValueError("A measured binary hash is required")
        report = {"schema_version": SCHEMA, "plan_id": plan["id"], "hardware_profile_id": plan["hardware_profile_id"],
                  "provenance": {"device_id": device_id, "os_build": os_build, "collector": "parallel_calibration_collect/1.2.0",
                                 "measured_at": utc_now(), "source_kind": "measured", "binary_sha256": binary_hash,
                                 "collector_sha256": sha256(__file__), "plan_sha256": sha256(options.plan),
                                 "build_info": prior_provenance.get("build_info", options.build_info) if options.merge_only else options.build_info,
                                 "cpu_map": cpu_map, "frequency_policy_ids": options.frequency_policy_ids,
                                 "thermal_evidence": "Operator declaration plus before/after raw thermal-zone snapshots; no automatic no-throttle inference",
                                 "observer_cpu": options.observer_cpu,
                                 "batch_ids": [], "merged_input_sha256": [sha256(path) for path in options.merge]},
                  "samples": [], "collection_errors": [], "invalid_samples": []}
        seen = set()
        for batch in batches:
            if batch.get("schema_version") != SCHEMA or batch.get("plan_id") != plan["id"] or batch.get("hardware_profile_id") != plan["hardware_profile_id"]:
                raise ValueError("Merged files must use the same frozen plan and hardware profile")
            provenance = batch.get("provenance", {})
            for key, value in (("device_id", device_id), ("os_build", os_build), ("binary_sha256", binary_hash)):
                if provenance.get(key) != value:
                    raise ValueError(f"Merged batches disagree on {key}; do not combine different calibration domains")
            if provenance.get("cpu_map") != cpu_map:
                raise ValueError("Merged batches use a different physical-core mapping")
            if provenance.get("frequency_policy_ids", options.frequency_policy_ids) != options.frequency_policy_ids:
                raise ValueError("Merged batches use different frequency-policy mappings")
            if provenance.get("observer_cpu", options.observer_cpu) != options.observer_cpu:
                raise ValueError("Merged batches use a different fixed observer CPU")
            for sample in batch.get("samples", []):
                validate_measurement_contract(sample)
                if sample.get("sample_id") in seen:
                    raise ValueError("Duplicate sample_id in merged batches")
                seen.add(sample.get("sample_id")); report["samples"].append(sample)
            for field in ("collection_errors", "invalid_samples"):
                report[field].extend(batch.get(field, []))
            report["provenance"]["batch_ids"].extend(provenance.get("batch_ids", []))
        batch_id = "batch_" + uuid.uuid4().hex
        if not options.merge_only:
            report["provenance"]["batch_ids"].append(batch_id)
            for condition, command in zip(conditions, commands):
                for repeat in range(condition["repeats"]):
                    run_id = "run_" + uuid.uuid4().hex
                    sample_id = "sample_" + uuid.uuid4().hex
                    before = thermal_zones()
                    try:
                        completed = subprocess.run(command["command"], check=True, capture_output=True, text=True, timeout=options.timeout_seconds)
                        measurement = json.loads(completed.stdout)
                        validate_measurement_contract(measurement)
                        for field in ("benchmark", "family", "frequency_khz", "active_tasks", "work_units"):
                            if measurement.get(field) != condition[field]:
                                raise ValueError(f"Benchmark result disagrees with frozen condition {field}")
                        expected_policy = options.frequency_policy_ids[condition["core_kind"]]
                        if measurement.get("frequency_policy_id") != expected_policy:
                            raise ValueError("Benchmark result used a different target frequency policy")
                        if condition["benchmark"] == "pair_work":
                            for field in ("workload_class", "peer_workload_class", "peer_frequency_khz", "working_set_bytes"):
                                if measurement.get(field) != condition[field]: raise ValueError(f"Pair benchmark disagrees with frozen condition {field}")
                            if condition["active_tasks"] == 2:
                                expected_peer_policy = options.frequency_policy_ids[condition["peer_core_kind"]]
                                if measurement.get("peer_frequency_policy_id") != expected_peer_policy:
                                    raise ValueError("Benchmark result used a different peer frequency policy")
                        if condition["benchmark"] == "roofline":
                            for field in ("workload_class", "working_set_bytes", "roofline_operations_per_element", "roofline_kernel_version"):
                                if measurement.get(field) != condition[field]: raise ValueError(f"Roofline benchmark disagrees with frozen condition {field}")
                        after = thermal_zones()
                        sample = {**measurement, **{key: condition[key] for key in ("benchmark", "family", "workload_id", "core_kind", "frequency_khz", "active_tasks", "work_units", "split")},
                                  "sample_id": sample_id, "run_id": run_id, "batch_id": batch_id, "condition_id": condition["id"], "repeat": repeat,
                                  "data_kind": "measured", "observed_frequency_khz": measurement.get("frequency_mean_khz"),
                                  "observed_frequency_min_khz": measurement.get("frequency_min_khz"), "observed_frequency_max_khz": measurement.get("frequency_max_khz"),
                                  "thermal_throttled": False if options.thermal_state == "normal" else True if options.thermal_state == "throttled" else None,
                                  "thermal_note": options.thermal_note,
                                  "thermal_snapshot_before": before, "thermal_snapshot_after": after,
                                  "raw_sha256": hashlib.sha256(completed.stdout.encode()).hexdigest(), "binary_sha256": binary_hash,
                                  "measured_at": utc_now(), "command": command["command"]}
                        if condition["benchmark"] == "pair_work":
                            sample["peer_core_kind"] = condition["peer_core_kind"]
                            # Static source-kernel semantics are hash-frozen in
                            # the plan.  Copy them beside the measured timing;
                            # do not infer FLOPs, retired instructions or DRAM
                            # bytes from the integer proxy at collection time.
                            for field in ("workload_intensity", "peer_workload_intensity"):
                                if field in condition:
                                    sample[field] = condition[field]
                        report["samples"].append(sample)
                        reasons = quality_reasons(sample)
                        if reasons:
                            report["invalid_samples"].append({"sample_id": sample_id, "reasons": reasons})
                        print(f'{condition["id"]} {repeat + 1}/{condition["repeats"]}: CPU {sample["cpu_ms"]:.3f} ms; {"invalid: " + "; ".join(reasons) if reasons else "quality checks passed"}', file=sys.stderr)
                    except (OSError, ValueError, subprocess.SubprocessError) as exc:
                        report["collection_errors"].append({"condition_id": condition["id"], "run_id": run_id, "repeat": repeat, "error": str(exc)[:2000]})
                        print(f'{condition["id"]}: measurement failed: {exc}', file=sys.stderr)
                    atomic_json(options.output, report)
                    if options.cooldown_seconds:
                        time.sleep(options.cooldown_seconds)
        report["collection_status"] = "partial" if report["collection_errors"] or report["invalid_samples"] else "completed"
        atomic_json(options.output, report)
        print(f'Wrote {len(report["samples"])} actual samples to {options.output}; {len(report["invalid_samples"])} invalid, {len(report["collection_errors"])} failed')
        return 2 if report["collection_status"] == "partial" else 0
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Calibration collection error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
