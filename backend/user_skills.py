"""Durable user-authored Skill versions.

Server Skills live in source control and are immutable at runtime. This store
keeps user edits in the mutable state root, with one explicitly active version
per Skill. Saving always appends a version; it never rewrites server assets or
an older user version.
"""

from __future__ import annotations

import json
import re
import secrets
import threading
from pathlib import Path
from typing import Any

from .core import PlatformError, atomic_write_json, file_lock, sha256_json, utc_now


SKILL_ID_PATTERN = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
PROMPT_SLOT_PATTERN = re.compile(r"^[a-z][a-z0-9_:-]{0,79}$")
USER_VERSION_ID_PATTERN = re.compile(r"^usr_[a-z0-9]{12,40}$")
MAX_SKILL_CONTENT_CHARS = 64 * 1024
MAX_VERSIONS_PER_SKILL = 200


def _text(value: Any, limit: int) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


class UserSkillStore:
    """Atomically append and select user Skill versions."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self.path = self.root / "library.json"
        self.lock_path = self.root / ".user-skill-library.lock"
        self._thread_lock = threading.RLock()

    @staticmethod
    def _empty() -> dict[str, Any]:
        return {
            "schema_version": "user_skill_library/v1",
            "revision": 0,
            "updated_at": None,
            "skills": {},
        }

    def _read_unlocked(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._empty()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PlatformError(
                "USER_SKILL_LIBRARY_CORRUPT",
                "用户 Skill 仓无法读取。",
                status_code=500,
                details={"path": str(self.path), "reason": str(exc)},
            ) from exc
        if not isinstance(value, dict) or value.get("schema_version") != "user_skill_library/v1":
            raise PlatformError(
                "USER_SKILL_LIBRARY_CORRUPT",
                "用户 Skill 仓的数据结构无效。",
                status_code=500,
                details={"path": str(self.path)},
            )
        if not isinstance(value.get("skills"), dict):
            value["skills"] = {}
        return value

    def read(self) -> dict[str, Any]:
        with self._thread_lock, file_lock(self.lock_path):
            return self._read_unlocked()

    def _write_unlocked(self, value: dict[str, Any]) -> None:
        value["revision"] = max(0, int(value.get("revision") or 0)) + 1
        value["updated_at"] = utc_now()
        atomic_write_json(self.path, value)

    @staticmethod
    def _skill_record(library: dict[str, Any], skill_id: str) -> dict[str, Any] | None:
        skills = library.get("skills") if isinstance(library.get("skills"), dict) else {}
        record = skills.get(skill_id)
        return record if isinstance(record, dict) else None

    @staticmethod
    def _versions(record: dict[str, Any]) -> list[dict[str, Any]]:
        value = record.get("versions")
        return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []

    def active_snapshot(self) -> tuple[int, dict[str, dict[str, Any]]]:
        library = self.read()
        result: dict[str, dict[str, Any]] = {}
        skills = library.get("skills") if isinstance(library.get("skills"), dict) else {}
        for skill_id, raw_record in skills.items():
            if not isinstance(raw_record, dict):
                continue
            active_id = _text(raw_record.get("active_version_id"), 80)
            if not active_id:
                continue
            for version in self._versions(raw_record):
                if version.get("version_id") == active_id:
                    result[str(skill_id)] = dict(version)
                    break
        return int(library.get("revision") or 0), result

    def active_versions(self) -> dict[str, dict[str, Any]]:
        return self.active_snapshot()[1]

    def get_version(self, skill_id: str, version_id: str | None = None) -> dict[str, Any]:
        skill_id = _text(skill_id, 64)
        if not SKILL_ID_PATTERN.fullmatch(skill_id):
            raise PlatformError("USER_SKILL_ID_INVALID", "Skill ID 只能使用小写字母、数字和连字符。", status_code=422)
        library = self.read()
        record = self._skill_record(library, skill_id)
        if record is None:
            raise PlatformError("USER_SKILL_NOT_FOUND", "用户 Skill 仓中不存在该 Skill。", status_code=404, details={"skill_id": skill_id})
        selected = _text(version_id, 80) or _text(record.get("active_version_id"), 80)
        for version in self._versions(record):
            if version.get("version_id") == selected:
                return dict(version)
        raise PlatformError(
            "USER_SKILL_VERSION_NOT_FOUND",
            "用户 Skill 版本不存在。",
            status_code=404,
            details={"skill_id": skill_id, "version_id": selected or None},
        )

    def save_version(
        self,
        *,
        skill_id: str,
        label_zh: str,
        description_zh: str,
        content: str,
        prompt_slot: str,
        change_summary_zh: str = "",
        created_by: str = "web_ui",
        based_on: dict[str, Any] | None = None,
        activate: bool = True,
    ) -> dict[str, Any]:
        skill_id = _text(skill_id, 64)
        label_zh = _text(label_zh, 120)
        description_zh = _text(description_zh, 800)
        prompt_slot = _text(prompt_slot, 80) or f"custom:{skill_id}"
        change_summary_zh = _text(change_summary_zh, 800)
        created_by = _text(created_by, 40) or "web_ui"
        if not SKILL_ID_PATTERN.fullmatch(skill_id):
            raise PlatformError("USER_SKILL_ID_INVALID", "Skill ID 只能使用小写字母、数字和连字符，长度为 2–64。", status_code=422)
        if not label_zh:
            raise PlatformError("USER_SKILL_LABEL_REQUIRED", "Skill 名称不能为空。", status_code=422)
        if not PROMPT_SLOT_PATTERN.fullmatch(prompt_slot):
            raise PlatformError("USER_SKILL_PROMPT_SLOT_INVALID", "Skill prompt slot 格式无效。", status_code=422)
        if not isinstance(content, str) or not content.strip():
            raise PlatformError("USER_SKILL_CONTENT_REQUIRED", "Skill 内容不能为空。", status_code=422)
        if len(content) > MAX_SKILL_CONTENT_CHARS:
            raise PlatformError(
                "USER_SKILL_CONTENT_TOO_LARGE",
                "Skill 内容超过 64 KiB 上限。",
                status_code=413,
                details={"content_chars": len(content), "max_content_chars": MAX_SKILL_CONTENT_CHARS},
            )
        if "\x00" in content:
            raise PlatformError("USER_SKILL_CONTENT_INVALID", "Skill 内容不能包含 NUL 字符。", status_code=422)

        now = utc_now()
        with self._thread_lock, file_lock(self.lock_path):
            library = self._read_unlocked()
            skills = library.setdefault("skills", {})
            record = skills.get(skill_id)
            if not isinstance(record, dict):
                record = {"skill_id": skill_id, "active_version_id": None, "versions": []}
                skills[skill_id] = record
            versions = self._versions(record)
            if len(versions) >= MAX_VERSIONS_PER_SKILL:
                raise PlatformError(
                    "USER_SKILL_VERSION_LIMIT",
                    "该 Skill 的自编辑版本已达到上限，请先整理历史版本。",
                    status_code=409,
                    details={"skill_id": skill_id, "max_versions": MAX_VERSIONS_PER_SKILL},
                )
            sequence = len(versions) + 1
            version_id = f"usr_{secrets.token_hex(8)}"
            compact_time = now.replace("-", "").replace(":", "").replace("T", ".").replace("Z", "")
            semantic_version = f"user.{compact_time}.{sequence}"
            normalized_based_on = {
                key: value
                for key, value in (based_on or {}).items()
                if key in {"origin", "profile_id", "version", "version_id", "content_sha256"}
                and isinstance(value, str)
                and value
            }
            version = {
                "version_id": version_id,
                "version": semantic_version,
                "skill_id": skill_id,
                "label_zh": label_zh,
                "description_zh": description_zh,
                "prompt_slot": prompt_slot,
                "prompt_mode": "replace",
                "content": content.strip(),
                "content_chars": len(content.strip()),
                "content_sha256": sha256_json({"skill_id": skill_id, "content": content.strip()}),
                "origin": "user",
                "created_at": now,
                "created_by": created_by,
                "change_summary_zh": change_summary_zh,
                "based_on": normalized_based_on,
            }
            versions.append(version)
            record.update({
                "skill_id": skill_id,
                "label_zh": label_zh,
                "description_zh": description_zh,
                "versions": versions,
            })
            if activate:
                record["active_version_id"] = version_id
            self._write_unlocked(library)
            return dict(version) | {"active": activate, "library_revision": library["revision"]}

    def activate(self, skill_id: str, version_id: str | None) -> dict[str, Any]:
        skill_id = _text(skill_id, 64)
        if not SKILL_ID_PATTERN.fullmatch(skill_id):
            raise PlatformError("USER_SKILL_ID_INVALID", "Skill ID 格式无效。", status_code=422)
        selected = _text(version_id, 80)
        with self._thread_lock, file_lock(self.lock_path):
            library = self._read_unlocked()
            record = self._skill_record(library, skill_id)
            if record is None:
                if selected:
                    raise PlatformError("USER_SKILL_NOT_FOUND", "用户 Skill 仓中不存在该 Skill。", status_code=404)
                return {"skill_id": skill_id, "active_version_id": None, "origin": "server", "library_revision": int(library.get("revision") or 0)}
            if selected and not any(version.get("version_id") == selected for version in self._versions(record)):
                raise PlatformError("USER_SKILL_VERSION_NOT_FOUND", "用户 Skill 版本不存在。", status_code=404)
            record["active_version_id"] = selected or None
            self._write_unlocked(library)
            return {
                "skill_id": skill_id,
                "active_version_id": selected or None,
                "origin": "user" if selected else "server",
                "library_revision": library["revision"],
            }

    def catalog(self) -> dict[str, Any]:
        library = self.read()
        skills = library.get("skills") if isinstance(library.get("skills"), dict) else {}
        records: list[dict[str, Any]] = []
        for skill_id, raw_record in sorted(skills.items()):
            if not isinstance(raw_record, dict):
                continue
            active_id = _text(raw_record.get("active_version_id"), 80)
            versions = self._versions(raw_record)
            records.append({
                "skill_id": skill_id,
                "label_zh": _text(raw_record.get("label_zh"), 120) or skill_id,
                "description_zh": _text(raw_record.get("description_zh"), 800),
                "active_version_id": active_id or None,
                "versions": [
                    {
                        key: value
                        for key, value in version.items()
                        if key != "content"
                    } | {"active": version.get("version_id") == active_id}
                    for version in reversed(versions)
                ],
            })
        return {
            "schema_version": "user_skill_library/v1",
            "revision": int(library.get("revision") or 0),
            "updated_at": library.get("updated_at"),
            "skill_count": len(records),
            "skills": records,
        }
