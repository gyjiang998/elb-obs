"""Append-only observability traces for one browser analysis record.

The immutable artifact store remains the source of evidence.  This module
records orchestration facts that span model requests, browser actions, tools,
and contract handling so a failed run can be reconstructed without logging
browser-held credentials.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .core import PlatformError, file_lock, sha256_json, utc_now


ANALYSIS_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,119}")
MAX_EVENT_BYTES = 2 * 1024 * 1024
MAX_TRACE_BYTES = 128 * 1024 * 1024
SECRET_FIELD_PATTERN = re.compile(
    r"(?:^|_)(?:api_?key|authorization|password|secret|credential|access_?key|pairing_?code)(?:$|_)",
    re.IGNORECASE,
)


def _validate_analysis_id(analysis_id: str) -> str:
    normalized = analysis_id.strip()
    if not ANALYSIS_ID_PATTERN.fullmatch(normalized):
        raise PlatformError(
            "ANALYSIS_TRACE_ID_INVALID",
            "分析记录标识不符合可观测 Trace 的安全格式。",
            status_code=422,
            details={"analysis_id": normalized[:120]},
        )
    return normalized


def _redact(value: Any, *, depth: int = 0) -> Any:
    if depth > 24:
        return "[超过可观测载荷嵌套深度]"
    if isinstance(value, dict):
        output: dict[str, Any] = {}
        for raw_key, item in value.items():
            key = str(raw_key)
            output[key] = "[已脱敏]" if SECRET_FIELD_PATTERN.search(key) else _redact(item, depth=depth + 1)
        return output
    if isinstance(value, list):
        return [_redact(item, depth=depth + 1) for item in value]
    if isinstance(value, tuple):
        return [_redact(item, depth=depth + 1) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


class AnalysisTraceStore:
    """Persist one ordered JSONL trace per browser analysis record."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._thread_lock = threading.RLock()

    def _path(self, analysis_id: str) -> Path:
        return self.root / f"{_validate_analysis_id(analysis_id)}.jsonl"

    def append(self, analysis_id: str, event: dict[str, Any]) -> dict[str, Any]:
        normalized_id = _validate_analysis_id(analysis_id)
        path = self._path(normalized_id)
        payload = _redact(event.get("payload") if isinstance(event.get("payload"), dict) else {})
        row = {
            "schema_version": "analysis_execution_trace_event/v1",
            "analysis_id": normalized_id,
            "event_id": str(event.get("event_id") or ""),
            "occurred_at": str(event.get("occurred_at") or utc_now()),
            "received_at": utc_now(),
            "category": str(event.get("category") or "workflow"),
            "operation": str(event.get("operation") or ""),
            "phase": str(event.get("phase") or ""),
            "status": str(event.get("status") or "observed"),
            "request_id": str(event.get("request_id") or ""),
            "run_id": str(event.get("run_id") or ""),
            "turn": event.get("turn") if isinstance(event.get("turn"), int) else None,
            "client_sequence": event.get("client_sequence") if isinstance(event.get("client_sequence"), int) else None,
            "payload": payload,
            "payload_sha256": sha256_json(payload),
        }
        encoded = (json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > MAX_EVENT_BYTES:
            raise PlatformError(
                "ANALYSIS_TRACE_EVENT_TOO_LARGE",
                "单条可观测 Trace 事件超过 2 MiB 上限。",
                status_code=413,
                details={"event_bytes": len(encoded), "max_event_bytes": MAX_EVENT_BYTES},
            )
        lock_path = path.with_suffix(path.suffix + ".lock")
        with self._thread_lock, file_lock(lock_path):
            existing_size = path.stat().st_size if path.exists() else 0
            if existing_size + len(encoded) > MAX_TRACE_BYTES:
                raise PlatformError(
                    "ANALYSIS_TRACE_TOO_LARGE",
                    "本次分析的可观测 Trace 已达到 128 MiB 上限。",
                    status_code=413,
                    details={"trace_bytes": existing_size, "max_trace_bytes": MAX_TRACE_BYTES},
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("ab") as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        return {
            "analysis_id": normalized_id,
            "trace_id": f"analysis-trace:{normalized_id}",
            "event_id": row["event_id"],
            "relative_path": str(path.relative_to(self.root.parent.parent)),
            "event_sha256": sha256_json(row),
        }

    def read(self, analysis_id: str, *, limit: int = 5000) -> dict[str, Any]:
        normalized_id = _validate_analysis_id(analysis_id)
        path = self._path(normalized_id)
        if not path.is_file():
            return {
                "analysis_id": normalized_id,
                "trace_id": f"analysis-trace:{normalized_id}",
                "relative_path": str(path.relative_to(self.root.parent.parent)),
                "event_count": 0,
                "events": [],
            }
        events: list[dict[str, Any]] = []
        with self._thread_lock, file_lock(path.with_suffix(path.suffix + ".lock")):
            for line in path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict):
                    events.append(item)
        events.sort(
            key=lambda item: (
                str(item.get("occurred_at") or ""),
                item.get("client_sequence") if isinstance(item.get("client_sequence"), int) else 0,
                str(item.get("received_at") or ""),
            )
        )
        bounded = events[-max(1, min(limit, 5000)) :]
        return {
            "analysis_id": normalized_id,
            "trace_id": f"analysis-trace:{normalized_id}",
            "relative_path": str(path.relative_to(self.root.parent.parent)),
            "event_count": len(events),
            "returned_event_count": len(bounded),
            "file_bytes": path.stat().st_size,
            "events": bounded,
        }

    def catalog(self, *, limit: int = 100, order_by: str = "updated_at") -> list[dict[str, Any]]:
        """List trace identities and sizes, with the first event as creation time."""

        rows: list[dict[str, Any]] = []
        bounded = max(1, min(int(limit), 500))
        with self._thread_lock:
            for path in self.root.glob("*.jsonl"):
                analysis_id = path.stem
                if not ANALYSIS_ID_PATTERN.fullmatch(analysis_id):
                    continue
                try:
                    stat = path.stat()
                    event_count = 0
                    first_event_at = ""
                    with path.open("rb") as handle:
                        for line in handle:
                            event_count += 1
                            if first_event_at:
                                continue
                            try:
                                first_event = json.loads(line)
                            except (json.JSONDecodeError, UnicodeDecodeError):
                                continue
                            if isinstance(first_event, dict):
                                first_event_at = str(
                                    first_event.get("received_at") or first_event.get("occurred_at") or ""
                                )
                except OSError:
                    continue
                rows.append({
                    "analysis_id": analysis_id,
                    "event_count": event_count,
                    "file_bytes": stat.st_size,
                    "created_at": first_event_at or datetime.fromtimestamp(
                        getattr(stat, "st_birthtime", stat.st_ctime),
                        tz=timezone.utc,
                    ).isoformat().replace("+00:00", "Z"),
                    "updated_at": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc).isoformat().replace("+00:00", "Z"),
                })
        sort_field = "created_at" if order_by == "created_at" else "updated_at"
        rows.sort(key=lambda item: str(item.get(sort_field) or ""), reverse=True)
        return rows[:bounded]

    def download_path(self, analysis_id: str) -> Path:
        normalized_id = _validate_analysis_id(analysis_id)
        path = self._path(normalized_id)
        if not path.is_file():
            raise PlatformError(
                "ANALYSIS_TRACE_NOT_FOUND",
                "此分析记录尚未生成可观测 Trace 文件。",
                status_code=404,
                details={"analysis_id": normalized_id},
            )
        return path
