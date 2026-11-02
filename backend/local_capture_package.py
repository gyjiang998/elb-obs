"""Validated import of portable Windows-local scenario capture packages.

The HTTP route is intentionally kept outside this module.  A caller first
authenticates the uploader and then supplies the trusted project/Bridge
identity here.  Package paths and provenance never override that identity.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import tempfile
from typing import Any
import zipfile

from .core import PlatformError, atomic_write_json, canonical_json_bytes, file_lock, fsync_directory, sha256_file, utc_now


RUN_ID_PATTERN = re.compile(r"capture_[a-f0-9]{24}")
HASH_PATTERN = re.compile(r"[a-f0-9]{64}")
IDENTITY_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,160}")
PLAN_ID_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,160}")
STEP_ID_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,160}")
KIND_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,63}")
PATH_SEGMENT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
TERMINAL_STATUSES = frozenset({"succeeded", "partial", "failed", "cancelled"})
ALLOWED_COMPRESSION = frozenset({zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED})
RESERVED_RUN_PATHS = frozenset({"manifest.json", "run.json"})
# The Windows Desktop owns a local-only ArtifactStore whose project name is an
# origin marker, not an authorization boundary on the analysis server.  The
# importer always replaces it with the authenticated destination project while
# retaining the portable value in provenance.  Arbitrary project names remain
# rejected so a package cannot claim another server project.
PORTABLE_LOCAL_PROJECT_IDS = frozenset({"desktop-local"})


class _DuplicateJsonKey(ValueError):
    pass


@dataclass(frozen=True)
class LocalCapturePackageLimits:
    max_archive_bytes: int = 2 * 1024 * 1024 * 1024
    max_total_uncompressed_bytes: int = 2 * 1024 * 1024 * 1024
    max_member_bytes: int = 2 * 1024 * 1024 * 1024
    max_manifest_bytes: int = 4 * 1024 * 1024
    max_members: int = 512
    max_steps: int = 1000
    max_json_nodes: int = 100_000
    max_json_depth: int = 40

    def __post_init__(self) -> None:
        values = (
            self.max_archive_bytes,
            self.max_total_uncompressed_bytes,
            self.max_member_bytes,
            self.max_manifest_bytes,
            self.max_members,
            self.max_steps,
            self.max_json_nodes,
            self.max_json_depth,
        )
        if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in values):
            raise ValueError("Local capture package limits must be positive integers")


@dataclass(frozen=True)
class ValidatedLocalCapturePackage:
    archive_path: Path
    archive_sha256: str
    archive_size_bytes: int
    content_sha256: str
    manifest: dict[str, Any]
    members: dict[str, zipfile.ZipInfo]
    total_uncompressed_bytes: int


def _fail(code: str, message: str, *, status_code: int = 422, **details: Any) -> None:
    raise PlatformError(code, message, status_code=status_code, details=details)


def _portable_path(value: Any, *, field: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        _fail("LOCAL_CAPTURE_PATH_INVALID", "采集包包含无效的相对路径。", field=field)
    if "\x00" in value or "\\" in value or value.startswith("/") or re.match(r"^[A-Za-z]:", value):
        _fail("LOCAL_CAPTURE_PATH_INVALID", "采集包路径必须使用安全的 POSIX 相对路径。", field=field, path=value)
    path = PurePosixPath(value)
    if not path.parts or any(part in {"", ".", ".."} or not PATH_SEGMENT_PATTERN.fullmatch(part) for part in path.parts):
        _fail("LOCAL_CAPTURE_PATH_INVALID", "采集包路径包含不安全的目录或文件名。", field=field, path=value)
    return path.as_posix()


def _bounded_json(value: Any, limits: LocalCapturePackageLimits) -> None:
    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > limits.max_json_nodes or depth > limits.max_json_depth:
            _fail("LOCAL_CAPTURE_MANIFEST_TOO_COMPLEX", "采集包清单嵌套或字段数量超过限制。")
        if isinstance(current, dict):
            for key, item in current.items():
                if not isinstance(key, str) or len(key) > 512:
                    _fail("LOCAL_CAPTURE_MANIFEST_INVALID", "采集包清单包含无效字段名。")
                stack.append((item, depth + 1))
        elif isinstance(current, list):
            stack.extend((item, depth + 1) for item in current)
        elif isinstance(current, str) and len(current) > 1024 * 1024:
            _fail("LOCAL_CAPTURE_MANIFEST_INVALID", "采集包清单包含过长文本字段。")
        elif isinstance(current, float) and not math.isfinite(current):
            _fail("LOCAL_CAPTURE_MANIFEST_INVALID", "采集包清单包含非有限数值。")
        elif current is not None and not isinstance(current, (str, int, float, bool)):
            _fail("LOCAL_CAPTURE_MANIFEST_INVALID", "采集包清单包含不支持的 JSON 值。")


def _member_kind(info: zipfile.ZipInfo) -> str:
    mode = info.external_attr >> 16
    file_type = stat.S_IFMT(mode)
    if info.is_dir() or file_type == stat.S_IFDIR:
        return "directory"
    if file_type not in {0, stat.S_IFREG}:
        return "link" if file_type == stat.S_IFLNK else "special"
    return "file"


def _read_manifest(bundle: zipfile.ZipFile, info: zipfile.ZipInfo, limits: LocalCapturePackageLimits) -> dict[str, Any]:
    if info.file_size > limits.max_manifest_bytes:
        _fail("LOCAL_CAPTURE_MANIFEST_TOO_LARGE", "采集包清单超过大小限制。", max_bytes=limits.max_manifest_bytes)
    try:
        raw = bundle.read(info)
        def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
            result: dict[str, Any] = {}
            for key, item in pairs:
                if key in result:
                    raise _DuplicateJsonKey(f"duplicate key: {key}")
                result[key] = item
            return result
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecursionError, _DuplicateJsonKey) as exc:
        _fail("LOCAL_CAPTURE_MANIFEST_INVALID", "采集包清单不是有效的 UTF-8 JSON。", reason=str(exc))
    if not isinstance(value, dict):
        _fail("LOCAL_CAPTURE_MANIFEST_INVALID", "采集包清单根节点必须是对象。")
    _bounded_json(value, limits)
    return value


def _validate_manifest(
    manifest: dict[str, Any],
    archive_members: dict[str, zipfile.ZipInfo],
    *,
    limits: LocalCapturePackageLimits,
) -> tuple[dict[str, Any], dict[str, zipfile.ZipInfo]]:
    if manifest.get("schema_version") != "scenario_capture_manifest/v1":
        _fail("LOCAL_CAPTURE_SCHEMA_UNSUPPORTED", "采集包清单版本不受支持。", schema_version=manifest.get("schema_version"))
    run_id = manifest.get("run_id")
    if not isinstance(run_id, str) or not RUN_ID_PATTERN.fullmatch(run_id):
        _fail("LOCAL_CAPTURE_MANIFEST_INVALID", "采集包 run_id 无效。")
    plan_id = manifest.get("plan_id")
    if not isinstance(plan_id, str) or not PLAN_ID_PATTERN.fullmatch(plan_id):
        _fail("LOCAL_CAPTURE_MANIFEST_INVALID", "采集包 plan_id 无效。")
    status = manifest.get("status")
    if status not in TERMINAL_STATUSES:
        _fail("LOCAL_CAPTURE_STATUS_INVALID", "只允许导入已经结束的本地采集包。", status=status)
    if "managed_path" in manifest:
        _fail("LOCAL_CAPTURE_MANIFEST_INVALID", "便携采集包不能声明服务端受管路径。")

    steps = manifest.get("steps")
    if not isinstance(steps, list) or len(steps) > limits.max_steps:
        _fail("LOCAL_CAPTURE_MANIFEST_INVALID", "采集包步骤列表无效或超过数量限制。")
    step_ids: set[str] = set()
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            _fail("LOCAL_CAPTURE_MANIFEST_INVALID", "采集步骤必须是对象。", step_index=index)
        step_id = step.get("step_id")
        if not isinstance(step_id, str) or not STEP_ID_PATTERN.fullmatch(step_id) or step_id in step_ids:
            _fail("LOCAL_CAPTURE_MANIFEST_INVALID", "采集步骤标识无效或重复。", step_index=index)
        step_ids.add(step_id)

    files = manifest.get("files")
    if not isinstance(files, list) or not files or len(files) > limits.max_members:
        _fail("LOCAL_CAPTURE_MANIFEST_INVALID", "采集包文件清单为空或超过数量限制。")
    declared: dict[str, zipfile.ZipInfo] = {}
    declared_casefold: set[str] = set()
    normalized_files: list[dict[str, Any]] = []
    for index, entry in enumerate(files):
        if not isinstance(entry, dict):
            _fail("LOCAL_CAPTURE_MANIFEST_INVALID", "采集文件条目必须是对象。", file_index=index)
        if "managed_path" in entry:
            _fail("LOCAL_CAPTURE_MANIFEST_INVALID", "便携采集文件不能声明服务端受管路径。", file_index=index)
        relative = _portable_path(entry.get("relative_path"), field=f"files[{index}].relative_path")
        folded = relative.casefold()
        if PurePosixPath(relative).parts[0].casefold() in RESERVED_RUN_PATHS:
            _fail("LOCAL_CAPTURE_PATH_RESERVED", "采集文件不能占用平台 Run 的控制路径。", path=relative)
        if folded in declared_casefold:
            _fail("LOCAL_CAPTURE_FILE_DUPLICATE", "采集包文件路径重复。", path=relative)
        info = archive_members.get(relative)
        if info is None or _member_kind(info) != "file":
            _fail("LOCAL_CAPTURE_FILE_MISSING", "采集包缺少清单声明的普通文件。", path=relative)
        size = entry.get("size_bytes")
        digest = entry.get("sha256")
        kind = entry.get("kind")
        step_id = entry.get("step_id", "")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0 or size != info.file_size:
            _fail("LOCAL_CAPTURE_FILE_SIZE_MISMATCH", "采集文件大小与清单不一致。", path=relative)
        if not isinstance(digest, str) or not HASH_PATTERN.fullmatch(digest):
            _fail("LOCAL_CAPTURE_FILE_HASH_INVALID", "采集文件缺少有效 SHA-256。", path=relative)
        if not isinstance(kind, str) or not KIND_PATTERN.fullmatch(kind):
            _fail("LOCAL_CAPTURE_MANIFEST_INVALID", "采集文件类型无效。", path=relative)
        if not isinstance(step_id, str) or (step_id and step_id not in step_ids):
            _fail("LOCAL_CAPTURE_MANIFEST_INVALID", "采集文件引用了未知步骤。", path=relative, step_id=step_id)
        name = entry.get("name")
        if name is not None and (not isinstance(name, str) or name != PurePosixPath(relative).name):
            _fail("LOCAL_CAPTURE_MANIFEST_INVALID", "采集文件名与相对路径不一致。", path=relative)
        clean = deepcopy(entry)
        clean.pop("storage_locations", None)
        clean["relative_path"] = relative
        clean["name"] = PurePosixPath(relative).name
        normalized_files.append(clean)
        declared[relative] = info
        declared_casefold.add(folded)

    declared_paths = set(declared)
    for relative in declared_paths:
        parent = PurePosixPath(relative).parent
        while parent != PurePosixPath("."):
            if parent.as_posix().casefold() in declared_casefold:
                _fail(
                    "LOCAL_CAPTURE_PATH_CONFLICT",
                    "采集包中同一路径不能同时作为文件和目录。",
                    path=relative,
                    conflicting_path=parent.as_posix(),
                )
            parent = parent.parent

    unexpected = sorted(name for name in archive_members if name != "manifest.json" and _member_kind(archive_members[name]) == "file" and name not in declared)
    if unexpected:
        _fail("LOCAL_CAPTURE_FILE_UNDECLARED", "采集包包含未在清单中声明的文件。", paths=unexpected[:20])
    declared_parents = {parent.as_posix() for name in declared for parent in PurePosixPath(name).parents if parent != PurePosixPath(".")}
    unexpected_directories = sorted(name for name, info in archive_members.items()
                                    if _member_kind(info) == "directory" and name not in declared_parents)
    if unexpected_directories:
        _fail("LOCAL_CAPTURE_FILE_UNDECLARED", "采集包包含无对应采集文件的目录。", paths=unexpected_directories[:20])
    result = deepcopy(manifest)
    result["files"] = normalized_files
    return result, declared


def validate_local_capture_zip(
    archive_path: Path,
    *,
    expected_sha256: str = "",
    expected_size_bytes: int | None = None,
    limits: LocalCapturePackageLimits | None = None,
) -> ValidatedLocalCapturePackage:
    """Validate a stable ZIP file without extracting it outside a staging area."""

    limits = limits or LocalCapturePackageLimits()
    path = Path(archive_path)
    try:
        current = path.lstat()
    except OSError as exc:
        _fail("LOCAL_CAPTURE_ARCHIVE_MISSING", "未找到本地采集包。", status_code=404, reason=str(exc))
    if path.is_symlink() or not stat.S_ISREG(current.st_mode):
        _fail("LOCAL_CAPTURE_ARCHIVE_INVALID", "本地采集包必须是普通 ZIP 文件。")
    if current.st_size > limits.max_archive_bytes:
        _fail("LOCAL_CAPTURE_ARCHIVE_TOO_LARGE", "本地采集包超过上传大小限制。", status_code=413, max_bytes=limits.max_archive_bytes)
    if expected_size_bytes is not None:
        if isinstance(expected_size_bytes, bool) or not isinstance(expected_size_bytes, int) or expected_size_bytes < 0:
            _fail("LOCAL_CAPTURE_ARCHIVE_SIZE_INVALID", "本地采集包声明大小无效。")
        if current.st_size != expected_size_bytes:
            _fail("LOCAL_CAPTURE_ARCHIVE_SIZE_MISMATCH", "本地采集包大小与声明不一致。", status_code=409)
    archive_sha256 = sha256_file(path)
    if expected_sha256:
        if not isinstance(expected_sha256, str) or not HASH_PATTERN.fullmatch(expected_sha256) or archive_sha256 != expected_sha256:
            _fail("LOCAL_CAPTURE_ARCHIVE_HASH_MISMATCH", "本地采集包 SHA-256 与声明不一致。", status_code=409)

    try:
        with zipfile.ZipFile(path) as bundle:
            infos = bundle.infolist()
            if not infos or len(infos) > limits.max_members:
                _fail("LOCAL_CAPTURE_MEMBER_LIMIT", "采集包为空或成员数量超过限制。", max_members=limits.max_members)
            members: dict[str, zipfile.ZipInfo] = {}
            folded_names: set[str] = set()
            total = 0
            for info in infos:
                raw_name = info.filename[:-1] if info.is_dir() and info.filename.endswith("/") else info.filename
                name = _portable_path(raw_name, field="zip member")
                folded = name.casefold()
                if folded in folded_names:
                    _fail("LOCAL_CAPTURE_FILE_DUPLICATE", "采集包包含重复或大小写冲突的成员。", path=name)
                folded_names.add(folded)
                if info.flag_bits & 0x1:
                    _fail("LOCAL_CAPTURE_MEMBER_ENCRYPTED", "采集包不能包含加密成员。", path=name)
                if info.compress_type not in ALLOWED_COMPRESSION:
                    _fail("LOCAL_CAPTURE_COMPRESSION_UNSUPPORTED", "采集包使用了不支持的压缩算法。", path=name)
                kind = _member_kind(info)
                if kind in {"link", "special"}:
                    _fail("LOCAL_CAPTURE_MEMBER_UNSAFE", "采集包不能包含链接或特殊文件。", path=name, member_kind=kind)
                if info.file_size > limits.max_member_bytes:
                    _fail("LOCAL_CAPTURE_MEMBER_TOO_LARGE", "采集包单个文件超过大小限制。", status_code=413, path=name, max_bytes=limits.max_member_bytes)
                total += info.file_size
                if total > limits.max_total_uncompressed_bytes:
                    _fail("LOCAL_CAPTURE_UNCOMPRESSED_TOO_LARGE", "采集包解压后总大小超过限制。", status_code=413, max_bytes=limits.max_total_uncompressed_bytes)
                members[name] = info
            manifest_info = members.get("manifest.json")
            if manifest_info is None or _member_kind(manifest_info) != "file":
                _fail("LOCAL_CAPTURE_MANIFEST_MISSING", "采集包根目录缺少 manifest.json。")
            manifest = _read_manifest(bundle, manifest_info, limits)
            normalized, declared = _validate_manifest(manifest, members, limits=limits)
    except zipfile.BadZipFile as exc:
        _fail("LOCAL_CAPTURE_ARCHIVE_INVALID", "本地采集包不是有效 ZIP 文件。", reason=str(exc))

    identity = {
        "schema_version": "local_capture_package_identity/v1",
        "manifest": normalized,
        "files": [{"relative_path": path, "size_bytes": info.file_size,
                   "sha256": next(item["sha256"] for item in normalized["files"] if item["relative_path"] == path)}
                  for path, info in sorted(declared.items())],
    }
    try:
        after = path.lstat()
    except OSError as exc:
        _fail("LOCAL_CAPTURE_ARCHIVE_CHANGED", "本地采集包在校验期间发生变化。", status_code=409, reason=str(exc))
    if path.is_symlink() or not stat.S_ISREG(after.st_mode) or (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
        _fail("LOCAL_CAPTURE_ARCHIVE_CHANGED", "本地采集包在校验期间发生变化。", status_code=409)
    return ValidatedLocalCapturePackage(
        archive_path=path,
        archive_sha256=archive_sha256,
        archive_size_bytes=current.st_size,
        content_sha256=hashlib.sha256(canonical_json_bytes(identity)).hexdigest(),
        manifest=normalized,
        members=declared,
        total_uncompressed_bytes=total,
    )


class LocalCapturePackageImporter:
    """Atomically materialize a validated package as a standard capture Run."""

    def __init__(self, state_root: Path, *, limits: LocalCapturePackageLimits | None = None) -> None:
        self.state_root = Path(state_root).resolve()
        self.runs_root = self.state_root / "scenario-capture" / "runs"
        self.runs_root.mkdir(parents=True, exist_ok=True)
        self.limits = limits or LocalCapturePackageLimits()
        self.lock_path = self.runs_root / ".local-capture-package.lock"

    def _snapshot(self, source: Path, *, expected_sha256: str, expected_size_bytes: int | None) -> Path:
        path = Path(source)
        try:
            before = path.lstat()
        except OSError as exc:
            _fail("LOCAL_CAPTURE_ARCHIVE_MISSING", "未找到本地采集包。", status_code=404, reason=str(exc))
        if path.is_symlink() or not stat.S_ISREG(before.st_mode):
            _fail("LOCAL_CAPTURE_ARCHIVE_INVALID", "本地采集包必须是普通 ZIP 文件。")
        if before.st_size > self.limits.max_archive_bytes:
            _fail("LOCAL_CAPTURE_ARCHIVE_TOO_LARGE", "本地采集包超过上传大小限制。", status_code=413, max_bytes=self.limits.max_archive_bytes)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".local-capture-upload-", suffix=".zip", dir=self.runs_root)
        snapshot = Path(temporary_name)
        digest = hashlib.sha256()
        size = 0
        try:
            with path.open("rb") as reader, os.fdopen(descriptor, "wb") as writer:
                while chunk := reader.read(1024 * 1024):
                    size += len(chunk)
                    if size > self.limits.max_archive_bytes:
                        _fail("LOCAL_CAPTURE_ARCHIVE_TOO_LARGE", "本地采集包超过上传大小限制。", status_code=413, max_bytes=self.limits.max_archive_bytes)
                    digest.update(chunk)
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            after = path.lstat()
            if path.is_symlink() or not stat.S_ISREG(after.st_mode) or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
                _fail("LOCAL_CAPTURE_ARCHIVE_CHANGED", "本地采集包在导入期间发生变化。", status_code=409)
            observed = digest.hexdigest()
            if expected_size_bytes is not None:
                if isinstance(expected_size_bytes, bool) or not isinstance(expected_size_bytes, int) or expected_size_bytes < 0:
                    _fail("LOCAL_CAPTURE_ARCHIVE_SIZE_INVALID", "本地采集包声明大小无效。")
                if size != expected_size_bytes:
                    _fail("LOCAL_CAPTURE_ARCHIVE_SIZE_MISMATCH", "本地采集包大小与声明不一致。", status_code=409)
            if expected_sha256 and (not isinstance(expected_sha256, str) or not HASH_PATTERN.fullmatch(expected_sha256) or observed != expected_sha256):
                _fail("LOCAL_CAPTURE_ARCHIVE_HASH_MISMATCH", "本地采集包 SHA-256 与声明不一致。", status_code=409)
            return snapshot
        except Exception:
            snapshot.unlink(missing_ok=True)
            try:
                os.close(descriptor)
            except OSError:
                pass
            raise

    @staticmethod
    def _trusted_identity(project_id: str, bridge_id: str, storage_host: str) -> tuple[str, str, str]:
        if not isinstance(project_id, str) or not IDENTITY_PATTERN.fullmatch(project_id):
            _fail("LOCAL_CAPTURE_PROJECT_INVALID", "可信项目标识无效。")
        if bridge_id and (not isinstance(bridge_id, str) or not IDENTITY_PATTERN.fullmatch(bridge_id)):
            _fail("LOCAL_CAPTURE_BRIDGE_INVALID", "可信 Bridge 标识无效。")
        if storage_host and (not isinstance(storage_host, str) or len(storage_host) > 255 or any(ord(char) < 32 for char in storage_host)):
            _fail("LOCAL_CAPTURE_HOST_INVALID", "采集主机名称无效。")
        return project_id, bridge_id, storage_host

    def _normalize(
        self,
        package: ValidatedLocalCapturePackage,
        *,
        project_id: str,
        bridge_id: str,
        storage_host: str,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        portable = package.manifest
        portable_project_id = portable.get("project_id")
        if portable_project_id not in (None, "", project_id) and portable_project_id not in PORTABLE_LOCAL_PROJECT_IDS:
            _fail("LOCAL_CAPTURE_IDENTITY_MISMATCH", "采集包身份与已认证上传端不一致。", status_code=409, field="project_id")
        supplied_bridge_id = portable.get("bridge_id")
        if supplied_bridge_id not in (None, "", bridge_id):
            _fail("LOCAL_CAPTURE_IDENTITY_MISMATCH", "采集包身份与已认证上传端不一致。", status_code=409, field="bridge_id")
        portable_device = portable.get("device") if isinstance(portable.get("device"), dict) else {}
        serial = portable.get("device_serial") or portable_device.get("serial")
        if not isinstance(serial, str) or not IDENTITY_PATTERN.fullmatch(serial):
            _fail("LOCAL_CAPTURE_DEVICE_INVALID", "采集包缺少有效设备序列号。")
        connection_mode = "bridge" if bridge_id else "local"
        executor = "windows_local_powershell" if bridge_id else "local_capture_package"
        imported_at = utc_now()
        provenance = {
            "schema_version": "local_capture_package/v1",
            "archive_sha256": package.archive_sha256,
            "archive_size_bytes": package.archive_size_bytes,
            "content_sha256": package.content_sha256,
            "imported_at": imported_at,
            "bridge_id": bridge_id,
            "storage_host": storage_host,
            "portable_project_id": portable_project_id if isinstance(portable_project_id, str) else "",
        }
        manifest = deepcopy(portable)
        manifest.pop("project_id", None)
        manifest.pop("bridge_id", None)
        manifest.pop("device", None)
        manifest.pop("connection_mode", None)
        portable_execution = manifest.pop("execution_provenance", None)
        manifest["project_id"] = project_id
        manifest["connection_mode"] = connection_mode
        manifest["device_serial"] = serial
        manifest["device"] = {"connection_mode": connection_mode, "executor": executor,
                              "bridge_id": bridge_id, "serial": serial}
        manifest["execution_provenance"] = {
            "connection_mode": connection_mode,
            "executor": executor,
            "bridge_id": bridge_id,
            "storage_host": storage_host,
            "portable_execution_provenance": portable_execution if isinstance(portable_execution, dict) else {},
        }
        manifest["local_capture_package"] = provenance
        source_connector_id = manifest.get("source_connector_id")
        if not isinstance(source_connector_id, str) or not IDENTITY_PATTERN.fullmatch(source_connector_id):
            source_connector_id = "manual-local-capture"
        source_snapshot_id = manifest.get("source_snapshot_id")
        if (not isinstance(source_snapshot_id, str) or len(source_snapshot_id) > 200
                or any(ord(char) < 32 for char in source_snapshot_id)):
            source_snapshot_id = ""
        manifest["source_connector_id"] = source_connector_id
        manifest["source_snapshot_id"] = source_snapshot_id
        run_id = manifest["run_id"]
        for entry in manifest["files"]:
            entry["managed_path"] = f"scenario-capture/runs/{run_id}/{entry['relative_path']}"
        run = {
            "schema_version": "scenario_capture_run/v1",
            "run_id": run_id,
            "plan_id": manifest["plan_id"],
            "project_id": project_id,
            "status": manifest["status"],
            "created_at": manifest.get("created_at") if isinstance(manifest.get("created_at"), str) else imported_at,
            "updated_at": imported_at,
            "connection_mode": connection_mode,
            "requested_connection_mode": connection_mode,
            "bridge_id": bridge_id,
            "device_serial": serial,
            "device": manifest["device"],
            "execution_provenance": manifest["execution_provenance"],
            "source_connector_id": source_connector_id,
            "source_snapshot_id": source_snapshot_id,
            "manifest": manifest,
            "local_capture_package": provenance,
        }
        return manifest, run

    def _verify_existing(
        self,
        destination: Path,
        package: ValidatedLocalCapturePackage,
        *,
        project_id: str,
        bridge_id: str,
    ) -> dict[str, Any] | None:
        if not destination.exists():
            return None
        if destination.is_symlink() or not destination.is_dir():
            _fail("LOCAL_CAPTURE_RUN_CONFLICT", "同名采集 Run 路径已存在且不受管理。", status_code=409)
        run_path = destination / "run.json"
        manifest_path = destination / "manifest.json"
        if run_path.is_symlink() or manifest_path.is_symlink() or not run_path.is_file() or not manifest_path.is_file():
            _fail("LOCAL_CAPTURE_EXISTING_CORRUPT", "已存在的采集 Run 缺少受管清单。", status_code=409)
        try:
            run = json.loads(run_path.read_text(encoding="utf-8"))
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            _fail("LOCAL_CAPTURE_EXISTING_CORRUPT", "已存在的采集 Run 无法读取。", status_code=409, reason=str(exc))
        existing_hash = (run.get("local_capture_package") or {}).get("content_sha256")
        if existing_hash != package.content_sha256:
            _fail("LOCAL_CAPTURE_RUN_CONFLICT", "相同 run_id 已由不同采集包占用。", status_code=409, run_id=package.manifest["run_id"])
        if run.get("project_id") != project_id or str(run.get("bridge_id") or "") != bridge_id:
            _fail("LOCAL_CAPTURE_RUN_CONFLICT", "相同 run_id 已由其他项目或 Bridge 占用。", status_code=409,
                  run_id=package.manifest["run_id"])
        if manifest != run.get("manifest"):
            _fail("LOCAL_CAPTURE_EXISTING_CORRUPT", "已存在 Run 的清单副本不一致。", status_code=409)
        for entry in manifest.get("files", []):
            relative = _portable_path(entry.get("relative_path"), field="existing files.relative_path")
            expected_managed = f"scenario-capture/runs/{package.manifest['run_id']}/{relative}"
            if entry.get("managed_path") != expected_managed:
                _fail("LOCAL_CAPTURE_EXISTING_CORRUPT", "已存在 Run 的受管路径发生变化。", status_code=409, path=relative)
            path = destination.joinpath(*PurePosixPath(relative).parts)
            if path.is_symlink() or not path.is_file() or path.stat().st_size != entry.get("size_bytes") or sha256_file(path) != entry.get("sha256"):
                _fail("LOCAL_CAPTURE_EXISTING_CORRUPT", "已存在 Run 的采集文件缺失或发生变化。", status_code=409, path=relative)
        return run

    def _extract(self, package: ValidatedLocalCapturePackage, staging: Path) -> None:
        with zipfile.ZipFile(package.archive_path) as bundle:
            for relative, info in package.members.items():
                destination = staging.joinpath(*PurePosixPath(relative).parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256()
                size = 0
                expected = next(item for item in package.manifest["files"] if item["relative_path"] == relative)
                with bundle.open(info) as source, destination.open("xb") as target:
                    while chunk := source.read(1024 * 1024):
                        size += len(chunk)
                        if size > info.file_size or size > self.limits.max_member_bytes:
                            _fail("LOCAL_CAPTURE_MEMBER_TOO_LARGE", "采集文件解压后超过声明大小。", status_code=413, path=relative)
                        digest.update(chunk)
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                if size != info.file_size or size != expected["size_bytes"]:
                    _fail("LOCAL_CAPTURE_FILE_SIZE_MISMATCH", "采集文件实际大小与清单不一致。", path=relative)
                if digest.hexdigest() != expected["sha256"]:
                    _fail("LOCAL_CAPTURE_FILE_HASH_MISMATCH", "采集文件 SHA-256 校验失败。", status_code=409, path=relative)

    def import_zip(
        self,
        archive_path: Path,
        *,
        project_id: str,
        bridge_id: str = "",
        storage_host: str = "",
        expected_sha256: str = "",
        expected_size_bytes: int | None = None,
    ) -> dict[str, Any]:
        """Import one package, returning ``imported`` or ``already_imported``."""

        project_id, bridge_id, storage_host = self._trusted_identity(project_id, bridge_id, storage_host)
        snapshot = self._snapshot(Path(archive_path), expected_sha256=expected_sha256, expected_size_bytes=expected_size_bytes)
        staging: Path | None = None
        try:
            package = validate_local_capture_zip(snapshot, limits=self.limits)
            destination = self.runs_root / package.manifest["run_id"]
            with file_lock(self.lock_path):
                existing = self._verify_existing(destination, package, project_id=project_id, bridge_id=bridge_id)
                if existing is not None:
                    return {"status": "already_imported", "run_id": existing["run_id"], "run": existing,
                            "package": existing["local_capture_package"]}
            staging = Path(tempfile.mkdtemp(prefix=f".{package.manifest['run_id']}.", dir=self.runs_root))
            self._extract(package, staging)
            manifest, run = self._normalize(package, project_id=project_id, bridge_id=bridge_id, storage_host=storage_host)
            atomic_write_json(staging / "manifest.json", manifest)
            atomic_write_json(staging / "run.json", run)
            fsync_directory(staging)
            with file_lock(self.lock_path):
                existing = self._verify_existing(destination, package, project_id=project_id, bridge_id=bridge_id)
                if existing is not None:
                    return {"status": "already_imported", "run_id": existing["run_id"], "run": existing,
                            "package": existing["local_capture_package"]}
                os.replace(staging, destination)
                staging = None
                fsync_directory(self.runs_root)
            return {"status": "imported", "run_id": run["run_id"], "run": run, "package": run["local_capture_package"]}
        finally:
            snapshot.unlink(missing_ok=True)
            if staging is not None and staging.exists():
                shutil.rmtree(staging)
