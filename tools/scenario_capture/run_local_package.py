#!/usr/bin/env python3
"""Run a versioned scenario-capture package through a workstation-local HDC.

The capture path is deliberately offline: after the package has been
downloaded, no platform or Device Bridge request is made while collectors are
active.  The existing fixed scenario runner still owns device commands,
collector cleanup, PMU preparation and result validation.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import platform
import re
import secrets
import shlex
import signal
import socket
import subprocess
import sys
import threading
import time
import types
import zipfile
from copy import deepcopy
from pathlib import Path, PurePosixPath
from typing import Any, Callable


APP_ROOT = Path(__file__).resolve().parents[2]
if str(APP_ROOT) not in sys.path:
    sys.path.insert(0, str(APP_ROOT))

from backend.core import atomic_write_json, sha256_file, utc_now  # noqa: E402


class LocalPackageError(RuntimeError):
    """Actionable local-package validation or execution failure."""


class CaptureCancelled(RuntimeError):
    """Cancellation name intentionally matches the fixed runner contract."""


# The portable runtime intentionally does not bundle backend.scenario_capture:
# that server module brings in FastAPI, persistence and bridge dependencies.
# backend.pmu_sampler only needs its cancellation type on error paths, so expose
# that tiny contract before importing it.  This also keeps Ctrl+C and PMU probe
# failures working in an offline, stdlib-only package.
if importlib.util.find_spec("backend.scenario_capture") is None:
    _scenario_capture_contract = types.ModuleType("backend.scenario_capture")
    _scenario_capture_contract.CaptureCancelled = CaptureCancelled
    sys.modules["backend.scenario_capture"] = _scenario_capture_contract

from backend.pmu_sampler import prepare_pmu_sampler  # noqa: E402
from tools.scenario_capture.runner import (  # noqa: E402
    RUNNER_VERSION,
    execute_plan,
    pmu_validation_schema,
)


PACKAGE_SCHEMA = "scenario_capture_local_package/v1"
RESULT_SCHEMA = "scenario_capture_local_result/v1"
SERIAL_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
PLAN_ID_PATTERN = re.compile(r"^art_[a-f0-9]{24}$")
RUN_ID_PATTERN = re.compile(r"^capture_[a-f0-9]{24}$")
HEX_SHA256 = re.compile(r"^[a-f0-9]{64}$")
MAX_JSON_BYTES = 8 * 1024 * 1024
MAX_COMMAND_BYTES = 15 * 1024
MAX_OUTPUT_BYTES = 64 * 1024
MAX_COLLECT_BYTES = 2 * 1024 * 1024 * 1024


def _json_record(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise LocalPackageError(f"{name} must be a JSON object")
    return value


def _read_json(path: Path, name: str) -> dict[str, Any]:
    try:
        if path.is_symlink() or not path.is_file():
            raise ValueError("not a regular file")
        if path.stat().st_size > MAX_JSON_BYTES:
            raise ValueError("file is too large")
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise LocalPackageError(f"Cannot read {name}: {exc}") from exc
    return _json_record(value, name)


def _safe_relative(root: Path, value: Any, name: str) -> Path:
    if not isinstance(value, str) or not value or "\\" in value:
        raise LocalPackageError(f"{name} must be a package-relative POSIX path")
    relative = PurePosixPath(value)
    if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
        raise LocalPackageError(f"{name} leaves the package directory")
    candidate = root
    for part in relative.parts:
        candidate = candidate / part
        if candidate.is_symlink():
            raise LocalPackageError(f"{name} contains a symbolic link")
    try:
        resolved = candidate.resolve()
    except OSError as exc:
        raise LocalPackageError(f"Cannot resolve {name}: {exc}") from exc
    if not resolved.is_relative_to(root):
        raise LocalPackageError(f"{name} leaves the package directory")
    return candidate


def _resolve_package_root(value: Path) -> Path:
    root = value.expanduser().resolve()
    if not root.is_dir() or root.is_symlink():
        raise LocalPackageError("--package must be an extracted package directory")
    if (root / "package.json").is_file():
        return root
    children = [item for item in root.iterdir() if item.is_dir() and not item.is_symlink()]
    matches = [item for item in children if (item / "package.json").is_file()]
    if len(matches) == 1:
        return matches[0].resolve()
    raise LocalPackageError("package.json was not found at the package root")


def _file_hash(path: Path) -> str:
    return sha256_file(path)


def load_package(value: Path) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    """Load and authenticate package metadata and its immutable plan."""

    root = _resolve_package_root(value)
    package = _read_json(root / "package.json", "package.json")
    if package.get("schema_version") != PACKAGE_SCHEMA:
        raise LocalPackageError(f"Unsupported package schema: {package.get('schema_version')!r}")

    plan_path = _safe_relative(root, package.get("plan_path", "plan.json"), "plan_path")
    if not plan_path.is_file():
        raise LocalPackageError("plan.json is missing from the package")
    plan_sha256 = _file_hash(plan_path)
    declared_plan_sha256 = package.get("plan_sha256")
    file_records = package.get("files", [])
    if file_records is not None and not isinstance(file_records, list):
        raise LocalPackageError("package.files must be a list")
    plan_record = None
    for index, item in enumerate(file_records or []):
        record = _json_record(item, f"package.files[{index}]")
        path = _safe_relative(root, record.get("path"), f"package.files[{index}].path")
        if not path.is_file():
            raise LocalPackageError(f"Package file is missing: {record.get('path')}")
        expected = record.get("sha256")
        size = record.get("size_bytes")
        if not isinstance(expected, str) or not HEX_SHA256.fullmatch(expected):
            raise LocalPackageError(f"Invalid SHA-256 for package file: {record.get('path')}")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise LocalPackageError(f"Invalid size for package file: {record.get('path')}")
        if path.stat().st_size != size or _file_hash(path) != expected:
            raise LocalPackageError(f"Package file changed after generation: {record.get('path')}")
        if path == plan_path:
            plan_record = record
    authenticated_plan_hash = declared_plan_sha256 or (plan_record or {}).get("sha256")
    if not isinstance(authenticated_plan_hash, str) or not HEX_SHA256.fullmatch(authenticated_plan_hash):
        raise LocalPackageError("package.json must authenticate plan.json with plan_sha256 or package.files")
    if authenticated_plan_hash != plan_sha256:
        raise LocalPackageError("plan.json SHA-256 does not match package.json")

    plan = _read_json(plan_path, "plan.json")
    _validate_plan(plan, package, plan_sha256)
    return root, package, plan


def _validate_plan(plan: dict[str, Any], package: dict[str, Any], digest: str) -> None:
    steps = plan.get("steps")
    profile = plan.get("profile")
    capture = plan.get("capture")
    if not isinstance(steps, list) or not steps or len(steps) > 100:
        raise LocalPackageError("plan.steps must contain 1-100 steps")
    if not isinstance(profile, dict) or not isinstance(capture, dict):
        raise LocalPackageError("plan.profile and plan.capture are required")
    step_ids = []
    for index, step in enumerate(steps):
        if not isinstance(step, dict) or not isinstance(step.get("step_id"), str):
            raise LocalPackageError(f"plan.steps[{index}] is invalid")
        step_ids.append(step["step_id"])
    if len(step_ids) != len(set(step_ids)):
        raise LocalPackageError("plan step IDs must be unique")
    selected = capture.get("selected_step_ids")
    if not isinstance(selected, list) or not selected or any(item not in step_ids for item in selected):
        raise LocalPackageError("capture.selected_step_ids must reference existing steps")
    collectors = capture.get("collectors")
    if not isinstance(collectors, list) or not collectors or any(item not in {"trace", "hilog", "pmu"} for item in collectors):
        raise LocalPackageError("capture.collectors is invalid")
    blockers = (plan.get("readiness") or {}).get("blockers", []) if isinstance(plan.get("readiness"), dict) else []
    if blockers:
        raise LocalPackageError("The packaged plan still has unresolved operations or selectors")
    plan_id = plan.get("artifact_id") or plan.get("plan_id") or package.get("plan_id")
    if not isinstance(plan_id, str) or not PLAN_ID_PATTERN.fullmatch(plan_id):
        plan_id = "art_" + digest[:24]
    plan["artifact_id"] = plan_id
    plan.setdefault("plan_id", plan_id)


def _bounded(data: bytes, limit: int = MAX_OUTPUT_BYTES) -> str:
    marker = b"\n[output truncated]"
    if len(data) > limit:
        data = data[: max(0, limit - len(marker))] + marker
    return data.decode("utf-8", errors="replace").replace("\r", "").strip()


def _safe_serial(value: str) -> str:
    serial = value.strip()
    if not SERIAL_PATTERN.fullmatch(serial):
        raise LocalPackageError("A valid HDC device serial is required")
    return serial


class DirectHdcDeviceClient:
    """Fixed argv-only HDC adapter for a package running on Windows."""

    operation_timeout_grace_seconds = 4

    def __init__(self, hdc_path: Path, serial: str, run_dir: Path, cancel_event: threading.Event) -> None:
        self.hdc_path = hdc_path.expanduser().resolve()
        if not self.hdc_path.is_file():
            raise LocalPackageError(f"HDC executable was not found: {self.hdc_path}")
        # An unbound package discovers a unique local device before any
        # targeted command. Explicit package/CLI bindings remain exact.
        self.serial = _safe_serial(serial) if serial else ""
        self.run_dir = run_dir
        self.cancel_event = cancel_event
        self.jobs: list[dict[str, Any]] = []
        self._sequence = 0

    def check_cancelled(self) -> None:
        if self.cancel_event.is_set():
            raise CaptureCancelled("Local scenario capture cancelled")

    def _persist_jobs(self) -> None:
        atomic_write_json(self.run_dir / "device_jobs.json", {
            "schema_version": "local_hdc_jobs/v1",
            "device_serial": self.serial,
            "jobs": self.jobs,
        })

    def _run(self, action: str, args: list[str], *, timeout: float, targeted: bool = True,
             command_sha256: str = "", cleanup: bool = False) -> dict[str, Any]:
        # Cancellation stops workload execution, but teardown must retain an
        # independent path to stop collectors, release their lock and restore
        # the screen timeout.  This mirrors the server-side cleanup executor.
        if not cleanup:
            self.check_cancelled()
        self._sequence += 1
        job_id = f"localjob_{secrets.token_hex(12)}"
        argv = [str(self.hdc_path)]
        if targeted:
            argv.extend(["-t", _safe_serial(self.serial)])
        argv.extend(args)
        record: dict[str, Any] = {
            "job_id": job_id,
            "sequence": self._sequence,
            "action": action,
            "started_at": utc_now(),
            "timeout_seconds": timeout,
            "cleanup": cleanup,
            "status": "running",
        }
        if command_sha256:
            record["command_sha256"] = command_sha256
        self.jobs.append(record)
        self._persist_jobs()
        started = time.monotonic()
        process: subprocess.Popen[bytes] | None = None
        stdout = stderr = b""
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
            )
            deadline = started + max(0.1, float(timeout))
            while True:
                if not cleanup and self.cancel_event.is_set():
                    raise CaptureCancelled("Local scenario capture cancelled")
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(argv, timeout)
                try:
                    stdout, stderr = process.communicate(timeout=min(0.2, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
            status = "succeeded" if process.returncode == 0 else "failed"
            result = {
                "return_code": process.returncode,
                "stdout": _bounded(stdout),
                "stderr": _bounded(stderr),
                "timed_out": False,
                "cancelled": False,
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
            }
            record.update(status=status, return_code=process.returncode)
            if status != "succeeded":
                raise LocalPackageError(
                    f"Local HDC {action} failed (exit {process.returncode}): "
                    + (result["stderr"] or result["stdout"] or "no output")[:1500]
                )
            return result
        except subprocess.TimeoutExpired as exc:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    stdout, stderr = process.communicate(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    stdout, stderr = process.communicate()
            record.update(status="timed_out", error=f"HDC operation exceeded {timeout} seconds")
            raise LocalPackageError(f"Local HDC {action} timed out after {timeout} seconds") from exc
        except CaptureCancelled:
            if process is not None and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            record.update(status="cancelled")
            raise
        except Exception as exc:
            if record.get("status") == "running":
                record.update(status="failed", error=str(exc)[:1500])
            raise
        finally:
            record.update(finished_at=utc_now(), duration_ms=round((time.monotonic() - started) * 1000, 3))
            self._persist_jobs()

    def verify_connected(self) -> dict[str, Any]:
        result = self._run("device.discover", ["list", "targets"], timeout=20, targeted=False)
        serials = sorted({line.strip().split()[0] for line in result["stdout"].splitlines()
                          if line.strip() and SERIAL_PATTERN.fullmatch(line.strip().split()[0])})
        if not self.serial:
            if not serials:
                raise LocalPackageError(
                    "No local HDC device was found. Connect the phone to this Windows PC, approve USB debugging, "
                    "and run hdc list targets (or & 'C:\\path\\to\\hdc.exe' list targets) before retrying."
                )
            if len(serials) > 1:
                commands = "; ".join(f'.\\run-capture.ps1 -DeviceSerial "{serial}"' for serial in serials)
                raise LocalPackageError(
                    f"Multiple local HDC devices were found: {', '.join(serials)}. "
                    f"Choose one explicitly and rerun: {commands}"
                )
            self.serial = serials[0]
            self._persist_jobs()
        if self.serial not in serials:
            raise LocalPackageError(
                f"Selected device {self.serial!r} is not present in the local HDC target list: {serials or 'empty'}. "
                "Reconnect the selected device, or explicitly override it with "
                '.\\run-capture.ps1 -DeviceSerial "YOUR_DEVICE_SERIAL".'
            )
        return {"device_serial": self.serial, "targets": serials}

    def hdc_version(self) -> str:
        for args in (["-v"], ["version"]):
            try:
                return self._run("hdc.version", args, timeout=10, targeted=False)["stdout"][:500]
            except LocalPackageError:
                continue
        return "unavailable"

    def shell(self, command: str, timeout: int = 40, *, cleanup: bool = False) -> str:
        encoded = command.encode("utf-8")
        if not encoded or b"\x00" in encoded or len(encoded) > MAX_COMMAND_BYTES:
            raise LocalPackageError("Device shell command is empty, contains NUL or exceeds 15 KiB")
        marker = "__SCENARIO_RC_" + secrets.token_hex(12)
        wrapped = f"( {command}\n); scenario_rc=$?; printf '\\n{marker}%s\\n' \"$scenario_rc\""
        result = self._run(
            "device.execute_shell.cleanup" if cleanup else "device.execute_shell",
            ["shell", wrapped],
            timeout=timeout,
            command_sha256=hashlib.sha256(encoded).hexdigest(),
            cleanup=cleanup,
        )
        output = result["stdout"]
        match = re.search(rf"(?m)^{marker}(\d+)\s*$", output)
        if not match:
            raise LocalPackageError("Device shell did not return its completion marker: " + output[-1500:])
        if int(match[1]):
            raise LocalPackageError(f"Device shell exited with {match[1]}: " + output[:match.start()][-2000:])
        return output[:match.start()].strip()

    def deploy(self, local_file: Path, remote_path: str, sha256: str, *, timeout: int = 180) -> dict[str, Any]:
        if not local_file.is_file() or not HEX_SHA256.fullmatch(sha256) or sha256_file(local_file) != sha256:
            raise LocalPackageError("PMU sampler package file is missing or its SHA-256 changed")
        remote = _safe_remote(remote_path)
        parent = str(PurePosixPath(remote).parent)
        self.shell("mkdir -p " + shlex.quote(parent), timeout=min(timeout, 30))
        self._run("device.deploy_artifact", ["file", "send", str(local_file), remote], timeout=min(timeout, 180))
        self.shell("chmod 700 " + shlex.quote(remote), timeout=min(timeout, 30))
        observed = self.shell(
            f"(sha256sum {shlex.quote(remote)} || toybox sha256sum {shlex.quote(remote)}) 2>/dev/null",
            timeout=min(timeout, 30),
        ).split()
        if not observed or observed[0] != sha256:
            raise LocalPackageError("PMU sampler hash verification failed after local HDC deployment")
        return {
            "status": "succeeded",
            "deployment_verified": True,
            "device_serial": self.serial,
            "remote_path": remote,
            "artifact_sha256": sha256,
            "remote_sha256": observed[0],
            "executable": True,
            "executor": "direct_local_hdc",
        }

    def collect(self, remote: str, destination: Path, *, timeout: int = 600,
                cleanup: bool = True) -> dict[str, Any]:
        remote_path = _safe_remote(remote)
        destination.parent.mkdir(parents=True, exist_ok=True)
        transfer = destination.with_name(f".{destination.name}.{secrets.token_hex(12)}.recv")
        try:
            result = self._run(
                "device.collect_artifact.cleanup" if cleanup else "device.collect_artifact",
                ["file", "recv", remote_path, str(transfer)],
                timeout=min(timeout, 600),
                cleanup=cleanup,
            )
            if transfer.is_symlink() or not transfer.is_file():
                raise LocalPackageError("HDC reported success but did not create the collected file")
            if transfer.stat().st_size > MAX_COLLECT_BYTES:
                raise LocalPackageError("Collected file exceeds the 2 GiB local package limit")
            digest = sha256_file(transfer)
            reused = False
            try:
                # A complete same-directory hard link is an exclusive atomic
                # publication: concurrent/replayed receives cannot overwrite a
                # previously retained artifact or fight over a shared temp name.
                os.link(transfer, destination)
            except FileExistsError:
                if (destination.is_symlink() or not destination.is_file()
                        or destination.stat().st_size != transfer.stat().st_size
                        or sha256_file(destination) != digest):
                    raise LocalPackageError(
                        f"Collected artifact destination already contains different data: {destination.name}")
                reused = True
            return {
                "job_id": self.jobs[-1]["job_id"],
                "status": "succeeded",
                "size_bytes": destination.stat().st_size,
                "sha256": digest,
                "hdc_duration_ms": result["duration_ms"],
                "publication": "idempotent_reuse" if reused else "created",
            }
        finally:
            transfer.unlink(missing_ok=True)


def _safe_remote(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"/data/local/tmp/[A-Za-z0-9._/-]{1,360}", value):
        raise LocalPackageError("Device path must stay under /data/local/tmp")
    path = PurePosixPath(value)
    if ".." in path.parts:
        raise LocalPackageError("Device path traversal is not allowed")
    return value


def _resolve_pmu_bundle(package_root: Path, package: dict[str, Any]) -> Path:
    relative = package.get("pmu_bundle_path", "pmu-sampler")
    packaged = _safe_relative(package_root, relative, "pmu_bundle_path")
    if packaged.is_dir() and (packaged / "manifest.json").is_file():
        return packaged.resolve()
    fallback = APP_ROOT / "tools" / "scenario_capture" / "prebuilt" / "aarch64-linux-ohos"
    return fallback.resolve()


def _target_from_package(package: dict[str, Any], override: str) -> str:
    if override.strip():
        return _safe_serial(override)
    target = package.get("selected_target")
    if isinstance(target, dict) and isinstance(target.get("device_serial"), str) and target["device_serial"]:
        return _safe_serial(target["device_serial"])
    if isinstance(package.get("device_serial"), str) and package["device_serial"]:
        return _safe_serial(package["device_serial"])
    return ""


def _package_identity(package: dict[str, Any]) -> str:
    value = package.get("package_id")
    if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,160}", value):
        return value
    return "localpkg_" + hashlib.sha256(
        json.dumps(package, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:24]


def _install_signal_handlers(cancel_event: threading.Event) -> dict[int, Any]:
    previous: dict[int, Any] = {}

    def cancel(_signum: int, _frame: Any) -> None:
        cancel_event.set()

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(signum, cancel)
    return previous


def _restore_signal_handlers(previous: dict[int, Any]) -> None:
    for signum, handler in previous.items():
        signal.signal(signum, handler)


def _terminal_device_run_ids(root: Path, *, project_id: str, device_serial: str,
                             explicitly_terminal: set[str] | None = None) -> set[str]:
    """Return terminal local runs that may own a recoverable device session.

    The Desktop runner previously always passed an empty set, so an interrupted
    local run could never authorize recovery of its own registered collectors.
    Only exact, terminal, same-project and same-device run records qualify.
    """
    result: set[str] = set()
    runs_root = root / "scenario-capture" / "runs"
    for path in runs_root.glob("capture_*/run.json"):
        if path.is_symlink() or path.parent.is_symlink() or not RUN_ID_PATTERN.fullmatch(path.parent.name):
            continue
        try:
            previous = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        if (isinstance(previous, dict)
                and previous.get("status") in {"failed", "cancelled", "succeeded", "partial"}
                and previous.get("project_id") == project_id
                and previous.get("connection_mode") == "local"
                and previous.get("device_serial") == device_serial
                and previous.get("run_id") == path.parent.name):
            result.add(path.parent.name)
    # The Desktop journal marks an in-flight child as interrupted when the app
    # exits.  Its device-side run.json may still say running because the final
    # write was the interrupted operation itself.  Desktop passes only exact
    # capture IDs from startup-recovered interrupted records for the selected
    # device; never infer these IDs from an arbitrary directory name here.
    for run_id in explicitly_terminal or ():
        if RUN_ID_PATTERN.fullmatch(run_id):
            result.add(run_id)
    return result


def _portable_manifest(manifest: dict[str, Any] | None, run_dir: Path,
                       local_execution: dict[str, Any], plan: dict[str, Any],
                       device: dict[str, Any]) -> dict[str, Any]:
    """Build the strict, location-independent manifest accepted by importers."""

    portable = deepcopy(manifest) if isinstance(manifest, dict) else {}
    portable.update(
        schema_version="scenario_capture_manifest/v1",
        run_id=local_execution["run_id"],
        plan_id=plan["artifact_id"],
        project_id=local_execution["project_id"],
        title=str(plan.get("title") or "本地场景采集")[:300],
        status=local_execution["status"],
        connection_mode="local",
        device_serial=local_execution["device_serial"],
        device=device,
        local_execution={
            "schema_version": RESULT_SCHEMA,
            "package_id": local_execution["package_id"],
            "host": local_execution["host"],
            "run_directory": local_execution["run_directory"],
            "created_at": local_execution["created_at"],
            "finished_at": local_execution["finished_at"],
        },
    )
    portable.pop("managed_path", None)
    steps = portable.get("steps")
    if not isinstance(steps, list):
        portable["steps"] = []

    normalized_files: list[dict[str, Any]] = []
    for index, raw in enumerate(portable.get("files") or []):
        if not isinstance(raw, dict):
            raise LocalPackageError(f"Manifest file entry {index} is not an object")
        entry = deepcopy(raw)
        entry.pop("managed_path", None)
        entry.pop("storage_locations", None)
        relative = entry.get("relative_path")
        path = _safe_result_file(run_dir, relative, f"manifest.files[{index}].relative_path")
        entry.update(
            relative_path=PurePosixPath(relative).as_posix(),
            name=path.name,
            size_bytes=path.stat().st_size,
            sha256=sha256_file(path),
        )
        normalized_files.append(entry)

    # A preparation failure can happen before the fixed runner has emitted an
    # evidence manifest.  Keep that terminal run importable by declaring the
    # local execution diagnostic as its sole evidence file.
    if not normalized_files:
        diagnostic = run_dir / "local-execution.json"
        if not diagnostic.is_file():
            raise LocalPackageError("The terminal local run has no evidence or execution diagnostic")
        normalized_files.append({
            "relative_path": "local-execution.json",
            "name": "local-execution.json",
            "kind": "local_execution",
            "step_id": "",
            "size_bytes": diagnostic.stat().st_size,
            "sha256": sha256_file(diagnostic),
        })
    portable["files"] = normalized_files
    return portable


def _safe_result_file(run_dir: Path, relative: Any, name: str) -> Path:
    if not isinstance(relative, str) or not relative or "\\" in relative:
        raise LocalPackageError(f"{name} must be a run-relative POSIX path")
    value = PurePosixPath(relative)
    if value.is_absolute() or any(part in {"", ".", ".."} for part in value.parts):
        raise LocalPackageError(f"{name} leaves the run directory")
    path = run_dir.joinpath(*value.parts)
    if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(run_dir):
        raise LocalPackageError(f"Declared capture file is missing or unsafe: {relative}")
    return path


def _write_archive(run_dir: Path, archive: Path, manifest: dict[str, Any]) -> None:
    temporary = archive.with_name(f".{archive.name}.{secrets.token_hex(12)}.partial")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, allowZip64=True) as output:
            output.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            for index, entry in enumerate(manifest["files"]):
                relative = entry["relative_path"]
                path = _safe_result_file(run_dir, relative, f"manifest.files[{index}].relative_path")
                output.write(path, relative)
        digest = sha256_file(temporary)
        try:
            os.link(temporary, archive)
        except FileExistsError:
            if (archive.is_symlink() or not archive.is_file()
                    or archive.stat().st_size != temporary.stat().st_size
                    or sha256_file(archive) != digest):
                raise LocalPackageError("Capture archive name is already occupied by different data")
    finally:
        temporary.unlink(missing_ok=True)


def _finalize_manifest(manifest: dict[str, Any], run_dir: Path, output_root: Path,
                       device: dict[str, Any], provenance: dict[str, Any],
                       project_id: str) -> None:
    for entry in manifest.get("files", []):
        relative = entry.get("relative_path")
        if not isinstance(relative, str):
            continue
        path = run_dir / PurePosixPath(relative)
        if not path.is_file() or not path.resolve().is_relative_to(run_dir):
            continue
        entry["managed_path"] = path.relative_to(output_root).as_posix()
        if entry.get("kind") == "trace":
            locations = [item for item in entry.get("storage_locations", []) if isinstance(item, dict)]
            locations.append({
                "kind": "windows_local",
                "local_path": str(path),
                "storage_host": socket.gethostname(),
                "connection_mode": "local",
                "label": "Windows 本地采集原始文件",
                "status": "available",
                "can_download": False,
                "can_delete": False,
                "observed_at": utc_now(),
            })
            entry["storage_locations"] = locations
    manifest.update(
        project_id=project_id,
        connection_mode="local",
        device_serial=device["serial"],
        device=device,
        execution_provenance=provenance,
    )
    atomic_write_json(run_dir / "manifest.json", manifest)


def run_package(package_path: Path, hdc_path: Path, device_serial: str, output_root: Path,
                *, prepare: Callable[..., tuple[dict[str, Any], dict[str, Any]]] = prepare_pmu_sampler,
                execute: Callable[..., dict[str, Any]] = execute_plan,
                cancel_event: threading.Event | None = None,
                progress_callback: Callable[[dict[str, Any]], None] | None = None,
                install_signal_handlers: bool = True,
                explicitly_terminal_run_ids: set[str] | None = None,
                private_device: bool = False) -> dict[str, Any]:
    package_root, package, plan = load_package(package_path)
    serial = _target_from_package(package, device_serial)
    root = output_root.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink():
        raise LocalPackageError("--output-root cannot be a symbolic link")
    run_id = "capture_" + secrets.token_hex(12)
    if not RUN_ID_PATTERN.fullmatch(run_id):
        raise AssertionError("invalid generated run ID")
    run_dir = root / "scenario-capture" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=False)
    cancel_event = cancel_event if cancel_event is not None else threading.Event()
    client = DirectHdcDeviceClient(hdc_path, serial, run_dir, cancel_event)
    # Device selection must finish before building a run identity: an unbound
    # package may not silently select the first of several phones.
    discovery = client.verify_connected() if not serial else None
    serial = client.serial
    previous_handlers = (_install_signal_handlers(cancel_event)
                         if install_signal_handlers and threading.current_thread() is threading.main_thread() else {})
    created_at = utc_now()
    package_id = _package_identity(package)
    project_id = str(package.get("project_id") or plan.get("project_id") or "local-default")[:120]
    source_target = package.get("selected_target") if isinstance(package.get("selected_target"), dict) else {}
    device = {
        "connection_mode": "local",
        "executor": "direct_windows_hdc",
        "bridge_id": "",
        "serial": serial,
        "source_bridge_id": str(source_target.get("bridge_id") or "")[:160],
    }
    execution_provenance: dict[str, Any] = {
        "connection_mode": "local",
        "requested_connection_mode": str(source_target.get("connection_mode") or "bridge"),
        "executor": "direct_windows_hdc",
        "execution_mode": "windows_local_package",
        "device_access_mode": "private" if private_device else "shared",
        "host_platform": sys.platform,
        "host_system": platform.system(),
        "host_shell": False,
        "runner_version": RUNNER_VERSION,
        "pmu_validation_schema": pmu_validation_schema((plan.get("capture") or {}).get("pmu", {}).get("group")),
        "package_id": package_id,
    }
    run: dict[str, Any] = {
        "schema_version": "scenario_capture_run/v1",
        "run_id": run_id,
        "plan_id": plan["artifact_id"],
        "project_id": project_id,
        "title": str(plan.get("title") or "Windows 本地场景采集")[:300],
        "status": "running",
        "created_at": created_at,
        "connection_mode": "local",
        "requested_connection_mode": execution_provenance["requested_connection_mode"],
        "bridge_id": "",
        "device_serial": serial,
        "device": device,
        "execution_provenance": execution_provenance,
        "source_connector_id": str(plan.get("source_connector_id") or "unbound"),
        "source_snapshot_id": str(plan.get("source_snapshot_id") or ""),
        "manifest": None,
    }
    atomic_write_json(run_dir / "plan.json", plan)
    atomic_write_json(run_dir / "package.json", package)
    atomic_write_json(run_dir / "run.json", run)

    def report_progress(stage: str, message: str, **details: Any) -> None:
        if progress_callback is not None:
            progress_callback({"stage": stage, "message": message, "run_id": run_id,
                "run_directory": str(run_dir), "run_path": str(run_dir / "run.json"),
                "status": run["status"], "device_serial": serial, **details})

    try:
        report_progress("device_preparation", "正在核验所选本机设备。")
        discovery = discovery or client.verify_connected()
        execution_provenance.update(
            hdc_path=str(client.hdc_path),
            hdc_sha256=sha256_file(client.hdc_path),
            hdc_version=client.hdc_version(),
            target_count=len(discovery["targets"]),
        )
        bundle_root = _resolve_pmu_bundle(package_root, package)

        def preparation_progress(value: dict[str, Any]) -> None:
            run.update(pmu_preparation=value, updated_at=utc_now())
            atomic_write_json(run_dir / "pmu_preparation.json", value)
            atomic_write_json(run_dir / "run.json", run)
            report_progress("pmu_preparation", str(value.get("message") or "正在准备通用线程 PMU 采样器。"),
                            pmu_preparation=value)

        execution_plan, preparation = prepare(plan, client, bundle_root=bundle_root,
                                              progress=preparation_progress)
        run["pmu_preparation"] = preparation

        def capture_progress(value: dict[str, Any]) -> None:
            run.update(status=value.get("status", "running"), manifest=value, updated_at=utc_now())
            atomic_write_json(run_dir / "run.json", run)
            steps = value.get("steps") or []
            step = steps[-1] if steps and isinstance(steps[-1], dict) else {}
            report_progress("capture", str(step.get("title") or "正在执行场景采集。"),
                step_id=step.get("step_id"), step_title=step.get("title"), step_status=step.get("status"),
                collector_status=step.get("collector_status", {}),
                completed_steps=sum(item.get("status") in {"succeeded", "partial", "completed"}
                                    for item in steps if isinstance(item, dict)),
                quality=value.get("quality", {}), error=step.get("error") or value.get("error"))

        manifest = execute(execution_plan, client, run_dir, run_id, capture_progress,
                           terminal_run_ids=_terminal_device_run_ids(
                               root, project_id=project_id, device_serial=serial,
                               explicitly_terminal=explicitly_terminal_run_ids),
                           private_device=private_device)
        _finalize_manifest(manifest, run_dir, root, device, execution_provenance, project_id)
        run.update(
            status=manifest.get("status", "failed"),
            manifest=manifest,
            updated_at=utc_now(),
            finished_at=utc_now(),
        )
    except CaptureCancelled as exc:
        run.update(status="cancelled", error={"type": type(exc).__name__, "message": str(exc)},
                   updated_at=utc_now(), finished_at=utc_now())
    except Exception as exc:
        run.update(status="failed", error={"type": type(exc).__name__, "message": str(exc)[:3000]},
                   updated_at=utc_now(), finished_at=utc_now())
    finally:
        _restore_signal_handlers(previous_handlers)

    local_execution = {
        "schema_version": RESULT_SCHEMA,
        "package_id": package_id,
        "project_id": project_id,
        "origin_plan_id": str(package.get("plan_id") or plan["artifact_id"]),
        "run_id": run_id,
        "status": run["status"],
        "device_serial": serial,
        "created_at": created_at,
        "finished_at": run.get("finished_at", utc_now()),
        "run_directory": str(run_dir),
        "host": socket.gethostname(),
        "execution_provenance": execution_provenance,
    }
    atomic_write_json(run_dir / "local-execution.json", local_execution)
    archive = root / f"{run_id}.capture.zip"
    run["result_archive"] = str(archive)
    atomic_write_json(run_dir / "run.json", run)
    portable_manifest = _portable_manifest(
        run.get("manifest") if isinstance(run.get("manifest"), dict) else None,
        run_dir,
        local_execution,
        plan,
        device,
    )
    # Use one terminal evidence set for the directory and ZIP. The Runner may
    # have emitted a manifest before cancellation/failure without producing a
    # file; _portable_manifest supplies the execution diagnostic in that case.
    # A dict's presence alone does not mean it is a complete terminal manifest.
    run["manifest"] = deepcopy(portable_manifest)
    _finalize_manifest(run["manifest"], run_dir, root, device, execution_provenance, project_id)
    atomic_write_json(run_dir / "run.json", run)
    report_progress("archive", "正在整理采集清单和结果归档。")
    _write_archive(run_dir, archive, portable_manifest)
    manifest_errors = (run.get("manifest") or {}).get("quality", {}).get("errors", [])
    result = {**local_execution, "archive": str(archive), "run_directory": str(run_dir),
              "manifest_path": str(run_dir / "manifest.json"), "run_path": str(run_dir / "run.json"),
              "error": run.get("error") or (manifest_errors[0] if manifest_errors else None)}
    report_progress("finished", "采集任务已结束。", archive=str(archive),
                    manifest_path=result["manifest_path"], error=run.get("error"))
    return result


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Execute an extracted scenario-capture package with the Windows PC's local HDC.")
    parser.add_argument("--package", required=True, type=Path, help="Extracted package directory containing package.json and plan.json")
    parser.add_argument("--hdc", required=True, type=Path, help="Local hdc.exe path")
    parser.add_argument("--device-serial", default="", help="HDC serial; uses package binding or the unique local HDC device")
    parser.add_argument("--output-root", required=True, type=Path, help="Directory that receives the standard run and .capture.zip")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = run_package(args.package, args.hdc, args.device_serial, args.output_root)
    except LocalPackageError as exc:
        print(f"Local capture package error: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"Local capture failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"succeeded", "partial"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
