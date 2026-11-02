"""Synchronous public API for import, replay, and reproducible conditional search.

The archived proxy model is intentionally marked legacy_compat. PMU is a measured
feature with interval uncertainty, never a substitute for a calibrated compute /
memory performance model. No result represents an application modification.
"""

from __future__ import annotations

import copy
import hashlib
import itertools
import json
import math
import os
import re
import shutil
import tempfile
import uuid
import zipfile
from collections import Counter
from pathlib import Path

from backend.core import PlatformError, sha256_file, utc_now
from . import service_dag
from .features import component_features, summarize
from .diagrams import annotate
from .importer import capture_inputs, raw_to_parsed
from .calibration import CalibrationManager
from .hardware_profiles import HardwareProfileStore
from .search_executor import SearchEvaluationCancelled, simulate_configurations

BUNDLE_ROOT = Path(__file__).resolve().parents[2] / "data" / "parallel-modeling"
BUILTIN_ID = "gallery-edit-pmu-20261014"
MODEL_ID = "gallery-native-transfer-v1"
HARDWARE_ID = "kirin-gallery-reference-v1"
FULL_PMU_BUILTIN_ID = "gallery-edit-all-pmu-20261023"
MAX_CONFIGURATION_CANDIDATES = 20_000
ANALYSIS_ENGINE = "portable_service_dag/v2+off_cpu_accounting/v1+source_placement_transfer/v1"
COST_MODEL_SEMANTICS = "frequency_roofline/v1+source_placement_transfer/v1+off_cpu_accounting/v1"
SEARCH_ENGINE = "portable_service_dag/v2+" + COST_MODEL_SEMANTICS
SEARCH_ID_ENGINE = "workload_interference/v1+core_combination/v1+" + COST_MODEL_SEMANTICS
ARTIFACT_PRODUCER = {"component": "parallel_modeling", "version": "1.2.0", "engine": SEARCH_ENGINE}
DEFAULT_SIMULATION_PARAMETERS = {"frequency_elasticity": None, "contention": True,
                                 "service_capacity": 1, "branch_overhead_ms": .05}
RAW_EVIDENCE_FILES = ("trace.ftrace", "pmu.csv", "pmu.json", "trace_validation.json",
                      "clock_before.txt", "clock_after.txt", "trace_clock.txt",
                      "hitrace_begin.log", "hitrace_finish.log", "raw-event-deltas.json", "manifest.json")
BUILTIN_DATASETS = {
    BUILTIN_ID: {
        "name": "图库 · 编辑页 · PMU 同轮归档", "dataset_name": "图库 · 编辑页 · 同轮 PMU 归档",
        "hardware_profile_id": HARDWARE_ID, "hardware_identity_verified": True,
        "prediction_applicability": "archived_reference_sample",
        "description": "2026-10-14，vsync 60209；真实 UI callback 86.940 ms。与历史 90.409 ms 样本分别保存。",
        "metrics": {"duration_ms": 86.94, "builditem_count": 26, "service_call_count": 65},
        "sample_identity": "2026-10-14 / vsync 60209 / 86.940 ms",
        "distinct_historical_sample": "90.409 ms / vsync 28031 is not the selected sample",
    },
    FULL_PMU_BUILTIN_ID: {
        "name": "图库 · 编辑页 · 全事件组 PMU 归档 · 复用估计",
        "hardware_profile_id": None, "hardware_identity_verified": False,
        "prediction_applicability": "analysis_only",
        "description": "2026-10-23，vsync 663634；真实 UI callback 136.517 ms。7 组、14 种请求事件；扩展组复用约 16.6%，分支指令零增量待核验。已核对参考硬件，可按原生代理模型进行条件寻优。",
        "metrics": {"duration_ms": 136.517, "builditem_count": 26, "service_call_count": 73},
        "sample_identity": "2026-10-23 / vsync 663634 / 136.517 ms",
    },
}
KINDS = ("big", "middle", "small")
CORE_COUNTS = {"big": 1, "middle": 3, "small": 4}
LOGICAL_CPUS = {"big": [10, 11], "middle": [4, 5, 6, 7, 8, 9], "small": [0, 1, 2, 3]}
ASSUMPTIONS = [
    "基于实际负载进行条件仿真；跨组件独立和线程安全是并行成立的前提。",
    "保留组件内顺序、同步 Binder 请求与返回、局部等待和未并行工作，依赖策略为 preserve_observed。",
    "所选 BuildItem 的输入假定在共同 fork 点就绪，输出在共同 join 后消费。",
    "服务停留时间沿用观测条件，服务并发容量独立于应用 CPU 资源建模。",
    "On-CPU 按核型、PMU阶段分类、已校准频率拐点与争用换算；未校准类别回退逐核频率弹性。Off-CPU 调度/服务账目保持Trace实测。",
    "core-ms 表示 CPU 占用时间；PMU 通用 cache miss 作为负载特征，不换算为能耗或 DRAM 字节。",
    "耗时终点为 UI callback 完成；预测基于分配给应用的核组资源，范围为该回调及其同步服务。",
]


def _fail(message, code="parallel_modeling_invalid_input", **details):
    raise PlatformError(code, message, details=details)


def _bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def _hash(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _capture_device_serial_sha256(manifest):
    """Hash the capture identity without carrying the raw HDC serial forward."""
    serial = manifest.get("device_serial") if isinstance(manifest, dict) else None
    if not isinstance(serial, str) or not serial or len(serial) > 160:
        return None
    return hashlib.sha256(serial.encode("utf-8")).hexdigest()


def _cpu_topology_signature(profile):
    """Compare CPU topology while deliberately ignoring OPP and display metadata."""
    groups = []
    for group in profile.get("core_groups", profile.get("cpu_groups", [])):
        logical_cpus = tuple(sorted(group.get("logical_cpu_ids", [])))
        topology = group.get("physical_core_topology")
        if isinstance(topology, list) and topology:
            physical_cores = tuple(sorted(
                tuple(sorted(core.get("logical_cpu_ids", [])))
                for core in topology if isinstance(core, dict)
            ))
            representatives = ()
        else:
            physical_cores = ()
            representatives = tuple(sorted(group.get("physical_core_representative_cpu_ids", [])))
        groups.append({
            "logical_cpu_ids": logical_cpus,
            "physical_core_count": group.get("physical_core_count"),
            "physical_core_topology": physical_cores,
            "physical_core_representative_cpu_ids": representatives,
        })
    return _hash(sorted(groups, key=lambda row: (row["logical_cpu_ids"], row["physical_core_count"] or 0)))


def _number(value, label, minimum=0):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < minimum:
        _fail(f"{label} must be a finite number >= {minimum}")
    return value


def _checkpoint(cancel_event=None, progress_callback=None, **status):
    if cancel_event is not None and cancel_event.is_set():
        _fail("Parallel modeling job was cancelled", code="job_cancelled")
    if progress_callback:
        progress_callback({**status, **({"percent": status["progress"]} if "progress" in status else {})})


def _core_combination(configuration, core_groups=None):
    """Describe allocated application physical cores, independent of OPP/dispatch.

    The enumerator reserves one distinct physical core for each worker and one
    for the main lane, including when their kinds match. Main participation
    reuses lane zero; it neither adds a core nor counts the SMT CPU IDs again.
    Service CPUs remain outside the application's allocated resource pool.
    """
    core_groups = core_groups or [{"id": kind, "name": {"big": "大", "middle": "中", "small": "小"}[kind]} for kind in KINDS]
    group_ids = [group["id"] for group in core_groups]
    counts = {group_id: 0 for group_id in group_ids}
    for lane in [configuration["main"], *configuration["workers"]]:
        if lane["kind"] not in counts:
            counts[lane["kind"]] = 0
            group_ids.append(lane["kind"])
        counts[lane["kind"]] += 1
    names = {group["id"]: group.get("name", group["id"]) for group in core_groups}
    return {"combination_id": "-".join(f"{kind}{counts[kind]}" for kind in group_ids),
            "combination_label": " + ".join(f"{counts[kind]} {names.get(kind, kind)}" for kind in group_ids if counts[kind]),
            "physical_core_counts": counts, "physical_core_count": sum(counts.values())}


def _model_assumptions(model):
    values = list(ASSUMPTIONS)
    if model.get("status") == "topology_proxy_uncalibrated":
        values[4] = "核组数量与 OPP 来自所选硬件画像；核型倍率、频率弹性、争用与启动成本继承自原生代理模型，尚未在该设备校准，结果仅用于条件比较。"
    elif model["id"] != MODEL_ID:
        values[4] = "On-CPU倍率、逐核/逐类型频率拐点、争用与启动成本使用所选校准版本；阶段PMU分类选择compute/memory/mixed曲线。Off-CPU保持Trace实测；未测类别/频率/共跑组合属于条件外推。"
    return values


def _source_transfer_assumption(summary):
    if summary.get("verified_component_count"):
        if summary.get("risk_level") != "none":
            return ("已核验阶段可按源核组/频率驻留还原 On-CPU 参考工作量，但分类置信度、驻留覆盖容差"
                    "或部分阶段回退仍带来证据风险；具体原因保存在 source_placement_cost_transfer。"
                    "Off-CPU 与服务账目不变。")
        if summary.get("fallback_component_count"):
            return ("已核验阶段按真实源核组/频率驻留将 On-CPU 时间还原为统一参考工作量；"
                    "其余阶段因源画像、覆盖率或曲线证据不完整回退旧参考锚。Off-CPU 与服务账目不变。")
        return ("On-CPU 按真实源核组/频率混合驻留先还原为统一参考工作量，再由目标核组/频率曲线预测；"
                "Off-CPU 与服务账目不变。")
    return ("源 CPU/核组/频率驻留未形成完整可核验成本锚，On-CPU 沿用旧参考锚回退；"
            "回退原因保存在 source_placement_cost_transfer，Off-CPU 与服务账目不变。")


def _validate_parsed(parsed):
    if not isinstance(parsed, dict) or not all(key in parsed for key in ("frame", "binder_chains", "wait_ledger")):
        _fail("parsed must contain frame, binder_chains and wait_ledger")
    frame = parsed["frame"]
    if not isinstance(frame, dict) or not isinstance(frame.get("spans"), list) or not isinstance(frame.get("units"), list):
        _fail("frame must contain spans and non-overlapping units")
    start, end = frame.get("start_ns"), frame.get("end_ns")
    _number(start, "frame.start_ns")
    _number(end, "frame.end_ns")
    if end <= start or end - start > 60e9:
        _fail("UI callback duration must be between 0 and 60 seconds")
    if abs(_number(frame.get("duration_us"), "frame.duration_us") * 1000 - (end - start)) > 1:
        _fail("frame.duration_us disagrees with frame boundaries")
    if len(frame["spans"]) > 100000 or len(frame["units"]) > 20000:
        _fail("Parsed frame exceeds supported work-unit limit")
    cursor = start
    for unit in frame["units"]:
        if not isinstance(unit, dict) or not all(key in unit for key in ("id", "name", "source_line", "phases")):
            _fail("Each unit needs id, name, source_line and phases")
        for phase in unit["phases"]:
            if phase.get("kind") not in ("on_cpu", "runnable", "blocked", "unknown"):
                _fail("Unknown scheduler phase kind")
            if phase.get("start_ns") != cursor or phase.get("end_ns", 0) <= cursor:
                _fail("Scheduler phases must form a contiguous, non-overlapping partition")
            cursor = phase["end_ns"]
    if cursor != end:
        _fail("Scheduler phases do not cover the entire UI callback")
    builds = []
    for span in frame["spans"]:
        if "CustomNode:BuildItem" not in span.get("name", ""):
            continue
        if not re.search(r"BuildItem \[([^]]+)\]", span["name"]):
            _fail("BuildItem scope must contain a component name in brackets")
        if not start <= span.get("start_ns", -1) < span.get("end_ns", -1) <= end or "line" not in span:
            _fail("BuildItem scope lies outside the selected callback")
        builds.append(span)
    if not builds:
        _fail("The selected callback contains no complete BuildItem scopes")
    if len(builds) > 1000:
        _fail("More than 1000 components are not supported in one search")
    if not isinstance(parsed["binder_chains"], dict) or not isinstance(parsed["binder_chains"].get("chains"), list):
        _fail("binder_chains must contain a chains array")
    for wait in parsed["wait_ledger"]:
        if not start <= wait.get("start_ns", -1) < wait.get("end_ns", -1) <= end:
            _fail("Wait ledger interval lies outside the callback")
        if len(wait.get("binder_request_ids", [])) > 1:
            _fail("Ambiguous blocked interval has multiple synchronous Binder requests; refine the evidence before modeling")
    # Nonblocking or unresolved calls remain provenance. Only exact matched blocking
    # requests can enter this legacy service-residence model.
    wait_ids = {item for wait in parsed["wait_ledger"] for item in wait.get("binder_request_ids", [])}
    matched = [chain for chain in parsed["binder_chains"]["chains"] if chain.get("kind") == "synchronous_stack_matched"]
    if any(chain.get("id") not in wait_ids for chain in matched):
        _fail("A matched Binder request has no unique blocked interval; this service model cannot replay it safely")


class ParallelModelingManager:
    def __init__(self, state_root, artifact_store=None, portable_calibration_package_root=None):
        self.capture_state_root = Path(state_root).resolve()
        self.state_root = self.capture_state_root / "parallel-modeling"
        self.state_root.mkdir(parents=True, exist_ok=True)
        self.artifact_store = artifact_store
        self.model = json.loads((BUNDLE_ROOT / f"{MODEL_ID}.json").read_text())
        self.bundle_provenance = json.loads((BUNDLE_ROOT / "provenance.json").read_text())
        for name, digest in self.bundle_provenance["files"].items():
            if hashlib.sha256((BUNDLE_ROOT / name).read_bytes()).hexdigest() != digest:
                _fail("Bundled modeling data hash verification failed", code="parallel_modeling_bundle_corrupt", file=name)
        for category in ("datasets", "analyses", "searches", "inputs"):
            (self.state_root / category).mkdir(exist_ok=True)
        self.hardware_profiles = HardwareProfileStore(self.state_root, self._builtin_hardware_profile())
        self.calibration = CalibrationManager(self.capture_state_root, self.model,
            self.hardware_profiles.get(HARDWARE_ID), artifact_store,
            hardware_profile_store=self.hardware_profiles)
        self.installed_calibration_packages = (
            self.calibration.install_portable_packages(portable_calibration_package_root)
            if portable_calibration_package_root is not None else [])
        from .workload_features import WorkloadFeaturesManager
        self.workload_features = WorkloadFeaturesManager(self)

    def _builtin_hardware_profile(self):
        profiles = self.model["profiles"]
        groups = [{"id": kind, "name": {"big": "大核组", "middle": "中核组", "small": "小核组"}[kind],
                   "kind": kind, "physical_core_count": CORE_COUNTS[kind], "logical_cpu_ids": LOGICAL_CPUS[kind],
                   "physical_core_representative_cpu_ids": {"big": [10], "middle": [4, 6, 8], "small": [0, 1, 2, 3]}[kind],
                   "frequency_domain_id": kind, "opps_khz": profiles[kind]["opps_khz"],
                   "min_khz": profiles[kind]["opps_khz"][0], "max_khz": profiles[kind]["max_khz"]} for kind in KINDS]
        return {"id": HARDWARE_ID, "name": "图库归档参考设备 · 1 大 / 3 中 / 4 小物理核", "core_groups": groups,
                "frequency_domains": [{"id": kind, "core_group_ids": [kind], "shared_opp": True,
                                       "opps_khz": profiles[kind]["opps_khz"]} for kind in KINDS]}

    def _proxy_model_id(self, profile):
        if profile["id"] == HARDWARE_ID:
            return MODEL_ID
        return "tm_" + _hash({"base_model_id": MODEL_ID, "base_model_version": self.model["version"],
                               "hardware_profile_id": profile["id"], "profile_sha256": profile["profile_sha256"]})[:24]

    def _proxy_model_summary(self, profile):
        return {"id": self._proxy_model_id(profile), "name": profile["name"] + " · 拓扑代理迁移模型",
                "version": self.model["version"], "status": "topology_proxy_uncalibrated",
                "hardware_profile_id": profile["id"],
                "description": "核组拓扑与 OPP 来自该硬件画像；性能倍率、频率响应、争用和启动成本继承自原生代理模型，未经该设备校准，仅用于条件比较。",
                "validation": {"status": "not_device_calibrated", "application_validation": "not_application_validated",
                               "topology_profile_sha256": profile["profile_sha256"],
                               "inherited_from_model_id": MODEL_ID},
                "calibrated_parameters": [],
                "inherited_parameters": ["core speed ratios", "frequency elasticity", "contention", "dispatch overhead", "service capacity"]}

    def _decorate_hardware_profile(self, profile):
        profile = copy.deepcopy(profile)
        if hasattr(self, "calibration"):
            profile = self.calibration.decorate_hardware_profile_for_display(profile)
        builtin = profile["id"] == HARDWARE_ID
        profile.update(builtin=builtin, archived=profile.get("status") == "archived")
        if profile.get("status") == "active":
            model_id = (profile.get("bundled_calibration_version_id")
                        or (MODEL_ID if builtin else self._proxy_model_id(profile)))
            profile.update(compatible_model_package_id=model_id, model_package_id=model_id,
                           model_status=(profile.get("bundled_calibration_status")
                                         or ("legacy_compat" if builtin else "topology_proxy_uncalibrated")))
        else:
            profile.update(compatible_model_package_id=None, model_package_id=None, model_status="unavailable")
        return profile

    def list_hardware_profiles(self, *, include_archived=True):
        return [self._decorate_hardware_profile(row)
                for row in self.hardware_profiles.list(include_archived=include_archived)]

    def get_hardware_profile(self, identity, *, require_active=False):
        return self._decorate_hardware_profile(self.hardware_profiles.get(identity, require_active=require_active))

    def create_hardware_profile(self, payload):
        return self._decorate_hardware_profile(self.hardware_profiles.create(payload))

    def archive_hardware_profile(self, identity):
        profile = self.hardware_profiles.get(identity)
        references = [row["id"] for row in self.list_datasets() if row.get("hardware_profile_id") == identity]
        if references:
            _fail("硬件画像已被数据集引用，不能归档。", code="parallel_hardware_profile_in_use",
                  hardware_profile_id=identity, dataset_ids=references[:50])
        return self._decorate_hardware_profile(self.hardware_profiles.archive(profile["id"]))

    def restore_hardware_profile(self, identity):
        if self.calibration.is_hardware_profile_superseded(identity):
            _fail("该硬件画像已被后续离线校准包退役，不能重新用于分析或寻优。",
                  code="parallel_hardware_profile_superseded",
                  hardware_profile_id=identity)
        return self._decorate_hardware_profile(self.hardware_profiles.restore(identity))

    def _resolve_model(self, identity):
        if not identity:
            identity = MODEL_ID
        if isinstance(identity, str) and identity.startswith("tm_"):
            for profile in self.hardware_profiles.list(include_archived=False):
                if profile["id"] != HARDWARE_ID and self._proxy_model_id(profile) == identity:
                    model = copy.deepcopy(self.model)
                    summary = self._proxy_model_summary(profile)
                    model.update(summary)
                    transferred_profiles = {}
                    for group in profile["core_groups"]:
                        group_id = group["id"]
                        proxy_kind = group.get("proxy_kind", group.get("kind"))
                        if proxy_kind not in KINDS:
                            _fail("硬件画像缺少有效的代理核型映射。", code="parallel_hardware_profile_invalid",
                                  hardware_profile_id=profile["id"], core_group_id=group_id)
                        transferred_profiles[group_id] = copy.deepcopy(self.model["profiles"][proxy_kind])
                        transferred_profiles[group_id]["opps_khz"] = copy.deepcopy(group["opps_khz"])
                        transferred_profiles[group_id]["max_khz"] = group["max_khz"]
                        transferred_profiles[group_id]["proxy_kind"] = proxy_kind
                        transferred_profiles[group_id]["provenance"] = (
                            "OPP domain from hardware profile; performance coefficients inherited from " + MODEL_ID)
                    model["profiles"] = transferred_profiles
                    model["hardware_profile"] = {key: copy.deepcopy(profile[key]) for key in
                        ("id", "name", "source", "core_groups", "frequency_domains", "profile_sha256")}
                    model["transfer_provenance"] = {"base_model_id": MODEL_ID,
                        "base_model_sha256": self.bundle_provenance["files"][MODEL_ID + ".json"],
                        "hardware_profile_id": profile["id"], "hardware_profile_sha256": profile["profile_sha256"],
                        "performance_coefficients": "inherited_not_device_calibrated"}
                    return model
            _fail("所选拓扑代理模型不存在或对应硬件画像已归档。", code="parallel_modeling_cost_model_unavailable",
                  model_package_id=identity)
        try:
            return self.calibration.resolve_model(identity)
        except PlatformError as exc:
            if exc.code == "parallel_calibration_not_found":
                _fail("所选成本模型不存在。", code="parallel_modeling_cost_model_unavailable",
                      model_package_id=identity)
            raise

    def _model_hardware_profile_sha256(self, model):
        """Recover the immutable hardware snapshot used to create a model."""
        embedded = model.get("hardware_profile") if isinstance(model.get("hardware_profile"), dict) else {}
        candidates = [
            model.get("hardware_profile_sha256"),
            embedded.get("profile_sha256"),
            (model.get("transfer_provenance") or {}).get("hardware_profile_sha256"),
            (model.get("validation") or {}).get("topology_profile_sha256"),
        ]
        if model.get("id") == MODEL_ID and model.get("hardware_profile_id") == HARDWARE_ID:
            candidates.append(self.hardware_profiles.get(HARDWARE_ID).get("profile_sha256"))
        return next((value for value in candidates if isinstance(value, str) and value), None)

    def _default_model_id(self, dataset):
        hardware_id = self.resolve_hardware(dataset)["hardware_profile_id"]
        if not hardware_id:
            return MODEL_ID
        # Published calibration versions are already hardware-bound and are
        # ordered newest first.  Prefer that measured response whenever the
        # caller leaves model_package_id unset; the legacy/proxy model remains
        # an explicit compatibility fallback when no calibrated version exists.
        versions = self.calibration.list_versions()
        for version in versions:
            if (version.get("hardware_profile_id") == hardware_id
                    and version.get("status") in ("proxy_calibrated", "partially_calibrated")):
                return version["id"]
        # A risk-calibrated version is still useful when it is the only
        # measured model for this device, but it must never shadow an older
        # version that passed the statistical/coverage gates.
        for version in versions:
            if (version.get("hardware_profile_id") == hardware_id
                    and version.get("status") == "risk_calibrated"):
                return version["id"]
        if hardware_id == HARDWARE_ID:
            return MODEL_ID
        profile = self.hardware_profiles.get(hardware_id, require_active=True)
        return self._proxy_model_id(profile)

    def catalog(self):
        versions = self.calibration.list_versions() if hasattr(self, "calibration") else []
        active_profiles = self.hardware_profiles.list(include_archived=False)
        custom_models = [self._proxy_model_summary(profile) for profile in active_profiles if profile["id"] != HARDWARE_ID]
        return {"schema_version": "parallel_modeling_catalog/v1",
                "builtins": [{"id": identity, **copy.deepcopy({key: value[key] for key in
                    ("name", "hardware_profile_id", "description", "metrics", "prediction_applicability")})}
                    for identity, value in BUILTIN_DATASETS.items()],
                "hardware_profiles": [self._decorate_hardware_profile(profile) for profile in active_profiles],
                "model_packages": [{"id": MODEL_ID, "name": "原生代理迁移模型 v1", "version": self.model["version"],
                                    "status": "legacy_compat", "hardware_profile_id": HARDWARE_ID,
                                    "description": "离线原生代理核型倍率与争用模型；频率弹性做条件敏感性推演，尚无图库并行实测验证。",
                                    "validation": self.model["validation"]}] + [
                    {key: version[key] for key in ("id", "name", "version", "status", "hardware_profile_id", "description", "validation", "calibrated_parameters", "inherited_parameters", "risk_level", "risk_summary", "evidence_grade", "failed_gates", "display_source", "display_source_label", "display_provenance")
                     if key in version}
                    for version in versions] + custom_models,
                "objectives": [{"id": "minimum_latency", "name": "延迟最小"},
                               {"id": "minimum_frequency_with_budget", "name": "满足预算的最低频率"}],
                "defaults": {"model_package_id": MODEL_ID, "dependency_policy": "preserve_observed", "main_core_group_id": "big",
                             "worker_counts": [1, 2, 3, 4], "frequency_policy": "all_opps", "main_participation": [False, True]},
                "import_formats": ["builtin_id", "parsed", "trace_text", "capture_manifest"], "assumptions": ASSUMPTIONS}

    def _write_json(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            temporary.write_bytes(_bytes(value))
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def _record(self, category, record):
        if self.artifact_store:
            artifact_type = "parallel_modeling_" + {"datasets": "dataset", "analyses": "analysis", "searches": "search"}[category]
            upstream = (self.get_dataset(record["dataset_id"]) if category == "analyses" else
                        self.get_analysis(record["analysis_id"]) if category == "searches" else {})
            input_artifacts = ["artifact:" + item["artifact_id"] for item in upstream.get("artifacts", [])]
            calibration = (record.get("cost_model") or record.get("model_package") or {}).get("calibration_provenance") or {}
            input_artifacts.extend("artifact:" + item["artifact_id"] for item in calibration.get("artifacts", []))
            input_artifacts.extend("artifact:" + item["artifact_id"] for item in
                                   (record.get("workload_profile") or {}).get("artifacts", []))
            artifact = self.artifact_store.write(artifact_type, record,
                source_connector_id="parallel-modeling", source_binding={"record_id": record["id"]},
                producer=ARTIFACT_PRODUCER, input_artifacts=input_artifacts)
            record = {**record, "artifacts": [{"artifact_id": artifact["artifact_id"], "artifact_type": artifact["artifact_type"]}]}
        else:
            record = {**record, "artifacts": []}
        self._write_json(self.state_root / category / (record["id"] + ".json"), record)
        return copy.deepcopy(record)

    def _read(self, category, identity):
        if not isinstance(identity, str) or not re.fullmatch(r"[a-z]{2}_[0-9a-f]{24}", identity):
            _fail("Invalid modeling record ID", code="parallel_modeling_not_found")
        path = self.state_root / category / (identity + ".json")
        if not path.is_file():
            _fail("Modeling record was not found", code="parallel_modeling_not_found", record_id=identity)
        return json.loads(path.read_text())

    def _list(self, category):
        rows = [json.loads(path.read_text()) for path in (self.state_root / category).glob("*.json")]
        return sorted(rows, key=lambda row: (row.get("created_at", ""), row["id"]), reverse=True)

    def get_dataset(self, identity):
        return self._read("datasets", identity)

    def get_analysis(self, identity):
        return self._read("analyses", identity)

    def get_search(self, identity):
        return self._read("searches", identity)

    def list_datasets(self):
        return self._list("datasets")

    def list_analyses(self):
        return self._list("analyses")

    def list_searches(self):
        return self._list("searches")

    def resolve_hardware(self, dataset):
        """Resolve verified sample evidence without rewriting dataset/profile IDs."""
        source = dataset.get("hardware_profile_id")
        if source:
            recorded = dataset.get("provenance", {}).get("hardware_resolution")
            if isinstance(recorded, dict) and recorded.get("hardware_profile_id") == source:
                return {**copy.deepcopy(recorded), "hardware_profile_id": source,
                        "source_hardware_profile_id": source,
                        "hardware_identity_verified": bool(dataset.get("quality", {}).get("hardware_identity_verified")),
                        "prediction_applicability": dataset.get("quality", {}).get("prediction_applicability", "unknown")}
            return {"hardware_profile_id": source, "source_hardware_profile_id": source,
                    "source": "dataset_profile", "hardware_identity_verified": bool(dataset.get("quality", {}).get("hardware_identity_verified")),
                    "prediction_applicability": dataset.get("quality", {}).get("prediction_applicability", "unknown")}
        parsed_path = self.state_root / "inputs" / dataset["id"] / "parsed.json"
        parsed = json.loads(parsed_path.read_text())
        parsed_digest = _hash(parsed)
        for name in self.bundle_provenance.get("hardware_bindings", []):
            if name not in self.bundle_provenance["files"]:
                _fail("Hardware evidence is missing from bundle verification", code="parallel_modeling_bundle_corrupt")
            evidence = json.loads((BUNDLE_ROOT / name).read_text())
            if (evidence.get("parsed_sha256") != parsed_digest
                    or evidence.get("trace_sha256") != parsed.get("selection", {}).get("trace_sha256")):
                continue
            return {"hardware_profile_id": evidence["hardware_profile_id"], "source_hardware_profile_id": None,
                    "source": "verified_archived_sample", "sample_id": evidence["sample_id"],
                    "hardware_identity_verified": True, "prediction_applicability": "verified_archive_proxy_prediction",
                    "evidence_file": name, "evidence_sha256": self.bundle_provenance["files"][name],
                    "parsed_sha256": parsed_digest, "trace_sha256": evidence["trace_sha256"],
                    "reference_anchor": evidence["reference_anchor"], "note": evidence["note"]}
        return {"hardware_profile_id": None, "source_hardware_profile_id": None,
                "source": "unresolved", "hardware_identity_verified": False, "prediction_applicability": "analysis_only"}

    def _unpack_builtin_raw(self, sample, directory):
        """Materialize verified demo inputs without depending on a capture host."""
        archive = sample.get("raw_archive")
        if not archive:
            return
        try:
            path = BUNDLE_ROOT / archive["file"]
            if not path.resolve().is_relative_to(BUNDLE_ROOT.resolve()):
                raise ValueError("Archive path is outside the bundled data directory")
            if sha256_file(path) != archive["sha256"]:
                raise ValueError("Archive hash does not match provenance")
            with zipfile.ZipFile(path) as bundle:
                for name, evidence in archive["files"].items():
                    if name not in RAW_EVIDENCE_FILES:
                        raise ValueError("Unknown raw evidence member")
                    if bundle.getinfo(name).file_size != evidence["size_bytes"]:
                        raise ValueError(f"Archive member size mismatch: {name}")
                    destination = directory / name
                    with bundle.open(name) as source, destination.open("wb") as target:
                        shutil.copyfileobj(source, target, length=1024 * 1024)
                    if sha256_file(destination) != evidence["sha256"]:
                        raise ValueError(f"Archive member hash mismatch: {name}")
                # The ZIP hash also covers the portable sample manifest.
                (directory / "manifest.json").write_bytes(bundle.read("manifest.json"))
        except (OSError, ValueError, KeyError, zipfile.BadZipFile) as exc:
            _fail("Bundled raw example verification failed", code="parallel_modeling_bundle_corrupt", reason=str(exc))

    def _resolve_capture_hardware_profile(self, manifest, requested_profile_id=None):
        """Resolve a capture to an active profile using recorded identity only.

        CPU numbers, frequency domains and OPPs are intentionally not used as
        identity evidence.  They are consulted only to reject an ambiguous set
        of same-device profiles whose CPU topologies disagree.
        """
        if not isinstance(manifest, dict):
            manifest = {}
        serial_sha256 = _capture_device_serial_sha256(manifest)
        if requested_profile_id is not None:
            profile = self.hardware_profiles.get(requested_profile_id, require_active=True)
            expected = (profile.get("evidence") or {}).get("device_serial_sha256")
            verified = bool(serial_sha256 and expected and serial_sha256 == expected)
            note = ("导入时显式选择的硬件画像与采集设备身份摘要一致。" if verified else
                    "导入时已显式选择硬件画像；设备身份摘要缺失或不一致，保留该选择但不标记为身份已核验。")
            return {
                "schema_version": "capture_hardware_resolution/v1",
                "status": "matched_import_request_profile", "source": "import_request_binding",
                "hardware_profile_id": profile["id"], "hardware_profile_sha256": profile["profile_sha256"],
                "hardware_identity_verified": verified, "device_serial_sha256": serial_sha256,
                "candidate_count": 1, "candidate_hardware_profile_ids": [profile["id"]], "note": note,
            }
        explicit = manifest.get("hardware_profile_id")
        if explicit is not None:
            try:
                profile = self.hardware_profiles.get(explicit, require_active=True)
            except PlatformError as exc:
                if exc.code not in {"parallel_hardware_profile_not_found", "parallel_hardware_profile_archived"}:
                    raise
                return {
                    "schema_version": "capture_hardware_resolution/v1",
                    "status": "recorded_profile_unavailable", "source": "capture_record_binding",
                    "hardware_profile_id": None, "hardware_identity_verified": False,
                    "device_serial_sha256": serial_sha256, "candidate_count": 0,
                    "candidate_hardware_profile_ids": [],
                    "note": "采集记录绑定的硬件画像不存在或已归档；保留为未绑定数据集。",
                }
            expected = (profile.get("evidence") or {}).get("device_serial_sha256")
            verified = bool(serial_sha256 and expected and serial_sha256 == expected)
            note = ("采集记录已绑定活动硬件画像，且设备身份摘要一致。" if verified else
                    "采集记录已绑定活动硬件画像；设备身份摘要缺失或不一致，保留显式绑定但不标记为身份已核验。")
            return {
                "schema_version": "capture_hardware_resolution/v1",
                "status": "matched_recorded_profile", "source": "capture_record_binding",
                "hardware_profile_id": profile["id"], "hardware_profile_sha256": profile["profile_sha256"],
                "hardware_identity_verified": verified, "device_serial_sha256": serial_sha256,
                "candidate_count": 1, "candidate_hardware_profile_ids": [profile["id"]], "note": note,
            }

        if not serial_sha256:
            return {
                "schema_version": "capture_hardware_resolution/v1",
                "status": "device_identity_unavailable", "source": "unresolved",
                "hardware_profile_id": None, "hardware_identity_verified": False,
                "device_serial_sha256": None, "candidate_count": 0,
                "candidate_hardware_profile_ids": [],
                "note": "采集记录未包含可用的设备身份，未自动绑定硬件画像。",
            }
        candidates = [profile for profile in self.hardware_profiles.list(include_archived=False)
                      if (profile.get("evidence") or {}).get("device_serial_sha256") == serial_sha256]
        candidate_ids = sorted(profile["id"] for profile in candidates)
        if not candidates:
            return {
                "schema_version": "capture_hardware_resolution/v1",
                "status": "no_matching_active_profile", "source": "device_serial_sha256",
                "hardware_profile_id": None, "hardware_identity_verified": False,
                "device_serial_sha256": serial_sha256, "candidate_count": 0,
                "candidate_hardware_profile_ids": [],
                "note": "设备身份未匹配到活动硬件画像；请先从该设备提取并登记画像。",
            }
        signatures = {_cpu_topology_signature(profile) for profile in candidates}
        if len(signatures) != 1:
            return {
                "schema_version": "capture_hardware_resolution/v1",
                "status": "ambiguous_conflicting_cpu_topology", "source": "device_serial_sha256",
                "hardware_profile_id": None, "hardware_identity_verified": False,
                "device_serial_sha256": serial_sha256, "candidate_count": len(candidates),
                "candidate_hardware_profile_ids": candidate_ids, "topology_variant_count": len(signatures),
                "note": "同一设备身份匹配到多个活动画像，但 CPU 拓扑互相冲突；为避免误绑，数据集保持未绑定。",
            }
        selected = max(candidates, key=lambda profile: (profile.get("created_at", ""), profile["id"]))
        multiple = len(candidates) > 1
        return {
            "schema_version": "capture_hardware_resolution/v1",
            "status": "matched_latest_compatible_profile" if multiple else "matched_unique_device_identity",
            "source": "device_serial_sha256", "hardware_profile_id": selected["id"],
            "hardware_profile_sha256": selected["profile_sha256"], "hardware_identity_verified": True,
            "device_serial_sha256": serial_sha256, "candidate_count": len(candidates),
            "candidate_hardware_profile_ids": candidate_ids, "topology_variant_count": 1,
            "selection_policy": "latest_created_at_then_profile_id" if multiple else "unique_identity_match",
            "note": (f"设备身份匹配到 {len(candidates)} 个 CPU 拓扑一致的活动画像，已选择最新画像。" if multiple else
                     "设备身份唯一匹配到活动硬件画像。"),
        }

    def import_dataset(self, payload):
        if not isinstance(payload, dict):
            _fail("Import payload must be a JSON object")
        payload = copy.deepcopy(payload)
        modes = [name for name in ("builtin_id", "parsed", "trace_text", "capture_manifest") if name in payload]
        if len(modes) != 1:
            _fail("Choose exactly one import source", supported=["builtin_id", "parsed", "trace_text", "capture_manifest"])
        source_kind = modes[0]
        supplied_hardware = payload.get("hardware_profile_id")
        if supplied_hardware:
            self.hardware_profiles.get(supplied_hardware, require_active=True)
        builtin = None
        capture_hardware_resolution = None
        provenance = {"source_kind": source_kind, "data_kind": "measured", "endpoint": "ui_callback"}
        with tempfile.TemporaryDirectory(prefix="import-", dir=self.state_root) as temporary:
            directory = Path(temporary)
            try:
                if source_kind == "builtin_id":
                    builtin_id = payload["builtin_id"]
                    builtin = BUILTIN_DATASETS.get(builtin_id)
                    if builtin is None:
                        _fail("Unknown built-in dataset")
                    parsed = json.loads((BUNDLE_ROOT / (builtin_id + ".json")).read_text())
                    hardware = builtin["hardware_profile_id"]
                    provenance.update(builtin_id=builtin_id, archive=self.bundle_provenance,
                                      sample_identity=builtin["sample_identity"])
                    if "distinct_historical_sample" in builtin:
                        provenance["distinct_historical_sample"] = builtin["distinct_historical_sample"]
                    if builtin_id in self.bundle_provenance.get("datasets", {}):
                        provenance["sample_archive"] = copy.deepcopy(self.bundle_provenance["datasets"][builtin_id])
                        self._unpack_builtin_raw(provenance["sample_archive"], directory)
                elif source_kind == "parsed":
                    parsed = payload["parsed"]
                    hardware = supplied_hardware
                    provenance["input_verification"] = "User-provided parsed evidence; original trace hash cannot be independently verified without raw trace"
                else:
                    if source_kind == "capture_manifest":
                        payload = capture_inputs(payload, self.capture_state_root)
                        provenance["capture"] = payload["capture_provenance"]
                        capture_hardware_resolution = self._resolve_capture_hardware_profile(
                            payload["capture_manifest"], supplied_hardware)
                        provenance["hardware_resolution"] = copy.deepcopy(capture_hardware_resolution)
                    parsed = raw_to_parsed(payload, directory)
                    hardware = (capture_hardware_resolution or {}).get("hardware_profile_id") if source_kind == "capture_manifest" else supplied_hardware
                    provenance["selection"] = parsed["selection"]
                _validate_parsed(parsed)
                if source_kind != "builtin_id":
                    for chain in parsed["binder_chains"]["chains"]:
                        # Historical PIDs do not identify services in another capture.
                        chain.setdefault("service_name", "PID " + str(chain.get("dest_pid", "unknown")))
                for name in ("frame", "binder_chains", "wait_ledger"):
                    self._write_json(directory / (name + ".json"), parsed[name])
                extracted = service_dag.extract(directory)
                if len(extracted["nodes"]) > 20000:
                    _fail("Extracted DAG exceeds the 20000-node limit")
                extracted["sources"] = [{"path": "dataset://" + name + ".json", "sha256": _hash(parsed[name])}
                                        for name in ("frame", "binder_chains", "wait_ledger")]
                dataset_identity = {"parsed": parsed, "hardware": hardware}
                if builtin and payload["builtin_id"] != BUILTIN_ID:
                    # Keep deployable archives separate from their live capture
                    # records, while preserving the original archive's stable ID.
                    dataset_identity["builtin_id"] = payload["builtin_id"]
                identity = "ds_" + _hash(dataset_identity)[:24]
                name = payload.get("name") or (builtin.get("dataset_name", builtin["name"]) if builtin else "导入的 BuildItem 回调")
                capture_verified = bool((capture_hardware_resolution or {}).get("hardware_identity_verified"))
                if source_kind == "capture_manifest":
                    resolution_source = (capture_hardware_resolution or {}).get("source")
                    prediction_applicability = ("device_identity_matched_profile_prediction" if capture_verified else
                                                "import_request_bound_profile_prediction" if resolution_source == "import_request_binding" else
                                                "capture_record_bound_profile_prediction" if hardware else "analysis_only")
                    binding_note = (capture_hardware_resolution or {}).get("note")
                else:
                    prediction_applicability = (builtin["prediction_applicability"] if builtin else
                                                "explicit_reference_profile_transfer_without_application_validation" if hardware else "analysis_only")
                    binding_note = None
                quality = {"input_partition_valid": True, "pmu": parsed.get("pmu_quality", {"status": "not_collected"}),
                           "scheduler_unknown_ms": extracted["states_ms"].get("unknown", 0),
                           "parse_warnings": parsed["frame"].get("parse_warnings", {}),
                           "unresolved_binder_calls": sum(chain.get("kind") == "synchronous_unresolved" for chain in parsed["binder_chains"]["chains"]),
                           "hardware_identity_verified": bool(builtin and builtin["hardware_identity_verified"]) or capture_verified,
                           "prediction_applicability": prediction_applicability,
                           "can_search": bool(hardware)}
                if binding_note:
                    quality["hardware_binding_note"] = binding_note
                    quality["hardware_binding_status"] = capture_hardware_resolution["status"]
                    quality["hardware_binding_candidate_count"] = capture_hardware_resolution["candidate_count"]
                record = {"schema_version": "parallel_modeling_dataset/v1", "id": identity, "name": str(name)[:200],
                          "created_at": utc_now(), "status": "ready", "source_kind": source_kind, "hardware_profile_id": hardware,
                          "summary": {"duration_ms": extracted["frame"]["duration_us"] / 1000, "builditem_count": len(extracted["components"]),
                                      "service_call_count": len(extracted["requests"]), "tid": extracted["frame"].get("tid"),
                                      "vsync_id": extracted["frame"].get("vsync_id"), "endpoint": "ui_callback"},
                          "provenance": provenance, "quality": quality}
                saved = self.state_root / "inputs" / identity
                self._write_json(saved / "extracted.json", extracted)
                self._write_json(saved / "parsed.json", parsed)
                # Preserve raw evidence so newly imported results can be independently re-parsed.
                for raw_name in RAW_EVIDENCE_FILES:
                    if (directory / raw_name).is_file():
                        shutil.copyfile(directory / raw_name, saved / raw_name)
                return self._record("datasets", record)
            except PlatformError:
                raise
            except (ValueError, KeyError, TypeError, IndexError, AssertionError, ZeroDivisionError) as exc:
                _fail("The input cannot be safely represented by this service-DAG model", reason=str(exc))

    def bind_dataset_hardware(self, dataset_id, payload):
        """Create an immutable dataset revision bound to one active profile.

        Analysis and workload-feature records retain dataset IDs in their
        identity.  A derived dataset therefore avoids silently changing the
        evidence basis of already persisted results.
        """
        if not isinstance(payload, dict):
            _fail("硬件绑定请求应为 JSON 对象。")
        source = self.get_dataset(dataset_id)
        profile = self.hardware_profiles.get(payload.get("hardware_profile_id"), require_active=True)
        if source.get("hardware_profile_id") == profile["id"]:
            return source
        source_input = self.state_root / "inputs" / source["id"]
        parsed_path = source_input / "parsed.json"
        extracted_path = source_input / "extracted.json"
        if not parsed_path.is_file() or not extracted_path.is_file():
            _fail("源数据集输入不完整，不能创建硬件绑定版本。", code="parallel_modeling_dataset_evidence_missing",
                  dataset_id=source["id"])
        parsed_digest = hashlib.sha256(parsed_path.read_bytes()).hexdigest()
        identity = "ds_" + _hash({"derived_from_dataset_id": source["id"],
                                    "hardware_profile_id": profile["id"],
                                    "hardware_profile_sha256": profile["profile_sha256"],
                                    "parsed_file_sha256": parsed_digest,
                                    "binding_schema": "parallel_dataset_hardware_binding/v1"})[:24]
        record_path = self.state_root / "datasets" / f"{identity}.json"
        if record_path.is_file():
            return self.get_dataset(identity)
        destination = self.state_root / "inputs" / identity
        staging = destination.with_name(destination.name + "." + uuid.uuid4().hex + ".tmp")
        try:
            if not destination.exists():
                shutil.copytree(source_input, staging)
                os.replace(staging, destination)
            record = copy.deepcopy(source)
            record.update(id=identity, created_at=utc_now(), status="ready", source_kind="hardware_binding",
                          hardware_profile_id=profile["id"],
                          name=str(payload.get("name") or (source["name"] + " · " + profile["name"]))[:200])
            record["provenance"] = {**copy.deepcopy(source.get("provenance", {})),
                "derived_from_dataset_id": source["id"],
                "hardware_binding": {"schema_version": "parallel_dataset_hardware_binding/v1",
                    "source_dataset_id": source["id"], "hardware_profile_id": profile["id"],
                    "hardware_profile_sha256": profile["profile_sha256"], "bound_at": record["created_at"],
                    "binding_kind": "user_selected_profile"}}
            record["quality"] = {**copy.deepcopy(source.get("quality", {})),
                "hardware_identity_verified": False,
                "prediction_applicability": "user_bound_topology_proxy_prediction",
                "can_search": True,
                "hardware_binding_note": "拓扑与 OPP 来自所选画像；负载与设备同一性及性能系数未由平台独立验证。"}
            record.pop("artifacts", None)
            return self._record("datasets", record)
        finally:
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    def analyze(self, payload, *, cancel_event=None, progress=None):
        if not isinstance(payload, dict):
            _fail("Analysis payload must be a JSON object")
        _checkpoint(cancel_event, progress, stage="replay", progress=0)
        dataset = self.get_dataset(payload.get("dataset_id"))
        model = self._resolve_model(payload.get("model_package_id") or self._default_model_id(dataset))
        resolution = self.resolve_hardware(dataset)
        if resolution["hardware_profile_id"] and model["hardware_profile_id"] != resolution["hardware_profile_id"]:
            _fail("所选成本模型与数据集硬件画像不一致。", code="parallel_modeling_cost_model_mismatch",
                  hardware_profile_id=resolution["hardware_profile_id"], model_hardware_profile_id=model["hardware_profile_id"])
        feature_profile = self.workload_features.resolve_for_dataset(payload.get("feature_profile_id"), dataset)
        directory = self.state_root / "inputs" / dataset["id"]
        extracted = json.loads((directory / "extracted.json").read_text())
        parsed = json.loads((directory / "parsed.json").read_text())
        observed = annotate(service_dag.make_graph(extracted), extracted)
        parallel = annotate(service_dag.make_graph(extracted, parallel=True), extracted)
        config = {"name": "observed_reference_replay", "main": {"kind": "big", "khz": self.model["profiles"]["big"]["max_khz"]},
                  "workers": [], "main_participates": False, "dispatch": "main_first"}
        replay = service_dag.simulate(observed, config, self.model["profiles"], elasticity=0, contention=False,
                                      service_capacity=1, overhead_ms=0, keep_timeline=True)
        expected_ms = extracted["frame"]["duration_us"] / 1000
        expected_cpu = extracted["states_ms"].get("on_cpu", 0)
        expected_service = sum(row["server_cpu_ms"] for row in extracted["requests"])
        errors = {"endpoint_ms": abs(replay["finish_ms"] - expected_ms), "app_cpu_ms": abs(replay["app_core_ms"] - expected_cpu),
                  "service_cpu_ms": abs(replay["service_core_ms"] - expected_service),
                  "completed_nodes": abs(replay["completed_nodes"] - len(observed["nodes"]))}
        gate = {"passed": all(value <= .01 for value in errors.values()), "tolerance_ms": .01, "errors": errors,
                "meaning": "Structural replay/accounting gate only; this does not validate predicted parallel speedup."}
        rows = component_features(extracted, parsed.get("component_features"))
        current_hardware_profile = (self.hardware_profiles.get(resolution["hardware_profile_id"])
                                    if resolution.get("hardware_profile_id") else None)
        source_transfers, source_transfer_summary = service_dag.resolve_source_cost_transfers(
            feature_profile, model["profiles"], model["hardware_profile_id"],
            self._model_hardware_profile_sha256(model),
            (current_hardware_profile or {}).get("profile_sha256"))
        if feature_profile:
            stage_features = {item["id"]: item for item in feature_profile["stages"]}
            for row in rows:
                row["workload_features"] = stage_features.get(row["id"])
                row["source_placement_cost_transfer"] = source_transfers.get(row["id"])
        observed["component_source_cost_transfers"] = copy.deepcopy(source_transfers)
        parallel["component_source_cost_transfers"] = copy.deepcopy(source_transfers)
        identity_input = {"dataset_id": dataset["id"], "engine": ANALYSIS_ENGINE}
        if feature_profile:
            identity_input["feature_profile_id"] = feature_profile["id"]
        if model["id"] != MODEL_ID:
            identity_input["model_package_id"] = model["id"]
        model_reference_costs = []
        target_compatibility_reasons = Counter()
        target_verified = 0
        for row in rows:
            for kind in model["profiles"]:
                predicted, prediction_detail = service_dag.predict_isolated_component_cpu_ms(
                    row["cpu_ms"], source_transfers.get(row["id"], {}), model["profiles"][kind],
                    model["profiles"][kind]["max_khz"],
                    (source_transfers.get(row["id"]) or {}).get(
                        "workload_class", (row.get("workload_features") or {}).get(
                            "workload_class", "unknown")))
                if prediction_detail["source_status"] == "verified_source_normalization":
                    target_verified += 1
                else:
                    target_compatibility_reasons[prediction_detail["source_fallback_reason"]] += 1
                model_reference_costs.append({
                    "component_id": row["id"], "core_kind": kind,
                    "frequency_khz": model["profiles"][kind]["max_khz"],
                    "predicted_isolated_cpu_ms": predicted,
                    "source_placement_cost_transfer": prediction_detail,
                    "data_kind": "conditional_proxy_prediction", "includes_contention": False,
                })
        target_compatibility = {
            "schema_version": "source_target_curve_compatibility/v1",
            "prediction_count": len(model_reference_costs),
            "verified_prediction_count": target_verified,
            "fallback_prediction_count": len(model_reference_costs) - target_verified,
            "fallback_reasons": dict(target_compatibility_reasons),
            "status": ("verified" if model_reference_costs and target_verified == len(model_reference_costs)
                       else "mixed_verified_and_fallback" if target_verified
                       else "fallback_reference_anchor"),
        }
        analysis_assumptions = [*_model_assumptions(model),
                                _source_transfer_assumption(source_transfer_summary)]
        if target_compatibility["status"] != "verified":
            analysis_assumptions.append(
                "部分隔离成本预测的目标类别/绝对曲线/参考系与源归一锚不兼容，按旧观测参考锚回退；"
                "原因保存在 source_target_curve_compatibility。")
        analysis = {"schema_version": "parallel_modeling_analysis/v1", "id": "an_" + _hash(identity_input)[:24],
                    "created_at": utc_now(), "dataset_id": dataset["id"], "name": dataset["name"], "status": "completed",
                    "baseline": {"duration_ms": expected_ms, "app_cpu_ms": expected_cpu, "service_cpu_ms": expected_service,
                                 "app_off_cpu_ms": replay["app_off_cpu_observed_ms"],
                                 "service_call_count": len(extracted["requests"]), "builditem_count": len(rows), "replay_ms": replay["finish_ms"],
                                 "endpoint": "ui_callback", "data_kind": "measured", "timeline": replay["timeline"],
                                 "critical_chain": replay["critical_chain"], "critical_edges": replay["critical_edges"],
                                 "time_accounting": replay["time_accounting"]},
                    "replay_gate": gate, "components": rows, "statistics": summarize(rows),
                    "feature_profile_id": feature_profile["id"] if feature_profile else None,
                    "workload_profile": feature_profile,
                    "analysis_provenance": {"engine": ANALYSIS_ENGINE, "identity_input_sha256": _hash(identity_input),
                                            "source_placement_cost_transfer": source_transfer_summary,
                                            "source_target_curve_compatibility": target_compatibility},
                    "requests": extracted["requests"], "states_ms": extracted["states_ms"],
                    "graphs": {"observed": observed, "parallel": parallel}, "dependency_policy": "preserve_observed",
                    "cost_model": {"id": model["id"], "status": model["status"], "validation": model["validation"],
                                   "hardware_profile_id": model["hardware_profile_id"],
                                   "description": model.get("description"),
                                   "calibration_provenance": model.get("calibration_provenance"),
                                   "transfer_provenance": model.get("transfer_provenance"),
                                   "calibrated_parameters": model.get("calibrated_parameters", []),
                                   "inherited_parameters": model.get("inherited_parameters", []),
                                   "reference_anchor": model.get("reference_anchor", self.calibration.catalog()["model_structure"]["reference_anchor"])},
                    "model_reference_costs": model_reference_costs,
                    "quality": dataset["quality"], "provenance": dataset["provenance"],
                    "assumptions": analysis_assumptions,
                    "hardware_resolution": resolution}
        _checkpoint(cancel_event, progress, stage="analysis_completed", progress=100)
        return self._record("analyses", analysis)

    def _configurations(self, payload, model=None, hardware_profile=None):
        if not isinstance(payload.get("space", {}), dict):
            _fail("space must be a JSON object")
        space = {**{key: payload[key] for key in ("available_core_group_ids", "main_core_group_id", "worker_counts", "main_participation", "frequency_policy", "frequencies_khz") if key in payload}, **payload.get("space", {})}
        profile = hardware_profile or self.hardware_profiles.get(HARDWARE_ID)
        profile_groups = profile["core_groups"]
        group_ids = [group["id"] for group in profile_groups]
        group_order = {group_id: index for index, group_id in enumerate(group_ids)}
        allowed = space.get("available_core_group_ids", space.get("core_kinds", group_ids))
        if not isinstance(allowed, list) or not allowed or any(kind not in group_ids for kind in allowed):
            _fail("available_core_group_ids must contain known core group IDs")
        default_main = max(profile_groups, key=lambda group: (group["max_khz"], group["physical_core_count"], group["id"]))["id"]
        main_kind = space.get("main_core_group_id", default_main)
        if main_kind not in allowed:
            _fail("main_core_group_id must be available")
        counts = space.get("worker_counts", [1, 2, 3, 4])
        if isinstance(counts, int) and not isinstance(counts, bool):
            counts = [counts]
        core_counts = {group["id"]: group["physical_core_count"] for group in profile_groups}
        maximum_workers = max(core_counts.values())
        accepted_count_limit = max(4, maximum_workers)
        if not isinstance(counts, list) or not counts or any(isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= accepted_count_limit for count in counts):
            _fail(f"worker_counts must contain integers from 0 to {accepted_count_limit}")
        participation = space.get("main_participation", [False, True])
        if isinstance(participation, bool):
            participation = [participation]
        if not isinstance(participation, list) or not participation or any(type(value) is not bool for value in participation):
            _fail("main_participation must be a boolean or an array of booleans")
        policy = space.get("frequency_policy", "all_opps")
        if policy not in ("all_opps", "max_opps"):
            _fail("frequency_policy must be all_opps or max_opps")
        profiles = (model or self.model)["profiles"]
        frequencies = {}
        requested = space.get("frequencies_khz", {})
        if not isinstance(requested, dict) or any(kind not in group_ids for kind in requested):
            _fail("frequencies_khz must map known core groups to supported OPP arrays")
        for kind in group_ids:
            values = requested.get(kind, [profiles[kind]["max_khz"]] if policy == "max_opps" else profiles[kind]["opps_khz"])
            if not isinstance(values, list) or not values or any(type(value) is not int or value not in profiles[kind]["opps_khz"] for value in values):
                _fail("Requested frequency is outside the offline model OPP domain", core_group_id=kind)
            if policy == "max_opps" and any(value != profiles[kind]["max_khz"] for value in values):
                _fail("Explicit frequencies conflict with max_opps policy", core_group_id=kind)
            frequencies[kind] = sorted(set(values))

        unique_counts = sorted(set(counts))
        unique_participation = sorted(set(participation))
        # Count the exact number of legal configurations before constructing
        # any lane arrays.  A many-domain profile with full OPP enumeration can
        # otherwise allocate millions of dictionaries before search begins.
        participation_variants = sum(2 if participates else 1 for participates in unique_participation)
        configurations_per_main_opp = 1 if 0 in unique_counts else 0
        for worker_kind in sorted(set(allowed), key=group_order.get):
            capacity = core_counts[worker_kind] - (1 if worker_kind == main_kind else 0)
            legal_worker_counts = sum(1 for count in unique_counts if 1 <= count <= capacity)
            worker_opp_count = 1 if worker_kind == main_kind else len(frequencies[worker_kind])
            configurations_per_main_opp += legal_worker_counts * worker_opp_count * participation_variants
        candidate_count = len(frequencies[main_kind]) * configurations_per_main_opp
        if candidate_count > MAX_CONFIGURATION_CANDIDATES:
            _fail(
                f"寻优空间将生成 {candidate_count} 个配置，超过上限 {MAX_CONFIGURATION_CANDIDATES}；"
                "请减少参与核组、候选 OPP 或 worker_counts。",
                code="parallel_modeling_search_space_too_large",
                candidate_count=candidate_count,
                candidate_limit=MAX_CONFIGURATION_CANDIDATES,
                main_core_group_id=main_kind,
                active_core_group_count=len(set(allowed)),
                main_opp_count=len(frequencies[main_kind]),
                frequency_counts={kind: len(frequencies[kind]) for kind in sorted(set(allowed), key=group_order.get)},
                worker_counts=unique_counts,
                main_participation=unique_participation,
            )
        configs = []
        for main_frequency in frequencies[main_kind]:
            main = {"kind": main_kind, "khz": main_frequency}
            # The serial reference is included only if explicitly requested with 0 workers.
            if 0 in counts:
                configs.append({"name": main_kind + "_serial", "main": main, "workers": [], "main_participates": False, "dispatch": "main_first"})
            for worker_kind in sorted(set(allowed), key=group_order.get):
                capacity = core_counts[worker_kind] - (1 if worker_kind == main_kind else 0)
                worker_frequencies = [main_frequency] if worker_kind == main_kind else frequencies[worker_kind]
                for count, frequency, participates in itertools.product(unique_counts, worker_frequencies, unique_participation):
                    if not 1 <= count <= capacity:
                        continue
                    for dispatch in (["main_first", "branch_first"] if participates else ["main_first"]):
                        configs.append({"name": f"{main_kind}_{worker_kind}{count}" + ("_help" if participates else ""), "main": main,
                                        "workers": [{"kind": worker_kind, "khz": frequency} for _ in range(count)],
                                        "main_participates": participates, "dispatch": dispatch})
        if not configs:
            _fail("No legal configuration remains after applying physical-core and frequency-domain constraints")
        # Freeze the resolved controls, not only the sparse request.  This lets
        # historical-search UIs restore exactly what the engine evaluated
        # without reverse-engineering inputs from the winning configuration.
        resolved_space = copy.deepcopy(space)
        resolved_space.update({
            "available_core_group_ids": sorted(set(allowed), key=group_order.get),
            "main_core_group_id": main_kind,
            "worker_counts": unique_counts,
            "main_participation": unique_participation,
            "frequency_policy": policy,
        })
        if "frequencies_khz" in space:
            resolved_space["frequencies_khz"] = {
                kind: frequencies[kind]
                for kind in sorted(requested, key=group_order.get)
            }
        return configs, resolved_space

    def search(self, payload, *, cancel_event=None, progress=None):
        if not isinstance(payload, dict):
            _fail("Search payload must be a JSON object")
        analysis = self.get_analysis(payload.get("analysis_id"))
        if not analysis["replay_gate"]["passed"]:
            _fail("Baseline replay gate failed; optimization is disabled", code="parallel_modeling_replay_failed", replay_gate=analysis["replay_gate"])
        dataset = self.get_dataset(analysis["dataset_id"])
        hardware = self.resolve_hardware(dataset)
        if not hardware["hardware_profile_id"]:
            _fail("当前数据集尚未绑定参考硬件画像。可继续分析实测特征；寻优前请从硬件模型管理页选择或提取画像，并创建硬件绑定的数据集版本。",
                  code="parallel_modeling_unknown_hardware", failure_kind="hardware_profile_unbound",
                  hardware_resolution=hardware)
        hardware_profile = self.hardware_profiles.get(hardware["hardware_profile_id"], require_active=True)
        default_model_id = self._proxy_model_id(hardware_profile)
        requested_model_id = payload.get("model_package_id") or analysis.get("cost_model", {}).get("id") or default_model_id
        # Analyses created before a hardware binding can still carry the legacy
        # default.  Prefer the exact profile model unless the caller explicitly
        # selected a package.
        if "model_package_id" not in payload and requested_model_id == MODEL_ID and hardware_profile["id"] != HARDWARE_ID:
            requested_model_id = default_model_id
        model = self._resolve_model(requested_model_id)
        feature_profile = self.workload_features.resolve_for_dataset(
            payload.get("feature_profile_id", analysis.get("feature_profile_id")), dataset)
        if model["hardware_profile_id"] != hardware["hardware_profile_id"]:
            _fail("所选成本模型与负载匹配的硬件画像不一致，请选择对应硬件的模型。",
                  code="parallel_modeling_unknown_hardware", failure_kind="cost_model_hardware_mismatch",
                  hardware_profile_id=hardware["hardware_profile_id"],
                  model_package_id=model["id"], model_hardware_profile_id=model["hardware_profile_id"])
        # ``None`` selects the profile's nominal frequency response.  A scalar
        # is reserved for an explicit sensitivity scenario, so the legacy
        # model still evaluates at 0.75 without being mislabeled as an override.
        nominal = {**DEFAULT_SIMULATION_PARAMETERS, **model.get("simulation_parameters", {})}
        nominal_kwargs = {"elasticity": nominal["frequency_elasticity"], "contention": nominal["contention"],
                          "service_capacity": nominal["service_capacity"], "overhead_ms": nominal["branch_overhead_ms"],
                          "interference_model": model.get("interference_model")}
        if payload.get("dependency_policy", "preserve_observed") != "preserve_observed":
            _fail("Only preserve_observed dependency policy is supported without application source")
        objective = payload.get("objective", "minimum_latency")
        if objective not in ("minimum_latency", "minimum_frequency_with_budget"):
            _fail("Unknown optimization objective")
        budget = _number(payload.get("budget_ms", analysis["baseline"]["duration_ms"]), "budget_ms", minimum=.000001)
        candidates = payload.get("candidate_ids", [row["id"] for row in analysis["components"]])
        known = {row["id"] for row in analysis["components"]}
        if not isinstance(candidates, list) or not candidates or any(not isinstance(item, str) or item not in known for item in candidates):
            _fail("candidate_ids must select known BuildItem component IDs")
        candidates = sorted(set(candidates))
        configurations, space = self._configurations(payload, model, hardware_profile)
        extracted = json.loads((self.state_root / "inputs" / dataset["id"] / "extracted.json").read_text())
        graph = annotate(service_dag.make_graph(extracted, parallel=True, selection=candidates), extracted)
        observed = copy.deepcopy(analysis["graphs"]["observed"])
        component_classes = {identity: kind if kind in {"compute", "memory", "mixed"} else "unknown"
                             for identity, kind in (feature_profile or {}).get("component_classes", {}).items()}
        graph["component_classes"] = component_classes
        observed["component_classes"] = component_classes
        profiles = model["profiles"]
        source_transfers, source_transfer_summary = service_dag.resolve_source_cost_transfers(
            feature_profile, profiles, model["hardware_profile_id"],
            self._model_hardware_profile_sha256(model), hardware_profile.get("profile_sha256"))
        graph["component_source_cost_transfers"] = copy.deepcopy(source_transfers)
        observed["component_source_cost_transfers"] = copy.deepcopy(source_transfers)
        robust = payload.get("robustness", False)
        if type(robust) is not bool:
            _fail("robustness must be a boolean")
        rows = []
        _checkpoint(cancel_event, progress, stage="search", progress=0, completed=0, total=len(configurations))
        def cancelled():
            return cancel_event is not None and cancel_event.is_set()

        def search_progress(completed, total):
            if completed == 1 or completed % 20 == 0 or completed == total:
                _checkpoint(None, progress, stage="search", progress=round(80 * completed / total, 1),
                            completed=completed, total=total)

        try:
            simulation_rows = simulate_configurations(
                parallel_graph=graph,
                observed_graph=observed,
                configurations=configurations,
                profiles=profiles,
                nominal_kwargs=nominal_kwargs,
                cancelled=cancelled,
                on_progress=search_progress,
            )
        except SearchEvaluationCancelled:
            _fail("Parallel modeling job was cancelled", code="job_cancelled")
        for index, result in simulation_rows:
            config = configurations[index]
            vector = {kind: next((lane["khz"] for lane in [config["main"], *config["workers"]] if lane["kind"] == kind), None)
                      for kind in profiles}
            normalized = [frequency / profiles[kind]["max_khz"] for kind, frequency in vector.items() if frequency is not None]
            metrics = {key: result[key] for key in ("finish_ms", "app_core_ms", "app_off_cpu_observed_ms", "service_core_ms", "service_queue_sum_ms", "service_queue_max_ms", "peak_app_cpu_concurrency")}
            rows.append({"configuration_index": index, "configuration": config, "metrics": metrics, "frequency_vector": vector,
                         "interference": result.get("interference"),
                         "frequency_model_usage": result.get("frequency_model_usage"),
                         "source_placement_cost_transfer": result.get("source_placement_cost_transfer"),
                         "frequency_unit": "kHz", "normalized_max_frequency": max(normalized), "normalized_frequency_sum": sum(normalized),
                         "core_slots": 1 + len(config["workers"]), "data_kind": "conditional_prediction", "model_status": model["status"]})

        def frequency_score(row):
            return (row["normalized_max_frequency"], row["normalized_frequency_sum"], row["core_slots"], row["metrics"]["finish_ms"], row["configuration_index"])

        latency_score = lambda row: (row["metrics"]["finish_ms"], row["core_slots"], row["metrics"]["app_core_ms"], row["configuration_index"])
        eligible = rows if objective == "minimum_latency" else [row for row in rows if row["metrics"]["finish_ms"] <= budget + 1e-8]
        rank = latency_score if objective == "minimum_latency" else frequency_score
        grouped = {}
        for row in rows:
            combination = _core_combination(row["configuration"], hardware_profile["core_groups"])
            row["core_combination_id"] = combination["combination_id"]
            grouped.setdefault(combination["combination_id"], {**combination, "rows": []})["rows"].append(row)
        # A calibrated frequency response remains the nominal response. Legacy
        # elasticity sweeps are not silently substituted for measured parameters.
        elasticities = ((None,) if model.get("status") in ("proxy_calibrated", "partially_calibrated", "risk_calibrated")
                        else (.5, .75, 1.0))
        sensitivity_scenarios = list(itertools.product(elasticities, (1, 2, 4), (False, True)))
        robust_evaluated = 0

        def evaluate_sensitivity(row):
            nonlocal robust_evaluated
            if row["metrics"].get("scenario_count") == len(sensitivity_scenarios):
                return row["metrics"]["scenario_worst_ms"]
            worst = 0.0
            config = row["configuration"]
            target = graph if config["workers"] else observed
            for elasticity, capacity, contention in sensitivity_scenarios:
                _checkpoint(cancel_event)
                value = service_dag.simulate(target, config, profiles, elasticity=elasticity, contention=contention,
                                            service_capacity=capacity, overhead_ms=nominal["branch_overhead_ms"],
                                            interference_model=model.get("interference_model"))
                worst = max(worst, value["finish_ms"])
                robust_evaluated += 1
            row["metrics"].update(scenario_worst_ms=worst, scenario_count=len(sensitivity_scenarios))
            _checkpoint(cancel_event, progress, stage="sensitivity", progress=90,
                        evaluated_scenarios=robust_evaluated, core_combination_id=row["core_combination_id"])
            return worst

        selections = []
        for combination in grouped.values():
            _checkpoint(cancel_event)
            group_rows = combination["rows"]
            fastest = min(group_rows, key=latency_score)
            feasible_rows = group_rows if objective == "minimum_latency" else [row for row in group_rows if row["metrics"]["finish_ms"] <= budget + 1e-8]
            ordered = sorted(feasible_rows, key=rank)
            selected = ordered[0] if ordered else None
            if robust and selected is not None:
                if objective == "minimum_latency":
                    # The nominal winner of EACH combination is checked. This
                    # is sensitivity analysis, not a minimax optimization.
                    evaluate_sensitivity(selected)
                else:
                    selected = None
                    # Each group searches its own exact objective order. A
                    # different group's winner cannot establish feasibility.
                    for row in ordered:
                        if evaluate_sensitivity(row) <= budget + 1e-8:
                            selected = row
                            break
            selections.append({"combination": combination, "selected": selected or fastest,
                               "fastest": fastest, "feasible": selected is not None,
                               "nominal_feasible_count": sum(row["metrics"]["finish_ms"] <= budget + 1e-8 for row in group_rows)})

        baseline = analysis["baseline"]
        for row in rows:
            row["comparison"] = {"baseline_ms": baseline["duration_ms"], "latency_gain_ms": baseline["duration_ms"] - row["metrics"]["finish_ms"],
                                 "latency_gain_pct": 100 * (1 - row["metrics"]["finish_ms"] / baseline["duration_ms"]),
                                 "app_cpu_delta_ms": row["metrics"]["app_core_ms"] - baseline["app_cpu_ms"],
                                 "cpu_resource_gain_pct": 100 * (1 - row["metrics"]["app_core_ms"] / baseline["app_cpu_ms"]) if baseline["app_cpu_ms"] else None}
            row["meets_budget"] = row["metrics"]["finish_ms"] <= budget + 1e-8
            row["sensitivity_status"] = ("passed_budget" if row["metrics"]["scenario_worst_ms"] <= budget + 1e-8 else "exceeds_budget") if row["metrics"].get("scenario_count") == len(sensitivity_scenarios) else "not_evaluated"
            row["nominal_feasible"] = objective == "minimum_latency" or row["meets_budget"]
            row["feasible"] = (False if not row["nominal_feasible"] else
                               None if row["sensitivity_status"] == "not_evaluated" else
                               row["sensitivity_status"] == "passed_budget") if robust and objective == "minimum_frequency_with_budget" else row["nominal_feasible"]
            row["rank_scope"] = "nominal_objective_with_explicit_selected_best"

        global_fastest = min(rows, key=latency_score)
        fastest_ms = global_fastest["metrics"]["finish_ms"]
        recommendations = []
        for selection in sorted(selections, key=lambda item: (not item["feasible"],
                                rank(item["selected"]) if item["feasible"] else latency_score(item["selected"]))):
            combination, selected, fastest = selection["combination"], selection["selected"], selection["fastest"]
            gap = max(0.0, selected["metrics"]["finish_ms"] - fastest_ms)
            recommendations.append({**copy.deepcopy(selected),
                **{key: value for key, value in combination.items() if key != "rows"},
                "rank": len(recommendations) + 1, "candidate_count": len(combination["rows"]),
                "nominal_feasible_count": selection["nominal_feasible_count"],
                "selection_status": "objective_optimum" if selection["feasible"] else "budget_infeasible",
                "recommendation_basis": ("fastest_nominal_fallback" if not selection["feasible"] else
                                         "declared_scenarios_objective" if robust and objective == "minimum_frequency_with_budget" else "nominal_objective"),
                "feasible": selection["feasible"],
                "fastest_configuration_index": fastest["configuration_index"],
                "fastest_finish_ms": fastest["metrics"]["finish_ms"],
                "global_fastest_finish_ms": fastest_ms, "latency_gap_ms": gap,
                "latency_gap_pct": 100 * gap / fastest_ms if fastest_ms else 0.0})
        winners = [selection["selected"] for selection in selections if selection["feasible"]]
        if not winners:
            message = ("No configuration meets the budget in all declared sensitivity scenarios" if robust and eligible else
                       "No configuration meets the requested latency budget")
            _fail(message, code="parallel_modeling_no_feasible_configuration", budget_ms=budget,
                  best_latency_ms=fastest_ms, evaluated_configurations=len(rows), evaluated_scenarios=robust_evaluated,
                  core_combination_recommendations=recommendations)
        best = copy.deepcopy(min(winners, key=rank))
        best_graph = graph if best["configuration"]["workers"] else observed
        timeline = service_dag.simulate(best_graph, best["configuration"], profiles, keep_timeline=True, **nominal_kwargs)
        best.update(timeline=timeline["timeline"], critical_chain=timeline["critical_chain"], critical_edges=timeline["critical_edges"],
                    graph=best_graph, node_start_ms=timeline["node_start_ms"], node_end_ms=timeline["node_end_ms"],
                    interference=timeline.get("interference"), frequency_model_usage=timeline.get("frequency_model_usage"),
                    source_placement_cost_transfer=timeline.get("source_placement_cost_transfer"),
                    time_accounting=timeline.get("time_accounting"))
        indexed = {node["id"]: node for node in best_graph["nodes"]}
        component_costs = {item["id"]: {"id": item["id"], "name": item["name"], "data_kind": "conditional_prediction",
                                      "selected_for_parallel": bool(best["configuration"]["workers"]) and item["id"] in candidates,
                                      "app_core_ms": 0.0, "service_core_ms": 0.0, "launch_overhead_ms": 0.0,
                                      "start_ms": None, "end_ms": None} for item in extracted["components"]}
        app_lanes = [{"lane": index, **lane, "app_core_ms": 0.0} for index, lane in enumerate([best["configuration"]["main"], *best["configuration"]["workers"]])]
        services = {}
        for segment in timeline["timeline"]:
            node = indexed[segment["node"]]
            duration = segment["end_ms"] - segment["start_ms"]
            component = node.get("component") or (node.get("group") if node.get("group") in component_costs else None)
            if component:
                cost = component_costs[component]
                cost["start_ms"] = segment["start_ms"] if cost["start_ms"] is None else min(cost["start_ms"], segment["start_ms"])
                cost["end_ms"] = segment["end_ms"] if cost["end_ms"] is None else max(cost["end_ms"], segment["end_ms"])
                if node["kind"] in ("cpu", "begin"):
                    cost["app_core_ms"] += duration
                    if node["kind"] == "begin":
                        cost["launch_overhead_ms"] += duration
                elif node["kind"] == "server_cpu":
                    cost["service_core_ms"] += duration
            if node["kind"] in ("cpu", "begin"):
                app_lanes[segment["lane"]]["app_core_ms"] += duration
            if node.get("resource"):
                service = services.setdefault(node["resource"], {"resource": node["resource"], "name": node.get("service"), "service_core_ms": 0.0})
                if node["kind"] == "server_cpu":
                    service["service_core_ms"] += duration
        for cost in component_costs.values():
            cost["completion_span_ms"] = cost["end_ms"] - cost["start_ms"] if cost["start_ms"] is not None else 0.0
        best["component_costs"] = list(component_costs.values())
        best["resource_costs"] = {"application_lanes": app_lanes, "services": list(services.values()),
                                  "unit": "core-ms", "meaning": "Simulated CPU occupancy; service CPUs are outside the application core pool. Not energy."}
        ranked = sorted(rows, key=lambda row: (row["configuration_index"] != best["configuration_index"],
                                               objective == "minimum_frequency_with_budget" and not row["meets_budget"], rank(row)))
        # Two-dimensional latency/CPU occupancy Pareto frontier; identical metrics
        # keep a single representative. This is distinct from frequency tie-breaking.
        pareto, lowest_cost = [], float("inf")
        for row in sorted(rows, key=lambda row: (row["metrics"]["finish_ms"], row["metrics"]["app_core_ms"], row["configuration_index"])):
            if row["metrics"]["app_core_ms"] < lowest_cost - 1e-9:
                pareto.append(row)
                lowest_cost = row["metrics"]["app_core_ms"]
        normalized_input = {"analysis_id": analysis["id"], "objective": objective, "budget_ms": budget, "candidate_ids": candidates,
                            "configurations": configurations, "model_package_id": model["id"], "robustness": robust,
                            "nominal_parameters": nominal, "hardware_profile_id": hardware_profile["id"],
                            "hardware_profile_sha256": hardware_profile["profile_sha256"]}
        # Search IDs include the numerical semantics.  Roofline frequency
        # response and explicit off-CPU accounting must never collide with a
        # result produced by the earlier scalar-elasticity engine.
        normalized_input["engine_version"] = SEARCH_ID_ENGINE
        normalized_input["feature_profile_id"] = feature_profile["id"] if feature_profile else None
        if hardware["source"] == "verified_archived_sample":
            normalized_input["hardware_resolution"] = hardware
        candidate_target_reasons = Counter(
            reason for row in rows
            for reason in (row.get("source_placement_cost_transfer") or {}).get(
                "fallback_reasons", {}))
        candidate_target_verified = sum(
            (row.get("source_placement_cost_transfer") or {}).get("status") == "verified"
            for row in rows)
        target_compatibility = {
            "schema_version": "source_target_curve_compatibility/v1",
            "candidate_count": len(rows),
            "verified_candidate_count": candidate_target_verified,
            "fallback_candidate_count": len(rows) - candidate_target_verified,
            "candidate_counts_by_fallback_reason": dict(candidate_target_reasons),
            "fallback_reason_measurement": (
                "candidate presence count; best preserves predicted core-ms by reason"),
            "best": copy.deepcopy(best.get("source_placement_cost_transfer")),
            "status": ("verified" if rows and candidate_target_verified == len(rows)
                       else "mixed_verified_and_fallback" if candidate_target_verified
                       else "fallback_reference_anchor"),
        }
        result_assumptions = [*_model_assumptions(model),
                              _source_transfer_assumption(source_transfer_summary)]
        if (best.get("source_placement_cost_transfer") or {}).get("status") != "verified":
            result_assumptions.append(
                "最终配置的目标类别/曲线/参考系未与全部源归一锚兼容；不兼容阶段按旧观测参考锚回退，"
                "原因保存在 source_target_curve_compatibility.best。")
        search_configuration = {
            "schema_version": "parallel_search_configuration/v1",
            "objective": objective,
            "budget_ms": budget,
            "candidate_ids": candidates,
            "model_package_id": model["id"],
            "feature_profile_id": feature_profile["id"] if feature_profile else None,
            "space": copy.deepcopy(space),
            "robustness": robust,
            "dependency_policy": "preserve_observed",
        }
        result = {"schema_version": "parallel_modeling_search/v1", "id": "sr_" + _hash(normalized_input)[:24],
                  "created_at": utc_now(), "status": "completed", "analysis_id": analysis["id"], "dataset_id": dataset["id"],
                  "objective": objective, "budget_ms": budget, "best": best, "candidates": ranked, "pareto": pareto,
                  "core_combination_recommendations": recommendations,
                  "search_configuration": search_configuration,
                  "feature_profile_id": feature_profile["id"] if feature_profile else None,
                  "workload_profile": feature_profile,
                  "hardware_resolution": hardware,
                  "candidate_ids": candidates, "dependency_policy": "preserve_observed",
                  "assumptions": result_assumptions,
                  "model_package": {"id": model["id"], "version": model["version"], "status": model["status"], "validation": model["validation"],
                                    "hardware_profile_id": model["hardware_profile_id"],
                                    "description": model.get("description"),
                                    "calibration_risk": model.get("calibration_risk"),
                                    "calibration_provenance": model.get("calibration_provenance"),
                                    "transfer_provenance": model.get("transfer_provenance"),
                                    "calibrated_parameters": model.get("calibrated_parameters", []),
                                    "inherited_parameters": model.get("inherited_parameters", []),
                                    "dataset_applicability": hardware["prediction_applicability"],
                                    "source_dataset_applicability": dataset["quality"].get("prediction_applicability", "unknown")},
                  "search_provenance": {"engine": SEARCH_ENGINE, "model_sha256": model.get("calibration_provenance", {}).get("model_sha256", _hash(model) if model.get("status") == "topology_proxy_uncalibrated" else self.bundle_provenance["files"][MODEL_ID + ".json"]),
                                        "hardware_profile_id": hardware_profile["id"], "hardware_profile_sha256": hardware_profile["profile_sha256"],
                                        "input_sha256": _hash(normalized_input), "evaluated_configurations": len(rows), "feasible_configurations": len(eligible),
                                        "source_placement_cost_transfer": source_transfer_summary,
                                        "source_target_curve_compatibility": target_compatibility,
                                        "scope": "Exhaustive within requested homogeneous worker-group configurations; no mixed worker groups or application transformation",
                                        "space": space, "robustness_scenarios_evaluated": robust_evaluated,
                                        "robustness_requested": robust,
                                        "core_combination_count": len(recommendations),
                                        "core_combination_grouping": "Allocated application physical cores by kind; main lane counted once including main participation; workers reserve distinct cores; service CPUs excluded.",
                                        "core_combination_order": "One optimum per physical-core combination in objective order; budget-infeasible combinations retain their nominal fastest candidate after feasible combinations.",
                                        "latency_gap_reference": "Minimum nominal simulated finish_ms across all requested configurations, not measured latency or the lowest-frequency recommendations.",
                                        "candidate_order": "Selected best first, then nominally feasible candidates in objective order, then nominally infeasible candidates; unchecked candidates do not inherit the winner's sensitivity validation.",
                                        "sensitivity_parameters": {"frequency_elasticity": list(elasticities), "per_core_fitted_elasticity": {kind: profile.get("frequency_elasticity", .75) for kind, profile in profiles.items()} if model.get("status") in ("proxy_calibrated", "partially_calibrated", "risk_calibrated") else None,
                                                                   "service_capacity": [1, 2, 4], "contention": [False, True]} if robust else None,
                                        "nominal_parameters": {**nominal,
                                            "per_core_frequency_elasticity": {kind: profile.get("frequency_elasticity", .75) for kind, profile in profiles.items()},
                                            "per_core_class_frequency_roofline": {kind: copy.deepcopy(profile.get("frequency_roofline")) for kind, profile in profiles.items() if profile.get("frequency_roofline")}},
                                        "frequency_score": ["maximum normalized active-domain OPP", "sum normalized active-domain OPP", "physical core slots", "latency"],
                                        "robustness_meaning": f"{len(sensitivity_scenarios)} declared scenarios only, not a confidence bound or device validation; minimum_latency evaluates each combination's nominal winner, not the minimax optimum; minimum_frequency_with_budget validates each combination independently" if robust else "not_requested"}}
        _checkpoint(cancel_event, progress, stage="search_completed", progress=100, completed=len(rows), total=len(rows))
        return self._record("searches", result)
