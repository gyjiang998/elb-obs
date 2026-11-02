"""Deterministic Skill inventory and per-Agent-Session acknowledgement contract."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from .core import PlatformError, sha256_file, sha256_json, utc_now


SKILL_BOOTSTRAP_SCHEMA_VERSION = "agent_skill_bootstrap/v2"
SKILL_BOOTSTRAP_ACK_ACTION = "platform.ack_skill_bootstrap"
ORCHESTRATOR_SKILL_ID = "agent-runtime-orchestrator"
DISCOVERY_SKILL_IDS: tuple[str, ...] = ()
DISCOVERY_STATUSES = frozenset({"loaded", "available", "not_found", "load_failed"})

# These operations benefit from Skill inventory diagnostics. Bootstrap state is
# deliberately advisory: an Agent framework may load Skills through its own
# registry, direct file reads, or an adapter-specific mechanism.
BOOTSTRAP_OBSERVED_ACTIONS = frozenset({
    "application_flow.inspect_repository",
    "application_flow.search_source",
    "application_flow.read_source",
    "application_flow.inspect_trace_instrumentation",
    "application_flow.inspect_runtime_trace",
    "application_flow.save_discovery",
    "application_flow.publish_candidates",
    "application_flow.save_scenario",
    "application_flow.compile_mermaid",
    "application_flow.enrich_trace_anchors",
    "application_flow.apply_runtime_trace_alignment",
    "code_graph.build",
    "code_graph.search",
    "code_graph.query_knowledge",
    "code_graph.publish_report",
    "knowledge.build_custom_api_kb",
    "reasoning.prepare_hardware_semantic_route",
    "reasoning.run_hardware_semantic_route",
    "reasoning.prepare_context",
    "reasoning.run_strategy",
    "reasoning.run_blind_analysis",
})


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any, limit: int = 1024) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _skill_versions(run: dict[str, Any]) -> dict[str, str]:
    versions: dict[str, str] = {}
    raw_versions = run.get("skill_versions")
    for item in raw_versions if isinstance(raw_versions, list) else []:
        if not isinstance(item, dict):
            continue
        skill_id = _text(item.get("skill_id"), 120)
        if skill_id:
            versions[skill_id] = _text(item.get("version"), 80)
    profile_skills = _record(_record(run.get("context")).get("skill_profile")).get("skills")
    for skill_id, item in _record(profile_skills).items():
        if isinstance(item, dict) and _text(skill_id, 120):
            versions[str(skill_id)] = _text(item.get("version"), 80) or versions.get(str(skill_id), "")
    return versions


def projected_skill_inventory(workspace: Path, run: dict[str, Any]) -> list[dict[str, Any]]:
    """Hash the neutral Skill projection actually delivered to the Agent."""

    resolved_workspace = workspace.resolve()
    root = resolved_workspace / ".agents" / "skills"
    versions = _skill_versions(run)
    inventory: list[dict[str, Any]] = []
    if not root.is_dir():
        return inventory
    for skill_file in sorted(root.glob("*/SKILL.md"), key=lambda item: item.parent.name):
        if not skill_file.is_file():
            continue
        skill_id = skill_file.parent.name
        inventory.append({
            "skill_id": skill_id,
            "version": versions.get(skill_id) or None,
            "content_sha256": sha256_file(skill_file),
            "source": skill_file.relative_to(resolved_workspace).as_posix(),
        })
    return inventory


def executable_fingerprint(executable: Path | None) -> dict[str, Any]:
    if executable is None:
        return {"status": "missing", "fingerprint_sha256": sha256_json({"status": "missing"})}
    resolved = executable.resolve()
    try:
        stat = resolved.stat()
    except OSError:
        return {
            "status": "missing",
            "name": resolved.name,
            "fingerprint_sha256": sha256_json({"status": "missing", "path": str(resolved)}),
        }
    identity = {
        "path": str(resolved),
        "size": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "mode": stat.st_mode,
    }
    return {
        "status": "ready" if os.access(resolved, os.X_OK) else "not_executable",
        "name": resolved.name,
        "fingerprint_sha256": sha256_json(identity),
    }


def build_skill_bootstrap_requirement(
    *,
    workspace: Path,
    run: dict[str, Any],
    adapter_id: str,
    adapter_fingerprint: str,
    agent_session_id: str,
    executable: Path | None,
) -> dict[str, Any]:
    inventory = projected_skill_inventory(workspace, run)
    by_id = {str(item["skill_id"]): item for item in inventory}
    profile = _record(_record(run.get("context")).get("skill_profile"))
    orchestrator = by_id.get(ORCHESTRATOR_SKILL_ID, {})
    target_skills = {
        skill_id: {
            "projection_status": "projected" if skill_id in by_id else "agent_discovery_required",
            "projected_source": _record(by_id.get(skill_id)).get("source"),
            "projected_sha256": _record(by_id.get(skill_id)).get("content_sha256"),
        }
        for skill_id in DISCOVERY_SKILL_IDS
    }
    executable_identity = executable_fingerprint(executable)
    identity = {
        "schema_version": SKILL_BOOTSTRAP_SCHEMA_VERSION,
        "profile_id": _text(profile.get("profile_id"), 120),
        "profile_sha256": _text(profile.get("profile_sha256"), 128),
        "user_skill_revision": int(profile.get("user_skill_revision") or 0),
        "orchestrator": {
            "skill_id": ORCHESTRATOR_SKILL_ID,
            "version": _text(orchestrator.get("version"), 80),
            "content_sha256": _text(orchestrator.get("content_sha256"), 128),
            "source": _text(orchestrator.get("source"), 512),
        },
        "inventory": inventory,
        "adapter_id": _text(adapter_id, 80),
        "adapter_fingerprint": _text(adapter_fingerprint, 128),
        "agent_session_id": _text(agent_session_id, 200),
        "executable_fingerprint": executable_identity.get("fingerprint_sha256"),
    }
    return {
        **identity,
        "bootstrap_id": sha256_json(identity),
        "status": "pending",
        "required_at": utc_now(),
        "ack_action": SKILL_BOOTSTRAP_ACK_ACTION,
        "enforcement": "advisory",
        "target_skills": target_skills,
        "inventory_count": len(inventory),
        "executable": executable_identity,
        "instructions_zh": (
            "该清单用于诊断 Skill 交付。按任务目标加载主规则与相关 Skill，"
            "并可调用 platform_ack_skill_bootstrap 留存实际加载结果。"
        ),
    }


def merge_skill_bootstrap_requirement(current: Any, requirement: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    prior = _record(current)
    if prior.get("bootstrap_id") == requirement.get("bootstrap_id"):
        return {**requirement, **{
            key: prior.get(key)
            for key in ("status", "required_at", "acknowledged_at", "acknowledgement")
            if prior.get(key) is not None
        }}, False
    return {**requirement, "status": "pending", "acknowledged_at": None, "acknowledgement": None}, True


def public_skill_bootstrap(value: Any) -> dict[str, Any]:
    state = _record(value)
    if not state:
        return {}
    return {
        "schema_version": state.get("schema_version"),
        "bootstrap_id": state.get("bootstrap_id"),
        "status": state.get("status"),
        "required_at": state.get("required_at"),
        "acknowledged_at": state.get("acknowledged_at"),
        "profile_id": state.get("profile_id"),
        "orchestrator": _record(state.get("orchestrator")),
        "adapter_id": state.get("adapter_id"),
        "agent_session_id": state.get("agent_session_id"),
        "inventory_count": state.get("inventory_count", 0),
        "target_skills": _record(state.get("target_skills")),
        "ack_action": state.get("ack_action"),
        "enforcement": state.get("enforcement", "advisory"),
        "instructions_zh": state.get("instructions_zh"),
        "acknowledgement": _record(state.get("acknowledgement")),
    }


def validate_skill_bootstrap_ack(run: dict[str, Any], input_value: dict[str, Any]) -> dict[str, Any]:
    state = _record(run.get("skill_bootstrap"))
    if not state:
        raise PlatformError(
            "SKILL_BOOTSTRAP_NOT_INITIALIZED",
            "当前 Agent Session 尚未生成 Skill Bootstrap 清单。",
            status_code=409,
        )
    expected_id = _text(state.get("bootstrap_id"), 128)
    if _text(input_value.get("bootstrap_id"), 128) != expected_id:
        raise PlatformError(
            "SKILL_BOOTSTRAP_STALE",
            "Skill Bootstrap 清单已经变化，请读取最新清单后重新确认。",
            status_code=409,
            details={"expected_bootstrap_id": expected_id},
        )
    expected_version = _text(_record(state.get("orchestrator")).get("version"), 80)
    if _text(input_value.get("orchestrator_version"), 80) != expected_version:
        raise PlatformError(
            "SKILL_BOOTSTRAP_ORCHESTRATOR_MISMATCH",
            "Agent 确认的主规则版本与当前投影版本不一致。",
            status_code=422,
            details={"expected_version": expected_version},
        )
    if _text(input_value.get("orchestrator_status"), 40) != "loaded":
        raise PlatformError(
            "SKILL_BOOTSTRAP_ORCHESTRATOR_NOT_LOADED",
            "确认代码分析能力前必须实际加载当前主规则。",
            status_code=422,
        )
    raw_discovery = input_value.get("skill_discovery")
    if not isinstance(raw_discovery, list):
        raise PlatformError("SKILL_BOOTSTRAP_DISCOVERY_INVALID", "skill_discovery 必须是数组。", status_code=422)
    discovered: dict[str, dict[str, Any]] = {}
    for raw in raw_discovery:
        if not isinstance(raw, dict):
            continue
        skill_id = _text(raw.get("skill_id"), 120)
        status = _text(raw.get("status"), 40)
        if not skill_id or status not in DISCOVERY_STATUSES:
            continue
        discovered[skill_id] = {
            "skill_id": skill_id,
            "status": status,
            "source": _text(raw.get("source"), 1024) or None,
            "detail": _text(raw.get("detail"), 1200) or None,
        }
    missing = [skill_id for skill_id in DISCOVERY_SKILL_IDS if skill_id not in discovered]
    if missing:
        raise PlatformError(
            "SKILL_BOOTSTRAP_DISCOVERY_INCOMPLETE",
            "Skill 发现结果不完整。",
            status_code=422,
            details={"missing_skill_ids": missing},
        )
    return {
        "schema_version": "agent_skill_bootstrap_ack/v2",
        "bootstrap_id": expected_id,
        "orchestrator_version": expected_version,
        "orchestrator_status": "loaded",
        "skill_discovery": [discovered[skill_id] for skill_id in sorted(discovered)],
        "agent_note": _text(input_value.get("agent_note"), 2000) or None,
    }


def acknowledge_skill_bootstrap(state: Any, acknowledgement: dict[str, Any]) -> dict[str, Any]:
    current = _record(state)
    if current.get("bootstrap_id") != acknowledgement.get("bootstrap_id"):
        raise PlatformError("SKILL_BOOTSTRAP_STALE", "Skill Bootstrap 清单已经变化。", status_code=409)
    return {
        **current,
        "status": "acknowledged",
        "acknowledged_at": utc_now(),
        "acknowledgement": acknowledgement,
    }


def observe_skill_bootstrap(run: dict[str, Any], action_id: str) -> dict[str, Any] | None:
    """Return pending bootstrap diagnostics without blocking the action."""

    if action_id not in BOOTSTRAP_OBSERVED_ACTIONS:
        return None
    state = _record(run.get("skill_bootstrap"))
    if not state:
        return None
    return {
        "action_id": action_id,
        "status": state.get("status"),
        "skill_bootstrap": public_skill_bootstrap(state),
    }
