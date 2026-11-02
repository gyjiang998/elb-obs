"""Prepare a fixed, verified device sampler before scenario collectors start."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import re
import secrets
import shlex
from typing import Any, Callable

from .core import utc_now
from .platform_paths import runtime_asset_root
from tools.scenario_capture.runner import pmu_capability_guard

DEFAULT_DEVICE_PATH = "/data/local/tmp/thread_pmu_sampler"
LEGACY_DEVICE_PATH = "/data/local/tmp/gallery_thread_pmu_focus"
MANAGED_DEFAULT_PATHS = {DEFAULT_DEVICE_PATH, LEGACY_DEVICE_PATH}
BUNDLE_ENV = "HETERO_PMU_SAMPLER_BUNDLE_ROOT"


def sampler_bundle_root() -> Path:
    configured = os.environ.get(BUNDLE_ENV, "").strip()
    return Path(configured).expanduser().resolve() if configured else runtime_asset_root() / "pmu-sampler"


def _bundle_remediation(root: Path) -> dict[str, str]:
    return {"environment": BUNDLE_ENV, "bundle_root": str(root),
        "build_command": f"python -m tools.scenario_capture.provision_pmu_sampler --output {shlex.quote(str(root))}",
        "next_action": "通过正常部署或 heteroctl pmu 恢复仓库随附采样器；若仍失败，请检查仓库文件完整性与目标目录权限。"}


def platform_bundle_status(bundle_root: Path | None = None) -> dict[str, Any]:
    """Inspect server assets only; neither build nor contact a device."""
    from tools.scenario_capture.pmu_bundle import SAMPLER_VERSION, SOURCE_PATH, TARGET, sha256_file, verify_bundle
    root = bundle_root or sampler_bundle_root()
    result: dict[str, Any] = {"status": "missing", "bundle_root": str(root), "sampler_version": SAMPLER_VERSION,
        "source_sha256": sha256_file(SOURCE_PATH), "target": TARGET, "remediation": _bundle_remediation(root),
        "allows_existing_device_sampler": True, "device_status": "not_probed"}
    if not root.is_dir():
        result["message"] = "平台 PMU 采样器尚未准备完成；正常部署会从仓库自动安装，设备已有可用采样器时仍可直接采集。"
        return result
    try:
        bundle = verify_bundle(root)
        result.update(status="ready", message="平台 PMU 采样器资产已校验；设备默认路径缺失时可自动安装。",
            binary_sha256=bundle["binary_sha256"], source_sha256=bundle["source_sha256"],
            sampler_version=bundle["sampler_version"], target=bundle["target"], remediation={})
    except Exception as exc:
        result.update(status="invalid", message="平台 PMU 采样器资产校验失败；设备已有可用采样器时仍可直接采集。",
            code=getattr(exc, "code", "PMU_SAMPLER_BUNDLE_INVALID"), reason=str(exc)[:2000])
    return result


def _propagate_cancellation(client: Any, exc: Exception) -> None:
    from .scenario_capture import CaptureCancelled
    if isinstance(exc, CaptureCancelled):
        raise exc
    client.check_cancelled()
    if getattr(exc, "code", "") == "DEVICE_COMMAND_CANCELLED":
        raise CaptureCancelled("Scenario capture cancelled") from exc


def _availability(client: Any, path: str) -> str:
    quoted = shlex.quote(path)
    output = client.shell(f"if [ -f {quoted} ] && [ -x {quoted} ]; then printf 'executable'; "
        f"elif [ -e {quoted} ] || [ -L {quoted} ]; then printf 'not_executable'; else printf 'missing'; fi", timeout=8)
    if output.strip() not in {"executable", "not_executable", "missing"}:
        raise RuntimeError("设备未返回可确认的 PMU 采样器文件状态。")
    return output.strip()


def _probe(client: Any, path: str, period_us: int) -> dict[str, Any]:
    # Reuse the collector's owned process group, output limit and two-second
    # capability contract. The probe is outside the measurement window.
    directory = "/data/local/tmp/hetero_pmu_probe_" + secrets.token_hex(12)
    command = "\n".join([
        f"out={shlex.quote(directory)}; pmu_bin={shlex.quote(path)}",
        'mkdir -m 700 "$out" || exit 1',
        'trap \'command -v stop_pmu_capability_probe >/dev/null 2>&1 && stop_pmu_capability_probe; rm -rf "$out"\' EXIT',
        "trap 'exit 130' INT TERM HUP",
        *pmu_capability_guard(period_us),
        "printf '__PMU_COMPAT__\\n'; cat \"$out/pmu_compatibility.json\"",
        "printf '\\n__PMU_PROBE__\\n'; cat \"$out/pmu_capability_probe.json\"",
        "printf '\\n__PMU_CAPS__\\n'; head -c 8192 \"$out/pmu_capabilities.json\"",
        "printf '\\n__PMU_HASH__\\n'; (sha256sum \"$pmu_bin\" || toybox sha256sum \"$pmu_bin\") 2>/dev/null || true",
        "printf '\\n__PMU_STDERR__\\n'; head -c 2048 \"$out/pmu_capabilities.log\"",
        "printf '\\n__PMU_END__\\n'",
    ])
    # HDC/bridge shells can emit CRLF or diagnostic lines before/after our
    # response. Only parse explicitly bounded sections, never those messages as
    # capabilities, and retain the surrounding diagnostics for investigation.
    output = client.shell(command, timeout=10).replace("\r\n", "\n")
    markers = ("COMPAT", "PROBE", "CAPS", "HASH", "STDERR", "END")
    boundaries = list(re.finditer(r"(?m)^__PMU_(COMPAT|PROBE|CAPS|HASH|STDERR|END)__(?:\n|$)", output))
    names = tuple(item[1] for item in boundaries)
    if names != markers:
        counts = {name: names.count(name) for name in markers}
        missing = [name for name, count in counts.items() if count == 0]
        repeated = [name for name, count in counts.items() if count > 1]
        detail = ("缺少段：" + ", ".join(missing)) if missing else "响应段次序错误"
        if repeated:
            detail += "；重复段：" + ", ".join(repeated)
        raise RuntimeError(f"PMU 能力探测未返回完整诊断（{detail}）。设备返回：{output[:1200].strip() or '空响应'}")
    # Gateway text normalization can remove blank lines. Adjacent markers are
    # a valid empty section (legacy capabilities and stderr commonly are empty).
    sections = {item[1]: output[item.end():following.start()].strip()
                for item, following in zip(boundaries, boundaries[1:])}
    try:
        compatibility = json.loads(sections["COMPAT"])
        probe = json.loads(sections["PROBE"])
    except (ValueError, TypeError) as exc:
        raise RuntimeError("PMU 能力探测诊断 JSON 无效：" + str(exc)[:300]) from exc
    if not isinstance(compatibility, dict) or not isinstance(probe, dict):
        raise RuntimeError("PMU 能力探测诊断格式错误。")
    try:
        capabilities = json.loads(sections["CAPS"])
    except (ValueError, TypeError):
        capabilities = None
    if not isinstance(capabilities, dict):
        capabilities = None
    tokens = sections["HASH"].split()
    digest = tokens[0] if tokens and re.fullmatch(r"[a-f0-9]{64}", tokens[0]) else None
    return {"compatibility": compatibility, "probe": probe, "capabilities": capabilities,
        "device_sha256": digest, "device_path": path, "probe_stderr": sections["STDERR"][:2048],
        "shell_diagnostics": (output[:boundaries[0].start()] + output[boundaries[-1].end():]).strip()[:2000]}


def prepare_pmu_sampler(plan: dict[str, Any], client: Any, *, bundle_root: Path | None = None,
                        progress: Callable[[dict[str, Any]], None] | None = None) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reuse compatible samplers; install a fixed version for missing/old defaults."""
    execution_plan = copy.deepcopy(plan)
    capture = execution_plan.get("capture", {})
    requested = capture.get("pmu", {})
    path = requested.get("binary_path", DEFAULT_DEVICE_PATH)
    period = int(requested.get("period_us", 100))
    record: dict[str, Any] = {"schema_version": "pmu_sampler_preparation/v1", "status": "not_requested",
        "requested_device_path": path, "requested_period_us": period, "started_at": utc_now(),
        "perf_permission": "not_tested", "perf_events": "not_tested", "automatic_compilation": False}

    def publish(status: str, message: str, **fields: Any) -> None:
        record.update(status=status, message=message, phase="pmu_preparation", **fields)
        if progress:
            progress(copy.deepcopy(record))

    def fail(status: str, code: str, message: str, **fields: Any) -> tuple[dict[str, Any], dict[str, Any]]:
        requested["preparation"] = {"schema_version": "pmu_sampler_execution_gate/v1", "allow_execution": False,
            "status": status, "code": code, "message": message}
        if fields.get("reason"):
            requested["preparation"]["reason"] = fields["reason"]
        publish(status, message, code=code, finished_at=utc_now(), **fields)
        return execution_plan, record

    if "pmu" not in capture.get("collectors", []):
        record["finished_at"] = utc_now()
        return execution_plan, record
    requested["preparation"] = {"schema_version": "pmu_sampler_execution_gate/v1", "allow_execution": False,
        "status": "checking", "code": "PMU_PREPARATION_INCOMPLETE", "message": "PMU 准备尚未完成。"}
    client.check_cancelled()
    publish("checking", "正在检查设备 PMU 采样器。")
    try:
        availability = _availability(client, path)
        record["initial_file_status"] = availability
        if path == DEFAULT_DEVICE_PATH and availability == "missing":
            legacy_availability = _availability(client, LEGACY_DEVICE_PATH)
            record["legacy_default_file_status"] = legacy_availability
            if legacy_availability == "executable":
                path, availability = LEGACY_DEVICE_PATH, legacy_availability
                record["origin"] = "existing_legacy_path"
    except Exception as exc:
        _propagate_cancellation(client, exc)
        return fail("probe_failed", "PMU_SAMPLER_PROBE_FAILED", "设备 PMU 采样器检查失败；继续采集其他数据。", reason=str(exc)[:2000])

    initial_observation = None
    if availability == "executable" and requested.get("group") in {"auto", "all"}:
        publish("probing_capabilities", "正在检查现有采样器的多事件组能力。", device_path=path)
        try:
            initial_observation = _probe(client, path, period)
            if initial_observation["probe"].get("timed_out") is True:
                record.update(initial_observation)
                return fail("probe_timed_out", "PMU_SAMPLER_CAPABILITIES_TIMEOUT", "PMU 采样器能力探测超时并已结束探测进程；继续采集其他数据。")
        except Exception as exc:
            _propagate_cancellation(client, exc)
            return fail("probe_failed", "PMU_SAMPLER_CAPABILITIES_FAILED", "PMU 采样器多事件组能力核验失败；继续采集其他数据。", reason=str(exc)[:2000])
        modes = (initial_observation.get("capabilities") or {}).get("event_groups", [])
        if (not isinstance(modes, list) or requested.get("group") not in modes) and path in MANAGED_DEFAULT_PATHS:
            record["previous_sampler"] = initial_observation
            record["upgrade_reason"] = "multi_group_mode_unavailable"
            availability = "upgrade_required"
            initial_observation = None

    installed = False
    managed = False
    if availability != "executable":
        if availability == "not_executable":
            return fail("not_executable", "PMU_SAMPLER_NOT_EXECUTABLE", "PMU 路径已存在但不可执行；保留原文件，继续采集其他数据。")
        if path not in MANAGED_DEFAULT_PATHS:
            return fail("custom_path_missing", "PMU_CUSTOM_SAMPLER_MISSING", "指定的 PMU 采样器不存在；保留自定义路径，继续采集其他数据。")
        root = bundle_root or sampler_bundle_root()
        publish("verifying_bundle", "正在校验支持所选事件模式的平台采样器资产。", bundle_root=str(root))
        if not root.is_dir():
            return fail("bundle_missing", "PMU_SAMPLER_BUNDLE_MISSING", "平台 PMU 采样器资产尚未构建或配置；Trace 与 HiLog 继续采集。",
                remediation=_bundle_remediation(root))
        try:
            from tools.scenario_capture.pmu_bundle import verify_bundle
            bundle = verify_bundle(root)
            # The verifier owns the source, target, ELF and content hash checks.
            binary = Path(bundle["binary_path"])
            digest = bundle["binary_sha256"]
            if not re.fullmatch(r"[a-f0-9]{64}", digest):
                raise ValueError("Invalid sampler bundle binary SHA-256")
            record["bundle"] = {key: str(value) if isinstance(value, Path) else value for key, value in bundle.items() if key != "binary_path"}
        except Exception as exc:
            _propagate_cancellation(client, exc)
            return fail("bundle_invalid", "PMU_SAMPLER_BUNDLE_INVALID", "平台 PMU 采样器资产校验失败；继续采集其他数据。", reason=str(exc)[:2000],
                bundle_error_code=getattr(exc, "code", "PMU_BUNDLE_INVALID"))
        path = f"/data/local/tmp/hetero_tools/pmu/{digest}/thread_pmu_sampler"
        legacy_managed_path = f"/data/local/tmp/hetero_tools/pmu/{digest}/gallery_thread_pmu"
        client.check_cancelled()
        record.update(managed_device_path=path, origin="managed_bundle")
        publish("checking_managed", "正在检查设备已安装的受管 PMU 版本。")
        try:
            managed_availability = _availability(client, path)
            if managed_availability == "missing":
                legacy_managed_availability = _availability(client, legacy_managed_path)
                record["legacy_managed_file_status"] = legacy_managed_availability
                if legacy_managed_availability == "executable":
                    path, managed_availability = legacy_managed_path, legacy_managed_availability
                    record.update(managed_device_path=path, reused_legacy_managed_path=True)
            record["managed_file_status"] = managed_availability
            if managed_availability == "not_executable":
                return fail("not_executable", "PMU_MANAGED_PATH_CONFLICT", "受管 PMU 版本路径存在但不可执行；保留现场并继续采集其他数据。")
            if managed_availability == "executable":
                quoted = shlex.quote(path)
                observed_hash = client.shell(f"(sha256sum {quoted} || toybox sha256sum {quoted}) 2>/dev/null", timeout=8).split()
                record["managed_sha256_before_probe"] = observed_hash[0] if observed_hash else None
                if not observed_hash or observed_hash[0] != digest:
                    return fail("incompatible", "PMU_INSTALLED_SAMPLER_IDENTITY_MISMATCH", "受管 PMU 采样器内容与构建资产不一致；保留现场，不执行该文件。")
            if managed_availability == "missing":
                publish("deploying", "正在安装已校验的 PMU 采样器。")
                deployment = client.deploy(binary, path, digest, timeout=180)
                if deployment.get("deployment_verified") is not True or deployment.get("remote_sha256") != digest:
                    raise RuntimeError("设备未确认 PMU 二进制的内容哈希。")
                record["deployment"] = deployment
                installed = True
            managed = True
        except Exception as exc:
            _propagate_cancellation(client, exc)
            return fail("deployment_failed", "PMU_SAMPLER_DEPLOYMENT_FAILED", "PMU 采样器安装失败；继续采集其他数据。", reason=str(exc)[:2000])

    publish("probing_capabilities", "正在核验 PMU 采样器版本与采样间隔。", device_path=path)
    try:
        observation = initial_observation or _probe(client, path, period)
        record.update(observation)
    except Exception as exc:
        _propagate_cancellation(client, exc)
        return fail("probe_failed", "PMU_SAMPLER_CAPABILITIES_FAILED", "PMU 采样器能力核验失败；继续采集其他数据。", reason=str(exc)[:2000])
    compatibility = observation["compatibility"].get("status")
    caps = observation.get("capabilities") or {}
    if observation["probe"].get("timed_out") is True:
        return fail("probe_timed_out", "PMU_SAMPLER_CAPABILITIES_TIMEOUT", "PMU 采样器能力探测超时并已结束探测进程；继续采集其他数据。")
    if managed and (observation.get("device_sha256") != digest or caps.get("sampler_version") != bundle.get("sampler_version")):
        return fail("incompatible", "PMU_INSTALLED_SAMPLER_IDENTITY_MISMATCH", "已安装 PMU 采样器的设备身份与构建资产不一致。")
    groups = caps.get("event_groups")
    if requested.get("group") in {"auto", "all"} and (not isinstance(groups, list) or requested["group"] not in groups):
        return fail("incompatible", "PMU_MULTI_GROUP_UPGRADE_REQUIRED", "当前采样器未声明支持多事件组；使用平台默认路径可自动安装新版，或更新自定义采样器。")
    if isinstance(groups, list) and requested.get("group") not in groups:
        return fail("incompatible", "PMU_EVENT_GROUP_UNSUPPORTED", "PMU 采样器不支持请求的事件组；继续采集其他数据。")
    if compatibility not in {"supported", "legacy_unverified"} or (managed and compatibility != "supported"):
        return fail("incompatible", "PMU_SAMPLER_INCOMPATIBLE", "PMU 采样器不支持请求的采样间隔；继续采集其他数据。")
    if path != record["requested_device_path"]:
        requested["binary_path"] = path
    if installed:
        publish("installed", "PMU 采样器已安装并核验；事件权限由本次真实采集验证。", finished_at=utc_now())
    elif compatibility == "legacy_unverified":
        publish("existing_legacy_unverified", "复用已有 PMU 采样器；旧版本未声明能力，实际结果仍需采集验证。", finished_at=utc_now())
    else:
        publish("existing", "复用已安装的 PMU 采样器；事件权限由本次真实采集验证。", finished_at=utc_now())
    requested["preparation"] = {"schema_version": "pmu_sampler_execution_gate/v1", "allow_execution": True, "status": record["status"]}
    return execution_plan, record
