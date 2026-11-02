"""Compatibility binding between an analysis record and a structured Run.

The interactive page Agent is page-scoped and does not use this registry:
multiple analysis records remain contexts inside one page-owned Kimi Session.
This index is retained for legacy structured inference, deep-link audit lookup,
and isolated blind-baseline calls that are intentionally analysis-scoped.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any

from .core import PlatformError, atomic_write_json, file_lock, utc_now


ANALYSIS_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")


def normalize_analysis_id(value: Any) -> str:
    analysis_id = str(value or "").strip()
    if not ANALYSIS_ID_PATTERN.fullmatch(analysis_id):
        raise PlatformError(
            "ANALYSIS_SESSION_ID_INVALID",
            "analysis_id 格式无效。",
            status_code=422,
        )
    return analysis_id


class AnalysisAgentSessionRegistry:
    """Small compatibility index from analysis ID to a structured Run."""

    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        self.lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        self._thread_lock = threading.RLock()

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"schema_version": "analysis_agent_sessions/v1", "sessions": {}}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PlatformError(
                "ANALYSIS_SESSION_REGISTRY_CORRUPT",
                "分析会话索引不可读取。",
                status_code=500,
                details={"path": str(self.path), "reason": str(exc)},
            ) from exc
        if not isinstance(value, dict) or not isinstance(value.get("sessions"), dict):
            raise PlatformError(
                "ANALYSIS_SESSION_REGISTRY_CORRUPT",
                "分析会话索引格式无效。",
                status_code=500,
                details={"path": str(self.path)},
            )
        return value

    def resolve(self, analysis_id: str) -> str | None:
        normalized = normalize_analysis_id(analysis_id)
        with self._thread_lock, file_lock(self.lock_path):
            record = self._read_unlocked()["sessions"].get(normalized)
            if not isinstance(record, dict):
                return None
            run_id = record.get("run_id")
            return str(run_id) if isinstance(run_id, str) and run_id else None

    def analysis_id_for_run(self, run_id: str) -> str | None:
        normalized_run_id = str(run_id or "").strip()
        with self._thread_lock, file_lock(self.lock_path):
            sessions = self._read_unlocked()["sessions"]
            for analysis_id, record in sessions.items():
                if isinstance(record, dict) and record.get("run_id") == normalized_run_id:
                    return str(analysis_id)
        return None

    def bind(self, analysis_id: str, run_id: str) -> dict[str, Any]:
        normalized = normalize_analysis_id(analysis_id)
        if not re.fullmatch(r"run_[a-f0-9]{24}", str(run_id or "")):
            raise PlatformError("ANALYSIS_SESSION_RUN_INVALID", "Agent Run ID 格式无效。", status_code=422)
        with self._thread_lock, file_lock(self.lock_path):
            value = self._read_unlocked()
            sessions = value["sessions"]
            prior_analysis_id = next(
                (
                    str(item_analysis_id)
                    for item_analysis_id, item in sessions.items()
                    if item_analysis_id != normalized
                    and isinstance(item, dict)
                    and item.get("run_id") == run_id
                ),
                None,
            )
            if prior_analysis_id:
                raise PlatformError(
                    "AGENT_RUN_ALREADY_BOUND",
                    "当前 Agent Run 已属于另一条优化分析记录。",
                    status_code=409,
                    details={"run_id": run_id, "analysis_id": prior_analysis_id},
                )
            existing = sessions.get(normalized)
            if isinstance(existing, dict) and existing.get("run_id") not in {None, run_id}:
                raise PlatformError(
                    "ANALYSIS_SESSION_ALREADY_BOUND",
                    "该分析记录已经绑定到另一个 Agent Run。",
                    status_code=409,
                    details={"analysis_id": normalized, "run_id": existing.get("run_id")},
                )
            record = {
                "analysis_id": normalized,
                "run_id": run_id,
                "created_at": existing.get("created_at") if isinstance(existing, dict) else utc_now(),
                "updated_at": utc_now(),
            }
            sessions[normalized] = record
            value["schema_version"] = "analysis_agent_sessions/v1"
            atomic_write_json(self.path, value)
            return dict(record)

    def forget(self, analysis_id: str, *, expected_run_id: str | None = None) -> None:
        normalized = normalize_analysis_id(analysis_id)
        with self._thread_lock, file_lock(self.lock_path):
            value = self._read_unlocked()
            existing = value["sessions"].get(normalized)
            if not isinstance(existing, dict):
                return
            if expected_run_id and existing.get("run_id") != expected_run_id:
                return
            value["sessions"].pop(normalized, None)
            atomic_write_json(self.path, value)
