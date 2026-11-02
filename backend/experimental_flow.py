"""Isolated evidence and result assets for the two flow Playgrounds.

The Playgrounds intentionally publish free-form Mermaid instead of the
application-flow scenario contracts.  This module owns only a small storage
envelope, source/evidence binding, and browser-safe rendering checks.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .core import ArtifactStore, JsonRegistry, PlatformError, SourceConnectorManager, utc_now


EVIDENCE_ARTIFACT_TYPE = "experimental_flow_evidence"
RESULT_ARTIFACT_TYPE = "experimental_flow_result"
ANALYSIS_KINDS = frozenset({"hiperf", "trace"})
_ARTIFACT_ID_RE = re.compile(r"^art_[a-f0-9]{24}$")
_UNSAFE_MERMAID_RE = re.compile(r"(?i)(?:<script|javascript:|\bclick\s+\S+|\bhref\s+|%%\{init)")


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any, maximum: int) -> str:
    return value.strip()[:maximum] if isinstance(value, str) else ""


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


class ExperimentalFlowManager:
    """Store Playground inputs and Agent-authored free-form diagrams."""

    def __init__(
        self,
        state_root: Path,
        connectors: SourceConnectorManager,
        artifact_store: ArtifactStore,
    ) -> None:
        self.state_root = state_root.resolve()
        self.connectors = connectors
        self.artifact_store = artifact_store
        self.registry = JsonRegistry(
            self.state_root / "experimental-flows" / "registry.json",
            {"schema_version": "experimental_flow_registry/v1", "deleted_artifacts": {}},
        )

    @staticmethod
    def _kind(value: str) -> str:
        kind = value.strip().casefold()
        if kind not in ANALYSIS_KINDS:
            raise PlatformError(
                "EXPERIMENTAL_FLOW_KIND_INVALID",
                "Playground 分析类型只能为 hiperf 或 trace。",
                status_code=422,
                details={"analysis_kind": value},
            )
        return kind

    @staticmethod
    def _binding(connector: dict[str, Any]) -> dict[str, Any]:
        snapshot = _record(connector.get("snapshot"))
        snapshot_id = str(snapshot.get("snapshot_id") or "")
        fingerprint = _record(connector.get("worktree_fingerprint"))
        return {
            "declared_revision": snapshot_id or None,
            "resolved_snapshot_id": snapshot_id or None,
            "match_level": "exact" if snapshot.get("clean") else "compatible",
            "resolver_version": "experimental-flow/1.0",
            "snapshot_state": "clean" if snapshot.get("clean") else "dirty",
            "worktree_fingerprint_sha256": fingerprint.get("sha256"),
        }

    def _source_scopes(
        self,
        *,
        primary_connector_id: str,
        primary_subpath: str,
        source_scopes: list[dict[str, Any]] | None,
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        raw_scopes = [item for item in (source_scopes or []) if isinstance(item, dict)]
        primary_raw = next(
            (item for item in raw_scopes if _text(item.get("source_connector_id"), 160) == primary_connector_id),
            None,
        )
        ordered = [
            {
                "source_connector_id": primary_connector_id,
                "subpath": _text((primary_raw or {}).get("subpath"), 2048) or primary_subpath,
            },
            *[
                item
                for item in raw_scopes
                if _text(item.get("source_connector_id"), 160) != primary_connector_id
            ],
        ]
        if len(ordered) > 12:
            raise PlatformError(
                "EXPERIMENTAL_FLOW_SOURCE_SCOPE_LIMIT",
                "Playground 一次最多分析 12 个代码仓范围。",
                status_code=422,
            )

        normalized: list[dict[str, Any]] = []
        bindings: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw in ordered:
            connector_id = _text(raw.get("source_connector_id"), 160)
            if not connector_id or connector_id in seen:
                continue
            connector = self.connectors.get(connector_id)
            subpath = _text(raw.get("subpath"), 2048).replace("\\", "/").removeprefix("./").rstrip("/")
            source_root = Path(str(connector.get("source_root") or "")).resolve()
            scope_path = (source_root / subpath).resolve() if subpath else source_root
            if not _is_relative_to(scope_path, source_root) or not scope_path.exists():
                raise PlatformError(
                    "EXPERIMENTAL_FLOW_SOURCE_SCOPE_INVALID",
                    "代码仓内不存在所选 Playground 分析范围。",
                    status_code=422,
                    details={"source_connector_id": connector_id, "subpath": subpath},
                )
            snapshot_id = str(_record(connector.get("snapshot")).get("snapshot_id") or "")
            normalized.append(
                {
                    "source_connector_id": connector_id,
                    "source_snapshot_id": snapshot_id,
                    "subpath": subpath,
                    "role": "primary" if connector_id == primary_connector_id else "related",
                }
            )
            bindings.append({"source_connector_id": connector_id, **self._binding(connector)})
            seen.add(connector_id)
        return normalized, bindings

    def _deleted_ids(self) -> set[str]:
        return set(_record(self.registry.read().get("deleted_artifacts")))

    def _ensure_active(self, artifact_id: str) -> None:
        if artifact_id in self._deleted_ids():
            raise PlatformError(
                "EXPERIMENTAL_FLOW_ARTIFACT_NOT_FOUND",
                "Playground 工件不存在。",
                status_code=404,
                details={"artifact_id": artifact_id},
            )

    def register_evidence(
        self,
        *,
        analysis_kind: str,
        source_connector_id: str,
        managed_path: str,
        file_name: str,
        content_sha256: str,
        size_bytes: int,
        media_type: str = "application/octet-stream",
    ) -> dict[str, Any]:
        kind = self._kind(analysis_kind)
        connector = self.connectors.get(source_connector_id)
        path = (self.state_root / managed_path).resolve()
        if not _is_relative_to(path, self.state_root) or not path.is_file():
            raise PlatformError(
                "EXPERIMENTAL_FLOW_EVIDENCE_PATH_INVALID",
                "Playground 运行数据未落入平台托管目录。",
                status_code=422,
            )
        artifact = self.artifact_store.write(
            EVIDENCE_ARTIFACT_TYPE,
            {
                "analysis_kind": kind,
                "evidence_kind": "hiperf_samples" if kind == "hiperf" else "runtime_trace",
                "file": {
                    "name": Path(file_name).name[:180],
                    "size_bytes": max(0, int(size_bytes)),
                    "sha256": content_sha256,
                    "media_type": _text(media_type, 160) or "application/octet-stream",
                    "managed_path": path.relative_to(self.state_root).as_posix(),
                },
            },
            source_connector_id=source_connector_id,
            source_binding=self._binding(connector),
            producer={"name": "experimental-flow-evidence-ingestion", "version": "1.0.0"},
            input_artifacts=[f"sha256:{content_sha256}"],
            schema_version="experimental_flow_evidence/v1",
        )
        return artifact

    def _evidence_path(self, artifact: dict[str, Any]) -> Path:
        managed_path = _text(_record(artifact.get("file")).get("managed_path"), 4096)
        path = (self.state_root / managed_path).resolve()
        if not managed_path or not _is_relative_to(path, self.state_root) or not path.is_file():
            raise PlatformError(
                "EXPERIMENTAL_FLOW_EVIDENCE_MISSING",
                "Playground 运行数据文件不存在。",
                status_code=410,
                details={"artifact_id": artifact.get("artifact_id")},
            )
        return path

    @staticmethod
    def _preview(path: Path, maximum: int = 192 * 1024) -> tuple[str, bool]:
        data = path.read_bytes()[:maximum]
        if not data or b"\x00" in data:
            return "", False
        try:
            value = data.decode("utf-8")
        except UnicodeDecodeError:
            return "", False
        control_count = sum(ord(char) < 32 and char not in "\r\n\t" for char in value)
        if control_count > max(4, len(value) // 100):
            return "", False
        return value, path.stat().st_size > len(data)

    def read_evidence(self, artifact_id: str) -> dict[str, Any]:
        if not _ARTIFACT_ID_RE.fullmatch(artifact_id):
            raise PlatformError("EXPERIMENTAL_FLOW_ARTIFACT_ID_INVALID", "Playground 工件 ID 非法。", status_code=422)
        self._ensure_active(artifact_id)
        artifact = self.artifact_store.read(EVIDENCE_ARTIFACT_TYPE, artifact_id)
        path = self._evidence_path(artifact)
        preview, truncated = self._preview(path)
        return {
            "schema_version": "experimental_flow_evidence_access/v1",
            "evidence": artifact,
            "server_path": str(path),
            "text_preview": preview,
            "preview_truncated": truncated,
            "text_readable": bool(preview),
        }

    def list_evidence(
        self,
        *,
        analysis_kind: str,
        source_connector_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        kind = self._kind(analysis_kind)
        deleted = self._deleted_ids()
        return [
            item
            for item in self.artifact_store.list_artifacts(
                EVIDENCE_ARTIFACT_TYPE,
                limit=max(1, min(limit, 500)),
                source_connector_id=source_connector_id or None,
            )
            if item.get("artifact_id") not in deleted and item.get("analysis_kind") == kind
        ]

    def publish_result(
        self,
        *,
        analysis_kind: str,
        source_connector_id: str,
        flow_goal: str,
        title: str,
        mermaid: str,
        summary_markdown: str,
        evidence_artifact_id: str,
        subpath: str = "",
        source_scopes: list[dict[str, Any]] | None = None,
        parent_result_artifact_id: str = "",
        agent_run_id: str = "",
    ) -> dict[str, Any]:
        kind = self._kind(analysis_kind)
        connector = self.connectors.get(source_connector_id)
        goal = _text(flow_goal, 12_000)
        result_title = _text(title, 240)
        diagram = _text(mermaid, 512 * 1024)
        summary = _text(summary_markdown, 512 * 1024)
        if not goal or not result_title or not diagram or not summary:
            raise PlatformError(
                "EXPERIMENTAL_FLOW_RESULT_INVALID",
                "Playground 结果需要流程目标、标题、Mermaid 与分析说明。",
                status_code=422,
            )
        if _UNSAFE_MERMAID_RE.search(diagram):
            raise PlatformError(
                "EXPERIMENTAL_FLOW_MERMAID_UNSAFE",
                "Mermaid 包含浏览器不接受的交互或脚本指令。",
                status_code=422,
            )
        evidence = self.read_evidence(evidence_artifact_id)["evidence"]
        if evidence.get("analysis_kind") != kind:
            raise PlatformError(
                "EXPERIMENTAL_FLOW_EVIDENCE_KIND_MISMATCH",
                "运行数据类型与当前 Playground 不一致。",
                status_code=422,
            )
        normalized_scopes, source_bindings = self._source_scopes(
            primary_connector_id=source_connector_id,
            primary_subpath=subpath,
            source_scopes=source_scopes,
        )
        scope_connector_ids = {str(item.get("source_connector_id") or "") for item in normalized_scopes}
        if evidence.get("source_connector_id") not in scope_connector_ids:
            raise PlatformError(
                "EXPERIMENTAL_FLOW_SOURCE_MISMATCH",
                "运行数据必须绑定到本轮选择的代码仓之一。",
                status_code=422,
            )

        parent: dict[str, Any] | None = None
        revision = 1
        if parent_result_artifact_id:
            parent = self.read_result(parent_result_artifact_id)
            if parent.get("analysis_kind") != kind or parent.get("source_connector_id") != source_connector_id:
                raise PlatformError(
                    "EXPERIMENTAL_FLOW_PARENT_MISMATCH",
                    "父版本不属于当前 Playground 或代码仓。",
                    status_code=422,
                )
            revision = max(1, int(parent.get("revision") or 1)) + 1

        primary_scope = next(
            item for item in normalized_scopes if item.get("source_connector_id") == source_connector_id
        )

        input_artifacts = [evidence_artifact_id]
        if parent_result_artifact_id:
            input_artifacts.append(parent_result_artifact_id)
        return self.artifact_store.write(
            RESULT_ARTIFACT_TYPE,
            {
                "analysis_kind": kind,
                "title": result_title,
                "flow_goal": goal,
                "source_scope": {
                    "subpath": primary_scope["subpath"],
                },
                "source_scopes": normalized_scopes,
                "source_bindings": source_bindings,
                "evidence_artifact_id": evidence_artifact_id,
                "parent_result_artifact_id": parent_result_artifact_id or None,
                "revision": revision,
                "mermaid": diagram,
                "summary_markdown": summary,
                "agent_run_id": _text(agent_run_id, 160) or None,
            },
            source_connector_id=source_connector_id,
            source_binding=self._binding(connector),
            producer={"name": f"experimental-flow-{kind}-agent", "version": "1.0.0"},
            input_artifacts=input_artifacts,
            schema_version="experimental_flow_result/v2",
        )

    def read_result(self, artifact_id: str) -> dict[str, Any]:
        if not _ARTIFACT_ID_RE.fullmatch(artifact_id):
            raise PlatformError("EXPERIMENTAL_FLOW_ARTIFACT_ID_INVALID", "Playground 工件 ID 非法。", status_code=422)
        self._ensure_active(artifact_id)
        return self.artifact_store.read(RESULT_ARTIFACT_TYPE, artifact_id)

    def list_results(
        self,
        *,
        analysis_kind: str,
        source_connector_id: str = "",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        kind = self._kind(analysis_kind)
        deleted = self._deleted_ids()
        artifacts = self.artifact_store.list_artifacts(
            RESULT_ARTIFACT_TYPE,
            limit=500,
        )
        matching = [
            item
            for item in artifacts
            if item.get("artifact_id") not in deleted
            and item.get("analysis_kind") == kind
            and (
                not source_connector_id
                or item.get("source_connector_id") == source_connector_id
                or any(
                    _record(scope).get("source_connector_id") == source_connector_id
                    for scope in item.get("source_scopes", [])
                    if isinstance(scope, dict)
                )
            )
        ][: max(1, min(limit, 500))]
        return [
            {
                key: value
                for key, value in item.items()
                if key not in {"mermaid", "summary_markdown"}
            } | {
                "summary_excerpt": _text(item.get("summary_markdown"), 360),
                "has_mermaid": bool(_text(item.get("mermaid"), 512 * 1024)),
            }
            for item in matching
        ]

    def delete_result(self, artifact_id: str) -> dict[str, Any]:
        artifact = self.read_result(artifact_id)
        registry = self.registry.read()
        deleted = _record(registry.setdefault("deleted_artifacts", {}))
        deleted[artifact_id] = {
            "artifact_type": RESULT_ARTIFACT_TYPE,
            "deleted_at": utc_now(),
        }
        registry["deleted_artifacts"] = deleted
        self.registry.write(registry)
        return {
            "status": "deleted",
            "artifact_id": artifact_id,
            "analysis_kind": artifact.get("analysis_kind"),
        }
