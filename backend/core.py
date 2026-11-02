"""Durable local primitives shared by the Phase-A platform API.

The platform deliberately keeps its mutable state small and explicit: JSON
registries for connectors/plans and immutable JSON artifacts for evidence and
derived results.  This module is intentionally independent from FastAPI so it
can be tested without a running web server.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlparse

try:
    import fcntl
except ImportError:  # pragma: no cover - Phase A targets macOS/Linux.
    fcntl = None  # type: ignore[assignment]


GIB = 1024**3
CLONE_MIN_FREE_BYTES = 80 * GIB
CLONE_RESERVE_BYTES = 10 * GIB
CLONE_REQUIRED_TOTAL_BYTES = CLONE_MIN_FREE_BYTES + CLONE_RESERVE_BYTES


class PlatformError(Exception):
    """An API-safe failure with a stable error code."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.details = details or {}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_json_bytes(value: Any) -> bytes:
    """Encode JSON deterministically for hashing and idempotency keys."""

    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def sha256_file(path: Path, *, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(chunk_size):
            digest.update(chunk)
    return digest.hexdigest()


def _hashable_artifact_content(value: dict[str, Any]) -> dict[str, Any]:
    """Remove envelope fields whose values cannot affect cache identity."""

    content = dict(value)
    for field in ("artifact_id", "created_at", "content_sha256"):
        content.pop(field, None)
    return content


def fsync_directory(path: Path) -> None:
    """Persist directory metadata on POSIX; Windows CRT has no directory fd."""

    # Windows os.open(directory) reports EACCES even for writable directories.
    # File contents must still be flushed by the caller before atomic replace.
    # Avoid catching permission failures, which remain meaningful on POSIX.
    if sys.platform == "win32":
        return
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def atomic_write_json(path: Path, value: Any) -> None:
    """Sync the file, replace atomically, and sync the directory on POSIX."""

    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = json.dumps(value, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        fsync_directory(path.parent)
    finally:
        if temporary.exists():
            temporary.unlink(missing_ok=True)


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """A process-level lock for registry updates on POSIX platforms."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        if fcntl is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if fcntl is not None:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class JsonRegistry:
    """Small atomically-updated JSON state document."""

    def __init__(self, path: Path, default: dict[str, Any]) -> None:
        self.path = path
        self.default = default
        self.lock_path = path.with_suffix(path.suffix + ".lock")
        self._thread_lock = threading.RLock()

    def read(self) -> dict[str, Any]:
        with self._thread_lock, file_lock(self.lock_path):
            if not self.path.exists():
                return json.loads(json.dumps(self.default))
            try:
                value = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise PlatformError(
                    "ARTIFACT_STORE_CORRUPT",
                    "Local registry cannot be read.",
                    status_code=500,
                    details={"path": str(self.path), "reason": str(exc)},
                ) from exc
            if not isinstance(value, dict):
                raise PlatformError(
                    "ARTIFACT_STORE_CORRUPT",
                    "Local registry has an invalid root value.",
                    status_code=500,
                    details={"path": str(self.path)},
                )
            return value

    def write(self, value: dict[str, Any]) -> None:
        with self._thread_lock, file_lock(self.lock_path):
            atomic_write_json(self.path, value)


class ArtifactStore:
    """Content-addressed immutable JSON artifact storage for one local project."""

    def __init__(self, root: Path, *, project_id: str = "local-default") -> None:
        self.root = root.resolve()
        self.project_id = project_id
        self.artifact_root = self.root / "artifacts"
        self.event_log = self.root / "events" / "analysis_events.jsonl"
        self.lock_path = self.root / ".artifact-store.lock"
        self._thread_lock = threading.RLock()
        self.artifact_root.mkdir(parents=True, exist_ok=True)

    def write(
        self,
        artifact_type: str,
        payload: dict[str, Any],
        *,
        source_connector_id: str,
        source_binding: dict[str, Any],
        producer: dict[str, str],
        input_artifacts: list[str] | None = None,
        schema_version: str = "1.0",
    ) -> dict[str, Any]:
        if not re.fullmatch(r"[a-z][a-z0-9_]{1,80}", artifact_type):
            raise ValueError(f"unsafe artifact type: {artifact_type!r}")

        base: dict[str, Any] = {
            "artifact_type": artifact_type,
            "schema_version": schema_version,
            "project_id": self.project_id,
            "source_connector_id": source_connector_id,
            "source_binding": source_binding,
            "producer": producer,
            "input_artifacts": input_artifacts or [],
            **payload,
        }
        content_sha256 = sha256_json(_hashable_artifact_content(base))
        artifact_id = f"art_{content_sha256[:24]}"
        path = self.artifact_root / artifact_type / f"{artifact_id}.json"

        with self._thread_lock, file_lock(self.lock_path):
            if path.exists():
                return json.loads(path.read_text(encoding="utf-8"))
            artifact = {
                **base,
                "artifact_id": artifact_id,
                "created_at": utc_now(),
                "content_sha256": content_sha256,
            }
            atomic_write_json(path, artifact)
            self._append_event_unlocked(
                {
                    "event_type": "artifact_written",
                    "artifact_id": artifact_id,
                    "artifact_type": artifact_type,
                    "content_sha256": content_sha256,
                    "created_at": artifact["created_at"],
                }
            )
            return artifact

    def read(self, artifact_type: str, artifact_id: str) -> dict[str, Any]:
        if not re.fullmatch(r"art_[a-f0-9]{24}", artifact_id):
            raise PlatformError(
                "ARTIFACT_NOT_FOUND",
                "Requested artifact identifier is invalid.",
                status_code=404,
                details={"artifact_type": artifact_type, "artifact_id": artifact_id},
            )
        path = self.artifact_root / artifact_type / f"{artifact_id}.json"
        if not path.is_file():
            raise PlatformError(
                "ARTIFACT_NOT_FOUND",
                "Requested artifact does not exist.",
                status_code=404,
                details={"artifact_type": artifact_type, "artifact_id": artifact_id},
            )
        return json.loads(path.read_text(encoding="utf-8"))

    def list_artifacts(
        self,
        artifact_type: str,
        *,
        limit: int = 100,
        source_connector_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """List immutable artifacts newest first without exposing filesystem paths."""

        if not re.fullmatch(r"[a-z][a-z0-9_]{1,80}", artifact_type):
            raise ValueError(f"unsafe artifact type: {artifact_type!r}")
        root = self.artifact_root / artifact_type
        if not root.is_dir():
            return []
        artifacts: list[dict[str, Any]] = []
        for path in root.glob("art_*.json"):
            try:
                artifact = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(artifact, dict):
                continue
            if source_connector_id and artifact.get("source_connector_id") != source_connector_id:
                continue
            artifacts.append(artifact)
        artifacts.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
        return artifacts[: max(1, min(limit, 500))]

    def append_event(self, event: dict[str, Any]) -> None:
        with self._thread_lock, file_lock(self.lock_path):
            self._append_event_unlocked(event)

    def _append_event_unlocked(self, event: dict[str, Any]) -> None:
        self.event_log.parent.mkdir(parents=True, exist_ok=True)
        event = {"created_at": utc_now(), **event}
        with self.event_log.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=True, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def summary(self) -> dict[str, Any]:
        count = sum(1 for _ in self.artifact_root.rglob("*.json"))
        usage = shutil.disk_usage(self.root)
        return {
            "artifact_count": count,
            "root": str(self.root),
            "filesystem_free_bytes": usage.free,
        }


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _safe_slug(value: str, *, fallback: str = "repository") -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value).strip(".-").lower()
    return slug[:56] or fallback


def validate_git_url(url: str) -> str:
    """Accept only explicit HTTPS/SSH URLs; reject local and ambiguous forms."""

    value = url.strip()
    if not value or any(ord(char) < 32 for char in value):
        raise PlatformError("GIT_URL_REJECTED", "Git URL is empty or contains control characters.")
    parsed = urlparse(value)
    if parsed.scheme not in {"https", "ssh"} or not parsed.hostname:
        raise PlatformError(
            "GIT_URL_REJECTED",
            "Only explicit https:// and ssh:// Git URLs are allowed in Phase A.",
            details={"url": value},
        )
    if parsed.username and any(char in parsed.username for char in "\r\n"):
        raise PlatformError("GIT_URL_REJECTED", "Git URL has an invalid user component.")
    if not parsed.path or parsed.path in {"/", "/."}:
        raise PlatformError("GIT_URL_REJECTED", "Git URL must include a repository path.")
    return value


def clone_capacity(path: Path) -> dict[str, Any]:
    usage = shutil.disk_usage(path)
    return {
        "filesystem": str(path),
        "available_bytes": usage.free,
        "minimum_clone_bytes": CLONE_MIN_FREE_BYTES,
        "reserve_bytes": CLONE_RESERVE_BYTES,
        "required_total_bytes": CLONE_REQUIRED_TOTAL_BYTES,
        "can_clone": usage.free >= CLONE_REQUIRED_TOTAL_BYTES,
    }


class SourceConnectorManager:
    """Registers source roots and executes a narrowly-scoped managed Git clone."""

    def __init__(
        self,
        state_root: Path,
        artifact_store: ArtifactStore,
        *,
        workspace_root: Path,
    ) -> None:
        self.state_root = state_root.resolve()
        self.artifact_store = artifact_store
        self.workspace_root = workspace_root.resolve()
        self.managed_clone_root = self.state_root / "managed-clones"
        self.managed_clone_root.mkdir(parents=True, exist_ok=True)
        self.registry = JsonRegistry(self.state_root / "connectors.json", {"connectors": []})
        self.plan_registry = JsonRegistry(self.state_root / "clone_plans.json", {"plans": []})

    def list(self) -> list[dict[str, Any]]:
        return list(self.registry.read().get("connectors", []))

    def get(self, connector_id: str) -> dict[str, Any]:
        for connector in self.list():
            if connector.get("connector_id") == connector_id:
                return connector
        raise PlatformError(
            "SOURCE_CONNECTOR_NOT_FOUND",
            "Source connector does not exist.",
            status_code=404,
            details={"source_connector_id": connector_id},
        )

    def ensure_local_workspace(self, source_root: Path, *, display_name: str = "OpenHarmony oh workspace") -> dict[str, Any]:
        source_root = source_root.resolve()
        for connector in self.list():
            if connector.get("kind") == "local" and connector.get("source_root") == str(source_root):
                # Migrate the presentation name without creating a new source
                # snapshot every time /overview is requested.
                if display_name.strip() and connector.get("display_name") != display_name.strip():
                    updated = dict(connector)
                    updated["display_name"] = display_name.strip()
                    self._replace_connector(updated)
                    return updated
                return connector
        return self.register_local(source_root, display_name=display_name)

    def register_local(self, source_root: Path, *, display_name: str) -> dict[str, Any]:
        source_root = source_root.expanduser().resolve()
        if not source_root.is_dir():
            raise PlatformError("SOURCE_PATH_INVALID", "Local source root is not a directory.", details={"path": str(source_root)})
        if _is_relative_to(source_root, self.state_root):
            raise PlatformError("SOURCE_PATH_INVALID", "Managed artifact storage cannot be a source root.")
        existing = next(
            (
                connector
                for connector in self.list()
                if connector.get("kind") == "local" and connector.get("source_root") == str(source_root)
            ),
            None,
        )
        if existing is not None:
            return self.refresh_snapshot(str(existing["connector_id"]), display_name=display_name)
        snapshot = self._create_snapshot(source_root)
        worktree_fingerprint = self._create_worktree_fingerprint(source_root, snapshot)
        identity = sha256_json({"path": str(source_root), "snapshot_id": snapshot["snapshot_id"]})
        connector_id = f"local_{identity[:16]}"
        snapshot_artifact = self._write_snapshot_artifact(connector_id, snapshot, worktree_fingerprint)
        connector = {
            "connector_id": connector_id,
            "kind": "local",
            "mode": "local_readonly",
            "display_name": display_name.strip() or source_root.name,
            "source_root": str(source_root),
            "snapshot": snapshot,
            "worktree_fingerprint": worktree_fingerprint,
            "snapshot_artifact_id": snapshot_artifact["artifact_id"],
            "created_at": utc_now(),
            "remote_url": None,
            "pinned_commit": None,
        }
        state = self.registry.read()
        existing = [item for item in state.get("connectors", []) if item.get("connector_id") != connector["connector_id"]]
        existing.append(connector)
        self.registry.write({"connectors": existing})
        return connector

    def refresh_snapshot(self, connector_id: str, *, display_name: str = "") -> dict[str, Any]:
        """Capture the current local worktree without requiring a Git commit.

        The connector identity remains stable while its immutable snapshot
        artifact advances.  Existing trace artifacts still reference their old
        snapshot and will fail live verification when the worktree has changed.
        """

        connector = self.get(connector_id)
        if connector.get("kind") != "local":
            raise PlatformError("SOURCE_SNAPSHOT_REFRESH_UNSUPPORTED", "Only local source connectors can refresh a mutable worktree snapshot.", status_code=422)
        source_root = Path(str(connector["source_root"])).resolve()
        snapshot = self._create_snapshot(source_root)
        worktree_fingerprint = self._create_worktree_fingerprint(source_root, snapshot)
        snapshot_artifact = self._write_snapshot_artifact(connector_id, snapshot, worktree_fingerprint)
        updated = dict(connector)
        updated["snapshot"] = snapshot
        updated["worktree_fingerprint"] = worktree_fingerprint
        updated["snapshot_artifact_id"] = snapshot_artifact["artifact_id"]
        updated["updated_at"] = utc_now()
        if display_name.strip():
            updated["display_name"] = display_name.strip()
        self._replace_connector(updated)
        return updated

    def create_clone_plan(
        self,
        url: str,
        *,
        display_name: str = "",
        source_connector_id: str,
    ) -> dict[str, Any]:
        # A failed clone has no new connector to bind to, so retain the initiating
        # connector in the immutable plan for its action audit.
        self.get(source_connector_id)
        remote_url = validate_git_url(url)
        capacity = clone_capacity(self.managed_clone_root)
        parsed = urlparse(remote_url)
        repository_name = _safe_slug(Path(parsed.path.rstrip("/")).stem)
        plan = {
            "plan_id": f"cloneplan_{secrets.token_hex(12)}",
            "kind": "git_clone",
            "remote_url": remote_url,
            "source_connector_id": source_connector_id,
            "display_name": display_name.strip() or repository_name,
            "target_slug": repository_name,
            "capacity": capacity,
            "command_preview": [
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "protocol.file.allow=never",
                "clone",
                "--no-recurse-submodules",
                "--depth",
                "1",
                remote_url,
                "<managed-target>",
            ],
            "hard_gates": [
                "HTTPS or SSH URL only",
                "free space >= 80 GiB plus 10 GiB reserve",
                "temporary managed directory then atomic rename",
                "no submodules, hooks, package install, or build",
            ],
            "created_at": utc_now(),
            "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=15)).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
        state = self.plan_registry.read()
        plans = [item for item in state.get("plans", []) if item.get("expires_at", "") >= utc_now()]
        plans.append(plan)
        self.plan_registry.write({"plans": plans[-50:]})
        return plan

    def clone_from_plan(self, plan_id: str) -> dict[str, Any]:
        plan = next((item for item in self.plan_registry.read().get("plans", []) if item.get("plan_id") == plan_id), None)
        if plan is None or plan.get("expires_at", "") < utc_now():
            raise PlatformError("CLONE_PLAN_INVALID", "Clone plan is missing or expired.", status_code=409)
        capacity = clone_capacity(self.managed_clone_root)
        if not capacity["can_clone"]:
            raise PlatformError(
                "DISK_SPACE_INSUFFICIENT",
                "Managed clone requires at least 80 GiB free plus 10 GiB reserve.",
                status_code=409,
                details=capacity,
            )
        if shutil.which("git") is None:
            raise PlatformError("GIT_UNAVAILABLE", "git executable is not available.", status_code=500)

        target_name = f"{plan['target_slug']}-{secrets.token_hex(4)}"
        final_target = (self.managed_clone_root / target_name).resolve()
        if not _is_relative_to(final_target, self.managed_clone_root) or final_target.exists():
            raise PlatformError("CLONE_TARGET_INVALID", "Managed clone target is not available.", status_code=409)

        temporary_root = Path(tempfile.mkdtemp(prefix=".clone-", dir=self.managed_clone_root))
        staged_target = temporary_root / "repository"
        command = [
            "git",
            "-c",
            "core.hooksPath=/dev/null",
            "-c",
            "protocol.file.allow=never",
            "clone",
            "--no-recurse-submodules",
            "--depth",
            "1",
            plan["remote_url"],
            str(staged_target),
        ]
        result: subprocess.CompletedProcess[str] | None = None
        cleanup_result = "not_started"
        connector: dict[str, Any] | None = None
        try:
            result = subprocess.run(
                command,
                text=True,
                capture_output=True,
                timeout=20 * 60,
                check=False,
                env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"},
            )
            if result.returncode != 0:
                raise PlatformError(
                    "GIT_CLONE_FAILED",
                    "Git clone failed; see clone audit artifact for the output tail.",
                    status_code=502,
                    details={"returncode": result.returncode},
                )
            os.replace(staged_target, final_target)
            cleanup_result = "staged_target_promoted"
            head = subprocess.run(
                ["git", "-C", str(final_target), "rev-parse", "HEAD"],
                text=True,
                capture_output=True,
                timeout=30,
                check=True,
            ).stdout.strip()
            connector = self.register_local(final_target, display_name=plan["display_name"])
            connector["kind"] = "git_managed"
            connector["mode"] = "managed_clone_readonly"
            connector["remote_url"] = plan["remote_url"]
            connector["pinned_commit"] = head
            self._replace_connector(connector)
        except subprocess.TimeoutExpired as exc:
            raise PlatformError("GIT_CLONE_TIMEOUT", "Git clone exceeded the 20 minute time limit.", status_code=504) from exc
        finally:
            if temporary_root.exists():
                shutil.rmtree(temporary_root, ignore_errors=True)
                if cleanup_result == "not_started":
                    cleanup_result = "temporary_directory_removed"
            if connector is None and final_target.exists():
                shutil.rmtree(final_target, ignore_errors=True)
                cleanup_result = "final_target_removed_after_connector_failure"
            audit_status = "succeeded" if connector is not None else "failed"
            audit_connector_id = connector["connector_id"] if connector is not None else plan["source_connector_id"]
            audit = self._clone_audit(
                plan,
                capacity,
                result,
                cleanup_result,
                audit_status,
                source_connector_id=audit_connector_id,
            )
        if connector is None:
            raise PlatformError("GIT_CLONE_FAILED", "Git clone did not produce a source connector.", status_code=502)
        return {"connector": connector, "audit": audit}

    def resolve_binding(self, connector_id: str, declared_revision: str | None) -> dict[str, Any]:
        connector = self.get(connector_id)
        snapshot = connector.get("snapshot", {})
        snapshot_id = snapshot.get("snapshot_id")
        if not declared_revision:
            return self._unknown_binding(resolved_snapshot_id=snapshot_id)
        if declared_revision == snapshot_id and snapshot.get("clean"):
            return {
                "declared_revision": declared_revision,
                "resolved_snapshot_id": snapshot_id,
                "match_level": "exact",
                "resolver_version": "1.0",
            }
        if declared_revision == snapshot_id:
            return {
                "declared_revision": declared_revision,
                "resolved_snapshot_id": snapshot_id,
                "match_level": "compatible",
                "resolver_version": "1.0",
                "reason": "dirty_snapshot_requires_content_attestation",
            }
        return {
            "declared_revision": declared_revision,
            "resolved_snapshot_id": snapshot_id,
            "match_level": "mismatch",
            "resolver_version": "1.0",
        }

    def verify_current_snapshot(
        self,
        connector_id: str,
        expected_snapshot_id: str,
        *,
        expected_worktree_fingerprint_sha256: str | None = None,
    ) -> dict[str, Any]:
        """Check that a mutable local root still equals the artifact-bound snapshot.

        v1.1 snapshot IDs include dirty-file content.  The explicit aggregate
        fingerprint is retained in the trace binding as readable provenance and
        lets the verifier reject legacy v1.0 dirty snapshots as well.
        """

        connector = self.get(connector_id)
        source_root = Path(connector["source_root"])
        current = self._create_snapshot(source_root)
        current_worktree_fingerprint = self._create_worktree_fingerprint(source_root, current)
        snapshot_matches = current["snapshot_id"] == expected_snapshot_id
        fingerprint_matches = (
            expected_worktree_fingerprint_sha256 is None
            or current_worktree_fingerprint["sha256"] == expected_worktree_fingerprint_sha256
        )
        return {
            "expected_snapshot_id": expected_snapshot_id,
            "current_snapshot_id": current["snapshot_id"],
            "current_clean": current["clean"],
            "expected_worktree_fingerprint_sha256": expected_worktree_fingerprint_sha256,
            "current_worktree_fingerprint_sha256": current_worktree_fingerprint["sha256"],
            "worktree_fingerprint_matches": fingerprint_matches,
            "matches_expected": snapshot_matches and fingerprint_matches,
        }

    def capture_worktree_fingerprint(self, connector_id: str) -> dict[str, Any]:
        """Capture changed-file contents for a connector's registered snapshot."""

        connector = self.get(connector_id)
        snapshot = connector.get("snapshot") if isinstance(connector.get("snapshot"), dict) else {}
        return self._create_worktree_fingerprint(Path(connector["source_root"]), snapshot)

    def _create_worktree_fingerprint(self, source_root: Path, snapshot: dict[str, Any]) -> dict[str, Any]:
        repositories = snapshot.get("repositories") if isinstance(snapshot.get("repositories"), list) else []
        try:
            from .source_revision_snapshot import build_worktree_fingerprint

            return build_worktree_fingerprint(source_root, repositories=repositories)
        except Exception as exc:
            raise PlatformError(
                "SOURCE_WORKTREE_FINGERPRINT_FAILED",
                "Cannot capture the local worktree content fingerprint.",
                details={"source_root": str(source_root), "reason": str(exc)},
            ) from exc

    def _write_snapshot_artifact(
        self,
        connector_id: str,
        snapshot: dict[str, Any],
        worktree_fingerprint: dict[str, Any],
    ) -> dict[str, Any]:
        return self.artifact_store.write(
            "source_revision_snapshot",
            {"snapshot": snapshot, "worktree_fingerprint": worktree_fingerprint},
            source_connector_id=connector_id,
            source_binding={
                "declared_revision": snapshot["snapshot_id"],
                "resolved_snapshot_id": snapshot["snapshot_id"],
                "match_level": "exact",
                "resolver_version": "1.1",
                "snapshot_state": "clean" if snapshot["clean"] else "dirty",
                "worktree_fingerprint_algorithm": worktree_fingerprint["algorithm"],
                "worktree_fingerprint_sha256": worktree_fingerprint["sha256"],
            },
            producer={"name": "source-snapshot-resolver", "version": "1.1.0"},
        )

    def resolve_source_path(self, connector_id: str, relative_path: str) -> Path:
        connector = self.get(connector_id)
        root = Path(connector["source_root"]).resolve()
        candidate_text = relative_path.replace("\\", "/")
        if candidate_text.startswith("source_code/") and root.name == "source_code":
            candidate_text = candidate_text[len("source_code/") :]
        candidate = (root / candidate_text).resolve()
        if not _is_relative_to(candidate, root) or not candidate.is_file():
            raise PlatformError(
                "SOURCE_FILE_NOT_FOUND",
                "Source evidence path is not available in the selected connector.",
                status_code=404,
                details={"path": relative_path, "source_connector_id": connector_id},
            )
        return candidate

    def _replace_connector(self, connector: dict[str, Any]) -> None:
        state = self.registry.read()
        connectors = [item for item in state.get("connectors", []) if item.get("connector_id") != connector["connector_id"]]
        connectors.append(connector)
        self.registry.write({"connectors": connectors})

    def _create_snapshot(self, source_root: Path) -> dict[str, Any]:
        try:
            from .source_revision_snapshot import build_snapshot

            return build_snapshot(source_root)
        except Exception as exc:
            raise PlatformError(
                "SOURCE_SNAPSHOT_FAILED",
                "Source root does not contain a readable Git snapshot.",
                details={"path": str(source_root), "reason": str(exc)},
            ) from exc

    @staticmethod
    def _unknown_binding(*, resolved_snapshot_id: str | None = None) -> dict[str, Any]:
        return {
            "declared_revision": None,
            "resolved_snapshot_id": resolved_snapshot_id,
            "match_level": "unknown",
            "resolver_version": "1.0",
        }

    def _clone_audit(
        self,
        plan: dict[str, Any],
        capacity: dict[str, Any],
        result: subprocess.CompletedProcess[str] | None,
        cleanup_result: str,
        status: str,
        *,
        source_connector_id: str,
    ) -> dict[str, Any]:
        payload = {
            "action_id": "source_connector.clone_git",
            "status": status,
            "plan_id": plan["plan_id"],
            "remote_url": plan["remote_url"],
            "capacity": capacity,
            "command": plan["command_preview"],
            "returncode": result.returncode if result else None,
            "stdout_tail": (result.stdout[-4000:] if result else ""),
            "stderr_tail": (result.stderr[-4000:] if result else ""),
            "cleanup_result": cleanup_result,
        }
        connector = self.get(source_connector_id)
        snapshot_id = connector["snapshot"]["snapshot_id"]
        return self.artifact_store.write(
            "action_audit",
            payload,
            source_connector_id=source_connector_id,
            source_binding=self.resolve_binding(source_connector_id, snapshot_id),
            producer={"name": "source-connector", "version": "1.0.0"},
        )
