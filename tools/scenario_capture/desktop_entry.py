#!/usr/bin/env python3
"""Stdlib-only desktop entry to the shared scenario/PMU capture runtime.

One JSON object per stdout line is the host contract. Device commands and
collector cleanup remain owned by run_local_package and the fixed Runner.
"""
from __future__ import annotations

import argparse
from copy import deepcopy
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import stat
import sys
import tempfile
import threading
import zipfile
from typing import Any, Callable

APP_ROOT = Path(__file__).resolve().parents[2]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from backend.core import ArtifactStore, PlatformError, canonical_json_bytes, sha256_file, utc_now
from backend.local_capture_package import (
    LocalCapturePackageLimits, _bounded_json, _portable_path, _validate_manifest,
    validate_local_capture_zip,
)
from backend.scenario_capture_local_package import build_local_capture_package
from tools.scenario_capture.plan_contract import OPERATIONS, create_plan_payload, gallery_steps
from tools.scenario_capture import run_local_package as local_runner
from backend.pmu_sampler import DEFAULT_DEVICE_PATH, LEGACY_DEVICE_PATH, platform_bundle_status
from tools.scenario_capture.pmu_bundle import SAMPLER_VERSION

ASSET_ROOT = Path(__file__).resolve().parent


def _directory_content_identity(directory: Path) -> str:
    """Hash an immutable package tree without following links."""
    digest = hashlib.sha256()
    for path in sorted(directory.rglob("*"), key=lambda item: item.relative_to(directory).as_posix()):
        if path.is_symlink():
            raise RuntimeError("Generated capture package contains a symbolic link")
        if not path.is_file():
            continue
        relative = path.relative_to(directory).as_posix().encode("utf-8")
        digest.update(len(relative).to_bytes(4, "big")); digest.update(relative)
        digest.update(path.stat().st_size.to_bytes(8, "big")); digest.update(bytes.fromhex(sha256_file(path)))
    return digest.hexdigest()
IDENTITY_PATTERN = re.compile(r"[A-Za-z0-9_.:-]{1,160}")
MAX_INPUT_BYTES = 4 * 1024 * 1024


def _fail(code: str, message: str) -> None:
    raise PlatformError(code, message, status_code=422)


def _load_json(path: Path | str) -> dict[str, Any]:
    if str(path) == "-":
        raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    else:
        value = Path(path)
        if value.is_symlink() or not value.is_file():
            _fail("DESKTOP_JSON_INVALID", "JSON 输入必须是普通文件。")
        with value.open("rb") as stream:
            raw = stream.read(MAX_INPUT_BYTES + 1)
    if len(raw) > MAX_INPUT_BYTES:
        _fail("DESKTOP_JSON_TOO_LARGE", "JSON 输入超过 4 MiB。")

    def unique(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                _fail("DESKTOP_JSON_INVALID", "JSON 输入包含重复字段：" + key)
            result[key] = value
        return result

    value = json.loads(raw.decode("utf-8-sig"), object_pairs_hook=unique)
    if not isinstance(value, dict):
        _fail("DESKTOP_JSON_INVALID", "JSON 输入必须是对象。")
    _bounded_json(value, LocalCapturePackageLimits())
    return value


def catalog() -> dict[str, Any]:
    """Desktop defaults; saved plans preserve their explicitly chosen period."""
    return {
        "schema_version": "scenario_capture_catalog/v1", "host_platform": sys.platform,
        "templates": [{"template_id": "gallery_17_steps_v1", "name": "图库 17 步", "steps": gallery_steps()},
                      {"template_id": "custom", "name": "自定义场景", "steps": []}],
        "default_template_id": "gallery_17_steps_v1", "default_connection_mode": "local",
        "connection_modes": [{"id": "local", "name": "本机 HDC 直连", "requires_bridge": False}],
        "default_profile": _load_json(ASSET_ROOT / "gallery_17_profile.json"),
        "default_capture": {"selected_step_ids": ["step_07"], "collectors": ["trace", "hilog", "pmu"],
            "preparation_mode": "auto", "pmu": {"binary_path": DEFAULT_DEVICE_PATH, "period_us": 10,
                "duration_ms": 10000, "group": "auto", "reader_cpu": 0}},
        "operations": sorted(OPERATIONS), "execution_policy": "through_last_selected",
        "runner_version": local_runner.RUNNER_VERSION,
        "pmu_sampler": {"display_name": "通用线程 PMU 采样器", "sampler_version": SAMPLER_VERSION,
            "platform_bundle": platform_bundle_status(ASSET_ROOT / "prebuilt" / "aarch64-linux-ohos"),
            "default_device_path": DEFAULT_DEVICE_PATH, "legacy_device_path": LEGACY_DEVICE_PATH,
            "period_us_range": [10, 10000], "duration_ms_range": [100, 10000],
            "period_semantics": "requested_interval_actual_cadence_measured",
            "supported_modes": ["auto", "cache", "branch", "stall", "l1d", "llc", "dtlb"],
            "auto_event_groups": ["base", "cache", "branch", "stall", "l1d", "llc", "dtlb"],
            "auto_event_count": 14, "scope": "target_main_thread", "user_only": True},
        "provenance": _load_json(ASSET_ROOT / "provenance.json"),
    }


def save_plan(payload: dict[str, Any], output_root: Path, *, project_id: str = "desktop-local",
              device_serial: str = "") -> dict[str, Any]:
    if not IDENTITY_PATTERN.fullmatch(project_id):
        _fail("DESKTOP_PROJECT_INVALID", "项目标识无效。")
    plan_payload = create_plan_payload(payload, catalog())
    root = Path(output_root).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    store = ArtifactStore(root / "plan-store", project_id=project_id)
    artifact = store.write("scenario_capture_plan", plan_payload,
        source_connector_id=plan_payload["source_connector_id"],
        source_binding={"match_level": "unbound", "declared_revision": plan_payload["source_snapshot_id"] or None},
        producer={"name": "scenario-capture", "version": "1.1.0"}, schema_version="scenario_capture_plan/v1")
    plan = {**artifact, "plan_id": artifact["artifact_id"]}
    built = build_local_capture_package(plan=plan, project_id=project_id, plan_id=plan["plan_id"],
        device_serial=device_serial, connection_mode="local")
    destination = root / (plan["plan_id"] + "-" + built["package"]["package_id"])
    staging = Path(tempfile.mkdtemp(prefix=".package-", dir=root))
    archive_path = root / built["filename"]
    descriptor, partial_name = tempfile.mkstemp(prefix=".capture-package-", suffix=".partial", dir=root)
    os.close(descriptor)
    partial_archive = Path(partial_name)
    try:
        # These members were just generated by the fixed package builder, not
        # supplied by a ZIP upload. Validate names before writing nonetheless.
        with zipfile.ZipFile(io.BytesIO(built["content"])) as bundle:
            for info in bundle.infolist():
                path = local_runner._safe_relative(staging.resolve(), info.filename, "generated package member")
                path.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as source, path.open("wb") as target:
                    shutil.copyfileobj(source, target)
        partial_archive.write_bytes(built["content"])
        try:
            staging.rename(destination)
        except OSError:
            if (destination.is_symlink() or not destination.is_dir()
                    or _directory_content_identity(destination) != _directory_content_identity(staging)):
                raise
            shutil.rmtree(staging)
        archive_digest = sha256_file(partial_archive)
        try:
            os.link(partial_archive, archive_path)
        except FileExistsError:
            if (archive_path.is_symlink() or not archive_path.is_file()
                    or archive_path.stat().st_size != partial_archive.stat().st_size
                    or sha256_file(archive_path) != archive_digest):
                raise RuntimeError("Capture package archive name is occupied by different data")
    finally:
        if staging.exists():
            shutil.rmtree(staging)
        partial_archive.unlink(missing_ok=True)
    return {"plan": plan, "plan_id": plan["plan_id"], "package_id": built["package"]["package_id"],
        "package_directory": str(destination), "package_zip": str(archive_path),
        "package_sha256": built["sha256"], "readiness": plan["readiness"]}


def _signature(path: Path) -> tuple[int, int, int, int]:
    value = path.lstat()
    if not stat.S_ISREG(value.st_mode):
        _fail("DESKTOP_TRACE_PATH_INVALID", "采集数据必须是普通文件。")
    return value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns


def _directory_member(root: Path, relative: str) -> Path:
    normalized = _portable_path(relative, field="manifest relative_path")
    path = root
    for part in normalized.split("/"):
        path = path / part
        if path.is_symlink():
            _fail("DESKTOP_TRACE_PATH_INVALID", "采集文件路径包含符号链接。")
    if not path.is_file() or not path.resolve().is_relative_to(root):
        _fail("DESKTOP_TRACE_FILE_MISSING", "采集文件缺失或路径越界：" + normalized)
    _signature(path)
    return path


def _identity(manifest: dict[str, Any]) -> str:
    # Local paths/storage metadata differ between a run directory and its ZIP.
    # Identity binds the same run/device/plan and the exact evidence file set.
    value = {"schema_version": "desktop_trace_evidence/v1",
        **{key: manifest.get(key) for key in ("run_id", "plan_id", "status", "project_id", "device_serial")},
        "files": sorted(({key: entry[key] for key in ("relative_path", "size_bytes", "sha256")}
                         for entry in manifest["files"]), key=lambda entry: entry["relative_path"])}
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def inspect_trace(source_path: Path) -> dict[str, Any]:
    source = Path(source_path).expanduser()
    if source.is_symlink():
        _fail("DESKTOP_TRACE_PATH_INVALID", "采集源不能是符号链接。")
    source = source.resolve(strict=True)
    limits = LocalCapturePackageLimits()
    if source.is_file():
        before = _signature(source)
        validated = validate_local_capture_zip(source)
        manifest = validated.manifest
        with zipfile.ZipFile(source) as bundle:
            for entry in manifest["files"]:
                digest = hashlib.sha256()
                size = 0
                with bundle.open(validated.members[entry["relative_path"]]) as stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                        size += len(chunk)
                        if size > entry["size_bytes"]:
                            _fail("LOCAL_CAPTURE_FILE_SIZE_MISMATCH", "采集包文件实际大小超过声明。")
                        digest.update(chunk)
                if size != entry["size_bytes"] or digest.hexdigest() != entry["sha256"]:
                    _fail("LOCAL_CAPTURE_FILE_HASH_MISMATCH", "采集包文件校验失败：" + entry["relative_path"])
        if _signature(source) != before:
            _fail("LOCAL_CAPTURE_ARCHIVE_CHANGED", "采集包在校验期间发生变化。")
        return {"manifest": manifest, "files": manifest["files"], "archive": str(source),
            "archive_sha256": validated.archive_sha256, "identity_sha256": _identity(manifest),
            "verified": True, "verified_at": utc_now()}
    if not source.is_dir():
        _fail("DESKTOP_TRACE_PATH_INVALID", "请选择采集目录或标准 .capture.zip。")
    manifest_path = _directory_member(source, "manifest.json")
    before_manifest = _signature(manifest_path)
    manifest = _load_json(manifest_path)
    clean = deepcopy(manifest)
    clean.pop("managed_path", None)
    members: dict[str, zipfile.ZipInfo] = {}
    total = 0
    files = clean.get("files")
    if not isinstance(files, list) or len(files) > limits.max_members:
        _fail("LOCAL_CAPTURE_MANIFEST_INVALID", "采集文件清单无效。")
    for entry in files:
        if not isinstance(entry, dict):
            _fail("LOCAL_CAPTURE_MANIFEST_INVALID", "采集文件条目无效。")
        entry.pop("managed_path", None)
        entry.pop("storage_locations", None)
        relative = _portable_path(entry.get("relative_path"), field="manifest relative_path")
        path = _directory_member(source, relative)
        info = zipfile.ZipInfo(relative)
        info.file_size = path.stat().st_size
        info.external_attr = (stat.S_IFREG | 0o600) << 16
        total += info.file_size
        if info.file_size > limits.max_member_bytes or total > limits.max_total_uncompressed_bytes:
            _fail("LOCAL_CAPTURE_UNCOMPRESSED_TOO_LARGE", "采集目录超过校验大小限制。")
        members[relative] = info
    normalized, _ = _validate_manifest(clean, members, limits=limits)
    verified_files = []
    for entry in normalized["files"]:
        path = _directory_member(source, entry["relative_path"])
        before = _signature(path)
        if sha256_file(path) != entry["sha256"] or _signature(path) != before:
            _fail("LOCAL_CAPTURE_FILE_HASH_MISMATCH", "采集目录文件校验失败：" + entry["relative_path"])
        verified_files.append({**entry, "absolute_path": str(path)})
    if _signature(manifest_path) != before_manifest:
        _fail("LOCAL_CAPTURE_ARCHIVE_CHANGED", "采集清单在校验期间发生变化。")
    return {"manifest": normalized, "files": verified_files, "run_directory": str(source),
        "manifest_path": str(manifest_path), "identity_sha256": _identity(normalized),
        "verified": True, "verified_at": utc_now()}


def _parent_alive(pid: int) -> bool:
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # Access denied is not proof of exit.
        try:
            code = wintypes.DWORD()
            return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def run_capture(package: Path, hdc: Path, serial: str, output_root: Path, *,
                cancel_file: Path | None = None, parent_pid: int | None = None,
                recover_run_ids: set[str] | None = None,
                progress: Callable[[dict[str, Any]], None] | None = None) -> dict[str, Any]:
    # The desktop explicitly freezes the device for each run. The older
    # standalone PowerShell package retains its separate unique-device mode.
    local_runner._safe_serial(serial)
    if parent_pid is not None and parent_pid <= 0:
        _fail("DESKTOP_PARENT_PID_INVALID", "父进程 PID 无效。")
    cancel_event, finished = threading.Event(), threading.Event()

    def watch_cancel() -> None:
        while not finished.is_set():
            if (cancel_file is not None and cancel_file.exists()) or (parent_pid is not None and not _parent_alive(parent_pid)):
                cancel_event.set()
                return
            finished.wait(0.2)

    monitor = threading.Thread(target=watch_cancel, name="desktop-capture-cancel", daemon=True)
    monitor.start()
    try:
        return local_runner.run_package(package, hdc, serial, output_root,
            cancel_event=cancel_event, progress_callback=progress,
            explicitly_terminal_run_ids=recover_run_ids, private_device=True)
    finally:
        finished.set()
        monitor.join(timeout=1)


def _emit(kind: str, value: dict[str, Any]) -> None:
    try:
        print(json.dumps({**value, "type": kind}, ensure_ascii=False, separators=(",", ":")), flush=True)
    except BrokenPipeError:
        # The process monitor still initiates ordinary Runner cleanup after
        # its desktop parent exits, even when the progress pipe is gone.
        with open(os.devnull, "w") as sink:
            os.dup2(sink.fileno(), sys.stdout.fileno())


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Desktop scenario-capture runtime (NDJSON output)")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("catalog")
    save = commands.add_parser("save-plan")
    save.add_argument("--input", required=True)
    save.add_argument("--output-root", required=True, type=Path)
    save.add_argument("--project-id", default="desktop-local")
    save.add_argument("--device-serial", default="")
    run = commands.add_parser("run")
    run.add_argument("--package", required=True, type=Path)
    run.add_argument("--hdc", required=True, type=Path)
    run.add_argument("--device-serial", required=True)
    run.add_argument("--output-root", required=True, type=Path)
    run.add_argument("--cancel-file", type=Path)
    run.add_argument("--parent-pid", type=int)
    run.add_argument("--recover-run-ids", default="")
    inspect = commands.add_parser("inspect-trace", aliases=["validate-result"])
    inspect.add_argument("--path", required=True, type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    args = _parser().parse_args(argv)
    try:
        if args.command == "catalog":
            result = catalog()
        elif args.command == "save-plan":
            result = save_plan(_load_json(args.input), args.output_root,
                project_id=args.project_id, device_serial=args.device_serial)
        elif args.command == "run":
            recover_run_ids = {value for value in args.recover_run_ids.split(",") if value}
            if any(not local_runner.RUN_ID_PATTERN.fullmatch(value) for value in recover_run_ids):
                _fail("DESKTOP_RECOVERY_RUN_INVALID", "待回收采集记录包含无效的 run ID。")
            result = run_capture(args.package, args.hdc, args.device_serial, args.output_root,
                cancel_file=args.cancel_file, parent_pid=args.parent_pid,
                recover_run_ids=recover_run_ids,
                progress=lambda value: _emit("progress", value))
        else:
            result = inspect_trace(args.path)
        _emit("result", result)
        return 1 if args.command == "run" and result["status"] not in {"succeeded", "partial"} else 0
    except Exception as exc:
        _emit("error", {"code": getattr(exc, "code", "DESKTOP_CAPTURE_ERROR"),
            "message": str(exc)[:3000], "exception_type": type(exc).__name__})
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
