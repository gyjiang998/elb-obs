"""Auditable execution-experience distillation and triage.

Historical Agent/analysis traces are evidence sources.  Model-authored reviews
remain hypotheses until a user selects a resolution and records an outcome.
This module keeps those two concerns separate: immutable review/decision
artifacts preserve provenance, while a small mutable index supports triage UI.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
from pathlib import Path
from typing import Any

from .core import ArtifactStore, PlatformError, atomic_write_json, file_lock, sha256_json, utc_now


SOURCE_TYPES = frozenset({"agent_run", "analysis_trace", "experiment_learning_report"})
CASE_STATUSES = frozenset({"observed", "triaged", "action_planned", "resolved", "verified", "dismissed"})
PITFALL_CATEGORIES = frozenset({
    "model_behavior",
    "agent_framework",
    "workflow_orchestration",
    "skill_design",
    "host_environment",
    "sdk_or_toolchain",
    "runtime_library",
    "source_repository",
    "device_or_os",
    "network_or_provider",
    "evidence_contract",
    "other",
})
RESOLUTION_KINDS = frozenset({
    "environment_config",
    "repository_change",
    "sdk_or_library_change",
    "device_change",
    "runtime_adapter_change",
    "workflow_change",
    "skill_revision",
    "code_fix",
    "additional_evidence",
    "no_action",
})
CONFIDENCE_LEVELS = frozenset({"low", "medium", "high"})
RECURRENCE_KEY_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,119}$")
CASE_ID_PATTERN = re.compile(r"^pit_[a-f0-9]{16}$")
REVIEW_ID_PATTERN = re.compile(r"^review_[a-f0-9]{16}$")
RESOLUTION_ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{2,79}$")


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _text_list(value: Any, *, field: str, maximum: int, item_limit: int = 1200) -> list[str]:
    if not isinstance(value, list) or len(value) > maximum:
        raise PlatformError(
            "EXPERIENCE_REVIEW_INVALID",
            f"{field} 必须是不超过 {maximum} 项的文本数组。",
            status_code=422,
            details={"field": field, "maximum": maximum},
        )
    result: list[str] = []
    for index, item in enumerate(value):
        text = _text(item, item_limit)
        if not text:
            raise PlatformError(
                "EXPERIENCE_REVIEW_INVALID",
                f"{field}[{index}] 不能为空。",
                status_code=422,
                details={"field": field, "index": index},
            )
        result.append(text)
    return result


class ExperienceEvolutionManager:
    """Build and triage reusable lessons without auto-promoting them."""

    def __init__(
        self,
        root: Path,
        *,
        artifact_store: ArtifactStore,
        analysis_trace_store: Any,
        agent_run_store: Any,
    ) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "library.json"
        self.lock_path = self.root / ".experience-evolution.lock"
        self.artifact_store = artifact_store
        self.analysis_trace_store = analysis_trace_store
        self.agent_run_store = agent_run_store
        self._thread_lock = threading.RLock()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "schema_version": "experience_evolution_library/v1",
            "revision": 0,
            "updated_at": None,
            "reviews": [],
            "cases": {},
        }

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.is_file():
            return self._empty()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PlatformError(
                "EXPERIENCE_LIBRARY_CORRUPT",
                "经验演进记录无法读取。",
                status_code=500,
                details={"path": str(self.path), "reason": str(exc)},
            ) from exc
        if not isinstance(value, dict) or value.get("schema_version") != "experience_evolution_library/v1":
            raise PlatformError("EXPERIENCE_LIBRARY_CORRUPT", "经验演进记录结构无效。", status_code=500)
        if not isinstance(value.get("reviews"), list):
            value["reviews"] = []
        if not isinstance(value.get("cases"), dict):
            value["cases"] = {}
        return value

    def _write_unlocked(self, value: dict[str, Any]) -> None:
        value["revision"] = int(value.get("revision") or 0) + 1
        value["updated_at"] = utc_now()
        atomic_write_json(self.path, value)

    @staticmethod
    def _agent_run_environment(item: dict[str, Any]) -> dict[str, Any]:
        engine = _record(item.get("engine"))
        transport = _record(engine.get("terminal_transport"))
        context = _record(item.get("context"))
        runtime_environment = _record(context.get("runtime_environment"))
        target = _record(context.get("target_platform_profile"))
        chipset = _record(target.get("chipset"))
        operating_system = _record(target.get("operating_system"))
        return {
            "agent_engine": engine.get("engine_id"),
            "agent_engine_version": engine.get("engine_version"),
            "agent_adapter": transport.get("adapter_id"),
            "agent_display_name": transport.get("agent_display_name") or runtime_environment.get("agent_display_name"),
            "model_name": runtime_environment.get("model_name"),
            "provider_host": runtime_environment.get("provider_host"),
            "host_os": runtime_environment.get("host_os"),
            "host_release": runtime_environment.get("host_release"),
            "host_architecture": runtime_environment.get("host_architecture"),
            "device_execution_mode": context.get("device_execution_mode"),
            "chipset": chipset.get("model_label"),
            "operating_system": operating_system.get("version_label"),
            "skill_profile_id": _record(context.get("skill_profile")).get("profile_id"),
            "source_connector_id": context.get("source_connector_id"),
            "source_snapshot_id": context.get("source_snapshot_id"),
        }

    def source_catalog(self, *, limit: int = 100) -> dict[str, Any]:
        bounded = max(1, min(int(limit), 300))
        sources: list[dict[str, Any]] = []
        for item in self.agent_run_store.list_recent(limit=500, order_by="created_at"):
            sources.append({
                "source_type": "agent_run",
                "source_id": item.get("run_id"),
                "title": _text(item.get("goal"), 180) or "Agent 执行记录",
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
                "status": item.get("state"),
                "summary": item.get("state_detail"),
                "environment": self._agent_run_environment(item),
                "event_count": int(item.get("event_cursor") or 0),
            })
        for item in self.analysis_trace_store.catalog(limit=500, order_by="created_at"):
            sources.append({
                "source_type": "analysis_trace",
                "source_id": item.get("analysis_id"),
                "title": f"分析 Trace · {item.get('analysis_id')}",
                "created_at": item.get("created_at"),
                "updated_at": item.get("updated_at"),
                "status": "recorded",
                "summary": f"{item.get('event_count', 0)} 条页面、模型与工具事件",
                "environment": {},
                "event_count": item.get("event_count", 0),
            })
        for item in self.artifact_store.list_artifacts("experiment_learning_report", limit=bounded):
            sources.append({
                "source_type": "experiment_learning_report",
                "source_id": item.get("artifact_id"),
                "title": _text(item.get("summary_zh"), 180) or "真机实验学习记录",
                "created_at": item.get("created_at"),
                "updated_at": item.get("created_at"),
                "status": item.get("capture_status"),
                "summary": f"{len(item.get('findings') or [])} 条实验发现",
                "environment": {},
                "event_count": len(item.get("findings") or []),
            })
        sources.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return {
            "schema_version": "experience_source_catalog/v1",
            "count": len(sources[:bounded]),
            "sources": sources[:bounded],
            "source_types": sorted(SOURCE_TYPES),
        }

    def read_source(
        self,
        source_type: str,
        source_id: str,
        *,
        after: int = 0,
        limit: int = 500,
    ) -> dict[str, Any]:
        source_type = _text(source_type, 80)
        source_id = _text(source_id, 160)
        bounded_limit = max(1, min(int(limit), 1000))
        if source_type not in SOURCE_TYPES:
            raise PlatformError("EXPERIENCE_SOURCE_TYPE_INVALID", "经验源类型不受支持。", status_code=422)
        if source_type == "agent_run":
            run = self.agent_run_store.read(source_id)
            events = self.agent_run_store.events(source_id, after_sequence=max(0, int(after)), limit=bounded_limit)
            latest = int(events[-1].get("sequence") or after) if events else max(0, int(after))
            cursor = max(0, int(run.get("next_event_sequence") or 1) - 1)
            return {
                "schema_version": "experience_source/v1",
                "source_type": source_type,
                "source_id": source_id,
                "environment": self._agent_run_environment(run),
                "run": self.agent_run_store.public(run),
                "events": events,
                "next_after": latest,
                "more_available": latest < cursor,
            }
        if source_type == "analysis_trace":
            trace = self.analysis_trace_store.read(source_id, limit=bounded_limit)
            return {
                "schema_version": "experience_source/v1",
                "source_type": source_type,
                "source_id": source_id,
                "trace": trace,
                "more_available": int(trace.get("returned_event_count") or trace.get("event_count") or 0) < int(trace.get("event_count") or 0),
            }
        artifact = self.artifact_store.read("experiment_learning_report", source_id)
        return {
            "schema_version": "experience_source/v1",
            "source_type": source_type,
            "source_id": source_id,
            "report": artifact,
            "more_available": False,
        }

    def _ensure_source_exists(self, source_ref: dict[str, str]) -> None:
        source_type = source_ref["source_type"]
        source_id = source_ref["source_id"]
        if source_type == "agent_run":
            self.agent_run_store.read(source_id)
            return
        if source_type == "analysis_trace":
            self.analysis_trace_store.download_path(source_id)
            return
        self.artifact_store.read("experiment_learning_report", source_id)

    @staticmethod
    def _normalize_source_refs(value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list) or not value or len(value) > 40:
            raise PlatformError(
                "EXPERIENCE_REVIEW_INVALID",
                "source_refs 必须包含 1 到 40 个历史执行源。",
                status_code=422,
            )
        result: list[dict[str, str]] = []
        for index, raw in enumerate(value):
            item = _record(raw)
            source_type = _text(item.get("source_type"), 80)
            source_id = _text(item.get("source_id"), 160)
            if source_type not in SOURCE_TYPES or not source_id:
                raise PlatformError(
                    "EXPERIENCE_REVIEW_INVALID",
                    "source_refs 包含无效历史执行源。",
                    status_code=422,
                    details={"index": index},
                )
            result.append({"source_type": source_type, "source_id": source_id})
        return result

    @staticmethod
    def _normalize_environment_comparison(value: Any) -> list[dict[str, str]]:
        if not isinstance(value, list) or len(value) > 24:
            raise PlatformError("EXPERIENCE_REVIEW_INVALID", "environment_comparison 最多包含 24 项。", status_code=422)
        result: list[dict[str, str]] = []
        for index, raw in enumerate(value):
            item = _record(raw)
            dimension = _text(item.get("dimension"), 120)
            if not dimension:
                raise PlatformError(
                    "EXPERIENCE_REVIEW_INVALID",
                    "环境差异维度不能为空。",
                    status_code=422,
                    details={"index": index},
                )
            result.append({
                "dimension": dimension,
                "baseline": _text(item.get("baseline"), 800) or "未知",
                "observed": _text(item.get("observed"), 800) or "未知",
                "impact": _text(item.get("impact"), 1200) or "尚未确定",
            })
        return result

    @staticmethod
    def _normalize_resolution(raw: Any, *, index: int) -> dict[str, Any]:
        item = _record(raw)
        resolution_id = _text(item.get("resolution_id"), 80)
        kind = _text(item.get("kind"), 80)
        if not RESOLUTION_ID_PATTERN.fullmatch(resolution_id):
            raise PlatformError(
                "EXPERIENCE_REVIEW_INVALID",
                "resolution_id 必须是稳定的小写标识。",
                status_code=422,
                details={"index": index},
            )
        if kind not in RESOLUTION_KINDS:
            raise PlatformError(
                "EXPERIENCE_REVIEW_INVALID",
                "处置类型不受支持。",
                status_code=422,
                details={"index": index, "kind": kind},
            )
        title = _text(item.get("title"), 240)
        rationale = _text(item.get("rationale"), 2000)
        verification = _text(item.get("verification"), 2000)
        if not title or not rationale or not verification:
            raise PlatformError(
                "EXPERIENCE_REVIEW_INVALID",
                "每个处置方案都需要 title、rationale 和 verification。",
                status_code=422,
                details={"index": index},
            )
        return {
            "resolution_id": resolution_id,
            "kind": kind,
            "title": title,
            "rationale": rationale,
            "operator_steps": _text_list(item.get("operator_steps", []), field=f"resolution_options[{index}].operator_steps", maximum=12),
            "agent_steps": _text_list(item.get("agent_steps", []), field=f"resolution_options[{index}].agent_steps", maximum=12),
            "prerequisites": _text_list(item.get("prerequisites", []), field=f"resolution_options[{index}].prerequisites", maximum=10),
            "risks": _text_list(item.get("risks", []), field=f"resolution_options[{index}].risks", maximum=10),
            "verification": verification,
            "target_skill_ids": _text_list(item.get("target_skill_ids", []), field=f"resolution_options[{index}].target_skill_ids", maximum=12, item_limit=80),
            "recommended": bool(item.get("recommended")),
        }

    @classmethod
    def _normalize_pitfall(cls, raw: Any, *, index: int, source_ids: set[str]) -> dict[str, Any]:
        item = _record(raw)
        recurrence_key = _text(item.get("recurrence_key"), 120).lower()
        category = _text(item.get("category"), 80)
        confidence = _text(item.get("confidence"), 20)
        if not RECURRENCE_KEY_PATTERN.fullmatch(recurrence_key):
            raise PlatformError(
                "EXPERIENCE_REVIEW_INVALID",
                "recurrence_key 必须描述可复用问题类，且使用小写字母、数字、点、横线或下划线。",
                status_code=422,
                details={"index": index},
            )
        if category not in PITFALL_CATEGORIES or confidence not in CONFIDENCE_LEVELS:
            raise PlatformError(
                "EXPERIENCE_REVIEW_INVALID",
                "踩坑分类或置信度不受支持。",
                status_code=422,
                details={"index": index, "category": category, "confidence": confidence},
            )
        evidence_source_ids = _text_list(
            item.get("evidence_source_ids"),
            field=f"pitfalls[{index}].evidence_source_ids",
            maximum=20,
            item_limit=160,
        )
        unknown = sorted(set(evidence_source_ids) - source_ids)
        if unknown:
            raise PlatformError(
                "EXPERIENCE_REVIEW_INVALID",
                "踩坑证据只能引用本次 review 声明的历史执行源。",
                status_code=422,
                details={"index": index, "unknown_source_ids": unknown},
            )
        resolutions_raw = item.get("resolution_options")
        if not isinstance(resolutions_raw, list) or not resolutions_raw or len(resolutions_raw) > 8:
            raise PlatformError(
                "EXPERIENCE_REVIEW_INVALID",
                "每个踩坑需要 1 到 8 个可选择处置方案。",
                status_code=422,
                details={"index": index},
            )
        resolutions = [cls._normalize_resolution(value, index=resolution_index) for resolution_index, value in enumerate(resolutions_raw)]
        if len({item["resolution_id"] for item in resolutions}) != len(resolutions):
            raise PlatformError("EXPERIENCE_REVIEW_INVALID", "同一踩坑的 resolution_id 不能重复。", status_code=422)

        required = {
            "title": 240,
            "symptom": 2400,
            "root_cause_hypothesis": 2400,
            "applicability_scope": 1800,
            "generalization_boundary": 1800,
        }
        normalized_text: dict[str, str] = {}
        for field, limit in required.items():
            value = _text(item.get(field), limit)
            if not value:
                raise PlatformError(
                    "EXPERIENCE_REVIEW_INVALID",
                    f"pitfalls[{index}].{field} 不能为空。",
                    status_code=422,
                )
            normalized_text[field] = value
        return {
            "recurrence_key": recurrence_key,
            "category": category,
            "confidence": confidence,
            **normalized_text,
            "failure_stage": _text(item.get("failure_stage"), 160),
            "trigger_conditions": _text_list(item.get("trigger_conditions"), field=f"pitfalls[{index}].trigger_conditions", maximum=12),
            "evidence_source_ids": evidence_source_ids,
            "affected_skill_ids": _text_list(item.get("affected_skill_ids", []), field=f"pitfalls[{index}].affected_skill_ids", maximum=16, item_limit=80),
            "resolution_options": resolutions,
        }

    def save_review(self, value: dict[str, Any], *, agent_run_id: str = "") -> dict[str, Any]:
        source_refs = self._normalize_source_refs(value.get("source_refs"))
        for source_ref in source_refs:
            self._ensure_source_exists(source_ref)
        source_ids = {item["source_id"] for item in source_refs}
        raw_pitfalls = value.get("pitfalls")
        if not isinstance(raw_pitfalls, list) or not raw_pitfalls or len(raw_pitfalls) > 30:
            raise PlatformError("EXPERIENCE_REVIEW_INVALID", "pitfalls 必须包含 1 到 30 项。", status_code=422)
        pitfalls = [self._normalize_pitfall(item, index=index, source_ids=source_ids) for index, item in enumerate(raw_pitfalls)]
        title = _text(value.get("title"), 240)
        summary_zh = _text(value.get("summary_zh"), 4000)
        if not title or not summary_zh:
            raise PlatformError("EXPERIENCE_REVIEW_INVALID", "经验提炼需要 title 和 summary_zh。", status_code=422)
        normalized = {
            "schema_version": "execution_experience_review/v1",
            "review_id": f"review_{sha256_json({'sources': source_refs, 'pitfalls': pitfalls})[:16]}",
            "title": title,
            "summary_zh": summary_zh,
            "source_refs": source_refs,
            "environment_comparison": self._normalize_environment_comparison(value.get("environment_comparison", [])),
            "pitfalls": pitfalls,
            "review_status": "captured_unreviewed",
            "agent_run_id": _text(agent_run_id, 160) or None,
            "promotion_policy": {
                "automatic_skill_update": False,
                "automatic_environment_change": False,
                "user_resolution_selection_required": True,
                "verification_required_before_generalization": True,
            },
        }
        artifact = self.artifact_store.write(
            "execution_experience_review",
            normalized,
            source_connector_id="platform-history",
            source_binding={"match_level": "historical_execution_sources", "source_refs": source_refs},
            producer={"name": "execution-pitfall-distillation", "version": "1.0.0"},
            schema_version="1.0",
        )
        now = utc_now()
        with self._thread_lock, file_lock(self.lock_path):
            library = self._read_unlocked()
            reviews = library["reviews"]
            if not any(_record(item).get("review_id") == normalized["review_id"] for item in reviews):
                reviews.append({
                    "review_id": normalized["review_id"],
                    "artifact_id": artifact["artifact_id"],
                    "title": title,
                    "summary_zh": summary_zh,
                    "source_refs": source_refs,
                    "pitfall_count": len(pitfalls),
                    "created_at": artifact["created_at"],
                })
            cases = library["cases"]
            case_ids: list[str] = []
            for pitfall in pitfalls:
                case_id = f"pit_{sha256_json({'recurrence_key': pitfall['recurrence_key']})[:16]}"
                case_ids.append(case_id)
                previous = _record(cases.get(case_id))
                observations = previous.get("observations") if isinstance(previous.get("observations"), list) else []
                observation = {
                    "review_id": normalized["review_id"],
                    "artifact_id": artifact["artifact_id"],
                    "source_refs": source_refs,
                    "observed_at": now,
                }
                if not any(_record(item).get("review_id") == normalized["review_id"] for item in observations):
                    observations.append(observation)
                cases[case_id] = {
                    **previous,
                    **pitfall,
                    "case_id": case_id,
                    "status": previous.get("status") if previous.get("status") in CASE_STATUSES else "observed",
                    "selected_resolution_id": previous.get("selected_resolution_id"),
                    "operator_note": previous.get("operator_note", ""),
                    "created_at": previous.get("created_at") or now,
                    "updated_at": now,
                    "observations": observations[-50:],
                    "observation_count": len(observations),
                    "latest_review_id": normalized["review_id"],
                }
            self._write_unlocked(library)
        return {
            "status": "succeeded",
            "review_id": normalized["review_id"],
            "review_artifact_id": artifact["artifact_id"],
            "case_ids": case_ids,
            "pitfall_count": len(pitfalls),
            "summary_zh": "经验候选已保存，等待用户选择处置方案；尚未自动修改环境或 Skill。",
        }

    def catalog(self) -> dict[str, Any]:
        with self._thread_lock, file_lock(self.lock_path):
            library = self._read_unlocked()
        cases = [item for item in library["cases"].values() if isinstance(item, dict)]
        cases.sort(key=lambda item: str(item.get("updated_at") or ""), reverse=True)
        reviews = [item for item in library["reviews"] if isinstance(item, dict)]
        reviews.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return {
            "schema_version": "experience_evolution_catalog/v1",
            "revision": int(library.get("revision") or 0),
            "updated_at": library.get("updated_at"),
            "case_count": len(cases),
            "review_count": len(reviews),
            "status_summary": {
                status: sum(1 for item in cases if item.get("status") == status)
                for status in sorted(CASE_STATUSES)
            },
            "cases": cases,
            "reviews": reviews,
            "resolution_kinds": sorted(RESOLUTION_KINDS),
        }

    def read_case(self, case_id: str) -> dict[str, Any]:
        if not CASE_ID_PATTERN.fullmatch(case_id):
            raise PlatformError("EXPERIENCE_CASE_NOT_FOUND", "经验案例标识无效。", status_code=404)
        catalog = self.catalog()
        for item in catalog["cases"]:
            if item.get("case_id") == case_id:
                return {"schema_version": "experience_case_detail/v1", "case": item}
        raise PlatformError("EXPERIENCE_CASE_NOT_FOUND", "经验案例不存在。", status_code=404)

    def triage_case(
        self,
        case_id: str,
        *,
        status: str,
        selected_resolution_id: str = "",
        operator_note: str = "",
        actor: str = "web_ui",
    ) -> dict[str, Any]:
        if not CASE_ID_PATTERN.fullmatch(case_id):
            raise PlatformError("EXPERIENCE_CASE_NOT_FOUND", "经验案例标识无效。", status_code=404)
        status = _text(status, 40)
        selected_resolution_id = _text(selected_resolution_id, 80)
        operator_note = _text(operator_note, 4000)
        if status not in CASE_STATUSES:
            raise PlatformError("EXPERIENCE_CASE_STATUS_INVALID", "经验案例状态不受支持。", status_code=422)
        with self._thread_lock, file_lock(self.lock_path):
            library = self._read_unlocked()
            case = _record(library["cases"].get(case_id))
            if not case:
                raise PlatformError("EXPERIENCE_CASE_NOT_FOUND", "经验案例不存在。", status_code=404)
            resolution_ids = {
                _text(_record(item).get("resolution_id"), 80)
                for item in case.get("resolution_options", [])
                if isinstance(item, dict)
            }
            if selected_resolution_id and selected_resolution_id not in resolution_ids:
                raise PlatformError("EXPERIENCE_RESOLUTION_NOT_FOUND", "所选处置方案不存在。", status_code=422)
            now = utc_now()
            decision_payload = {
                "schema_version": "experience_triage_decision/v1",
                "decision_id": f"decision_{secrets.token_hex(8)}",
                "case_id": case_id,
                "status": status,
                "selected_resolution_id": selected_resolution_id or None,
                "operator_note": operator_note,
                "actor": _text(actor, 80) or "web_ui",
                "decided_at": now,
            }
            artifact = self.artifact_store.write(
                "experience_triage_decision",
                decision_payload,
                source_connector_id="platform-history",
                source_binding={"match_level": "experience_case", "case_id": case_id},
                producer={"name": "experience-evolution-workbench", "version": "1.0.0"},
                schema_version="1.0",
            )
            decisions = case.get("decisions") if isinstance(case.get("decisions"), list) else []
            decisions.append({**decision_payload, "artifact_id": artifact["artifact_id"]})
            case.update({
                "status": status,
                "selected_resolution_id": selected_resolution_id or None,
                "operator_note": operator_note,
                "updated_at": now,
                "decisions": decisions[-100:],
            })
            library["cases"][case_id] = case
            self._write_unlocked(library)
        return {
            "status": "succeeded",
            "case": case,
            "decision_artifact_id": artifact["artifact_id"],
        }
