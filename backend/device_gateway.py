"""Typed device gateway shared by local HDC and remote Windows bridges.

The gateway is deliberately the only place where a backend Agent may cross
from immutable build artifacts to a physical OpenHarmony target.  The Agent
never receives a workstation executable path, a shell, or a Bridge secret.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import posixpath
import re
import secrets
import signal
import subprocess
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from .core import JsonRegistry, PlatformError, canonical_json_bytes, sha256_file, utc_now
from .device_verification import (
    MAX_COMMAND_OUTPUT_BYTES,
    MAX_REMOTE_SHELL_COMMAND_BYTES,
    REMOTE_SHELL_MAX_TIMEOUT_SECONDS,
    _CappedPipeDrain,
    _available_devices,
    _bounded,
    _clean_output,
    _safe_serial,
    discover_devices,
    probe_device,
    resolve_hdc_binary,
)


BRIDGE_ID_PATTERN = re.compile(r"bridge_[a-f0-9]{24}")
LEASE_ID_PATTERN = re.compile(r"lease_[a-f0-9]{24}")
JOB_ID_PATTERN = re.compile(r"devjob_[a-f0-9]{24}")
LOGICAL_DEVICE_PATTERN = re.compile(r"device_[A-Za-z0-9._-]{1,120}")
REMOTE_PATH_PATTERN = re.compile(r"^/data/local/tmp/[A-Za-z0-9._/-]{1,360}$")
MAX_JOB_SECONDS = 900
BRIDGE_HEARTBEAT_GRACE_SECONDS = 30
DEFAULT_LEASE_SECONDS = 2 * 60 * 60
MAX_LEASE_SECONDS = 24 * 60 * 60
LEASE_REUSE_MIN_REMAINING_SECONDS = 5 * 60
MAX_COLLECT_BYTES = 32 * 1024 * 1024
MAX_GATEWAY_EVENT_BYTES = 128 * 1024
MAX_POLL_QUEUE_MESSAGES = 512
WEBSOCKET_PROTOCOL_VERSION = "device-bridge-ws/v1"
HTTP_POLL_PROTOCOL_VERSION = "device-bridge-http-poll/v1"
SUPPORTED_BRIDGE_PROTOCOLS = frozenset({WEBSOCKET_PROTOCOL_VERSION, HTTP_POLL_PROTOCOL_VERSION})
CONNECTION_ID_PATTERN = re.compile(r"^connection_[a-f0-9]{24,64}$")

DEVICE_ACTIONS = frozenset(
    {
        "device.discover",
        "device.probe",
        "device.deploy_artifact",
        "device.execute_shell",
        "device.collect_artifact",
        "device.cancel_job",
        "trace.library.list",
        "trace.library.delete",
    }
)

WRITE_DEVICE_ACTIONS = frozenset({"device.deploy_artifact", "device.execute_shell"})
TERMINAL_JOB_STATES = frozenset({"succeeded", "failed", "timed_out", "cancelled"})


def _record(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any, limit: int = 1024) -> str:
    return value.strip()[:limit] if isinstance(value, str) else ""


def _new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


def normalize_deployment_result(
    result: dict[str, Any],
    *,
    expected_sha256: str,
    expected_remote_path: str,
    expected_serial: str,
) -> dict[str, Any]:
    """Reject a deployment success that lacks device-side proof."""

    normalized = dict(result)
    status = _text(normalized.get("status"), 40)
    if status != "succeeded":
        normalized.setdefault("deployment_verified", False)
        raw_error = normalized.get("error")
        if isinstance(raw_error, str) and raw_error.strip():
            normalized["error"] = {
                "code": "DEVICE_DEPLOYMENT_FAILED",
                "message": raw_error.strip()[:1200],
            }
        elif status == "failed" and not isinstance(raw_error, dict):
            normalized["error"] = {
                "code": "DEVICE_DEPLOYMENT_FAILED",
                "message": f"设备部署在 {_text(normalized.get('stage'), 120) or 'unknown'} 阶段失败。",
            }
        return normalized

    violations: list[str] = []
    if normalized.get("deployment_verified") is not True:
        violations.append("deployment_verified_not_true")
    if normalized.get("executable") is not True:
        violations.append("executable_not_true")
    if _text(normalized.get("device_serial"), 160) != expected_serial:
        violations.append("device_serial_mismatch")
    if _text(normalized.get("remote_path"), 512) != expected_remote_path:
        violations.append("remote_path_mismatch")
    if _text(normalized.get("artifact_sha256"), 80) != expected_sha256:
        violations.append("artifact_sha256_mismatch")
    if _text(normalized.get("remote_sha256"), 80) != expected_sha256:
        violations.append("remote_sha256_mismatch")
    if not violations:
        return normalized

    normalized.update({
        "status": "failed",
        "reported_status": "succeeded",
        "stage": "deployment_contract_verification",
        "deployment_verified": False,
        "error": {
            "code": "DEVICE_DEPLOYMENT_NOT_VERIFIED",
            "message": "设备部署执行器未返回可证明文件已落到目标手机的完整校验结果。",
            "details": {
                "violations": violations,
                "expected_device_serial": expected_serial,
                "expected_remote_path": expected_remote_path,
                "expected_sha256": expected_sha256,
            },
        },
    })
    return normalized


def _now_deadline(seconds: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_deadline(value: str) -> float:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError as exc:
        raise PlatformError("DEVICE_JOB_INVALID", "Device Job deadline 无效。", status_code=422) from exc


def _safe_remote_path(value: Any) -> str:
    path = _text(value, 512)
    if not REMOTE_PATH_PATTERN.fullmatch(path) or "/../" in path or path.endswith("/.."):
        raise PlatformError("DEVICE_REMOTE_PATH_INVALID", "设备路径必须位于 /data/local/tmp/ 管理目录。", status_code=422)
    return path


class DeviceExecutor(Protocol):
    """Common contract used by the local adapter and an outbound Bridge."""

    def discover(self, *, timeout_seconds: int | None = None) -> dict[str, Any]: ...

    def probe(self, serial: str) -> dict[str, Any]: ...

    def deploy(self, *, serial: str, local_file: Path, expected_sha256: str, remote_path: str, timeout_seconds: int) -> dict[str, Any]: ...

    def execute_shell(self, *, serial: str, command: str, timeout_seconds: int) -> dict[str, Any]: ...

    def collect(self, *, serial: str, remote_path: str, destination: Path, timeout_seconds: int) -> dict[str, Any]: ...

    def cancel(self, job_id: str) -> dict[str, Any]: ...


class LocalHdcDeviceExecutor:
    """Compatibility executor for single-machine macOS/Linux/Windows installs."""

    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()
        self._lock = threading.RLock()
        self._active_processes: dict[str, set[subprocess.Popen[bytes]]] = {}
        self._cancelled_runs: set[str] = set()

    @staticmethod
    def _terminate_process(process: subprocess.Popen[bytes]) -> None:
        """Terminate the fixed HDC child without ever invoking a host shell."""

        if process.poll() is not None:
            return
        try:
            if os.name == "posix":
                # HDC can spawn helper children.  Every tracked invocation is
                # its own session, so cancellation reaches that small process
                # tree rather than leaving a detached child behind.
                os.killpg(process.pid, signal.SIGTERM)
            else:  # pragma: no cover - exercised on a Windows local adapter.
                process.terminate()
        except (OSError, ProcessLookupError):
            return

    def _run_hdc_tracked(
        self,
        hdc_binary: Path,
        command: tuple[str, ...],
        *,
        serial: str | None = None,
        timeout_seconds: int | float,
        agent_run_id: str = "",
    ) -> dict[str, Any]:
        """Run one fixed HDC argv and make it cancellable by Agent Run.

        The command is still a typed DeviceExecutor operation.  ``command`` is
        never parsed by a host shell; only the remote-shell payload itself is
        passed as one final HDC argument.
        """

        run_id = _text(agent_run_id, 120)
        if run_id:
            with self._lock:
                if run_id in self._cancelled_runs:
                    return {
                        "return_code": None,
                        "stdout": "",
                        "stderr": "所属 Agent Run 已取消，HDC 命令未启动。",
                        "timed_out": False,
                        "cancelled": True,
                    }
        argv = [str(hdc_binary)]
        if serial is not None:
            argv.extend(["-t", _safe_serial(serial)])
        argv.extend(command)
        process: subprocess.Popen[bytes] | None = None
        stdout_drain: _CappedPipeDrain | None = None
        stderr_drain: _CappedPipeDrain | None = None
        stdout_thread: threading.Thread | None = None
        stderr_thread: threading.Thread | None = None
        timed_out = False
        try:
            process = subprocess.Popen(
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=False,
                shell=False,
                start_new_session=os.name == "posix",
            )
            assert process.stdout is not None
            assert process.stderr is not None
            stdout_drain = _CappedPipeDrain(process.stdout, limit=MAX_COMMAND_OUTPUT_BYTES)
            stderr_drain = _CappedPipeDrain(process.stderr, limit=MAX_COMMAND_OUTPUT_BYTES)
            stdout_thread = threading.Thread(target=stdout_drain.drain, name="local-hdc-stdout", daemon=True)
            stderr_thread = threading.Thread(target=stderr_drain.drain, name="local-hdc-stderr", daemon=True)
            stdout_thread.start()
            stderr_thread.start()
            if run_id:
                with self._lock:
                    self._active_processes.setdefault(run_id, set()).add(process)
                    cancelled_before_registration = run_id in self._cancelled_runs
                if cancelled_before_registration:
                    # A cancel may race with process creation.  Register first
                    # so it is visible to diagnostics, then terminate before
                    # it can become an untracked local HDC child.
                    self._terminate_process(process)
            try:
                process.wait(timeout=max(0.001, min(float(timeout_seconds), MAX_JOB_SECONDS)))
            except subprocess.TimeoutExpired:
                timed_out = True
                self._terminate_process(process)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    try:
                        process.kill()
                    except OSError:
                        pass
                    process.wait()
        except FileNotFoundError as exc:
            raise PlatformError("DEVICE_TOOL_UNAVAILABLE", "真机验证所需的本机 HDC 不可用。", status_code=503) from exc
        finally:
            if run_id and process is not None:
                with self._lock:
                    active = self._active_processes.get(run_id)
                    if active is not None:
                        active.discard(process)
                        if not active:
                            self._active_processes.pop(run_id, None)
            if stdout_thread is not None:
                stdout_thread.join(timeout=3)
            if stderr_thread is not None:
                stderr_thread.join(timeout=3)
        cancelled = bool(run_id and run_id in self._cancelled_runs)
        return {
            "return_code": process.returncode if process is not None else None,
            "stdout": _bounded(_clean_output(stdout_drain.text() if stdout_drain else ""), MAX_COMMAND_OUTPUT_BYTES),
            "stderr": _bounded(_clean_output(stderr_drain.text() if stderr_drain else ""), MAX_COMMAND_OUTPUT_BYTES),
            "timed_out": timed_out,
            "cancelled": cancelled,
        }

    def cancel_run(self, agent_run_id: str) -> dict[str, Any]:
        """Terminate active local HDC children belonging to an Agent Run."""

        run_id = _text(agent_run_id, 120)
        if not run_id:
            return {"status": "acknowledged", "active_process_count": 0}
        with self._lock:
            self._cancelled_runs.add(run_id)
            active = list(self._active_processes.get(run_id, set()))
        for process in active:
            self._terminate_process(process)
        return {
            "status": "cancel_requested",
            "active_process_count": len(active),
            "detail": "已向当前 Agent Run 的本机固定 HDC 子进程发送终止请求。",
        }

    def discover(self, *, timeout_seconds: int | None = None) -> dict[str, Any]:
        return discover_devices(self.workspace_root) if timeout_seconds is None else discover_devices(self.workspace_root, timeout_seconds=timeout_seconds)

    def probe(self, serial: str) -> dict[str, Any]:
        return probe_device(self.workspace_root, serial)

    @staticmethod
    def _remaining_operation_seconds(deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise PlatformError("DEVICE_COMMAND_TIMEOUT", "本机 HDC 操作预算已耗尽，后续命令未启动。", status_code=504)
        return remaining

    def _ensure_connected(self, serial: str, *, deadline: float | None = None, agent_run_id: str = "") -> Path:
        normalized = _safe_serial(serial)
        hdc = resolve_hdc_binary(self.workspace_root)
        if deadline is None:
            devices = _available_devices(hdc)
        else:
            # Connection verification and the selected command share one
            # budget and cancellation owner. A stalled targets probe must not
            # add an untracked twenty seconds before every UI operation.
            result = self._run_hdc_tracked(hdc, ("list", "targets"),
                timeout_seconds=min(20, self._remaining_operation_seconds(deadline)), agent_run_id=agent_run_id)
            if result["cancelled"]:
                raise PlatformError("DEVICE_COMMAND_CANCELLED", "本次本机 HDC 操作已取消。", status_code=409)
            if result["timed_out"]:
                raise PlatformError("DEVICE_COMMAND_TIMEOUT", "本机 HDC 设备连接检查超时。", status_code=504)
            devices = [line.strip().split()[0] for line in result["stdout"].splitlines() if line.strip()] if result["return_code"] == 0 else []
        if normalized not in devices:
            raise PlatformError("DEVICE_NOT_CONNECTED", "目标设备已从本机 HDC target 列表断开。", status_code=409, details={"device_serial": normalized})
        return hdc

    def deploy(
        self,
        *,
        serial: str,
        local_file: Path,
        expected_sha256: str,
        remote_path: str,
        timeout_seconds: int,
        agent_run_id: str = "",
    ) -> dict[str, Any]:
        normalized = _safe_serial(serial)
        remote = _safe_remote_path(remote_path)
        if not local_file.is_file() or sha256_file(local_file) != expected_sha256:
            raise PlatformError("DEVICE_DEPLOY_ARTIFACT_INVALID", "待部署构建产物不存在或哈希不匹配。", status_code=409)
        hdc = self._ensure_connected(normalized)
        directory = posixpath.dirname(remote)
        mkdir = self._run_hdc_tracked(hdc, ("shell", f"mkdir -p {directory}"), serial=normalized, timeout_seconds=min(timeout_seconds, 90), agent_run_id=agent_run_id)
        if mkdir["cancelled"]:
            return {"status": "cancelled", "device_serial": normalized, "stage": "mkdir", "hdc": mkdir}
        if mkdir["return_code"] != 0:
            return {
                "status": "failed",
                "device_serial": normalized,
                "stage": "mkdir",
                "return_code": mkdir["return_code"],
                "stdout": _bounded(mkdir["stdout"], 4096),
                "stderr": _bounded(mkdir["stderr"], 4096),
            }
        uploaded = self._run_hdc_tracked(hdc, ("file", "send", str(local_file), remote), serial=normalized, timeout_seconds=min(timeout_seconds, 240), agent_run_id=agent_run_id)
        if uploaded["cancelled"]:
            return {"status": "cancelled", "device_serial": normalized, "stage": "file_send", "hdc": uploaded}
        if uploaded["return_code"] != 0:
            return {
                "status": "failed",
                "device_serial": normalized,
                "stage": "file_send",
                "return_code": uploaded["return_code"],
                "stdout": _bounded(uploaded["stdout"], 4096),
                "stderr": _bounded(uploaded["stderr"], 4096),
            }
        executable = self._run_hdc_tracked(
            hdc,
            ("shell", f"chmod 700 {remote}"),
            serial=normalized,
            timeout_seconds=min(timeout_seconds, 60),
            agent_run_id=agent_run_id,
        )
        if executable["cancelled"]:
            return {"status": "cancelled", "device_serial": normalized, "stage": "chmod", "hdc": executable}
        if executable["return_code"] != 0:
            return {
                "status": "failed",
                "device_serial": normalized,
                "stage": "chmod",
                "return_code": executable["return_code"],
                "stdout": _bounded(executable["stdout"], 4096),
                "stderr": _bounded(executable["stderr"], 4096),
            }
        # The remote checksum is a fixed gateway operation, not model-provided
        # shell.  A target without sha256sum does not qualify as a verified
        # deployment, so it cannot be executed through this result.
        verify = self._run_hdc_tracked(
            hdc,
            ("shell", f"(sha256sum {remote} || toybox sha256sum {remote}) 2>/dev/null"),
            serial=normalized,
            timeout_seconds=min(timeout_seconds, 90),
            agent_run_id=agent_run_id,
        )
        if verify["cancelled"]:
            return {"status": "cancelled", "device_serial": normalized, "stage": "remote_hash_verification", "hdc": verify}
        observed = verify["stdout"].split()
        observed_hash = observed[0] if observed else ""
        if verify["return_code"] != 0 or observed_hash != expected_sha256:
            return {
                "status": "failed",
                "device_serial": normalized,
                "stage": "remote_hash_verification",
                "expected_sha256": expected_sha256,
                "observed_sha256": observed_hash or None,
                "return_code": verify["return_code"],
                "stdout": _bounded(verify["stdout"], 4096),
                "stderr": _bounded(verify["stderr"], 4096),
            }
        return {
            "status": "succeeded",
            "device_serial": normalized,
            "remote_path": remote,
            "artifact_sha256": expected_sha256,
            "remote_sha256": observed_hash,
            "deployment_verified": True,
            "executable": True,
        }

    def execute_shell(self, *, serial: str, command: str, timeout_seconds: int, agent_run_id: str = "") -> dict[str, Any]:
        normalized = _safe_serial(serial)
        if not command or "\x00" in command or len(command.encode("utf-8", errors="strict")) > MAX_REMOTE_SHELL_COMMAND_BYTES:
            raise PlatformError("DEVICE_REMOTE_SHELL_INVALID", "远端 Shell 命令为空、包含 NUL 或超过大小上限。", status_code=422)
        if not 1 <= int(timeout_seconds) <= REMOTE_SHELL_MAX_TIMEOUT_SECONDS:
            raise PlatformError("DEVICE_REMOTE_SHELL_TIMEOUT_INVALID", "远端 Shell 超时必须在允许范围内。", status_code=422)
        started = time.monotonic()
        deadline = started + timeout_seconds
        hdc = self._ensure_connected(normalized, deadline=deadline, agent_run_id=agent_run_id)
        result = self._run_hdc_tracked(
            hdc,
            ("shell", command),
            serial=normalized,
            timeout_seconds=self._remaining_operation_seconds(deadline),
            agent_run_id=agent_run_id,
        )
        status = "cancelled" if result["cancelled"] else "timed_out" if result["timed_out"] else "succeeded" if result["return_code"] == 0 else "failed"
        return {
            "schema_version": "device_remote_shell_run/v1",
            "device": {"serial": normalized},
            "command": command,
            "command_sha256": hashlib.sha256(command.encode("utf-8")).hexdigest(),
            "status": status,
            "execution": {
                "duration_ms": round((time.monotonic() - started) * 1000, 3),
                "return_code": result["return_code"],
                "timed_out": result["timed_out"],
                "cancelled": result["cancelled"],
                "timeout_seconds": timeout_seconds,
                "stdout": result["stdout"],
                "stderr": result["stderr"],
            },
            "workstation_execution_boundary": {
                "host_shell": False,
                "hdc_binary_source": "server_resolved_fixed_path",
                "device_serial_source": "previously_probed_artifact",
                "host_command_input": "fixed_hdc_argv_plus_one_remote_shell_argument",
                "host_working_directory": None,
            },
            "device_execution_boundary_zh": "全授权页面助手可选择设备侧 Shell 命令；平台只约束工作站侧进程创建、设备绑定、超时、输出大小和审计，不把该命令解释为工作站命令。",
            "summary_zh": "本机 HDC 设备侧命令已完成。" if status == "succeeded" else "本机 HDC 设备侧命令未成功完成；请以本次真实输出为准。",
        }

    def collect(self, *, serial: str, remote_path: str, destination: Path, timeout_seconds: int, agent_run_id: str = "", max_bytes: int = MAX_COLLECT_BYTES) -> dict[str, Any]:
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or not 1 <= max_bytes <= 2 * 1024 * 1024 * 1024:
            raise PlatformError("DEVICE_COLLECT_LIMIT_INVALID", "设备拉取文件大小上限必须在 1 byte 至 2 GiB 范围内。", status_code=422)
        normalized = _safe_serial(serial)
        remote = _safe_remote_path(remote_path)
        deadline = time.monotonic() + max(1, min(timeout_seconds, 240))
        hdc = self._ensure_connected(normalized, deadline=deadline, agent_run_id=agent_run_id)
        destination.parent.mkdir(parents=True, exist_ok=True)
        received = self._run_hdc_tracked(hdc, ("file", "recv", remote, str(destination)), serial=normalized,
            timeout_seconds=self._remaining_operation_seconds(deadline), agent_run_id=agent_run_id)
        if received["cancelled"]:
            return {"status": "cancelled", "device_serial": normalized, "hdc": received}
        if received["timed_out"]:
            return {"status": "timed_out", "device_serial": normalized, "hdc": received}
        if received["return_code"] != 0 or not destination.is_file():
            return {
                "status": "failed",
                "device_serial": normalized,
                "return_code": received["return_code"],
                "stdout": _bounded(received["stdout"], 4096),
                "stderr": _bounded(received["stderr"], 4096),
            }
        if destination.stat().st_size > max_bytes:
            destination.unlink(missing_ok=True)
            raise PlatformError("DEVICE_COLLECT_TOO_LARGE", f"设备拉取文件超过 {max_bytes} bytes 上限。", status_code=413)
        return {
            "status": "succeeded",
            "device_serial": normalized,
            "local_path": str(destination),
            "bytes": destination.stat().st_size,
            "sha256": sha256_file(destination),
        }

    def cancel(self, job_id: str) -> dict[str, Any]:
        return {"status": "acknowledged", "job_id": job_id, "detail": "本机 HDC 操作按 Agent Run 统一取消；单独 Job 没有跨请求持久句柄。"}


@dataclass
class BridgeSession:
    bridge_id: str
    send: Callable[[dict[str, Any]], None]
    connected_at: float
    last_heartbeat: float
    capabilities: dict[str, Any]
    transport: str = "websocket"
    connection_id: str = ""


@dataclass
class PendingJob:
    event: threading.Event
    result: dict[str, Any] | None = None


class DeviceGateway:
    """Lease-aware job dispatcher for a local adapter or outbound Bridge."""

    def __init__(self, *, workspace_root: Path, state_root: Path) -> None:
        self.workspace_root = workspace_root.resolve()
        self.state_root = state_root.resolve()
        self.local_executor = LocalHdcDeviceExecutor(self.workspace_root)
        self.registry = JsonRegistry(
            self.state_root / "device-gateway" / "registry.json",
            {
                "schema_version": "device_gateway_registry/v1",
                "bridges": {},
                "pairing_codes": {},
                "leases": {},
                "jobs": {},
                "idempotency_index": {},
            },
        )
        self.spool_root = self.state_root / "device-gateway" / "spool"
        self.spool_root.mkdir(parents=True, exist_ok=True)
        self.event_log = self.state_root / "device-gateway" / "device_bridge_events.jsonl"
        self._sessions: dict[str, BridgeSession] = {}
        self._poll_messages: dict[str, deque[dict[str, Any]]] = {}
        self._pending_jobs: dict[str, PendingJob] = {}
        self._download_grants: dict[str, dict[str, Any]] = {}
        self._last_audited_heartbeats: dict[str, float] = {}
        # Registered by the application after AgentRunStore is available. This
        # keeps the gateway transport-only while allowing Bridge progress to
        # appear in the owning Agent Run's SSE stream.
        self._run_event_sink: Callable[[dict[str, Any]], None] | None = None
        self._lock = threading.RLock()

    def set_run_event_sink(self, sink: Callable[[dict[str, Any]], None] | None) -> None:
        with self._lock:
            self._run_event_sink = sink

    @staticmethod
    def _event_safe(value: Any, *, depth: int = 0) -> Any:
        """Bound gateway audit records without retaining Bridge credentials."""

        if depth > 12:
            return "[payload nesting limit]"
        if isinstance(value, dict):
            return {
                str(key): "[redacted]" if str(key).casefold() in {"bridge_secret", "signature", "token", "authorization", "x-bridge-secret", "pairing_code", "x-pairing-code"}
                else DeviceGateway._event_safe(item, depth=depth + 1)
                for key, item in list(value.items())[:128]
            }
        if isinstance(value, list):
            return [DeviceGateway._event_safe(item, depth=depth + 1) for item in value[:128]]
        if isinstance(value, str):
            return value[:16 * 1024]
        if value is None or isinstance(value, (bool, int, float)):
            return value
        return str(value)[:1024]

    def _append_bridge_event(self, event_type: str, bridge_id: str, payload: dict[str, Any]) -> None:
        """Persist low-level Bridge lifecycle facts outside individual runs.

        Run events retain the agent-facing tool observation.  This log retains
        transport ownership, reconnect and Job reconciliation evidence needed
        to explain a bridge pause after the original run is archived.
        """

        event = {
            "schema_version": "device_bridge_event/v1",
            "occurred_at": utc_now(),
            "event_type": event_type,
            "bridge_id": bridge_id,
            "payload": self._event_safe(payload),
        }
        encoded = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        if len(encoded) > MAX_GATEWAY_EVENT_BYTES:
            event["payload"] = {"truncated": True, "payload_sha256": hashlib.sha256(encoded).hexdigest(), "bytes": len(encoded)}
            encoded = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        with self._lock:
            self.event_log.parent.mkdir(parents=True, exist_ok=True)
            with self.event_log.open("ab") as handle:
                handle.write(encoded)
                handle.flush()

    def record_ignored_bridge_event(
        self,
        bridge_id: str,
        *,
        event_type: str,
        job_id: str,
        reason_code: str,
    ) -> None:
        """Audit an authenticated replay that no longer has server state."""

        self._append_bridge_event(
            "stale_bridge_event_ignored",
            bridge_id,
            {
                "event_type": _text(event_type, 80),
                "job_id": _text(job_id, 80),
                "reason_code": _text(reason_code, 160),
            },
        )

    @staticmethod
    def logical_device_id(serial: str) -> str:
        digest = hashlib.sha256(_safe_serial(serial).encode("utf-8")).hexdigest()[:24]
        return f"device_{digest}"

    def _read_registry(self) -> dict[str, Any]:
        return self.registry.read()

    def _write_registry(self, value: dict[str, Any]) -> None:
        self.registry.write(value)

    @staticmethod
    def _public_job(job: dict[str, Any]) -> dict[str, Any]:
        """Return an audit-safe device job view without a signing secret."""

        return {key: value for key, value in job.items() if key not in {"signature", "bridge_secret"}}

    def _record_issued_job(self, job: dict[str, Any]) -> None:
        """Persist an outbound job before offering it to a Bridge.

        This is the backend half of disconnect reconciliation.  A timeout after
        a WebSocket send is a state-unknown job, not permission to rerun the
        same command on a possibly still-running phone.
        """

        with self._lock:
            registry = self._read_registry()
            jobs = _record(registry.get("jobs"))
            public_job = self._public_job(job)
            jobs[str(job["job_id"])] = {
                "schema_version": "device_gateway_job_record/v1",
                "job": public_job,
                "state": "offered",
                "created_at": utc_now(),
                "updated_at": utc_now(),
                "result": None,
            }
            registry["jobs"] = jobs
            index = _record(registry.get("idempotency_index"))
            index[f"{job['bridge_id']}:{job['idempotency_key']}"] = str(job["job_id"])
            registry["idempotency_index"] = index
            self._write_registry(registry)
            self._append_bridge_event(
                "device_job_issued",
                str(job["bridge_id"]),
                {"job_id": job["job_id"], "request_id": job["request_id"], "action": job["action"], "deadline": job["deadline"]},
            )

    def _record_job_state(self, job_id: str, state: str, *, result: dict[str, Any] | None = None) -> bool:
        """Apply a state transition atomically without regressing a terminal job."""

        with self._lock:
            registry = self._read_registry()
            jobs = _record(registry.get("jobs"))
            entry = _record(jobs.get(job_id))
            if not entry or entry.get("state") in TERMINAL_JOB_STATES:
                return False
            if entry.get("state") == "cancel_requested" and state not in TERMINAL_JOB_STATES:
                return False
            entry["state"] = state
            entry["updated_at"] = utc_now()
            if result is not None:
                entry["result"] = result
            jobs[job_id] = entry
            registry["jobs"] = jobs
            job = _record(entry.get("job"))
            lease_id = _text(job.get("device_lease_id"), 80)
            if state in TERMINAL_JOB_STATES and lease_id:
                leases = _record(registry.get("leases"))
                lease = _record(leases.get(lease_id))
                if lease.get("active_job_id") == job_id:
                    lease["active_job_id"] = None
                    leases[lease_id] = lease
                    registry["leases"] = leases
            self._write_registry(registry)
            bridge_id = _text(job.get("bridge_id"), 80)
            if bridge_id:
                self._append_bridge_event(
                    "device_job_state_changed",
                    bridge_id,
                    {"job_id": job_id, "state": state, "result": result or {}},
                )
            return True

    def _prior_idempotent_result(self, job: dict[str, Any]) -> dict[str, Any] | None:
        registry = self._read_registry()
        index = _record(registry.get("idempotency_index"))
        prior_id = _text(index.get(f"{job['bridge_id']}:{job['idempotency_key']}"), 80)
        entry = _record(_record(registry.get("jobs")).get(prior_id))
        result = entry.get("result")
        if entry.get("state") in TERMINAL_JOB_STATES and isinstance(result, dict):
            if job.get("action") == "device.deploy_artifact":
                envelope = dict(result)
                payload = _record(job.get("payload"))
                envelope["result"] = normalize_deployment_result(
                    _record(envelope.get("result")),
                    expected_sha256=_text(payload.get("artifact_sha256"), 80),
                    expected_remote_path=_text(payload.get("remote_path"), 512),
                    expected_serial=_text(payload.get("device_serial"), 160),
                )
                return envelope
            return result
        if entry:
            raise PlatformError(
                "BRIDGE_JOB_STATE_UNKNOWN",
                "相同 Device Job 的前一次执行尚未形成终态；为避免重复设备命令，需等待 Bridge 对账。",
                status_code=409,
                details={"prior_job_id": prior_id, "state": entry.get("state")},
            )
        return None

    def _record_bridge_receipt(self, job_id: str) -> None:
        """Persist remote receipt evidence before lease recovery can run."""

        with self._lock:
            registry = self._read_registry()
            entry = _record(_record(registry.get("jobs")).get(job_id))
            if entry and not entry.get("bridge_received_at"):
                entry["bridge_received_at"] = utc_now()
                entry["delivery_state"] = "received"
                self._write_registry(registry)

    def list_bridges(self, *, project_id: str) -> list[dict[str, Any]]:
        registry = self._read_registry()
        result: list[dict[str, Any]] = []
        for bridge in _record(registry.get("bridges")).values():
            entry = _record(bridge)
            if entry.get("project_id") != project_id:
                continue
            public = {key: value for key, value in entry.items() if key != "bridge_secret"}
            with self._lock:
                session = self._sessions.get(_text(entry.get("bridge_id"), 80))
            public["connection_state"] = "connected" if session and time.time() - session.last_heartbeat <= BRIDGE_HEARTBEAT_GRACE_SECONDS else "offline"
            if session is not None:
                public["capabilities"] = session.capabilities
                public["heartbeat_age_seconds"] = round(max(0.0, time.time() - session.last_heartbeat), 3)
                public["transport"] = session.transport
            result.append(public)
        return sorted(result, key=lambda item: str(item.get("created_at") or ""), reverse=True)

    def create_pairing_code(self, *, project_id: str, ttl_seconds: int = 600) -> dict[str, Any]:
        code = secrets.token_urlsafe(18)
        expires_at = time.time() + max(60, min(ttl_seconds, 3600))
        with self._lock:
            registry = self._read_registry()
            registry["pairing_codes"] = {
                key: item
                for key, item in _record(registry.get("pairing_codes")).items()
                if isinstance(item, dict) and float(item.get("expires_at_epoch", 0)) > time.time()
            }
            registry["pairing_codes"][hashlib.sha256(code.encode("utf-8")).hexdigest()] = {
                "project_id": project_id,
                "expires_at_epoch": expires_at,
                "used": False,
            }
            self._write_registry(registry)
        return {"pairing_code": code, "expires_at": _now_deadline(int(expires_at - time.time())), "project_id": project_id}

    def register_bridge(self, *, pairing_code: str, display_name: str, capabilities: dict[str, Any]) -> dict[str, Any]:
        code_hash = hashlib.sha256(pairing_code.encode("utf-8")).hexdigest()
        with self._lock:
            registry = self._read_registry()
            pairing = _record(_record(registry.get("pairing_codes")).get(code_hash))
            if not pairing or pairing.get("used") or float(pairing.get("expires_at_epoch", 0)) <= time.time():
                raise PlatformError("BRIDGE_PAIRING_INVALID", "Bridge 配对码无效、已使用或已过期。", status_code=422)
            response = self._register_bridge_identity(
                registry=registry,
                project_id=_text(pairing.get("project_id"), 120),
                display_name=display_name,
                capabilities=capabilities,
                registration_mode="pairing_code",
            )
            pairing["used"] = True
            registry.setdefault("pairing_codes", {})[code_hash] = pairing
            self._write_registry(registry)
        return response

    def register_trusted_bridge(self, *, project_id: str, display_name: str, capabilities: dict[str, Any]) -> dict[str, Any]:
        """Register a Bridge without a user pairing ceremony.

        Phase A is explicitly a single-user trusted deployment.  The Bridge
        still receives an opaque machine identity so jobs remain bound,
        signed and idempotent, but users do not copy an authentication code.
        """

        with self._lock:
            registry = self._read_registry()
            response = self._register_bridge_identity(
                registry=registry,
                project_id=project_id,
                display_name=display_name,
                capabilities=capabilities,
                registration_mode="trusted_single_user",
            )
            self._write_registry(registry)
        return response

    def _register_bridge_identity(
        self,
        *,
        registry: dict[str, Any],
        project_id: str,
        display_name: str,
        capabilities: dict[str, Any],
        registration_mode: str,
    ) -> dict[str, Any]:
        bridge_id = _new_id("bridge")
        bridge_secret = secrets.token_urlsafe(36)
        bridge = {
            "schema_version": "device_bridge_identity/v1",
            "bridge_id": bridge_id,
            "project_id": project_id,
            "display_name": _text(display_name, 160) or "Windows HDC Device Bridge",
            "created_at": utc_now(),
            "last_seen_at": None,
            "capabilities": capabilities,
            "registration_mode": registration_mode,
            # The server needs this key to sign jobs.  It is stored in the
            # private gateway registry only, never in artifacts or API traces.
            "bridge_secret": bridge_secret,
        }
        registry.setdefault("bridges", {})[bridge_id] = bridge
        self._append_bridge_event(
            "bridge_registered",
            bridge_id,
            {
                "project_id": bridge["project_id"],
                "display_name": bridge["display_name"],
                "capabilities": capabilities,
                "registration_mode": registration_mode,
            },
        )
        return {
            "bridge_id": bridge_id,
            "bridge_secret": bridge_secret,
            "project_id": bridge["project_id"],
            "protocol_version": HTTP_POLL_PROTOCOL_VERSION,
            "registration_mode": registration_mode,
        }

    def authenticate_bridge(self, bridge_id: str, bridge_secret: str) -> dict[str, Any]:
        if not BRIDGE_ID_PATTERN.fullmatch(bridge_id):
            raise PlatformError("BRIDGE_AUTH_INVALID", "Bridge ID 无效。", status_code=401)
        bridge = _record(_record(self._read_registry().get("bridges")).get(bridge_id))
        expected = _text(bridge.get("bridge_secret"), 200)
        if not expected or not isinstance(bridge_secret, str) or not hmac.compare_digest(expected, bridge_secret):
            raise PlatformError("BRIDGE_AUTH_INVALID", "Bridge 身份验证失败。", status_code=401)
        return bridge

    def connect_bridge(
        self,
        bridge_id: str,
        bridge_secret: str,
        *,
        capabilities: dict[str, Any],
        send: Callable[[dict[str, Any]], None],
        transport: str = "websocket",
        connection_id: str = "",
    ) -> dict[str, Any]:
        bridge = self.authenticate_bridge(bridge_id, bridge_secret)
        return self._connect_authenticated_bridge(
            bridge_id,
            bridge,
            capabilities=capabilities,
            send=send,
            transport=transport,
            connection_id=connection_id,
        )

    def _connect_authenticated_bridge(
        self,
        bridge_id: str,
        bridge: dict[str, Any],
        *,
        capabilities: dict[str, Any],
        send: Callable[[dict[str, Any]], None],
        transport: str,
        connection_id: str,
    ) -> dict[str, Any]:
        """Establish a session after this request has authenticated its identity."""

        session = BridgeSession(
            bridge_id=bridge_id,
            send=send,
            connected_at=time.time(),
            last_heartbeat=time.time(),
            capabilities=capabilities,
            transport=transport,
            connection_id=connection_id,
        )
        with self._lock:
            self._sessions[bridge_id] = session
            registry = self._read_registry()
            stored = _record(_record(registry.get("bridges")).get(bridge_id))
            stored["last_seen_at"] = utc_now()
            stored["capabilities"] = capabilities
            registry["bridges"][bridge_id] = stored
            self._write_registry(registry)
        self._append_bridge_event(
            "bridge_connected",
            bridge_id,
            {"capabilities": capabilities, "transport": transport, "connection_id": connection_id or None},
        )
        protocol_version = HTTP_POLL_PROTOCOL_VERSION if transport == "http_poll" else WEBSOCKET_PROTOCOL_VERSION
        return {
            "bridge_id": bridge_id,
            "project_id": bridge.get("project_id"),
            "protocol_version": protocol_version,
            "transport": transport,
        }

    def _enqueue_poll_message(self, bridge_id: str, connection_id: str, message: dict[str, Any]) -> None:
        with self._lock:
            session = self._sessions.get(bridge_id)
            if (
                session is None
                or session.transport != "http_poll"
                or session.connection_id != connection_id
            ):
                raise PlatformError(
                    "BRIDGE_POLL_SESSION_REPLACED",
                    "Device Bridge HTTP 轮询会话已被新的连接替换；任务将通过重连对账重新派发。",
                    status_code=503,
                    details={"bridge_id": bridge_id},
                )
            queue = self._poll_messages.setdefault(bridge_id, deque())
            if len(queue) >= MAX_POLL_QUEUE_MESSAGES:
                raise PlatformError(
                    "BRIDGE_POLL_QUEUE_FULL",
                    "Device Bridge HTTP 轮询队列已满，未继续接受设备任务。",
                    status_code=503,
                    details={"bridge_id": bridge_id, "queue_limit": MAX_POLL_QUEUE_MESSAGES},
                )
            queue.append(dict(message))

    def poll_bridge_messages(
        self,
        bridge_id: str,
        bridge_secret: str,
        *,
        connection_id: str,
        capabilities: dict[str, Any],
        reconcile_jobs: list[dict[str, Any]] | None,
        max_messages: int,
    ) -> dict[str, Any]:
        """Establish or refresh an HTTP Bridge session and drain queued work.

        Each request is an ordinary authenticated HTTP exchange.  Device jobs
        keep the same signed envelopes and reconciliation rules as the legacy
        WebSocket transport, but no Upgrade, compression bit or close frame is
        involved.
        """

        if not CONNECTION_ID_PATTERN.fullmatch(connection_id):
            raise PlatformError("BRIDGE_CONNECTION_INVALID", "Bridge connection_id 无效。", status_code=422)
        bridge = self.authenticate_bridge(bridge_id, bridge_secret)
        with self._lock:
            current = self._sessions.get(bridge_id)
            is_new_connection = not (
                current is not None
                and current.transport == "http_poll"
                and current.connection_id == connection_id
            )
        if is_new_connection:
            with self._lock:
                self._poll_messages[bridge_id] = deque()

            def send(message: dict[str, Any]) -> None:
                self._enqueue_poll_message(bridge_id, connection_id, message)

            connection = self._connect_authenticated_bridge(
                bridge_id,
                bridge,
                capabilities=capabilities,
                send=send,
                transport="http_poll",
                connection_id=connection_id,
            )
        else:
            self.heartbeat(bridge_id, capabilities=capabilities)
            connection = {
                "bridge_id": bridge_id,
                "protocol_version": HTTP_POLL_PROTOCOL_VERSION,
                "transport": "http_poll",
            }

        if reconcile_jobs is not None:
            for job in self.reconcile_bridge_jobs(bridge_id, reconcile_jobs):
                self._enqueue_poll_message(bridge_id, connection_id, {"type": "job.offer", "job": job})
            known_ids = {
                _text(item.get("job_id"), 80)
                for item in reconcile_jobs
                if isinstance(item, dict)
            }
            registry = self._read_registry()
            for entry_value in _record(registry.get("jobs")).values():
                entry = _record(entry_value)
                job = _record(entry.get("job"))
                job_id = _text(job.get("job_id"), 80)
                if (
                    job.get("bridge_id") == bridge_id
                    and job_id in known_ids
                    and entry.get("state") == "cancel_requested"
                ):
                    self._enqueue_poll_message(
                        bridge_id,
                        connection_id,
                        {"type": "job.cancel", "job_id": job_id},
                    )

        bounded_max = max(1, min(int(max_messages), 64))
        messages: list[dict[str, Any]] = []
        with self._lock:
            queue = self._poll_messages.setdefault(bridge_id, deque())
            while queue and len(messages) < bounded_max:
                messages.append(queue.popleft())
        return {
            **connection,
            "new_connection": is_new_connection,
            "messages": messages,
            "poll_after_ms": 750,
        }

    def heartbeat(self, bridge_id: str, *, capabilities: dict[str, Any] | None = None) -> None:
        now = time.time()
        should_audit = False
        with self._lock:
            session = self._sessions.get(bridge_id)
            if session:
                session.last_heartbeat = now
                if capabilities is not None:
                    session.capabilities = capabilities
                last = self._last_audited_heartbeats.get(bridge_id, 0.0)
                if now - last >= 60:
                    self._last_audited_heartbeats[bridge_id] = now
                    should_audit = True
        if should_audit:
            with self._lock:
                registry = self._read_registry()
                stored = _record(_record(registry.get("bridges")).get(bridge_id))
                if stored:
                    stored["last_seen_at"] = utc_now()
                    if capabilities is not None:
                        stored["capabilities"] = capabilities
                    registry["bridges"][bridge_id] = stored
                    self._write_registry(registry)
            self._append_bridge_event("bridge_heartbeat", bridge_id, {"capabilities_updated": capabilities is not None})

    def disconnect_bridge(self, bridge_id: str, *, transport: str = "", connection_id: str = "") -> None:
        disconnected = False
        with self._lock:
            session = self._sessions.get(bridge_id)
            if session is None:
                return
            if transport and session.transport != transport:
                return
            if connection_id and session.connection_id != connection_id:
                return
            self._sessions.pop(bridge_id, None)
            if session.transport == "http_poll":
                self._poll_messages.pop(bridge_id, None)
            disconnected = True
        if disconnected:
            self._append_bridge_event("bridge_disconnected", bridge_id, {"transport": session.transport})

    def _recover_unsent_offline_jobs(
        self,
        bridge_id: str,
        *,
        known_job_ids: set[str] | None = None,
        registry: dict[str, Any] | None = None,
    ) -> None:
        """Release legacy pre-send failures only when their audit proves that path.

        Older dispatchers wrote ``bridge_offline`` solely when session lookup
        failed before ``send``. Require the matching audit sequence and reject
        any receipt, progress, result or unknown-delivery evidence. A timeout
        after a send is never recovered by this migration.
        """

        with self._lock:
            registry = self._read_registry() if registry is None else registry
            jobs = _record(registry.get("jobs"))
            candidates = {
                job_id: entry for job_id, entry in jobs.items()
                if isinstance(entry, dict) and entry.get("state") == "bridge_offline"
                and _record(entry.get("job")).get("bridge_id") == bridge_id
                and not entry.get("result") and not entry.get("bridge_received_at")
                and not entry.get("delivery_state")
                and job_id not in (known_job_ids or set())
            }
            if not candidates:
                return
            issued: set[str] = set()
            offline: set[str] = set()
            received: set[str] = set()
            try:
                with self.event_log.open(encoding="utf-8") as handle:
                    for line in handle:
                        event = json.loads(line)
                        payload = _record(event.get("payload"))
                        if event.get("bridge_id") != bridge_id:
                            continue
                        if event.get("event_type") == "bridge_reconciled":
                            reoffered = payload.get("reoffered_job_ids")
                            if isinstance(reoffered, list):
                                received.update(item for item in reoffered if isinstance(item, str) and item in candidates)
                        job_id = _text(payload.get("job_id"), 80)
                        if job_id not in candidates:
                            continue
                        event_type = event.get("event_type")
                        if event_type == "device_job_issued":
                            issued.add(job_id)
                        elif event_type == "device_job_state_changed" and payload.get("state") == "bridge_offline" and not payload.get("result"):
                            offline.add(job_id)
                        else:
                            received.add(job_id)
            except (OSError, ValueError, AttributeError):
                # Missing or incomplete audit is not proof that nothing ran.
                return
            recovered = (issued & offline) - received
            if not recovered:
                return
            index = _record(registry.get("idempotency_index"))
            leases = _record(registry.get("leases"))
            for job_id in recovered:
                entry = candidates[job_id]
                job = _record(entry.get("job"))
                entry.update({
                    "state": "failed", "updated_at": utc_now(), "delivery_state": "not_sent",
                    "result": {"job_id": job_id, "result": {"status": "failed", "error": {
                        "code": "BRIDGE_OFFLINE_BEFORE_SEND", "message": "旧任务在发送前发现 Bridge 离线，已释放设备占用。",
                    }}},
                })
                key = f"{bridge_id}:{job.get('idempotency_key', '')}"
                if index.get(key) == job_id:
                    index.pop(key)
                for lease in leases.values():
                    if isinstance(lease, dict) and lease.get("active_job_id") == job_id:
                        lease["active_job_id"] = None
            self._write_registry(registry)
            for job_id in recovered:
                self._append_bridge_event("device_job_unsent_recovered", bridge_id, {"job_id": job_id})

    @staticmethod
    def _target_busy_job(registry: dict[str, Any], *, project_id: str, bridge_id: str, logical_device_id: str) -> dict[str, Any]:
        for entry_value in _record(registry.get("jobs")).values():
            entry = _record(entry_value)
            job = _record(entry.get("job"))
            if (
                job.get("project_id") == project_id and job.get("bridge_id") == bridge_id
                and job.get("logical_device_id") == logical_device_id
                and job.get("action") in WRITE_DEVICE_ACTIONS
                and entry.get("state") not in TERMINAL_JOB_STATES
            ):
                return entry
        return {}

    @staticmethod
    def _raise_target_busy(entry: dict[str, Any]) -> None:
        job = _record(entry.get("job"))
        raise PlatformError(
            "DEVICE_LEASE_BUSY", "该设备已有未完成的写入型任务，正在等待任务结束或 Bridge 对账。",
            status_code=409,
            details={"lease_id": job.get("device_lease_id"), "active_job_id": job.get("job_id"), "state": entry.get("state")},
        )

    def create_lease(
        self,
        *,
        project_id: str,
        bridge_id: str,
        logical_device_id: str,
        ttl_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> dict[str, Any]:
        if not BRIDGE_ID_PATTERN.fullmatch(bridge_id) or not LOGICAL_DEVICE_PATTERN.fullmatch(logical_device_id):
            raise PlatformError("DEVICE_LEASE_INVALID", "Bridge 或逻辑设备标识无效。", status_code=422)
        normalized_ttl = max(60, min(int(ttl_seconds), MAX_LEASE_SECONDS))
        with self._lock:
            registry = self._read_registry()
            bridge = _record(_record(registry.get("bridges")).get(bridge_id))
            if not bridge or bridge.get("project_id") != project_id:
                raise PlatformError("DEVICE_LEASE_UNAUTHORIZED", "此 Bridge 不属于当前项目。", status_code=403)
            self._recover_unsent_offline_jobs(bridge_id)
            registry = self._read_registry()
            matching = [
                lease for lease in _record(registry.get("leases")).values()
                if isinstance(lease, dict) and lease.get("project_id") == project_id
                and lease.get("bridge_id") == bridge_id and lease.get("logical_device_id") == logical_device_id
                and not lease.get("superseded_by")
            ]
            now = time.time()
            fresh = [lease for lease in matching if float(lease.get("expires_at_epoch", 0)) > now]
            if fresh:
                lease = max(fresh, key=lambda item: (bool(item.get("active_job_id")), float(item.get("expires_at_epoch", 0))))
                lease["expires_at_epoch"] = max(float(lease["expires_at_epoch"]), now + normalized_ttl)
                lease["ttl_seconds"] = max(int(lease.get("ttl_seconds", 0)), normalized_ttl)
                self._write_registry(registry)
                return self.public_lease(lease)
            busy = self._target_busy_job(registry, project_id=project_id, bridge_id=bridge_id, logical_device_id=logical_device_id)
            if busy:
                self._raise_target_busy(busy)
            if matching:
                previous = max(matching, key=lambda item: float(item.get("expires_at_epoch", 0)))
                return self.ensure_active_lease(str(previous["lease_id"]), project_id=project_id,
                    bridge_id=bridge_id, logical_device_id=logical_device_id, ttl_seconds=normalized_ttl)["device_lease"]
            lease_id = _new_id("lease")
            lease = {
                "schema_version": "device_lease/v1", "lease_id": lease_id, "project_id": project_id,
                "bridge_id": bridge_id, "logical_device_id": logical_device_id, "created_at": utc_now(),
                "expires_at_epoch": now + normalized_ttl, "ttl_seconds": normalized_ttl,
                "mode": "single_writer", "active_job_id": None,
            }
            registry.setdefault("leases", {})[lease_id] = lease
            self._write_registry(registry)
        return self.public_lease(lease)

    @staticmethod
    def public_lease(lease: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in lease.items() if key != "active_job_id" or value is None}

    def lease_is_reusable(
        self,
        lease_id: str,
        *,
        project_id: str,
        bridge_id: str,
        logical_device_id: str,
        minimum_remaining_seconds: int = LEASE_REUSE_MIN_REMAINING_SECONDS,
    ) -> bool:
        """Return whether an exact lease is fresh enough for checkpoint reuse."""

        if not LEASE_ID_PATTERN.fullmatch(lease_id):
            return False
        lease = _record(_record(self._read_registry().get("leases")).get(lease_id))
        if (
            lease.get("project_id") != project_id
            or lease.get("bridge_id") != bridge_id
            or lease.get("logical_device_id") != logical_device_id
        ):
            return False
        return float(lease.get("expires_at_epoch", 0)) > time.time() + max(0, minimum_remaining_seconds)

    def ensure_active_lease(
        self,
        lease_id: str,
        *,
        project_id: str,
        bridge_id: str,
        logical_device_id: str,
        ttl_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> dict[str, Any]:
        """Resolve a valid lease or atomically replace an expired lease.

        Device leases are a platform concurrency primitive, not a reasoning
        fact.  A long build must not strand a valid Agent checkpoint merely
        because the earlier lease elapsed.  The expired ID remains auditable
        and points to one replacement so repeated checkpoint inputs converge
        on the same effective lease.
        """

        if not LEASE_ID_PATTERN.fullmatch(lease_id):
            raise PlatformError("DEVICE_LEASE_INVALID", "设备租约标识无效。", status_code=422)
        normalized_ttl = max(60, min(int(ttl_seconds), MAX_LEASE_SECONDS))
        now = time.time()
        replacement_event: dict[str, Any] | None = None
        with self._lock:
            registry = self._read_registry()
            leases = _record(registry.get("leases"))
            requested = _record(leases.get(lease_id))
            if (
                not requested
                or requested.get("project_id") != project_id
                or requested.get("bridge_id") != bridge_id
                or requested.get("logical_device_id") != logical_device_id
            ):
                raise PlatformError("DEVICE_LEASE_UNAUTHORIZED", "设备租约不匹配。", status_code=403)

            self._recover_unsent_offline_jobs(bridge_id)
            registry = self._read_registry()
            leases = _record(registry.get("leases"))
            requested = _record(leases.get(lease_id))

            current = requested
            visited = {lease_id}
            for _ in range(16):
                successor_id = _text(current.get("superseded_by"), 80)
                if not successor_id:
                    break
                if successor_id in visited:
                    raise PlatformError("DEVICE_LEASE_STATE_INVALID", "设备租约替换链存在循环。", status_code=500)
                successor = _record(leases.get(successor_id))
                if (
                    not successor
                    or successor.get("project_id") != project_id
                    or successor.get("bridge_id") != bridge_id
                    or successor.get("logical_device_id") != logical_device_id
                ):
                    raise PlatformError("DEVICE_LEASE_STATE_INVALID", "设备租约替换链不完整。", status_code=500)
                visited.add(successor_id)
                current = successor
            else:
                raise PlatformError("DEVICE_LEASE_STATE_INVALID", "设备租约替换链超过安全上限。", status_code=500)

            current_id = _text(current.get("lease_id"), 80)
            if not LEASE_ID_PATTERN.fullmatch(current_id):
                raise PlatformError("DEVICE_LEASE_STATE_INVALID", "设备租约替换链缺少有效标识。", status_code=500)
            if float(current.get("expires_at_epoch", 0)) > now:
                return {
                    "requested_lease_id": lease_id,
                    "effective_lease_id": current_id,
                    "replaced": current_id != lease_id,
                    "replacement_reason": "previously_replaced" if current_id != lease_id else None,
                    "device_lease": self.public_lease(current),
                }

            busy = self._target_busy_job(registry, project_id=project_id, bridge_id=bridge_id, logical_device_id=logical_device_id)
            if busy:
                self._raise_target_busy(busy)

            replacement_id = _new_id("lease")
            replacement = {
                "schema_version": "device_lease/v1",
                "lease_id": replacement_id,
                "project_id": project_id,
                "bridge_id": bridge_id,
                "logical_device_id": logical_device_id,
                "created_at": utc_now(),
                "expires_at_epoch": now + normalized_ttl,
                "ttl_seconds": normalized_ttl,
                "mode": "single_writer",
                "active_job_id": None,
                "replaces_lease_id": current_id,
            }
            current["superseded_by"] = replacement_id
            current["superseded_at"] = utc_now()
            leases[current_id] = current
            # Compress the originally supplied stale ID to the newest lease so
            # old checkpoint inputs do not create one replacement per action.
            if lease_id != current_id:
                requested["superseded_by"] = replacement_id
                requested["superseded_at"] = utc_now()
                leases[lease_id] = requested
            leases[replacement_id] = replacement
            registry["leases"] = leases
            self._write_registry(registry)
            replacement_event = {
                "requested_lease_id": lease_id,
                "expired_lease_id": current_id,
                "effective_lease_id": replacement_id,
                "logical_device_id": logical_device_id,
            }

        assert replacement_event is not None
        self._append_bridge_event("device_lease_replaced", bridge_id, replacement_event)
        return {
            "requested_lease_id": lease_id,
            "effective_lease_id": replacement_id,
            "replaced": True,
            "replacement_reason": "expired",
            "device_lease": self.public_lease(replacement),
        }

    def _lease(self, lease_id: str, *, project_id: str, bridge_id: str, logical_device_id: str) -> dict[str, Any]:
        if not LEASE_ID_PATTERN.fullmatch(lease_id):
            raise PlatformError("DEVICE_LEASE_INVALID", "设备租约标识无效。", status_code=422)
        lease = _record(_record(self._read_registry().get("leases")).get(lease_id))
        if not lease or lease.get("project_id") != project_id or lease.get("bridge_id") != bridge_id or lease.get("logical_device_id") != logical_device_id:
            raise PlatformError("DEVICE_LEASE_UNAUTHORIZED", "设备租约不匹配。", status_code=403)
        if float(lease.get("expires_at_epoch", 0)) <= time.time():
            raise PlatformError("DEVICE_LEASE_EXPIRED", "设备租约已过期。", status_code=409)
        return lease

    def _sign_job(self, bridge_secret: str, job: dict[str, Any]) -> str:
        unsigned = dict(job)
        unsigned.pop("signature", None)
        return hmac.new(bridge_secret.encode("utf-8"), canonical_json_bytes(unsigned), hashlib.sha256).hexdigest()

    def verify_job_signature(self, bridge_id: str, job: dict[str, Any]) -> bool:
        bridge = _record(_record(self._read_registry().get("bridges")).get(bridge_id))
        secret = _text(bridge.get("bridge_secret"), 200)
        signature = _text(job.get("signature"), 128)
        return bool(secret and signature and hmac.compare_digest(self._sign_job(secret, job), signature))

    def _active_session(self, bridge_id: str, *, action: str | None = None) -> BridgeSession:
        with self._lock:
            session = self._sessions.get(bridge_id)
            if not session or time.time() - session.last_heartbeat > BRIDGE_HEARTBEAT_GRACE_SECONDS:
                raise PlatformError("BRIDGE_OFFLINE", "目标 Windows HDC Device Bridge 当前不在线；任务已保留，等待重连后可继续。", status_code=503, details={"bridge_id": bridge_id})
            capabilities = session.capabilities
            protocol = _text(capabilities.get("protocol_version"), 80)
            if protocol and protocol not in SUPPORTED_BRIDGE_PROTOCOLS:
                raise PlatformError("BRIDGE_UPGRADE_REQUIRED", "目标 Device Bridge 协议版本不兼容，不能降级为任意命令通道。", status_code=409, details={"bridge_id": bridge_id, "protocol_version": protocol})
            supported_actions = capabilities.get("actions")
            if action and isinstance(supported_actions, list) and action not in {str(item) for item in supported_actions}:
                raise PlatformError("BRIDGE_CAPABILITY_UNSUPPORTED", "目标 Device Bridge 未声明支持该类型化动作。", status_code=422, details={"bridge_id": bridge_id, "action": action})
            return session

    def _bridge_job(
        self,
        *,
        project_id: str,
        agent_run_id: str,
        bridge_id: str,
        lease_id: str,
        logical_device_id: str,
        action: str,
        payload: dict[str, Any],
        idempotency_key: str,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        if action not in DEVICE_ACTIONS:
            raise PlatformError("DEVICE_JOB_ACTION_INVALID", "Device Job action 未注册。", status_code=422)
        bridge = _record(_record(self._read_registry().get("bridges")).get(bridge_id))
        if not bridge or bridge.get("project_id") != project_id:
            raise PlatformError("DEVICE_BRIDGE_UNAUTHORIZED", "目标 Device Bridge 不属于当前项目。", status_code=403)
        # Discovery and the project-scoped Bridge file library do not operate
        # on a phone. Other device operations remain bound to a device lease.
        if action in {"device.discover", "trace.library.list", "trace.library.delete"}:
            normalized_lease_id: str | None = None
            normalized_logical_device_id = "device_discovery" if action == "device.discover" else "trace_library"
        else:
            self._lease(lease_id, project_id=project_id, bridge_id=bridge_id, logical_device_id=logical_device_id)
            normalized_lease_id = lease_id
            normalized_logical_device_id = logical_device_id
        job = {
            "schema_version": "device_job/v1",
            "job_id": _new_id("devjob"),
            "request_id": _new_id("req"),
            "idempotency_key": _text(idempotency_key, 256),
            "project_id": project_id,
            "agent_run_id": agent_run_id,
            "bridge_id": bridge_id,
            "device_lease_id": normalized_lease_id,
            "logical_device_id": normalized_logical_device_id,
            "action": action,
            "deadline": _now_deadline(max(1, min(timeout_seconds, MAX_JOB_SECONDS))),
            "payload": payload,
        }
        job["signature"] = self._sign_job(_text(bridge.get("bridge_secret"), 200), job)
        return job

    def dispatch_bridge_job(self, job: dict[str, Any], *, wait_seconds: int | None = None) -> dict[str, Any]:
        bridge_id = _text(job.get("bridge_id"), 80)
        job_id = str(job["job_id"])
        pending = PendingJob(event=threading.Event())
        with self._lock:
            self._recover_unsent_offline_jobs(bridge_id)
            prior = self._prior_idempotent_result(job)
            if prior is not None:
                return {**prior, "idempotency_reused": True}
            try:
                session = self._active_session(bridge_id, action=_text(job.get("action"), 120))
            except PlatformError as exc:
                # A known offline session cannot have received this job. Leave
                # both the lease and idempotency key available for reconnect.
                if exc.code != "BRIDGE_OFFLINE":
                    self._record_issued_job(job)
                    self._record_job_state(job_id, "failed", result={"job_id": job_id, "result": {
                        "status": "failed", "error": {"code": exc.code, "message": str(exc)},
                    }})
                raise
            if str(job.get("action")) in WRITE_DEVICE_ACTIONS:
                self._lease(_text(job.get("device_lease_id"), 80), project_id=str(job["project_id"]),
                    bridge_id=bridge_id, logical_device_id=str(job["logical_device_id"]))
                busy = self._target_busy_job(self._read_registry(), project_id=str(job["project_id"]),
                    bridge_id=bridge_id, logical_device_id=str(job["logical_device_id"]))
                if busy:
                    self._raise_target_busy(busy)
            # Target-level exclusion and reservation share one transaction,
            # including jobs prepared concurrently or using legacy leases.
            self._record_issued_job(job)
            registry = self._read_registry()
            registry["jobs"][job_id]["delivery_state"] = "send_started"
            if str(job.get("action")) in WRITE_DEVICE_ACTIONS:
                registry["leases"][str(job["device_lease_id"])]["active_job_id"] = job_id
            self._write_registry(registry)
            self._pending_jobs[job_id] = pending
        try:
            try:
                session.send({"type": "job.offer", "job": job})
            except Exception:
                # Once send starts, a transport error alone cannot prove that
                # the remote side did not receive the command.
                self._record_job_state(job_id, "state_unknown")
                raise
            self._record_job_state(job_id, "accepted_waiting_result")
            pending.event.wait(timeout=max(1, min(wait_seconds or MAX_JOB_SECONDS, MAX_JOB_SECONDS + 15)))
            with self._lock:
                # A result may arrive precisely as wait expires. Decide from
                # the protected result, not only the Event.wait return value.
                if pending.result is not None:
                    return pending.result
                self._record_job_state(job_id, "state_unknown")
                raise PlatformError("BRIDGE_JOB_TIMEOUT", "Device Bridge 未在 job deadline 前返回结果；任务状态未知，等待重连对账。", status_code=504, details={"job_id": job_id})
        finally:
            with self._lock:
                self._pending_jobs.pop(job_id, None)

    def report_bridge_result(self, bridge_id: str, result: dict[str, Any]) -> None:
        with self._lock:
            job_id = _text(result.get("job_id"), 80)
            if not JOB_ID_PATTERN.fullmatch(job_id):
                raise PlatformError("BRIDGE_JOB_RESULT_INVALID", "Bridge Job Result 缺少有效 job_id。", status_code=422)
            registry = self._read_registry()
            entry = _record(_record(registry.get("jobs")).get(job_id))
            job = _record(entry.get("job"))
            if not entry:
                raise PlatformError(
                    "BRIDGE_JOB_RESULT_STALE",
                    "后端已不再保存该 Device Job；Bridge 应保留本地审计，但停止重复回放此结果。",
                    status_code=410,
                    details={"job_id": job_id},
                )
            if job.get("bridge_id") != bridge_id:
                raise PlatformError("BRIDGE_JOB_RESULT_UNAUTHORIZED", "Bridge 不能提交不属于自己的 Device Job 结果。", status_code=403)
            self._record_bridge_receipt(job_id)
            output = _record(result.get("result"))
            if job.get("action") == "device.deploy_artifact":
                payload = _record(job.get("payload"))
                output = normalize_deployment_result(
                    output,
                    expected_sha256=_text(payload.get("artifact_sha256"), 80),
                    expected_remote_path=_text(payload.get("remote_path"), 512),
                    expected_serial=_text(payload.get("device_serial"), 160),
                )
                result = {**result, "result": output}
            state = _text(output.get("status"), 40) or "failed"
            if state not in TERMINAL_JOB_STATES:
                state = "failed" if state not in {"running", "accepted"} else "state_unknown"
            if entry.get("state") == "cancelled" and state != "cancelled":
                # The user-facing cancellation has already been acknowledged. A
                # late HDC result is still useful audit evidence, but it must not
                # resurrect a cancelled job or release/reassign its lease twice.
                self._append_bridge_event(
                    "device_job_late_result_after_cancel",
                    bridge_id,
                    {"job_id": job_id, "reported_state": state, "result": result},
                )
                return
            with self._lock:
                applied = self._record_job_state(job_id, state, result=result)
                if not applied:
                    persisted = _record(_record(self._read_registry().get("jobs")).get(job_id)).get("result")
                    if not isinstance(persisted, dict):
                        return
                    result = persisted
                pending = self._pending_jobs.get(job_id)
                if pending:
                    pending.result = result
                    pending.event.set()

    def _cancel_pending_job(self, job_id: str, *, detail: str) -> None:
        """Unblock a synchronous dispatch after Run cancellation.

        The Bridge may still be terminating an HDC child.  Returning a typed
        cancellation observation lets the Runtime stop its thread immediately
        while the gateway retains ``cancel_requested`` for reconciliation.
        """

        with self._lock:
            pending = self._pending_jobs.get(job_id)
            if pending is None:
                return
            pending.result = {
                "job_id": job_id,
                "result": {"status": "cancelled", "detail": detail},
            }
            pending.event.set()

    def report_bridge_progress(self, bridge_id: str, progress: dict[str, Any]) -> None:
        """Record a non-terminal Bridge update without unblocking the job."""

        job_id = _text(progress.get("job_id"), 80)
        if not JOB_ID_PATTERN.fullmatch(job_id):
            raise PlatformError("BRIDGE_JOB_PROGRESS_INVALID", "Bridge Job progress 缺少有效 job_id。", status_code=422)
        with self._lock:
            entry = _record(_record(self._read_registry().get("jobs")).get(job_id))
            job = _record(entry.get("job"))
            if not entry:
                raise PlatformError("BRIDGE_JOB_PROGRESS_STALE", "后端已不再保存该 Device Job 进度。", status_code=410, details={"job_id": job_id})
            if job.get("bridge_id") != bridge_id:
                raise PlatformError("BRIDGE_JOB_PROGRESS_UNAUTHORIZED", "Bridge 不能提交不属于自己的 Device Job 进度。", status_code=403)
            self._record_bridge_receipt(job_id)
        detail = _record(progress.get("progress"))
        self._append_bridge_event("device_job_progress", bridge_id, {"job_id": job_id, "request_id": progress.get("request_id"), "progress": detail})
        with self._lock:
            sink = self._run_event_sink
        if sink is not None:
            try:
                sink({
                    "agent_run_id": _text(job.get("agent_run_id"), 120),
                    "job_id": job_id,
                    "request_id": _text(progress.get("request_id"), 120),
                    "bridge_id": bridge_id,
                    "action_id": _text(job.get("action"), 160),
                    "progress": detail,
                })
            except Exception:
                # Progress mirroring must not disrupt a real HDC action when
                # the API process is being restarted or its SSE store is busy.
                self._append_bridge_event("device_job_progress_observer_failed", bridge_id, {"job_id": job_id})

    def mark_bridge_job_accepted(self, bridge_id: str, job_id: str) -> None:
        """Record receipt without treating it as a completed device action."""

        with self._lock:
            if not JOB_ID_PATTERN.fullmatch(job_id):
                raise PlatformError("BRIDGE_JOB_RESULT_INVALID", "Bridge Job accepted 消息缺少有效 job_id。", status_code=422)
            entry = _record(_record(self._read_registry().get("jobs")).get(job_id))
            job = _record(entry.get("job"))
            if not entry:
                raise PlatformError("BRIDGE_JOB_ACCEPTED_STALE", "后端已不再保存该 Device Job 接收状态。", status_code=410, details={"job_id": job_id})
            if job.get("bridge_id") != bridge_id:
                raise PlatformError("BRIDGE_JOB_ACCEPTED_UNAUTHORIZED", "Bridge 不能确认不属于自己的 Device Job。", status_code=403)
            self._record_bridge_receipt(job_id)
            if entry.get("state") not in TERMINAL_JOB_STATES:
                self._record_job_state(job_id, "accepted_waiting_result")

    def reconcile_bridge_jobs(self, bridge_id: str, known_jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return server-side state for a reconnecting Bridge.

        A known terminal result is accepted separately through ``job.result``.
        This response only re-offers jobs that the server knows were sent but
        the Bridge did not list, and never re-offers a state-unknown write job.
        """

        known_ids = {_text(item.get("job_id"), 80) for item in known_jobs if isinstance(item, dict)}
        with self._lock:
            registry = self._read_registry()
            jobs = _record(registry.get("jobs"))
            received_at = utc_now()
            receipts_changed = False
            for job_id in known_ids:
                entry = _record(jobs.get(job_id))
                if _record(entry.get("job")).get("bridge_id") == bridge_id and not entry.get("bridge_received_at"):
                    entry["bridge_received_at"] = received_at
                    entry["delivery_state"] = "received"
                    receipts_changed = True
            # Reconnects can advertise 256 historical jobs. A full registry
            # read/write per receipt made one poll scale with history squared
            # and held the gateway lock beyond the transport timeout. Commit
            # their receipt evidence together, before unsent-job recovery.
            if receipts_changed:
                self._write_registry(registry)
            self._recover_unsent_offline_jobs(bridge_id, known_job_ids=known_ids, registry=registry)
        offers: list[dict[str, Any]] = []
        bridge = _record(_record(registry.get("bridges")).get(bridge_id))
        secret = _text(bridge.get("bridge_secret"), 200)
        for entry_value in _record(registry.get("jobs")).values():
            entry = _record(entry_value)
            job = _record(entry.get("job"))
            job_id = _text(job.get("job_id"), 80)
            if job.get("bridge_id") != bridge_id or not job_id:
                continue
            if job_id in known_ids or entry.get("state") in TERMINAL_JOB_STATES | {"state_unknown", "bridge_offline", "cancel_requested"}:
                continue
            signed = dict(job)
            signed["signature"] = self._sign_job(secret, signed)
            offers.append(signed)
        self._append_bridge_event("bridge_reconciled", bridge_id, {"known_job_count": len(known_ids), "reoffered_job_ids": [item["job_id"] for item in offers]})
        return offers

    def cancel_bridge_job(self, *, bridge_id: str, job_id: str) -> dict[str, Any]:
        if not JOB_ID_PATTERN.fullmatch(job_id):
            raise PlatformError("DEVICE_JOB_INVALID", "Device Job ID 无效。", status_code=422)
        registry = self._read_registry()
        entry = _record(_record(registry.get("jobs")).get(job_id))
        job = _record(entry.get("job"))
        if not entry or job.get("bridge_id") != bridge_id:
            raise PlatformError("DEVICE_JOB_NOT_FOUND", "未找到属于当前 Bridge 的 Device Job。", status_code=404)
        session = self._active_session(bridge_id)
        session.send({"type": "job.cancel", "job_id": job_id})
        self._record_job_state(job_id, "cancel_requested")
        self._cancel_pending_job(job_id, detail="用户已请求取消该 Device Job，Bridge 正在终止对应 HDC 子进程。")
        return {"status": "cancel_requested", "job_id": job_id}

    def cancel_agent_run(self, agent_run_id: str) -> dict[str, Any]:
        """Request cancellation for every non-terminal remote job of a Run.

        When a Bridge is offline, the server records ``cancel_requested`` and
        deliberately does not re-offer the job after reconciliation.  It does
        not pretend that a possibly in-flight phone command has already ended.
        """

        normalized_run_id = _text(agent_run_id, 120)
        if not normalized_run_id:
            return {"cancelled_job_ids": [], "deferred_job_ids": [], "local_executor": {"status": "acknowledged", "active_process_count": 0}}
        local_cancellation = self.local_executor.cancel_run(normalized_run_id)
        registry = self._read_registry()
        requested: list[str] = []
        deferred: list[str] = []
        for entry_value in _record(registry.get("jobs")).values():
            entry = _record(entry_value)
            job = _record(entry.get("job"))
            job_id = _text(job.get("job_id"), 80)
            if job.get("agent_run_id") != normalized_run_id or not job_id or entry.get("state") in TERMINAL_JOB_STATES:
                continue
            bridge_id = _text(job.get("bridge_id"), 80)
            self._record_job_state(job_id, "cancel_requested")
            try:
                session = self._active_session(bridge_id)
                session.send({"type": "job.cancel", "job_id": job_id})
                self._cancel_pending_job(job_id, detail="所属 Agent Run 已取消，Bridge 正在终止对应 HDC 子进程。")
                requested.append(job_id)
            except PlatformError:
                deferred.append(job_id)
                self._append_bridge_event("device_job_cancel_deferred", bridge_id, {"job_id": job_id, "agent_run_id": normalized_run_id})
        return {"cancelled_job_ids": requested, "deferred_job_ids": deferred, "local_executor": local_cancellation}

    def verify_job_owner(self, *, job_id: str, bridge_id: str) -> dict[str, Any]:
        if not JOB_ID_PATTERN.fullmatch(job_id):
            raise PlatformError("DEVICE_JOB_INVALID", "Device Job ID 无效。", status_code=422)
        entry = _record(_record(self._read_registry().get("jobs")).get(job_id))
        job = _record(entry.get("job"))
        if not entry or job.get("bridge_id") != bridge_id:
            raise PlatformError("BRIDGE_JOB_UNAUTHORIZED", "Bridge 无权访问该 Device Job。", status_code=403)
        return entry

    def issue_download_grant(self, *, job_id: str, bridge_id: str, path: Path, expected_sha256: str, expires_seconds: int = 300) -> str:
        if not JOB_ID_PATTERN.fullmatch(job_id):
            raise PlatformError("DEVICE_JOB_INVALID", "Device Job ID 无效。", status_code=422)
        if not path.is_file() or sha256_file(path) != expected_sha256:
            raise PlatformError("DEVICE_DEPLOY_ARTIFACT_INVALID", "待下载构建产物不存在或哈希不匹配。", status_code=409)
        token = secrets.token_urlsafe(32)
        with self._lock:
            self._download_grants[token] = {
                "job_id": job_id,
                "bridge_id": bridge_id,
                "path": path,
                "sha256": expected_sha256,
                "expires_at": time.time() + max(60, min(expires_seconds, 3600)),
            }
        return token

    def resolve_download_grant(self, *, token: str, job_id: str, bridge_id: str) -> dict[str, Any]:
        with self._lock:
            grant = self._download_grants.get(token)
            if not grant or grant["expires_at"] <= time.time() or grant["job_id"] != job_id or grant["bridge_id"] != bridge_id:
                raise PlatformError("BRIDGE_DOWNLOAD_UNAUTHORIZED", "构建产物下载授权无效或已过期。", status_code=403)
            path = grant["path"]
            if not path.is_file() or sha256_file(path) != grant["sha256"]:
                raise PlatformError("BRIDGE_DOWNLOAD_ARTIFACT_CHANGED", "构建产物在下载前发生变化。", status_code=409)
            return {"path": path, "sha256": grant["sha256"]}

    def local_discover(self, *, timeout_seconds: int | None = None) -> dict[str, Any]:
        return self.local_executor.discover() if timeout_seconds is None else self.local_executor.discover(timeout_seconds=timeout_seconds)

    def local_probe(self, serial: str) -> dict[str, Any]:
        return self.local_executor.probe(serial)

    def local_deploy(
        self,
        *,
        serial: str,
        local_file: Path,
        artifact_sha256: str,
        remote_path: str,
        timeout_seconds: int,
        agent_run_id: str = "",
    ) -> dict[str, Any]:
        return self.local_executor.deploy(
            serial=serial,
            local_file=local_file,
            expected_sha256=artifact_sha256,
            remote_path=remote_path,
            timeout_seconds=timeout_seconds,
            agent_run_id=agent_run_id,
        )

    def local_execute_shell(self, *, serial: str, command: str, timeout_seconds: int, agent_run_id: str = "") -> dict[str, Any]:
        return self.local_executor.execute_shell(serial=serial, command=command, timeout_seconds=timeout_seconds, agent_run_id=agent_run_id)

    def local_collect(self, *, serial: str, remote_path: str, destination: Path, timeout_seconds: int, agent_run_id: str = "", max_bytes: int = MAX_COLLECT_BYTES) -> dict[str, Any]:
        return self.local_executor.collect(serial=serial, remote_path=remote_path, destination=destination, timeout_seconds=timeout_seconds, agent_run_id=agent_run_id, max_bytes=max_bytes)
